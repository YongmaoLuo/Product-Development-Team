"""VP-002: Plan State Machine Phase Transitions.

Covers the API contract for ``POST /api/plan/{id}/state``:

  * Valid phase transitions return 200 and the persisted
    ``plan_routing`` row reflects the new ``current_phase``.
  * Invalid phase jumps (e.g. ``interview`` -> ``executing``) are
    rejected with HTTP 400 (client error), never 500.
  * Invalid phase names (e.g. ``nonexistent_phase``) are also rejected
    with 400.
  * A failed transition must NOT mutate ``plan_routing`` — the
    persisted value still matches the last accepted transition.

Task #3.7: persistence moved from ``plan_state.json`` to the
``plan_routing`` SQLite table. These tests point ``PDT_STATE_DB_PATH``
at a hermetic per-test database and read the row back via
:func:`read_plan_routing_state` instead of reading the legacy file.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plan_state import PlanState
from server import app


client = TestClient(app)


def read_plan_routing_state(plan_id, db_path):
    """Read the legacy ``PlanState`` shape for ``plan_id`` from the
    ``plan_routing`` SQLite row at ``db_path``.

    Task #3.7 moved plan-state persistence from ``plan_state.json``
    to the ``plan_routing`` table. Tests that used to assert on the
    JSON file now use this helper (with ``PDT_STATE_DB_PATH`` pointed
    at a hermetic tmp database) to assert on the single source of
    truth instead. Returns the same dict shape ``PlanState.get_state()``
    yields.
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_db(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    if row is None:
        return None
    return PlanState._sqlite_row_to_state(row)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _setup_plan(tmp_path, plan_id, current_phase):
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_state = {
        "plan_id": plan_id,
        "current_phase": current_phase,
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(plan_state), encoding="utf-8",
    )
    return plan_dir


def _patch_plans_dir(monkeypatch, plans_dir):
    import server
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------


class TestValidPhaseTransitions:
    def test_legal_transition_interview_to_interview_complete(self, tmp_path, monkeypatch, state_db):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "p-1", "interview")

        resp = client.post(
            "/api/plan/p-1/state",
            json={"current_phase": "interview_complete"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["current_phase"] == "interview_complete"
        on_disk = read_plan_routing_state("p-1", state_db)
        assert on_disk["current_phase"] == "interview_complete"

    def test_legal_transition_interview_complete_to_prd_generation(self, tmp_path, monkeypatch, state_db):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "p-2", "interview_complete")

        resp = client.post(
            "/api/plan/p-2/state",
            json={"current_phase": "prd_generation"},
        )

        assert resp.status_code == 200, resp.text
        on_disk = read_plan_routing_state("p-2", state_db)
        assert on_disk["current_phase"] == "prd_generation"

    def test_legal_transition_executing_to_completed(self, tmp_path, monkeypatch, state_db):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "p-3", "executing")

        resp = client.post(
            "/api/plan/p-3/state",
            json={"current_phase": "completed"},
        )

        assert resp.status_code == 200, resp.text
        on_disk = read_plan_routing_state("p-3", state_db)
        assert on_disk["current_phase"] == "completed"

    def test_legal_transition_prd_review_to_prd_refining(self, tmp_path, monkeypatch, state_db):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "p-4", "prd_review")

        resp = client.post(
            "/api/plan/p-4/state",
            json={"current_phase": "prd_refining"},
        )

        assert resp.status_code == 200, resp.text
        on_disk = read_plan_routing_state("p-4", state_db)
        assert on_disk["current_phase"] == "prd_refining"


# ---------------------------------------------------------------------------
# Invalid phase transitions — must return 400, NOT 500
# ---------------------------------------------------------------------------


class TestInvalidPhaseTransitions:
    def test_invalid_phase_transition_interview_to_executing(self, tmp_path, monkeypatch):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "bad-1", "interview")

        resp = client.post(
            "/api/plan/bad-1/state",
            json={"current_phase": "executing"},
        )

        assert resp.status_code == 400, (
            f"Expected 400 for illegal phase jump, got {resp.status_code}: {resp.text}"
        )
        assert resp.status_code < 500
        on_disk = json.loads(
            (tmp_path / "plans" / "bad-1" / "plan_state.json").read_text(encoding="utf-8")
        )
        assert on_disk["current_phase"] == "interview"

    def test_invalid_phase_transition_ready_to_completed(self, tmp_path, monkeypatch):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "bad-2", "ready")

        resp = client.post(
            "/api/plan/bad-2/state",
            json={"current_phase": "completed"},
        )

        assert resp.status_code == 400, (
            f"Expected 400 for ready->completed, got {resp.status_code}: {resp.text}"
        )
        on_disk = json.loads(
            (tmp_path / "plans" / "bad-2" / "plan_state.json").read_text(encoding="utf-8")
        )
        assert on_disk["current_phase"] == "ready"

    def test_invalid_phase_transition_prd_generation_to_arch_approved(self, tmp_path, monkeypatch):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "bad-3", "prd_generation")

        resp = client.post(
            "/api/plan/bad-3/state",
            json={"current_phase": "arch_approved"},
        )

        assert resp.status_code == 400, (
            f"Expected 400, got {resp.status_code}: {resp.text}"
        )
        on_disk = json.loads(
            (tmp_path / "plans" / "bad-3" / "plan_state.json").read_text(encoding="utf-8")
        )
        assert on_disk["current_phase"] == "prd_generation"


# ---------------------------------------------------------------------------
# Invalid phase NAME
# ---------------------------------------------------------------------------


class TestInvalidPhaseNames:
    def test_invalid_phase_name_returns_400(self, tmp_path, monkeypatch):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "bad-name-1", "interview")

        resp = client.post(
            "/api/plan/bad-name-1/state",
            json={"current_phase": "nonexistent_phase"},
        )

        assert resp.status_code == 400, (
            f"Expected 400 for unknown phase, got {resp.status_code}: {resp.text}"
        )
        on_disk = json.loads(
            (tmp_path / "plans" / "bad-name-1" / "plan_state.json").read_text(encoding="utf-8")
        )
        assert on_disk["current_phase"] == "interview"

    def test_invalid_phase_empty_string_returns_400(self, tmp_path, monkeypatch):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "bad-name-2", "interview")

        resp = client.post(
            "/api/plan/bad-name-2/state",
            json={"current_phase": ""},
        )

        assert resp.status_code in (400, 422), (
            f"Expected 4xx for empty phase, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Persistence — last accepted value matches plan_state.json on disk
# ---------------------------------------------------------------------------


class TestPersistedState:
    def test_persisted_state_matches_last_accepted_transition(self, tmp_path, monkeypatch, state_db):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        _setup_plan(tmp_path, "persist-1", "interview")

        sequence = ["interview_complete", "prd_generation", "prd_review"]
        for phase in sequence:
            resp = client.post(
                "/api/plan/persist-1/state",
                json={"current_phase": phase},
            )
            assert resp.status_code == 200, (
                f"Transition to {phase} failed: {resp.status_code} {resp.text}"
            )

        on_disk = read_plan_routing_state("persist-1", state_db)
        assert on_disk["current_phase"] == sequence[-1]

    def test_persisted_state_unchanged_after_rejected_transition(self, tmp_path, monkeypatch):
        _patch_plans_dir(monkeypatch, tmp_path / "plans")
        plan_dir = _setup_plan(tmp_path, "persist-2", "interview")

        resp = client.post(
            "/api/plan/persist-2/state",
            json={"current_phase": "executing"},
        )
        assert resp.status_code == 400

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "interview"
