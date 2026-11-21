"""Waiting until a plan's verification work is really quiet.

2026-11-20 — a stop that flips a plan's phase has to answer a question
the registry cannot answer on its own: "is this plan *done* talking to
the model?".  A process list that happens to be empty at one instant is
not that answer — the work can start again a millisecond later, which
is exactly the race a stop must not lose.  The quiet judgement this
file pins is the one the architecture decided on (decision point 3):

    an empty process tree, held empty for at least five seconds,
    observed at least twice, with nothing new dispatched.

Each of those four conditions has its own case here, and each case is
written so that it fails when the condition it names is dropped: every
one of them is paired with a *control* — the same wait without that
condition — and the assertions compare the two.  Nothing here touches
a real process: the scanner is injected, so the tests say what they
mean ("this tree is empty" / "this tree still has a survivor") without
depending on what this machine happens to be running.
"""

from __future__ import annotations

import inspect
import time
from typing import List

import pytest

import verification_cancel
from verification_quiesce import (
    QuiesceResult,
    Survivor,
    wait_for_quiescence,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_dispatch_ledger():
    """The dispatch ledger is process-global — leave it as found."""
    verification_cancel._reset_for_tests()
    yield
    verification_cancel._reset_for_tests()


def _empty_scan(calls: List[str]):
    """A scanner that always reports an empty process tree."""
    def _scan(plan_id: str) -> List[Survivor]:
        calls.append(plan_id)
        return []

    return _scan


def _stamped_scan(stamps: List[float]):
    """A scanner that always reports an empty tree and notes the time."""
    def _scan(plan_id: str) -> List[Survivor]:
        stamps.append(time.monotonic())
        return []

    return _scan


# ---------------------------------------------------------------------------
# 1) the scan count
# ---------------------------------------------------------------------------


def test_quiet_requires_two_scans():
    """One empty scan is not evidence: the tree has to be *seen* empty
    twice before the plan counts as quiet."""
    calls: List[str] = []

    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=2,
        max_seconds=5.0,
        poll_interval=0.0,
        dispatch_baseline=0,
        scan=_empty_scan(calls),
    )

    assert result.quiet is True
    assert result.scans == 2
    assert len(calls) == 2
    assert result.survivors == []


def test_one_scan_is_not_enough():
    """The control for the case above: with a single scan required the
    wait ends after one scan — so the two-scan rule is what made the
    other wait longer."""
    calls: List[str] = []

    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=1,
        max_seconds=5.0,
        poll_interval=0.0,
        dispatch_baseline=0,
        scan=_empty_scan(calls),
    )

    assert result.quiet is True
    assert result.scans == 1
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 2) the window
# ---------------------------------------------------------------------------


def test_quiet_requires_window_elapsed():
    """An empty tree is not enough — it has to stay empty for the whole
    window.  The two waits below differ only in the window they
    demand, and the difference shows up as wall-clock time: the wait
    that demands a window spends it, the one that demands none returns
    at once."""
    stamps: List[float] = []

    started = time.monotonic()
    without_window = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=2,
        max_seconds=10.0,
        poll_interval=0.0,
        dispatch_baseline=0,
        scan=_stamped_scan(stamps),
    )
    fast = time.monotonic() - started

    stamps.clear()
    started = time.monotonic()
    with_window = wait_for_quiescence(
        "p1",
        quiet_seconds=0.4,
        min_scans=2,
        max_seconds=10.0,
        poll_interval=0.01,
        dispatch_baseline=0,
        scan=_stamped_scan(stamps),
    )
    slow = time.monotonic() - started

    assert without_window.quiet is True
    assert with_window.quiet is True
    # the window is wall-clock time the wait actually spent
    assert slow >= 0.4
    # ... and it is what the wait without one did not spend
    assert fast < 0.4
    # the window is measured from the first empty scan onwards
    assert stamps[-1] - stamps[0] >= 0.4 - 0.02


