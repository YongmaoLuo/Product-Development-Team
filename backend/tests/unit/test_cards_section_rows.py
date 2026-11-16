"""Unit tests for the card sections' reusable row data.

The Feishu card answers two questions in its body: how many tasks are
done, and what is running right now. A second surface — the scheduler
page's plan-detail panel — has to answer the same two questions, and
it must not grow its own phrasing of them: the wording lives in the
card's section builders, and the other surface reuses those builders
instead of writing its own sentences.

For that to work the builders have to hand back *data* as well as
wording. These tests pin the two structured helpers the card's own
section builders use:

  * ``_task_summary_data`` — the counts, the percentage and the bar of
    the task-progress section, or ``None`` when the plan has no tasks;
  * ``_in_progress_task_rows`` — the running tasks as ``id`` / ``title``
    rows, so a consumer can name them without re-deriving the filter.

and they pin that extracting those helpers did not change a single
word of the card: the sections still render the same lines.
"""
from __future__ import annotations

from typing import Any, Dict, List

from notifications.cards import (
    _in_progress_task_rows,
    _in_progress_tasks_section,
    _task_summary_data,
    _task_summary_section,
)


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


def _rows(*rows: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A task-progress list, as the progress endpoint hands it over."""
    return list(rows)


# ---------------------------------------------------------------------------
# The task summary data
# ---------------------------------------------------------------------------


def test_task_summary_data_builds_counts_and_bar():
    data = _task_summary_data({"total": 4, "completed": 3, "in_progress": 1})

    assert data is not None
    assert data["counts"] == {
        "total": 4,
        "completed": 3,
        "in_progress": 1,
        "pending": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert data["percent"] == 75
    assert len(data["bar"]) == 10
    assert data["bar"] == "█" * 7 + "░" * 3


def test_task_summary_data_empty_plan_is_none():
    assert _task_summary_data({"total": 0}) is None
    assert _task_summary_data(None) is None
    assert _task_summary_data({}) is None


def test_task_summary_data_clamps_negative_counts():
    data = _task_summary_data({"total": 4, "completed": -3, "pending": -1})

    assert data is not None
    assert data["counts"]["completed"] == 0
    assert data["counts"]["pending"] == 0
    assert data["percent"] == 0
    assert len(data["bar"]) == 10


def test_task_summary_data_clamps_percent_into_range():
    """A count larger than the total cannot push the bar past its width."""
    data = _task_summary_data({"total": 4, "completed": 40})

    assert data is not None
    assert data["percent"] == 100
    assert len(data["bar"]) == 10
    assert data["bar"] == "█" * 10


# ---------------------------------------------------------------------------
# The task summary section
# ---------------------------------------------------------------------------


def test_task_summary_section_keeps_wording():
    elements = _task_summary_section({"total": 4, "completed": 3, "in_progress": 1})

    content = _content(elements)
    assert "75% (3/4)" in content
    assert "███████░░░" in content
    assert "✅3" in content
    assert "⏳1" in content


def test_task_summary_section_without_tasks_renders_nothing():
    assert _task_summary_section(None) == []
    assert _task_summary_section({"total": 0}) == []


# ---------------------------------------------------------------------------
# The running-task rows
# ---------------------------------------------------------------------------


def test_in_progress_rows_extract_id_title():
    rows = _in_progress_task_rows(_rows(
        {"id": "1-2", "title": "写门禁", "status": "in_progress"},
        {"title": "没有 id 的行", "status": "in_progress"},
        {"id": "1-3", "status": "in_progress"},
    ))

    assert rows == [
        {"id": "1-2", "title": "写门禁"},
        {"id": "1-3", "title": ""},
    ]


def test_in_progress_rows_skip_finished_tasks():
    rows = _in_progress_task_rows(_rows(
        {"id": "1-1", "title": "已完成", "status": "completed"},
        {"id": "1-2", "title": "写门禁", "status": "in_progress"},
    ))

    assert rows == [{"id": "1-2", "title": "写门禁"}]


def test_in_progress_rows_without_rows_is_empty():
    assert _in_progress_task_rows(None) == []
    assert _in_progress_task_rows([]) == []


# ---------------------------------------------------------------------------
# The running-tasks section
# ---------------------------------------------------------------------------


def test_in_progress_section_renders_from_rows():
    elements = _in_progress_tasks_section(_rows(
        {"id": "1-2", "title": "写门禁", "status": "in_progress"},
    ))

    content = _content(elements)
    assert "1-2" in content
    assert "写门禁" in content


def test_in_progress_section_names_a_task_without_a_title():
    """A task with no title still gets a line — the id alone."""
    elements = _in_progress_tasks_section(_rows(
        {"id": "1-3", "status": "in_progress"},
    ))

    content = _content(elements)
    assert "1-3" in content
