"""Integration tests: a reconciliation round in the steady state writes nothing.

Background
----------
The scheduler's tick reads the state, reconciles the queue and then
reconciles the slots.  Those two reconciliations are the only places
where the tick *changes* the database, and a change is a write
transaction: the store opens ``BEGIN IMMEDIATE``, which takes SQLite's
write lock up front.  Every other writer of the database waits behind
that lock, so a scheduler that takes the write lock on every tick --
to write rows that already say the right thing -- makes the rest of
the backend wait for it, every tick, for nothing.

The ledger already reads before it writes, and a unit case pins that
rule for the ledger alone.  This file pins the **round**: the three
phases together, on a real database with the production schema, the
real store, the real facts reader and the real ledger.  The round is
where the two rules meet, so the round is what the round-level
evidence has to be about.

What the cases hold down
------------------------
1. **A round in the steady state writes nothing.**  Two rounds run over
   the same facts.  The first may converge -- a round is allowed to
   write while the world still disagrees with itself -- but the second
   must not write at all: not one row, and not one write transaction.
2. **A round that has something to fix still fixes it.**  The pin
   above would also be satisfied by a scheduler that never writes
   anything, so the second case takes a queue row away and asks for it
   back.

The case's own doubles are the clock (moved by hand), the configuration
(one fixed snapshot), the sink (which records instead of executing) and
the lifecycle (which writes down what it was asked to do).  Everything
else -- the store, the facts reader, the ledger, the database and its
schema -- is production.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


pytestmark = pytest.mark.integration


#: The moment every case starts at.
BASE_EPOCH = 1_700_000_000.0

#: The writes the store is asked for.  Every one of them is counted
#: separately, because "the round wrote nothing" is a claim about each
#: of them: a round that opened a transaction and wrote no row is not
#: the same as a round that wrote a row and opened no transaction.
WRITE_METHODS: Tuple[str, ...] = (
    "transaction",
    "enqueue",
    "remove",
    "promote",
    "upsert_slot",
    "delete_slot",
    "set_setting",
)


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand.

    It is callable, because that is what the ledger, the facts reader
    and the engine ask for: ``clock()`` is "what time is it now".
    """

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new moment."""
        self._now += float(seconds)
        return self._now


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


class RecordingSink:
    """A sink that writes down every decision it is handed.

    It is the dry-run exit: it applies nothing and starts no plan, so a
    case can watch what the tick decided without the world changing.
    """

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Remember the decisions and hand back what they produced."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch


class LifecycleResult:
    """What the lifecycle answers: the work was done."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.ok = True
        self.detail = "ok"


class RecordingLifecycle:
    """A lifecycle that writes down what it was asked to do."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Record the pause and answer that it was done."""
        self.calls.append(("pause_plan", plan_id))
        return LifecycleResult(plan_id)

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Record the requeue and answer that it was done."""
        self.calls.append(("requeue_plan", plan_id))
        return LifecycleResult(plan_id)

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Record the stop and answer that it was done."""
        self.calls.append(("stop_plan", plan_id))
        return LifecycleResult(plan_id)


class CountingStore:
    """The real store, with a counter on every write it is asked for.

    The reads are the real store's reads: the counting happens *around*
    the call, so the store still does the writing and the case still
    sees a production database afterwards.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.counts: Dict[str, int] = {name: 0 for name in WRITE_METHODS}

    def reset(self) -> None:
        """Forget every write counted so far."""
        for name in self.counts:
            self.counts[name] = 0

    def transaction(self, conn: sqlite3.Connection) -> Any:
        """Count the transaction and hand back the real one."""
        self.counts["transaction"] += 1
        return self._inner.transaction(conn)

    def enqueue(self, *args: Any, **kwargs: Any) -> Any:
        """Count the enqueue and run the real one."""
        self.counts["enqueue"] += 1
        return self._inner.enqueue(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> Any:
        """Count the removal and run the real one."""
        self.counts["remove"] += 1
        return self._inner.remove(*args, **kwargs)

    def promote(self, *args: Any, **kwargs: Any) -> Any:
        """Count the promotion and run the real one."""
        self.counts["promote"] += 1
        return self._inner.promote(*args, **kwargs)

    def upsert_slot(self, *args: Any, **kwargs: Any) -> Any:
        """Count the slot write and run the real one."""
        self.counts["upsert_slot"] += 1
        return self._inner.upsert_slot(*args, **kwargs)

    def delete_slot(self, *args: Any, **kwargs: Any) -> Any:
        """Count the slot removal and run the real one."""
        self.counts["delete_slot"] += 1
        return self._inner.delete_slot(*args, **kwargs)

    def set_setting(self, *args: Any, **kwargs: Any) -> Any:
        """Count the setting write and run the real one."""
        self.counts["set_setting"] += 1
        return self._inner.set_setting(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Every read is the real store's read."""
        return getattr(self._inner, name)


