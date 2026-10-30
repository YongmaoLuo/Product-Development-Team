"""Unit tests: the scheduler's policy — three pure functions, one vocabulary.

What this file holds down
-------------------------
The scheduler asks three questions over and over: **may this plan start
now**, **who gets interrupted when a slot has to be freed**, and **what
has to go when more plans are running than there are slots**.  The
tick, the operator panel and the dry run all ask the same three
questions, so they all have to get the same three answers — a panel
that explains one thing while the engine does another is worse than no
panel at all.

The contracts this file pins:

1. **The M boundary is inclusive.**  A plan that has been running for
   ``M - 1`` seconds is not a candidate; ``M`` and ``M + 1`` are.  The
   default 600 seconds of protection is exactly that long — off by one
   and the protection is either void or a stall.
2. **The victim is the youngest eligible plan.**  The plan that was
   created most recently is the one that goes, not the one closest to
   finishing.  A running entry that carries no creation time is judged
   by the moment it took its slot — the rule the policy had before the
   creation times were known.  (The creation-time ordering itself is
   pinned in ``test_victim_creation_age.py``.)
3. **No candidate is an answer, not a crash.**  An empty candidate set
   gives ``None``.
4. **Convergence is bounded.**  The scheduler stops as soon as it fits
   inside the capacity, and it stops when there is nobody left to
   evict — it never spins.
5. **Every reason is one of the eight.**  The vocabulary is closed: the
   panel renders it, the dry run records it, the API publishes it, and
   none of them composes a sentence of its own.

The tests assert **the decision the operator sees** — the reason, the
plan id, the order of the victims — and not the shape of the snapshot
that produced it.  The snapshot is the caller's business; the decision
is the policy's.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plan_scheduler import policy
from plan_scheduler.policy import REASONS, admission_verdict, converge, select_victim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queued(plan_id: str = "p1", **flags) -> dict:
    """A queued plan, shaped like the entry the tick assembles."""
    entry = {"plan_id": plan_id, "due": True, "workspace_busy": False}
    entry.update(flags)
    return entry


def _running(plan_id: str, elapsed: float, acquired_at: float) -> dict:
    """A running plan: how long it has run, and when it took its slot.

    The entry carries no creation time, so the policy falls back to the
    slot time — which is what these cases are about.
    """
    return {"plan_id": plan_id, "elapsed": elapsed, "acquired_at": acquired_at}


def _snapshot(
    queued=None,
    used: int = 0,
    capacity: int = 4,
    running=None,
    allow_evict: bool = False,
    **extra,
) -> dict:
    """A snapshot, shaped like the one the tick hands to the policy."""
    snapshot = {
        "queued": list(queued) if queued is not None else [],
        "used": used,
        "capacity": capacity,
        "running": list(running) if running is not None else [],
        "allow_evict": allow_evict,
    }
    snapshot.update(extra)
    return snapshot


#: The truth table of the admission predicates — one reachable
#: combination per reason.  ``(label, snapshot, expected reason)``.
_ADMISSION_CASES = [
    (
        "nothing-queued",
        _snapshot(),
        "running",
    ),
    (
        "paused",
        _snapshot(queued=[_queued(paused=True)]),
        "paused",
    ),
    (
        "hitl-pending",
        _snapshot(queued=[_queued(hitl_pending=True)]),
        "hitl_pending",
    ),
    (
        "not-due-yet",
        _snapshot(queued=[_queued(due=False)]),
        "due_later",
    ),
    (
        "workspace-taken",
        _snapshot(queued=[_queued(workspace_busy=True)]),
        "workspace_busy",
    ),
    (
        "over-capacity",
        _snapshot(queued=[_queued()], used=5, capacity=4),
        "over_capacity",
    ),
    (
        "free-slot",
        _snapshot(queued=[_queued()], used=1, capacity=4),
        "running",
    ),
    (
        "full-and-no-eviction",
        _snapshot(queued=[_queued()], used=4, capacity=4),
        "waiting_for_slot",
    ),
    (
        "full-and-nobody-to-evict",
        _snapshot(
            queued=[_queued()],
            used=4,
            capacity=4,
            allow_evict=True,
            running=[_running("r1", 10, 100)],
        ),
        "blocked_by_capacity",
    ),
    (
        "full-and-somebody-to-evict",
        _snapshot(
            queued=[_queued()],
            used=4,
            capacity=4,
            allow_evict=True,
            running=[_running("r1", 600, 100)],
        ),
        "running",
    ),
]


# ---------------------------------------------------------------------------
# 1. The M boundary is inclusive
# ---------------------------------------------------------------------------


def test_eviction_threshold_boundaries() -> None:
    """``M - 1`` is not a candidate; ``M`` and ``M + 1`` are."""
    assert select_victim(_snapshot(running=[_running("r1", 599, 100)]), 600) is None
    assert select_victim(_snapshot(running=[_running("r1", 600, 100)]), 600) == "r1"
    assert select_victim(_snapshot(running=[_running("r1", 601, 100)]), 600) == "r1"


def test_threshold_applies_to_every_candidate() -> None:
    """A young plan is skipped even when an old one is there beside it."""
    snapshot = _snapshot(
        running=[_running("young", 10, 100), _running("old", 700, 200)]
    )

    assert select_victim(snapshot, 600) == "old"


# ---------------------------------------------------------------------------
# 2. The victim is the youngest eligible plan
# ---------------------------------------------------------------------------


def test_victim_is_youngest_eligible() -> None:
    """The plan that took its slot most recently is the one that goes."""
    snapshot = _snapshot(
        running=[
            _running("older", 900, 100),
            _running("younger", 900, 200),
        ]
    )

    assert select_victim(snapshot, 600) == "younger"


def test_tie_is_broken_by_plan_id() -> None:
    """Two plans that took their slot at the same moment are told apart by name."""
    snapshot = _snapshot(
        running=[
            _running("beta", 900, 100),
            _running("alpha", 900, 100),
        ]
    )

    assert select_victim(snapshot, 600) == "alpha"


def test_the_youngest_wins_whatever_order_the_entries_came_in() -> None:
    """The victim's identity does not depend on the order of the running set."""
    youngest_first = _snapshot(
        running=[_running("younger", 900, 200), _running("older", 900, 100)]
    )
    oldest_first = _snapshot(
        running=[_running("older", 900, 100), _running("younger", 900, 200)]
    )

    assert select_victim(youngest_first, 600) == "younger"
    assert select_victim(oldest_first, 600) == "younger"


