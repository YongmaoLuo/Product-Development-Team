"""The three-state result of ``FeishuClient.update_message``.

The 2026-10-25 03:32:11 incident
-------------------------------
A Feishu card PATCH failed once, with an SSL EOF. ``update_message``
answered ``False`` — the very same value it answers when the card is
gone — and the notifier read that ``False`` as "this card is dead",
cleared the id and sent a SECOND card. The first card stayed in the
chat forever, so the operator saw two cards with the same plan name.

The fix splits the one boolean into three states:

  * ``UPDATE_OK``        — the card was patched in place.
  * ``UPDATE_GONE``      — the card is provably dead (Feishu answered
                            400 / 403 / 404). ONLY this state may
                            replace the card with a fresh one.
  * ``UPDATE_TRANSIENT`` — the update did not happen, but the card is
                            NOT known to be dead: 429, 5xx, a network /
                            SSL / timeout failure, or a non-zero
                            business code. The card is very likely
                            still alive, so the caller keeps it and
                            retries later instead of sending a
                            duplicate.

A second half of the fix lives in the notifier: the fingerprint only
advances on a SUCCESSFUL push. Otherwise one transient failure is
enough for the fingerprint dedup to swallow the retry, and the card
silently stops updating for good.

No network call happens in this file — ``urlopen`` and the Feishu
client itself are stubbed out.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from notifications.feishu_client import (
    UPDATE_GONE,
    UPDATE_OK,
    UPDATE_TRANSIENT,
    FeishuClient,
)
from notifications.feishu_notifier import FeishuNotifier, PlanCardState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client() -> FeishuClient:
    """A ``FeishuClient`` that talks to nothing.

    ``__init__`` is skipped on purpose: it imports ``lark_oapi``, reads
    the app secret out of the OS keychain and demands env vars. None
    of that is under test here — only the classification of the PATCH
    outcome is.
    """
    client = FeishuClient.__new__(FeishuClient)
    client.get_tenant_access_token = lambda *a, **k: "t-token"  # type: ignore[method-assign]
    return client


def _http_error(code: int) -> urllib.error.HTTPError:
    """A bare ``HTTPError`` — the shape ``urlopen`` raises."""
    return urllib.error.HTTPError(
        "https://open.feishu.cn/open-apis/im/v1/messages/om_1",
        code, "boom", None, None,
    )


def _response(body: Dict[str, Any]) -> MagicMock:
    """A ``urlopen`` context manager answering with ``body`` as JSON."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    return ctx


#: A card that is good enough to fingerprint and to send.
CARD: Dict[str, Any] = {
    "header": {"title": {"content": "plan x"}},
    "elements": [{"tag": "div", "text": {"content": "body"}}],
}


def _notifier(
    update_result: str,
) -> Tuple[FeishuNotifier, PlanCardState, MagicMock]:
    """A notifier whose Feishu client is a mock answering ``update_result``."""
    notifier = FeishuNotifier()
    client = MagicMock()
    client.update_message.return_value = update_result
    notifier._client = client
    state = PlanCardState(
        plan_id="plan-x", chat_id="chat-x", message_id="om_old",
    )
    notifier._states["plan-x"] = state
    return notifier, state, client


def _run_handle_plan(
    notifier: FeishuNotifier,
    plan_id: str,
    push_result: bool,
    calls: List[str],
) -> None:
    """Run ``_handle_plan`` with the card build and the transports stubbed.

    ``calls`` collects one entry per ``_push_to_feishu`` call, so the
    test can tell "the card was pushed again" from "the card was
    swallowed by the fingerprint dedup".
    """

    def _push(chat_id: str, state: PlanCardState, card: Dict[str, Any]) -> bool:
        calls.append(state.plan_id)
        return push_result

    with patch.object(
        notifier, "_rebuild_card",
        return_value=(CARD, "completed", "passed", {}),
    ), patch.object(
        notifier, "_push_to_feishu", side_effect=_push,
    ), patch.object(
        notifier, "_push_to_telegram", return_value=True,
    ), patch.object(
        notifier, "_write_card_snapshot",
    ), patch(
        "notifications.feishu_notifier._save_card_state",
    ):
        notifier._handle_plan(plan_id, set(), final_flush=False)


# ---------------------------------------------------------------------------
# 1. A provably dead card is GONE (400 / 403 / 404)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 403, 404])
def test_update_message_returns_gone_for_dead_card_codes(code: int) -> None:
    """400 / 403 / 404 mean the card cannot be edited any more."""
    client = _client()
    with patch(
        "notifications.feishu_client.urllib.request.urlopen",
        side_effect=_http_error(code),
    ):
        assert client.update_message("om_1", "{}") == UPDATE_GONE


# ---------------------------------------------------------------------------
# 2. A retryable HTTP status is TRANSIENT (429 / 5xx)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502])
def test_update_message_returns_transient_for_retryable_codes(code: int) -> None:
    """Rate limits and server errors say nothing about the card's life."""
    client = _client()
    with patch(
        "notifications.feishu_client.urllib.request.urlopen",
        side_effect=_http_error(code),
    ):
        assert client.update_message("om_1", "{}") == UPDATE_TRANSIENT


@pytest.mark.parametrize("code", [409, 422])
def test_update_message_returns_transient_for_other_4xx(code: int) -> None:
    """Any other 4xx is not proof that the card is gone."""
    client = _client()
    with patch(
        "notifications.feishu_client.urllib.request.urlopen",
        side_effect=_http_error(code),
    ):
        assert client.update_message("om_1", "{}") == UPDATE_TRANSIENT


# ---------------------------------------------------------------------------
# 3. A transport failure is TRANSIENT (the 03:32 SSL EOF itself)
# ---------------------------------------------------------------------------


