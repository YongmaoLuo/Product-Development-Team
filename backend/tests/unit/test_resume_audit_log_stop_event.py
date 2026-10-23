"""A stop leaves an audit trail in the plan's own execution log.

What this pins
--------------
``stop`` used to answer the operator in two places and nowhere else: the
HTTP response said how many tasks were interrupted, and
``pause_state.json`` held the detail. Neither of those is an *audit*
record — the response is gone when the request ends, and the checkpoint
is consumed on the next resume, so once the resume has happened there is
nothing left that says "this plan was stopped here, and this is what was
in flight when it was". Auditing a stop then meant counting how many
times a session had been reused and reconstructing the story from that.

``backend/resume_audit_log.py`` is the third record: the stop is written
into the plan's execution log, next to everything else the executor
already says about that plan, and it stays there.

These tests pin three properties of that record:

* **one event, carrying the detail** -- a stop writes exactly one
  ``execution_stop_checkpoint`` event, and the interrupted / unfinished
  task lists in it are the ones the checkpoint held;
* **it lands in the plan's own directory** -- the event goes to
  ``<plan_dir>/execution.log`` and the module never consults the
  process-wide plans resolver, because a resolver that pointed
  elsewhere would file a fixture's event into the operator's live plans
  tree;
* **a thin checkpoint is still a checkpoint** -- a checkpoint with no
  task lists at all is a legitimate answer ("the stop interrupted
  nothing"), not a crash.

The fourth test pins the boundary the writer promises: entries that are
not objects are skipped, and a task list that is not a list at all is
read as empty.

Note that the module is imported as a module rather than by name: the
attribute is looked up at call time, so a missing ``log_stop_checkpoint``
fails these tests as a plain failure rather than as a collection error
that hides whether the assertions hold.
"""

import json
from pathlib import Path

import execution_logger
import resume_audit_log
from resume_audit_log import STOP_EVENT

#: A checkpoint with the shape the stop path writes: one task caught
#: mid-flight (with the session a resume could re-open) and one task
#: that was never reached.
CHECKPOINT = {
    "kind": "execution",
    "paused_at": "2026-10-23T06:34:22.123501+00:00",
    "interrupted_tasks": [
        {
            "task_id": "08",
            "status": "in_progress",
            "strategy": "session_resume",
            "reason": "session_id_available",
            "session_id": "2db84cba",
        }
    ],
    "unfinished_tasks": [{"task_id": "09", "status": "pending"}],
}


def _plan_dir(tmp_path: Path) -> Path:
    """A plan directory inside a throwaway plans tree."""
    plan_dir = tmp_path / "plans" / "p1"
    plan_dir.mkdir(parents=True)
    return plan_dir


def _log_entries(plan_dir: Path):
    """Every JSON line of the plan's execution log, in write order."""
    log_path = plan_dir / "execution.log"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def _stop_events(plan_dir: Path):
    """The stop events in the plan's execution log."""
    return [
        entry for entry in _log_entries(plan_dir)
        if entry.get("event") == STOP_EVENT
    ]


def test_stop_event_written_once(tmp_path):
    """One stop writes one event, and it carries the checkpoint's detail."""
    plan_dir = _plan_dir(tmp_path)

    resume_audit_log.log_stop_checkpoint(
        "p1", plan_dir, CHECKPOINT, hard_killed=False
    )

    events = _stop_events(plan_dir)
    assert len(events) == 1, (
        "a stop must leave exactly one audit event, so a reader can count "
        f"stops without de-duplicating; got {len(events)}"
    )

    event = events[0]
    assert event["event"] == STOP_EVENT
    assert event["message"] == "Stop left a resume point: 1 interrupted task(s)"

    data = event["data"]
    assert data["checkpoint_kind"] == "execution"
    assert data["hard_killed"] is False
    assert data["task_count"] == 1
    assert data["interrupted"] == CHECKPOINT["interrupted_tasks"]
    assert data["unfinished"] == CHECKPOINT["unfinished_tasks"]


def test_stop_event_ignores_resolve_plans_dir(tmp_path, monkeypatch):
    """The event lands in the given plan directory, not in the live tree.

    ``resolve_plans_dir()`` is the process-wide resolver, and it answers
    with the operator's real plans tree. A writer that used it would file
    a fixture's event into that tree — the exact pollution the resolver
    exists to make harder. So the resolver is made to fail loudly here:
    if the module consulted it at all, this test would raise rather than
    quietly write somewhere plausible.
    """
    plan_dir = _plan_dir(tmp_path)

    def _must_not_be_called():
        raise AssertionError(
            "the plans resolver must not be consulted for a plan's own log"
        )

    monkeypatch.setattr(execution_logger, "resolve_plans_dir", _must_not_be_called)

    resume_audit_log.log_stop_checkpoint(
        "p1", plan_dir, CHECKPOINT, hard_killed=False
    )

    events = _stop_events(plan_dir)
    assert len(events) == 1
    assert events[0]["data"]["task_count"] == 1


def test_stop_event_missing_lists(tmp_path):
    """A checkpoint with no task lists is a stop that interrupted nothing."""
    plan_dir = _plan_dir(tmp_path)

    resume_audit_log.log_stop_checkpoint(
        "p1",
        plan_dir,
        {"kind": "execution", "paused_at": "2026-10-23T06:34:22.123501+00:00"},
        hard_killed=True,
    )

    events = _stop_events(plan_dir)
    assert len(events) == 1

    data = events[0]["data"]
    assert data["task_count"] == 0
    assert data["interrupted"] == []
    assert data["unfinished"] == []
    assert data["hard_killed"] is True
    assert data["checkpoint_kind"] == "execution"


def test_stop_event_skips_malformed_entries(tmp_path):
    """Entries that are not objects are skipped; a non-list is read empty.

    The checkpoint is written by another layer, so its task lists are
    not this module's to trust: a list of non-objects carries no task to
    audit, and a value that is not a list at all is not a task list. Both
    are read as "nothing to report" rather than raising — the stop
    itself already succeeded, and losing the audit record over a
    malformed list is worse than recording an empty one.
    """
    plan_dir = _plan_dir(tmp_path)

    resume_audit_log.log_stop_checkpoint(
        "p1",
        plan_dir,
        {
            "kind": "execution",
            "interrupted_tasks": ["not-a-task", 17, {"task_id": "08"}],
            "unfinished_tasks": "not-a-list",
        },
        hard_killed=False,
    )

    events = _stop_events(plan_dir)
    assert len(events) == 1

    data = events[0]["data"]
    assert data["interrupted"] == [{"task_id": "08"}]
    assert data["unfinished"] == []
    assert data["task_count"] == 1
