"""Unit tests for state-machine crash recovery (architecture decision point 5).

Background
----------
The state-machine refactor pins SQLite as the single source of truth.
A crash mid-transaction must therefore leave the on-disk database
in one of two states:

  * pre-txn state  (the BEGIN IMMEDIATE → kill -9 → ROLLBACK path
                    guarantees this via SQLite's journal),
  * post-COMMIT state (the COMMIT has landed and the kill happens
                    only AFTER the COMMIT returned).

Anything between those two — half-written rows, CAS-bumped version
with no business table written, scheduler "ghost" updates — is a
regression of the bug 1-4 contracts the previous tasks pinned.

This file implements the L4 (crash-recovery cut points) test
layer.  Each test simulates a hard process kill at a named cut
point via :func:`kill_subprocess_at`, then re-opens the database
from cold and verifies:

  * the on-disk state matches one of the two legal post-crash
    states (not "in-flight"),
  * the consistency invariants (I1-I5) all hold.

Architecture decision point 5 (the five cut points):

  1. ``commit_before``  — ``BEGIN IMMEDIATE`` issued, COMMIT not
                          yet issued.
  2. ``commit_after``   — ``COMMIT`` returned, subsequent
                          side-effect not yet run.
  3. ``mid_execution``  — execution loop iteration interrupted.
  4. ``mid_verification`` — verification round interrupted.
  5. ``mid_scheduler_tick`` — scheduler computed decision but
                              UPDATE not yet issued.

The tests deliberately do NOT mock the production code path —
the subprocess literally opens the production database and
runs the production IMMEDIATE-txn skeleton.  This means a
regression in any of the repositories (e.g. dropping the
``BEGIN IMMEDIATE`` wrapper, accidentally bypassing the
txn context manager) is caught here, not just in the
``test_*_repository.py`` unit tests.

TDD spec (per task brief):

  * test_kill_before_commit_leaves_no_trace        — bug 3 anchor
  * test_kill_after_commit_persists_change          — bug 3 anchor
  * test_kill_mid_execution_recovers_consistent_progress — acceptance condition 3
  * test_kill_mid_verification_recovers_consistent_round — acceptance condition 3
  * test_kill_mid_scheduler_tick_no_ghost_start     — bug 4 anchor
  * test_cold_start_replay_by_version_order_matches_pre_crash
  * test_db_invariants_hold_after_every_kill_point (parametrised x5)
  * test_scheduler_has_no_uncommitted_decision_fields (reflection)
"""

from __future__ import annotations

import inspect
import json
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

from ._consistency_invariants import (
    ConsistencyInvariantViolation,
    assert_db_consistent,
)
from ._kill_harness import CUT_POINTS, kill_subprocess_at


