"""A rate limit is waited out only while there is budget to wait with.

What this pins
--------------
The backoff added on 2026-10-22 waits a rate-limited provider out on
the SAME provider: 5s, 10s, 20s, 40s, and the work is asked again
where it already was. That is right for a provider that is merely
slow, and wrong for a provider whose budget is already gone — a
provider sitting at 0% left, or one whose week has gone missing, is
not slow, it is EMPTY, and four waits on an empty provider buy nothing
but delay the move to a provider that can serve the work.

So the live reading is consulted BEFORE the first wait. The reading is
asked one question only: may this provider be asked again? Three
answers block the retry:

1. the reading says the provider is out of budget, or its weekly
   window went missing on a provider known to carry one
   (:func:`quota_allows_dispatch` says no);
2. the reading says the burst window is empty — ``remaining_pct`` of
   ``0``;
3. the cap that dispatch would honour comes out at nothing
   (:func:`compute_dynamic_limit` returns ``0``).

The reading is allowed to be ABSENT, and absence does not block: CC
Switch cannot meter every provider, and a provider it cannot meter
serves traffic perfectly well. No reading is not evidence of an empty
budget, so a provider with no row keeps the old behaviour exactly.

The contract these tests hold in place:

1. **0% left is not slow.** The dispatch does not wait at all — it
   parks the provider and moves the work on to the next one.
2. **A missing week is a missing week.** A provider whose weekly
   window has gone missing is not waited out either.
3. **A healthy meter keeps the old behaviour.** 85% left is a
   provider worth waiting out, and it is waited out on the same
   provider, exactly as before.

Why the wait is on the clock and not on the wall
-------------------------------------------------
The waits are tens of seconds and the suite is not patient, so the
production sleep is a module-level name that a test replaces — the
same seam :func:`coding_tool._rate_limit_sleep` already is. What a
test reads back is the *decision* (which waits, in which order), not
the patience of the suite.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
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
from provider_order import ProviderQuota  # noqa: E402

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

#: The answer every successful JSON dispatch returns.
PAYLOAD = {"verdict": "pass"}

#: The fake ``claude`` the real launcher runs: one result line and
#: nothing else. No network, no CLI, no session — just the answer.
def _ok_script(result: str) -> str:
    """A ``claude`` that answers every dispatch with ``result``."""
    return (
        "#!/bin/sh\n"
        "echo "
        + shlex.quote(
            json.dumps({"type": "result", "result": result}, ensure_ascii=False)
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
    meter is not what any of these cases are about. The tests that ARE
    about the meter replace this stub with a reading of their own.
    """
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )


