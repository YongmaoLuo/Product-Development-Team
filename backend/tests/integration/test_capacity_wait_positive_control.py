"""Positive control: a provider that comes back keeps the task alive.

2026-10-24 — the import path's positive control
-----------------------------------------------
The import path's contract is one sentence: **a provider that refuses
because it has no room is not evidence that the task failed.** The
negative half of that contract is pinned by task 4's unit suite — when
the wait budget runs out, the task is not failed, it gets a
``waiting_for_quota`` marker and stays eligible for another dispatch.

This file is the other half, and it is the half that can be *silently*
broken: the case where the provider comes back. One 429, one park of
exactly the poll interval, and the SAME attempt asks again and
succeeds. What has to be true afterwards:

  * the call was made twice and the answer came on the second call;
  * the park was exactly one poll interval long — not "at most", not
    "as long as it took";
  * **no failure marker was written**: the task was not failed, no
    ``failed`` status was ever handed to ``update_task_status``, and
    ``tasks.json`` carries no ``stop_reason``;
  * the attempt budget was not spent: the whole thing happened on
    attempt 1, and ``record_attempt`` was never called.

The two levels, and why both are here
-------------------------------------
Two of the tests drive the helper directly
(:meth:`agent.AutonomousAgent._query_with_capacity_wait`) — that is the
unit of behaviour the import path promises, and driving it directly is
what says "one park, then success" without the rest of the executor
in the way. The other two drive the whole retry loop, because the
failure-marker assertions are only load-bearing there: a helper call
cannot write a failure marker no matter what the code does, so
"``record_task_failure`` was never called" proved by a helper call
proves nothing. The full path is where a marker could actually appear,
so that is where it is asserted.

Both levels share the same fake clock, and neither ever sleeps for
real, spawns a process, or talks to a provider. The "leaves nothing
behind" half of that is not just an intention here: the child
processes the code under test starts are counted and asserted to be
none, and the application worker threads are covered by the suite's
own autouse ``clean_execution_state`` fixture, which fails a test
whose worker outlives it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import agent as agent_module  # noqa: E402
from coding_tool import ApiError  # noqa: E402
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402


pytestmark = pytest.mark.integration


#: The directory the plan's ``tasks.json`` lives in. A real plan id
#: (not a pytest tmp dir name) keeps ``TaskManager``'s plan-id
#: derivation on its normal path.
_PLAN_DIR_NAME = "plan-under-test"


# ---------------------------------------------------------------------------
# The wait's clock
# ---------------------------------------------------------------------------


class _FakeClock:
    """The wait, and the reading of the wait — both under the test's hand.

    The wait is measured in wall-clock seconds, and a test that has to
    wait for real seconds to test a wait is a test nobody runs. Both
    halves of the measurement are therefore faked, and the two halves
    have to agree: sleeping records what the wait was asked for and
    moves the clock by exactly that much, so "waited 30 seconds" is a
    fact the test can assert rather than a number the machine happened
    to produce. The budget decision the helper makes — "have I waited
    out my budget?" — is then a function of the script and not of how
    loaded the runner is.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        """What the wait does instead of sleeping: record, then advance.

        The advance is the whole point: the clock and the sleeper are
        two views of the same fake time, so a wait of 30 seconds costs
        the budget exactly 30 seconds.
        """
        self.sleeps.append(float(seconds))
        self.now += float(seconds)

    def clock(self) -> float:
        """What the wait reads instead of the wall clock."""
        return self.now


@pytest.fixture
def wait(monkeypatch):
    """The fake clock, installed where the helper looks for it.

    :meth:`agent.AutonomousAgent._query_with_capacity_wait` reads the
    two seams off the agent (``getattr(self, ...)``) and falls back to
    the module-level pair. The module-level pair is installed here as
    the safety net: a seam that gets renamed by accident must not turn
    a test into a real thirty-second sleep. The instance-level pair
    (see :func:`_install_wait_seams`) is what the contract names.

    The poll interval and the budget are the ones the import path
    documents — look at the provider every 30 seconds, for at most half
    an hour. "The provider came back within 30 seconds" is exactly
    "one poll interval", and it is expressed in the environment the
    production code reads, not in a patched constant. Each test
    restates them, so a test's own preconditions are readable in the
    test; this fixture only makes sure no test can forget them.
    """
    fake = _FakeClock()
    monkeypatch.setattr(
        agent_module, "_capacity_wait_sleep", fake.sleep, raising=False,
    )
    monkeypatch.setattr(
        agent_module, "_capacity_wait_clock", fake.clock, raising=False,
    )
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")
    return fake


