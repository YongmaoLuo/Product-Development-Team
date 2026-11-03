"""RED regression tests: an infra-unresolved round must stop, not iterate.

Background
----------
A point whose environment kept breaking has no verdict about the
deliverable — the product was **never asked**.  The report says so with
a word of its own
(:data:`verification.failure_triage.INFRA_UNRESOLVED_TERMINAL`): the
entry's status is ``BLOCKED`` rather than ``FAILED``, and it carries the
terminal in ``failure_terminal``.

On the base point such a round falls into the failure path.  Nothing was
broken, so there is nothing to repair -- but the round asks the repair
model to fix the product for a defect nobody found, and the round after
it asks the same question again, until the chain runs out of rounds and
ends as ``max_rounds_reached``.  That ending says "the deliverable never
got through N tries"; the round actually says "the environment never let
us look".  The two are not the same verdict, and only one of them is
true.

What these tests pin
--------------------
 1. an infra-unresolved round ends as ``loop_stopped``: ``should_stop``
    is true, ``stop_reason`` stays ``None``, there are no repair tasks,
    and the exit says which kind of round it was
    (``infrastructure_unresolved``);
 2. the exit does **not** claim the round was environment-blocked --
    the two flags are mutually exclusive, and a consumer that reads one
    of them must not read both;
 3. the negative controls: an all-PASSED round still passes, a plain
    code failure still produces repair tasks with the stop reason it
    has always had, and a mixed round (a real defect *and* an
    infra-unresolved point) still repairs the real defect -- and only
    the real defect;
 4. the infra terminal buys no repair task: a point carrying it never
    reaches the repair model, whatever class it names.

The interface contract
-----------------------
``check_cycle_conditions(report)`` gains one more exit::

    {"status": "loop_stopped", "should_stop": True, "stop_reason": None,
     "repair_tasks": [], "infrastructure_unresolved": True}

``stop_reason`` stays ``None``: the round's *status* is the verdict, and
a new reason string would have to enter the state machine's closed
vocabulary (``plan_state.PlanState.stop_verification_loop``'s
``valid_reasons`` and ``server.VERIFICATION_TERMINAL_STOP_REASONS``).

Pure rule tests: no model, no subprocess -- the only side effect is
pytest's own ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import PlanState  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402


#: The terminal the report carries when the environment never let the
#: round judge a point.  Spelled out here so the test pins the code
#: itself rather than whatever the constant holds.
INFRA_UNRESOLVED_TERMINAL = "infrastructure_unresolved"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    ``PlanState`` keeps the plan's state in the ``plan_routing`` row of
    ``state.db``.  Without an override the orchestrator would write its
    ``test-plan`` row into the repo's real ``state.db`` and read back
    whatever an earlier test left behind.  Autouse, so no test in this
    module can leak into -- or inherit from -- another.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _infra_unresolved_report():
    """A round whose infra budget ran out for one point.

    The point was **never asked** -- the product was never judged -- so
    the report says so with the infra terminal, and the entry's status
    is ``BLOCKED`` rather than ``FAILED``.
    """
    return {
        "overall_status": "SKIPPED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "BLOCKED",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
                "infrastructure_unresolved": True,
            },
        ],
    }


def _environment_blocked_report():
    """A round one outage took points out of (the sibling exit)."""
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "environment_blocked": True,
        "verification_results": [
            {
                "id": "VP-1",
                "status": "BLOCKED",
                "evidence": {"reason_code": "service_data_plane_mismatch"},
            },
            {"id": "VP-2", "status": "DEFERRED"},
        ],
    }


def _run_cycle(plan_dir, project_dir, coding_tool, report):
    """Run one verification cycle against ``report`` and return its verdict."""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(plan_dir, project_dir, coding_tool)
        orch.start_verification_cycle(round_number=1)
        return orch, orch.check_cycle_conditions(report)


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


def test_infra_unresolved_stops_with_loop_stopped(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """An infra-unresolved round ends as ``loop_stopped``.

    The environment never let the round judge the product, so the round
    has no verdict to give and nothing to repair.  It stops, and it says
    which kind of round it was.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = {
        "verification_results": [
            {
                "id": "VP-006",
                "status": "BLOCKED",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
            },
        ],
    }

    _orch, result = _run_cycle(
        temp_plan_dir, temp_project_dir, mock_coding_tool, report,
    )

    assert result["status"] == "loop_stopped", (
        f"基础设施没解决的轮次必须以 loop_stopped 收尾，实际是 {result!r}"
    )
    assert result["should_stop"] is True, (
        f"基础设施没解决的轮次必须停链，实际是 {result!r}"
    )
    assert result["stop_reason"] is None, (
        f"这一停的理由由 status 承担，不得是新字面量，实际是 {result!r}"
    )
    assert result["repair_tasks"] == [], (
        f"没被判过的产品不得派修复任务，实际是 {result!r}"
    )
    assert result["infrastructure_unresolved"] is True, (
        f"出口必须写明这一轮的基础设施没解决，实际是 {result!r}"
    )


