"""Dispatches rotate across the tier's EFFECTIVE providers.

2026-09-22 — work should be spread in proportion to how many providers a
tier can actually call, so that one provider's quota is not drained before
the next one is tried at all. A tier that declares three providers but can
only call two should rotate over two.

Worked example: a tier declaring three providers, one of which is
unreachable, has two effective members, and dispatch alternates between
them:

    agent 1 → the first
    agent 2 → the second
    agent 3 → the first
    agent 4 → the second

The old walk always started at index 0 and took the first candidate with
a free slot, so with caps of 10 the first ten dispatches all landed on
one provider and the second only received work once the first was
saturated. That is precisely the burn pattern this replaces.

These tests pin the rotation, the "divide by EFFECTIVE providers" rule
(a dead or parked provider must not occupy a slot in the cycle), and the
preservation of the 2026-09-17 queueing behaviour (a merely-busy pool
still hands over rather than blocking).
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


class TestRotationOrder:
    def test_two_pools_alternate(self):
        """The directive's worked example, literally: 1→A, 2→B, 3→A, 4→B."""
        tracker = ActiveConcurrencyTracker()
        picked = []
        for _ in range(6):
            name = _dispatch(tracker)
            picked.append(name)
            _release(tracker, [name])

        assert picked == [
            "Vendor A Pro", "Vendor A",
            "Vendor A Pro", "Vendor A",
            "Vendor A Pro", "Vendor A",
        ]

    def test_a_dead_provider_does_not_hold_a_rotation_slot(self):
        """``Vendor A API`` is unreachable — but it is not even in the
        candidate list by the time the walk runs (the caller filters on
        availability). The rotation must still alternate over two, not
        leave a dead every-third slot."""
        tracker = ActiveConcurrencyTracker()
        picked = []
        for _ in range(4):
            name = _dispatch(tracker)
            picked.append(name)
            _release(tracker, [name])

        assert picked.count("Vendor A Pro") == 2
        assert picked.count("Vendor A") == 2

    def test_a_parked_provider_is_excluded_from_the_count(self):
        """Same rule when the provider is dead because of a 429: the
        rotation divides by the EFFECTIVE count, not the declared one."""
        tracker = ActiveConcurrencyTracker()
        get_shared_cooldown().park("Vendor A Pro", seconds=600)

        picked = []
        for _ in range(4):
            name = _dispatch(tracker)
            picked.append(name)
            _release(tracker, [name])

        assert picked == ["Vendor A"] * 4, (
            "a parked provider must not occupy a rotation slot"
        )

    def test_a_quota_exhausted_provider_is_excluded_from_the_count(self):
        tracker = ActiveConcurrencyTracker()
        picked = []
        for _ in range(4):
            name = _dispatch(tracker, usage={"Vendor A Pro": 0.0})
            picked.append(name)
            _release(tracker, [name])

        assert picked == ["Vendor A"] * 4

    def test_three_live_pools_rotate_through_all_three(self):
        tracker = ActiveConcurrencyTracker()
        pool = ["Vendor A Pro", "Vendor A", "Vendor A API"]
        picked = []
        for _ in range(6):
            name = _dispatch(tracker, providers=pool)
            picked.append(name)
            _release(tracker, [name])

        assert picked == pool + pool


class TestRotationUnderConcurrency:
    def test_concurrent_dispatches_spread_evenly(self):
        """The point of the rule is the CONCURRENT fan-out — a dozen
        agents dispatched at once must not all aim at the first pool."""
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

        assert sorted(picked) == sorted(names * 4), (
            f"12 concurrent dispatches over 3 pools must give each pool 4; "
            f"got {picked}"
        )

    def test_the_cursor_is_fetch_and_advance(self):
        tracker = ActiveConcurrencyTracker()
        indices = [tracker.next_rotation_index(2) for _ in range(4)]
        assert indices == [0, 1, 0, 1]

    def test_a_shrinking_pool_does_not_need_cursor_coordination(self):
        """The cursor is monotonic and taken modulo the live count, so a
        provider dropping out (or coming back) is handled for free."""
        tracker = ActiveConcurrencyTracker()
        assert tracker.next_rotation_index(2) == 0
        assert tracker.next_rotation_index(3) == 1
        assert tracker.next_rotation_index(1) == 0

    def test_a_zero_sized_pool_is_not_a_crash(self):
        tracker = ActiveConcurrencyTracker()
        assert tracker.next_rotation_index(0) == 0


class TestQueueingIsUnchanged:
    def test_a_full_rotation_slot_falls_through_to_the_next_pool(self):
        """Rotation picks the STARTING point; it must not turn into
        "block on my slot" — a saturated first pool still hands over."""
        tracker = ActiveConcurrencyTracker()
        for _ in range(10):  # the first pool is at its cap
            tracker.try_acquire("Vendor A Pro", 10)

        assert _dispatch(tracker) == "Vendor A"

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
