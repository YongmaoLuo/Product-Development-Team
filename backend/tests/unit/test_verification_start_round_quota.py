"""A verification round parked for quota resumes IN PLACE — same round.

Background
----------
A round that ran out of provider capacity is **parked**, not finished.
The plan keeps the phase ``waiting_for_quota``, the ``plan_verification``
row keeps the round it was on, and the verdicts it had already
collected stay on that row.  When capacity comes back the plan is
started again — and that start has to pick the round back up: the
round number is the contract the plan's verdicts, report and repair
tasks are written against.

Two things stood in the way of that, and both are pinned here.

* ``_resolve_start_round`` — the function that answers "which round
  does this start run?" — knew exactly two interrupted states,
  ``running`` and ``loop_stopped``.  A parked row matched neither, so
  the start answered ``round + 1``: the parked round's work was
  relabelled under a round that never ran, and the round counter moved
  on without the round having finished.
* the routing CAS that moves the plan into verification listed the
  phases it accepts as sources.  ``waiting_for_quota`` was not among
  them, so a parked plan was refused with ``409 stage_mismatch``
  before the round resolution was ever reached — the "the state
  machine let it through, the API says 409 forever" shape the shared
  predicate exists to prevent.

The parked round is therefore resumed in place, exactly like a round
the operator stopped: same round number, verdicts untouched.

What it pins
------------
1. a parked row (``verification_status == "waiting_for_quota"``) whose
   round is > 0 resumes THAT round — ``(round, True)``;
2. a parked row with round 0 names no round, so there is nothing to
   resume: an ordinary fresh start at round 1 — ``(1, False)``;
3. ``waiting_for_quota`` is one of the source phases the start
   endpoint's CAS accepts, so a parked plan is not refused with
   ``stage_mismatch``;
4. every other status keeps its old answer.  The parked state is ONE
   more interrupted state, not a new vocabulary.
"""

from __future__ import annotations

import ast
import inspect

import pytest

# ``server`` has to be imported FIRST: it pulls in the routers at its
# own module scope, and reaching ``routes.verification`` directly
# trips a circular import (``ArchivedPlanError`` is still being
# defined when the router re-enters).  Same ordering the application
# itself uses, and the same ordering
# ``tests/static_gates/test_verification_resume_docs_contract.py``
# relies on.
import server as server_mod  # noqa: F401  (imported for its side effect)
from routes.verification import _resolve_start_round, start_verification


# ---------------------------------------------------------------------------
# The source-phase set the start endpoint's CAS accepts
# ---------------------------------------------------------------------------