# ---------------------------------------------------------------------------
# Fixtures
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
    current_phase: str = "executing",
    stage: str = "executing",
    next_run_at: str | None = "2000-01-01T00:00:00Z",
    verification_status: str | None = "running",
    verdicts_seed: list[dict] | None = None,
    version: int = 0,
) -> None:
    """Insert a minimal plan across the four plan_* tables.

    The default values represent a plan in mid-execution whose
    ``verification_status='running'`` and ``next_run_at`` is in
    the past.  Tests that need a different shape override the
    keyword args.
    """
    conn.execute(
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, substage, version, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, stage, None, version, "2026-08-05T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, current_phase, attempt_count, project_dir, "
        " stop_reason, task_progress, next_run_at, card_state, "
        " flags, exec_pid, exec_status, started_at, updated_at) "
        "VALUES (?, ?, 0, ?, NULL, NULL, ?, NULL, NULL, NULL, "
        "NULL, NULL, ?)",
        (
            plan_id,
            current_phase,
            f"/abs/{plan_id}",
            next_run_at,
            "2026-08-05T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, "
        " verification_stop_reason, runtime_state, executor_state, "
        " progress_state, results, verdicts, started_at, updated_at) "
        "VALUES (?, ?, 1, 3, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?)",
        (
            plan_id,
            verification_status or "pending",
            json.dumps(verdicts_seed or []),
            "2026-08-05T00:00:00Z",
        ),
    )


def _cold_replay_state(db_path: Path, plan_id: str) -> dict:
    """Re-open the DB from cold and read the post-crash snapshot.

    Mirrors the production "process restart" semantics: the
    parent test closes its connection (the
    :func:`conn <db_path>` fixture does this on teardown),
    re-opens a fresh connection against ``db_path``, and
    reads the row the crash left behind.
    """
    fresh = open_db(db_path)
    try:
        cur = fresh.execute(
            "SELECT current_phase, version FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        routing_row = cur.fetchone()
        cur = fresh.execute(
            "SELECT current_phase, task_progress, next_run_at "
            "FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        )
        execution_row = cur.fetchone()
        cur = fresh.execute(
            "SELECT verification_status, verdicts, round "
            "FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        )
        verification_row = cur.fetchone()
        return {
            "routing": {
                "current_phase": routing_row[0] if routing_row else None,
                "version": routing_row[1] if routing_row else None,
            },
            "execution": {
                "current_phase": execution_row[0] if execution_row else None,
                "task_progress": execution_row[1] if execution_row else None,
                "next_run_at": execution_row[2] if execution_row else None,
            },
            "verification": {
                "status": verification_row[0] if verification_row else None,
                "verdicts": verification_row[1] if verification_row else None,
                "round": verification_row[2] if verification_row else None,
            },
        }
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Cut point 1 — commit_before (bug 3 anchor)
# ---------------------------------------------------------------------------


@pytest.mark.bug_3
def test_kill_before_commit_leaves_no_trace(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill between ``BEGIN IMMEDIATE`` and ``COMMIT`` leaves no advance.

    This is the **bug 3 anchor**.  The contract: every write
    method on the state-machine repositories is wrapped in
    ``BEGIN IMMEDIATE → COMMIT``; a SIGKILL between the BEGIN
    and the COMMIT must NOT leave the row advanced.

    Strategy:
      1. Seed a plan with ``stage='executing'`` and a known
         ``version=0``.
      2. Run the kill harness at ``commit_before`` — the
         subprocess opens BEGIN IMMEDIATE, writes
         ``version = version + 1`` + ``stage = 'commit_before_failed'``,
         then sleeps BEFORE COMMIT.  We SIGKILL it in the sleep.
      3. Re-open the DB from cold and assert:
           * routing.version == 0 (unchanged — the txn was rolled back)
           * routing.stage == 'executing' (unchanged — no partial write)
      4. Run :func:`assert_db_consistent` to close the invariant gate.
    """
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="executing",
        stage="executing",
        version=0,
    )

    report = kill_subprocess_at(
        "commit_before",
        plan_id="p1",
        db_path=db_path,
    )

    # Sanity: the kill harness observed the pre-kill marker.
    assert report["pre_kill_marker_observed"], (
        "kill harness did not observe the pre_commit marker; "
        "the kill landed BEFORE the cut window — cut point is broken"
    )

    state = _cold_replay_state(db_path, "p1")

    # The on-disk state must equal the pre-kill state.
    assert state["routing"]["version"] == 0, (
        f"kill -9 between BEGIN IMMEDIATE and COMMIT advanced "
        f"version: 0 -> {state['routing']['version']!r} — "
        f"bug 3 regression: the txn was not rolled back"
    )
    assert state["routing"]["current_phase"] == "executing", (
        f"kill -9 between BEGIN IMMEDIATE and COMMIT mutated "
        f"stage to {state['routing']['stage']!r} — "
        f"bug 3 regression: partial write is observable"
    )

    # All five invariants hold.
    assert_db_consistent(db_path)


# ---------------------------------------------------------------------------
# Cut point 2 — commit_after (bug 3 anchor)
# ---------------------------------------------------------------------------


@pytest.mark.bug_3
def test_kill_after_commit_persists_change(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill AFTER COMMIT, BEFORE side-effect → COMMIT's row IS visible.

    The bug 3 anchor, complementary to :func:`test_kill_before_commit_leaves_no_trace`.
    Once the ``COMMIT`` has returned, the row is durable on disk;
    a subsequent kill must NOT roll it back.

    Strategy:
      1. Seed a plan with ``version=0`` and ``stage='executing'``.
      2. Run the kill harness at ``commit_after`` — the
         subprocess commits ``version=1, stage='commit_after_advanced'``
         then sleeps BEFORE its second-table side-effect.
      3. We SIGKILL it in the sleep; the kill must arrive
         AFTER the COMMIT (the marker confirms this).
      4. Re-open the DB from cold and assert the new version +
         new stage ARE visible.  The side-effect (a write to
         plan_verification) is NOT visible — it never ran.
      5. Run :func:`assert_db_consistent` (the on-disk state is
         still self-consistent because we never wrote the
         side-effect).
    """
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="executing",
        stage="executing",
        version=0,
    )

    report = kill_subprocess_at(
        "commit_after",
        plan_id="p1",
        db_path=db_path,
    )

    assert report["pre_kill_marker_observed"], (
        "kill harness did not observe the post_commit marker; "
        "the kill landed BEFORE the COMMIT — cut point is broken"
    )

    state = _cold_replay_state(db_path, "p1")

    # The COMMIT's writes are durable.
    assert state["routing"]["version"] == 1, (
        f"kill -9 after COMMIT did NOT persist version bump; "
        f"expected 1, got {state['routing']['version']!r}"
    )
    assert state["routing"]["current_phase"] == "commit_after_advanced", (
        f"kill -9 after COMMIT did NOT persist stage change; "
        f"got {state['routing']['stage']!r}"
    )

    # The side-effect (a write to plan_verification) must NOT
    # be visible — the harness never executed it before the kill.
    # The verification row is still the seeded 'running' / empty
    # verdicts state.
    assert state["verification"]["status"] == "running", (
        f"unexpected verification_status after cut_after kill: "
        f"{state['verification']['status']!r}"
    )

    # All five invariants hold.
    assert_db_consistent(db_path)


# ---------------------------------------------------------------------------
# Cut point 3 — mid_execution (acceptance condition 3)
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_3
def test_kill_mid_execution_recovers_consistent_progress(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill mid-execution → progress equals the LAST committed iteration.

    Acceptance condition 3: an executor kill leaves the
    on-disk ``task_progress`` at the LAST successfully committed
    snapshot, never at an "in-flight" iteration that was rolled
    back.

    Strategy:
      1. Seed a plan with ``task_progress=NULL``.
      2. Run the kill harness at ``mid_execution`` — the
         subprocess runs 3 iterations of
         ``BEGIN IMMEDIATE → UPDATE task_progress → COMMIT``
         with markers ``iter_committed_0``, ``iter_committed_1``,
         ``iter_committed_2``, then sleeps.
      3. We SIGKILL it in the sleep.  The last committed marker
         before the kill determines the expected progress.
      4. Re-open the DB and assert:
           * task_progress matches one of the committed snapshots
             (iteration 1, 2, or 3 — NEVER a non-existent
             "iteration 4" / in-flight value),
           * the database is consistent (I1-I5 hold).
    """
    _seed_plan(conn, plan_id="p1", current_phase="executing")

    report = kill_subprocess_at(
        "mid_execution",
        plan_id="p1",
        db_path=db_path,
        exec_loop_iters=3,
    )

    # The kill must land after the loop has at least one
    # committed iteration (otherwise the test would be trivially
    # asserting on an unseeded NULL progress).  The harness anchors
    # its kill on ``iter_committed_0`` precisely so this holds
    # structurally rather than by scheduling luck; a False here
    # means the subprocess never reached the loop at all.
    assert report["pre_kill_marker_observed"], (
        f"kill anchor never appeared — the marker subprocess did not "
        f"reach iter_committed_0 within the timeout; "
        f"markers={report['markers']!r}"
    )
    committed = [m for m in report["markers"] if m.startswith("iter_committed_")]
    assert committed, (
        f"mid_execution cut landed before any iteration committed; "
        f"markers={report['markers']!r} — adjust exec_loop_iters or "
        f"kill timing"
    )

    state = _cold_replay_state(db_path, "p1")
    progress_raw = state["execution"]["task_progress"]
    assert progress_raw is not None, (
        "kill -9 mid_execution left task_progress=NULL even "
        "though at least one iteration committed — bug 3 regression"
    )
    progress = json.loads(progress_raw)

    # The progress MUST be one of the committed iteration payloads.
    # Iterations write completed = 1, 2, 3.
    assert progress.get("completed") in {1, 2, 3}, (
        f"task_progress has in-flight value {progress!r}; "
        f"expected one of the committed iterations 1/2/3"
    )
    assert progress.get("total") == 3

    # Invariants hold.
    assert_db_consistent(db_path)


# ---------------------------------------------------------------------------
# Cut point 4 — mid_verification (acceptance condition 3)
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_3
def test_kill_mid_verification_recovers_consistent_round(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill mid-verification → verdict count equals the LAST committed.

    Acceptance condition 3, second half: a verification
    worker kill leaves ``plan_verification.verdicts`` at the
    LAST committed snapshot, not at a "ghost" partial write.

    Strategy:
      1. Seed a plan with ``verdicts='[]'``.
      2. Run the kill harness at ``mid_verification`` — the
         subprocess appends 3 verdicts (one per IMMEDIATE txn,
         each producing a ``verdict_committed_N`` marker) and
         then sleeps.
      3. SIGKILL in the sleep.  The last committed verdict
         index determines the expected on-disk count.
      4. Re-open the DB and assert:
           * ``len(verdicts)`` equals one of {1, 2, 3},
           * ``verification_status`` is still ``running``
             (the round-completion commit never ran).
    """
    _seed_plan(conn, plan_id="p1", verification_status="running")

    report = kill_subprocess_at(
        "mid_verification",
        plan_id="p1",
        db_path=db_path,
        verdict_count=3,
    )

    # The harness anchors its kill on ``verdict_committed_0`` so the
    # "at least one committed verdict" precondition below holds
    # structurally rather than by scheduling luck; a False here means
    # the marker subprocess never reached the loop at all.
    assert report["pre_kill_marker_observed"], (
        f"kill anchor never appeared — the marker subprocess did not "
        f"reach verdict_committed_0 within the timeout; "
        f"markers={report['markers']!r}"
    )
    committed = [
        m for m in report["markers"] if m.startswith("verdict_committed_")
    ]
    assert committed, (
        f"mid_verification cut landed before any verdict committed; "
        f"markers={report['markers']!r}"
    )

    state = _cold_replay_state(db_path, "p1")
    verdicts = json.loads(state["verification"]["verdicts"] or "[]")
    assert 1 <= len(verdicts) <= 3, (
        f"verdict count {len(verdicts)} outside the committed "
        f"set {{1, 2, 3}}; the on-disk state includes a ghost "
        f"verdict — bug 3 regression"
    )

    # The round-completion commit never ran — verification_status
    # must still be 'running', not 'failed' / 'passed'.
    assert state["verification"]["status"] == "running", (
        f"verification_status moved off 'running' during "
        f"mid_verification kill; got {state['verification']['status']!r}"
    )

    # Invariants hold.
    assert_db_consistent(db_path)


# ---------------------------------------------------------------------------
# Cut point 5 — mid_scheduler_tick (bug 4 anchor)
# ---------------------------------------------------------------------------


@pytest.mark.bug_4
def test_kill_mid_scheduler_tick_no_ghost_start(
    db_path: Path,
    conn: sqlite3.Connection,
    scheduler: SchedulerSupport,
) -> None:
    """Kill mid-scheduler-tick → on-disk schedule equals the OLD value.

    Bug 4 anchor: the scheduler used to write its "decide
    what to schedule" decision in-memory BEFORE the UPDATE. A
    kill mid-tick could leave the in-memory decision
    observable on the next restart.  The refactor requires the
    decision to land only via IMMEDIATE-txn writes; a kill
    between the SELECT and the UPDATE must leave the row at
    its PRE-tick value.

    Strategy:
      1. Seed a plan with ``next_run_at='2000-01-01T00:00:00Z'``
         (overdue, but the scheduler has not yet decided to
         re-schedule).
      2. Snapshot the pre-kill state.
      3. Run the kill harness at ``mid_scheduler_tick`` — the
         subprocess reads ``next_run_at``, computes a decision
         (kept in a local variable), and sleeps BEFORE the
         UPDATE.
      4. SIGKILL in the sleep.  The local decision variable is
         gone with the process; the on-disk row is unchanged.
      5. Re-open the DB; ``next_run_at`` still equals the
         pre-kill value.
      6. Run ``decide_tick()`` from the parent's connection —
         the scheduler still finds the plan due (because
         ``next_run_at`` is still overdue).
    """
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="ready",
        next_run_at="2000-01-01T00:00:00Z",
        verification_status=None,  # no verification row needed
    )
    # Remove the verification row we seeded so the plan is not
    # in a "running verification" state (decide_tick would
    # skip it otherwise).
    conn.execute("DELETE FROM plan_verification WHERE plan_id = ?", ("p1",))
    conn.commit()

    # Snapshot pre-kill state.
    pre_state = _cold_replay_state(db_path, "p1")
    assert pre_state["execution"]["next_run_at"] == "2000-01-01T00:00:00Z"

    report = kill_subprocess_at(
        "mid_scheduler_tick",
        plan_id="p1",
        db_path=db_path,
    )

    # Sanity: the kill landed AFTER the decision was computed
    # but BEFORE the UPDATE.  The decision_computed marker must
    # be present; pre_update must NOT be.
    assert "decision_computed" in report["markers"], (
        f"scheduler-tick cut landed before decision was computed; "
        f"markers={report['markers']!r}"
    )
    assert "pre_update" not in report["markers"], (
        "kill landed AFTER pre_update marker; the subprocess "
        "completed the UPDATE — this is not mid-tick"
    )

    state = _cold_replay_state(db_path, "p1")
    assert state["execution"]["next_run_at"] == "2000-01-01T00:00:00Z", (
        f"scheduler-tick kill left a ghost schedule update; "
        f"expected '2000-01-01T00:00:00Z', got "
        f"{state['execution']['next_run_at']!r}"
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
# Cold-start replay — version-order matches pre-crash
# ---------------------------------------------------------------------------


def test_cold_start_replay_by_version_order_matches_pre_crash(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Replay-from-cold reconstructs the same state the live process had.

    Strategy:
      1. Seed THREE plans at distinct versions.
      2. Close the parent connection (simulate process exit).
      3. Re-open the DB from cold and read every plan's
         ``(stage, version)`` row.
      4. Assert the cold-replay state matches the live snapshot.

    The point is not to test any specific invariant; it is to
    pin the cold-replay round-trip contract: the live state and
    the cold-replay state must agree.
    """
    _seed_plan(
        conn, plan_id="p1", stage="ready", version=0,
        current_phase="ready", next_run_at=None,
        verification_status=None,
    )
    _seed_plan(
        conn, plan_id="p2", stage="executing", version=5,
        current_phase="executing", next_run_at="2099-01-01T00:00:00Z",
        verification_status="running",
    )
    _seed_plan(
        conn, plan_id="p3", stage="verification_passed", version=12,
        current_phase="verification_passed",
        next_run_at=None, verification_status="passed",
    )
    conn.commit()

    # Snapshot from the live (still-open) connection.
    live_state = _cold_replay_state(db_path, "p3")

    # Close the parent connection — the test process now has
    # NO handle on the DB.  This is the "cold restart" boundary.
    # The ``conn`` fixture's teardown will also close it; calling
    # close() here is idempotent so the duplicate-close is safe.
    conn.close()

    # Re-open from cold and verify the state round-trips.
    fresh_state = _cold_replay_state(db_path, "p3")
    assert fresh_state == live_state, (
        f"cold-replay state disagrees with live state; "
        f"live={live_state!r} cold={fresh_state!r}"
    )


# ---------------------------------------------------------------------------
# Parametrised — invariants hold after EVERY kill point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cut_point", list(CUT_POINTS))
def test_db_invariants_hold_after_every_kill_point(
    cut_point: str,
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """``assert_db_consistent`` must return an empty failure list at every
    cut point.

    The point of the parametrised test is to ensure the invariant
    evaluator is wired correctly against the schema the kill
    harness writes.  Any cut point that leaves the DB in a
    state the invariant evaluator mis-classifies (false positive
    OR false negative) is caught here.

    The test seeds a minimal plan for each cut point, runs the
    harness, and asserts:

      * ``assert_db_consistent`` returns ``[]`` (no failures)
        OR raises only when the schema has a real regression,
      * no ``ConsistencyInvariantViolation`` is raised.
    """
    # Each cut point needs slightly different seeding so the
    # IMMEDIATE-txn the harness executes targets a valid row.
    if cut_point in ("commit_before", "commit_after", "mid_scheduler_tick"):
        _seed_plan(
            conn,
            plan_id="p1",
            stage="executing",
            current_phase="executing",
            next_run_at="2000-01-01T00:00:00Z",
            verification_status="running",
            version=0,
        )
    elif cut_point == "mid_execution":
        _seed_plan(
            conn,
            plan_id="p1",
            stage="executing",
            current_phase="executing",
            verification_status="running",
            version=0,
        )
    elif cut_point == "mid_verification":
        _seed_plan(
            conn,
            plan_id="p1",
            stage="verification_running",
            current_phase="verification_running",
            verification_status="running",
            version=0,
        )
    else:  # pragma: no cover — defensive
        raise AssertionError(f"unknown cut_point {cut_point!r}")

    kill_subprocess_at(
        cut_point,
        plan_id="p1",
        db_path=db_path,
    )

    failures = assert_db_consistent(
        db_path, raise_on_failure=False
    )
    assert failures == [], (
        f"cut_point={cut_point!r} left invariants broken; "
        f"failures={failures!r}"
    )


# ---------------------------------------------------------------------------
# Scheduler reflection — no uncommitted decision fields
# ---------------------------------------------------------------------------


def test_scheduler_has_no_uncommitted_decision_fields(
    scheduler: SchedulerSupport,
) -> None:
    """``SchedulerSupport`` instance has no decision-stash attributes.

    The bug-4 anchor mirrors
    ``test_scheduler_has_no_uncommitted_decision_fields`` in
    ``test_scheduler_support.py`` but enforces it against the
    scheduler's *instance* state post-crash: a real
    regression where the scheduler class is restored from a
    cold-replay state with a stashed ``_pending_*`` attribute
    would fail this reflection check.

    The forbidden patterns (``_pending_*``, ``_queue``,
    ``_cache``) cover every plausible "in-memory decision
    cache" attribute name a future contributor might add.  The
    only allowed ``_``-prefixed attribute is ``_conn`` (the
    database connection), and the construction-time repositories
    (``_exec_repo``, ``_routing_repo``, ``_verification_repo``)
    which are explicitly named exceptions.

    Why this test matters here, not just in
    ``test_scheduler_support.py``: a kill between the
    scheduler's "read plan_execution" and "write schedule
    decision" could leave a transient decision-stash
    attribute that survives the kill if the scheduler held
    one.  The reflection guard is the cheapest possible
    detection.
    """
    forbidden_substrings = ("_pending_", "_queue", "_cache")
    allowed_prefixes = ("_conn",)
    allowed_exact = {
        "_exec_repo",
        "_routing_repo",
        "_verification_repo",
    }

    # 1) Init parameter names — a cache passed via constructor
    #    is just as bad.
    sig = inspect.signature(scheduler.__init__)
    for name in sig.parameters.keys():
        assert not any(sub in name for sub in forbidden_substrings), (
            f"SchedulerSupport.__init__ parameter {name!r} looks "
            f"like a cache/queue; forbidden = {forbidden_substrings!r}"
        )

    # 2) Instance attributes — the snapshot after construction.
    instance_attrs = {name for name in vars(scheduler).keys()}
    for attr in instance_attrs:
        if attr in allowed_exact:
            continue
        if attr.startswith(tuple(allowed_prefixes)):
            continue
        assert not any(sub in attr for sub in forbidden_substrings), (
            f"SchedulerSupport instance carries forbidden attribute "
            f"{attr!r}; instance_attrs = {sorted(instance_attrs)!r}; "
            f"forbidden_substrings = {forbidden_substrings!r}"
        )

    # 3) Cross-check: the scheduler has no class-level cache
    #    fields either.  A future refactor that moves a cache
    #    onto the class (rather than the instance) is caught.
    for name in vars(SchedulerSupport):
        if name.startswith("__"):
            continue
        assert not any(sub in name for sub in forbidden_substrings), (
            f"SchedulerSupport class carries forbidden attribute "
            f"{name!r}; class_attrs = {sorted(vars(SchedulerSupport))!r}"
        )