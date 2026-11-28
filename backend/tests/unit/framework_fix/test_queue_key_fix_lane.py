"""Unit tests: the queue's order once the framework-fix lane is in it.

The queue's order is one pure function of the row —
:func:`plan_scheduler.queue.queue_key` — and every reader of the queue
sorts by it.  Before the framework-fix lane it read three columns:
``promote_order``, ``priority`` and ``seq``.

The lane adds a fourth thing to read: the **kind** of the plan.  A
framework-fix plan is a repair of the framework itself, and the lane's
decision point reserves the front of the line for it — a framework-fix
plan runs ahead of an ordinary plan no matter what the ordinary plan's
promotion, priority or arrival says.

So the key grew a first element: the **partition** the row belongs to.
It is a number, not a flag — ``FRAMEWORK_FIX_PRIORITY`` (0) for a
framework-fix plan and ``ORDINARY_MIN_PRIORITY`` (1) for an ordinary one
— and because it comes first, it decides the partition before anything
else gets a say.  Inside a partition the order is the one the queue
already had.

What this file holds down:

1. **A framework-fix plan runs first.**  It stands ahead of every
   ordinary plan, whatever the ordinary plan's promotion and priority
   are.
2. **A row that says nothing about its kind is an ordinary plan.**  The
   queue's rows do not all carry a kind, and a row without one may not
   be mistaken for a framework-fix plan.
3. **Inside a partition the order does not change.**  Priority and then
   arrival decide, and a promotion still moves a plan to the front of
   its own partition.
"""

from __future__ import annotations

from plan_scheduler.policy import FRAMEWORK_FIX_PRIORITY, ORDINARY_MIN_PRIORITY
from plan_scheduler.queue import queue_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    plan_id: str,
    seq: int,
    plan_kind: str = "normal",
    priority: int = 0,
    promote_order=None,
) -> dict:
    """A queue row, shaped like the row the queue table stores."""
    return {
        "plan_id": plan_id,
        "seq": seq,
        "priority": priority,
        "promote_order": promote_order,
        "plan_kind": plan_kind,
    }


def _order(rows: list) -> list:
    """Return the plan ids of ``rows`` in the order the queue runs them."""
    return [row["plan_id"] for row in sorted(rows, key=queue_key)]


# ---------------------------------------------------------------------------
# 1. A framework-fix plan runs first
# ---------------------------------------------------------------------------


def test_framework_first() -> None:
    """A framework-fix plan stands ahead of every ordinary plan.

    The ordinary plans here are ahead of it on every column the queue
    used to read: one arrived first, one was promoted, one has the
    better priority.  None of that may put it in front of a
    framework-fix plan — the partition decides that, and it decides it
    first.
    """
    rows = [
        _row("ordinary-arrived-first", 1),
        _row("ordinary-promoted", 2, promote_order=-1),
        _row("framework", 9, plan_kind="framework_fix"),
        _row("ordinary-prioritised", 3, priority=7),
    ]

    assert _order(rows) == [
        "framework",
        "ordinary-promoted",
        "ordinary-arrived-first",
        "ordinary-prioritised",
    ]


def test_framework_partition_bit() -> None:
    """The key's first element is the partition the row belongs to.

    The framework-fix partition is the lane's own level
    (:data:`FRAMEWORK_FIX_PRIORITY`) and the ordinary one is the level
    behind it (:data:`ORDINARY_MIN_PRIORITY`); the key says which a row
    is in before anything else about it is read.
    """
    framework = queue_key({"plan_kind": "framework_fix", "priority": 9, "seq": 5})
    ordinary = queue_key({"plan_kind": "normal", "priority": 9, "seq": 5})

    assert framework[0] == FRAMEWORK_FIX_PRIORITY
    assert ordinary[0] == ORDINARY_MIN_PRIORITY
    # The documented shape of the key: the partition, the promotion
    # slot, the priority and the arrival.
    assert framework == (FRAMEWORK_FIX_PRIORITY, 0, 9, 5)


# ---------------------------------------------------------------------------
# 2. A row that says nothing about its kind is an ordinary plan
# ---------------------------------------------------------------------------


def test_missing_kind_defaults() -> None:
    """A row without a ``plan_kind`` is an ordinary plan.

    The queue's rows do not all carry a kind, and one that does not may
    not be mistaken for a framework-fix plan: it waits behind the
    lane's own plans and is ordered among the ordinary ones by the
    columns the queue already read.
    """
    rows = [
        {"plan_id": "no-kind", "seq": 1, "priority": 0, "promote_order": None},
        _row("ordinary", 2),
        _row("framework", 9, plan_kind="framework_fix"),
    ]

    assert _order(rows) == ["framework", "no-kind", "ordinary"]
    assert queue_key(rows[0])[0] == ORDINARY_MIN_PRIORITY


# ---------------------------------------------------------------------------
# 3. Inside a partition the order does not change
# ---------------------------------------------------------------------------


def test_stable_within_partition() -> None:
    """Inside a partition, priority and then arrival decide.

    The partition only says which half of the queue a row is in; it
    does not flatten the order inside its own half.
    """
    rows = [
        _row("framework-late", 3, plan_kind="framework_fix", priority=5),
        _row("framework-early", 1, plan_kind="framework_fix", priority=5),
        _row("framework-high", 2, plan_kind="framework_fix", priority=1),
        _row("ordinary", 4),
    ]

    assert _order(rows) == [
        "framework-high",
        "framework-early",
        "framework-late",
        "ordinary",
    ]


def test_promotion_still_wins_inside_the_partition() -> None:
    """A promotion still moves a plan to the front of its partition.

    The lane reserves the front of the queue for its own plans; it does
    not take the promotion away from them.
    """
    rows = [
        _row("framework-arrived-first", 1, plan_kind="framework_fix"),
        _row("framework-promoted", 2, plan_kind="framework_fix", promote_order=-1),
    ]

    assert _order(rows) == ["framework-promoted", "framework-arrived-first"]
