"""Process-local cooperative cancellation for a plan's verification round.

2026-09-15: ``POST /api/verification/{id}/stop`` must
actually STOP the work — previously it only CAS'd the routing row to
``verification_idle`` and updated the in-memory status, while the
verification thread kept running: the in-flight VP sub-agent stayed
alive (burning tokens and CPU) and the thread carried on into the next
round, writing stage/state AFTER the operator's stop. That is what made
a stopped plan look wedged and re-blocked ``/start`` with
``409 stage_mismatch``.

The stop path now does three things:

1. **Hard kill** — ``_cleanup_dead_verification_processes`` SIGTERMs the
   registered sub-agent subprocesses and sweeps orphaned pytest/bash
   children (see ``server.py``).
2. **Cooperative cancel** — ``request_cancel(plan_id)`` flips the flag
   here; the verification executor skips every not-yet-started VP and
   converts an interrupted in-flight attempt to SKIPPED, and the
   auto-loop exits instead of running the judgment / repair chain.
3. **No post-stop writes** — with the loop exited, the round cannot
   re-stamp the routing stage the operator just parked.

The registry is intentionally process-local and tiny: one deployment,
one server process, and the flag's lifetime is a single round (cleared
by ``/start`` and by the post-repair re-entry).
"""

from __future__ import annotations

import threading
from typing import List, Optional, Set

_lock = threading.Lock()
_cancelled: Set[str] = set()


class VerificationCancelled(RuntimeError):
    """The operator stopped this plan while an LLM call was in flight.

    2026-10-22. A dispatch that the ``/stop`` endpoint killed used to
    look exactly like a dispatch that failed: the subprocess died, the
    read loop came back empty, and the caller walked the ordinary
    failure path — retry it, record it as a failure, and (for the
    repair-task generation) manufacture a repair task out of a round the
    operator had just cancelled.

    That is the wrong answer twice over. A stopped round is not a failed
    round: it must not be retried (a stop was requested),
    it must not leave a failure record behind (there is nothing to
    triage), and it must not produce repair tasks (the plan is paused,
    not broken).

    So the cancellation is raised as its own type, from the place that
    can see it — the dispatch that was killed — and every layer above
    it either re-raises it untouched or treats it as "cancelled". The
    type lives here, next to ``is_cancelled``, because the flag it is
    derived from lives here: a caller that wants to ask "was this
    cancelled?" needs the flag and the exception to come from the same
    place, or the two can drift.

    It is a :class:`RuntimeError` subclass so that a caller written
    before this type existed still catches it in a generic
    ``except Exception`` — it simply must not *convert* it, and every
    layer on the path does exactly that.
    """

    def __init__(self, plan_id: str, message: Optional[str] = None):
        self.plan_id = plan_id or ""
        super().__init__(
            message
            or (
                f"verification cancelled for plan {self.plan_id!r}: "
                "the operator stopped this round"
            )
        )

    def __str__(self) -> str:
        return super().__str__()


def request_cancel(plan_id: str) -> None:
    """Mark ``plan_id``'s verification round as cancelled."""
    if not plan_id:
        return
    with _lock:
        _cancelled.add(plan_id)


def clear(plan_id: str) -> None:
    """Drop the cancel flag — called when a fresh round starts."""
    if not plan_id:
        return
    with _lock:
        _cancelled.discard(plan_id)


def is_cancelled(plan_id: str) -> bool:
    """True when the operator has stopped this plan's round."""
    if not plan_id:
        return False
    with _lock:
        return plan_id in _cancelled


def cancelled_plans() -> List[str]:
    """Snapshot of the currently-cancelled plan ids (diagnostics/tests)."""
    with _lock:
        return sorted(_cancelled)


def _reset_for_tests() -> None:
    """Test hook — clears every flag."""
    with _lock:
        _cancelled.clear()
