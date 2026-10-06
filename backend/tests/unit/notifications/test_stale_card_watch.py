"""A card that stops moving while a worker runs is a lie of omission.

2026-10-05, on a real plan's verification round 1. The judgment phase
ran 98 minutes (20:53 → 22:31), emitting ``judgment_heartbeat`` once
per VP across VP-001…VP-017. Every heartbeat published
``KIND_VP_STATE_CHANGED``, so the notifier's worker rebuilt the card
throughout — and the fingerprint dedup discarded every rebuild, because
none of them changed a rendered field. Zero pushes in 106 minutes. The
card showed a verdict from a phase that had already ended.

The dedup is not the bug; it is the right default. What was missing is
any way to distinguish "nothing is happening" from "something is
happening that renders to no visible change" — and during a long job an
operator cannot tell those apart from the card alone.

So the notifier now bounds staleness rather than guessing: a plan with
a worker in flight and no push for ``stale_refresh_seconds`` gets a
forced refresh that bypasses the dedup. The push it produces is usually
identical to the last one. That is the point — the "🕐 刷新于"
timestamp advances, so a live job looks live and a stalled one shows it.

Pinned here:

  * an in-flight plan past the threshold enqueues ``KIND_STALE_REFRESH``;
  * a plan whose card is fresh does not;
  * a plan with no worker in flight does not — re-pushing an idle plan
    forever is exactly the spam the dedup exists to prevent;
  * ``KIND_STALE_REFRESH`` bypasses the fingerprint dedup, which is the
    whole reason it is a distinct kind;
  * a non-positive threshold disables the watch.
"""

from __future__ import annotations

import queue
import time

import pytest

from notifications.feishu_notifier import FeishuNotifier
from notifications.state_events import KIND_STALE_REFRESH

PLAN_ID = "plan-stale"


@pytest.fixture
def notifier() -> FeishuNotifier:
    return FeishuNotifier(
        coalesce_seconds=0.05,
        min_interval_seconds=0.0,
        stale_refresh_seconds=300.0,
    )


def _drain(notifier: FeishuNotifier) -> list:
    events = []
    while True:
        try:
            events.append(notifier._queue.get_nowait())
        except queue.Empty:
            return events


def _install_state(notifier: FeishuNotifier, *, age_seconds: float,
                   disabled: bool = False) -> None:
    """Register a tracked plan whose last push is ``age_seconds`` old."""
    from notifications.feishu_notifier import PlanCardState

    state = PlanCardState(plan_id=PLAN_ID)
    state.last_push_ts = time.time() - age_seconds
    state.permanently_disabled = disabled
    notifier._states[PLAN_ID] = state


def _stub_status(monkeypatch, payload) -> None:
    monkeypatch.setattr(
        "notifications.feishu_notifier.fetch_plan_status",
        lambda plan_id, base_url=None: payload,
    )


def test_an_in_flight_plan_past_the_threshold_is_refreshed(
    notifier, monkeypatch,
):
    _install_state(notifier, age_seconds=600.0)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": True,
        "verification_in_flight": False,
    })

    notifier._watch_stale_cards()

    events = _drain(notifier)
    assert [e.kind for e in events] == [KIND_STALE_REFRESH], events
    assert events[0].plan_id == PLAN_ID
    assert notifier._stale_refreshes == 1


def test_a_live_verification_round_also_counts(notifier, monkeypatch):
    """The 98-minute case was a verification round, not an execution —
    gating on execution alone would have missed it."""
    _install_state(notifier, age_seconds=600.0)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": False,
        "verification_in_flight": True,
    })

    notifier._watch_stale_cards()

    assert [e.kind for e in _drain(notifier)] == [KIND_STALE_REFRESH]


def test_a_fresh_card_is_left_alone(notifier, monkeypatch):
    _install_state(notifier, age_seconds=10.0)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": True,
        "verification_in_flight": True,
    })

    notifier._watch_stale_cards()

    assert _drain(notifier) == []
    assert notifier._stale_refreshes == 0


def test_an_idle_plan_is_left_alone(notifier, monkeypatch):
    """No worker means no news. Pushing anyway would be the spam."""
    _install_state(notifier, age_seconds=10_000.0)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": False,
        "verification_in_flight": False,
    })

    notifier._watch_stale_cards()

    assert _drain(notifier) == []


