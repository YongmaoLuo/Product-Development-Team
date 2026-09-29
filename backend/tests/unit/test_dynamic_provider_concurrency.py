"""
TDD tests for ``backend/dynamic_provider_concurrency.py``.

Pins the contract of dynamic per-provider max concurrency scaled by
each provider's 5-hour remaining quota:

  * 100% remaining → base cap (vendor-a-pro=5, vendor-b-pro=3)  # kebab-case test_provider marker
  * 60% remaining  → floor(base * 0.6 + 0.5) (vendor-a-pro=3, vendor-b-pro=2)  # kebab-case test_provider marker
  * 0% remaining   → 0 (caller falls back to parent config)
  * ``parent``     → never selected by this strategy
                    (parent is the caller's explicit fallback)

The module is a sibling of ``provider_concurrency.py`` (which carries
the dual-layer global+per-provider semaphore controller). This one
focuses on the *selection* contract: given a fallback chain, a live
counter, and a 5h-usage map, return the first provider with spare
capacity, or ``None`` if the chain is exhausted.

Rounding rule
-------------
The user's spec pins "四舍五入" (round-half-up). ``int(math.floor(x +
0.5))`` is the canonical implementation: 0.5 → 1, 1.5 → 2, 2.5 → 3.
Python's built-in :func:`round` uses banker's rounding (half-to-even)
and produces 2 for 2.5 — wrong for this spec.

Thread safety
-------------
:func:`compute_dynamic_limit` is pure (no shared state).
:class:`ActiveConcurrencyTracker` uses a single :class:`threading.Lock`
to serialize increment / decrement / current-count access. The lock is
held only for the dict mutation, not for the caller-visible critical
section — contention is bounded by the duration of a dict update.
"""

from __future__ import annotations

import concurrent.futures
import math
import threading
from datetime import datetime, timedelta, timezone

import pytest

from dynamic_provider_concurrency import (
    DEFAULT_MAX_CONCURRENCY_AT_FULL,
    ActiveConcurrencyTracker,
    DynamicProviderSelectionError,
    acquired_provider_slot,
    compute_dynamic_limit,
    round_half_up,
    select_provider_with_dynamic_capacity,
)


# Caps come from ``provider_capacity.yaml`` rules. The classes below
# declare the rules they need through the ``capacity_config`` fixture
# rather than relying on a table in source, so the arithmetic under test
# (a base cap of 10 scaling to 6 at 60 %) stays visible and the caps stay
# a property of the configuration.
#
# ``vendor-a-pro`` / ``vendor-b-pro`` are stand-ins for two providers with
# different caps; ``kebab-case test_provider marker`` marks them for the
# repo-wide static scan, which allows that spelling in tests.
CAPS_FIXTURE_PROVIDERS = [("^vendor-a-pro", 10), ("^vendor-b-pro", 3)]


@pytest.fixture
def caps(capacity_config):
    """Ten for one provider, three for another."""
    capacity_config(CAPS_FIXTURE_PROVIDERS)


# ---------------------------------------------------------------------------
# 0. round_half_up — the rounding primitive
# ---------------------------------------------------------------------------


class TestRoundHalfUp:
    """``round_half_up(x) == math.floor(x + 0.5)`` for non-negative x."""

    def test_zero(self):
        assert round_half_up(0) == 0

    def test_below_half(self):
        # 0.4 → floor(0.9) = 0
        assert round_half_up(0.4) == 0
        # 1.4 → floor(1.9) = 1
        assert round_half_up(1.4) == 1

    def test_exactly_half(self):
        # 0.5 → floor(1.0) = 1
        assert round_half_up(0.5) == 1
        # 1.5 → floor(2.0) = 2
        assert round_half_up(1.5) == 2
        # 2.5 → floor(3.0) = 3 (NOT banker's rounding 2)
        assert round_half_up(2.5) == 3

    def test_above_half(self):
        assert round_half_up(0.6) == 1
        assert round_half_up(2.6) == 3

    def test_above_one(self):
        # 5.7 → 6
        assert round_half_up(5.7) == 6


# ---------------------------------------------------------------------------
# 1. compute_dynamic_limit
# ---------------------------------------------------------------------------


