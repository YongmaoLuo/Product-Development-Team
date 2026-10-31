"""The scheduler's claim on a run is the one thing that survives a restart.

``scheduler_ownership.scheduler_holds`` is the single predicate the
startup reconcile (``routes.execution._reconcile_exec_pid``) and the
startup sweep (``service_manager.reap_all_plans``) ask before they take
a run away from the process that is still running it.  These tests pin
the two answers that matter:

* the answer comes from the scheduler's **own** table, so it survives
  the backend that wrote it; and
* an answer that cannot be read is *no*, so a broken database leaves
  every other rule alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scheduler_ownership import scheduler_holds


def _make_db(path: Path, plan_ids: list) -> Path:
    """Create a state database with a ``scheduler_slot`` table."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS scheduler_slot ("
            "  plan_id TEXT PRIMARY KEY,"
            "  acquired_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL)"
        )
        for plan_id in plan_ids:
            connection.execute(
                "INSERT INTO scheduler_slot (plan_id, acquired_at, updated_at) "
                "VALUES (?, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')",
                (plan_id,),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def test_a_plan_the_scheduler_holds_is_answered_yes(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "state.db", ["p1"])

    assert scheduler_holds("p1", db_path=db) is True


def test_a_plan_the_scheduler_does_not_hold_is_answered_no(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "state.db", ["p1"])

    assert scheduler_holds("p2", db_path=db) is False


def test_a_database_without_the_slot_table_answers_no(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    connection = sqlite3.connect(str(db))
    connection.execute("CREATE TABLE unrelated (x TEXT)")
    connection.commit()
    connection.close()

    assert scheduler_holds("p1", db_path=db) is False


def test_a_database_that_does_not_exist_answers_no(tmp_path: Path) -> None:
    assert scheduler_holds("p1", db_path=tmp_path / "missing.db") is False


def test_an_empty_plan_id_is_answered_no(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "state.db", ["p1"])

    assert scheduler_holds("", db_path=db) is False
