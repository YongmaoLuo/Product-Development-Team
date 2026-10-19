"""
Contract for the refiner split gate — "split on the SECOND occurrence
of the SAME failure".

The incident this pins
======================

One production task chain was split to the 5-level breakdown cap and
still ended in failure: 4 tasks went straight to ``failed`` at depth 4,
6 downstream tasks were permanently ``deferred``, and the executor
stopped with ``No schedulable micro-layer found``. The dominant failure
along the way was ``audit_second_pass_failed`` — the second-pass
reviewer refused the *evidence*. The code may well have been correct.
Decomposing a report-shape problem cannot fix it; it only reproduces the
same failure in the children, one level deeper each time.

The change, in one paragraph
============================

On a failure the executor classifies it (report_format / empty_diff /
test_failure / infra) and stamps it with a stable signature. If that
signature has not been seen before in the task's lineage, the executor
does NOT split — it grants one extra subagent attempt, with feedback
naming the problem as the ANSWER rather than the code. Only on a repeat
of the same signature may the refiner split, and it is handed the
classification so its own prompt can obey the gate.

What these tests pin
====================

1. A first occurrence costs an extra attempt and no split.
2. A repeat of the SAME signature splits exactly once, and the refiner
   is told so.
3. Two DIFFERENT signatures are two first occurrences — neither unlocks
   a split. Failing differently each time is not evidence that a task
   is too big.
4. The attempt budget is bounded: ``max_retries=2`` means at most three
   subagent runs, not an unbounded loop.

Everything here drives the production entry point
``AutonomousAgent._execute_task_with_retry``. No test calls
``_refine_after_failure`` directly, because a gate that is only
reachable by calling the gate by hand is not a gate.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# The real strings the production tails classify. Taken from the
# producers (agent._cross_verify_test_result and the empty-diff branch)
# so a rename on the producer side fails here rather than quietly
# re-routing the gate.
AUDIT_REJECTION = (
    "audit_second_pass_failed: evidence table truncated at row 40 of 118"
)
OTHER_AUDIT_REJECTION = (
    "audit_second_pass_failed: quoted exit code 0 but the run exited 1"
)
REAL_TEST_FAILURE = "AssertionError: expected 3, got 2"


def _build_mock_agent(max_retries: int = 2):
    """A bare ``AutonomousAgent`` with every external dep mocked.

    ``__new__`` bypasses ``__init__``, so every attribute the retry loop
    touches is mirrored here from the real initialiser. Two of these
    are load-bearing rather than decorative:

      * ``_failure_occurrences`` — the gate's occurrence table.
      * ``_refinement_count``    — read by the real
        ``_refine_after_failure`` before it will split.

    Unlike ``test_agent_refine_timing``, this builder leaves
    ``_refine_after_failure`` UNMOCKED: these tests are about what
    reaches the refiner, so the real method has to run and
    ``refiner.refine`` is the observation point.
    """
    from agent import AutonomousAgent
    from retry_manager import RetryManager
    from task import SubTask

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp/_refiner_split_gate_dummy")
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

    # The refiner under observation. Returning the caller's own task
    # list makes ``_refine_after_failure`` take its "refiner returned
    # no changes" branch, which keeps these tests pointed at the GATE
    # rather than at the split-application machinery downstream of it.
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


def _run(agent, cross_verify_side_effect, task_id: str = "1", max_retries: int = 2):
    """Drive the production retry loop once and return the outcome."""
    from task import SubTask

    agent._cross_verify_test_result = MagicMock(side_effect=cross_verify_side_effect)
    task = SubTask(
        id=task_id,
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )
    result = agent._execute_task_with_retry(task, max_retries=max_retries)
    return result


def _fail(reason: str):
    return (False, reason)


def _pass():
    return (True, "")


# ---------------------------------------------------------------------------
# 1. A first occurrence buys an iteration, not a split
# ---------------------------------------------------------------------------


def test_first_occurrence_gets_one_more_attempt_and_never_splits():
    """The headline behaviour.

    The subagent fails the same way on both configured attempts. That is
    the FIRST time this signature is seen in this lineage, so the
    executor must not split — it must spend one more attempt, and if
    that attempt answers correctly the task simply completes.

    Before this change the refiner ran here, and a report rejection was
    indistinguishable from a task that is too big.
    """
    agent, _ = _build_mock_agent(max_retries=2)

    result = _run(
        agent,
        [_fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION), _pass()],
    )

    assert result is True, "the extra attempt let the subagent answer correctly"
    # Two configured attempts became three subagent runs.
    assert agent._cross_verify_test_result.call_count == 3
    # The whole point: no refinement, therefore no split.
    assert agent.refiner.refine.call_count == 0, (
        "a first occurrence must not reach the refiner; splitting a "
        "report-shape failure is what caused the depth-4 collapse"
    )


def test_the_extra_attempt_is_told_the_problem_is_the_answer():
    """The retry is only worth its cost if the subagent is told what
    kind of thing it got wrong. ``report_format`` must be fed back as a
    REPORT problem, or the subagent's response is to rewrite working
    code.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    # Spy on the real recorder (wraps, so the retry state the loop
    # reads back is still genuinely written).
    agent.retry_manager.record_attempt = MagicMock(
        wraps=agent.retry_manager.record_attempt,
    )
    # Capture the modifier AS THE LOOP CONSUMES IT. Reading it after
    # the run would prove nothing: the success path calls
    # ``reset_state``, so a post-hoc read returns "" and passes
    # vacuously. What matters is what the subagent was actually sent.
    delivered = []
    real_modifier = agent.retry_manager.get_retry_prompt_modifier

    def _capture_modifier(task_id, bound=None):
        text = real_modifier(task_id, bound)
        delivered.append(text)
        return text

    agent.retry_manager.get_retry_prompt_modifier = _capture_modifier

    _run(agent, [_fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION), _pass()])

    recorded = [
        call.args[1]
        for call in agent.retry_manager.record_attempt.call_args_list
        if call.args[1].startswith("report_format_retry:")
    ]
    assert recorded, (
        "the gate must record a report_format_retry attempt so the next "
        f"iteration gets targeted feedback; got "
        f"{agent.retry_manager.record_attempt.call_args_list}"
    )
    assert "report_format" in recorded[0]

    assert delivered, "a retry must render some retry context"
    extra_attempt_prompt = delivered[-1]
    assert "report_format_retry" in extra_attempt_prompt
    assert "SHAPE OF YOUR ANSWER" in extra_attempt_prompt
    assert "Do NOT restructure the task" in extra_attempt_prompt


