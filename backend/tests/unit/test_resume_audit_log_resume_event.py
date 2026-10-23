"""A resume writes its decision into the plan's own execution log.

What this pins
--------------
``start`` resolves a stop's checkpoint into three things: the overall
``resume_strategy``, the per-task ``resume_plan``, and the task id →
session id mapping. All three went into the HTTP response — and into
nothing else. The response is gone when the request ends, so afterwards
there was no way to answer the question the operator actually asks:
*why did this task continue in its session instead of being re-run?*

``backend/resume_audit_log.py`` is the third record: the decision is
written into the plan's execution log, next to the ``execution_stop_
checkpoint`` event it is the other half of.

These tests pin three properties of that record:

* **it is a verbatim copy of the decision** -- the strategy, the plan,
  and the sessions in the event are the ones the response carried,
  character for character. This module does not re-derive the
  strategy: a second derivation is a second answer, and the two could
  disagree, which would make the audit record worse than useless.
* **it lands in the plan's own directory** -- the event goes to
  ``<plan_dir>/execution.log`` and the module never consults the
  process-wide plans resolver, because a resolver that pointed
  elsewhere would file a fixture's event into the operator's live plans
  tree.
* **a checkpoint without a timestamp is still a checkpoint** -- the
  write time is a nicety carried alongside the decision, and its
  absence is not a reason to lose the decision.

Note that the module is imported as a module rather than by name: the
attribute is looked up at call time, so a missing
``log_resume_decision`` fails these tests as a plain failure rather
than as a collection error that hides whether the assertions hold.
"""

import json
from pathlib import Path

import execution_logger
import resume_audit_log
from resume_audit_log import RESUME_EVENT

#: The decision a mixed resume makes: one task continues in the session
#: the interruption left usable, the other is re-run whole.
RESUME_STRATEGY = "mixed"

RESUME_PLAN = [
    {"task_id": "08", "strategy": "session_resume", "reason": "session_id_available"},
    {"task_id": "09", "strategy": "full_rerun", "reason": "no_session_id"},
]

#: Only the task that continues *in* a session appears here -- a
#: re-run task is deliberately absent, because handing it a session id
#: would resume a conversation the interruption invalidated.
SESSIONS = {"08": "2db84cba"}

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


def _resume_events(plan_dir: Path):
    """The resume events in the plan's execution log."""
    return [
        entry for entry in _log_entries(plan_dir)
        if entry.get("event") == RESUME_EVENT
    ]


def test_resume_event_copies_fields(tmp_path):
    """The event carries the decision the response carried, verbatim."""
    plan_dir = _plan_dir(tmp_path)

    resume_audit_log.log_resume_decision(
        "p1",
        plan_dir,
        resume_strategy=RESUME_STRATEGY,
        resume_plan=RESUME_PLAN,
        sessions=SESSIONS,
        checkpoint=CHECKPOINT,
    )

    events = _resume_events(plan_dir)
    assert len(events) == 1, (
        "a resume must leave exactly one decision record, so a reader can "
        f"find the decision without de-duplicating; got {len(events)}"
    )

    event = events[0]
    assert event["event"] == RESUME_EVENT
    assert event["message"] == "Resuming from checkpoint: mixed"

    data = event["data"]
    assert data["resume_strategy"] == RESUME_STRATEGY
    assert data["resume_plan"] == RESUME_PLAN
    assert data["sessions"] == SESSIONS
    assert data["checkpoint_written_at"] == CHECKPOINT["paused_at"]


def test_resume_event_missing_paused_at(tmp_path):
    """A checkpoint with no timestamp still records the decision.

    The write time travels alongside the decision; it is not the
    decision. A checkpoint that never got one -- because it was
    written by a path that did not stamp it -- must not cost the
    operator the reason the resume chose what it chose.
    """
    plan_dir = _plan_dir(tmp_path)

    resume_audit_log.log_resume_decision(
        "p1",
        plan_dir,
        resume_strategy=RESUME_STRATEGY,
        resume_plan=RESUME_PLAN,
        sessions=SESSIONS,
        checkpoint={"kind": "execution"},
    )

    events = _resume_events(plan_dir)
    assert len(events) == 1

    data = events[0]["data"]
    assert data["checkpoint_written_at"] is None
    assert data["resume_strategy"] == RESUME_STRATEGY
    assert data["resume_plan"] == RESUME_PLAN
    assert data["sessions"] == SESSIONS


def test_resume_event_ignores_resolve_plans_dir(tmp_path, monkeypatch):
    """The event lands in the given plan directory, not in the live tree.

    ``resolve_plans_dir()`` is the process-wide resolver, and it answers
    with the operator's real plans tree. A writer that used it would
    file a fixture's event into that tree -- the exact pollution the
    resolver exists to make harder. So the resolver is made to fail
    loudly here: if the module consulted it at all, this test would
    raise rather than quietly write somewhere plausible.
    """
    plan_dir = _plan_dir(tmp_path)

    def _must_not_be_called():
        raise AssertionError(
            "the plans resolver must not be consulted for a plan's own log"
        )

    monkeypatch.setattr(execution_logger, "resolve_plans_dir", _must_not_be_called)

    resume_audit_log.log_resume_decision(
        "p1",
        plan_dir,
        resume_strategy=RESUME_STRATEGY,
        resume_plan=RESUME_PLAN,
        sessions=SESSIONS,
        checkpoint=CHECKPOINT,
    )

    events = _resume_events(plan_dir)
    assert len(events) == 1
    assert events[0]["data"]["resume_strategy"] == RESUME_STRATEGY