# ---------------------------------------------------------------------------
# 3. No candidate is an answer, not a crash
# ---------------------------------------------------------------------------


def test_no_victim_returns_none() -> None:
    """Nobody is running — there is nobody to interrupt."""
    assert select_victim(_snapshot(running=[]), 600) is None


def test_no_victim_when_everyone_is_too_young() -> None:
    """Everybody is still inside the protected window."""
    snapshot = _snapshot(running=[_running("r1", 10, 100), _running("r2", 20, 200)])

    assert select_victim(snapshot, 600) is None


# ---------------------------------------------------------------------------
# 4. Convergence is bounded
# ---------------------------------------------------------------------------


def test_converge_stops_without_victim(monkeypatch) -> None:
    """Six running plans, none of them old enough: nothing is evicted.

    The counter below counts how many rounds the convergence asked for
    a victim; it delegates to the real function, so no behaviour is
    faked — it only makes the round count observable.
    """
    rounds: list = []
    real_select_victim = policy.select_victim

    def counting_select_victim(snapshot, min_runtime_seconds):
        rounds.append(min_runtime_seconds)
        return real_select_victim(snapshot, min_runtime_seconds)

    monkeypatch.setattr(policy, "select_victim", counting_select_victim)

    snapshot = _snapshot(
        used=6,
        capacity=4,
        running=[_running(f"r{index}", 10, 100 + index) for index in range(6)],
    )

    assert converge(snapshot, 4) == []
    assert len(rounds) <= snapshot["used"]


def test_converge_converges_six_to_four() -> None:
    """Six running plans and four slots: the two youngest are evicted."""
    snapshot = _snapshot(
        used=6,
        capacity=4,
        running=[_running(f"r{index}", 900, 100 + index) for index in range(6)],
    )

    assert converge(snapshot, 4) == ["r5", "r4"]


def test_converge_stops_as_soon_as_it_fits() -> None:
    """A scheduler that already fits evicts nobody."""
    snapshot = _snapshot(
        used=4,
        capacity=4,
        running=[_running(f"r{index}", 900, 100 + index) for index in range(4)],
    )

    assert converge(snapshot, 4) == []


