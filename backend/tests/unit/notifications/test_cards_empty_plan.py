"""Regression test for ``build_progress_card`` empty-plan edge case.

2026-09-11: three smoke-test plans (``vp-sync-targets-null``,
``vp001-pid-in-payload``,
``20260906-repair-loop``) had ``tasks.total=0`` and the old code
incorrectly rendered them as "❌ 失败" instead of an accurate label.

These tests pin the three branches the new logic adds in
``build_progress_card``:

  1. ``phase=completed + verification=passed + total=0`` → "⚪ 空 plan"
     (smoke-test artifact or genuinely empty plan that finished)
  2. ``phase=failed + total=0`` → "❌ 失败" (real plan-level failure,
     e.g. dispatcher crashed before scheduling the first task)
  3. ``phase=intermediate (e.g. interview, prd_review) + total=0`` →
     base phase label (these are normal mid-plan states)

Each test asserts the *header* (not body) — body sections still draw
from ``tasks_info`` directly so zero counts there are fine.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from notifications.cards import build_progress_card


def _summary(
    phase: str = "completed",
    stage: str = "completed",
    verification_status: str = "passed",
    total: int = 0,
    completed: int = 0,
    failed: int = 0,
    pending: int = 0,
    in_progress: int = 0,
    skipped: int = 0,
) -> Dict[str, Any]:
    """Minimal summary dict that lets the header decision run."""
    return {
        "plan_id": "test-empty-plan",
        "state": {
            "current_phase": phase,
            "stage": stage,
            "verification": {
                "status": verification_status,
                "round": 1,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "in_progress": in_progress,
            "skipped": skipped,
        },
        "execution": {
            "status": "not_started",
            "project_dir": None,
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "stop_reason": None,
            "sync_targets": None,
        },
        "artifacts": [],
    }


def _header_title(card: Dict[str, Any]) -> str:
    return card.get("header", {}).get("title", {}).get("content", "")


# --- 1. completed + verification=passed + total=0 → "⚪ 空 plan" ---

def test_total_zero_phase_completed_no_failed_label() -> None:
    """Smoke-test artifact with all tasks done but tasks.total=0 must not
    render as "❌ 失败". This is the regression case from 2026-09-11 08:12.
    """
    summary = _summary(phase="completed", stage="completed",
                       verification_status="passed", total=0)
    card = build_progress_card("vp-sync-targets-null", summary, progress=None)
    title = _header_title(card)
    assert "❌ 失败" not in title, (
        f"empty plan with phase=completed rendered as {title!r}; "
        f"expected '⚪ 空 plan'"
    )
    assert "空 plan" in title, (
        f"expected '⚪ 空 plan' in header, got {title!r}"
    )


# --- 2. failed + total=0 → "❌ 失败" (preserve failed semantics) ---

def test_total_zero_phase_failed_shows_failed_label() -> None:
    """A real plan-level failure with zero tasks should still show
    "❌ 失败" — e.g. dispatcher crashed before scheduling the first
    task. The total=0 branch must distinguish this from the
    smoke-test artifact case.
    """
    summary = _summary(phase="failed", stage="failed",
                       verification_status="pending", total=0)
    card = build_progress_card("vp001-pid-in-payload", summary, progress=None)
    title = _header_title(card)
    assert "❌ 失败" in title, (
        f"failed plan with total=0 must keep '❌ 失败' label; got {title!r}"
    )


# --- 3. intermediate phase + total=0 → base phase label ---

def test_total_zero_interview_shows_base_phase_label() -> None:
    """An interview-phase plan with zero tasks is NORMAL — tasks are
    not generated until after PRD is approved. Must not fall into the
    terminal branch and render '❌ 失败' / '⏸ 暂停'.
    """
    summary = _summary(phase="interview", stage="interview",
                       verification_status="pending", total=0)
    card = build_progress_card("test-empty-interview", summary, progress=None)
    title = _header_title(card)
    # base_phase_label maps "interview" → "需求澄清"
    assert "需求澄清" in title, (
        f"interview phase with total=0 should show base label '需求澄清'; "
        f"got {title!r}"
    )
    assert "失败" not in title, (
        f"interview phase must not render as failed; got {title!r}"
    )


# --- 4. existing terminal branch still works when total>0 ---

def test_total_positive_completed_keeps_success_label() -> None:
    """Sanity check: when tasks.total > 0 and all completed, the
    header still shows '✅ 已完成'. The new total==0 branch must not
    have broken the existing path.
    """
    summary = _summary(phase="completed", stage="completed",
                       verification_status="passed",
                       total=10, completed=10, failed=0)
    card = build_progress_card("test-real-completed", summary, progress=None)
    title = _header_title(card)
    assert "✅ 已完成" in title, (
        f"completed plan with total=10 should render '✅ 已完成'; "
        f"got {title!r}"
    )


def test_total_positive_with_failures_keeps_partial_label() -> None:
    """Sanity check: when tasks.total > 0 with some failures, the
    header shows '⚠️ 已完成（N 个失败）'. The new total==0 branch
    must not have changed this behaviour.
    """
    summary = _summary(phase="completed", stage="completed",
                       verification_status="passed",
                       total=10, completed=7, failed=3)
    card = build_progress_card("test-partial-failed", summary, progress=None)
    title = _header_title(card)
    assert "⚠️ 已完成" in title and "3 个失败" in title, (
        f"partial-failed plan should render '⚠️ 已完成（3 个失败）'; "
        f"got {title!r}"
    )