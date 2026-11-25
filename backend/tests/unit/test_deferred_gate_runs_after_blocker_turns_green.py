"""被门禁延后的层，阻塞者翻绿之后必须在本轮真正跑起来（2026-11-25）。

事故
----
2026-11-25 两例（``20261116-K-Magic-DNS`` / ``20261111-DataViewer-2``）：
一轮里已执行的验证点全部 PASSED、0 FAILED，两个 Phase 2 全量关卡却是
``DEFERRED``，理由写着「Phase 1 的 VP-001 结论为 BLOCKED」—— 而 VP-001 的
**最终**结论是 PASSED。整轮因此被算成 SKIPPED：绿的交付物拿不到 PASS。

根因
----
门禁判定的输入不是「Phase 1 的**最终**结论」，而是某一瞬的快照：

* 那一瞬 VP-001 还是 BLOCKED → 关卡层被延后，理由是**当时**的状态；
* 后来 VP-001 翻绿 → 那一层必须在本轮真正派发，并产出**真实结论**；
* 旧实现里「本轮被延后过哪些层」只记在**进程内**的表
  （``_deferred_gate_layers``）。执行器重建（续跑 / 换进程 / 重启）之后
  那张表是空的 —— 于是没人记得「这一层还没跑」，它也就永远不会被派发，
  那条 DEFERRED 带着**过期**的理由留在账本里。

契约（本文件钉住）
----------------
1. 门禁只按 Phase 1 的**最终结论**放行：阻塞者已是终态成功时，被延后的
   那一层必须**在本轮**真正派发，并产出真实结论；
2. **不得放宽门禁**：阻塞者仍是 BLOCKED / FAILED / 未产出结论时，那一层
   继续 DEFERRED，且理由必须点名**当前**的阻塞者；
3. 延后理由**不得**点名一个已经不挡路的结论 —— 理由落在报告与状态里，
   撒谎就是欺骗。

五条用例
--------
1. ``test_a_layer_deferred_by_a_blocker_that_turns_green_runs_in_the_same_round``
   —— 阻塞者翻绿 → 那一层在本轮被派发、有真实结论、账本里没有引用旧
   BLOCKED 的 DEFERRED 条目；
2. ``test_a_blocker_that_stays_blocked_keeps_the_layer_deferred``
   —— 阻塞者一直 BLOCKED → 那一层仍然 DEFERRED（负控：门禁没被放宽）；
3. ``test_a_failed_blocker_keeps_the_layer_deferred``
   —— 阻塞者 FAILED → 那一层仍然 DEFERRED（负控）；
4. ``test_a_deferral_reason_never_names_a_blocker_that_turned_green``
   —— 阻塞者翻绿之后，延后理由当场改口，不许留在旧阻塞者身上；
5. ``test_the_round_close_runs_a_gate_whose_blocker_turned_green_after_the_round``
   —— 轮次**收口**（编排器那一侧）再判一次：执行器自己收口之后才翻绿的
   阻塞者，同样必须让那一层在本轮跑起来。
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

    ``round_no`` 是**这一轮**的轮次号：轮次已知时，裁决才分得清「本轮的」
    与「上一轮的」（``VERDICT_ROUND_FIELD``）。

    同一个 ``tmp_path`` 建出来的两个执行器共用一个 ``plan_dir``，所以第二个
    会把第一个写下的裁决从状态文件里读回来 —— **轮次续跑**就是这样看见
    上一段过程的结论的（也是「进程内的延后层表丢了、账本还在」的那一幕）。
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
        plan_id="plan-gate-green",
        plan_dir=plan_dir,
        sub_agent_runner=runner,
        verif_repo=None,
        round_no=round_no,
    )
    return executor, calls


def _deferred_entries(executor: VerificationExecutor) -> List[Dict[str, Any]]:
    """本轮账本里所有 DEFERRED 条目。"""
    return [
        dict(verdict)
        for verdict in executor._current_round_bucket().values()
        if isinstance(verdict, dict)
        and str(verdict.get("status") or "") == "DEFERRED"
    ]


def _quotes_blocked(entry: Dict[str, Any], vp_id: str) -> bool:
    """这条 DEFERRED 条目是否点名了「{vp_id} 结论为 BLOCKED」。"""
    reasons = " ".join(str(r) for r in (entry.get("reasons") or []))
    actual = str(entry.get("actual_result") or "")
    blob = f"{reasons} {actual}"
    return vp_id in blob and "BLOCKED" in blob


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def test_a_layer_deferred_by_a_blocker_that_turns_green_runs_in_the_same_round(
    tmp_path: Path,
) -> None:
    """阻塞者翻绿 → 被延后的那一层在本轮真正派发，并产出真实结论。

    这一轮的第一段：VP-001 是 BLOCKED → Phase 2 关卡层被延后，理由是**当时**
    的状态（「Phase 1 的 VP-001 结论为 BLOCKED」）。

    第二段：**执行器重建**（续跑 / 换进程 / 重启 —— 生产上真实发生过），
    同一个 ``plan_dir``、同一轮号。「本轮被延后过哪些层」只记在进程内的
    表里，重建之后那张表是空的；账本里那条 DEFERRED 还在 —— 它才是
    「这一层还没跑」的真相。

    然后阻塞者翻绿（结论被改写），走重算路径：那一层必须被派发、产出真实
    结论，账本里不许再留着引用旧 BLOCKED 的 DEFERRED 条目。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-GATE", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )

    # 第一段：Phase 1 的 VP-001 是 BLOCKED → 关卡层被延后。
    first, first_calls = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    asyncio.run(first.run(max_parallel=1))
    assert "VP-GATE" not in first_calls, "阻塞者没过时关卡不得执行"
    assert first._verdicts["VP-GATE"]["status"] == "DEFERRED"
    assert _quotes_blocked(first._verdicts["VP-GATE"], "VP-001"), (
        "延后理由必须点名当时的阻塞者"
    )

    # 第二段：执行器重建（同轮、同一个 plan_dir）—— 账本还在，内存表没了。
    second, second_calls = _make_executor(plan, tmp_path, {}, round_no=1)
    assert second._deferred_gate_layers == [], (
        "进程内的延后层表是空的：重建之后没人记得「那一层还没跑」"
    )

    # 阻塞者翻绿。
    second.record_verdict(
        "VP-001",
        {"status": "PASSED", "reasons": ["重试通过"], "evidence": {}},
    )

    # 重算路径：前提没了 → 那一层必须在本轮真正跑起来。
    asyncio.run(second.rerun_deferred_gate_layers())

    assert "VP-GATE" in second_calls, (
        "阻塞者已经是终态成功，被延后的关卡层必须在本轮补跑"
    )
    assert second._verdicts["VP-GATE"]["status"] == "PASSED", (
        "补跑必须产出真实结论，不能还留着一条 DEFERRED"
    )
    assert not [
        entry for entry in _deferred_entries(second)
        if _quotes_blocked(entry, "VP-001")
    ], "账本里不许留着引用旧 BLOCKED 结论的 DEFERRED 条目"


