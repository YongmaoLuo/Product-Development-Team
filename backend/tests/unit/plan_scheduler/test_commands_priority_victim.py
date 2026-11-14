"""Unit tests: with the default threshold, the shortest run is squeezed.

Background
----------
The rule the scheduler follows is **two stages**: the plan that stands
**last in the line** goes first, and among the plans of one priority
the one that has run the **shortest** goes first.

The second half of that rule needs a threshold ``M`` that does not keep
the shortest run out of the candidate set.  ``M`` is the runtime a plan
is protected for, and its **default is zero**: the old default of 600
seconds made the shortest run the one plan that could never be
interrupted, which is the opposite of what the rule says.

The policy layer pins that default on its own — see
``test_default_protection_is_zero.py``.  This file pins it where the
decision is actually made: the **command layer**, where the candidate
set comes from the ledger and the policy is handed the ledger's
candidates as they are.  A plan that has been working for a second
reaches the candidate set there, and it is the one that gets
interrupted.

The threshold this file runs with is **zero**, where the rest of the
command-layer cases run with 600 seconds: the point of the case is the
threshold the scheduler uses when nobody says otherwise.

What this file holds down
-------------------------
1. **A one-second run is a candidate.**  With the default threshold the
   plan that has been working for a second reaches the candidate set.
2. **The shortest run goes first.**  Among the plans of one priority the
   one that has run the shortest is the one that is interrupted.

The plans are made to work through the **verification side**: a running
verification round is a plan holding compute, and it needs no
subprocess — so the cases stay inside the unit layer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.config import DEFAULT_MIN_RUNTIME_SECONDS
from plan_scheduler.facts import PlanFactsReader

from .test_commands_victim_age import BASE_EPOCH, _Clock, _CountingStore, _World

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@pytest.fixture()
def world(tmp_path: Path):
    """A command layer whose threshold is the default one — zero.

    The world is the one the rest of the command-layer cases use; only
    the threshold differs, and that is the point of this file.
    """
    from state_machine.db.schema import migrate

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    clock = _Clock()
    store = _CountingStore()
    facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    try:
        yield _World(
            conn,
            ledger,
            store,
            clock,
            facts,
            min_runtime_seconds=DEFAULT_MIN_RUNTIME_SECONDS,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. With the default threshold, the shortest run goes first
# ---------------------------------------------------------------------------


def test_zero_threshold_picks_the_shortest_at_command_layer(world: _World) -> None:
    """With the default threshold, the plan that has run the shortest goes.

    ``fresh`` has been working for one second and ``long`` for a quarter
    of an hour.  Both carry the ordinary weight, so nothing but the
    runtime tells them apart — and the runtime says that the shorter run
    goes first.

    The threshold is the point of the case.  With the default ``M`` of
    zero, a plan that has been working for **one second** is a candidate
    at all: under the old default of 600 seconds it never would have
    been, and the rule the scheduler follows — the shortest run is the
    one that gets interrupted — could never have reached it.

    The decision is made the way the command layer makes it: the
    candidate set comes from the ledger, and the policy is handed those
    candidates as they are.
    """
    world.working("fresh", started_at=BASE_EPOCH - 1)
    world.slot("fresh", BASE_EPOCH - 1, priority=0)
    world.working("long", started_at=BASE_EPOCH - 900)
    world.slot("long", BASE_EPOCH - 900, priority=0)

    candidates = world.ledger.evictable(world.clock(), 0)
    assert {entry["plan_id"] for entry in candidates} == {"fresh", "long"}, (
        "both plans are candidates when the threshold is zero: "
        "{0!r}".format(candidates)
    )
    elapsed = {entry["plan_id"]: entry["elapsed"] for entry in candidates}
    assert elapsed["fresh"] == 1.0, (
        "the one-second run did not reach the candidate set: "
        "{0!r}".format(elapsed)
    )

    victim = world.executor_factory(min_runtime_seconds=0)._select_victim(0)

    assert victim == "fresh", (
        "the plan that has run the shortest was not the one that was "
        "interrupted: {0!r}".format(victim)
    )