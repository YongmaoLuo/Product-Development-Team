"""A pool with no width left is a window, not a verdict.

What this file pins
-------------------
``fleet_capacity(...).total == 0`` reads like a dead end: no candidate
has a slot, so there is nothing to wait for. That reading is wrong, and
the shape that proves it is the one this file drives end to end -- a
reading whose providers are **not** exhausted, with a remaining budget
low enough that ``compute_dynamic_limit`` rounds their caps down to
zero. Nothing in that state says "shut forever": it says "the budget is
nearly gone", and a budget that is nearly gone comes back when the
provider's own window rolls.

So the dispatch waits on the window probe -- the path already written for
a reading that *does* call the window shut -- and resumes the moment a
probe finds it open. Failing there turned a pause measured in hours
into a failed task, for a fleet that was on its way back.

What this file does **not** claim
---------------------------------
* That every unreachable scene now waits. It does not: a chain the tier
  never matched, and a chain whose every row was excluded or has no
  configuration, are facts about configuration with no clock on them,
  and they still fail closed. Those are pinned below.
* That a saturated pool is waited for here. It is not: a pool with
  width left and no free slot in it is a *lease*, and the lease wait
  keeps its own clock (``_slot_wake_step_sec``) -- a wait pointed at
  the queue's doubling beat would not see a reopen no reread can
  predict. The two waits stay separate, and this file asserts that the
  zero-cap wait is not the queue.

No test here reaches the network: the payload is parsed by the real
reader, and the sleep is recorded rather than served.
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
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    SceneProviderUnavailable,
    WorkClock,
)
from dynamic_provider_concurrency import (  # noqa: E402
    DEFAULT_COOLDOWN_SEC,
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

#: Ten per pool, so "fill it" is ``range(10)`` and a cap of zero is
#: unambiguously the budget's doing -- ``round_half_up(10 * 0.04) == 0``.
_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"


# ---------------------------------------------------------------------------
# Readings -- the shape under test is "not exhausted, no width left"
# ---------------------------------------------------------------------------


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
                "resets_at": None if resets_at is None else resets_at.isoformat(),
            },
        ],
        "success": True,
    }


def _reading(*rows):
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=_now()
    )


def _starved(provider, *, resets_in_minutes=12):
    """A provider with budget nearly gone: not exhausted, no width left.

    96% used leaves 4%, and ``compute_dynamic_limit`` rounds
    ``10 * 0.04`` down to ``0``. The reading says ``exhausted: False``
    throughout, which is the whole point: the meters are not closed, the
    caps they scale have nothing left to hand out.
    """
    return _window(
        provider,
        used=96.0,
        resets_at=_now() + timedelta(minutes=resets_in_minutes),
    )


def _starved_fleet(*, resets_in_minutes=12):
    """The whole tier at zero width.

    A single closed window among three would leave two candidates to
    dispatch to, and the wait would never be reached at all.
    """
    return _reading(*[
        _starved(name, resets_in_minutes=resets_in_minutes) for name in CHAIN
    ])


def _open(provider):
    return _window(
        provider, used=4.0, resets_at=_now() + timedelta(minutes=180)
    )


def _fleet_open():
    return _reading(*[_open(name) for name in CHAIN])


# ---------------------------------------------------------------------------
# Harness -- the real dispatch, a fake claude, a recorded wait
# ---------------------------------------------------------------------------


class _Reads:
    """The seam: what the dispatch waited for, and what it read.

    ``sleeps`` records the durations the dispatch *asked for* rather than
    serving them -- the numbers on this path are minutes long, and a test
    that served them would be testing the suite's patience.
    """

    def __init__(self, monkeypatch):
        self.sleeps: list = []
        self.reads = 0
        self.forced: list = []
        monkeypatch.setattr(
            coding_tool, "_quota_wake_sleep", self.sleeps.append
        )

    def serve(self, monkeypatch, readings):
        """Answer the quota reader with a queue, one reading per call.

        A single entry is served forever, which is how "the meters have
        not caught up yet" and "the budget never comes back" are both
        described: from the dispatch's side they are one observation.
        """
        queue = list(readings)

        def _load(*_a, **kw):
            self.reads += 1
            self.forced.append(bool(kw.get("force")))
            return queue[min(self.reads - 1, len(queue) - 1)]

        monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)
        return self


@pytest.fixture
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


class _Log:
    """The event logger, kept for the assertions about *which* wait ran."""

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


def _tool(tracker, log=None):
    tool = ClaudeCodingTool(
        scene="verification", concurrency_tracker=tracker, logger=log,
    )
    tool.provider_slot_wait_sec = 0.2
    return tool


def _run(tool, **kw):
    kwargs = dict(idle_timeout=30, total_timeout=30)
    kwargs.update(kw)
    return tool._run_claude_interactive("hi", **kwargs)


# ---------------------------------------------------------------------------
# The contract: zero width waits, and resumes on the probe that finds it
# ---------------------------------------------------------------------------


class TestZeroWidthWaitsForTheWindow:
    def test_a_cap_of_zero_is_probed_and_then_dispatches(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """The headline case, and the one the defect is about.

        Nothing dispatches, the wait goes through the window probe's
        cadence, and the work goes out through the same rules against
        the same chain as if none of it had happened. The stamp on the
        first reading is three days out, which also pins the cadence
        against being a countdown to it: a plan upgrade brings the
        budget back at a moment no stamp was going to name.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(monkeypatch, [
            _starved_fleet(resets_in_minutes=60 * 24 * 3),
            _fleet_open(),
        ])

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = _run(tool)

        assert result == "ok", (
            "a budget with nothing left is a pause, and the pause has to "
            "end in the work rather than in a failed call"
        )
        assert tool.current_call_provider in CHAIN, tool.current_call_provider
        assert reads.sleeps == [DEFAULT_COOLDOWN_SEC], reads.sleeps
        assert reads.forced[-1] is True, (
            f"a probe that could be answered from the cache read before "
            f"the sleep cannot report what happened during it: "
            f"{reads.forced}"
        )

    def test_the_reopen_is_dispatched_on_from_the_probe_that_found_it(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """Two probes, two waits, one dispatch -- and nothing after it.

        The window opens during the second wait, so the probe that reads
        it open is also the round that dispatches. A round spent
        noticing the reopen without dispatching on it would show up here
        as a third sleep, and is what a wait that re-decides "nothing
        free" once more before trying would look like.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(monkeypatch, [
            _starved_fleet(),
            _starved_fleet(),
            _fleet_open(),
        ])

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert tool.current_call_provider in CHAIN
        assert reads.sleeps == [DEFAULT_COOLDOWN_SEC] * 2, reads.sleeps
        assert reads.reads == 3, (
            f"one selection read plus one read per probe, and no more: "
            f"{reads.reads} reads"
        )

    def test_the_wait_is_the_probe_and_not_the_slot_queue(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """The two waits stay separate, and this is the half that matters.

        A slot is a lease released when the agent holding it finishes,
        so it is queued for on a short doubling beat. A zero cap is a
        budget that comes back on somebody else's clock, so it is probed
        for on the fixed interval. Merging them would spend an hours
        long wait on a seconds long premise, or -- the other way round
        -- give up on a window five minutes after it opened.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        log = _Log()
        _Reads(monkeypatch).serve(monkeypatch, [
            _starved_fleet(), _fleet_open(),
        ])

        tool = _tool(ActiveConcurrencyTracker(), log=log)
        _run(tool)

        assert "provider_quota_window_wait" in log.names(), sorted(log.names())
        assert "provider_scene_slot_wait" not in log.names(), (
            "a lease wait was queued for a window; the two are separate "
            "clocks and the window one is the right answer here"
        )
        assert coding_tool._slot_wake_step_sec(0) != DEFAULT_COOLDOWN_SEC, (
            "if the two clocks ever became one figure, this file's "
            "distinction would be unfalsifiable"
        )

    def test_the_wait_names_who_is_shut(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """A wait that names nobody is a wait an operator cannot read.

        The reading does not call these providers exhausted -- their
        caps were scaled to zero -- so the line that announces the wait
        has to name the rows with no width left instead of leaving the
        field empty. An operator reading it has to be able to tell
        "quota" from "broken", which is the difference between a
        provider name and an empty list.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        log = _Log()
        _Reads(monkeypatch).serve(monkeypatch, [
            _starved_fleet(), _fleet_open(),
        ])

        _run(_tool(ActiveConcurrencyTracker(), log=log))

        waits = log.of("provider_quota_window_wait")
        assert waits, sorted(log.names())
        assert waits[0]["quota_exhausted"] == sorted(CHAIN), waits[0]

    def test_a_wait_longer_than_the_budget_still_dispatches(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """Waiting is not working.

        One probe of five minutes against a budget of two seconds is the
        shape of a provider closed for an afternoon against a short cap.
        The budget is parked across the whole selection-and-wait region,
        so the wait is not charged to it and the call survives it.
        """

        class _VirtualClock:
            def __init__(self):
                self.now = 1000.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        budget = 2.0
        _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(monkeypatch, [
            _starved_fleet(resets_in_minutes=60 * 24),
            _fleet_open(),
        ])
        mono = _VirtualClock()
        tool = _tool(ActiveConcurrencyTracker())
        tool._work_clock = WorkClock(budget, monotonic=mono)

        # Serve the wait: the seam advances the test's own clock by the
        # interval it was asked for, so the budget sees elapsed time
        # rather than a number nobody moved.
        real_append = reads.sleeps.append

        def _serve(seconds):
            mono.advance(seconds)
            real_append(seconds)

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _serve)

        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert reads.sleeps == [DEFAULT_COOLDOWN_SEC], reads.sleeps
        assert DEFAULT_COOLDOWN_SEC > budget, (
            "the wait served must be longer than the whole budget "
            f"({DEFAULT_COOLDOWN_SEC}s against {budget}s), or this "
            f"proves nothing"
        )
        assert tool._work_clock.remaining() == budget, (
            "the wait must not have been charged to the budget at all"
        )

    def test_a_budget_that_never_comes_back_does_not_fail_the_call(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """No round budget, and this is the shape that shows it.

        How long a tier stays shut is a fact about somebody else's
        account, so the wait has no counter of its own to exhaust: a
        caller that gave up on a figure it invented would report a
        failure for a wait it declared over rather than get the window
        back. Four shut readings in a row, then an open one, so the
        assertion is that the fourth round is still waiting rather than
        the fourth having given up.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        readings = [_starved_fleet()] * 4 + [_fleet_open()]
        reads = _Reads(monkeypatch).serve(monkeypatch, readings)

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert reads.sleeps == [DEFAULT_COOLDOWN_SEC] * 4, (
            f"the wait ended after {len(reads.sleeps)} rounds; a budget "
            f"that has not come back is not a call that should fail"
        )


# ---------------------------------------------------------------------------
# The fail-closed answers that must survive the change
# ---------------------------------------------------------------------------


class TestTheAnswersThatStillFailClosed:
    """No clock on them, so no wait can end them.

    A chain the tier never matched, and a chain whose every row was
    excluded or has no configuration, are configuration facts. Waiting
    for them would wait for something nothing is going to do, which is
    the fail-closed answer rather than a pause.
    """

    def test_a_chain_the_tier_never_matched_still_raises(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(monkeypatch, [_fleet_open()])
        monkeypatch.setattr(
            provider_routing, "resolve_provider_chain", lambda *a, **k: []
        )

        with pytest.raises(SceneProviderUnavailable) as excinfo:
            _run(_tool(ActiveConcurrencyTracker()))

        assert excinfo.value.reason == "no_tier_match"
        assert reads.sleeps == [], reads.sleeps

    def test_every_candidate_filtered_out_still_raises(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """Excluded or unconfigured: the rows exist and none may be used.

        Deliberately *not* the zero-width case, which is a budget with
        nothing left rather than a row nothing may be served from. This
        one has no waiting to do, so the wait must not be entered.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(monkeypatch, [_fleet_open()])
        monkeypatch.setattr(
            ClaudeCodingTool, "_check_provider_availability",
            staticmethod(lambda name: (False, None)),
        )

        with pytest.raises(SceneProviderUnavailable) as excinfo:
            _run(_tool(ActiveConcurrencyTracker()))

        assert excinfo.value.reason == "all_candidates_skipped"
        assert reads.sleeps == [], reads.sleeps
