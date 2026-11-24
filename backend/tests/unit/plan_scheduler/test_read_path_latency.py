"""Unit tests: the panel's document answers while the worker waits for the lock.

Background
----------
The panel renders a document about the scheduler: the line of waiting
plans, the plans that hold a slot, the number that is in force.  That
document is worth more than a fresh row — an operator who sees the
state of a moment ago can act, and an operator who waits for the worker
cannot.

So the document is read through a connection of its own.  The worker
writes on another one, and a worker that sits in ``BEGIN IMMEDIATE``
holds the database's write lock for as long as the transaction runs.
A document read on the worker's own connection waits for that
transaction; a document read on a second connection reads the WAL
snapshot and comes back at once.

This file pins the **latency** of that promise.  The split itself is
pinned by ``test_read_conn_plumbing.py`` — that file shows which
connection a row came from.  This one shows how long the document
takes while the write lock is held, which is the claim an operator
actually feels.

The case
--------
Three connections on **one database file** — an in-memory database would
not do, because it does not cross connections at all:

* the **write connection**, the one the engine's worker writes on.  It
  is given a short ``busy_timeout``, and a thread asks it for the write
  lock while the blocker holds that lock, so the thread is inside
  SQLite's busy wait;
* the **read connection**, the one the document is read through;
* the **blocker**, the connection that holds the write lock.

The case waits until the writing thread is *really* waiting before it
reads anything: a statement that starts, plus a thread that has not come
back, are together the proof that the wait is a wait and not an idle
window.  A case that measured an idle window would answer in a
microsecond every time and would prove nothing at all.

What this file holds down
-------------------------
1. **The document comes back in a hundred milliseconds** while the
   worker waits for the write lock, and the worker is still waiting
   while it comes back.  The section below says which defect this
   case holds down and how it was closed.
2. **The document is still the document** — the queued plan is in it.
   A document that came back empty would be worse than a slow one: the
   operator would see no plan at all.

The gap this file found, and the fix that closed it
----------------------------------------------------
The first case was **red** against the tree this file was written
against (2026-11-23), and the reason was a defect and not a defect of
the case: ``PlanSchedulerEngine._running_view`` read the plans that
are working through the engine's **write-side** facts reader — ``for
row in self._facts.active_plans()`` in
``backend/plan_scheduler/engine.py`` — so the document waited for the
worker's connection no matter which connection the rest of the read
path used: it took seconds where the budget is 100 ms.

Every other part of the state already took the facts the caller reads
on (``_read_state``, ``_rebuild_line``, ``_parked_plans``);
``_running_view`` was the one that was left out.

The gap is **closed**: ``_running_view`` now reads the working plans
through the facts the caller reads on, so the document answers in a few
milliseconds while the worker waits for the lock.  The case below
therefore asserts the budget **directly** — there is no known-failure
marker left to hide behind, and a read path that starts depending on
the write connection again turns this file red.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore
from state_machine.db.connection import open as open_state_db
from state_machine.db.schema import migrate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The bounds the case works within
# ---------------------------------------------------------------------------

#: The budget the document has while the worker waits for the write
#: lock: a hundred milliseconds.
SNAPSHOT_BUDGET_SECONDS = 0.1

#: How long the writing connection waits for the lock before SQLite
#: gives up.  The wait is bounded, so a case cannot hang; the worker is
#: expected to still be waiting while the document is read.
WRITER_BUSY_TIMEOUT_MS = 800

#: The plan the case puts in the line.
PLAN_ID = "p1"


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


def _utc_iso() -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class _World:
    """The world the case works in: one database file, three connections.

    The connections are the production ones — the state machine's own
    factory, with its WAL contract — so the case is about the real
    contention and not about a test double's idea of it.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = tmp_path / "state.db"
        self.plans_root = tmp_path / "plans"
        # The plan is a directory as well as a row: the reader drops a
        # plan whose directory is not there.
        (self.plans_root / PLAN_ID).mkdir(parents=True)

        # The connection the worker writes on.  Its busy timeout is
        # shortened, so the worker's wait is bounded and the case
        # cannot hang.
        self.write_conn = open_state_db(self.db_path)
        migrate(self.write_conn)
        self.write_conn.execute(
            "PRAGMA busy_timeout = {0}".format(WRITER_BUSY_TIMEOUT_MS)
        )

        # The connection the document is read through: the same file, a
        # second handle.
        self.read_conn = open_state_db(self.db_path)

        # The connection that holds the write lock.
        self.blocker = open_state_db(self.db_path)

        self._queue_plan(PLAN_ID)

        store = SqliteSchedulerStore()
        # The facts read on the **write** connection — the shape the
        # binder builds: the worker decides on what it has just written.
        facts = PlanFactsReader(self.write_conn, self.plans_root)
        ledger = CapacityLedger(store, facts, conn=self.write_conn)
        self.engine = PlanSchedulerEngine(
            config_loader=StaticConfigLoader(),
            store=store,
            facts=facts,
            ledger=ledger,
            commands=object(),
            sink=object(),
            conn=self.write_conn,
            read_conn=self.read_conn,
        )
        self.engine._config = self.engine._read_config()

        # The blocker takes the write lock and holds it.  It is the last
        # thing the setup does: everything above needed the lock to be
        # free.
        self.blocker.execute("BEGIN IMMEDIATE")

        self._writer: Optional[threading.Thread] = None
        self._statement_started = threading.Event()
        self._writer_fault: Optional[BaseException] = None

    # -- the setup ---------------------------------------------------------

    def _queue_plan(self, plan_id: str) -> None:
        """Put the plan in the line: the routing row and the queue row.

        Both rows are needed: the phase lives in ``plan_routing`` and
        the place in the line lives in ``scheduler_queue``, and the
        document is the two of them together.
        """
        self.write_conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "queued", _utc_iso()),
        )
        SqliteSchedulerStore().enqueue(
            self.write_conn, plan_id, priority=0, now=_utc_iso()
        )

    # -- the contention ----------------------------------------------------

    def start_writer(self) -> None:
        """Start the thread that asks for the write lock."""
        self.write_conn.set_trace_callback(self._trace)
        self._writer = threading.Thread(
            target=self._wait_for_the_lock,
            name="scheduler-writer",
        )
        self._writer.daemon = True
        self._writer.start()

    def _trace(self, statement: str) -> None:
        """Notice the moment the writing statement starts.

        The trace callback fires when SQLite prepares the statement, so
        the thread is already inside the call that waits.
        """
        if "BEGIN IMMEDIATE" in statement.upper():
            self._statement_started.set()

    def _wait_for_the_lock(self) -> None:
        """Ask for the write lock, and wait for it.

        The blocker holds that lock, so the call cannot come back before
        the blocker gives it up.  When it does come back it comes back
        with SQLite's own answer — ``database is locked`` — which is the
        fault the case is about, not the fault the case asserts.
        """
        try:
            self.write_conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:  # noqa: BLE001 — recorded
            self._writer_fault = exc

    def wait_for_the_writer(self, timeout: float = 5.0) -> None:
        """Wait until the writing thread is inside the call that waits.

        Two facts together are the proof: the trace callback says the
        statement has started, and the thread being alive says it has
        not come back.  Between them the writer is inside SQLite's busy
        wait — the blocker holds the write lock, so the call cannot
        succeed before the blocker gives it up.

        Waiting for the second fact alone would be a hole: a thread that
        had not started yet is alive too.
        """
        assert self._statement_started.wait(timeout), (
            "the writing thread never reached the write lock"
        )
        assert self.writer_alive(), (
            "the writing thread came back before the lock was contended"
            "{0}".format(
                "" if self._writer_fault is None
                else ": {0!r}".format(self._writer_fault)
            )
        )

    def writer_alive(self) -> bool:
        """Whether the writing thread is still waiting."""
        return self._writer is not None and self._writer.is_alive()

    # -- the teardown ------------------------------------------------------

    def close(self) -> None:
        """Give the lock back, stop the writer, close the connections.

        The order matters twice over.  The blocker gives the write lock
        back first, so the waiting thread can come back and be joined.
        The trace callback is taken off **after** the join: taking it off
        while the thread waits would block on the very mutex the waiting
        thread holds, and the teardown would last as long as the wait.
        """
        try:
            if self.blocker.in_transaction:
                self.blocker.execute("ROLLBACK")
        except sqlite3.Error:  # noqa: BLE001 — the teardown never raises
            pass
        if self._writer is not None:
            self._writer.join(timeout=5.0)
        try:
            self.write_conn.set_trace_callback(None)
        except sqlite3.Error:  # noqa: BLE001 — the teardown never raises
            pass
        for conn in (self.write_conn, self.read_conn, self.blocker):
            try:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()
            except sqlite3.Error:  # noqa: BLE001 — the teardown never raises
                pass


