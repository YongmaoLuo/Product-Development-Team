"""A round blocked by base drift must not spend a round of the budget.

Why this gate exists
--------------------
A round the base-drift check blocked dispatched **nothing**: the branch had
drifted away from its base, merging the base back left conflicts, and the
round therefore has no verdict at all — nobody ever looked at the product.
It is not a failed round; it is a round that did not happen.

``_plan_next_round`` used to answer "which round comes next?" with
``current.round + 1``, unconditionally.  So a plan whose branch kept
drifting burned one round of its ``max_rounds`` budget per drift: four
drifts and the plan reported ``max_rounds_reached`` after **four rounds
that never ran**, with the plan no further from finished than when it
started.  The budget is the plan's own limit on how much work it may do
— a round that did no work must not be charged to it.

The fix reuses the same round number for a blocked round.  The round is
re-run under the number it was blocked at, so the verdict, the report and
the repair tasks of the eventual real round all carry the number the plan
was on.  A round that really finished keeps advancing as before — the
"round + 1" rule is unchanged for every round that produced a verdict.

What is pinned here
-------------------
1. A row whose stop reason is ``base_drift_conflicts`` reuses its round
   number, and is never ``exhausted`` — not even sitting exactly on the
   cap.
2. A row with no stop reason, or with any *other* stop reason, still
   advances (``round + 1``), and still exhausts at the cap.
3. The block is recognised from the ``verification_stop_reason`` column
   **or** from the ``results`` envelope alone — the two are written by
   different call sites, and a row that carries only one of them is
   still a blocked row.
4. A blocked round numbered 0 re-runs as round 1: the round counter is
   never 0, because 0 means "no round has run yet" everywhere else in
   the codebase.
5. The cap is still immutable — a blocked round returns the row's own
   ``max_rounds``, unchanged.

The helper is pinned directly too (``_round_was_blocked_by_base_drift``):
it is the predicate the round decision is built on, and a predicate that
quietly grows a second source of truth is how the two paths drift apart.
"""

from __future__ import annotations

import json

import pytest

# ``server`` has to be imported first: it pulls in the routers at its own
# module scope, and reaching ``routes.verification`` directly trips a
# circular import.  Same ordering the sibling round-resolution tests use.
import server as server_mod
import verification_loop
from verification_loop import _plan_next_round


#: The stop reason a base-drift-blocked round carries.
BASE_DRIFT = verification_loop.BASE_DRIFT_CONFLICTS_STOP_REASON


