"""A plan that reaches a TERMINAL phase must get a NEW card, not an edited one.

The bug
-------
A plan that runs for hours owns ONE card, and that card is edited in
place for the whole run.  Meanwhile other plans are started, restarted
and finished, and each of those pushes its own card — so the
long-running plan's card drifts UP the chat and out of sight.  When it
finally finishes, the result is written into that same old message,
which is nowhere near the bottom of the chat any more: the operator
never sees "it finished".

The fix
-------
The phase that ends a plan arrives with the event that says so (the
routing CAS publishes the new ``current_phase``).  A terminal phase
therefore rotates the card: the persisted message ids are cleared, and
the next push takes the "no message id yet" branch and sends a FRESH
card — at the bottom of the chat.  No special case is added to the
push path; the rotation is what makes the existing path send a new
card.

What is pinned here
-------------------
  * the terminal phase set is the SAME set the plans listing filters
    on (``routes.plans.TERMINAL_PHASES``) — one notion of "over";
  * the first sighting of a plan that is ALREADY terminal records the
    baseline and does NOT rotate (a notifier that just started must
    not post a fresh card for every finished plan in the tree);
  * a non-terminal phase is recorded, and a terminal phase after it
    rotates: the message ids and the fingerprint are cleared and the
    terminal phase is recorded;
  * the same terminal phase seen again rotates nothing — the rotation
    is idempotent;
  * "running → completed → running → completed" rotates twice: the
    second ending gets its own card;
  * the restart rotation still works and CLEARS the recorded terminal
    phase, so the new run's ending is not blocked by the old run's;
  * the push that follows a terminal rotation calls ``send_message``
    (Feishu) and a fresh send (Telegram) — never the edit path;
  * the terminal card is not swallowed by the min-interval gate: a
    plan that has just finished gets no further events, so a held
    card is a card that never arrives.

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
from notifications.feishu_notifier import TERMINAL_PHASES, FeishuNotifier
from notifications.state_events import KIND_PLAN_PHASE_CHANGED, StateEvent

PLAN_ID = "plan-terminal"

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

    ``min_interval_seconds=0.0`` turns the per-plan gate off so the
    tests exercise the ROTATION and not the throttle; the gate has its
    own case below.
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


def _setup_watch(monkeypatch, payload) -> None:
    """Point the exec watch at a scripted progress feed."""
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


def _live_card(tmp_path: Path, **extra) -> None:
    """A plan with a card that is live in the chat right now."""
    _write_state(
        tmp_path,
        message_id="om_old",
        telegram_message_id="1402",
        last_fingerprint="sha256:old",
        chat_id="oc_test",
        **extra,
    )


def _rebuild_for(monkeypatch, notifier: FeishuNotifier, phase: str) -> None:
    """Make the card rebuild report ``phase`` for this plan."""
    monkeypatch.setattr(
        notifier,
        "_rebuild_card",
        lambda plan_id, kinds: (CARD, phase, None, {"state": {}}),
    )


# ---------------------------------------------------------------------------
# The terminal phase set
# ---------------------------------------------------------------------------


def test_the_terminal_phase_set_is_the_one_the_listing_filters_on():
    """One notion of "over" — the card and the listing must agree.

    ``routes.plans.TERMINAL_PHASES`` is the set the plans listing
    drops terminal plans by.  The notifier cannot import it (the
    ``routes`` package imports ``server``, which imports the notifier
    back), so it keeps its own copy — and a copy that drifts is a plan
    that is "over" to the chat and "alive" to the listing.
    """
    import server  # noqa: F401  — imported first: routes.plans imports it
    from routes import plans

    assert TERMINAL_PHASES == set(plans.TERMINAL_PHASES), (
        "the notifier's terminal phase set has drifted from "
        "routes.plans.TERMINAL_PHASES"
    )


# ---------------------------------------------------------------------------
# The rotation itself
# ---------------------------------------------------------------------------


def test_first_sighting_of_an_already_terminal_plan_does_not_rotate(
    notifier, tmp_path,
):
    """A plan that was ALREADY over when the notifier first looked.

    The card in the chat is the one for that finished run, and the
    notifier has just started: it must not post a fresh card for every
    finished plan in the tree.  The baseline is recorded, the card is
    left alone.
    """
    _live_card(tmp_path)

    rotated = notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed")

    assert rotated is False
    state = notifier._states[PLAN_ID]
    assert state.message_id == "om_old"
    assert state.telegram_message_id == "1402"
    assert state.last_fingerprint == "sha256:old"
    # The baseline IS recorded — otherwise the next ending of this
    # plan would be treated as a first sighting too.
    assert state.last_terminal_phase == "completed"
    assert state.last_observed_phase == "completed"

    on_disk = _read_state(tmp_path)
    assert on_disk["last_terminal_phase"] == "completed"
    assert on_disk["message_id"] == "om_old"


def test_a_terminal_phase_after_a_non_terminal_one_rotates(
    notifier, tmp_path,
):
    """Running → completed: the card is retired, a fresh one is due."""
    _live_card(tmp_path)

    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "executing") is False
    # Still the same card — nothing has ended yet.
    assert notifier._states[PLAN_ID].message_id == "om_old"

    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed") is True

    state = notifier._states[PLAN_ID]
    assert state.message_id is None
    assert state.telegram_message_id is None
    assert state.last_fingerprint is None
    assert state.last_terminal_phase == "completed"
    assert state.last_observed_phase == "completed"

    # ... and it is persisted, so a backend restart does not lose it.
    on_disk = _read_state(tmp_path)
    assert on_disk["message_id"] is None
    assert on_disk["telegram_message_id"] is None
    assert on_disk["last_fingerprint"] is None
    assert on_disk["last_terminal_phase"] == "completed"


def test_the_same_terminal_phase_seen_again_does_not_rotate(
    notifier, tmp_path,
):
    """The rotation is idempotent — a second tick rotates nothing."""
    _live_card(tmp_path)

    notifier._maybe_rotate_card_for_terminal(PLAN_ID, "executing")
    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed") is True

    # The watch and the push path both look at the same plan over and
    # over; a second look at the same ending must not clear the ids
    # a second time (the push in between needs them).
    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed") is False
    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed") is False

    state = notifier._states[PLAN_ID]
    assert state.message_id is None
    assert state.last_terminal_phase == "completed"


def test_a_second_ending_rotates_again(
    notifier, tmp_path,
):
    """running → completed → running → completed: two rotations.

    The plan was started again after it finished, so the second ending
    is a different ending and gets its own card.  A non-terminal phase
    clears the recorded terminal phase — that is what lets the second
    ending through.
    """
    _live_card(tmp_path)

    notifier._maybe_rotate_card_for_terminal(PLAN_ID, "executing")
    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed") is True
    assert notifier._states[PLAN_ID].last_terminal_phase == "completed"

    # Started again — the plan is alive, so the old ending is history.
    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "executing") is False
    assert notifier._states[PLAN_ID].last_terminal_phase is None

    # ... and finished again: a second rotation.
    assert notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed") is True
    assert notifier._states[PLAN_ID].last_terminal_phase == "completed"


def test_a_non_terminal_phase_clears_the_recorded_terminal_phase(
    notifier, tmp_path,
):
    """The recorded ending is history once the plan is alive again."""
    _live_card(tmp_path)

    notifier._maybe_rotate_card_for_terminal(PLAN_ID, "executing")
    notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed")
    assert notifier._states[PLAN_ID].last_terminal_phase == "completed"

    notifier._maybe_rotate_card_for_terminal(PLAN_ID, "verification_running")

    assert notifier._states[PLAN_ID].last_terminal_phase is None
    assert _read_state(tmp_path)["last_terminal_phase"] is None


def test_the_restart_rotation_still_works_and_clears_the_terminal_phase(
    notifier, tmp_path, monkeypatch,
):
    """The two rotations coexist.

    A plan that was started again is running a NEW run, so the ending
    recorded for the old run must not block the new run's ending.  The
    restart rotation clears it — and it still clears the message ids,
    which is what it was for.
    """
    _live_card(tmp_path,
               last_execution_started_at="2026-11-14T00:00:00Z",
               last_observed_phase="executing")
    notifier._maybe_rotate_card_for_terminal(PLAN_ID, "completed")
    assert notifier._states[PLAN_ID].last_terminal_phase == "completed"

    _setup_watch(monkeypatch, _progress("2026-11-14T01:00:00Z"))
    notifier._watch_execution_progress()

    state = notifier._states[PLAN_ID]
    # The restart still rotates the card ...
    assert state.message_id is None
    assert state.telegram_message_id is None
    assert state.last_fingerprint is None
    assert state.last_execution_started_at == "2026-11-14T01:00:00Z"
    # ... and the old run's ending no longer blocks the new one.
    assert state.last_terminal_phase is None
    assert _read_state(tmp_path)["last_terminal_phase"] is None


# ---------------------------------------------------------------------------
# The push that follows the rotation
# ---------------------------------------------------------------------------


def test_the_push_after_a_rotation_sends_a_new_feishu_card(
    notifier, tmp_path, monkeypatch,
):
    """The rotated card goes out as a NEW message, not an edit.

    This is the whole point of the rotation, exercised through the
    production path (``_handle_plan`` → the push).
    """
    _live_card(tmp_path, last_observed_phase="executing")
    _rebuild_for(monkeypatch, notifier, "completed")
    notifier._client = _FakeClient()

    notifier._handle_plan(PLAN_ID, {KIND_PLAN_PHASE_CHANGED}, final_flush=False)

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
    _live_card(tmp_path, last_observed_phase="executing")
    _rebuild_for(monkeypatch, notifier, "completed")
    notifier._client = _FakeClient()

    notifier._handle_plan(PLAN_ID, {KIND_PLAN_PHASE_CHANGED}, final_flush=False)

    assert len(calls) == 1
    assert calls[0][2] is None, "the mirror must not edit the old message"
    assert notifier._states[PLAN_ID].telegram_message_id == "9999"


def test_the_terminal_card_is_not_swallowed_by_the_min_interval(
    tmp_path, monkeypatch,
):
    """The card for the ending must go out NOW.

    The plan was pushed seconds ago (the last progress card), so the
    min-interval gate would hold the terminal card back — and a plan
    that has just finished gets no further events, so a held card is a
    card that never arrives at all.  The phase that ends the plan
    arrives WITH the event that says so, and a rotation is a bypass:
    the card for the ending is the one the operator is waiting for.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))
    notifier = FeishuNotifier(
        coalesce_seconds=0.05, min_interval_seconds=3600.0,
    )
    _live_card(tmp_path, last_observed_phase="executing",
               last_push_ts=time.time())
    _rebuild_for(monkeypatch, notifier, "completed")
    notifier._client = _FakeClient()

    # The event the routing CAS publishes when the plan ends: the new
    # phase travels with it.  ``_on_event`` is the bus handler, so this
    # is the path the event really takes.
    notifier._on_event(StateEvent(
        kind=KIND_PLAN_PHASE_CHANGED,
        plan_id=PLAN_ID,
        payload={"phase": "completed"},
    ))
    notifier._drain_and_push()

    assert len(notifier._client.sent) == 1, (
        "the terminal card must not wait for the min-interval"
    )
    assert notifier._client.updated == []


def test_a_plan_that_is_gated_and_has_no_phase_hint_is_not_pushed(
    tmp_path, monkeypatch,
):
    """The negative control for the bypass above.

    Without a phase hint there is nothing to rotate on, so the gate
    holds the push back — exactly as it did before this change.  The
    bypass is the ROTATION, not a hole in the gate.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))
    notifier = FeishuNotifier(
        coalesce_seconds=0.05, min_interval_seconds=3600.0,
    )
    _live_card(tmp_path, last_observed_phase="executing",
               last_push_ts=time.time())
    _rebuild_for(monkeypatch, notifier, "completed")
    notifier._client = _FakeClient()

    # No ``phase`` in the payload — the gate applies.
    notifier._on_event(StateEvent(
        kind=KIND_PLAN_PHASE_CHANGED,
        plan_id=PLAN_ID,
        payload={"sub_kind": "exec_progress_watch"},
    ))
    notifier._drain_and_push()

    assert notifier._client.sent == []
    assert notifier._client.updated == []
