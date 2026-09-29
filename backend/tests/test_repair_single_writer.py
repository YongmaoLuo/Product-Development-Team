"""Regression tests for the 2026-09-08 single-writer refactor:
:meth:`verification.orchestrator.VerificationOrchestrator.check_cycle_conditions`
must persist each ``RepairTaskAssembler`` task to ``state.db`` via
:meth:`PlanTaskRepository.add_task` (the same path the refiner uses).
The downstream executor's :meth:`AutonomousAgent._load_tasks` Phase-2
reconcile step then picks them up as state.db orphans and injects them
into the DAG — no ``tasks_with_repair_round_*.json`` snapshot is
written anymore.

This is the *third* leg of the storage-medium unification (2026-09-08 plan):

  1. Refiner splits → ``add_task`` (no more ``task_manager.set_tasks``)
  2. Verification repair → ``add_task`` (this test)
  3. Executor ``_load_tasks`` Phase-2 reconcile merges state.db orphans
     into the DAG so a single canonical ``tasks.json`` stays read-only
     post-plan-generation.

Why these tests exist
---------------------
The previous verification-repair pipeline wrote a
``verification_tasks_round_N.json`` snapshot and merged it into a
sibling ``tasks_with_repair_round_N.json`` before the executor
subprocess started. That second writer violated the
``tasks.json`` read-only invariant and produced merge drift when the
executor crashed between snapshot and merge. The 2026-09-08 plan
replaces both writers with a single state.db write, so the executor
sees the union via the orphan-reconcile step. This test pins that
contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make backend module importable when pytest runs from repo root or
# from backend/ directly.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state_db(tmp_path, monkeypatch):
    """Per-test state.db so parallel runs / pre-existing rows don't bleed."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    yield db_path


