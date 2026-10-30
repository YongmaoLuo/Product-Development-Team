"""The exclusion ledger remembers WHY a provider was dropped.

What this pins
--------------
The readmission event (``provider_readmitted_after_quota_wait``) has to
report the failure the provider was excluded for. The exclusion set only
ever held the provider's *name*, so the reason was gone by the time the
provider came back: the event could say "Vendor A is back" but not
"Vendor A is back from the 500 that took it out".

The ledger below is that memory. It is written in one place only — the
moment a provider is appended to the exclusion set — and it is read for
diagnosis only: no routing decision reads it, and a provider that is
missing from it is read as ``"unknown"``.

Two rules make the ledger honest:

1. **Only a real exclusion is written down.** A provider that was
   *parked* (a spent plan, a quota window) is not excluded — the
   cooldown is the whole record of it — so it is not in this ledger
   either. Writing it there would report a failure kind for a provider
   the exclusion set never held.
2. **The latest failure wins.** A provider that fails again is written
   again, with the kind that failure was.

What must not move: nothing here reads the ledger. The failure kinds
decide what happens to a provider (park / wait / exclude / give up) and
they keep deciding it exactly as before; the ledger only records what
was already decided.

Nothing here reaches the network: the dispatch is a stub and the quota
map is stubbed empty.
"""

from __future__ import annotations

import json
import sys
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
    ProviderCooldown,
    get_shared_cooldown,
    set_shared_cooldown,
)

#: The provider every test fails on.
PROVIDER = "Vendor A"

#: The vendor's spent-plan refusal — the failure that is a window, and
#: the one that is parked rather than excluded.
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
    """The cooldown list is process-global; leave it as it was found.

    A park left behind by another test would answer "is this provider
    parked?" for this one, and the answer decides whether a provider is
    excluded — which is exactly what the ledger below records.
    """
    cooldown = ProviderCooldown()
    set_shared_cooldown(cooldown)
    yield cooldown
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _no_live_meter(monkeypatch):
    """The live usage reading is stubbed out.

    A unit test must not ask CC Switch what a provider's meter says:
    the answer would depend on the machine the test runs on, and the
    meter is not what any of these cases are about. The read state is
    reset too — the reason of the last read lives in module state that
    :func:`provider_order.cache_clear` does not touch.
    """
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )
    yield
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())


@pytest.fixture(autouse=True)
def _no_rate_limit_wait(monkeypatch):
    """Record the rate-limit waits instead of serving them.

    Autouse, so a case that reaches the backoff branch by accident does
    not spend the suite's patience on it.
    """
    monkeypatch.setattr(ct, "_rate_limit_sleep", lambda seconds: None)


def _tool(monkeypatch, side_effect):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    ``side_effect(n)`` receives the 1-based dispatch index and returns
    either ``(text, session_id, meta)`` or an exception instance to
    raise. The provider label is stamped on every dispatch, so the
    failure has somebody to be attributed to.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded = []

    def _spy(prompt, *args, **kwargs):
        recorded.append({"prompt": prompt, **kwargs})
        tool.current_call_provider = PROVIDER
        result = side_effect(len(recorded))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_claude_interactive", _spy)
    tool._calls = recorded
    return tool


def _fails_once_with(exc):
    """A side effect that refuses once with ``exc`` and then succeeds."""

    def _side(n):
        if n == 1:
            return exc
        return ("done", f"sid-{n}", {})

    return _side


def _fails_once_with_json(exc):
    """The same, for the JSON path — the reply has to be JSON."""

    def _side(n):
        if n == 1:
            return exc
        return (json.dumps(PAYLOAD), f"sid-{n}", {})

    return _side


# ---------------------------------------------------------------------------
# 1. The failure kind of an excluded provider is written down
# ---------------------------------------------------------------------------


def test_failed_provider_kind_is_recorded(monkeypatch):
    """A provider that was excluded is remembered with the reason.

    A 500 is not a capacity failure and not a spent plan: it is the
    ``other`` bucket, the one that drops the provider outright. That is
    the kind the ledger has to hold, because the readmission event
    reports it later — long after the exclusion set itself is gone.
    """
    tool = _tool(
        monkeypatch,
        _fails_once_with(ApiError("transient boom", status="500")),
    )

    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "the failure, then the switch"
    assert tool._excluded_provider_kinds.get(PROVIDER) == "other", (
        "the provider was excluded for a 500, and the ledger has to say "
        f"so; the ledger holds {tool._excluded_provider_kinds!r}"
    )


