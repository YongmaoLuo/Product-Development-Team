"""Integration tests: the queue view and the queue table drop a ghost.

Background
----------
The facts layer already answers "who is waiting?" without the plans
that are not there any more: a queued row whose directory is gone is a
**ghost**, and the ghost is dropped from that answer.

Two places still read the queue **table** directly, and the table
still carries the ghost's row:

* the **view** — ``state["queue"]``, the rows the panel renders — was
  built from the table, row by row, so the panel showed the ghost;
* the **reconcile** — the phase that makes the table agree with the
  facts — took the set of plans that already have a row from the
  *view*.  Once the view drops the ghost, the reconcile no longer sees
  it, so the ghost's row is never deleted: the row stays forever, and
  the number it holds is never handed out again.

So the view skips the rows that are not waiting, and the reconcile
takes the rows that already exist from the **table** — the table is the
thing the reconcile repairs, so it has to see the table's rows.

What is pinned here
-------------------
``test_queue_view_skips_missing_plan``
    The view holds the plan that is there and not the ghost that is not.
``test_reconcile_removes_missing_plan_row``
    One tick later the ghost's row is gone from the table.
``test_empty_queue_view_is_empty``
    An empty queue is an empty view.
``test_reconcile_removes_row_of_a_plan_that_is_not_waiting``
    A row for a plan that is not waiting loses the row — and the plan's
    phase is not touched.
``test_head_carries_the_policy_verdict``
    The head of the view carries the reason the policy gave for it.
``test_ghost_does_not_hold_the_line``
    The ghost does not hold the line: the plan behind it starts.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader and the command executor are the real ones,
on a real SQLite file with the production schema.  Only three things
are the case's own: the clock, the sink (which writes the decisions
down and starts nothing) and the facts the case writes itself.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_queue_view_excludes_ghost.py -v
"""

from __future__ import annotations

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

#: The phase a plan is in while it works.
EXECUTING = "executing"

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
    """The exit that only says what it decided — the dry-run one.

    The tick's decisions are written down and nothing else is touched,
    so a case can watch what the scheduler decided without the world
    changing under it: a plan that is admitted by a tick is still
    waiting when the tick is over.
    """

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Write the decisions down and hand back what they were."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch

    def decisions(self) -> List[Dict[str, Any]]:
        """Every decision the sink was handed, in order."""
        return [decision for batch in self.batches for decision in batch]


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


def _queue_row(
    conn: sqlite3.Connection, plan_id: str, seq: int = 1, priority: int = 0
) -> None:
    """Write the scheduler's own queue row for the plan."""
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (plan_id, seq, priority, _iso(), _iso()),
    )


def _queue_ids(conn: sqlite3.Connection) -> List[str]:
    """The plans that have a queue row, in the production order."""
    store = SqliteSchedulerStore()
    return [str(row.get("plan_id") or "") for row in store.list_queue(conn)]


def _phase_of(conn: sqlite3.Connection, plan_id: str) -> Any:
    """The phase the plan carries in the routing table."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


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
            enabled=True,
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
# 1. The view does not show the ghost
# ---------------------------------------------------------------------------


def test_queue_view_skips_missing_plan(
    db_factory: Any, plan_factory: Any
) -> None:
    """The view the panel reads holds the plan that is there.

    The ghost is ahead of the real plan in the queue table — the ghost
    took the first number, so it sorts first — and its directory is
    gone, so the facts do not answer "waiting" for it.  The view is
    built from the table, so it has to skip the row the facts do not
    vouch for: the panel must not render a plan that cannot run.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "ghost", QUEUED)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "ghost", seq=1)
        _queue_row(world.conn, "p1", seq=2)

        state = world.engine._read_state()

        assert [row["plan_id"] for row in state["queue"]] == ["p1"], (
            "the view shows a plan that is not there: {0!r}".format(
                state["queue"]
            )
        )
    finally:
        world.close()


def test_empty_queue_view_is_empty(db_factory: Any, plan_factory: Any) -> None:
    """An empty queue is an empty view."""
    world = _world(db_factory, plan_factory)
    try:
        state = world.engine._read_state()

        assert state["queue"] == [], (
            "there is nothing in the queue, so there is nothing to show"
        )
    finally:
        world.close()


