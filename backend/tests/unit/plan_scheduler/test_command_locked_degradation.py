"""Unit tests: a queue write that hits the write lock degrades, it does not fail.

Background
----------
A resume and a start are **two transactions**.  The first one moves the
plan's phase — ``stopped → queued`` — and it belongs to the lifecycle,
which opens its own connection and commits it.  The second one writes
the plan's row in the scheduler queue, and it belongs to the store.

The second one can lose the race for SQLite's write lock.  The lock is
held for the whole write transaction, the second writer waits for it —
but the wait has a limit, and when the limit is reached SQLite says
``database is locked`` and **the write did not happen**.

That used to be a 500 for the whole action, even though the plan had
already come back: the phase says ``queued`` and the plan is waiting,
and only the queue row is missing.  On 2026-11-23 a batch resume of
twelve plans answered 500 eight times while all nine plans ended up
``queued`` — the action had been carried out and the answer said it
had not.

What this file holds down
-------------------------
1. **A locked queue write degrades.**  The resume answers ``applied``
   with the detail ``queued_pending_reconcile``: the plan is waiting,
   the queue row is still missing, and the reconciler will put it
   there.
2. **The start degrades the same way** — it is the same second
   transaction and the same fault.
3. **Only a lock degrades.**  Another ``OperationalError`` — a disk
   that is full, a database that is corrupt — is a fault of the
   database, and it stays a fault: it is raised, and the engine
   answers 500 as it does for any other fault.

The doubles are narrow on purpose
----------------------------------
The phase and the queue row are two facts in **one** SQLite file, and
the executor reads both of them there.  So the database is real, the
store is real, and only the thing that has to go wrong goes wrong:
the **store's** ``promote`` raises the fault, exactly as SQLite would
raise it.  The lifecycle is a stand-in that moves the phase and
commits it — the phase CAS is its own transaction, and that is the
whole point of the case.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import APPLIED, CommandExecutor
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The detail a degraded answer carries: the plan is queued, the queue
#: row is still missing, the reconciler will put it there.
PENDING_RECONCILE = "queued_pending_reconcile"


def _utc_iso(epoch: float = BASE_EPOCH) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class _Clock:
    """A clock the case reads; it never moves."""

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now


class _LifecycleResult:
    """What a lifecycle answers with.

    The executor reads ``ok`` and ``detail`` off the answer; the rest is
    here so the stand-in has the same shape as the real result.
    """

    def __init__(self, ok: bool = True, detail: str = "requeue") -> None:
        self.ok = ok
        self.detail = detail


class _LifecycleThatQueues:
    """The lifecycle, moving the phase to ``queued`` in its own transaction.

    The phase belongs to the state machine, and the lifecycle writes it
    on a connection of its own — so the phase is committed **before** the
    executor writes the queue row.  That is the order the case is about:
    the phase is already ``queued`` when the queue write hits the lock.

    A lifecycle that answers ``ok`` and moves **nothing** is the other
    half of the story: the phase stays where it was, and the lock is
    then not the reason the plan did not come back.
    """

    def __init__(self, conn: sqlite3.Connection, queues: bool = True) -> None:
        self._conn = conn
        self.queues = queues
        self.calls: List[str] = []

    def requeue_plan(self, plan_id: str) -> _LifecycleResult:
        """Move the plan to ``queued``, commit it, and answer."""
        self.calls.append(plan_id)
        if not self.queues:
            return _LifecycleResult(ok=True, detail="requeue")
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            ("queued", plan_id),
        )
        # The phase CAS is its own transaction: it is committed before
        # the executor writes anything else.
        self._conn.commit()
        return _LifecycleResult(ok=True, detail="requeue")


class _World:
    """The case's world: a database, a ledger, an executor and a clock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ledger: CapacityLedger,
        store: SqliteSchedulerStore,
        clock: _Clock,
        facts: PlanFactsReader,
    ) -> None:
        self.conn = conn
        self.ledger = ledger
        self.store = store
        self.clock = clock
        self.facts = facts
        self.lifecycle = _LifecycleThatQueues(conn)
        self.executor = CommandExecutor(
            self.store,
            self.ledger,
            self.lifecycle,
            facts=self.facts,
            conn=self.conn,
            capacity=1,
            clock=self.clock,
        )

    def plan(self, plan_id: str, phase: str) -> None:
        """Register a plan that is in the phase the case needs."""
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, phase, _utc_iso(self.clock())),
        )
        self.conn.commit()

    def phase_of(self, plan_id: str) -> Optional[str]:
        """Return the phase the plan carries in the database."""
        row = self.conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def queue(self) -> List[str]:
        """Return the plan ids the scheduler queue holds."""
        return [str(row["plan_id"]) for row in self.store.list_queue(self.conn)]

    def commands(self, plan_id: str) -> List[Any]:
        """Return the command rows written down for the plan."""
        return list(
            self.conn.execute(
                "SELECT action, outcome, detail FROM scheduler_command "
                "WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        )


@pytest.fixture()
def world(tmp_path: Path):
    """An executor over an in-memory database that carries the real schema."""
    from state_machine.db.schema import migrate

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    clock = _Clock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    try:
        yield _World(conn, ledger, store, clock, facts)
    finally:
        conn.close()


def _raise_locked(conn: sqlite3.Connection, plan_id: str, now: Any = None) -> Any:
    """Fail the way SQLite fails when the write lock cannot be taken."""
    raise sqlite3.OperationalError("database is locked")


def _raise_disk_error(
    conn: sqlite3.Connection, plan_id: str, now: Any = None
) -> Any:
    """Fail the way SQLite fails when the disk is not there."""
    raise sqlite3.OperationalError("disk I/O error")


# ---------------------------------------------------------------------------
# 1. A locked queue write degrades
# ---------------------------------------------------------------------------


def test_resume_returns_applied_when_queue_write_locked(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume whose queue write hits the lock is applied, not a fault.

    The phase was moved in the lifecycle's own transaction and committed
    before the queue write was even attempted: the plan is **waiting**.
    Only the queue row is missing, and the reconciler is what puts
    queue rows right — so the action did what it was asked to do and the
    answer says so.

    The answer names what is still missing: ``queued_pending_reconcile``.
    It is not ``resumed``, because the queue row really is not there
    yet, and it is not a conflict, because the plan is waiting.

    The phase is checked in the database rather than in the executor's
    memory: the degradation is only allowed to speak for a plan that
    really is ``queued``.
    """
    world.plan("20261123-locked", "stopped")
    monkeypatch.setattr(world.store, "promote", _raise_locked)

    result = world.executor.resume("20261123-locked")

    assert result.outcome == APPLIED, (
        "a resume that hit the write lock was not applied: {0!r}".format(result)
    )
    assert result.detail == PENDING_RECONCILE, (
        "the degraded answer does not name what is still missing: "
        "{0!r}".format(result)
    )
    assert world.phase_of("20261123-locked") == "queued", (
        "the phase did not move: {0!r}".format(world.phase_of("20261123-locked"))
    )

    # The queue row really is missing — the write was rolled back — and
    # the reconciler is what puts it there.
    assert world.queue() == [], (
        "the queue row survived the rolled-back write: {0!r}".format(
            world.queue()
        )
    )
    world.store.enqueue(world.conn, "20261123-locked")
    assert world.queue() == ["20261123-locked"], (
        "the reconciler could not put the row back: {0!r}".format(world.queue())
    )

    # The command is written down: the action was carried out, and the
    # log says what it answered.
    assert world.commands("20261123-locked") == [
        ("resume", APPLIED, PENDING_RECONCILE)
    ], "the command was not written down: {0!r}".format(
        world.commands("20261123-locked")
    )


# ---------------------------------------------------------------------------
# 2. The start degrades the same way
# ---------------------------------------------------------------------------


def test_start_degrades_the_same_way(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start whose queue write hits the lock degrades exactly as a resume does.

    It is the same second transaction — the one that writes the queue row
    — and the same fault, so it gets the same answer: the plan is
    waiting and the queue row is still missing.  The start does not go
    on to ask about the capacity: the plan is at the head of the queue
    either way, and the reconciler will put the row there.
    """
    world.plan("20261123-start", "stopped")
    monkeypatch.setattr(world.store, "promote", _raise_locked)

    result = world.executor.start("20261123-start")

    assert result.outcome == APPLIED, (
        "a start that hit the write lock was not applied: {0!r}".format(result)
    )
    assert result.detail == PENDING_RECONCILE, (
        "the degraded answer does not name what is still missing: "
        "{0!r}".format(result)
    )
    assert world.phase_of("20261123-start") == "queued", (
        "the phase did not move: {0!r}".format(world.phase_of("20261123-start"))
    )
    assert world.queue() == [], (
        "the queue row survived the rolled-back write: {0!r}".format(
            world.queue()
        )
    )


# ---------------------------------------------------------------------------
# 3. Only a lock degrades
# ---------------------------------------------------------------------------


def test_non_lock_errors_still_raise(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another ``OperationalError`` is a fault, and it stays a fault.

    ``disk I/O error`` is not a lock: it says the database itself is
    not in a state where the write could happen, and it is not a thing
    that can be waited out.  Degrading it would tell the operator the
    plan is queued when nothing about the database can be believed.

    So it is raised, exactly as it was before — and the engine answers
    500 for it, as it does for any other fault.
    """
    world.plan("20261123-broken", "stopped")
    monkeypatch.setattr(world.store, "promote", _raise_disk_error)

    with pytest.raises(sqlite3.OperationalError) as caught:
        world.executor.resume("20261123-broken")

    assert "disk I/O error" in str(caught.value), (
        "the fault that was raised is not the one that happened: "
        "{0!r}".format(caught.value)
    )


# ---------------------------------------------------------------------------
# 4. The degradation speaks only for a plan that is waiting
# ---------------------------------------------------------------------------


def test_a_lock_on_a_plan_that_is_not_queued_still_raises(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that did not come back is not saved by a lock.

    The lock is a fault of the **write**, not of the plan.  When the
    phase does not say the plan is waiting, the plan did not come back
    — and the locked write is not the reason.  So the fault is raised,
    exactly as it was before: answering ``applied`` for a plan that is
    not waiting would be the very defect this file is about, only
    dressed up as a degradation.
    """
    world.plan("20261123-not-queued", "stopped")
    world.lifecycle.queues = False
    monkeypatch.setattr(world.store, "promote", _raise_locked)

    with pytest.raises(sqlite3.OperationalError):
        world.executor.resume("20261123-not-queued")

    assert world.phase_of("20261123-not-queued") == "stopped", (
        "the phase moved although the lifecycle moved nothing: {0!r}".format(
            world.phase_of("20261123-not-queued")
        )
    )
