"""Integration tests: a pause or a resume waits out a locked database.

Background
----------
The scheduler's pause and resume are two calls into the lifecycle
facade, and the facade writes to the state database.  The state
database is shared with every other writer in the process, so a write
that arrives while somebody else holds the write lock comes back as
SQLite's ``database is locked``.

Before, one lock was enough to fail the command.  The exception came
out of the lifecycle, the executor turned it into a conflict, and the
engine logged "the scheduler's pause command ... failed:
OperationalError: database is locked" — for a plan that was never
actually stopped.  Meanwhile the pause intent was already on disk (it
is written *before* the stop), so the repair round was told "a pause
was asked for" while the operator was told "the pause failed".  Two
stories that do not match, and the operator is the one who has to
reconcile them.

What this file holds down
-------------------------
1. **One lock does not stop a pause.**  The lifecycle call is waited
   out and the pause is carried out.
2. **A lock that does not go away is a conflict with its own name.**
   The detail is ``database_locked`` — a reason an operator can act
   on, and one that is not confused with the other conflicts (a phase
   that does not allow the pause, a plan that is running).
3. **A pause whose intent could not be written is not retried.**  The
   lifecycle answered "not ok": the disk is not going to start
   cooperating because we waited, and the answer keeps the lifecycle's
   own reason.
4. **A resume waits the lock out too.**
5. **An error that is not a lock is not retried.**  A missing table is
   not going to appear because we waited.
6. **An exhausted pause leaves the intent on disk.**  The repair round
   still needs it: the executor may already be dead, and the intent is
   what tells a pause from a crash.

How the lock is produced
------------------------
The lock is *scripted*: the lifecycle raises SQLite's own
``OperationalError("database is locked")`` — the error the production
sees, with the production's own wording.  Nothing is timed and no
second process is started, so what is under test is what the executor
does with the error, and that has to be the same on every machine.

The one case that needs the lock to arrive *inside* the lifecycle
(the intent has to be on disk before the lock) scripts it one level
down: the queue write the lifecycle makes while parking the plan is
the one that loses the database.  That is the production order — the
intent lands, then the database is contended.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

pytestmark = pytest.mark.integration


#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a plan is protected for — the ``M`` of the eviction rule.
MIN_RUNTIME_SECONDS = 600.0

#: The phase a plan is in while it waits for its turn to run.
QUEUED = "queued"

#: The phase a plan is in after it was parked.
STOPPED = "stopped"


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand.

    It is callable, because that is what the ledger, the facts reader
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


def _plans_root() -> Path:
    """The plans tree the facade and the facts reader resolve plans in."""
    import server

    return Path(server.PLANS_DIR)


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot and
    then materialised into ``plan_routing`` — the same path a plan
    that was bootstrapped by the API takes.  The row is not optional:
    the scheduler reads the phase from ``plan_routing``, and a plan
    whose row was never written is a plan the scheduler does not know.
    """
    from plan_state import PlanState

    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    PlanState(plan_dir).ensure_row()
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


def _executor(
    conn: sqlite3.Connection,
    clock: FakeClock,
    lifecycle: Any,
) -> Any:
    """A command executor over the case's database.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader and the lifecycle facade.  Only the clock
    and the capacity are the case's own.
    """
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
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=1,
        min_runtime_seconds=MIN_RUNTIME_SECONDS,
        clock=clock,
    )


class _Calls:
    """How many times the case's patched method was called."""

    def __init__(self) -> None:
        self.count = 0


def _counting(lifecycle: Any, name: str) -> _Calls:
    """Wrap ``lifecycle.name`` so the case can see how often it was called.

    The wrapper is installed on the **instance**, so the method it hides
    is the production one: every call goes straight through to the real
    implementation, and the case only counts them.
    """
    original = getattr(lifecycle, name)
    calls = _Calls()

    def _counting_method(plan_id: str) -> Any:
        calls.count += 1
        return original(plan_id)

    setattr(lifecycle, name, _counting_method)
    return calls


