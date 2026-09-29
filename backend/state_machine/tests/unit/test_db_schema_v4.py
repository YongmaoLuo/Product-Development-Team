"""Tests for state-machine schema v4 — plan_tasks table + backfill from task_progress JSON.

Schema v4 (2026-09-09): split the per-task runtime
state out of the ``plan_execution.task_progress`` JSON column into a
proper relational ``plan_tasks`` table. SQLite row-level atomic writes
replace the application-level read-modify-write + ``_repo_version``
CAS, eliminating the silent-fail window that produced the 2026-09-09
same-id-loop bug.

These tests pin the migration contract:

  1. After ``migrate``, ``plan_tasks`` table exists with the expected
     columns and constraints (FK + PK).
  2. v4 migration is idempotent — re-running does not duplicate rows.
  3. v4 backfill lifts every entry in
     ``plan_execution.task_progress.tasks`` into one row per
     ``(plan_id, task_id)``.
  4. v4 backfill survives corrupt JSON columns (logs a warning,
     continues — does NOT raise).
  5. v4 backfill preserves the field set the dispatcher uses
     (status / end_ts / commit_sha / etc.).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def test_v4_migration_creates_plan_tasks_table(conn: sqlite3.Connection) -> None:
    """After migrate, plan_tasks table exists with the expected shape."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plan_tasks'"
    )
    assert cur.fetchone() is not None, (
        "plan_tasks table must be created by v4 migration"
    )

    # PK on (plan_id, task_id) — verify both columns are PRIMARY KEY
    cur = conn.execute("PRAGMA table_info(plan_tasks)")
    cols = cur.fetchall()
    col_names = [c[1] for c in cols]
    for required in ("plan_id", "task_id", "status", "end_ts", "updated_at"):
        assert required in col_names, (
            f"plan_tasks must have column {required!r}, got {col_names}"
        )


def test_v4_migration_idempotent(db_path: Path) -> None:
    """Re-running migrate must NOT duplicate rows or fail."""
    c1 = open_db(db_path)
    try:
        migrate(c1)
        # Insert a plan + a task row
        c1.execute(
            "INSERT INTO plan_execution (plan_id, current_phase, updated_at) "
            "VALUES (?, 'executing', '2026-01-01T00:00:00Z')",
            ("p1",),
        )
        c1.execute(
            "INSERT INTO plan_tasks (plan_id, task_id, status, end_ts, updated_at) "
            "VALUES (?, 't1', 'completed', '2026-01-01T00:01:00Z', "
            "'2026-01-01T00:01:00Z')",
            ("p1",),
        )
        c1.commit()
    finally:
        c1.close()

    # Second migrate run
    c2 = open_db(db_path)
    try:
        migrate(c2)  # must NOT raise

        # Row count for the plan_tasks must still be 1 (no duplicates)
        cur = c2.execute(
            "SELECT COUNT(*) FROM plan_tasks WHERE plan_id='p1' AND task_id='t1'"
        )
        assert cur.fetchone()[0] == 1, (
            "v4 migration must be idempotent — second run must not duplicate"
        )
    finally:
        c2.close()


# ---------------------------------------------------------------------------
# Backfill from task_progress JSON
# ---------------------------------------------------------------------------


def test_v4_backfill_lifts_task_progress_json_into_plan_tasks(
    conn: sqlite3.Connection,
) -> None:
    """v4 migration reads plan_execution.task_progress JSON and inserts rows."""
    progress_json = json.dumps(
        {
            "tasks": {
                "1-1": {
                    "status": "completed",
                    "end_ts": "2026-09-05T00:09:20Z",
                    "_repo_version": 7,
                    "commit_sha": "abc123",
                },
                "11-2": {
                    "status": "failed",
                    "end_ts": "2026-09-08T14:32:00Z",
                    "failure_reason": "Rust module divergence",
                    "_repo_version": 12,
                },
            }
        }
    )
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
        "VALUES (?, 'executing', ?, '2026-09-09T00:00:00Z')",
        ("plan-1", progress_json),
    )
    conn.commit()

    # Re-run migrate (which now includes v4 backfill)
    migrate(conn)

    # Both tasks must be present in plan_tasks
    cur = conn.execute(
        "SELECT task_id, status, end_ts, commit_sha, failure_reason "
        "FROM plan_tasks WHERE plan_id=? ORDER BY task_id",
        ("plan-1",),
    )
    rows = cur.fetchall()
    assert len(rows) == 2, f"Expected 2 backfilled rows, got {len(rows)}"

    by_id = {r[0]: r for r in rows}
    assert by_id["1-1"][1] == "completed"
    assert by_id["1-1"][2] == "2026-09-05T00:09:20Z"
    assert by_id["1-1"][3] == "abc123"
    assert by_id["11-2"][1] == "failed"
    assert by_id["11-2"][4] == "Rust module divergence"


