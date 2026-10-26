"""Negative control: a provider that never comes back parks the plan.

2026-10-24 — the import path's negative control
-----------------------------------------------
The positive control next door
(``test_capacity_wait_positive_control.py``) proves the happy half: a
provider that refuses once and comes back keeps the task alive.  This
file proves the other half, and it is the half that decides whether a
quota park is a *pause* or a *death*: a provider that refuses FOREVER.

The contract, in one sentence: **a provider that never recovers must
not fail the task, and must not take the plan down with it.**  The run
parks (``waiting_for_quota``), every task stays eligible for another
dispatch, and the downstream tasks — the ones that depend on the
blocked one — stay ``pending`` instead of being written off.

Why this needed its own file
----------------------------
The unit suite can prove that a *marker* is written; it cannot prove
that the marker is written by the code path a real run takes.  What is
exercised here is the whole chain, end to end:

  * the REAL ``AutonomousAgent._execute_task_with_retry`` — the retry
    loop a run actually uses — against a coding tool whose provider
    refuses every single time;
  * the REAL ``TaskManager`` over a REAL ``tasks.json`` on disk and a
    throwaway ``state.db``, so "the task was not failed" is a
    statement about a file and a database, not about a mock;
  * the REAL server-side disposition, driven through the watcher body
    a start actually runs, so "no task was skipped and no verification
    was started" is a statement about the branch that WOULD have done
    those things.

The "would have" is the load-bearing half.  Without the quota marker
the watcher's next branch writes ``failed`` over the whole plan — so a
parked plan that still reads ``failed`` is not a cosmetic difference,
it is the plan dead.  That is why the disposition is driven through the
watcher rather than called directly: calling ``_enter_quota_wait`` on
its own would make "no verification was started" a tautology.

Two things this file refuses to do
----------------------------------
It does not sleep.  The wait budget is ``0`` — the first refusal
escalates — and the seam that would sleep is replaced by a recorder, so
"the run did not wait" is an assertion rather than a hope.

It does not leave anything running.  The child processes the code under
test starts are counted and asserted to be none, and the watcher thread
is handed back to the suite's own reclamation fixture.

Where the task statuses are read from — and why
------------------------------------------------
Not from ``tasks.json``.  ``TaskManager.save_tasks`` strips the
per-task runtime fields (``status`` / ``updated_time`` /
``failure_reason`` / ``breakdown_count``) on the way to disk, so the
file has no per-task status to read — the runtime state lives in the
task objects and in SQLite.  The assertions below read the task
objects.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
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
from routes import execution as routes_execution  # noqa: E402

from coding_tool import ApiError  # noqa: E402
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402


pytestmark = pytest.mark.integration


#: The plan under test.  A real plan id (not a pytest tmp dir name)
#: keeps ``TaskManager``'s plan-id derivation and the routing/execution
#: rows on their normal path.
_PLAN_ID = "2026-10-24-quota-park"

#: The provider that refuses.  The name is never assumed by the
#: assertions — they read it back off ``agent.coding_tool`` — but the
#: tool has to carry one, because that is where the agent looks for
#: "who refused" when it writes the marker.
_PROVIDER = "vendor-never-recovers"

#: What the provider says.  A 429 is a capacity answer: the provider
#: is full, not the request broken.
_REFUSAL = "用量上限 2056"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Collects the structured events the agent writes.

    The same shape as the recorder the provider-rotation unit suite
    uses (``tests/unit/test_provider_quota_rotation.py``): the agent
    logs through ``logger.info`` / ``logger.warning`` / ``logger.error``
    and nothing else, so the double only has to answer those five
    names.
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


class _QuotaWaitEventSpy:
    """Records the server-side ``waiting_for_quota`` events.

    Wraps ``routes.execution._quota_wait_log`` and calls through, so
    the event still lands in the plan's own log exactly as it would in
    production — the spy only makes it observable without reading that
    file back.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.events: List[dict] = []

    def __call__(self, plan_id, event, message, data=None):
        self.events.append({
            "plan_id": plan_id,
            "event": event,
            "message": message,
            "data": data,
        })
        return self._real(plan_id, event, message, data)

    def names(self):
        return [e["event"] for e in self.events]

    def data_for(self, event):
        for entry in self.events:
            if entry["event"] == event:
                return entry["data"]
        return None


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


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle.

    The watcher waits on it and reads its return code; the executor
    itself is never started (the agent half of this test already ran
    in-process).
    """

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, *args, **kwargs):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


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


def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _seed_plan(
    db_path: Path,
    plan_id: str,
    project_dir: Path,
    phase: str = "queued",
) -> None:
    """Insert the ``plan_routing`` / ``plan_execution`` rows a start needs.

    2026-10-26 (merge): the seeded phase is ``queued``, not ``ready``.
    ``ready`` is the legacy alias of ``queued`` (R10 / ``phase_vocabulary``):
    the write path no longer produces it, the startup migration rewrites the
    rows that still carry it, and the execution-start CAS refuses it on
    purpose (a legacy ``ready`` row must get 409, not a silent start — see
    ``tests/unit/test_execution_start_phases.py``).  Seeding ``queued`` is
    what the migration would have done before the plan reached this test;
    the capacity-wait behaviour under test is unchanged.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, phase, 0, "[]",
                "{}", "{}",
                json.dumps({
                    "status": "pending",
                    "round": 0,
                    "max_rounds": 3,
                    "stop_reason": None,
                }),
                "2026-10-24T00:00:00", "2026-10-24T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(project_dir), None, json.dumps({"tasks": {}}),
                None, "{}", "{}", None, "not_started", None,
                "2026-10-24T00:00:00Z",
            ),
        )
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


