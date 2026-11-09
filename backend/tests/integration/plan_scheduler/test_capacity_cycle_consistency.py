"""Integration tests: the capacity cycle 6 → 8 → 6, and the sets it keeps.

Background
----------
The operator turns the capacity knob, and the world has to stay
self-consistent: a plan is either **working** or **waiting**, never
both, and never neither.  The cycle this file walks is the one the
field report describes — six, then eight, then six again — with ten
plans in it, so that every step of the cycle has a line to fill and a
line to give back.

The three steps
---------------
* **6** — six plans work, four wait.
* **8** — the two that waited take the rooms that opened up.
* **6** — the capacity goes back down, and the two plans that are over
  the limit give way: they stop working, they keep their place in the
  line, and the row that says why is the **eviction verdict** — the
  execution row's ``stop_reason`` starting with ``evicted``.

What this file holds down
--------------------------
1. every plan is in exactly one bucket — working or waiting;
2. the number of working plans never goes over the capacity;
3. every plan that carries an eviction verdict is waiting, and is not
   working;
4. raising the capacity fills the line again.

The engine is the real one
--------------------------
The cases drive the **production** engine — the store, the ledger, the
facts reader and the command executor are the real ones, on a real
SQLite file with the production schema.  Three things are the case's
own: the clock, the lifecycle (which changes the facts the way the real
one does) and the sink (which carries the decisions out).

The clock is **injected**, never monkeypatched: ``time.time`` belongs
to the whole process, and a case that moved it would move it for every
other thread in it.

The give-way goes through the executor
--------------------------------------
A plan that is interrupted does not get its row written by the double:
the row is the **eviction verdict**, and the verdict is the executor's
own write — the executor is what says "this plan gave way, and here is
why".  A double that wrote the row would make the case pass for the
wrong reason: the plan would be in the queue because the double said
so, and the verdict would never be exercised.

So the sink carries a stop out through the executor's own eviction, the
same way the operator's explicit start does: the plan gives way, the
room comes back to the line, and the verdict names the plan the room
was for — the one at the head of the line.

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
BASE_EPOCH = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The capacity steps of the cycle, in the order the operator turns the
#: knob: down to six, up to eight, and back down to six.
STEPS: Tuple[int, ...] = (6, 8, 6)

#: How many plans the world holds.  Ten is more than the biggest
#: capacity in the cycle, so every step has a line to fill and a line to
#: give back.
PLAN_COUNT = 10

#: The plans of the world.
PLAN_IDS: Tuple[str, ...] = tuple(
    "pdt-cycle-{0}".format(index) for index in range(1, PLAN_COUNT + 1)
)

#: The runtime a young run is protected for — ``M``.  Zero, and that is
#: the point: the case wants every plan to be evictable, so that
#: shrinking the capacity always converges.  With a real ``M`` a run
#: that has not had its turn yet is never interrupted, and the case
#: would be about the protection window instead of about the sets.
MIN_RUNTIME = 0.0

#: How far the clock moves between two steps of the cycle.  The plans
#: that started last are the youngest ones, and the convergence
#: interrupts the youngest first — so the clock has to move for the
#: victims to be a fact rather than an accident of the plan ids.
STEP_SECONDS = 3600.0

#: How many ticks one step of the cycle gets.  Two: the first tick
#: converges (or fills), the second one settles what the first one
#: wrote.  The bound is a bound — the cycle may not wait for ever.
TICKS_PER_STEP = 2

#: The phase a plan is in while it waits for its turn.
QUEUED = "queued"

#: The phase a plan is in while it works.
EXECUTING = "executing"

#: The phase a plan is in after it gave way.
STOPPED = "stopped"


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


class MutableConfigLoader:
    """The configuration the case turns the capacity knob on.

    The tick reads the configuration on **every** tick, so a knob that
    is turned here is a knob that is in force on the next tick — which
    is how the operator's own override behaves.
    """

    def __init__(
        self,
        *,
        capacity: int,
        min_runtime_seconds: float = MIN_RUNTIME,
        tick_seconds: float = 0.01,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        """Bind the loader to the capacity the cycle starts at."""
        self._capacity = int(capacity)
        self._min_runtime = float(min_runtime_seconds)
        self._tick_seconds = float(tick_seconds)
        self._command_timeout = float(command_timeout_seconds)

    def current(self) -> SchedulerConfig:
        """Return the snapshot the tick reads."""
        return SchedulerConfig(
            enabled=True,
            capacity=self._capacity,
            min_runtime_seconds=self._min_runtime,
            tick_seconds=self._tick_seconds,
            command_timeout_seconds=self._command_timeout,
        )

    def set_capacity(self, capacity: int) -> None:
        """Turn the knob: the next tick reads the new number."""
        self._capacity = int(capacity)


class LifecycleResult:
    """What the lifecycle answers.

    The three attributes are the ones the command layer reads: the
    verdict (``detail``), whether the operation was carried out
    (``ok``) and the plan it was about.
    """

    def __init__(self, plan_id: str, detail: str = "ok", ok: bool = True) -> None:
        self.plan_id = plan_id
        self.detail = detail
        self.ok = ok


class CycleLifecycle:
    """The lifecycle the decisions go through — it moves the phase.

    The real lifecycle is the only writer of a plan's phase: it starts
    the run, stops the process and puts a stopped plan back into the
    line.  This one does the same three things to the same tables, and
    **nothing else**: in particular it does not write the execution
    row.

    That write is the eviction's own verdict — the thing this file is
    about — and a double that wrote it would make the case pass for
    wrong reason: the plan would be in the queue because the double
    said so, and the verdict would never be exercised.

    The stop parks the phase at ``stopped`` and moves it to ``queued``
    when the caller asked for a re-queue, the way the facade does.
    """

    def __init__(self, conn: sqlite3.Connection, facts: Any) -> None:
        """Bind the double to the case's connection and to its facts."""
        self._conn = conn
        self._facts = facts
        self.calls: List[Any] = []

    def start_plan(self, plan_id: str, spec: Any = None) -> LifecycleResult:
        """Start the plan: a live process, a running status, the phase."""
        self.calls.append(("start_plan", plan_id))
        started = self._facts.start_spec(plan_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, exec_pid, exec_status, "
            " updated_at) VALUES (?, 1, ?, ?, 'running', ?)",
            (
                plan_id,
                started.get("project_dir"),
                os.getpid(),
                _iso(BASE_EPOCH),
            ),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
            (plan_id, EXECUTING, _iso(BASE_EPOCH)),
        )
        return LifecycleResult(plan_id, "start_execution(fresh)")

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Stop the plan: the phase is parked, and re-queued on request."""
        self.calls.append(("stop_plan", plan_id, requeue))
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = ?, updated_at = ? "
            "WHERE plan_id = ?",
            (STOPPED, _iso(BASE_EPOCH), plan_id),
        )
        if requeue:
            self.requeue_plan(plan_id)
        return LifecycleResult(plan_id, "stop_execution+requeue")

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Move the plan to the waiting phase, through the lifecycle's edge."""
        self.calls.append(("requeue_plan", plan_id))
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = ?, updated_at = ? "
            "WHERE plan_id = ?",
            (QUEUED, _iso(BASE_EPOCH), plan_id),
        )
        return LifecycleResult(plan_id, "requeue")

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Park the plan."""
        self.calls.append(("pause_plan", plan_id))
        return LifecycleResult(plan_id, "paused")


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
    """The plans tree the facts reader reads.

    The tree is the one the case's own plans live in: the fixture
    writes into the tree the environment names, and the reader reads
    the tree the environment names.  A reader that looked anywhere else
    would not find a single plan.
    """
    override = os.environ.get("PDT_PLANS_DIR")
    if override:
        return Path(override)
    import server

    return Path(server.PLANS_DIR)


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a connection to the case's database with the production contract."""
    from state_machine.db.connection import open as open_db

    return open_db(path)