class TestComputeDynamicLimit:
    """compute_dynamic_limit(provider, remaining_pct) → int (>=0)."""

    @pytest.fixture(autouse=True)
    def _caps(self, caps):
        """Every test here works against the declared caps."""

    def test_vendor_a_full(self):  # kebab-case test_provider marker
        """vendor-a-pro at 100% → its configured cap (10)."""  # kebab-case test_provider marker
        assert compute_dynamic_limit("vendor-a-pro", 100.0) == 10  # kebab-case test_provider marker

    def test_vendor_b_full(self):
        """vendor-b-pro at 100% → base cap (3)."""
        assert compute_dynamic_limit("vendor-b-pro", 100.0) == 3

    def test_vendor_a_60_percent(self):  # kebab-case test_provider marker
        """vendor-a-pro at 60% → 6 (10 * 0.6 = 6.0 → 6)."""  # kebab-case test_provider marker
        assert compute_dynamic_limit("vendor-a-pro", 60.0) == 6  # kebab-case test_provider marker

    def test_vendor_b_60_percent(self):
        """vendor-b-pro at 60% → 2 (3 * 0.6 = 1.8 → round-half-up = 2)."""
        assert compute_dynamic_limit("vendor-b-pro", 60.0) == 2

    def test_zero_remaining_returns_zero(self):
        """0% remaining → 0 (caller must fall back to parent)."""
        assert compute_dynamic_limit("vendor-a-pro", 0) == 0  # kebab-case test_provider marker
        assert compute_dynamic_limit("vendor-b-pro", 0) == 0

    def test_negative_remaining_clamped_to_zero(self):
        """-5% → 0 (defensive: bad CC Switch data must not give capacity)."""
        assert compute_dynamic_limit("vendor-a-pro", -5.0) == 0  # kebab-case test_provider marker

    def test_over_100_clamped_to_base(self):
        """150% (sentinel) → base cap, never above the cap."""
        assert compute_dynamic_limit("vendor-a-pro", 150.0) == 10  # kebab-case test_provider marker
        assert compute_dynamic_limit("vendor-b-pro", 200.0) == 3

    def test_unknown_provider_uses_default(self):
        """A provider matching no rule gets the documented default (5)."""
        assert compute_dynamic_limit("vendor-c-app", 100.0) == DEFAULT_MAX_CONCURRENCY_AT_FULL  # kebab-case test_provider marker

    def test_configured_caps_reach_both_spellings(self, capacity_config):
        """A rule is matched against the name and its canonical form.

        The scene path yields CC Switch display names and the executor
        path yields their kebab-case equivalents; one rule has to cover
        both or the same provider would get two different caps depending
        on which path dispatched it. This is why the module carries no
        name→id translation table.
        """
        capacity_config([("^vendor a", 7)])

        assert compute_dynamic_limit("Vendor A", 100.0) == 7
        assert compute_dynamic_limit("vendor-a", 100.0) == 7
        assert compute_dynamic_limit("VENDOR_A", 100.0) == 7

    def test_first_matching_rule_wins(self, capacity_config):
        """Rules are ordered; the narrow one goes first or it never fires."""
        capacity_config([("^vendor", 2), ("^vendor special", 9)])

        assert compute_dynamic_limit("Vendor Standard", 100.0) == 2
        assert compute_dynamic_limit("Vendor Special", 100.0) == 2, (
            "a broad rule listed first shadows the narrow one — the "
            "ordering contract, pinned so it cannot silently invert"
        )

    def test_distinct_rows_get_distinct_caps(self, capacity_config):
        """Two rows sharing a prefix are separate quota pools.

        They are separate CC Switch rows with separate credentials, so a
        rule per row is what keeps them from sharing one slot counter.
        """
        capacity_config([("^vendor a$", 10), ("^vendor a pro", 4)])

        assert compute_dynamic_limit("Vendor A", 100.0) == 10
        assert compute_dynamic_limit("Vendor A Pro", 100.0) == 4

    def test_unknown_provider_proportional(self):
        """Unknown provider scales proportionally from the default base."""
        # 5 * 0.6 = 3.0 → 3
        assert compute_dynamic_limit("vendor-c-app", 60.0) == 3  # kebab-case test_provider marker
        # 5 * 0.5 = 2.5 → 3
        assert compute_dynamic_limit("vendor-c-app", 50.0) == 3  # kebab-case test_provider marker

    def test_round_half_up_at_vendor_a_50_percent(self):  # kebab-case test_provider marker
        """vendor-a-pro 50% → 10 * 0.5 = 5.0 → 5, exactly on the base/2 line."""  # kebab-case test_provider marker
        result = compute_dynamic_limit("vendor-a-pro", 50.0)  # kebab-case test_provider marker
        assert result == 5, (
            f"vendor-a-pro 50% should be half its base cap (5), got {result}"  # kebab-case test_provider marker
        )

    def test_round_half_up_at_vendor_b_50_percent(self):
        """vendor-b-pro 50% → 3 * 0.5 = 1.5 → round-half-up → 2."""
        result = compute_dynamic_limit("vendor-b-pro", 50.0)
        assert result == 2, f"vendor-b-pro 50% should round-half-up to 2, got {result}"

    def test_fractional_pct(self):
        """Fractional percentages behave predictably."""
        # vendor-a-pro at 10% → 10 * 0.1 = 1.0 → 1  # kebab-case test_provider marker
        assert compute_dynamic_limit("vendor-a-pro", 10.0) == 1  # kebab-case test_provider marker
        # vendor-a-pro at 4% → 10 * 0.04 = 0.4 → round-half-up = 0  # kebab-case test_provider marker
        assert compute_dynamic_limit("vendor-a-pro", 4.0) == 0  # kebab-case test_provider marker


