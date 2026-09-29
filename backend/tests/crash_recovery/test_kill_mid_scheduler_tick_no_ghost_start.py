"""Acceptance test 5 (mid_scheduler_tick) — crash recovery cut point.

This file lives at the path the verification harness expects
(``tests/crash_recovery/test_kill_mid_scheduler_tick_no_ghost_start.py``)
and re-implements the test in a self-contained way so that the
``db_path`` / ``conn`` fixtures resolve locally. The single source of
truth remains ``state_machine/tests/unit/test_crash_recovery.py``.

The test verifies acceptance condition 5 (the bug-4 anchor):

  * ``kill -9`` mid-scheduler-tick → on-disk ``next_run_at`` is
    unchanged (the "decision" lived only in the killed process's
    local variable; it never landed on disk).
  * A subsequent ``SchedulerSupport.decide_tick()`` call from a
    cold-restart connection STILL finds the overdue plan — there
    is no "ghost start" and no missed schedule.
  * Reflection: ``SchedulerSupport`` instance has no
    ``_pending_*`` / ``_queue`` / ``_cache`` attributes.
  * Invariants I1-I5 all hold after cold-start replay.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import (
    ExecutionRepository,
)
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)
from state_machine.services.scheduler_support import SchedulerSupport
from state_machine.tests.unit._consistency_invariants import assert_db_consistent
from state_machine.tests.unit._kill_harness import kill_subprocess_at


# ---------------------------------------------------------------------------
# Fixtures (mirror the module-local fixtures in test_crash_recovery.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Yield a fresh tmp_path SQLite path; migrate the schema."""
    path = tmp_path / "state.db"
    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a parent-side connection to the tmp DB.

    Closed on teardown so the subprocess that the kill harness
    spawns has its OWN independent connection.
    """
    connection = open_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def routing_repo(conn: sqlite3.Connection) -> RoutingRepository:
    return RoutingRepository(conn)


@pytest.fixture
def exec_repo(conn: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(conn)


@pytest.fixture
def verification_repo(
    conn: sqlite3.Connection,
) -> VerificationRepository:
    return VerificationRepository(conn)


@pytest.fixture
def scheduler(
    conn: sqlite3.Connection,
    exec_repo: ExecutionRepository,
    routing_repo: RoutingRepository,
    verification_repo: VerificationRepository,
) -> SchedulerSupport:
    return SchedulerSupport(
        conn=conn,
        exec_repo=exec_repo,
        routing_repo=routing_repo,
        verification_repo=verification_repo,
    )


def _seed_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    current_phase: str = "ready",
    stage: str = "ready",
    next_run_at: str | None = "2000-01-01T00:00:00Z",
    verification_status: str | None = None,
    version: int = 0,
) -> None:
    """Insert a minimal plan across the plan_* tables.

    The default values represent a plan whose ``next_run_at`` is
    overdue and which is NOT inside a running verification round
    (so ``decide_tick`` should pick it up).
    """
    conn.execute(
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, substage, version, updated_at) "
        "VALUES (?, ?, NULL, ?, '2026-08-05T00:00:00Z')",
        (plan_id, stage, version),
    )
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, current_phase, attempt_count, project_dir, "
        " stop_reason, task_progress, next_run_at, card_state, "
        " flags, exec_pid, exec_status, started_at, updated_at) "
        "VALUES (?, ?, 0, ?, NULL, NULL, ?, NULL, NULL, NULL, "
        "NULL, NULL, '2026-08-05T00:00:00Z')",
        (plan_id, current_phase, f"/abs/{plan_id}", next_run_at),
    )
    if verification_status is not None:
        conn.execute(
            "INSERT INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            " verification_stop_reason, runtime_state, executor_state, "
            " progress_state, results, verdicts, started_at, "
            " updated_at) "
            "VALUES (?, ?, 0, 3, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, '2026-08-05T00:00:00Z')",
            (plan_id, verification_status),
        )
    conn.commit()


def _cold_replay_state(db_path: Path, plan_id: str) -> dict:
    """Re-open the DB from cold and read the post-crash snapshot."""
    fresh = open_db(db_path)
    try:
        routing_row = fresh.execute(
            "SELECT current_phase, version FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        execution_row = fresh.execute(
            "SELECT current_phase, next_run_at FROM plan_execution "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return {
            "routing": {
                "stage": routing_row[0] if routing_row else None,
                "version": routing_row[1] if routing_row else None,
            },
            "execution": {
                "current_phase": execution_row[0] if execution_row else None,
                "next_run_at": execution_row[1] if execution_row else None,
            },
        }
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Acceptance condition 5 — cut point mid_scheduler_tick (bug 4 anchor)
# ---------------------------------------------------------------------------


@pytest.mark.bug_4
def test_kill_mid_scheduler_tick_no_ghost_start(
    db_path: Path,
    conn: sqlite3.Connection,
    scheduler: SchedulerSupport,
) -> None:
    """Kill mid-scheduler-tick → no ghost start, scheduler still finds the plan.

    Bug 4 anchor: the scheduler used to keep a "decide what to
    schedule" decision in-memory BEFORE writing the UPDATE.  A
    kill mid-tick could leave the in-memory decision observable
    on the next restart (the "ghost start" the bug got its name
    from).  The refactor requires the decision to land only via
    IMMEDIATE-txn writes; a kill between the SELECT and the
    UPDATE must leave the row at its PRE-tick value.

    Strategy:
      1. Seed a plan with ``next_run_at='2000-01-01T00:00:00Z'``
         (overdue, but no scheduler has yet decided to
         re-schedule).
      2. Snapshot the pre-kill state.
      3. Run the kill harness at ``mid_scheduler_tick`` — the
         subprocess reads ``next_run_at``, computes a decision
         (kept in a local variable), and sleeps BEFORE the
         UPDATE.
      4. SIGKILL in the sleep.  The local decision variable is
         gone with the process; the on-disk row is unchanged.
      5. Re-open the DB; ``next_run_at`` still equals the
         pre-kill value (no ghost schedule update).
      6. Run ``decide_tick()`` from the parent's connection —
         the scheduler still finds the plan due (because
         ``next_run_at`` is still overdue).
    """
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="ready",
        stage="ready",
        next_run_at="2000-01-01T00:00:00Z",
        verification_status=None,
    )
    # No verification row needed — decide_tick should pick up the
    # plan because there is no running verification to gate it.

    # Snapshot pre-kill state.
    pre_state = _cold_replay_state(db_path, "p1")
    assert pre_state["execution"]["next_run_at"] == "2000-01-01T00:00:00Z", (
        "pre-kill state already wrong; the test setup wrote "
        "something other than '2000-01-01T00:00:00Z' to next_run_at"
    )

    report = kill_subprocess_at(
        "mid_scheduler_tick",
        plan_id="p1",
        db_path=db_path,
    )

    # Sanity: the kill landed AFTER the decision was computed
    # but BEFORE the UPDATE.  decision_computed must be present;
    # pre_update must NOT be.
    markers = report.get("markers", [])
    assert "decision_computed" in markers, (
        f"scheduler-tick cut landed before decision was computed; "
        f"markers={markers!r}"
    )
    assert "pre_update" not in markers, (
        "kill landed AFTER pre_update marker; the subprocess "
        "completed the UPDATE — this is not mid-tick"
    )

    state = _cold_replay_state(db_path, "p1")
    assert state["execution"]["next_run_at"] == "2000-01-01T00:00:00Z", (
        f"scheduler-tick kill left a ghost schedule update; "
        f"expected '2000-01-01T00:00:00Z', got "
        f"{state['execution']['next_run_at']!r} — bug 4 regression"
    )

    # The scheduler's next decide_tick() call (from the parent
    # process's still-alive connection) must STILL find the plan
    # due — the kill did not advance the schedule.
    picks = scheduler.decide_tick()
    assert "p1" in picks, (
        "scheduler lost track of the overdue plan after "
        "mid_scheduler_tick kill — bug 4 regression"
    )

    # Invariants hold.
    assert_db_consistent(db_path)


# ---------------------------------------------------------------------------
# Reflection guard — SchedulerSupport has no _pending_* / _queue / _cache
# ---------------------------------------------------------------------------


@pytest.mark.bug_4
def test_scheduler_instance_has_no_uncommitted_decision_fields(
    scheduler: SchedulerSupport,
) -> None:
    """Reflection guard — SchedulerSupport instance has no cache / queue fields.

    The contract pinned by the architecture decision is that
    the scheduler's state lives ONLY in SQLite — there is no
    in-memory ``_pending_*`` queue, no ``_queue``, no ``_cache``.
    This test inspects both the constructor signature and the
    live instance ``__dict__`` and refuses any attribute name
    matching the forbidden patterns.
    """
    forbidden_substrings = ("_pending_", "_queue", "_cache")
    allowed_prefixes = ("_conn",)

    # 1) Init parameters — a cache passed via constructor is
    # just as bad as one set on the instance.
    sig = inspect.signature(scheduler.__init__)
    for name in sig.parameters.keys():
        assert not any(sub in name for sub in forbidden_substrings), (
            f"SchedulerSupport.__init__ parameter {name!r} looks like a "
            f"cache / queue; forbidden patterns = {forbidden_substrings!r}"
        )

    # 2) Instance attributes — the snapshot after construction.
    instance_attrs = {name for name in vars(scheduler).keys()}
    for attr in instance_attrs:
        if not attr.startswith("_"):
            continue
        if any(attr.startswith(prefix) for prefix in allowed_prefixes):
            continue
        assert not any(sub in attr for sub in forbidden_substrings), (
            f"SchedulerSupport instance carries forbidden attribute "
            f"{attr!r}; instance_attrs = {sorted(instance_attrs)!r}; "
            f"forbidden_substrings = {forbidden_substrings!r}"
        )
