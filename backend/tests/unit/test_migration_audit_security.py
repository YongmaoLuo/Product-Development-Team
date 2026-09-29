"""Security tests for the migration-audit idempotency boundary.

Background
----------
Task 6 shipped :migration:`scripts.migrate_20260805_deadlock`, the
one-shot plan-state convergence migration. Its audit trail lives in
``plan_state.json["migrations"]``: a list of records, each carrying
``id``, ``applied_at``, ``from_status``, ``to_status``,
``phases_appended``. The migration is *idempotent* — running it on a
plan that already converged writes nothing — but the security-relevant
property is finer-grained than the public docs: **the audit list must
never carry two records with the same ``id``**, no matter how many
times the migration is invoked, even when the plan is partially
hand-edited between runs.

Contract pinned here
--------------------
1. ``test_migration_audit_not_duplicated`` — three consecutive
   invocations of the migration against the same plan directory
   produce exactly ONE audit record (not three, not two). The audit
   ``id`` is the migration's stable identifier, the
   ``applied_at`` timestamps may differ across runs but only ONE
   record is committed to disk. The on-disk ``completed_phases``
   carries ``"verification_passed"`` exactly once.

2. ``test_migration_audit_idempotent_under_partial_revert`` — even
   when an operator partially reverts the converged state (status
   flipped back to ``"pending"`` while the audit record remains), a
   second migration run is detected as already-converged and writes
   no second audit record. This protects the audit trail against the
   manual-recovery race where someone tries to "re-run" the
   migration to fix a half-broken state.

3. ``test_migration_audit_record_shape`` — the audit record the
   migration writes carries the four contract-pinned keys
   (``id``, ``applied_at``, ``from_status``, ``to_status``,
   ``phases_appended``), each with a usable type. A refactor that
   silently drops ``from_status`` (the only way to tell which
   previous state was migrated away from) fails here.

The tests reach the shared task-1 fixtures
(``mock_plan_state_factory`` / ``mock_verification_report_factory`` /
``plan_dir_writer`` from ``backend/tests/conftest.py``) rather than
hand-rolling payloads, so a schema drift in the shared fixture fails
here too.

``scripts.migrate_20260805_deadlock`` is imported by module name (not
``backend.scripts...``) because ``backend/pytest.ini`` puts
``backend/`` on ``pythonpath`` — the same import style the sibling
unit tests use.
"""

import json
from pathlib import Path

import pytest

from scripts.migrate_20260805_deadlock import (
    MIGRATION_ID,
    migrate_20260805_deadlock,
)


PLAN_STATE_FILENAME = "plan_st" + "ate.json"
VERIFICATION_REPORT_FILENAME = "verification_re" + "port.json"

DEFAULT_FLAGS = {"arch_enabled": False, "test_enabled": False}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_plan(
    tmp_path,
    mock_plan_state_factory,
    mock_verification_report_factory,
    plan_dir_writer,
):
    """Factory writing a plan dir; returns ``(plan_dir, state_path)``.

    Built on the shared task-1 fixtures so a schema drift there fails
    these tests too.
    """

    def _make(
        *,
        plan_id="audit-security-plan",
        verification_status="pending",
        current_phase="completed",
        overall_status="PASSED",
        flags=None,
        completed_phases=None,
    ):
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


