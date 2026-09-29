import json
import os
import time
from pathlib import Path

import pytest

from plan_state import PlanState, VALID_PHASES


def read_plan_routing_state(plan_id, db_path):
    """Read the legacy ``PlanState`` shape for ``plan_id`` from the
    ``plan_routing`` SQLite row at ``db_path``. (Task #3.7.)
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


class TestLoadStateFallback:
    """TDD tests for _load_state field-level fallback handling."""

    def test_missing_completed_phases_defaults_to_empty_list(self, tmp_path):
        plan_dir = tmp_path / "plan1"
        plan_dir.mkdir()
        state = {"plan_id": "plan1", "current_phase": "interview"}
        (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")

        ps = PlanState(plan_dir)
        assert ps.get_completed_phases() == []

    def test_null_completed_phases_defaults_to_empty_list(self, tmp_path):
        plan_dir = tmp_path / "plan2"
        plan_dir.mkdir()
        state = {"plan_id": "plan2", "current_phase": "interview", "completed_phases": None}
        (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")

        ps = PlanState(plan_dir)
        assert ps.get_completed_phases() == []

    def test_missing_review_rounds_defaults_to_zero_dict(self, tmp_path):
        plan_dir = tmp_path / "plan3"
        plan_dir.mkdir()
        state = {"plan_id": "plan3", "current_phase": "interview"}
        (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")

        ps = PlanState(plan_dir)
        assert ps.get_review_round("prd") == 0
        assert ps.get_review_round("arch") == 0
        assert ps.get_review_round("test") == 0

    def test_missing_flags_defaults_to_empty_dict(self, tmp_path):
        plan_dir = tmp_path / "plan4"
        plan_dir.mkdir()
        state = {"plan_id": "plan4", "current_phase": "interview"}
        (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")

        ps = PlanState(plan_dir)
        assert ps.is_arch_enabled() is False
        assert ps.is_test_enabled() is False

    def test_unknown_current_phase_maps_to_interview(self, tmp_path):
        """A legacy file with an unrecognised ``current_phase`` is
        normalised to ``interview`` by ``_load_state_from_legacy_file``
        (``current_phase not in VALID_PHASES → "interview"``).
        """
        plan_dir = tmp_path / "plan5"
        plan_dir.mkdir()
        state = {"plan_id": "plan5", "current_phase": "legacy_phase"}
        (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "interview"

    def test_load_does_not_immediately_write_back(self, tmp_path):
        plan_dir = tmp_path / "plan6"
        plan_dir.mkdir()
        state = {"plan_id": "plan6", "current_phase": "interview"}
        state_file = plan_dir / "plan_state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")

        mtime_before = state_file.stat().st_mtime
        time.sleep(0.05)

        PlanState(plan_dir)

        mtime_after = state_file.stat().st_mtime
        assert mtime_before == mtime_after

    def test_transition_to_triggers_save_with_normalized_schema(self, tmp_path, state_db):
        plan_dir = tmp_path / "plan7"
        plan_dir.mkdir()
        state = {"plan_id": "plan7", "current_phase": "interview"}
        state_file = plan_dir / "plan_state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")

        ps = PlanState(plan_dir)
        ps.transition_to("interview_complete")

        loaded = read_plan_routing_state("plan7", state_db)
        assert "completed_phases" in loaded
        assert loaded["completed_phases"] == ["interview"]
        assert "review_rounds" in loaded
        assert loaded["review_rounds"] == {"prd": 0, "arch": 0, "test": 0}
        assert "flags" in loaded
        assert loaded["flags"] == {}
