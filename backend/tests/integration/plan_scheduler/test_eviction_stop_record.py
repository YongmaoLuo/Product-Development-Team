"""Integration tests: the eviction writes its verdict, and the slot comes back.

Background
----------
An eviction is a **give-way**, not a failure: the plan that was working
steps aside so that the plan that asked may run.  Two things have to be
true afterwards, and the executor used to guarantee neither of them.

**The verdict.**  The victim's execution row said ``running``, with a
live pid, long after the plan had given way.  The row is the only
place that says "this plan is not working any more" — the ledger reads
it, the panel reads it, the scheduler's own admission reads it — so a
row that still says ``running`` keeps the plan counted as working and
keeps its slot.  A plan that gave way was still holding the room, and
the plan that asked for the room could not have it.  The verdict is
``exec_status = 'stopped'`` with ``exec_pid`` cleared, and the reason
names the plan that took the place: ``evicted:<requester>``.

**The wait.**  The stop goes through the lifecycle, and the lifecycle
answers with a **verdict of its own**:

* ``kill_incomplete`` — the process is still alive, so the plan did
  **not** stop.  There is nothing to record, nothing to re-queue and
  nothing to promote: the eviction did not happen, and the start that
  asked for the room has to be told so (``blocked_by_capacity`` with
  ``eviction_incomplete:<victim>``).
* ``idle`` — there was nothing running here to stop, and the phase stayed
  where it was.  The plan still has to go back to waiting, through the
  lifecycle's own edge: a plan that is neither running nor waiting waits
  nowhere, and the panel's "the queue is empty and the plan is not
  running" is exactly that plan.

What this file holds down
--------------------------
The five cases drive the eviction **directly** — the executor's own
method, with the plan that asked and the plan that gives way spelled
out — because the eviction is where the two facts are written:

1. the victim's execution row carries the **evicted verdict**;
2. the victim is **waiting** afterwards, and it is not working;
3. the slot it held is **given back**, and the ledger counts one plan
   fewer;
4. a kill that did not complete **blocks the start** and keeps the slot;
5. the plan that asked really **takes the room** on the next tick.

Why the lifecycle is a double
-----------------------------
The double stops a plan the way the facade does — the phase is parked
at ``stopped`` and, because the caller asked for a re-queue, moved to
``queued`` — and it does **nothing else**.  In particular it does not
write the execution row: that write is the handler's, and the thing
under test here is the eviction's own verdict.  A double that wrote the
row would make the case pass for the wrong reason: the slot would come
back because the double said so, and the verdict would never be
exercised.

The database is the case's own
------------------------------
Every case writes to its **own** SQLite file (the ``db_factory``
fixture) and its own plans tree (the ``plan_factory`` fixture).  The
production database is never touched.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.dryrun import ExecuteSink
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The moment every case starts at.  It is written out rather than taken
#: from the wall clock: the clock is injected, and a case that read the
#: real time would be a case whose answer depends on when it ran.
BASE_EPOCH = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a young run is protected for — ``M``.  A plan inside its
#: protected window is never interrupted, whatever else is true.
MIN_RUNTIME = 600.0

#: The plan that gives way.
VICTIM = "pdt-e2e-2"

#: The plan that asked for the room.
REQUESTER = "pdt-e2e-9"

#: The plans that are working when the capacity is full: four of them,
#: and the fifth one asks.
WORKERS = ("pdt-e2e-1", "pdt-e2e-2", "pdt-e2e-3", "pdt-e2e-4")


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


class LifecycleResult:
    """What the lifecycle answers.

    The three attributes are the ones the command layer reads: the
    verdict (``detail``), whether the operation was carried out
    (``ok``) and the plan it was about.
    """

    def __init__(self, plan_id: str, detail: str, ok: bool = True) -> None:
        self.plan_id = plan_id
        self.detail = detail
        self.ok = ok


class StoppingLifecycle:
    """The lifecycle, stopping a plan the way the facade does.

    The facade's stop does two things: the handler stops the process
    and parks the phase at ``stopped``, and the facade then moves the
    phase to ``queued`` because the caller asked for a re-queue.  This
    double does the same two — and **nothing else**.  In particular it
    does not write the execution row: that write is the handler's own,
    and the thing under test here is the eviction's own verdict.

    The ``kill_incomplete`` verdict is the facade's answer when the
    process is still alive: nothing was stopped, nothing moved, and the
    double says so.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        projects: Dict[str, str],
        detail: str = "stop_execution+requeue",
    ) -> None:
        """Bind the double to the case's connection, its projects and its verdict."""
        self._conn = conn
        self._projects = projects
        self._detail = detail
        self.calls: List[Any] = []

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Stop the plan, or say that it could not be stopped."""
        self.calls.append(("stop_plan", plan_id, requeue))
        if self._detail == "kill_incomplete":
            # The process is still alive: the plan did not stop, and
            # nothing about it moved.
            return LifecycleResult(plan_id, "kill_incomplete")
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = 'stopped', updated_at = ? "
            "WHERE plan_id = ?",
            (_iso(BASE_EPOCH), plan_id),
        )
        if requeue:
            self.requeue_plan(plan_id)
        return LifecycleResult(plan_id, self._detail)

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Move the plan to the waiting phase, through the lifecycle's edge."""
        self.calls.append(("requeue_plan", plan_id))
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = 'queued', updated_at = ? "
            "WHERE plan_id = ?",
            (_iso(BASE_EPOCH), plan_id),
        )
        return LifecycleResult(plan_id, "requeue")

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Park the plan."""
        self.calls.append(("pause_plan", plan_id))
        return LifecycleResult(plan_id, "paused")

    def start_plan(self, plan_id: str, spec: Any = None) -> LifecycleResult:
        """Start the plan: a live process, a running status, the phase."""
        self.calls.append(("start_plan", plan_id))
        self._conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, exec_pid, exec_status, "
            " updated_at) VALUES (?, 1, ?, ?, 'running', ?)",
            (
                plan_id,
                self._projects.get(plan_id),
                os.getpid(),
                _iso(BASE_EPOCH),
            ),
        )
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = 'executing', updated_at = ? "
            "WHERE plan_id = ?",
            (_iso(BASE_EPOCH), plan_id),
        )
        return LifecycleResult(plan_id, "start_execution(fresh)")


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
    project_dir: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    started_at: Optional[float] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and its state."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, attempt_count, project_dir, next_run_at, exec_pid, "
        " exec_status, started_at, updated_at) "
        "VALUES (?, 1, ?, NULL, ?, ?, ?, ?)",
        (
            plan_id,
            project_dir,
            exec_pid,
            exec_status,
            _iso(BASE_EPOCH - 3600 if started_at is None else started_at),
            _iso(BASE_EPOCH),
        ),
    )


def _slot(conn: sqlite3.Connection, plan_id: str) -> None:
    """Give the plan a slot, taken long enough ago to be interruptible."""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_slot "
        "(plan_id, acquired_at, updated_at) VALUES (?, ?, ?)",
        (plan_id, _iso(BASE_EPOCH - 3600), _iso(BASE_EPOCH)),
    )


def _phase(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """Return the phase the plan carries in the database."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?", (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _execution_row(conn: sqlite3.Connection, plan_id: str) -> Dict[str, Any]:
    """Return the plan's execution row, by column name."""
    columns = ("exec_status", "exec_pid", "stop_reason")
    row = conn.execute(
        "SELECT exec_status, exec_pid, stop_reason FROM plan_execution "
        "WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if row is None:
        return {}
    return dict(zip(columns, tuple(row)))


def _slot_rows(conn: sqlite3.Connection, store: SqliteSchedulerStore) -> List[str]:
    """Return the plans that hold a slot."""
    return [str(row["plan_id"]) for row in store.list_slots(conn)]


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class World:
    """What the case drives: the engine, the command executor, the tables.

    Attributes:
        engine: the engine under test — the production one, with its
            tick behind a public name.
        commands: the command executor — the production one.
        facts: the reader that derives the plan facts.
        store: the scheduler store.
        ledger: the capacity ledger — the authority on who holds compute.
        lifecycle: the lifecycle the decisions go through.
        conn: the case's own connection.
        clock: the clock the case moves by hand.
    """

    def __init__(
        self,
        engine: Any,
        commands: Any,
        facts: Any,
        store: Any,
        ledger: Any,
        lifecycle: StoppingLifecycle,
        conn: sqlite3.Connection,
        clock: FakeClock,
    ) -> None:
        self.engine = engine
        self.commands = commands
        self.facts = facts
        self.store = store
        self.ledger = ledger
        self.lifecycle = lifecycle
        self.conn = conn
        self.clock = clock

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


class _Engine(PlanSchedulerEngine):
    """The production engine, with its tick behind a public name.

    The tick is the production one — every phase, in the production
    order.  The subclass only gives it a name the case may call: the
    engine's own worker calls it from its thread, and a case that
    drives the engine by hand needs to say "tick" out loud.
    """

    def tick(self) -> None:
        """Run one tick, in the production order."""
        self._run_tick()


def _build(
    db_factory: Any,
    plan_factory: Any,
    *,
    capacity: int,
    workers: tuple,
    detail: str = "stop_execution+requeue",
) -> World:
    """Build a world: the plans that work, and the one that asks.

    Args:
        db_factory: the fixture that makes the case's own database.
        plan_factory: the fixture that makes the case's own plans.
        capacity: how many plans may work at the same time.
        workers: the plans that are working.
        detail: the verdict the lifecycle's stop answers with.

    Returns:
        The world the case drives.
    """
    db = db_factory()
    projects = {}
    plans_root = None
    for plan_id in (*workers, REQUESTER):
        fixture = plan_factory(plan_id=plan_id)
        projects[plan_id] = str(fixture.project_dir)
        plans_root = fixture.plan_dir.parent

    conn = db.connect()
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root, clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    lifecycle = StoppingLifecycle(conn, projects, detail=detail)
    config_loader = StaticConfigLoader(
        enabled=True,
        capacity=capacity,
        min_runtime_seconds=MIN_RUNTIME,
        tick_seconds=0.01,
        command_timeout_seconds=10.0,
    )
    commands = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=MIN_RUNTIME,
        clock=clock,
        config_provider=config_loader,
    )

    def _execute(decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out, the way the execute sink's executor does."""
        action = str(decision.get("action") or "")
        plan_id = str(decision.get("plan_id") or "")
        if action == "admit":
            lifecycle.start_plan(plan_id)
        elif action == "stop":
            lifecycle.stop_plan(plan_id, requeue=True)
        return {}

    engine = _Engine(
        config_loader=config_loader,
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=ExecuteSink(_execute),
        clock=clock,
    )

    # The scene: the workers are working, and the plan that asked waits
    # for its turn.
    for plan_id in workers:
        _routing(conn, plan_id, "executing")
        _execution(
            conn,
            plan_id,
            projects[plan_id],
            exec_pid=os.getpid(),
            exec_status="running",
        )
        _slot(conn, plan_id)
    _routing(conn, REQUESTER, "queued")

    return World(engine, commands, facts, store, ledger, lifecycle, conn, clock)


@pytest.fixture
def eviction_world(db_factory: Any, plan_factory: Any) -> Any:
    """One plan working, the capacity full, and a plan asking for the room."""
    world = _build(
        db_factory,
        plan_factory,
        capacity=1,
        workers=(VICTIM,),
    )
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def full_world(db_factory: Any, plan_factory: Any) -> Any:
    """Four plans working, the capacity full, and a fifth one asking."""
    world = _build(
        db_factory,
        plan_factory,
        capacity=len(WORKERS),
        workers=WORKERS,
    )
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def kill_incomplete_world(db_factory: Any, plan_factory: Any) -> Any:
    """One plan working, and a stop that could not stop it."""
    world = _build(
        db_factory,
        plan_factory,
        capacity=1,
        workers=(VICTIM,),
        detail="kill_incomplete",
    )
    try:
        yield world
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 1. The row carries the verdict
# ---------------------------------------------------------------------------


def test_eviction_writes_the_evicted_stop_record(full_world: World) -> None:
    """The victim's row says the execution is over, and why.

    The capacity is full — four plans are working — and a fifth asks to
    start, so one of them has to give way.  The plan that gives way
    leaves a row behind, and the row has to say what happened: the
    execution is over (``stopped``, with the pid cleared, because a
    terminal status means there is no executor process any more), and
    the reason names the plan that took the place.
    """
    world = full_world

    result = world.commands.start(REQUESTER)
    assert result.outcome == "started", (
        "the start did not carry out: {0!r}".format(result)
    )
    victim = str(result.detail).split(":", 1)[1]
    assert victim in WORKERS, (
        "the plan that gave way is not one of the working plans: "
        "{0!r}".format(victim)
    )

    row = _execution_row(world.conn, victim)
    assert row.get("exec_status") == "stopped", (
        "the evicted plan's execution row still says it is running: "
        "{0!r}".format(row)
    )
    assert str(row.get("stop_reason") or "").startswith("evicted:"), (
        "the evicted plan's execution row does not say why it stopped: "
        "{0!r}".format(row)
    )
    assert row.get("stop_reason") == "evicted:{0}".format(REQUESTER), (
        "the reason does not name the plan that took the place: {0!r}".format(row)
    )
    assert row.get("exec_pid") is None, (
        "the evicted plan's execution row still holds a pid: {0!r}".format(row)
    )


# ---------------------------------------------------------------------------
# 2. The victim waits, and does not work
# ---------------------------------------------------------------------------


def test_eviction_leaves_the_victim_queued_and_out_of_the_running_set(
    eviction_world: World,
) -> None:
    """The plan that gave way waits for its turn, and is not working.

    A give-way is not a failure: the plan keeps a place in the line.
    The queue holds the plans that are **waiting**, so the victim is in
    it — and the execution is over, so the victim is not in the running
    set.
    """
    world = eviction_world

    ok = world.commands._evict(VICTIM, REQUESTER)
    assert ok is True, "the eviction did not carry out"

    queued = {str(row["plan_id"]) for row in world.facts.queued_plans()}
    active = {str(row["plan_id"]) for row in world.facts.active_plans()}

    assert VICTIM in queued, (
        "the evicted plan is not waiting for its turn: {0!r}".format(sorted(queued))
    )
    assert VICTIM not in active, (
        "the evicted plan is still working: {0!r}".format(sorted(active))
    )


# ---------------------------------------------------------------------------
# 3. The slot comes back
# ---------------------------------------------------------------------------


def test_eviction_releases_the_slot_on_reconcile(eviction_world: World) -> None:
    """The slot the victim held is given back, and the ledger agrees.

    A slot is released when the ledger *observes* that the plan is not
    working any more, and the observation is the verdict.  The ledger
    is the authority on who holds compute, so the count falls with the
    slot: one plan fewer is holding the room.
    """
    world = eviction_world

    before = world.ledger.used()
    assert before == 1, (
        "the case does not start with one plan working: {0!r}".format(before)
    )

    ok = world.commands._evict(VICTIM, REQUESTER)
    assert ok is True, "the eviction did not carry out"

    holders = _slot_rows(world.conn, world.store)
    assert VICTIM not in holders, (
        "the evicted plan still holds a slot: {0!r}".format(sorted(holders))
    )
    assert world.ledger.used() == before - 1, (
        "the ledger still counts the evicted plan as working: {0!r}".format(
            world.ledger.used()
        )
    )


# ---------------------------------------------------------------------------
# 4. A plan that has no execution row
# ---------------------------------------------------------------------------


def test_eviction_of_a_plan_without_an_execution_row_does_not_raise(
    eviction_world: World,
) -> None:
    """A plan with no execution row is not working either — and that is fine.

    The verdict is written on the execution row, and a plan that has no
    row has nowhere to write it.  That is not a failure: the row was
    never there, so the plan was never holding compute, and the
    eviction still happened.  A write that created the row would be
    inventing a run that never was.
    """
    world = eviction_world
    world.conn.execute("DELETE FROM plan_execution WHERE plan_id = ?", (VICTIM,))

    ok = world.commands._evict(VICTIM, REQUESTER)
    assert ok is True, "the eviction did not carry out"

    assert _execution_row(world.conn, VICTIM) == {}, (
        "the eviction created an execution row that was never there"
    )


# ---------------------------------------------------------------------------
# 5. A kill that did not complete
# ---------------------------------------------------------------------------


def test_kill_incomplete_blocks_the_start_and_keeps_the_slot(
    kill_incomplete_world: World,
) -> None:
    """A plan that is still alive was not stopped, so nothing is written.

    The lifecycle answers ``kill_incomplete``: the process is still
    there, so the plan did not stop.  There is nothing to record, no
    re-queue and no promotion to spend — the eviction did not happen.
    The start that asked for the room is told so, and it is told in a
    way it can be acted on: the capacity blocked it, and the detail
    names the plan that is still holding the room.
    """
    world = kill_incomplete_world

    result = world.commands.start(REQUESTER)
    assert result.outcome == "blocked_by_capacity", (
        "the start was carried out although nothing was stopped: {0!r}".format(result)
    )
    assert str(result.detail).startswith("eviction_incomplete:"), (
        "the refusal does not name the plan that is still holding the room: "
        "{0!r}".format(result.detail)
    )
    assert str(result.detail) == "eviction_incomplete:{0}".format(VICTIM), (
        "the refusal names the wrong plan: {0!r}".format(result.detail)
    )

    holders = _slot_rows(world.conn, world.store)
    assert VICTIM in holders, (
        "the slot was given back although nothing was stopped: {0!r}".format(
            sorted(holders)
        )
    )

    row = _execution_row(world.conn, VICTIM)
    assert row.get("exec_status") == "running", (
        "the execution row was written although nothing was stopped: "
        "{0!r}".format(row)
    )
    assert row.get("stop_reason") is None, (
        "the execution row carries a verdict although nothing was stopped: "
        "{0!r}".format(row)
    )
    assert _phase(world.conn, VICTIM) != "queued", (
        "the plan was put back into the queue although nothing was stopped"
    )


# ---------------------------------------------------------------------------
# 6. The plan that asked takes the room
# ---------------------------------------------------------------------------


def test_promoted_plan_starts_on_the_next_tick(eviction_world: World) -> None:
    """The plan that asked really takes the room — on the next tick.

    The start puts the plan at the head of the queue and gives way to
    somebody; the room itself is free only once the ledger observes
    that the plan that gave way is not working any more.  That
    observation happens on the next tick, and the tick then admits the
    head of the queue: the plan that asked really runs.
    """
    world = eviction_world

    result = world.commands.start(REQUESTER)
    assert result.outcome == "started", (
        "the start did not carry out: {0!r}".format(result)
    )

    world.engine.tick()

    holders = _slot_rows(world.conn, world.store)
    assert REQUESTER in holders, (
        "the plan that asked did not take the room: {0!r}".format(sorted(holders))
    )
    row = _execution_row(world.conn, REQUESTER)
    assert row.get("exec_status") == "running", (
        "the plan that asked did not start: {0!r}".format(row)
    )
