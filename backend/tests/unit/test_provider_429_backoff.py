"""A rate-limited provider is waited out, not thrown away.

What this pins
--------------
The backend used to read every provider failure as "this provider has
no budget": park it for the quota window (half an hour), exclude it, and
re-dispatch the work somewhere else. That answer is right for a spent
plan and wrong for a rate limit. The vendor's rate-limit refusal (its
text carries the code ``2062``) clears itself in seconds — a provider
that answered ``2062`` four times in a row would have been removed from
the pool for half an hour over a refusal a coffee break would have
cleared, and the work that was in flight on it had to be re-sent
elsewhere from the top.

The contract these tests hold in place:

1. **A rate limit is waited out on the SAME provider.** The dispatch
   asks again after 5s, 10s, 20s and 40s — the same session, the same
   provider — and nothing is parked and nothing is excluded while that
   is still working.
2. **A spent plan is not waited out.** ``2056`` and a live meter that
   says the budget is gone park the provider at once and move on: no
   amount of waiting brings a window back sooner than the window does.
3. **The meter outranks the prose.** A ``2062`` refusal next to a
   reading that says the budget is gone is an exhausted provider, not a
   slow one.
4. **The wait is only the first answer.** When the last wait is refused
   too, the provider is parked and the work moves on.
5. **A request-shaped failure is not a provider failure.** A ``400`` is
   the caller's own mistake: no provider and no wait will fix it, so it
   is raised instead of being retried anywhere.
6. **Everything else keeps the old behaviour.** A ``401`` and friends
   still rotate to the next provider, because a bad credential is worth
   one more provider even if it is not worth a wait.
7. **The park is the only ledger.** A provider that is parked is not
   *also* written into the permanent exclusion set. The exclusion set
   does not expire — it rides down the retry chain and is only rebuilt
   when the chain is rebuilt — so a provider written into it would stay
   out of the pool long after its own window had rolled. The next
   dispatch skips it because of the PARK: the candidate collection
   filters a parked provider out, and the exclusion set says nothing
   about it.

Why the wait is on the clock and not on the wall
-------------------------------------------------
The waits are tens of seconds and the suite is not patient, so the
production sleep is a module-level name that a test replaces — the same
seam :func:`coding_tool._quota_wake_sleep` already is. What a test
reads back is the *decision* (which waits, in which order), not the
patience of the suite.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool as ct  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from coding_tool import ApiError, ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    get_shared_cooldown,
)

#: The provider every test fails on. One spelling, so the exclusion set
#: and the cooldown list are both readable as themselves.
PROVIDER = "vendor-a-pro"

#: The provider that can serve the work once ``PROVIDER`` is parked.
OTHER_PROVIDER = "vendor-b-lite"

#: The scene the dispatch is routed through, and the chain it resolves
#: to: the provider that fails, and the one that can take the work.
SCENE = "verification"
CHAIN = [PROVIDER, OTHER_PROVIDER]

#: The vendor's rate-limit refusal — same plan, a different failure.
RATE_LIMIT_TEXT = "已达到 Token Plan 速率限制：请稍后重试。(2062)"

#: The vendor's spent-plan refusal.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)

#: The fake ``claude`` the real launcher runs: one result line and
#: nothing else. No network, no CLI, no session — just the answer.
OK_SCRIPT = (
    "#!/bin/sh\n"
    "echo "
    + shlex.quote(
        json.dumps({"type": "result", "result": "done"}, ensure_ascii=False)
    )
    + "\n"
)


class _Backoff:
    """The waits the dispatch asked for, in order."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []


class _Log:
    """The event logger, kept for the assertions about the collection.

    The dispatch logs why it dropped a candidate, and that reason is
    the difference between "the park kept this provider out" and "the
    exclusion set did" — two books for one fact, and only one of them
    expires.
    """

    def __init__(self) -> None:
        self.events: list = []

    def _record(self, name, message="", data=None):
        self.events.append((name, message, data or {}))
        return self

    warning = info = error = debug = _record

    def names(self):
        return {name for name, _msg, _data in self.events}

    def of(self, name):
        return [data for event, _msg, data in self.events if event == name]


@pytest.fixture(autouse=True)
def backoff(monkeypatch) -> _Backoff:
    """Record the backoff's waits instead of serving them.

    Autouse, so a test that forgets to ask for the recorder still does
    not wait 150 seconds for a decision it is only reading.
    """
    recorded = _Backoff()
    monkeypatch.setattr(ct, "_rate_limit_sleep", recorded.sleeps.append)
    return recorded


@pytest.fixture(autouse=True)
def _clean_cooldown():
    """The cooldown list is process-global; leave it as it was found."""
    cooldown = get_shared_cooldown()
    cooldown.clear()
    yield cooldown
    cooldown.clear()