def test_infra_unresolved_stop_leaves_the_plan_alone(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The plan is not recorded as a failed verification.

    The round produced no verdict about the deliverable, so it is not a
    failed verification and the plan is not put into the repair phase.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = _infra_unresolved_report()

    _run_cycle(temp_plan_dir, temp_project_dir, mock_coding_tool, report)

    ps = PlanState(temp_plan_dir)
    assert ps.get_current_phase() != "verification_failed", (
        f"基础设施没解决的轮次不得被记成验证失败，实际相位是 "
        f"{ps.get_current_phase()!r}"
    )
    assert ps.get_current_phase() != "verification_repairing", (
        f"基础设施没解决的轮次不得把计划推进修复相位，实际相位是 "
        f"{ps.get_current_phase()!r}"
    )
    assert ps.get_verification_status() != "failed", (
        f"基础设施没解决的轮次不得被记成一次失败的验证，实际状态是 "
        f"{ps.get_verification_status()!r}"
    )


def test_no_repair_tasks_for_infra_unresolved(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The repair model is never asked about an infra-unresolved point.

    Nothing was broken, so there is nothing to rework: asking the repair
    model to fix the product for a defect nobody found is the exact
    mistake this exit exists to stop.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = _infra_unresolved_report()

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": "VP-006", "title": "the point", "priority": "high"},
        ]
        mock_assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "title": "the point", "failed_vp_id": "VP-006"},
        ]
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.repair_generator.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-006", "title": "the point"},
        ]
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["repair_tasks"] == [], (
        f"基础设施没解决的点不得拿到修复任务，实际是 {result!r}"
    )
    orch.repair_generator.generate_repair_contents.assert_not_called()


