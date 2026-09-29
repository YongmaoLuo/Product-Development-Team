import shutil
from pathlib import Path

import pytest

from plan_state import PlanState


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "legacy_plans"


class TestLegacyPlans:
    def test_legacy_minimal_loads(self):
        """Minimal plan_state.json loads without error."""
        plan_dir = FIXTURES_DIR / "plan_minimal"
        ps = PlanState(plan_dir)
        state = ps.get_state()
        assert state["plan_id"] == "plan_minimal"
        assert state["current_phase"] == "interview"
        assert state["completed_phases"] == []
        assert state["review_rounds"] == {"prd": 0, "arch": 0, "test": 0}
        assert state["flags"] == {}

    def test_legacy_unknown_phase_maps_to_file_inference(self):
        """Unknown phase in old JSON falls back to file-based phase inference.

        2026-09-13 contract update: with SQLite as the source of truth,
        a legacy plan whose ``plan_routing`` row does not exist falls
        back to ``PlanState._infer_phase()`` (file-based inference),
        NOT to the old "unknown phase → 'ready'" mapping. A fixture dir
        containing only a garbage-phase ``plan_state.json`` infers the
        default ``'interview'`` phase.
        """
        plan_dir = FIXTURES_DIR / "plan_unknown_phase"
        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "interview"

    def test_legacy_null_fields_normalized(self):
        """Null completed_phases/review_rounds/flags → empty defaults."""
        plan_dir = FIXTURES_DIR / "plan_unknown_phase"
        ps = PlanState(plan_dir)
        state = ps.get_state()
        assert state["completed_phases"] == []
        assert state["review_rounds"] == {"prd": 0, "arch": 0, "test": 0}
        assert state["flags"] == {}

    def test_legacy_transition_persists_new_schema(self, tmp_path):
        """After transition, the new schema is persisted to SQLite.

        2026-09-13 port: ``PlanState.transition_to`` persists to the
        ``plan_routing`` SQLite row; the ``plan_state.json`` mirror
        write was removed (the file is a one-shot migration input).
        The readback therefore goes through ``PlanState.get_state()``.
        """
        # Copy fixture to tmp_path so we don't mutate the shared fixture
        src = FIXTURES_DIR / "plan_minimal"
        plan_dir = tmp_path / "plan_minimal_copy"
        shutil.copytree(src, plan_dir)

        ps = PlanState(plan_dir)
        ps.transition_to("interview_complete")

        # Fresh instance reads the persisted SQLite row.
        raw = PlanState(plan_dir).get_state()
        assert "last_updated" in raw
        assert "completed_phases" in raw
        assert "review_rounds" in raw
        assert "flags" in raw
        assert raw["current_phase"] == "interview_complete"

    def test_legacy_full_workflow_no_exception(self, tmp_path):
        """Simulate full workflow transitions on legacy data without exceptions."""
        src = FIXTURES_DIR / "plan_minimal"
        plan_dir = tmp_path / "plan_minimal_copy"
        shutil.copytree(src, plan_dir)

        ps = PlanState(plan_dir)

        ps.transition_to("interview_complete")
        ps.transition_to("prd_generation")
        ps.transition_to("prd_review")
        ps.transition_to("prd_approved")
        ps.transition_to("tasks_generation")
        ps.transition_to("ready")
        ps.transition_to("executing")
        ps.transition_to("completed")

        assert ps.get_current_phase() == "completed"
        assert "execution" in ps.get_completed_phases()
