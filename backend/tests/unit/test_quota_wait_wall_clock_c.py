"""A quota wait is not work, so it must not be charged to the budget.

What this file pins
-------------------
``timeout=N`` has always been enforced in two layers on the Claude
path: layer 1 is the silence watcher inside ``_run_claude_interactive``
and layer 2 is the ``Future.result`` that surfaces the finished
dispatch. Layer 1 was already honest about this -- it is started
*after* ``Popen``, so the minutes a dispatch spends parked on a closed
provider window, before there is a subprocess to watch, never reach it.

Layer 2 was not. ``Future.result(timeout=N)`` spends N on **wall** time,
and the dispatch on the other side of the executor can be parked on
somebody else's quota meter for as long as that meter says. So a
``timeout=300`` dispatch that spent 40 minutes asleep in the
closed-window wake was killed at 5 minutes by a number it had not spent
5 minutes of -- and when the window came back at minute 51 and the work
was done and dispatched successfully, that expired ``TimeoutError`` took
the answer with it, excluded the provider that had just served it, and
reported the call as a provider outage.

The contract these tests hold:

  * wall time spent parked on a quota window is not charged to
    ``timeout``;
  * the wait itself stays unbounded -- the operator's explicit design,
    and nothing here may quietly add a round cap or a ceiling to it;
  * a dispatch that is not waiting is bounded exactly as before, in both
    directions (a generous budget passes, a tight one still raises), and
    work done *after* a wait is still charged.

Every test drives the **production entry point**
``ClaudeCodingTool.query_json`` / ``.query``, through the real provider
selection and the real park in ``_run_claude_interactive``. The only
substitutions are the quota reader (a queue of readings) and the wait
itself (``_quota_wake_sleep``, the module-level seam the dispatch is
documented to take), which records what was asked for and then serves a
real slice of it -- real, because the thing under test *is* a wall
clock, and a fake one would prove nothing.

No test here reaches the network and none waits on a real provider
window: the slice served is small enough to keep the suite quick and
large enough that the budget expires several times over inside it.

§2 of the change (the ``idle=1800`` guess in the log and error text,
and four dead ``future.cancel()`` calls) is pinned by the last test in
this file, because that one has an observable payload. The dead
``cancel()`` calls have no observable behaviour to assert at all -- they
returned a value nobody read and did nothing to a running future -- so
they are deliberately left untested rather than covered by a test that
would pass whatever they became.
"""
from __future__ import annotations

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
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    HardTimeoutError,
    WorkClock,
)
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

#: Ten per pool, so "fill it" is ``range(10)`` and a cap of zero is
#: unambiguously the reading's doing -- ``round_half_up(10 * 0.04) == 0``.
_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]

#: What the fake CLI answers with. The ``result`` field is the text the
#: read loop hands back, and for ``query_json`` that text has to be a
#: JSON document in its own right.
REBUILD_REPLY = '{"type":"result","result":"{\\"rebuilt\\": true}"}'

#: The same answer, delivered as an assistant text delta instead. Used by
#: the CLI that then keeps working: it gives the read loop something to
#: return, so a kill mid-stream is not mistaken for an empty response
#: and sent down the fresh-session retry path.
REBUILD_ASSISTANT = (
    '{"type":"assistant","message":{"content":[{"type":"text","text":'
    '"{\\"rebuilt\\": true}"}]}}'
)


# ---------------------------------------------------------------------------
# Readings
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


def _starved(provider):
    """Budget nearly gone: ``exhausted`` is False, the cap rounds to 0.

    The meters are not closed; the width they scale has nothing left to
    hand out, which is the reading the closed-window wake was written
    for and the one that keeps the dispatch parked here.
    """
    return _window(
        provider,
        used=96.0,
        resets_at=_now() + timedelta(minutes=60 * 24),
    )


def _starved_fleet():
    return _reading(*[_starved(name) for name in CHAIN])


def _open(provider):
    return _window(
        provider, used=4.0, resets_at=_now() + timedelta(minutes=180)
    )


def _fleet_open():
    return _reading(*[_open(name) for name in CHAIN])


