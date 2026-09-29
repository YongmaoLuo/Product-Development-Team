"""Unit tests for ``FeishuNotifier`` per-plan rate-limiting (2026-09-11 plan v4).

The rule: one push per card per minute, and throttling one card never
delays another.

The v4 design is the simplest possible:

  * Per-plan 1/min gate, INDEPENDENT across plans.
  * Each plan tracks its own ``state.last_push_ts``. Plan A's push
    does NOT throttle plan B's push.
  * No global / cycle / batch gate.
  * Bypass: ``KIND_PLAN_CLOSED`` (terminal must always land) and
    ``final_flush=True`` (shutdown drain) skip the per-plan gate.

Five scenarios pinned here:

  1. ``min_interval_seconds`` default is 60.0.
  2. After a successful push, the same plan can't push again within
     the window (per-plan gate active).
  3. After the window expires, the same plan can push again.
  4. Different plans are NOT throttled together — plan A being
     throttled does NOT block plan B.
  5. ``final_flush=True`` (shutdown) bypasses the per-plan gate so
     terminal / queued events drain before exit.

All tests run against the real ``FeishuNotifier`` with mocked transport
layers so no real network calls happen.
"""

from __future__ import annotations

import time
from typing import List
from unittest.mock import patch as _mock_patch

import pytest

from notifications.feishu_notifier import FeishuNotifier, PlanCardState


@pytest.fixture
def notifier() -> FeishuNotifier:
    """A bare ``FeishuNotifier`` with default config — no ``start()``,
    no network. Used to test per-plan gate behavior directly.
    """
    n = FeishuNotifier()
    # Inject chat_id for 3 plans so _handle_plan can proceed past
    # the chat_id check during integration-style tests.
    for pid in ("plan-a", "plan-b", "plan-c"):
        s = PlanCardState(plan_id=pid)
        s.chat_id = "chat-x"
        s.message_id = "om_test_" + pid
        n._states[pid] = s
    return n


# --- 1. default min_interval is 60s ---

def test_min_interval_default_is_60(notifier: FeishuNotifier) -> None:
    """Per-plan ``min_interval_seconds`` default is 60s — aligns with
    legacy polling cadence.
    """
    assert notifier._min_interval_seconds == 60.0, (
        f"min_interval default must be 60.0 (legacy polling cadence), "
        f"got {notifier._min_interval_seconds}"
    )


def test_min_interval_is_configurable() -> None:
    """Override still works for tests that need faster cadence."""
    n = FeishuNotifier(min_interval_seconds=2.0)
    assert n._min_interval_seconds == 2.0


def test_no_global_rate_limit_attributes(notifier: FeishuNotifier) -> None:
    """v4 has NO global / cycle / batch rate-limit fields.

    User clarified the requirement as "per-card 1/min, independent
    across cards". The v3 ``_last_global_push_ts`` /
    ``_pushes_skipped_global_limit`` / cycle-level gate are all
    gone — each plan throttles independently using its own
    ``state.last_push_ts``.
    """
    assert not hasattr(notifier, "_last_global_push_ts"), (
        "_last_global_push_ts must NOT exist in v4 — per-plan only"
    )
    assert not hasattr(notifier, "_pushes_skipped_global_limit"), (
        "_pushes_skipped_global_limit must NOT exist in v4 — "
        "no global skip counter"
    )
    assert not hasattr(notifier, "_push_timestamps"), (
        "_push_timestamps deque must NOT exist in v4 — no global "
        "sliding window"
    )
    assert not hasattr(notifier, "_max_pushes_per_minute"), (
        "_max_pushes_per_minute must NOT exist in v4 — no global cap"
    )


# --- 2. Per-plan gate blocks subsequent pushes within window ---

