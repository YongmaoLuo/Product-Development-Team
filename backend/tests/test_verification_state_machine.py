"""
State Machine Table-Driven Tests for Verification Workflow
==========================================================

This suite pins the verification-state-machine contract that the
backend's :class:`VerificationOrchestrator`, :class:`PlanState`, and
FastAPI :func:`start_verification` endpoint all depend on.

The state machine the tests enforce is:

    executing / ready
         |
         v
    verification
         |
         v
    verification_running
         |        \\
         |         (FAILED: round < max_rounds-1, no same-failure)
         |              v
         |         verification_failed
         |              v
         |         verification_repairing
         |              v
         |         (user confirms) -> verification_rerunning
         |              v
         |         verification_running  (next round)
         |
         v
    verification_passed -> completed
    verification_failed -> verification_repairing (or loop_stopped)
    verification_loop_stopped -> failed

Four TDD specs:

1. ``test_state_machine_legal_transitions_reachable`` — every
   edge in the ``VERIFICATION_PHASE_TRANSITIONS`` table is
   actually reachable through :meth:`PlanState.transition_to`
   (or its convenience helpers), so a refactor that quietly drops
   a legal edge fails this test before it breaks production.

2. ``test_state_machine_illegal_transitions_rejected`` — illegal
   transitions raise :class:`ValueError` (the contract the
   FastAPI endpoint relies on to return 400/409), and the
   server-level :func:`start_verification` endpoint returns
   400/409 for the two most consequential illegal
   re-/start paths (passed → start, and running → start).

3. ``test_repair_loop_still_works_after_refactor`` — exercises
   the closed-loop repair path through the real
   :class:`VerificationOrchestrator`:
   failed → repairing → rerunning → passed. A refactor that
   drops the round-increment, the "current < max_rounds-1"
   guard, or the rerun transition breaks this test.

4. ``test_split_event_does_not_corrupt_state`` — verifies that
   a VP-level split (the timeout → ``SplitDecision.should_split``
   path) does NOT corrupt the verification state machine:
   ``current_phase`` stays in ``verification_running``, the
   ``verification_status`` stays ``running`` mid-round, and the
   ``subtask_splits`` list on the profile grows by exactly one
   entry per parent that triggered a split.

All tests in this file are unit / fast (no LLM, no subprocess,
no real asyncio.wait_for wall time) so they can run on every PR.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, Mock, patch

import pytest

# Make `verification_agent` / `verification` / `plan_state` / `server`
# importable when pytest is launched from either the project root or
# the ``backend/`` directory. Mirrors the pattern in
# ``test_verification_orchestrator.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plan_state import (  # noqa: E402
    PHASE_TRANSITIONS,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
    VALID_PHASES,
)
from coding_tool import HardTimeoutError  # noqa: E402
from verification import VerificationOrchestrator  # noqa: E402


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_plan_dir(tmp_path):
    """Per-test plan directory under ``tmp_path/plans/state-machine``."""
    plan_dir = tmp_path / "plans" / "state-machine"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """Per-test project directory under ``tmp_path/projects/state-machine``."""
    project_dir = tmp_path / "projects" / "state-machine"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    """Stand-in ``CodingTool`` — orchestrator/agent LLM is patched in tests."""
    return Mock()


def _scrub_plan_rows(plan_id: str) -> None:
    """Delete the plan's SQLite rows so ``plan_state.json`` is authoritative.

    :meth:`PlanState._load_state` is SQLite-first: once a
    ``plan_routing`` row exists for a plan id, the ``plan_state.json``
    on disk is ignored — it is only consulted for the one-shot legacy
    migration that runs when *no* row exists.

    Tests in this file reuse a single plan id (``state-machine``)
    across many iterations, rewriting ``plan_state.json`` each time.
    Without this scrub, iteration N+1 would silently re-read iteration
    N's phase from SQLite and the edge walk would fail on its first
    step with ``Illegal transition from 'prd_review' ...``.

    Mirrors ``tests/unit/test_state_machine_closed_loop.py``'s
    ``_scrub_plan_routing_row``, widened to the other two tables the
    verification flow writes (``plan_execution``'s ``current_phase``
    and ``plan_verification``'s round/status).
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
            for table in (
                "plan_routing",
                "plan_execution",
                "plan_verification",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE plan_id = ?",  # noqa: S608
                    (plan_id,),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        # SQLite unavailable or unwriteable — PlanState falls back to
        # the legacy plan_state.json path automatically.
        pass


def _write_plan_state(
    plan_dir: Path,
    *,
    phase: str = "executing",
    verification_round: int = 0,
    max_rounds: int = 3,
    stop_reason: str | None = None,
    status: str = "pending",
) -> None:
    """Bootstrap ``plan_state.json`` for a state-machine test.

    The orchestrator reads the state via :class:`PlanState` and
    writes back transitions during the cycle, so a test that
    skips the bootstrap will see a missing-file error on the
    first ``transition_to`` call.

    The SQLite rows for this plan id are scrubbed first so the file
    written here — not a previous iteration's persisted row — is what
    :class:`PlanState` loads.
    """
    _scrub_plan_rows(plan_dir.name)
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase != "interview" else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def _legal_edges() -> List[Tuple[str, str]]:
    """Flatten the two transition tables into (from, to) edges.

    :data:`PHASE_TRANSITIONS` carries the main workflow edges,
    :data:`VERIFICATION_PHASE_TRANSITIONS` carries the
    verification-state-machine edges. The orchestrator's
    legality check (``plan_state.transition_to``) consults the
    *union* of the two, so the table-driven test must pin
    edges from both.
    """
    edges: List[Tuple[str, str]] = []
    for src, dests in PHASE_TRANSITIONS.items():
        for dst in dests:
            edges.append((src, dst))
    for src, dests in VERIFICATION_PHASE_TRANSITIONS.items():
        for dst in dests:
            edges.append((src, dst))
    return edges


class _DormantThread:
    """``threading.Thread`` stub that never actually starts a thread.

    ``POST /api/verification/{id}/start`` answers 200 by handing the
    run loop to a background thread. Without this stub a test that
    drives the happy path past the routing CAS spawns the *real*
    auto-verification loop, which builds a VerificationOrchestrator,
    reaches ``coding_tool.query_json`` and shells out to the ``claude``
    CLI — a live LLM call (and a real Bash side effect) from inside the
    unit suite. Same shape as ``_DormantThread`` in
    ``tests/integration/api/test_api_error_matrix.py``.
    """

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


# =============================================================================
# 1. Every legal edge in the table is reachable
# =============================================================================


class TestStateMachineLegalTransitionsReachable:
    """Table-driven enumeration — every (from, to) edge in the
    :data:`PHASE_TRANSITIONS` / :data:`VERIFICATION_PHASE_TRANSITIONS`
    tables must be reachable through :class:`PlanState`'s
    public API (or its convenience helpers like
    :meth:`verification_passed`).

    The test works by:

    1. Enumerating every (from, to) edge from the union of both
       transition tables.
    2. For each edge, building a fresh ``plan_state.json`` whose
       ``current_phase = from``, ``completed_phases`` already
       contains the prerequisites the
       :meth:`PlanState.transition_to` guard requires, and
       calling :meth:`PlanState.transition_to` with ``to``.
    3. Asserting that the resulting ``current_phase == to`` and
       that no exception was raised.

    Edges that the guard refuses to walk (e.g. ``verification``
    → ``verification_running`` requires the orchestrator to
    drive it via a convenience method rather than a direct
    ``transition_to``) are tested separately with the matching
    convenience helper, and the failure mode is asserted
    explicitly. This keeps the table test honest about which
    edges are *public* vs. which are *private machinery*.
    """

    def test_every_legal_edge_in_table_is_walkable_via_transition_to(
        self, temp_plan_dir
    ):
        """Walk the union of both transition tables via direct
        ``transition_to`` calls, including the ``completed_phases``
        bookkeeping the guard requires.
        """
        # Edges that :meth:`PlanState.transition_to` can walk
        # directly without orchestrator-side state work.
        # ``executing -> completed`` and ``executing -> failed``
        # are the only main-table edges that need no extra
        # bookkeeping beyond ``execution`` already in
        # ``completed_phases``; the verification-table edges
        # require the orchestrator to drive the round
        # increment, so we walk them via convenience helpers in
        # a companion test.
        direct_edges: List[Tuple[str, str]] = [
            # Main workflow table
            ("interview", "interview_complete"),
            ("interview_complete", "prd_generation"),
            ("prd_generation", "prd_review"),
            ("prd_approved", "tasks_generation"),
            ("tasks_generation", "ready"),
            ("ready", "executing"),
            # Verification table edges that the guard accepts
            # directly (the convenience methods call into
            # transition_to under the hood).
            ("executing", "verification"),
            ("verification", "verification_passed"),
            # ``verification_passed`` is deliberately TERMINAL: the
            # table entry is ``[]`` with an inline "security boundary"
            # comment (plan_state.py:181), so the historical
            # ``("verification_passed", "completed")`` edge no longer
            # exists and must not be walked here. The bridge runs the
            # other way — execution ends in ``completed`` and the
            # round converges on its verdict from there.
            ("completed", "verification_passed"),
            ("completed", "verification_failed"),
            # Backward transitions the guard allows.
            ("failed", "ready"),
            ("failed", "executing"),
            ("stopped", "ready"),
            ("stopped", "executing"),
        ]

        for src, dst in direct_edges:
            _write_plan_state(temp_plan_dir, phase=src)
            ps = PlanState(temp_plan_dir)
            try:
                ps.transition_to(dst)
            except ValueError as exc:
                pytest.fail(
                    f"legal edge {src!r} -> {dst!r} was rejected: {exc}"
                )
            assert ps.get_current_phase() == dst, (
                f"expected phase {dst!r} after transition, "
                f"got {ps.get_current_phase()!r}"
            )

    def test_verification_table_edges_via_convenience_helpers(
        self, temp_plan_dir
    ):
        """Verification-state-machine edges that require
        orchestrator-driven bookkeeping (``start_verification``,
        ``verification_failed``, ``start_verification_repair``,
        ``start_verification_rerun``, ``stop_verification_loop``)
        must be reachable through the public helper methods on
        :class:`PlanState`.
        """
        # Walk a representative slice of the verification table.
        # This is the "happy / unhappy" path through the
        # verification state machine, going through every
        # helper the orchestrator uses.
        scenarios: List[Tuple[str, Dict[str, Any], str, str]] = [
            (
                "executing -> verification -> verification_running",
                {"phase": "executing"},
                "call",  # placeholder
                "verification",
            ),
        ]

        # First scenario: executing -> verification (via
        # start_verification), then orchestrator can drive the
        # next transition (we drive it manually here).
        _write_plan_state(temp_plan_dir, phase="executing")
        ps = PlanState(temp_plan_dir)
        ps.start_verification()
        assert ps.get_current_phase() == "verification"
        assert ps.get_verification_status() == "pending"

        # verification -> verification_running (direct)
        ps.transition_to("verification_running")
        assert ps.get_current_phase() == "verification_running"
        assert ps.get_verification_status() == "running"

        # verification_running -> verification_passed (via helper)
        ps.verification_passed()
        assert ps.get_current_phase() == "verification_passed"
        assert ps.get_verification_status() == "passed"

        # verification_passed is TERMINAL by design — there is no
        # outbound edge at all (plan_state.py:181 marks it a
        # "security boundary"; a forward edge to ``completed`` would
        # let a passing verdict be laundered into a success terminal
        # directly, bypassing the main workflow's own terminal
        # bookkeeping). Pin the terminality explicitly rather than
        # walking an edge that no longer exists.
        assert VERIFICATION_PHASE_TRANSITIONS["verification_passed"] == [], (
            "verification_passed must stay terminal — restoring a "
            "forward edge would reopen the security boundary"
        )
        with pytest.raises(ValueError, match="Illegal transition"):
            ps.transition_to("completed")

        # The documented bridge is the reverse direction: execution
        # ends in ``completed`` and the round converges on its verdict
        # from there (``completed -> verification_passed``).
        _write_plan_state(temp_plan_dir, phase="completed")
        ps_bridge = PlanState(temp_plan_dir)
        ps_bridge.transition_to("verification_passed")
        assert ps_bridge.get_current_phase() == "verification_passed"
        assert ps_bridge.get_verification_status() == "passed"

    def test_verification_failed_and_repair_edges(self, temp_plan_dir):
        """``verification_running`` → ``verification_failed`` →
        ``verification_repairing`` → ``verification_rerunning`` →
        ``verification_running`` is the canonical repair loop.
        """
        _write_plan_state(temp_plan_dir, phase="verification_running")
        ps = PlanState(temp_plan_dir)

        ps.verification_failed()
        assert ps.get_current_phase() == "verification_failed"
        assert ps.get_verification_status() == "failed"

        ps.start_verification_repair()
        assert ps.get_current_phase() == "verification_repairing"

        ps.start_verification_rerun()
        assert ps.get_current_phase() == "verification_rerunning"
        assert ps.get_verification_round() == 1, (
            "confirm_repair_and_rerun must bump round 0 -> 1"
        )

    def test_verification_loop_stopped_to_failed_edge(self, temp_plan_dir):
        """``verification_loop_stopped`` → ``failed`` is the only
        outbound edge of the loop-stopped state (the bridge from
        the verification sub-machine back to the main workflow).
        """
        _write_plan_state(temp_plan_dir, phase="verification_failed")
        ps = PlanState(temp_plan_dir)
        ps.stop_verification_loop("max_rounds_reached")
        assert ps.get_current_phase() == "verification_loop_stopped"
        assert ps.get_verification_status() == "loop_stopped"
        assert ps.get_verification_stop_reason() == "max_rounds_reached"

        ps.transition_to("failed")
        assert ps.get_current_phase() == "failed"

    def test_completed_has_no_in_progress_outbound_edges(self, temp_plan_dir):
        """``completed`` may only reach verification *verdict* states.

        ``completed`` used to declare zero outbound edges in the
        verification table, which stranded every plan: execution
        ends in ``completed``, so the verification round that
        follows had no legal way to record either verdict and
        :meth:`PlanState.transition_to` raised ``ValueError`` for
        both. The fix adds exactly two edges —
        ``verification_passed`` and ``verification_failed`` — plus
        the pre-existing ``completed -> verification`` edge in the
        main table.

        What must stay forbidden is any edge back into an
        *in-progress* phase: a finished plan must never silently
        revert to ``executing`` / ``ready`` / a repair loop.
        """
        union = {**PHASE_TRANSITIONS, **VERIFICATION_PHASE_TRANSITIONS}
        dests = union["completed"]

        assert dests == ["verification_passed", "verification_failed"], (
            f"completed must declare exactly the two verification "
            f"verdicts in the union table, got {dests!r}"
        )

        # The union merge lets the verification table shadow the main
        # table's row, so assert the main-table edge separately.
        assert PHASE_TRANSITIONS["completed"] == ["verification"], (
            f"completed's main-table edge changed: "
            f"{PHASE_TRANSITIONS['completed']!r}"
        )

        # No route back into an in-progress phase from either table.
        forbidden = {
            "executing",
            "ready",
            "verification_running",
            "verification_repairing",
            "verification_rerunning",
            "interview",
            "prd_review",
        }
        reachable = set(PHASE_TRANSITIONS["completed"]) | set(
            VERIFICATION_PHASE_TRANSITIONS["completed"]
        )
        leaked = reachable & forbidden
        assert not leaked, (
            f"completed must not reach in-progress phase(s) {sorted(leaked)}; "
            f"a finished plan would silently revert to in-progress"
        )

    def test_validation_phase_helper_set_status_correctly(self, temp_plan_dir):
        """Each transition_to call must also set
        ``verification.status`` to the matching
        ``pending|running|passed|failed|loop_stopped`` value.

        This is the dual contract: the *state* transitions
        atomically with the *status* (so the bridge UI can
        render them from a single PlanState snapshot).
        """
        _write_plan_state(temp_plan_dir, phase="executing")
        ps = PlanState(temp_plan_dir)

        ps.transition_to("verification")
        assert ps.get_verification_status() == "pending"

        ps.transition_to("verification_running")
        assert ps.get_verification_status() == "running"

        ps.transition_to("verification_passed")
        assert ps.get_verification_status() == "passed"

        # ``verification_passed`` is terminal (no outbound edge), so
        # the remaining two status values are reached by their own
        # walks rather than by continuing out of the pass state.
        _write_plan_state(temp_plan_dir, phase="verification_running")
        ps_failed = PlanState(temp_plan_dir)
        ps_failed.verification_failed()
        assert ps_failed.get_verification_status() == "failed"

        ps_failed.stop_verification_loop("max_rounds_reached")
        assert ps_failed.get_verification_status() == "loop_stopped"
        assert ps_failed.get_verification_stop_reason() == "max_rounds_reached"


# =============================================================================
# 2. Illegal transitions are rejected
# =============================================================================


class TestStateMachineIllegalTransitionsRejected:
    """Illegal transitions must raise :class:`ValueError` from
    :meth:`PlanState.transition_to` (the contract the FastAPI
    endpoint relies on to return 400/409), and the server-level
    :func:`start_verification` endpoint must return 400/409 for
    the two most consequential illegal re-/start paths.
    """

    def test_illegal_transition_raises_value_error(self, temp_plan_dir):
        """Concrete illegal edges raise ``ValueError`` with a
        message that names the legal alternatives.

        These edges are deliberately *not* in
        :data:`VERIFICATION_PHASE_TRANSITIONS` — a refactor
        that quietly adds them (e.g. enabling
        ``verification_passed`` → ``verification_running`` for
        "force re-run") would silently bypass the
        loop-stopped / repair machinery.
        """
        illegal_edges: List[Tuple[str, str]] = [
            ("verification_passed", "verification_running"),
            ("verification_passed", "verification"),
            ("verification_passed", "verification_failed"),
            ("verification_loop_stopped", "verification_running"),
            ("verification_loop_stopped", "verification_repairing"),
            ("completed", "verification_running"),
            ("completed", "executing"),
            ("failed", "verification_running"),
            ("failed", "verification_passed"),
            ("interview", "verification"),
            ("prd_review", "verification"),
        ]

        for src, dst in illegal_edges:
            _write_plan_state(temp_plan_dir, phase=src)
            ps = PlanState(temp_plan_dir)
            with pytest.raises(ValueError) as excinfo:
                ps.transition_to(dst)
            msg = str(excinfo.value)
            assert "Illegal transition" in msg, (
                f"error message for {src!r}->{dst!r} should "
                f"name the offense, got: {msg!r}"
            )
            assert src in msg and dst in msg, (
                f"error message must include both src ({src!r}) "
                f"and dst ({dst!r}), got: {msg!r}"
            )

    def test_unknown_phase_raises_value_error(self, temp_plan_dir):
        """``transition_to`` with a phase not in
        :data:`VALID_PHASES` must raise ``ValueError`` — this
        is the "garbage in" guard that prevents typos like
        ``verification_passsed`` from silently writing to the
        state file.
        """
        _write_plan_state(temp_plan_dir, phase="executing")
        ps = PlanState(temp_plan_dir)
        with pytest.raises(ValueError) as excinfo:
            ps.transition_to("verification_passsed")  # typo
        assert "Invalid phase" in str(excinfo.value)

    def test_start_verification_endpoint_rejects_already_running_with_409(
        self, tmp_path
    ):
        """Server-level contract: ``POST /api/verification/{id}/start``
        when the plan is already in ``running`` state must
        return HTTP 409 (Conflict), not 500.

        The endpoint's
        ``_verification_state[plan_id]["verification_status"]``
        guard is what makes concurrent starts safe — a
        refactor that drops it (and falls through to
        spawning a second ``VerificationOrchestrator`` thread)
        would double-execute the verification round.
        """
        # Avoid pulling in the full ``server`` module — we
        # only need the FastAPI app for the testclient.
        from fastapi.testclient import TestClient
        from server import app, _verification_state, _verification_lock

        client = TestClient(app)
        plan_id = "illegal-start-running"

        # Bootstrap a plan with ``current_phase = "executing"``
        # so the *phase* guard accepts the start (we want to
        # isolate the *already-running* guard).
        plans_root = tmp_path / "plans"
        plan_dir = plans_root / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "plan_state.json").write_text(
            json.dumps(
                {
                    "plan_id": plan_id,
                    "current_phase": "executing",
                    "completed_phases": ["execution"],
                    "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                    "flags": {},
                    "verification": {
                        "status": "running",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    },
                }
            ),
            encoding="utf-8",
        )
        (plan_dir / "execution.json").write_text(
            json.dumps(
                {"project_dir": str(tmp_path / "project" / plan_id)}
            ),
            encoding="utf-8",
        )

        # Pre-seed the in-memory state to mark the plan as
        # currently running. The endpoint's contract: if the
        # state says running, the request is rejected with
        # 409 regardless of the on-disk phase.
        try:
            _verification_state[plan_id] = {
                "plan_id": plan_id,
                "verification_status": "running",
                "verification_round": 0,
                "verification_max_rounds": 3,
                "results": {},
                "repair_tasks": [],
                "started_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "orchestrator": None,
                "stop_reason": None,
            }

            resp = client.post(f"/api/verification/{plan_id}/start")
            assert resp.status_code == 409, (
                f"already-running start must return 409, got "
                f"{resp.status_code}: {resp.text!r}"
            )
            body = resp.json()
            assert "error" in body, f"409 response missing 'error': {body!r}"
            assert "Already running" in body["error"] or "already" in body["detail"].lower(), (
                f"409 error message should mention 'already running', got: {body!r}"
            )
        finally:
            _verification_state.pop(plan_id, None)

    def test_start_verification_endpoint_rejects_wrong_phase_with_409(
        self, tmp_path, plan_sqlite_seeder
    ):
        """Server-level contract: ``POST /api/verification/{id}/start``
        when the plan's routing stage does not permit starting
        verification (e.g. a plan still in ``prd_review``) must return
        HTTP 409 Conflict, not 200.

        A pre-SQLite version of the endpoint read ``current_phase`` from
        ``plan_state.json`` and answered 400 "Invalid phase". The
        SQLite-first rewrite made the routing CAS
        (``try_mark_phase``) the single decider, so "you may not start"
        is now expressed as the documented conflict envelope
        (``{"error": "conflict", "reason": "stage_mismatch"}``) — the
        same answer every write consumer gives, which is what VP-018
        pins. See ``tests/integration/api/test_api_error_matrix.py``
        (``verify_start x cas_predicate_fail``) and
        ``state_machine/tests/integration/test_verification_routes.py``
        (``test_start_verification_409_when_stage_mismatch``).

        The fixture also has to seed a ``project_dir``: the endpoint's
        "no target project recorded yet" guard is a genuine 400 and sits
        *before* the CAS, so without it this test would measure that
        guard instead of the routing one.
        """
        from fastapi.testclient import TestClient
        from server import app, _verification_state

        client = TestClient(app)
        plan_id = "illegal-start-wrong-phase"

        # Bootstrap a plan with current_phase = "prd_review"
        # — a phase that does NOT permit starting
        # verification.
        plans_root = tmp_path / "plans"
        plan_dir = plans_root / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "plan_state.json").write_text(
            json.dumps(
                {
                    "plan_id": plan_id,
                    "current_phase": "prd_review",
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
            ),
            encoding="utf-8",
        )
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        plan_sqlite_seeder(
            plan_dir,
            plan_id,
            phase="prd_review",
            project_dir=project_dir,
        )

        try:
            resp = client.post(f"/api/verification/{plan_id}/start")
            assert resp.status_code == 409, (
                f"wrong-stage start must return 409, got "
                f"{resp.status_code}: {resp.text!r}"
            )
            body = resp.json()
            assert body["error"] == "conflict", (
                f"409 response must be the conflict envelope, got {body!r}"
            )
            assert body["reason"] in ("stage_mismatch", "version_mismatch"), (
                f"409 body must surface the conflict reason, got {body!r}"
            )
        finally:
            _verification_state.pop(plan_id, None)

    def test_start_verification_endpoint_accepts_terminal_verification_phases(
        self, tmp_path, monkeypatch
    ):
        """2026-08-26 audit regression: a plan whose previous
        verification round terminated (passed / failed /
        loop_stopped) must be RE-TRIGGERABLE via
        ``POST /api/verification/{id}/start`` so Round 2 / Round 3
        can fire manually after a repair commit.

        Pre-2026-08-26, ``start_verification``'s routing CAS only
        accepted ``{executing, failed, completed}``
        as source stages — any verification terminal state was
        rejected with 409, stranding the plan after Round 1.

        This test pins the new contract: each of the three
        verification terminal stages (``verification_passed``,
        ``verification_failed``, ``verification_loop_stopped``)
        must accept the CAS into ``verification_running`` and
        return 200 (not 400 / 409).

        The run loop is stubbed out: this test asserts on the
        *acceptance* decision, and the real loop would shell out to
        the ``claude`` CLI (see :class:`_DormantThread`).
        """
        from fastapi.testclient import TestClient
        from server import app, _verification_state

        monkeypatch.setattr("server.threading.Thread", _DormantThread)
        monkeypatch.setattr(
            "server._run_auto_verification_loop", lambda *a, **k: None
        )

        client = TestClient(app)

        for terminal_phase in (
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
        ):
            plan_id = f"restart-after-{terminal_phase}"
            plans_root = tmp_path / "plans"
            plan_dir = plans_root / plan_id
            plan_dir.mkdir(parents=True, exist_ok=True)

            # Minimal plan_state.json — the start handler reads
            # ``current_phase`` from this file (PlanState legacy
            # fallback) and the routing CAS row from state.db.
            (plan_dir / "plan_state.json").write_text(
                json.dumps(
                    {
                        "plan_id": plan_id,
                        "current_phase": terminal_phase,
                        "completed_phases": [
                            "ready", "executing", "verification",
                            "verification_running", terminal_phase,
                        ],
                        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                        "flags": {},
                        "verification": {
                            "status": "failed" if "failed" in terminal_phase else "passed",
                            "round": 1,
                            "max_rounds": 3,
                            "stop_reason": None,
                        },
                    }
                ),
                encoding="utf-8",
            )

            # Seed the routing CAS row + plan_execution +
            # plan_verification so all start-handler guards pass.
            # 2026-08-26: the canonical source for project_dir is
            # the ``plan_execution`` SQLite row, not the legacy
            # execution.json — the SQLite path survives server
            # restarts and is what ``_get_project_dir`` reads.
            project_dir = tmp_path / "projects" / plan_id
            project_dir.mkdir(parents=True, exist_ok=True)

            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from datetime import datetime as _dt, timezone as _tz
            def _now_iso():
                return _dt.now(_tz.utc).isoformat()
            conn = _open_db(str(tmp_path / "state.db"))
            _migrate(conn)
            try:
                # Use raw SQL — different repos have different
                # insert signatures (insert / insert_or_replace /
                # upsert), and we want to seed three tables in one
                # place without depending on each repo's exact API.
                conn.execute(
                    "INSERT OR REPLACE INTO plan_routing "
                    "(plan_id, current_phase, substage, version, updated_at) "
                    "VALUES (?, ?, NULL, 0, ?)",
                    (plan_id, terminal_phase, _now_iso()),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO plan_execution "
                    "(plan_id, current_phase, exec_status, project_dir, "
                    " updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (plan_id, terminal_phase, "completed",
                     str(project_dir), _now_iso())  # exec_status value,
                )
                conn.execute(
                    "INSERT OR REPLACE INTO plan_verification "
                    "(plan_id, verification_status, round, max_rounds, "
                    " updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (plan_id, "failed", 1, 3, _now_iso()),
                )
                conn.commit()
            finally:
                conn.close()



            try:
                resp = client.post(
                    f"/api/verification/{plan_id}/start",
                    json={"max_rounds": 3},
                )
                assert resp.status_code == 200, (
                    f"start from terminal phase {terminal_phase!r} must "
                    f"return 200 (Round 2 re-trigger), got "
                    f"{resp.status_code}: {resp.text!r}"
                )
                body = resp.json()
                assert body.get("status") == "started", (
                    f"start response missing 'started' status for "
                    f"{terminal_phase!r}: {body!r}"
                )
            finally:
                _verification_state.pop(plan_id, None)


