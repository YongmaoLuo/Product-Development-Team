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
from typing import Any, Dict, List, Optional

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


def _make_executor(
    plan: Dict[str, Any],
    tmp_path: Path,
    verdicts: Dict[str, str],
    *,
    round_no: Optional[int] = None,
):
    """Executor whose runner returns ``verdicts[vp_id]`` as the status.

    ``round_no`` 给的是**这一轮**的轮次号：轮次已知时，裁决才分得清
    「本轮的」与「上一轮的」（``VERDICT_ROUND_FIELD``）。省略时执行器去问
    状态库 —— 这里它什么也不知道 —— 于是轮次是 0（"未知"）。

    两个用同一个 ``tmp_path`` 建出来的执行器共用一个 ``plan_dir``，所以
    第二个会把第一个的裁决从状态文件里读回来 —— 下一轮就是这样看见上一轮
    的结论的。
    """
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
        round_no=round_no,
    )
    return executor, calls


def _state_file(tmp_path: Path) -> Path:
    return tmp_path / "plan" / "verification_executor_state.json"


def _carry_round_verdicts(executor, tmp_path: Path) -> None:
    """让下一轮的执行器能继承这一轮的结论（补上命令指纹）。

    跨轮继承在生产路径上靠的是盘上那份 state 文件。这里补上**命令指纹**
    —— 生产路径（``_record_result``）写出来的结论不带它，而分桶侧的判据
    是「没有指纹 = 失效」：下一轮装载时这些结论会被当成「出处过期」摘掉，
    继承就等于没继承。
    """
    import json

    from verification_verdict_provenance import stamp as stamp_command

    state_file = _state_file(tmp_path)
    data = json.loads(state_file.read_text(encoding="utf-8"))

    def _stamp(verdict: Dict[str, Any], vp_id: str) -> Dict[str, Any]:
        return stamp_command(
            dict(verdict), executor._current_command_for(vp_id),
        )

    data["verdicts"] = {
        vp_id: _stamp(verdict, vp_id)
        for vp_id, verdict in (data.get("verdicts") or {}).items()
    }
    data["verdict_rounds"] = {
        str(round_no): {
            vp_id: _stamp(verdict, vp_id)
            for vp_id, verdict in (verdicts or {}).items()
        }
        for round_no, verdicts in (data.get("verdict_rounds") or {}).items()
    }
    state_file.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8",
    )


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
# 跨轮：被延后的关卡必须重新盖上**本轮**轮次戳（2026-11-09 · VP-024）
# ---------------------------------------------------------------------------
#
# 契约：Phase 2 关卡被门禁拦下时记 ``DEFERRED``。它**不是终态** ——
# 下一次 Phase 1 修好的轮次必须真跑。于是每一轮它都要有一条**属于本轮**
# 的 DEFERRED 结论。
#
# 踩过的坑（VP-024，第 4/5 轮）：``_mark_layer_deferred`` 见到裁决表里
# 已经有这条验证点就 ``continue``，于是上一轮那条 DEFERRED 永远停在历史轮。
# 报告侧按轮次筛（``_verdict_round_of(v) == current_round``）就看不见它 ——
# Phase 3 的判定 LLM 面对「没有执行记录」只能写成 FAILED，而 FAILED 又会
# 为一条**根本没被允许执行**的验证点生成修复任务：这就是「派发失败不得
# 变成无限循环」要禁止的循环。


