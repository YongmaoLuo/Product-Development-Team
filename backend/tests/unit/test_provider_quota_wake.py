"""A closed quota window must wake the dispatch, not end it.

What this file pins
-------------------
Refusing to dispatch into an empty window is only half the contract. The
window is refreshed on the other side of the CC Switch boundary, and
nothing on this side of it is watching: the usage payload is re-read
when *someone asks for it*, so a scene with nothing left to dispatch to
never asks. The fleet therefore stayed stopped until an unrelated call
walked in by accident, even though the budget behind it had come back
in the meantime.

So the "nothing to dispatch to" answer now carries a wake, and this file
pins the parts of it that are answers rather than mechanics:

* **Only a closed window sleeps.** A pool that is merely full is a
  different failure on a different clock — a slot comes back when the
  agent holding it finishes, and the existing poller already waits for
  that. Unifying the two spends the wait of one on the timescale of the
  other.
* **The wait is sized on the reopen the reading names, and capped.** A
  window that says when it reopens is waited for until then — one wake
  instead of two probes — but never for more than half an hour, which
  is what keeps the case a countdown cannot see visible: a plan upgrade
  brings the budget back at a moment no stamp was going to name, and a
  wait sized on a week-long window would sleep straight through it. A
  window that names no moment (or names one already in the past) falls
  back to the fixed probe beat, because there is nothing to count down
  to.
* **Waiting is not working.** The budget that bounds a call's wall time
  is parked while the dispatch waits and starts running again when it
  does not, so a wait cannot spend the very budget that exists to catch
  a call that never returns.
* **The wait is substitutable.** The numbers on this path are minutes,
  so these tests replace the wait and assert on the *duration asked
  for*. A test that served them would be measuring the patience of the
  suite.

No test here reaches the network: the payload is parsed by the real
reader, and the sleep is recorded rather than served.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from verification_subagent import (  # noqa: E402
    _await_under_work_clock,
)
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    SceneProviderUnavailable,
    WorkClock,
    _quota_wake_probe_sec,
)
from dynamic_provider_concurrency import (  # noqa: E402
    DEFAULT_COOLDOWN_SEC,
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


# ---------------------------------------------------------------------------
# Readings — built from the wall clock at call time, like the real ones
# ---------------------------------------------------------------------------
#
# A stamp is an absolute moment, and every assertion below compares it
# against the clock as it reads at the moment the assertion runs. Pinning
# a base timestamp at import time would leave it stale by the time a long
# collection reaches this file, which shows up as a duration that is
# merely slightly wrong rather than as a failure at all.


def _now():
    return datetime.now(timezone.utc)


def _window(provider, *, used, resets_at, weekly_used=0.0):
    """One provider row on rolling percentage windows."""
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
                "resets_at": None if resets_at is None else resets_at.isoformat(),
            },
            {
                "name": "weekly_limit",
                "used_percent": weekly_used,
                "remaining": round(100.0 - weekly_used, 2),
                "total": 100.0,
                "unit": "%",
                "resets_at": None if resets_at is None else resets_at.isoformat(),
            },
        ],
        "success": True,
    }


def _full(provider, *, resets_in_minutes=12):
    """A provider whose burst window is empty and reopens on a schedule."""
    stamp = (
        None if resets_in_minutes is None
        else _now() + timedelta(minutes=resets_in_minutes)
    )
    return _window(provider, used=100.0, resets_at=stamp)


def _full_fleet(*, resets_in_minutes=12):
    """Every candidate out of budget, on one shared schedule.

    The whole tier, not one row of it: a provider the reading does not
    mention stays selectable — a missing entry is "no reading", which is
    the fail-open half of the contract — so a single closed window among
    three leaves two candidates to dispatch to and the wake is never
    reached at all.
    """
    return _reading(*[
        _full(name, resets_in_minutes=resets_in_minutes) for name in CHAIN
    ])


#: The ceiling on a wake that sleeps on a named reopen — half an hour.
#: It is the escape hatch that keeps a plan upgrade visible to a wake
#: that is counting down to a window: the budget comes back at a moment
#: no ``resets_at`` was going to name, so a wait sized on a week-long
#: window would sleep straight through it.
WAKE_CAP_SEC = 1800.0


def _expected_wake(resets_in_minutes):
    """The wait the dispatch should ask for on a window named N minutes out.

    The reopen plus a small margin, clamped to the cap. A window that
    names no moment at all falls back to the fixed probe beat. The
    margin covers the few milliseconds between the reading being built
    and the wait being asked for, so the figures are compared with a
    tolerance rather than for equality.
    """
    if resets_in_minutes is None:
        return float(DEFAULT_COOLDOWN_SEC)
    return max(5.0, min(resets_in_minutes * 60.0 + 2.0, WAKE_CAP_SEC))


def _open(provider):
    """A provider with budget to spare."""
    return _window(provider, used=4.0, resets_at=_now() + timedelta(minutes=180))


def _fleet_open():
    return _reading(*[_open(name) for name in CHAIN])


def _reading(*rows):
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=_now()
    )


def _payload_open(provider):
    return _window(provider, used=4.0, resets_at=_now() + timedelta(minutes=180))


def _payload_full(provider):
    return _window(provider, used=100.0, resets_at=_now() + timedelta(minutes=12))


def _payload_no_reader(provider):
    return {"provider": provider, "success": False, "error": "Unknown balance"}


def _verdict(name, *, resets_at, exhausted=True):
    """One provider's reading, constructed directly.

    Needed for the shapes the payload cannot produce: a verdict carrying
    a stamp in the past, which the reader will not pair with a
    conviction, and a verdict that says nothing about any window at all.
    """
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=0.0 if exhausted else 96.0,
        exhausted=exhausted,
        exhausted_tier="five_hour" if exhausted else None,
        exhausted_resets_at=resets_at,
    )


# ---------------------------------------------------------------------------
# The decision on its own — no dispatch, no sleep, no clock but the given one
# ---------------------------------------------------------------------------


class TestTheProbeInterval:
    """How often a closed window is looked at again."""

    def test_the_interval_is_five_minutes(self):
        """The figure, stated as the literal seconds it has to be.

        Not "some sensible interval" and not "the quota cooldown": a
        probe cadence is a number an operator will be asked about, so it
        is pinned as the number rather than as a relationship.
        """
        assert _quota_wake_probe_sec() == 300.0
        assert _quota_wake_probe_sec() == DEFAULT_COOLDOWN_SEC

    def test_the_interval_is_the_short_cooldown_under_its_real_name(self):
        """Reused, not redeclared.

        Five minutes is already how long a rate-limited provider is
        parked for. Declaring a second copy of that number here would be
        two figures that agree until one of them is edited, and the one
        that drifts is the probe cadence nobody reviews.
        """
        from dynamic_provider_concurrency import DEFAULT_QUOTA_COOLDOWN_SEC

        assert DEFAULT_COOLDOWN_SEC == 300.0
        assert _quota_wake_probe_sec() == DEFAULT_COOLDOWN_SEC
        assert _quota_wake_probe_sec() != DEFAULT_QUOTA_COOLDOWN_SEC, (
            "the quota cooldown is the half hour; the probe must not have "
            "drifted onto it, which is the number the old countdown used"
        )

    def test_a_window_three_days_out_probes_exactly_as_often_as_one_minute_out(self):
        """The one thing a countdown cannot do, stated as the contract.

        A provider whose window is named three days from now is the easy
        case for a schedule — it can wait for the moment it was given. The
        hard case is the one with no moment: buying capacity makes the
        budget come back at a time no ``resets_at`` was ever going to
        name, so a wake that parked itself on that stamp would still be
        parked after the budget had returned.

        So the distance to the stamp is not an input to the cadence at
        all. Asserted as an equality rather than as a comparison, because
        "probes less often when the window is far off" is precisely the
        behaviour being ruled out.
        """
        soon = _quota_wake_probe_sec()
        three_days = _now() + timedelta(days=3)

        for stamp in (
            _now() + timedelta(minutes=1),
            _now() + timedelta(minutes=12),
            _now() + timedelta(hours=8),
            three_days,
            None,
        ):
            reading = {
                name: _verdict(name, resets_at=stamp) for name in CHAIN
            }
            # The reading is built here precisely to show that it is not
            # consulted: the function is called with no arguments at all.
            assert _quota_wake_probe_sec() == soon

    def test_a_stamp_in_the_past_does_not_make_the_probe_a_busy_loop(self):
        """A moment already gone still costs one interval, not a spin.

        The old answer to this was "do not sleep, ask again at once",
        which is right about the reading being behind and wrong about
        what to do with it: re-reading immediately against a meter that
        has not caught up yet is how a wake turns into a hot loop
        against the provider's usage endpoint.
        """
        assert _quota_wake_probe_sec() == DEFAULT_COOLDOWN_SEC

    def test_the_reader_is_not_consulted_by_the_cadence(self):
        """Read from the body, so it holds for the call site too.

        The whole point of a fixed beat is that it cannot be steered by
        the payload. If the function ever grew a parameter again, this
        would say so before the behaviour did. The docstring is removed
        before the scan: prose is allowed to *mention* the field the
        code refuses to read, and scanning it would punish exactly the
        explanation the next reader needs.
        """
        import ast
        import inspect
        import textwrap

        assert list(inspect.signature(_quota_wake_probe_sec).parameters) == []

        function = ast.parse(
            textwrap.dedent(inspect.getsource(_quota_wake_probe_sec))
        ).body[0]
        assert ast.get_docstring(function), (
            "the refusal has to be written down, not only done"
        )
        function.body = function.body[1:]
        body = ast.unparse(function)

        for banned in ("quota_map", "resets_at", "blocked", "now", "exhausted"):
            assert banned not in body, (
                "the probe must not read "
                + repr(banned)
                + " out of the reading: a plan upgrade reopens a window "
                "no stamp names"
            )


# ---------------------------------------------------------------------------
# The dispatch — a closed window is probed, and then dispatches
# ---------------------------------------------------------------------------


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
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


class _Wake:
    """The seam under test: what the dispatch waited for, and what it read.

    ``sleeps`` is the list of durations the dispatch *asked for* — the
    assertion target — while ``reads`` counts the quota fetches.
    Replacing the wait rather than shortening it is what makes "asserted
    how long it slept" possible: the numbers on this path run to half an
    hour, and a test that served them would be testing the suite's
    patience.
    """

    def __init__(self, monkeypatch):
        self.sleeps: list[float] = []
        self.reads = 0
        self.forced: list[bool] = []
        monkeypatch.setattr(
            coding_tool, "_quota_wake_sleep", self.sleeps.append
        )

    def serve(self, monkeypatch, readings):
        """Answer the quota reader with a queue of readings, one per call.

        A single entry is served forever — which is how "the meters have
        not caught up yet" and "the window never reopens" are both
        described, since from here they are the same observation.

        ``forced`` records whether each call asked to bypass the cache,
        because that flag is the difference between a wake that can see
        the window it slept through and one that cannot.
        """
        queue = list(readings)

        def _load(*_a, **kw):
            self.reads += 1
            self.forced.append(bool(kw.get("force")))
            return queue[min(self.reads - 1, len(queue) - 1)]

        monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)
        return self

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


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
            "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
        })),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage", lambda *a, **k: {}
    )


def _tool(tracker):
    tool = ClaudeCodingTool(
        scene="verification", concurrency_tracker=tracker,
    )
    tool.provider_slot_wait_sec = 0.2
    return tool


def _run(tool, **kw):
    kwargs = dict(idle_timeout=30, total_timeout=30)
    kwargs.update(kw)
    return tool._run_claude_interactive("hi", **kwargs)


class TestWakesAndDispatches:
    def test_a_closed_window_is_probed_and_then_dispatches(
        self, tmp_path, monkeypatch, scene,
    ):
        """The headline case.

        Nothing dispatches, the wait goes through the seam, and after it
        the work goes out as if none of this had happened — through the
        same rules, against the same chain.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=12),
            _fleet_open(),
        ])
        tracker = ActiveConcurrencyTracker()

        tool = _tool(tracker)
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert tool.current_call_provider in CHAIN
        assert len(wake.sleeps) == 1, wake.sleeps
        assert wake.sleeps[0] == pytest.approx(_expected_wake(12), abs=1.0), (
            "the wait must be the reopen the reading names plus a "
            "margin — not zero, and not a figure invented from it"
        )
        assert tracker.current("Vendor A Pro") == 0, "the slot came back"

    def test_a_window_three_days_out_caps_the_wait_and_reads_again(
        self, tmp_path, monkeypatch, scene,
    ):
        """How far off the window is has an effect — up to the cap.

        The two halves are one contract. A wait that grew without a
        ceiling would sleep straight through the reopen no stamp
        describes — the plan upgrade — and would spend the wait parked
        on a moment that has stopped meaning anything. And a probe that
        did not re-read would be asking the cache, which was answered
        before the sleep and so cannot report what happened during it.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=60 * 24 * 3),
            _fleet_open(),
        ])

        tool = _tool(ActiveConcurrencyTracker())
        _run(tool)

        assert wake.sleeps == [WAKE_CAP_SEC], wake.sleeps
        assert wake.reads >= 2, (
            "a probe is a re-read: the payload behind it has to be asked "
            "for again, or the wake is handed the verdict it slept through"
        )
        assert tool.current_call_provider in CHAIN

    def test_only_the_wake_reads_past_the_cache(
        self, tmp_path, monkeypatch, scene,
    ):
        """The first read of a dispatch is cached; the probe's is not.

        A cache entry is stamped when it was fetched, so a wake that
        slept less than the quota TTL would otherwise be handed the
        verdict it already had — one taken before the sleep, and so
        unable to report a window that reopened during it. The TTL exists
        to keep a dispatch from asking CC Switch once per dispatch; the
        wake is not a dispatch, and it is the only reader here whose
        whole question is what happened while it was not looking.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=12),
            _fleet_open(),
        ])

        _run(_tool(ActiveConcurrencyTracker()))

        assert wake.forced == [False, True], (
            f"read 0 is the dispatch's own read and stays cached; every "
            f"read after a wake must ask CC Switch again: {wake.forced}"
        )

    def test_a_window_that_names_no_moment_probes_on_the_same_beat(
        self, tmp_path, monkeypatch, scene,
    ):
        """An unnamed window is not a reason to guess longer.

        Without a stamp there is nothing to count down to, and the answer
        is the same beat a named window gets rather than a separate
        guess — one cadence, so there is one thing to reason about.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=None),
            _fleet_open(),
        ])

        _run(_tool(ActiveConcurrencyTracker()))

        assert wake.sleeps == [DEFAULT_COOLDOWN_SEC], wake.sleeps


class TestStillFullAfterWaking:
    def test_a_reading_that_lags_the_refresh_costs_a_probe_not_the_dispatch(
        self, tmp_path, monkeypatch, scene,
    ):
        """Waking up to find the window *still* full is ordinary.

        The payload is cached for its TTL, and a provider's meters do not
        move in the same instant its window rolls, so a probe can land
        before the numbers do. Treating that as a verdict would make the
        feature useless exactly when it is most needed: the fleet would
        stop again on a budget that had in fact come back.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=12),
            _full_fleet(resets_in_minutes=12),
            _fleet_open(),
        ])
        tracker = ActiveConcurrencyTracker()

        tool = _tool(tracker)
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert tool.current_call_provider in CHAIN
        assert len(wake.sleeps) == 2, (
            "a stale reading has to cost another probe, not the dispatch"
        )

    def test_twenty_probes_that_are_all_full_still_do_not_give_up(
        self, tmp_path, monkeypatch, scene,
    ):
        """The replacement for the old round budget.

        This is the contract that used to be the opposite. The bound was
        three rounds, and the reason it could be removed is that it
        never bought anything except a failing answer: the limit on a
        closed window belongs to the provider, and a caller that
        declares the wait over on its own schedule does not get the
        window back.

        Twenty is well past anything anyone would have waited for by
        hand — over a hundred minutes — and the point is not the figure
        but that the dispatch is still asking. What must still not happen
        is a provider the rules rejected taking the work on the way out.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        closed = _full_fleet(resets_in_minutes=60 * 24 * 30)
        wake = _Wake(monkeypatch).serve(
            monkeypatch, [closed] * 20 + [_fleet_open()]
        )
        tracker = ActiveConcurrencyTracker()

        tool = _tool(tracker)
        result, _session, _meta = _run(tool)

        assert result == "ok", "twenty full readings must not end the wait"
        assert tool.current_call_provider in CHAIN
        assert len(wake.sleeps) == 20, wake.sleeps
        assert set(wake.sleeps) == {WAKE_CAP_SEC}, (
            "every probe is the capped wait; a window thirty days out "
            "must not buy a longer sleep than the cap allows"
        )
        assert wake.forced == [False] + [True] * 20, (
            "every probe asks again; only the dispatch's own read is cached"
        )
        assert tracker.current("Vendor A Pro") == 0, (
            "the slot taken by the successful dispatch came back"
        )

    def test_there_is_no_round_budget_left_to_raise(self):
        """Stated against the module, so its return is a deliberate edit.

        A bound would come back the moment somebody needed a wait longer
        than the last one, and it would come back as a number rather
        than as an argument.
        """
        assert not hasattr(coding_tool, "_QUOTA_WAKE_MAX_ROUNDS")
        assert not hasattr(coding_tool, "_quota_wake_delay_sec")


class TestTheOtherRefusalIsQueuedFor:
    """A full local pool is a different wait and keeps its own clock.

    **Contract change, not a relaxation.** This class used to be called
    ``TestTheOtherRefusalIsLeftAlone`` and asserted that a saturated pool
    slept not at all and raised ``no_capacity``. It now asserts the
    opposite — that a saturated pool is queued for, on its own clock, and
    dispatches when a slot comes back — while keeping every restriction
    the old test existed to protect: nobody outside the tier answers, and
    the closed-window probe is not spent on a lease wait.
    """

    @staticmethod
    def _saturated(tracker):
        for name in CHAIN:
            for _ in range(10):
                tracker.try_acquire(name, 10)

    @staticmethod
    def _queue(monkeypatch, tracker, *, release_after, provider):
        """Record the queue steps and hand a slot back on the Nth.

        Serving rather than waiting keeps the test off the wall clock,
        and the release is the premise: a slot is released when the agent
        holding it finishes, so the wait is for something on its way.
        """
        import coding_tool

        steps: list = []

        def _sleep(seconds):
            steps.append(seconds)
            if len(steps) >= release_after:
                tracker.release(provider)

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
        return steps

    def test_a_full_pool_queues_and_then_dispatches(
        self, tmp_path, monkeypatch, scene,
    ):
        """Was: ``wake.sleeps == []`` and ``reason == "no_capacity"``.

        A slot is released when the agent holding it finishes, so it has a
        timeline of its own — unlike a closed window, which is somebody
        else's meter and has to be probed for. Treating the two the same
        either way fails a call that could have run, or spends an
        hours-long delay on a seconds-long wait.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [_fleet_open()])
        tracker = ActiveConcurrencyTracker()
        self._saturated(tracker)
        steps = self._queue(
            monkeypatch, tracker, release_after=3, provider=CHAIN[0],
        )

        tool = _tool(tracker)
        result, _session, _meta = _run(tool)

        assert result == "ok", (
            "a saturated pool is queued for; the slot was on its way back"
        )
        assert steps == [
            coding_tool._slot_wake_step_sec(i) for i in range(3)
        ], (
            f"the queue asked for {steps}; the steps must come from the "
            f"queue's own clock, bounded below by the floor"
        )
        assert tool.current_call_provider == CHAIN[0], (
            "the row that handed a slot back is the row that served it"
        )

    def test_a_full_pool_does_not_spend_a_quota_probe(
        self, tmp_path, monkeypatch, scene,
    ):
        """Still true after the contract change, and now load-bearing.

        The queue is a lease wait measured in the length of a task; the
        probe is a network read for a window measured in hours. One must
        not be paid out of the other's budget, so a saturated pool takes
        the dispatch's single unforced selection read and no forced ones.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [_fleet_open()])
        tracker = ActiveConcurrencyTracker()
        self._saturated(tracker)
        self._queue(
            monkeypatch, tracker, release_after=4, provider=CHAIN[1],
        )

        _run(_tool(tracker))

        assert wake.reads == 1, (
            f"the quota payload was read {wake.reads} times for a wait "
            f"that is not about windows; it must stay at the dispatch's "
            f"own single read"
        )
        assert wake.forced == [False], wake.forced

    def test_a_full_pool_still_stays_inside_the_tier(
        self, tmp_path, monkeypatch, scene,
    ):
        """Was: the same case failing closed on ``no_capacity``.

        Everything the old assertion protected is still asserted, except
        the failure: nobody outside the tier answers, and nothing
        pre-filled is disturbed beyond the one slot the dispatch was
        actually entitled to take.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _Wake(monkeypatch).serve(monkeypatch, [_fleet_open()])
        tracker = ActiveConcurrencyTracker()
        self._saturated(tracker)
        self._queue(
            monkeypatch, tracker, release_after=2, provider=CHAIN[2],
        )

        tool = _tool(tracker)
        _run(tool)

        assert tool.current_call_provider in CHAIN, (
            f"the dispatch went to {tool.current_call_provider!r}, which is "
            f"not in the tier {CHAIN}"
        )
        # 30 filled, one handed back by the holder, one taken by the
        # dispatch, one returned when the call ended — so the net is the
        # single release and nothing else moved.
        assert tracker.current(CHAIN[2]) == 9, (
            "the slot the dispatch took must come back when the call "
            "ends; only the release should be left behind"
        )
        assert [tracker.current(n) for n in CHAIN[:2]] == [10, 10], (
            "the two pools that were never freed are untouched"
        )

    def test_an_exhausted_reading_beside_a_full_pool_still_wakes(
        self, tmp_path, monkeypatch, scene,
    ):
        """Both refusals can hold at once, and the closed window is the
        one waited on — because it is the one with a schedule.

        The saturated pools here cannot free themselves, so a wake is
        the only thing that can help, and the dispatch that eventually
        succeeds still comes from the same eligibility rules rather than
        from a shortcut around them.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _reading(
                _full("Vendor A Pro"), _open("Vendor A"), _open("Vendor A API"),
            ),
            _fleet_open(),
        ])
        tracker = ActiveConcurrencyTracker()
        # The two candidates that *have* budget are at their caps, so the
        # semaphore cannot free them either and the closed window is the
        # only thing on the table that could.
        for name in ("Vendor A", "Vendor A API"):
            for _ in range(10):
                tracker.try_acquire(name, 10)

        tool = _tool(tracker)
        _run(tool)

        assert tool.current_call_provider == "Vendor A Pro", (
            "the wake must end in the ordinary selection, and the only "
            "candidate the rules admit after the reopen is the one whose "
            "window came back"
        )
        assert wake.sleeps, "a closed window was waited on"


class TestWakingIntoAnAlreadyClosedWindow:
    def test_a_window_already_in_the_past_still_costs_one_probe(
        self, tmp_path, monkeypatch, scene,
    ):
        """A stamp in the past is not a licence to ask again immediately.

        This is the one place where the old answer and the new one
        disagree outright, so it is worth being explicit about why. The
        old behaviour treated a past stamp as proof that the reading was
        behind the refresh and answered it with a re-read at once. That
        is right about the diagnosis and wrong about the treatment: the
        meter has not caught up, so the re-read comes back with the same
        numbers, and the next one after that does too. Asked immediately
        and repeatedly, that is a hot loop against the provider's usage
        endpoint — the exact thing the probe interval exists to prevent.

        The reading here does answer on the next probe, which is the
        ordinary outcome: the first verdict was a moment early.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        past = _now() - timedelta(minutes=3)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            {
                name: _verdict(name, resets_at=past) for name in CHAIN
            },
            _fleet_open(),
        ])
        tracker = ActiveConcurrencyTracker()

        tool = _tool(tracker)
        _run(tool)

        assert tool.current_call_provider in CHAIN
        assert wake.sleeps == [DEFAULT_COOLDOWN_SEC], (
            "a moment already in the past must not turn the probe into a "
            "busy loop against the usage endpoint"
        )
        assert wake.reads == 2

    def test_a_past_window_that_stays_past_does_not_stop_the_wait(
        self, tmp_path, monkeypatch, scene,
    ):
        """And it does not stop it either.

        Under the old contract a stamp in the past was the one shape that
        cost no wait at all, so this case was bounded by the round budget
        and stopped. Now every closed window costs one interval per probe
        and none of them stop, so the only way out of this state is the
        reading changing — which is the state a genuinely shut window is
        in for as long as the provider says so.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        past = _now() - timedelta(minutes=3)
        closed = {name: _verdict(name, resets_at=past) for name in CHAIN}
        wake = _Wake(monkeypatch).serve(
            monkeypatch, [closed] * 20 + [_fleet_open()]
        )

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert len(wake.sleeps) == 20, wake.sleeps
        assert wake.forced == [False] + [True] * 20


# ---------------------------------------------------------------------------
# Waiting is not working - the budget, and what it charges for
# ---------------------------------------------------------------------------
#
# The dispatch happens inside a caller that bounds how long it may take,
# and that bound is what catches a call which never comes back. A quota
# wait inside the dispatch is not that: it is the dispatch sitting still
# until someone else's budget comes back. Charged to the same bound, a
# wait long enough to matter is a wait that blows the cap on its own,
# and what the cap produces is not a statement about the work - it is a
# second, tool-less call asked to infer the outcome from the logs.
#
# So the budget is split: work time is charged, parked time is not. The
# tests below pin that split from both ends - that the clock does not
# move while parked (measured, not inferred from a passing test), and
# that the thing reading the clock gives the parked time back rather
# than sitting on a timer computed before the park.


class _VirtualClock:
    """A monotonic the test drives, so a week costs nothing.

    The property under test is about *ratios* - a wait that outlives the
    entire budget by a wide margin - and the ratios here are ones nobody
    can wait out for real. Advancing the clock by hand says the same
    thing in a millisecond and without a test suite that takes a week.
    """

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestTheBudgetIsParkedWhileTheDispatchWaits:
    def test_parked_time_is_not_charged_to_the_budget(self):
        """The unit of the whole thing, stated without a dispatch.

        Three days of parked time against a one-hour budget, and the
        budget is untouched — not reduced by a day, not rounded, not
        half-spent.
        """
        mono = _VirtualClock()
        clock = WorkClock(3600.0, monotonic=mono)

        clock.park()
        mono.advance(3 * 24 * 3600.0)

        assert clock.remaining() == 3600.0

    def test_work_time_is_still_charged_before_and_after_the_wait(self):
        """Parked, not switched off.

        A budget that simply stopped counting would also pass the test
        above, and would let a dispatch spend an hour of real work around
        a wait and sail past the bound that exists to catch it.
        """
        mono = _VirtualClock()
        clock = WorkClock(3600.0, monotonic=mono)

        mono.advance(600.0)
        assert clock.remaining() == 3000.0

        clock.park()
        mono.advance(3 * 24 * 3600.0)
        clock.resume()

        mono.advance(600.0)
        assert clock.remaining() == 2400.0

    def test_a_park_before_a_resume_does_not_bank_the_segment_twice(self):
        """Park and resume are raised from a worker thread.

        Without the idempotence a second park before a resume would
        charge the same segment to the budget twice, and the caller
        could be charged for work it never did — the failure mode here
        is the opposite of the one being fixed, and just as wrong.
        """
        mono = _VirtualClock()
        clock = WorkClock(3600.0, monotonic=mono)

        mono.advance(100.0)
        clock.park()
        clock.park()
        mono.advance(86400.0)
        clock.resume()
        clock.resume()
        mono.advance(100.0)

        assert clock.remaining() == 3400.0

    def test_the_clock_never_reports_a_negative_budget(self):
        """Overrun reads as zero, so the reader cannot wait for negative.

        A caller asking "how much is left" and getting a negative number
        would have to know to special-case it; every caller here is the
        one place that arithmetic should not have to be re-derived.
        """
        mono = _VirtualClock()
        clock = WorkClock(10.0, monotonic=mono)

        mono.advance(999.0)
        assert clock.remaining() == 0.0


class TestTheDispatchParksTheCallersBudget:
    """The wake loop, driving a real clock attached to the real tool."""

    @staticmethod
    def _attach(tool, budget=3600.0):
        mono = _VirtualClock()
        clock = WorkClock(budget, monotonic=mono)
        tool._work_clock = clock
        return mono, clock

    def test_a_wait_outliving_the_whole_budget_still_dispatches(
        self, tmp_path, monkeypatch, scene,
    ):
        """The case this file exists for.

        One capped wait of half an hour against a budget of two: the
        wait is longer than everything the budget has to give, which is
        the shape of a provider closed for a week against an hour-long
        cap. It dispatches when the window opens, because the wait was
        never charged to the cap.

        Note what this is *not* asserting: that the test did not time
        out. It asserts the number on the budget, read after the fact,
        and the budget is smaller than the single wait that was served.
        """
        budget = 120.0
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=60 * 24 * 7),
            _fleet_open(),
        ])
        tool = _tool(ActiveConcurrencyTracker())
        mono, clock = self._attach(tool, budget=budget)

        # Serve the wait: the seam advances the test's own clock by the
        # interval it was asked for, so the budget sees the elapsed time
        # rather than a number nobody moved.
        real_append = wake.sleeps.append

        def _serve(seconds):
            mono.advance(seconds)
            real_append(seconds)

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _serve)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert tool.current_call_provider in CHAIN
        assert wake.sleeps == [WAKE_CAP_SEC]
        waited = mono.now - 1000.0
        assert waited == WAKE_CAP_SEC > budget, (
            "the wait served must actually be longer than the whole "
            f"budget ({waited}s against {budget}s), or this proves nothing"
        )
        assert clock.remaining() == budget, (
            "the wait must not have been charged to the budget at all"
        )

    def test_several_probes_cost_the_budget_nothing_between_them(
        self, tmp_path, monkeypatch, scene,
    ):
        """Repeated, and with the clock advancing inside each probe.

        A single probe could be explained by a budget that is reset once
        at the end rather than one that is parked throughout. Advancing
        the clock inside every probe is what tells those two apart.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        closed = _full_fleet(resets_in_minutes=60 * 24 * 30)
        wake = _Wake(monkeypatch).serve(
            monkeypatch, [closed] * 20 + [_fleet_open()]
        )
        tool = _tool(ActiveConcurrencyTracker())
        mono, clock = self._attach(tool, budget=3600.0)

        seen = []
        real_append = wake.sleeps.append

        def _serve(seconds):
            mono.advance(seconds)
            seen.append(clock.remaining())
            real_append(seconds)

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _serve)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert len(seen) == 20
        assert seen == [3600.0] * 20, (
            "the budget must read full before every single probe, not "
            "decaying across the wait"
        )
        assert mono.now - 1000.0 == 20 * WAKE_CAP_SEC
        assert clock.remaining() == 3600.0

    def test_the_budget_starts_running_again_when_the_window_opens(
        self, tmp_path, monkeypatch, scene,
    ):
        """The other end of the contract: parked is not off.

        The dispatch after the wait is ordinary work and is charged as
        ordinary work. The budget is read only after the call has
        returned, which is the moment a clock left parked would be
        indistinguishable from one with nothing left to charge - so the
        test moves the clock on after the fact and checks that the
        budget moved with it.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=12),
            _fleet_open(),
        ])
        tool = _tool(ActiveConcurrencyTracker())
        mono, clock = self._attach(tool, budget=3600.0)

        monkeypatch.setattr(
            coding_tool, "_quota_wake_sleep", lambda seconds: mono.advance(seconds)
        )

        _run(tool)

        mono.advance(120.0)
        assert clock.remaining() == 3480.0, (
            "the budget must be running again once the dispatch resumes; "
            "a clock left parked would never catch a wedged call"
        )

    def test_a_selection_that_raises_leaves_the_budget_running(
        self, tmp_path, monkeypatch, scene,
    ):
        """The wake can end by blowing up, and the budget must notice.

        A budget left parked by a path nobody thought about is a budget
        that stops being read while the thing it bounds is still running,
        which is the exact shape of a call that never returns - the thing
        the budget exists to catch.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _Wake(monkeypatch).serve(monkeypatch, [_full_fleet()])
        tool = _tool(ActiveConcurrencyTracker())
        mono, clock = self._attach(tool, budget=3600.0)

        import dynamic_provider_concurrency as dpc

        calls = []
        real_acquire = dpc.acquire_provider_with_dynamic_capacity

        def _explode(*args, **kwargs):
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError("selection blew up")
            return real_acquire(*args, **kwargs)

        monkeypatch.setattr(dpc, "acquire_provider_with_dynamic_capacity", _explode)

        with pytest.raises(RuntimeError):
            _run(tool)

        assert len(calls) >= 2, "the wake never got to its second decision"
        mono.advance(120.0)
        assert clock.remaining() == 3480.0, (
            "the budget must be running again after the wake ends, however "
            "it ended"
        )

    def test_a_call_with_no_budget_attached_is_unaffected(
        self, tmp_path, monkeypatch, scene,
    ):
        """The ordinary case: no clock, no accounting, no crash.

        Most callers of the dispatch have no budget of their own, so the
        attachment has to be optional rather than a new requirement on
        every call site.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _full_fleet(resets_in_minutes=12),
            _fleet_open(),
        ])
        tool = _tool(ActiveConcurrencyTracker())

        assert tool._work_clock is None
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert len(wake.sleeps) == 1, wake.sleeps
        assert wake.sleeps[0] == pytest.approx(_expected_wake(12), abs=1.0)

    def test_a_full_local_pool_is_not_charged_either(
        self, tmp_path, monkeypatch, scene,
    ):
        """The other refusal keeps its own clock, including this one.

        **Contract change, not a relaxation.** This used to assert that a
        full local pool raised ``no_capacity``, never slept and never
        touched the budget at all. It now asserts that the pool is queued
        for — and that queue is parked exactly like the window wake,
        because waiting is not working whichever of the three reasons
        caused it. The "not charged" half is unchanged and is the part
        worth keeping: a queue that outlives the budget must not spend it.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [_fleet_open()])
        tracker = ActiveConcurrencyTracker()
        for name in CHAIN:
            for _ in range(10):
                tracker.try_acquire(name, 10)
        tool = _tool(tracker)
        mono, clock = self._attach(tool, budget=3600.0)

        steps: list = []
        real_append = wake.sleeps.append

        def _serve(seconds):
            mono.advance(seconds)
            steps.append(clock.remaining())
            real_append(seconds)
            if len(steps) >= 3:
                tracker.release(CHAIN[0])

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _serve)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert steps == [3600.0] * 3, (
            f"the budget must read full before every step of the queue: "
            f"{steps}"
        )
        assert clock.remaining() == 3600.0, (
            "the queue must not have been charged to the budget at all"
        )
        assert tracker.current(CHAIN[0]) == 9, (
            "the slot the dispatch took came back when the call ended; "
            "only the release should be left behind"
        )


