"""
Tests for the 2026-09-11 plan v10 card fixes.

Background (4 explicit asks):
  1. "最近活动" → "最近完成活动" + exclude ``in_progress`` so the
     same task never appears in both "当前任务" and "最近完成活动".
  2. Card layout: dedup the trailing ``<hr>`` between the 任务执行
     状态 bar and the 当前任务 section (was two consecutive ``<hr>``s).
  3. Header follows current phase (not stale ``verification_status``
     cached from a prior verification round) — when execution is
     in flight, fall through to the execution / mid-plan branch
     even if verification_status="passed" was cached.
  4. Same dedup rationale for "❌ 验证失败", "⚠️ 部分通过", "⏹ 循环停止"
     branches — only honor when execution is NOT in flight.

These tests pin the contracts at the unit level by calling the
helpers directly with crafted inputs. The contract surface is
small enough that pulling in the full ``build_card`` plumbing is
overkill — we test the helpers in isolation.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from notifications.cards import _resolve_header as _header_from_status


def _resolve_header(
    plan_id, state, tasks_info, *, execution_in_flight, **over,
):
    """Test-local adapter: the pre-2026-09-23 call shape over a real
    ``PlanStatus``.

    ``execution_in_flight`` is a **required** keyword on purpose. The
    header used to guess it from the task counts, and the reason it now
    takes a snapshot is that guessing is exactly what went wrong — a
    suite that means "an execution is running" has to say so.
    """
    from plan_status import PlanStatus

    return _header_from_status(PlanStatus(
        plan_id=plan_id,
        phase=(state or {}).get("current_phase") or "",
        verification_status=over.pop("verification_status", "") or "",
        verification_round=over.pop("verification_round", 0) or 0,
        verification_max_rounds=over.pop("max_rounds", 0) or 0,
        verification_stop_reason=over.pop("stop_reason", None),
        execution_in_flight=bool(execution_in_flight),
        verification_in_flight=False,
        tasks=dict(tasks_info or {}),
    ), dict(tasks_info or {}))


# ---------------------------------------------------------------------------
# Fix #1 + #2 — _execution_sections: "最近完成活动" + exclude in_progress
# ---------------------------------------------------------------------------


def _task(title: str = "T", status: str = "completed", end_ts: str = "2026-09-11T01:00:00Z") -> dict:
    """Build a fake runtime-overlay task dict."""
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "status": status,
        "end_ts": end_ts,
    }


def test_execution_sections_renames_recent_activity_to_completed():
    """"最近活动" → "最近完成活动" string label appears in the card."""
    from notifications.cards import _execution_sections

    tasks_info = {"total": 3, "completed": 1, "in_progress": 0, "pending": 2, "failed": 0}
    progress = {
        "tasks": [
            _task("done", status="completed"),
        ]
    }
    elements = _execution_sections(tasks_info, progress)
    # find the "最近完成活动" div
    found_completed_label = False
    found_old_label = False
    for e in elements:
        if isinstance(e, dict) and e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            if "最近完成活动" in text:
                found_completed_label = True
            if "最近活动：" in text and "最近完成" not in text:
                found_old_label = True
    assert found_completed_label, "card must include '最近完成活动' label"
    assert not found_old_label, "card must NOT include the old '最近活动' label"


def test_execution_sections_excludes_in_progress_from_completed_list():
    """in_progress tasks must NOT appear in the 最近完成活动 list."""
    from notifications.cards import _execution_sections

    tasks_info = {"total": 3, "completed": 1, "in_progress": 1, "pending": 1, "failed": 0}
    progress = {
        "tasks": [
            _task("done-1", status="completed", end_ts="2026-09-11T01:00:00Z"),
            # in_progress with end_ts set (defensive — overlay should not
            # do this, but if it does we still must exclude it)
            _task("currently-running", status="in_progress", end_ts="2026-09-11T02:00:00Z"),
            _task("done-2", status="completed", end_ts="2026-09-11T00:30:00Z"),
        ]
    }
    elements = _execution_sections(tasks_info, progress)
    for e in elements:
        if isinstance(e, dict) and e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            if "最近完成活动" in text:
                # The in_progress task must NOT appear under this label.
                assert "currently-running" not in text, (
                    f"in_progress task leaked into 最近完成活动: {text!r}"
                )
                # Both completed tasks should be present.
                assert "done-1" in text
                assert "done-2" in text


def test_execution_sections_no_double_hr_before_current_task():
    """Trailing <hr> from 任务执行状态 must not be doubled before 当前任务."""
    from notifications.cards import _execution_sections

    tasks_info = {"total": 2, "completed": 0, "in_progress": 1, "pending": 1, "failed": 0}
    progress = {
        "tasks": [
            _task("running", status="in_progress", end_ts=None),
        ]
    }
    elements = _execution_sections(tasks_info, progress)
    # find the index of 任务执行状态 div and 当前任务 div, ensure no two
    # consecutive <hr> between them
    summary_idx = None
    current_idx = None
    for i, e in enumerate(elements):
        if isinstance(e, dict) and e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            if "任务执行状态" in text and summary_idx is None:
                summary_idx = i
            if "当前任务" in text and current_idx is None:
                current_idx = i
    assert summary_idx is not None
    assert current_idx is not None
    # slice elements[summary_idx+1 .. current_idx] and count <hr>
    between = elements[summary_idx + 1 : current_idx]
    hr_count = sum(1 for e in between if isinstance(e, dict) and e.get("tag") == "hr")
    assert hr_count <= 1, (
        f"expected ≤1 hr between 任务执行状态 and 当前任务, got {hr_count}: {between!r}"
    )


# ---------------------------------------------------------------------------
# Fix #3 + #4 — _resolve_header: skip verification verdict during execution
# ---------------------------------------------------------------------------


def test_header_and_body_read_one_task_snapshot():
    """A card must not report two different totals.

    The header read ``status.tasks`` while the body's counts came from
    the summary, falling back to the status. On a plan where the two
    disagreed — the header's source counted fewer tasks than the summary,
    because some of them had never been scheduled at all — the card
    rendered a green
    "⚠️ 已完成（1 个失败）" above a body that still owed two tasks and had
    never run verification. Someone reading the top line concluded the
    plan was done.
    """
    from notifications.cards import build_card
    from plan_status import PlanStatus

    status = PlanStatus(
        plan_id="p1",
        phase="failed",
        verification_status="",
        verification_round=0,
        verification_max_rounds=0,
        verification_stop_reason=None,
        execution_in_flight=False,
        verification_in_flight=False,
        tasks={"total": 20, "completed": 19, "failed": 1,
               "in_progress": 0, "pending": 0, "skipped": 0},
    )
    card = build_card("p1", status, summary={"tasks": {
        "total": 22, "completed": 19, "failed": 1,
        "in_progress": 0, "pending": 2, "skipped": 0,
    }})

    title = card["header"]["title"]["content"]
    body = " ".join(
        str(el.get("text", {}).get("content", "")) for el in card["elements"]
    )

    assert "(19/22)" in body, f"body lost the per-task total: {body[:240]!r}"
    assert "已完成" not in title, (
        f"the header claims completion while the body reports (19/22) and "
        f"two tasks were never scheduled — the two are reading different "
        f"totals again: {title!r}"
    )
def test_resolve_header_falls_through_when_execution_in_flight_passed():
    """verification_status='passed' but execution is in flight → NOT '✅ 验证通过'."""
    from notifications.cards import _resolve_header as _header_from_status


    plan_id = "test-plan"
    state = {"current_phase": "executing", "stage": "executing"}
    tasks_info = {
        "total": 5, "completed": 2, "in_progress": 1, "pending": 2,
        "failed": 0, "skipped": 0,
    }
    # verification_status='passed' is cached from a prior round.
    result = _resolve_header(
        plan_id, state, tasks_info,
        verification_status="passed",
        execution_in_flight=True,
    )
    assert "验证通过" not in result["title"], (
        f"header must not say '验证通过' when execution is in flight, "
        f"got: {result!r}"
    )
    # Should be one of the execution / mid-plan labels.
    assert "执行中" in result["title"] or "暂停" in result["title"]


def test_resolve_header_honors_verified_when_no_in_progress():
    """verification_status='passed' AND no in_progress / pending → '✅ 验证通过'."""

    plan_id = "test-plan"
    state = {"current_phase": "completed", "stage": "completed"}
    tasks_info = {
        "total": 5, "completed": 5, "in_progress": 0, "pending": 0,
        "failed": 0, "skipped": 0,
    }
    result = _resolve_header(
        plan_id, state, tasks_info,
        verification_status="passed",
        execution_in_flight=False,
    )
    assert "验证通过" in result["title"], (
        f"header must say '验证通过' for completed plan with passed "
        f"verification, got: {result!r}"
    )


def test_resolve_header_skips_verification_failed_during_execution():
    """verification_status='failed' but execution restarted → NOT '❌ 验证失败'."""

    plan_id = "test-plan"
    state = {"current_phase": "executing", "stage": "executing"}
    tasks_info = {
        "total": 5, "completed": 2, "in_progress": 1, "pending": 2,
        "failed": 0, "skipped": 0,
    }
    result = _resolve_header(
        plan_id, state, tasks_info,
        verification_status="failed",
        execution_in_flight=True,
    )
    assert "验证失败" not in result["title"], (
        f"header must not say '验证失败' when execution is in flight, "
        f"got: {result!r}"
    )


def test_resolve_header_pending_with_executing_phase_skips_verdict():
    """phase='executing' with pending > 0 (no in_progress yet) → mid-plan header."""

    plan_id = "test-plan"
    state = {"current_phase": "executing", "stage": "executing"}
    tasks_info = {
        "total": 5, "completed": 0, "in_progress": 0, "pending": 5,
        "failed": 0, "skipped": 0,
    }
    # Even though verification_status='passed' is cached, the
    # plan is freshly executing, so the header must not be '验证通过'.
    result = _resolve_header(
        plan_id, state, tasks_info,
        verification_status="passed",
        execution_in_flight=True,
    )
    assert "验证通过" not in result["title"], (
        f"header must not say '验证通过' when plan is in executing "
        f"phase with pending tasks, got: {result!r}"
    )


def test_resolve_header_ready_phase_with_pending_skips_verdict():
    """phase='ready' with pending tasks → mid-plan, not '验证通过'."""

    plan_id = "test-plan"
    state = {"current_phase": "ready", "stage": "ready"}
    tasks_info = {
        "total": 5, "completed": 0, "in_progress": 0, "pending": 5,
        "failed": 0, "skipped": 0,
    }
    result = _resolve_header(
        plan_id, state, tasks_info,
        verification_status="passed",
        execution_in_flight=True,
    )
    assert "验证通过" not in result["title"]


# ---------------------------------------------------------------------------
# stop_reason reaches the header (2026-09-19)
# ---------------------------------------------------------------------------


def test_max_rounds_reached_renders_the_specific_label():
    """``loop_stopped`` + ``max_rounds_reached`` → "⏹ 循环停止".

    The dedicated branch used to sit *below* the generic
    failed/loop_stopped one, where it was unreachable: a max-rounds stop
    always reports ``loop_stopped``, so "❌ 验证失败" always won. Only
    fixing the ``stop_reason`` plumbing (``/api/verification/{id}/progress``
    + the ``plan_routing.verification`` mirror) made this reachable at all.
    """

    result = _resolve_header(
        "test-plan",
        {"current_phase": "failed", "stage": "failed"},
        {"total": 5, "completed": 5, "in_progress": 0, "pending": 0,
         "failed": 0, "skipped": 0},
        verification_status="loop_stopped",
        stop_reason="max_rounds_reached",
        execution_in_flight=False,
    )
    assert "循环停止" in result["title"], (
        f"a max-rounds stop must render its own label, got {result!r}"
    )


def test_loop_stopped_without_a_reason_still_reads_as_a_failure():
    """No ``stop_reason`` → the generic terminal label, not a blank card."""

    result = _resolve_header(
        "test-plan",
        {"current_phase": "failed", "stage": "failed"},
        {"total": 5, "completed": 5, "in_progress": 0, "pending": 0,
         "failed": 0, "skipped": 0},
        verification_status="loop_stopped",
        execution_in_flight=False,
    )
    assert "验证失败" in result["title"], (
        f"loop_stopped without a reason must still render a terminal "
        f"verdict, got {result!r}"
    )


# ---------------------------------------------------------------------------
# A terminal phase must not outrank a live repair execution (2026-09-23)
#
# The four tests above fix the same class of bug one branch earlier: they
# stop a *verification verdict* from rendering while execution is in
# flight. The execution-terminal branch (step 4) had no such guard. On
# a production plan the phase was stamped ``completed`` the
# instant its repair round was dispatched, so step 4 ran, saw 9 pending
# tasks and no way to reach the repair-execution branch, and labelled a
# plan that was running ``repair-r1-01`` as "⏸ 暂停（上游阻塞）".
# ---------------------------------------------------------------------------


def test_a_terminal_phase_does_not_outrank_a_live_repair_execution():
    """The incident, with the numbers the operator's card carried."""

    # A realistic plan id: the system only ever produces
    # ``derive_plan_id`` output ([A-Za-z0-9._-]), and server.py rejects
    # anything else before it touches ``plans/{plan_id}``.
    plan_id = "20260101-example-plan"
    state = {"current_phase": "completed", "stage": "completed"}
    tasks_info = {
        "total": 33, "completed": 22, "failed": 1,
        "in_progress": 1, "pending": 9, "skipped": 0,
    }

    result = _resolve_header(
        plan_id, state, tasks_info,
        verification_status="failed", verification_round=1, max_rounds=4,
        execution_in_flight=True,
    )

    assert "暂停" not in result["title"], (
        "the card claimed the plan was paused while repair-r1-01 was "
        f"running: {result['title']!r}"
    )
    assert "修复执行中" in result["title"], result
    assert plan_id in result["title"], (
        "the label must keep the plan identity — operators watch several"
    )


