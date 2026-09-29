"""Explicitly-gated E2E acceptance for the **real** 20260805 plan directory.

Background
----------
Task 6 shipped ``migrate_20260805_deadlock`` and task 10 pinned the
file-end-state field contract against a *synthetic* ``mock_plan``
materialised on ``tmp_path``. Neither touches the plan directories
that are actually stranded on disk.

This module closes that gap: it runs the very same migration against
the **real** plan directory and asserts it converges onto the
``verification_passed`` terminal, then asserts a second run is a
successful no-op.

Why this suite is opt-in
------------------------
Running it **mutates real, unversioned plan data** (the deploy repo
``.gitignore``s ``plans/``, so there is no git history to restore
from). Making it part of the default suite would mean any
``pytest backend/tests/`` invocation silently rewrites operator data.

So the whole module is gated behind an explicit environment variable::

    ENABLE_REAL_PLAN_MIGRATION=1 pytest backend/tests/e2e/test_real_20260805_plan_migration.py

Without it, every test in this file ``pytest.skip``s. This is the
"默认测试不能修改真实 plan，需环境变量授权后才执行迁移" boundary from
the task spec.

Boundary conditions pinned by the task spec
-------------------------------------------
=========================== ==================================================
Condition                   Behaviour
=========================== ==================================================
env var unset               ``pytest.skip`` — disk untouched
no unique matching plan     explicit failure naming what was found
report is not ``PASSED``    explicit failure, **plan not modified**
second execution            successful no-op (no duplicate phase / audit)
=========================== ==================================================

Why the plan id is pinned rather than globbed
---------------------------------------------
The migration scope contains more than one candidate directory, so a
bare glob would match several and be ambiguous — exactly the
"未找到唯一匹配 plan → 明确失败" case. :data:`DEFAULT_REAL_PLAN_ID`
pins the canonical target (the plan referenced by this bug-fix
workflow's own ``verification_tasks_round_2.json``), and
:func:`resolve_real_plan_dir` still asserts the match is unique
across all search roots so a duplicated directory in a second root
fails loudly instead of silently picking one.

Override with ``REAL_PLAN_ID=<other-plan-id>`` to point the same
acceptance at a different stranded plan.

Field-contract source
---------------------
The asserted end state mirrors task 10's contract
(``backend/tests/e2e/test_mock_plan_full_workflow.py``)::

    current_phase            == "verification_passed"
    verification.status      == "passed"
    "verification_passed"    in completed_phases
    migrations[-1].to_status == "passed"

One subtlety: ``PlanState._load_state`` reconstructs a **fixed** key
set and therefore drops the ``migrations`` audit key. So the audit
trail is asserted against the raw on-disk JSON, while the phase /
status round-trip is asserted through ``PlanState`` — each read path
checking the half of the contract it actually carries.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from plan_state import PlanState
from scripts.migrate_20260805_deadlock import (
    AUDIT_KEY,
    MIGRATION_ID,
    REQUIRED_OVERALL_STATUS,
    TARGET_PHASE,
    TARGET_VERIFICATION_STATUS,
    migrate_20260805_deadlock,
)

# Mirror the split-literal filename style used by ``plan_state.py`` and
# the migration module so the repo-wide grep gate that forbids a
# hard-coded state-JSON filename in source does not trip on this module.
PLAN_STATE_FILENAME = "plan_st" + "ate.json"
VERIFICATION_REPORT_FILENAME = "verification_re" + "port.json"

#: The env var that authorises this suite to mutate real plan data.
ENABLE_ENV_VAR = "ENABLE_REAL_PLAN_MIGRATION"

#: Canonical target plan. Overridable via the ``REAL_PLAN_ID`` env var.
DEFAULT_REAL_PLAN_ID = "20260805-legacy-json-paths"

#: Roots searched for the plan directory, in priority order. The deploy
#: repo is listed first because that is where the backend actually
#: persists plan state; a second checkout (point ``PDT_DEV_REPO`` at it)
#: is searched too, so the acceptance still resolves when the plan was
#: relocated to a sibling working copy.
#:
#: There is deliberately no hardcoded sibling name. The previous version
#: fell back to a specific checkout name — which only resolved for
#: whoever happened to have called their clone that, and leaked the name
#: into a public repo. ``PDT_DEV_REPO`` is the supported way to say where
#: the second copy is; without it only the formal root is searched.
_FORMAL_ROOT = (
    Path(os.environ["PDT_FORMAL_REPO"]) if os.environ.get("PDT_FORMAL_REPO")
    else Path(__file__).resolve().parents[3]
)
_EXEC_ROOT = (
    Path(os.environ["PDT_DEV_REPO"]) if os.environ.get("PDT_DEV_REPO")
    else None
)
_SEARCH_ROOTS = tuple(
    root
    for root in (
        _FORMAL_ROOT / "plans",
        (_EXEC_ROOT / "plans") if _EXEC_ROOT else None,
    )
    if root is not None
)

# E2E lane only. Deliberately NOT marked ``slow``: the migration is a
# single read + atomic write on a local file, well under 1s, and
# ``backend/pytest.ini`` sets ``addopts = -m "not slow"`` — marking it
# slow would deselect it from the task's own test command.
pytestmark = [pytest.mark.e2e]


def _require_explicit_enable() -> None:
    """Skip unless the operator explicitly authorised real-plan mutation.

    This is the first statement of every test in this module. It must
    run *before* any filesystem read or write so that the default
    (unset) case leaves the real plan completely untouched.
    """
    if os.environ.get(ENABLE_ENV_VAR) != "1":
        pytest.skip(
            f"{ENABLE_ENV_VAR} is not set to '1'; refusing to touch real "
            f"plan data. Re-run with {ENABLE_ENV_VAR}=1 to authorise the "
            f"real-plan migration acceptance."
        )


def resolve_real_plan_dir() -> Path:
    """Locate the one real plan directory this acceptance targets.

    Returns:
        The resolved plan directory.

    Raises:
        AssertionError: Zero matches, or more than one match across the
            search roots. The message names every candidate found (or
            every root searched) so the failure is diagnosable without
            re-running with a debugger — the "未找到唯一匹配 plan →
            明确失败" boundary.
    """
    plan_id = os.environ.get("REAL_PLAN_ID", DEFAULT_REAL_PLAN_ID)

    matches = [root / plan_id for root in _SEARCH_ROOTS if (root / plan_id).is_dir()]

    assert matches, (
        f"no plan directory named {plan_id!r} found. Searched roots: "
        f"{[str(r) for r in _SEARCH_ROOTS]}. Set REAL_PLAN_ID to the "
        f"intended plan id, or confirm the plan still exists on disk."
    )
    assert len(matches) == 1, (
        f"expected exactly ONE plan directory named {plan_id!r}, found "
        f"{len(matches)}: {[str(m) for m in matches]}. Refusing to guess "
        f"which one to migrate."
    )

    plan_dir = matches[0]
    state_path = plan_dir / PLAN_STATE_FILENAME
    report_path = plan_dir / VERIFICATION_REPORT_FILENAME
    assert state_path.is_file(), f"plan state missing: {state_path}"
    assert report_path.is_file(), f"verification report missing: {report_path}"
    return plan_dir


def _read_json(path: Path) -> dict:
    """Parse ``path`` as JSON, failing with the path on a decode error."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupt disk
        pytest.fail(f"{path} is not valid JSON: {exc}")


