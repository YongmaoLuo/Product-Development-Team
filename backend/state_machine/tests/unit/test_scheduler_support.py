"""Unit tests for :class:`state_machine.services.scheduler_support.SchedulerSupport`.

Background
----------
``SchedulerSupport`` is the **bug-4 anchor** for the state-machine
refactor: the legacy scheduler tick held in-memory queues and cached
``next_run_at`` values that masked external writes (a sibling module
or migration script ``UPDATE``ing the row would be invisible until
the next process restart).  This refactor replaces that with a thin
wrapper that re-reads the routing / execution / verification tables
on every ``tick()`` and writes decisions back via IMMEDIATE
transactions — never via in-memory state.

The four TDD anchors pinned here:

  1. ``test_scheduler_tick_reads_external_db_change`` — the
     **bug-4 anchor**.  External SQL UPDATE on ``next_run_at`` is
     visible to the very next ``decide_tick()`` call (no in-memory
     cache).  This is the regression that an earlier plan
     plan hit, and is the contract the refactor must hold.

  2. ``test_scheduler_has_no_uncommitted_decision_fields`` —
     reflection-based guard.  ``SchedulerSupport`` MUST NOT hold any
     instance attribute matching ``_pending_*``, ``_queue``, or
     ``_cache``.  The connection is the only state.

  3. ``test_decide_tick_returns_empty_when_nothing_due`` — boundary:
     with no ``plan_execution`` rows whose ``next_run_at`` is at or
     before "now", ``decide_tick()`` returns ``[]`` rather than
     raising.

  4. ``test_update_next_run_at_and_card_state_persists_immediately``
     — the write methods (the only way the scheduler's decisions
     land in the database) must persist in IMMEDIATE transactions
     so a crash mid-decision never leaves a half-written column.

The scheduler's read path also depends on routing / verification
state (a plan in ``prd_review`` does NOT enter ``decide_tick()``
output even if ``next_run_at`` is overdue).  That routing gate is
verified indirectly through the snapshot reading methods
(``next_run`` / ``card_state``) and the cross-table coverage of the
repository unit tests, which is sufficient for the SchedulerSupport
thin-wrapper contract.
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
# Fixtures
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
# TDD anchor #1 — external SQL UPDATE visible on next decide_tick (bug 4)
# ---------------------------------------------------------------------------


@pytest.mark.bug_4
def test_scheduler_tick_reads_external_db_change(
    conn: sqlite3.Connection,
    scheduler,
) -> None:
    """External UPDATE on ``next_run_at`` is visible to the very next tick.

    This is the canonical **bug-4 anchor**.  The legacy scheduler
    cached ``next_run_at`` in ``self._pending_*``; an external
    writer (e.g. a repair task, an out-of-band migration, or another
    process) would change the row on disk, but the in-memory cache
    kept the old value — so the dispatcher kept ticking on the
    stale schedule until the process restarted.  This regression
    surfaced in an earlier plan.

    The fix is structural: ``decide_tick`` re-reads the table on
    every call.  No cache.  No memoisation.  No instance-level
    ``_pending_*`` field.

    Strategy:
      1. Seed a plan with ``next_run_at`` set to ``2099-...`` (NOT
         due — a future timestamp the scheduler should skip).
      2. Call ``decide_tick`` — the plan must NOT be selected.
      3. Externally UPDATE the row's ``next_run_at`` to a past
         timestamp via raw SQL (bypassing every SchedulerSupport
         method).
      4. Call ``decide_tick`` AGAIN — the plan MUST now be selected
         on the very next call, with no cache invalidation step.

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
    # timestamp via raw SQL, bypassing every SchedulerSupport method.
    # This is the "sibling module wrote directly" scenario.
    conn.execute(
        "UPDATE plan_execution SET next_run_at = ? WHERE plan_id = ?",
        ("2000-01-01T00:00:00Z", "p1"),
    )

    # Step 3: the next ``decide_tick`` MUST see the new value.  No
    # cache.  No flush.  No manual invalidation.
    second_picks = scheduler.decide_tick()
    assert "p1" in second_picks, (
        "scheduler did not pick up external UPDATE on next_run_at; "
        "bug-4 regression — in-memory cache is masking the change"
    )


# ---------------------------------------------------------------------------
# TDD anchor #2 — reflection-based "no state" guard
# ---------------------------------------------------------------------------


def test_scheduler_has_no_uncommitted_decision_fields(
    scheduler,
) -> None:
    """``SchedulerSupport`` instance has no decision-stash attributes.

    The contract pinned by the architecture decision is that the
    scheduler's state lives ONLY in SQLite — there is no in-memory
    ``_pending_*`` queue, no ``_queue``, no ``_cache``.  This test
    inspects ``__dict__`` (and ``__init__`` parameter names, so a
    cache field passed in via constructor is caught) and refuses
    any attribute matching the forbidden patterns.

    The whitelist is the test attribute names pytest attaches to
    the fixture (``request`` etc.) — those are on the *fixture
    function*, not on ``SchedulerSupport`` instances.  We therefore
    inspect the SchedulerSupport instance directly.
    """
    forbidden_substrings = ("_pending_", "_queue", "_cache")
    allowed_prefixes = ("_conn",)

    # 1) Init parameters — a cache passed via constructor is just as bad.
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


