"""Sleep until the window reopens; say which window binds, and which is missing.

What this file pins
-------------------
Three answers, one per defect:

* **R1 - the cooldown is sized on the window that actually binds.** A
  provider whose burst window *and* whose week are both empty is empty
  until the **later** of the two comes back. Sizing the cooldown on the
  burst window - the one that trips first - puts the provider back in
  front of a dispatcher in ten minutes, to be refused again, for three
  days. The single-window case is unchanged, and that is pinned too:
  the change only ever speaks when a second window is empty as well.

* **R2 - the wake sleeps until the named reopen, up to a cap.** The
  reading says when the window reopens; sleeping until then costs one
  wake instead of two probes. A window that names no moment still falls
  back to the fixed probe beat, because a plan upgrade comes back at a
  moment no ``resets_at`` was going to name - so the cap, not the
  countdown, is what keeps that case visible.

* **R3 - a missing window is recorded, not decided on.** A provider
  whose reading carries only the burst window has no week reading at
  all. That is a fact about the *reading*, not about the budget, so it
  goes into the log lines and changes no verdict.

* **The two levels, and the label that tells them apart.** A provider's
  own moment (when *it* can serve again) and the queue's moment (when
  the fleet should try again) are two questions with opposite answers:
  the provider's is the **latest** empty window, the queue's is the
  **earliest** blocked provider. Both are pinned here so that one
  cannot be read as the other, and the wait says which case it is in —
  ``weekly_only`` when no burst-window reopen can save the queue.

Every wait below is observed from the production dispatch, not from a
helper: the number a wake asks for and the reason it gives for it are
only worth anything if the dispatch that owns them uses them, and a
test that called the helper would not notice the call site being
unwired. The sleeps are recorded rather than served - the numbers on
this path are minutes long, and a test that served them would be
measuring the patience of the suite.

No test here reaches the network: the payload is parsed by the real
reader.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
import quota_watchdog  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    _quota_wake_delay,
    _quota_wake_probe_sec,
    _quota_wake_soonest_reset,
)
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"

#: The ceiling on a wake that sleeps on a named reopen - half an hour.
WAKE_CAP_SEC = 1800.0


# ---------------------------------------------------------------------------
# Readings - the shapes under test, built from the wall clock at call time
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _row(provider, *, five_hour_used=0.0, five_hour_resets_at=None,
         weekly_used=None, weekly_resets_at=None):
    """One ``/api/usage`` provider row, with the weekly tier optional.

    The weekly tier is optional because that is the case R3 is about: a
    provider whose reading carries the burst window alone, so nothing
    on this side can say whether its week is spent.
    """
    tiers = [{
        "name": "five_hour",
        "used_percent": five_hour_used,
        "remaining": round(100.0 - five_hour_used, 2),
        "total": 100.0,
        "unit": "%",
        "resets_at": (
            None if five_hour_resets_at is None
            else five_hour_resets_at.isoformat()
        ),
    }]
    if weekly_used is not None:
        tiers.append({
            "name": "weekly_limit",
            "used_percent": weekly_used,
            "remaining": round(100.0 - weekly_used, 2),
            "total": 100.0,
            "unit": "%",
            "resets_at": (
                None if weekly_resets_at is None
                else weekly_resets_at.isoformat()
            ),
        })
    return {
        "provider": provider,
        "app_type": "claude",
        "tiers": tiers,
        "success": True,
    }


def _payload(rows):
    """The reader's map for a set of rows - what the reader hands back."""
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=_now()
    )


def _quota(provider, **kw):
    """The reader's verdict for one row - the production entry point."""
    return _payload([_row(provider, **kw)])[provider]


# ---------------------------------------------------------------------------
# Harness - the real dispatch, a fake claude, a recorded wait
# ---------------------------------------------------------------------------


class _Log:
    """The event logger, kept for the assertions about *which* line ran."""

    def __init__(self):
        self.events: list = []

    def _record(self, name, message, data=None):
        self.events.append((name, message, data or {}))
        return self

    warning = info = error = debug = _record

    def names(self):
        return {name for name, _msg, _data in self.events}

    def of(self, name):
        return [data for event, _msg, data in self.events if event == name]


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _pools(capacity_config):
    capacity_config(_POOL_CAPS, default=10)


def _install_fake_claude(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )


@pytest.fixture
def scene(monkeypatch):
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m", "sonnet": "m",
                       "haiku": "m"},
        })),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage", lambda *a, **k: {}
    )


