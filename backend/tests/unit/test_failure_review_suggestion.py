"""failure_review 的 suggestion 提炼：三级优先级与 200 字截断。

本文件钉住 R1 的**建议面**。提炼是确定性的（不新增 LLM 调用），
所以同一个输入必须永远得到同一句 suggestion：

* 报告里**未完成**的修复任务标题优先（最多 3 条）；
* 没有可用的修复任务时，用报告 ``human_decisions`` / ``unresolved``
  的前 3 条；
* 再没有，给固定兜底句；
* 结果不得长过 200 字。

「未完成」= 条目没有带 ``status``，或者 ``status`` 不是
``completed`` / ``done`` / ``skipped``。
"""

from __future__ import annotations

from failure_review import (
    FALLBACK_SUGGESTION,
    FailureSources,
    MAX_SUGGESTION_CHARS,
    extract_suggestion,
)


def _sources(**kwargs) -> FailureSources:
    """Build a :class:`FailureSources` with the irrelevant fields defaulted."""
    defaults = {
        "report": None,
        "verification_plan": None,
        "repair_tasks": None,
        "stop_reason": None,
        "failed_tasks": [],
        "round": 1,
    }
    defaults.update(kwargs)
    return FailureSources(**defaults)


def test_repair_task_titles():
    """两条修复任务 → '建议修复：标题；标题'。"""
    sources = _sources(
        repair_tasks={
            "tasks": [
                {"id": "R-1", "title": "修复登录接口"},
                {"id": "R-2", "title": "补鉴权用例"},
            ]
        }
    )

    assert extract_suggestion(sources) == "建议修复：修复登录接口；补鉴权用例"


def test_human_decisions_fallback():
    """无修复任务 → 取 human_decisions 前 3 条。"""
    sources = _sources(
        report={
            "human_decisions": ["确认是否回滚", "决定是否继续", "批准修复", "第四条不该出现"],
            "unresolved": [],
        }
    )

    result = extract_suggestion(sources)

    assert result == "待人工确认：确认是否回滚；决定是否继续；批准修复"
    assert "第四条不该出现" not in result


def test_fixed_sentence_when_empty():
    """全部分支都无内容 → 固定兜底句。"""
    sources = _sources()

    assert extract_suggestion(sources) == FALLBACK_SUGGESTION
    assert extract_suggestion(sources) == "建议重跑验证或人工排查（详见 verification_report.json）"


def test_skips_completed_repair_tasks():
    """status=completed 的条目不计入；未完成的照常计入。"""
    sources = _sources(
        repair_tasks={
            "tasks": [
                {"id": "R-1", "title": "已完成的修复", "status": "completed"},
                {"id": "R-2", "title": "还在做的修复", "status": "pending"},
                {"id": "R-3", "title": "跳过的修复", "status": "skipped"},
            ]
        }
    )

    assert extract_suggestion(sources) == "建议修复：还在做的修复"


def test_done_status_is_also_skipped():
    """status=done 与 skipped 一样算「已完成」。"""
    sources = _sources(
        repair_tasks={
            "tasks": [
                {"id": "R-1", "title": "跑完了", "status": "done"},
                {"id": "R-2", "title": "还在做", "status": "in_progress"},
            ]
        }
    )

    assert extract_suggestion(sources) == "建议修复：还在做"


def test_repair_tasks_capped_at_three():
    """修复任务最多取 3 条。"""
    sources = _sources(
        repair_tasks={
            "tasks": [
                {"id": "R-1", "title": "一"},
                {"id": "R-2", "title": "二"},
                {"id": "R-3", "title": "三"},
                {"id": "R-4", "title": "四"},
            ]
        }
    )

    assert extract_suggestion(sources) == "建议修复：一；二；三"


def test_all_completed_repair_tasks_fall_through():
    """修复任务全部完成 → 落到报告的待确认项。"""
    sources = _sources(
        repair_tasks={"tasks": [{"id": "R-1", "title": "做完了", "status": "completed"}]},
        report={"human_decisions": ["确认是否回滚"], "unresolved": []},
    )

    assert extract_suggestion(sources) == "待人工确认：确认是否回滚"


def test_repair_tasks_without_tasks_key_falls_through():
    """repair_tasks 没有 tasks 键 → 落到下一级。"""
    sources = _sources(
        repair_tasks={"rounds": [{"round": 1, "tasks": []}]},
        report={"human_decisions": ["确认是否回滚"], "unresolved": []},
    )

    assert extract_suggestion(sources) == "待人工确认：确认是否回滚"


def test_empty_task_list_falls_through():
    """repair_tasks 的 tasks 是空列表 → 落到下一级。"""
    sources = _sources(
        repair_tasks={"tasks": []},
        report={"human_decisions": ["确认是否回滚"], "unresolved": []},
    )

    assert extract_suggestion(sources) == "待人工确认：确认是否回滚"


def test_description_used_when_no_title():
    """条目没有 title → 用 description。"""
    sources = _sources(
        repair_tasks={"tasks": [{"id": "R-1", "description": "补充登录用例"}]}
    )

    assert extract_suggestion(sources) == "建议修复：补充登录用例"


def test_dict_entry_without_title_or_description_is_dumped_as_json():
    """字典条目既无 title 也无 description → json.dumps（不丢中文）。"""
    sources = _sources(
        repair_tasks={"tasks": [{"id": "R-1", "note": "手工处理"}]}
    )

    assert extract_suggestion(sources) == '建议修复：{"id": "R-1", "note": "手工处理"}'


def test_unresolved_items_used_when_no_human_decisions():
    """human_decisions 为空 → 用 unresolved。"""
    sources = _sources(report={"human_decisions": [], "unresolved": ["超时未复现"]})

    assert extract_suggestion(sources) == "待人工确认：超时未复现"


def test_never_exceeds_200_chars():
    """超长标题 → 长度 200 且以 '…' 结尾。"""
    sources = _sources(repair_tasks={"tasks": [{"id": "R-1", "title": "长" * 500}]})

    result = extract_suggestion(sources)

    assert len(result) == MAX_SUGGESTION_CHARS
    assert len(result) <= 200
    assert result.endswith("…")
