"""Regression tests for the verification state-machine repairs (2026-08-19 audit).

Background
----------
A plan hit a verification deadlock where
51 verification points all PASSED but ``/api/verification/{id}/status``
still reported ``status="running", round=1`` for 4.5 hours. The root
cause was a chain of independent defects:

1. ``VerificationRepository.complete_round`` hard-coded ``status="failed"``
   regardless of the actual outcome, so a passing round always surfaced
   as failed in the SQLite ``plan_verification`` row.
2. The auto-loop never persisted the round number to ``plan_verification``
   so the round counter frozen at 1.
3. The auto-loop CAS'd the routing row to ``completed`` only on the
   PASSED path, so stage stayed at ``verification_running`` — and the
   status endpoint then forced ``status="running"`` whenever the stage
   column was ``verification_running``.
4. The ``/start`` worker's blanket ``except Exception`` silently swallowed
   ``Illegal transition`` from a stale ``PlanState`` instance, leaving
   the phase frozen.
5. The ``PlanState`` in-memory cache drifted across multiple instances
   (orchestrator, ``/start`` worker, auto-loop).

The fix repairs all five. These tests pin the new behaviour so future
refactors don't reintroduce the deadlock.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Pull the helpers under test. The module name shadows the ``server``
# module that owns the routes, so we import the individual functions
# directly.
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import VerificationRepository
from state_machine.repositories.routing_repository import RoutingRepository


@pytest.fixture
def state_db():
    """Yield a hermetic ``state.db`` SQLite connection for one test."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "state.db"
        conn = open_db(db_path)
        migrate(conn)
        try:
            yield conn
        finally:
            conn.close()


# ----------------------------------------------------------------------
# Bug 1.1: complete_round must accept a status parameter
# ----------------------------------------------------------------------

def test_complete_round_persists_passed_status(state_db):
    """A passing round must write ``verification_status='passed'``,
    not the legacy hard-coded ``failed``."""
    plan_id = "test-complete-round-passed"
    repo = VerificationRepository(state_db)
    repo.insert(plan_id, "running")
    repo.init_round(plan_id, round_n=1, max_rounds=3)
    repo.complete_round(
        plan_id,
        {"status": "passed"},
        status="passed",
    )
    row = repo.current(plan_id)
    assert row["verification_status"] == "passed", (
        "complete_round(passed) must persist 'passed' (legacy code always "
        "wrote 'failed')."
    )
    assert row["results"]["status"] == "passed"


def test_complete_round_rejects_invalid_status(state_db):
    """Defensive guard: only the canonical status set is accepted."""
    plan_id = "test-complete-round-validation"
    repo = VerificationRepository(state_db)
    repo.insert(plan_id, "running")
    repo.init_round(plan_id, round_n=1, max_rounds=3)
    with pytest.raises(ValueError):
        repo.complete_round(
            plan_id,
            {"status": "YOLO"},
            status="YOLO",
        )


def test_complete_round_default_status_still_failed_for_backcompat(state_db):
    """Backwards compatibility: a call site that does NOT pass the new
    keyword must still get ``failed`` (the legacy behaviour that callers
    like the start endpoint used to rely on)."""
    plan_id = "test-complete-round-default"
    repo = VerificationRepository(state_db)
    repo.insert(plan_id, "running")
    repo.init_round(plan_id, round_n=1, max_rounds=3)
    repo.complete_round(plan_id, {"status": "failed"})
    row = repo.current(plan_id)
    assert row["verification_status"] == "failed"


# ----------------------------------------------------------------------
# Bug 1.5: PlanState.reload() picks up parallel writer's updates
# ----------------------------------------------------------------------

def test_plan_state_reload_picks_up_parallel_writes(tmp_path, monkeypatch):
    """Two PlanState instances pointed at the same plan_dir must
    converge after one calls ``reload()``."""
    plan_dir = tmp_path / "plans" / "test-reload"
    plan_dir.mkdir(parents=True)

    # Pin the state-machine DB to a hermetic sandbox so the PlanState
    # loader doesn't reach into the dev repo's state.db.
    sandbox_db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(sandbox_db))

    from plan_state import PlanState

    writer = PlanState(plan_dir)
    reader = PlanState(plan_dir)

    # Force a SQLite row to exist so the loader doesn't take the
    # legacy-file branch.
    writer.set_verification_max_rounds(3)
    # Round 1: simulate the auto-loop bumping the round number.
    writer.increment_verification_round()
    writer.increment_verification_round()
    fresh_round = writer.get_verification_round()
    assert fresh_round >= 2

    # Reader's in-memory cache is stale.
    stale_round = reader.get_verification_round()
    assert stale_round != fresh_round, (
        "Setup precondition: the reader must be stale before reload."
    )

    # Fix: reload() converges the reader.
    reader.reload()
    assert reader.get_verification_round() == fresh_round, (
        "PlanState.reload() must surface the latest persisted round number."
    )


# ----------------------------------------------------------------------
# Bug 1.6: verification_loop_stopped → verification_passed is a legal edge
# ----------------------------------------------------------------------

def test_verification_loop_stopped_to_verification_passed_is_legal():
    """The state-machine table must admit ``verification_loop_stopped`` →
    ``verification_passed`` so the auto-loop can stop on a passing round
    without falling through to ``failed``."""
    from plan_state import VERIFICATION_PHASE_TRANSITIONS

    allowed = set(VERIFICATION_PHASE_TRANSITIONS.get("verification_loop_stopped", []))
    assert "verification_passed" in allowed, (
        "Add 'verification_passed' to verification_loop_stopped's allowed "
        "targets so the auto-loop can stop on a passing round without "
        "clobbering the verdict."
    )
    # Backwards compatibility: the legacy edge to ``failed`` must stay.
    assert "failed" in allowed


# ----------------------------------------------------------------------
# Bug 1.2: routing stage must leave verification_running on the PASSED path
# ----------------------------------------------------------------------

def test_routing_stage_advances_on_passed(state_db):
    """The auto-loop's PASSED branch must CAS the routing row from
    ``verification_running`` to ``completed`` so the status endpoint
    stops forcing ``status="running"``."""
    plan_id = "test-routing-stage-advance"
    routing = RoutingRepository(state_db)
    routing.write_plan_state(
        plan_id,
        phase="verification_running",
        completed_phases=[],
        review_rounds={},
        flags={},
        verification={},
        last_updated="2026-08-20T00:00:00Z",
    )
    # The fixed auto-loop calls this exact CAS.
    routing.try_mark_phase(
        plan_id,
        ("verification_running", "verification_rerunning"),
        "completed",
    )
    row = routing.current(plan_id)
    assert row["current_phase"] == "completed", (
        "After the PASSED branch CAS, the routing row must show "
        "``completed`` (or another non-verification_running stage) "
        "so the status endpoint stops forcing ``status='running'``."
    )
