"""A plan the shutdown interrupted comes back as ``queued``, not as ``failed``.

Background
----------
The shutdown path leaves a checkpoint behind for every plan that was
still running (``backend/shutdown_checkpoint.py``).  On the next boot the
recovery scan (``routes.execution._recover_execution_states``) finds the
plan's row still saying ``running`` while its pid is gone, and asks
what to make of it.

Two verdicts are possible, and the evidence decides which one:

* the run the *server* interrupted is not a crash -- the work is
  unfinished, not broken, and it deserves another attempt.  Writing it
  as ``failed`` made the scheduler's waiting set blind to it, so an
  interrupted plan was never picked up again -- the exact silence the
  checkpoint was written to end.  The checkpoint is the evidence that
  says so (``shutdown_checkpoint.is_shutdown_interrupted``), and when
  it holds the plan is parked:

  - the row says ``stopped`` with ``stop_reason='server_shutdown'`` and
    no pid;
  - the plan's phase is ``queued`` -- the phase the scheduler waits on;
  - the task rows the dead executor owned go back to ``pending``;

* a run the scan cannot prove the server interrupted is parked as
  ``stopped`` with ``stop_reason='startup_reconcile'`` and phase
  ``stopped``.  That is the branch's startup-reconcile verdict: the
  pid is gone, so the run is over for this process, but nothing proves
  *how* it ended, so nothing claims that it died unexpectedly.  The
  plan is parked, not failed -- it is resumable, and the operator (or
  the scheduler) may pick it up again.

What these tests pin
--------------------
* a fresh shutdown checkpoint parks the plan in ``queued``;
* the row carries that verdict (status, reason, no pid);
* the interrupted task is handed back to the scheduler;
* a run with no checkpoint is parked as ``stopped`` -- the negative
  control for the checkpoint;
* a checkpoint *older* than the run proves nothing: it belongs to an
  earlier run and says nothing about this one;
* a checkpoint that says ``user_requested`` is somebody pressing stop,
  not the server going down -- the predicate may not launder it;
* a failing phase write does not escape the recovery scan, and does not
  cost the row its verdict.

The stamps in these tests are *computed* rather than hard-coded: the
checkpoint's ``paused_at`` is an aware UTC stamp and the row's
``started_at`` is a naive local one, exactly as the two writers spell
them, so each test means the same instant whatever timezone the machine
running it is in.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pause_state import (
    PAUSE_CHECKPOINT_FILENAME,
    write_pause_checkpoint,
)
from plan_state import PlanState
from server import _execution_state, _recover_execution_states
from shutdown_checkpoint import SHUTDOWN_STOP_REASON


#: A pid that cannot belong to a live process.  The recovery scan probes
#: it with ``os.kill(pid, 0)`` and reads the ``ProcessLookupError`` as
#: "the executor is gone".
_DEAD_PID = 999999


def _naive_local(moment: datetime) -> str:
    """``moment`` (aware) spelled the way the execution row spells it.

    ``routes.execution`` writes ``started_at`` as ``datetime.now()
    .isoformat()`` -- a naive *local* timestamp.  The helper makes the
    same conversion, so a test can hand the predicate a naive stamp that
    denotes a known instant.
    """
    return moment.astimezone().replace(tzinfo=None).isoformat()


def _write_checkpoint(plan_dir, *, kind, reason, paused_at):
    """Write a checkpoint whose ``paused_at`` the test controls.

    ``write_pause_checkpoint`` stamps ``paused_at`` itself, and the
    freshness judgement is exactly about that field -- so the helper goes
    through the public writer (which owns the schema version) and then
    rewrites the one field the test needs to pin.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    document = dict(
        write_pause_checkpoint(plan_dir, kind=kind, reason=reason)
    )
    document["paused_at"] = paused_at
    (plan_dir / PAUSE_CHECKPOINT_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return document


def _seed_execution_row(plan_id: str, **fields) -> None:
    """Insert a ``plan_execution`` row in the hermetic per-test database."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


def _seed_in_progress_task(plan_id: str, task_id: str) -> None:
    """Stand a task row in ``in_progress`` -- the state a dead executor leaves."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        PlanTaskRepository(conn).update_task(
            plan_id, task_id, {"status": "in_progress"}
        )
    finally:
        conn.close()


