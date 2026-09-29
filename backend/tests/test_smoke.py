import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks, heartbeat_monitor

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan_dir(plan_id, tmp_path):
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "ready",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
        }),
        encoding="utf-8",
    )
    return plan_dir


class FakeProcess:
    """Fake subprocess.Popen for smoke tests."""

    def __init__(self, returncode=0):
        self.pid = 12345
        self._returncode = returncode
        self._stdin = None

    @property
    def returncode(self):
        return self._returncode

    @property
    def stdout(self):
        return iter([])

    @property
    def stdin(self):
        return self._stdin

    def wait(self):
        return self._returncode

    def poll(self):
        return self._returncode

    def terminate(self):
        pass


# 2026-09-13 port (SQLite decision): the retired ``execution.json`` is
# neither read nor written by production. Smoke assertions read the
# ``plan_execution`` SQLite row via ``state_db_reader`` (hermetic
# per-test ``PDT_STATE_DB_PATH`` DB) and the in-memory ``_execution_state``
# for runtime-only fields (``stop_reason`` / ``ended_at`` have no
# ``plan_execution`` column).


def _seed_execution_row(plan_id: str, **fields) -> None:
    """Insert/update a ``plan_execution`` row in the hermetic test DB."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_phase(
            plan_id,
            current_phase=fields.pop("current_phase", "executing"),
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


class TestSmoke:
    def test_smoke_start_to_completed(self, tmp_path, monkeypatch, state_db_reader):
        """FakeProcess with returncode=0 → plan_state='completed' with execution."""
        plan_id = "smoke-completed"
        plan_dir = _setup_plan_dir(plan_id, tmp_path)

        monkeypatch.setattr(
            "server.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(returncode=0),
        )
        # Disable auto-verification to avoid subprocess mocking conflicts
        monkeypatch.setattr("server._run_auto_verification_loop", lambda *args, **kwargs: None)

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(plan_dir / "project")},
        )
        assert resp.status_code == 200

        # Wait for the subprocess to finish
        time.sleep(0.5)

        # plan_execution row (SQLite is the source of truth)
        row = state_db_reader.execution(plan_id)
        assert row is not None, "plan_execution row missing after start"
        assert row["exec_status"] == "completed"
        # stop_reason / ended_at are runtime-only in-memory fields
        assert _execution_state[plan_id]["stop_reason"] is None
        assert _execution_state[plan_id]["ended_at"] is not None

    def test_smoke_restart_recovery(self, tmp_path, monkeypatch):
        """A stale running plan_execution row with dead pid → recovery marks failed."""
        plan_id = "smoke-restart"
        plan_dir = _setup_plan_dir(plan_id, tmp_path)

        # Simulate a stale running plan_execution row with dead PID
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=99999,  # non-existent
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        # Trigger recovery
        from server import _recover_execution_states
        _recover_execution_states(tmp_path / "plans")

        # Should be marked failed
        assert _execution_state[plan_id]["status"] == "failed"
        assert _execution_state[plan_id]["stop_reason"] == "process_died_unexpectedly"

        # plan_state should be failed
        from plan_state import PlanState
        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "failed"

    def test_smoke_kill9(self, tmp_path, monkeypatch):
        """Start a real subprocess, kill -9 it, then lazy check marks failed."""
        plan_id = "smoke-kill9"
        plan_dir = _setup_plan_dir(plan_id, tmp_path)

        # Start a real long-running subprocess via the API
        real_proc = subprocess.Popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Inject the real process into execution state
        _execution_state[plan_id] = {
            "status": "running",
            "pid": real_proc.pid,
            "started_at": "2024-01-01T00:00:00",
            "project_dir": str(plan_dir / "project"),
        }
        (plan_dir / "execution.json").write_text(
            json.dumps(_execution_state[plan_id]), encoding="utf-8"
        )

        # Kill the process
        real_proc.kill()
        real_proc.wait()

        # Lazy check should detect death
        from server import _lazy_check_execution
        _lazy_check_execution(plan_id)

        assert _execution_state[plan_id]["status"] == "failed"
        assert _execution_state[plan_id]["stop_reason"] == "process_died_unexpectedly"
