"""2026-09-14 阶段记账不得摧毁本轮成果（现场事故回归）。

现象（round 3）::

    [verification_terminal] entry ... stop_reason=Illegal transition from
      'failed' to 'verification_failed'
    [verification_terminal] step2 SKIPPED (mid-loop)
    [verification_terminal] step3 SKIPPED (mid-loop)

因果: 看门狗先误标 verification_log_stale 把计划置为 ``failed``;
随后裁决**跑完并写出报告**(overall_status=FAILED) 之后,
``check_cycle_conditions`` 走到 "can continue → 生成修复任务" 分支, 里面的
``plan_state.verification_failed()`` 抛 Illegal transition —— 异常掀翻整个
auto-loop, 于是**修复任务与拆分判定全都没跑**, 一轮的成果被记账失败抹掉。

修复原则: 阶段转换是记账, 本轮的真实产物是报告/修复任务/拆分决策 ——
记账冲突只能被记录 + force 纠正, 绝不能向上传播。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verification.orchestrator import VerificationOrchestrator


class _PlanState:
    """PlanState double whose transitions blow up the way the live one did."""

    def __init__(self, fail_methods=()):
        self.calls = []
        self.forced = []
        self._fail = set(fail_methods)

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append(name)
            if name in self._fail:
                raise ValueError(
                    f"Illegal transition from 'failed' to 'verification_failed'"
                )
            return None
        return _call

    def force_set_phase(self, phase):
        self.forced.append(phase)


def _orch(plan_state) -> VerificationOrchestrator:
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_state = plan_state
    return orch


def test_conflicting_phase_is_forced_not_raised():
    ps = _PlanState(fail_methods={"verification_failed"})
    orch = _orch(ps)

    # Must not raise.
    orch._safe_phase_call("verification_failed", "verification_failed")

    assert "verification_failed" in ps.calls
    assert ps.forced == ["verification_failed"], (
        "a conflicting transition must be force-corrected, not propagated"
    )


def test_repair_phase_call_survives_conflict():
    ps = _PlanState(fail_methods={"start_verification_repair"})
    orch = _orch(ps)

    orch._safe_phase_call("start_verification_repair", "verification_repairing")

    assert ps.forced == ["verification_repairing"]


def test_successful_phase_call_is_passthrough():
    ps = _PlanState()
    orch = _orch(ps)
    orch._safe_phase_call("verification_failed", "verification_failed")
    assert ps.calls == ["verification_failed"]
    assert ps.forced == [], "no forcing needed when the transition is legal"


def test_force_set_failure_is_swallowed():
    """Even a broken force_set_phase must not escalate."""

    class _BadPlanState(_PlanState):
        def force_set_phase(self, phase):
            raise RuntimeError("state store unavailable")

    ps = _BadPlanState(fail_methods={"verification_failed"})
    orch = _orch(ps)
    orch._safe_phase_call("verification_failed", "verification_failed")


def test_check_cycle_conditions_does_not_use_raw_phase_transitions():
    """Source pin: the repair-generation path must go through
    ``_safe_phase_call`` — a raw ``plan_state.verification_failed()``
    there is the exact regression that discarded round 3's judgment."""
    import inspect

    src = inspect.getsource(VerificationOrchestrator.check_cycle_conditions)
    assert "self.plan_state.verification_failed()" not in src
    assert "self.plan_state.start_verification_repair()" not in src
    assert '_safe_phase_call("verification_failed"' in src
    assert '_safe_phase_call("start_verification_repair"' in src
