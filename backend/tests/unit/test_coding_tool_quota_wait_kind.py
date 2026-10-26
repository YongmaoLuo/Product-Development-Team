"""The park length follows the *kind* of refusal, not one number for all.

One refusal used to buy one park length: half an hour, for everything
from "slow down, ask again in a minute" to "this balance is empty and
a person has to top it up". Both ends of that range are wrong, in
opposite directions — a rate limit parks a healthy provider for half an
hour, and a hard cap is parked for half an hour as if a timer could
ever fix it.

:func:`capacity_wait.resolve_quota_wait` already answers *which* kind
of wait a refusal is, from the same text and the same live reading the
dispatcher uses. This file pins what the park does with that answer:

  * a window that names its own reset time is waited out exactly,
    because the window is a fact and a constant is a guess;
  * a balance that a person has to top up is parked long, and says so
    in the log — a silent long park is indistinguishable from a hang;
  * a refusal the reading cannot corroborate keeps the half hour,
    because "we cannot tell" is not "somebody has to act";
  * a rate limit keeps its short park *even when the meter says the
    window is full*. The reading is not consulted on that path at all:
    a rate limit is a fact about the traffic, not about the budget,
    and letting the meter into that branch would park a provider for
    the length of a window it never spent.

The operator's number wins over every one of those, which is the point
of having one: an operator who knows better than the reading does not
have to wait for a restart to be obeyed.

No test here reaches the network: the readings are the shapes the
parser is documented against, and the reader that fetches them is
stubbed.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_order  # noqa: E402
from capacity_wait import topup_park_sec  # noqa: E402
from coding_tool import ApiError, _cooldown_seconds_for  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    DEFAULT_COOLDOWN_SEC,
    DEFAULT_QUOTA_COOLDOWN_SEC,
)

#: The vendor's spent-plan refusal — the same plan, a different failure.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)

#: The operator's number for a quota park.
ENV_QUOTA_COOLDOWN_SEC = "PDT_PROVIDER_QUOTA_COOLDOWN_SEC"

#: The operator's number for a rate-limit park.
ENV_COOLDOWN_SEC = "PDT_PROVIDER_COOLDOWN_SEC"


def _now():
    return datetime.now(timezone.utc)


def _exhaustion_error():
    """The refusal of a provider whose budget is gone."""
    return ApiError(EXHAUSTION_TEXT, status="429")


def _rolling(provider, used, *, resets_in_minutes=60, weekly_used=10.0,
             weekly_resets_in_days=6, base=None):
    """A provider metered on rolling percentage windows."""
    base = base or _now()
    return {
        "provider": provider,
        "app_type": "claude",
        "tiers": [
            {
                "name": "five_hour",
                "used_percent": used,
                "remaining": round(100.0 - used, 2),
                "total": 100.0,
                "unit": "%",
                "resets_at": (
                    None if resets_in_minutes is None
                    else (base + timedelta(minutes=resets_in_minutes)).isoformat()
                ),
            },
            {
                "name": "weekly_limit",
                "used_percent": weekly_used,
                "remaining": round(100.0 - weekly_used, 2),
                "total": 100.0,
                "unit": "%",
                "resets_at": (
                    None if weekly_resets_in_days is None
                    else (base + timedelta(days=weekly_resets_in_days)).isoformat()
                ),
            },
        ],
        "updated_at": base.isoformat(),
        "success": True,
    }


def _balance(provider, remaining):
    """A provider metered as a currency amount.

    ``used_percent`` is 0.0 whatever the balance is — the payload
    divides by a ``total`` of 0 — so the amount is the only thing
    worth reading, and an empty one is the hard cap.
    """
    return {
        "provider": provider,
        "app_type": "claude",
        "tiers": [
            {
                "name": "CNY",
                "used_percent": 0.0,
                "remaining": remaining,
                "total": 0.0,
                "unit": "CNY",
                "resets_at": None,
            }
        ],
        "success": True,
    }


def _install_quota(monkeypatch, rows):
    """Make the live reading return ``rows`` for this test."""
    quota_map = provider_order.parse_usage_payload({"providers": list(rows)})
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: quota_map
    )
    return quota_map


@pytest.fixture(autouse=True)
def _no_leftover_operator_numbers(monkeypatch):
    """No operator number leaks in from the environment."""
    for name in (ENV_QUOTA_COOLDOWN_SEC, ENV_COOLDOWN_SEC,
                 "PDT_QUOTA_TOPUP_PARK_SEC"):
        monkeypatch.delenv(name, raising=False)
    provider_order.cache_clear()
    yield
    provider_order.cache_clear()


# ---------------------------------------------------------------------------
# The four kinds of refusal
# ---------------------------------------------------------------------------


class TestParkLengthFollowsTheKind:
    def test_window_park_uses_resets_at(self, monkeypatch):
        """A window that says when it comes back is waited out exactly.

        A fixed park has to cover the *worst* window — a week — because
        it does not know which one it is looking at. A provider parked
        at the twelve-minute mark of a five-hour window is unavailable
        for the rest of an hour it could have been serving in.
        """
        _install_quota(
            monkeypatch,
            [_rolling("MiniMax", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        seconds = _cooldown_seconds_for(_exhaustion_error(), "MiniMax")
        assert seconds == pytest.approx(12 * 60, abs=2.0), seconds

    def test_topup_park_is_long_and_logged(self, monkeypatch, caplog):
        """An empty balance is not a timer: it needs a person.

        So it is parked long — and it says so. A silent long park is
        indistinguishable from a hang, and the whole point of parking a
        provider for an hour is that somebody goes and buys the
        credits; a log nobody reads does not buy anything.
        """
        _install_quota(monkeypatch, [_balance("MiniMax", 0.0)])
        with caplog.at_level(logging.WARNING, logger="coding_tool"):
            seconds = _cooldown_seconds_for(_exhaustion_error(), "MiniMax")
        assert seconds == topup_park_sec(), seconds
        assert "provider_quota_manual_topup_required" in caplog.text
        assert "MiniMax" in caplog.text
        assert str(int(topup_park_sec())) in caplog.text

    def test_unknown_keeps_half_hour(self, monkeypatch):
        """A refusal the meter does not corroborate waits the half hour.

        The exception says the provider is out of budget; the meter says
        nothing is. Believing the exception and inventing a window for
        it would park a provider for half an hour on the strength of a
        number this process cannot see — and claiming a person has to
        top it up would page somebody for a rate limit.
        """
        _install_quota(monkeypatch, [_rolling("MiniMax", 12.0)])
        assert _cooldown_seconds_for(_exhaustion_error(), "MiniMax") == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )

    def test_rate_limit_keeps_short_park_even_with_a_full_meter(
        self, monkeypatch,
    ):
        """The counterweight: the meter does not enter this branch.

        A 429 that does not read as "out of budget" is expected to clear
        in minutes, and nothing in the payload makes it a window. It
        stays a rate limit even when the meter says the window is
        full — a full window is a fact about the budget, and a rate
        limit is a fact about the traffic, and neither one makes the
        other true.
        """
        _install_quota(
            monkeypatch,
            [_rolling("MiniMax", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        seconds = _cooldown_seconds_for(
            ApiError("slow down", status="429"), "MiniMax"
        )
        assert seconds == DEFAULT_COOLDOWN_SEC


# ---------------------------------------------------------------------------
# The operator's number wins
# ---------------------------------------------------------------------------


class TestOperatorOverride:
    def test_env_override_beats_reset(self, monkeypatch):
        """The operator beats the reading, not the other way round.

        A reading is a fact about the provider; the operator's number is
        a decision about what this deployment does about it. The
        decision is the one that has to win, or the knob is decoration.
        """
        monkeypatch.setenv(ENV_QUOTA_COOLDOWN_SEC, "42")
        _install_quota(
            monkeypatch,
            [_rolling("MiniMax", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        assert _cooldown_seconds_for(_exhaustion_error(), "MiniMax") == 42.0

    def test_topup_env_beats_default(self, monkeypatch):
        """The top-up park is a knob too, and it beats the default."""
        monkeypatch.setenv("PDT_QUOTA_TOPUP_PARK_SEC", "99")
        _install_quota(monkeypatch, [_balance("MiniMax", 0.0)])
        assert _cooldown_seconds_for(_exhaustion_error(), "MiniMax") == 99.0

    def test_the_quota_override_still_applies_to_a_topup(self, monkeypatch):
        """The old knob keeps working for a hard cap.

        Before the kinds existed there was one quota knob and one quota
        park; an operator who had set it was parking their hard caps
        with it, and that has to keep working.
        """
        monkeypatch.setenv(ENV_QUOTA_COOLDOWN_SEC, "77")
        _install_quota(monkeypatch, [_balance("MiniMax", 0.0)])
        assert _cooldown_seconds_for(_exhaustion_error(), "MiniMax") == 77.0

    def test_the_topup_knob_wins_over_the_quota_knob(self, monkeypatch):
        """Two knobs for the same park: the specific one is the answer.

        ``PDT_QUOTA_TOPUP_PARK_SEC`` is about a top-up and
        ``PDT_PROVIDER_QUOTA_COOLDOWN_SEC`` is about a quota park in
        general. The specific one is set by the operator who knows it
        is a top-up, so it is the one that is obeyed.
        """
        monkeypatch.setenv(ENV_QUOTA_COOLDOWN_SEC, "77")
        monkeypatch.setenv("PDT_QUOTA_TOPUP_PARK_SEC", "99")
        _install_quota(monkeypatch, [_balance("MiniMax", 0.0)])
        assert _cooldown_seconds_for(_exhaustion_error(), "MiniMax") == 99.0

    def test_an_unreadable_number_falls_back_to_the_reading(self, monkeypatch):
        """A knob that cannot be read is not a knob."""
        monkeypatch.setenv(ENV_QUOTA_COOLDOWN_SEC, "not-a-number")
        _install_quota(
            monkeypatch,
            [_rolling("MiniMax", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        seconds = _cooldown_seconds_for(_exhaustion_error(), "MiniMax")
        assert seconds == pytest.approx(12 * 60, abs=2.0), seconds

    def test_the_rate_limit_knob_is_untouched(self, monkeypatch):
        """The rate-limit park has its own knob, and its own default."""
        monkeypatch.setenv(ENV_COOLDOWN_SEC, "45")
        _install_quota(
            monkeypatch,
            [_rolling("MiniMax", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        assert _cooldown_seconds_for(
            ApiError("slow down", status="429"), "MiniMax"
        ) == 45.0
