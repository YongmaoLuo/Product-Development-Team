"""Integration tests: the engine's three capacity rules.

Background
----------
The scheduler's capacity is a promise about the machine: at most ``N``
plans work at the same time.  Three rules keep that promise, and this
file holds down each of them.

1. **A slot belongs to a working plan.**  A plan that is parked
   awaiting a human answer holds its slot only while its execution
   process is alive.  The process is what occupies the machine: a
   pending decision is a *guard* against mistaking a parked plan for
   idle work, never a reason to give the slot back on its own.  The
   rule is therefore a double condition — a pending decision **and** a
   process that is gone — and the four quadrants around it are spelled
   out below.
2. **One plan at a time per workspace.**  The directory a plan works
   in is derived from the plan facts, so the rule survives a restart.
   A plan whose directory is held says so — ``workspace_busy`` — and
   says it whether it is the head of the line or one place behind it:
   a silent wait would send the operator looking in the wrong place.
3. **The excess is given back.**  More plans working than there are
   slots is a state the scheduler converges out of: the plans that
   have been running long enough are interrupted, the youngest first,
   and a plan that has not had its turn yet is never interrupted for
   it.  The excess that cannot be given back is *reported*
   (``over_capacity``), not hidden.

The cases drive the **production** engine — the store, the ledger, the
facts reader and the command executor are the real ones, on a real
SQLite file with the production schema.  Only three things are the
case's own: the clock, the lifecycle (which changes the facts the way
the real one does) and the sink (which carries the decisions out
through that lifecycle).

The clock is **injected**, never monkeypatched: ``time.time`` belongs
to the whole process, and a case that moved it would move it for every
other thread in it — including the ones the verification rules are
judged by.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

#: The protection a young run has — ``M``.  A plan inside its
#: protected window is never interrupted, whatever else is true.
MIN_RUNTIME = 600.0

#: How long the case's child process — the plan that is really
#: working — stays alive.
CHILD_LIFETIME = 30

#: The phase a plan is in while it waits for its turn to run.
QUEUED = "queued"

#: The phase a plan is in while it works.
EXECUTING = "executing"

#: A phase in which the verification thread is the thing working on the
#: plan: there is no execution process, and the plan holds compute
#: anyway.
VERIFYING = "verify_first_pass"


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
    """What the lifecycle answers: the work was done."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.ok = True
        self.detail = "ok"


