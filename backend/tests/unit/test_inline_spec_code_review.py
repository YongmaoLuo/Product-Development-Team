"""
TDD tests for the inline spec/code review inserted into
``AutonomousAgent._execute_task_with_retry`` (DP3).

Background
----------
DP3 requires that, after the cross-verify layer confirms the test
command exited 0 but BEFORE the git checkpoint commit, the agent
performs a lightweight adversarial self-review:

  * Pass ``task.description`` + ``git diff --stat`` to an LLM.
  * The LLM grades the implementation along two dimensions —
    spec compliance and code quality — and emits ``should_block``.
  * If the LLM flags either dimension as ``high`` severity, the
    task is reverted to ``pending``, ``failure_reason`` is written,
    and the commit is skipped.
  * If the LLM call fails (network, API error, JSON parse), the
    agent logs a warning and proceeds with the commit — the review
    is a safety net, not a hard gate.
  * If the git diff is empty, the review is skipped entirely.

These tests pin the integration contract between
``_execute_task_with_retry`` and ``_inline_spec_code_review`` /
``_get_git_diff_stat_for_review`` — the actual LLM prompt is
exercised by ``test_agent_refine_timing.py`` and the unit tests
on ``_inline_spec_code_review`` itself. We mock the seams the
caller exposes so the test does not depend on a real LLM round
trip.

TDD spec — 5 contract tests
---------------------------
1. ``test_execute_review_blocks_commit_on_high_spec``:
   ``_inline_spec_code_review`` returns ``spec_compliance="high"``,
   ``should_block=True``. The commit must NOT be invoked; the task
   must be reverted to ``pending``; ``failure_reason`` must be
   recorded.

2. ``test_execute_review_blocks_commit_on_high_quality``:
   ``_inline_spec_code_review`` returns ``code_quality="high"``,
   ``should_block=True``. Same expectations as #1.

3. ``test_execute_review_allows_commit_on_low``:
   ``_inline_spec_code_review`` returns ``spec_compliance="low"``,
   ``code_quality="low"``, ``should_block=False``. The commit MUST
   be invoked; the task status moves to ``completed``.

4. ``test_execute_review_llm_failure_continues_commit``:
   ``_inline_spec_code_review`` raises. The outer try/except in
   ``_execute_task_with_retry`` catches it, logs a warning, and
   proceeds with the commit.

5. ``test_execute_review_skipped_when_diff_empty``:
   ``_get_git_diff_stat_for_review`` returns ``""``. The inline
   review is NOT invoked at all; the commit proceeds normally.
"""

import sys
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _build_mock_agent(max_retries: int = 1):
    """Construct a bare ``AutonomousAgent`` with all external deps mocked.

    Mirrors the harness in ``test_agent_refine_timing.py``: we bypass
    ``__init__`` because the real constructor instantiates TaskManager
    / GitManager / BackgroundManager / RetryManager / RollbackManager,
    none of which we need for driving the post-test pre-commit
    inline-review branch deterministically.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp/_inline_review_dummy")
    agent.logger = None

    agent.retry_manager = __import__("retry_manager").RetryManager()

    # coding_tool.query default — never reached on the happy path
    # because ``_inline_spec_code_review`` is mocked at the agent
    # boundary. The default is still useful so a stray call surfaces
    # clearly in test output.
    cheat_response = "Some impl work.\n\nTEST_RESULT: PASSED\nREASON: ok\n"
    agent.coding_tool = MagicMock()
    agent.coding_tool.query = MagicMock(return_value=cheat_response)

    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)

    agent.task_manager = MagicMock()
    agent.task_manager.update_task_status = MagicMock()
    agent.task_manager.record_task_failure = MagicMock()

    agent.config = MagicMock()
    agent.config.executor_system_prompt = ""
    agent.config.max_retries = max_retries

    agent.subagent_cfg = None

    agent.parse_files_from_response = MagicMock(return_value={})

    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files = MagicMock(return_value=[])

    # commit: spy — the tests assert against call_count
    agent._commit_task_changes = MagicMock()

    # Empty-output gate: tests pin the inline-review contract and must
    # not be blocked by the earlier plan's 2026-08-19 gate. Mark every
    # task's declared deliverable as already on disk so an empty diff
    # still passes the gate (the inline-review branch is exercised
    # independently via the explicit ``changed_files`` mocks below).
    agent._task_declared_files_exist = MagicMock(return_value=True)

    agent._clean_test_command = lambda cmd: cmd

    # cross_verify: default to "tests passed" so we reach the review
    # branch — this is the precondition for DP3.
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))

    # diff-stat helper — tests override as needed.
    agent._get_git_diff_stat_for_review = MagicMock(return_value="src/x.py | 5 +++")

    # inline review — tests override as needed.
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "low",
            "code_quality": "low",
            "should_block": False,
            "reason": "",
        }
    )

    # Session counter required by the post-commit bookkeeping
    # (``_session_task_completed_counts``).
    agent._session_task_completed_counts = {}

    return agent


def _build_task(task_id: str = "1"):
    from task import SubTask

    return SubTask(
        id=task_id,
        title="Test task",
        description="Implement X with password hashing",
        test_command="echo test",
        status="pending",
    )


# ---------------------------------------------------------------------------
# Test 1: high spec deviation -> block commit
# ---------------------------------------------------------------------------


def test_execute_review_blocks_commit_on_high_spec():
    """LLM flags ``spec_compliance='high'`` with ``should_block=True``.

    The commit MUST NOT be invoked. The task must be reverted to
    ``pending`` and ``failure_reason`` recorded so the next run
    picks it up.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "high",
            "code_quality": "low",
            "should_block": True,
            "reason": "spec deviation: missing password hashing",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    # Task ultimately fails on this run
    assert result is False

    # HEADLINE: commit was NOT called — bad code stays out of git
    assert agent._commit_task_changes.call_count == 0, (
        f"Expected _commit_task_changes NOT to be called when "
        f"spec_compliance='high', but it was called "
        f"{agent._commit_task_changes.call_count} time(s)."
    )

    # Task reverted to pending so the next run re-picks it
    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "pending" in statuses, (
        f"Expected task to be reverted to 'pending', got statuses: {statuses}"
    )

    # failure_reason captured
    assert agent.task_manager.record_task_failure.call_count == 1
    failure_args = agent.task_manager.record_task_failure.call_args
    failure_reason = failure_args.args[1] if len(failure_args.args) > 1 else failure_args.kwargs.get("error_msg", "")
    assert "spec deviation" in failure_reason or "password" in failure_reason, (
        f"Expected failure_reason to mention spec deviation, got: {failure_reason!r}"
    )

    # Inline review WAS invoked
    assert agent._inline_spec_code_review.call_count == 1


