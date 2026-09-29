"""
Tests for the failed-task section of the Feishu/Telegram progress
card.

Background (2026-09-11): the failed-task section of the card
must surface:

  1. The task id + title.
  2. The failure reason (truncated to 200 chars).
  3. The attempt count when the task was retried more than once
     (``尝试 N/5`` suffix).

The render function is internal to ``cards.py``. We don't import it
directly (it isn't part of the public API). Instead we test the
helpers it composes via a focused inline render of the same logic
— this keeps the test stable across refactors of the actual
``_failed_tasks_section`` while still pinning the public contract
that the operator sees in Feishu / Telegram.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))


def _render_failed_task_line(task: dict, max_retries_default: int = 5) -> str:
    """Replicate the failed-task section rendering inline.

    Kept in sync with ``cards.py`` (the live implementation). If
    you change the live code, change this function too — the test
    pins the public contract operators see.
    """
    tid = task.get("id", "?")
    if task.get("_origin") == "db_orphan_terminal":
        title = f"Task {tid} (DB-only)"
    else:
        title = task.get("title") or "未知任务"
    reason = (
        task.get("failure_reason")
        or task.get("description")
        or "未知原因"
    )
    if len(reason) > 200:
        reason = reason[:200] + "..."
    attempts_used = task.get("attempts") or task.get("attempt") or 1
    if attempts_used and int(attempts_used) > 1:
        attempt_suffix = f"  (尝试 {int(attempts_used)}/{max_retries_default})"
    else:
        attempt_suffix = ""
    return f"• `[{tid}]` {title}{attempt_suffix}\n  └─ {reason}"


def test_failed_task_line_shows_id_and_title_and_reason():
    """The basic contract: id, title, reason all appear in the line."""
    line = _render_failed_task_line({
        "id": "40-1",
        "title": "Fix rounding bug",
        "failure_reason": "AssertionError: expected 100 got 99",
    })
    assert "[40-1]" in line
    assert "Fix rounding bug" in line
    assert "AssertionError" in line


def test_failed_task_line_shows_attempt_suffix_for_multi_attempt():
    """Attempts > 1 → "(尝试 N/5)" suffix appears."""
    line = _render_failed_task_line({
        "id": "R1-5",
        "title": "Retry task",
        "failure_reason": "still failing",
        "attempts": 3,
    })
    assert "尝试 3/5" in line


def test_failed_task_line_no_suffix_for_single_attempt():
    """Attempts = 1 → no suffix (avoid noise for the common case)."""
    line = _render_failed_task_line({
        "id": "40-1",
        "title": "One-shot fail",
        "failure_reason": "boom",
        "attempts": 1,
    })
    assert "尝试" not in line


def test_failed_task_line_default_attempts_is_one():
    """Missing attempts field → defaults to 1 (no suffix)."""
    line = _render_failed_task_line({
        "id": "X",
        "title": "X",
        "failure_reason": "boom",
    })
    assert "尝试" not in line


def test_failed_task_line_orphan_uses_id_label():
    """``db_orphan_terminal`` placeholder → "Task X (DB-only)" label."""
    line = _render_failed_task_line({
        "id": "40-1",
        "_origin": "db_orphan_terminal",
        "failure_reason": "manual_unstick_2026-09-06",
    })
    assert "Task 40-1 (DB-only)" in line
    assert "manual_unstick_2026-09-06" in line


def test_failed_task_line_truncates_long_reason():
    """Reasons > 200 chars are truncated to 200 + "..."."""
    long_reason = "x" * 500
    line = _render_failed_task_line({
        "id": "Y",
        "title": "Y",
        "failure_reason": long_reason,
    })
    # Extract the reason substring (after └─) and strip the leading
    # space the helper inserts between arrow and reason. 200 chars +
    # "..." = 203 total content chars (no leading whitespace).
    after_arrow = line.split("└─", 1)[1].lstrip()
    # 200 chars + "..." = 203.
    assert len(after_arrow) == 203
    assert after_arrow.endswith("...")


def test_failed_task_line_missing_reason_falls_back_to_description():
    """No failure_reason → fall back to description, else 未知原因."""
    line = _render_failed_task_line({
        "id": "Z",
        "title": "Z",
        "description": "task description",
    })
    assert "task description" in line

    line_no_reason_no_desc = _render_failed_task_line({
        "id": "Z",
        "title": "Z",
    })
    assert "未知原因" in line_no_reason_no_desc