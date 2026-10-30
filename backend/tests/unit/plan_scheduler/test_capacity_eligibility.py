"""Unit tests: the eviction candidates — who may be interrupted.

Background
----------
The scheduler interrupts a running plan when a slot has to be freed, and
it may only interrupt a plan that has been running for at least ``M``
seconds.  The moment that runtime was measured from is the slot's own
timestamp — the moment the plan took its concurrency slot — and that
timestamp is rewritten.

The engine re-stamps the slots of the plans it adopts: "a plan that is
working gets a slot timed from *now*".  So after every restart every
plan looks as if it had just taken its slot, and for the next ``M``
seconds nobody may be interrupted at all — the operator asks for a
plan to start and is told ``no_eligible_victim`` while the slot sits
full of work that has been going on for hours.

The moment that does not move is the plan's own: ``plan_execution``
says when the plan's work began, and no reconciliation rewrites it.
So the eligibility is measured from there, and the slot's timestamp is
what the ledger falls back to when the plan's own start is unknown.

What this file holds down
-------------------------
1. **The plan's own start decides.**  A slot that says "just taken"
   does not make a plan that has been working for an hour young again.
2. **The candidate carries what the policy needs.**  The plan id, the
   slot's timestamp, the plan's creation time and the runtime.
3. **The slot time is the fallback.**  A plan whose own start is
   missing or unreadable is judged by its slot, exactly as before.
4. **The boundaries hold.**  No slot row, no working plan, no creation
   time: each is answered, none of them is guessed at.

The cases assert **the answer the policy is given** — the candidate
and the numbers on it — and not how the ledger got there.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from plan_scheduler.capacity import CapacityLedger
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

    It is callable, because that is what the ledger and the facts
    reader ask for: ``clock()`` is "what time is it now".
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


def _local_iso(epoch: float) -> str:
    """Return the moment as the execution path writes it.

    ``backend.routes.execution`` stamps ``started_at`` with
    ``datetime.now().isoformat()`` — the local time, and no zone with
    it.  The cases write the stamp the same way, so they read the
    stamp the production database really holds.
    """
    return datetime.fromtimestamp(epoch).isoformat()


def _local_midnight(year: int, month: int, day: int) -> float:
    """Local midnight of a day, in epoch seconds.

    A plan id that begins with eight digits names the day the plan was
    made, and the facts reader reads that day at local midnight — the
    same rule, so the case holds on every host.
    """
    return datetime(year, month, day).timestamp()


class _World:
    """The case's world: a database, a ledger and a clock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ledger: CapacityLedger,
        clock: _Clock,
        root: Path,
    ) -> None:
        self.conn = conn
        self.ledger = ledger
        self.clock = clock
        self.root = root
        self.store = SqliteSchedulerStore()

    def working(self, plan_id: str, started_at: Optional[str] = None) -> None:
        """Make the plan hold compute, and say when its work began.

        The plan is on the verification side — a round that is running
        — so the case needs no subprocess to make the plan work.  The
        execution row carries ``started_at``, the moment the plan's own
        work began, and that is the timestamp the eligibility rule
        reads.
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
            (plan_id, started_at, _utc_iso(self.clock())),
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

    def restamp_slot(self, plan_id: str, acquired_at: float) -> None:
        """Re-stamp the plan's slot, the way the engine adopts a plan.

        The engine's recovery gives every working plan a slot timed
        from *now*, so the row no longer says when the plan took its
        slot.  This is that write.
        """
        self.conn.execute(
            "UPDATE scheduler_slot SET acquired_at = ?, updated_at = ? "
            "WHERE plan_id = ?",
            (_utc_iso(acquired_at), _utc_iso(acquired_at), plan_id),
        )
        self.conn.commit()


@pytest.fixture()
def world(tmp_path: Path):
    """A ledger over an in-memory database that carries the real schema."""
    from state_machine.db.schema import migrate

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    clock = _Clock()
    facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
    ledger = CapacityLedger(SqliteSchedulerStore(), facts, clock)
    try:
        yield _World(conn, ledger, clock, tmp_path)
    finally:
        conn.close()


def _only(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the one candidate, and say what came back if there is not one."""
    assert len(candidates) == 1, (
        "expected exactly one candidate, got {0!r}".format(candidates)
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# 1. The plan's own start decides
# ---------------------------------------------------------------------------


def test_slot_reconcile_does_not_reset_eligibility(world: _World) -> None:
    """A re-stamped slot does not make a working plan young again.

    The engine's recovery re-stamps the slot of every plan it adopts,
    so after a restart the slot says the plan took its slot *now* — and
    the plan has been working for an hour.  Measured from the slot, the
    plan is inside its protected window and nobody may be interrupted;
    measured from the plan's own start, it is an hour old and may be.

    The rule is about the work, not about the bookkeeping: the slot is
    a record the scheduler keeps, and the scheduler is free to rewrite
    it.
    """
    plan_id = "20261029-long"
    world.working(plan_id, started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot(plan_id, BASE_EPOCH - 3600)

    world.ledger.reconcile()
    world.restamp_slot(plan_id, BASE_EPOCH)

    candidates = world.ledger.evictable(world.clock(), MIN_RUNTIME)

    assert [candidate["plan_id"] for candidate in candidates] == [plan_id], (
        "the plan that has been working for an hour is not a candidate: "
        "{0!r}".format(candidates)
    )
    assert candidates[0]["elapsed"] == 3600.0, (
        "the runtime was measured from the slot instead of the plan's own "
        "start: {0!r}".format(candidates[0])
    )


def test_evictable_reads_a_start_that_carries_a_zone(world: _World) -> None:
    """A start that names its zone is read as the moment it names.

    The execution path writes a naive local stamp, but the column takes
    any ISO-8601 stamp — a UTC one with a ``Z``, or one with an offset.
    Such a stamp names an exact moment, and it is read as that moment.
    """
    world.working("20261029-utc", started_at=_utc_iso(BASE_EPOCH - 3600))
    world.slot("20261029-utc", BASE_EPOCH)

    candidate = _only(world.ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate["elapsed"] == 3600.0, (
        "the runtime was not measured from the plan's own start: "
        "{0!r}".format(candidate)
    )


# ---------------------------------------------------------------------------
# 2. The candidate carries what the policy needs
# ---------------------------------------------------------------------------


def test_evictable_entries_carry_created_at(world: _World) -> None:
    """A candidate carries the plan's creation time.

    The policy picks the **youngest** candidate, and the youngest is
    the one that was created most recently — so the candidate has to
    carry the creation time.  The plan's id names the day it was made,
    and that day at local midnight is the creation time the facts
    reader has for it.
    """
    plan_id = "20261029-old"
    world.working(plan_id, started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot(plan_id, BASE_EPOCH)

    candidate = _only(world.ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate["plan_id"] == plan_id
    assert candidate["acquired_at"] == BASE_EPOCH, (
        "the slot's own timestamp was not carried: {0!r}".format(candidate)
    )
    assert candidate["created_at"] == _local_midnight(2026, 10, 29), (
        "the plan's creation time was not carried: {0!r}".format(candidate)
    )
    assert candidate["elapsed"] == 3600.0, (
        "the runtime was not carried: {0!r}".format(candidate)
    )


def test_evictable_reports_an_unknown_creation_time_as_none(world: _World) -> None:
    """A plan the facts cannot date carries no creation time.

    An id that names no day names no creation time, and the answer is
    ``None`` — not a guess.  The policy falls back to the slot
    timestamp for such a plan.
    """
    world.working("vp-003", started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot("vp-003", BASE_EPOCH)

    candidate = _only(world.ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate["created_at"] is None, (
        "a plan with no creation time was given one: {0!r}".format(candidate)
    )


# ---------------------------------------------------------------------------
# 3. The slot time is the fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "started_at",
    [None, "not a timestamp"],
    ids=["missing", "unreadable"],
)
def test_evictable_falls_back_to_slot_time_without_started_at(
    world: _World, started_at: Optional[str]
) -> None:
    """A plan whose own start is unknown is judged by its slot.

    A row that says nothing, and a timestamp that cannot be read, are
    the same situation: the ledger cannot know when the plan's work
    began, so it answers with the moment the plan took its slot — the
    rule it always had.
    """
    plan_id = "20261029-fallback"
    world.working(plan_id, started_at=started_at)
    world.slot(plan_id, BASE_EPOCH - 3600)

    candidates = world.ledger.evictable(world.clock(), MIN_RUNTIME)

    assert [candidate["plan_id"] for candidate in candidates] == [plan_id], (
        "the plan was not judged by its slot: {0!r}".format(candidates)
    )
    assert candidates[0]["elapsed"] == 3600.0, (
        "the runtime was not measured from the slot: {0!r}".format(candidates[0])
    )


# ---------------------------------------------------------------------------
# 4. The boundaries hold
# ---------------------------------------------------------------------------


def test_evictable_skips_a_plan_without_a_slot(world: _World) -> None:
    """A working plan with no slot row is not a candidate.

    The candidates are the plans that hold a slot.  A plan that holds
    none is not holding compute as far as the scheduler's bookkeeping
    is concerned, and there is nothing to interrupt.
    """
    world.working("20261029-no-slot", started_at=_local_iso(BASE_EPOCH - 3600))

    assert world.ledger.evictable(world.clock(), MIN_RUNTIME) == []


def test_evictable_skips_a_plan_that_is_not_working(world: _World) -> None:
    """A slot row on its own does not make a plan a candidate.

    The candidate set is "working **and** old enough".  A row left
    over from a plan that has since stopped is not working, and
    interrupting it would interrupt nothing.
    """
    world.slot("20261029-stopped", BASE_EPOCH - 3600)

    assert world.ledger.evictable(world.clock(), MIN_RUNTIME) == []


def test_evictable_orders_by_the_runtime_start(world: _World) -> None:
    """The candidates come back in a fixed order.

    The order is the runtime start first and the plan id second, so two
    calls answer in the same order and a caller that walks the list is
    never at the mercy of how SQLite returned the rows.  Two plans that
    took their slot in the same second are told apart by the moment
    their own work began.
    """
    world.working("20261029-b", started_at=_local_iso(BASE_EPOCH - 7200))
    world.slot("20261029-b", BASE_EPOCH)
    world.working("20261029-a", started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot("20261029-a", BASE_EPOCH)

    first = world.ledger.evictable(world.clock(), MIN_RUNTIME)
    second = world.ledger.evictable(world.clock(), MIN_RUNTIME)

    assert [candidate["plan_id"] for candidate in first] == [
        "20261029-b",
        "20261029-a",
    ], (
        "the candidates are not in the order of their runtime start: "
        "{0!r}".format(first)
    )
    assert first == second, "two calls answered in different orders"
