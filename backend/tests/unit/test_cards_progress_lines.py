"""Unit tests for ``cards.build_progress_lines`` — the shared progress area.

The Feishu progress card answers three questions in its body: where
the plan is right now, how many tasks are done, and what is running
at this moment. The scheduler page's plan-detail panel answers the
same three questions, and it must not grow a second phrasing of them
— the wording lives in the card's section builders, and the web view
reuses those builders instead of writing its own sentences.

These tests pin:

  * a payload with a phase produces a ``phase`` line first, worded
    exactly the way the card words it (the same builder, not a copy);
  * the lines come from the card's own sections — replacing one of
    those sections changes what this function returns;
  * a payload without a ``summary`` still answers, with the phase
    line alone;
  * a plan with nothing running produces no ``task`` line.
"""
from __future__ import annotations

from typing import Any, Dict, List

from notifications import cards
from notifications.cards import _unified_phase_section, build_progress_lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(**overrides: Any) -> Dict[str, Any]:
    """A plan-status-shaped payload; the minimum is a plan and a phase."""
    payload: Dict[str, Any] = {
        "plan_id": "p1",
        "current_phase": "verification",
        "summary": {"total": 5, "done": 2},
    }
    payload.update(overrides)
    return payload


def _card_lines(elements: Any) -> List[str]:
    """The text lines a card section rendered, one entry per line."""
    lines: List[str] = []
    for element in elements or []:
        text = element.get("text") if isinstance(element, dict) else None
        content = text.get("content") if isinstance(text, dict) else None
        for raw in str(content or "").splitlines():
            line = raw.strip()
            if line:
                lines.append(line)
    return lines


def _kinds(out: Dict[str, Any]) -> List[str]:
    return [line["kind"] for line in out["lines"]]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_progress_lines_include_phase_section():
    out = build_progress_lines(_payload())

    assert out["title"] == "p1"
    assert out["lines"], "a payload with a phase must produce at least one line"
    assert out["lines"][0]["kind"] == "phase"
    # The wording is the card's wording: the phase line is one of the
    # lines the card's own phase section renders, not a second
    # phrasing of the same fact.
    expected = _card_lines(_unified_phase_section("verification", verification_status=""))
    assert out["lines"][0]["text"] in expected


def test_progress_lines_reuse_card_sections(monkeypatch):
    """The running-tasks section is the card's, not a copy of it."""
    payload = _payload(
        execution={
            "tasks": [
                {"id": "T3", "title": "修复空指针", "status": "in_progress"},
            ],
        },
    )

    before = build_progress_lines(payload)
    before_task_lines = [
        line["text"] for line in before["lines"] if line["kind"] == "task"
    ]
    assert before_task_lines, "the running task must show up as a task line"

    def fake_section(task_progress, *args, **kwargs):
        return [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "🔄 **正在执行：**\n- `[T3]` 来自卡片构建器",
            },
        }]

    monkeypatch.setattr(cards, "_in_progress_tasks_section", fake_section)

    after = build_progress_lines(payload)
    after_task_lines = [
        line["text"] for line in after["lines"] if line["kind"] == "task"
    ]
    assert after_task_lines != before_task_lines, (
        "the lines must follow the card's section builder, not a copy of it"
    )
    assert any("来自卡片构建器" in text for text in after_task_lines), after


def test_progress_lines_tolerate_missing_summary():
    out = build_progress_lines({"plan_id": "p2", "current_phase": "executing"})

    assert isinstance(out, dict)
    assert out["lines"], "a payload without a summary still answers with its phase"
    assert out["lines"][0]["kind"] == "phase"
    assert "summary" not in _kinds(out)


def test_progress_lines_without_running_tasks_have_no_task_line():
    out = build_progress_lines(_payload(
        current_phase="executing",
        execution={
            "tasks": [
                {"id": "T1", "title": "已完成", "status": "completed"},
                {"id": "T2", "title": "待执行", "status": "pending"},
            ],
        },
    ))

    assert isinstance(out, dict)
    assert "task" not in _kinds(out), out["lines"]


def test_progress_lines_task_text_comes_from_the_card_section():
    running = [{"id": "T3", "title": "修复空指针", "status": "in_progress"}]
    out = build_progress_lines(_payload(execution={"tasks": running}))

    card_texts = _card_lines(cards._in_progress_tasks_section(running))
    task_texts = [line["text"] for line in out["lines"] if line["kind"] == "task"]
    assert task_texts
    for text in task_texts:
        assert text in card_texts
