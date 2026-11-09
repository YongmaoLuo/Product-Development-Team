"""The ``archived`` phase — archived for good, never runs again.

Why this phase exists
----------------------
The historical test plans (a batch of old ones that **never ran**) were
parked in ``stopped``.  But ``stopped`` is an *operable* phase in the
scheduler panel (``resume`` / ``start``), so those plans were shown as
"resumable / startable" — the exact opposite of "we archived this on
purpose, never run it again".

``archived`` is a new **terminal** phase that says the second thing, and
it stays **separate** from ``completed`` ("it really finished").

What this pins
--------------
1. ``archived`` is a legal phase and it is terminal.
2. It is reachable from the early phases — ``interview``,
   ``prd_approved``, ``queued``, ``stopped`` — without a per-phase edge
   (the generic entry point in ``PlanState._transition_to`` allows it).
3. Its only out-edge is ``queued`` (an explicit un-archive); from
   ``archived`` you may NOT go back to ``executing``.
4. An archived plan carries **no execution record** — the archived
   plans are precisely the ones that never ran — while ``completed``
   keeps requiring one.
5. ``PHASE_LABELS["archived"]`` is the stable label ``已归档``.

Scope, stated honestly
----------------------
This reads the phase vocabulary, the transition machinery and the
routing repository's write path.  It does not migrate any existing
plan into ``archived`` — the historical ``stopped`` plans are migrated
by the operator, one plan at a time, through the generic transition.
"""

from __future__ import annotations

import json

import pytest

from plan_state import (
    PHASE_LABELS,
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    VALID_PHASES,
    PlanState,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _hermetic_state_db(tmp_path, monkeypatch):
    """Point ``PlanState`` at a per-test database.

    ``PlanState`` persists into ``plan_routing`` (SQLite).  Without this
    the test would read and write the repository's own ``state.db``.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


# ---------------------------------------------------------------------------
# 1. The phase itself
# ---------------------------------------------------------------------------


def test_archived_is_a_valid_phase():
    assert "archived" in VALID_PHASES


def test_archived_is_terminal():
    assert "archived" in TERMINAL_PHASES


def test_completed_stays_terminal_and_separate():
    """``archived`` must not swallow ``completed``.

    They say two different things — "it really finished" vs. "we
    shelved it" — so both stay in the vocabulary and both stay
    terminal.
    """
    assert "completed" in TERMINAL_PHASES
    assert "completed" in VALID_PHASES
    assert "archived" != "completed"


# ---------------------------------------------------------------------------
# 2. Reaching ``archived`` from the early phases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_phase",
    ["interview", "prd_approved", "queued", "stopped"],
)
def test_early_phases_can_be_archived(sample_plan_factory, source_phase):
    plan_dir = sample_plan_factory(phase=source_phase)
    ps = PlanState(plan_dir)

    ps.transition_to("archived")

    assert ps.get_current_phase() == "archived"


def test_archived_is_a_generic_target_not_a_per_phase_edge():
    """The generic entry point carries the rule, not the phase table.

    ``archived`` must not be listed in every phase's row of
    ``PHASE_TRANSITIONS`` — that would make every new phase forget the
    edge.  The rule lives in the validation itself.
    """
    rows_with_archived = [
        phase for phase, targets in PHASE_TRANSITIONS.items()
        if "archived" in targets
    ]
    assert rows_with_archived == [], (
        "``archived`` is a generic target — no phase row may list it "
        f"explicitly, but these do: {rows_with_archived!r}"
    )


def test_an_archived_plan_carries_no_execution_record(
    sample_plan_factory,
):
    """The archived plans are the ones that never ran.

    Stamping ``execution`` onto their history would record something
    that did not happen.
    """
    plan_dir = sample_plan_factory(phase="stopped")
    ps = PlanState(plan_dir)

    ps.transition_to("archived")

    assert "execution" not in ps.get_completed_phases()


# ---------------------------------------------------------------------------
# 3. Leaving ``archived``
# ---------------------------------------------------------------------------


def test_archived_has_exactly_one_out_edge():
    assert PHASE_TRANSITIONS["archived"] == ["queued"]


def test_archived_can_be_requeued(sample_plan_factory):
    """A mis-archived plan is recoverable — explicitly, by the operator."""
    plan_dir = sample_plan_factory(phase="archived")
    ps = PlanState(plan_dir)

    ps.transition_to("queued")

    assert ps.get_current_phase() == "queued"


def test_archived_cannot_go_back_to_executing(sample_plan_factory):
    plan_dir = sample_plan_factory(phase="archived")
    ps = PlanState(plan_dir)
    # The plan really is in ``archived`` — otherwise the refusal below
    # would be about some other phase and would prove nothing.
    assert ps.get_current_phase() == "archived"

    with pytest.raises(ValueError, match="Illegal transition"):
        ps.transition_to("executing")


# ---------------------------------------------------------------------------
# 4. The routing repository's write path
# ---------------------------------------------------------------------------


def _routing_repo(tmp_path):
    """A :class:`RoutingRepository` on a hermetic database."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    return RoutingRepository(conn), conn


def _completed_phases_of(repo, plan_id):
    row = repo.find(plan_id)
    return list(json.loads(row["completed_phases"] or "[]"))


def test_a_plan_without_execution_history_can_be_archived(tmp_path):
    """The write path must not demand an execution record for ``archived``.

    The archived plans are precisely the ones that never ran, so the
    terminal-phase rule that stamps ``execution`` onto the history has
    to let ``archived`` through.
    """
    from state_machine.repositories import routing_repository

    assert "archived" in routing_repository._TERMINAL_PHASES, (
        "``archived`` is a terminal phase on the routing side too — the "
        "repository keeps its own copy of the terminal set"
    )

    repo, conn = _routing_repo(tmp_path)
    try:
        repo.insert("never-ran", "stopped")

        repo.try_mark_phase("never-ran", ("stopped",), "archived")

        row = repo.find("never-ran")
        assert row["current_phase"] == "archived"
        assert "execution" not in _completed_phases_of(repo, "never-ran"), (
            "an archived plan never ran — its history must not claim "
            "otherwise"
        )
    finally:
        conn.close()


def test_completed_still_records_the_execution(tmp_path):
    """The rule for ``completed`` is not relaxed.

    ``completed`` means "it really finished" — the plan ran — so the
    execution marker still goes into the history.  This is the pair of
    the test above: one phase is let through without a run, the other
    is not.
    """
    repo, conn = _routing_repo(tmp_path)
    try:
        repo.insert("really-ran", "executing")

        repo.try_mark_phase("really-ran", ("executing",), "completed")

        row = repo.find("really-ran")
        assert row["current_phase"] == "completed"
        assert "execution" in _completed_phases_of(repo, "really-ran"), (
            "``completed`` still requires the execution to have run — "
            "the archived exemption must not spill over onto it"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. The stable label
# ---------------------------------------------------------------------------


def test_archived_label():
    assert PHASE_LABELS["archived"] == "已归档"
