"""Scene-routed dispatch is gated by local pool capacity (2026-09-17).

Downgrade is driven by *local slot exhaustion*, not by reachability. A
provider that answers is not a provider that should take all the work:
the call succeeds, but concentrating every concurrent agent on one row
drains that row's quota.

Before this, ``_run_claude_interactive``'s scene walk asked one question
per candidate — "does it have a base_url + token?" — and took the first
yes. Every pool was reachable, so the first one won every time: 100% of
verification traffic landed on Vendor A Pro while ``Vendor A`` and
``Vendor A API`` (separate CC Switch rows, separate tokens, separate
quota) served nothing.

These tests drive the REAL ``_run_claude_interactive`` against a fake
``claude`` on ``PATH`` so the slot's lifecycle — acquire in the walk,
release on every teardown arm — is exercised rather than mocked.
"""

from __future__ import annotations

import os
import stat
import sys
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
    EmptyResponseError,
    SceneProviderUnavailable,
)
from dynamic_provider_concurrency import ActiveConcurrencyTracker  # noqa: E402

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

#: Ten per pool, so "fill it" is ``range(10)`` throughout and the three
#: pools sum to the fleet these tests describe.
_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]


@pytest.fixture(autouse=True)
def _pools(capacity_config):
    capacity_config(_POOL_CAPS)


_PROVIDER_ENV = {
    "base_url": "https://api.vendor-a.example/anthropic",
    "api_key": "sk-fake",
    "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
}


def _install_fake_claude(tmp_path, monkeypatch, script: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture
def scene(monkeypatch):
    """Wire a 3-pool scene chain; returns a helper to set per-pool quotas."""
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, dict(_PROVIDER_ENV))),
    )

    def _set_usage(usage):
        monkeypatch.setattr(
            provider_order, "load_provider_5h_usage", lambda *a, **k: dict(usage),
        )

    _set_usage({p: 100.0 for p in CHAIN})
    return _set_usage


def _tool(tracker, **kw):
    return ClaudeCodingTool(scene="verification", concurrency_tracker=tracker, **kw)


def _run(tool, **kw):
    kwargs = dict(idle_timeout=30, total_timeout=30)
    kwargs.update(kw)
    return tool._run_claude_interactive("hi", **kwargs)


OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"
BOOM_SCRIPT = "#!/bin/sh\nexit 4\n"


# ---------------------------------------------------------------------------
# The rule: a full pool hands over to the next one
# ---------------------------------------------------------------------------


def test_a_saturated_first_pool_does_not_get_the_work(tmp_path, monkeypatch, scene):
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()
    for _ in range(10):                       # Vendor A Pro is at its cap
        tracker.try_acquire("Vendor A Pro", 10)

    tool = _tool(tracker)
    _run(tool)

    assert tool.current_call_provider == "Vendor A", (
        "the walk must consult the local semaphore, not just reachability — "
        "the first pool was full but perfectly reachable"
    )
    # And the borrowed slot went back: the call is over.
    assert tracker.current("Vendor A") == 0
    assert tracker.current("Vendor A Pro") == 10, "the pre-filled 10 are untouched"


def test_the_capacity_check_beats_chain_order(tmp_path, monkeypatch, scene):
    """Fill the LAST pool instead; the first is still picked (order intact)."""
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()
    for _ in range(10):
        tracker.try_acquire("Vendor A API", 10)

    tool = _tool(tracker)
    _run(tool)

    assert tool.current_call_provider == "Vendor A Pro"