def _head_of_the_line(facts: Any) -> Optional[str]:
    """Return the plan at the head of the line, or ``None`` when there is none."""
    waiting = facts.queued_plans()
    if not waiting:
        return None
    return str(waiting[0]["plan_id"])


def _evicted_plans(conn: sqlite3.Connection) -> List[str]:
    """Return the plans whose execution row carries an eviction verdict.

    The verdict is what the panel and the troubleshooting read: a plan
    that gave way says so on its own row, and the row says why.
    """
    rows = conn.execute(
        "SELECT plan_id FROM plan_execution WHERE stop_reason LIKE 'evicted%' "
        "ORDER BY plan_id"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _queue_rows(conn: sqlite3.Connection) -> List[str]:
    """Return the plans that hold a row in the scheduler's queue."""
    rows = conn.execute("SELECT plan_id FROM scheduler_queue ORDER BY plan_id").fetchall()
    return [str(row[0]) for row in rows]


def _slot_rows(conn: sqlite3.Connection) -> List[str]:
    """Return the plans that hold a slot."""
    rows = conn.execute("SELECT plan_id FROM scheduler_slot ORDER BY plan_id").fetchall()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class _World:
    """What the cycle runs in.

    Attributes:
        conn: the case's own connection.
        ledger: the capacity ledger — the authority on who holds compute.
        engine: the engine under test — the production one, with its
            tick behind a public name.
        plan_ids: the plans of the world.
        min_runtime: the runtime a young run is protected for.
        facts: the reader that derives the plan facts.
        store: the scheduler store.
        commands: the command executor — the four operator actions.
        clock: the clock the case moves by hand.
        config: the configuration the case turns the capacity knob on.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        ledger: Any,
        engine: Any,
        plan_ids: Tuple[str, ...],
        min_runtime: float,
        *,
        facts: Any,
        store: Any,
        commands: Any,
        clock: FakeClock,
        config: MutableConfigLoader,
    ) -> None:
        """Bind the world to its collaborators."""
        self.conn = conn
        self.ledger = ledger
        self.engine = engine
        self.plan_ids = tuple(plan_ids)
        self.min_runtime = float(min_runtime)
        self.facts = facts
        self.store = store
        self.commands = commands
        self.clock = clock
        self.config = config
        #: What every step of the cycle looked like — the buckets as
        #: they were **at that step**, because the world moves on and
        #: the buckets of the last step are not the buckets of the
        #: first one.
        self.history: List[Dict[str, Any]] = []

    # -- the two buckets --------------------------------------------------

    def running(self) -> List[str]:
        """Return the plans that are working, by plan id."""
        return sorted(str(row["plan_id"]) for row in self.facts.active_plans())

    def queued(self) -> List[str]:
        """Return the plans that are waiting, by plan id."""
        return sorted(str(row["plan_id"]) for row in self.facts.queued_plans())

    def used(self) -> int:
        """Return how many plans hold compute."""
        return int(self.ledger.used())

    def evicted(self) -> List[str]:
        """Return the plans whose row carries an eviction verdict."""
        return _evicted_plans(self.conn)

    # -- the cycle --------------------------------------------------------

    def step(self, capacity: int) -> Dict[str, Any]:
        """Turn the knob to ``capacity``, let the engine settle, and report.

        The knob is turned first, and the engine is then given its
        ticks: the first tick converges (or fills) and the second one
        settles what the first one wrote.  The number of ticks is
        bounded — the cycle may not wait for ever — and a step that did
        not settle inside the bound fails the case rather than hanging.

        The clock moves on between the steps, so that the plans which
        started last really are the youngest ones, and the convergence
        really does give way to the plans that started last.

        Returns:
            What the world looks like after the step: the capacity in
            force, the plans that are working, the plans that are
            waiting and the number of plans holding compute.
        """
        self.config.set_capacity(capacity)
        for _ in range(TICKS_PER_STEP):
            self.engine.tick()
        self.clock.advance(STEP_SECONDS)
        running = self.running()
        queued = self.queued()
        used = self.used()
        self.history.append(
            {
                "capacity": int(capacity),
                "running_ids": list(running),
                "queued_ids": list(queued),
                "queue_ids": _queue_rows(self.conn),
                "used": used,
            }
        )
        return {
            "capacity": int(capacity),
            "running": len(running),
            "queued": len(queued),
            "used": used,
        }

    def cycle(self, steps: Tuple[int, ...] = STEPS) -> List[Dict[str, Any]]:
        """Run the whole cycle and report every step."""
        return [self.step(int(capacity)) for capacity in steps]

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
    plan_ids: Tuple[str, ...],
    min_runtime: float = MIN_RUNTIME,
) -> _World:
    """Build the world the cycle runs in.

    Args:
        db_factory: the fixture that makes the case's own database.
        plan_factory: the fixture that makes the case's own plans.
        capacity: the capacity the cycle starts at.
        plan_ids: the plans of the world.
        min_runtime: the runtime a young run is protected for.

    Returns:
        The world the case drives.
    """
    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(plan_id=plan_id)

    conn = _open_db(database.path)
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    lifecycle = CycleLifecycle(conn, facts)
    config = MutableConfigLoader(capacity=capacity, min_runtime_seconds=min_runtime)
    commands = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=min_runtime,
        clock=clock,
        config_provider=config,
    )

    def _execute(decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out, the way the execute sink's executor does."""
        action = str(decision.get("action") or "")
        plan_id = str(decision.get("plan_id") or "")
        if action == "admit":
            # A plan that was admitted starts working: a live process
            # and the phase that says so.
            lifecycle.start_plan(plan_id)
        elif action == "stop":
            # The plan is over the capacity and gives way.  The eviction
            # is the executor's own: it stops the plan through the
            # lifecycle, writes the verdict on the plan's row, keeps
            # the plan's place in the line and gives the slot back.
            # The verdict names the plan the room was for — the one at
            # the head of the line.
            requester = _head_of_the_line(facts)
            if requester is None:
                raise RuntimeError(
                    "the line is empty: there is no plan the room of "
                    "{0} could be for".format(plan_id)
                )
            if not commands._evict(plan_id, requester):
                raise RuntimeError(
                    "the eviction of {0} did not carry out".format(plan_id)
                )
        return {}

    engine = _Engine(
        config_loader=config,
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=ExecuteSink(_execute),
        clock=clock,
    )

    # The scene: every plan waits for its turn.
    for plan_id in plan_ids:
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
            (plan_id, QUEUED, _iso(BASE_EPOCH)),
        )

    return _World(
        conn,
        ledger,
        engine,
        plan_ids,
        min_runtime,
        facts=facts,
        store=store,
        commands=commands,
        clock=clock,
        config=config,
    )


