"""Phase 2 门禁：上游 UNKNOWN 时下游标「不可用」，而不是「延后」。

背景
----
:meth:`VerificationExecutor._blocking_gate_reason` 过去只要上游不是成功状态
就返回一条「失败理由」，:meth:`VerificationExecutor._should_short_circuit_layer`
据此把下游的 Phase 2 关卡标成 ``DEFERRED``（本轮延后、下轮重跑）。

但「判定不可用」（``UNKNOWN``）跟「判定失败」（``FAILED``）根本不是一回事：

* ``FAILED`` —— 验了，没过。下游延后，等上游修好再跑，语义正确；
* ``UNKNOWN`` —— **没验出结论**。上游对交付物什么都没说。把它记成「延后」
  等于假装「已知它失败、只是没跑」—— 真相比「延后」更弱：连「它会不会
  失败」都不知道。

arch 决策点 3：判定不可用**不触发** DEFERRED 连锁 —— 下游标不可用
（``UNKNOWN``）并记下原因，而不是伪装成延后。

本文件钉住三条契约（任务书 TDD 规格）：

  1. ``test_unknown_upstream_marks_downstream_unknown`` —— UNKNOWN 上游 →
     下游 ``UNKNOWN``，reason 前缀 ``Phase 2 关卡不可用：``；
  2. ``test_failed_upstream_still_defers_downstream`` —— FAILED 上游 → 下游
     仍 ``DEFERRED``（不许把关卡改松）；
  3. ``test_unknown_absent_from_deferred_vps`` —— 下游**不**写入
     ``_deferred_vps``（UNKNOWN 不进任何桶，见 :mod:`verification_buckets`）。

外加两条边界：同层同时有 FAILED 与 UNKNOWN → FAILED 理由优先（仍 DEFERRED）；
上游 BLOCKED → 维持 DEFERRED。

末尾两条是**跨轮**契约：上面那些用例全都只跑一轮，所以它们看不见「上一轮
留下的结论挡住这一轮的结论」这个坑（详见下面那一节的说明）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import VerificationExecutor  # noqa: E402
from verification_phases import PHASE_CORE, PHASE_FINAL_GATE  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verdict(status: str) -> Dict[str, Any]:
    """一条最小合法的裁决（schema: status / reasons / evidence）。"""
    return {"status": status, "reasons": [], "evidence": {}}


def _phase1_vp(vp_id: str, **extra: Any) -> Dict[str, Any]:
    """一条 Phase 1（普通）验证点。"""
    vp: Dict[str, Any] = {
        "id": vp_id,
        "title": f"verify {vp_id}",
        "verification_method": "automated_test",
        "verification_phase": 1,
    }
    vp.update(extra)
    return vp


def _phase2_gate_vp(vp_id: str = "VP-018", phase_order: int = 1) -> Dict[str, Any]:
    """一条 Phase 2 全量关卡（门禁本身，不是前置条件）。"""
    return {
        "id": vp_id,
        "title": "full suite",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": phase_order,
    }


def _executor(
    tmp_path: Path,
    vps: List[Dict[str, Any]],
) -> VerificationExecutor:
    """造一个执行器（真实构造 —— 门禁判定与写裁决都要用到完整状态）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return VerificationExecutor(
        verification_plan={"vps": vps},
        plan_id="phase2-gate-unknown",
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: _verdict("PASSED"),
    )


def _run_gate(ex: VerificationExecutor, gate_vp: Dict[str, Any]) -> None:
    """走生产路径：门禁判定 → 短路该层。

    ``BaseExecutor.run`` 的顺序是「先问 :meth:`_should_short_circuit_layer`，
    再调 :meth:`_short_circuit_layer`」，这里照抄。
    """
    ex._phase_of_layer = [PHASE_CORE, PHASE_FINAL_GATE]
    ex._gate_of_layer = [None, gate_vp]
    assert ex._should_short_circuit_layer(0, []) is True, (
        "上游没过时 Phase 2 必须被拦下"
    )
    ex._short_circuit_layer([gate_vp], "short-circuit after layer 0")


def _downstream(ex: VerificationExecutor, vp_id: str) -> Dict[str, Any]:
    """下游关卡那条裁决。"""
    for row in ex.collect_verdicts():
        if row["vp_id"] == vp_id:
            return row
    raise AssertionError(f"{vp_id} 没有产生裁决：{ex.collect_verdicts()}")


