"""The happy path must still look happy.

The three fixes this file is the control for
---------------------------------------------
* **task 5** — a dispatch-phase exception is retried a bounded number of
  times and then terminalised (``task_dispatch_failed``).
* **task 7** — a layer that changes nothing ends the run with
  ``stop_reason="zero_progress"``.
* **task 2** — the executor's log is bounded and rotating.

Every one of them can break a run that used to work, and none of them
can be checked by the test that covers the failure: a test that asserts
"the failure is retried" is just as happy whether the normal case still
runs or not.  So this file is the NEGATIVE CONTROL for all three — the
case that has to behave exactly as it did before the fixes landed.

The contracts pinned here
-------------------------
  1. ``test_normal_task_completes_once``
     A task that declares a file which EXISTS is dispatched ONCE, ends
     ``completed``, and the run finishes with ``stop_reason="success"``.
     The dispatch phase is not retried — no ``task_dispatch_retry`` and
     no ``task_dispatch_failed`` is logged — and the executor body runs
     exactly once, which is the attempt count the old semantics had.
  2. ``test_normal_small_log_does_not_rotate``
     A run whose output fits in one file leaves ONE file: no ``.1`` copy
     appears next to the live log, and the reader that has always read
     these logs (``read_tail_lines``, the disk fallback of the status
     endpoint) still reads the child's last line.

The dispatch is driven through the real ``_run_async`` and the real
pre-task lock hook, so the assertions are about the loop as it runs and
not about a re-implementation of it.  Only the executor BODY is stubbed:
no LLM is contacted, and no provider is asked for anything.

The log case uses a real child process and the production spawn helper,
because the bound is a property of the SPAWN: a fake would prove that
the test wrote the file, not that the spawn did.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ``server`` is imported BEFORE ``routes.execution`` on purpose: the route
# module late-binds into the application module, and ``server`` seeds
# ``sys.modules['server']`` before importing it.
import server  # noqa: E402
from routes import execution as routes_execution  # noqa: E402
from execution_log_tail import read_tail_lines  # noqa: E402


#: The directory the plan's ``tasks.json`` lives in.  A real plan id
#: (not a pytest tmp dir name) keeps the plan-id derivation on its
#: normal path.
_PLAN_DIR_NAME = "plan-under-test"

#: The plan id every spawn in this file uses.  It only has to be a legal
#: plan id — the log path is given explicitly, so the plan directory is
#: never read.
PLAN_ID = "p-normal-negative-control"

#: The last line the stand-in executor writes.  A reader can tell where
#: the output ends.
LOG_MARKER = "the tail MARKER"

#: The body of the stand-in executor: ONE line, which is what a normal
#: run's log looks like.  The marker comes from :data:`LOG_MARKER`, so the
#: line the child writes and the line the reader must find are the same
#: string by construction.
WRITER_ONE_LINE = f"""
import sys

sys.stdout.write("{LOG_MARKER}\\n")
sys.stdout.flush()
"""


# ---------------------------------------------------------------------------
# Helpers — the agent side
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


def _task(task_id: str, files: list[str]) -> dict:
    """A task that declares real FILES — the normal case.

    The declaration names files that exist in the project directory, so
    the pre-task lock hook is on its ordinary path: it takes the locks
    and returns, instead of raising and sending the task down the
    dispatch-failure branch.
    """
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "does the work",
        "test_command": "true",
        "depends_on": [],
        "files_to_modify": list(files),
    }


def _make_agent(tmp_path, logger, tasks):
    """Build an agent bound to a plan directory under ``tmp_path``."""
    from agent import AutonomousAgent
    from config import AgentConfig

    plan_dir = tmp_path / _PLAN_DIR_NAME
    plan_dir.mkdir(parents=True, exist_ok=True)

    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "normal path spec", "tasks": tasks}, indent=2),
        encoding="utf-8",
    )

    agent = AutonomousAgent(
        requirement="normal path spec",
        project_dir=tmp_path,
        coding_tool=None,
        config=AgentConfig(planner_system_prompt="spec", max_retries=1),
        logger=logger,
        tasks_file=tasks_file,
    )
    return agent, plan_dir


@pytest.fixture(autouse=True)
def _hermetic_state_db(tmp_path, monkeypatch):
    """Point ``state.db`` at a per-test file.

    ``plan_id`` is the SQLite key for the runtime-status rows, and the
    default database is shared by every test in the process.  A row
    leaked from another test would hydrate into this one and change what
    the dispatcher sees.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


# ---------------------------------------------------------------------------
# Helpers — the log side
# ---------------------------------------------------------------------------


def _writer_script(tmp_path: Path, body: str) -> Path:
    """Write ``body`` to a python script and return its path."""
    script = tmp_path / "writer.py"
    script.write_text(body, encoding="utf-8")
    return script


def _command_for(script: Path) -> str:
    """The command line that runs ``script`` — quoted for :func:`shlex.split`."""
    return " ".join(shlex.quote(part) for part in (sys.executable, str(script)))


