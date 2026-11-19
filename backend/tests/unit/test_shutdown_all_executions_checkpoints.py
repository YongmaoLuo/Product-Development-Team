"""The shutdown leaves a checkpoint behind for every plan it interrupts.

Background
----------
``server._shutdown_all_executions`` is the one place the server goes
through on its way out: the lifespan ``finally`` block and the ``atexit``
hook both call it.  It killed the subprocesses and walked away.  Nothing
on disk said *which* plan had been in flight, so a restart found a plan
directory that looked exactly like one that had never run.

``backend/shutdown_checkpoint.py`` knows how to write that checkpoint.
This file covers the *caller*: that the server calls the writer, that it
calls it **before** the first SIGTERM, and that a writer which fails does
not cost the shutdown its kills.

What these tests pin
--------------------
* **order** -- the checkpoint file is already on disk by the time
  ``_kill_process_tree`` runs.  A kill that landed first would leave a
  checkpoint describing work that had already been torn down.
* **coverage** -- every in-flight execution *and* verification gets one.
* **judgement** -- "in flight" is ``status == 'running'``, not "carries a
  process handle".  A row restored from the database at boot
  (``routes.execution._restore_execution_state_from_row``) has
  ``process=None``; judging by the handle would skip exactly the restart
  case this feature exists for.
* **isolation** -- a writer that raises costs the checkpoints, not the
  shutdown: the kills still happen and nothing propagates.
* **idempotence** -- the lifespan shutdown and the ``atexit`` hook are two
  calls in one process; the second must not restamp the first.
* **teardown** -- the ``atexit`` hook runs after the interpreter has begun
  tearing down, when an import can no longer be performed.  The writer
  module is therefore resolved at *import* time, and a checkpoint pass
  that cannot run costs the shutdown nothing.

The interrupted / unfinished task lists are the writer's own seam (task
1's file covers them).  They are stubbed here so this file stays a unit
test of the *wiring* and does not reach into an execution log or the
state database.
"""

import builtins
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
import shutdown_checkpoint
import verification_loop
from pause_state import read_pause_checkpoint
from plan_state import PlanState
from shutdown_checkpoint import reset_shutdown_checkpoint_state


#: The HTTP client the watcher test drives the start endpoint through.
client = TestClient(server.app)


#: The file a checkpoint lives in -- ``<plan_dir>/pause_state.json``.
CHECKPOINT_NAME = "pause_state.json"


