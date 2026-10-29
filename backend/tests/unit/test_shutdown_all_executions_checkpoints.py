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
import sys
from pathlib import Path

import pytest

import server
import shutdown_checkpoint
from pause_state import read_pause_checkpoint
from shutdown_checkpoint import reset_shutdown_checkpoint_state


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
