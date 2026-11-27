"""The repair batch's outcome is FOUR outcomes, not one reason string.

Background
----------
Production stopped the chain with a single reason string that packed two
different facts together::

    repair_batch_not_drained:repair_non_zero_exit:1_pending

Read left to right that says "the batch did not drain, because the executor
exited non-zero, and 1 task is still pending".  But the executor exiting
non-zero and a task still being queued are *independent* facts, and the
string welded them together in the one order that made them unreadable: a
caller could not tell "there is still work to run" (retry) from "the gate is
red and re-running buys nothing" (stop).  The two need opposite responses.

The verdict was therefore split into a pure classifier with four outcomes:

``drained``
    The batch finished: exit code 0, nothing queued, nothing failed.
``pending_remains``
    Work was LEFT — repair tasks are still queued.  The batch has more to
    run, so the caller should continue.
``repair_failed``
    A repair task itself broke.  The batch is over and it is broken.
``gate_red``
    The executor exited non-zero but no repair task failed: the gate is red.
    The batch is over and the code is still wrong.

Contract pinned here
--------------------
* One pending task out of three → ``pending_remains``, ``should_continue``
  is True, and the stop reason carries the count.
* A non-zero exit splits by *why*: a failed repair task is ``repair_failed``,
  a red gate with no failed task is ``gate_red``.  The two are not the same
  event and must not collapse into one.
* Pending work outranks BOTH the exit code and the failed-task count: with
  2 pending and 5 failed tasks the verdict is still ``pending_remains``,
  because the batch has not drained and still has work to run.
* The classifier is a pure function: it reads no environment variable, so
  changing one between two identical calls cannot change the verdict.
* Exit code 0 with nothing left over is ``drained`` and has no stop reason.
* Negative counts are nonsense, not an exception and not a truth: they
  count as zero.
"""

from __future__ import annotations

from verification.repair_outcome import (
    RepairOutcome,
    classify_repair_outcome,
)


def test_one_pending_is_pending_remains():
    """Three repair tasks went in, one is still queued → NOT drained.

    The executor exited non-zero, but the reason the batch did not finish
    is that work was LEFT, not that a task broke.  That distinction is the
    whole point of the classifier: the caller must be able to tell "there
    is still work to run" from "the gate is red".
    """
    outcome = classify_repair_outcome(
        returncode=1, pending_count=1, failed_task_count=0,
    )

    assert outcome.kind == "pending_remains"
    assert outcome.should_continue is True
    assert outcome.repair_failed is False
    assert outcome.stop_reason == "repair_batch_not_drained:1_pending"

    # The count is part of the reason — two pending is a different string
    # from one pending, so the reason cannot be a constant.
    two_pending = classify_repair_outcome(
        returncode=1, pending_count=2, failed_task_count=0,
    )
    assert two_pending.stop_reason == "repair_batch_not_drained:2_pending"
    assert two_pending.kind == "pending_remains"


def test_repair_task_failure_is_distinct_from_gate_red():
    """A non-zero exit splits by WHY: broken repair task vs red gate.

    Both cases exit non-zero with nothing left queued, so the exit code
    alone cannot tell them apart — the failed-task count is what
    distinguishes "the repair broke" from "the gate is still red".
    """
    failed = classify_repair_outcome(
        returncode=1, pending_count=0, failed_task_count=2,
    )
    assert failed.kind == "repair_failed"
    assert failed.repair_failed is True
    assert failed.should_continue is False
    assert failed.stop_reason == "repair_task_failed"

    red = classify_repair_outcome(
        returncode=1, pending_count=0, failed_task_count=0,
    )
    assert red.kind == "gate_red"
    assert red.repair_failed is False
    assert red.should_continue is False
    assert red.stop_reason == "repair_gate_red"


def test_drained_when_nothing_left():
    """Exit code 0 with nothing pending and nothing failed → drained.

    ``stop_reason`` is None: there is nothing to report because nothing
    went wrong.
    """
    outcome = classify_repair_outcome(
        returncode=0, pending_count=0, failed_task_count=0,
    )

    assert outcome.kind == "drained"
    assert outcome.repair_failed is False
    assert outcome.should_continue is False
    assert outcome.stop_reason is None


def test_negative_counts_are_treated_as_zero():
    """A negative count is nonsense — it counts as zero, it does not raise.

    A negative count must not be read as "there IS pending work" (which
    would mis-report the batch as undrained) and must not blow up the
    caller that produced it.
    """
    # Treated as 0 pending → the drained branch, not the pending one.
    drained = classify_repair_outcome(
        returncode=0, pending_count=-1, failed_task_count=-3,
    )
    assert drained.kind == "drained"
    assert drained.should_continue is False
    assert drained.stop_reason is None

    # Treated as 0 pending AND 0 failed → gate_red, not repair_failed.
    red = classify_repair_outcome(
        returncode=1, pending_count=-1, failed_task_count=-3,
    )
    assert red.kind == "gate_red"
    assert red.repair_failed is False
    assert red.stop_reason == "repair_gate_red"


def test_pending_outranks_exit_code_and_failed_tasks():
    """Pending work wins over BOTH the exit code and the failed-task count.

    Two repair tasks are still queued and five already failed.  The
    tempting read is "five failures, the batch is broken" — but the batch
    has NOT drained, so it still has work to run and the caller must be
    told to continue.  ``pending_count > 0`` is checked first, ahead of
    the exit code and ahead of the failed-task count; reversing that order
    would mis-report "there is still work" as "the gate is red" or "the
    repair broke", which is the exact confusion the classifier exists to
    remove.
    """
    outcome = classify_repair_outcome(
        returncode=1, pending_count=2, failed_task_count=5,
    )

    assert outcome.kind == "pending_remains"
    assert outcome.should_continue is True
    assert outcome.repair_failed is False
    assert outcome.stop_reason == "repair_batch_not_drained:2_pending"


def test_classifier_does_not_read_environment(monkeypatch):
    """The classifier is a pure function: no environment, no clock, no DB.

    The same inputs must give the same verdict even when the environment
    changes underneath — otherwise "did this batch drain?" becomes a
    question nobody can recompute, across processes or across retries.

    ``monkeypatch`` restores every variable it touches when the test
    finishes, so the change is scoped to this test and cannot leak into
    the rest of the suite.
    """
    before = classify_repair_outcome(
        returncode=1, pending_count=2, failed_task_count=5,
    )

    monkeypatch.setenv("PDT_REPAIR_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("PDT_FORCE_CONTINUE", "1")
    monkeypatch.setenv("CI", "1")

    after = classify_repair_outcome(
        returncode=1, pending_count=2, failed_task_count=5,
    )

    # RepairOutcome is a frozen dataclass: == compares every field.
    assert after == before
    assert before.kind == "pending_remains"
    assert after.kind == "pending_remains"