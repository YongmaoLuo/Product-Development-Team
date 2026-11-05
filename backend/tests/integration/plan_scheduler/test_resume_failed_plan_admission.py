"""Integration: a **failed** plan that is resumed really comes back.

The defect
----------
``GET /api/plans`` decides whether a plan is *finished* from **two**
verdicts, not one (``routes/plans.py``'s ``_plan_is_terminal``):

* the routing phase — the workflow's own answer;
* the persisted execution verdict — the run's own answer.

A run that failed or was stopped is over **whatever the workflow phase
says**.  That second verdict exists for the crash path: a process dies,
the crash handler persists ``failed`` on the execution row, and the
routing row never catches up.

``PlanLifecycleService.requeue_plan`` moves the routing phase back to
``queued`` (``failed → queued`` is an existing edge of the state
machine) — but it used to leave the execution verdict alone.  So the two
verdicts contradicted each other: the phase said *waiting*, the verdict
said *over*.  The listing read the verdict and filtered the plan out —
the operator pressed "resume", the command answered ``applied``, and the
plan **vanished from the page**.

The stop path already had the answer for this: ``stop_plan(requeue=True)``
clears the terminal verdict right after it re-queues (see
``_clear_terminal_exec_verdict``).  The resume path did not.

What this file holds down
-------------------------
1. A resumed plan's execution verdict is cleared — ``exec_status``
   becomes ``not_started`` and ``stop_reason`` goes away.
2. The plan is **in the listing** again, and the listing's own predicate
   agrees (``_plan_is_terminal('queued', 'not_started')`` is False).
3. The plan is **in the queue** again — the queue row is the scheduler's
   own fact, written by the command layer that the resume action goes
   through.
4. The **tasks are not touched**: a failed task stays failed and a
   pending task stays pending.  A resume is not a re-run of the tasks
   that already failed — it puts the plan back in line; what happens to
   the tasks is the executor's business.
5. A **running** plan is left alone: a non-terminal verdict
   (``running``) is not cleared, and requeueing a running plan is a
   conflict that moves nothing.
6. A plan with **no execution row** is not an error: there is no verdict
   to clear, and the resume still works.

The database is real
--------------------
The queue row and the execution verdict are two facts in **one**
SQLite file, and the listing reads them from the same file the facade
writes.  A test with a stand-in for either would not be testing the
thing that breaks.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plans_root() -> Path:
    """The plans tree the facade resolves plans in.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the facade
    resolves a plan through ``server._plan_dir``, so the case has to
    write where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan directory whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot and
    then read once, which is what migrates it into ``plan_routing`` —
    the same path a plan that was bootstrapped by the API takes.  The
    migration is not optional here: the scheduler reads the phase from
    ``plan_routing``, and a plan whose row was never written is a plan
    the scheduler does not know.
    """
    from plan_state import PlanState

    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    # The first read migrates the legacy file into the routing row.
    PlanState(plan_dir)
    return plan_dir


def _write_tasks(plan_dir: Path, statuses: List[str]) -> None:
    """Write a task list whose tasks carry ``statuses``, in order."""
    tasks = [
        {
            "id": f"task-{index + 1}",
            "title": f"Task {index + 1}",
            "status": status,
        }
        for index, status in enumerate(statuses)
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"requirement": "a resumed plan", "tasks": tasks}),
        encoding="utf-8",
    )


def _task_statuses(plan_dir: Path) -> List[str]:
    """The status of every task of the plan, in order."""
    payload = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    return [str(task.get("status")) for task in payload.get("tasks", [])]


def _write_execution_row(
    plan_id: str,
    *,
    exec_status: str,
    stop_reason: Optional[str] = None,
    exec_pid: Optional[int] = None,
) -> None:
    """Give the plan the execution row a real run would have left."""
    from state_machine.repositories.execution_repository import ExecutionRepository

    conn = _open_state_db()
    try:
        ExecutionRepository(conn).insert(
            plan_id,
            exec_status=exec_status,
            stop_reason=stop_reason,
            exec_pid=exec_pid,
            project_dir=str(_plans_root() / plan_id),
        )
    finally:
        conn.close()


def _exec_row(plan_id: str) -> Optional[Dict[str, Any]]:
    """The plan's execution row, or ``None`` when it has none."""
    from state_machine.repositories.execution_repository import ExecutionRepository

    conn = _open_state_db()
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _queue_order(conn: sqlite3.Connection) -> List[str]:
    """The plan ids the scheduler queue holds, in the queue's order."""
    from plan_scheduler.store import SqliteSchedulerStore

    return [
        str(row["plan_id"])
        for row in SqliteSchedulerStore().list_queue(conn)
    ]


