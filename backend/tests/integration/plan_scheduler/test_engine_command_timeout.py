"""Integration tests: the engine's bounded wait and its pending answer.

Background
----------
A command is carried out by the engine's worker thread, and the caller
waits for the answer.  The wait has to be **bounded**: the caller is
usually an HTTP handler whose thread comes from a pool, and a lifecycle
call that takes a long time would take a pool thread with it.

Bounding the wait does not withdraw the command.  The command stays in
the mailbox, the worker still carries it out in its turn, and the
outcome it reaches lands in ``scheduler_command``.  What the caller gets
when the bound is reached is a *pending* answer and the command's id —
enough to come back later and ask what happened.

This file holds down the four things that promise:

1. **The timeout answers pending, with an id.**  The caller is told the
   command is still in flight and is given the id to ask about it with.
2. **The command is finished anyway.**  The record goes from
   ``accepted`` to a final outcome — ``done`` or ``failed`` — even
   though nobody was waiting any more.
3. **The command is carried out once.**  The record's final outcome
   lands once, and the action itself runs once.
4. **The wait is one event wait.**  The caller waits on the command's
   event, once, with the configured bound — it does not poll.

The cases drive the **production** engine — the store, the ledger, the
facts reader and the command executor are the real ones, on a real
SQLite file with the production schema.  Only three things are the
case's own: the clock, the sink (which records instead of executing),
and the lifecycle (which is slow, on purpose).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The moment every case starts at.
BASE_EPOCH = 1_700_000_000.0

#: How long the caller's wait is bounded to.  It is the point of the
#: case: the bound is reached long before the command is finished.
COMMAND_TIMEOUT = 0.01

#: How long the lifecycle takes.  The wait must not last this long — it
#: is the whole difference between a bounded wait and a hung handler.
LIFECYCLE_SLEEP = 0.5

#: The outcomes a finished command's record may carry.  A command that
#: is still in flight carries ``accepted``; these are the two words a
#: finished one carries.
FINAL_OUTCOMES = frozenset({"done", "failed"})


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

    The dry-run exit: it applies nothing and starts no plan, so a case
    can watch what the tick decided without the world changing.
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


class SlowLifecycle:
    """A lifecycle that takes its time — the slow call the wait is for.

    Every call is written down **before** it sleeps, so the case sees
    the attempt even while the command is still running.
    """

    def __init__(self, sleep: float = LIFECYCLE_SLEEP) -> None:
        self.sleep = float(sleep)
        self.calls: List[Tuple[str, str]] = []

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Take a long time, and say that the plan was parked."""
        self.calls.append(("pause_plan", plan_id))
        time.sleep(self.sleep)
        return LifecycleResult(plan_id)

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Take a long time, and say that the plan was put back."""
        self.calls.append(("requeue_plan", plan_id))
        time.sleep(self.sleep)
        return LifecycleResult(plan_id)

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Take a long time, and say that the plan was stopped."""
        self.calls.append(("stop_plan", plan_id))
        time.sleep(self.sleep)
        return LifecycleResult(plan_id)


class RecordingEvent(threading.Event):
    """An event that writes down every wait it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: List[Optional[float]] = []

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Write the wait down, then wait as an event does."""
        self.wait_calls.append(timeout)
        return super().wait(timeout)


class ProbeEngine(PlanSchedulerEngine):
    """The engine, watched: it hands out the events its commands wait on."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.events: List[RecordingEvent] = []

    def _new_result_event(self) -> threading.Event:
        """Hand out an event the case can look at."""
        event = RecordingEvent()
        self.events.append(event)
        return event


class CountingStore:
    """The store, counting the command outcomes it was asked to write.

    ``finish_command`` writes an outcome **once**: a second attempt on a
    row that is already finished changes nothing and says so.  The case
    reads that answer to see that the final outcome landed once.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.finished: List[Tuple[Optional[int], str, bool]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def finish_command(
        self,
        conn: sqlite3.Connection,
        command_id: int,
        outcome: str,
        detail: Optional[str] = None,
    ) -> bool:
        """Write the outcome down, and remember whether it was written."""
        wrote = self._inner.finish_command(conn, command_id, outcome, detail)
        self.finished.append((command_id, outcome, wrote))
        return wrote


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
        engine: the engine under test.
        store: the scheduler store the case reads through.
        lifecycle: the slow lifecycle the command goes through.
        read_conn: the case's own connection, opened on the same file
            as the engine's — so the case reads committed state and
            never joins the worker's transaction.
    """

    def __init__(
        self,
        engine: Any,
        store: Any,
        lifecycle: SlowLifecycle,
        read_conn: sqlite3.Connection,
    ) -> None:
        self.engine = engine
        self.store = store
        self.lifecycle = lifecycle
        self.read_conn = read_conn

    def outcome_of(self, command_id: Optional[int]) -> Optional[str]:
        """Return the outcome the command's record carries, if it has one."""
        if command_id is None:
            return None
        row = self.store.get_command(self.read_conn, command_id)
        return None if row is None else str(row["outcome"])

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
    lifecycle: Optional[SlowLifecycle] = None,
    store: Any = None,
    engine_class: Any = PlanSchedulerEngine,
    command_timeout_seconds: float = COMMAND_TIMEOUT,
) -> World:
    """Build the world a case drives.

    The plan is a real plan directory and it is **queued** in the
    database — the phase lives in ``plan_routing``, and a plan the
    scheduler does not know about is a plan it never commands.
    """
    database = db_factory()
    plan_factory(plan_id="p1")

    conn = _open_db(database.path)
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        ("p1", "queued", _iso(BASE_EPOCH)),
    )

    clock = FakeClock()
    the_store = store if store is not None else SqliteSchedulerStore()
    the_lifecycle = lifecycle if lifecycle is not None else SlowLifecycle()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(the_store, facts, clock)
    commands = CommandExecutor(
        the_store,
        ledger,
        the_lifecycle,
        facts=facts,
        conn=conn,
        capacity=1,
        min_runtime_seconds=0.0,
        clock=clock,
    )

    engine = engine_class(
        config_loader=StaticConfigLoader(
            capacity=1,
            min_runtime_seconds=0.0,
            tick_seconds=0.01,
            command_timeout_seconds=command_timeout_seconds,
        ),
        store=the_store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=RecordingSink(),
        clock=clock,
    )
    return World(engine, the_store, the_lifecycle, _open_db(database.path))


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_timeout_returns_pending_with_id(db_factory, plan_factory) -> None:
    """A command that is not answered in time comes back pending, with its id.

    The caller is not left hanging on a lifecycle call that may never
    come back, and it is given the id it needs to ask about the command
    later.
    """
    world = _world(db_factory, plan_factory)
    engine = world.engine
    try:
        engine.start()
        started = time.monotonic()
        result = engine.submit_command("pause", "p1")
        elapsed = time.monotonic() - started
    finally:
        engine.stop()

    assert result.outcome == "pending", (
        "a command that timed out came back as {0!r}".format(result.outcome)
    )
    assert isinstance(result.command_id, int) and result.command_id > 0, (
        "the pending answer lost the command's id: {0!r}".format(
            result.command_id
        )
    )
    assert elapsed < LIFECYCLE_SLEEP, (
        "the wait lasted {0:.3f}s — it waited for the lifecycle instead of "
        "its own bound".format(elapsed)
    )


