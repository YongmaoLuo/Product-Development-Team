"""Contract: a verifier timeout fails the task — it does not run it again
and it does not split it.

The incident this pins
======================

The cross-verification layer classifies a verifier timeout as its own
kind, ``verifier_timeout``, with the remedy
``reverify_with_more_budget``: the verification did not finish inside
its budget, so there is NO evidence about what the subagent did —
neither that it worked nor that it lied.

The executor did not act on that remedy. It classified the failure and
then carried on with the generic tail: the first occurrence of a
failure bought the task ANOTHER RUN, and a repeat reached the refiner,
which split the task into children. Both moves are inferences the
evidence does not support:

  * running the task again answers a question nobody asked. The code
    was never disproven; only the measurement was missing. And a
    second run spends the second occurrence, which is the very thing
    the split gate reads as "the task is too big for one subagent";
  * splitting a task whose verification never produced a verdict
    reproduces the same non-answer in the children, one level deeper.

So a budget that was too small got promoted into a task that was too
big — twice over.

What these tests pin
====================

1. The disposition of a failure is derived from its REMEDY:
   ``verifier_timeout`` is ``fail_only`` — whatever the occurrence
   count says.
2. Every other kind keeps its disposition: a first occurrence is
   ``extra_attempt``, a repeat is ``refine`` (and ``report_format``
   remains split-eligible, because it is the one kind whose repeat
   really does mean "too big").
3. The executor runs the task ONCE on a verifier timeout, never
   reaches the refiner, and says so in the log.

The builder below is a copy of ``tests/unit/test_refiner_split_gate.py``'s
``_build_mock_agent`` on purpose: the two contracts are separate, and a
shared fixture would couple them.
"""

import sys
from pathlib import Path
from tempfile import gettempdir
from unittest.mock import MagicMock


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The executor's own spelling for a verification that ran out of budget.
#: Taken from the producer (``_cross_verify_test_result``), not invented,
#: so a rename on the producer side fails here rather than quietly
#: re-routing the disposition.
VERIFIER_TIMEOUT_REASON = (
    "verifier_timeout: 验证命令在 600s 内未完成，不能据此判定 subagent 说谎"
)

#: A rejected report — the shape of the answer was refused.
REPORT_REJECTION = (
    "audit_second_pass_failed: evidence table truncated at row 40 of 118"
)

#: Tests genuinely ran and genuinely failed.
REAL_TEST_FAILURE = "AssertionError: expected 3, got 2"


def _build_mock_agent(max_retries: int = 2):
    """A bare ``AutonomousAgent`` with every external dep mocked.

    Mirrors ``tests/unit/test_refiner_split_gate.py``'s builder. Three
    attributes are load-bearing rather than decorative:

      * ``_failure_occurrences`` — the occurrence table the gate counts on.
      * ``_refinement_count``    — read by the real ``_refine_after_failure``
        before it will split.
      * ``refiner.refine``       — the observation point: it returns the
        caller's own task list, so the real ``_refine_after_failure``
        takes its "refiner returned no changes" branch and these tests
        stay pointed at the DISPOSITION rather than at the
        split-application machinery downstream of it.
    """
    from agent import AutonomousAgent
    from retry_manager import RetryManager
    from task import SubTask

    agent = AutonomousAgent.__new__(AutonomousAgent)
    # A directory that does not exist: the pre-flight test_command check
    # falls through on a missing project dir, so the subagent path runs.
    agent.project_dir = Path(gettempdir()) / "_verifier_timeout_dummy"
    agent.plan_id = "test-plan"
    agent.logger = None

    agent.retry_manager = RetryManager()
    agent.config = MagicMock()
    agent.config.executor_system_prompt = ""
    agent.config.max_retries = max_retries

    cheat_response = "Some impl work.\n\nTEST_RESULT: PASSED\nREASON: looks good\n"
    agent.coding_tool = MagicMock()
    agent.coding_tool.query = MagicMock(return_value=cheat_response)

    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)

    task_manager = MagicMock()
    task_manager.requirement = "Build something small."
    agent.task_manager = task_manager

    agent.subagent_cfg = None
    agent.parse_files_from_response = MagicMock(return_value={})
    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files = MagicMock(return_value=[])
    agent._commit_task_changes = MagicMock()
    agent._task_declared_files_exist = MagicMock(return_value=True)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._clean_test_command = lambda cmd: cmd
    agent._session_task_completed_counts = {}

    # The gate's own state, mirrored from ``__init__``.
    agent._failure_occurrences = {}
    agent._refinement_count = 0

    task = SubTask(
        id="1",
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )
    agent.refiner = MagicMock()
    agent.refiner.refine = MagicMock(
        side_effect=lambda **kwargs: kwargs["tasks"],
    )
    agent._persist_refiner_output = MagicMock()
    task_manager.tasks = [task]

    return agent, task


def _logged_events(logger) -> list[str]:
    """Every event name the logger received, whatever its level.

    The level is not the contract; the event NAME is. A task that
    failed on a verifier timeout has to be findable in the log, and an
    operator greps for the name.
    """
    events: list[str] = []
    for level in ("info", "warning", "error"):
        for call in getattr(logger, level).call_args_list:
            if call.args:
                events.append(call.args[0])
    return events