def _deferred_round_pair(tmp_path: Path):
    """跑两轮（Phase 1 都失败），返回 ``(第2轮执行器, 第3轮执行器)``。

    第 3 轮从盘上水合第 2 轮的结论（同一个 ``plan_dir``）—— 这正是生产
    路径：每一轮都是新的执行器实例，结论从状态库/状态文件读回来。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    ex2, _ = _make_executor(plan, tmp_path, {"VP-001": "FAILED"}, round_no=2)
    asyncio.run(ex2.run(max_parallel=1))
    ex3, _ = _make_executor(plan, tmp_path, {"VP-001": "FAILED"}, round_no=3)
    asyncio.run(ex3.run(max_parallel=1))
    return ex2, ex3


def test_deferred_gate_is_restamped_for_current_round(tmp_path: Path) -> None:
    """上一轮延后的关卡，本轮再次被拦下时必须带上**本轮**轮次戳。"""
    from verification_executor import VerificationExecutor

    ex2, ex3 = _deferred_round_pair(tmp_path)

    assert ex2._verdicts["VP-010"]["status"] == "DEFERRED"
    assert VerificationExecutor._verdict_round_of(ex2._verdicts["VP-010"]) == 2

    verdict = ex3._verdicts["VP-010"]
    assert verdict["status"] == "DEFERRED"
    assert VerificationExecutor._verdict_round_of(verdict) == 3, (
        "延后的关卡必须重新盖上本轮轮次戳 —— 停在历史轮的话，报告侧按轮次"
        "筛就看不见它，Phase 3 只能把「没跑」写成 FAILED"
    )


def test_deferred_gate_survives_report_side_round_filter(tmp_path: Path) -> None:
    """报告侧的轮次筛选必须看得见这条 DEFERRED（否则它从本轮结果里消失）。"""
    from verification_executor import VerificationExecutor

    _, ex3 = _deferred_round_pair(tmp_path)
    current_round = 3

    kept = [
        v for v in ex3.collect_verdicts()
        if VerificationExecutor._verdict_round_of(v) == current_round
    ]
    kept_ids = {v["vp_id"] for v in kept}
    assert "VP-010" in kept_ids, (
        "被延后的关卡必须出现在本轮结果里 —— 缺席等于「没有执行记录」"
    )
    assert [v for v in kept if v["vp_id"] == "VP-010"][0]["status"] == "DEFERRED"


def test_deferred_gate_stays_deferred_next_round(tmp_path: Path) -> None:
    """本轮再次延后后，它仍在 DEFERRED 桶里，不进任何终态桶。"""
    _, ex3 = _deferred_round_pair(tmp_path)

    assert "VP-010" in ex3._deferred_vps
    assert "VP-010" not in ex3.completed_vps
    assert "VP-010" not in ex3.skipped_vps
    assert "VP-010" not in ex3.failed_vps


def test_deferred_gate_actually_runs_when_phase1_turns_green(tmp_path: Path) -> None:
    """延后过的关卡，Phase 1 修好之后的那一轮必须**真跑**（由真实输出判定）。

    这条是整条链的终点：VP-024 那一类全量关卡被延后之后，不是永远延后，
    而是等 Phase 1 全绿的那一轮真的把整仓门禁跑起来。

    （回归护栏：这一条在修复前后都成立 —— 它锁的是「延后 ≠ 永不跑」，
    而上面三条锁的是「延后必须被记成本轮的 DEFERRED」。）
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    # 第 2 轮：Phase 1 失败 → 关卡被延后（**没跑**）
    ex2, calls2 = _make_executor(plan, tmp_path, {"VP-001": "FAILED"}, round_no=2)
    asyncio.run(ex2.run(max_parallel=1))
    assert "VP-010" not in calls2
    assert ex2._verdicts["VP-010"]["status"] == "DEFERRED"

    # 第 3 轮：Phase 1 全绿 → 关卡必须真跑
    ex3, calls3 = _make_executor(
        plan, tmp_path, {"VP-001": "PASSED", "VP-010": "PASSED"}, round_no=3,
    )
    asyncio.run(ex3.run(max_parallel=1))

    assert "VP-010" in calls3, "延后过的关卡必须在 Phase 1 修好后的轮次真跑"
    assert ex3._verdicts["VP-010"]["status"] == "PASSED"


def test_gate_does_not_overwrite_terminal_verdict(tmp_path: Path) -> None:
    """负控：门禁不得覆盖**终态**结论（PASSED/FAILED/SKIPPED …）。

    DEFERRED 是非终态（下一轮必须重跑），所以要重新盖戳；其它状态是终态，
    门禁拦下时也不该把它们改写成 DEFERRED。
    """
    from verification_executor import VerificationExecutor

    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    ex2, _ = _make_executor(plan, tmp_path, {"VP-001": "FAILED"}, round_no=2)
    # 手工给关卡一条**终态**结论（模拟上一轮它真的跑过并通过了）。
    ex2.record_verdict("VP-010", {
        "status": "PASSED", "reasons": ["上一轮真跑过"], "evidence": {},
    })
    ex3, _ = _make_executor(plan, tmp_path, {"VP-001": "FAILED"}, round_no=3)
    asyncio.run(ex3.run(max_parallel=1))

    assert ex3._verdicts["VP-010"]["status"] == "PASSED", (
        "终态结论不得被门禁改写成 DEFERRED"
    )


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


# ---------------------------------------------------------------------------
# 跨轮记账：被本轮延后的关卡，在本轮账本里必须看得见
# ---------------------------------------------------------------------------
#
# 2026-11-06（VP-016 根因）：``_mark_layer_deferred`` 的跳过判据曾经是
# **跨轮**的 ``item_id in self._verdicts`` ——「这个 VP 已经有裁决了（哪怕
# 是上一轮的 DEFERRED）就不写新的」。于是从第二轮起，被门禁延后的关卡在
# 本轮**一条裁决都没有**：执行器不写、轮次作用域过滤按轮次戳把它筛掉、
# ``execution_results`` 里查无此 VP。
#
# 判定层（Phase 3 LLM）看到的是「计划里有 VP-016、没有执行结果」——一张
# 空白支票——于是替它编了一个 FAILED（missing → high），框架再照着这条
# 「失败」去买一条「去修一个没跑过的门禁」的任务。
#
# 不变量：**被本轮延后的关卡，在本轮账本里必须看得见。**


