"""Integration tests: the execute phase isolates the decisions and refills.

Background
----------
The tick's decisions go through the **sink** — the live exit hands them
to the lifecycle, one by one, and the dry-run exit writes them down.
But the engine was handing the whole batch over in one call and never
looking at what came back: a decision that failed was neither recorded
nor acted upon, and the plan it was about kept its place in the queue
for ever — holding the line for the plans behind it.

So the execute phase reads the batch's outcomes **one by one**, and the
two kinds of failure are told apart:

* A failure that will never go away — the plan is not there, the plan
  is archived, the request conflicts with the plan's own state — is
  **permanent** (see
  :func:`plan_scheduler.engine.is_permanent_failure`).  The plan is
  **pruned**: its queue row goes, its slot goes, its phase becomes
  ``stopped``.  And the tick **refills** — the state is read again and
  the free slots are put to work, so the plan behind the pruned one is
  not left waiting for the next tick.
* A failure that may go away by itself is **transient**.  It is
  recorded — in ``engine.errors``, under the phase ``execute`` — and the
  plan keeps its place in the queue, to be tried again on the next
  tick.

What is pinned here
-------------------
``test_second_decision_runs_after_first_permanent_failure``
    The decision behind a permanently failed one is still carried out,
    and the failed plan is out of the queue.
``test_refill_admits_next_plan_same_tick``
    The plan behind the pruned one is admitted in the **same** tick —
    the sink is handed the second batch in that tick, not in the next.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader and the command executor are the real ones,
on a real SQLite file with the production schema.  The exit is the
production :class:`plan_scheduler.dryrun.ExecuteSink`, so the isolation
between the decisions is the isolation the live scheduler has.  Only
three things are the case's own: the clock, the executor the exit is
given (which fails for the plan the case names) and the facts the case
writes itself.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_execute_isolation_and_refill.py -v
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.dryrun import ExecuteSink
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The moment every case starts at.  It is written out rather than
#: taken from the wall clock: the clock is injected, and a case that
#: read the real time would be a case whose answer depends on when it
#: ran.
BASE_EPOCH = 7000.0

#: The phase a plan is in while it waits for its turn to run.
QUEUED = "queued"

#: The phase a plan is in when it works.
EXECUTING = "executing"

#: How many plans may work at the same time in these cases.
CAPACITY = 2


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


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


class RecordingExecuteSink:
    """The live exit, with every batch it was handed written down.

    The exit itself is the production one — :class:`ExecuteSink` — so
    the isolation between the decisions is the isolation the live
    scheduler has.  What the case owns is the **executor** the exit is
    given: it fails for the plans the case names, and it starts the
    plans it does not fail.

    Starting a plan means moving its phase to ``executing``, which is
    the part of the lifecycle the scheduler's own facts depend on: a
    plan that works is not waiting any more, so it is not admitted a
    second time.

    Attributes:
        failures: the failure each named plan raises, keyed by plan id.
        batches: every batch the exit was handed, in order.
        carried: the plans that were carried out, in order.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sink = ExecuteSink(self._execute)
        self.failures: Dict[str, BaseException] = {}
        self.batches: List[List[Dict[str, Any]]] = []
        self.carried: List[str] = []

    def _execute(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out — or fail the way the case says."""
        plan_id = str(decision.get("plan_id") or "")
        error = self.failures.get(plan_id)
        if error is not None:
            raise error
        self.carried.append(plan_id)
        _routing(self._conn, plan_id, EXECUTING)
        return {}

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Write the batch down and hand it to the live exit."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return self._sink.apply(decisions)

    def handed(self) -> List[List[str]]:
        """The plan ids of every batch, in the order they were handed over."""
        return [
            [str(decision.get("plan_id") or "") for decision in batch]
            for batch in self.batches
        ]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float = BASE_EPOCH) -> str:
    """Return the moment the way the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """The plans tree the facts reader reads."""
    override = os.environ.get("PDT_PLANS_DIR")
    if override:
        return Path(override)
    import server

    return Path(server.PLANS_DIR)


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a connection to the case's database, production contract."""
    from state_machine.db.connection import open as open_db

    return open_db(path)


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing fact: the phase it is in."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        (plan_id, phase, _iso()),
    )


def _queue_row(
    conn: sqlite3.Connection, plan_id: str, seq: int = 1, priority: int = 0
) -> None:
    """Write the scheduler's own queue row for the plan."""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (plan_id, seq, priority, _iso(), _iso()),
    )


def _queue_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that have a queue row, in the production order."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_queue(conn)]


def _slot_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that hold a concurrency slot."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_slots(conn)]


