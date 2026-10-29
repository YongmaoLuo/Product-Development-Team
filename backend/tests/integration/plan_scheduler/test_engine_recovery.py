"""Integration tests: the engine's recovery after a restart.

Background
----------
A restart is the moment the scheduler's own facts are put to the test:
the queue and the slots are the two things it owns, and both of them
survive the process.  The recovery is what makes them mean the same
thing again once the process is back — and it is the only place where a
scheduler may quietly change the operator's mind.

This file holds down the three rules the recovery follows:

1. **The queue is used as it stands.**  The rows are neither renumbered
   nor reordered, so the line the panel shows after the restart is the
   line the scheduler was running before it stopped.
2. **The slots are reconciled with the plan facts.**  A plan that is
   working is **adopted** and timed from *now* — it has not had its
   turn yet, so it is protected for a whole M — and a row nobody holds
   any more is deleted.
3. **A waiting plan without a queue row joins the tail of the line.**
   The order is the plan's own (``updated_at`` and then the plan id),
   and the number it is given is ``MAX(seq) + 1``.

The recovery decides nothing and starts nothing: the first tick after it
is an ordinary tick, and a plan that is already working is not in
``queued``, so it cannot be admitted a second time.

The cases drive the **production** engine — the store, the ledger, the
facts reader and the command executor are the real ones, on a real
SQLite file with the production schema.  Only three things are the
case's own: the clock, the sink (which records instead of executing) and
the lifecycle (which records instead of stopping anything).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The moment every case starts at.
BASE_EPOCH = 1_700_000_000.0

#: The protection a young run has — ``M``.  The default is 600
#: seconds; a case writes it out so that a case which means "ten
#: minutes" does not have to know that.
MIN_RUNTIME = 600.0

#: How long the case's child process — the plan that is really
#: working — stays alive.
CHILD_LIFETIME = 30


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

    def decisions(self) -> List[Dict[str, Any]]:
        """Every decision the sink was handed, in order."""
        return [decision for batch in self.batches for decision in batch]

    def admitted(self) -> List[str]:
        """The plans the tick decided to admit, in order."""
        return [
            str(decision.get("plan_id"))
            for decision in self.decisions()
            if decision.get("action") == "admit"
        ]


class LifecycleResult:
    """What the lifecycle answers: the work was done."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.ok = True
        self.detail = "ok"


class RecordingLifecycle:
    """A lifecycle that writes down what it was asked to do.

    It never stops anything for real, so a case can ask whether the
    scheduler *tried* to interrupt somebody.
    """

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


def _wait_until(
    predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.005
) -> bool:
    """Wait for the predicate to come true; return whether it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class World:
    """What a case drives: the engine and the collaborators around it.

    Attributes:
        engine: the engine under test — a probe, so the case can see
            the order the recovery and the phases ran in.
        store: the scheduler store the case reads through.
        sink: the recording sink the tick's decisions go to.
        lifecycle: the recording lifecycle the commands call.
        ledger: the capacity ledger — the authority on who may be
            interrupted.
        clock: the clock the case moves by hand.
        read_conn: the case's own connection, opened on the same file
            as the engine's — so the case reads committed state and
            never joins the worker's transaction.
    """

    def __init__(
        self,
        engine: Any,
        store: Any,
        sink: RecordingSink,
        lifecycle: RecordingLifecycle,
        ledger: Any,
        clock: FakeClock,
        read_conn: sqlite3.Connection,
    ) -> None:
        self.engine = engine
        self.store = store
        self.sink = sink
        self.lifecycle = lifecycle
        self.ledger = ledger
        self.clock = clock
        self.read_conn = read_conn

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.read_conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def _probe_engine(**kwargs: Any) -> Any:
    """Return an engine that writes down when it recovered.

    The probe is a subclass: it intercepts the recovery as it is entered
    and writes it down, so the order it records is the order the engine
    really ran in — not the order the engine says it ran in.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    class ProbeEngine(PlanSchedulerEngine):
        """The engine, watched."""

        def __init__(self, *args: Any, **kw: Any) -> None:
            super().__init__(*args, **kw)
            self.events: List[Tuple[str, str]] = []
            self._probe_lock = threading.Lock()

        def _recover(self) -> Dict[str, Any]:
            """Write the recovery down, then let the engine recover."""
            with self._probe_lock:
                self.events.append(("phase", "recover"))
            return super()._recover()

    return ProbeEngine(**kwargs)


