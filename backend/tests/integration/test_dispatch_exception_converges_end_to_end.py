"""The dispatch loop must CONVERGE on a task that cannot be dispatched.

The accident this reproduces
---------------------------
A task whose ``files_to_modify`` names a DIRECTORY (e.g.
``backend/.venv``) makes ``AutonomousAgent._pre_task_lock_hook`` raise
``RuntimeError("files_to_modify must name files, not directories: ...")``
— a directory is not a lock key and cannot authorise a write.

That raise used to unwind straight past ``_execute_task_with_retry`` —
the only place that records a task verdict — so the task stayed
``pending`` forever and the outer dispatcher re-picked it on every tick.
A plan carrying such a task burned 13.5 hours and made ZERO LLM calls:
the executor never even started, and the plan could never converge,
because nothing ever changed the task's status.

Why this file exists
--------------------
The unit-level tests pin the two halves of the fix in isolation (the
dispatch phase terminalises the task; the zero-progress guard ends a
run that changes nothing).  Neither of them can answer the question an
operator actually asks: *does the REAL loop converge?*

So this file drives the production entry point —
``AutonomousAgent.run`` → ``_run_async`` — over a real plan directory,
with the real ``_load_tasks``, the real ``TaskManager`` and the real
``_pre_task_lock_hook``.  Nothing about the failure is re-implemented
here: the declaration is a real ``files_to_modify`` entry, the
``backend/.venv`` it names is a real directory, and the task reaches its
terminal state through the same code path a production run takes.

The contracts pinned here
-------------------------
  1. ``test_directory_declaration_reaches_terminal_state_without_infinite_loop``
     The accident, end to end.  The task ends ``failed`` with the
     ORIGINAL hook message in ``failure_reason``, the run stops with
     ``stop_reason="failed"``, the executor was called ZERO times, the
     dispatcher logged NO ``task_error``, and the loop dispatched a
     BOUNDED number of layers.  The bound is asserted, not assumed: a
     test that merely waits long enough proves nothing about a loop
     that never ends.
  2. ``test_normal_task_still_runs_once``
     The negative control, and the guard against "fixing" the accident
     by failing everything.  A task that declares a FILE goes through
     the very same loop, the executor runs EXACTLY ONCE, and the task
     completes.

No LLM is involved.  ``_execute_task_with_retry`` is stubbed; in the
accident case the stub is wired to FAIL if it is ever reached, because
the executor must not start for a task whose dispatch cannot be
prepared.  The provider slot is stubbed for the same reason — no
provider is contacted, no quota is consumed.

The waits are zeroed (``PDT_DISPATCH_RETRY_BACKOFF_SECONDS`` /
``PDT_ZERO_PROGRESS_BACKOFF_SECONDS``) so the case measures LOGIC, not
wall-clock, and the run leaves nothing behind: the broker the run
starts is stopped by ``run`` itself, and every subprocess this file
spawns is waited for.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# This file spawns a real ``git`` subprocess and drives the real
# dispatch loop (broker + asyncio + worker thread).  That is what the
# ``integration`` marker means here ("spawns local subprocesses / drives
# the real server, but needs NO model"), and it is where the file
# belongs: the integration lane runs ``-m "integration and not e2e"``
# with a 120-second per-test ceiling, while the unit shards exclude
# the ``integration`` marker.
pytestmark = pytest.mark.integration


#: The prefix of the directory the plan's ``tasks.json`` lives in.  A
#: real plan id (not a pytest tmp dir name) keeps the plan-id
#: derivation on its normal path — ``derive_plan_id_from_tasks_file``
#: refuses generic names, and a refusal would quietly change what the
#: test exercises.
_PLAN_DIR_PREFIX = "plan-under-test"

#: The number of tasks the run is allowed to dispatch.  This is a
#: SAFETY NET, not the assertion: it bounds the loop if convergence
#: regresses, so a broken build fails an assertion below instead of
#: hanging the suite.  The bound the test actually asserts on is the
#: ``layer_started`` count.
_MAX_TASKS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Collects the structured events the agent writes."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str):
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

    def names(self) -> List[str]:
        return [e["event"] for e in self.events]

    def count(self, event: str) -> int:
        return self.names().count(event)


class _RecordingController:
    """Provider-slot stub — no provider is contacted in these tests.

    Mirrors the real :class:`ProviderConcurrencyController` surface the
    dispatcher uses: an awaitable ``acquire(provider)`` and a
    synchronous ``release(provider)``.
    """

    def __init__(self) -> None:
        self.acquired: List[str] = []
        self.released: List[str] = []

    async def acquire(self, provider: str) -> None:
        self.acquired.append(provider)

    def release(self, provider: str) -> None:
        self.released.append(provider)


def _git_init(project_dir: Path) -> None:
    """Init a real git repo so ``GitManager`` can bind to it.

    ``AutonomousAgent.__init__`` constructs a ``GitManager``, which
    refuses to bind to a directory that is not inside a git repository.
    Every subprocess here is waited for, so nothing outlives the test.
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