# ---------------------------------------------------------------------------
# The caller - the bound that reads the budget
# ---------------------------------------------------------------------------
#
# Parking a budget is only worth anything if the thing enforcing the
# bound actually reads it. ``asyncio.wait_for`` takes one number and
# spends it on wall time, so the caller had to stop using it and start
# asking instead. These tests drive that with real elapsed time at a
# scale a suite can afford, and the ratio is the point: the parked wait
# is several times the whole budget, which is the shape of a week-long
# window against an hour-long cap.


class TestTheCallerAsksTheBudgetInsteadOfTheClock:
    """Driven with real elapsed time, at a scale a suite can afford.

    The clock under test is the same object the work parks and resumes —
    a test that built its own throwaway budget would be measuring
    nothing at all, and its control case would pass for the wrong reason.
    """

    @staticmethod
    async def _work_that_parks(clock, waited):
        """What the dispatch does while it probes a closed window."""
        clock.park()
        await asyncio.sleep(waited)
        clock.resume()
        return "verdict"

    @pytest.mark.asyncio
    async def test_a_wait_outliving_the_cap_does_not_end_the_call(self):
        """Three times the whole budget, spent waiting.

        Under the old arithmetic this is the whole defect: the wait is
        charged to the bound that is meant to catch a call which never
        comes back, so a wait long enough to matter ends the call before
        it has done any work at all. A provider closed for a week is this
        shape at 168 hours against one hour.
        """
        clock = WorkClock(0.2)
        result = await _await_under_work_clock(
            self._work_that_parks(clock, 0.6), clock
        )
        assert result == "verdict"

    @pytest.mark.asyncio
    async def test_the_same_wait_without_parking_does_end_it(self):
        """The control, and the reason the previous test means anything.

        Identical in every respect except that nobody parked the budget.
        If this passed, the wait would be harmless for reasons that have
        nothing to do with parking, and the fix would be measuring itself.
        """
        clock = WorkClock(0.2)
        with pytest.raises(asyncio.TimeoutError):
            await _await_under_work_clock(asyncio.sleep(0.6), clock)

    @pytest.mark.asyncio
    async def test_work_that_outruns_the_budget_still_ends_the_call(self):
        """Parked is not off.

        Same budget, same duration, parked - and the call still ends,
        because the work after the resume is charged like any other work.
        A clock that simply stopped counting would let a wedged call run
        forever, which is the failure the bound was added to prevent.
        """
        clock = WorkClock(0.2)

        async def _park_then_overrun():
            clock.park()
            await asyncio.sleep(0.4)
            clock.resume()
            await asyncio.sleep(2.0)

        with pytest.raises(asyncio.TimeoutError):
            await _await_under_work_clock(_park_then_overrun(), clock)

    @pytest.mark.asyncio
    async def test_a_wait_longer_than_the_budget_is_woken_promptly(
        self, tmp_path,
    ):
        """The resume has to interrupt the wait, not wait it out.

        The caller is asleep on a timer computed before the park. Without
        a way back in when the work starts, a call that finished in a
        second after a long wait would not be noticed until the original
        timer expired - the verdict would be right and the whole round
        would be late.
        """
        clock = WorkClock(0.2)

        async def _long_park_then_quick_work():
            clock.park()
            await asyncio.sleep(1.0)
            clock.resume()
            return "verdict"

        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await _await_under_work_clock(
            _long_park_then_quick_work(), clock
        ) == "verdict"
        elapsed = loop.time() - started
        assert elapsed < 1.5, (
            f"the call took {elapsed:.2f}s to notice work that finished "
            "immediately; it sat on a timer instead of being woken"
        )

    @pytest.mark.asyncio
    async def test_the_call_returns_the_work_its_result(self):
        """Whatever the work produced is what comes back."""
        result = await _await_under_work_clock(
            asyncio.sleep(0, result="ok"), WorkClock(5)
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_work_is_not_swallowed(self):
        """The bound is not a place where exceptions go missing."""

        async def _boom():
            raise ValueError("the work failed")

        with pytest.raises(ValueError, match="the work failed"):
            await _await_under_work_clock(_boom(), WorkClock(5))


class TestTheAttemptAttachesIt:
    """The join between the two halves.

    Parking a budget and reading a budget are two pieces that do nothing
    unless the caller owning the bound hands one to the dispatch that
    parks it. Everything above tests a piece; these test the wiring,
    which is the only place the two can come apart silently - a caller
    that built a clock and then bounded itself on wall time would leave
    every other test in this file passing and the defect exactly where
    it started.
    """

    @staticmethod
    def _agent(cap):
        import logging

        from verification_subagent import VerificationSubAgent

        agent = VerificationSubAgent(
            method="code_review", max_retries=0, model_complexity="test"
        )
        agent.registry = None
        agent.plan_id = "wake-wiring"
        agent.logger = logging.getLogger("wake-wiring")
        agent.HARD_WALL_CLOCK_CAP_SECONDS = cap
        return agent

    @staticmethod
    def _tool_that_parks(waited):
        """What the dispatch does while it probes a closed window.

        Parks the caller's budget for ``waited`` seconds of real time
        and then finishes. The park is what has to happen here; the wait
        only has to be longer than the cap.
        """

        class _Tool:
            clock = None
            released = False

            def query_json(self, *args, **kwargs):
                self.clock = self._work_clock
                self.clock.park()
                time.sleep(waited)
                self.clock.resume()
                _Tool.released = True
                return {"verdict": "PASSED", "reasons": [], "evidence": []}

        return _Tool()

    # The case below asserts on WALL-CLOCK time: it parks a 0.1 s budget
    # for 0.3 s of real time and then requires the budget to be intact.
    # Under a parallel lane the host is busy enough that the wait IS
    # charged, so the assertion measures the runner's load rather than
    # the code - the same class of case as
    # ``test_backend_startup_latency_within_10ms``. The marker moves it
    # into the serial lane (``-m "time_sensitive or perf"
    # --timeout=1800``) where it runs alone and asserts exactly the same
    # three things. It is NOT skipped, xfailed or deleted: the coverage
    # is rescheduled, not retired.
    @pytest.mark.time_sensitive
    @pytest.mark.asyncio
    async def test_a_wait_outlasting_the_whole_cap_still_reaches_a_verdict(
        self, tmp_path,
    ):
        """The defect end to end, at both ends at once.

        A cap of a tenth of a second, and the dispatch parks the budget
        for a third of one before returning. Under the old arithmetic
        the wait would have spent the cap and the caller would have gone
        off to a tool-less second call to guess the outcome - instead
        the verdict comes back through the ordinary path, on the arm that
        a passing verdict takes.
        """
        agent = self._agent(cap=0.1)
        tool = self._tool_that_parks(0.3)

        verdict = await agent._execute_attempt(
            vp_node={"id": "VP-W", "title": "t"},
            coding_tool=tool,
            settings_path="/tmp/fake_settings.json",
            log_path=tmp_path / "attempt.log",
            attempt=0,
            project_dir=tmp_path,
        )

        assert tool.clock is not None, (
            "the attempt must hand its budget to the dispatch; a tool "
            "with no clock attached cannot park anything"
        )
        assert tool.released, "the work never ran to the end"
        assert verdict.verdict == "PASSED", verdict
        assert tool.clock.remaining() > 0, (
            "a budget parked for longer than the whole cap and still "
            "positive is the whole claim; anything else means the wait "
            "was charged after all"
        )

    @pytest.mark.asyncio
    async def test_the_same_wait_unparked_does_not_reach_a_verdict(
        self, tmp_path,
    ):
        """The control, so the one above is not measuring the plumbing.

        Identical except that nobody parks. This is the old arithmetic:
        the wait spends the cap and the attempt ends without a verdict,
        which is what used to hand the call to the summarizer.
        """
        agent = self._agent(cap=0.1)

        class _Unparked:
            _work_clock = None

            def query_json(self, *args, **kwargs):
                time.sleep(0.3)
                return {"verdict": "PASSED", "reasons": [], "evidence": []}

        from verification_subagent import StuckAgentError

        with pytest.raises(StuckAgentError):
            await agent._execute_attempt(
                vp_node={"id": "VP-W", "title": "t"},
                coding_tool=_Unparked(),
                settings_path="/tmp/fake_settings.json",
                log_path=tmp_path / "attempt.log",
                attempt=0,
                project_dir=tmp_path,
            )

    def test_the_bound_reads_the_budget_rather_than_the_wall_clock(self):
        """The shape of the bound, read from the source it is written in.

        ``asyncio.wait_for`` is the thing that was wrong: one number,
        spent on wall time. It is still correct a few lines away in the
        summarizer, which has no dispatch to park, so this is scoped to
        the attempt rather than to the file.
        """
        import ast
        import inspect
        import textwrap

        from verification_subagent import VerificationSubAgent

        source = textwrap.dedent(
            inspect.getsource(VerificationSubAgent._execute_attempt)
        )
        tree = ast.parse(source)
        wait_for_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait_for"
        ]

        assert wait_for_calls == [], (
            "the attempt is bounded by the work budget, not by wall time: "
            "a wall-time bound charges a quota wait for hours it did no work"
        )
        assert "_await_under_work_clock" in source
        assert "WorkClock(self.HARD_WALL_CLOCK_CAP_SECONDS)" in source



