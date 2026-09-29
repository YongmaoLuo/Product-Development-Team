"""2026-09-15 — round-start liveness bookkeeping.

Both round starters (the auto-loop's ``for round_num`` body and the
post-repair re-entry in ``_on_repair_complete``) used to leave the
PREVIOUS round's terminal status in ``_verification_state`` until the next
terminal write. Because ``/api/system/active`` only counts a verification
whose status is ``running`` / ``repairing`` / ``rerunning``, the operator
saw ``total_active: 0`` — and the card kept the old verdict — for the
ENTIRE duration of round N ≥ 2 while VPs were actively running. Observed
live on a production plan: round 2 was executing (attempt logs written every
few seconds) while the liveness endpoint reported nothing active.

``_mark_verification_round_running`` is the shared helper both starters
now call; it is module-level so it can be tested directly (the callers
are unreachable closures).
"""

from __future__ import annotations

import threading

import pytest

import server


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    for pid in ("p-roundstart", "p-roundstart-repairing"):
        server._verification_state.pop(pid, None)


def test_round_start_flips_previous_terminal_status_to_running():
    server._verification_state["p-roundstart"] = {
        "verification_status": "failed",
        "verification_round": 1,
        "stop_reason": "Illegal transition from 'failed' to 'verification_failed'",
        "updated_at": "2026-09-15T05:25:24",
    }

    server._mark_verification_round_running("p-roundstart", 2)

    state = server._verification_state["p-roundstart"]
    assert state["verification_status"] == "running"
    assert state["verification_round"] == 2
    assert state["stop_reason"] is None, (
        "a stale stop reason from round N-1 must not leak into round N"
    )
    assert state["updated_at"] != "2026-09-15T05:25:24"


def test_round_start_creates_the_entry_when_absent():
    server._mark_verification_round_running("p-roundstart", 3)

    state = server._verification_state["p-roundstart"]
    assert state["verification_status"] == "running"
    assert state["verification_round"] == 3


def test_round_start_is_visible_to_the_liveness_endpoint(monkeypatch):
    """The contract that actually matters: after a round starts,
    /api/system/active must report it as an active verification."""
    server._verification_state["p-roundstart-repairing"] = {
        "verification_status": "loop_stopped",
        "verification_round": 1,
        "stop_reason": "verification_log_stale",
    }

    payload = server.get_active_tasks()
    assert payload["verification_running"] == 0, (
        "terminal statuses must not count as active"
    )

    server._mark_verification_round_running("p-roundstart-repairing", 4)

    payload = server.get_active_tasks()
    assert payload["verification_running"] == 1
    entry = next(
        d for d in payload["details"]
        if d["plan_id"] == "p-roundstart-repairing"
    )
    assert entry["round"] == 4
    assert entry["status"] == "running"


def test_round_start_rebinds_the_liveness_thread():
    """The liveness handle must follow the thread that drives the round.

    The post-repair re-entry runs ``start_verification_cycle`` on the
    repair-watcher thread and never goes through
    ``POST /api/verification/{id}/start``, so the handle kept pointing at
    round 1's orchestrator — finished since round 1 ended. HeartbeatMonitor
    polls ``thread.is_alive()`` and treats a dead handle as immediate,
    unambiguous proof of death, so it terminally failed the plan five
    seconds into the round.

    End-to-end coverage of the same regression lives in
    ``tests/test_verification_watchdog_fix.py``.
    """
    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    assert not finished.is_alive()

    server._verification_state["p-roundstart"] = {
        "verification_status": "running",
        "verification_round": 1,
        "thread": finished,
    }

    server._mark_verification_round_running("p-roundstart", 2)

    bound = server._verification_state["p-roundstart"]["thread"]
    assert bound is threading.current_thread(), (
        "the watchdog polls state['thread']; it must be the thread now "
        "driving round 2"
    )
    assert bound.is_alive()