# ---------------------------------------------------------------------------
# 2. A repeat of the SAME signature is the only thing that unlocks a split
# ---------------------------------------------------------------------------


def test_second_occurrence_of_the_same_signature_splits_exactly_once():
    """The extra attempt also fails, with the same rejection. Only now
    is a split a legitimate option, and it must happen exactly once.
    """
    agent, _ = _build_mock_agent(max_retries=2)

    result = _run(
        agent,
        [
            _fail(AUDIT_REJECTION),
            _fail(AUDIT_REJECTION),
            _fail(AUDIT_REJECTION),
        ],
    )

    assert result is False
    assert agent.refiner.refine.call_count == 1, (
        "a repeated identical failure is the one case that unlocks a "
        "split, and it must not fire twice"
    )


def test_the_refiner_is_told_why_it_may_split():
    """The refiner's prompt says "read the classification block first",
    so the block has to actually be in ``last_result`` — and has to say
    this is occurrence 2, or the prompt's gate has nothing to read.
    """
    agent, _ = _build_mock_agent(max_retries=2)

    _run(
        agent,
        [_fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION)],
    )

    last_result = agent.refiner.refine.call_args.kwargs["last_result"]
    assert "[FAILURE CLASSIFICATION]" in last_result
    assert "occurrences_in_lineage: 2" in last_result
    assert "kind: report_format" in last_result
    assert "split_allowed: true" in last_result
    # The block leads, because the prompt tells the refiner to read it
    # first — a trailer would be read after the raw output it corrects.
    assert last_result.startswith("[FAILURE CLASSIFICATION]")


