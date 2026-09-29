"""Regression test for build_verification_card intermediate-state headers.

The card must distinguish verification_repairing / verification_rerunning /
verify_first_pass from generic "🔄 验证中".

Each test exercises one header branch by passing ``current_phase`` (and a
matching ``progress`` dict so the function reaches its header decision
without short-circuiting on missing keys).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from notifications.cards import build_verification_card


def _progress(verification_status: str = "running") -> Dict[str, Any]:
    """Minimal progress dict that lets the header logic run."""
    return {
        "verification_status": verification_status,
        "verification_round": 2,
        "total_vps": 10,
        "passed": 5,
        "failed": 1,
        "in_progress": 4,
        "results": [],
    }


@pytest.mark.parametrize(
    "current_phase,expected_header_substr",
    [
        ("verification_repairing", "🔧 正在生成修复任务"),
        ("verification_rerunning", "🔁 重跑验证"),
        ("verify_first_pass", "🔍 初次验证"),
        # Unknown / None phase falls back to the generic "验证中" header.
        (None, "🔄 验证中"),
        ("verification_running", "🔄 验证中"),
    ],
)
def test_build_verification_card_intermediate_phase_headers(
    current_phase: Optional[str], expected_header_substr: str
) -> None:
    """When current_phase is one of the sub-phases, the header shows the
    sub-phase label instead of the generic "验证中" string."""
    card = build_verification_card(
        plan_id="20260101-test",
        progress=_progress(),
        current_phase=current_phase,
    )

    # Feishu card JSON walks: header.title is at top level
    header = card.get("header") or {}
    title = header.get("title") or {}
    title_text = title.get("content", "") if isinstance(title, dict) else str(title)

    assert expected_header_substr in title_text, (
        f"expected header to contain {expected_header_substr!r}, "
        f"got {title_text!r} (current_phase={current_phase!r})"
    )


def test_build_verification_card_terminal_status_wins_over_phase() -> None:
    """When verification_status is terminal (failed/passed) the header
    must reflect that, even if current_phase is e.g. verification_repairing
    (which can briefly co-exist while the orchestrator is winding down)."""
    card_failed = build_verification_card(
        plan_id="20260101-test",
        progress=_progress(verification_status="failed"),
        current_phase="verification_repairing",
    )
    header_failed = card_failed.get("header", {}).get("title", {}).get("content", "")
    assert "❌ 验证失败" in header_failed
    assert "正在生成修复任务" not in header_failed

    card_passed = build_verification_card(
        plan_id="20260101-test",
        progress=_progress(verification_status="passed"),
        current_phase="verification_rerunning",
    )
    header_passed = card_passed.get("header", {}).get("title", {}).get("content", "")
    assert "✅ 验证通过" in header_passed
    assert "重跑验证" not in header_passed


def test_build_verification_card_backward_compatible_without_current_phase() -> None:
    """Omitting current_phase (existing callers) keeps old default header."""
    card = build_verification_card(
        plan_id="20260101-test",
        progress=_progress(),
    )
    header = card.get("header", {}).get("title", {}).get("content", "")
    assert "🔄 验证中" in header