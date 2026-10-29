"""Integration tests: the engine's tick order, its mailbox and its thread.

Background
----------
The engine is the scheduler's **only** orchestrator: one worker thread,
one mailbox, and a tick whose steps always run in the same order.
Everything else in the package is a pure function, a reader or a
writer; the engine is what puts them in a row.

This file holds down the three things the engine promises:

1. **The tick's order is fixed.**  The eight phases run in the same
   order every time, so a decision is always made on a state that was
   read in the phase before it, and the phase that failed is the phase
   everybody can name.
2. **A command never lands inside a tick.**  The mailbox and the tick
   share one thread, so the two are strictly ordered: a command is
   handled between two ticks, never in the middle of one.
3. **The thread is reclaimed.**  ``stop()`` joins it, and afterwards
   nothing of the engine is still running.

The cases drive the **production** engine — the store, the ledger, the
facts reader and the command executor are the real ones, on a real
SQLite file with the production schema.  Only three things are the
case's own: the clock, the sink (which records instead of executing)
and the lifecycle (which raises when a case wants it to).

The expected phase list is written out **here** rather than imported
from the engine: a test that reads the expectation out of the code it
is testing proves nothing.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The name the worker thread carries.  It is the name the teardown and
#: the operator look for, so it is spelled out here rather than
#: imported.
THREAD_NAME = "PlanSchedulerEngine"

#: The tick's phases, in the order they run.  Eight steps: the
#: configuration is refreshed, the state is read, the queue and the
#: slots are reconciled, the over-capacity is converged, the free slots
#: are filled, the decisions are carried out and the decisions are
#: written down.
EXPECTED_PHASES: Tuple[str, ...] = (
    "refresh_config",
    "read_state",
    "reconcile_queue",
    "reconcile_capacity",
    "converge",
    "fill",
    "execute",
    "record",
)

#: The six phases that decide — the ones the architecture names as the
#: tick's order.  The configuration refresh comes before them and the
#: recording comes after them.
CORE_PHASES: Tuple[str, ...] = (
    "read_state",
    "reconcile_queue",
    "reconcile_capacity",
    "converge",
    "fill",
    "execute",
)

#: The moment every case starts at.
BASE_EPOCH = 1_700_000_000.0


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


class BrokenStore:
    """A store whose one named method always raises.

    The failure is injected where the scheduler writes — the store is
    the only writer of the scheduling facts — so the case can ask what
    the engine does when the world refuses to be written to.
    """

    def __init__(self, inner: Any, method: str) -> None:
        self._inner = inner
        self._method = method
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        if name == self._method:
            self.calls += 1

            def _boom(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("the scheduler store is broken")

            return _boom
        return getattr(self._inner, name)


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


class BrokenLifecycle:
    """A lifecycle that always raises.

    The engine has to survive a lifecycle that blows up: the command
    is written down as failed, and the scheduler keeps ticking.
    """

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Blow up."""
        raise RuntimeError("the lifecycle is broken")

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Blow up."""
        raise RuntimeError("the lifecycle is broken")

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Blow up."""
        raise RuntimeError("the lifecycle is broken")


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
    """The plans tree the facts reader reads."""
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
            the order the phases ran in.
        store: the scheduler store the case reads through.
        sink: the recording sink the tick's decisions go to.
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
        clock: FakeClock,
        read_conn: sqlite3.Connection,
    ) -> None:
        self.engine = engine
        self.store = store
        self.sink = sink
        self.clock = clock
        self.read_conn = read_conn

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.read_conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def _world(
    db_factory: Any,
    plan_factory: Any,
    *,
    plan_ids: Tuple[str, ...] = ("p1",),
    capacity: int = 1,
    min_runtime_seconds: float = 0.0,
    tick_seconds: float = 0.01,
    store: Any = None,
    lifecycle: Any = None,
) -> World:
    """Build the world a case drives.

    The plans are real plan directories and they are **queued** in the
    database — the phase lives in ``plan_routing``, and a plan the
    scheduler does not know about is a plan it never admits.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(plan_id=plan_id)

    conn = _open_db(database.path)
    for plan_id in plan_ids:
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
            (plan_id, "queued", _iso(BASE_EPOCH)),
        )

    clock = FakeClock()
    the_store = store if store is not None else SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(the_store, facts, clock)
    commands = CommandExecutor(
        the_store,
        ledger,
        lifecycle if lifecycle is not None else RecordingLifecycle(),
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=min_runtime_seconds,
        clock=clock,
    )
    sink = RecordingSink()

    engine = _probe_engine(
        config_loader=StaticConfigLoader(
            enabled=True,
            capacity=capacity,
            min_runtime_seconds=min_runtime_seconds,
            tick_seconds=tick_seconds,
            command_timeout_seconds=10.0,
        ),
        store=the_store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=sink,
        clock=clock,
    )
    return World(engine, the_store, sink, clock, _open_db(database.path))


def _probe_engine(**kwargs: Any) -> Any:
    """Return an engine that writes down every phase and every command.

    The probe is a subclass: it intercepts the phases as they are
    entered and the commands as they are handled, so the order it
    records is the order the engine really ran in — not the order the
    engine says it ran in.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    class ProbeEngine(PlanSchedulerEngine):
        """The engine, watched."""

        def __init__(self, *args: Any, **kw: Any) -> None:
            super().__init__(*args, **kw)
            self.events: List[Tuple[str, str]] = []
            self._probe_lock = threading.Lock()

        def _run_phase(self, name: str) -> None:
            """Write the phase down, then let the engine run it."""
            with self._probe_lock:
                self.events.append(("phase", name))
            super()._run_phase(name)

        def _handle_command(self, request: Any) -> None:
            """Let the engine handle the command, then write it down."""
            super()._handle_command(request)
            with self._probe_lock:
                self.events.append(("command", request.action))

    return ProbeEngine(**kwargs)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_tick_order_is_fixed(db_factory, plan_factory) -> None:
    """The eight phases of a tick run in the same order, every time."""
    world = _world(db_factory, plan_factory)
    engine = world.engine
    try:
        engine.start()
        assert _wait_until(lambda: len(engine.events) >= len(EXPECTED_PHASES)), (
            "the engine never ran a whole tick: {0!r}".format(engine.events)
        )
    finally:
        engine.stop()

    phases = [name for kind, name in engine.events if kind == "phase"]
    assert phases[: len(EXPECTED_PHASES)] == list(EXPECTED_PHASES), (
        "the tick's phases ran in another order: {0!r}".format(phases)
    )

    # The six deciding phases, in the order the architecture names them.
    core = [name for name in phases if name in CORE_PHASES]
    assert core[: len(CORE_PHASES)] == list(CORE_PHASES), (
        "the deciding phases ran in another order: {0!r}".format(core)
    )


