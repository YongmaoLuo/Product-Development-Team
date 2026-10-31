"""Integration tests: a transient failure is recorded, not pruned.

Background
----------
Not every failure is an answer.

Some failures are answers: the plan is not there, the plan is archived,
the request conflicts with the plan's own state.  Retrying one of those
a thousand times buys nothing, so a plan that failed like that is
**pruned** — it is out of the line, its slot is given back, its phase
becomes ``stopped``, and the tick refills so the plans behind it do not
wait for the next tick (see
:func:`plan_scheduler.engine.PlanSchedulerEngine._prune_failed_plan`).

Other failures are not answers.  A database that was busy for a moment,
a 503 that came and went, a fault that may go away by itself: they say
that **this attempt** did not work — not that the plan will never work.
Pruning on one of those throws the plan out of the line for a reason
that may be gone by the next tick: the row is gone, the phase says
``stopped``, and nobody will ever run that plan again.  A scheduler
that prunes on a busy database is a scheduler that loses work because
the disk was busy.

So the two are told apart, and the difference is a difference of
**consequence**:

* a **permanent** failure prunes — the plan leaves the line;
* a **transient** failure is only **recorded** — the plan keeps its
  place in the queue, its phase is not touched, and the next tick tries
  it again.

The telling apart is :func:`plan_scheduler.engine.is_permanent_failure`'s
job, and this file pins what the telling apart *means*.

What is pinned here
-------------------
``test_transient_failure_keeps_queue_row``
    The plan keeps its queue row and its phase, and the slot of another
    plan that is working is not given back on the way.
``test_transient_failure_recorded_in_errors``
    The failure is **recorded**: the engine's ``errors`` carry the
    ``execute`` phase and the exception that was raised, so an operator
    can see what went wrong without the plan being thrown away.
``test_transient_failure_retried_next_tick``
    The next tick tries the plan again: the executor is called a second
    time and fails a second time, and the failure is recorded again.
``test_mixed_batch_prunes_only_the_permanent_failure``
    A batch that carries both kinds of failure prunes **only** the
    permanent one; the transient one keeps its row and its phase.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader, the command executor and the live exit — on a
real SQLite file with the production schema.  The exit is the
production :class:`plan_scheduler.dryrun.ExecuteSink`, so the pruning
that decides what a batch's failures mean is the pruning the live
scheduler has.  Only three things are the case's own: the clock, the
sink's executor (which fails for the plans the case names) and the
facts the case writes itself.

The tick is driven **synchronously**, one call at a time, so nothing the
case asserts on depends on a worker thread and on when it happened to
run.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_transient_failure_not_pruned.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

#: The phase a plan is in when it will never run again.
STOPPED = "stopped"

#: How many plans may work at the same time in these cases.  The
#: capacity is **ample** on purpose: the queue is not full, so the only
#: thing that could keep a plan from being tried is the failure itself.
CAPACITY = 2

#: How long a spawned child process may live.
CHILD_LIFETIME = 30

#: What the database says when it is busy.  The wording matters
#: because it is the wording an operator reads in the log: a failure
#: that may go away by itself has to say so.
BUSY = "database is locked"


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


class FailingExecuteSink:
    """The live exit, whose executor fails the way the case says.

    The exit itself is the production one — :class:`ExecuteSink` — so
    the decisions are carried out, and read back one by one, the way the
    live scheduler carries them out: a decision that failed for good is
    **pruned**, and a decision that failed for a moment is only
    **recorded**.

    What the case owns is the **executor** the exit is given: it fails
    for the plans the case names, and it starts the plans it does not
    fail.  Starting a plan means moving its phase to ``executing``,
    which is the part of the lifecycle the scheduler's own facts depend
    on: a plan that works is not waiting any more.

    Attributes:
        failures: the failure each named plan raises, keyed by plan id.
        batches: every batch the exit was handed, in order.
        calls: the plans the executor was handed, in order.  A failure
            is a call too — the executor *was* called, and it is what
            the next tick's retry is about.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sink = ExecuteSink(self._execute)
        self.failures: Dict[str, BaseException] = {}
        self.batches: List[List[Dict[str, Any]]] = []
        self.calls: List[str] = []

    def _execute(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out — or fail the way the case says."""
        plan_id = str(decision.get("plan_id") or "")
        self.calls.append(plan_id)
        error = self.failures.get(plan_id)
        if error is not None:
            raise error
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


def _execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and its state."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, NULL, ?, ?, ?)",
        (plan_id, project_dir, exec_pid, exec_status, _iso()),
    )


def _queue_row(
    conn: sqlite3.Connection, plan_id: str, seq: int = 1, priority: int = 0
) -> None:
    """Write the scheduler's own queue row for the plan."""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (plan_id, int(seq), int(priority), _iso(), _iso()),
    )