def _serve(monkeypatch, readings):
    """Answer the quota reader with a queue of readings, one per call."""
    queue = list(readings)
    state = {"n": 0}

    def _load(*_a, **kw):
        state["n"] += 1
        return queue[min(state["n"] - 1, len(queue) - 1)]

    monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)


def _tool(tracker, log):
    tool = ClaudeCodingTool(
        scene="verification", concurrency_tracker=tracker, logger=log,
    )
    tool.provider_slot_wait_sec = 0.2
    return tool


def _run(tool, **kw):
    kwargs = dict(idle_timeout=30, total_timeout=30)
    kwargs.update(kw)
    return tool._run_claude_interactive("hi", **kwargs)


def _open_fleet():
    """Every candidate with budget to spare -- in both windows.

    The week is *stated*, not omitted.  A reading that carries the burst
    window alone means "this provider's week went missing", and the
    dispatch gate treats a week that stopped being reported as an
    exhausted one (a provider that once reported a ``weekly_limit`` tier
    and no longer does is not a provider whose week reopened).  An open
    week is a week at 0% with a reset still ahead of it, so the fixture
    says exactly that rather than leaving the tier out and meaning the
    opposite of what it says.
    """
    stamp = _now() + timedelta(minutes=180)
    week = _now() + timedelta(days=7)
    return _payload([
        _row(
            name, five_hour_used=4.0, five_hour_resets_at=stamp,
            weekly_used=0.0, weekly_resets_at=week,
        )
        for name in CHAIN
    ])


def _shut_fleet(*, resets_at, weekly=None):
    """The whole tier out of burst window, on one shared schedule.

    ``weekly`` is ``None`` for the shape R3 is about - a reading that
    carries the burst window alone - and a ``(used, resets_at)`` pair
    for the shape R1 is about, where the week is empty as well.
    """
    rows = []
    for name in CHAIN:
        if weekly is None:
            rows.append(_row(
                name, five_hour_used=100.0, five_hour_resets_at=resets_at,
            ))
        else:
            rows.append(_row(
                name,
                five_hour_used=100.0, five_hour_resets_at=resets_at,
                weekly_used=weekly[0], weekly_resets_at=weekly[1],
            ))
    return _payload(rows)


def _dispatch(tmp_path, monkeypatch, first_reading):
    """One dispatch against a shut fleet; return ``(log, waits)``."""
    _install_fake_claude(tmp_path, monkeypatch)
    _serve(monkeypatch, [first_reading, _open_fleet()])
    waits: list = []
    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", waits.append)

    log = _Log()
    tool = _tool(ActiveConcurrencyTracker(), log)
    _run(tool)
    return log, waits


# ---------------------------------------------------------------------------
# R1 - which window the cooldown is sized on
# ---------------------------------------------------------------------------


class TestTheBindingWindow:
    """The moment a closed provider is really waiting on."""

    def test_two_empty_windows_bind_on_the_later_one(self):
        """Both windows empty: the provider is empty until the week.

        The burst window reopens in ten minutes and the week in three
        days. A cooldown sized on the burst window would put the
        provider back in front of a dispatcher in ten minutes, to be
        refused again - the same refusal, ten minutes apart, for three
        days.
        """
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)

        quota = _quota(
            "Vendor A",
            five_hour_used=100.0, five_hour_resets_at=burst,
            weekly_used=100.0, weekly_resets_at=week,
        )

        assert quota.exhausted is True
        assert quota.exhausted_resets_at == week, (
            "both windows are empty, so the provider is empty until the "
            "week comes back; the burst window reopening does not make "
            "it dispatchable"
        )
        assert quota.binding_tier == "weekly_limit"
        assert quota.exhausted_tier == "five_hour", (
            "the tier that tripped the gate is still the short window - "
            "only the moment the cooldown is sized on has moved"
        )

    def test_one_empty_window_binds_on_that_window(self):
        """The single-window case is what it was, to the microsecond.

        This is the regression guard for the whole change: with one
        window empty there is nothing to disagree with, so the moment
        the cooldown is sized on has to be the very same one as before.
        """
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)

        quota = _quota(
            "Vendor A",
            five_hour_used=100.0, five_hour_resets_at=burst,
            weekly_used=70.0, weekly_resets_at=week,
        )

        assert quota.exhausted is True
        assert quota.exhausted_tier == "five_hour"
        assert quota.exhausted_resets_at == burst, (
            "only the burst window is empty, so the cooldown is sized on "
            "the burst window - exactly as it was before the change"
        )
        assert quota.exhausted_resets_at != week
        assert quota.binding_tier == "five_hour"

    def test_an_empty_window_that_names_no_moment_binds_on_nothing(self):
        """No moment, no cooldown to size.

        The caller falls back to its own default rather than to a
        number this reading did not give.
        """
        quota = _quota(
            "Vendor A",
            five_hour_used=100.0, five_hour_resets_at=None,
            weekly_used=100.0, weekly_resets_at=None,
        )

        assert quota.exhausted is True
        assert quota.exhausted_resets_at is None
        assert quota.binding_tier is None

    def test_the_missing_week_is_recorded_and_decided_on_nothing(self):
        """A window that is not in the reading is a gap, not a verdict.

        The provider is out of burst window either way; the point is
        that nothing here claims to know whether its week is spent.
        """
        stamp = _now() + timedelta(minutes=10)
        quota = _quota(
            "Vendor A",
            five_hour_used=100.0, five_hour_resets_at=stamp,
        )

        assert quota.tiers_missing == ("weekly_limit",)
        assert quota.exhausted is True
        assert quota.exhausted_resets_at == stamp