def test_pending_command_eventually_done(db_factory, plan_factory) -> None:
    """A command nobody waits for any more is finished anyway.

    The timeout gives up the *wait*, not the command: the worker still
    carries it out, and the record goes from ``accepted`` to a final
    outcome the caller can come back and read.
    """
    world = _world(db_factory, plan_factory)
    engine = world.engine
    try:
        engine.start()
        result = engine.submit_command("pause", "p1")
        assert result.outcome == "pending", (
            "the command was answered before its bound: {0!r}".format(
                result.outcome
            )
        )
        assert _wait_until(
            lambda: world.outcome_of(result.command_id) in FINAL_OUTCOMES
        ), (
            "the command was left as {0!r}".format(
                world.outcome_of(result.command_id)
            )
        )
    finally:
        engine.stop()

    assert world.outcome_of(result.command_id) == "done", (
        "the command did not reach a final outcome: {0!r}".format(
            world.outcome_of(result.command_id)
        )
    )


def test_command_not_executed_twice(db_factory, plan_factory) -> None:
    """A command that timed out is carried out once, and finished once.

    Giving up the wait must not make the command happen twice, and the
    final outcome must land once: a record that is written twice says
    two things happened, and only one did.
    """
    counting = CountingStore(SqliteSchedulerStore())
    world = _world(db_factory, plan_factory, store=counting)
    engine = world.engine
    try:
        engine.start()
        result = engine.submit_command("pause", "p1")
        assert result.outcome == "pending", (
            "the command was answered before its bound: {0!r}".format(
                result.outcome
            )
        )
        assert _wait_until(
            lambda: any(wrote for _id, _outcome, wrote in counting.finished)
        ), "the command was never finished"
    finally:
        engine.stop()

    assert world.lifecycle.calls == [("pause_plan", "p1")], (
        "the command was carried out {0} time(s): {1!r}".format(
            len(world.lifecycle.calls), world.lifecycle.calls
        )
    )
    landed = [entry for entry in counting.finished if entry[2]]
    assert len(landed) == 1, (
        "the final outcome landed {0} time(s): {1!r}".format(
            len(landed), counting.finished
        )
    )
    assert landed[0][0] == result.command_id, (
        "the outcome that landed belongs to another command: {0!r}".format(
            landed[0]
        )
    )


def test_answered_command_carries_its_id(db_factory, plan_factory) -> None:
    """A command that is answered in time carries its id as well.

    Both answers name the command: the one that carries the outcome
    and the one that says "come back for it".  A client that got an id
    in one answer and none in the other would have two ways of asking
    the same question.
    """
    world = _world(db_factory, plan_factory, lifecycle=SlowLifecycle(sleep=0.0))
    engine = world.engine
    try:
        engine.start()
        result = engine.submit_command("pause", "p1")
    finally:
        engine.stop()

    assert result.outcome == "applied", (
        "a command that was carried out came back as {0!r}".format(
            result.outcome
        )
    )
    assert isinstance(result.command_id, int) and result.command_id > 0, (
        "the answer lost the command's id: {0!r}".format(result.command_id)
    )
    assert world.outcome_of(result.command_id) == "done", (
        "the command's record did not reach a final outcome: {0!r}".format(
            world.outcome_of(result.command_id)
        )
    )


def test_wait_uses_event_not_polling(db_factory, plan_factory) -> None:
    """The caller waits on the command's event, once, with the bound.

    One event wait and no polling: a handler that polled would keep a
    pool thread busy for the whole wait and would answer later than the
    bound it was given.
    """
    world = _world(db_factory, plan_factory, engine_class=ProbeEngine)
    engine = world.engine
    try:
        engine.start()
        result = engine.submit_command("pause", "p1")
        assert result.outcome == "pending", (
            "the command was answered before its bound: {0!r}".format(
                result.outcome
            )
        )
    finally:
        engine.stop()

    assert len(engine.events) == 1, (
        "the command was given {0} event(s): {1!r}".format(
            len(engine.events), engine.events
        )
    )
    waits = engine.events[0].wait_calls
    assert waits == [COMMAND_TIMEOUT], (
        "the wait was not one bounded event wait: {0!r}".format(waits)
    )