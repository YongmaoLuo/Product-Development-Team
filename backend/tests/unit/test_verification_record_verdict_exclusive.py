"""``record_verdict`` 之后三个终态桶必须互斥。

背景
----
``VerificationExecutor.record_verdict`` 以前只往目标桶**追加**，从不在其它桶
里移除：同一个 VP 先 FAILED 后 PASSED，会同时留在 ``failed_vps`` 与
``completed_vps`` 里 —— 仪表盘于是报「1 失败 1 通过」，而裁决表里只有一条
结论。

更糟的是 PASSED **根本没有分支**：一条 PASSED 裁决压根不进
``completed_vps``（现有实现只有 FAILED / SKIPPED / DEFERRED 三支）。

本文件钉住四个契约（任务书 TDD 规格）：

  1. ``test_failed_then_passed_record_verdict`` —— 先失败后通过 → 只在 completed；
  2. ``test_failed_then_skipped_record_verdict`` —— 先失败后跳过 → 只在 skipped；
  3. ``test_deferred_after_failed_leaves_terminal`` —— 失败后延后 → 只在 deferred；
  4. ``test_repeat_verdict_no_duplicate`` —— 同一状态重复记录 → 不产生重复项。

外加两条边角：PASSED 首次写入必须进 completed（现有实现缺该分支），
以及「verdict 落库与 provenance 逻辑不变」。

桶的归属规则来自 :mod:`verification_buckets`（任务 1 的纯函数模块），
本文件不重新定义它 —— 只钉住写侧的**互斥性**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import (  # noqa: E402
    DEFAULT_STATE_FILENAME,
    VerificationExecutor,
)
from verification_verdict_provenance import FINGERPRINT_FIELD  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _verdict(status: str) -> Dict[str, Any]:
    """一条最小合法的裁决（schema: status / reasons / evidence）。"""
    return {"status": status, "reasons": [], "evidence": {}}


def _plan(vps: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一份最小的验证计划（``vps`` 是执行器读的形状）。"""
    return {
        "vps": vps
        if vps is not None
        else [{"id": "VP-003", "test_command": "pytest tests/test_x.py"}]
    }


