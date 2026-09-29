"""Per-tick in-flight file validator - owns the acquire / release /
in_flight_count public surface that the VP-022 verification point
imports.

Background
----------
The dispatcher refactor splits AutonomousAgent._run_async (a ~380 LOC
inline while True loop in backend/agent.py) into three orthogonal
collaborators:

  * SchedulingDispatcher - per-tick loop controller. See
    scheduling.dispatcher.
  * InFlightFileGuard (this module) - per-tick validator that
    enforces the file-modify contract (no two concurrent tasks may
    modify the same file at the same time) and surfaces the
    in-flight set so the dispatcher can pick the next eligible
    task without re-deriving the conflict map.
  * runtime_state - typed wrapper around the
    plan_verification.runtime_state SQLite JSON column.

The guard is checked once per tick BEFORE the dispatcher picks the
next task. acquire returns True on success and False when the file
is already in flight (caller must skip the task). release is called
when a task finishes (success, failure, or abort) so the file can
be picked up by another task. in_flight_count is an observability
hook used by the duplicate-by-title guard in _run_async and the
self-heal for files_to_modify conflicts.

Backward compatibility
----------------------
The earlier skeleton slice (commit 5adf23e1) named the class Guard.
Both names re-export the same class so existing
test_scheduling_skeleton imports keep resolving while VP-022 imports
the new name.
"""
from __future__ import annotations

from typing import Dict, Optional, Set

__all__ = ["InFlightFileGuard", "Guard"]


class InFlightFileGuard:
    """Coarse-grained per-tick validator placeholder.

    The skeleton pins the public method surface so VP-022 and any
    follow-up tasks have a stable target:

      * acquire(file_path) -> bool - register file_path as in-flight
        for the calling task. Returns False if the file is already
        in flight (caller must skip the task). Returns True on
        success.
      * release(file_path) -> bool - unregister file_path as
        in-flight. Idempotent: returns True even if the file was
        not tracked.
      * in_flight_count() -> int - observability hook returning the
        number of files currently in flight across all tasks.

    The methods are intentionally no-op stubs that satisfy the
    import-and-shape contract without committing to a final
    signature. Downstream tasks will accept the task repository,
    logger, and runtime_state handle as constructor kwargs.
    """

    def __init__(self) -> None:
        """Initialise an empty in-flight guard placeholder."""
        # file_path -> owner task_id (so release can be called by
        # any task, not just the owner; this matches the
        # contract observed in _run_async where the dispatcher
        # releases on the task's behalf).
        self._in_flight: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API required by VP-022
    # ------------------------------------------------------------------

    def acquire(self, file_path: str) -> bool:
        """Mark ``file_path`` as in-flight for the calling task.

        Returns ``True`` on success and ``False`` when ``file_path``
        is already tracked by another task. The caller must skip
        the task on ``False`` so the dispatcher can pick something
        else from the layer.
        """
        if file_path in self._in_flight:
            return False
        self._in_flight[file_path] = "owner"
        return True

    def release(self, file_path: str) -> bool:
        """Unregister ``file_path`` as in-flight.

        Idempotent: returns ``True`` whether or not ``file_path`` was
        previously tracked. The skeleton does not enforce owner
        matching - real wiring (when landed) will check that the
        caller is the recorded owner before clearing.
        """
        self._in_flight.pop(file_path, None)
        return True

    def in_flight_count(self) -> int:
        """Return the number of files currently in flight.

        Observability hook used by the duplicate-by-title guard and
        the files_to_modify self-heal in ``_run_async``.
        """
        return len(self._in_flight)


# Backward-compatible alias for the earlier skeleton slice (commit
# 5adf23e1) which named the class Guard. VP-022 imports
# InFlightFileGuard; test_scheduling_skeleton still imports Guard.
# Both must resolve to the same class.
Guard = InFlightFileGuard