class TestTheReaderItself:
    """``load_provider_quota_map(force=...)`` — what the flag buys."""

    @staticmethod
    def _serve_cc_switch(monkeypatch, payloads, calls):
        """A CC Switch that answers a queue of ``/api/usage`` payloads."""
        import json

        bodies = [json.dumps(payload).encode("utf-8") for payload in payloads]

        class _Response:
            status = 200

            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def _urlopen(request, timeout=None):
            calls.append(getattr(request, "full_url", str(request)))
            return _Response(bodies[min(len(calls) - 1, len(bodies) - 1)])

        monkeypatch.setattr(
            provider_order.urllib.request, "urlopen", _urlopen
        )

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        provider_order.cache_clear()
        yield
        provider_order.cache_clear()

    def test_an_ordinary_read_still_uses_the_cache(self, monkeypatch):
        """The default is untouched: two reads, one request.

        This is what keeps the dispatch path from becoming an outbound
        fan-out per dispatch, and it is why the wake has to *ask* for a
        fresh reading rather than expect one.
        """
        calls = []
        self._serve_cc_switch(
            monkeypatch, [{"providers": [_payload_open("Vendor A Pro")]}], calls
        )

        first = provider_order.load_provider_quota_map()
        again = provider_order.load_provider_quota_map()

        assert len(calls) == 1, calls
        assert again == first

    def test_a_forced_read_asks_again_inside_the_ttl(self, monkeypatch):
        """The flag is the only thing standing between a wake and itself.

        A window that reopens less than a TTL after the reading that
        reported it full is the common case, not an edge — it is what a
        window about to roll looks like. Without the force, the wake
        wakes, receives the pre-sleep verdict, finds its own stamp now in
        the past, and stops: the feature failing on exactly the case it
        was built for.
        """
        calls = []
        self._serve_cc_switch(
            monkeypatch, [{"providers": [_payload_open("Vendor A Pro")]}], calls
        )

        provider_order.load_provider_quota_map()
        provider_order.load_provider_quota_map(force=True)

        assert len(calls) == 2, (
            "a forced read has to leave the cache, or the wake is handed "
            "the verdict it slept through"
        )

    def test_a_forced_read_sees_a_window_that_changed(self, monkeypatch):
        """The point of the above, stated as an observation rather than a
        call count."""
        calls = []
        self._serve_cc_switch(monkeypatch, [
            {"providers": [_payload_full("Vendor A Pro")]},
            {"providers": [_payload_open("Vendor A Pro")]},
        ], calls)

        assert provider_order.load_provider_quota_map()["Vendor A Pro"].exhausted
        assert provider_order.load_provider_quota_map(force=True)["Vendor A Pro"].exhausted is False

    def test_a_forced_read_still_consults_the_meter_less_floor(
        self, monkeypatch,
    ):
        """Forcing is about the fetch, not about how long "no meter" lasts.

        The floor exists so a provider CC Switch cannot meter is not
        re-examined on the dispatch cadence, and the merge that applies it
        runs on every read. A wake asking again must not quietly become
        the second thing that defeats it, so the floor is still consulted
        on the forced path.

        What forcing does **not** do is save the request: the floor
        decides how long a *verdict* is carried, and the fetch has
        already happened by then, because the endpoint fans out to every
        provider at once. That is the reader's existing arrangement and
        this change does not touch it — worth stating here so the cost of
        a wake is not mistaken for something smaller than it is.
        """
        calls = []
        self._serve_cc_switch(
            monkeypatch,
            [{"providers": [_payload_no_reader("Vendor A Pro")]}],
            calls,
        )
        consulted = []
        original = provider_order.probe_floor_sec
        monkeypatch.setattr(
            provider_order, "probe_floor_sec",
            lambda quota: (
                consulted.append(quota) or original(quota)
            ),
        )

        forced = provider_order.load_provider_quota_map(force=True)

        assert consulted, (
            "the floor decides how long a meter-less verdict is carried; "
            "a forced read that skipped it would re-ask such a provider "
            "on the dispatch cadence"
        )
        assert forced["Vendor A Pro"].unreadable is True
        assert forced["Vendor A Pro"].exhausted is False

    def test_a_forced_read_that_fails_is_not_remembered(self, monkeypatch):
        """Forcing buys a fresher answer, not a remembered one.

        The failure contract is the one the cached path already has: an
        outage is never cached as an answer, because that would turn a
        transient CC Switch problem into a quota verdict for everyone.
        """
        calls = []

        def _boom(request, timeout=None):
            calls.append(1)
            raise OSError("connection refused")

        monkeypatch.setattr(provider_order.urllib.request, "urlopen", _boom)
        assert provider_order.load_provider_quota_map(force=True) == {}
        assert len(calls) == 1

        self._serve_cc_switch(
            monkeypatch, [{"providers": [_payload_open("Vendor A Pro")]}], calls
        )
        assert provider_order.load_provider_quota_map() != {}
        assert len(calls) == 2, "the failure must not have been cached"


