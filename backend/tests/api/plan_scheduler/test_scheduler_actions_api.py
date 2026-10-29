"""``POST /api/plan/{plan_id}/scheduler/{action}`` — the four operator actions.

What is pinned here
-------------------
The operator panel and the LUI reach the scheduler through the same four
endpoints, and both read the answer the same way: the **outcome** of the
action, the **id** of the command that carried it out, and the **detail**
that says what happened.  The status code is part of that answer, not a
decoration on it:

* **200** — the action was carried out (``applied``), it started the plan
  (``started``), or it could not be carried out **because the capacity is
  full** (``blocked_by_capacity``) — which is an answer, not a failure;
* **202** — the command is still in the mailbox.  The caller is not left
  hanging, the command is **not** withdrawn, and the caller can come back
  for the outcome with the command id it was handed;
* **404** — there is no such plan;
* **409** — the action conflicts with the plan's state: it is already
  working, it is in a phase the action does not apply to.  The **reason**
  says which.

The matrix
----------
The four actions mean four different things, and what each one answers
depends on the phase the plan is in and on whether the capacity has room.
So the cases here are a **matrix** — every action, every source phase,
every capacity state — and the matrix itself is the evidence: a rule that
holds for three of the four actions and quietly does not hold for the
fourth is a rule the panel would get wrong on exactly the plan the
operator cared about.

The matrix is driven through the **mounted app**, with a **real engine**
behind it: the route reads ``app.state.scheduler_engine``, and what is
mounted there is a worker thread with a real mailbox, a real store, a
real ledger and the real lifecycle facade.  A route that never reaches
the engine, or one that reaches a stand-in that agrees with it, would
pass a matrix that says nothing about production.

Why the plans are made by hand
------------------------------
Three facts have to agree for a plan to be "working": its phase, its
execution row with a **live** pid, and the slot row that says **when** it
took the slot.  The slot's clock is what the eviction rule reads, so a
case cannot reach "running for 700 seconds" by waiting — it writes the
slot's ``acquired_at`` itself.  The same goes for the phase: the plan's
phase is the state machine's, and a case sets it the way the API does
(through the legacy snapshot the state machine migrates).

The clock is injected for the same reason: ``M`` is 600 seconds, and the
suite must not take longer than the thing it measures.

Why the command's own vocabulary is not enough
----------------------------------------------
The engine answers with a **closed vocabulary** — ``applied``,
``started``, ``blocked_by_capacity``, ``conflict``, ``not_found``, plus
the engine's own ``pending`` and ``failed``.  The route's job is to turn
that vocabulary into an HTTP status **without inventing a word of its
own**: an outcome that is not in the vocabulary is a bug in the route,
and the cases below assert the vocabulary is closed.

The guard header
----------------
Every request here carries ``X-PDT-Request: 1``: the request guard is
middleware over every ``/api/`` path, so a request without the header
never reaches a handler at all.  The suite's hermetic environment
injects the header into every ``TestClient``, which is why the calls
below do not spell it out — and why a 403 in this file would mean the
route sits outside the guarded surface rather than that the caller
forgot a header.

Why this directory has no ``__init__.py``
-----------------------------------------
The production package is called ``plan_scheduler``, and so is this
directory.  A ``__init__.py`` here would make pytest import this
directory as the **top-level** package ``plan_scheduler`` — and the
name would then mean two things at once, with the winner decided by the
order the files happen to be imported in.  The sibling file
``test_scheduler_state_api.py`` says the whole of it; the short version
is that a package marker here would bind the suite's import order to
the name of a production package.  This directory deliberately has none.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)

#: The four actions, in the order the panel offers them.
ACTIONS: Tuple[str, ...] = ("promote", "pause", "resume", "start")

#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a plan is protected for — the ``M`` of the eviction rule.
MIN_RUNTIME_SECONDS = 600.0

#: How long a working plan has held its slot: long enough to be
#: interrupted, and long enough not to be.
ELAPSED_EVICTABLE = 700.0
ELAPSED_PROTECTED = 100.0


# ---------------------------------------------------------------------------
# The helpers
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


class _StaticConfig:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """Return the snapshot."""
        return self._config


def _iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """The plans tree the facade and the facts reader resolve plans in.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the
    facade resolves a plan through ``server._plan_dir``, so the case has
    to write where the facade looks.
    """
    return Path(server.PLANS_DIR)


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


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


def _write_execution_row(
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
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            project_dir,
            None,
            exec_pid,
            exec_status,
            _iso(BASE_EPOCH),
        ),
    )


def _mark_execution_alive(plan_id: str) -> None:
    """Mark the execution round as in flight (``status=running``).

    The facade asks the **process** whether a plan is running, so this
    is what makes ``stop_plan`` dispatch to the execution side.
    """
    with server._execution_state_lock:
        server._execution_state[plan_id] = {"status": "running", "ended_at": None}


def _phase_in_db(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """The phase the plan carries **in the database**."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _queue_order(conn: sqlite3.Connection) -> List[str]:
    """The plan ids the scheduler queue holds, in the queue's order."""
    from plan_scheduler.store import SqliteSchedulerStore

    return [
        str(row["plan_id"]) for row in SqliteSchedulerStore().list_queue(conn)
    ]