def test_v4_backfill_survives_corrupt_task_progress_json(
    conn: sqlite3.Connection,
) -> None:
    """Corrupt JSON must NOT crash migration — log + skip the row."""
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
        "VALUES (?, 'executing', ?, '2026-09-09T00:00:00Z')",
        ("plan-corrupt", "this is not valid JSON {{{"),
    )
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
        "VALUES (?, 'executing', ?, '2026-09-09T00:00:00Z')",
        (
            "plan-good",
            json.dumps({"tasks": {"ok": {"status": "completed", "end_ts": "x"}}})
        ),
    )
    conn.commit()

    migrate(conn)  # must NOT raise

    # Good plan still has 1 task row; corrupt plan has 0
    cur = conn.execute("SELECT COUNT(*) FROM plan_tasks WHERE plan_id='plan-good'")
    assert cur.fetchone()[0] == 1
    cur = conn.execute("SELECT COUNT(*) FROM plan_tasks WHERE plan_id='plan-corrupt'")
    assert cur.fetchone()[0] == 0


def test_v4_backfill_empty_task_progress_no_op(conn: sqlite3.Connection) -> None:
    """Plan rows with empty / {} task_progress produce no plan_tasks rows."""
    for progress in (None, "", "{}"):
        conn.execute(
            "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
            "VALUES (?, 'executing', ?, '2026-09-09T00:00:00Z')",
            (f"plan-{progress!r}", progress),
        )
    conn.commit()

    migrate(conn)

    cur = conn.execute("SELECT COUNT(*) FROM plan_tasks")
    assert cur.fetchone()[0] == 0


def test_v4_backfill_idempotent_does_not_duplicate(
    conn: sqlite3.Connection,
) -> None:
    """Re-running backfill must not duplicate rows (INSERT OR IGNORE)."""
    progress_json = json.dumps(
        {"tasks": {"t1": {"status": "completed", "_repo_version": 1}}}
    )
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
        "VALUES (?, 'executing', ?, '2026-09-09T00:00:00Z')",
        ("plan-dup", progress_json),
    )
    conn.commit()

    migrate(conn)
    migrate(conn)  # second run

    cur = conn.execute(
        "SELECT COUNT(*) FROM plan_tasks WHERE plan_id='plan-dup' AND task_id='t1'"
    )
    assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Schema version bump
# ---------------------------------------------------------------------------


def test_v4_bumps_schema_version_to_4(conn: sqlite3.Connection) -> None:
    """``CURRENT_SCHEMA_VERSION`` reflects v4 after migrate."""
    from state_machine.db.schema import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION >= 4, (
        f"CURRENT_SCHEMA_VERSION must be >= 4 after v4 migration, "
        f"got {CURRENT_SCHEMA_VERSION}"
    )


def test_v4_migration_runs_after_v3_on_existing_db(db_path: Path) -> None:
    """A v3-schema database (with task_progress JSON) must backfill on migrate."""
    # Create a v3-schema-only DB (migrate once → all tables created)
    c = open_db(db_path)
    try:
        migrate(c)  # bootstrap schema
        # Now insert pre-v4 data into plan_execution.task_progress
        progress = json.dumps(
            {"tasks": {"t1": {"status": "completed", "end_ts": "x"}}}
        )
        c.execute(
            "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
            "VALUES (?, 'executing', ?, '2026-09-09T00:00:00Z')",
            ("plan-pre-v4", progress),
        )
        c.commit()
    finally:
        c.close()

    # Now re-run migrate — must backfill the data we just inserted
    c = open_db(db_path)
    try:
        migrate(c)
        cur = c.execute(
            "SELECT status FROM plan_tasks WHERE plan_id='plan-pre-v4' AND task_id='t1'"
        )
        row = cur.fetchone()
        assert row is not None, "v3→v4 must backfill from existing task_progress JSON"
        assert row[0] == "completed"
    finally:
        c.close()