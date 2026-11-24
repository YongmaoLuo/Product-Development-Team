"""Integration tests: the reconcile phases survive a locked database.

Background
----------
A tick has two reconcile phases, and they are what keep the scheduler's
two columns true: the plans that are **waiting** are in the queue, and
the plans that are **working** hold a slot.  Both phases write to the
state database, and the state database is shared with every other
writer in the process.  A write that arrives while somebody else holds
the write lock comes back as SQLite's ``database is locked`` — a
transient condition, not a fault of the plan it was about.

Before, one lock was enough to stop a phase: the exception came out of
the phase, the tick recorded it, and the queue and the capacity did not
move for that tick.  A plan that was waiting was then in **neither**
column — not running, not queued — and the operator watched it vanish
from the panel.

What this file holds down:

1. **One lock does not stop the queue.**  The phase waits the lock out
   and the queue is reconciled the way this tick meant it.
2. **One lock does not stop the capacity.**  Same for the slots: the
   ledger's reconciliation is retried and the count the tick decides on
   is the count the ledger says.
3. **A lock that does not go away is a deferral, not a failure.**  The
   phase says so and returns.  Nothing is marked failed, no command
   failure is written — a reconcile is not a command — and the two
   phases stay independent: the one that is deferred does not stop the
   other one.

The cases drive the **production** engine, the production store, the
production ledger and the production facts reader, on a real SQLite
file with the production schema.  Only the clock and the *fault* are
the case's own: the fault raises SQLite's own locked error, so the
engine sees exactly the exception the production sees.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from plan_scheduler.capacity import CapacityLedger
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

#: The phase a plan is in while it works.
EXECUTING = "executing"


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


class LockFault:
    """SQLite's locked error, a fixed number of times.

    The error is the one SQLite really raises when the write lock is
    held elsewhere: ``sqlite3.OperationalError`` carrying the phrase
    ``database is locked``.  Nothing else about it is simulated — the
    engine cannot tell this lock from a real one, which is the point.

    Attributes:
        calls: how many times the patched method was called.
    """

    def __init__(self, times: int = 1) -> None:
        self._left = max(0, int(times))
        self.calls = 0

    def fire(self) -> None:
        """Raise the locked error while the fault still has fires left."""
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise sqlite3.OperationalError("database is locked")


class BrokenFault:
    """An error that is **not** a lock — the reconcile is broken.

    A missing table is not going to be fixed by waiting, so the engine
    must not treat it as a busy database: the error has to come out of
    the phase.

    Attributes:
        calls: how many times the patched method was called.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def fire(self) -> None:
        """Raise the error, every time."""
        self.calls += 1
        raise self._error


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
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
    """Open a connection to the case's database with the production contract."""
    from state_machine.db.connection import open as open_db

    return open_db(path)


def _inject_fault(target: Any, name: str, fault: Any) -> Any:
    """Make ``target.name`` fail with the fault's error.

    The fault is installed on the **instance**, so the method it hides is
    the production one: every call that is not a fire goes straight
    through to the real implementation.

    Args:
        target: the object whose method is patched.
        name: the name of the method.
        fault: the fault to install — anything with a ``fire()``.

    Returns:
        The fault, so the case can see how many times the method was
        called.
    """
    original = getattr(target, name)

    def _failing(*args: Any, **kwargs: Any) -> Any:
        fault.fire()
        return original(*args, **kwargs)

    setattr(target, name, _failing)
    return fault


def _inject_lock(target: Any, name: str, times: int = 1) -> LockFault:
    """Make ``target.name`` fail with SQLite's locked error ``times`` times.

    Args:
        target: the object whose method is patched.
        name: the name of the method.
        times: how many calls raise the locked error.

    Returns:
        The fault, so the case can see how many times the method was
        called.
    """
    return _inject_fault(target, name, LockFault(times))


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing fact: the phase it is in."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        (plan_id, phase, _iso(BASE_EPOCH)),
    )


