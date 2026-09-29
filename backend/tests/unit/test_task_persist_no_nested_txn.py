"""Tests that ``task_manager._persist_status_to_sqlite`` no longer needs nested transactions.

Schema v4 (2026-09-09): after splitting
per-task state into the ``plan_tasks`` table, ``update_task`` is a
single SQL statement with row-level atomic write.  The legacy
``_persist_status_to_sqlite`` had:

  * ``INSERT OR IGNORE INTO plan_execution ...`` to bootstrap the
    plan_execution row (no longer required — plan_tasks is independent)
  * ``repo.update_task(...)`` with version snapshot
  * retry loop on ``TaskProgressConflictError``
  * explicit ``conn.rollback()`` between attempts

Plan v4 simplifies to a single ``update_task`` call with no retries
(no conflict exception is raised in v4) and no bootstrap INSERT.
This test pins the new shape.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def state_conn(state_db_path: Path) -> sqlite3.Connection:
    c = open_db(state_db_path)
    migrate(c)
    yield c
    c.close()


def _make_task_manager_with_db(state_db_path: Path):
    """Build a TaskManager instance whose _persist_status_to_sqlite
    writes to ``state_db_path``.
    """
    from task_manager import TaskManager

    tm = TaskManager(tasks_file=Path("/tmp/fake_tasks.json"))
    # Override the persistence path to point at our test DB
    return tm


def test_persist_status_writes_directly_to_plan_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_persist_status_to_sqlite must write to plan_tasks (single SQL)."""
    # Set up: fake state.db under tmp_path
    db_path = tmp_path / "state.db"
    state_conn = open_db(db_path)
    migrate(state_conn)
    state_conn.close()

    # Make TaskManager use this DB via env var
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    from task_manager import TaskManager, derive_plan_id_from_tasks_file
    tasks_file = tmp_path / "fake_plan_id" / "tasks.json"
    tasks_file.parent.mkdir()
    tasks_file.write_text("[]")
    tm = TaskManager(project_dir=tasks_file.parent, tasks_file=tasks_file)
    plan_id = derive_plan_id_from_tasks_file(tasks_file)
    task_id = "t-1"

    # Act
    tm._persist_status_to_sqlite(task_id, "completed")

    # Assert: the row landed in plan_tasks with the new status
    verify_conn = open_db(db_path)
    try:
        cur = verify_conn.execute(
            "SELECT status, end_ts FROM plan_tasks "
            "WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        row = cur.fetchone()
        assert row is not None, (
            "v4 persist must write directly to plan_tasks"
        )
        assert row[0] == "completed"
        # end_ts must be ISO-8601 UTC
        try:
            datetime.strptime(row[1], "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            pytest.fail(f"end_ts must be ISO-8601 UTC, got {row[1]!r}")
    finally:
        verify_conn.close()


def test_persist_status_does_not_require_plan_execution_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v4 persist must NOT raise TaskProgressNotFound when the
    plan_execution row is absent — plan_tasks is independent.
    """
    db_path = tmp_path / "state.db"
    state_conn = open_db(db_path)
    migrate(state_conn)
    state_conn.close()
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    from task_manager import TaskManager, derive_plan_id_from_tasks_file
    tasks_file = tmp_path / "some-plan" / "tasks.json"
    tasks_file.parent.mkdir()
    tasks_file.write_text("[]")
    tm = TaskManager(project_dir=tasks_file.parent, tasks_file=tasks_file)
    plan_id = derive_plan_id_from_tasks_file(tasks_file)

    # Plan has NO plan_execution row (legacy code would have
    # INSERT OR IGNORE'd it; v4 code must not need it).
    tm._persist_status_to_sqlite("t-x", "completed")

    c = open_db(db_path)
    try:
        cur = c.execute(
            "SELECT status FROM plan_tasks WHERE plan_id=? AND task_id=?",
            (plan_id, "t-x"),
        )
        assert cur.fetchone() is not None
    finally:
        c.close()


def test_persist_status_no_retry_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v4 persist must not retry on conflict — there is no conflict
    exception to retry on.  We assert this by injecting a counter
    that increments per ``update_task`` call.
    """
    db_path = tmp_path / "state.db"
    state_conn = open_db(db_path)
    migrate(state_conn)
    state_conn.close()
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    from task_manager import TaskManager, derive_plan_id_from_tasks_file
    from state_machine.repositories import plan_task_repository as ptr_mod

    tasks_file = tmp_path / "x-plan" / "tasks.json"
    tasks_file.parent.mkdir()
    tasks_file.write_text("[]")
    tm = TaskManager(project_dir=tasks_file.parent, tasks_file=tasks_file)
    plan_id = derive_plan_id_from_tasks_file(tasks_file)

    call_count = {"n": 0}
    original = ptr_mod.PlanTaskRepository.update_task

    def counting_update_task(self, *args, **kwargs):
        call_count["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ptr_mod.PlanTaskRepository, "update_task", counting_update_task)

    tm._persist_status_to_sqlite("t-y", "completed")

    assert call_count["n"] == 1, (
        f"v4 persist must call update_task exactly once, got {call_count['n']}"
    )