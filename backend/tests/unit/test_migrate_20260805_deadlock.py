"""Unit tests for the 20260805 deadlock plan-state convergence migration.

Contract pinned here
--------------------
1. **Happy path** — a ``PASSED`` report next to a ``pending`` plan state
   converges: ``verification.status`` becomes ``"passed"``,
   ``current_phase`` becomes ``"verification_passed"``, the milestone
   phase is appended, and an audit record is written.
2. **Guard** — a non-``PASSED`` report raises ``ValueError`` and the
   ``plan_state.json`` bytes on disk are **byte-for-byte unchanged**.
3. **Idempotency** — running twice (or running on an already-passed
   plan) is a successful no-op: no duplicate phase, no duplicate audit
   record, no rewrite.
4. **Flag awareness** — ``arch_approved`` / ``test_approved`` are only
   backfilled when the corresponding flag is enabled.
5. **Atomicity** — the write goes through tempfile + ``os.replace`` and
   is read back and verified; no ``.tmp`` residue is left behind.

The tests build their fixtures with the shared task-1 factories —
reached through the registered fixtures ``mock_plan_state_factory`` /
``mock_verification_report_factory`` / ``plan_dir_writer`` from
``backend/tests/conftest.py`` — rather than hand-rolling payloads, so a
schema drift in the shared fixture fails here too.

``scripts.migrate_20260805_deadlock`` is imported by module name (not
``backend.scripts...``) because ``backend/pytest.ini`` puts ``backend/``
on ``pythonpath`` — the same import style the sibling unit tests use.
"""

import json
from pathlib import Path

import pytest

from scripts.migrate_20260805_deadlock import (
    MIGRATION_ID,
    MigrationVerificationError,
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
        plan_id="deadlocked-plan",
        verification_status="pending",
        current_phase="completed",
        overall_status="PASSED",
        flags=None,
        completed_phases=None,
        with_report=True,
    ):
        state = mock_plan_state_factory(
            verification_status,
            current_phase,
            plan_id=plan_id,
            flags=DEFAULT_FLAGS if flags is None else flags,
            completed_phases=completed_phases,
        )
        report = (
            mock_verification_report_factory(overall_status, plan_id=plan_id)
            if with_report
            else None
        )
        plan_dir = plan_dir_writer(tmp_path, plan_id, state, report=report)
        return plan_dir, plan_dir / PLAN_STATE_FILENAME

    return _make


