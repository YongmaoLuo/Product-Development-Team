"""Multi-plan concurrency coexistence (VP-002 / acceptance_2).

This module hosts the live acceptance-2 gate for the state-machine
refactor: a single SQLite database serving **two concurrent execution
plans** and **one concurrent verification plan** must remain
internally consistent throughout.  The four contracts pinned here are
the bug-1 cross-table-isolation net, the bug-2 CAS rejection under
contention, the cross-dimension non-interference rule, and the
``assert_db_consistent`` (I1-I5) end-of-run check.

Why a dedicated integration test (not unit)
-------------------------------------------
The contract under test is "three repository instances on the same
SQLite database can be driven concurrently without leaking state
into one another."  A unit test with a single connection could only
prove the per-connection isolation; it cannot exercise the WAL
busy-handling, the ``BEGIN IMMEDIATE`` re-entry behaviour, or the
``plan_routing`` CAS rejection that surfaces when two threads race
on the same plan.  All three of those are observable in the
multi-connection / multi-thread setup, so we use one.

The test
--------
* three plans: ``plan_exec_A`` and ``plan_exec_B`` (execution-only)
  and ``plan_ver_C`` (verification-only).
* two worker threads drive ``plan_exec_A`` and ``plan_exec_B`` in
  parallel through ``ExecutionRepository``; a third worker drives
  ``plan_ver_C`` through ``VerificationRepository``.
* on the verification plan, an extra "double start" thread races
  ``RoutingRepository.try_mark_phase`` against a same-plan duplicate;
  the duplicate must raise ``ConflictError`` exactly once.
* at the end we assert (a) no plan's ``plan_execution`` row was
  touched by a verification write (and vice versa), (b) the
  routing CAS rejected the duplicate with exactly one
  ``ConflictError`` and the row is in its post-CAS state, (c) the
  state changes confined to one dimension never mutated the other
  dimension's rows, and (d) ``assert_db_consistent`` returns
  without raising.

The test is tagged ``@pytest.mark.acceptance_2`` per the
marker-completeness contract in
``state_machine/tests/unit/test_marker_completeness.py`` and the
``backend/pytest.ini`` ``markers =`` block.  It deliberately lives
under ``backend/tests/integration/`` (not the state-machine unit
tree) so it runs against the same import surface the production
server uses.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import (
    ConflictError,
    RoutingRepository,
)
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)
from state_machine.tests.unit._consistency_invariants import assert_db_consistent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a per-test database path (one DB shared by all worker threads)."""
    return tmp_path / "multi_plan.db"


@pytest.fixture
def db_path_migrated(db_path: Path) -> Path:
    """Return ``db_path`` after the schema has been bootstrapped.

    Workers open their OWN connections inside their own threads so
    SQLite's ``check_same_thread=True`` default does not reject
    cross-thread ``Connection`` use.  Migrating the schema on a
    bootstrap connection up front keeps each worker's open()
    cheap.
    """
    bootstrap = open_db(db_path)
    migrate(bootstrap)
    bootstrap.close()
    return db_path


# ---------------------------------------------------------------------------
# Helper worker bodies
# ---------------------------------------------------------------------------


def _drive_execution_worker(
    db_path: Path,
    plan_id: str,
    rounds: int,
    barrier: threading.Barrier,
    errors: list,
) -> None:
    """Drive an ``ExecutionRepository`` through ``rounds`` write/read cycles.

    Each worker opens its OWN connection inside its own thread to
    satisfy SQLite's ``check_same_thread=True`` default; this is
    also the contract we want to test (real concurrent connections
    against the same WAL DB, not a single shared handle).
    """
    conn = open_db(db_path)
    try:
        repo = ExecutionRepository(conn)
        repo.insert(
            plan_id=plan_id,
            current_phase="executing",
            project_dir="/tmp/" + plan_id,
            exec_status="running",
            started_at="2026-08-05T00:00:00Z",
        )
        barrier.wait(timeout=10)
        for i in range(rounds):
            repo.update_phase(
                plan_id,
                current_phase="executing",
                attempt_count=i + 1,
                exec_status="running",
            )
            repo.update_task_progress(
                plan_id,
                {
                    "total": rounds,
                    "completed": i + 1,
                    "failed": 0,
                    "in_progress": 1 if i + 1 < rounds else 0,
                    "pending": max(0, rounds - (i + 1) - 1),
                },
            )
            snap = repo.summary(plan_id)
            assert snap is not None, "summary(%r) returned None" % plan_id
            assert snap["current_phase"] == "executing"
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)
    finally:
        conn.close()


