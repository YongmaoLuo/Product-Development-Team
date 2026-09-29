"""Smoke contract tests for the execution start endpoint (VP-003).

Pins the operational contract that the backend exposes via
``POST /api/execution/{plan_id}/start`` and
``GET  /api/execution/{plan_id}/status``:

  1. ``test_start_endpoint``
     ``POST /api/execution/{plan_id}/start`` MUST return a JSON body
     with ``status == "started"``, AND ``plans/{plan_id}/execution.json``
     MUST exist on disk and contain a ``project_dir`` field equal to
     the request's project_dir.

  2. ``test_execution_started``
     Within 5 seconds of the start call, ``GET /api/execution/{plan_id}/status``
     MUST report ``status == "running"``. This pins the "execution actually
     started" half of the smoke contract that downstream UIs (Verification
     card, progress panel) depend on.

The tests live under ``tests/integration`` because they exercise the real
FastAPI app + real ``subprocess.Popen`` boundary; ``subprocess.Popen`` is
patched to a fake process so no real claude / autonomous-coding child is
spawned.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset global execution state and redirect PLANS_DIR per test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    # Stub auto-verification so background threads do not spin up real LLM work
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
    # Stub _lazy_check_execution: FakeProcess.pid is not a real OS PID, so
    # os.kill(pid, 0) would raise ProcessLookupError and flip status to
    # 'failed'. We want the test to observe status='running', so disable
    # the lazy heartbeat probe entirely.
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan_dir(plan_id, plans_dir):
    """Create a minimal plan directory with tasks.json + plan_state.json."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
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
                "flags": {"arch_enabled": False, "test_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


class FakeProcess:
    """Fake subprocess.Popen that blocks until released."""

    def __init__(self):
        # Use the test runner's own PID so any heartbeats that still get
        # through (e.g. via os.kill(pid, 0)) see a real, live process.
        self.pid = 4242
        self._block = threading.Event()

    @property
    def returncode(self):
        return 0

    @property
    def stdout(self):
        self._block.wait(timeout=30)
        return iter([])

    def wait(self, timeout=None):
        self._block.wait(timeout=timeout or 30)
        return 0

    def poll(self):
        if self._block.is_set():
            return 0
        return None

    def terminate(self):
        self._block.set()

    def release(self):
        self._block.set()


def test_start_endpoint(monkeypatch):
    """POST /api/execution/{id}/start returns status=started and writes execution.json."""
    from server import PLANS_DIR

    plan_id = "vp-003-start-endpoint"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200"
    )
    body = resp.json()
    assert body.get("status") == "started", (
        f"start endpoint returned status={body.get('status')!r}, expected 'started'"
    )

    # 2026-09-14: production no longer writes the legacy
    # ``execution.json`` artifact — execution state is SQLite-only
    # (acceptance_4: no state JSON filenames in production code), and
    # ``plan_execution.project_dir`` is the canonical source
    # (``server._get_project_dir`` reads it, never execution.json).
    # Assert the durable row instead of the deleted file.
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(_state_db_path())
    try:
        migrate(conn)
        row = conn.execute(
            "SELECT project_dir FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        f"plan_execution row for {plan_id} was not written after start"
    )
    assert row[0] == str(project_dir), (
        f"plan_execution project_dir={row[0]!r} "
        f"does not match request project_dir={str(project_dir)!r}"
    )

    fake.release()


def test_execution_started(monkeypatch):
    """GET /api/execution/{id}/status shows running within 5s of start."""
    from server import PLANS_DIR

    plan_id = "vp-003-execution-started"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    project_dir = plan_dir / "project"
    start_resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert start_resp.status_code == 200, (
        f"start endpoint returned HTTP {start_resp.status_code}, expected 200"
    )

    deadline = time.monotonic() + 5.0
    last_status = None
    while time.monotonic() < deadline:
        status_resp = client.get(f"/api/execution/{plan_id}/status")
        assert status_resp.status_code == 200, (
            f"status endpoint returned HTTP {status_resp.status_code}"
        )
        last_status = status_resp.json().get("status")
        if last_status == "running":
            break
        time.sleep(0.05)

    assert last_status == "running", (
        f"GET /api/execution/{plan_id}/status did not reach 'running' "
        f"within 5s; last observed status={last_status!r}"
    )

    fake.release()