def test_head_carries_the_policy_verdict(
    db_factory: Any, plan_factory: Any
) -> None:
    """The head of the view carries the reason the policy gave for it.

    The ghost was the head of the **table**, but it is not a plan, so
    the head of the **line** is the plan behind it — and the head of
    the line is the plan the admission check is about.  The reason on
    the head of the view is therefore the policy's verdict about that
    plan, not the "somebody is in front of me" reason the plans behind
    the head carry.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "ghost", QUEUED)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "ghost", seq=1)
        _queue_row(world.conn, "p1", seq=2)

        state = world.engine._read_state()

        assert state["queue"], "the real plan is in the line: {0!r}".format(
            state["queue"]
        )
        assert state["queue"][0]["reason"] == "running", (
            "the head of the view is the plan the policy decided about, so "
            "it carries the policy's reason: {0!r}".format(state["queue"][0])
        )
    finally:
        world.close()


def test_ghost_does_not_hold_the_line(
    db_factory: Any, plan_factory: Any
) -> None:
    """The ghost does not hold the line: the plan behind it starts.

    The ghost took the first number, so it sorts ahead of the real
    plan — and a scheduler that waited for the ghost would wait for
    ever.  The ghost is not a plan, so it is not in the line, and the
    plan behind it is admitted: the fill phase takes the head of the
    line, and the head of the line is the plan that is really there.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "ghost", QUEUED)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "ghost", seq=1)
        _queue_row(world.conn, "p1", seq=2)

        world.engine._run_tick()

        admits = [
            str(decision.get("plan_id"))
            for decision in world.sink.decisions()
            if decision.get("action") == "admit"
        ]
        assert admits == ["p1"], (
            "the ghost is not in the line, so it does not hold it: "
            "{0!r}".format(admits)
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. The reconcile deletes the ghost's row
# ---------------------------------------------------------------------------


def test_reconcile_removes_missing_plan_row(
    db_factory: Any, plan_factory: Any
) -> None:
    """One tick later the ghost's row is gone from the queue table.

    The reconcile makes the table agree with the facts, and the facts
    do not say the ghost is waiting.  The reconcile has to read the
    **table** to do it: the view drops the ghost on purpose, so a
    reconcile that took the rows it knows about from the view would
    never learn that the ghost's row is there, and the row would stay
    forever.
    """
    world = _world(db_factory, plan_factory)
    try:
        _routing(world.conn, "ghost", QUEUED)
        _routing(world.conn, "p1", QUEUED)
        _queue_row(world.conn, "ghost", seq=1)
        _queue_row(world.conn, "p1", seq=2)

        world.engine._run_tick()

        assert _queue_ids(world.conn) == ["p1"], (
            "the ghost's row is still in the queue table: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
    finally:
        world.close()


def test_reconcile_removes_row_of_a_plan_that_is_not_waiting(
    db_factory: Any, plan_factory: Any
) -> None:
    """A row for a plan that is not waiting loses the row — and only the row.

    The plan is there, but it is working, so it is not waiting: the
    queue holds the plans that are waiting, and a working plan is not
    in a line.  The reconcile takes the row away.  It does not touch
    the plan's **phase**: the phase is the lifecycle's word, and the
    scheduler does not rewrite it.
    """
    world = _world(db_factory, plan_factory, plan_ids=("p1", "p2"))
    try:
        _routing(world.conn, "p1", QUEUED)
        _routing(world.conn, "p2", EXECUTING)
        _queue_row(world.conn, "p1", seq=1)
        _queue_row(world.conn, "p2", seq=2)

        world.engine._run_tick()

        assert _queue_ids(world.conn) == ["p1"], (
            "the working plan is not in a line: {0!r}".format(
                _queue_ids(world.conn)
            )
        )
        assert _phase_of(world.conn, "p2") == EXECUTING, (
            "the reconcile does not rewrite the plan's phase"
        )
    finally:
        world.close()