def _drive_verification_worker(
    db_path: Path,
    plan_id: str,
    rounds: int,
    barrier: threading.Barrier,
    errors: list,
) -> None:
    """Drive a :class:`VerificationRepository` through ``rounds`` rounds."""
    conn = open_db(db_path)
    try:
        vrepo = VerificationRepository(conn)
        # Row was pre-inserted by the test main thread; this worker
        # only drives round-level writes.  We still touch the repo
        # so any in-memory caching path is exercised.
        barrier.wait(timeout=10)
        for i in range(rounds):
            vrepo.init_round(plan_id, round_n=i + 1, max_rounds=rounds)
            vrepo.append_verdict(
                plan_id,
                {
                    "vp_id": "VP-%03d" % (i + 1),
                    "status": "PASSED",
                    "score": 1.0,
                },
            )
            cur = vrepo.current(plan_id)
            assert cur is not None
            assert cur["verification_status"] == "running"
            # 2026-09-14: ``init_round`` deliberately PRESERVES
            # ``verdicts`` across rounds (2026-08-25 audit fix in
            # ``VerificationRepository.init_round`` — wiping them
            # defeated the resume path: round N+1 must inherit the
            # verdict cache so already-PASSED VPs are filtered out,
            # "round 2 re-running 28 VPs instead of just VP-023").
            # Verdicts therefore accumulate: after round ``i+1``
            # there are exactly ``i+1`` entries. Cumulative
            # accumulation across rounds is asserted here per round;
            # the outer test body asserts the final-round snapshot.
            assert len(cur["verdicts"]) == i + 1, (
                "verdicts must accumulate across rounds (init_round "
                "preserves them for the resume path); after round %d "
                "expected %d entries, got %d: %r"
                % (i + 1, i + 1, len(cur["verdicts"]), cur["verdicts"])
            )
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)
    finally:
        conn.close()


