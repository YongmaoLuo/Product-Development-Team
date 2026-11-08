"""``ensure_verification_repairing`` establishes the repair phase loudly.

Background (2026-11-08): two plans crashed while closing a verification
round with ``ValueError: Cannot start repair execution from
'<phase>'. Must be in 'verification_repairing' phase.``  The auto
verification loop decides to run the pending RP-* queue after a round
that ended in ``verification_passed`` (round PASSED but repair tasks
still pending) or in ``verification_failed`` (``should_stop`` branch).
Neither of those exits walks the plan into ``verification_repairing``:
only the *ordinary* failure branch of ``check_cycle_conditions`` calls
``start_verification_repair``.  ``verification_failed`` has a legal edge
into the repair phase; the two terminal phases do not.

Contract exercised here:

  * ``verification_repairing`` — already there, nothing happens;
  * ``verification_failed`` — the legal edge is used, and nothing is
    forced (no ``plan_repair_phase_forced`` warning);
  * ``verification_passed`` / ``verification_loop_stopped`` — the two
    terminal phases are forced back into the repair phase, loudly;
  * anything else (``executing``, ``completed``, ...) — a caller bug,
    and it raises instead of silently doing the wrong thing.

The orchestrator-level cases go through the production entry point
``VerificationOrchestrator.confirm_repair_and_rerun`` — the method the
auto-loop actually calls — so the test cannot pass while the production
path stays broken.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import PlanState  # noqa: E402

#: The module-level logger of ``backend/plan_state.py``.
LOGGER_NAME = "plan_state"


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every case gets its own state DB."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _seed_plan(
    plan_dir: Path,
    *,
    phase: str,
    stop_reason: str | None = None,
    status: str = "pending",
) -> None:
    """Seed the ``plan_routing`` row the way the production write path does.

    ``PlanState`` reads its state from that row, so writing it here means
    the test and production take the same road.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_dir.name,
            phase=phase,
            completed_phases=["execution"],
            review_rounds={},
            flags={},
            verification={
                "status": status,
                "round": 0,
                "max_rounds": 3,
                "stop_reason": stop_reason,
            },
            last_updated=None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PlanState level
# ---------------------------------------------------------------------------


def test_already_repairing_stays(state_db: Path, tmp_path: Path):
    """``verification_repairing`` is left exactly as it is."""
    plan_dir = tmp_path / "plan-already-repairing"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase="verification_repairing")

    ps = PlanState(plan_dir)
    ps.ensure_verification_repairing()

    assert ps.get_current_phase() == "verification_repairing"


def test_verification_failed_uses_the_legal_edge(
    state_db: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """``verification_failed`` -> ``verification_repairing`` without a force."""
    plan_dir = tmp_path / "plan-from-failed"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase="verification_failed")

    ps = PlanState(plan_dir)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        ps.ensure_verification_repairing()

    assert ps.get_current_phase() == "verification_repairing"
    assert "plan_repair_phase_forced" not in caplog.text


def test_verification_passed_is_forced_loudly(
    state_db: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """``verification_passed`` is forced back into the repair phase."""
    plan_dir = tmp_path / "plan-from-passed"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase="verification_passed")

    ps = PlanState(plan_dir)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        ps.ensure_verification_repairing()

    assert ps.get_current_phase() == "verification_repairing"
    assert "plan_repair_phase_forced" in caplog.text
    assert "from=verification_passed" in caplog.text


def test_verification_loop_stopped_is_forced_loudly(
    state_db: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """``verification_loop_stopped`` is forced back into the repair phase."""
    plan_dir = tmp_path / "plan-from-loop-stopped"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase="verification_loop_stopped")

    ps = PlanState(plan_dir)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        ps.ensure_verification_repairing()

    assert ps.get_current_phase() == "verification_repairing"
    assert "plan_repair_phase_forced" in caplog.text
    assert "from=verification_loop_stopped" in caplog.text


@pytest.mark.parametrize("phase", ["executing", "completed"])
def test_other_phases_raise_and_do_not_move(
    state_db: Path,
    tmp_path: Path,
    phase: str,
):
    """Any other phase is a caller bug — it raises and changes nothing."""
    plan_dir = tmp_path / f"plan-from-{phase}"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase=phase)

    ps = PlanState(plan_dir)
    with pytest.raises(ValueError):
        ps.ensure_verification_repairing()

    assert ps.get_current_phase() == phase


# ---------------------------------------------------------------------------
# Orchestrator level — the production entry point the auto-loop calls
# ---------------------------------------------------------------------------


def _make_orchestrator(plan_dir: Path, project_dir: Path):
    """Build a ``VerificationOrchestrator`` the way production builds one."""
    from verification import VerificationOrchestrator

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        return VerificationOrchestrator(plan_dir, project_dir, Mock())


@pytest.mark.parametrize(
    "phase",
    [
        "verification_repairing",
        "verification_failed",
        "verification_passed",
        "verification_loop_stopped",
    ],
)
def test_confirm_repair_and_rerun_from_any_repairable_phase(
    state_db: Path,
    tmp_path: Path,
    phase: str,
):
    """The repair dispatch works from every phase the auto-loop can reach."""
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    _seed_plan(plan_dir, phase=phase)

    orch = _make_orchestrator(plan_dir, project_dir)
    orch.confirm_repair_and_rerun()

    ps = PlanState(plan_dir)
    assert ps.get_current_phase() == "executing"
    assert ps.get_verification_round() == 1


def test_confirm_repair_and_rerun_rejects_a_foreign_phase(
    state_db: Path,
    tmp_path: Path,
):
    """``executing`` is not a phase repairs may be dispatched from."""
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    _seed_plan(plan_dir, phase="executing")

    orch = _make_orchestrator(plan_dir, project_dir)
    with pytest.raises(ValueError):
        orch.confirm_repair_and_rerun()

    ps = PlanState(plan_dir)
    assert ps.get_current_phase() == "executing"
    assert ps.get_verification_round() == 0