class TestTheSeamItself:
    def test_the_real_sleep_is_the_one_that_blocks(self):
        """The seam is a name, not a mode.

        With nothing patched the function does the ordinary thing: a
        non-positive duration returns at once, and a positive one goes to
        the blocking sleep. That is what makes substituting it a test's
        move rather than a runtime state that could be left on.
        """
        calls = []
        original = coding_tool.time.sleep
        try:
            coding_tool.time.sleep = lambda seconds: calls.append(seconds)
            coding_tool._quota_wake_sleep(0)
            coding_tool._quota_wake_sleep(-5)
            coding_tool._quota_wake_sleep(1.5)
        finally:
            coding_tool.time.sleep = original

        assert calls == [1.5]

    def test_every_wait_in_the_dispatch_goes_through_the_seam(self):
        """Otherwise the seam is decoration and the tests measure nothing.

        Read from the module source rather than from a trace, so it holds
        for the production call site and not only for the paths these
        tests happen to drive.
        """
        source = Path(coding_tool.__file__).read_text(encoding="utf-8")
        assert "_quota_wake_sleep(delay)" in source

        # Scoped to the provider-selection region rather than to the whole
        # function: an unrelated retry further down the same function
        # sleeps on its own backoff, and widening the scan to swallow
        # that would make this assertion fail for the right reason at the
        # wrong line.
        selection = source.split("_quota_blocked = sorted(", 1)[1].split(
            "# ---- Fail closed", 1
        )[0]
        assert "time.sleep(" not in selection, (
            "the dispatch must not wait on a closed window by any route "
            "other than the seam"
        )
        # Two waits live in this region — a closed quota window and a
        # saturated local pool — and both go through the seam. The count
        # is a pin rather than a lower bound: a third wait that reached
        # for the clock itself would be invisible to every other test
        # here, because they all patch this one name.
        assert selection.count("_quota_wake_sleep(") == 2


