"""A hard timeout buys a RESTART first; only a repeat reaches the refiner.

What this file pins
-------------------
The hard-timeout tail of ``AutonomousAgent._execute_task_with_retry``
used to go straight to the refiner: it never called ``_note_failure``
and never passed ``failure=``, so the run logged no
``failure_classified`` event at all and the task was split on the FIRST
hard timeout.

That was the wrong answer twice over. The executor's message ("Hard
timeout after 3600s of work time") classifies as ``coder_timeout`` --
the sub-agent used up its work budget -- and NOT as a test failure: no
assertion ever ran. So the honest response to the first one is to give
the task another go with the SAME budget, and only a repeat is evidence
that the task needs to be smaller.

Two contracts:

1. The first hard timeout, with the inner retries exhausted, buys ONE
   extra attempt. The task is not failed, and the refiner is not
   called. A run where the third dispatch succeeds ENDS SUCCEEDING --
   which is the observable proof that the task was not failed on the
   first exhausted timeout.

2. The second hard timeout in the same lineage reaches the refiner,
   and the refiner is handed the classified failure (not ``None``) and
   the occurrence count that authorises the split.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from coding_tool import HardTimeoutError  # noqa: E402
from failure_classification import CODER_TIMEOUT  # noqa: E402


PASSED_RESPONSE = "Some impl work.\n\nTEST_RESULT: PASSED\nREASON: looks good\n"


def _hard_timeout() -> HardTimeoutError:
    """The executor's own work-budget timeout, verbatim.

    ``for_work_budget`` is the constructor that produces the message
    production produced: "Hard timeout after 3600s of work time
    (elapsed=3600.0s of wall clock)". The silence variant is a
    different message and is not what this file drives.
    """
    return HardTimeoutError.for_work_budget(3600, 3600.0)


class _RecordingLogger:
    """A logger that keeps every event it is handed."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str, event: str, message: str, **kwargs: Any) -> None:
        self.events.append(
            {"level": level, "event": event, "message": message, **kwargs}
        )

    def info(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("info", event, message, **kwargs)

    def warning(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("warning", event, message, **kwargs)

    def error(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("error", event, message, **kwargs)

    def debug(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("debug", event, message, **kwargs)

    def events_named(self, name: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == name]


def _build_mock_agent(max_retries: int = 2):
    """An ``AutonomousAgent`` with the fields the failure tail touches.

    Built the same way as ``test_agent_refine_timing.py`` — ``__new__``
    instead of ``__init__``, because the real constructor starts a lock
    broker, opens a task database and resolves a coding tool, none of
    which the hard-timeout tail depends on.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp/_agent_hard_timeout_restart_dummy")
    agent.logger = _RecordingLogger()

    agent.retry_manager = __import__("retry_manager").RetryManager()

    agent.coding_tool = MagicMock()
    agent.coding_tool.query = MagicMock(return_value=PASSED_RESPONSE)

    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)

    agent.task_manager = MagicMock()
    agent.task_manager.update_task_status = MagicMock()
    agent.task_manager.record_task_failure = MagicMock()
    agent.task_manager.record_task_superseded = MagicMock(return_value=False)

    agent.config = MagicMock()
    agent.config.executor_system_prompt = ""
    agent.config.max_retries = max_retries

    agent.subagent_cfg = None
    agent.parse_files_from_response = MagicMock(return_value={})

    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files = MagicMock(return_value=[])

    agent._commit_task_changes = MagicMock()
    agent._task_declared_files_exist = MagicMock(return_value=True)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._clean_test_command = lambda cmd: cmd
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))

    refine_call_log: List[Dict[str, Any]] = []

    def _refine_spy(*args, **kwargs):
        # The two failure tails call ``_refine_after_failure``
        # differently — the generic one positionally, the hard-timeout
        # one by keyword — so the task is read from whichever carries
        # it.
        task = args[0] if args else kwargs.get("task")
        refine_call_log.append(
            {"args": args, "kwargs": kwargs, "task_id": task.id}
        )

    agent._refine_after_failure = MagicMock(side_effect=_refine_spy)
    agent.refine_call_log = refine_call_log

    agent._session_task_completed_counts = {}
    # The split gate's occurrence table, mirrored from ``__init__``.
    agent._failure_occurrences = {}
    agent._refinement_count = 0

    return agent


def _build_task(task_id: str = "9"):
    from task import SubTask

    return SubTask(
        id=task_id,
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )


def test_first_hard_timeout_buys_an_extra_attempt_instead_of_failing() -> None:
    """The first exhausted hard timeout must NOT fail the task.

    Two dispatches time out (the inner retry + the exhausted attempt),
    the third succeeds. If the first exhausted timeout had failed the
    task, the run would end here with the task marked failed and the
    third dispatch would never happen.
    """
    agent = _build_mock_agent(max_retries=2)
    agent.coding_tool.query = MagicMock(side_effect=[
        _hard_timeout(), _hard_timeout(), PASSED_RESPONSE,
    ])
    task = _build_task("9")

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is True, (
        "the task must survive the first exhausted hard timeout — it "
        "gets an extra attempt, not a failure"
    )
    assert agent.coding_tool.query.call_count == 3, (
        "the third dispatch is the extra attempt the first hard timeout "
        "bought"
    )
    assert agent.refine_call_log == [], (
        "the refiner must not see a first-occurrence hard timeout"
    )
    assert agent.task_manager.record_task_failure.call_count == 0

    # The run says what happened: the failure was classified, and the
    # extra attempt was granted. Before this change the whole round
    # produced no ``failure_classified`` event at all.
    classified = agent.logger.events_named("failure_classified")
    assert len(classified) == 1
    assert classified[0]["data"]["kind"] == CODER_TIMEOUT
    assert classified[0]["data"]["occurrence"] == 1
    assert agent.logger.events_named("failure_first_occurrence_retry")


def test_second_hard_timeout_in_the_same_lineage_reaches_the_refiner_with_a_failure() -> None:
    """A repeat is what the split gate reads as "too big for one
    subagent" — so the refiner may look at it, and it is handed the
    classified failure rather than nothing.
    """
    agent = _build_mock_agent(max_retries=2)
    agent.coding_tool.query = MagicMock(side_effect=_hard_timeout())
    task = _build_task("9")

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is False
    assert len(agent.refine_call_log) == 1, (
        "a REPEAT hard timeout is the one that reaches the refiner"
    )
    call = agent.refine_call_log[0]
    failure = call["kwargs"].get("failure")
    assert failure is not None, (
        "the refiner must be handed the classified failure; without it "
        "the split gate cannot tell a repeat from a first occurrence"
    )
    assert failure.kind == CODER_TIMEOUT
    assert call["kwargs"].get("occurrence") == 2

    classified = agent.logger.events_named("failure_classified")
    assert [e["data"]["occurrence"] for e in classified] == [1, 2]


def test_a_hard_timeout_is_not_classified_as_a_test_failure() -> None:
    """The regression itself: before the ``coder_timeout`` kind, the
    executor's message fell through every timeout rule and was filed as
    ``test_failure`` — "the code does not pass, split it" — for a run
    whose tests never executed.
    """
    from failure_classification import TEST_FAILURE, classify_failure

    failure = classify_failure(str(_hard_timeout()))
    assert failure.kind == CODER_TIMEOUT
    assert failure.kind != TEST_FAILURE
