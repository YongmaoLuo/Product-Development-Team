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