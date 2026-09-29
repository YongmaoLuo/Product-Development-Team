"""
TDD tests for ``AutonomousAgent._execute_task_with_retry`` — refine timing.

Background
----------
Before this fix, every test failure immediately called
``self._refine_after_failure(...)`` and then retried. With
``max_retries=2`` and a flaky/cheating subagent, the loop became:

  attempt 0: subagent claims PASSED, pytest actually fails
            -> refine (split into N children)
            -> retry
  attempt 1: refined child fails (cheat again or genuine)
            -> refine again (more children)
            -> exhausted -> record failure

Net effect: the refiner was invoked on EVERY failure, so a single
hard task could balloon the task list from 15 -> 35 in plan
20260615-refactor-provider-config. The cross-verify layer already
records an explicit "Subagent claimed PASSED but pytest exit N" reason
in ``error_msg``; ``get_retry_prompt_modifier`` includes that in the
next attempt's prompt so subagent can self-correct. So the correct
flow is:

  attempt 0: cheat detected -> retry with explicit failure feedback
  attempt 1: still failing  -> NOW refine (split the task)

TDD spec — 3 contract tests
---------------------------
1. ``test_refine_only_after_retries_exhausted_cheat_path``:
   Subagent cheats on BOTH attempts. ``_refine_after_failure`` is
   called exactly ONCE (after the retry budget is gone), not twice.
   This is the headline regression test.

2. ``test_refine_first_attempt_passes_no_refine``:
   Subagent passes on the first attempt. No refine, no retry.

3. ``test_refine_first_fails_second_passes_no_refine``:
   Subagent cheats on attempt 0, passes on attempt 1 (after seeing
   the failure feedback in the retry context). No refine — the retry
   self-correction was enough.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _build_mock_agent(max_retries: int = 2):
    """Construct a bare ``AutonomousAgent`` with all external deps mocked.

    We bypass ``__init__`` because the real constructor instantiates
    ``TaskManager`` / ``GitManager`` / ``BackgroundManager`` /
    ``RetryManager`` / ``RollbackManager`` — none of which we need to
    drive ``_execute_task_with_retry`` deterministically. We only
    set the attributes the method actually touches.

    Returns the agent plus the list of recorded ``_refine_after_failure``
    calls so each test can assert against the call log.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp/_agent_refine_timing_dummy")
    agent.logger = None

    agent.retry_manager = __import__("retry_manager").RetryManager()

    # coding_tool.query returns a response that _parses_ as TEST_RESULT: PASSED,
    # so the cross-verify layer (mocked below) gets to overrule it. We do
    # NOT want the real _cross_verify_test_result to invoke subprocess.
    cheat_response = "Some impl work.\n\nTEST_RESULT: PASSED\nREASON: looks good\n"
    agent.coding_tool = MagicMock()
    agent.coding_tool.query = MagicMock(return_value=cheat_response)

    # executor: had_previous_timeout always False → no background mode
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)

    # task_manager: noop — we only care about refine timing, not persistence
    agent.task_manager = MagicMock()
    agent.task_manager.update_task_status = MagicMock()
    agent.task_manager.record_task_failure = MagicMock()

    # config: minimum needed by _execute_task_with_retry
    agent.config = MagicMock()
    agent.config.executor_system_prompt = ""
    agent.config.max_retries = max_retries

    # subagent_cfg: None skips write_tmp_settings branch entirely
    agent.subagent_cfg = None

    # parse_files_from_response: empty dict → no real file writes
    agent.parse_files_from_response = MagicMock(return_value={})

    # git: no changes
    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files = MagicMock(return_value=[])

    # commit: noop
    agent._commit_task_changes = MagicMock()

    # Empty-output gate: by default the test pretends every declared
    # deliverable already exists on disk so the task is not blocked
    # by the earlier plan's 2026-08-19 audit gate. Tests that want
    # to exercise the gate path override this with a MagicMock.
    if "_task_declared_files_exist_mock" not in _build_mock_agent.__dict__:
        _build_mock_agent._task_declared_files_exist_mock = True
    agent._task_declared_files_exist = MagicMock(return_value=True)

    # Inline review (DP3): empty-diff path skips the review entirely
    # so the tests can assert on commit / refine without having to
    # mock the review return value. The actual review contract is
    # pinned in test_inline_spec_code_review.py.
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")

    # _clean_test_command: pass through (avoid re-implementing the regex)
    agent._clean_test_command = lambda cmd: cmd

    # _cross_verify_test_result: default to "cheat detected". Tests that
    # want a different verdict override this attribute.
    agent._cross_verify_test_result = MagicMock(
        return_value=(False, "Subagent claimed PASSED but pytest exit 1. "
                            "Failed: test_x")
    )

    # _refine_after_failure: spy. Each call is recorded in refine_call_log.
    refine_call_log = []

    def _refine_spy(*args, **kwargs):
        refine_call_log.append(
            {"args": args, "kwargs": kwargs, "task_id": args[0].id}
        )

    agent._refine_after_failure = MagicMock(side_effect=_refine_spy)
    agent.refine_call_log = refine_call_log

    # Session-local counter for the same-id re-run loop guard
    # (see ``agent._run_async`` at line ~1951). Without this,
    # _execute_task_with_retry's loop break path raises AttributeError.
    agent._session_task_completed_counts = {}

    return agent