# ---------------------------------------------------------------------------
# 1. UNKNOWN 上游 → 下游 UNKNOWN
# ---------------------------------------------------------------------------


def test_unknown_upstream_marks_downstream_unknown(tmp_path: Path) -> None:
    """上游 UNKNOWN → 下游关卡标 UNKNOWN，理由说清「不可用」而不是「延后」。"""
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), gate])

    ex.record_verdict("VP-017", _verdict("UNKNOWN"))

    _run_gate(ex, gate)

    row = _downstream(ex, "VP-018")
    assert row["status"] == "UNKNOWN", (
        "上游没验出结论时，下游不得被记成「延后」—— 那是在假装已知它失败"
    )
    assert row["reasons"] == [
        "Phase 2 关卡不可用：Phase 1 的 VP-017 结论为 UNKNOWN"
    ], "理由必须说清是「不可用」，并带上上游那条具体理由"


# ---------------------------------------------------------------------------
# 2. FAILED 上游 → 仍然 DEFERRED（不许把关卡改松）
# ---------------------------------------------------------------------------


def test_failed_upstream_still_defers_downstream(tmp_path: Path) -> None:
    """上游 FAILED → 下游仍 DEFERRED（上游确有失败，下游等它修好再跑）。"""
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), gate])

    ex.record_verdict("VP-017", _verdict("FAILED"))

    _run_gate(ex, gate)

    row = _downstream(ex, "VP-018")
    assert row["status"] == "DEFERRED", (
        "真 FAILED 时下游必须仍被延后 —— 本次改动只针对「不知道」"
    )
    assert row["reasons"] == [
        "Phase 2 关卡延后：Phase 1 的 VP-017 结论为 FAILED"
    ]


# ---------------------------------------------------------------------------
# 3. UNKNOWN 的下游不写入 _deferred_vps
# ---------------------------------------------------------------------------


def test_unknown_absent_from_deferred_vps(tmp_path: Path) -> None:
    """下游记 UNKNOWN → 不进 ``_deferred_vps``，也不进任何其它桶。"""
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), gate])

    ex.record_verdict("VP-017", _verdict("UNKNOWN"))

    _run_gate(ex, gate)

    assert list(ex._deferred_vps) == [], (
        "UNKNOWN 的下游不得写进 _deferred_vps —— 它不是「延后」，是「不可用」"
    )
    assert ex.failed_vps == [], (
        "UNKNOWN 不得折算成失败（那会给一个没人验过的判据生产修复任务）"
    )
    assert ex.completed_vps == [], (
        "UNKNOWN 不得折算成通过（那会把没验过的东西记成「验证通过」）"
    )
    assert ex.skipped_vps == []


# ---------------------------------------------------------------------------
# 边界：同层 FAILED + UNKNOWN → FAILED 理由优先
# ---------------------------------------------------------------------------


def test_failed_reason_wins_over_unknown_in_same_layer(tmp_path: Path) -> None:
    """同层同时有 FAILED 与 UNKNOWN → 取 FAILED 那条理由，下游仍 DEFERRED。"""
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(
        tmp_path,
        [_phase1_vp("VP-016"), _phase1_vp("VP-017"), gate],
    )

    # VP-016 在计划里排在前面，但它只是「没验出结论」。
    ex.record_verdict("VP-016", _verdict("UNKNOWN"))
    ex.record_verdict("VP-017", _verdict("FAILED"))

    _run_gate(ex, gate)

    row = _downstream(ex, "VP-018")
    assert row["status"] == "DEFERRED", (
        "同层只要有一条真 FAILED，下游就仍是「延后」而不是「不可用」"
    )
    assert row["reasons"] == [
        "Phase 2 关卡延后：Phase 1 的 VP-017 结论为 FAILED"
    ], "理由必须取 FAILED 那条（确定失败优先于不知道）"


# ---------------------------------------------------------------------------
# 边界：确知失败优先于没验出结论（跨清单）
# ---------------------------------------------------------------------------


