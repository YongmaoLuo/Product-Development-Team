"""One plan-level record of a stop that is in progress.

2026-11-20 — the stop path had no memory of itself.  The HTTP handler
read the phase and then forgot it, the spawn gate remembered a bare
boolean, and the quiet-window judgement took a dispatch count on its own
and compared it with a second count it took later.  Three facts, three
places, no shared record — so nothing could answer the three questions a
cooperative stop has to answer:

* **which** plan is stopping (and only that plan — a stop must not
  reach sideways into another plan's work);
* **from which phase** it came in, because the phase decides what has
  to be shut down (a verification round, a repair executor, the main
  execution);
* **what the dispatch baseline was** when the stop began, because
  "nothing new was dispatched" is only meaningful against a baseline
  taken *before* the window opened.

``stop_state`` is that record, and it is deliberately the *first* one:
``begin()`` is called when the stop starts, and the baseline it carries
is the count at that moment.  A baseline re-taken later — or re-taken by
a second stop attempt — would erase the very evidence the quiet window
exists to produce, which is why a second ``begin()`` **keeps the first
record** instead of overwriting it.  A stop that is requested twice is
one stop.

The registry is process-local and tiny, like ``verification_cancel``'s
flag next door: one deployment, one server process, one stop at a time.
It is the *plan-level* layer though, not the round-level one — the
cancel flag says "this round was cancelled", this says "this plan is
being stopped, from here, with this baseline".

One more thing belongs here: the **requeue intent**.  A stop is a
process, so the move back to the waiting phase cannot happen while the
stop is still running — the phase would claim "queued" while the
plan's processes are still alive.  The caller that was asked to
requeue therefore records the intent (see :func:`arm_requeue`), and the
layer that owns the quiet window delivers it (see
:func:`requeue_requested`).  An intent nobody delivers is a lost
requeue, so the two halves live in one place: the same registry that
remembers the stop remembers what should happen once it is over.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set

__all__ = [
    "SCOPE_EXECUTION",
    "SCOPE_VERIFICATION",
    "SCOPE_REPAIR",
    "StopRecord",
    "begin",
    "get",
    "is_stopping",
    "arm_requeue",
    "requeue_requested",
    "clear_requeue",
    "clear",
    "_reset_for_tests",
]


# ---------------------------------------------------------------------------
# The scopes
# ---------------------------------------------------------------------------

#: The main task-execution phase was stopped.
SCOPE_EXECUTION = "execution"

#: The verification round was stopped.
SCOPE_VERIFICATION = "verification"

#: The repair executor was stopped.
SCOPE_REPAIR = "repair"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopRecord:
    """What a plan is being stopped from, and where it was when it began.

    ``scope`` says *which* work is being shut down (one of the
    ``SCOPE_*`` constants), ``previous_phase`` says which phase the
    plan was in when the stop began, ``dispatch_baseline`` is the
    dispatch count at that moment — the reference the quiet window
    compares against — and ``started_at`` is when the stop began
    (epoch seconds).

    The record is frozen: it is the evidence the stop is judged by, and
    evidence that can change after the fact is not evidence.
    """

    plan_id: str
    scope: str
    previous_phase: str
    dispatch_baseline: int
    started_at: float


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_records: Dict[str, StopRecord] = {}

#: The plans that asked to be put back into the waiting phase once their
#: stop is really over.  Keyed by plan id — one plan's stop must never
#: reach sideways into another plan's queue (G5).
_requeue_intents: Set[str] = set()


def begin(
    plan_id: str,
    scope: str = "",
    *,
    previous_phase: str = "",
    dispatch_baseline: int = 0,
) -> Optional[StopRecord]:
    """Record that ``plan_id`` is being stopped, and return the record.

    The first call for a plan wins: a second ``begin()`` returns the
    record that is already there and changes nothing, so the baseline
    taken when the stop actually began survives every later stop
    attempt.  A stop that is requested twice is one stop.

    An empty ``plan_id`` is a no-op (``None``): a caller that is not
    about a plan must not register anywhere, or it would gate work that
    belongs to nobody in particular.
    """
    if not plan_id:
        return None

    with _lock:
        existing = _records.get(plan_id)
        if existing is not None:
            return existing

        try:
            baseline = int(dispatch_baseline)
        except (TypeError, ValueError):
            baseline = 0

        record = StopRecord(
            plan_id=plan_id,
            scope=scope or "",
            previous_phase=previous_phase or "",
            dispatch_baseline=baseline,
            started_at=time.time(),
        )
        _records[plan_id] = record
        return record


def get(plan_id: str) -> Optional[StopRecord]:
    """The stop record of ``plan_id``, or ``None`` when it is not stopping.

    An empty ``plan_id`` is never stopping.
    """
    if not plan_id:
        return None
    with _lock:
        return _records.get(plan_id)


def is_stopping(plan_id: str) -> bool:
    """True while ``plan_id`` has a stop record.

    This is the predicate the spawn gate consults, so it is the one that
    must be cheap and total: it never raises and never blocks on
    anything but the registry lock.
    """
    if not plan_id:
        return False
    with _lock:
        return plan_id in _records


def arm_requeue(plan_id: str) -> bool:
    """Record that ``plan_id`` must go back to the waiting phase.

    Why the intent has to be *remembered*
    -------------------------------------
    A stop is a **process** (PRD G7): the request answers ``stopping``
    while the background layer is still killing the plan's tree, and the
    phase only reaches ``stopped`` after the plan has fallen silent
    (PRD G3).  The move back to ``queued`` therefore happens *after* the
    stop is over — there is no ``user_stopping → queued`` edge, and
    adding one would make the phase claim "queued" while the plan's
    processes are still alive (PRD T3).

    So the caller that was asked to requeue records the **intent** here,
    and the layer that owns the quiet window delivers it.  An intent
    that nobody delivers is a lost requeue, which is exactly the bug
    this record exists to remove.

    The intent is **plan-level**: it is stored per plan id, so one
    plan's stop never queues another plan (G5).  Arming it twice is
    the same intent, not two.

    An empty ``plan_id`` is a no-op (``False``): a caller that is not
    about a plan must not register anywhere.
    """
    if not plan_id:
        return False
    with _lock:
        _requeue_intents.add(plan_id)
    return True


def requeue_requested(plan_id: str) -> bool:
    """True while ``plan_id`` is waiting to be put back into the queue.

    The predicate the background layer consults before it moves a
    stopped plan back to ``queued``.  Empty ids are never waiting.
    """
    if not plan_id:
        return False
    with _lock:
        return plan_id in _requeue_intents


def clear_requeue(plan_id: str) -> None:
    """Forget ``plan_id``'s requeue intent.

    Called when the intent was delivered, and when it was dropped (the
    plan is still busy — a plan that is still working must not be put
    back into the queue).  Clearing an unknown plan (or an empty one)
    is a no-op.
    """
    if not plan_id:
        return
    with _lock:
        _requeue_intents.discard(plan_id)


def clear(plan_id: str) -> None:
    """Forget ``plan_id``'s stop record — and its requeue intent.

    Called when the plan is started afresh: the old stop must not gate
    the new run.  The requeue intent goes with it: the stop it belonged
    to is over, so an intent that outlived it would queue the plan at
    some later, unrelated moment.  Clearing an unknown plan (or an empty
    one) is a no-op.
    """
    if not plan_id:
        return
    with _lock:
        _records.pop(plan_id, None)
        _requeue_intents.discard(plan_id)


def _reset_for_tests() -> None:
    """Test hook — drops every stop record and every requeue intent."""
    with _lock:
        _records.clear()
        _requeue_intents.clear()