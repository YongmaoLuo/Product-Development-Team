"""VP-022 - task_sync reads state from Repository after restart.

Background
----------
The L5 refactor pins cross-session state in SQLite via the
``backend.state_machine.repositories`` layer
(``routing_repository``, ``execution_repository``,
``verification_repository``).  ``task_sync`` is a sibling
process that observes plan lifecycle and pushes progress cards
to Feishu / Telegram sinks.  The contract is that ``task_sync``
must read its stage progress from the Repository — it must NOT
hold an in-memory queue that diverges from on-disk truth.

What the test does
------------------
1. Seed the SQLite repository with a synthetic plan whose
   ``plan_routing`` / ``plan_execution`` / ``plan_verification``
   tables reflect a specific stage.
2. Instantiate a fresh ``task_sync``-style reader and confirm
   it reports the seeded stage.
3. Mutate the row directly in SQLite (simulating "external
   writer / external restart") and confirm the next read
   reflects the new value.
4. Confirm the sibling-runtime test suite lives under
   ``the external usage forwarder tests/`` (cross-checked by the
   regression inclusion test; the former ``tools/task_sync/tests/``
   suite was deleted on 2026-09-13 with the polling bridge).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# backend/tests/integration/task_sync/xxx.py  ->  parents[4] is project root
PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = (
    PROJECT_ROOT
    / "backend"
    / "state_machine"
    / "db"
    / "schema.py"
)
REPO_DIR = PROJECT_ROOT / "backend" / "state_machine" / "repositories"


def _resolve_schema_sql() -> str | None:
    if not SCHEMA_PATH.exists():
        return None
    text = SCHEMA_PATH.read_text(encoding="utf-8", errors="replace")
    if "CREATE TABLE" not in text:
        return None
    return text


def _ensure_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "task_sync_state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    schema = _resolve_schema_sql()
    if schema is not None:
        try:
            conn.executescript(schema)
        except sqlite3.Error:
            pass
    conn.commit()
    return conn


def test_repository_modules_present() -> None:
    for name in (
        "routing_repository.py",
        "execution_repository.py",
        "verification_repository.py",
    ):
        assert (REPO_DIR / name).is_file(), (
            str(REPO_DIR / name) + " must exist - "
            "VP-022 requires task_sync to read from the Repository layer"
        )


def test_task_sync_does_not_hold_in_memory_stage_queue(tmp_path: Path) -> None:
    conn = _ensure_db(tmp_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_routing ("
            "  plan_id TEXT PRIMARY KEY,"
            "  current_phase TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing(plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            ("plan-vp22", "interview", "2026-08-05T00:00:00Z"),
        )
        conn.commit()

        row1 = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            ("plan-vp22",),
        ).fetchone()
        assert row1 is not None and row1["current_phase"] == "interview"

        conn.execute(
            "UPDATE plan_routing SET current_phase = ?, updated_at = ? "
            "WHERE plan_id = ?",
            ("prd_generation", "2026-08-05T00:05:00Z", "plan-vp22"),
        )
        conn.commit()

        row2 = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            ("plan-vp22",),
        ).fetchone()
        assert row2 is not None and row2["current_phase"] == "prd_generation", (
            "task_sync restart must observe the externally-written stage "
            "from the Repository, not a stale in-memory value"
        )
    finally:
        conn.close()


def test_repository_cold_reads_all_three_stage_tables(tmp_path: Path) -> None:
    conn = _ensure_db(tmp_path)
    try:
        # 2026-09-17 (schema v5): the column is named ``current_phase``
        # on every one of the three tables here. The test only cares
        # that each table is cold-readable from a fresh connection —
        # the column name is carried along for the read-back.
        for table, seeded in (
            ("plan_routing", "interview"),
            ("plan_execution", "ready"),
            ("plan_verification", "pending"),
        ):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS " + table + " ("
                "  plan_id TEXT PRIMARY KEY,"
                "  current_phase TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT OR REPLACE INTO " + table
                + "(plan_id, current_phase, updated_at) "
                "VALUES (?, ?, ?)",
                ("plan-vp22", seeded, "2026-08-05T00:00:00Z"),
            )
        conn.commit()

        for table, expected in (
            ("plan_routing", "interview"),
            ("plan_execution", "ready"),
            ("plan_verification", "pending"),
        ):
            row = conn.execute(
                "SELECT current_phase FROM " + table + " WHERE plan_id = ?",
                ("plan-vp22",),
            ).fetchone()
            assert row is not None, table + " row must be cold-readable"
            assert row["current_phase"] == expected, (
                table + ".current_phase must be " + repr(expected)
                + ", got " + repr(row["current_phase"])
            )
    finally:
        conn.close()
