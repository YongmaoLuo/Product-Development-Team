"""Regression test for the 2026-09-09 unified-phase card header.

The Feishu card previously had two parallel "where am I" sections:

  * ``📋任务执行状态`` — counts execution tasks
  * ``验证状态:🔄 验证中 (round 1/3)`` — describes verification loop

The execution and verification phases read as two separate things, but
for *status* they are one. The fix introduces a
single ``📍 当前状态`` line at the top of both the progress and verification
cards that talks about the top-level worker (executing vs verification_*) and
the inner sub-state (running / repairing / rerunning / passed / failed /
loop_stopped) in one place.

These tests pin:

  * The unified header line is present and uses ``📍 当前状态`` as the marker.
  * The executing window reports the execution sub-phase (``正在跑 tasks``).
  * The verification window reports the verification sub-stage and the
    ``round N/M`` denominator (when ``max_rounds`` is known).
  * The "正在执行(N 个并行)" line is shown ONLY when there are
    ``in_progress`` tasks (silent when there are none, so quiet
    phases don't carry redundant info).
  * The unified section is shared between ``build_progress_card`` and
    ``build_verification_card`` (the same helper, not two duplicates).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from notifications.cards import (
    _in_progress_tasks_section,
    _unified_phase_section,
    build_progress_card,
    build_verification_card,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _progress(verification_status: str = "running") -> Dict[str, Any]:
    return {
        "verification_status": verification_status,
        "verification_round": 2,
        "max_rounds": 3,
        "vps": [],
        "completed_vps": [],
        "failed_vps": [],
        "skipped_vps": [],
        "current_vp": None,
        "layer_summaries": {},
        "counts": {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
    }


def _flatten_card_text(card: Dict[str, Any]) -> str:
    """Concatenate every ``lark_md`` content for substring searches."""
    out: list[str] = []
    for e in card.get("elements", []):
        if e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            if text:
                out.append(text)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# _unified_phase_section — pure helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage,execution_current_phase,verification_status,expect_substring",
    [
        # Executing window — inner sub-state reported.
        ("executing", "executing", "pending", "▶️ 执行中"),
        ("executing", "executing", "pending", "正在跑 tasks"),
        # Verification sub-stages — each maps to its own label.
        ("verification_running", "completed", "running", "🔍 初次验证"),
        ("verification_repairing", "completed", "running", "🔧 正在生成修复任务"),
        ("verification_rerunning", "completed", "running", "🔁 重跑验证"),
        # Terminal verification states.
        ("verification_passed", "completed", "passed", "✅ 验证通过"),
        ("verification_failed", "completed", "failed", "❌ 验证失败"),
        ("verification_loop_stopped", "completed", "loop_stopped", "⏹ 循环停止"),
    ],
)
def test_unified_phase_section_renders_label_and_substate(
    stage, execution_current_phase, verification_status, expect_substring,
):
    """For every (stage, sub-state) combo, the unified line includes the
    expected label so the user sees "where am I" in one glance.
    """
    out = _unified_phase_section(
        stage,
        verification_status=verification_status,
        verification_round=2,
        verification_max_rounds=3,
    )
    assert out, f"empty elements for {stage!r}"
    content = out[0]["text"]["content"]
    assert "📍 当前状态" in content, content
    assert expect_substring in content, (
        f"expected {expect_substring!r} in unified line, got {content!r}"
    )


def test_unified_phase_section_round_max_denominator():
    """Verification line shows ``round N/M`` when both are known — fixes
    the gap where the legacy "验证状态:" line hard-coded ``/3``.
    """
    out = _unified_phase_section(
        "verification_running",
        verification_status="running",
        verification_round=2,
        verification_max_rounds=5,
    )
    content = out[0]["text"]["content"]
    assert "round 2/5" in content, content


def test_unified_phase_section_omits_round_when_unknown():
    """When ``max_rounds`` is unknown, don't fabricate "round N/0" — show
    only the round number with the status label.
    """
    out = _unified_phase_section(
        "verification_running",
        verification_status="running",
        verification_round=2,
        verification_max_rounds=None,
    )
    content = out[0]["text"]["content"]
    assert "round 2/" not in content, content
    assert "🔄 验证中" in content


def test_unified_phase_section_max_rounds_reached_surfaces_cap_message():
    """When the orchestrator flagged ``stop_reason='max_rounds_reached'``
    but ``verification_status`` is still ``running`` (loop suspended at
    the round cap), the unified line MUST show "round 上限, 等待
    /reset_rounds" so the operator does not mistake the suspended state
    for a still-running round.
    """
    out = _unified_phase_section(
        "verification_running",
        verification_status="running",
        verification_round=3,
        verification_max_rounds=3,
        verification_stop_reason="max_rounds_reached",
    )
    content = out[0]["text"]["content"]
    assert "round 3/3" in content, content
    assert "⏹ round 上限" in content, content
    # The legacy "🔄 验证中" sub-label is suppressed because the
    # stop_reason override takes precedence.
    assert "🔄 验证中" not in content, (
        "stop_reason=max_rounds_reached must take precedence over the "
        "generic running status label — the loop is suspended, not active"
    )


def test_unified_phase_section_empty_inputs():
    """Brand-new plans (no stage, no execution phase) don't render a
    placeholder that would mislead the operator.
    """
    assert _unified_phase_section(None, None, None) == []


# ---------------------------------------------------------------------------
# _in_progress_tasks_section — minimal "what is running" line
# ---------------------------------------------------------------------------


def test_in_progress_tasks_section_lists_count_and_titles():
    """2026-09-11 plan v14 (regression fix): the previous format was
    "正在执行：`t-1`、`t-2`" (ids only). The heading was unreadable
    without titles — operators recognised the title, not the id.
    New format: heading shows count, body lines show
    `[id] title` per task (or bare id when title missing for orphan
    RP-* tasks). Updated assertions:
    """
    tp = [
        {"id": "t-1", "status": "in_progress", "title": "A"},
        {"id": "t-2", "status": "in_progress", "title": "B"},
        {"id": "t-3", "status": "completed", "title": "C"},
    ]
    out = _in_progress_tasks_section(tp)
    assert out, "expected a section when in_progress tasks exist"
    # Body content is split across multiple lines now (heading + body
    # list). Find the div element that has the per-task list.
    body_chunks = [el["text"]["content"] for el in out if el.get("tag") == "div"]
    combined = "\n".join(body_chunks)
    assert "2 个并行" in combined  # exactly 2 in_progress
    assert "3 个并行" not in combined
    assert "[t-1]" in combined and "A" in combined  # id + title rendered
    assert "[t-2]" in combined and "B" in combined
    assert "[t-3]" not in combined  # completed tasks must not show


def test_in_progress_tasks_section_falls_back_to_id_when_title_missing():
    """RP-* / db_orphan tasks without a ``title`` field still render."""
    tp = [
        {"id": "RP-1", "status": "in_progress"},  # no title
    ]
    out = _in_progress_tasks_section(tp)
    assert out, "section must render even without title"
    body_chunks = [el["text"]["content"] for el in out if el.get("tag") == "div"]
    combined = "\n".join(body_chunks)
    assert "RP-1" in combined


def test_in_progress_tasks_section_silent_when_empty():
    """Per user: don't show "currently executing" when nothing is running —
    quiet phases should stay quiet.
    """
    assert _in_progress_tasks_section([]) == []
    assert _in_progress_tasks_section(None) == []
    assert _in_progress_tasks_section(
        [{"id": "t-1", "status": "completed", "title": "A"}],
    ) == []


# ---------------------------------------------------------------------------
# build_progress_card — unified header on the execution card
# ---------------------------------------------------------------------------


def test_progress_card_has_unified_header_in_executing_stage():
    summary = {
        "state": {
            "stage": "executing",
            "current_phase": "executing",
            "verification": {"status": "pending", "round": 0, "max_rounds": 3},
        },
        "tasks": {"total": 5, "completed": 2, "failed": 0, "in_progress": 1, "pending": 2, "skipped": 0},
        "execution": {"status": "running"},
    }
    progress = {
        "tasks": [{"id": "t-1", "status": "in_progress", "title": "T1"}],
    }
    card = build_progress_card("plan-exec", summary, progress=progress)
    text = _flatten_card_text(card)
    assert "📍 当前状态" in text, text
    assert "▶️ 执行中" in text
    # 2026-09-14: the unified line names the running task instead of
    # the bare "正在跑 tasks" label.
    assert "正在跑 `[t-1]` T1" in text
    assert "🔄" in text
    # 2026-09-11 plan v14 changed the in-progress block from an
    # id-only line ("🔄 正在执行：`t-1`") to a heading + one
    # `[id] title` line per task, because operators recognise the
    # title rather than the id. The v14 commit updated
    # ``test_in_progress_tasks_section_lists_count_and_titles`` but
    # not this card-level assertion — it was already failing on the
    # missing unified banner, so the second stale layer went unseen.
    # Pin the current contract: heading + `[t-1]` title line.
    assert "当前任务" in text  # the in-progress heading
    assert "[t-1]" in text


def test_progress_card_silent_when_no_in_progress_tasks():
    """When execution has finished (62/62) and no tasks are running, do
    NOT render the "正在执行(N 个并行)" line — quiet phase stays quiet.
    """
    summary = {
        "state": {
            "stage": "executing",
            "current_phase": "completed",
            "verification": {"status": "pending", "round": 0, "max_rounds": 3},
        },
        "tasks": {"total": 62, "completed": 62, "failed": 0, "in_progress": 0, "pending": 0, "skipped": 0},
        "execution": {"status": "completed"},
    }
    card = build_progress_card("plan-exec", summary, progress=None)
    text = _flatten_card_text(card)
    assert "📍 当前状态" in text
    assert "正在执行" not in text


# ---------------------------------------------------------------------------
# build_verification_card — unified header on the verification card
# ---------------------------------------------------------------------------


def test_verification_card_unified_header_replaces_old_status_line():
    """The legacy "验证状态:🔄 验证中 (round 1/3)" line is gone — its
    information is folded into the unified header section. The change
    is a status unification, not an extra line.
    """
    card = build_verification_card(
        "plan",
        _progress("running"),
        tasks_info={"total": 10, "completed": 8, "failed": 0, "in_progress": 2, "pending": 0, "skipped": 0},
        task_progress=[
            {"id": "v-1", "status": "in_progress", "title": "VP1"},
        ],
        current_phase="verification_running",
        max_rounds_override=3,
    )
    text = _flatten_card_text(card)
    assert "📍 当前状态" in text, text
    assert "🔍 初次验证" in text
    assert "round 2/3" in text
    # The old redundant line is gone — the unified section replaces it.
    assert "**验证状态：**" not in text, (
        "verification card should not have both the old '验证状态:' line "
        "and the new unified section — they overlap"
    )


def test_verification_card_passes_max_rounds_override_through():
    """When the verification progress endpoint doesn't carry
    ``max_rounds``, the notifier fetches it from
    ``summary.state.verification.max_rounds`` and passes it via
    ``max_rounds_override`` — the card must surface it.
    """
    progress = _progress("running")
    progress.pop("max_rounds", None)  # simulate the missing-field case
    card = build_verification_card(
        "plan",
        progress,
        tasks_info={"total": 10, "completed": 8, "failed": 0, "in_progress": 2, "pending": 0, "skipped": 0},
        task_progress=None,
        current_phase="verification_repairing",
        max_rounds_override=5,  # passed in by the notifier
    )
    text = _flatten_card_text(card)
    assert "round 2/5" in text, text  # NOT 2/3 (the hard-coded fallback)
    assert "🔧 正在生成修复任务" in text


def test_verification_card_silent_on_in_progress_tasks_when_terminal():
    """When verification_status is terminal (passed / failed /
    loop_stopped), the in-progress task section still renders for any
    repair tasks the executor is currently running — but ONLY if there
    are in-progress tasks. Empty / passed plan: silent.
    """
    card = build_verification_card(
        "plan",
        _progress("passed"),
        tasks_info={"total": 10, "completed": 10, "failed": 0, "in_progress": 0, "pending": 0, "skipped": 0},
        task_progress=None,
        current_phase="verification_passed",
        max_rounds_override=3,
    )
    text = _flatten_card_text(card)
    assert "📍 当前状态" in text
    assert "✅ 验证通过" in text
    assert "正在执行" not in text


def test_verification_card_max_rounds_reached_flips_header_to_capped_loop():
    """When ``verification_status`` is still ``running`` but
    ``stop_reason='max_rounds_reached'`` (loop suspended at the
    round cap), the header must flip from the default "🔄 验证中"
    to "⏹ 循环停止(round 已达上限)" + red colour. Without this
    the user sees the loop appearing to run forever while the
    watchdog waits for ``/reset_rounds``.
    """
    progress = _progress("running")
    progress["stop_reason"] = "max_rounds_reached"
    card = build_verification_card(
        "plan",
        progress,
        tasks_info={"total": 10, "completed": 9, "failed": 0, "in_progress": 0, "pending": 0, "skipped": 0},
        task_progress=None,
        current_phase="verification_running",
        max_rounds_override=3,
        stop_reason_override="max_rounds_reached",
    )
    # Header flips
    assert "⏹ 循环停止" in card["header"]["title"]["content"]
    assert "上限" in card["header"]["title"]["content"]
    assert card["header"]["template"] == "red", (
        f"capped-loop header must be red, got {card['header']['template']!r}"
    )
    text = _flatten_card_text(card)
    # Unified line carries the cap message too.
    assert "⏹ round 上限" in text, text


# ---------------------------------------------------------------------------
# Cross-card contract: same helper, same marker
# ---------------------------------------------------------------------------


def test_unified_header_is_first_element_in_both_cards():
    """The card should answer "where am I" before anything else. The
    unified header is the FIRST body element of both cards (right
    after the colored header banner).
    """
    summary = {
        "state": {
            "stage": "executing",
            "current_phase": "executing",
            "verification": {"status": "pending", "round": 0, "max_rounds": 3},
        },
        "tasks": {"total": 5, "completed": 2, "failed": 0, "in_progress": 1, "pending": 2, "skipped": 0},
        "execution": {"status": "running"},
    }
    progress = {
        "tasks": [{"id": "t-1", "status": "in_progress", "title": "T1"}],
    }
    prog_card = build_progress_card("plan", summary, progress=progress)
    verif_card = build_verification_card(
        "plan", _progress("running"),
        tasks_info=summary["tasks"],
        task_progress=progress["tasks"],
        current_phase="verification_running",
        max_rounds_override=3,
    )
    # The first non-empty div content must be the unified header.
    for name, card in (("progress", prog_card), ("verification", verif_card)):
        first = card["elements"][0]
        content = first.get("text", {}).get("content", "")
        assert "📍 当前状态" in content, (
            f"{name} card: first element should be the unified header, "
            f"got {content!r}"
        )


def test_verification_card_section_order_is_unified_then_tasks_then_verification():
    """Three ordered groups: 1. 📍 当前状态, 2. 📋 任务执行状态,
    3. 🔍 verification 状态. The verification sub-sections
    (总进度, 层级进度, 当前 VP, 最近 VP, 失败 VP) MUST come after
    the "🔍 verification 状态" banner — otherwise the operator
    sees the verification sub-machine activity without an
    identifying banner.
    """
    card = build_verification_card(
        "plan", _progress("running"),
        tasks_info={"total": 5, "completed": 3, "failed": 0, "in_progress": 1, "pending": 1, "skipped": 0},
        task_progress=[{"id": "v-1", "status": "in_progress", "title": "VP1"}],
        current_phase="verification_running",
        max_rounds_override=3,
    )
    # Extract every div content in order.
    divs = [
        e.get("text", {}).get("content", "")
        for e in card["elements"]
        if e.get("tag") == "div"
    ]
    # 1. 📍 当前状态 must appear before 2. 📋 任务执行状态.
    i_unified = next(
        (i for i, d in enumerate(divs) if "📍 当前状态" in d), None,
    )
    i_tasks = next(
        (i for i, d in enumerate(divs) if "📋 任务执行状态" in d), None,
    )
    i_verif = next(
        (i for i, d in enumerate(divs) if "🔍 verification 状态" in d), None,
    )
    assert i_unified is not None and i_tasks is not None and i_verif is not None, (
        f"missing one of the 3 banners: {divs!r}"
    )
    assert i_unified < i_tasks < i_verif, (
        f"section order broken: unified={i_unified} tasks={i_tasks} "
        f"verif={i_verif}; divs={divs!r}"
    )


def test_progress_and_verification_cards_share_unified_helper():
    """Both cards route through ``_unified_phase_section`` so the
    operator reads the same line in either view. This test pins that
    by rendering the same (stage, sub-state) input through both cards
    and asserting the unified line shows up in both.
    """
    summary = {
        "state": {
            "stage": "verification_running",
            "current_phase": "verification_running",
            "verification": {"status": "running", "round": 1, "max_rounds": 3},
        },
        "tasks": {"total": 5, "completed": 3, "failed": 0, "in_progress": 1, "pending": 1, "skipped": 0},
        "execution": {"status": "completed"},
    }
    progress = {
        "tasks": [{"id": "v-1", "status": "in_progress", "title": "VP1"}],
    }
    prog_card = build_progress_card("plan", summary, progress=progress)
    verif_card = build_verification_card(
        "plan", _progress("running"),
        tasks_info=summary["tasks"],
        task_progress=progress["tasks"],
        current_phase="verification_running",
        max_rounds_override=3,
    )
    prog_text = _flatten_card_text(prog_card)
    verif_text = _flatten_card_text(verif_card)
    assert "📍 当前状态" in prog_text
    assert "📍 当前状态" in verif_text
    # Same stage label appears in both.
    assert "🔍 初次验证" in prog_text
    assert "🔍 初次验证" in verif_text