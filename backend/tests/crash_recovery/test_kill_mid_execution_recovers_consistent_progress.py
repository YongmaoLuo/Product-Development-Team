"""Acceptance test 3 (mid_execution) — crash recovery cut point.

This file lives at the path the verification harness expects
(``tests/crash_recovery/test_kill_mid_execution_recovers_consistent_progress.py``)
and re-implements the test in a self-contained way so that the
``db_path`` / ``conn`` fixtures resolve locally. The single source of
truth remains ``state_machine/tests/unit/test_crash_recovery.py``.

The test verifies acceptance condition 3:

  * ``kill -9`` mid-execution → task_progress equals the LAST committed
    iteration snapshot (not an in-flight value)
  * ``plan_routing.stage`` correctly reflects the executing sub-phase
  * Invariants I1-I5 all hold after cold-start replay
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

    The default values represent a plan in mid-execution whose
    ``verification_status='running'`` and ``next_run_at`` is in
    the past.
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
# Acceptance condition 3 — cut point mid_execution
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
      2. Run the kill harness at ``mid_execution`` — the subprocess
         runs 3 iterations of
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

    # plan_routing.current_phase must reflect the executing sub-phase.
    assert state["routing"]["current_phase"] == "executing", (
        f"plan_routing.current_phase should be 'executing' after kill mid-execution, "
        f"got {state['routing']['current_phase']!r}"
    )

    # Invariants hold.
    assert_db_consistent(db_path)