def test_each_pool_contributes_its_own_ten(tmp_path, monkeypatch, scene):
    """Three CC Switch rows, three independent budgets of 10.

    Slots are *held* between steps (a completed call returns its slot, so
    sequential calls alone would never fill a pool) to model the live
    case: a dozen verification VPs in flight at once.
    """
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()

    def _pick():
        tool = _tool(tracker)
        _run(tool)
        return tool.current_call_provider

    assert _pick() == "Vendor A Pro"

    for _ in range(10):
        tracker.try_acquire("Vendor A Pro", 10)
    assert _pick() == "Vendor A", "the second row takes the overflow"

    for _ in range(10):
        tracker.try_acquire("Vendor A", 10)
    assert _pick() == "Vendor A API", "the third row is not decoration"

    for _ in range(10):
        tracker.try_acquire("Vendor A API", 10)

    # 30 in flight is the whole Vendor A family, so the 31st has nowhere
    # approved to go *this second*. That is a queue, not a failure: a
    # slot comes back when the agent holding it finishes, and until then
    # there is nothing to fail on. What must still hold is that the
    # parent config — outside the scene's tier table — never answers.
    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 0.0
    steps: list = []

    def _sleep(seconds):
        steps.append(seconds)
        if len(steps) >= 3:
            tracker.release("Vendor A API")

    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
    result, _session, _meta = _run(tool)

    assert result == "ok"
    assert tool.current_call_provider == "Vendor A API", (
        f"the work went to {tool.current_call_provider!r}; with every pool "
        f"full the only rows the scene approves are its own three"
    )
    assert tracker.current("Vendor A Pro") == 10, (
        "the other two pools were not borrowed from"
    )
    assert tracker.current("Vendor A") == 10


# ---------------------------------------------------------------------------
# The slot comes back
# ---------------------------------------------------------------------------


def test_the_slot_is_returned_after_a_successful_call(tmp_path, monkeypatch, scene):
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()

    _run(_tool(tracker))

    assert tracker.current("Vendor A Pro") == 0
    assert tracker.total() == 0, (
        "a leaked slot never comes back — the pool would report itself "
        "full for the life of the process"
    )


def test_the_slot_is_returned_when_the_subprocess_produces_nothing(
    tmp_path, monkeypatch, scene,
):
    """The error path is the one that leaks if the release is not in a finally."""
    _install_fake_claude(tmp_path, monkeypatch, BOOM_SCRIPT)
    tracker = ActiveConcurrencyTracker()

    tool = _tool(tracker)
    with pytest.raises(EmptyResponseError):
        _run(tool)

    assert tracker.total() == 0


def test_the_slot_is_returned_when_the_provider_times_out(tmp_path, monkeypatch, scene):
    """A silently-hanging provider must not hold its slot forever."""
    _install_fake_claude(tmp_path, monkeypatch, "#!/bin/sh\nsleep 30\n")
    tracker = ActiveConcurrencyTracker()

    tool = _tool(tracker)
    with pytest.raises(coding_tool.HardTimeoutError):
        _run(tool, total_timeout=1, idle_timeout=60)

    assert tracker.total() == 0


# ---------------------------------------------------------------------------
# Queueing, and what it must NOT wait for
# ---------------------------------------------------------------------------


def test_all_pools_full_queues_and_then_dispatches(tmp_path, monkeypatch, scene):
    """Every pool full → wait, then serve. Never over-subscribe, never borrow.

    **Contract change, not a relaxation.** This asserted
    ``reason == "no_capacity"`` after a 0.2 s wait. It now asserts the
    call waits for a slot and comes out the other side having dispatched:
    a slot is a lease that comes back when its holder finishes, so
    failing on it reported a failure for a queue the dispatcher itself
    declared over. Both properties the old test protected are kept —
    nothing extra was taken from a pool that was not freed, and the
    parent environment (outside the scene's tier table) never answered.

    The negative half lives in
    ``test_a_pool_with_no_quota_is_not_waited_for`` below: a cap of zero
    is a window, and a window is not queued for.
    """
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()
    for name in CHAIN:
        for _ in range(10):
            tracker.try_acquire(name, 10)

    steps: list = []

    def _sleep(seconds):
        steps.append(seconds)
        if len(steps) >= 2:
            tracker.release(CHAIN[0])

    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 0.0
    result, _session, _meta = _run(tool)

    assert result == "ok"
    assert steps == [coding_tool._slot_wake_step_sec(i) for i in range(2)]
    assert tool.current_call_provider == CHAIN[0], (
        f"the work went to {tool.current_call_provider!r}; the parent "
        f"config is not a tier member and must not answer"
    )
    assert [tracker.current(n) for n in CHAIN[1:]] == [10, 10], (
        "nothing extra was taken from the pools that were not freed"
    )


def _open_verdict(name):
    """A reading with budget in it: a cap, and not shut."""
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=96.0,
        exhausted=False,
    )


