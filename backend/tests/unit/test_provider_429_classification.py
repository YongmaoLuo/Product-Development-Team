"""One 429, four different failures.

A provider that answers ``429`` is not always saying the same thing,
and the old backend had a single question where four answers were
needed: "is this out of budget?" — with ``token plan`` among the
markers, the vendor's rate-limit refusal (its text carries the code
``2062`` and the phrase for a rate limit) matched the same phrase the
vendor uses for a spent plan (code ``2056``). A rate-limited provider
was therefore parked for the quota window instead of the short rate
limit, so a transient refusal removed a healthy provider from the pool
for half an hour.

The classification is now one function with one fixed order, so the
decision cannot be re-derived differently at each call site:

  1. a live usage reading that says the budget is gone — the meter
     beats the prose;
  2. text that says the budget is gone (code ``2056``, the phrase for
     a usage cap) — exhausted;
  3. text that says "slow down" (code ``2062``, the phrase for a rate
     limit) — rate limited;
  4. a capacity status (``429`` / ``402`` / ``503`` / ``529``) with no
     text to read — rate limited;
  5. a request-shaped status (``400`` / ``405`` / ``406`` / ``413`` /
     ``414`` / ``415`` / ``422`` / ``501``) — fatal, because the request
     itself is wrong and no other provider will fix it;
  6. anything else — other, which keeps the old rotate-to-the-next
     provider behaviour. Authentication (``401`` / ``403``),
     not-found (``404``) and conflict (``409``) stay in this bucket on
     purpose: a bad credential is worth one more provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dynamic_provider_concurrency import (  # noqa: E402
    CAPACITY_ERROR_STATUSES,
    EXHAUSTION_ERROR_MARKERS,
    FATAL_ERROR_STATUSES,
    QUOTA_ERROR_MARKERS,
    RATE_LIMIT_ERROR_MARKERS,
    classify_capacity_failure,
    looks_like_quota_exhaustion,
)

#: The vendor's rate-limit refusal — same plan, a different failure.
RATE_LIMIT_TEXT = "已达到 Token Plan 速率限制：请稍后重试。(2062)"

#: The vendor's spent-plan refusal.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)


# ---------------------------------------------------------------------------
# The four-way classification
# ---------------------------------------------------------------------------


class TestClassifyCapacityFailure:
    def test_2062_is_rate_limited(self):
        assert classify_capacity_failure(RATE_LIMIT_TEXT, "429") == "rate_limited"

    def test_2056_is_exhausted(self):
        assert classify_capacity_failure(EXHAUSTION_TEXT, "429") == "exhausted"

    def test_live_reading_beats_2062_text(self):
        """The meter says the budget is gone; the prose says "slow
        down". The meter is the one that is not a guess."""
        assert (
            classify_capacity_failure(RATE_LIMIT_TEXT, "429", live_exhausted=True)
            == "exhausted"
        )

    def test_capacity_status_without_markers(self):
        assert classify_capacity_failure("slow down", "429") == "rate_limited"

    def test_request_error_is_fatal(self):
        assert classify_capacity_failure("bad request", "400") == "fatal"

    def test_auth_status_stays_other(self):
        assert classify_capacity_failure("bad key", "401") == "other"

    def test_unknown_status_is_other(self):
        assert classify_capacity_failure("something odd", "unknown") == "other"

    def test_empty_text_with_429_is_rate_limited(self):
        assert classify_capacity_failure("", "429") == "rate_limited"

    def test_empty_text_and_empty_status_is_other(self):
        assert classify_capacity_failure("", "") == "other"

    def test_missing_status_defaults_to_other(self):
        assert classify_capacity_failure("something odd") == "other"


# ---------------------------------------------------------------------------
# The status sets
# ---------------------------------------------------------------------------


class TestStatusSets:
    def test_capacity_statuses_are_the_rate_limit_bucket(self):
        assert CAPACITY_ERROR_STATUSES == frozenset({"429", "402", "503", "529"})

    def test_fatal_statuses_are_the_request_bucket(self):
        assert FATAL_ERROR_STATUSES == frozenset(
            {"400", "405", "406", "413", "414", "415", "422", "501"}
        )

    def test_auth_not_found_and_conflict_are_not_fatal(self):
        """A wrong credential is worth one more provider, not a dead
        task — these must keep rotating."""
        for status in ("401", "403", "404", "409"):
            assert status not in FATAL_ERROR_STATUSES, status
            assert classify_capacity_failure("nope", status) == "other", status

    def test_every_capacity_status_is_rate_limited(self):
        for status in CAPACITY_ERROR_STATUSES:
            assert classify_capacity_failure("", status) == "rate_limited", status

    def test_every_fatal_status_is_fatal(self):
        for status in FATAL_ERROR_STATUSES:
            assert classify_capacity_failure("", status) == "fatal", status


# ---------------------------------------------------------------------------
# The markers
# ---------------------------------------------------------------------------


class TestExhaustionMarkers:
    def test_token_plan_alone_is_not_exhaustion(self):
        """``token plan`` appears in the vendor's rate-limit refusal as
        well as in its spent-plan refusal, so it cannot separate them."""
        assert looks_like_quota_exhaustion(RATE_LIMIT_TEXT) is False

    def test_the_plan_cap_still_reads_as_exhaustion(self):
        assert looks_like_quota_exhaustion(EXHAUSTION_TEXT) is True

    def test_token_plan_is_not_an_exhaustion_marker(self):
        assert not any("token plan" in marker for marker in EXHAUSTION_ERROR_MARKERS)

    def test_english_exhaustion_markers_are_kept(self):
        for marker in (
            "quota",
            "insufficient",
            "balance",
            "billing",
            "payment",
            "credit",
            "plan limit",
        ):
            assert marker in EXHAUSTION_ERROR_MARKERS, marker

    def test_rate_limit_markers_cover_the_2062_wording(self):
        assert any("2062" in marker for marker in RATE_LIMIT_ERROR_MARKERS)
        assert any("速率限制" in marker for marker in RATE_LIMIT_ERROR_MARKERS)

    def test_the_rate_limit_markers_are_not_exhaustion_markers(self):
        """A marker that sits in both sets would make the order of the
        two text checks the only thing separating the two answers."""
        assert not set(RATE_LIMIT_ERROR_MARKERS) & set(EXHAUSTION_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# The compatibility alias and the module surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_quota_error_markers_aliases_the_exhaustion_set(self):
        assert QUOTA_ERROR_MARKERS == EXHAUSTION_ERROR_MARKERS

    def test_the_new_names_are_exported(self):
        import dynamic_provider_concurrency as mod

        for name in (
            "CAPACITY_ERROR_STATUSES",
            "EXHAUSTION_ERROR_MARKERS",
            "FATAL_ERROR_STATUSES",
            "QUOTA_ERROR_MARKERS",
            "RATE_LIMIT_ERROR_MARKERS",
            "classify_capacity_failure",
            "looks_like_quota_exhaustion",
        ):
            assert name in mod.__all__, name
