"""The running view carries the priority the plan runs at.

Background
----------
The eviction rule reads the running plans and compares them by the
**priority** the caller gave them — the plan that stands last in the
line goes first.  The policy has always been able to read a priority
off a running entry (``policy._priority()``), but the engine's
``_running_view()`` assembled each running row out of the plan id, the
slot's moment, the elapsed time, the workspace and the start time —
and dropped the priority on the way.  So on the engine's own path
(a tick that finds more plans running than there are slots) the two
rungs of the comparison were missing: every plan looked like it carried
the ordinary weight, and the runtime alone decided.

The number is not invented here: the **slot** holds it.  The store
writes the priority the plan was admitted with onto the slot row, and
the slot row is what the running view is built from.

These cases pin the last hop:

* a running row carries the priority the slot holds, unchanged — a
  plan that stands early in the line keeps its place;
* a slot that carries no priority, or a plan with no slot row at all,
  reads as ``0`` — the ordinary weight, the one every plan without a
  stated priority stands at.

The other fields are untouched: the elapsed time is still measured
against the slot's moment, because that is what the eviction rule
reads.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterator, List

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Facts:
    """A facts reader that says which plans are working."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def active_plans(self) -> List[Dict[str, Any]]:
        """The plans that hold compute, as the facts report them."""
        return list(self._rows)


@pytest.fixture()
def engine() -> Iterator[Any]:
    """An engine over a facts reader that says nothing is working.

    The collaborators the running view does not read — the store, the
    ledger, the command executor — are placeholders: the running view
    is a pure reading of the facts and the slots, and a case that
    needed them would be testing something else.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    connection = sqlite3.connect(":memory:")
    try:
        yield PlanSchedulerEngine(
            config_loader=object(),
            store=object(),
            facts=_Facts([]),
            ledger=object(),
            commands=object(),
            sink=object(),
            conn=connection,
        )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_running_view_exposes_the_slot_priority(engine: Any) -> None:
    """A running row carries the priority the slot holds.

    The number is the one the caller gave the plan, and the one the
    store wrote onto the slot when the plan was admitted.  It is
    passed on as it is: the engine does not make it up, does not
    change its sign, and does not read it from anywhere else.

    A plan that stands early in the line — a negative priority, the
    caller asking for the plan to go first — keeps that priority here,
    so the eviction rule can compare the plans by it.
    """
    engine._facts = _Facts(
        [
            {
                "plan_id": "bug",
                "side": "execution",
                "workspace": "/tmp/bug",
                "started_at": "2026-11-01T09:00:00",
            }
        ]
    )

    running = engine._running_view(
        [
            {
                "plan_id": "bug",
                "acquired_at": "2026-11-01T10:00:00",
                "updated_at": "2026-11-01T10:00:00",
                "priority": -10,
            }
        ],
        1793307612.0,
    )

    assert len(running) == 1
    assert running[0]["priority"] == -10


def test_running_view_defaults_priority_to_zero(engine: Any) -> None:
    """A slot with no priority, and a plan with no slot, read as ``0``.

    Three ways for the number not to be there, and all three land on
    the same place: the ordinary weight — the one every plan without a
    stated priority stands at.

    * the slot row has no ``priority`` column at all — a row written
      before the column existed;
    * the slot row's priority is ``NULL``;
    * the plan is working but has no slot row — the running view is
      built from the facts, and a plan the facts report without a slot
      still gets a row, so it must get a weight too.

    The other fields keep their behaviour: the plan without a slot
    still gets the moment ``now``, and the elapsed time is still
    measured against the slot's moment.
    """
    engine._facts = _Facts(
        [
            {
                "plan_id": "no-column",
                "side": "execution",
                "workspace": None,
                "started_at": None,
            },
            {
                "plan_id": "null-priority",
                "side": "execution",
                "workspace": None,
                "started_at": None,
            },
            {
                "plan_id": "no-slot",
                "side": "execution",
                "workspace": None,
                "started_at": None,
            },
        ]
    )

    running = engine._running_view(
        [
            {
                "plan_id": "no-column",
                "acquired_at": "2026-10-29T21:00:00",
            },
            {
                "plan_id": "null-priority",
                "acquired_at": "2026-10-29T21:00:00",
                "priority": None,
            },
        ],
        1793307612.0,
    )

    weights = {entry["plan_id"]: entry["priority"] for entry in running}
    assert weights == {
        "no-column": 0,
        "null-priority": 0,
        "no-slot": 0,
    }

    # The rest of the row is unchanged: the plan without a slot is
    # dated by ``now``, and the ones with a slot by the slot's moment.
    by_id = {entry["plan_id"]: entry for entry in running}
    assert by_id["no-slot"]["acquired_at"] == 1793307612.0
    assert by_id["no-slot"]["elapsed"] == 0.0
    assert by_id["no-column"]["acquired_at"] == 1793307600.0
    assert by_id["no-column"]["elapsed"] == 12.0
