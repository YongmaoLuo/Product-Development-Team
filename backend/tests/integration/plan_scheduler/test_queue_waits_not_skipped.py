"""Integration tests: the legitimate waits still hold the head of the queue.

Background
----------
A plan that will never run again is **pruned**: its queue row goes, its
slot goes, its phase becomes ``stopped``, and the tick **refills** — the
state is read again and the free slots are put to work, so the plan
behind the pruned one does not wait for the next tick (see
:func:`plan_scheduler.engine.PlanSchedulerEngine._prune_failed_plan` and
:func:`plan_scheduler.engine.PlanSchedulerEngine._phase_execute`).

The pruning is for the plans that will never run.  The plans that are
**waiting** are a different thing altogether, and the pruning must not
change one byte of their behaviour.  The scheduler has five words for
"this plan is waiting, and here is why":

``due_later``
    it has not come due yet;
``workspace_busy``
    another plan holds the directory it works in;
``waiting_for_slot``
    every slot is taken and nobody may be interrupted;
``paused``
    the operator stopped it;
``hitl_pending``
    it is parked awaiting an answer.

These are **legitimate waits**.  The line is a line, and the scheduler
does not skip anybody: a head that may not start holds the queue, and
the plans behind it wait their turn.  The pruning is not allowed to
turn any of these five into "not waiting any more" — that is what this
file pins.

Every case puts **two** plans in the line — the head that waits and the
one behind it.  A line of one cannot be skipped: when the scheduler
walks past a head, the plan behind it is what shows it — the plan that
started, or the plan that took the head's place at the front of the
line.

What is pinned here
-------------------
``test_due_later_head_holds_queue``
    A plan scheduled for later holds the head of the line.
``test_workspace_busy_head_holds_queue``
    A plan whose directory is held holds the head of the line.
``test_no_slot_head_holds_queue``
    A plan waiting for a slot holds the head of the line.
``test_paused_head_holds_queue``
    A paused head is not a head that may start — the policy's word for
    it, and the engine's world where a held plan keeps its place.
``test_hitl_pending_head_holds_queue``
    A plan parked awaiting an answer holds the head of the line.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader and the command executor are the real ones, on
a real SQLite file with the production schema.  The exit is the
production :class:`plan_scheduler.dryrun.ExecuteSink`, so the pruning
that decides what the batch's failures mean is the pruning the live
scheduler has.  Only three things are the case's own: the clock, the
sink's executor (which starts the plans it is handed, and nothing
else) and the facts the case writes itself.

The paused case is pinned in the policy
---------------------------------------
The engine's own line never carries ``paused``: a plan that is in the
queue is waiting, and a waiting plan is neither paused nor parked (see
``PlanSchedulerEngine._rebuild_line``).  The engine cannot manufacture
the situation, so the word is pinned where it is decided — in
:func:`plan_scheduler.policy.admission_verdict` — and no product code is
touched to make the engine produce one.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_queue_waits_not_skipped.py -v
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
from plan_scheduler.policy import admission_verdict
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

#: How many plans may work at the same time in these cases.
CAPACITY = 2

#: How long a spawned child process may live.
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


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


class RecordingExecuteSink:
    """The live exit, with every batch it was handed written down.

    The exit itself is the production one — :class:`ExecuteSink` — so
    the decisions are carried out, and read back one by one, the way
    the live scheduler carries them out: a decision that failed for
    good is **pruned**, and the tick refills.

    What the case owns is the **executor** the exit is given: it
    starts the plans it is handed and records that it did.

    Attributes:
        batches: every batch the exit was handed, in order.
        carried: the plans that were carried out, in order.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sink = ExecuteSink(self._execute)
        self.batches: List[List[Dict[str, Any]]] = []
        self.carried: List[str] = []

    def _execute(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out — starting the plan it names."""
        plan_id = str(decision.get("plan_id") or "")
        self.carried.append(plan_id)
        _routing(self._conn, plan_id, EXECUTING)
        return {}

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Write the batch down and hand it to the live exit."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return self._sink.apply(decisions)

    def admits(self) -> List[str]:
        """The plans the tick decided to admit, in order."""
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


def _execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
    next_run_at: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and its state."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, project_dir, next_run_at, exec_pid, exec_status, _iso()),
    )


def _pending_decision(conn: sqlite3.Connection, plan_id: str) -> None:
    """Write a decision nobody has answered yet."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_decisions "
        "(decision_id, plan_id, source, question, options, mode, status, "
        " created_at, updated_at) "
        "VALUES (?, ?, 'scheduler', 'May the plan go on?', '[]', 'auto', "
        "'pending', ?, ?)",
        (f"{plan_id}-decision", plan_id, _iso(), _iso()),
    )


