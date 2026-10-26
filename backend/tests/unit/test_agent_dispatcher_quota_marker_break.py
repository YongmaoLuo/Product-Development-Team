"""A quota-wait marker must stop the dispatcher — and must not lock the next run out.

2026-10-24 — the wait that never ended
---------------------------------------
Task 4 made the agent write a ``waiting_for_quota`` marker onto
``tasks.json`` when every provider had no room for the whole wait
budget. The marker says "this run stopped because the provider was out
of budget", and it deliberately does NOT fail the task: the task stays
eligible for another dispatch.

The dispatcher ignored it. Two consequences, both bad:

  1. **The run did not stop.** The marker was written mid-layer; the
     dispatcher went on to the next layer and re-dispatched the very
     task that had just run out of provider room — burning another
     wait budget on the same full provider.
  2. **The NEXT run was locked out forever.** The marker lives on
     ``tasks.json``, and ``TaskManager`` reads it back when the agent
     is constructed. A later run would see
     ``stop_reason == "waiting_for_quota"`` and — once the dispatcher
     honours the marker — stop before doing anything at all. One
     unlucky afternoon with a full provider would have frozen the plan
     permanently.

These tests pin both halves:

  * the dispatcher STOPS when it sees the marker (the in-memory flag
    or the persisted ``stop_reason``), and the stop changes no task's
    status — nothing is failed, nothing is skipped;
  * a stale marker from an earlier run is CLEARED at the start of the
    next run, and a non-quota ``stop_reason`` is left alone.

The dispatcher is driven through the real ``_run_async`` so the
assertions are about the loop, not about a re-implementation of it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


#: The directory the plan's ``tasks.json`` lives in. A real plan id
#: (not a pytest tmp dir name) keeps the plan-id derivation on its
#: normal path.
_PLAN_DIR_NAME = "plan-under-test"


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

    def count(self, event):
        return self.names().count(event)

    def index_of(self, event):
        for index, entry in enumerate(self.events):
            if entry["event"] == event:
                return index
        return -1


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=False,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir), capture_output=True, text=True, check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )


def _upstream_task():
    return {
        "id": "1",
        "title": "upstream task",
        "description": "does the upstream work",
        "test_command": "true",
        "depends_on": [],
        # A read-only task declares no files, so it takes no file lock
        # and the dispatcher test never touches the OS lock machinery.
        "files_to_modify": [],
    }


def _downstream_task():
    return {
        "id": "2",
        "title": "downstream task",
        "description": "does the downstream work",
        "test_command": "true",
        "depends_on": ["1"],
        "files_to_modify": [],
    }


def _stub_dispatch(agent):
    """Make a dispatched task a no-op that completes itself.

    The subject of this suite is the dispatcher's DECISION to stop, not
    what a task does once it runs. The stub completes the task so the
    loop moves on by itself instead of re-scheduling the same task
    forever — and the stub's call count is what proves whether a task
    was dispatched at all.
    """
    def _execute(task, max_retries=None, timeout=None):
        task.status = "completed"
        return True

    agent._execute_task_with_retry = MagicMock(side_effect=_execute)
    return agent._execute_task_with_retry


def _make_agent(tmp_path, logger, tasks, stop_reason=None, reason_detail=None):
    """Build an agent bound to a plan directory under ``tmp_path``."""
    from agent import AutonomousAgent
    from config import AgentConfig

    plan_dir = tmp_path / _PLAN_DIR_NAME
    plan_dir.mkdir(parents=True, exist_ok=True)

    payload = {"requirement": "quota marker spec", "tasks": tasks}
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
        payload["reason_detail"] = reason_detail
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    agent = AutonomousAgent(
        requirement="quota marker spec",
        project_dir=tmp_path,
        coding_tool=None,
        config=AgentConfig(planner_system_prompt="spec", max_retries=1),
        logger=logger,
        tasks_file=tasks_file,
    )
    return agent, plan_dir


def _tasks_on_disk(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The dispatcher stops when it sees the marker
# ---------------------------------------------------------------------------


def test_quota_wait_flag_breaks_dispatcher(tmp_path, monkeypatch):
    """The in-memory flag stops the loop before anything is dispatched."""
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, _plan_dir = _make_agent(
        tmp_path, logger, [_upstream_task(), _downstream_task()],
    )
    execute = _stub_dispatch(agent)

    agent._quota_wait_requested = True

    # ``max_tasks=1`` only bounds the loop: the quota stop is announced
    # with ``reason="waiting_for_quota"``, while the max-tasks stop
    # carries ``tasks_executed`` — the two are told apart by the data.
    asyncio.run(agent._run_async(max_tasks=1))

    stopped = logger.data_for("execution_stopped")
    assert stopped is not None, (
        "the dispatcher must announce that it stopped — a silent stop "
        "looks like a hung run"
    )
    assert stopped.get("reason") == "waiting_for_quota", (
        "the stop reason must say WHY the run stopped: the provider had "
        f"no room. Got: {stopped!r}"
    )
    assert execute.call_count == 0, (
        "no task may be dispatched once the quota marker has been seen — "
        "re-dispatching is what burned a second wait budget"
    )


def test_downstream_tasks_stay_pending(tmp_path, monkeypatch):
    """The break must not touch any task's status.

    A quota wait says the provider had no room. It says nothing about
    the work, so nothing may be failed or skipped on the way out.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, _plan_dir = _make_agent(
        tmp_path, logger, [_upstream_task(), _downstream_task()],
    )
    execute = _stub_dispatch(agent)

    agent.task_manager.record_task_failure = MagicMock(
        wraps=agent.task_manager.record_task_failure,
    )
    agent._quota_wait_requested = True

    asyncio.run(agent._run_async(max_tasks=1))

    tasks = {t.id: t for t in agent.task_manager.tasks}
    assert execute.call_count == 0, (
        "the dispatcher must stop before it dispatches anything"
    )
    assert tasks["1"].status == "pending", (
        "the upstream task was never dispatched, so it is still pending"
    )
    assert tasks["2"].status == "pending", (
        "the downstream task must stay pending — a quota wait is not a "
        f"failure. Got: {tasks['2'].status!r}"
    )
    assert tasks["2"].status not in ("skipped", "failed")
    assert agent.task_manager.record_task_failure.call_count == 0, (
        "the break must not fail anything"
    )


