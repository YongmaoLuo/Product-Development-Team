"""An ApiError with no status is a rate limit, not an excuse to drop the provider.

The defect this pins: a provider failure that carries no status code at all
used to land in the bucket that excludes a provider from the pool outright —
even when that provider still had budget to spend and the failure was
nothing but a shrug from the upstream. The provider was never slow and never
out of budget; the failure simply carried no status, so the classification
had nothing to go on and fell through to the bucket that discards
providers.

A missing status is not evidence of a bad request. It is evidence that the
provider answered without one — which is what an overloaded or throttled
upstream looks like from here. So a status that is empty (``None``, ``""``
or whitespace) is read as ``rate_limited``: the SAME provider is waited out
(5s / 10s / 20s / 40s) and asked again, and only a refusal that survives
every wait excludes it. That is the behaviour the other three buckets
already have; this test pins the fourth.

What must not move:

  * a status that IS present keeps its own bucket — 400 is ``fatal``,
    429 is ``rate_limited``, 500 is ``other``;
  * text that reads as an exhaustion still wins over the status
    (``2056`` -> ``exhausted``), and so does a live reading that says the
    budget is gone;
  * :func:`dynamic_provider_concurrency.classify_capacity_failure` itself
    is untouched — the empty-status rule lives in the wrapper that calls
    it, because the public function has another caller
    (``capacity_wait``) whose contract must not change.

Nothing here contacts CC Switch or reads the payload file: the quota map
is stubbed empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_order  # noqa: E402
from coding_tool import ApiError, _classify_provider_failure  # noqa: E402

#: The provider the incident happened on.
PROVIDER = "MiniMax Token Plan mother"

#: The vendor's spent-plan refusal — the text that reads as an
#: exhaustion, so an empty status must not paper over it.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)

#: A failure that says nothing in particular: no exhaustion marker, no
#: rate-limit marker, and no status code to go on.
NEUTRAL_TEXT = "upstream hiccup"


@pytest.fixture(autouse=True)
def _no_live_reading(monkeypatch):
    """Each test starts with no live reading at all, and stays hermetic.

    The quota map is stubbed empty, so no CC Switch is contacted and no
    reading exists for any provider. The read state is reset too: the
    reason of the last read lives in module state that
    :func:`provider_order.cache_clear` does not touch, so a test that ran
    earlier in the session could otherwise hand this one a reason of its
    own.
    """
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {}
    )
    yield
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())


# ---------------------------------------------------------------------------
# 1. The empty status
# ---------------------------------------------------------------------------


def test_status_none_without_text_is_rate_limited():
    """No status and no marker: wait the provider out, do not drop it."""
    exc = ApiError(NEUTRAL_TEXT, status=None)

    assert _classify_provider_failure(exc, PROVIDER) == "rate_limited"


def test_blank_status_is_rate_limited():
    """An empty string is an empty status too."""
    exc = ApiError(NEUTRAL_TEXT, status="")

    assert _classify_provider_failure(exc, PROVIDER) == "rate_limited"


def test_whitespace_status_is_rate_limited():
    """Whitespace is not a status code either."""
    exc = ApiError(NEUTRAL_TEXT, status="   ")

    assert _classify_provider_failure(exc, PROVIDER) == "rate_limited"


# ---------------------------------------------------------------------------
# 2. What the empty status must not swallow
# ---------------------------------------------------------------------------


def test_status_none_with_exhaustion_text_is_exhausted():
    """The text outranks the missing status."""
    exc = ApiError(EXHAUSTION_TEXT, status=None)

    assert _classify_provider_failure(exc, PROVIDER) == "exhausted"


def test_live_exhausted_still_wins(monkeypatch):
    """A meter that says the budget is gone still outranks the prose."""

    class _Quota:
        exhausted = True

    monkeypatch.setattr(
        provider_order,
        "load_provider_quota_map",
        lambda *a, **k: {PROVIDER: _Quota()},
    )

    exc = ApiError(NEUTRAL_TEXT, status=None)

    assert _classify_provider_failure(exc, PROVIDER) == "exhausted"


def test_quota_map_failure_leaves_exhausted_false(monkeypatch):
    """A quota map that cannot be read is not a reading that says "gone"."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("CC Switch is not answering")

    monkeypatch.setattr(provider_order, "load_provider_quota_map", _boom)

    exc = ApiError(NEUTRAL_TEXT, status=None)

    assert _classify_provider_failure(exc, PROVIDER) == "rate_limited"


# ---------------------------------------------------------------------------
# 3. The statuses that are present keep their own bucket
# ---------------------------------------------------------------------------


def test_status_400_is_fatal():
    """A rejected request is still fatal."""
    exc = ApiError("bad request", status="400")

    assert _classify_provider_failure(exc, PROVIDER) == "fatal"


def test_status_429_is_rate_limited():
    """A rate limit is still a rate limit."""
    exc = ApiError("slow down", status="429")

    assert _classify_provider_failure(exc, PROVIDER) == "rate_limited"


def test_status_500_stays_other():
    """Only the *empty* status moves; a 500 keeps the old bucket."""
    exc = ApiError("upstream hiccup", status="500")

    assert _classify_provider_failure(exc, PROVIDER) == "other"


def test_status_unknown_stays_other():
    """The default status is a status, and it is not empty."""
    exc = ApiError(NEUTRAL_TEXT)

    assert exc.status == "unknown"
    assert _classify_provider_failure(exc, PROVIDER) == "other"