# ---------------------------------------------------------------------------
# 2. ActiveConcurrencyTracker
# ---------------------------------------------------------------------------


class TestActiveConcurrencyTracker:
    """Thread-safe per-provider active-count tracker."""

    def test_starts_at_zero(self):
        """New tracker reports 0 for any provider."""
        t = ActiveConcurrencyTracker()
        assert t.current("vendor-a-pro") == 0  # kebab-case test_provider marker
        assert t.current("vendor-b-pro") == 0

    def test_acquire_increments(self):
        """Each ``acquire(p)`` raises the count for p by exactly 1."""
        t = ActiveConcurrencyTracker()
        t.acquire("vendor-a-pro")  # kebab-case test_provider marker
        assert t.current("vendor-a-pro") == 1  # kebab-case test_provider marker
        t.acquire("vendor-a-pro")  # kebab-case test_provider marker
        assert t.current("vendor-a-pro") == 2  # kebab-case test_provider marker

    def test_release_decrements(self):
        """Each ``release(p)`` lowers the count for p by exactly 1."""
        t = ActiveConcurrencyTracker()
        t.acquire("vendor-a-pro")  # kebab-case test_provider marker
        t.acquire("vendor-a-pro")  # kebab-case test_provider marker
        t.release("vendor-a-pro")  # kebab-case test_provider marker
        assert t.current("vendor-a-pro") == 1  # kebab-case test_provider marker
        t.release("vendor-a-pro")  # kebab-case test_provider marker
        assert t.current("vendor-a-pro") == 0  # kebab-case test_provider marker

    def test_release_below_zero_clamps_to_zero(self):
        """Defensive: over-release never goes negative."""
        t = ActiveConcurrencyTracker()
        t.release("vendor-a-pro")  # kebab-case test_provider marker
        assert t.current("vendor-a-pro") == 0  # kebab-case test_provider marker
        t.release("vendor-b-pro")
        assert t.current("vendor-b-pro") == 0

    def test_per_provider_isolation(self):
        """Counts are per-provider, not a global pool."""
        t = ActiveConcurrencyTracker()
        t.acquire("vendor-a-pro")  # kebab-case test_provider marker
        t.acquire("vendor-a-pro")  # kebab-case test_provider marker
        t.acquire("vendor-b-pro")
        assert t.current("vendor-a-pro") == 2  # kebab-case test_provider marker
        assert t.current("vendor-b-pro") == 1
        assert t.current("vendor-c-app") == 0  # kebab-case test_provider marker

    def test_acquire_release_returns_new_count(self):
        """acquire/release return the post-mutation count (callers may log it)."""
        t = ActiveConcurrencyTracker()
        assert t.acquire("vendor-a-pro") == 1  # kebab-case test_provider marker
        assert t.acquire("vendor-a-pro") == 2  # kebab-case test_provider marker
        assert t.release("vendor-a-pro") == 1  # kebab-case test_provider marker

    def test_concurrent_acquire_release_thread_safe(self):
        """100 concurrent acquires from 10 threads yield count == 100."""
        t = ActiveConcurrencyTracker()

        def hammer():
            for _ in range(100):
                t.acquire("vendor-a-pro")  # kebab-case test_provider marker

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(hammer) for _ in range(10)]
            for f in futures:
                f.result()

        assert t.current("vendor-a-pro") == 1000  # kebab-case test_provider marker

    def test_concurrent_mixed_acquire_release_balances(self):
        """Balanced acquire/release under threads converges to 0."""
        t = ActiveConcurrencyTracker()

        def worker():
            for _ in range(50):
                t.acquire("vendor-a-pro")  # kebab-case test_provider marker
            for _ in range(50):
                t.release("vendor-a-pro")  # kebab-case test_provider marker

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(worker) for _ in range(10)]
            for f in futures:
                f.result()

        assert t.current("vendor-a-pro") == 0  # kebab-case test_provider marker