def test_the_classification_is_persisted_alongside_the_refiner_result():
    """A postmortem must not have to re-parse it out of a truncated
    preview string.
    """
    agent, _ = _build_mock_agent(max_retries=2)

    _run(
        agent,
        [_fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION)],
    )

    payload = agent._persist_refiner_output.call_args.args[1]
    assert payload["failure_kind"] == "report_format"
    assert payload["failure_occurrences"] == 2
    assert payload["failure_signature"].startswith("report_format:")


# ---------------------------------------------------------------------------
# 3. Different failures are not confirmation of each other
# ---------------------------------------------------------------------------


def test_two_different_signatures_each_count_as_a_first_occurrence():
    """Failing differently on each attempt is not evidence that the
    task is too big — it is evidence the executor has not yet found the
    one thing that is wrong. Neither failure may unlock a split.
    """
    agent, _ = _build_mock_agent(max_retries=2)

    result = _run(
        agent,
        [
            _fail(AUDIT_REJECTION),
            _fail(AUDIT_REJECTION),
            _fail(OTHER_AUDIT_REJECTION),
        ],
    )

    assert result is False
    assert agent.refiner.refine.call_count == 0, (
        "the third failure is a NEW signature (occurrence 1), so the gate "
        "must stay shut — an unrelated second failure must never be "
        "mistaken for confirmation of the first"
    )


def test_the_occurrence_table_is_keyed_by_lineage_not_by_raw_task_id():
    """A split parent and its children are the same work. Keyed by raw
    task id the counter would reset at every level and the gate would
    never open — which is exactly the runaway this replaces.
    """
    agent, task = _build_mock_agent(max_retries=2)

    agent._note_failure(task, AUDIT_REJECTION)
    # A child of the split parent, in a fresh agent instance.
    from task import SubTask

    child = SubTask(
        id="1-3",
        title="child",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )
    _, occurrence = agent._note_failure(child, AUDIT_REJECTION)

    assert agent._lineage_id(child.id) == agent._lineage_id(task.id) == "1"
    assert occurrence == 2, (
        "the same rejection recurring inside a split child is a repeat, "
        "not a fresh first occurrence"
    )


def test_a_repeated_infra_failure_is_never_marked_split_allowed():
    """A timeout is not a statement about the task, so however often it
    repeats it must not be handed to the refiner as a licence to split.

    Note the refiner IS still invoked: the gate's rule 1 lets it clarify
    a task's description or test_command, and for a timeout the useful
    refinement is often exactly that. What must not happen is the block
    telling it ``split_allowed: true``.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    task = agent.task_manager.tasks[0]

    from failure_classification import INFRA, classify_failure

    timeout = "HARD TIMEOUT after 1800s while running the test_command"
    assert classify_failure(timeout).kind == INFRA

    agent._note_failure(task, timeout)
    _, occurrence = agent._note_failure(task, timeout)
    assert occurrence == 2

    agent._refine_after_failure(
        task, "coder", "TIMEOUT", "", 1,
        failure=classify_failure(timeout), occurrence=occurrence,
    )

    last_result = agent.refiner.refine.call_args.kwargs["last_result"]
    assert "occurrences_in_lineage: 2" in last_result, (
        "the repeat is real, so the count must be reported honestly"
    )
    assert "split_allowed: false" in last_result, (
        "infra is never a decomposition problem, however often it repeats"
    )


# ---------------------------------------------------------------------------
# 4. The retry budget stays bounded
# ---------------------------------------------------------------------------


def test_max_retries_two_means_at_most_three_subagent_runs():
    """The extra attempt is a single, one-off grant. If the loop could
    keep extending itself the gate would be an infinite retry loop with
    extra steps — worse than the runaway it replaces.
    """
    agent, _ = _build_mock_agent(max_retries=2)

    _run(
        agent,
        [
            _fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION),
            _fail(AUDIT_REJECTION),
        ],
    )

    assert agent._cross_verify_test_result.call_count == 3, (
        "a fourth side_effect entry must never be consumed: the loop "
        "grants at most ONE extra attempt"
    )


def test_the_attempt_counter_never_renders_attempt_three_of_two():
    """The bound shown to the subagent must match the loop. A prompt
    that says "Attempt 3 of 2" is the exact class of bug that
    ``get_retry_prompt_modifier``'s bound parameter was added to kill.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    seen = []

    logger = MagicMock()

    def _capture(event, message, **kwargs):
        if event == "task_started":
            seen.append(message)

    logger.info.side_effect = _capture
    agent.logger = logger

    _run(
        agent,
        [_fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION), _fail(AUDIT_REJECTION)],
    )

    assert seen, "the task_started event must fire once per attempt"
    # Attempts 1 and 2 render against the configured bound of 2; the
    # third attempt exists only because the gate granted it, and its
    # denominator grows to match rather than rendering "3 of 2".
    assert seen == [
        "Processing Task [1] attempt 1/2",
        "Processing Task [1] attempt 2/2",
        "Processing Task [1] attempt 3/3",
    ]


