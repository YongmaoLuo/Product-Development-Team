"""Unit tests: the capacity ledger reads before it writes.

Background
----------
``CapacityLedger.reconcile()`` runs on every scheduler tick, and it used
to open a ``BEGIN IMMEDIATE`` transaction every time — even when the
slot rows already said what the plan facts said.  ``BEGIN IMMEDIATE``
takes SQLite's write lock *up front*, so a tick that had nothing to
write still made every other writer on the database wait: the
scheduler's own writes, the execution side's writes, the verifier's
writes.  When the database is busy, that wait is where ``database is
locked`` comes from.

A reconciliation only has to write when the two sides **disagree**.  So
the ledger reads first — the working plans and the slot rows — and
opens the write transaction only when the two disagree.

These tests pin the read-first shape:

1. **A stable state takes no write transaction at all.**  The slots
   already say what the facts say, so the ledger writes nothing and
   takes no write lock.
2. **A divergence converges in exactly one transaction.**  The write
   happens, and it happens once.
3. **The write re-checks inside the transaction.**  The difference may
   disappear between the light read and the moment the write lock is
   taken; the read inside the transaction is the one that counts, and a
   difference that is gone writes nothing.
4. **A write that fails leaves nothing behind.**  The transaction rolls
   back and the failure comes out unchanged.

The cases assert **the number of write transactions and what the ledger
wrote** — not how the ledger got there.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore

pytestmark = pytest.mark.unit


#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def _utc_iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class _Clock:
    """A clock the case moves by hand."""

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now


class _Boom(RuntimeError):
    """The failure a case makes the slot write raise."""


class _CountingStore(SqliteSchedulerStore):
    """A store that counts the write transactions and the slot reads.

    ``SqliteSchedulerStore.transaction`` opens a ``BEGIN IMMEDIATE`` when
    the connection is not in a transaction yet, and joins the caller's
    transaction when it is.  Only the first kind takes SQLite's write
    lock, so only the first kind is counted here: the number is how
    many times the ledger made the other writers of the database wait.

    The slot rows are counted too, because "the ledger read the slots
    again inside the transaction" is a part of the contract.
    """

    def __init__(self) -> None:
        super().__init__()
        #: How many times the ledger took SQLite's write lock.
        self.write_transactions = 0
        #: How many times the ledger read the slot rows.
        self.slot_reads = 0

    @contextlib.contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Count the write transaction, then run the real one."""
        if not conn.in_transaction:
            self.write_transactions += 1
        with super().transaction(conn):
            yield conn

    def list_slots(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Count the read, then answer with the real rows."""
        self.slot_reads += 1
        return super().list_slots(conn)

    def reset(self) -> None:
        """Forget the transactions and the reads counted so far."""
        self.write_transactions = 0
        self.slot_reads = 0


class _FailingStore(_CountingStore):
    """A store whose slot write fails after it has written the row."""

    def upsert_slot(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        now: Any = None,
        priority: int = 0,
    ) -> bool:
        """Write the row, and then fail — the way a broken write does."""
        super().upsert_slot(conn, plan_id, now=now, priority=priority)
        raise _Boom("the slot could not be written")


class _ScriptedFacts:
    """A facts reader that hands over the working plans it was given.

    The reader answers with one list of plan ids per call, and the
    lists may differ between two calls.  That is what makes "the
    difference disappeared between the light read and the write"
    expressible: the first answer names a plan that is not working, the
    second does not.
    """

    def __init__(self, *reads: Sequence[str]) -> None:
        self._reads = [list(read) for read in reads]
        self.calls = 0

    def active_plans(self) -> List[Dict[str, str]]:
        """Return the working plans of this read."""
        self.calls += 1
        if self.calls <= len(self._reads):
            plan_ids = self._reads[self.calls - 1]
        else:
            plan_ids = self._reads[-1] if self._reads else []
        return [{"plan_id": plan_id} for plan_id in plan_ids]


class _World:
    """The case's world: a database, a store and a clock."""

    def __init__(self, conn: sqlite3.Connection, root: Path) -> None:
        self.conn = conn
        self.root = root
        self.clock = _Clock()
        self.store = _CountingStore()

    def working(self, plan_id: str) -> None:
        """Make the plan hold compute.

        The plan is on the verification side — a round that is running —
        so the case needs no subprocess to make the plan work.
        """
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "verify_first_pass", _utc_iso(self.clock())),
        )
        self.conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "stop_reason, task_progress, next_run_at, card_state, flags, "
            "exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (plan_id, _utc_iso(self.clock()), _utc_iso(self.clock())),
        )
        self.conn.execute(
            "INSERT INTO plan_verification (plan_id, verification_status, "
            "round, max_rounds, updated_at) VALUES (?, ?, 0, 3, ?)",
            (plan_id, "running", _utc_iso(self.clock())),
        )

    def slot(self, plan_id: str) -> None:
        """Give the plan a slot."""
        self.store.upsert_slot(self.conn, plan_id, now=_utc_iso(BASE_EPOCH))

    def slot_ids(self) -> List[str]:
        """The plans that hold a slot, sorted — read without the store."""
        rows = self.conn.execute(
            "SELECT plan_id FROM scheduler_slot ORDER BY plan_id"
        ).fetchall()
        return [row[0] for row in rows]

    def ledger(self, facts: Any = None, store: Any = None) -> CapacityLedger:
        """A ledger over this world, with the real facts unless told otherwise."""
        reader = (
            facts
            if facts is not None
            else PlanFactsReader(self.conn, self.root / "plans", clock=self.clock)
        )
        return CapacityLedger(
            store if store is not None else self.store,
            reader,
            self.clock,
            conn=self.conn,
        )


