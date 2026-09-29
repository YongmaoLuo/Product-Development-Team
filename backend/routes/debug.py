"""Operator/debug endpoints (state injection, card repush, watchdog view).

Extracted from ``server.py`` on 2026-09-25. See ``routes/phases.py`` for the
late-binding rule that governs every module in this package.
"""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

router = APIRouter()


@router.post("/api/_debug/inject_verification_state/{plan_id}")
def debug_inject_verification_state(plan_id: str, payload: dict):
    """DEBUG ONLY: inject in-memory _verification_state for testing."""
    _server._validated_plan_id(plan_id)   # uniform contract — see that function
    state = {
        "plan_id": plan_id,
        "verification_status": payload.get("verification_status", "failed"),
        "verification_round": payload.get("verification_round", 0),
        "verification_max_rounds": payload.get(
            "verification_max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
        ),
        "results": payload.get("results", {"pytest_summary": "1 passed, 1 failed", "llm_findings": "VP-024 failed", "performance_metrics": {}}),
        "repair_tasks": payload.get("repair_tasks", []),
        "execution_profile": {},
        "started_at": payload.get("started_at"),
        "updated_at": payload.get("updated_at"),
        "orchestrator": None,
        "stop_reason": payload.get("stop_reason"),
    }
    _server._verification_state[plan_id] = state
    return {"injected": True, "state_keys": list(state.keys())}