def _phase_in_db(plan_id: str) -> Optional[str]:
    """The phase the plan carries **in the database**."""
    conn = _open_state_db()
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def _executor(conn: sqlite3.Connection) -> Any:
    """A command executor over the case's database.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader and the lifecycle facade.  Only the
    connection is the case's own.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root())
    ledger = CapacityLedger(store, facts)
    return CommandExecutor(
        store,
        ledger,
        PlanLifecycleService(),
        facts=facts,
        conn=conn,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def failed_plan() -> str:
    """A plan whose task 1 failed: phase ``failed``, verdict ``failed``.

    This is the shape the defect was reported against: task 1 failed, the
    plan is over, and the operator wants it back in line.
    """
    plan_id = "resume-failed-p1"
    plan_dir = _make_plan(plan_id, "failed")
    _write_tasks(plan_dir, ["failed", "pending"])
    _write_execution_row(
        plan_id,
        exec_status="failed",
        stop_reason="failed:1",
    )
    return plan_id


@pytest.fixture
def listing_client():
    """A client for the listing route, with its cache dropped.

    The listing is cached by design (same plans tree, same
    ``include_terminal``), so the cache is dropped before the case reads
    it — otherwise the second read of the same tree would answer with
    the first read's rows.
    """
    import server

    client = TestClient(server.app)
    server.invalidate_plans_cache()
    yield client
    server.invalidate_plans_cache()


# ---------------------------------------------------------------------------
# 1. The terminal verdict is cleared
# ---------------------------------------------------------------------------


def test_resume_clears_terminal_exec_verdict(failed_plan: str) -> None:
    """A resumed plan's terminal execution verdict is cleared.

    The requeue moves the routing phase to ``queued``; the execution
    verdict is the **other** half of the terminal decision, and it still
    said ``failed``.  While it does, the listing filters the plan out —
    the plan the operator just resumed disappears from the page.

    The resting verdict of a plan that waits in line is ``not_started``:
    it is in the queue and it has not started this round.  ``stop_reason``
    belongs to the run that is over, so it goes away with the verdict.
    """
    from plan_lifecycle_service import PlanLifecycleService

    # Before the resume: the plan really is hidden by the two verdicts.
    assert _phase_in_db(failed_plan) == "failed"
    assert _exec_row(failed_plan)["exec_status"] == "failed"

    result = PlanLifecycleService().requeue_plan(failed_plan)

    assert result.ok is True
    assert result.final_phase == "queued"

    row = _exec_row(failed_plan)
    assert row["exec_status"] == "not_started", (
        f"the terminal verdict survived the resume: {row!r}"
    )
    assert row["stop_reason"] is None, (
        f"the old run's stop reason survived the resume: {row!r}"
    )
    # The pid of the run that is over does not survive it either — a pid
    # left on the row makes "is this execution alive?" believe it is.
    assert row["exec_pid"] is None, (
        f"the old run's pid survived the resume: {row!r}"
    )
    # And the phase really moved (the database, not just the return value).
    assert _phase_in_db(failed_plan) == "queued"


# ---------------------------------------------------------------------------
# 2. The plan is back in the listing
# ---------------------------------------------------------------------------


def test_plan_returns_to_listing(
    failed_plan: str, listing_client: TestClient
) -> None:
    """The resumed plan is in the listing again.

    Two things are asserted, and the second one is the reason the first
    one is worth writing: the listing's **own predicate** says the plan
    is not terminal any more, and the route that uses that predicate
    really serves the plan.

    The predicate is asserted directly because it is the thing that
    filters the plan: a listing that answers "200 with the plan in it"
    could also be a listing that degraded to a directory scan (which
    cannot filter anything) — and that would be a green test that proves
    nothing.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from routes.plans import _plan_is_terminal

    # Before the resume the plan is hidden: the phase says failed, and
    # so does the execution verdict.
    assert _plan_is_terminal("failed", "failed") is True

    PlanLifecycleService().requeue_plan(failed_plan)

    # The two verdicts agree now: the plan waits, and it is not over.
    assert _plan_is_terminal("queued", "not_started") is False
    assert _plan_is_terminal("queued", "failed") is True, (
        "the listing's predicate must still hide a plan whose run failed"
    )

    response = listing_client.get("/api/plans")
    assert response.status_code == 200, response.text
    entries = {entry["id"] for entry in response.json()}
    assert failed_plan in entries, (
        f"the resumed plan is not in the listing: {sorted(entries)!r}"
    )


# ---------------------------------------------------------------------------
# 3. The plan is back in the queue
# ---------------------------------------------------------------------------