def _start_cas_source_phases() -> frozenset:
    """The phases the start endpoint's routing CAS accepts as sources.

    Read out of the endpoint's OWN source rather than copied into this
    file: a copy would test the copy, and the copy is exactly what
    drifts when somebody edits the endpoint.

    The argument the endpoint hands to ``try_mark_phase`` is a tuple
    built by concatenation, so the union of the string constants in
    that expression is the set of phases the CAS can be handed — the
    conditional parts contribute their phase only in the situation they
    apply to.
    """
    tree = ast.parse(inspect.getsource(start_verification))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "try_mark_phase"
    ]
    assert len(calls) == 1, (
        "expected exactly one try_mark_phase call in start_verification, "
        f"found {len(calls)} — the source-phase set this file reads has "
        "to be unambiguous"
    )
    call = calls[0]
    assert len(call.args) >= 3, (
        "the routing CAS is expected to be called with (plan_id, "
        f"source_phases, new_phase); got {len(call.args)} positional "
        "arguments"
    )
    target = call.args[2]
    assert (
        isinstance(target, ast.Constant)
        and target.value == "verification_running"
    ), (
        "the CAS in start_verification is expected to move the row to "
        "'verification_running'; the call this file reads moves it to "
        f"{ast.dump(target)}"
    )
    return frozenset(
        node.value
        for node in ast.walk(call.args[1])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


# ---------------------------------------------------------------------------
# The parked round resumes in place
# ---------------------------------------------------------------------------


def test_parked_round_resumes_same_round():
    """A parked round is resumed in place — same round, not round + 1.

    The round was interrupted by capacity, not finished: the row is
    still on it and its verdicts are still on the row.  Opening
    "round 3" here would relabel round 2's work under a round that
    never ran, and would leave the plan's own round counter pointing
    at work that does not exist.
    """
    assert _resolve_start_round(
        {"round": 2, "verification_status": "waiting_for_quota"}, None,
    ) == (2, True)


def test_parked_row_with_zero_round_starts_fresh():
    """Round 0 names no round, so there is nothing to resume.

    The parked state says "this round is unfinished", but round 0 says
    which one is unknown.  Without a round to pick up, the start is an
    ordinary fresh start — and it must NOT claim to be a resume of a
    round that does not exist.
    """
    assert _resolve_start_round(
        {"round": 0, "verification_status": "waiting_for_quota"}, None,
    ) == (1, False)


def test_parked_phase_is_a_valid_start_source():
    """``waiting_for_quota`` is a phase the start endpoint accepts.

    The round resolution is only reached if the routing CAS lets the
    start through at all.  A parked plan whose phase is missing from
    the source set is refused with ``409 stage_mismatch`` before the
    round is ever resolved — so the parked round could never be
    resumed, however the round resolution answered.
    """
    assert "waiting_for_quota" in _start_cas_source_phases()


def test_parked_phase_is_accepted_by_the_real_routing_cas(tmp_path):
    """The accepted phase is a phase a row can actually hold.

    A source-phase list naming a phase no routing row can carry would
    pass the "is it in the list" check and still refuse every parked
    plan.  So the list this file reads is handed to the real
    ``try_mark_phase``: a row seeded at ``waiting_for_quota`` has to
    survive the CAS and land on ``verification_running``.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        repo.insert("plan-parked", "waiting_for_quota")

        assert repo.try_mark_phase(
            "plan-parked",
            tuple(_start_cas_source_phases()),
            "verification_running",
        ) is True

        row = repo.find("plan-parked")
        assert row is not None
        assert row["current_phase"] == "verification_running"
    finally:
        conn.close()


def test_the_source_phase_set_only_grew_by_the_parked_phase():
    """The parked phase was ADDED — no other source phase was lost.

    The gate is the list of phases the start accepts.  Adding one is
    safe; rewriting the list is not — a phase that drops out of it
    stops being re-enterable, and the plan behind it can never be
    started again.  So the whole previous set has to survive, and the
    parked phase has to be the only thing that joined it.
    """
    phases = _start_cas_source_phases()
    previous = {
        "executing",
        "verification",
        "verification_failed",
        "verification_loop_stopped",
        "verification_passed",
        "completed",
        "failed",
        "stopped",
        "verification_repairing",
        # The two conditional entries, counted at their unconditional
        # value: the endpoint adds them when the situation applies.
        "verification_running",
        "verification_plan_review",
    }

    assert previous <= phases, (
        "the start endpoint's source phases lost a phase: "
        f"{sorted(previous - phases)}"
    )
    assert phases - previous == {"waiting_for_quota"}, (
        "the start endpoint's source phases grew by more than the "
        f"parked phase: {sorted(phases - previous)}"
    )


def test_the_auto_loop_path_still_advances_past_a_parked_row():
    """The auto-loop path is not touched by the parked state.

    ``_plan_next_round`` answers "which round comes after a round that
    really finished", and it is reached only once a round is done — so
    a row that still looks interrupted must not make it reuse the
    round number.  The parked state belongs to the ``/start`` path
    alone: if it leaked into this one, the loop would start skipping
    round numbers.
    """
    from verification_loop import _plan_next_round

    assert _plan_next_round(
        {"round": 2, "max_rounds": 3, "verification_status": "waiting_for_quota"},
    ) == (3, 3, False)


# ---------------------------------------------------------------------------
# Everything else keeps its old answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        # A terminal round really finished: the next round is the next one.
        ("failed", (3, False)),
        ("passed", (3, False)),
        # An interrupted round is resumed in place — unchanged.
        ("loop_stopped", (2, True)),
        # The operator's stop is carried by the pause CHECKPOINT, not by
        # the row's status; the row's answer stays what it always was.
        ("user_stopped", (3, False)),
    ],
)
def test_terminal_statuses_keep_old_semantics(status, expected):
    """One more interrupted state, not a new vocabulary.

    The parked state joins ``running`` and ``loop_stopped`` as a third
    way of saying "this round was cut short".  Every other status
    answers exactly what it answered before: a finished round advances,
    an interrupted one resumes, and a user stop keeps being carried by
    the checkpoint (see the checkpoint cases below).
    """
    assert _resolve_start_round(
        {"round": 2, "verification_status": status}, None,
    ) == expected


def test_checkpoint_still_wins_over_a_parked_row():
    """The row's round wins over the checkpoint's — unchanged.

    The DB row is the authority on which round was in flight; the
    checkpoint is the fallback for a row that was never written.  A
    parked row does not change that order.
    """
    assert _resolve_start_round(
        {"round": 2, "verification_status": "waiting_for_quota"},
        {"verification_round": 5},
    ) == (2, True)


def test_checkpoint_alone_still_resumes_its_round():
    """No row, but a checkpoint: the checkpoint names the round."""
    assert _resolve_start_round(
        None, {"verification_round": 5},
    ) == (5, True)


def test_resuming_a_parked_row_keeps_the_round_verdicts(tmp_path):
    """The round number does not move AND the verdicts are not reset.

    R2 has two halves, and the second one is easy to lose: the round
    number is what ``_resolve_start_round`` decides, while the
    verdicts are what the start's ``init_round`` writes over.  A
    resume that keeps the number but wipes the verdicts would relabel
    the round as empty work — the plan would report a round it never
    ran.

    So both halves are pinned here on the real repository: park a row
    that already carries a verdict, resolve the round, re-open THAT
    round, and look at what the row holds afterwards.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(
            "plan-parked",
            verification_status="running",
            round=2,
            max_rounds=3,
            verdicts=[{"vp_id": "VP-001", "verdict": "fail"}],
        )
        repo.mark_parked("plan-parked")

        parked = repo.current("plan-parked")
        assert parked is not None
        assert parked["verification_status"] == "waiting_for_quota"

        # The start resolves the parked round to itself ...
        next_round, resumed = _resolve_start_round(parked, None)
        assert (next_round, resumed) == (2, True)

        # ... and re-opens THAT round.
        repo.init_round("plan-parked", round_n=next_round, max_rounds=3)

        after = repo.current("plan-parked")
        assert after is not None
        assert after["round"] == 2
        assert after["verdicts"] == [{"vp_id": "VP-001", "verdict": "fail"}]
    finally:
        conn.close()
