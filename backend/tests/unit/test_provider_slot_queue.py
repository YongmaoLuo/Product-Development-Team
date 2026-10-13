"""A saturated local pool must be QUEUED for, not failed on.

What this file pins
-------------------
A scene's tier is a pool, and a pool has three ways of being unable to
serve a call. Two of them are somebody else's business — a quota window
that is shut, a park this process wrote itself — and the third is
entirely local: every candidate is reachable, inside its window, and at
its local concurrency cap.

The third one used to end the call. The reasoning was that a held slot
comes back in seconds, the poller had already spent the whole configured
window on it, and there was nothing further for a wake to add. That
reasoning confused a lease with a deadline. A quota window is somebody
else's meter on their clock; a park is a timestamp this process wrote. A
slot is neither — it is a counter in this process, held by an agent that
is running right now and handed back when that agent finishes. So it has
a timeline of its own, measured in the length of a task, and waiting on
it is waiting for something that is genuinely on its way.

So the saturated pool is queued for, on its own bounded-and-growing step,
with the caller's budget parked across the wait like the other two. This
file pins that behaviour and — just as importantly — the cases it must
NOT swallow: a tier that names nothing, a tier the caller excluded, a
tier that is parked (which belongs to the *park* wait, on a different
clock and a different log line), and a neighbouring tier with room in it.

No test here waits for real: the sleep is recorded rather than served, and
a virtual clock is driven by hand so a queue longer than the whole budget
costs a millisecond.
"""

from __future__ import annotations

import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_routing  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    SceneProviderUnavailable,
    WorkClock,
)
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    get_shared_cooldown,
    set_shared_cooldown,
)

#: Three rows in the scene's own tier, and one row in the tier next to
#: it. The neighbouring row is reachable and idle in every test here: it
#: is the thing that must never be used, and a test that proved it by
#: removing the alternative would prove nothing.
TIER = ["Vendor A Pro", "Vendor A", "Vendor B"]
NEIGHBOUR = "Vendor D API"
ALL_ROWS = TIER + [NEIGHBOUR]

#: Every pool holds ten, so "fill it" is ``range(10)`` throughout.
CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor b$", 10),
    ("^vendor d", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"


# ---------------------------------------------------------------------------
# Fixtures — a scene on ``medium`` with a populated ``high`` beside it
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    """Every test gets its own park list; the module default is shared."""
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _capacity(capacity_config):
    capacity_config(CAPS, default=10)


@pytest.fixture
def tracker():
    """The shared tracker, so a test can fill a pool before dispatching."""
    return ActiveConcurrencyTracker()


def _install_fake_claude(tmp_path, monkeypatch, script=None) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(script or OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""),
    )


@pytest.fixture
def routing(tmp_path, monkeypatch):
    """Point the scene table at a three-row ``medium`` and a ``high``."""
    cfg = tmp_path / "provider_routing.yaml"
    cfg.write_text(textwrap.dedent("""
        version: 1
        default_tier: medium
        tiers:
          medium: ["^Vendor A", "^Vendor B"]
          high:   ["^Vendor D"]
        scenes:
          execution: medium
    """), encoding="utf-8")

    monkeypatch.setattr(
        provider_routing, "_config_path", lambda config_path=None: cfg,
    )
    monkeypatch.setattr(
        provider_routing, "_list_display_names",
        lambda db_path=None: list(ALL_ROWS),
    )
    monkeypatch.setattr(provider_routing, "_optimizer_order", lambda: [])
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": f"https://{name}.test/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m",
                       "sonnet": "m", "haiku": "m"},
        })),
    )
    provider_routing.clear_routing_cache()
    yield cfg
    provider_routing.clear_routing_cache()


