"""2026-10-30: ``POST /api/execution/{id}/stop`` must kill the plan's tree.

The bug
-------
``_stop_running_execution`` killed exactly one thing: the process
handle in ``_execution_state``.  That handle dies with the backend
process, so after a restart the endpoint had nothing to kill — and it
answered ``{"status": "stopped"}`` anyway.  The plan was marked
stopped, the phase was parked, a checkpoint was written, and every
process the plan still owned went on running: the executor, its
sub-agents, the VP services.  A stop that kills nothing and says
"stopped" is worse than no stop at all, because the operator believes
the work is over.

The ledger (``backend/process_ledger.py``) is the durable answer to
"which pids does this plan still own", and the reaper
(``backend/process_reaper.py``) is what acts on it: children first,
every signal preceded by an identity check (a pid alone does not
identify a process), and a process that cannot be identified reported
rather than guessed at.

What is pinned here:

  1. **the ledger is the authority** — with no handle in memory, the
     stop still kills what the ledger records.
  2. **"stopped" means dead** — a survivor turns the answer into
     ``kill_incomplete`` plus the surviving pids.
  3. **an incomplete stop keeps the pid** — the row must not claim the
     plan is stopped while its process is still alive.
  4. **no ledger, no handle, no kill** — the idle semantics stay
     exactly as they were, and nothing pretends to have been killed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
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


PLAN_ID = "20261030-stop-uses-ledger"


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


def _record_in_ledger(plan_dir: Path, proc, *, role: str = "executor",
                      plan_id: str = PLAN_ID):
    """Write a spawn record for ``proc`` into the plan's ledger."""
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _seed_plan(plans_dir: Path, db_path: Path, *, plan_id: str = PLAN_ID,
               phase: str = "executing", exec_status: str = "running",
               exec_pid: Optional[int] = None):
    """A plan mid-flight, with a routing row, a phase and a run state."""
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
            "completed_phases": ["execution"],
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
            exec_status=exec_status,
            exec_pid=exec_pid,
        )
    finally:
        conn.close()

    return plan_dir, project_dir


def _mark_running_without_handle(plan_id: str, project_dir: Path,
                                 pid: Optional[int] = None):
    """The state a backend restart leaves behind: running, no handle.

    ``_restore_execution_state`` puts ``process: None`` back on purpose
    — the handle died with the backend, and the ledger, not this dict,
    is what says which pids the plan still owns.
    """
    server._execution_state[plan_id] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(project_dir),
        "process": None,
        "started_at": "2026-10-30T04:20:00",
        "ended_at": None,
        "pid": pid,
        "stop_reason": None,
    }


def _read_exec_row(plan_id: str, db_path: Path) -> Dict[str, Any]:
    """The execution row as it is on disk right now."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        row = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    return dict(row or {})


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


def _killed_pids(plan_dir: Path) -> List[int]:
    """The pids the ledger says the stop killed."""
    return [
        int(event["pid"])
        for event in ledger.read_events(plan_dir)
        if event.get("event") == "killed" and event.get("pid") is not None
    ]


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
    server._execution_state.clear()
    yield plans_dir, db_path
    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# 1) the ledger is the authority
# ---------------------------------------------------------------------------


def test_stop_without_handle_kills_from_ledger(env, client):
    """No handle in memory; the ledger still says who to kill.

    This is the shape a backend restart leaves behind: the in-memory
    handle is gone, the ledger is not.  The old code marked the plan
    stopped, wrote a checkpoint, parked the phase — and killed nothing.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="executor")
        _mark_running_without_handle(PLAN_ID, project_dir, pid=child.pid)

        resp = client.post(f"/api/execution/{PLAN_ID}/stop")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stopped", resp.json()

        child.wait(timeout=15)
        assert child.poll() is not None, (
            "the stop answered 'stopped' but the process the ledger "
            "records for this plan is still running"
        )
        assert _killed_pids(plan_dir) == [child.pid], (
            "the kill did not come from the ledger"
        )
    finally:
        _cleanup(child)


def test_stop_without_any_state_still_kills_from_the_ledger(env, client):
    """The plan is not in memory at all — the ledger is still enough.

    The handle is not the only way a plan can own processes: a
    sub-agent or a VP service the backend started outlives the
    backend.  A plan the backend has no record of must still be
    stoppable.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="subagent")
        server._execution_state.pop(PLAN_ID, None)

        resp = client.post(f"/api/execution/{PLAN_ID}/stop")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stopped", resp.json()
        child.wait(timeout=15)
        assert child.poll() is not None, (
            "the plan was not in memory, so the stop killed nothing — "
            "and said 'stopped' anyway"
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# 2) "stopped" has to mean dead
# ---------------------------------------------------------------------------


def test_stop_returns_kill_incomplete_when_survivor_remains(
    env, client, monkeypatch
):
    """A process that survived turns the answer into ``kill_incomplete``.

    The answer has to name what is still running: an operator who sees
    "stopped" stops looking, and the process that is still burning
    tokens is then nobody's problem.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="executor")
        _mark_running_without_handle(PLAN_ID, project_dir, pid=child.pid)
        _the_process_refuses_to_die(monkeypatch)

        resp = client.post(f"/api/execution/{PLAN_ID}/stop")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "kill_incomplete", body
        assert body["surviving_pids"] == [child.pid], body
    finally:
        _cleanup(child)


def test_stop_keeps_exec_pid_when_incomplete(env, client, monkeypatch):
    """An incomplete stop must not clear the pid.

    The row is marked "stopped" *before* the kill — the executor's
    watcher thread and the heartbeat watchdog both act on a row that
    still says "running".  When the kill does not hold, that mark is a
    lie: the plan is still running, and the row has to say so, with
    the pid that is still alive.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    child = _spawn_child()
    try:
        _record_in_ledger(plan_dir, child, role="executor")
        _mark_running_without_handle(PLAN_ID, project_dir, pid=child.pid)
        conn = open_db(db_path)
        try:
            migrate(conn)
            ExecutionRepository(conn).update_fields(
                PLAN_ID, exec_status="running", exec_pid=child.pid,
            )
        finally:
            conn.close()
        _the_process_refuses_to_die(monkeypatch)

        resp = client.post(f"/api/execution/{PLAN_ID}/stop")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "kill_incomplete", resp.json()
        row = _read_exec_row(PLAN_ID, db_path)
        assert row["exec_pid"] == child.pid, (
            f"the incomplete stop cleared exec_pid; row={row!r}"
        )
        assert row["exec_status"] != "stopped", (
            f"the incomplete stop marked the plan stopped; row={row!r}"
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# 3) nothing to kill — the idle semantics stay as they were
# ---------------------------------------------------------------------------


def test_stop_without_handle_and_without_ledger_keeps_idle_semantics(
    env, client
):
    """No handle, no ledger: the old idle answer, and no invented kill.

    The ledger only ever *adds* to what a stop knows.  A plan the
    ledger says nothing about is a plan with nothing to kill, and the
    answer must not claim that anything was killed.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path, exec_status="not_started",
    )
    server._execution_state.pop(PLAN_ID, None)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopped", body
    assert body["paused"] is False, body
    assert body["interrupted_task_count"] == 0, body
    assert "surviving_pids" not in body, body
    assert _killed_pids(plan_dir) == [], (
        "a stop that killed nothing must not write a killed event"
    )
    assert not (plan_dir / "pause_state.json").exists(), (
        "the idle path writes no resume point"
    )