def test_definite_failure_upstream_beats_unknown_in_another_list(
    tmp_path: Path,
) -> None:
    """Phase 1 是 UNKNOWN、更早的关卡是 FAILED → 下游仍 DEFERRED。

    「确知失败优先于没验出结论」这条判据必须**跨清单**生效：Phase 1 那条
    UNKNOWN 只说明「Phase 1 的一半没验出结论」，而更早的关卡确实失败了 ——
    下游等的是那个失败被修好，语义是「延后」而不是「不可用」。
    """
    early = _phase2_gate_vp("VP-016", phase_order=1)
    late = _phase2_gate_vp("VP-018", phase_order=2)
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), early, late])

    ex.record_verdict("VP-017", _verdict("UNKNOWN"))
    ex.record_verdict("VP-016", _verdict("FAILED"))

    _run_gate(ex, late)

    row = _downstream(ex, "VP-018")
    assert row["status"] == "DEFERRED", (
        "上游确知失败时下游必须延后 —— 那半边「没验出结论」不构成理由"
    )
    assert row["reasons"] == [
        "Phase 2 关卡延后：更早的全量关卡 VP-016 结论为 FAILED"
    ], "理由要点名真正卡住下游的那条确知失败"


# ---------------------------------------------------------------------------
# 边界：上游 BLOCKED → 维持 DEFERRED
# ---------------------------------------------------------------------------


def test_blocked_upstream_still_defers_downstream(tmp_path: Path) -> None:
    """上游 BLOCKED → 维持 DEFERRED（既有行为不变）。"""
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), gate])

    ex.record_verdict("VP-017", _verdict("BLOCKED"))

    _run_gate(ex, gate)

    row = _downstream(ex, "VP-018")
    assert row["status"] == "DEFERRED"
    assert row["reasons"] == [
        "Phase 2 关卡延后：Phase 1 的 VP-017 结论为 BLOCKED"
    ]


# ---------------------------------------------------------------------------
# 生产路径：executor.run() 走一遍真实的分层与门禁
# ---------------------------------------------------------------------------


def test_unknown_upstream_marks_gate_unknown_in_a_real_run(tmp_path: Path) -> None:
    """``executor.run()`` 走完整条链路：上游 UNKNOWN → 关卡不执行、记 UNKNOWN。

    前面的用例是**直接**调门禁方法的；这一条把 ``BaseExecutor.run`` 的
    分层 → 门禁判定 → 短路整条链路都走一遍，钉住「接线」也是对的。
    """
    gate = _phase2_gate_vp("VP-018")
    plan = {"vps": [_phase1_vp("VP-017"), gate]}
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    calls: List[str] = []

    def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(vp["id"])
        return _verdict("UNKNOWN")

    ex = VerificationExecutor(
        verification_plan=plan,
        plan_id="phase2-gate-unknown-run",
        plan_dir=plan_dir,
        sub_agent_runner=runner,
        verif_repo=None,
    )

    asyncio.run(ex.run(max_parallel=1))

    assert calls == ["VP-017"], "上游没验出结论时关卡不得执行"
    verdict = ex._verdicts["VP-018"]
    assert verdict["status"] == "UNKNOWN"
    assert verdict["reasons"] == [
        "Phase 2 关卡不可用：Phase 1 的 VP-017 结论为 UNKNOWN"
    ]
    assert "VP-018" not in ex._deferred_vps


# ---------------------------------------------------------------------------
# 跨轮：上一轮的结论不许挡住这一轮更准确的结论
# ---------------------------------------------------------------------------
#
# 前面那些用例全都只跑一轮，所以它们看不见这个坑：
#
#   * 第 1 轮：上游 FAILED → 门禁记 DEFERRED（进 ``_deferred_vps``）；
#   * 第 2 轮：上游改成 UNKNOWN → 门禁必须改记 UNKNOWN。
#
# 坑在判重上：:meth:`_mark_layer_unknown` 原来读的是 ``item_id in
# self._verdicts``，而 ``_verdicts`` 每个 VP 只留**最新**一条、跨轮继承靠
# 它 —— 第 1 轮那条 DEFERRED 就在里面，于是第 2 轮的「不可用」整段空转。
# 门禁在本轮账本里一条结论都没有，收尾闸（:meth:`_ensure_current_round_
# entries`）再把它补成 DEFERRED（「本轮未产生结论」）—— 「上游没验出结论」
# 又被说成了「本轮延后」。
#
# 轮末那两步（:meth:`rerun_deferred_gate_layers` 与
# :meth:`_ensure_current_round_entries`）正是上一轮把结论写回 DEFERRED 的
# 地方，所以它们必须一起被钉住。