class _FakeProc:
    """The only two attributes the shutdown path touches on a process.

    No thread, no real process: the shutdown path only asks whether the
    process is still running (``poll``) and logs its pid.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid

    def poll(self):
        """``None`` means "still running"."""
        return None


class _Kills:
    """A stand-in for ``server._kill_process_tree`` that records its calls."""

    def __init__(self) -> None:
        self.calls = []
        #: An optional callback, invoked with the process on every kill.
        #: The ordering test uses it to look at the disk *while* the
        #: shutdown is in the middle of killing things.
        self.observe = None

    def __call__(self, proc):
        self.calls.append(proc)
        if self.observe is not None:
            self.observe(proc)


class _Env:
    """The server, rewired: temp plans, empty registries, recorded kills."""

    def __init__(self, root):
        self.root = Path(root)
        self.execution_state = {}
        self.verification_state = {}
        self.kills = _Kills()
        self.reaped = []

    def plan_dir(self, plan_id):
        """Create the plan's directory and return it."""
        path = self.root / plan_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def checkpoint(self, plan_id):
        """The plan's checkpoint, or ``None``."""
        return read_pause_checkpoint(self.root / plan_id)

    def checkpointed_plans(self):
        """The plans that have a checkpoint on disk right now."""
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if (entry / CHECKPOINT_NAME).exists()
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A server whose shutdown path is observable end to end."""
    reset_shutdown_checkpoint_state()
    server._reset_shutdown_flag()
    e = _Env(tmp_path)

    # ``server._plan_dir`` resolves ``PLANS_DIR / plan_id`` against the
    # module global, so pointing that global at the temp directory is
    # what puts the checkpoints on disk here.
    monkeypatch.setattr(server, "PLANS_DIR", e.root)
    monkeypatch.setattr(server, "_execution_state", e.execution_state)
    monkeypatch.setattr(server, "_verification_state", e.verification_state)
    monkeypatch.setattr(server, "_kill_process_tree", e.kills)
    monkeypatch.setattr(
        server,
        "_reap_all_plans",
        lambda plans_dir, **kwargs: e.reaped.append(kwargs),
    )
    # The writer's interruption lists are task 1's seam.  Stubbing them
    # keeps this file a unit test of the wiring: the real lookup reads an
    # execution log and the state database, neither of which is what this
    # file is about.
    monkeypatch.setattr(
        shutdown_checkpoint,
        "_default_interruption_for",
        lambda plan_id, plan_dir: ([], []),
    )
    yield e
    reset_shutdown_checkpoint_state()
    server._reset_shutdown_flag()


def test_the_checkpoint_exists_when_the_kill_is_called(env):
    """The checkpoint is on disk by the time the process tree is killed."""
    env.execution_state["p1"] = {"status": "running", "process": _FakeProc()}
    env.plan_dir("p1")

    seen = []
    env.kills.observe = lambda killed: seen.append(
        (env.root / "p1" / CHECKPOINT_NAME).exists()
    )

    server._shutdown_all_executions()

    assert seen == [True], "the checkpoint must precede the kill"


def test_the_checkpoint_precedes_every_termination(env):
    """Two in-flight plans: two checkpoints, and both precede both kills."""
    procs = {"p1": _FakeProc(11), "p2": _FakeProc(12)}
    for plan_id, proc in procs.items():
        env.execution_state[plan_id] = {"status": "running", "process": proc}
        env.plan_dir(plan_id)

    snapshots = []
    env.kills.observe = lambda killed: snapshots.append(env.checkpointed_plans())

    server._shutdown_all_executions()

    assert len(env.kills.calls) == 2
    assert snapshots == [["p1", "p2"], ["p1", "p2"]], (
        "every checkpoint must be on disk before the first termination"
    )
    for plan_id in procs:
        assert env.checkpoint(plan_id) is not None, plan_id


def test_a_failed_checkpoint_does_not_stop_the_shutdown(env, monkeypatch):
    """A writer that raises costs the checkpoints, not the kills."""

    attempts = []

    def _boom(*args, **kwargs):
        attempts.append(kwargs.get("plan_dir_for"))
        raise RuntimeError("the checkpoint pass failed")

    monkeypatch.setattr(
        shutdown_checkpoint, "write_shutdown_checkpoints", _boom
    )

    procs = {"p1": _FakeProc(11), "p2": _FakeProc(12)}
    for plan_id, proc in procs.items():
        env.execution_state[plan_id] = {"status": "running", "process": proc}
        env.plan_dir(plan_id)

    server._shutdown_all_executions()  # must not raise

    assert len(attempts) == 1, "the shutdown must attempt the checkpoint pass"
    assert len(env.kills.calls) == 2
    assert env.checkpoint("p1") is None


def test_a_failure_while_marking_does_not_cost_the_checkpoints(
    env, monkeypatch,
):
    """Marking the interrupted plans cannot cost the resume points.

    The shutdown marks every plan it interrupts, so a watcher can tell
    "the server took this down" from "this died on its own".  That walk
    can fail -- a record that is not a dict, a registry another thread
    is mutating -- and the function it happens in carries its own
    contract: *nothing here may raise*, because the checkpoint that
    follows is the one thing that has to reach the disk.

    So the marking gets a guard of its own.  A failure there is logged
    and the checkpoint pass still runs: the resume point is worth more
    than the mark, and a plan that is not marked only costs the verdict
    its careful wording.
    """

    def _boom(plan_id):
        raise RuntimeError("the marking failed")

    monkeypatch.setattr(server, "_mark_shutdown_interrupted", _boom)

    env.execution_state["p1"] = {"status": "running", "process": _FakeProc()}
    env.plan_dir("p1")

    server._shutdown_all_executions()  # must not raise

    assert env.checkpoint("p1") is not None, (
        "a failure while marking must not cost the plan its resume point"
    )
    assert len(env.kills.calls) == 1, "the kill still happens"


def test_the_writer_module_is_resolved_at_import_time():
    """The shutdown path must not need an import it can no longer perform.

    ``_shutdown_all_executions`` runs from an ``atexit`` hook — after the
    interpreter has begun tearing down, and under pytest after the
    ``pythonpath`` plugin has removed the entries it injected.  An import
    there raises ``ModuleNotFoundError``, and *reporting* that failure
    hits a logger whose stream is already closed ("I/O operation on
    closed file") — which is exactly the error block printed after an
    otherwise green run that ``server.py`` already documents for the
    ``service_manager`` imports it moved up for this reason.

    So the module has to be in ``sys.modules`` before the process starts
    going away: ``server`` holds the reference.
    """
    assert getattr(server, "shutdown_checkpoint", None) is not None, (
        "server must resolve the writer module at import time"
    )
    assert server.shutdown_checkpoint is sys.modules["shutdown_checkpoint"]


def test_the_checkpoint_pass_survives_a_writer_that_cannot_be_imported(
    env, monkeypatch
):
    """A teardown that broke the import must not cost the shutdown its kills."""
    real_import = builtins.__import__

    def _refuse(name, *args, **kwargs):
        if name == "shutdown_checkpoint":
            raise ModuleNotFoundError("No module named 'shutdown_checkpoint'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _refuse)

    procs = {"p1": _FakeProc(11), "p2": _FakeProc(12)}
    for plan_id, proc in procs.items():
        env.execution_state[plan_id] = {"status": "running", "process": proc}
        env.plan_dir(plan_id)

    server._shutdown_all_executions()  # must not raise

    assert len(env.kills.calls) == 2


def test_a_second_shutdown_call_does_not_rewrite(env, monkeypatch):
    """The lifespan shutdown and the atexit hook are two calls, one file."""
    writes = []
    real_write = shutdown_checkpoint.write_pause_checkpoint

    def _counting_write(plan_dir, **kwargs):
        writes.append(Path(plan_dir).name)
        return real_write(plan_dir, **kwargs)

    monkeypatch.setattr(
        shutdown_checkpoint, "write_pause_checkpoint", _counting_write
    )

    env.execution_state["p1"] = {"status": "running", "process": _FakeProc()}
    env.plan_dir("p1")

    server._shutdown_all_executions()
    first = env.checkpoint("p1")
    server._shutdown_all_executions()
    second = env.checkpoint("p1")

    assert first is not None
    assert writes == ["p1"], "the second shutdown must not rewrite the checkpoint"
    assert second["paused_at"] == first["paused_at"]


def test_a_verification_round_is_checkpointed(env):
    """A running verification is checkpointed with the round it reached."""
    env.verification_state["p2"] = {
        "verification_status": "running",
        "verification_round": 2,
    }
    env.plan_dir("p2")

    server._shutdown_all_executions()

    checkpoint = env.checkpoint("p2")
    assert checkpoint is not None
    assert checkpoint["kind"] == "verification"
    assert checkpoint["verification_round"] == 2


def test_a_restored_row_with_no_process_handle_is_checkpointed(env):
    """A row restored at boot has no handle -- the plan is still in flight."""
    env.execution_state["p3"] = {
        "status": "running",
        "process": None,
        "project_dir": "/w/p3",
    }
    env.plan_dir("p3")

    server._shutdown_all_executions()

    checkpoint = env.checkpoint("p3")
    assert checkpoint is not None
    assert checkpoint["kind"] == "execution"
    assert checkpoint["project_dir"] == "/w/p3"
    # Nothing to kill -- the handle is gone -- but the plan was still
    # running when the server went down, so it still gets a resume point.
    assert env.kills.calls == []


# ---------------------------------------------------------------------------
# The shutdown flag
# ---------------------------------------------------------------------------
#
# The checkpoint says one thing: *the server went down under this
# work*.  A verdict written after it says another: *the executor
# crashed*.  The two cannot both be true, and the verdict wins because
# it is written last — the checkpoint's ``queued`` was overwritten with
# ``failed`` and the plan was never picked up again.
#
# The flag is how a watcher learns which of the two it is looking at.
# ``_shutdown_all_executions`` sets it before the first SIGTERM, and
# every path that judges a dead process stands down while it is set:
# the exit is recorded, the verdict is left to the next boot, which
# parks the plan from the checkpoint that is already on disk.
#
# The stand-down is **per plan**.  The flag alone is process-level and
# therefore too coarse: it says "the server is going down", not "the
# server took *this* plan down".  So raising the flag also marks the
# plans that were in flight at that moment — the ones the shutdown
# wrote a checkpoint for and is about to take down — and a verdict path
# stands down only for a plan that carries the mark.  A plan the
# shutdown never took down still gets its crash verdict.


def test_the_shutdown_flag_is_set_before_the_first_kill(env):
    """The flag is up before anything is killed.

    A flag set after the kill would be set too late: the watcher wakes
    up from ``process.wait()`` as soon as the process dies.
    """
    env.execution_state["p1"] = {"status": "running", "process": _FakeProc()}
    env.plan_dir("p1")

    seen = []
    env.kills.observe = lambda killed: seen.append(server._is_shutting_down())

    server._shutdown_all_executions()

    assert seen == [True], "the flag must be set before the first kill"


def test_the_flag_is_clear_until_the_shutdown_starts(env):
    """The flag says "the server is going down", not "the server did"."""
    assert server._is_shutting_down() is False

    env.execution_state["p1"] = {"status": "running", "process": _FakeProc()}
    env.plan_dir("p1")
    server._shutdown_all_executions()

    assert server._is_shutting_down() is True


def test_the_shutdown_marks_the_plan_it_interrupted(env):
    """The mark is written before the kill, so the watcher finds it."""
    env.execution_state["p1"] = {"status": "running", "process": _FakeProc()}
    env.plan_dir("p1")

    seen = []
    env.kills.observe = lambda killed: seen.append(
        server._shutdown_interrupted("p1")
    )

    server._shutdown_all_executions()

    assert seen == [True], "the mark must be there before the kill"


def test_raising_the_flag_marks_every_plan_in_flight():
    """The flag and the marks go up together.

    A plan that is running when the shutdown starts is work the
    shutdown ends, so it carries the mark from the moment the flag
    goes up — before the first SIGTERM, and before any watcher can
    wake up and judge it.  A plan that is not running is not marked:
    the shutdown did not end anything of its own.
    """
    server._execution_state["p-inflight"] = {
        "status": "running",
        "pid": 4242,
        "stop_reason": None,
        "process": None,
    }
    server._execution_state["p-finished"] = {
        "status": "completed",
        "pid": None,
        "stop_reason": None,
        "process": None,
    }

    server._begin_shutdown()
    try:
        assert server._shutdown_interrupted("p-inflight") is True
        assert server._shutdown_interrupted("p-finished") is False
    finally:
        server._reset_shutdown_flag()


def _running_state(plan_id, pid=999999):
    """Put a running plan into the server's own execution registry."""
    server._execution_state[plan_id] = {
        "status": "running",
        "pid": pid,
        "stop_reason": None,
        "process": None,
    }
    return server._execution_state[plan_id]


def _begin_shutdown_for(plan_id):
    """The shutdown's own two facts: the flag and this plan's mark."""
    server._begin_shutdown()
    server._mark_shutdown_interrupted(plan_id)


def test_the_watchdog_does_not_fail_a_plan_while_the_server_is_shutting_down():
    """A dead executor during a shutdown is not a crashed executor."""
    plan_id = "p-shutdown-watchdog"
    state = _running_state(plan_id)

    _begin_shutdown_for(plan_id)
    try:
        with patch("os.kill", side_effect=ProcessLookupError):
            with patch.object(server, "_mark_failed_dead") as mark:
                verification_loop._lazy_check_execution(plan_id)
    finally:
        server._reset_shutdown_flag()

    mark.assert_not_called()
    assert state["status"] == "running"
    assert state.get("stop_reason") is None


def test_the_watchdog_fires_when_the_server_is_not_shutting_down():
    """The negative control: without the flag the watchdog still fires."""
    plan_id = "p-running-watchdog"
    state = _running_state(plan_id)

    server._reset_shutdown_flag()
    with patch("os.kill", side_effect=ProcessLookupError):
        with patch.object(server, "_mark_failed_dead") as mark:
            verification_loop._lazy_check_execution(plan_id)

    mark.assert_called_once_with(plan_id)
    assert state["status"] == "running"


def test_a_crash_of_a_plan_the_shutdown_never_took_down_is_still_a_crash():
    """The stand-down is for the work the shutdown ended.

    The shutdown marks the plans that were in flight when it started —
    the ones it wrote a checkpoint for and is about to take down.  A
    plan it never took down carries no mark, so its crash is still a
    crash: the shutdown is not an excuse for every dead process in the
    process.
    """
    plan_id = "p-unrelated-crash"

    server._begin_shutdown()
    try:
        # The plan is in flight *after* the shutdown marked the work it
        # took down, so nothing marked this one.
        state = _running_state(plan_id, pid=4242)
        verification_loop._mark_failed_dead(plan_id)
    finally:
        server._reset_shutdown_flag()

    assert state["status"] == "failed"
    assert state["stop_reason"] == "process_died_unexpectedly"


def test_the_crash_verdict_is_not_written_while_the_server_is_shutting_down():
    """``_mark_failed_dead`` stands down while the server is going down."""
    plan_id = "p-shutdown-verdict"
    state = _running_state(plan_id, pid=4242)

    _begin_shutdown_for(plan_id)
    try:
        verification_loop._mark_failed_dead(plan_id)
    finally:
        server._reset_shutdown_flag()

    assert state["status"] == "running"
    assert state.get("stop_reason") is None


# ---------------------------------------------------------------------------
# The executor watcher
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """A subprocess stand-in that reports the shutdown's own SIGTERM.

    ``wait()`` blocks until the test releases it, so the watcher is
    parked exactly where a real executor is when the server goes down:
    alive, waiting, and about to be killed.
    """

    def __init__(self, returncode=-15):
        self.pid = 4242
        self._returncode = returncode
        self._block = threading.Event()

    @property
    def returncode(self):
        return self._returncode

    @property
    def stdout(self):
        return iter([])

    def wait(self, timeout=None):
        self._block.wait(timeout=timeout or 30)
        return self._returncode

    def poll(self):
        return self._returncode if self._block.is_set() else None

    def terminate(self):
        self._block.set()

    def release(self):
        self._block.set()


def _setup_plan_dir(plan_id):
    """A minimal plan directory: a project dir, tasks, and a plan state."""
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "project").mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "ready",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def _start_execution(monkeypatch, plan_id, plan_dir, fake):
    """Start an execution whose executor is the fake."""
    monkeypatch.setattr("server.subprocess.Popen", lambda *a, **k: fake)
    monkeypatch.setattr(
        "server._run_auto_verification_loop", lambda *a, **k: None
    )

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(plan_dir / "project")},
    )
    assert resp.status_code == 200, resp.text


