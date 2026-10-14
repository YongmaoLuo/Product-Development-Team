"""Dispatch must follow the live quota: stop when there is none, switch when there is.

What this file pins
-------------------
The eligibility decision reads CC Switch's own ``/api/usage`` payload
through three shapes, and each of them fails in a different way:

* **Rolling window** — a 5-hour and a weekly tier, both percentages. The
  5-hour window is the burst window and the weekly one is what is left
  of the account; a provider with either empty cannot be dispatched to.
  A tier whose ``resets_at`` has already passed is a *closed* window, and
  its percentage belongs to the window before it — reading it literally
  would keep a freshly-reset provider disqualified for as long as the
  stale number is trusted, which is forever, because a full window does
  not update until it rolls.

* **Balance** — a tier in a currency. Its ``used_percent`` is
  structurally ``0.0`` (the payload divides by a ``total`` of ``0``), so
  that field carries no information at all, and such a provider often
  ranks first. The only question to ask of this shape is whether a
  balance remains.

* **No reader** — CC Switch could not meter the provider and said so
  with ``success: false`` and no tiers. That is not a refusal, and it is
  not evidence of one: the provider stays selectable, and the cost is
  that asking about it is done rarely.

The countdown is the other half. A provider parked for a fixed half hour
is unavailable for a window that may be twelve minutes or a week, and
the reading carries the moment that window returns.

No test here reaches the network: the payloads are the shapes the reader
is documented against, and the one group that exercises the HTTP leg
stubs ``urllib`` and counts the calls.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_order  # noqa: E402
from coding_tool import (  # noqa: E402
    ApiError,
    _cooldown_seconds_for,
    _park_provider_after_error,
)
from dynamic_provider_concurrency import (  # noqa: E402
    DEFAULT_COOLDOWN_SEC,
    DEFAULT_QUOTA_COOLDOWN_SEC,
    ActiveConcurrencyTracker,
    ProviderCooldown,
    acquire_provider_with_dynamic_capacity,
    get_shared_cooldown,
    provider_is_eligible,
    select_provider_with_dynamic_capacity,
    set_shared_cooldown,
)

# ---------------------------------------------------------------------------
# Payloads — the three shapes, spelled the way CC Switch spells them
# ---------------------------------------------------------------------------
#
# Every ``resets_at`` is built from the wall clock at the moment the
# payload is constructed rather than from a module-level constant. A
# park length is computed against the real clock, so a base pinned at
# import time is already stale by the time a long collection reaches
# these tests — which silently turns a twelve-minute countdown into a
# shorter one and, further down the line, would turn it into "already
# closed".


def _now():
    return datetime.now(timezone.utc)


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
                    else (
                        base + timedelta(days=weekly_resets_in_days)
                    ).isoformat()
                ),
            },
        ],
        "updated_at": base.isoformat(),
        "success": True,
    }


def _balance(provider, remaining):
    """A provider metered as a currency amount.

    ``used_percent`` is 0.0 whatever the balance is, and ``total`` is
    0.0 — the percentage is degenerate by construction, which is the
    whole point of this shape.
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


def _stamp(fmt):
    """A reset half an hour from now, in UTC."""
    return (_now() + timedelta(minutes=30)).strftime(fmt)


def _stamp_offset(fmt, hours):
    """The same instant expressed at another offset."""
    local = _now().astimezone(timezone(timedelta(hours=hours))) + timedelta(minutes=30)
    return local.strftime(fmt)


def _no_reader(provider, error):
    """A provider CC Switch could not meter at all."""
    return {"provider": provider, "success": False, "error": error}


def _quota(*rows, now=None):
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=now or _now()
    )


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _fresh_quota_cache():
    provider_order.cache_clear()
    yield
    provider_order.cache_clear()


@pytest.fixture(autouse=True)
def _capacity(capacity_config):
    capacity_config([("^Example", 10)], default=10)


# ---------------------------------------------------------------------------
# The rolling window
# ---------------------------------------------------------------------------


