"""The repair round is on the ledger under its own role.

Why this exists
---------------
The ledger (``backend/process_ledger.py``) records every process the
backend starts, and the reaper (``backend/process_reaper.py``) decides
what to do with it *by role* — children first, parents last::

    REAP_ORDER = (subagent, vp_service, tool, repair_executor, executor)

A repair round spawns an executor of its own, and that executor is a
child of the plan's main executor in every sense that matters: it
writes the same files, it holds the same locks, and it has to be torn
down before the main executor is. If the repair executor is recorded
as a plain ``executor``, the reaper cannot tell the two apart and the
ordering the ledger was built for is lost.

So the repair path asks for its own role::

    _run_repair_execution(...) -> _spawn_executor_subprocess(role="repair_executor")

and the spawn helper refuses any role it does not know. The refusal is
a ``ValueError`` raised *before* the process starts: a role the ledger
does not know would describe a process the ledger cannot represent,
and the "a ledger failure never blocks a run" rule would otherwise
swallow the mistake instead of naming it.

What is pinned here
-------------------
* the repair round's spawn is recorded with the role
  ``repair_executor`` — the live record says so while the process
  runs;
* a role outside the whitelist is refused, and nothing is spawned;
* the exit is written after ``process.wait()``, and the ``finally``
  writes it even when the round blows up mid-wait — a pid the backend
  has stopped tracking must not stay live in the ledger;
* a ledger write that fails does not break the repair round;
* a failed repair round does not change the role back to ``executor``;
* the **asynchronous** repair round — the one production actually
  runs — is on the ledger under the same role, and its exit is written
  when the process ends.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` has to be imported before ``routes.execution``: the server
# module wires the router at import time and ``routes.execution``
# late-binds back into it, so the other order is a circular import.
import server  # noqa: E402
import verification_loop  # noqa: E402
from routes import execution as execution_route  # noqa: E402

import process_ledger as ledger  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the spawn puts the child in a session of its own, and the "
        "record's pgid is POSIX-only"
    ),
)

PLAN_ID = "20261030-repair-ledger"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fake_backend(tmp_path: Path, exit_code: int = 0) -> Path:
    """A ``backend/`` directory whose ``cli.py`` is a harmless stub.

    The real spawn helper builds ``<backend_dir>/cli.py`` onto the
    command line, so pointing it at this directory gives the test a
    real child process without launching the real executor. The stub
    exits immediately — the repair round only needs a process that
    starts and ends.
    """
    backend_dir = tmp_path / "fake_backend"
    backend_dir.mkdir()
    (backend_dir / "cli.py").write_text(
        "import sys\n"
        "sys.stdout.write('fake repair executor up\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(%d)\n" % exit_code,
        encoding="utf-8",
    )
    return backend_dir


def _terminate(proc) -> None:
    """Stop a spawned child without ever touching our own process group."""
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid is not None and pgid != os.getpgid(0):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 - the child is disposable
        pass


def _tasks_file(pdir: Path) -> Path:
    tasks_file = pdir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]}),
        encoding="utf-8",
    )
    return tasks_file


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A plan directory wired into the server's plans root."""
    plans_dir = tmp_path / "plans"
    pdir = plans_dir / PLAN_ID
    pdir.mkdir(parents=True)
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    return PLAN_ID, pdir


@pytest.fixture
def project_dir(tmp_path):
    """The working directory the executor is pointed at."""
    pdir = tmp_path / "project"
    pdir.mkdir()
    return pdir


def _spawn_events(pdir: Path) -> List[Dict[str, Any]]:
    return [e for e in ledger.read_events(pdir) if e.get("event") == "spawn"]


def _exit_events(pdir: Path) -> List[Dict[str, Any]]:
    return [e for e in ledger.read_events(pdir) if e.get("event") == "exit"]


