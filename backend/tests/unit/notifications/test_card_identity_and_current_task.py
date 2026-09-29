"""2026-09-14 card identity + current-task naming fixes.

Two header gaps are pinned here:

* Execution-phase headers ("✅ 已完成" / "⚠️ 已完成（N 个失败）" /
    "⏸ 暂停…" / "❌ 失败" / base phase labels) carried NO plan identity,
    while every verification branch appends "· {plan_id}". Every header
    title now carries the plan id.

  * "它显示在执行中, 正在执行task, 但是实际上又没有在显示具体在执行什么
    task" — the unified line said "正在跑 tasks" with no task identity.
    ``_unified_phase_section`` now accepts an optional ``current_task``
    (id, title) and names the running task inline; ``build_card`` derives
    it from the first in_progress row of the execution progress payload.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from status_payload import status_from
from notifications.cards import (
    _resolve_header,
    _unified_phase_section,
    build_card,
)


# ---------------------------------------------------------------------------
# B — every header title carries the plan id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,tasks_info,expected",
    [
        # Execution terminal — completed clean.
        (
            {"current_phase": "completed"},
            {"total": 3, "completed": 3, "failed": 0,
             "pending": 0, "in_progress": 0, "skipped": 0},
            "✅ 已完成 · plan-x",
        ),
        # Execution terminal — completed with failures.
        (
            {"current_phase": "completed"},
            {"total": 4, "completed": 3, "failed": 1,
             "pending": 0, "in_progress": 0, "skipped": 0},
            "⚠️ 已完成（1 个失败） · plan-x",
        ),
        # Execution terminal — dispatcher stopped with pending remain.
        (
            {"current_phase": "failed"},
            {"total": 5, "completed": 2, "failed": 1,
             "pending": 2, "in_progress": 0, "skipped": 0},
            "⏸ 暂停（上游阻塞） · plan-x",
        ),
        # Execution terminal — stopped mid-task.
        (
            {"current_phase": "failed"},
            {"total": 5, "completed": 2, "failed": 1,
             "pending": 0, "in_progress": 2, "skipped": 0},
            "⏸ 暂停（执行中断） · plan-x",
        ),
        # Execution terminal — nothing finished.
        (
            {"current_phase": "failed"},
            {"total": 3, "completed": 0, "failed": 0,
             "pending": 0, "in_progress": 0, "skipped": 0},
            "❌ 失败 · plan-x",
        ),
        # Mid-plan base label.
        (
            {"current_phase": "executing"},
            {"total": 3, "completed": 1, "failed": 0,
             "pending": 2, "in_progress": 0, "skipped": 0},
            "执行中 · plan-x",
        ),
        # Empty-plan failure.
        (
            {"current_phase": "failed"},
            {"total": 0, "completed": 0, "failed": 0,
             "pending": 0, "in_progress": 0, "skipped": 0},
            "❌ 失败 · plan-x",
        ),
    ],
)
def test_header_titles_carry_plan_id(state, tasks_info, expected):
    from plan_status import PlanStatus

    header = _resolve_header(PlanStatus(
        plan_id="plan-x",
        phase=(state or {}).get("current_phase") or "",
        # These cases are about the execution-terminal labels;
        # nothing is running in any of them (2026-09-23: the
        # header no longer guesses this from task counts).
        execution_in_flight=False,
        tasks=dict(tasks_info or {}),
    ), dict(tasks_info or {}))
    assert header["title"] == expected


# ---------------------------------------------------------------------------
# C — the unified line names the running task
# ---------------------------------------------------------------------------


def _line_text(elements) -> str:
    return "\n".join(
        e.get("text", {}).get("content", "") for e in elements
    )


def test_unified_line_names_current_task_with_title():
    elements = _unified_phase_section(
        "executing",
        verification_status="",
        current_task=("1-2", "修复 VP-013 的 test_command"),
    )
    text = _line_text(elements)
    assert "正在跑 `[1-2]` 修复 VP-013 的 test_command" in text


def test_unified_line_falls_back_to_id_only_task():
    elements = _unified_phase_section(
        "executing",
        verification_status="",
        current_task=("1-2", ""),
    )
    text = _line_text(elements)
    assert "正在跑 task `[1-2]`" in text


def test_unified_line_unchanged_without_current_task():
    """Backward compat: no current_task → the bare label stays."""
    elements = _unified_phase_section(
        "executing",
        verification_status="",
    )
    text = _line_text(elements)
    assert text.endswith("正在跑 tasks")


def test_build_card_derives_current_task_from_progress():
    """End-to-end: build_card picks the in_progress row out of the
    execution progress payload and names it in the unified line."""
    summary: Dict[str, Any] = {
        "state": {
            "current_phase": "executing",
            "stage": "executing",
            "verification": {"status": "pending", "round": 0,
                             "max_rounds": 3, "stop_reason": None},
        },
        "tasks": {"total": 3, "completed": 1, "failed": 0,
                  "pending": 1, "in_progress": 1, "skipped": 0},
        "execution": {"status": "running", "project_dir": "/tmp/p"},
    }
    execution_progress: Dict[str, Any] = {
        "tasks": [
            {"id": "1-1", "status": "completed", "title": "done"},
            {"id": "1-2", "status": "in_progress", "title": "修复登录跳转"},
        ],
    }
    card = build_card(
        "plan-x",
        status_from(summary, execution=execution_progress, plan_id="plan-x"),
        summary,
        execution_progress=execution_progress,
    )
    body_text = "\n".join(
        e.get("text", {}).get("content", "")
        for e in card.get("elements", [])
        if isinstance(e, dict)
    )
    assert "正在跑 `[1-2]` 修复登录跳转" in body_text


# ---------------------------------------------------------------------------
# 2026-09-14 — the verification window names the current VP
# ---------------------------------------------------------------------------

from notifications.cards import _verification_sections  # noqa: E402


def _vp_progress(current_vp) -> Dict[str, Any]:
    return {
        "verification_status": "running",
        "verification_round": 2,
        "max_rounds": 3,
        "vps": [],
        "current_vp": current_vp,
        "counts": {"total": 30, "completed": 20, "failed": 1,
                   "skipped": 0, "in_progress": 1},
    }


def test_unified_line_names_current_vp():
    elements = _unified_phase_section(
        "verification_running",
        verification_status="running",
        verification_round=2,
        verification_max_rounds=3,
        current_vp={"id": "VP-023", "title": "Nightly CI 全过"},
    )
    text = _line_text(elements)
    assert "🔍 正在验证 `[VP-023]` Nightly CI 全过" in text


def test_unified_line_falls_back_to_bare_running_without_vp():
    elements = _unified_phase_section(
        "verification_running",
        verification_status="running",
        verification_round=2,
        verification_max_rounds=3,
    )
    text = _line_text(elements)
    assert "🔄 验证中" in text
    assert "正在验证" not in text


def test_verification_section_renders_current_vp_line():
    elements = _verification_sections(
        _vp_progress({
            "id": "VP-023",
            "title": "Nightly CI 全过 (docker compose up + pytest)",
            "started_at": "2026-09-14T17:23:28",
        }),
        False,
    )
    text = _line_text(elements)
    assert "🔄 正在验证" in text
    assert "[VP-023]" in text
    assert "Nightly CI 全过" in text
    assert "2026-09-14T17:23:28" in text


def test_verification_section_omits_vp_line_when_none_running():
    elements = _verification_sections(_vp_progress(None), False)
    text = _line_text(elements)
    assert "🔄 正在验证" not in text
