"""Tests for the card "被阻塞的待执行任务" section.

2026-09-10: the card title was showing
``⏸ 暂停（上游阻塞）`` but the body never told the operator
*which* tasks were blocked and *why*. These tests pin down:

  * ``_blocked_tasks_section`` surfaces every pending task that has
    at least one failed or still-pending dep.
  * Each blocked task is rendered with a clear marker — ``被 [...] 阻塞（失败）``
    for failed upstream deps vs ``等待 [...]`` for cascade waiting.
  * Tasks with all-completed deps (i.e. truly schedulable) are NOT
    rendered (they're not actually blocked).
  * Tasks with no ``depends_on`` are not blocked.

Pure-function tests, no fixtures / DB needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``notifications`` importable when running this file directly.
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from notifications.cards import _blocked_tasks_section, _extract_failure_context  # noqa: E402


# ---------------------------------------------------------------------------
# _blocked_tasks_section
# ---------------------------------------------------------------------------

def test_blocked_section_lists_pending_failed_dep():
    """Pending task with a failed upstream dep is listed as 'blocked by failure'."""
    progress = {
        "tasks": [
            {"id": "1-1", "status": "completed"},
            {"id": "1-2", "status": "failed", "depends_on": ["1-1"]},
            {"id": "1-3", "status": "pending", "depends_on": ["1-2"]},
        ]
    }
    out = _blocked_tasks_section(progress)
    # Section has 2 divs: heading + bullet list. Bullet is the LAST div.
    divs = [e for e in out if e.get("tag") == "div"]
    assert len(divs) == 2, f"expected heading + bullet, got {len(divs)} divs"
    content = divs[-1]["text"]["content"]
    assert "1-3" in content
    assert "被" in content
    assert "1-2" in content
    assert "失败" in content


def test_blocked_section_lists_waiting_cascade():
    """Pending task whose dep is still pending shows up as 'waiting'."""
    progress = {
        "tasks": [
            {"id": "1-1", "status": "pending"},
            {"id": "1-2", "status": "pending", "depends_on": ["1-1"]},
            {"id": "1-3", "status": "pending", "depends_on": ["1-2"]},
        ]
    }
    out = _blocked_tasks_section(progress)
    divs = [e for e in out if e.get("tag") == "div"]
    content = divs[-1]["text"]["content"]
    # 1-2 is waiting on 1-1 (also pending) → "等待 [1-1]"
    assert "1-2" in content and "等待" in content and "1-1" in content
    # 1-3 is waiting on 1-2 (also pending) → "等待 [1-2]"
    assert "1-3" in content and "1-2" in content


def test_blocked_section_skips_truly_schedulable_tasks():
    """Pending task whose all deps are completed is NOT blocked."""
    progress = {
        "tasks": [
            {"id": "1-1", "status": "completed"},
            {"id": "1-2", "status": "pending", "depends_on": ["1-1"]},
        ]
    }
    assert _blocked_tasks_section(progress) == []


def test_blocked_section_skips_pending_with_no_deps():
    """Pending task with no depends_on is schedulable, not blocked."""
    progress = {
        "tasks": [
            {"id": "1-1", "status": "pending", "depends_on": []},
        ]
    }
    assert _blocked_tasks_section(progress) == []


def test_blocked_section_skips_completed_tasks():
    """Only ``pending`` tasks are inspected; completed / failed rows are not re-listed."""
    progress = {
        "tasks": [
            {"id": "1-1", "status": "failed", "depends_on": []},
            {"id": "1-2", "status": "completed", "depends_on": ["1-1"]},
        ]
    }
    assert _blocked_tasks_section(progress) == []


def test_blocked_section_returns_empty_for_none():
    """None progress is tolerated (returns [])."""
    assert _blocked_tasks_section(None) == []
    assert _blocked_tasks_section({}) == []


def test_blocked_section_handles_defensive_input():
    """``progress.tasks`` may contain non-dict entries (partial hydrate);
    they must not crash the section."""
    progress = {
        "tasks": [
            "garbage",  # str, not a dict
            {"id": "1-1", "status": "completed"},
            None,
            {"id": "1-2", "status": "pending", "depends_on": ["1-1"]},
        ]
    }
    # 1-2 has dep 1-1 (completed) → not blocked → no output
    assert _blocked_tasks_section(progress) == []


def test_blocked_section_realistic_layout():
    """The graph that triggered this fix: one task failed and everything
    downstream of it is blocked."""
    progress = {
        "tasks": [
            {"id": "11-1", "status": "completed"},
            {"id": "11-2", "status": "failed", "depends_on": ["11-1"]},
            {"id": "11-3", "status": "completed"},
            {"id": "11-4", "status": "pending", "depends_on": ["11-2", "11-3"]},
            {"id": "11-5-1", "status": "pending", "depends_on": ["11-4"]},
            {"id": "11-5-2", "status": "pending", "depends_on": ["11-5-1"]},
            {"id": "11-5-3", "status": "pending", "depends_on": ["11-5-2"]},
            {"id": "11-6", "status": "pending", "depends_on": ["11-5-3"]},
        ]
    }
    out = _blocked_tasks_section(progress)
    divs = [e for e in out if e.get("tag") == "div"]
    content = divs[-1]["text"]["content"]
    # 11-4 has dep 11-2 (failed) → "被 11-2 阻塞"
    assert "11-4" in content and "被" in content and "11-2" in content
    # 11-5-* and 11-6 are all waiting on prior pending
    for tid in ("11-5-1", "11-5-2", "11-5-3", "11-6"):
        assert tid in content, f"missing {tid} from blocked section"
    # Five bullet rows
    assert content.count("•") == 5


# ---------------------------------------------------------------------------
# _extract_failure_context (failure_reason=None → "任务背景: …" fallback)
# ---------------------------------------------------------------------------

def test_extract_failure_context_returns_background_section():
    """A description with a ``## 背景`` heading should yield the paragraph
    immediately after that heading, not the whole spec."""
    desc = (
        "## 背景\n"
        "11-1 已确认 detect_signal_at 是死代码,生产路径不携带 trend/consolidation。\n"
        "\n"
        "## 目标\n"
        "让生产 emit 路径产出的每条信号记录都携带 trend/consolidation 分类。\n"
    )
    snippet = _extract_failure_context(desc)
    assert "11-1 已确认" in snippet
    assert "让生产 emit" not in snippet, "should not bleed into 目标 section"
    assert len(snippet) <= 200


def test_extract_failure_context_truncates_long_snippets():
    """If the description is longer than max_chars, snippet ends with ellipsis."""
    desc = "## 背景\n" + ("非常长的中文" * 50)
    snippet = _extract_failure_context(desc, max_chars=80)
    assert snippet.endswith("…")
    assert len(snippet) <= 81


def test_extract_failure_context_handles_missing_or_empty():
    """Empty / None description returns empty string (not crash, not '未知原因')."""
    assert _extract_failure_context(None) == ""
    assert _extract_failure_context("") == ""
    assert _extract_failure_context("   \n  ") == ""


def test_extract_failure_context_falls_back_to_first_chunk():
    """A description with no Chinese-context heading returns the first
    ``max_chars`` of text (leading bullets and headings stripped)."""
    desc = "Some plain text about a failed task.\nMore details."
    snippet = _extract_failure_context(desc, max_chars=80)
    assert "Some plain text" in snippet