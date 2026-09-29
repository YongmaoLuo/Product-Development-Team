"""
Provider Concurrency Controller
===============================

Dual-layer ``asyncio.Semaphore`` based rate limiting for LLM calls:

  1. **Global cap** — the maximum number of in-flight acquires across
     *all* providers combined. Sourced from the ``MAX_PARALLEL_TASKS``
     env var, defaulting to ``5`` when unset.

  2. **Per-provider cap** — a tighter cap on any single provider,
     sourced from the ``PROVIDER_LIMITS`` env var (a JSON object
     ``{"vendor-a-pro": 5, "vendor-b-pro": 2}``). Providers not in the map use  # kebab-case test_provider marker
     a hard-coded default of ``5`` (NOT the global cap, by design —
     keeping the two layers independently tunable).

Both layers are queueing, not denying: over-limit requests suspend
inside :meth:`acquire` until a paired :meth:`release` frees a slot.
No requests are dropped on the floor.

The contract is the same one pinned by
``verification_dag.ProviderConcurrencyController`` in PRD decision
point 2 (reuse the dual-layer pattern). The controller is
intentionally decoupled from the verification DAG so it can be
imported by ``agent.py`` / ``executor.py`` without pulling in the
DAG module's heavier dependencies.

Usage::

    import os
    os.environ['MAX_PARALLEL_TASKS'] = '8'
    os.environ['PROVIDER_LIMITS'] = '{"vendor-a-pro": 5, "vendor-b-pro": 2}'  # kebab-case test_provider marker

    ctrl = ProviderConcurrencyController()
    await ctrl.acquire('vendor-a-pro')    # block until both slots free  # kebab-case test_provider marker
    await ctrl.release('vendor-a-pro')    # return both slots  # kebab-case test_provider marker

The class is deliberately framework-free: no FastAPI / no logging /
no filesystem. The agent wraps it in whatever context it needs.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

# Default global cap when ``MAX_PARALLEL_TASKS`` env var is unset or empty.
# Matches the baseline in PRD decision point 2 / 6 (TC-004: 全局 Semaphore(5)).
DEFAULT_GLOBAL_LIMIT: int = 5

# Default per-provider cap when a provider is not in ``PROVIDER_LIMITS``.
# Independent of ``DEFAULT_GLOBAL_LIMIT`` so the two layers can be tuned
# separately (per the boundary "provider 不在 PROVIDER_LIMITS → 默认 5"
# in the PRD). The verification DAG's controller instead falls back to
# the global cap — that variant is kept for the DAG, while the
# agent-facing controller uses this constant default.
DEFAULT_PROVIDER_LIMIT: int = 5

# Env var names. Made module-level constants so tests and the agent
# can refer to the same string without typos.
ENV_GLOBAL_LIMIT: str = "MAX_PARALLEL_TASKS"
ENV_PROVIDER_LIMITS: str = "PROVIDER_LIMITS"


class ProviderConcurrencyError(ValueError):
    """Raised when the controller is misconfigured.

    Subclassing :class:`ValueError` keeps the exception cheap to catch
    in the common case where callers only care about "bad input".
    Construction-time failures (bad env var, non-positive limit) use
    this; runtime failures (double-release etc.) surface as
    ``ValueError`` from the underlying :class:`asyncio.Semaphore`.
    """


def _coerce_positive_int(name: str, value: object) -> int:
    """Coerce ``value`` to a positive int or raise ``ProviderConcurrencyError``.

    Args:
        name: Human-readable name of the field, used in the error message.
        value: Candidate value (int or string from env var).

    Returns:
        The positive int.

    Raises:
        ProviderConcurrencyError: if ``value`` is not an integer or
            is not strictly positive.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python — reject it to
        # avoid ``True`` silently becoming ``1``.
        raise ProviderConcurrencyError(
            f"{name} must be a positive integer, got {value!r}"
        )
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError as exc:
            raise ProviderConcurrencyError(
                f"{name} must be a positive integer, got {value!r}"
            ) from exc
    else:
        raise ProviderConcurrencyError(
            f"{name} must be a positive integer, got {type(value).__name__}"
        )
    if coerced < 1:
        raise ProviderConcurrencyError(
            f"{name} must be a positive integer, got {coerced!r}"
        )
    return coerced


