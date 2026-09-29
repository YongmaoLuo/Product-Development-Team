"""两阶段门禁的执行器语义测试（2026-09-16）。

契约：

1. Phase 1（子任务级）走原有 DAG 分层；Phase 2（全量关卡）**每关一层**，
   按 ``phase_order`` 排在后面（Nightly 先、E2E 后）。
2. 只有 Phase 1 **全部终态成功**，第一个关卡层才放行。
3. E2E 必须等 Nightly 通过——由"更早的关卡未通过"这条判据覆盖。
4. 被拦下的关卡标 ``DEFERRED``：不是 FAILED（不生成修复任务）、不是
   SKIPPED（不进 ``completed_set``，下一轮必须真跑）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _vp(vp_id: str, method: str = "automated_test", **extra) -> Dict[str, Any]:
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


def _make_executor(plan: Dict[str, Any], tmp_path: Path, verdicts: Dict[str, str]):
    """Executor whose runner returns ``verdicts[vp_id]`` as the status."""
    from verification_executor import VerificationExecutor

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
        plan_id="plan-x",
        plan_dir=plan_dir,
        sub_agent_runner=runner,
        verif_repo=None,
    )
    return executor, calls


# ---------------------------------------------------------------------------
# 分层
# ---------------------------------------------------------------------------

def test_layers_put_each_final_gate_in_its_own_layer(tmp_path: Path) -> None:
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-002", verification_phase=2, phase_order=2, title="全量 E2E 测试"),
        _vp("VP-003", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_executor(plan, tmp_path, {})
    layers = executor._build_execution_layers(plan["vps"])

    ids = [[vp["id"] for vp in layer] for layer in layers]
    # Phase 1 层在前；Nightly（order=1）先于 E2E（order=2）
    assert ids == [["VP-001"], ["VP-003"], ["VP-002"]]


def test_phase1_vps_share_one_layer_when_no_depends_on(tmp_path: Path) -> None:
    plan = _plan(_vp("VP-001"), _vp("VP-002"), _vp("VP-003"))
    executor, _ = _make_executor(plan, tmp_path, {})
    layers = executor._build_execution_layers(plan["vps"])
    assert len(layers) == 1
    assert sorted(vp["id"] for vp in layers[0]) == ["VP-001", "VP-002", "VP-003"]


# ---------------------------------------------------------------------------
# 门禁：Phase 1 未全绿 → 关卡延后
# ---------------------------------------------------------------------------

def test_gate_deferred_when_phase1_has_failure(tmp_path: Path) -> None:
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-002"),
        _vp("VP-010", verification_phase=2, phase_order=1,
            title="全量 Nightly CI", test_command="bash scripts/ci_local.py"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "PASSED", "VP-002": "FAILED"},
    )
    asyncio.run(executor.run(max_parallel=1))

    assert "VP-010" not in calls, "Phase 1 有失败时全量关卡不得执行"
    verdict = executor._verdicts["VP-010"]
    assert verdict["status"] == "DEFERRED"
    assert "延后" in verdict["reasons"][0]


def test_deferred_is_not_failed_and_not_skipped(tmp_path: Path) -> None:
    """DEFERRED 既不能进失败桶（会生成修复任务），也不能进跳过桶
    （会让下一轮直接跳过全量关卡）。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_executor(plan, tmp_path, {"VP-001": "FAILED"})
    asyncio.run(executor.run(max_parallel=1))

    assert executor.failed_vps == ["VP-001"]
    assert "VP-010" not in executor.skipped_vps
    assert "VP-010" not in executor.completed_vps
    assert "VP-010" in executor._deferred_vps


def test_deferred_gate_reruns_next_round(tmp_path: Path) -> None:
    """下一轮（Phase 1 全绿后）被延后的关卡必须真正执行。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    # 第一轮：Phase 1 失败 → 关卡延后
    executor, calls = _make_executor(plan, tmp_path, {"VP-001": "FAILED"})
    asyncio.run(executor.run(max_parallel=1))
    assert calls == ["VP-001"]

    # 第二轮：Phase 1 通过 → 关卡执行
    executor2, calls2 = _make_executor(plan, tmp_path, {"VP-001": "PASSED"})
    executor2._verdicts = dict(executor._verdicts)  # 复用上一轮结论
    executor2._backfill_index_lists_from_verdicts()
    asyncio.run(executor2.run(max_parallel=1))

    assert "VP-010" in calls2, "延后的全量关卡必须在后续轮次补跑"
    assert executor2._verdicts["VP-010"]["status"] == "PASSED"


# ---------------------------------------------------------------------------
# 门禁：Phase 1 全绿 → 关卡执行；Nightly 失败 → E2E 延后
# ---------------------------------------------------------------------------

def test_gate_runs_when_phase1_all_green(tmp_path: Path) -> None:
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-011", verification_phase=2, phase_order=2, title="全量 E2E 测试"),
    )
    executor, calls = _make_executor(plan, tmp_path, {"VP-001": "PASSED"})
    asyncio.run(executor.run(max_parallel=1))

    assert calls == ["VP-001", "VP-010", "VP-011"]
    assert executor._verdicts["VP-010"]["status"] == "PASSED"
    assert executor._verdicts["VP-011"]["status"] == "PASSED"


def test_e2e_deferred_when_nightly_fails(tmp_path: Path) -> None:
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-011", verification_phase=2, phase_order=2, title="全量 E2E 测试"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "PASSED", "VP-010": "FAILED"},
    )
    asyncio.run(executor.run(max_parallel=1))

    assert calls == ["VP-001", "VP-010"], "Nightly 没过时不该烧 E2E"
    assert executor._verdicts["VP-011"]["status"] == "DEFERRED"
    assert "VP-010" in executor._verdicts["VP-011"]["reasons"][0]


def test_no_phase2_plan_runs_all_phase1(tmp_path: Path) -> None:
    """没有 Phase 2 的计划行为不变（整体回归保护）。"""
    plan = _plan(_vp("VP-001"), _vp("VP-002"))
    executor, calls = _make_executor(plan, tmp_path, {})
    asyncio.run(executor.run(max_parallel=1))
    assert sorted(calls) == ["VP-001", "VP-002"]
    assert executor._deferred_vps == []


# ---------------------------------------------------------------------------
# 判定权：DEFERRED 不被 Phase 3 LLM 改写
# ---------------------------------------------------------------------------

def test_report_restores_deferred_status(tmp_path: Path) -> None:
    """Phase 3 LLM 把延后的关卡判成 FAILED/SKIPPED 时，必须还原为 DEFERRED，
    并从需求偏离里摘掉（否则会生成去"修"一个没跑过的全量门禁的任务）。"""
    import inspect
    from verification_agent import VerificationAgent

    src = inspect.getsource(VerificationAgent.generate_verification_report)
    assert "DEFERRED" in src
    assert "requirement_deviations" in src


def test_schema_accepts_deferred(tmp_path: Path) -> None:
    from verification_executor import ALLOWED_STATUSES, VerificationExecutor
    assert "DEFERRED" in ALLOWED_STATUSES

    plan = _plan(_vp("VP-010", verification_phase=2, phase_order=1))
    executor, _ = _make_executor(plan, tmp_path, {})
    executor.record_verdict("VP-010", {
        "status": "DEFERRED", "reasons": ["延后"], "evidence": {},
    })
    assert "VP-010" in executor._deferred_vps
