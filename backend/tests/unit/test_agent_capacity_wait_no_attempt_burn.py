"""A provider with no room must not spend the task's retry budget.

2026-10-24 — the 429 that killed the plan
------------------------------------------
``agent._execute_task_with_retry`` used to treat a provider capacity
error as "one attempt, then try again":

    if is_capacity_error(e) and attempt < max_retries - 1:
        self.retry_manager.record_attempt(task.id, str(e), False)
        ...
        continue

Two things were wrong with that, and they compounded:

  1. **The attempt was spent on the provider's behalf.** A 429 says
     nothing about the task — it says the provider is out of budget.
     Charging the task's retry budget for it means a task that could
     have run the moment a provider came back dies instead, and every
     task downstream of it is deferred forever ("No schedulable
     micro-layer found").
  2. **The other capacity refusal was not capacity at all.** When every
     provider is parked, ``coding_tool.query`` raises
     ``SceneProviderUnavailable(reason="all_candidates_skipped")`` — a
     ``RuntimeError``, not an ``ApiError``. It fell through to the
     generic ``except Exception`` arm, which records an attempt and
     retries, so the same accident happened with a different exception
     type.

What these tests pin:

  * a capacity refusal is WAITED OUT, and the wait does not consume the
    task's attempt — the same attempt retries the same call;
  * when the wait budget runs out, the task is NOT failed: it gets a
    ``waiting_for_quota`` marker on ``tasks.json`` and stays alive, so
    the dispatcher can pick it up again;
  * a routing failure that waiting cannot fix (``no_tier_match``) is
    still an error, and is not turned into a wait.

The wait is measured in seconds, so the two module-level seams
(``agent._capacity_wait_sleep`` / ``agent._capacity_wait_clock``) are
patched here and nothing in this file ever sleeps for real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import agent as agent_module  # noqa: E402
from capacity_wait import QuotaWaitExhausted  # noqa: E402
from coding_tool import ApiError, SceneProviderUnavailable  # noqa: E402
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402


#: The directory the plan's ``tasks.json`` lives in. A real plan id
#: (not a pytest tmp dir name) keeps ``TaskManager``'s plan-id
#: derivation on its normal path.
_PLAN_DIR_NAME = "plan-under-test"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Record every capacity wait instead of performing it.

    The seam is module level on purpose: the wait is wall-clock, and a
    test that has to wait for real seconds to test a wait is a test
    nobody runs. ``raising=False`` keeps this fixture usable while the
    seam does not exist yet; the budget below is the safety net for the
    day the seam is renamed by accident — a wait that is not patched
    cannot cost more than two seconds.
    """
    slept = []
    monkeypatch.setattr(
        agent_module,
        "_capacity_wait_sleep",
        lambda seconds: slept.append(seconds),
        raising=False,
    )
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "2")
    return slept


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Collects the structured events the agent writes."""

    def __init__(self) -> None:
        self.events = []

    def _record(self, level):
        def _fn(event, message, **kwargs):
            self.events.append({
                "level": level.upper(),
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug", "critical"):
            return self._record(name)
        raise AttributeError(name)

    def names(self):
        return [e["event"] for e in self.events]

    def data_for(self, event):
        for entry in self.events:
            if entry["event"] == event:
                return entry["data"]
        return None


class _ScriptedTool:
    """A coding tool whose ``query`` walks a scripted list of outcomes.

    The last outcome repeats, so a script of three refusals and one
    success answers "ok" from the fourth call on.
    """

    def __init__(self, outcomes, provider="vendor-a"):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.current_call_provider = provider

    def query(self, *args, **kwargs):
        self.calls += 1
        index = min(self.calls - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _make_task(tmp_path, task_id="14-2"):
    return SubTask(
        id=task_id,
        title="implement the thing",
        description="implement the thing",
        # ``exit 1`` so the pre-flight gate falls through to the real
        # subagent path these tests exercise.
        test_command="exit 1",
        status="pending",
        project_dir=str(tmp_path),
        files_to_modify=["changed.py"],
        model_type="medium",
    )


def _make_agent(tmp_path, logger, task, tool):
    """An agent wired with just enough to run the retry loop."""
    from agent import AutonomousAgent

    plan_dir = tmp_path / _PLAN_DIR_NAME
    plan_dir.mkdir(parents=True, exist_ok=True)

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.logger = logger
    agent.project_dir = tmp_path
    agent.coding_tool = tool
    agent._task_progress_repo = None
    agent._session_task_completed_counts = {}
    agent._quota_wait_requested = False

    task_manager = TaskManager(tmp_path, tasks_file=plan_dir / "tasks.json")
    task_manager.tasks = [task]
    task_manager.save_tasks()
    # The real method, counted — the assertion is "the task was never
    # failed", so the call has to be observable.
    task_manager.record_task_failure = MagicMock(
        wraps=task_manager.record_task_failure,
    )
    agent.task_manager = task_manager

    retry_manager = type("RM", (), {})()
    retry_manager.get_retry_prompt_modifier = lambda *a, **k: ""
    retry_manager.record_attempt = MagicMock()
    retry_manager.reset_state = lambda *a, **k: None
    agent.retry_manager = retry_manager

    executor = type("EX", (), {})()
    executor.had_previous_timeout = lambda *a, **k: False
    executor.record_timeout = lambda *a, **k: None
    agent.executor = executor

    agent.config = type("CFG", (), {"executor_system_prompt": "sys"})()

    git_manager = type("GM", (), {})()
    git_manager.get_changed_files = lambda *a, **k: []
    agent.git_manager = git_manager

    # The success path, stubbed down to the parts this suite is about.
    agent._build_files_to_modify_hint = lambda task: ""
    agent._build_report_hint = lambda task: ""
    agent._build_prior_failure_block = lambda task: ""
    agent._clean_test_command = lambda cmd: cmd
    agent._validate_test_command_shape = lambda *a, **k: None
    agent._preflight_test_command_skip = lambda *a, **k: False
    agent._looks_like_audit_task = lambda *a, **k: False
    agent._expects_clean_tree = lambda *a, **k: False
    agent._persist_agent_output = lambda *a, **k: None
    agent.parse_files_from_response = lambda response: {"changed.py": "x = 1\n"}
    agent._parse_test_result = lambda response: (True, "stubbed")
    agent._cross_verify_test_result = lambda **k: (True, "stubbed")
    agent._audit_task_second_pass = lambda **k: (True, "stubbed")
    # An empty diff stat skips the inline review, so the only query in
    # a successful run is the one this suite scripts.
    agent._get_git_diff_stat_for_review = lambda *a, **k: ""
    agent._commit_task_changes = MagicMock()
    return agent


def _tasks_json(tmp_path):
    return json.loads(
        (tmp_path / _PLAN_DIR_NAME / "tasks.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# The refusal is waited out, and the attempt is not spent on it
# ---------------------------------------------------------------------------


def test_capacity_error_does_not_consume_attempt(tmp_path, monkeypatch):
    """Three 429s and then a good answer: one attempt, four calls.

    The provider refused three times. The task's answer came on the
    fourth call — and it did so on attempt 1, because a provider that
    has no room is not evidence that the task is wrong.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([
        ApiError("quota exceeded", status="429"),
        ApiError("quota exceeded", status="429"),
        ApiError("quota exceeded", status="429"),
        "done",
    ])
    agent = _make_agent(tmp_path, logger, task, tool)

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is True, (
        "the task must still complete: a 429 says the provider has no "
        "room, not that the work is impossible"
    )
    assert tool.calls == 4, (
        "the same attempt must retry the call after each wait — the "
        "refusal is waited out, not turned into a new attempt"
    )
    assert agent.retry_manager.record_attempt.call_count == 0, (
        "a capacity refusal must not be charged to the task's retry "
        "budget"
    )
    assert logger.names().count("task_started") == 1, (
        "the whole thing happened on attempt 1 — the wait did not "
        "advance the attempt counter"
    )


