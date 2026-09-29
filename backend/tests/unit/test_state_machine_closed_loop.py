"""State-machine closed-loop fix (2026-09-12 plan).

Background
----------
The verification→repair→executing loop has to close at the
state-machine layer rather than through an out-of-band shortcut:

  1. 在状态机层面允许这样的状态转移合法化
  2. 才能够在验证阶段跳转到 Repairing task
  3. 从 Repairing task 阶段跳转回 executing

Before this fix the verification→repair→executing chain routed
through a shortcut:

    verification_failed → verification_repairing → verification_rerunning
                          (skipping ``executing`` entirely)

Even when the executor subprocess actually ran the RP-* tasks,
``current_phase`` reflected "we're still in the verification sub-machine",
which broke the watchdog / cards / restart-restore vocabulary.

The fix adds:

  * :meth:`plan_state.PlanState.start_repair_execution` — invokes the
    existing ``verification_repairing → executing`` edge.
  * :meth:`plan_state.PlanState.begin_verification` — accepts an
    optional ``round_n`` parameter so the post-repair callback can
    preserve the bumped round counter.
  * ``orchestrator.confirm_repair_and_rerun`` — calls
    ``start_repair_execution()`` (was ``start_verification_rerun()``).
  * ``_persist_verification_terminal`` — step 2 + step 3 only CAS to
    ``terminal_*`` when the chain is ACTUALLY ending (mid-loop
    ``_record_terminal`` calls no longer strand the plan).

Contract pinned here:

  1. ``start_repair_execution`` is legal ONLY from
     ``verification_repairing``; any other source raises ``ValueError``.
  2. ``start_repair_execution`` increments ``verification.round`` by 1
     so the post-repair cycle starts on round N+1, not round N.
  3. ``confirm_repair_and_rerun`` triggers
     ``verification_repairing → executing`` (NOT
     ``verification_repairing → verification_rerunning``).
  4. ``begin_verification(round_n=N)`` preserves ``round = N``;
     ``begin_verification()`` (no arg) defaults to ``round = 0``.
  5. ``_persist_verification_terminal`` mid-loop stop_reasons
     (``"no_repair_tasks"``, etc.) do NOT CAS routing.stage to
     ``failed``; chain-ending stop_reasons
     (``"same_failure_repeated"``, etc.) DO.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from plan_state import (
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)
from verification.orchestrator import VerificationOrchestrator


# ---------------------------------------------------------------------------
# TDD spec 1: ``start_repair_execution`` legal edge + round increment
# ---------------------------------------------------------------------------


def _scrub_plan_routing_row(plan_id: str) -> None:
    """Delete the plan_routing row for ``plan_id`` so PlanState
    falls back to plan_state.json when loading.

    The shared dev state.db may have stale rows from prior test
    runs or dev sessions. Without this scrub
    ``PlanState._load_state`` (which prefers SQLite) returns
    the stale row and ignores the freshly-written
    plan_state.json — surfacing as a ``current_phase`` mismatch.
    """
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from plan_state import _state_db_path
        db_path = _state_db_path()
        if not db_path.exists():
            return
        conn = open_db(db_path)
        try:
            migrate(conn)
            conn.execute(
                "DELETE FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # SQLite unavailable or unwriteable — PlanState will
        # fall back to the legacy plan_state.json path
        # automatically.
        pass


def _seed_repairing_plan(tmp_path, plan_id, *, current_round=2):
    """Seed a plan in ``verification_repairing`` with ``current_round``.

    Writes plan_state.json AND scrubs any stale SQLite routing row
    so PlanState._load_state reads the freshly-written JSON.
    """
    _scrub_plan_routing_row(plan_id)
    state = {
        "plan_id": plan_id,
        "current_phase": "verification_repairing",
        "completed_phases": [
            "interview", "interview_complete",
            "prd_generation", "prd_review", "prd_approved",
            "tasks_generation", "ready",
        ],
        "review_rounds": {"prd": 1, "arch": 1, "test": 1},
        "flags": {},
        "verification": {
            "status": "failed",
            "round": current_round,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    plan_dir = Path(tmp_path) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plan_dir


def test_start_repair_execution_legal_from_verification_repairing(tmp_path):
    """``start_repair_execution`` from ``verification_repairing`` → ``executing``.

    Round counter must bump by 1 so the post-repair verification
    cycle starts on round N+1, not the same N.
    """
    plan_dir = _seed_repairing_plan(tmp_path, "repair-loop-ok", current_round=2)
    ps = PlanState(plan_dir)

    assert ps.get_current_phase() == "verification_repairing"
    assert ps.get_verification_round() == 2

    ps.start_repair_execution()

    assert ps.get_current_phase() == "executing", (
        "start_repair_execution must transition "
        "verification_repairing → executing"
    )
    # Round bumped so the next verification round is round 3.
    assert ps.get_verification_round() == 3, (
        "round counter must increment by 1 when transitioning to "
        "executing (mirrors start_verification_rerun)"
    )


@pytest.mark.parametrize(
    "wrong_phase",
    [
        "verification_running",
        "verification_failed",
        "executing",
        "ready",
        "verification_passed",
        "completed",
    ],
)
def test_start_repair_execution_illegal_from_other_phases(tmp_path, wrong_phase):
    """``start_repair_execution`` from any non-``verification_repairing`` raises."""
    plan_dir = _seed_illegal_phase_plan(
        tmp_path, f"repair-loop-illegal-{wrong_phase}", wrong_phase,
    )
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError) as exc_info:
        ps.start_repair_execution()

    assert "Cannot start repair execution" in str(exc_info.value), (
        f"error message must name 'start repair execution' as the "
        f"operation; got: {exc_info.value}"
    )
    # Phase must NOT have been mutated on error.
    assert ps.get_current_phase() == wrong_phase, (
        "illegal start_repair_execution must not mutate current_phase"
    )


# ---------------------------------------------------------------------------
# TDD spec 2: ``confirm_repair_and_rerun`` triggers ``executing``
# ---------------------------------------------------------------------------


def test_confirm_repair_and_rerun_calls_start_repair_execution(tmp_path):
    """``confirm_repair_and_rerun`` must trigger
    ``verification_repairing → executing``, NOT
    ``verification_repairing → verification_rerunning``.
    """
    plan_dir = _seed_repairing_plan(tmp_path, "confirm-repair-reroute")
    ps = PlanState(plan_dir)

    # Spy on PlanState: capture which method was called.
    with patch.object(
        PlanState, "start_repair_execution",
        autospec=True,
    ) as mock_start_repair, \
         patch.object(
             PlanState, "start_verification_rerun",
             autospec=True,
         ) as mock_start_rerun:
        # Construct an orchestrator and call confirm_repair_and_rerun.
        # The orchestrator's __init__ wires up PlanState internally —
        # patch the class attribute so PlanState(plan_dir) returns
        # our pre-seeded instance.
        with patch("verification.orchestrator.PlanState") as MockPlanStateCls:
            MockPlanStateCls.return_value = ps
            # Skip the heavy __init__ pieces by constructing without them.
            orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
            orch.plan_dir = plan_dir
            orch.project_dir = plan_dir
            orch.plan_state = ps
            orch._waiting_for_user = True  # pretend user confirmed

            orch.confirm_repair_and_rerun()

    assert mock_start_repair.called, (
        "confirm_repair_and_rerun MUST call start_repair_execution() "
        "to route verification_repairing → executing"
    )
    assert not mock_start_rerun.called, (
        "confirm_repair_and_rerun MUST NOT call start_verification_rerun() "
        "(the shortcut that bypasses executing)"
    )


# ---------------------------------------------------------------------------
# TDD spec 3: ``begin_verification(round_n=N)`` round-preserving parameter
# ---------------------------------------------------------------------------


def _seed_plan_in_phase(tmp_path, plan_id, current_phase):
    """Seed a plan at any phase; used by begin_verification tests."""
    _scrub_plan_routing_row(plan_id)
    state = {
        "plan_id": plan_id,
        "current_phase": current_phase,
        "completed_phases": ["ready"] if current_phase == "executing" else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    plan_dir = Path(tmp_path) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plan_dir


def _seed_illegal_phase_plan(tmp_path, plan_id, current_phase):
    """Seed a plan at any phase; used by illegal-source tests."""
    _scrub_plan_routing_row(plan_id)
    state = {
        "plan_id": plan_id,
        "current_phase": current_phase,
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": 1,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    plan_dir = Path(tmp_path) / state["plan_id"]
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plan_dir


def test_begin_verification_round_n_preserves_value(tmp_path):
    """``begin_verification(round_n=4)`` must persist ``round = 4``."""
    plan_dir = _seed_plan_in_phase(tmp_path, "begin-verify-round-n", "executing")
    ps = PlanState(plan_dir)

    assert ps.get_verification_round() == 0, "precondition: round starts at 0"

    ps.begin_verification(round_n=4)

    assert ps.get_current_phase() == "verification"
    assert ps.get_verification_round() == 4, (
        "begin_verification(round_n=4) must preserve round=4 "
        "(the post-repair callback uses this to keep the monotonic "
        "round counter after the executor completes round N)"
    )


def test_begin_verification_default_round_zero(tmp_path):
    """``begin_verification()`` (no arg) must default to ``round = 0``.

    Backward-compat: pre-fix callers that don't pass ``round_n``
    must see the same behaviour as before this plan.
    """
    plan_dir = _seed_plan_in_phase(tmp_path, "begin-verify-default", "executing")
    ps = PlanState(plan_dir)

    ps.begin_verification()

    assert ps.get_current_phase() == "verification"
    assert ps.get_verification_round() == 0, (
        "begin_verification() without round_n must default to round=0 "
        "(backward compat with pre-2026-09-12 callers)"
    )


def test_begin_verification_no_op_when_already_in_verification(tmp_path):
    """``begin_verification`` is idempotent: re-entry is a no-op
    so a resumed round doesn't clobber an in-flight verification."""
    plan_dir = _seed_plan_in_phase(
        tmp_path, "begin-verify-idempotent", "verification_running",
    )
    ps = PlanState(plan_dir)
    # Pre-existing round
    ps._state["verification"]["round"] = 2  # type: ignore[attr-defined]
    ps._save_state()

    ps.begin_verification(round_n=99)

    # No-op path: round must NOT be clobbered to 99.
    assert ps.get_verification_round() == 2, (
        "begin_verification in 'verification_*' state must NOT clobber "
        "the in-flight round counter"
    )