def _current_round_status(ex: VerificationExecutor, vp_id: str) -> str:
    """**本轮**那条裁决的状态。

    跨轮判重必须看本轮，不能看 ``_verdicts``（那是每个 VP 的最新一条，
    跨轮继承靠它）。
    """
    row = ex._current_round_verdicts().get(vp_id)
    assert isinstance(row, dict), f"{vp_id} 本轮没有裁决"
    return str(row.get("status") or "")


def test_unknown_upstream_across_rounds_is_not_rewritten_as_deferred(
    tmp_path: Path,
) -> None:
    """第 1 轮 DEFERRED → 第 2 轮上游 UNKNOWN → 门禁必须改记 UNKNOWN。

    判重看**本轮**：上一轮那条 DEFERRED 不许挡住这一轮更准确的「不可用」。
    """
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), gate])

    # ---- 第 1 轮：上游 FAILED → 门禁延后
    ex._round_no = 1
    ex.record_verdict("VP-017", _verdict("FAILED"))
    _run_gate(ex, gate)
    assert _current_round_status(ex, "VP-018") == "DEFERRED"
    assert "VP-018" in ex._deferred_vps, "延后的关卡要留在延后桶里"

    # ---- 第 2 轮：上游改成 UNKNOWN（判定不可用）
    ex._round_no = 2
    ex.record_verdict("VP-017", _verdict("UNKNOWN"))
    _run_gate(ex, gate)

    # 轮末那两步：上一轮把结论写回 DEFERRED 就是它们干的
    asyncio.run(ex.rerun_deferred_gate_layers())
    ex._ensure_current_round_entries()

    assert _current_round_status(ex, "VP-018") == "UNKNOWN", (
        "第 1 轮那条 DEFERRED 不许挡住第 2 轮的「不可用」"
    )
    assert ex._current_round_verdicts()["VP-018"]["reasons"] == [
        "Phase 2 关卡不可用：Phase 1 的 VP-017 结论为 UNKNOWN"
    ], "理由必须说清是「不可用」，并带上上游那条具体结论"
    assert "VP-018" not in ex._deferred_vps, (
        "UNKNOWN 的关卡不得留在延后桶里 —— 它不是「延后」，是「不可用」"
    )
    assert ex.failed_vps == [], "UNKNOWN 不得折算成失败"
    assert ex.completed_vps == [], "UNKNOWN 不得折算成通过"
    assert ex.skipped_vps == []


def test_deferred_gate_recheck_marks_unknown_when_blocker_becomes_unknown(
    tmp_path: Path,
) -> None:
    """本轮内阻塞者从 FAILED 变成 UNKNOWN → 门禁重算必须改记 UNKNOWN。

    门禁重算那条路原来只拿**理由**、把**状态**丢了，于是上游变成 UNKNOWN
    时它照样把门禁写成 DEFERRED —— 「判定不可用」被说成了「本轮延后」。
    """
    gate = _phase2_gate_vp("VP-018")
    ex = _executor(tmp_path, [_phase1_vp("VP-017"), gate])
    ex._round_no = 1

    ex.record_verdict("VP-017", _verdict("FAILED"))
    _run_gate(ex, gate)
    assert _current_round_status(ex, "VP-018") == "DEFERRED"

    # 本轮内：阻塞者从「确知失败」变成「没验出结论」
    # （record_verdict 收尾会触发门禁重算）
    ex.record_verdict("VP-017", _verdict("UNKNOWN"))

    assert _current_round_status(ex, "VP-018") == "UNKNOWN", (
        "上游没验出结论时，门禁重算必须改记「不可用」，不是「延后」"
    )
    assert ex._current_round_verdicts()["VP-018"]["reasons"] == [
        "Phase 2 关卡不可用：Phase 1 的 VP-017 结论为 UNKNOWN"
    ]
    assert "VP-018" not in ex._deferred_vps, (
        "UNKNOWN 的关卡不得留在延后桶里 —— 它不是「延后」，是「不可用」"
    )
