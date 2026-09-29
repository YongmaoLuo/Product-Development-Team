"""Capacity-gated provider selection — "no slot ⇒ move to the next pool".

Why downgrade is slot-driven rather than failure-driven: a provider that
answers is not necessarily a provider that should take more work. When
the walk asks each candidate only "does it have a base_url + token", the
first entry wins every time and 100% of verification traffic lands on one
provider while its siblings sit idle. Waiting on a failure to spread the
load means the failure has to happen first.

Two things are pinned here:

* :func:`canonical_capacity_key` keeps CC Switch *rows* distinct. Two
  rows are two credentials and two quota pools; collapsing them (as a
  name→logical-ID map does for config resolution) would silently give
  them one shared slot counter.
* :meth:`ActiveConcurrencyTracker.try_acquire` does check-and-take under
  one lock. The obvious ``if current < limit: acquire()`` shape races —
  with a dozen VPs polling, several pass the check before any of them
  increments.

Caps come from ``provider_capacity.yaml`` via the ``capacity_config``
fixture; see ``conftest`` for the isolation rule.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    acquire_provider_with_dynamic_capacity,
    canonical_capacity_key,
    compute_dynamic_limit,
    get_shared_tracker,
    set_shared_tracker,
)


# ---------------------------------------------------------------------------
# 1. The key space
# ---------------------------------------------------------------------------


class TestCanonicalCapacityKey:
    def test_display_name_and_kebab_id_agree(self):
        assert canonical_capacity_key("Vendor A Pro") == "vendor-a-pro"
        assert canonical_capacity_key("vendor-a-pro") == "vendor-a-pro"

    def test_rows_sharing_a_prefix_stay_distinct(self):
        """The regression this function exists to prevent.

        ``cc_switch`` owns a name→id table that may map two display names
        onto one id. That is right for *config resolution* — two rows
        configured the same way may share an answer — and wrong for
        *capacity*: two CC Switch rows are two quota pools with two
        credentials, and collapsing them onto one key would give them one
        shared slot counter.
        """
        keys = {
            canonical_capacity_key(n)
            for n in ("Vendor A Pro", "Vendor A", "Vendor A API")
        }
        assert keys == {"vendor-a-pro", "vendor-a", "vendor-a-api"}

    def test_each_row_is_capped_from_its_own_rule(self, capacity_config):
        """Two rows at different caps is a configuration, not a lookup."""
        capacity_config([("^vendor-a-pro$", 10), ("^vendor a$", 3)])

        assert compute_dynamic_limit("Vendor A Pro", 100.0) == 10
        assert compute_dynamic_limit("Vendor A", 100.0) == 3

    @pytest.mark.parametrize("raw", ["", None])
    def test_empty_input_does_not_raise(self, raw):
        assert canonical_capacity_key(raw) == ""


# ---------------------------------------------------------------------------
# 2. Atomic acquire
# ---------------------------------------------------------------------------


class TestTryAcquire:
    def test_takes_a_slot_while_under_the_limit(self):
        t = ActiveConcurrencyTracker()
        assert t.try_acquire("Vendor A Pro", 2) is True
        assert t.try_acquire("Vendor A Pro", 2) is True
        assert t.try_acquire("Vendor A Pro", 2) is False
        # The kebab spelling addresses the same counter.
        assert t.current("vendor-a-pro") == 2

    def test_zero_limit_is_never_acquired(self):
        t = ActiveConcurrencyTracker()
        assert t.try_acquire("Vendor A Pro", 0) is False
        assert t.current("Vendor A Pro") == 0

    def test_concurrent_racers_never_exceed_the_cap(self):
        """The whole reason ``try_acquire`` exists.

        The check-then-act shape (``if current < limit: acquire()``)
        lets several threads pass the check before any increments. Here
        64 threads race for 5 slots; exactly 5 must win.
        """
        tracker = ActiveConcurrencyTracker()
        cap = 5
        winners = []
        barrier = threading.Barrier(64)
        lock = threading.Lock()

        def _race():
            barrier.wait()
            if tracker.try_acquire("Vendor A Pro", cap):
                with lock:
                    winners.append(1)

        threads = [threading.Thread(target=_race) for _ in range(64)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert len(winners) == cap, (
            f"{len(winners)} threads took a slot out of {cap} — the "
            f"check-and-increment is not atomic"
        )
        assert tracker.current("Vendor A Pro") == cap

    def test_release_returns_the_slot(self):
        t = ActiveConcurrencyTracker()
        assert t.try_acquire("Vendor A Pro", 1) is True
        assert t.try_acquire("Vendor A Pro", 1) is False
        t.release("Vendor A Pro")
        assert t.try_acquire("Vendor A Pro", 1) is True


class TestGlobalCeiling:
    """There is no fleet ceiling — capacity is the sum of the caps.

    The tracker used to enforce a hand-written ``GLOBAL_MAX_CONCURRENCY``
    on top of the per-provider caps. That number duplicated what the caps
    already determine, and it had to be re-asserted every time a provider
    was added or retired. The replacement contract is that a provider
    which cannot be called never takes a slot, so the ceiling falls out
    of the configuration; see
    ``test_agent_dynamic_concurrency.TestReserveSlot``.
    """

    def test_caps_do_not_leak_across_providers(self):
        """Each provider's slots are its own — one full pool does not
        consume another's room."""
        t = ActiveConcurrencyTracker()
        assert t.try_acquire("Vendor A", 1) is True
        assert t.try_acquire("Vendor A", 1) is False
        assert t.try_acquire("Vendor B", 1) is True
        assert t.try_acquire("Vendor B", 1) is False
        assert t.total() == 2

    def test_total_is_observable_and_unbounded_by_the_tracker(self):
        """``total()`` reports depth; nothing in the tracker caps it."""
        t = ActiveConcurrencyTracker()
        for index in range(50):
            t.acquire(f"Vendor {index}")
        assert t.total() == 50