def test_persisted_marker_alone_requests_the_stop(tmp_path, monkeypatch):
    """The persisted marker is a stop signal in its own right.

    The in-memory flag is per-agent; the ``stop_reason`` on
    ``tasks.json`` outlives the process. Either one is enough.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, _plan_dir = _make_agent(tmp_path, logger, [_upstream_task()])

    agent.task_manager.stop_reason = "waiting_for_quota"

    assert agent._stop_requested_for_quota_wait() is True


# ---------------------------------------------------------------------------
# The stale marker must not lock the next run out
# ---------------------------------------------------------------------------


def test_stale_marker_cleared_at_run_start(tmp_path, monkeypatch):
    """A marker left by an earlier run is cleared before the run starts.

    Otherwise one afternoon with a full provider would freeze the plan
    for good: every later run would read the marker back and stop
    before doing anything.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(
        tmp_path, logger, [_upstream_task()],
        stop_reason="waiting_for_quota",
        reason_detail='{"providers": ["vendor-a"]}',
    )

    # The agent read the marker back from disk when it was constructed.
    assert agent.task_manager.stop_reason == "waiting_for_quota"

    # ``max_tasks=0`` stops the loop before anything is dispatched — the
    # point here is what the run's START did with the marker.
    asyncio.run(agent._run_async(max_tasks=0))

    assert agent.task_manager.stop_reason is None, (
        "the stale marker must be cleared in memory too"
    )
    assert agent.task_manager.reason_detail is None
    on_disk = _tasks_on_disk(plan_dir)
    assert on_disk["stop_reason"] is None, (
        "and on disk — otherwise the next run reads it back"
    )
    assert logger.count("quota_wait_marker_cleared") == 1, (
        "the clear must be visible in the execution log"
    )
    assert logger.index_of("quota_wait_marker_cleared") < logger.index_of(
        "execution_started"
    ), (
        "the clear happens at the start of the run, before it announces "
        "itself — otherwise the log reads as if the run started already "
        "locked out"
    )
    stopped = logger.data_for("execution_stopped") or {}
    assert stopped.get("reason") != "waiting_for_quota", (
        "the run must NOT be stopped by a marker it just cleared"
    )


def test_success_reason_is_not_cleared(tmp_path, monkeypatch):
    """Only a quota marker is a stale lock-out.

    ``success`` is a real answer about the plan ("the work is done").
    Clearing it would rewrite history: the plan would look unfinished
    to the next reader.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(
        tmp_path, logger, [_upstream_task()],
        stop_reason="success",
        reason_detail="all done",
    )

    asyncio.run(agent._run_async(max_tasks=0))

    assert agent.task_manager.stop_reason == "success", (
        "a 'success' marker is not a stale quota marker — it must be "
        "left exactly as it was"
    )
    assert logger.count("quota_wait_marker_cleared") == 0
    assert _tasks_on_disk(plan_dir)["stop_reason"] == "success"


# ---------------------------------------------------------------------------
# An agent without the flag must not blow up
# ---------------------------------------------------------------------------


def test_flag_missing_on_legacy_agent_does_not_raise(tmp_path):
    """An agent built by ``__new__`` has no ``_quota_wait_requested``.

    The check is called on the dispatcher's hot path, and an agent
    restored from a checkpoint written before the flag existed must
    not raise AttributeError just because the attribute is missing.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)

    assert agent._stop_requested_for_quota_wait() is False

    # And with a task manager that carries no marker, still False.
    agent.task_manager = type(
        "TM", (), {"stop_reason": None, "reason_detail": None},
    )()
    assert agent._stop_requested_for_quota_wait() is False
