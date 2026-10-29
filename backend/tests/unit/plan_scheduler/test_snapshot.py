"""Unit tests: the scheduler's single read model.

The panel, the LUI and the API all read **one** snapshot — the same
numbers, the same order, the same reasons.  If the panel sorted the
queue on its own, the operator would see one order while the scheduler
ran another, and the only way to tell the two apart would be to watch
plans start in the wrong order.

So the read model is assembled in exactly one place, and this file holds
down the four things that make it trustworthy:

1. **The order is the production one.**  The queue comes out sorted by
   :func:`plan_scheduler.queue.queue_key` — the function the admission
   check itself sorts by — and the positions are numbered from one.
2. **The eviction flag is the threshold.**  ``evictable`` is true
   exactly when the plan has been running for at least ``M`` seconds.
3. **The capacity flag is the count.**  ``over_capacity`` is true
   exactly when more plans hold a slot than the scheduler has slots.
4. **The shape is closed.**  The keys are fixed, the lists are lists and
   never ``null``, and every reason is one of the policy's own words.

The tests assert **the fields the panel reads**, not the whole
document.  A whole-document golden file would break every time the
clock moved, and the clock moves all the time — the contract is the
*shape* and the *semantics*, and the shape is asserted key by key.
"""

from __future__ import annotations

from datetime import datetime

from plan_scheduler.policy import REASONS
from plan_scheduler.queue import queue_key
from plan_scheduler.snapshot import build


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queue_row(
    plan_id: str,
    seq: int,
    priority: int = 0,
    promote_order=None,
    reason: str = "waiting_for_slot",
) -> dict:
    """A queue row, shaped like the row the queue table stores."""
    return {
        "plan_id": plan_id,
        "seq": seq,
        "priority": priority,
        "promote_order": promote_order,
        "reason": reason,
    }


def _running_row(
    plan_id: str, acquired_at: float, workspace: str = "/ws"
) -> dict:
    """A running row, shaped like the row the slot table stores."""
    return {
        "plan_id": plan_id,
        "acquired_at": acquired_at,
        "workspace": workspace,
    }