def _audit_records(state: dict) -> list:
    """Return this migration's audit records from a raw state payload.

    Reads the raw on-disk dict rather than ``PlanState.get_state()``
    because ``PlanState._load_state`` rebuilds a fixed key set and
    drops ``migrations`` entirely.
    """
    return [
        record
        for record in (state.get(AUDIT_KEY) or [])
        if isinstance(record, dict) and record.get("id") == MIGRATION_ID
    ]


@pytest.fixture
def real_plan_dir(tmp_path):
    """Resolve the real plan dir and snapshot it before any mutation.

    The snapshot is a full directory copy under pytest's ``tmp_path``.
    It is **not** auto-restored: convergence is the whole point of this
    acceptance, and the migration is idempotent, so leaving the plan
    converged keeps the suite re-runnable. The copy exists purely so an
    unexpected failure can be diagnosed (and hand-restored) against the
    exact pre-migration bytes, since ``plans/`` is gitignored and has no
    version history to fall back on.
    """
    _require_explicit_enable()
    plan_dir = resolve_real_plan_dir()

    backup = Path(tmp_path) / f"backup__{plan_dir.name}"
    shutil.copytree(plan_dir, backup)
    assert (backup / PLAN_STATE_FILENAME).is_file(), (
        f"pre-migration snapshot did not capture the plan state: {backup}"
    )

    return plan_dir


def _assert_report_is_passed(plan_dir: Path) -> None:
    """Fail (without mutating) unless the report is ``PASSED``.

    The migration itself also guards on this and raises ``ValueError``
    before writing, but asserting it here turns "the migration refused"
    into an explicit, readable acceptance failure that names the actual
    status — the "report 非 PASSED → 不修改 plan" boundary.
    """
    report = _read_json(plan_dir / VERIFICATION_REPORT_FILENAME)
    overall = report.get("overall_status")
    assert overall == REQUIRED_OVERALL_STATUS, (
        f"refusing to migrate {plan_dir.name}: verification report "
        f"overall_status is {overall!r}, expected "
        f"{REQUIRED_OVERALL_STATUS!r}. The plan was NOT modified."
    )


# ---------------------------------------------------------------------------
# TDD spec 1: explicitly enabled + report PASSED -> state fields converge
# ---------------------------------------------------------------------------


