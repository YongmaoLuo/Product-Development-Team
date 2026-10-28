"""The chain comes first: one slow provider must not hold the work.

What this pins
--------------
A rate-limited provider (the vendor's ``2062`` refusal) is waited out
on the SAME provider before the dispatch gives up on it — but the wait is
deliberately SHORT. The window is ``5s / 10s / 20s / 40s``: 75 seconds
in total, not the 150 seconds the old ``10 / 20 / 40 / 80`` ladder cost.

The reason is the incident that produced the shorter ladder: a sub-agent
sat on one provider's refusal for 38 minutes while a healthy provider
sat idle in the chain behind it. A refusal that clears itself in seconds
is worth a short wait; a wait measured in minutes is just a slow way of
not trying the next provider.

The contract these tests hold in place:

1. **The window is 5 / 10 / 20 / 40.** One constant, shared by both
   dispatch paths (the text path and the JSON path), so the two cannot
   drift apart and one of them keep the long ladder.
2. **A rate limit is waited out, then the chain moves on.** Four refusals
   produce exactly the four waits — ``5, 10, 20, 40`` — and the provider
   is neither parked nor excluded while the wait is still working.
3. **A spent plan is not waited out at all.** ``2056`` is a window, and
   the window decides how long to wait: the provider is parked and the
   dispatch switches to the next provider in the chain immediately, with
   no wait spent on it.

Why the wait is on the clock and not on the wall
-------------------------------------------------
The waits are seconds and the suite is not patient, so the production
sleep is a module-level name that a test replaces — the same seam
:func:`coding_tool._rate_limit_sleep` already is. What a test reads back
is the *decision* (which waits, in which order), not the patience of the
suite.
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

#: The provider every test fails on. One spelling, so the exclusion set
#: and the cooldown list are both readable as themselves.
PROVIDER = "vendor-a-pro"

#: The provider that can serve the work once ``PROVIDER`` is parked.
OTHER_PROVIDER = "vendor-b-lite"

#: The scene the dispatch is routed through, and the chain it resolves
#: to: the provider that fails, and the one that can take the work.
SCENE = "verification"
CHAIN = [PROVIDER, OTHER_PROVIDER]

#: The vendor's rate-limit refusal — slow down, ask again shortly.
RATE_LIMIT_TEXT = "已达到 Token Plan 速率限制：请稍后重试。(2062)"

#: The vendor's spent-plan refusal — the budget is gone.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)

#: The waits the dispatch must use: short enough that the chain is
#: reached in a minute, long enough that a provider answering after a
#: few seconds is not asked again immediately.
EXPECTED_WINDOWS = (5.0, 10.0, 20.0, 40.0)

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


@pytest.fixture(autouse=True)
def backoff(monkeypatch) -> _Backoff:
    """Record the backoff's waits instead of serving them.

    Autouse, so a test that forgets to ask for the recorder still does
    not wait for a decision it is only reading.
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
    meter is not what any of these cases are about.
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
    was skipped. This fixture makes a dispatch REAL: the same chain,
    the same park state, the same collection, and a ``claude`` on the
    PATH that answers without a network.
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
# 1. The window itself
# ---------------------------------------------------------------------------


def test_backoff_constant_is_five_ten_twenty_forty():
    """One constant, and it is the short one.

    A single provider must not be able to hold the chain for minutes:
    5 / 10 / 20 / 40 is 75 seconds in total, where the old
    10 / 20 / 40 / 80 ladder was 150.
    """
    assert ClaudeCodingTool.RATE_LIMIT_BACKOFF_SEC == (5.0, 10.0, 20.0, 40.0)
    assert tuple(ClaudeCodingTool.RATE_LIMIT_BACKOFF_SEC) == EXPECTED_WINDOWS


# ---------------------------------------------------------------------------
# 2. A rate limit is waited out — briefly — on the same provider
# ---------------------------------------------------------------------------


def test_rate_limit_backs_off_five_ten_twenty_forty(monkeypatch, backoff):
    """Four refusals, four waits: 5s, 10s, 20s, 40s."""
    tool = _tool(monkeypatch, _failing_then(4))
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [5.0, 10.0, 20.0, 40.0], (
        "the waits are the short ladder: a refusal that clears itself in "
        "seconds must not cost the chain minutes of waiting"
    )
    assert len(tool._calls) == 5, "four refusals and the answer that came"
    assert not get_shared_cooldown().is_parked(PROVIDER), (
        "a provider that is only slow must not be parked — the park is "
        "what removes it from the pool for half an hour"
    )
    assert PROVIDER not in (tool._calls[-1]["excluded_providers"] or []), (
        "the provider is not excluded while the wait is still working"
    )


# ---------------------------------------------------------------------------
# 3. A spent plan is not waited out at all
# ---------------------------------------------------------------------------


def test_exhausted_switches_without_backoff(monkeypatch, backoff, scene):
    """``2056`` is a window, and the window decides how long to wait."""
    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(EXHAUSTION_TEXT, status="429")
        raise AssertionError(
            f"dispatch {n} must be the real launcher, not the stub"
        )

    tool = _tool(monkeypatch, _side, real_from=2)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff.sleeps == [], (
        "a spent plan does not clear in 5 seconds — waiting for it only "
        "delays the move to a provider that can serve the work"
    )
    assert get_shared_cooldown().is_parked(PROVIDER), (
        "the refused provider is parked, so the next dispatch does not "
        "pick it again"
    )
    assert len(tool._calls) == 2, "the refusal, then the switch"

    # The switch is a REAL dispatch, so the candidate set the launcher
    # built is the one on trial: the parked provider was dropped by the
    # park filter, and the work went to the provider that can serve it.
    assert tool.current_call_provider == OTHER_PROVIDER, (
        f"a parked provider must not be a candidate; the dispatch went to "
        f"{tool.current_call_provider!r}"
    )
