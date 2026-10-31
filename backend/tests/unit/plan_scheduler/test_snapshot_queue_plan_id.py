"""Unit tests: the queue row carries the plan it is about.

What is pinned here
-------------------
A queued plan is a plan, and the panel has to be able to say *which*
one it is looking at.  The queue row used to be a set of numbers —
where it stands, how urgent it is, why it is waiting — and the plan
itself was missing from it, so the panel drew a dash in the plan
column and the "promote" and "start" buttons had nothing to act on.

So the read model hands out the plan's identity and its ``due`` flag
alongside the numbers:

1. **The row names the plan.**  ``plan_id`` is the plan's own name,
   taken from the row the queue holds.
2. **The row says whether the plan may start.**  ``due`` is the facts'
   answer, passed on as it is: a plan scheduled for later is not due,
   and the panel shows that.
3. **A row that says nothing says something sensible.**  A row without
   a ``plan_id`` reads as the empty name and a row without a ``due``
   reads as due — a plan nobody has scheduled is due, because nothing
   holds it back.

The tests assert the fields the panel reads, not the whole document:
the contract is the row, and the row is what the operator looks at.
"""

from __future__ import annotations

from typing import Any, Dict, List

from plan_scheduler.snapshot import build


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queue_row(**overrides: Any) -> Dict[str, Any]:
    """A queue row, shaped like the row the engine writes into the state."""
    row: Dict[str, Any] = {
        "plan_id": "p-1",
        "seq": 1,
        "priority": 0,
        "promote_order": None,
        "due": True,
        "reason": "waiting_for_slot",
    }
    row.update(overrides)
    return row


def _state(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The state of one tick, with the queue it decides on."""
    return {
        "capacity": 4,
        "used": 0,
        "now": 1000.0,
        "min_runtime_seconds": 600,
        "queue": rows,
        "running": [],
    }


# ---------------------------------------------------------------------------
# 1. The row names the plan
# ---------------------------------------------------------------------------


def test_queue_row_carries_plan_id() -> None:
    """A queued row carries the name of the plan it is about.

    Without it the panel has nothing to put in the plan column and
    nothing to hand to the "promote" and "start" buttons — the row is
    a line in a list with no way of saying which plan it belongs to.
    """
    result = build(_state([_queue_row(plan_id="p-42")]))

    assert result["queue"][0]["plan_id"] == "p-42"


def test_queue_row_without_plan_id_is_empty_string() -> None:
    """A row with no plan in it reads as the empty name, not as ``None``.

    The panel draws a dash for an empty name and a dash for nothing at
    all, so the two are the same to it — but the read model's contract
    says the field is a string, and a ``None`` in it would be a second
    answer to "which plan is this".
    """
    row = _queue_row()
    del row["plan_id"]

    result = build(_state([row]))

    assert result["queue"][0]["plan_id"] == ""


# ---------------------------------------------------------------------------
# 2. The row says whether the plan may start
# ---------------------------------------------------------------------------


def test_queue_row_carries_due() -> None:
    """A queued row carries the facts' ``due`` answer as it is.

    A plan scheduled for later is not due, and the panel has to be able
    to show that — so the flag is passed on rather than recomputed and
    rather than dropped.
    """
    result = build(_state([_queue_row(due=False)]))

    assert result["queue"][0]["due"] is False


def test_queue_row_without_due_is_due() -> None:
    """A row that says nothing about ``due`` reads as due.

    A plan nothing has been scheduled for is due: nothing holds it
    back.  Reading it as "not due" would hide a runnable plan behind a
    flag nobody set.
    """
    row = _queue_row()
    del row["due"]

    result = build(_state([row]))

    assert result["queue"][0]["due"] is True


# ---------------------------------------------------------------------------
# 3. The row's shape
# ---------------------------------------------------------------------------


def test_queue_row_shape_matches_the_contract() -> None:
    """A queue row carries exactly the fields the contract declares."""
    result = build(_state([_queue_row(plan_id="p-1")]))

    assert result["queue"][0] == {
        "position": 1,
        "plan_id": "p-1",
        "seq": 1,
        "priority": 0,
        "promote_order": None,
        "due": True,
        "reason": "waiting_for_slot",
    }


def test_an_empty_queue_is_an_empty_list() -> None:
    """Nothing queued is an empty list, not an error.

    The read model is read on every poll, and an idle scheduler is a
    normal thing to look at.
    """
    result = build(_state([]))

    assert result["queue"] == []
    assert result["queue_length"] == 0
