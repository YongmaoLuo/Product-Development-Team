"""Regression nails for the two historical plan-scheduler defects.

Background
----------
The native scheduler replaced a hand-rolled "one plan at a time" runner
that had two defects worth a nail each.  Both of them cost real work,
and both of them are invisible in a green suite unless something pins
them:

**Defect 1 — a plan that enters ``queued`` never enters the line.**

The scheduler collects the plans that are waiting in two places: the
start-up scan (``_backfill_queue``), which runs once when the scheduler
starts, and the tick's own reconciliation (``_phase_reconcile_queue``),
which runs on every turn.  The start-up scan cannot see a plan that is
imported *after* it ran, so the tick is the only thing that can put
such a plan in the line.  With the tick's reconciliation missing, the
plan waits for ever: it is in ``queued``, it is due, there is a free
slot — and it is never admitted, because it was never in the queue.

The import path promises this cannot happen.  ``routes.fast_lane``
writes the queue row in a step *after* the plan is marked ``queued``,
and when that write fails it says so in as many words: "the scheduler
reconciles a waiting plan that has no queue row at its next tick, so the
plan still runs — at the default priority".  A plan that is waiting
without a queue row is therefore a state the production code promises
to handle, and it is the state this case drives.

**Defect 2 — the adoption is lost.**

A restart has to take over the plans that are still working.  The
scheduler does that in the recovery (``_recover_slots``): a plan that is
working is **adopted** — it gets a slot, and its slot is timed from
*now*.  An adopted plan has not had its turn yet, so it is protected for
a whole M; carrying an old timestamp over would put the plan into the
eviction candidates the moment it was taken over, and the takeover
would evict the very work it had just taken over.  When the adoption
was lost, the plan that was running across the restart was either
invisible to the scheduler (its slot was gone, so the capacity looked
free) or carried a stale timestamp (so it was evictable at once).

What these cases do
-------------------
Each case drives the **production** scheduler — the same engine, store,
facts, ledger, command executor and lifecycle the server assembles in
``server._build_scheduler_engine`` — and observes the world through the
production write paths:

* a plan is put in ``queued`` by ``routes.fast_lane._mark_queued``,
  the same two writes the import makes;
* a plan is started by the lifecycle, which is the only thing allowed  to move a plan's phase;
* the state is read back through the repositories and through the
  scheduler's own store.

No case writes a scheduling row into ``state.db`` by hand.  A row that
was written by hand would prove only that SQLite holds what the test
put there; the rows these cases read were written by the code under
test.

The negative controls
---------------------
Both defects are *absences* — a phase that does not run, a step that
does not happen — so a green suite cannot tell them apart from a
scheduler that never had the problem.  Each case therefore names the
control that turns it red, and the control is the same defect the case
is about, with the offending step switched off.

**The method.**  The control is applied at runtime: a scratch pytest
plugin (outside the repository, never committed) replaces the one
method that implements the step, so the engine is in exactly the state
it would be in with that code deleted — without the production file
being edited, and without a tree left dirty behind it.

``test_regression_queued_plan_is_admitted`` — **defect 1**
    Control: ``_phase_reconcile_queue`` stops enqueueing, so the
    start-up scan is the only scan there is.  The plan is imported
    after the scan has run, so nothing puts it in the line, nothing
    admits it, and the case fails on the missing ``plan_execution``
    row.  *Observed:* this case alone goes red, with the engine
    reporting no errors of its own — the scheduler is healthy, and the
    plan is simply never admitted.

``test_regression_adoption_keeps_plan_running`` and
``test_adoption_resets_acquired_at`` — **defect 2**
    Control: ``_recover_slots`` keeps the slot row it finds instead
    of re-timing it, so the row survives the restart with the moment
    it was written.  The plan is still running, but its slot says it
    took the slot an hour ago: it is an eviction candidate the moment
    it is taken over, and the takeover would evict the very work it
    had just taken over.  *Observed:* both cases go red — one on the
    plan no longer being protected, one on the stale ``acquired_at``.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

import server
from plan_lifecycle_service import PlanLifecycleService
from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfigLoader
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore
from routes import fast_lane
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository

# ---------------------------------------------------------------------------
# The constants
# ---------------------------------------------------------------------------

#: The stand-in executor.  Resolved through this file's own location, so
#: the path is right on every checkout and no absolute path is written
#: down anywhere.
EXECUTOR = (
    Path(__file__).resolve().parents[2]
    / "e2e"
    / "fixtures"
    / "deterministic_executor.py"
)

#: The moment the scheduler starts at: 2026-01-01T00:00:00Z.  The clock
#: is injected, so the case moves time by hand instead of waiting for
#: it — an hour of waiting is an hour of the suite's life.
BASE_EPOCH = 1767225600.0

#: How long the second scheduler starts an hour after the first one.
#: The gap has to be larger than M, or the two moments would not be
#: distinguishable: the point of the restart is that the plan had been
#: running long enough to be evictable if its slot were carried over.
RESTART_OFFSET = 3600.0

#: How long a young run is protected for — the scheduler's M.
MIN_RUNTIME_SECONDS = 600.0

#: How long the scheduler waits between two ticks.  Short, because the
#: cases wait for ticks.
TICK_SECONDS = 0.05

#: How long a helper waits for the world to reach a state before it
#: gives up and reports what it saw.  Generous on purpose: the machine
#: running the test may be busy, and a timeout here means "not yet",
#: not "never".
SETTLE_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


class _Clock:
    """A clock the case moves by hand.

    The scheduler asks the clock two different questions: *how often
    should it look* (real time, and the engine keeps that to itself)
    and *when did this happen* (the injected clock).  The second
    question is the one the cases need to control: the moment a plan
    took its slot and the moment a restarted scheduler takes it over
    have to be two different moments, or "the slot was re-timed" is not
    a thing anybody can observe.
    """

    def __init__(self, now: float) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now

    def set(self, now: float) -> None:
        """Move the clock to ``now``."""
        self._now = float(now)

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now += float(seconds)


def _iso(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps.

    The same format the scheduler's own stamp uses — the state database
    writes UTC, second resolution, with a ``Z`` suffix.
    """
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class _World:
    """One scheduler, over one database, with one plans tree."""

    conn: sqlite3.Connection
    store: SqliteSchedulerStore
    facts: PlanFactsReader
    ledger: CapacityLedger
    engine: PlanSchedulerEngine


