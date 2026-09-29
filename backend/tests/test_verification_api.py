"""Contract and boundary tests for verification API endpoints.

TDD Specs:
- test_response_structure: all API responses ->符合jsonschema定义
- test_status_codes: various scenarios -> HTTP status codes correct
- test_error_messages: error responses -> contain error and detail fields, friendly messages
- test_boundary_values: boundary conditions -> return 400 or 404
- test_concurrency: concurrent start verification -> only one succeeds, other returns 409
- test_frontend_contract: mock server response -> frontend JavaScript contract validation
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import requests

pytestmark = pytest.mark.integration
import requests_mock
from fastapi.testclient import TestClient
from jsonschema import validate

from server import app, _verification_state


def _delete_plan_rows(plan_id: str) -> None:
    """Remove the state-machine rows ``/start`` writes for ``plan_id``.

    These boundary tests talk to the real ``_state_db_path()`` (there is
    no per-test patch), so a start's init_round + stage CAS persist on
    disk across posts within the same test.
    """
    import sqlite3
    from server import _state_db_path

    conn = sqlite3.connect(_state_db_path())
    try:
        for table in ("plan_verification", "plan_tasks"):
            conn.execute(f"DELETE FROM {table} WHERE plan_id = ?", (plan_id,))
        # The start handler's phase CAS requires an existing routing row,
        # so instead of deleting it we rewind it to the pre-start phase.
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? "
            "WHERE plan_id = ?",
            ("executing", plan_id),
        )
        conn.commit()
    finally:
        conn.close()

client = TestClient(app)

# ---------------------------------------------------------------------------
# JSON Schema definitions
# ---------------------------------------------------------------------------

VERIFICATION_STATUS_SCHEMA = {
    "type": "object",
    "required": [
        "plan_id",
        "verification_status",
        "verification_round",
        "verification_max_rounds",
        "results",
        "repair_tasks",
        "started_at",
        "updated_at",
    ],
    "properties": {
        "plan_id": {"type": "string"},
        "verification_status": {
            "type": "string",
            "enum": [
                "not_started",
                "running",
                "passed",
                "failed",
                "loop_stopped",
                "verification_failed",
                "pending",
            ],
        },
        "verification_round": {"type": "integer", "minimum": 0},
        "verification_max_rounds": {"type": "integer", "minimum": 1, "maximum": 10},
        "results": {"type": "object"},
        "repair_tasks": {"type": "array"},
        "started_at": {"type": ["string", "null"]},
        "updated_at": {"type": ["string", "null"]},
    },
}

START_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["plan_id", "status"],
    "properties": {
        "plan_id": {"type": "string"},
        "status": {"type": "string", "enum": ["started"]},
    },
}

STOP_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["stopped_at", "reason", "current_round"],
    "properties": {
        "stopped_at": {"type": "string"},
        "reason": {"type": "string"},
        "current_round": {"type": "integer", "minimum": 0},
    },
}

REPAIR_TASKS_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "description", "test_command", "failure_reason"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "test_command": {"type": "string"},
                    "failure_reason": {"type": "string"},
                },
            },
        },
    },
}

ERROR_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["error", "detail"],
    "properties": {
        "error": {"type": "string", "minLength": 1},
        "detail": {"type": "string", "minLength": 1},
    },
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_verification_state(monkeypatch, tmp_path):
    """Clear global verification state and redirect the plans root per test.

    2026-09-13/14: ``server.PLANS_DIR`` is bound **once at import time**,
    so the ``setattr`` below only covers the server module's own reads.
    Every other plans-directory consumer (``plan_state``,
    ``execution_logger``, ``notifications.plan_dir_resolver``, …) goes
    through ``config_paths.resolve_plans_dir()``, which gives the
    ``PDT_PLANS_DIR`` environment variable precedence. Pointing both at
    the same ``tmp_path`` keeps the two views consistent — otherwise a
    fixture that writes its artifacts under ``tmp_path`` would be read
    back from the shared conftest scratch root, or worse, from the
    operator's live ``plans/`` tree.
    """
    _verification_state.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    yield
    _verification_state.clear()


@pytest.fixture
def sample_plan_factory(tmp_path, plan_sqlite_seeder):
    """Factory that creates a minimal plan directory.

    Materialises the plan in **both** stores. The on-disk artifacts
    (``plan_state.json`` / ``interview.json`` / ``prd.json`` /
    ``tasks.json``) are what the legacy readers expect, but production
    is SQLite-first — ``PlanState`` reads ``plan_routing`` and the
    ``/api/verification/{id}/start`` endpoint reads
    ``plan_execution.project_dir``. A fixture that only wrote the JSON
    files looked complete on disk while every endpoint answered 404.

    ``with_project_dir=False`` reproduces the "plan exists but no target
    project has been recorded yet" shape, which ``/start`` must reject
    with 400.
    """

    def _make(plan_id="test-plan", phase="executing", with_project_dir=True,
              verification_status=None, verification_round=None,
              verification_results=None, max_rounds=None):
        plan_dir = tmp_path / "plans" / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        (plan_dir / "plan_state.json").write_text(
            json.dumps(
                {
                    "plan_id": plan_id,
                    "current_phase": phase,
                    "completed_phases": ["execution"] if phase == "executing" else [],
                    "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                    "flags": {},
                    "last_updated": "2026-01-01T00:00:00",
                    "verification": {
                        "status": "not_started",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    },
                }
            ),
            encoding="utf-8",
        )

        (plan_dir / "interview.json").write_text(
            json.dumps({"requirement": "test requirement"}),
            encoding="utf-8",
        )

        (plan_dir / "prd.json").write_text(
            json.dumps({"prd": "test prd"}),
            encoding="utf-8",
        )

        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": []}),
            encoding="utf-8",
        )

        project_dir = None
        if with_project_dir:
            project_dir = plan_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)
            (plan_dir / "execution.json").write_text(
                json.dumps({"project_dir": str(project_dir)}),
                encoding="utf-8",
            )

        plan_sqlite_seeder(
            plan_dir,
            plan_id,
            phase=phase,
            project_dir=str(project_dir) if project_dir else None,
            verification_status=verification_status,
            verification_round=verification_round,
            verification_results=verification_results,
            max_rounds=max_rounds,
        )

        return plan_dir

    return _make


# ---------------------------------------------------------------------------
# 1. Response structure tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestResponseStructure:
    """All API responses ->符合jsonschema定义."""

    def _setup_executing_plan(self, sample_plan_factory, plan_id="test-plan"):
        plan_dir = sample_plan_factory(plan_id, phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}),
            encoding="utf-8",
        )
        return plan_dir

    def test_start_response_schema(self, sample_plan_factory, monkeypatch):
        """POST /start response matches START_RESPONSE_SCHEMA."""
        self._setup_executing_plan(sample_plan_factory)
        monkeypatch.setattr("server.threading.Thread", FakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        resp = client.post("/api/verification/test-plan/start")
        assert resp.status_code == 200
        validate(instance=resp.json(), schema=START_RESPONSE_SCHEMA)

    def test_status_response_schema(self, sample_plan_factory):
        """GET /status response matches VERIFICATION_STATUS_SCHEMA."""
        sample_plan_factory("test-plan", phase="executing")
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "running",
            "verification_round": 2,
            "verification_max_rounds": 5,
            "results": {
                "pytest_summary": "10 passed, 0 failed",
                "llm_findings": "All good",
                "performance_metrics": {},
            },
            "repair_tasks": [],
            "started_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T02:00:00",
            "orchestrator": None,
            "stop_reason": None,
        }

        resp = client.get("/api/verification/test-plan/status")
        assert resp.status_code == 200
        validate(instance=resp.json(), schema=VERIFICATION_STATUS_SCHEMA)

    def test_status_not_started_schema(self, sample_plan_factory):
        """GET /status for plan without verification matches schema with nulls."""
        sample_plan_factory("test-plan", phase="executing")
        resp = client.get("/api/verification/test-plan/status")
        assert resp.status_code == 200
        data = resp.json()
        validate(instance=data, schema=VERIFICATION_STATUS_SCHEMA)
        assert data["verification_status"] == "not_started"
        assert data["started_at"] is None
        assert data["updated_at"] is not None  # falls back to plan_state last_updated

    def test_stop_response_schema(self, sample_plan_factory):
        """POST /stop response matches STOP_RESPONSE_SCHEMA."""
        # A live round the stop path can actually transition: routing
        # stage matches the CAS predicate and a ``plan_verification``
        # row exists for ``mark_stopped``.
        sample_plan_factory(
            "test-plan",
            phase="verification_running",
            verification_status="running",
            verification_round=1,
        )
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "running",
            "verification_round": 1,
            "orchestrator": MagicMock(),
        }

        resp = client.post("/api/verification/test-plan/stop")
        assert resp.status_code == 200
        validate(instance=resp.json(), schema=STOP_RESPONSE_SCHEMA)

    def test_repair_tasks_response_schema(self, sample_plan_factory):
        """GET /repair_tasks response matches REPAIR_TASKS_RESPONSE_SCHEMA."""
        sample_plan_factory("test-plan", phase="executing")
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "verification_failed",
            "repair_tasks": [
                {
                    "id": "1-1",
                    "title": "Fix auth",
                    "description": "Auth failing",
                    "test_command": "pytest tests/test_auth.py",
                    "failure_reason": "401 on login",
                }
            ],
        }

        resp = client.get("/api/verification/test-plan/repair_tasks")
        assert resp.status_code == 200
        validate(instance=resp.json(), schema=REPAIR_TASKS_RESPONSE_SCHEMA)


# ---------------------------------------------------------------------------
# 2. Status code tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStatusCodes:
    """各种场景 -> HTTP状态码正确."""

    def test_start_success_returns_200(self, sample_plan_factory, monkeypatch):
        """Valid start request returns 200."""
        plan_dir = sample_plan_factory("test-plan", phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )
        monkeypatch.setattr("server.threading.Thread", FakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        resp = client.post("/api/verification/test-plan/start")
        assert resp.status_code == 200

    def test_start_plan_not_found_returns_404(self):
        """Start on missing plan returns 404."""
        resp = client.post("/api/verification/missing-plan/start")
        assert resp.status_code == 404

    def test_start_wrong_phase_returns_409(self, sample_plan_factory):
        """Start on a plan whose routing stage forbids it returns 409.

        A pre-SQLite version of ``start_verification`` read
        ``current_phase`` from ``plan_state.json`` and answered 400
        "Invalid phase". The SQLite-first rewrite made the routing CAS
        (``try_mark_phase``) the single decider: the plan's stage is not
        one of the accepted source stages, so the answer is the
        documented conflict envelope. See
        ``tests/integration/api/test_api_error_matrix.py``
        (``verify_start x cas_predicate_fail`` → 409) and
        ``state_machine/tests/integration/test_verification_routes.py``
        (``test_start_verification_409_when_stage_mismatch``, seeded
        with stage ``interview``).
        """
        sample_plan_factory("test-plan", phase="prd_review")
        resp = client.post("/api/verification/test-plan/start")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "conflict"
        assert body["reason"] in ("stage_mismatch", "version_mismatch")

    def test_start_missing_project_dir_returns_400(self, sample_plan_factory):
        """Start on a plan with no recorded target project returns 400."""
        sample_plan_factory("test-plan", phase="executing", with_project_dir=False)
        resp = client.post("/api/verification/test-plan/start")
        assert resp.status_code == 400

    def test_start_already_running_returns_409(self, sample_plan_factory, monkeypatch):
        """Start when already running returns 409."""
        plan_dir = sample_plan_factory("test-plan", phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "running",
        }

        resp = client.post("/api/verification/test-plan/start")
        assert resp.status_code == 409

    def test_status_plan_not_found_returns_404(self):
        """Status on missing plan returns 404."""
        resp = client.get("/api/verification/missing-plan/status")
        assert resp.status_code == 404

    def test_status_existing_plan_returns_200(self, sample_plan_factory):
        """Status on existing plan returns 200."""
        sample_plan_factory("test-plan", phase="executing")
        resp = client.get("/api/verification/test-plan/status")
        assert resp.status_code == 200

    def test_repair_tasks_plan_not_found_returns_404(self):
        """Repair tasks on missing plan returns 404."""
        resp = client.get("/api/verification/missing-plan/repair_tasks")
        assert resp.status_code == 404

    def test_repair_tasks_existing_plan_returns_200(self, sample_plan_factory):
        """Repair tasks on existing plan returns 200."""
        sample_plan_factory("test-plan", phase="executing")
        resp = client.get("/api/verification/test-plan/repair_tasks")
        assert resp.status_code == 200

    def test_stop_plan_not_found_returns_404(self):
        """Stop on missing plan returns 404."""
        resp = client.post("/api/verification/missing-plan/stop")
        assert resp.status_code == 404

    def test_stop_not_running_returns_409(self, sample_plan_factory):
        """Stop on a plan that is not running returns 409.

        ``/stop`` is a routing CAS on ``verification_running``; the
        plan's stage is ``executing``, so the predicate fails and the
        answer is the documented conflict envelope — not a 400. The
        in-memory status is set to ``not_started`` to make the point
        that ``_verification_state`` no longer decides this; the CAS
        does. See ``tests/integration/api/test_api_error_matrix.py``
        (``verify_stop x normal_plan`` → 409) and
        ``state_machine/tests/integration/test_verification_routes.py``
        (``test_stop_verification_409_when_not_running``).
        """
        sample_plan_factory("test-plan", phase="executing")
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "not_started",
        }
        resp = client.post("/api/verification/test-plan/stop")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "conflict"
        assert body["reason"] in ("stage_mismatch", "version_mismatch")

    def test_stop_running_returns_200(self, sample_plan_factory):
        """Stop when running returns 200."""
        # A live round needs three things to agree: the in-memory state
        # (set below), the routing stage the stop CAS matches on, and a
        # ``plan_verification`` row for ``mark_stopped`` to update. Seed
        # all three so the fixture really describes "a round is running".
        sample_plan_factory(
            "test-plan",
            phase="verification_running",
            verification_status="running",
            verification_round=1,
        )
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "running",
            "verification_round": 1,
            "orchestrator": MagicMock(),
        }
        resp = client.post("/api/verification/test-plan/stop")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. Error message tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestErrorMessages:
    """错误响应 -> 包含error和detail字段，消息友好."""

    def _assert_error_response(self, resp, expected_status):
        assert resp.status_code == expected_status
        data = resp.json()
        validate(instance=data, schema=ERROR_RESPONSE_SCHEMA)
        assert "error" in data
        assert "detail" in data
        assert len(data["error"]) > 0
        assert len(data["detail"]) > 0
        # Friendly message check: should be human-readable, not just codes
        assert not data["error"].isdigit()
        assert not data["detail"].isdigit()

    def test_start_plan_not_found_error(self):
        resp = client.post("/api/verification/missing-plan/start")
        self._assert_error_response(resp, 404)
        assert "does not exist" in resp.json()["detail"].lower()

    def test_start_wrong_phase_error(self, sample_plan_factory):
        """A routing-stage rejection answers the conflict envelope.

        Not routed through ``_assert_error_response``: the CAS conflict
        body is ``{"error": "conflict", "reason": ...}`` (VP-018's
        documented shape), which has no ``detail`` field — the
        ``ERROR_RESPONSE_SCHEMA`` above describes the *other* error
        family (404/400 with a human-readable detail).
        """
        sample_plan_factory("test-plan", phase="prd_review")
        resp = client.post("/api/verification/test-plan/start")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "conflict"
        assert body["reason"] in ("stage_mismatch", "version_mismatch")

    def test_start_missing_project_dir_error(self, sample_plan_factory):
        sample_plan_factory("test-plan", phase="executing", with_project_dir=False)
        resp = client.post("/api/verification/test-plan/start")
        self._assert_error_response(resp, 400)
        assert "project directory" in resp.json()["detail"].lower()

    def test_start_already_running_error(self, sample_plan_factory):
        plan_dir = sample_plan_factory("test-plan", phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "running",
        }
        resp = client.post("/api/verification/test-plan/start")
        self._assert_error_response(resp, 409)
        assert "already running" in resp.json()["detail"].lower()

    def test_status_plan_not_found_error(self):
        resp = client.get("/api/verification/missing-plan/status")
        self._assert_error_response(resp, 404)

    def test_repair_tasks_plan_not_found_error(self):
        resp = client.get("/api/verification/missing-plan/repair_tasks")
        self._assert_error_response(resp, 404)

    def test_stop_plan_not_found_error(self):
        resp = client.post("/api/verification/missing-plan/stop")
        self._assert_error_response(resp, 404)

    def test_stop_not_running_error(self, sample_plan_factory):
        """Stop on an idle plan answers the CAS conflict envelope.

        Same shape note as ``test_start_wrong_phase_error``: the 409 is
        ``{"error": "conflict", "reason": ...}``, not the
        ``ERROR_RESPONSE_SCHEMA`` family.
        """
        sample_plan_factory("test-plan", phase="executing")
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "loop_stopped",
        }
        resp = client.post("/api/verification/test-plan/stop")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "conflict"
        assert body["reason"] in ("stage_mismatch", "version_mismatch")


# ---------------------------------------------------------------------------
# 4. Boundary value tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBoundaryValues:
    """边界条件 -> 返回400或404."""

    def _setup_start_ready_plan(self, sample_plan_factory, plan_id="boundary-plan"):
        plan_dir = sample_plan_factory(plan_id, phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )
        return plan_dir

    def test_boundary_plan_id_not_found(self):
        """Non-existent plan_id on all endpoints returns 404."""
        resp = client.post("/api/verification/nonexistent/start")
        assert resp.status_code == 404

        resp = client.get("/api/verification/nonexistent/status")
        assert resp.status_code == 404

        resp = client.get("/api/verification/nonexistent/repair_tasks")
        assert resp.status_code == 404

        resp = client.post("/api/verification/nonexistent/stop")
        assert resp.status_code == 404

    def test_boundary_verification_not_started(self, sample_plan_factory):
        """Operations on plan that never started verification are handled gracefully."""
        sample_plan_factory("test-plan", phase="executing")
        # GET /status should return a valid status (200, not error)
        # Note: plan_state fallback returns "not_started" when verification block has status "not_started"
        resp = client.get("/api/verification/test-plan/status")
        assert resp.status_code == 200
        assert resp.json()["verification_status"] in ("not_started", "pending")

        # GET /repair_tasks should return empty tasks (200)
        resp = client.get("/api/verification/test-plan/repair_tasks")
        assert resp.status_code == 200
        assert resp.json()["tasks"] == []

        # POST /stop should return 409 (not running) — the CAS predicate
        # ``verification_running`` fails against the plan's ``executing``
        # stage. A pre-SQLite version answered 400 here; see
        # ``test_stop_not_running_returns_409``.
        resp = client.post("/api/verification/test-plan/stop")
        assert resp.status_code == 409
        assert resp.json()["error"] == "conflict"

    def test_boundary_max_rounds_less_than_one(self, sample_plan_factory, monkeypatch):
        """max_rounds < 1 returns 400."""
        self._setup_start_ready_plan(sample_plan_factory)
        monkeypatch.setattr("server.threading.Thread", FakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        resp = client.post("/api/verification/boundary-plan/start", json={"max_rounds": 0})
        assert resp.status_code == 400
        data = resp.json()
        assert "max_rounds" in data["detail"].lower()

        resp = client.post("/api/verification/boundary-plan/start", json={"max_rounds": -1})
        assert resp.status_code == 400

    def test_boundary_max_rounds_greater_than_one_thousand(self, sample_plan_factory, monkeypatch):
        """max_rounds > 1000 returns 400."""
        self._setup_start_ready_plan(sample_plan_factory)
        monkeypatch.setattr("server.threading.Thread", FakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        resp = client.post("/api/verification/boundary-plan/start", json={"max_rounds": 1001})
        assert resp.status_code == 400
        data = resp.json()
        assert "max_rounds" in data["detail"].lower()

        resp = client.post("/api/verification/boundary-plan/start", json={"max_rounds": 10000})
        assert resp.status_code == 400

    def test_boundary_max_rounds_at_limits(self, sample_plan_factory, monkeypatch):
        """max_rounds at boundaries 1 and 1000 returns 200."""
        self._setup_start_ready_plan(sample_plan_factory)
        monkeypatch.setattr("server.threading.Thread", FakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        resp = client.post("/api/verification/boundary-plan/start", json={"max_rounds": 1})
        assert resp.status_code == 200

        # Reset state for the next boundary case. The first start wrote
        # real rows (init_round + the stage CAS), so without a cleanup the
        # second /start would correctly see stage=verification_running and
        # answer 409. Before 2026-09-15 this test passed only because a
        # PlanState mirror write clobbered the CAS'd stage back to
        # ``executing`` (the bug fixed in the 0481294f stage-preservation
        # change).
        _verification_state.clear()
        _delete_plan_rows("boundary-plan")
        resp = client.post("/api/verification/boundary-plan/start", json={"max_rounds": 1000})
        assert resp.status_code == 200

    def test_boundary_current_phase_prd_review(self, sample_plan_factory):
        """Starting verification from ``prd_review`` returns the CAS conflict."""
        self._setup_start_ready_plan(sample_plan_factory)
        # change phase to prd_review
        plan_dir = sample_plan_factory("boundary-plan", phase="prd_review")
        resp = client.post("/api/verification/boundary-plan/start")
        assert resp.status_code == 409
        data = resp.json()
        assert data["error"] == "conflict"
        assert data["reason"] == "stage_mismatch"

    def test_boundary_current_phase_ready(self, sample_plan_factory):
        """Starting verification from ``ready`` returns the CAS conflict.

        ``ready`` maps to the routing stage ``ready``, which is not
        an accepted source stage for the CAS — so the answer is the
        documented conflict envelope rather than the 400 a pre-SQLite
        version returned. See
        ``test_start_wrong_phase_returns_409``.
        """
        sample_plan_factory("ready-plan", phase="ready")
        resp = client.post("/api/verification/ready-plan/start")
        assert resp.status_code == 409
        data = resp.json()
        assert data["error"] == "conflict"
        assert data["reason"] == "stage_mismatch"

    def test_boundary_empty_repair_tasks(self, sample_plan_factory):
        """Repair tasks when verification passed (no repairs) returns empty array."""
        sample_plan_factory("test-plan", phase="executing")
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "passed",
            "repair_tasks": [],
        }
        resp = client.get("/api/verification/test-plan/repair_tasks")
        assert resp.status_code == 200
        assert resp.json()["tasks"] == []


# ---------------------------------------------------------------------------
# 5. Concurrency tests
# ---------------------------------------------------------------------------


class SlowDict(dict):
    """Dict that delays get() to widen the race-condition window."""

    def __init__(self, delay: float = 0.05, target_key: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._delay = delay
        self._target_key = target_key

    def get(self, key, default=None):
        if key == self._target_key:
            time.sleep(self._delay)
        return super().get(key, default)


class FakeThread:
    """Thread replacement that immediately executes target on start()."""

    def __init__(self, target=None, daemon=None, *args, **kwargs):
        self._target = target
        # Mirror ``threading.Thread``: ``args=`` / ``kwargs=`` are passed
        # as keywords and must be forwarded to the target on start().
        self._target_args = kwargs.pop("args", ())
        self._target_kwargs = kwargs.pop("kwargs", {})

    def start(self):
        if self._target:
            self._target(*self._target_args, **self._target_kwargs)

    def join(self, timeout=None):
        pass


class NoOpFakeThread:
    """Thread replacement that does NOT execute target — keeps verification state as 'running'."""

    def __init__(self, target=None, daemon=None, *args, **kwargs):
        self._target = target
        # Mirror ``threading.Thread``: ``args=`` / ``kwargs=`` are passed
        # as keywords and must be forwarded to the target on start().
        self._target_args = kwargs.pop("args", ())
        self._target_kwargs = kwargs.pop("kwargs", {})

    def start(self):
        pass

    def join(self, timeout=None):
        pass


@pytest.mark.integration
class TestConcurrency:
    """并发启动verification -> 只有一个成功，其他返回409."""

    @pytest.mark.asyncio
    async def test_async_concurrent_start(self, sample_plan_factory, monkeypatch):
        """Using httpx AsyncClient + asyncio.gather for concurrent requests."""
        plan_dir = sample_plan_factory("async-concurrent", phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )

        # Use NoOpFakeThread so verification state stays "running" after first request
        monkeypatch.setattr("server.threading.Thread", NoOpFakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        # ``base_url="http://testserver"`` and the guard header are both
        # required: ``request_guard`` refuses a non-loopback Host and any
        # /api/* request without ``X-PDT-Request``. ``testserver`` is the
        # host ``conftest`` registers; see its request-guard block for why
        # the suite satisfies the guard rather than bypassing it.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-PDT-Request": "1"},
        ) as ac:
            coros = [ac.post("/api/verification/async-concurrent/start") for _ in range(5)]
            responses = await asyncio.gather(*coros, return_exceptions=True)

        status_codes = [r.status_code for r in responses if not isinstance(r, Exception)]
        exceptions = [r for r in responses if isinstance(r, Exception)]

        assert not exceptions, f"Exceptions during concurrent requests: {exceptions}"
        assert status_codes.count(200) == 1, f"Expected exactly one 200, got {status_codes}"
        assert status_codes.count(409) == 4, f"Expected exactly four 409, got {status_codes}"


# ---------------------------------------------------------------------------
# 6. Frontend contract tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFrontendContract:
    """Mock server模拟Verification API响应 -> 前端JavaScript契约验证."""

    def test_frontend_status_contract(self):
        """Mocked status response matches what frontend/api.js expects."""
        with requests_mock.Mocker() as m:
            m.get(
                "http://testserver/api/verification/frontend-plan/status",
                json={
                    "plan_id": "frontend-plan",
                    "verification_status": "running",
                    "verification_round": 1,
                    "verification_max_rounds": 3,
                    "results": {
                        "pytest_summary": "5 passed, 1 failed",
                        "llm_findings": "Code review passed",
                        "performance_metrics": {"response_time_ms": 120},
                    },
                    "repair_tasks": [],
                    "started_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T01:00:00",
                },
            )

            resp = requests.get("http://testserver/api/verification/frontend-plan/status")
            assert resp.status_code == 200
            data = resp.json()

            # Frontend expects these exact top-level keys
            validate(instance=data, schema=VERIFICATION_STATUS_SCHEMA)
            assert data["plan_id"] == "frontend-plan"
            assert data["verification_status"] == "running"
            assert isinstance(data["repair_tasks"], list)
            assert isinstance(data["results"], dict)
            # Fields frontend JS accesses (from api.js)
            assert "pytest_summary" in data["results"]
            assert "llm_findings" in data["results"]

    def test_frontend_start_contract(self):
        """Mocked start response matches what frontend/api.js expects."""
        with requests_mock.Mocker() as m:
            m.post(
                "http://testserver/api/verification/frontend-plan/start",
                json={"plan_id": "frontend-plan", "status": "started"},
            )

            resp = requests.post("http://testserver/api/verification/frontend-plan/start")
            assert resp.status_code == 200
            data = resp.json()
            validate(instance=data, schema=START_RESPONSE_SCHEMA)
            assert data["status"] == "started"

    def test_frontend_repair_tasks_contract(self):
        """Mocked repair_tasks response matches what frontend/api.js expects."""
        with requests_mock.Mocker() as m:
            m.get(
                "http://testserver/api/verification/frontend-plan/repair_tasks",
                json={
                    "tasks": [
                        {
                            "id": "1-1",
                            "title": "Fix timeout",
                            "description": "Fix login timeout",
                            "test_command": "pytest tests/test_login.py",
                            "failure_reason": "Response time > 2s",
                        }
                    ]
                },
            )

            resp = requests.get("http://testserver/api/verification/frontend-plan/repair_tasks")
            assert resp.status_code == 200
            data = resp.json()
            validate(instance=data, schema=REPAIR_TASKS_RESPONSE_SCHEMA)
            assert len(data["tasks"]) == 1
            task = data["tasks"][0]
            assert task["id"] == "1-1"
            assert task["title"] == "Fix timeout"

    def test_frontend_stop_contract(self):
        """Mocked stop response matches what frontend/api.js expects."""
        with requests_mock.Mocker() as m:
            m.post(
                "http://testserver/api/verification/frontend-plan/stop",
                json={
                    "stopped_at": "2026-01-01T02:00:00",
                    "reason": "user_stopped",
                    "current_round": 1,
                },
            )

            resp = requests.post("http://testserver/api/verification/frontend-plan/stop")
            assert resp.status_code == 200
            data = resp.json()
            validate(instance=data, schema=STOP_RESPONSE_SCHEMA)
            assert data["reason"] == "user_stopped"
            assert data["current_round"] == 1

    def test_frontend_error_contract(self):
        """Mocked error responses contain error+detail fields for frontend/api.js parsing."""
        with requests_mock.Mocker() as m:
            m.post(
                "http://testserver/api/verification/frontend-plan/start",
                status_code=400,
                json={
                    "error": "Invalid phase",
                    "detail": "Verification can only be started when current phase is 'executing'.",
                },
            )

            resp = requests.post("http://testserver/api/verification/frontend-plan/start")
            assert resp.status_code == 400
            data = resp.json()

            # Frontend JS does: err.detail || err.error || res.statusText
            assert "error" in data
            assert "detail" in data
            validate(instance=data, schema=ERROR_RESPONSE_SCHEMA)
            assert "executing" in data["detail"]

            # Simulate frontend error parsing logic
            detail = data.get("detail") or data.get("error") or resp.reason
            assert "executing" in detail


# ---------------------------------------------------------------------------
# Execution-profile exposure (TDD spec from task 8-4)
# ---------------------------------------------------------------------------


class TestExecutionProfileExposure:
    """TDD spec for exposing ``execution_profile`` on the status endpoint.

    The bridge UI / polling client expects the
    ``/api/verification/{plan_id}/status`` response to include a
    top-level ``execution_profile`` field so the verification budget
    card can render total duration, group breakdown, and
    parallelism cap without a second round trip.

    Two contracts pinned here:

    1. ``test_status_endpoint_exposes_execution_profile`` — the
       response carries an ``execution_profile`` field whose shape
       mirrors :class:`ExecutionProfileGenerator.build()`'s output.

    2. ``test_status_endpoint_backward_compatible`` — adding the
       new field does NOT break the four pre-existing fields the
       frontend JS already keys on. ``plan_id``,
       ``verification_status``, ``verification_round``,
       ``verification_max_rounds``, ``results``, ``repair_tasks``,
       ``started_at``, ``updated_at`` must remain unchanged in
       type and name. The schema validator (which is the
       canonical contract document) must still pass.
    """

    def test_status_endpoint_exposes_execution_profile(
        self, sample_plan_factory
    ):
        """GET /status includes top-level ``execution_profile``."""
        sample_plan_factory("test-plan", phase="executing")
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "passed",
            "verification_round": 1,
            "verification_max_rounds": 3,
            "results": {
                "pytest_summary": "10 passed, 0 failed",
                "llm_findings": "All good",
                "performance_metrics": {},
            },
            "repair_tasks": [],
            "started_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T02:00:00",
            "orchestrator": None,
            "stop_reason": None,
            "execution_profile": {
                "total_duration_sec": 1820,
                "group_profiles": [
                    {
                        "method": "automated_test",
                        "count": 5,
                        "timeout_seconds": 60,
                        "duration_seconds": 300,
                        "vp_ids": ["VP-001", "VP-002", "VP-003", "VP-004", "VP-005"],
                    },
                ],
                "subtask_splits": [],
                "per_method_timeouts": {
                    "automated_test": 60,
                    "code_review": 90,
                    "__global__": 120,
                },
                "parallelism_cap": 4,
            },
        }

        resp = client.get("/api/verification/test-plan/status")
        assert resp.status_code == 200
        data = resp.json()

        # New field present at the top level (NOT nested inside
        # ``results`` or any other envelope).
        assert "execution_profile" in data, (
            f"status response must include 'execution_profile', got "
            f"keys: {list(data.keys())!r}"
        )
        profile = data["execution_profile"]
        assert isinstance(profile, dict), (
            f"execution_profile must be a dict, got "
            f"{type(profile).__name__}"
        )

        # Schema compliance with ExecutionProfileGenerator's output
        required_keys = {
            "total_duration_sec",
            "group_profiles",
            "subtask_splits",
            "per_method_timeouts",
            "parallelism_cap",
        }
        missing = required_keys - set(profile.keys())
        assert not missing, (
            f"execution_profile is missing required keys: {missing!r}"
        )

        # Type contract
        assert isinstance(profile["total_duration_sec"], int)
        assert isinstance(profile["group_profiles"], list)
        assert isinstance(profile["subtask_splits"], list)
        assert isinstance(profile["per_method_timeouts"], dict)
        assert isinstance(profile["parallelism_cap"], int)

        # The values are forwarded verbatim (the endpoint is
        # a passthrough, not a recomputation point).
        assert profile["total_duration_sec"] == 1820
        assert profile["parallelism_cap"] == 4

    def test_status_endpoint_backward_compatible(self, sample_plan_factory):
        """Adding ``execution_profile`` does not break legacy fields.

        The 4 critical pre-existing fields (verification_status,
        verification_round, results, repair_tasks) plus the
        rest of the schema must remain present and unchanged in
        name and type. The frontend JS in
        ``frontend/api.js::getVerificationStatus`` keys on these
        names and types — breaking them breaks the bridge UI.

        Additionally, the canonical
        :data:`VERIFICATION_STATUS_SCHEMA` (the contract
        document) must still validate the response.

        2026-09-14: the running round is seeded into the SQLite store
        (``/status`` no longer reads ``_verification_state`` for the
        status/round/results fields), while the live-only observability
        fields — ``repair_tasks`` and ``execution_profile`` — still come
        from the in-memory state of the running round.
        """
        sample_plan_factory(
            "test-plan",
            phase="verification_running",
            verification_status="running",
            verification_round=2,
            max_rounds=5,
            verification_results={
                "pytest_summary": "10 passed, 0 failed",
                "llm_findings": "All good",
                "performance_metrics": {},
            },
        )
        _verification_state["test-plan"] = {
            "plan_id": "test-plan",
            "verification_status": "running",
            "verification_round": 2,
            "verification_max_rounds": 5,
            "results": {
                "pytest_summary": "10 passed, 0 failed",
                "llm_findings": "All good",
                "performance_metrics": {},
            },
            "repair_tasks": [
                {
                    "id": "1-1",
                    "title": "Fix auth",
                    "description": "Auth failing",
                    "test_command": "pytest tests/test_auth.py",
                    "failure_reason": "401 on login",
                }
            ],
            "started_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T02:00:00",
            "orchestrator": None,
            "stop_reason": None,
            # New field — coexisting with the legacy fields.
            "execution_profile": {
                "total_duration_sec": 0,
                "group_profiles": [],
                "subtask_splits": [],
                "per_method_timeouts": {"__global__": 120},
                "parallelism_cap": 4,
            },
        }

        resp = client.get("/api/verification/test-plan/status")
        assert resp.status_code == 200
        data = resp.json()

        # Backward-compat: all 4 pre-existing fields must still be
        # present with the right type and value.
        assert data["verification_status"] == "running", (
            f"verification_status must be 'running', got "
            f"{data['verification_status']!r}"
        )
        assert isinstance(data["verification_status"], str)
        assert isinstance(data["verification_round"], int)
        assert data["verification_round"] == 2
        assert isinstance(data["verification_max_rounds"], int)
        assert data["verification_max_rounds"] == 5
        assert isinstance(data["results"], dict)
        assert data["results"]["pytest_summary"] == "10 passed, 0 failed"
        assert isinstance(data["repair_tasks"], list)
        # Repair-task *content* is served by its own endpoint now; the
        # status response carries the key as an empty-list placeholder.
        # Assert the content where it lives, so the contract is still
        # pinned rather than dropped.
        repair = client.get("/api/verification/test-plan/repair_tasks")
        assert repair.status_code == 200
        repair_tasks = repair.json()["tasks"]
        assert len(repair_tasks) == 1
        assert repair_tasks[0]["id"] == "1-1"
        assert isinstance(data["started_at"], str)
        assert isinstance(data["updated_at"], str)
        assert isinstance(data["plan_id"], str)
        assert data["plan_id"] == "test-plan"


        # The new field coexists without disturbing the schema.
        assert "execution_profile" in data
        assert isinstance(data["execution_profile"], dict)

        # The canonical schema must still validate the response —
        # the schema is the contract document, and it's deliberately
        # NOT updated for this task (the new field is additive, not
        # required-by-schema, so old JS clients can ignore it).
        validate(instance=data, schema=VERIFICATION_STATUS_SCHEMA)


# ---------------------------------------------------------------------------
# Endpoint contract regression (TDD spec from task 9-3)
# ---------------------------------------------------------------------------


class TestEndpointContractRegression:
    """Pinned-by-TDD contract suite for the verification API.

    Five contracts are validated here, each guarding a different
    surface of the verification endpoints after the task 9 status
    refactor:

    1. ``execution_profile.groups`` + ``current_group_index`` are
       surfaced on the ``/status`` payload when verification is
       running, so the bridge UI can render per-group progress
       without a second round trip.
    2. The 4 pre-existing top-level fields (``verification_status``,
       ``verification_round``, ``results``, ``repair_tasks``) remain
       present with their documented types — adding the new profile
       field must not silently break the bridge JS.
    3. ``/start`` tolerates unknown body fields (Pydantic's default
       behaviour of silently filtering extras) — future frontends
       that send a new flag must not be able to brick the backend.
    4. Concurrent ``/start`` requests for the same plan are
       serialised: exactly one returns 200, the rest return 409.
    5. ``verification_plan.json`` (phase 1 output) carries
       per-VP ``timeout_seconds`` and ``execution_group`` metadata
       so downstream consumers can project the execution budget
       without re-walking the plan.
    """

    def test_status_includes_execution_profile_when_running(
        self, sample_plan_factory
    ):
        """``/status`` exposes ``execution_profile.groups`` + ``current_group_index``.

        Contract:
            The response body must contain an ``execution_profile``
            object whose ``groups`` is a list of group dicts and
            whose ``current_group_index`` is an integer index into
            that list. The endpoint is a passthrough, so the
            pinned shape comes from
            :class:`ExecutionProfileGenerator.build()` and the
            test seeds the state with the canonical 3-group plan.
        """
        sample_plan_factory("exec-profile-running", phase="executing")
        _verification_state["exec-profile-running"] = {
            "plan_id": "exec-profile-running",
            "verification_status": "running",
            "verification_round": 1,
            "verification_max_rounds": 3,
            "results": {
                "pytest_summary": "1 passed, 0 failed",
                "llm_findings": "ok",
                "performance_metrics": {},
            },
            "repair_tasks": [],
            "started_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:30:00",
            "orchestrator": None,
            "stop_reason": None,
            "execution_profile": {
                "total_duration_sec": 2640,
                "groups": [
                    {
                        "method": "automated_test",
                        "count": 2,
                        "timeout_seconds": 120,
                        "duration_seconds": 240,
                        "vp_ids": ["VP-001", "VP-002"],
                    },
                    {
                        "method": "ui_validation",
                        "count": 1,
                        "timeout_seconds": 1800,
                        "duration_seconds": 1800,
                        "vp_ids": ["VP-003"],
                    },
                    {
                        "method": "code_review",
                        "count": 1,
                        "timeout_seconds": 1800,
                        "duration_seconds": 1800,
                        "vp_ids": ["VP-004"],
                    },
                ],
                "current_group_index": 1,
                "subtask_splits": [],
                "per_method_timeouts": {
                    "automated_test": 3600,
                    "ui_validation": 3600,
                    "code_review": 3600,
                    "__global__": 3600,
                },
                "parallelism_cap": 4,
            },
        }

        resp = client.get("/api/verification/exec-profile-running/status")
        assert resp.status_code == 200
        data = resp.json()

        # New top-level field is present
        assert "execution_profile" in data, (
            f"status response must include 'execution_profile', got "
            f"keys: {list(data.keys())!r}"
        )
        profile = data["execution_profile"]
        assert isinstance(profile, dict)

        # `groups` key — list of group dicts
        assert "groups" in profile, (
            f"execution_profile must expose 'groups' for the bridge UI, "
            f"got keys: {list(profile.keys())!r}"
        )
        assert isinstance(profile["groups"], list)
        assert len(profile["groups"]) == 3
        for group in profile["groups"]:
            assert isinstance(group, dict)
            assert "method" in group
            assert "count" in group
            assert "vp_ids" in group

        # `current_group_index` — integer pointing at the running group
        assert "current_group_index" in profile, (
            "execution_profile must expose 'current_group_index' so the "
            "UI can highlight the active group"
        )
        assert isinstance(profile["current_group_index"], int)
        assert profile["current_group_index"] == 1
        # Sanity: index is in range
        assert 0 <= profile["current_group_index"] < len(profile["groups"])

    def test_status_backward_compatible_4_fields(self, sample_plan_factory):
        """Adding ``execution_profile`` does not break the 4 legacy fields.

        Contract:
            For a running verification, the 4 pre-existing top-level
            fields the frontend JS keys on — ``verification_status``,
            ``verification_round``, ``results``, ``repair_tasks`` —
            must remain present and of the documented Python type
            (``str``, ``int``, ``dict``, ``list``).

        2026-09-14: ``/status`` is SQLite-sourced, so the round it
        describes is seeded into ``plan_routing`` / ``plan_verification``
        rather than only into ``_verification_state``. The repair-task
        *content* moved to its own endpoint (``/repair_tasks``) — the
        status response keeps the key as an empty-list placeholder — so
        that assertion is made against the endpoint that owns it.
        """
        sample_plan_factory(
            "backward-compat",
            phase="verification_running",
            verification_status="running",
            verification_round=2,
            verification_results={
                "pytest_summary": "5 passed, 1 failed",
                "llm_findings": "one known flake",
                "performance_metrics": {"response_time_ms": 240},
            },
        )
        _verification_state["backward-compat"] = {
            "plan_id": "backward-compat",
            "verification_status": "running",
            "verification_round": 2,
            "verification_max_rounds": 3,
            "results": {
                "pytest_summary": "5 passed, 1 failed",
                "llm_findings": "one known flake",
                "performance_metrics": {"response_time_ms": 240},
            },
            "repair_tasks": [
                {
                    "id": "R-1",
                    "title": "Fix login timeout",
                    "description": "Login hangs > 2s",
                    "test_command": "pytest tests/test_login.py",
                    "failure_reason": "TimeoutError",
                }
            ],
            "started_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T01:00:00",
            "orchestrator": None,
            "stop_reason": None,
            "execution_profile": {
                "total_duration_sec": 1200,
                "groups": [
                    {
                        "method": "automated_test",
                        "count": 1,
                        "timeout_seconds": 120,
                        "duration_seconds": 120,
                        "vp_ids": ["VP-001"],
                    }
                ],
                "current_group_index": 0,
                "subtask_splits": [],
                "per_method_timeouts": {"automated_test": 3600},
                "parallelism_cap": 4,
            },
        }

        resp = client.get("/api/verification/backward-compat/status")
        assert resp.status_code == 200
        data = resp.json()

        # 4 legacy fields, all present, all correctly typed
        assert data["verification_status"] == "running"
        assert isinstance(data["verification_status"], str)

        assert data["verification_round"] == 2
        assert isinstance(data["verification_round"], int)

        assert isinstance(data["results"], dict)
        assert data["results"]["pytest_summary"] == "5 passed, 1 failed"
        assert isinstance(data["results"]["performance_metrics"], dict)

        assert isinstance(data["repair_tasks"], list)
        # ``/status`` carries the key as a placeholder; the repair tasks
        # themselves are served by their own endpoint, which reads the
        # live round's freshly generated list out of the in-memory state
        # seeded above.
        repair = client.get("/api/verification/backward-compat/repair_tasks")
        assert repair.status_code == 200
        repair_tasks = repair.json()["tasks"]
        assert len(repair_tasks) == 1
        assert repair_tasks[0]["id"] == "R-1"

        # The new field coexists
        assert "execution_profile" in data
        assert isinstance(data["execution_profile"], dict)

    def test_start_ignores_unknown_params(
        self, sample_plan_factory, monkeypatch
    ):
        """``/start`` silently filters unknown body fields.

        Contract:
            Pydantic 2.x's default behaviour for ``BaseModel`` is to
            silently ignore fields that are not declared on the model.
            A future frontend that adds a new flag (e.g.
            ``"report_to_slack": true``) must not 4xx the request;
            the backend keeps working and the unknown flag is
            discarded.
        """
        # Use the standard "ready to start" plan setup
        plan_dir = sample_plan_factory("unknown-params", phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )
        monkeypatch.setattr("server.threading.Thread", NoOpFakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        # Send a body that mixes known + unknown fields.
        resp = client.post(
            "/api/verification/unknown-params/start",
            json={
                "max_rounds": 3,
                "auto_fix": True,
                "unknown_field": "should-be-ignored",
                "extra_int": 42,
                "nested": {"also": "ignored"},
            },
        )

        assert resp.status_code == 200, (
            f"Unknown body fields must be silently filtered, got "
            f"{resp.status_code} {resp.text!r}"
        )
        body = resp.json()
        assert body["plan_id"] == "unknown-params"
        assert body["status"] == "started"

        # The verification state must reflect the known params, not the
        # unknown ones (so a future change to surface the unknown
        # field would surface here, in the test).
        #
        # 2026-09-14: under the default ``auto_fix=True`` the handler
        # deliberately defers ``_init_verification_state`` to the
        # auto-loop, so ``verification_max_rounds`` is not populated on
        # the in-memory dict synchronously any more. The request's
        # ``max_rounds`` does land synchronously — ``init_round`` writes
        # it to ``plan_verification.max_rounds`` before the response is
        # returned — so assert there, using the connection the handler
        # bound to the in-memory state.
        state = _verification_state.get("unknown-params", {})
        for unknown in ("unknown_field", "extra_int", "nested"):
            assert unknown not in state, (
                f"unknown body field {unknown!r} must be discarded, "
                f"not stored: {state!r}"
            )

        conn = state.get("_state_db_conn")
        assert conn is not None, (
            "start must bind its long-lived SQLite connection to the "
            "in-memory state so the verification thread can use it"
        )
        row = conn.execute(
            "SELECT max_rounds FROM plan_verification WHERE plan_id = ?",
            ("unknown-params",),
        ).fetchone()
        assert row is not None and row[0] == 3, (
            f"the request's max_rounds must reach plan_verification "
            f"unchanged, got {row!r}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_start_returns_409_5way(
        self, sample_plan_factory, monkeypatch
    ):
        """5 concurrent ``/start`` requests → exactly 1×200 + 4×409.

        Contract:
            The ``/start`` endpoint must serialise concurrent
            requests for the same plan. The first request to
            acquire ``_verification_lock`` succeeds (200) and
            transitions the plan to ``verification_status=running``;
            the next 4 requests all see the running state and
            return 409. This is the load-bearing guarantee that
            prevents two orchestrators from being started for the
            same plan.
        """
        plan_dir = sample_plan_factory("concurrent-5way", phase="executing")
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8"
        )

        # NoOpFakeThread: state stays "running" after the first
        # request, so the 4 follow-ups hit the 409 branch instead
        # of finding an already-completed plan.
        monkeypatch.setattr("server.threading.Thread", NoOpFakeThread)
        monkeypatch.setattr("server.VerificationOrchestrator", MagicMock())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-PDT-Request": "1"},
        ) as ac:
            coros = [
                ac.post("/api/verification/concurrent-5way/start")
                for _ in range(5)
            ]
            responses = await asyncio.gather(*coros, return_exceptions=True)

        status_codes = [r.status_code for r in responses if not isinstance(r, Exception)]
        exceptions = [r for r in responses if isinstance(r, Exception)]
        assert not exceptions, f"Unexpected exceptions: {exceptions}"

        # Exactly 1 success + 4 conflicts
        assert status_codes.count(200) == 1, (
            f"Expected exactly one 200, got {status_codes}"
        )
        assert status_codes.count(409) == 4, (
            f"Expected exactly four 409, got {status_codes}"
        )
        assert len(status_codes) == 5

    def test_plan_json_includes_per_vp_timeout(self, tmp_path, monkeypatch):
        """Phase 1 output (``verification_plan.json``) carries per-VP metadata.

        Contract:
            For every VP written to ``verification_plan.json``,
            ``generate_verification_plan`` must add two fields:

            * ``timeout_seconds`` (``int``) — the resolved per-VP
              timeout (per-VP override > per-method default > global
              default), matching the executor's ``wait_for``
              boundary.
            * ``execution_group`` (``int``) — the 0-based index of
              the group this VP belongs to in the
              :class:`ExecutionProfileGenerator` partition.

            VPs sharing ``verification_method`` MUST share the same
            ``execution_group``; VPs in different methods MUST land
            in different groups (modulo the first-seen order).

        The test mocks the LLM call to return a fixed 3-VP plan
        spanning 2 methods, so the assertions are deterministic
        and free of network latency.
        """
        # Use isolated dirs to keep the test self-contained.
        plan_dir = tmp_path / "plan-with-timeouts"
        plan_dir.mkdir(parents=True, exist_ok=True)
        project_dir = tmp_path / "project-no-venv"
        project_dir.mkdir(parents=True, exist_ok=True)

        # Make sure PYTHONPATH-via-sys.path includes the backend
        # directory so ``from verification_agent import …`` resolves.
        import sys
        backend_dir = str(Path(__file__).resolve().parent.parent)
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        from verification_agent import VerificationAgent
        from verification_config import TimeoutPolicy

        sample_plan = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "First automated test",
                    "verification_method": "automated_test",
                    "priority": "high",
                    "expected_result": "should pass",
                    "test_command": "echo 1",
                },
                {
                    "id": "VP-002",
                    "title": "UI check",
                    "verification_method": "ui_validation",
                    "priority": "medium",
                    "expected_result": "ui renders",
                    "target_url": "http://localhost:8000/",
                },
                {
                    "id": "VP-003",
                    "title": "Second automated test",
                    "verification_method": "automated_test",
                    "priority": "low",
                    "expected_result": "should also pass",
                    "test_command": "echo 3",
                },
            ]
        }

        class _FakeCodingTool:
            """Stand-in for the LLM-backed coding tool."""

            def __init__(self, plan):
                self._plan = plan

            def query_json(self, prompt, system_instruction, timeout=None):
                return self._plan

        fake_tool = _FakeCodingTool(sample_plan)
        agent = VerificationAgent(
            plan_dir, project_dir, coding_tool=fake_tool
        )
        # Pin the timeout policy to the documented defaults so the
        # per-method timeouts in the assertion are stable.
        agent.timeout_policy = TimeoutPolicy.defaults()

        # Run phase 1 (retry=1 so a single LLM call is enough)
        result = agent.generate_verification_plan(retry_llm=1)

        # --- Returned object has the enriched VPs ---
        points = result.get("verification_points", [])
        assert len(points) == 3, f"expected 3 VPs in result, got {len(points)}"

        for vp in points:
            assert "timeout_seconds" in vp, (
                f"VP {vp.get('id')!r} is missing 'timeout_seconds'; "
                f"present keys: {list(vp.keys())!r}"
            )
            assert isinstance(vp["timeout_seconds"], int), (
                f"VP {vp.get('id')!r}.timeout_seconds must be int, "
                f"got {type(vp['timeout_seconds']).__name__}"
            )
            assert vp["timeout_seconds"] > 0, (
                f"VP {vp.get('id')!r}.timeout_seconds must be > 0"
            )

            assert "execution_group" in vp, (
                f"VP {vp.get('id')!r} is missing 'execution_group'"
            )
            assert isinstance(vp["execution_group"], int), (
                f"VP {vp.get('id')!r}.execution_group must be int, "
                f"got {type(vp['execution_group']).__name__}"
            )
            assert vp["execution_group"] >= 0

        # --- Saved file has the same enrichment ---
        saved_path = plan_dir / "verification_plan.json"
        assert saved_path.exists(), (
            f"{saved_path} was not written by generate_verification_plan"
        )
        with open(saved_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)

        saved_points = saved.get("verification_points", [])
        assert len(saved_points) == 3
        for vp in saved_points:
            assert "timeout_seconds" in vp
            assert "execution_group" in vp
            assert isinstance(vp["timeout_seconds"], int)
            assert isinstance(vp["execution_group"], int)

        # --- Per-method timeout is resolved correctly ---
        # 2026-09-08: raised from 120 → 3600 to match the 1-hour outer cap
        by_id = {vp["id"]: vp for vp in saved_points}
        assert by_id["VP-001"]["timeout_seconds"] == 3600
        assert by_id["VP-003"]["timeout_seconds"] == 3600
        assert by_id["VP-002"]["timeout_seconds"] == 3600

        # --- Grouping invariants ---
        # automated_test VPs share the same group; ui_validation is
        # in a different group.
        assert (
            by_id["VP-001"]["execution_group"]
            == by_id["VP-003"]["execution_group"]
        ), "automated_test VPs must share the same execution_group"
        assert (
            by_id["VP-002"]["execution_group"]
            != by_id["VP-001"]["execution_group"]
        ), "ui_validation must be in a different group from automated_test"

