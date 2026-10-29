"""Integration tests: the lifecycle facade's two new edges.

Background
----------
``PlanLifecycleService`` is the one place a plan's phase is written
(``backend/plan_lifecycle_service.py``).  It had two directions — ``stop_plan``
(stop a running plan) and ``start_plan`` (resume an interrupted one) — and the
scheduler needed two more: **pause** a plan and **requeue** it.

Both are *existing* edges of the state machine, not new ones:

* ``queued → stopped`` — take a waiting plan out of the queue and park it;
* ``stopped → queued`` — put a parked plan back into the waiting phase.

The point of these tests is that the facade **reuses** those edges instead of
inventing a vocabulary of its own: no new phase, no new edge, no change to
the CAS.  A scheduler that could pause a plan into a phase the rest of the
repository has never heard of would leave the plan stranded — the phase is
what the rest of the system reads, and a phase nothing else can leave is a
dead end.

What this file holds down
-------------------------
1. **A running plan is paused through the existing stop path** — with
   ``requeue=False``, so pausing never puts a plan back into the queue.
2. **A queued plan is parked through the existing edge** — and its queue row
   is deleted, because a paused plan that is still queued will be picked up
   again by the scheduler, which makes the pause a lie.
3. **A parked plan is requeued through the existing edge.**
4. **The phase vocabulary and the edge tables are untouched** — compared
   item by item against the snapshot below.
5. **Everything else is a conflict** — the phase does not move.

The conflict vocabulary
-----------------------
Two different things go by the name "conflict", and they are **not** the
same thing:

* **The phase does not allow the operation** — the caller asked for
  something this phase has no edge for.  That answer is **returned**, not
  raised: the caller wants "that did not happen" together with "it is
  still over there", and a 500 would be a lie.  The result says so in
  ``detail`` (``'conflict'``) and in ``ok`` (``False``), and both phase
  fields still carry the phase the plan is in.
* **The row was bumped under the writer** — that is the repository's
  ``ConflictError``, and it keeps the semantics it already has everywhere
  else in this codebase: it surfaces to the caller, and it is **not**
  retried.  Retrying is how you overwrite somebody else's write with
  yours.

The result type itself is a **frozen contract**: ``LifecycleResult`` has
exactly six fields, because the two lifecycle endpoints answer with it
verbatim and the consumer on the other side of the repository parses
those keys (``docs/development/cross-repo-contract.md`` §1.3).  ``ok`` and
``phase`` are read-only *views* over those six fields, not two more
fields — a dataclass field would become a seventh key in a response body
somebody else has to parse.

These tests run against a real SQLite file: the queue row and the plan phase
are two facts in one database, and a test that used a stand-in for either
would not be testing the thing that breaks.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# The vocabulary before the facade grew its two entries
#
# This is a snapshot, not a copy of the truth: ``plan_state`` remains the
# truth, and this file is the witness that it did not move.  Every entry is
# spelled out so that a change shows up as a *diff* in the review, not as a
# test that quietly stopped asserting anything.
# ---------------------------------------------------------------------------

_PHASES_BEFORE = [
    "interview",
    "interview_complete",
    "prd_generation",
    "prd_review",
    "prd_refining",
    "prd_approved",
    "arch_generation",
    "arch_review",
    "arch_refining",
    "arch_approved",
    "test_generation",
    "test_review",
    "test_refining",
    "test_approved",
    "preflight_review",
    "tasks_generation",
    "ready",
    "queued",
    "executing",
    "waiting_for_quota",
    "verification_plan_review",
    "verification",
    "verification_running",
    "verify_first_pass",
    "verify_recheck",
    "verification_passed",
    "verification_failed",
    "verification_repairing",
    "verification_loop_stopped",
    "completed",
    "failed",
    "stopped",
]

_MAIN_EDGES_BEFORE = {
    "interview": ["interview_complete"],
    "interview_complete": ["prd_generation"],
    "prd_generation": ["prd_review"],
    "prd_review": ["prd_refining", "prd_approved"],
    "prd_refining": ["prd_review"],
    "prd_approved": [
        "prd_refining",
        "arch_generation",
        "test_generation",
        "preflight_review",
        "tasks_generation",
        "executing",
    ],
    "arch_generation": ["arch_review"],
    "arch_review": ["arch_refining", "arch_approved"],
    "arch_refining": ["arch_review"],
    "arch_approved": [
        "arch_refining",
        "test_generation",
        "preflight_review",
        "tasks_generation",
        "executing",
    ],
    "test_generation": ["test_review", "test_approved"],
    "test_review": ["test_refining", "test_approved"],
    "test_refining": ["test_review"],
    "test_approved": [
        "test_refining",
        "preflight_review",
        "tasks_generation",
        "executing",
    ],
    "preflight_review": [
        "tasks_generation",
        "prd_review",
        "arch_review",
        "test_review",
    ],
    "tasks_generation": [
        "ready",
        "queued",
        "executing",
        "verification_plan_review",
    ],
    "verification_plan_review": ["verification", "tasks_generation", "stopped"],
    "ready": ["executing", "queued", "stopped"],
    "queued": ["ready", "executing", "stopped"],
    "executing": [
        "completed",
        "failed",
        "stopped",
        "ready",
        "queued",
        "waiting_for_quota",
    ],
    "waiting_for_quota": ["executing", "ready", "queued", "stopped"],
    "completed": ["verification"],
    "failed": ["executing", "ready", "stopped", "queued"],
    "stopped": ["executing", "ready", "queued"],
}

_VERIFICATION_EDGES_BEFORE = {
    "ready": ["verification", "verification_running"],
    "executing": [
        "verification",
        "completed",
        "failed",
        "stopped",
        "ready",
        "queued",
    ],
    "verification": [
        "verification_running",
        "verification_passed",
        "verification_failed",
        "verification_loop_stopped",
        "stopped",
    ],
    "verification_running": [
        "verify_first_pass",
        "verification_passed",
        "verification_failed",
        "stopped",
    ],
    "verify_first_pass": [
        "verify_recheck",
        "verification_passed",
        "verification_failed",
        "verification_loop_stopped",
        "stopped",
    ],
    "verify_recheck": [
        "verification_passed",
        "verification_failed",
        "verification_loop_stopped",
        "stopped",
    ],
    "verification_passed": [],
    "verification_failed": [
        "verification_repairing",
        "verification_passed",
        "verification_loop_stopped",
        "stopped",
    ],
    "verification_repairing": [
        "verification",
        "verification_passed",
        "verification_loop_stopped",
        "verification_failed",
        "executing",
        "stopped",
    ],
    "verification_loop_stopped": ["failed", "verification_passed"],
    "completed": ["verification_passed", "verification_failed"],
    "failed": ["executing", "ready", "queued"],
    "stopped": ["executing", "ready", "queued"],
}

_DEDICATED_EDGES_BEFORE = {
    "prd_review": ["tasks_generation"],
}

_TERMINAL_PHASES_BEFORE = ["completed", "failed", "stopped"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plans_root() -> Path:
    """The plans tree this case writes into.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the facade
    resolves a plan through ``server._plan_dir``, so the fixture has to
    write where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan directory whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot:
    ``PlanState`` migrates it into ``plan_routing`` on the first read, which
    is the same path a plan that was bootstrapped by the API takes.
    """
    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    return plan_dir


def _persisted_phase(plan_dir: Path) -> str:
    """The phase the plan carries **in the database** (not in the file)."""
    from plan_state import PlanState

    return PlanState(plan_dir).get_current_phase()


def _mark_execution_alive(plan_id: str) -> None:
    """Mark the execution round as in flight (``status=running``)."""
    import server

    with server._execution_state_lock:
        server._execution_state[plan_id] = {"status": "running", "ended_at": None}


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _enqueue(plan_id: str) -> None:
    """Put ``plan_id`` into the scheduler queue."""
    from plan_scheduler.queue import enqueue

    conn = _open_state_db()
    try:
        enqueue(conn, plan_id)
    finally:
        conn.close()


def _queued_plan_ids() -> List[str]:
    """The plans the scheduler queue holds right now."""
    from plan_scheduler.store import SqliteSchedulerStore

    conn = _open_state_db()
    try:
        return [
            str(row["plan_id"])
            for row in SqliteSchedulerStore().list_queue(conn)
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stop_handlers(monkeypatch):
    """Replace the two stop endpoints with recorders.

    The real stop handler kills processes and writes a checkpoint; what the
    facade's contract is about is *which* handler it dispatched and that the
    plan ends up in ``stopped``.  The recorder does both — it writes the
    checkpoint and parks the phase, exactly like the real handler's last two
    steps — so the assertions are about the facade, not about the handler.

    ``import server`` has to come before ``from routes import ...``:
    ``routes.execution`` imports ``server`` at module level and ``server``
    imports the routes at the end of the file — the other order is a
    half-initialised circular import.
    """
    import server  # noqa: F401  (the import order is load-bearing)

    from routes import execution as routes_execution
    from routes import verification as routes_verification

    calls: List[Any] = []

    def fake_stop_execution(plan_id, discard=False):
        calls.append(("execution", plan_id, discard))
        plan_dir = _plans_root() / plan_id
        _write_checkpoint(plan_dir, "execution")
        _park_as_stopped(plan_dir)

    def fake_stop_verification(plan_id):
        calls.append(("verification", plan_id))
        plan_dir = _plans_root() / plan_id
        _write_checkpoint(plan_dir, "verification")
        _park_as_stopped(plan_dir)

    monkeypatch.setattr(routes_execution, "stop_execution", fake_stop_execution)
    monkeypatch.setattr(
        routes_verification, "stop_verification", fake_stop_verification,
    )
    return calls


def _write_checkpoint(plan_dir: Path, kind: str) -> None:
    """Do what the real stop handler does: leave a resume point behind."""
    from pause_state import write_pause_checkpoint

    write_pause_checkpoint(
        plan_dir,
        kind=kind,
        project_dir=str(plan_dir),
        reason="user_requested",
    )


def _park_as_stopped(plan_dir: Path) -> None:
    """Do what the real stop handler does: park the phase in ``stopped``."""
    from plan_state import PlanState

    state = PlanState(plan_dir)
    state.reload()
    state.transition_to("stopped")


# ---------------------------------------------------------------------------
# 1. A running plan is paused through the existing stop path
# ---------------------------------------------------------------------------


def test_pause_running_uses_stop_no_requeue(stop_handlers, monkeypatch):
    """A running plan is paused with ``stop_plan(requeue=False)``.

    Two things have to be true at once: the facade goes through the
    **existing** stop path (so the round is really interrupted, the
    checkpoint is written and the phase is parked by the handler that owns
    it), and it asks that path **not** to requeue (a pause must not put the
    plan back into the queue — that would be a resume).
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "pause-running-p1"
    plan_dir = _make_plan(plan_id, "executing")
    _mark_execution_alive(plan_id)

    calls: List[Any] = []
    real_stop_plan = PlanLifecycleService.stop_plan

    def recording_stop_plan(self, pid, requeue=False):
        calls.append((pid, requeue))
        return real_stop_plan(self, pid, requeue=requeue)

    monkeypatch.setattr(PlanLifecycleService, "stop_plan", recording_stop_plan)

    result = PlanLifecycleService().pause_plan(plan_id)

    # The stop path was taken, and it was told not to requeue.
    assert calls == [(plan_id, False)]
    # The execution round was the one that got stopped — not discarded.
    assert stop_handlers == [("execution", plan_id, False)]
    assert result.ok is True
    assert result.final_phase == "stopped"
    assert result.phase == "stopped"
    assert _persisted_phase(plan_dir) == "stopped"