def _blocked_row(**overrides):
    """A row that says "this round was blocked by a base-drift conflict"."""
    row = {
        "round": 1,
        "max_rounds": 4,
        "verification_status": "running",
        "verification_stop_reason": BASE_DRIFT,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# A blocked round reuses its number
# ---------------------------------------------------------------------------


def test_blocked_round_is_reused():
    """The blocked round is re-run under the number it was blocked at.

    Round 1 was blocked by a drift, so the next round **is** round 1 —
    not round 2.  Advancing here is what charged the drift to the
    plan's budget.
    """
    assert _plan_next_round(_blocked_row(round=1, max_rounds=4)) == (
        1, 4, False,
    )


def test_blocked_round_at_cap_is_not_exhausted():
    """A blocked round sitting exactly on the cap is not out of budget.

    ``max_rounds`` counts rounds that RAN.  A round blocked before it
    dispatched anything did not run, so a plan blocked at round 4 of 4
    still has its budget — and must not be reported as
    ``max_rounds_reached``.
    """
    assert _plan_next_round(_blocked_row(round=4, max_rounds=4)) == (
        4, 4, False,
    )


def test_blocked_round_zero_never_returns_zero():
    """Round 0 means "no round has run yet" — a blocked row never stays 0.

    Reusing the number verbatim would hand back 0, which every other
    consumer of the counter reads as "no round yet".  A blocked row
    numbered 0 therefore re-runs as round 1.
    """
    next_round, _cap, exhausted = _plan_next_round(
        _blocked_row(round=0, max_rounds=4),
    )
    assert next_round == 1
    assert exhausted is False


def test_blocked_round_does_not_raise_the_cap():
    """The cap is the plan's budget; a blocked round does not change it."""
    _next_round, cap, _exhausted = _plan_next_round(
        _blocked_row(round=2, max_rounds=3),
    )
    assert cap == 3


# ---------------------------------------------------------------------------
# A round that really finished still advances
# ---------------------------------------------------------------------------


def test_normal_round_advances():
    """No stop reason at all: the round really finished, so advance."""
    assert _plan_next_round({
        "round": 1,
        "max_rounds": 4,
        "verification_stop_reason": None,
    }) == (2, 4, False)


@pytest.mark.parametrize(
    "stop_reason",
    [
        "max_rounds_reached",
        "same_failure_repeated_after_max_attempts",
        "user_stopped",
        "verification_passed",
    ],
)
def test_other_stop_reasons_advance(stop_reason):
    """Only the base-drift block reuses the round.

    Every other stop reason describes a round that produced a verdict —
    it passed, it failed, the operator stopped it.  Those rounds spent
    budget, so they advance as before.
    """
    assert _plan_next_round({
        "round": 1,
        "max_rounds": 4,
        "verification_stop_reason": stop_reason,
    }) == (2, 4, False)


def test_a_finished_round_still_exhausts_at_the_cap():
    """The cap still bounds a plan whose rounds really ran."""
    assert _plan_next_round({
        "round": 3,
        "max_rounds": 3,
        "verification_stop_reason": None,
    }) == (4, 3, True)


def test_without_a_row_the_plan_starts_at_one():
    """No row at all: round 1 against the default cap, unchanged."""
    assert _plan_next_round(None) == (
        1,
        server_mod.DEFAULT_MAX_VERIFICATION_ROUNDS,
        False,
    )


# ---------------------------------------------------------------------------
# The block is recognised from the envelope alone too
# ---------------------------------------------------------------------------


def test_results_envelope_alone_is_enough():
    """A row whose ``results`` envelope carries the reason is blocked.

    The stop reason reaches the row by two different routes: the
    ``verification_stop_reason`` column, and the ``results`` envelope
    the round wrote down.  A row that carries only the envelope is still
    a blocked row — otherwise the same drift is charged to the budget
    depending on which writer got there first.
    """
    row = {
        "round": 1,
        "max_rounds": 4,
        "verification_status": "running",
        "results": {"stop_reason": BASE_DRIFT},
    }
    assert _plan_next_round(row) == (1, 4, False)


def test_results_envelope_as_json_text_is_enough():
    """The envelope may still be a JSON string on the row.

    The repository decodes the column, but a row read straight out of
    SQLite carries the raw JSON.  Both shapes mean the same thing, so
    both must be recognised.
    """
    row = {
        "round": 2,
        "max_rounds": 4,
        "results": json.dumps({"stop_reason": BASE_DRIFT}),
    }
    assert _plan_next_round(row) == (2, 4, False)


def test_a_results_envelope_with_another_reason_advances():
    """An envelope with a different reason is not a base-drift block."""
    row = {
        "round": 1,
        "max_rounds": 4,
        "results": {"stop_reason": "max_rounds_reached"},
    }
    assert _plan_next_round(row) == (2, 4, False)


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------


def test_helper_recognises_the_blocked_column():
    """The helper answers True for a row blocked by a base drift."""
    assert verification_loop._round_was_blocked_by_base_drift(
        _blocked_row(),
    ) is True


def test_helper_rejects_an_ordinary_row():
    """The helper answers False for a row with no reason at all."""
    assert verification_loop._round_was_blocked_by_base_drift({
        "round": 1,
        "max_rounds": 4,
        "verification_stop_reason": None,
    }) is False


def test_helper_rejects_a_missing_row():
    """No row at all is not a blocked round."""
    assert verification_loop._round_was_blocked_by_base_drift(None) is False