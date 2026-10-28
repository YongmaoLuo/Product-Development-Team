"""A 429 that names the plan's usage is a spent plan, not a slow provider.

What this pins
--------------
The exhaustion markers hold the phrase for a usage cap (``用量上限``)
and the code the vendor puts next to it (``2056``), but not the other
wording the same vendor uses for the same fact: a refusal that says the
plan's usage is used up (``plan 用量``). That text carries no code the
markers knew about, so it fell through the text check, landed on the
status check, and was classified ``rate_limited`` — a refusal that
clears itself in seconds.

The consequence was not a cosmetic mislabel. A rate-limited provider
is *waited out* on the same provider (10s / 20s / 40s / 80s) and is
never parked; an exhausted provider is parked for the quota window and
the work moves on. So a spent plan was asked four more times — each
wait burning the dispatch's budget on a refusal that was never going to
clear — and only then thrown away, and even then for the short rate
limit park instead of the window it actually needed.

The rule the markers encode is the one the vendor's own two refusals
prove: the wording next to the plan name is what separates a spent plan
from a rate limit, and the plan name itself cannot. So a wording that
says the plan's usage is gone is a spent plan.

The contract these tests hold in place:

1. **The wording is the verdict.** ``plan 用量`` classifies as
   ``exhausted`` on a 429.
2. **The rate-limit wording is untouched.** A text carrying the code
   ``2062`` still classifies as ``rate_limited``, so the new marker did
   not widen the exhaustion set into the rate-limit set.
3. **The marker reaches the dispatch.** The classifier the coding tool
   uses sees the same answer, so the call site cannot disagree with the
   function.
4. **The consequence is a park, not a wait.** The provider is parked
   for the quota window and the work lands on the next provider — the
   whole point of classifying the refusal correctly.

Why the wait is not on the wall
-------------------------------
The waits are tens of seconds and the suite is not patient, so the
production sleep is a module-level name that a test replaces — the same
seam the existing 429 backoff tests use. What a test reads back is the
*decision* (parked or not, and where the work went), not the patience
of the suite.
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
    classify_capacity_failure,
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

#: The refusal that names the plan's usage. No code, no ``用量上限`` —
#: only the wording.
PLAN_USAGE_TEXT = "你的 plan 用量已用尽，请稍后再试"

#: The vendor's rate-limit refusal — the same plan, a different failure.
RATE_LIMIT_TEXT = "已达到 Token Plan 速率限制：请稍后重试。(2062)"

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


@pytest.fixture(autouse=True)
def backoff(monkeypatch):
    """Record the backoff's waits instead of serving them.

    Autouse, so a test that forgets to ask for the recorder still does
    not wait 150 seconds for a decision it is only reading.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(ct, "_rate_limit_sleep", sleeps.append)
    return sleeps


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


def _tool(monkeypatch, side_effect, *, real_from=None, logger=None):
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
    recorded: list = []
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


# ---------------------------------------------------------------------------
# 1. The wording is the verdict
# ---------------------------------------------------------------------------


def test_plan_usage_wording_is_exhausted():
    """The refusal names the plan's usage, so the budget is gone.

    Rule 1 of the upstream decision: the wording is the verdict. No
    code, no reading, no meter — the text alone says the plan is used
    up.
    """
    assert classify_capacity_failure(PLAN_USAGE_TEXT, "429") == "exhausted"


# ---------------------------------------------------------------------------
# 2. The rate-limit wording is untouched
# ---------------------------------------------------------------------------


def test_rate_limit_wording_still_rate_limited():
    """The other refusal of the same plan is still a rate limit.

    The new marker must not swallow the vendor's rate-limit refusal:
    that one clears itself in seconds, and treating it as a spent plan
    parks a healthy provider for half an hour.
    """
    assert classify_capacity_failure(RATE_LIMIT_TEXT, "429") == "rate_limited"


# ---------------------------------------------------------------------------
# 3. The marker reaches the dispatch
# ---------------------------------------------------------------------------


def test_marker_hits_through_coding_tool_classifier():
    """The classifier the coding tool uses sees the same answer.

    The call site folds the live reading in and then delegates; a
    disagreement between the two would put the dispatch back on the
    rate-limit path for a refusal the marker already covers.
    """
    exc = ApiError(PLAN_USAGE_TEXT, status="429")
    assert ct._classify_provider_failure(exc, PROVIDER) == "exhausted"


# ---------------------------------------------------------------------------
# 4. The consequence is a park, not a wait
# ---------------------------------------------------------------------------


def test_usage_wording_parks_provider(monkeypatch, backoff, scene):
    """The provider is parked and the work moves on.

    The classification is only worth anything if it changes what
    happens: a spent plan is parked for the quota window and the work
    goes to the next provider, while a rate limit would have been
    waited out four times on the same provider first.
    """
    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(PLAN_USAGE_TEXT, status="429")
        raise AssertionError(
            f"dispatch {n} must be the real launcher, not the stub"
        )

    log = _Log()
    tool = _tool(monkeypatch, _side, real_from=2, logger=log)
    assert tool.query("do the task", timeout=10) == "done"

    assert backoff == [], (
        "a spent plan does not clear in 10 seconds — waiting for it "
        "only delays the move to a provider that can serve the work"
    )
    assert get_shared_cooldown().is_parked(PROVIDER), (
        "the refusal must be recorded in the cooldown — the park is what "
        "keeps the provider out of the pool"
    )
    assert get_shared_cooldown().remaining(PROVIDER) > 1500, (
        "the quota window is half an hour; a rate limit's short park "
        "would put the provider back in the pool too early"
    )
    assert len(tool._calls) == 2, "the refusal, then the switch"

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
