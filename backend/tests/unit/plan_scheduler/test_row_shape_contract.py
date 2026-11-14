"""The read model's rows are a closed contract — and the phase is part of it.

Background
----------
The scheduler's read model is read by the panel, by the LUI and by the
API, and all three read it **by key name**.  A row that carries a field
one reader knows about and another does not is not a shared read model —
it is two read models that happen to agree most of the time.

So the shape is closed, and it is pinned **exactly**: the key set of a
queued row, the key set of a running row and the key set of the
document are each written down here, and a field that appears without
being declared fails the test rather than quietly reaching the panel.

What changed
------------
The phase — what a plan is *doing*, in the routing layer's own words —
became part of the row.  It used to die in the last hop: the state
carried it, and the snapshot's two views picked their fields from a
fixed list that did not mention it, so the panel was told "running"
without being told what was running.

Adding a field to a row whose shape is closed is a decision, not an
accident, and a decision that is not written down anywhere is a decision
that gets undone by the next person to touch the whitelist.  This file
is where that decision is written down.

What this file asserts
----------------------
**Key sets, and nothing else.**  The values the rows carry are pinned
elsewhere — the phase travels on
``tests/unit/plan_scheduler/test_snapshot_phase.py``, the order is
pinned on ``tests/unit/plan_scheduler/test_snapshot.py``.  Repeating
them here would make this file a second place to keep in step, and a
file that asserts everything breaks on everything.

The assertions are exact equality, never ``issubset``: a subset
assertion would still pass when a field was added, and adding a field
is precisely the thing that has to be a decision.

The document's own six keys are asserted too, and they are asserted as
**six** — a sub-phase is a field on a *row*, and it did not turn the
document into a seventh-key document.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from plan_scheduler.snapshot import build


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: The keys a **queued** row carries — and no others.
#:
#: The phase sits next to the reason: both say what is happening to the
#: plan, one in the routing layer's words and one in the policy's.
QUEUE_ROW_KEYS = {
    "position",
    "plan_id",
    "seq",
    "priority",
    "promote_order",
    "due",
    "reason",
    "phase",
}

#: The keys a **running** row carries — and no others.
#:
#: The phase sits next to the two clocks: the row says how long the
#: plan has been running and what the plan is running *for*, and a
#: panel that shows the first without the second draws the same word
#: for a plan that is executing and for one that is repairing.
RUNNING_ROW_KEYS = {
    "plan_id",
    "acquired_at",
    "elapsed_sec",
    "running_sec",
    "started_at",
    "evictable",
    "workspace",
    "phase",
}

#: The keys the **document** carries — and no others.
#:
#: Six, and still six: the phase is a field on a row, and it did not
#: turn the document into a seventh-key document.
TOP_LEVEL_KEYS = {
    "capacity",
    "queue_length",
    "over_capacity",
    "queue",
    "running",
    "generated_at",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queue_row(plan_id: str, seq: int, phase: Optional[str] = None) -> Dict[str, Any]:
    """A queue row, shaped like the row the engine writes into the state.

    ``phase`` is left out of the row entirely when it is ``None``, so a
    case can hand the snapshot a row written before the phase existed.
    """
    row: Dict[str, Any] = {
        "plan_id": plan_id,
        "seq": seq,
        "priority": 0,
        "promote_order": None,
        "due": True,
        "reason": "waiting_for_slot",
    }
    if phase is not None:
        row["phase"] = phase
    return row


def _running_row(
    plan_id: str, acquired_at: float, phase: Optional[str] = None
) -> Dict[str, Any]:
    """A running row, shaped like the row the slot table stores."""
    row: Dict[str, Any] = {
        "plan_id": plan_id,
        "acquired_at": acquired_at,
        "workspace": "/ws",
    }
    if phase is not None:
        row["phase"] = phase
    return row


def _state(
    queue: List[Dict[str, Any]], running: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """The state of one tick, as the engine hands it to the snapshot."""
    return {
        "capacity": 4,
        "used": len(running),
        "now": 1000.0,
        "min_runtime_seconds": 600,
        "queue": queue,
        "running": running,
    }


# ---------------------------------------------------------------------------
# The three shapes
# ---------------------------------------------------------------------------


def test_queue_row_keys_include_phase() -> None:
    """A queued row carries exactly the eight keys the contract declares.

    The phase is one of them.  It is the field that says what the plan
    is doing, and a panel that cannot read it draws "waiting" for a
    plan that is waiting to be repaired and for one that is waiting to
    be verified — the same word for two different things.

    The assertion is exact equality: a row that grew a field the
    contract does not declare is a row the panel cannot rely on, and a
    subset assertion would let exactly that through.
    """
    state = _state([_queue_row("p1", 1, phase="repair")], [])

    result = build(state)

    assert set(result["queue"][0]) == QUEUE_ROW_KEYS, sorted(result["queue"][0])


def test_running_row_keys_include_phase() -> None:
    """A running row carries exactly the eight keys the contract declares.

    The running line is where the phase matters most: "running" is a
    fact about the slot, and the phase is a fact about the plan, and
    the two are not the same fact.
    """
    state = _state([], [_running_row("r1", 400.0, phase="executing")])

    result = build(state)

    assert set(result["running"][0]) == RUNNING_ROW_KEYS, sorted(result["running"][0])


def test_top_level_keys_unchanged() -> None:
    """The document still carries exactly its six keys.

    The phase was added to the **rows**, and the document is not a row:
    a reader that finds a seventh key at the top level is reading a
    document whose shape has moved, and every reader that was written
    against the six keys is now reading a document it was not written
    for.
    """
    result = build(_state([_queue_row("p1", 1)], [_running_row("r1", 400.0)]))

    assert set(result) == TOP_LEVEL_KEYS, sorted(result)