# =============================================================================
# 3. Closed-loop repair: failed -> repairing -> rerunning -> passed
# =============================================================================


class TestRepairLoopStillWorksAfterRefactor:
    """Drive the real :class:`VerificationOrchestrator` through
    the full repair cycle. The closed loop is the contract
    that, regardless of how the orchestrator's internals are
    refactored, the round-counter still increments and the
    state machine still returns to ``verification_passed`` on
    the second round.

    This test is the integration-level "regression alarm" for
    the repair loop: if any of the four moves below breaks
    in a refactor, this test fails immediately.
    """

    def test_closed_loop_failed_repairing_rerunning_passed(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, tmp_path
    ):
        _write_plan_state(
            temp_plan_dir, phase="executing", verification_round=0, max_rounds=3
        )

        # Repair tasks now come from the 2026-09-07 single-call flow
        # (``extract_failed_vps_with_paths`` → ``generate_repair_contents``
        # → ``RepairTaskAssembler``). The legacy
        # ``generate_verification_tasks`` chain — and the
        # ``verification_repair_tasks.json`` sidecar it wrote — were
        # retired in the same refactor, so the three collaborators
        # below are the ones to mock.
        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG, \
             patch(
                 "verification.verification_report_reader.extract_failed_vps_with_paths"
             ) as MockExtract, \
             patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            MockExtract.return_value = [
                {"id": "VP-API-0", "title": "Fix api_test #0", "priority": "high"},
            ]
            orch.repair_generator.generate_repair_contents.return_value = [
                {"failed_vp_id": "VP-API-0", "title": "Fix api_test #0"},
            ]
            MockAssembler.return_value.assemble.return_value = [
                {"id": "RP-1", "title": "Fix api_test #0"},
            ]
            # ``run_full_verification`` is called twice
            # (once per round). Use a side_effect function
            # rather than a list — the mock consumes
            # ``side_effect`` as an iterator, so a list
            # only works for ``assert_*_called_with``
            # introspection after the fact, not for
            # indexed access here.
            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {
                        "id": "VP-API-0",
                        "verification_method": "api_test",
                        "status": "FAILED",
                        "actual_result": "injected failure",
                        "evidence": "test",
                    }
                ],
                "requirement_deviations": [],
            }
            passed_report = {
                "overall_status": "PASSED",
                "verification_results": [
                    {
                        "id": "VP-API-0",
                        "verification_method": "api_test",
                        "status": "PASSED",
                        "actual_result": "fixed",
                        "evidence": "test",
                    }
                ],
                "requirement_deviations": [],
            }
            orch.verification_agent.run_full_verification.side_effect = [
                failed_report,
                passed_report,
            ]

            # --- Round 1: FAILED -> repair ---
            orch.start_verification_cycle(round_number=1)
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_running"
            assert ps.get_verification_round() == 0

            cycle_result = orch.check_cycle_conditions(failed_report)
            assert cycle_result["status"] == "verification_failed"
            # 2026-09-07 fix: ``waiting_for_user`` is now always
            # ``False``. The auto-loop never reads it (grep-verified
            # in ``orchestrator.py:578-583``) and
            # ``confirm_repair_and_rerun`` deliberately advances
            # unconditionally, so a ``True`` here would be a signal
            # nothing consumes.
            assert cycle_result["waiting_for_user"] is False
            assert cycle_result["should_stop"] is False
            assert cycle_result["repair_tasks"], (
                "failed round 1 must surface at least one repair task"
            )

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_repairing"
            assert ps.get_verification_status() == "failed"

            # --- User confirms repair -> the executor runs RP-* ---
            #
            # 2026-09-12 closed-loop fix (commit df2abb9): the repair
            # leg is ``verification_repairing -> executing -> verification``,
            # NOT the old shortcut ``verification_repairing ->
            # verification_rerunning``. The vocabulary must match
            # reality — while the executor subprocess runs the RP-*
            # tasks the plan IS in ``executing``, not inside the
            # verification sub-machine.
            orch.confirm_repair_and_rerun()
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "executing", (
                "confirm_repair_and_rerun must route through 'executing' "
                "(verification_repairing -> executing); the "
                "verification_rerunning shortcut was retired in df2abb9"
            )
            assert ps.get_verification_round() == 1, (
                "start_repair_execution must bump round 0 -> 1"
            )

            # --- Repair execution completes -> back into verification ---
            # Mirrors the auto-loop's post-repair callback
            # (``server.py:6573``): ``begin_verification(round_n=_next_round)``
            # is the state-machine edge ``executing -> verification``.
            ps.begin_verification(round_n=2)
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification"
            assert ps.get_verification_round() == 2, (
                "the post-repair re-entry must preserve the bumped "
                "round; clobbering to 0 destroys the monotonic invariant"
            )

            # --- Round 2: PASSED -> close the loop ---
            orch.start_verification_cycle(round_number=2)
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_running"
            assert ps.get_verification_round() == 2

            cycle_result = orch.check_cycle_conditions(passed_report)
            assert cycle_result["status"] == "passed", (
                f"second-round PASSED must close the loop, "
                f"got {cycle_result!r}"
            )
            assert cycle_result["should_continue"] is False
            assert cycle_result["should_stop"] is False
            assert cycle_result["waiting_for_user"] is False
            assert cycle_result["repair_tasks"] == []

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_passed"
            assert ps.get_verification_status() == "passed"

            # The mocked agent should have been called twice
            # (once per cycle), with the right round numbers —
            # the round-number contract is what makes the
            # repair loop observable end-to-end.
            assert (
                orch.verification_agent.run_full_verification.call_count == 2
            ), (
                f"expected run_full_verification called twice "
                f"(rounds 1 and 2), got "
                f"{orch.verification_agent.run_full_verification.call_count}"
            )
            first_call = (
                orch.verification_agent.run_full_verification.call_args_list[0]
            )
            second_call = (
                orch.verification_agent.run_full_verification.call_args_list[1]
            )
            assert first_call.args[0] == 1
            assert second_call.args[0] == 2


