"""``verification.repair_coverage`` —— 覆盖判定与提问文案的纯函数。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2）
------------------------------------------------------
一轮验证里有两个 ``deliverable_defect`` 失败（VP-013、VP-023），生成器
只交回一条 repair 任务（``repair-29``，指向 VP-013）。VP-023 既没有 repair
任务、也没有 HITL —— 它在没有人知道的情况下消失了，直到下一轮再次失败。

「消失」这一步发生在两件事之间：

* ``RepairTaskAssembler.assemble`` 把 ``files_to_modify`` 为空的行丢掉；
* orchestrator 拿到 assemble 的结果就直接持久化，**没有人核对哪些 VP
  拿到了任务、哪些没有**。

本模块给出的就是那个核对：**纯函数**，不读文件、不发请求、不看时钟，也
不导入 orchestrator（orchestrator 将来要 import 它，方向反了就是环）。
它回答三件事：

* :func:`missing_repair_targets` —— 哪些候选 VP 没有拿到 repair 任务；
* :func:`missed_reason` / :func:`describe_reason` —— 为什么没有（生成器
  根本没交回这条，还是交回了但被 assemble 丢掉）；
* :func:`recommended_default` / :func:`missed_vp_question` /
  :func:`infra_terminal_question` —— HITL 该问什么、默认答案是哪个。

钉住的部分
----------
这些函数的**字面量**是接口，不是实现细节：

* ``REASON_GENERATOR_MISSING`` / ``REASON_EMPTY_FILES_TO_MODIFY`` 会被写进
  日志与 HITL 问题里，任务 7（``test_repair_coverage_hitl.py``）直接断言
  ``describe_reason`` 的文本片段，所以两个 token 的拼写与描述文本都被
  钉在这里；
* 选项表与默认值同样是契约：R3 的缺失 VP 选项是
  ``manual_fix / revise_criterion / defer``，R4 的 infra 终端选项是
  ``retry_more / skip_this_vp / defer``，默认值分别是 ``manual_fix`` 与
  ``retry_more``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.repair_coverage import (  # noqa: E402
    INFRA_TERMINAL_DEFAULT_OPTION,
    INFRA_TERMINAL_OPTIONS,
    MISSED_VP_DEFAULT_OPTION,
    MISSED_VP_OPTIONS,
    REASON_EMPTY_FILES_TO_MODIFY,
    REASON_GENERATOR_MISSING,
    describe_reason,
    infra_terminal_question,
    missed_reason,
    missed_vp_question,
    missing_repair_targets,
    recommended_default,
)


# ---------------------------------------------------------------------------
# R1 —— 覆盖判定
# ---------------------------------------------------------------------------


def test_missing_preserves_candidate_order():
    """缺失列表按**候选**的顺序返回，不按任务表的顺序。"""
    repair_tasks = [{"id": "repair-29", "failed_vp_id": "VP-013"}]

    assert missing_repair_targets(["VP-013", "VP-023"], repair_tasks) == ["VP-023"]

    # 三个候选、只覆盖中间那个：返回的顺序是候选的顺序（VP-030 在
    # VP-023 前面），不是排序、也不是任务表碰巧的顺序。
    assert missing_repair_targets(
        ["VP-030", "VP-013", "VP-023"], repair_tasks
    ) == ["VP-030", "VP-023"]


def test_all_covered_returns_empty():
    """每个候选都有任务 → 没有缺失。"""
    repair_tasks = [
        {"id": "repair-29", "failed_vp_id": "VP-013"},
        {"id": "repair-30", "failed_vp_id": "VP-023"},
    ]

    assert missing_repair_targets(["VP-013", "VP-023"], repair_tasks) == []


def test_missing_skips_blank_and_duplicate_candidates():
    """空候选 → 空列表；重复 id 只算一次；空串 id 跳过。"""
    assert missing_repair_targets([], [{"id": "repair-29", "failed_vp_id": "VP-013"}]) == []
    assert missing_repair_targets(
        ["VP-023", "VP-023"], [{"id": "repair-29", "failed_vp_id": "VP-013"}]
    ) == ["VP-023"]
    assert missing_repair_targets(["", "VP-023"], []) == ["VP-023"]


# ---------------------------------------------------------------------------
# R7 —— 缺失原因可观测
# ---------------------------------------------------------------------------


def test_missed_reason_distinguishes_drop():
    """「生成器没交回」与「交回了但被丢掉」是两种原因。"""
    # 生成器为这个 VP 交回了内容，却没有任务落下来 → 被 assemble 丢掉
    assert (
        missed_reason("VP-023", ["VP-013", "VP-023"]) == REASON_EMPTY_FILES_TO_MODIFY
    )
    # 生成器为别的 VP 交回了内容，这个 VP 它根本没提 → 生成器漏了
    assert missed_reason("VP-023", ["VP-013"]) == REASON_GENERATOR_MISSING
    # 一条内容都没有 → 还是「生成器漏了」
    assert missed_reason("VP-023", []) == REASON_GENERATOR_MISSING


def test_reason_tokens_are_frozen():
    """两个原因 token 的字面量与描述文本被钉住。"""
    assert REASON_GENERATOR_MISSING == "generator_returned_no_entry"
    assert REASON_EMPTY_FILES_TO_MODIFY == "files_to_modify_was_empty"

    assert "no entry" in describe_reason(REASON_GENERATOR_MISSING)
    assert "files_to_modify was empty" in describe_reason(REASON_EMPTY_FILES_TO_MODIFY)


# ---------------------------------------------------------------------------
# R3 / R4 —— HITL 的选项、默认值与文案
# ---------------------------------------------------------------------------


def test_recommended_default_falls_back():
    """recommendation 不在选项表内（或没有）→ 回落到 fallback。"""
    assert (
        recommended_default(
            {"recommendation": "defer"}, MISSED_VP_OPTIONS, MISSED_VP_DEFAULT_OPTION
        )
        == "defer"
    )
    assert (
        recommended_default(
            {"recommendation": "not_an_option"},
            MISSED_VP_OPTIONS,
            MISSED_VP_DEFAULT_OPTION,
        )
        == MISSED_VP_DEFAULT_OPTION
    )
    assert (
        recommended_default({}, MISSED_VP_OPTIONS, MISSED_VP_DEFAULT_OPTION)
        == MISSED_VP_DEFAULT_OPTION
    )
    # "manual_fix" 在 R3 的选项表里，但不在 R4 的选项表里 —— 落在表外的
    # recommendation 没法被选中，于是回落到 fallback。
    assert (
        recommended_default(
            {"recommendation": "manual_fix"},
            INFRA_TERMINAL_OPTIONS,
            INFRA_TERMINAL_DEFAULT_OPTION,
        )
        == INFRA_TERMINAL_DEFAULT_OPTION
    )
    # "defer" 在两张表里，所以它跟着 recommendation 走。
    assert (
        recommended_default(
            {"recommendation": "defer"},
            INFRA_TERMINAL_OPTIONS,
            INFRA_TERMINAL_DEFAULT_OPTION,
        )
        == "defer"
    )


def test_option_vocabularies_are_frozen():
    """两套选项表与默认值被钉住。"""
    assert MISSED_VP_OPTIONS == ("manual_fix", "revise_criterion", "defer")
    assert MISSED_VP_DEFAULT_OPTION == "manual_fix"
    assert INFRA_TERMINAL_OPTIONS == ("retry_more", "skip_this_vp", "defer")
    assert INFRA_TERMINAL_DEFAULT_OPTION == "retry_more"


def test_questions_name_the_point_and_the_reason():
    """两个提问都点名 VP；缺失提问还带原因。"""
    question = missed_vp_question("VP-023", REASON_EMPTY_FILES_TO_MODIFY, 3)

    assert "VP-023" in question
    assert "files_to_modify" in question
    assert "3" in question

    infra_question = infra_terminal_question("VP-007", 2)

    assert "VP-007" in infra_question
    assert "2" in infra_question
