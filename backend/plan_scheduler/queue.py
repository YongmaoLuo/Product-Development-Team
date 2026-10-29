"""The queue's order — one comparator, shared by every reader.

Three places have to agree on which queued plan runs next: the
admission check that picks a plan to start, the panel that shows the
queue to the operator, and the recovery that runs after a restart.  If
each of them sorted on its own, the panel would show one order while
admission ran another — and the operator's only way to find out which
was which would be to watch plans start in the wrong order.

So there is exactly one order, and it is a **pure function of the
row**: :func:`queue_key` reads ``promote_order``, ``priority`` and
``seq`` and returns the key a row is sorted by.  The same rows always
produce the same order, in this process and in the next one — a restart
does not rearrange the queue, because the order lives in the persisted
columns and not in the memory of the process that happened to be
running.

The key mirrors the order the schema documents for the queue table
(``(promote_order IS NULL), promote_order, priority, seq``):

* the first element is ``True`` for a plan that was never promoted and
  ``False`` for one that was, so promoted plans sort ahead of the
  unpromoted ones;
* the second is the ``promote_order`` itself — a promotion is recorded
  as a decreasing counter, so the most recent promotion has the
  smallest number and runs first;
* the third is the plan's ``priority``;
* the fourth is the ``seq`` — the unique, monotonic tie-breaker that
  makes the order total, and that makes a queue with nothing promoted
  and nothing prioritised a plain FIFO.

Because ``seq`` is unique, two different rows never share a key: the
order is total, and "who runs next" always has exactly one answer.

The writes
----------
The comparator above reads the queue; the four operations at the bottom
of this module change it — :func:`enqueue` puts a plan in the queue,
:func:`remove` takes it out again, :func:`promote` moves it to the
front, and :func:`consume_promotion` spends the promotion that moved it.

They are thin wrappers over
:class:`plan_scheduler.store.SqliteSchedulerStore`, which is the only
writer of the queue table.  The wrappers exist for one reason: the
store answers "there was nothing to promote" with ``None``, and "the
plan was never promoted" and "the plan is not in the queue" are
different answers — the second one is an error the caller has to hear
about, so it is raised as a :class:`KeyError` here and turned into a
conflict or a not-found by the commands layer above.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - imported for the type only
    from .store import SqliteSchedulerStore

#: The key a queue row is ordered by: promoted-first, then by promotion
#: order, then by priority, then by arrival.
QueueKey = Tuple[bool, int, int, int]

#: A queue row.  It is either a mapping (a row the caller assembled) or
#: a ``sqlite3.Row`` (a row read out of the queue table); both are read
#: the same way, so the comparator does not care which it was handed.
QueueRow = Any


def _field(row: QueueRow, name: str, default: Any) -> Any:
    """Read ``name`` out of ``row``, falling back to ``default``.

    A row is read the same way whatever shape it has: a mapping answers
    ``get``, a ``sqlite3.Row`` answers ``[]`` and raises when the column
    is not part of the row.  A value that is present but ``NULL`` reads
    as the default — for the queue's purposes a ``NULL`` column and an
    absent one mean the same thing.
    """
    getter = getattr(row, "get", None)
    if callable(getter):
        value = getter(name, default)
    else:
        try:
            value = row[name]
        except (KeyError, IndexError):
            value = default
    return default if value is None else value


def queue_key(row: QueueRow) -> QueueKey:
    """Return the key a queue row is ordered by.

    The key is meant to be handed straight to :func:`sorted` as
    ``key=queue_key``.  It is a pure function: the same row always
    produces the same key, which is what lets the admission check, the
    operator panel and the restart recovery share one order without
    agreeing on anything else.

    A row that was never promoted (``promote_order`` is ``NULL``) sorts
    behind every promoted row; a promoted row sorts ahead of them, and
    the more recently it was promoted — the smaller its
    ``promote_order`` — the further ahead it sorts.  Within the same
    promotion state the plan's ``priority`` decides, and ``seq`` — the
    unique tie-breaker — decides last, which leaves an untouched queue
    in plain arrival order.
    """
    promote_order = _field(row, "promote_order", None)
    priority = int(_field(row, "priority", 0))
    seq = int(_field(row, "seq", 0))

    if promote_order is None:
        # Never promoted: the first element sorts it behind every
        # promoted row, and the second is a neutral 0 — promoted rows
        # are compared among themselves by their own promote_order.
        return (True, 0, priority, seq)
    return (False, int(promote_order), priority, seq)


# ---------------------------------------------------------------------------
# The queue's writes
# ---------------------------------------------------------------------------


def _store() -> "SqliteSchedulerStore":
    """The store the queue's writes go through.

    The store is imported here rather than at the top of the module:
    :mod:`plan_scheduler.store` imports *this* module for
    :func:`queue_key`, so importing it at the top of this module would
    be a cycle.

    The store holds no state of its own — no cached queue, no
    remembered writes — so there is nothing to share between callers and
    a fresh one is as good as a shared one.
    """
    from .store import SqliteSchedulerStore

    return SqliteSchedulerStore()


def _queued_row(
    store: "SqliteSchedulerStore", conn: sqlite3.Connection, plan_id: str
) -> Optional[Dict[str, Any]]:
    """The queue row of ``plan_id``, or ``None`` if it is not queued.

    Read through the store's own read path, so the row that is found
    here is the row the store would hand out — same columns, same
    shape.
    """
    for row in store.list_queue(conn):
        if row["plan_id"] == plan_id:
            return row
    return None


def enqueue(
    conn: sqlite3.Connection, plan_id: str, priority: int = 0
) -> int:
    """Put a plan in the queue and return the number it was given.

    The number is handed out by the store inside the write transaction,
    so it is never given to two plans: it is the tie-breaker that makes
    the queue's order total.

    A plan that is already queued keeps its number and its place — the
    queue holds one row per plan, and a retried "enqueue this plan"
    must not push it to the back of the line.  Only the priority is
    updated, because that is the one thing about a waiting plan an
    operator may change.

    Args:
        conn: the connection to write on.
        plan_id: the plan to queue.
        priority: the plan's priority.  The number is recorded as it
            was given; :func:`queue_key` decides which way it sorts.

    Returns:
        The sequence number the plan holds in the queue.
    """
    return _store().enqueue(conn, plan_id, priority)


def remove(conn: sqlite3.Connection, plan_id: str) -> bool:
    """Take a plan out of the queue.

    A plan that is not queued is not an error: the queue may have
    changed under the caller — the plan may already have run — and a
    removal that matches nothing leaves the queue as it was.

    Args:
        conn: the connection to write on.
        plan_id: the plan to take out.

    Returns:
        ``True`` if the plan was in the queue and was taken out,
        ``False`` if it was not there.
    """
    return _store().remove(conn, plan_id)


def promote(conn: sqlite3.Connection, plan_id: str) -> int:
    """Move a plan ahead of the plans that were never promoted.

    A promotion is recorded as a *decreasing* counter, so the most
    recent promotion has the smallest number and runs first — which is
    what makes "promote this plan" mean "this plan runs next", even
    when several plans were promoted.  The number is
    ``COALESCE(MIN(promote_order), 0) - 1``: the first promotion is
    ``-1``, the second ``-2``, and so on.

    Args:
        conn: the connection to write on.
        plan_id: the plan to promote.

    Returns:
        The promotion number the plan was given.

    Raises:
        KeyError: if the plan is not in the queue.  There is nothing to
            promote, and the caller asked for a plan that is not there —
            the commands layer turns this into a conflict or a
            not-found.
    """
    promote_order = _store().promote(conn, plan_id)
    if promote_order is None:
        raise KeyError(plan_id)
    return promote_order


def consume_promotion(conn: sqlite3.Connection, plan_id: str) -> Optional[int]:
    """Spend a plan's promotion and return the number it had.

    A promotion is a one-shot.  Once the plan has run, the promotion
    has been spent: leaving it on the row would keep pushing that plan
    ahead of the queue, long after the reason for promoting it was
    gone.  What is left is the row the plan had before it was promoted
    — sorted by its priority and its arrival again.

    Args:
        conn: the connection to write on.
        plan_id: the plan whose promotion is spent.

    Returns:
        The promotion number that was cleared, or ``None`` if the plan
        was in the queue but had never been promoted — there was
        nothing to spend.

    Raises:
        KeyError: if the plan is not in the queue.  A promotion belongs
            to a queue row, and there is no row to spend it on.
    """
    store = _store()
    with store.transaction(conn):
        if _queued_row(store, conn, plan_id) is None:
            raise KeyError(plan_id)
        return store.consume_promotion(conn, plan_id)


__all__ = [
    "QueueKey",
    "QueueRow",
    "consume_promotion",
    "enqueue",
    "promote",
    "queue_key",
    "remove",
]