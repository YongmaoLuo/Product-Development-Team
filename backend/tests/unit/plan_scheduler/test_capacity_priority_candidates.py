"""Unit tests: the eviction candidate carries the slot's priority.

Background
----------
The scheduler interrupts a running plan when a slot has to be freed, and
it has to pick the right one.  The rule it applies is **two stages**:
the priority decides — the plan that stands **last in the line** goes
first — and the runtime decides what is left among plans of the same
priority.

The candidates that rule reads are the ledger's: ``evictable()``
answers the plans that may be interrupted, and each answer carries what
the policy needs to choose among them.  The priority used to be one of
the things that answer did **not** carry, so the rule could not be
applied: the candidate set was the ledger's, but the priority the
policy compares was missing from it, and the choice fell back to the
runtime alone.

These tests pin the missing half:

1. **The candidate carries the priority the slot holds.**  A slot
   written with a priority of ``-10`` gives a candidate of ``-10``.
2. **A slot that names no priority gives the ordinary weight.**  A row
   written before the column existed, and a row whose priority is
   ``NULL``, both read as ``0``.
3. **A priority the ledger cannot read is ``0``.**  A boolean is not a
   number, a string is not a number, and a number that is not whole is
   not a priority — none of them may lift a plan to the front of the
   order.
4. **The rest of the answer does not move.**  The elapsed time, the
   slot's timestamp and the order of the candidates are what they were.

The cases assert **the candidate the policy is given** — the priority
on it — and not how the ledger got there.
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
BASE_EPOCH = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a plan is protected for — the threshold the cases use.
MIN_RUNTIME = 600.0


class _Clock:
    """A clock the case moves by hand."""

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
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


class _World:
    """The case's world: a database, a ledger and a clock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ledger: CapacityLedger,
        clock: _Clock,
        facts: PlanFactsReader,
    ) -> None:
        self.conn = conn
        self.ledger = ledger
        self.clock = clock
        self.facts = facts
        self.store = SqliteSchedulerStore()

    def working(self, plan_id: str, started_at: Optional[str] = None) -> None:
        """Make the plan hold compute, and say when its work began.

        The plan is on the verification side — a round that is running —
        so the case needs no subprocess to make the plan work.
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

    def slot(
        self,
        plan_id: str,
        acquired_at: float,
        priority: int = 0,
    ) -> None:
        """Give the plan a slot, taken at ``acquired_at``, at ``priority``."""
        self.store.upsert_slot(
            self.conn, plan_id, now=_utc_iso(acquired_at), priority=priority
        )


class _RawSlotStore:
    """A store whose slot rows are handed over as they are.

    The real store answers ``COALESCE(priority, 0)``, so a priority the
    ledger cannot read never reaches the ledger through the database.
    This store gives the rows over untouched, so the ledger's **own**
    reading of a priority — the one that decides what a candidate
    carries — can be exercised.
    """

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def list_slots(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Return the slot rows, whatever they hold."""
        return [dict(row) for row in self._rows]


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
        yield _World(conn, ledger, clock, facts)
    finally:
        conn.close()


