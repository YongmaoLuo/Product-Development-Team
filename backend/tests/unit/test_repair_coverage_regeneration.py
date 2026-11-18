"""``verification.repair_coverage.ensure_repair_coverage`` —— 覆盖编排。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2）
------------------------------------------------------
一轮验证里两个 ``deliverable_defect`` 失败（VP-013、VP-023），生成器只交回
一条 repair 任务（``repair-29``，指向 VP-013）。VP-023 既没有 repair 任务、
也没有 HITL —— 它在没有人知道的情况下消失了。

本文件钉住的是**编排**的那一步：批次物化之后先核对覆盖，缺了就补生成
**一次**（不得循环），再核对一次；仍然缺的逐个提问。

四个依赖全部是注入进来的，所以这一段编排可以被单测钉住，而不必真的去调
模型、去装配任务、去开 HITL：

* ``regenerate(missed_vps)`` —— 只为缺失的 VP 生成内容；
* ``assemble(contents, ids)`` —— 把内容装配成任务行（会丢掉
  ``files_to_modify`` 为空的行）；
* ``ask_human(vp, reason)`` —— 为仍然缺失的 VP 提问，返回空串表示没真的
  开出去。

钉住的部分
----------
* 全覆盖 → 三个回调一次都不调；
* 部分覆盖 → ``regenerate`` 恰好一次，且只收到缺失的那几个 VP；
* 补生成之后仍然缺 → 每个缺失 VP 问一次，原因是 ``missed_reason`` 给的
  token；
* 补生成带回已覆盖的 VP → 过滤掉，不合并；
* 补生成提到了某个 VP 但 ``assemble`` 把它丢了 → 归因
  ``REASON_EMPTY_FILES_TO_MODIFY``，不能误报成 ``REASON_GENERATOR_MISSING``
  —— 这两者的修法完全不同（改生成器的输入 vs 改生成器的输出契约）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.repair_coverage import (  # noqa: E402
    REASON_EMPTY_FILES_TO_MODIFY,
    REASON_GENERATOR_MISSING,
    ensure_repair_coverage,
)


def _recorder():
    """Three recorders — one per injectable dependency.

    The records are lists so the tests can assert *how many* times each
    dependency was called, not just that it was called: "regenerate at
    most once" is a claim about the count.
    """
    regenerate_calls: list = []
    assemble_calls: list = []
    ask_calls: list = []

    def regenerate(missed_vps):
        regenerate_calls.append(list(missed_vps))
        return []

    def assemble(contents, ids):
        assemble_calls.append((list(contents), list(ids)))
        return []

    def ask_human(vp, reason):
        ask_calls.append((vp, reason))
        return ""

    return regenerate, assemble, ask_human, regenerate_calls, assemble_calls, ask_calls


# ---------------------------------------------------------------------------
# 决策点 1 —— 全覆盖不触发补生成
# ---------------------------------------------------------------------------


def test_full_coverage_skips_regeneration():
    """每个候选都拿到了任务 → 补生成、装配、提问一次都不调。"""
    regenerate, assemble, ask_human, regen, asm, ask = _recorder()
    tasks = [
        {"id": "repair-29", "failed_vp_id": "VP-013"},
        {"id": "repair-30", "failed_vp_id": "VP-023"},
    ]

    outcome = ensure_repair_coverage(
        candidates=[{"id": "VP-013"}, {"id": "VP-023"}],
        repair_tasks=tasks,
        generated_vp_ids=["VP-013", "VP-023"],
        regenerate=regenerate,
        assemble=assemble,
        ask_human=ask_human,
        round_number=3,
    )

    assert regen == []
    assert asm == []
    assert ask == []
    assert outcome.missed == []
    assert outcome.regenerated is False
    assert outcome.regenerated_task_ids == []
    assert outcome.hitl_raised == []
    assert outcome.repair_tasks == tasks


# ---------------------------------------------------------------------------
# 决策点 2 —— 部分覆盖 → 补生成一次并补齐
# ---------------------------------------------------------------------------


def test_partial_coverage_regenerates_once():
    """缺 1 个 → ``regenerate`` 恰一次且只收到那个 VP，任务被补齐。"""
    regenerate, assemble, ask_human, regen, asm, ask = _recorder()
    regenerate_calls, assemble_calls, ask_calls = regen, asm, ask

    def regenerate_for_missing(missed_vps):
        regenerate_calls.append(list(missed_vps))
        return [
            {"failed_vp_id": "VP-023", "files_to_modify": ["backend/x.py"]}
        ]

    def assemble_for_missing(contents, ids):
        assemble_calls.append((list(contents), list(ids)))
        return [{"id": "repair-30", "failed_vp_id": "VP-023"}]

    outcome = ensure_repair_coverage(
        candidates=[{"id": "VP-013"}, {"id": "VP-023"}],
        repair_tasks=[{"id": "repair-29", "failed_vp_id": "VP-013"}],
        generated_vp_ids=["VP-013"],
        regenerate=regenerate_for_missing,
        assemble=assemble_for_missing,
        ask_human=ask_human,
        round_number=3,
    )

    # 只补那一个 VP，且只补这一次（不得循环）。
    assert regenerate_calls == [["VP-023"]]

    # 装配收到的是缺失 VP 的内容，以及缺失 VP 的 id。
    assert assemble_calls == [
        ([{"failed_vp_id": "VP-023", "files_to_modify": ["backend/x.py"]}], ["VP-023"])
    ]

    # 补齐了 → 不必提问。
    assert ask_calls == []

    assert outcome.missed == []
    assert outcome.regenerated is True
    assert outcome.regenerated_task_ids == ["repair-30"]
    assert outcome.hitl_raised == []
    assert outcome.repair_tasks == [
        {"id": "repair-29", "failed_vp_id": "VP-013"},
        {"id": "repair-30", "failed_vp_id": "VP-023"},
    ]


# ---------------------------------------------------------------------------
# 决策点 3 —— 补生成后仍缺 → 开 HITL
# ---------------------------------------------------------------------------


def test_still_missing_asks_human():
    """补生成没产出内容 → 仍然缺失的 VP 被逐个提问（原因：生成器漏了）。"""
    regenerate, assemble, ask_human, regenerate_calls, assemble_calls, ask_calls = (
        _recorder()
    )

    def regenerate_without_content(missed_vps):
        regenerate_calls.append(list(missed_vps))
        return []

    def assemble_without_tasks(contents, ids):
        assemble_calls.append((list(contents), list(ids)))
        return []

    def ask_and_record(vp, reason):
        ask_calls.append((vp, reason))
        return f"decision-for-{vp}"

    outcome = ensure_repair_coverage(
        candidates=[{"id": "VP-013"}, {"id": "VP-023"}],
        repair_tasks=[{"id": "repair-29", "failed_vp_id": "VP-013"}],
        generated_vp_ids=["VP-013"],
        regenerate=regenerate_without_content,
        assemble=assemble_without_tasks,
        ask_human=ask_and_record,
        round_number=3,
    )

    # 补生成只发生一次 —— 再缺就提问，不循环。
    assert regenerate_calls == [["VP-023"]]

    # 每个仍然缺失的 VP 问一次，原因是生成器漏了它。
    assert ask_calls == [("VP-023", REASON_GENERATOR_MISSING)]

    assert outcome.missed == ["VP-023"]
    assert outcome.regenerated is True
    assert outcome.regenerated_task_ids == []
    assert outcome.hitl_raised == ["VP-023"]
    assert outcome.repair_tasks == [{"id": "repair-29", "failed_vp_id": "VP-013"}]


def test_empty_ask_human_response_is_not_counted():
    """``ask_human`` 返回空串 → 没真的开出去，不计入 ``hitl_raised``。"""
    regenerate, assemble, ask_human, regenerate_calls, assemble_calls, ask_calls = (
        _recorder()
    )

    def regenerate_without_content(missed_vps):
        regenerate_calls.append(list(missed_vps))
        return []

    def ask_and_record(vp, reason):
        ask_calls.append((vp, reason))
        return ""

    outcome = ensure_repair_coverage(
        candidates=[{"id": "VP-013"}, {"id": "VP-023"}],
        repair_tasks=[{"id": "repair-29", "failed_vp_id": "VP-013"}],
        generated_vp_ids=["VP-013"],
        regenerate=regenerate_without_content,
        assemble=assemble,
        ask_human=ask_and_record,
        round_number=3,
    )

    # 问题确实问了（VP 没有消失），只是没开出去。
    assert ask_calls == [("VP-023", REASON_GENERATOR_MISSING)]
    assert outcome.hitl_raised == []
    assert outcome.missed == ["VP-023"]


# ---------------------------------------------------------------------------
# 补生成带回已覆盖的 VP → 过滤掉，不合并
# ---------------------------------------------------------------------------


def test_unrelated_regenerated_row_is_dropped():
    """补生成提到已覆盖的 VP → 不合并（任务表里不能出现第二条同 VP 的任务）。"""
    regenerate, assemble, ask_human, regenerate_calls, assemble_calls, ask_calls = (
        _recorder()
    )

    def regenerate_with_covered_row(missed_vps):
        regenerate_calls.append(list(missed_vps))
        return [
            {"failed_vp_id": "VP-013", "files_to_modify": ["backend/y.py"]},
            {"failed_vp_id": "VP-023", "files_to_modify": ["backend/x.py"]},
        ]

    def assemble_with_covered_row(contents, ids):
        assemble_calls.append((list(contents), list(ids)))
        rows = [
            {"id": f"repair-{30 + index}", "failed_vp_id": content["failed_vp_id"]}
            for index, content in enumerate(contents)
        ]
        # 装配器把已经覆盖的 VP 也带回来了 —— 它同样不能被合并。
        rows.append({"id": "repair-99", "failed_vp_id": "VP-013"})
        return rows

    outcome = ensure_repair_coverage(
        candidates=[{"id": "VP-013"}, {"id": "VP-023"}],
        repair_tasks=[{"id": "repair-29", "failed_vp_id": "VP-013"}],
        generated_vp_ids=["VP-013"],
        regenerate=regenerate_with_covered_row,
        assemble=assemble_with_covered_row,
        ask_human=ask_human,
        round_number=3,
    )

    # 已覆盖的 VP 的内容根本没进装配这一步。
    assert [
        content["failed_vp_id"] for content in assemble_calls[0][0]
    ] == ["VP-023"]

    # 合并后的任务表里，VP-013 只有原来那一条；repair-99 没进来。
    assert outcome.repair_tasks == [
        {"id": "repair-29", "failed_vp_id": "VP-013"},
        {"id": "repair-30", "failed_vp_id": "VP-023"},
    ]
    assert outcome.regenerated_task_ids == ["repair-30"]
    assert outcome.missed == []
    assert ask_calls == []


# ---------------------------------------------------------------------------
# 再校验的原因归因
# ---------------------------------------------------------------------------


def test_reason_after_regeneration_uses_second_batch_ids():
    """补生成提到了这个 VP，但 ``assemble`` 把它丢了 → 归因「scope 为空」。"""
    regenerate, assemble, ask_human, regenerate_calls, assemble_calls, ask_calls = (
        _recorder()
    )

    def regenerate_with_empty_scope(missed_vps):
        regenerate_calls.append(list(missed_vps))
        return [{"failed_vp_id": "VP-023", "files_to_modify": []}]

    def assemble_dropping_row(contents, ids):
        assemble_calls.append((list(contents), list(ids)))
        return []

    def ask_and_record(vp, reason):
        ask_calls.append((vp, reason))
        return f"decision-for-{vp}"

    outcome = ensure_repair_coverage(
        candidates=[{"id": "VP-013"}, {"id": "VP-023"}],
        repair_tasks=[{"id": "repair-29", "failed_vp_id": "VP-013"}],
        generated_vp_ids=["VP-013"],
        regenerate=regenerate_with_empty_scope,
        assemble=assemble_dropping_row,
        ask_human=ask_and_record,
        round_number=3,
    )

    # 生成器**提到过** VP-023，只是装配时丢了 → 不能误报成「生成器漏了」。
    assert ask_calls == [("VP-023", REASON_EMPTY_FILES_TO_MODIFY)]
    assert ask_calls[0][1] != REASON_GENERATOR_MISSING

    assert outcome.missed == ["VP-023"]
    assert outcome.regenerated is True
    assert outcome.regenerated_task_ids == []
    assert outcome.hitl_raised == ["VP-023"]