def _locked_lifecycle(lifecycle: Any, name: str, times: int) -> _Calls:
    """Make ``lifecycle.name`` lose the database ``times`` times.

    The error is the one SQLite really raises when the write lock is
    held elsewhere: ``sqlite3.OperationalError`` carrying the phrase
    ``database is locked``.  The executor cannot tell this lock from a
    real one, which is the point.  A call that is not a fire goes
    straight through to the production method.
    """
    original = getattr(lifecycle, name)
    calls = _Calls()
    left = max(0, int(times))

    def _failing(plan_id: str) -> Any:
        nonlocal left
        calls.count += 1
        if left > 0:
            left -= 1
            raise sqlite3.OperationalError("database is locked")
        return original(plan_id)

    setattr(lifecycle, name, _failing)
    return calls


def _locked_queue_write(monkeypatch: Any, times: int) -> _Calls:
    """Make the queue write inside the lifecycle lose the database.

    The lifecycle parks a plan by taking its queue row away, and that
    write is the one that runs **after** the pause intent has landed.
    Scripting the lock here therefore reproduces the production order:
    the intent is on disk, and then the database is contended.
    """
    import plan_scheduler.queue as queue_module

    original = queue_module.remove
    calls = _Calls()
    left = max(0, int(times))

    def _failing(conn: sqlite3.Connection, plan_id: str) -> bool:
        nonlocal left
        calls.count += 1
        if left > 0:
            left -= 1
            raise sqlite3.OperationalError("database is locked")
        return original(conn, plan_id)

    monkeypatch.setattr(queue_module, "remove", _failing)
    return calls


@pytest.fixture()
def state_db() -> Iterator[sqlite3.Connection]:
    """A migrated connection to the case's own state database."""
    conn = _open_state_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_pause_retries_locked_then_applies(state_db: sqlite3.Connection) -> None:
    """One lock does not stop a pause.

    The lifecycle is asked to park the plan, the database is busy, and
    the executor waits it out and asks again.  The pause happens, the
    plan is really parked, and the operator is told the truth.
    """
    from plan_lifecycle_service import PlanLifecycleService

    clock = FakeClock()
    _make_plan("p1", QUEUED)
    lifecycle = PlanLifecycleService()
    calls = _locked_lifecycle(lifecycle, "pause_plan", times=1)
    executor = _executor(state_db, clock, lifecycle)

    result = executor.pause("p1")

    assert result.outcome == "applied", result.detail
    assert calls.count == 2, "the lock was not waited out"
    assert _phase_in_db("p1") == STOPPED, "the plan was not parked"


def test_pause_conflicts_when_exhausted(state_db: sqlite3.Connection) -> None:
    """A lock that does not go away is a conflict with its own name.

    The attempts are spent, and the executor says so in words an
    operator can act on: ``database_locked``.  That is a different
    situation from "the phase does not allow this pause" or "the plan
    is running", and it must not borrow their names.
    """
    from plan_lifecycle_service import PlanLifecycleService

    clock = FakeClock()
    _make_plan("p1", QUEUED)
    lifecycle = PlanLifecycleService()
    calls = _locked_lifecycle(lifecycle, "pause_plan", times=99)
    executor = _executor(state_db, clock, lifecycle)

    result = executor.pause("p1")

    assert result.outcome == "conflict", result.outcome
    assert result.detail == "database_locked", result.detail
    assert calls.count == 4, "the lock was waited out more or less often than the retryer allows"
    assert _phase_in_db("p1") == QUEUED, "the plan was parked although the pause never happened"


