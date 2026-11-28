"""轮次收口的门禁重算必须看到**本轮最终结论**（2026-11-28）。

事故
----
``20261116-K-Magic-DNS`` 连续三轮同一失败：Phase 1 的验证点全部通过
（含 VP-001、VP-006），两条 Phase 2 全量门禁（VP-017 / VP-018）却是
``DEFERRED``、**从未被派发**，整轮被算成 SKIPPED，计划 failed。

根因
----
两份账本撕裂：

* 裁决表（``plan_verification.verdicts``）里 VP-001 的最后一条是
  ``{"status": "BLOCKED", "round": 1}`` —— 那是**旧轮**的结论；
* 本轮的执行信封（``plan_verification.execution_results``）里 VP-001 是
  ``PASSED`` —— 那是**本轮**的最终结论（报告读的就是它）。

门禁重算（``rerun_deferred_gate_layers`` → ``_gate_block_decision_for`` →
``_blocking_gate_candidates`` → ``_terminal_status_of``）**只读裁决表**，
于是「裁决表里还留着旧 BLOCKED」被当成了「本轮仍被挡着」，被延后的
关卡层永远不被派发。

契约（本文件钉住）
----------------
1. 收口重算以**本轮最终结论**为准：执行信封里阻塞者是终态成功
   （``PASSED`` / ``SKIPPED``）时，即使裁决表里同一 VP 还留着旧
   ``BLOCKED`` / ``FAILED``，被延后的门禁层也**必须派发**；
2. 信封里阻塞者仍不是终态成功（``BLOCKED``）或信封里没有它 → 维持既有
   行为：不派发、账本里的 ``DEFERRED`` 保留；
3. 轮次隔离不许被绕过：信封属于别的轮次时**不采信**。

四条用例
--------
1. ``test_the_gate_layer_runs_when_the_round_truth_says_the_blocker_passed``
   —— 回归用例（修前必红）：裁决表 BLOCKED + 本轮信封 PASSED → 派发；
2. ``test_the_gate_layer_stays_deferred_when_the_round_truth_agrees``
   —— 负控：信封也是 BLOCKED → 不派发、DEFERRED 保留；
3. ``test_the_gate_layer_stays_deferred_without_a_round_truth``
   —— 负控：没有信封 → 既有行为不变；
4. ``test_a_round_truth_from_another_round_is_not_used``
   —— 轮次隔离：信封属于别的轮次 → 不采信、不派发。

另加两条**生产形状**的用例（本轮账本 = 本轮真跑出来的执行结果，报告读的
就是它；跨轮裁决表 = 撕裂现场的那本旧账）：

5. ``test_the_gate_layer_runs_when_the_round_ledger_says_the_blocker_passed``
   —— 回归用例（修前必红）：本轮账本 PASSED + 跨轮裁决表旧 BLOCKED →
   派发；
6. ``test_the_gate_layer_stays_deferred_when_the_round_ledger_agrees``
   —— 负控：本轮账本也是 BLOCKED → 不派发、DEFERRED 保留。

另加两条**顺序**的用例（生产入口 ``run_full_verification`` 走一遍）：
收口重算原本跑在「环境受阻重跑」**之前**，而环境受阻重跑会把被环境
BLOCKED 的 Phase 1 验证点翻成 PASSED —— 重算看到的是旧的 BLOCKED，
被延后的全量关卡层仍然一次都没被派发（2026-11-28 生产实测：VP-017 /
VP-018 从未被跑，整轮 SKIPPED，DEFERRED 还带着过期的理由）。

7. ``test_the_late_close_dispatches_the_gate_layer_after_the_env_rerun``
   —— 回归用例（修前必红）：环境受阻重跑把阻塞者翻绿 → **晚收口**必须
   真正派发那条关卡层，且它不再带着旧 BLOCKED 理由留在最终字典里；
8. ``test_the_late_close_does_not_dispatch_when_the_blocker_is_still_red``
   —— 负控：阻塞者仍被挡着 → 晚收口不得派发，DEFERRED 保留。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import VerificationExecutor  # noqa: E402


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _vp(vp_id: str, method: str = "automated_test", **extra) -> Dict[str, Any]:
    """一条验证点（执行器 schema）。"""
    vp = {
        "id": vp_id,
        "method": method,
        "title": f"vp {vp_id}",
        "expected_result": "通过",
        "test_command": f"pytest tests/{vp_id}.py",
    }
    vp.update(extra)
    return vp


def _plan(*vps: Dict[str, Any]) -> Dict[str, Any]:
    return {"vps": list(vps)}


def _make_executor(
    plan: Dict[str, Any],
    tmp_path: Path,
    verdicts: Dict[str, str],
    *,
    round_no: Optional[int] = None,
):
    """执行器：runner 按 ``verdicts[vp_id]`` 返回状态。

    ``round_no`` 是**这一轮**的轮次号：轮次已知时，结论才分得清「本轮的」
    与「上一轮的」。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    calls: List[str] = []

    async def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        vp_id = vp["id"]
        calls.append(vp_id)
        return {
            "status": verdicts.get(vp_id, "PASSED"),
            "reasons": [f"{vp_id} stub"],
            "evidence": {},
        }

    executor = VerificationExecutor(
        verification_plan=plan,
        plan_id="plan-round-truth",
        plan_dir=plan_dir,
        sub_agent_runner=runner,
        verif_repo=None,
        round_no=round_no,
    )
    return executor, calls


