"""Unit tests: the queue's order — one comparator, shared by every reader.

Three places have to agree on which queued plan runs next: the
admission check that picks a plan to start, the panel that shows the
queue to the operator, and the recovery that runs after a restart.  If
each of them sorted on its own, the panel would show one order while
admission ran another.

So there is exactly one order, defined by one pure function of the
row.  What this file holds down:

1. **An unpromoted queue is a FIFO.** Plans that were never promoted
   run in the order they arrived.
2. **A promoted plan runs first.** Promotion moves a plan ahead of
   every plan that was never promoted.
3. **The later promotion wins.** Promotions are recorded as a
   decreasing counter, so the most recent promotion has the smallest
   number and runs first.
4. **The order is total.** No two rows ever share a key, so "who runs
   next" always has exactly one answer.

The tests assert **the order the rows come out in**, not the raw
values of the keys — the key is an implementation detail, the order is
the contract.  (A test that asserted ``key == (True, 0, 0, 7)`` would
fail the moment the key gained a field, without the queue's behaviour
changing at all.)
"""

from __future__ import annotations

import random

from plan_scheduler.queue import queue_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(plan_id: str, seq: int, promote_order=None, priority: int = 0) -> dict:
    """A queue row, shaped like the row the queue table stores."""
    return {
        "plan_id": plan_id,
        "seq": seq,
        "priority": priority,
        "promote_order": promote_order,
    }


def _order(rows: list) -> list:
    """Return the plan ids of ``rows`` in the order the queue runs them."""
    return [row["plan_id"] for row in sorted(rows, key=queue_key)]


# ---------------------------------------------------------------------------
# 1. An unpromoted queue is a FIFO
# ---------------------------------------------------------------------------


def test_unpromoted_sorts_by_seq() -> None:
    """Three plans that were never promoted run in the order they arrived."""
    rows = [
        _row("third", 3),
        _row("first", 1),
        _row("second", 2),
    ]

    assert _order(rows) == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# 2. A promoted plan runs first
# ---------------------------------------------------------------------------


def test_promoted_row_goes_first() -> None:
    """A promoted plan runs ahead of every plan that was never promoted."""
    rows = [
        _row("arrived-first", 1),
        _row("promoted", 9, promote_order=-1),
        _row("arrived-second", 2),
    ]

    assert _order(rows) == ["promoted", "arrived-first", "arrived-second"]


# ---------------------------------------------------------------------------
# 3. The later promotion wins
# ---------------------------------------------------------------------------


def test_two_promotions_reverse() -> None:
    """Promotions are recorded as a decreasing counter — the later one runs first."""
    rows = [
        _row("promoted-first", 4, promote_order=-1),
        _row("promoted-second", 8, promote_order=-2),
    ]

    assert _order(rows) == ["promoted-second", "promoted-first"]


# ---------------------------------------------------------------------------
# 4. The order is total
# ---------------------------------------------------------------------------


def test_random_total_order_no_ties() -> None:
    """Two hundred random rows never share a key — the order is total."""
    rng = random.Random(0)
    rows = [
        {
            "plan_id": f"plan-{index}",
            "seq": index,
            "priority": rng.choice([0, 1, 2]),
            "promote_order": rng.choice([None, -1, -2, -3]),
        }
        for index in range(200)
    ]

    keys = [queue_key(row) for row in rows]

    assert len(set(keys)) == len(keys)


def test_random_total_order_sorts_deterministically() -> None:
    """The same rows sort the same way every time — twice over."""
    rng = random.Random(0)
    rows = [
        {
            "plan_id": f"plan-{index}",
            "seq": index,
            "priority": rng.choice([0, 1, 2]),
            "promote_order": rng.choice([None, -1, -2, -3]),
        }
        for index in range(200)
    ]

    first = _order(rows)
    second = _order(list(reversed(rows)))

    assert first == second


# ---------------------------------------------------------------------------
# Boundaries of the key itself
# ---------------------------------------------------------------------------


def test_default_priority_is_zero() -> None:
    """A row that says nothing about its priority runs as if it were 0."""
    rows = [
        _row("negative", 1, priority=-1),
        {"plan_id": "unspecified", "seq": 2, "promote_order": None},
    ]

    # 0 sorts after -1, so the row whose priority was left out runs second.
    assert _order(rows) == ["negative", "unspecified"]


def test_seq_zero_and_large_int_compare() -> None:
    """``seq`` is compared as a number — 0 and a huge value both sort right."""
    rows = [
        _row("huge", 2**63),
        _row("zero", 0),
        _row("one", 1),
    ]

    assert _order(rows) == ["zero", "one", "huge"]