class FakeLifecycle:
    """The lifecycle the decisions go through — it changes the facts.

    The real lifecycle is the only writer of a plan's phase: it starts
    the run, stops the process, abandons the verification round and
    puts a stopped plan back into the queue.  This one does the same
    four things to the same tables, so a case can watch the world
    change under the scheduler the way it changes in production — a
    stopped plan really is no longer working, and an abandoned
    verification round really has given its compute back.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        facts: Any,
        pid_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        """Bind the lifecycle to the case's connection and facts."""
        self._conn = conn
        self._facts = facts
        self._pid_provider = pid_provider if pid_provider is not None else (
            lambda: os.getpid()
        )
        self.calls: List[Tuple[str, str]] = []

    def start_plan(self, plan_id: str, spec: Any = None) -> LifecycleResult:
        """Start the plan: a live process, a running status, the phase."""
        self.calls.append(("start_plan", plan_id))
        started = self._facts.start_spec(plan_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, exec_pid, exec_status, updated_at) "
            "VALUES (?, 1, ?, ?, 'running', ?)",
            (
                plan_id,
                started.get("project_dir"),
                int(self._pid_provider()),
                _iso(BASE_EPOCH),
            ),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
            (plan_id, EXECUTING, _iso(BASE_EPOCH)),
        )
        return LifecycleResult(plan_id)

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Stop the plan: the process is gone and the round is abandoned.

        The verification round is abandoned by the same call, because a
        round that went on holding compute nobody owns is a slot that
        is free on paper and busy in fact.
        """
        self.calls.append(("stop_plan", plan_id))
        self._conn.execute(
            "UPDATE plan_execution SET exec_status = 'stopped', exec_pid = NULL, "
            "updated_at = ? WHERE plan_id = ?",
            (_iso(BASE_EPOCH), plan_id),
        )
        self._conn.execute(
            "UPDATE plan_verification SET verification_status = 'stopped', "
            "updated_at = ? WHERE plan_id = ?",
            (_iso(BASE_EPOCH), plan_id),
        )
        if requeue:
            self.requeue_plan(plan_id)
        return LifecycleResult(plan_id)

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Park the plan."""
        self.calls.append(("pause_plan", plan_id))
        return LifecycleResult(plan_id)

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Put the plan back into the waiting phase."""
        self.calls.append(("requeue_plan", plan_id))
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = ?, updated_at = ? "
            "WHERE plan_id = ?",
            (QUEUED, _iso(BASE_EPOCH), plan_id),
        )
        return LifecycleResult(plan_id)


class ApplyingSink:
    """The exit the tick's decisions go through — the live one.

    The dry-run exit writes the decisions down and touches nothing
    else.  This one carries them out through the lifecycle, so a case
    can watch the world change the way it changes in production: an
    admitted plan starts working, and a stopped plan stops working.
    """

    def __init__(self, lifecycle: FakeLifecycle) -> None:
        self.lifecycle = lifecycle
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Carry the decisions out and hand back what they were."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        for decision in batch:
            action = str(decision.get("action") or "")
            plan_id = str(decision.get("plan_id") or "")
            if action == "admit":
                self.lifecycle.start_plan(plan_id)
            elif action == "stop":
                self.lifecycle.stop_plan(plan_id, requeue=True)
        return batch

    def decisions(self) -> List[Dict[str, Any]]:
        """Every decision the sink was handed, in order."""
        return [decision for batch in self.batches for decision in batch]

    def admits(self) -> List[str]:
        """The plans the tick decided to admit, in order."""
        return [
            str(decision.get("plan_id"))
            for decision in self.decisions()
            if decision.get("action") == "admit"
        ]

    def stops(self) -> List[str]:
        """The plans the tick decided to interrupt, in order."""
        return [
            str(decision.get("plan_id"))
            for decision in self.decisions()
            if decision.get("action") == "stop"
        ]


class RecordingSink:
    """The exit that only says what it decided — the dry-run one.

    The convergence *decides* who to interrupt; this exit writes the
    decision down and touches nothing else.  A case that compares the
    tick's answer with the explicit start's answer needs the world to
    stay put between the two — a sink that carries the decisions out
    would move it, and the second answer would be about a different
    world than the first.
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

    def admits(self) -> List[str]:
        """The plans the tick decided to admit, in order."""
        return [
            str(decision.get("plan_id"))
            for decision in self.decisions()
            if decision.get("action") == "admit"
        ]

    def stops(self) -> List[str]:
        """The plans the tick decided to interrupt, in order."""
        return [
            str(decision.get("plan_id"))
            for decision in self.decisions()
            if decision.get("action") == "stop"
        ]


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


class World:
    """What a case drives: the engine and the collaborators around it.

    Attributes:
        engine: the engine under test.
        store: the scheduler store the case reads through.
        sink: the sink the tick's decisions go through.
        lifecycle: the lifecycle the decisions are carried out by.
        ledger: the capacity ledger — the authority on who holds compute.
        clock: the clock the case moves by hand.
        conn: the case's own connection — a **second** one on the same
            file as the engine's, so the case reads what the engine
            committed and never joins the engine's transaction.
    """

    def __init__(
        self,
        engine: Any,
        store: Any,
        sink: ApplyingSink,
        lifecycle: FakeLifecycle,
        ledger: Any,
        clock: FakeClock,
        conn: sqlite3.Connection,
        commands: Any = None,
        config_loader: Any = None,
    ) -> None:
        self.engine = engine
        self.store = store
        self.sink = sink
        self.lifecycle = lifecycle
        self.ledger = ledger
        self.clock = clock
        self.conn = conn
        self.commands = commands
        self.config_loader = config_loader

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
    capacity: int = 1,
    min_runtime_seconds: float = MIN_RUNTIME,
    sink: Any = None,
) -> World:
    """Build the world a case drives.

    The plans are real plan directories; the facts about them — the
    phase, the subprocess, the pending decision, the slot row — are
    written by the case itself, because a state that has to be reached
    by waiting is a state no case may depend on.

    The engine and the command executor read the settings from **one**
    loader, the way the production wiring does: the tick and the
    explicit start are two triggers that may interrupt somebody, and
    they have to reach the same answer at the same moment.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    database = db_factory()
    for plan_id in plan_ids:
        plan_factory(plan_id=plan_id)

    conn = _open_db(database.path)
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    lifecycle = FakeLifecycle(conn, facts)
    config_loader = StaticConfigLoader(
        enabled=True,
        capacity=capacity,
        min_runtime_seconds=min_runtime_seconds,
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
        min_runtime_seconds=min_runtime_seconds,
        clock=clock,
        config_provider=config_loader,
    )
    sink = sink if sink is not None else ApplyingSink(lifecycle)

    engine = PlanSchedulerEngine(
        config_loader=config_loader,
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=sink,
        clock=clock,
    )
    # The case reads through a second connection on the same file: what
    # it sees is what the engine committed, never the engine's open
    # transaction.
    return World(
        engine,
        store,
        sink,
        lifecycle,
        ledger,
        clock,
        _open_db(database.path),
        commands=commands,
        config_loader=config_loader,
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
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and its state."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, NULL, ?, ?, ?)",
        (plan_id, project_dir, exec_pid, exec_status, _iso(BASE_EPOCH)),
    )


def _verification(
    conn: sqlite3.Connection,
    plan_id: str,
    status: str = "running",
) -> None:
    """Write the plan's verification fact: the round and its state."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_verification "
        "(plan_id, verification_status, round, updated_at) VALUES (?, ?, 1, ?)",
        (plan_id, status, _iso(BASE_EPOCH)),
    )