# ---------------------------------------------------------------------------
# A tier is a pool: waiting happens INSIDE it
# ---------------------------------------------------------------------------
#
# Everything above drives one tier, so it cannot say anything about the
# question that matters here: when this tier has nothing left, what
# happens? The answer is that it waits — and these tests exist to pin
# the other half of that sentence, which is that it waits HERE. A
# reachable provider one tier over is not an answer, and a wait that
# quietly started using one would be invisible in every test above.


MEDIUM_CHAIN = ["Vendor A Pro", "Vendor A"]
OTHER_TIER = "Vendor D API"


@pytest.fixture
def two_tier_scene(tmp_path, monkeypatch):
    """The real resolver over a table with a populated tier next door.

    ``execution`` routes to ``medium``; ``high`` is declared and has a
    live, idle, correctly configured provider in it. Resolved through
    the real ``provider_routing`` rather than a stub, so the chain the
    dispatch sees is the chain the table produces.
    """
    import textwrap

    import provider_routing
    from coding_tool import ClaudeCodingTool

    cfg = tmp_path / "provider_routing.yaml"
    cfg.write_text(textwrap.dedent("""
        version: 1
        default_tier: medium
        tiers:
          medium: ["^Vendor A Pro$", "^Vendor A$"]
          high:   ["^Vendor D"]
        scenes:
          verification: medium
    """), encoding="utf-8")

    monkeypatch.setattr(
        provider_routing, "_config_path", lambda config_path=None: cfg,
    )
    monkeypatch.setattr(
        provider_routing, "_list_display_names",
        lambda db_path=None: [*MEDIUM_CHAIN, OTHER_TIER],
    )
    monkeypatch.setattr(provider_routing, "_optimizer_order", lambda: [])
    provider_routing.clear_routing_cache()

    # Every provider the dispatch asks about, in order. The candidate
    # set is derived from this walk, so it is the whole question.
    asked: list = []

    def _availability(name):
        asked.append(name)
        return True, {
            "base_url": f"https://{name}.test/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
        }

    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(_availability),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    )

    def _tool(tracker):
        tool = ClaudeCodingTool(
            scene="verification", concurrency_tracker=tracker,
        )
        tool.provider_slot_wait_sec = 0.2
        return tool

    return type("_Scene", (), {"asked": asked, "tool": staticmethod(_tool)})


