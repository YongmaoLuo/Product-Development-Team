"""
Tests for carrying a task's persisted ``failure_reason`` into the first
attempt of a *re-dispatched* task.

Background
--------------------------------------
A task that had already failed and got re-scheduled started its next
run with a clean prompt — no trace of why it failed. Two carriers
existed and neither covered that moment:

  * ``RetryManager`` is in-memory and consulted only when
    ``attempt > 0``, so the first attempt of a dispatch never saw it;
  * the dispatcher rebuilds micro-layers from disk, where
    ``save_tasks`` strips ``status``, so a failed task looks
    ``pending`` again and is re-scheduled.

``task.failure_reason`` is hydrated from ``plan_tasks`` at load time,
so it is the one carrier that survives. These tests pin:

  1. ``_build_prior_failure_block`` renders the reason, and renders
     nothing when there is none;
  2. the first attempt (``attempt == 0``) of a run carries it;
  3. a later in-run retry does NOT duplicate it — ``RetryManager``
     owns that slot;
  4. a task that has since succeeded must not keep a stale reason.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task import SubTask  # noqa: E402


REASON = (
    "Subagent test-report mismatch: claimed TEST_RESULT: PASSED but "
    "actual pytest exit code was 1."
)


def _make_task(**kw) -> SubTask:
    base = dict(
        id="11-5-1",
        title="example",
        description="example description",
        test_command="pytest -q",
        files_to_modify=["foo.py"],
    )
    base.update(kw)
    return SubTask(**base)


def _bare_agent() -> AutonomousAgent:
    """``AutonomousAgent`` skeleton — enough for the prompt builder."""
    agent = AutonomousAgent.__new__(AutonomousAgent)
    return agent


# ---------------------------------------------------------------------------
# 1. The renderer itself
# ---------------------------------------------------------------------------


def test_block_empty_when_no_recorded_failure():
    agent = _bare_agent()
    assert agent._build_prior_failure_block(_make_task()) == ""


def test_block_empty_for_none_and_whitespace():
    agent = _bare_agent()
    assert agent._build_prior_failure_block(
        _make_task(failure_reason=None)
    ) == ""
    assert agent._build_prior_failure_block(
        _make_task(failure_reason="   \n  ")
    ) == ""


def test_block_carries_the_reason():
    agent = _bare_agent()
    block = agent._build_prior_failure_block(_make_task(failure_reason=REASON))
    assert REASON in block
    assert "PREVIOUS ATTEMPT FAILED" in block


def test_block_tells_the_model_it_may_report_a_broken_task():
    """A task whose test command can never pass must be reportable.

    the subagent claimed ``TEST_RESULT: PASSED`` while the
    command exited 1 — the failure was structural (a probe chain that
    always exits non-zero), not a coding mistake. The block has to give
    the model a non-lying way out.
    """
    agent = _bare_agent()
    block = agent._build_prior_failure_block(_make_task(failure_reason=REASON))
    lowered = block.lower()
    assert "task itself is wrong" in lowered
    assert "do not claim success" in lowered


def test_block_truncates_an_over_long_reason():
    agent = _bare_agent()
    long_reason = "x" * (agent._PRIOR_FAILURE_MAX_CHARS + 500)
    block = agent._build_prior_failure_block(_make_task(failure_reason=long_reason))
    assert "[truncated]" in block
    assert len(block) < len(long_reason)


# ---------------------------------------------------------------------------
# 2 & 3. Where it lands in the assembled prompt
# ---------------------------------------------------------------------------


def _run_one_attempt(task, *, captured_contexts, attempts=1, retry_modifier=""):
    """Drive ``_execute_task_with_retry`` far enough to capture prompts.

    Returns ``(contexts, agent)`` — the ``context`` strings handed to
    ``coding_tool.query``, plus the mocked agent so callers can inspect
    which collaborator was asked for what. Downstream verification is
    mocked away; the subject under test is prompt assembly.
    """
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp")
    agent.config = MagicMock()
    agent.config.executor_system_prompt = "fake prompt"
    agent.logger = MagicMock()

    agent.task_manager = MagicMock()
    agent.retry_manager = MagicMock()
    agent.retry_manager.get_retry_prompt_modifier.return_value = retry_modifier
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout.return_value = False

    agent._preflight_test_command_skip = MagicMock(return_value=None)
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))
    agent._commit_task_changes = MagicMock()
    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files.return_value = ["foo.py"]

    def _query(context, **kwargs):
        captured_contexts.append(context)
        return "done\nTEST_RESULT: PASSED\n"

    agent.coding_tool = MagicMock()
    agent.coding_tool.query.side_effect = _query

    if attempts > 1:
        # Force the in-run retry branch: first attempt fails, second
        # attempt is the one we assert on.
        agent._cross_verify_test_result.side_effect = [
            (False, "boom"), (True, ""),
        ]

    AutonomousAgent._execute_task_with_retry(
        agent, task, max_retries=attempts
    )
    return captured_contexts, agent


def test_first_attempt_carries_the_previous_failure():
    captured, _ = _run_one_attempt(
        _make_task(failure_reason=REASON), captured_contexts=[]
    )
    assert len(captured) == 1
    assert REASON in captured[0], captured[0]
    assert "PREVIOUS ATTEMPT FAILED" in captured[0]


def test_first_attempt_without_a_failure_has_no_block():
    captured, _ = _run_one_attempt(_make_task(), captured_contexts=[])
    assert "PREVIOUS ATTEMPT FAILED" not in captured[0]


def test_in_run_retry_defers_to_retry_manager():
    """``attempt > 0`` is ``RetryManager``'s slot — no duplication.

    The persisted reason and the in-run modifier would otherwise say
    much the same thing twice, spending context on the repeat.
    """
    captured, _ = _run_one_attempt(
        _make_task(failure_reason=REASON),
        captured_contexts=[],
        attempts=2,
        retry_modifier="\n\n[RETRY CONTEXT - Attempt 2 of 2]\nboom\n",
    )
    assert len(captured) == 2
    # First attempt: persisted reason, no retry modifier.
    assert REASON in captured[0]
    assert "RETRY CONTEXT" not in captured[0]
    # Second attempt: retry modifier, and the persisted reason is not
    # stacked on top of it.
    assert "RETRY CONTEXT" in captured[1]
    assert "PREVIOUS ATTEMPT FAILED" not in captured[1]


def test_retry_manager_is_consulted_with_this_loops_bound():
    """C1 wiring, asserted through the real call site.

    A renderer-only test stayed green through the original bug
    (``agent.py`` calling it without a bound); this drives the actual
    call site.
    """
    captured, agent = _run_one_attempt(
        _make_task(failure_reason=REASON),
        captured_contexts=[],
        attempts=2,
    )
    agent.retry_manager.get_retry_prompt_modifier.assert_called_once_with(
        "11-5-1", 2
    )
    assert len(captured) == 2
