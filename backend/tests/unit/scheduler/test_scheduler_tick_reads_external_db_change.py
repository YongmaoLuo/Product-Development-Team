"""Unit test: Scheduler.tick() reads only from SQLite (bug-4 anchor).

This file lives at the path the verification harness expects
(``tests/unit/scheduler/test_scheduler_tick_reads_external_db_change.py``)
and re-implements the canonical bug-4 regression test in a
self-contained way so the ``conn`` / ``scheduler`` fixtures resolve
locally. The single source of truth remains
``state_machine/tests/unit/test_scheduler_support.py``.

The test pins the **bug-4 anchor**: external SQL ``UPDATE`` on
``plan_execution.next_run_at`` is visible to the very next
``SchedulerSupport.decide_tick()`` call with no cache
invalidation step.  This was the regression an
earlier plan hit — the legacy scheduler cached
``next_run_at`` in ``self._pending_*`` and the in-memory cache
masked external writes until the process restarted.

The contract this test holds:

  1. With ``next_run_at`` in the future, ``decide_tick()`` does
     NOT select the plan.
  2. An external raw-SQL ``UPDATE`` (bypassing every
     SchedulerSupport method) makes the row overdue.
  3. The very next ``decide_tick()`` call sees the new value.
     No cache.  No flush.  No manual invalidation.

It also asserts the reflection guard: ``SchedulerSupport``
instances hold no ``_pending_*`` / ``_queue`` / ``_cache``
attribute.
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


# ---------------------------------------------------------------------------
# Fixtures (mirror the module-local fixtures in test_scheduler_support.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a fresh, migrated SQLite connection (autocommit mode)."""
    db_path = tmp_path / "state.db"
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def exec_repo(conn: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(conn)


@pytest.fixture
def routing_repo(conn: sqlite3.Connection) -> RoutingRepository:
    return RoutingRepository(conn)


@pytest.fixture
def verification_repo(conn: sqlite3.Connection) -> VerificationRepository:
    return VerificationRepository(conn)


@pytest.fixture
def scheduler(
    conn: sqlite3.Connection,
    exec_repo: ExecutionRepository,
    routing_repo: RoutingRepository,
    verification_repo: VerificationRepository,
):
    """Yield a ``SchedulerSupport`` bound to the test connection."""
    from state_machine.services.scheduler_support import SchedulerSupport

    return SchedulerSupport(
        conn=conn,
        exec_repo=exec_repo,
        routing_repo=routing_repo,
        verification_repo=verification_repo,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    current_phase: str = "ready",
    next_run_at: str | None = None,
    stage: str = "executing",
    verification_status: str | None = None,
) -> None:
    """Insert minimal rows for ``plan_id`` into the three plan_* tables.

    The default values cover the "execution should run" path.  A
    caller who wants the plan to be *excluded* (e.g. by setting a
    non-executing routing stage) overrides the keyword args.
    """
    conn.execute(
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, substage, version, updated_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (plan_id, stage, None, "2026-08-05T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, current_phase, attempt_count, project_dir, "
        " stop_reason, task_progress, next_run_at, card_state, "
        " flags, exec_pid, exec_status, started_at, updated_at) "
        "VALUES (?, ?, 0, ?, NULL, NULL, ?, NULL, NULL, NULL, "
        "NULL, NULL, ?)",
        (plan_id, current_phase, f"/abs/{plan_id}", next_run_at,
         "2026-08-05T00:00:00Z"),
    )
    if verification_status is not None:
        conn.execute(
            "INSERT INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            " verification_stop_reason, runtime_state, executor_state, "
            " progress_state, results, verdicts, started_at, "
            " updated_at) "
            "VALUES (?, ?, 0, 3, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, ?)",
            (plan_id, verification_status, "2026-08-05T00:00:00Z"),
        )


# ---------------------------------------------------------------------------
# TDD anchor — bug 4: external SQL UPDATE visible on next decide_tick
# ---------------------------------------------------------------------------


@pytest.mark.bug_4
def test_scheduler_tick_reads_external_db_change(
    conn: sqlite3.Connection,
    scheduler,
) -> None:
    """External UPDATE on ``next_run_at`` is visible to the very next tick.

    This is the canonical **bug-4 anchor**.  The legacy scheduler
    cached ``next_run_at`` in ``self._pending_*``; an external
    writer (e.g. a repair task, an out-of-band migration, or
    another process) would change the row on disk, but the
    in-memory cache kept the old value — so the dispatcher kept
    ticking on the stale schedule until the process restarted.
    This regression surfaced in an earlier plan.

    The fix is structural: ``decide_tick`` re-reads the table on
    every call.  No cache.  No memoisation.  No instance-level
    ``_pending_*`` field.

    Strategy:
      1. Seed a plan with ``next_run_at`` set to ``2099-...``
         (NOT due — a future timestamp the scheduler should skip).
      2. Call ``decide_tick`` — the plan must NOT be selected.
      3. Externally UPDATE the row's ``next_run_at`` to a past
         timestamp via raw SQL (bypassing every
         SchedulerSupport method).
      4. Call ``decide_tick`` AGAIN — the plan MUST now be
         selected on the very next call, with no cache
         invalidation step.

    The "very next call" requirement is the regression pin: an
    in-memory cache that flushes only on a periodic timer would
    return the stale value here and fail the test.
    """
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="ready",
        next_run_at="2099-01-01T00:00:00Z",
    )

    # Step 1: with a far-future ``next_run_at``, the plan is not due.
    first_picks = scheduler.decide_tick()
    assert "p1" not in first_picks, (
        "plan with next_run_at=2099 was selected as due; "
        "the scheduler is treating future timestamps as overdue"
    )

    # Step 2: external writer rewrites ``next_run_at`` to a past
    # timestamp via raw SQL, bypassing every SchedulerSupport
    # method.  This is the "sibling module wrote directly"
    # scenario.
    conn.execute(
        "UPDATE plan_execution SET next_run_at = ? WHERE plan_id = ?",
        ("2000-01-01T00:00:00Z", "p1"),
    )

    # Step 3: the next ``decide_tick`` MUST see the new value.
    # No cache.  No flush.  No manual invalidation.
    second_picks = scheduler.decide_tick()
    assert "p1" in second_picks, (
        "scheduler did not pick up external UPDATE on next_run_at; "
        "bug-4 regression — in-memory cache is masking the change"
    )


# ---------------------------------------------------------------------------
# Reflection guard — no _pending_* / _queue / _cache on SchedulerSupport
# ---------------------------------------------------------------------------


@pytest.mark.bug_4
def test_scheduler_has_no_uncommitted_decision_fields(
    scheduler,
) -> None:
    """``SchedulerSupport`` instance has no decision-stash attributes.

    The contract pinned by the architecture decision is that the
    scheduler's state lives ONLY in SQLite — there is no
    in-memory ``_pending_*`` queue, no ``_queue``, no ``_cache``.
    This test inspects ``__dict__`` (and ``__init__`` parameter
    names, so a cache field passed in via constructor is caught)
    and refuses any attribute matching the forbidden patterns.
    """
    forbidden_substrings = ("_pending_", "_queue", "_cache")
    allowed_prefixes = ("_conn",)

    # 1) Init parameters — a cache passed via constructor is
    # just as bad as one set on the instance.
    sig = inspect.signature(scheduler.__init__)
    init_param_names = list(sig.parameters.keys())
    for name in init_param_names:
        assert not any(sub in name for sub in forbidden_substrings), (
            f"SchedulerSupport.__init__ parameter {name!r} looks like a "
            f"cache / queue; forbidden patterns = {forbidden_substrings!r}"
        )

    # 2) Instance attributes — the snapshot after construction.
    instance_attrs = {name for name in vars(scheduler).keys()}
    for attr in instance_attrs:
        # Skip attrs that the dataclass / fixture machinery adds;
        # we only care about SchedulerSupport's own keys.
        if not attr.startswith("_"):
            continue
        if any(attr.startswith(prefix) for prefix in allowed_prefixes):
            continue
        assert not any(sub in attr for sub in forbidden_substrings), (
            f"SchedulerSupport instance carries forbidden attribute "
            f"{attr!r}; instance_attrs = {sorted(instance_attrs)!r}; "
            f"forbidden_substrings = {forbidden_substrings!r}"
        )
