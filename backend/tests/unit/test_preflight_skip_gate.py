"""Tests for the test_command pre-flight skip gate.

Background (per the project's CLAUDE.md, "Task completion dual-criterion
rule" 2026-08-24):

  For every task the executor is about to dispatch, run the task's
  declared ``test_command`` against the current project_dir BEFORE
  invoking the subagent. If the command exits 0, the work the task
  was supposed to do is already in place in a passing state, so the
  subagent round-trip is redundant — mark the task completed and
  move on. If the command exits non-zero, fall through to the normal
  subagent path so the implementation work is actually done. If the
  check is undeliverable (no test_command, no project_dir, OSError,
  timeout) the caller falls through to the normal path; an
  undeliverable pre-flight is never a reason to mark a task done.

Audit-style tasks (declared ``verification_only=True`` OR heuristically
detected by ``_looks_like_audit_task``) skip the pre-flight check
entirely — their declared deliverable is a written answer /
line-number finding, not a code diff, and running their shell probes
on a clean checkout is a no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ``tools/`` is on sys.path via pytest.ini's ``pythonpath``, so
# ``from agent import AutonomousAgent`` resolves to ``backend/agent.py``.
from agent import AutonomousAgent


def _make_agent(project_dir: Path) -> AutonomousAgent:
    """Build a minimal AutonomousAgent stub with only the fields
    ``_preflight_test_command_skip`` and ``_execute_task_with_retry``
    touch."""
    config = MagicMock()
    config.executor_system_prompt = ""
    coding_tool = MagicMock()
    retry_manager = MagicMock()
    task_manager = MagicMock()
    task_manager.update_task_status = MagicMock()

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = project_dir
    agent.config = config
    agent.coding_tool = coding_tool
    agent.retry_manager = retry_manager
    agent.task_manager = task_manager
    agent.logger = None
    # ``_execute_task_with_retry`` reads ``self.executor`` to check
    # previous-timeout state; stub it.
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)
    return agent


def _make_task(task_id: str = "t1", test_command: str = "", verification_only: bool = False, project_dir: Path | None = None) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.test_command = test_command
    t.verification_only = verification_only
    t.project_dir = str(project_dir) if project_dir else None
    t.title = f"task {task_id}"
    t.description = ""
    return t


class TestPreflightSkipGate(unittest.TestCase):
    def test_empty_test_command_returns_none(self):
        """No test_command → undeliverable pre-flight, fall through."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(test_command="")
            self.assertIsNone(agent._preflight_test_command_skip(task))

    def test_missing_project_dir_returns_none(self):
        """No project_dir (and no agent.project_dir fallback) → fall through."""
        agent = _make_agent(Path("/nonexistent/does/not/exist"))
        task = _make_task(test_command="true")
        self.assertIsNone(agent._preflight_test_command_skip(task))

    def test_audit_task_returns_none_even_with_passing_test(self):
        """Audit-style task: pre-flight MUST return None even if the
        test would have passed — running shell probes on a clean tree
        would falsely mark an audit as done without the audit happening."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(
                test_command="echo audit-passed && exit 0",
                # Make _looks_like_audit_task classify this as audit
                # via the title (which matches a real audit-task keyword).
            )
            task.title = "路径审计：定位 src/ 与 tests/ 目录"
            self.assertIsNone(agent._preflight_test_command_skip(task))

    def test_verification_only_task_returns_none(self):
        """verification_only=True → fall through regardless of test_command."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(
                test_command="true",
                verification_only=True,
            )
            self.assertIsNone(agent._preflight_test_command_skip(task))

    def test_passing_test_command_returns_true(self):
        """Real test_command that exits 0 on a real (empty) project →
        True (skip the subagent)."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(test_command="true")  # POSIX true = exit 0
            self.assertTrue(agent._preflight_test_command_skip(task))

    def test_failing_test_command_returns_false(self):
        """Real test_command that exits non-zero → False (don't skip,
        the subagent must do the work)."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(test_command="false")  # POSIX false = exit 1
            self.assertFalse(agent._preflight_test_command_skip(task))

    def test_misleading_echo_suffix_is_stripped(self):
        """``; echo TEST_RESULT: PASSED`` suffix is stripped by
        ``_clean_test_command`` so a subagent would still need to run
        the real tests. A passing ``echo "TEST_RESULT: PASSED"`` alone
        must NOT count as a passing pre-flight — we strip the suffix
        first, which then leaves ``true`` (still exit 0) or ``false``
        (exit 1) to decide."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            # If the suffix were NOT stripped the command would still
            # exit 0 (echo is the last step); we want the suffix
            # stripped so the underlying exit code reflects reality.
            task = _make_task(test_command="false; echo TEST_RESULT: PASSED")
            self.assertFalse(agent._preflight_test_command_skip(task))

    def test_test_command_with_real_artifact(self):
        """A task whose test_command writes a real file and checks
        it → True when the artifact already exists (legit skip)."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            # Create a sentinel file the test will check for
            sentinel = Path(tmp) / "deliverable.txt"
            sentinel.write_text("done")
            # test that exits 0 iff the sentinel exists
            cmd = f"test -f {sentinel!s}"
            task = _make_task(test_command=cmd)
            self.assertTrue(agent._preflight_test_command_skip(task))
            sentinel.unlink()
            self.assertFalse(agent._preflight_test_command_skip(task))


class TestPreflightGateIntegration(unittest.TestCase):
    """Smoke test that the pre-flight gate integrates with
    ``_execute_task_with_retry``: when the gate says "skip", the
    subagent is NOT invoked and the task is marked completed."""

    def test_passing_preflight_short_circuits(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(test_command="true")

            # Stub coding_tool.query — if it gets called, the test
            # fails because the pre-flight gate should have stopped
            # us before reaching the subagent.
            agent.coding_tool.query = MagicMock(
                side_effect=AssertionError(
                    "subagent was invoked despite passing pre-flight"
                )
            )

            result = agent._execute_task_with_retry(task, max_retries=2)

            self.assertTrue(result)
            # task_manager.update_task_status was called at least
            # once with "completed" (the post-skip marker).
            completed_calls = [
                c
                for c in agent.task_manager.update_task_status.call_args_list
                if c.args and c.args[1] == "completed"
            ]
            self.assertTrue(
                len(completed_calls) >= 1,
                f"expected at least one completed status update, got "
                f"{agent.task_manager.update_task_status.call_args_list}",
            )
            # And the subagent was NOT called.
            agent.coding_tool.query.assert_not_called()

    def test_failing_preflight_falls_through(self):
        """Failing test_command → subagent path runs normally."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(Path(tmp))
            task = _make_task(test_command="false")

            # The subagent path tries to call coding_tool.query which
            # we stub to immediately fail. We just need to verify the
            # pre-flight didn't short-circuit us — i.e. coding_tool
            # WAS called.
            agent.coding_tool.query = MagicMock(
                return_value="no real test result"
            )

            # The agent.run() loop catches all exceptions and marks
            # failure; we expect _execute_task_with_retry to return
            # False (failed) because the AI didn't report a
            # TEST_RESULT line.
            result = agent._execute_task_with_retry(task, max_retries=1)

            # Subagent WAS called (pre-flight did not skip).
            agent.coding_tool.query.assert_called_once()


if __name__ == "__main__":
    unittest.main()