"""
Tests for ``retry_manager.get_retry_prompt_modifier``'s dedicated
undeclared-write (write-contract) branch.

Background (2026-11-21): when an attempt writes a path it never
declared, the executor now ROLLS THE WRITE BACK and keeps the
rolled-back content as a patch — the work is not lost, it lives in the
patch.  The next attempt therefore has to be told two things:

  1. where the patch is (``rollback_patch: <path>``), so the qualified
     part of the work can be restored with ``git apply <patch>`` instead
     of being rewritten from scratch; and
  2. that the restored file has to be added to ``files_to_modify``,
     because a file outside the declared scope is a violation no matter
     how good its content is.

Without this branch the failure fell through to the generic
"please try a different approach" hint, which reads as "your code is
wrong" — the subagent's response is to rewrite the work, throwing away
the patch that already holds it.

These tests pin the branch, and pin that the two existing targeted
branches (``report_format_retry:`` / ``empty_diff_no_changes``) and the
generic branch are NOT shadowed by it.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from retry_manager import RetryManager  # noqa: E402


#: The failure text the executor writes after rolling an undeclared
#: write back (see ``attempt_rollback.format_rollback_failure``).
UNDECLARED_WRITE_ERROR = (
    "undeclared_write: scripts/x.sh\n"
    "rollback_patch: /p/1.patch\n"
    "rollback_rolled_back: scripts/x.sh\n"
    "rollback_failed: (none)"
)


def test_modifier_carries_patch_path_and_restore_instruction():
    """Undeclared write → the prompt names the patch and ``git apply``."""
    rm = RetryManager()
    rm.record_attempt("t1", UNDECLARED_WRITE_ERROR, False)
    text = rm.get_retry_prompt_modifier("t1")
    # The attempt counter header is still rendered.
    assert "[RETRY CONTEXT - Attempt 2 of" in text
    # The patch path is quoted verbatim — the subagent has to be able to
    # copy it into a command.
    assert "/p/1.patch" in text
    # The recovery instruction names the command that restores it.
    assert "git apply" in text
    # The original failure text is quoted so the subagent knows which
    # path was out of scope.
    assert "undeclared_write" in text
    assert "scripts/x.sh" in text
    # The restored file has to be declared, or the next attempt trips
    # the same gate.
    assert "files_to_modify" in text


def test_modifier_forbids_rewrite():
    """The prompt forbids rewriting the work instead of restoring it."""
    rm = RetryManager()
    rm.record_attempt("t2", UNDECLARED_WRITE_ERROR, False)
    text = rm.get_retry_prompt_modifier("t2")
    assert "do not rewrite" in text


def test_missing_patch_line_still_warns():
    """No ``rollback_patch:`` line → the contract hint still renders."""
    rm = RetryManager()
    rm.record_attempt(
        "t3",
        "undeclared_write: scripts/x.sh\nrollback_failed: (none)",
        False,
    )
    text = rm.get_retry_prompt_modifier("t3")
    # The write-contract hint is still there.
    assert "undeclared_write" in text
    assert "scripts/x.sh" in text
    assert "do not rewrite" in text
    # No bogus patch path is invented for a missing patch.
    assert "git apply (none)" not in text


def test_generic_error_unchanged():
    """An ordinary failure still gets the generic hint."""
    rm = RetryManager()
    rm.record_attempt("t4", "AssertionError: test_xyz expected 1 got 2", False)
    text = rm.get_retry_prompt_modifier("t4")
    assert "try a different approach" in text
    assert "AssertionError" in text
    # The targeted branches must not leak into the generic one.
    assert "ACTION REQUIRED" not in text
    assert "git apply" not in text


def test_report_format_branch_is_not_shadowed():
    """``report_format_retry:`` still takes its own branch."""
    rm = RetryManager()
    rm.record_attempt(
        "t5",
        "report_format_retry: report_format: the evidence was truncated",
        False,
    )
    text = rm.get_retry_prompt_modifier("t5")
    assert "SHAPE OF YOUR ANSWER" in text
    assert "report_format_retry" in text
    # The write-contract branch must not swallow it.
    assert "git apply" not in text
    assert "do not rewrite" not in text
