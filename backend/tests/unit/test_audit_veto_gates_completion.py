"""The audit second pass must be able to FAIL a task.

2026-09-20 (post-mortem)
-----------------------------

``_audit_task_second_pass`` is the *second signal* for every task that has
no runnable ``test_command``. Repair tasks are exactly that population —
``_cross_verify_test_result`` can only echo the subagent's own claim when
there is no command to re-run (see ``test_cross_verify_unverified``), so
the Claude ``-p`` adversarial review is the only independent judgment
standing between a plausible-sounding answer and a ``completed`` verdict.

The call site made that veto a **dead store**: it sat *inside* the
``if tests_passed:`` block, with ``tests_passed = False`` on the next
line. By the time the assignment ran, the branch had already been taken,
so it could not reach the ``else`` that retries — and the empty-output
gate in between never reads ``tests_passed`` at all (it reads
``changed_files`` and ``is_audit`` only).

Observed on a repair round::

    WARN task_audit_second_pass_failed
             {'reason': 'The answer only references a prior commit and
              asserts "EXIT_CODE=0" without showing any pytest output...'}
    INFO task_completed
             Task [repair-r1-02] completed successfully

The rejection was recorded and discarded, all six repair tasks were
marked completed with an empty git diff, and the next round re-verified
to the identical failure set.

Why the existing tests did not catch it
---------------------------------------

``test_audit_second_pass.py::TestAuditIntegrationWithExecute`` and both
tests in ``test_empty_diff_hard_fail.py`` **re-implement the branch they
claim to test** — they copy the ``if not audit_passed: tests_passed =
False`` snippet or the ``record_attempt`` call into the test body and
assert on that. A copy of a statement proves the statement works; it can
never prove the statement is *reachable*. This module drives the real
``_execute_task_with_retry`` instead, which is the only way a
control-flow defect of this shape can be observed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))


def _make_task(*, verification_only: bool = True, test_command: str = ""):
    from task import SubTask

    return SubTask(
        id="repair-r1-02",
        title="修复 VP-004：让 9/2 13:41 盘整信号成为第 3 个信号",
        description="【失败证据】GET /api/items ... 只返回 2 个信号。",
        test_command=test_command,
        files_to_modify=[],
        verification_only=verification_only,
    )


def _make_agent(*, audit_passed: bool, prior_deliverable: bool):
    """An ``AutonomousAgent`` skeleton wired so ``_execute_task_with_retry``
    reaches the completion decision with everything around it neutralised.

    Only the two gates under test are left real: the audit second pass
    and the branch that decides completed-vs-retry.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp")
    agent.subagent_cfg = None
    agent.logger = MagicMock()
    agent.config = MagicMock()
    agent.config.executor_system_prompt = "fake prompt"
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout.return_value = False
    agent.retry_manager = MagicMock()
    agent.retry_manager.get_retry_prompt_modifier.return_value = ""
    agent.task_manager = MagicMock()
    agent.rollback_manager = MagicMock()
    agent._session_task_completed_counts = {}

    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files.return_value = []
    agent.git_manager.get_diff.return_value = ""

    agent.coding_tool = MagicMock()
    agent.coding_tool.query.return_value = (
        "Inspected the workspace; the prior round already applied this.\n"
        "TEST_RESULT: PASSED"
    )

    # Everything around the two gates under test is neutralised so a
    # failure can only come from the audit veto being ignored.
    agent._preflight_test_command_skip = MagicMock(return_value=False)
    agent._parse_test_result = MagicMock(return_value=(True, ""))
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))
    agent._task_declared_files_exist = MagicMock(return_value=prior_deliverable)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._refine_after_failure = MagicMock()
    agent._persist_task_status = MagicMock()
    agent._commit_task_changes = MagicMock()
    agent._audit_task_second_pass = MagicMock(
        return_value=(audit_passed, "" if audit_passed else "audit_second_pass_failed: nope")
    )
    return agent