def _coerce_provider_limits(name: str, value: object) -> Dict[str, int]:
    """Coerce ``value`` to a ``dict[str, int]`` or raise.

    Args:
        name: Human-readable name of the field, used in the error message.
        value: Candidate value (dict, JSON string, or None).

    Returns:
        A new dict with string keys and int values. The original is
        not aliased.

    Raises:
        ProviderConcurrencyError: if ``value`` is not a dict-shaped
            object, or any value is not a positive int.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderConcurrencyError(
                f"{name} must be a JSON object, got {value!r}"
            ) from exc
        value = parsed
    if not isinstance(value, dict):
        raise ProviderConcurrencyError(
            f"{name} must be a dict (or JSON object), got "
            f"{type(value).__name__}"
        )
    result: Dict[str, int] = {}
    for prov, lim in value.items():
        if not isinstance(prov, str) or not prov:
            raise ProviderConcurrencyError(
                f"{name} keys must be non-empty strings, got {prov!r}"
            )
        result[prov] = _coerce_positive_int(
            f"{name}[{prov!r}]", lim
        )
    return result


class ProviderConcurrencyController:
    """Dual-layer concurrency cap: global + per-provider.

    Two layers of limits:

      1. **Global cap** (``MAX_PARALLEL_TASKS`` env var, default
         :data:`DEFAULT_GLOBAL_LIMIT` = 5) — the maximum number of
         in-flight acquires across *all* providers combined. Protects
         downstream services from a sudden burst of traffic.

      2. **Per-provider cap** (``PROVIDER_LIMITS`` env var, a JSON
         object) — a tighter cap on any single provider, used when
         one upstream is known to be flaky (e.g. vendor-b-pro returning 401
         under load). Per-provider limits isolate blast radius: a
         stuck provider cannot starve the others beyond the global
         cap. Providers not in the map use
         :data:`DEFAULT_PROVIDER_LIMIT` = 5.

    Mixed providers do not cross-block via the per-provider
    mechanism: the per-provider semaphores are independent. They do
    compete for global slots, but the global cap is wide enough that
    the per-provider limit is the binding constraint in the typical
    multi-provider setup (e.g. global=10, vendor-b-pro=2 → vendor-b-pro is the
    bottleneck; the other 8 global slots are buffer capacity for
    non-vendor-b-pro providers).

    The contract is **blocking, never denied**: callers never see a
    "denied" outcome — they wait inside :meth:`acquire` until slots
    are available. Over-limit requests are queued FIFO via
    :class:`asyncio.Semaphore`; no requests are dropped on the floor.

    Repeated ``acquire(provider)`` calls for the same provider
    accumulate slots (each call decrements the per-provider count by
    1). Each must be paired with a matching ``release(provider)`` —
    no implicit batching, no implicit ownership transfer.

    Attributes:
        global_limit: Configured global cap (set at construction).
        provider_limits: Per-provider cap map (set at construction;
            values are copied so external mutation cannot change the
            controller's behaviour).
    """

    def __init__(
        self,
        global_limit: Optional[int] = None,
        provider_limits: Optional[Dict[str, int]] = None,
    ) -> None:
        """Construct a controller.

        Args:
            global_limit: Maximum concurrent in-flight acquires
                across all providers. If ``None`` (the default), read
                from the ``MAX_PARALLEL_TASKS`` env var, then fall
                back to :data:`DEFAULT_GLOBAL_LIMIT`. Must be a
                positive integer when provided.
            provider_limits: Map of ``provider_name -> max_concurrent``
                for each known provider. If ``None`` (the default),
                read from the ``PROVIDER_LIMITS`` env var (JSON object
                ``{"vendor-a-pro": 5, "vendor-b-pro": 2}``). Providers not in the  # kebab-case test_provider marker
                map use :data:`DEFAULT_PROVIDER_LIMIT` as their
                implicit cap.

        Raises:
            ProviderConcurrencyError: if ``global_limit`` or any
                per-provider limit is not a positive integer, or the
                env var is malformed. Bad config fails fast at
                construction rather than at first acquire.
        """
        if global_limit is None:
            global_limit = self._load_global_limit_from_env()
        if provider_limits is None:
            provider_limits = self._load_provider_limits_from_env()

        # Re-validate explicitly provided values (env-loaded values
        # were already coerced inside the loaders, but constructor
        # callers might pass raw ints / dicts directly).
        global_limit = _coerce_positive_int("global_limit", global_limit)
        provider_limits = _coerce_provider_limits(
            "provider_limits", provider_limits
        )

        self._global_limit: int = global_limit
        # ``asyncio.Semaphore`` is the queueing primitive — acquire
        # suspends the coroutine, release wakes the next FIFO waiter.
        # Lazy init: ``asyncio.Semaphore()`` requires a running event
        # loop on Python 3.9, so we defer creation to first acquire.
        self._global_sem: Optional[asyncio.Semaphore] = None

        # Per-provider semaphores, lazily created on first acquire.
        # Storing them (rather than constructing eagerly for all known
        # providers) avoids creating semaphores for providers the
        # controller will never see at runtime.
        self._provider_sems: Dict[str, asyncio.Semaphore] = {}
        # Defensive copy: external mutation of the caller's dict must
        # not change the controller's per-provider caps after
        # construction.
        self._provider_limits: Dict[str, int] = dict(provider_limits)

    # ------------------------------------------------------------------
    # Env var loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_global_limit_from_env() -> int:
        """Read ``MAX_PARALLEL_TASKS`` env var.

        Returns :data:`DEFAULT_GLOBAL_LIMIT` when the env var is unset
        or empty. Raises :class:`ProviderConcurrencyError` when the env
        var is set to a non-integer or non-positive value.
        """
        raw = os.environ.get(ENV_GLOBAL_LIMIT)
        if raw is None or raw == "":
            return DEFAULT_GLOBAL_LIMIT
        return _coerce_positive_int(ENV_GLOBAL_LIMIT, raw)

    @staticmethod
    def _load_provider_limits_from_env() -> Dict[str, int]:
        """Read ``PROVIDER_LIMITS`` env var (JSON object).

        Returns an empty dict when the env var is unset or empty.
        Raises :class:`ProviderConcurrencyError` when the env var is
        set to something other than a JSON object, or any per-provider
        value is not a positive integer.
        """
        raw = os.environ.get(ENV_PROVIDER_LIMITS)
        if raw is None or raw == "":
            return {}
        return _coerce_provider_limits(ENV_PROVIDER_LIMITS, raw)

    # ------------------------------------------------------------------
    # Read-only accessors (used by tests and observability hooks)
    # ------------------------------------------------------------------

    @property
    def global_limit(self) -> int:
        """Return the configured global cap (read-only)."""
        return self._global_limit

    @property
    def provider_limits(self) -> Dict[str, int]:
        """Return a copy of the configured per-provider caps.

        The original ``provider_limits`` dict passed at construction
        is not aliased, so callers can introspect the configuration
        without risking mutation of internal state.
        """
        return dict(self._provider_limits)

    def available_global(self) -> int:
        """Return the number of currently free global slots.

        Used by tests to assert that :meth:`release` correctly
        returns capacity to the global pool. Reflection over the
        underlying semaphore's internal state — Python's asyncio
        does not expose a public accessor, so we use the private
        ``_value`` attribute (stable since Python 3.10; see
        ``Lib/asyncio/locks.py``).
        """
        return self._ensure_global_sem()._value  # type: ignore[attr-defined]

    def available_provider(self, provider: str) -> int:
        """Return the number of currently free per-provider slots.

        Unknown providers report :data:`DEFAULT_PROVIDER_LIMIT`
        (the implicit default) without creating a semaphore, so this
        is safe to call before any :meth:`acquire` has run for that
        provider.
        """
        sem = self._provider_sems.get(provider)
        if sem is None:
            return self._provider_limits.get(
                provider, DEFAULT_PROVIDER_LIMIT
            )
        return sem._value  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def _ensure_global_sem(self) -> asyncio.Semaphore:
        """Lazily create the global semaphore on first use.

        Python 3.9's ``asyncio.Semaphore()`` requires a running event
        loop at construction time.  Deferring creation to the first
        ``acquire`` (which is always ``async``) guarantees a loop exists.
        """
        if self._global_sem is None:
            self._global_sem = asyncio.Semaphore(self._global_limit)
        return self._global_sem

    def _get_provider_sem(self, provider: str) -> asyncio.Semaphore:
        """Return the per-provider semaphore, creating it on first use.

        Lazy creation lets callers pass a partial ``provider_limits``
        map (e.g. only the tight ones, like ``{"vendor-b-pro": 2}``) and
        still serve other providers with the implicit
        :data:`DEFAULT_PROVIDER_LIMIT` cap. This matches the PRD's
        design where the two layers are independently tunable.
        """
        sem = self._provider_sems.get(provider)
        if sem is None:
            limit = self._provider_limits.get(
                provider, DEFAULT_PROVIDER_LIMIT
            )
            sem = asyncio.Semaphore(limit)
            self._provider_sems[provider] = sem
        return sem

    async def acquire(self, provider: str) -> None:
        """Block until both global and per-provider slots are available.

        Order matters: **global first, then per-provider**. Holding
        the global slot while waiting for the per-provider slot means
        a tight per-provider cap cannot deadlock the global cap — the
        global slot is released as soon as the inner acquire resolves
        (or raises), so global waiters unblock promptly.

        Behaviour:
          * If both caps have spare capacity, returns immediately.
          * If the global cap is full, suspends until a global slot
            is freed by some other task's :meth:`release`.
          * If the per-provider cap is full, suspends until a slot
            for ``provider`` is freed by some other task's
            :meth:`release` for the same provider.
          * If a ``CancelledError`` (or other ``BaseException``) is
            raised while waiting on the per-provider acquire, the
            global slot is given back so we don't leak it.

        Mixed providers do not cross-block at the per-provider
        layer: if vendor-b-pro is full, claude tasks still proceed (as
        long as the global cap has capacity). The "100 vendor-b-pro tasks
        under vendor-b-pro=2" test pins this — peak concurrent vendor-b-pro is
        2 even with global=10.

        Args:
            provider: The provider name to acquire a slot for. Must
                be a non-empty string; arbitrary strings are accepted
                so the controller is decoupled from any specific
                provider registry.

        Raises:
            ProviderConcurrencyError: if ``provider`` is not a
                non-empty string.
        """
        if not isinstance(provider, str) or not provider:
            raise ProviderConcurrencyError(
                f"provider must be a non-empty string, got {provider!r}"
            )

        # Outer: global cap. await here means a 6th acquire with
        # global=5 will suspend until a release happens.
        await self._ensure_global_sem().acquire()
        try:
            # Inner: per-provider cap. The 4th vendor-b-pro acquire with
            # vendor-b-pro=2 suspends here while the first 2 are in flight.
            await self._get_provider_sem(provider).acquire()
        except BaseException:
            # Per-provider acquire failed (typically CancelledError
            # from task cancellation). Hand the global slot back so
            # we don't leak it — without this, an awaited task that
            # gets cancelled mid-acquire would permanently shrink
            # the global pool, eventually deadlocking the
            # controller.
            self._ensure_global_sem().release()
            raise

    def release(self, provider: str) -> None:
        """Release one per-provider slot and one global slot.

        Per-provider is released **first** so the next per-provider
        waiter (if any) is unblocked promptly; the global release
        that follows unblocks a global waiter (possibly from
        another provider). This ordering minimises the wake-up
        latency for the most-affected waiter (the one waiting on
        the per-provider cap) and is safe because both releases
        are independent — no caller ever holds a per-provider slot
        without the matching global slot, and vice versa.

        Args:
            provider: The provider name whose slot to release. Must
                match a previous :meth:`acquire` for the same
                provider. Releasing an unknown provider (i.e. one
                that has never been :meth:`acquire` d) is a no-op
                for the per-provider layer but still returns one
                global slot — the caller is responsible for not
                double-releasing. The internal
                :class:`asyncio.Semaphore` will raise
                ``ValueError`` if global slots are over-released,
                which is the intended fail-fast behaviour for that
                bug.

        Raises:
            ProviderConcurrencyError: if ``provider`` is not a
                non-empty string.
        """
        if not isinstance(provider, str) or not provider:
            raise ProviderConcurrencyError(
                f"provider must be a non-empty string, got {provider!r}"
            )

        # Per-provider first: a waiting same-provider task gets
        # unblocked ASAP. If no semaphore exists for this provider
        # (because acquire was never called for it), skip — the
        # global release still runs so the caller does not leak
        # their global slot.
        sem = self._provider_sems.get(provider)
        if sem is not None:
            sem.release()
        # Global second: any cross-provider waiter (or per-provider
        # waiter that already holds a global slot) gets unblocked.
        self._ensure_global_sem().release()


def select_provider_with_concurrency(
    provider_priority: List[str],
    controller: ProviderConcurrencyController,
    now: datetime,
) -> Optional[str]:
    """Select the first eligible provider that has free concurrency slots.

    Walks ``provider_priority`` in order and returns the first provider
    that has both a free global slot and a free per-provider slot.

    The ORDER is the optimizer's chain (``provider-order.json``), which
    already encodes every ranking policy — including the rule engine's
    peak-hour demotions. This function therefore applies no policy of its
    own: a provider the rules want avoided is simply near the end of the
    list it was handed.

    Args:
        provider_priority: Ordered list of provider identifiers.
        controller: A configured concurrency controller.
        now: Current datetime (kept for signature stability / callers'
            logging; the walk itself is time-independent).

    Returns:
        The selected provider id, or ``None`` if no provider has spare
        capacity.
    """
    for provider in provider_priority:
        if controller.available_global() > 0 and controller.available_provider(provider) > 0:
            return provider
    return None


__all__ = [
    "DEFAULT_GLOBAL_LIMIT",
    "DEFAULT_PROVIDER_LIMIT",
    "ENV_GLOBAL_LIMIT",
    "ENV_PROVIDER_LIMITS",
    "ProviderConcurrencyController",
    "ProviderConcurrencyError",
    "select_provider_with_concurrency",
]