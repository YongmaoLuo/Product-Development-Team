"""Acceptance test 1 (commit_before) — crash recovery cut point.

This file lives at the path the verification harness expects
(``tests/crash_recovery/test_kill_before_commit_leaves_no_trace.py``)
and re-implements the test in a self-contained way so that the
``db_path`` / ``conn`` fixtures resolve locally. The single source
of truth remains ``state_machine/tests/unit/test_crash_recovery.py``.

The test verifies acceptance condition 1 (the bug 3 anchor):

  * ``kill -9`` after ``BEGIN IMMEDIATE`` but BEFORE ``COMMIT``
    must NOT advance the routing row.  The on-disk state must
    equal the pre-txn snapshot — a SIGKILL in the BEGIN→COMMIT
    window is rolled back by SQLite's journal.
  * The ``version`` CAS and the ``stage`` rewrite that the
    in-flight txn attempted are BOTH invisible post-cold-replay.
  * Invariants I1-I5 all hold after cold-start replay.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
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


def _seed_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    current_phase: str = "executing",
    stage: str = "executing",
    next_run_at: str | None = "2000-01-01T00:00:00Z",
    verification_status: str | None = "running",
    version: int = 0,
) -> None:
    """Insert a minimal plan across the four plan_* tables.

    The defaults represent a plan whose ``version=0`` is about
    to be CAS-bumped in an IMMEDIATE transaction that the test
    will SIGKILL mid-flight.  ``task_progress`` defaults to
    ``NULL`` — the routing row is the only thing the cut point
    is allowed to mutate, and it must NOT mutate it on a kill
    before commit.
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
        " task_progress, next_run_at, updated_at) "
        "VALUES (?, ?, 0, NULL, NULL, ?, '2026-08-05T00:00:00Z')",
        (plan_id, current_phase, next_run_at),
    )
    if verification_status is not None:
        conn.execute(
            "INSERT INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            " verdicts, results, updated_at) "
            "VALUES (?, ?, 0, 3, NULL, NULL, '2026-08-05T00:00:00Z')",
            (plan_id, verification_status),
        )
    conn.commit()


def _cold_replay_state(db_path: Path, plan_id: str) -> dict:
    """Re-open the database from cold and read the routing + execution state."""
    c = open_db(db_path)
    try:
        routing_row = c.execute(
            "SELECT current_phase, substage, version FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        exec_row = c.execute(
            "SELECT current_phase, task_progress FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return {
            "routing": {
                "current_phase": routing_row[0] if routing_row else None,
                "substage": routing_row[1] if routing_row else None,
                "version": routing_row[2] if routing_row else None,
            },
            "execution": {
                "current_phase": exec_row[0] if exec_row else None,
                "task_progress": exec_row[1] if exec_row else None,
            },
        }
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Acceptance condition 1 — cut point commit_before
# ---------------------------------------------------------------------------


@pytest.mark.bug_3
def test_kill_before_commit_leaves_no_trace(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill between ``BEGIN IMMEDIATE`` and ``COMMIT`` leaves no advance.

    Bug 3 anchor — the contract: every write method on the
    state-machine repositories is wrapped in
    ``BEGIN IMMEDIATE → COMMIT``; a SIGKILL between the BEGIN
    and the COMMIT must NOT leave the row advanced.

    Strategy:
      1. Seed a plan with ``stage='executing'`` and a known
         ``version=0``.
      2. Run the kill harness at ``commit_before`` — the
         subprocess opens ``BEGIN IMMEDIATE``, writes
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
        f"current_phase to {state['routing']['current_phase']!r} — "
        f"bug 3 regression: partial write is observable"
    )

    # All five invariants hold.
    assert_db_consistent(db_path)