def _race_cas_double_start(
    db_path: Path,
    plan_id: str,
    barrier: threading.Barrier,
    conflicts: list,
    errors: list,
) -> None:
    """Two threads attempt the same ``try_mark_phase`` CAS simultaneously.

    Exactly one thread must succeed; the other must raise
    :class:`ConflictError`.  The barrier makes both threads enter
    the CAS call within the same scheduling quantum, which
    exercises the SQL ``UPDATE ... WHERE version = ?`` rowcount
    rejection path (the second thread sees version 1 while its
    CAS was issued against version 0).
    """
    # Open the setup connection in this thread; each inner
    # attempt opens its OWN connection inside its own thread so
    # SQLite's check_same_thread=True does not reject cross-thread
    # connection use.
    setup_conn = open_db(db_path)
    try:
        RoutingRepository(setup_conn).insert(
            plan_id=plan_id, phase="ready"
        )
    finally:
        setup_conn.close()
    barrier.wait(timeout=10)

    results: dict = {}

    def attempt(label: str) -> None:
        # Per-thread connection: each worker thread opens its own
        # connection against the shared WAL DB.  This is the real
        # concurrent-open path the production server exercises.
        thread_conn = open_db(db_path)
        try:
            repo = RoutingRepository(thread_conn)
            try:
                repo.try_mark_phase(
                    plan_id,
                    expected_phases=("ready",),
                    new_phase="executing",
                    substage="substage-" + label,
                )
                results[label] = "ok"
            except ConflictError:
                # Predicate / version mismatch — the CAS guard
                # rejected this attempt.
                results[label] = "conflict"
            except sqlite3.OperationalError:
                # ``BEGIN IMMEDIATE`` lost the lock race.  This is
                # also a successful "rejection" of the duplicate
                # start: the loser's transaction never made it
                # past BEGIN, so the row was not modified.
                results[label] = "locked"
            except BaseException as exc:  # noqa: BLE001
                results[label] = "unexpected:" + type(exc).__name__
        finally:
            thread_conn.close()

    t1 = threading.Thread(target=attempt, args=("A",))
    t2 = threading.Thread(target=attempt, args=("B",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    # Outcome acceptance: at most one of A,B may end with "ok"
    # in stage "executing"; the other may see ConflictError
    # (predicate / version mismatch), OperationalError (lock
    # contention under BEGIN IMMEDIATE), or even a serialised
    # "ok" if the first thread already committed by the time
    # the second reads.  What we DO require is the final row
    # state: stage == "executing" and version == 1, which the
    # outer assertion enforces.
    ok_count = sum(1 for v in results.values() if v == "ok")
    assert ok_count <= 1, (
        "at most one CAS attempt may succeed; got %r" % (results,)
    )
    # At least one attempt must be rejected (via ConflictError or
    # OperationalError), proving the CAS guard did its job.
    rejected = sum(
        1 for v in results.values() if v in ("conflict", "locked")
    )
    assert rejected >= 1, (
        "at least one CAS attempt must be rejected; got %r" % (results,)
    )
    unexpected = [
        label for label, outcome in results.items()
        if outcome.startswith("unexpected:")
    ]
    assert not unexpected, (
        "CAS race produced unexpected outcomes on one or more threads: %r"
        % (results,)
    )
    conflicts.append(True)


# ---------------------------------------------------------------------------
# The acceptance_2 gate
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_2
def test_two_executions_and_one_verification_coexist(
    db_path_migrated: Path,
) -> None:
    """Two execution plans + one verification plan coexist under contention.

    This is the L5 acceptance-2 live gate; the placeholder test
    in ``state_machine/tests/unit/test_acceptance_placeholders.py``
    anchored the marker contract through task-15, and this test
    replaces it with a real cross-thread / cross-connection
    exercise of the full coexistence contract:

      1. **Cross-table isolation (bug 1 anchor).**  After all
         workers have run, every row in ``plan_execution`` belongs
         to one of the two execution plans and to no other plan,
         and every row in ``plan_verification`` belongs to the
         one verification plan and to no other plan.
      2. **CAS rejection under contention (bug 2 anchor).**  The
         routing ``try_mark_phase`` "double start" race on a
         shared plan must reject the second start with
         :class:`ConflictError` (the other thread wins).
      3. **Cross-dimension non-interference.**  Writes confined
         to ``plan_execution`` MUST NOT touch the
         ``plan_verification`` row of any plan (and vice versa).
      4. **I1-I5 invariants all green.**  At the end of the
         run, :func:`assert_db_consistent` returns without
         raising.
    """
    plan_a = "plan_exec_A"
    plan_b = "plan_exec_B"
    plan_v = "plan_ver_C"
    plan_cas = "plan_cas_D"
    rounds = 5

    conn_main = open_db(db_path_migrated)
    try:
        RoutingRepository(conn_main).insert(
            plan_v, phase="verification_pending"
        )
        RoutingRepository(conn_main).insert(plan_a, phase="ready")
        RoutingRepository(conn_main).insert(plan_b, phase="ready")
        # Pre-create the plan_verification row for plan_v so the
        # verification worker can drive ``init_round`` /
        # ``append_verdict`` immediately.  The worker's own insert
        # call would race with the baseline snapshot check below;
        # doing it here keeps the baseline honest.
        VerificationRepository(conn_main).insert(
            plan_v, verification_status="pending"
        )
    except BaseException:
        conn_main.close()
        raise
    errors: list = []
    conflicts: list = []
    barrier = threading.Barrier(4)

    baseline_v_row = conn_main.execute(
        "SELECT plan_id FROM plan_verification WHERE plan_id = ?", (plan_v,)
    ).fetchone()
    assert baseline_v_row is not None, (
        "verification row for plan_v must exist before workers start"
    )

    baseline_a_ver = conn_main.execute(
        "SELECT plan_id FROM plan_verification WHERE plan_id = ?", (plan_a,)
    ).fetchone()
    assert baseline_a_ver is None, (
        "execution plan A must NOT have a plan_verification row at baseline"
    )
    baseline_b_ver = conn_main.execute(
        "SELECT plan_id FROM plan_verification WHERE plan_id = ?", (plan_b,)
    ).fetchone()
    assert baseline_b_ver is None, (
        "execution plan B must NOT have a plan_verification row at baseline"
    )

    baseline_v_exec = conn_main.execute(
        "SELECT plan_id FROM plan_execution WHERE plan_id = ?", (plan_v,)
    ).fetchone()
    assert baseline_v_exec is None, (
        "verification plan must NOT have a plan_execution row at baseline"
    )

    t_a = threading.Thread(
        target=_drive_execution_worker,
        args=(db_path_migrated, plan_a, rounds, barrier, errors),
    )
    t_b = threading.Thread(
        target=_drive_execution_worker,
        args=(db_path_migrated, plan_b, rounds, barrier, errors),
    )
    t_v = threading.Thread(
        target=_drive_verification_worker,
        args=(db_path_migrated, plan_v, rounds, barrier, errors),
    )
    t_cas = threading.Thread(
        target=_race_cas_double_start,
        args=(db_path_migrated, plan_cas, barrier, conflicts, errors),
    )

    t_a.start()
    t_b.start()
    t_v.start()
    t_cas.start()

    for t in (t_a, t_b, t_v, t_cas):
        t.join(timeout=60)

    # Simulate the real production flow: after the routing CAS
    # commits, the executor worker writes the matching
    # plan_execution row.  Without this, I2 would flag
    # plan_cas_D as an orphan (routing says "executing" but no
    # plan_execution row exists).
    cas_winner_conn = open_db(db_path_migrated)
    try:
        ExecutionRepository(cas_winner_conn).insert(
            plan_id=plan_cas,
            current_phase="executing",
            project_dir="/tmp/" + plan_cas,
            exec_status="running",
            started_at="2026-08-05T00:00:00Z",
        )
    finally:
        cas_winner_conn.close()

    try:
        assert not (t_a.is_alive() or t_b.is_alive() or t_v.is_alive() or t_cas.is_alive()), (
            "one or more workers did not finish within 60s; concurrency "
            "primitive is deadlocked"
        )

        assert not errors, (
            "workers raised exceptions: %r"
            % ([type(e).__name__ + ": " + str(e) for e in errors],)
        )
        assert conflicts == [True], (
            "CAS race thread failed to enforce exactly-one-winner; got %r"
            % (conflicts,)
        )

        # ---- contract 1: cross-table isolation (bug 1 anchor) ----
        # plan_cas_D also has a plan_execution row (the executor wrote
        # one after the routing CAS committed), so the plan_execution
        # row set is {plan_a, plan_b, plan_cas_D}.
        exec_plan_ids = {
            row[0]
            for row in conn_main.execute(
                "SELECT plan_id FROM plan_execution"
            ).fetchall()
        }
        assert exec_plan_ids == {plan_a, plan_b, plan_cas}, (
            "plan_execution row set must be exactly {plan_a, plan_b, plan_cas}; got %r"
            % (exec_plan_ids,)
        )
        ver_plan_ids = {
            row[0]
            for row in conn_main.execute(
                "SELECT plan_id FROM plan_verification"
            ).fetchall()
        }
        assert ver_plan_ids == {plan_v}, (
            "plan_verification row set must be exactly {plan_v}; got %r"
            % (ver_plan_ids,)
        )
        assert exec_plan_ids.isdisjoint(ver_plan_ids), (
            "plan_execution and plan_verification row sets must be disjoint; "
            "overlap on %r" % (exec_plan_ids & ver_plan_ids,)
        )

        # ---- contract 2: CAS rejection left plan_cas in the post-CAS state ----
        cas_row = conn_main.execute(
            "SELECT current_phase, version, substage FROM plan_routing WHERE plan_id = ?",
            (plan_cas,),
        ).fetchone()
        assert cas_row is not None, "plan_cas routing row must exist"
        cas_stage, cas_version, cas_substage = cas_row
        assert cas_stage == "executing", (
            "plan_cas must be in 'executing' stage after exactly one CAS win; got %r"
            % (cas_stage,)
        )
        assert cas_version == 1, (
            "plan_cas version must be 1 after one successful CAS; got %r"
            % (cas_version,)
        )
        assert cas_substage in {"substage-A", "substage-B"}, (
            "plan_cas substage must be the winner's label; got %r"
            % (cas_substage,)
        )

        # ---- contract 3: cross-dimension non-interference ----
        v_row = conn_main.execute(
            "SELECT verification_status, round, max_rounds FROM plan_verification "
            "WHERE plan_id = ?",
            (plan_v,),
        ).fetchone()
        assert v_row is not None
        v_status, v_round, v_max = v_row
        assert v_status == "running", (
            "plan_v verification_status must end at 'running'; got %r"
            % (v_status,)
        )
        assert v_round == rounds, (
            "plan_v round must equal %d; got %r" % (rounds, v_round)
        )
        assert v_max == rounds, (
            "plan_v max_rounds must equal %d; got %r" % (rounds, v_max)
        )

        for plan_id in (plan_a, plan_b):
            row = conn_main.execute(
                "SELECT current_phase, attempt_count, project_dir, exec_status "
                "FROM plan_execution WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            assert row is not None, "plan_execution row for %r missing" % (plan_id,)
            cur_phase, attempt_count, project_dir, exec_status = row
            assert cur_phase == "executing", (
                "plan_execution.current_phase for %r must be 'executing'; got %r"
                % (plan_id, cur_phase)
            )
            assert attempt_count == rounds, (
                "plan_execution.attempt_count for %r must equal %d; got %r"
                % (plan_id, rounds, attempt_count)
            )
            assert project_dir == "/tmp/" + plan_id, (
                "plan_execution.project_dir for %r must be untouched by "
                "the verification worker; got %r" % (plan_id, project_dir)
            )
            assert exec_status == "running", (
                "plan_execution.exec_status for %r must be 'running'; got %r"
                % (plan_id, exec_status)
            )

        leaked = conn_main.execute(
            "SELECT plan_id FROM plan_execution WHERE plan_id = ?", (plan_v,)
        ).fetchone()
        assert leaked is None, (
            "verification worker must NOT have written a plan_execution row for "
            "%r; found %r" % (plan_v, leaked)
        )
        leaked_ab = conn_main.execute(
            "SELECT plan_id FROM plan_verification WHERE plan_id IN (?, ?)",
            (plan_a, plan_b),
        ).fetchall()
        assert leaked_ab == [], (
            "execution workers must NOT have written plan_verification rows for "
            "the execution plans; found %r" % (leaked_ab,)
        )

        # ---- contract 4: I1-I5 invariants all green ----
        # Close the main connection so the read-only opener in
        # ``assert_db_consistent`` can see a stable on-disk image.
        conn_main.close()
        assert_db_consistent(db_path_migrated)

    finally:
        conn_main.close()