def test_survivor_resets_the_quiet_window():
    """A scan that finds a survivor restarts the quiet period: the
    window runs from the moment the tree became empty *again*, not
    from the first scan.  The tree is busy for exactly one scan here,
    and the wait therefore has to pay the window twice."""
    survivor = Survivor(pid=4242, kind="registry", detail="llm-dispatch-7")
    stamps: List[float] = []

    def _scan(plan_id: str) -> List[Survivor]:
        stamps.append(time.monotonic())
        if len(stamps) == 2:
            return [survivor]
        return []

    started = time.monotonic()
    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.4,
        min_scans=2,
        max_seconds=10.0,
        poll_interval=0.05,
        dispatch_baseline=0,
        scan=_scan,
    )
    elapsed = time.monotonic() - started

    assert result.quiet is True
    assert result.survivors == []
    # the window restarts at the scan that followed the survivor
    assert stamps[-1] - stamps[2] >= 0.4 - 0.02
    # ... so the wait spent the window twice over
    assert elapsed >= 0.45


# ---------------------------------------------------------------------------
# 3) the dispatch ledger
# ---------------------------------------------------------------------------


def test_new_dispatch_breaks_quiet():
    """A dispatch that lands inside the window means the plan is not
    quiet: the window is broken by new work, not by the tree happening
    to be empty at the moment it was looked at."""
    calls: List[str] = []

    def _scan_with_a_dispatch(plan_id: str) -> List[Survivor]:
        calls.append(plan_id)
        if len(calls) == 2:
            # a verification point is dispatched while the window is open
            verification_cancel.note_dispatch(plan_id)
        return []

    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=2,
        max_seconds=0.05,
        poll_interval=0.01,
        dispatch_baseline=0,
        scan=_scan_with_a_dispatch,
    )

    assert result.quiet is False
    assert result.dispatch_advanced is True
    assert result.scans >= 2


def test_no_dispatch_keeps_the_wait_quiet():
    """The control for the case above: the same wait without a dispatch
    inside it reaches quiet and says so."""
    calls: List[str] = []

    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=2,
        max_seconds=5.0,
        poll_interval=0.0,
        dispatch_baseline=0,
        scan=_empty_scan(calls),
    )

    assert result.quiet is True
    assert result.dispatch_advanced is False


def test_default_baseline_is_the_ledger_as_it_stands():
    """With no baseline given, the wait compares against the ledger as
    it stands when the wait starts — so work dispatched *before* the
    wait does not count against it."""
    calls: List[str] = []
    verification_cancel.note_dispatch("p1")  # before the wait starts

    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=2,
        max_seconds=5.0,
        poll_interval=0.0,
        scan=_empty_scan(calls),
    )

    assert result.quiet is True
    assert result.dispatch_advanced is False


# ---------------------------------------------------------------------------
# 4) the deadline
# ---------------------------------------------------------------------------


def test_timeout_returns_survivors():
    """A plan that never goes quiet ends the wait with a report, not an
    exception: the caller gets the survivors that are still there."""
    survivor = Survivor(pid=4242, kind="registry", detail="llm-dispatch-7")

    def _scan(plan_id: str) -> List[Survivor]:
        return [survivor]

    result = wait_for_quiescence(
        "p1",
        quiet_seconds=0.0,
        min_scans=2,
        max_seconds=0.05,
        poll_interval=0.01,
        dispatch_baseline=0,
        scan=_scan,
    )

    assert result.quiet is False
    assert result.survivors == [survivor]
    assert result.scans >= 2
    assert result.dispatch_advanced is False


# ---------------------------------------------------------------------------
# 5) the published shape
# ---------------------------------------------------------------------------


def test_public_signature_matches_the_contract():
    """The call shape the architecture decided on, defaults included."""
    params = inspect.signature(wait_for_quiescence).parameters

    assert list(params) == [
        "plan_id",
        "quiet_seconds",
        "min_scans",
        "max_seconds",
        "poll_interval",
        "dispatch_baseline",
        "scan",
        "execution_state",
    ]
    assert params["quiet_seconds"].default == 5.0
    assert params["min_scans"].default == 2
    assert params["max_seconds"].default == 60.0
    assert params["poll_interval"].default == 0.5
    assert params["dispatch_baseline"].default is None
    assert params["scan"].default is None
    assert params["execution_state"].default is None


def test_result_carries_the_four_facts():
    """The verdict is one value with the four facts a caller acts on."""
    result = QuiesceResult(
        quiet=True, scans=3, survivors=[], dispatch_advanced=False,
    )

    assert result.quiet is True
    assert result.scans == 3
    assert result.survivors == []
    assert result.dispatch_advanced is False
