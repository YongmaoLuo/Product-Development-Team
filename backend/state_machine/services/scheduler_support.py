r"""Scheduler-support service: thin wrapper around the three repositories.

This module implements :class:`SchedulerSupport`, the bug-4 anchor for
the state-machine refactor.  The class is a deliberately narrow
facade:

  * Reads (snapshots) are derived on demand from
    :class:`RoutingRepository`, :class:`ExecutionRepository`, and
    :class:`VerificationRepository`.
  * Writes (``update_next_run_at`` / ``update_card_state``) delegate
    to the corresponding :class:`ExecutionRepository` method so the
    same IMMEDIATE-txn skeleton that the repository tests pin is
    reused unchanged.

The contract pinned by ``test_scheduler_support.py``:

  * No instance attribute matching ``_pending_*``, ``_queue``, or
    ``_cache`` (reflection-based guard).
  * ``decide_tick`` re-reads the tables on every call — an external
    ``UPDATE plan_execution SET next_run_at = ?`` is visible to the
    very next tick with no cache invalidation step.
  * ``decide_tick`` returns ``[]`` (not ``None``, not an exception)
    when no plan is due.
  * Writes commit before the method returns — a crash mid-decision
    never leaves a half-written column.

Why a separate module
---------------------
The scheduler logic used to live inside ``backend/agent.py`` and held
in-memory queues / caches (``self._pending_*``, ``self._queue``).  This
class is the explicit replacement for that legacy state.  Keeping it
in its own module makes the "no cache" invariant easy to audit
(``grep -n '_pending_\|_queue\|_cache' backend/state_machine/services/``
returns zero hits) and lets ``test_scheduler_has_no_uncommitted_decision_fields``
hold the line going forward.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from state_machine.repositories.execution_repository import (
    ExecutionRepository,
)
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

__all__ = ["SchedulerSupport"]


#: Routing phases where the scheduler should pick a plan up.  Plans
#: outside this set (e.g. ``prd_review``, ``verification_running``)
#: are owned by other workers and MUST NOT enter ``decide_tick()``.
#: Kept as a module-level frozenset so reflection / lint can audit it
#: easily and the value is not "owned" by any single instance.
#:
#: 2026-09-17 (schema v5): renamed from ``_SCHEDULABLE_STAGES`` and
#: switched to the phase vocabulary, so this set now speaks the same
#: language as ``plan_routing.current_phase``.
#:
#: ``decide_tick`` builds its SQL ``IN`` list FROM this set rather than
#: repeating the values.  Before v5 it repeated them, and the two
#: drifted: ``tasks_queued`` was added here on 2026-09-15 but the query
#: kept its hardcoded ``IN ('tasks_ready', 'executing')``, so a queued
#: plan was invisible to the scheduler and the ``queued`` gate never
#: auto-started anything.  The assertion below is the structural fix —
#: there is now exactly one place the values live.
_SCHEDULABLE_PHASES: frozenset[str] = frozenset(
    # 2026-09-15: ``queued`` is the scheduler's inbox —
    # only plans the operator explicitly queued are auto-started.
    # ``ready`` stays in the set for backward compatibility with plans
    # already parked there (a ``ready`` plan is the MANUAL gate: the
    # operator asks, the assistant reports, the operator authorizes;
    # see ``_run_auto_verification_loop``/``/execution/{id}/start``).
    {"ready", "queued", "executing"}
)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix.

    Mirrors the helper used in the repository layer so the
    lexicographic ``WHERE next_run_at <= ?`` comparison in
    :meth:`SchedulerSupport.decide_tick` lines up with the value
    written by :meth:`ExecutionRepository.update_next_run_at`.
    """
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class SchedulerSupport:
    """Bug-4 anchor — re-read everything on every tick.

    The scheduler's state lives ONLY in SQLite.  No in-memory
    queue, no cached ``next_run_at``, no memoised routing decision.
    Every ``decide_tick`` call re-runs the SELECT and re-evaluates
    the candidate set from scratch.

    Parameters
    ----------
    conn:
        An open autocommit-mode SQLite connection produced by
        :func:`state_machine.db.connection.open`.  Held for
        read-only snapshot queries that the repositories do not
        already expose (e.g. the JOIN in ``decide_tick``).
    exec_repo:
        Bound :class:`ExecutionRepository` — used for
        ``update_next_run_at`` / ``update_card_state`` writes
        (so the same IMMEDIATE-txn skeleton as the rest of the
        executor path is reused) and for ``summary`` reads.
    routing_repo:
        Bound :class:`RoutingRepository` — used to filter out
        non-schedulable plans (the ``plan_routing.stage`` column
        is the gating dimension).
    verification_repo:
        Bound :class:`VerificationRepository` — used so a plan
        that already has a running verification round does not
        re-enter the execution queue.

    Notes
    -----
    The instance MUST NOT hold any ``_pending_*`` / ``_queue`` /
    ``_cache`` attribute; this is enforced by
    ``test_scheduler_has_no_uncommitted_decision_fields``.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        exec_repo: ExecutionRepository,
        routing_repo: RoutingRepository,
        verification_repo: VerificationRepository,
    ) -> None:
        self._conn = conn
        self._exec_repo = exec_repo
        self._routing_repo = routing_repo
        self._verification_repo = verification_repo

    # ------------------------------------------------------------------
    # Snapshot accessors (read-only)
    # ------------------------------------------------------------------

    def next_run(self, plan_id: str) -> Optional[str]:
        """Return ``plan_execution.next_run_at`` for ``plan_id``.

        Mirrors :meth:`RoutingRepository.current` — missing row →
        ``None``, present row → the raw ISO-8601 string (the column
        is stored as text and compared lexicographically).
        """
        cur = self._conn.execute(
            "SELECT next_run_at FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        value = row[0]
        return value  # may be None when the column is NULL

    def card_state(self, plan_id: str) -> dict[str, Any]:
        """Return the parsed ``plan_execution.card_state`` for ``plan_id``.

        A missing row or an empty / NULL JSON column returns ``{}``
        so callers (the dispatcher loop, the UI card view) get a
        plain dict they can index without a None-guard.  A corrupt
        JSON column is treated the same way — the next
        :meth:`update_card_state` call overwrites it cleanly.
        """
        cur = self._conn.execute(
            "SELECT card_state FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {}
        raw = row[0]
        if raw is None or raw == "":
            return {}
        import json

        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return value

    # ------------------------------------------------------------------
    # Tick (the bug-4 surface)
    # ------------------------------------------------------------------

    def decide_tick(self) -> list[str]:
        """Return the plan_ids whose ``next_run_at`` is overdue.

        Algorithm (single SELECT, no in-memory accumulation):

          1. ``JOIN plan_routing ↔ plan_execution`` on ``plan_id``.
          2. Filter:

             * ``plan_routing.current_phase`` ∈
               :data:`_SCHEDULABLE_PHASES` (plans owned by another
               worker — e.g. ``verification_running``, ``prd_review`` —
               are excluded so the executor never preempts a verifier).
             * ``plan_execution.next_run_at`` is non-NULL AND
               lexicographically ≤ current UTC time (i.e. due).
             * No row in ``plan_verification`` with
               ``verification_status = 'running'`` (a running
               verification round means the plan is owned by the
               verification worker, even if ``next_run_at`` is
               overdue).

          3. Return the ``plan_id`` column as a list.

        Crucially, this method does NOT cache the result.  The very
        next call re-runs the same SELECT, so an external
        ``UPDATE plan_execution SET next_run_at = ?`` (or any
        other column touched by the gating filter) is visible on
        the next call with no cache invalidation step.  This is
        the bug-4 anchor.
        """
        now_iso = _now_iso()
        # The ``IN`` list is generated from :data:`_SCHEDULABLE_PHASES`
        # so the query and the constant cannot disagree (see the
        # constant's docstring — they did, and a queued plan was
        # unschedulable as a result).
        phase_placeholders = ", ".join("?" for _ in _SCHEDULABLE_PHASES)
        cur = self._conn.execute(
            "SELECT e.plan_id "
            "FROM plan_execution e "
            "JOIN plan_routing r ON r.plan_id = e.plan_id "
            "LEFT JOIN plan_verification v "
            "  ON v.plan_id = e.plan_id "
            "  AND v.verification_status = 'running' "
            "WHERE e.next_run_at IS NOT NULL "
            "  AND e.next_run_at <= ? "
            f"  AND r.current_phase IN ({phase_placeholders}) "
            "  AND v.plan_id IS NULL "
            "ORDER BY e.next_run_at ASC",
            (now_iso, *sorted(_SCHEDULABLE_PHASES)),
        )
        return [row[0] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Write API — IMMEDIATE-only via the repository
    # ------------------------------------------------------------------

    def update_next_run_at(
        self, plan_id: str, next_run_at: Optional[str]
    ) -> None:
        """Schedule the next run for ``plan_id``.

        Delegates to :meth:`ExecutionRepository.update_next_run_at`,
        which wraps the write in ``BEGIN IMMEDIATE → COMMIT`` so a
        crash mid-decision never leaves a half-written column.
        ``None`` clears the column (the scheduler treats NULL as
        "no scheduled next run").
        """
        self._exec_repo.update_next_run_at(plan_id, next_run_at)

    def update_card_state(
        self, plan_id: str, card_state: dict[str, Any]
    ) -> None:
        """Persist ``card_state`` for the UI / dispatcher view.

        Delegates to :meth:`ExecutionRepository.update_card_state`
        so the same IMMEDIATE-txn skeleton + JSON encoding as the
        rest of the executor path is reused unchanged.
        """
        self._exec_repo.update_card_state(plan_id, card_state)