def test_per_plan_gate_blocks_same_plan_within_window(
    notifier: FeishuNotifier,
) -> None:
    """After a successful push on plan A, plan A can't push again
    within the 60s window. This is the per-plan gate invariant.
    """
    plan_a = notifier._states["plan-a"]
    # Simulate a successful push 10s ago
    plan_a.last_push_ts = time.time() - 10
    plan_a.last_fingerprint = "stale"  # force rebuild != last fp

    from unittest.mock import patch as mp
    with mp.object(notifier, "_rebuild_card", return_value=(
        {"header": {"title": {"content": "new"}}}, "completed", "passed", {}
    )), mp.object(notifier, "_push_to_feishu", return_value=True), \
         mp.object(notifier, "_push_to_telegram", return_value=True), \
         mp.object(notifier, "_write_card_snapshot"):

        import notifications.feishu_notifier as fn
        with mp.object(fn, "_save_card_state"):
            notifier._handle_plan("plan-a", set(), final_flush=False)

    # Push should have been BLOCKED by the per-plan gate
    assert plan_a.last_fingerprint == "stale", (
        "plan-A should be throttled — fingerprint must NOT update"
    )


def test_per_plan_gate_unblocks_after_window(
    notifier: FeishuNotifier,
) -> None:
    """After 60s elapses since the last push, the per-plan gate
    releases.
    """
    plan_a = notifier._states["plan-a"]
    plan_a.last_push_ts = time.time() - 61  # 61s ago
    plan_a.last_fingerprint = "stale"

    from unittest.mock import patch as mp
    feishu_calls: List[str] = []
    def _push_feishu(chat_id, state, card):
        feishu_calls.append(state.plan_id)
        return True

    with mp.object(notifier, "_rebuild_card", return_value=(
        {"header": {"title": {"content": "new"}}}, "completed", "passed", {}
    )), mp.object(notifier, "_push_to_feishu", side_effect=_push_feishu), \
         mp.object(notifier, "_push_to_telegram", return_value=True), \
         mp.object(notifier, "_write_card_snapshot"):

        import notifications.feishu_notifier as fn
        with mp.object(fn, "_save_card_state"):
            notifier._handle_plan("plan-a", set(), final_flush=False)

    assert feishu_calls == ["plan-a"], (
        f"after window expires, plan-a should push; got {feishu_calls}"
    )


def test_per_plan_gate_unblocked_when_no_prior_push(
    notifier: FeishuNotifier,
) -> None:
    """First push on a plan (no prior timestamp) is always allowed.
    """
    plan_a = notifier._states["plan-a"]
    assert plan_a.last_push_ts is None
    plan_a.last_fingerprint = "stale"

    from unittest.mock import patch as mp
    feishu_calls: List[str] = []
    def _push_feishu(chat_id, state, card):
        feishu_calls.append(state.plan_id)
        return True

    with mp.object(notifier, "_rebuild_card", return_value=(
        {"header": {"title": {"content": "new"}}}, "completed", "passed", {}
    )), mp.object(notifier, "_push_to_feishu", side_effect=_push_feishu), \
         mp.object(notifier, "_push_to_telegram", return_value=True), \
         mp.object(notifier, "_write_card_snapshot"):

        import notifications.feishu_notifier as fn
        with mp.object(fn, "_save_card_state"):
            notifier._handle_plan("plan-a", set(), final_flush=False)

    assert feishu_calls == ["plan-a"]


# --- 3. Different plans are NOT throttled together ---

def test_different_plans_independent_throttling(
    notifier: FeishuNotifier,
) -> None:
    """Plan A being throttled does NOT block plan B's push.

    This is the core v4 invariant: per-plan isolation — one plan's
    throttle must not suppress another plan's push.
    """
    # Plan A: throttled (push 10s ago)
    notifier._states["plan-a"].last_push_ts = time.time() - 10
    notifier._states["plan-a"].last_fingerprint = "stale"

    # Plan B: no prior push (can push)
    notifier._states["plan-b"].last_push_ts = None
    notifier._states["plan-b"].last_fingerprint = "stale"

    from unittest.mock import patch as mp
    feishu_calls: List[str] = []
    def _push_feishu(chat_id, state, card):
        feishu_calls.append(state.plan_id)
        return True

    def _rebuild(plan_id, kinds):
        return (
            {"header": {"title": {"content": "new-" + plan_id}}},
            "completed", "passed", {"state": {}},
        )

    with mp.object(notifier, "_rebuild_card", side_effect=_rebuild), \
         mp.object(notifier, "_push_to_feishu", side_effect=_push_feishu), \
         mp.object(notifier, "_push_to_telegram", return_value=True), \
         mp.object(notifier, "_write_card_snapshot"):

        import notifications.feishu_notifier as fn
        with mp.object(fn, "_save_card_state"):
            # Plan A: should be blocked
            notifier._handle_plan("plan-a", set(), final_flush=False)
            # Plan B: should push (independent throttle)
            notifier._handle_plan("plan-b", set(), final_flush=False)

    assert feishu_calls == ["plan-b"], (
        f"plan A throttled must NOT block plan B; "
        f"expected ['plan-b'], got {feishu_calls}"
    )