class TestRollingWindow:
    def test_a_full_five_hour_window_is_not_eligible(self):
        quota = _quota(_rolling("Example A", 100.0))
        assert quota["Example A"].exhausted is True
        assert quota["Example A"].exhausted_tier == "five_hour"

    def test_a_five_hour_window_with_room_is_eligible(self):
        quota = _quota(_rolling("Example A", 3.0))
        assert quota["Example A"].exhausted is False

    def test_the_weekly_window_gates_independently_of_the_burst_window(self):
        """5h at 3% and the week at 100%: the week still says no."""
        quota = _quota(_rolling("Example A", 3.0, weekly_used=100.0))
        assert quota["Example A"].exhausted is True
        assert quota["Example A"].exhausted_tier == "weekly_limit"

    def test_a_window_that_has_already_closed_reads_as_empty(self):
        """``resets_at`` in the past means the number is last window's.

        The provider carries a 100% reading and a reset time in the
        past, which together are contradictory: a window at 100% does
        not update until it rolls. Reading it literally would hold the
        provider out for as long as the stale number is trusted.
        """
        quota = _quota(
            _rolling("Example A", 100.0, resets_in_minutes=-5, weekly_used=0.0)
        )
        assert quota["Example A"].exhausted is False
        assert quota["Example A"].remaining_pct == 100.0

    def test_a_future_window_is_read_literally(self):
        """The counterweight to the rule above: only a *closed* window
        is discounted, never one that is merely due soon."""
        quota = _quota(_rolling("Example A", 100.0, resets_in_minutes=1))
        assert quota["Example A"].exhausted is True

    # The four values stay dynamic on purpose — the property under test is
    # "an instant in the *future*", which a literal would stop being the
    # moment it aged past — but the IDs are pinned. An unpinned parametrize
    # takes its ID from the value, so the wall-clock string above became the
    # case ID. Two xdist workers that import this module in different seconds
    # then collect different IDs, and xdist answers
    # "Different tests were collected between gw2 and gw14" and aborts the
    # whole run. The value is free to move; the name it is known by is not.
    @pytest.mark.parametrize(
        "stamp",
        [
            _stamp("%Y-%m-%dT%H:%M:%S+00:00"),
            _stamp("%Y-%m-%dT%H:%M:%SZ"),
            _stamp_offset("%Y-%m-%dT%H:%M:%S+08:00", hours=8),
            _stamp("%Y-%m-%dT%H:%M:%S.") + "105+00:00",
        ],
        ids=["utc-offset-colon", "zulu-suffix", "east-of-utc-offset", "millisecond-fraction"],
    )
    def test_every_timestamp_spelling_is_read(self, stamp):
        """The same instant, four spellings, all in the future."""
        row = _rolling("Example A", 100.0, resets_in_minutes=None)
        row["tiers"][0]["resets_at"] = stamp
        quota = _quota(row)
        assert quota["Example A"].exhausted is True
        assert quota["Example A"].exhausted_resets_at is not None

    def test_an_unparseable_timestamp_is_no_timestamp(self):
        row = _rolling("Example A", 100.0, resets_in_minutes=None)
        row["tiers"][0]["resets_at"] = "whenever"
        quota = _quota(row)
        assert quota["Example A"].exhausted_resets_at is None


# ---------------------------------------------------------------------------
# The balance — the field that must not be read
# ---------------------------------------------------------------------------