# ---------------------------------------------------------------------------
# Harness -- the real dispatch, a fake claude, a served slice of the wait
# ---------------------------------------------------------------------------


class _Reads:
    """What the dispatch read, and the wait it was served.

    ``sleeps`` records the duration the dispatch *asked* for (its own
    probe interval, minutes long) while ``served`` records what this
    test actually spent on the wall clock. The first number is what the
    cadence is; the second is what the budget had to survive.
    """

    def __init__(self, monkeypatch):
        self.sleeps: list = []
        self.served = 0.0
        self.reads = 0

    def serve(self, monkeypatch, readings, *, slice_sec: float = 0.5):
        """Answer the quota reader with a queue, one reading per call.

        A single entry is served forever, which is how "the meters have
        not caught up yet" is described: from the dispatch's side it is
        one observation, repeated.
        """
        queue = list(readings)
        reads = self

        def _load(*_a, **kw):
            reads.reads += 1
            return queue[min(reads.reads - 1, len(queue) - 1)]

        monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)

        def _wake(seconds: float) -> None:
            reads.sleeps.append(seconds)
            reads.served += slice_sec
            time.sleep(slice_sec)

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _wake)
        return self


@pytest.fixture
def _fresh_cooldown():
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _pools(capacity_config):
    capacity_config(_POOL_CAPS, default=10)


