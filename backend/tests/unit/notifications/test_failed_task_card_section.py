"""Regression tests for :func:`cards.build_progress_card` failed-task section.

2026-09-09: the Feishu / Telegram
progress card's "❌ 失败任务详情" section silently showed nothing even
when ``counts.failed > 0`` in the summary. Root cause was reading
``summary.execution_progress.tasks`` (which the ``/api/plan/{id}/summary``
endpoint NEVER sets — it carries only aggregated rollups) instead of
``progress.tasks`` (the live per-task list returned by
``/api/execution/{id}/progress``).

Audit trigger: a plan had a failed task visible in the per-task API
and the live card
showed only the aggregate "❌1" without naming the task or its
failure reason — the operator had to manually run ``curl
/api/execution/{id}/progress`` to find out which task failed.

These tests pin the corrected contract:
  1. ``failed > 0`` + a non-empty ``progress.tasks`` with a
     ``status == "failed"`` entry → the rendered card MUST include a
     "❌ 失败任务详情" line + a per-task line naming the failed task.
  2. ``failed > 0`` + empty ``progress.tasks`` → no failed-task line
     (we don't fabricate from counts alone; the operator should fix
     the upstream bug).
  3. ``failed == 0`` → no failed-task line regardless of progress
     contents.
  4. ``skipped > 0`` + a ``progress.tasks`` entry with
     ``status == "skipped"`` → the rendered card MUST include the
     "⏭ 跳过的任务" line.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _card_text(card: dict) -> str:
    """Concatenate every ``text.content`` line in the card body."""
    parts: list = []
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            content = el.get("text", {}).get("content", "")
            if content:
                parts.append(content)
    return "\n".join(parts)


def _summary(failed: int = 0, skipped: int = 0) -> dict:
    """Build a minimal ``summary`` payload matching ``/api/plan/{id}/summary``."""
    return {
        "state": {
            "current_phase": "executing",
            "stage": "executing",
            "verification": {"status": "", "round": 0, "max_rounds": 5},
        },
        # NOTE: the summary endpoint does NOT populate
        # ``execution_progress`` — that was the original bug. We
        # intentionally leave it absent to prove the card falls back
        # to ``progress.tasks`` instead.
        "tasks": {
            "total": 28,
            "completed": 10,
            "failed": failed,
            "skipped": skipped,
            "in_progress": 0,
            "pending": 17,
        },
    }


def _progress(tasks: list) -> dict:
    """Build a ``progress`` payload matching ``/api/execution/{id}/progress``."""
    return {
        "plan_id": "test-plan",
        "execution_status": "running",
        "tasks": tasks,
        "counts": {"total": len(tasks)},
        "current": None,
        "next": None,
    }


def test_failed_task_section_shows_task_id_and_title():
    """Regression: failed-task section must include the failed task's id."""
    from notifications.cards import build_progress_card

    failed_task = {
        "id": "11-2",
        "title": "Rust:把 trend/consolidation 分类接入生产 emit 路径",
        "status": "failed",
        "failure_reason": "detect_metric_divergences 不携带 trend/consolidation 标签",
    }
    progress = _progress([failed_task])
    summary = _summary(failed=1)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)

    assert "❌ 失败任务详情" in text, (
        f"Card must include the failed-task heading. Got: \n{text}"
    )
    assert "[11-2]" in text, (
        f"Card must include the failed task id. Got: \n{text}"
    )
    assert "trend/consolidation" in text, (
        f"Card must include the failed task title. Got: \n{text}"
    )
    assert "detect_metric_divergences" in text, (
        f"Card must include the failure reason (truncated to 200 chars). "
        f"Got: \n{text}"
    )


def test_failed_task_section_absent_when_progress_empty():
    """When ``progress.tasks`` is empty, do NOT fabricate a failed line.

    Without the fix, the card code read ``summary.execution_progress``
    which was always empty, so it silently dropped the section. With
    the fix, the code now reads ``progress.tasks`` — if THAT is also
    empty (upstream bug), the section still does not render. We do
    not guess from ``counts.failed > 0`` alone, because the operator
    needs to know the upstream bug exists rather than see a phantom
    line.
    """
    from notifications.cards import build_progress_card

    progress = _progress([])  # upstream bug — per-task list missing
    summary = _summary(failed=1)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)

    assert "❌ 失败任务详情" not in text, (
        "Card must not fabricate failed-task lines when progress.tasks is empty"
    )


def test_failed_task_section_absent_when_no_failures():
    from notifications.cards import build_progress_card

    completed = {
        "id": "1-1", "title": "test", "status": "completed",
    }
    progress = _progress([completed])
    summary = _summary(failed=0)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)
    assert "❌ 失败任务详情" not in text


def test_skipped_task_section_shows_task_id():
    from notifications.cards import build_progress_card

    skipped_task = {
        "id": "5-1", "title": "已通过 refiner 跳过", "status": "skipped",
    }
    progress = _progress([skipped_task])
    summary = _summary(skipped=1)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)

    assert "⏭ 跳过的任务" in text
    assert "[5-1]" in text


def test_failed_task_long_reason_truncated_to_200_chars():
    """failure_reason > 200 chars must be truncated for the card."""
    from notifications.cards import build_progress_card

    long_reason = "x" * 500
    failed_task = {
        "id": "X",
        "title": "test",
        "status": "failed",
        "failure_reason": long_reason,
    }
    progress = _progress([failed_task])
    summary = _summary(failed=1)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)
    assert "..." in text, "Long failure_reason should be truncated with ..."
    # The full 500-char reason should not appear verbatim.
    assert long_reason not in text


def test_failed_task_missing_reason_shows_unknown():
    """When failure_reason is missing, show '未知原因' (operator cue)."""
    from notifications.cards import build_progress_card

    failed_task = {
        "id": "X", "title": "test", "status": "failed",
        # NOTE: no failure_reason field
    }
    progress = _progress([failed_task])
    summary = _summary(failed=1)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)
    assert "未知原因" in text


def test_failed_task_defensive_isinstance_filter():
    """``progress.tasks`` may contain non-dict entries (e.g. strings from
    partial hydration) — the filter must not crash on them.
    """
    from notifications.cards import build_progress_card

    # Mix of dict and string entries — only dicts should be inspected.
    progress = _progress([
        "stray-string-entry",
        {"id": "Y", "title": "valid failed", "status": "failed"},
    ])
    summary = _summary(failed=1)

    card = build_progress_card("test-plan", summary, progress=progress)
    text = _card_text(card)
    assert "[Y]" in text, "Valid failed entry must render"