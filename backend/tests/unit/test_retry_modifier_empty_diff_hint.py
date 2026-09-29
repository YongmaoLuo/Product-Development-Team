"""
Tests for ``retry_manager.get_retry_prompt_modifier`` actionable
empty-diff hint.

Background (2026-09-11): a generic
"please try a different approach" hint gives the subagent nothing
actionable when the failure was ``empty_diff_no_changes``. The
subagent must be told to actually use the Edit / Write tools and to
verify the diff with ``git diff --stat`` before claiming done.

These tests assert the new branch in ``get_retry_prompt_modifier``
emits a targeted hint when ``state.last_error`` starts with the
``empty_diff_no_changes`` sentinel, and that the generic hint is
preserved for all other failure modes.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from retry_manager import RetryManager  # noqa: E402


def test_empty_diff_hint_renders_actionable_instructions():
    """Empty-diff prefix → prompt mentions Edit/Write + git diff --stat."""
    rm = RetryManager()
    sentinel = (
        "empty_diff_no_changes: task [1-1] produced no file modifications "
        "and no prior deliverable exists on disk. You must use Edit/Write "
        "tools to actually modify the source files declared in files_to_modify."
    )
    rm.record_attempt("t1", sentinel, success=False)
    text = rm.get_retry_prompt_modifier("t1")
    # ACTION REQUIRED prefix marks the targeted branch.
    assert "ACTION REQUIRED" in text
    # Tells the subagent exactly which tool to use.
    assert "Edit" in text
    assert "Write" in text
    # Asks for self-verification via ``git diff --stat``.
    assert "git diff --stat" in text
    # Carries the original error so the subagent knows what failed.
    assert "empty_diff_no_changes" in text


def test_non_empty_diff_hint_keeps_generic_message():
    """Non-empty-diff errors still get the original generic hint."""
    rm = RetryManager()
    rm.record_attempt("t2", "AssertionError: test_xyz expected 1 got 2", success=False)
    text = rm.get_retry_prompt_modifier("t2")
    # Generic hint should NOT include the actionable empty-diff branch.
    assert "ACTION REQUIRED" not in text
    # But it should carry the error context (the original branch).
    assert "AssertionError" in text
    assert "try a different approach" in text


def test_empty_diff_hint_does_not_suggest_simplification():
    """The targeted hint does not metric into "break it down" suggestions.

    The "consider: 1. Breaking down the task" branch is reserved for
    later retries with a generic error; the empty-diff branch should
    always emit the actionable instructions regardless of attempt count.
    """
    rm = RetryManager()
    sentinel = "empty_diff_no_changes: still nothing on disk"
    # Burn through 4 attempts — past the attempt_count <= 3 generic branch.
    for _ in range(4):
        rm.record_attempt("t3", sentinel, success=False)
    text = rm.get_retry_prompt_modifier("t3")
    assert "ACTION REQUIRED" in text
    # The "Breaking down the task" suggestion belongs to the
    # generic branch and must NOT show for empty-diff errors.
    assert "Breaking down the task" not in text