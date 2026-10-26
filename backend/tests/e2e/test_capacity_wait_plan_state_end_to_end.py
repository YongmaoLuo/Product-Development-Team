"""End to end: a plan parked for quota stays alive, and can be started again.

What this file is
-----------------
The whole capacity-wait chain in ONE process, with every part of it
real: the agent's own retry loop, the real ``TaskManager`` over a real
``tasks.json`` on disk, the real ``PlanState`` and
``ExecutionRepository`` over a real (throwaway) SQLite database, and a
coding tool whose provider refuses every single time.

The only thing faked is the provider, and that is the point: a 429 is
what the whole feature is about, so a provider that answered would
prove nothing about it.  Everything downstream of the refusal is
production code, exercised in the order production exercises it:

    the agent's retry loop
        -> the marker on ``tasks.json``
        -> the disposition helper that reads the marker
        -> the workflow phase (``PlanState``)
        -> the execution status the API layer answers with
            (``ExecutionRepository``)

The claim this file exists to support is one sentence: **a provider
that has no room pauses the plan, it does not kill it.**  A plan in
``waiting_for_quota`` has work left, its tasks are still eligible for
another dispatch, and the operator can start it again when capacity
comes back.

What is deliberately NOT here
-----------------------------
A server, an executor subprocess, or a monitor thread.

This file covers the *state* a parked plan is left in, and the state
it can be started from.  The run that follows the restart belongs to
a different file.  So the exit branch of the executor's watcher is
entered through its own seam — :func:`routes.execution._enter_quota_wait`
— rather than by standing the watcher up, and the restart is the
start endpoint's own CAS rather than an HTTP call.  Nothing is
spawned, so nothing can be left behind: the suite's own reclamation
fixture has nothing to reclaim, and the child-process count asserted
below is the check that says so.

The wait itself is faked, and only in its clock.  The wait's LENGTH
is production's own: one poll interval of thirty seconds, the
provider asked twice, the budget spent exactly.  What the fake clock
replaces is the passage of those thirty seconds, so the case costs the
suite nothing and says the same thing on an idle machine and on a
loaded one.  The wall-clock half of the wait is a different file's
job — see ``test_provider_quota_wake_end_to_end.py``, which sleeps
through a real wake over a real socket.

Where the task statuses are read from — and why
------------------------------------------------
Not from ``tasks.json``.  ``TaskManager.save_tasks`` strips the
per-task runtime fields (``status`` / ``updated_time`` /
``failure_reason`` / ``breakdown_count``) on the way to disk, so the
file has no per-task status to read — the runtime state lives in the
task objects and in SQLite.  The assertions read the task objects
*and* the ``plan_tasks`` rows, so "the downstream work is still
waiting" is a statement about both.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ``server`` must be imported first: it seeds ``sys.modules['server']``
# and then wires the ``routes`` package, and ``routes.execution``
# late-binds back into it.
import server  # noqa: E402
import agent as agent_module  # noqa: E402
import execution_logger  # noqa: E402
from routes import execution as routes_execution  # noqa: E402

from coding_tool import ApiError  # noqa: E402
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402


pytestmark = pytest.mark.e2e


#: The plan under test.  A real plan id (not a pytest tmp dir name)
#: keeps ``TaskManager``'s plan-id derivation and the routing/execution
#: rows on their normal path.
_PLAN_ID = "quota-park-e2e"

#: The provider that refuses.  The name is never assumed by the
#: assertions — they read it back off ``agent.coding_tool`` — but the
#: tool has to carry one, because that is where the agent looks for
#: "who refused" when it writes the marker.
_PROVIDER = "vendor-never-recovers"

#: What the provider says.  A 429 is a capacity answer: the provider
#: is full, not the request broken.
_REFUSAL = "用量上限 2056"

#: The wait's own figures, in the environment the production code
#: reads.  The poll interval is the production default and the budget
#: is set to exactly one poll interval, so the run waits ONCE — for
#: one poll — and then escalates.  A budget larger than the poll would
#: make the case wait longer without making it say anything the
#: one-poll case does not.
_POLL_SEC = 30
_BUDGET_SEC = 30


# ---------------------------------------------------------------------------
# The wait's clock
# ---------------------------------------------------------------------------


class _FakeClock:
    """The wait, and the reading of the wait — both under the test's hand.

    The wait is measured in wall-clock seconds, and a test that has to
    wait for real seconds in order to test a wait is a test nobody
    runs.  Both halves of the measurement are therefore faked, and the
    two halves have to agree: sleeping records what the wait was asked
    for and moves the clock by exactly that much, so "waited thirty
    seconds" is a fact the test can assert rather than a number the
    machine happened to produce.  The budget decision the helper makes
    — "have I waited out my budget?" — is then a function of the
    script and not of how loaded the runner is.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        """What the wait does instead of sleeping: record, then advance.

        The advance is the whole point: the clock and the sleeper are
        two views of the same fake time, so a wait of thirty seconds
        costs the budget exactly thirty seconds.
        """
        self.sleeps.append(float(seconds))
        self.now += float(seconds)

    def clock(self) -> float:
        """What the wait reads instead of the wall clock."""
        return self.now


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Collects the structured events the run writes.

    The agent logs through ``logger.info`` / ``logger.warning`` /
    ``logger.error`` and nothing else, so the double only has to
    answer those five names.

    It is also what the plan's own logger is replaced with (see the
    ``parked_plan`` fixture), so BOTH halves of the "why did this run
    stop" story — the executor's ``task_quota_wait_exhausted`` and the
    watcher-side ``execution_waiting_for_quota`` — land in one place
    that can be read without opening the plan's ``execution.log``.
    """

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
        """The payload of the first event with this name (or ``None``)."""
        for entry in self.events:
            if entry["event"] == event:
                return entry["data"]
        return None

    def all_data_for(self, event):
        """The payloads of EVERY event with this name."""
        return [e["data"] for e in self.events if e["event"] == event]


class _NeverRecoveringTool:
    """A coding tool whose provider refuses forever.

    There is no script to walk off the end of: every call raises the
    same capacity refusal.  That is what makes this the negative
    control — the wait can only end by running out of budget, never by
    the provider coming back.

    ``current_call_provider`` is what the agent reads to say *who*
    refused, so the tool carries it explicitly rather than letting the
    agent invent a name.
    """

    def __init__(self, provider: str = _PROVIDER) -> None:
        self.provider = provider
        self.calls = 0
        self.current_call_provider = provider

    def query(self, *args, **kwargs):
        self.calls += 1
        raise ApiError(_REFUSAL, status="429")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _make_task(task_id: str, depends_on=None, project_dir: Path = None) -> SubTask:
    """One task of the plan under test."""
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description=f"task {task_id}",
        # ``exit 1`` so the pre-flight gate falls through to the real
        # subagent path this file exercises.
        test_command="exit 1",
        status="pending",
        project_dir=str(project_dir) if project_dir else None,
        files_to_modify=["changed.py"],
        model_type="medium",
        depends_on=list(depends_on or []),
    )


def _make_agent(project_dir, logger, tasks, tool, tasks_file):
    """An agent wired with just enough to run the retry loop.

    The plan state is real: a real :class:`TaskManager` over a real
    ``tasks.json`` on disk, so "the task was not failed" is a statement
    about a file and a database, not about a mock.  What is stubbed is
    the work around the call — the git commit, the test command, the
    inline review — none of which is what this file is about.

    The two task-status methods are wrapped, not replaced: the real
    methods still run (the statuses still land in the task objects and
    in SQLite), and the calls become observable, which is the only way
    to say "the task was never handed a ``failed`` status".
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.logger = logger
    agent.project_dir = project_dir
    agent.coding_tool = tool
    agent._task_progress_repo = None
    agent._session_task_completed_counts = {}
    agent._quota_wait_requested = False

    task_manager = TaskManager(project_dir, tasks_file=tasks_file)
    task_manager.tasks = list(tasks)
    task_manager.save_tasks()
    task_manager.record_task_failure = MagicMock(
        wraps=task_manager.record_task_failure,
    )
    task_manager.update_task_status = MagicMock(
        wraps=task_manager.update_task_status,
    )
    agent.task_manager = task_manager

    retry_manager = type("RM", (), {})()
    retry_manager.get_retry_prompt_modifier = lambda *a, **k: ""
    # Counted, not stubbed away: a capacity refusal must not be
    # charged to the task's retry budget.
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

    agent._build_files_to_modify_hint = lambda task: ""
    agent._build_report_hint = lambda task: ""
    agent._build_prior_failure_block = lambda task: ""
    agent._preflight_test_command_skip = lambda *a, **k: False
    agent._looks_like_audit_task = lambda *a, **k: False
    return agent


