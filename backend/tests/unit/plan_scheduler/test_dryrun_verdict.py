"""Unit tests: the dry-run verdict judge and the difference classifier.

What this file holds down
-------------------------
The native scheduler replaces the old cron scheduler, but only after a
24 hour dry run proves the two behave the same.  "Behave the same"
is not something a human can eyeball across 48 half-hour windows, so
the criterion is written down once and then **executed**:

* **48 windows, at least 45 of them identical.**  The remaining windows
  may differ, but only in ways the architecture calls *allowed*.
* **No forbidden difference anywhere.**  One is one too many; the switch
  does not happen.
* **An unknown difference is forbidden.**  A kind nobody classified is
  not quietly ignored — it is treated as the worst case, because the
  cost of a missed difference is a wrong switch and the cost of a false
  alarm is another 24 hour run.
* **A window that was never compared is not evidence.**  The writer
  holds one side of the comparison — the new engine's side — so a
  window without a comparison carries no verdict at all.  It is
  counted as *uncompared*, and a dry run with an uncompared window in
  it does not pass: "the two engines agreed" is not something a run
  that compared nothing may claim.

Two pure functions carry that criterion: :func:`evaluate_windows` turns
the windows into a :class:`Verdict`, and :func:`classify_difference`
says whether one difference is allowed or forbidden.  Neither of them
reads a file, starts a process or touches the scheduler — they take
data and give an answer, which is what makes the criterion checkable
in a unit test instead of only after a real 24 hour run.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from plan_scheduler.dryrun import (
    ALLOWED,
    ALLOWED_ONLY,
    FORBIDDEN,
    IDENTICAL,
    UNCOMPARED,
    Verdict,
    classify_difference,
    classify_window,
    evaluate_windows,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The kinds the architecture calls allowed: a difference caused by the
#: two schedulers running on slightly different rhythms, and a
#: difference in the *order* two workspaces were served in.
ALLOWED_KINDS = ("transient_set_diff", "workspace_order_diff")

#: The kinds the architecture calls forbidden: the new engine started a
#: plan the old engine never started, the two engines disagreed about
#: whether a plan was evictable, and the two engines queued the plans
#: in the opposite order.
FORBIDDEN_KINDS = ("new_start", "eviction_flip", "queue_order_flip")


def _identical_window() -> Dict[str, Any]:
    """A window in which the two engines agree on everything.

    The window says it was compared: the comparison is what found the
    agreement, and a window nobody compared agrees with nothing.
    """
    return {"differences": [], "compared": True}


def _uncompared_window() -> Dict[str, Any]:
    """A window that was never compared with the old engine."""
    return {"differences": [], "compared": False}


def _window_with(kind: str) -> Dict[str, Any]:
    """A window carrying exactly one difference of the given kind."""
    return {"differences": [{"kind": kind}], "compared": True}


def _windows(
    identical: int, allowed: int = 0, forbidden: int = 0
) -> List[Dict[str, Any]]:
    """Build a list of windows with the given shape.

    The identical windows come first and the differing ones after, so
    the shape of the list says nothing about the verdict — only the
    differences in it do.
    """
    windows = [_identical_window() for _ in range(identical)]
    windows += [_window_with(ALLOWED_KINDS[0]) for _ in range(allowed)]
    windows += [_window_with(FORBIDDEN_KINDS[0]) for _ in range(forbidden)]
    return windows


# ---------------------------------------------------------------------------
# 1. The judge: 48 windows, at least 45 identical, no forbidden difference
# ---------------------------------------------------------------------------


def test_45_of_48_passes() -> None:
    """45 identical windows plus 3 allowed-only windows passes."""
    verdict = evaluate_windows(_windows(identical=45, allowed=3))

    assert verdict.passed is True
    assert verdict.consistent == 45
    assert verdict.allowed_only == 3
    assert verdict.forbidden == 0
    assert verdict.uncompared == 0


def test_44_of_48_fails() -> None:
    """44 identical windows fails, even though the rest are allowed.

    The threshold is on the number of *identical* windows, not on the
    number of acceptable windows: a dry run that agrees with the old
    engine in 44 of 48 windows is not a dry run that agrees with it.
    """
    verdict = evaluate_windows(_windows(identical=44, allowed=4))

    assert verdict.passed is False
    assert verdict.consistent == 44
    assert verdict.allowed_only == 4
    assert verdict.forbidden == 0


def test_one_forbidden_fails() -> None:
    """A single forbidden difference fails the whole dry run."""
    verdict = evaluate_windows(
        _windows(identical=45, allowed=2, forbidden=1)
    )

    assert verdict.passed is False
    assert verdict.consistent == 45
    assert verdict.forbidden == 1


def test_short_window_count_fails() -> None:
    """47 windows fail even though every one of them is identical.

    The comparison unit is the old engine's 30 minute cron window, and
    a 24 hour run has 48 of them.  A run that stopped early has not
    produced the evidence the criterion asks for.
    """
    verdict = evaluate_windows(_windows(identical=47))

    assert verdict.passed is False
    assert verdict.consistent == 47
    assert verdict.forbidden == 0


def test_verdict_counts_partition_the_windows() -> None:
    """Every window lands in exactly one of the four buckets."""
    verdict = evaluate_windows(_windows(identical=45, allowed=2, forbidden=1))

    assert (
        verdict.consistent
        + verdict.allowed_only
        + verdict.forbidden
        + verdict.uncompared
        == 48
    )


def test_verdict_is_the_documented_shape() -> None:
    """The verdict reads the way the criterion is written down."""
    verdict = evaluate_windows(_windows(identical=45, allowed=3))

    assert verdict == Verdict(
        passed=True, consistent=45, allowed_only=3, forbidden=0
    )


# ---------------------------------------------------------------------------
# 2. A window that was never compared is not evidence
# ---------------------------------------------------------------------------


def test_an_uncompared_window_is_not_consistent() -> None:
    """A window nobody compared is not a window the two engines agree on."""
    verdict = evaluate_windows([_uncompared_window()])

    assert verdict.consistent == 0
    assert verdict.uncompared == 1
    assert verdict.passed is False


def test_a_window_without_a_comparison_marker_is_not_consistent() -> None:
    """A window that only *claims* to be consistent is not evidence.

    This is the shape a writer that compared nothing produces: an empty
    difference list and a hard-coded ``consistent`` flag.  The claim
    is not backed by a comparison, so it is not counted.
    """
    windows = [
        {
            "start": "2026-10-28T00:00:00Z",
            "end": "2026-10-28T00:30:00Z",
            "running": [],
            "queue": [],
            "differences": [],
            "consistent": True,
        }
        for _ in range(48)
    ]

    verdict = evaluate_windows(windows)

    assert verdict.consistent == 0
    assert verdict.uncompared == 48
    assert verdict.passed is False


def test_uncompared_windows_block_the_gate() -> None:
    """One uncompared window is enough to stop the switch."""
    windows = _windows(identical=47) + [_uncompared_window()]

    verdict = evaluate_windows(windows)

    assert verdict.consistent == 47
    assert verdict.uncompared == 1
    assert verdict.passed is False


def test_a_window_that_lists_a_difference_was_compared() -> None:
    """A window that carries a difference has been compared.

    Somebody compared and found something; the window is judged by the
    difference it found, not by the marker it forgot to write.
    """
    windows = [
        {"differences": [{"kind": FORBIDDEN_KINDS[0]}]},
        {"differences": [{"kind": ALLOWED_KINDS[0]}]},
    ]

    verdict = evaluate_windows(windows)

    assert verdict.forbidden == 1
    assert verdict.allowed_only == 1
    assert verdict.uncompared == 0


def test_a_window_that_says_it_differs_without_naming_one_is_forbidden() -> None:
    """A window that says it differs and names no difference is forbidden.

    The difference is unexplained, and an unexplained difference is
    never quietly allowed.
    """
    verdict = evaluate_windows([{"differences": [], "consistent": False}])

    assert verdict.forbidden == 1
    assert verdict.uncompared == 0
    assert verdict.passed is False


# ---------------------------------------------------------------------------
# 3. The classifier of one window
# ---------------------------------------------------------------------------


def test_classify_window_says_what_one_window_is() -> None:
    """One window is judged on its own, in the judge's own words."""
    assert classify_window(_identical_window()) == IDENTICAL
    assert classify_window(_window_with(ALLOWED_KINDS[0])) == ALLOWED_ONLY
    assert classify_window(_window_with(FORBIDDEN_KINDS[0])) == FORBIDDEN
    assert classify_window(_uncompared_window()) == UNCOMPARED