@pytest.fixture(autouse=True)
def _no_live_meter(monkeypatch):
    """The live usage reading is stubbed out.

    A unit test must not ask CC Switch what a provider's meter says:
    the answer would depend on the machine the test runs on, and the
    meter is not what any of these cases are about. The one test that
    IS about the meter replaces this stub with a reading of its own.
    """
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """The chain the dispatch is routed through, and a ``claude`` to run.

    The launcher picks the provider, and the picker is where the park
    filter lives — so a test that stubs the launcher out never runs it,
    and a test that stubs it out cannot say whether a parked provider
    was skipped. This fixture makes a dispatch REAL: the same chain, the
    same park state, the same collection, and a ``claude`` on the PATH
    that answers without a network.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    monkeypatch.setattr(
        provider_routing,
        "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": f"https://api.{name}.example/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m", "sonnet": "m",
                       "haiku": "m"},
        })),
    )


def _tool(monkeypatch, side_effect, *, calls=None, real_from=None, logger=None):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    ``side_effect(n)`` receives the 1-based dispatch index and returns
    either ``(text, session_id, meta)`` or an exception instance to
    raise. Every dispatch is recorded — prompt positionally, the rest by
    keyword — so a test can assert on what the launcher was asked to
    run.

    ``real_from`` names the first dispatch that goes through the REAL
    launcher instead of the stub. From that dispatch on the provider
    selection runs for real, which is the only way a test can see the
    candidate set the launcher built — and the only way it can see that
    a parked provider was dropped by the park filter rather than by the
    exclusion set.
    """
    tool = ClaudeCodingTool(
        scene=SCENE if real_from is not None else None,
        concurrency_tracker=(
            ActiveConcurrencyTracker() if real_from is not None else None
        ),
    )
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = logger if logger is not None else MagicMock()
    recorded = calls if calls is not None else []
    real_launcher = tool._run_claude_interactive

    def _spy(prompt, *args, **kwargs):
        recorded.append({"prompt": prompt, **kwargs})
        if "provider" not in kwargs:
            # The real launcher stamps the provider it selected; the
            # fallback handlers key off it.
            tool.current_call_provider = PROVIDER
        if real_from is not None and len(recorded) >= real_from:
            # A real dispatch: the launcher picks the provider, and the
            # park filter is what decides whether this one is still a
            # candidate.
            return real_launcher(prompt, *args, **kwargs)
        result = side_effect(len(recorded), tool)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_claude_interactive", _spy)
    tool._calls = recorded
    return tool


def _failing_then(failures, text="done"):
    """A side effect that refuses ``failures`` times, then succeeds."""

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n <= failures:
            return ApiError(RATE_LIMIT_TEXT, status="429")
        return (text, f"sid-{n}", {})

    return _side


# ---------------------------------------------------------------------------
# 1. The rate limit is waited out, on the same provider
# ---------------------------------------------------------------------------


def test_2062_backs_off_four_times_on_the_same_provider(monkeypatch, backoff):
    """Four refusals, four waits, and the work still on the same provider."""
    tool = _tool(monkeypatch, _failing_then(4))
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [5.0, 10.0, 20.0, 40.0], (
        "the waits grow with the number of refusals already spent: a "
        "provider that answers a refusal after 10s is being asked too "
        "fast"
    )
    assert len(tool._calls) == 5, "four refusals and the answer that came"
    assert not get_shared_cooldown().is_parked(PROVIDER), (
        "a provider that is only slow must not be parked — the park is "
        "what removes it from the pool for half an hour"
    )
    assert PROVIDER not in (tool._calls[-1]["excluded_providers"] or []), (
        "the provider is not excluded while the wait is still working"
    )


def test_the_backoff_resumes_the_session_it_was_using(monkeypatch, backoff):
    """The wait is cheap; re-sending the prompt is not."""
    tool = _tool(monkeypatch, _failing_then(2))
    assert tool.query("do the task", timeout=10) == "done"

    first, second, third = tool._calls
    assert second["resume"] is True
    assert third["resume"] is True
    assert second["session_id"] == first["session_id"]
    assert third["session_id"] == first["session_id"], (
        "the conversation is worth keeping across a wait — restarting it "
        "pays for every round already done a second time"
    )


# ---------------------------------------------------------------------------
# 2. A spent plan is not waited out
# ---------------------------------------------------------------------------