def _pending_decision(conn: sqlite3.Connection, plan_id: str) -> None:
    """Write a decision nobody has answered yet."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_decisions "
        "(decision_id, plan_id, source, question, options, mode, status, "
        " created_at, updated_at) "
        "VALUES (?, ?, 'scheduler', 'May the plan go on?', '[]', 'auto', "
        "'pending', ?, ?)",
        (f"{plan_id}-decision", plan_id, _iso(BASE_EPOCH), _iso(BASE_EPOCH)),
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


def _slot(
    conn: sqlite3.Connection,
    plan_id: str,
    acquired_at: Optional[float] = None,
) -> None:
    """Write a slot row: the moment the plan took its slot."""
    moment = _iso(BASE_EPOCH if acquired_at is None else acquired_at)
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_slot (plan_id, acquired_at, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, moment, moment),
    )


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


def _dead_pid(case_registry: Any) -> int:
    """The pid of a process that has already exited.

    The pid is **proved** dead before the case uses it: a pid that
    happens to be alive would turn the case into a test of the wrong
    thing, and the failure would read like a scheduler bug.
    """
    process = case_registry.spawn([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    pid = int(process.pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return pid
    raise AssertionError(
        "the pid {0} is still alive: the case would not test what it "
        "claims to test".format(pid)
    )


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_hitl_releases_only_when_process_dead(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A pending decision gives a slot back only when the process is gone.

    The four quadrants, one by one:

    * **pending + alive** — the plan is parked awaiting an answer, but
      its process is still there: it holds the slot.
    * **pending + dead** — the process is gone: the slot goes back.
    * **no pending + alive** — the plan is working: it holds the slot.
    * **no pending + dead** — the run is over: the slot goes back.

    The last two are the ordinary rule — a plan that is not working
    does not hold compute — and the ledger's reconciliation is what
    carries them out.  The first two are this rule, and the engine
    judges them itself: a pending decision is a guard against
    mistaking a parked plan for idle work, and it is never a reason to
    give the slot back on its own.
    """
    world = _world(
        db_factory,
        plan_factory,
        plan_ids=("p_hitl_alive", "p_hitl_dead", "p_plain_alive", "p_plain_dead"),
        capacity=4,
    )
    conn = world.conn

    alive = _live_pid(case_registry)
    dead = _dead_pid(case_registry)

    for plan_id, pid in (
        ("p_hitl_alive", alive),
        ("p_hitl_dead", dead),
        ("p_plain_alive", alive),
        ("p_plain_dead", dead),
    ):
        _routing(conn, plan_id, EXECUTING)
        _execution(conn, plan_id, exec_pid=pid, exec_status="running")
        _slot(conn, plan_id, acquired_at=BASE_EPOCH - 100)

    _pending_decision(conn, "p_hitl_alive")
    _pending_decision(conn, "p_hitl_dead")

    # The engine judges the parked plans itself, and it gives the slot
    # back to the one whose process is gone — and to that one only.
    released = world.engine._release_hitl_slots()

    assert released == ["p_hitl_dead"], (
        "the engine gave the wrong slots back: {0!r}".format(released)
    )
    slots = _slots(conn)
    assert "p_hitl_dead" not in slots, (
        "the slot of a plan whose process is gone was kept: {0!r}".format(slots)
    )
    assert "p_hitl_alive" in slots, (
        "the slot of a parked plan with a live process was given back: "
        "{0!r}".format(slots)
    )

    # The tick settles the other two quadrants: the plan that is
    # working keeps its slot, and the plan whose run is over gives it
    # back.
    world.engine._run_tick()

    slots = _slots(conn)
    assert sorted(slots) == ["p_hitl_alive", "p_plain_alive"], (
        "the slots do not say who is working: {0!r}".format(slots)
    )

    snapshot = world.engine.snapshot()
    assert snapshot["capacity"]["used"] == 2, (
        "the capacity says {0!r} plans are working, and two are".format(
            snapshot["capacity"]["used"]
        )
    )


