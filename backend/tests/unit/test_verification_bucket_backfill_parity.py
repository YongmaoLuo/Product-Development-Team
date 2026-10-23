"""backfill 的四个桶 == ``derive_index_buckets`` 的输出（写侧不再自带一份规则）。

背景
----
``VerificationExecutor._backfill_index_lists_from_verdicts`` 是**加载期**重算四个
索引桶的地方：进程重启、从 DB 水合裁决表、状态文件落盘后都要走它。它以前自带
一份「status → 桶」的判断，而读侧（报告 / 卡片 / API）用的是
:func:`verification_buckets.derive_index_buckets` —— 两份规则迟早漂移。

漂移已经发生过一次：``BLOCKED`` 在 ``_record_result`` 里进失败桶（历史 else 分支
的合约），在 backfill 里却因为「不认识这个 status」而**不进任何桶** —— 同一个
裁决，两个地方说法不同。

本文件钉住「backfill 委托共享函数」这条契约（任务书 TDD 规格）：

  1. ``test_backfill_matches_shared_helper``：混合裁决表的结果逐桶等于
     ``derive_index_buckets``；
  2. ``test_backfill_retired_unchanged``：计划作废（``obsolete`` /
     ``superseded_by``）的行为不变；
  3. ``test_backfill_stale_verdict_dropped``：出处过期的结论被摘掉，不进任何桶；
  4. ``test_backfill_deferred_not_terminal``：DEFERRED 只在 deferred 桶。

外加两条边界条件：BLOCKED 落 failed（与 ``_record_result`` 一致）、以及桶内顺序
沿用裁决表的插入顺序。

本文件**不重新定义**桶的规则，只钉住「写侧不再自带一份判断」。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_buckets import (  # noqa: E402
    BUCKET_COMPLETED,
    BUCKET_DEFERRED,
    BUCKET_FAILED,
    BUCKET_SKIPPED,
    derive_index_buckets,
)
from verification_executor import VerificationExecutor  # noqa: E402
from verification_verdict_provenance import command_fingerprint  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _verdict(status: str) -> Dict[str, Any]:
    """一条最小合法的裁决（schema: status / reasons / evidence）。"""
    return {"status": status, "reasons": [], "evidence": {}}


def _stamped(status: str, command: Optional[str]) -> Dict[str, Any]:
    """一条盖过「产生它的那条命令」指纹的裁决（指纹对得上 → 不是 stale）。"""
    verdict = _verdict(status)
    if command is not None:
        verdict["command_fingerprint"] = command_fingerprint(command)
    return verdict


def _make_executor(
    tmp_path: Path,
    vps: List[Dict[str, Any]],
    plan_id: str = "vp-bucket-backfill-parity",
) -> VerificationExecutor:
    """构造一个 ``VerificationExecutor``（计划用 ``vps`` 形状）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return VerificationExecutor(
        verification_plan={"vps": vps},
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: _verdict("PASSED"),
    )


def _buckets_of(ex: VerificationExecutor) -> Dict[str, List[str]]:
    """执行器当前四个桶的快照（键名与 ``verification_buckets`` 的常量一致）。"""
    return {
        BUCKET_COMPLETED: list(ex._completed_vps),
        BUCKET_FAILED: list(ex._failed_vps),
        BUCKET_SKIPPED: list(ex._skipped_vps),
        BUCKET_DEFERRED: list(ex._deferred_vps),
    }


def _buckets_holding(ex: VerificationExecutor, vp_id: str) -> List[str]:
    """这个 vp_id 落在哪些桶里（互斥时恰好一个）。"""
    return [
        name
        for name, members in _buckets_of(ex).items()
        if vp_id in members
    ]


# ---------------------------------------------------------------------------
# 1. backfill == 共享纯函数
# ---------------------------------------------------------------------------