# ---------------------------------------------------------------------------
# 5. The empty-diff tail is gated the same way
# ---------------------------------------------------------------------------


def test_empty_diff_on_its_first_occurrence_is_not_split():
    """The other failure tail. Producing no diff is a report-shape
    problem — the subagent wrote commentary instead of calling Edit —
    and decomposing the task cannot make a subagent start using its
    tools.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    # Pretend every declared deliverable is missing AND nothing changed,
    # which is what drives the empty-diff branch.
    agent._task_declared_files_exist = MagicMock(return_value=False)

    from task import SubTask

    task = SubTask(
        id="1",
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
        files_to_modify=["src/x.py"],
    )

    # The loop decides this branch on the git diff plus the declared
    # files; with neither present it reports an empty diff every time.
    agent.git_manager.get_changed_files = MagicMock(return_value=[])

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is False
    # 3 attempts, then the repeat unlocks exactly one refine.
    assert agent.coding_tool.query.call_count == 3
    assert agent.refiner.refine.call_count == 1


def test_timeout_tail_prompt_has_a_no_block_fallback():
    """The two timeout tails call ``_refine_after_failure`` without
    ``failure=``, so their context carries no [FAILURE CLASSIFICATION]
    block at all. Without an explicit fallback the SPLIT GATE preamble
    ("read the block first", "if split_allowed is false, do NOT split")
    leaves the model to guess — and guessing ``false`` silently turns
    hard-timeout auto-split off. This asserts the prompt says what to do
    when the block is absent, without running an LLM.
    """
    from config_registry import ConfigRegistry

    prompt = ConfigRegistry.get("coding").refiner_system_prompt
    assert "does NOT contain a [FAILURE CLASSIFICATION] block" in prompt


# ---------------------------------------------------------------------------
# 6. The extra attempt is told the RIGHT thing, not just "something"
# ---------------------------------------------------------------------------


def _capture_delivered_modifiers(agent):
    """Collect the retry text AS THE LOOP CONSUMES IT.

    Reading ``get_retry_prompt_modifier`` after the run would prove
    nothing: the success path calls ``reset_state``, so a post-hoc read
    returns "" and passes vacuously. What matters is the text the
    subagent was actually handed on the extra attempt.
    """
    delivered = []
    real_modifier = agent.retry_manager.get_retry_prompt_modifier

    def _capture(task_id, bound=None):
        text = real_modifier(task_id, bound)
        delivered.append(text)
        return text

    agent.retry_manager.get_retry_prompt_modifier = _capture
    # wraps= so the retry state the loop reads back is genuinely
    # written; a bare MagicMock would leave ``last_error`` empty and the
    # whole assertion would pass for the wrong reason.
    agent.retry_manager.record_attempt = MagicMock(
        wraps=agent.retry_manager.record_attempt,
    )
    return delivered


def test_empty_diff_extra_attempt_keeps_the_actionable_tool_hint():
    """An empty diff must NOT be re-labelled as a report-shape problem.

    Both tails share this gate, so it is tempting to record one
    ``report_format_retry:`` line for both. That silently swaps the most
    actionable hint in the whole subsystem: the subagent did not call
    Edit, and ``get_retry_prompt_modifier`` has a dedicated branch for
    exactly that — "use Edit/Write, then run ``git diff --stat`` and
    check". Telling it instead that "this is not a code problem, do NOT
    restructure the task" is the one response guaranteed to produce the
    same empty diff again.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    # Drive the empty-diff tail: nothing changed AND no declared
    # deliverable on disk, which is the condition for that branch.
    agent._task_declared_files_exist = MagicMock(return_value=False)
    delivered = _capture_delivered_modifiers(agent)

    from task import SubTask

    task = SubTask(
        id="1",
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
        files_to_modify=["src/x.py"],
    )
    agent.git_manager.get_changed_files = MagicMock(return_value=[])
    agent._execute_task_with_retry(task, max_retries=2)

    recorded = [
        call.args[1]
        for call in agent.retry_manager.record_attempt.call_args_list
    ]
    assert any(
        text.startswith("empty_diff_no_changes:") for text in recorded
    ), (
        "the empty-diff sentinel must still reach RetryManager; got "
        f"{recorded}"
    )
    assert not any(
        text.startswith("report_format_retry:") for text in recorded
    ), (
        "an empty diff must not be re-labelled as a report-shape "
        f"rejection; got {recorded}"
    )

    assert len(delivered) == 2, (
        "attempts 2 and 3 are the in-run retries, so the modifier is "
        f"rendered twice; got {delivered}"
    )
    extra_attempt_prompt = delivered[-1]
    assert "ACTION REQUIRED — you must actually modify source files" in (
        extra_attempt_prompt
    ), (
        "the extra attempt must keep the hint that tells the subagent "
        "which tool to use; the branch that emits it matches on the "
        f"`empty_diff_no_changes` prefix. Got:\n{extra_attempt_prompt}"
    )
    assert "SHAPE OF YOUR ANSWER" not in extra_attempt_prompt, (
        "an empty diff is not a report-shape rejection; telling the "
        "subagent not to restructure the task is the opposite advice"
    )


