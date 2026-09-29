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
from typing import List, Set

_lock = threading.Lock()
_cancelled: Set[str] = set()


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
