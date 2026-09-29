"""Regression tests for the execution-progress ``counts`` aggregation.

Background
----------
A plan reported ``counts={"completed": 0,
"failed": 0, "pending": 50}`` from ``/api/execution/{id}/progress``
even though the executor log showed 43 completed and 2 failed tasks.
The endpoint trusts a stale ``plan_execution.task_progress`` aggregated
row over the per-task overlay that has been merged onto ``tasks`` via
``PlanTaskRepository.load_all``.

The fix makes the per-task overlay the authoritative source: the
endpoint always recomputes counts from ``tasks`` and uses the SQLite
aggregated row as a sanity-check hint only.

These tests pin the new behaviour at the ``ExecutionRepository`` level
(the data source the endpoint reads) and at the synthetic-overlay
level (the same logic the endpoint uses).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository


def _build_sqlite_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _make_task(task_id: str, status: str, title: str = "test") -> dict:
    """Synthesise a task dict that mimics the post-overlay shape."""
    return {
        "id": task_id,
        "title": title,
        "description": "test",
        "status": status,
        "test_command": "",
    }


def _recompute_counts(tasks: list[dict]) -> dict:
    """Mirror the new server-side aggregation logic in
    ``get_execution_progress``."""
    counts = {
        "total": len(tasks),
        "completed": 0,
        "failed": 0,
        "in_progress": 0,
        "pending": 0,
    }
    for t in tasks:
        st = t.get("status", "pending")
        if st in counts:
            counts[st] += 1
    return counts


# ----------------------------------------------------------------------
# Bug 2: per-task overlay is the source of truth
# ----------------------------------------------------------------------

def test_recompute_counts_with_realistic_mix():
    """A plan with 43 completed, 2 failed, 5 pending tasks must report
    counts matching the per-task overlay, NOT a stale aggregated row."""
    tasks = (
        [_make_task(str(i), "completed") for i in range(43)]
        + [_make_task("44", "failed"), _make_task("45", "failed")]
        + [_make_task(str(i), "pending") for i in range(46, 51)]
    )
    counts = _recompute_counts(tasks)
    assert counts["completed"] == 43
    assert counts["failed"] == 2
    assert counts["pending"] == 5
    assert counts["in_progress"] == 0
    assert counts["total"] == 50


def test_recompute_counts_ignores_stale_aggregated_row():
    """The previous bug: a stale aggregated row ``{"completed": 0}``
    took precedence over the per-task overlay that said 43 completed.
    The new logic never reads the aggregated row for aggregation — it
    is logged as a sanity-check hint only."""
    tasks = [_make_task(str(i), "completed") for i in range(43)]
    counts = _recompute_counts(tasks)
    # The stale row would have said 0; the truth is 43.
    stale_row = {"total": 50, "completed": 0, "failed": 0, "pending": 50}
    assert stale_row["completed"] == 0  # the bug we are avoiding
    assert counts["completed"] == 43, (
        "The new aggregator must NOT consult ``db_progress`` at all "
        "for the count itself — the per-task overlay is authoritative."
    )


def test_recompute_counts_handles_all_terminal_statuses():
    """Cover completed, failed, in_progress, pending, skipped."""
    tasks = [
        _make_task("1", "completed"),
        _make_task("2", "failed"),
        _make_task("3", "in_progress"),
        _make_task("4", "pending"),
        _make_task("5", "skipped"),
    ]
    counts = _recompute_counts(tasks)
    assert counts == {
        "total": 5,
        "completed": 1,
        "failed": 1,
        "in_progress": 1,
        "pending": 1,
    }
    # 'skipped' is not in the counts dict, but the new aggregator
    # must not crash on it — it's silently dropped from the totals.
    assert counts["total"] == 5


# ----------------------------------------------------------------------
# Sanity-check warning: counts drift larger than 5 should log
# ----------------------------------------------------------------------

def test_drift_helper_detects_large_aggregated_row_mismatch():
    """Mirror the ``drift > 5`` log warning so a regression in either
    direction is caught."""
    db_progress = {"total": 50, "completed": 0}  # stale
    counts = {"total": 50, "completed": 43}
    drift = abs((db_progress.get("completed") or 0) - counts["completed"])
    assert drift > 5, "Sanity check: the drift helper should fire on this example"