def test_capacity_wait_is_logged_with_the_waited_seconds(
    tmp_path, monkeypatch, _no_real_sleep
):
    """The wait is observable: the retry event carries ``waited_sec``."""
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([
        ApiError("quota exceeded", status="429"),
        "done",
    ])
    agent = _make_agent(tmp_path, logger, task, tool)

    agent._execute_task_with_retry(task, max_retries=2)

    assert _no_real_sleep == [30.0]
    data = logger.data_for("task_api_error_provider_retry")
    assert data is not None, (
        "the wait must be visible in the plan's execution log"
    )
    assert "waited_sec" in data
    assert data["attempt"] == 1


# ---------------------------------------------------------------------------
# The budget running out is not a failure
# ---------------------------------------------------------------------------


def test_budget_exhaustion_leaves_task_pending(tmp_path, monkeypatch):
    """No budget left → the task is not failed. It is still in progress."""
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([ApiError("quota exceeded", status="429")])
    agent = _make_agent(tmp_path, logger, task, tool)

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is False
    assert agent.task_manager.record_task_failure.call_count == 0, (
        "a provider that has no room must not fail the task — that is "
        "what strands everything downstream"
    )
    assert task.status != "failed"
    assert task.status == "in_progress"


def test_budget_exhaustion_records_waiting_for_quota(tmp_path, monkeypatch):
    """The marker is written where an operator will look for it."""
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([ApiError("quota exceeded", status="429")])
    agent = _make_agent(tmp_path, logger, task, tool)

    agent._execute_task_with_retry(task, max_retries=2)

    data = _tasks_json(tmp_path)
    assert data["stop_reason"] == "waiting_for_quota", (
        "the plan must record that it stopped because the provider ran "
        f"out of room, not because the work failed. Got: "
        f"{data.get('stop_reason')!r}"
    )
    detail = json.loads(data["reason_detail"])
    assert detail["providers"] == ["vendor-a"]
    assert detail["needs_manual_topup"] is False
    assert detail["waited_sec"] >= 0
    assert detail["at"]
    assert logger.names().count("task_quota_wait_exhausted") == 1


