"""System-wide stop and start — one call for every plan.

Restarting the backend is a two-step operation: stop everything that is
running, then start everything that was paused. Done per plan that is N
calls and N chances to forget one, and the plan that was forgotten
either keeps a subprocess alive across the restart or — the other way
round — is never resumed, and the operator is left with a plan that
"quietly stopped doing something".

What is pinned here:

1. **stop_all stops every running plan, of either kind.** An execution
   and a verification are two different subsystems; the operator should
   not have to know which one a plan is in.
2. **One plan's failure is one row, not a 500.** A plan that cannot be
   stopped (a conflict, say) is reported as ``ok: false`` and the other
   plans are still stopped — a global stop that gives up half way
   through is worse than one that reports what it could not do.
3. **start_all resumes every pause checkpoint**, dispatching each plan
   to the start that matches its kind.
4. **start_all also picks up the orphans a hard kill leaves behind** — a
   verification whose state was recovered from disk and whose thread is
   gone. Such a plan has no checkpoint (it was killed, not stopped), and
   without this source it could never be started again: its recovered
   state still says "running", so the start endpoint answers 409 forever.
5. **No targets is an empty answer**, not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import pause_state
import server
from routes import execution as routes_execution
from routes import verification as routes_verification
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate

EXEC_PLAN_ID = "20261020-sysall-exec"
VERIF_PLAN_ID = "20261020-sysall-verif"
ORPHAN_PLAN_ID = "20261020-sysall-orphan"


class Recorder:
    """Stands in for a stop/start route: records what it was asked to do.

    ``error`` makes it raise — the way a conflicting CAS does.
    ``outcome`` makes it return a fixed value — the way a verification
    stop answers a conflict (a 409 response, not an exception).
    """

    def __init__(
        self,
        error: Optional[BaseException] = None,
        outcome: Any = None,
    ) -> None:
        self.error = error
        self.outcome = outcome
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        plan_id = args[0] if args else kwargs.get("plan_id")
        self.calls.append({"plan_id": plan_id, "args": args, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        if self.outcome is not None:
            return self.outcome
        return {"plan_id": plan_id, "stopped": True}

    @property
    def plan_ids(self) -> List[Any]:
        return [call["plan_id"] for call in self.calls]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation, both state dicts emptied."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _mark_execution_running(plans_dir: Path, plan_id: str) -> Path:
    """A plan the executor is working on right now."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._execution_state[plan_id] = {
        "status": "running",
        "process": None,
        "ended_at": None,
        "logs": [],
        "project_dir": str(plan_dir / "project"),
        "started_at": "2026-10-20T11:00:00",
        "pid": None,
    }
    return plan_dir


def _mark_verification_running(plans_dir: Path, plan_id: str) -> Path:
    """A plan whose verification round is cycling right now."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._verification_state[plan_id] = {
        "verification_status": "running",
        "verification_round": 1,
        "started_at": "2026-10-20T11:00:00",
        "updated_at": "2026-10-20T11:00:00",
        "thread": None,
    }
    return plan_dir


def _write_checkpoint(
    plans_dir: Path,
    plan_id: str,
    *,
    kind: str = "execution",
) -> Path:
    """A paused plan directory: the checkpoint is what is left behind."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    pause_state.write_pause_checkpoint(
        plan_dir,
        kind=kind,
        project_dir=str(plan_dir / "project"),
        reason="user_requested",
        interrupted_tasks=[
            {
                "task_id": "1",
                "status": "in_progress",
                "phase": kind,
                "session_id": "sess-1",
                "paused_at": None,
                "strategy": "session_resume",
                "reason": "session_id_available",
            },
        ],
        unfinished_tasks=[{"task_id": "2", "status": "pending"}],
    )
    return plan_dir


# ---------------------------------------------------------------------------
# 1) stop_all stops every running plan, of either kind
# ---------------------------------------------------------------------------