def _only(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the one candidate, and say what came back if there is not one."""
    assert len(candidates) == 1, (
        "expected exactly one candidate, got {0!r}".format(candidates)
    )
    return candidates[0]


def _ledger_over(world: _World, rows: List[Dict[str, Any]]) -> CapacityLedger:
    """A ledger over a store whose slot rows are given outright."""
    return CapacityLedger(_RawSlotStore(rows), world.facts, world.clock)


# ---------------------------------------------------------------------------
# 1. The candidate carries the priority the slot holds
# ---------------------------------------------------------------------------


def test_candidate_carries_the_slot_priority(world: _World) -> None:
    """The candidate carries the priority the slot was written with.

    ``bug`` was pushed back by the caller (``priority = -10``) and
    ``feat`` stands later in the line (``priority = 0``).  The rule
    that picks the victim compares those numbers, so the candidates
    have to carry them — without them the choice falls back to the
    runtime alone, and the plan the caller put first gets squeezed out.
    """
    world.working("bug", started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot("bug", BASE_EPOCH - 3600, priority=-10)
    world.working("feat", started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot("feat", BASE_EPOCH - 3600, priority=0)

    candidates = world.ledger.evictable(world.clock(), 0)

    priorities = {
        candidate["plan_id"]: candidate.get("priority") for candidate in candidates
    }
    assert priorities == {"bug": -10, "feat": 0}, (
        "the candidates do not carry the priority of their slots: "
        "{0!r}".format(candidates)
    )


# ---------------------------------------------------------------------------
# 2. A slot that names no priority gives the ordinary weight
# ---------------------------------------------------------------------------


def test_candidate_priority_defaults_to_zero(world: _World) -> None:
    """A slot written without a priority gives a candidate of ``0``.

    The column was added after the slot table was first shipped, so a
    plan's slot may name no priority at all.  The ordinary weight is
    ``0`` — the weight every plan carries when nothing says otherwise
    — and a missing priority must never lift a plan to the front of the
    order.
    """
    world.working("20261101-default", started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot("20261101-default", BASE_EPOCH - 3600)

    candidate = _only(world.ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate.get("priority") == 0, (
        "a slot that names no priority did not give the ordinary weight: "
        "{0!r}".format(candidate)
    )


def test_a_null_priority_gives_the_ordinary_weight(world: _World) -> None:
    """A row whose priority is ``NULL`` gives a candidate of ``0``.

    ``NULL`` does not compare, and the eviction rule compares numbers.
    A row that holds no priority therefore weighs ``0`` — it is neither
    lifted to the front of the order nor sunk below a plan that stands
    there.
    """
    world.working("20261101-null", started_at=_local_iso(BASE_EPOCH - 3600))
    ledger = _ledger_over(
        world,
        [
            {
                "plan_id": "20261101-null",
                "acquired_at": _utc_iso(BASE_EPOCH - 3600),
                "updated_at": _utc_iso(BASE_EPOCH - 3600),
                "priority": None,
            }
        ],
    )

    candidate = _only(ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate.get("priority") == 0, (
        "a NULL priority did not give the ordinary weight: {0!r}".format(candidate)
    )


# ---------------------------------------------------------------------------
# 3. A priority the ledger cannot read is zero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "-10", "10", True, False, 1.5],
    ids=["null", "negative-string", "string", "true", "false", "not-whole"],
)
def test_a_priority_that_cannot_be_read_is_zero(world: _World, raw: object) -> None:
    """A priority the ledger cannot read is the ordinary weight, ``0``.

    A boolean is not a number, a string is not a number, and a number
    that is not whole is not a priority.  All of them read as ``0``, and
    ``0`` must never put the plan at the front of the order: the
    runtime decides, exactly as it does for a plan whose priority
    really is ``0``.
    """
    world.working("20261101-unreadable", started_at=_local_iso(BASE_EPOCH - 3600))
    ledger = _ledger_over(
        world,
        [
            {
                "plan_id": "20261101-unreadable",
                "acquired_at": _utc_iso(BASE_EPOCH - 3600),
                "updated_at": _utc_iso(BASE_EPOCH - 3600),
                "priority": raw,
            }
        ],
    )

    candidate = _only(ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate.get("priority") == 0, (
        "a priority that cannot be read did not give the ordinary weight: "
        "{0!r}".format(candidate)
    )


# ---------------------------------------------------------------------------
# 4. The rest of the answer does not move
# ---------------------------------------------------------------------------


def test_the_candidate_still_carries_the_rest_of_the_answer(
    world: _World,
) -> None:
    """Adding the priority changes nothing else about the candidate.

    The rule reads four numbers off a candidate — the slot's timestamp,
    the plan's creation time, the runtime and now the priority — and
    the other three are not moved by the fourth.
    """
    plan_id = "20261101-whole"
    world.working(plan_id, started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot(plan_id, BASE_EPOCH - 3600, priority=-10)

    candidate = _only(world.ledger.evictable(world.clock(), MIN_RUNTIME))

    assert candidate["plan_id"] == plan_id
    assert candidate["acquired_at"] == BASE_EPOCH - 3600
    assert candidate["elapsed"] == 3600.0
    assert candidate["created_at"] is not None
    assert candidate["priority"] == -10


def test_the_order_of_the_candidates_does_not_move(world: _World) -> None:
    """The candidates come back in the ledger's own order.

    The order is the runtime start first and the plan id second, and
    the priority has no say in it: the ledger hands the candidates over
    in a fixed order and the *policy* chooses among them.
    """
    world.working("20261101-b", started_at=_local_iso(BASE_EPOCH - 7200))
    world.slot("20261101-b", BASE_EPOCH, priority=10)
    world.working("20261101-a", started_at=_local_iso(BASE_EPOCH - 3600))
    world.slot("20261101-a", BASE_EPOCH, priority=-10)

    candidates = world.ledger.evictable(world.clock(), MIN_RUNTIME)

    assert [candidate["plan_id"] for candidate in candidates] == [
        "20261101-b",
        "20261101-a",
    ], (
        "the order of the candidates moved: {0!r}".format(candidates)
    )
