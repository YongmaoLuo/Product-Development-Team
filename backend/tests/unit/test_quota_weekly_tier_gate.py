"""A provider that lost its weekly window is treated as out of week.

What this pins
--------------
CC Switch reports the quota windows a provider *currently* carries.
Some providers stop reporting their weekly window mid-session — the
``MiniMax Token Plan Huawei`` row, for one, comes back carrying only
``five_hour``.  Read on its own, such a provider looks *idle*: its burst
window says 100% left, so the dispatcher keeps handing it work while
the account's week is already spent.

A reading is not the only evidence there is.  The provider history
(:mod:`quota_tier_memory`, carried on
:attr:`provider_order.ProviderQuota.weekly_tier_seen`) remembers that
this provider has carried a ``weekly_limit`` before, so a missing
weekly window is a *lost* window rather than "this provider has no week
to lose".  A lost week is treated as an exhausted week: no dispatch,
and no slots either.

The two absences must stay apart, and this file pins both halves:

* **missing after seen** -- the provider is known to have a week, so
  the absence counts as an exhausted week: dispatch is refused and the
  headroom is zero;
* **never seen** -- the provider has never had a week (a metered API
  product), so there is nothing to exhaust and the gate stays open;
* **no reading at all** -- unchanged: a provider CC Switch cannot
  meter is admitted, because a missing reading is not evidence.

What these tests pin:

* :func:`dynamic_provider_concurrency.quota_allows_dispatch` refuses a
  provider whose week went missing, and only that one;
* :func:`dynamic_provider_concurrency.quota_remaining_pct` reports
  ``0.0`` for it, so the cap scaling follows —
  :func:`dynamic_provider_concurrency.provider_headroom_pct` hands that
  ``0.0`` to :func:`dynamic_provider_concurrency.compute_dynamic_limit`,
  which answers ``0``;
* the fail-open cases stay fail-open: no reading, a week never seen, a
  reading that still carries its week, and a stand-in object that
  predates the weekly attributes.

No test here touches the network or the quota memory file: the readings
are built directly, so the file this change is about is never written.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dynamic_provider_concurrency import (  # noqa: E402
    compute_dynamic_limit,
    provider_headroom_pct,
    quota_allows_dispatch,
    quota_remaining_pct,
)
from provider_order import ProviderQuota  # noqa: E402

#: A provider name spelled the way CC Switch spells it.
PROVIDER = "MiniMax Token Plan Huawei"

#: The window whose absence the failover rule cares about.
WEEKLY = "weekly_limit"


def _quota(**kwargs) -> ProviderQuota:
    """A reading for :data:`PROVIDER` with the given fields overridden."""
    fields = {"provider": PROVIDER, "remaining_pct": 100.0}
    fields.update(kwargs)
    return ProviderQuota(**fields)


class _StandIn:
    """A reading-shaped object from before the weekly attributes existed.

    Callers hand :func:`quota_allows_dispatch` whatever the quota map
    holds, and the map is not typed.  An object that has no
    ``weekly_tier_seen`` and no ``tiers_missing`` must be read as "no
    week to lose" rather than raising.
    """

    provider = PROVIDER
    remaining_pct = 100.0
    exhausted = False


def test_missing_weekly_after_seen_blocks():
    """A week that went missing on a provider known to have one blocks.

    This is the case the whole change is about: the burst window still
    says 100% left, but the account's week has stopped being reported
    and the provider has been seen to carry one before.
    """
    quota = _quota(tiers_missing=(WEEKLY,), weekly_tier_seen=True)

    assert quota_allows_dispatch(quota) is False


def test_never_seen_weekly_stays_open():
    """A provider that never had a weekly window is not blocked.

    Same absence in the reading, opposite meaning: a metered API
    product has no week to exhaust, so the gate must stay open.
    """
    quota = _quota(tiers_missing=(WEEKLY,), weekly_tier_seen=False)

    assert quota_allows_dispatch(quota) is True


def test_gate_zeroes_remaining_pct():
    """The blocked reading reports no headroom.

    Zero, not ``None``: ``None`` means "this reading says nothing" and
    the caller falls back to another source, while ``0.0`` is the
    reading's own answer — no room.  That is what makes the cap
    scaling follow.
    """
    quota = _quota(tiers_missing=(WEEKLY,), weekly_tier_seen=True)

    assert quota_remaining_pct(quota) == 0.0


def test_no_reading_stays_open():
    """A provider with no reading at all is still admitted."""
    assert quota_allows_dispatch(None) is True
    assert quota_remaining_pct(None) is None


def test_weekly_window_present_is_unchanged():
    """A reading that still carries its week is not gated.

    The gate is about the *absence* of a window, so a reading that has
    one keeps its own verdict and its own headroom.
    """
    quota = _quota(tiers_missing=(), weekly_tier_seen=True, remaining_pct=42.0)

    assert quota_allows_dispatch(quota) is True
    assert quota_remaining_pct(quota) == 42.0


def test_exhausted_reading_is_still_blocked():
    """An exhausted reading stays blocked, whatever the week says."""
    quota = _quota(exhausted=True, tiers_missing=(), weekly_tier_seen=False)

    assert quota_allows_dispatch(quota) is False


def test_stand_in_without_weekly_attributes_is_tolerated():
    """A stand-in reading with no weekly fields is read as "no week"."""
    quota = _StandIn()

    assert quota_allows_dispatch(quota) is True
    assert quota_remaining_pct(quota) == 100.0


def test_gate_shrinks_the_cap_to_zero():
    """The blocked provider gets no slots either.

    The headroom is what scales the cap, so a provider whose week went
    missing is not merely refused — it is refused *and* scaled to
    nothing, so a caller that only looks at capacity sees the same
    answer the gate gives.
    """
    quota = _quota(tiers_missing=(WEEKLY,), weekly_tier_seen=True)

    headroom = provider_headroom_pct(PROVIDER, {PROVIDER: 100.0}, {PROVIDER: quota})

    assert headroom == 0.0
    assert compute_dynamic_limit(PROVIDER, headroom) == 0


def test_open_reading_keeps_its_cap():
    """A provider that was never seen to have a week keeps its slots."""
    quota = _quota(tiers_missing=(WEEKLY,), weekly_tier_seen=False)

    headroom = provider_headroom_pct(PROVIDER, {PROVIDER: 100.0}, {PROVIDER: quota})

    assert headroom == 100.0
    assert compute_dynamic_limit(PROVIDER, headroom) > 0
