"""A provider whose cooldown expires must come back into the window wait.

What this file pins
-------------------
The closed-window wait re-reads the quota payload on every round, and the
reading is the only thing it watches. But the *candidate set* it waits
with was collected before the wait began, and a wait can easily last
longer than what that set was built from: a provider parked by a refusal
earlier in the run comes back when its cooldown expires, and it is in the
chain the whole time.

So the wait kept offering a set that could not contain a provider that
had come back, and the fleet sat on a provider it had already paid for
the privilege of forgetting. The set is rebuilt now, on every round, by
the same collection that built it the first time — same chain, same
exclusions, same park state read fresh.

What this file does **not** claim
---------------------------------
* That the rebuild bends a rule to get out of the wait. It does not: a
  provider still inside its cooldown is still filtered out, by the same
  filter that filtered it out the first time, and that is pinned below.
* That the wait can spin forever. A rebuild that comes back empty with
  nothing on a clock is the fail-closed answer, and that is pinned too.

No test here reaches the network: the payload is parsed by the real
reader, and the wait is recorded and served on a clock the test owns.
"""

from __future__ import annotations

import os
import stat
import sys
import time as real_time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import dynamic_provider_concurrency  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    SceneProviderUnavailable,
)
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)

#: The tier this file drives. 'Vendor A Pro' is the one that gets
#: parked; 'Vendor A' is the one whose window shuts and reopens.
CHAIN = ["Vendor A Pro", "Vendor A"]

PARKED = CHAIN[0]
WINDOW = CHAIN[1]

#: How far the fake wait moves the clock. The park under test is a
#: hundredth of a second long, so the wait has to be longer than the
#: park and shorter than nothing else in the file cares about.
WAIT_ADVANCE_SEC = 0.05

#: A park long enough to still be alive after one wait and expired
#: after two — the shape the "keep waiting" case needs.
TWO_WAIT_PARK_SEC = 0.07

_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor-a$", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"


# ---------------------------------------------------------------------------
# Readings -- the shape under test is "one window shut, one park running"
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _window(provider, *, used, resets_in_minutes=30):
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
                    _now() + timedelta(minutes=resets_in_minutes)
                ).isoformat(),
            },
        ],
        "success": True,
    }


def _reading(*rows):
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=_now()
    )


def _fleet(*, window_closed: bool):
    """One reading of the whole tier.

    'Vendor A Pro' is always open -- it is the provider the park is
    about -- and 'Vendor A' is shut or open depending on the round.
    """
    return _reading(
        _window(PARKED, used=4.0),
        _window(WINDOW, used=100.0 if window_closed else 4.0),
    )


