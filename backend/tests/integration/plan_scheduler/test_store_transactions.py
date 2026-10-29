"""Integration tests: the scheduler store's sequence, its transactions, its commands.

Background
----------
The scheduler derives every *plan* fact from the ``plan_*`` tables on
every tick.  What it cannot derive is its own bookkeeping, and that is
what the three ``scheduler_*`` tables hold: the order in which queued
plans may run, which plans hold a concurrency slot, and the manual
commands an operator issued.  :class:`SqliteSchedulerStore` is the
only thing that writes them, and it has to hold four promises at once:

1. **The sequence is strictly increasing.**  ``seq`` is the tie-breaker
   that makes "who runs next" a question with exactly one answer, so
   two plans may never claim the same number — not even when two
   connections reach the write lock at the same moment.
2. **A write is all-or-nothing.**  Changing the queue and writing a
   slot are one fact ("this plan is running, and here is where it sits
   in the queue"), so a failure half way through may not leave half of
   it behind.
3. **A freed number may be reused, but never twice.**  The number a
   removed plan held goes back into the pool, and the plans that are
   still queued keep the order they had.
4. **A command lands once.**  A command is written ``accepted`` and
   finished by the worker; finishing it twice may not overwrite the
   first outcome.

These tests run against a real SQLite file (never ``:memory:``): the
contract they pin is a *concurrency* contract — two connections
racing for the same write lock — and two connections cannot race over
an in-memory database.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, List

import pytest

from plan_scheduler.store import SqliteSchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the production PRAGMA contract.

    The production contract is the point: ``busy_timeout = 5000`` is
    what lets the second writer wait for the first one to commit
    instead of failing with "database is locked", and WAL is what lets
    a reader see the queue while a writer holds the lock.
    """
    from state_machine.db.connection import open as open_db

    return open_db(db_path)


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler tables."""
    from state_machine.db.schema import migrate

    conn = _connect(db_path)
    migrate(conn)
    return conn


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """The path of the throwaway state database the tests write to."""
    return tmp_path / "state.db"


@pytest.fixture()
def conn(db_path: Path):
    """A migrated connection, closed when the test is over."""
    connection = _open_migrated(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _plan_ids(rows: List[Dict[str, object]]) -> List[str]:
    """The plan ids of ``rows``, in the order they came out."""
    return [str(row["plan_id"]) for row in rows]


def _seqs(rows: List[Dict[str, object]]) -> List[int]:
    """The sequence numbers of ``rows``, in the order they came out."""
    return [int(row["seq"]) for row in rows]


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------


def test_seq_strictly_increasing(db_path: Path) -> None:
    """Two connections racing to enqueue get two different numbers.

    This is the promise the whole queue rests on: ``seq`` is the
    tie-breaker that makes the order total, so two plans sharing a
    number would make "who runs next" ambiguous.

    The race is real: both connections are opened, both reach the
    barrier, and both then try to take the write lock.  The second one
    waits for the first to commit (SQLite's busy handler, inside
    ``busy_timeout``) and therefore reads the sequence *after* the
    first one committed it — it never reads the same maximum and hands
    out the same number.
    """
    store = SqliteSchedulerStore()

    # Migrate once, up front, so the two racing connections do not
    # compete for the write lock over the DDL.
    setup = _open_migrated(db_path)
    setup.close()

    barrier = threading.Barrier(2)
    seqs: Dict[str, int] = {}
    errors: List[BaseException] = []
    lock = threading.Lock()

    def enqueue(plan_id: str) -> None:
        connection = _connect(db_path)
        try:
            # Both threads are released at the same moment, so the two
            # BEGIN IMMEDIATE statements really do overlap.
            barrier.wait(timeout=30)
            seq = store.enqueue(connection, plan_id, priority=0)
            with lock:
                seqs[plan_id] = seq
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=enqueue, args=(plan_id,))
        for plan_id in ("p1", "p2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"the concurrent enqueue raised: {errors!r}"
    assert not any(t.is_alive() for t in threads), "an enqueue thread hung"
    assert sorted(seqs.values()) == [1, 2], (
        f"the sequence numbers are not strictly increasing: {seqs!r}"
    )

    # And the queue itself holds no tie.
    connection = _connect(db_path)
    try:
        rows = store.list_queue(connection)
    finally:
        connection.close()
    assert len(rows) == 2
    assert len(set(_seqs(rows))) == 2, f"two plans share a number: {_seqs(rows)!r}"


def test_seq_reuse_keeps_order(conn: sqlite3.Connection) -> None:
    """A freed number is handed out again — and only once.

    The number a removed plan held goes back into the pool, because the
    next number is ``MAX(seq) + 1`` over the rows that are still there.
    What must not happen is a tie: the plans that are still queued keep
    the order they had, and nobody ends up with the same number.
    """
    store = SqliteSchedulerStore()
    store.enqueue(conn, "p1")
    store.enqueue(conn, "p2")
    store.enqueue(conn, "p3")

    before = _plan_ids(store.list_queue(conn))
    assert before == ["p1", "p2", "p3"]
    assert _seqs(store.list_queue(conn)) == [1, 2, 3]

    # The plan holding the highest number leaves the queue.
    assert store.remove(conn, "p3") is True

    seq = store.enqueue(conn, "p4")
    rows = store.list_queue(conn)

    assert seq == 3, "the freed number should be handed out again"
    assert len(set(_seqs(rows))) == len(rows), (
        f"two plans share a number: {_seqs(rows)!r}"
    )
    assert _plan_ids(rows) == ["p1", "p2", "p4"], (
        "the plans that were already queued moved"
    )


def test_enqueue_of_a_queued_plan_keeps_its_place(conn: sqlite3.Connection) -> None:
    """Enqueueing a plan that is already queued does not move it.

    The queue holds one row per plan (``plan_id`` is the primary key),
    so a second ``enqueue`` of the same plan cannot add a second row —
    it updates the plan's priority and leaves its place alone.  A
    retried command that re-enqueues a plan must not push it to the
    back of the line.
    """
    store = SqliteSchedulerStore()
    store.enqueue(conn, "p1")
    store.enqueue(conn, "p2")

    seq = store.enqueue(conn, "p1", priority=-1)

    rows = store.list_queue(conn)
    assert len(rows) == 2, "a plan may only be in the queue once"
    assert seq == 1, "the queued plan kept its number"
    assert rows[0]["plan_id"] == "p1", "the queued plan moved"
    assert int(rows[0]["priority"]) == -1, "the priority was not updated"


# ---------------------------------------------------------------------------
# The transaction boundary
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_both_tables(conn: sqlite3.Connection) -> None:
    """A failure half way through leaves neither table changed.

    Changing the queue and writing a slot are one fact about one plan,
    so they belong in one transaction.  When the body raises, the
    whole thing goes back — including the row that was written
    *before* the failure, which is the half that is easy to forget.
    """
    store = SqliteSchedulerStore()

    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction(conn):
            store.enqueue(conn, "p1")
            store.upsert_slot(conn, "p1")
            raise RuntimeError("boom")

    assert store.list_queue(conn) == [], "the queue row survived the rollback"
    assert store.list_slots(conn) == [], "the slot row survived the rollback"


def test_transaction_commits_both_tables(conn: sqlite3.Connection) -> None:
    """The same two writes, without the failure, are both there.

    The rollback test would also pass if the writes never happened at
    all; this is the other half of the contract — a transaction that
    succeeds keeps everything it wrote.
    """
    store = SqliteSchedulerStore()

    with store.transaction(conn):
        store.enqueue(conn, "p1")
        store.upsert_slot(conn, "p1")

    assert _plan_ids(store.list_queue(conn)) == ["p1"]
    assert _plan_ids(store.list_slots(conn)) == ["p1"]


# ---------------------------------------------------------------------------
# The command lifecycle
# ---------------------------------------------------------------------------


def test_command_lifecycle_accepted_to_done(conn: sqlite3.Connection) -> None:
    """A command is recorded ``accepted`` and finished by the worker.

    This is the real lifecycle of a command: the mailbox writes it
    down the moment the operator asks for it, and the worker that
    carries it out writes the outcome and the detail back.  The
    intermediate ``accepted`` state is what makes the two steps
    visible — a command that was never acted on is still recorded, and
    it is still readable as "not finished yet".
    """
    store = SqliteSchedulerStore()

    command_id = store.record_command(conn, "p1", "promote")
    assert isinstance(command_id, int) and command_id > 0

    recorded = store.get_command(conn, command_id)
    assert recorded is not None
    assert recorded["plan_id"] == "p1"
    assert recorded["action"] == "promote"
    assert recorded["outcome"] == "accepted", (
        "a command starts out accepted — the worker has not run it yet"
    )
    assert recorded["detail"] is None

    assert store.finish_command(conn, command_id, outcome="done", detail="promoted") is True

    finished = store.get_command(conn, command_id)
    assert finished is not None
    assert finished["outcome"] == "done"
    assert finished["detail"] == "promoted", "the worker's detail was not written back"


def test_finish_command_idempotent(conn: sqlite3.Connection) -> None:
    """Finishing a command twice lands the outcome only once.

    The worker may retry — the command is written down again, the
    mailbox is drained again, the worker answers again.  The second
    answer must not overwrite the first: the command table records
    *what happened*, and "what happened" happened once.
    """
    store = SqliteSchedulerStore()

    command_id = store.record_command(conn, "p1", "promote")
    assert store.finish_command(conn, command_id, outcome="done", detail="first") is True
    assert store.finish_command(conn, command_id, outcome="failed", detail="second") is False

    finished = store.get_command(conn, command_id)
    assert finished is not None
    assert finished["outcome"] == "done", "the second finish overwrote the first"
    assert finished["detail"] == "first", "the second finish overwrote the first"


def test_finish_command_keeps_the_detail_it_was_given(conn: sqlite3.Connection) -> None:
    """Finishing without a detail does not erase the one already there."""
    store = SqliteSchedulerStore()

    command_id = store.record_command(
        conn, "p1", "promote", detail="promote this plan to the front"
    )
    assert store.finish_command(conn, command_id, outcome="done") is True

    finished = store.get_command(conn, command_id)
    assert finished is not None
    assert finished["outcome"] == "done"
    assert finished["detail"] == "promote this plan to the front"


# ---------------------------------------------------------------------------
# The queue's order, the promotion and the slots
# ---------------------------------------------------------------------------


def test_list_queue_uses_the_shared_order(conn: sqlite3.Connection) -> None:
    """The queue comes back in the order the one comparator says.

    The comparator in :mod:`plan_scheduler.queue` is what the panel,
    the admission check and the restart recovery all sort by, so the
    store has to hand the rows out in that order and not in whatever
    order SQLite happens to return them in.
    """
    store = SqliteSchedulerStore()
    store.enqueue(conn, "p1")
    store.enqueue(conn, "p2")
    store.enqueue(conn, "p3")

    # A promotion moves a plan ahead of every plan that was never
    # promoted, and the most recent promotion wins.
    assert store.promote(conn, "p2") == -1, "the first promotion is -1"
    assert _plan_ids(store.list_queue(conn)) == ["p2", "p1", "p3"]

    assert store.promote(conn, "p3") == -2, "the promotion counter decreases"
    assert _plan_ids(store.list_queue(conn)) == ["p3", "p2", "p1"]

    # The priority is sorted by the shared comparator, and the
    # comparator sorts it ascending — a smaller number runs earlier.
    # The store has no opinion of its own about that; it hands the rows
    # out in the comparator's order.
    store.enqueue(conn, "p4", priority=-5)
    assert _plan_ids(store.list_queue(conn)) == ["p3", "p2", "p4", "p1"]


def test_promotion_is_consumed_once(conn: sqlite3.Connection) -> None:
    """A promotion is a one-shot: consuming it clears it.

    A promotion says "this plan runs next".  Once the plan has run, the
    promotion has been used up — leaving it on the row would keep
    pushing that plan ahead of the queue forever, long after the
    operator's reason for promoting it was gone.
    """
    store = SqliteSchedulerStore()
    store.enqueue(conn, "p1")
    store.enqueue(conn, "p2")

    promote_order = store.promote(conn, "p1")
    assert promote_order == -1, "the first promotion is -1"
    assert _plan_ids(store.list_queue(conn)) == ["p1", "p2"]

    assert store.consume_promotion(conn, "p1") == promote_order

    # The promotion is gone: the queue is back to plain arrival order
    # among plans that were never promoted.
    rows = store.list_queue(conn)
    assert _plan_ids(rows) == ["p1", "p2"]
    assert rows[0]["promote_order"] is None
    assert store.consume_promotion(conn, "p1") is None


def test_slot_lifecycle(conn: sqlite3.Connection) -> None:
    """A slot is taken, listed and given back."""
    store = SqliteSchedulerStore()

    assert store.upsert_slot(conn, "p1") is True
    assert store.upsert_slot(conn, "p2") is True

    slots = store.list_slots(conn)
    assert set(_plan_ids(slots)) == {"p1", "p2"}
    assert all(slot["acquired_at"] for slot in slots)

    # Taking a slot that is already held does not reset the clock: the
    # eviction rule reads ``acquired_at``, and a plan that has been
    # running for an hour must not get an hour more because its slot
    # was written down twice.
    acquired_at = {slot["plan_id"]: slot["acquired_at"] for slot in slots}
    store.upsert_slot(conn, "p1")
    slots = {slot["plan_id"]: slot for slot in store.list_slots(conn)}
    assert slots["p1"]["acquired_at"] == acquired_at["p1"]

    assert store.delete_slot(conn, "p1") is True
    assert store.delete_slot(conn, "p1") is False
    assert _plan_ids(store.list_slots(conn)) == ["p2"]
