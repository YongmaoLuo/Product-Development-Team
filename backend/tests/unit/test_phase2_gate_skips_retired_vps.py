"""Phase-2 门禁不得向**已被作废/移除**的 Phase 1 验证点要结论。

Bug
---
:meth:`VerificationExecutor._build_items_from_plan` 构造本轮真正会跑的
工作集时会跳过两类验证点：计划自己作废的（``obsolete`` /
``superseded_by``）与被移除的（主题外移除）。它们**永远不会被派发**，
因此也永远产生不了结论。

:meth:`VerificationExecutor._blocking_gate_reason` 却遍历**原始**计划列表
（``_plan_points()``），不看这两个标记，于是把一条永远不会有结论的验证点
当成 Phase 2 的前置条件 —— "Phase 1 的 VP-009 尚未产生结论" —— 把
Phase 2（e2e / full_ci）永久锁死。

Fix
---
门禁的前置集合 = 执行器的工作集：``_blocking_gate_reason`` 跳过
``_vp_retired_marker`` / ``_is_removed_vp`` 判定的验证点。

第三个用例是**负控**：既没作废也没被移除、且确实没有结论的 Phase 1 验证点
**必须**仍然拦得住 Phase 2（否则这个修复就是把门禁整个拆了）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import VerificationExecutor  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _phase2_gate_vp(vp_id: str = "VP-GATE") -> Dict[str, Any]:
    """一条 Phase 2 全量关卡（门禁本身，不是前置条件）。"""
    return {
        "id": vp_id,
        "title": "end to end",
        "verification_method": "e2e",
        "verification_phase": 2,
        "phase_order": 1,
    }


def _executor(
    vps: List[Dict[str, Any]],
    verdicts: Optional[Dict[str, Dict[str, Any]]] = None,
    removed_vp_ids: Optional[set] = None,
) -> VerificationExecutor:
    """造一个**只够门禁判定用**的执行器。

    不走 ``__init__``（它会去装载/初始化状态库、读盘），而是绕过它只设置
    门禁这条路径真正读到的那几个属性：

      * ``verification_plan`` —— ``_plan_points()`` 读它；
      * ``plan`` —— ``_plan_vps()``（= ``_build_items_from_plan``）读它；
      * ``_verdicts`` —— ``_terminal_status_of()`` 读它；
      * ``_removed_vp_ids`` —— ``_is_removed_vp()`` 读它。
    """
    executor = object.__new__(VerificationExecutor)
    plan: Dict[str, Any] = {"vps": vps}
    executor.verification_plan = plan
    executor.plan = plan
    executor._verdicts = dict(verdicts or {})
    executor._removed_vp_ids = set(removed_vp_ids or ())
    return executor


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_obsolete_phase1_vp_does_not_block_phase2_gate() -> None:
    """作废的 Phase 1 验证点永远不会有结论，不得因此锁死 Phase 2。"""
    executor = _executor(
        [
            _phase1_vp("VP-001", obsolete=True),
            _phase2_gate_vp(),
        ]
    )

    # 前置条件：这条 VP 确实不在执行器的工作集里（所以它永远不会有结论）。
    working_set = [vp["id"] for vp in executor._build_items_from_plan()]
    assert "VP-001" not in working_set

    assert executor._blocking_gate_reason() == ""


def test_superseded_phase1_vp_does_not_block_phase2_gate() -> None:
    """被拆分取代的 Phase 1 验证点同样不会被派发，不得锁死 Phase 2。"""
    executor = _executor(
        [
            _phase1_vp("VP-002", superseded_by=["VP-002-A", "VP-002-B"]),
            _phase2_gate_vp(),
        ]
    )

    working_set = [vp["id"] for vp in executor._build_items_from_plan()]
    assert "VP-002" not in working_set

    assert executor._blocking_gate_reason() == ""


def test_live_phase1_vp_without_verdict_still_blocks_phase2_gate() -> None:
    """负控：真正在跑的 Phase 1 验证点没有结论时，Phase 2 仍必须被拦下。"""
    executor = _executor(
        [
            _phase1_vp("VP-003"),
            _phase2_gate_vp(),
        ]
    )

    working_set = [vp["id"] for vp in executor._build_items_from_plan()]
    assert "VP-003" in working_set

    reason = executor._blocking_gate_reason()
    assert reason, "没有结论的 Phase 1 验证点必须继续拦着 Phase 2"
    assert "VP-003" in reason