def _spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    script: Path,
    log_path: Path,
):
    """Spawn the executor through the production helper.

    The executor is replaced by ``script`` through the documented
    ``PDT_EXECUTOR_CMD`` port, so the *spawn* is the production one —
    only the body of the child changes.
    """
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "worktree"
    project_dir.mkdir(exist_ok=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    monkeypatch.setenv("PDT_EXECUTOR_CMD", _command_for(script))
    # The plan directory is never read (the log path is given), but the
    # spawn resolves it — point it at this test's scratch directory so
    # nothing outside the tmp dir is touched.
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")

    return routes_execution._spawn_executor_subprocess(
        plan_id=PLAN_ID,
        backend_dir=backend_dir,
        project_dir=project_dir,
        tasks_file=tasks_file,
        tool=None,
        extra_args=None,
        log_path=log_path,
    )


# ---------------------------------------------------------------------------
# 1. The normal task: dispatched once, completed, run finishes normally
# ---------------------------------------------------------------------------


def test_normal_task_completes_once(tmp_path):
    """A task that declares an existing file runs ONCE and completes.

    This is the negative control for the dispatch-failure work: the
    retry machinery that now exists around the pre-task lock hook must
    stay out of the way when the hook does its job.  A task whose
    declaration is fine is dispatched once, its executor body runs once,
    and the run ends the way it always ended — ``success``.
    """
    _git_init(tmp_path)

    # The declared file EXISTS: the declaration is usable, so the
    # pre-task lock hook has nothing to complain about.
    declared = tmp_path / "src" / "app.py"
    declared.parent.mkdir(parents=True, exist_ok=True)
    declared.write_text("# the file this task declares\n", encoding="utf-8")

    logger = _LogRecorder()
    agent, _plan_dir = _make_agent(tmp_path, logger, [_task("1", ["src/app.py"])])
    agent._provider_controller = _RecordingController()

    executor_calls = []
    dispatch_attempts = []

    real_pre_task_lock_hook = agent._pre_task_lock_hook

    def _counting_pre_task_lock_hook(task):
        # The dispatch phase is the pre-task lock hook; counting its
        # calls IS the attempt count the retry wrapper would have
        # counted, seen from the outside.
        dispatch_attempts.append(task.id)
        return real_pre_task_lock_hook(task)

    agent._pre_task_lock_hook = _counting_pre_task_lock_hook

    def _executor_completes(task, max_retries=5, timeout=None):
        # The production verdict path: the task's status moves to
        # ``completed`` and the task leaves the schedulable set.
        executor_calls.append(task.id)
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    agent._execute_task_with_retry = _executor_completes

    asyncio.run(agent._run_async())

    assert executor_calls == ["1"], (
        "a normal task must run its executor exactly once — the dispatch "
        f"retry machinery must not fire on the happy path. Got {executor_calls!r}"
    )
    assert dispatch_attempts == ["1"], (
        "the dispatch phase must be attempted exactly once — a second "
        "attempt would be a retry, and a retry is what the failure path "
        f"does. Got {dispatch_attempts!r}"
    )

    task = next(t for t in agent.task_manager.tasks if t.id == "1")
    assert task.status == "completed", (
        f"the task must end completed, got {task.status!r}"
    )
    assert task.failure_reason is None, (
        "a completed task carries no failure reason — the dispatch "
        f"failure branch must not have touched it: {task.failure_reason!r}"
    )

    assert agent.task_manager.stop_reason == "success", (
        "a run that completed its task must finish normally, got: "
        f"{agent.task_manager.stop_reason!r}"
    )

    assert logger.count("task_dispatch_retry") == 0, (
        "a task that dispatches cleanly must not be retried — the retry "
        f"event must stay silent. Got: {logger.names()}"
    )
    assert logger.count("task_dispatch_failed") == 0, (
        "a task that dispatches cleanly must not be failed — the terminal "
        f"dispatch-failure event must stay silent. Got: {logger.names()}"
    )
    assert logger.count("layer_zero_progress") == 0, (
        "a layer that completed a task changed the plan — the zero-progress "
        f"guard must stay quiet. Got: {logger.names()}"
    )


# ---------------------------------------------------------------------------
# 2. The normal log: one file, and the reader still reads it
# ---------------------------------------------------------------------------


def test_normal_small_log_does_not_rotate(monkeypatch, tmp_path):
    """A small log stays ONE file, and the existing reader still reads it.

    The bound is invisible when it does not bind: a run that writes one
    line leaves the live log and nothing else, so a reader that finds a
    ``.1`` next to it knows something rotated that had no reason to.
    And the reader that has always read these logs — ``read_tail_lines``,
    the disk fallback of the status endpoint — must still read the last
    line the child wrote.
    """
    log_path = tmp_path / "logs" / "executor.log"
    script = _writer_script(tmp_path, WRITER_ONE_LINE)

    process, returned_log = _spawn(
        tmp_path, monkeypatch, script=script, log_path=log_path
    )
    assert returned_log == log_path, (
        "the spawn must hand back the log path it was given"
    )
    assert process.wait() == 0, "the stand-in executor must exit cleanly"

    assert log_path.exists(), "the live log is missing"
    assert not Path(str(log_path) + ".1").exists(), (
        "a log that fits in one file must not rotate — a ``.1`` next to it "
        "is a visible side effect of a bound that did not bind"
    )
    assert sorted(log_path.parent.glob(f"{log_path.name}*")) == [log_path], (
        "a small run must leave exactly one file next to the live log — "
        f"got {sorted(p.name for p in log_path.parent.glob(f'{log_path.name}*'))!r}"
    )

    lines = read_tail_lines(log_path)
    assert lines, "the reader found nothing in a log the child wrote to"
    assert lines[-1].endswith(LOG_MARKER), (
        "the reader must reach the child's LAST line — a reader that stops "
        f"short is a reader that lost the output. Got: {lines!r}"
    )