def _run_executor(agent, cross_verify_result, max_retries: int = 2):
    """Drive the production retry loop once and return the outcome."""
    from task import SubTask

    agent._cross_verify_test_result = MagicMock(
        side_effect=lambda **kwargs: cross_verify_result,
    )
    task = SubTask(
        id="1",
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )
    return agent._execute_task_with_retry(task, max_retries=max_retries)


# ---------------------------------------------------------------------------
# 1. The remedy decides the disposition
# ---------------------------------------------------------------------------


def test_verifier_timeout_is_fail_only():
    """The headline behaviour.

    A verifier timeout produced no verdict, so the executor must not
    buy the task another run and must not hand it to the refiner: it
    fails the task, and the task's code is left unproven.
    """
    from failure_classification import classify_failure

    agent, _ = _build_mock_agent()
    failure = classify_failure(VERIFIER_TIMEOUT_REASON)

    assert agent._failure_disposition(failure, 1, False) == "fail_only"


def test_verifier_timeout_stays_fail_only_on_a_repeat():
    """The occurrence count must not launder a timeout into a split.

    ``occurrence == 2`` is what the split gate reads as "this task is
    too big for one subagent". A timeout that happened twice is still
    two missing measurements, not two pieces of evidence about the
    code.
    """
    from failure_classification import classify_failure

    agent, _ = _build_mock_agent()
    failure = classify_failure(VERIFIER_TIMEOUT_REASON)

    assert agent._failure_disposition(failure, 2, True) == "fail_only"


# ---------------------------------------------------------------------------
# 2. Every other kind keeps its disposition
# ---------------------------------------------------------------------------


def test_first_occurrence_keeps_extra_attempt():
    """A first occurrence is evidence that one ANSWER was wrong, not
    that the task is too big — so it still buys one more attempt.
    """
    from failure_classification import classify_failure

    agent, _ = _build_mock_agent()
    failure = classify_failure(REPORT_REJECTION)
    assert failure.kind == "report_format"

    assert agent._failure_disposition(failure, 1, False) == "extra_attempt"


def test_report_format_repeat_still_refines():
    """The repeat of a report rejection is the one case that may reach
    the refiner, and it must still be split-eligible.
    """
    from failure_classification import classify_failure, is_split_allowed

    agent, _ = _build_mock_agent()
    failure = classify_failure(REPORT_REJECTION)

    assert agent._failure_disposition(failure, 2, True) == "refine"
    assert is_split_allowed(failure) is True


def test_repeat_goes_to_refine():
    """A repeated genuine test failure is the case the refiner exists
    for: the code really does not pass, and a smaller subtask is a
    plausible next move.
    """
    from failure_classification import classify_failure

    agent, _ = _build_mock_agent()
    failure = classify_failure(REAL_TEST_FAILURE)
    assert failure.kind == "test_failure"

    assert agent._failure_disposition(failure, 2, True) == "refine"


# ---------------------------------------------------------------------------
# 3. The executor acts on the disposition
# ---------------------------------------------------------------------------


def test_executor_runs_task_once_on_verifier_timeout():
    """The task runs ONCE.

    Not twice for the in-run retry, not a third time for the extra
    attempt the gate grants on a first occurrence: the verification ran
    out of budget, and re-running the task would spend the occurrence
    that turns "the budget was too small" into "the task is too big".
    """
    agent, _ = _build_mock_agent(max_retries=2)

    result = _run_executor(
        agent, (False, VERIFIER_TIMEOUT_REASON), max_retries=2,
    )

    assert result is False
    assert agent.coding_tool.query.call_count == 1, (
        "a verifier timeout must not buy another run of the task; "
        f"got {agent.coding_tool.query.call_count} runs"
    )
    assert agent.refiner.refine.call_count == 0, (
        "a task whose verification produced no verdict must not be "
        "split: the children cannot be verified either"
    )


def test_log_names_unproven_task():
    """The operator has to be able to find this. A task that failed on
    a verifier timeout is NOT a task that was disproven, and the log
    must say so by name.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    logger = MagicMock()
    agent.logger = logger

    _run_executor(agent, (False, VERIFIER_TIMEOUT_REASON), max_retries=2)

    assert "task_verifier_timeout_unproven" in _logged_events(logger)


def test_a_failure_that_was_not_retried_is_not_logged_as_a_retry():
    """``task_retry`` is what the attempt counters are built on.

    A verifier timeout buys no further run of the task, so the log
    must not claim one — an operator reading the event stream would
    otherwise count attempts that never happened.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    logger = MagicMock()
    agent.logger = logger

    _run_executor(agent, (False, VERIFIER_TIMEOUT_REASON), max_retries=2)

    assert "task_retry" not in _logged_events(logger)


def test_refine_after_failure_refuses_a_verifier_timeout():
    """Defence in depth.

    The executor's dispatch already keeps a timeout away from the
    refiner, but ``_refine_after_failure`` is also called from three
    other tails, and a caller that hands it a timeout failure must not
    get a split out of it.
    """
    from failure_classification import classify_failure

    agent, task = _build_mock_agent()
    logger = MagicMock()
    agent.logger = logger

    agent._refine_after_failure(
        task, "coder", "TIMEOUT", "", 1,
        failure=classify_failure(VERIFIER_TIMEOUT_REASON),
        occurrence=2,
    )

    assert agent.refiner.refine.call_count == 0, (
        "the refiner must never see a verifier timeout: there is no "
        "verdict to act on, so there is nothing to decompose"
    )
    assert "refine_skipped_verifier_timeout" in _logged_events(logger)
