"""2026-10-30: ``POST /api/execution/{id}/start`` must not start a 2nd executor.

The bug
-------
``/start`` decided "is this plan already running?" from the in-memory
handle alone (``server._execution_state``).  That handle dies with the
backend, so after a restart the plan looked idle and the start spawned
a SECOND executor for a plan whose first one was still running: two
runs writing the same task rows, the same git worktree and the same
log file.

The ledger (``backend/process_ledger.py``) is the durable record of
what this backend started, so it is what the start asks now.  A ledger
record counts as *live* only while the kernel still has a process at
that pid — a record whose process is gone is what a run that ended
without writing its exit event leaves behind, and it must not block
the next start for ever.  The reaper draws the same line when it
sweeps.

What is pinned here:

  1. **one executor per plan** — a live executor in the ledger means
     409 ``already_running``, the pids that are running, and no spawn.
  2. **force reclaims first** — ``?force=true`` sweeps the plan's tree
     from the ledger and starts only if the sweep was clean.
  3. **an unclean sweep does not start** — 409 ``kill_incomplete`` and
     the surviving pids: the stop's vocabulary, the same shape.
  4. **dead records do not block** — a ledger that only knows processes
     that are gone starts normally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import process_identity
import process_ledger as ledger
import process_reaper
import server
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository


PLAN_ID = "20261030-start-single-instance"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spawn_child():
    """A real, detached child process — one process of this plan.

    ``start_new_session=True`` gives it a process group of its own,
    which is what a detached PDT layer looks like.  The reaper refuses
    to signal the caller's own group, so a child that is to be
    signalled at all has to detach — exactly like the real tree.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _cleanup(*procs):
    """Kill and reap every child the test started."""
    for proc in procs:
        if proc is None:
            continue
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001 — cleanup must not fail the test
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _stable_identity(pid, tries=40, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    On macOS the framework launcher rewrites ``argv[0]`` shortly after
    the exec, so a reading taken the instant a process is spawned can
    spell the command line differently from a reading taken a moment
    later.  The identity check compares command lines, so the
    *recorded* one has to be the settled one.
    """
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _record_for(proc, ident, *, role: str = "executor",
                plan_id: str = PLAN_ID) -> ledger.ProcessRecord:
    """The ledger record of ``proc`` — taken while it is still alive."""
    return ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time if ident.create_time is not None else 0.0,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )


def _record_in_ledger(plan_dir: Path, proc, *, role: str = "executor",
                      plan_id: str = PLAN_ID) -> ledger.ProcessRecord:
    """Write a spawn record for ``proc`` into the plan's ledger."""
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = _record_for(proc, ident, role=role, plan_id=plan_id)
    ledger.record_spawn(plan_dir, record)
    return record


def _seed_plan(plans_dir: Path, db_path: Path, *, plan_id: str = PLAN_ID,
               phase: str = "queued"):
    """A plan ready to start: tasks, phase, routing row, execution row.

    The waiting phase is ``queued``, not ``ready``: 2026-10-25 (R10)
    retired ``ready`` as a phase name — it survives only as an input
    alias — and ``POST /api/execution/{id}/start`` CASes on the routing
    row against ``server._EXECUTION_START_SOURCE_PHASES``, which lists
    ``queued`` and no longer lists ``ready``.  A routing row parked at
    the retired word is refused with ``409 stage_mismatch`` — the start
    route is right and the fixture would be out of date.  (The same
    repair was made to ``test_exec_pid_cleared_on_terminal.py`` and
    ``test_execution_resume_start.py``; see
    ``backend/tests/static_gates/test_reconciled_start_seed_phases.py``.)
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "first task"}]}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": phase,
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
        )
    finally:
        conn.close()

    return plan_dir, project_dir


def _read_routing_phase(plan_id: str, db_path: Path) -> Optional[str]:
    """The phase the routing row carries right now."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _the_process_refuses_to_die(monkeypatch):
    """Make the reaper see the process as still alive after SIGKILL.

    Nothing survives SIGKILL for real, so the sweep is told the
    process is still there.  That is what a survivor looks like to the
    reaper: it escalated and the process did not go away.
    """
    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(process_reaper, "_KILL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(
        server, "_resolve_stop_grace_seconds",
        lambda grace_seconds=None: 0.05,
    )


class _DormantThread:
    """A ``threading.Thread`` stub that never actually starts a thread.

    The route starts a daemon thread to watch the spawned executor.
    The unit of these tests is the *decision* the route makes before
    the spawn, so the watcher has nothing to do — and a real thread
    would keep working on the plan after the test ended.
    """

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


class _FakeSpawnedProc:
    """The executor subprocess the route spawns — a stub, never a process."""

    def __init__(self) -> None:
        self.pid = 5150
        self.returncode = 0

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)
    server._execution_state.clear()
    yield plans_dir, db_path
    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def spawns(monkeypatch):
    """Record every executor spawn; never spawn a real one.

    ``server._spawn_executor_subprocess`` IS
    ``routes/execution.py::_spawn_executor_subprocess`` — the single
    place an executor subprocess comes into existence.
    """
    calls: List[Dict[str, Any]] = []

    def _spawn(**kwargs):
        calls.append(kwargs)
        return _FakeSpawnedProc(), Path(str(kwargs.get("log_path") or "/dev/null"))

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _spawn)
    return calls


