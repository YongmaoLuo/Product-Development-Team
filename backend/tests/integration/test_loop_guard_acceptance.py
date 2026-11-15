"""End-to-end acceptance for the three dispatch-loop fixes.

What is being accepted
----------------------
Tasks 1-9 landed three fixes, each of them pinned by unit tests that
isolate one half of it:

* **task 1 / 2 / 3 — the logs are bounded.**  The plan directory's log
  writers rotate: the live log is moved aside before it would grow past
  ``MAX_PLAN_LOG_BYTES``, and the copies are numbered ``<name>.1`` …
  ``<name>.N`` with the **highest number holding the oldest lines**.
  The executor subprocess's output goes through that writer instead of
  straight into a bare file — which is what grew one ``repair_*.log``
  past 5.4 GB.
* **task 5 — a dispatch failure is terminal.**  A task whose
  ``files_to_modify`` names a directory makes the pre-task lock hook
  raise.  The raise used to unwind past the executor — the only place
  that records a verdict — so the task stayed ``pending`` and the outer
  dispatcher re-picked it forever: 13.5 hours, zero LLM calls.
* **task 7 — a run that changes nothing stops.**  The dispatch loop
  reacts to a finished layer by dispatching the next one immediately,
  so a layer that ends with the task list exactly as it started is
  invisible from the outside.  The zero-progress guard counts those
  layers and eventually says stop, with a named reason.

Why this file exists
--------------------
A unit test that isolates one half cannot answer the question an
operator asks after an incident: *does the REAL thing converge, is the
REAL log bounded, and do the readers that already existed still read
it?*  So this file drives the production entry points — the real
dispatch loop (``AutonomousAgent.run``) and the real executor spawn
(``routes.execution._spawn_executor_subprocess``) — over real
directories, with real subprocesses.

The three cases
---------------
  A. ``test_incident_task_converges_with_bounded_log``
     The accident, end to end.  A task that cannot be dispatched ends
     ``failed`` with the ORIGINAL hook message, in a BOUNDED number of
     layers, with the executor called ZERO times — and the log of the
     run that spun stays inside the bound.
  B. ``test_normal_run_is_unchanged``
     The negative control for all three fixes at once.  A task that
     declares a file runs its executor EXACTLY once, completes, and
     the run ends ``success`` — with no dispatch-retry event and no
     zero-progress event.
  C. ``test_existing_readers_still_read_rotated_logs``
     After a rotation the readers that already existed still read the
     logs: the tail reader reaches the child's LAST line, and the
     reader that looks for rotated copies finds them under the names it
     expects (``.3``, ``.2``, ``.1``, oldest first) and still finds a
     binding that was rotated out.

No LLM is contacted.  The executor BODY is stubbed in the two dispatch
cases (no provider is asked for anything); the log cases use a real
child process, because the bound is a property of the SPAWN — a fake
would prove that the test wrote the file, not that the spawn did.

Every wait this file can hit is zeroed, every subprocess is waited
for, and the lock broker the run starts is checked for afterwards — the
run must leave no thread behind.
"""

from __future__ import annotations

import json
import shlex
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


import bounded_log  # noqa: E402
import pause_state  # noqa: E402
from execution_log_tail import read_tail_lines  # noqa: E402

# ``server`` is imported BEFORE ``routes.execution`` on purpose: the route
# module late-binds into the application module, and ``server`` seeds
# ``sys.modules['server']`` before importing it.
import server  # noqa: E402
from routes import execution as routes_execution  # noqa: E402


# This file spawns a real ``git`` subprocess, a real executor subprocess
# and drives the real dispatch loop (broker + asyncio + worker thread).
# That is what the ``integration`` marker means here ("spawns local
# subprocesses / drives the real server, but needs NO model"), and it is
# where the file belongs: the integration lane runs
# ``-m "integration and not e2e"`` with a 120-second per-test ceiling,
# while the unit shards exclude the ``integration`` marker.
pytestmark = pytest.mark.integration


#: The prefix of the directory the plan's ``tasks.json`` lives in.  A
#: real plan id (not a pytest tmp dir name) keeps the plan-id derivation
#: on its normal path — ``derive_plan_id_from_tasks_file`` refuses
#: generic names, and a refusal would quietly change what the test
#: exercises.
_PLAN_DIR_PREFIX = "plan-under-test"

#: The plan id the executor spawns use.  It only has to be a legal plan
#: id — the log path is given explicitly, so the plan directory is never
#: read for the log.
PLAN_ID = "p-loop-guard-acceptance"