def _round_truth(*entries) -> Dict[str, Any]:
    """本轮执行信封（``execution_results`` 那一层）。"""
    return {
        "execution_results": [
            {"id": vp_id, "status": status} for vp_id, status in entries
        ],
    }


def _deferred_entries(executor: VerificationExecutor) -> List[Dict[str, Any]]:
    """本轮账本里所有 DEFERRED 条目。"""
    return [
        dict(verdict)
        for verdict in executor._current_round_bucket().values()
        if isinstance(verdict, dict)
        and str(verdict.get("status") or "") == "DEFERRED"
    ]


def _blocked_round(tmp_path: Path):
    """跑出那一幕：VP-001 BLOCKED → 两条 Phase 2 关卡被门禁延后。

    返回 ``(executor, calls)``。此时：

    * 裁决表里 VP-001 是 **BLOCKED**；
    * 本轮账本里 VP-017 / VP-018 是 **DEFERRED**，
      ``evidence.gate == "verification_phase_2"``；
    * 两条关卡**一次都没被派发**。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-017", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-018", verification_phase=2, phase_order=2, title="全量 E2E"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))

    assert executor._verdicts["VP-001"]["status"] == "BLOCKED", (
        "前置条件：裁决表里 VP-001 是 BLOCKED"
    )
    for gate_id in ("VP-017", "VP-018"):
        entry = executor._current_round_bucket().get(gate_id)
        assert isinstance(entry, dict), f"{gate_id} 必须在本轮账本里"
        assert entry.get("status") == "DEFERRED", (
            f"{gate_id} 必须被门禁延后"
        )
        assert (entry.get("evidence") or {}).get("gate") == (
            "verification_phase_2"
        ), f"{gate_id} 的 DEFERRED 必须由门禁写下"
    assert "VP-017" not in calls, "阻塞者没过时关卡不得执行"
    assert "VP-018" not in calls, "阻塞者没过时关卡不得执行"
    return executor, calls


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def test_the_gate_layer_runs_when_the_round_truth_says_the_blocker_passed(
    tmp_path: Path,
) -> None:
    """回归用例：裁决表还留着旧 BLOCKED，本轮信封说它 PASSED → 必须派发。

    修前必红：``_terminal_status_of`` 只读裁决表，看到 BLOCKED 就把两条
    全量关卡继续挡在门外 —— 它们永远不被派发，整轮 SKIPPED。
    """
    executor, calls = _blocked_round(tmp_path)

    # 本轮的最终结论（执行信封 = 报告读的那份账）：VP-001 PASSED。
    executor.set_round_execution_results(
        _round_truth(("VP-001", "PASSED")), round_no=1,
    )

    dispatched = asyncio.run(executor.rerun_deferred_gate_layers())

    assert "VP-017" in dispatched, (
        "本轮最终结论说阻塞者已经通过，被延后的关卡层必须在本轮派发"
    )
    assert "VP-018" in dispatched, (
        "本轮最终结论说阻塞者已经通过，被延后的关卡层必须在本轮派发"
    )
    assert "VP-017" in calls, "VP-017 必须真的被派发"
    assert "VP-018" in calls, "VP-018 必须真的被派发"
    assert executor._verdicts["VP-017"]["status"] == "PASSED", (
        "补跑必须产出真实结论，不能还留着一条 DEFERRED"
    )
    assert executor._verdicts["VP-018"]["status"] == "PASSED", (
        "补跑必须产出真实结论，不能还留着一条 DEFERRED"
    )


def test_the_gate_layer_stays_deferred_when_the_round_truth_agrees(
    tmp_path: Path,
) -> None:
    """负控：本轮信封也说 BLOCKED → 不派发、DEFERRED 保留。

    门禁没被放宽：信封只让「已经通过」的那条结论说话。
    """
    executor, calls = _blocked_round(tmp_path)

    executor.set_round_execution_results(
        _round_truth(("VP-001", "BLOCKED")), round_no=1,
    )

    dispatched = asyncio.run(executor.rerun_deferred_gate_layers())

    assert dispatched == [], "阻塞者仍挡着时不得派发"
    assert "VP-017" not in calls, "阻塞者仍挡着时关卡不得执行"
    assert "VP-018" not in calls, "阻塞者仍挡着时关卡不得执行"
    assert len(_deferred_entries(executor)) >= 2, "DEFERRED 必须留在账本里"
    assert executor._verdicts["VP-017"]["status"] == "DEFERRED"
    assert executor._verdicts["VP-018"]["status"] == "DEFERRED"


def test_the_gate_layer_stays_deferred_without_a_round_truth(
    tmp_path: Path,
) -> None:
    """负控：没有本轮信封 → 既有行为不变（不派发、保留账本理由）。"""
    executor, calls = _blocked_round(tmp_path)

    dispatched = asyncio.run(executor.rerun_deferred_gate_layers())

    assert dispatched == [], "读不到本轮结论时不得派发"
    assert "VP-017" not in calls, "读不到本轮结论时关卡不得执行"
    assert "VP-018" not in calls, "读不到本轮结论时关卡不得执行"
    assert executor._verdicts["VP-017"]["status"] == "DEFERRED"
    assert executor._verdicts["VP-018"]["status"] == "DEFERRED"


def test_a_round_truth_from_another_round_is_not_used(
    tmp_path: Path,
) -> None:
    """轮次隔离：信封属于别的轮次 → 不采信、不派发。

    上一轮的信封不代表这一轮 —— 拿它放行门禁等于让上一轮的结论替这一轮
    说话（那正是这一层要治的病）。
    """
    executor, calls = _blocked_round(tmp_path)

    executor.set_round_execution_results(
        _round_truth(("VP-001", "PASSED")), round_no=2,
    )

    dispatched = asyncio.run(executor.rerun_deferred_gate_layers())

    assert dispatched == [], "别的轮次的信封不得放行门禁"
    assert "VP-017" not in calls, "别的轮次的信封不得放行门禁"
    assert executor._verdicts["VP-017"]["status"] == "DEFERRED"
    assert executor._verdicts["VP-018"]["status"] == "DEFERRED"


def test_the_gate_layer_runs_when_the_round_ledger_says_the_blocker_passed(
    tmp_path: Path,
) -> None:
    """生产形状：本轮账本说阻塞者 PASSED，跨轮裁决表还留着旧 BLOCKED。

    撕裂的两本账就是现场那两本：**跨轮**裁决表（``_verdicts``）里那条旧
    ``BLOCKED``，和**本轮**账本（``_verdicts_by_round``）里本轮真跑出来的
    ``PASSED``。报告读的是本轮账本（:meth:`collect_verdicts` 的「本轮账本
    优先」），门禁重算读的是跨轮裁决表 —— 门禁因此被一个已经不成立的旧
    结论挡住，被延后的全量关卡层永远不被派发。

    修前必红：门禁只看跨轮裁决表，看到 BLOCKED 就继续挡着。
    """
    executor, calls = _blocked_round(tmp_path)

    # 本轮账本：阻塞者这一轮真跑出来了（PASSED）。
    executor._verdicts_by_round[1]["VP-001"] = {
        "vp_id": "VP-001",
        "status": "PASSED",
        "round": 1,
        "reasons": ["VP-001 stub"],
        "evidence": {},
    }
    # 跨轮裁决表：还留着那条旧 BLOCKED（本轮真跑出来之前写的）。
    assert executor._verdicts["VP-001"]["status"] == "BLOCKED"

    dispatched = asyncio.run(executor.rerun_deferred_gate_layers())

    assert "VP-017" in dispatched, (
        "本轮账本说阻塞者已经通过，被延后的关卡层必须在本轮派发"
    )
    assert "VP-018" in dispatched, (
        "本轮账本说阻塞者已经通过，被延后的关卡层必须在本轮派发"
    )
    assert "VP-017" in calls, "VP-017 必须真的被派发"
    assert "VP-018" in calls, "VP-018 必须真的被派发"


def test_the_gate_layer_stays_deferred_when_the_round_ledger_agrees(
    tmp_path: Path,
) -> None:
    """负控：本轮账本也说它没过 → 不派发、DEFERRED 保留。

    本轮账本只在说「它过了」的时候才放行；说「它没过」时门禁维持既有
    语义（读不到终态成功 → 维持挡着）。
    """
    executor, calls = _blocked_round(tmp_path)

    executor._verdicts_by_round[1]["VP-001"] = {
        "vp_id": "VP-001",
        "status": "BLOCKED",
        "round": 1,
        "reasons": ["VP-001 stub"],
        "evidence": {},
    }

    dispatched = asyncio.run(executor.rerun_deferred_gate_layers())

    assert dispatched == [], "阻塞者仍挡着时不得派发"
    assert "VP-017" not in calls, "阻塞者仍挡着时关卡不得执行"
    assert executor._verdicts["VP-017"]["status"] == "DEFERRED"
    assert executor._verdicts["VP-018"]["status"] == "DEFERRED"


# ---------------------------------------------------------------------------
# 晚收口：环境受阻重跑之后，门禁必须按**最终**结论再判一次
# ---------------------------------------------------------------------------
#
# 事故（2026-11-28 K-Magic 实测）
# ------------------------------
# 收口重算跑在 ``_rerun_environment_blocked_vps`` **之前**：环境受阻重跑
# 把一个被环境 BLOCKED 的 Phase 1 验证点翻成 PASSED，而重算看到的是旧的
# BLOCKED —— 于是被延后的全量门禁层（VP-017 / VP-018）一次都没被派发，
# 报告里那条 DEFERRED 还带着过期理由，整轮 SKIPPED。
#
# 契约
# ----
# 1. 环境受阻重跑**之后**、生成报告**之前**要再收口一次：按本轮**最终**
#    结论重判门禁，被延后的层**必须真的被派发**；
# 2. 派发层的条目回到最终字典里，且**不再**是「带旧 BLOCKED 理由的
#    DEFERRED」；
# 3. 环境受阻重跑刚翻绿的那些验证点**不许**被合并覆盖。


class _Stub:
    """只为了 ``_freshness_cache`` / ``_rebuild_coordinator``。"""

    def clear(self) -> None:
        pass


def _late_close_plan() -> Dict[str, Any]:
    """LLM schema 的计划（``verification_points``）：Phase 1 + 两条关卡。"""
    return {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "automated_test",
                "title": "vp VP-001",
                "expected_result": "通过",
            },
            {
                "id": "VP-017",
                "verification_method": "full_ci",
                "verification_phase": 2,
                "phase_order": 1,
                "title": "全量 Nightly CI",
                "expected_result": "通过",
            },
            {
                "id": "VP-018",
                "verification_method": "e2e",
                "verification_phase": 2,
                "phase_order": 2,
                "title": "全量 E2E",
                "expected_result": "通过",
            },
        ],
    }


def _late_close_agent(tmp_path: Path, executor: Any, plan_data: Dict[str, Any]):
    """一个只桩掉「与本用例无关的协作者」的轮次编排器。

    走的是**生产入口** :meth:`Orchestrator.run_full_verification` ——
    执行器收口、环境受阻重跑、晚收口、生成报告，都在同一个函数里按
    真实的顺序跑一遍（桩掉的全是与门禁/收口无关的协作者：计划生成、
    服务预检、树的指纹、报告的 LLM 判定）。
    """
    from verification_agent import Orchestrator

    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = tmp_path / "plan"
    agent.plan_dir.mkdir(parents=True, exist_ok=True)
    agent.project_dir = tmp_path / "project"
    agent.verif_repo = None
    agent._freshness_cache = _Stub()
    agent._rebuild_coordinator = _Stub()
    agent._clear_verification_state_files = lambda: None
    agent.start_verification_round = lambda round_number: None
    agent.generate_verification_plan = lambda: plan_data
    agent._preflight_service_freshness = lambda plan_data: None
    agent._resolve_service_placeholders = lambda plan_data: plan_data
    agent._current_tree_fingerprint = lambda: "eb64e8a:aaa"
    agent._build_verification_executor = lambda plan_data: executor
    agent._note_vp_capacity = lambda peak: None
    agent._flush_deferred_evidence_checks = lambda executor: []
    agent._quarantine_round_tree_mutation = lambda executor: None
    agent._run_base_drift_preflight = lambda: None
    agent._run_binary_freshness_round_preflight = lambda: None
    agent._run_round_service_preflight = lambda: None
    # 证据核对：本用例里没有互相矛盾的证据 —— 真实实现此时也是原样返回。
    agent._reconcile_round_evidence_conflicts = (
        lambda plan_data, execution_results: execution_results
    )
    agent.generate_verification_report = lambda execution_results: {
        "overall_status": "PASSED",
        "execution_results": execution_results,
    }
    return agent


def _environment_rerun_flips_the_blocker(
    plan_data: Dict[str, Any], execution_results: dict,
) -> dict:
    """环境受阻重跑的**替身**：把被环境 BLOCKED 的 Phase 1 验证点翻绿。

    真实实现（``_rerun_environment_blocked_vps``）做的事就是这一件：服务
    回来之后重跑一遍，把重跑的结论**合并进** ``execution_results``。这里
    只驱动它对轮次顺序的影响 —— 翻绿发生在**晚收口之前**，正是这一缝的
    要害。
    """
    for entry in execution_results.get("execution_results") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id")) == "VP-001":
            entry["status"] = "PASSED"
            entry["actual_result"] = "VP-001 stub (the service came back)"
    return execution_results


def _final_entries(execution_results: dict) -> Dict[str, Dict[str, Any]]:
    return {
        str(entry.get("id")): entry
        for entry in execution_results.get("execution_results") or []
        if isinstance(entry, dict)
    }


def test_the_late_close_dispatches_the_gate_layer_after_the_env_rerun(
    tmp_path: Path,
) -> None:
    """回归用例：环境受阻重跑把阻塞者翻绿 → 晚收口必须派发那条关卡层。

    修前必红：收口重算跑在环境受阻重跑**之前**，看到的是旧的 BLOCKED，
    于是 VP-017 / VP-018 一次都没被派发，报告里那条 DEFERRED 还带着
    「Phase 1 的 VP-001 结论为 BLOCKED」的理由，整轮 SKIPPED。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-017", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-018", verification_phase=2, phase_order=2, title="全量 E2E"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    agent = _late_close_agent(tmp_path, executor, _late_close_plan())
    agent._rerun_environment_blocked_vps = _environment_rerun_flips_the_blocker

    report = agent.run_full_verification(round_number=1)

    # 门禁层**真的**被派发了（执行器跑过了它，不只是账本上换了状态）。
    assert "VP-017" in calls, (
        "晚收口必须在本轮真正派发被延后的关卡层（VP-017 从未被跑过）"
    )
    assert "VP-018" in calls, (
        "晚收口必须在本轮真正派发被延后的关卡层（VP-018 从未被跑过）"
    )

    # 派生行为：最终字典里那条门禁不再是「带旧 BLOCKED 理由的 DEFERRED」。
    entries = _final_entries(report["execution_results"])
    assert entries["VP-017"]["status"] == "PASSED", (
        "派发出去的关卡层必须带着真实结论回到最终字典"
    )
    assert "BLOCKED" not in str(entries["VP-017"].get("actual_result")), (
        "派发出去的关卡层不得还留着旧 BLOCKED 的理由"
    )
    assert entries["VP-018"]["status"] == "PASSED"

    # 环境受阻重跑刚翻绿的那一条**不许**被合并覆盖。
    assert entries["VP-001"]["status"] == "PASSED", (
        "环境受阻重跑刚翻绿的验证点不许被晚收口的合并覆盖"
    )


def test_the_late_close_does_not_dispatch_when_the_blocker_is_still_red(
    tmp_path: Path,
) -> None:
    """负控：阻塞者仍被挡着 → 晚收口不得派发，DEFERRED 保留。

    晚收口只是**补缝**：门禁的判定语义一点没变 —— 阻塞者没有翻绿时，
    被延后的层仍然只能等。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-017", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-018", verification_phase=2, phase_order=2, title="全量 E2E"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    agent = _late_close_agent(tmp_path, executor, _late_close_plan())
    # 环境受阻重跑没有翻绿（服务还是没起来）。
    agent._rerun_environment_blocked_vps = (
        lambda plan_data, execution_results: execution_results
    )

    report = agent.run_full_verification(round_number=1)

    assert "VP-017" not in calls, "阻塞者仍挡着时关卡不得执行"
    assert "VP-018" not in calls, "阻塞者仍挡着时关卡不得执行"
    entries = _final_entries(report["execution_results"])
    assert entries["VP-017"]["status"] == "DEFERRED"
    assert entries["VP-018"]["status"] == "DEFERRED"
