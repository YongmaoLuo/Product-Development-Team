"""A fleet that is entirely at a hard cap escalates instead of spinning.

The wait under the closed-window probe is unbounded by design: a quota
window is somebody else's meter on their own clock, and no
``resets_at`` was ever going to name the moment a plan upgrade brought
the budget back. That is right for a window and wrong for a hard cap.

A hard cap is a balance that was *paid for* and is not coming back on
any timer. Probing it every five minutes for the rest of the run buys
nothing — the answer cannot change until a person tops the balance up.
So when every provider the fleet is blocked on reads as a hard cap, the
dispatch stops waiting and says so:
:class:`capacity_wait.QuotaWaitExhausted`, which the agent turns into a
``waiting_for_quota`` marker instead of a failed task.

Two things have to hold for that to be right:

  * the verdict is *all-or-nothing*. One provider on a window among the
    blocked ones is a reason to keep waiting, and a provider whose
    reading is missing is not evidence of anything — "we cannot tell" is
    not "somebody has to act";
  * the escalation must reach the caller. It used to be swallowed by the
    resume fallback, which read it as "the resume failed" and answered
    with a fresh session and the whole prompt re-sent — a second
    dispatch, to a provider that had already said it has no budget.

And one thing has to hold *only* in the execution scene: the other
phases (PRD, architecture, refine) have nobody to receive a
``waiting_for_quota`` marker, so raising there turns a parked plan into
an unhandled exception. Their hard caps stay visible through the
``provider_quota_manual_topup_required`` log line instead.

No test here reaches the network: the readings are the shapes the parser
is documented against, and the reader that fetches them is stubbed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool as ct  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from capacity_wait import QuotaWaitExhausted  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import ActiveConcurrencyTracker  # noqa: E402
from provider_order import ProviderQuota  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


def _hard_cap(provider):
    """A provider whose balance is spent — no timer brings it back.

    The tier is a currency rather than one of the gated windows, which
    is exactly how a balance tier reads: a person has to top it up.
    """
    return ProviderQuota(
        provider=provider,
        shape=provider_order.SHAPE_BALANCE,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="CNY",
        exhausted_resets_at=None,
        unreadable=False,
    )


def _window(provider, *, minutes=30):
    """A provider whose 5h window is spent — the timer brings it back."""
    return ProviderQuota(
        provider=provider,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="five_hour",
        exhausted_resets_at=_now() + timedelta(minutes=minutes),
        unreadable=False,
    )


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_all_blocked_hard_cap_escalates():
    """Every blocked provider is a hard cap → escalate, do not keep waiting.

    A hard cap does not come back on a timer, so the wait under the
    closed-window probe would run for the rest of the dispatch and
    could not end in anything. The escalation is what ends it: the
    agent turns it into a ``waiting_for_quota`` marker and the task
    stays alive.
    """
    quota_map = {"MiniMax": _hard_cap("MiniMax")}

    with pytest.raises(QuotaWaitExhausted) as caught:
        ct._raise_if_all_blocked_are_manual_topup(["MiniMax"], quota_map)

    exc = caught.value
    assert exc.providers == ["MiniMax"], exc.providers
    assert exc.needs_manual_topup is True, exc.needs_manual_topup
    assert exc.waited_sec == 0.0, exc.waited_sec


def test_mixed_window_and_cap_keeps_waiting():
    """One window among the blocked ones is a reason to keep waiting.

    The escalation is all-or-nothing on purpose. A provider whose
    five-hour window is spent is going to come back on its own; paging
    somebody to top up a balance because a *different* provider in the
    same fleet is waiting on a window would be asking for the wrong
    thing.
    """
    quota_map = {
        "MiniMax": _hard_cap("MiniMax"),
        "VendorB": _window("VendorB"),
    }

    # No exception: the wait continues.
    ct._raise_if_all_blocked_are_manual_topup(["MiniMax", "VendorB"], quota_map)


def test_no_blocked_does_not_raise():
    """Nothing is blocked → there is nothing to escalate."""
    quota_map = {"MiniMax": _hard_cap("MiniMax")}

    ct._raise_if_all_blocked_are_manual_topup([], quota_map)


def test_reading_missing_keeps_waiting():
    """A missing reading is not evidence of a hard cap.

    Two ways for the reading to be missing, and both mean the same
    thing here: the provider is not in the map at all, and the provider
    is in it but the meter came back unreadable. "We cannot tell" is
    not "somebody has to act" — a verdict that claimed a top-up on a
    missing reading would page somebody for a rate limit.
    """
    # Absent from the map entirely.
    ct._raise_if_all_blocked_are_manual_topup(["MiniMax"], {})

    # Present, but the meter could not be read.
    unreadable = ProviderQuota(
        provider="MiniMax",
        shape=provider_order.SHAPE_UNREADABLE,
        remaining_pct=None,
        exhausted=False,
        exhausted_tier=None,
        exhausted_resets_at=None,
        unreadable=True,
    )
    ct._raise_if_all_blocked_are_manual_topup(
        ["MiniMax"], {"MiniMax": unreadable},
    )


def test_non_execution_scene_keeps_waiting():
    """Only the execution scene escalates.

    PRD, architecture and refine have nobody to receive a
    ``waiting_for_quota`` marker, so an exception there is an unhandled
    one. Their hard caps stay visible through the
    ``provider_quota_manual_topup_required`` log line instead.
    """
    quota_map = {"MiniMax": _hard_cap("MiniMax")}

    ct._raise_if_all_blocked_are_manual_topup(
        ["MiniMax"], quota_map, scene="prd",
    )


# ---------------------------------------------------------------------------
# The escalation survives the resume fallback
# ---------------------------------------------------------------------------


def test_escalation_is_not_swallowed_by_resume_fallback(monkeypatch):
    """The hard cap reaches the caller instead of becoming a retry.

    ``_resume_or_fresh_session`` answers "the resume failed" with a
    fresh session and the whole prompt re-sent. An exhausted wait is not
    a failed resume: it is the final answer, and re-sending the prompt
    to a provider that has already said it has no budget answers a
    question nobody asked.
    """
    tool = ClaudeCodingTool()
    tool.logger = MagicMock()
    tool.current_call_provider = "MiniMax"
    escalated = QuotaWaitExhausted(
        providers=["MiniMax"], needs_manual_topup=True, waited_sec=0.0,
    )
    attempts: list = []

    def _fake_resilient(*args, **kwargs):
        attempts.append(kwargs.get("resume"))
        raise escalated

    monkeypatch.setattr(tool, "_run_interactive_resilient", _fake_resilient)

    with pytest.raises(QuotaWaitExhausted) as caught:
        tool._resume_or_fresh_session(
            prev_session_id="sid-1",
            resume_prompt="resume the work",
            fresh_prompt="do the work from scratch",
            system_instruction=None,
            excluded_providers=[],
            total_timeout=None,
            allowed_tools=None,
            scene="execution",
        )

    assert caught.value is escalated, "the exception must arrive unchanged"
    assert attempts == [True], (
        f"only the resume attempt may run; a fresh-session fallback "
        f"re-sends the whole prompt to a provider with no budget "
        f"(attempts={attempts})"
    )


# ---------------------------------------------------------------------------
# The verdict is wired into the dispatch loop
# ---------------------------------------------------------------------------

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

_PROVIDER_ENV = {
    "base_url": "https://api.vendor-a.example/anthropic",
    "api_key": "sk-fake",
    "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
}


@pytest.fixture
def scene(monkeypatch):
    """Wire a 3-pool scene chain for any scene name."""
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, dict(_PROVIDER_ENV))),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage",
        lambda *a, **k: {p: 100.0 for p in CHAIN},
    )


def _fleet_at_hard_cap(monkeypatch):
    """Every provider in the fleet reads as a spent balance."""
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map",
        lambda *a, **k: {p: _hard_cap(p) for p in CHAIN},
    )


def _tool(scene_name):
    return ClaudeCodingTool(
        scene=scene_name,
        concurrency_tracker=ActiveConcurrencyTracker(),
    )


class _StopWaiting(Exception):
    """Ends the wait so the test does not spin for real."""


def _sleep_only_once(monkeypatch):
    """Record the wait and stop there.

    The wait under the closed-window probe is unbounded by design, so a
    test that entered it for real would spin until the pytest timeout.
    The sentinel says "the wait was entered" without serving it.
    """
    sleeps: list = []

    def _sleep(seconds):
        sleeps.append(seconds)
        raise _StopWaiting()

    monkeypatch.setattr(ct, "_quota_wake_sleep", _sleep)
    return sleeps


def test_an_execution_dispatch_on_a_hard_capped_fleet_escalates(
    monkeypatch, scene,
):
    """The dispatch raises instead of probing a cap that cannot clear.

    Without this the dispatch would probe every five minutes for the
    rest of the run, and every probe would read the same answer.
    """
    _fleet_at_hard_cap(monkeypatch)
    sleeps = _sleep_only_once(monkeypatch)

    tool = _tool("execution")
    with pytest.raises(QuotaWaitExhausted) as caught:
        tool._run_claude_interactive(
            "hi", idle_timeout=30, total_timeout=30,
        )

    assert caught.value.needs_manual_topup is True
    assert set(caught.value.providers) == set(CHAIN), caught.value.providers
    assert sleeps == [], (
        f"a hard cap must not be waited on at all (slept {sleeps})"
    )


def test_a_non_execution_dispatch_on_a_hard_capped_fleet_keeps_waiting(
    monkeypatch, scene,
):
    """The other scenes wait: nobody there receives the marker.

    The wait is entered exactly as it was before this change — the
    difference is only that the execution scene does not enter it when
    the whole fleet is at a cap.
    """
    _fleet_at_hard_cap(monkeypatch)
    sleeps = _sleep_only_once(monkeypatch)

    tool = _tool("verification")
    with pytest.raises(_StopWaiting):
        tool._run_claude_interactive(
            "hi", idle_timeout=30, total_timeout=30,
        )

    assert sleeps, "the non-execution scene must still enter the wait"


def test_the_escalation_reaches_the_caller_of_query(monkeypatch, scene):
    """``query()`` hands the escalation up unchanged.

    Two layers sit between the dispatch loop and the caller, and both
    of them used to be able to swallow it: the resume fallback, which
    read the escalation as "the resume failed" and answered with a
    fresh session and the whole prompt re-sent, and the generic arm of
    the query, which logs and re-raises. The signal has to arrive as
    the same exception, or the agent cannot turn it into a
    ``waiting_for_quota`` marker.
    """
    _fleet_at_hard_cap(monkeypatch)
    sleeps = _sleep_only_once(monkeypatch)

    tool = _tool("execution")
    tool.logger = MagicMock()
    with pytest.raises(QuotaWaitExhausted) as caught:
        tool.query("hi", timeout=30)

    assert caught.value.needs_manual_topup is True
    assert set(caught.value.providers) == set(CHAIN), caught.value.providers
    assert sleeps == [], (
        f"a hard cap must not be waited on at all (slept {sleeps})"
    )
    # The query's generic arm logs the failure and re-raises it, and
    # that is the arm the escalation has to survive.
    logged = [call.args[0] for call in tool.logger.error.call_args_list]
    assert "coding_query_failed" in logged, logged
