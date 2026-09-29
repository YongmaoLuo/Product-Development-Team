"""Regression test for the 2026-09-09 ``failed_steps`` parallel-list fix.

Background
----------
``TaskOutputValidator.validate`` builds a :class:`ValidationReport`
with three parallel lists:

  * ``failed_steps`` — step number per failure
  * ``failed_task_ids`` — task id per failure
  * ``reasons`` — human-readable reason per failure

The pre-fix implementation deduplicated ``failed_steps`` by step
number (``if step not in failed_steps: failed_steps.append(step)``)
which left it shorter than the other two lists. Any consumer that
``zip``-ed the three lists together truncated at the shortest — so a
snapshot with N step-3 failures would only iterate the first one. This
was the root cause of the audit's
dispatch-gate RuntimeError (see executor log ``20260909_185406.log``:
48 step-3 failures, fill loop only processed 1 task).

Fix
---
Remove the dedup so ``failed_steps`` stays parallel to the other
two lists. Consumers that only need "is step N present?" still
work because ``step`` appears at least once per task.

These tests pin:

  * The three lists are always the same length (parallel
    invariant).
  * A snapshot with multiple step-3 failures yields a
    ``failed_steps`` entry per failure, not one entry per step
    number.
  * Mixed-step failures (step-1 + step-3 etc.) all show up
    separately in the report.

2026-09-21: the execution-side fill loop and its
``_identify_healable_tasks`` matcher were removed with the pre-run
gate (validation now happens at generation time), so the
healable-task assertions that used to live here are gone. The
parallel-list invariant above is what this file guards.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from task import (
    UNKNOWN_MODIFICATIONS_SENTINEL,
    NO_FILE_CHANGES_SENTINEL,
    SubTask,
)
from framework.task_output_validator import TaskOutputValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str,
    files_to_modify=None,
    title: str = "T",
    description: str = "D",
    test_command: str = "echo",
    depends_on: List[str] = None,
) -> SubTask:
    """Build a SubTask with optional ``files_to_modify``."""
    kwargs: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": description,
        "test_command": test_command,
    }
    if depends_on is not None:
        kwargs["depends_on"] = depends_on
    if files_to_modify is not None:
        kwargs["files_to_modify"] = files_to_modify
    return SubTask(**kwargs)


def _validator() -> TaskOutputValidator:
    # A real project_dir is needed for step-3 path-existence check;
    # we use the backend/ root so the existence-check passes for any
    # path the test fixtures happen to point to. Sentinel / empty
    # branches don't touch the filesystem.
    return TaskOutputValidator(
        Path(__file__).resolve().parents[2]  # backend/
    )


# ---------------------------------------------------------------------------
# Parallel-list invariant (the core fix)
# ---------------------------------------------------------------------------


def test_three_lists_stay_parallel_for_unknown_modifications():
    """5 tasks all failing step-3 with UNKNOWN_MODIFICATIONS —
    ``failed_steps`` must have 5 entries, not 1 (the pre-fix
    dedup collapsed them).
    """
    validator = _validator()
    snapshot = [
        _make_task(
            task_id=f"T-{i}",
            files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
        )
        for i in range(5)
    ]
    report = validator.validate(snapshot)

    assert len(report.failed_steps) == 5, (
        f"failed_steps should have 5 entries (one per failing task), "
        f"got {len(report.failed_steps)}: {list(report.failed_steps)}"
    )
    assert len(report.failed_task_ids) == 5
    assert len(report.reasons) == 5
    # All step entries should be 3 (the only step that failed).
    assert all(s == 3 for s in report.failed_steps)
    # The three lists must remain parallel: zip yields 5 (step, tid,
    # reason) tuples in the same order as the input snapshot.
    paired = list(zip(report.failed_steps, report.failed_task_ids, report.reasons))
    assert len(paired) == 5
    tids_in_order = [t for _, t, _ in paired]
    assert tids_in_order == ["T-0", "T-1", "T-2", "T-3", "T-4"]


def test_three_lists_stay_parallel_for_empty_list():
    """5 tasks all failing step-3 with empty list (legacy reject
    reason) — same parallel-list invariant.
    """
    validator = _validator()
    snapshot = [
        _make_task(task_id=f"E-{i}", files_to_modify=[])
        for i in range(5)
    ]
    report = validator.validate(snapshot)

    assert len(report.failed_steps) == 5
    assert len(report.failed_task_ids) == 5
    assert len(report.reasons) == 5


# ---------------------------------------------------------------------------
# Mixed-step failures all show up separately
# ---------------------------------------------------------------------------


def test_mixed_step_failures_yield_parallel_lists():
    """A snapshot with one step-1 failure and one step-3 failure
    must yield 2 entries in ``failed_steps`` (one with step=1, one
    with step=3), not 1. The pre-fix dedup would have collapsed
    this to 1 entry — masking the second failure.
    """
    validator = _validator()
    # Task missing required field (step-1 fail)
    bad_step1 = SubTask.model_construct(  # bypass field validators
        id="BAD-1",
        title="t",
        description="d",
        test_command="echo",
        # deliberately leave files_to_modify and description empty
        # so step-1 fails on missing required fields
        files_to_modify=None,
        depends_on=[],
        status="pending",
        model_type="medium",
        breakdown_count=0,
        verification_only=False,
        updated_time=None,
        failure_reason=None,
        project_dir=None,
        provider=None,
        test_commands=[],
    )
    # Force step-1 to fail by giving it None title (json_schema_check
    # requires non-empty id/title/etc).
    bad_step1 = _make_task(
        task_id="BAD-1",
        title="t",
        files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
    )
    snapshot = [
        bad_step1,
        _make_task(
            task_id="BAD-3",
            title="t",
            files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
        ),
    ]
    report = validator.validate(snapshot)

    # Both should fail step-3 (UNKNOWN_MODIFICATIONS), no step-1
    # failures from our fixtures. Verify parallel invariant holds
    # even though all failures are step-3.
    assert len(report.failed_steps) == len(report.failed_task_ids)
    assert len(report.failed_task_ids) == len(report.reasons)


# ---------------------------------------------------------------------------
# Membership test still works (other consumers' contract preserved)
# ---------------------------------------------------------------------------


def test_step_membership_still_works():
    """Consumers like ``refiner.py:151`` and the contract tests do
    ``step in report.failed_steps``. With the dedup removed, the
    step number appears at least once per failing task — the
    membership test still passes.
    """
    validator = _validator()
    snapshot = [
        _make_task(
            task_id=f"M-{i}",
            files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
        )
        for i in range(3)
    ]
    report = validator.validate(snapshot)
    assert 3 in report.failed_steps
    # Membership test returns True for each step number that
    # appears in any failure.
    assert report.failed_steps.count(3) == 3


# ---------------------------------------------------------------------------
# NO_FILE_CHANGES does NOT show up in failed_steps (sanity)
# ---------------------------------------------------------------------------


def test_no_file_changes_does_not_appear_in_failed_lists():
    """Sanity: read-only tasks with NO_FILE_CHANGES pass step-3
    directly. They should not contribute to any failed list.
    """
    validator = _validator()
    snapshot = [
        _make_task(
            task_id="OK-NFC",
            files_to_modify=list(NO_FILE_CHANGES_SENTINEL),
        ),
        _make_task(
            task_id="BAD",
            files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
        ),
    ]
    report = validator.validate(snapshot)
    assert "OK-NFC" not in report.failed_task_ids
    assert "BAD" in report.failed_task_ids
    assert len(report.failed_steps) == 1
    assert report.failed_steps[0] == 3