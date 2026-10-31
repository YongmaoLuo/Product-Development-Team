"""Integration tests: the pruning writes the scheduler's own facts down.

Background
----------
A failure that will never go away — the plan is not there, the plan is
archived, the request conflicts with the plan's own state — is a
**permanent** failure (see
:func:`plan_scheduler.engine.is_permanent_failure`).  Retrying one of
those buys nothing, and leaving the plan in the queue buys less: the
plan is not there, so it is not waiting, and the line it holds is a
line that nobody walks.

So the scheduler **prunes** such a plan, and the pruning is three
writes and a warning:

* the plan's **queue row** goes — it is not waiting any more;
* the plan's **slot** goes — but only when the plan was *admitted*: a
  plan that failed to be **stopped** may still be running, and the
  scheduler does not give back a room somebody is still working in;
* the plan's **phase** becomes ``stopped`` — the terminal word for a
  plan that will not run again.

The phase is the state machine's word, so it is written through the
state machine's own repository — the scheduler does not spell the
transition in SQL of its own.  And a phase that cannot be written is
not a reason to keep a ghost in the queue: the row goes anyway, and
the failure is logged.

What is pinned here
-------------------
``test_prune_removes_queue_and_slot``
    The plan is out of the queue and holds no slot any more.
``test_prune_marks_phase_stopped``
    The phase is ``stopped`` and the history says the plan was
    ``queued``.
``test_phase_write_failure_still_removes_row``
    A plan with no routing row at all loses its queue row anyway — the
    phase write fails, the row still goes.
``test_stop_failure_keeps_the_slot``
    A plan that failed to be *stopped* keeps its slot: it may still be
    running.
``test_prune_warns_once``
    The pruning says so, in one warning, naming the plan.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader and the command executor are the real ones,
on a real SQLite file with the production schema.  Only three things
are the case's own: the clock, the sink (which writes the decisions
down and starts nothing) and the facts the case writes itself.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_prune_writes.py -v
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The moment every case starts at.  It is written out rather than
#: taken from the wall clock: the clock is injected, and a case that
#: read the real time would be a case whose answer depends on when it
#: ran.
BASE_EPOCH = 7000.0

#: The phase a plan is in while it waits for its turn to run.
QUEUED = "queued"

#: The phase a plan is in when it will not run again.
STOPPED = "stopped"

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


class RecordingSink:
    """The exit that only says what it decided — the dry-run one."""

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Write the decisions down and hand back what they were."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch


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


def _phase_of(conn: sqlite3.Connection, plan_id: str) -> Any:
    """The phase the plan carries in the routing table."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _completed_phases(conn: sqlite3.Connection, plan_id: str) -> List[str]:
    """The phases the plan has been through."""
    row = conn.execute(
        "SELECT completed_phases FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return []
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _permanent_error(plan_id: str) -> BaseException:
    """The failure that will never go away: the plan is not there."""
    from state_machine.repositories.routing_repository import PlanNotFoundError

    return PlanNotFoundError(plan_id)


def _prune(
    engine: Any,
    plan_id: str = "ghost",
    action: str = "admit",
    error: Any = None,
) -> None:
    """Hand the engine the outcome of a failure that will not go away."""
    outcome = {
        "action": action,
        "plan_id": plan_id,
        "ok": False,
        "error": error if error is not None else _permanent_error(plan_id),
    }
    engine._prune_failed_plan(outcome)


class World:
    """What a case drives: the engine and the collaborators around it.

    Attributes:
        engine: the engine under test.
        store: the scheduler store the case reads through.
        sink: the sink the tick's decisions are written to.
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
    db_factory: Any, plan_factory: Any, plan_ids: Any = ("p1",)
) -> World:
    """Build the world a case drives.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader and the command executor.  Only the clock
    and the sink are the case's own.
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
        capacity=CAPACITY,
        min_runtime_seconds=0.0,
        clock=clock,
    )
    sink = RecordingSink()
    engine = PlanSchedulerEngine(
        config_loader=StaticConfigLoader(
            capacity=CAPACITY,
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
# 1. The pruning takes the plan out of the queue
# ---------------------------------------------------------------------------


def test_prune_removes_queue_and_slot(
    db_factory: Any, plan_factory: Any
) -> None:
    """The plan is out of the queue and holds no slot any more.

    The plan failed in a way that will never go away, so it is not
    waiting any more: its queue row goes, and the room it took goes
    with it — the plan was **admitted**, so the slot was its own.
    """
    world = _world(db_factory, plan_factory)
    try:
        world.store.enqueue(world.conn, "ghost")
        world.store.upsert_slot(world.conn, "ghost", _iso())

        _prune(world.engine, "ghost", "admit")

        assert _queue_ids(world.conn) == [], (
            "the plan that will never run is still in the queue: "
            "{0!r}".format(_queue_ids(world.conn))
        )
        assert _slot_ids(world.conn) == [], (
            "the plan that will never run still holds a slot: "
            "{0!r}".format(_slot_ids(world.conn))
        )
    finally:
        world.close()


def test_stop_failure_keeps_the_slot(db_factory: Any, plan_factory: Any) -> None:
    """A plan that failed to be stopped keeps its slot.

    The two failures are not the same.  A plan that failed to be
    **admitted** never got the room: giving it back is right.  A plan
    that failed to be **stopped** may still be running — the stop is
    what did not work — and the scheduler does not give back a room
    somebody is still working in.
    """
    world = _world(db_factory, plan_factory)
    try:
        world.store.enqueue(world.conn, "ghost")
        world.store.upsert_slot(world.conn, "ghost", _iso())

        _prune(world.engine, "ghost", "stop")

        assert _queue_ids(world.conn) == [], (
            "the plan is not waiting any more: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert _slot_ids(world.conn) == ["ghost"], (
            "the slot of a plan that may still be running was given away: "
            "{0!r}".format(_slot_ids(world.conn))
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. The pruning puts the phase where it will never move again
# ---------------------------------------------------------------------------


def test_prune_marks_phase_stopped(db_factory: Any, plan_factory: Any) -> None:
    """The phase is ``stopped`` and the history says ``queued``.

    The phase is the state machine's word, so it is written through the
    state machine's own repository — and the history is maintained by
    the same write, so the row cannot say "stopped" and forget how it
    got there.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "ghost", QUEUED)
        world.store.enqueue(world.conn, "ghost")

        _prune(world.engine, "ghost", "admit")

        assert _phase_of(world.conn, "ghost") == STOPPED, (
            "the plan that will never run is not in a terminal phase: "
            "{0!r}".format(_phase_of(world.conn, "ghost"))
        )
        assert QUEUED in _completed_phases(world.conn, "ghost"), (
            "the history does not say the plan was queued: {0!r}".format(
                _completed_phases(world.conn, "ghost")
            )
        )
    finally:
        world.close()


def test_phase_write_failure_still_removes_row(
    db_factory: Any, plan_factory: Any
) -> None:
    """A plan with no routing row loses its queue row anyway.

    There is no phase to move when there is no row: the write fails,
    the failure is logged, and the queue row goes all the same.  A
    ghost that cannot be written down must not stay in the line.
    """
    world = _world(db_factory, plan_factory)
    try:
        world.store.enqueue(world.conn, "ghost")
        world.store.upsert_slot(world.conn, "ghost", _iso())

        _prune(world.engine, "ghost", "admit")

        assert _queue_ids(world.conn) == [], (
            "the ghost is still in the queue because its phase could not "
            "be written: {0!r}".format(_queue_ids(world.conn))
        )
        assert _slot_ids(world.conn) == [], (
            "the ghost still holds a slot: {0!r}".format(_slot_ids(world.conn))
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 3. The pruning says so
# ---------------------------------------------------------------------------


def test_prune_warns_once(db_factory: Any, plan_factory: Any, caplog: Any) -> None:
    """The pruning is said out loud, once, naming the plan.

    A plan that is dropped without a word is a plan nobody can ask
    about, and the warning is the only trace the pruning leaves.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "ghost", QUEUED)
        world.store.enqueue(world.conn, "ghost")

        with caplog.at_level(logging.WARNING):
            _prune(world.engine, "ghost", "admit")

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        pruned = [line for line in warnings if "pruned plan" in line]
        assert len(pruned) == 1, (
            "the pruning is said exactly once: {0!r}".format(warnings)
        )
        assert "ghost" in pruned[0], (
            "the warning names the plan: {0!r}".format(pruned[0])
        )
        assert "admit" in pruned[0], (
            "the warning names the action that failed: {0!r}".format(
                pruned[0]
            )
        )
        assert "PlanNotFoundError" in pruned[0], (
            "the warning names the failure: {0!r}".format(pruned[0])
        )
    finally:
        world.close()