def _seed_plan_rows(plan_id: str, project_dir: Path, tasks) -> None:
    """Insert the rows a running plan has, through the real repositories.

    The routing row says ``executing`` because that is where a plan
    stands while its executor works, and the park is a move OUT of
    ``executing`` — so seeding it anywhere else would make the park
    take the forced path instead of the legal one.

    The ``plan_tasks`` rows are the plan's task list as the state
    machine holds it.  ``add_task`` writes them as ``pending``, which
    is the status the downstream tasks must still carry after the park
    and the restart.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "executing")
        ExecutionRepository(conn).insert(
            plan_id,
            project_dir=str(project_dir),
            exec_status="running",
            attempt_count=0,
        )
        task_repo = PlanTaskRepository(conn)
        for task in tasks:
            task_repo.add_task(plan_id, {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "depends_on": list(task.depends_on or []),
                "files_to_modify": list(task.files_to_modify or []),
                "model_type": task.model_type,
            })
        conn.commit()
    finally:
        conn.close()


def _status_of(agent, task_id: str) -> str:
    """A task's status, read from the task object.

    NOT from ``tasks.json``: ``save_tasks`` strips the per-task runtime
    fields on the way to disk, so the file carries no per-task status.
    The runtime state lives here and in SQLite.
    """
    for task in agent.task_manager.tasks:
        if task.id == task_id:
            return task.status
    raise AssertionError(f"no task {task_id!r} in the plan")


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


def _db_status(plan_id: str, task_id: str) -> str:
    """A task's status as the state machine holds it."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        row = PlanTaskRepository(conn).get_task(plan_id, task_id)
    finally:
        conn.close()
    assert row is not None, f"no plan_tasks row for {task_id!r}"
    return row.get("status")


