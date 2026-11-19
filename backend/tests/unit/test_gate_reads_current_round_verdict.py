"""Phase-2 门禁只读**本轮**的结论。

Bug
---
§5 实测：round 3 里 VP-015 因「Phase 1 的 VP-009 为 FAILED」被判 DEFERRED，
而本轮 VP-009 实际 PASSED。``_terminal_status_of`` 读的是 ``_verdicts`` 里
**任意一轮**的结论，于是关卡拿上一轮的失败当本轮的失败。

Fix
---
结论按轮次隔离：

  * :meth:`VerificationExecutor.record_verdict` 给每条结论盖上轮次戳
    （``round``）；
  * :meth:`VerificationExecutor._terminal_status_of` 只认**本轮**的结论 ——
    读到旧轮结论时当「本轮尚未产生结论」处理，并记一条
    ``stale_verdict_ignored``（``vp_id`` + 它自称的轮次）；
  * :meth:`VerificationExecutor._blocking_gate_reason` 在轮次已知时把理由
    写成「本轮尚未产生结论」，不再报旧轮的结论。

边界
----
``round_no`` 未设置 → 保持旧行为（不做轮次过滤）：调用方不知道自己是第几轮
时，宁可沿用旧结论，也不要凭空把一轮的结论作废。

覆盖范围
--------
单元级（``_terminal_status_of`` / ``_blocking_gate_reason`` /
``_stale_verdict_ignored``）**和** layer 级（``_should_short_circuit_layer`` /
``_mark_layer_deferred``）都在本文件里钉住 —— 只钉单元级是不够的：只断言
辅助函数的返回值时，"关卡总是 DEFERRED"这种坏实现照样全绿。

  * layer 级 · 本轮 PASSED
    （``test_last_round_failed_this_round_passed_does_not_defer_gate``）：
    上一轮 FAILED、本轮 PASSED → 关卡**不短路**，Phase 2 的验证点不得被判
    DEFERRED；
  * layer 级 · 本轮 FAILED（``test_current_round_failed_still_defers_gate``）：
    短路与 DEFERRED 照旧成立（负控：证明本文件对"关卡总是 DEFERRED"这种坏
    实现有检测力）。

layer 级的形态与 ``test_phase2_gate_current_round_regression.py`` 一致；两处
都留一份，是因为验收要单独钉住这一个文件。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import VerificationExecutor  # noqa: E402
from verification_phases import PHASE_CORE, PHASE_FINAL_GATE  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _phase1_vp(vp_id: str) -> Dict[str, Any]:
    """一条 Phase 1（普通）验证点。"""
    return {
        "id": vp_id,
        "title": f"verify {vp_id}",
        "verification_method": "automated_test",
        "verification_phase": 1,
    }


def _phase2_gate_vp(vp_id: str = "VP-GATE") -> Dict[str, Any]:
    """一条 Phase 2 全量关卡（门禁本身，不是前置条件）。"""
    return {
        "id": vp_id,
        "title": "end to end",
        "verification_method": "e2e",
        "verification_phase": 2,
        "phase_order": 1,
    }


def _verdict(status: str) -> Dict[str, Any]:
    """一条最小合法的裁决（schema: status / reasons / evidence）。"""
    return {"status": status, "reasons": [], "evidence": {}}


def _make_executor(
    tmp_path: Path,
    *,
    round_no: Optional[int] = None,
    vps: Optional[List[Dict[str, Any]]] = None,
) -> VerificationExecutor:
    """造一个只够门禁判定用的执行器。

    不绑 ``verif_repo`` → 走旧的 JSON 落库路径，测试不碰状态库。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan: Dict[str, Any] = {
        "vps": vps if vps is not None else [_phase1_vp("VP-009"), _phase2_gate_vp()]
    }
    return VerificationExecutor(
        verification_plan=plan,
        plan_id="gate-current-round",
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: _verdict("PASSED"),
        round_no=round_no,
    )


def _prepare_gate(
    executor: VerificationExecutor,
) -> List[List[Dict[str, Any]]]:
    """把执行器摆成「一层 Phase 1 + 一层 Phase 2 关卡」。

    层序（``_phase_of_layer`` / ``_gate_of_layer``）由
    ``_build_execution_layers`` 建立 —— 门禁判定读的就是它。
    """
    layers = executor._build_execution_layers(executor._build_items_from_plan())
    assert executor._phase_of_layer == [PHASE_CORE, PHASE_FINAL_GATE]
    assert [len(layer) for layer in layers] == [1, 1]
    return layers


def _spy_deferred(
    executor: VerificationExecutor,
) -> List[Tuple[str, List[str]]]:
    """盯住 ``_mark_layer_deferred``：记录调用，但**照常执行原实现**。

    照常执行是刻意的：既要「有没有被调用」的证据，也要「调用之后到底写了
    什么」的证据 —— 换句话说，DEFERRED 裁决本身仍是生产代码写出来的，
    这里只在外面加一只眼睛。
    """
    calls: List[Tuple[str, List[str]]] = []
    original = executor._mark_layer_deferred

    def spy(layer: List[Dict[str, Any]], reason: str) -> None:
        calls.append((reason, [str(item.get("id")) for item in layer]))
        return original(layer, reason)

    executor._mark_layer_deferred = spy  # type: ignore[assignment]
    return calls


