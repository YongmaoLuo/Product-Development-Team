"""Unit tests for ``_rebuild_card`` verification-card routing (2026-09-11 plan v4).

Symptom: plans whose verification had reached a terminal state lost
their verification section — only the execution section rendered.

Root cause: ``_rebuild_card`` routing flag ``wants_verification`` only
triggered when ``current_phase in VERIFICATION_PHASES`` or
``KIND_VP_STATE_CHANGED`` was in the event kinds. Plans with
``current_phase="completed"`` (execution terminal) +
``verification.status in {"passed","failed","loop_stopped"}`` (verification
terminal) fell through to ``build_progress_card`` which has no
verification section.

Fix: extend ``wants_verification`` to also accept plans with terminal
``verification.status`` — verified that
``/api/verification/{id}/progress`` continues to return data for
terminal plans, and that ``build_verification_card`` already renders
correctly via its ``is_terminal`` branch (cards.py:813).

These tests pin the routing table:

  1. ``verification.status="passed"`` → build_verification_card → header "✅ 验证通过 · "
  2. ``verification.status="failed"`` → build_verification_card → header "❌ 验证失败 · "
  3. ``verification.status="loop_stopped"`` → build_verification_card → header "❌ 验证失败 · "
  4. ``current_phase in VERIFICATION_PHASES`` → build_verification_card (regression)
  5. ``current_phase="executing" + verification.status="running"`` → build_progress_card (regression)
  6. smoke-test plan: no verification state → build_progress_card (regression)

All tests run against the real ``FeishuNotifier._rebuild_card`` with
patched fetcher functions so no real network calls happen.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import patch as _mock_patch

import pytest

from notifications.feishu_notifier import FeishuNotifier
from status_payload import build_payload


# --- Test fixtures / helpers ---


def _summary(
    phase: str = "completed",
    stage: str = "completed",
    verification_status: Optional[str] = "passed",
    tasks_total: int = 10,
    tasks_completed: int = 10,
    tasks_failed: int = 0,
) -> Dict[str, Any]:
    """Minimal ``/api/plan/{id}/summary`` response dict."""
    return {
        "plan_id": "test-plan",
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
            "total": tasks_total,
            "completed": tasks_completed,
            "failed": tasks_failed,
            "pending": 0,
            "in_progress": 0,
            "skipped": 0,
        },
        "execution": {
            "status": "completed",
            "project_dir": None,
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "stop_reason": None,
            "sync_targets": None,
        },
        "artifacts": [],
    }


def _execution_progress() -> Dict[str, Any]:
    """Minimal ``/api/execution/{id}/progress`` response."""
    return {
        "plan_id": "test-plan",
        "status": "completed",
        "tasks": [],
    }


def _verification_progress(status: str = "passed") -> Dict[str, Any]:
    """Minimal ``/api/verification/{id}/progress`` response."""
    return {
        "plan_id": "test-plan",
        "verification_status": status,
        "verification_round": 1,
        "max_rounds": 3,
        "stop_reason": None,
        "current_vp": None,
        "completed_vps": [],
        "failed_vps": [],
        "skipped_vps": [],
        "pending_vps": [],
        "vps": [],
        "current_layer": None,
        "layer_summaries": {},
        "counts": {
            "completed": 5, "failed": 0, "skipped": 0,
            "in_progress": 0, "pending": 0, "total": 5,
        },
        "last_updated_at": "2026-09-11T00:00:00Z",
    }


@pytest.fixture
def notifier() -> FeishuNotifier:
    """A bare ``FeishuNotifier`` with default config — no ``start()``,
    no network. Used to invoke ``_rebuild_card`` directly.
    """
    return FeishuNotifier()


def _rebuild_with(
    notifier: FeishuNotifier,
    summary: Dict[str, Any],
    verification: Optional[Dict[str, Any]] = None,
    execution: Optional[Dict[str, Any]] = None,
    kinds: Optional[set] = None,
):
    """Invoke ``_rebuild_card`` with patched fetcher functions.

    Returns the ``(card, phase, verification_status, summary)`` tuple.
    """
    if kinds is None:
        kinds = set()
    exec_progress = execution if execution is not None else _execution_progress()
    payload = build_payload("test-plan", summary, exec_progress, verification)
    with _mock_patch(
        "notifications.feishu_notifier.fetch_plan_status",
        return_value=payload,
    ):
        return notifier._rebuild_card("test-plan", kinds)


def _header_title(card: Dict[str, Any]) -> str:
    return card.get("header", {}).get("title", {}).get("content", "")


# --- 1. terminal verification_status="passed" → verification card ---


def test_wants_verification_when_verification_status_passed(
    notifier: FeishuNotifier,
) -> None:
    """Plans with ``current_phase="completed" + verification.status="passed"``
    must render the verification card (header "✅ 验证通过 · ..."),
    NOT the progress card. This is the 2026-09-11 regression case — in
    that state the verification section disappeared entirely.
    """
    summary = _summary(
        phase="completed",
        stage="completed",
        verification_status="passed",
        tasks_total=79,
        tasks_completed=77,
        tasks_failed=2,
    )
    card, _, _, _ = _rebuild_with(
        notifier,
        summary=summary,
        verification=_verification_progress(status="passed"),
    )
    title = _header_title(card)
    assert "✅ 验证通过" in title, (
        f"plan with verification.status=passed must show '✅ 验证通过' "
        f"header; got {title!r}"
    )
    assert "test-plan" in title, (
        f"header must include plan_id; got {title!r}"
    )


# --- 2. terminal verification_status="failed" → verification card ---


def test_wants_verification_when_verification_status_failed(
    notifier: FeishuNotifier,
) -> None:
    """Plans with ``verification.status="failed"`` must render the
    verification card with header "❌ 验证失败 · ...".
    """
    summary = _summary(
        phase="completed",
        stage="completed",
        verification_status="failed",
        tasks_total=10,
        tasks_completed=8,
        tasks_failed=2,
    )
    card, _, _, _ = _rebuild_with(
        notifier,
        summary=summary,
        verification=_verification_progress(status="failed"),
    )
    title = _header_title(card)
    assert "❌ 验证失败" in title, (
        f"plan with verification.status=failed must show '❌ 验证失败' "
        f"header; got {title!r}"
    )


# --- 3. terminal verification_status="loop_stopped" → verification card ---


def test_wants_verification_when_verification_status_loop_stopped(
    notifier: FeishuNotifier,
) -> None:
    """Plans with ``verification.status="loop_stopped"`` (max rounds
    exhausted without operator reset) must also route to verification
    card. The header groups ``failed`` / ``loop_stopped`` under the
    same "❌ 验证失败" label per cards.py:833-835.
    """
    summary = _summary(
        phase="completed",
        stage="completed",
        verification_status="loop_stopped",
        tasks_total=5,
        tasks_completed=5,
        tasks_failed=0,
    )
    card, _, _, _ = _rebuild_with(
        notifier,
        summary=summary,
        verification=_verification_progress(status="loop_stopped"),
    )
    title = _header_title(card)
    assert "❌ 验证失败" in title, (
        f"plan with verification.status=loop_stopped must show "
        f"'❌ 验证失败' header; got {title!r}"
    )


# --- 4. current_phase in VERIFICATION_PHASES → verification card (regression) ---


def test_wants_verification_when_phase_in_verification_phases(
    notifier: FeishuNotifier,
) -> None:
    """Plans with ``current_phase`` in ``VERIFICATION_PHASES`` (active
    verification loop) must continue to route to the verification card.
    This pins the existing path that the v4 fix must NOT have broken.
    """
    summary = _summary(
        phase="verification_repairing",
        stage="verification",
        verification_status="running",
        tasks_total=10,
        tasks_completed=8,
        tasks_failed=2,
    )
    card, _, _, _ = _rebuild_with(
        notifier,
        summary=summary,
        verification=_verification_progress(status="running"),
    )
    title = _header_title(card)
    # verification_repairing → "🔧 正在生成修复任务"
    assert "🔧 正在生成修复任务" in title, (
        f"plan with current_phase=verification_repairing must show "
        f"'🔧 正在生成修复任务' header; got {title!r}"
    )


# --- 5. current_phase="executing" + verification.status="running" → progress card ---


def test_progress_card_when_verification_status_running(
    notifier: FeishuNotifier,
) -> None:
    """Plans with ``current_phase="executing"`` and verification not
    yet started (status="running" but not in terminal set) must
    continue to route to the progress card. The v4 fix only added
    terminal-status routing — must NOT have over-extended to running.
    """
    summary = _summary(
        phase="executing",
        stage="executing",
        verification_status="running",  # not in VERIFICATION_TERMINAL_STATUSES
        tasks_total=10,
        tasks_completed=5,
        tasks_failed=0,
    )
    card, _, _, _ = _rebuild_with(
        notifier,
        summary=summary,
        verification=_verification_progress(status="running"),
    )
    title = _header_title(card)
    # progress card for executing phase shows execution progress bar
    # (NOT verification card header like "🔄 验证中")
    assert "验证通过" not in title and "验证失败" not in title, (
        f"plan with verification.status=running (not terminal) must "
        f"NOT route to verification card; got header {title!r}"
    )


# --- 6. smoke-test plan: no verification state → progress card (regression) ---


def test_progress_card_when_no_verification_state(
    notifier: FeishuNotifier,
) -> None:
    """Smoke-test / fixture plans (e.g. ``vp-sync-targets-null``) have
    no verification state. They must continue to route to the
    progress card. The empty-plan fix from 2026-09-11 v1 still applies
    ("⚪ 空 plan" instead of "❌ 失败").
    """
    summary = _summary(
        phase="completed",
        stage="completed",
        verification_status=None,  # missing — no verification state
        tasks_total=0,
        tasks_completed=0,
        tasks_failed=0,
    )
    # No verification progress fetch — fetch returns None for
    # plans that have never run verification.
    card, _, _, _ = _rebuild_with(
        notifier,
        summary=summary,
        verification=None,  # /api/verification/{id}/progress returns None
    )
    title = _header_title(card)
    # Smoke-test plan → "⚪ 空 plan" (from build_progress_card's total=0 branch)
    assert "⚪ 空 plan" in title, (
        f"smoke-test plan (no verification, total=0) must show "
        f"'⚪ 空 plan' header; got {title!r}"
    )
