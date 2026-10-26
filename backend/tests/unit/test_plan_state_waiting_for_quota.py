"""2026-10-24: the ``waiting_for_quota`` phase — an explicit "parked,
waiting for provider capacity" state.

Why this phase exists
---------------------
When every provider is out of quota (HTTP 429), the executor used to have
nowhere to put the plan: the phase vocabulary had no value for "work
stopped, capacity has not come back yet". The plan either stayed in
``executing`` (so it read as live work that had silently stalled) or was
pushed into a terminal phase (so the operator had to re-create it from
scratch).

``waiting_for_quota`` gives that state a name, which buys four things:

* the plan can be **restarted** from it once capacity returns
  (``→ executing``), pulled back to ``ready``, or parked in ``stopped``;
* ``POST /api/execution/{id}/start`` accepts it as a source phase, so a
  plan that was waiting for quota can be restarted after a backend
  restart without an operator hand-hack;
* the status / progress response models accept it, so polling
  ``/api/execution/{id}/status`` does not 500 on every poll — the same
  class of failure as the 2026-09-14 ``stop_reason`` Literal incident;
* the Feishu / Telegram cards render a Chinese label instead of falling
  through to the raw English phase string.

What this phase deliberately is NOT:

* it is **not terminal** — a plan waiting for quota is still live and can
  be resumed;
* it is **not part of the verification sub-machine** — this import covers
  execution-time capacity only, so ``VERIFICATION_PHASE_TRANSITIONS`` is
  untouched;
* it is **not a "terminal failure"** (``cards._TERMINAL_FAILURE_PHASES``)
  — no worker is stuck and nothing has failed; the plan is simply parked.

The illegal-source case is pinned too: ``queued → waiting_for_quota``
must raise. A plan that has not started executing has no quota to wait
for, so entering this phase from the waiting queue would be a lie about
state.  (``ready`` is the legacy alias of ``queued`` — R10 / ``phase_vocabulary``
— and it is rejected the same way; both are pinned below.)
"""

from __future__ import annotations

import pytest

import plan_state
from plan_state import (
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    VALID_PHASES,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)


