"""verdicts → 四个索引桶：唯一的派生规则（读侧 / 写侧共用）。

背景
----
「一个 VP 属于 completed / failed / skipped / deferred 哪个桶」这条规则以前
只存在于写侧的实例方法
``VerificationExecutor._backfill_index_lists_from_verdicts`` 里。读侧（报告 /
卡片 / API）要判断同一个 VP 的归属时只能另写一份，两份必然漂移。

本模块把规则抽成纯函数：``bucket_for_status``（单条裁决 → 桶）与
``derive_index_buckets``（整张裁决表 → 四个桶）。两边共用同一个真相。

契约（与写侧现状逐条对齐）
--------------------------
  * PASSED               → completed
  * FAILED / BLOCKED     → failed
  * SKIPPED / SPLIT      → skipped
  * DEFERRED             → deferred（**只**进 deferred）
  * 未知 / 缺失 status   → 不进任何桶
  * 计划已作废（``obsolete``）的 FAILED → 不进任何桶
  * 计划已被取代（``superseded_by``）的 FAILED → skipped

重复裁决由**调用方**归一（dict 后写覆盖），本模块只读不写。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_buckets import (  # noqa: E402
    BUCKET_COMPLETED,
    BUCKET_DEFERRED,
    BUCKET_FAILED,
    BUCKET_SKIPPED,
    bucket_for_status,
    derive_index_buckets,
)


def _buckets_of(buckets: dict, vp_id: str) -> list:
    """这个 vp_id 落在哪些桶里（正常情况下恰好一个）。"""
    return [
        name
        for name, members in buckets.items()
        if vp_id in members
    ]


# ---------------------------------------------------------------------------
# 输入 / 输出示例（任务书里给定的那一组）
# ---------------------------------------------------------------------------


def test_derive_index_buckets_matches_documented_example() -> None:
    """任务书给的输入 → 输出必须逐字对得上。"""
    buckets = derive_index_buckets(
        {"VP-1": {"status": "FAILED"}, "VP-2": {"status": "PASSED"}}
    )
    assert buckets == {
        "completed_vps": ["VP-2"],
        "failed_vps": ["VP-1"],
        "skipped_vps": [],
        "deferred_vps": [],
    }


def test_bucket_constants_are_the_dict_keys() -> None:
    """常量值就是返回 dict 的键名 —— 两个函数可以直接互相索引。"""
    buckets = derive_index_buckets({})
    assert set(buckets) == {
        BUCKET_COMPLETED,
        BUCKET_FAILED,
        BUCKET_SKIPPED,
        BUCKET_DEFERRED,
    }


# ---------------------------------------------------------------------------
# 重复裁决：后写覆盖，先前的裁决不留痕
# ---------------------------------------------------------------------------


def test_failed_then_passed_only_completed() -> None:
    """先 FAILED 后 PASSED → 只在 completed 桶里。"""
    verdicts = {"VP-1": {"status": "FAILED"}}
    verdicts["VP-1"] = {"status": "PASSED"}  # 后写覆盖

    buckets = derive_index_buckets(verdicts)

    assert buckets[BUCKET_COMPLETED] == ["VP-1"]
    assert buckets[BUCKET_FAILED] == []
    assert buckets[BUCKET_SKIPPED] == []
    assert buckets[BUCKET_DEFERRED] == []
    assert _buckets_of(buckets, "VP-1") == [BUCKET_COMPLETED]


def test_failed_then_skipped_only_skipped() -> None:
    """先 FAILED 后 SKIPPED → 只在 skipped 桶里。"""
    verdicts = {"VP-1": {"status": "FAILED"}}
    verdicts["VP-1"] = {"status": "SKIPPED"}

    buckets = derive_index_buckets(verdicts)

    assert buckets[BUCKET_SKIPPED] == ["VP-1"]
    assert buckets[BUCKET_FAILED] == []
    assert buckets[BUCKET_COMPLETED] == []
    assert buckets[BUCKET_DEFERRED] == []
    assert _buckets_of(buckets, "VP-1") == [BUCKET_SKIPPED]


def test_passed_then_failed_only_failed() -> None:
    """先 PASSED 后 FAILED → 只在 failed 桶里（后来的结论算数）。"""
    verdicts = {"VP-1": {"status": "PASSED"}}
    verdicts["VP-1"] = {"status": "FAILED"}

    buckets = derive_index_buckets(verdicts)

    assert buckets[BUCKET_FAILED] == ["VP-1"]
    assert buckets[BUCKET_COMPLETED] == []
    assert buckets[BUCKET_SKIPPED] == []
    assert buckets[BUCKET_DEFERRED] == []
    assert _buckets_of(buckets, "VP-1") == [BUCKET_FAILED]


# ---------------------------------------------------------------------------
# 终态桶与非终态桶
# ---------------------------------------------------------------------------


def test_deferred_not_in_terminal_buckets() -> None:
    """DEFERRED 只出现在 deferred 桶 —— 三个终态桶里都没有它。"""
    buckets = derive_index_buckets(
        {
            "VP-D": {"status": "DEFERRED"},
            "VP-C": {"status": "PASSED"},
        }
    )

    assert buckets[BUCKET_DEFERRED] == ["VP-D"]
    assert buckets[BUCKET_COMPLETED] == ["VP-C"]
    assert buckets[BUCKET_FAILED] == []
    assert buckets[BUCKET_SKIPPED] == []
    assert _buckets_of(buckets, "VP-D") == [BUCKET_DEFERRED]


# ---------------------------------------------------------------------------
# 计划作废 / 被取代
# ---------------------------------------------------------------------------


def test_retired_obsolete_and_superseded() -> None:
    """obsolete 的 FAILED 不进任何桶；superseded_by 的 FAILED 进 skipped。"""
    verdicts = {
        "VP-OBS": {"status": "FAILED"},
        "VP-SUP": {"status": "FAILED"},
    }
    retired = {"VP-OBS": "obsolete", "VP-SUP": "superseded_by"}

    buckets = derive_index_buckets(verdicts, retired=retired)

    assert buckets[BUCKET_FAILED] == [], (
        "计划已经作废的 VP 不该被记成失败 —— 为一个没人要的判据生产失败"
    )
    assert buckets[BUCKET_COMPLETED] == []
    assert buckets[BUCKET_DEFERRED] == []
    assert buckets[BUCKET_SKIPPED] == ["VP-SUP"], (
        "被子 VP 取代的父 VP 必须进 skipped（= completed_set），"
        "否则下一轮会把拆分过的那条大 VP 重跑一遍"
    )
    assert _buckets_of(buckets, "VP-OBS") == []
    assert _buckets_of(buckets, "VP-SUP") == [BUCKET_SKIPPED]


def test_retired_marker_other_than_superseded_by_lands_in_no_bucket() -> None:
    """只要被计划作废了，FAILED 就不进任何桶（写侧是同一条规则）。"""
    retired = {"VP-1": "obsolete", "VP-2": "some_future_marker"}

    buckets = derive_index_buckets(
        {"VP-1": {"status": "FAILED"}, "VP-2": {"status": "FAILED"}},
        retired=retired,
    )

    assert buckets[BUCKET_FAILED] == []
    assert buckets[BUCKET_COMPLETED] == []
    assert buckets[BUCKET_SKIPPED] == []
    assert buckets[BUCKET_DEFERRED] == []


# ---------------------------------------------------------------------------
# 入参不被修改
# ---------------------------------------------------------------------------


def test_input_not_mutated() -> None:
    """调用之后入参 dict 一字未改。"""
    verdicts = {
        "VP-1": {"status": "FAILED"},
        "VP-2": {"status": "PASSED"},
        "VP-3": {"status": "DEFERRED"},
    }
    retired = {"VP-1": "obsolete"}
    verdicts_before = copy.deepcopy(verdicts)
    retired_before = dict(retired)

    derive_index_buckets(verdicts, retired=retired)

    assert verdicts == verdicts_before
    assert retired == retired_before


def test_returned_buckets_do_not_alias_the_input() -> None:
    """返回的桶是**新** list，改它不影响入参。"""
    verdicts = {"VP-1": {"status": "PASSED"}}

    buckets = derive_index_buckets(verdicts)
    buckets[BUCKET_COMPLETED].append("VP-999")

    assert verdicts == {"VP-1": {"status": "PASSED"}}


# ---------------------------------------------------------------------------
# 单条裁决 → 桶（bucket_for_status）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("PASSED", BUCKET_COMPLETED),
        ("FAILED", BUCKET_FAILED),
        ("BLOCKED", BUCKET_FAILED),
        ("SKIPPED", BUCKET_SKIPPED),
        ("SPLIT", BUCKET_SKIPPED),
        ("DEFERRED", BUCKET_DEFERRED),
        ("UNKNOWN", None),
        ("", None),
        (None, None),
    ],
)
def test_bucket_for_status_matrix(status, expected) -> None:
    """status → 桶 的完整映射（未知 status 不进任何桶）。"""
    assert bucket_for_status(status) == expected


def test_bucket_for_status_applies_the_retired_rule() -> None:
    """retired 里的标记只影响 FAILED（与写侧现状一致）。"""
    retired = {"VP-1": "obsolete", "VP-2": "superseded_by"}

    assert bucket_for_status("FAILED", vp_id="VP-1", retired=retired) is None
    assert bucket_for_status("FAILED", vp_id="VP-2", retired=retired) == BUCKET_SKIPPED
    assert bucket_for_status("FAILED", vp_id="VP-3", retired=retired) == BUCKET_FAILED
    # 非 FAILED 的裁决不受 retired 影响
    assert bucket_for_status("PASSED", vp_id="VP-1", retired=retired) == BUCKET_COMPLETED


def test_bucket_for_status_without_vp_id_ignores_retired() -> None:
    """没给 vp_id 就没法查 retired —— 按未作废处理。"""
    assert bucket_for_status("FAILED", retired={"VP-1": "obsolete"}) == BUCKET_FAILED


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_unknown_status_lands_in_no_bucket() -> None:
    """未知 status 不进任何桶（不猜、不兜底成 failed）。"""
    buckets = derive_index_buckets(
        {
            "VP-X": {"status": "WAT"},
            "VP-Y": {},
            "VP-Z": "not-a-dict",
        }
    )
    assert buckets == {
        BUCKET_COMPLETED: [],
        BUCKET_FAILED: [],
        BUCKET_SKIPPED: [],
        BUCKET_DEFERRED: [],
    }


def test_empty_verdicts_give_four_empty_buckets() -> None:
    """空裁决表 → 四个空桶（键齐全，调用方不必判空）。"""
    assert derive_index_buckets({}) == {
        BUCKET_COMPLETED: [],
        BUCKET_FAILED: [],
        BUCKET_SKIPPED: [],
        BUCKET_DEFERRED: [],
    }
    assert derive_index_buckets(None) == {
        BUCKET_COMPLETED: [],
        BUCKET_FAILED: [],
        BUCKET_SKIPPED: [],
        BUCKET_DEFERRED: [],
    }


def test_bucket_order_follows_verdict_insertion_order() -> None:
    """桶内顺序跟裁决表的写入顺序一致（仪表盘按行对齐计数）。"""
    verdicts = {
        "VP-3": {"status": "PASSED"},
        "VP-1": {"status": "PASSED"},
        "VP-2": {"status": "PASSED"},
    }
    buckets = derive_index_buckets(verdicts)
    assert buckets[BUCKET_COMPLETED] == ["VP-3", "VP-1", "VP-2"]