# ---------------------------------------------------------------------------
# R2 - how long the next wake sleeps, and why
# ---------------------------------------------------------------------------


class TestTheWakeDelay:
    """The wait the dispatch asks for, and the reason it gives for it."""

    def test_a_window_ten_minutes_out_sleeps_until_it(
        self, tmp_path, monkeypatch, scene,
    ):
        """The reopen plus a margin: one wake instead of two probes."""
        log, waits = _dispatch(
            tmp_path, monkeypatch,
            _shut_fleet(resets_at=_now() + timedelta(minutes=10)),
        )

        assert len(waits) == 1, waits
        assert waits[0] == pytest.approx(602.0, abs=1.0), waits
        data = log.of("provider_quota_window_wait")[0]
        assert data["wait_reason"] == "soonest_reset", data

    def test_a_window_three_days_out_is_capped(
        self, tmp_path, monkeypatch, scene,
    ):
        """A window a week out is not slept through.

        The cap is what keeps the case a countdown cannot see - the
        plan upgrade - visible to a wake that is counting down.
        """
        log, waits = _dispatch(
            tmp_path, monkeypatch,
            _shut_fleet(resets_at=_now() + timedelta(days=3)),
        )

        assert len(waits) == 1, waits
        assert waits[0] == WAKE_CAP_SEC, waits
        data = log.of("provider_quota_window_wait")[0]
        assert data["wait_reason"] == "soonest_reset", data

    def test_the_soonest_of_several_closed_providers_is_the_one_waited_on(
        self, tmp_path, monkeypatch, scene,
    ):
        """The wait is about the first moment the fleet could serve again."""
        soon = _now() + timedelta(minutes=10)
        later = _now() + timedelta(days=3)
        rows = [
            _row("Vendor A Pro", five_hour_used=100.0,
                 five_hour_resets_at=soon),
            _row("Vendor A", five_hour_used=100.0,
                 five_hour_resets_at=later),
            _row("Vendor A API", five_hour_used=100.0,
                 five_hour_resets_at=later),
        ]

        _log, waits = _dispatch(tmp_path, monkeypatch, _payload(rows))

        assert len(waits) == 1, waits
        assert waits[0] == pytest.approx(602.0, abs=1.0), waits

    def test_a_window_that_names_no_moment_falls_back_to_the_probe(
        self, tmp_path, monkeypatch, scene,
    ):
        """The case the fixed beat exists for.

        Without a stamp there is nothing to count down to, and a plan
        upgrade brings the budget back at a moment no stamp was going
        to name.
        """
        log, waits = _dispatch(
            tmp_path, monkeypatch, _shut_fleet(resets_at=None),
        )

        assert len(waits) == 1, waits
        assert waits[0] == _quota_wake_probe_sec(), waits
        data = log.of("provider_quota_window_wait")[0]
        assert data["wait_reason"] == "probe", data
        assert data["soonest_reset_at"] is None, data

    def test_the_cap_is_configurable(self, tmp_path, monkeypatch, scene):
        monkeypatch.setenv("PDT_QUOTA_WAKE_MAX_SEC", "60")

        _log, waits = _dispatch(
            tmp_path, monkeypatch,
            _shut_fleet(resets_at=_now() + timedelta(days=3)),
        )

        assert len(waits) == 1, waits
        assert waits[0] == 60.0, waits

    def test_an_unreadable_cap_falls_back_to_half_an_hour(
        self, tmp_path, monkeypatch, scene,
    ):
        """A knob that cannot be read is the default, not a crash.

        The window is three days out, so an uncapped wait would be
        three days; the fallback cap is what it is measured against.
        """
        monkeypatch.setenv("PDT_QUOTA_WAKE_MAX_SEC", "not-a-number")

        _log, waits = _dispatch(
            tmp_path, monkeypatch,
            _shut_fleet(resets_at=_now() + timedelta(days=3)),
        )

        assert len(waits) == 1, waits
        assert waits[0] == WAKE_CAP_SEC, waits

    def test_the_helper_answers_with_the_reason_it_used(self):
        """The function's own contract, away from the dispatch.

        The dispatch above is the claim; this is the arithmetic behind
        it, stated once so a change to the helper cannot drift away
        from the answer the dispatch gives.
        """
        quota = _quota(
            "Vendor A",
            five_hour_used=100.0,
            five_hour_resets_at=_now() + timedelta(minutes=10),
        )

        delay, reason = _quota_wake_delay(["Vendor A"], {"Vendor A": quota})

        assert reason == "soonest_reset"
        assert delay == pytest.approx(602.0, abs=1.0), delay

        delay, reason = _quota_wake_delay(["Vendor A"], {})

        assert reason == "probe"
        assert delay == _quota_wake_probe_sec()