def test_failed_provider_kind_is_recorded_on_the_json_path(monkeypatch):
    """The JSON path keeps one ledger too.

    ``_query_json_internal`` has its own ``except ApiError`` arm, and it
    used to keep its own copy of the exclusion rule — so the text path
    and the JSON path could disagree about where a failed provider is
    recorded. They answer one question the same way now.
    """
    tool = _tool(
        monkeypatch,
        _fails_once_with_json(ApiError("transient boom", status="500")),
    )

    assert tool.query_json("produce a verdict", timeout=10) == PAYLOAD

    assert len(tool._calls) == 2, "the failure, then the switch"
    assert tool._excluded_provider_kinds.get(PROVIDER) == "other", (
        "the JSON path excluded the provider for a 500 and the ledger "
        f"has to say so; the ledger holds {tool._excluded_provider_kinds!r}"
    )


def test_timed_out_provider_kind_is_recorded(monkeypatch):
    """A provider that timed out is remembered as a timeout.

    A timeout is not one of the four failure kinds — it never went
    through the classifier — but it is still the reason the provider was
    dropped, so the ledger names it as one.
    """

    def _side(n):
        if n == 1:
            return TimeoutError("the provider went quiet")
        return ("done", f"sid-{n}", {})

    tool = _tool(monkeypatch, _side)

    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "the timeout, then the switch"
    assert tool._excluded_provider_kinds.get(PROVIDER) == "timeout", (
        "the provider was excluded for a timeout, and the ledger has to "
        f"say so; the ledger holds {tool._excluded_provider_kinds!r}"
    )


# ---------------------------------------------------------------------------
# 2. What the ledger must not hold
# ---------------------------------------------------------------------------


def test_parked_provider_is_not_in_the_ledger(monkeypatch):
    """A parked provider was never excluded, so it is not in the ledger.

    A spent plan is a window, not an exclusion: the cooldown is the
    whole record of it, and the exclusion set never held the provider.
    The ledger mirrors the exclusion set — one book, one fact — so a
    provider that was parked has no entry in it.
    """
    tool = _tool(
        monkeypatch,
        _fails_once_with(ApiError(EXHAUSTION_TEXT, status="429")),
    )

    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "the refusal, then the switch"
    assert PROVIDER not in (tool._calls[1].get("excluded_providers") or []), (
        "a parked provider is not excluded: the exclusion does not "
        "expire, so it would keep the provider out of the pool long "
        "after its park ran out"
    )
    assert get_shared_cooldown().is_parked(PROVIDER), (
        "the park is where the failure is recorded — dropping the "
        "exclusion must not drop the park with it"
    )
    assert PROVIDER not in tool._excluded_provider_kinds, (
        "the ledger mirrors the exclusion set, and this provider was "
        f"never excluded; the ledger holds {tool._excluded_provider_kinds!r}"
    )


def test_a_provider_that_never_failed_is_not_in_the_ledger(monkeypatch):
    """A healthy provider has no entry — absence is the default."""
    tool = _tool(monkeypatch, lambda n: ("done", f"sid-{n}", {}))

    assert tool.query("do the task", timeout=10) == "done"

    assert tool._excluded_provider_kinds == {}, (
        "nothing failed, so the ledger is empty; a reader that finds no "
        "entry reads the kind as 'unknown'"
    )


def test_the_latest_failure_wins(monkeypatch):
    """A provider that fails again is written again, with the newer kind.

    The ledger holds the LAST failure of an excluded provider, not the
    first: a provider that went out for a 500 and came back only to go
    quiet is a timeout, and reporting the 500 would report a reason that
    is no longer the reason.
    """

    def _side(n):
        if n == 1:
            # The first dispatch: a 500 — excluded, and the ledger says
            # so.
            return ApiError("transient boom", status="500")
        if n == 2:
            # The retry inside the first query: the work moves on.
            return ("done", "sid-1", {})
        if n == 3:
            # A second, independent dispatch: the same provider, this
            # time it goes quiet. The chain is fresh, so the provider is
            # not excluded yet and the timeout arm excludes it again.
            return TimeoutError("the provider went quiet")
        return ("done", "sid-2", {})

    tool = _tool(monkeypatch, _side)

    assert tool.query("do the task", timeout=10) == "done"
    assert tool._excluded_provider_kinds.get(PROVIDER) == "other"

    assert tool.query("do the task again", timeout=10) == "done"

    assert tool._excluded_provider_kinds.get(PROVIDER) == "timeout", (
        "the newer failure is the one that happened last, so it is the "
        f"one the ledger holds; the ledger holds "
        f"{tool._excluded_provider_kinds!r}"
    )