def _queue_row(
    conn: sqlite3.Connection, plan_id: str, seq: int, priority: int = 0
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


def _reasons(world: Any) -> List[str]:
    """The reason the panel gives each plan in the line."""
    snapshot = world.engine.snapshot()
    return [str(row.get("reason")) for row in snapshot.get("queue", [])]


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
        sink: RecordingExecuteSink,
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
    project_dirs: Optional[Dict[str, Path]] = None,
) -> World:
    """Build the world a case drives.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader, the command executor and the live exit.
    Only the clock and the executor the exit is given are the case's
    own.

    Args:
        db_factory: the fixture that makes the case's database.
        plan_factory: the fixture that makes the case's plan
            directories.
        plan_ids: the plans the case creates.
        capacity: how many plans may work at the same time.
        project_dirs: the directory each named plan works in.  A plan
            the case does not name gets one of its own.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.engine import PlanSchedulerEngine

    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(
            plan_id=plan_id,
            project_dir=(project_dirs or {}).get(plan_id),
        )

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
    sink = RecordingExecuteSink(conn)
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


def _assert_line_holds(world: Any, head: str, behind: str, reason: str) -> None:
    """Assert that the head of the line is still waiting, and why.

    Four things make a head hold:

    * it is still in the line, and still the first in it;
    * the plan behind it is still in the line, still behind it;
    * nobody was admitted — neither the head nor the plan behind it;
    * nobody took a slot.

    And the reason the head gives is the one it gave before the pruning
    was there.
    """
    assert _queue_ids(world.conn) == [head, behind], (
        "the line is not the line the case wrote — the head was skipped or "
        "dropped: {0!r}".format(_queue_ids(world.conn))
    )
    assert world.sink.admits() == [], (
        "a plan that was waiting was admitted: {0!r}".format(
            world.sink.admits()
        )
    )
    slots = _slot_ids(world.conn)
    assert head not in slots, (
        "a plan that was waiting took a slot: {0!r}".format(slots)
    )
    assert behind not in slots, (
        "the plan behind the head took the head's slot: {0!r}".format(slots)
    )
    assert _reasons(world)[:1] == [reason], (
        "the reason the head gives is not {0!r}: {1!r}".format(
            reason, _reasons(world)
        )
    )


# ---------------------------------------------------------------------------
# 1. A plan that is not due yet
# ---------------------------------------------------------------------------


def test_due_later_head_holds_queue(db_factory: Any, plan_factory: Any) -> None:
    """A plan scheduled for later holds the head of the line.

    ``next_run_at`` is in the future, so the plan has not come due: it
    is waiting for the clock, not for a room.  The tick says so and
    stops — the head keeps its place, the plan behind it does not move
    up, nobody is admitted, and nobody takes a slot.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p_later", "p_next"))
    try:
        _routing(world.conn, "p_later", QUEUED)
        _execution(world.conn, "p_later", next_run_at=_iso(BASE_EPOCH + 3600))
        _queue_row(world.conn, "p_later", seq=1)

        _routing(world.conn, "p_next", QUEUED)
        _queue_row(world.conn, "p_next", seq=2)

        world.engine._run_tick()

        _assert_line_holds(world, "p_later", "p_next", "due_later")
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. A plan whose directory is held
# ---------------------------------------------------------------------------


def test_workspace_busy_head_holds_queue(
    db_factory: Any, plan_factory: Any, case_registry: Any, tmp_path: Path
) -> None:
    """A plan whose directory is held holds the head of the line.

    Two plans work in one directory, and only one plan at a time may
    work in it.  The one that is working holds the directory; the one
    at the head of the line waits for it — and says so, because a plan
    waiting for a directory must not be reported as waiting for a slot.

    There is a free slot (the capacity is two and one plan is working),
    so the directory is the only thing the head waits for.  The plan
    behind the head works in a directory of its own, so it *could*
    start: a case that saw it start would be a case that saw the
    scheduler walk past the head.
    """
    shared = tmp_path / "shared-workspace"
    shared.mkdir(parents=True, exist_ok=True)

    world = _world(
        db_factory,
        plan_factory,
        plan_ids=("p1", "p2", "p3"),
        capacity=2,
        project_dirs={"p1": shared, "p2": shared},
    )
    try:
        live = _live_pid(case_registry)

        # The plan that is working, and the directory it works in.
        _routing(world.conn, "p2", EXECUTING)
        _execution(
            world.conn,
            "p2",
            exec_pid=live,
            exec_status="running",
            project_dir=str(shared),
        )
        _slot(world.conn, "p2", acquired_at=BASE_EPOCH - 100)

        # The plan at the head of the line, in the same directory.
        _routing(world.conn, "p1", QUEUED)
        _execution(world.conn, "p1", project_dir=str(shared))
        _queue_row(world.conn, "p1", seq=1)

        # The plan behind the head, in a directory of its own.
        _routing(world.conn, "p3", QUEUED)
        _queue_row(world.conn, "p3", seq=2)

        world.engine._run_tick()

        _assert_line_holds(world, "p1", "p3", "workspace_busy")
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 3. A plan waiting for a slot
# ---------------------------------------------------------------------------


