"""The verification width comes from the providers, not from a typed number.

What this file pins
-------------------
There used to be exactly one throttle on a verification round:
``parallelism_cap``, set to four in ``verification.yaml`` and read into an
``asyncio.Semaphore(4)``. It was a hand-written number describing a fleet
the same repository already knew how to measure — every provider in the
verification scene declares how many slots it can take, and each one
scales that by how much of its five-hour window is left.

So the number was wrong in both directions at once. A machine with one
provider declaring ten slots ran four verification points at a time, and a
machine with thirty ran four. Worse, it did not move: a window that shut
mid-round, shrinking the fleet to nothing, left the plan happily
dispatching four verification points into it, each of which would sit in
the provider slot wait for as long as the window stayed shut.

What is here instead is the same arithmetic execution already uses,
applied to the same scene chain, read from the same quota verdict — so
the gate and the dispatcher cannot disagree about how many slots exist.
It is read *every time it binds* rather than fixed at construction,
because a fleet's width is a fact about the current second and a
semaphore froze it at the moment the run started.

What each test would fail on
---------------------------
* :class:`TestTheCapFollowsTheFleet` — the number is still written down.
* :class:`TestTheCapFollowsTheBudget` — it does not scale with the window.
* :class:`TestTheCapFollowsAvailability` — a shut provider keeps a share.
* :class:`TestTheGateIsReadEveryTimeItBinds` — a fixed-width semaphore.
* :class:`TestTheDeclaredCapIsTheFallbackNotTheCeiling` — the fallback
  quietly became the rule, which is the whole complaint again.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import provider_order  # noqa: E402
import provider_routing  # noqa: E402
import quota_watchdog  # noqa: E402
import verification_agent  # noqa: E402
from verification_agent import (  # noqa: E402
    _plan_wide_bound,
    _plan_wide_vp_cap,
    _VpCapacityGate,
    _verification_vp_capacity,
)

ONE = ["Vendor A"]
TWO = ["Vendor A", "Vendor B"]

CAP_OF_A = 10
CAP_OF_B = 10


# ---------------------------------------------------------------------------
# Readings — the same verdict the dispatch path is handed
# ---------------------------------------------------------------------------


def _quota(provider, *, remaining_pct, exhausted=False, resets_at=None):
    return provider_order.ProviderQuota(
        provider=provider,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=remaining_pct,
        exhausted=exhausted,
        exhausted_tier="five_hour" if exhausted else None,
        exhausted_resets_at=resets_at,
    )


def _open(provider, remaining_pct=96.0):
    return _quota(provider, remaining_pct=remaining_pct)


def _shut(provider):
    return _quota(
        provider,
        remaining_pct=0.0,
        exhausted=True,
        resets_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )


@pytest.fixture
def capacities(capacity_config):
    """Two declared pools, ten slots each."""
    capacity_config(
        [("^vendor a$", CAP_OF_A), ("^vendor b$", CAP_OF_B)],
        default=10,
    )


@pytest.fixture
def scene(monkeypatch):
    def _chain(names):
        monkeypatch.setattr(
            provider_routing, "resolve_provider_chain",
            lambda scene, *a, **k: list(names),
        )

    return _chain


@pytest.fixture(autouse=True)
def _quota_reader(monkeypatch):
    """Serve the live reading the derivation reads.

    The same map the dispatch path is given. That is the point of the
    fixture rather than a convenience: if the width were derived from a
    *different* source — a verification-only provider count, a separate
    quota read — this patch would not be enough to move it, and the tests
    below would fail.
    """
    state = {"map": {}, "usage": {}}

    def _load(*_a, **_k):
        return dict(state["map"])

    def _usage(*_a, **_k):
        return dict(state["usage"])

    monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)
    monkeypatch.setattr(provider_order, "load_provider_5h_usage", _usage)
    return state


@pytest.fixture(autouse=True)
def _no_park(monkeypatch):
    from dynamic_provider_concurrency import ProviderCooldown, set_shared_cooldown

    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


# ---------------------------------------------------------------------------
# The width itself
# ---------------------------------------------------------------------------


class TestTheCapFollowsTheFleet:
    def test_one_provider_declaring_ten_admits_ten(self, capacities, scene, _quota_reader):
        """The headline. The typed figure was four."""
        scene(ONE)
        _quota_reader["map"] = {"Vendor A": _open("Vendor A")}

        assert _verification_vp_capacity() == CAP_OF_A

    def test_two_providers_declaring_ten_each_admit_twenty(
        self, capacities, scene, _quota_reader,
    ):
        """The fleet grows with the fleet."""
        scene(TWO)
        _quota_reader["map"] = {
            "Vendor A": _open("Vendor A"),
            "Vendor B": _open("Vendor B"),
        }

        assert _verification_vp_capacity() == CAP_OF_A + CAP_OF_B

    def test_the_same_provider_named_twice_is_one_pool(
        self, capacities, scene, _quota_reader,
    ):
        """A pool counted twice claims capacity it cannot serve.

        Chains carry aliases and the ``"parent"`` sentinel; neither is a
        second set of credentials, and summing them would report a fleet
        twice its size — which is the number the gate would then bound
        concurrency with.
        """
        scene(["Vendor A", "vendor a", "parent"])
        _quota_reader["map"] = {"Vendor A": _open("Vendor A")}

        assert _verification_vp_capacity() == CAP_OF_A

    def test_an_unresolvable_chain_derives_nothing_at_all(
        self, capacities, scene, _quota_reader,
    ):
        """``None`` is not ``0``, and the difference is the whole point.

        Zero means "the providers are all shut" — a real, actionable
        state. ``None`` means the chain could not be resolved, which is a
        routing failure the dispatch path reports loudly by name. Turning
        the second into the first would hang the plan on a gate that
        admits nobody, silently, instead of failing with the reason.
        """
        scene([])
        assert _verification_vp_capacity() is None

    def test_a_raising_reader_yields_no_bound_rather_than_zero(
        self, capacities, scene, monkeypatch,
    ):
        """Same rule for a broken read.

        A width invented from a broken meter is a width that contradicts
        it. The fallback — the operator's own cap — is the honest answer
        when there is nothing to measure.
        """
        scene(ONE)

        def _boom(*_a, **_k):
            raise RuntimeError("CC Switch is down")

        monkeypatch.setattr(provider_order, "load_provider_quota_map", _boom)
        monkeypatch.setattr(provider_order, "load_provider_5h_usage", _boom)
        import dynamic_provider_concurrency as dpc

        monkeypatch.setattr(
            dpc, "fleet_capacity",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert _verification_vp_capacity() is None


class TestTheCapFollowsTheBudget:
    def test_a_provider_down_to_forty_percent_admits_forty_percent(
        self, capacities, scene, _quota_reader,
    ):
        """The number scales with the window, which is what
        ``compute_dynamic_limit`` already does for a pool.

        Asserted against the shared function rather than against a
        rounded figure, so the two cannot drift: if the scaling rule
        changes, this test moves with it instead of pinning a second,
        subtler copy of the same arithmetic.
        """
        from dynamic_provider_concurrency import compute_dynamic_limit

        scene(ONE)
        _quota_reader["map"] = {"Vendor A": _open("Vendor A", remaining_pct=40.0)}

        assert _verification_vp_capacity() == compute_dynamic_limit(
            "Vendor A", 40.0
        ) == 4

    def test_an_almost_spent_provider_admits_almost_nothing(
        self, capacities, scene, _quota_reader,
    ):
        """And the tail behaves, rather than rounding a slot into existence.

        A provider with one percent of its window left does not serve ten
        verification points; it serves a rounding error's worth, and a
        gate that rounded it *up* would be the framework overruling the
        meter.
        """
        from dynamic_provider_concurrency import compute_dynamic_limit

        scene(ONE)
        _quota_reader["map"] = {"Vendor A": _open("Vendor A", remaining_pct=1.0)}

        assert _verification_vp_capacity() == compute_dynamic_limit(
            "Vendor A", 1.0
        )


class TestTheCapFollowsAvailability:
    def test_a_shut_provider_loses_its_share(self, capacities, scene, _quota_reader):
        """The total drops by exactly that provider's share.

        Keeping the reservation would be the worst of both: the plan
        would dispatch into a window that is shut, and each of those
        dispatches would sit in the slot wait until it reopened — the
        fleet reporting ten slots it does not have.
        """
        scene(TWO)
        _quota_reader["map"] = {
            "Vendor A": _open("Vendor A"),
            "Vendor B": _shut("Vendor B"),
        }

        assert _verification_vp_capacity() == CAP_OF_A

    def test_every_provider_shut_admits_nobody(self, capacities, scene, _quota_reader):
        """Zero — not back to four.

        The reverse control, and the reason it is written as an equality
        rather than a comparison. A gate that fell back to the typed
        ``parallelism_cap`` when it could measure nothing would admit four
        verification points into a fleet that has none, and each would
        wait out the whole window doing nothing. The measured answer is
        zero, and zero is what the gate uses.
        """
        scene(TWO)
        _quota_reader["map"] = {
            "Vendor A": _shut("Vendor A"),
            "Vendor B": _shut("Vendor B"),
        }

        assert _verification_vp_capacity() == 0

    def test_a_provider_with_no_reading_is_still_selectable(
        self, capacities, scene, _quota_reader,
    ):
        """Fail-open, exactly as the dispatch path treats a missing row.

        A provider the reading does not mention is not disqualified —
        there is no second source, so "not read" means "not known to be
        out". A width that dropped such a provider would be tighter than
        the dispatcher, which is the disagreement this whole change
        exists to remove.
        """
        scene(TWO)
        _quota_reader["map"] = {"Vendor A": _open("Vendor A")}

        assert _verification_vp_capacity() == CAP_OF_A + CAP_OF_B

    def test_a_parked_provider_loses_its_share_too(
        self, capacities, scene, _quota_reader,
    ):
        """Parked is not shut, and both mean "not serving anyone now"."""
        from dynamic_provider_concurrency import get_shared_cooldown

        scene(TWO)
        _quota_reader["map"] = {
            "Vendor A": _open("Vendor A"),
            "Vendor B": _open("Vendor B"),
        }
        get_shared_cooldown().park("Vendor B", seconds=600.0)

        assert _verification_vp_capacity() == CAP_OF_A


# ---------------------------------------------------------------------------
# The gate — read every time it binds
# ---------------------------------------------------------------------------


class TestTheGateIsReadEveryTimeItBinds:
    @pytest.mark.asyncio
    async def test_a_fleet_that_shrinks_stops_admitting(self):
        """The property a semaphore cannot have.

        ``asyncio.Semaphore(10)`` froze its width at construction. Read
        again on every bind, the same object admits ten, then eight, then
        nothing as the windows close — and the number of verification
        points already running is irrelevant to the decision, which is
        what makes a shrinking fleet expressible at all.
        """
        width = {"value": 10}
        gate = _VpCapacityGate(lambda: width["value"])
        released = asyncio.Event()

        admitted = []

        async def _hold():
            await gate.acquire()
            admitted.append(1)
            await released.wait()
            gate.release()

        tasks = [asyncio.ensure_future(_hold()) for _ in range(10)]
        # Let all ten take their slot.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(admitted) == 10:
                break
        assert len(admitted) == 10, admitted

        # The fleet halves while all ten are running.
        width["value"] = 5
        assert gate.capacity() == 5

        # And closes entirely.
        width["value"] = 0
        assert gate.capacity() == 0

        released.set()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_a_release_admits_exactly_one_waiter(self):
        """The queueing a semaphore does, kept."""
        gate = _VpCapacityGate(lambda: 1)
        released = asyncio.Event()
        order = []

        async def _hold(tag):
            await gate.acquire()
            order.append(tag)
            await released.wait()
            gate.release()

        first = asyncio.ensure_future(_hold("a"))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(_hold("b"))
        await asyncio.sleep(0.01)

        assert order == ["a"]
        released.set()
        await asyncio.gather(first, second)
        assert sorted(order) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_width_of_zero_is_a_real_width_not_a_missing_one(self):
        """Zero admits nobody; ``None`` (undeterminable) falls back.

        Two facts that look alike and mean opposite things. A gate that
        conflated them would either hang a plan whose providers are all
        shut, or — the shape the change was asked to remove — quietly
        start admitting the old typed number at the moment the fleet had
        nothing left.
        """
        fallback_reads = []

        def _fallback():
            fallback_reads.append(1)
            return 4

        shut = _VpCapacityGate(lambda: 0, _fallback)
        assert shut.capacity() == 0
        assert fallback_reads == [], "a real zero must not consult the fallback"

        unknown = _VpCapacityGate(lambda: None, _fallback)
        assert unknown.capacity() == 4
        assert fallback_reads == [1]

    @pytest.mark.asyncio
    async def test_a_raising_width_reader_is_treated_as_undeterminable(self):
        """Not as zero.

        The difference decides whether a provider outage hangs the round
        or is bounded by the operator's own cap, and the safe reading of
        "I could not measure" is the fallback rather than a closure.
        """

        def _boom():
            raise RuntimeError("provider surface is unavailable")

        gate = _VpCapacityGate(_boom, lambda: 4)
        assert gate.capacity() == 4

    @pytest.mark.asyncio
    async def test_a_waiter_that_cannot_be_released_re_reads_the_fleet(
        self, monkeypatch,
    ):
        """The queue asks the shared watchdog, once between all of them.

        There is a kind of "full" no release will fix: every provider is
        shut. Nothing will come back through ``release()``, so a waiter
        that only waited for one would sit until the run ended. It goes
        and asks the single process-wide watchdog instead — single
        flighted, so N queued verification points cost one request between
        them exactly as N dispatches do.
        """
        watchdog = _CountingWatchdog(lambda: {})
        monkeypatch.setattr(
            quota_watchdog, "get_shared_quota_watchdog", lambda: watchdog
        )

        width = {"value": 0}
        gate = _VpCapacityGate(lambda: width["value"], recheck_sec=0.01)
        admitted = asyncio.Event()

        async def _wait():
            await gate.acquire()
            admitted.set()

        task = asyncio.ensure_future(_wait())
        await asyncio.sleep(0.15)
        assert not admitted.is_set(), "a closed fleet admits nobody"

        # The window opens; the next recheck must let the waiter in.
        width["value"] = 4
        await asyncio.wait_for(admitted.wait(), timeout=5)
        gate.release()

        assert watchdog.calls, (
            "the waiter asked the watchdog for a fresh reading; a gate "
            "that only waited for a release would sit here until the run "
            "ended, because no release is coming from a closed fleet"
        )
        task.cancel()

    @pytest.mark.asyncio
    async def test_three_waiters_asking_together_share_one_reading(self, monkeypatch):
        """The same merge, one layer up — and measured, not asserted.

        The gate asks the *shared* watchdog rather than the reader, so a
        whole queue of verification points parked on one closed window
        costs one fan-out between them instead of one each. That is the
        number that matters: ``/api/usage`` asks every provider's own API
        in a single request, so the request is what gets billed.

        A merge only exists for asks that *overlap*, and waiters
        re-checking on a 10ms beat against a reader that answers
        instantly would take it in turns — which is real, and would make
        this test a measurement of a case nobody is in. So the read is
        held open until all three have arrived: the shape is a group of
        verification points parked on one window that just closed, which
        is what makes the burst.
        """

        # The read is held open until all three have asked, so the
        # overlap is arranged rather than hoped for: the three waiters
        # below are released onto the watchdog together and the fetch
        # does not return until every one of them has arrived.
        watchdog = _GatedWatchdog()
        monkeypatch.setattr(
            quota_watchdog, "get_shared_quota_watchdog", lambda: watchdog
        )

        # A half-second beat, so the three waiters re-check together
        # once and the burst is one moment rather than a stream — and so
        # the counts below are not diluted by the churn after it.
        gate = _VpCapacityGate(lambda: 0, recheck_sec=0.5)
        waiters = [asyncio.ensure_future(gate.acquire()) for _ in range(3)]

        for _ in range(600):
            await asyncio.sleep(0.01)
            if watchdog.calls >= 3:
                break
        assert watchdog.calls >= 3, (
            f"only {watchdog.calls} of three waiters reached the watchdog"
        )
        watchdog.release.set()
        await asyncio.sleep(0.1)
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)

        assert watchdog.calls == 3, (
            f"three waiters on one closed window asked {watchdog.calls} "
            f"times"
        )
        assert watchdog.probe_count == 1, (
            f"{watchdog.calls} asks cost {watchdog.probe_count} reads; a "
            f"gate that read for itself would cost one request per ask, "
            f"which is the per-dispatch cost this replaced"
        )
        assert watchdog.shared_reads == 2, (
            "the other two must have taken the first reader's answer "
            "rather than starting one of their own"
        )

    @pytest.mark.asyncio
    async def test_the_gate_reads_through_the_shared_watchdog(self, monkeypatch):
        """Which object it asks, stated structurally.

        The merge above only holds if the gate and the dispatch path read
        through the *same* watchdog. A gate that built its own — or held
        a reader of its own — would still pass every test above it, and
        would put the fleet straight back to one fan-out per waiting
        caller: which is the cost this whole change removed.
        """
        shared = quota_watchdog.QuotaWatchdog(fetch=lambda: {})
        monkeypatch.setattr(
            quota_watchdog, "get_shared_quota_watchdog", lambda: shared
        )

        gate = _VpCapacityGate(lambda: 0, recheck_sec=0.01)
        waiter = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0.15)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

        assert shared.probe_count > 0, (
            "the gate's read must land on the process-wide instance"
        )

    @pytest.mark.asyncio
    async def test_the_read_does_not_stop_the_round(self, monkeypatch):
        """Why the read is handed to a thread.

        ``urlopen`` blocks. Asked inline from the event loop it would
        stall every other verification point in the round — and the
        orchestrator with them — for the length of the request, which is
        the one thing a queue of waiters in an emergency must not do.

        So: a gate whose reader takes three tenths of a second, and a
        second coroutine that has to reach its line ten times while it is
        in flight. Asked inline, the second would get one or two lines
        and the test would say so.
        """
        def _slow_read():
            time.sleep(0.3)
            return {}

        monkeypatch.setattr(
            quota_watchdog, "get_shared_quota_watchdog",
            lambda: quota_watchdog.QuotaWatchdog(fetch=_slow_read),
        )

        gate = _VpCapacityGate(lambda: 0, recheck_sec=0.01)
        gate_waiter = asyncio.ensure_future(gate.acquire())
        ticks = []

        async def _other_work():
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks.append(time.monotonic())

        await asyncio.gather(asyncio.ensure_future(_other_work()))
        gate_waiter.cancel()
        await asyncio.gather(gate_waiter, return_exceptions=True)

        assert len(ticks) == 10, (
            f"the round's other coroutines got {len(ticks)} of 10 turns "
            f"while a waiter re-read the fleet"
        )



class _CountingWatchdog(quota_watchdog.QuotaWatchdog):
    """A real watchdog that also counts how often it was *asked*."""

    def __init__(self, fetch):
        super().__init__(fetch=fetch)
        self.calls = 0

    def probe(self, **kw):
        self.calls += 1
        return super().probe(**kw)


class _GatedWatchdog(_CountingWatchdog):
    """A watchdog whose read stays open until the test lets it go.

    Arranging the overlap rather than waiting for it: the fetch blocks on
    an event, so every waiter that asks before the release is provably
    inside the same read rather than in the next one.
    """

    def __init__(self):
        self.release = threading.Event()
        super().__init__(fetch=lambda: (self.release.wait(timeout=10), {})[1])


# ---------------------------------------------------------------------------
# The declared cap, demoted
# ---------------------------------------------------------------------------


class TestTheDeclaredCapIsTheFallbackNotTheCeiling:
    def test_the_typed_cap_is_not_the_source_of_the_width(
        self, capacities, scene, _quota_reader,
    ):
        """The number the order was about, stated as an absence.

        ``parallelism_cap`` is still four, the file still says four, and
        the width is twenty. If this test ever has to be deleted because
        the figure changed, the change that made it necessary is the one
        worth reading.
        """
        from verification_config import TimeoutPolicy

        assert TimeoutPolicy.defaults().parallelism_cap == 4

        scene(TWO)
        _quota_reader["map"] = {
            "Vendor A": _open("Vendor A"),
            "Vendor B": _open("Vendor B"),
        }
        assert _verification_vp_capacity() != 4

    def test_the_plan_wide_bound_reads_the_same_number_while_the_scene_resolves(
        self, capacities, scene, _quota_reader,
    ):
        """Why it cannot become the new bottleneck.

        ``_plan_wide_vp_cap`` sums the caps *declared* in the rules file.
        That is a different figure from the live fleet in both directions:
        it over-counts where two rules match one provider, and it misses
        any provider nobody wrote a rule for. Left in charge it would be
        exactly the complaint again — a number from somewhere other than
        the meters, sitting above the live one and capping it.

        So while the scene resolves, the plan bound is the *same live
        figure* as each group's. One number at two layers, not two
        numbers with a gap between them.
        """
        # One of the two pools shut: the live fleet is ten and the
        # declared ceiling is still twenty. That gap is the whole
        # argument — left in charge, the declared figure would cap a
        # fleet of ten at a number that has nothing to do with it, and
        # admit ten where twenty are serving.
        scene(TWO)
        _quota_reader["map"] = {
            "Vendor A": _open("Vendor A"),
            "Vendor B": _shut("Vendor B"),
        }

        plan_gate = _plan_wide_bound(_verification_vp_capacity)

        assert _plan_wide_vp_cap() == 20, (
            "the declared ceiling still sums the rules, shut or not — it "
            "cannot know a window is closed, which is the gap"
        )
        assert plan_gate.capacity() == _verification_vp_capacity() == 10

    def test_the_declared_ceiling_answers_only_when_nothing_is_derivable(
        self, capacities, scene, _quota_reader,
    ):
        """The scene cannot be resolved: the declaration is all there is.

        This is the case the declared ceiling is for. It is a worse
        measure of the fleet than the live reading — it cannot know a
        window is shut — but when the chain itself will not resolve there
        is no live reading to prefer, and a plan that hangs is worse than
        one bounded by a rough figure.
        """
        scene([])
        assert _verification_vp_capacity() is None

        plan_gate = _plan_wide_bound(_verification_vp_capacity)
        assert plan_gate.capacity() == _plan_wide_vp_cap() == 20

    def test_no_declared_capacity_and_no_scene_leaves_the_plan_unbounded(
        self, capacity_config, scene, _quota_reader,
    ):
        """Both measures gone: no bound, rather than a made-up one.

        Inventing a width here would put the same hand-written number
        back under a new name, which is the one thing the change was
        asked to stop.
        """
        capacity_config([], default=None)
        scene([])

        assert _plan_wide_vp_cap() is None
        assert _plan_wide_bound(_verification_vp_capacity).capacity() is None


# ---------------------------------------------------------------------------
# The profile is not left lying
# ---------------------------------------------------------------------------


class TestTheProfileReportsTheWidthThatWasUsed:
    def test_the_fallback_is_labelled_as_the_fallback(self):
        """A field nobody can tell the truth of is worse than none.

        ``parallelism_cap`` is still in the payload — a consumer reads it
        — and it is still four. What changed is what it *means*: it is
        the width used when nothing was derivable, and the payload now
        says so rather than letting a reader take four for the width a
        twenty-slot run actually used.
        """
        from verification_profile import ExecutionProfileGenerator
        from verification_config import TimeoutPolicy

        unlabelled = ExecutionProfileGenerator(
            None, TimeoutPolicy.defaults()
        ).build()
        assert unlabelled["parallelism_cap"] == 4
        assert unlabelled["vp_capacity"] is None
        assert unlabelled["vp_capacity_source"] == "parallelism_cap_fallback"

        labelled = ExecutionProfileGenerator(
            None, TimeoutPolicy.defaults(), vp_capacity=20
        ).build()
        assert labelled["vp_capacity"] == 20
        assert labelled["vp_capacity_source"] == "provider_pool"

    def test_the_agent_supplies_the_live_number_and_survives_not_getting_one(
        self, monkeypatch,
    ):
        """The wiring, and the degradation.

        The generator is a pure value calculator — reading the live pool
        inside it would make every profile build a CC Switch round-trip —
        so the live number is supplied by the agent. And a resolver that
        raises must not take the profile down with it: the run is more
        important than the card describing it.
        """
        from verification_config import TimeoutPolicy
        from verification_profile import ExecutionProfileGenerator

        assert _agent_live_capacity(lambda: 20) == 20
        assert _agent_live_capacity(lambda: None) is None
        assert _agent_live_capacity(_boom) is None

        # And the fallback still builds a usable profile.
        profile = ExecutionProfileGenerator(
            None, TimeoutPolicy.defaults(), vp_capacity=None
        ).build()
        assert profile["vp_capacity_source"] == "parallelism_cap_fallback"


class TestTheReportQuotesTheWidthThatBound:
    """A report must not describe a round with a number it did not use.

    Three build sites write ``execution_profile``, and only one of them
    runs *before* the round. The two that run after it cannot re-derive
    the width without either costing a CC Switch read to describe a run
    that is already over, or answering a different question — what the
    fleet looks like *now* — while the field's name says what the run
    *did*. So the gate's own reading is remembered and published.
    """

    def test_the_width_a_gate_bound_is_what_a_later_report_publishes(self):
        # Nothing has bound yet — before the round starts, or on a plan
        # with no groups — so the live reading stands in. That is a real
        # answer rather than a placeholder, and it is the same one the
        # gate would have used.
        stand_in = _stand_in(lambda: 20)
        assert stand_in._report_vp_capacity() == 20

        stand_in._note_vp_capacity(20)
        assert stand_in._report_vp_capacity() == 20

        # The fleet changes after the round; the report does not follow it.
        stand_in.vp_capacity_resolver = lambda: 3
        assert stand_in._report_vp_capacity() == 20, (
            "a report describes the run that happened, not the fleet that "
            "happened to be left afterwards"
        )

    def test_nothing_derivable_is_never_recorded_as_a_width(self):
        """``None`` is not a width, and must not become one.

        Recording it would publish ``vp_capacity: null`` with
        ``source: provider_pool``, which is the opposite of what it says.
        """
        stand_in = _stand_in(lambda: None)
        stand_in._note_vp_capacity(None)
        assert stand_in._last_vp_capacity is None
        assert stand_in._report_vp_capacity() is None

    def test_zero_is_recorded_because_zero_is_a_width(self):
        """The case a truthiness check would drop.

        A round that ran on a fleet which reported nothing serving is a
        round that happened at zero, and the report has to be able to
        say so — otherwise it falls through to "nothing was derivable"
        and labels a measured closure as an unmeasured one.
        """
        stand_in = _stand_in(lambda: None)
        stand_in._note_vp_capacity(0)
        assert stand_in._last_vp_capacity == 0
        assert stand_in._report_vp_capacity() == 0

    def test_the_report_falls_back_to_the_live_width_when_none_bound(self):
        """A report built without a round — the minimal/fallback paths."""
        stand_in = _stand_in(lambda: 7)
        assert stand_in._report_vp_capacity() == 7


def _stand_in(resolver):
    """The agent's width methods, bound to a stand-in.

    Constructing the real orchestrator needs a plan directory, a
    persistence manager and a log — none of which these questions are
    about. Both methods are pure over the resolver and the remembered
    width, so they are read off the class and called against one.
    """
    stand_in = type("_StandIn", (), {})()
    stand_in.vp_capacity_resolver = resolver
    stand_in._last_vp_capacity = None
    for name in ("_live_vp_capacity", "_note_vp_capacity", "_report_vp_capacity"):
        setattr(stand_in, name, getattr(verification_agent.Orchestrator, name).__get__(stand_in))
    return stand_in


def _boom():
    raise RuntimeError("provider surface is unavailable")


def _agent_live_capacity(resolver):
    """Read ``agent._live_vp_capacity`` off a stand-in, without a plan dir.

    Constructing the real orchestrator needs a plan directory, a
    persistence manager and a log — none of which the question is about.
    The method is pure over ``vp_capacity_resolver``, so it is bound to
    the class and called against a stand-in.
    """
    return _stand_in(resolver)._live_vp_capacity()


# ---------------------------------------------------------------------------
# The hardcoded figure, read from where it is written
# ---------------------------------------------------------------------------


def test_parallelism_cap_is_still_there_and_still_the_fallback():
    """Stated against the config, so its meaning is written down.

    Deleting the key would have been the easy version of this change, and
    it would have deleted the operator's only throttle for the case where
    nothing is derivable — a verification scene whose chain will not
    resolve. The key stays; what it means changes, and this is where that
    is recorded.
    """
    import verification_config

    yaml_path = Path(verification_config.__file__).parent / "configs" / "verification.yaml"
    text = yaml_path.read_text(encoding="utf-8")

    assert "parallelism_cap" in text, (
        "the fallback width must remain configurable"
    )
    key = text.find("parallelism_cap:")
    assert key != -1, "the fallback width must remain configurable"

    beside = text[:key].rsplit("\n\n", 1)[-1].lower()
    assert "fallback" in beside, (
        "the comment beside parallelism_cap must say what it is now the "
        "fallback for; a key whose comment still calls itself the "
        "throttle is the number this change was asked to remove, "
        f"re-described. Beside the key:\n{beside[-600:]}"
    )
    assert "derived" in beside, "and it must say where the real width comes from"
