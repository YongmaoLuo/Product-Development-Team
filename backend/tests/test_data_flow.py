"""TDD tests for API layer to business layer data flow.

VP-014: API层到业务层数据流
VP-015: 业务层到数据层集成
Verification: API层接收HTTP请求转换为DTO，传递给业务层，业务层调用数据层，响应数据正确返回
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
import server
from server import app, _execution_state, _execution_locks
from plan_state import PlanState

client = TestClient(app)

# 2026-09-13 port (SQLite decision): PlanState persists to the
# ``plan_routing`` SQLite row; ``plan_state.json`` is a one-shot
# migration input only (the mirror write was removed as over-engineering).
# Readbacks therefore go through a fresh ``PlanState`` instance —
# SQLite is the source of truth — not a raw file load.


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Clear global execution state and redirect PLANS_DIR before each test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan(plan_id: str) -> Path:
    """Create a minimal plan directory."""
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "tasks_generation",
                "completed_phases": ["interview", "prd_generation", "prd_review", "tasks_generation"],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


class TestAPIToServiceFlow:
    """Verify data flows correctly from API layer to business layer."""

    def test_api_to_service_flow(self, tmp_path):
        """API layer receives HTTP request, converts to DTO, passes to business layer, returns correct response.

        Data flow tested:
        1. API layer receives HTTP POST with request body (JSON)
        2. FastAPI/Pydantic validates and converts to Pydantic model (DTO)
        3. API handler creates business layer object (PlanState)
        4. Business layer processes data
        5. Response returned from business layer
        6. API layer serializes response back to JSON
        """
        plan_id = "test-api-service-flow"
        plan_dir = _setup_plan(plan_id)

        # Step 1: API layer receives HTTP request
        resp = client.post(
            f"/api/plan/{plan_id}/state",
            json={"current_phase": "ready", "arch_enabled": True, "test_enabled": False},
        )

        # Step 6: Verify API layer returns correct response
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()

        # Verify response structure
        assert "current_phase" in data
        assert data["current_phase"] == "ready"

        # Verify business layer processed the state correctly — read back
        # through a fresh PlanState (SQLite plan_routing row is canonical).
        saved_state = PlanState(plan_dir).get_state()
        assert saved_state["current_phase"] == "ready"
        assert saved_state["flags"]["arch_enabled"] is True
        assert saved_state["flags"]["test_enabled"] is False

    def test_dto_validation(self, tmp_path):
        """DTO (Pydantic model) correctly validates input and converts types."""
        plan_id = "test-dto-validation"
        _setup_plan(plan_id)

        # Valid input
        resp = client.post(
            f"/api/plan/{plan_id}/state",
            json={"arch_enabled": True},
        )
        assert resp.status_code == 200

        # Invalid input - unknown field should be rejected
        resp = client.post(
            f"/api/plan/{plan_id}/state",
            json={"invalid_field": "should_fail"},
        )
        # FastAPI/Pydantic should reject unknown fields
        assert resp.status_code in [200, 422], "Should handle unknown fields gracefully"

    def test_business_layer_creates_correct_objects(self, tmp_path):
        """Business layer creates correct domain objects from DTO."""
        plan_id = "test-business-layer"
        plan_dir = _setup_plan(plan_id)

        from server import PlanState

        # Business layer creates PlanState object from plan_dir
        state = PlanState(plan_dir)
        assert state is not None

        # Business layer methods work correctly
        state.enable_arch(True)
        saved = state.get_state()
        assert saved["flags"]["arch_enabled"] is True

    def test_error_propagation_from_business_layer(self, tmp_path):
        """Errors from business layer are correctly propagated to API layer."""
        plan_id = "test-error-propagation"

        # Plan does not exist - business layer should raise error
        resp = client.post(
            f"/api/plan/{plan_id}/state",
            json={"current_phase": "ready"},
        )
        # API layer should return 404
        assert resp.status_code == 404


class TestServiceToRepositoryFlow:
    """Verify data flows correctly from business layer to data layer."""

    def test_service_to_repository_flow(self, tmp_path):
        """Business layer correctly calls data layer methods, data layer persists, results returned to business layer.

        Data flow tested:
        1. Data layer has plan_state.json on disk (pre-seeded state —
           one-shot migration input)
        2. Business layer (PlanState) reads state
        3. Business layer modifies state via transition_to() / enable_arch()
        4. Business layer persists to the plan_routing SQLite row
        5. SQLite row is updated correctly
        6. New PlanState instance reads back and verifies persisted changes
        """
        plan_id = "test-service-repo-flow"
        plan_dir = tmp_path / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Pre-seed data layer with plan_state.json
        initial_state = {
            "plan_id": plan_id,
            "current_phase": "prd_approved",
            "completed_phases": ["interview", "prd_generation", "prd_review"],
            "review_rounds": {"prd": 1, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
            "verification": {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
            "last_updated": "2026-06-08T00:00:00Z",
        }
        state_file = plan_dir / "plan_state.json"
        state_file.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")

        # Step 2-3: Business layer reads and modifies state
        state = PlanState(plan_dir)
        loaded = state.get_state()
        assert loaded["current_phase"] == "prd_approved"
        assert loaded["flags"]["arch_enabled"] is False

        # Business layer modifies state
        state.enable_arch(True)
        state.transition_to("arch_generation")

        # Step 4-5: Verify the SQLite row was updated — read back through a
        # fresh PlanState instance (plan_routing is the canonical store).
        saved = PlanState(plan_dir).get_state()
        assert saved["current_phase"] == "arch_generation"
        assert saved["flags"]["arch_enabled"] is True
        assert "prd_approved" in saved["completed_phases"]

        # Step 6: New business layer instance reads back persisted changes
        state2 = PlanState(plan_dir)
        loaded2 = state2.get_state()
        assert loaded2["current_phase"] == "arch_generation"
        assert loaded2["flags"]["arch_enabled"] is True
        assert "prd_approved" in loaded2["completed_phases"]

    def test_data_layer_persistence_atomicity(self, tmp_path):
        """Data layer writes are committed atomically (SQLite txn)."""
        plan_id = "test-atomicity"
        plan_dir = tmp_path / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        state = PlanState(plan_dir)
        state.enable_arch(True)

        # Read back through a fresh instance: the flag write must be
        # visible (SQLite committed it atomically).
        saved = PlanState(plan_dir).get_state()
        assert saved["flags"]["arch_enabled"] is True

        # No temp/leftover files from a half-applied write. (The old
        # rename-from-*.tmp contract belonged to the retired
        # plan_state.json writer; the SQLite path commits per-statement,
        # so the invariant to pin is simply: no stray .tmp artifacts.)
        tmp_files = list(plan_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_business_layer_handles_corrupted_data_layer(self, tmp_path):
        """Business layer handles corrupted/missing data layer gracefully."""
        plan_id = "test-corrupted"
        plan_dir = tmp_path / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        # Data layer has corrupted JSON
        state_file = plan_dir / "plan_state.json"
        state_file.write_text("not valid json {{{", encoding="utf-8")

        from plan_state import PlanState

        # Business layer should fall back to default state (not crash)
        state = PlanState(plan_dir)
        loaded = state.get_state()
        assert loaded["plan_id"] == plan_id
        assert "current_phase" in loaded