def test_a_blocker_that_stays_blocked_keeps_the_layer_deferred(
    tmp_path: Path,
) -> None:
    """负控：阻塞者一直是 BLOCKED → 那一层仍然 DEFERRED，门禁没被放宽。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-GATE", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )

    first, _ = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    asyncio.run(first.run(max_parallel=1))
    assert first._verdicts["VP-GATE"]["status"] == "DEFERRED"

    # 执行器重建（同轮）—— 阻塞者没有翻绿。
    second, second_calls = _make_executor(plan, tmp_path, {}, round_no=1)
    asyncio.run(second.rerun_deferred_gate_layers())

    assert "VP-GATE" not in second_calls, "阻塞者还挡着时关卡不得执行"
    assert second._verdicts["VP-GATE"]["status"] == "DEFERRED"
    assert _quotes_blocked(second._verdicts["VP-GATE"], "VP-001"), (
        "理由必须点名**当前**的阻塞者"
    )


def test_a_failed_blocker_keeps_the_layer_deferred(tmp_path: Path) -> None:
    """负控：阻塞者 FAILED → 那一层仍然 DEFERRED，门禁没被放宽。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-GATE", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )

    first, _ = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=1,
    )
    asyncio.run(first.run(max_parallel=1))
    assert first._verdicts["VP-GATE"]["status"] == "DEFERRED"

    # 执行器重建（同轮）—— 阻塞者没有翻绿。
    second, second_calls = _make_executor(plan, tmp_path, {}, round_no=1)
    asyncio.run(second.rerun_deferred_gate_layers())

    assert "VP-GATE" not in second_calls, "阻塞者还挡着时关卡不得执行"
    assert second._verdicts["VP-GATE"]["status"] == "DEFERRED"
    reason = " ".join(
        str(r) for r in (second._verdicts["VP-GATE"]["reasons"] or [])
    )
    assert "VP-001" in reason and "FAILED" in reason, (
        "理由必须点名**当前**的阻塞者"
    )


