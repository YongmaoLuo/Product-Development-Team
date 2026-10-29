"""backend.plan_scheduler.store — the scheduler's own facts, written down.

Background
----------
The scheduler derives every *plan* fact from the ``plan_*`` tables on
every tick: which phase a plan is in, which one is running, which one
is waiting for a human.  What it cannot derive is its own bookkeeping,
and that is what the three ``scheduler_*`` tables hold:

* ``scheduler_queue`` — the order in which queued plans may run;
* ``scheduler_slot`` — which plans hold a concurrency slot;
* ``scheduler_command`` — the manual commands an operator issued.

They live in the same database as the plan facts on purpose: "is this
plan running?" and "is this plan in the queue?" have to be readable in
one transaction, otherwise they are two facts that can disagree.

This module is the **only** writer of those three tables.  That is not a
style choice — it is what makes the sequence number meaningful.  The
queue's ``seq`` is the tie-breaker that turns "who runs next" into a
question with exactly one answer, and a number is only meaningful if
there is exactly one place that hands it out.

The three promises
------------------
**One writer, one sequence.**  ``seq`` is ``MAX(seq) + 1`` over the
rows that are in the queue right now, computed *inside* the write
transaction.  Because the transaction starts with ``BEGIN IMMEDIATE``,
the write lock is taken before the maximum is read: two connections
that arrive at the same moment are serialised by SQLite, the second one
reads the sequence the first one committed, and the two numbers can
never be the same.

**All or nothing.**  Changing the queue and writing a slot are one fact
about one plan, so they belong in one transaction.  Every write here
runs inside a transaction — :meth:`SqliteSchedulerStore.transaction` —
and a body that raises takes the whole thing back, including the rows
that were written before the failure.

**A command lands once.**  A command is written down ``accepted`` the
moment the operator asks for it, and the worker writes the outcome and
the detail back when it has carried the command out.  Finishing a
command that is already finished changes nothing: the table records
*what happened*, and what happened happened once.

The queue holds one row per plan
--------------------------------
``scheduler_queue.plan_id`` is the primary key, so a plan is in the
queue once — enqueueing a plan that is already queued updates its
priority and leaves its place alone, rather than adding a second row.
(That is also why the queue's order is a *total* order: the key ends
in ``seq``, and two rows can never share one.)

The order
---------
The rows come back in the order :func:`plan_scheduler.queue.queue_key`
says they should — one comparator, shared by the panel, the admission
check and the restart recovery.  The store does not sort by anything of
its own: a second order would be a second answer to "who runs next",
and the operator's only way to find out which was which would be to
watch plans start in the wrong order.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .queue import queue_key

__all__ = [
    "PENDING_OUTCOME",
    "SqliteSchedulerStore",
]


#: The outcome a command carries between being *recorded* and being
#: *finished*.  It is the marker that says "the mailbox has this, the
#: worker has not answered yet" — and it is what makes a second
#: :meth:`~SqliteSchedulerStore.finish_command` a no-op: the row is no
#: longer pending, so the update matches nothing.
PENDING_OUTCOME = "accepted"


#: The columns of ``scheduler_queue``, in the order they are read.
_QUEUE_COLUMNS = (
    "plan_id",
    "seq",
    "priority",
    "promote_order",
    "enqueued_at",
    "updated_at",
)

#: The columns of ``scheduler_slot``, in the order they are read.
_SLOT_COLUMNS = ("plan_id", "acquired_at", "updated_at")

#: The columns of ``scheduler_command``, in the order they are read.
_COMMAND_COLUMNS = (
    "command_id",
    "plan_id",
    "action",
    "outcome",
    "detail",
    "created_at",
)


def _utcnow_iso() -> str:
    """The current UTC time, in the format the state database uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_dict(columns: tuple, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Turn a selected row into a plain dict.

    The columns are named here rather than taken from the row, so the
    result does not depend on the connection's ``row_factory`` — the
    store reads the same rows out of a connection that returns tuples
    and out of one that returns :class:`sqlite3.Row`.
    """
    if row is None:
        return None
    return dict(zip(columns, tuple(row)))


class SqliteSchedulerStore:
    """Read and write the scheduler's three tables.

    Every method takes the connection it works on, so one store can
    serve any number of connections and a caller keeps control of the
    connection's lifetime.  The connection is expected to be the one
    :func:`state_machine.db.connection.open` produces: autocommit mode
    (``isolation_level = None``), WAL, and ``busy_timeout = 5000``.

    The store keeps no state of its own — it does not cache a queue,
    hold a connection, or remember what it wrote.  Every fact it
    returns was read from the database just now, which is what lets the
    scheduler see a change another process made.
    """

    # ------------------------------------------------------------------
    # The transaction boundary
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Run the body inside one ``BEGIN IMMEDIATE`` transaction.

        ``BEGIN IMMEDIATE`` takes the write lock *up front*, before the
        body reads anything.  That is what makes the sequence safe: a
        deferred transaction would read the maximum, and only then try
        to upgrade to a write lock — so two writers could read the same
        maximum and hand out the same number.  With the lock held from
        the start, the second writer waits (SQLite's busy handler,
        inside ``busy_timeout``) and reads what the first one
        committed.

        A body that raises is rolled back — every row it wrote goes
        back, including the ones written before the failure — and the
        exception is re-raised unchanged.

        Calling this while a transaction is already open *joins* that
        transaction instead of starting a second one (SQLite refuses to
        nest).  The inner block then has no commit of its own: the
        outer transaction decides whether the work is kept.  That is
        what lets a caller put several of the store's writes into one
        fact.
        """
        if conn.in_transaction:
            # The caller already opened a transaction — join it.
            yield conn
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # A rollback can fail if the transaction is already in
                # a state that cannot be rolled back; the original
                # exception is the one that matters.
                pass
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # The queue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        priority: int = 0,
        now: Optional[str] = None,
    ) -> int:
        """Put a plan in the queue and return the number it was given.

        The number is ``MAX(seq) + 1`` over the plans that are queued
        right now, read inside the write transaction — so it is the
        next number, and it is never handed out twice.

        A plan that is already queued keeps its number and its place:
        the queue holds one row per plan, and a retried "enqueue this
        plan" must not push it to the back of the line.  Only the
        priority is updated, because that is the one thing an operator
        can change about a plan that is already waiting.

        Args:
            conn: the connection to write on.
            plan_id: the plan to queue.
            priority: the plan's priority.  The store records the
                number and has no opinion of its own about what it
                means — :func:`plan_scheduler.queue.queue_key` decides
                which way it sorts.
            now: the timestamp to record; the current UTC time when
                omitted.

        Returns:
            The sequence number the plan holds in the queue.
        """
        stamp = now or _utcnow_iso()
        with self.transaction(conn):
            queued = conn.execute(
                "SELECT seq FROM scheduler_queue WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if queued is not None:
                conn.execute(
                    "UPDATE scheduler_queue SET priority = ?, updated_at = ? "
                    "WHERE plan_id = ?",
                    (int(priority), stamp, plan_id),
                )
                return int(queued[0])

            highest = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM scheduler_queue"
            ).fetchone()
            seq = int(highest[0]) + 1
            conn.execute(
                "INSERT INTO scheduler_queue "
                "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (plan_id, seq, int(priority), stamp, stamp),
            )
            return seq

    def remove(self, conn: sqlite3.Connection, plan_id: str) -> bool:
        """Take a plan out of the queue.

        The number the plan held goes back into the pool: the next
        ``enqueue`` reads ``MAX(seq)`` over what is left, so the freed
        number is handed out again — and to exactly one plan, because
        the queue is read inside the write transaction.

        Args:
            conn: the connection to write on.
            plan_id: the plan to take out.

        Returns:
            ``True`` if the plan was in the queue, ``False`` if it was
            not.
        """
        with self.transaction(conn):
            cursor = conn.execute(
                "DELETE FROM scheduler_queue WHERE plan_id = ?", (plan_id,)
            )
            return cursor.rowcount > 0

    def promote(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        now: Optional[str] = None,
    ) -> Optional[int]:
        """Move a plan ahead of the plans that were never promoted.

        A promotion is recorded as a *decreasing* counter, so the most
        recent promotion has the smallest number and runs first — which
        is what makes "promote this plan" mean "this plan runs next",
        even when several plans were promoted.

        The number is ``COALESCE(MIN(promote_order), 0) - 1`` over the
        rows that have been promoted: the first promotion is ``-1``,
        the second ``-2``, and so on.

        Args:
            conn: the connection to write on.
            plan_id: the plan to promote.
            now: the timestamp to record; the current UTC time when
                omitted.

        Returns:
            The promotion number the plan was given, or ``None`` if the
            plan is not in the queue — there is nothing to promote.
        """
        stamp = now or _utcnow_iso()
        with self.transaction(conn):
            queued = conn.execute(
                "SELECT seq FROM scheduler_queue WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if queued is None:
                return None

            lowest = conn.execute(
                "SELECT COALESCE(MIN(promote_order), 0) FROM scheduler_queue "
                "WHERE promote_order IS NOT NULL"
            ).fetchone()[0]
            promote_order = int(lowest) - 1

            conn.execute(
                "UPDATE scheduler_queue SET promote_order = ?, updated_at = ? "
                "WHERE plan_id = ?",
                (promote_order, stamp, plan_id),
            )
            return promote_order

    def consume_promotion(
        self, conn: sqlite3.Connection, plan_id: str
    ) -> Optional[int]:
        """Use up a plan's promotion and return the number it had.

        A promotion is a one-shot.  Once the plan has run, the
        promotion has been spent: leaving it on the row would keep
        pushing that plan ahead of the queue, long after the reason for
        promoting it was gone.

        Args:
            conn: the connection to write on.
            plan_id: the plan whose promotion is spent.

        Returns:
            The promotion number that was cleared, or ``None`` if the
            plan is not in the queue or was never promoted.
        """
        with self.transaction(conn):
            row = conn.execute(
                "SELECT promote_order FROM scheduler_queue WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None or row[0] is None:
                return None

            conn.execute(
                "UPDATE scheduler_queue SET promote_order = NULL, updated_at = ? "
                "WHERE plan_id = ?",
                (_utcnow_iso(), plan_id),
            )
            return int(row[0])

    def list_queue(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Return the queue, in the order the shared comparator says.

        The order is :func:`plan_scheduler.queue.queue_key` — promoted
        plans first (the most recently promoted one first), then by
        priority, then by arrival.  The store sorts by that comparator
        and by nothing else, so the panel, the admission check and the
        restart recovery cannot disagree about who runs next.

        Args:
            conn: the connection to read on.

        Returns:
            The queued plans, each a dict of the row's columns, in
            queue order.  Empty when the queue is empty.
        """
        rows = conn.execute(
            "SELECT plan_id, seq, priority, promote_order, enqueued_at, updated_at "
            "FROM scheduler_queue"
        ).fetchall()
        queued = [dict(zip(_QUEUE_COLUMNS, tuple(row))) for row in rows]
        return sorted(queued, key=queue_key)

    # ------------------------------------------------------------------
    # The concurrency slots
    # ------------------------------------------------------------------

    def upsert_slot(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        now: Optional[str] = None,
    ) -> bool:
        """Record that a plan holds a concurrency slot.

        The slot says *when* the plan took it, and that is all it says
        — which plan is running is a plan fact, read from the plan
        tables.  Writing the slot a second time does not reset the
        clock: the eviction rule reads ``acquired_at``, and a plan that
        has been running for an hour must not get an hour more because
        its slot was written down twice.

        Args:
            conn: the connection to write on.
            plan_id: the plan that holds the slot.
            now: the timestamp to record; the current UTC time when
                omitted.

        Returns:
            ``True`` — the slot is held by the plan afterwards.
        """
        stamp = now or _utcnow_iso()
        with self.transaction(conn):
            conn.execute(
                "INSERT INTO scheduler_slot (plan_id, acquired_at, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET updated_at = excluded.updated_at",
                (plan_id, stamp, stamp),
            )
            return True

    def delete_slot(self, conn: sqlite3.Connection, plan_id: str) -> bool:
        """Give a plan's concurrency slot back.

        Args:
            conn: the connection to write on.
            plan_id: the plan that gives the slot back.

        Returns:
            ``True`` if the plan held a slot, ``False`` if it did not.
        """
        with self.transaction(conn):
            cursor = conn.execute(
                "DELETE FROM scheduler_slot WHERE plan_id = ?", (plan_id,)
            )
            return cursor.rowcount > 0

    def list_slots(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Return the held slots, oldest acquisition first.

        The order is by ``acquired_at`` and then by ``plan_id``, so two
        slots taken in the same second still come back in a fixed
        order — the eviction rule walks this list, and a rule that
        depended on the order SQLite happened to return would evict a
        different plan on every run.

        Args:
            conn: the connection to read on.

        Returns:
            The held slots, each a dict of the row's columns.
        """
        rows = conn.execute(
            "SELECT plan_id, acquired_at, updated_at FROM scheduler_slot "
            "ORDER BY acquired_at, plan_id"
        ).fetchall()
        return [dict(zip(_SLOT_COLUMNS, tuple(row))) for row in rows]

    # ------------------------------------------------------------------
    # The commands
    # ------------------------------------------------------------------

    def record_command(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        action: str,
        outcome: str = PENDING_OUTCOME,
        detail: Optional[str] = None,
        now: Optional[str] = None,
    ) -> int:
        """Write down a command the operator issued.

        The command is written the moment it is received, with the
        outcome it has *so far* — ``accepted``, meaning the mailbox has
        it and the worker has not answered yet.  That is what lets a
        reader tell "this was requested" apart from "this was carried
        out".

        Args:
            conn: the connection to write on.
            plan_id: the plan the command is about.
            action: what was asked for (``promote``, ``pause``, ...).
            outcome: the outcome recorded with the command; pending by
                default.
            detail: free-form detail recorded with the command.
            now: the timestamp to record; the current UTC time when
                omitted.

        Returns:
            The id of the command that was written.
        """
        stamp = now or _utcnow_iso()
        with self.transaction(conn):
            cursor = conn.execute(
                "INSERT INTO scheduler_command "
                "(plan_id, action, outcome, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (plan_id, action, outcome, detail, stamp),
            )
            return int(cursor.lastrowid)

    def finish_command(
        self,
        conn: sqlite3.Connection,
        command_id: int,
        outcome: str,
        detail: Optional[str] = None,
    ) -> bool:
        """Write the outcome of a command back, once.

        Only a command that is still pending is updated.  A worker that
        answers twice — because the command was written down again, or
        because the mailbox was drained again — changes nothing the
        second time: the table records what happened, and what happened
        happened once.

        A ``detail`` of ``None`` leaves the detail that is already there
        alone; finishing a command with an outcome is not a reason to
        erase the detail the command already carries.

        Args:
            conn: the connection to write on.
            command_id: the command to finish.
            outcome: the final outcome.
            detail: the detail to record; the existing detail is kept
                when omitted.

        Returns:
            ``True`` if this call finished the command, ``False`` if the
            command was already finished (or does not exist).
        """
        with self.transaction(conn):
            cursor = conn.execute(
                "UPDATE scheduler_command "
                "SET outcome = ?, detail = COALESCE(?, detail) "
                "WHERE command_id = ? AND outcome = ?",
                (outcome, detail, command_id, PENDING_OUTCOME),
            )
            return cursor.rowcount > 0

    def get_command(
        self, conn: sqlite3.Connection, command_id: int
    ) -> Optional[Dict[str, Any]]:
        """Return one command, or ``None`` if there is no such command.

        Args:
            conn: the connection to read on.
            command_id: the command to read.

        Returns:
            The command as a dict of the row's columns, or ``None``.
        """
        row = conn.execute(
            "SELECT command_id, plan_id, action, outcome, detail, created_at "
            "FROM scheduler_command WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        return _as_dict(_COMMAND_COLUMNS, row)