def _exec_status(db_path: Path, plan_id: str) -> str:
    """The execution status the API layer answers with."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(db_path)
    try:
        summary = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    return summary["exec_status"]


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
def parked_run(tmp_path, monkeypatch, child_processes):
    """The whole chain, run for real: a provider that never comes back.

    Two halves, in the order a real run has them:

    1. **The executor half.**  The real
       ``_execute_task_with_retry`` runs task 1 against a coding tool
       whose provider refuses every time.  The wait budget is ``0``,
       so the first refusal escalates and the run writes the
       ``waiting_for_quota`` marker.  Nothing is waited for and no
       attempt is spent.

    2. **The watcher half.**  The real start/disposition path runs over
       the marker the first half produced: the watcher body is handed
       over (rather than run in a thread) and called synchronously, so
       the assertions cannot race it.  This is the half that makes
       "no task was skipped" and "no verification was started"
       load-bearing — the branches that do those things are right
       there, one ``if`` away.

    Everything is hermetic: a throwaway ``state.db``, a throwaway
    plans directory, and a fake executor process.  The real database
    and the operator's ``plans/`` tree are never touched.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / _PLAN_ID
    plan_dir.mkdir(parents=True)
    project_dir = (tmp_path / "project").resolve()
    project_dir.mkdir()
    tasks_file = plan_dir / "tasks.json"

    task1 = _make_task("task1", project_dir=project_dir)
    task2 = _make_task("task2", depends_on=["task1"], project_dir=project_dir)
    task3 = _make_task("task3", depends_on=["task1"], project_dir=project_dir)

    logger = _LogRecorder()
    tool = _NeverRecoveringTool(_PROVIDER)
    agent = _make_agent(
        project_dir, logger, [task1, task2, task3], tool, tasks_file,
    )

    # The state machine's own database — a throwaway one.
    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, _PLAN_ID, project_dir, phase="queued")
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)

    # --- half 1: the executor ------------------------------------------
    #
    # ``PDT_CAPACITY_WAIT_MAX_SEC=0`` means "do not wait, escalate now"
    # (see ``capacity_wait.capacity_wait_budget_sec``).  The sleep seam
    # is a recorder, so "the run never waited" is asserted rather than
    # assumed.
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", "30")
    sleeps: List[float] = []
    monkeypatch.setattr(
        agent,
        "_capacity_wait_sleep",
        lambda seconds: sleeps.append(float(seconds)),
        raising=False,
    )

    executor_result = agent._execute_task_with_retry(task1)

    # --- half 2: the watcher -------------------------------------------
    skipped_calls: List[Any] = []
    verification_calls: List[Any] = []
    monkeypatch.setattr(
        server,
        "_mark_task_skipped",
        lambda *a, **k: skipped_calls.append((a, k)),
    )
    monkeypatch.setattr(
        server,
        "_run_auto_verification_loop",
        lambda *a, **k: verification_calls.append((a, k)),
    )
    monkeypatch.setattr(server, "_reject_if_placeholder_prd", lambda *a, **k: None)
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan", lambda *a, **k: "active",
    )
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (_FakeProcess(), tmp_path / "executor.log"),
    )

    spy = _QuotaWaitEventSpy(routes_execution._quota_wait_log)
    monkeypatch.setattr(routes_execution, "_quota_wait_log", spy)

    # Hand the watcher body over instead of letting a daemon thread run
    # it: the assertions below must not race the thread.
    captured: List[Any] = []
    monkeypatch.setattr(
        server, "_run_in_plan_ctx", lambda plan_id_arg, fn: captured.append(fn),
    )

    server._execution_state.pop(_PLAN_ID, None)
    try:
        server.start_execution(
            _PLAN_ID,
            server.StartExecutionRequest(project_dir=str(project_dir)),
        )
        deadline = time.monotonic() + 10.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured, "the execution watcher never handed over"
        captured[0]()
        state = dict(server._execution_state[_PLAN_ID])
    finally:
        server._execution_state.pop(_PLAN_ID, None)

    return SimpleNamespace(
        agent=agent,
        tool=tool,
        logger=logger,
        sleeps=sleeps,
        executor_result=executor_result,
        tasks=(task1, task2, task3),
        plan_id=_PLAN_ID,
        plan_dir=plan_dir,
        tasks_file=tasks_file,
        project_dir=project_dir,
        db_path=db_path,
        state=state,
        spy=spy,
        skipped_calls=skipped_calls,
        verification_calls=verification_calls,
        child_processes=child_processes,
    )


