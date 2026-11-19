"""Phase-2 关卡判定不得沿用**上一轮**的结论（§5 回归用例）。

Bug
---
2026-11-02 实测：round 3 里 Phase 2 的 VP-015 因「Phase 1 的 VP-009 为
FAILED」被判 ``DEFERRED``，而本轮 VP-009 实际 PASSED ——
:meth:`VerificationExecutor._terminal_status_of` 读的是 ``_verdicts`` 里
**任意一轮**的结论，于是关卡把上一轮的失败当成本轮的失败。

Fix
---
轮次隔离（任务 12）：每条结论带 ``round`` 戳，关卡只读**本轮**的结论；
读到旧轮结论时记「本轮尚未产生结论」，并把旧结论归档进
``_round_verdicts_history``。

本文件钉住 **layer 级**的回归形态（``_should_short_circuit_layer`` /
``_mark_layer_deferred``）——单元级的轮次过滤由
``test_gate_reads_current_round_verdict.py`` 钉住。

三条用例
--------
1. ``test_last_round_failed_this_round_passed`` —— 上一轮 FAILED、本轮
   PASSED → 关卡**不短路**，依赖它的 Phase 2 VP 不得成立 DEFERRED；
2. ``test_missing_current_round_is_unknown`` —— 本轮没有结论 → 记「本轮尚未
   产生结论」（未知），不拿旧轮的 FAILED 顶替，也不写 DEFERRED；
3. ``test_current_round_failed_still_short_circuits`` —— 本轮 FAILED → 短路
   照旧成立（负控：轮次隔离不得把门禁整个拆了）。

边界
----
``_mark_layer_deferred`` 的既有语义本文件只加钉、不放宽：被拦下的 Phase 2
关卡层记 ``DEFERRED``，``reasons`` 里写明拦截理由，``evidence`` 为
``{"gate": "verification_phase_2"}``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


def _make_executor(tmp_path: Path, *, round_no: int = 3) -> VerificationExecutor:
    """造一个只够门禁判定用的执行器。

    ``round_no=3`` 与 ``executor.round_no = 3`` 是同一个旋钮：显式告知
    「本执行器跑的是第 3 轮」，关卡据此过滤旧轮结论。

    不绑 ``verif_repo`` → 走旧的 JSON 落库路径，测试不碰状态库。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan: Dict[str, Any] = {
        "vps": [_phase1_vp("VP-009"), _phase2_gate_vp()],
    }
    return VerificationExecutor(
        verification_plan=plan,
        plan_id="phase2-gate-current-round",
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: {
            "status": "PASSED",
            "reasons": [],
            "evidence": {},
        },
        round_no=round_no,
    )


def _prepare_gate(
    executor: VerificationExecutor,
) -> List[List[Dict[str, Any]]]:
    """把执行器摆成「一层 Phase 1 + 一层 Phase 2 关卡」。

    返回层表；层序（``_phase_of_layer`` / ``_gate_of_layer``）由
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


def test_last_round_failed_this_round_passed(tmp_path: Path) -> None:
    """上一轮 FAILED、本轮 PASSED → 关卡不短路，Phase 2 VP 不得 DEFERRED。

    这是 2026-11-02 实测形态：上一轮 VP-009 FAILED，本轮 VP-009 PASSED，
    依赖它的 Phase 2 关卡（VP-015 / VP-GATE）**不得**因此被判 DEFERRED。
    """
    executor = _make_executor(tmp_path, round_no=3)
    _prepare_gate(executor)
    deferred_calls = _spy_deferred(executor)

    # 本轮结论（带轮次戳）+ 上一轮留档。
    executor._verdicts = {"VP-009": {"status": "PASSED", "round": 3}}
    executor._round_verdicts_history = {
        "VP-009": [{"status": "FAILED", "round": 2}],
    }

    # 本轮的 PASSED 才是关卡读的那一条。
    assert executor._terminal_status_of("VP-009") == "PASSED"

    # 不短路：Phase 2 放行。
    assert executor._should_short_circuit_layer(0, []) is False

    # 没有拦下 → 没有 DEFERRED，理由也是空的。
    assert deferred_calls == []
    assert executor._gate_block_reason == ""
    assert _deferred_vp_ids(executor) == []
    assert executor._deferred_vps == []
    assert "VP-GATE" not in executor._verdicts

    # 本轮这条是本轮产生的 → 不该被当成"旧轮结论"丢弃。
    assert executor._stale_verdict_ignored == []


def test_missing_current_round_is_unknown(tmp_path: Path) -> None:
    """本轮没有 VP-009 的结论 → 记「本轮尚未产生结论」，不写 DEFERRED。

    旧轮的 FAILED 不是本轮的结论：关卡必须报**未知**，而不是把上一轮的
    失败顶替成本轮的失败。判定本身不写 DEFERRED —— 写 DEFERRED 是
    ``_short_circuit_layer`` 的职责。
    """
    executor = _make_executor(tmp_path, round_no=3)
    _prepare_gate(executor)
    deferred_calls = _spy_deferred(executor)

    # 本轮没有结论：表里只剩上一轮那条。
    executor._verdicts = {"VP-009": {"status": "FAILED", "round": 2}}
    executor._round_verdicts_history = {
        "VP-009": [{"status": "FAILED", "round": 2}],
    }

    # 旧轮结论不可用 → 本轮"还没有结论"（未知），不是 FAILED。
    assert executor._terminal_status_of("VP-009") is None

    reason = executor._blocking_gate_reason()
    assert reason == "Phase 1 的 VP-009 本轮尚未产生结论"
    assert "FAILED" not in reason

    # 未知 ≠ 成功：关卡仍然拦着 Phase 2，但拦截理由来自"未知"。
    assert executor._should_short_circuit_layer(0, []) is True
    assert executor._gate_block_reason == "Phase 1 的 VP-009 本轮尚未产生结论"

    # 判定不写 DEFERRED。
    assert deferred_calls == []
    assert _deferred_vp_ids(executor) == []
    assert executor._deferred_vps == []
    assert "VP-GATE" not in executor._verdicts

    # 丢弃旧轮结论要留痕：哪条验证点、它自称的是第几轮。
    assert executor._stale_verdict_ignored == [
        {"vp_id": "VP-009", "seen_round": 2}
    ]


def test_current_round_failed_still_short_circuits(tmp_path: Path) -> None:
    """本轮 FAILED → 短路照旧成立（负控：轮次隔离不得把门禁拆了）。

    轮次隔离只挡"读旧值"，不挡"本轮真失败"：本轮 FAILED 的 Phase 1 验证点
    仍然拦着 Phase 2，被拦下的关卡层照旧记 DEFERRED。
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

    # 照旧：被拦下的关卡层记 DEFERRED（本轮延后，下轮重跑）。
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