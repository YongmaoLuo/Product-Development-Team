"""The scheduler's read model reads through a connection of its own.

Background
----------
The scheduler had **one** connection: the worker wrote the queue, the
slots and the command log on it, and the panel's read model read on it
too.  A worker that sits in ``BEGIN IMMEDIATE`` holds the database's
write lock, and a read on the *same* connection waits for that
transaction — so the document the panel renders blocked behind the
worker's transaction for as long as the transaction ran.

The split this file pins:

* the **tick** keeps the write connection.  It decides on what it has
  just written, and it writes; a tick that read through a second
  connection could decide on a state it had already changed.
* the **read model** — the document the panel renders — reads through a
  second connection, and every row it shows comes from there.

The read model is allowed to be a moment behind: a panel that shows
the state of the moment is worth more than a panel that waits for the
worker.

These cases pin:

* the snapshot reads the queue **and** the plan facts through the read
  connection — a plan only the read connection knows about is in the
  document;
* the tick's own state still reads the write connection;
* without a read connection the read model falls back to the write
  connection — the old behaviour, kept as the negative control;
* the read-side facts are a *twin* of the write-side ones — the same
  plans root, the same clock, the same live set — bound to the read
  connection;
* the read-side ledger answers from the read-side facts;
* the engine closes the read connection only when it took it over.

Why two real databases
----------------------
The claim is a claim about **which connection a query ran on**.  Two
databases with different rows are the only way to tell the two apart:
one database answers the same either way, and the case would pass
whatever the engine did.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


def _now() -> str:
    """The current UTC time, as the state database writes it."""
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _fixed_clock() -> float:
    """A fixed "now", so no rule can depend on the wall."""
    return datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def _database() -> sqlite3.Connection:
    """A state database on the production schema."""
    from state_machine.db.schema import migrate

    connection = sqlite3.connect(":memory:", isolation_level=None)
    migrate(connection)
    return connection


@pytest.fixture()
def write_conn() -> Iterator[sqlite3.Connection]:
    """The connection the worker writes on."""
    connection = _database()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def read_conn() -> Iterator[sqlite3.Connection]:
    """The connection the read model reads on."""
    connection = _database()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def plans_root(tmp_path: Path) -> Path:
    """The plans tree, with the two plans the cases write about."""
    root = tmp_path / "plans"
    (root / "p-read").mkdir(parents=True)
    (root / "p-write").mkdir(parents=True)
    return root


def _queue_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    """Put a plan in the line: the routing row and the queue row.

    Both rows are needed: the phase lives in ``plan_routing`` and the
    place in the line lives in ``scheduler_queue``, and the read model
    is the two of them together.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, "queued", _now()),
    )
    SqliteSchedulerStore().enqueue(conn, plan_id, priority=0, now=_now())


def _engine(
    write_conn: sqlite3.Connection,
    plans_root: Path,
    *,
    read_conn: Optional[sqlite3.Connection] = None,
) -> Any:
    """An engine whose worker writes on ``write_conn``.

    The collaborators are the production ones — the scheduler's own
    store, the plan facts and the capacity ledger.  Only the
    connections differ between the cases, and the connections are the
    thing under test.
    """
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    store = SqliteSchedulerStore()
    facts = PlanFactsReader(write_conn, plans_root)
    ledger = CapacityLedger(store, facts, conn=write_conn)
    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=object(),
        sink=object(),
        conn=write_conn,
        read_conn=read_conn,
    )
    engine._config = engine._read_config()
    return engine


# ---------------------------------------------------------------------------
# 1. The snapshot reads through the read connection
# ---------------------------------------------------------------------------