def test_stop_all_two_plans(env, client, monkeypatch):
    """One execution and one verification, both stopped by one call."""
    plans_dir, _db_path = env
    _mark_execution_running(plans_dir, EXEC_PLAN_ID)
    _mark_verification_running(plans_dir, VERIF_PLAN_ID)

    exec_stop = Recorder()
    verif_stop = Recorder()
    monkeypatch.setattr(routes_execution, "stop_execution", exec_stop)
    monkeypatch.setattr(routes_verification, "stop_verification", verif_stop)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) == {"results"}, body
    results = {row["plan_id"]: row for row in body["results"]}
    assert set(results) == {EXEC_PLAN_ID, VERIF_PLAN_ID}, body

    # A success carries no "error" key at all — a consumer must not have
    # to check for one to know the plan was stopped.
    assert results[EXEC_PLAN_ID] == {
        "plan_id": EXEC_PLAN_ID, "type": "execution", "ok": True,
    }, body
    assert results[VERIF_PLAN_ID] == {
        "plan_id": VERIF_PLAN_ID, "type": "verification", "ok": True,
    }, body

    assert exec_stop.plan_ids == [EXEC_PLAN_ID], exec_stop.calls
    assert verif_stop.plan_ids == [VERIF_PLAN_ID], verif_stop.calls


# ---------------------------------------------------------------------------
# 2) one plan's failure is one row, not a 500
# ---------------------------------------------------------------------------


def test_stop_all_isolates_failure(env, client, monkeypatch):
    """A plan that refuses to stop does not stop the others."""
    plans_dir, _db_path = env
    _mark_execution_running(plans_dir, EXEC_PLAN_ID)
    _mark_verification_running(plans_dir, VERIF_PLAN_ID)

    exec_stop = Recorder(error=HTTPException(409, "conflict"))
    verif_stop = Recorder()
    monkeypatch.setattr(routes_execution, "stop_execution", exec_stop)
    monkeypatch.setattr(routes_verification, "stop_verification", verif_stop)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert set(results) == {EXEC_PLAN_ID, VERIF_PLAN_ID}, body

    failed = results[EXEC_PLAN_ID]
    assert failed["ok"] is False, failed
    assert failed["error"] == "conflict", failed

    # The other plan was still stopped — the failure is isolated.
    assert results[VERIF_PLAN_ID]["ok"] is True, results[VERIF_PLAN_ID]
    assert verif_stop.plan_ids == [VERIF_PLAN_ID], verif_stop.calls


def test_stop_all_reports_a_conflict_response(env, client, monkeypatch):
    """A conflict answered as a 409 response is still a failure.

    The verification routes answer a CAS conflict with a 409 response
    rather than by raising, so a dispatcher that only catches
    exceptions would report that plan as stopped — which it was not.
    """
    plans_dir, _db_path = env
    _mark_verification_running(plans_dir, VERIF_PLAN_ID)

    verif_stop = Recorder(
        outcome=JSONResponse(
            status_code=409,
            content={"error": "conflict", "reason": "stage_mismatch"},
        ),
    )
    monkeypatch.setattr(routes_verification, "stop_verification", verif_stop)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert results[VERIF_PLAN_ID]["ok"] is False, body
    assert results[VERIF_PLAN_ID]["error"] == "conflict", body


def test_stop_all_handles_one_plan_once(env, client, monkeypatch):
    """A plan that is somehow both kinds is stopped once, not twice.

    The two registries are independent, so a plan can be listed as an
    execution and as a verification at the same time. Stopping it twice
    would stop a phase that has already been stopped, and the second
    stop would report a conflict for a plan that is in fact fine.
    """
    plans_dir, _db_path = env
    _mark_execution_running(plans_dir, EXEC_PLAN_ID)
    _mark_verification_running(plans_dir, EXEC_PLAN_ID)

    exec_stop = Recorder()
    verif_stop = Recorder()
    monkeypatch.setattr(routes_execution, "stop_execution", exec_stop)
    monkeypatch.setattr(routes_verification, "stop_verification", verif_stop)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["results"]) == 1, body
    assert body["results"][0]["plan_id"] == EXEC_PLAN_ID, body
    assert exec_stop.plan_ids == [EXEC_PLAN_ID], exec_stop.calls
    assert verif_stop.calls == [], verif_stop.calls


# ---------------------------------------------------------------------------
# 3) start_all resumes every pause checkpoint
# ---------------------------------------------------------------------------