def _install_scene(tmp_path, monkeypatch, result: str) -> None:
    """Put a fake ``claude`` and a two-provider chain on the PATH.

    The launcher picks the provider, and the picker is where the park
    filter lives — so a test that stubs the launcher out never runs it,
    and a test that stubs it out cannot say whether a parked provider
    was skipped. This makes a dispatch REAL: the same chain, the same
    park state, the same collection, and a ``claude`` on the PATH that
    answers without a network.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(_ok_script(result), encoding="utf-8")
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


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """The chain the dispatch is routed through, and a ``claude`` to run."""
    _install_scene(tmp_path, monkeypatch, "done")


@pytest.fixture
def json_scene(tmp_path, monkeypatch):
    """The same, for the JSON path: the answer has to be JSON."""
    _install_scene(tmp_path, monkeypatch, json.dumps(PAYLOAD))


def _reading(monkeypatch, quota):
    """Make the live reading for ``PROVIDER`` be ``quota``.

    The stub replaces the whole map, so every OTHER provider keeps the
    "no reading" answer — which is the fail-open half of the contract.
    """
    monkeypatch.setattr(
        provider_order,
        "load_provider_quota_map",
        lambda *a, **k: {PROVIDER: quota},
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


def _refuse_once_then_succeed():
    """A side effect that refuses once, then succeeds."""

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(RATE_LIMIT_TEXT, status="429")
        raise AssertionError(
            f"dispatch {n} must be the real launcher, not the stub"
        )

    return _side


# ---------------------------------------------------------------------------
# 0. The helper's own contract
# ---------------------------------------------------------------------------


def test_the_precheck_only_blocks_on_an_affirmative_reading(monkeypatch):
    """The pre-check fails open — a missing reading is not an empty one.

    This is the boundary the whole contract rests on: CC Switch cannot
    meter every provider, and a provider it cannot meter serves
    traffic perfectly well. Only a reading that says the budget is gone
    may stop a retry.
    """
    # No row at all.
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )
    assert ct._retry_blocked_by_quota(PROVIDER) is False, (
        "a provider with no reading must not be blocked — absence of "
        "evidence is not evidence of an empty budget"
    )

    # A row that exists but says nothing about the window.
    _reading(monkeypatch, ProviderQuota(provider=PROVIDER))
    assert ct._retry_blocked_by_quota(PROVIDER) is False, (
        "a reading that carries no number is not a reading that says zero"
    )

    # A row that says the window is empty.
    _reading(
        monkeypatch,
        ProviderQuota(provider=PROVIDER, remaining_pct=0.0),
    )
    assert ct._retry_blocked_by_quota(PROVIDER) is True, (
        "a reading of 0% is an affirmative one: the provider is empty"
    )

    # A row that says the provider is spent outright.
    _reading(
        monkeypatch,
        ProviderQuota(provider=PROVIDER, exhausted=True),
    )
    assert ct._retry_blocked_by_quota(PROVIDER) is True, (
        "an exhausted reading blocks the retry whatever the window says"
    )


# ---------------------------------------------------------------------------
# 1. 0% left is not slow
# ---------------------------------------------------------------------------


def test_zero_pct_blocks_backoff_and_switches(monkeypatch, backoff, scene):
    """A provider with nothing left is not waited out — it is dropped.

    Four waits on a provider whose meter says 0% would buy nothing: the
    budget they wait for is not going to grow, and every one of them
    delays the move to a provider that can serve the work.
    """
    _reading(
        monkeypatch,
        ProviderQuota(provider=PROVIDER, remaining_pct=0.0),
    )

    log = _Log()
    tool = _tool(monkeypatch, _refuse_once_then_succeed(), real_from=2,
                 logger=log)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [], (
        "a provider whose budget is already gone is not a provider that "
        "is slow — waiting it out delays the switch and buys nothing"
    )
    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert tool.current_call_provider == OTHER_PROVIDER, (
        f"the work must move to a provider that can serve it; it went to "
        f"{tool.current_call_provider!r}"
    )
    assert "provider_retry_blocked_by_quota" in log.names(), (
        "the block must be recorded: a switch that looks like an "
        "ordinary timeout is indistinguishable from a hang"
    )
    blocked = log.of("provider_retry_blocked_by_quota")
    assert blocked and blocked[0].get("from_provider") == PROVIDER, (
        "the record must name the provider that was not waited out"
    )


# ---------------------------------------------------------------------------
# 2. A missing week is a missing week
# ---------------------------------------------------------------------------


def test_weekly_gated_provider_is_not_retried(monkeypatch, backoff, scene):
    """A week that went missing is a week that cannot be dispatched to.

    The burst window still says 85%, but the week is the budget that
    gates the dispatch, and a week that stopped being reported is a
    week that cannot be asked against.
    """
    _reading(
        monkeypatch,
        ProviderQuota(
            provider=PROVIDER,
            remaining_pct=85.0,
            weekly_tier_seen=True,
            tiers_missing=("weekly_limit",),
        ),
    )

    log = _Log()
    tool = _tool(monkeypatch, _refuse_once_then_succeed(), real_from=2,
                 logger=log)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [], (
        "a provider whose week is gone is not a provider that is slow"
    )
    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert tool.current_call_provider == OTHER_PROVIDER, (
        f"the work must move to a provider that can serve it; it went to "
        f"{tool.current_call_provider!r}"
    )
    assert "provider_retry_blocked_by_quota" in log.names(), (
        "the block must be recorded"
    )


# ---------------------------------------------------------------------------
# 3. A healthy meter keeps the old behaviour
# ---------------------------------------------------------------------------


def test_healthy_meter_still_retries(monkeypatch, backoff):
    """85% left is a provider worth waiting out.

    The pre-check must not turn every rate limit into a switch: the
    whole point of the backoff is that a healthy provider gets asked
    again on the same provider.
    """
    _reading(
        monkeypatch,
        ProviderQuota(provider=PROVIDER, remaining_pct=85.0),
    )

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(RATE_LIMIT_TEXT, status="429")
        return ("done", f"sid-{n}", {})

    tool = _tool(monkeypatch, _side)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [5.0], (
        "a provider with budget left is waited out — the reading must not "
        "turn every rate limit into a switch"
    )
    assert len(tool._calls) == 2, "the refusal, then the retry"
    assert PROVIDER not in (tool._calls[1]["excluded_providers"] or []), (
        "the retry went back to the same provider: it was not dropped"
    )
    assert tool._calls[1]["resume"] is True, (
        "the retry is a resume of the session that was refused, not a "
        "fresh dispatch of the whole prompt"
    )


# ---------------------------------------------------------------------------
# 4. The JSON path answers the same way
# ---------------------------------------------------------------------------


def test_json_zero_pct_blocks_backoff_and_switches(
    monkeypatch, backoff, json_scene,
):
    """The JSON dispatch path gets the same pre-check.

    Both paths carry their own backoff loop, and a pre-check on one of
    them alone would leave the other spending four waits on a provider
    with nothing left.
    """
    _reading(
        monkeypatch,
        ProviderQuota(provider=PROVIDER, remaining_pct=0.0),
    )

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(RATE_LIMIT_TEXT, status="429")
        raise AssertionError(
            f"dispatch {n} must be the real launcher, not the stub"
        )

    log = _Log()
    tool = _tool(monkeypatch, _side, real_from=2, logger=log)
    assert tool.query_json("produce a verdict", timeout=10) == PAYLOAD

    assert backoff.sleeps == [], (
        "a provider whose budget is already gone is not a provider that "
        "is slow — waiting it out delays the switch and buys nothing"
    )
    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert tool.current_call_provider == OTHER_PROVIDER, (
        f"the work must move to a provider that can serve it; it went to "
        f"{tool.current_call_provider!r}"
    )
    assert "provider_retry_blocked_by_quota" in log.names(), (
        "the block must be recorded"
    )
