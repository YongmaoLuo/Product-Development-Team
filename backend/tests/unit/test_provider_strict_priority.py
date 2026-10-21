"""Dispatches consume a tier in RANKING order (strict priority).

2026-10-21 — user contract: a tier is consumed strictly in chain order.
The first provider that still has a free slot takes the dispatch, and it
keeps taking dispatches for as long as it has room. Only a full (or
unusable) pool hands over to the next one in the chain.

Worked example: a tier declaring three providers, each with cap 10, and
twelve concurrent dispatches:

    dispatch 1..10  → the first
    dispatch 11..12 → the second
    the third       → nothing

This file replaces the 2026-09-22 rotation, which spread work in
proportion to how many providers a tier could actually call (1 → A,
2 → B, 3 → A, 4 → B). The rotation wasted quota in two ways: every
account's weekly allowance was left partly unspent when its window
reset, and a lower-ranked account was drawn down while the higher-ranked
ones still had budget left. Strict priority spends the highest-ranked
quota first, which is what the ranking was for.

These tests pin the strict order, the hand-over to the next pool when
the first one is full, the exclusion of an unusable first pool (parked
or quota-exhausted) from the walk, and the preservation of the
2026-09-17 queueing behaviour (a merely-busy pool still hands over
rather than blocking).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    acquire_provider_with_dynamic_capacity,
    get_shared_cooldown,
    set_shared_cooldown,
)

POOL = ["Vendor A Pro", "Vendor A"]

#: Ten each, so "fill the pool" is ``range(10)`` throughout.
_POOL_CAPS = [("^vendor-a-pro$", 10), ("^vendor a$", 10)]

#: A third pool, used by the concurrent-fan-out test.
_THREE_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]


@pytest.fixture(autouse=True)
def _pools(capacity_config):
    capacity_config(_POOL_CAPS)


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


def _dispatch(tracker, providers=None, usage=None, **kw):
    return acquire_provider_with_dynamic_capacity(
        providers if providers is not None else POOL,
        tracker,
        usage or {},
        timeout=kw.pop("timeout", 0.1),
        poll_interval=kw.pop("poll_interval", 0.01),
        **kw,
    )


def _release(tracker, names):
    for name in names:
        tracker.release(name)


class TestStrictPriorityOrder:
    def test_six_dispatches_all_take_the_first_pool(self):
        """The contract, literally: the first pool with room takes every
        dispatch, and it keeps taking them while it has room."""
        tracker = ActiveConcurrencyTracker()
        picked = []
        for _ in range(6):
            name = _dispatch(tracker)
            picked.append(name)
            # Release immediately, so the first pool is never full: the
            # hand-over must be caused by a full pool, not by a queue.
            _release(tracker, [name])

        assert picked == ["Vendor A Pro"] * 6

    def test_a_full_first_pool_hands_over_to_the_next(self):
        """The first pool is at its cap of 10 → the next dispatch goes to
        the second pool. Nothing blocks: the walk falls through."""
        tracker = ActiveConcurrencyTracker()
        for _ in range(10):  # the first pool is at its cap
            tracker.try_acquire("Vendor A Pro", 10)

        assert _dispatch(tracker) == "Vendor A"

    def test_a_parked_first_pool_hands_over_to_the_next(self):
        """A refusal earlier in this run parked the first pool. It is
        unreachable for the rest of the run, so the walk starts at the
        second one — the park is not a "wait for a slot" case."""
        tracker = ActiveConcurrencyTracker()
        get_shared_cooldown().park("Vendor A Pro", seconds=600)

        picked = []
        for _ in range(4):
            name = _dispatch(tracker)
            picked.append(name)
            _release(tracker, [name])

        assert picked == ["Vendor A"] * 4

    def test_a_quota_exhausted_first_pool_hands_over_to_the_next(self):
        """The live reading says the first pool's window is empty. That is
        not a slot that frees up, so it is filtered out of the chain."""
        tracker = ActiveConcurrencyTracker()
        picked = []
        for _ in range(4):
            name = _dispatch(tracker, usage={"Vendor A Pro": 0.0})
            picked.append(name)
            _release(tracker, [name])

        assert picked == ["Vendor A"] * 4


class TestStrictPriorityUnderConcurrency:
    def test_twelve_concurrent_dispatches_fill_the_first_pool(self, capacity_config):
        """The point of the rule is the CONCURRENT fan-out: a dozen
        agents dispatched at once fill the highest-ranked pool first and
        only spill into the next one once it is full."""
        capacity_config(_THREE_POOL_CAPS)
        tracker = ActiveConcurrencyTracker()
        names = ["Vendor A Pro", "Vendor A", "Vendor A API"]
        picked = []
        lock = threading.Lock()
        barrier = threading.Barrier(12)

        def _worker():
            barrier.wait()
            name = acquire_provider_with_dynamic_capacity(
                names, tracker, {}, timeout=5.0, poll_interval=0.01,
            )
            with lock:
                picked.append(name)

        threads = [threading.Thread(target=_worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert picked.count("Vendor A Pro") == 10, (
            f"the first pool must fill to its cap of 10; got {picked}"
        )
        assert picked.count("Vendor A") == 2, (
            f"the overflow must spill into the second pool; got {picked}"
        )
        assert picked.count("Vendor A API") == 0, (
            f"the third pool must stay untouched; got {picked}"
        )
        assert tracker.total() == 12, "no slot beyond the caps was taken"


class TestQueueingIsUnchanged:
    def test_every_pool_full_waits_then_returns_none(self):
        tracker = ActiveConcurrencyTracker()
        for _ in range(10):
            tracker.try_acquire("Vendor A Pro", 10)
        for _ in range(10):
            tracker.try_acquire("Vendor A", 10)

        assert _dispatch(tracker, timeout=0.2) is None

    def test_a_saturated_fleet_queues_then_returns_none(self):
        """Both pools at cap → wait, then give up; no extra slot is taken.

        There is no fleet-wide constant above the per-pool caps, so the
        fleet is full exactly when every pool is.
        """
        tracker = ActiveConcurrencyTracker()
        for name in POOL:
            for _ in range(10):
                tracker.try_acquire(name, 10)

        assert _dispatch(tracker, timeout=0.2) is None
        assert tracker.total() == 20, "nothing extra was taken"