def _exec_status(plan_id: str) -> str:
    """The execution status the API layer answers with."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        summary = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    return summary["exec_status"]


def _routing_phase(plan_id: str) -> str:
    """The workflow phase the state machine holds for this plan."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    assert row is not None, f"no plan_routing row for {plan_id!r}"
    return row["current_phase"]


def _tasks_json(tasks_file: Path) -> dict:
    return json.loads(tasks_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The child processes the code under test starts
# ---------------------------------------------------------------------------


@pytest.fixture
def child_processes(monkeypatch):
    """The child processes the code under test starts, counted.

    A quota park is a wait, not a spawn: the run must not leave a
    subprocess behind.  The count is asserted rather than assumed, so
    "nothing was left running" is a fact of this run.
    """
    started: List[Any] = []
    real_popen = subprocess.Popen

    def _counting_popen(*args, **kwargs):
        started.append(args[0] if args else kwargs.get("args"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _counting_popen)
    return started


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@pytest.fixture
def parked_plan(tmp_path, monkeypatch, isolated_plans_dir, child_processes):
    """The whole chain, run for real: a provider that never comes back.

    Three halves, in the order a real run has them:

    1. **The executor half.**  The real
       ``_execute_task_with_retry`` runs task 1 against a coding tool
       whose provider refuses every time.  The wait budget is one poll
       interval, so the run waits once — for exactly that poll — asks
       again, and escalates.  It writes the ``waiting_for_quota``
       marker and leaves the task alive.

    2. **The disposition half.**  The real
       :func:`routes.execution._enter_quota_wait` reads that marker
       back and parks the plan: the workflow phase, the execution
       status row, and the in-memory state the status endpoint reads.

    3. **The log half.**  Both events — the executor's
       ``task_quota_wait_exhausted`` and the disposition's
       ``execution_waiting_for_quota`` — land in one recorder, so the
       "why did this run stop" story can be read without opening the
       plan's own log file.

    Everything is hermetic: a throwaway ``state.db`` and a throwaway
    plans directory, both under the test's ``tmp_path``.  The real
    database and the operator's ``plans/`` tree are never touched.
    """
    from plan_state import PlanState

    plans_dir = Path(isolated_plans_dir)
    plan_dir = plans_dir / _PLAN_ID
    plan_dir.mkdir(parents=True)
    project_dir = (tmp_path / "project").resolve()
    project_dir.mkdir()
    tasks_file = plan_dir / "tasks.json"

    task1 = _make_task("task1", project_dir=project_dir)
    task2 = _make_task("task2", depends_on=["task1"], project_dir=project_dir)
    task3 = _make_task("task3", depends_on=["task1"], project_dir=project_dir)
    tasks = [task1, task2, task3]

    logger = _LogRecorder()
    tool = _NeverRecoveringTool(_PROVIDER)
    agent = _make_agent(
        project_dir, logger, tasks, tool, tasks_file,
    )

    # The state machine's rows — a plan that is running, with its task
    # list in the state machine as well as on disk.
    _seed_plan_rows(_PLAN_ID, project_dir, tasks)

    # The plan's own logger is the recorder too, so the disposition's
    # event is observable without reading ``execution.log`` back.  The
    # helper imports ``get_logger`` INSIDE the function, so patching
    # the module attribute is what takes effect.
    monkeypatch.setattr(
        execution_logger, "get_logger", lambda plan_id=None: logger,
    )

    # --- the wait's clock -------------------------------------------
    fake = _FakeClock()
    monkeypatch.setattr(
        agent_module, "_capacity_wait_sleep", fake.sleep, raising=False,
    )
    monkeypatch.setattr(
        agent_module, "_capacity_wait_clock", fake.clock, raising=False,
    )
    monkeypatch.setattr(
        agent, "_capacity_wait_sleep", fake.sleep, raising=False,
    )
    monkeypatch.setattr(
        agent, "_capacity_wait_clock", fake.clock, raising=False,
    )
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", str(_POLL_SEC))
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", str(_BUDGET_SEC))

    # --- half 1: the executor ---------------------------------------
    executor_result = agent._execute_task_with_retry(task1)

    # --- half 2: the disposition ------------------------------------
    # The in-memory state the status endpoint reads, shaped like the
    # one the watcher keeps for a running plan.
    state = {
        "status": "running",
        "stop_reason": None,
        "ended_at": None,
        "pid": None,
        "project_dir": str(project_dir),
        "logs": deque(maxlen=2000),
        "process": None,
    }
    server._execution_state[_PLAN_ID] = state
    parked = routes_execution._enter_quota_wait(
        _PLAN_ID, plan_dir, state, tasks_file,
    )

    try:
        yield SimpleNamespace(
            agent=agent,
            tool=tool,
            logger=logger,
            clock=fake,
            executor_result=executor_result,
            parked=parked,
            tasks=tasks,
            task1=task1,
            task2=task2,
            task3=task3,
            plan_id=_PLAN_ID,
            plan_dir=plan_dir,
            plans_dir=plans_dir,
            tasks_file=tasks_file,
            project_dir=project_dir,
            state=state,
            child_processes=child_processes,
            plan_state=PlanState,
        )
    finally:
        server._execution_state.pop(_PLAN_ID, None)


@pytest.fixture
def restarted_plan(parked_plan):
    """The parked plan, started again.

    The restart is the start endpoint's own work: the routing CAS that
    moves the plan from a phase the endpoint accepts into ``executing``,
    followed by the phase transition that mirrors it.  The executor
    subprocess the endpoint would spawn afterwards is deliberately NOT
    started — this file is about the state a parked plan can be
    started FROM, and a subprocess would add a thing to clean up
    without adding a claim.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        try:
            marked = RoutingRepository(conn).try_mark_phase(
                parked_plan.plan_id,
                server._EXECUTION_START_SOURCE_PHASES,
                "executing",
            )
        except ConflictError as exc:
            # A conflict is the CAS's own answer, and it is what the
            # endpoint turns into a 409.  It is handed to the test
            # rather than raised here, so the test can say what the
            # conflict was about.
            marked = exc
    finally:
        conn.close()

    # The endpoint's second step: the legacy phase mirror.
    parked_plan.plan_state(parked_plan.plan_dir).transition_to("executing")

    yield SimpleNamespace(**vars(parked_plan), marked=marked)


# ---------------------------------------------------------------------------
# 1. The plan stays alive across the wait
# ---------------------------------------------------------------------------


def test_plan_stays_alive_across_the_wait(parked_plan):
    """The wait ends in a pause, not in a death.

    What has to be true afterwards:

      * the wait really happened — the provider was asked again after
        one poll interval, and the run stopped when the budget was
        gone rather than spinning on a provider that will not answer;
      * the task was never handed a ``failed`` status and its retry
        budget was not spent — a provider with no room says nothing
        about the work, and charging it to the task is what strands
        everything downstream of it;
      * the marker is on the plan, and it says which provider refused;
      * the disposition is "parked" everywhere it is written: the
        workflow phase, the execution status row, and the in-memory
        state the status endpoint reads.
    """
    agent = parked_plan.agent
    marker = json.loads(_tasks_json(parked_plan.tasks_file)["reason_detail"])

    # --- the wait ---------------------------------------------------
    assert parked_plan.tool.calls == 2, (
        f"the provider was asked {parked_plan.tool.calls} time(s); one "
        f"refusal, one wait, one more refusal — the second answer is what "
        f"spends the budget"
    )
    assert parked_plan.clock.sleeps == [float(_POLL_SEC)], (
        f"the run must have waited exactly one poll interval and no more. "
        f"Got: {parked_plan.clock.sleeps!r}"
    )
    assert marker["waited_sec"] == float(_POLL_SEC), (
        f"the marker must record the wait that happened. Got: "
        f"{marker['waited_sec']!r}"
    )

    # --- the task is alive -------------------------------------------
    assert agent.task_manager.record_task_failure.call_count == 0, (
        "a provider with no room must not fail the task — that is what "
        "strands everything downstream of it"
    )
    assert "failed" not in _statuses(agent.task_manager.update_task_status), (
        f"the task must never be handed a 'failed' status. Got: "
        f"{_statuses(agent.task_manager.update_task_status)!r}"
    )
    assert _status_of(agent, "task1") != "failed", (
        f"the task must still be alive. Got: {_status_of(agent, 'task1')!r}"
    )
    assert agent.retry_manager.record_attempt.call_count == 0, (
        "a capacity refusal must not be charged to the task's retry budget"
    )
    assert parked_plan.executor_result is False, (
        "the task did not complete — the run stopped instead. What it "
        "must not do is call that a failure"
    )

    # --- the marker --------------------------------------------------
    plan = _tasks_json(parked_plan.tasks_file)
    assert plan.get("stop_reason") == "waiting_for_quota", (
        f"the plan must carry the quota marker. Got: "
        f"{plan.get('stop_reason')!r}"
    )
    assert marker["providers"] == [parked_plan.tool.current_call_provider], (
        f"the marker must name the provider that refused. Got: "
        f"{marker['providers']!r}"
    )
    assert marker["needs_manual_topup"] is False

    # --- the disposition ---------------------------------------------
    assert parked_plan.parked is True, (
        "the disposition helper must recognise the marker and park the plan"
    )
    assert parked_plan.state["status"] == "waiting_for_quota"
    assert parked_plan.state["stop_reason"] == "waiting_for_quota"
    assert parked_plan.state["ended_at"] is None, (
        "the run is parked, not over — a parked run has no end time"
    )
    assert parked_plan.plan_state(parked_plan.plan_dir).get_current_phase() == (
        "waiting_for_quota"
    ), (
        "the workflow phase must be waiting_for_quota, not failed"
    )
    assert _exec_status(parked_plan.plan_id) == "waiting_for_quota", (
        "the execution status row must say waiting_for_quota — 'failed' "
        "there is what tells a restart the run died"
    )
    assert parked_plan.child_processes == [], (
        f"a quota park spawns nothing — it waits and stops. Got: "
        f"{parked_plan.child_processes!r}"
    )


# ---------------------------------------------------------------------------
# 2. The waiting phase can be restarted
# ---------------------------------------------------------------------------


def test_waiting_phase_can_be_restarted(restarted_plan):
    """A parked plan is a plan the operator can start again.

    Three things have to hold, and they are the difference between a
    pause and a one-way door:

      * the start endpoint's own vocabulary accepts the parked phase,
        so the CAS that guards a start does not answer "conflict";
      * the CAS itself succeeds against the parked plan, and the
        workflow phase follows it into ``executing``;
      * a backend restart restores the plan as PARKED, not as failed —
        the recovery path probes the PID only for ``running`` rows, so
        a parked plan comes back parked.
    """
    assert "waiting_for_quota" in server._EXECUTION_START_SOURCE_PHASES, (
        "the start endpoint's source phases must accept a parked plan — "
        "without it, a provider outage ends the plan for good"
    )

    assert restarted_plan.marked is True, (
        f"the start endpoint's CAS must accept a plan parked for quota. "
        f"Got: {restarted_plan.marked!r}"
    )
    assert _routing_phase(restarted_plan.plan_id) == "executing", (
        "the parked plan must move into executing when it is started again"
    )
    assert restarted_plan.plan_state(
        restarted_plan.plan_dir,
    ).get_current_phase() == "executing", (
        "the workflow phase must follow the start"
    )

    # --- a backend restart -------------------------------------------
    # The recovery path is what a real restart runs.  It is driven
    # here against a cleared in-memory state, which is what a fresh
    # process has.
    server._execution_state.pop(restarted_plan.plan_id, None)
    routes_execution._recover_execution_states(restarted_plan.plans_dir)

    restored = server._execution_state.get(restarted_plan.plan_id)
    assert restored is not None, (
        "the restart must find the parked plan at all — a plan the "
        "recovery path cannot see is a plan the operator has lost"
    )
    assert restored["status"] == "waiting_for_quota", (
        f"a parked plan must come back parked. Got: {restored['status']!r}"
    )
    assert _exec_status(restarted_plan.plan_id) == "waiting_for_quota", (
        "the restart must not rewrite a parked plan's status — 'failed' "
        "there is what tells the operator the work is lost"
    )


# ---------------------------------------------------------------------------
# 3. The downstream tasks stay pending across the restart
# ---------------------------------------------------------------------------


def test_downstream_pending_after_restart(restarted_plan):
    """The work that depends on the blocked task is still waiting.

    This is the point of the whole disposition.  The downstream tasks
    are not "blocked by a failed upstream" — they are waiting for a
    provider to have room, which is a different thing entirely, and
    the disposition has to keep them apart.

    So the downstream tasks stay ``pending`` (still eligible for
    another dispatch), in the task objects AND in the state machine's
    own task rows, and the restart does not touch them either.
    """
    agent = restarted_plan.agent

    for task_id in ("task2", "task3"):
        assert _status_of(agent, task_id) == "pending", (
            f"{task_id} must stay pending — it is waiting for capacity, "
            f"not for a failure. Got: {_status_of(agent, task_id)!r}"
        )
        assert _db_status(restarted_plan.plan_id, task_id) == "pending", (
            f"{task_id} must stay pending in the state machine too. Got: "
            f"{_db_status(restarted_plan.plan_id, task_id)!r}"
        )

    assert _db_status(restarted_plan.plan_id, "task1") != "failed", (
        "the task that ran out of room must not be failed either — it is "
        "waiting for capacity, not for a failure"
    )
    assert "failed" not in _statuses(agent.task_manager.update_task_status), (
        f"no task may be failed on a quota park. Got: "
        f"{_statuses(agent.task_manager.update_task_status)!r}"
    )
    assert "skipped" not in _statuses(agent.task_manager.update_task_status), (
        f"no task may be skipped on a quota park — a skip throws the work "
        f"away. Got: {_statuses(agent.task_manager.update_task_status)!r}"
    )
    assert _exec_status(restarted_plan.plan_id) != "failed", (
        "the plan must not be failed: without the quota disposition the "
        "watcher's next branch writes 'failed' over the whole plan"
    )


# ---------------------------------------------------------------------------
# 4. The events are logged
# ---------------------------------------------------------------------------


def test_events_are_logged(parked_plan):
    """Both halves of "why did this run stop" are written down.

    Two audiences, two carriers, and they have to agree:

      * the executor's ``task_quota_wait_exhausted`` — the run's own
        account of the wait it gave up on;
      * the disposition's ``execution_waiting_for_quota`` — the park
        itself, written where the operator (and the watchdog) read.

    Both carry the SAME detail as the marker on the plan, so the three
    can never disagree about why the run stopped.  They are read from
    the recorder, not from the plan's ``execution.log``: the point is
    the event, not the file it happens to be written to.
    """
    names = parked_plan.logger.names()
    marker = json.loads(_tasks_json(parked_plan.tasks_file)["reason_detail"])

    assert "task_quota_wait_exhausted" in names, (
        f"the executor must log why it stopped. Got: {names!r}"
    )
    assert "execution_waiting_for_quota" in names, (
        f"the disposition must log the park. Got: {names!r}"
    )

    exhausted = parked_plan.logger.data_for("task_quota_wait_exhausted")
    assert exhausted["providers"] == [parked_plan.tool.current_call_provider], (
        f"the executor's log must name the provider that refused. Got: "
        f"{exhausted!r}"
    )
    assert exhausted["reason_detail"] == _tasks_json(
        parked_plan.tasks_file,
    )["reason_detail"], (
        "the executor's log and the plan's marker must carry the same "
        "detail, so the two never disagree about why the run stopped"
    )

    waiting = parked_plan.logger.data_for("execution_waiting_for_quota")
    assert waiting["providers"] == marker["providers"], (
        f"the logged event must carry the marker's providers. Got: "
        f"{waiting!r}"
    )
    assert waiting["needs_manual_topup"] == marker["needs_manual_topup"]
    assert waiting["waited_sec"] == marker["waited_sec"], (
        f"the logged event must carry the marker's wait. Got: {waiting!r}"
    )
    assert waiting["at"] == marker["at"], (
        f"the logged event must carry the marker's timestamp, so the whole "
        f"'why did this run stop' story ends up in one place. Got: "
        f"{waiting!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