# ---------------------------------------------------------------------------
# R1 + R3 - what the two quota log lines now carry
# ---------------------------------------------------------------------------


class TestTheQuotaLogLines:
    """What the two quota lines carry, read off a real dispatch."""

    @staticmethod
    def _shut_fleet(burst, week):
        """The whole tier shut, with the week missing from two rows.

        ``CHAIN[0]`` carries both windows - the burst one reopening in
        ten minutes and the week in three days - so the moment that
        binds it is the week. The other two carry the burst window
        alone, which is the shape R3 is about.
        """
        return _payload([
            _row(
                CHAIN[0],
                five_hour_used=100.0, five_hour_resets_at=burst,
                weekly_used=100.0, weekly_resets_at=week,
            ),
            _row(CHAIN[1], five_hour_used=100.0,
                 five_hour_resets_at=week),
            _row(CHAIN[2], five_hour_used=100.0,
                 five_hour_resets_at=week),
        ])

    def _dispatch(self, tmp_path, monkeypatch):
        """One dispatch against a shut fleet, with the waits recorded."""
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)
        log, waits = _dispatch(
            tmp_path, monkeypatch, self._shut_fleet(burst, week),
        )
        return burst, week, log, waits

    def test_the_wait_line_carries_the_reopen_that_binds(
        self, tmp_path, monkeypatch, scene,
    ):
        """R1, as an operator sees it.

        The line names the moment the fleet was waiting on. It has to
        be the *week* - the window that actually binds - and not the
        burst window that trips first, or the wait would be a lie about
        the reading it was taken from.
        """
        burst, week, log, _waits = self._dispatch(tmp_path, monkeypatch)

        waits = log.of("provider_quota_window_wait")
        assert waits, sorted(log.names())
        data = waits[0]

        assert data["wait_reason"] == "soonest_reset", data
        assert data["soonest_reset_at"] == week.isoformat(), data
        assert data["soonest_reset_at"] != burst.isoformat(), (
            "the burst window is not the one that binds; naming it here "
            "would put the provider back in front of a dispatcher in ten "
            "minutes, to be refused again"
        )
        assert data["wait_sec"] == WAKE_CAP_SEC, data

    def test_the_quota_lines_carry_the_missing_windows(
        self, tmp_path, monkeypatch, scene,
    ):
        """R3: a window the reading did not carry is recorded.

        Two of the three rows carry the burst window alone, so nothing
        on this side can say whether their week is spent. Both quota
        lines have to say so, or "no budget" and "no reading" are the
        same silence.
        """
        _burst, _week, log, _waits = self._dispatch(tmp_path, monkeypatch)

        expected = {
            "Vendor A": ["weekly_limit"],
            "Vendor A API": ["weekly_limit"],
        }

        waits = log.of("provider_quota_window_wait")
        assert waits, sorted(log.names())
        assert waits[0]["tiers_missing"] == expected, waits[0]

        exhausted = log.of("provider_scene_all_pools_exhausted")
        assert exhausted, sorted(log.names())
        assert exhausted[0]["tiers_missing"] == expected, exhausted[0]


