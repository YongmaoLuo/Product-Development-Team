"""The running view carries the moment the plan really started.

Background
----------
The scheduler's "已运行(秒)" is a slot clock, not a clock:
``_recover_slots()`` deletes and re-inserts every slot on each
restart, so the number the operator sees starts again from zero
whenever the scheduler does — it says how long the *scheduler* has
held the slot, not how long the *plan* has been running.

The plan's real start time is already written down
(``plan_execution.started_at``) and ``facts.active_plans()`` already
reads it (see ``test_facts_active_started_at.py``).  What was missing
was the last hop: ``PlanSchedulerEngine._running_view()`` assembled
each running row out of the plan id, the slot's moment, the elapsed
time and the workspace — and dropped the start time on the way.

These cases pin that last hop:

* a running row carries the ``started_at`` the facts handed over,
  unchanged — the stamp is passed through, never parsed here;
* a facts row without a start time says ``None`` rather than raising
  — the engine has no opinion about a plan it cannot date.

The elapsed time is untouched: it is still measured against the
slot's moment, because that is what the eviction rule reads.
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


def test_running_view_passes_started_at(engine: Any) -> None:
    """A running row carries the start time the facts handed over.

    The stamp is passed through unchanged — the engine does not parse
    it and does not round it: what the facts said is what the row
    says, microseconds and all.  The elapsed time beside it is still
    the slot's clock, because that is what the eviction rule reads.
    """
    engine._facts = _Facts(
        [
            {
                "plan_id": "p-1",
                "side": "execution",
                "workspace": None,
                "started_at": "2026-10-29T20:04:26",
            }
        ]
    )

    running = engine._running_view(
        [{"plan_id": "p-1", "acquired_at": "2026-10-29T21:00:00"}],
        1793307612.0,
    )

    assert running == [
        {
            "plan_id": "p-1",
            "acquired_at": 1793307600.0,
            "elapsed": 12.0,
            "workspace": None,
            "started_at": "2026-10-29T20:04:26",
        }
    ]


def test_running_view_started_at_none(engine: Any) -> None:
    """A facts row without a start time says ``None``.

    A plan that cannot be dated is not an error — the engine answers
    "I don't know", and the caller decides what to do with that.
    Raising here would take the whole scheduler down over a field
    that is allowed to be missing.
    """
    engine._facts = _Facts(
        [
            {
                "plan_id": "p-1",
                "side": "verification",
                "workspace": None,
            }
        ]
    )

    running = engine._running_view(
        [{"plan_id": "p-1", "acquired_at": "2026-10-29T21:00:00"}],
        1793307612.0,
    )

    assert len(running) == 1
    assert running[0]["started_at"] is None