# ---------------------------------------------------------------------------
# Test 2: high quality issue -> block commit
# ---------------------------------------------------------------------------


def test_execute_review_blocks_commit_on_high_quality():
    """LLM flags ``code_quality='high'`` with ``should_block=True``.

    Same expectations as the high-spec case: no commit, task
    reverted to pending, failure_reason recorded.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "low",
            "code_quality": "high",
            "should_block": True,
            "reason": "quality: plaintext password in code",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    assert result is False

    assert agent._commit_task_changes.call_count == 0, (
        f"Expected _commit_task_changes NOT to be called when "
        f"code_quality='high', but it was called "
        f"{agent._commit_task_changes.call_count} time(s)."
    )

    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "pending" in statuses, (
        f"Expected task to be reverted to 'pending', got statuses: {statuses}"
    )

    assert agent.task_manager.record_task_failure.call_count == 1
    assert agent._inline_spec_code_review.call_count == 1


# ---------------------------------------------------------------------------
# Test 3: low severity on both dimensions -> commit proceeds
# ---------------------------------------------------------------------------


def test_execute_review_allows_commit_on_low():
    """LLM returns low severity on both spec and code quality.

    ``should_block=False`` -> the commit MUST proceed; the task
    status moves to ``completed``.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "low",
            "code_quality": "low",
            "should_block": False,
            "reason": "minor suggestions only",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    assert result is True

    # HEADLINE: commit WAS called
    assert agent._commit_task_changes.call_count == 1, (
        f"Expected _commit_task_changes to be called exactly once when "
        f"review allows, got {agent._commit_task_changes.call_count}."
    )

    # Task marked completed
    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "completed" in statuses, (
        f"Expected task to be marked 'completed', got statuses: {statuses}"
    )

    # No failure recorded on the happy path
    assert agent.task_manager.record_task_failure.call_count == 0

    assert agent._inline_spec_code_review.call_count == 1


# ---------------------------------------------------------------------------
# Test 4: LLM raises -> log warning, continue commit
# ---------------------------------------------------------------------------


def test_execute_review_llm_failure_continues_commit():
    """``_inline_spec_code_review`` raises an exception.

    The outer try/except in ``_execute_task_with_retry`` MUST
    catch it, log a warning, and proceed with the commit — the
    review is a safety net, not a hard gate.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        side_effect=RuntimeError("simulated LLM outage")
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    # Despite the review failure, the task still completes
    assert result is True, (
        "Inline review exception must NOT block commit. The review is "
        "best-effort; outages degrade gracefully."
    )

    # HEADLINE: commit was still called
    assert agent._commit_task_changes.call_count == 1, (
        f"Expected _commit_task_changes to be called even after review "
        f"exception, got {agent._commit_task_changes.call_count}."
    )

    # Task marked completed (not reverted to pending)
    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "completed" in statuses, (
        f"Expected task to be 'completed' despite review exception, "
        f"got statuses: {statuses}"
    )

    # No failure reason recorded — review outage is not a task failure
    assert agent.task_manager.record_task_failure.call_count == 0


# ---------------------------------------------------------------------------
# Test 5: empty diff -> skip review entirely
# ---------------------------------------------------------------------------


def test_execute_review_skipped_when_diff_empty():
    """``_get_git_diff_stat_for_review`` returns ``""``.

    The inline review MUST be skipped entirely — calling an LLM
    with no diff context is wasteful (and would grade on spec
    alone, which is what the test layer already proved). The
    commit proceeds directly.
    """
    agent = _build_mock_agent()
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    # If the review WERE invoked, this would block — proving the
    # skip path.
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "high",
            "code_quality": "high",
            "should_block": True,
            "reason": "should never reach here",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    assert result is True

    # HEADLINE: inline review was NOT called
    assert agent._inline_spec_code_review.call_count == 0, (
        "Expected _inline_spec_code_review to be SKIPPED when the git "
        "diff is empty, but it was called "
        f"{agent._inline_spec_code_review.call_count} time(s)."
    )

    # Commit proceeds normally
    assert agent._commit_task_changes.call_count == 1

    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "completed" in statuses