def _install_fake_claude(tmp_path, monkeypatch, *, mode: str = "quick") -> None:
    """Put a ``claude`` on PATH that answers, in one of three ways.

    ``quick``     -- answers at once; the dispatch is over in
                     milliseconds, so anything that times it out is the
                     budget and not the CLI.
    ``streaming`` -- answers, then keeps emitting stdout for
                     ``stream_sec`` seconds. Layer 1 measures silence,
                     so a CLI that keeps printing is never killed by it
                     and the budget that expires is unambiguously layer
                     2's. This is the "alive and working but slower
                     than you allowed" case the silence guard exists to
                     let through.

    Every mode also appends a line to ``<tmp>/launches.log``. The chain
    here has three providers and the provider-fallback ladder has two
    retries, so a call that times out once can still come back with the
    right answer -- three dispatches late, against three providers the
    incident would have exhausted. Counting launches is what separates
    "the answer arrived" from "the answer arrived after the timeout
    threw away the dispatch that had already produced it".
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    launches = tmp_path / "launches.log"
    launches.write_text("", encoding="utf-8")
    stamp = ["echo launched >> '%s'" % launches]
    if mode == "quick":
        body = ["#!/bin/sh", *stamp, "echo '%s'" % REBUILD_REPLY]
    elif mode == "streaming":
        body = [
            "#!/bin/sh",
            *stamp,
            "echo '%s'" % REBUILD_ASSISTANT,
            "i=0",
            "while [ $i -lt 60 ]; do",
            "  echo 'still working'",
            "  sleep 0.1",
            "  i=$((i + 1))",
            "done",
        ]
    else:  # pragma: no cover - a typo in a test, not a product path
        raise AssertionError("unknown fake claude mode %r" % mode)
    fake.write_text("\n".join(body) + "\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    return launches


def _launches(log_path):
    """How many times the CLI was actually spawned."""
    return len(log_path.read_text(encoding="utf-8").split())


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
    """The event logger, kept for the assertions about what was said."""

    def __init__(self):
        self.events: list = []

    def _record(self, name, message, data=None):
        self.events.append((name, message, data or {}))

    warning = info = error = debug = _record

    def names(self):
        return {name for name, _msg, _data in self.events}

    def of(self, name):
        return [data for event, _msg, data in self.events if event == name]


def _tool(log=None):
    tool = ClaudeCodingTool(
        scene="verification",
        concurrency_tracker=ActiveConcurrencyTracker(),
        logger=log,
    )
    tool.provider_slot_wait_sec = 0.2
    return tool


# ---------------------------------------------------------------------------
# The wait is not charged to the caller's budget
# ---------------------------------------------------------------------------


class TestQuotaWaitIsNotChargedToTheBudget:
    def test_a_wait_many_times_the_budget_still_returns_the_answer(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """The incident, in miniature, and the headline of this file.

        The budget is one second. The fleet is shut, so the dispatch
        parks, probes three times -- three seconds of wall clock against
        a budget it never spent -- and then the window opens, the work
        goes out, and the answer comes back. Before 2026-10-14 the
        ``Future.result(timeout=...)`` expired inside the first probe,
        and what the caller got instead was a ``TimeoutError`` for a
        dispatch that was about to succeed.
        """
        budget = 1.0
        launches = _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(
            monkeypatch,
            [_starved_fleet(), _starved_fleet(), _starved_fleet(), _fleet_open()],
            slice_sec=1.0,
        )
        tool = _tool()

        started = time.monotonic()
        result = tool.query_json("rebuild the binary", timeout=budget)
        elapsed = time.monotonic() - started

        assert result == {"rebuilt": True}, result
        assert len(reads.sleeps) == 3, reads.sleeps
        assert reads.served > 2 * budget, (
            f"the wait only served {reads.served:.2f}s against a {budget}s "
            f"budget; a shorter wait proves nothing"
        )
        assert reads.served > 0.5 * elapsed, (
            f"the dispatch was not parked for most of its {elapsed:.2f}s "
            f"({reads.served:.2f}s served)"
        )
        assert _launches(launches) == 1, (
            f"{_launches(launches)} dispatches for one answer: the expired "
            f"timeout did not stop at the first one. The incident is not "
            f"that the call failed -- it is that a successful dispatch was "
            f"thrown away and the next provider was burned to replace it."
        )

    def test_the_wait_itself_is_not_capped(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """No round budget on the wait, and this is the shape that shows it.

        How long a tier stays shut is a fact about somebody else's
        account, so the wait has no counter of its own to exhaust. This
        drives more parked probes than any cap this change could have
        introduced a ceiling under -- and past ``MAX_TIMEOUT_RETRIES``,
        which is the only round counter anywhere on this path that a
        reader might mistake for one. The call still comes back with its
        answer.

        What this cannot show is unboundedness itself; no finite test
        can. It shows the absence of any *small* bound, which is what
        adding one by accident would look like.
        """
        rounds = 12
        budget = 1.0
        launches = _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(
            monkeypatch,
            [_starved_fleet() for _ in range(rounds)] + [_fleet_open()],
            slice_sec=0.4,
        )
        tool = _tool()

        result = tool.query_json("rebuild the binary", timeout=budget)

        assert result == {"rebuilt": True}, result
        assert len(reads.sleeps) >= rounds, (
            f"only {len(reads.sleeps)} probes; the wait gave up at a "
            f"round number instead of on an open window"
        )
        assert reads.served > 4 * budget, reads.served
        assert _launches(launches) == 1, _launches(launches)
        assert tool.MAX_TIMEOUT_RETRIES < rounds, (
            "the round count has to exceed the only retry counter on "
            "this path or the assertion cannot tell the two apart"
        )

    def test_a_caller_supplied_clock_is_the_one_that_gets_spent(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """Adoption is one-way: whoever declared the budget keeps it.

        The verification sub-agent hands its sub-agents a ``WorkClock``
        covering the hard cap before the call, and that clock is already
        the one ``_run_claude_interactive`` parks. Overwriting it with a
        fresh one built from ``timeout`` would silently replace the hard
        cap for every verification dispatch -- so the existing clock has
        to win, and the wait still has to come off its budget.
        """
        budget = 1.0
        launches = _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(
            monkeypatch,
            [_starved_fleet(), _starved_fleet(), _starved_fleet(), _fleet_open()],
            slice_sec=0.5,
        )
        tool = _tool()
        clock = WorkClock(120.0)
        tool._work_clock = clock

        result = tool.query_json("rebuild the binary", timeout=budget)

        assert result == {"rebuilt": True}, result
        assert tool._work_clock is clock, (
            "the caller's clock was replaced; its budget is no longer the "
            "one being spent"
        )
        assert reads.served > budget, reads.served
        assert _launches(launches) == 1, _launches(launches)
        assert clock.remaining() > 100.0, (
            f"a {reads.served}s wait was charged to a 120s budget that "
            f"leaves {clock.remaining():.1f}s; the park is supposed to "
            f"cost this clock nothing"
        )

    def test_the_adopted_clock_does_not_outlive_the_call(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """Adopted for the call, put back afterwards.

        Left installed, that clock goes on answering ``is_parked`` for a
        dispatch that has finished -- and ``SubAgentHandle.
        waiting_on_quota_window`` asks exactly that question of every
        handle the heartbeat sweep holds. An abandoned clock parked at
        the last wait would read "not stuck" for a handle whose
        subprocess really is stuck.

        The absence before the call is the production case that has to
        work at all: the rebuild agent never hands a clock down.
        """
        budget = 1.0
        launches = _install_fake_claude(tmp_path, monkeypatch)
        _Reads(monkeypatch).serve(
            monkeypatch,
            [_starved_fleet(), _starved_fleet(), _fleet_open()],
            slice_sec=0.5,
        )
        tool = _tool()

        assert tool._work_clock is None, (
            "precondition: this tool arrives with no clock, which is "
            "exactly the rebuild-agent shape"
        )
        result = tool.query_json("rebuild the binary", timeout=budget)

        assert result == {"rebuilt": True}, result
        assert _launches(launches) == 1, _launches(launches)
        assert tool._work_clock is None, (
            "the adopted clock outlived the dispatch that adopted it"
        )

    def test_query_is_bounded_the_same_way(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """``query`` is the other Claude entry, and it had the same defect.

        ``_query_internal_m1`` wraps its dispatch in the same executor
        and the same ``Future.result(timeout=...)``. Fixing only the JSON
        half would leave ``query(..., timeout=N)`` as the one public
        entry still chargeable for a window it was asleep through.
        """
        budget = 1.0
        launches = _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(
            monkeypatch,
            [_starved_fleet(), _starved_fleet(), _starved_fleet(), _fleet_open()],
            slice_sec=0.5,
        )
        tool = _tool()

        text = tool.query("say ok", timeout=budget)

        assert text == '{"rebuilt": true}', text
        assert reads.served > budget, reads.served
        assert _launches(launches) == 1, _launches(launches)


# ---------------------------------------------------------------------------
# A dispatch that is not waiting is bounded exactly as before
# ---------------------------------------------------------------------------


class TestTheBudgetStillBoundsWork:
    def test_a_generous_budget_passes_through_untouched(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """Nothing parked, nothing changed: the budget is still the budget.

        An open fleet, so the dispatch never enters the wait and the
        clock is never parked. This is the half of the contract that
        says the fix is not "a bigger timeout" -- the figure the caller
        passed is still the figure that is spent, and thirty seconds of
        it is thirty seconds of dispatch.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        reads = _Reads(monkeypatch).serve(monkeypatch, [_fleet_open()])
        tool = _tool()

        result = tool.query_json("rebuild the binary", timeout=30.0)

        assert result == {"rebuilt": True}, result
        assert reads.sleeps == [], (
            f"this dispatch was never parked, so it must not have probed: "
            f"{reads.sleeps}"
        )
        assert reads.served == 0.0, reads.served

    def test_a_tight_budget_still_raises(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """And the other direction: a budget that is too small still fires.

        Without this, "the wait is not charged" is also satisfied by a
        fix that simply stopped enforcing anything. The fleet is open --
        no park, no wait -- and the CLI keeps streaming, which layer 1's
        silence watcher is documented to let through, so the only thing
        that can end this call is the caller's own figure.
        """
        _install_fake_claude(tmp_path, monkeypatch, mode="streaming")
        _Reads(monkeypatch).serve(monkeypatch, [_fleet_open()])
        tool = _tool()

        with pytest.raises(TimeoutError) as caught:
            tool.query_json("rebuild the binary", timeout=1.0)

        assert not isinstance(caught.value, HardTimeoutError), (
            "layer 1 fired, so this did not test the layer-2 budget"
        )

    def test_work_done_after_the_wait_is_still_charged(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """The wait is free; the work behind it is not.

        A wait that is not charged has to stop being free the moment the
        window reopens, or "park the budget" is just "disable the
        budget". The fleet is shut for three probes, the wait is served
        past the whole budget, and then a CLI that keeps working takes
        longer than the budget again -- so the timeout has to arrive,
        and it has to arrive from the work rather than from the wait.
        """
        budget = 1.0
        launches = _install_fake_claude(tmp_path, monkeypatch, mode="streaming")
        reads = _Reads(monkeypatch).serve(
            monkeypatch,
            [_starved_fleet(), _starved_fleet(), _starved_fleet(), _fleet_open()],
            slice_sec=0.5,
        )
        tool = _tool()

        started = time.monotonic()
        with pytest.raises(TimeoutError) as caught:
            tool.query_json("rebuild the binary", timeout=budget)
        elapsed = time.monotonic() - started

        assert not isinstance(caught.value, HardTimeoutError), caught.value
        assert len(reads.sleeps) == 3, reads.sleeps
        assert _launches(launches) >= 1, _launches(launches)
        assert reads.served > budget, (
            "precondition: the wait alone outlasted the budget, so a "
            f"timeout here can only come from the work ({reads.served}s)"
        )
        # The streaming CLI answers and then prints for six seconds, so
        # the shape being ruled out is "waited the CLI out". One attempt
        # per provider-fallback retry, each billed the budget: three
        # attempts times one second, not three times six.
        attempts = tool.MAX_TIMEOUT_RETRIES + 1
        assert elapsed < reads.served + attempts * budget + 1.5, (
            f"the call ran {elapsed:.2f}s -- {reads.served:.2f}s of wait "
            f"plus {attempts} attempts at a {budget}s budget. It waited "
            f"the CLI out instead of billing it."
        )

    def test_the_layer_two_budget_is_reported_as_a_budget(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """The figure that expires is ``timeout``, labelled as a budget.

        This is the other half of the change: the log and the error text
        used to quote ``DEFAULT_IDLE_TIMEOUT`` (1800s) as the cause of a
        timeout that had nothing to do with it. The caller's figure is a
        budget, so it is reported as one, and the cause is whatever
        exception actually arrived.
        """
        budget = 1.0
        _install_fake_claude(tmp_path, monkeypatch, mode="streaming")
        _Reads(monkeypatch).serve(monkeypatch, [_fleet_open()])
        log = _Log()
        tool = _tool(log=log)

        with pytest.raises(TimeoutError) as caught:
            tool.query_json("rebuild the binary", timeout=budget)

        assert "1800" not in str(caught.value), caught.value
        assert "Idle timeout" not in str(caught.value), caught.value

        fallbacks = log.of("provider_timeout_fallback_json")
        assert fallbacks, sorted(log.names())
        for payload in fallbacks:
            assert "idle_sec" not in payload, payload
            assert payload["layer2_budget_sec"] == budget, payload
            assert payload["timeout_error"], payload

    def test_the_m1_fallback_says_the_same_thing(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown,
    ):
        """``query`` reports its budget the same way ``query_json`` does.

        The two provider-fallback arms are copies of each other, and the
        ``idle=1800`` guess was in both. Fixing the JSON one and leaving
        the text one quoting a constant that never fired is the same lie
        in the same file, so this drives the other entry and reads its
        log. The retries are exhausted on the way out, which is also the
        only place the "All providers failed" message is rendered -- and
        that message used to open with the same 1800s guess.
        """
        budget = 1.0
        _install_fake_claude(tmp_path, monkeypatch, mode="streaming")
        _Reads(monkeypatch).serve(monkeypatch, [_fleet_open()])
        log = _Log()
        tool = _tool(log=log)

        with pytest.raises(TimeoutError) as caught:
            tool.query("say ok", timeout=budget)

        assert "1800" not in str(caught.value), caught.value
        assert "Idle timeout" not in str(caught.value), caught.value

        fallbacks = log.of("provider_timeout_fallback")
        assert fallbacks, sorted(log.names())
        for payload in fallbacks:
            assert "idle_sec" not in payload, payload
            assert payload["layer2_budget_sec"] == budget, payload
            assert payload["timeout_error"], payload