# ---------------------------------------------------------------------------
# 2. A queued plan is parked, and leaves the queue
# ---------------------------------------------------------------------------


def test_pause_queued_moves_to_stopped():
    """A queued plan is parked in ``stopped`` and its queue row is gone.

    The queue row is half of the operation: a plan that is parked but still
    queued will be picked up by the scheduler's admission check, and a pause
    that gets undone by the next tick is not a pause.
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "pause-queued-p2"
    plan_dir = _make_plan(plan_id, "queued")
    _enqueue(plan_id)
    assert plan_id in _queued_plan_ids()

    result = PlanLifecycleService().pause_plan(plan_id)

    assert result.ok is True
    assert result.final_phase == "stopped"
    assert result.phase == "stopped"
    # The phase really moved (the database, not just the return value).
    assert _persisted_phase(plan_dir) == "stopped"
    # And the plan is no longer waiting to be started.
    assert plan_id not in _queued_plan_ids()


# ---------------------------------------------------------------------------
# 3. A parked plan goes back into the waiting phase
# ---------------------------------------------------------------------------


def test_requeue_stopped_to_queued():
    """A parked plan is requeued: ``stopped → queued``."""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-stopped-p1"
    plan_dir = _make_plan(plan_id, "stopped")

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is True
    assert result.final_phase == "queued"
    assert result.phase == "queued"
    assert _persisted_phase(plan_dir) == "queued"


# ---------------------------------------------------------------------------
# 4. The vocabulary did not move
# ---------------------------------------------------------------------------


def test_phase_and_edge_sets_unchanged():
    """The phases and the legal edges are the ones that were there before.

    Item by item, not "roughly the same": a phase that disappeared strands
    every plan in it, and an edge that disappeared makes a transition that
    used to work raise.  The facade's two new entries **reuse** edges; they
    do not add any.
    """
    import plan_state

    assert plan_state.VALID_PHASES == _PHASES_BEFORE
    assert plan_state.PHASE_TRANSITIONS == _MAIN_EDGES_BEFORE
    assert (
        plan_state.VERIFICATION_PHASE_TRANSITIONS == _VERIFICATION_EDGES_BEFORE
    )
    assert plan_state.DEDICATED_PHASE_TRANSITIONS == _DEDICATED_EDGES_BEFORE
    assert sorted(plan_state.TERMINAL_PHASES) == _TERMINAL_PHASES_BEFORE


# ---------------------------------------------------------------------------
# 5. Everything else is a conflict
# ---------------------------------------------------------------------------


def test_requeue_from_running_conflicts():
    """Requeueing a running plan is a conflict, and nothing moves.

    A running plan is not parked, so there is nothing to requeue — and the
    answer must not be "stop it and queue it again", because that is two
    operations wearing one name.
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-running-p1"
    plan_dir = _make_plan(plan_id, "executing")
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is False
    assert result.detail == "conflict"
    # The phase did not move — the caller is told where the plan still is.
    assert result.final_phase == "executing"
    assert result.berth_phase == "executing"
    assert _persisted_phase(plan_dir) == "executing"