def test_queue_row_exists_after_resume(failed_plan: str) -> None:
    """The resumed plan is in the scheduler's queue.

    The queue row is the scheduler's own fact — the plan's place in the
    line — and the **command layer** is what writes it (the lifecycle
    facade owns the phase; the queue belongs to the store).  So the
    resume is driven the way the operator drives it: through the
    command executor, which is what the action endpoint calls.

    The phase is the facade's, the queue row is the command layer's, and
    the verdict that used to hide the plan is the facade's again.  All
    three have to be right for the plan to come back.
    """
    conn = _open_state_db()
    try:
        executor = _executor(conn)
        assert failed_plan not in _queue_order(conn)

        result = executor.resume(failed_plan)

        assert result.outcome == "applied", (
            f"the resume did not go through: {result!r}"
        )
        assert failed_plan in _queue_order(conn), (
            f"the resumed plan is not in the queue: {_queue_order(conn)!r}"
        )
        assert _phase_in_db(failed_plan) == "queued"
        # And the verdict that used to hide the plan from the listing is
        # gone, so the plan is not queued *and* invisible.
        assert _exec_row(failed_plan)["exec_status"] == "not_started"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. The tasks are not touched
# ---------------------------------------------------------------------------


def test_failed_task_status_untouched(failed_plan: str) -> None:
    """A resume does not rewrite the task list.

    The task list is the plan's own history: task 1 failed, task 2 never
    ran.  A resume puts the **plan** back in line — it does not undo
    task 1's failure and it does not start task 2 by itself.  What
    happens to a task whose upstream failed (skipping it) is the
    executor's decision, made when the plan runs, not the resume's.
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_dir = _plans_root() / failed_plan
    assert _task_statuses(plan_dir) == ["failed", "pending"]

    result = PlanLifecycleService().requeue_plan(failed_plan)

    assert result.ok is True
    assert _task_statuses(plan_dir) == ["failed", "pending"], (
        "the resume rewrote the task list: "
        f"{_task_statuses(plan_dir)!r}"
    )


# ---------------------------------------------------------------------------
# 5. A running plan is left alone
# ---------------------------------------------------------------------------


def test_running_plan_not_touched() -> None:
    """A running plan's verdict is not cleared, and it is not requeued.

    Two different guards, both about the same mistake: writing over a
    run that is still in flight.

    * ``_clear_terminal_exec_verdict`` only writes when the verdict is
      **terminal**.  A running run is not terminal — it does not hide the
      plan from the listing — and overwriting it would destroy the fact
      that says "this plan is working right now".
    * ``requeue_plan`` refuses a running plan: a plan that is working
      has no "put it back to wait" to do, and the answer is a conflict
      that moves nothing.
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "resume-running-p2"
    plan_dir = _make_plan(plan_id, "executing")
    _write_tasks(plan_dir, ["in_progress", "pending"])
    _write_execution_row(
        plan_id,
        exec_status="running",
        stop_reason=None,
        exec_pid=4242,
    )

    # The verdict of a running plan is not terminal: clearing it would
    # erase the only answer that says the run is in flight.
    PlanLifecycleService._clear_terminal_exec_verdict(plan_id)

    row = _exec_row(plan_id)
    assert row["exec_status"] == "running", (
        f"a running verdict was overwritten: {row!r}"
    )
    assert row["exec_pid"] == 4242, (
        f"a running pid was overwritten: {row!r}"
    )

    # And the running plan is not requeued — that is a conflict.
    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is False
    assert result.detail == "conflict"
    assert result.final_phase == "executing"
    assert _phase_in_db(plan_id) == "executing"
    row = _exec_row(plan_id)
    assert row["exec_status"] == "running", (
        f"the running plan's verdict was touched: {row!r}"
    )


# ---------------------------------------------------------------------------
# 6. A plan with no execution row is not an error
# ---------------------------------------------------------------------------


def test_missing_execution_row_is_not_an_error(failed_plan: str) -> None:
    """A plan with no execution row is resumed without complaint.

    "There is no verdict to clear" is not an error: the verdict is an
    optional fact, and a plan that never ran has none.  The resume still
    works, and it still lands the plan in ``queued``.
    """
    from plan_lifecycle_service import PlanLifecycleService

    conn = _open_state_db()
    try:
        conn.execute(
            "DELETE FROM plan_execution WHERE plan_id = ?", (failed_plan,),
        )
        conn.commit()
    finally:
        conn.close()

    assert _exec_row(failed_plan) is None

    result = PlanLifecycleService().requeue_plan(failed_plan)

    assert result.ok is True
    assert result.final_phase == "queued"
    assert _phase_in_db(failed_plan) == "queued"
    # Nothing was written: there was no row to write on.
    assert _exec_row(failed_plan) is None