def _directory_task() -> Dict[str, Any]:
    """A task whose declaration names a DIRECTORY — the accident."""
    return {
        "id": "1",
        "title": "write into the virtualenv directory",
        "description": "declares a directory in files_to_modify",
        "test_command": "true",
        "depends_on": [],
        "provider": "test-provider",
        "files_to_modify": ["backend/.venv"],
    }


def _file_task() -> Dict[str, Any]:
    """A task whose declaration names a FILE — the negative control."""
    return {
        "id": "1",
        "title": "write a real file",
        "description": "declares a file in files_to_modify",
        "test_command": "true",
        "depends_on": [],
        "provider": "test-provider",
        "files_to_modify": ["src/app.py"],
    }


def _write_plan(plan_root: Path, tasks: List[Dict[str, Any]]) -> Path:
    """Write the plan's ``tasks.json`` and return its path.

    The plan lives in its own directory (not in the workspace), so the
    plan-id derivation stays on its normal path and the lock files land
    beside the plan instead of inside somebody's checkout.

    The directory name is UNIQUE per call.  The directory name IS the
    plan id, and the plan id is the key the runtime-status rows are
    stored under: two cases sharing one plan id share one row, and the
    second case would hydrate the first case's verdict.  That is not a
    hypothetical — a fixed name made the negative control read the
    accident's failure.  A unique name makes the cases independent of
    each other AND of whatever state the database already held.
    """
    plan_dir = plan_root / f"{_PLAN_DIR_PREFIX}-{uuid.uuid4().hex[:12]}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            {
                "requirement": "a task that cannot be dispatched must converge",
                "tasks": tasks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return tasks_file


def _build_agent(project_dir: Path, tasks_file: Path, logger: _LogRecorder):
    """Build an ``AutonomousAgent`` bound to the plan and the workspace."""
    from agent import AutonomousAgent
    from config import AgentConfig

    agent = AutonomousAgent(
        requirement="a task that cannot be dispatched must converge",
        project_dir=project_dir,
        coding_tool=None,
        config=AgentConfig(planner_system_prompt="spec", max_retries=1),
        logger=logger,
        tasks_file=tasks_file,
    )
    # No provider is contacted: the slot is a stub.  Everything else in
    # the dispatch path is the real thing.
    agent._provider_controller = _RecordingController()
    return agent


def _task_by_id(agent, task_id: str):
    """The task the run ended with, read from the manager AFTER the run.

    ``_load_tasks`` re-points ``task_manager.tasks`` at freshly parsed
    objects, so a reference captured before the run would read a status
    nobody ever wrote.
    """
    matches = [t for t in agent.task_manager.tasks if t.id == task_id]
    assert matches, (
        f"the plan must still hold task {task_id!r}; got "
        f"{[t.id for t in agent.task_manager.tasks]!r}"
    )
    return matches[0]


