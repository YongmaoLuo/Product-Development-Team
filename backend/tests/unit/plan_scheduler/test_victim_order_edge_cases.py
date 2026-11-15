"""Unit tests: the **order** of the victims — where the other files stop.

What this file holds down
-------------------------
Two files already pin the victim rule itself: ``test_policy.py`` pins
the three questions the scheduler asks, and ``test_victim_priority_order.py``
pins the two stages of the order — the priority first, the runtime
second.  Neither of them looks at the **order in which a convergence
interrupts the plans**, and neither of them looks at a tie that
involves more than two plans.

The contracts this file pins:

1. **A convergence is ordered.**  When the scheduler has to interrupt
   more than one plan, the plan ids come back in the order they are to
   be interrupted — the one that stands last in the line first, then the
   next one down.  The order is part of the decision the operator sees.
2. **The priority beats the runtime in that order too.**  A plan that
   has run for five seconds stands behind a plan that has run for a
   hundred when the first carries the larger priority number, so the
   short run is the one that is interrupted first.
3. **A tie of three breaks on the name.**  When three plans share both
   the priority and the runtime, the smallest ``plan_id`` in dictionary
   order is the first one to go — and the answer does not depend on the
   order the entries happened to arrive in.

The protected window is not what this file is about — that is pinned in
``test_policy.py`` — so every plan in these cases is a candidate and
the snapshot says so outright.

The tests assert **the decision the operator sees** — the plan ids, in
the order they are to be interrupted — and not the shape of the
snapshot that produced it.
"""

from __future__ import annotations

import pytest

from plan_scheduler.policy import converge, select_victim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _running(plan_id: str, elapsed: float, priority: int) -> dict:
    """A running plan: how long it has run, and the weight the caller gave it."""
    return {"plan_id": plan_id, "elapsed": elapsed, "priority": priority}


def _snapshot(running, used: int, capacity: int) -> dict:
    """A snapshot, shaped like the one the tick hands to the policy.

    The protected window is ``0`` — every running plan is a candidate —
    because what these cases are about is the order the candidates are
    taken in, not whether they may be taken at all.
    """
    return {
        "running": list(running),
        "used": used,
        "capacity": capacity,
        "min_runtime_seconds": 0,
    }


# ---------------------------------------------------------------------------
# 1. A convergence is ordered
# ---------------------------------------------------------------------------


def test_converge_evicts_priority_first() -> None:
    """The plan that stands later in the line is interrupted first.

    ``a`` stands last in the line (``priority = 5``) and goes first.
    ``b`` stands behind ``c`` and ``d`` but ahead of nothing — its
    priority of ``1`` is larger than their ``0`` — so it goes second,
    even though it has run for five seconds where ``c`` has run for a
    hundred.  The runtime never gets a say across two priorities.
    """
    running = [
        _running("a", 10, 5),
        _running("b", 5, 1),
        _running("c", 100, 0),
        _running("d", 20, 0),
    ]

    assert converge(_snapshot(running, used=4, capacity=2), 2) == ["a", "b"]


def test_converge_evicts_priority_first_even_though_the_names_and_the_runtime_say_otherwise() -> None:
    """Two controls: the order is the priority's, and neither the names' nor the runtime's.

    With every plan carrying the same weight, the very same entries are
    interrupted in the opposite order — the shortest run first.  And with
    the names the other way round, the same two plans are interrupted.
    So the order the previous case pins is the priority's doing and not
    the runtime's nor the names'.
    """
    running = [
        _running("a", 10, 5),
        _running("b", 5, 1),
        _running("c", 100, 0),
        _running("d", 20, 0),
    ]
    flattened = [_running(entry["plan_id"], entry["elapsed"], 0) for entry in running]
    renamed = [
        _running("z", 10, 5),
        _running("y", 5, 1),
        _running("x", 100, 0),
        _running("w", 20, 0),
    ]

    assert converge(_snapshot(flattened, used=4, capacity=2), 2) == ["b", "a"]
    assert converge(_snapshot(renamed, used=4, capacity=2), 2) == ["z", "y"]


# ---------------------------------------------------------------------------
# 2. A tie of three breaks on the name
# ---------------------------------------------------------------------------


def test_three_way_tie_breaks_on_plan_id() -> None:
    """Three plans of the same priority and the same runtime: the smallest name goes.

    ``gamma``, ``alpha`` and ``beta`` share both the priority and the
    runtime, so the name is the last word: ``alpha`` is the first one to
    go — whether the policy is asked for a single victim or for the
    order of a whole convergence, and whichever order the entries
    happened to arrive in.
    """
    running = [
        _running("gamma", 300, 2),
        _running("alpha", 300, 2),
        _running("beta", 300, 2),
    ]
    reversed_running = list(reversed(running))

    assert select_victim(_snapshot(running, used=3, capacity=3), 0) == "alpha"
    assert select_victim(_snapshot(reversed_running, used=3, capacity=3), 0) == "alpha"
    assert converge(_snapshot(running, used=3, capacity=2), 2) == ["alpha"]
    assert converge(_snapshot(reversed_running, used=3, capacity=2), 2) == ["alpha"]
