"""Unit tests: the executor picks the victim by the plan's place in the line.

Background
----------
An explicit ``start`` may interrupt a running plan — but only when the
capacity is full and only when the rules allow it.  A plan that has been
working for at least ``M`` seconds may be interrupted, and among those
the choice is made in **two stages**: the plan that stands **last in the
line** — the largest priority number — is interrupted first, and among
the plans of one priority the one that has run the **shortest** goes
first.

The executor used to answer that question itself.  It asked the ledger
who may be interrupted, and then read the **slots** again to put the
running set back together.  The set it rebuilt carried no priority, so
the policy could only fall back to the slot's timestamp — and the
slot's timestamp is rewritten by every reconciliation.  The victim was
therefore decided by the bookkeeping rather than by the rule.

The ledger's candidates carry the priority now, so the executor hands
them to the policy as they are: one candidate set, read once, and the
policy's own rule decides which of them is the victim.

What this file holds down
-------------------------
1. **The priority decides.**  The plan that stands last in the line is
   the victim — even when the other plan took its slot more recently,
   and even when the other plan's name sorts first.
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
from plan_scheduler.config import DEFAULT_MIN_RUNTIME_SECONDS
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
        store: _CountingStore,
        clock: _Clock,
        facts: PlanFactsReader,
        min_runtime_seconds: float = MIN_RUNTIME,
    ) -> None:
        self.conn = conn
        self.ledger = ledger
        self.store = store
        self.clock = clock
        self.facts = facts
        self.min_runtime_seconds = min_runtime_seconds
        self.executor = self.executor_factory(min_runtime_seconds)

    def executor_factory(
        self, min_runtime_seconds: float = DEFAULT_MIN_RUNTIME_SECONDS
    ) -> CommandExecutor:
        """Return an executor over this world, running with this threshold.

        ``M`` — the runtime a plan is protected for — is a knob, and a
        case that wants another one asks for it here instead of building
        a second world.  The default is the threshold the scheduler runs
        with when nobody says otherwise.
        """
        return CommandExecutor(
            self.store,
            self.ledger,
            _Lifecycle(),
            facts=self.facts,
            conn=self.conn,
            capacity=1,
            min_runtime_seconds=min_runtime_seconds,
            clock=self.clock,
        )

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

    def slot(self, plan_id: str, acquired_at: float, priority: int = 0) -> None:
        """Give the plan a slot, taken at ``acquired_at``, at ``priority``.

        The priority is the plan's place in the line: the larger the
        number, the later the plan stands, and the later it stands the
        more the scheduler can afford to interrupt it.  A plan whose
        priority nobody mentions runs at the ordinary weight, ``0``.
        """
        self.store.upsert_slot(
            self.conn, plan_id, now=_utc_iso(acquired_at), priority=priority
        )


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
    try:
        yield _World(
            conn, ledger, store, clock, facts, min_runtime_seconds=MIN_RUNTIME
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The priority decides
# ---------------------------------------------------------------------------


def test_select_victim_passes_priority_through_from_ledger(world: _World) -> None:
    """The victim is the plan that stands last in the line.

    Two plans are working and both are old enough to be interrupted,
    and they have been running for exactly as long as each other — so
    the runtime cannot tell them apart.  What tells them apart is their
    place in the line: ``beta`` was pushed back by the caller
    (``priority = -10``) and keeps its slot, while ``alpha`` stands
    last (``priority = 0``) and is the plan the scheduler can most
    afford to interrupt.

    Two other answers are ruled out by the data, so that the priority
    is the only thing left that can decide.  The **slot timestamps**
    point the other way — ``beta`` took its slot more recently than
    ``alpha``, so a runtime measured from the slots would squeeze
    ``beta`` — and so do the **plan ids**: ``20260101-beta`` sorts
    before ``20261029-alpha``, so a tie broken by name would pick
    ``beta`` as well.  Only the priority makes ``alpha`` the victim.

    The priority has to reach the policy for that to be true, and it
    reaches it through the ledger's candidates — the executor does not
    rebuild the candidate set out of the slot rows.
    """
    world.working("20261029-alpha", started_at=BASE_EPOCH - 3600)
    world.slot("20261029-alpha", BASE_EPOCH - 3000, priority=0)
    world.working("20260101-beta", started_at=BASE_EPOCH - 3600)
    world.slot("20260101-beta", BASE_EPOCH - 700, priority=-10)

    candidates = world.ledger.evictable(world.clock(), MIN_RUNTIME)
    assert {entry["plan_id"] for entry in candidates} == {
        "20261029-alpha",
        "20260101-beta",
    }, "both plans are old enough to be interrupted: {0!r}".format(candidates)
    priorities = {entry["plan_id"]: entry["priority"] for entry in candidates}
    assert priorities == {"20261029-alpha": 0, "20260101-beta": -10}, (
        "the candidates do not carry the priority of their slots: "
        "{0!r}".format(priorities)
    )
    elapsed = {entry["plan_id"]: entry["elapsed"] for entry in candidates}
    assert elapsed["20261029-alpha"] == elapsed["20260101-beta"], (
        "the two plans have to have run for as long as each other, or the "
        "runtime decides instead of the priority: {0!r}".format(elapsed)
    )

    world.store.list_slots_calls = 0
    victim = world.executor._select_victim(MIN_RUNTIME)

    assert victim == "20261029-alpha", (
        "the victim was not the plan that stands last in the line: "
        "{0!r}".format(victim)
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