def _slot(conn: sqlite3.Connection, plan_id: str, acquired_at: float) -> None:
    """Write a slot row: the moment the plan took its slot."""
    moment = _iso(acquired_at)
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_slot (plan_id, acquired_at, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, moment, moment),
    )


def _queue_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that have a queue row, in the production order."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_queue(conn)]


def _slot_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that hold a concurrency slot."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_slots(conn)]


def _phase_of(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """The phase the plan is in, as the state database says it."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _permanent_error(plan_id: str) -> BaseException:
    """The failure that will never go away: the plan is not there."""
    from state_machine.repositories.routing_repository import PlanNotFoundError

    return PlanNotFoundError(plan_id)


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep({0})".format(CHILD_LIFETIME)]
    )
    return int(process.pid)


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
        sink: FailingExecuteSink,
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
    sink = FailingExecuteSink(conn)
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


def _transient_error() -> RuntimeError:
    """The failure that may go away by itself: the database was busy."""
    return RuntimeError(BUSY)


# ---------------------------------------------------------------------------
# 1. The transient failure keeps the plan in the line
# ---------------------------------------------------------------------------


def test_transient_failure_keeps_queue_row(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """The plan keeps its row and its phase, and nobody's slot is given back.

    The plan is tried, the database is busy, and the attempt fails.  The
    plan may well run the next time — the disk was busy, not the plan —
    so it keeps its place in the line and its phase says ``queued``:
    the scheduler did not decide that this plan will never run.

    The other plan in the world is **working**: it holds a slot, and it
    keeps it.  A failure that may go away by itself is about one plan's
    attempt, and it is not the working plan's slot that is given back.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2"))
    try:
        live = _live_pid(case_registry)

        # The plan that is working, and the slot it holds.
        _routing(world.conn, "p2", EXECUTING)
        _execution(world.conn, "p2", exec_pid=live, exec_status="running")
        _slot(world.conn, "p2", acquired_at=BASE_EPOCH - 100)

        # The plan whose attempt fails for a moment.
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "p1", seq=1)
        world.sink.failures["p1"] = _transient_error()

        world.engine._run_tick()

        assert world.sink.calls == ["p1"], (
            "the plan was not tried at all: {0!r}".format(world.sink.calls)
        )
        assert _queue_ids(world.conn) == ["p1"], (
            "the plan that may run the next time is not in the line any "
            "more: {0!r}".format(_queue_ids(world.conn))
        )
        assert _phase_of(world.conn, "p1") == QUEUED, (
            "the phase says the plan will never run: {0!r}".format(
                _phase_of(world.conn, "p1")
            )
        )
        assert "p2" in _slot_ids(world.conn), (
            "the working plan's slot was given back on the way: {0!r}".format(
                _slot_ids(world.conn)
            )
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. The transient failure is recorded
# ---------------------------------------------------------------------------


def test_transient_failure_recorded_in_errors(
    db_factory: Any, plan_factory: Any
) -> None:
    """The failure is written down, so an operator can see what happened.

    A failure that may go away by itself still happened, and the
    operator has to be able to see it — otherwise the plan is simply
    tried again for ever and nothing says why.  The engine's ``errors``
    carry the phase the failure happened in (``execute``) and the
    exception itself.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "p1", seq=1)
        world.sink.failures["p1"] = _transient_error()

        world.engine._run_tick()

        recorded = world.engine.errors
        assert len(recorded) == 1, (
            "the failure was not recorded once: {0!r}".format(recorded)
        )
        phase, error = recorded[0]
        assert phase == "execute", (
            "the failure was recorded under another phase: {0!r}".format(phase)
        )
        assert isinstance(error, RuntimeError), (
            "the recorded failure is not the one that was raised: {0!r}".format(
                error
            )
        )
        assert BUSY in str(error), (
            "the recorded failure does not say what went wrong: {0!r}".format(
                str(error)
            )
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 3. The next tick tries the plan again
# ---------------------------------------------------------------------------


def test_transient_failure_retried_next_tick(
    db_factory: Any, plan_factory: Any
) -> None:
    """The plan is tried again on the next tick, and it fails again.

    This is the whole point of not pruning: the plan is still in the
    line, so the next tick tries it again.  The executor is called a
    second time, the failure happens a second time, and the second
    failure is recorded as well — a plan that is retried and fails
    again is a plan whose trouble the operator can see.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "p1", seq=1)
        world.sink.failures["p1"] = _transient_error()

        world.engine._run_tick()
        world.engine._run_tick()

        assert world.sink.calls == ["p1", "p1"], (
            "the plan was not tried again on the next tick: {0!r}".format(
                world.sink.calls
            )
        )
        assert _queue_ids(world.conn) == ["p1"], (
            "the plan is not in the line any more, so it will not be "
            "tried again: {0!r}".format(_queue_ids(world.conn))
        )
        assert _phase_of(world.conn, "p1") == QUEUED, (
            "the phase says the plan will never run: {0!r}".format(
                _phase_of(world.conn, "p1")
            )
        )
        recorded = world.engine.errors
        assert len(recorded) == 2, (
            "the two failures were not recorded once each: {0!r}".format(
                recorded
            )
        )
        assert all(phase == "execute" for phase, _ in recorded), (
            "the failures were recorded under another phase: {0!r}".format(
                recorded
            )
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 4. Only the permanent failure is pruned
# ---------------------------------------------------------------------------


def test_mixed_batch_prunes_only_the_permanent_failure(
    db_factory: Any, plan_factory: Any
) -> None:
    """A batch that carries both kinds of failure prunes only the permanent one.

    Two plans are admitted in the same batch and both fail: one of them
    fails for good — the plan is not there — and the other one fails
    for a moment.  The two are told apart, and the telling apart is
    what keeps the line honest: the plan that will never run leaves the
    line, and the plan that may run the next time stays in it, keeps
    its phase, and is tried again.
    """
    world = _world(db_factory, plan_factory, plan_ids=("gone", "p1"))
    try:
        _routing(world.conn, "gone", QUEUED)
        _queue_row(world.conn, "gone", seq=1)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "p1", seq=2)

        world.sink.failures["gone"] = _permanent_error("gone")
        world.sink.failures["p1"] = _transient_error()

        world.engine._run_tick()

        assert _queue_ids(world.conn) == ["p1"], (
            "the plan that will never run is still in the line, or the plan "
            "that may run the next time is not: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert _phase_of(world.conn, "gone") == STOPPED, (
            "the plan that failed for good was not stopped: {0!r}".format(
                _phase_of(world.conn, "gone")
            )
        )
        assert _phase_of(world.conn, "p1") == QUEUED, (
            "the phase says the plan will never run: {0!r}".format(
                _phase_of(world.conn, "p1")
            )
        )
        transient = [
            error
            for _, error in world.engine.errors
            if isinstance(error, RuntimeError)
        ]
        assert transient, (
            "the failure that may go away by itself was not recorded: "
            "{0!r}".format(world.engine.errors)
        )
    finally:
        world.close()