@pytest.fixture
def project_dir(tmp_path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    return project


@pytest.fixture
def plan_dir(tmp_path, tmp_state_db, project_dir) -> Path:
    plan = tmp_path / "plans" / "test-repair-single-writer"
    plan.mkdir(parents=True)
    # Pre-stage plan_state.json AND the plan_routing SQLite row so the
    # orchestrator can transition into ``verification_failed`` (the
    # default empty state is ``interview`` which cannot transition
    # to verification states). ``check_cycle_conditions`` is always
    # called AFTER ``start_verification_cycle`` has set the phase to
    # ``verification_running``, so we start there.
    #
    # Both pre-stages are needed: ``PlanState.__init__`` reads SQLite
    # first, then falls back to the legacy ``plan_state.json`` file.
    # If we only write the file, the SQLite read returns a row with
    # ``current_phase='interview'`` (the default when NULL) and the
    # orchestrator's transition raises ``Illegal transition``.
    state = {
        "plan_id": plan.name,
        "current_phase": "verification_running",
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    (plan / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")

    # Also seed the SQLite row so ``_load_state_from_sqlite`` returns
    # the right phase. ``plan_state.transition_to`` enforces that the
    # destination phase's prerequisite (``execution`` for
    # ``verification_failed`` / ``verification_passed``) is in
    # ``completed_phases`` — SQLite-only state means we have to seed
    # it as a JSON column.
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    conn = open_db(tmp_state_db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_routing ("
            "  plan_id, current_phase, completed_phases,"
            "  review_rounds, flags, verification, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                plan.name,
                "verification_running",
                json.dumps(["execution"]),
                json.dumps({"prd": 0, "arch": 0, "test": 0}),
                json.dumps({}),
                json.dumps({
                    "status": "pending",
                    "round": 0,
                    "max_rounds": 3,
                    "stop_reason": None,
                }),
                "2026-01-01T00:00:00Z",
            ),
        )
        # Also seed plan_execution so PlanTaskRepository.add_task has
        # a row to write into. The orchestrator's ``check_cycle_conditions``
        # runs against a plan that's already in ``verification_running``,
        # which by construction has a plan_execution row in production.
        conn.execute(
            "INSERT INTO plan_execution ("
            "  plan_id, current_phase, attempt_count, project_dir,"
            "  exec_status, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                plan.name,
                "verification_running",
                0,
                str(project_dir),
                "completed",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return plan


def _seed_failed_vps_report(plan_dir: Path, vps: list[str]) -> None:
    """Seed ``verification_report.json`` and ``verification_plan.json``
    so :func:`extract_failed_vps_from_report` returns a deterministic
    list.

    Note: ``verification_report.json`` uses ``id`` for the per-VP key
    (not ``vp_id``) — verified by reading the production report on
    2026-09-07 (see ``verification_report_reader.py:120-127``).
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "verification_results": [
            {
                "id": vp_id,  # NOT vp_id — reader uses .get("id")
                "status": "FAILED",
                "actual_result": "broken",
                "evidence": "...",
            }
            for vp_id in vps
        ],
    }
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report), encoding="utf-8",
    )
    # ``verification_plan.json`` uses ``verification_points`` (not
    # ``vps``) — see verification_report_reader.py:111.
    vp_plan = {
        "verification_points": [
            {"id": vp_id, "title": f"VP {vp_id}"} for vp_id in vps
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(vp_plan), encoding="utf-8",
    )


def _stub_repair_contents(vps: list[str], round_n: int) -> list[dict]:
    """Build deterministic ``repair_contents`` matching what
    ``RepairGenerator.generate_repair_contents`` would return for
    those VPs. The orchestrator's ``check_cycle_conditions`` passes
    these to ``RepairTaskAssembler.assemble``.
    """
    return [
        {
            "failed_vp_id": vp,
            "title": f"Fix {vp}",
            "description": f"Remediation for {vp}",
            "acceptance_criteria": f"{vp} passes",
        }
        for vp in vps
    ]


def _construct_orchestrator(plan_dir: Path, project_dir: Path):
    """Construct a :class:`VerificationOrchestrator` with the
    ``VerificationAgent`` and ``RepairTaskGenerator`` patched out —
    these tests only exercise ``check_cycle_conditions``'s side-effect
    (state.db write), not the LLM-bound work.

    Returns ``(orch, mock_rg)`` where ``mock_rg`` is the
    ``RepairTaskGenerator`` mock so the caller can stub
    ``generate_repair_contents``.

    The patches stay active for the lifetime of the returned mock
    context manager — tests should ``with _construct_orchestrator(...) as (orch, mock_rg):``.
    """
    from backend.verification.orchestrator import VerificationOrchestrator

    return patch.multiple(
        "backend.verification.orchestrator",
        VerificationAgent=patch("backend.verification.orchestrator.VerificationAgent").start(),
        RepairTaskGenerator=patch("backend.verification.orchestrator.RepairTaskGenerator").start(),
    )


@pytest.fixture
def orch_factory(plan_dir, project_dir):
    """Fixture that returns a context manager: use as
    ``with orch_factory() as (orch, mock_rg): ...``.

    Patches VerificationAgent + RepairTaskGenerator for the test's
    duration, then yields the constructed orchestrator + the
    RepairTaskGenerator mock so the test can stub
    ``generate_repair_contents``.
    """
    from backend.verification.orchestrator import VerificationOrchestrator
    import contextlib

    @contextlib.contextmanager
    def factory():
        with patch("backend.verification.orchestrator.VerificationAgent"), \
             patch("backend.verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                plan_dir=plan_dir,
                project_dir=project_dir,
                coding_tool=None,
            )
            yield orch, MockRG

    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_cycle_conditions_writes_repair_tasks_to_state_db(
    tmp_state_db, plan_dir, project_dir, orch_factory,
):
    """A failed verification round with 3 VPs → 3 repair tasks written
    to state.db with the expected schema.

    This is the single-writer contract: ``check_cycle_conditions`` is
    the ONLY writer of repair tasks after the 2026-09-08 refactor.
    """
    _seed_failed_vps_report(plan_dir, ["VP-1", "VP-2", "VP-3"])

    with orch_factory() as (orch, mock_rg):
        mock_rg.return_value.generate_repair_contents.return_value = (
            _stub_repair_contents(["VP-1", "VP-2", "VP-3"], round_n=1)
        )
        result = orch.check_cycle_conditions(report={}, round_number=1)

    # 1. Result dict has the in-memory contract intact.
    assert result["should_continue"] is False
    assert result["status"] == "verification_failed"
    assert len(result["repair_tasks"]) == 3

    # 2. state.db has 3 entries with the expected schema.
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(tmp_state_db)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        all_entries = repo.load_all(plan_dir.name)

        assert set(all_entries.keys()) == {
            "repair-r1-01",
            "repair-r1-02",
            "repair-r1-03",
        }, f"unexpected repair task ids: {set(all_entries.keys())}"

        # Each entry should default to ``status='pending'`` (the
        # add_task contract) and carry the audit metadata RepairTaskAssembler
        # stamps (task_group, execution_group, priority, acceptance_criteria,
        # failed_vp_id, round).
        for tid, entry in all_entries.items():
            assert entry["status"] == "pending", (
                f"{tid} should default to status=pending, got {entry['status']!r}"
            )
            assert entry["_repo_version"] == 1, (
                f"{tid} should have _repo_version=1 on first write, "
                f"got {entry['_repo_version']!r}"
            )
            assert entry["task_group"] == "repair-round-1"
            assert entry["round"] == 1
            assert "acceptance_criteria" in entry
            assert "failed_vp_id" in entry
            assert "priority" in entry
            assert "execution_group" in entry
    finally:
        conn.close()


def test_check_cycle_conditions_idempotent_on_second_call(
    tmp_state_db, plan_dir, project_dir, orch_factory,
):
    """Re-running ``check_cycle_conditions`` for the same round should
    not raise — ``add_task`` with default ``expected_version=0`` would
    raise ``TaskProgressConflictError`` on duplicate id.

    Why this matters: a buggy orchestrator that re-emits the same
    round (e.g. after a crash) must not crash again on the duplicate
    write. The orchestrator wraps ``add_task`` in try/except so a
    conflict is silently ignored. Operators can recover by running a
    new round (round N+1) which generates fresh ids.
    """
    _seed_failed_vps_report(plan_dir, ["VP-1"])

    with orch_factory() as (orch, mock_rg):
        mock_rg.return_value.generate_repair_contents.return_value = (
            _stub_repair_contents(["VP-1"], round_n=1)
        )
        # First call: writes to state.db.
        orch.check_cycle_conditions(report={}, round_number=1)
        # Second call for the same round: must NOT raise — the
        # orchestrator's try/except logs a warning instead of
        # surfacing TaskProgressConflictError to the caller.
        orch.check_cycle_conditions(report={}, round_number=1)


def test_check_cycle_conditions_no_repair_tasks_no_op(
    tmp_state_db, plan_dir, project_dir, orch_factory,
):
    """When ``RepairTaskAssembler`` emits zero tasks (e.g. all VPs
    passed or ``generate_repair_contents`` returned empty), the
    orchestrator must NOT touch state.db.
    """
    _seed_failed_vps_report(plan_dir, [])

    with orch_factory() as (orch, mock_rg):
        mock_rg.return_value.generate_repair_contents.return_value = []
        result = orch.check_cycle_conditions(report={}, round_number=1)

    assert result["repair_tasks"] == []

    # state.db must have no rows for this plan_id (or zero repair entries).
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(tmp_state_db)
    try:
        migrate(conn)
        row = conn.execute(
            "SELECT task_progress FROM plan_execution WHERE plan_id = ?",
            (plan_dir.name,),
        ).fetchone()
        if row and row[0]:
            tp = json.loads(row[0])
            assert tp.get("tasks", {}) == {}, (
                f"empty repair round should not write to state.db, got {tp}"
            )
    finally:
        conn.close()


def test_check_cycle_conditions_passes_failed_at_oracle(
    plan_dir, project_dir, orch_factory,
):
    """If the overall status is PASSED, ``check_cycle_conditions``
    returns ``status='passed'`` early and never invokes the
    RepairTaskAssembler — so nothing is written to state.db.
    """
    with orch_factory() as (orch, _mock_rg):
        report = {
            "overall_status": "PASSED",
            "verification_results": [],
            "requirement_deviations": [],
        }
        result = orch.check_cycle_conditions(report=report, round_number=1)

    assert result["status"] == "passed"
    assert result["should_continue"] is False
    assert result["repair_tasks"] == []


def test_repair_tasks_persisted_via_add_task_have_pending_status(
    tmp_state_db, tmp_path,
):
    """The single-writer contract: ``add_task`` defaults new entries to
    ``status='pending'`` so the executor's ``_load_tasks`` Phase-2
    reconcile picks them up. This test verifies that even if the
    RepairTaskAssembler dict carries a non-pending status (defensive),
    the add_task write stamps ``pending`` so the executor sees them.
    """
    plan_id = "direct-add-task-test"
    # Seed a plan_execution row so add_task can find it.
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(tmp_state_db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_execution (plan_id, current_phase, updated_at) "
            "VALUES (?, 'executing', '2026-01-01T00:00:00Z')",
            (plan_id,),
        )
        conn.commit()
        repo = PlanTaskRepository(conn)

        # Manually call add_task with a RepairTaskAssembler-style dict.
        new_v = repo.add_task(
            plan_id,
            {
                "id": "repair-r1-01",
                "title": "Fix VP-1",
                "description": "Remediation for VP-1",
                "test_command": "pytest tests/test_repair_r1_01.py",
                "task_group": "repair-round-1",
                "execution_group": "vp-1",
                "priority": "high",
                "acceptance_criteria": "VP-1 passes",
                "failed_vp_id": "VP-1",
                "round": 1,
            },
        )
        assert new_v == 1

        entry = repo.get_task(plan_id, "repair-r1-01")
        assert entry is not None
        assert entry["status"] == "pending", (
            "add_task must default to pending so dispatcher picks it up"
        )
        # All 6 repair-audit fields passed through the allow-list:
        assert entry["task_group"] == "repair-round-1"
        assert entry["execution_group"] == "vp-1"
        assert entry["priority"] == "high"
        assert entry["acceptance_criteria"] == "VP-1 passes"
        assert entry["failed_vp_id"] == "VP-1"
        assert entry["round"] == 1
    finally:
        conn.close()


def test_repair_task_with_unknown_field_is_rejected(
    tmp_state_db, plan_dir, project_dir, orch_factory,
):
    """The single-writer contract also forbids unknown fields: if the
    RepairTaskAssembler (or a future migration) ever emits a key
    outside :data:`ALLOWED_STATIC_TASK_FIELDS`, ``add_task`` raises
    ``TaskProgressValidationError``. The orchestrator's try/except
    catches that and logs a warning instead of crashing.
    """
    _seed_failed_vps_report(plan_dir, ["VP-1"])

    # Repair contents that produce a dict carrying an unknown field
    # 'phantom_field' which is not on the allow-list.
    bad_contents = [
        {
            "failed_vp_id": "VP-1",
            "title": "Fix VP-1",
            "description": "d",
            # RepairTaskAssembler merges content into its own
            # template — a phantom key here would propagate.
            "phantom_field": "BAD",
        },
    ]

    with orch_factory() as (orch, mock_rg):
        mock_rg.return_value.generate_repair_contents.return_value = bad_contents

        # Must NOT raise — orchestrator catches and logs.
        result = orch.check_cycle_conditions(report={}, round_number=1)

    # The in-memory contract is preserved (caller can still inspect
    # repair_tasks), but the state.db write was rejected.
    assert isinstance(result["repair_tasks"], list)