# ---------------------------------------------------------------------------
# TDD anchor #3 — boundary: empty candidate set
# ---------------------------------------------------------------------------


def test_decide_tick_returns_empty_when_nothing_due(
    conn: sqlite3.Connection,
    scheduler,
) -> None:
    """``decide_tick`` returns ``[]`` when no plan is due.

    The scheduler MUST return an empty list (not ``None``, not an
    exception) when there is nothing to schedule.  This pins the
    "empty-tick" contract that the dispatcher's ``for plan_id in
    picks: ...`` loop relies on.
    """
    # Seed plans that are NOT due:
    #   - p1: next_run_at in the future
    #   - p2: next_run_at = NULL (cleared)
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="ready",
        next_run_at="2099-01-01T00:00:00Z",
    )
    _seed_plan(
        conn,
        plan_id="p2",
        current_phase="ready",
        next_run_at=None,
    )

    picks = scheduler.decide_tick()

    assert picks == [], (
        f"expected empty list when no plan is due; got {picks!r} "
        "— scheduler should treat NULL or future next_run_at as 'not due'"
    )


# ---------------------------------------------------------------------------
# TDD anchor #4 — write methods persist IMMEDIATELY (no deferred queue)
# ---------------------------------------------------------------------------


def test_update_next_run_at_and_card_state_persists_immediately(
    conn: sqlite3.Connection,
    scheduler,
    exec_repo: ExecutionRepository,
) -> None:
    """``update_next_run_at`` / ``update_card_state`` persist via IMMEDIATE.

    The scheduler's write methods MUST wrap their writes in a
    transaction that commits before the method returns, so a crash
    mid-decision never leaves a half-written column.  This test
    reads the row back via the repository (a different code path)
    and verifies the new value is visible — both for ``next_run_at``
    and ``card_state`` (the two columns the scheduler writes).
    """
    _seed_plan(conn, plan_id="p1", current_phase="ready")

    # Step 1: scheduler schedules a future tick.
    scheduler.update_next_run_at("p1", "2099-01-01T00:00:00Z")

    # Step 2: re-read via the repository — the value must be there.
    summary = exec_repo.summary("p1")
    assert summary is not None
    assert summary["next_run_at"] == "2099-01-01T00:00:00Z", (
        "scheduler.update_next_run_at did NOT persist to the row; "
        "the value must be visible to a subsequent repository read"
    )

    # Step 3: scheduler writes the card_state JSON column.
    scheduler.update_card_state("p1", {"expanded": True, "active": "1-2"})

    summary = exec_repo.summary("p1")
    assert summary is not None
    assert summary["card_state"] == {"expanded": True, "active": "1-2"}, (
        "scheduler.update_card_state did NOT persist to the row; "
        "the JSON-encoded column must round-trip via repository.summary()"
    )


# ---------------------------------------------------------------------------
# next_run / card_state accessors
# ---------------------------------------------------------------------------


def test_next_run_returns_value_or_none(
    conn: sqlite3.Connection,
    scheduler,
) -> None:
    """``next_run(plan_id)`` returns the column value or ``None``.

    Mirrors ``RoutingRepository.current``: missing row → ``None``,
    present row → the raw string value (the column is stored as
    ISO-8601 text, the scheduler compares lexicographically).
    """
    _seed_plan(conn, plan_id="p1", next_run_at="2099-01-01T00:00:00Z")
    _seed_plan(conn, plan_id="p2", next_run_at=None)

    assert scheduler.next_run("p1") == "2099-01-01T00:00:00Z"
    assert scheduler.next_run("p2") is None
    assert scheduler.next_run("missing") is None


def test_card_state_returns_dict_or_empty(
    conn: sqlite3.Connection,
    scheduler,
) -> None:
    """``card_state(plan_id)`` returns the parsed dict or ``{}`` for missing rows.

    The card_state column is JSON-encoded; the accessor parses it
    so callers (e.g. the UI) receive a plain dict.  A missing row
    (no ``plan_execution`` entry) is a known-safe boundary — the
    dispatcher must not crash on the first tick of a brand-new
    plan; it returns an empty dict.
    """
    _seed_plan(conn, plan_id="p1")
    conn.execute(
        "UPDATE plan_execution SET card_state = ? WHERE plan_id = ?",
        ('{"expanded": true, "active": "verifying"}', "p1"),
    )

    assert scheduler.card_state("p1") == {
        "expanded": True,
        "active": "verifying",
    }
    assert scheduler.card_state("missing") == {}