# ---------------------------------------------------------------------------
# The two levels - a provider's own moment, and the queue's
# ---------------------------------------------------------------------------


class TestTheTwoLevels:
    """Two questions, two levels, and they take opposite extrema.

    The first level is per provider: *when can this provider serve
    again?* The answer is the **latest** reopen among that provider's
    empty windows, because a provider whose burst window and whose week
    are both empty is empty until the later of the two comes back.

    The second level is per queue: *when should the whole queue try
    again?* The answer is the **earliest** reopen among the blocked
    providers, because the queue can serve again as soon as the first
    of them is back.

    The two take opposite extrema on purpose, and reading one as the
    other is the bug this class pins shut. Reading the provider-level
    answer as a queue-level one puts a provider back in front of a
    dispatcher ten minutes too early, to be refused again for three
    days; reading the queue-level answer as a provider-level one keeps
    a provider out of the chain long after it could have served.

    The wait's *reason* is observed from the production dispatch, so
    the label the operator reads is the label the dispatch actually
    emits — a helper-level assertion would not notice the call site
    being unwired.
    """

    @pytest.mark.parametrize(
        "five_hour_used, weekly_used, expected_binding, expected_in_sec",
        [
            pytest.param(
                100.0, 70.0, "five_hour", 600.0,
                id="burst-window-empty-the-week-still-has-room",
            ),
            pytest.param(
                100.0, 100.0, "weekly_limit", 3 * 24 * 3600.0,
                id="both-windows-empty",
            ),
            pytest.param(
                40.0, 100.0, "weekly_limit", 3 * 24 * 3600.0,
                id="the-week-empty-even-though-the-burst-window-has-room",
            ),
        ],
    )
    def test_a_provider_serves_again_when_its_own_latest_empty_window_returns(
        self, five_hour_used, weekly_used, expected_binding, expected_in_sec,
    ):
        """The provider's own moment: the **latest** empty window.

        The burst window reopens in ten minutes and the week in three
        days. Whichever of the two is empty decides when this provider
        can serve again — and when *both* are empty, the later one
        decides, because the burst window reopening does not make a
        week-empty provider dispatchable.
        """
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)

        quota = _quota(
            "Vendor A",
            five_hour_used=five_hour_used, five_hour_resets_at=burst,
            weekly_used=weekly_used, weekly_resets_at=week,
        )

        expected = burst if expected_binding == "five_hour" else week
        assert quota.exhausted_resets_at == expected, (
            "the provider's own moment is the latest reopen among the "
            "windows that are empty; the ten-minute burst window does "
            "not shorten it when the week is empty as well"
        )
        assert quota.binding_tier == expected_binding
        assert quota.exhausted is True, (
            "an empty window disqualifies the provider whatever the "
            "other window says - a burst window with room left in it "
            "does not make a week-empty provider dispatchable"
        )

        in_sec = (quota.exhausted_resets_at - _now()).total_seconds()
        assert in_sec == pytest.approx(expected_in_sec, abs=5.0), (
            f"expected this provider to be able to serve again in about "
            f"{expected_in_sec:.0f}s, got {in_sec:.0f}s"
        )

        if expected_binding == "weekly_limit":
            assert quota.exhausted_resets_at != burst, (
                "taking the earliest reopen here would put the provider "
                "back in front of a dispatcher in ten minutes, to be "
                "refused again for three days"
            )

            delay, reason = _quota_wake_delay(
                ["Vendor A"], {"Vendor A": quota},
            )
            assert reason == "weekly_only", (
                f"{reason} - the burst window reopening cannot save a "
                f"provider whose week is empty, so the wait says so "
                f"instead of looking like a ten-minute wait"
            )
            assert delay == WAKE_CAP_SEC, (
                f"{delay} - the delay itself is unchanged: the week is "
                f"three days out, so the cap still applies to it"
            )
        else:
            delay, reason = _quota_wake_delay(
                ["Vendor A"], {"Vendor A": quota},
            )
            assert reason == "soonest_reset", (
                f"{reason} - a provider bound on its burst window is "
                f"waiting for that window, and saying otherwise would "
                f"hide a ten-minute wait behind a three-day one"
            )
            assert delay == pytest.approx(602.0, abs=1.0), (
                f"{delay} - the burst window reopens in ten minutes, so "
                f"the wait is ten minutes and not the three days the week "
                f"would have asked for"
            )

    def test_the_queue_retries_on_the_earliest_of_its_blocked_providers(self):
        """The queue's moment: the **earliest** of the blocked providers.

        Both providers are out of their burst window, and both burst
        windows reopen in ten minutes — but one of them is out of its
        week as well, so that provider stays shut for three days and
        only the other one can put the queue back in play. The queue's
        moment is therefore ten minutes, and both places that answer
        this question name the same provider's moment.
        """
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)

        quota_map = {
            "Vendor A": _quota(
                "Vendor A",
                five_hour_used=100.0, five_hour_resets_at=burst,
                weekly_used=70.0, weekly_resets_at=week,
            ),
            "Vendor B": _quota(
                "Vendor B",
                five_hour_used=100.0, five_hour_resets_at=burst,
                weekly_used=100.0, weekly_resets_at=week,
            ),
        }
        blocked = ["Vendor A", "Vendor B"]

        assert quota_map["Vendor A"].exhausted_resets_at == burst
        assert quota_map["Vendor B"].exhausted_resets_at == week, (
            "each provider answers for itself: Vendor B is bound by its "
            "week, not by the burst window that reopens in ten minutes"
        )

        resets_at, seconds = _quota_wake_soonest_reset(blocked, quota_map)

        assert resets_at == burst, (
            "the queue can serve again as soon as the first blocked "
            "provider is back, so the wait is sized on the earliest - "
            "not on the latest, which would keep the whole queue asleep "
            "for three days after a provider could already serve"
        )
        assert seconds == pytest.approx(600.0, abs=5.0)

        assert quota_watchdog.earliest_reset(quota_map, blocked) == (
            burst.isoformat()
        ), (
            "the watchdog answers the same question as the dispatch and "
            "has to name the same moment"
        )

    def test_a_fleet_that_can_only_wait_for_the_week_says_so(
        self, tmp_path, monkeypatch, scene,
    ):
        """The wait says it is *not* waiting for the burst window.

        Every provider in the fleet still has room in its burst window
        — that window reopens in ten minutes and reopening it saves
        nothing, because the week is empty and the week is what binds
        them. The wait line says that in one word, because "waiting ten
        minutes" and "waiting three days" look the same to a reader who
        only has the seconds.
        """
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)
        rows = [
            _row(
                name, five_hour_used=40.0, five_hour_resets_at=burst,
                weekly_used=100.0, weekly_resets_at=week,
            )
            for name in CHAIN
        ]

        log, waits = _dispatch(tmp_path, monkeypatch, _payload(rows))

        assert len(waits) == 1, waits
        assert waits[0] == WAKE_CAP_SEC, (
            f"{waits} - the delay itself is unchanged: the week is three "
            f"days out, so the cap still applies to it"
        )
        data = log.of("provider_quota_window_wait")[0]
        assert data["wait_reason"] == "weekly_only", data

    def test_one_provider_a_burst_reopen_can_save_keeps_the_old_reason(
        self, tmp_path, monkeypatch, scene,
    ):
        """The label only appears when it is true of the whole fleet.

        One provider is out of its burst window alone, so its burst
        window reopening does put the queue back in play — the wait is a
        ten-minute wait, not a three-day one, and calling that
        "weekly only" would be a lie about it.
        """
        burst = _now() + timedelta(minutes=10)
        week = _now() + timedelta(days=3)
        rows = [
            _row(
                CHAIN[0], five_hour_used=100.0, five_hour_resets_at=burst,
                weekly_used=70.0, weekly_resets_at=week,
            ),
            _row(
                CHAIN[1], five_hour_used=100.0, five_hour_resets_at=burst,
                weekly_used=100.0, weekly_resets_at=week,
            ),
            _row(
                CHAIN[2], five_hour_used=100.0, five_hour_resets_at=burst,
                weekly_used=100.0, weekly_resets_at=week,
            ),
        ]

        log, waits = _dispatch(tmp_path, monkeypatch, _payload(rows))

        assert len(waits) == 1, waits
        assert waits[0] == pytest.approx(602.0, abs=1.0), waits
        data = log.of("provider_quota_window_wait")[0]
        assert data["wait_reason"] == "soonest_reset", data
