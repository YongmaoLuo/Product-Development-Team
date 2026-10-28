"""A quota reading remembers the weekly window it has seen before.

What this pins
--------------
CC Switch reports the quota windows a provider *currently* carries.  A
provider on a plan with a weekly limit reports a ``weekly_limit`` tier
next to its ``five_hour`` burst window; a provider on a metered API
product reports no weekly tier at all.

Those two facts look identical in a single reading — "this row carries no
weekly tier" — but they are not the same fact, and the failover
architecture (decision point 1) needs them apart: a missing weekly tier
counts as an exhausted weekly tier, but only for a provider that has
*ever* been seen to carry one.  A provider that has no weekly window at
all must not be killed by the rule.

So the reading has to answer a question the reading itself cannot: has
this provider ever reported a weekly window?  The answer comes from
:mod:`quota_tier_memory`, which carries the history across processes, and
:func:`provider_order._quota_from_record` is what ties the two together.

What these tests pin:

* **present** -- a row carrying ``weekly_limit`` is recorded, and the
  verdict says the provider has a weekly window;
* **missing after seen** -- a row that stopped carrying ``weekly_limit``
  is flagged, because the provider is known to have one;
* **never seen** -- a provider that has only ever reported
  ``five_hour`` is *not* flagged, because it never had a week to lose;
* **nothing observed** -- a row that carried no tiers at all (an
  unreadable row) writes nothing, so it cannot invent a history it did
  not see.

And the boundary that keeps the dispatch path alive: a memory that
raises must not raise out of the reading.  The memory is history, not
evidence — losing it costs a decision, but it must not fail a dispatch
that was otherwise fine.

No test here reaches the network: the payloads are the shapes the reader
is documented against.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_order  # noqa: E402
import quota_tier_memory  # noqa: E402
from provider_order import _quota_from_record  # noqa: E402

#: A provider name spelled the way CC Switch spells it.
PROVIDER = "MiniMax Token Plan Huawei"

#: A fixed "now", so a tier's ``resets_at`` is judged against a known
#: moment rather than against the wall clock.
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

#: The tier whose absence the failover rule cares about.
WEEKLY = "weekly_limit"


def _record(*tier_names: str) -> dict:
    """A ``/api/usage`` row carrying exactly ``tier_names``."""
    return {
        "success": True,
        "tiers": [{"name": name, "used_percent": 0} for name in tier_names],
    }


def test_weekly_tier_present_is_recorded():
    """A row carrying ``weekly_limit`` is remembered and reported."""
    quota = _quota_from_record(PROVIDER, _record("five_hour", WEEKLY), NOW)

    # The window is there, so it is not missing from this reading.
    assert quota.tiers_missing == ()
    # And the provider is known to carry one.
    assert quota.weekly_tier_seen is True
    # The memory really took the fact, not just the verdict.
    assert quota_tier_memory.has_seen_tier(PROVIDER, WEEKLY) is True


def test_missing_tier_after_seen_is_flagged():
    """A weekly window that stopped being reported is flagged."""
    _quota_from_record(PROVIDER, _record("five_hour", WEEKLY), NOW)

    quota = _quota_from_record(PROVIDER, _record("five_hour"), NOW)

    # This reading did not carry it ...
    assert quota.tiers_missing == (WEEKLY,)
    # ... but the provider is known to have one, so it is not "never had
    # a weekly window" — it is "lost one".
    assert quota.weekly_tier_seen is True


def test_never_seen_tier_is_not_flagged():
    """A provider that never had a weekly window is not flagged."""
    quota = _quota_from_record(PROVIDER, _record("five_hour"), NOW)

    assert quota.tiers_missing == (WEEKLY,)
    assert quota.weekly_tier_seen is False


def test_unreadable_row_writes_nothing():
    """A row CC Switch could not meter writes no memory at all."""
    quota = _quota_from_record(PROVIDER, {"success": False}, NOW)

    assert quota.unreadable is True
    assert quota.weekly_tier_seen is False
    assert quota_tier_memory.seen_tiers(PROVIDER) == ()


def test_row_without_tiers_writes_nothing():
    """A readable row that carried no tiers writes no memory either."""
    quota = _quota_from_record(PROVIDER, {"success": True}, NOW)

    assert quota.unreadable is True
    assert quota.weekly_tier_seen is False
    assert quota_tier_memory.seen_tiers(PROVIDER) == ()


def test_memory_failure_does_not_break_the_reading(monkeypatch):
    """A memory that raises must not raise out of the reading.

    The memory is history, not evidence: losing it costs the caller a
    decision, but a dispatch that was otherwise fine must still get its
    verdict.
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("the memory is unavailable")

    monkeypatch.setattr(quota_tier_memory, "record_seen_tiers", _boom)
    monkeypatch.setattr(quota_tier_memory, "seen_tiers", _boom)

    quota = _quota_from_record(PROVIDER, _record("five_hour", WEEKLY), NOW)

    assert quota.weekly_tier_seen is False
    # The reading itself is unharmed: the tiers are still classified.
    assert quota.tiers_missing == ()
    assert quota.exhausted is False


def test_memory_is_written_to_the_redirected_file(tmp_path):
    """The memory lands in ``tmp_path``, never in the operator's config.

    The redirect is the ``backend/tests/conftest.py`` autouse fixture:
    without it the suite would write the operator's real
    ``.config`` directory, and the next run on this machine would
    answer from a history this suite made up.
    """
    _quota_from_record(PROVIDER, _record("five_hour", WEEKLY), NOW)

    memory_path = quota_tier_memory.memory_file_path()
    assert memory_path.parent == tmp_path
    assert memory_path.exists()
