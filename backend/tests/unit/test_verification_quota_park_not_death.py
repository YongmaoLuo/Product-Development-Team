"""A quota-parked execution is NOT a dead verification round.

Background
----------
2026-11-15 09:12: the repair execution of plan
``20261101-scheduler-page-truth`` ran out of provider capacity and was
parked — ``exec_status='waiting_for_quota'``.  Twenty-one seconds later
the verification watchdog wrote ``verification_thread_died_unexpectedly``
onto the plan, and the plan was closed as failed.

The reason is structural.  The death latch asked ONE question before it
declared a round dead: *"is a background execution in flight for this
plan?"* — and a parked execution is not in flight.  In-flight means
``status == 'running'`` with a live process; a parked execution has
``status == 'waiting_for_quota'`` and no process at all.  So the
hand-off branch was skipped, the ended thread was read as a death, and a
plan that was WAITING was written off.  A park is not a failure (arch
decision R1), and the latch has to know that.

What this file pins
-------------------
1. a parked execution + an ended verification thread is NOT a death: the
   round keeps its ``running`` status, no death sequence runs, and no
   terminal write happens;
2. an ended thread with NO park is still a death — the negative control,
   so the fix cannot quietly turn the latch off;
3. a park probe that RAISES is still a death: a doubt about the park
   must never become a licence to leave a dead round running.

Hermeticity
------------
Everything lives inside ``tmp_path``: the plan directory and the state
database.  The HITL sweep is stubbed out — it is orthogonal to the
death latch, and it would otherwise reach for the state database of the
machine the test runs on.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as server_mod  # noqa: E402
import verification_loop as vl  # noqa: E402


#: The plan the watchdog is watching.
PLAN_ID = "20261115-quota-park-not-death"

#: The status a parked execution carries — the same word the execution
#: path parks under (``plan_status.QUOTA_WAIT_PHASE``).
PARKED_STATUS = "waiting_for_quota"

#: The reason the latch writes when a round really did die.
THREAD_DEATH = "verification_thread_died_unexpectedly"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dead_thread() -> threading.Thread:
    """The orchestrator's own handle after its round ended."""
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    assert not thread.is_alive()
    return thread


def _running_state() -> Dict[str, Any]:
    """A verification state that says "a round is running"."""
    return {
        "verification_status": "running",
        "verification_round": 1,
        "thread": _dead_thread(),
        "started_at": "2026-11-15T09:12:00",
        # Well past the startup grace period, so the staleness ladder is
        # allowed to fire at all.
        "started_at_ts": time.time() - 7200.0,
    }


def _seed_execution_row(
    db_path: Path, plan_id: str, exec_status: str, stop_reason: Any = None,
) -> None:
    """Write the plan's ``plan_execution`` row — the durable half of a park."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(str(db_path))
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, exec_status, stop_reason, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (plan_id, exec_status, stop_reason, "2026-11-15T09:12:00Z"),
        )
    finally:
        conn.close()


def _parked_execution_in_memory() -> None:
    """The in-memory half of a park: the executor's own record."""
    server_mod._execution_state[PLAN_ID] = {
        "status": PARKED_STATUS,
        "stop_reason": PARKED_STATUS,
        "ended_at": None,
        "process": None,
    }


def _dead_execution_in_memory() -> None:
    """An execution whose process is gone: ``running``, but nothing alive.

    This is the negative control's in-memory shape — and it is also the
    shape the parked case would have if the park were ignored: the row
    says ``running`` and no process is alive, so the record does not
    read as in flight.
    """
    server_mod._execution_state[PLAN_ID] = {
        "status": "running",
        "stop_reason": None,
        "ended_at": "2026-11-15T09:12:21",
        "process": None,
    }


@pytest.fixture
def watchdog_env(tmp_path, monkeypatch):
    """A plan whose verification round has ended, inside the test's tree."""
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir()

    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_root)
    monkeypatch.setattr(
        server_mod, "_state_db_path", lambda *a, **k: str(db_path),
    )
    # The HITL sweep is orthogonal to the death latch, and it would
    # otherwise reach for the state database of the machine the test
    # runs on.
    monkeypatch.setattr(vl, "_expire_hitl_decisions", lambda now: 0)
    monkeypatch.setattr(
        vl, "_handle_hitl_strict_timeout", lambda plan_id, now: None,
    )
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    server_mod._verification_state[PLAN_ID] = _running_state()

    yield {"plan_dir": plan_dir, "db_path": db_path, "plans_root": plans_root}

    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._execution_state.pop(PLAN_ID, None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)


