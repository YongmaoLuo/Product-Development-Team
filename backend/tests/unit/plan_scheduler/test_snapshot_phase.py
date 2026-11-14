"""Unit tests: the snapshot's rows carry the plan's phase.

Background
----------
The scheduler's read model hands the panel one line per plan — one for
the plans that are waiting, one for the plans that are working.  Each
line said *which* plan it was about and, for the queued ones, *why*
the plan was waiting — but not *what* the plan was doing.  A panel
that says "running" without saying what is running draws the same
word for a plan that is executing and for one that is repairing.

The phase is already written down — it is the plan's own
``plan_routing.current_phase`` — and the facts reader reads it, and
the engine carries it on the row it writes into the state.  What the
snapshot did was drop it: the two views pick the fields they hand out
from a fixed list, and ``phase`` was not on it.  So the phase died in
the last hop, between the state and the document the panel renders.

These cases pin that last hop:

* a queued row carries the phase the state holds;
* a running row carries the phase the state holds;
* a row without a phase says ``None`` rather than raising — a state
  written before the phase existed is a normal thing to read.

The phase is passed on **as it is**.  The snapshot is the layer that
knows nothing about the phase vocabulary, and the label a human reads
is worked out further up; a second mapping here would be a second
answer to the same question.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from plan_scheduler.snapshot import build


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
# The phase travels to the panel
# ---------------------------------------------------------------------------


def test_queue_view_carries_phase() -> None:
    """A queued row carries the phase the state holds.

    The phase is the plan's own — the word the routing layer wrote —
    and it is passed on unchanged.  Without it the panel says
    "waiting" and the operator cannot tell whether the plan is waiting
    to be repaired or waiting to be verified.
    """
    state = _state([_queue_row("p1", 1, phase="repair")], [])

    result = build(state)

    assert result["queue"][0]["phase"] == "repair"


def test_running_view_carries_phase() -> None:
    """A running row carries the phase the state holds.

    The running line is where the phase matters most: "running" is a
    fact about the slot, and the phase is a fact about the plan, and
    the panel needs both to say what is actually happening.
    """
    state = _state([], [_running_row("r1", 400.0, phase="executing")])

    result = build(state)

    assert result["running"][0]["phase"] == "executing"


def test_missing_phase_is_none() -> None:
    """A row without a phase says ``None`` rather than raising.

    A state written before the phase existed is a normal thing to
    read — the snapshot answers "I don't know" and the caller decides
    what to do with that.  Raising here would take the whole read
    model down over a field that is allowed to be absent.
    """
    state = _state([_queue_row("p1", 1)], [_running_row("r1", 400.0)])

    result = build(state)

    assert result["queue"][0]["phase"] is None
    assert result["running"][0]["phase"] is None