def test_snapshot_reads_via_read_connection(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """The document names the plan the read connection knows about.

    The write connection is empty and the read connection holds one
    queued plan, so the document can only name it if the read really
    went through the read connection — the queue row *and* the plan
    facts, because the line is the two of them together.
    """
    _queue_plan(read_conn, "p-read")
    engine = _engine(write_conn, plans_root, read_conn=read_conn)

    snapshot = engine.snapshot()

    assert snapshot["queue"][0]["plan_id"] == "p-read", (
        "the queue row and the plan facts are read on the read connection"
    )
    assert snapshot["queue_length"] == 1


# ---------------------------------------------------------------------------
# 2. The tick still reads the write connection
# ---------------------------------------------------------------------------


def test_tick_state_still_reads_write_connection(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """The tick's own state reads the write connection.

    The tick decides on what it has just written, so it reads on the
    connection it writes on.  Here the write connection is empty, so
    the tick's state names no waiting plan — even though the read
    connection knows one.
    """
    _queue_plan(read_conn, "p-read")
    engine = _engine(write_conn, plans_root, read_conn=read_conn)

    state = engine._read_state()

    assert state["waiting"] == [], (
        "the tick reads the write connection, not the read one"
    )


# ---------------------------------------------------------------------------
# 3. Without a read connection the read model reads the write one
# ---------------------------------------------------------------------------


def test_read_conn_absent_falls_back_to_the_write_connection(
    write_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """No read connection means the old behaviour: the write connection.

    This is the negative control.  A plan in the write connection is in
    the document, because there is no other connection to read on.
    """
    _queue_plan(write_conn, "p-write")
    engine = _engine(write_conn, plans_root)

    snapshot = engine.snapshot()

    assert snapshot["queue"][0]["plan_id"] == "p-write"


# ---------------------------------------------------------------------------
# 4. The read side is a twin, bound to the read connection
# ---------------------------------------------------------------------------


def test_with_connection_replicates_the_readers_parameters(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """The twin carries the same parameters as the reader it came from.

    The plans root, the clock and the live set belong to the reader,
    not to the connection: a twin that read another tree, or asked
    another "now", or named another set of live plans, would be a
    second answer to the same question.
    """
    from plan_scheduler.facts import PlanFactsReader

    live = {"p-live"}
    facts = PlanFactsReader(
        write_conn,
        plans_root,
        clock=_fixed_clock,
        live_plan_ids=lambda: live,
    )

    twin = facts.with_connection(read_conn)

    assert twin is not facts
    assert twin._conn is read_conn, "the twin reads on the given connection"
    assert twin._project_root == facts._project_root
    assert twin._clock is facts._clock
    assert twin._live_plan_ids_source is facts._live_plan_ids_source
    assert twin._live_plan_ids() == {"p-live"}


def test_read_side_facts_are_bound_to_the_read_connection(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """The engine's read-side facts read on the read connection.

    A twin that fell back to the write connection would put the very
    lock this split removes back: the panel would wait for the worker
    again, one layer further down.
    """
    _queue_plan(read_conn, "p-read")
    engine = _engine(write_conn, plans_root, read_conn=read_conn)

    twin = engine._read_facts

    assert twin is not engine._facts, "the read side has its own reader"
    assert twin._conn is read_conn
    assert [row["plan_id"] for row in twin.queued_plans()] == ["p-read"]


def test_read_side_ledger_answers_from_the_read_side_facts(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """The read-side ledger is bound to the read-side facts.

    The ledger asks the facts who is working, so a read-side ledger
    that still held the write-side facts would count the plans of the
    write connection — and the number on the panel would be the number
    of another database.
    """
    engine = _engine(write_conn, plans_root, read_conn=read_conn)

    assert engine._read_ledger is not engine._ledger
    assert engine._read_ledger._facts is engine._read_facts
    assert engine._read_ledger._conn is read_conn


def test_a_reader_that_cannot_be_twinned_is_refused(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """A reader that cannot read through another connection is refused.

    The read model would otherwise be a mixture of two databases — the
    queue read on one connection and the plan facts on another — and a
    mixture is not an answer anybody can act on.  The engine says so
    instead of quietly reading the write connection.
    """
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine

    with pytest.raises(RuntimeError):
        PlanSchedulerEngine(
            config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
            store=object(),
            facts=object(),
            ledger=object(),
            commands=object(),
            sink=object(),
            conn=write_conn,
            read_conn=read_conn,
        )


def test_a_ledger_that_cannot_be_twinned_is_refused(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """A ledger that cannot read through another connection is refused.

    The same rule as the facts' one: the read-side ledger must be bound
    to the read connection, and a ledger that quietly keeps the write
    one would put the lock back.
    """
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader

    with pytest.raises(RuntimeError):
        PlanSchedulerEngine(
            config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
            store=object(),
            facts=PlanFactsReader(write_conn, plans_root),
            ledger=object(),
            commands=object(),
            sink=object(),
            conn=write_conn,
            read_conn=read_conn,
        )


# ---------------------------------------------------------------------------
# 5. The engine closes the connections it took over, and only those
# ---------------------------------------------------------------------------


def test_stop_leaves_a_read_connection_it_was_given(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """A connection the engine was given is not the engine's to close.

    The caller made it and the caller gives it back — the same rule the
    write connection has always followed.
    """
    engine = _engine(write_conn, plans_root, read_conn=read_conn)

    engine.stop()

    assert read_conn.execute("SELECT 1").fetchone() == (1,), (
        "the read connection is still the caller's"
    )


def test_stop_closes_the_read_connection_it_took_over(
    write_conn: sqlite3.Connection,
    read_conn: sqlite3.Connection,
    plans_root: Path,
) -> None:
    """The read connection the binder handed over is the engine's to close.

    The binder opened it, and a connection that is opened and never
    closed is a file descriptor that is never returned.
    """
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import EngineParts, PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    def _bind() -> EngineParts:
        store = SqliteSchedulerStore()
        facts = PlanFactsReader(write_conn, plans_root)
        return EngineParts(
            store=store,
            facts=facts,
            ledger=CapacityLedger(store, facts, conn=write_conn),
            commands=object(),
            sink=object(),
            conn=write_conn,
            read_conn=read_conn,
        )

    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
        binder=_bind,
    )
    assert engine._bind() is True

    engine.stop()

    with pytest.raises(sqlite3.ProgrammingError):
        read_conn.execute("SELECT 1")
