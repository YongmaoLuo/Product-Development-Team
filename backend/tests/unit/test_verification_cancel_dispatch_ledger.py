"""2026-11-20: the quiet-window judgement needs a dispatch counter.

``verification_cancel`` only knows *whether* a round was cancelled, so a
stop cannot answer the question the silent phase actually asks: "did
anything new get dispatched after the stop?". The judgement compares a
dispatch count taken when the window opens against the count taken when
it closes, which requires a monotonic per-plan counter living next to the
cancel flag.

Two properties are load-bearing:

  * the counter is monotonic — it only ever grows, and every dispatch
    returns the running total so the caller can record a baseline in
    the same call that counts the dispatch;
  * ``clear()`` does NOT reset it. ``clear()`` means "a fresh round
    starts", but zeroing the counter inside the quiet window would make
    a brand-new dispatch look like "nothing was dispatched" — exactly
    the false "quiet" the counter exists to prevent.
"""

from __future__ import annotations

import pytest

import verification_cancel


@pytest.fixture(autouse=True)
def _clean_dispatch_ledger():
    verification_cancel._reset_for_tests()
    yield
    verification_cancel._reset_for_tests()


def test_note_dispatch_is_monotonic():
    assert verification_cancel.note_dispatch("p1") == 1
    assert verification_cancel.note_dispatch("p1") == 2
    assert verification_cancel.note_dispatch("p1") == 3

    assert verification_cancel.dispatch_total("p1") == 3


def test_clear_keeps_dispatch_total():
    verification_cancel.note_dispatch("p1")
    verification_cancel.note_dispatch("p1")

    verification_cancel.request_cancel("p1")
    verification_cancel.clear("p1")

    assert verification_cancel.dispatch_total("p1") == 2, (
        "clearing the cancel flag must not zero the dispatch counter — "
        "a new dispatch inside the quiet window has to stay visible"
    )


def test_empty_plan_id_is_a_noop():
    assert verification_cancel.note_dispatch("") == 0
    assert verification_cancel.dispatch_total("") == 0

    assert "" not in verification_cancel.cancelled_plans(), (
        "an empty plan id must not register anywhere"
    )


def test_dispatch_total_is_per_plan():
    verification_cancel.note_dispatch("p1")
    verification_cancel.note_dispatch("p2")

    assert verification_cancel.dispatch_total("p1") == 1
    assert verification_cancel.dispatch_total("p2") == 1
    assert verification_cancel.dispatch_total("p3") == 0