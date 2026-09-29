"""
Tests for ``AutonomousAgent._commit_task_changes`` SHA writeback.

Background (2026-09-11 plan): the executor was emitting commits but
never recording the SHA back into the per-task row, leaving 108/108
completed tasks with ``commit_sha IS NULL`` in state.db. This test
file pins down the new contract:

  1. ``_commit_task_changes`` calls ``git_manager.rev_parse("HEAD")``
     after the commit.
  2. ``task_manager.update_task_commit_sha`` is called with the
     returned SHA.
  3. ``rev_parse`` exceptions are caught and logged but do not crash
     the task.
  4. ``update_task_commit_sha`` exceptions are caught and logged but
     do not crash the task.
  5. An empty / falsy SHA from ``rev_parse`` skips the writeback
     (defensive: don't write empty string).

We patch ``git_manager`` and ``task_manager`` directly so the test
doesn't need a real git repo.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))


def _make_agent_skeleton():
    """Build an ``AutonomousAgent`` skeleton with the few attrs the
    new ``_commit_task_changes`` touches.

    We bypass ``__init__`` so we don't need an LLM client, plan
    dir, executor, etc. — only the attributes actually used by the
    new code path matter: ``git_manager``, ``task_manager``,
    ``logger``, ``rollback_manager``.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.git_manager = MagicMock()
    agent.task_manager = MagicMock()
    agent.logger = MagicMock()
    agent.rollback_manager = MagicMock()
    return agent


def _make_task(task_id: str = "11-4"):
    from task import SubTask
    return SubTask(
        id=task_id,
        title="example",
        description="example description",
        test_command="pytest -q",
        files_to_modify=["foo.py"],
    )


def test_commit_task_changes_calls_rev_parse_and_writes_sha():
    """The happy path: rev_parse returns a SHA → update_task_commit_sha called."""
    agent = _make_agent_skeleton()
    agent.git_manager.rev_parse.return_value = "abc123" * 4

    agent._commit_task_changes(_make_task("11-4"), ["foo.py", "bar.py"])

    agent.git_manager.commit.assert_called_once()
    agent.git_manager.rev_parse.assert_called_once_with("HEAD")
    agent.task_manager.update_task_commit_sha.assert_called_once_with(
        "11-4", "abc123" * 4,
    )


def test_commit_task_changes_rev_parse_failure_does_not_crash():
    """``rev_parse`` raises → still no crash; writeback skipped with a logger warning."""
    agent = _make_agent_skeleton()
    agent.git_manager.rev_parse.side_effect = RuntimeError("git error")

    agent._commit_task_changes(_make_task("11-5"), ["foo.py"])

    # commit still happened
    agent.git_manager.commit.assert_called_once()
    # update_task_commit_sha was NOT called because rev_parse failed
    agent.task_manager.update_task_commit_sha.assert_not_called()
    # A warning was emitted
    assert agent.logger.warning.called
    warning_args = agent.logger.warning.call_args[0]
    assert warning_args[0] == "task_commit_sha_rev_parse_failed"


def test_commit_task_changes_writeback_failure_does_not_crash():
    """``update_task_commit_sha`` raises → task still completes with a logger warning."""
    agent = _make_agent_skeleton()
    agent.git_manager.rev_parse.return_value = "f" * 40
    agent.task_manager.update_task_commit_sha.side_effect = RuntimeError(
        "sqlite locked"
    )

    agent._commit_task_changes(_make_task("11-6"), ["foo.py"])

    agent.git_manager.commit.assert_called_once()
    agent.task_manager.update_task_commit_sha.assert_called_once()
    # Method returned normally despite the writeback failure.
    # (No assert on return value — the method doesn't return anything
    # meaningful; just that no exception escaped.)
    assert agent.logger.warning.called
    warning_args = agent.logger.warning.call_args[0]
    assert warning_args[0] == "task_commit_sha_writeback_failed"


def test_commit_task_changes_empty_sha_skips_writeback():
    """rev_parse returns falsy → writeback skipped (don't write garbage)."""
    agent = _make_agent_skeleton()
    agent.git_manager.rev_parse.return_value = ""

    agent._commit_task_changes(_make_task("11-7"), ["foo.py"])

    agent.git_manager.commit.assert_called_once()
    agent.task_manager.update_task_commit_sha.assert_not_called()


def test_commit_task_changes_no_changed_files_still_writes_sha():
    """Empty ``changed_files`` does not block the SHA writeback.

    Edge case the original diagnosis flagged: deferred tasks
    get empty commits (no files changed) but the executor
    still called ``_commit_task_changes`` → must still record the
    SHA so an empty executor commit doesn't get hidden.
    """
    agent = _make_agent_skeleton()
    agent.git_manager.rev_parse.return_value = "9" * 40

    agent._commit_task_changes(_make_task("11-8"), [])

    agent.git_manager.commit.assert_called_once()
    agent.task_manager.update_task_commit_sha.assert_called_once_with(
        "11-8", "9" * 40,
    )