class TestBalanceShape:
    def test_a_funded_account_is_eligible_despite_a_zero_percentage(self):
        """``used_percent`` is 0.0 for this shape whatever the balance."""
        quota = _quota(_balance("Example A", 113.1))
        assert quota["Example A"].shape == provider_order.SHAPE_BALANCE
        assert quota["Example A"].exhausted is False
        assert quota["Example A"].remaining_pct == 100.0

    def test_an_empty_balance_is_not_eligible(self):
        quota = _quota(_balance("Example A", 0.0))
        assert quota["Example A"].exhausted is True
        assert quota["Example A"].remaining_pct == 0.0

    def test_a_negative_balance_is_not_eligible(self):
        quota = _quota(_balance("Example A", -3.0))
        assert quota["Example A"].exhausted is True

    def test_the_percentage_alone_would_not_separate_these_two(self):
        """The counterweight, stated as a difference.

        Two accounts, both reporting ``used_percent: 0.0``. Deciding on
        that field calls the funded one fully available and the empty
        one fully available too. Deciding on the balance separates them.
        """
        funded = _quota(_balance("Example A", 113.1))["Example A"]
        empty = _quota(_balance("Example B", 0.0))["Example B"]
        assert funded.exhausted is False
        assert empty.exhausted is True

    def _both(self, *, remaining, used):
        row = _balance("Example A", remaining)
        row["tiers"].append(
            {
                "name": "five_hour", "used_percent": used, "remaining": 100.0 - used,
                "total": 100.0, "unit": "%", "resets_at": None,
            }
        )
        return _quota(row)["Example A"]

    def test_a_row_carrying_both_reads_as_rolling(self):
        quota = self._both(remaining=50.0, used=4.0)
        assert quota.shape == provider_order.SHAPE_ROLLING
        assert quota.exhausted is False
        assert quota.remaining_pct == 96.0

    def test_an_empty_balance_disqualifies_a_row_that_also_has_a_window(self):
        """Two independent budgets, and either one empty is enough.

        Deciding this row by whichever tier is walked first would let an
        account with no money through on the strength of a window that
        has room in it.
        """
        assert self._both(remaining=0.0, used=4.0).exhausted is True

    def test_a_full_window_disqualifies_a_row_that_also_has_a_balance(self):
        assert self._both(remaining=50.0, used=100.0).exhausted is True

    def test_the_window_is_the_tier_named_when_both_are_empty(self):
        """A window has a moment attached; a balance does not.

        Naming the window is what lets the park last exactly as long as
        the window does instead of falling back to a guess.
        """
        quota = self._both(remaining=0.0, used=100.0)
        assert quota.exhausted_tier == "five_hour"


# ---------------------------------------------------------------------------
# The meter-less provider — fail open, ask slowly
# ---------------------------------------------------------------------------


class TestNoReader:
    def test_a_meter_less_provider_stays_selectable(self):
        quota = _quota(_no_reader("Example A", "Unknown balance provider"))
        assert quota["Example A"].exhausted is False
        assert quota["Example A"].unreadable is True

    def test_a_malformed_row_is_the_same_verdict_as_a_meter_less_one(self):
        """Both are "no reading", so both are treated identically.

        Otherwise a provider whose payload arrived malformed would be
        re-asked at the cheap cadence while a provider CC Switch
        explicitly could not meter would not — which is backwards.
        """
        quota = _quota({"provider": "Example A", "success": True, "tiers": []})
        assert quota["Example A"].unreadable is True
        assert quota["Example A"].exhausted is False

    def test_a_meter_less_provider_is_asked_at_most_half_hourly(self):
        quota = _quota(_no_reader("Example A", "Unknown balance provider"))
        assert provider_order.probe_floor_sec(quota["Example A"]) >= 1800.0

    def test_a_metered_provider_is_asked_at_the_cache_ttl(self):
        quota = _quota(_rolling("Example A", 10.0))
        floor = provider_order.probe_floor_sec(quota["Example A"])
        assert 0 < floor < 1800.0

    def test_the_half_hour_floor_is_the_existing_quota_cooldown(self):
        """One number, not two: a separate constant would drift."""
        quota = _quota(_no_reader("Example A", "Unknown balance provider"))
        assert provider_order.probe_floor_sec(quota["Example A"]) == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )


# ---------------------------------------------------------------------------
# The eligibility predicate, end to end
# ---------------------------------------------------------------------------


