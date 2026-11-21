"""The global stop counts a plan that answers ``stopping`` as handled.

2026-11-20 — the execution stop answers ``{"status": "stopping"}``
now: the work is being shut down and the phase is not final yet.  The
global stop reads that answer to decide two things — whether the plan
counts as stopped (``ok``) and whether it belongs to the batch the
next global start resumes.

Both answers have to survive the contract change:

* a plan whose stop is **in progress** is a plan the global stop did
  reach and did ask to stop — it is ``ok``, and it is in the batch.
  Reporting it as a failure would tell the operator the stop did not
  hold when it did, and dropping it from the batch would lose the
  resume point of a plan that is being paused;
* the batch is written **before** the stop finishes — the plan's resume
  point is written by the background layer, so the batch has to name
  the plan even though the plan is not parked yet.

What is pinned here
-------------------
1. the plan's row says ``ok: true``;
2. the plan is in the batch the next start reads;
3. the plan really does reach its terminal phase afterwards.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import stop_state
import stop_supervisor
import verification_cancel
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261120-stop-all-stopping-batch"

#: The batch record's file name — the same one the backend writes.
BATCH_FILENAME = "stop_all_batch.json"

#: The quiet window the background layer waits for (see the execution
#: stop suite for why the tests choose a short one).
QUIET_SECONDS = "0.2"


@pytest.fixture(autouse=True)
def _clean_process_state():
    stop_state._reset_for_tests()
    stop_supervisor._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_supervisor._reset_for_tests()
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test plans directory + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    monkeypatch.setenv("PDT_STOP_QUIET_SECONDS", QUIET_SECONDS)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, tmp_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _seed_running_plan(plans_dir: Path, db_path: Path):
    """A plan the executor is working on right now."""
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        '{"tasks": [{"id": "1", "title": "first task"}]}', encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "executing")
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID,
            project_dir=str(project_dir),
            exec_status="running",
        )
    finally:
        conn.close()

    server._execution_state[PLAN_ID] = {
        "status": "running",
        "logs": [],
        "project_dir": str(project_dir),
        "process": None,
        "started_at": "2026-11-20T12:00:00",
        "ended_at": None,
        "pid": None,
        "stop_reason": None,
    }
    return plan_dir


def _read_phase(db_path: Path, plan_id: str = PLAN_ID) -> str:
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _wait_for_phase(
    db_path: Path, phase: str, *, plan_id: str = PLAN_ID, timeout: float = 30.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_phase(db_path, plan_id) == phase:
            return True
        time.sleep(0.05)
    return _read_phase(db_path, plan_id) == phase


def _read_batch(root: Path) -> dict:
    return json.loads((root / BATCH_FILENAME).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_stop_all_counts_stopping_as_handled(env, client, monkeypatch):
    """A plan whose stop is still in progress is a handled plan.

    The global stop asks every running plan to stop and reports one row
    per plan.  A plan that answers ``stopping`` has been reached and
    has been asked — the work is still winding down, but the stop held.
    Reporting it as a failure would be the wrong answer, and leaving it
    out of the batch would throw away the resume point of a plan that
    is being paused.
    """
    from routes import execution as routes_execution

    plans_dir, root = env
    db_path = root / "state.db"
    _seed_running_plan(plans_dir, db_path)

    # The answer the global stop actually received.  The route imports
    # ``stop_execution`` inside the function, so it resolves the module
    # attribute at call time — patching the attribute is what makes the
    # spy see the real answer.
    seen: list = []
    real_stop = routes_execution.stop_execution

    def _spy(plan_id, *args, **kwargs):
        answer = real_stop(plan_id, *args, **kwargs)
        seen.append(answer)
        return answer

    monkeypatch.setattr(routes_execution, "stop_execution", _spy)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert seen, "the global stop did not stop the plan at all"
    assert seen[0].get("status") == "stopping", seen[0]

    results = {row["plan_id"]: row for row in body["results"]}
    assert results[PLAN_ID] == {
        "plan_id": PLAN_ID, "type": "execution", "ok": True,
    }, body

    batch = _read_batch(root)
    assert batch["version"] == 1, batch
    assert PLAN_ID in batch["plan_ids"], batch

    # The stop is asynchronous: the plan reaches its terminal phase
    # after the answer came back.
    assert _wait_for_phase(db_path, "stopped"), (
        f"the plan never reached the stopped phase — it is "
        f"{_read_phase(db_path)!r}"
    )