class _VirtualClock:
    """A monotonic the test drives, so a queue can outlive any budget."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Log:
    """Captures the dispatch's structured events by name.

    Three waits now live under the same scene, and the *name* of the
    event is the only thing from outside the process that tells them
    apart — so it is captured rather than inferred from a step size.
    """

    def __init__(self):
        self.events: list = []

    def info(self, event, message="", data=None):
        self.events.append((event, "info", message, data or {}))

    def warning(self, event, message="", data=None):
        self.events.append((event, "warning", message, data or {}))

    error = debug = warning

    def exception(self, event, message="", data=None):
        self.events.append((event, "error", message, data or {}))

    def names(self):
        return {name for name, _lvl, _msg, _data in self.events}

    def payload(self, event):
        return [d for n, _l, _m, d in self.events if n == event]


def _tool(tracker, log=None, budget=None):
    tool = ClaudeCodingTool(scene="execution", concurrency_tracker=tracker)
    # One attempt, one look at the tracker. The queue's own steps are
    # what this file is about; letting each attempt poll as well would
    # make a three-round test cost seconds of real sleeping without
    # testing anything extra.
    tool.provider_slot_wait_sec = 0.0
    if log is not None:
        tool.logger = log
    if budget is not None:
        mono = _VirtualClock()
        tool._work_clock = WorkClock(budget, monotonic=mono)
        tool._mono = mono
    return tool


def _fill(tracker, providers=None):
    """Take every slot in ``providers`` (default: the whole tier)."""
    for name in (providers if providers is not None else TIER):
        for _ in range(10):
            assert tracker.try_acquire(name, 10), name


class _Queue:
    """The seam under test: what the dispatch queued for, and for how long.

    ``sleeps`` is the assertion target — the durations the dispatch asked
    for — and ``release_after`` says how many of those steps go by before
    the pool hands a slot back, which is the whole premise of the queue:
    the thing being waited on is on its way.
    """

    def __init__(self, monkeypatch, tracker, *, release_after=None,
                 provider=None, tool=None, advance=None):
        self.sleeps: list[float] = []
        self.seen: list[float] = []
        self.release_after = release_after
        self.provider = provider
        self.tool = tool
        # What a queue *asks* for and what it *holds* are two numbers, and
        # the bound is on the second. ``advance`` says how many of the
        # requested seconds the call really spends asleep on the clock the
        # bound reads — the whole step by default (a queue that waits what
        # it asked for), less than that for a step cut short by a wake, and
        # zero to hold the bound out of the test entirely.
        self.advance = advance if advance is not None else (lambda s: s)

        def _sleep(seconds):
            self.sleeps.append(seconds)
            if self.tool is not None:
                clock = getattr(self.tool, "_slot_wait_clock", None)
                if clock is not None:
                    clock.advance(self.advance(seconds))
            if self.tool is not None and self.tool._work_clock is not None:
                self.tool._mono.advance(seconds)
                self.seen.append(self.tool._work_clock.remaining())
            if (self.release_after is not None
                    and len(self.sleeps) >= self.release_after):
                tracker.release(self.provider or TIER[0])

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)

    def install(self, tool):
        self.tool = tool
        return self


def _run(tool, **kw):
    kwargs = dict(idle_timeout=30, total_timeout=30)
    kwargs.update(kw)
    return tool._run_claude_interactive("hi", **kwargs)


# ---------------------------------------------------------------------------
# The headline: a saturated pool is queued for and then serves
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestASaturatedPoolIsQueuedFor:
    def test_the_call_waits_for_a_slot_and_then_dispatches(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The whole contract in one test.

        All three of the scene's own rows are reachable, inside their
        window and at their cap; one of them hands a slot back three
        steps later. The call must not have failed, must have waited for
        it, and must have gone out on a row of its own tier.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=3, provider=TIER[2],
        )
        tool = _tool(tracker)
        queue.install(tool)

        result, _session, _meta = _run(tool)

        assert result == "ok", (
            "a saturated pool is not a failed call — the slot was on its "
            "way back and the call waited for it"
        )
        assert queue.sleeps, "no queue was entered at all"
        assert tool.current_call_provider in TIER, (
            f"the work went to {tool.current_call_provider!r}; the only "
            f"providers this scene approves are its own tier {TIER}"
        )
        assert tool.current_call_provider == TIER[2], (
            "the one pool that had a slot released should be the one that "
            "was taken — if a different row served this, the dispatch "
            "found capacity it was not entitled to find"
        )

    def test_the_queue_is_announced_by_its_own_event(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """A queue that emits no line is a fleet that went quiet.

        Nothing about a queued dispatch is visible from outside — it is
        asleep, it writes nothing and its budget is deliberately not
        running down — so the line is what distinguishes "waiting its
        turn" from "hung", and it carries the pools it is waiting on.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        log = _Log()
        queue = _Queue(
            monkeypatch, tracker, release_after=3, provider=TIER[0],
        )
        tool = _tool(tracker, log=log)
        queue.install(tool)

        _run(tool)

        queued = log.payload("provider_scene_slot_wait")
        assert len(queued) == 3, queued
        for data in queued:
            assert set(data["candidates"]) <= set(TIER), (
                f"the queue was announced for {data['candidates']}; this "
                f"scene approves {TIER}"
            )
        assert [d["slot_rounds"] for d in queued] == [1, 2, 3], queued
        assert [d["wait_sec"] for d in queued] == [
            coding_tool._slot_wake_step_sec(i) for i in range(3)
        ], queued

    def test_the_queue_reads_no_quota_payload(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The queue is a lease wait, so it costs no network.

        A slot comes back in seconds and a window reopens in hours, so a
        forced read of the usage payload per queue step would be buying a
        fact that cannot have changed at that rate. The closed-window
        wake re-reads on its own account, on the only clock that can see
        a reopen.
        """
        import provider_order

        _install_fake_claude(tmp_path, monkeypatch)
        reads: list = []
        monkeypatch.setattr(
            provider_order, "load_provider_quota_map",
            lambda *a, **k: reads.append(k.get("force")) or {},
        )
        monkeypatch.setattr(
            provider_order, "load_provider_5h_usage", lambda *a, **k: {},
        )
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=3, provider=TIER[2],
        )
        tool = _tool(tracker)
        queue.install(tool)

        _run(tool)

        assert reads in ([], [None], [False]), (
            f"the quota payload was read {reads} during the queue; the "
            f"dispatch's own single unforced selection read is all it may "
            f"cost"
        )
        assert len(queue.sleeps) == 3

    def test_the_step_is_bounded_below_and_above(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """No hot loop: every step costs at least the floor.

        A slot can be free the instant the check that said it was not
        runs, so a step of zero would re-ask the tracker as fast as the
        clock allows. The ceiling is the other end of the same argument:
        a thread asleep for half a minute cannot notice that the holder
        finished.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=8, provider=TIER[0],
        )
        tool = _tool(tracker)
        queue.install(tool)

        _run(tool)

        assert len(queue.sleeps) >= 8, queue.sleeps
        assert all(
            coding_tool.SLOT_WAKE_MIN_STEP_SEC <= s
            <= coding_tool.SLOT_WAKE_MAX_STEP_SEC
            for s in queue.sleeps
        ), f"queue steps out of bounds: {queue.sleeps}"

    def test_the_step_grows_so_a_stalled_pool_is_not_polled(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """And it grows, because a pool busy for eight rounds is not
        free next second.

        A flat step would ask a table that is obviously not moving once a
        second for as long as the fleet is busy. Asserted as a
        non-decreasing sequence that reaches the ceiling, because that is
        the whole claim.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=10, provider=TIER[1],
        )
        tool = _tool(tracker)
        queue.install(tool)

        _run(tool)

        steps = queue.sleeps
        assert steps == sorted(steps), f"the step shrank: {steps}"
        assert steps[0] == coding_tool.SLOT_WAKE_MIN_STEP_SEC, steps
        assert steps[-1] == coding_tool.SLOT_WAKE_MAX_STEP_SEC, steps
        assert len(set(steps)) > 1, (
            f"every step was identical ({steps}); the step is supposed to "
            f"back off so a stalled pool is not polled once a second"
        )

    def test_the_step_function_is_a_third_clock(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """It reads nothing, which is what makes it different from the
        other two.

        The park wait reads the cooldown's remaining time; the
        closed-window wait probes on a fixed beat and consults no
        timestamp. A slot has neither a deadline to read nor a stamp to
        count down to, so its step takes the length of the wait and
        nothing else — and takes no argument beyond the round number, so
        the signature itself rules out the other two's inputs.
        """
        import inspect

        assert list(
            inspect.signature(coding_tool._slot_wake_step_sec).parameters
        ) == ["round_index"]
        assert coding_tool._slot_wake_step_sec(0) == \
            coding_tool.SLOT_WAKE_MIN_STEP_SEC
        assert coding_tool._slot_wake_step_sec(10_000) == \
            coding_tool.SLOT_WAKE_MAX_STEP_SEC
        assert coding_tool._slot_wake_step_sec(-5) == \
            coding_tool.SLOT_WAKE_MIN_STEP_SEC, (
            "a negative round index must not produce a step under the "
            "floor — that is the spin"
        )
        assert coding_tool.SLOT_WAKE_MIN_STEP_SEC == 1.0
        assert coding_tool.SLOT_WAKE_MAX_STEP_SEC == 30.0


# ---------------------------------------------------------------------------
# Waiting is not working
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheQueueDoesNotChargeTheBudget:
    def test_a_queue_longer_than_the_whole_budget_still_dispatches(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """A queue longer than everything the budget has to give.

        One slot freed after steps totalling far more than the budget,
        against a budget smaller than any single one of them. It
        dispatches, because the wait was never charged.
        """
        budget = 3.0
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=6, provider=TIER[0],
        )
        tool = _tool(tracker, budget=budget)
        queue.install(tool)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        waited = sum(queue.sleeps)
        assert waited > budget, (
            f"the queue served must actually exceed the whole budget "
            f"({waited}s against {budget}s) or this proves nothing"
        )
        assert tool._work_clock.remaining() == budget, (
            "the queue must not have been charged to the budget at all"
        )

    def test_the_budget_reads_full_before_every_single_step(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Parked throughout, not reset at the end.

        A budget that was zeroed once when the queue finished would pass
        the test above and fail this one; reading the clock on every step
        is what tells the two apart.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=6, provider=TIER[0],
        )
        tool = _tool(tracker, budget=600.0)
        queue.install(tool)

        _run(tool)

        assert queue.seen == [600.0] * len(queue.sleeps), (
            f"the budget decayed across the queue: {queue.seen}"
        )

    def test_the_budget_runs_again_once_the_slot_is_taken(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Parked is not off.

        The dispatch after the queue is ordinary work and is charged as
        ordinary work, so a clock left parked would never catch a call
        that wedges after it got its slot.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=3, provider=TIER[0],
        )
        tool = _tool(tracker, budget=600.0)
        queue.install(tool)

        _run(tool)

        tool._mono.advance(120.0)
        assert tool._work_clock.remaining() == 480.0, (
            "the budget must be running again once the dispatch resumes"
        )

    def test_a_call_with_no_budget_attached_is_unaffected(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Most callers have no clock, so the attachment stays optional."""
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=2, provider=TIER[0],
        )
        tool = _tool(tracker)
        queue.install(tool)

        assert tool._work_clock is None
        result, _session, _meta = _run(tool)

        assert result == "ok"

    def test_a_selection_that_raises_leaves_the_budget_running(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The queue can end by blowing up, and the budget must notice.

        A budget left parked by a path nobody thought about is a budget
        that stops being read while the thing it bounds is still running
        — the exact shape of a call that never returns, which is what the
        budget exists to catch.
        """
        import dynamic_provider_concurrency as dpc

        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(monkeypatch, tracker)
        tool = _tool(tracker, budget=600.0)
        queue.install(tool)

        real_acquire = dpc.acquire_provider_with_dynamic_capacity
        calls: list = []

        def _explode(*args, **kwargs):
            calls.append(1)
            if len(calls) >= 3:
                raise RuntimeError("selection blew up")
            return real_acquire(*args, **kwargs)

        monkeypatch.setattr(
            dpc, "acquire_provider_with_dynamic_capacity", _explode,
        )

        with pytest.raises(RuntimeError):
            _run(tool)

        assert len(calls) >= 3, "the queue never got to a third decision"
        tool._mono.advance(120.0)
        assert tool._work_clock.remaining() == 480.0, (
            "the budget must be running again after the queue ends, "
            "however it ended"
        )


# ---------------------------------------------------------------------------
# The knob
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheAttemptWindowIsStillAWiredKnob:
    def test_the_queue_asks_for_its_own_steps_not_the_windows(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """``PDT_PROVIDER_SLOT_WAIT_SEC`` bounds the poller, not the cadence.

        It is the window a single attempt gives the tracker before the
        dispatch re-decides, and it is not what decides how long to sleep
        between queue rounds. The steps that were asked for are the queue's
        own clock, and a single one of them is longer than the window — so
        the cadence is not derived from it. (The bound the same figure also
        carries is a separate test below; this one is about step sizes, and
        the slot comes back on the round whose step crosses the window.)
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=3, provider=TIER[0],
            advance=lambda _s: 0.0,
        )
        tool = _tool(tracker)
        tool.provider_slot_wait_sec = 3.5
        # This test is about step sizes; the bound the same figure also
        # carries is a separate test below. A queue that holds nothing is
        # how to say so — the bound reads the clock, the clock stays at
        # zero, and the steps are left to speak for themselves.
        tool._slot_wait_clock = _VirtualClock()
        queue.install(tool)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert len(queue.sleeps) == 3, queue.sleeps
        assert queue.sleeps == [
            coding_tool._slot_wake_step_sec(i) for i in range(3)
        ], (
            f"the steps must come from the queue's clock, not from the "
            f"attempt window: {queue.sleeps}"
        )
        assert queue.sleeps[-1] > tool.provider_slot_wait_sec, (
            "a step longer than the window is the whole claim; if every step "
            "fitted inside it the window would be deciding the cadence"
        )

    def test_a_queue_past_the_window_hands_the_wait_back(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The window is also how long a queue may go on holding its caller.

        A slot wait that never ends is a caller that never comes back, and
        on the verification path the caller is holding a capacity gate while
        it sleeps. The same figure bounds both: the queue stops asking at the
        point where its own elapsed waiting has reached the window, and says
        so with a signal the verification layer turns into a re-queue.

        The give-up is not a failure — nothing was dispatched, nothing was
        refused — so it is a distinct type rather than a reused ``ApiError``,
        and it is raised rather than folded into the fail-closed chain.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(monkeypatch, tracker)
        tool = _tool(tracker)
        tool.provider_slot_wait_sec = 2.0
        # The bound runs on elapsed time, so the queue's clock is handed in
        # rather than left on the wall. Without this the seam below returns
        # without spending anything, the point never actually holds
        # anything, and no bound could ever fire — which is the right
        # behaviour and would make this test assert nothing.
        mono = _VirtualClock()
        tool._slot_wait_clock = mono
        queue.install(tool)

        with pytest.raises(coding_tool.ProviderSlotWaitExpired) as caught:
            _run(tool)

        assert caught.value.waited_sec >= caught.value.bound_sec, (
            f"gave up after {caught.value.waited_sec}s against a "
            f"{caught.value.bound_sec}s bound"
        )
        assert caught.value.requested_sec >= sum(queue.sleeps) - 1e-9, (
            f"asked to wait {caught.value.requested_sec}s but recorded "
            f"{sum(queue.sleeps)}s of steps"
        )
        assert caught.value.bound_sec == 2.0
        assert caught.value.scene == "execution"
        assert set(caught.value.candidates) <= set(TIER), caught.value.candidates
        assert not isinstance(caught.value, coding_tool.ApiError), (
            "a queue that gave up is not a provider refusing a request; "
            "an ApiError would send the fallback machinery down the priority "
            "chain and exclude the very tier that was merely busy"
        )

    def test_a_queue_that_asked_for_a_lot_but_slept_no_time_is_kept(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The bound is on time *held*, not on time *asked for*.

        This queue asks for far more than the bound — eight steps, a
        hundred-odd seconds of requested waiting — and holds nothing,
        because the sleep seam returns without spending anything. A bound
        measured on the requested total would hand this wait back on the
        second step; a bound measured on elapsed time does not, and rightly:
        the whole cost the bound exists to cap is a gate held by a point
        that is asleep, and this point is not holding one.

        It also keeps the two numbers honestly separate. The requested
        figure is still reported, as ``requested_sec``, because it is what
        explains the round count.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        held = 0.05
        queue = _Queue(
            monkeypatch, tracker, release_after=4, provider=TIER[0],
            advance=lambda _s: held,
        )
        tool = _tool(tracker)
        tool.provider_slot_wait_sec = 0.5
        tool._slot_wait_clock = _VirtualClock()
        queue.install(tool)

        result, _session, _meta = _run(tool)

        assert sum(queue.sleeps) > 10 * tool.provider_slot_wait_sec, (
            f"the queue only asked for {sum(queue.sleeps)}s; it has to ask "
            f"for many times the bound for this to prove anything"
        )
        assert 4 * held < tool.provider_slot_wait_sec, (
            f"the queue held {4 * held}s against a "
            f"{tool.provider_slot_wait_sec}s bound; the case is only "
            f"meaningful while the hold stays under it"
        )
        assert result == "ok", (
            "the queue was given up on for asking to wait, while it was "
            "actually holding less than the bound"
        )

    def test_an_unbounded_queue_is_still_unbounded(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """A window of zero turns the poller off, so it turns the bound off.

        Zero already means "do not spend a window waiting on the tracker"
        inside each attempt. Reading it as "also queue forever" keeps the one
        figure from saying two contradictory things, and keeps a test that
        wants a queue of any length expressible by setting it to zero.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=24, provider=TIER[0],
        )
        tool = _tool(tracker)
        tool.provider_slot_wait_sec = 0.0
        queue.install(tool)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert sum(queue.sleeps) > ClaudeCodingTool.PROVIDER_SLOT_WAIT_SEC, (
            f"the queue only waited {sum(queue.sleeps)}s; it has to outlast "
            f"the default bound for this to prove anything"
        )

    def test_the_bound_is_on_the_queue_and_not_on_the_work(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Reverse control at the real stack: a slow *dispatch* is not a queue.

        The bound is read inside the queue loop, and the loop is left behind
        the moment a provider is chosen. So a call that queues, gets admitted,
        and then spends longer than the bound actually running must come back
        normally: the holding is over, and a figure about waiting has nothing
        to say about a figure about running.

        The queue is real here and it *holds*: one step is waited out, on the
        clock the bound reads, and the slot is released. So the bound is live
        by the time the work starts, and moving the check one branch later —
        to cover the dispatch rather than the queue — turns this into a call
        that was killed while working. That is the case that decides where the
        bound goes.

        The other half of it is that an available slot is taken even though
        the hold already exceeds the bound. The bound exists to stop a gate
        being held by something asleep; a point that is about to work is not
        asleep, and refusing it the slot it was waiting for would trade a
        stalled gate for a stalled round.
        """
        _install_fake_claude(tmp_path, monkeypatch, script=(
            "#!/bin/sh\nsleep 1.2\n"
            "echo '{\"type\":\"result\",\"result\":\"ok\"}'\n"
        ))
        _fill(tracker)
        queue = _Queue(monkeypatch, tracker, release_after=1, provider=TIER[0])
        tool = _tool(tracker)
        tool.provider_slot_wait_sec = 0.5
        tool._slot_wait_clock = _VirtualClock()
        queue.install(tool)

        started = time.monotonic()
        result, _session, _meta = _run(tool)
        elapsed = time.monotonic() - started

        assert sum(queue.sleeps) > tool.provider_slot_wait_sec, (
            f"the call only queued for {sum(queue.sleeps)}s, under the "
            f"{tool.provider_slot_wait_sec}s bound; it has to outlast the "
            f"bound for the running to be the thing being measured"
        )
        assert elapsed > tool.provider_slot_wait_sec, (
            f"the working dispatch took {elapsed:.3f}s, which does not "
            f"outlast the {tool.provider_slot_wait_sec}s bound"
        )
        assert result == "ok", (
            "a call that waited and was then admitted was cut off once its "
            "running time outlasted the bound; the bound is about waiting "
            "for a slot, not about working"
        )
        assert tool.current_call_provider in TIER, tool.current_call_provider


# ---------------------------------------------------------------------------
# What the queue must NOT swallow
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWhatTheQueueMustNotSwallow:
    def test_a_tier_that_names_nothing_fails_immediately(
        self, tmp_path, monkeypatch, tracker,
    ):
        """Reverse control: an empty chain is a configuration fact.

        No pattern in the scene's tier matches any CC Switch name, so the
        chain is empty before any pool, window or slot is consulted.
        Nothing about that is a clock, so nothing waits.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text(textwrap.dedent("""
            version: 1
            default_tier: medium
            tiers:
              medium: ["^Nothing At All"]
            scenes:
              execution: medium
        """), encoding="utf-8")
        monkeypatch.setattr(
            provider_routing, "_config_path", lambda config_path=None: cfg,
        )
        monkeypatch.setattr(
            provider_routing, "_list_display_names",
            lambda db_path=None: list(ALL_ROWS),
        )
        provider_routing.clear_routing_cache()

        sleeps: list = []
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", sleeps.append)
        tool = _tool(tracker)

        with pytest.raises(SceneProviderUnavailable) as excinfo:
            _run(tool)

        assert excinfo.value.reason == "no_tier_match"
        assert sleeps == [], (
            "an empty chain is not on any clock; waiting on it would wait "
            "for a configuration change nothing here would make"
        )

    def test_a_fully_excluded_tier_fails_immediately(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Reverse control: an exclusion is an instruction, not a clock.

        The caller has said which providers this call may not use. A park
        expires, a window reopens and a slot comes back — an exclusion
        does none of those, so there is nothing to wait for and the answer
        stays the immediate one.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        sleeps: list = []
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", sleeps.append)
        tool = _tool(tracker)

        with pytest.raises(SceneProviderUnavailable) as excinfo:
            _run(tool, excluded_providers=list(TIER))

        assert excinfo.value.reason == "all_candidates_skipped"
        assert sleeps == [], (
            "an exclusion is not a deadline; the queue must not spin on it"
        )

    def test_an_exclusion_beside_a_saturated_pool_does_not_stop_the_queue(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The mixed case, and the one most likely to be got wrong.

        Two rows at their cap and one the caller excluded. The pool the
        call may use is full and the row it may not use is idle, and the
        right answer is still the queue for its own pool — not the
        excluded row, and not an immediate failure.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker, providers=[TIER[0], TIER[1]])
        assert tracker.current(TIER[2]) == 0, "the excluded row must be idle"
        queue = _Queue(
            monkeypatch, tracker, release_after=3, provider=TIER[1],
        )
        tool = _tool(tracker)
        queue.install(tool)

        result, _session, _meta = _run(tool, excluded_providers=[TIER[2]])

        assert result == "ok"
        assert tool.current_call_provider in (TIER[0], TIER[1])
        assert tracker.current(TIER[2]) == 0, "the excluded row served it"
        assert queue.sleeps, "the call's own pool was full and not waited for"

    def test_a_fully_parked_tier_still_waits_on_the_park(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Reverse control: a parked tier keeps the PARK wait.

        Three waits now live under the same scene, and this is the one
        that must not be confused with the queue: the pools are all
        absent because a refusal parked them, the step comes off the
        park's own remaining time, and the queue's own log line never
        appears.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cd = get_shared_cooldown()
        for name in TIER:
            cd.park(name, seconds=600, reason="429")

        log = _Log()
        sleeps: list = []

        def _sleep(seconds):
            sleeps.append(seconds)
            cd.clear()

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
        tool = _tool(tracker, log=log)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert sleeps, "a parked tier must still be waited for"
        assert all(
            s <= coding_tool.PARK_WAKE_MAX_STEP_SEC for s in sleeps
        ), (
            f"the park wait's step must come off the park, not off the "
            f"queue's clock: {sleeps}"
        )
        assert sleeps[0] == pytest.approx(30.0, abs=0.001), (
            f"a half-hour park must cost the park wake's ceiling, not the "
            f"queue's opening step: {sleeps}"
        )
        names = log.names()
        assert "provider_scene_park_wait" in names, (
            f"the park wait did not announce itself: {sorted(names)}"
        )
        assert "provider_scene_slot_wait" not in names, (
            "the queue was entered for a parked tier; the two waits are "
            "separate clocks and the park one is the right answer here"
        )

    def test_a_pool_scaled_to_zero_is_probed_for_and_not_queued_for(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """A shut window and a busy pool are both "nothing free".

        The queue must be able to tell them apart, because only one of
        them is on its way back. A cap of zero is a 5-hour window that
        reopens on the provider's own schedule; queueing on it would be
        the lease wait pointed at a clock it cannot outlast.

        **Contract change, not a relaxation.** This used to end in
        ``no_capacity`` with an empty ``sleeps``. A pool with no width at
        all is not a call that should never run — it is a window, and the
        window's own wait now serves it: the probe re-reads, and the work
        goes out the moment a reading finds the budget back. What the
        queue must never do is spin on it, and that half is unchanged and
        asserted from the log's event name rather than from a step size.
        """
        import provider_order

        _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setattr(
            provider_order, "load_provider_5h_usage",
            lambda *a, **k: {name: 0.0 for name in ALL_ROWS},
        )
        reads: list = []

        def _load(*_a, **_kw):
            """No reading first, so the zero budget above is what decides."""
            reads.append(True)
            if len(reads) == 1:
                return {}
            return {
                name: provider_order.ProviderQuota(
                    provider=name,
                    shape=provider_order.SHAPE_ROLLING,
                    remaining_pct=96.0,
                    exhausted=False,
                )
                for name in ALL_ROWS
            }

        monkeypatch.setattr(
            provider_order, "load_provider_quota_map", _load,
        )
        sleeps: list = []
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", sleeps.append)
        log = _Log()
        tool = _tool(tracker, log=log)

        result, _session, _meta = _run(tool)

        assert result == "ok", (
            "every pool is shut rather than busy; a shut window is a "
            "pause, and the pause has to end in the work"
        )
        assert tool.current_call_provider in TIER
        assert sleeps == [coding_tool._quota_wake_probe_sec()], (
            f"the wait must be the probe interval, and on the queue's "
            f"clock it would not be: {sleeps}"
        )
        assert "provider_scene_slot_wait" not in log.names(), (
            "every pool is shut rather than busy; the queue must not spin "
            "on a window measured in hours"
        )
        assert "provider_quota_window_wait" in log.names(), sorted(log.names())
        assert tracker.total() == 0

    def test_the_queue_ends_on_the_round_the_slot_comes_back(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """A freed slot ends the queue rather than being absorbed by it.

        The queue is only allowed to be unbounded because the thing it
        waits on is bounded in practice: an agent finishes. A loop that
        kept stepping after the slot was gone would be a wait with
        nothing left to wait for, and would look identical in the logs
        to one that is genuinely still queued.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        queue = _Queue(
            monkeypatch, tracker, release_after=2, provider=TIER[2],
        )
        tool = _tool(tracker)
        queue.install(tool)

        _run(tool)

        assert len(queue.sleeps) == 2, (
            f"the queue ran {len(queue.sleeps)} rounds past the release; "
            f"a freed slot ends it"
        )
        assert tool.current_call_provider == TIER[2]

    def test_a_full_pool_never_reaches_another_tier(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """The neighbouring tier is reachable, idle and correctly
        configured, and is never used.

        Asserted rather than left implicit, because "waited for the pool"
        is exactly the shape of a rule that quietly widens into "waited
        for the pool, then looked somewhere else".
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        assert tracker.current(NEIGHBOUR) == 0, (
            "the neighbouring tier's row must be idle for this to mean "
            "anything"
        )
        queue = _Queue(
            monkeypatch, tracker, release_after=4, provider=TIER[0],
        )
        tool = _tool(tracker)
        queue.install(tool)

        _run(tool)

        assert tool.current_call_provider != NEIGHBOUR, (
            "the queue crossed into a tier this scene was not put in"
        )
        assert tool.current_call_provider in TIER
        assert tracker.current(NEIGHBOUR) == 0, "the neighbour took nothing"
        assert queue.sleeps, "the queue was skipped entirely"

    def test_a_queue_that_outlives_the_neighbour_still_stays_in_tier(
        self, tmp_path, monkeypatch, routing, tracker,
    ):
        """Ten rounds of queueing, and the neighbouring tier is still
        never a candidate.

        The narrow version of the claim is that the work landed in the
        tier; this one is that the *waiting* stayed there too, read out
        of the queue's own announcements.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        _fill(tracker)
        log = _Log()
        queue = _Queue(
            monkeypatch, tracker, release_after=10, provider=TIER[0],
        )
        tool = _tool(tracker, log=log)
        queue.install(tool)

        _run(tool)

        queued = log.payload("provider_scene_slot_wait")
        assert len(queued) == 10, queued
        for data in queued:
            assert NEIGHBOUR not in data["candidates"], (
                f"the queue was announced for {data['candidates']}, which "
                f"includes a row from a tier this scene is not in"
            )
        assert tool.current_call_provider != NEIGHBOUR
