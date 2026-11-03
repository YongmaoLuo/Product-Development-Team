"""``PlanState.stop_verification_loop`` keeps the FIRST stop reason.

Background: a plan had already terminated its verification
round with ``base_drift_conflicts``.  A later stop rewrote
``verification.stop_reason`` to ``user_stopped``, erasing the real cause
of the termination.  On this layer the root cause was
:meth:`plan_state.PlanState.stop_verification_loop` writing the reason
unconditionally.

Contract exercised here:

  * an **empty** reason is written as before — the first stop owns the
    field — and the plan still transitions to ``verification_loop_stopped``;
  * a reason that is **already** recorded is kept: the attempted overwrite
    is only logged, the state is not touched and the transition is not
    repeated;
  * the vocabulary check runs **first**: an invalid reason still raises
    ``ValueError``, whether or not a reason is already recorded.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

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
# 1) The first stop owns the field.
# ---------------------------------------------------------------------------


def test_first_stop_writes_reason(state_db: Path, tmp_path: Path):
    """An empty reason is written, and the plan stops as usual."""
    plan_dir = tmp_path / "plan-first-stop"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase="verification_failed", stop_reason=None)

    ps = PlanState(plan_dir)
    ps.stop_verification_loop("max_rounds_reached")

    assert ps.get_verification_stop_reason() == "max_rounds_reached"
    assert ps.get_current_phase() == "verification_loop_stopped"
    assert ps.get_verification_status() == "loop_stopped"

    # An empty string is "no reason yet" too — it is written, not kept.
    empty_dir = tmp_path / "plan-first-stop-empty-string"
    empty_dir.mkdir()
    _seed_plan(empty_dir, phase="verification_failed", stop_reason="")

    ps_empty = PlanState(empty_dir)
    ps_empty.stop_verification_loop("user_stopped")

    assert ps_empty.get_verification_stop_reason() == "user_stopped"
    assert ps_empty.get_current_phase() == "verification_loop_stopped"


# ---------------------------------------------------------------------------
# 2) A reason that is already there stays.
# ---------------------------------------------------------------------------


def test_existing_reason_is_not_overwritten(
    state_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The second stop keeps the first reason and touches nothing else."""
    plan_dir = tmp_path / "plan-kept-reason"
    plan_dir.mkdir()
    _seed_plan(
        plan_dir,
        phase="verification_loop_stopped",
        stop_reason="base_drift_conflicts",
    )

    ps = PlanState(plan_dir)

    transitions: list[str] = []
    monkeypatch.setattr(
        ps, "transition_to", lambda phase: transitions.append(phase),
    )

    ps.stop_verification_loop("user_stopped")

    assert ps.get_verification_stop_reason() == "base_drift_conflicts"
    assert ps.get_current_phase() == "verification_loop_stopped"
    assert transitions == [], (
        "the kept-reason path must not repeat the transition"
    )


# ---------------------------------------------------------------------------
# 3) The vocabulary check still runs first.
# ---------------------------------------------------------------------------


def test_invalid_reason_still_raises(state_db: Path, tmp_path: Path):
    """An invalid reason is an error in every case — before or after a stop."""
    plan_dir = tmp_path / "plan-invalid-reason"
    plan_dir.mkdir()
    _seed_plan(plan_dir, phase="verification_failed", stop_reason=None)

    ps = PlanState(plan_dir)
    with pytest.raises(ValueError):
        ps.stop_verification_loop("not_a_valid_reason")

    assert ps.get_verification_stop_reason() is None
    assert ps.get_current_phase() == "verification_failed"

    # Same verdict when a reason is already recorded: the check runs
    # before the keep-or-write decision.
    kept_dir = tmp_path / "plan-invalid-reason-kept"
    kept_dir.mkdir()
    _seed_plan(
        kept_dir,
        phase="verification_loop_stopped",
        stop_reason="base_drift_conflicts",
    )

    ps_kept = PlanState(kept_dir)
    with pytest.raises(ValueError):
        ps_kept.stop_verification_loop("not_a_valid_reason")

    assert ps_kept.get_verification_stop_reason() == "base_drift_conflicts"


# ---------------------------------------------------------------------------
# 4) The attempted overwrite is logged.
# ---------------------------------------------------------------------------


def test_second_stop_logs_kept_and_attempted(
    state_db: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """The kept reason and the attempted one both show up in the log."""
    plan_dir = tmp_path / "plan-kept-log"
    plan_dir.mkdir()
    _seed_plan(
        plan_dir,
        phase="verification_loop_stopped",
        stop_reason="base_drift_conflicts",
    )

    ps = PlanState(plan_dir)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        ps.stop_verification_loop("user_stopped")

    messages = [record.getMessage() for record in caplog.records]
    assert any("plan_stop_reason_kept" in m for m in messages), (
        f"the kept reason must be logged, got {messages!r}"
    )
    assert any(
        f"plan={plan_dir.name} kept=base_drift_conflicts attempted=user_stopped" in m
        for m in messages
    ), f"the log must name the kept and the attempted reason, got {messages!r}"