def test_intent_failure_is_conflict_not_retry(
    state_db: sqlite3.Connection, monkeypatch: Any
) -> None:
    """A pause whose intent could not be written is not retried.

    The lifecycle wrote nothing and stopped nothing, and it said so:
    ``ok`` is false and the detail names the reason.  Waiting does not
    make the disk cooperate, so the call is made **once** and the
    lifecycle's own reason is the answer.
    """
    import pause_state
    from plan_lifecycle_service import (
        PAUSE_INTENT_WRITE_FAILED_DETAIL,
        PlanLifecycleService,
    )

    clock = FakeClock()
    _make_plan("p1", QUEUED)
    lifecycle = PlanLifecycleService()
    calls = _counting(lifecycle, "pause_plan")
    executor = _executor(state_db, clock, lifecycle)

    def _no_disk(plan_dir: Any, *, reason: str) -> Any:
        raise OSError("the disk is not cooperating")

    monkeypatch.setattr(pause_state, "write_pause_intent", _no_disk)

    result = executor.pause("p1")

    assert result.outcome == "conflict", result.outcome
    assert result.detail == PAUSE_INTENT_WRITE_FAILED_DETAIL, result.detail
    assert calls.count == 1, "a pause that could not write its intent was retried"
    assert _phase_in_db("p1") == QUEUED, "the plan was parked although nothing was stopped"


def test_resume_retries_locked_then_applies(state_db: sqlite3.Connection) -> None:
    """A resume waits the lock out too.

    The resume is the other half of the pause, and it writes to the
    same shared database.  One lock does not stop it either: the plan
    comes back to the head of the queue and the answer says it did.
    """
    from plan_lifecycle_service import PlanLifecycleService

    clock = FakeClock()
    _make_plan("p1", STOPPED)
    lifecycle = PlanLifecycleService()
    calls = _locked_lifecycle(lifecycle, "requeue_plan", times=1)
    executor = _executor(state_db, clock, lifecycle)

    result = executor.resume("p1")

    assert result.outcome == "applied", result.detail
    assert calls.count == 2, "the lock was not waited out"
    assert _phase_in_db("p1") == QUEUED, "the plan did not come back"


def test_non_locked_error_is_not_retried(state_db: sqlite3.Connection) -> None:
    """An error that is not a lock is raised at once.

    A missing table is not going to appear because the executor
    waited.  The error keeps its meaning and reaches the caller after
    a single attempt, exactly as it did before the retry was added.
    """
    from plan_lifecycle_service import PlanLifecycleService

    clock = FakeClock()
    _make_plan("p1", QUEUED)
    lifecycle = PlanLifecycleService()
    calls = _Calls()

    def _broken(plan_id: str) -> Any:
        calls.count += 1
        raise sqlite3.OperationalError("no such table: plan_routing")

    lifecycle.pause_plan = _broken
    executor = _executor(state_db, clock, lifecycle)

    with pytest.raises(sqlite3.OperationalError):
        executor.pause("p1")

    assert calls.count == 1, "an error that is not a lock was retried"


def test_exhausted_pause_keeps_the_pause_intent(
    state_db: sqlite3.Connection, monkeypatch: Any
) -> None:
    """An exhausted pause leaves the intent on disk.

    The pause intent is written **before** the plan is stopped, because
    the process that does the stopping may be killed a moment later.
    So when the database stays busy, the intent is already there — and
    the executor must not take it away: the repair round still needs it
    to tell a pause from a crash, and the executor that failed may
    already be dead.
    """
    import pause_state
    from plan_lifecycle_service import PlanLifecycleService

    clock = FakeClock()
    plan_dir = _make_plan("p1", QUEUED)
    lifecycle = PlanLifecycleService()
    calls = _counting(lifecycle, "pause_plan")
    executor = _executor(state_db, clock, lifecycle)

    locked = _locked_queue_write(monkeypatch, times=99)

    result = executor.pause("p1")

    assert result.outcome == "conflict", result.outcome
    assert result.detail == "database_locked", result.detail
    assert calls.count == 4, "the lock was waited out more or less often than the retryer allows"
    assert locked.count == 4, "the lock did not arrive inside the lifecycle"

    intent = pause_state.read_pause_intent(plan_dir)
    assert intent is not None, "the pause intent was taken away"
    assert intent.get("reason") == "user_pause", intent