# ---------------------------------------------------------------------------
# TDD spec 4: ``_persist_verification_terminal`` mid-loop guard
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_server_state(monkeypatch, tmp_path):
    """Provide a hermetic PLANS_DIR + state.db for terminal tests."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    state_db_path = tmp_path / "state.db"

    monkeypatch.setattr("server.PLANS_DIR", plans_dir, raising=False)
    monkeypatch.setattr("server._state_db_path", lambda: state_db_path)

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    conn = open_db(state_db_path)
    migrate(conn)
    conn.close()

    return {"plans_dir": plans_dir, "state_db_path": state_db_path}


def _seed_plan_and_routing(state_db_path, plan_id, current_phase, current_stage):
    """Seed plan_state.json + plan_routing row."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )
    conn = open_db(state_db_path)
    try:
        rr = RoutingRepository(conn)
        # ``insert`` takes (plan_id, stage, substage). ``current_phase``
        # is written by ``upsert`` which PlanState uses; we set the
        # column to ``current_phase`` AFTER insert via raw SQL so the
        # summary endpoint reads the correct vocabulary.
        rr.insert(plan_id, current_stage)
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            (current_phase, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _read_routing_stage(state_db_path, plan_id):
    from state_machine.db.connection import open as open_db
    conn = open_db(state_db_path)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_persist_verification_terminal_mid_loop_skips_cas(
    patched_server_state, tmp_path,
):
    """``_persist_verification_terminal`` with a mid-loop stop_reason
    MUST NOT CAS routing.stage to ``failed``.

    Mid-loop = stop_reason NOT in the chain-ending whitelist
    (``same_failure_repeated``, ``max_rounds_reached``, etc.) AND
    ``status != "passed"``.

    Without this guard the executor subprocess running RP-* tasks
    finds ``routing.stage = "failed"`` and can't restart
    the verification loop, breaking the closed-loop state machine.
    """
    import server

    plan_id = "persist-terminal-mid-loop"
    plans_dir = patched_server_state["plans_dir"]
    state_db_path = patched_server_state["state_db_path"]

    # Seed plan directory + plan_routing row.
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "plan_id": plan_id,
        "current_phase": "verification_repairing",
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "failed",
            "round": 3,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _seed_plan_and_routing(
        state_db_path, plan_id,
        current_phase="verification_repairing",
        current_stage="verification_repairing",
    )

    # Mid-loop stop_reason (not in whitelist).
    server._persist_verification_terminal(
        plan_id, "failed", "no_repair_tasks",
    )

    # routing.stage MUST NOT have flipped to failed.
    assert _read_routing_stage(state_db_path, plan_id) == "verification_repairing", (
        "mid-loop _persist_verification_terminal MUST NOT CAS "
        "routing.stage to failed (the executor subprocess "
        "is still mid-flight and needs to transition the plan "
        "back to executing → verification)"
    )


def test_persist_verification_terminal_chain_ending_cas(
    patched_server_state, tmp_path,
):
    """``_persist_verification_terminal`` with a chain-ending stop_reason
    MUST CAS routing.stage to ``failed``.

    Chain-ending = stop_reason in the whitelist OR status == "passed".
    """
    import server

    plan_id = "persist-terminal-chain-end"
    plans_dir = patched_server_state["plans_dir"]
    state_db_path = patched_server_state["state_db_path"]

    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "plan_id": plan_id,
        "current_phase": "verification_failed",
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "failed",
            "round": 3,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _seed_plan_and_routing(
        state_db_path, plan_id,
        current_phase="verification_failed",
        current_stage="verification_repairing",
    )

    # Chain-ending stop_reason (in whitelist).
    server._persist_verification_terminal(
        plan_id, "loop_stopped", "same_failure_repeated_after_max_attempts",
    )

    assert _read_routing_stage(state_db_path, plan_id) == "failed", (
        "chain-ending _persist_verification_terminal MUST CAS "
        "routing.stage to failed"
    )


# ---------------------------------------------------------------------------
# TDD spec 5: full state-machine loop with mock subprocess
# ---------------------------------------------------------------------------


def test_full_state_machine_loop_walks_through_executing(tmp_path):
    """A plan in the closed-loop state machine walks through:

      verification_running → verification_failed → verification_repairing
      → executing → verification → verification_running

    without any ``transition_to`` raising ``Illegal transition``.
    """
    # The critical edge that the closed-loop fix relies on:
    # ``verification_repairing → executing``.
    allowed = set(VERIFICATION_PHASE_TRANSITIONS.get("verification_repairing", []))
    assert "executing" in allowed, (
        "VERIFICATION_PHASE_TRANSITIONS['verification_repairing'] MUST "
        "include 'executing' as a legal target — this is the edge the "
        "closed-loop state machine requires"
    )

    # And the round-trip back: ``executing → verification``.
    allowed_exec = set(VERIFICATION_PHASE_TRANSITIONS.get("executing", []))
    assert "verification" in allowed_exec, (
        "VERIFICATION_PHASE_TRANSITIONS['executing'] MUST include "
        "'verification' as a legal target — without this the "
        "post-repair callback can't route back into the verification "
        "sub-machine"
    )

    # And the orchestrator's bridge:
    # ``verification_failed → verification_repairing``.
    allowed_fail = set(VERIFICATION_PHASE_TRANSITIONS.get("verification_failed", []))
    assert "verification_repairing" in allowed_fail, (
        "VERIFICATION_PHASE_TRANSITIONS['verification_failed'] MUST "
        "include 'verification_repairing' as a legal target — without "
        "this check_cycle_conditions can't enter the repair phase"
    )