@pytest.fixture
def terminal_writes(monkeypatch):
    """Record the terminal writes the death path makes — and only those.

    The writes are observed, not performed: the test is about WHICH
    verdict the latch reaches, not about what the write does to the
    database.
    """
    writes: List[Dict[str, Any]] = []

    def _persist(plan_id, status, stop_reason, *args, **kwargs):
        writes.append({
            "plan_id": plan_id,
            "status": status,
            "stop_reason": stop_reason,
        })

    monkeypatch.setattr(server_mod, "_persist_verification_terminal", _persist)
    monkeypatch.setattr(
        server_mod, "_cleanup_dead_verification_processes",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        server_mod, "_update_plan_state_to_terminal", lambda *a, **k: None,
    )
    return writes


def _run_watchdog() -> None:
    """Drive the production watchdog entry point."""
    server_mod._lazy_check_verification(PLAN_ID)


# ---------------------------------------------------------------------------
# 1. A parked execution is not a death
# ---------------------------------------------------------------------------


def test_quota_parked_execution_is_not_a_death(watchdog_env, terminal_writes, caplog):
    """The round is WAITING for capacity — so it must not be written off.

    Both halves of the park are in place: the executor's own record says
    ``waiting_for_quota``, and the durable ``plan_execution`` row says
    the same thing.  The latch asks the question it must ask — *is this
    plan parked?* — before it declares anything dead.

    The consequence of getting this wrong is what happened on
    2026-11-15: a plan that was waiting for the quota window to reopen
    was closed as failed, and nothing would ever have reopened it.
    """
    caplog.set_level(logging.INFO, logger="server")
    _seed_execution_row(watchdog_env["db_path"], PLAN_ID, PARKED_STATUS, PARKED_STATUS)
    _parked_execution_in_memory()

    _run_watchdog()

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "running", (
        "a round whose execution is parked for quota is WAITING, not dead — "
        f"the watchdog wrote {state['verification_status']!r}"
    )
    assert state.get("stop_reason") is None, (
        "a parked round must not carry a stop reason — it carries none of "
        "the death vocabulary"
    )
    assert terminal_writes == [], (
        "a parked round must not be written off: the terminal write ran "
        f"anyway: {terminal_writes!r}"
    )
    assert any(
        "quota park" in record.getMessage() for record in caplog.records
    ), (
        "the latch must say that it saw a park — the log is the only "
        "record of why the round was left alone"
    )


# ---------------------------------------------------------------------------
# 2. No park → still a death (the negative control)
# ---------------------------------------------------------------------------


def test_dead_thread_without_park_still_dies(watchdog_env, terminal_writes):
    """An ended thread with nothing parked is a death, and stays one.

    The fix must not turn the latch off.  A round whose execution says
    ``running`` but whose process is gone is exactly the case the latch
    exists for: the thread ended, nothing is outstanding, nothing is
    waiting for capacity — the round is dead and the plan has to learn
    about it.
    """
    _seed_execution_row(watchdog_env["db_path"], PLAN_ID, "running", None)
    _dead_execution_in_memory()

    _run_watchdog()

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "failed", (
        "a dead thread with no park must still be judged dead"
    )
    assert state["stop_reason"] == THREAD_DEATH, (
        f"the death must be recorded as a thread death, got "
        f"{state.get('stop_reason')!r}"
    )
    assert len(terminal_writes) == 1, (
        f"the terminal write must have happened exactly once, got "
        f"{terminal_writes!r}"
    )
    assert terminal_writes[0]["stop_reason"] == THREAD_DEATH


# ---------------------------------------------------------------------------
# 3. A probe that fails is not a park
# ---------------------------------------------------------------------------


def test_park_probe_failure_is_not_a_park(watchdog_env, terminal_writes, monkeypatch):
    """A park that cannot be read is not a park — the round is judged dead.

    The latch has to decide something when the park cannot be read, and
    the decision it makes is the conservative one.  A doubt about the
    park must not become a licence to leave a dead round running: the
    round stays declared dead, loudly, and an operator can look again.

    The opposite choice — treating an unreadable park as a park — would
    leave every round whose database hiccuped alive and un-owned, which
    is the failure mode this latch was built to end.
    """
    _seed_execution_row(watchdog_env["db_path"], PLAN_ID, PARKED_STATUS, PARKED_STATUS)
    _parked_execution_in_memory()

    def _explode(plan_id):
        raise RuntimeError("the park could not be read")

    monkeypatch.setattr(vl, "_plan_parked_for_quota", _explode)

    _run_watchdog()

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "failed", (
        "a park that could not be read must not keep a dead round alive"
    )
    assert state["stop_reason"] == THREAD_DEATH, (
        f"the death must be recorded as a thread death, got "
        f"{state.get('stop_reason')!r}"
    )
    assert len(terminal_writes) == 1, (
        f"the terminal write must have happened exactly once, got "
        f"{terminal_writes!r}"
    )
