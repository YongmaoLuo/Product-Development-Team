"""Unit tests: the executor picks the victim by the plan's creation time.

Background
----------
An explicit ``start`` may interrupt a running plan — but only when the
capacity is full and only when the rules allow it.  A plan that has been
working for at least ``M`` seconds may be interrupted, and among those the
**youngest** one is chosen: the plan that was created most recently, and
therefore the one least likely to be wrapping up.

The executor used to answer that question itself.  It asked the ledger
who may be interrupted, and then read the **slots** again to put the
running set back together.  The set it rebuilt carried no creation time,
so the policy could only fall back to the slot's timestamp — and the
slot's timestamp is rewritten by every reconciliation.  The victim was
therefore decided by the bookkeeping rather than by the rule.

The ledger's candidates carry the creation time now, so the executor
hands them to the policy as they are: one candidate set, read once, and
the policy's own rule decides which of them is the victim.

What this file holds down
-------------------------
1. **The creation time decides.**  The plan that was created last is the
   victim — even when the other plan took its slot more recently.
2. **Nobody eligible means no victim.**  A plan inside its protected
   window is not interrupted.
3. **The slots are read once.**  The candidate set comes from the
   ledger; the executor does not read the slot table again to rebuild it.

The plans are made to work through the **verification side**: a running
verification round is a plan holding compute, and it needs no
subprocess — so the cases stay inside the unit layer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a plan is protected for — the threshold the cases use.
MIN_RUNTIME = 600.0


class _Clock:
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


def _utc_iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class _CountingStore(SqliteSchedulerStore):
    """The store, counting how often the slot table is read.

    The candidate set is the ledger's, and the ledger reads the slots
    once.  A second read — the executor rebuilding the running set out
    of the slot rows — would be a second opinion about who is working,
    and the two opinions drift.
    """

    def __init__(self) -> None:
        super().__init__()
        self.list_slots_calls = 0

    def list_slots(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Return the held slots, and remember that they were read."""
        self.list_slots_calls += 1
        return super().list_slots(conn)


class _Lifecycle:
    """A lifecycle that is never asked anything.

    Choosing a victim does not stop anybody: it only decides *which*
    plan may be interrupted, and stopping it is the executor's other
    method.  The stub keeps the case inside the unit layer, and it says
    so loudly if the code under test ever starts asking.
    """

    def stop_plan(self, plan_id: str, requeue: bool = False) -> None:
        """Refuse: nothing in this case stops a plan."""
        raise AssertionError(
            "the lifecycle must not be asked to stop {0!r}".format(plan_id)
        )


class _World:
    """The case's world: a database, a ledger, an executor and a clock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ledger: CapacityLedger,
        executor: CommandExecutor,
        store: _CountingStore,
        clock: _Clock,
    ) -> None:
        self.conn = conn
        self.ledger = ledger
        self.executor = executor
        self.store = store
        self.clock = clock

    def working(self, plan_id: str, started_at: Optional[float] = None) -> None:
        """Make the plan hold compute, and say when its work began.

        The plan is on the verification side — a round that is running —
        so the case needs no subprocess to make the plan work.  The
        execution row carries ``started_at``, the moment the plan's own
        work began, and that is the timestamp the eligibility rule reads.
        """
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "verify_first_pass", _utc_iso(self.clock())),
        )
        self.conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "stop_reason, task_progress, next_run_at, card_state, flags, "
            "exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (
                plan_id,
                None if started_at is None else _utc_iso(started_at),
                _utc_iso(self.clock()),
            ),
        )
        self.conn.execute(
            "INSERT INTO plan_verification (plan_id, verification_status, "
            "round, max_rounds, updated_at) VALUES (?, ?, 0, 3, ?)",
            (plan_id, "running", _utc_iso(self.clock())),
        )
        self.conn.commit()

    def slot(self, plan_id: str, acquired_at: float) -> None:
        """Give the plan a slot, taken at ``acquired_at``."""
        self.store.upsert_slot(self.conn, plan_id, now=_utc_iso(acquired_at))


@pytest.fixture()
def world(tmp_path: Path):
    """An executor over an in-memory database that carries the real schema."""
    from state_machine.db.schema import migrate

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    clock = _Clock()
    store = _CountingStore()
    facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    executor = CommandExecutor(
        store,
        ledger,
        _Lifecycle(),
        facts=facts,
        conn=conn,
        capacity=1,
        min_runtime_seconds=MIN_RUNTIME,
        clock=clock,
    )
    try:
        yield _World(conn, ledger, executor, store, clock)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The creation time decides
# ---------------------------------------------------------------------------


def test_select_victim_passes_created_at_through_from_ledger(world: _World) -> None:
    """The victim is the plan that was created last.

    Two plans are working and both are old enough to be interrupted, but
    they were created on different days: ``alpha`` was made in January,
    ``beta`` was made today.  The slot timestamps point the other way —
    ``alpha`` took its slot more recently than ``beta`` — because a slot
    is taken when a plan is adopted, and the bookkeeping says nothing
    about how long the work has been going on.

    The **youngest** plan is the one that may be interrupted: the plan
    that was made last is the one least likely to be wrapping up.  The
    creation time has to reach the policy for that to be true, and it
    reaches it through the ledger's candidates — the executor does not
    rebuild the candidate set out of the slot rows, because those rows
    carry no creation time at all.
    """
    world.working("20260101-alpha", started_at=BASE_EPOCH - 3600)
    world.slot("20260101-alpha", BASE_EPOCH - 700)
    world.working("20261029-beta", started_at=BASE_EPOCH - 3600)
    world.slot("20261029-beta", BASE_EPOCH - 3000)

    candidates = world.ledger.evictable(world.clock(), MIN_RUNTIME)
    assert {entry["plan_id"] for entry in candidates} == {
        "20260101-alpha",
        "20261029-beta",
    }, "both plans are old enough to be interrupted: {0!r}".format(candidates)
    created = {entry["plan_id"]: entry["created_at"] for entry in candidates}
    assert created["20261029-beta"] > created["20260101-alpha"], (
        "the two plans were not created in the order the case needs: "
        "{0!r}".format(created)
    )

    world.store.list_slots_calls = 0
    victim = world.executor._select_victim(MIN_RUNTIME)

    assert victim == "20261029-beta", (
        "the victim was not the plan that was created last: {0!r}".format(victim)
    )
    assert world.store.list_slots_calls == 1, (
        "the slots were read {0} times: the candidate set comes from the "
        "ledger and is not rebuilt".format(world.store.list_slots_calls)
    )


# ---------------------------------------------------------------------------
# 2. Nobody eligible means no victim
# ---------------------------------------------------------------------------


def test_select_victim_none_when_no_candidate(world: _World) -> None:
    """Two plans inside their protected window: nobody may be interrupted.

    Both plans took their slots a moment ago, so both are still inside
    the protected window of ``M`` seconds.  There is no victim to
    choose, and the answer is ``None`` — which is what tells the caller
    that the start has to wait rather than interrupt somebody's work.
    """
    world.working("20261029-gamma", started_at=BASE_EPOCH - 60)
    world.slot("20261029-gamma", BASE_EPOCH - 60)
    world.working("20261029-delta", started_at=BASE_EPOCH - 120)
    world.slot("20261029-delta", BASE_EPOCH - 120)

    assert world.executor._select_victim(MIN_RUNTIME) is None, (
        "a plan inside its protected window must not be interrupted"
    )
