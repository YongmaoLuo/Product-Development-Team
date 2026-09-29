"""Security tests for state-machine ↔ audit boundary hardening.

Background
----------
Task 3 unlocked the legal terminal pair ``completed -> {verification_passed,
verification_failed}`` (the dead-end fix), and task 6 shipped the
:migration:`scripts.migrate_20260805_deadlock` one-shot migration that
back-converges stranded plans on disk. Both halves are necessary, but they
must NOT combine into a bypass: an attacker (or an integration bug) that
drops a forged ``verification_report.json`` with ``overall_status == "PASSED"``
onto a plan still parked in ``pending`` must not be able to coerce
``PlanState.transition_to`` into flipping ``current_phase`` to
``verification_passed`` without going through the legal whitelist.

Contract pinned here
--------------------
1. ``test_forged_report_cannot_bypass_state_machine`` —
   a pending plan (``current_phase == "pending"``, ``"execution"`` NOT in
   ``completed_phases``) sitting next to a forged ``PASSED`` report cannot
   reach ``verification_passed`` via :meth:`PlanState.transition_to`. The
   call must raise ``ValueError``. The presence of the forged report on
   disk is irrelevant to the state machine — the migration is the only
   path that may consult the report, and it is also guarded (see
   :mod:`test_migration_audit_security`).
2. ``test_completed_to_verification_passed_still_legal`` — defensive
   sanity check that the legal transition the migration relies on
   (``completed -> verification_passed`` when ``"execution"`` is in
   ``completed_phases``) still works after the security tightening. This
   pins the contract from the OTHER side so a future "tightening" cannot
   accidentally break the migration's only valid path.

The tests reach the shared task-1 fixtures
(``mock_plan_state_factory`` / ``mock_verification_report_factory`` /
``plan_dir_writer`` from ``backend/tests/conftest.py``) rather than
hand-rolling payloads, so a schema drift in the shared fixture fails
here too.

``plan_state`` is imported by module name (not ``backend.plan_state``)
because ``backend/pytest.ini`` puts ``backend/`` on ``pythonpath`` —
the same import style the sibling unit tests use.
"""

import pytest

from plan_state import (
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
    TERMINAL_PHASES,
)


# ---------------------------------------------------------------------------
# TDD spec: forged PASSED report cannot coerce the state machine.
# ---------------------------------------------------------------------------


def test_forged_report_cannot_bypass_state_machine(
    tmp_path,
    mock_plan_state_factory,
    mock_verification_report_factory,
    plan_dir_writer,
):
    """A forged ``PASSED`` report cannot coerce ``pending -> verification_passed``.

    Setup:
      * ``plan_state.json`` is parked in ``pending`` with ``verification
        status == "pending"`` and ``"execution"`` NOT in
        ``completed_phases`` (i.e. the plan never even ran).
      * A forged ``verification_report.json`` carrying
        ``overall_status == "PASSED"`` is dropped next to it — exactly the
        disk artefact task 6's migration would normally pick up, but
        this test deliberately arranges it WITHOUT the migration running
        (no audit record, no milestone backfill).

    Expectation:
      ``PlanState.transition_to("verification_passed")`` raises
      ``ValueError``. The state machine must refuse the transition on
      table-legal grounds alone (``pending`` has no outbound edge to
      ``verification_passed`` in either ``PHASE_TRANSITIONS`` or
      ``VERIFICATION_PHASE_TRANSITIONS``) AND on the
      ``"execution" in completed_phases`` prerequisite guard, so the
      forged report is a no-op regardless of which guard fires first.

    The test asserts the exact exception type so a future refactor that
    converts the guard into a warning (rather than a hard raise) fails
    here.
    """
    state = mock_plan_state_factory(
        "pending",
        "pending",
        plan_id="forged-bypass-pending",
        flags={"arch_enabled": False, "test_enabled": False},
        completed_phases=[],  # NOT "execution" — plan never ran.
    )
    forged_report = mock_verification_report_factory(
        "PASSED",
        plan_id="forged-bypass-pending",
    )
    plan_dir = plan_dir_writer(
        tmp_path, "forged-bypass-pending", state, report=forged_report
    )
    ps = PlanState(plan_dir)

    # Sanity: the forged report is actually on disk and reads as PASSED.
    # This isolates the test from accidental fixture-construction bugs.
    import json
    on_disk = json.loads((plan_dir / "verification_report.json").read_text())
    assert on_disk["overall_status"] == "PASSED", (
        "fixture sanity check failed: forged report must read as PASSED "
        "so the test exercises the bypass path, not a fixture-typo path"
    )

    with pytest.raises(ValueError) as exc_info:
        ps.transition_to("verification_passed")

    # The message should reference either the illegal-transition guard
    # or the execution-completion prerequisite — both are valid, both
    # are hard ``ValueError``s, and either is sufficient to prove the
    # bypass is rejected. We accept either wording.
    msg = str(exc_info.value)
    assert (
        "Illegal transition" in msg
        or "Cannot transition to 'verification_passed'" in msg
    ), (
        f"unexpected ValueError message — expected the bypass to be "
        f"rejected by either the transition-table guard or the "
        f"execution-completion prerequisite; got: {msg!r}"
    )

    # And the on-disk state must be unchanged: no implicit migration
    # happened as a side-effect of the raise.
    after = json.loads((plan_dir / "plan_state.json").read_text())
    assert after["current_phase"] == "pending", (
        "transition_to must not mutate current_phase on failure; "
        f"got {after['current_phase']!r}"
    )
    assert after["verification"]["status"] == "pending", (
        "transition_to must not mutate verification.status on failure; "
        f"got {after['verification']['status']!r}"
    )
    assert "verification_passed" not in after.get("completed_phases", []), (
        "transition_to must not silently append a milestone on failure; "
        f"got completed_phases={after.get('completed_phases', [])!r}"
    )


