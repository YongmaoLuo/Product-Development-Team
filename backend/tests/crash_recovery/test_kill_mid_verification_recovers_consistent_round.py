"""Acceptance test 4 (mid_verification) — crash recovery cut point.

This file lives at the path the verification harness expects
(``tests/crash_recovery/test_kill_mid_verification_recovers_consistent_round.py``)
and re-implements the test in a self-contained way so that the
``db_path`` / ``conn`` fixtures resolve locally. The single source
of truth remains ``state_machine/tests/unit/test_crash_recovery.py``.

The test verifies acceptance condition 4:

  * ``kill -9`` mid-verification → ``plan_verification.verdicts``
    holds the LAST committed verdict snapshot, never a ghost /
    in-flight value
  * ``verification_status`` is NOT mis-classified as ``passed`` /
    ``failed`` / ``loop_stopped`` after a cold-start replay
    (the round-completion commit never landed)
  * The round counter visible after the kill equals the round
    that was current BEFORE the kill (no in-flight round bump)
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
    current_phase: str = "verification",
    stage: str = "verification_running",
    next_run_at: str | None = "2000-01-01T00:00:00Z",
    verification_status: str | None = "running",
    verdicts_seed: list[dict] | None = None,
    round_num: int = 2,
    version: int = 0,
) -> None:
    """Insert a minimal plan across the four plan_* tables.

    The defaults represent a plan mid-verification: the
    routing layer is in ``verification_running``, the
    verification row reports ``verification_status='running'``,
    and the round counter is ``2`` (i.e. an in-flight round 2).
    The ``verdicts`` column is initialised to ``[]`` so the
    subprocess can append per-verdict commits cleanly.
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
            "VALUES (?, ?, ?, 3, ?, NULL, '2026-08-05T00:00:00Z')",
            (
                plan_id,
                verification_status,
                round_num,
                json.dumps(verdicts_seed or []),
            ),
        )
    conn.commit()


def _cold_replay_state(db_path: Path, plan_id: str) -> dict:
    """Re-open the database from cold and read the post-crash snapshot."""
    c = open_db(db_path)
    try:
        routing_row = c.execute(
            "SELECT current_phase, substage, version FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        verification_row = c.execute(
            "SELECT verification_status, round, verdicts "
            "FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return {
            "routing": {
                "current_phase": routing_row[0] if routing_row else None,
                "substage": routing_row[1] if routing_row else None,
                "version": routing_row[2] if routing_row else None,
            },
            "verification": {
                "status": verification_row[0] if verification_row else None,
                "round": verification_row[1] if verification_row else None,
                "verdicts": verification_row[2] if verification_row else None,
            },
        }
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Acceptance condition 4 — cut point mid_verification
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_3
def test_kill_mid_verification_recovers_consistent_round(
    db_path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Kill mid-verification → consistent round + verdict snapshot.

    Acceptance condition 4: a verification worker kill leaves
    the on-disk database in a self-consistent state:

      * ``plan_verification.verdicts`` holds the LAST
        successfully committed verdict snapshot (count ∈ {1, 2, 3}
        — never 0, never 4, never a partial in-flight write).
      * ``verification_status`` is still ``running`` (the
        round-completion commit never landed, so the worker
        must not be mis-classified as ``passed`` / ``failed``
        / ``loop_stopped``).
      * ``round`` equals the round that was current BEFORE the
        kill (we seed round=2; the subprocess does not bump
        round, so the post-kill value must also be 2).
      * Invariants I1-I5 hold.

    Strategy:
      1. Seed a plan with ``verdicts='[]'`` and ``round=2``.
      2. Run the kill harness at ``mid_verification`` — the
         subprocess appends 3 verdicts (one per IMMEDIATE txn,
         each producing a ``verdict_committed_N`` marker) and
         then sleeps.
      3. SIGKILL in the sleep.  The last committed verdict
         index determines the expected on-disk count.
      4. Re-open the DB and assert all four properties above.
    """
    _seed_plan(
        conn,
        plan_id="p1",
        current_phase="verification",
        stage="verification_running",
        verification_status="running",
        round_num=2,
    )

    report = kill_subprocess_at(
        "mid_verification",
        plan_id="p1",
        db_path=db_path,
        verdict_count=3,
    )

    # The kill must land after the loop has at least one
    # committed verdict (otherwise the test would be trivially
    # asserting on an unseeded empty verdicts list).  The harness
    # anchors its kill on ``verdict_committed_0`` precisely so this
    # holds structurally rather than by scheduling luck; a False here
    # means the subprocess never reached the loop at all.
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
        f"markers={report['markers']!r} — adjust verdict_count or "
        f"kill timing"
    )

    state = _cold_replay_state(db_path, "p1")
    verdicts_raw = state["verification"]["verdicts"]
    assert verdicts_raw is not None, (
        "kill -9 mid_verification left plan_verification.verdicts "
        "NULL even though at least one verdict committed — bug 3 regression"
    )
    verdicts = json.loads(verdicts_raw)

    # The verdict count MUST be one of the committed snapshots.
    # Iterations append 1, 2, 3 verdicts.
    assert 1 <= len(verdicts) <= 3, (
        f"verdict count {len(verdicts)} outside the committed "
        f"set {{1, 2, 3}}; the on-disk state includes a ghost "
        f"verdict — bug 3 regression"
    )

    # The round-completion commit never ran — verification_status
    # must still be 'running', not 'passed' / 'failed' /
    # 'loop_stopped'.  Mis-classifying as 'passed' would let a
    # crashed verifier silently green-light a plan.
    assert state["verification"]["status"] == "running", (
        f"verification_status moved off 'running' during "
        f"mid_verification kill; got {state['verification']['status']!r} "
        "— the cold-start replay must NOT mis-classify an interrupted "
        "round as a terminal result"
    )

    # The round counter must equal the round that was current
    # before the kill — the subprocess does not bump round, so
    # the post-kill value must still be 2 (the seeded value).
    assert state["verification"]["round"] == 2, (
        f"round counter drifted during mid_verification kill; "
        f"expected 2 (seeded), got {state['verification']['round']!r}"
    )

    # The routing stage must reflect the verification-running
    # sub-phase (not a terminal stage).
    assert state["routing"]["current_phase"] == "verification_running", (
        f"plan_routing.current_phase should be 'verification_running' after "
        f"kill mid-verification, got {state['routing']['current_phase']!r}"
    )

    # Invariants hold.
    assert_db_consistent(db_path)