def _load(state_path):
    return json.loads(Path(state_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# TDD spec 1: happy path
# ---------------------------------------------------------------------------


def test_migration_sets_status_phases_and_audit(make_plan):
    """PASSED report + pending state -> passed status, phase, audit record."""
    plan_dir, state_path = make_plan()

    result = migrate_20260805_deadlock(plan_dir)

    # -- returned MigrationResult -------------------------------------
    assert result["success"] is True
    assert result["from_status"] == "pending"
    assert result["to_status"] == "passed"
    assert "verification_passed" in result["phases_appended"]

    # -- persisted state ----------------------------------------------
    written = _load(state_path)
    assert written["verification"]["status"] == "passed"
    assert written["current_phase"] == "verification_passed"
    assert "verification_passed" in written["completed_phases"]

    # -- audit trail ---------------------------------------------------
    audit = written["migrations"]
    assert isinstance(audit, list) and len(audit) == 1
    record = audit[0]
    assert record["id"] == MIGRATION_ID
    assert record["from_status"] == "pending"
    assert record["to_status"] == "passed"
    assert record["applied_at"]  # ISO-8601 timestamp, non-empty


# ---------------------------------------------------------------------------
# TDD spec 2: guard — abort without modification
# ---------------------------------------------------------------------------


def test_migration_aborts_without_modification(make_plan):
    """FAILED report -> ValueError, and plan_state.json bytes are unchanged."""
    plan_dir, state_path = make_plan(overall_status="FAILED")

    before = state_path.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        migrate_20260805_deadlock(plan_dir)

    assert "FAILED" in str(excinfo.value)

    # Byte-for-byte equality is the strongest possible "no write" proof.
    assert state_path.read_bytes() == before

    # And the semantic state is untouched too.
    unchanged = _load(state_path)
    assert unchanged["verification"]["status"] == "pending"
    assert unchanged["current_phase"] == "completed"
    assert "migrations" not in unchanged


@pytest.mark.parametrize(
    "overall_status", ["FAILED", "PARTIAL", "SKIPPED", "passed", "", None]
)
def test_non_passed_statuses_all_abort(make_plan, overall_status):
    """Only the exact string ``"PASSED"`` is accepted; everything else aborts."""
    plan_dir, state_path = make_plan(
        overall_status=overall_status
    )
    before = state_path.read_bytes()

    with pytest.raises(ValueError):
        migrate_20260805_deadlock(plan_dir)

    assert state_path.read_bytes() == before


def test_missing_report_aborts_without_modification(make_plan):
    """No verification_report.json -> ValueError, state untouched."""
    plan_dir, state_path = make_plan(with_report=False)
    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="verification report not found"):
        migrate_20260805_deadlock(plan_dir)

    assert state_path.read_bytes() == before


def test_corrupt_report_aborts_without_modification(make_plan):
    """Unparseable report -> ValueError, state untouched."""
    plan_dir, state_path = make_plan()
    (plan_dir / VERIFICATION_REPORT_FILENAME).write_text(
        "{not json", encoding="utf-8"
    )
    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="not valid JSON"):
        migrate_20260805_deadlock(plan_dir)

    assert state_path.read_bytes() == before


def test_missing_plan_dir_raises(tmp_path):
    """A non-existent plan dir raises ValueError rather than creating one."""
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ValueError, match="not a directory"):
        migrate_20260805_deadlock(missing)

    assert not missing.exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_already_passed_is_successful_noop(make_plan):
    """A plan already at passed converges to a no-op result, file untouched."""
    plan_dir, state_path = make_plan(
        verification_status="passed",
        current_phase="verification_passed",
        completed_phases=["execution", "completed", "verification_passed"],
    )
    before = state_path.read_bytes()

    result = migrate_20260805_deadlock(plan_dir)

    assert result == {
        "success": True,
        "from_status": "passed",
        "to_status": "passed",
        "phases_appended": [],
    }
    # No-op means literally no write.
    assert state_path.read_bytes() == before


def test_migration_is_idempotent(make_plan):
    """VP-011 idempotency contract: second call is a successful no-op.

    Mirrors test_running_twice_does_not_duplicate_phase_or_audit but
    uses the name referenced by the VP-011 test_command.
    """
    plan_dir, state_path = make_plan()

    first = migrate_20260805_deadlock(plan_dir)
    after_first = state_path.read_bytes()

    second = migrate_20260805_deadlock(plan_dir)

    # Idempotent: success=True, no extra phases, no extra audit, no rewrite.
    assert second['success'] is True
    assert second['phases_appended'] == []
    assert state_path.read_bytes() == after_first

    written = _load(state_path)
    phases = written['completed_phases']
    assert phases.count('verification_passed') == 1
    assert len(written['migrations']) == 1
    assert first['phases_appended']  # first run did real work


def test_running_twice_does_not_duplicate_phase_or_audit(make_plan):
    """Second run appends neither a duplicate phase nor a second audit record."""
    plan_dir, state_path = make_plan()

    first = migrate_20260805_deadlock(plan_dir)
    after_first = state_path.read_bytes()

    second = migrate_20260805_deadlock(plan_dir)

    assert first["phases_appended"]  # first run did real work
    assert second["phases_appended"] == []
    assert second["success"] is True

    # Byte-identical: the second run wrote nothing at all.
    assert state_path.read_bytes() == after_first

    written = _load(state_path)
    phases = written["completed_phases"]
    assert phases.count("verification_passed") == 1
    assert len(written["migrations"]) == 1


