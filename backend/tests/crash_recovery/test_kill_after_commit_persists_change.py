"""Acceptance test 2 (commit_after) — crash recovery cut point.

This file lives at the path the verification harness expects
(``tests/crash_recovery/test_kill_after_commit_persists_change.py``)
and re-implements the test in a self-contained way so that the
``db_path`` / ``conn`` fixtures resolve locally. The single source
of truth remains ``state_machine/tests/unit/test_crash_recovery.py``.

The test verifies acceptance condition 2 (the bug 3 anchor):

  * ``kill -9`` AFTER ``COMMIT`` but BEFORE the subsequent
    side-effect must leave the COMMIT's row durable on disk.
    The on-disk state must reflect the post-COMMIT state —
    the version bump and the stage rewrite ARE visible.
  * The side-effect (which would have written a sibling table)
    is NOT visible — the kill arrived before the side-effect
    ran.
  * Invariants I1-I5 all hold after cold-start replay.

Together with :func:`test_kill_before_commit_leaves_no_trace`,
this pins the contract: a row is durable IFF its COMMIT has
landed. Anything between BEGIN and COMMIT rolls back; anything
after COMMIT survives. This is the bug 3 cut-point semantics
the architecture decision point 5 contract pins.
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

    The defaults represent a plan whose ``version=0`` will be
    CAS-bumped + ``stage`` rewritten in an IMMEDIATE txn that
    WILL commit.  After COMMIT the harness sleeps BEFORE the
    side-effect write so we can SIGKILL it cleanly.
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
    """Re-open the database from cold and read routing + execution + verification."""
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
        verif_row = c.execute(
            "SELECT verification_status, verdicts FROM plan_verification "
            "WHERE plan_id = ?",
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
            "verification": {
                "status": verif_row[0] if verif_row else None,
                "verdicts": verif_row[1] if verif_row else None,
            },
        }
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Acceptance condition 2 — cut point commit_after
# ---------------------------------------------------------------------------


@pytest.mark.bug_3
def test_kill_after_commit_persists_change(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill AFTER COMMIT, BEFORE side-effect → COMMIT's row IS visible.

    Bug 3 anchor — complementary to
    :func:`test_kill_before_commit_leaves_no_trace`.  Once
    ``COMMIT`` has returned, the row is durable on disk; a
    subsequent kill must NOT roll it back.

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
        f"got {state['routing']['current_phase']!r}"
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
