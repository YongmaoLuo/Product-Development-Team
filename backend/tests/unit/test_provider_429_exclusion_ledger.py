"""One ledger for "this provider is not to be used right now".

What this pins
--------------
The provider fallback used to keep two books for one fact. A provider
that answered "my budget is gone" was **parked** — a cooldown entry with
an expiry of its own — and was *also* **excluded**, and the exclusion
was the book that traveled: ``new_excluded`` rides down the retry chain
and is only rebuilt when the chain is rebuilt, so a provider written
into it stayed out of the candidate set long after its park had run
out. The scene-candidate walk reads ``excluded`` first, so a provider
whose half-hour cooldown had already expired was still skipped: it
never came back.

The contract these tests hold in place:

1. **The park is the accounting.** A provider that is parked is out of
   the pool, and the cooldown's own expiry is what puts it back. It is
   *not* also written into the exclusion set — the two are the same
   fact written down twice, and only one of them can expire.
2. **A failure that parked nothing still rotates.** The exclusion set
   is still how a provider is kept out of *this* retry. Only the parked
   case changes, and the change is decided by what actually happened
   (a park that is in the cooldown) rather than by what the parking
   call reported (a number it returns).
3. **The priority walk reads the park too.** A provider that is parked
   is skipped by the ``provider_priority`` walk, the same way the
   scene-candidate walk skips it, so the two paths answer one question
   the same way.
4. **The park expires, and the provider comes back.** Because the
   exclusion set no longer holds the provider, the cooldown's own
   expiry is the whole story: when the window rolls, the provider is
   offered to the walk again.

Why the order matters
----------------------
The park has to be taken *before* the decision is made. The question
"is this provider parked?" is answered by the cooldown, not by the
number the parking call returned — so a decision taken first would read
a cooldown that does not have the entry yet and answer "no" every time.
A test that replaces the parking call with one that only returns a
number pins exactly that: nothing was parked, so the provider is still
excluded, and the old behaviour survives for the case where it should.

No test here reaches the network: the dispatch is a stub, and the one
case that runs the real launcher gets a fake ``claude`` on the PATH.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool as ct  # noqa: E402
import provider_order  # noqa: E402
from coding_tool import ApiError, ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    get_shared_cooldown,
    set_shared_cooldown,
)
from tests.unit.test_provider_zero_cap_window_wait import (  # noqa: E402
    _install_fake_claude,
)

#: The provider every test fails on. One spelling, so the exclusion set
#: and the cooldown list are both readable as themselves.
PROVIDER = "vendor-a-pro"

#: The vendor's spent-plan refusal — the failure that is a window, and
#: the one that is parked.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)

#: The answer every successful JSON dispatch returns.
PAYLOAD = {"verdict": "pass"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    """The cooldown list is process-global; leave it as it was found."""
    cooldown = ProviderCooldown()
    set_shared_cooldown(cooldown)
    yield cooldown
    set_shared_cooldown(None)


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


@pytest.fixture(autouse=True)
def _no_rate_limit_wait(monkeypatch):
    """Record the rate-limit waits instead of serving them.

    Autouse, so a case that reaches the backoff branch by accident does
    not spend the suite's patience on it.
    """
    monkeypatch.setattr(ct, "_rate_limit_sleep", lambda seconds: None)


def _tool(monkeypatch, side_effect, *, calls=None):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    ``side_effect(n)`` receives the 1-based dispatch index and returns
    either ``(text, session_id, meta)`` or an exception instance to
    raise. Every dispatch is recorded, so a test can assert on the
    exclusion set the *retry* carried.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded = calls if calls is not None else []

    def _spy(prompt, *args, **kwargs):
        recorded.append({"prompt": prompt, **kwargs})
        if "provider" not in kwargs:
            tool.current_call_provider = PROVIDER
        result = side_effect(len(recorded), tool)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_claude_interactive", _spy)
    tool._calls = recorded
    return tool


def _exhausted_then_ok():
    """A side effect that refuses once with a spent-plan refusal."""

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(EXHAUSTION_TEXT, status="429")
        return ("done", f"sid-{n}", {})

    return _side


def _exhausted_then_ok_json():
    """The same, for the JSON path."""

    def _side(n, tool):
        tool.current_call_provider = PROVIDER
        if n == 1:
            return ApiError(EXHAUSTION_TEXT, status="429")
        return (json.dumps(PAYLOAD), f"sid-{n}", {})

    return _side


# ---------------------------------------------------------------------------
# 1. The park is the accounting
# ---------------------------------------------------------------------------


def test_parked_provider_is_not_added_to_the_exclusion_set(monkeypatch):
    """A parked provider is out of the pool on the cooldown's clock alone.

    The retry still happens — the work moves on to a provider that can
    serve it — but the failed provider is not written into the set that
    travels down the retry chain. The cooldown entry is the whole
    record of it, and that entry has an expiry.
    """
    tool = _tool(monkeypatch, _exhausted_then_ok())

    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert PROVIDER not in (tool._calls[1].get("excluded_providers") or []), (
        "a provider that is parked must not also be excluded: the "
        "exclusion does not expire, so it would keep the provider out "
        "of the pool long after its park ran out"
    )
    assert get_shared_cooldown().is_parked(PROVIDER), (
        "the park is where the failure is recorded — dropping the "
        "exclusion must not drop the park with it"
    )


def test_parked_provider_is_not_added_to_the_exclusion_set_on_the_json_path(
    monkeypatch,
):
    """The JSON path keeps one ledger too.

    ``_query_json_internal`` has its own ``except ApiError`` arm, and it
    used to keep its own copy of the exclusion rule — so the text path
    and the JSON path could disagree about where a failed provider is
    recorded. They answer one question the same way now.
    """
    tool = _tool(monkeypatch, _exhausted_then_ok_json())

    assert tool.query_json("produce a verdict", timeout=10) == PAYLOAD

    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert PROVIDER not in (tool._calls[1].get("excluded_providers") or []), (
        "a provider that is parked must not also be excluded: the "
        "exclusion does not expire, so it would keep the provider out "
        "of the pool long after its park ran out"
    )
    assert get_shared_cooldown().is_parked(PROVIDER), (
        "the park is where the failure is recorded — dropping the "
        "exclusion must not drop the park with it"
    )


# ---------------------------------------------------------------------------
# 2. A failure that parked nothing still rotates
# ---------------------------------------------------------------------------


def test_unparked_failure_is_still_excluded(monkeypatch):
    """Nothing was parked, so the exclusion set is still what rotates.

    The decision is made from the cooldown's actual state, not from the
    number the parking call returned. A parking call that only returns
    a number — a test double, a cooldown module that is not there —
    parks nothing, and a provider that was not parked has to be excluded
    by hand or the retry would ask the same provider again.
    """
    monkeypatch.setattr(
        ct, "_park_provider_after_error", lambda provider, exc: 0.0,
    )
    tool = _tool(monkeypatch, _exhausted_then_ok())

    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert PROVIDER in (tool._calls[1].get("excluded_providers") or []), (
        "a provider that was not parked is kept out of this retry by "
        "the exclusion set, exactly as before"
    )
    assert not get_shared_cooldown().is_parked(PROVIDER)


# ---------------------------------------------------------------------------
# 3. The priority walk reads the park too
# ---------------------------------------------------------------------------


def test_provider_priority_walk_skips_a_parked_provider(tmp_path, monkeypatch):
    """The scene-less walk answers the same question the same way.

    Without a scene the walk goes down ``provider_priority`` instead of
    the scene chain, and it used to read only the exclusion set — so a
    provider that was parked was still picked, and the cooldown entry
    was ignored by half the dispatch paths.
    """
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.delenv("PDT_PROVIDER_PRIORITY", raising=False)
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m", "sonnet": "m",
                       "haiku": "m"},
        })),
    )
    # No CC Switch row and no ambient environment: the walk must be the
    # only thing that can select a provider.
    monkeypatch.setattr(
        ClaudeCodingTool, "_load_provider_from_db",
        staticmethod(lambda provider_name=None: None),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    )

    get_shared_cooldown().park(
        "Vendor A Pro", seconds=600, reason="spent plan",
    )

    tool = ClaudeCodingTool(
        provider_priority=["Vendor A Pro", "Vendor A"],
        concurrency_tracker=ActiveConcurrencyTracker(),
    )
    result, _session, _meta = tool._run_claude_interactive(
        "hi", idle_timeout=30, total_timeout=30,
    )

    assert result == "ok"
    assert tool.current_call_provider == "Vendor A", (
        "the parked provider is out of the pool until its cooldown "
        f"expires; the walk picked {tool.current_call_provider!r}"
    )


# ---------------------------------------------------------------------------
# 4. The park expires, and the provider comes back
# ---------------------------------------------------------------------------


def test_a_parked_provider_is_offered_again_when_its_cooldown_expires(
    tmp_path, monkeypatch,
):
    """The point of the one-ledger rule: the provider comes back.

    A provider that was parked is not excluded, so nothing keeps it out
    of the pool once the cooldown runs out — the park's own expiry is
    the whole story. Before the change the exclusion set held it as
    well, and the exclusion set does not expire: the provider was
    written out of the pool for good.
    """
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.delenv("PDT_PROVIDER_PRIORITY", raising=False)
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
        ClaudeCodingTool, "_load_provider_from_db",
        staticmethod(lambda provider_name=None: None),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    )

    cooldown = get_shared_cooldown()
    cooldown.park("Vendor A Pro", seconds=0.05, reason="spent plan")

    tool = ClaudeCodingTool(
        provider_priority=["Vendor A Pro", "Vendor A"],
        concurrency_tracker=ActiveConcurrencyTracker(),
    )
    tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)
    assert tool.current_call_provider == "Vendor A", (
        "the parked provider is out of the pool while the park holds"
    )

    time.sleep(0.2)
    assert not cooldown.is_parked("Vendor A Pro"), (
        "the park expired on its own — that is the whole reason the "
        "exclusion set must not have kept the provider out as well"
    )

    tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)
    assert tool.current_call_provider == "Vendor A Pro", (
        "a provider whose cooldown expired is offered to the walk again"
    )