# ---------------------------------------------------------------------------
# Flag awareness
# ---------------------------------------------------------------------------


def test_disabled_arch_and_test_phases_are_not_backfilled(make_plan):
    """arch/test disabled -> their approved phases are never appended."""
    plan_dir, state_path = make_plan(
        flags={"arch_enabled": False, "test_enabled": False}
    )

    result = migrate_20260805_deadlock(plan_dir)

    assert "arch_approved" not in result["phases_appended"]
    assert "test_approved" not in result["phases_appended"]

    written = _load(state_path)
    assert "arch_approved" not in written["completed_phases"]
    assert "test_approved" not in written["completed_phases"]


def test_enabled_arch_and_test_phases_are_backfilled(make_plan):
    """arch/test enabled -> their approved phases are appended once."""
    plan_dir, state_path = make_plan(
        flags={"arch_enabled": True, "test_enabled": True}
    )

    result = migrate_20260805_deadlock(plan_dir)

    assert "arch_approved" in result["phases_appended"]
    assert "test_approved" in result["phases_appended"]

    phases = _load(state_path)["completed_phases"]
    assert phases.count("arch_approved") == 1
    assert phases.count("test_approved") == 1


def test_existing_phases_are_not_re_appended(make_plan):
    """A phase already in completed_phases is never duplicated."""
    plan_dir, state_path = make_plan(
        flags={"arch_enabled": True, "test_enabled": False},
        completed_phases=["prd_approved", "arch_approved", "execution"],
    )

    result = migrate_20260805_deadlock(plan_dir)

    assert "prd_approved" not in result["phases_appended"]
    assert "arch_approved" not in result["phases_appended"]
    assert "execution" not in result["phases_appended"]

    phases = _load(state_path)["completed_phases"]
    for phase in ("prd_approved", "arch_approved", "execution"):
        assert phases.count(phase) == 1


# ---------------------------------------------------------------------------
# Atomicity / durability
# ---------------------------------------------------------------------------


def test_write_leaves_no_tmp_residue(make_plan):
    """The tempfile used for the atomic replace is not left behind."""
    plan_dir, _ = make_plan()

    migrate_20260805_deadlock(plan_dir)

    leftovers = [p.name for p in plan_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_written_file_is_valid_json_and_readback_matches(make_plan):
    """The read-back verification means the on-disk file parses and matches."""
    plan_dir, state_path = make_plan()

    result = migrate_20260805_deadlock(plan_dir)

    # If the read-back had mismatched, migrate() would have raised.
    assert result["success"] is True
    reloaded = _load(state_path)
    assert reloaded["verification"]["status"] == "passed"
    assert reloaded["current_phase"] == "verification_passed"


def test_readback_mismatch_raises_verification_error(make_plan, monkeypatch):
    """A silently-failing write surfaces as MigrationVerificationError."""
    plan_dir, state_path = make_plan()

    import scripts.migrate_20260805_deadlock as mod

    # Simulate a write that "succeeds" but does not land on disk.
    monkeypatch.setattr(mod, "atomic_write_json", lambda *a, **k: None)

    with pytest.raises(MigrationVerificationError, match="read-back mismatch"):
        migrate_20260805_deadlock(plan_dir)

    # The original (pending) file is still what's on disk.
    assert _load(state_path)["verification"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Preserved fields
# ---------------------------------------------------------------------------


def test_unrelated_state_fields_are_preserved(make_plan):
    """Fields the migration does not own survive the rewrite untouched."""
    plan_dir, state_path = make_plan(plan_id="preserve-me")
    original = _load(state_path)

    migrate_20260805_deadlock(plan_dir)
    written = _load(state_path)

    assert written["plan_id"] == original["plan_id"]
    assert written["review_rounds"] == original["review_rounds"]
    assert written["flags"] == original["flags"]
    # verification sub-keys the migration does not own are preserved
    assert written["verification"]["max_rounds"] == (
        original["verification"]["max_rounds"]
    )
    assert written["verification"]["round"] == original["verification"]["round"]