def _build_task(task_id: str = "1"):
    from task import SubTask

    return SubTask(
        id=task_id,
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )


# ---------------------------------------------------------------------------
# Test 1: cheat on both attempts -> refine ONCE (the headline regression)
# ---------------------------------------------------------------------------


def test_refine_only_after_retries_exhausted_cheat_path():
    """Subagent cheats on both attempts. ``_refine_after_failure`` must be
    called exactly ONCE — after the retry budget is exhausted — NOT on
    every failure.

    Regression for: task list explosion in plan
    20260615-refactor-provider-config (15 -> 35 subtasks) caused by
    immediate refine on each failure.
    """
    agent = _build_mock_agent(max_retries=2)
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=2)

    # Task ultimately fails (cheat detected in both attempts)
    assert result is False

    # HEADLINE assertion: refine called exactly once, not twice
    assert len(agent.refine_call_log) == 1, (
        f"Expected _refine_after_failure to be called once (after retries "
        f"exhausted), got {len(agent.refine_call_log)}. This is the "
        f"regression: every-failure refine causes task list explosion. "
        f"Calls: {agent.refine_call_log}"
    )

    # Sanity: cross_verify ran once per attempt (2 attempts in max_retries=2)
    assert agent._cross_verify_test_result.call_count == 2

    # The single refine call is the LAST action before record_task_failure,
    # so the failure is properly recorded after the breakdown.
    assert agent.task_manager.record_task_failure.call_count == 1


# ---------------------------------------------------------------------------
# Test 2: first attempt passes -> no refine, no retry
# ---------------------------------------------------------------------------


def test_refine_first_attempt_passes_no_refine():
    """Subagent passes on the first attempt. No refine. No retry."""
    agent = _build_mock_agent(max_retries=2)
    # cross_verify agrees: pass
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is True
    assert len(agent.refine_call_log) == 0
    assert agent._cross_verify_test_result.call_count == 1
    # No failure recorded on the happy path
    assert agent.task_manager.record_task_failure.call_count == 0


# ---------------------------------------------------------------------------
# Test 3: first fails, retry self-corrects -> no refine
# ---------------------------------------------------------------------------


def test_refine_first_fails_second_passes_no_refine():
    """Subagent cheats on attempt 0, sees the failure feedback in the retry
    context, and passes on attempt 1. No refine — the self-correction
    through ``get_retry_prompt_modifier`` was enough.
    """
    agent = _build_mock_agent(max_retries=2)
    agent._cross_verify_test_result = MagicMock(side_effect=[
        (False, "Subagent claimed PASSED but pytest exit 1"),
        (True, ""),
    ])
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is True
    assert len(agent.refine_call_log) == 0
    assert agent._cross_verify_test_result.call_count == 2