def _execution(
    conn: sqlite3.Connection,
    plan_id: str,
    project_dir: Optional[Path] = None,
) -> None:
    """Write the plan's execution fact: a live process that is working.

    The pid is the case's own: it is alive by definition, so the facts
    read the plan as **working** — the plan holds compute, and the
    ledger owes it a slot.
    """
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, NULL, ?, 'running', ?)",
        (
            plan_id,
            str(project_dir) if project_dir is not None else None,
            os.getpid(),
            _iso(BASE_EPOCH),
        ),
    )


@dataclass
class World:
    """What a case drives: the engine and the collaborators around it.

    Attributes:
        engine: the engine under test.
        store: the scheduler store the engine writes through.
        ledger: the capacity ledger — the authority on who holds compute.
        facts: the reader that derives the plan facts.
        conn: the case's connection — the engine's own, so the case
            reads what the engine committed.
        clock: the clock the case moves by hand.
    """

    engine: Any
    store: Any
    ledger: Any
    facts: Any
    conn: sqlite3.Connection
    clock: FakeClock

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def _world(
    db_factory: Any,
    plan_factory: Any,
    *,
    plan_ids: Tuple[str, ...] = ("p1",),
) -> World:
    """Build the world a case drives.

    The plans are real plan directories, so the facts reader sees what
    the production facts reader sees; the phase a plan is in is written
    by the case itself, because a state that has to be reached by
    waiting is a state no case may depend on.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(plan_id=plan_id)

    conn = _open_db(database.path)
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock, conn=conn)
    config_loader = StaticConfigLoader(
        enabled=True,
        capacity=1,
        min_runtime_seconds=600.0,
        tick_seconds=0.01,
        command_timeout_seconds=10.0,
    )
    engine = PlanSchedulerEngine(
        config_loader=config_loader,
        store=store,
        facts=facts,
        ledger=ledger,
        clock=clock,
        conn=conn,
        plans_dir=_plans_root(),
    )
    return World(engine, store, ledger, facts, conn, clock)


def _queue_rows(conn: sqlite3.Connection) -> List[str]:
    """The plans in the queue table, in the queue's own order."""
    return [
        str(row["plan_id"])
        for row in SqliteSchedulerStore().list_queue(conn)
    ]


def _slot_rows(conn: sqlite3.Connection) -> List[str]:
    """The plans that hold a slot, in the store's own order."""
    return [
        str(row["plan_id"])
        for row in SqliteSchedulerStore().list_slots(conn)
    ]


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_queue_reconcile_survives_one_lock(db_factory, plan_factory):
    """One lock does not stop the queue.

    The write that was locked is tried again, and the queue is
    reconciled the way this tick meant it: every plan that is waiting is
    in the queue, in the queue's order.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2"))
    try:
        for plan_id in ("p1", "p2"):
            _routing(world.conn, plan_id, QUEUED)
        world.engine._phase_read_state()

        # The queue's write is the thing a busy database refuses.
        fault = _inject_lock(world.store, "enqueue", times=1)

        world.engine._phase_reconcile_queue()

        assert fault.calls >= 2, (
            "the locked write must be tried again, not given up on: "
            f"{fault.calls} call(s)"
        )
        assert _queue_rows(world.conn) == ["p1", "p2"]
        assert [
            str(entry["plan_id"]) for entry in world.engine._state["queued"]
        ] == ["p1", "p2"]
        assert [
            str(entry["plan_id"]) for entry in world.engine._state["queue"]
        ] == ["p1", "p2"]
        # A reconcile is not a command: nothing was written to the
        # command log, and no plan was marked failed.
        assert world.engine.errors == []
        assert world.conn.execute(
            "SELECT COUNT(*) FROM scheduler_command"
        ).fetchone()[0] == 0
    finally:
        world.close()


def test_capacity_reconcile_survives_one_lock(db_factory, plan_factory):
    """One lock does not stop the capacity.

    The ledger's reconciliation is tried again, and the count the tick
    decides on is the count the ledger says: the plan that is working
    holds a slot, and the state says it is running.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1",))
    try:
        _routing(world.conn, "p1", EXECUTING)
        _execution(world.conn, "p1")
        world.engine._phase_read_state()

        # The ledger's own write is the thing a busy database refuses.
        fault = _inject_lock(world.ledger, "reconcile", times=1)

        world.engine._phase_reconcile_capacity()

        assert fault.calls == 2, (
            "the locked reconciliation must be tried exactly once more: "
            f"{fault.calls} call(s)"
        )
        assert world.engine._state["used"] == 1
        assert [
            str(entry["plan_id"]) for entry in world.engine._state["running"]
        ] == ["p1"]
        assert _slot_rows(world.conn) == ["p1"]
        assert world.engine.errors == []
    finally:
        world.close()


