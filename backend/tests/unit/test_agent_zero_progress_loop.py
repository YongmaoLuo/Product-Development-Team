"""The dispatch loop must notice a layer that changed nothing.

The spin this pins down
-----------------------
The dispatcher runs one *layer* at a time and reacts to a finished
layer by dispatching the next one **immediately**.  A layer that ends
with the task list exactly as it started is therefore invisible from
the outside: the loop sees the same tasks, dispatches the same work,
and gets the same nothing back — at the same speed, forever.

That is not a hypothetical.  A task whose dispatch keeps failing
without ever touching its status leaves the plan alive and productive
on paper: the run is "still executing", the log has entries, and not
one line of it says that nothing is happening.  The plan burns hours
and produces zero work.

``backend/progress_guard.py`` (the judgement) is the counter; THIS
file pins the wiring — the loop has to *use* it:

* the two snapshots that bracket a layer are taken at the right
  moments (before the layer runs, after the parent-verdict roll-up);
* a layer that changed nothing is counted, waited out, and — once the
  limit is reached — ends the run with a **named** reason
  (``zero_progress``), so a run that gave up can be told apart from a
  run that finished and from a run that stopped for a human;
* a layer that DID change something resets the counter, so a run that
  is working is allowed an occasional quiet layer.

The contracts pinned here
-------------------------
  1. ``test_zero_progress_layers_stop_with_named_reason``
     The accident.  An executor that changes no state produces quiet
     layers; with the limit at two, the run stops on the second one,
     writes ``stop_reason="zero_progress"`` (in memory AND on disk),
     and records one ``layer_zero_progress`` event per quiet layer.
  2. ``test_completed_task_is_progress_negative_control``
     The negative control, and the guard against "fixing" the spin by
     breaking the happy path: a task that completes is progress, the
     run finishes normally with ``stop_reason="success"``, and the
     guard never fires.

The dispatcher is driven through the real ``_run_async``, so the
assertions are about the loop, not about a re-implementation of it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


#: The directory the plan's ``tasks.json`` lives in.  A real plan id
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

    def messages(self):
        return [e["message"] for e in self.events]

    def data_for(self, event):
        for entry in self.events:
            if entry["event"] == event:
                return entry["data"]
        return None

    def count(self, event):
        return self.names().count(event)


class _RecordingController:
    """Provider-slot stub — no provider is contacted in these tests."""

    def __init__(self) -> None:
        self.acquired = []
        self.released = []

    async def acquire(self, provider: str) -> None:
        self.acquired.append(provider)

    def release(self, provider: str) -> None:
        self.released.append(provider)


def _git_init(project_dir: Path) -> None:
    """Init a real git repo so ``GitManager`` can bind to it.

    ``AutonomousAgent.__init__`` constructs a ``GitManager``, which
    refuses to bind to a directory that is not inside a git repository.
    """
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
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test User"),
    ):
        subprocess.run(
            ["git", "config", key, value],
            cwd=str(project_dir), capture_output=True, text=True, check=False,
        )


def _task(task_id: str) -> dict:
    """A read-only task — it takes no file lock, so the dispatcher
    test never touches the OS lock machinery."""
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "does the work",
        "test_command": "true",
        "depends_on": [],
        "files_to_modify": [],
    }


def _make_agent(tmp_path, logger, tasks):
    """Build an agent bound to a plan directory under ``tmp_path``."""
    from agent import AutonomousAgent
    from config import AgentConfig

    plan_dir = tmp_path / _PLAN_DIR_NAME
    plan_dir.mkdir(parents=True, exist_ok=True)

    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "zero progress spec", "tasks": tasks}, indent=2),
        encoding="utf-8",
    )

    agent = AutonomousAgent(
        requirement="zero progress spec",
        project_dir=tmp_path,
        coding_tool=None,
        config=AgentConfig(planner_system_prompt="spec", max_retries=1),
        logger=logger,
        tasks_file=tasks_file,
    )
    return agent, plan_dir


def _tasks_on_disk(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _hermetic_state_db(tmp_path, monkeypatch):
    """Point ``state.db`` at a per-test file.

    ``plan_id`` is the SQLite key for the runtime-status rows, and the
    default database is shared by every test in the process.  A row
    leaked from another test would hydrate into this one and change
    what the dispatcher sees.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


@pytest.fixture(autouse=True)
def _guard_knobs(monkeypatch):
    """Two quiet layers are enough to stop, and the wait is zero.

    The limit and the backoff are the guard's documented knobs
    (``PDT_ZERO_PROGRESS_MAX_LAYERS`` / ``PDT_ZERO_PROGRESS_BACKOFF_SECONDS``).
    Setting them through the environment is what a deployment does, and
    it keeps the test honest: the loop reads the same knobs a
    production run reads.
    """
    monkeypatch.setenv("PDT_ZERO_PROGRESS_MAX_LAYERS", "2")
    monkeypatch.setenv("PDT_ZERO_PROGRESS_BACKOFF_SECONDS", "0")