def _wait_for_exit(
    pdir: Path, pid: int, timeout: float = 20.0
) -> Dict[str, Any]:
    """Poll the ledger until it carries a terminal event for ``pid``.

    The async repair round writes its exit from a background thread, so
    the event lands some time after the spawn. Returns the event, or an
    empty dict when the timeout passes with nothing written.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in ledger.read_events(pdir):
            if event.get("pid") != pid:
                continue
            if str(event.get("event") or "") in ledger.TERMINAL_EVENTS:
                return event
        time.sleep(0.05)
    return {}


# ---------------------------------------------------------------------------
# 1. The repair round is recorded under its own role.
# ---------------------------------------------------------------------------
def test_repair_spawn_records_repair_executor_role(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """The repair round's record says ``repair_executor``, not ``executor``.

    The reaper orders the roles it reaps; a repair executor recorded
    as a plain executor is indistinguishable from the plan's main
    executor, and the "children first" ordering is lost.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    monkeypatch.setattr(server, "_BACKEND_DIR", backend_dir)
    _tasks_file(pdir)

    # The live set is captured at the moment the spawn is written —
    # that is the window in which the record names a running process.
    captured: Dict[str, Any] = {}
    real_write_spawn = execution_route._ledger_write_spawn

    def _capturing_write_spawn(*args: Any, **kwargs: Any) -> None:
        real_write_spawn(*args, **kwargs)
        captured["live"] = dict(ledger.live_records(pdir))

    monkeypatch.setattr(
        execution_route, "_ledger_write_spawn", _capturing_write_spawn
    )

    assert verification_loop._run_repair_execution(
        pid_, project_dir, tool="claude"
    ) is True, "the repair round did not report success"

    live = captured.get("live") or {}
    assert len(live) == 1, (
        "the repair round did not put exactly one process on the ledger"
    )
    record = next(iter(live.values()))
    assert record.role == "repair_executor", (
        "the repair executor is recorded as %r — the reaper cannot tell "
        "it apart from the plan's main executor, so it cannot reap it "
        "first" % (record.role,)
    )
    assert record.plan_id == pid_
    assert record.pgid == record.pid, (
        "the repair executor does not lead its own process group, so a "
        "kill aimed at the group cannot reach it"
    )

    # The record on disk says the same thing.
    spawns = _spawn_events(pdir)
    assert len(spawns) == 1
    assert spawns[0]["record"]["role"] == "repair_executor"

    # And nothing anywhere in the ledger claims this round was a plain
    # executor — a failed repair round must not fall back to the
    # default role.
    for event in ledger.read_events(pdir):
        assert event.get("role") != "executor", (
            "the repair round left an 'executor' record behind"
        )
        nested = event.get("record")
        if isinstance(nested, dict):
            assert nested.get("role") != "executor"


# ---------------------------------------------------------------------------
# 2. A role the ledger does not know is refused at the door.
# ---------------------------------------------------------------------------
def test_spawn_rejects_unknown_role(plan_dir, project_dir, tmp_path):
    """Only ``executor`` and ``repair_executor`` may be spawned.

    The refusal happens before the process starts. A role the ledger
    cannot represent would produce a record the reader skips — and the
    "a ledger failure never blocks a run" rule would swallow the
    mistake instead of naming it.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    def _spawn(role: str):
        return execution_route._spawn_executor_subprocess(
            plan_id=pid_,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=None,
            log_path=tmp_path / ("spawn_%s.log" % role),
            role=role,
        )

    with pytest.raises(ValueError) as excinfo:
        _spawn("subagent")
    assert "subagent" in str(excinfo.value), (
        "the error does not name the offending role — the caller cannot "
        "tell which value was refused"
    )

    # Nothing was spawned, so nothing was recorded.
    assert _spawn_events(pdir) == [], (
        "a refused role still started a process"
    )
    assert ledger.live_records(pdir) == {}

    # The two roles the ledger knows are accepted.
    for role in ("executor", "repair_executor"):
        proc, _log_path = _spawn(role)
        try:
            live = ledger.live_records(pdir)
            assert proc.pid in live, (
                "the %s spawn was not recorded on the ledger" % role
            )
            assert live[proc.pid].role == role
        finally:
            _terminate(proc)


# ---------------------------------------------------------------------------
# 3. The exit is written even when the round blows up.
# ---------------------------------------------------------------------------
def test_repair_writes_exit_even_on_exception(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """A round that dies mid-wait still leaves an exit on the ledger.

    The exit is written after ``process.wait()`` — a process that has
    not ended cannot have exited. The ``finally`` covers the path where
    the round blew up before or during the wait: without it the pid
    would stay live in the ledger and the reaper would go on believing
    a process the backend stopped tracking is still running.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    monkeypatch.setattr(server, "_BACKEND_DIR", backend_dir)
    _tasks_file(pdir)

    real_spawn = execution_route._spawn_executor_subprocess
    spawned: List[Any] = []

    class _WaitExplodes:
        """A process whose ``wait()`` raises — the round blows up."""

        def __init__(self, proc) -> None:
            self.pid = proc.pid
            self.returncode = proc.returncode

        def wait(self, *args: Any, **kwargs: Any) -> int:
            raise RuntimeError("wait exploded")

    def _spawn_whose_wait_raises(**kwargs: Any):
        proc, log_path = real_spawn(**kwargs)
        spawned.append(proc)
        return _WaitExplodes(proc), log_path

    monkeypatch.setattr(
        server, "_spawn_executor_subprocess", _spawn_whose_wait_raises
    )

    try:
        with pytest.raises(RuntimeError):
            verification_loop._run_repair_execution(
                pid_, project_dir, tool="claude"
            )

        assert len(spawned) == 1, "the round never started a process"
        pid = spawned[0].pid

        exits = _exit_events(pdir)
        assert len(exits) == 1, (
            "the round died without writing an exit — the ledger still "
            "believes the pid is running"
        )
        assert exits[0]["pid"] == pid

        # The exit took the record out of the live set.
        assert pid not in ledger.live_records(pdir), (
            "the exit was written but the pid is still live"
        )

        # And the exit came after the spawn.
        events = ledger.read_events(pdir)
        assert [e.get("event") for e in events] == ["spawn", "exit"]
    finally:
        for proc in spawned:
            _terminate(proc)