def test_a_real_test_failure_is_not_told_its_a_report_problem():
    """The other half of the same bug, in the other tail.

    ``test_failure`` means the tests genuinely ran and genuinely failed.
    Telling the subagent "this is a report/evidence problem, not
    necessarily a code problem... Do NOT restructure the task" points
    it away from the one thing that could fix the run. It must fall
    through to the generic "here is the error, try again" branch, which
    at least shows it the actual failure text.
    """
    agent, _ = _build_mock_agent(max_retries=2)
    delivered = _capture_delivered_modifiers(agent)

    # Genuine test output, not an audit rejection: this classifies as
    # ``test_failure``, which must NOT take the report-format branch.
    from failure_classification import classify_failure

    assert classify_failure(REAL_TEST_FAILURE).kind == "test_failure"

    _run(agent, [_fail(REAL_TEST_FAILURE), _fail(REAL_TEST_FAILURE), _pass()])

    recorded = [
        call.args[1]
        for call in agent.retry_manager.record_attempt.call_args_list
    ]
    assert not any(
        text.startswith("report_format_retry:") for text in recorded
    ), (
        "only a report_format failure may be re-labelled; a real test "
        f"failure must be recorded verbatim. Got {recorded}"
    )
    assert REAL_TEST_FAILURE in recorded, (
        "the real error text must reach RetryManager so the generic "
        f"branch can show it; got {recorded}"
    )

    assert delivered, "a retry must render some retry context"
    extra_attempt_prompt = delivered[-1]
    assert "Previous attempt failed with error" in extra_attempt_prompt, (
        "a real test failure belongs in the generic branch, which hands "
        f"the subagent the error. Got:\n{extra_attempt_prompt}"
    )
    assert "SHAPE OF YOUR ANSWER" not in extra_attempt_prompt, (
        "'do NOT rewrite the code' is backwards advice when the tests "
        "genuinely do not pass"
    )