# ---------------------------------------------------------------------------
# 1. The task is not failed
# ---------------------------------------------------------------------------


def test_never_recovering_provider_does_not_fail_the_task(parked_run):
    """A provider that never comes back must not fail the task.

    This is the assertion the whole import path exists for.  On
    2026-09-22 a provider's token-plan cap produced exactly this
    situation, and task 14-2 was marked ``failed`` on its very first
    429 — which permanently deferred 14-3 and 14-5 and stranded the
    run.

    What has to be true afterwards:

      * the task was never handed a ``failed`` status;
      * ``record_task_failure`` was never called;
      * the task's retry budget was not spent either — a capacity
        refusal is not the task's fault, so it must not be charged to
        the task;
      * the provider was asked exactly once: with a zero wait budget
        the first refusal escalates, so there is no second call to
        make and nothing to wait for.
    """
    agent = parked_run.agent

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
        "a capacity refusal must not be charged to the task's retry "
        "budget"
    )
    assert parked_run.tool.calls == 1, (
        f"the wait budget is zero, so the first refusal escalates: one "
        f"call, no second ask. Got: {parked_run.tool.calls!r}"
    )
    assert parked_run.sleeps == [], (
        f"the run must not have waited for real. Got: {parked_run.sleeps!r}"
    )
    assert parked_run.executor_result is False, (
        "the task did not complete — the run stopped instead. What it "
        "must not do is call that a failure"
    )


# ---------------------------------------------------------------------------
# 2. The plan parks
# ---------------------------------------------------------------------------


def test_plan_enters_waiting_for_quota(parked_run):
    """The plan parks in ``waiting_for_quota`` — everywhere it is written.

    Three places carry the disposition and all three have to agree:
    the plan file's top-level marker, the workflow phase, and the
    execution status row the API layer answers with.

    ``waiting_for_quota`` is a *pause*: the run is alive but parked,
    which is why ``ended_at`` stays ``None`` and why the status is not
    ``failed`` (a restart probes the PID for ``running`` rows and would
    report a quota pause as a crash).
    """
    from plan_state import PlanState

    plan = _tasks_json(parked_run.tasks_file)
    assert plan.get("stop_reason") == "waiting_for_quota", (
        f"the plan must carry the quota marker. Got: "
        f"{plan.get('stop_reason')!r}"
    )
    assert plan.get("reason_detail"), (
        "the marker must say why the run stopped"
    )

    assert PlanState(parked_run.plan_dir).get_current_phase() == (
        "waiting_for_quota"
    ), (
        "the workflow phase must be waiting_for_quota, not failed"
    )

    assert _exec_status(parked_run.db_path, parked_run.plan_id) == (
        "waiting_for_quota"
    ), (
        "the execution status row must say waiting_for_quota — 'failed' "
        "there is what tells a restart the run died"
    )

    state = parked_run.state
    assert state["status"] == "waiting_for_quota"
    assert state["stop_reason"] == "waiting_for_quota"
    assert state["ended_at"] is None, (
        "the run is parked, not over — a parked run has no end time"
    )


# ---------------------------------------------------------------------------
# 3. The downstream tasks stay pending
# ---------------------------------------------------------------------------