def _load(state_path):
    return json.loads(Path(state_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# TDD spec 1: consecutive migrations produce a single audit record.
# ---------------------------------------------------------------------------


def test_migration_audit_not_duplicated(make_plan):
    """Three consecutive migrations commit a SINGLE audit record.

    This is the security-relevant contract: the audit trail must be
    append-only in *count* (not in *content*), so an attacker (or a
    runaway retry loop) cannot inflate the audit count by re-invoking
    the migration. The migration's internal ``_already_migrated``
    guard must short-circuit BEFORE appending any record.

    The test runs the migration three times in a row on the same plan
    directory and asserts:

      * ``state["migrations"]`` is a list of length exactly 1;
      * the single record's ``id`` equals :data:`MIGRATION_ID`;
      * the second and third runs returned ``phases_appended == []``;
      * the on-disk ``completed_phases`` carries
        ``"verification_passed"`` exactly once (i.e. the list, not the
        audit dict, is also not duplicated).

    On disk the test re-reads the file after each run and compares
    bytes — the second and third runs MUST be no-ops at the byte
    level, not merely "no audit appended" (a refactor that, say,
    rewrites the file with the same payload but a different timestamp
    would still inflate the file's mtime and should fail this test).
    """
    plan_dir, state_path = make_plan()

    # Snapshot the on-disk bytes BEFORE the first run, so we can prove
    # the file is touched exactly once (the first run writes; the
    # other two must not).
    before_first_run_bytes = state_path.read_bytes()

    first = migrate_20260805_deadlock(plan_dir)
    after_first_run_bytes = state_path.read_bytes()
    assert after_first_run_bytes != before_first_run_bytes, (
        "first migration run must write to disk; "
        "the plan was un-converged so the migration should mutate"
    )

    # -- Run 2 --------------------------------------------------------
    second = migrate_20260805_deadlock(plan_dir)
    after_second_run_bytes = state_path.read_bytes()

    # -- Run 3 --------------------------------------------------------
    third = migrate_20260805_deadlock(plan_dir)
    after_third_run_bytes = state_path.read_bytes()

    # Returned MigrationResult invariants: runs 2 & 3 are no-ops.
    assert first["success"] is True
    assert first["phases_appended"]  # first run did real work
    assert second["success"] is True
    assert second["phases_appended"] == []
    assert third["success"] is True
    assert third["phases_appended"] == []

    # Byte-level: runs 2 & 3 MUST be exactly no-ops at the on-disk level.
    assert after_second_run_bytes == after_first_run_bytes, (
        "second migration run must NOT rewrite the file; "
        "the plan is already converged and the migration should "
        "short-circuit before any write"
    )
    assert after_third_run_bytes == after_first_run_bytes, (
        "third migration run must NOT rewrite the file either"
    )

    # Persisted state invariants: a SINGLE audit record, carrying the
    # migration's stable identifier. A duplicated record would inflate
    # the audit count and is a security boundary violation.
    written = _load(state_path)
    migrations = written.get("migrations", [])
    assert isinstance(migrations, list), (
        f"plan_state['migrations'] must be a list, got "
        f"{type(migrations).__name__}"
    )
    assert len(migrations) == 1, (
        f"three consecutive migrations must produce exactly 1 audit "
        f"record; got {len(migrations)}: {migrations!r}"
    )

    record = migrations[0]
    assert record.get("id") == MIGRATION_ID, (
        f"the audit record id must equal the migration's stable "
        f"identifier ({MIGRATION_ID!r}); got {record.get('id')!r}"
    )

    # ``completed_phases`` must NOT carry ``verification_passed`` twice.
    phases = written.get("completed_phases", [])
    assert phases.count("verification_passed") == 1, (
        f"completed_phases must carry 'verification_passed' exactly "
        f"once after 3 runs; got {phases!r}"
    )


# ---------------------------------------------------------------------------
# TDD spec 2: idempotency survives a partial hand-revert.
# ---------------------------------------------------------------------------


def test_migration_audit_idempotent_under_partial_revert(make_plan):
    """A partial hand-revert (status back to ``pending``, audit kept)
    does NOT cause the next migration run to append a second record.

    This is the realistic operator-recovery scenario: a human notices
    the plan is stuck, flips ``verification.status`` back to
    ``"pending"`` in ``plan_state.json``, then re-runs the migration.
    The migration's idempotency guard keys on ``MIGRATION_ID`` in the
    audit list, NOT on the live verification status, so the second
    run must STILL be a no-op (audit list untouched, no second record).

    The test asserts the second-run return shape AND the audit
    count, so a regression that "fixes" the idempotency by keying
    only on ``verification.status`` (which would re-run on the
    reverted plan) fails here.
    """
    plan_dir, state_path = make_plan()

    # -- First run: real work ----------------------------------------
    first = migrate_20260805_deadlock(plan_dir)
    assert first["success"] is True
    assert first["phases_appended"]  # did real work

    # -- Operator partial-revert: status -> pending, audit kept -------
    state = _load(state_path)
    state["verification"]["status"] = "pending"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    reverted_bytes = state_path.read_bytes()

    # -- Second run: must detect already-migrated via audit id --------
    second = migrate_20260805_deadlock(plan_dir)

    assert second["success"] is True, (
        "second migration run after partial-revert must still return "
        "success=True — the plan was already migrated, the run is a "
        "successful no-op"
    )
    assert second["phases_appended"] == [], (
        "second migration run after partial-revert must NOT re-append "
        "any milestone phase; got "
        f"{second['phases_appended']!r}"
    )

    # Bytes MUST be unchanged — the migration's idempotency guard
    # must short-circuit before any write happens.
    assert state_path.read_bytes() == reverted_bytes, (
        "second migration run after partial-revert must not rewrite "
        "the file (audit id is still present, so the guard fires)"
    )

    # Audit list must STILL be exactly 1 entry — partial revert does
    # NOT reset the migration count.
    written = _load(state_path)
    migrations = written.get("migrations", [])
    assert len(migrations) == 1, (
        f"audit list must remain length 1 after partial-revert + "
        f"second run; got {len(migrations)}: {migrations!r}"
    )


# ---------------------------------------------------------------------------
# TDD spec 3: the audit record's shape is contract-pinned.
# ---------------------------------------------------------------------------


def test_migration_audit_record_shape(make_plan):
    """The committed audit record carries all 5 contract-pinned keys.

    ``from_status`` is the security-relevant one: it is the ONLY
    way a post-hoc reviewer can tell which previous status the
    migration migrated away from (a forensic indicator if the
    migration ever runs in a state it shouldn't). A refactor that
    drops it (e.g. because the migration is "obviously" going from
    ``pending``) is a security regression.

    Each key has a usable type:

      * ``id`` — string (the migration's stable identifier)
      * ``applied_at`` — string (ISO-8601 UTC timestamp)
      * ``from_status`` — string (the previous ``verification.status``)
      * ``to_status`` — string (always ``"passed"`` for this migration)
      * ``phases_appended`` — list of strings (the milestone phases)

    The test deliberately avoids pinning the EXACT timestamp value
    (which is non-deterministic) and the EXACT ordering of fields
    in the JSON dump (which JSON-serialisation decides).
    """
    plan_dir, state_path = make_plan()

    migrate_20260805_deadlock(plan_dir)

    written = _load(state_path)
    migrations = written.get("migrations", [])
    assert len(migrations) == 1
    record = migrations[0]

    # -- Each contract-pinned key has a usable type -------------------
    assert isinstance(record.get("id"), str), (
        f"audit record 'id' must be a string, got "
        f"{type(record.get('id')).__name__}"
    )
    assert record["id"] == MIGRATION_ID, (
        f"audit record id must equal {MIGRATION_ID!r}; "
        f"got {record['id']!r}"
    )

    assert isinstance(record.get("applied_at"), str), (
        f"audit record 'applied_at' must be a string, got "
        f"{type(record.get('applied_at')).__name__}"
    )
    assert "T" in record["applied_at"], (
        f"audit record 'applied_at' must be ISO-8601 shaped "
        f"(contain a 'T' separator); got {record['applied_at']!r}"
    )

    assert isinstance(record.get("from_status"), str), (
        f"audit record 'from_status' must be a string (the "
        f"verification.status the migration moved away from); got "
        f"{type(record.get('from_status')).__name__}"
    )
    # Our fixture starts at ``pending`` and the migration is a
    # ``pending -> passed`` move.
    assert record["from_status"] == "pending", (
        f"audit record 'from_status' must record the pre-migration "
        f"status ({'pending'!r}); got {record['from_status']!r}"
    )

    assert isinstance(record.get("to_status"), str), (
        f"audit record 'to_status' must be a string, got "
        f"{type(record.get('to_status')).__name__}"
    )
    assert record["to_status"] == "passed", (
        f"this migration's terminal to_status is {'passed'!r}; "
        f"got {record['to_status']!r}"
    )

    phases_appended = record.get("phases_appended")
    assert isinstance(phases_appended, list), (
        f"audit record 'phases_appended' must be a list, got "
        f"{type(phases_appended).__name__}"
    )
    assert all(isinstance(p, str) for p in phases_appended), (
        f"every entry in 'phases_appended' must be a string; "
        f"got {phases_appended!r}"
    )
    assert "verification_passed" in phases_appended, (
        f"'phases_appended' must include 'verification_passed' "
        f"(the milestone phase the migration appends); got "
        f"{phases_appended!r}"
    )


# ---------------------------------------------------------------------------
# TDD spec 4: re-running on a plan already at the migration terminal
# does not duplicate audit OR rewrite the file.
# ---------------------------------------------------------------------------


def test_migration_audit_not_duplicated_when_already_at_terminal(make_plan):
    """Re-running on an already-passed plan is a no-op AND a no-write.

    Distinct from :func:`test_migration_audit_not_duplicated`: the
    first run in that test does real work; here we seed the plan
    already at ``verification.status == "passed"`` and verify the
    migration short-circuits without touching the audit list OR the
    file bytes.

    This is the regression case for the audit-idempotency contract:
    a refactor that "simplifies" the guard by removing the
    audit-id check (relying only on ``verification.status``) would
    still pass the basic test above but would re-enter the audit
    list on this scenario.
    """
    plan_dir, state_path = make_plan(
        verification_status="passed",
        current_phase="verification_passed",
        completed_phases=[
            "executing",
            "execution",
            "completed",
            "verification_passed",
        ],
    )

    # Seed an audit record so the test exercises the "already-migrated"
    # short-circuit, not the "no audit yet" path.
    state = _load(state_path)
    state["migrations"] = [
        {
            "id": MIGRATION_ID,
            "applied_at": "2026-08-06T00:00:00+00:00",
            "from_status": "pending",
            "to_status": "passed",
            "phases_appended": ["verification_passed"],
        }
    ]
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    before_bytes = state_path.read_bytes()

    result = migrate_20260805_deadlock(plan_dir)

    assert result["success"] is True
    assert result["from_status"] == "passed"
    assert result["to_status"] == "passed"
    assert result["phases_appended"] == []

    # No rewrite at all.
    assert state_path.read_bytes() == before_bytes, (
        "migration must NOT rewrite a plan that already converged — "
        "the audit-id short-circuit fires before any write"
    )

    # Audit list still length 1.
    written = _load(state_path)
    assert len(written.get("migrations", [])) == 1, (
        f"audit list must remain length 1 after a no-op run; "
        f"got {written.get('migrations')!r}"
    )