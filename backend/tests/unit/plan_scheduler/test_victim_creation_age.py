"""Unit tests: ``created_at`` no longer has a say in the victim's identity.

What this file holds down
-------------------------
The scheduler interrupts a running plan when a slot has to be freed, and
it has to pick the right one.  The rule it used to follow — "the plan
that was created most recently" — read a column that says nothing about
how much work a plan has done, and the column it fell back to — the
moment the slot was taken — is rewritten whenever the ledger is
reconciled, so after a reconcile every plan looks as if it had just
started and the rule stops meaning anything.

Both columns are gone from the order.  What decides now is the
**priority** and the **runtime**, and neither of them is a timestamp of
the plan's birth.  The order itself is pinned in
``test_victim_priority_order.py``; what this file holds down is that
the creation time is out of it altogether.

The contracts this file pins:

1. **The creation time is not read.**  Two plans that differ only in
   ``created_at`` are told apart by their runtime, and a ``created_at``
   built to point the wrong way does not move the victim.
2. **The order does not depend on the order the entries came in.**
3. **A creation time the policy cannot read is no creation time at
   all** — and since the column is not read, an unusable one changes
   nothing.
4. **A tie is broken by name.**  Two plans of the same priority and the
   same runtime are told apart by ``plan_id`` in dictionary order.
5. **No candidate is an answer, not a crash.**  An empty candidate set
   gives ``None``.

The tests assert **the decision the operator sees** — the plan id — and
not the shape of the snapshot that produced it.
"""

from __future__ import annotations

import pytest

from plan_scheduler.policy import select_victim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _running(plan_id: str, elapsed: float, acquired_at: float, **extra) -> dict:
    """A running plan: how long it has run, when it took its slot, and
    whatever else the caller knows about it.
    """
    entry = {"plan_id": plan_id, "elapsed": elapsed, "acquired_at": acquired_at}
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# 1. The creation time is not read
# ---------------------------------------------------------------------------


def test_created_at_is_not_read() -> None:
    """The creation time has no say: the runtime decides.

    ``late`` was created most recently and has run the longest; ``early``
    was created first and has run the shortest.  The victim is ``early``
    — the creation time is not read, and the answer is the runtime's.
    ``M`` is 60 seconds, so that both plans are eligible and the runtime
    alone is what is left to decide.
    """
    snapshot = {
        "running": [
            _running("late", 900, 100, created_at=2000),
            _running("early", 100, 200, created_at=1000),
        ]
    }

    assert select_victim(snapshot, 60) == "early"


def test_created_at_does_not_move_the_victim() -> None:
    """Two snapshots that differ only in ``created_at`` give the same answer.

    The creation times are swapped between the two runs of the same two
    plans — and the victim is the shorter run both times.
    """
    creation_times_swapped = {
        "running": [
            _running("a", 900, 100, created_at=2000),
            _running("b", 100, 200, created_at=1000),
        ]
    }
    creation_times_the_other_way_round = {
        "running": [
            _running("a", 900, 100, created_at=1000),
            _running("b", 100, 200, created_at=2000),
        ]
    }

    assert select_victim(creation_times_swapped, 60) == "b"
    assert select_victim(creation_times_the_other_way_round, 60) == "b"


def test_created_at_does_not_depend_on_the_order_the_entries_came_in() -> None:
    """The victim's identity does not depend on the order of the running set."""
    shortest_first = {
        "running": [
            _running("early", 100, 200, created_at=1000),
            _running("late", 900, 100, created_at=2000),
        ]
    }
    longest_first = {
        "running": [
            _running("late", 900, 100, created_at=2000),
            _running("early", 100, 200, created_at=1000),
        ]
    }

    assert select_victim(shortest_first, 60) == "early"
    assert select_victim(longest_first, 60) == "early"


def test_select_victim_without_created_at_judges_by_the_runtime() -> None:
    """A running entry that carries no creation time is judged like any other.

    The slot times point the other way round — ``long`` took its slot
    most recently — so the answer is the runtime's and not the slot's.
    """
    snapshot = {
        "running": [
            _running("long", 900, 200),
            _running("short", 100, 100),
        ]
    }

    assert select_victim(snapshot, 60) == "short"


# ---------------------------------------------------------------------------
# 2. An unreadable creation time changes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,created_at",
    [
        ("null", None),
        ("zero", 0),
        ("not-a-number", "not a number"),
        ("a-flag", True),
    ],
    ids=["null", "zero", "not-a-number", "a-flag"],
)
def test_select_victim_treats_an_unusable_created_at_as_no_creation_time(
    label: str, created_at
) -> None:
    """A creation time the policy cannot read changes nothing.

    ``None``, ``0``, a string and a boolean all read as no creation time
    at all — and since the column is not read, the answer is the
    runtime's in every one of the four cases.
    """
    snapshot = {
        "running": [
            _running("late", 900, 100, created_at=created_at),
            _running("early", 100, 200, created_at=created_at),
        ]
    }

    assert select_victim(snapshot, 60) == "early"


# ---------------------------------------------------------------------------
# 3. A tie is broken by name
# ---------------------------------------------------------------------------


def test_select_victim_ties_break_on_plan_id() -> None:
    """Two plans of the same priority and the same runtime are told apart by name.

    The creation times point the other way round — ``beta`` was created
    most recently — so the answer is the name's and not the creation
    time's.
    """
    snapshot = {
        "running": [
            _running("beta", 300, 200, created_at=5000),
            _running("alpha", 300, 100, created_at=1000),
        ]
    }

    assert select_victim(snapshot, 60) == "alpha"


# ---------------------------------------------------------------------------
# 4. No candidate is an answer, not a crash
# ---------------------------------------------------------------------------


def test_select_victim_returns_none_without_candidates() -> None:
    """Nobody is running — there is nobody to interrupt."""
    assert select_victim({"running": []}, 600) is None