def test_reconcile_gives_up_quietly(db_factory, plan_factory, caplog):
    """A lock that does not go away is a deferral, not a failure.

    The phase says it deferred and returns: no exception comes out, no
    plan is marked failed, and no command failure is written.  The two
    phases stay independent — the one that is deferred does not stop the
    other one from doing its work.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2"))
    try:
        # p1 is waiting, p2 is working: one of each column.
        _routing(world.conn, "p1", QUEUED)
        _routing(world.conn, "p2", EXECUTING)
        _execution(world.conn, "p2")
        world.engine._phase_read_state()

        with caplog.at_level(logging.WARNING, logger="plan_scheduler.engine"):
            # The queue's write never succeeds.
            queue_fault = _inject_lock(world.store, "enqueue", times=99)
            world.engine._phase_reconcile_queue()

            # The capacity is a different phase: it goes on working.
            world.engine._phase_reconcile_capacity()

            # And the capacity's own write never succeeds either.
            capacity_fault = _inject_lock(world.ledger, "reconcile", times=99)
            world.engine._phase_reconcile_capacity()

        assert queue_fault.calls >= 2
        assert capacity_fault.calls >= 2
        assert "reconcile_queue_deferred" in caplog.text
        assert "reconcile_capacity_deferred" in caplog.text

        # The deferred phase changed nothing.
        assert _queue_rows(world.conn) == []

        # The capacity was not held up by the queue's deferral: it did
        # its own work, and the plan that is working holds its slot.
        assert world.engine._state["used"] == 1
        assert _slot_rows(world.conn) == ["p2"]

        # Nothing was marked failed, and no command failure was written:
        # a reconcile is not a command.
        assert world.engine.errors == []
        phases = {
            str(row[0]): str(row[1])
            for row in world.conn.execute(
                "SELECT plan_id, current_phase FROM plan_routing"
            )
        }
        assert phases == {"p1": QUEUED, "p2": EXECUTING}
        assert world.conn.execute(
            "SELECT COUNT(*) FROM scheduler_command WHERE outcome = 'failed'"
        ).fetchone()[0] == 0
    finally:
        world.close()


def test_non_locked_error_still_bubbles(db_factory, plan_factory, caplog):
    """An error that is not a lock is not swallowed.

    A missing table is not going to be fixed by waiting, and a
    reconcile that cannot do its work must be seen: the error comes out
    of the phase the way it always did, it is not retried, and it is not
    dressed up as a deferral.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1",))
    try:
        _routing(world.conn, "p1", QUEUED)
        world.engine._phase_read_state()

        fault = _inject_fault(
            world.store,
            "enqueue",
            BrokenFault(
                sqlite3.OperationalError("no such table: scheduler_queue")
            ),
        )

        with caplog.at_level(logging.WARNING, logger="plan_scheduler.engine"):
            with pytest.raises(sqlite3.OperationalError) as raised:
                world.engine._phase_reconcile_queue()

        assert "no such table" in str(raised.value)
        assert fault.calls == 1, "a broken reconcile is not tried again"
        assert "deferred" not in caplog.text
    finally:
        world.close()