def test_downstream_tasks_stay_pending(parked_run):
    """The tasks that depend on the blocked one are untouched.

    This is the whole point of the marker.  The downstream tasks are
    not "blocked by a failed upstream" — they are waiting for a
    provider to have room, which is a different thing entirely, and
    the disposition has to keep them apart.

    So the downstream tasks stay ``pending`` (still eligible for
    another dispatch) and ``_mark_task_skipped`` is never called: a
    skip would throw their work away, and the partial-completion
    branch would then auto-verify the plan as if it were done.
    """
    agent = parked_run.agent

    for task_id in ("task2", "task3"):
        assert _status_of(agent, task_id) == "pending", (
            f"{task_id} must stay pending — it is waiting for capacity, "
            f"not for a failure. Got: {_status_of(agent, task_id)!r}"
        )

    assert parked_run.skipped_calls == [], (
        f"no task may be skipped on a quota park — a skip throws the "
        f"work away. Got: {parked_run.skipped_calls!r}"
    )

    assert _exec_status(parked_run.db_path, parked_run.plan_id) != "failed", (
        "the plan must not be failed: without the quota disposition the "
        "watcher's next branch writes 'failed' over the whole plan"
    )


# ---------------------------------------------------------------------------
# 4. The marker and the log
# ---------------------------------------------------------------------------


def test_marker_and_log_are_present(parked_run):
    """The run says why it stopped — in the marker and in the log.

    Two audiences, two carriers.  The marker (``reason_detail``) is
    what the next reader of the plan sees; the log is where the run's
    own history is.  The marker's provider list is read back off the
    coding tool, so the assertion follows the code rather than a name
    this file happens to have chosen.
    """
    marker = json.loads(_tasks_json(parked_run.tasks_file)["reason_detail"])

    assert marker["providers"] == [parked_run.tool.current_call_provider], (
        f"the marker must name the provider that refused. Got: "
        f"{marker['providers']!r}"
    )
    assert marker["needs_manual_topup"] is False
    # ``waited_sec`` is the elapsed time the wait MEASURED, so it is not
    # literally zero — it is the few microseconds between the refusal
    # and the budget check.  What matters is that it is nowhere near a
    # poll interval: the run escalated on the first refusal instead of
    # parking for thirty seconds and asking again.
    assert 0.0 <= marker["waited_sec"] < 1.0, (
        f"the run must not have waited a poll interval. Got: "
        f"{marker['waited_sec']!r}"
    )

    assert "task_quota_wait_exhausted" in parked_run.logger.names(), (
        f"the executor must log why it stopped. Got: "
        f"{parked_run.logger.names()!r}"
    )
    exhausted = parked_run.logger.data_for("task_quota_wait_exhausted")
    assert exhausted["providers"] == [parked_run.tool.current_call_provider], (
        f"the executor's log must name the provider that refused. Got: "
        f"{exhausted!r}"
    )
    assert exhausted["reason_detail"] == _tasks_json(
        parked_run.tasks_file,
    )["reason_detail"], (
        "the executor's log and the plan's marker must carry the same "
        "detail, so the two never disagree about why the run stopped"
    )
    assert "execution_waiting_for_quota" in parked_run.spy.names(), (
        f"the watcher must log the park. Got: {parked_run.spy.names()!r}"
    )
    logged = parked_run.spy.data_for("execution_waiting_for_quota")
    assert logged["providers"] == [parked_run.tool.current_call_provider], (
        f"the logged event must carry the marker's providers. Got: "
        f"{logged!r}"
    )
    assert logged["needs_manual_topup"] is False
    assert logged["waited_sec"] == marker["waited_sec"], (
        f"the logged event must carry the marker's wait. Got: {logged!r}"
    )
    assert logged["at"] == marker["at"], (
        f"the logged event must carry the marker's timestamp, so the "
        f"whole 'why did this run stop' story ends up in one place. "
        f"Got: {logged!r}"
    )


# ---------------------------------------------------------------------------
# 5. No verification
# ---------------------------------------------------------------------------


def test_no_verification_is_started(parked_run):
    """A parked plan is not verified.

    The auto-verification loop verifies the plan as if it were done.
    On a quota park it is not done — the work is waiting for capacity
    — so verifying it would either fail the round or, worse, ask a
    provider that has no room to judge the plan.

    The loop is counted rather than stubbed away, so "it did not run"
    is a fact of this run.
    """
    assert parked_run.verification_calls == [], (
        f"no verification may be started on a quota park. Got: "
        f"{parked_run.verification_calls!r}"
    )
    assert parked_run.child_processes == [], (
        f"a quota park spawns nothing — it waits and stops. Got: "
        f"{parked_run.child_processes!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
