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
