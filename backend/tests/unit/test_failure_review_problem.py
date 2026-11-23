"""failure_review 的 problem 提炼：三级优先级与 300 字截断。

本文件钉住 R1 的**输出面**。提炼是确定性的（不新增 LLM 调用），
所以同一个输入必须永远得到同一句 problem：

* 报告 ``summary`` 的首段优先；
* 没有报告（或 summary 是纯空白）时，用 stop_reason + 最多 2 条
  失败 VP 标题；
* 再没有，用失败任务的标题与 failure_reason；
* 全都没有，给固定兜底句；
* 任何一级产出的文本都不得长过 300 字。
"""

from __future__ import annotations

from failure_review import FailureSources, extract_problem

#: 三条验证点 —— 用来验证「最多取前 2 条」与「按计划顺序」。
THREE_POINTS = {
    "verification_points": [
        {"id": "VP-1", "title": "登录接口"},
        {"id": "VP-2", "title": "鉴权"},
        {"id": "VP-3", "title": "超时"},
    ]
}


def test_summary_first_paragraph():
    """两段 summary → 只取首段。"""
    sources = FailureSources(
        report={"summary": "第一段。\n\n第二段。"},
        verification_plan=None,
        repair_tasks=None,
        stop_reason=None,
        failed_tasks=[],
        round=1,
    )

    assert extract_problem(sources) == "第一段。"


def test_stop_reason_with_failed_vp_titles():
    """无报告 → stop_reason + 前 2 条失败 VP 标题。"""
    sources = FailureSources(
        report=None,
        verification_plan=THREE_POINTS,
        repair_tasks=None,
        stop_reason="max_rounds_reached",
        failed_tasks=[],
        round=2,
    )

    assert extract_problem(sources) == "max_rounds_reached；登录接口；鉴权"


def test_failed_tasks_fallback():
    """无报告无 VP → 失败任务的标题与 failure_reason。"""
    sources = FailureSources(
        report=None,
        verification_plan=None,
        repair_tasks=None,
        stop_reason=None,
        failed_tasks=[
            {"id": "T-1", "title": "修复登录接口", "failure_reason": "pytest 超时"},
        ],
        round=1,
    )

    result = extract_problem(sources)

    assert "修复登录接口" in result
    assert "pytest 超时" in result


def test_never_exceeds_300_chars():
    """超长 summary → 长度 300 且以 '…' 结尾。"""
    sources = FailureSources(
        report={"summary": "长" * 500},
        verification_plan=None,
        repair_tasks=None,
        stop_reason=None,
        failed_tasks=[],
        round=1,
    )

    result = extract_problem(sources)

    assert len(result) == 300
    assert result.endswith("…")


def test_failed_vp_titles_follow_verification_points_order():
    """报告点名的失败 VP 按 verification_points 的顺序取前 2。

    报告里的 ``verification_results`` 可以是任意顺序 —— 提炼按**计划**
    的顺序点名，操作者看到的顺序才是他熟悉的顺序。
    """
    sources = FailureSources(
        report={
            "summary": "   ",
            "verification_results": [
                {"id": "VP-3", "status": "FAILED"},
                {"id": "VP-1", "status": "PASSED"},
                {"id": "VP-2", "status": "FAILED"},
            ],
        },
        verification_plan=THREE_POINTS,
        repair_tasks=None,
        stop_reason="verification_failed",
        failed_tasks=[],
        round=1,
    )

    assert extract_problem(sources) == "verification_failed；鉴权；超时"


def test_blank_summary_falls_through_to_next_level():
    """summary 是纯空白 → 跳过该级，落到 stop_reason 一级。"""
    sources = FailureSources(
        report={"summary": "  \n\t "},
        verification_plan=None,
        repair_tasks=None,
        stop_reason="verification_failed",
        failed_tasks=[],
        round=1,
    )

    assert extract_problem(sources) == "verification_failed"


def test_fixed_sentence_when_nothing_is_known():
    """全部分支都无内容 → 固定兜底句。"""
    sources = FailureSources(
        report=None,
        verification_plan=None,
        repair_tasks=None,
        stop_reason=None,
        failed_tasks=[],
        round=0,
    )

    assert extract_problem(sources) == "计划进入终态失败，未见验证报告"