def _state(**overrides) -> dict:
    """The state of one tick, as the engine hands it to the snapshot."""
    state = {
        "capacity": 4,
        "used": 5,
        "now": 1000.0,
        "min_runtime_seconds": 600,
        "queue": [_queue_row("p1", 1)],
        "running": [_running_row("r1", 400.0)],
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# 1. The order is the production one
# ---------------------------------------------------------------------------


def test_queue_sorted_by_production_key() -> None:
    """The queue comes out in the order the production comparator gives.

    The comparison is against :func:`queue_key` itself — the function
    the admission check sorts by — and not against a copy of it written
    out here: a copy would keep passing after the real comparator
    changed, which is exactly the divergence this snapshot exists to
    prevent.
    """
    rows = [
        _queue_row("third", 3),
        _queue_row("promoted", 9, promote_order=-1),
        _queue_row("first", 1),
        _queue_row("second", 2, priority=1),
    ]

    result = build(_state(queue=rows))

    expected = [row["seq"] for row in sorted(rows, key=queue_key)]
    assert [item["seq"] for item in result["queue"]] == expected
    # The promoted plan runs first, and the untouched queue behind it
    # is a plain FIFO.
    assert expected == [9, 1, 3, 2]


def test_positions_are_numbered_from_one() -> None:
    """The position of a queued plan is its place in the line, counting from one."""
    rows = [_queue_row("c", 3), _queue_row("a", 1), _queue_row("b", 2)]

    result = build(_state(queue=rows))

    assert [item["position"] for item in result["queue"]] == [1, 2, 3]


def test_queue_length_counts_the_queue() -> None:
    """The queue's length is the number of plans waiting for a slot."""
    result = build(_state(queue=[_queue_row("a", 1), _queue_row("b", 2)]))

    assert result["queue_length"] == 2
    assert result["queue_length"] == len(result["queue"])


# ---------------------------------------------------------------------------
# 2. The eviction flag is the threshold
# ---------------------------------------------------------------------------


def test_evictable_matches_threshold() -> None:
    """A plan is evictable exactly when it has run for at least ``M``.

    One second short of the threshold is not evictable; exactly at the
    threshold it is.  The rule is ``>=`` and not ``>``: the protection
    is ``M`` seconds long, and a rule that was off by one either voided
    the protection or stalled the scheduler for good.
    """
    running = [
        _running_row("young", 401.0),
        _running_row("old", 400.0),
    ]

    result = build(_state(now=1000.0, min_runtime_seconds=600, running=running))

    flags = {item["plan_id"]: item["evictable"] for item in result["running"]}
    assert flags == {"young": False, "old": True}


def test_evictable_follows_the_reported_elapsed() -> None:
    """The flag is the threshold applied to the elapsed time the panel is shown.

    The panel reads ``elapsed_sec`` and the flag beside it; the two must
    agree, or the operator is told a plan is evictable while the number
    next to it says otherwise.
    """
    running = [_running_row("r1", 400.0)]

    result = build(_state(now=1000.0, min_runtime_seconds=600, running=running))

    entry = result["running"][0]
    assert entry["elapsed_sec"] == 600
    assert entry["evictable"] == (entry["elapsed_sec"] >= 600)


def test_elapsed_is_measured_from_the_state_clock() -> None:
    """The elapsed time is measured against the moment the state carries.

    The state's ``now`` is the tick's moment, and the runtime is
    measured against it — not against the wall clock, which would make
    the number jump between two reads of the same state.
    """
    running = [_running_row("r1", 400.0)]

    result = build(_state(now=1000.0, running=running))

    assert result["running"][0]["elapsed_sec"] == 600


# ---------------------------------------------------------------------------
# 3. The capacity flag is the count
# ---------------------------------------------------------------------------


def test_over_capacity_matches_used() -> None:
    """More plans than slots is over capacity; exactly as many is not.

    The two are the same statement read twice: ``over_capacity`` is
    true exactly when ``used`` is greater than the total.  A snapshot
    that said otherwise would show the operator a normal-looking
    scheduler while the ledger was already over its limit.
    """
    over = build(_state(capacity=4, used=5))
    exact = build(_state(capacity=4, used=4))
    under = build(_state(capacity=4, used=3))

    assert over["over_capacity"] is True
    assert exact["over_capacity"] is False
    assert under["over_capacity"] is False


def test_capacity_reports_total_and_used() -> None:
    """The capacity block says how many slots there are and how many are taken."""
    result = build(_state(capacity=4, used=5))

    assert result["capacity"] == {"total": 4, "used": 5}


# ---------------------------------------------------------------------------
# 4. The shape is closed
# ---------------------------------------------------------------------------


def test_empty_lists_not_null() -> None:
    """Nothing queued and nothing running is two empty lists, not two nulls.

    ``null`` would be a second answer to "how many are waiting" — the
    panel would have to decide what an absent list means, and the LUI
    would have to decide it a second time.
    """
    result = build(_state(queue=[], running=[]))

    assert result["queue"] == []
    assert result["running"] == []
    assert result["queue_length"] == 0
    assert isinstance(result["queue"], list)
    assert isinstance(result["running"], list)


def test_key_set_is_closed() -> None:
    """The snapshot carries exactly the keys the contract declares.

    A field that appears without being declared is a field the panel
    cannot render and the LUI cannot rely on; the key set is closed so
    that adding one is a decision rather than an accident.
    """
    result = build(_state())

    assert set(result) == {
        "capacity",
        "queue_length",
        "over_capacity",
        "queue",
        "running",
        "generated_at",
    }
    assert set(result["capacity"]) == {"total", "used"}
    assert set(result["queue"][0]) == {
        "position",
        "seq",
        "priority",
        "promote_order",
        "reason",
    }
    assert set(result["running"][0]) == {
        "plan_id",
        "acquired_at",
        "elapsed_sec",
        "evictable",
        "workspace",
    }


def test_reason_comes_from_the_closed_vocabulary() -> None:
    """Every reason the panel renders is one of the policy's own words.

    The panel maps a reason to a piece of text and the LUI reports it;
    neither may show a word the scheduler never decided on.  A reason
    the policy does not know falls back to a word it does know, so the
    panel is never handed a label it cannot draw.
    """
    rows = [
        _queue_row("known", 1, reason="workspace_busy"),
        _queue_row("unknown", 2, reason="not_a_real_reason"),
    ]

    result = build(_state(queue=rows))

    reasons = [item["reason"] for item in result["queue"]]
    assert all(reason in REASONS for reason in reasons)
    # The reason the policy decided is the reason the panel is shown.
    assert result["queue"][0]["reason"] == "workspace_busy"


def test_generated_at_is_a_utc_stamp() -> None:
    """The moment the snapshot was made is written the way the database writes it."""
    result = build(_state())

    stamp = result["generated_at"]
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_a_state_with_nothing_in_it_still_builds() -> None:
    """A state that says nothing at all still produces a readable snapshot.

    The panel must not get an error because the scheduler had nothing
    to say: an empty scheduler is a normal thing to look at.
    """
    result = build({})

    assert result["queue"] == []
    assert result["running"] == []
    assert result["queue_length"] == 0
    assert result["over_capacity"] is False
    assert set(result) == {
        "capacity",
        "queue_length",
        "over_capacity",
        "queue",
        "running",
        "generated_at",
    }
