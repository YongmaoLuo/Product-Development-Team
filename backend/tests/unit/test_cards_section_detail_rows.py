"""Unit tests for the card sections' reusable detail rows.

The scheduler page's plan-detail panel has to answer the same three
questions the Feishu card answers — *which* tasks are blocked upstream,
*which* tasks failed and why, and *which* VPs failed the verification
round — and it must not grow its own phrasing of them. The card's
section builders used to fold those facts straight into a sentence, so
the panel had to re-derive them, and re-derived them differently.

These tests pin the three structured extractors that make the card's
facts reusable:

  * ``_blocked_task_rows`` — the pending tasks an upstream failure holds
    up, as ``id`` / ``title`` / ``dep`` / ``state`` rows;
  * ``_failed_task_rows`` — the failed tasks as ``id`` / ``title`` /
    ``reason`` / ``attempt`` / ``max_attempts`` rows;
  * ``_failed_vp_rows`` — the failed VPs as ``id`` / ``title`` rows;

and they pin that extracting them did not change a single word of the
card: the sections still render the same lines.

The extractors are private, so the tests reach them through the module
(``cards._failed_task_rows``) rather than importing the names directly:
a missing extractor then fails the test that needs it instead of
breaking collection for the whole file.
"""

from __future__ import annotations

from typing import Any, Dict, List

from notifications import cards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content(elements: Any) -> str:
    """The text a card section rendered, as one string."""
    parts: List[str] = []
    for element in elements or []:
        text = element.get("text") if isinstance(element, dict) else None
        content = text.get("content") if isinstance(text, dict) else None
        if content:
            parts.append(str(content))
    return "\n".join(parts)


def _blocked_progress() -> Dict[str, Any]:
    """A progress payload with one failed task and two blocked ones."""
    return {
        "tasks": [
            {"id": "1-1", "status": "failed", "title": "写门禁"},
            {
                "id": "1-2", "status": "pending", "title": "跑测试",
                "depends_on": ["1-1"],
            },
            {
                "id": "1-3", "status": "pending", "title": "发布",
                "depends_on": ["1-2"],
            },
            {"id": "1-4", "status": "pending", "title": "可调度"},
        ],
    }


# ---------------------------------------------------------------------------
# The blocked-task rows
# ---------------------------------------------------------------------------


def test_blocked_rows_waiting_and_failed():
    """A blocked task says which dep holds it, and how."""
    rows = cards._blocked_task_rows(_blocked_progress())

    assert rows == [
        {"id": "1-2", "title": "跑测试", "dep": "1-1", "state": "failed"},
        {"id": "1-3", "title": "发布", "dep": "1-2", "state": "waiting"},
    ]


def test_blocked_rows_skip_schedulable_and_non_pending_tasks():
    """Only *pending* tasks held up by a dep are blocked."""
    progress = {
        "tasks": [
            {"id": "1-1", "status": "completed"},
            {
                "id": "1-2", "status": "pending", "title": "可调度",
                "depends_on": ["1-1"],
            },
            {"id": "1-3", "status": "pending", "title": "没有依赖"},
            {"id": "1-4", "status": "failed", "title": "已失败"},
        ],
    }

    assert cards._blocked_task_rows(progress) == []


def test_blocked_rows_tolerate_missing_and_broken_input():
    """``None`` / a non-list ``tasks`` yield no rows instead of a crash."""
    assert cards._blocked_task_rows(None) == []
    assert cards._blocked_task_rows({}) == []
    assert cards._blocked_task_rows({"tasks": "not-a-list"}) == []


# ---------------------------------------------------------------------------
# The blocked-tasks section
# ---------------------------------------------------------------------------


def test_blocked_section_renders_from_rows():
    """The section renders the rows it is given — same wording as before."""
    elements = cards._blocked_tasks_section(_blocked_progress())

    content = _content(elements)
    assert "**🚧 被阻塞的待执行任务：**" in content
    assert "• `1-2` 跑测试　↳ 被 [`1-1`] 阻塞（失败）" in content
    assert "• `1-3` 发布　↳ 等待 [`1-2`]" in content
    # A schedulable pending task is not blocked — it must not be listed.
    assert "可调度" not in content


# ---------------------------------------------------------------------------
# The failed-task rows
# ---------------------------------------------------------------------------


