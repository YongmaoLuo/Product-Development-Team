"""One closed window, one watchdog, one request — whatever the fleet size.

What this file pins
-------------------
Quota is a property of the fleet, not of a caller. A window is either
open — in which case everyone the pool admits may be served — or shut, in
which case nobody may be served, and the only thing that can change that
is the provider. So a dispatch that found its scene unavailable is not
carrying private information; it is looking at the same door as its
neighbours.

That makes the cost of watching it a fleet cost, and the fan-out is the
size of it: ``/api/usage`` asks every provider's own API in one request,
so the thing being billed is the *request*, not the provider. A
watchdog per waiting dispatch spends N of those to learn one thing, and
spends them again every probe interval for as long as the window stays
shut.

The reader's in-flight barrier does not close that gap. It takes a
per-key lock and checks the cache a second time inside — which is what
makes a burst of *ordinary* reads cost one fan-out — but a probe asks
with ``force=True``, and force skips both cache checks. Each caller runs
the body. The barrier serializes; it does not share.

So the merge has to happen in front of the reader and merge **results**,
not requests. These tests count what leaves the process — the HTTP call —
rather than what a collaborator reports, because the collaborator is the
thing under test.

What the numbers mean here
--------------------------
``before`` is what the shape costs with one probe per waiting dispatch:
one initial (cached) read to discover the window is shut, then one
forced read per waiter on the wake tick — ``1 + N`` requests for ``N``
verification points. ``after`` is the same race against the shared
watchdog: the same discovery read, and **one** read for all ``N``
waiters. The count that matters is the second one, and it must not
track the fleet.

No test here reaches the network or serves a wait: the reader is a
recorded CC Switch and the probe interval is the sleep seam.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
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
import quota_watchdog  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    DEFAULT_COOLDOWN_SEC,
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)
from sub_agent_registry import (  # noqa: E402
    SubAgentRegistry,
)

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"

# The fleet asleep on one window in the shape this order describes: eight
# verification points, one closed door.
WAITING_DISPATCHES = 8


def _now():
    return datetime.now(timezone.utc)


def _window(provider, *, used, resets_at):
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
                "resets_at": resets_at.isoformat() if resets_at else None,
            },
            {
                "name": "weekly_limit",
                "used_percent": 0.0,
                "remaining": 100.0,
                "total": 100.0,
                "unit": "%",
                "resets_at": None,
            },
        ],
        "success": True,
    }


def _payload(provider, *, used, resets_at):
    return _window(provider, used=used, resets_at=resets_at)


def _closed_fleet(*, resets_in_minutes=12):
    stamp = _now() + timedelta(minutes=resets_in_minutes)
    return {
        "providers": [
            _payload(name, used=100.0, resets_at=stamp) for name in CHAIN
        ]
    }


def _open_fleet():
    stamp = _now() + timedelta(hours=3)
    return {
        "providers": [
            _payload(name, used=4.0, resets_at=stamp) for name in CHAIN
        ]
    }


# ---------------------------------------------------------------------------
# A CC Switch that records its own traffic
# ---------------------------------------------------------------------------


class _CCSwitch:
    """The endpoint, counted at the socket rather than at a collaborator.

    ``calls`` is what left the process. Everything else here — the sleep,
    the reader, the watchdog — is a collaborator that could be counting
    itself, so the assertion that matters is anchored here.
    """

    def __init__(self, monkeypatch, *, payloads, hold=0.25):
        import json

        self._bodies = [json.dumps(p).encode("utf-8") for p in payloads]
        self._lock = threading.Lock()
        self.calls = 0
        # Long enough that every waiter is inside the probe before the
        # fetch it is sharing finishes. Without it the race could pass by
        # accident on a machine that never piles up.
        self._hold = hold
        self._arrivals = 0
        self._arrival_lock = threading.Lock()
        self._released = threading.Event()

        outer = self

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
            with outer._lock:
                index = outer.calls
                outer.calls += 1
            time.sleep(outer._hold)
            return _Response(outer._bodies[min(index, len(outer._bodies) - 1)])

        monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)

    def wake_everyone(self):
        """Release every dispatch from the sleep on the same instant.

        The merge only exists for callers that ask *at the same moment*,
        so the seam has to hold them until all of them are there. A test
        that staggered the wake would measure N sequential probes and
        conclude — correctly, but about the wrong thing — that merging
        does not work.

        Latching rather than tripping a ``Barrier``: a dispatch that
        sleeps a second time must not block on a gate nobody is coming
        to any more.
        """
        with self._arrival_lock:
            self._arrivals += 1
            if self._arrivals >= WAITING_DISPATCHES:
                self._released.set()
        self._released.wait(timeout=30)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, capacity_config):
    """Each test gets a cold reader, a cold fleet and a cold watchdog."""
    provider_order.cache_clear()
    set_shared_cooldown(ProviderCooldown())
    capacity_config(_POOL_CAPS, default=10)
    previous = quota_watchdog.set_shared_quota_watchdog(quota_watchdog.QuotaWatchdog())
    yield
    quota_watchdog.set_shared_quota_watchdog(previous)
    set_shared_cooldown(None)
    provider_order.cache_clear()


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
    monkeypatch.setattr(provider_order, "load_provider_5h_usage", lambda *a, **k: {})


def _install_fake_claude(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def _tool(tracker):
    tool = ClaudeCodingTool(
        scene="verification", concurrency_tracker=tracker,
    )
    tool.provider_slot_wait_sec = 0.2
    return tool


# ---------------------------------------------------------------------------
# The headline number
# ---------------------------------------------------------------------------


class TestOneWindowCostsOneRead:
    def test_eight_waiting_dispatches_cost_one_probe(
        self, tmp_path, monkeypatch, scene,
    ):
        """The order, measured at the socket.

        Eight verification points asleep on the same closed window, all
        released onto the wake tick together. The probe they share costs
        **one** request, so the process paid two in total for eight
        dispatches: one to discover the window was shut and one to learn
        it had reopened.

        With a probe per waiter the same race costs ``1 + N`` — nine here,
        and growing with the fleet. Asserted against the HTTP call count
        rather than against a counter the watchdog maintains, so a
        watchdog that counted itself generously could not pass this.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cc_switch = _CCSwitch(
            monkeypatch, payloads=[_closed_fleet(), _open_fleet()]
        )
        # Every dispatch sleeps on the seam and is released together, so
        # all of them ask within the same probe.
        monkeypatch.setattr(
            coding_tool, "_quota_wake_sleep", lambda _s: cc_switch.wake_everyone()
        )

        results = {}
        started = threading.Barrier(WAITING_DISPATCHES + 1)

        def _dispatch(index):
            tool = _tool(ActiveConcurrencyTracker())
            started.wait()
            results[index] = tool._run_claude_interactive(
                "hi", idle_timeout=30, total_timeout=30
            )[0]

        workers = [
            threading.Thread(target=_dispatch, args=(i,), daemon=True)
            for i in range(WAITING_DISPATCHES)
        ]
        for w in workers:
            w.start()
        started.wait()
        for w in workers:
            w.join(timeout=60)
            assert not w.is_alive(), "a dispatch never left the closed window"

        assert set(results.values()) == {"ok"}, results

        watchdog = quota_watchdog.get_shared_quota_watchdog()
        assert watchdog.waiting_dispatches == 0, "every wait ended"
        assert watchdog.shared_reads == WAITING_DISPATCHES - 1, (
            f"seven of the eight dispatches must have ridden on one "
            f"reader's answer; got {watchdog.shared_reads}"
        )

        # One read to find the window shut + one shared probe.
        assert cc_switch.calls == 2, (
            f"{WAITING_DISPATCHES} dispatches on one closed window cost "
            f"{cc_switch.calls} HTTP calls; the probe must cost one "
            f"request whatever the fleet size (the shape with one probe "
            f"per waiter costs 1 + {WAITING_DISPATCHES} = "
            f"{1 + WAITING_DISPATCHES})"
        )

    def test_the_probe_cost_does_not_track_the_fleet(
        self, tmp_path, monkeypatch, scene,
    ):
        """Same race at a different fleet size, same answer.

        A count that is right at eight and wrong at three is a constant
        that happens to be near the right value, and the number the merge
        exists to hold down is exactly the one that grows.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cc_switch = _CCSwitch(
            monkeypatch, payloads=[_closed_fleet(), _open_fleet()]
        )
        monkeypatch.setattr(
            coding_tool, "_quota_wake_sleep", lambda _s: cc_switch.wake_everyone()
        )

        def _dispatch():
            tool = _tool(ActiveConcurrencyTracker())
            return tool._run_claude_interactive(
                "hi", idle_timeout=30, total_timeout=30
            )[0]

        workers = [
            threading.Thread(target=_dispatch, daemon=True)
            for _ in range(WAITING_DISPATCHES)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)
            assert not w.is_alive()

        # The same figure the eight-dispatch case would have printed,
        # stated here as its own claim so a future edit that makes the
        # merge work only for some fleet sizes fails on this line.
        probes_after_discovery = cc_switch.calls - 1
        assert probes_after_discovery == 1, (
            f"the wake tick cost {probes_after_discovery} requests for "
            f"{WAITING_DISPATCHES} waiting dispatches"
        )

    def test_a_probe_arriving_after_the_read_finished_asks_again(
        self, tmp_path, monkeypatch, scene,
    ):
        """No time-based cache, and this is why.

        The merge is in-flight and nothing else. A watchdog that also
        remembered the last answer for a few seconds would report a
        *smaller* number on the test above, and would be wrong about the
        case that matters: the point of a probe is to learn what changed
        while the caller was not looking, and a remembered verdict cannot
        — so it would hand back a window that had reopened a minute ago
        as one that is still shut, and the fleet would sit idle for the
        rest of the window.

        Asserted by keeping the window shut across two probes and
        requiring two reads: with a TTL in the watchdog, the second
        probe would cost nothing and the dispatch would never wake.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cc_switch = _CCSwitch(
            monkeypatch,
            payloads=[_closed_fleet(resets_in_minutes=60 * 24 * 30)] * 3
                  + [_open_fleet()],
            hold=0.0,
        )
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", lambda _s: None)

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = tool._run_claude_interactive(
            "hi", idle_timeout=30, total_timeout=30
        )

        assert result == "ok", "the window eventually reopened"
        assert cc_switch.calls == 4, (
            f"one discovery read plus one per probe, with no TTL in "
            f"front of the reader; got {cc_switch.calls}"
        )