#: The bound the log cases shrink the writer to.  Small enough that a
#: few hundred lines of output cannot fit in one file, small enough
#: that a mistake in the bound shows up as a size and not as a slow
#: test.
TEST_MAX_BYTES = 4096

#: How many rotated copies the writer keeps — the production value.  The
#: reader in :mod:`pause_state` reads exactly that many, so the two
#: numbers have to agree.
EXPECTED_BACKUP_COUNT = 3

#: The number of tasks the run is allowed to dispatch.  A SAFETY NET,
#: not the assertion: it bounds the loop if convergence regresses, so a
#: broken build fails an assertion below instead of hanging the suite.
_MAX_TASKS = 3

#: The last line the stand-in executor writes.  A reader can tell where
#: the output ends.
LOG_MARKER = "the tail MARKER"

#: The session id the rotating writer binds.  It is written FIRST on
#: purpose: it has to end up in a ROTATED copy, because that is what
#: the reader that looks for rotated copies has to find.
SESSION_ID = "session-under-test"

#: A child that binds a session, writes filler, and ends with a marker.
#:
#: The filler is 100 bytes a line, so a 4096-byte file holds 40 of them
#: and 152 lines force exactly three rotations — which puts the binding
#: (the first line) into ``.3`` and the marker into the live log.
WRITER_ROTATING = """
import json
import sys

sys.stdout.write(json.dumps({
    "event": "task_session_bound",
    "task_id": "1",
    "data": {"session_id": "%(session_id)s"},
}) + "\\n")
filler = "y" * 99 + "\\n"
for _ in range(150):
    sys.stdout.write(filler)
sys.stdout.write("%(marker)s\\n")
sys.stdout.flush()
"""


# ---------------------------------------------------------------------------
# Helpers — the agent side
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


def _task(task_id: str, files: List[str]) -> Dict[str, Any]:
    """A task that declares ``files`` in ``files_to_modify``."""
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "does the work",
        "test_command": "true",
        "depends_on": [],
        "provider": "test-provider",
        "files_to_modify": list(files),
    }


def _write_plan(plan_root: Path, tasks: List[Dict[str, Any]]) -> Path:
    """Write the plan's ``tasks.json`` and return its path.

    The directory name is UNIQUE per call.  The directory name IS the
    plan id, and the plan id is the key the runtime-status rows are
    stored under: two cases sharing one plan id share one row, and the
    second case would hydrate the first case's verdict.  A unique name
    makes the cases independent of each other AND of whatever state the
    database already held.
    """
    plan_dir = plan_root / f"{_PLAN_DIR_PREFIX}-{uuid.uuid4().hex[:12]}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            {
                "requirement": "the dispatch loop must converge",
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
        requirement="the dispatch loop must converge",
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


def _log_files(log_path: Path) -> List[Path]:
    """The live log and every rotated copy of it."""
    return sorted(log_path.parent.glob(f"{log_path.name}*"))