def _make_executor(
    tmp_path: Path,
    plan: Dict[str, Any] = None,
    plan_id: str = "vp-bucket-exclusive",
) -> VerificationExecutor:
    """构造一个 ``VerificationExecutor``（不绑 verif_repo → 走 JSON 落库路径）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return VerificationExecutor(
        verification_plan=plan if plan is not None else _plan(),
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: _verdict("PASSED"),
    )


def _buckets_of(ex: VerificationExecutor) -> Dict[str, List[str]]:
    """执行器当前四个桶的快照（键名与 ``verification_buckets`` 的常量一致）。"""
    return {
        "completed_vps": ex.completed_vps,
        "failed_vps": ex.failed_vps,
        "skipped_vps": ex.skipped_vps,
        "deferred_vps": list(ex._deferred_vps),
    }


def _buckets_holding(ex: VerificationExecutor, vp_id: str) -> List[str]:
    """这个 vp_id 落在哪些桶里（互斥时恰好一个）。"""
    return [
        name
        for name, members in _buckets_of(ex).items()
        if vp_id in members
    ]


# ---------------------------------------------------------------------------
# 1. 先失败后通过 → 只在 completed
# ---------------------------------------------------------------------------


def test_failed_then_passed_record_verdict(tmp_path: Path) -> None:
    """先 FAILED 后 PASSED：VP-003 只留在 completed 桶里。

    旧实现把 FAILED 留在 ``failed_vps``，PASSED 又**不进**任何桶 ——
    于是同一个 VP 既是失败又没有结论。
    """
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-003", _verdict("FAILED"))
    ex.record_verdict("VP-003", _verdict("PASSED"))

    assert ex.failed_vps == []
    assert ex.completed_vps == ["VP-003"]
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []
    assert _buckets_holding(ex, "VP-003") == ["completed_vps"]


# ---------------------------------------------------------------------------
# 2. 先失败后跳过 → 只在 skipped
# ---------------------------------------------------------------------------


def test_failed_then_skipped_record_verdict(tmp_path: Path) -> None:
    """先 FAILED 后 SKIPPED：VP-003 只留在 skipped 桶里。"""
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-003", _verdict("FAILED"))
    ex.record_verdict("VP-003", _verdict("SKIPPED"))

    assert ex.failed_vps == []
    assert ex.skipped_vps == ["VP-003"]
    assert ex.completed_vps == []
    assert list(ex._deferred_vps) == []
    assert _buckets_holding(ex, "VP-003") == ["skipped_vps"]


# ---------------------------------------------------------------------------
# 3. 失败后延后 → 只在 deferred，三个终态桶都不留它
# ---------------------------------------------------------------------------


def test_deferred_after_failed_leaves_terminal(tmp_path: Path) -> None:
    """DEFERRED 只进 deferred 桶，并把三个终态桶里的旧记录清掉。

    DEFERRED 的语义是「本轮延后，下轮必须真跑」—— 它既不是终态成功
    （不能进 completed/skipped），也不是失败（不该生成修复任务）。
    """
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-003", _verdict("FAILED"))
    ex.record_verdict("VP-003", _verdict("DEFERRED"))

    assert ex.failed_vps == []
    assert ex.completed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == ["VP-003"]
    assert _buckets_holding(ex, "VP-003") == ["deferred_vps"]


# ---------------------------------------------------------------------------
# 4. 同一状态重复记录 → 不产生重复项
# ---------------------------------------------------------------------------


def test_repeat_verdict_no_duplicate(tmp_path: Path) -> None:
    """重复记录同一条 PASSED：completed 桶里只有一项。"""
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-003", _verdict("PASSED"))
    ex.record_verdict("VP-003", _verdict("PASSED"))

    assert ex.completed_vps == ["VP-003"]
    assert ex.completed_vps.count("VP-003") == 1
    assert ex.failed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []


# ---------------------------------------------------------------------------
# 5. PASSED 首次写入必须进 completed（旧实现缺这个分支）
# ---------------------------------------------------------------------------


def test_passed_first_record_enters_completed(tmp_path: Path) -> None:
    """一条 PASSED 裁决落地后就在 completed 桶里（不是「哪儿都没有」）。"""
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-003", _verdict("PASSED"))

    assert ex.completed_vps == ["VP-003"]
    assert _buckets_holding(ex, "VP-003") == ["completed_vps"]


# ---------------------------------------------------------------------------
# 6. 落库与 provenance 逻辑不变
# ---------------------------------------------------------------------------


def test_record_verdict_still_persists_verdict_and_provenance(
    tmp_path: Path,
) -> None:
    """分桶改写不得动落库与出处：裁决表、状态文件、命令指纹都照旧。"""
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-003", _verdict("FAILED"))
    ex.record_verdict("VP-003", _verdict("PASSED"))

    # 裁决表：后写覆盖，只剩最后那条结论。
    collected = ex.collect_verdicts()
    assert len(collected) == 1
    assert collected[0]["vp_id"] == "VP-003"
    assert collected[0]["status"] == "PASSED"

    # 出处：命令指纹照旧盖在裁决上。
    assert collected[0][FINGERPRINT_FIELD] != ""

    # 状态文件：落库路径没变（plan_id / pending_vps / verdicts）。
    state = json.loads((tmp_path / "plan" / DEFAULT_STATE_FILENAME).read_text())
    assert state["plan_id"] == "vp-bucket-exclusive"
    assert "VP-003" not in state["pending_vps"]
    assert state["verdicts"]["VP-003"]["status"] == "PASSED"


# ---------------------------------------------------------------------------
# 7. 计划已作废（obsolete）的 VP 的失败不进任何桶 —— 与派生规则一致
# ---------------------------------------------------------------------------


def test_retired_obsolete_failed_leaves_every_bucket(tmp_path: Path) -> None:
    """计划标了 ``obsolete`` 的 VP：它的 FAILED 不进任何桶。

    为一个没人要的判据生产失败会污染失败统计与修复任务的输入 ——
    这条规则由 :mod:`verification_buckets` 统一给出，写侧必须遵守。
    """
    plan = _plan(
        [
            {
                "id": "VP-003",
                "test_command": "pytest tests/test_x.py",
                "obsolete": True,
            }
        ]
    )
    ex = _make_executor(tmp_path, plan=plan)

    ex.record_verdict("VP-003", _verdict("FAILED"))

    assert _buckets_holding(ex, "VP-003") == []
    assert ex.failed_vps == []
    assert ex.completed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []


# ---------------------------------------------------------------------------
# 8. ``_place_in_bucket`` 本身：搬桶 + 拒绝未知桶名
# ---------------------------------------------------------------------------


def test_place_in_bucket_moves_vp_between_buckets(tmp_path: Path) -> None:
    """直接调用 ``_place_in_bucket``：VP 从一个桶搬到另一个桶，不留残影。"""
    ex = _make_executor(tmp_path)

    ex._place_in_bucket("VP-003", "failed_vps")
    assert ex.failed_vps == ["VP-003"]

    ex._place_in_bucket("VP-003", "completed_vps")
    assert ex.failed_vps == []
    assert ex.completed_vps == ["VP-003"]
    assert _buckets_holding(ex, "VP-003") == ["completed_vps"]


def test_place_in_bucket_rejects_unknown_bucket(tmp_path: Path) -> None:
    """桶名写错时必须**响**，不能悄悄把 VP 丢进「哪个桶都不是」。"""
    ex = _make_executor(tmp_path)

    with pytest.raises(ValueError):
        ex._place_in_bucket("VP-003", "not_a_bucket")


# ---------------------------------------------------------------------------
# 9. 互斥性：任意顺序的状态序列，VP 都只落在一个桶里
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statuses",
    [
        ["FAILED", "PASSED"],
        ["FAILED", "SKIPPED"],
        ["FAILED", "DEFERRED"],
        ["PASSED", "FAILED"],
        ["SKIPPED", "PASSED"],
        ["DEFERRED", "FAILED"],
        ["PASSED", "SKIPPED", "DEFERRED", "FAILED"],
    ],
)
def test_buckets_stay_mutually_exclusive(
    tmp_path: Path,
    statuses: List[str],
) -> None:
    """无论状态怎么变，同一个 VP 永远只落在一个桶里。"""
    ex = _make_executor(tmp_path)

    for status in statuses:
        ex.record_verdict("VP-003", _verdict(status))

    holders = _buckets_holding(ex, "VP-003")
    assert len(holders) == 1, (
        f"VP-003 同时落在 {holders!r}（状态序列 {statuses!r}）"
    )