# ---------------------------------------------------------------------------
# Defensive sanity: the LEGAL edge the migration relies on still works.
# ---------------------------------------------------------------------------


def test_completed_to_verification_passed_still_legal(
    tmp_path,
    mock_plan_state_factory,
    plan_dir_writer,
):
    """The migration's only valid path (``completed -> verification_passed``)
    still works after the security tightening.

    This is a sanity check from the OTHER direction: if a future
    "hardening" tightened the state machine so far that even the legal
    ``completed -> verification_passed`` edge stopped working, the
    migration would silently break (it relies on that exact edge being
    legal). Pinning the positive case here means the security boundary
    cannot be tightened past the point the migration needs.

    The fixture seeds ``"execution"`` in ``completed_phases`` because
    ``transition_to`` refuses any ``verification*`` target unless that
    milestone is present.
    """
    state = mock_plan_state_factory(
        "pending",
        "completed",
        plan_id="legal-completed-to-verification-passed",
        flags={"arch_enabled": False, "test_enabled": False},
        completed_phases=["executing", "execution"],
    )
    plan_dir = plan_dir_writer(
        tmp_path, "legal-completed-to-verification-passed", state
    )
    ps = PlanState(plan_dir)

    # The transition must NOT raise.
    ps.transition_to("verification_passed")

    assert ps.get_current_phase() == "verification_passed"
    assert "completed" in ps.get_state()["completed_phases"]
    # verification.status is updated by transition_to on this edge.
    assert ps.get_state()["verification"]["status"] == "passed"


def test_verification_passed_to_completed_is_rejected(
    tmp_path,
    mock_plan_state_factory,
    plan_dir_writer,
):
    """Forward-direction edge ``verification_passed -> completed`` must be rejected.

    This is the second half of the boundary: even after a successful
    migration lands the plan in ``verification_passed``, it must NOT be
    possible to advance further into ``completed`` (the terminal). The
    whitelist does not declare that edge — it only declares
    ``completed -> verification_passed`` for the dead-end fix. A
    downstream caller that tries ``verification_passed -> completed`` is
    asking to make the plan "more done" than the workflow allows, which
    is a class of bug the security boundary exists to prevent.

    This test pins the negation of the dead-end fix: the legal pair
    (task 3) is ``completed -> verification_passed``, NOT its reverse.
    """
    state = mock_plan_state_factory(
        "passed",
        "verification_passed",
        plan_id="rejected-verification-passed-to-completed",
        flags={"arch_enabled": False, "test_enabled": False},
        completed_phases=[
            "executing",
            "execution",
            "completed",
            "verification_passed",
        ],
    )
    plan_dir = plan_dir_writer(
        tmp_path, "rejected-verification-passed-to-completed", state
    )
    ps = PlanState(plan_dir)

    # Defensive cross-check: the table genuinely does not declare this edge.
    # If a future refactor accidentally adds it, both this assertion and
    # the next one will fail, surfacing the contradiction loudly.
    assert "completed" not in VERIFICATION_PHASE_TRANSITIONS.get(
        "verification_passed", []
    ), (
        "the whitelist must not declare verification_passed -> completed; "
        "the migration's terminal IS verification_passed, not 'completed'"
    )

    with pytest.raises(ValueError) as exc_info:
        ps.transition_to("completed")

    msg = str(exc_info.value)
    assert "Illegal transition" in msg, (
        f"expected the transition-table guard to fire with an "
        f"'Illegal transition' message; got: {msg!r}"
    )
    # The terminal is "completed" — the assertion below is a defensive
    # check that the constants module exposes the expected terminal set.
    assert "completed" in TERMINAL_PHASES


# ---------------------------------------------------------------------------
# TDD spec: forged PASSED report on a `ready` plan is also rejected.
# ---------------------------------------------------------------------------


def test_ready_with_forged_report_cannot_skip_to_verification_passed(
    tmp_path,
    mock_plan_state_factory,
    mock_verification_report_factory,
    plan_dir_writer,
):
    """A forged PASSED report on a ``ready`` plan cannot bypass to
    ``verification_passed``.

    Distinct from the ``pending`` case in
    ``test_forged_report_cannot_bypass_state_machine``: ``ready`` is
    further along the workflow than ``pending``, but the plan still has
    not entered ``executing`` (so ``"execution"`` is NOT in
    ``completed_phases``). The transition must still be rejected on the
    same security grounds — there is no path from ``ready`` to
    ``verification_passed`` that bypasses ``executing``.
    """
    state = mock_plan_state_factory(
        "pending",
        "ready",
        plan_id="forged-bypass-ready",
        flags={"arch_enabled": False, "test_enabled": False},
        completed_phases=["prd_approved"],
    )
    forged_report = mock_verification_report_factory(
        "PASSED",
        plan_id="forged-bypass-ready",
    )
    plan_dir = plan_dir_writer(
        tmp_path, "forged-bypass-ready", state, report=forged_report
    )
    ps = PlanState(plan_dir)

    with pytest.raises(ValueError):
        ps.transition_to("verification_passed")

    import json
    after = json.loads((plan_dir / "plan_state.json").read_text())
    assert after["current_phase"] == "ready"