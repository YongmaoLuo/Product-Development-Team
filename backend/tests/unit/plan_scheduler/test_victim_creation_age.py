"""Unit tests: the victim is the **youngest plan**, not the newest slot.

What this file holds down
-------------------------
The scheduler interrupts a running plan when a slot has to be freed,
and it has to pick the right one.  The old rule — "the plan that took
its slot most recently" — reads the wrong column: the slot timestamp
is rewritten whenever the ledger is reconciled, so after a reconcile
every plan looks as if it had just started and the rule stops meaning
anything.

The rule that survives the rewrite is the one about the **plan** rather
than about the slot: the youngest plan is the one that was created
most recently, because it is the one least likely to be wrapping up.
Interrupting the plan that is closest to finishing is what the operator
would not forgive.

The contracts this file pins:

1. **The creation time decides.**  A plan that took its slot early but
   was created late is the victim; a plan that took its slot late but
   was created early is not.
2. **An unreadable creation time falls back to the slot time.**  A
   missing, ``NULL``, zero or non-numeric ``created_at`` is not a
   creation time — the policy falls back to the moment the slot was
   taken, which is the behaviour it always had.
3. **A tie is broken by name.**  Two plans created at the same moment
   are told apart by ``plan_id`` in dictionary order, so the answer
   never depends on the order the entries happened to arrive in.
4. **No candidate is an answer, not a crash.**  An empty candidate set
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
# 1. The creation time decides
# ---------------------------------------------------------------------------


def test_select_victim_orders_by_creation_age_not_slot_time() -> None:
    """The plan created last is the victim — not the one that took its slot last.

    ``A`` took its slot most recently and ``B`` was created most
    recently.  Under the old rule (``acquired_at``) the victim would be
    ``A``; the rule this file pins picks ``B``.
    """
    snapshot = {
        "running": [
            _running("A", 3600, 2000, created_at=1000),
            _running("B", 3600, 1000, created_at=2000),
        ]
    }

    assert select_victim(snapshot, 600) == "B"


def test_select_victim_orders_by_creation_age_whatever_order_the_entries_came_in() -> None:
    """The victim's identity does not depend on the order of the running set."""
    youngest_first = {
        "running": [
            _running("A", 3600, 2000, created_at=1000),
            _running("B", 3600, 1000, created_at=2000),
        ]
    }
    oldest_first = {
        "running": [
            _running("B", 3600, 1000, created_at=2000),
            _running("A", 3600, 2000, created_at=1000),
        ]
    }

    assert select_victim(youngest_first, 600) == "B"
    assert select_victim(oldest_first, 600) == "B"


# ---------------------------------------------------------------------------
# 2. An unreadable creation time falls back to the slot time
# ---------------------------------------------------------------------------


def test_select_victim_falls_back_to_acquired_at_without_created_at() -> None:
    """A running entry without a creation time is judged by its slot time."""
    snapshot = {
        "running": [
            _running("older", 3600, 1000),
            _running("younger", 3600, 2000),
        ]
    }

    assert select_victim(snapshot, 600) == "younger"


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
def test_select_victim_treats_an_unusable_created_at_as_missing(
    label: str, created_at
) -> None:
    """A creation time the policy cannot read is no creation time at all.

    ``None``, ``0``, a string and a boolean all fall back to the moment
    the slot was taken — the behaviour the policy always had.
    """
    snapshot = {
        "running": [
            _running("older", 3600, 1000, created_at=created_at),
            _running("younger", 3600, 2000, created_at=created_at),
        ]
    }

    assert select_victim(snapshot, 600) == "younger"


# ---------------------------------------------------------------------------
# 3. A tie is broken by name
# ---------------------------------------------------------------------------


def test_select_victim_ties_break_on_plan_id() -> None:
    """Two plans created at the same moment are told apart by name.

    The slot times point the other way round, so the answer is the
    name's and not the slot's.
    """
    snapshot = {
        "running": [
            _running("beta", 3600, 2000, created_at=5000),
            _running("alpha", 3600, 1000, created_at=5000),
        ]
    }

    assert select_victim(snapshot, 600) == "alpha"


# ---------------------------------------------------------------------------
# 4. No candidate is an answer, not a crash
# ---------------------------------------------------------------------------


def test_select_victim_returns_none_without_candidates() -> None:
    """Nobody is running — there is nobody to interrupt."""
    assert select_victim({"running": []}, 600) is None
