"""Regression tests: the notifier must never push a contentless card.

User report (2026-09-13): "它新推了一张卡片, 在展示执行状态, 但是那张卡片
里面除了状态什么都没有。" — a freshly pushed card whose entire body was a
single "📍 当前状态" line plus an empty "🔍 verification 状态" header.

That happens when a plan id exists in the routing store but has no
execution content at all: ``tasks.total == 0``, verification still at its
fresh-plan default (``status=pending``, ``round=0``) and
``/api/execution/{id}/progress`` returning nothing. In the wild those ids
were pytest fixtures that leaked into the live state store (``vp-*``,
``vp001-*``, ``ac-skip-test-*``); the conftest ``PDT_STATE_DB_PATH`` /
``PDT_PLANS_DIR`` redirects close that leak, and this guard makes the
notifier robust against any other source of the same shape.

Two layers are pinned:

  * ``_has_operator_content`` — the pure predicate (all three content
    sources empty → False; any one populated → True).
  * ``_handle_plan`` — end-to-end: a contentless plan produces NO
    Feishu / Telegram push and bumps the ``empty_skipped`` counter, so an
    operator can see in ``/api/debug/notifications`` that a push was
    suppressed rather than deduped or throttled.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch as _mock_patch

from status_payload import build_payload

import pytest

from notifications.feishu_notifier import FeishuNotifier, PlanCardState
from notifications.state_events import KIND_PLAN_PHASE_CHANGED


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _summary(
    *,
    tasks_total: int = 0,
    phase: str = "executing",
    stage: str = "executing",
    verification: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Minimal ``/api/plan/{id}/summary`` payload."""
    return {
        "plan_id": "plan-contentless",
        "state": {
            "plan_id": "plan-contentless",
            "stage": stage,
            "current_phase": phase,
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": verification
            or {"status": "pending", "round": 0, "max_rounds": 3,
                "stop_reason": None},
        },
        "tasks": {
            "total": tasks_total,
            "completed": 0,
            "failed": 0,
            "pending": tasks_total,
            "in_progress": 0,
        },
        "execution": {
            "status": "running" if tasks_total else "not_started",
            "project_dir": "/tmp/target",
        },
    }


@pytest.fixture
def notifier() -> FeishuNotifier:
    """A bare notifier with a chat_id injected so ``_handle_plan`` can
    get past the "no chat_id → permanently disabled" branch.
    """
    n = FeishuNotifier(min_interval_seconds=0.0)
    state = PlanCardState(plan_id="plan-contentless")
    state.chat_id = "chat-x"
    state.message_id = "om_test_contentless"
    n._states["plan-contentless"] = state
    return n


# ---------------------------------------------------------------------------
# Layer 1 — the predicate
# ---------------------------------------------------------------------------


def test_predicate_false_for_zero_task_plan_with_no_history() -> None:
    """No tasks + fresh verification + no progress → NOT content."""
    assert FeishuNotifier._has_operator_content(_summary(), {}) is False


def test_predicate_true_when_tasks_exist() -> None:
    """A single task is enough — the task bar and lists render."""
    assert FeishuNotifier._has_operator_content(_summary(tasks_total=3), {}) is True


def test_predicate_true_when_verification_has_run() -> None:
    """A recorded verification round renders the verification section."""
    summary = _summary(
        verification={"status": "failed", "round": 2, "max_rounds": 3,
                      "stop_reason": None}
    )
    assert FeishuNotifier._has_operator_content(summary, {}) is True


def test_predicate_true_when_progress_has_task_rows() -> None:
    """``/api/execution/{id}/progress`` task rows count even when the
    summary snapshot lags behind (it is fetched separately).
    """
    assert FeishuNotifier._has_operator_content(
        _summary(), {"tasks": [{"id": "1-1", "status": "completed"}]}
    ) is True


def test_predicate_true_when_progress_has_current_task() -> None:
    assert FeishuNotifier._has_operator_content(
        _summary(), {"current": {"id": "1-1", "title": "do the thing"}}
    ) is True


# ---------------------------------------------------------------------------
# Layer 2 — end-to-end suppression
# ---------------------------------------------------------------------------


def test_handle_plan_skips_push_for_contentless_plan(notifier: FeishuNotifier) -> None:
    """No Feishu / Telegram call, and ``empty_skipped`` is bumped."""
    with _mock_patch(
        "notifications.feishu_notifier.fetch_plan_status",
        return_value=build_payload("plan-contentless", _summary(), {}, None),
    ), _mock_patch.object(
        notifier, "_push_to_feishu",
    ) as push_feishu, _mock_patch.object(
        notifier, "_push_to_telegram",
    ) as push_telegram:
        notifier._handle_plan(
            "plan-contentless", {KIND_PLAN_PHASE_CHANGED}, final_flush=False,
        )

    assert push_feishu.call_count == 0, (
        "a contentless card must never reach the Feishu transport"
    )
    assert push_telegram.call_count == 0, (
        "a contentless card must never reach the Telegram transport"
    )
    assert notifier._empty_skipped == 1, (
        "suppressed pushes must be observable via stats()['empty_skipped']"
    )


def test_handle_plan_still_pushes_when_tasks_exist(notifier: FeishuNotifier) -> None:
    """Counter-check: the guard must not suppress plans that DO have
    content, otherwise it would silence real progress cards.
    """
    with _mock_patch(
        "notifications.feishu_notifier.fetch_plan_status",
        return_value=build_payload(
            "plan-contentless", _summary(tasks_total=2),
            {"tasks": [{"id": "1-1", "status": "in_progress"}]}, None,
        ),
    ), _mock_patch.object(
        notifier, "_push_to_feishu", return_value=True,
    ) as push_feishu, _mock_patch.object(
        notifier, "_push_to_telegram", return_value=True,
    ):
        notifier._handle_plan(
            "plan-contentless", {KIND_PLAN_PHASE_CHANGED}, final_flush=False,
        )

    assert push_feishu.call_count == 1, (
        "a plan with tasks must still push its card"
    )
    assert notifier._empty_skipped == 0


def test_stats_exposes_empty_skipped(notifier: FeishuNotifier) -> None:
    """``/api/debug/notifications`` reads ``stats()`` — the new counter
    must be part of it so an operator can distinguish "suppressed as
    empty" from "deduped" or "throttled".
    """
    assert "empty_skipped" in notifier.stats()
