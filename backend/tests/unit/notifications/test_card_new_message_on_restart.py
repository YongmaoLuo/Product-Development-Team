"""A plan that is started AGAIN must get a NEW card, not an edited one.

The bug
-------
``POST /api/execution/{plan_id}/start`` writes a fresh ``started_at``
on every call, so a plan that is re-run is the SAME plan id with a
DIFFERENT execution behind it. The notifier only ever knew the plan
id: the card it kept in the chat was edited in place, so the operator
watching the second run saw the first run's card — its phase, its task
list, its verdict — while the chat kept showing one card that looked
like it belonged to the run that was actually happening.

The fix
-------
The progress payload the exec-watch already polls carries
``started_at``. A CHANGE in that value means "this plan is running
again", so the card is rotated: the persisted message ids are cleared,
and the next push takes the "no message id yet" branch and sends a
FRESH card. No special case is added to the push path — the rotation
is what makes the existing path send a new card.

What is pinned here
-------------------
  * first sighting (``last_execution_started_at`` is None) records the
    baseline and does NOT rotate — the startup sweep already pushed a
    card, and rotating would throw it away;
  * an unchanged ``started_at`` does not rotate;
  * a changed ``started_at`` rotates: message ids + fingerprint are
    cleared, the new value is persisted, and exactly one
    ``plan_restarted`` event is queued;
  * the rotation is idempotent — a second watch tick with the same
    ``started_at`` rotates nothing;
  * a restarted backend (the state is re-read from disk) with an
    unchanged ``started_at`` does not rotate;
  * the push that follows a rotation calls ``send_message`` (Feishu)
    and a fresh send (Telegram) — never the edit path;
  * the rotation's push is not swallowed by the min-interval gate: the
    card for the new run must go out now.

No network call happens in this file — the Feishu client, the Telegram
transport and the backend reads are all stubbed.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from notifications import feishu_notifier as fn
from notifications.feishu_notifier import FeishuNotifier
from notifications.state_events import KIND_PLAN_RESTARTED

PLAN_ID = "plan-restart"

#: A card that is good enough to fingerprint and to send.
CARD: Dict[str, Any] = {
    "header": {"title": {"content": "plan x"}},
    "elements": [{"tag": "div", "text": {"content": "body"}}],
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def notifier(tmp_path, monkeypatch) -> FeishuNotifier:
    """A notifier whose plans root is ``tmp_path``.

    The card state lives at ``<plans_root>/<plan_id>/notifications/
    feishu_card.json`` and the plans root is resolved per call through
    ``config_paths.resolve_plans_dir()``, which gives
    ``PDT_PLANS_DIR`` precedence — pointing it at ``tmp_path`` is the
    supported redirect and keeps these tests off the operator's live
    ``plans/`` tree.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))
    return FeishuNotifier(coalesce_seconds=0.05, min_interval_seconds=0.0)


def _state_path(tmp_path: Path, plan_id: str = PLAN_ID) -> Path:
    return tmp_path / plan_id / "notifications" / "feishu_card.json"