# ---------------------------------------------------------------------------
# The world the cases drive
# ---------------------------------------------------------------------------


@pytest.fixture
def cycle_world(db_factory: Any, plan_factory: Any) -> Any:
    """Ten plans, a capacity the case turns, and an engine that ticks."""
    world = _build(
        db_factory,
        plan_factory,
        capacity=STEPS[0],
        plan_ids=PLAN_IDS,
        min_runtime=MIN_RUNTIME,
    )
    try:
        yield world
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 1. Every plan is in exactly one bucket
# ---------------------------------------------------------------------------


def test_every_plan_is_in_exactly_one_bucket(cycle_world: _World) -> None:
    """Every plan is either working or waiting — never both, never neither.

    The cycle is the one the field report describes: six plans working
    and four waiting, then eight working and two waiting, then six
    again — the two that were over the limit gave way and went back
    into the line.

    At every step the two buckets together are the ten plans, and they
    do not overlap.  A plan in both is a plan the panel counts twice —
    the very contradiction the report is about — and a plan in neither
    is a plan that waits nowhere and works nowhere, which is the state
    the panel cannot show at all.

    The counts are read from the **plans**, not from the capacity: the
    question is whether every plan is in a bucket, and comparing the
    number of working plans with the capacity would answer a different
    question (and would be wrong on a step whose line is empty).
    """
    world = cycle_world
    every = set(world.plan_ids)

    steps = world.cycle()
    assert steps == [
        {"capacity": 6, "running": 6, "queued": 4, "used": 6},
        {"capacity": 8, "running": 8, "queued": 2, "used": 8},
        {"capacity": 6, "running": 6, "queued": 4, "used": 6},
    ], "the cycle did not go 6 → 8 → 6: {0!r}".format(steps)

    for step, record in zip(steps, world.history):
        running = set(record["running_ids"])
        queued = set(record["queued_ids"])

        assert running | queued == every, (
            "the plans are not all in a bucket at capacity {0}: {1!r}".format(
                step["capacity"], sorted(every - (running | queued))
            )
        )
        assert not (running & queued), (
            "a plan is both working and waiting at capacity {0}: {1!r}".format(
                step["capacity"], sorted(running & queued)
            )
        )
        assert len(running) + len(queued) == len(every), (
            "the buckets hold {0} plans at capacity {1}, and the world "
            "holds {2}: {3!r} against {4!r}".format(
                len(running) + len(queued),
                step["capacity"],
                len(every),
                sorted(running),
                sorted(queued),
            )
        )
        assert len(running) == step["running"], (
            "the working set does not match the count at capacity {0}: "
            "{1!r}".format(step["capacity"], sorted(running))
        )
        assert len(queued) == step["queued"], (
            "the waiting set does not match the count at capacity {0}: "
            "{1!r}".format(step["capacity"], sorted(queued))
        )
        # The queue table is the line the panel reads, and it has to
        # say the same thing the facts say: a plan that is working may
        # not hold a place in the line, and a plan that is waiting may
        # not be missing from it.
        assert set(record["queue_ids"]) == queued, (
            "the queue table and the waiting set disagree at capacity "
            "{0}: {1!r} against {2!r}".format(
                step["capacity"],
                sorted(record["queue_ids"]),
                sorted(queued),
            )
        )

    assert not world.engine.errors, (
        "the engine hit a failure while the cycle ran: {0!r}".format(
            world.engine.errors
        )
    )


