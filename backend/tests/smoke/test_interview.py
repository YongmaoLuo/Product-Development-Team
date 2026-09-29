"""
Smoke test for the interview requirement-clarification phase.

The interview phase collects structured requirements through five
mandatory dimensions (background / goals / scope / constraints /
acceptance) before the system can advance to PRD generation.  This
test exercises the direct-answer endpoint that bypasses the LLM
interviewer and asserts:

  * ``test_interview_complete``: 5 dimensions covered →
    ``current_phase == "interview_complete"``.
  * ``test_interview_json_persisted``: interview.json contains all
    five dimension keys.

The test uses the FastAPI ``TestClient`` and patches ``PLANS_DIR``
to a temporary directory so the global state is isolated per test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


REQUIRED_DIMENSIONS = [
    "background",
    "goals",
    "scope",
    "constraints",
    "acceptance",
]


def _make_plan_dir(tmp_path: Path, plan_id: str) -> Path:
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "plan_id": plan_id,
        "current_phase": "interview",
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )
    return plan_dir


def _load_plan_state(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "plan_state.json").read_text(encoding="utf-8"))


def _load_interview(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "interview.json").read_text(encoding="utf-8"))


def test_interview_complete(tmp_path, monkeypatch):
    """Submitting answers for all 5 dimensions flips plan state to
    ``interview_complete`` and the response confirms coverage."""
    from server import app

    plan_id = "smoke-test-interview-complete"
    plan_dir = _make_plan_dir(tmp_path, plan_id)
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")

    client = TestClient(app)

    for dimension in REQUIRED_DIMENSIONS:
        response = client.post(
            f"/api/interview/{plan_id}/answer",
            json={"dimension": dimension, "answer": f"smoke {dimension} content"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["plan_id"] == plan_id
        assert dimension in payload["dimensions_covered"]

    # The 5th (final) answer should mark the interview complete.
    final_payload = response.json()
    assert final_payload["complete"] is True
    assert set(final_payload["dimensions_covered"]) == set(REQUIRED_DIMENSIONS)
    assert final_payload["current_phase"] == "interview_complete"

    # Persisted state also reflects the transition — read back through
    # PlanState (the plan_routing SQLite row is canonical; the
    # plan_state.json mirror write was removed 2026-09-13).
    from plan_state import PlanState

    state = PlanState(plan_dir).get_state()
    assert state["current_phase"] == "interview_complete"


def test_interview_json_persisted(tmp_path, monkeypatch):
    """interview.json is created and contains all five dimension keys
    after the answers are submitted."""
    from server import app

    plan_id = "smoke-test-interview-persisted"
    plan_dir = _make_plan_dir(tmp_path, plan_id)
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")

    client = TestClient(app)

    for dimension in REQUIRED_DIMENSIONS:
        client.post(
            f"/api/interview/{plan_id}/answer",
            json={
                "dimension": dimension,
                "answer": f"smoke {dimension} persisted value",
            },
        )

    assert (plan_dir / "interview.json").exists(), (
        "interview.json should be persisted on disk"
    )

    interview = _load_interview(plan_dir)
    dimensions = interview.get("dimensions", {})
    for dimension in REQUIRED_DIMENSIONS:
        assert dimension in dimensions, (
            f"interview.json missing dimension key: {dimension}"
        )
        assert dimensions[dimension].strip() != "", (
            f"interview.json dimension {dimension!r} is empty"
        )
