"""State-machine dead-end fix: ``completed`` must reach a verification verdict.

Background
----------
Execution ends in the ``completed`` phase.  The verification round that
follows then has to record its verdict by transitioning the plan to
``verification_passed`` or ``verification_failed``.  Before this fix
``VERIFICATION_PHASE_TRANSITIONS["completed"]`` was ``[]`` — an empty
target list — so :meth:`PlanState.transition_to` raised
``ValueError: Illegal transition from 'completed' to '...'`` for *both*
verdicts and the plan was stranded in ``completed`` forever.  Neither a
passing nor a failing report could converge.

Contract pinned here
--------------------
1. ``VERIFICATION_PHASE_TRANSITIONS["completed"]`` equals exactly
   ``["verification_passed", "verification_failed"]`` — two targets, no
   more, no fewer, in that order.
2. Any other target out of ``completed`` (notably ``completed`` itself,
   plus the legacy in-progress phases) is rejected with ``ValueError``.
3. Every other row of the table is untouched by this change.

The tests reach the shared bug-fix plan fixtures from task 1
(``mock_plan_state_factory`` / ``plan_dir_writer``, registered in
``backend/tests/conftest.py``) rather than hand-rolling a
``plan_state.json`` payload, so a schema drift in the shared fixture
fails here too.

``plan_state`` is imported by module name (not ``backend.plan_state``)
because ``backend/pytest.ini`` puts ``backend/`` on ``pythonpath`` — the
same import style the existing
``tests/test_verification_state_machine.py`` uses.
"""

import pytest

from plan_state import (
    PHASE_TRANSITIONS,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)


#: The exact target list the fix installs for the ``completed`` row.
EXPECTED_COMPLETED_TARGETS = ["verification_passed", "verification_failed"]


def _allowed_targets(current: str) -> set:
    """Mirror ``PlanState.transition_to``'s legality lookup.

    ``transition_to`` unions the two tables before checking membership::

        allowed = set(PHASE_TRANSITIONS.get(current, []))
        allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(current, []))

    Reproducing that union here lets the table-level tests assert on the
    *effective* legality set rather than on one table in isolation.
    """
    allowed = set(PHASE_TRANSITIONS.get(current, []))
    allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(current, []))
    return allowed


@pytest.fixture
def completed_plan(tmp_path, mock_plan_state_factory, plan_dir_writer):
    """A plan parked in ``completed`` with execution already recorded.

    ``transition_to`` refuses any ``verification*`` target unless the
    current phase is ``executing`` **or** ``"execution"`` is already in
    ``completed_phases``.  A plan that legitimately reached ``completed``
    always satisfies the second condition (``transition_to`` appends
    ``"execution"`` for every terminal phase), so the fixture seeds it
    explicitly — otherwise the test would trip the prerequisite guard
    instead of exercising the transition table.
    """
    state = mock_plan_state_factory(
        "pending",
        "completed",
        plan_id="completed-transition",
        completed_phases=["executing", "execution"],
    )
    plan_dir = plan_dir_writer(tmp_path, "completed-transition", state)
    return PlanState(plan_dir)


# ---------------------------------------------------------------------------
# TDD spec 1: the table declares exactly the two verification verdicts.
# ---------------------------------------------------------------------------


def test_verification_phase_transitions_completed_has_two_targets():
    """Reading the dict yields the two verdict targets, exactly.

    An equality assertion (not a subset check) is deliberate: a refactor
    that *adds* an outbound edge — e.g. ``completed -> executing``, which
    would let a finished plan silently revert to in-progress — must fail
    here.
    """
    targets = VERIFICATION_PHASE_TRANSITIONS["completed"]

    assert targets == EXPECTED_COMPLETED_TARGETS, (
        f"VERIFICATION_PHASE_TRANSITIONS['completed'] must be exactly "
        f"{EXPECTED_COMPLETED_TARGETS!r}, got {targets!r}"
    )
    assert len(targets) == 2, (
        f"expected exactly 2 outbound targets, got {len(targets)}: {targets!r}"
    )
    # Self-loop must not be declared: ``transition_to`` treats a same-phase
    # call as an idempotent no-op, and the table must not imply otherwise.
    assert "completed" not in targets, (
        "completed must not declare a self-loop in the transition table"
    )


def test_completed_reaches_both_verification_verdicts(
    tmp_path, mock_plan_state_factory, plan_dir_writer
):
    """Both declared edges actually work through the public API.

    Each verdict gets its own freshly-written plan directory so the two
    transitions never interfere.
    """
    for target in EXPECTED_COMPLETED_TARGETS:
        state = mock_plan_state_factory(
            "pending",
            "completed",
            plan_id=f"completed-to-{target}",
            completed_phases=["executing", "execution"],
        )
        plan_dir = plan_dir_writer(tmp_path, f"completed-to-{target}", state)
        ps = PlanState(plan_dir)

        ps.transition_to(target)

        assert ps.get_current_phase() == target, (
            f"completed -> {target} must land in {target}, "
            f"got {ps.get_current_phase()!r}"
        )
        assert "completed" in ps.get_state()["completed_phases"], (
            "the source phase must be recorded in completed_phases"
        )


