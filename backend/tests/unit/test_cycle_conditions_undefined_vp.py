"""计划没有定义的验证点不能把计划按在 FAILED 上（判据层）。

背景
====

``20261021-PDT-HITL-1-infra-cri`` 这个计划的 ``verification_plan.json``
里只有 VP-001..VP-030，**没有 VP-031**：某一轮的 delta 分配过这个 id，
定义却没有落盘。那次尝试 8 秒结束，逐字是::

    coding tool call failed: verification cancelled for plan
    '20261021-PDT-HITL-1-infra-cri' while the dispatch was in flight

于是报告里留下一条 FAILED 记录 —— 一条**没有判据可核**的记录：既没有
预期结果，也没有断言，只有一次被取消的尝试。

前两轮的修复分别管到了**分派时**（``vp_dispatch_gaps`` 把它判成 BLOCKED）
和**修复任务生成时**（``definition_missing`` 让它不进可修复集合）。这两层
都生效了，可这条记录**每轮还是回来** —— 因为**判据层**没有把它算掉。

判据层的三处（``backend/verification/orchestrator.py``）：

* ``failed_count`` 直接数报告里的 FAILED 条目，不核对这个 id 还有没有定义
  —— 一条**无法判定**的记录把整轮按在 FAILED 上；
* ``_obsolete_vp_ids`` 只排除**计划自己作废**的点：一个**根本不在计划里**
  的点无法被标 retired，所以永远排除不掉；
* ``current_failed_ids`` / ``current_failed_keys`` 因此仍带着它，同因计数器
  被一个不存在的判据推动。

本文件钉住的是**判据层**这一层：

* 计划没有定义的点不进失败集合（它不再把计划按在 FAILED 上）；
* 两条安全边界必须写死，否则就是放水：
  **计划文件读不出来**、**计划一条验证点都没写** —— 两种情况都**不作废
  任何东西**（"计划读不到"≠"计划不要求"；一份没写下任何验证点的计划不能
  制造 criterion_defect，边界见 ``e394a52``）；
* 出口可见：判据层把作废的点报出来，并写明**人要做的动作**（作废，或由
  计划属主重新表达判据），而不是把一条无法判定的记录伪装成通过；
* 负控：计划里**有**定义的失败点照旧算失败、照旧进修复通道；
* 报告里的记录**不删** —— 证据与审计保留。

纯规则测试：不调模型、不起子进程，唯一副作用是 pytest 自己的
``tmp_path``。
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

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


#: 那次被取消的尝试的逐字文本（取自 attempt log）。
CANCELLED_TEXT = (
    "coding tool call failed: verification cancelled for plan "
    "'20261021-PDT-HITL-1-infra-cri' while the dispatch was in flight"
)

#: 一次**真的**失败（产物不对）的逐字文本 —— 负控用。
#:
#: 它与 ``CANCELLED_TEXT`` 必须分开：一次被取消的尝试判的是 ``infra``
#: （重试的是验证本身），一次真的断言失败判的是 ``deliverable_defect``
#: （要修的是产物）。两者的处置完全不同。
REAL_FAILURE_TEXT = "assert 1 == 2  —— 交付物真的不满足判据"

#: 计划里**有**定义的一个点 —— 负控用。
DEFINED_VP_ID = "VP-001"

#: 计划里**没有**定义的那个点。
UNDEFINED_VP_ID = "VP-031"


def _write_plan(plan_dir: Path, vp_ids) -> Path:
    """一份只列出 ``vp_ids`` 的验证计划。"""
    path = plan_dir / "verification_plan.json"
    path.write_text(
        json.dumps({
            "verification_points": [
                {
                    "id": vp_id,
                    "title": f"{vp_id} 的判据",
                    "verification_method": "pytest",
                    "expected_result": "PASSED",
                }
                for vp_id in vp_ids
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _make_report(results, overall_status="PARTIAL") -> dict:
    """一份报告：``results`` 是 ``(id, status)`` 或 ``(id, status, text)``。"""
    entries = []
    for item in results:
        vp_id, status = item[0], item[1]
        if len(item) > 2:
            actual = item[2]
        else:
            actual = CANCELLED_TEXT if status == "FAILED" else ""
        entries.append({
            "id": vp_id,
            "status": status,
            "title": f"{vp_id} 的判据",
            "actual_result": actual,
            "evidence": "",
        })
    return {
        "overall_status": overall_status,
        "summary": "单元测试的报告",
        "verification_results": entries,
        "requirement_deviations": [],
    }


def _write_report(plan_dir: Path, report: dict) -> Path:
    """把报告落盘 —— 修复通道读的是**文件**，不是传进来的那份 dict。"""
    path = plan_dir / "verification_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


class _PlanStateStub:
    """``PlanState`` double：判据层用到的记账，别的都不管。"""

    def __init__(self, round_number: int = 1, max_rounds: int = 3):
        self.round_number = round_number
        self.max_rounds = max_rounds
        self.calls: list = []

    def reload(self):
        self.calls.append("reload")

    def verification_passed(self):
        self.calls.append("verification_passed")

    def verification_failed(self):
        self.calls.append("verification_failed")

    def transition_to(self, phase):
        self.calls.append(f"transition_to:{phase}")

    def force_set_phase(self, phase):
        self.calls.append(f"force_set_phase:{phase}")

    def get_verification_round(self):
        return self.round_number

    def get_verification_max_rounds(self):
        return self.max_rounds

    def stop_verification_loop(self, reason):
        self.calls.append(f"stop_verification_loop:{reason}")


class _RepairGeneratorStub:
    """``RepairTaskGenerator`` double：记下被问到的失败点，不调模型。"""

    def __init__(self):
        self.asked: list = []

    def generate_repair_contents(self, candidates, round_number, **kwargs):
        self.asked.append([str(c.get("id") or "") for c in candidates or []])
        return []


@pytest.fixture
def plan_dir(tmp_path) -> Path:
    path = tmp_path / "plans" / "test-plan"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def project_dir(tmp_path) -> Path:
    path = tmp_path / "projects" / "test-project"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def orchestrator(plan_dir: Path, project_dir: Path) -> VerificationOrchestrator:
    """一个只够跑判据层的 orchestrator（不碰模型、不起子进程）。"""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
    orch.plan_state = _PlanStateStub()
    orch.repair_generator = _RepairGeneratorStub()
    return orch


# ---------------------------------------------------------------------------
# 正题：计划没有定义的点不进失败集合
# ---------------------------------------------------------------------------

def test_undefined_vp_is_not_counted_as_a_failure(
    plan_dir, project_dir, orchestrator,
):
    """一条没有判据的失败记录，不能把这一轮按在 FAILED 上。

    报告里唯一没通过的点是 ``VP-031``，而计划里**没有**它的定义。判据层
    数失败时必须把它排除 —— 否则这一轮永远过不了，而交付物那边根本
    没有东西可修。

    这里用 ``PARTIAL`` 的报告：报告层的 ``overall_status`` 只在
    ``PARTIAL`` / ``PASSED`` 时参与通过判定，用它把"失败数=0"这件事
    **单独**看见（报告自己说 FAILED 时，通过与否还取决于别的条件）。
    """
    _write_plan(plan_dir, [DEFINED_VP_ID])
    report = _make_report([
        (DEFINED_VP_ID, "PASSED"),
        (UNDEFINED_VP_ID, "FAILED"),
    ])
    _write_report(plan_dir, report)

    result = orchestrator.check_cycle_conditions(report, round_number=1)

    assert result["status"] == "passed", (
        "一条计划没有定义的失败记录把整轮按在 FAILED 上了 —— "
        "判据层必须把它排除在失败数之外"
    )
    assert result["void_vp_ids"] == [UNDEFINED_VP_ID], (
        "作废的点必须在出口报出来，否则没人知道它为什么没算"
    )
    assert result["undefined_vp_ids"] == [UNDEFINED_VP_ID]


def test_undefined_vp_does_not_move_the_same_cause_counter(
    plan_dir, project_dir, orchestrator,
):
    """同因计数器不得被一个不存在的判据推动。

    ``VP-001`` 是真的失败了（计划里有定义），``VP-031`` 没有定义。前者
    要进修复通道，后者一个都不进 —— 而且它不得出现在同因计数里：计数是
    "同一个判据连续失败了几轮"，一个没有判据的点没有"连续失败"可言。
    """
    _write_plan(plan_dir, [DEFINED_VP_ID])
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
        (UNDEFINED_VP_ID, "FAILED"),
    ], overall_status="FAILED")
    _write_report(plan_dir, report)

    result = orchestrator.check_cycle_conditions(report, round_number=1)

    counted = sorted(orchestrator._same_failure_counts)
    assert not any(key.startswith(UNDEFINED_VP_ID) for key in counted), (
        f"没有定义的点不得推动同因计数器，却出现在 {counted} 里"
    )
    assert any(key.startswith(DEFINED_VP_ID) for key in counted), (
        f"计划里有定义的失败点必须照旧计数，却不在 {counted} 里"
    )
    # 修复通道只问得到真正有判据、真正坏了的那个点。
    assert orchestrator.repair_generator.asked == [[DEFINED_VP_ID]], (
        "没有定义的点不得进修复通道（它没有判据可满足）"
    )
    # 出口说明的是"没有判据"，不是"产物坏了"。
    deferred = {item["id"]: item for item in result.get("deferred_vps") or []}
    assert UNDEFINED_VP_ID in deferred, (
        "没有定义的点必须在出口被点名 —— 操作者要看的正是这一条"
    )
    assert deferred[UNDEFINED_VP_ID]["failure_class"] == "criterion_defect", (
        "没有定义的点是判据坏了，不是产物坏了"
    )


def test_operator_action_is_visible_at_the_exit(
    plan_dir, project_dir, orchestrator,
):
    """出口必须写明**人要做的动作**，而不是把记录伪装成通过。"""
    _write_plan(plan_dir, [DEFINED_VP_ID])
    report = _make_report([
        (DEFINED_VP_ID, "PASSED"),
        (UNDEFINED_VP_ID, "FAILED"),
    ])
    _write_report(plan_dir, report)

    result = orchestrator.check_cycle_conditions(report, round_number=1)

    action = result.get("operator_action") or ""
    assert UNDEFINED_VP_ID in action, (
        f"出口的动作说明里必须点名 {UNDEFINED_VP_ID}，实际是 {action!r}"
    )
    assert "作废" in action and "重新表达" in action, (
        f"动作说明必须说清人要做什么（作废，或重新表达判据），实际是 {action!r}"
    )


def test_report_entries_are_never_deleted(
    plan_dir, project_dir, orchestrator,
):
    """报告里的记录**不删** —— 证据与审计保留。

    判据层只是**不算**它，不是把它从证据里抹掉。
    """
    _write_plan(plan_dir, [DEFINED_VP_ID])
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
        (UNDEFINED_VP_ID, "FAILED"),
    ], overall_status="FAILED")
    report_path = _write_report(plan_dir, report)
    before = report_path.read_text(encoding="utf-8")

    orchestrator.check_cycle_conditions(report, round_number=1)

    assert report_path.read_text(encoding="utf-8") == before, (
        "判据层不得改写报告 —— 那条记录是操作者要看的证据"
    )


# ---------------------------------------------------------------------------
# 安全边界：两种"不知道"都不得作废任何东西
# ---------------------------------------------------------------------------

def test_unreadable_plan_voids_nothing(
    plan_dir, project_dir, orchestrator,
):
    """计划文件读不出来 —— 不作废任何东西。

    "计划读不到"和"计划不要求"是两件事：读不到的计划不能用来 excuse
    一次失败。
    """
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
        (UNDEFINED_VP_ID, "FAILED"),
    ], overall_status="FAILED")
    _write_report(plan_dir, report)
    # 故意不写 verification_plan.json。

    assert orchestrator._undefined_vp_ids(report) == set(), (
        "计划读不出来时不得作废任何点 —— 计划读不到 ≠ 计划不要求"
    )
    result = orchestrator.check_cycle_conditions(report, round_number=1)
    assert result["status"] != "passed", (
        "计划读不出来时不得把这一轮判过"
    )
    assert result["void_vp_ids"] == []


def test_malformed_plan_voids_nothing(
    plan_dir, project_dir, orchestrator,
):
    """计划文件是坏的 —— 同样不作废任何东西。"""
    (plan_dir / "verification_plan.json").write_text("{ not json", encoding="utf-8")
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
        (UNDEFINED_VP_ID, "FAILED"),
    ], overall_status="FAILED")
    _write_report(plan_dir, report)

    assert orchestrator._undefined_vp_ids(report) == set(), (
        "坏掉的计划文件不得作废任何点"
    )


def test_plan_without_any_verification_point_voids_nothing(
    plan_dir, project_dir, orchestrator,
):
    """计划一条验证点都没写 —— 不作废任何东西，也不得把整轮判过。

    边界见 ``e394a52``：一份没写下任何验证点的计划不能制造
    criterion_defect，也不能靠"把所有失败都算成没定义"把整轮判过。
    """
    _write_plan(plan_dir, [])
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
        (UNDEFINED_VP_ID, "FAILED"),
    ], overall_status="FAILED")
    _write_report(plan_dir, report)

    assert orchestrator._undefined_vp_ids(report) == set(), (
        "一条验证点都没写的计划不得作废任何点"
    )
    result = orchestrator.check_cycle_conditions(report, round_number=1)
    assert result["status"] != "passed", (
        "一份没写下任何验证点的计划不得把整轮判过"
    )
    assert result["void_vp_ids"] == []


# ---------------------------------------------------------------------------
# 负控：计划里有定义的失败点照旧算失败
# ---------------------------------------------------------------------------

def test_defined_failure_is_still_a_failure(
    plan_dir, project_dir, orchestrator,
):
    """计划里有定义的失败点，照旧算失败、照旧进修复通道。"""
    _write_plan(plan_dir, [DEFINED_VP_ID])
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
    ], overall_status="FAILED")
    _write_report(plan_dir, report)

    assert orchestrator._undefined_vp_ids(report) == set(), (
        "计划里有定义的点不得被当成没定义"
    )
    result = orchestrator.check_cycle_conditions(report, round_number=1)
    assert result["status"] == "verification_failed", (
        "真的失败了就得留在失败通道里"
    )
    assert result["void_vp_ids"] == []
    assert orchestrator.repair_generator.asked == [[DEFINED_VP_ID]], (
        "真的失败了的点必须进修复通道"
    )


def test_obsolete_and_undefined_are_both_voided(
    plan_dir, project_dir, orchestrator,
):
    """计划**自己作废**的点与计划**没定义**的点，作废的方式一样。

    ``_obsolete_vp_ids`` 与 ``_undefined_vp_ids`` 是同构的两条规则：一个
    是计划明说"这条不要了"，一个是计划压根没写过这条。
    """
    _write_plan(plan_dir, [DEFINED_VP_ID])
    plan = json.loads(
        (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
    )
    plan["verification_points"].append({
        "id": "VP-002",
        "title": "计划自己作废的点",
        "verification_method": "pytest",
        "expected_result": "PASSED",
        "obsolete": True,
        "obsolete_reason": "计划属主作废",
    })
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8",
    )
    report = _make_report([
        (DEFINED_VP_ID, "FAILED", REAL_FAILURE_TEXT),
        ("VP-002", "FAILED"),
        (UNDEFINED_VP_ID, "FAILED"),
    ], overall_status="FAILED")
    _write_report(plan_dir, report)

    result = orchestrator.check_cycle_conditions(report, round_number=1)

    assert sorted(result["void_vp_ids"]) == ["VP-002", UNDEFINED_VP_ID], (
        "两种'判据没了'都要作废，且都必须在出口报出来"
    )
    assert result["undefined_vp_ids"] == [UNDEFINED_VP_ID], (
        "只有计划没定义的那个点算'没定义'；计划自己作废的点是另一回事"
    )