# ---------------------------------------------------------------------------
# 2. The used count never goes over the capacity
# ---------------------------------------------------------------------------


def test_used_never_exceeds_capacity(cycle_world: _World) -> None:
    """The number of plans holding compute never goes over the capacity.

    The capacity is a promise about the machine: at most ``N`` plans
    work at the same time.  The ledger is the authority on who holds
    compute, so the number the ledger says is the number the promise
    is judged by.

    The count is read **after** the step has settled — the tick that
    converges is the tick that gives the extra plans back, and the
    count is only true once the tick has written what it decided.
    """
    world = cycle_world

    for capacity in STEPS:
        world.step(capacity)

        used = world.used()
        assert used <= capacity, (
            "more plans are working than the capacity allows at "
            "capacity {0}: {1!r}".format(capacity, used)
        )
        assert used == capacity, (
            "the capacity is {0} and nobody is inside a protected window, "
            "so the line should be full — but {1!r} plans are working".format(
                capacity, used
            )
        )
        assert len(world.running()) == used, (
            "the ledger and the facts disagree about who is working at "
            "capacity {0}: {1!r} against {2!r}".format(
                capacity, used, world.running()
            )
        )


# ---------------------------------------------------------------------------
# 3. The evicted plans wait, and do not work
# ---------------------------------------------------------------------------


