import pytest

from plan_state import VALID_PHASES, TERMINAL_PHASES, PHASE_TRANSITIONS, PlanState


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Task #3.7: plan-state persists to ``plan_routing`` SQLite. Point
    each test at a hermetic per-test database so repeated runs (and the
    shared ``test-plan`` plan_id) don't read stale rows from the
    repo-root ``state.db``.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


class TestValidPhases:
    def test_valid_phases_contains_terminals(self):
        assert "completed" in VALID_PHASES
        assert "failed" in VALID_PHASES
        assert "stopped" in VALID_PHASES


class TestPhaseTransitions:
    def test_legal_executing_to_completed(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("completed")
        assert ps.get_current_phase() == "completed"

    def test_legal_executing_to_failed(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("failed")
        assert ps.get_current_phase() == "failed"

    def test_legal_executing_to_stopped(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("stopped")
        assert ps.get_current_phase() == "stopped"

    def test_illegal_completed_to_executing(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="completed")
        ps = PlanState(plan_dir)
        with pytest.raises(ValueError, match="Illegal transition"):
            ps.transition_to("executing")

    def test_illegal_ready_to_completed(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        with pytest.raises(ValueError, match="Illegal transition"):
            ps.transition_to("completed")

    def test_illegal_prd_generation_to_arch_approved(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="prd_generation")
        ps = PlanState(plan_dir)
        with pytest.raises(ValueError, match="Illegal transition"):
            ps.transition_to("arch_approved")

    def test_review_loop_prd_review_to_prd_refining_to_prd_review(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="prd_review")
        ps = PlanState(plan_dir)
        ps.transition_to("prd_refining")
        assert ps.get_current_phase() == "prd_refining"
        ps.transition_to("prd_review")
        assert ps.get_current_phase() == "prd_review"


class TestTerminalAutoAppend:
    def test_terminal_appends_execution(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("completed")
        assert "execution" in ps.get_completed_phases()

    def test_append_idempotent(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("completed")
        # Repeating transition to the same terminal state is a no-op
        ps.transition_to("completed")
        assert ps.get_completed_phases().count("execution") == 1

    def test_failed_appends_execution(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("failed")
        assert "execution" in ps.get_completed_phases()

    def test_terminal_stopped_appends_execution(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        ps = PlanState(plan_dir)
        ps.transition_to("stopped")
        assert "execution" in ps.get_completed_phases()


class TestTransitionValidation:
    def test_invalid_phase_raises(self, sample_plan_factory):
        plan_dir = sample_plan_factory()
        ps = PlanState(plan_dir)
        with pytest.raises(ValueError, match="Invalid phase"):
            ps.transition_to("nonexistent_phase")