def _live_broker_threads() -> List[str]:
    """The lock-broker threads currently running in this process.

    The broker is the only thread the dispatcher starts, and it is
    started for the run's lifetime — so a run that leaves one behind
    would leave a socket nobody serves requests from.
    """
    return [t.name for t in threading.enumerate() if t.name == "pdt-lock-broker"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def _no_backoff(monkeypatch):
    """Zero every wait this test can hit.

    The dispatch retry backoff and the zero-progress backoff are
    documented knobs (``PDT_DISPATCH_RETRY_BACKOFF_SECONDS`` /
    ``PDT_ZERO_PROGRESS_BACKOFF_SECONDS``).  The contracts pinned here
    are about convergence — how many layers, which terminal state — not
    about wall-clock, and a test that waits out a backoff measures the
    backoff.
    """
    monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("PDT_ZERO_PROGRESS_BACKOFF_SECONDS", "0")
    monkeypatch.delenv("PDT_DISPATCH_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("PDT_ZERO_PROGRESS_MAX_LAYERS", raising=False)


# ---------------------------------------------------------------------------
# 1. The accident: a directory declaration must converge, not spin
# ---------------------------------------------------------------------------


def test_directory_declaration_reaches_terminal_state_without_infinite_loop(
    tmp_path,
):
    """A directory declaration ends ``failed`` — in a bounded number of layers.

    Driven through the real dispatch loop, so what is asserted here is
    the behaviour of the production path rather than of a re-implementation
    of it.
    """
    project_dir = tmp_path / "project"
    # The declaration names a directory that REALLY is one: the guard
    # checks the filesystem, so a fake would not reproduce anything.
    (project_dir / "backend" / ".venv").mkdir(parents=True)
    _git_init(project_dir)

    tasks_file = _write_plan(tmp_path, [_directory_task()])

    logger = _LogRecorder()
    agent = _build_agent(project_dir, tasks_file, logger)

    executor_calls: List[str] = []

    def _executor_must_not_run(task, max_retries=5, timeout=None):
        executor_calls.append(task.id)
        raise AssertionError(
            "the executor must never start for a task whose dispatch "
            "cannot be prepared — the failure belongs to the dispatch "
            "phase, before the executor is reached"
        )

    agent._execute_task_with_retry = _executor_must_not_run

    brokers_before = _live_broker_threads()
    agent.run(max_tasks=_MAX_TASKS)

    task = _task_by_id(agent, "1")

    assert executor_calls == [], (
        "the executor must not run at all for a task whose dispatch "
        f"fails; it ran for {executor_calls!r}"
    )
    assert task.status == "failed", (
        "the task must reach a terminal status — the accident was a task "
        f"stuck at 'pending' forever. Got {task.status!r}"
    )
    assert task.failure_reason is not None
    assert "files_to_modify must name files, not directories" in task.failure_reason, (
        "the failure_reason must carry the ORIGINAL hook message, so an "
        "operator can see WHY the task could not be dispatched. Got "
        f"{task.failure_reason!r}"
    )
    assert agent.task_manager.stop_reason == "failed", (
        "the run must say it stopped because a task failed, not because "
        "it finished and not because it gave up. Got "
        f"{agent.task_manager.stop_reason!r}"
    )
    assert logger.count("task_error") == 0, (
        "a dispatch failure is a VERDICT, not an exception that escapes "
        "the task coroutine — the dispatcher must not log it as a "
        f"task_error. Got {logger.names()!r}"
    )
    layers = logger.count("layer_started")
    assert 1 <= layers <= 2, (
        "the loop must converge in a BOUNDED number of layers — the "
        "accident was an unbounded re-dispatch of the same task. "
        f"Got {layers} layer(s)"
    )
    on_disk = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert on_disk.get("stop_reason") == "failed", (
        "the stop reason must be persisted on tasks.json — it is what "
        "every later reader of the plan sees. Got "
        f"{on_disk.get('stop_reason')!r}"
    )
    assert _live_broker_threads() == brokers_before, (
        "the run must leave no lock-broker thread behind — the broker "
        "lives for the run and is stopped by it"
    )
    assert agent._lock_broker is None and not agent._lock_broker_started, (
        "the run must tear the broker down, or the next run adopts a "
        "socket nobody serves requests from"
    )


# ---------------------------------------------------------------------------
# 2. Negative control: an ordinary task still runs exactly once
# ---------------------------------------------------------------------------


def test_normal_task_still_runs_once(tmp_path):
    """A task that declares a FILE still runs once and completes.

    Without this, "the loop converges" could be satisfied by a loop
    that fails everything — the accident would be fixed by refusing to
    dispatch at all.
    """
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "src" / "app.py").write_text(
        "print('hello')\n", encoding="utf-8",
    )
    _git_init(project_dir)

    tasks_file = _write_plan(tmp_path, [_file_task()])

    logger = _LogRecorder()
    agent = _build_agent(project_dir, tasks_file, logger)

    executor_calls: List[str] = []

    def _executor_completes(task, max_retries=5, timeout=None):
        executor_calls.append(task.id)
        # The production verdict path: the task's status moves to
        # ``completed`` and the task leaves the schedulable set.
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    agent._execute_task_with_retry = _executor_completes

    brokers_before = _live_broker_threads()
    agent.run(max_tasks=_MAX_TASKS)

    task = _task_by_id(agent, "1")

    assert executor_calls == ["1"], (
        "an ordinary task must be dispatched EXACTLY once — a second "
        f"dispatch is the re-run loop. Got {executor_calls!r}"
    )
    assert task.status == "completed", (
        f"the task must complete; got {task.status!r}"
    )
    assert agent.task_manager.stop_reason == "success", (
        "a run that completed its work must finish normally, got: "
        f"{agent.task_manager.stop_reason!r}"
    )
    assert logger.count("layer_started") == 1, (
        "one layer for one task — the dispatcher must not dispatch it "
        f"twice. Got {logger.count('layer_started')}"
    )
    assert logger.count("task_error") == 0, (
        f"no task may error out. Got {logger.names()!r}"
    )
    assert _live_broker_threads() == brokers_before, (
        "the run must leave no lock-broker thread behind — the task took "
        "a real file lock through the broker, and the broker lives for "
        "the run"
    )
    assert agent._lock_broker is None and not agent._lock_broker_started, (
        "the run must tear the broker down, or the next run adopts a "
        "socket nobody serves requests from"
    )