def _world(tmp_path: Path, clock: _Clock, capacity: int = 1) -> _World:
    """Assemble the scheduler the way the server assembles it.

    The objects are the production ones — the engine, the store, the
    facts reader, the ledger, the command executor and the lifecycle    — wired the way ``server._build_scheduler_engine`` wires them, with
    the same clock injected into each of them.  The only thing that is
    not production is the configuration file: it is written into the
    case's own directory so that the capacity and the tick cadence are
    the case's to choose.
    """
    config_path = tmp_path / "scheduler.yaml"
    config_path.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: false\n"
        f"  capacity: {capacity}\n"
        f"  min_runtime_seconds: {MIN_RUNTIME_SECONDS}\n"
        f"  tick_seconds: {TICK_SECONDS}\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )

    loader = SchedulerConfigLoader(config_path)
    conn = open_db(server._state_db_path())
    migrate(conn)
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, server.PLANS_DIR, clock=clock)
    ledger = CapacityLedger(store, facts, clock=clock, conn=conn)
    lifecycle = PlanLifecycleService()
    commands = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=MIN_RUNTIME_SECONDS,
        clock=clock,
    )
    sink = server._scheduler_sink(loader.current(), lifecycle, facts)
    engine = PlanSchedulerEngine(
        loader,
        store,
        facts,
        ledger,
        commands,
        sink,
        clock=clock,
        conn=conn,
    )
    return _World(conn=conn, store=store, facts=facts, ledger=ledger, engine=engine)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _write_plan(plan_id: str, tmp_path: Path) -> Path:
    """Write a minimal, legal plan directory for ``plan_id``.

    Three things make a plan directory legal for the scheduler: the task
    list it has to run, the project directory it runs in, and a phase
    the routing table can carry.  The task list is written here; the
    phase is written by :func:`_mark_queued`, the way the import
    writes it.

    The project directory is a real directory of its own, outside
    ``backend/`` — the execution side refuses to work inside the
    service's own tree.
    """
    plan_dir = server._plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)

    project_dir = tmp_path / "workspaces" / plan_id
    project_dir.mkdir(parents=True, exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": (
                    "A regression nail: the plan has to be admitted, and it "
                    "has to keep running across a restart."
                ),
                "project_dir": str(project_dir),
                "tasks": [
                    {
                        "id": f"{plan_id}-1",
                        "title": "The one task this minimal plan has",
                        "description": (
                            "The plan exists so that the scheduler has "
                            "something to admit."
                        ),
                        "test_command": "true",
                        "project_dir": str(project_dir),
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return plan_dir


def _mark_queued(plan_id: str, plan_dir: Path) -> None:
    """Put the plan in ``queued`` the way the import path does.

    ``routes.fast_lane._mark_queued`` is the production write path: it
    seeds the routing row and moves the phase through the state store,
    and it is called here directly so that the case drives the same two
    writes the import drives.

    The **queue row is deliberately not written**.  The import path
    writes it in a separate step, and it may fail to be written at all
    — fast_lane says so in as many words, and promises that the
    scheduler will pick the plan up at its next tick.  A plan that is
    waiting without a queue row is therefore a state the production
    code promises to handle, and it is the state the first regression
    case is about.
    """
    fast_lane._mark_queued(plan_id, plan_dir)


# ---------------------------------------------------------------------------
# Reading the world back
# ---------------------------------------------------------------------------


def _execution_row(conn: sqlite3.Connection, plan_id: str) -> Optional[Dict[str, Any]]:
    """Return the plan's execution row, or ``None`` when it has none."""
    return ExecutionRepository(conn).summary(plan_id)


def _phase(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """Return the plan's phase, or ``None`` when the routing row is gone."""
    row = RoutingRepository(conn).current(plan_id)
    if row is None:
        return None
    return row.get("current_phase")


def _pid_is_alive(pid: Optional[int]) -> bool:
    """Return whether the process ``pid`` is still there."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # It is there, and it belongs to somebody else.
        return True
    return True


def _executor_pids() -> List[int]:
    """The pids of the live processes running the stand-in executor.

    Read from the process table rather than from anything the executor
    said about itself: the count is taken from where the operating
    system keeps it.  Only the children of this process are counted —
    the stand-in is spawned by the scheduler, which runs in this
    process, so a stand-in that belongs to another test is not this
    test's business.

    The two ``w`` options are load-bearing.  ``ps`` cuts the command
    column down to the width of the screen it believes it is writing
    to — eighty columns when it cannot find one — and the stand-in's
    command line is longer than that, because the file name sits at
    the end of a path of a hundred characters.  A cut line does not
    contain the file name, so the filter below drops the very process
    it went looking for and the count comes back empty: a process that
    is alive and running is reported as no process at all.  ``-ww``
    turns the cutting off, so what is read is the whole command line.
    The same trap is spelled out in
    ``tests/e2e/test_keychain_full_delivery_macos.py``, where a cut
    environment would be read as an absent secret.
    """
    listing = subprocess.run(
        ["ps", "-ww", "-A", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pids: List[int] = []
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if parent != os.getpid():
            continue
        if EXECUTOR.name not in fields[2]:
            continue
        pids.append(pid)
    return sorted(pids)


def _wait_for(
    predicate: Callable[[], Any],
    what: str,
    engine: Optional[PlanSchedulerEngine] = None,
    timeout: float = SETTLE_TIMEOUT,
) -> Any:
    """Wait until ``predicate`` answers something true.

    A timeout means "not yet", not "never" — but a case that waits for
    ever is a case that hangs the suite, so the wait is bounded and
    the failure says what the scheduler was doing when it gave up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    detail = ""
    if engine is not None:
        detail = f"; the engine's errors were: {engine.errors}"
    raise AssertionError(f"{what} did not happen within {timeout}s{detail}")


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _executor_override(monkeypatch):
    """Swap the executor for the deterministic stand-in.

    ``PDT_EXECUTOR_CMD`` is the production seam that names the whole
    command the execution side spawns, and the stand-in is a real
    process: a real pid, a real process group, and an exit that
    happens when the case says so.  The stand-in stays alive until it
    is signalled, so the plan is running for as long as the case needs
    it to be.

    The ``PDT_E2E_*`` variables are cleared first: a value that
    happened to be set in the shell the test runs under must not decide
    how long the stand-in lives.
    """
    for name in [key for key in os.environ if key.startswith("PDT_E2E_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "PDT_EXECUTOR_CMD", f'"{sys.executable}" -u "{EXECUTOR}"'
    )


@pytest.fixture(autouse=True)
def _reclaim_executors():
    """Hand back the stand-in processes this case started.

    A stand-in that outlives its case is a stand-in the next case
    counts: the process count these cases assert on is the count of
    the processes this process started, so a leak from the previous case
    would be read as a second process here.
    """
    yield
    for pid in _executor_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _executor_pids():
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# The shared scenario
# ---------------------------------------------------------------------------


def _plan_running_under(tmp_path: Path, clock: _Clock, plan_id: str) -> _World:
    """Bring a plan up under a scheduler and return the world running it.

    The plan is waiting *before* the scheduler starts, so the start-up
    scan puts it in the line and the first tick admits it.  The plan is
    then running with a real process, and the scheduler holds its slot.

    The scheduler is stopped at the end: the plan's process outlives it,
    which is what a restart looks like from the plan's side — the work
    keeps running while the scheduler that started it goes away.
    """
    world = _world(tmp_path, clock)
    plan_dir = _write_plan(plan_id, tmp_path)
    _mark_queued(plan_id, plan_dir)

    world.engine.start()
    try:
        _wait_for(
            lambda: _execution_row(world.conn, plan_id) is not None,
            f"the plan {plan_id} to start",
            engine=world.engine,
        )
    finally:
        world.engine.stop()
    return world


# ---------------------------------------------------------------------------
# Defect 1 — a plan that enters ``queued`` never enters the line
# ---------------------------------------------------------------------------


def test_regression_queued_plan_is_admitted(tmp_path):
    """Defect 1: a plan that enters ``queued`` after the start-up scan
    is admitted by the next tick.

    Reproduction condition
    ----------------------
    * the scheduler is **already running** — its start-up scan has run
      and found nothing; and
    * the plan is imported **afterwards**, so it is waiting but has no
      queue row (the import writes that row in a separate step, and
      that step may fail without undoing the import).

    Negative control
    ----------------
    Switch the tick-level reconciliation off (``_phase_reconcile_queue``
    stops enqueueing, which is the engine with that code deleted) and
    leave only the start-up scan: the plan is never enqueued, so the
    fill phase has no head of the line to admit, and this case fails
    on the missing ``plan_execution`` row.
    """
    clock = _Clock(BASE_EPOCH)
    world = _world(tmp_path, clock)
    plan_id = "20261028-regression-admission-p1"
    plan_dir = _write_plan(plan_id, tmp_path)

    world.engine.start()
    try:
        # The start-up scan has run, and it found nothing: there is no
        # plan to take over and nobody in the line.
        _wait_for(
            lambda: world.engine.ticks >= 1,
            "the first tick",
            engine=world.engine,
        )
        assert world.store.list_slots(world.conn) == []
        assert world.store.list_queue(world.conn) == []

        # The plan is imported now: it enters ``queued`` with no queue
        # row, because the import's queue write is a separate step and
        # it may fail without undoing the import.
        clock.advance(60.0)
        _mark_queued(plan_id, plan_dir)

        # The next tick puts the plan in the line and admits it.
        _wait_for(
            lambda: _execution_row(world.conn, plan_id) is not None,
            f"the plan {plan_id} to be admitted",
            engine=world.engine,
        )
    finally:
        world.engine.stop()

    # The plan is running: the execution row says so, and it carries
    # the pid of a real process.
    row = _execution_row(world.conn, plan_id)
    assert row is not None
    assert row["exec_status"] == "running"
    assert row["exec_pid"]
    assert _pid_is_alive(row["exec_pid"])

    # The scheduler holds its slot.
    slots = world.store.list_slots(world.conn)
    assert [slot["plan_id"] for slot in slots] == [plan_id]

    # The plan moved on to ``executing`` — the lifecycle moved it, not
    # the test.
    assert _phase(world.conn, plan_id) == "executing"

    # And the admission was decided by a **tick**, not by the start-up
    # scan: the decision is stamped with the moment the plan was
    # imported, and the start-up scan ran a minute earlier.
    admits = [
        decision
        for decision in world.engine.decisions
        if decision["action"] == "admit" and decision["plan_id"] == plan_id
    ]
    assert len(admits) == 1
    assert admits[0]["ts"] == _iso(BASE_EPOCH + 60.0)


# ---------------------------------------------------------------------------
# Defect 2 — the adoption is lost
# ---------------------------------------------------------------------------


def test_regression_adoption_keeps_plan_running(tmp_path):
    """Defect 2: a plan that is running across a restart keeps running,
    and no second process is derived for it.

    Reproduction condition
    ----------------------
    * a plan is running under a scheduler, with a live process; and
    * the scheduler restarts — a new engine over the same database,
      an hour later.

    Negative control
    ----------------
    Switch the re-timing off (``_recover_slots`` keeps the slot row it
    finds, which is the engine with that code deleted): the plan is
    still running, but its slot says it took the slot an hour ago, so
    it is an eviction candidate the moment it is taken over.  This case
    fails on the plan no longer being protected.
    """
    clock = _Clock(BASE_EPOCH)
    plan_id = "20261028-regression-adoption-p2"

    world_a = _plan_running_under(tmp_path, clock, plan_id)
    row_a = _execution_row(world_a.conn, plan_id)
    assert row_a is not None
    pid = row_a["exec_pid"]
    assert pid and _pid_is_alive(pid)
    assert _executor_pids() == [pid]

    # The scheduler restarts an hour later.  The plan is still running
    # and its process is alive: that is the situation the recovery has
    # to take over.
    clock.set(BASE_EPOCH + RESTART_OFFSET)
    world_b = _world(tmp_path, clock)
    world_b.engine.start()
    try:
        _wait_for(
            lambda: world_b.engine.ticks >= 1,
            "the first tick after the restart",
            engine=world_b.engine,
        )
    finally:
        world_b.engine.stop()

    # The plan kept running: the same process, still alive, and the
    # phase it reached before the restart.
    row_b = _execution_row(world_b.conn, plan_id)
    assert row_b is not None
    assert row_b["exec_pid"] == pid
    assert row_b["exec_status"] == "running"
    assert _pid_is_alive(pid)
    assert _phase(world_b.conn, plan_id) == "executing"

    # No second process was derived for it.
    assert _executor_pids() == [pid]

    # The scheduler took the plan over: it holds the plan's slot.
    slots = world_b.store.list_slots(world_b.conn)
    assert [slot["plan_id"] for slot in slots] == [plan_id]

    # And the plan it took over is protected: an adopted plan has not
    # had its turn yet, so it is not an eviction candidate.
    assert (
        world_b.ledger.evictable(
            BASE_EPOCH + RESTART_OFFSET, MIN_RUNTIME_SECONDS
        )
        == []
    )


def test_adoption_resets_acquired_at(tmp_path):
    """Defect 2: the slot of an adopted plan is timed from the moment of
    the takeover, not from the moment it first took it.

    Reproduction condition
    ----------------------
    * a plan is running under a scheduler, with a live process, and its
      slot says when it took it; and
    * the scheduler restarts — a new engine over the same database, an
      hour later.

    Negative control
    ----------------
    Switch the re-timing off (``_recover_slots`` keeps the slot row it
    finds, which is the engine with that code deleted): the row
    survives the restart with the old timestamp, and this case fails on
    the stale ``acquired_at``.
    """
    clock = _Clock(BASE_EPOCH)
    plan_id = "20261028-regression-adoption-p3"

    world_a = _plan_running_under(tmp_path, clock, plan_id)

    # The slot the first scheduler wrote: the moment the plan was
    # admitted.
    slots_a = world_a.store.list_slots(world_a.conn)
    assert [slot["plan_id"] for slot in slots_a] == [plan_id]
    assert slots_a[0]["acquired_at"] == _iso(BASE_EPOCH)

    # The scheduler restarts an hour later.
    clock.set(BASE_EPOCH + RESTART_OFFSET)
    world_b = _world(tmp_path, clock)
    world_b.engine.start()
    try:
        _wait_for(
            lambda: world_b.engine.ticks >= 1,
            "the first tick after the restart",
            engine=world_b.engine,
        )
    finally:
        world_b.engine.stop()

    # The adopted plan's slot is timed from the takeover.
    slots_b = world_b.store.list_slots(world_b.conn)
    assert [slot["plan_id"] for slot in slots_b] == [plan_id]
    assert slots_b[0]["acquired_at"] == _iso(BASE_EPOCH + RESTART_OFFSET)
    assert slots_b[0]["acquired_at"] != slots_a[0]["acquired_at"]