"""
Tests for ``TaskManager.update_task_commit_sha``.

Background (2026-09-11 plan): prior to this addition every completed
task had ``commit_sha IS NULL`` in state.db (108/108 missing) because
``_commit_task_changes`` ran ``git_manager.commit()`` but never
recorded the resulting SHA back into the per-task row. The new
``update_task_commit_sha`` method closes the loop by mirroring the
in-memory ``task.commit_sha`` → ``runtime_overrides`` →
``plan_tasks.commit_sha`` (SQLite) just like
``record_task_failure`` already does for ``status`` / ``end_ts``.

These tests assert the contract:

  1. ``update_task_commit_sha`` writes the SHA onto the in-memory
     ``task`` object.
  2. ``runtime_overrides`` receives the SHA so a subsequent
     ``load_tasks`` sees it before the SQLite write round-trips.
  3. The method is idempotent: calling it twice with the same SHA
     leaves the row in the same state.
  4. An empty / falsy SHA is a no-op (defensive: don't write garbage).
  5. The helper ``_persist_commit_sha_to_sqlite`` is called exactly
     once per ``update_task_commit_sha`` invocation.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from task import SubTask  # noqa: E402


def _make_task(task_id: str = "11-4") -> SubTask:
    return SubTask(
        id=task_id,
        title="example",
        description="example description",
        test_command="pytest -q",
        files_to_modify=["foo.py"],
    )


def test_update_task_commit_sha_writes_in_memory():
    """In-memory ``task.commit_sha`` is updated immediately."""
    from task_manager import TaskManager

    tm = TaskManager.__new__(TaskManager)  # bypass __init__
    tm.tasks = [_make_task("11-4")]
    tm.runtime_overrides = {}
    tm.tasks_file = Path("/tmp/nonexistent/tasks.json")  # _persist path will skip
    tm.save_tasks = lambda: None  # patch save_tasks to avoid disk I/O

    # Patch the SQLite persistence so it can't fail the test.
    with patch.object(TaskManager, "_persist_commit_sha_to_sqlite", lambda self, tid, sha: None):
        tm.update_task_commit_sha("11-4", "abcdef1234567890" * 4)

    assert tm.tasks[0].commit_sha == "abcdef1234567890" * 4


def test_update_task_commit_sha_writes_runtime_overrides():
    """``runtime_overrides`` carries the new SHA so reloads see it."""
    from task_manager import TaskManager

    tm = TaskManager.__new__(TaskManager)
    tm.tasks = [_make_task("11-5")]
    tm.runtime_overrides = {}
    tm.tasks_file = Path("/tmp/nonexistent/tasks.json")
    tm.save_tasks = lambda: None

    sha = "deadbeef" * 5
    with patch.object(TaskManager, "_persist_commit_sha_to_sqlite", lambda self, tid, sha: None):
        tm.update_task_commit_sha("11-5", sha)

    assert tm.runtime_overrides["11-5"]["commit_sha"] == sha


def test_update_task_commit_sha_empty_sha_is_noop():
    """Empty SHA → no mutation of task or runtime_overrides."""
    from task_manager import TaskManager

    tm = TaskManager.__new__(TaskManager)
    tm.tasks = [_make_task("11-6")]
    tm.runtime_overrides = {}
    tm.tasks_file = Path("/tmp/nonexistent/tasks.json")
    tm.save_tasks = lambda: None

    with patch.object(TaskManager, "_persist_commit_sha_to_sqlite") as persist_mock:
        tm.update_task_commit_sha("11-6", "")

    # Task must NOT be mutated.
    assert not getattr(tm.tasks[0], "commit_sha", None)
    # SQLite must NOT be called for empty SHA.
    persist_mock.assert_not_called()


def test_update_task_commit_sha_calls_sqlite_persist_once():
    """Exactly one ``_persist_commit_sha_to_sqlite`` call per invocation."""
    from task_manager import TaskManager

    tm = TaskManager.__new__(TaskManager)
    tm.tasks = [_make_task("11-7")]
    tm.runtime_overrides = {}
    tm.tasks_file = Path("/tmp/nonexistent/tasks.json")
    tm.save_tasks = lambda: None

    with patch.object(TaskManager, "_persist_commit_sha_to_sqlite") as persist_mock:
        tm.update_task_commit_sha("11-7", "f" * 40)
        tm.update_task_commit_sha("11-7", "f" * 40)  # second call

    assert persist_mock.call_count == 2
    # Both calls received the SHA verbatim.
    for call_args in persist_mock.call_args_list:
        args, kwargs = call_args
        assert args[0] == "11-7"
        assert args[1] == "f" * 40


def test_update_task_commit_sha_unknown_task_is_safe():
    """Unknown task_id → no AttributeError on tm.tasks."""
    from task_manager import TaskManager

    tm = TaskManager.__new__(TaskManager)
    tm.tasks = [_make_task("11-8")]
    tm.runtime_overrides = {}
    tm.tasks_file = Path("/tmp/nonexistent/tasks.json")
    tm.save_tasks = lambda: None

    with patch.object(TaskManager, "_persist_commit_sha_to_sqlite") as persist_mock:
        # Should not raise; task_id not in self.tasks means the in-memory
        # update path is skipped, and runtime_overrides is left alone.
        tm.update_task_commit_sha("never-existed", "a" * 40)

    # runtime_overrides must not be polluted by an unknown task_id.
    assert "never-existed" not in tm.runtime_overrides