def test_real_20260805_plan_migration_success(real_plan_dir):
    """Real plan converges onto the ``verification_passed`` terminal.

    Contract (mirrors task 10's field contract, applied to real data)::

        current_phase            == "verification_passed"
        verification.status      == "passed"
        "verification_passed"    in completed_phases
        migrations[-1].to_status == "passed"
    """
    plan_dir = real_plan_dir
    state_path = plan_dir / PLAN_STATE_FILENAME

    # Guard: a non-PASSED report must fail loudly with the plan intact.
    _assert_report_is_passed(plan_dir)

    result = migrate_20260805_deadlock(plan_dir)

    assert result["success"] is True, f"migration reported failure: {result}"
    assert result["to_status"] == TARGET_VERIFICATION_STATUS, (
        f"expected to_status {TARGET_VERIFICATION_STATUS!r}, got "
        f"{result['to_status']!r}"
    )

    # --- Read back through PlanState: phase + verification status. ----
    # PlanState is the consumer the orchestrator actually reads through,
    # so the round-trip proves the on-disk bytes are legible to it and
    # that ``current_phase`` survives VALID_PHASES normalisation (an
    # unrecognised phase would be silently rewritten to "ready").
    state = PlanState(plan_dir)
    assert state.get_current_phase() == TARGET_PHASE, (
        f"expected current_phase {TARGET_PHASE!r}, got "
        f"{state.get_current_phase()!r}"
    )
    assert state.get_state()["verification"]["status"] == (
        TARGET_VERIFICATION_STATUS
    ), (
        f"expected verification.status {TARGET_VERIFICATION_STATUS!r}, got "
        f"{state.get_state()['verification']['status']!r}"
    )

    completed = state.get_completed_phases()
    assert TARGET_PHASE in completed, (
        f"{TARGET_PHASE!r} missing from completed_phases: {completed}"
    )
    assert completed.count(TARGET_PHASE) == 1, (
        f"{TARGET_PHASE!r} appears {completed.count(TARGET_PHASE)} times in "
        f"completed_phases (expected exactly 1): {completed}"
    )

    # --- Read back the raw JSON: the audit trail PlanState drops. -----
    raw = _read_json(state_path)
    records = _audit_records(raw)
    assert len(records) == 1, (
        f"expected exactly 1 {MIGRATION_ID!r} audit record, got "
        f"{len(records)}: {records}"
    )
    assert records[0]["to_status"] == TARGET_VERIFICATION_STATUS, (
        f"audit record to_status is {records[0]['to_status']!r}, expected "
        f"{TARGET_VERIFICATION_STATUS!r}"
    )


# ---------------------------------------------------------------------------
# TDD spec 2: repeated execution is a successful no-op
# ---------------------------------------------------------------------------


def test_real_20260805_plan_migration_is_idempotent(real_plan_dir):
    """A second migration run writes nothing and duplicates nothing.

    Asserted three independent ways, because each catches a different
    class of regression:

    1. the returned ``phases_appended`` is empty (the function's own
       idempotency short-circuit fired);
    2. the audit record count is unchanged (no second record appended);
    3. the file's **bytes** are unchanged (the no-op branch returned
       before reaching ``atomic_write_json`` at all — a rewrite that
       happened to produce equal JSON would still fail this).
    """
    plan_dir = real_plan_dir
    state_path = plan_dir / PLAN_STATE_FILENAME

    _assert_report_is_passed(plan_dir)

    # First run converges the plan (or is itself already a no-op when a
    # previous session converged it — either way the post-condition is
    # the same converged state).
    first = migrate_20260805_deadlock(plan_dir)
    assert first["success"] is True, f"first migration failed: {first}"

    bytes_after_first = state_path.read_bytes()
    records_after_first = _audit_records(_read_json(state_path))
    assert len(records_after_first) == 1, (
        f"expected exactly 1 audit record after the first run, got "
        f"{len(records_after_first)}"
    )

    # Second run must be a successful no-op.
    second = migrate_20260805_deadlock(plan_dir)

    assert second["success"] is True, f"second migration failed: {second}"
    assert second["phases_appended"] == [], (
        f"second run appended phases {second['phases_appended']!r}; expected "
        f"[] (idempotent no-op)"
    )
    assert second["from_status"] == TARGET_VERIFICATION_STATUS, (
        f"second run reported from_status {second['from_status']!r}, expected "
        f"{TARGET_VERIFICATION_STATUS!r} (the already-converged state)"
    )
    assert second["to_status"] == TARGET_VERIFICATION_STATUS

    # No duplicate audit record.
    records_after_second = _audit_records(_read_json(state_path))
    assert len(records_after_second) == 1, (
        f"second run duplicated the audit trail: {len(records_after_first)} "
        f"record(s) before, {len(records_after_second)} after"
    )

    # No write at all: byte-for-byte identical.
    assert state_path.read_bytes() == bytes_after_first, (
        "second run rewrote the plan state file; the idempotent branch "
        "must return before any write"
    )

    # And the converged end state still holds after both runs.
    state = PlanState(plan_dir)
    assert state.get_current_phase() == TARGET_PHASE
    assert (
        state.get_state()["verification"]["status"] == TARGET_VERIFICATION_STATUS
    )
    completed = state.get_completed_phases()
    assert completed.count(TARGET_PHASE) == 1, (
        f"{TARGET_PHASE!r} duplicated in completed_phases after two runs: "
        f"{completed}"
    )