def test_start_all_resumes_checkpoints(env, client, monkeypatch):
    """Each checkpoint is resumed by the start that matches its kind."""
    plans_dir, _db_path = env
    _write_checkpoint(plans_dir, EXEC_PLAN_ID, kind="execution")
    _write_checkpoint(plans_dir, VERIF_PLAN_ID, kind="verification")

    exec_start = Recorder()
    verif_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert set(results) == {EXEC_PLAN_ID, VERIF_PLAN_ID}, body
    assert results[EXEC_PLAN_ID] == {
        "plan_id": EXEC_PLAN_ID, "type": "execution", "ok": True,
    }, body
    assert results[VERIF_PLAN_ID] == {
        "plan_id": VERIF_PLAN_ID, "type": "verification", "ok": True,
    }, body

    assert exec_start.plan_ids == [EXEC_PLAN_ID], exec_start.calls
    assert verif_start.plan_ids == [VERIF_PLAN_ID], verif_start.calls

    # The execution start is handed an explicitly empty request: the
    # checkpoint carries the project directory, and a request that named
    # one would redirect the resumed work somewhere else.
    request = exec_start.calls[0]["args"][1]
    assert isinstance(request, server.StartExecutionRequest), request
    assert request.project_dir is None, request


# ---------------------------------------------------------------------------
# 4) start_all picks up the orphans a hard kill leaves behind
# ---------------------------------------------------------------------------


def test_start_all_includes_recovered_verification(env, client, monkeypatch):
    """A verification whose thread is gone is started again.

    The backend was killed mid-round: the state was recovered from
    disk, so it still says "running" while nothing is running it. The
    start endpoint would answer 409 for that plan forever, so the
    global start has to name it explicitly.
    """
    plans_dir, _db_path = env
    plan_dir = plans_dir / ORPHAN_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._verification_state[ORPHAN_PLAN_ID] = {
        "verification_status": "running",
        "verification_round": 2,
        "recovered": True,
        "thread": None,
    }

    exec_start = Recorder()
    verif_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["results"] == [
        {
            "plan_id": ORPHAN_PLAN_ID, "type": "verification", "ok": True,
        },
    ], body
    assert verif_start.plan_ids == [ORPHAN_PLAN_ID], verif_start.calls
    assert exec_start.calls == [], exec_start.calls


def test_start_all_skips_a_live_verification(env, client, monkeypatch):
    """A verification that still owns a thread is not started again.

    The recovered marker is what makes a hard-killed plan resumable; a
    round that is genuinely running has a live thread and must be left
    alone, or the global start would run two rounds over one plan.
    """
    plans_dir, _db_path = env
    plan_dir = plans_dir / ORPHAN_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._verification_state[ORPHAN_PLAN_ID] = {
        "verification_status": "running",
        "verification_round": 2,
        "recovered": True,
        "thread": object(),
    }

    verif_start = Recorder()
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"results": []}, resp.text
    assert verif_start.calls == [], verif_start.calls


# ---------------------------------------------------------------------------
# 5) the dispatch reaches the real routes
# ---------------------------------------------------------------------------


def test_stop_all_reaches_the_real_execution_stop(env, client):
    """The global stop really stops the plan — checkpoint and all.

    The other tests patch the stop routes, so they prove the
    *dispatch*; this one runs the real one. A plan the executor is
    working on is stopped, and the pause it leaves behind is the same
    checkpoint a single-plan stop would leave — otherwise a restart
    would lose the resume point of every plan it stopped.
    """
    plans_dir, _db_path = env
    plan_dir = _mark_execution_running(plans_dir, EXEC_PLAN_ID)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert results[EXEC_PLAN_ID] == {
        "plan_id": EXEC_PLAN_ID, "type": "execution", "ok": True,
    }, body
    assert server._execution_state[EXEC_PLAN_ID]["status"] == "stopped"

    checkpoint = pause_state.read_pause_checkpoint(plan_dir)
    assert checkpoint is not None, "the global stop left no resume point"
    assert checkpoint["kind"] == "execution", checkpoint


def test_start_all_reaches_the_real_execution_start(env, client):
    """The global start really reaches the execution start.

    The plan carries a checkpoint but no tasks, so the real start
    refuses it — and the refusal is reported as a failed row rather
    than swallowed. A global start that hid the refusal would tell the
    operator a plan was resumed when it was not.
    """
    plans_dir, _db_path = env
    _write_checkpoint(plans_dir, EXEC_PLAN_ID, kind="execution")

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert results[EXEC_PLAN_ID]["ok"] is False, body
    assert "Tasks not found" in results[EXEC_PLAN_ID]["error"], body


# ---------------------------------------------------------------------------
# 6) no targets is an empty answer
# ---------------------------------------------------------------------------


def test_start_all_empty(env, client):
    """Nothing paused, nothing running: an empty list, not an error."""
    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"results": []}, resp.text


def test_stop_all_empty(env, client):
    """Nothing running: an empty list, not an error."""
    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"results": []}, resp.text
