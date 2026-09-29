"""Runtime state dataclass for the FastAPI lifespan and the
verification executor.

This module pins two contracts behind a single :class:`RuntimeState`
dataclass so the FastAPI request loop and the verification executor
share one well-typed handle:

1. **Verification executor state** — the in-memory bookkeeping
   (``pending_vps`` queue, future runtime knobs such as
   ``force_recheck``) the executor persists into the SQLite
   ``plan_verification.runtime_state`` JSON column. This is the
   shape the executor's :func:`from_dict` / :meth:`to_dict`
   round-trip contract depends on.

2. **Dependency-injection handle** — the four orthogonal
   collaborators the FastAPI lifespan constructs and exposes on
   ``app.state.runtime_state``:

     * :attr:`config` — the parsed ``backend/config.yaml`` dict
       (server-wide config consumed by providers and the executor).
     * :attr:`provider_order_file` — absolute :class:`pathlib.Path`
       to ``provider-order.json``, resolved through
       :func:`config_paths.resolve_provider_order_file`.
     * :attr:`in_flight_guard` — :class:`scheduling.guard.InFlightFileGuard`
       instance owned by the lifespan.
     * :attr:`dynamic_tracker` — :class:`dynamic_provider_concurrency.ActiveConcurrencyTracker`
       instance owned by the lifespan.

   The :class:`scheduling.dispatcher.SchedulingDispatcher` and
   :class:`backend.agent.AutonomousAgent` collaborators reach these
   collaborators through the same handle instead of importing
   module-level singletons.

Construction
------------
The dataclass defaults to ``pending_vps == []`` and ``extra == {}``
(matching the executor's fallback when the SQLite column is
missing the key) and ``None`` for the four DI fields. The FastAPI
lifespan is the canonical constructor for the DI fields — any
request handler or downstream collaborator that needs the handle
reads it from ``app.state.runtime_state``.

Backward compatibility
----------------------
A fresh ``RuntimeState()`` keeps the pre-refactor executor contract
intact: ``to_dict`` / :func:`from_dict` round-trip through JSON
unchanged, ``extra`` absorbs unknown keys for forward compatibility,
and :attr:`pending_vps` is a mutable list the executor appends /
removes in place.

Public surface
--------------

  * :class:`RuntimeState` — the dataclass itself.
  * :data:`RUNTIME_STATE_VERSION` — the integer schema version
    pinned by ``to_dict``. Bump when the JSON shape changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["RuntimeState", "RUNTIME_STATE_VERSION"]

#: Schema version pinned by :meth:`RuntimeState.to_dict`. Bump whenever
#: the JSON envelope gains / renames a top-level key, so migration
#: code can branch on it without guessing at field presence.
RUNTIME_STATE_VERSION: int = 1


@dataclass
class RuntimeState:
    """Typed handle on the lifespan-managed runtime collaborators.

    Attributes:
        pending_vps: VP ids still awaiting execution. Mirrors the
            executor's ``self._pending_vps`` and is the only field
            actively read by the live production recovery path
            (``verification_executor.py:702``). New VPs appended by
            ``mark_pending`` / removed by ``mark_completed`` mutate
            this list in place; no copy-on-construct coercion.
        extra: Catch-all for forward-compatible fields. Unknown keys
            found by :meth:`from_dict` land here, and the contents
            are merged back into :meth:`to_dict`'s output so a newer
            writer's metadata is preserved verbatim across a
            round-trip through an older reader. Callers should prefer
            a typed attribute for fields they own; this is purely a
            safety net for fields this version of the dataclass
            doesn't model yet.
        config: Parsed ``backend/config.yaml`` dict (typically the
            output of :func:`server._load_backend_config_yaml`).
            Defaults to ``None`` so a freshly-constructed
            :class:`RuntimeState` (e.g. in unit tests) does not
            require a config. The FastAPI lifespan populates this
            during startup so request handlers can read it from
            ``app.state.runtime_state.config`` without re-importing
            the loader.
        provider_order_file: Absolute :class:`pathlib.Path` to
            ``provider-order.json``, resolved through
            :func:`config_paths.resolve_provider_order_file`.
            Defaults to ``None``; the lifespan populates it so
            ``agent.AutonomousAgent`` can pick up the resolved
            path without going through the module-level
            ``server.PROVIDER_ORDER_FILE`` singleton.
        in_flight_guard: :class:`scheduling.guard.InFlightFileGuard`
            instance owned by the FastAPI lifespan. Downstream
            collaborators reach the guard through
            ``app.state.runtime_state.in_flight_guard`` instead of
            importing the module-level singleton.
        dynamic_tracker: :class:`dynamic_provider_concurrency.ActiveConcurrencyTracker`
            instance owned by the FastAPI lifespan. The tracker is
            the single source of truth for the dynamic 5h-cap slot
            counts; the lifespan hands the same instance to
            ``agent.AutonomousAgent`` so all dispatches in this
            process observe each other's slot acquisitions.

    Construction defaults to an empty ``pending_vps`` list (matching
    the executor's fallback when the JSON column lacks the key) and
    an empty ``extra`` dict. The four DI fields default to ``None``
    so test fixtures can build a ``RuntimeState()`` without the
    FastAPI lifespan running first.
    """

    pending_vps: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    # Dependency-injection fields — populated by the FastAPI lifespan.
    config: Optional[Dict[str, Any]] = None
    provider_order_file: Optional[Path] = None
    in_flight_guard: Optional[Any] = None
    dynamic_tracker: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-friendly dict.

        The output is shaped for the SQLite ``runtime_state`` column:
        ``pending_vps`` appears as a top-level list, and any
        forward-compatible keys collected in ``extra`` are merged
        back into the top-level dict so a round-trip preserves them
        verbatim.

        ``pending_vps`` always wins over a same-named key in ``extra``
        — the dataclass owns the field and will not let unknown
        metadata shadow it.
        """
        payload: Dict[str, Any] = dict(self.extra)
        payload["pending_vps"] = list(self.pending_vps)
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RuntimeState":
        """Hydrate a :class:`RuntimeState` from a JSON-shaped dict.

        Tolerates the legacy / empty / partially-populated shapes
        the executor has historically written to the SQLite column:

          * Missing ``pending_vps`` key → defaults to ``[]`` (never
            raises ``KeyError``).
          * ``pending_vps is None`` → normalised to ``[]`` rather
            than propagated, so the executor's ``list(state.pending_vps)``
            access never blows up with ``TypeError: 'NoneType' is
            not iterable``.
          * Unknown keys → preserved in ``extra`` for forward
            compatibility (the next :meth:`to_dict` writes them
            back verbatim).
          * DI fields (``config`` / ``provider_order_file`` /
            ``in_flight_guard`` / ``dynamic_tracker``) are NOT
            round-tripped through ``to_dict`` / :func:`from_dict` —
            they live on the FastAPI lifespan's in-memory handle,
            not in the persisted JSON column, so they default to
            ``None`` on rehydrate.
        """
        if payload is None:
            payload = {}
        raw_pending = payload.get("pending_vps", [])
        if raw_pending is None:
            pending_vps: List[str] = []
        elif isinstance(raw_pending, list):
            pending_vps = list(raw_pending)
        else:
            # Defensive: an older writer may have serialised a
            # non-list (e.g. comma-joined string). Coerce to a list
            # so downstream code can rely on ``isinstance(..., list)``.
            pending_vps = [raw_pending]
        extra: Dict[str, Any] = {
            key: value for key, value in payload.items() if key != "pending_vps"
        }
        return cls(pending_vps=pending_vps, extra=extra)