def test_a_stalled_terminal_plan_still_reads_paused():
    """Anti-vacuity control: nothing in flight → the pause label stands.

    Without this, the guard could be "fixed" by deleting the branch.
    """

    plan_id = "plan-stalled"
    result = _resolve_header(
        plan_id,
        {"current_phase": "completed", "stage": "completed"},
        {"total": 33, "completed": 22, "failed": 1,
         "in_progress": 0, "pending": 10, "skipped": 0},
        execution_in_flight=False,
    )

    assert result["title"] == f"⏸ 暂停（上游阻塞） · {plan_id}", result


def test_a_plan_that_died_mid_task_still_reads_interrupted():
    """The label this guard must NOT swallow.

    Within the execution-terminal branch ``execution_in_flight``
    degenerates to ``in_progress > 0`` (``phase`` is already terminal, so
    the execution-capable clause cannot fire). Guarding the whole branch
    therefore deletes "⏸ 暂停（执行中断）" — which is the only way this
    card can say "the server died with a task still marked running". The
    first cut of the 2026-09-23 fix did exactly that; the parametrised
    table in ``notifications/test_card_identity_and_current_task.py``
    caught it.
    """

    plan_id = "plan-died-mid-task"
    result = _resolve_header(
        plan_id,
        {"current_phase": "failed", "stage": "failed"},
        {"total": 5, "completed": 2, "failed": 1,
         "in_progress": 2, "pending": 0, "skipped": 0},
        execution_in_flight=False,
    )

    assert result["title"] == f"⏸ 暂停（执行中断） · {plan_id}", result


def test_a_finished_plan_is_unaffected():
    """``finished >= total`` still wins — the guard sits below it."""

    plan_id = "plan-done"
    result = _resolve_header(
        plan_id,
        {"current_phase": "completed", "stage": "completed"},
        {"total": 5, "completed": 5, "failed": 0,
         "in_progress": 0, "pending": 0, "skipped": 0},
        execution_in_flight=False,
    )

    assert result["title"] == f"✅ 已完成 · {plan_id}", result


def test_the_repair_label_carries_the_round_budget():
    """Repair rounds are numbered; the header must show which one."""

    result = _resolve_header(
        "plan-round-2",
        {"current_phase": "completed", "stage": "completed"},
        {"total": 12, "completed": 8, "failed": 0,
         "in_progress": 1, "pending": 3, "skipped": 0},
        verification_round=2, max_rounds=4,
        execution_in_flight=True,
    )

    assert "round 2/4" in result["title"], result