def test_failed_task_rows_carry_reason_and_attempt():
    """Each failed task carries its reason (with the fallback chain) and
    how many attempts it took."""
    progress = {
        "tasks": [
            {
                "id": "1-1", "status": "failed", "title": "写门禁",
                "failure_reason": "断言失败",
            },
            {
                "id": "1-2", "status": "failed", "title": "跑测试",
                "attempts": 3,
            },
            {
                "id": "1-3", "status": "failed", "title": "读配置",
                "description": "## 背景\n任务背景说明",
            },
            {"id": "1-4", "status": "completed", "title": "已完成"},
        ],
    }

    rows = cards._failed_task_rows(progress)

    assert [row["id"] for row in rows] == ["1-1", "1-2", "1-3"]

    assert rows[0]["title"] == "写门禁"
    assert rows[0]["reason"] == "断言失败"
    assert rows[0]["attempt"] == 1
    assert rows[0]["max_attempts"] == 5

    assert rows[1]["attempt"] == 3

    # No ``failure_reason`` → the description's context snippet.
    assert rows[2]["reason"].startswith("任务背景说明")


def test_failed_task_rows_fall_back_to_unknown_reason():
    """No reason and no description → ``未知原因``."""
    rows = cards._failed_task_rows({
        "tasks": [{"id": "1-1", "status": "failed", "title": "写门禁"}],
    })

    assert rows[0]["reason"] == "未知原因"


def test_failed_task_rows_truncate_a_long_reason():
    """A reason over 200 chars is cut, with the ellipsis the card shows."""
    rows = cards._failed_task_rows({
        "tasks": [{
            "id": "1-1", "status": "failed", "title": "写门禁",
            "failure_reason": "x" * 500,
        }],
    })

    assert rows[0]["reason"] == "x" * 200 + "..."


def test_failed_task_rows_keep_the_db_orphan_title_fallback():
    """A DB-only orphan without a title is named after its id."""
    rows = cards._failed_task_rows({
        "tasks": [{
            "id": "R1-5", "status": "failed", "failure_reason": "boom",
            "_origin": "db_orphan_terminal",
        }],
    })

    assert rows[0]["title"] == "Task R1-5 (DB-only)"


def test_failed_task_rows_tolerate_missing_and_broken_input():
    assert cards._failed_task_rows(None) == []
    assert cards._failed_task_rows({}) == []
    assert cards._failed_task_rows({"tasks": "not-a-list"}) == []


# ---------------------------------------------------------------------------
# The failed-VP rows
# ---------------------------------------------------------------------------


def test_failed_vp_rows_accept_strings_and_dicts():
    """``failed_vps`` mixes bare ids and dicts; a bare id is resolved
    through the ``vps`` index, an unknown one keeps an empty title."""
    verification = {
        "failed_vps": ["VP-2", {"id": "VP-3"}],
        "vps": [{"id": "VP-2", "title": "端口"}],
    }

    rows = cards._failed_vp_rows(verification)

    assert rows == [
        {"id": "VP-2", "title": "端口"},
        {"id": "VP-3", "title": ""},
    ]


def test_failed_vp_rows_without_failed_vps_is_empty():
    assert cards._failed_vp_rows(None) == []
    assert cards._failed_vp_rows({}) == []
    assert cards._failed_vp_rows({"failed_vps": []}) == []


# ---------------------------------------------------------------------------
# The failed-task detail section
# ---------------------------------------------------------------------------


def test_execution_sections_wording_unchanged():
    """The "❌ 失败任务详情" section renders the same lines as before."""
    tasks_info = {
        "total": 3, "completed": 1, "failed": 1, "skipped": 0,
        "in_progress": 0, "pending": 1,
    }
    execution_progress = {
        "tasks": [
            {
                "id": "1-1", "status": "failed", "title": "写门禁",
                "failure_reason": "断言失败", "attempts": 3,
            },
            {
                "id": "1-2", "status": "pending", "title": "跑测试",
                "depends_on": ["1-1"],
            },
        ],
    }

    content = _content(cards._execution_sections(tasks_info, execution_progress))

    assert "**❌ 失败任务详情：**" in content
    assert "• `[1-1]` 写门禁  (尝试 3/5)" in content
    assert "└─ 断言失败" in content
    # The blocked section below it is unchanged too.
    assert "**🚧 被阻塞的待执行任务：**" in content
    assert "• `1-2` 跑测试　↳ 被 [`1-1`] 阻塞（失败）" in content
