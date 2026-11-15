"""Unit tests: what the eviction rule does when the scheduler stays over capacity.

Why this file exists
--------------------
The rule that picks the plan to interrupt answers one question — **who**
goes first.  It does not answer the other one — **whether** the
scheduler keeps interrupting — and under a queue that stays due the two
come apart: the plan the scheduler admits may be the plan it interrupts,
tick after tick, and the work never gets done.

The protection window ``M`` is the mechanism that damps that, and it has
been ruled out as the answer.  So the question has to be answered with a
measurement instead of an argument: **does the churn happen, and how bad
is it?**  This file is that measurement.  It runs the scheduler's own
decisions — the real :func:`plan_scheduler.policy.converge` and the real
:func:`plan_scheduler.policy.select_victim`, with nothing replaced by a
double — over a run of consecutive ticks, and prints the numbers it saw
as one ``EVIDENCE`` line.

The scenario
------------
The scenario is small on purpose, and every part of it is written down
here so that the numbers can be read:

* **The scheduler has two slots**, and **six plans** are in play.  Two
  of them hold a slot when the observation opens; the other four wait.
* **Every plan carries the ordinary weight** (``priority = 0``) — the
  weight a plan has when the caller expressed no priority.  The
  priority stage of the rule therefore cannot tell the plans apart and
  the runtime stage decides; that is the ordinary case, and it is the
  case the churn has to be measured in.
* **Each tick**: the clock moves one tick interval forward, the head of
  the line takes a slot — its clock starts at zero — and the scheduler
  converges on the state it now holds, which is one slot more than it
  has.
* **An interrupted plan goes back into the line**, behind the plans that
  are still waiting.  That is the relay: A gives way to B, B gives way
  to C, and A comes round again.
* **The protection window is the one the configuration ships** — its
  default is zero, which is what
  ``test_default_protection_is_zero.py`` pins — so the observation
  measures the rule as it runs and not a damped version of it.

What the file does **not** pin
------------------------------
The numbers.  They are what the run produced, and they are printed
rather than asserted: a test that asserted them would fail the day the
rule changes, and a test that fails the day the rule changes is a
statement about the old rule and not about the churn.  What the file
pins are the structural properties that have to hold whatever the
numbers turn out to be:

1. **The observation runs.**  The run reports one ``EVIDENCE`` line; the
   count of interrupted plans is a non-negative whole number, and there
   is one entry per tick.
2. **A tick never interrupts more plans than it is over capacity.**  A
   tick that is one slot over interrupts at most one plan.
3. **A tick never interrupts the same plan twice.**  The plans a tick
   interrupts are distinct.
4. **The plans a tick interrupts were running in that tick.**  A victim
   is a plan that held a slot.
5. **The observation uses the rule itself.**  The first plan a tick
   interrupts is the plan :func:`select_victim` names when it is asked
   about the same state, so the numbers are the rule's numbers and not
   this file's paraphrase of them.

The evidence
------------
The ``EVIDENCE`` line is printed by the test, so ``pytest -s`` shows it::

    EVIDENCE ticks=<N> evictions=<X> per_tick=[...]

The line is the report: how many ticks ran, how many plans were
interrupted in all, and how many of them each tick interrupted.  It is
what the pull request carries as its verification report.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional

import pytest

from plan_scheduler.config import DEFAULT_MIN_RUNTIME_SECONDS, DEFAULT_TICK_SECONDS
from plan_scheduler.policy import converge, select_victim
from plan_scheduler.queue import queue_key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

#: How many slots the scheduler has in the scenario.
CAPACITY = 2

#: How many ticks the observation runs.
TICKS = 10

#: How many plans are in play: two hold a slot, the rest wait in the line.
PLANS = 6


# ---------------------------------------------------------------------------
# What a run produces
# ---------------------------------------------------------------------------


class Tick(NamedTuple):
    """What one tick of the scenario produced.

    The fields are the facts the tick's decision was made on and the
    answer it produced — nothing derived from them, so a reader can
    check the numbers against the state they came from.
    """

    admitted: List[str]
    """The plans that took a slot in this tick."""

    victims: List[str]
    """The plans this tick interrupted, in the order it interrupted them."""

    running: List[str]
    """The plans that held a slot when the tick converged."""

    over_capacity: int
    """How many slots over the scheduler was when the tick converged."""

    selected: Optional[str]
    """The plan :func:`select_victim` names for the same state."""


class Observation(NamedTuple):
    """A whole run of the scenario."""

    ticks: List[Tick]
    """The ticks, in order."""

    @property
    def per_tick(self) -> List[int]:
        """How many plans each tick interrupted."""
        return [len(tick.victims) for tick in self.ticks]

    @property
    def evictions(self) -> int:
        """How many plans the whole run interrupted."""
        return sum(self.per_tick)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def observe(
    capacity: int = CAPACITY,
    ticks: int = TICKS,
    plans: int = PLANS,
    starts_per_tick: int = 1,
) -> Observation:
    """Run the scenario and return what the scheduler decided.

    The scenario is the one the module docstring describes: the
    scheduler has ``capacity`` slots, ``plans`` plans are in play, and
    each tick the head of the line takes a slot before the scheduler
    converges on the state it holds.

    Nothing here replaces a decision: the plans a tick interrupts are
    the ones :func:`plan_scheduler.policy.converge` names, and the state
    it is given is the state the tick holds.

    Args:
        capacity: how many slots the scheduler has.
        ticks: how many ticks to run.
        plans: how many plans are in play.
        starts_per_tick: how many plans take a slot in each tick.

    Returns:
        One :class:`Tick` per tick, in order.
    """
    running: List[Dict[str, Any]] = []
    queue: List[Dict[str, Any]] = []
    recorded: List[Tick] = []
    seq = 0

    def enqueue(plan_id: str) -> None:
        """Put a plan into the line, behind the plans already waiting.

        A plan that comes back after being interrupted arrives again:
        it is given the next number in the line, and the line is the
        queue's own order, so it waits its turn behind the plans that
        are still there.
        """
        nonlocal seq
        seq += 1
        queue.append(
            {
                "plan_id": plan_id,
                "priority": 0,
                "seq": seq,
                "promote_order": None,
                "due": True,
            }
        )

    # The scheduler opens with its slots full: the first plans took
    # their slots at the moment the observation starts, and their clocks
    # start at zero there.
    for index in range(min(capacity, plans)):
        running.append({"plan_id": f"p{index + 1}", "elapsed": 0.0, "priority": 0})
    for index in range(min(capacity, plans), plans):
        enqueue(f"p{index + 1}")

    for _ in range(ticks):
        # The clock moves on: every plan that holds a slot has been
        # running for one more tick.
        for entry in running:
            entry["elapsed"] += DEFAULT_TICK_SECONDS

        # The head of the line takes a slot.  The line is the queue's
        # own order, so the head is the plan the shared comparator puts
        # first.
        admitted: List[str] = []
        for _ in range(starts_per_tick):
            if not queue:
                break
            queue.sort(key=queue_key)
            head = queue.pop(0)
            running.append(
                {
                    "plan_id": str(head["plan_id"]),
                    "elapsed": 0.0,
                    "priority": int(head["priority"]),
                }
            )
            admitted.append(str(head["plan_id"]))

        snapshot: Dict[str, Any] = {
            "running": [dict(entry) for entry in running],
            "used": len(running),
            "capacity": capacity,
            "allow_evict": True,
            "min_runtime_seconds": DEFAULT_MIN_RUNTIME_SECONDS,
            "queued": [dict(row) for row in queue],
        }

        # The scheduler converges on the state it now holds.
        victims = converge(snapshot, capacity)

        # The plans that held a slot when the tick converged — the ones
        # a victim has to be one of.
        held = [str(entry["plan_id"]) for entry in running]
        over_capacity = len(held) - capacity

        # The interrupted plans go back into the line, behind the plans
        # that are still waiting: the relay.
        for victim in victims:
            running = [
                entry for entry in running if str(entry["plan_id"]) != victim
            ]
            enqueue(victim)

        recorded.append(
            Tick(
                admitted=admitted,
                victims=list(victims),
                running=held,
                over_capacity=over_capacity,
                selected=select_victim(snapshot, DEFAULT_MIN_RUNTIME_SECONDS),
            )
        )

    return Observation(ticks=recorded)


# ---------------------------------------------------------------------------
# 1. The observation runs
# ---------------------------------------------------------------------------


def test_churn_over_ten_ticks_is_observable() -> None:
    """Ten ticks of an over-capacity scheduler, and the numbers they gave.

    The run prints one ``EVIDENCE`` line — the count of interrupted
    plans in all, and how many of them each tick interrupted — so the
    measurement travels with the run instead of being written down by
    hand.

    What the test pins is the shape of the report and the shape of
    every tick in it: the count is a non-negative whole number, there is
    one entry per tick, a tick never interrupts more plans than it is
    over capacity, the plans a tick interrupts were running in that
    tick, and the first of them is the plan the rule names on its own.
    """
    observation = observe(capacity=CAPACITY, ticks=TICKS, plans=PLANS)
    per_tick = observation.per_tick
    evictions = observation.evictions

    print(
        "EVIDENCE ticks={0} evictions={1} per_tick=[{2}]".format(
            TICKS, evictions, ",".join(str(count) for count in per_tick)
        )
    )

    assert isinstance(evictions, int)
    assert evictions >= 0
    assert len(per_tick) == TICKS
    assert evictions == sum(per_tick)

    for index, tick in enumerate(observation.ticks):
        assert len(tick.victims) <= tick.over_capacity, (
            f"tick {index} interrupted {len(tick.victims)} plans while it was "
            f"{tick.over_capacity} over capacity"
        )
        assert len(set(tick.victims)) == len(tick.victims), (
            f"tick {index} interrupted the same plan twice: {tick.victims}"
        )
        assert set(tick.victims) <= set(tick.running), (
            f"tick {index} interrupted a plan that was not running: {tick.victims}"
        )
        if tick.victims:
            assert tick.victims[0] == tick.selected, (
                f"tick {index} interrupted {tick.victims[0]} first, but the rule "
                f"names {tick.selected}"
            )


# ---------------------------------------------------------------------------
# 2. A tick never interrupts the same plan twice
# ---------------------------------------------------------------------------


def test_no_plan_is_evicted_twice_in_one_tick() -> None:
    """The plans one tick interrupts are distinct.

    The tick is three slots over the capacity, so it has three plans to
    interrupt and the check has something to chew on: the same plan
    cannot be interrupted twice in the same tick, because a plan that
    has been interrupted is no longer running.
    """
    observation = observe(
        capacity=CAPACITY, ticks=1, plans=PLANS, starts_per_tick=3
    )
    tick = observation.ticks[0]
    victims = tick.victims

    assert len(set(victims)) == len(victims)
    assert len(victims) <= tick.over_capacity
    assert set(victims) <= set(tick.running)
