"""The negative control: the old judgement was wrong on a real row.

Why this file exists
--------------------
``test_panel_and_api_parity`` used to compare the panel's drawing with
the read model's **slot** clock::

    assert abs(drawn_elapsed - int(carried["elapsed_sec"])) <= 2 + slack

The panel draws ``item.running_sec ?? item.elapsed_sec``
(``frontend/app.js:2223``) — the **plan** clock when the read model
carries one, and the slot clock only when it does not.  The old
comparison therefore asked the read model a question the panel had
never asked, and the two sides of it were answering two different
questions: the plan clock keeps counting across a scheduler restart,
while the slot clock starts again from zero.

On CI that comparison went red once, as ``assert 3 <= (2 + 0)``: the
panel drew ``2s`` and the read model said ``5s``.  A single random red
does not say which of the two numbers was wrong, and a fix that cannot
be shown to be repeatable cannot be shown to be a fix at all.  So the
old judgement is pinned here, on a state built the way the scheduler
builds it:

* the **old** judgement is false on that state — it would have failed
  there, which is what CI saw;
* the **new** judgement is true on the very same state;
* the two clocks are two different questions, and the row carries both
  of them.

The state is built through ``plan_scheduler.snapshot.build`` — the
builder the scheduler itself uses — so the row under test has the shape
a real row has rather than the shape of a drawing of one.

Where the numbers come from
----------------------------
The slot was taken five seconds ago and the plan started two seconds
ago: the plan began three seconds after its slot.  That gap is what
CI saw, and it is a gap the scheduler produces on its own — the slot is
taken first and the execution route stamps the plan's start a moment
later, so the two clocks are never the same number.
"""

from __future__ import annotations

from datetime import datetime

from plan_scheduler.snapshot import build
from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (
    _panel_clock_seconds,
)


#: A whole-second moment, so the arithmetic in this file is exact.
_NOW = 1_700_000_000.0

#: The runtime a plan is protected for, in seconds.
_MIN_RUNTIME = 600

#: The tolerance the old comparison allowed, in seconds: ``2 + slack``.
_OLD_TOLERANCE = 2

#: What a comparison adds for the seconds a drawing left out.  A
#: duration drawn in whole seconds leaves nothing out, so it is zero —
#: which is why CI's red read ``assert 3 <= (2 + 0)``.
_DRAWING_SLACK = 0


def _local_stamp(epoch: float) -> str:
    """The stamp the execution route writes, built the way it builds it.

    ``datetime.now().isoformat()`` is a naive **local** time string, and
    the snapshot reads a naive stamp as local time — the zone the writer
    used, which is the only zone the string can mean.  The stamp under
    test is built the same way, so a reader that read it as UTC would
    be caught here rather than in production.
    """
    return datetime.fromtimestamp(epoch).isoformat()


def _real_row() -> dict:
    """The running row of a state built the way the scheduler builds it.

    The slot was taken five seconds ago and the plan started two
    seconds ago, so the row carries a slot clock of ``5`` and a plan
    clock of ``2``.
    """
    state = {
        "capacity": 4,
        "used": 1,
        "now": _NOW,
        "min_runtime_seconds": _MIN_RUNTIME,
        "queue": [],
        "running": [
            {
                "plan_id": "p-1",
                "acquired_at": _NOW - 5,
                "started_at": _local_stamp(_NOW - 2),
            }
        ],
    }
    return build(state)["running"][0]


# ---------------------------------------------------------------------------
# The old judgement
# ---------------------------------------------------------------------------


def test_old_predicate_fails_on_real_state() -> None:
    """The old judgement is false on a real row — CI's red was this.

    The old comparison took what the panel drew (the plan clock) and
    asked the read model for its slot clock.  On this row the two are
    three seconds apart, and the old comparison allowed two — so it
    would have failed here, with CI's own numbers: ``3 <= (2 + 0)``.
    """
    row = _real_row()

    # What the panel drew: the plan clock, which the panel takes from
    # ``running_sec`` (``item.running_sec ?? item.elapsed_sec``).
    drawn = _panel_clock_seconds(row)
    assert drawn is not None, "the row under test carries a clock"
    assert row["elapsed_sec"] == 5, (
        "the slot clock is the time since the slot was taken"
    )
    assert row["running_sec"] == 2, (
        "the plan clock is the time since the plan itself started"
    )

    # The three of CI's ``assert 3 <= (2 + 0)``.
    assert abs(drawn - int(row["elapsed_sec"])) == 3, (
        "the two clocks on this row are three seconds apart — this is the "
        "3 of CI's `assert 3 <= (2 + 0)`"
    )
    assert abs(row["running_sec"] - row["elapsed_sec"]) > _OLD_TOLERANCE, (
        "the old judgement's tolerance is two seconds, and this row is "
        "three seconds away from it"
    )
    assert not (
        abs(drawn - int(row["elapsed_sec"])) <= _OLD_TOLERANCE + _DRAWING_SLACK
    ), (
        "the old judgement must be false on this row: a comparison that "
        "cannot be shown to have been wrong cannot be shown to have been "
        "fixed"
    )


# ---------------------------------------------------------------------------
# The new judgement
# ---------------------------------------------------------------------------


def test_new_predicate_passes_on_same_state() -> None:
    """The new judgement is true on the very same row.

    The comparison now asks the read model the question the panel asked
    — :func:`_panel_clock_seconds`, the plan clock when the row carries
    one — so the two sides of it are two readings of one clock and they
    agree exactly.

    This is not a tautology: the drawn value is read off the row the way
    the panel reads it (``running_sec ?? elapsed_sec``), and the
    expected value is read through the panel's own clock.  A clock that
    preferred the slot clock would answer ``5`` here and this case would
    go red.
    """
    row = _real_row()

    drawn = row["running_sec"] if row["running_sec"] is not None else row["elapsed_sec"]
    expected = _panel_clock_seconds(row)

    assert drawn == 2, "the panel draws the plan clock of this row"
    assert expected == 2, (
        "the panel's own clock is the plan clock when the row carries one"
    )
    assert abs(drawn - expected) <= _DRAWING_SLACK, (
        "the two readings of the panel's own clock must agree exactly — the "
        "old comparison asked for a different clock and got three seconds"
    )


# ---------------------------------------------------------------------------
# The two clocks
# ---------------------------------------------------------------------------


def test_two_clocks_answer_different_questions() -> None:
    """The row carries both clocks, and they are not the same number.

    ``elapsed_sec`` answers "how long has this plan held its slot?" and
    ``running_sec`` answers "how long has the plan been at it?".  The
    eviction rule is read off the first and the panel draws the second;
    they are two facts, and a comparison that takes one for the other is
    comparing two different things — which is exactly what the old
    judgement did.
    """
    row = _real_row()

    assert row["elapsed_sec"] is not None, (
        "the row must carry the slot clock — the panel falls back to it"
    )
    assert row["running_sec"] is not None, (
        "the row must carry the plan clock — the panel draws it"
    )
    assert row["elapsed_sec"] == 5
    assert row["running_sec"] == 2
    assert row["elapsed_sec"] != row["running_sec"], (
        "the two clocks answer two different questions, so on a row whose "
        "plan started after its slot was taken they have to differ"
    )