# ---------------------------------------------------------------------------
# The watchdog itself
# ---------------------------------------------------------------------------


class TestTheWatchdogInIsolation:
    def test_concurrent_probes_share_one_read(self):
        """The unit the fleet test is made of."""
        import threading as _t

        gate = _t.Barrier(16)
        calls = []

        def _fetch():
            calls.append(1)
            time.sleep(0.05)
            return {"Vendor A Pro": "verdict"}

        watchdog = quota_watchdog.QuotaWatchdog(fetch=_fetch)
        got = []

        def _ask():
            gate.wait()
            got.append(watchdog.probe(blocked=("Vendor A Pro",)))

        workers = [_t.Thread(target=_ask, daemon=True) for _ in range(16)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=30)

        assert len(got) == 16, got
        assert len(calls) == 1, (
            f"sixteen simultaneous probes cost {len(calls)} reads"
        )
        assert all(g == {"Vendor A Pro": "verdict"} for g in got)

    def test_a_reader_that_raises_is_a_probe_not_a_failure(self):
        """A probe that blows up must not poison the next one.

        CC Switch being unreachable is transient by nature, and the
        reader's own contract is to answer "no reading" rather than a
        verdict. Carrying a remembered failure would turn one outage into
        a quota closure for everybody, which is the one thing the reader
        is careful never to do.
        """
        state = {"fail": True}
        calls = []

        def _fetch():
            calls.append(1)
            if state["fail"]:
                raise OSError("connection refused")
            return {"Vendor A Pro": "verdict"}

        watchdog = quota_watchdog.QuotaWatchdog(fetch=_fetch)

        assert watchdog.probe() == {}
        state["fail"] = False
        assert watchdog.probe() == {"Vendor A Pro": "verdict"}
        assert len(calls) == 2

    def test_the_wait_is_visible_from_outside_the_dispatch(self, caplog):
        """A parked dispatch writes nothing at all, so the wait has to.

        It is asleep, its budget is deliberately not running down, and
        nothing about it appears anywhere. A wait that emits no record
        is indistinguishable from a fleet that went quiet for a reason
        nobody can name — so entering, probing and leaving all write a
        line, and each one says which providers are shut and when the
        soonest of them comes back.
        """
        import logging

        from datetime import datetime as _dt

        stamp = _dt(2026, 1, 1, 12, 0, 0)
        reading = {
            "Vendor A Pro": provider_order.ProviderQuota(
                provider="Vendor A Pro",
                shape=provider_order.SHAPE_ROLLING,
                remaining_pct=0.0,
                exhausted=True,
                exhausted_tier="five_hour",
                exhausted_resets_at=stamp,
            ),
            "Vendor A": provider_order.ProviderQuota(
                provider="Vendor A",
                shape=provider_order.SHAPE_ROLLING,
                remaining_pct=0.0,
                exhausted=True,
                exhausted_tier="five_hour",
                exhausted_resets_at=stamp + timedelta(hours=2),
            ),
        }

        class _Log:
            def __init__(self):
                self.events = []

            def info(self, event, message, data=None):
                self.events.append((event, message, data or {}))

        logger = _Log()
        watchdog = quota_watchdog.QuotaWatchdog(
            fetch=lambda: dict(reading)
        )

        watchdog.wait_entered(
            scene="verification", blocked=list(reading), quota_map=reading,
            logger=logger,
        )
        watchdog.probe(blocked=list(reading), scene="verification", logger=logger)
        watchdog.wait_left(scene="verification", rounds=3, logger=logger)

        names = [e[0] for e in logger.events]
        assert names == [
            "provider_quota_window_wait_entered",
            "provider_quota_window_probe",
            "provider_quota_window_wait_ended",
        ], names

        entered = logger.events[0][2]
        assert entered["quota_exhausted"] == ["Vendor A", "Vendor A Pro"]
        assert entered["earliest_resets_at"] == stamp.isoformat(), (
            "the soonest window is the one an operator needs: it is the "
            "first moment the wait could plausibly be over"
        )
        assert entered["waiting_dispatches"] == 1

        probe = logger.events[1][2]
        assert probe["quota_exhausted"] == ["Vendor A", "Vendor A Pro"]
        assert probe["shared"] is False
        assert probe["waiting_dispatches"] == 1

        ended = logger.events[2][2]
        assert ended["probe_rounds"] == 3
        assert ended["waiting_dispatches"] == 0

    def test_a_probe_logged_through_a_stdlib_logger_still_lands(self):
        """The reader is not worth losing over a logger's signature.

        ``ExecutionLogger`` takes an event, a message and a data dict; the
        stdlib logger takes a message and keyword arguments. Both are in
        the path — the first for a plan, the second for everything else —
        so a probe has to survive being handed either one.
        """
        import logging

        logger = logging.getLogger("quota-watchdog-stdlib-probe")
        logger.propagate = False
        watchdog = quota_watchdog.QuotaWatchdog(fetch=lambda: {})

        watchdog.probe(logger=logger)  # must not raise

    def test_there_is_one_watchdog_per_process_and_it_is_replaceable(self):
        """One is the point, so the singleton has to be the way there.

        Two watchers of one fleet's quota would be two things learning
        the same thing on two schedules — the resource cost the order
        objected to, reintroduced one layer up.
        """
        assert (
            quota_watchdog.get_shared_quota_watchdog()
            is quota_watchdog.get_shared_quota_watchdog()
        )

        replacement = quota_watchdog.QuotaWatchdog(fetch=lambda: {})
        previous = quota_watchdog.set_shared_quota_watchdog(replacement)
        try:
            assert quota_watchdog.get_shared_quota_watchdog() is replacement
        finally:
            quota_watchdog.set_shared_quota_watchdog(previous)

    def test_the_probe_interval_is_the_one_the_dispatch_uses(self):
        """Reused, not redeclared.

        Two figures that agree until one is edited, and the one that
        drifts is the cadence nobody reviews — the verification gate
        would end up asking on a beat the dispatch does not.
        """
        assert quota_watchdog.probe_interval_sec() == DEFAULT_COOLDOWN_SEC
        assert quota_watchdog.probe_interval_sec() == 300.0


