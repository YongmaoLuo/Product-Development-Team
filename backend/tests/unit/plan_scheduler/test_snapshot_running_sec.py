"""Unit tests: the running row's two clocks.

The running row answers two different questions, and it has to answer
both of them:

* ``elapsed_sec`` is the **slot** clock — the time since the scheduler
  took the plan's slot.  It is measured against the tick, so it starts
  again from zero whenever the scheduler restarts, and it is the number
  the eviction rule is applied to.
* ``running_sec`` is the **plan** clock — the time since the plan
  itself started, measured from ``plan_execution.started_at``.

The two are not two readings of the same fact, and a reader that
confused them would be wrong in a way the operator cannot see.  A plan
that has been working for an hour but took its slot ten seconds ago
would read as "evictable" under the plan clock — and the protection
that keeps a plan alive for its first minutes is exactly the thing
that must not restart with the scheduler.

The stamp behind the plan clock
-------------------------------
``started_at`` is written by the execution route as
``datetime.now().isoformat()`` — a naive **local** time string — and
the very same string is what ``/api/system/active`` reports.  It is
read here as local time, because that is what the writer meant:
reading it as UTC would move every plan's start by exactly the offset
that was guessed, and on a machine that is not on UTC the number on
the panel would be wrong by that much — always, and in the same
direction.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from plan_scheduler.snapshot import build


#: A whole-second moment, so the arithmetic in these tests is exact.
_NOW = 1_700_000_000.0

#: The runtime a plan is protected for, in seconds.
_MIN_RUNTIME = 600


def _local_stamp(epoch: float) -> str:
    """The stamp the execution route writes, built the way it builds it.

    ``datetime.now().isoformat()`` is what puts ``started_at`` into
    ``plan_execution`` and into ``/api/system/active``: a naive
    **local** time string.  The tests build their stamps the same way,
    so a reader that read them as UTC is caught here rather than in
    production.
    """
    return datetime.fromtimestamp(epoch).isoformat()


def _state(running, now: float = _NOW, min_runtime: float = _MIN_RUNTIME) -> dict:
    """The state of one tick, as the engine hands it to the snapshot."""
    return {
        "capacity": 4,
        "used": len(running),
        "now": now,
        "min_runtime_seconds": min_runtime,
        "queue": [],
        "running": running,
    }


# ---------------------------------------------------------------------------
# The plan clock
# ---------------------------------------------------------------------------


def test_running_sec_uses_started_at() -> None:
    """``running_sec`` counts from the plan's own start, not from the slot.

    The slot was taken ten seconds ago; the plan has been working for
    an hour.  The two clocks answer different questions, and the row
    has to carry both of them: an operator asking "how long has this
    plan been at it?" is asking the second question, and the first one
    is the scheduler's business.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "p-1",
                    "acquired_at": _NOW - 10,
                    "started_at": _local_stamp(_NOW - 3600),
                    "workspace": "/ws",
                }
            ]
        )
    )

    row = result["running"][0]
    assert row["elapsed_sec"] == 10, (
        "the slot clock must stay the time since the slot was taken"
    )
    assert row["running_sec"] == pytest.approx(3600, abs=1), (
        "the plan clock must be the time since the plan started, not the "
        "time since the slot was acquired"
    )
    assert row["started_at"] == _local_stamp(_NOW - 3600), (
        "the stamp the plan carries is the stamp the row has to show"
    )


def test_started_at_is_read_as_local_time() -> None:
    """A naive stamp is local time — the zone the writer used.

    The stamp is built exactly as the execution route builds it, and
    the answer must be the moment it names.  A reader that assumed UTC
    would be wrong by the machine's offset: eight hours here, and zero
    on a machine that happens to sit on UTC — which is exactly why the
    expectation is written against the writer's own reading of the
    moment rather than against a number that would pass by accident.
    """
    stamp = _local_stamp(_NOW - 7200)
    assert datetime.fromisoformat(stamp).tzinfo is None, (
        "the stamp under test must be naive — a stamp that carries an "
        "offset says which zone it is in"
    )

    result = build(
        _state([{"plan_id": "p-1", "acquired_at": _NOW, "started_at": stamp}])
    )

    row = result["running"][0]
    assert row["running_sec"] == pytest.approx(7200, abs=1), (
        "the plan clock is off by the time zone that was guessed — a "
        "naive local stamp must be read as local time"
    )


