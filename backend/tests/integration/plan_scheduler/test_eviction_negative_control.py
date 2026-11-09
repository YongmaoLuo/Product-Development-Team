"""Integration tests: the eviction, seen from the panel — the negative control.

Background
----------
The panel said two things that could not both be true: **the queue was
empty** and **the plan was not running**.  The world reaches that state
through the eviction:

* a plan is working — it holds a slot, and the ledger counts it;
* another plan asks to start, the capacity is full, so somebody has to
  give way;
* the eviction stops the victim and puts it back into the queue.

The third step is where the world used to break.  The stop went through
the lifecycle's **idle** branch — this process had no handle on the
victim's process, so there was nothing for it to stop — and the idle
branch leaves the phase where it was.  The victim was a ``failed`` plan,
and a failed plan is not *waiting*: it never enters ``queued_plans``,
and the tick's queue reconciliation takes its queue row away, because
the queue holds the plans that are waiting.  The victim was therefore
neither running nor waiting: the queue was empty and the plan was not
running.

The eviction is also where the **verdict** belongs.  The victim's
execution row still said ``running`` with a live pid, so the ledger
kept counting it as working and kept its slot: a plan that had given
way was still holding the room, and the plan that asked for the room
could not have it.  The row has to say what happened — the execution is
over, and the reason is that it was evicted for the plan that asked.

What this file holds down
--------------------------
The production scene is **built by hand** — a failed plan whose
execution row says it is running, a slot it holds, and an empty queue —
and it is driven through the **public** entry points: the command
executor and the engine's tick.  Nothing here calls the eviction
directly: what the panel sees is the command's answer and the tables
the tick leaves behind, and the panel never sees anything else.

The four cases are the four claims:

1. the victim **waits** afterwards (its phase is ``queued``), not failed
   and not running;
2. its execution row carries the **evicted verdict** — ``stopped``, with
   the reason naming the plan that took its place;
3. the slot it held is **given back**;
4. the queue **keeps** it: it is waiting, and it is not working.

Why the lifecycle is a double
-----------------------------
The scene is the one the *idle* branch produces, and the idle branch is
the lifecycle facade's own verdict — "there is nothing running here
that I can stop".  The double says exactly that, and nothing else: its
stop does not move the phase and does not touch the execution row.
Both of those are the eviction's own work, and a double that did them
would hide the very thing this file is about.

The double's ``requeue_plan`` is the lifecycle's own edge
(``failed → queued``) — it is the one thing in the double that moves a
phase, and it moves it the way the real facade moves it.

The database is the case's own
------------------------------
Every case here writes to its **own** SQLite file (the ``db_factory``
fixture) and its own plans tree (the ``plan_factory`` fixture).  The
production database is never touched: a negative control that wrote to
``.pdt/state.db`` would take the scheduler's own state with it.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
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

#: The plan that is working when the scene is built — the one that will
#: be evicted.
VICTIM = "pdt-e2e-2"

#: The plan that asks to start, and takes the victim's place.
REQUESTER = "pdt-e2e-9"


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


class IdleLifecycle:
    """The lifecycle, answering the one verdict this scene produces.

    ``stop_plan`` says **idle**: this process has no handle on the
    victim's process, so there is nothing for it to stop — and the phase
    stays where it is.  The execution row is not touched either: that
    write is the handler's business, and the handler is not what this
    scene is about.  The eviction's own verdict is.

    ``requeue_plan`` is the lifecycle's own edge (``failed → queued``),
    and it is the only thing in this double that moves a phase.
    """

    def __init__(self, conn: sqlite3.Connection, projects: Dict[str, str]) -> None:
        """Bind the double to the case's connection and its projects."""
        self._conn = conn
        self._projects = projects
        self.calls: List[Any] = []

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Say what the idle branch says: nothing was stopped."""
        self.calls.append(("stop_plan", plan_id, requeue))
        return LifecycleResult(plan_id, "idle")

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
        lifecycle: IdleLifecycle,
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


@pytest.fixture
def production_scene(db_factory: Any, plan_factory: Any) -> Any:
    """The production scene: a failed plan that is working, and an empty queue.

    The scene is built by hand, because a state that has to be reached
    by waiting is a state no case may depend on:

    * the victim's phase is ``failed`` — the phase the idle branch leaves
      behind;
    * its execution row says ``running``, with a pid that is alive right
      now (this process), so the ledger counts it as working;
    * it holds a slot, taken long enough ago to be interruptible;
    * the queue is empty;
    * the requester waits for its turn (``queued``).
    """
    db = db_factory()
    victim = plan_factory(plan_id=VICTIM)
    requester = plan_factory(plan_id=REQUESTER)
    projects = {
        VICTIM: str(victim.project_dir),
        REQUESTER: str(requester.project_dir),
    }

    conn = db.connect()
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, victim.plan_dir.parent, clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    lifecycle = IdleLifecycle(conn, projects)
    config_loader = StaticConfigLoader(
        enabled=True,
        capacity=1,
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
        capacity=1,
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

    # The scene itself.
    _routing(conn, VICTIM, "failed")
    _execution(
        conn,
        VICTIM,
        projects[VICTIM],
        exec_pid=os.getpid(),
        exec_status="running",
    )
    _slot(conn, VICTIM)
    _routing(conn, REQUESTER, "queued")

    world = World(engine, commands, facts, store, ledger, lifecycle, conn, clock)
    try:
        yield world
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 1. The evicted plan waits for its turn again
# ---------------------------------------------------------------------------


def test_negative_control_phase_is_queued_not_failed(
    production_scene: World,
) -> None:
    """The evicted plan waits for its turn — it is not left failed.

    The scene starts with the victim **failed** while its execution row
    says it is running.  The stop goes through the idle branch, so the
    phase does not move by itself: the eviction has to put the plan back
    into the waiting phase, through the lifecycle's own edge.

    A victim that stays failed is a plan that waits nowhere: it is not
    running any more, and it is not waiting either.
    """
    world = production_scene

    world.commands.start(REQUESTER)
    world.engine.tick()

    phase = _phase(world.conn, VICTIM)
    assert phase == "queued", (
        "the evicted plan is not waiting for its turn: {0!r}".format(phase)
    )


# ---------------------------------------------------------------------------
# 2. The execution row carries the verdict
# ---------------------------------------------------------------------------


def test_negative_control_stop_reason_is_evicted(production_scene: World) -> None:
    """The execution row says the execution is over, and why.

    The row is the only place that says "this plan is not working any
    more".  Without the verdict it keeps saying ``running``, the ledger
    keeps counting the plan as working, and the plan that gave way
    still holds the room.

    The reason names the plan that took its place: ``evicted:<requester>``.
    """
    world = production_scene

    world.commands.start(REQUESTER)
    world.engine.tick()

    row = _execution_row(world.conn, VICTIM)
    assert row.get("exec_status") == "stopped", (
        "the evicted plan's execution row still says it is running: "
        "{0!r}".format(row)
    )
    assert str(row.get("stop_reason") or "").startswith("evicted:"), (
        "the evicted plan's execution row does not say why it stopped: "
        "{0!r}".format(row)
    )
    assert row.get("exec_pid") is None, (
        "the evicted plan's execution row still holds a pid: {0!r}".format(row)
    )


# ---------------------------------------------------------------------------
# 3. The slot is given back
# ---------------------------------------------------------------------------


def test_negative_control_slot_is_released(production_scene: World) -> None:
    """The slot the evicted plan held is given back.

    A slot is released when the ledger *observes* that the plan is not
    working any more.  The observation is the verdict: without it the
    ledger keeps counting the victim, and the plan that asked for the
    room waits for a room that is not free.
    """
    world = production_scene

    world.commands.start(REQUESTER)
    world.engine.tick()

    holders = _slot_rows(world.conn, world.store)
    assert VICTIM not in holders, (
        "the evicted plan still holds a slot: {0!r}".format(sorted(holders))
    )


# ---------------------------------------------------------------------------
# 4. The queue keeps the evicted plan
# ---------------------------------------------------------------------------


def test_negative_control_queue_keeps_the_evicted_plan(
    production_scene: World,
) -> None:
    """The queue keeps the evicted plan: it waits, and it does not work.

    The queue holds the plans that are **waiting**, and a plan is
    waiting when its own phase says so.  The victim's phase is ``queued``
    after the eviction, so it belongs in the queue — and it is not
    working any more, so it must not be in the running set either.
    """
    world = production_scene

    world.commands.start(REQUESTER)
    world.engine.tick()

    queued = {str(row["plan_id"]) for row in world.facts.queued_plans()}
    active = {str(row["plan_id"]) for row in world.facts.active_plans()}

    assert VICTIM in queued, (
        "the evicted plan is not in the queue: {0!r}".format(sorted(queued))
    )
    assert VICTIM not in active, (
        "the evicted plan is still working: {0!r}".format(sorted(active))
    )