def _permanent_error(plan_id: str) -> BaseException:
    """The failure that will never go away: the plan is not there."""
    from state_machine.repositories.routing_repository import PlanNotFoundError

    return PlanNotFoundError(plan_id)


class World:
    """What a case drives: the engine and the collaborators around it.

    Attributes:
        engine: the engine under test.
        store: the scheduler store the case reads through.
        sink: the exit the tick's decisions are handed to.
        conn: the case's own connection.
    """

    def __init__(
        self,
        engine: Any,
        store: SqliteSchedulerStore,
        sink: RecordingExecuteSink,
        conn: sqlite3.Connection,
    ) -> None:
        self.engine = engine
        self.store = store
        self.sink = sink
        self.conn = conn

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def _world(
    db_factory: Any,
    plan_factory: Any,
    plan_ids: Any = ("p1",),
    capacity: int = CAPACITY,
) -> World:
    """Build the world a case drives.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader, the command executor and the live exit.
    Only the clock and the executor the exit is given are the case's
    own.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.engine import PlanSchedulerEngine

    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(plan_id=plan_id)

    conn = _open_db(database.path)
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    commands = CommandExecutor(
        store,
        ledger,
        PlanLifecycleService(),
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=0.0,
        clock=clock,
    )
    sink = RecordingExecuteSink(conn)
    engine = PlanSchedulerEngine(
        config_loader=StaticConfigLoader(
            enabled=True,
            capacity=capacity,
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
    )
    return World(engine, store, sink, conn)


# ---------------------------------------------------------------------------
# 1. The batch is isolated: one failure does not void the rest
# ---------------------------------------------------------------------------


def test_second_decision_runs_after_first_permanent_failure(
    db_factory: Any, plan_factory: Any
) -> None:
    """The decision behind a failed one is still carried out.

    The batch is handed over as a batch, but the outcomes are read one
    by one: the decision that failed for good does not take the
    decisions behind it with it.  A plan that failed for good is also
    **pruned** — it is not there any more, so it must not hold the
    line — while the plan behind it is admitted.
    """
    world = _world(db_factory, plan_factory, plan_ids=("ghost", "p1"))
    try:
        _routing(world.conn, "ghost", QUEUED)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "ghost", seq=1)
        _queue_row(world.conn, "p1", seq=2)
        world.sink.failures["ghost"] = _permanent_error("ghost")

        world.engine._run_tick()

        assert world.sink.handed() == [["ghost", "p1"]], (
            "the batch the exit was handed: {0!r}".format(world.sink.handed())
        )
        assert world.sink.carried == ["p1"], (
            "the decision behind the failed one was not carried out: "
            "{0!r}".format(world.sink.carried)
        )
        assert _queue_ids(world.conn) == ["p1"], (
            "the plan that will never run is still in the queue: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert _slot_ids(world.conn) == ["p1"], (
            "the plan that will never run still holds a slot: {0!r}".format(
                _slot_ids(world.conn)
            )
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. The pruned plan is replaced in the same tick
# ---------------------------------------------------------------------------


def test_refill_admits_next_plan_same_tick(
    db_factory: Any, plan_factory: Any
) -> None:
    """The plan behind the pruned one is admitted in the same tick.

    The capacity is one, so the tick admits one plan: the one at the
    head of the line — the ghost, whose directory is there but whose
    plan is not.  The ghost fails for good, so it is pruned, and the
    tick **refills**: the state is read again and the free slot is put
    to work, so the plan behind the ghost does not wait for the next
    tick.

    The exit is handed two batches: the first with the ghost, the
    second with the plan behind it.
    """
    world = _world(
        db_factory, plan_factory, plan_ids=("ghost", "p1"), capacity=1
    )
    try:
        _routing(world.conn, "ghost", QUEUED)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "ghost", seq=1)
        _queue_row(world.conn, "p1", seq=2)
        world.sink.failures["ghost"] = _permanent_error("ghost")

        world.engine._run_tick()

        assert world.sink.handed() == [["ghost"], ["p1"]], (
            "the exit was not handed the plan behind the pruned one in the "
            "same tick: {0!r}".format(world.sink.handed())
        )
        assert world.sink.carried == ["p1"], (
            "the plan behind the pruned one was not carried out: {0!r}".format(
                world.sink.carried
            )
        )
        assert _queue_ids(world.conn) == ["p1"], (
            "the ghost's row is still in the queue table: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert _slot_ids(world.conn) == ["p1"], (
            "the ghost still holds the slot the plan behind it took: "
            "{0!r}".format(_slot_ids(world.conn))
        )
    finally:
        world.close()