def test_backfill_matches_shared_helper(tmp_path: Path) -> None:
    """混合裁决表的分桶结果逐桶等于 ``derive_index_buckets``。

    这是「写侧不再自带一份规则」的全部含义：给同一张裁决表，两个入口必须给出
    同一份答案。计划里带一个 ``obsolete`` 的 VP，让 ``retired=`` 参数非空 ——
    空 retired 的比对太弱，写侧把作废规则抄错也照样能过。
    """
    ex = _make_executor(
        tmp_path,
        [
            {"id": "VP-1", "test_command": "pytest tests/one.py"},
            {"id": "VP-2", "test_command": "pytest tests/two.py"},
            {"id": "VP-3", "test_command": "pytest tests/three.py"},
            {"id": "VP-4", "test_command": "pytest tests/four.py"},
            {"id": "VP-5", "test_command": "pytest tests/five.py"},
            {"id": "VP-6", "test_command": "pytest tests/six.py"},
            {"id": "VP-OBS", "test_command": "pytest tests/obs.py", "obsolete": True},
        ],
    )
    ex._verdicts = {
        "VP-1": _stamped("PASSED", "pytest tests/one.py"),
        "VP-2": _stamped("FAILED", "pytest tests/two.py"),
        "VP-3": _stamped("SKIPPED", "pytest tests/three.py"),
        "VP-4": _stamped("SPLIT", "pytest tests/four.py"),
        "VP-5": _stamped("DEFERRED", "pytest tests/five.py"),
        "VP-6": _stamped("BLOCKED", "pytest tests/six.py"),
        "VP-OBS": _stamped("FAILED", "pytest tests/obs.py"),
    }
    verdicts_before = copy.deepcopy(ex._verdicts)
    retired_before = ex._plan_retired_vps()
    assert retired_before, "这个用例要真的带上计划作废的 VP，否则比对太弱"

    ex._backfill_index_lists_from_verdicts()

    expected = derive_index_buckets(verdicts_before, retired=retired_before)
    actual = _buckets_of(ex)
    assert actual == expected, (
        f"backfill 与共享纯函数给出了不同的桶：\n"
        f"  backfill = {actual}\n"
        f"  derive_index_buckets = {expected}"
    )


def test_backfill_blocked_lands_in_failed(tmp_path: Path) -> None:
    """BLOCKED 以失败的身份出现 —— 与 ``_record_result`` 同一份规则。

    以前 backfill 不认识 BLOCKED（那是写侧历史 else 分支才有的合约），于是同一个
    裁决在两处说法不同：``_record_result`` 报失败、backfill 报「哪儿都不是」。
    """
    ex = _make_executor(
        tmp_path, [{"id": "VP-1", "test_command": "pytest tests/one.py"}],
    )
    ex._verdicts = {"VP-1": _stamped("BLOCKED", "pytest tests/one.py")}

    ex._backfill_index_lists_from_verdicts()

    assert ex.failed_vps == ["VP-1"], (
        "BLOCKED 是「验不下去」，必须以失败的身份出现，否则没人会去修它"
    )
    assert _buckets_holding(ex, "VP-1") == [BUCKET_FAILED]


def test_backfill_keeps_verdict_insertion_order(tmp_path: Path) -> None:
    """桶内顺序沿用裁决表的插入顺序（仪表盘的计数要与逐条裁决表对齐）。"""
    ex = _make_executor(
        tmp_path,
        [
            {"id": "VP-3", "test_command": "pytest tests/three.py"},
            {"id": "VP-1", "test_command": "pytest tests/one.py"},
            {"id": "VP-2", "test_command": "pytest tests/two.py"},
        ],
    )
    ex._verdicts = {
        "VP-3": _stamped("FAILED", "pytest tests/three.py"),
        "VP-1": _stamped("FAILED", "pytest tests/one.py"),
        "VP-2": _stamped("FAILED", "pytest tests/two.py"),
    }

    ex._backfill_index_lists_from_verdicts()

    assert ex.failed_vps == ["VP-3", "VP-1", "VP-2"]


# ---------------------------------------------------------------------------
# 2. 计划作废：行为不变
# ---------------------------------------------------------------------------


def test_backfill_retired_unchanged(tmp_path: Path) -> None:
    """``obsolete`` 不进任何桶；``superseded_by`` 进 skipped。

    这两条是「计划自己不再要求这个判据」的语义，委托共享函数之后必须原样保留：
    为一个没人要的判据生产失败会污染失败统计与修复任务的输入，而被子 VP 取代的
    父 VP 必须留在 ``completed_set`` 里，否则下一轮把拆分过的大 VP 重跑一遍。
    """
    ex = _make_executor(
        tmp_path,
        [
            {
                "id": "VP-OBS",
                "test_command": "pytest tests/obs.py",
                "obsolete": True,
            },
            {
                "id": "VP-SUP",
                "test_command": "pytest tests/sup.py",
                "superseded_by": ["VP-SUP-1", "VP-SUP-2"],
            },
        ],
    )
    ex._verdicts = {
        "VP-OBS": _stamped("FAILED", "pytest tests/obs.py"),
        "VP-SUP": _stamped("FAILED", "pytest tests/sup.py"),
    }

    ex._backfill_index_lists_from_verdicts()

    assert ex.failed_vps == [], "计划已经作废的 VP 不该被记成失败"
    assert "VP-OBS" not in ex.completed_vps
    assert "VP-OBS" not in ex.skipped_vps
    assert "VP-OBS" not in ex._deferred_vps
    assert ex.skipped_vps == ["VP-SUP"], (
        "被子 VP 取代的父 VP 必须进 skipped（= completed_set）"
    )
    assert _buckets_holding(ex, "VP-OBS") == []
    assert _buckets_holding(ex, "VP-SUP") == [BUCKET_SKIPPED]


