"""Tests for the rewritten :meth:`ExecutionRepository.progress` against plan_tasks.

Schema v4 (2026-09-09): the per-task
aggregation ``progress()`` now reads from the new ``plan_tasks``
table rather than parsing the legacy ``plan_execution.task_progress``
JSON column.  The contract from the API perspective is unchanged:

  * Return ``None`` when there are no tasks for the plan.
  * Return ``{"total": N, "completed": n, "failed": n, ...}`` when
    tasks exist, with counts by status.

The implementation now uses ``SELECT status, COUNT(*) ... GROUP BY
status`` against ``plan_tasks`` — a single index scan, no JSON
parsing, no race window.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository


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


@pytest.fixture
def exec_repo(conn: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(conn)


@pytest.fixture
def task_repo(conn: sqlite3.Connection) -> PlanTaskRepository:
    return PlanTaskRepository(conn)


def test_progress_returns_none_when_no_tasks(
    exec_repo: ExecutionRepository,
) -> None:
    """No plan_tasks rows for the plan → None."""
    assert exec_repo.progress("no-such-plan") is None


def test_progress_aggregates_from_plan_tasks_table(
    exec_repo: ExecutionRepository, task_repo: PlanTaskRepository,
) -> None:
    """Counts come from plan_tasks — not the legacy task_progress JSON column."""
    task_repo.update_task("p1", "1", {"status": "completed"}, expected_version=0)
    task_repo.update_task("p1", "2", {"status": "completed"}, expected_version=0)
    task_repo.update_task("p1", "3", {"status": "failed"}, expected_version=0)
    task_repo.update_task("p1", "4", {"status": "in_progress"}, expected_version=0)
    task_repo.update_task("p1", "5", {"status": "pending"}, expected_version=0)
    # And a task in a different plan that must NOT be counted
    task_repo.update_task("p2", "x", {"status": "completed"}, expected_version=0)

    counts = exec_repo.progress("p1")
    assert counts is not None
    assert counts["total"] == 5
    assert counts["completed"] == 2
    assert counts["failed"] == 1
    assert counts["in_progress"] == 1
    assert counts["pending"] == 1


def test_progress_ignores_legacy_task_progress_json_column(
    exec_repo: ExecutionRepository, conn: sqlite3.Connection,
) -> None:
    """Even if plan_execution.task_progress JSON has stale counts,
    the new ``progress()`` only reads from plan_tasks — stale JSON
    cannot affect the aggregate.
    """
    # Insert a plan_execution row with stale task_progress JSON
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, task_progress, updated_at) "
        "VALUES (?, 'executing', ?, '2026-01-01T00:00:00Z')",
        (
            "p-stale",
            '{"tasks": {"a":": {"status": "completed"}, "b":": {"status": "failed"}}}',
        ),
    )
    conn.commit()
    # No plan_tasks rows for p-stale — progress() returns None
    # even though task_progress JSON has 2 entries.
    assert exec_repo.progress("p-stale") is None


def test_progress_zero_pending_after_all_complete(
    exec_repo: ExecutionRepository, task_repo: PlanTaskRepository,
) -> None:
    """All-completed plans still show the right counts."""
    for tid in ("a", "b", "c"):
        task_repo.update_task("p1", tid, {"status": "completed"}, expected_version=0)
    counts = exec_repo.progress("p1")
    assert counts == {
        "total": 3,
        "completed": 3,
        "failed": 0,
        "in_progress": 0,
        "pending": 0,
    }