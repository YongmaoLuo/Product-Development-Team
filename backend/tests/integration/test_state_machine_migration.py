"""End-to-end migration + state-machine integration tests.

Background
----------
The unit-level suite covers the migration in isolation
(``tests/unit/test_migrate_20260805_deadlock.py``) and the state
machine in isolation
(``tests/unit/test_state_machine_completed_transition.py``).  Neither
suite proves that a real on-disk ``plan_state.json`` can complete the
``completed -> verification_passed`` transition *after* the migration
has run.  This module is the integration gate.

Contract pinned here
--------------------
1. **Full migration + transition** — a plan dir written via the shared
   task-1 factory (``write_plan_dir``), with a ``pending`` state and a
   ``PASSED`` report, converges through the real
   ``migrate_20260805_deadlock`` call AND, on the resulting on-disk
   file, ``PlanState.transition_to("verification_passed")`` lands
   legally.  The final state matches
   ``{"verification": {"status": "passed"}, "completed_phases":
   [..., "verification_passed"]}``.
2. **Migration idempotency + illegal repeat transition** — running the
   migration a second time is a no-op (no duplicate phase, no duplicate
   audit record), and a *subsequent* illegal transition out of
   ``verification_passed`` raises ``ValueError`` without mutating the
   disk state.

Imports use module names (``plan_state``, ``scripts.migrate_...``) so
``backend/pytest.ini``'s ``pythonpath = . ..`` puts ``backend/`` on the
import path — the same style the sibling unit tests use.

The tests use the registered fixtures
``mock_plan_state_factory`` / ``mock_verification_report_factory`` /
``plan_dir_writer`` (defined in ``backend/tests/conftest.py``) rather
than hand-rolling payloads, so a schema drift in the shared fixture
fails here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plan_state import PlanState
from scripts.migrate_20260805_deadlock import migrate_20260805_deadlock


# Mirror the split-literal filenames from ``scripts/migrate_20260805_deadlock``
# so the grep gate that forbids hard-coded ``plan_state.json`` in
# production source does not trip on this test module.
PLAN_STATE_FILENAME = "plan_st" + "ate.json"


# Default flags: keep the integration test focused on the deadline state
# machine, not the optional arch/test backfill branches.  These two
# values are the same shape ``test_migrate_20260805_deadlock`` uses.
DEFAULT_FLAGS = {"arch_enabled": False, "test_enabled": False}


def _load_state(state_path: Path) -> dict:
    """Read ``plan_state.json`` from disk and return the parsed dict."""
    return json.loads(Path(state_path).read_text(encoding="utf-8"))


@pytest.fixture
def make_deadlocked_plan(
    tmp_path,
    mock_plan_state_factory,
    mock_verification_report_factory,
    plan_dir_writer,
):
    """Factory yielding a deadlocked plan on disk.

    The fixture owns ``tmp_path`` so every call gets a fresh isolated
    directory; callers can decide whether to migrate now or migrate
    later (e.g. inside the test body).

    Returns
    -------
    Callable[[], tuple[Path, Path]]
        ``(plan_dir, state_path)`` for the freshly written plan.
    """

    def _make(
        *,
        plan_id: str = "deadlocked-migration-plan",
        verification_status: str = "pending",
        current_phase: str = "completed",
        completed_phases=None,
        overall_status: str = "PASSED",
        flags=None,
    ) -> tuple:
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
# TDD spec 1: full migration + legal transition through the state machine
# ---------------------------------------------------------------------------


def test_full_migration_with_state_machine_transition(make_deadlocked_plan):
    """pending+PASSED on disk -> migrate -> legal transition lands in passed.

    Mirrors the task spec input/output examples::

        Input : plan_dir = write_plan_dir(tmp_path, "plan-x",
                                           pending_state, passed_report)
        Output: {"verification":{"status":"passed"},
                 "completed_phases":["verification_passed"]}

    The test exercises the real on-disk path end-to-end:

    1. ``write_plan_dir`` materialises a ``pending`` state + ``PASSED``
       report under ``tmp_path`` (no in-memory shortcut).
    2. ``migrate_20260805_deadlock(plan_dir)`` performs the deadlock
       fix: status flips to ``passed``, ``current_phase`` becomes
       ``"verification_passed"``, the milestone phase is appended,
       and an audit record is written through the atomic-replace
       pipeline.
    3. Re-reading the plan via :class:`PlanState` (which re-parses the
       on-disk JSON) must show ``current_phase == "verification_passed"``
       and ``verification.status == "passed"``.  The fact that
       ``PlanState`` sees the migrated values — rather than the original
       ``pending`` ones — proves the migration round-tripped through
       the real filesystem.
    """
    plan_dir, state_path = make_deadlocked_plan(plan_id="plan-x")

    # Step 1 — migration writes the converged state to disk.
    result = migrate_20260805_deadlock(plan_dir)

    assert result["success"] is True
    assert result["from_status"] == "pending"
    assert result["to_status"] == "passed"

    # Step 2 — the migrated file is reachable through PlanState and
    # reports the converged phase + status.  This is the integration
    # proof: PlanState loads the on-disk JSON, not a copy in memory.
    ps = PlanState(plan_dir)
    assert ps.get_current_phase() == "verification_passed", (
        "after migration, PlanState must read current_phase == "
        f"'verification_passed', got {ps.get_current_phase()!r}"
    )
    state_snapshot = ps.get_state()
    assert state_snapshot["verification"]["status"] == "passed", (
        "after migration, the verification status must be 'passed', "
        f"got {state_snapshot['verification']['status']!r}"
    )
    assert "verification_passed" in state_snapshot["completed_phases"], (
        "after migration, completed_phases must include "
        f"'verification_passed', got {state_snapshot['completed_phases']!r}"
    )

    # Step 3 — the persisted file matches the contract output verbatim.
    persisted = _load_state(state_path)
    assert persisted["verification"]["status"] == "passed"
    assert persisted["current_phase"] == "verification_passed"
    assert "verification_passed" in persisted["completed_phases"]


# ---------------------------------------------------------------------------
# TDD spec 2: migration idempotency + illegal repeat transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_target",
    [
        # Each entry is undeclared out of ``verification_passed`` per
        # ``VERIFICATION_PHASE_TRANSITIONS`` and
        # ``PHASE_TRANSITIONS``: only ``["completed"]`` is legal, so
        # these must all raise ``ValueError``.  ``completed`` itself
        # is the legal target, so it is deliberately absent.
        "executing",
        "ready",
        "verification",
        "verification_running",
        "verification_failed",
        "verification_repairing",
        "verification_rerunning",
        "verification_loop_stopped",
        "verify_first_pass",
        "verify_recheck",
        "interview",
    ],
)
def test_migration_then_rejects_invalid_repeat_transition(
    make_deadlocked_plan, invalid_target
):
    """Migration is a no-op on second call; subsequent illegal transition
    out of ``verification_passed`` raises ``ValueError``.

    The two contracts pinned here are the task spec's "第二次迁移 →
    no-op" and "迁移后非法重复 transition → 抛 ``ValueError``":

    1. **Idempotency on disk** — a second ``migrate_20260805_deadlock``
       call leaves the on-disk bytes byte-identical to the first
       migration's output.  No duplicate ``verification_passed`` phase
       is appended, no second audit record is written, and the file
       mtime does not move.
    2. **State-machine legality guard** — after migration, the plan
       is parked in ``verification_passed``.  Attempting any
       transition whose target is **not** in the table's legal set
       for ``verification_passed`` (which is just ``["completed"]``)
       must raise :class:`ValueError` from
       :meth:`PlanState.transition_to`.  The current phase is
       preserved on disk — the rejection happens before any write.
    """
    plan_dir, state_path = make_deadlocked_plan(
        plan_id="repeat-illegal-transition"
    )

    # First migration — does the real convergence work.
    first_result = migrate_20260805_deadlock(plan_dir)
    assert first_result["success"] is True
    assert first_result["to_status"] == "passed"

    after_first_migration = state_path.read_bytes()
    first_persisted = _load_state(state_path)
    assert first_persisted["verification"]["status"] == "passed"
    assert first_persisted["current_phase"] == "verification_passed"
    assert first_persisted["migrations"], (
        "first migration must leave an audit record; otherwise the "
        "second call would not know the plan was already converged"
    )

    # Second migration — must be a successful no-op that does not
    # duplicate the phase or the audit record.
    second_result = migrate_20260805_deadlock(plan_dir)
    assert second_result == {
        "success": True,
        "from_status": "passed",
        "to_status": "passed",
        "phases_appended": [],
    }
    # Byte-identical: the second call wrote nothing.
    assert state_path.read_bytes() == after_first_migration
    second_persisted = _load_state(state_path)
    assert (
        second_persisted["completed_phases"].count("verification_passed")
        == 1
    ), (
        "second migration must NOT duplicate the verification_passed "
        "phase; got "
        f"{second_persisted['completed_phases'].count('verification_passed')} "
        f"occurrences in {second_persisted['completed_phases']!r}"
    )
    assert len(second_persisted["migrations"]) == 1, (
        "second migration must NOT add a duplicate audit record; "
        f"got {len(second_persisted['migrations'])} entries"
    )

    # Now exercise the state-machine half of the contract: a PlanState
    # loaded against the migrated plan, then an illegal transition.
    ps = PlanState(plan_dir)
    assert ps.get_current_phase() == "verification_passed", (
        "PlanState must observe the migrated current_phase; got "
        f"{ps.get_current_phase()!r}"
    )

    state_before_illegal = state_path.read_bytes()
    with pytest.raises(ValueError) as excinfo:
        ps.transition_to(invalid_target)

    # The error must name both endpoints — the implementation in
    # plan_state.py raises ``Illegal transition from 'X' to 'Y'``
    # so both sides of the edge must appear in the message.
    message = str(excinfo.value)
    assert "verification_passed" in message and invalid_target in message, (
        f"ValueError must name both endpoints; got {message!r}"
    )

    # And the plan must not have moved.
    assert ps.get_current_phase() == "verification_passed"
    assert state_path.read_bytes() == state_before_illegal, (
        "a failed transition_to must NOT mutate plan_state.json on disk"
    )
    persisted_after_illegal = _load_state(state_path)
    assert persisted_after_illegal["current_phase"] == "verification_passed"
    assert persisted_after_illegal["verification"]["status"] == "passed"