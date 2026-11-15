"""The snapshot's running row carries the priority the plan runs at.

Background
----------
The scheduler's read model has to answer one question about the running
plans that an operator cannot answer by looking at the panel: **who
goes first when a slot has to be given up**.  The eviction rule
compares the running plans by the **priority** the caller gave them —
the plan that stands last in the line goes first — and the queue rows
have always carried that number, so an operator could see the order of
the plans that were *waiting*.

The running rows did not.  The engine's own running view carries the
priority (the slot holds it, and the engine passes it on), but the
snapshot's running row dropped it on the way: it read the plan id, the
slot's moment, the two clocks, the eviction flag and the workspace —
and nothing else.  So the number that decides *who is squeezed* was
present in the state and absent from the one document everybody reads.

What is pinned here
-------------------
* a running row carries the priority the state holds, unchanged — a
  plan that stands early in the line (a negative priority, the caller
  asking for the plan to go first) keeps that priority here;
* a row that carries no usable priority reads as ``0`` — the ordinary
  weight, the one every plan without a stated priority stands at.

The number is read, not invented: the snapshot reports what the state
holds and never makes a plan's weight up.

The other fields are untouched: the elapsed time is still measured
against the slot's moment, because that is what the eviction rule
reads.
"""

from __future__ import annotations

from plan_scheduler.snapshot import build


#: The tick's own moment — 2026-11-01T10:00:00Z, the moment the
#: running rows below were taken at.
_NOW = 1_793_527_200.0

#: The runtime a plan is protected for, in seconds.
_MIN_RUNTIME = 600


def _state(running) -> dict:
    """The state of one tick, as the engine hands it to the snapshot."""
    return {
        "capacity": 4,
        "used": len(running),
        "now": _NOW,
        "min_runtime_seconds": _MIN_RUNTIME,
        "queue": [],
        "running": running,
    }


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_running_entry_exposes_priority() -> None:
    """A running row carries the priority the state holds.

    The number is the one the caller gave the plan, and the one the
    store wrote onto the slot when the plan was admitted.  It is
    passed on as it is: the snapshot does not make it up, does not
    change its sign, and does not read it from anywhere else.

    A plan that stands early in the line — a negative priority, the
    caller asking for the plan to go first — keeps that priority here,
    so an operator can see who will be squeezed first.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "bug",
                    "priority": -10,
                    "acquired_at": "2026-11-01T10:00:00Z",
                }
            ]
        )
    )

    assert len(result["running"]) == 1
    assert result["running"][0]["priority"] == -10


def test_running_entry_defaults_priority_to_zero() -> None:
    """A row without a usable priority reads as ``0``.

    Three ways for the number not to be there, and all three land on
    the same place: the ordinary weight — the one every plan without a
    stated priority stands at.

    * the row carries no ``priority`` at all — a slot written before
      the column existed;
    * the row's priority is ``NULL``;
    * the row's priority is not a number the snapshot can read.

    None of them may raise: the panel has to draw the row either way,
    and a read model that failed on a missing field would be a read
    model that fails on an ordinary scheduler.

    The other fields keep their behaviour: the elapsed time is still
    measured against the slot's moment.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "no-field",
                    "acquired_at": _NOW - 10,
                },
                {
                    "plan_id": "null-priority",
                    "acquired_at": _NOW - 20,
                    "priority": None,
                },
                {
                    "plan_id": "not-a-number",
                    "acquired_at": _NOW - 30,
                    "priority": "not a number",
                },
            ]
        )
    )

    weights = {entry["plan_id"]: entry["priority"] for entry in result["running"]}
    assert weights == {
        "no-field": 0,
        "null-priority": 0,
        "not-a-number": 0,
    }

    # The rest of the row is unchanged: the slot clock still reads the
    # time since the slot was taken.
    by_id = {entry["plan_id"]: entry for entry in result["running"]}
    assert by_id["no-field"]["elapsed_sec"] == 10
    assert by_id["null-priority"]["elapsed_sec"] == 20
    assert by_id["not-a-number"]["elapsed_sec"] == 30
