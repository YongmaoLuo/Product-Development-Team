"""The ``postprocess`` package skeleton and the two phases it registers.

What this file witnesses
------------------------
This is the entry task of the optional post-processing (收尾) stage.  It
does three things and nothing else:

1. it proves the ``postprocess`` package is importable as a **namespace
   package** — there is deliberately no ``__init__.py``;
2. it proves the two new phase names are constants the rest of the
   subsystem imports instead of re-spelling the strings;
3. it proves the phase vocabulary grew by exactly those two names and by
   nothing else — the pre-existing members keep their relative order.

Why the vocabulary check is a *snapshot*
----------------------------------------
"the new set contains the old set" is only checkable if the old set is
written down somewhere.  Deriving it from the live list would make the
assertion vacuous (a list trivially contains itself), so the
pre-existing vocabulary is spelled out member by member in
:data:`PHASES_BEFORE`.  A phase that disappears, a phase that moves, or a
third phase that sneaks in all fail here.

The plan-level stopping phase (``user_stopping``, introduced by the stop
contract) is inserted **between** two pre-existing phases, so "the old
vocabulary sits at the head of the live one" stopped being true.  What
is true — and what this file now pins — is that the old vocabulary is an
**ordered subsequence** of the live one, and that the live vocabulary is
the old one plus exactly the three names in :data:`PHASES_AFTER`.

The transition case pins the **entry** into the new phase.  The approved
architecture extends the phase model with::

    verification_passed → post_processing → completed
                            ↘ postprocess_failed

so a plan whose verification round passed can enter the finishing stage.
That edge lives in the MAIN transition table (``PHASE_TRANSITIONS``), not
in ``VERIFICATION_PHASE_TRANSITIONS``: the verification sub-table's
``verification_passed`` row is a frozen security boundary (a passing
verdict may not be laundered onwards by a stray retry), while the
finishing stage is a workflow-level phase the orchestrator's terminal exit
enters — not a step of the verification sub-machine.

The unknown-phase case is the other half of the same contract: widening
the vocabulary must not widen what the state machine *accepts*.
"""

from __future__ import annotations

import pytest

from plan_state import VALID_PHASES, PlanState
from postprocess.state import (
    PHASE_ADDITIONS,
    POSTPROCESS_FAILED,
    POST_PROCESSING,
)


#: The phase vocabulary as it stood **before** this task registered the two
#: post-processing phases.  Written out member by member on purpose: a
#: snapshot computed from the live list cannot witness that the list did
#: not move.
PHASES_BEFORE = [
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
    "import_failed",
    "completed",
    "failed",
    "stopped",
    "archived",
]


def test_phase_constants_exist():
    """The two phase names are importable constants, spelled exactly."""
    assert POST_PROCESSING == "post_processing"
    assert POSTPROCESS_FAILED == "postprocess_failed"
    assert set(PHASE_ADDITIONS) == {POST_PROCESSING, POSTPROCESS_FAILED}


#: The phases that joined the vocabulary **after** this task's two
#: post-processing phases were registered.  ``user_stopping`` is the
#: plan-level stopping phase the stop contract introduced: a stop is a
#: process with an order, and the phase that says "a stop is in progress"
#: is part of the vocabulary like any other.
PHASES_AFTER = ("post_processing", "postprocess_failed", "user_stopping")


def test_new_phases_are_additive():
    """The vocabulary grew by exactly three names and kept the old order.

    The old invariant — "the pre-existing phases sit at the head of the
    vocabulary, in order" — died with the plan-level stopping phase:
    ``user_stopping`` was inserted **between** two pre-existing phases
    (before ``verification_loop_stopped``), so the head-of-the-vocabulary
    assertion is no longer true and is not the guarantee any more.

    The guarantee that survives is the stronger one: the pre-existing
    vocabulary is an **ordered subsequence** of the live one — every old
    phase survives, and the old phases keep their relative order, no
    matter what was inserted between them.
    """
    assert set(PHASES_BEFORE) <= set(VALID_PHASES), (
        f"the pre-existing phases must survive; missing: "
        f"{sorted(set(PHASES_BEFORE) - set(VALID_PHASES))}"
    )
    kept_in_order = [p for p in VALID_PHASES if p in set(PHASES_BEFORE)]
    assert kept_in_order == PHASES_BEFORE, (
        "the pre-existing phases must keep their relative order; got "
        f"{kept_in_order}"
    )
    assert len(VALID_PHASES) == len(PHASES_BEFORE) + len(PHASES_AFTER), (
        f"expected exactly {len(PHASES_AFTER)} new phases; got "
        f"{sorted(set(VALID_PHASES) - set(PHASES_BEFORE))}"
    )
    assert set(VALID_PHASES) == set(PHASES_BEFORE) | set(PHASES_AFTER), (
        f"unexpected vocabulary: {sorted(set(VALID_PHASES) - set(PHASES_BEFORE))}"
    )
    # The post-processing phases are still exactly this task's two.
    assert set(PHASE_ADDITIONS) <= set(PHASES_AFTER)


def test_new_phases_transition_allowed(sample_plan_factory):
    """A passed plan may enter the finishing stage — the edge exists."""
    plan_dir = sample_plan_factory(
        plan_id="postprocess-entry", phase="verification_passed"
    )
    ps = PlanState(plan_dir)

    ps.transition_to(POST_PROCESSING)

    assert ps.get_current_phase() == POST_PROCESSING, (
        f"expected the plan to sit in {POST_PROCESSING!r}, got "
        f"{ps.get_current_phase()!r}"
    )


def test_unknown_phase_still_rejected(sample_plan_factory):
    """Widening the vocabulary must not widen what the machine accepts."""
    plan_dir = sample_plan_factory(
        plan_id="postprocess-unknown", phase="verification_passed"
    )
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError, match="Invalid phase"):
        ps.transition_to("bogus")

    assert ps.get_current_phase() == "verification_passed", (
        "a rejected transition must not move the plan"
    )