def test_environment_blocked_flag_is_not_true_together(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The two flags are mutually exclusive.

    ``environment_blocked`` says one outage took the round out;
    ``infrastructure_unresolved`` says the environment kept breaking until
    the retry budget ran out.  They are different verdicts, and a
    consumer that reads one of them must never read both.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = _infra_unresolved_report()

    _orch, result = _run_cycle(
        temp_plan_dir, temp_project_dir, mock_coding_tool, report,
    )

    assert result.get("environment_blocked") is not True, (
        f"环境阻断与基础设施未解决不得同时为真，实际是 {result!r}"
    )
    assert result["infrastructure_unresolved"] is True, (
        f"这一轮的基础设施未解决必须写在出口上，实际是 {result!r}"
    )


def test_environment_blocked_exit_does_not_claim_infra_unresolved(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The other direction of the same exclusivity.

    The environment-blocked exit is the sibling's, and it says what it
    said before: the round was taken out by an outage.  It must not also
    claim the infra terminal.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = _environment_blocked_report()

    _orch, result = _run_cycle(
        temp_plan_dir, temp_project_dir, mock_coding_tool, report,
    )

    assert result["status"] == "loop_stopped", (
        f"环境阻断的轮次必须以 loop_stopped 收尾，实际是 {result!r}"
    )
    assert result.get("environment_blocked") is True, (
        f"环境阻断的轮次必须写明它被环境阻断，实际是 {result!r}"
    )
    assert result.get("infrastructure_unresolved") is not True, (
        f"环境阻断的轮次不得同时声称基础设施未解决，实际是 {result!r}"
    )


# ---------------------------------------------------------------------------
# The rounds that already say the truth
# ---------------------------------------------------------------------------


def test_all_passed_report_is_not_infra_unresolved(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """Every point passed: the round passes, and it does not stop.

    The negative control for the whole file.  An empty round (no entry
    at all) must not stop either -- the predicate may not be vacuously
    true.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = {
        "overall_status": "PASSED",
        "verification_results": [{"id": "VP-1", "status": "PASSED"}],
    }

    _orch, result = _run_cycle(
        temp_plan_dir, temp_project_dir, mock_coding_tool, report,
    )

    assert result["status"] == "passed", (
        f"全 PASSED 的一轮必须照旧判过，实际是 {result!r}"
    )
    assert result.get("infrastructure_unresolved") is not True, (
        f"全 PASSED 的一轮不得声称基础设施未解决，实际是 {result!r}"
    )

    from verification.orchestrator import _all_failed_are_exhausted_infra

    assert _all_failed_are_exhausted_infra(report) is False, (
        "全 PASSED 的报告不是基础设施未解决"
    )
    assert _all_failed_are_exhausted_infra({}) is False, (
        "空报告不是基础设施未解决"
    )
    assert _all_failed_are_exhausted_infra(None) is False, (
        "不是字典的报告不是基础设施未解决"
    )


def test_plain_failed_report_unchanged(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A plain code failure is still a failure.

    The negative control for the whole file: the infra exit must not
    swallow the rounds that really did fail -- they still produce repair
    tasks, and the stop reason stays the one it has always been.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = {
        "overall_status": "FAILED",
        "verification_results": [{"id": "VP-9", "status": "FAILED"}],
    }

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": "VP-9", "title": "fix the thing", "priority": "high"},
        ]
        mock_assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "title": "fix the thing", "failed_vp_id": "VP-9"},
        ]
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.repair_generator.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-9", "title": "fix the thing"},
        ]
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["status"] == "verification_failed", (
        f"真的失败了就得留在失败通道里，实际是 {result!r}"
    )
    assert result["repair_tasks"], (
        f"真的失败的点必须拿到修复任务，实际是 {result!r}"
    )
    assert result["stop_reason"] is None, (
        f"普通失败没有停机理由，实际是 {result!r}"
    )
    assert result.get("infrastructure_unresolved") is not True, (
        f"普通失败不是基础设施未解决，实际是 {result!r}"
    )


def test_mixed_round_repairs_real_defect_first(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A round with a real defect *and* an infra point repairs the defect.

    The infra terminal never swallows a real defect: the point that
    failed for a reason somebody can act on still gets its repair task,
    and only that point does.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-006", "status": "BLOCKED",
             "failure_terminal": INFRA_UNRESOLVED_TERMINAL},
            {"id": "VP-009", "status": "FAILED"},
        ],
    }

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": "VP-009", "title": "fix the thing", "priority": "high"},
            {"id": "VP-006", "title": "the blocked point", "priority": "high",
             "failure_terminal": INFRA_UNRESOLVED_TERMINAL},
        ]
        mock_assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "title": "fix the thing", "failed_vp_id": "VP-009"},
        ]
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.repair_generator.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-009", "title": "fix the thing"},
        ]
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["status"] == "verification_failed", (
        f"混合轮次里有真缺陷就得留在失败通道里，实际是 {result!r}"
    )
    assert result["repair_tasks"], (
        f"混合轮次里的真缺陷必须拿到修复任务，实际是 {result!r}"
    )
    repaired = {
        str(task.get("failed_vp_id") or "") for task in result["repair_tasks"]
    }
    assert "VP-009" in repaired, (
        f"真缺陷必须进修复任务，实际修复了 {sorted(repaired)}"
    )
    assert "VP-006" not in repaired, (
        f"基础设施未解决的点不得进修复任务，实际修复了 {sorted(repaired)}"
    )
    offered = orch.repair_generator.generate_repair_contents.call_args
    offered_ids = {
        str(vp.get("id") or "") for vp in (offered.args[0] if offered else [])
    }
    assert "VP-006" not in offered_ids, (
        f"基础设施未解决的点不得出现在修复模型面前，实际是 {sorted(offered_ids)}"
    )


def test_route_failed_vps_excludes_the_infra_terminal(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The split itself keeps the infra terminal out of ``repairable``.

    The end-to-end round above proves the outcome; this one pins the
    split, because the split is where the terminal is read.
    """
    _write_plan_state(temp_plan_dir, phase="executing")

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        repairable, deferred = orch._route_failed_vps(
            [
                {"id": "VP-009", "title": "the real defect"},
                {"id": "VP-006", "title": "the blocked point",
                 "failure_terminal": INFRA_UNRESOLVED_TERMINAL},
            ],
            1,
        )

    assert [vp.get("id") for vp in repairable] == ["VP-009"], (
        f"只有真缺陷可以修复，实际是 {[vp.get('id') for vp in repairable]}"
    )
    assert [record.get("id") for record in deferred] == ["VP-006"], (
        f"基础设施未解决的点必须被记下来而不是修复，实际是 "
        f"{[record.get('id') for record in deferred]}"
    )
