"""2026-09-15: the ``queued`` phase — the auto-scheduling gate.

Semantics:

* ``ready`` — tasks are generated and approved; a human decides when to
  start and authorizes the run. Never auto-started.
* ``queued`` — the plan was handed to the scheduler explicitly, so the
  scheduler may start it on its own. It starts queued plans one at a
  time per workspace, so two
  plans sharing a working tree can never run concurrently.
* ``ready → executing`` stays legal: the manual path is unchanged.

These tests pin the backend half (vocabulary, transitions, schedulable
set, execution-start CAS, card labels). Whatever consumes this API from
outside — a queue scheduler, a card pusher — is a separate deployment and
is deliberately not named here: nothing in this repository may depend on
knowing what it is called.

2026-09-17 (schema v5): ``plan_routing`` carries exactly one
workflow-state column (``current_phase``), so this file no longer pins a
"phase → routing stage" projection — ``queued`` IS the routing value.
The tests that used to prove "the projection lands the plan where the
scheduler looks" now prove the stronger property directly: the phase the
operator sets is the phase the scheduler selects on.
"""

from __future__ import annotations

import pytest

from plan_state import PHASE_TRANSITIONS, VALID_PHASES, PlanState


def test_queued_is_a_valid_phase():
    assert "queued" in VALID_PHASES


def test_queue_and_unqueue_transitions_are_legal():
    assert "queued" in PHASE_TRANSITIONS["ready"], (
        "the operator must be able to queue a ready plan"
    )
    assert "ready" in PHASE_TRANSITIONS["queued"], (
        "the operator must be able to pull a plan back out of the queue"
    )
    assert "executing" in PHASE_TRANSITIONS["queued"], (
        "the scheduler starts a queued plan by moving it to executing"
    )


def test_manual_path_is_preserved():
    assert "executing" in PHASE_TRANSITIONS["ready"], (
        "ready → executing (manual start, no queue) must keep working"
    )


def test_queued_cannot_jump_into_unrelated_phases():
    allowed = set(PHASE_TRANSITIONS["queued"])
    assert allowed == {"ready", "executing"}, (
        f"queued must be a narrow gate; got {sorted(allowed)}"
    )


def test_plan_state_write_lands_the_queued_phase_on_the_routing_row(
    sample_plan_factory, tmp_path, monkeypatch,
):
    """``queued`` must reach ``plan_routing`` verbatim.

    This is the property the old ``_PLAN_PHASE_TO_ROUTING_STAGE``
    projection was there to provide (``queued`` → ``tasks_queued``).
    With one column there is nothing to project, but the write still has
    to actually happen — hence the assertion.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    plan_dir = sample_plan_factory(plan_id="queued-plan", phase="ready")
    ps = PlanState(plan_dir)

    ps.transition_to("queued")

    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = _open_db(str(db))
    try:
        migrate(conn)
        row = RoutingRepository(conn).current("queued-plan")
    finally:
        conn.close()
    assert row is not None
    assert row["current_phase"] == "queued", row


def test_queued_is_schedulable():
    from state_machine.services.scheduler_support import _SCHEDULABLE_PHASES

    assert "queued" in _SCHEDULABLE_PHASES, (
        "the scheduler's inbox phase must be schedulable"
    )


def test_scheduler_sql_selects_exactly_the_schedulable_phases():
    """The tick query must be generated FROM the constant.

    Before v5 these two drifted apart: ``tasks_queued`` was added to
    ``_SCHEDULABLE_STAGES`` on 2026-09-15 while ``decide_tick`` kept its
    hardcoded ``IN ('tasks_ready', 'executing')``, so a queued plan was
    invisible to the scheduler and the queue gate never auto-started
    anything. Pinning the generated SQL is what stops that recurring.
    """
    import inspect

    from state_machine.services import scheduler_support

    src = inspect.getsource(scheduler_support.SchedulerSupport.decide_tick)
    assert "_SCHEDULABLE_PHASES" in src, (
        "decide_tick must build its IN list from _SCHEDULABLE_PHASES, "
        "not repeat the values inline"
    )
    assert "current_phase IN" in src, (
        "decide_tick must filter on plan_routing.current_phase"
    )


def test_execution_start_accepts_a_queued_plan():
    """The execution-start CAS must accept the queued phase — otherwise a
    queued plan can never actually be started by the scheduler."""
    import server

    assert "queued" in server._EXECUTION_START_SOURCE_PHASES
    assert "ready" in server._EXECUTION_START_SOURCE_PHASES, (
        "the manual gate must stay startable"
    )


def test_execution_start_cas_accepts_the_queued_phase_end_to_end(tmp_path):
    """CAS-level proof: a row sitting at ``queued`` can move to
    ``executing`` with exactly the tuple the endpoint passes."""
    import server
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    db = tmp_path / "state.db"
    conn = _open_db(str(db))
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        repo.insert("queued-cas-plan", "queued")
        repo.try_mark_phase(
            "queued-cas-plan",
            server._EXECUTION_START_SOURCE_PHASES,
            "executing",
        )
        assert repo.current("queued-cas-plan")["current_phase"] == "executing"
    finally:
        conn.close()


def test_card_labels_render_the_queued_gate():
    from notifications import cards

    assert "已排队" in cards._PHASE_LABEL.get("queued", ""), (
        f"card header label missing for queued: {cards._PHASE_LABEL.get('queued')!r}"
    )
    assert "已排队" in cards._EXECUTION_PHASE_LABEL.get("queued", "")


def test_card_phase_label_map_is_exhaustive_for_phases_we_show():
    """Every schedulable phase needs a label.

    An unmapped phase falls through to the raw string, which would show
    the operator an English ``queued`` on a Chinese card — the failure
    mode ``test_card_compact_phase_map_knows_queued`` used to pin with a
    source-text grep.
    """
    from notifications import cards

    for phase in ("ready", "queued", "executing"):
        assert cards._PHASE_LABEL.get(phase, ""), (
            f"no card label for the schedulable phase {phase!r}"
        )