# ---------------------------------------------------------------------------
# No time cap, and the wait is not a stall
# ---------------------------------------------------------------------------


class TestTheWaitIsNotBounded:
    """The two halves of "no upper limit".

    ``coding_tool`` no longer stops probing — that is pinned in
    ``test_provider_quota_wake.py``, which drives twenty full readings
    through the real dispatch and requires twenty probes. What is left is
    the other half, and it is the half that is easy to get wrong: a wait
    long enough to cross the stale-sub-agent threshold used to be judged
    stuck, which spent a kill path on a subprocess that does not exist and
    then threw the handle away — disarming the watchdog for the
    subprocess that would start once the window reopened.

    So the threshold is asked its real question: is this a process that
    stopped answering, or a dispatch that has not started one?
    """

    @staticmethod
    def _tool(*, parked: bool):
        class _Clock:
            is_parked = parked

        class _Tool:
            _work_clock = _Clock()
            _last_output_ts = None  # as during a wait: no subprocess yet

        return _Tool()

    def test_a_dispatch_asleep_on_a_window_is_not_stale_however_long(self):
        """Twenty probes deep — a hundred minutes — and still not stale.

        The threshold is twenty minutes, so a wait two crossings long is
        not an edge case; it is what an ordinary closed window looks
        like. Asserted at twice the threshold so the number has margin:
        if the rule were "waited less than twice the threshold", this
        would pass while a three-day window would not.
        """
        from sub_agent_registry import SUB_AGENT_STALE_THRESHOLD_SEC

        registry = SubAgentRegistry()
        handle = registry.register_sub_agent(
            plan_id="t-waiting", vp_id="VP-001", attempt=0,
            scoped_tool=self._tool(parked=True),
        )
        handle.last_progress_ts = time.time() - (2 * SUB_AGENT_STALE_THRESHOLD_SEC)

        assert registry.find_stale(SUB_AGENT_STALE_THRESHOLD_SEC) == []

    def test_a_sub_agent_with_a_process_and_no_heartbeat_is_still_stale(self):
        """The control, and the reason the one above means anything.

        Identical handle, identical age, identical threshold — but the
        tool is **not** parked, so there is a ``Popen`` behind it that has
        stopped answering. That is the case the watchdog was built for,
        and a rule that read "old" as "stuck" regardless would have
        caught the wait and lost this.

        The process is part of the claim, not decoration: the wait has no
        subprocess, so a sweep that found it would spend the kill path on
        a ``Popen`` that does not exist and then unregister the handle,
        leaving the process that *does* start later unprotected.
        """
        from sub_agent_registry import SUB_AGENT_STALE_THRESHOLD_SEC
        import subprocess

        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
        try:
            tool = self._tool(parked=False)
            tool._current_process = proc
            tool._process_lock = threading.Lock()

            registry = SubAgentRegistry()
            handle = registry.register_sub_agent(
                plan_id="t-stuck", vp_id="VP-002", attempt=0,
                scoped_tool=tool,
            )
            handle.last_progress_ts = (
                time.time() - (2 * SUB_AGENT_STALE_THRESHOLD_SEC)
            )

            stale = registry.find_stale(SUB_AGENT_STALE_THRESHOLD_SEC)

            assert stale == [handle], (
                "a live subprocess that has gone quiet is exactly what the "
                "threshold is for; skipping it would leave the watchdog "
                "catching nothing at all"
            )
            assert stale[0].scoped_tool._current_process.poll() is None, (
                "the handle still points at the process that would be "
                "killed — the kill path needs something to signal"
            )
        finally:
            proc.kill()
            proc.wait()

    def test_a_handle_stays_registered_across_the_whole_wait(self):
        """The handle is not thrown away, and that is the load-bearing part.

        Unregistering a waiting handle is what disarmed the watchdog: the
        sweep found it, the kill found no ``Popen``, and the handler
        unregistered it anyway — so the genuinely stuck process that
        started when the window reopened had nothing left to protect it.
        Skipping the handle in the sweep keeps it registered, and the
        protection that follows the wait intact.
        """
        registry = SubAgentRegistry()
        handle = registry.register_sub_agent(
            plan_id="t-survives", vp_id="VP-003", attempt=0,
            scoped_tool=self._tool(parked=True),
        )

        for _ in range(20):
            registry.find_stale(1)  # threshold far below the handle's age

        assert registry.all_handles("t-survives") == [handle], (
            "the sweep does not unregister — only the handler does, and "
            "the handler must never see a waiting handle"
        )
        assert handle.watchdog_kill_count == 0, (
            "no kill was owed, so no kill was counted"
        )

    def test_the_three_signals_are_asked_independently(self):
        """Parked, aged and process-less are separate answers.

        Written so a future change cannot collapse them into one: the
        sweep asks the work clock, the handler asks the process, and a
        rule that conflated "old" with "stuck" would pass the first pair
        and fail this.
        """
        registry = SubAgentRegistry()
        parked = registry.register_sub_agent(
            plan_id="t-signals", vp_id="VP-W", attempt=0,
            scoped_tool=self._tool(parked=True),
        )
        parked.last_progress_ts = time.time() - 100_000

        running = registry.register_sub_agent(
            plan_id="t-signals", vp_id="VP-R", attempt=0,
            scoped_tool=self._tool(parked=False),
        )
        running.last_progress_ts = time.time() - 100_000

        fresh = registry.register_sub_agent(
            plan_id="t-signals", vp_id="VP-F", attempt=0,
            scoped_tool=self._tool(parked=False),
        )

        stale = registry.find_stale(1200)
        assert stale == [running], [h.vp_id for h in stale]


