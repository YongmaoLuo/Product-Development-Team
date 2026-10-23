"""The new audit events must not change what the card thinks the plan is doing.

What this pins
--------------
``current_activity()`` derives the executor's activity from the tail of the
plan's execution log, mapping log events onto the small closed vocabulary
the card renders (``_ACTIVITY_EVENT_KINDS``). An event that is not in that
table is *skipped* — the scan keeps walking backwards until it finds a
recognised event.

That is the right shape for a new event. The two events the resume audit
introduces — ``execution_stop_checkpoint`` and
``execution_resume_decision`` — are *records*, not activities: nothing is
running while they are written, and the executor may still be in the
middle of a task either side of them. Two things must therefore hold:

* the new events must not make a running plan look **idle** — a card that
  says "空闲" while a task is in flight is the exact failure this
  regression is about;
* the new events must not introduce an **activity kind of their own** —
  the card would then render an activity type that no code path can
  produce a label for, and the vocabulary the card renders is closed.

These tests pin both, and the third pins that the pre-existing terminal
semantics survive: a stop that ends the run still reads as idle.

Note that no production code changes here. ``_ACTIVITY_EVENT_KINDS`` is
deliberately left as it was, and the test that checks the new events are
absent from it is the guard against a future "helpful" registration that
would break the card.
"""

from __future__ import annotations

from pathlib import Path

from execution_logger import _ACTIVITY_EVENT_KINDS, ExecutionLogger
from resume_audit_log import RESUME_EVENT, STOP_EVENT

#: A plan id that exists nowhere but in this test's throwaway tree.
PLAN_ID = "p1"


def _plan_dir(tmp_path: Path) -> Path:
    """A plan directory inside a throwaway plans tree."""
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True)
    return plan_dir


def _logger(plan_dir: Path) -> ExecutionLogger:
    """A logger writing into the plan's own plans tree."""
    return ExecutionLogger(PLAN_ID, plans_dir=plan_dir.parent)


def test_new_events_keep_last_task_activity(tmp_path):
    """A stop / resume pair after a task start does not idle the plan.

    The executor was still working on task 08 when the pause happened, and
    it is still working on it after the resume. The two audit events are
    records of that pause — they must be skipped by the activity
    derivation, so the card keeps naming the running task.
    """
    plan_dir = _plan_dir(tmp_path)
    logger = _logger(plan_dir)

    logger.info("task_started", "started", task_id="08")
    logger.info(STOP_EVENT, "Stop left a resume point: 1 interrupted task(s)")
    logger.info(RESUME_EVENT, "Resuming from checkpoint: session_resume")

    activity = ExecutionLogger.current_activity(PLAN_ID, plans_dir=plan_dir.parent)

    assert activity["kind"] == "task", activity
    assert activity["event"] == "task_started", activity
    assert activity["task_id"] == "08", activity


def test_only_new_events_report_unknown(tmp_path):
    """A log that holds nothing but the new events reads as *unknown*.

    Not as *idle*: "unknown" is the honest answer when no recognised
    activity event exists, and it is what the card already falls back to
    for an unrecognised event. "idle" would be a claim — that the executor
    has nothing in flight — and a log full of audit records does not make
    that claim.
    """
    plan_dir = _plan_dir(tmp_path)
    logger = _logger(plan_dir)

    logger.info(STOP_EVENT, "Stop left a resume point: 1 interrupted task(s)")
    logger.info(RESUME_EVENT, "Resuming from checkpoint: session_resume")

    activity = ExecutionLogger.current_activity(PLAN_ID, plans_dir=plan_dir.parent)

    assert activity["kind"] == "unknown", activity


def test_execution_stopped_still_terminal(tmp_path):
    """The terminal event still wins — the new events do not revive a run.

    ``execution_stopped`` is the pre-existing end-of-run marker. A stop
    that writes its audit record and then ends the run must still read as
    idle, whether the audit record comes before or after it.
    """
    plan_dir = _plan_dir(tmp_path)
    logger = _logger(plan_dir)

    logger.info(STOP_EVENT, "Stop left a resume point: 1 interrupted task(s)")
    logger.info(RESUME_EVENT, "Resuming from checkpoint: session_resume")
    logger.info("execution_stopped", "stopped")

    activity = ExecutionLogger.current_activity(PLAN_ID, plans_dir=plan_dir.parent)

    assert activity["kind"] == "idle", activity
    assert activity["event"] == "execution_stopped", activity


def test_new_events_are_not_activity_kinds(tmp_path):
    """The new events are not registered as activities, and must not be.

    Registering them would give the card an activity kind that describes
    no unit of work — the two events are written *around* a pause, while
    the task they concern is still in flight. A card that renders them as
    an activity would be describing a state the executor is not in.

    The absence is asserted rather than the behaviour: a future change
    that registers them would otherwise still pass every test above if it
    happened to register them under a terminal kind.
    """
    assert STOP_EVENT not in _ACTIVITY_EVENT_KINDS
    assert RESUME_EVENT not in _ACTIVITY_EVENT_KINDS