def test_a_permanently_disabled_plan_is_skipped(notifier, monkeypatch):
    _install_state(notifier, age_seconds=600.0, disabled=True)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": True,
        "verification_in_flight": True,
    })

    notifier._watch_stale_cards()

    assert _drain(notifier) == []


def test_a_never_pushed_plan_is_not_refreshed(notifier, monkeypatch):
    """No ``last_push_ts`` means the startup sweep owns the first card;
    the watch must not race it."""
    from notifications.feishu_notifier import PlanCardState

    notifier._states[PLAN_ID] = PlanCardState(plan_id=PLAN_ID)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": True,
    })

    notifier._watch_stale_cards()

    assert _drain(notifier) == []


def test_an_unreadable_status_is_skipped_without_raising(
    notifier, monkeypatch,
):
    _install_state(notifier, age_seconds=600.0)
    _stub_status(monkeypatch, None)

    notifier._watch_stale_cards()  # must not raise

    assert _drain(notifier) == []


def test_a_raising_fetch_is_skipped_without_raising(notifier, monkeypatch):
    _install_state(notifier, age_seconds=600.0)

    def _boom(plan_id, base_url=None):
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "notifications.feishu_notifier.fetch_plan_status", _boom,
    )

    notifier._watch_stale_cards()  # must not raise

    assert _drain(notifier) == []


def test_a_non_positive_threshold_disables_the_watch(monkeypatch):
    notifier = FeishuNotifier(
        coalesce_seconds=0.05, min_interval_seconds=0.0,
        stale_refresh_seconds=0.0,
    )
    _install_state(notifier, age_seconds=100_000.0)
    _stub_status(monkeypatch, {
        "plan_id": PLAN_ID,
        "execution_in_flight": True,
    })

    notifier._watch_stale_cards()

    assert _drain(notifier) == []


def test_stale_refresh_bypasses_the_fingerprint_dedup(notifier, monkeypatch):
    """The reason this is its own event kind.

    Without the bypass the forced rebuild would produce a card identical
    to the last one and be discarded — reproducing the exact freeze the
    watch exists to prevent.
    """
    from notifications.fingerprint import card_fingerprint

    card = {"header": {"title": {"content": "执行中"}}, "elements": []}
    fp = card_fingerprint(card)
    state = notifier._states.get(PLAN_ID)
    assert state is None

    _install_state(notifier, age_seconds=600.0)
    notifier._states[PLAN_ID].last_fingerprint = fp

    pushed: list = []
    monkeypatch.setattr(
        notifier, "_rebuild_card",
        lambda plan_id, kinds: (card, "executing", None, {}),
    )
    monkeypatch.setattr(
        notifier, "_push_to_feishu", lambda chat_id, st, c: pushed.append(c) or True,
    )
    monkeypatch.setattr(notifier, "_push_to_telegram", lambda st, c: None)
    notifier._states[PLAN_ID].chat_id = "oc_test"

    # Same fingerprint, but the stale-refresh kind is present.
    notifier._handle_plan(PLAN_ID, {KIND_STALE_REFRESH}, final_flush=False)

    assert len(pushed) == 1, "identical card was deduped away"


def test_an_ordinary_event_still_dedupes_an_identical_card(
    notifier, monkeypatch,
):
    """The dedup itself must survive the new bypass — it is what keeps a
    busy plan from re-posting every coalesce window."""
    from notifications.fingerprint import card_fingerprint
    from notifications.state_events import KIND_PLAN_PHASE_CHANGED

    card = {"header": {"title": {"content": "执行中"}}, "elements": []}
    _install_state(notifier, age_seconds=0.0)
    notifier._states[PLAN_ID].last_fingerprint = card_fingerprint(card)
    notifier._states[PLAN_ID].chat_id = "oc_test"

    pushed: list = []
    monkeypatch.setattr(
        notifier, "_rebuild_card",
        lambda plan_id, kinds: (card, "executing", None, {}),
    )
    monkeypatch.setattr(
        notifier, "_push_to_feishu", lambda chat_id, st, c: pushed.append(c) or True,
    )
    monkeypatch.setattr(notifier, "_push_to_telegram", lambda st, c: None)

    notifier._handle_plan(PLAN_ID, {KIND_PLAN_PHASE_CHANGED}, final_flush=False)

    assert pushed == []
    assert notifier._deduped == 1
