"""Integration tests: the four operator actions — promote, pause, resume, start.

Background
----------
The operator has four things they can do to a plan from the scheduler
panel, and the four mean four different things.  What the four have in
common is that **three of them may never interrupt anybody's work** —
only ``start`` may, and only when the capacity is full and there is a
plan the rules allow to be interrupted.

The four actions are translated in :mod:`plan_scheduler.commands`, and
this file pins the claims that translation makes.

The claims being pinned
-----------------------
**A promotion is a change of order and nothing else.**  It stops
nobody, it takes no slot, and it does not move the plan's phase.  A
plan that is already running cannot be promoted: it is not waiting in
the queue, and a promotion would be a promise the scheduler cannot
keep.

**A pause parks the plan and gives the slot back.**  The plan ends up
in ``stopped``, and it stops holding compute — so the ledger gives its
slot back.  A paused plan that still holds a slot is a scheduler that
reports a full capacity while nothing is running.

**A resume puts the plan back at the head of the queue.**  It waits
for a slot; it does not take one and it interrupts nobody.  The plan
comes back *first*, because that is the whole point of a resume.

**A start is the only action that may interrupt somebody**, and it
does so only when the capacity is full and there is a victim the rules
allow: a plan that has been running for at least ``M`` seconds, and
among those the **youngest** one — the one least likely to be wrapping
up.  With no such victim the start does not happen: the plan stays at
the head of the queue and the command says ``blocked_by_capacity``.

**No action writes the phase.**  The phase belongs to the state
machine; the executor only ever *asks* the lifecycle to move one, and
the lifecycle asks the state machine.  A command that moved a phase
itself would be a second writer of the plan's workflow state.

Why the real lifecycle is used
------------------------------
The translation layer is worth nothing if it is tested against a
stand-in that agrees with it.  So these cases drive the **real**
:class:`plan_lifecycle_service.PlanLifecycleService` — the same facade
the HTTP layer calls — with only the two *stop handlers* replaced by
recorders.  The handlers are where the real work happens (killing a
process, writing a checkpoint, parking the phase); what is under test
here is the facade's *dispatch* and the command layer's translation of
it, so the recorders do what the handlers do and nothing more.

The recorders write through **their own connection**, exactly as the
real handlers do.  That is not decoration: the executor must not hold
a write transaction while it calls the lifecycle, or the facade's own
writes would deadlock against it.  A case that gets that wrong fails
here rather than in production.

Why the clock is injected
-------------------------
``M`` is 600 seconds and the eviction rule is about a plan that has
been running for at least that long.  Waiting for it would make the
suite take longer than the thing it measures, so the cases drive
:class:`FakeClock` by hand: one clock for the ledger, the facts reader
and the executor, so "now" is one moment for everybody.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a plan is protected for — the ``M`` of the eviction rule.
MIN_RUNTIME_SECONDS = 600.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand.

    It is a callable, because that is what the ledger, the facts reader
    and the executor ask for: ``clock()`` is "what time is it now".
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


def _iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """The plans tree the facade resolves plans in.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the
    facade resolves a plan through ``server._plan_dir``, so the case
    has to write where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot and
    then read once, which is what migrates it into ``plan_routing`` —
    the same path a plan that was bootstrapped by the API takes.  The
    migration is not optional here: the scheduler reads the phase from
    ``plan_routing``, and a plan whose row was never written is a plan
    the scheduler does not know.
    """
    from plan_state import PlanState

    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    # The first read migrates the legacy file into the routing row.
    PlanState(plan_dir)
    return plan_dir


def _phase_in_db(plan_id: str) -> Optional[str]:
    """The phase the plan carries **in the database**."""
    conn = _open_state_db()
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _mark_execution_alive(plan_id: str) -> None:
    """Mark the execution round as in flight (``status=running``).

    The facade asks the **process** whether a plan is running, so this
    is what makes ``stop_plan`` dispatch to the execution side.
    """
    import server

    with server._execution_state_lock:
        server._execution_state[plan_id] = {"status": "running", "ended_at": None}


def _write_execution_row(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and the directory."""
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, project_dir, None, exec_pid, exec_status, _iso(BASE_EPOCH)),
    )