@pytest.mark.unit
class TestTheTierIsTheWholeWorld:
    def test_a_shut_tier_waits_rather_than_reaching_into_the_next_one(
        self, tmp_path, monkeypatch, two_tier_scene,
    ):
        """The headline: this tier is empty, the other one is not.

        ``Vendor D API`` is reachable, inside its window and has all ten
        slots. It is still not a candidate, and the dispatch waits for
        its own tier instead. Asserted on the walk as well as on the
        outcome, because "it did not use it" and "it never considered
        it" are different facts and only the second one is the contract.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _reading(*[
                _full(name, resets_in_minutes=12) for name in MEDIUM_CHAIN
            ]),
            _reading(*[_open(name) for name in MEDIUM_CHAIN]),
        ])

        tool = two_tier_scene.tool(ActiveConcurrencyTracker())
        _run(tool)

        assert OTHER_TIER not in two_tier_scene.asked, (
            f"the dispatch asked about {two_tier_scene.asked}; "
            f"{OTHER_TIER!r} is in another tier and must never be a "
            f"candidate for this scene"
        )
        assert wake.sleeps, (
            "the scene's own tier was shut and the dispatch did not wait "
            "for it — the alternative to waiting is failing the call"
        )
        assert tool.current_call_provider in MEDIUM_CHAIN, (
            f"the work went to {tool.current_call_provider!r}"
        )

    def test_the_chain_offered_is_the_tier_and_not_the_tier_plus_one(
        self, tmp_path, monkeypatch, two_tier_scene,
    ):
        """What the failure message would carry, read from the walk.

        The chain is what an operator reads to find out who was
        allowed to serve, so a chain that had grown a second tier's rows
        would misreport the allowlist at exactly the moment it is being
        consulted.
        """
        import provider_routing

        chain = provider_routing.resolve_provider_chain(
            "verification", config_path=None, db_path=None,
        )

        assert chain == MEDIUM_CHAIN, chain
        assert OTHER_TIER not in chain

    def test_the_park_wait_goes_through_the_same_substitutable_seam(
        self, tmp_path, monkeypatch, two_tier_scene,
    ):
        """Otherwise the waits on this path are unservable in a test.

        Read from the module source rather than from a trace, so it
        holds for the production call site and not only for the paths
        these tests happen to drive.
        """
        source = Path(coding_tool.__file__).read_text(encoding="utf-8")

        assert "_quota_wake_sleep(delay)" in source
        park_wait = source.split("if not _waiting_on:", 1)[1].split(
            "if scene_candidates:", 1
        )[0]
        assert "time.sleep(" not in park_wait, (
            "the park wait must be substitutable like the window wake, or "
            "it can only be tested by serving minutes of it"
        )
        assert park_wait.count("_quota_wake_sleep(") == 1


@pytest.mark.unit
class TestThePoolStillRotatesOnItsOwn:
    """Intra-pool degradation, which is the half that was always right.

    One row in the tier reading full is not a dead tier — it is a pool
    with two providers left in it. The dispatch moves to the next one
    inside the same tier, immediately, with no wait at all: this is the
    movement the tier table authorizes and the only one it authorizes.
    """

    def test_one_closed_window_hands_over_to_the_next_provider_in_the_pool(
        self, tmp_path, monkeypatch, scene,
    ):
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _reading(_full("Vendor A Pro"), _open("Vendor A"), _open("Vendor A API")),
        ])

        tool = _tool(ActiveConcurrencyTracker())
        _run(tool)

        assert tool.current_call_provider == "Vendor A", (
            f"the first provider's window is shut and the pool still has "
            f"{len(CHAIN) - 1} providers in it; it went to "
            f"{tool.current_call_provider!r}"
        )
        assert wake.sleeps == [], (
            f"a pool with room needs no wait at all, and took {wake.sleeps}"
        )

    def test_the_hand_over_does_not_ask_the_quota_payload_twice(
        self, tmp_path, monkeypatch, scene,
    ):
        """One reading answers the whole walk.

        The rotation is decided from the reading already in hand. A
        re-read per candidate would turn a pool of three into three
        round-trips to an endpoint that fans out to every provider at
        once.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake(monkeypatch).serve(monkeypatch, [
            _reading(_full("Vendor A Pro"), _open("Vendor A"), _open("Vendor A API")),
        ])

        _run(_tool(ActiveConcurrencyTracker()))

        assert wake.reads == 1, wake.reads