def test_zero_budget_escalates_on_first_refusal(tmp_path, monkeypatch):
    """``PDT_CAPACITY_WAIT_MAX_SEC=0`` means "do not wait, escalate now"."""
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([ApiError("quota exceeded", status="429")])
    agent = _make_agent(tmp_path, logger, task, tool)

    agent._execute_task_with_retry(task, max_retries=2)

    assert tool.calls == 1, (
        "with no budget there is nothing to wait for — the first "
        "refusal is the last one"
    )


# ---------------------------------------------------------------------------
# The other capacity refusal: no provider at all
# ---------------------------------------------------------------------------


def test_scene_provider_unavailable_all_candidates_skipped_waits(
    tmp_path, monkeypatch
):
    """Every provider parked → wait, and when the budget is gone, mark.

    ``SceneProviderUnavailable`` is a ``RuntimeError``, not an
    ``ApiError``: under the old code it landed in the generic
    ``except Exception`` arm, which recorded an attempt and retried —
    the same accident, wearing a different exception type.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([
        SceneProviderUnavailable(
            "no provider is dispatchable",
            scene="execution",
            reason="all_candidates_skipped",
            candidates=["vendor-a"],
        ),
    ])
    agent = _make_agent(tmp_path, logger, task, tool)

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is False
    assert agent.task_manager.record_task_failure.call_count == 0
    assert _tasks_json(tmp_path)["stop_reason"] == "waiting_for_quota"

    # And with a budget it WAITS instead of escalating at once.
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")
    logger2 = _LogRecorder()
    task2 = _make_task(tmp_path, task_id="14-3")
    tool2 = _ScriptedTool([
        SceneProviderUnavailable(
            "no provider is dispatchable",
            scene="execution",
            reason="all_candidates_skipped",
            candidates=["vendor-a"],
        ),
        "done",
    ])
    agent2 = _make_agent(tmp_path, logger2, task2, tool2)

    result2 = agent2._execute_task_with_retry(task2, max_retries=2)

    assert result2 is True
    assert tool2.calls == 2, (
        "a parked-provider refusal must be waited out like any other "
        "capacity refusal"
    )


def test_scene_provider_unavailable_config_error_propagates(tmp_path):
    """``no_tier_match`` is a configuration error. Waiting cannot fix it."""
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool(["done"])
    agent = _make_agent(tmp_path, logger, task, tool)

    refusal = SceneProviderUnavailable(
        "no CC Switch provider name matches any pattern",
        scene="execution",
        reason="no_tier_match",
    )

    def _call():
        raise refusal

    with pytest.raises(SceneProviderUnavailable) as excinfo:
        agent._query_with_capacity_wait(_call, task, (1, 2))

    assert excinfo.value is refusal
    assert excinfo.value.reason == "no_tier_match"


# ---------------------------------------------------------------------------
# The hard cap, from the coding tool
# ---------------------------------------------------------------------------


def test_quota_wait_exhausted_is_not_swallowed(tmp_path, monkeypatch):
    """``QuotaWaitExhausted`` is the final answer — it must not be
    mistaken for a retryable error."""
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([
        QuotaWaitExhausted(
            providers=["vendor-a"],
            needs_manual_topup=True,
            waited_sec=12.0,
        ),
    ])
    agent = _make_agent(tmp_path, logger, task, tool)

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is False
    assert agent.task_manager.record_task_failure.call_count == 0, (
        "an exhausted wait is not a task failure"
    )
    assert agent.retry_manager.record_attempt.call_count == 0

    data = _tasks_json(tmp_path)
    assert data["stop_reason"] == "waiting_for_quota"
    detail = json.loads(data["reason_detail"])
    assert detail["needs_manual_topup"] is True
    assert detail["providers"] == ["vendor-a"]
    assert detail["waited_sec"] == 12.0
    assert logger.names().count("task_quota_wait_exhausted") == 1