class SqlTrace:
    """The SQL one connection ran, kept so a case can look for writes.

    The store's counters say *which* write methods were called; this
    says what the database was actually asked to do.  The two are not
    the same question: a write method opens its own transaction, and a
    transaction may be opened where no write method was called at all.
    """

    def __init__(self) -> None:
        self.statements: List[str] = []
        self._marks: List[int] = []

    def __call__(self, statement: str) -> None:
        """Remember one statement."""
        self.statements.append(str(statement))

    def mark(self) -> None:
        """Remember where the next chunk of statements starts."""
        self._marks.append(len(self.statements))

    def since_mark(self) -> List[str]:
        """The statements run since the last mark."""
        start = self._marks[-1] if self._marks else 0
        return list(self.statements[start:])

    def writes_since_mark(self) -> List[str]:
        """The statements since the last mark that wrote or took the lock."""
        return [text for text in self.since_mark() if _is_write(text)]


def _is_write(statement: str) -> bool:
    """Whether the statement took the write lock or wrote a row.

    ``BEGIN IMMEDIATE`` is the write lock itself, and it is the one
    that matters: it is what makes the other writers of the database
    wait.  A ``SELECT`` is not a write, however long it takes.
    """
    text = " ".join(str(statement).split()).upper()
    if text.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")):
        return True
    return text.startswith("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """Return the plans tree the facts reader reads."""
    import server

    return Path(server.PLANS_DIR)


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a connection to the case's database with the production contract."""
    from state_machine.db.connection import open as open_db

    return open_db(path)


class World:
    """What a case drives: the engine, the counting store and the trace.

    Attributes:
        engine: the engine under test.
        store: the counting store every write goes through.
        conn: the connection the engine reads and writes on -- the one
            the trace watches.
        trace: the SQL the connection ran.
    """

    def __init__(
        self,
        engine: Any,
        store: CountingStore,
        conn: sqlite3.Connection,
        trace: SqlTrace,
    ) -> None:
        self.engine = engine
        self.store = store
        self.conn = conn
        self.trace = trace

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def _world(db_factory: Any, plan_factory: Any) -> World:
    """Build the world a case drives.

    Two plans, one of each kind the round reconciles:

    * ``q1`` is **waiting** -- its routing row says ``queued`` and it
      holds a queue row, so the queue phase has nothing to fix;
    * ``r1`` is **working** -- its routing row says it is verifying,
      its verification round says it is running, and it holds a slot,
      so the ledger has nothing to fix either.

    Neither plan waits for an answer from a human.  A plan with a
    pending decision gives its slot back, and that release is a
    legitimate write -- so the steady state this file pins is the state
    where nobody is waiting for anybody.

    The database is a real file with the production schema, and the
    store, the facts reader and the ledger are the production ones.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    database = db_factory()
    plan_factory(plan_id="q1")

    conn = _open_db(database.path)
    store = CountingStore(SqliteSchedulerStore())

    clock = FakeClock()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock, conn=conn)
    commands = CommandExecutor(
        store,
        ledger,
        RecordingLifecycle(),
        facts=facts,
        conn=conn,
        capacity=1,
        min_runtime_seconds=0.0,
        clock=clock,
    )
    sink = RecordingSink()

    engine = PlanSchedulerEngine(
        config_loader=StaticConfigLoader(
            enabled=True,
            capacity=1,
            min_runtime_seconds=0.0,
            tick_seconds=0.01,
            command_timeout_seconds=10.0,
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=sink,
        clock=clock,
        conn=conn,
    )

    # The world the round reconciles: the waiting plan holds its queue
    # row, the working plan holds its slot, and neither of them is out
    # of step with the facts.
    stamp = _iso(BASE_EPOCH)
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        ("q1", "queued", stamp),
    )
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        ("r1", "verification_running", stamp),
    )
    conn.execute(
        "INSERT OR REPLACE INTO plan_verification "
        "(plan_id, verification_status, updated_at) VALUES (?, ?, ?)",
        ("r1", "running", stamp),
    )
    store.enqueue(conn, "q1", priority=0, now=stamp)
    store.upsert_slot(conn, "r1", stamp)

    # The tick refreshes its configuration before it reads anything, so
    # the engine holds the same snapshot the tick would hold.
    engine._phase_refresh_config()

    # The trace starts here: the setup above is the case's own doing,
    # and the round is what the case is about.
    trace = SqlTrace()
    conn.set_trace_callback(trace)
    store.reset()

    return World(engine, store, conn, trace)


def _round(world: World) -> Dict[str, int]:
    """Run one reconciliation round and return what it wrote.

    A round is the three phases the tick runs between reading the state
    and deciding on it: the queue is reconciled against the facts, and
    the slots are reconciled against the facts.  The phases are called
    directly rather than through the worker thread, so the case sees
    one round at a time and nothing else is running.
    """
    world.store.reset()
    world.trace.mark()
    world.engine._phase_read_state()
    world.engine._phase_reconcile_queue()
    world.engine._phase_reconcile_capacity()
    return dict(world.store.counts)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_steady_state_tick_takes_no_writes(db_factory, plan_factory) -> None:
    """Two rounds over the same facts: the second one writes nothing.

    The world is already reconciled, so the second round has nothing to
    say.  It must therefore take no write lock and write no row: every
    counter on the store is zero, the connection ran no write
    statement, and the connection is not left inside a transaction.
    """
    world = _world(db_factory, plan_factory)
    try:
        # The precondition of the steady state: nobody is waiting for an
        # answer.  A plan with a pending decision gives its slot back,
        # and that release is a legitimate write -- so a case with one
        # would not be measuring the steady state at all.
        pending = world.conn.execute(
            "SELECT COUNT(*) FROM plan_decisions WHERE status = 'pending'"
        ).fetchone()[0]
        assert pending == 0, (
            "the case has {0} plan(s) waiting for an answer, and giving "
            "a slot back is a legitimate write".format(pending)
        )

        _round(world)
        assert world.conn.in_transaction is False, (
            "the first round left the connection inside a transaction"
        )

        round2 = _round(world)

        assert round2 == {
            "transaction": 0,
            "enqueue": 0,
            "remove": 0,
            "promote": 0,
            "upsert_slot": 0,
            "delete_slot": 0,
            "set_setting": 0,
        }, (
            "the second round wrote to a database that was already right: "
            "{0!r}".format(round2)
        )

        writes = world.trace.writes_since_mark()
        assert writes == [], (
            "the second round ran a write statement: {0!r}".format(writes)
        )

        assert world.conn.in_transaction is False, (
            "the second round left the connection inside a transaction"
        )
    finally:
        world.close()


def test_divergence_still_converges(db_factory, plan_factory) -> None:
    """A queue row that is taken away is put back within one round.

    The steady-state case would also be satisfied by a scheduler that
    never writes anything, so this case asks for the write: the queue
    row is removed behind the scheduler's back, and the next round must
    put it back.
    """
    world = _world(db_factory, plan_factory)
    try:
        world.conn.execute(
            "DELETE FROM scheduler_queue WHERE plan_id = ?", ("q1",)
        )
        assert world.store.list_queue(world.conn) == [], (
            "the queue row was not taken away"
        )

        counts = _round(world)

        rows = [
            str(row["plan_id"]) for row in world.store.list_queue(world.conn)
        ]
        assert rows == ["q1"], (
            "the round did not put the queue row back: {0!r}".format(rows)
        )
        assert counts["enqueue"] == 1, (
            "the row came back without an enqueue: {0!r}".format(counts)
        )
    finally:
        world.close()