def test_workspace_serial_blocks_second(
    db_factory: Any, plan_factory: Any, case_registry: Any, tmp_path: Path
) -> None:
    """Two plans may not work in one directory, and the blocked one says so.

    The workspace is derived from the plan facts — the directory the
    plan declares — so the rule survives a restart and holds for the
    second plan with the same key just as it holds for the first.

    The reason is the point: a plan that is waiting for a directory
    must not be reported as waiting for a slot, because the operator
    looking at the panel would go and look in the wrong place.  Both
    the plan at the head of the line and the one behind it are
    reported — the one behind it is the one the head's verdict never
    reaches.
    """
    shared = tmp_path / "shared-workspace"
    shared.mkdir(parents=True, exist_ok=True)

    world = _world(
        db_factory,
        plan_factory,
        plan_ids=("p_running", "p_first", "p_second"),
        capacity=1,
    )
    conn = world.conn

    # The three plans share one directory: the running one holds it,
    # and the two waiting ones are behind it in the line.
    for plan_id in ("p_running", "p_first", "p_second"):
        _execution(conn, plan_id, project_dir=str(shared))

    live = _live_pid(case_registry)

    _routing(conn, "p_running", EXECUTING)
    _execution(
        conn,
        "p_running",
        exec_pid=live,
        exec_status="running",
        project_dir=str(shared),
    )
    _slot(conn, "p_running", acquired_at=BASE_EPOCH - 100)

    _routing(conn, "p_first", QUEUED)
    _execution(conn, "p_first", project_dir=str(shared))
    _queue_row(conn, "p_first", seq=1)

    _routing(conn, "p_second", QUEUED)
    _execution(conn, "p_second", project_dir=str(shared))
    _queue_row(conn, "p_second", seq=2)

    world.engine._run_tick()

    # The two plans behind the running one are both waiting for the
    # directory it holds, and neither of them is started.
    snapshot = world.engine.snapshot()
    reasons = [str(row["reason"]) for row in snapshot["queue"]]
    assert reasons == ["workspace_busy", "workspace_busy"], (
        "the blocked plans are not reported as blocked: {0!r}".format(reasons)
    )

    seqs = [int(row["seq"]) for row in snapshot["queue"]]
    assert seqs == [1, 2], (
        "the line is not the line the case wrote: {0!r}".format(seqs)
    )

    assert world.sink.admits() == [], (
        "a plan was started in a directory that was held: {0!r}".format(
            world.sink.admits()
        )
    )
    assert "p_running" in _slots(conn), (
        "the plan holding the directory gave its slot back: {0!r}".format(_slots(conn))
    )


