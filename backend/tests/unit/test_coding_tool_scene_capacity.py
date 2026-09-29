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
from coding_tool import ClaudeCodingTool, EmptyResponseError  # noqa: E402
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
    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 0.2
    _run(tool)
    assert tool.current_call_provider == "parent", (
        "30 in flight is the whole Vendor A family; the 31st falls back to "
        "the parent config instead of over-subscribing"
    )


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
# Queueing, and what happens when the wait runs out
# ---------------------------------------------------------------------------


def test_all_pools_full_queues_then_falls_back_to_the_parent(tmp_path, monkeypatch, scene):
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()
    for name in CHAIN:
        for _ in range(10):
            tracker.try_acquire(name, 10)

    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 0.2
    _run(tool)

    # ``"parent"`` is the walk's explicit "use the process env"
    # sentinel — reachable only because the scene walk found no pool.
    assert tool.current_call_provider == "parent", (
        "with every pool full the dispatch must fall back to the parent "
        "config rather than over-subscribing the first pool"
    )
    assert tracker.total() == 30, "nothing extra was taken"


def test_a_pool_with_no_quota_is_not_waited_for(tmp_path, monkeypatch, scene):
    """0% means out of budget — waiting cannot help, so it must not."""
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    scene({p: 0.0 for p in CHAIN})
    tracker = ActiveConcurrencyTracker()

    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 30.0   # would hang the test if it waited
    _run(tool)

    assert tool.current_call_provider == "parent"
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


def test_a_saturated_fleet_falls_back_to_the_parent(tmp_path, monkeypatch, scene):
    """All three pools at cap → the parent config, not an over-subscribed pool.

    The fleet size is the three declared caps (30 here), not a constant:
    that is what "every pool is full" means.
    """
    _install_fake_claude(tmp_path, monkeypatch, OK_SCRIPT)
    tracker = ActiveConcurrencyTracker()
    for name in CHAIN:
        for _ in range(10):
            tracker.try_acquire(name, 10)

    tool = _tool(tracker)
    tool.provider_slot_wait_sec = 0.2
    _run(tool)

    assert tool.current_call_provider == "parent"
    assert tracker.total() == 30, "nothing extra was taken"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_slot_wait_is_configurable_by_env(monkeypatch):
    monkeypatch.setenv("PDT_PROVIDER_SLOT_WAIT_SEC", "12.5")
    assert ClaudeCodingTool().provider_slot_wait_sec == 12.5


def test_a_bad_env_value_does_not_break_construction(monkeypatch):
    monkeypatch.setenv("PDT_PROVIDER_SLOT_WAIT_SEC", "not-a-number")
    assert ClaudeCodingTool().provider_slot_wait_sec == ClaudeCodingTool.PROVIDER_SLOT_WAIT_SEC