def _release_executor(reclaim_workers, fake):
    """Let the executor exit and let its watcher see the exit."""
    fake.release()
    reclaim_workers()


def test_the_executor_watcher_does_not_fail_a_plan_while_the_server_is_shutting_down(
    monkeypatch, reclaim_workers
):
    """The shutdown's own SIGTERM is not written as a crashed executor."""
    plan_id = "p-shutdown-watcher"
    plan_dir = _setup_plan_dir(plan_id)
    fake = _FakeExecutor(returncode=-15)

    server._begin_shutdown()
    try:
        _start_execution(monkeypatch, plan_id, plan_dir, fake)
        # The shutdown's mark: the plan it took down.
        server._mark_shutdown_interrupted(plan_id)
        _release_executor(reclaim_workers, fake)
    finally:
        server._reset_shutdown_flag()

    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") != "failed", (
        "the shutdown's own kill must not be written as a crash"
    )
    assert state.get("stop_reason") != "process_died_unexpectedly"
    # The watcher's other verdict spelling: a death by signal carries
    # the signal that caused it ("signal:15"), so that is the second
    # way this run can be written off as a crash.  Neither of them may
    # be written while the server is shutting down.
    assert state.get("stop_reason") != "signal:15"
    assert PlanState(plan_dir).get_current_phase() != "failed", (
        "the checkpoint's verdict must not be overwritten by the shutdown"
    )


