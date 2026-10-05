"""State Event Bus — in-process publish/subscribe for backend state changes.

Why this exists
---------------
The backend mutates plan state via repository write methods
(`RoutingRepository.try_mark_phase`, `ExecutionRepository.update_status`,
`VerificationRepository.append_verdict`, `ExecutionLogger.log`,
`VerificationPersistenceManager.write_verification_point_log`). All of
those are silent side-effects inside SQLite `_txn` blocks.

Previously the only consumer of these changes was an out-of-process
polling daemon (`tools/main.py`) that read `/api/plan/{id}/summary`,
`/api/execution/{id}/progress`, and `/api/verification/{id}/progress`
every 30 seconds. Polling is fragile (depends on a local symlink,
60-second worst-case latency, no in-process state coherence) and was
the root cause of the 2026-09-06 incident where Feishu card pushes
silently died because the bridge's Telegram import path threw and the
Feishu branch was unreachable behind an early-return guard.

This module gives the backend a single in-process event bus. After
every state change, the repository fires a `StateEvent` into the bus;
subscribers (today: `FeishuNotifier`) consume them in their own
threads.

Design rules
------------
* **Never raise to the caller.** `publish_safe` swallows every
  exception (handler crash, queue full, dataclass hash failure) and
  records it under `stats()`. Repositories call this from inside
  `_txn` blocks — a raise there would roll back the state change
  the operator already approved.
* **Module-level singleton.** The bus is constructed at import time;
  subscribers are added in `_lifespan` and removed on shutdown.
* **Thread-safe.** Repository writes happen from executor subprocess
  stdout-readers, FastAPI request threads, and the verification
  orchestrator's background threads. The bus is locked with an
  RLock; the handler iteration runs under it.
* **Opaque payload.** The bus does NOT validate payload shape — the
  subscriber knows which fields to read. Adding a new field is a
  subscriber change, not a bus change.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event kind constants
# ---------------------------------------------------------------------------
#
# Four kinds cover every state-change the notifier cares about:
#
#   plan_phase_changed  — plan_routing.current_phase advanced via CAS
#                          (covers ready → executing → verification_* →
#                           completed / failed / stopped).
#   task_state_changed  — per-task execution status write
#                          (covers task_started / completed / failed / timeout /
#                           status flip via ExecutionRepository.update_*).
#   vp_state_changed    — per-VP lifecycle write
#                          (covers vp_start / vp_complete / verdict recorded /
#                           progress snapshot saved / round init / round complete /
#                           verification stopped).
#   plan_closed         — terminal reached. The notifier flushes a final card
#                          immediately, bypassing the coalesce window, then
#                          evicts the in-memory plan state.
#   stale_refresh       — NOT a state change. The notifier's own watchdog
#                          raises it when a worker has been in flight
#                          longer than a push should have gone out and
#                          the rendered card would be byte-identical to
#                          the last one. It exists because the dedup
#                          that keeps the chat quiet also silences
#                          genuine progress that renders to no visible
#                          change — see ``_watch_stale_cards`` in
#                          ``notifications/feishu_notifier``.

KIND_PLAN_PHASE_CHANGED = "plan_phase_changed"
KIND_TASK_STATE_CHANGED = "task_state_changed"
KIND_VP_STATE_CHANGED = "vp_state_changed"
KIND_PLAN_CLOSED = "plan_closed"
KIND_STALE_REFRESH = "stale_refresh"


# ---------------------------------------------------------------------------
# Event payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateEvent:
    """A single state-change notification.

    `kind` is one of the four `KIND_*` constants above. `plan_id`
    identifies the affected plan. `payload` carries kind-specific
    metadata (phase name, task id, verdict id, terminal reason, etc.)
    — the bus does not interpret it.

    `ts` is wall-clock seconds at construction; used by subscribers
    for staleness / coalescing decisions. Frozen so a subscriber
    can't mutate the event mid-dispatch.
    """

    kind: str
    plan_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


Handler = Callable[[StateEvent], None]


class StateEventBus:
    """In-process pub/sub for backend state changes.

    Subscribers are callables taking a single ``StateEvent``. They
    must be idempotent and thread-safe — the bus does not guarantee
    delivery ordering across multiple subscribers, and a handler
    that raises is logged + counted but does NOT block the others.

    The bus is intentionally tiny (no retry queue, no persistence,
    no cross-process bridge). Subscribers that need any of those
    build them on top: ``FeishuNotifier`` keeps its own bounded
    ``queue.Queue`` for back-pressure.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: List[Handler] = []
        self._published = 0
        self._handler_errors = 0

    def subscribe(self, handler: Handler) -> None:
        """Register a handler. Idempotent: subscribing the same
        callable twice is a no-op (the operator can re-subscribe
        during lifespan restart without producing double pushes)."""
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        """Remove a handler. Silently ignores unknown callables."""
        with self._lock:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass

    def publish(self, event: StateEvent) -> None:
        """Dispatch ``event`` to every subscriber.

        Each handler runs under the bus lock so a single slow
        handler can't starve the others; but the lock is RELEASED
        between handlers so a crashed handler doesn't pin the
        others. Handler exceptions are caught, logged at ERROR,
        and counted under ``stats()['handler_errors']``.

        Never raises — repositories call this from inside ``_txn``
        and a raise there would roll back state changes the
        operator already approved.
        """
        with self._lock:
            self._published += 1
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                with self._lock:
                    self._handler_errors += 1
                logger.exception(
                    "[state_events] handler %s raised on %s for plan=%s",
                    getattr(handler, "__qualname__", repr(handler)),
                    event.kind,
                    event.plan_id,
                )

    def stats(self) -> Dict[str, int]:
        """Snapshot counters for the /api/debug/notifications endpoint."""
        with self._lock:
            return {
                "published": self._published,
                "handler_errors": self._handler_errors,
                "subscribers": len(self._handlers),
            }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
#
# Repositories import `publish_safe` directly (one symbol), not the
# bus, so they never see a partially-initialised object and we can
# add metrics or tracing in one place without touching every caller.
# Subscribers (notifier, tests) import `STATE_EVENT_BUS` and call
# `.subscribe()` on it.

STATE_EVENT_BUS = StateEventBus()


def publish_safe(kind: str, plan_id: str, **payload: Any) -> None:
    """Fire a `StateEvent` from inside a repository write.

    Repository write methods call this AFTER the SQLite `_txn` block
    returns. Failure modes — handler crash, queue full, bus in a
    bad state — are all caught and logged but never propagated.

    Args:
        kind: One of the four `KIND_*` constants.
        plan_id: The plan whose state changed.
        **payload: Kind-specific metadata. Typical keys: ``phase``,
            ``previous_phase``, ``task_id``, ``status``, ``round_id``,
            ``verdict``, ``terminal_reason``.
    """
    try:
        STATE_EVENT_BUS.publish(
            StateEvent(kind=kind, plan_id=plan_id, payload=dict(payload))
        )
    except Exception:
        # Belt-and-suspenders: StateEventBus.publish already catches
        # handler exceptions, but if the bus itself raises (e.g. a
        # future change to its constructor), we still must not let
        # that surface to the repository caller — that would roll
        # back a state transition the operator already approved.
        logger.exception(
            "[state_events] publish_safe swallowed exception for kind=%s plan=%s",
            kind,
            plan_id,
        )