def test_no_slot_head_holds_queue(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A plan waiting for a slot holds the head of the line.

    The one slot is taken and nobody may be interrupted, so the plan
    behind it waits — and the line does not skip it: the plan behind a
    plan that cannot start does not start either.
    """
    world = _world(
        db_factory,
        plan_factory,
        plan_ids=("p_run", "p2", "p3"),
        capacity=1,
    )
    try:
        live = _live_pid(case_registry)

        _routing(world.conn, "p_run", EXECUTING)
        _execution(
            world.conn, "p_run", exec_pid=live, exec_status="running"
        )
        _slot(world.conn, "p_run", acquired_at=BASE_EPOCH - 100)

        _routing(world.conn, "p2", QUEUED)
        _queue_row(world.conn, "p2", seq=1)

        _routing(world.conn, "p3", QUEUED)
        _queue_row(world.conn, "p3", seq=2)

        world.engine._run_tick()

        _assert_line_holds(world, "p2", "p3", "waiting_for_slot")
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 4. A plan the operator paused
# ---------------------------------------------------------------------------


def test_paused_head_holds_queue(db_factory: Any, plan_factory: Any) -> None:
    """A paused head holds the line — the policy's word, and the world.

    The engine's own line never carries ``paused``: a plan that is in
    the queue is waiting, and a waiting plan is neither paused nor
    parked.  So the word is pinned where it is decided — in the
    policy — and what matters about it is that a paused head is **not**
    a head that may start.  Without the flag the very same snapshot
    says ``running``; with it, the head waits.

    And the engine's world agrees: the plan the operator is holding
    keeps its place in the line after a tick, the plan behind it does
    not move up, nobody is admitted, and nobody takes a slot.
    """
    snapshot: Dict[str, Any] = {
        "queued": [{"plan_id": "p", "paused": True}],
        "used": 0,
        "capacity": 1,
        "running": [],
        "allow_evict": False,
    }

    assert admission_verdict(snapshot) == "paused", (
        "the policy does not call a paused head paused: {0!r}".format(
            admission_verdict(snapshot)
        )
    )

    without_pause = {
        "queued": [{"plan_id": "p"}],
        "used": 0,
        "capacity": 1,
        "running": [],
        "allow_evict": False,
    }
    assert admission_verdict(without_pause) == "running", (
        "the pause is not what holds the head: {0!r}".format(
            admission_verdict(without_pause)
        )
    )

    world = _world(db_factory, plan_factory, plan_ids=("p", "p_next"))
    try:
        _routing(world.conn, "p", QUEUED)
        _pending_decision(world.conn, "p")
        _queue_row(world.conn, "p", seq=1)

        _routing(world.conn, "p_next", QUEUED)
        _queue_row(world.conn, "p_next", seq=2)

        world.engine._run_tick()

        # The engine's line does not manufacture the situation it cannot
        # see: a waiting plan is neither paused nor parked.
        queued = world.engine._state.get("queued", [])
        assert queued and queued[0].get("paused") is False, (
            "the engine's line carries a pause it never produces: {0!r}".format(
                queued
            )
        )

        _assert_line_holds(world, "p", "p_next", "hitl_pending")
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 5. A plan parked awaiting an answer
# ---------------------------------------------------------------------------


def test_hitl_pending_head_holds_queue(
    db_factory: Any, plan_factory: Any
) -> None:
    """A plan parked awaiting an answer holds the head of the line.

    The decision is still pending, so nobody is waiting for a slot —
    saying so would send the operator looking in the wrong place.  The
    head keeps its place, the plan behind it does not move up, nobody
    is admitted, and nobody takes a slot.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p3", "p_next"))
    try:
        _routing(world.conn, "p3", QUEUED)
        _pending_decision(world.conn, "p3")
        _queue_row(world.conn, "p3", seq=1)

        _routing(world.conn, "p_next", QUEUED)
        _queue_row(world.conn, "p_next", seq=2)

        world.engine._run_tick()

        _assert_line_holds(world, "p3", "p_next", "hitl_pending")
    finally:
        world.close()
