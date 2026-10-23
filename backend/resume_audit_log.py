"""Resume audit log — the durable record of a stop and the resume it left.

Why this module exists
----------------------
``stop`` used to answer the operator in exactly two places, and neither
of them was an audit record. The HTTP response said how many tasks were
interrupted — and was gone when the request ended. ``pause_state.json``
held the detail — and was *consumed* on the next resume, deleted before
the work restarted. Once the resume had happened there was nothing left
anywhere that said "this plan was stopped here, and this is what was in
flight when it was". Auditing a stop then meant counting how many times
a session had been reused and reconstructing the story from that count,
which cannot distinguish a stop that interrupted one task from one that
interrupted five.

The execution log is where the answer belongs. It already carries every
other thing the executor says about a plan, it is append-only, it
rotates rather than being consumed, and it lives in the plan's own
directory — so the audit record of a stop travels with the plan instead
of with the process that stopped it.

The events
----------
Two names, one per side of the pause:

* :data:`STOP_EVENT` — ``execution_stop_checkpoint``. Written when a stop
  leaves a resume point behind. It records what kind of interruption it
  was, whether the executor had to be killed outright, how many tasks
  were caught mid-flight, and the task lists themselves — so a reader
  can reconstruct the interruption without the checkpoint that a later
  resume will delete.
* :data:`RESUME_EVENT` — ``execution_resume_decision``. The other side
  of the pause: what a resume actually decided to do with the point it
  found. It is named here, next to the event it pairs with, so the two
  halves of a pause share one vocabulary.

The stop event
--------------
::

    {
      "event": "execution_stop_checkpoint",
      "message": "Stop left a resume point: 1 interrupted task(s)",
      "data": {
        "checkpoint_kind": "execution",
        "hard_killed": false,
        "task_count": 1,
        "interrupted": [{"task_id": "08", "status": "in_progress",
                         "strategy": "session_resume",
                         "reason": "session_id_available",
                         "session_id": "2db84cba"}],
        "unfinished": [{"task_id": "09", "status": "pending"}]
      }
    }

``task_count`` is the number of *interrupted* tasks, not the sum of
both lists: the interrupted list is the one that says what was in flight
when the stop landed, and the unfinished list is what was never reached.
A count of the sum would blur the two, and "how much was in flight" is
the question the audit is asked.

The resume event
----------------
::

    {
      "event": "execution_resume_decision",
      "message": "Resuming from checkpoint: session_resume",
      "data": {
        "resume_strategy": "session_resume",
        "resume_plan": [{"task_id": "08", "strategy": "session_resume",
                         "reason": "session_id_available"}],
        "sessions": {"08": "2db84cba"},
        "checkpoint_written_at": "2026-10-23T06:34:22.123501+00:00"
      }
    }

This is the other half of the pause, and it answers the question the
stop event cannot: *why did this task continue in its session instead
of being re-run?* The decision is made by the resume path, which
already published it in its HTTP response — and a response is gone
when the request ends, so afterwards nothing said why.

The event is a **copy** of that decision, not a second derivation of
it. The strategy, the plan and the sessions are taken exactly as the
caller resolved them; re-deriving them here would be a second answer,
and two answers that disagree make the audit record worse than none.
``checkpoint_written_at`` is the only field this module contributes
that the decision does not already carry: the moment the stop wrote the
resume point, so a reader can place the pause on a timeline.

Degradation, not exception
--------------------------
The task lists belong to another layer — the stop path writes them, and
a checkpoint is a document read back from disk. So they are not trusted:
a list that is missing, a list that is not a list at all, and an entry
that is not an object are all read as "nothing to report" rather than
raising. The stop has already succeeded by the time this module is
called, and losing the audit record over a malformed list is worse than
recording an empty one.

A write that fails is **not** swallowed here. The caller isolates it —
the audit record is worth having, but not worth failing a stop over, and
the caller knows which side of that line it is on.

The resume side degrades the same way, and for the same reason. The
decision it records belongs to another layer and is not re-derived
here; the one field this module adds — the checkpoint's write time — is
a nicety that travels alongside the decision, so a checkpoint without
one is recorded with a null timestamp rather than being refused. Losing
the decision because the timestamp was missing would cost the reader
the very thing the event exists to preserve.

Where the event lands
---------------------
The event goes to ``<plan_dir>/execution.log``, which means the logger
is built from ``plan_dir.parent`` — the plan's own plans tree — and not
from the process-wide ``resolve_plans_dir()``. The resolver answers with
the operator's live plans tree, so a writer that used it would file a
fixture's event into that tree. The plan directory is the truth about
*which* plan the event belongs to, and the writer follows it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List

#: The event that records the resume point a stop left behind.
STOP_EVENT = "execution_stop_checkpoint"

#: The event that records what a resume decided to do about that point.
RESUME_EVENT = "execution_resume_decision"


def _logger(plan_id: str, plan_dir: Path):
    """The execution logger for the plan that lives in ``plan_dir``.

    ``plan_dir`` is the plan's own directory (``plans/<plan_id>/``), so
    its parent is the plans tree the log belongs to. The process-wide
    resolver is deliberately not used: it answers with the operator's
    live tree, which is not where a fixture's events belong.
    """
    from execution_logger import ExecutionLogger

    return ExecutionLogger(plan_id, plans_dir=Path(plan_dir).parent)


def _task_entries(checkpoint: Mapping[str, Any], key: str) -> List[Dict[str, Any]]:
    """The object entries of ``checkpoint[key]``, and nothing else.

    A missing key, a value that is not a list, and an entry that is not
    an object all read as "nothing here". The entries that survive are
    copied, so the event carries a snapshot of the checkpoint rather
    than a live view of it.
    """
    raw = checkpoint.get(key)
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, dict)]


def log_stop_checkpoint(
    plan_id: str,
    plan_dir: Path,
    checkpoint: Mapping[str, Any],
    *,
    hard_killed: bool,
) -> None:
    """Record the resume point a stop left, in the plan's execution log.

    ``checkpoint`` is the document the stop path wrote: the ``kind`` of
    interruption, and the two task lists. ``hard_killed`` says whether
    the executor had to be killed outright — a different kind of
    interruption from a cooperative exit, and the one thing about a stop
    that the checkpoint itself does not carry.

    The event is written once per stop, into ``<plan_dir>/execution.log``.
    A write that fails propagates: this module does not decide that a
    missing audit record is worth failing a stop over — the caller does.
    """
    source: Mapping[str, Any] = checkpoint if isinstance(checkpoint, Mapping) else {}

    interrupted = _task_entries(source, "interrupted_tasks")
    unfinished = _task_entries(source, "unfinished_tasks")

    _logger(plan_id, plan_dir).info(
        STOP_EVENT,
        f"Stop left a resume point: {len(interrupted)} interrupted task(s)",
        data={
            "checkpoint_kind": source.get("kind"),
            "hard_killed": bool(hard_killed),
            "task_count": len(interrupted),
            "interrupted": interrupted,
            "unfinished": unfinished,
        },
    )


def log_resume_decision(
    plan_id: str,
    plan_dir: Path,
    *,
    resume_strategy: str,
    resume_plan: Sequence[Mapping[str, Any]],
    sessions: Mapping[str, str],
    checkpoint: Mapping[str, Any],
) -> None:
    """Record what a resume decided to do, in the plan's execution log.

    The four arguments are the decision the resume path already made and
    already published: the overall ``resume_strategy``, the per-task
    ``resume_plan``, the task id → session id mapping, and the
    checkpoint the decision was made from. They are **copied**, never
    re-derived — a second derivation would be a second answer, and two
    answers that disagree make the audit record worse than none. The
    checkpoint is read only for its write time, which is the one field
    the event adds that the decision does not already carry.

    The event is written once per resume, into ``<plan_dir>/execution.log``.
    A write that fails propagates: this module does not decide that a
    missing audit record is worth failing a resume over — the caller
    does.
    """
    source: Mapping[str, Any] = checkpoint if isinstance(checkpoint, Mapping) else {}

    _logger(plan_id, plan_dir).info(
        RESUME_EVENT,
        f"Resuming from checkpoint: {resume_strategy}",
        data={
            "resume_strategy": resume_strategy,
            "resume_plan": [dict(entry) for entry in resume_plan],
            "sessions": dict(sessions),
            "checkpoint_written_at": source.get("paused_at"),
        },
    )
