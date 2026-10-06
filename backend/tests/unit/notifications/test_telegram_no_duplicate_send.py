"""An unchanged Telegram edit must not open a second message.

2026-10-05, from the notifier log on a real plan:

    22:39:20  patched card om_x100b6... for plan=20261004-PDT-...
    22:39:21  telegram_edit_no_change ... status_code=400
              body={"ok":false,"error_code":400,
                    "description":"Bad Request: message is not modified:
                    specified new message content and reply markup are
                    exactly the same as a current content and reply
                    markup of the message"}
    22:39:23  telegram_send_ok ... message_id=2242

Telegram answered 400 because the text was already correct — the edit
"failed" only in the sense that there was nothing to do. That is
indistinguishable by status code from a real rejection (message
deleted, bot removed from the chat), and ``send_or_edit_telegram``
treated both as "the message is gone, send a fresh one". So a render
that happened to be byte-identical to the live message produced a NEW
message, and the mirror accumulated duplicates.

The two are separated by the response body, not the status code.

Pinned here:

  * ``message is not modified`` is recognised as a success;
  * it keeps the existing message id and does not send;
  * a genuine 400 (different description) still falls back to a send —
    the message really may be gone;
  * a transport failure still falls back to a send;
  * the boolean wrapper keeps its historical meaning (True only when
    the edit was applied), so existing callers are unaffected.
"""

from __future__ import annotations

from typing import Optional

import pytest

from notifications import telegram_client as tc


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


_CONFIG = {"enabled": True, "bot_token": "tok", "chat_id": "123"}


def _no_change() -> _Resp:
    return _Resp(400, '{"ok":false,"error_code":400,"description":'
                     '"Bad Request: message is not modified: specified new '
                     'message content and reply markup are exactly the same '
                     'as a current content and reply markup of the message"}')


@pytest.fixture
def no_network(monkeypatch):
    """Any HTTP attempt is a test failure — these paths must not dial out."""
    def _boom(*a, **k):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(tc, "_post", _boom)
    monkeypatch.setattr(tc, "send_to_telegram", _boom)


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


def test_no_change_is_recognised():
    assert tc._is_unchanged(_no_change()) is True


def test_a_different_400_is_not_no_change():
    """Same status code, different meaning — this is how the two are told
    apart."""
    other = _Resp(400, '{"ok":false,"error_code":400,"description":'
                       '"Bad Request: message to edit not found"}')

    assert tc._is_unchanged(other) is False


def test_a_200_is_not_no_change():
    assert tc._is_unchanged(_Resp(200, "{}")) is False


def test_no_response_is_not_no_change():
    assert tc._is_unchanged(None) is False


# ---------------------------------------------------------------------------
# send_or_edit — the behaviour that produced the duplicates
# ---------------------------------------------------------------------------


def test_unchanged_text_keeps_the_same_message_id(monkeypatch, no_network):
    monkeypatch.setattr(tc, "_post", lambda *a, **k: _no_change())

    live = tc.send_or_edit_telegram("same text", _CONFIG, last_message_id=42)

    assert live == 42, "a new message was opened for an unchanged card"


def test_unchanged_text_does_not_send(monkeypatch):
    sent: list = []
    monkeypatch.setattr(tc, "_post", lambda *a, **k: _no_change())
    monkeypatch.setattr(
        tc, "send_to_telegram",
        lambda text, config: sent.append(text) or 99,
    )

    tc.send_or_edit_telegram("same text", _CONFIG, last_message_id=42)

    assert sent == [], "fell back to a send for content that was already live"


def test_a_real_400_still_falls_back_to_a_send(monkeypatch):
    """The message really may be gone. Suppressing the send here would
    lose the card entirely."""
    monkeypatch.setattr(
        tc, "_post",
        lambda *a, **k: _Resp(400, '{"description":"message to edit not found"}'),
    )
    monkeypatch.setattr(
        tc, "send_to_telegram", lambda text, config: 99,
    )

    assert tc.send_or_edit_telegram("new", _CONFIG, last_message_id=42) == 99


def test_a_transport_failure_still_falls_back_to_a_send(monkeypatch):
    monkeypatch.setattr(tc, "_post", lambda *a, **k: None)
    monkeypatch.setattr(tc, "send_to_telegram", lambda text, config: 99)

    assert tc.send_or_edit_telegram("new", _CONFIG, last_message_id=42) == 99


def test_a_successful_edit_keeps_the_same_message_id(monkeypatch):
    monkeypatch.setattr(tc, "_post", lambda *a, **k: _Resp(200, '{"ok":true}'))

    assert tc.send_or_edit_telegram("new", _CONFIG, last_message_id=42) == 42


def test_no_previous_message_sends_fresh(monkeypatch):
    monkeypatch.setattr(
        tc, "send_to_telegram", lambda text, config: 7,
    )

    assert tc.send_or_edit_telegram("new", _CONFIG, last_message_id=None) == 7


# ---------------------------------------------------------------------------
# Boolean wrapper compatibility
# ---------------------------------------------------------------------------


def test_the_bool_wrapper_is_true_only_for_an_applied_edit(monkeypatch):
    monkeypatch.setattr(tc, "_post", lambda *a, **k: _Resp(200, "{}"))
    assert tc.edit_telegram_message("t", _CONFIG, 42) is True

    monkeypatch.setattr(tc, "_post", lambda *a, **k: _no_change())
    assert tc.edit_telegram_message("t", _CONFIG, 42) is False


def test_disabled_config_short_circuits(monkeypatch):
    """Both halves of the pair short-circuit on ``enabled``; nothing
    reaches the network and the caller gets ``None``."""
    def _boom(*a, **k):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(tc, "_post", _boom)

    assert tc.send_or_edit_telegram(
        "t", {"enabled": False}, last_message_id=42,
    ) is None