class TestEligibility:
    def _pick(self, rows=(), **kw):
        return select_provider_with_dynamic_capacity(
            ["Example A", "Example B"],
            kw.pop("tracker", ActiveConcurrencyTracker()),
            {},
            quota_map=_quota(*rows) if rows else {},
            **kw,
        )

    def test_an_exhausted_provider_is_skipped_for_the_next_one(self):
        assert self._pick(rows=(_rolling("Example A", 100.0),)) == "Example B"

    def test_a_funded_balance_ranked_first_is_chosen(self):
        assert self._pick(rows=(_balance("Example A", 113.1),)) == "Example A"

    def test_a_meter_less_provider_ranked_first_is_chosen(self):
        assert self._pick(
            rows=(_no_reader("Example A", "Unknown balance"),)
        ) == "Example A"

    def test_the_ranking_is_the_policy_and_is_not_re_scored(self):
        """A provider ranked second is used while the first is out.

        The first entry being full is a fact about the fleet, not an
        argument for preferring the next-best by some other measure.
        Nothing here re-orders; it only drops.
        """
        rows = (_rolling("Example A", 100.0), _rolling("Example B", 90.0))
        assert self._pick(rows=rows) == "Example B"

    def test_a_parked_provider_is_skipped(self):
        cooldown = ProviderCooldown()
        cooldown.park("Example A", seconds=600, reason="quota")
        assert self._pick(
            rows=(_rolling("Example A", 1.0),), cooldown=cooldown
        ) == "Example B"

    def test_parent_is_never_eligible(self):
        assert select_provider_with_dynamic_capacity(
            ["parent"], ActiveConcurrencyTracker(), {}, quota_map={},
        ) is None

    def test_the_verdict_governs_even_when_the_percentage_would_not(self):
        """The rule is the verdict, not the number that scales the cap.

        For every shape the reader knows today, an exhausted reading also
        reports zero headroom, so the cap check would disqualify the same
        providers on its own — and a test that only exercised those shapes
        would pass against an implementation that never read the verdict
        at all. This is the case that separates them: a verdict with head
        room left, which the cap check would happily grant a full pool to.
        """
        contradictory = provider_order.ProviderQuota(
            provider="Example A",
            shape=provider_order.SHAPE_ROLLING,
            remaining_pct=100.0,
            exhausted=True,
            exhausted_tier="five_hour",
        )
        assert provider_is_eligible(
            "Example A", ActiveConcurrencyTracker(), {}, {"Example A": contradictory}
        ) is False
        assert select_provider_with_dynamic_capacity(
            ["Example A"], ActiveConcurrencyTracker(), {}, quota_map={"Example A": contradictory},
        ) is None


# ---------------------------------------------------------------------------
# The countdown — a park lasts as long as the window it waits on
# ---------------------------------------------------------------------------


def _quota_error():
    return ApiError("已达到 Token Plan 用量上限", status="429")


def _install_quota(monkeypatch, rows):
    """Make the live reading return ``rows`` for this test."""
    quota_map = _quota(*rows)
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: quota_map
    )
    return quota_map


