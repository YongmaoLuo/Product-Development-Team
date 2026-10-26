"""One 429, three different waits.

:class:`dynamic_provider_concurrency.classify_capacity_failure` already
answers *what kind* of refusal a provider just made: a spent plan
(``exhausted``), a transient rate limit (``rate_limited``), a bad
request (``fatal``) or anything else (``other``). What it does not
answer is *how long to wait*, and the old backend had one number for
every kind of refusal: park for thirty minutes and try again. That is
wrong in both directions at once. A transient rate limit clears in
minutes, so thirty minutes throws away a healthy provider's throughput
for no reason; a hard cap — a balance that a person has to top up —
never clears on a timer at all, so waiting for it is waiting for
somebody else to notice.

This module maps the classification bucket plus CC Switch's live quota
reading onto one wait answer, and pins the three cases that matter:

  * a transient rate limit — wait a few minutes and try again;
  * a 5h / weekly window that has a ``resets_at`` — wait exactly until
    the window comes back, because the window is a fact and a constant
    is a guess;
  * a hard cap that has to be topped up by a person — say so, and do
    not pretend a timer will fix it.

The reading is judged by *tier*, not by shape: the parser records
``exhausted_tier = window_exhausted_tier or balance_exhausted_tier``
(:mod:`provider_order`), so a provider whose 5h window still has room
but whose balance is empty reads as a rolling row while naming a
balance tier. The balance tier's name comes from the vendor's own
payload and is not necessarily called ``balance``, so the only safe
question is "is this tier one of the gated windows?".

Every test here is pure: no server, no provider, no network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from capacity_wait import (  # noqa: E402
    ENV_QUOTA_TOPUP_PARK_SEC,
    ENV_WAIT_MAX_SEC,
    ENV_WAIT_POLL_SEC,
    WAIT_MANUAL_TOPUP,
    WAIT_RATE_LIMITED,
    WAIT_UNKNOWN,
    WAIT_WINDOW,
    QuotaWait,
    QuotaWaitExhausted,
    capacity_wait_budget_sec,
    capacity_wait_poll_sec,
    resolve_quota_wait,
    topup_park_sec,
)
from dynamic_provider_concurrency import classify_capacity_failure  # noqa: E402
from provider_order import (  # noqa: E402
    GATED_TIERS,
    SHAPE_BALANCE,
    SHAPE_UNREADABLE,
    ProviderQuota,
)

#: The vendor's rate-limit refusal — same plan, a different failure.
RATE_LIMIT_TEXT = "已达到 Token Plan 速率限制：请稍后重试。(2062)"

#: The vendor's spent-plan refusal.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)


def _rolling_window(seconds_from_now: float) -> ProviderQuota:
    """A reading whose 5h window is spent and comes back later."""
    return ProviderQuota(
        provider="MiniMax",
        shape="rolling",
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="five_hour",
        exhausted_resets_at=datetime.now(timezone.utc)
        + timedelta(seconds=seconds_from_now),
    )


def _hard_cap() -> ProviderQuota:
    """A reading whose balance is empty — a person has to top it up.

    The tier name is the vendor's own, and deliberately not
    ``balance``: the parser takes it from the payload
    (``sorted(balance_tiers)[0]``), so a name check would be a
    coincidence rather than a rule.
    """
    return ProviderQuota(
        provider="MiniMax",
        shape=SHAPE_BALANCE,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="credits",
        exhausted_resets_at=None,
    )


# ---------------------------------------------------------------------------
# The three cases that matter
# ---------------------------------------------------------------------------


class TestClassifyCapacityFailure:
    def test_classify_transient_rate_limit(self):
        """A rate limit is a rate limit: short wait, no top-up."""
        assert classify_capacity_failure(RATE_LIMIT_TEXT, "429") == "rate_limited"

        wait = resolve_quota_wait("MiniMax", RATE_LIMIT_TEXT, "429", None)
        assert wait.kind == WAIT_RATE_LIMITED
        assert wait.seconds == 300.0
        assert wait.needs_manual_topup is False

    def test_classify_five_hour_window(self):
        """A 5h window that names its own reset time is waited out
        exactly — the window is a fact, a constant would be a guess."""
        assert (
            classify_capacity_failure(EXHAUSTION_TEXT, "429", live_exhausted=True)
            == "exhausted"
        )

        wait = resolve_quota_wait(
            "MiniMax", EXHAUSTION_TEXT, "429", _rolling_window(2 * 3600)
        )
        assert wait.kind == WAIT_WINDOW
        assert wait.seconds == pytest.approx(7200.0, abs=1.0)
        assert wait.needs_manual_topup is False

    def test_classify_hard_cap(self):
        """An empty balance is not a timer: it needs a person."""
        assert (
            classify_capacity_failure(EXHAUSTION_TEXT, "429", live_exhausted=True)
            == "exhausted"
        )

        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", _hard_cap())
        assert wait.kind == WAIT_MANUAL_TOPUP
        assert wait.needs_manual_topup is True
        assert wait.seconds == topup_park_sec()


class TestDocumentedOrderIsUntouched:
    """The classification order is a contract, and this module must not
    re-derive it. If someone changes the order to make the wait answer
    line up, the wait answer starts lying about the vendor's own text.
    """

    def test_documented_order_is_untouched(self):
        assert classify_capacity_failure(RATE_LIMIT_TEXT, "429") == "rate_limited"
        assert classify_capacity_failure(EXHAUSTION_TEXT, "429") == "exhausted"

    def test_live_reading_outranks_the_prose(self):
        """The meter is not a guess, so it outranks the vendor's text
        in either direction."""
        assert (
            classify_capacity_failure(RATE_LIMIT_TEXT, "429", live_exhausted=True)
            == "exhausted"
        )
        assert (
            classify_capacity_failure(EXHAUSTION_TEXT, "429", live_exhausted=True)
            == "exhausted"
        )


# ---------------------------------------------------------------------------
# What we cannot read, we do not invent
# ---------------------------------------------------------------------------


class TestUnknownDegradation:
    def test_no_reading_degrades_to_unknown(self):
        """No reading at all: wait the default budget, and do not guess
        that a person has to top anything up."""
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", None)
        assert wait.kind == WAIT_UNKNOWN
        assert wait.seconds == 1800.0
        assert wait.needs_manual_topup is False

    def test_funded_reading_does_not_invent_a_window(self):
        """A reading that still has budget does not corroborate a spent
        plan, so there is no window to wait for."""
        quota = ProviderQuota(
            provider="MiniMax",
            shape="rolling",
            remaining_pct=42.0,
            exhausted=False,
        )
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_UNKNOWN
        assert wait.seconds == 1800.0
        assert wait.needs_manual_topup is False

    def test_unreadable_reading_degrades_to_unknown(self):
        quota = ProviderQuota(provider="MiniMax", shape=SHAPE_UNREADABLE, unreadable=True)
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_UNKNOWN
        assert wait.seconds == 1800.0
        assert wait.needs_manual_topup is False

    def test_exhausted_without_a_tier_is_unknown(self):
        """No tier means the reading cannot say *what* ran out. That is
        not proof of a hard cap, so it is not called one."""
        quota = ProviderQuota(
            provider="MiniMax",
            shape="rolling",
            exhausted=True,
            exhausted_tier=None,
            exhausted_resets_at=None,
        )
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_UNKNOWN
        assert wait.seconds == 1800.0
        assert wait.needs_manual_topup is False

    def test_window_without_a_reset_time_is_unknown(self):
        """A window that names no moment cannot be waited out exactly."""
        quota = ProviderQuota(
            provider="MiniMax",
            shape="rolling",
            exhausted=True,
            exhausted_tier="five_hour",
            exhausted_resets_at=None,
        )
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_UNKNOWN
        assert wait.seconds == 1800.0

    def test_window_that_already_reset_is_unknown(self):
        """A stamp in the past describes a window that has rolled."""
        quota = ProviderQuota(
            provider="MiniMax",
            shape="rolling",
            exhausted=True,
            exhausted_tier="five_hour",
            exhausted_resets_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_UNKNOWN
        assert wait.seconds == 1800.0

    def test_hard_cap_outranks_a_reset_stamp(self):
        """A balance is topped up by a person, not by a timer: a reset
        stamp on the row does not turn a hard cap back into a window."""
        quota = ProviderQuota(
            provider="MiniMax",
            shape=SHAPE_BALANCE,
            exhausted=True,
            exhausted_tier="credits",
            exhausted_resets_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_MANUAL_TOPUP
        assert wait.needs_manual_topup is True


# ---------------------------------------------------------------------------
# The knobs
# ---------------------------------------------------------------------------


class TestEnvironment:
    def test_budget_zero_escalates_immediately(self, monkeypatch):
        monkeypatch.setenv(ENV_WAIT_MAX_SEC, "0")
        assert capacity_wait_budget_sec() == 0.0

    def test_env_defaults_and_bounds(self, monkeypatch):
        for name in (ENV_WAIT_MAX_SEC, ENV_WAIT_POLL_SEC, ENV_QUOTA_TOPUP_PARK_SEC):
            monkeypatch.delenv(name, raising=False)

        assert capacity_wait_budget_sec() == 1800.0
        assert capacity_wait_poll_sec() == 30.0
        assert topup_park_sec() == 3600.0

        # A value that is not a number falls back to the default.
        monkeypatch.setenv(ENV_WAIT_MAX_SEC, "not-a-number")
        monkeypatch.setenv(ENV_WAIT_POLL_SEC, "")
        monkeypatch.setenv(ENV_QUOTA_TOPUP_PARK_SEC, "abc")
        assert capacity_wait_budget_sec() == 1800.0
        assert capacity_wait_poll_sec() == 30.0
        assert topup_park_sec() == 3600.0

        # A negative wait is illegal, not "wait a very long time".
        monkeypatch.setenv(ENV_WAIT_MAX_SEC, "-1")
        monkeypatch.setenv(ENV_WAIT_POLL_SEC, "-5")
        monkeypatch.setenv(ENV_QUOTA_TOPUP_PARK_SEC, "-5")
        assert capacity_wait_budget_sec() == 1800.0
        assert capacity_wait_poll_sec() == 30.0
        assert topup_park_sec() == 3600.0

        # A legal but too-small value is raised to the minimum.
        monkeypatch.setenv(ENV_WAIT_POLL_SEC, "0")
        monkeypatch.setenv(ENV_QUOTA_TOPUP_PARK_SEC, "0")
        assert capacity_wait_poll_sec() == 1.0
        assert topup_park_sec() == 60.0

    def test_env_is_read_on_every_call(self, monkeypatch):
        """No module-level cache: an operator who lowers the budget
        mid-run must not have to restart the process to be obeyed."""
        monkeypatch.setenv(ENV_WAIT_MAX_SEC, "120")
        assert capacity_wait_budget_sec() == 120.0
        monkeypatch.setenv(ENV_WAIT_MAX_SEC, "900")
        assert capacity_wait_budget_sec() == 900.0


# ---------------------------------------------------------------------------
# The value objects
# ---------------------------------------------------------------------------


class TestQuotaWait:
    def test_seconds_are_never_negative(self):
        with pytest.raises(ValueError):
            QuotaWait(WAIT_WINDOW, -1.0, False)

    def test_exhausted_is_a_runtime_error_and_not_an_api_error(self):
        """The agent catches ``ApiError`` and rotates the provider for a
        capacity failure. An exhausted wait is the opposite of that: it
        is the answer, and it has to reach the caller."""
        from coding_tool import ApiError

        assert issubclass(QuotaWaitExhausted, RuntimeError)
        assert not issubclass(QuotaWaitExhausted, ApiError)

    def test_exhausted_carries_its_evidence(self):
        exc = QuotaWaitExhausted(
            ["MiniMax", "Anthropic"], needs_manual_topup=True, waited_sec=1800.0
        )
        assert exc.providers == ["MiniMax", "Anthropic"]
        assert exc.needs_manual_topup is True
        assert exc.waited_sec == 1800.0
        assert "MiniMax" in str(exc)


# ---------------------------------------------------------------------------
# The reading's own vocabulary
# ---------------------------------------------------------------------------


class TestTierVocabulary:
    def test_gated_tiers_are_the_windows(self):
        """The wait answer is built on the parser's own vocabulary: the
        two gated tiers are the ones that come back on a timer."""
        assert GATED_TIERS == ("five_hour", "weekly_limit")

    def test_a_weekly_window_is_a_window_too(self):
        quota = ProviderQuota(
            provider="MiniMax",
            shape="rolling",
            exhausted=True,
            exhausted_tier="weekly_limit",
            exhausted_resets_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        wait = resolve_quota_wait("MiniMax", EXHAUSTION_TEXT, "429", quota)
        assert wait.kind == WAIT_WINDOW
        assert wait.seconds == pytest.approx(3 * 24 * 3600, abs=1.0)