def test_evicted_plans_are_queued_and_not_running(cycle_world: _World) -> None:
    """A plan that gave way is waiting, and is not working.

    The verdict is written on the plan's own row: the execution is over
    (``stopped``) and the reason starts with ``evicted``.  The row is
    the only place that says "this plan is not working any more", so a
    plan that carries the verdict has to be **out** of the working set
    — and it has to be **in** the line, because a give-way is not a
    failure: the plan keeps a place in the queue.

    The plans that carry the verdict are read out of the database, not
    out of the engine's memory: the panel and the troubleshooting read
    the row, so the row is what the case checks.
    """
    world = cycle_world

    world.cycle()

    evicted = world.evicted()
    assert evicted, (
        "no plan carries an eviction verdict: the cycle shrank the "
        "capacity, and the two plans that were over the limit gave way"
    )

    running = set(world.running())
    queued = set(world.queued())
    for plan_id in evicted:
        assert plan_id in queued, (
            "the plan that gave way is not waiting for its turn: "
            "{0!r}".format(plan_id)
        )
        assert plan_id not in running, (
            "the plan that gave way is still working: {0!r}".format(plan_id)
        )

    # The slots say the same thing: a plan that is not working holds
    # no room.
    holders = set(_slot_rows(world.conn))
    for plan_id in evicted:
        assert plan_id not in holders, (
            "the plan that gave way still holds a slot: {0!r}".format(plan_id)
        )


# ---------------------------------------------------------------------------
# 4. Raising the capacity fills the line again
# ---------------------------------------------------------------------------


def test_capacity_grow_refills_the_line(cycle_world: _World) -> None:
    """Raising the capacity puts the waiting plans back to work.

    Six plans work and four wait.  The capacity goes to eight, and the
    two that were waiting take the rooms that opened up: the line is
    full again, and the two that are still waiting are the ones the
    capacity has no room for.
    """
    world = cycle_world

    world.step(6)
    assert len(world.running()) == 6, (
        "the cycle did not start with six plans working: {0!r}".format(
            world.running()
        )
    )
    assert len(world.queued()) == 4, (
        "the cycle did not start with four plans waiting: {0!r}".format(
            world.queued()
        )
    )

    world.step(8)

    running = world.running()
    assert len(running) == 8, (
        "the capacity went to eight and the line was not filled: "
        "{0!r}".format(running)
    )
    assert len(world.queued()) == 2, (
        "the capacity went to eight and the queue did not shrink: "
        "{0!r}".format(world.queued())
    )
    assert world.used() == 8, (
        "the ledger does not count the eight plans that are working: "
        "{0!r}".format(world.used())
    )