def _make_round_executor(
    plan: Dict[str, Any],
    plan_dir: Path,
    verdicts: Dict[str, str],
    round_no: int,
):
    """一个绑定轮次的执行器 —— 每轮一个自己的 ``plan_dir``。

    轮次必须显式绑定：跨轮记账的判据就是「**本轮**有没有裁决」，轮次
    未知时执行器按旧口径退回去（那正是缺陷的藏身处）。
    """
    from verification_executor import VerificationExecutor

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
        round_no=round_no,
    )
    return executor, calls


def _two_deferred_rounds(
    plan: Dict[str, Any],
    tmp_path: Path,
    verdicts: Dict[str, str],
):
    """连跑两轮，两轮都把同一个 Phase 2 关卡延后；返回第二个执行器。

    第二轮把第一轮的结论**水合**回来 —— 那正是真实的跨轮继承（进程重启
    后从状态库水合），也正是缺陷发作的条件：延后判据一旦跨轮，被延后的
    关卡在第二轮就一条裁决都不写。
    """
    executor1, _ = _make_round_executor(
        plan, tmp_path / "round1", verdicts, round_no=1,
    )
    asyncio.run(executor1.run(max_parallel=1))

    executor2, calls2 = _make_round_executor(
        plan, tmp_path / "round2", verdicts, round_no=2,
    )
    for vp_id, verdict in executor1._verdicts.items():
        executor2._store_verdict(vp_id, dict(verdict))
    executor2._backfill_index_lists_from_verdicts()
    asyncio.run(executor2.run(max_parallel=1))
    return executor1, executor2, calls2


def test_deferred_gate_writes_a_verdict_in_every_round(tmp_path: Path) -> None:
    """第二轮再次延后时，本轮账本里必须有一条**本轮盖戳**的 DEFERRED。

    修复前：``_verdicts_by_round[2]`` 里没有它、``_deferred_vps`` 是空的
    ——「本轮没问过」这件事从账本里彻底消失。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    _, executor2, _ = _two_deferred_rounds(
        plan, tmp_path, {"VP-001": "FAILED"},
    )

    round2 = executor2._verdicts_by_round.get(2, {})
    assert "VP-010" in round2, "第二轮的账本里必须有本轮盖戳的 DEFERRED"
    assert round2["VP-010"]["status"] == "DEFERRED"
    assert round2["VP-010"]["round"] == 2, "轮次戳必须是本轮，不是上一轮"
    assert "VP-010" in executor2._deferred_vps


def test_deferred_gate_stays_in_the_current_round_ledger(tmp_path: Path) -> None:
    """本轮视图（``_current_round_verdicts``）在第二轮仍要读得到那条延后。

    修复前：第二轮的账本里只有 Phase 1 的结论 —— 关卡整个消失，判定层
    拿到的就是「有计划条目、无执行结果」。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    _, executor2, _ = _two_deferred_rounds(
        plan, tmp_path, {"VP-001": "FAILED"},
    )

    ledger = executor2._current_round_verdicts()
    assert "VP-010" in ledger, "被本轮延后的关卡必须留在本轮账本里"
    assert ledger["VP-010"]["status"] == "DEFERRED"
    assert ledger["VP-010"]["round"] == 2


def test_report_projects_an_unjudged_gate_as_deferred(tmp_path: Path) -> None:
    """计划里有、本轮没有裁决的 Phase 2 关卡，必须以 DEFERRED 进账本。

    修复前它整个从账本里消失（``deferred_ids`` 是空集），判定层拿到的是
    一张空白支票，只能替它编一个状态。
    """
    from verification_agent import VerificationAgent


    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_round_executor(
        plan, tmp_path / "round2", {"VP-001": "FAILED"}, round_no=2,
    )
    # 本轮没有裁决：只有**上一轮**的 DEFERRED 被水合回来。
    executor._store_verdict("VP-010", {
        "status": "DEFERRED",
        "reasons": ["Phase 2 关卡延后：Phase 1 的 VP-001 结论为 FAILED"],
        "evidence": {},
        "round": 1,
    })

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None
    envelope = agent._build_execution_results_from_executor(
        {"verification_points": plan["vps"]}, executor,
    )

    statuses = {er["id"]: er["status"] for er in envelope["execution_results"]}
    assert statuses.get("VP-010") == "DEFERRED", (
        "本轮没被问过的 Phase 2 关卡必须以 DEFERRED 的身份留在账本里"
    )