def _write_state(tmp_path: Path, plan_id: str = PLAN_ID, **fields) -> Dict[str, Any]:
    """Write a card-state JSON by hand and return what was written."""
    data: Dict[str, Any] = {"plan_id": plan_id}
    data.update(fields)
    path = _state_path(tmp_path, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _read_state(tmp_path: Path, plan_id: str = PLAN_ID) -> Dict[str, Any]:
    return json.loads(_state_path(tmp_path, plan_id).read_text(encoding="utf-8"))


def _progress(started_at: Optional[str]) -> Dict[str, Any]:
    """A progress payload shaped like ``/api/execution/{id}/progress``."""
    payload: Dict[str, Any] = {
        "plan_id": PLAN_ID,
        "tasks": [{"id": "1", "status": "completed"}],
        "current": None,
        "counts": {"total": 1, "completed": 1},
    }
    if started_at is not None:
        payload["started_at"] = started_at
    return payload


def _setup(monkeypatch, payload) -> None:
    """Point the watch at a scripted progress feed."""
    monkeypatch.setattr(
        "notifications.feishu_notifier.list_active_plan_ids",
        lambda: [PLAN_ID],
    )
    monkeypatch.setattr(
        "notifications.feishu_notifier.fetch_execution_progress",
        lambda pid, base_url=None: payload,
    )


def _drain(notifier: FeishuNotifier) -> list:
    events = []
    while True:
        try:
            events.append(notifier._queue.get_nowait())
        except queue.Empty:
            return events


def _kinds(events) -> List[str]:
    return [ev.kind for ev in events]


def _peek(notifier: FeishuNotifier) -> list:
    """The queued events, WITHOUT removing them.

    The push tests need the queue to still hold the event when the
    drain runs, so they cannot use ``_drain`` to look at it.
    """
    return list(notifier._queue.queue)


# ---------------------------------------------------------------------------
# The rotation itself
# ---------------------------------------------------------------------------


def test_first_sight_records_the_baseline_without_rotating(
    notifier, tmp_path, monkeypatch,
):
    """A legacy card-state JSON has no ``last_execution_started_at``.

    The plan is running, its card is live, and the watch is seeing it
    for the first time: nothing is rotated, and the card that is in the
    chat stays the card for that run.
    """
    _write_state(tmp_path, message_id="om_old", telegram_message_id="1402",
                 last_fingerprint="sha256:old", chat_id="oc_test")
    _setup(monkeypatch, _progress("2026-11-14T00:00:00Z"))

    notifier._watch_execution_progress()

    assert _kinds(_drain(notifier)) == []
    state = notifier._states[PLAN_ID]
    assert state.message_id == "om_old"
    assert state.telegram_message_id == "1402"
    assert state.last_fingerprint == "sha256:old"
    # The baseline IS recorded — otherwise the next tick would read
    # "first sight" again and the plan would never rotate.
    assert _read_state(tmp_path)["last_execution_started_at"] == "2026-11-14T00:00:00Z"


def test_an_unchanged_started_at_does_not_rotate(
    notifier, tmp_path, monkeypatch,
):
    """Same execution → the live card is still the right card."""
    _write_state(tmp_path, message_id="om_old", telegram_message_id="1402",
                 last_fingerprint="sha256:old", chat_id="oc_test",
                 last_execution_started_at="2026-11-14T00:00:00Z")
    _setup(monkeypatch, _progress("2026-11-14T00:00:00Z"))

    notifier._watch_execution_progress()

    assert _kinds(_drain(notifier)) == []
    state = notifier._states[PLAN_ID]
    assert state.message_id == "om_old"
    assert state.telegram_message_id == "1402"
    assert state.last_fingerprint == "sha256:old"


def test_a_changed_started_at_rotates_the_card(
    notifier, tmp_path, monkeypatch,
):
    """The plan was started again → the old card is retired."""
    _write_state(tmp_path, message_id="om_old", telegram_message_id="1402",
                 last_fingerprint="sha256:old", chat_id="oc_test",
                 last_execution_started_at="2026-11-14T00:00:00Z")
    _setup(monkeypatch, _progress("2026-11-14T01:00:00Z"))

    notifier._watch_execution_progress()

    state = notifier._states[PLAN_ID]
    assert state.message_id is None
    assert state.telegram_message_id is None
    assert state.last_fingerprint is None
    assert state.last_execution_started_at == "2026-11-14T01:00:00Z"

    # ... and it is persisted, so a backend restart does not lose it.
    on_disk = _read_state(tmp_path)
    assert on_disk["last_execution_started_at"] == "2026-11-14T01:00:00Z"
    assert on_disk["message_id"] is None
    assert on_disk["telegram_message_id"] is None
    assert on_disk["last_fingerprint"] is None

    events = _drain(notifier)
    assert _kinds(events) == [KIND_PLAN_RESTARTED]
    assert events[0].plan_id == PLAN_ID
    assert events[0].payload.get("sub_kind") == "execution_restarted"


def test_the_rotation_is_idempotent(notifier, tmp_path, monkeypatch):
    """A second tick with the same ``started_at`` rotates nothing."""
    _write_state(tmp_path, message_id="om_old", chat_id="oc_test",
                 last_execution_started_at="2026-11-14T00:00:00Z")
    _setup(monkeypatch, _progress("2026-11-14T01:00:00Z"))

    notifier._watch_execution_progress()
    first = _drain(notifier)
    assert _kinds(first) == [KIND_PLAN_RESTARTED]

    notifier._watch_execution_progress()
    assert _drain(notifier) == []


def test_a_restarted_backend_does_not_rotate_an_unchanged_execution(
    notifier, tmp_path, monkeypatch,
):
    """The state is re-read from disk; the execution has not moved."""
    _write_state(tmp_path, message_id="om_old", chat_id="oc_test",
                 last_execution_started_at="2026-11-14T00:00:00Z")
    _setup(monkeypatch, _progress("2026-11-14T00:00:00Z"))

    # A fresh notifier == a restarted backend: nothing in memory.
    assert notifier._states == {}
    notifier._watch_execution_progress()

    assert _kinds(_drain(notifier)) == []
    assert notifier._states[PLAN_ID].message_id == "om_old"


def test_a_payload_without_started_at_is_left_alone(
    notifier, tmp_path, monkeypatch,
):
    """No execution stamp → no rotation (the pre-existing behaviour)."""
    _write_state(tmp_path, message_id="om_old", chat_id="oc_test",
                 last_execution_started_at="2026-11-14T00:00:00Z")
    _setup(monkeypatch, _progress(None))

    notifier._watch_execution_progress()

    assert _kinds(_drain(notifier)) == []
    # Nothing was rotated, so the live card is untouched on disk.
    assert _read_state(tmp_path)["message_id"] == "om_old"


# ---------------------------------------------------------------------------
# The push that follows the rotation
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records what the notifier asked the Feishu transport to do."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.updated: List[Any] = []

    def send_message(self, receive_id, content, msg_type="interactive"):
        self.sent.append({"receive_id": receive_id, "content": content,
                          "msg_type": msg_type})
        return "om_new"

    def update_message(self, message_id, content):
        self.updated.append(message_id)
        return "ok"


def _rotation_setup(notifier, tmp_path, monkeypatch, started_at: str) -> None:
    """A plan with a live card for the PREVIOUS run, then re-started."""
    _write_state(tmp_path, message_id="om_old", telegram_message_id="1402",
                 last_fingerprint="sha256:old", chat_id="oc_test",
                 last_execution_started_at="2026-11-14T00:00:00Z",
                 last_push_ts=time.time())
    _setup(monkeypatch, _progress(started_at))
    notifier._client = _FakeClient()
    monkeypatch.setattr(
        notifier, "_rebuild_card",
        lambda plan_id, kinds: (CARD, "executing", None, {"state": {}}),
    )


def test_the_push_after_a_rotation_sends_a_new_feishu_card(
    notifier, tmp_path, monkeypatch,
):
    """The rotated card goes out as a NEW message, not an edit."""
    _rotation_setup(notifier, tmp_path, monkeypatch, "2026-11-14T01:00:00Z")

    notifier._watch_execution_progress()
    assert _kinds(_peek(notifier)) == [KIND_PLAN_RESTARTED]

    notifier._drain_and_push()

    client = notifier._client
    assert client.updated == [], "the old card must not be edited"
    assert len(client.sent) == 1
    assert client.sent[0]["receive_id"] == "oc_test"
    # The new message id is the one that is persisted from now on.
    assert notifier._states[PLAN_ID].message_id == "om_new"


def test_the_push_after_a_rotation_sends_a_new_telegram_message(
    notifier, tmp_path, monkeypatch,
):
    """Same for the mirror: a fresh send, not an edit."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    calls: List[tuple] = []

    def _fake_send(text, config, last_message_id=None):
        calls.append((text, config, last_message_id))
        return 9999

    monkeypatch.setattr(fn, "send_or_edit_telegram", _fake_send)
    _rotation_setup(notifier, tmp_path, monkeypatch, "2026-11-14T01:00:00Z")

    notifier._watch_execution_progress()
    assert _kinds(_peek(notifier)) == [KIND_PLAN_RESTARTED]
    notifier._drain_and_push()

    assert len(calls) == 1
    assert calls[0][2] is None, "the mirror must not edit the old message"
    assert notifier._states[PLAN_ID].telegram_message_id == "9999"


def test_the_rotation_push_is_not_swallowed_by_the_min_interval(
    tmp_path, monkeypatch,
):
    """The card for the new run must go out NOW.

    The plan was pushed seconds ago (the previous run's card), so the
    min-interval gate would hold the new run's card back — the very
    card the operator needs to see the re-run at all.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))
    notifier = FeishuNotifier(
        coalesce_seconds=0.05, min_interval_seconds=3600.0,
    )
    _rotation_setup(notifier, tmp_path, monkeypatch, "2026-11-14T01:00:00Z")

    notifier._watch_execution_progress()
    notifier._drain_and_push()

    assert len(notifier._client.sent) == 1, (
        "the fresh card must not wait for the min-interval"
    )


def test_the_bypass_is_what_lets_the_rotation_through_the_gate(
    tmp_path, monkeypatch,
):
    """Pin the bypass by showing the gate WITHOUT it.

    Without the ``KIND_PLAN_RESTARTED`` bypass the push is gated by the
    min-interval and NO card goes out; with it, one does. This test
    asserts both halves, so dropping the bypass has a visible failure.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))
    notifier = FeishuNotifier(
        coalesce_seconds=0.05, min_interval_seconds=3600.0,
    )
    _rotation_setup(notifier, tmp_path, monkeypatch, "2026-11-14T01:00:00Z")

    notifier._watch_execution_progress()
    events = _peek(notifier)
    assert _kinds(events) == [KIND_PLAN_RESTARTED]

    # Re-run the push with the restart kind filtered out of the dirty
    # set: that is what the gate sees without the bypass.
    notifier._handle_plan(PLAN_ID, set(), final_flush=False)
    assert notifier._client.sent == []

    # ... and with the kind present, the card goes out.
    notifier._handle_plan(PLAN_ID, {KIND_PLAN_RESTARTED}, final_flush=False)
    assert len(notifier._client.sent) == 1
