"""End to end: the ghost at the head of the queue does not stop the line.

Background
----------
The accident this pins down is a queue that cannot move.

A ``queued`` row in ``plan_routing`` says that a plan is waiting to
run.  It does not say that the plan still *exists*: a plan whose
directory is gone — the tree was cleaned up, the import was rolled back
— leaves its row behind, and the row keeps saying "queued" for ever,
because nothing else in the system removes it.  That is a **ghost**.

The ghost took the first number in the queue, so it sorts ahead of
every real plan.  A scheduler that waits for the head of its line waits
for the ghost, and every plan behind it waits with it: the line is
full, the capacity is free, and **nothing runs**.  The ghost does not
heal by itself, and the operator sees a queue with no head.

Three things have to hold for the line to move again:

* the **facts** layer does not answer "waiting" for a plan that is not
  there — the ghost is dropped from the answer and reported **once**,
  so the warning is a report and not a heartbeat;
* the **reconcile** takes the ghost's row out of the queue table, so
  the row does not sit there for ever;
* the **view** the panel reads never shows the ghost, so the operator
  is never told about a plan that cannot run.

What is pinned here
-------------------
``test_ghost_head_does_not_block_next_plan``
    The plan behind the ghost is admitted in the **same** tick and
    holds a slot: the ghost does not hold the line.
``test_ghost_row_removed_and_warned_once``
    The ghost's row is gone, the operator heard about it **once** — not
    once per tick — and the panel's view shows no ghost.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader, the command executor and the live exit — on
a real SQLite file with the production schema.  Only three things are
the case's own: the clock, the executor the live exit is given (which
writes the decision down and starts nothing) and the facts the case
writes itself.

The tick is driven **synchronously**, one call at a time, so nothing
the case asserts on depends on a worker thread and on when it happened
to run.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_ghost_queue_head.py -v
"""

from __future__ import annotations

import logging
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
#: capacity is **ample** on purpose: the queue is not full, so the only
#: thing that could keep the plan behind the ghost from running is the
#: ghost itself.
CAPACITY = 2

#: How many ticks a case drives when it wants to see that the ghost is
#: not a heartbeat.
TICKS = 3


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


def _queue_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that have a queue row, in the production order."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_queue(conn)]


def _slot_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that hold a concurrency slot."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_slots(conn)]


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

    def panel_queue(self) -> List[str]:
        """The plans the panel's queue view shows, in the panel's order.

        The snapshot does not carry the plan id — it carries the row's
        numbers — so the ids are read back from the table the view was
        built from: the view's ``seq`` is the row's ``seq``.  A row the
        table does not have any more is reported by its number, so a
        view that shows a row nobody has is visible as such.
        """
        by_seq = {
            int(row.get("seq")): str(row.get("plan_id"))
            for row in self.store.list_queue(self.conn)
        }
        return [
            by_seq.get(int(entry.get("seq") or 0), "?seq={0}".format(entry.get("seq")))
            for entry in self.engine.snapshot()["queue"]
        ]


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


def _ghost_at_the_head(world: World) -> None:
    """Put a ghost at the head of the line, and a real plan behind it.

    The ghost is a plan whose routing row says "queued" and whose queue
    row is the first one — it took the first number, so it sorts ahead
    of the real plan — but **there is no directory for it**: the plan
    is not there any more, only its row is.

    The real plan is there: it has a directory, a routing row and a
    queue row behind the ghost's.
    """
    _routing(world.conn, "ghost", QUEUED)
    _routing(world.conn, "p1", QUEUED)
    world.store.enqueue(world.conn, "ghost")
    world.store.enqueue(world.conn, "p1")


# ---------------------------------------------------------------------------
# 1. The ghost does not hold the line
# ---------------------------------------------------------------------------


def test_ghost_head_does_not_block_next_plan(
    db_factory: Any, plan_factory: Any
) -> None:
    """The plan behind the ghost runs, in the same tick.

    The ghost is at the head of the queue and the capacity is free, so
    a scheduler that waited for its head would wait for ever.  The
    ghost is not a plan, so it is not in the line: the tick admits the
    plan behind it and gives it a slot.
    """
    world = _world(db_factory, plan_factory)
    try:
        _ghost_at_the_head(world)

        world.engine._run_tick()

        assert world.sink.admits() == ["p1"], (
            "the ghost is not a plan, so it is not in the line and it does "
            "not hold it: {0!r}".format(world.sink.admits())
        )
        assert _queue_ids(world.conn) == ["p1"], (
            "the ghost's row is still in the queue table: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert _slot_ids(world.conn) == ["p1"], (
            "the plan behind the ghost does not hold a slot: {0!r}".format(
                _slot_ids(world.conn)
            )
        )
        assert world.panel_queue() == ["p1"], (
            "the panel is told about a plan that cannot run: {0!r}".format(
                world.panel_queue()
            )
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. The ghost's row goes, and the operator hears about it once
# ---------------------------------------------------------------------------


def test_ghost_row_removed_and_warned_once(
    db_factory: Any, plan_factory: Any, caplog: Any
) -> None:
    """The row is gone, the warning is said once, and the view is clean.

    Three ticks, because a ghost that is only cleaned up once is a
    heartbeat: the scheduler reads the queue over and over, and a
    warning that repeated would say the same thing over and over and
    drown the log.  So: exactly one warning, no ghost's row, and no
    ghost in the view the panel reads.
    """
    world = _world(db_factory, plan_factory)
    try:
        _ghost_at_the_head(world)

        with caplog.at_level(logging.WARNING, logger="plan_scheduler.facts"):
            for _ in range(TICKS):
                world.engine._run_tick()
            panel = world.panel_queue()

        warnings = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and "no plan directory" in record.getMessage()
        ]

        assert len(warnings) == 1, (
            f"{TICKS} ticks, one warning — got {len(warnings)}: "
            f"{[record.getMessage() for record in warnings]}"
        )
        assert "ghost" in warnings[0].getMessage()
        assert "ghost" not in _queue_ids(world.conn), (
            "the ghost's row is still in the queue table: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert "ghost" not in panel, (
            "the panel is told about a plan that cannot run: {0!r}".format(panel)
        )
    finally:
        world.close()