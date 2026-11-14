"""Integration: the priority a plan was queued with survives its admission.

Background
----------
A plan's priority lives in ``scheduler_queue`` — the number the caller
gave when the plan was queued, and the number the shared comparator
sorts the line by.  It is **not** a plan fact: the queue table is the
scheduler's own copy of it.

The copy has a very short life.  The queue row is taken out of the
table the moment the plan is admitted — a plan that is running is not
in a line — so the number the caller gave is gone from the queue the
moment the plan starts working.  After that moment the only place the
number can live is ``scheduler_slot``: the row that says "this plan
holds compute, and it holds it at this priority".

So the admission is the moment the number has to be **moved**, not
merely read: the slot row is written while the queue row is still
there, and the two are written from the same number.  A scheduler that
admits a plan without carrying the number over has a running plan
whose priority nobody can read — and an eviction rule that compares
priorities has nothing to compare.

What is pinned here
-------------------
``test_admission_writes_the_queued_priority_to_the_slot``
    A plan queued with priority ``-10`` holds a slot whose priority is
    ``-10`` after it is admitted: the number was moved, not dropped.
``test_admission_without_a_priority_writes_zero``
    A queue row that carries no priority of its own gives a slot row
    of ``0``: a plan nobody gave a number to runs at the default, and
    the slot row never holds NULL.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader, the command executor and the live exit — on a
real SQLite file with the production schema.  Only three things are the
case's own: the clock, the executor the live exit is given (which
writes the decision down and starts nothing) and the facts the case
writes itself.

The tick is driven **synchronously**, one call at a time, so nothing
the case asserts on depends on a worker thread and on when it happened
to run.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_slot_priority_admission.py -v
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

#: How many plans may work at the same time in these cases.  The
#: capacity is ample on purpose: the only thing that could keep the
#: plan from being admitted is the queue itself.
CAPACITY = 2

#: The priority the case queues a plan with, to prove that the number
#: is carried over and not merely defaulted.
QUEUED_PRIORITY = -10


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


class RecordingSink:
    """The live exit, with every batch it was handed written down.

    The exit itself is the production one — :class:`ExecuteSink` — so
    the decisions the tick produces are handed over the way the live
    scheduler hands them over.  What the case owns is the **executor**
    the exit is given: it writes the decision down and starts nothing,
    so a case can watch what the scheduler decided without a process
    appearing out of it.

    Attributes:
        batches: every batch the exit was handed, in order.
    """

    def __init__(self) -> None:
        self._sink = ExecuteSink(self._execute)
        self.batches: List[List[Dict[str, Any]]] = []

    def _execute(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out — by writing it down."""
        return {}

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Write the batch down and hand it to the live exit."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return self._sink.apply(decisions)

    def admits(self) -> List[str]:
        """The plans that were admitted, in the order they were admitted."""
        return [
            str(decision.get("plan_id"))
            for batch in self.batches
            for decision in batch
            if decision.get("action") == "admit"
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


def _slot_priority(conn: sqlite3.Connection, plan_id: str) -> Any:
    """The priority the slot row of the plan holds.

    ``None`` means there is no slot row at all — a plan that holds no
    slot has no priority to read, and a case that reads the priority
    of a row that is not there would be reading nothing.
    """
    row = conn.execute(
        "SELECT priority FROM scheduler_slot WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        return None
    return row[0]


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
        sink: RecordingSink,
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
    sink = RecordingSink()
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
# The cases
# ---------------------------------------------------------------------------


def test_admission_writes_the_queued_priority_to_the_slot(
    db_factory: Any, plan_factory: Any
) -> None:
    """The number the plan was queued with is the number its slot holds.

    The queue row is deleted the moment the plan is admitted, so the
    admission is the only moment the number can be moved into the slot.
    A slot row of ``0`` for a plan that was queued at ``-10`` is a
    number that was dropped on the floor: the running plan can no
    longer be read for its priority, and the eviction rule has nothing
    to compare.
    """
    world = _world(db_factory, plan_factory, plan_ids=("bug",))
    try:
        _routing(world.conn, "bug", QUEUED)
        world.store.enqueue(world.conn, "bug", priority=QUEUED_PRIORITY)

        world.engine._run_tick()

        assert world.sink.admits() == ["bug"], (
            "the plan was not admitted, so its slot says nothing: {0!r}".format(
                world.sink.admits()
            )
        )
        assert _slot_priority(world.conn, "bug") == QUEUED_PRIORITY, (
            "the slot row does not carry the priority the plan was queued "
            "with; it carries {0!r}".format(_slot_priority(world.conn, "bug"))
        )
    finally:
        world.close()


def test_admission_without_a_priority_writes_zero(
    db_factory: Any, plan_factory: Any
) -> None:
    """A queue row without a priority gives a slot row of zero.

    The queue's priority column is ``NOT NULL DEFAULT 0``, so a queue
    row that names no priority carries the default — and a plan
    nobody gave a number to runs at that default.  The slot row must
    say ``0`` and not NULL: the eviction rule compares numbers, and
    NULL does not compare.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "p1", QUEUED)
        # The row is written without naming a priority, so it carries
        # the column's own default.
        world.conn.execute(
            "INSERT INTO scheduler_queue "
            "(plan_id, seq, enqueued_at, updated_at) VALUES (?, ?, ?, ?)",
            ("p1", 1, _iso(), _iso()),
        )

        world.engine._run_tick()

        assert world.sink.admits() == ["p1"], (
            "the plan was not admitted, so its slot says nothing: {0!r}".format(
                world.sink.admits()
            )
        )
        assert _slot_priority(world.conn, "p1") == 0, (
            "a plan nobody gave a priority to runs at 0; its slot row "
            "carries {0!r}".format(_slot_priority(world.conn, "p1"))
        )
    finally:
        world.close()
