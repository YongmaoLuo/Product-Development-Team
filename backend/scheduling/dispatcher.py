"""Per-tick loop controller - owns the run_until_done / dispatch_next
public surface that the VP-022 verification point imports.

Background
----------
The dispatcher refactor splits AutonomousAgent._run_async (a ~380 LOC
inline while True loop in backend/agent.py) into three orthogonal
collaborators:

  * guard - coarse-grained validator (cycle, same-id loop,
    file-modify contract) called once per tick before the dispatcher
    picks the next task. See scheduling.guard.
  * SchedulingDispatcher (this module) - per-tick loop controller.
    Owns run_until_done (the entry point that drives the layer
    rebuild + next-task-pick loop until the queue drains) and
    dispatch_next (the per-iteration "given the current layer graph
    and runtime state, return the next schedulable task or signal
    completion").
  * runtime_state - typed wrapper around the
    plan_verification.runtime_state SQLite JSON column (already
    landed as backend/runtime_state.py).

Backward compatibility
----------------------
The earlier skeleton slice (commit 5adf23e1) named the class
Dispatcher. Both names re-export the same class so existing
test_scheduling_skeleton imports keep resolving while VP-022
imports the new name.
"""
from __future__ import annotations

from typing import Any, List, Optional

__all__ = ["SchedulingDispatcher", "Dispatcher"]


class SchedulingDispatcher:
    """Per-tick loop controller placeholder.

    The skeleton pins the public method surface so VP-022 and any
    follow-up tasks have a stable target:

      * run_until_done - drive the dispatcher loop to completion
        (drain the task queue). Real wiring lands later.
      * dispatch_next - single-step variant of the loop body:
        rebuild the active task set and return the next eligible
        task (or None when the queue is empty).
    """

    def __init__(self) -> None:
        """Initialise an empty dispatcher placeholder."""
        self._done: bool = False
        self._queue: List[Any] = []

    # ------------------------------------------------------------------
    # Public API required by VP-022
    # ------------------------------------------------------------------

    def run_until_done(self) -> None:
        """Drain the queued tasks until none remain.

        Skeleton placeholder - real implementation will iterate over
        dispatch_next results and invoke the executor for each
        returned task. This stub merely drives dispatch_next to
        completion so the loop exits cleanly.
        """
        while self.dispatch_next() is not None:
            pass
        self._done = True

    def dispatch_next(self) -> Optional[Any]:
        """Return the next schedulable task, or None when drained.

        Skeleton placeholder - real implementation will rebuild the
        layer graph from the task repository and return the first
        non-terminal task whose depends_on is satisfied. This stub
        pops from the local queue (FIFO) so callers can drive
        run_until_done with test fixtures.
        """
        if not self._queue:
            return None
        return self._queue.pop(0)


# Backward-compatible alias for the earlier skeleton slice (commit
# 5adf23e1) which named the class Dispatcher. VP-022 imports
# SchedulingDispatcher; test_scheduling_skeleton still imports
# Dispatcher. Both must resolve to the same class.
Dispatcher = SchedulingDispatcher
