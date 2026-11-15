"""Unit tests: the victim is the **last in the line**, not the youngest.

What this file holds down
-------------------------
The scheduler interrupts a running plan when a slot has to be freed,
and it has to pick the right one.  The old rule — "the plan that was
created most recently" — read a column that says nothing about how
much work a plan has done, and it let a plan the caller deliberately
pushed back (``priority = -10``) be squeezed out by a plan that stands
later in the line (``priority = 0``).

The rule that replaces it is **two stages**:

1. **The priority decides.**  The queue orders plans by ascending
   priority, so a smaller number stands *earlier* in the line — and the
   plan that stands earlier is the one the caller asked to be first.
   The plan that stands *last* is the one the scheduler can most
   afford to interrupt, so the **larger** priority number is the one
   that goes, whatever it has been running for.
2. **The runtime decides what is left.**  Among plans with the same
   priority the one that has run the **shortest** goes first: it has
   invested the least, and interrupting it loses the least work.

A priority the policy cannot read is the ordinary weight, ``0`` — a
parse failure must never lift a plan to the front of the order.

The contracts this file pins:

1. **The priority beats the runtime.**  A long-running plan with a
   larger priority number is the victim even when a younger plan with
   a smaller one is running beside it.
2. **The runtime breaks a tie in priority.**  The shortest run goes
   first.
3. **The creation time no longer decides anything.**  ``created_at``
   is not read; two plans that differ only in it are told apart by
   their runtime.
4. **A missing priority is zero.**  A plan the snapshot says nothing
   about carries the same weight as a plan whose priority is ``0``.
5. **A tie is broken by name.**  Two plans with the same priority and
   the same runtime are told apart by ``plan_id`` in dictionary order,
   so the answer never depends on the order the entries came in.
6. **No candidate is an answer, not a crash.**  An empty candidate set
   gives ``None``.
7. **A row may be an object.**  The priority is read the way every other
   field of the row is read — by ``get`` when the row answers it, by
   ``getattr`` when it does not.

The tests assert **the decision the operator sees** — the plan id — and
not the shape of the snapshot that produced it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plan_scheduler.policy import select_victim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Sentinel for "the snapshot says nothing about this plan's priority".
_MISSING = object()


def _running(plan_id: str, elapsed: object, priority: object = _MISSING, **extra) -> dict:
    """A running plan: how long it has run, what the caller asked for,
    and whatever else the caller knows about it.

    The priority is left out of the entry entirely when the caller does
    not give one — that is the "the snapshot says nothing" case, and it
    has to be different from a priority of ``0`` only in the snapshot,
    never in the decision.
    """
    entry = {"plan_id": plan_id, "elapsed": elapsed}
    if priority is not _MISSING:
        entry["priority"] = priority
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# 1. The priority beats the runtime
# ---------------------------------------------------------------------------


def test_priority_beats_runtime() -> None:
    """The plan that stands later in the line goes, however long it ran.

    ``bug`` was pushed back by the caller (``priority = -10``) and has
    only been running for half a minute; ``feat`` stands later in the
    line (``priority = 0``) and has been running for an hour.  The
    victim is ``feat``: the plan the caller put first is not the one
    that gets squeezed out.
    """
    snapshot = {
        "running": [
            _running("bug", 30, priority=-10),
            _running("feat", 3600, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "feat"


def test_a_higher_priority_number_goes_first() -> None:
    """The larger the number, the earlier the plan is interrupted."""
    snapshot = {
        "running": [
            _running("back", 600, priority=-10),
            _running("middle", 600, priority=0),
            _running("front", 600, priority=10),
        ]
    }

    assert select_victim(snapshot, 0) == "front"


# ---------------------------------------------------------------------------
# 2. The runtime breaks a tie in priority
# ---------------------------------------------------------------------------


def test_shortest_runtime_wins_within_same_priority() -> None:
    """Two plans of the same priority: the shorter run goes first."""
    snapshot = {
        "running": [
            _running("long", 900, priority=0),
            _running("short", 100, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "short"


def test_the_runtime_does_not_overrule_a_protected_window() -> None:
    """A plan inside its protected window is not a candidate at all.

    ``young`` has the largest priority number and the shortest runtime,
    but it has not been running for ``M`` seconds — so it is not in the
    candidate set and the priority cannot lift it in.
    """
    snapshot = {
        "running": [
            _running("young", 10, priority=100),
            _running("old", 600, priority=0),
        ]
    }

    assert select_victim(snapshot, 600) == "old"


# ---------------------------------------------------------------------------
# 3. The creation time no longer decides
# ---------------------------------------------------------------------------


def test_created_at_no_longer_decides() -> None:
    """``created_at`` is not read: the runtime decides.

    ``late`` was created most recently and has run the longest;
    ``early`` was created first and has run the shortest.  The victim is
    ``early`` — the creation time no longer has a say.
    """
    snapshot = {
        "running": [
            _running("late", 3600, priority=0, created_at=2000),
            _running("early", 100, priority=0, created_at=1000),
        ]
    }

    assert select_victim(snapshot, 0) == "early"


def test_acquired_at_no_longer_decides() -> None:
    """The slot timestamp is not read either."""
    snapshot = {
        "running": [
            _running("late-slot", 3600, priority=0, acquired_at=2000),
            _running("early-slot", 100, priority=0, acquired_at=1000),
        ]
    }

    assert select_victim(snapshot, 0) == "early-slot"


# ---------------------------------------------------------------------------
# 4. A missing priority is zero
# ---------------------------------------------------------------------------


def test_missing_priority_is_zero() -> None:
    """A plan the snapshot says nothing about carries the weight ``0``.

    ``no-priority`` has no priority at all and ``zero`` has ``0``:
    they weigh the same, so the runtime decides between them.
    """
    snapshot = {
        "running": [
            _running("no-priority", 900),
            _running("zero", 100, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "zero"


@pytest.mark.parametrize(
    "priority",
    [None, True, False, "not a number", 1.5],
    ids=["null", "true", "false", "not-a-number", "not-whole"],
)
def test_a_priority_that_cannot_be_read_is_zero(priority: object) -> None:
    """A priority the policy cannot read is the ordinary weight.

    ``None``, a boolean, a string and a number that is not whole all
    read as ``0`` — and ``0`` must never put the plan at the front of
    the order: the runtime decides, exactly as it does for a plan whose
    priority really is ``0``.
    """
    snapshot = {
        "running": [
            _running("unreadable", 900, priority=priority),
            _running("ordinary", 100, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "ordinary"


def test_an_unreadable_priority_does_not_protect_a_pushed_back_plan() -> None:
    """An unreadable priority is the ordinary weight, not the top of the line.

    ``unreadable`` carries a priority the policy cannot read, so it weighs
    ``0``.  ``pushed-back`` was put first by the caller
    (``priority = -10``), so it stands earlier in the line and keeps its
    slot — so the unreadable one is the victim, not the plan the caller
    protected.  A parse failure must not lift a plan to the front of the
    order, and it must not sink it below a plan that stands there either.
    """
    snapshot = {
        "running": [
            _running("unreadable", 600, priority="not a number"),
            _running("pushed-back", 600, priority=-10),
        ]
    }

    assert select_victim(snapshot, 0) == "unreadable"


# ---------------------------------------------------------------------------
# 5. A tie is broken by name
# ---------------------------------------------------------------------------


def test_ties_break_on_plan_id() -> None:
    """Two plans of the same priority and the same runtime: the smaller
    name goes first."""
    snapshot = {
        "running": [
            _running("beta", 600, priority=0),
            _running("alpha", 600, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "alpha"


def test_the_victim_does_not_depend_on_the_order_the_entries_came_in() -> None:
    """The decision is a function of the set, not of the order."""
    one = {
        "running": [
            _running("bug", 30, priority=-10),
            _running("feat", 3600, priority=0),
        ]
    }
    other = {
        "running": [
            _running("feat", 3600, priority=0),
            _running("bug", 30, priority=-10),
        ]
    }

    assert select_victim(one, 0) == select_victim(other, 0) == "feat"


# ---------------------------------------------------------------------------
# 6. No candidate is an answer, not a crash
# ---------------------------------------------------------------------------


def test_no_candidate_returns_none() -> None:
    """Nobody is running — there is nobody to interrupt."""
    assert select_victim({"running": []}, 600) is None


def test_everybody_inside_the_protected_window_returns_none() -> None:
    """Every plan is still inside its protected window."""
    snapshot = {
        "running": [
            _running("r1", 10, priority=10),
            _running("r2", 20, priority=20),
        ]
    }

    assert select_victim(snapshot, 600) is None


def test_a_duration_that_cannot_be_read_protects_the_plan() -> None:
    """A duration the policy cannot read is treated as "just started"."""
    snapshot = {
        "running": [
            _running("r1", "not a number", priority=100),
        ]
    }

    assert select_victim(snapshot, 600) is None


def test_an_unreadable_duration_is_a_candidate_without_protection() -> None:
    """Without a protected window the unreadable duration is a candidate.

    The unreadable duration reads as ``0``, so it is the shortest run
    there is and it is the one that goes.
    """
    snapshot = {
        "running": [
            _running("broken", "not a number", priority=0),
            _running("other", 600, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "broken"


# ---------------------------------------------------------------------------
# 7. Rows that are not mappings
# ---------------------------------------------------------------------------


def test_a_row_that_is_not_a_mapping_is_read_by_attribute() -> None:
    """A snapshot may carry running rows that are objects, not mappings.

    The priority is read the way every other field of the row is: a row
    that answers ``get`` is asked for it, and anything else is asked with
    ``getattr``.
    """
    snapshot = {
        "running": [
            SimpleNamespace(plan_id="back", elapsed=600, priority=-10),
            SimpleNamespace(plan_id="front", elapsed=600, priority=0),
        ]
    }

    assert select_victim(snapshot, 0) == "front"


def test_a_row_that_is_not_a_mapping_says_nothing_about_its_priority() -> None:
    """An object without a priority attribute carries the ordinary weight."""
    snapshot = {
        "running": [
            SimpleNamespace(plan_id="older", elapsed=600),
            SimpleNamespace(plan_id="shorter", elapsed=100),
        ]
    }

    assert select_victim(snapshot, 0) == "shorter"