def _world(
    db_factory: Any,
    plan_factory: Any,
    *,
    plan_ids: Tuple[str, ...] = ("p1",),
    capacity: int = 1,
    min_runtime_seconds: float = MIN_RUNTIME,
    tick_seconds: float = 0.01,
) -> World:
    """Build the world a case drives.

    The plans are real plan directories; the facts about them — the
    phase, the subprocess, the queue row — are written by the case
    itself, because a restart is a state that has to be set up, not
    something a case can wait for.
    """
    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(plan_id=plan_id)

    conn = _open_db(database.path)
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    lifecycle = RecordingLifecycle()
    commands = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=min_runtime_seconds,
        clock=clock,
    )
    sink = RecordingSink()

    engine = _probe_engine(
        config_loader=StaticConfigLoader(
            capacity=capacity,
            min_runtime_seconds=min_runtime_seconds,
            tick_seconds=tick_seconds,
            command_timeout_seconds=10.0,
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=sink,
        clock=clock,
    )
    return World(engine, store, sink, lifecycle, ledger, clock, _open_db(database.path))


def _routing(
    conn: sqlite3.Connection,
    plan_id: str,
    phase: str,
    updated_at: Optional[float] = None,
) -> None:
    """Write the plan's routing fact: the phase it is in, and when."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        (plan_id, phase, _iso(BASE_EPOCH if updated_at is None else updated_at)),
    )


def _execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and its state."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, NULL, ?, ?, ?)",
        (plan_id, None, exec_pid, exec_status, _iso(BASE_EPOCH)),
    )


def _queue_row(
    conn: sqlite3.Connection,
    plan_id: str,
    seq: int,
    priority: int = 0,
    promote_order: Optional[int] = None,
) -> None:
    """Write a queue row: where the plan stands in the line."""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            int(seq),
            int(priority),
            promote_order,
            _iso(BASE_EPOCH),
            _iso(BASE_EPOCH),
        ),
    )


def _slot(conn: sqlite3.Connection, plan_id: str, acquired_at: str) -> None:
    """Write a slot row: the moment the plan took its slot."""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_slot (plan_id, acquired_at, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, acquired_at, acquired_at),
    )


def _queue_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The queue rows, as the table holds them, in the order of ``seq``."""
    rows = conn.execute(
        "SELECT plan_id, seq, priority, promote_order, enqueued_at, updated_at "
        "FROM scheduler_queue ORDER BY seq"
    ).fetchall()
    return [
        {
            "plan_id": row[0],
            "seq": row[1],
            "priority": row[2],
            "promote_order": row[3],
            "enqueued_at": row[4],
            "updated_at": row[5],
        }
        for row in rows
    ]


def _slots(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """The held slots, by plan id."""
    rows = conn.execute(
        "SELECT plan_id, acquired_at, updated_at FROM scheduler_slot"
    ).fetchall()
    return {
        row[0]: {"plan_id": row[0], "acquired_at": row[1], "updated_at": row[2]}
        for row in rows
    }


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep({0})".format(CHILD_LIFETIME)]
    )
    return int(process.pid)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_queue_order_unchanged_after_restart(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A restart neither renumbers nor reorders the queue.

    The queue is the operator's line: the order the panel shows and the
    order the admission check reads are the same order, and a restart
    that renumbered the rows would turn one into the other — the plan
    the operator promoted would stop running first.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2", "p3"))
    conn = world.read_conn
    live = _live_pid(case_registry)

    # The state the previous run left behind: p1 is working, p2 and p3
    # are waiting, and the queue holds all three.
    _routing(conn, "p1", "executing")
    _execution(conn, "p1", exec_pid=live, exec_status="running")
    _routing(conn, "p2", "queued")
    _routing(conn, "p3", "queued")
    _queue_row(conn, "p3", seq=5, priority=1, promote_order=-1)
    _queue_row(conn, "p2", seq=2, priority=5, promote_order=None)
    _queue_row(conn, "p1", seq=7, priority=0, promote_order=None)

    before = _queue_rows(conn)
    before_by_plan = {row["plan_id"]: row for row in before}

    engine = world.engine
    engine._recover()

    assert _queue_rows(conn) == before, (
        "the recovery changed the queue: {0!r} -> {1!r}".format(
            before, _queue_rows(conn)
        )
    )

    engine.start()
    try:
        assert _wait_until(lambda: engine.ticks >= 1), (
            "the engine never ran a tick: {0!r}".format(engine.events)
        )
    finally:
        engine.stop()

    # The recovery runs before the first tick — the state is put right
    # before the scheduler starts acting on it.
    assert engine.events[0] == ("phase", "recover"), (
        "the recovery did not run first: {0!r}".format(engine.events)
    )

    after = {row["plan_id"]: row for row in _queue_rows(conn)}
    for plan_id in ("p2", "p3"):
        assert after.get(plan_id) == before_by_plan[plan_id], (
            "the row of {0} changed across the restart: {1!r} -> {2!r}".format(
                plan_id, before_by_plan[plan_id], after.get(plan_id)
            )
        )

    # The line is still the line: the promoted plan runs first.
    assert [row["plan_id"] for row in world.store.list_queue(conn)] == ["p3", "p2"], (
        "the queue is in another order: {0!r}".format(
            world.store.list_queue(conn)
        )
    )

    # A plan that is already working is not waiting, so it cannot be
    # admitted a second time.
    assert "p1" not in world.sink.admitted(), (
        "the working plan was admitted again: {0!r}".format(world.sink.admitted())
    )


def test_adopted_plan_gets_now(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A working plan without a slot row is adopted with the clock at now.

    The plan was started outside the scheduler — through the lifecycle
    API, or by the run that was interrupted by the restart.  The
    scheduler adopts it, and an adopted plan is timed from the moment it
    was adopted: it has not had its turn yet, so it is protected for a
    whole M.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1",))
    conn = world.read_conn
    live = _live_pid(case_registry)

    _routing(conn, "p1", "executing")
    _execution(conn, "p1", exec_pid=live, exec_status="running")

    assert _slots(conn) == {}, "the case did not start without a slot row"

    world.engine._recover()

    slots = _slots(conn)
    assert "p1" in slots, "the working plan was not adopted: {0!r}".format(slots)
    assert slots["p1"]["acquired_at"] == _iso(BASE_EPOCH), (
        "the adopted plan was not timed from now: {0!r}".format(slots["p1"])
    )


def test_adopted_plan_is_retimed_on_restart(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A working plan that already had a slot row is timed from now too.

    The row survived the restart, and it says the plan took its slot an
    hour ago.  Carrying that timestamp over would put the plan into the
    eviction candidates the moment it was adopted — the takeover would
    evict the very work it had just taken over.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1",))
    conn = world.read_conn
    live = _live_pid(case_registry)

    _routing(conn, "p1", "executing")
    _execution(conn, "p1", exec_pid=live, exec_status="running")
    _slot(conn, "p1", _iso(BASE_EPOCH - 3600))

    world.engine._recover()

    slots = _slots(conn)
    assert slots["p1"]["acquired_at"] == _iso(BASE_EPOCH), (
        "the adopted plan kept its old clock: {0!r}".format(slots["p1"])
    )


def test_adopted_plan_not_evictable_immediately(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A plan adopted by the restart may not be evicted straight away.

    The operator asks for a plan to be started; the capacity is full,
    so the start may interrupt somebody.  The plan the restart adopted
    is not that somebody: it has been running for no time at all, so it
    is inside its protected window and the start waits.
    """
    world = _world(
        db_factory,
        plan_factory,
        plan_ids=("p1", "p2"),
        capacity=1,
        min_runtime_seconds=MIN_RUNTIME,
    )
    conn = world.read_conn
    live = _live_pid(case_registry)

    _routing(conn, "p1", "executing")
    _execution(conn, "p1", exec_pid=live, exec_status="running")
    # It had been running for an hour before the restart.
    _slot(conn, "p1", _iso(BASE_EPOCH - 3600))
    _routing(conn, "p2", "queued")

    engine = world.engine
    engine.start()
    try:
        assert _wait_until(lambda: engine.ticks >= 1), (
            "the engine never ran a tick: {0!r}".format(engine.events)
        )
        result = engine.submit_command("start", "p2")
    finally:
        engine.stop()

    assert result.outcome == "blocked_by_capacity", (
        "the start went through: {0!r}".format(result.outcome)
    )
    assert ("stop_plan", "p1") not in world.lifecycle.calls, (
        "the adopted plan was interrupted: {0!r}".format(world.lifecycle.calls)
    )
    assert world.ledger.evictable(BASE_EPOCH, MIN_RUNTIME) == [], (
        "the adopted plan may be evicted straight away"
    )


def test_stale_slot_removed_on_recovery(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A slot row nobody holds any more is deleted by the recovery.

    The plan stopped while the scheduler was not looking — or it
    stopped before the restart and the row outlived it.  The row is a
    reservation nobody holds, and it would keep counting a plan that
    is not working.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2"))
    conn = world.read_conn

    _routing(conn, "p1", "stopped")
    _slot(conn, "p1", _iso(BASE_EPOCH - 60))
    _routing(conn, "p2", "queued")

    assert "p1" in _slots(conn), "the case did not start with a stale row"

    world.engine._recover()

    slots = _slots(conn)
    assert "p1" not in slots, (
        "the stale slot row survived the recovery: {0!r}".format(slots)
    )
    assert slots == {}, "the recovery left a slot behind: {0!r}".format(slots)


def test_queued_without_row_backfilled_at_tail(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A waiting plan without a queue row joins the tail of the line.

    The plan was imported before the scheduler ever ran — the phase
    lives in ``plan_routing`` and the queue row does not exist yet.  It
    joins the line at the tail, behind the plans that are already in it,
    and it gets the number after the highest one there is.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2", "p3"))
    conn = world.read_conn

    # p2 is waiting and already has its row.
    _routing(conn, "p2", "queued")
    _queue_row(conn, "p2", seq=4, priority=0, promote_order=None)

    # p3 and p1 are waiting without a row.  They are written in the
    # opposite order of their ``updated_at``, so a backfill that took
    # the rows as it found them would put them in the wrong order.
    _routing(conn, "p3", "queued", updated_at=BASE_EPOCH + 120)
    _routing(conn, "p1", "queued", updated_at=BASE_EPOCH + 60)

    world.engine._recover()

    rows = {row["plan_id"]: row for row in _queue_rows(conn)}
    assert set(rows) == {"p1", "p2", "p3"}, (
        "the waiting plans are not all in the line: {0!r}".format(rows)
    )
    assert rows["p1"]["seq"] == 5, (
        "the backfilled plan did not take the next number: {0!r}".format(rows["p1"])
    )
    assert rows["p3"]["seq"] == 6, (
        "the second backfilled plan did not take the number after it: "
        "{0!r}".format(rows["p3"])
    )

    # The tail: the plan that has been waiting longest is behind the
    # one that has been waiting second longest.
    assert [row["plan_id"] for row in world.store.list_queue(conn)] == [
        "p2",
        "p1",
        "p3",
    ], "the backfilled plans are not at the tail: {0!r}".format(
        world.store.list_queue(conn)
    )


def test_empty_database_recovers_to_nothing(
    db_factory: Any, plan_factory: Any
) -> None:
    """An empty database recovers into an empty queue and no slots."""
    world = _world(db_factory, plan_factory, plan_ids=())

    world.engine._recover()

    assert _queue_rows(world.read_conn) == [], "the recovery invented a queue"
    assert _slots(world.read_conn) == {}, "the recovery invented a slot"