# ---------------------------------------------------------------------------
# 3. The queueing walk
# ---------------------------------------------------------------------------


CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]
FULL = {p: 100.0 for p in CHAIN}

#: Caps the walk tests below assume: 10 per pool, so "fill it" is
#: ``range(10)`` throughout and the 60 %-remaining case halves to 6.
_CHAIN_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]


class TestAcquireProviderWithDynamicCapacity:
    @pytest.fixture(autouse=True)
    def _caps(self, capacity_config):
        capacity_config(_CHAIN_CAPS)

    def test_first_candidate_with_room_wins(self):
        t = ActiveConcurrencyTracker()
        assert acquire_provider_with_dynamic_capacity(CHAIN, t, FULL) == "Vendor A Pro"
        assert t.current("Vendor A Pro") == 1

    def test_a_full_first_provider_hands_over_to_the_next(self):
        """The rule in one test: the slot, not the failure, drives the swap."""
        t = ActiveConcurrencyTracker()
        for _ in range(10):
            t.try_acquire("Vendor A Pro", 10)

        picked = acquire_provider_with_dynamic_capacity(
            CHAIN, t, FULL, timeout=0.1, poll_interval=0.01,
        )

        assert picked == "Vendor A", "the second pool must take the overflow"
        assert t.current("Vendor A") == 1
        assert t.current("Vendor A Pro") == 10

    def test_three_full_pools_queue_rather_than_overfill(self):
        """All full → wait, do not pile a 31st call onto the first pool."""
        t = ActiveConcurrencyTracker()
        for name in CHAIN:
            for _ in range(10):
                t.try_acquire(name, 10)

        started = time.monotonic()
        picked = acquire_provider_with_dynamic_capacity(
            CHAIN, t, FULL, timeout=0.3, poll_interval=0.01,
        )
        elapsed = time.monotonic() - started

        assert picked is None
        assert elapsed >= 0.25, "it must actually wait, not return instantly"
        assert t.total() == 30, "nothing extra was taken"

    def test_a_queued_caller_runs_as_soon_as_a_slot_frees(self):
        """The point of the wait: the fleet stays saturated, not starved."""
        t = ActiveConcurrencyTracker()
        for name in CHAIN:
            for _ in range(10):
                t.try_acquire(name, 10)

        def _free_a_slot():
            time.sleep(0.05)
            t.release("Vendor A")

        threading.Thread(target=_free_a_slot).start()
        picked = acquire_provider_with_dynamic_capacity(
            CHAIN, t, FULL, timeout=5.0, poll_interval=0.01,
        )

        assert picked == "Vendor A"
        assert t.current("Vendor A") == 10, "the freed slot was re-taken"

    def test_exhausted_quota_does_not_spin_until_timeout(self):
        """A 0% provider is out of budget — waiting cannot help it."""
        t = ActiveConcurrencyTracker()
        usage = {"Vendor A Pro": 0.0, "Vendor A": 0.0, "Vendor A API": 0.0}

        started = time.monotonic()
        picked = acquire_provider_with_dynamic_capacity(
            CHAIN, t, usage, timeout=5.0, poll_interval=0.01,
        )
        elapsed = time.monotonic() - started

        assert picked is None
        assert elapsed < 1.0, (
            "waiting for a provider with no quota burns the caller's "
            "timeout for a pool that cannot serve it"
        )

    def test_reduced_quota_shrinks_the_pool(self):
        t = ActiveConcurrencyTracker()
        usage = {**FULL, "Vendor A Pro": 60.0}   # limit 6
        for _ in range(6):
            t.try_acquire("Vendor A Pro", 10)

        assert acquire_provider_with_dynamic_capacity(
            CHAIN, t, usage, timeout=0.1, poll_interval=0.01,
        ) == "Vendor A"

    def test_parent_is_never_a_pool(self):
        t = ActiveConcurrencyTracker()
        assert acquire_provider_with_dynamic_capacity(
            ["parent"], t, {}, timeout=0.05, poll_interval=0.01,
        ) is None

    def test_empty_chain_returns_none(self):
        t = ActiveConcurrencyTracker()
        assert acquire_provider_with_dynamic_capacity([], t, {}) is None

    def test_missing_usage_entry_is_treated_as_full_quota(self):
        t = ActiveConcurrencyTracker()
        assert acquire_provider_with_dynamic_capacity(
            ["Vendor A"], t, {}, timeout=0.05, poll_interval=0.01,
        ) == "Vendor A"


# ---------------------------------------------------------------------------
# 4. The process-wide handle
# ---------------------------------------------------------------------------


class TestSharedTracker:
    def test_get_returns_a_stable_instance(self):
        assert get_shared_tracker() is get_shared_tracker()

    def test_set_replaces_the_handle(self):
        original = get_shared_tracker()
        try:
            replacement = ActiveConcurrencyTracker()
            set_shared_tracker(replacement)
            assert get_shared_tracker() is replacement
        finally:
            set_shared_tracker(original)

    def test_reset_to_none_relazily_creates(self):
        original = get_shared_tracker()
        try:
            set_shared_tracker(None)
            fresh = get_shared_tracker()
            assert fresh is not None
            assert fresh.total() == 0
        finally:
            set_shared_tracker(original)
