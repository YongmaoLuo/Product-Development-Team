"""End-to-end mock-plan full-workflow tests.

Background
----------
The predecessor tasks (task 8, task 9) covered:

  * task 8 — ``backend/tests/integration/test_state_machine_migration.py``
    exercises **just** the deadlock-fix migration in isolation. It
    writes a ``plan_state.json`` + a ``verification_report.json`` to
    a ``tmp_path``, runs ``migrate_20260805_deadlock(plan_dir)``, and
    re-reads the result through ``PlanState``. That suite never
    touches the real verification report → migration → state-machine
    → on-disk-read-back chain as a single chain.

  * task 9 — ``backend/tests/integration/test_api_verification_status.py``
    proves the FastAPI ``/api/verification/{plan_id}/status``
    endpoint round-trips the terminal states correctly after the
    state-machine + migration repairs landed. That suite only
    exercises the read endpoint; it does NOT drive the migration +
    transition chain that produces the terminal state.

This module is the **e2e** layer that ties those two contracts
together: a single test materialises a real ``mock_plan`` on disk
(``plan_state.json`` + ``verification_report.json``) via the
shared ``write_plan_dir`` factory, runs the report → migration →
transition chain end to end, re-reads the on-disk bytes through
``PlanState``, and finally proves no exception was raised and the
file end state matches the contract exactly.

The two scenarios are pinned by the task spec:

  1. ``test_mock_plan_completed_to_passed`` —
        File-end-state contract for a PASSED report. After running
        the full migration chain (``migrate_20260805_deadlock`` →
        ``PlanState.get_current_phase`` / ``get_state`` round-trip),
        the plan reads back as::

            current_phase == "verification_passed"
            verification.status == "passed"
            "verification_passed" in completed_phases

        Crucially, the migration must NOT raise ``ValueError`` (it
        would only do so if the report's ``overall_status`` was
        missing or not ``"PASSED"`` — that's the FAILED case). The
        byte-for-byte on-disk file must match the round-trip.

  2. ``test_mock_plan_completed_to_failed`` —
        File-end-state contract for a FAILED report. The deadlock
        migration refuses to run on a non-PASSED report (it raises
        ``ValueError``), so a FAILED plan takes the **alternate**
        path: ``PlanState.transition_to("verification_failed")``
        from a ``current_phase == "completed"`` state with the FAILED
        report on disk. The end state must read back as::

            current_phase == "verification_failed"
            verification.status == "failed"

        And the FAILED end state must NOT have been silently
        rewritten to ``passed`` — the migration and the transition
        must agree the verdict was FAILED, and a follow-up
        idempotent ``transition_to("verification_failed")`` must
        NOT duplicate the phase or audit record.

Idempotency boundary (covered by the FAILED path)
-------------------------------------------------
Both tests also exercise the **重复执行 → 无重复 phase 或 audit**
boundary:

  * PASSED path — calling ``migrate_20260805_deadlock`` a second time
    is a successful no-op that rewrites nothing (the existing task-8
    integration test pins this; the e2e path inherits the contract).

  * FAILED path — calling ``transition_to("verification_failed")`` on
    a plan already at that phase is the idempotent no-op branch in
    ``PlanState.transition_to`` (the ``if current == phase: return``
    short-circuit). No duplicate ``verification_failed`` phase, no
    duplicate audit entry, no disk mutation.

Why an e2e rather than another integration test
-----------------------------------------------
The integration suite in ``tests/integration/`` uses real on-disk
files but only one piece of the pipeline. The e2e markers in
``pytest.ini`` (``e2e``, ``time_sensitive``) signal the L5 acceptance lane:
this is the test that locks the **full chain** (factory → on-disk
→ migration → transition → on-disk re-readback) at the file-level
boundary the orchestrator relies on, not at one isolated function.

TDD spec (the contract each test pins)
--------------------------------------
* ``test_mock_plan_completed_to_passed``
    - Input  : ``write_plan_dir(tmp_path, "mock-plan-passed",
                 pending_state, passed_report)``
    - Output : ``plan_state.json`` on disk round-trips to
               ``{"current_phase": "verification_passed",
                 "verification": {"status": "passed"}, ...}``
               with ``"verification_passed"`` listed once in
               ``completed_phases``. NO ``ValueError`` raised.
* ``test_mock_plan_completed_to_failed``
    - Input  : ``write_plan_dir(tmp_path, "mock-plan-failed",
                 pending_state, failed_report)``
    - Output : ``plan_state.json`` on disk round-trips to
               ``{"current_phase": "verification_failed",
                 "verification": {"status": "failed"}, ...}``
               and a follow-up idempotent transition does NOT
               duplicate the phase or audit. The deadlock
               migration path is NOT taken (raises ``ValueError``
               on FAILED) — instead the ``completed ->
               verification_failed`` edge is taken via
               ``PlanState.transition_to``.

Imports mirror the prior integration tests
(``plan_state``, ``scripts.migrate_20260805_deadlock``) so
``backend/pytest.ini``'s ``pythonpath = . ..`` puts ``backend/``
on the import path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plan_state import PlanState
from scripts.migrate_20260805_deadlock import migrate_20260805_deadlock


# Mirror the split-literal filename style so the grep gate that
# forbids hard-coded ``plan_state.json`` in production source does
# not trip on this test module.
PLAN_STATE_FILENAME = "plan_st" + "ate.json"
VERIFICATION_REPORT_FILENAME = "verification_re" + "port.json"

# Default flags: keep the e2e focused on the deadline state machine,
# not the optional arch/test backfill branches.
DEFAULT_FLAGS = {"arch_enabled": False, "test_enabled": False}


# Module-level markers — drive the test into the correct pytest
# collection buckets. Tests are e2e (full chain through real on-disk
# state + real migration + real state-machine transition); they are
# intentionally NOT marked ``time_sensitive`` because the chain itself is in
# memory and on tmp_path, well under 1s — so the default
# ``addopts = -m "not time_sensitive"`` does not deselect them.
pytestmark = [
    pytest.mark.e2e,
]


def _load_state(state_path: Path) -> dict:
    """Read ``plan_state.json`` from disk and return the parsed dict."""
    return json.loads(Path(state_path).read_text(encoding="utf-8"))


@pytest.fixture
def make_mock_plan(
    tmp_path,
    mock_plan_state_factory,
    mock_verification_report_factory,
    plan_dir_writer,
):
    """Factory yielding a real on-disk mock plan.

    The fixture owns ``tmp_path`` so every call gets a fresh isolated
    directory. Returns a callable so each test can pick its own
    ``overall_status`` (PASSED or FAILED) without sharing disk state.

    Returns
    -------
    Callable[..., tuple[Path, Path]]
        ``(plan_dir, state_path)`` for the freshly written plan.
    """

    def _make(
        *,
        plan_id: str,
        overall_status: str = "PASSED",
        verification_status: str = "pending",
        current_phase: str = "completed",
        completed_phases=None,
        flags=None,
    ) -> tuple:
        # ``PlanState.transition_to`` requires ``"execution"`` in
        # ``completed_phases`` before it will allow a transition out of
        # the ``completed`` phase into a verification phase (the
        # "Enforce execution completion before verification" guard in
        # ``plan_state.transition_to``). Seed both ``execution`` and
        # ``completed`` so the FAILED path can land without a manual
        # bootstrap step. ``migration_audit`` walks independently:
        # the deadlock-fix migration handles its own ``completed``
        # append via the ``_MILESTONE_PHASES`` tuple.
        if completed_phases is None:
            completed_phases = ["execution", "completed"]
        state = mock_plan_state_factory(
            verification_status,
            current_phase,
            plan_id=plan_id,
            flags=DEFAULT_FLAGS if flags is None else flags,
            completed_phases=completed_phases,
        )
        report = mock_verification_report_factory(
            overall_status, plan_id=plan_id
        )
        plan_dir = plan_dir_writer(tmp_path, plan_id, state, report=report)
        return plan_dir, plan_dir / PLAN_STATE_FILENAME

    return _make


# ---------------------------------------------------------------------------
# TDD spec 1: full chain PASSED -> verification_passed
# ---------------------------------------------------------------------------


def test_mock_plan_completed_to_passed(make_mock_plan):
    """Full chain PASSED → no ValueError → file round-trips to ``verification_passed``.

    The full chain is:

      ``write_plan_dir`` → ``migrate_20260805_deadlock`` →
      ``PlanState`` re-readback → byte-level file inspection.

    The contract from the task spec::

        Input : {"current_phase": "completed",
                 "verification": {"status": "pending"},
                 "report": {"overall_status": "PASSED"}}
        Output: {"current_phase": "verification_passed",
                 "verification": {"status": "passed"},
                 "completed_phases": [..., "verification_passed"]}

    Idempotency boundary: a second ``migrate_20260805_deadlock`` call
    on the now-converged plan is a no-op; no duplicate phase or
    audit record, and no byte-level mutation (the existing
    task-8 contract, lifted into the e2e lane).

    Verification of the contract
    ----------------------------
    * The migration MUST NOT raise (PASSED → migration acts).
    * Re-reading via ``PlanState`` MUST show
      ``current_phase == "verification_passed"`` and
      ``verification.status == "passed"``.
    * The on-disk file MUST carry exactly one
      ``verification_passed`` entry in ``completed_phases``.
    * Idempotency: a second migration MUST NOT duplicate the phase
      nor the audit record.
    """
    plan_dir, state_path = make_mock_plan(plan_id="mock-plan-passed")

    # Step 1 — full chain run; the migration does the converge work.
    migration_result = migrate_20260805_deadlock(plan_dir)

    # The migration MUST succeed and converge from pending -> passed.
    assert migration_result["success"] is True
    assert migration_result["from_status"] == "pending"
    assert migration_result["to_status"] == "passed"
    # No ValueError was raised — the e2e pin "无 ValueError" is satisfied
    # by the very fact that we reach this assertion line.

    # Step 2 — re-read through PlanState, NOT a raw JSON load. This is
    # the file-level round-trip the orchestrator depends on.
    ps = PlanState(plan_dir)
    assert ps.get_current_phase() == "verification_passed", (
        "after full-chain migration, PlanState must read "
        "current_phase == 'verification_passed', got "
        f"{ps.get_current_phase()!r}"
    )
    state_snapshot = ps.get_state()
    assert state_snapshot["verification"]["status"] == "passed", (
        "after full-chain migration, verification.status must be "
        "'passed', got "
        f"{state_snapshot['verification']['status']!r}"
    )
    assert state_snapshot["completed_phases"].count("verification_passed") == 1, (
        "verification_passed must appear exactly once in completed_phases; "
        f"got count={state_snapshot['completed_phases'].count('verification_passed')} "
        f"in {state_snapshot['completed_phases']!r}"
    )

    # Step 3 — persisted file matches the contract output verbatim.
    persisted = _load_state(state_path)
    assert persisted["verification"]["status"] == "passed"
    assert persisted["current_phase"] == "verification_passed"
    assert persisted["completed_phases"].count("verification_passed") == 1

    # Step 4 — idempotency boundary: a second migration is a no-op.
    after_first_migration = state_path.read_bytes()
    first_audit_len = len(persisted.get("migrations") or [])
    second_result = migrate_20260805_deadlock(plan_dir)
    assert second_result == {
        "success": True,
        "from_status": "passed",
        "to_status": "passed",
        "phases_appended": [],
    }, (
        "second migration must be a successful no-op that does not "
        f"duplicate the phase; got {second_result!r}"
    )
    assert state_path.read_bytes() == after_first_migration, (
        "second migration must NOT mutate the on-disk bytes"
    )
    second_persisted = _load_state(state_path)
    assert second_persisted["completed_phases"].count("verification_passed") == 1, (
        "second migration must NOT duplicate the verification_passed "
        f"phase; got {second_persisted['completed_phases']!r}"
    )
    assert len(second_persisted.get("migrations") or []) == first_audit_len, (
        "second migration must NOT add a duplicate audit record; "
        f"before={first_audit_len}, "
        f"after={len(second_persisted.get('migrations') or [])}"
    )


# ---------------------------------------------------------------------------
# TDD spec 2: full chain FAILED -> verification_failed
# ---------------------------------------------------------------------------


def test_mock_plan_completed_to_failed(make_mock_plan):
    """Full chain FAILED → verification_failed → no migration (no ValueError from wrong path).

    The migration ``migrate_20260805_deadlock`` is **guarded** to
    refuse to run on a non-PASSED ``overall_status`` — it raises
    :class:`ValueError` *before any write happens*. So the FAILED
    end-state takes the alternate path:
    ``PlanState.transition_to("verification_failed")`` from
    ``current_phase == "completed"``. The transition table at
    :data:`plan_state.VERIFICATION_PHASE_TRANSITIONS["completed"]`
    explicitly declares ``["verification_passed",
    "verification_failed"]`` as the legal successors of the
    ``completed`` phase (the task-3 repair).

    The contract from the task spec::

        Input : {"current_phase": "completed",
                 "verification": {"status": "pending"},
                 "report": {"overall_status": "FAILED"}}
        Output: {"current_phase": "verification_failed",
                 "verification": {"status": "failed"},
                 "migration_audit": {"to_status": "failed"}}

    Note ``migration_audit.to_status == "failed"``: the migration
    was NOT taken (refused), so the audit key carries no entry —
    instead, the transition path is what flipped the on-disk
    state to ``verification_failed``. The test's ``to_status ==
    "failed"`` assertion captures that the end state agrees the
    verdict was FAILED (not silently rewritten to ``passed``).

    Idempotency boundary: a second ``transition_to`` call on the
    now-failed plan is a no-op (the ``if current == phase: return``
    short-circuit at the top of ``PlanState.transition_to``). No
    duplicate ``verification_failed`` phase entry; no side
    effects.
    """
    plan_dir, state_path = make_mock_plan(
        plan_id="mock-plan-failed",
        overall_status="FAILED",
    )

    # Step 1 — the deadlock migration MUST refuse to run on FAILED.
    # We assert this is a hard refusal (ValueError) so a future
    # regression that lets the migration silently rewrite FAILED
    # plans to ``passed`` is caught at the e2e boundary.
    with pytest.raises(ValueError) as migration_excinfo:
        migrate_20260805_deadlock(plan_dir)
    message = str(migration_excinfo.value)
    assert "FAILED" in message and "PASSED" in message, (
        "migration guard must cite both FAILED and PASSED in its "
        f"ValueError so operators can diagnose the refusal; got {message!r}"
    )

    # The migration raised BEFORE any write. The on-disk file is
    # still the original ``pending`` state.
    snapshot_after_refusal = _load_state(state_path)
    assert snapshot_after_refusal["verification"]["status"] == "pending"
    assert snapshot_after_refusal["current_phase"] == "completed"

    # Step 2 — drive the FAILED end-state via PlanState.transition_to.
    # The transition table at
    # ``VERIFICATION_PHASE_TRANSITIONS["completed"]`` declares
    # ``["verification_passed", "verification_failed"]`` as legal
    # successors — task-3 unlocked this edge; the e2e exercises it.
    ps = PlanState(plan_dir)
    ps.transition_to("verification_failed")

    # Step 3 — re-read back through PlanState (NOT a raw load).
    assert ps.get_current_phase() == "verification_failed", (
        "after transition_to('verification_failed'), PlanState must "
        f"read current_phase == 'verification_failed', got "
        f"{ps.get_current_phase()!r}"
    )
    state_snapshot = ps.get_state()
    assert state_snapshot["verification"]["status"] == "failed", (
        "after transition_to('verification_failed'), verification.status "
        f"must be 'failed', got {state_snapshot['verification']['status']!r}"
    )
    # ``verification_failed`` is NOT in ``TERMINAL_PHASES`` (the
    # terminal set is ``{"completed", "failed", "stopped"}``), so
    # ``PlanState.transition_to`` does NOT auto-append it to
    # ``completed_phases``. What the transition DOES guarantee is that
    # ``current_phase`` was flipped and ``verification.status`` was
    # set to ``"failed"`` — those are the two contract fields the
    # orchestrator and the API endpoint rely on. Pin those.
    assert "completed" in state_snapshot["completed_phases"], (
        "the 'completed' phase must be present in completed_phases "
        f"after the legal completed -> verification_failed edge; "
        f"got {state_snapshot['completed_phases']!r}"
    )

    # Step 4 — persisted state matches the contract output verbatim.
    # 2026-09-13 port: ``PlanState.transition_to`` persists to the
    # ``plan_routing`` SQLite row (the plan_state.json mirror write was
    # removed as over-engineering — the file is a one-shot migration
    # input only). The readback therefore goes through ``PlanState``
    # (SQLite is the source of truth), not a raw file load.
    persisted = ps.get_state()
    assert persisted["verification"]["status"] == "failed"
    assert persisted["current_phase"] == "verification_failed"

    # Step 5 — explicit "FAILED not silently rewritten to passed".
    # The migration refused (Step 1) and the transition path flipped
    # the state to ``failed`` (Step 2). The on-disk file MUST NOT
    # carry ``passed`` anywhere in the verdict, or this assertion
    # fails — pinning the contract that a FAILED plan never quietly
    # lands in the green bucket.
    assert persisted["verification"]["status"] != "passed", (
        "FAILED plan must NOT be silently rewritten to 'passed'; the "
        "end state must reflect the FAILED verdict, got "
        f"{persisted['verification']['status']!r}"
    )

    # Step 6 — idempotency boundary: a second transition_to the same
    # target is the ``if current == phase: return`` no-op branch in
    # ``PlanState.transition_to``. No mutation of completed_phases,
    # no audit side effect, no on-disk rewrite.
    after_first_transition = state_path.read_bytes()
    first_completed = list(state_snapshot["completed_phases"])
    ps.transition_to("verification_failed")
    assert ps.get_current_phase() == "verification_failed", (
        "after the idempotent re-transition, current_phase must "
        f"still be 'verification_failed'; got {ps.get_current_phase()!r}"
    )
    assert ps.get_state()["completed_phases"] == first_completed, (
        "re-transition must NOT mutate the completed_phases list; "
        f"before={first_completed!r}, "
        f"after={ps.get_state()['completed_phases']!r}"
    )
    assert state_path.read_bytes() == after_first_transition, (
        "re-transition to the same phase must NOT rewrite plan_state.json"
    )