class TestCountdown:
    def test_a_window_twelve_minutes_out_parks_for_twelve_minutes(
        self, monkeypatch,
    ):
        """Not for the half hour a fixed number has to be.

        A fixed park has to cover the *worst* window — a week — because
        it does not know which one it is looking at. A provider parked at
        the twelve-minute mark of a five-hour window is unavailable for
        the rest of an hour it could have been serving in.
        """
        _install_quota(
            monkeypatch,
            [_rolling("Example A", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        seconds = _park_provider_after_error("Example A", _quota_error())
        assert 11 * 60 <= seconds <= 13 * 60, seconds
        assert seconds < DEFAULT_QUOTA_COOLDOWN_SEC / 2, seconds

    def test_the_park_deadline_is_the_window_not_the_now(self, monkeypatch):
        """``park()`` counts in seconds from a monotonic clock.

        Handing it the raw timestamp would park for roughly fifty
        thousand years — or, with a past timestamp, park for nothing at
        all, since ``park`` refuses a non-positive duration.
        """
        _install_quota(
            monkeypatch,
            [_rolling("Example A", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        _park_provider_after_error("Example A", _quota_error())
        remaining = get_shared_cooldown().remaining("Example A")
        assert 11 * 60 <= remaining <= 13 * 60, remaining

    def test_the_weekly_window_sets_the_length_when_it_is_the_full_one(
        self, monkeypatch,
    ):
        """The tier that convicted the provider decides how long.

        Reading the 5-hour tier here would park for three hours on a
        refusal the week caused — an hour of refusing to come back for a
        window that had not even closed yet.
        """
        _install_quota(
            monkeypatch,
            [_rolling("Example A", 3.0, resets_in_minutes=180,
                      weekly_used=100.0, weekly_resets_in_days=6)],
        )
        seconds = _park_provider_after_error("Example A", _quota_error())
        assert seconds > 6 * 24 * 3600 * 0.9, seconds

    def test_a_missing_reset_time_falls_back_to_the_half_hour(
        self, monkeypatch,
    ):
        _install_quota(
            monkeypatch,
            [_rolling("Example A", 100.0, resets_in_minutes=None,
                      weekly_used=0.0, weekly_resets_in_days=None)],
        )
        assert _park_provider_after_error("Example A", _quota_error()) == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )

    def test_an_unparseable_reset_time_falls_back_to_the_half_hour(
        self, monkeypatch,
    ):
        row = _rolling("Example A", 100.0, resets_in_minutes=None,
                       weekly_used=0.0, weekly_resets_in_days=None)
        row["tiers"][0]["resets_at"] = "soon"
        _install_quota(monkeypatch, [row])
        assert _park_provider_after_error("Example A", _quota_error()) == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )

    def test_a_refusal_the_meter_does_not_corroborate_uses_the_half_hour(
        self, monkeypatch,
    ):
        """A refusal with no exhausted reading behind it.

        The exception says the provider is out of budget; the meter says
        nothing is. Believing the exception and inventing a window for it
        would park a provider for half an hour on the strength of a
        number this process cannot see.
        """
        _install_quota(monkeypatch, [_rolling("Example A", 12.0)])
        assert _park_provider_after_error("Example A", _quota_error()) == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )

    def test_no_reading_at_all_uses_the_half_hour(self):
        """CC Switch unreachable: the same answer as an unusable stamp."""
        assert _park_provider_after_error("Example A", _quota_error()) == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )

    def test_a_rate_limit_still_parks_for_the_short_default(self, monkeypatch):
        """The counterweight: this change is about windows, not limits.

        A 429 that does not read as "out of budget" is expected to clear
        in minutes, and nothing in the payload would make it a window.
        Parking it for twelve minutes because a countdown exists would
        be a regression dressed as a feature.
        """
        _install_quota(
            monkeypatch,
            [_rolling("Example A", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        seconds = _park_provider_after_error(
            "Example A", ApiError("slow down", status="429")
        )
        assert seconds == DEFAULT_COOLDOWN_SEC

    def test_a_non_capacity_error_parks_nothing(self):
        assert _park_provider_after_error(
            "Example A", ApiError("bad key", status="401")
        ) == 0.0

    def test_an_operator_override_still_wins(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_QUOTA_COOLDOWN_SEC", "42")
        _install_quota(
            monkeypatch,
            [_rolling("Example A", 100.0, resets_in_minutes=12,
                      weekly_used=0.0)],
        )
        assert _park_provider_after_error("Example A", _quota_error()) == 42.0

    def test_the_cooldown_helper_is_callable_without_a_provider(self):
        """The one-argument form still answers the fixed question."""
        assert _cooldown_seconds_for(_quota_error()) == (
            DEFAULT_QUOTA_COOLDOWN_SEC
        )


# ---------------------------------------------------------------------------
# Stop dispatching — and not sleeping through a window
# ---------------------------------------------------------------------------


class TestStopDispatching:
    def test_nothing_is_selected_when_every_window_is_full(self):
        quota = _quota(
            _rolling("Example A", 100.0), _rolling("Example B", 100.0)
        )
        assert select_provider_with_dynamic_capacity(
            ["Example A", "Example B"], ActiveConcurrencyTracker(), {}, quota_map=quota,
        ) is None

    def test_an_exhausted_fleet_returns_without_waiting(self):
        """A window that does not open for hours cannot be polled for.

        The poller's unit is a fraction of a second and it belongs to
        slot contention. Entering it with nothing that can ever become
        available spends the caller's whole timeout to arrive at an
        answer the filter already produced.
        """
        quota = _quota(
            _rolling("Example A", 100.0), _rolling("Example B", 100.0)
        )
        started = time.monotonic()
        chosen = acquire_provider_with_dynamic_capacity(
            ["Example A", "Example B"], ActiveConcurrencyTracker(), {},
            timeout=30.0, poll_interval=5.0, quota_map=quota,
        )
        elapsed = time.monotonic() - started
        assert chosen is None
        assert elapsed < 1.0, f"waited {elapsed:.2f}s for an answer it had"

    def test_a_held_slot_is_still_waited_for(self):
        """The other half of the split, so the split is a real one.

        Both providers are within quota and serve fine; one is simply
        held. This is the case the poller exists for, and it resolves in
        seconds rather than never.
        """
        tracker = ActiveConcurrencyTracker()
        for _ in range(10):
            tracker.try_acquire("Example A", 10)
        quota = _quota(
            _rolling("Example A", 1.0), _rolling("Example B", 1.0)
        )
        threading.Thread(
            target=tracker.release, args=("Example A",), daemon=True
        ).start()
        chosen = acquire_provider_with_dynamic_capacity(
            ["Example A", "Example B"], tracker, {},
            timeout=5.0, poll_interval=0.02, quota_map=quota,
        )
        assert chosen is not None

    def test_a_meter_less_fleet_is_dispatched_to_rather_than_stopped(self):
        """Fail-open at the fleet level, not only per provider.

        Every provider being unmetered is indistinguishable, from this
        process, from having no providers at all. Stopping would let a
        single probe failure halt the fleet — the opposite of what the
        reading is for.
        """
        quota = _quota(
            _no_reader("Example A", "Unknown balance"),
            _no_reader("Example B", "Unknown balance"),
        )
        chosen = acquire_provider_with_dynamic_capacity(
            ["Example A", "Example B"], ActiveConcurrencyTracker(), {},
            timeout=1.0, poll_interval=0.01, quota_map=quota,
        )
        assert chosen in ("Example A", "Example B")

    def test_a_funded_balance_fleet_is_dispatched_to(self):
        quota = _quota(
            _balance("Example A", 113.1), _balance("Example B", 0.01)
        )
        chosen = acquire_provider_with_dynamic_capacity(
            ["Example A", "Example B"], ActiveConcurrencyTracker(), {},
            timeout=1.0, poll_interval=0.01, quota_map=quota,
        )
        assert chosen == "Example A"

    def test_the_walk_is_told_which_providers_the_window_closed(self):
        """The "stop" answer has to name a reason.

        "no free slot" and "every window is full" need different handling
        from the caller — one comes back in seconds, the other on a
        schedule — and a bare ``None`` cannot tell them apart.
        """
        quota = _quota(
            _rolling("Example A", 100.0), _rolling("Example B", 4.0)
        )
        assert select_provider_with_dynamic_capacity(
            ["Example A"], ActiveConcurrencyTracker(), {}, quota_map=quota,
        ) is None
        assert select_provider_with_dynamic_capacity(
            ["Example A", "Example B"], ActiveConcurrencyTracker(), {}, quota_map=quota,
        ) == "Example B"


# ---------------------------------------------------------------------------
# The TTL cache — one request per window, not one per dispatch
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def read(self):
        return self._body

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCache:
    def _stub(self, monkeypatch, payload, calls):
        import json

        body = json.dumps(payload).encode("utf-8")

        def _urlopen(request, timeout=None):
            calls.append(getattr(request, "full_url", str(request)))
            return _FakeResponse(body)

        monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)
        return body

    def test_two_dispatches_make_one_request(self, monkeypatch):
        """The dispatch path asks on every dispatch.

        Without this, every subagent launch is an outbound fan-out to
        CC Switch, which then asks every provider's own API. That is the
        difference between a cached reading and a request per dispatch,
        and it is not a cost this layer can absorb.
        """
        calls = []
        self._stub(
            monkeypatch, {"providers": [_rolling("Example A", 10.0)]}, calls
        )
        first = provider_order.load_provider_quota_map()
        second = provider_order.load_provider_quota_map()
        assert len(calls) == 1, calls
        assert first == second

    def test_the_url_is_the_usage_endpoint(self, monkeypatch):
        calls = []
        self._stub(
            monkeypatch, {"providers": [_rolling("Example A", 10.0)]}, calls
        )
        provider_order.load_provider_quota_map()
        assert calls[0].endswith("/api/usage")

    def test_the_env_override_redirects_the_endpoint(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", "example.invalid:9")
        calls = []
        self._stub(
            monkeypatch, {"providers": [_rolling("Example A", 10.0)]}, calls
        )
        provider_order.load_provider_quota_map()
        assert calls[0] == "http://example.invalid:9/api/usage"

    def test_an_expired_cache_re_reads(self, monkeypatch):
        calls = []
        self._stub(
            monkeypatch, {"providers": [_rolling("Example A", 10.0)]}, calls
        )
        provider_order.load_provider_quota_map()
        monkeypatch.setattr(provider_order, "_QUOTA_TTL_SEC", 0.0)
        provider_order.load_provider_quota_map()
        assert len(calls) == 2

    def test_a_cache_clear_re_reads(self, monkeypatch):
        calls = []
        self._stub(
            monkeypatch, {"providers": [_rolling("Example A", 10.0)]}, calls
        )
        provider_order.load_provider_quota_map()
        provider_order.cache_clear()
        provider_order.load_provider_quota_map()
        assert len(calls) == 2

    def test_a_meter_less_verdict_survives_a_refresh(self, monkeypatch):
        """A "no meter" answer is carried forward for its own floor.

        The floor is what stops a provider that cannot be metered from
        being re-examined at the cadence the metered ones use.
        """
        calls = []
        self._stub(
            monkeypatch,
            {"providers": [_no_reader("Example A", "Unknown balance")]},
            calls,
        )
        provider_order.load_provider_quota_map()
        monkeypatch.setattr(provider_order, "_QUOTA_TTL_SEC", 0.0)
        monkeypatch.setattr(
            provider_order, "probe_floor_sec",
            lambda q: DEFAULT_QUOTA_COOLDOWN_SEC,
        )
        again = provider_order.load_provider_quota_map()
        assert again["Example A"].unreadable is True

    def test_a_failed_read_admits_everyone(self, monkeypatch):
        """An outage must not become a halt.

        There is no second source to fall back on, so an unreachable
        CC Switch yields no readings at all — and no readings mean no
        disqualification. A provider that is genuinely uncallable is
        caught by its own refusal, which is an observation rather than
        an inference from a gap in a payload.
        """

        def _boom(request, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(provider_order.urllib.request, "urlopen", _boom)
        assert provider_order.load_provider_quota_map() == {}
        assert select_provider_with_dynamic_capacity(
            ["Example A"], ActiveConcurrencyTracker(), {}, quota_map={},
        ) == "Example A"

    def test_a_malformed_body_is_a_failed_read(self, monkeypatch):
        monkeypatch.setattr(
            provider_order.urllib.request,
            "urlopen",
            lambda request, timeout=None: _FakeResponse(b"<html>nope</html>"),
        )
        assert provider_order.load_provider_quota_map() == {}