def _install_wait_seams(monkeypatch, agent, fake: _FakeClock) -> None:
    """Put the fake clock on the agent itself.

    The helper reads the seams off the agent first
    (``getattr(self, "_capacity_wait_sleep", None)``), so this is where
    a test that wants a specific clock puts it. ``raising=False``
    because the attribute is not declared on the class: the seam is a
    module-level global that an instance may override.
    """
    monkeypatch.setattr(
        agent, "_capacity_wait_sleep", fake.sleep, raising=False,
    )
    monkeypatch.setattr(
        agent, "_capacity_wait_clock", fake.clock, raising=False,
    )


#: The read-only probes the per-round context pack runs to gather the
#: round's environment facts once, in one place, so the sub-agent does not
#: have to explore them again (see ``backend/context_pack.py``).  They belong
#: to building the brief — every round, parked or not — and they are finished
#: before the run returns, so a parked or recovering path does not spawn
#: them: it inherits them from the round that asked for the brief.
#:
#: They are the one exemption from the count below, and it is a named one.
#: Every other spawn — a provider, a mutation, an unrelated program — is
#: still counted, and still fails the assertion.
_CONTEXT_PROBE_PREFIXES = (
    ("git", "branch", "--show-current"),
    ("git", "rev-parse", "--verify", "--quiet"),
    ("git", "merge-base", "HEAD"),
    ("git", "diff", "--stat"),
    ("git", "diff", "--name-only"),
    ("git", "ls-files", "--others", "--exclude-standard"),
    ("lsof", "-a", "-n", "-P", "-iTCP", "-sTCP:LISTEN", "-F", "cn"),
)


def _is_context_probe(argv: Any) -> bool:
    """Is this spawn one of the context pack's read-only probes?"""
    if not argv:
        return False
    if isinstance(argv, str):
        argv = argv.split()
    parts = [str(part) for part in argv]
    if parts[0].rsplit("/", 1)[-1] == "git" and len(parts) > 3 and parts[1] == "-C":
        # ``git -C <repo> <args>``: the repository is the worktree the
        # pack read, not part of the command.
        parts = [parts[0], *parts[3:]]
    return any(
        tuple(parts[: len(prefix)]) == prefix for prefix in _CONTEXT_PROBE_PREFIXES
    )


@pytest.fixture
def child_processes(monkeypatch):
    """The child processes the code under test starts, counted.

    The recovery path is a wait and a re-ask — it spawns nothing, and
    a child process that outlives its test is a defect the suite
    already treats as one (see the conftest's ``clean_execution_state``,
    which reclaims descriptors and children and fails on an
    application worker thread that survives its test). The count is
    asserted here rather than assumed, so "nothing was left running"
    is a fact of this run.

    The read-only probes the per-round context pack runs are the one
    exemption: they belong to building the brief, not to this path.
    """
    started: List[Any] = []
    real_popen = subprocess.Popen

    def _counting_popen(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if not _is_context_probe(argv):
            started.append(argv)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _counting_popen)
    return started


def test_the_context_probe_exemption_is_named_and_narrow() -> None:
    """The exemption the count makes is exactly the context pack's probes.

    The count is the evidence that this path starts no work.  Were the
    exemption wider than the probes, a provider or a mutation would slip
    through it and the count would stop being evidence.
    """
    assert _is_context_probe(
        ["git", "-C", "/tmp/worktree", "branch", "--show-current"]
    )
    assert _is_context_probe(
        [
            "git",
            "-C",
            "/tmp/worktree",
            "rev-parse",
            "--verify",
            "--quiet",
            "origin/main^{commit}",
        ]
    )
    assert _is_context_probe(
        ["lsof", "-a", "-n", "-P", "-iTCP", "-sTCP:LISTEN", "-F", "cn"]
    )
    # Not a probe: a mutation, a provider, and an unrelated program.
    assert not _is_context_probe(["git", "-C", "/tmp/worktree", "commit", "-m", "x"])
    assert not _is_context_probe(["claude", "-p", "do the work"])
    assert not _is_context_probe(["python3", "-c", "print(1)"])
    assert not _is_context_probe([])


# ---------------------------------------------------------------------------
# Doubles
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