def test_backfill_keeps_retired_verdicts_for_audit(tmp_path: Path) -> None:
    """作废的裁决**不从裁决表里摘掉** —— 它是审计痕迹，报告侧自己过滤。

    刻意与 stale 的处理不同：stale 的结论摘掉（避免重跑前被读到），作废的结论
    留着（要能回答「这一轮到底判过什么」）。
    """
    ex = _make_executor(
        tmp_path,
        [
            {
                "id": "VP-OBS",
                "test_command": "pytest tests/obs.py",
                "obsolete": True,
            },
        ],
    )
    ex._verdicts = {"VP-OBS": _stamped("FAILED", "pytest tests/obs.py")}

    ex._backfill_index_lists_from_verdicts()

    assert "VP-OBS" in ex._verdicts, "作废的裁决是审计痕迹，不该被摘掉"


# ---------------------------------------------------------------------------
# 3. 出处过期：结论被摘掉，不进任何桶
# ---------------------------------------------------------------------------


def test_backfill_stale_verdict_dropped(tmp_path: Path) -> None:
    """命令变过之后，那条结论不再代表当前要验的东西 → 摘掉，不进任何桶。

    刻意**不**塞进 failed：它没有失败，只是结论的出处过期了 —— 报成 FAILED 会
    污染失败统计与修复任务的输入。
    """
    ex = _make_executor(
        tmp_path, [{"id": "VP-1", "test_command": "pytest tests/new.py"}],
    )
    ex._verdicts = {"VP-1": _stamped("PASSED", "pytest tests/old.py")}

    ex._backfill_index_lists_from_verdicts()

    assert "VP-1" not in ex._verdicts, "过期结论被摘掉，避免重跑前被读到"
    for name, members in _buckets_of(ex).items():
        assert "VP-1" not in members, f"过期结论不该进 {name}"


def test_backfill_keeps_verdict_matching_current_command(tmp_path: Path) -> None:
    """命令没变的 VP 照旧留在原桶 —— 撞顶后重置轮次继续迭代，不该作废结论。"""
    ex = _make_executor(
        tmp_path, [{"id": "VP-1", "test_command": "pytest tests/one.py"}],
    )
    ex._verdicts = {"VP-1": _stamped("PASSED", "pytest tests/one.py")}

    ex._backfill_index_lists_from_verdicts()

    assert ex.completed_vps == ["VP-1"]
    assert "VP-1" in ex._verdicts


# ---------------------------------------------------------------------------
# 4. DEFERRED：只在 deferred 桶
# ---------------------------------------------------------------------------


def test_backfill_deferred_not_terminal(tmp_path: Path) -> None:
    """DEFERRED **只**进 deferred 桶 —— 三个终态桶里都没有它。

    ``BaseExecutor.run`` 的 ``completed_set`` 是 completed ∪ skipped，不在里面才
    会在下一轮重跑 —— 这正是「Phase 1 修好之后要把全量关卡真正跑一遍」的语义。
    """
    ex = _make_executor(
        tmp_path, [{"id": "VP-D", "test_command": "pytest tests/d.py"}],
    )
    ex._verdicts = {"VP-D": _stamped("DEFERRED", "pytest tests/d.py")}

    ex._backfill_index_lists_from_verdicts()

    assert list(ex._deferred_vps) == ["VP-D"]
    assert ex.completed_vps == []
    assert ex.failed_vps == []
    assert ex.skipped_vps == []
    assert _buckets_holding(ex, "VP-D") == [BUCKET_DEFERRED]


# ---------------------------------------------------------------------------
# 任务书给定的输入 / 输出示例
# ---------------------------------------------------------------------------


def test_backfill_documented_example(tmp_path: Path) -> None:
    """任务书的输入 → 输出必须逐字对得上。"""
    ex = _make_executor(tmp_path, [])

    ex._verdicts = {"VP-1": {"status": "FAILED"}, "VP-2": {"status": "SKIPPED"}}
    ex._backfill_index_lists_from_verdicts()

    assert ex.failed_vps == ["VP-1"]
    assert ex.skipped_vps == ["VP-2"]