def test_2056_parks_immediately_without_backoff(monkeypatch, backoff, scene):
    """``2056`` is a window, and the window decides how long to wait."""
    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(EXHAUSTION_TEXT, status="429")
        raise AssertionError(
            f"dispatch {n} must be the real launcher, not the stub"
        )

    log = _Log()
    tool = _tool(monkeypatch, _side, real_from=2, logger=log)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [], (
        "a spent plan does not clear in 5 seconds — waiting for it "
        "only delays the move to a provider that can serve the work"
    )
    assert get_shared_cooldown().is_parked(PROVIDER)
    assert get_shared_cooldown().remaining(PROVIDER) > 1500, (
        "the quota window is half an hour; a rate limit's short park "
        "would put the provider back in the pool too early"
    )
    assert len(tool._calls) == 2, "the refusal, then the switch"

    # The park is where the refusal is recorded, and the exclusion set
    # is NOT — the exclusion set does not expire, so a provider written
    # into it would stay out of the pool long after its own window had
    # rolled. The cooldown is the only ledger.
    assert ct._provider_is_parked(PROVIDER) is True, (
        "the refusal must be recorded in the cooldown — the park is what "
        "keeps the provider out of the pool"
    )
    assert PROVIDER not in (tool._calls[1]["excluded_providers"] or []), (
        "a parked provider must not ALSO be excluded: the exclusion does "
        "not expire, so it would keep the provider out of the pool long "
        "after its window had rolled"
    )

    # ... and the switch is a REAL dispatch, so the candidate set the
    # launcher built is the one on trial: the parked provider was
    # dropped by the park filter, and the work went to the provider
    # that can serve it.
    assert tool.current_call_provider == OTHER_PROVIDER, (
        f"a parked provider must not be a candidate; the dispatch went to "
        f"{tool.current_call_provider!r}"
    )
    assert "provider_scene_candidate_parked" in log.names(), (
        "the candidate collection did not run, so this test proved "
        "nothing about the park filter"
    )


def test_live_reading_exhausted_overrides_2062_text(monkeypatch, backoff):
    """The meter says the budget is gone; the prose says "slow down".

    The meter is the one that is not a guess.
    """
    monkeypatch.setattr(
        provider_order,
        "load_provider_quota_map",
        lambda *a, **k: {PROVIDER: SimpleNamespace(exhausted=True)},
    )

    tool = _tool(monkeypatch, _failing_then(1))
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [], (
        "a 2062 next to an exhausted meter is an exhausted provider"
    )
    assert get_shared_cooldown().is_parked(PROVIDER)
    assert len(tool._calls) == 2


# ---------------------------------------------------------------------------
# 3. The wait is only the first answer
# ---------------------------------------------------------------------------


def test_backoff_exhaustion_switches_provider_and_parks(
    monkeypatch, backoff, scene,
):
    """When the last wait is refused too, the provider is dropped."""
    log = _Log()
    tool = _tool(monkeypatch, _failing_then(5), real_from=6, logger=log)
    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 6, (
        "four refusals answered by four waits, the fifth refused, and "
        "then the switch that succeeded"
    )
    assert backoff.sleeps == [5.0, 10.0, 20.0, 40.0]
    assert get_shared_cooldown().is_parked(PROVIDER), (
        "a provider that refused five times in a row is parked, so the "
        "next dispatch does not pick it again"
    )

    # The park is the whole record: the exclusion set is not written
    # to, because the exclusion set does not expire and the provider
    # has to come back when its own window rolls.
    assert ct._provider_is_parked(PROVIDER) is True, (
        "the refusal must be recorded in the cooldown — the park is what "
        "keeps the provider out of the pool"
    )
    assert PROVIDER not in (tool._calls[5]["excluded_providers"] or []), (
        "a parked provider must not ALSO be excluded: the exclusion does "
        "not expire, so it would keep the provider out of the pool long "
        "after its window had rolled"
    )

    # The switch is a REAL dispatch: the candidate set the launcher
    # built skipped the provider that was waited out, so the work went
    # to the provider that can serve it.
    assert tool.current_call_provider == OTHER_PROVIDER, (
        f"a parked provider must not be a candidate; the dispatch went to "
        f"{tool.current_call_provider!r}"
    )
    assert "provider_scene_candidate_parked" in log.names(), (
        "the candidate collection did not run, so this test proved "
        "nothing about the park filter"
    )


# ---------------------------------------------------------------------------
# 4. A request-shaped failure is not a provider failure
# ---------------------------------------------------------------------------


def test_fatal_status_raises_without_parking(monkeypatch, backoff):
    """A 400 is the caller's own mistake; no provider can fix it."""

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        return ApiError("bad request", status="400")

    tool = _tool(monkeypatch, _side)
    with pytest.raises(ApiError):
        tool.query("do the task", timeout=10)

    assert len(tool._calls) == 1, "a fatal failure is not retried"
    assert backoff.sleeps == [], "there is nothing to wait out"
    assert not get_shared_cooldown().is_parked(PROVIDER), (
        "the provider did not do anything wrong"
    )


# ---------------------------------------------------------------------------
# 5. Everything else keeps the old behaviour
# ---------------------------------------------------------------------------


def test_an_auth_failure_keeps_the_old_behaviour(monkeypatch, backoff):
    """A bad credential is worth one more provider — but not a wait."""

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError("bad key", status="401")
        return ("done", "sid-2", {})

    tool = _tool(monkeypatch, _side)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [], "a 401 does not clear in 10 seconds"
    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert PROVIDER in (tool._calls[1]["excluded_providers"] or [])
    assert not get_shared_cooldown().is_parked(PROVIDER)