def _deferred_vp_ids(executor: VerificationExecutor) -> List[str]:
    """当前裁决表里被判 DEFERRED 的验证点。"""
    return [
        str(vp_id)
        for vp_id, verdict in executor._verdicts.items()
        if isinstance(verdict, dict) and verdict.get("status") == "DEFERRED"
    ]


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_stale_round_verdict_not_used(tmp_path: Path) -> None:
    """上一轮的 FAILED 不得当本轮的结论用。"""
    executor = _make_executor(tmp_path, round_no=3)
    executor._verdicts = {"VP-009": {"status": "FAILED", "round": 2}}

    # 旧轮结论不可用 → 本轮还没有结论。
    assert executor._terminal_status_of("VP-009") is None
    assert (
        executor._blocking_gate_reason()
        == "Phase 1 的 VP-009 本轮尚未产生结论"
    )
    # 丢弃旧轮结论要留痕：哪条验证点、它自称的是第几轮。
    assert executor._stale_verdict_ignored == [
        {"vp_id": "VP-009", "seen_round": 2}
    ]


def test_current_round_passed_unblocks(tmp_path: Path) -> None:
    """本轮 PASSED 放行 Phase 2。"""
    executor = _make_executor(tmp_path, round_no=3)

    executor.record_verdict("VP-009", _verdict("PASSED"))

    # 写下来的结论带轮次戳。
    assert executor._verdicts["VP-009"]["round"] == 3
    assert executor._terminal_status_of("VP-009") == "PASSED"
    assert executor._blocking_gate_reason() == ""
    assert executor._stale_verdict_ignored == []


def test_no_round_no_keeps_behaviour(tmp_path: Path) -> None:
    """没给轮次号 → 不做轮次过滤，旧行为原样保留。"""
    executor = _make_executor(tmp_path)
    executor._verdicts = {"VP-009": {"status": "FAILED", "round": 2}}

    assert executor._terminal_status_of("VP-009") == "FAILED"
    assert executor._blocking_gate_reason() == "Phase 1 的 VP-009 结论为 FAILED"
    assert executor._stale_verdict_ignored == []


# ---------------------------------------------------------------------------
# Cases — layer 级（关卡判定本身）
# ---------------------------------------------------------------------------


def test_last_round_failed_this_round_passed_does_not_defer_gate(
    tmp_path: Path,
) -> None:
    """上一轮 FAILED、本轮 PASSED → 关卡不短路，Phase 2 VP 不得 DEFERRED。

    layer 级的形态（§5 回归用例）：上一轮 VP-009 FAILED，本轮 VP-009
    PASSED —— 依赖它的 Phase 2 关卡（VP-GATE）**不得**因此被判 DEFERRED。
    只断言辅助函数的返回值是不够的：那样"关卡总是 DEFERRED"的坏实现照样
    全绿。这里断言的是关卡**做了什么**（短路与否、DEFERRED 有没有写出来）。
    """
    executor = _make_executor(tmp_path, round_no=3)
    layers = _prepare_gate(executor)
    deferred_calls = _spy_deferred(executor)

    # 本轮结论（带轮次戳）+ 上一轮留档。
    executor._verdicts = {"VP-009": {"status": "PASSED", "round": 3}}
    executor._round_verdicts_history = {
        "VP-009": [{"status": "FAILED", "round": 2}],
    }

    # 关卡读的是本轮那条 PASSED。
    assert executor._terminal_status_of("VP-009") == "PASSED"

    # 不短路：Phase 2 放行。
    short_circuit = executor._should_short_circuit_layer(0, [])
    assert short_circuit is False

    # 编排器的动作：只有短路时才把关卡层标 DEFERRED。
    if short_circuit:
        executor._short_circuit_layer(layers[1], executor._gate_block_reason)

    # 没有拦下 → 没有 DEFERRED，理由也是空的。
    assert deferred_calls == []
    assert executor._gate_block_reason == ""
    assert _deferred_vp_ids(executor) == []
    assert executor._deferred_vps == []
    assert "VP-GATE" not in executor._verdicts

    # 本轮这条是本轮产生的 → 不该被当成"旧轮结论"丢弃。
    assert executor._stale_verdict_ignored == []


def test_current_round_failed_still_defers_gate(tmp_path: Path) -> None:
    """本轮 FAILED → 短路与 DEFERRED 照旧成立（负控）。

    轮次隔离只挡"读旧值"，不挡"本轮真失败"：本轮 FAILED 的 Phase 1 验证点
    仍然拦着 Phase 2，被拦下的关卡层照旧记 DEFERRED。这条负控是上一条的
    对照面 —— 两条一起才说明本文件对"关卡短路 / 不短路"有检测力。
    """
    executor = _make_executor(tmp_path, round_no=3)
    layers = _prepare_gate(executor)
    deferred_calls = _spy_deferred(executor)

    executor._verdicts = {"VP-009": {"status": "FAILED", "round": 3}}
    executor._round_verdicts_history = {
        "VP-009": [{"status": "FAILED", "round": 2}],
    }

    assert executor._terminal_status_of("VP-009") == "FAILED"

    # 短路：Phase 2 被拦下。
    assert executor._should_short_circuit_layer(0, []) is True
    assert executor._gate_block_reason == "Phase 1 的 VP-009 结论为 FAILED"

    executor._short_circuit_layer(layers[1], executor._gate_block_reason)

    assert len(deferred_calls) == 1
    reason, layer_ids = deferred_calls[0]
    assert layer_ids == ["VP-GATE"]
    assert reason == "Phase 2 关卡延后：Phase 1 的 VP-009 结论为 FAILED"

    gate_verdict = executor._verdicts["VP-GATE"]
    assert gate_verdict["status"] == "DEFERRED"
    assert gate_verdict["reasons"] == [
        "Phase 2 关卡延后：Phase 1 的 VP-009 结论为 FAILED"
    ]
    assert gate_verdict["evidence"] == {"gate": "verification_phase_2"}
    assert executor._deferred_vps == ["VP-GATE"]