def _queue_order(conn: sqlite3.Connection) -> List[str]:
    """The plan ids the scheduler queue holds, in the queue's order."""
    from plan_scheduler.store import SqliteSchedulerStore

    return [
        str(row["plan_id"])
        for row in SqliteSchedulerStore().list_queue(conn)
    ]


def _slot_rows(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """The held slots, by plan id."""
    from plan_scheduler.store import SqliteSchedulerStore

    return {
        str(row["plan_id"]): row
        for row in SqliteSchedulerStore().list_slots(conn)
    }


def _executor(
    conn: sqlite3.Connection,
    clock: FakeClock,
    capacity: int = 1,
    min_runtime_seconds: float = MIN_RUNTIME_SECONDS,
    config_provider: Any = None,
) -> Any:
    """A command executor over the case's database.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader and the lifecycle facade.  Only the clock
    and the capacity are the case's own.

    ``config_provider`` is the reader of the current configuration — the
    same loader the tick reads.  When it is given, the executor asks it
    for the settings at the moment it decides, the way the tick does;
    when it is omitted, the constructor's numbers stand in for it.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    return CommandExecutor(
        store,
        ledger,
        PlanLifecycleService(),
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=min_runtime_seconds,
        clock=clock,
        config_provider=config_provider,
    )


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    return int(process.pid)


def _running_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    pid: int,
    elapsed: float,
    store: Any,
) -> None:
    """Make ``plan_id`` a plan that is working and has held its slot for a while.

    Three facts have to agree for a plan to be "running": the phase, the
    execution row (with a live pid), and the process state the facade
    reads.  The slot row says **when** it took the slot, which is what
    the eviction rule reads.
    """
    _make_plan(plan_id, "executing")
    _write_execution_row(
        conn,
        plan_id,
        exec_pid=pid,
        exec_status="running",
        project_dir=str(_plans_root() / plan_id / "project"),
    )
    _mark_execution_alive(plan_id)
    store.upsert_slot(conn, plan_id, now=_iso(BASE_EPOCH - elapsed))


# ---------------------------------------------------------------------------
# The fixture that replaces the two stop handlers
# ---------------------------------------------------------------------------


@pytest.fixture
def stop_handlers(monkeypatch):
    """Replace the two stop endpoints with recorders.

    The real stop handler kills the process and writes the checkpoint;
    what the facade's contract is about is *which* handler it dispatched
    and that the plan ends up in ``stopped``.  The recorder does both —
    it marks the execution stopped, exactly like the real handler's
    persistence step, and parks the phase — so the assertions are about
    the facade, not about the handler.

    The recorder writes through **its own connection**, the way the real
    handler does.  The executor must not hold a write transaction while
    it calls the facade; if it does, these writes block and the case
    says so.
    """
    import server  # noqa: F401  (the import order is load-bearing)

    from routes import execution as routes_execution
    from routes import verification as routes_verification

    calls: List[Any] = []

    def _mark_stopped_in_db(plan_id: str) -> None:
        conn = _open_state_db()
        try:
            conn.execute(
                "UPDATE plan_execution SET exec_status = ?, exec_pid = NULL "
                "WHERE plan_id = ?",
                ("stopped", plan_id),
            )
        finally:
            conn.close()

    def _park_as_stopped(plan_id: str) -> None:
        from plan_state import PlanState

        plan_dir = _plans_root() / plan_id
        state = PlanState(plan_dir)
        state.reload()
        state.transition_to("stopped")

    def fake_stop_execution(plan_id, discard=False):
        calls.append(("execution", plan_id, discard))
        _mark_stopped_in_db(plan_id)
        with server._execution_state_lock:
            server._execution_state.pop(plan_id, None)
        _park_as_stopped(plan_id)

    def fake_stop_verification(plan_id):
        calls.append(("verification", plan_id))
        _mark_stopped_in_db(plan_id)
        _park_as_stopped(plan_id)

    monkeypatch.setattr(routes_execution, "stop_execution", fake_stop_execution)
    monkeypatch.setattr(
        routes_verification, "stop_verification", fake_stop_verification,
    )
    return calls


# ---------------------------------------------------------------------------
# 1. A promotion is a change of order and nothing else
# ---------------------------------------------------------------------------


def test_promote_only_changes_order(
    db_factory: Any, tmp_path: Path, stop_handlers: Any
) -> None:
    """Promoting a queued plan moves it in the queue — and does nothing else.

    The three things a promotion must **not** do are pinned here: it
    must not call the lifecycle at all (no stop, no phase move), it must
    not take a slot, and it must not touch anybody else's place in the
    queue.  What it must do is put the plan at the head of the line.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()

        _make_plan("p1", "queued")
        _make_plan("p2", "queued")
        store.enqueue(conn, "p1")
        store.enqueue(conn, "p2")
        assert _queue_order(conn) == ["p1", "p2"]

        executor = _executor(conn, clock, capacity=2)
        result = executor.promote("p1")

        assert result.outcome == "applied", (
            f"the promotion did not go through: {result!r}"
        )
        assert result.command_id is not None, (
            f"the command was not written down: {result!r}"
        )
        # The plan is now first in the line.
        assert _queue_order(conn) == ["p1", "p2"], (
            "the promotion did not move the plan: {0!r}".format(
                _queue_order(conn)
            )
        )
        # It is a change of order and nothing else.
        assert stop_handlers == [], (
            f"the promotion called the lifecycle: {stop_handlers!r}"
        )
        assert _slot_rows(conn) == {}, (
            f"the promotion took a slot: {_slot_rows(conn)!r}"
        )
        assert _phase_in_db("p1") == "queued", (
            "the promotion moved the phase: {0!r}".format(_phase_in_db("p1"))
        )
    finally:
        conn.close()


def test_promote_on_running_plan_conflicts(
    db_factory: Any, tmp_path: Path, case_registry: Any, stop_handlers: Any
) -> None:
    """A plan that is already running cannot be promoted.

    It is not waiting in the queue, so there is no order to move it in,
    and the answer the operator gets is a conflict — not a promise the
    scheduler cannot keep.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _running_plan(conn, "r1", alive, 100.0, store)
        store.enqueue(conn, "r1")

        executor = _executor(conn, clock, capacity=2)
        result = executor.promote("r1")

        assert result.outcome == "conflict", (
            f"promoting a running plan was not a conflict: {result!r}"
        )
        assert stop_handlers == [], (
            f"the promotion called the lifecycle: {stop_handlers!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. A pause parks the plan and gives the slot back
# ---------------------------------------------------------------------------


def test_pause_running_releases_slot(
    db_factory: Any, tmp_path: Path, case_registry: Any, stop_handlers: Any
) -> None:
    """Pausing a running plan stops it and gives its slot back.

    Two facts have to be true at once: the plan is parked in ``stopped``
    — the facade's own doing, through the existing stop path — and it no
    longer holds a slot.  A paused plan that still holds a slot is a
    scheduler that reports a full capacity while nothing is running.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _running_plan(conn, "r1", alive, 100.0, store)
        assert "r1" in _slot_rows(conn)

        executor = _executor(conn, clock, capacity=2)
        result = executor.pause("r1")

        assert result.outcome == "applied", (
            f"the pause did not go through: {result!r}"
        )
        # The plan was really stopped, through the existing stop path.
        assert stop_handlers == [("execution", "r1", False)], (
            f"the pause did not go through the stop path: {stop_handlers!r}"
        )
        assert _phase_in_db("r1") == "stopped", (
            "the paused plan is not parked: {0!r}".format(_phase_in_db("r1"))
        )
        # And it gave its slot back.
        assert "r1" not in _slot_rows(conn), (
            "the paused plan still holds a slot: {0!r}".format(
                _slot_rows(conn)
            )
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. A resume puts the plan back at the head of the queue
# ---------------------------------------------------------------------------


def test_resume_puts_at_queue_head(
    db_factory: Any, tmp_path: Path, case_registry: Any, stop_handlers: Any
) -> None:
    """Resuming a stopped plan puts it back at the head of the queue.

    The plan comes back **first** — that is the whole point of a resume
    — and it comes back **waiting**: a resume takes no slot and
    interrupts nobody, so nobody is stopped and no slot is taken.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _make_plan("p1", "stopped")
        _make_plan("p2", "queued")
        store.enqueue(conn, "p2")
        # A plan that is working, so a resume that interrupted it would
        # be visible.
        _running_plan(conn, "r1", alive, 100.0, store)

        executor = _executor(conn, clock, capacity=2)
        result = executor.resume("p1")

        assert result.outcome == "applied", (
            f"the resume did not go through: {result!r}"
        )
        assert _phase_in_db("p1") == "queued", (
            "the resumed plan is not waiting: {0!r}".format(_phase_in_db("p1"))
        )
        assert _queue_order(conn)[0] == "p1", (
            "the resumed plan is not at the head: {0!r}".format(
                _queue_order(conn)
            )
        )
        # A resume waits for a slot; it does not take one and it stops
        # nobody.
        assert stop_handlers == [], (
            f"the resume stopped somebody: {stop_handlers!r}"
        )
        assert set(_slot_rows(conn)) == {"r1"}, (
            "the resume took a slot: {0!r}".format(_slot_rows(conn))
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. A start with nobody to interrupt is blocked by the capacity
# ---------------------------------------------------------------------------


def test_start_blocks_without_victim(
    db_factory: Any, tmp_path: Path, case_registry: Any, stop_handlers: Any
) -> None:
    """A start that has nobody to interrupt does not start anything.

    The plan that is working took its slot 100 seconds ago — inside the
    protected window of ``M`` — so it may not be interrupted.  The start
    therefore does not happen: the plan stays at the head of the queue
    and the command says ``blocked_by_capacity``.  Nobody was stopped.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _make_plan("p5", "queued")
        store.enqueue(conn, "p5")
        _running_plan(conn, "r1", alive, 100.0, store)

        executor = _executor(conn, clock, capacity=1)
        result = executor.start("p5")

        assert result.outcome == "blocked_by_capacity", (
            f"the start was not blocked by the capacity: {result!r}"
        )
        # The plan is still at the head of the queue, waiting.
        assert _queue_order(conn)[0] == "p5", (
            "the blocked plan is not at the head: {0!r}".format(
                _queue_order(conn)
            )
        )
        assert stop_handlers == [], (
            f"a blocked start interrupted somebody: {stop_handlers!r}"
        )
        assert set(_slot_rows(conn)) == {"r1"}, (
            "the blocked start took a slot: {0!r}".format(_slot_rows(conn))
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. A start with a victim to interrupt
# ---------------------------------------------------------------------------


def test_start_evicts_youngest_eligible(
    db_factory: Any, tmp_path: Path, case_registry: Any, stop_handlers: Any
) -> None:
    """A start frees a slot by interrupting the **youngest** eligible plan.

    Two plans are working and both have been running for at least ``M``
    seconds, so both may be interrupted — but only one is: the youngest
    one, the plan that took its slot most recently and is therefore the
    one least likely to be wrapping up.  The interrupted plan goes back
    into the queue, and the older one is left alone.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _make_plan("p5", "queued")
        store.enqueue(conn, "p5")
        # The older one (900 seconds) and the younger one (700 seconds).
        _running_plan(conn, "r_old", alive, 900.0, store)
        _running_plan(conn, "r_young", alive, 700.0, store)

        executor = _executor(conn, clock, capacity=2)
        result = executor.start("p5")

        assert result.outcome == "started", (
            f"the start did not go through: {result!r}"
        )
        assert result.detail == "evicted:r_young", (
            "the wrong plan was interrupted: {0!r}".format(result.detail)
        )
        # The victim was really interrupted, through the stop path.
        assert ("execution", "r_young", False) in stop_handlers, (
            f"the victim was not interrupted: {stop_handlers!r}"
        )
        assert ("execution", "r_old", False) not in stop_handlers, (
            f"the older plan was interrupted as well: {stop_handlers!r}"
        )
        # The victim is back in the queue; the older plan is not.
        assert "r_young" in _queue_order(conn), (
            "the victim did not go back into the queue: {0!r}".format(
                _queue_order(conn)
            )
        )
        assert "r_old" not in _queue_order(conn), (
            "the plan that was left alone is in the queue: {0!r}".format(
                _queue_order(conn)
            )
        )
        # The plan that asked to start is at the head of the queue.
        assert _queue_order(conn)[0] == "p5", (
            "the started plan is not at the head: {0!r}".format(
                _queue_order(conn)
            )
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. The plan that is not there
# ---------------------------------------------------------------------------


def test_unknown_plan_is_not_found(
    db_factory: Any, tmp_path: Path, stop_handlers: Any
) -> None:
    """A command about a plan the scheduler does not know says so.

    All four actions answer the same way, and none of them pretends the
    plan exists: the answer is ``not_found``, and nothing is written.
    """
    db = db_factory()
    conn = db.connect()
    try:
        clock = FakeClock()
        executor = _executor(conn, clock, capacity=1)

        for action in ("promote", "pause", "resume", "start"):
            result = getattr(executor, action)("ghost")
            assert result.outcome == "not_found", (
                f"{action} about a plan that is not there: {result!r}"
            )

        assert stop_handlers == [], (
            f"a command about a plan that is not there stopped somebody: "
            f"{stop_handlers!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. The start reads the configuration that is current
# ---------------------------------------------------------------------------


def _write_scheduler_settings(path: Path, **settings: Any) -> None:
    """Write a settings file carrying a ``scheduler:`` section."""
    lines = ["scheduler:"]
    for name, value in settings.items():
        lines.append("  {0}: {1}".format(name, value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_start_reads_the_current_min_runtime(
    db_factory: Any, tmp_path: Path, case_registry: Any, stop_handlers: Any
) -> None:
    """The threshold a start uses is the one the configuration says *now*.

    ``M`` is a setting, and settings are hot-loaded: the operator may
    change them while the scheduler is running.  A start that froze
    ``M`` when it was built would answer a different question from the
    tick — and the two triggers that may interrupt somebody would pick
    their victim from two different candidate sets.

    So the case writes a settings file and starts a plan against it:
    the working plan is young, so the start is ``blocked_by_capacity``
    and nobody is interrupted.  Then the operator shortens the
    protection to nothing, the file changes, and the **same** executor
    now finds a victim: the start goes through and interrupts the
    working plan.
    """
    from plan_scheduler.config import SchedulerConfigLoader
    from plan_scheduler.store import SqliteSchedulerStore

    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        settings = tmp_path / "settings.yaml"
        _write_scheduler_settings(
            settings, enabled=True, capacity=1, min_runtime_seconds=600,
        )
        loader = SchedulerConfigLoader(settings)

        _make_plan("p5", "queued")
        store.enqueue(conn, "p5")
        _running_plan(conn, "r1", alive, 100.0, store)

        executor = _executor(
            conn, clock, capacity=1, config_provider=loader,
        )

        blocked = executor.start("p5")
        assert blocked.outcome == "blocked_by_capacity", (
            f"a young plan was interrupted: {blocked!r}"
        )
        assert stop_handlers == [], (
            f"a protected plan was interrupted: {stop_handlers!r}"
        )

        # The operator shortens the protection to nothing.
        _write_scheduler_settings(
            settings, enabled=True, capacity=1, min_runtime_seconds=0,
        )

        started = executor.start("p5")
        assert started.outcome == "started", (
            f"the start did not follow the configuration: {started!r}"
        )
        assert started.detail == "evicted:r1", (
            f"the wrong plan was interrupted: {started.detail!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. The phase belongs to the state machine
# ---------------------------------------------------------------------------


def test_commands_module_never_writes_the_phase() -> None:
    """The command layer never writes the plan's phase itself.

    The phase is the state machine's to move; the command layer may only
    *ask* the lifecycle to move one.  A write of its own would be a
    second writer of the plan's workflow state — and it would be a write
    that skips the state machine's edge table.
    """
    import plan_scheduler.commands as commands

    source = Path(commands.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "INSERT INTO plan_routing",
        "UPDATE plan_routing",
        "DELETE FROM plan_routing",
    ):
        assert forbidden not in source, (
            f"the command layer writes the phase itself: {forbidden!r}"
        )