@router.post("/api/_debug/fire_event/{plan_id}")
def debug_fire_event(plan_id: str, payload: dict):
    """DEBUG ONLY: fire a synthetic event through the real ``publish_safe`` path.

    Used to verify the event-driven Feishu push round-trip without
    having to drive a real plan through a state transition. The
    notifier subscriber receives the event through the same bus it
    uses in production, so a successful push is a real proof that
    event → bus → notifier → Feishu works end-to-end.

    Body params:
      * ``kind`` — one of the four event kinds
        (``plan_phase_changed``, ``task_state_changed``,
        ``vp_state_changed``, ``plan_closed``). Defaults to
        ``task_state_changed``.
      * ``sub_kind`` — free-form string, surfaces in event payload.
      * any extra keyword args get passed to ``publish_safe``.

    Returns the bus stats snapshot before and after the publish so
    the caller can see ``published`` increment by 1.
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function
    from notifications.state_events import (
        KIND_PLAN_CLOSED,
        KIND_PLAN_PHASE_CHANGED,
        KIND_TASK_STATE_CHANGED,
        KIND_VP_STATE_CHANGED,
        STATE_EVENT_BUS,
        publish_safe,
    )
    valid = {
        KIND_PLAN_PHASE_CHANGED,
        KIND_TASK_STATE_CHANGED,
        KIND_VP_STATE_CHANGED,
        KIND_PLAN_CLOSED,
    }
    kind = payload.get("kind", KIND_TASK_STATE_CHANGED)
    if kind not in valid:
        raise HTTPException(400, f"unknown kind {kind!r}; must be one of {sorted(valid)}")
    kwargs = {k: v for k, v in payload.items() if k != "kind"}
    before = STATE_EVENT_BUS.stats()
    publish_safe(kind, plan_id, **kwargs)
    after = STATE_EVENT_BUS.stats()
    return {
        "published_kind": kind,
        "plan_id": plan_id,
        "before": {"published": before["published"]},
        "after": {"published": after["published"]},
    }


@router.get("/api/debug/notifications")
def debug_notifications():
    """Observability for the in-process event bus + Feishu notifier.

    Distinguishes the three failure modes the user previously
    couldn't tell apart ("no card appeared"):

      * ``bus.handler_errors`` non-zero  →  a subscriber raised,
        most likely a bug in the notifier itself.
      * ``notifier.pushes_failed`` non-zero  →  Feishu API
        rejected the call (bad creds, expired card, rate limit).
      * ``notifier.queued > 0`` for long  →  worker thread is
        stuck (rare; would show up as ``deduped`` static too).

    A safe "everything fine" looks like:
      ``{"bus": {"published": N, "handler_errors": 0, ...},
        "notifier": {"enabled": true, "pushes_ok": N,
        "pushes_failed": 0, "dropped": 0, "deduped": N, ...}}``
    """
    from notifications.state_events import STATE_EVENT_BUS
    bus_stats = STATE_EVENT_BUS.stats()
    notifier = getattr(_server.app.state, "feishu_notifier", None)
    notifier_stats = notifier.stats() if notifier is not None else {"enabled": False}
    return {"bus": bus_stats, "notifier": notifier_stats}


@router.post("/api/debug/repush_plan_card/{plan_id:path}")
def debug_repush_plan_card(plan_id: str):
    """Force-repush a card by injecting a ``plan_phase_changed`` event
    directly into the in-process FeishuNotifier queue.

    2026-09-10: the card body must be inspectable in Feishu itself,
    not just via a local render. The
    normal startup sweep filters out terminal plans (executing /
    verification_running only), so terminal plans never get re-pushed
    even after a server restart. This endpoint bypasses that filter
    by enqueuing a synthetic event so the operator can confirm the
    card body changes land in their chat.

    Use case: after editing ``backend/notifications/cards.py`` for a
    terminal plan, call this endpoint to push the latest card.

    Returns the notifier's stats snapshot so the caller can see
    whether the repush was deduped or actually pushed.
    """
    from notifications.state_events import (
        KIND_PLAN_PHASE_CHANGED,
        StateEvent,
    )
    notifier = getattr(_server.app.state, "feishu_notifier", None)
    if notifier is None:
        return {"ok": False, "reason": "notifier not initialized"}
    # Bypass the in-process bus and inject directly into the
    # notifier's queue. Same code path as the startup sweep.
    notifier._enqueue_event(StateEvent(
        kind=KIND_PLAN_PHASE_CHANGED,
        plan_id=plan_id,
        payload={"sub_kind": "debug_repush"},
    ))
    # Give the worker a moment to drain — the coalesce window is
    # 5s, so the caller can poll ``/api/debug/notifications`` after
    # 6s to see pushes_ok / deduped counts.
    return {
        "ok": True,
        "plan_id": plan_id,
        "stats": notifier.stats(),
    }


@router.get("/api/debug/verification_watchdog")
def debug_verification_watchdog():
    """Observability for the verification watchdog (2026-09-06 plan).

    Distinct from ``/api/debug/notifications`` (which only surfaces
    event-bus + Feishu-push health). This endpoint surfaces the
    watchdog's own heartbeat:

    - ``last_sweep_at``: epoch seconds of the most recent
      ``HeartbeatMonitor._check_once`` invocation — proves the
      heartbeat thread is alive even on an idle server.
    - ``last_sweep_count``: number of plans in
      ``_execution_state + _verification_state + sub_agent_registry``
      at the last sweep (zero on idle).
    - ``staleness_threshold_seconds``: current value of
      ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` (env-overridable).
    - ``recent_terminal_actions``: bounded ring buffer of the last
      ~200 transitions the watchdog (or ``force_terminal`` API)
      triggered, newest-first. Each entry is
      ``{plan_id, stop_reason, ts}``. Useful for distinguishing
      automatic recovery (``verification_thread_died_unexpectedly`` /
      ``verification_log_stale`` / ``verification_results_stale``)
      from operator-driven ``user_force_terminal`` calls.
    - ``per_plan_last_action_ts``: per-plan epoch timestamps for the
      most recent watchdog action — used internally for the 60s dedup
      window in ``_lazy_check_verification`` (so a stuck plan can't
      emit duplicate ``KIND_PLAN_CLOSED`` events), but also useful for
      debugging "why hasn't this plan been re-triggered yet?".
    """
    actions_snapshot = list(_server._WATCHDOG_STATS["actions"])
    return {
        "interval_seconds": _server.HEARTBEAT_INTERVAL,
        "staleness_threshold_seconds": _server.VERIFICATION_WATCHDOG_STALENESS_SECONDS,
        "last_sweep_at": _server._WATCHDOG_STATS["last_sweep_at"],
        "last_sweep_count": _server._WATCHDOG_STATS["last_sweep_count"],
        "recent_terminal_actions": actions_snapshot[-20:],
        "per_plan_last_action_ts": dict(_server._WATCHDOG_STATS["per_plan_last_action_ts"]),
        # 2026-09-07: counters for the kill+retry+SKIP path.
        # The sub-agent watchdog no longer aborts the round on kill;
        # it signals the executor to retry (kill_count == 1) or give
        # up + mark SKIPPED (kill_count >= 2). Operators can grep
        # ``sub_agent_skipped_total`` to detect accumulating SKIPs
        # without tailing server.log.
        "sub_agent_killed_total": _server._WATCHDOG_STATS["sub_agent_killed_total"],
        "sub_agent_skipped_total": _server._WATCHDOG_STATS["sub_agent_skipped_total"],
    }

