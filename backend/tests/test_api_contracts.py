"""TDD tests for execution API response schema contracts."""

import json
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Clear global execution state and redirect PLANS_DIR before each test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    # The /api/plans response cache (2s TTL) survives across tests and
    # would otherwise serve a stale payload from a sibling test.
    import server

    server._PLANS_CACHE.clear()
    yield
    _execution_state.clear()
    _execution_locks.clear()
    server._PLANS_CACHE.clear()


VALID_STATUSES = {"running", "completed", "failed", "stopped", "not_started"}
VALID_STOP_REASONS = {None, "user_requested", "non_zero_exit", "process_died_unexpectedly"}


def _setup_plan(plan_id: str) -> Path:
    """Create a minimal plan directory with tasks.json and a default plan_state."""
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
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


class TestStatusResponseSchema:
    """GET /api/execution/{id}/status response shape."""

    def test_status_response_schema(self):
        """Response contains started_at, ended_at, stop_reason with correct types."""
        plan_id = "test-schema"
        plan_dir = _setup_plan(plan_id)
        _execution_state[plan_id] = {
            "status": "running",
            "logs": deque(["log1"], maxlen=100),
            "project_dir": str(plan_dir / "project"),
            "process": None,
            "started_at": "2026-01-01T00:00:00",
            "ended_at": None,
            "pid": None,
            "stop_reason": None,
        }

        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200
        data = resp.json()

        assert "started_at" in data
        assert "ended_at" in data
        assert "stop_reason" in data
        assert isinstance(data["started_at"], str)
        assert data["ended_at"] is None or isinstance(data["ended_at"], str)
        assert data["stop_reason"] is None or isinstance(data["stop_reason"], str)

    def test_status_enum_valid(self):
        """status is in the legal set."""
        plan_id = "test-status-enum"
        plan_dir = _setup_plan(plan_id)
        _execution_state[plan_id] = {
            "status": "running",
            "logs": deque(maxlen=100),
            "project_dir": str(plan_dir / "project"),
            "process": None,
            "started_at": "2026-01-01T00:00:00",
            "ended_at": None,
            "pid": None,
            "stop_reason": None,
        }

        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in VALID_STATUSES

    def test_stop_reason_enum(self):
        """stop_reason is in the legal set."""
        plan_id = "test-stop-enum"
        plan_dir = _setup_plan(plan_id)
        _execution_state[plan_id] = {
            "status": "failed",
            "logs": deque(maxlen=100),
            "project_dir": str(plan_dir / "project"),
            "process": None,
            "started_at": "2026-01-01T00:00:00",
            "ended_at": "2026-01-01T01:00:00",
            "pid": None,
            "stop_reason": "non_zero_exit",
        }

        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stop_reason"] in VALID_STOP_REASONS

    def test_stop_reason_out_of_vocab_free_form_ok(self):
        """2026-09-14 regression: the live state.db carries historical
        stop_reason values outside the old Literal enum (e.g.
        ``executor_killed_infinite_loop``, ``"pre-restart cleanup: …"``)
        and the strict Literal made this endpoint 500 on every poll.
        stop_reason is a free-form str and must round-trip as-is.
        """
        plan_id = "test-stop-oov"
        plan_dir = _setup_plan(plan_id)
        for free_form in ("executor_killed_infinite_loop",
                          "pre-restart cleanup: stale running row, no live pid"):
            _execution_state[plan_id] = {
                "status": "failed",
                "logs": deque(maxlen=100),
                "project_dir": str(plan_dir / "project"),
                "process": None,
                "started_at": "2026-01-01T00:00:00",
                "ended_at": "2026-01-01T01:00:00",
                "pid": None,
                "stop_reason": free_form,
            }

            resp = client.get(f"/api/execution/{plan_id}/status")
            assert resp.status_code == 200, (
                f"free-form stop_reason {free_form!r} must not 500"
            )
            data = resp.json()
            assert data["stop_reason"] == free_form

            prog = client.get(f"/api/execution/{plan_id}/progress")
            assert prog.status_code == 200, (
                f"free-form execution_stop_reason {free_form!r} must not 500"
            )
            assert prog.json()["execution_stop_reason"] == free_form

    def test_completed_has_ended_at(self):
        """Completed execution must have ended_at set."""
        plan_id = "test-completed"
        plan_dir = _setup_plan(plan_id)
        _execution_state[plan_id] = {
            "status": "completed",
            "logs": deque(maxlen=100),
            "project_dir": str(plan_dir / "project"),
            "process": None,
            "started_at": "2026-01-01T00:00:00",
            "ended_at": "2026-01-01T01:00:00",
            "pid": None,
            "stop_reason": None,
        }

        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["ended_at"] is not None

    def test_not_started_default(self):
        """No execution.json and no in-memory state → status='not_started'."""
        plan_id = "test-not-started"
        _setup_plan(plan_id)

        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "not_started"
        assert data["started_at"] is None
        assert data["ended_at"] is None
        assert data["stop_reason"] is None