def _no_reading_then(verdicts):
    """Answer the reader once with nothing, then with ``verdicts``.

    The first payload is "no reading", which is the fail-open half of
    the reader's contract, so the legacy usage map this test sets to
    ``0.0`` is what decides the cap — a budget with nothing left in it.
    """
    reads: list = []

    def _load(*_a, **_kw):
        reads.append(True)
        return {} if len(reads) == 1 else dict(verdicts)

    return _load


def test_a_pool_with_no_quota_waits_on_the_window_and_then_dispatches(
    tmp_path, monkeypatch, scene,
):
    """0% means the budget is gone — so wait for it, do not fail.

    **Contract change, not a relaxation.** This asserted
    ``reason == "no_capacity"`` with an empty ``sleeps``. Nothing about
    the pool changed: a remaining budget with nothing in it is a window
    that rolls, so the dispatch is handed to the same wait a shut reading
    gets — probe, re-read, re-decide — and is served the moment a probe
    finds the budget back. Failing closed answered "should this call
    never run?" about a pause that had an end.

    What it must still never do is *queue*: a lease wait pointed at a
    five-hour clock does not outlast it.
    """
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    scene({p: 0.0 for p in CHAIN})
    tracker = ActiveConcurrencyTracker()

    sleeps: list = []
    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", sleeps.append)
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map",
        _no_reading_then({p: _open_verdict(p) for p in CHAIN}),
    )
    tool = _tool(tracker)
    result, _session, _meta = _run(tool)

    assert result == "ok", (
        "a budget with nothing left is a pause; the pause has to end in "
        "the work rather than in a failed call"
    )
    assert tool.current_call_provider in CHAIN
    assert sleeps == [coding_tool._quota_wake_probe_sec()], sleeps
    assert sleeps[0] != coding_tool._slot_wake_step_sec(0), (
        "a shut window is not a held slot; the queue's doubling beat is "
        "the wrong clock for it"
    )
    assert tracker.total() == 0


def test_a_reduced_quota_shrinks_that_pool(tmp_path, monkeypatch, scene):
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    usage = {p: 100.0 for p in CHAIN}
    usage["Vendor A Pro"] = 60.0        # limit 6
    scene(usage)
    tracker = ActiveConcurrencyTracker()
    for _ in range(6):
        tracker.try_acquire("Vendor A Pro", 10)

    tool = _tool(tracker)
    _run(tool)

    assert tool.current_call_provider == "Vendor A"


def test_a_saturated_fleet_queues_and_never_over_subscribes(
    tmp_path, monkeypatch, scene,
):
    """All three pools at cap → the call queues, and takes nothing extra.

    **Contract change, not a relaxation.** Was: ``reason ==
    "no_capacity"`` and ``tracker.total() == 30``. Now: the queue is
    entered and the call is served once a slot is released. The
    over-subscription half is untouched and is the one worth keeping —
    the fleet size is the three declared caps (30 here), not a constant,
    and a queue must not be a licence to take a 31st slot.
    """
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()
    for name in CHAIN:
        for _ in range(10):
            tracker.try_acquire(name, 10)

    rounds: list = []

    def _sleep(seconds):
        rounds.append(tracker.total())
        if len(rounds) >= 3:
            tracker.release(CHAIN[1])

    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 0.0
    _run(tool)

    assert rounds[:3] == [30, 30, 30], (
        f"the pool stood at {rounds} while the dispatch was queued; a "
        f"queue is for waiting, not for over-subscribing"
    )
    assert tool.current_call_provider == CHAIN[1], (
        "the row that released a slot is the row that served it"
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_slot_wait_is_configurable_by_env(monkeypatch):
    monkeypatch.setenv("PDT_PROVIDER_SLOT_WAIT_SEC", "12.5")
    assert ClaudeCodingTool().provider_slot_wait_sec == 12.5


def test_a_bad_env_value_does_not_break_construction(monkeypatch):
    monkeypatch.setenv("PDT_PROVIDER_SLOT_WAIT_SEC", "not-a-number")
    assert ClaudeCodingTool().provider_slot_wait_sec == ClaudeCodingTool.PROVIDER_SLOT_WAIT_SEC