def test_stop_joins_thread(db_factory, plan_factory) -> None:
    """``stop()`` joins the worker; nothing of the engine is left running."""
    world = _world(db_factory, plan_factory)
    engine = world.engine
    engine.start()
    engine.stop()

    alive = [
        thread
        for thread in threading.enumerate()
        if thread.name == THREAD_NAME and thread.is_alive()
    ]
    assert alive == [], (
        "{0} engine thread(s) outlived stop(): {1!r}".format(
            len(alive), [thread.name for thread in alive]
        )
    )


def test_store_failure_does_not_kill_tick(db_factory, plan_factory) -> None:
    """A store that refuses to be written to does not stop the ticks."""
    broken = BrokenStore(SqliteSchedulerStore(), "upsert_slot")
    world = _world(db_factory, plan_factory, store=broken)
    engine = world.engine
    try:
        engine.start()
        assert _wait_until(lambda: engine.ticks >= 2), (
            "the engine stopped ticking after the store broke: "
            "{0!r}".format(engine.errors)
        )
    finally:
        engine.stop()

    assert broken.calls > 0, "the case never made the store fail"
    assert engine.ticks >= 2, "the tick after the failure never ran"
    assert engine.errors, "the failure was swallowed instead of recorded"


def test_command_failure_recorded(db_factory, plan_factory) -> None:
    """A command whose lifecycle blows up is written down as failed."""
    world = _world(db_factory, plan_factory, lifecycle=BrokenLifecycle())
    engine = world.engine
    try:
        engine.start()
        result = engine.submit_command("pause", "p1")
    finally:
        engine.stop()

    assert result.outcome == "failed", (
        "a command that raised came back as {0!r}".format(result.outcome)
    )
    assert result.command_id is not None, "the failed command lost its id"

    row = world.store.get_command(world.read_conn, result.command_id)
    assert row is not None, "the failed command was not written down at all"
    assert row["outcome"] == "failed", (
        "the command was left as {0!r}".format(row["outcome"])
    )
    assert row["detail"], "the failure was not explained in the record"


