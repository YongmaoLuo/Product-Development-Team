"""The panel's clock, the panel's rows, and how close two readings may be.

What this file is for
---------------------
The operator's panel and the scheduler's read model are two readings of
one fact, and comparing them is only a comparison when both sides read
**the same** fact.  The panel draws

    item.running_sec ?? item.elapsed_sec

(``frontend/app.js:2223``) — the **plan** clock when the read model
carries one, and the **slot** clock only when it does not.  A
comparison that asks the read model for ``elapsed_sec`` alone compares
two *different* clocks, and the two do not have to agree: the plan clock
keeps counting across a scheduler restart, while the slot clock starts
again from zero.

Three facts decide whether a panel and a read model say the same thing:

1. **which clock the panel draws** — the plan clock, with the slot
   clock only as the fallback;
2. **whether a drawn row says what the model's row says** — the plan,
   the evictable flag and the duration;
3. **how far apart two readings of one clock may be** — the panel draws
   the model it read at its last poll, so a reading of it can be one
   poll period old.

All three are pure functions of their arguments, so they are pinned
here rather than through a browser.  A seam that is not fixed cannot
tell a later fix from a later accident.

Where the functions live
------------------------
In the end-to-end module
(``tests/e2e/plan_scheduler/test_scheduler_operator_e2e.py``), because
that is where the panel is driven and where the comparison is made.  They
are imported from there rather than copied, so there is exactly one
definition of what the panel means.
"""

from __future__ import annotations

import pytest

from tests.e2e.plan_scheduler.test_scheduler_operator_e2e import (
    PANEL_POLL_SECONDS,
    _panel_clock_seconds,
    _panel_parity_bound,
    _panel_rows_match,
)


# ---------------------------------------------------------------------------
# The clock the panel draws
# ---------------------------------------------------------------------------


def test_panel_clock_prefers_running_sec():
    """The panel draws the plan clock when the read model carries one.

    The row carries both clocks and they disagree — this is the very
    shape that made the end-to-end case read ``2s`` on the panel and
    ``5s`` in the read model.  The panel's answer is the plan clock.
    """
    row = {"plan_id": "pdt-e2e-1", "running_sec": 2, "elapsed_sec": 5}
    assert _panel_clock_seconds(row) == 2


def test_zero_running_sec_is_kept():
    """``??`` is not ``||``: a plan clock of zero is a number, not a gap.

    The panel draws a plan that has just started as ``0s``.  Reading
    zero as "missing" would hand the comparison the slot clock instead
    — a different clock, and the one this case is about.
    """
    row = {"plan_id": "pdt-e2e-1", "running_sec": 0, "elapsed_sec": 9}
    assert _panel_clock_seconds(row) == 0


def test_falls_back_to_slot_clock():
    """Without a plan clock the panel draws the slot clock.

    A row whose start stamp cannot be read carries no plan clock, and
    the panel falls back to the slot clock — so the comparison has to
    fall back with it.
    """
    row = {"plan_id": "pdt-e2e-1", "running_sec": None, "elapsed_sec": 9}
    assert _panel_clock_seconds(row) == 9


def test_unknown_when_both_missing():
    """No clock at all is unknown — it is not zero.

    A row that carries neither clock has nothing to compare, and zero
    would be a claim about a plan's age that nobody made.
    """
    assert _panel_clock_seconds({"plan_id": "pdt-e2e-1"}) is None


# ---------------------------------------------------------------------------
# The bound two readings of one clock may differ by
# ---------------------------------------------------------------------------


def test_bound_is_one_poll_plus_gap():
    """The bound is one poll, the gap, a second and the drawing's slack.

    ``5.0`` is the panel's poll period, ``0.8`` is how long the
    comparison's own reading took, ``1`` is the second a whole-second
    clock may lose to its own truncation, and ``0`` is a drawing that
    left nothing out.
    """
    assert _panel_parity_bound(0.8, 0) == pytest.approx(6.8)


def test_bound_counts_a_negative_gap_as_zero():
    """A negative gap is not a gap: it is read as no gap at all."""
    assert _panel_parity_bound(-3.0, 0) == pytest.approx(PANEL_POLL_SECONDS + 1)


def test_bound_adds_what_the_drawing_left_out():
    """An hour and a half is drawn ``1h 30m``: the seconds are gone.

    The slack is what the drawing left out, and the bound has to carry
    it — otherwise a duration drawn to the minute is compared to a
    number of seconds as if both were exact.
    """
    assert _panel_parity_bound(0.0, 59) == pytest.approx(PANEL_POLL_SECONDS + 60)


def test_poll_seconds_is_the_panels_own_number():
    """The poll period is the panel's, not a number this file invented.

    ``SCHEDULER_POLL_MS = 5000`` in ``frontend/app.js:2429`` — five
    seconds is how often the panel re-reads the read model, and that is
    how old a reading of the panel may be.
    """
    assert PANEL_POLL_SECONDS == 5.0


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


def test_rows_match_when_the_panel_drew_the_plan_clock():
    """The panel's ``2s`` and the model's plan clock of ``2s`` agree.

    The model's slot clock says ``5s``, and it is not what the panel
    drew: the row matches, because the comparison asks the panel's
    question.
    """
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "2s", "否", "—"]],
        [
            {
                "plan_id": "pdt-e2e-1",
                "running_sec": 2,
                "elapsed_sec": 5,
                "evictable": False,
            }
        ],
    )
    assert matched is True
    assert why == ""


def test_rows_do_not_match_when_the_counts_differ():
    """A row the model does not have is a disagreement."""
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "2s", "否", "—"]],
        [],
    )
    assert matched is False
    assert why


def test_rows_do_not_match_when_the_plan_differs():
    """The same row, a different plan: the two sides are not talking about
    the same thing."""
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "2s", "否", "—"]],
        [
            {
                "plan_id": "pdt-e2e-2",
                "running_sec": 2,
                "elapsed_sec": 5,
                "evictable": False,
            }
        ],
    )
    assert matched is False
    assert why


def test_rows_do_not_match_when_the_evictable_flag_differs():
    """The panel says the plan may be interrupted; the model says it may
    not.  That is the flag the eviction rule is read off."""
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "2s", "是", "—"]],
        [
            {
                "plan_id": "pdt-e2e-1",
                "running_sec": 2,
                "elapsed_sec": 5,
                "evictable": False,
            }
        ],
    )
    assert matched is False
    assert why


def test_rows_do_not_match_when_the_duration_is_off():
    """A duration further off than the bound is a disagreement.

    The bound is one poll plus a second, so a difference of eighteen
    seconds is not a clock that was read a moment later — it is two
    different facts.
    """
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "2s", "否", "—"]],
        [
            {
                "plan_id": "pdt-e2e-1",
                "running_sec": 20,
                "elapsed_sec": 20,
                "evictable": False,
            }
        ],
    )
    assert matched is False
    assert why


def test_rows_do_not_match_when_the_duration_is_not_a_duration():
    """A cell that is not a duration is not a number of seconds."""
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "—", "否", "—"]],
        [
            {
                "plan_id": "pdt-e2e-1",
                "running_sec": 2,
                "elapsed_sec": 5,
                "evictable": False,
            }
        ],
    )
    assert matched is False
    assert why


def test_rows_do_not_match_when_the_model_carries_no_clock():
    """A row with neither clock cannot be compared — and is not read as
    zero."""
    matched, why = _panel_rows_match(
        [["pdt-e2e-1", "2s", "否", "—"]],
        [{"plan_id": "pdt-e2e-1", "evictable": False}],
    )
    assert matched is False
    assert why