# --- 4. Bypass paths ---

def test_final_flush_bypasses_per_plan_gate(
    notifier: FeishuNotifier,
) -> None:
    """``final_flush=True`` (shutdown drain) bypasses the per-plan
    gate so all dirty plans push before exit.
    """
    plan_a = notifier._states["plan-a"]
    plan_a.last_push_ts = time.time() - 10  # would normally block
    plan_a.last_fingerprint = "stale"

    from unittest.mock import patch as mp
    feishu_calls: List[str] = []
    def _push_feishu(chat_id, state, card):
        feishu_calls.append(state.plan_id)
        return True

    with mp.object(notifier, "_rebuild_card", return_value=(
        {"header": {"title": {"content": "new"}}}, "completed", "passed", {}
    )), mp.object(notifier, "_push_to_feishu", side_effect=_push_feishu), \
         mp.object(notifier, "_push_to_telegram", return_value=True), \
         mp.object(notifier, "_write_card_snapshot"):

        import notifications.feishu_notifier as fn
        with mp.object(fn, "_save_card_state"):
            notifier._handle_plan("plan-a", set(), final_flush=True)

    assert feishu_calls == ["plan-a"], (
        f"final_flush must bypass per-plan gate; got {feishu_calls}"
    )


def test_kind_plan_closed_bypasses_per_plan_gate(
    notifier: FeishuNotifier,
) -> None:
    """``KIND_PLAN_CLOSED`` events bypass the per-plan gate —
    terminal status must always reach Feishu / Telegram.
    """
    plan_a = notifier._states["plan-a"]
    plan_a.last_push_ts = time.time() - 10  # would normally block
    plan_a.last_fingerprint = "stale"

    from unittest.mock import patch as mp
    from notifications.feishu_notifier import KIND_PLAN_CLOSED
    feishu_calls: List[str] = []
    def _push_feishu(chat_id, state, card):
        feishu_calls.append(state.plan_id)
        return True

    with mp.object(notifier, "_rebuild_card", return_value=(
        {"header": {"title": {"content": "new"}}}, "completed", "passed", {}
    )), mp.object(notifier, "_push_to_feishu", side_effect=_push_feishu), \
         mp.object(notifier, "_push_to_telegram", return_value=True), \
         mp.object(notifier, "_write_card_snapshot"):

        import notifications.feishu_notifier as fn
        with mp.object(fn, "_save_card_state"):
            notifier._handle_plan(
                "plan-a", {KIND_PLAN_CLOSED}, final_flush=False,
            )

    assert feishu_calls == ["plan-a"], (
        f"KIND_PLAN_CLOSED must bypass per-plan gate; got {feishu_calls}"
    )


# --- 5. stats() surfaces config ---

def test_stats_includes_min_interval(notifier: FeishuNotifier) -> None:
    """``stats()`` exposes ``min_interval_seconds`` so the operator
    can read back the configured limit.
    """
    stats = notifier.stats()
    assert stats["min_interval_seconds"] == 60.0


def test_stats_does_not_contain_global_v2_v3_fields(
    notifier: FeishuNotifier,
) -> None:
    """v4 stats must NOT contain the global / cycle fields from v2/v3.
    The gate is per-plan only; there is no global cap.
    """
    stats = notifier.stats()
    assert "global_max_pushes_per_minute" not in stats
    assert "pushes_skipped_global_limit" not in stats
    assert "seconds_since_last_global_push" not in stats
    assert "pushes_in_last_60s" not in stats