def test_commands_do_not_interleave_tick_steps(db_factory, plan_factory) -> None:
    """A hundred commands and ten ticks share one strict order."""
    world = _world(db_factory, plan_factory, tick_seconds=0.005)
    engine = world.engine
    try:
        engine.start()
        for _ in range(100):
            result = engine.submit_command("promote", "p1")
            assert result.outcome == "applied", (
                "a promote came back as {0!r}".format(result.outcome)
            )
            # A pause long enough for the worker to find its mailbox
            # empty, so the ticks happen between the commands and not
            # only after them.
            time.sleep(0.01)
        assert _wait_until(lambda: engine.ticks >= 10), (
            "the engine only ran {0} tick(s)".format(engine.ticks)
        )
    finally:
        engine.stop()

    phases = 0
    commands = 0
    for kind, _name in engine.events:
        if kind == "phase":
            phases += 1
            continue
        commands += 1
        assert phases % len(EXPECTED_PHASES) == 0, (
            "a command was handled in the middle of a tick: {0!r}".format(
                engine.events
            )
        )

    assert phases % len(EXPECTED_PHASES) == 0, (
        "a tick was cut in half: {0!r}".format(engine.events)
    )
    assert commands == 100, "only {0} command(s) were handled".format(commands)
    assert engine.ticks >= 10, "only {0} tick(s) ran".format(engine.ticks)


def test_snapshot_is_the_read_model(db_factory, plan_factory) -> None:
    """The engine hands the panel the read model the tick reads with."""
    world = _world(db_factory, plan_factory)
    engine = world.engine
    engine.start()
    assert _wait_until(lambda: engine.ticks >= 1), "the engine never ticked"
    # The snapshot is read after the worker has stopped, so the case
    # never reads the engine's connection while the worker is writing
    # on it.
    engine.stop()

    snapshot = engine.snapshot()
    assert isinstance(snapshot, dict), "the snapshot is not a document"
    assert set(snapshot) == {
        "capacity",
        "queue_length",
        "over_capacity",
        "queue",
        "running",
        "generated_at",
    }, "the snapshot has another shape: {0!r}".format(sorted(snapshot))

    assert snapshot["capacity"] == {"total": 1, "used": 0}, (
        "the capacity is {0!r}".format(snapshot["capacity"])
    )
    assert snapshot["over_capacity"] is False
    assert snapshot["queue_length"] == 1, (
        "the queue holds {0} plan(s)".format(snapshot["queue_length"])
    )
    assert snapshot["queue"][0]["position"] == 1, (
        "the queue is not numbered from one: {0!r}".format(snapshot["queue"])
    )
    assert snapshot["queue"][0]["reason"] == "running", (
        "the head of the queue is {0!r}".format(snapshot["queue"][0]["reason"])
    )
    assert snapshot["running"] == [], (
        "nothing is working, but the panel says {0!r}".format(
            snapshot["running"]
        )
    )