@pytest.fixture()
def world(tmp_path: Path):
    """A ledger over an in-memory database that carries the real schema.

    The connection is opened the way the scheduler opens it in
    production — in autocommit mode (``isolation_level = None``).  That
    matters here: in the default mode the module opens a transaction of
    its own around the schema migration and leaves it open, and a
    connection that is already in a transaction *joins* it instead of
    taking the write lock — so the case would measure the wrong thing.
    """
    from state_machine.db.schema import migrate

    conn = sqlite3.connect(":memory:", isolation_level=None)
    migrate(conn)
    try:
        yield _World(conn, tmp_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. A stable state takes no write transaction
# ---------------------------------------------------------------------------


def test_stable_state_takes_no_write_transaction(world: _World) -> None:
    """The slots already say what the facts say: nothing is written.

    This is the steady state — every tick but the ones where a plan
    started or finished.  A tick like this used to take SQLite's write
    lock for nothing, and the write lock is what makes the other
    writers of the database wait.
    """
    world.working("a")
    world.working("b")
    world.slot("a")
    world.slot("b")
    world.store.reset()

    summary = world.ledger().reconcile()

    assert world.store.write_transactions == 0, (
        "a reconciliation that had nothing to write took the write lock "
        "anyway"
    )
    assert world.store.slot_reads == 1, (
        "the slots were read more than once in the steady state: {0!r}".format(
            world.store.slot_reads
        )
    )
    assert summary == {
        "used": 2,
        "added": [],
        "removed": [],
        "preserved": ["a", "b"],
    }, "the summary of a steady state moved: {0!r}".format(summary)
    assert world.slot_ids() == ["a", "b"]


# ---------------------------------------------------------------------------
# 2. A divergence converges in one transaction
# ---------------------------------------------------------------------------


def test_divergence_converges_in_one_transaction(world: _World) -> None:
    """A plan that works without a slot row gets one — in one transaction.

    The write happens, and it happens once: the ledger opens the write
    transaction, writes the missing row inside it, and commits.
    """
    world.working("a")
    world.working("b")
    world.working("c")
    world.slot("a")
    world.slot("b")
    world.store.reset()

    summary = world.ledger().reconcile()

    assert world.store.write_transactions == 1, (
        "the divergence was not converged in exactly one write "
        "transaction: {0!r}".format(world.store.write_transactions)
    )
    assert summary["added"] == ["c"], (
        "the working plan without a slot row did not get one: {0!r}".format(summary)
    )
    assert summary["removed"] == []
    assert summary["preserved"] == ["a", "b"]
    assert summary["used"] == 3
    assert world.slot_ids() == ["a", "b", "c"], (
        "the slot row was not written: {0!r}".format(world.slot_ids())
    )


# ---------------------------------------------------------------------------
# 3. The write re-checks inside the transaction
# ---------------------------------------------------------------------------


def test_write_rechecks_inside_transaction(world: _World) -> None:
    """The read inside the transaction is the one that counts.

    The difference may disappear between the light read and the moment
    the write lock is taken — a plan that finished in the meantime.
    The ledger reads the two sides again inside the transaction, and
    that second read is the one that decides what is written.
    """
    world.slot("a")
    world.slot("b")
    # The first read sees a plan that is working without a slot row;
    # the second read — the one inside the write transaction — does not.
    facts = _ScriptedFacts(["a", "b", "c"], ["a", "b"])
    world.store.reset()

    summary = world.ledger(facts=facts).reconcile()

    assert facts.calls == 2, (
        "the ledger did not read the working plans again inside the "
        "transaction: {0!r}".format(facts.calls)
    )
    assert world.store.slot_reads == 2, (
        "the ledger did not read the slots again inside the transaction: "
        "{0!r}".format(world.store.slot_reads)
    )
    assert world.store.write_transactions == 1, (
        "the write transaction was not the one transaction the "
        "reconciliation takes"
    )
    assert summary == {
        "used": 2,
        "added": [],
        "removed": [],
        "preserved": ["a", "b"],
    }, (
        "the summary did not follow the read inside the transaction: "
        "{0!r}".format(summary)
    )
    assert world.slot_ids() == ["a", "b"], (
        "a difference that was gone was written anyway: {0!r}".format(
            world.slot_ids()
        )
    )


# ---------------------------------------------------------------------------
# 4. A write that fails leaves nothing behind
# ---------------------------------------------------------------------------


def test_write_failure_rolls_back_and_raises(world: _World) -> None:
    """A write that fails is rolled back, and the failure comes out as it was.

    The write is one transaction, and a transaction that fails is one
    that leaves nothing behind — the ledger must not report a slot it
    did not write, and it must not swallow the failure that stopped it.
    """
    world.slot("a")
    world.slot("b")
    facts = _ScriptedFacts(["a", "b", "c"])
    store = _FailingStore()

    with pytest.raises(_Boom) as caught:
        world.ledger(facts=facts, store=store).reconcile()

    assert str(caught.value) == "the slot could not be written", (
        "the failure came out changed: {0!r}".format(caught.value)
    )
    assert world.slot_ids() == ["a", "b"], (
        "the failed write was not rolled back: {0!r}".format(world.slot_ids())
    )
    assert world.conn.in_transaction is False, (
        "the failed transaction was left open"
    )
