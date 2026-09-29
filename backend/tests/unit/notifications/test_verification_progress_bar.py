"""Test the verification progress bar appears in terminal cards (2026-09-11 plan v7).

The card carried no verification progress bar, so the reader could not
tell how many VPs there were, how many had completed, or how many were
still outstanding. That information belongs on the card.

Test pins that ``build_card`` renders the overall VP progress bar
("📈 总进度：███████░ 97% (41/42) ✅41 ❌1") for BOTH running and
terminal verification plans — the bar is the operator's at-a-glance
view of how many VPs have completed vs how many remain.

Previously the bar was gated behind ``if not is_terminal`` so terminal
plans only rendered the "📈 总结：..." text line without the bar,
forcing operators to mentally parse "通过 41 失败 1 跳过 0 总计 42".
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from notifications.cards import build_card
from status_payload import status_from


def _summary() -> Dict[str, Any]:
    """Plan summary that matches a terminal plan
    (current_phase=completed, total>0)."""
    return {
        "plan_id": "test-plan",
        "state": {
            "current_phase": "completed",
            "stage": "completed",
            "verification": {
                "status": "passed",
                "round": 1,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {
            "total": 79,
            "completed": 77,
            "failed": 2,
            "pending": 0,
            "in_progress": 0,
            "skipped": 0,
        },
        "execution": {"status": "completed"},
    }


def _verification_progress_terminal() -> Dict[str, Any]:
    """Verification progress payload for a terminal plan (some VPs
    passed, one failed, the totals agree)."""
    return {
        "plan_id": "test-plan",
        "verification_status": "passed",
        "verification_round": 1,
        "max_rounds": 3,
        "stop_reason": None,
        "current_vp": None,
        "completed_vps": ["VP-041", "VP-042", "VP-040"],
        "failed_vps": ["VP-034"],
        "skipped_vps": [],
        "pending_vps": [],
        "vps": [],
        "current_layer": None,
        "layer_summaries": {},
        "counts": {
            "completed": 41, "failed": 1, "skipped": 0,
            "in_progress": 0, "pending": 0, "total": 42,
        },
        "last_updated_at": "2026-09-11T00:00:00Z",
    }


def _verification_progress_running() -> Dict[str, Any]:
    """Verification progress payload for a mid-run plan (some VPs
    in flight)."""
    return {
        "plan_id": "test-plan",
        "verification_status": "running",
        "verification_round": 2,
        "max_rounds": 5,
        "stop_reason": None,
        "current_vp": None,
        "completed_vps": ["VP-010", "VP-011"],
        "failed_vps": [],
        "skipped_vps": [],
        "pending_vps": ["VP-015", "VP-016", "VP-017"],
        "vps": [],
        "current_layer": None,
        "layer_summaries": {},
        "counts": {
            "completed": 5, "failed": 0, "skipped": 0,
            "in_progress": 3, "pending": 12, "total": 20,
        },
        "last_updated_at": "2026-09-11T00:00:00Z",
    }


def _find_verif_section_text(card: Dict[str, Any]) -> str:
    """Return the text content of the verification section, joined
    across all div elements between the "🔍 verification 状态" header
    and the next separator. Used to assert both bar + summary line
    appear together.
    """
    body = card.get("elements", [])
    chunks: list[str] = []
    in_section = False
    for e in body:
        if not isinstance(e, dict):
            continue
        text = e.get("text", {}).get("content", "") if isinstance(e.get("text"), dict) else ""
        if "🔍 verification" in text:
            in_section = True
            continue
        if in_section:
            if text.startswith("🕐"):
                break
            if text:
                chunks.append(text)
    return "\n".join(chunks)


# --- 1. terminal plan: bar at top + no redundant summary line ---


def test_terminal_verification_renders_progress_bar() -> None:
    """Terminal verification cards MUST show the overall VP progress
    bar (NOT just the "📈 总结" text line). This is the 2026-09-11
    v7 regression case — operators could not see at a glance how
    much verification had completed.

    2026-09-11 v8 update: the bar is rendered at the TOP of the
    verification section (just under "🔍 verification 状态"), and
    the redundant "📈 总结" line is dropped because the bar already
    shows counts in the non-zero suffix.
    """
    _summary_payload = _summary()
    _verification_payload = _verification_progress_terminal()
    card = build_card(
        "test-plan",
        status_from(_summary_payload, verification=_verification_payload,
                    plan_id="test-plan"),
        _summary_payload,
        execution_progress={"tasks": []},
        verification_progress=_verification_payload,
    )
    section_text = _find_verif_section_text(card)
    assert "📈 总进度" in section_text, (
        f"terminal verification section must show '📈 总进度' bar; "
        f"got section: {section_text!r}"
    )
    # Bar content for 41/42 = 97% — operator sees progress + counts
    assert "97%" in section_text, (
        f"bar must show 97% pct for 41/42 VPs completed; "
        f"got section: {section_text!r}"
    )
    assert "(41/42)" in section_text, (
        f"bar must show (41/42) ratio; got section: {section_text!r}"
    )
    # Non-zero counts visible
    assert "✅41" in section_text, (
        f"bar must show ✅41 count; got section: {section_text!r}"
    )
    assert "❌1" in section_text, (
        f"bar must show ❌1 count; got section: {section_text!r}"
    )
    # Trailing zero counts must be suppressed (no ⏭0, no 📋0)
    assert "⏭0" not in section_text, (
        f"bar must suppress trailing zero counts; got: {section_text!r}"
    )
    assert "📋0" not in section_text, (
        f"bar must suppress trailing zero counts; got: {section_text!r}"
    )
    # 2026-09-11 v8: redundant "📈 总结" line dropped — bar carries
    # the same info.
    assert "📈 总结" not in section_text, (
        f"terminal plan must NOT show '📈 总结' line (redundant with "
        f"bar counts); got section: {section_text!r}"
    )


def test_terminal_verification_bar_is_above_vp_sections() -> None:
    """2026-09-11 v8: bar sits at the TOP of the verification section,
    directly under "🔍 verification 状态". Mirrors the execution
    layout where the task summary bar sits at the top of the
    execution section. Operator reads top-to-bottom: section
    header → progress bar → detail sections.
    """
    _summary_payload = _summary()
    _verification_payload = _verification_progress_terminal()
    card = build_card(
        "test-plan",
        status_from(_summary_payload, verification=_verification_payload,
                    plan_id="test-plan"),
        _summary_payload,
        execution_progress={"tasks": []},
        verification_progress=_verification_payload,
    )
    body = card.get("elements", [])
    # Find indexes of verification header, progress bar, recent VPs
    verif_header_idx = bar_idx = recent_vp_idx = None
    for i, e in enumerate(body):
        if not isinstance(e, dict):
            continue
        text = e.get("text", {}).get("content", "") if isinstance(e.get("text"), dict) else ""
        if "🔍 verification" in text:
            verif_header_idx = i
        elif "📈 总进度" in text and verif_header_idx is not None:
            bar_idx = i
        elif "最近完成的 VP" in text and verif_header_idx is not None:
            recent_vp_idx = i
            break
    assert verif_header_idx is not None and bar_idx is not None and recent_vp_idx is not None, (
        f"missing required elements; got verif_header={verif_header_idx}, "
        f"bar={bar_idx}, recent_vp={recent_vp_idx}"
    )
    assert verif_header_idx < bar_idx < recent_vp_idx, (
        f"bar must sit between section header and recent VPs; "
        f"got header={verif_header_idx}, bar={bar_idx}, recent_vp={recent_vp_idx}"
    )


# --- 2. running plan: bar still appears (regression of original behavior) ---


def test_running_verification_renders_progress_bar() -> None:
    """Mid-run verification still shows the bar — same behavior as
    before v7, but pinned here so we don't break it.
    """
    _summary_payload = _summary()
    _verification_payload = _verification_progress_running()
    card = build_card(
        "test-plan",
        status_from(_summary_payload, verification=_verification_payload,
                    plan_id="test-plan"),
        _summary_payload,
        execution_progress={"tasks": []},
        verification_progress=_verification_payload,
    )
    section_text = _find_verif_section_text(card)
    assert "📈 总进度" in section_text, (
        f"running verification section must show '📈 总进度' bar; "
        f"got section: {section_text!r}"
    )
    # 5 completed + 3 in_progress + 12 pending = 20 total
    # 5 done out of 20 = 25%
    assert "25%" in section_text, (
        f"bar must show 25% for 5/20 VPs done; "
        f"got section: {section_text!r}"
    )
    assert "✅5" in section_text and "⏳3" in section_text and "📋12" in section_text, (
        f"bar must show running counts (✅5 ⏳3 📋12); "
        f"got section: {section_text!r}"
    )
    # No terminal summary line during running
    assert "📈 总结" not in section_text, (
        f"running verification must NOT show '📈 总结' (terminal-only); "
        f"got section: {section_text!r}"
    )