def _routing_phase(plan_id: str, db_path) -> str:
    """Read ``plan_routing.current_phase`` for ``plan_id`` straight from
    the state machine.

    The routing row — not the in-memory ``PlanState._state`` — is what
    the scheduler, the execution-start CAS and the card pusher read. A
    phase that only lands in memory is a phase nothing else can see.
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = _open_db(str(db_path))
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    assert row is not None, f"no plan_routing row for {plan_id!r}"
    return row["current_phase"]


def test_waiting_for_quota_is_a_valid_phase():
    assert "waiting_for_quota" in VALID_PHASES, (
        f"the phase vocabulary must accept waiting_for_quota; "
        f"got {VALID_PHASES}"
    )


def test_executing_can_enter_waiting_for_quota(sample_plan_factory):
    """The whole point: a running plan that ran out of capacity parks."""
    plan_dir = sample_plan_factory(plan_id="quota-wait-plan", phase="executing")
    ps = PlanState(plan_dir)

    ps.transition_to("waiting_for_quota")

    assert ps.get_current_phase() == "waiting_for_quota"
    assert "waiting_for_quota" in PHASE_TRANSITIONS["executing"], (
        "the executing row must carry the edge into the wait state"
    )


def test_waiting_for_quota_is_idempotent(sample_plan_factory):
    """Re-entering the same phase is a no-op, not an error.

    The wait loop will retry "park again" every poll; a repeated
    transition must not raise.
    """
    plan_dir = sample_plan_factory(plan_id="quota-wait-idem", phase="executing")
    ps = PlanState(plan_dir)

    ps.transition_to("waiting_for_quota")
    ps.transition_to("waiting_for_quota")

    assert ps.get_current_phase() == "waiting_for_quota"


@pytest.mark.parametrize("target", ["executing", "queued", "stopped"])
def test_waiting_for_quota_can_restart(sample_plan_factory, target):
    """A parked plan must be able to leave the wait state.

    ``→ executing`` is the restart path (capacity came back),
    ``→ queued`` pulls it back to the waiting queue (the manual gate),
    ``→ stopped`` parks it for good. All three must be legal *and* must
    land on the routing row, because the routing row is what the
    scheduler and the execution-start CAS actually read.

    2026-10-26 (merge): the target is ``queued``, not ``ready`` — R10 made
    ``queued`` the only waiting phase and ``ready`` its legacy alias, so
    ``transition_to("ready")`` is normalized to ``queued`` before the
    legality check and never lands on ``ready``.  The edge
    ``waiting_for_quota → queued`` is the one the write path uses.
    """
    plan_id = f"quota-wait-{target}"
    plan_dir = sample_plan_factory(plan_id=plan_id, phase="executing")
    ps = PlanState(plan_dir)

    ps.transition_to("waiting_for_quota")
    ps.transition_to(target)

    assert ps.get_current_phase() == target
    assert target in PHASE_TRANSITIONS["waiting_for_quota"], (
        f"the wait row must allow {target!r}; "
        f"got {PHASE_TRANSITIONS['waiting_for_quota']!r}"
    )
    assert _routing_phase(plan_id, plan_state._state_db_path()) == target


@pytest.mark.parametrize("source", ["queued", "ready"])
def test_illegal_source_rejected(sample_plan_factory, source):
    """``queued → waiting_for_quota`` must raise (and so must ``ready``).

    A plan that never started executing has no quota to wait for.
    The message is asserted too, so this test cannot pass for the wrong
    reason ("Invalid phase" — i.e. the phase was not in the vocabulary
    at all, which is a different bug).

    2026-10-26 (merge): ``queued`` is the live waiting phase (R10);
    ``ready`` is the legacy alias, and a row that still carries it must
    be rejected the same way — the phase rule must not have a second,
    softer door for the old word.
    """
    plan_dir = sample_plan_factory(plan_id="quota-wait-illegal", phase=source)
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError, match="Illegal transition"):
        ps.transition_to("waiting_for_quota")

    assert ps.get_current_phase() == source


def test_not_terminal():
    """The wait state is live, not finished."""
    assert "waiting_for_quota" not in TERMINAL_PHASES, (
        "a plan waiting for quota can still be resumed — it is not terminal"
    )

    from notifications import cards

    assert "waiting_for_quota" not in cards._TERMINAL_FAILURE_PHASES, (
        "nothing failed here — the plan is parked, not broken"
    )


def test_not_part_of_the_verification_sub_machine():
    """This import covers execution-time capacity only.

    A plan waiting for quota during VERIFICATION would need its own
    edges; adding them here would silently claim that support exists.
    """
    assert "waiting_for_quota" not in VERIFICATION_PHASE_TRANSITIONS
    assert "waiting_for_quota" not in VERIFICATION_PHASE_TRANSITIONS.get(
        "waiting_for_quota", []
    )


def test_execution_start_accepts_the_phase():
    """The execution-start CAS must accept the wait phase.

    Without this, a plan parked in the wait state could never be
    restarted through ``POST /api/execution/{id}/start`` — including
    after a backend restart.
    """
    import server

    assert "waiting_for_quota" in server._EXECUTION_START_SOURCE_PHASES


def test_status_response_models_accept_the_phase(tmp_path):
    """Both response models must accept the wait state.

    ``/api/execution/{plan_id}/status`` uses ``ExecutionStatusResponse``
    as its ``response_model`` (backend/routes/execution.py). A Literal
    that does not list the phase makes the endpoint raise
    ``ResponseValidationError`` → 500 on every poll. Both Literals have
    to grow the value together — the 2026-09-14 ``stop_reason`` incident
    was exactly this class of bug.
    """
    import server

    status = server.ExecutionStatusResponse(
        plan_id="p",
        status="waiting_for_quota",
        logs=[],
        project_dir=str(tmp_path),
    )
    progress = server.ExecutionProgressResponse(
        plan_id="p",
        project_dir=str(tmp_path),
        execution_status="waiting_for_quota",
        tasks=[],
        counts={},
    )

    assert status.status == "waiting_for_quota"
    assert progress.execution_status == "waiting_for_quota"


def test_card_labels_are_chinese():
    """The card must not show the operator a raw English phase."""
    from notifications import cards

    assert "等待" in cards._PHASE_LABEL.get("waiting_for_quota", ""), (
        f"card header label missing for waiting_for_quota: "
        f"{cards._PHASE_LABEL.get('waiting_for_quota')!r}"
    )
    assert "等待" in cards._EXECUTION_PHASE_LABEL.get("waiting_for_quota", ""), (
        f"card sub-line label missing for waiting_for_quota: "
        f"{cards._EXECUTION_PHASE_LABEL.get('waiting_for_quota')!r}"
    )