def test_report_restores_deferred_and_drops_the_deviation_of_an_unjudged_gate(
    tmp_path: Path,
) -> None:
    """判定层把「本轮没问过」的关卡写成 FAILED + missing 偏离时，
    报告层必须把它还原成 DEFERRED，并把那条需求偏离摘掉。

    修复前：报告里留着 ``FAILED`` 与 ``type=missing / severity=high``
    —— 失败统计被污染，修复任务被引到错误靶子（去「修」一个没跑过的
    全量门禁）。
    """
    from unittest.mock import Mock

    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    coding_tool = Mock()
    coding_tool.query_json.return_value = {
        "overall_status": "FAILED",
        "summary": "判定层自己编的状态",
        "verification_results": [
            {
                "id": "VP-001", "status": "FAILED",
                "actual_result": "boom", "evidence": "e",
            },
            {
                "id": "VP-010", "status": "FAILED",
                "actual_result": "端到端验收未执行，功能未验证", "evidence": "e",
            },
        ],
        "requirement_deviations": [
            {
                "verification_point_id": "VP-010",
                "type": "missing",
                "severity": "high",
                "description": "本轮没有任何产物",
            },
        ],
    }
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=coding_tool,
    )

    execution_results = {
        "verification_points": [
            _vp("VP-001"),
            _vp("VP-010", verification_phase=2, phase_order=1,
                title="全量 Nightly CI"),
        ],
        "execution_results": [
            {
                "id": "VP-001", "status": "FAILED",
                "actual_result": "boom", "evidence": "e",
            },
        ],
    }
    report = agent.generate_verification_report(execution_results, retry_llm=1)

    entries = {
        vr.get("id"): vr.get("status")
        for vr in report.get("verification_results", [])
    }
    assert entries.get("VP-010") == "DEFERRED", (
        "本轮没被问过的 Phase 2 关卡不得报成 FAILED"
    )
    deviations = [
        d for d in (report.get("requirement_deviations") or [])
        if d.get("verification_point_id") == "VP-010"
    ]
    assert deviations == [], "延后的关卡不得留下需求偏离"


def test_deferred_gate_never_enters_the_failure_history(tmp_path: Path) -> None:
    """被延后的关卡不进失败集合 → 不进失败历史 → 不买修复任务。

    失败历史与指纹的入口只认 ``FAILED / timeout / SPLIT / hard_timeout``；
    报告里那条 ``FAILED`` 一旦留下，这条「没跑过的门禁」就会按
    ``deliverable_defect`` 买下一条修复任务，并在 legacy-v2 规则下把判定层
    的散文哈希成指纹 —— 措辞一变指纹就变，循环于是永远说「根因已演进」。
    """
    from unittest.mock import Mock

    from verification.orchestrator import VerificationOrchestrator
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    coding_tool = Mock()
    coding_tool.query_json.return_value = {
        "overall_status": "FAILED",
        "summary": "判定层自己编的状态",
        "verification_results": [
            {
                "id": "VP-001", "status": "FAILED",
                "actual_result": "boom", "evidence": "e",
            },
            {
                "id": "VP-010", "status": "FAILED",
                "actual_result": "端到端验收未执行，功能未验证", "evidence": "e",
            },
        ],
        "requirement_deviations": [
            {
                "verification_point_id": "VP-010",
                "type": "missing",
                "severity": "high",
                "description": "本轮没有任何产物",
            },
        ],
    }
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=coding_tool,
    )
    execution_results = {
        "verification_points": [
            _vp("VP-001"),
            _vp("VP-010", verification_phase=2, phase_order=1,
                title="全量 Nightly CI"),
        ],
        "execution_results": [
            {
                "id": "VP-001", "status": "FAILED",
                "actual_result": "boom", "evidence": "e",
            },
        ],
    }
    report = agent.generate_verification_report(execution_results, retry_llm=1)

    orchestrator = VerificationOrchestrator.__new__(VerificationOrchestrator)
    failed = orchestrator._extract_failed_vps_with_evidence(report)

    assert "VP-010" not in failed, "被延后的关卡不得进失败集合"
    assert "VP-001" in failed, "真失败仍然要留在失败集合里"


# ---------------------------------------------------------------------------
# VP-029：真失败不得被洗成延后
# ---------------------------------------------------------------------------
#
# 「机器跑不动」不得被记成「代码坏了」；反过来同样成立：**真的跑过并红了
# 的关卡也不得被记成「没跑过」**。真失败被洗成延后，修复环就永远不收敛
# （没人去修它）。
#
# 判据的第一位是「**本轮有没有裁决**」：关卡自己有一条真实裁决，它的身份
# 就是那条裁决 —— 前置红不改变它「被问过」的事实。


def _report_layer_gate_ids(points: List[Dict[str, Any]], ledger: Dict[str, str]):
    """报告层算出来的「本轮没被问过」的 Phase 2 关卡 id 集合。"""
    from verification_agent import _deferred_gate_ids

    return _deferred_gate_ids({"verification_points": points}, ledger)


def test_deferred_gate_ids_keeps_a_gate_with_a_real_verdict_out_of_deferred() -> None:
    """关卡自己有一条真裁决 ⇒ 它被问过 ⇒ 前置红也不许把它算成「没被问过」。"""
    points = [
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    ]
    ledger = {"VP-001": "FAILED", "VP-015": "FAILED"}

    assert _report_layer_gate_ids(points, ledger) == set(), (
        "真跑过并红了的关卡不得被算成「本轮没被问过」"
    )