def test_audit_rejection_fails_the_task():
    """A rejected audit must produce a FAILED task, not ``completed``.

    Pre-fix this returned True and called ``update_task_status(...,
    "completed")`` — the downgrade was unreachable from inside the
    ``if tests_passed:`` block.
    """
    agent = _make_agent(audit_passed=False, prior_deliverable=True)
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is False, (
        "the audit second pass rejected the answer but the task still "
        "reported success — the veto is not reaching the completion decision"
    )
    assert not any(
        call.args[:2] == (task.id, "completed")
        for call in agent.task_manager.update_task_status.call_args_list
    ), "a task the second pass rejected must never be marked completed"
    assert agent._commit_task_changes.call_count == 0, (
        "no git checkpoint may be written for a rejected task"
    )


def test_audit_pass_still_completes():
    """The veto must not become a blanket failure — a passing audit still completes."""
    agent = _make_agent(audit_passed=True, prior_deliverable=True)
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True
    assert (task.id, "completed") in [
        call.args[:2] for call in agent.task_manager.update_task_status.call_args_list
    ]


def test_audit_veto_is_not_swallowed_by_the_empty_output_gate():
    """The exact the observed shape: rejected audit + empty diff on a repair task.

    With ``prior_deliverable=False`` the empty-output gate also runs. It
    reads ``changed_files`` / ``is_audit`` and never ``tests_passed``, so
    before the fix it waved the task through to ``completed``.
    """
    agent = _make_agent(audit_passed=False, prior_deliverable=False)
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is False
    assert agent._commit_task_changes.call_count == 0


def _executor_prompts(agent) -> list[str]:
    """Every prompt the executor sent on the ``execution`` scene."""
    return [
        call.args[0]
        for call in agent.coding_tool.query.call_args_list
        if call.kwargs.get("scene") == "execution"
    ]


def test_commandless_task_is_told_to_self_verify():
    """A task with no ``test_command`` must be told to verify itself.

    2026-09-20 (post-mortem). ``test_instruction`` interpolated
    ``clean_test_cmd`` unconditionally, so a commandless task — which
    is every repair task since the 2026-09-07 content-only refactor —
    got "YOU MUST run the test command yourself:  " with an empty
    slot. There is nothing to run, so the subagent could only satisfy
    the instruction by inventing a result. It did: answers
    along the lines of "the prior round already applied this,
    EXIT_CODE=0".

    The replacement names the situation instead of pretending a
    command exists, and asks for the one thing the adversarial second
    pass can actually falsify: pasted terminal output.
    """
    agent = _make_agent(audit_passed=True, prior_deliverable=True)
    task = _make_task(test_command="")

    agent._execute_task_with_retry(task, max_retries=1)

    prompts = _executor_prompts(agent)
    assert prompts, "the executor never dispatched a subagent"
    prompt = prompts[0]
    self_check = "This task has NO test_command"
    assert self_check in prompt, (
        "the self-verification contract is missing — a commandless "
        "task would still see the empty-slot 'run the test command "
        "yourself:  ' instruction that the run left the subagent to invent"
    )
    assert "Paste the exact command you ran" in prompt
    # The two rejection patterns that map 1:1 onto that run subagent
    # answers.
    assert "already applied in a previous round" in prompt
    assert "`EXIT_CODE=0` alone proves nothing" in prompt
    # And the empty-slot instruction must not survive alongside it.
    assert "YOU MUST run the test command yourself" not in prompt


def test_a_task_with_a_command_still_gets_the_command_instruction():
    """The self-verification branch must not swallow real commands."""
    agent = _make_agent(audit_passed=True, prior_deliverable=True)
    task = _make_task(test_command="pytest tests/test_vp004.py -q")

    agent._execute_task_with_retry(task, max_retries=1)

    prompt = _executor_prompts(agent)[0]
    assert "YOU MUST run the test command yourself" in prompt
    assert "pytest tests/test_vp004.py -q" in prompt
    assert "This task has NO test_command" not in prompt
