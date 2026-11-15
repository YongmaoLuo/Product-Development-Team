"""Integration: the priority a plan was queued with reaches the panel.

Background
----------
The number a caller gives a plan when it is queued is the scheduler's
own copy of the caller's intent, and it lives in two places: the
**queue row**, while the plan waits for its turn, and the **slot
row**, while the plan holds compute.

The two halves are pinned apart elsewhere.  The file
``test_slot_priority_admission.py`` holds down the write — the
admission moves the number into the slot — and the snapshot's own cases
hold down the read — the panel carries the weight of every running
plan.  Neither of them walks the whole way, and a scheduler that moved
the number into the slot but never read it back out would pass both and
still leave the operator looking at a running plan whose weight nobody
can name.

So this file walks the way once, from end to end: a plan is queued
with a priority, the tick admits it, and the number the caller gave is
read back out of the panel's running rows.

What is pinned here
-------------------
``test_priority_survives_admission``
    A plan queued at ``-10`` holds a slot of ``-10``, and the snapshot
    reports it as running at ``-10``.
``test_default_priority_is_zero_after_admission``
    A plan nobody gave a number to runs at ``0`` — and the default is
    not a crown: queued behind a plan at ``-10``, it waits its turn.

The real engine is used
-----------------------
The cases drive the **production** engine — the store, the capacity
ledger, the facts reader, the command executor and the live exit — on a
real SQLite file with the production schema.  Only three things are the
case's own: the clock, the lifecycle (which changes the facts the way
the real one does) and the facts the case writes itself.

The world, the doubles and the helpers are the ones
``test_engine_capacity_rules.py`` already builds: they are imported
rather than copied, so the two files cannot drift apart.  That file is
not modified by this one.

The tick is driven **synchronously**, one call at a time, so nothing
the case asserts on depends on a worker thread and on when it happened
to run.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_priority_survives_admission.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from tests.integration.plan_scheduler.test_engine_capacity_rules import (
    QUEUED,
    ApplyingSink,
    FakeLifecycle,
    World,
    _open_db,
    _routing,
    _world,
)


#: The priority a plan is queued with, to prove that the number is
#: carried over and not merely defaulted.
QUEUED_PRIORITY = -10


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _live_world(
    db_factory: Any,
    plan_factory: Any,
    plan_ids: Any,
    capacity: int,
) -> World:
    """Build the world the case drives, with the exit that acts.

    The exit the tick's decisions go through has to be the **live**
    one — the one that starts the admitted plan through the lifecycle.
    A recording exit would leave the plan admitted but not working, and
    a plan that is not working is not in the snapshot's running rows at
    all: the chain this file walks would break in the middle without
    saying so.
    """
    world = _world(
        db_factory,
        plan_factory,
        plan_ids=tuple(plan_ids),
        capacity=capacity,
    )
    assert isinstance(world.sink, ApplyingSink), (
        "the case needs the exit that carries the decisions out, not a "
        "recording one: {0!r}".format(type(world.sink))
    )
    assert isinstance(world.lifecycle, FakeLifecycle), (
        "the case needs the lifecycle that changes the facts: {0!r}".format(
            type(world.lifecycle)
        )
    )
    return world


def _slot_priority(db_path: Path, plan_id: str) -> Any:
    """The priority the slot row of the plan holds.

    ``None`` means there is no slot row at all — a plan that holds no
    slot has no priority to read, and a case that read the priority of
    a row that is not there would be reading nothing.

    The row is read through a connection of its own, opened on the
    case's database file: what the case reads is what the engine
    committed, and never the engine's open transaction.
    """
    conn = _open_db(db_path)
    try:
        row = conn.execute(
            "SELECT priority FROM scheduler_slot WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _running_priorities(world: World) -> Dict[str, Any]:
    """The priority every running plan is reported at, by plan id."""
    running: List[Dict[str, Any]] = world.engine.snapshot()["running"]
    return {str(row["plan_id"]): row["priority"] for row in running}


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_priority_survives_admission(
    db_factory: Any, plan_factory: Any
) -> None:
    """The number the plan was queued with is the number the panel says.

    The chain is walked once, from end to end: the plan is queued at
    ``-10``, the tick admits it, the slot row holds ``-10``, and the
    snapshot's running rows say ``-10`` as well.

    The second plan is the witness.  It is queued at the default, and
    there is only one slot: the line sorts by ascending priority, so
    the plan at ``-10`` stands ahead of it and is the one that gets
    the slot.  A scheduler that dropped the number on the way in would
    admit the other plan instead, and the panel would report a weight
    nobody gave.
    """
    database = db_factory()
    world = _live_world(db_factory, plan_factory, ("bug", "feat"), capacity=1)
    try:
        _routing(world.conn, "bug", QUEUED)
        _routing(world.conn, "feat", QUEUED)
        world.store.enqueue(world.conn, "bug", priority=QUEUED_PRIORITY)
        world.store.enqueue(world.conn, "feat")

        world.engine._run_tick()

        assert world.sink.admits() == ["bug"], (
            "the plan that stands first in the line is the one that runs; "
            "the admitted plans are {0!r}".format(world.sink.admits())
        )
        assert _slot_priority(database.path, "bug") == QUEUED_PRIORITY, (
            "the slot row does not carry the priority the plan was queued "
            "with; it carries {0!r}".format(
                _slot_priority(database.path, "bug")
            )
        )
        assert _running_priorities(world) == {"bug": QUEUED_PRIORITY}, (
            "the panel does not report the running plan at the priority it "
            "was queued with; it reports {0!r}".format(
                _running_priorities(world)
            )
        )
    finally:
        world.close()


def test_default_priority_is_zero_after_admission(
    db_factory: Any, plan_factory: Any
) -> None:
    """A plan nobody gave a number to runs at zero — and it waits.

    Two things are pinned at once.  The default is ``0``: a plan queued
    without naming a priority holds a slot of ``0`` and is reported by
    the snapshot at ``0`` — never NULL, because a weight nobody can
    compare is not a weight.

    And the default is not a crown.  The queue sorts by ascending
    priority, so ``0`` stands **behind** ``-10``: the plan that was
    queued first is the one that waits, and the panel reports the two
    running plans at the two weights they were given.
    """
    database = db_factory()
    world = _live_world(db_factory, plan_factory, ("plain", "bug"), capacity=2)
    try:
        _routing(world.conn, "plain", QUEUED)
        _routing(world.conn, "bug", QUEUED)
        # The row is written without naming a priority, so it carries
        # the default — and it is queued *first*, so the order the
        # plans are admitted in is the order the line gives.
        world.store.enqueue(world.conn, "plain")
        world.store.enqueue(world.conn, "bug", priority=QUEUED_PRIORITY)

        world.engine._run_tick()

        assert world.sink.admits() == ["bug", "plain"], (
            "the plan at -10 stands first in the line and the default does "
            "not make it stand ahead of it; the admitted plans are "
            "{0!r}".format(world.sink.admits())
        )
        assert _slot_priority(database.path, "plain") == 0, (
            "a plan nobody gave a priority to runs at 0; its slot row "
            "carries {0!r}".format(_slot_priority(database.path, "plain"))
        )
        assert _running_priorities(world) == {"bug": QUEUED_PRIORITY, "plain": 0}, (
            "the panel does not report the two running plans at the weights "
            "they were given; it reports {0!r}".format(_running_priorities(world))
        )
    finally:
        world.close()