def test_deferred_gate_ids_marks_a_gate_without_any_verdict_as_deferred() -> None:
    """负控：本轮一条裁决都没有的关卡仍然必须落 DEFERRED。"""
    points = [
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    ]

    assert _report_layer_gate_ids(points, {}) == {"VP-015"}, (
        "本轮没有裁决的关卡必须仍然是「没被问过」"
    )


def test_report_keeps_a_gate_failed_when_it_really_ran_and_failed(
    tmp_path: Path,
) -> None:
    """账本里真跑过并红了的关卡，报告里必须仍是 FAILED。

    修复前：报告层的「规则 4」只看前置红不红，前置红 ⇒ 判定成「本轮没被
    问过」⇒ 无条件写成 DEFERRED ⇒ 真失败被洗成延后 ⇒ 修复环不收敛。
    """
    from unittest.mock import Mock

    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    points = [
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    ]

    coding_tool = Mock()
    coding_tool.query_json.return_value = {
        "overall_status": "FAILED",
        "summary": "判定层自己编的状态",
        "verification_results": [
            {
                "id": "VP-001", "status": "FAILED",
                "actual_result": "phase 1 red", "evidence": "e",
            },
            {
                "id": "VP-015", "status": "FAILED",
                "actual_result": "full_ci exit_code=1", "evidence": "e",
            },
        ],
        "requirement_deviations": [],
    }
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=coding_tool,
    )

    execution_results = {
        "verification_points": points,
        "execution_results": [
            {
                "id": "VP-001", "status": "FAILED",
                "actual_result": "phase 1 red", "evidence": "e",
            },
            {
                "id": "VP-015", "status": "FAILED",
                "actual_result": "full_ci exit_code=1", "evidence": "e",
            },
        ],
    }
    report = agent.generate_verification_report(execution_results, retry_llm=1)

    statuses = {
        vr.get("id"): vr.get("status")
        for vr in report.get("verification_results", [])
    }
    assert statuses.get("VP-015") == "FAILED", (
        "真跑过并红了的关卡不得被洗成 DEFERRED"
    )


def test_report_keeps_an_unjudged_gate_deferred(tmp_path: Path) -> None:
    """负控：本轮没有裁决的关卡仍然必须落 DEFERRED。"""
    from unittest.mock import Mock

    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    points = [
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    ]

    coding_tool = Mock()
    coding_tool.query_json.return_value = {
        "overall_status": "FAILED",
        "summary": "判定层自己编的状态",
        "verification_results": [
            {
                "id": p["id"], "status": "FAILED",
                "actual_result": "judgment stub", "evidence": "e",
            }
            for p in points
        ],
        "requirement_deviations": [],
    }
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=coding_tool,
    )

    report = agent.generate_verification_report(
        {"verification_points": points, "execution_results": []}, retry_llm=1,
    )

    statuses = {
        vr.get("id"): vr.get("status")
        for vr in report.get("verification_results", [])
    }
    assert statuses.get("VP-015") == "DEFERRED", (
        "本轮没有裁决的关卡必须仍然是 DEFERRED"
    )