# ---------------------------------------------------------------------------
# 4. A failed round is still a repair executor.
# ---------------------------------------------------------------------------
def test_failed_repair_round_keeps_the_repair_executor_role(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """A round that failed does not fall back to the default role.

    The role says *what kind of process this is*, not how it went. A
    failed repair round is still the plan's child, and the reaper has
    to be able to reap it before the main executor.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path, exit_code=3)
    monkeypatch.setattr(server, "_BACKEND_DIR", backend_dir)
    _tasks_file(pdir)

    assert verification_loop._run_repair_execution(
        pid_, project_dir, tool="claude"
    ) is False, "a round whose executor exited 3 reported success"

    events = ledger.read_events(pdir)

    spawns = _spawn_events(pdir)
    assert len(spawns) == 1
    assert spawns[0]["record"]["role"] == "repair_executor", (
        "the failed round was recorded as %r"
        % (spawns[0]["record"]["role"],)
    )

    exits = _exit_events(pdir)
    assert len(exits) == 1, "the failed round never wrote an exit"
    assert exits[0]["role"] == "repair_executor", (
        "the exit says the process was a %r — the record it came from "
        "was wrong" % (exits[0]["role"],)
    )
    assert exits[0]["exit_code"] == 3, (
        "the exit does not carry the return code — the ledger cannot "
        "tell a failed round from a successful one"
    )

    # Nothing anywhere claims to be a plain executor.
    for event in events:
        assert event.get("role") != "executor"


# ---------------------------------------------------------------------------
# 5. A ledger that cannot be written does not break the round.
# ---------------------------------------------------------------------------
def test_ledger_write_failure_does_not_break_the_repair_round(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """The ledger is evidence, not a gate.

    A read-only plan directory or a full disk must not cost the
    operator the repair round they asked for. The failure is logged
    and the work continues.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    monkeypatch.setattr(server, "_BACKEND_DIR", backend_dir)
    _tasks_file(pdir)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("the ledger is on a read-only filesystem")

    monkeypatch.setattr(execution_route, "ledger_record_exit", _boom)
    monkeypatch.setattr(execution_route, "_ledger_write_exit", _boom)

    assert verification_loop._run_repair_execution(
        pid_, project_dir, tool="claude"
    ) is True, (
        "a ledger that could not be written broke the repair round"
    )


# ---------------------------------------------------------------------------
# 6. The path production actually runs is on the ledger too.
# ---------------------------------------------------------------------------
def test_async_repair_round_is_recorded_and_closed(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """The asynchronous repair round is on the ledger under its own role.

    ``_run_repair_execution`` — the synchronous variant — is the one
    the ledger was first written for, but nothing in production calls
    it: every production caller goes through
    ``_run_repair_execution_async`` (the verification auto-loop, the
    re-entry after a repair round, and the verification route). That
    path spawns the same executor and returns, so two things have to
    be true of it or the ledger lies about the plan:

    * the spawn is recorded under the role ``repair_executor``, so the
      reaper can order it before the plan's main executor;
    * the exit is written when the process ends. A pid that stays live
      in the ledger is a pid the reaper goes on collecting, round after
      round, for a process that ended long ago.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    monkeypatch.setattr(server, "_BACKEND_DIR", backend_dir)
    _tasks_file(pdir)

    with server._execution_state_lock:
        server._execution_state.pop(pid_, None)

    try:
        result = verification_loop._run_repair_execution_async(
            pid_, project_dir
        )
        assert result.get("status") == "started", result
        pid = result.get("pid")
        assert pid, "the asynchronous repair round reported no pid"

        spawns = _spawn_events(pdir)
        assert len(spawns) == 1, (
            "the asynchronous repair round put %d processes on the ledger"
            % len(spawns)
        )
        assert spawns[0]["record"]["role"] == "repair_executor", (
            "the asynchronous repair round was recorded as %r — the "
            "reaper cannot tell it apart from the plan's main executor"
            % (spawns[0]["record"]["role"],)
        )

        exit_event = _wait_for_exit(pdir, pid)
        assert exit_event, (
            "the asynchronous repair round never wrote an exit — its pid "
            "stays in the live set of the ledger forever"
        )
        assert exit_event["role"] == "repair_executor"
        assert exit_event["detail"] == "repair_finished", (
            "the exit says %r — the detail has to say the round really "
            "finished" % (exit_event["detail"],)
        )

        assert pid not in ledger.live_records(pdir), (
            "the exit was written but the pid is still live"
        )
    finally:
        with server._execution_state_lock:
            server._execution_state.pop(pid_, None)