def _slot_rows(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """The held slots, by plan id."""
    from plan_scheduler.store import SqliteSchedulerStore

    return {
        str(row["plan_id"]): row
        for row in SqliteSchedulerStore().list_slots(conn)
    }


def _command_row(
    conn: sqlite3.Connection, command_id: int
) -> Optional[Dict[str, Any]]:
    """The command's record, or ``None`` when there is no such row."""
    from plan_scheduler.store import SqliteSchedulerStore

    return SqliteSchedulerStore().get_command(conn, command_id)


def _wait_for_tick(engine: Any, timeout: float = 10.0) -> None:
    """Wait until the engine has run its first tick.

    The tick reconciles the queue and the slots against the plan facts.
    A case that wrote its plans **before** that tick would have the tick
    re-stamp the slot it wrote — and a slot stamped with "now" is a plan
    that has just started, not one that has been working for 700
    seconds.  So the case waits for the tick, and only then writes.
    """
    deadline = time.monotonic() + timeout
    while engine.ticks < 1:
        if time.monotonic() > deadline:
            raise AssertionError(
                "the scheduler engine did not run its first tick in time"
            )
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# The world a case drives
# ---------------------------------------------------------------------------


@dataclass
class World:
    """What a case drives: the engine, and the facts it can read.

    Attributes:
        engine: the running engine — the worker, the mailbox and the
            command executor behind it.
        store: the scheduler's own store.
        clock: the clock every collaborator reads.
        conn: the case's **own** connection — a second one on the same
            file as the engine's, so the case reads what the engine
            committed and never joins the engine's transaction.
        ledger: the capacity ledger.
        commands: the command executor.
        lifecycle: the lifecycle facade the executor asks.
    """

    engine: Any
    store: Any
    clock: FakeClock
    conn: sqlite3.Connection
    ledger: Any
    commands: Any
    lifecycle: Any
    pid: int = 0


@pytest.fixture
def live_pid(register_child_process):
    """The pid of a process that is running right now.

    The facts reader decides "is this plan working" by asking whether
    the process is **alive**, so a case that wants a working plan needs
    a process that really is running.
    """
    process = register_child_process(
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
    )
    return int(process.pid)


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


@pytest.fixture
def scheduler(monkeypatch):
    """A scheduler the HTTP layer can talk to — a real engine, really running.

    The factory takes the capacity, the protection window and the
    command's timeout, and hands back the :class:`World`.  The engine is
    mounted on ``app.state.scheduler_engine`` — the handle the route
    reads — and the first tick is waited for, so that the case writes
    its plans into a world the tick has already reconciled.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.dryrun import ExecuteSink
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    started: List[Tuple[Any, sqlite3.Connection, sqlite3.Connection]] = []

    def _make(
        capacity: int = 1,
        min_runtime_seconds: float = MIN_RUNTIME_SECONDS,
        command_timeout_seconds: float = 10.0,
        lifecycle: Any = None,
    ) -> World:
        engine_conn = _open_state_db()
        case_conn = _open_state_db()
        store = SqliteSchedulerStore()
        clock = FakeClock()
        facts = PlanFactsReader(engine_conn, _plans_root(), clock=clock)
        ledger = CapacityLedger(store, facts, clock, conn=engine_conn)
        facade = lifecycle if lifecycle is not None else PlanLifecycleService()
        commands = CommandExecutor(
            store,
            ledger,
            facade,
            facts=facts,
            conn=engine_conn,
            capacity=capacity,
            min_runtime_seconds=min_runtime_seconds,
            clock=clock,
        )
        engine = PlanSchedulerEngine(
            _StaticConfig(
                SchedulerConfig(
                    enabled=True,
                    dry_run=False,
                    capacity=capacity,
                    min_runtime_seconds=min_runtime_seconds,
                    # One tick, and then a very long wait: the cases
                    # drive the scheduler through the **mailbox**, and a
                    # tick that kept running would decide things behind
                    # the case's back.
                    tick_seconds=3600.0,
                    command_timeout_seconds=command_timeout_seconds,
                )
            ),
            store=store,
            facts=facts,
            ledger=ledger,
            commands=commands,
            # The tick's exit: the decisions are carried out by nobody.
            # The cases drive the actions through the mailbox, and a
            # tick that started plans behind the case's back would make
            # every assertion about "who is running" a race.
            sink=ExecuteSink(lambda decision: decision),
            clock=clock,
            conn=engine_conn,
        )
        engine.start()
        _wait_for_tick(engine)
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        started.append((engine, engine_conn, case_conn))
        return World(
            engine=engine,
            store=store,
            clock=clock,
            conn=case_conn,
            ledger=ledger,
            commands=commands,
            lifecycle=facade,
        )

    yield _make

    for engine, engine_conn, case_conn in started:
        engine.stop()
        for conn in (engine_conn, case_conn):
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass


# ---------------------------------------------------------------------------
# The world the matrix drives
# ---------------------------------------------------------------------------

#: The four source phases a plan can be in when an action is asked for.
PHASES: Tuple[str, ...] = ("queued", "running", "stopped", "missing")

#: The three capacity states the actions are asked in.
CAPACITIES: Tuple[str, ...] = ("free", "full_no_victim", "full_with_victim")


def _make_working_plan(
    world: World, plan_id: str, elapsed: float, pid: int
) -> None:
    """Make ``plan_id`` a plan that is working and has held its slot.

    Three facts have to agree for a plan to be "working": the phase, the
    execution row (with a live pid), and the process state the facade
    reads.  The slot row says **when** it took the slot, which is what
    the eviction rule reads — so it is written with the elapsed time the
    case wants, not with the moment the case happened to run.
    """
    _make_plan(plan_id, "executing")
    _write_execution_row(
        world.conn,
        plan_id,
        exec_pid=pid,
        exec_status="running",
        project_dir=str(_plans_root() / plan_id / "project"),
    )
    _mark_execution_alive(plan_id)
    world.store.upsert_slot(
        world.conn, plan_id, now=_iso(BASE_EPOCH - elapsed)
    )


def _build_world(
    world: World,
    phase: str,
    capacity_state: str,
    pid: int,
    target: str = "p-target",
) -> Optional[str]:
    """Put the world into the state the matrix asks about.

    The **target** is the plan the action is asked about; the **filler**
    is the plan that holds the capacity.  They are different plans,
    because an action is asked about one plan and the capacity is
    everybody's business.

    Returns the filler's id, or ``None`` when there is no filler.
    """
    capacity = 2 if capacity_state == "free" else 1
    elapsed = (
        ELAPSED_EVICTABLE
        if capacity_state == "full_with_victim"
        else ELAPSED_PROTECTED
    )

    if phase == "running":
        # The target is the one that is working: it holds the capacity
        # itself, and it is the one the eviction rule would look at.
        _make_working_plan(world, target, elapsed, pid)
        return None

    if phase == "queued":
        _make_plan(target, "queued")
        world.store.enqueue(world.conn, target)
    elif phase == "stopped":
        _make_plan(target, "stopped")

    if capacity_state == "free":
        return None

    filler = "p-filler"
    _make_working_plan(world, filler, elapsed, pid)
    return filler


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

#: What each action answers, by the phase the plan is in.
#:
#: The capacity does not change these answers: none of the three may
#: take a slot or interrupt anybody, so the capacity is not theirs to
#: look at.
_EXPECTED: Dict[Tuple[str, str], Tuple[int, str]] = {
    ("promote", "queued"): (200, "applied"),
    ("promote", "running"): (409, "conflict"),
    ("promote", "stopped"): (409, "conflict"),
    ("promote", "missing"): (404, "not_found"),
    ("pause", "queued"): (200, "applied"),
    ("pause", "running"): (200, "applied"),
    ("pause", "stopped"): (409, "conflict"),
    ("pause", "missing"): (404, "not_found"),
    ("resume", "queued"): (409, "conflict"),
    ("resume", "running"): (409, "conflict"),
    ("resume", "stopped"): (200, "applied"),
    ("resume", "missing"): (404, "not_found"),
}

#: What ``start`` answers, by the phase and by the capacity.
#:
#: ``start`` is the only action that looks at the capacity: it is the
#: only one that may interrupt somebody.  With room it starts; with the
#: capacity full and nobody to interrupt it says ``blocked_by_capacity``
#: — which is a 200, because the caller asked a question and got an
#: answer; with the capacity full and a victim the rules allow, it
#: interrupts the victim and starts.
_EXPECTED_START: Dict[Tuple[str, str], Tuple[int, str]] = {
    ("queued", "free"): (200, "started"),
    ("queued", "full_no_victim"): (200, "blocked_by_capacity"),
    ("queued", "full_with_victim"): (200, "started"),
    ("running", "free"): (409, "conflict"),
    ("running", "full_no_victim"): (409, "conflict"),
    ("running", "full_with_victim"): (409, "conflict"),
    ("stopped", "free"): (200, "started"),
    ("stopped", "full_no_victim"): (200, "blocked_by_capacity"),
    ("stopped", "full_with_victim"): (200, "started"),
    ("missing", "free"): (404, "not_found"),
    ("missing", "full_no_victim"): (404, "not_found"),
    ("missing", "full_with_victim"): (404, "not_found"),
}

#: Every combination the matrix covers: four actions, four phases, three
#: capacity states.
MATRIX: List[Tuple[str, str, str]] = [
    (action, phase, capacity_state)
    for action in ACTIONS
    for phase in PHASES
    for capacity_state in CAPACITIES
]


def _expected(action: str, phase: str, capacity_state: str) -> Tuple[int, str]:
    """The status and the outcome the matrix expects for one cell."""
    if action == "start":
        return _EXPECTED_START[(phase, capacity_state)]
    return _EXPECTED[(action, phase)]


def _action_url(plan_id: str, action: str) -> str:
    """The endpoint one action is asked through."""
    return "/api/plan/{0}/scheduler/{1}".format(plan_id, action)


# ---------------------------------------------------------------------------
# 1 — the matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,phase,capacity_state", MATRIX)
def test_action_matrix_4x4(
    action: str,
    phase: str,
    capacity_state: str,
    scheduler: Any,
    live_pid: int,
    stop_handlers: Any,
) -> None:
    """Every action, in every phase, in every capacity state.

    The matrix is the evidence: each cell says what the endpoint answers
    for one combination of "what was asked", "what the plan is doing" and
    "how much room there is".  A rule that holds everywhere except one
    cell is a rule the panel gets wrong on exactly the plan the operator
    was looking at.
    """
    world = scheduler(
        capacity=2 if capacity_state == "free" else 1,
    )
    _build_world(world, phase, capacity_state, live_pid)

    response = client.post(_action_url("p-target", action))

    status, outcome = _expected(action, phase, capacity_state)
    assert response.status_code == status, (
        "{0} on a {1} plan with the capacity {2} answered {3}: {4}".format(
            action, phase, capacity_state, response.status_code, response.text
        )
    )
    body = response.json()
    assert body.get("outcome") == outcome, body


def test_action_matrix_covers_every_combination() -> None:
    """The matrix is the whole grid, not a sample of it.

    A matrix that quietly lost a cell would still pass every case in it:
    the coverage is the claim, and it is checked here rather than
    counted by eye.
    """
    assert len(MATRIX) == len(ACTIONS) * len(PHASES) * len(CAPACITIES)
    assert len(set(MATRIX)) == len(MATRIX), "the matrix has a duplicate cell"
    for action in ACTIONS:
        for phase in PHASES:
            for capacity_state in CAPACITIES:
                assert (action, phase, capacity_state) in MATRIX


# ---------------------------------------------------------------------------
# 2 — the shape of the answer
# ---------------------------------------------------------------------------


def test_action_response_carries_outcome_command_id_and_detail(
    scheduler: Any, live_pid: int
) -> None:
    """A 200 says what happened, which command did it, and how.

    The key set is closed: a field that appears without being declared is
    a field the panel cannot render and the LUI cannot rely on.
    """
    world = scheduler()
    _build_world(world, "queued", "free", live_pid)

    response = client.post(_action_url("p-target", "promote"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"outcome", "command_id", "detail"}, sorted(body)
    assert body["outcome"] == "applied", body
    assert isinstance(body["command_id"], int), body
    assert body["detail"], body


def test_conflict_carries_the_reason(scheduler: Any, live_pid: int) -> None:
    """A 409 says **why** it is a conflict.

    "Conflict" on its own leaves the operator guessing; the reason is
    the executor's own word for the conflict — ``running`` for a plan
    that is already working.
    """
    world = scheduler()
    _build_world(world, "running", "free", live_pid)

    response = client.post(_action_url("p-target", "promote"))

    assert response.status_code == 409, response.text
    body = response.json()
    assert set(body) == {"outcome", "command_id", "detail", "reason"}, sorted(
        body
    )
    assert body["outcome"] == "conflict", body
    assert body["reason"] == "running", body


def test_not_found_carries_the_outcome(scheduler: Any) -> None:
    """A 404 says the plan is not there — in the vocabulary, not in prose.

    The panel reads ``outcome``; a body that only said "not found" in
    prose would be a second vocabulary to keep in step with the first.
    """
    scheduler()

    response = client.post(_action_url("p-nobody", "promote"))

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["outcome"] == "not_found", body
    assert isinstance(body["command_id"], int), body


# ---------------------------------------------------------------------------
# 3 — the command that carried the action out
# ---------------------------------------------------------------------------


def test_both_200_and_202_carry_command_id(
    scheduler: Any, live_pid: int
) -> None:
    """A 200 and a 202 both name the command they are about.

    The command id is the only handle a caller has on a command that is
    still in flight, and it is the handle that lets the caller read the
    outcome the worker reached afterwards.  A response without it would
    be a response the caller cannot come back to.
    """
    world = scheduler()
    _build_world(world, "queued", "free", live_pid)

    answered = client.post(_action_url("p-target", "promote"))
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert isinstance(body["command_id"], int), body

    row = _command_row(world.conn, body["command_id"])
    assert row is not None, (
        "the command id the answer names does not exist in "
        "scheduler_command: {0!r}".format(body["command_id"])
    )
    assert row["outcome"] == "done", row
    assert "applied" in str(row["detail"]), row


def test_202_is_pending_and_carries_the_command_id(
    scheduler: Any, live_pid: int
) -> None:
    """A command that is not answered in time is 202 — and not withdrawn.

    The caller's wait is bounded; the command is not.  So the answer is
    202 with the command's id, the command is still carried out by the
    worker, and the row the id names ends up with a final outcome the
    caller can read.
    """
    from plan_lifecycle_service import PlanLifecycleService

    class SlowLifecycle(PlanLifecycleService):
        """The real lifecycle, with a pause that takes its time."""

        def __init__(self, delay: float) -> None:
            super().__init__()
            self._delay = delay

        def pause_plan(self, plan_id: str):
            time.sleep(self._delay)
            return super().pause_plan(plan_id)

    world = scheduler(
        command_timeout_seconds=0.01, lifecycle=SlowLifecycle(0.4)
    )
    _build_world(world, "queued", "free", live_pid)

    response = client.post(_action_url("p-target", "pause"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["outcome"] == "pending", body
    assert body["status"] == "pending", body
    assert isinstance(body["command_id"], int), body

    # The command was not withdrawn: the worker still carries it out,
    # and the row the id names ends up saying so.
    deadline = time.monotonic() + 20.0
    row = None
    while time.monotonic() < deadline:
        row = _command_row(world.conn, body["command_id"])
        if row is not None and row["outcome"] != "accepted":
            break
        time.sleep(0.02)
    assert row is not None, (
        "the command the 202 named is not in scheduler_command: "
        "{0!r}".format(body["command_id"])
    )
    assert row["outcome"] == "done", row


# ---------------------------------------------------------------------------
# 4 — the capacity
# ---------------------------------------------------------------------------


def test_start_blocked_without_victim(
    scheduler: Any, live_pid: int
) -> None:
    """Nobody to interrupt: the start does not happen, and says so.

    The plan stays at the head of the queue — it is the next one to run
    — and the answer is ``blocked_by_capacity``.  That is a 200: the
    operator asked what would happen and got an answer, and the answer
    is "not yet, and here is why".
    """
    world = scheduler(capacity=1)
    _build_world(world, "queued", "full_no_victim", live_pid)

    response = client.post(_action_url("p-target", "start"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "blocked_by_capacity", body
    assert isinstance(body["command_id"], int), body

    # The plan is still waiting, and it is still first in the line.
    assert _queue_order(world.conn)[0] == "p-target", _queue_order(world.conn)
    assert _phase_in_db(world.conn, "p-target") == "queued"


def test_start_evicts_and_returns_started(
    scheduler: Any, live_pid: int, stop_handlers: Any
) -> None:
    """A victim the rules allow: the start interrupts it, and says started.

    The victim is the plan that has been working for at least ``M``
    seconds, and it is stopped through the lifecycle's own stop path —
    so it keeps a place in the line instead of losing it.
    """
    world = scheduler(capacity=1)
    _build_world(world, "queued", "full_with_victim", live_pid)

    response = client.post(_action_url("p-target", "start"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "started", body
    assert isinstance(body["command_id"], int), body

    # The victim was stopped through the lifecycle's stop path.
    assert ("execution", "p-filler", False) in stop_handlers, stop_handlers
    assert _phase_in_db(world.conn, "p-filler") == "queued", (
        "the interrupted plan lost its place in the line"
    )


def test_resume_never_evicts(scheduler: Any, live_pid: int, stop_handlers: Any) -> None:
    """A resume is a wait, not a take: it interrupts nobody.

    This is the one rule the panel would get most wrong if it were
    broken: a resume that evicted somebody would stop work to make room
    for a plan that is content to wait.
    """
    world = scheduler(capacity=1)
    _build_world(world, "stopped", "full_with_victim", live_pid)

    response = client.post(_action_url("p-target", "resume"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "applied", body
    assert stop_handlers == [], (
        "the resume interrupted somebody: {0!r}".format(stop_handlers)
    )
    # The plan is back, and it is waiting — it did not take the slot.
    assert _queue_order(world.conn)[0] == "p-target", _queue_order(world.conn)
    assert _slot_rows(world.conn).get("p-target") is None, (
        "the resume took a slot: {0!r}".format(_slot_rows(world.conn))
    )


# ---------------------------------------------------------------------------
# 5 — the errors are passed through
# ---------------------------------------------------------------------------


def _refusing_lifecycle(monkeypatch: Any, exc: BaseException) -> Dict[str, Any]:
    """Make the **real** lifecycle refuse, and hand back its state.

    Only the facade's *answer* is replaced: the facade itself is the
    production one, so the case is about the refusal and not about a
    stand-in that agrees with the route.  The engine behind the route is
    the real one as well — a real store, a real ledger, the real facts
    reader, the real executor and a worker thread with a real mailbox —
    so the refusal travels the whole way: mailbox, worker, lifecycle,
    worker, caller, route.

    The returned dict holds the refusal the facade is about to give, so
    that one case can give several of them, one at a time.
    """
    from plan_lifecycle_service import PlanLifecycleService

    state: Dict[str, Any] = {"exc": exc}

    def _refusing_pause(self: Any, plan_id: str) -> Any:
        """Give the refusal the case is about."""
        raise state["exc"]

    monkeypatch.setattr(PlanLifecycleService, "pause_plan", _refusing_pause)
    return state


def test_conflict_error_passthrough_on_production_engine(
    scheduler: Any, monkeypatch: Any
) -> None:
    """A lifecycle that refuses with a conflict says 409 — not 500.

    The engine is the **production** one: a real store, a real ledger,
    the real facts reader, the real executor and a worker thread with a
    real mailbox.  Only the facade's *answer* is replaced, so the
    refusal travels the whole way — mailbox, worker, lifecycle, worker,
    caller, route — exactly as it does for the operator.

    The answer carries the lifecycle's own reason: the operator has to
    be able to tell "not now" from "you sent something wrong", and a
    500 would take that difference away.
    """
    from state_machine.repositories.routing_repository import ConflictError

    world = scheduler(capacity=1)
    _make_plan("p-target", "queued")
    _refusing_lifecycle(
        monkeypatch, ConflictError("p-target", "the row changed under the command")
    )

    response = client.post(_action_url("p-target", "pause"))

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["outcome"] == "conflict", body
    assert body["reason"] == "the row changed under the command", (
        "the lifecycle's own reason did not survive the trip: {0!r}".format(body)
    )


def test_http_exception_status_passthrough(
    scheduler: Any, monkeypatch: Any
) -> None:
    """The lifecycle's own 400 and 409 come through with their status.

    The status is the lifecycle's own and the detail is the
    lifecycle's own: a route that turned either of them into a 500
    would take away the difference between "you sent something wrong"
    and "not now".  The body is the body the lifecycle's own error
    would have had.
    """
    from fastapi import HTTPException

    world = scheduler(capacity=1)
    _make_plan("p-target", "queued")
    refusal = _refusing_lifecycle(monkeypatch, None)

    for status, detail in (
        (400, {"error": "the plan id is not valid"}),
        (409, "not now"),
    ):
        refusal["exc"] = HTTPException(status, detail)

        response = client.post(_action_url("p-target", "pause"))

        assert response.status_code == status, (
            "the route turned the lifecycle's {0} into {1}: {2}".format(
                status, response.status_code, response.text
            )
        )
        assert response.status_code != 500, response.text
        body = response.json()
        carried = body.get("detail", body) if isinstance(body, dict) else body
        assert carried == detail, (
            "the lifecycle's own detail did not survive the trip: "
            "{0!r} -> {1!r}".format(detail, body)
        )


def test_plan_not_found_still_404(scheduler: Any, monkeypatch: Any) -> None:
    """A plan the lifecycle does not know is still a 404.

    The refusal boundary must not swallow the one refusal the executor
    already had a word for: "there is no such plan" is the
    ``not_found`` outcome, and it is a 404.
    """
    from state_machine.repositories.routing_repository import PlanNotFoundError

    world = scheduler(capacity=1)
    _make_plan("p-target", "queued")
    _refusing_lifecycle(monkeypatch, PlanNotFoundError("p-target"))

    response = client.post(_action_url("p-target", "pause"))

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["outcome"] == "not_found", body


def test_unexpected_failure_still_500(scheduler: Any, monkeypatch: Any) -> None:
    """A fault that is not a refusal is still a 500.

    The explicit error boundary must not degrade: **not** every
    exception may be passed through as a refusal.  A lifecycle that
    blows up is a fault of the scheduler's own, and it says so — with
    the ``failed`` outcome, which is the word for exactly that.
    """
    world = scheduler(capacity=1)
    _make_plan("p-target", "queued")
    _refusing_lifecycle(monkeypatch, RuntimeError("the lifecycle is broken"))

    response = client.post(_action_url("p-target", "pause"))

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["outcome"] == "failed", body


def test_refused_command_is_recorded(scheduler: Any, monkeypatch: Any) -> None:
    """A refusal is not a lost command: it is on record, and it says why.

    The record says the command was **carried out** and the action was
    **refused** — those are two different things, and a record that said
    only "failed" would take away the difference between "the scheduler
    is broken" and "the lifecycle said no".  The record is final, and it
    is found by the command's id.
    """
    from state_machine.repositories.routing_repository import ConflictError

    world = scheduler(capacity=1)
    _make_plan("p-target", "queued")
    _refusing_lifecycle(
        monkeypatch, ConflictError("p-target", "the row changed under the command")
    )

    response = client.post(_action_url("p-target", "pause"))

    assert response.status_code == 409, response.text

    row = world.conn.execute(
        "SELECT command_id FROM scheduler_command "
        "WHERE plan_id = ? ORDER BY command_id DESC LIMIT 1",
        ("p-target",),
    ).fetchone()
    assert row is not None, "the refused command was not written down at all"

    # The record is looked up by the command's id — the id the caller
    # may come back with.
    record = _command_row(world.conn, int(row[0]))
    assert record is not None, "the refused command was not written down at all"
    assert record["outcome"] == "done", (
        "the refused command was left as {0!r}".format(record["outcome"])
    )
    assert "rejected" in str(record["detail"]), (
        "the record does not say the action was refused: {0!r}".format(
            record["detail"]
        )
    )
    assert "409" in str(record["detail"]), (
        "the record does not carry the status: {0!r}".format(record["detail"])
    )
    assert "the row changed under the command" in str(record["detail"]), (
        "the record does not carry the reason: {0!r}".format(record["detail"])
    )


def test_malformed_plan_id_is_the_guard_s_own_400(scheduler: Any) -> None:
    """A malformed plan id is the shared guard's 400, unchanged.

    The guard is the one place a request-supplied id may become a path,
    and it answers with its own body.  The route lets that answer
    through: it does not replace a bad request with a missing plan.

    The segments are percent-encoded so that they arrive as **one** path
    segment: a decoded ``/`` would split the path and the request would
    never reach the endpoint at all — which would be the catch-all's
    405, not the guard's 400.
    """
    scheduler()

    for raw, segment in (
        ("..", "%2e%2e"),            # traversal component
        ("plan id", "plan%20id"),    # whitespace — outside [A-Za-z0-9._-]
        ("plan\x00id", "plan%00id"),  # control byte
    ):
        response = client.post("/api/plan/{0}/scheduler/promote".format(segment))

        assert response.status_code == 400, (
            "a malformed plan id ({0!r}) must be refused with 400; got "
            "{1}: {2!r}".format(raw, response.status_code, response.text)
        )
        body = response.json()
        assert body.get("error") == "Invalid plan id", body


# ---------------------------------------------------------------------------
# 6 — the routes are where the panel expects them
# ---------------------------------------------------------------------------


def test_action_routes_are_registered_before_the_catch_all() -> None:
    """The four endpoints are on the app, ahead of the frontend catch-all.

    The registration order is what a 404 of a POST is made of, so it is
    asserted directly rather than only through its symptom.
    """
    paths = [route.path for route in server.iter_app_routes()]

    for action in ACTIONS:
        path = _action_url("{plan_id}", action)
        assert path in paths, (
            "the {0} endpoint is not registered on the app".format(action)
        )
        assert paths.index(path) < paths.index("/{path:path}"), (
            "the {0} endpoint is registered after the frontend catch-all".format(
                action
            )
        )