# ---------------------------------------------------------------------------
# 3. select_provider_with_dynamic_capacity
# ---------------------------------------------------------------------------


def _beijing_midnight(year: int = 2026, month: int = 6, day: int = 15) -> datetime:
    """Return a Beijing-local midnight (a neutral, fixed clock)."""
    return datetime(year, month, day, 0, 0, tzinfo=timezone(timedelta(hours=8)))


def _beijing_peak(year: int = 2026, month: int = 6, day: int = 15, hour: int = 15) -> datetime:
    """Return a Beijing-local peak hour (15:00 → in 14-18 window)."""
    return datetime(year, month, day, hour, 0, tzinfo=timezone(timedelta(hours=8)))


class TestSelectProviderDynamic:
    """select_provider_with_dynamic_capacity walks the chain."""

    @pytest.fixture(autouse=True)
    def _caps(self, caps):
        """Every test here works against the declared caps."""

    def test_returns_first_provider_with_capacity(self):
        """Empty tracker + full quota → first chain entry returned."""
        tracker = ActiveConcurrencyTracker()
        usage = {"vendor-a-pro": 100.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-a-pro"  # kebab-case test_provider marker

    def test_skips_provider_at_dynamic_limit(self):
        """vendor-a-pro at dynamic cap → fallback to vendor-b-pro."""  # kebab-case test_provider marker
        tracker = ActiveConcurrencyTracker()
        # Fill vendor-a-pro to its cap (10 at 100%).  # kebab-case test_provider marker
        for _ in range(10):
            tracker.acquire("vendor-a-pro")  # kebab-case test_provider marker
        usage = {"vendor-a-pro": 100.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-b-pro"

    def test_returns_none_when_all_at_capacity(self):
        """Both providers full → None (caller falls back to parent config)."""
        tracker = ActiveConcurrencyTracker()
        for _ in range(10):
            tracker.acquire("vendor-a-pro")  # kebab-case test_provider marker
        for _ in range(3):
            tracker.acquire("vendor-b-pro")
        usage = {"vendor-a-pro": 100.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result is None

    def test_dynamic_limit_scales_with_usage(self):
        """At 60% vendor-a-pro has limit=6; 6 active → skip, fallback to vendor-b-pro."""  # kebab-case test_provider marker
        tracker = ActiveConcurrencyTracker()
        for _ in range(6):
            tracker.acquire("vendor-a-pro")  # kebab-case test_provider marker
        # vendor-a-pro at 60% → limit 6; 6 active means capacity exhausted.  # kebab-case test_provider marker
        usage = {"vendor-a-pro": 60.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-b-pro"

    def test_dynamic_limit_allows_under_threshold(self):
        """At 60% vendor-a-pro has limit=6; 2 active → still has capacity."""  # kebab-case test_provider marker
        tracker = ActiveConcurrencyTracker()
        for _ in range(2):
            tracker.acquire("vendor-a-pro")  # kebab-case test_provider marker
        usage = {"vendor-a-pro": 60.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-a-pro"  # kebab-case test_provider marker

    def test_zero_usage_means_no_capacity(self):
        """Provider at 0% remaining → limit=0 → always skipped."""
        tracker = ActiveConcurrencyTracker()
        usage = {"vendor-a-pro": 0.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-b-pro"

    def test_missing_usage_defaults_to_full_capacity(self):
        """Provider missing from usage map → treated as 100% remaining."""
        tracker = ActiveConcurrencyTracker()
        # vendor-a-pro not in usage map.  # kebab-case test_provider marker
        usage = {"vendor-b-pro": 100.0}
        priority = ["vendor-a-pro", "vendor-b-pro"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-a-pro"  # kebab-case test_provider marker

    def test_parent_entry_never_selected_by_dynamic_strategy(self):
        """``parent`` is the caller's explicit fallback, not this strategy's choice."""
        tracker = ActiveConcurrencyTracker()
        for _ in range(10):
            tracker.acquire("vendor-a-pro")  # kebab-case test_provider marker
        for _ in range(3):
            tracker.acquire("vendor-b-pro")
        usage = {"vendor-a-pro": 100.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "vendor-b-pro", "parent"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result is None, (
            f"select_provider_with_dynamic_capacity must NOT return 'parent'; "
            f"got {result!r}"
        )

    def test_parent_skipped_even_when_no_other_provider_full(self):
        """Even with a parent entry and empty tracker, the strategy returns the real provider."""
        tracker = ActiveConcurrencyTracker()
        usage = {"vendor-a-pro": 100.0, "vendor-b-pro": 100.0}  # kebab-case test_provider marker
        priority = ["vendor-a-pro", "parent"]  # kebab-case test_provider marker

        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=_beijing_midnight()
        )

        assert result == "vendor-a-pro"  # kebab-case test_provider marker

    def test_empty_priority_returns_none(self):
        """Empty chain → None (caller falls back to parent)."""
        tracker = ActiveConcurrencyTracker()
        result = select_provider_with_dynamic_capacity(
            [], tracker, {}, now=_beijing_midnight()
        )
        assert result is None

    def test_now_defaults_to_current_time(self):
        """``now=None`` uses datetime.now() — off-peak Beijing time."""
        tracker = ActiveConcurrencyTracker()
        usage = {"vendor-b-pro": 100.0}
        priority = ["vendor-b-pro", "vendor-a-pro"]  # kebab-case test_provider marker

        # At runtime, datetime.now() may be peak or off-peak depending on
        # the wall clock; pin the test to a known-off-peak value via the
        # ``now`` parameter for determinism. This test just pins that
        # ``now=None`` is accepted without raising.
        result = select_provider_with_dynamic_capacity(
            priority, tracker, usage, now=None
        )
        # Result is either vendor-b-pro (off-peak) or vendor-a-pro (peak); the only  # kebab-case test_provider marker
        # contract pin is "no exception, returned str or None".
        assert result in (None, "vendor-b-pro", "vendor-a-pro")  # kebab-case test_provider marker


# ---------------------------------------------------------------------------
# 4. acquired_provider_slot — context manager
# ---------------------------------------------------------------------------


class TestAcquiredProviderSlot:
    """acquired_provider_slot(p) increments on enter, decrements on exit."""

    def test_increments_on_enter_decrements_on_exit(self):
        tracker = ActiveConcurrencyTracker()
        assert tracker.current("vendor-a-pro") == 0  # kebab-case test_provider marker
        with acquired_provider_slot("vendor-a-pro", tracker):  # kebab-case test_provider marker
            assert tracker.current("vendor-a-pro") == 1  # kebab-case test_provider marker
        assert tracker.current("vendor-a-pro") == 0  # kebab-case test_provider marker

    def test_releases_on_exception(self):
        """An exception inside the with-block must still release the slot."""
        tracker = ActiveConcurrencyTracker()
        try:
            with acquired_provider_slot("vendor-a-pro", tracker):  # kebab-case test_provider marker
                assert tracker.current("vendor-a-pro") == 1  # kebab-case test_provider marker
                raise RuntimeError("subagent crashed")
        except RuntimeError:
            pass
        assert tracker.current("vendor-a-pro") == 0  # kebab-case test_provider marker

    def test_nested_calls_per_provider(self):
        """Two sequential context-manager entries on the same provider net to 0."""
        tracker = ActiveConcurrencyTracker()
        for _ in range(2):
            with acquired_provider_slot("vendor-b-pro", tracker):
                assert tracker.current("vendor-b-pro") == 1
            assert tracker.current("vendor-b-pro") == 0