def test_converge_never_exceeds_the_running_set() -> None:
    """The victims are a subset of the running plans, each one of them once."""
    snapshot = _snapshot(
        used=6,
        capacity=1,
        running=[_running(f"r{index}", 900, 100 + index) for index in range(6)],
    )

    victims = converge(snapshot, 1)

    assert len(victims) == len(set(victims))
    assert set(victims) <= {entry["plan_id"] for entry in snapshot["running"]}


def test_converge_with_a_capacity_of_zero_empties_the_running_set() -> None:
    """A capacity of zero is a coherent answer: nothing may run."""
    snapshot = _snapshot(
        used=3,
        capacity=0,
        running=[_running(f"r{index}", 900, 100 + index) for index in range(3)],
    )

    assert converge(snapshot, 0) == ["r2", "r1", "r0"]


# ---------------------------------------------------------------------------
# 5. Every reason is one of the eight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,snapshot,expected",
    _ADMISSION_CASES,
    ids=[case[0] for case in _ADMISSION_CASES],
)
def test_reason_in_closed_enum(label: str, snapshot: dict, expected: str) -> None:
    """Every branch answers with one of the eight reasons — and with the right one."""
    verdict = admission_verdict(snapshot)

    assert verdict in REASONS
    assert verdict == expected


def test_reasons_are_the_documented_eight() -> None:
    """The vocabulary is exactly the eight words the panel and the API share."""
    assert REASONS == {
        "running",
        "waiting_for_slot",
        "blocked_by_capacity",
        "workspace_busy",
        "due_later",
        "hitl_pending",
        "paused",
        "over_capacity",
    }


def test_admission_uses_the_snapshot_threshold() -> None:
    """The snapshot's own ``M`` decides who may be evicted, not a fixed constant."""
    snapshot = _snapshot(
        queued=[_queued()],
        used=4,
        capacity=4,
        allow_evict=True,
        min_runtime_seconds=100,
        running=[_running("r1", 150, 100)],
    )

    assert admission_verdict(snapshot) == "running"


def test_admission_reads_the_head_of_the_queue() -> None:
    """The head of the queue is the plan the next admission is about."""
    snapshot = _snapshot(
        queued=[_queued("head", workspace_busy=True), _queued("second")],
        used=1,
        capacity=4,
    )

    assert admission_verdict(snapshot) == "workspace_busy"


def test_policy_does_no_io() -> None:
    """The policy is a pure function: it reads the snapshot and nothing else."""
    snapshot = _snapshot(queued=[_queued()], used=1, capacity=4)

    verdict = admission_verdict(snapshot)

    # The snapshot is untouched by the decision — the policy reads it,
    # it never rewrites the caller's data.
    assert snapshot["used"] == 1
    assert verdict == "running"


# ---------------------------------------------------------------------------
# Rows the policy cannot read
# ---------------------------------------------------------------------------


def test_a_row_that_is_not_a_mapping_is_read_by_attribute() -> None:
    """A snapshot may carry rows that are objects rather than mappings."""
    snapshot = {
        "queued": [],
        "used": 1,
        "capacity": 4,
        "running": [SimpleNamespace(plan_id="r1", elapsed=600, acquired_at=100)],
    }

    assert select_victim(snapshot, 600) == "r1"


def test_a_duration_that_cannot_be_read_protects_the_plan() -> None:
    """A duration the policy cannot read is treated as "just started"."""
    snapshot = _snapshot(
        running=[{"plan_id": "r1", "elapsed": "not a number", "acquired_at": 100}]
    )

    assert select_victim(snapshot, 600) is None


def test_a_flag_is_not_a_duration() -> None:
    """``True`` is a number in Python, but a flag is not a duration."""
    snapshot = _snapshot(running=[{"plan_id": "r1", "elapsed": True, "acquired_at": 100}])

    assert select_victim(snapshot, 600) is None


def test_a_snapshot_whose_queue_cannot_be_read_says_nothing() -> None:
    """A snapshot whose queue is not a list is a snapshot with no queue."""
    snapshot = {
        "queued": 5,
        "used": 0,
        "capacity": 4,
        "running": [],
        "allow_evict": False,
    }

    assert admission_verdict(snapshot) == "running"


def test_a_snapshot_whose_running_set_cannot_be_read_has_nobody_running() -> None:
    """A snapshot whose running set is not a list leaves nobody to interrupt."""
    snapshot = {
        "queued": [_queued()],
        "used": 4,
        "capacity": 4,
        "running": 5,
        "allow_evict": True,
    }

    assert admission_verdict(snapshot) == "blocked_by_capacity"