# ---------------------------------------------------------------------------
# The clock the park list keeps its deadlines on
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock the test owns, with the real module behind it.

    ``ProviderCooldown`` parks against ``time.monotonic()``, so "this
    park expires while the dispatch waits" is not expressible without
    either sleeping for real -- which is a coin flip on a loaded
    machine, since a hundredth of a second is also the width of the
    setup between the park and the first read of it -- or handing that
    module a clock.

    Only the *module's* ``time`` name is replaced, not the global one,
    so nothing outside the cooldown sees the fake. Everything the
    module asks of ``time`` that this class does not implement is
    delegated to the real module.
    """

    def __init__(self, real):
        self._real = real
        self.now = 1_000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)

    def sleep(self, seconds):
        # The dispatch's own poll, if it ever gets there: virtual, and
        # it moves the clock, so a poll loop still terminates.
        self.advance(seconds)

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Harness -- the real dispatch, a fake claude, a recorded wait
# ---------------------------------------------------------------------------


def _config():
    return {
        "base_url": "https://api.vendor-a.example/anthropic",
        "api_key": "sk-fake",
        "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
    }


class _Reads:
    """The two seams: what the dispatch waited for, and what it read.

    ``sleeps`` records the durations the dispatch *asked for* rather
    than serving them -- the numbers on this path are minutes long --
    and the clock is moved by a fixed step instead, which is what
    expires a park.
    """

    def __init__(self, monkeypatch, clock):
        self.clock = clock
        self.sleeps: list = []
        self.reads = 0
        self.forced: list = []
        #: Called with the round number on every wait, so a test can
        #: change the world between two rounds.
        self.on_sleep = None
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", self._sleep)

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))
        self.clock.advance(WAIT_ADVANCE_SEC)

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
def clock(monkeypatch):
    fake = _FakeClock(real_time)
    monkeypatch.setattr(dynamic_provider_concurrency, "time", fake)
    return fake


@pytest.fixture
def reads(monkeypatch, clock):
    return _Reads(monkeypatch, clock)


@pytest.fixture
def cooldown():
    fresh = ProviderCooldown()
    set_shared_cooldown(fresh)
    yield fresh
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
        staticmethod(lambda name: (True, _config())),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage", lambda *a, **k: {}
    )


class _Log:
    """The event logger, kept for the assertions about which wait ran."""

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
# The contract: the candidate set is rebuilt on every round of the wait
# ---------------------------------------------------------------------------


class TestTheWindowWaitRebuildsItsCandidates:
    def test_cooldown_expiry_reenters_the_window_wait(
        self, tmp_path, monkeypatch, scene, cooldown, clock, reads,
    ):
        """The headline case, and the one the defect is about.

        'Vendor A Pro' is parked for a hundredth of a second -- a
        refusal earlier in the run, nothing more. 'Vendor A''s window
        is shut, so the dispatch waits on the probe. The wait is longer
        than the park, so by the time the first probe comes back
        'Vendor A Pro' is out of cooldown, and it is in the chain the
        whole time.

        The answer is 'Vendor A Pro', on the first probe, after one
        wait. The set that was collected before the wait could not
        contain it, and asking again with the same set is what kept the
        fleet on a provider it had already stopped believing in.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cooldown.park(PARKED, seconds=0.01, reason="quota exhausted")
        reads.serve(monkeypatch, [
            _fleet(window_closed=True),
            _fleet(window_closed=True),
            _fleet(window_closed=False),
        ])

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = _run(tool)

        assert result == "ok", (
            "a provider whose cooldown expired is dispatchable again; "
            "the wait has to be able to see that"
        )
        assert tool.current_call_provider == PARKED, (
            f"the parked provider came back and outranks the shut one; "
            f"got {tool.current_call_provider!r}"
        )
        assert len(reads.sleeps) == 1, (
            f"the answer was available on the first probe; the wait took "
            f"{len(reads.sleeps)} rounds: {reads.sleeps}"
        )

    def test_parked_provider_is_not_resurrected_before_cooldown(
        self, tmp_path, monkeypatch, scene, cooldown, clock, reads,
    ):
        """The other half: the rebuild is not a way around the park.

        'Vendor A Pro' is parked for ten minutes. The wait happens, the
        rebuild happens, and the answer is still 'Vendor A' -- because a
        provider inside its cooldown is filtered out by the same rule
        that filtered it out the first time. A rebuild that put it back
        would be a dispatch into a refusal the fleet already paid for.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cooldown.park(PARKED, seconds=600.0, reason="quota exhausted")
        log = _Log()
        reads.serve(monkeypatch, [
            _fleet(window_closed=True),
            _fleet(window_closed=False),
        ])

        tool = _tool(ActiveConcurrencyTracker(), log=log)
        result, _session, _meta = _run(tool)

        assert result == "ok"
        assert tool.current_call_provider == WINDOW, (
            f"a provider inside its cooldown must not be dispatched to; "
            f"got {tool.current_call_provider!r}"
        )
        assert "provider_scene_candidate_parked" in log.names(), (
            "the rebuild did not run, so this file proved nothing about "
            "it"
        )

    def test_a_rebuild_with_nothing_left_fails_closed(
        self, tmp_path, monkeypatch, scene, cooldown, clock, reads,
    ):
        """A rebuild that comes back empty, with nothing on a clock, ends.

        The wait has no round budget -- the limit on a closed window
        belongs to the provider, not to the dispatcher -- so a rebuild
        that came back empty and kept the wait alive would spin until
        the caller's own timeout, towards an answer that was already
        known. Nothing in the chain is parked, so nothing is coming
        back: the fail-closed answer is the right one.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cooldown.park(PARKED, seconds=0.01, reason="quota exhausted")
        reads.serve(monkeypatch, [
            _fleet(window_closed=True),
            _fleet(window_closed=False),
        ])

        # Both rows stop being reachable once the dispatch has waited:
        # the rebuild finds a chain whose every member is gone.
        state = {"waited": False}
        reads.on_sleep = lambda _round: state.__setitem__("waited", True)
        monkeypatch.setattr(
            ClaudeCodingTool, "_check_provider_availability",
            staticmethod(
                lambda name: (False, None) if state["waited"] else (True, _config())
            ),
        )

        tool = _tool(ActiveConcurrencyTracker())
        with pytest.raises(SceneProviderUnavailable) as excinfo:
            _run(tool)

        assert excinfo.value.reason == "all_candidates_skipped"
        assert len(reads.sleeps) == 1, (
            f"the wait must not spin on an empty rebuild; it took "
            f"{len(reads.sleeps)} rounds: {reads.sleeps}"
        )

    def test_a_rebuild_of_only_parked_members_keeps_waiting(
        self, tmp_path, monkeypatch, scene, cooldown, clock, reads,
    ):
        """Empty, but something is coming back: the wait continues.

        The park list is process-wide, so a provider can be parked while
        this dispatch waits -- by another agent's refusal, in another
        chain. When the rebuild finds the whole tier parked, the wait is
        not over: the park expires on its own, and the next round's
        rebuild finds it. Failing closed here would turn a pause into a
        failed call for a fleet that was on its way back.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        cooldown.park(PARKED, seconds=0.01, reason="quota exhausted")

        def _refuse_elsewhere(round_no):
            if round_no == 1:
                # Another agent's refusal parks the whole tier while
                # this dispatch is asleep in its first wait.
                cooldown.park(PARKED, seconds=TWO_WAIT_PARK_SEC, reason="429")
                cooldown.park(WINDOW, seconds=TWO_WAIT_PARK_SEC, reason="429")

        reads.on_sleep = _refuse_elsewhere
        reads.serve(monkeypatch, [
            _fleet(window_closed=True),
            _fleet(window_closed=True),
            _fleet(window_closed=False),
        ])

        tool = _tool(ActiveConcurrencyTracker())
        result, _session, _meta = _run(tool)

        assert result == "ok", (
            "a tier that is entirely parked is a pause, and the pause "
            "ends when the park does"
        )
        assert tool.current_call_provider == PARKED, (
            f"the park expired and the provider came back; got "
            f"{tool.current_call_provider!r}"
        )
        assert len(reads.sleeps) == 2, (
            f"the wait had to outlive the park; it took "
            f"{len(reads.sleeps)} rounds: {reads.sleeps}"
        )