class TestProgressResponseSchema:
    """GET /api/execution/{id}/progress response shape."""

    def test_progress_includes_lifecycle_fields(self):
        """Response contains started_at, ended_at, execution_stop_reason."""
        plan_id = "test-progress"
        plan_dir = _setup_plan(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "tasks.json").write_text(
            json.dumps({"tasks": [{"id": "1", "title": "T", "status": "in_progress"}]}),
            encoding="utf-8",
        )
        _execution_state[plan_id] = {
            "status": "running",
            "logs": deque(maxlen=100),
            "project_dir": str(project_dir),
            "process": None,
            "started_at": "2026-01-01T00:00:00",
            "ended_at": None,
            "pid": None,
            "stop_reason": None,
        }

        resp = client.get(f"/api/execution/{plan_id}/progress")
        assert resp.status_code == 200
        data = resp.json()
        assert "started_at" in data
        assert "ended_at" in data
        assert "execution_stop_reason" in data
        assert data["started_at"] == "2026-01-01T00:00:00"
        assert data["ended_at"] is None
        assert data["execution_stop_reason"] is None


class TestPlanSummaryExecution:
    """GET /api/plan/{id}/summary execution block."""

    def test_summary_includes_execution(self):
        """/summary contains execution summary with started_at, ended_at, stop_reason."""
        plan_id = "test-summary"
        plan_dir = _setup_plan(plan_id)
        _execution_state[plan_id] = {
            "status": "failed",
            "logs": deque(maxlen=100),
            "project_dir": str(plan_dir / "project"),
            "process": None,
            "started_at": "2026-01-01T00:00:00",
            "ended_at": "2026-01-01T01:00:00",
            "pid": None,
            "stop_reason": "non_zero_exit",
        }

        resp = client.get(f"/api/plan/{plan_id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        exec_block = data["execution"]
        assert "started_at" in exec_block
        assert "ended_at" in exec_block
        assert "stop_reason" in exec_block
        assert exec_block["status"] == "failed"
        assert exec_block["started_at"] == "2026-01-01T00:00:00"
        assert exec_block["ended_at"] == "2026-01-01T01:00:00"
        assert exec_block["stop_reason"] == "non_zero_exit"

    def test_summary_fallback_to_execution_row(self, state_db_reader):
        """When in-memory state is lost, /summary falls back to the SQLite plan_execution row.

        2026-09-13 port: the retired ``execution.json`` fallback was
        replaced by the ``plan_execution`` SQLite row (same
        crash-recovery source ``_recover_execution_states`` uses).
        """
        plan_id = "test-summary-fallback"
        plan_dir = _setup_plan(plan_id)
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
                current_phase="executing",
                create_if_missing=True,
                exec_status="stopped",
                stop_reason="user_requested",
                started_at="2026-02-01T00:00:00",
                project_dir=str(plan_dir / "project"),
            )
        finally:
            conn.close()

        resp = client.get(f"/api/plan/{plan_id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        exec_block = data["execution"]
        assert exec_block["status"] == "stopped"
        assert exec_block["started_at"] == "2026-02-01T00:00:00"
        assert exec_block["stop_reason"] == "user_requested"


class TestPlansList:
    """GET /api/plans — plan listing API contract.

    2026-09-13 contract update: the endpoint returns a bare JSON ARRAY
    (repository refactor + response cache); the frontend consumes both
    shapes (``Array.isArray(data) ? data : data.plans``). The old
    ``{"plans": [...]}`` wrapper is no longer produced.
    """

    def test_plans_list_returns_json_array(self):
        """Response is a JSON array of plan objects."""
        resp = client.get("/api/plans")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_plans_list_with_no_plans(self):
        """Empty plan directory returns empty list."""
        resp = client.get("/api/plans")
        assert resp.status_code == 200
        data = resp.json()
        assert data == []

    def test_plans_list_includes_plan_summary_fields(self):
        """Each plan in the list has id, requirement, status, and steps fields."""
        _setup_plan("test-plan-list")
        resp = client.get("/api/plans")
        assert resp.status_code == 200
        plans = resp.json()
        assert len(plans) >= 1
        plan = next(p for p in plans if p.get("id") == "test-plan-list")
        assert "id" in plan
        assert "requirement" in plan
        assert "status" in plan
        assert "steps" in plan

    def test_plans_list_steps_structure(self):
        """Steps object contains booleans for each document/phase."""
        _setup_plan("test-plan-steps")
        resp = client.get("/api/plans")
        assert resp.status_code == 200
        plan = next(p for p in resp.json() if p.get("id") == "test-plan-steps")
        steps = plan["steps"]
        expected_keys = ["interview", "prd", "review", "arch", "test", "tasks"]
        for key in expected_keys:
            assert key in steps, f"Missing step key: {key}"
            assert isinstance(steps[key], bool)
