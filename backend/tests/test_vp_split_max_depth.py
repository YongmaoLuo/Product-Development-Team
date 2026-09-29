"""Regression tests for the 2026-09-08 VP split max-depth guard.

Background: VP-034 in plan 2026-09-04 was
recursively auto-split 8 levels deep on hard_timeout, spawning 471
VP attempts (113 at L7, 68 at L8). Each split = 1 Claude LLM call,
each leaf = 60-300s pytest timeout. The 1-hour outer cap does NOT
catch this — recursion happens at the 60-300s leaf pytest timeout.

2026-09-08: split depth is capped at 5.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. _split_depth parser
# ---------------------------------------------------------------------------


def test_split_depth_root_vp():
    """A root VP (no -L segments) has depth 0."""
    from verification_split_llm import _split_depth

    assert _split_depth("VP-034") == 0
    assert _split_depth("VP-1") == 0
    assert _split_depth("VP") == 0


def test_split_depth_first_split():
    """After 1 split, depth = 1."""
    from verification_split_llm import _split_depth

    assert _split_depth("VP-034-L1") == 1
    assert _split_depth("VP-034-L2") == 1
    assert _split_depth("VP-034-L3") == 1


def test_split_depth_nested_splits():
    """Each nested -L increments the depth."""
    from verification_split_llm import _split_depth

    assert _split_depth("VP-034-L1-L2") == 2
    assert _split_depth("VP-034-L1-L1-L1") == 3
    assert _split_depth("VP-034-L2-L2-L1-L4-L3-L4-L1") == 7  # the real runaway case


def test_split_depth_multi_digit_levels():
    """Multi-digit level numbers (L10, L11) count as one segment."""
    from verification_split_llm import _split_depth

    assert _split_depth("VP-034-L10") == 1
    assert _split_depth("VP-034-L10-L11-L12") == 3


def test_split_depth_ignores_l_inside_other_tokens():
    """Tokens like 'VP-L-test' don't count — only L<digit>+ segments."""
    from verification_split_llm import _split_depth

    assert _split_depth("VP-L-test") == 0  # 'L' alone, no digit
    assert _split_depth("VP-LLM-L1") == 1  # 'LLM' is not L<digit>


def test_split_depth_edge_cases():
    """Empty / non-string / None inputs → 0, never raise."""
    from verification_split_llm import _split_depth

    assert _split_depth("") == 0
    assert _split_depth(None) == 0  # type: ignore[arg-type]
    assert _split_depth(123) == 0  # type: ignore[arg-type]


def test_max_split_depth_is_5_per_user():
    """MAX_SPLIT_DEPTH is the user-picked value of 5 (not auto-tuned)."""
    from verification_split_llm import MAX_SPLIT_DEPTH

    assert MAX_SPLIT_DEPTH == 5


# ---------------------------------------------------------------------------
# 2. should_split refuses at depth >= MAX_SPLIT_DEPTH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_split_returns_none_at_max_depth():
    """A VP at depth 5 (already at cap) refuses to split further —
    returns None, no LLM call is made, caller falls through to plain
    timeout verdict."""
    from verification_split_llm import LLMVPSplitDecision

    vp = {
        "id": "VP-034-L1-L1-L1-L1-L1",  # depth = 5
        "verification_method": "automated_test",
        "expected_result": "some test",
        "test_command": "pytest tests/",
    }

    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError(
        "should_split must NOT call LLM at max depth"
    )

    result = await LLMVPSplitDecision.should_split(
        agent=MagicMock(coding_tool=fake_tool),
        vp=vp,
        result={"status": "hard_timeout"},
    )
    assert result is None
    fake_tool.query_json.assert_not_called()


@pytest.mark.asyncio
async def test_should_split_returns_none_above_max_depth():
    """A VP at depth 8 (the actual runaway case) also refuses."""
    from verification_split_llm import LLMVPSplitDecision

    vp = {
        "id": "VP-034-L2-L2-L1-L4-L3-L4-L1",  # depth = 7
        "verification_method": "automated_test",
        "expected_result": "...",
        "test_command": "pytest tests/",
    }
    fake_tool = MagicMock()
    fake_tool.query_json.side_effect = AssertionError("must not call LLM")
    result = await LLMVPSplitDecision.should_split(
        agent=MagicMock(coding_tool=fake_tool),
        vp=vp,
        result={"status": "hard_timeout"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_should_split_does_call_llm_below_cap():
    """A VP at depth < MAX_SPLIT_DEPTH still uses LLM split."""
    from verification_split_llm import LLMVPSplitDecision

    vp = {
        "id": "VP-034-L1-L1",  # depth = 2 (well below cap)
        "verification_method": "automated_test",
        "expected_result": "...",
        "test_command": "pytest tests/",
    }
    fake_tool = MagicMock()
    fake_tool.query_json.return_value = {
        "children": [
            {"id": "VP-034-L1-L1-L1", "test_command": "pytest tests/a.py",
             "expected_result": "...", "timeout_seconds": 60},
            {"id": "VP-034-L1-L1-L2", "test_command": "pytest tests/b.py",
             "expected_result": "...", "timeout_seconds": 60},
        ],
    }

    result = await LLMVPSplitDecision.should_split(
        agent=MagicMock(coding_tool=fake_tool),
        vp=vp,
        result={"status": "hard_timeout"},
    )
    assert result is not None
    assert len(result) == 2
    fake_tool.query_json.assert_called_once()


@pytest.mark.asyncio
async def test_should_split_logs_warning_at_cap(caplog):
    """At max depth, a WARNING log is emitted so operators can grep
    ``vp_max_depth_reached``-equivalent markers."""
    import logging
    from verification_split_llm import LLMVPSplitDecision

    vp = {
        "id": "VP-034-L1-L1-L1-L1-L1",
        "verification_method": "automated_test",
        "expected_result": "...",
        "test_command": "pytest tests/",
    }
    fake_tool = MagicMock()
    with caplog.at_level(logging.WARNING, logger="verification_split_llm"):
        await LLMVPSplitDecision.should_split(
            agent=MagicMock(coding_tool=fake_tool),
            vp=vp,
            result={"status": "hard_timeout"},
        )
    assert any("MAX_SPLIT_DEPTH" in rec.message for rec in caplog.records)