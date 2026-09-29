import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks
from plan_state import PlanState

client = TestClient(app)

# 2026-09-13 port (SQLite decision): the retired ``execution.json`` file's
# reads/writes were removed from the backend; execution state persists to
# the ``plan_execution`` SQLite row. These tests assert ``exec_status`` /
# ``exec_pid`` / ``started_at`` against that row via the
# ``state_db_reader`` fixture (hermetic per-test ``PDT_STATE_DB_PATH`` DB,
# never the live ``state.db``). ``stop_reason`` / ``ended_at`` have no
# ``plan_execution`` column and remain runtime-only in-memory fields, so
# those assertions read ``_execution_state``.


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Clear global execution state and redirect PLANS_DIR before each test.

    Also stub out the auto-verification loop so background threads never
    spin up real LLM / puppeteer work that leaks into subsequent tests.
    """
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan_dir(plan_id):
    """Create a minimal plan directory with tasks.json and a default plan_state."""
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "ready",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )
    return plan_dir


class FakeProcess:
    """Fake subprocess.Popen for lifecycle tests."""

    def __init__(self, returncode=0, block_on_wait=None):
        self.pid = 12345
        self._returncode = returncode
        self._block = block_on_wait  # threading.Event
        self._terminated = False

    @property
    def returncode(self):
        return self._returncode

    @property
    def stdout(self):
        # When blocking, yield nothing but don't end the iterator until
        # the block event fires — this prevents the _run thread's
        # `for line in process.stdout` from finishing prematurely.
        if self._block is not None:
            self._block.wait(timeout=30)
        return iter([])

    def wait(self, timeout=None):
        if self._block is not None:
            self._block.wait(timeout=timeout or 30)
        return self._returncode

    def poll(self):
        if self._terminated or (self._block is not None and self._block.is_set()):
            return self._returncode
        return None

    def terminate(self):
        self._terminated = True
        if self._block is not None:
            self._block.set()


def _wait_for_db_status(state_db_reader, plan_id, target_status, timeout=5):
    """Poll the ``plan_execution`` SQLite row until ``exec_status`` matches."""
    for _ in range(int(timeout * 20)):
        row = state_db_reader.execution(plan_id)
        if row and row.get("exec_status") == target_status:
            return row
        time.sleep(0.05)
    return None


def _wait_for_plan_phase(plan_dir, target_phase, timeout=5):
    """Poll ``PlanState.get_current_phase()`` (plan_routing SQLite row)
    until the target phase is reached."""
    for _ in range(int(timeout * 20)):
        phase = PlanState(plan_dir).get_current_phase()
        if phase == target_phase:
            return {"current_phase": phase}
        time.sleep(0.05)
    return None


class TestExecutionLifecycle:
    """TDD tests for execution lifecycle management."""

    def test_start_sets_executing(self, monkeypatch, state_db_reader):
        """POST /start → plan_state='executing' and execution status='running'."""
        plan_id = "test-start"
        plan_dir = _setup_plan_dir(plan_id)

        # Use a blocking process so the status stays "running" for inspection
        block = threading.Event()
        monkeypatch.setattr(
            "server.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(returncode=0, block_on_wait=block),
        )

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(plan_dir / "project")},
        )
        assert resp.status_code == 200

        # Execution state in-memory
        assert _execution_state[plan_id]["status"] == "running"
        assert _execution_state[plan_id]["pid"] == 12345

        # plan_execution row persisted (SQLite is the source of truth)
        row = state_db_reader.execution(plan_id)
        assert row is not None, "plan_execution row missing after start"
        assert row["exec_status"] == "running"
        assert row["exec_pid"] == 12345
        assert row["started_at"] is not None

        # plan_state transitioned to executing
        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "executing"

        block.set()

    def test_normal_exit(self, monkeypatch, state_db_reader):
        """returncode=0 → status='completed', plan_state='completed'."""
        plan_id = "test-normal"
        plan_dir = _setup_plan_dir(plan_id)

        monkeypatch.setattr(
            "server.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(returncode=0),
        )

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(plan_dir / "project")},
        )
        assert resp.status_code == 200

        row = _wait_for_db_status(state_db_reader, plan_id, "completed")
        assert row is not None, "plan_execution exec_status never became 'completed'"
        # stop_reason / ended_at are runtime-only in-memory fields
        assert _execution_state[plan_id]["stop_reason"] is None
        assert _execution_state[plan_id]["ended_at"] is not None

        # Wait for background thread to finish plan_state transition
        ps_data = _wait_for_plan_phase(plan_dir, "completed")
        assert ps_data is not None
        assert ps_data["current_phase"] == "completed"

    def test_nonzero_exit(self, monkeypatch, state_db_reader):
        """returncode=1 → status='failed', stop_reason='non_zero_exit'."""
        plan_id = "test-fail"
        plan_dir = _setup_plan_dir(plan_id)

        monkeypatch.setattr(
            "server.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(returncode=1),
        )

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(plan_dir / "project")},
        )
        assert resp.status_code == 200

        row = _wait_for_db_status(state_db_reader, plan_id, "failed")
        assert row is not None, "plan_execution exec_status never became 'failed'"
        assert _execution_state[plan_id]["stop_reason"] == "non_zero_exit"
        assert _execution_state[plan_id]["ended_at"] is not None

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "failed"

    def test_user_stop(self, monkeypatch, state_db_reader):
        """POST /stop → status='stopped', stop_reason='user_requested'."""
        plan_id = "test-stop"
        plan_dir = _setup_plan_dir(plan_id)

        block = threading.Event()
        monkeypatch.setattr(
            "server.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(returncode=0, block_on_wait=block),
        )

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(plan_dir / "project")},
        )
        assert resp.status_code == 200

        # Give the thread a moment to reach wait()
        time.sleep(0.1)

        resp_stop = client.post(f"/api/execution/{plan_id}/stop")
        assert resp_stop.status_code == 200

        row = state_db_reader.execution(plan_id)
        assert row is not None, "plan_execution row missing after stop"
        assert row["exec_status"] == "stopped"
        # stop_reason / ended_at are runtime-only in-memory fields
        assert _execution_state[plan_id]["stop_reason"] == "user_requested"
        assert _execution_state[plan_id]["ended_at"] is not None

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "stopped"

        block.set()

    def test_run_python_exception(self, monkeypatch, state_db_reader):
        """Popen raises OSError → status='failed', plan_state='failed'."""
        plan_id = "test-exception"
        plan_dir = _setup_plan_dir(plan_id)

        def raise_oserror(*args, **kwargs):
            raise OSError("No such file")

        monkeypatch.setattr("server.subprocess.Popen", raise_oserror)

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(plan_dir / "project")},
        )
        assert resp.status_code == 200

        # The spawn-failure path persists a plan_execution row
        # (create_if_missing) so the failure is visible in SQLite.
        row = _wait_for_db_status(state_db_reader, plan_id, "failed")
        assert row is not None, "plan_execution exec_status never became 'failed'"
        assert row.get("stop_reason") == "process_died_unexpectedly"
        assert _execution_state[plan_id]["ended_at"] is not None

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "failed"

    def test_progress(self, monkeypatch):
        """GET /api/execution/{id}/progress returns plan_id, execution_status, tasks, counts."""
        plan_id = "test-progress"
        plan_dir = _setup_plan_dir(plan_id)

        # Create project_dir with tasks.json
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        tasks_data = {
            "tasks": [
                {"id": "1", "title": "Task One", "status": "completed"},
                {"id": "2", "title": "Task Two", "status": "in_progress"},
                {"id": "3", "title": "Task Three", "status": "pending"},
                {"id": "4", "title": "Task Four", "status": "failed"},
            ]
        }
        # The progress endpoint prefers <PLANS_DIR>/<plan_id>/tasks.json over
        # <project_dir>/tasks.json, so write the actual tasks there.
        (plan_dir / "tasks.json").write_text(json.dumps(tasks_data), encoding="utf-8")

        # The progress endpoint resolves project_dir via _get_project_dir,
        # which consults the in-memory _execution_state first, then the
        # plan_execution SQLite row. Seed the in-memory record directly
        # (no start call in this test).
        _execution_state[plan_id] = {
            "status": "running",
            "project_dir": str(project_dir),
        }

        resp = client.get(f"/api/execution/{plan_id}/progress")
        assert resp.status_code == 200
        data = resp.json()

        assert data["plan_id"] == plan_id
        assert "execution_status" in data
        assert "tasks" in data
        assert "counts" in data
        assert data["counts"]["total"] == 4
        assert data["counts"]["completed"] == 1
        assert data["counts"]["in_progress"] == 1
        assert data["counts"]["pending"] == 1
        assert data["counts"]["failed"] == 1
        assert data["current"] == {"id": "2", "title": "Task Two", "description": None}

    def test_progress_raw_list_format(self, monkeypatch):
        """GET /api/execution/{id}/progress handles raw list format (no {"tasks": wrapper})."""
        plan_id = "test-progress-raw-list"
        plan_dir = _setup_plan_dir(plan_id)

        # Create project_dir with tasks.json as raw array (not wrapped in {"tasks": [...]})
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        raw_list = [
            {"id": "1", "title": "Task One", "status": "completed"},
            {"id": "2", "title": "Task Two", "status": "in_progress"},
            {"id": "3", "title": "Task Three", "status": "pending"},
        ]
        # The progress endpoint prefers <PLANS_DIR>/<plan_id>/tasks.json.
        (plan_dir / "tasks.json").write_text(json.dumps(raw_list), encoding="utf-8")

        # The progress endpoint resolves project_dir via _get_project_dir:
        # seed the in-memory record directly (no start call in this test).
        _execution_state[plan_id] = {
            "status": "running",
            "project_dir": str(project_dir),
        }

        resp = client.get(f"/api/execution/{plan_id}/progress")
        assert resp.status_code == 200
        data = resp.json()

        assert data["plan_id"] == plan_id
        assert "execution_status" in data
        assert "tasks" in data
        assert "counts" in data
        assert data["counts"]["total"] == 3
        assert data["counts"]["completed"] == 1
        assert data["counts"]["in_progress"] == 1
        assert data["counts"]["pending"] == 1
        assert data["current"] == {"id": "2", "title": "Task Two", "description": None}
        # stop_reason/stop_detail should be None, not crash
        assert data["stop_reason"] is None
        assert data["stop_detail"] is None