class _ScriptedCall:
    """A callable that walks a scripted list of outcomes.

    The last outcome repeats, so a script of one refusal and one
    success answers "ok" from the second call on — which is what makes
    "one refusal, one park, then the answer" expressible without a
    second helper.

    The call counter is the point of the double: it is what says the
    helper asked again (twice) rather than that the script was walked
    twice by something else.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        index = min(self.calls - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _ScriptedTool:
    """A coding tool whose ``query`` walks a scripted list of outcomes.

    The last outcome repeats, so a script of one refusal and one
    success answers "done" from the second call on.
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
    """An agent wired with just enough to run the retry loop.

    The plan state is real: a real :class:`TaskManager` over a real
    ``tasks.json`` on disk, so "the plan carries no failure marker" is
    a statement about a file, not about a mock. What is stubbed is the
    work around the call — the git commit, the test command, the
    inline review — none of which is what this file is about.
    """
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
    # The real methods, counted: the assertion is "the task was never
    # failed", so the calls have to be observable.
    task_manager.record_task_failure = MagicMock(
        wraps=task_manager.record_task_failure,
    )
    task_manager.update_task_status = MagicMock(
        wraps=task_manager.update_task_status,
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

    # The success path, stubbed down to the parts this file is about.
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
    # a successful run is the one this file scripts.
    agent._get_git_diff_stat_for_review = lambda *a, **k: ""
    agent._commit_task_changes = MagicMock()
    return agent


def _tasks_json(tmp_path):
    return json.loads(
        (tmp_path / _PLAN_DIR_NAME / "tasks.json").read_text(encoding="utf-8")
    )


def _statuses(mock) -> List[Any]:
    """Every status a spied ``update_task_status`` was handed.

    The call is positional in the agent (``update_task_status(id,
    status)``), but the spy has to survive production changing that
    to a keyword — so both shapes are read.
    """
    seen = []
    for call in mock.call_args_list:
        args, kwargs = call
        if len(args) >= 2:
            seen.append(args[1])
        elif "status" in kwargs:
            seen.append(kwargs["status"])
    return seen


# ---------------------------------------------------------------------------
# The helper: one refusal, one park, then the answer
# ---------------------------------------------------------------------------


def test_recovers_after_one_park(tmp_path, monkeypatch, wait):
    """A 429, one park, and the same attempt succeeds.

    The provider refused once and came back. The helper waited out the
    poll interval, asked again, and got the answer — no failure marker
    was written, and the plan carries no ``stop_reason``.

    The "no failure marker" half of that is asserted here for
    completeness; the load-bearing version of it is
    :func:`test_no_failure_marker_after_recovery`, which drives the
    whole retry loop, because a helper call cannot write a failure
    marker in the first place. The attempt budget is the same story:
    :func:`test_attempt_budget_is_not_consumed` is where "the attempt
    was not spent" is a fact rather than a tautology.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")

    task = _make_task(tmp_path)
    agent = _make_agent(tmp_path, _LogRecorder(), task, _ScriptedTool(["ok"]))
    _install_wait_seams(monkeypatch, agent, wait)

    call = _ScriptedCall([
        ApiError("quota exceeded", status="429"),
        "ok",
    ])

    result = agent._query_with_capacity_wait(call, task, attempt_bound=1)

    assert result == "ok", (
        "the call must be made again after the wait: a provider with no "
        "room says nothing about the answer"
    )
    assert call.calls == 2, (
        "exactly one retry: the refusal, then the answer"
    )
    assert wait.sleeps == [30.0], (
        "one park of exactly one poll interval, and no more"
    )
    assert agent.task_manager.record_task_failure.call_count == 0
    assert _tasks_json(tmp_path).get("stop_reason") is None


def test_one_park_sleeps_exactly_the_poll_interval(
    tmp_path, monkeypatch, wait
):
    """The park is the poll interval — not shorter, not longer.

    The provider is asked again after exactly one poll interval, and
    the fake clock has moved by exactly that much, so the budget the
    helper spends is the budget it says it spent.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")

    task = _make_task(tmp_path)
    agent = _make_agent(tmp_path, _LogRecorder(), task, _ScriptedTool(["ok"]))
    _install_wait_seams(monkeypatch, agent, wait)

    call = _ScriptedCall([
        ApiError("quota exceeded", status="429"),
        "ok",
    ])

    result = agent._query_with_capacity_wait(call, task, attempt_bound=1)

    assert result == "ok"
    assert len(wait.sleeps) == 1, (
        f"the provider came back on the second call — there is no reason "
        f"to park twice. Got: {wait.sleeps!r}"
    )
    assert wait.sleeps[0] == 30.0, (
        f"the park must be exactly the poll interval. Got: {wait.sleeps[0]!r}"
    )
    assert wait.now == 30.0, (
        f"the clock must have moved by exactly what was slept, or the "
        f"budget the helper spends is fiction. Got: {wait.now!r}"
    )


# ---------------------------------------------------------------------------
# The whole retry loop: the markers that could actually appear
# ---------------------------------------------------------------------------


def test_no_failure_marker_after_recovery(
    tmp_path, monkeypatch, wait, child_processes
):
    """A recovered provider leaves the plan exactly as it found it.

    This is the test that makes the "no failure marker" claim mean
    something: it drives the whole retry loop, which is where a marker
    could be written at all. After a 429 and a successful retry:

      * the task was not failed — ``record_task_failure`` was never
        called;
      * no ``failed`` status was ever handed to ``update_task_status``;
      * ``tasks.json`` carries no ``stop_reason`` (the plan did not
        stop — it finished);
      * no ``task_quota_wait_exhausted`` event was written, because the
        wait was never exhausted.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")

    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([
        ApiError("quota exceeded", status="429"),
        "done",
    ])
    agent = _make_agent(tmp_path, logger, task, tool)
    _install_wait_seams(monkeypatch, agent, wait)

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is True, (
        "the task must finish: a provider that had no room for thirty "
        "seconds is not evidence that the work is impossible"
    )
    assert agent.task_manager.record_task_failure.call_count == 0, (
        "a provider with no room must not fail the task — that is what "
        "strands everything downstream of it"
    )
    assert "failed" not in _statuses(agent.task_manager.update_task_status), (
        f"the task must never be handed a 'failed' status. Got: "
        f"{_statuses(agent.task_manager.update_task_status)!r}"
    )
    assert "completed" in _statuses(agent.task_manager.update_task_status), (
        f"the task must be marked completed. Got: "
        f"{_statuses(agent.task_manager.update_task_status)!r}"
    )

    plan = _tasks_json(tmp_path)
    assert plan.get("stop_reason") is None, (
        f"the plan did not stop — it finished. Got: "
        f"{plan.get('stop_reason')!r}"
    )
    assert plan.get("reason_detail") is None
    assert "task_quota_wait_exhausted" not in logger.names(), (
        f"the wait was not exhausted, so no exhaustion marker may be "
        f"written. Got: {logger.names()!r}"
    )
    assert child_processes == [], (
        f"the recovery path spawns nothing — it waits and asks again. "
        f"Got: {child_processes!r}"
    )


def test_attempt_budget_is_not_consumed(
    tmp_path, monkeypatch, wait, child_processes
):
    """Two refusals, two parks, one attempt.

    The provider refused twice and came back. Neither refusal was
    charged to the task's retry budget: ``record_attempt`` was never
    called, the whole thing ran on attempt 1, and the task finished.

    This is the case that used to kill the plan: under the old code a
    429 recorded an attempt, the budget ran out, and the task died
    with everything downstream of it deferred forever.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "1800")

    logger = _LogRecorder()
    task = _make_task(tmp_path)
    tool = _ScriptedTool([
        ApiError("quota exceeded", status="429"),
        ApiError("quota exceeded", status="429"),
        "done",
    ])
    agent = _make_agent(tmp_path, logger, task, tool)
    _install_wait_seams(monkeypatch, agent, wait)

    result = agent._execute_task_with_retry(task, max_retries=2)

    assert result is True
    assert agent.retry_manager.record_attempt.call_count == 0, (
        "a capacity refusal must not be charged to the task's retry "
        "budget — not the first one and not the second"
    )
    assert logger.names().count("task_started") == 1, (
        "the whole thing happened on attempt 1 — the waits did not "
        "advance the attempt counter"
    )
    assert tool.calls == 3, (
        "two refusals and then the answer: three calls, one attempt"
    )
    assert wait.sleeps == [30.0, 30.0], (
        f"one park per refusal, and no more. Got: {wait.sleeps!r}"
    )
    assert agent.task_manager.record_task_failure.call_count == 0
    assert _tasks_json(tmp_path).get("stop_reason") is None
    assert child_processes == [], (
        f"two refusals and two parks spawn nothing. Got: "
        f"{child_processes!r}"
    )