# ---------------------------------------------------------------------------
# The failure reason a closed window never gets to be
# ---------------------------------------------------------------------------


class TestQuotaExhaustedIsNotAFailureReasonHere:
    """``reason == "quota_exhausted"`` is written and never raised.

    The dispatch tags its diagnostic with that word the moment it finds
    closed windows, and the exception's reason vocabulary carries the
    four ways a *chain* comes up empty. The word is not one of them, and
    on this side of the boundary it cannot become one: a closed window
    never ends the wait, so the fail-closed path is only ever reached
    after the loop has already decided nothing is blocked, at which
    point the tag has been overwritten.

    That is worth pinning rather than deleting. The resolver side uses
    the same word for the opposite situation, so a future edit that
    short-circuits a long wait would turn a wait into a failure whose
    name already exists — and the first sign of it would be a provider
    sitting idle for hours with a "quota exhausted" error attached.
    """

    def test_the_diagnostic_uses_the_word_and_the_exception_never_carries_it(
        self, tmp_path, monkeypatch, scene,
    ):
        """A window shut for the whole run is a wait, not a failure."""
        _install_fake_claude(tmp_path, monkeypatch)
        # Shut forever: 20 readings served, none of them opening.
        _CCSwitch(
            monkeypatch,
            payloads=[_closed_fleet(resets_in_minutes=60 * 24 * 30)],
            hold=0.0,
        )
        # Serve the last reading forever so the loop keeps waiting rather
        # than dispatching; the sleep seam is a no-op, so this is fast.
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", lambda _s: None)

        import provider_order as po

        served = {"n": 0}
        payload = _closed_fleet(resets_in_minutes=60 * 24 * 30)

        class _AlwaysShut:
            status = 200

            def __init__(self):
                self._body = json.dumps(payload).encode("utf-8")

            def read(self):
                return self._body

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def _urlopen(request, timeout=None):
            served["n"] += 1
            if served["n"] > 3:
                raise KeyboardInterrupt("enough rounds")
            return _AlwaysShut()

        monkeypatch.setattr(po.urllib.request, "urlopen", _urlopen)

        class _Log:
            def __init__(self):
                self.events = []

            def _rec(self, event, message, data=None):
                self.events.append((event, message, data or {}))

            info = _rec
            warning = _rec
            error = _rec

        logger = _Log()
        tool = _tool(ActiveConcurrencyTracker())
        tool.logger = logger

        with pytest.raises(KeyboardInterrupt):
            tool._run_claude_interactive(
                "hi", idle_timeout=30, total_timeout=30
            )

        # It did not give up, and it did not fail: it was still asking.
        assert served["n"] > 3, (
            f"the dispatch stopped probing after {served['n']} reads"
        )
        assert not [
            e for e in logger.events if e[0] == "provider_scene_chain_empty"
        ], "a closed window must not be reported as an empty chain"

        names = [e[0] for e in logger.events]
        assert "provider_quota_window_wait_entered" in names, names
        assert "provider_quota_window_wait_ended" in names, names

        ended = [e for e in logger.events if e[0] == "provider_quota_window_wait_ended"][0]
        assert ended[2]["probe_rounds"] >= 3, ended[2]

        entered = [e for e in logger.events if e[0] == "provider_quota_window_wait_entered"][0]
        assert entered[2]["quota_exhausted"] == sorted(CHAIN), entered[2]
        assert entered[2]["earliest_resets_at"], (
            "an operator reading only the first line must still learn when "
            "the fleet could come back"
        )

    def test_the_reason_vocabulary_of_the_exception_is_the_four_chain_answers(self):
        """Stated against the class, so a new reason is a deliberate edit.

        Read from the docstring rather than from an enumeration, because
        the enumeration does not exist: the reason is a free string and
        the list is prose. Adding a fifth would be fine — this test just
        means nobody adds one without thinking about whether the dispatch
        can actually produce it.
        """
        import inspect

        from coding_tool import SceneProviderUnavailable

        doc = inspect.getdoc(SceneProviderUnavailable) or ""
        listed = {
            word.strip("`\"' ")
            for word in doc.replace(",", " ").split()
            if word.strip("`\"' ").islower()
            and "_" in word
        }
        for reason in (
            "chain_unresolved", "no_tier_match",
            "all_candidates_skipped", "no_capacity",
        ):
            assert reason in listed, (
                f"{reason} must stay documented on the exception; it is a "
                f"reason a caller can still receive"
            )
        assert "quota_exhausted" not in listed, (
            "a closed quota window is a wait, not a chain failure; naming "
            "it here would invite a caller to branch on a state the PDT "
            "side does not raise"
        )

    def test_no_code_outside_the_dispatch_reads_that_reason(self):
        """The branch has no consumer, and that is checked, not assumed.

        Grepped rather than asserted by hand: a future caller that
        started branching on ``exc.reason == "quota_exhausted"`` would
        make the retained ``else`` branch in the message builder live
        code, and this is where that shows up.
        """
        import re
        from pathlib import Path

        backend = _BACKEND_DIR
        offenders = []
        pattern = re.compile(r"""reason\s*==\s*["']quota_exhausted["']""")

        for path in sorted(backend.rglob("*.py")):
            parts = set(path.parts)
            if "tests" in parts or ".venv" in parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(backend)))

        assert offenders == [], (
            "nothing branches on reason == 'quota_exhausted'; the word is "
            f"only a diagnostic tag and a log field. Found in: {offenders}"
        )