def test_over_capacity_converges_within_two_ticks(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """Six plans working and four slots: the excess is gone in two ticks.

    A tick decides to interrupt the plans that have been running long
    enough — the youngest first — and the slot of an interrupted plan
    is given back when the plan is *observed* to be gone.  So the
    excess takes two ticks to disappear: one to interrupt, one to
    observe.  Two ticks is the bound, and the panel says ``False`` for
    ``over_capacity`` once it is behind us.
    """
    plan_ids = ("r1", "r2", "r3", "r4", "r5", "r6")
    world = _world(
        db_factory, plan_factory, plan_ids=plan_ids, capacity=4
    )
    conn = world.conn

    live = _live_pid(case_registry)

    # Every plan took its slot long ago, so every one of them is past
    # its protected window and may be interrupted.
    for index, plan_id in enumerate(plan_ids):
        _routing(conn, plan_id, EXECUTING)
        _execution(conn, plan_id, exec_pid=live, exec_status="running")
        _slot(conn, plan_id, acquired_at=BASE_EPOCH - (6000 - index * 1000))

    world.engine._run_tick()

    # The two youngest are interrupted: r6 took its slot last, and r5
    # before it.
    assert sorted(world.sink.stops()) == ["r5", "r6"], (
        "the wrong plans were interrupted: {0!r}".format(world.sink.stops())
    )

    world.engine._run_tick()

    snapshot = world.engine.snapshot()
    assert snapshot["capacity"]["used"] == 4, (
        "the excess is still there: {0!r}".format(snapshot["capacity"])
    )
    assert snapshot["over_capacity"] is False, (
        "the excess is still reported: {0!r}".format(snapshot)
    )
    assert sorted(_slots(conn)) == ["r1", "r2", "r3", "r4"], (
        "the slots do not say who is working: {0!r}".format(_slots(conn))
    )


def test_no_eviction_when_no_eligible_victim(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """Nobody inside the protected window is interrupted, and the excess shows.

    Six plans are working and there are four slots, but every one of
    them took its slot moments ago.  The scheduler may not interrupt a
    plan that has not had its turn yet, so the excess stays — and it is
    **reported**: ``over_capacity`` is true, because a capacity that
    is over its own limit and says nothing is worse than one that says
    so.
    """
    plan_ids = ("r1", "r2", "r3", "r4", "r5", "r6")
    world = _world(
        db_factory, plan_factory, plan_ids=plan_ids, capacity=4
    )
    conn = world.conn

    live = _live_pid(case_registry)

    # Every plan is young: it took its slot a hundred seconds ago, and
    # the protection is six hundred.
    for plan_id in plan_ids:
        _routing(conn, plan_id, EXECUTING)
        _execution(conn, plan_id, exec_pid=live, exec_status="running")
        _slot(conn, plan_id, acquired_at=BASE_EPOCH - 100)

    world.engine._run_tick()

    assert world.sink.stops() == [], (
        "a plan inside its protected window was interrupted: {0!r}".format(
            world.sink.stops()
        )
    )

    snapshot = world.engine.snapshot()
    assert snapshot["capacity"]["used"] == 6, (
        "the working set changed: {0!r}".format(snapshot["capacity"])
    )
    assert snapshot["over_capacity"] is True, (
        "the excess is not reported: {0!r}".format(snapshot)
    )
    assert sorted(_slots(conn)) == sorted(plan_ids), (
        "a slot was given back: {0!r}".format(_slots(conn))
    )


def test_start_and_tick_share_one_candidate_set(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """The explicit start and the tick's convergence name the same victim.

    The two triggers that may interrupt a running plan — an operator
    asking for a plan to start now, and the tick finding more plans
    working than there are slots — have to answer **at the same
    moment** with the same candidate set.  Otherwise the operator is
    told that a plan is protected while the tick is interrupting it, or
    the other way round, and nobody can tell which of the two is right.

    The case asks both, twice.  First with the default protection
    (``M`` = 600): the working plans are young, so the tick interrupts
    nobody and the explicit start says ``blocked_by_capacity``.  Then
    the operator shortens the protection to nothing and the settings
    change: now the tick interrupts the youngest working plan, and the
    explicit start has to name the very same one.

    The tick's exit here only *records* its decisions, so the world
    stays put between the two answers and the comparison is about the
    same instant rather than about two different worlds.
    """
    plan_ids = ("r_old", "r_young", "p1")
    world = _world(
        db_factory,
        plan_factory,
        plan_ids=plan_ids,
        capacity=1,
        sink=RecordingSink(),
    )
    conn = world.conn

    live = _live_pid(case_registry)

    # Both working plans are young: they took their slot moments ago.
    _routing(conn, "r_old", EXECUTING)
    _execution(conn, "r_old", exec_pid=live, exec_status="running")
    _slot(conn, "r_old", acquired_at=BASE_EPOCH - 200)
    _routing(conn, "r_young", EXECUTING)
    _execution(conn, "r_young", exec_pid=live, exec_status="running")
    _slot(conn, "r_young", acquired_at=BASE_EPOCH - 100)
    _routing(conn, "p1", QUEUED)
    _queue_row(conn, "p1", seq=1)

    # -- the default protection: the young plans may not be interrupted.
    world.engine._run_tick()

    assert world.sink.stops() == [], (
        "a plan inside its protected window was interrupted: {0!r}".format(
            world.sink.stops()
        )
    )

    blocked = world.commands.start("p1")
    assert blocked.outcome == "blocked_by_capacity", (
        "the explicit start did not agree with the tick: {0!r}".format(blocked)
    )

    # -- the operator shortens the protection to nothing.
    world.config_loader.config = SchedulerConfig(
        enabled=True,
        dry_run=False,
        capacity=1,
        min_runtime_seconds=0.0,
        tick_seconds=0.01,
        command_timeout_seconds=10.0,
    )

    world.engine._run_tick()

    assert world.sink.stops() == ["r_young"], (
        "the tick did not interrupt the youngest plan: {0!r}".format(
            world.sink.stops()
        )
    )

    started = world.commands.start("p1")
    assert started.outcome == "started", (
        "the explicit start did not agree with the tick: {0!r}".format(started)
    )
    assert started.detail == "evicted:r_young", (
        "the explicit start named another victim than the tick: {0!r}".format(
            started.detail
        )
    )


def test_backfill_never_calls_stop(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """A free slot is filled by the next plan waiting, and nobody is stopped.

    The executor of the running plan exits on its own.  The slot it
    held comes back, the next plan in the line takes it, and **not one
    working plan is interrupted**: only an explicit start and the
    over-capacity convergence ever interrupt anybody, and a plain
    backfill is neither of them.
    """
    world = _world(
        db_factory, plan_factory, plan_ids=("p_running", "p_next"), capacity=1
    )
    conn = world.conn

    live = _live_pid(case_registry)

    _routing(conn, "p_running", EXECUTING)
    _execution(conn, "p_running", exec_pid=live, exec_status="running")
    _slot(conn, "p_running", acquired_at=BASE_EPOCH - 5000)

    _routing(conn, "p_next", QUEUED)
    _queue_row(conn, "p_next", seq=1)

    # The executor exits on its own: the status says the run is over
    # and the pid is gone.
    _execution(conn, "p_running", exec_pid=None, exec_status="completed")

    world.engine._run_tick()

    assert world.sink.stops() == [], (
        "the backfill interrupted a working plan: {0!r}".format(world.sink.stops())
    )
    assert world.sink.admits() == ["p_next"], (
        "the free slot was not filled by the next plan: {0!r}".format(
            world.sink.admits()
        )
    )

    slots = _slots(conn)
    assert "p_next" in slots, (
        "the admitted plan does not hold a slot: {0!r}".format(slots)
    )
    assert "p_running" not in slots, (
        "the slot of the finished plan was kept: {0!r}".format(slots)
    )


def test_verification_side_eviction_gives_the_slot_back(
    db_factory: Any, plan_factory: Any, case_registry: Any
) -> None:
    """An interrupted verification round gives its slot back — no fake room.

    A verification round has no execution process, and it holds compute
    all the same.  When the round is abandoned the plan is no longer
    working, and the slot has to go with it: a slot that is free on
    paper while the round is still running is the worst kind of free.
    """
    world = _world(
        db_factory, plan_factory, plan_ids=("v1", "v2"), capacity=1
    )
    conn = world.conn

    for index, plan_id in enumerate(("v1", "v2")):
        _routing(conn, plan_id, VERIFYING)
        _verification(conn, plan_id, status="running")
        _slot(conn, plan_id, acquired_at=BASE_EPOCH - (2000 - index * 1000))

    world.engine._run_tick()

    # The younger round is interrupted: v2 took its slot last.
    assert world.sink.stops() == ["v2"], (
        "the wrong verification round was interrupted: {0!r}".format(
            world.sink.stops()
        )
    )

    world.engine._run_tick()

    status = conn.execute(
        "SELECT verification_status FROM plan_verification WHERE plan_id = ?",
        ("v2",),
    ).fetchone()
    assert status is not None and str(status[0]) != "running", (
        "the abandoned round is still running: {0!r}".format(status)
    )
    slots = _slots(conn)
    assert "v2" not in slots, (
        "the slot of the abandoned round was kept: {0!r}".format(slots)
    )
    assert "v1" in slots, (
        "the slot of the running round was given back: {0!r}".format(slots)
    )