def _read_execution_row(plan_id: str) -> dict:
    """The durable ``plan_execution`` row, as the database holds it."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open

    conn = db_open(_state_db_path())
    try:
        row = conn.execute(
            "SELECT exec_status, exec_pid, stop_reason FROM plan_execution "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no plan_execution row for {plan_id}"
    return {
        "exec_status": row[0],
        "exec_pid": row[1],
        "stop_reason": row[2],
    }


def _read_task_status(plan_id: str, task_id: str):
    """The ``plan_tasks`` status of one task, or ``None`` if it is gone."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open

    conn = db_open(_state_db_path())
    try:
        row = conn.execute(
            "SELECT status FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _running_plan(plan_dir, *, started_at):
    """Seed the row of a plan whose executor is gone."""
    plan_id = plan_dir.name
    _seed_execution_row(
        plan_id,
        exec_status="running",
        exec_pid=_DEAD_PID,
        started_at=started_at,
        project_dir=str(plan_dir / "project"),
    )
    return plan_id


def test_a_fresh_shutdown_checkpoint_parks_in_queued(sample_plan_factory):
    """A run the shutdown interrupted is parked in ``queued``, not failed."""
    plan_dir = sample_plan_factory(plan_id="p-shutdown", phase="executing")
    paused = datetime.now(timezone.utc)
    plan_id = _running_plan(
        plan_dir, started_at=_naive_local(paused - timedelta(minutes=1))
    )
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    _execution_state.clear()
    _recover_execution_states(plan_dir.parent)

    state = _execution_state[plan_id]
    assert state["status"] == "stopped", (
        "the shutdown interrupted this run; it did not crash it"
    )
    assert state["stop_reason"] == "server_shutdown"
    assert state["pid"] is None
    assert PlanState(plan_dir).get_current_phase() == "queued", (
        "queued is the phase the scheduler waits on"
    )


def test_the_row_carries_the_stopped_verdict(sample_plan_factory):
    """The row says the run was stopped by the shutdown, and carries no pid."""
    plan_dir = sample_plan_factory(plan_id="p-shutdown-row", phase="executing")
    paused = datetime.now(timezone.utc)
    plan_id = _running_plan(
        plan_dir, started_at=_naive_local(paused - timedelta(minutes=1))
    )
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    _execution_state.clear()
    _recover_execution_states(plan_dir.parent)

    row = _read_execution_row(plan_id)
    assert row["exec_status"] == "stopped"
    assert row["stop_reason"] == "server_shutdown"
    assert row["exec_pid"] is None, (
        "a stopped row must not carry a pid: every reader believes a pid "
        "means alive"
    )


def test_the_in_progress_task_is_handed_back(sample_plan_factory):
    """The task the dead executor owned goes back to the scheduler."""
    plan_dir = sample_plan_factory(plan_id="p-shutdown-task", phase="executing")
    paused = datetime.now(timezone.utc)
    plan_id = _running_plan(
        plan_dir, started_at=_naive_local(paused - timedelta(minutes=1))
    )
    _seed_in_progress_task(plan_id, "task-1")
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    _execution_state.clear()
    _recover_execution_states(plan_dir.parent)

    assert _read_task_status(plan_id, "task-1") == "pending", (
        "an in_progress row with no owner behind it would keep the plan "
        "claiming to be running a task"
    )


def test_a_crash_without_a_checkpoint_is_parked_stopped(sample_plan_factory):
    """Negative control: no checkpoint, so the run is parked, not queued.

    Without the checkpoint there is no evidence that the *server* went
    down under this run, so the plan does not get the ``queued``
    parking.  It is parked as ``stopped`` — the startup reconcile's
    verdict — because the pid is gone and the restart interrupted the
    run, but nothing proves how it ended.
    """
    plan_dir = sample_plan_factory(plan_id="p-crash", phase="executing")
    plan_id = _running_plan(
        plan_dir, started_at=_naive_local(datetime.now(timezone.utc))
    )

    _execution_state.clear()
    _recover_execution_states(plan_dir.parent)

    state = _execution_state[plan_id]
    assert state["status"] == "stopped", (
        "nothing proves the server went down under this run, so the run "
        "is parked — not failed, and not 'died unexpectedly'"
    )
    assert state["stop_reason"] == "startup_reconcile"
    assert PlanState(plan_dir).get_current_phase() == "stopped"


def test_a_user_requested_checkpoint_is_not_a_server_shutdown(
    sample_plan_factory,
):
    """Somebody pressing stop is not the server going down.

    The ``user_requested`` reason is a *different* event, and the
    predicate may not launder it into "the server went down": the run
    was cancelled, so it does not get the ``queued`` parking either.
    """
    plan_dir = sample_plan_factory(plan_id="p-user-stop", phase="executing")
    paused = datetime.now(timezone.utc)
    plan_id = _running_plan(
        plan_dir, started_at=_naive_local(paused - timedelta(minutes=1))
    )
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason="user_requested",
        paused_at=paused.isoformat(),
    )

    _execution_state.clear()
    _recover_execution_states(plan_dir.parent)

    state = _execution_state[plan_id]
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "startup_reconcile"
    assert PlanState(plan_dir).get_current_phase() == "stopped", (
        "a cancelled run is not put back in the scheduler's queue"
    )


def test_a_checkpoint_older_than_the_run_proves_nothing(sample_plan_factory):
    """A checkpoint left over from an earlier run is not evidence of anything."""
    plan_dir = sample_plan_factory(plan_id="p-stale", phase="executing")
    now = datetime.now(timezone.utc)
    plan_id = _running_plan(plan_dir, started_at=_naive_local(now))
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=(now - timedelta(hours=1)).isoformat(),
    )

    _execution_state.clear()
    _recover_execution_states(plan_dir.parent)

    state = _execution_state[plan_id]
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "startup_reconcile"
    assert PlanState(plan_dir).get_current_phase() == "stopped"


def test_a_phase_write_failure_does_not_escape_the_recovery_scan(
    sample_plan_factory, monkeypatch
):
    """A failing phase write costs the phase, not the scan and not the row."""
    plan_dir = sample_plan_factory(plan_id="p-phase-boom", phase="executing")
    paused = datetime.now(timezone.utc)
    plan_id = _running_plan(
        plan_dir, started_at=_naive_local(paused - timedelta(minutes=1))
    )
    _seed_in_progress_task(plan_id, "task-1")
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    class _ExplodingPlanState(PlanState):
        def force_set_phase(self, phase):
            raise RuntimeError("phase write failed")

    monkeypatch.setattr("server.PlanState", _ExplodingPlanState)

    _execution_state.clear()
    # Must not raise: the recovery scan owes every other plan its recovery.
    _recover_execution_states(plan_dir.parent)

    row = _read_execution_row(plan_id)
    assert row["exec_status"] == "stopped"
    assert row["stop_reason"] == "server_shutdown"
    assert _execution_state[plan_id]["status"] == "stopped"
    # The task rows are handed back BEFORE the phase write, so a failing
    # phase write cannot leave the plan stuck on a task nobody owns.
    assert _read_task_status(plan_id, "task-1") == "pending"