def test_a_deferral_reason_never_names_a_blocker_that_turned_green(
    tmp_path: Path,
) -> None:
    """阻塞者翻绿之后，延后理由当场改口 —— 不许留在旧阻塞者身上。

    理由落在报告与状态里。一条写着「Phase 1 的 VP-001 结论为 BLOCKED」的
    延后记录，而 VP-001 已经 PASSED —— 那是在撒谎：读报告的人会以为这一轮
    是被一个已经翻绿的结论挡住的。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-GATE", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )

    first, _ = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    asyncio.run(first.run(max_parallel=1))
    assert _quotes_blocked(first._verdicts["VP-GATE"], "VP-001")

    second, _ = _make_executor(plan, tmp_path, {}, round_no=1)
    second.record_verdict(
        "VP-001",
        {"status": "PASSED", "reasons": ["重试通过"], "evidence": {}},
    )

    assert not [
        entry for entry in _deferred_entries(second)
        if _quotes_blocked(entry, "VP-001")
    ], "阻塞者已经翻绿，延后理由不许还点名它"


def test_the_round_close_runs_a_gate_whose_blocker_turned_green_after_the_round(
    tmp_path: Path,
) -> None:
    """轮次**收口**再判一次：执行器自己收口之后才翻绿的阻塞者也算数。

    执行器的收口（``_run_round`` 末尾）跑在挂起核对重跑**之前** —— 那一刻
    之后，这一轮的结论还可能被改写。于是真正的轮次收口（编排器那一侧）必须
    再判一次门禁：阻塞者已经终态成功，那一层就必须在本轮真正跑起来。
    """
    from verification_agent import VerificationAgent

    plan = _plan(
        _vp("VP-001"),
        _vp("VP-GATE", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )

    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))
    assert "VP-GATE" not in calls, "阻塞者没过时关卡不得执行"
    assert executor._verdicts["VP-GATE"]["status"] == "DEFERRED"

    # 执行器收口之后，阻塞者才翻绿（结论被改写）。
    executor.record_verdict(
        "VP-001",
        {"status": "PASSED", "reasons": ["重试通过"], "evidence": {}},
    )
    assert "VP-GATE" not in calls, "执行器自己的收口已经过去了"

    # 轮次收口。
    agent = VerificationAgent.__new__(VerificationAgent)
    agent._close_round_gate_layers(executor)

    assert "VP-GATE" in calls, (
        "轮次收口时阻塞者已是终态成功，被延后的关卡层必须在本轮补跑"
    )
    assert executor._verdicts["VP-GATE"]["status"] == "PASSED", (
        "补跑必须产出真实结论，不能还留着一条 DEFERRED"
    )
