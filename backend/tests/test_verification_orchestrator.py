"""
State Machine Unit Tests for VerificationOrchestrator

Test strategy:
1. State transitions — mock VerificationAgent and RepairTaskGenerator, verify
   all loop control paths and boundary conditions
2. Exception handling — verify VerificationAgent exception propagates with
   correct state transition
3. Persistence — verify plan_state.json reflects verification_round and
   stop_reason accurately
4. Group-based parallel execution — verify the agent partitions VPs by
   method, runs groups via asyncio.gather, applies a
   ``parallelism_cap`` semaphore, and emits ``group_started`` /
   ``group_completed`` events on the persistence log

Fixture scenarios (tests/fixtures/verification_orchestrator/):
- scenario_a_round1_failed.json          第1次循环失败
- scenario_b_round2_same_failure.json    第2次循环失败（同一任务）
- scenario_c_round3_max_rounds.json      第3次循环失败达到max_rounds
- scenario_d_round1_passed.json          第1次循环成功
- scenario_e_user_stopped.json           用户手动停止
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Make `verification_agent` importable when pytest is launched from
# either the project root or the `backend/` directory. The
# orchestrator tests need direct access to ``VerificationAgent``,
# ``FakeVerifierBackend`` and ``_select_backend`` to drive the
# group-partitioning / parallel-execution contract end-to-end.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from verification_agent import (  # noqa: E402
    VerificationAgent,
    FakeVerifierBackend,
)

from verification import VerificationOrchestrator
from plan_state import PlanState
from coding_tool import ApiError


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures" / "verification_orchestrator"


def _write_plan_state(plan_dir, phase="executing", verification_round=0, max_rounds=3,
                      stop_reason=None, status="pending"):
    """Helper to bootstrap plan_state.json for a test."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase not in ("interview",) else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    Task #3.7 moved per-plan state off ``plan_state.json`` onto the
    ``plan_routing`` table: :class:`PlanState` no longer rewrites the JSON
    file at all. Two consequences for this module:

      * assertions must read the SQLite row, not the file. The file is
        frozen at whatever ``_write_plan_state`` bootstrapped, so
        asserting on it either passes vacuously or fails outright — the
        round / status / stop_reason checks below were failing because
        they were reading a value production had already stopped
        maintaining.
      * without an override the orchestrator would write its
        ``test-plan`` row into the repo's real ``state.db`` and read back
        whatever an earlier test left behind. Autouse, so no test in this
        module can leak into — or inherit from — another.

    ``_write_plan_state`` is still used to bootstrap: ``PlanState``
    migrates a legacy ``plan_state.json`` into SQLite on first read, and
    that is exactly how a real pre-retrofit plan is picked up.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _read_state(plan_dir):
    """Read the persisted plan state from the ``plan_routing`` row.

    Returns the same dict shape ``PlanState`` used to serialise into
    ``plan_state.json`` (``current_phase`` / ``completed_phases`` /
    ``verification`` / …), so the assertions below keep reading the same
    keys — they just come from SQLite now, which is the single source of
    truth.
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_db(Path(os.environ["PDT_STATE_DB_PATH"]))
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_dir.name)
    finally:
        conn.close()
    if row is None:
        return None
    return PlanState._sqlite_row_to_state(row)


def _make_orchestrator(plan_dir, project_dir, coding_tool):
    """Create orchestrator with patched VerificationAgent & RepairTaskGenerator."""
    with patch("verification.orchestrator.VerificationAgent") as MockVA, \
         patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
        orch = VerificationOrchestrator(
            plan_dir=plan_dir,
            project_dir=project_dir,
            coding_tool=coding_tool,
        )
        # Default mock behaviours — tests override as needed
        orch.verification_agent.run_full_verification.return_value = {
            "overall_status": "PASSED",
            "verification_results": [],
            "requirement_deviations": [],
        }
        orch.repair_generator.generate_and_append_tasks.return_value = []
        yield orch


# =============================================================================
# State Transitions — All Scenarios
# =============================================================================

class TestStateTransitions:
    """All scenarios -> 状态转换正确"""

    # ------------------------------------------------------------------
    # Scenario A — 第1次循环失败 -> repair -> 第2次循环
    # ------------------------------------------------------------------
    def test_scenario_a_first_round_failed_then_repair_and_rerun(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, fixture_dir
    ):
        _write_plan_state(temp_plan_dir, phase="executing", verification_round=0)
        report = json.loads((fixture_dir / "scenario_a_round1_failed.json").read_text())

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG, \
             patch("verification.verification_report_reader.extract_failed_vps_with_paths") as MockExtract, \
             patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            orch.verification_agent.run_full_verification.return_value = report
            # Repair tasks now come from the 2026-09-07 single-call flow
            # (``extract_failed_vps_from_report`` → ``generate_repair_contents``
            # → ``RepairTaskAssembler``), not from the legacy
            # ``generate_verification_tasks`` chain. Mock the two collaborators
            # the flow actually consults.
            MockExtract.return_value = [
                {"id": "VP-1", "title": "Fix login flow", "priority": "high"},
            ]
            orch.repair_generator.generate_repair_contents.return_value = [
                {"failed_vp_id": "VP-1", "title": "Fix login flow"},
            ]
            MockAssembler.return_value.assemble.return_value = [
                {"id": "RP-1", "title": "Fix login flow"},
            ]

            # Step 1: start verification cycle (round 1)
            orch.start_verification_cycle(round_number=1)
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_running"
            assert ps.get_verification_round() == 0

            # Step 2: check cycle conditions -> should enter repair
            result = orch.check_cycle_conditions(report)
            assert result["status"] == "verification_failed"
            # 2026-09-07 fix: the field is retained for backwards compat but
            # is always False — the auto-loop drives repair itself and never
            # waits on a manual confirmation. See
            # ``VerificationOrchestrator.check_cycle_conditions``.
            assert result["waiting_for_user"] is False
            assert result["repair_tasks"] == [{"id": "RP-1", "title": "Fix login flow"}]
            assert result["should_stop"] is False

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_repairing"
            assert ps.get_verification_status() == "failed"

            # Step 3: confirm repair -> the orchestrator hands control back to
            # the executor. 2026-09-12 state-machine fix: this now routes
            # ``verification_repairing → executing`` (via
            # ``PlanState.start_repair_execution``), not
            # ``verification_repairing → verification_rerunning`` — the repair
            # tasks genuinely run in the executor, so the phase vocabulary
            # reflects that. The round counter is bumped by the same call.
            orch.confirm_repair_and_rerun()
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "executing"
            assert ps.get_verification_round() == 1

            # Step 4: start next verification cycle (round 2)
            orch.start_verification_cycle(round_number=2)
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_running"
            assert ps.get_verification_round() == 1

    # ------------------------------------------------------------------
    # Scenario B — 2026-09-12: chain must NOT terminate on the
    # first same-failure repeat. A failed VP must generate a repairing
    # task and return to the executor: the iteration loop must keep
    # running rather than stalling.
    # We only terminate after ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS``
    # rounds with the same failure set; the executor gets N tries.
    # ------------------------------------------------------------------
    def test_scenario_b_same_failure_repeated_stops_the_loop(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, fixture_dir
    ):
        """Round 1 → failures detected, ``_previous_failed_ids`` set.
        Round 2 with the SAME failures → terminate.

        2026-09-19 (two identical failure sets stop the loop): this test used to
        assert the opposite, pinning the 2026-09-12 rule that a single
        repeat must never kill the chain. That rule, combined with a
        threshold of 3, made the stop condition unreachable under the
        shipped ``max_rounds=3`` — every failing plan burned a full extra
        round and ended with ``max_rounds_reached`` instead.
        """
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=1, max_rounds=3)
        report = json.loads((fixture_dir / "scenario_b_round2_same_failure.json").read_text())

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG, \
             patch("verification.verification_report_reader.extract_failed_vps_with_paths") as MockExtract, \
             patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            orch.verification_agent.run_full_verification.return_value = report
            tasks_file = temp_project_dir / "verification_tasks_round_2.json"
            tasks_file.write_text(json.dumps({
                "tasks": [{"id": "1-2", "title": "Fix login again"}]
            }))
            orch.repair_generator.generate_repair_tasks.return_value = tasks_file
            orch.repair_generator.generate_repair_contents.return_value = []

            MockExtract.return_value = [
                {"id": "VP-006", "title": "fail A", "priority": "high"},
            ]
            MockAssembler.return_value.assemble.return_value = [
                {"id": "RP-1", "title": "fix VP-006"},
            ]

            # First cycle: enters repair, sets _previous_failed_ids
            orch.start_verification_cycle(round_number=2)
            orch.check_cycle_conditions(report)

            # Second cycle with IDENTICAL failure → must terminate
            orch.start_verification_cycle(round_number=2)
            result = orch.check_cycle_conditions(report)

            assert result["status"] == "loop_stopped", (
                "Two rounds with the SAME failure set must stop the loop — "
                "that is the documented stop condition"
            )
            assert result["stop_reason"] == (
                "same_failure_repeated_after_max_attempts"
            ), "The repeat is reported via the _after_max_attempts reason"
            assert result["should_stop"] is True
            assert result["repair_tasks"] == [], (
                "Nothing left to repair — the same fix already ran and "
                "changed nothing"
            )
            assert orch._consecutive_same_failure_rounds == 1, (
                "Counter counts repeats: the 2nd identical round is 1"
            )

    # ------------------------------------------------------------------
    # Scenario C — 第3次循环失败达到 max_rounds
    # ------------------------------------------------------------------
    def test_scenario_c_max_rounds_reached_stops_loop(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, fixture_dir
    ):
        # Start with round=2 (already failed twice)
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=2, max_rounds=3)
        report = json.loads((fixture_dir / "scenario_c_round3_max_rounds.json").read_text())

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            orch.verification_agent.run_full_verification.return_value = report
            # Mock generate_verification_tasks - returns empty tasks (max_rounds soft limit)
            tasks_file = temp_plan_dir / "verification_tasks_round_3.json"
            tasks_file.write_text(json.dumps({"tasks": []}))
            orch.repair_generator.generate_verification_tasks.return_value = (tasks_file, 0)

            orch.start_verification_cycle(round_number=3)
            result = orch.check_cycle_conditions(report)

            # With soft max_rounds limit, the loop continues and enters repair mode
            assert result["status"] == "verification_failed"
            assert result["repair_tasks"] == []

    def test_terminates_after_max_consecutive_same_failure_rounds(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """Chain terminates as soon as TWO rounds share a failure set.

        2026-09-19 (reversing the 2026-09-12 decision not to terminate
        on the first repeat): two identical failure sets stop the loop.

        Counting semantics: ``_consecutive_same_failure_rounds`` counts
        *repeats*, so the first comparison (round 2 vs round 1) landing
        on the same set makes it 1, and ``_MAX_...`` is 1. The old
        threshold of 3 could not fire at all under the shipped
        ``max_rounds=3`` — it needed a fourth round — which is why every
        failing plan ended with ``max_rounds_reached``.
        """
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=0, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent"), \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG, \
             patch("verification.verification_report_reader.extract_failed_vps_with_paths") as MockExtract, \
             patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            tasks_file = temp_project_dir / "verification_tasks_round_1.json"
            tasks_file.write_text(json.dumps({"tasks": []}))
            orch.repair_generator.generate_repair_tasks.return_value = tasks_file
            orch.repair_generator.generate_repair_contents.return_value = []

            MockExtract.return_value = [
                {"id": "VP-006", "title": "permafail", "priority": "high"},
            ]
            MockAssembler.return_value.assemble.return_value = [
                {"id": "RP-1", "title": "fix"},
            ]

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-006", "status": "FAILED"},
                ],
                "requirement_deviations": [],
            }

            # Round 1 — first occurrence of this failure set
            orch.start_verification_cycle(round_number=1)
            r1 = orch.check_cycle_conditions(failed_report)
            assert r1["status"] != "loop_stopped"
            assert orch._consecutive_same_failure_rounds == 0
            assert r1.get("repair_tasks")

            # Round 2 — same failures → 1 repeat >= MAX=1 → terminate
            orch.start_verification_cycle(round_number=2)
            r2 = orch.check_cycle_conditions(failed_report)
            assert r2["status"] == "loop_stopped", (
                "Round 2 repeating round 1's failure set MUST terminate — "
                "two identical rounds is the documented stop condition"
            )
            assert orch._consecutive_same_failure_rounds == 1
            assert r2["stop_reason"] == "same_failure_repeated_after_max_attempts"
            assert r2["should_stop"] is True
            assert r2["repair_tasks"] == []

    def test_loop_tracking_survives_a_fresh_orchestrator(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """``_previous_failed_ids`` must outlive the orchestrator instance.

        ``server.py`` ``_on_repair_complete`` builds a FRESH orchestrator
        for every post-repair round, so instance-only tracking resets to
        ``None`` between rounds and the comparison can never fire: the
        consecutive rounds fail the identical VP set and the loop still
        runs to ``max_rounds``.
        """
        with patch("verification.orchestrator.VerificationAgent"), \
             patch("verification.orchestrator.RepairTaskGenerator"):
            orch_a = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            assert orch_a._previous_failed_ids is None
            assert orch_a._consecutive_same_failure_rounds == 0

            orch_a._previous_failed_ids = {"VP-001", "VP-013"}
            orch_a._consecutive_same_failure_rounds = 1
            orch_a._save_loop_tracking()

            orch_b = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            assert orch_b._previous_failed_ids == {"VP-001", "VP-013"}
            assert orch_b._consecutive_same_failure_rounds == 1

    def test_loop_tracking_tolerates_a_malformed_file(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """A corrupt tracking file degrades to the pre-2026-09-19 default."""
        (temp_plan_dir / "verification_loop_tracking.json").write_text(
            "{not json", encoding="utf-8"
        )
        with patch("verification.orchestrator.VerificationAgent"), \
             patch("verification.orchestrator.RepairTaskGenerator"):
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            assert orch._previous_failed_ids is None
            assert orch._consecutive_same_failure_rounds == 0

    def test_counter_resets_on_failure_set_change(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """When the failure set changes, the consecutive counter resets
        to 0. This means a new VP failing doesn't inherit the streak
        from previously-failing VPs.
        """
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=0, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent"), \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG, \
             patch("verification.verification_report_reader.extract_failed_vps_with_paths") as MockExtract, \
             patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            tasks_file = temp_project_dir / "verification_tasks.json"
            tasks_file.write_text(json.dumps({"tasks": []}))
            orch.repair_generator.generate_repair_tasks.return_value = tasks_file
            orch.repair_generator.generate_repair_contents.return_value = []

            MockAssembler.return_value.assemble.return_value = [
                {"id": "RP-1", "title": "fix"},
            ]

            # Round 1: VP-006 fails
            MockExtract.return_value = [
                {"id": "VP-006", "title": "fail A"},
            ]
            failed_a = {
                "overall_status": "FAILED",
                "verification_results": [{"id": "VP-006", "status": "FAILED"}],
                "requirement_deviations": [],
            }
            orch.start_verification_cycle(round_number=1)
            orch.check_cycle_conditions(failed_a)
            assert orch._consecutive_same_failure_rounds == 0

            # Round 2: VP-027 fails instead (different set)
            MockExtract.return_value = [
                {"id": "VP-027", "title": "fail B"},
            ]
            failed_b = {
                "overall_status": "FAILED",
                "verification_results": [{"id": "VP-027", "status": "FAILED"}],
                "requirement_deviations": [],
            }
            orch.start_verification_cycle(round_number=2)
            orch.check_cycle_conditions(failed_b)
            assert orch._consecutive_same_failure_rounds == 0, (
                "Counter must reset to 0 when failure set changes"
            )

            # Round 3: VP-027 fails again — counter = 1
            orch.start_verification_cycle(round_number=3)
            orch.check_cycle_conditions(failed_b)
            assert orch._consecutive_same_failure_rounds == 1

    def test_counter_resets_on_pass(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """When verification passes, the consecutive counter resets to
        0. This means the executor's successful fix doesn't get
        retroactively penalised.
        """
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=0, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent"), \
             patch("verification.orchestrator.RepairTaskGenerator"), \
             patch("verification.verification_report_reader.extract_failed_vps_with_paths"), \
             patch("repair_generator.RepairTaskAssembler"):
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [{"id": "VP-006", "status": "FAILED"}],
                "requirement_deviations": [],
            }
            passed_report = {
                "overall_status": "PASSED",
                "verification_results": [],
                "requirement_deviations": [],
            }

            # Round 1: fail
            orch.start_verification_cycle(round_number=1)
            orch.check_cycle_conditions(failed_report)
            # Round 2: fail again — counter = 1
            orch.start_verification_cycle(round_number=2)
            orch.check_cycle_conditions(failed_report)
            assert orch._consecutive_same_failure_rounds == 1

            # Round 3: PASS — counter resets to 0
            orch.start_verification_cycle(round_number=3)
            r3 = orch.check_cycle_conditions(passed_report)
            assert r3["status"] == "passed"
            assert orch._consecutive_same_failure_rounds == 0

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_passed"

    # ------------------------------------------------------------------
    # Scenario D — 第1次循环成功
    # ------------------------------------------------------------------
    def test_scenario_d_first_round_passed_completes(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, fixture_dir
    ):
        _write_plan_state(temp_plan_dir, phase="executing", verification_round=0)
        report = json.loads((fixture_dir / "scenario_d_round1_passed.json").read_text())

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            orch.verification_agent.run_full_verification.return_value = report

            orch.start_verification_cycle(round_number=1)
            result = orch.check_cycle_conditions(report)

            assert result["status"] == "passed"
            assert result["should_continue"] is False
            assert result["should_stop"] is False
            assert result["stop_reason"] is None
            assert result["repair_tasks"] == []
            assert result["waiting_for_user"] is False

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_passed"
            assert ps.get_verification_status() == "passed"

    # ------------------------------------------------------------------
    # Scenario E — 用户手动停止
    # ------------------------------------------------------------------
    def test_scenario_e_user_stopped(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool, fixture_dir
    ):
        _write_plan_state(temp_plan_dir, phase="executing", verification_round=0)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )

            # Start a cycle first
            orch.start_verification_cycle(round_number=1)
            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_running"

            # User stops
            orch.stop_verification()

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_loop_stopped"
            assert ps.get_verification_status() == "loop_stopped"
            assert ps.get_verification_stop_reason() == "user_stopped"
            assert orch.is_waiting_for_user() is False


# =============================================================================
# Boundary Conditions — verification_round and max_rounds
# =============================================================================

class TestBoundaryConditions:
    """verification_round和max_rounds -> 正确性验证"""

    def test_verification_round_increments_from_0_to_max(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """Round increments 0 -> 1 -> 2 and continues past max_rounds=3 (soft limit)."""
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=0, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            # Mock generate_verification_tasks for each round
            tasks_file_1 = temp_plan_dir / "verification_tasks_round_1.json"
            tasks_file_1.write_text(json.dumps({"tasks": [{"id": "1-1", "title": "Fix"}]}))
            tasks_file_2 = temp_plan_dir / "verification_tasks_round_2.json"
            tasks_file_2.write_text(json.dumps({"tasks": [{"id": "1-2", "title": "Fix 2"}]}))
            tasks_file_3 = temp_plan_dir / "verification_tasks_round_3.json"
            tasks_file_3.write_text(json.dumps({"tasks": [{"id": "1-3", "title": "Fix 3"}]}))
            orch.repair_generator.generate_verification_tasks.side_effect = [
                (tasks_file_1, 1), (tasks_file_2, 1), (tasks_file_3, 1)
            ]

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-001", "status": "FAILED"}
                ],
                "requirement_deviations": [],
            }

            # Round 0 -> failure -> repair -> rerun -> round 1
            orch.start_verification_cycle(round_number=1)
            orch.check_cycle_conditions(failed_report)
            orch.confirm_repair_and_rerun()
            ps = PlanState(temp_plan_dir)
            assert ps.get_verification_round() == 1

            # Round 1 -> failure -> repair -> rerun -> round 2
            orch.start_verification_cycle(round_number=2)
            orch.check_cycle_conditions({
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-002", "status": "FAILED"}
                ],
                "requirement_deviations": [],
            })
            orch.confirm_repair_and_rerun()
            ps = PlanState(temp_plan_dir)
            assert ps.get_verification_round() == 2

            # Round 2 -> failure -> soft max_rounds, loop continues to repair
            orch.start_verification_cycle(round_number=3)
            result = orch.check_cycle_conditions(failed_report)
            # With soft max_rounds limit, loop continues (no stop_reason)
            assert result["stop_reason"] is None
            assert result["status"] == "verification_failed"
            ps = PlanState(temp_plan_dir)
            assert ps.get_verification_round() == 2  # unchanged (repair doesn't increment)

    def test_round_number_passed_to_verification_agent(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """修复任务执行后重新调用VerificationAgent时传入正确round_number."""
        _write_plan_state(temp_plan_dir, phase="executing", verification_round=0)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )

            # Call start_verification_cycle with round_number=2
            orch.start_verification_cycle(round_number=2)
            # 2026-09-13: the agent signature gained a keyword-only
            # ``resume`` flag (rounds > 1 pass ``resume=True`` so the
            # verification agent continues from the previous round's
            # partial results instead of re-running every VP from
            # scratch). Pin the full call, not just the positional round.
            orch.verification_agent.run_full_verification.assert_called_once_with(
                2, resume=False
            )

    def test_max_rounds_boundary_at_exact_limit(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """max_rounds=3, round=2 (3rd failure) -> soft limit, loop continues."""
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=2, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            # Mock generate_verification_tasks
            tasks_file = temp_plan_dir / "verification_tasks_round_3.json"
            tasks_file.write_text(json.dumps({"tasks": [{"id": "1-3", "title": "Fix"}]}))
            orch.repair_generator.generate_verification_tasks.return_value = (tasks_file, 1)

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-001", "status": "FAILED"}
                ],
                "requirement_deviations": [],
            }

            orch.start_verification_cycle(round_number=3)
            result = orch.check_cycle_conditions(failed_report)

            # With soft max_rounds limit, loop continues (no stop)
            assert result["should_stop"] is False
            assert result["stop_reason"] is None
            assert result["status"] == "verification_failed"

    def test_plan_state_persists_verification_round(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """verification round is persisted to SQLite across the loop."""
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=0, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            # Mock generate_verification_tasks
            tasks_file = temp_plan_dir / "verification_tasks_round_1.json"
            tasks_file.write_text(json.dumps({"tasks": [{"id": "1-1", "title": "Fix"}]}))
            orch.repair_generator.generate_verification_tasks.return_value = (tasks_file, 1)

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-001", "status": "FAILED"}
                ],
                "requirement_deviations": [],
            }

            orch.start_verification_cycle(round_number=1)
            orch.check_cycle_conditions(failed_report)
            orch.confirm_repair_and_rerun()

            raw = _read_state(temp_plan_dir)
            # ``confirm_repair_and_rerun`` → ``start_repair_execution``
            # bumps the round and hands control to the executor.
            assert raw["verification"]["round"] == 1
            assert raw["current_phase"] == "executing"


# =============================================================================
# Exception Handling
# =============================================================================

class TestExceptionHandling:
    """VerificationAgent抛出异常 -> verification_status变为failed"""

    def test_verification_agent_exception_sets_failed_status(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        _write_plan_state(temp_plan_dir, phase="executing", verification_round=0)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            orch.verification_agent.run_full_verification.side_effect = ApiError(
                "LLM API error", status=500
            )

            with pytest.raises(ApiError):
                orch.start_verification_cycle(round_number=1)

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_failed"
            assert ps.get_verification_status() == "failed"

    def test_verification_agent_runtime_exception_sets_failed_status(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        _write_plan_state(temp_plan_dir, phase="executing", verification_round=0)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            orch.verification_agent.run_full_verification.side_effect = RuntimeError(
                "Unexpected error"
            )

            with pytest.raises(RuntimeError):
                orch.start_verification_cycle(round_number=1)

            ps = PlanState(temp_plan_dir)
            assert ps.get_current_phase() == "verification_failed"
            assert ps.get_verification_status() == "failed"


# =============================================================================
# Persistence
# =============================================================================

class TestPersistence:
    """plan_state.json -> 正确持久化verification_round和stop_reason"""

    def test_persistence_after_max_rounds_reached(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """max_rounds is soft limit - loop continues to repair mode."""
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=2, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            # Mock generate_verification_tasks
            tasks_file = temp_plan_dir / "verification_tasks_round_3.json"
            tasks_file.write_text(json.dumps({"tasks": []}))
            orch.repair_generator.generate_verification_tasks.return_value = (tasks_file, 0)

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-001", "status": "FAILED"}
                ],
                "requirement_deviations": [],
            }

            orch.start_verification_cycle(round_number=3)
            orch.check_cycle_conditions(failed_report)

            raw = _read_state(temp_plan_dir)
            # Soft max_rounds: loop continues, enters repair mode
            assert raw["verification"]["round"] == 2
            assert raw["verification"]["stop_reason"] is None  # No stop - continues
            assert raw["verification"]["status"] == "failed"
            assert raw["current_phase"] == "verification_repairing"

    def test_persistence_after_same_failure_repeated(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        # 2026-09-19 (two identical failure sets stop the loop): the chain terminates
        # on the SECOND identical failure set. This test used to assert
        # the 2026-09-12 rule ("stay alive after 1 consecutive repeat"),
        # which — with a threshold of 3 and ``max_rounds=3`` — could
        # never fire at all.
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=1, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG, \
             patch("verification.verification_report_reader.extract_failed_vps_with_paths") as MockExtract, \
             patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )
            tasks_file = temp_plan_dir / "verification_tasks_round_2.json"
            tasks_file.write_text(json.dumps({"tasks": [{"id": "1-1", "title": "Fix"}]}))
            orch.repair_generator.generate_repair_tasks.return_value = tasks_file
            orch.repair_generator.generate_repair_contents.return_value = []

            MockExtract.return_value = [
                {"id": "VP-001", "title": "fail A", "priority": "high"},
            ]
            MockAssembler.return_value.assemble.return_value = [
                {"id": "RP-1", "title": "fix VP-001"},
            ]

            failed_report = {
                "overall_status": "FAILED",
                "verification_results": [
                    {"id": "VP-001", "status": "FAILED"}
                ],
                "requirement_deviations": [],
            }

            # First check sets _previous_failed_ids and counter=0
            orch.start_verification_cycle(round_number=2)
            orch.check_cycle_conditions(failed_report)
            orch.confirm_repair_and_rerun()

            # Second check with the same failure — counter increments to
            # 1, which meets the threshold, so the chain terminates.
            orch.start_verification_cycle(round_number=2)
            result = orch.check_cycle_conditions(failed_report)

            assert (
                orch._consecutive_same_failure_rounds == 1
            ), "Counter counts repeats: the 2nd identical round is 1"
            assert result["status"] == "loop_stopped"

            raw = _read_state(temp_plan_dir)
            assert raw["current_phase"] == "verification_loop_stopped", (
                "Two identical failure sets must park the plan — the "
                "executor already tried a fix and it changed nothing"
            )
            assert raw["verification"].get("stop_reason") == (
                "same_failure_repeated_after_max_attempts"
            )

    def test_persistence_after_user_stopped(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=1, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )

            orch.start_verification_cycle(round_number=2)
            orch.stop_verification()

            raw = _read_state(temp_plan_dir)
            assert raw["verification"]["stop_reason"] == "user_stopped"
            assert raw["verification"]["status"] == "loop_stopped"
            assert raw["current_phase"] == "verification_loop_stopped"
            assert raw["verification"]["round"] == 1  # unchanged

    def test_persistence_after_verification_passed(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        _write_plan_state(temp_plan_dir, phase="executing",
                          verification_round=0, max_rounds=3)

        with patch("verification.orchestrator.VerificationAgent") as MockVA, \
             patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
            orch = VerificationOrchestrator(
                temp_plan_dir, temp_project_dir, mock_coding_tool
            )

            passed_report = {
                "overall_status": "PASSED",
                "verification_results": [],
                "requirement_deviations": [],
            }

            orch.start_verification_cycle(round_number=1)
            orch.check_cycle_conditions(passed_report)

            raw = _read_state(temp_plan_dir)
            assert raw["verification"]["status"] == "passed"
            assert raw["current_phase"] == "verification_passed"
            assert raw["verification"]["stop_reason"] is None
            assert raw["verification"]["round"] == 0


# =============================================================================
# Group-Based Parallel Execution
# =============================================================================

class TestGroupParallelExecution:
    """Contract for group-based parallel VP execution.

    Pins the four behavioural guarantees the orchestrator must hold
    once the verification pipeline moves from "iterate VPs
    sequentially" to "partition by method, run groups in parallel
    with a semaphore-bounded rate limit":

    1. **Partitioning** — ``_partition_vps_by_method`` must bucket
       VPs strictly by ``verification_method``, in first-seen order,
       producing one group per distinct method.
    2. **Intra-group parallelism** — non-``manual_check`` VPs within
       a single group must run concurrently via
       :func:`asyncio.gather`, not serially.
    3. **Semaphore rate-limiting** — ``TimeoutPolicy.parallelism_cap``
       must throttle the in-flight VPs across the whole batch
       (groups run in parallel internally, the semaphore caps the
       total in-flight at any moment).
    4. **Group events** — every non-``manual_check`` group must emit
       one ``group_started`` and one ``group_completed`` event on the
       persistence log, carrying ``method``/``count``/``status_counts``/
       ``duration_sec``.

    The tests exercise the *real* :class:`VerificationAgent` (no
    mocking of the agent itself) but replace the per-method leaf
    executors with pure-``asyncio.sleep`` stubs so the wall-clock
    cost of each VP is deterministic and the parallelism /
    rate-limit / event contracts can be measured without LLM,
    subprocess, or filesystem overhead.
    """

    @staticmethod
    def _make_vps_by_method(counts):
        """Build a flat list of VPs whose methods match ``counts``.

        ``counts`` is a mapping ``{method: n}`` and the returned list
        interleaves methods in the order they appear in the dict
        (insertion order is preserved in Python 3.7+), so the
        first-seen-order contract is testable from the same list.
        """
        vps = []
        for method, n in counts.items():
            for i in range(n):
                vps.append(
                    {
                        "id": f"VP-{method.upper().replace('_', '')}-{i}",
                        "title": f"{method} #{i}",
                        "verification_method": method,
                        "priority": "medium",
                        "expected_result": "ok",
                    }
                )
        return vps

    @staticmethod
    def _patch_leaf_to_sleep(agent, method_to_sleep):
        """Replace _run_single_vp_async with ``asyncio.sleep`` stubs per method.

        The canonical VP execution path is now _run_single_vp_async -> _delegate_to_sub_agent.
        We patch _run_single_vp_async to intercept all VP execution and sleep for
        the configured duration based on the VP's verification_method.
        ``manual_check`` VPs short-circuit immediately (no sleep).
        """
        import asyncio as _asyncio

        async def _patched_run_single_vp_async(vp):
            method = vp.get("verification_method", "manual_check")
            if method == "manual_check":
                # manual_check short-circuits - return SKIPPED immediately
                return {
                    "id": vp.get("id", "unknown"),
                    "status": "SKIPPED",
                    "actual_result": "需要人工验证",
                    "evidence": "test stub: manual_check"
                }
            sleep_seconds = method_to_sleep.get(method, 0.1)
            await _asyncio.sleep(sleep_seconds)
            return {
                "id": vp.get("id", "unknown"),
                "status": "PASSED",
                "actual_result": f"fake sleep {sleep_seconds}s",
                "evidence": "fake_sleep_stub",
            }

        agent._run_single_vp_async = _patched_run_single_vp_async

    # ------------------------------------------------------------------
    # 1. Partitioning
    # ------------------------------------------------------------------

    def test_orchestrator_partitions_by_method(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """4ui+3cr+14auto+2api → 4 groups, each with consistent method.

        This is the spec example: a mixed-method plan must yield a
        group for every distinct ``verification_method`` value
        (not, e.g. one group per VP, or one group per priority).
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )

        vps = self._make_vps_by_method(
            {
                "ui_validation": 4,
                "code_review": 3,
                "automated_test": 14,
                "api_test": 2,
            }
        )
        plan = {"verification_points": vps}

        groups = agent._partition_vps_by_method(plan)

        # Exactly 4 distinct methods → 4 groups
        assert len(groups) == 4, (
            f"expected 4 groups, got {len(groups)}: {list(groups.keys())}"
        )

        # Each group carries VPs of exactly one method, with the
        # expected count.
        assert len(groups["ui_validation"]) == 4
        assert all(
            vp["verification_method"] == "ui_validation"
            for vp in groups["ui_validation"]
        )
        assert len(groups["code_review"]) == 3
        assert all(
            vp["verification_method"] == "code_review"
            for vp in groups["code_review"]
        )
        assert len(groups["automated_test"]) == 14
        assert all(
            vp["verification_method"] == "automated_test"
            for vp in groups["automated_test"]
        )
        assert len(groups["api_test"]) == 2
        assert all(
            vp["verification_method"] == "api_test"
            for vp in groups["api_test"]
        )

    def test_orchestrator_partition_preserves_first_seen_order(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """First-seen method order drives the group keys.

        Stability guarantee: callers can rely on iterating
        ``groups.items()`` in plan order, which is what makes the
        log parser's per-group event timeline deterministic.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )

        # Interleave methods in a non-alphabetical order to make
        # the test independent of dict-iteration accidental order.
        vps = (
            self._make_vps_by_method({"api_test": 2})
            + self._make_vps_by_method({"automated_test": 3})
            + self._make_vps_by_method({"ui_validation": 4})
        )
        plan = {"verification_points": vps}

        groups = agent._partition_vps_by_method(plan)

        assert list(groups.keys()) == [
            "api_test",
            "automated_test",
            "ui_validation",
        ]

    # ------------------------------------------------------------------
    # 2. Intra-group parallelism
    # ------------------------------------------------------------------

    def test_orchestrator_runs_groups_in_parallel(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """4 ui VPs, each sleeping 5s → wall time ∈ [5, 7]s.

        With the default ``parallelism_cap=4``, four ui_validation
        VPs fit in a single semaphore batch and finish in roughly
        the duration of one VP (5s), not four (20s). The 2s upper
        bound absorbs asyncio scheduling overhead and the
        ``asyncio.run`` startup cost.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        agent.start_verification_round(1)

        vps = self._make_vps_by_method({"ui_validation": 4})
        self._patch_leaf_to_sleep(agent, {"ui_validation": 5})

        start = time.monotonic()
        asyncio.run(agent.execute_verification_plan_async({"verification_points": vps}))
        wall_time = time.monotonic() - start

        assert 5.0 <= wall_time <= 7.0, (
            f"expected wall time ∈ [5, 7]s for 4 VPs in parallel, "
            f"got {wall_time:.2f}s (would be ~20s if serial, ~5s if parallel)"
        )

    # ------------------------------------------------------------------
    # 3. Semaphore rate-limiting
    # ------------------------------------------------------------------

    def test_orchestrator_respects_parallelism_cap(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """cap=2 + 6 ui VPs at 3s each → wall time ∈ [8, 10]s.

        The semaphore (``parallelism_cap=2``) throttles in-flight
        VPs to two at a time, so 6 VPs complete in three sequential
        rounds of two, each 3s long. Total ≈ 9s. The [8, 10]s band
        absorbs asyncio scheduling / ``asyncio.run`` overhead.
        Without the semaphore the same 6 VPs would finish in a
        single round of 6 (~3s) — that's the negative control
        this test guards against.
        """
        from verification_config import TimeoutPolicy

        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        # Cap in-flight VPs at 2; raise per-method timeout well above
        # the 3s sleep so the semaphore — not the timeout — is the
        # rate-limiting mechanism.
        agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 60},
            global_default_timeout_seconds=60,
            parallelism_cap=2,
        )
        agent.start_verification_round(1)

        vps = self._make_vps_by_method({"ui_validation": 6})
        self._patch_leaf_to_sleep(agent, {"ui_validation": 3})

        start = time.monotonic()
        asyncio.run(agent.execute_verification_plan_async({"verification_points": vps}))
        wall_time = time.monotonic() - start

        # 6 VPs / cap=2 = 3 sequential rounds × 3s = ~9s.
        # Without the cap, all 6 would run in one round (~3s).
        assert 8.0 <= wall_time <= 10.0, (
            f"expected wall time ∈ [8, 10]s with cap=2 and 6 VPs at 3s, "
            f"got {wall_time:.2f}s (would be ~3s if cap ignored, ~9s if cap honoured)"
        )

    # ------------------------------------------------------------------
    # 4. Group events on the persistence log
    # ------------------------------------------------------------------

    def test_orchestrator_emits_group_events(
        self, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """Two non-manual_check groups → two ``group_started`` and
        two ``group_completed`` events, with the documented data
        shape (``method``/``count``/``status_counts``/``duration_sec``).

        ``manual_check`` is the one method that must NOT emit
        group events: it short-circuits to ``SKIPPED`` and never
        enters the gather / semaphore path, so adding it to the
        plan tests the negative case as well.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        agent.start_verification_round(1)

        vps = self._make_vps_by_method(
            {
                "ui_validation": 2,
                "code_review": 1,
                "manual_check": 1,  # must NOT emit group events
            }
        )
        self._patch_leaf_to_sleep(agent, {"ui_validation": 0.1, "code_review": 0.1})

        asyncio.run(agent.execute_verification_plan_async({"verification_points": vps}))

        # Read JSON-lines log written by the persistence layer.
        log_files = sorted((temp_plan_dir / "logs").glob("verification_*.log"))
        assert log_files, (
            "no verification log file was created — agent never started a round"
        )

        entries = []
        with open(log_files[-1], "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entries.append(json.loads(stripped))

        started = [e for e in entries if e.get("event_type") == "group_started"]
        completed = [e for e in entries if e.get("event_type") == "group_completed"]

        # Two non-manual_check groups → 2 of each event.
        assert len(started) == 2, (
            f"expected 2 group_started events, got {len(started)}: "
            f"{[e.get('data') for e in started]}"
        )
        assert len(completed) == 2, (
            f"expected 2 group_completed events, got {len(completed)}: "
            f"{[e.get('data') for e in completed]}"
        )

        # Each event carries the documented data fields.
        methods_seen_started = {e["data"]["method"] for e in started}
        methods_seen_completed = {e["data"]["method"] for e in completed}
        assert methods_seen_started == {"ui_validation", "code_review"}
        assert methods_seen_completed == {"ui_validation", "code_review"}

        for event in started:
            data = event["data"]
            assert "method" in data
            assert "count" in data
            assert data["count"] >= 1

        for event in completed:
            data = event["data"]
            assert "method" in data
            assert "count" in data
            assert "status_counts" in data, (
                f"group_completed data missing status_counts: {data}"
            )
            assert "duration_sec" in data, (
                f"group_completed data missing duration_sec: {data}"
            )
            assert isinstance(data["duration_sec"], (int, float))
            assert data["duration_sec"] >= 0

        # manual_check must NOT have produced a group event.
        assert all(
            e["data"]["method"] != "manual_check" for e in started + completed
        ), "manual_check must not emit group events"
