"""Unit tests: the queue's four write operations.

:func:`plan_scheduler.queue.queue_key` decides *which* queued plan runs
next; this file pins the four writes that put a plan into the queue, take
it out again, move it to the front, and spend the promotion that moved
it.

What this file holds down:

1. **Arrival is numbered.** Plans that join the queue get 1, 2, 3 — the
   numbers are what make the order total, so "who runs next" always has
   one answer.
2. **A repeated enqueue is not a second arrival.** The queue holds one
   row per plan, so re-enqueueing a queued plan keeps its number and its
   place.
3. **A promotion counts down.** The first promotion is ``-1``, the next
   ``-2``: the most recent promotion has the smallest number and runs
   first.
4. **A promotion is a one-shot.** Spending it clears the promotion, and
   the plan falls back to its priority and its arrival.
5. **Removing a plan that is not queued is not an error.** The queue may
   have changed under the caller; a removal that matches nothing quietly
   does nothing.

These tests run against a real SQLite database, because the contract
they hold is a *storage* contract: what survives is what is in the
table, not what a mock was told to expect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List

import pytest

from plan_scheduler.queue import consume_promotion, enqueue, promote, remove


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler's tables."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    migrate(conn)
    return conn


@pytest.fixture()
def conn(tmp_path: Path):
    """A migrated connection, closed when the test is over."""
    connection = _open_migrated(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


def _rows(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    """The queue, read back through the store, in queue order."""
    from plan_scheduler.store import SqliteSchedulerStore

    return SqliteSchedulerStore().list_queue(conn)


def _plan_ids(rows: List[Dict[str, object]]) -> List[str]:
    """The plan ids of ``rows``, in the order they came out."""
    return [str(row["plan_id"]) for row in rows]


def _row_of(rows: List[Dict[str, object]], plan_id: str) -> Dict[str, object]:
    """The row of ``plan_id`` in ``rows``."""
    for row in rows:
        if row["plan_id"] == plan_id:
            return row
    raise AssertionError(f"{plan_id!r} is not in the queue: {rows!r}")


# ---------------------------------------------------------------------------
# 1. Arrival is numbered
# ---------------------------------------------------------------------------


def test_enqueue_assigns_increasing_seq(conn: sqlite3.Connection) -> None:
    """Three plans that join the queue get the numbers 1, 2 and 3.

    The number is the queue's tie-breaker: two plans may never claim the
    same one, or "who runs next" would have two answers.  It is handed
    out by the store inside the write transaction, so the second plan
    cannot read the maximum the first one has not committed yet.
    """
    assert enqueue(conn, "p1") == 1
    assert enqueue(conn, "p2") == 2
    assert enqueue(conn, "p3") == 3

    rows = _rows(conn)
    assert [int(row["seq"]) for row in rows] == [1, 2, 3]
    assert _plan_ids(rows) == ["p1", "p2", "p3"]


# ---------------------------------------------------------------------------
# 2. A repeated enqueue is not a second arrival
# ---------------------------------------------------------------------------


def test_enqueue_idempotent_keeps_seq(conn: sqlite3.Connection) -> None:
    """A plan that is already queued keeps its number and its place.

    The queue holds one row per plan (``plan_id`` is the primary key),
    so a second ``enqueue`` of the same plan cannot add a second row —
    and it may not push the plan to the back of the line either.  A
    retried command that re-enqueues a plan has to leave the queue
    exactly as it found it, apart from the priority the caller gave.
    """
    first = enqueue(conn, "p1")
    again = enqueue(conn, "p1", priority=-1)

    rows = _rows(conn)
    assert len(rows) == 1, "a plan may only be in the queue once"
    assert again == first == 1, "the queued plan kept its number"
    assert int(rows[0]["seq"]) == 1, "the number moved"
    assert rows[0]["promote_order"] is None, "a repeated enqueue promoted the plan"
    assert int(rows[0]["priority"]) == -1, "the priority was not updated"


def test_enqueue_of_a_queued_plan_keeps_its_arrival_time(
    conn: sqlite3.Connection,
) -> None:
    """A repeated enqueue refreshes the row without re-arranging it.

    The arrival time is when the plan joined the queue; a repeated
    enqueue is not a new arrival, so the timestamp that says "when did
    this plan arrive" must not be rewritten by it.
    """
    enqueue(conn, "p1")
    arrived_at = _row_of(_rows(conn), "p1")["enqueued_at"]

    enqueue(conn, "p1")

    assert _row_of(_rows(conn), "p1")["enqueued_at"] == arrived_at


# ---------------------------------------------------------------------------
# 3. A promotion counts down
# ---------------------------------------------------------------------------


def test_promote_sets_min_minus_one(conn: sqlite3.Connection) -> None:
    """Promotions count down: the first is -1, the next -2.

    The counter is ``COALESCE(MIN(promote_order), 0) - 1`` over the
    rows that have been promoted, so the most recent promotion has the
    *smallest* number — and the comparator sorts promoted rows by that
    number ascending, which puts the most recent promotion first.
    """
    enqueue(conn, "p1")
    enqueue(conn, "p2")

    assert promote(conn, "p2") == -1, "the first promotion is -1"
    assert _plan_ids(_rows(conn)) == ["p2", "p1"], "the promoted plan runs first"

    assert promote(conn, "p1") == -2, "the promotion counter counts down"
    assert _plan_ids(_rows(conn)) == ["p1", "p2"], "the later promotion runs first"


def test_promote_missing_plan_raises_key_error(conn: sqlite3.Connection) -> None:
    """Promoting a plan that is not queued is an error, not a no-op.

    The store answers "there was nothing to promote" with ``None``,
    which is not an answer the caller can act on: the plan that was
    asked for is not in the queue.  The commands layer turns this into
    a conflict or a not-found, so the error has to be raised here.
    """
    with pytest.raises(KeyError):
        promote(conn, "not-queued")


# ---------------------------------------------------------------------------
# 4. A promotion is a one-shot
# ---------------------------------------------------------------------------


def test_consume_promotion_clears_order(conn: sqlite3.Connection) -> None:
    """Spending a promotion clears it and the plan falls back into line.

    A promotion says "this plan runs next".  Once the plan has run, the
    promotion has been used up — leaving it on the row would keep
    pushing that plan ahead of the queue, long after the reason for
    promoting it was gone.
    """
    enqueue(conn, "p1")
    enqueue(conn, "p2")

    assert promote(conn, "p2") == -1
    assert _plan_ids(_rows(conn)) == ["p2", "p1"]

    assert consume_promotion(conn, "p2") == -1, "the spent promotion is returned"

    rows = _rows(conn)
    assert _row_of(rows, "p2")["promote_order"] is None, "the promotion was not cleared"
    assert _plan_ids(rows) == ["p1", "p2"], "the plan fell back into arrival order"


def test_consume_promotion_falls_back_to_priority(conn: sqlite3.Connection) -> None:
    """After the promotion is spent, the priority decides again.

    The fallback is the comparator's, not this function's: the plan
    goes back to being sorted by its priority and then by its arrival,
    which is where it was before it was promoted.
    """
    enqueue(conn, "p1")
    enqueue(conn, "p2")
    promote(conn, "p2")

    consume_promotion(conn, "p2")

    enqueue(conn, "p3", priority=-1)
    assert _plan_ids(_rows(conn)) == ["p3", "p1", "p2"]


def test_consume_promotion_of_an_unpromoted_plan_is_nothing(
    conn: sqlite3.Connection,
) -> None:
    """A plan that was never promoted has no promotion to spend.

    This is not an error: the plan is in the queue, there was simply
    nothing to spend, and the caller gets ``None`` to say so.
    """
    enqueue(conn, "p1")

    assert consume_promotion(conn, "p1") is None
    assert _row_of(_rows(conn), "p1")["promote_order"] is None


def test_consume_promotion_missing_plan_raises_key_error(
    conn: sqlite3.Connection,
) -> None:
    """Spending the promotion of a plan that is not queued is an error."""
    with pytest.raises(KeyError):
        consume_promotion(conn, "not-queued")


# ---------------------------------------------------------------------------
# 5. Removing a plan that is not queued is not an error
# ---------------------------------------------------------------------------


def test_remove_missing_is_noop(conn: sqlite3.Connection) -> None:
    """Removing a plan that is not queued changes nothing and raises nothing.

    The queue may have changed under the caller — the plan may already
    have run.  A removal that matches nothing is not a failure: there
    was nothing to remove, and the queue is left as it was.
    """
    enqueue(conn, "p1")

    assert remove(conn, "not-queued") is False

    assert _plan_ids(_rows(conn)) == ["p1"], "the queue changed"


def test_remove_takes_a_queued_plan_out(conn: sqlite3.Connection) -> None:
    """A plan that is removed leaves the queue, and the others stay."""
    enqueue(conn, "p1")
    enqueue(conn, "p2")

    assert remove(conn, "p1") is True

    assert _plan_ids(_rows(conn)) == ["p2"]


# ---------------------------------------------------------------------------
# The priority is recorded as it was given
# ---------------------------------------------------------------------------


def test_priority_is_kept_verbatim(conn: sqlite3.Connection) -> None:
    """A priority of 0, a negative one and a very large one are all kept.

    The store records the number and has no opinion of its own about
    what it means; the comparator decides which way it sorts.  Storing
    it as it was given is what lets an operator use any scale they like.
    """
    enqueue(conn, "zero", priority=0)
    enqueue(conn, "negative", priority=-7)
    enqueue(conn, "huge", priority=2**62)

    priorities = {
        str(row["plan_id"]): int(row["priority"]) for row in _rows(conn)
    }
    assert priorities == {"zero": 0, "negative": -7, "huge": 2**62}


def test_enqueue_defaults_to_priority_zero(conn: sqlite3.Connection) -> None:
    """A plan that is queued without a priority is queued with 0."""
    enqueue(conn, "p1")

    row = _row_of(_rows(conn), "p1")
    assert int(row["priority"]) == 0