# ---------------------------------------------------------------------------
# 1. A layer that changes nothing must stop the run, with a name
# ---------------------------------------------------------------------------


def test_zero_progress_layers_stop_with_named_reason(tmp_path):
    """Two quiet layers → the run stops with ``zero_progress``.

    The executor below changes NOTHING: no status, no commit, no new
    task.  The task list after the layer is the task list before it,
    which is exactly the shape of the spin — and the run has to stop
    rather than dispatch the same work again.
    """
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(tmp_path, logger, [_task("1")])
    agent._provider_controller = _RecordingController()

    executor_calls = []

    def _executor_changes_nothing(task, max_retries=5, timeout=None):
        executor_calls.append(task.id)
        return False

    agent._execute_task_with_retry = _executor_changes_nothing

    # ``max_tasks`` is a safety net only: it bounds the loop if the
    # guard does not fire, so a broken wiring fails the assertion
    # below instead of hanging the suite.
    asyncio.run(agent._run_async(max_tasks=5))

    assert agent.task_manager.stop_reason == "zero_progress", (
        "a run whose layers changed nothing must stop with the named "
        f"reason zero_progress, got: {agent.task_manager.stop_reason!r}"
    )
    assert agent.task_manager.reason_detail, (
        "the named stop reason must carry a detail explaining it"
    )
    assert _tasks_on_disk(plan_dir).get("stop_reason") == "zero_progress", (
        "the named stop reason must be persisted on tasks.json — it is "
        "what every later reader of the plan sees"
    )
    assert logger.count("layer_zero_progress") == 2, (
        "one event per quiet layer — two quiet layers, two events. "
        f"Got: {logger.count('layer_zero_progress')}"
    )
    assert len(executor_calls) == 2, (
        "the run must dispatch exactly two layers and then stop — the "
        f"third dispatch would be the spin. Got {len(executor_calls)}"
    )


# ---------------------------------------------------------------------------
# 2. Below the limit: wait, then carry on
# ---------------------------------------------------------------------------


def test_quiet_layer_below_the_limit_waits_and_continues(tmp_path, monkeypatch):
    """A quiet layer that is NOT the last one is waited out, not fatal.

    The guard has two halves and this is the one that keeps a run
    alive: below the limit the loop waits (longer each time) and
    dispatches again.  Only the limit ends the run — and a run that
    hit the limit here dispatched exactly three layers, not one and
    not thirty.
    """
    monkeypatch.setenv("PDT_ZERO_PROGRESS_MAX_LAYERS", "3")
    monkeypatch.setenv("PDT_ZERO_PROGRESS_BACKOFF_SECONDS", "0.05")

    delays = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)

    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(tmp_path, logger, [_task("1")])
    agent._provider_controller = _RecordingController()

    executor_calls = []

    def _executor_changes_nothing(task, max_retries=5, timeout=None):
        executor_calls.append(task.id)
        return False

    agent._execute_task_with_retry = _executor_changes_nothing

    asyncio.run(agent._run_async(max_tasks=10))

    assert agent.task_manager.stop_reason == "zero_progress", (
        "the limit is three quiet layers, and the run must stop there"
    )
    assert len(executor_calls) == 3, (
        "the run must dispatch one layer per quiet layer and stop at the "
        f"limit — three layers, not {len(executor_calls)}"
    )
    assert delays == [0.05, 0.1], (
        "the run must wait before each retry below the limit, and the "
        f"wait doubles per quiet layer. Got: {delays!r}"
    )


# ---------------------------------------------------------------------------
# 3. Negative control: a layer that DID something must not be punished
# ---------------------------------------------------------------------------


def test_completed_task_is_progress_negative_control(tmp_path):
    """A layer that finishes its task is progress — the guard stays quiet.

    This is the guard against "fixing" the spin by stopping every run
    that takes more than one layer: a completed task leaves the active
    set, the snapshot changes, the counter resets, and the run finishes
    the way it always did — with ``success``.
    """
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(tmp_path, logger, [_task("1")])
    agent._provider_controller = _RecordingController()

    def _executor_completes(task, max_retries=5, timeout=None):
        # The production verdict path: the task's status moves to
        # ``completed`` and the task leaves the schedulable set.
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    agent._execute_task_with_retry = _executor_completes

    asyncio.run(agent._run_async())

    assert agent.task_manager.stop_reason == "success", (
        "a run that completed its task must finish normally, got: "
        f"{agent.task_manager.stop_reason!r}"
    )
    assert logger.count("layer_zero_progress") == 0, (
        "a layer that changed the task state is progress — the zero-"
        f"progress guard must stay quiet. Got: {logger.names()}"
    )