def test_pause_completed_conflicts_without_phase_change():
    """Pausing a completed plan is a conflict, and the phase stays put.

    ``completed`` is terminal: there is nothing running to stop and nothing
    queued to take out of the queue.  The phase must not move — a pause
    that quietly re-opened a finished plan would be worse than a refusal.
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "pause-completed-p1"
    plan_dir = _make_plan(plan_id, "completed")

    result = PlanLifecycleService().pause_plan(plan_id)

    assert result.ok is False
    assert result.detail == "conflict"
    assert result.final_phase == "completed"
    assert _persisted_phase(plan_dir) == "completed"


def test_cas_conflict_is_reported_without_retry(monkeypatch):
    """A CAS conflict keeps the existing semantics: it surfaces, it is not retried.

    The row was bumped by somebody else between the read and the write.
    The repository says so with ``ConflictError`` — the same exception
    every other CAS in this codebase raises, and the one the caller is
    expected to answer with a 409.  Retrying would overwrite their change
    with ours, so the transition is attempted exactly once.
    """
    import plan_state
    from state_machine.repositories.routing_repository import ConflictError

    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-conflict-p1"
    plan_dir = _make_plan(plan_id, "stopped")

    attempts: List[str] = []

    def conflicting_transition(self, phase):
        attempts.append(phase)
        raise ConflictError(
            plan_id=plan_id,
            reason=(
                "version mismatch: row was bumped between read and update "
                "(expected version 0)"
            ),
            current_phase="stopped",
            current_version=1,
        )

    monkeypatch.setattr(
        plan_state.PlanState, "transition_to", conflicting_transition,
    )

    with pytest.raises(ConflictError):
        PlanLifecycleService().requeue_plan(plan_id)

    # One attempt, not two: the conflict is not retried over the writer.
    assert attempts == ["queued"]
    # And the row was not overwritten behind the conflict's back.
    assert _persisted_phase(plan_dir) == "stopped"


def test_lifecycle_result_keeps_the_frozen_field_set():
    """``LifecycleResult`` still has exactly the six contract fields.

    The two lifecycle endpoints answer with this dataclass verbatim, so
    every field is a key in a response body the consumer on the other side
    of the repository parses (``docs/development/cross-repo-contract.md``
    §1.3).  ``ok`` and ``phase`` are read-only views over those six, not
    two more fields — a seventh field is a seventh key.
    """
    import dataclasses

    from plan_lifecycle_service import LifecycleResult

    assert tuple(f.name for f in dataclasses.fields(LifecycleResult)) == (
        "plan_id",
        "dispatched",
        "berth_phase",
        "final_phase",
        "checkpoint",
        "detail",
    )
    # A view is not a key: the serialized body is the six fields and
    # nothing else.
    assert set(dataclasses.asdict(
        LifecycleResult(
            plan_id="p1",
            dispatched=None,
            berth_phase="stopped",
            final_phase="stopped",
            checkpoint=None,
            detail="pause_queued",
        )
    )) == {
        "plan_id",
        "dispatched",
        "berth_phase",
        "final_phase",
        "checkpoint",
        "detail",
    }


def test_requeue_unknown_plan_raises_not_found():
    """An unknown plan is still the existing not-found error."""
    from plan_lifecycle_service import PlanLifecycleService, PlanNotFoundError

    with pytest.raises(PlanNotFoundError):
        PlanLifecycleService().requeue_plan("no-such-plan-at-all")