# ---------------------------------------------------------------------------
# 4. The classifier: allowed kinds, forbidden kinds, and the unknown one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALLOWED_KINDS)
def test_allowed_kinds_are_allowed(kind: str) -> None:
    """A rhythm difference or a workspace ordering difference is allowed."""
    assert classify_difference(kind) == ALLOWED


@pytest.mark.parametrize("kind", FORBIDDEN_KINDS)
def test_forbidden_kinds_are_forbidden(kind: str) -> None:
    """A new start, an eviction flip or a flipped queue order is not."""
    assert classify_difference(kind) == FORBIDDEN


def test_unknown_kind_is_forbidden() -> None:
    """A kind nobody classified is forbidden, never silently ignored.

    The cost of guessing wrong here is asymmetric: a difference that was
    really forbidden would let the switch happen on bad evidence, while
    a difference that was really allowed only costs another dry run.
    """
    assert classify_difference("no_such_kind") == FORBIDDEN


def test_unknown_kind_in_a_window_fails_the_verdict() -> None:
    """An unclassified difference fails the dry run it appears in."""
    verdict = evaluate_windows(
        _windows(identical=45, allowed=2) + [_window_with("no_such_kind")]
    )

    assert verdict.passed is False
    assert verdict.forbidden == 1
    assert verdict.allowed_only == 2


def test_a_window_with_both_an_allowed_and_a_forbidden_difference_fails() -> None:
    """A window is allowed-only only when *every* difference is allowed."""
    windows = _windows(identical=45, allowed=2) + [
        {
            "differences": [
                {"kind": ALLOWED_KINDS[0]},
                {"kind": FORBIDDEN_KINDS[0]},
            ],
            "compared": True,
        }
    ]

    verdict = evaluate_windows(windows)

    assert verdict.passed is False
    assert verdict.forbidden == 1
    assert verdict.allowed_only == 2