def test_projection_keeps_a_failed_gate_failed_when_the_round_is_unknown(
    tmp_path: Path,
) -> None:
    """轮次未知时，跨轮表里一条真 FAILED 仍然算「被问过」。

    修复前：投影层在轮次未知时传 ``asked_this_round=set()``，把跨轮表里
    一条真实的 FAILED 也写成 DEFERRED —— 真失败被洗成延后。
    """
    from verification_agent import VerificationAgent

    plan = _plan(
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    # 轮次未绑定 ⇒ 账本是跨轮的。
    executor, _ = _make_round_executor(
        plan, tmp_path / "unbound", {}, round_no=None,
    )
    executor._store_verdict("VP-001", {
        "status": "FAILED", "reasons": ["phase 1 red"], "evidence": {}, "round": 1,
    })
    executor._store_verdict("VP-015", {
        "status": "FAILED", "reasons": ["full_ci exit_code=1"],
        "evidence": {}, "round": 1,
    })

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None
    envelope = agent._build_execution_results_from_executor(
        {"verification_points": plan["vps"]}, executor,
    )

    statuses = {er["id"]: er["status"] for er in envelope["execution_results"]}
    assert statuses.get("VP-015") == "FAILED", (
        "轮次未知时，真跑过并红了的关卡也不得被写成 DEFERRED"
    )
    assert "VP-015" not in envelope["deferred_vp_ids"]


def test_projection_does_not_treat_a_cross_round_passed_gate_as_asked(
    tmp_path: Path,
) -> None:
    """负控：轮次未知时，跨轮的 PASSED 不算「本轮被问过」的证据。

    跨轮表里那条 PASSED 可能是上一轮的 —— 拿它证明「本轮被问过」，就等于
    让上一轮的成功替这一轮说话。
    """
    from verification_agent import VerificationAgent

    plan = _plan(
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_round_executor(
        plan, tmp_path / "unbound", {}, round_no=None,
    )
    executor._store_verdict("VP-001", {
        "status": "FAILED", "reasons": ["phase 1 red"], "evidence": {}, "round": 1,
    })
    executor._store_verdict("VP-015", {
        "status": "PASSED", "reasons": ["上一轮跑过"], "evidence": {}, "round": 1,
    })

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None
    envelope = agent._build_execution_results_from_executor(
        {"verification_points": plan["vps"]}, executor,
    )

    statuses = {er["id"]: er["status"] for er in envelope["execution_results"]}
    assert statuses.get("VP-015") == "DEFERRED", (
        "跨轮的 PASSED 不得被当成本轮「被问过」的证据"
    )
    assert "VP-015" in envelope["deferred_vp_ids"]


# ---------------------------------------------------------------------------
# VP-029（补）：被前置关卡挡住的关卡，它的「失败」不是本轮的
# ---------------------------------------------------------------------------
#
# 「真失败被洗成延后」和「没跑过的门禁被记成跑挂了」是同一个错误的两个
# 方向。区分它们要问的是**相位契约**：关卡按 ``phase_order`` 串行，前置
# 关卡红了 ⇒ 后面的关卡本轮**根本没被派发** ⇒ 它自己那条裁决只可能是上
# 一轮的残留（plan 20261102-Plan 第 15 轮的真实形状：Nightly 真跑了并
# 红了，E2E 没轮到，而 E2E 在账本里留着上一轮的 FAILED）。


def test_deferred_gate_ids_defers_a_gate_blocked_by_an_earlier_red_gate() -> None:
    """更早的关卡红了 ⇒ 这个关卡本轮没被派发 ⇒ 它那条 FAILED 是残留。"""
    points = [
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-016", verification_phase=2, phase_order=2, title="全量 E2E 测试"),
    ]
    ledger = {
        "VP-001": "PASSED",
        "VP-015": "FAILED",
        "VP-016": "FAILED",
    }

    assert _report_layer_gate_ids(points, ledger) == {"VP-016"}, (
        "被前置关卡挡住的关卡不得被算成「本轮被问过」"
    )


def test_projection_defers_a_gate_blocked_by_an_earlier_red_gate(
    tmp_path: Path,
) -> None:
    """负控（跨轮表）：前置红了 ⇒ 后面的关卡本轮没被派发 ⇒ DEFERRED。"""
    from verification_agent import VerificationAgent

    plan = _plan(
        _vp("VP-001"),
        _vp("VP-015", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
        _vp("VP-016", verification_phase=2, phase_order=2, title="全量 E2E 测试"),
    )
    executor, _ = _make_round_executor(
        plan, tmp_path / "unbound", {}, round_no=None,
    )
    executor._store_verdict("VP-001", {
        "status": "PASSED", "reasons": ["ok"], "evidence": {}, "round": 2,
    })
    executor._store_verdict("VP-015", {
        "status": "FAILED", "reasons": ["nightly ci 红"], "evidence": {}, "round": 2,
    })
    executor._store_verdict("VP-016", {
        "status": "FAILED", "reasons": ["上一轮残留"], "evidence": {}, "round": 1,
    })

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None
    envelope = agent._build_execution_results_from_executor(
        {"verification_points": plan["vps"]}, executor,
    )

    statuses = {er["id"]: er["status"] for er in envelope["execution_results"]}
    assert statuses.get("VP-015") == "FAILED", "真跑挂的关卡不能被洗成 DEFERRED"
    assert statuses.get("VP-016") == "DEFERRED", (
        "被前置关卡挡住的关卡本轮没被问过 —— 上一轮残留的裁决不算数"
    )
    assert "VP-016" in envelope["deferred_vp_ids"]


# 门禁重算（2026-11-06）：延后不是终态，理由必须点名当前的阻塞者
# ---------------------------------------------------------------------------
#
# 门禁原来只在层被短路的那一刻读一次结论，之后不再重算。于是：
#
#   * 阻塞前提在本轮后续（同一轮的重试 / 第二遍 pass）翻成终态成功之后，
#     那一层再也没被派发过 —— DEFERRED 被留成了终态；
#   * 上一轮留下的延后理由一直留在账本里，点名一个**已经翻绿**的结论
#     （「Phase 1 的 VP-016 结论为 FAILED」，而 VP-016 早已 PASSED），
#     而本轮的账本里又没有这个关卡的任何条目。


def test_deferred_gate_is_recomputed_and_rerun_when_blocker_flips(
    tmp_path: Path,
) -> None:
    """前提在本轮后续翻成终态成功 → 门禁重算 → 那一层必须在本轮真正派发。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))
    assert "VP-010" not in calls, "Phase 1 有失败时全量关卡不得执行"
    assert executor._verdicts["VP-010"]["status"] == "DEFERRED"

    # 本轮后续（同一轮的重试 / 第二遍 pass）：阻塞前提翻成终态成功
    executor.record_verdict(
        "VP-001",
        {"status": "PASSED", "reasons": ["重试通过"], "evidence": {}},
    )
    # 门禁重算：无阻塞就在本轮把那一层真正派发执行
    asyncio.run(executor.rerun_deferred_gate_layers())

    assert "VP-010" in calls, "前提消失后被延后的关卡必须在本轮补跑"
    assert executor._verdicts["VP-010"]["status"] == "PASSED"


def test_deferral_reason_follows_the_blocker_within_the_round(
    tmp_path: Path,
) -> None:
    """本轮内阻塞者换了人 → 延后理由必须跟着换。

    理由落在报告与状态里：点名一个**当前已经不成立**的结论状态（阻塞者早已
    PASSED，理由还写着「结论为 FAILED」）就是撒谎。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-002"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED", "VP-002": "FAILED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))
    assert "VP-010" not in calls
    reason = executor._verdicts["VP-010"]["reasons"][0]
    assert "VP-001" in reason, "延后理由必须点名当前的阻塞者"

    # 本轮内：VP-001 翻绿 —— 理由必须改口成还挡着的那一个
    executor.record_verdict(
        "VP-001",
        {"status": "PASSED", "reasons": ["重试通过"], "evidence": {}},
    )
    reason = executor._verdicts["VP-010"]["reasons"][0]
    assert "VP-002" in reason, "延后理由必须改口成当前的阻塞者"
    assert "VP-001" not in reason, "延后理由不得点名一个已经翻绿的结论"


def test_deferral_reason_names_the_current_blocker(tmp_path: Path) -> None:
    """延后理由必须点名**本轮**的阻塞者，不能留着上一轮那个已经翻绿的结论。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-002"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED", "VP-002": "PASSED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))
    first = executor._verdicts["VP-010"]["reasons"][0]
    assert "VP-001" in first, "延后理由必须点名阻塞者"

    # 下一轮：VP-001 翻绿、VP-002 失败 → 理由必须改口成当前的阻塞者
    _carry_round_verdicts(executor, tmp_path)
    executor2, _ = _make_executor(
        plan, tmp_path, {"VP-001": "PASSED", "VP-002": "FAILED"}, round_no=2,
    )
    asyncio.run(executor2.run(max_parallel=1))

    second = executor2._verdicts["VP-010"]["reasons"][0]
    assert "VP-002" in second, "延后理由必须改口成当前的阻塞者"
    assert "VP-001" not in second, "延后理由不得点名一个已经翻绿的结论"


def test_deferral_is_recorded_in_the_round_that_deferred_it(
    tmp_path: Path,
) -> None:
    """本轮再次延后时，本轮账本里必须有一条**自己的**条目。"""
    from verification_executor import VERDICT_ROUND_FIELD


    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))
    assert executor._verdicts["VP-010"][VERDICT_ROUND_FIELD] == 1

    _carry_round_verdicts(executor, tmp_path)
    executor2, _ = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=2,
    )
    asyncio.run(executor2.run(max_parallel=1))

    assert "VP-010" in executor2._current_round_verdicts(), (
        "本轮延后过的关卡必须在本轮账本里有一条自己的条目"
    )
    assert executor2._verdicts["VP-010"][VERDICT_ROUND_FIELD] == 2


def test_a_real_conclusion_is_not_overwritten_by_a_deferral(
    tmp_path: Path,
) -> None:
    """「已有结论就不覆盖」的跨轮规则：真跑出来的结论不得被延后记录顶掉。"""
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, calls = _make_executor(
        plan, tmp_path, {"VP-001": "PASSED"}, round_no=1,
    )
    asyncio.run(executor.run(max_parallel=1))
    assert "VP-010" in calls
    assert executor._verdicts["VP-010"]["status"] == "PASSED"

    # 下一轮：Phase 1 失败 → 关卡这一轮被延后 —— 上一轮的结论不许被顶掉
    _carry_round_verdicts(executor, tmp_path)
    executor2, calls2 = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=2,
    )
    asyncio.run(executor2.run(max_parallel=1))

    assert "VP-010" not in calls2, "本轮 Phase 1 失败时关卡不得执行"
    assert executor2._verdicts["VP-010"]["status"] == "PASSED", (
        "已有结论不得被一条延后记录覆盖"
    )


    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )



# ---------------------------------------------------------------------------
# 轮次作用域：被拦下的关卡，每一轮都要留下**本轮**自己的结论
# ---------------------------------------------------------------------------

def test_deferred_gate_is_recorded_again_in_every_round(tmp_path: Path) -> None:
    """被门禁拦下的关卡，每一轮都要重新落一条带本轮轮次戳的 DEFERRED。

    2026-11-09：账本只取**本轮**的裁决（``_current_round_verdicts`` /
    ``_build_execution_results_from_executor``），所以「这一轮被拦下了」
    这件事必须每一轮都重新落账。写侧原来的判据是「这条验证点有裁决就
    跳过」—— 那是**跨轮**的判据：上一轮留下的 DEFERRED 让本轮一条结论
    都不写，于是这个关卡从本轮账本里消失，报告只能对「没有记录」猜一个
    FAILED（"未获得执行结果"），修复环于是去修一条从没跑过的关卡。

    与 ``_mark_layer_skipped`` 的轮次语义对齐：整层跳过每一轮都落一条
    本轮结论（它走 ``_stamp_round``）。
    """
    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )

    # 第 1 轮：Phase 1 失败 → 关卡延后
    executor1, calls1 = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=1,
    )
    asyncio.run(executor1.run(max_parallel=1))
    assert calls1 == ["VP-001"], "Phase 1 失败时全量关卡不得执行"
    assert executor1._verdicts["VP-010"]["status"] == "DEFERRED"
    assert executor1._verdicts["VP-010"]["round"] == 1

    # 第 2 轮：同一个 plan_dir → 上一轮的结论从状态文件带过来；
    # Phase 1 又失败 → 关卡再次延后
    executor2, calls2 = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=2,
    )
    asyncio.run(executor2.run(max_parallel=1))
    assert calls2 == ["VP-001"], "Phase 1 失败时全量关卡不得执行"

    # 本轮必须有一条**自己的** DEFERRED 结论
    this_round = executor2._verdicts_by_round.get(2, {})
    assert "VP-010" in this_round, (
        "被拦下的关卡在第 2 轮必须重新落一条 DEFERRED —— "
        "上一轮的结论不替本轮说话"
    )
    assert this_round["VP-010"]["status"] == "DEFERRED"

    # 本轮视图（账本的来源）里必须看得见它
    current = executor2._current_round_verdicts()
    assert current.get("VP-010", {}).get("status") == "DEFERRED"

    # 上一轮的结论不许被抹掉（审计痕迹）
    assert executor2._verdicts_by_round[1]["VP-010"]["status"] == "DEFERRED"


def test_deferred_gate_keeps_the_conclusion_of_the_same_round(tmp_path: Path) -> None:
    """同一轮内不覆盖既有结论。

    跨轮要重落一条本轮结论（上一轮的结论不替本轮说话）；同一轮里已经有
    结论了就以那条为准 —— 一轮里反复落账只会把「本轮为什么延后」改写成
    最后一次的理由。
    """
    plan = _plan(
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    executor, _ = _make_executor(plan, tmp_path, {}, round_no=1)
    executor.record_verdict("VP-010", {
        "status": "DEFERRED",
        "reasons": ["本轮已经落过账"],
        "evidence": {},
    })

    executor._mark_layer_deferred(plan["vps"], "又一个理由")

    assert executor._verdicts["VP-010"]["reasons"] == ["本轮已经落过账"]
    assert executor._verdicts_by_round[1]["VP-010"]["reasons"] == ["本轮已经落过账"]


def test_deferred_gate_reaches_the_round_ledger_as_deferred(tmp_path: Path) -> None:
    """本轮账本里必须看得见那条 DEFERRED。

    账本是 Phase 3 判定与报告的唯一输入：关卡在里面缺席 → 报告只能对
    「没有记录」猜一个 FAILED，修复环于是去修一条从没跑过的全量关卡。
    """
    from unittest.mock import Mock

    from verification_agent import VerificationAgent

    plan = _plan(
        _vp("VP-001"),
        _vp("VP-010", verification_phase=2, phase_order=1, title="全量 Nightly CI"),
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # 第 1 轮：Phase 1 失败 → 关卡延后
    executor1, _ = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=1,
    )
    asyncio.run(executor1.run(max_parallel=1))

    # 第 2 轮：又被门禁拦下 → 必须在本轮账本里留下 DEFERRED
    executor2, _ = _make_executor(
        plan, tmp_path, {"VP-001": "FAILED"}, round_no=2,
    )
    asyncio.run(executor2.run(max_parallel=1))

    agent = VerificationAgent(
        plan_dir=tmp_path / "plan",
        project_dir=project_dir,
        coding_tool=Mock(),
    )
    envelope = agent._build_execution_results_from_executor(
        {"verification_points": plan["vps"]}, executor2,
    )
    by_id = {r["id"]: r for r in envelope["execution_results"]}
    assert "VP-010" in by_id, (
        "被拦下的关卡必须出现在本轮账本里 —— 缺席等于让报告去猜一个 FAILED"
    )
    assert by_id["VP-010"]["status"] == "DEFERRED"