# ---------------------------------------------------------------------------
# 1) one executor per plan
# ---------------------------------------------------------------------------


def test_start_returns_409_when_live_executor_in_ledger(env, client, spawns):
    """A plan that already has a live executor is not started again.

    This is the shape a backend restart leaves behind: the in-memory
    handle is gone, the ledger is not.  The old code asked the handle,
    found nothing and spawned a second executor for a plan whose first
    one was still running.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="executor")

        resp = client.post(
            f"/api/execution/{PLAN_ID}/start",
            json={"project_dir": str(project_dir)},
        )

        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body == {
            "error": "already_running",
            "pids": [child.pid],
        }, body
        assert spawns == [], (
            "the refused start spawned an executor anyway"
        )
        assert child.poll() is None, (
            "the refusal killed the running executor; it is a refusal, "
            "not a stop"
        )
        assert _read_routing_phase(PLAN_ID, db_path) == "queued", (
            "the refusal moved the plan's phase; a plan that is not "
            "running must not read as 'executing'"
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# 2) force reclaims first
# ---------------------------------------------------------------------------


def test_force_start_reaps_then_spawns(env, client, spawns):
    """``?force=true`` sweeps the plan's tree, then starts.

    The sweep is what makes the second executor safe: the first one
    is gone before the second one exists.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="executor")

        resp = client.post(
            f"/api/execution/{PLAN_ID}/start?force=true",
            json={"project_dir": str(project_dir)},
        )

        assert resp.status_code == 200, resp.text
        child.wait(timeout=15)
        assert child.poll() is not None, (
            "the forced start spawned a second executor while the first "
            "one was still running"
        )
        assert len(spawns) == 1, spawns
        killed = [
            int(event["pid"])
            for event in ledger.read_events(plan_dir)
            if event.get("event") == "killed" and event.get("pid") is not None
        ]
        assert killed == [child.pid], (
            "the forced start did not reap the plan's tree from the ledger"
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# 3) an unclean sweep does not start
# ---------------------------------------------------------------------------


def test_force_start_returns_kill_incomplete_when_survivor_remains(
    env, client, spawns, monkeypatch
):
    """A sweep that could not take everything down does not start.

    The answer names what survived: an operator who sees "started"
    stops looking, and the process that is still burning tokens is
    then nobody's problem.  The vocabulary and the shape are the stop's
    — ``kill_incomplete`` plus the pids that are still alive.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="executor")
        _the_process_refuses_to_die(monkeypatch)

        resp = client.post(
            f"/api/execution/{PLAN_ID}/start?force=true",
            json={"project_dir": str(project_dir)},
        )

        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body == {
            "error": "kill_incomplete",
            "pids": [child.pid],
        }, body
        assert spawns == [], (
            "the start went ahead although the sweep left a survivor"
        )
        assert _read_routing_phase(PLAN_ID, db_path) == "queued", (
            "the refusal moved the plan's phase; a plan that is not "
            "running must not read as 'executing'"
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# 4) dead records do not block
# ---------------------------------------------------------------------------


def test_start_proceeds_when_ledger_has_only_dead_records(env, client, spawns):
    """A ledger that only knows processes that are gone starts normally.

    Two shapes of "dead", both of which must not refuse a start:

      * a spawn record followed by an exit record — the ledger knows
        the process and knows it ended;
      * a spawn record with no exit record — the run ended without
        writing one (the backend was killed), and the process behind
        the pid is gone.

    The second shape is why the guard asks the kernel and not the
    record: a pid on its own does not say whether the process behind
    it is still running, and a ledger that kept refusing starts on the
    strength of a dead pid would lock the plan out for ever.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)

    exited = _spawn_child()
    stale = _spawn_child()
    try:
        # (a) spawn + exit: the ledger knows it and knows it ended.
        _record_in_ledger(plan_dir, exited, role="executor")
        exited.kill()
        exited.wait(timeout=15)
        ledger.record_exit(
            plan_dir, exited.pid, ledger.EVENT_EXIT, exit_code=0,
        )

        # (b) spawn with no exit record: the process is gone, the
        # ledger still lists it.
        ident = _stable_identity(stale.pid)
        assert ident is not None, "the test child has no readable identity"
        stale.kill()
        stale.wait(timeout=15)
        ledger.record_spawn(plan_dir, _record_for(stale, ident))

        resp = client.post(
            f"/api/execution/{PLAN_ID}/start",
            json={"project_dir": str(project_dir)},
        )

        assert resp.status_code == 200, resp.text
        assert len(spawns) == 1, spawns
    finally:
        _cleanup(exited, stale)
