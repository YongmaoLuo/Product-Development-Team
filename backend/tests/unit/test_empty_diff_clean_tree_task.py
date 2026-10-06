"""A task whose test command demands a clean tree is not an empty-diff failure.

2026-10-05: task ``20-5-12-3-1`` was titled "Restore the clobbered
gate file from HEAD and prove the restored module is the committed
one", and carried this ``test_command``:

    git diff --exit-code HEAD -- backend/tests/static_gates/… && …

Its success criterion *was* a clean tree. The executor's empty-diff
gate read that same clean tree as "the subagent did nothing", retried
five times, and failed the task — the two signals were in direct
contradiction, both of them readable from the same task row.

The audit-task carve-out that lets an empty diff stand does not cover
this case, and must not be widened to cover it: 2026-09-20 established
that a repair task may never be classified audit-style, because a
repair task that changes nothing and completes anyway leaves the
failure set byte-identical and the loop reports false convergence.
Widening that rule would reopen that bug.

So the exemption is its own signal, read from the task's declared
success criterion rather than inferred from prose.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent, is_repair_task  # noqa: E402
from task import SubTask  # noqa: E402


def _agent() -> AutonomousAgent:
    return AutonomousAgent.__new__(AutonomousAgent)


def _task(test_command: str, *, title: str = "t", group: str | None = None):
    return SubTask(
        id="20-5-12-3-1",
        title=title,
        description="d",
        test_command=test_command,
        files_to_modify=[],
        depends_on=[],
        model_type="medium",
        project_dir=None,
        task_group=group,
    )


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------


def test_restore_task_asserting_a_clean_tree_is_exempt():
    task = _task(
        "git diff --exit-code HEAD -- backend/tests/static_gates/x.py "
        "&& cd backend && ./.venv/bin/python3 -m pytest x.py --collect-only -q",
        title="Restore the clobbered gate file from HEAD",
    )
    assert _agent()._expects_clean_tree(task) is True


def test_git_diff_quiet_also_counts():
    assert _agent()._expects_clean_tree(_task("git diff --quiet HEAD")) is True


# ---------------------------------------------------------------------------
# Ordinary code tasks are untouched
# ---------------------------------------------------------------------------


def test_ordinary_test_command_is_not_exempt():
    assert _agent()._expects_clean_tree(_task("pytest -q tests/")) is False


def test_a_diff_without_a_clean_tree_assertion_is_not_exempt():
    """``git diff --stat`` prints a diff; it does not require one absent."""
    task = _task("git diff --stat HEAD && pytest -q")
    assert _agent()._expects_clean_tree(task) is False


def test_missing_test_command_is_not_exempt():
    assert _agent()._expects_clean_tree(_task("")) is False


def test_the_flag_does_not_leak_across_operators():
    """The pattern must not match past a ``&&`` into another command."""
    task = _task("pytest -q && echo 'git diff --exit-code HEAD'")
    assert _agent()._expects_clean_tree(task) is False


# ---------------------------------------------------------------------------
# The 2026-09-20 protection is not reopened
# ---------------------------------------------------------------------------


def test_repair_task_is_still_not_audit_classified():
    """A repair task keeps the non-audit verdict it was given.

    The exemption is a separate signal checked alongside
    ``is_audit``; it must not have been smuggled into
    ``_looks_like_audit_task``, which would let a repair task that
    changes nothing complete on an empty diff.
    """
    task = _task(
        "pytest -q",
        title="把 provider 默认读取的钥匙串改指独立钥匙串",
        group="repair-round-1",
    )
    assert is_repair_task(task) is True
    assert _agent()._looks_like_audit_task(task) is False
    # ...and, having an ordinary test command, it gains nothing here.
    assert _agent()._expects_clean_tree(task) is False


def test_repair_restore_task_gets_the_exemption_not_the_audit_verdict():
    """The one repair shape that legitimately produces no diff."""
    task = _task(
        "git diff --exit-code HEAD -- backend/gate.py",
        title="Restore the clobbered file from HEAD",
        group="repair-round-2",
    )
    assert is_repair_task(task) is True
    assert _agent()._looks_like_audit_task(task) is False
    assert _agent()._expects_clean_tree(task) is True