def _sizes(log_path: Path) -> Dict[str, int]:
    """The size of every file of the log family, by name."""
    return {path.name: path.stat().st_size for path in _log_files(log_path)}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_state_db(tmp_path, monkeypatch):
    """Point ``state.db`` at a per-test file.

    ``plan_id`` is the SQLite key for the runtime-status rows, and the
    default database is shared by every test in the process.  A row
    leaked from another test would hydrate into this one and change what
    the dispatcher sees.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Zero every wait this test can hit.

    The dispatch retry backoff and the zero-progress backoff are
    documented knobs (``PDT_DISPATCH_RETRY_BACKOFF_SECONDS`` /
    ``PDT_ZERO_PROGRESS_BACKOFF_SECONDS``).  The contracts pinned here
    are about convergence and about the log — how many layers, which
    terminal state, how big a file — not about wall-clock, and a test
    that waits out a backoff measures the backoff.
    """
    monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("PDT_ZERO_PROGRESS_BACKOFF_SECONDS", "0")
    monkeypatch.delenv("PDT_DISPATCH_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("PDT_ZERO_PROGRESS_MAX_LAYERS", raising=False)


# ---------------------------------------------------------------------------
# A. The accident: the loop converges and the log stays bounded
# ---------------------------------------------------------------------------


def test_incident_task_converges_with_bounded_log(tmp_path, monkeypatch):
    """A task that cannot be dispatched ends ``failed`` — and the log is bounded.

    Two halves, because they were one incident:

    * the loop part — the task ends ``failed`` with the ORIGINAL hook
      message, the executor is never called, and the dispatcher runs a
      BOUNDED number of layers.  The bound is asserted, not assumed: a
      test that merely waits long enough proves nothing about a loop
      that never ends.
    * the log part — the executor's output goes through the bounded,
      rotating writer, so the file the accident grew past 5.4 GB cannot
      grow past the bound any more.  The child is a real process and
      the spawn is the production one.
    """
    from agent import DISPATCH_FAILURE_PREFIX

    project_dir = tmp_path / "project"
    # The declaration names a directory that REALLY is one: the guard
    # checks the filesystem, so a fake would not reproduce anything.
    (project_dir / "backend" / ".venv").mkdir(parents=True)
    _git_init(project_dir)

    plan_root = tmp_path / "plans"
    tasks_file = _write_plan(plan_root, [_task("1", ["backend/.venv"])])
    plan_dir = tasks_file.parent

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

    # -- the loop part --
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
    assert task.failure_reason.startswith(DISPATCH_FAILURE_PREFIX), (
        "the failure_reason must say the task died in the DISPATCH phase, "
        "before the executor ever ran. Got "
        f"{task.failure_reason!r}"
    )
    layers = logger.count("layer_started")
    assert 1 <= layers <= 2, (
        "the loop must converge in a BOUNDED number of layers — the "
        "accident was an unbounded re-dispatch of the same task. "
        f"Got {layers} layer(s)"
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
    assert _live_broker_threads() == brokers_before, (
        "the run must leave no lock-broker thread behind — the broker "
        "lives for the run and is stopped by it"
    )
    assert agent._lock_broker is None and not agent._lock_broker_started, (
        "the run must tear the broker down, or the next run adopts a "
        "socket nobody serves requests from"
    )

    # -- the log part --
    #
    # The log of THIS plan — the one the accident grew.  The executor
    # is replaced by a child through the documented ``PDT_EXECUTOR_CMD``
    # port, so the SPAWN is the production one.
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", TEST_MAX_BYTES)

    log_path = plan_dir / "logs" / "executor.log"
    script = _writer_script(
        tmp_path,
        WRITER_ROTATING % {"session_id": SESSION_ID, "marker": LOG_MARKER},
    )

    process, returned_log = _spawn(
        tmp_path, monkeypatch, script=script, log_path=log_path,
    )
    assert returned_log == log_path, (
        "the spawn must hand back the log path it was given"
    )
    try:
        assert process.wait() == 0, "the stand-in executor must exit cleanly"
    finally:
        # The child is waited for either way: a test that leaves a
        # subprocess behind leaves a writer nobody joined.
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()
            process.wait()

    sizes = _sizes(log_path)
    assert sizes, f"the executor wrote no log at all: {sizes!r}"
    for name, size in sizes.items():
        assert size <= TEST_MAX_BYTES, (
            f"the log file {name} is {size} bytes — the bound is "
            f"{TEST_MAX_BYTES} and every file of the family must be "
            "inside it"
        )
    assert any(name.endswith(".1") for name in sizes), (
        "the executor wrote more than the bound and did NOT rotate — "
        f"the log family is {sizes!r}"
    )


# ---------------------------------------------------------------------------
# B. The normal path is unchanged
# ---------------------------------------------------------------------------


def test_normal_run_is_unchanged(tmp_path):
    """A task that declares a FILE runs once, completes, and the run succeeds.

    Without this, "the loop converges" could be satisfied by a loop that
    fails everything — the accident would be fixed by refusing to
    dispatch at all.  This is the negative control for all three fixes:
    the retry machinery must stay out of the way, the zero-progress
    guard must not fire on a productive layer, and the run must end the
    way it always ended.
    """
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "src" / "app.py").write_text(
        "print('hello')\n", encoding="utf-8",
    )
    _git_init(project_dir)

    plan_root = tmp_path / "plans"
    tasks_file = _write_plan(plan_root, [_task("1", ["src/app.py"])])

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
        "an ordinary task must run its executor EXACTLY once — a second "
        f"run is the re-dispatch loop. Got {executor_calls!r}"
    )
    assert task.status == "completed", (
        f"the task must complete; got {task.status!r}"
    )
    assert task.failure_reason is None, (
        "a completed task carries no failure reason — the dispatch "
        f"failure branch must not have touched it: {task.failure_reason!r}"
    )
    assert agent.task_manager.stop_reason == "success", (
        "a run that completed its work must finish normally, got: "
        f"{agent.task_manager.stop_reason!r}"
    )
    assert logger.count("task_dispatch_retry") == 0, (
        "a task that dispatches cleanly must not be retried — the retry "
        f"event must stay silent. Got {logger.names()!r}"
    )
    assert logger.count("task_dispatch_failed") == 0, (
        "a task that dispatches cleanly must not be failed — the terminal "
        f"dispatch-failure event must stay silent. Got {logger.names()!r}"
    )
    assert logger.count("layer_zero_progress") == 0, (
        "a layer that completed a task changed the plan — the "
        "zero-progress guard must stay quiet. Got "
        f"{logger.names()!r}"
    )
    assert logger.count("zero_progress_backoff") == 0, (
        "a layer that completed a task must not make the loop wait — "
        f"the backoff must stay silent. Got {logger.names()!r}"
    )
    assert logger.count("execution_stopped") == 0, (
        "a run that finished its work must not be stopped by the guard. "
        f"Got {logger.names()!r}"
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


# ---------------------------------------------------------------------------
# C. The readers that already existed still read the rotated logs
# ---------------------------------------------------------------------------


def test_existing_readers_still_read_rotated_logs(tmp_path, monkeypatch):
    """After a rotation the old readers still read the log.

    Three readers, three claims:

    * ``read_tail_lines`` — the disk fallback of the status endpoint —
      still reaches the child's LAST line.  A reader that stops short
      is a reader that lost the output.
    * ``pause_state._rotated_log_paths`` — the reader that looks for the
      rotated copies — finds them under the names it expects
      (``.3``, ``.2``, ``.1``, oldest first), which is the layout the
      writer produces.
    * ``pause_state.collect_task_sessions`` — the reader that a resume
      depends on — still finds a binding that was rotated OUT of the
      live log.  A binding that is not found looks exactly like a task
      that was never bound, and the resume starts a task from scratch.

    And the bound holds on every file of the family, live and rotated
    alike.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", TEST_MAX_BYTES)

    log_path = tmp_path / "logs" / "execution.log"
    script = _writer_script(
        tmp_path,
        WRITER_ROTATING % {"session_id": SESSION_ID, "marker": LOG_MARKER},
    )

    process, returned_log = _spawn(
        tmp_path, monkeypatch, script=script, log_path=log_path,
    )
    assert returned_log == log_path, (
        "the spawn must hand back the log path it was given"
    )
    try:
        assert process.wait() == 0, "the stand-in executor must exit cleanly"
    finally:
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()
            process.wait()

    # -- the bound holds on every file --
    sizes = _sizes(log_path)
    assert sizes, f"the executor wrote no log at all: {sizes!r}"
    for name, size in sizes.items():
        assert size <= TEST_MAX_BYTES, (
            f"the log file {name} is {size} bytes — the bound is "
            f"{TEST_MAX_BYTES} and every file of the family must be "
            "inside it"
        )
    assert log_path.name in sizes, (
        f"the live log is missing: {sizes!r}"
    )
    assert any(name.endswith(".1") for name in sizes), (
        "the child wrote more than the bound and the log did NOT rotate — "
        f"the log family is {sizes!r}"
    )

    # -- the tail reader reaches the last line --
    lines = read_tail_lines(log_path)
    assert lines, "the reader found nothing in a log the child wrote to"
    assert lines[-1].endswith(LOG_MARKER), (
        "the reader must reach the child's LAST line — a reader that "
        f"stops short is a reader that lost the output. Got: {lines[-3:]!r}"
    )

    # -- the rotated copies are where the reader expects them --
    rotated = pause_state._rotated_log_paths(log_path)
    assert rotated == [
        log_path.parent / f"{log_path.name}.3",
        log_path.parent / f"{log_path.name}.2",
        log_path.parent / f"{log_path.name}.1",
    ], (
        "the reader must look for the rotated copies under the names the "
        "writer produces — the HIGHEST number holds the OLDEST lines, so "
        f"the order is descending. Got {rotated!r}"
    )
    assert len(rotated) == EXPECTED_BACKUP_COUNT, (
        "the reader must look for as many copies as the writer keeps — "
        f"{EXPECTED_BACKUP_COUNT}. Got {rotated!r}"
    )

    # -- the binding that was rotated out is still found --
    rotated_text = "".join(
        path.read_text(encoding="utf-8")
        for path in rotated
        if path.exists()
    )
    assert SESSION_ID in rotated_text, (
        "the rotated copies must hold the lines that were rotated out of "
        "the live log — the binding is written first, so it can only be "
        f"in a copy. The copies hold: {rotated_text[-400:]!r}"
    )

    bindings = pause_state.collect_task_sessions(log_path)
    assert bindings.get("1") == SESSION_ID, (
        "the resume reader must still find the binding that was rotated "
        "out of the live log — a binding that is not found looks exactly "
        f"like a task that was never bound. Got {bindings!r}"
    )