# ---------------------------------------------------------------------------
# TDD spec 2: any other target out of ``completed`` raises ValueError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_target",
    [
        # NOTE: ``verification`` is deliberately absent — it is a legal edge
        # out of ``completed`` via the *main* table
        # (``PHASE_TRANSITIONS["completed"] == ["verification"]``), which this
        # fix leaves untouched. Only targets that neither table declares
        # belong in this list.
        "executing",
        "ready",
        "verification_running",
        "verification_repairing",
        "verification_rerunning",
        "verification_loop_stopped",
        "failed",
        "stopped",
        "interview",
        "prd_review",
    ],
)
def test_completed_rejects_invalid_target(completed_plan, invalid_target):
    """An undeclared target raises ``ValueError`` naming the legal set."""
    assert invalid_target not in _allowed_targets("completed"), (
        f"test bug: {invalid_target!r} is declared legal out of 'completed'"
    )

    with pytest.raises(ValueError) as excinfo:
        completed_plan.transition_to(invalid_target)

    message = str(excinfo.value)
    assert "completed" in message and invalid_target in message, (
        f"the error must name both endpoints, got {message!r}"
    )
    # The plan must not have moved.
    assert completed_plan.get_current_phase() == "completed"


def test_completed_self_loop_is_not_a_legal_declared_target():
    """``completed -> completed`` is not a declared edge.

    ``PlanState.transition_to`` short-circuits a same-phase call as an
    idempotent no-op *before* the legality lookup, so the runtime call
    does not raise.  The contract that matters for the table is that the
    self-loop is absent from the effective legality set — pinned here so a
    refactor cannot smuggle it in via either table.
    """
    allowed = _allowed_targets("completed")

    assert "completed" not in allowed, (
        f"completed -> completed must not be a declared edge; "
        f"effective legal targets: {sorted(allowed)}"
    )
    assert allowed == set(EXPECTED_COMPLETED_TARGETS) | set(
        PHASE_TRANSITIONS.get("completed", [])
    ), (
        f"effective legal targets out of 'completed' drifted: {sorted(allowed)}"
    )


# ---------------------------------------------------------------------------
# TDD spec 3: no other row of the table was disturbed.
# ---------------------------------------------------------------------------


def test_other_phase_transitions_unchanged():
    """The rest of the verification table keeps its existing definition."""
    expected_rows = {
        "ready": ["verification", "verification_running"],
        "executing": [
            "verification",
            "completed",
            "failed",
            "stopped",
            "ready",
        ],
        "verification": [
            "verification_running",
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
        ],
        "verification_running": [
            "verify_first_pass",
            "verification_passed",
            "verification_failed",
        ],
        "verify_first_pass": [
            "verify_recheck",
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
        ],
        "verify_recheck": [
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
        ],
        # ``verification_passed`` is the security-boundary terminal:
        # the migration lands plans here and the whitelist does NOT
        # permit a forward edge to ``completed``. A previous version
        # of this table declared ``["completed"]`` here; task 7
        # tightened the boundary so that an attacker / runaway
        # retry cannot push a verified-passed plan further into
        # ``completed`` via ``transition_to``.
        "verification_passed": [],
        "verification_failed": [
            "verification_repairing",
            "verification_rerunning",
            "verification_passed",
            "verification_loop_stopped",
        ],
        "verification_repairing": [
            "verification",
            "verification_rerunning",
            "verification_passed",
            "verification_loop_stopped",
            "verification_failed",
            "executing",
        ],
        "verification_rerunning": [
            "verification_running",
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
        ],
        # 2026-09-13: production added the ``verification_passed``
        # recovery edge (operator force-pass after the loop stopped;
        # plan_state.py:179). Kept in sync here so the table-pinning
        # test reflects the live contract.
        "verification_loop_stopped": ["failed", "verification_passed"],
        "failed": ["executing", "ready"],
        "stopped": ["executing", "ready"],
    }

    for source, targets in expected_rows.items():
        assert VERIFICATION_PHASE_TRANSITIONS[source] == targets, (
            f"VERIFICATION_PHASE_TRANSITIONS[{source!r}] changed: "
            f"expected {targets!r}, got "
            f"{VERIFICATION_PHASE_TRANSITIONS[source]!r}"
        )

    # And no row was added or removed alongside the ``completed`` fix.
    assert set(VERIFICATION_PHASE_TRANSITIONS) == set(expected_rows) | {
        "completed"
    }, (
        "the set of source phases in VERIFICATION_PHASE_TRANSITIONS changed: "
        f"{sorted(VERIFICATION_PHASE_TRANSITIONS)}"
    )