def test_the_executor_watcher_fails_a_crashed_executor_while_the_server_runs(
    monkeypatch, reclaim_workers
):
    """The negative control: a real crash is still a crash."""
    plan_id = "p-crashed-watcher"
    plan_dir = _setup_plan_dir(plan_id)
    fake = _FakeExecutor(returncode=-15)

    server._reset_shutdown_flag()
    _start_execution(monkeypatch, plan_id, plan_dir, fake)
    _release_executor(reclaim_workers, fake)

    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") == "failed"
    # The watcher names the death it saw: the executor was killed by
    # SIGTERM, so the reason carries the signal.  The flat
    # "process_died_unexpectedly" belongs to the paths that cannot say
    # how the process died -- see test_executor_stop_reason_refined.py.
    assert state.get("stop_reason") == "signal:15"
    assert PlanState(plan_dir).get_current_phase() == "failed"


# ---------------------------------------------------------------------------
# The executor watcher's own crash
# ---------------------------------------------------------------------------


def _break_the_watcher_body(monkeypatch):
    """Make the watcher's post-exit body raise.

    The watcher's ``except Exception`` fallback is the one verdict path
    that never asked the shutdown flag: it wrote ``failed`` /
    ``process_died_unexpectedly`` / ``transition_to("failed")`` for any
    exception raised anywhere in the watcher body.

    The injection point is the forensics call.  It sits right after the
    executor's exit is recorded, so the watcher is exactly where it is
    when the shutdown's own SIGTERM wakes it up -- alive, mid-body, with
    the exit already on record.
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("the watcher's body exploded")

    monkeypatch.setattr(
        server._routes_execution, "_finalize_executor_forensics", _boom
    )


def test_the_watcher_crash_does_not_fail_a_plan_while_the_server_is_shutting_down(
    env, monkeypatch, reclaim_workers
):
    """A watcher that breaks during the shutdown is not a crash.

    The watcher's ``except Exception`` fallback is the one verdict path
    that never asked the shutdown flag.  It wrote ``failed`` and moved
    the plan to its terminal phase for any exception raised in the
    watcher body -- and during the shutdown that overwrites the
    checkpoint's verdict ("interrupted") with a terminal one.  A plan
    parked as ``failed`` is in nobody's waiting set, so it is never
    picked up again: the work is lost, not resumed.

    So the fallback stands down with the other two verdict paths: the
    crash is *recorded* and nothing is written.  The row is left for the
    next boot, which parks it from the checkpoint that is already on
    disk.

    The stand-down is written onto the record by the shutdown itself --
    the plan the shutdown took down is stopped, not running -- so the
    test drives the real shutdown path rather than the flag and the
    mark alone: the flag says "the server is going down", but it is the
    shutdown that puts the verdict on the record, and only the real
    path does that.  (``env`` records the kill instead of performing it,
    so the fake executor is released by hand below.)
    """
    plan_id = "p-shutdown-watcher-crash"
    plan_dir = _setup_plan_dir(plan_id)
    fake = _FakeExecutor(returncode=-15)
    _break_the_watcher_body(monkeypatch)

    _start_execution(monkeypatch, plan_id, plan_dir, fake)
    server._shutdown_all_executions()
    _release_executor(reclaim_workers, fake)

    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") != "failed", (
        "a watcher that broke during the shutdown must not fail the plan"
    )
    assert state.get("stop_reason") != "process_died_unexpectedly"
    assert PlanState(plan_dir).get_current_phase() != "failed", (
        "the checkpoint's verdict must not be overwritten by the shutdown"
    )


def test_the_watcher_crash_fails_a_plan_while_the_server_runs(
    monkeypatch, reclaim_workers
):
    """The negative control: a watcher that breaks on its own is a crash.

    The stand-down above is for the work the shutdown ended.  A watcher
    that blows up while the server is running still gets the crash
    verdict: the shutdown is not an excuse for every dead process in the
    process.
    """
    plan_id = "p-crashed-watcher-body"
    plan_dir = _setup_plan_dir(plan_id)
    fake = _FakeExecutor(returncode=-15)
    _break_the_watcher_body(monkeypatch)

    server._reset_shutdown_flag()
    _start_execution(monkeypatch, plan_id, plan_dir, fake)
    _release_executor(reclaim_workers, fake)

    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") == "failed"
    assert state.get("stop_reason") == "process_died_unexpectedly"
    assert PlanState(plan_dir).get_current_phase() == "failed"


# ---------------------------------------------------------------------------
# The repair watcher
# ---------------------------------------------------------------------------


def _spawn_fake_executor(monkeypatch, fake):
    """Route the repair path's spawn at the fake executor.

    The repair path spawns through ``server._spawn_executor_subprocess``
    (the shared helper), so stubbing that one call is what puts the
    fake in front of the repair watcher.
    """
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (fake, kwargs.get("log_path")),
    )


def test_the_repair_watcher_does_not_fail_a_plan_while_the_server_is_shutting_down(
    monkeypatch, reclaim_workers
):
    """The shutdown's own SIGTERM is not written as a failed repair round.

    The repair watcher is the third place a dead process is judged (the
    main executor watcher and the execution watchdog are the other two).
    The shutdown kills the repair executor with the same SIGTERM it sends
    to the main executor, so the same rule applies.

    The repair watcher adds one thing the other two do not: on its way
    out it fires ``on_complete``, and that callback
    (``_on_repair_complete``) starts a **fresh verification round** and
    writes the plan's phase.  During a shutdown that is new work on a
    server that is going away, and the phase it writes contradicts the
    checkpoint the next boot parks the plan from.  So the callback
    stands down with the verdict: the round is left for the restart.
    """
    plan_id = "p-shutdown-repair"
    plan_dir = _setup_plan_dir(plan_id)
    fake = _FakeExecutor(returncode=-15)
    _spawn_fake_executor(monkeypatch, fake)

    fired = []

    server._begin_shutdown()
    try:
        verification_loop._run_repair_execution_async(
            plan_id,
            plan_dir / "project",
            on_complete=fired.append,
        )
        # The shutdown's mark: the plan it took down.
        server._mark_shutdown_interrupted(plan_id)
        _release_executor(reclaim_workers, fake)
    finally:
        server._reset_shutdown_flag()

    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") != "failed", (
        "the shutdown's own kill must not be written as a failed repair round"
    )
    assert state.get("stop_reason") != "repair_signal:15", (
        "the shutdown's own kill must not be written as a failed repair "
        "round"
    )
    assert fired == [], (
        "the shutdown must not re-enter verification — the next boot "
        "parks the plan from the checkpoint"
    )


def test_the_repair_watcher_fails_a_crashed_repair_while_the_server_runs(
    monkeypatch, reclaim_workers
):
    """The negative control: a real crash is still a crash.

    The stand-down above is for the work the shutdown ended.  A repair
    executor that died on its own while the server is running still gets
    the crash verdict, and the round still re-enters verification — that
    is what lets the chain decide what to do next.

    The verdict names the death: the repair executor was killed by
    SIGTERM, so the reason carries the signal ("repair_signal:15").
    See test_repair_exit_forensics.py for the wording.
    """
    plan_id = "p-crashed-repair"
    plan_dir = _setup_plan_dir(plan_id)
    fake = _FakeExecutor(returncode=-15)
    _spawn_fake_executor(monkeypatch, fake)

    fired = []

    server._reset_shutdown_flag()
    verification_loop._run_repair_execution_async(
        plan_id,
        plan_dir / "project",
        on_complete=fired.append,
    )
    _release_executor(reclaim_workers, fake)

    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") == "failed"
    assert state.get("stop_reason") == "repair_signal:15"
    assert fired == [-15], "the round still has to re-enter verification"