@pytest.fixture()
def world(tmp_path: Path) -> Iterator[_World]:
    """A file database, three connections on it, and one queued plan."""
    the_world = _World(tmp_path)
    try:
        yield the_world
    finally:
        the_world.close()


# ---------------------------------------------------------------------------
# 1. The document answers while the worker waits
# ---------------------------------------------------------------------------


def test_snapshot_returns_within_100ms_while_writer_waits(world: _World) -> None:
    """The document answers in a hundred milliseconds while the worker waits.

    The worker is waiting for the write lock — the blocker holds it and
    the worker has not come back — and the document is read through the
    read connection.  The panel must not wait for the worker.

    The worker being alive afterwards is part of the answer: a document
    that came back quickly because the wait had already ended would
    prove nothing about the contended case.
    """
    world.start_writer()
    world.wait_for_the_writer()

    started = time.perf_counter()
    world.engine.snapshot()
    elapsed = time.perf_counter() - started

    writer_alive_during_snapshot = world.writer_alive()

    assert elapsed < SNAPSHOT_BUDGET_SECONDS, (
        "the document took {0:.3f}s while the worker waited for the "
        "write lock; the budget is {1:.3f}s".format(
            elapsed, SNAPSHOT_BUDGET_SECONDS
        )
    )
    if not writer_alive_during_snapshot:
        # A case that measured an idle window is a broken case, not a
        # broken product: it would answer in a microsecond every time
        # and would prove nothing about the contended case.
        pytest.fail(
            "the worker was not waiting any more: the case measured an "
            "idle window, not a contended one"
        )


# ---------------------------------------------------------------------------
# 2. The document is still the document
# ---------------------------------------------------------------------------


def test_snapshot_still_lists_queue_under_lock(world: _World) -> None:
    """The document still names the queued plan while the worker waits.

    The read connection reads the WAL snapshot as it was, so the line is
    there even though the worker is waiting for the lock.  A document
    that came back empty would be worse than a slow one: the operator
    would see no plan at all.
    """
    world.start_writer()
    world.wait_for_the_writer()

    snapshot = world.engine.snapshot()

    queued: List[str] = [
        str(row.get("plan_id")) for row in snapshot.get("queue", [])
    ]
    assert PLAN_ID in queued, (
        "the queued plan is not in the document: {0!r}".format(queued)
    )