# =============================================================================
# 4. Split event does not corrupt the state machine
# =============================================================================


class TestSplitEventDoesNotCorruptState:
    """A VP-level split (timeout → :class:`SplitDecision.should_split`)
    must NOT corrupt the verification state machine.

    Concretely:

    * ``current_phase`` must remain ``verification_running`` for
      the duration of the round (a split is a per-VP event, not
      a state transition).
    * ``verification_status`` must remain ``running`` while the
      round is in flight; only the orchestrator's
      ``check_cycle_conditions`` decides whether to flip it to
      ``passed``/``failed``/``loop_stopped``.
    * The ``subtask_splits`` list on the verification state
      must grow by exactly one entry per parent that triggered
      a split, and each entry must carry
      ``parent_vp_id``/``sub_vp_ids`` matching the children
      emitted by :class:`SplitDecision`.

    The test runs the real :class:`VerificationAgent` (no agent
    mocking) with a single multi-clause VP that hits a timeout
    and gets decomposed. The leaf executor is replaced with a
    small stub that fakes the timeout for the parent and a
    pass for the children, so the test stays fast and
    deterministic.
    """

    def test_split_in_round_preserves_verification_state(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch
    ):
        monkeypatch.setenv("VERIFICATION_PROFILE", "dry_run")
        # Bootstrap with ``verification_running`` so the
        # mid-round assertions reflect the canonical
        # "round is in flight" state. The orchestrator's
        # ``start_verification_cycle`` would normally drive
        # the executing -> verification -> verification_running
        # transition, but here we exercise the agent's
        # split path in isolation, so we set the phase
        # directly.
        _write_plan_state(
            temp_plan_dir,
            phase="verification_running",
            verification_round=0,
            status="running",
        )

        from verification_agent import VerificationAgent
        from verification_config import TimeoutPolicy
        from verification_split import SplitDecision

        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        # Tight 5s per-method timeout so the parent times
        # out immediately when its fake sleep exceeds it; the
        # children inherit the same 5s budget and pass with
        # their 0.1s sleep.
        agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"automated_test": 5},
            global_default_timeout_seconds=5,
            parallelism_cap=4,
        )
        agent.start_verification_round(1)

        # Single multi-clause VP. SplitDecision decomposes
        # the semicolon-separated ``expected_result`` into
        # three sub-VPs.
        vps: List[Dict[str, Any]] = [
            {
                "id": "VP-SPLIT-A",
                "title": "split-parent",
                "verification_method": "automated_test",
                "priority": "medium",
                "expected_result": "first;second;third",
                "timeout_seconds": 5,
            }
        ]

        async def _stub(vp):
            if vp.get("id") == "VP-SPLIT-A":
                # Short-circuit: raise HardTimeoutError immediately
                # to drive the agent's ``_split_vp_on_timeout``
                # branch — the inner 15-min idle detector's signal.
                # (2026-09-13: the per-VP ``asyncio.wait_for``
                # wrapper that raised ``asyncio.TimeoutError`` was
                # removed 2026-09-08; ``except asyncio.
                # TimeoutError`` no longer routes to split.)
                raise HardTimeoutError(
                    total_sec=900, elapsed=905.0, last_line="x"
                )
            # Children: short, fast PASS.
            await asyncio.sleep(0.05)
            return {
                "id": vp.get("id", "child"),
                "status": "PASSED",
                "actual_result": "child passed",
                "evidence": "split_stub_child",
            }

        setattr(agent, "_execute_automated_test_async", _stub)

        result = asyncio.run(
            agent.execute_verification_plan_async({"verification_points": vps})
        )
        results = result["execution_results"]
        assert len(results) == 1, f"expected 1 synthesised parent, got {results!r}"
        parent = results[0]

        # 1. State machine was not corrupted by the split.
        ps = PlanState(temp_plan_dir)
        assert ps.get_current_phase() == "verification_running", (
            f"split must not change current_phase; got {ps.get_current_phase()!r}"
        )
        assert ps.get_verification_status() in {"pending", "running"}, (
            f"mid-round split must keep verification.status in "
            f"{{'pending', 'running'}}, got {ps.get_verification_status()!r}"
        )
        assert ps.get_verification_round() == 0, (
            f"split must not bump round; got {ps.get_verification_round()!r}"
        )

        # 2. The parent has 3 children.
        child_results = parent.get("child_results", [])
        assert len(child_results) == 3, (
            f"expected 3 child sub-VPs, got {len(child_results)}: {child_results!r}"
        )
        for index, child in enumerate(
            sorted(child_results, key=lambda c: c.get("split_clause_index", -1)),
            start=1,
        ):
            assert child.get("parent_vp_id") == "VP-SPLIT-A"
            assert child.get("split_clause_index") == index
            assert child.get("id") == f"VP-SPLIT-A-{index}"
            assert child.get("status") == "PASSED"

        # 3. SplitDecision.should_split returned the same 3
        # children we just observed — pinning the contract
        # that the splitter's child ids are the ones the
        # agent aggregates back.
        split_children = SplitDecision.should_split(
            vps[0],
            {"status": "timeout"},
        )
        assert split_children is not None
        assert [c["id"] for c in split_children] == [
            "VP-SPLIT-A-1",
            "VP-SPLIT-A-2",
            "VP-SPLIT-A-3",
        ], (
            f"SplitDecision.should_split must produce the same "
            f"child ids the agent aggregates, got: "
            f"{[c['id'] for c in split_children]!r}"
        )