def test_started_at_with_an_offset_is_read_with_that_offset() -> None:
    """A stamp that names its zone is read with that zone.

    The writer that produced this stamp said which zone it is in, so
    there is nothing to guess: the moment it names is the moment it
    means, whichever zone that is.
    """
    stamp = "2026-10-29T20:04:26.016813+08:00"
    expected = datetime.fromisoformat(stamp).timestamp()

    result = build(
        _state(
            [
                {
                    "plan_id": "p-1",
                    "acquired_at": _NOW,
                    "started_at": stamp,
                }
            ],
            now=expected + 120,
        )
    )

    row = result["running"][0]
    assert row["running_sec"] == pytest.approx(120, abs=1), (
        "a stamp that carries its own offset must be read with that "
        "offset, not localised a second time"
    )


def test_running_sec_none_without_started_at() -> None:
    """A row without a start time has no plan clock — and does not guess one.

    ``None`` is the honest answer, and the panel falls back to the slot
    clock for it.  A plan that started an unknown time ago has not run
    for zero seconds; it has run for an unknown length.
    """
    result = build(_state([{"plan_id": "p-1", "acquired_at": _NOW - 10}]))

    row = result["running"][0]
    assert row["running_sec"] is None, (
        "a row without a start time must not carry a made-up plan clock"
    )
    assert row["started_at"] is None
    assert row["elapsed_sec"] == 10, (
        "the slot clock is still there to fall back to"
    )


def test_running_sec_none_when_started_at_cannot_be_read() -> None:
    """A stamp that cannot be read is no stamp at all.

    The row must survive it: a plan whose start time is written in a
    shape nobody can parse is still a plan that is running, and the
    panel still has to draw it.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "p-1",
                    "acquired_at": _NOW - 10,
                    "started_at": "not a timestamp at all",
                }
            ]
        )
    )

    row = result["running"][0]
    assert row["running_sec"] is None
    assert row["started_at"] == "not a timestamp at all", (
        "the stamp is passed through as it was written — the snapshot "
        "does not rewrite what it cannot read"
    )
    assert row["elapsed_sec"] == 10


def test_running_sec_is_zero_when_the_plan_started_in_the_future() -> None:
    """A stamp from the future reads as zero, not as a negative duration.

    The clock can drift, and a plan whose start is stamped ahead of the
    tick has not run for a negative number of seconds.  Zero is the
    only answer that is not a lie.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "p-1",
                    "acquired_at": _NOW - 10,
                    "started_at": _local_stamp(_NOW + 600),
                }
            ]
        )
    )

    row = result["running"][0]
    assert row["running_sec"] == 0, (
        "a start stamp from the future must not produce a negative "
        "duration"
    )


# ---------------------------------------------------------------------------
# The two clocks do not pollute each other
# ---------------------------------------------------------------------------


def test_evictable_still_uses_slot_clock() -> None:
    """The eviction rule is applied to the slot clock, not to the plan clock.

    A plan that has been working for an hour but held its slot for ten
    seconds is **not** evictable: the protection is measured from the
    moment the slot was taken, so a scheduler restart cannot make a
    long-running plan immediately evictable.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "p-1",
                    "acquired_at": _NOW - 10,
                    "started_at": _local_stamp(_NOW - 3600),
                }
            ],
            min_runtime=_MIN_RUNTIME,
        )
    )

    row = result["running"][0]
    assert row["running_sec"] >= _MIN_RUNTIME, (
        "the plan clock is well past the threshold — that is what makes "
        "this case worth running"
    )
    assert row["elapsed_sec"] == 10
    assert row["evictable"] is False, (
        "the eviction rule must be applied to the slot clock: a plan is "
        "protected for a minimum runtime measured from the moment its "
        "slot was acquired"
    )


def test_evictable_is_true_when_the_slot_clock_says_so() -> None:
    """The other direction of the same rule: an old slot is evictable.

    The plan clock is not consulted at all here — the plan started a
    moment ago and is still not evictable, because the slot it holds
    has been held for longer than the protection.
    """
    result = build(
        _state(
            [
                {
                    "plan_id": "p-1",
                    "acquired_at": _NOW - 700,
                    "started_at": _local_stamp(_NOW - 5),
                }
            ],
            min_runtime=_MIN_RUNTIME,
        )
    )

    row = result["running"][0]
    assert row["elapsed_sec"] == 700
    assert row["running_sec"] == pytest.approx(5, abs=1)
    assert row["evictable"] is True, (
        "the eviction rule is applied to the slot clock, and the slot "
        "has been held for longer than the protection"
    )