def test_update_message_returns_transient_on_ssl_eof() -> None:
    """The exact 2026-10-25 03:32:11 failure — an SSL EOF, not a dead card."""
    client = _client()
    err = urllib.error.URLError(
        ssl.SSLError("EOF occurred in violation of protocol"),
    )
    with patch(
        "notifications.feishu_client.urllib.request.urlopen",
        side_effect=err,
    ):
        assert client.update_message("om_1", "{}") == UPDATE_TRANSIENT


# ---------------------------------------------------------------------------
# 4. The business code decides OK vs TRANSIENT
# ---------------------------------------------------------------------------


def test_update_message_returns_ok_when_business_code_is_zero() -> None:
    """HTTP 200 with ``code == 0`` is a patched card."""
    client = _client()
    with patch(
        "notifications.feishu_client.urllib.request.urlopen",
        return_value=_response({"code": 0, "msg": "success"}),
    ):
        assert client.update_message("om_1", "{}") == UPDATE_OK


def test_update_message_returns_transient_when_business_code_is_not_zero() -> None:
    """HTTP 200 with a non-zero code did NOT patch the card."""
    client = _client()
    with patch(
        "notifications.feishu_client.urllib.request.urlopen",
        return_value=_response({"code": 230002, "msg": "message not found"}),
    ):
        assert client.update_message("om_1", "{}") == UPDATE_TRANSIENT


# ---------------------------------------------------------------------------
# 5. A TRANSIENT failure must not replace the card
# ---------------------------------------------------------------------------


def test_transient_update_keeps_the_card_and_sends_nothing() -> None:
    """The duplicate-card bug: a transient failure sent a second card."""
    notifier, state, client = _notifier(UPDATE_TRANSIENT)

    ok = notifier._push_to_feishu("chat-x", state, CARD)

    assert ok is False
    client.send_message.assert_not_called()
    assert state.message_id == "om_old", (
        "a transient failure must keep the existing card id — the card is "
        "most likely still alive in the chat"
    )


def test_ok_update_patches_in_place_without_sending_a_new_card() -> None:
    """The happy path is unchanged: patch, never re-send."""
    notifier, state, client = _notifier(UPDATE_OK)

    ok = notifier._push_to_feishu("chat-x", state, CARD)

    assert ok is True
    client.send_message.assert_not_called()
    assert state.message_id == "om_old"


# ---------------------------------------------------------------------------
# 6. A GONE card is replaced exactly once
# ---------------------------------------------------------------------------


def test_gone_update_sends_a_fresh_card_and_replaces_the_id() -> None:
    """A provably dead card is the ONE case that earns a replacement."""
    notifier, state, client = _notifier(UPDATE_GONE)
    client.send_message.return_value = "om_new"

    ok = notifier._push_to_feishu("chat-x", state, CARD)

    assert ok is True
    assert client.send_message.call_count == 1
    assert state.message_id == "om_new", (
        "the state must point at the new card, otherwise the next event "
        "would try to patch the dead one again"
    )


# ---------------------------------------------------------------------------
# 7. A failed push must not burn the fingerprint
# ---------------------------------------------------------------------------


def test_transient_failure_does_not_advance_last_fingerprint() -> None:
    """A failed push must leave ``last_fingerprint`` where it was."""
    notifier = FeishuNotifier()
    state = PlanCardState(
        plan_id="plan-x", chat_id="chat-x", message_id="om_old",
    )
    notifier._states["plan-x"] = state
    state.last_fingerprint = "an-old-fingerprint"

    calls: List[str] = []
    _run_handle_plan(notifier, "plan-x", False, calls)

    assert calls == ["plan-x"], "the push must have been attempted"
    assert state.last_fingerprint == "an-old-fingerprint", (
        "a failed push must NOT advance last_fingerprint — otherwise the "
        "fingerprint dedup swallows the retry and the card update is lost"
    )


def test_same_card_is_pushed_again_after_a_transient_failure() -> None:
    """The retry the fingerprint must not swallow."""
    notifier = FeishuNotifier()
    state = PlanCardState(
        plan_id="plan-x", chat_id="chat-x", message_id="om_old",
    )
    notifier._states["plan-x"] = state
    state.last_fingerprint = "an-old-fingerprint"

    calls: List[str] = []
    _run_handle_plan(notifier, "plan-x", False, calls)

    # Open the min-interval gate so the fingerprint dedup is the ONLY
    # thing that can stop the second push.
    state.last_push_ts = time.time() - 61
    _run_handle_plan(notifier, "plan-x", False, calls)

    assert calls == ["plan-x", "plan-x"], (
        "the same card must be pushed again after a transient failure — "
        f"got {calls}"
    )


def test_successful_push_advances_the_fingerprint_and_dedups_the_next_one() -> None:
    """The control: a successful push DOES advance the fingerprint.

    Without this, the two tests above would also pass against a
    notifier that never deduplicated anything.
    """
    notifier = FeishuNotifier()
    state = PlanCardState(
        plan_id="plan-x", chat_id="chat-x", message_id="om_old",
    )
    notifier._states["plan-x"] = state
    state.last_fingerprint = "an-old-fingerprint"

    calls: List[str] = []
    _run_handle_plan(notifier, "plan-x", True, calls)

    assert state.last_fingerprint != "an-old-fingerprint", (
        "a successful push must advance last_fingerprint"
    )

    # Open the min-interval gate so the fingerprint dedup is the ONLY
    # thing that can stop the second push.
    state.last_push_ts = time.time() - 61
    _run_handle_plan(notifier, "plan-x", True, calls)

    assert calls == ["plan-x"], (
        "an unchanged card must be deduped after a successful push — "
        f"got {calls}"
    )
