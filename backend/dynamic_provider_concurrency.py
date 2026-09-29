"""
Dynamic per-provider concurrency limits scaled by 5-hour remaining quota.

The :class:`provider_concurrency.ProviderConcurrencyController` uses static
per-provider caps (env-var driven). This module extends the model with
**dynamic caps** computed from each provider's 5-hour quota remaining:

  * 100% remaining → the provider's configured cap
  * 60% remaining  → round-half-up(cap * 0.6)
  * 0% remaining   → 0 (caller falls back to parent config)

The configured cap is resolved through :mod:`provider_capacity`, which
reads regex rules over provider names from ``provider_capacity.yaml`` and
applies one documented default to anything no rule matches. Caps are
therefore a property of the deployment, not of this module.

The selection contract is decoupled from the slot bookkeeping:

  * :func:`compute_dynamic_limit` — pure function: provider + 5h pct → int.
  * :class:`ActiveConcurrencyTracker` — thread-safe per-provider count of
    in-flight subagents.
  * :func:`select_provider_with_dynamic_capacity` — walks the fallback
    chain and returns the first provider with spare capacity, or ``None``.
  * :func:`acquired_provider_slot` — context manager that increments the
    tracker on enter and decrements on exit (even when the inner block
    raises).

Why this lives in its own module
--------------------------------
``provider_concurrency.py`` is the dual-layer semaphore primitive used
inside the verification DAG. Mixing the dynamic-5h-cap selection rule
into it would force the DAG to know about CC Switch quota tiers. The two
modules are kept orthogonal: ``ProviderConcurrencyController`` keeps
doing what it does (queueing over a fixed cap), and this module
introduces the dynamic selection rule.

Where the fleet ceiling comes from
----------------------------------
This module deliberately keeps **no** fleet-wide number. The effective
concurrency of an installation is the sum of the caps of the providers
that are actually callable, and that sum is produced by the per-provider
caps plus one fact the dispatch layer already enforces: a provider with
no reachable CC Switch row never takes a slot (see
:func:`agent.select_provider_with_dynamic_capacity`). A provider that
cannot be called therefore contributes 0, and the total falls out of the
configured caps rather than being asserted alongside them.

Rounding rule
-------------
Rounding is half-up (0.5 → 1, 1.5 → 2), implemented via
``int(math.floor(x + 0.5))`` rather than Python's built-in :func:`round`,
which uses banker's rounding and would produce 2 for 2.5.

The ``parent`` entry
--------------------
``provider-order.json`` chains end with a literal ``"parent"`` entry.
``parent`` is the caller's explicit fallback (the SubagentConfig
inherits the parent process's CC Switch proxy config). The dynamic
selection strategy MUST NOT pick ``parent`` — if no real provider has
capacity the function returns ``None`` and the caller decides what to
do (today: fall back to ``parent`` env config).
"""

from __future__ import annotations

import math
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence

import provider_capacity


#: Re-exported so the default has one definition. A provider no rule in
#: ``provider_capacity.yaml`` matches gets this cap.
DEFAULT_MAX_CONCURRENCY_AT_FULL: int = provider_capacity.DEFAULT_MAX_CONCURRENCY


def canonical_capacity_key(provider: str) -> str:
    """Normalise a provider reference to its slot-bookkeeping key.

    Two naming schemes reach this module and they must agree, because
    they describe the same provider:

      * CC Switch display names (``"Vendor A Pro"``) — what
        ``provider-order.json`` carries and what
        ``provider_routing.resolve_provider_chain`` yields, i.e. what
        almost every caller passes;
      * kebab-case forms of the same names (``"vendor-a-pro"``).

    Normalisation is deliberately **mechanical** (casefold, whitespace
    and underscores to ``-``, collapse runs) rather than a
    name→logical-ID lookup table. ``cc_switch`` owns a table like that,
    and it is right for *config resolution* — two rows configured the
    same way may share a lookup answer — but wrong for *capacity*: two
    CC Switch rows are two quota pools, each with its own
    ``ANTHROPIC_AUTH_TOKEN``, and collapsing them onto one key would
    give two providers one shared slot counter. Mechanical
    normalisation keeps ``"Vendor A"`` and ``"Vendor A Pro"`` apart,
    which is what independent pools require.

    The cost is that a display name and some other spelling of the same
    pool can diverge; the fix is to pass one spelling consistently, not
    to reintroduce a table here.
    """
    raw = (provider or "").strip()
    key = re.sub(r"[\s_]+", "-", raw.casefold())
    return re.sub(r"-+", "-", key).strip("-")


class DynamicProviderSelectionError(ValueError):
    """Raised when the dynamic selection module is misconfigured.

    Construction-time failures (bad provider id, negative remaining)
    use this; runtime failures surface as ``None`` returns (caller
    decides on the parent-process fallback). Subclassing
    :class:`ValueError` keeps the exception cheap to catch in the
    common case where callers only care about "bad input".
    """


def round_half_up(x: float) -> int:
    """Return ``x`` rounded half-up (0.5 → 1, 1.5 → 2, 2.5 → 3).

    Differs from Python's :func:`round`, which uses banker's rounding
    (half-to-even) and would return ``2`` for ``2.5``. Half-up is the
    convention this module's scaling rule is defined in.

    Negative inputs are floored toward zero (``round_half_up(-0.5) ==
    0``) — the dynamic-limit caller is expected to clamp negatives
    before this function sees them, but the behaviour is defined here
    so a bad caller does not crash.
    """
    if x >= 0:
        return int(math.floor(x + 0.5))
    # For negatives, round_half_up toward zero (the math.floor of a
    # negative plus 0.5 would round away from zero, which is the wrong
    # convention for our use case where the value is clamped >= 0).
    return -int(math.floor(-x + 0.5))


def compute_dynamic_limit(provider: str, remaining_pct: float) -> int:
    """Return the dynamic max concurrency for ``provider``.

    The cap scales linearly with the provider's 5-hour remaining
    percentage, rounded half-up. The result is clamped to
    ``[0, base_cap]`` so a bad CC Switch payload (negative or >100)
    cannot widen the cap beyond the design intent.

    ``base_cap`` comes from :func:`provider_capacity.capacity_for` — the
    regex rules in ``provider_capacity.yaml``, with one documented
    default for providers no rule matches.

    Args:
        provider: Provider reference. Any spelling is accepted; the
            configured rules are matched case-insensitively against both
            the name as given and its canonical form.
        remaining_pct: 5-hour quota remaining, as a percentage in
            ``[0, 100]``. Values outside this range are clamped.

    Returns:
        Non-negative integer cap. ``0`` means "no capacity — caller
        must skip this provider".

    Raises:
        DynamicProviderSelectionError: if ``provider`` is not a
            non-empty string.
    """
    if not isinstance(provider, str) or not provider:
        raise DynamicProviderSelectionError(
            f"provider must be a non-empty string, got {provider!r}"
        )

    base = provider_capacity.capacity_for(provider)

    # Clamp the input percentage. A negative pct is treated as "exhausted"
    # (limit 0) so a bad CC Switch payload cannot grant free capacity.
    # A >100 pct is treated as "fully available" (cap = base) so a
    # refresh glitch cannot widen the cap beyond its design maximum.
    if remaining_pct <= 0:
        return 0
    if remaining_pct >= 100:
        return base

    scaled = base * (remaining_pct / 100.0)
    return round_half_up(scaled)


class ActiveConcurrencyTracker:
    """Thread-safe per-provider active-count tracker.

    Records how many subagents are currently in flight against each
    provider. Used by
    :func:`select_provider_with_dynamic_capacity` to decide whether a
    provider has spare capacity under its dynamic limit.

    Locking model
    -------------
    A single :class:`threading.Lock` serializes all mutations and reads.
    The critical section is bounded by the duration of a dict update
    (a few Python bytecodes), so contention is negligible relative to
    the LLM call latency this module gates.

    Defensive clamps
    ----------------
    * :meth:`release` clamps the post-decrement count at ``0`` so an
      accidental double-release cannot yield a negative count.
    * The ``current`` view returned by :meth:`current` is a snapshot;
      callers needing an atomic check-then-act must use
      :meth:`try_acquire` (or the :func:`acquired_provider_slot` context
      manager), which do the check and the increment under one lock.

    Keys
    ----
    Every key is passed through :func:`canonical_capacity_key`, so
    ``current("Vendor A Pro")`` and ``current("vendor-a-pro")`` describe
    the same provider even though the scene path and the executor path
    spell it differently.

    There is deliberately **no fleet-wide ceiling here**. The tracker
    counts slots; it does not decide how many the installation may open.
    That number is the sum of the per-provider caps in
    ``provider_capacity.yaml`` over the providers that are actually
    callable, and a provider with no reachable CC Switch row never
    reaches :meth:`try_acquire` at all — the dispatch layer validates
    the row and releases the slot when it fails. A ceiling asserted here
    would therefore be a second copy of a number the configuration
    already determines, and the two copies drift the first time a
    provider is added or retired.
    """

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}
        self._lock = threading.Lock()
        # Round-robin cursor (2026-09-22). Monotonic; readers take it
        # modulo the live candidate count, so a changing pool size does
        # not have to be coordinated with the counter.
        self._rotation_cursor = 0

    def acquire(self, provider: str) -> int:
        """Increment the active count for ``provider`` by 1.

        Unconditional — it ignores the per-provider limit. Prefer
        :meth:`try_acquire` on any path where something else may be
        holding slots; this remains for callers that have already
        decided to run (tests, back-compat) and for the ``parent``
        sentinel, which is not a dispatch decision.

        Returns the post-mutation count (useful for logging the new
        depth at acquisition time).
        """
        key = canonical_capacity_key(provider)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]

    def try_acquire(self, provider: str, limit: int) -> bool:
        """Atomically take a slot for ``provider`` if one is free.

        The check and the increment happen under a single lock — the
        point of this method. A ``current() < limit`` test followed by a
        separate ``acquire()`` races: with a dozen verification VPs
        polling the same provider, several would pass the check before
        any of them incremented, and the cap would be exceeded by
        exactly the amount it exists to prevent.

        Args:
            provider: Provider reference (any spelling).
            limit: Per-provider cap for this call. The caller computes
                it with :func:`compute_dynamic_limit` so the 5h-quota
                scaling stays outside the lock.

        Returns:
            ``True`` when the slot was taken, ``False`` when the
            provider is already at ``limit``. A ``limit <= 0`` is never
            acquired.
        """
        if limit <= 0:
            return False
        key = canonical_capacity_key(provider)
        with self._lock:
            if self._counts.get(key, 0) >= limit:
                return False
            self._counts[key] = self._counts.get(key, 0) + 1
            return True

    def release(self, provider: str) -> int:
        """Decrement the active count for ``provider`` by 1.

        Clamps at ``0`` so an over-release cannot leak a negative
        count into the next decision round. Returns the post-mutation
        count.
        """
        key = canonical_capacity_key(provider)
        with self._lock:
            current = self._counts.get(key, 0)
            new = max(0, current - 1)
            self._counts[key] = new
            return new

    def current(self, provider: str) -> int:
        """Return the current active count for ``provider``.

        ``0`` for any provider that has never been acquired. Thread-safe
        via the same lock used by :meth:`acquire` / :meth:`release`.
        """
        with self._lock:
            return self._counts.get(canonical_capacity_key(provider), 0)

    def total(self) -> int:
        """Return the fleet-wide in-flight count across every provider."""
        with self._lock:
            return sum(self._counts.values())

    def snapshot(self) -> Dict[str, int]:
        """Return a defensive copy of the per-provider counts.

        Useful for observability hooks (logging the active depth at
        decision time) and for tests that want to assert the entire
        state in one assertion.
        """
        with self._lock:
            return dict(self._counts)

    def next_rotation_index(self, count: int) -> int:
        """Return this call's starting slot in a round-robin of ``count``.

        Fetch-and-advance under the tracker's lock, so two dispatchers
        racing here get DIFFERENT slots and the work spreads across the
        pool instead of both aiming at the first entry.

        The cursor is monotonic and only reduced modulo ``count`` at
        read time, so a candidate set that shrinks or grows (a provider
        parked, another recovering) needs no coordination with it.
        """
        if count <= 0:
            return 0
        with self._lock:
            start = self._rotation_cursor % count
            self._rotation_cursor += 1
            return start


def select_provider_with_dynamic_capacity(
    provider_priority: List[str],
    tracker: ActiveConcurrencyTracker,
    usage_map: Dict[str, float],
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return the first provider in ``provider_priority`` with capacity.

    Walks ``provider_priority`` in order. A provider is *eligible* when:

      1. It is not the literal ``"parent"`` entry (parent is the
         caller's explicit fallback, not this strategy's choice).
      2. It is in the order it was handed — the optimizer's chain, whose
         rule engine already applied any peak-hour demotion.
      3. ``tracker.current(provider)`` is strictly less than
         :func:`compute_dynamic_limit` for the provider's current
         5-hour remaining percentage.

    If no entry satisfies all three, the function returns ``None``.
    The caller is then expected to fall back to the parent process's
    CC Switch proxy config — that fallback is NOT this function's
    responsibility (it would conflate "no provider has capacity" with
    "subagent should inherit parent proxy", which are deliberately
    separate decisions).

    Args:
        provider_priority: Ordered list of logical provider IDs from
            :func:`provider_order.load_fallback_order`. May include the
            ``"parent"`` sentinel at the end.
        tracker: An :class:`ActiveConcurrencyTracker` recording the
            current active count per provider.
        usage_map: Per-provider 5-hour remaining percentage. Missing
            entries default to 100% (the provider is treated as having
            full quota).
        now: Current datetime. ``None`` defers to
            :func:`datetime.datetime.now`. Timezone-aware or naive; also
            used to expire stale rule verdicts.

    Returns:
        The selected provider id, or ``None`` when the chain has no
        eligible entry.
    """
    if not provider_priority:
        return None

    if now is None:
        now = datetime.now()

    for provider in provider_priority:
        # 1. ``parent`` is never picked by the dynamic strategy.
        if provider == "parent":
            continue

        # 2. Dynamic capacity check.
        remaining_pct = usage_map.get(provider, 100.0)
        limit = compute_dynamic_limit(provider, remaining_pct)
        if tracker.current(provider) < limit:
            return provider

    return None


def acquire_provider_with_dynamic_capacity(
    providers: Sequence[str],
    tracker: ActiveConcurrencyTracker,
    usage_map: Dict[str, float],
    *,
    timeout: float = 300.0,
    poll_interval: float = 0.5,
) -> Optional[str]:
    """Block until one of ``providers`` has a free slot; return its name.

    Delegation is driven by *local slot exhaustion*, not by reaching a
    provider and failing. Being able to call a provider is not the same
    as being allowed to pile more work onto it: once a provider is at
    its cap, the pressure of adding another concurrent agent is felt by
    that provider's quota, not by the caller. So when every candidate is
    full the caller waits here instead of stacking more onto the first
    entry.

    Round-robin across the EFFECTIVE candidates
    -------------------------------------------
    Work is spread in proportion to how many providers are actually
    callable in the current tier, so one provider's quota is not drained
    before the next one is tried at all — without the rotation, a tier
    whose first entry has cap 10 takes the first ten dispatches before
    the second entry sees one.

    The walk therefore STARTS at a rotating slot instead of always at
    index 0, and the rotation is taken over the candidates that are
    actually callable — parked providers and quota-exhausted ones are
    filtered out first, so "3 declared, 1 unreachable" rotates over the
    2 real ones rather than leaving a dead slot in the cycle.

    Concretely, with two effective providers: dispatch 1 → provider A,
    2 → B, 3 → A, 4 → B. Without the rotation, dispatches 1..10 would
    all land on A (at cap 10) and only dispatch 11 reach B.

    Contrast with :func:`select_provider_with_dynamic_capacity`, which
    only *reports* the first provider with room and leaves the slot
    bookkeeping to the caller. That split is a race: under a dozen
    concurrent VPs, several callers observe the same free slot and all
    take it. This function does check-and-take atomically via
    :meth:`ActiveConcurrencyTracker.try_acquire`.

    Args:
        providers: Ordered candidate names, exactly as the caller spells
            them (the return value is one of these, so the caller can
            release the same slot). ``"parent"`` is skipped — it is the
            caller's explicit fallback, not a pool.
        tracker: The shared :class:`ActiveConcurrencyTracker`.
        usage_map: Per-provider 5-hour remaining percentage. Missing
            entries default to 100%.
        timeout: Seconds to keep polling before giving up. The default
            (300 s) is well inside the 1800 s executor task timeout and
            the 900 s default total timeout, so queueing cannot by
            itself trip a timeout.
        poll_interval: Seconds between walks. A full fleet is usually
            waiting on an LLM call that takes tens of seconds, so a
            sub-second poll just burns CPU.

    Returns:
        The name from ``providers`` whose slot was taken, or ``None``
        when the chain had no *eligible* provider at all (every entry
        exhausted its 5h quota, or the list was empty) or the wait timed
        out. ``None`` is not an error: the caller falls back to its
        documented parent-process behaviour.
    """
    if not providers:
        return None

    deadline = time.monotonic() + max(0.0, timeout)
    cooldown = get_shared_cooldown()

    while True:
        # 1. The EFFECTIVE candidate set — what can actually be called.
        #    Filtering first (rather than skipping inside the walk) is
        #    what makes the rotation divide work by the number of usable
        #    providers: a chain of three with one dead provider rotates
        #    over two, with no empty slot in the cycle.
        candidates: List[str] = []
        for name in providers:
            if name == "parent":
                # ``parent`` is the caller's explicit fallback, not a
                # pool, and it must never occupy a rotation slot.
                continue
            if cooldown.is_parked(name):
                # 2026-09-22: recently answered "quota exhausted". It is
                # reachable and configured — that is exactly why every
                # availability probe says yes and why the run kept
                # re-selecting it twelve times. Skip until the park
                # expires; it is not a "wait for a slot" case.
                continue
            if compute_dynamic_limit(name, usage_map.get(name, 100.0)) <= 0:
                # Out of budget for the 5h window. Also not a
                # "wait for a slot" case.
                continue
            candidates.append(name)

        if not candidates:
            # Nothing to wait for: waiting here would spin until the
            # timeout for providers that cannot serve us either way.
            return None

        # 2. Start the walk at this dispatch's rotation slot and fall
        #    through to the rest, so a full pool still hands over rather
        #    than blocking the fleet.
        start = tracker.next_rotation_index(len(candidates))
        for offset in range(len(candidates)):
            name = candidates[(start + offset) % len(candidates)]
            limit = compute_dynamic_limit(name, usage_map.get(name, 100.0))
            if tracker.try_acquire(name, limit):
                return name

        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Provider cooldown — park a provider that answered "quota exhausted"
# ---------------------------------------------------------------------------
#
# 2026-09-22, a production plan.
#
# The run hit one provider's quota-exhaustion error twelve times. Each
# hit cost a failed round-trip (~3 min of a stuck agent) and the very
# next dispatch selected the SAME provider again, because nothing
# remembered that it had just said "no". The provider was reachable and
# correctly configured — it simply had no budget left in its window — so
# every availability probe kept answering "yes".
#
# Parking is deliberately in-process rather than on disk: the useful
# lifetime is "the rest of this execution run", and a run is one
# executor process. A stale on-disk park would survive into a run whose
# provider has long since refilled.


#: How long a provider stays parked after a generic 429. Long enough
#: that a burst of concurrently-dispatched agents does not re-hit it,
#: short enough that a transient rate-limit self-heals within a run.
DEFAULT_COOLDOWN_SEC: float = 300.0

#: How long a provider stays parked after an explicit quota / billing
#: rejection. Those reset on a rolling window (5h / weekly), not in
#: seconds, so re-probing inside a run is wasted work.
DEFAULT_QUOTA_COOLDOWN_SEC: float = 1800.0

#: Substrings that mark a 429 as "out of budget" rather than "slow
#: down". Matched case-insensitively against the provider's error text.
#: The list spans languages because CC Switch surfaces vendor text
#: verbatim, and vendors do not answer in one.
QUOTA_ERROR_MARKERS: tuple[str, ...] = (
    "用量上限",
    "quota",
    "insufficient",
    "balance",
    "billing",
    "payment",
    "credit",
    "token plan",
    "plan limit",
    "exceeded your current quota",
)


def looks_like_quota_exhaustion(text: str) -> bool:
    """True when a provider error reads as "out of budget", not "slow down".

    Used to choose between :data:`DEFAULT_COOLDOWN_SEC` and
    :data:`DEFAULT_QUOTA_COOLDOWN_SEC` when parking. A false negative
    only costs a short park; a false positive parks a healthy provider
    for 30 minutes, so the markers are specific phrases rather than the
    bare word "limit".
    """
    if not text:
        return False
    lowered = text.casefold()
    return any(marker in lowered for marker in QUOTA_ERROR_MARKERS)


class ProviderCooldown:
    """Thread-safe park list for providers that reported no capacity.

    A parked provider is skipped by both the scene-candidate walk and
    :func:`acquire_provider_with_dynamic_capacity`. Park entries expire
    on their own; nothing has to unpark them.
    """

    def __init__(self) -> None:
        self._until: Dict[str, float] = {}
        self._reasons: Dict[str, str] = {}
        self._lock = threading.Lock()

    def park(
        self,
        provider: str,
        *,
        seconds: float,
        reason: str = "",
    ) -> None:
        """Skip ``provider`` for the next ``seconds`` seconds.

        Re-parking extends the window to the later deadline — a provider
        that answers "still exhausted" an hour later must not inherit
        the first park's already-expired deadline.
        """
        key = canonical_capacity_key(provider)
        if not key or seconds <= 0:
            return
        deadline = time.monotonic() + float(seconds)
        with self._lock:
            if deadline > self._until.get(key, 0.0):
                self._until[key] = deadline
                self._reasons[key] = reason[:300]

    def is_parked(self, provider: str) -> bool:
        """True when ``provider`` is inside its cooldown window."""
        key = canonical_capacity_key(provider)
        with self._lock:
            deadline = self._until.get(key)
            if deadline is None:
                return False
            if time.monotonic() >= deadline:
                # Lazy expiry — keeps the dict from growing across a
                # long run without a background sweeper.
                self._until.pop(key, None)
                self._reasons.pop(key, None)
                return False
            return True

    def remaining(self, provider: str) -> float:
        """Seconds left on ``provider``'s park, or ``0.0``."""
        key = canonical_capacity_key(provider)
        with self._lock:
            deadline = self._until.get(key)
            if deadline is None:
                return 0.0
            return max(0.0, deadline - time.monotonic())

    def reason(self, provider: str) -> str:
        """The reason string recorded when ``provider`` was parked."""
        return self._reasons.get(canonical_capacity_key(provider), "")

    def clear(self, provider: Optional[str] = None) -> None:
        """Drop the park for ``provider``, or every park when ``None``."""
        with self._lock:
            if provider is None:
                self._until.clear()
                self._reasons.clear()
                return
            key = canonical_capacity_key(provider)
            self._until.pop(key, None)
            self._reasons.pop(key, None)

    def snapshot(self) -> Dict[str, float]:
        """``{provider: seconds_remaining}`` for everything still parked."""
        with self._lock:
            now = time.monotonic()
            return {
                key: round(deadline - now, 1)
                for key, deadline in self._until.items()
                if deadline > now
            }


_shared_cooldown: Optional["ProviderCooldown"] = None
_shared_cooldown_lock = threading.Lock()


def get_shared_cooldown() -> "ProviderCooldown":
    """Return the process-wide :class:`ProviderCooldown` (created lazily)."""
    global _shared_cooldown
    if _shared_cooldown is None:
        with _shared_cooldown_lock:
            if _shared_cooldown is None:
                _shared_cooldown = ProviderCooldown()
    return _shared_cooldown


def set_shared_cooldown(cooldown: Optional["ProviderCooldown"]) -> None:
    """Install ``cooldown`` as the process-wide instance (``None`` resets)."""
    global _shared_cooldown
    with _shared_cooldown_lock:
        _shared_cooldown = cooldown


# ---------------------------------------------------------------------------
# Process-wide tracker handle
# ---------------------------------------------------------------------------
#
# The tracker must be shared by everything that dispatches a subagent, or
# each holder counts only its own work and the caps mean nothing. The
# FastAPI lifespan owns the canonical instance (it already builds one for
# ``RuntimeState`` and hands it to ``AutonomousAgent``) and registers it
# here via :func:`set_shared_tracker`; this module holds the handle so
# ``ClaudeCodingTool`` — constructed in ~25 places, almost none of which
# have the app state in scope — can reach the same instance by default.
#
# Ordering matters and is safe: the lifespan runs before any request
# handler, so a tool built for a request always sees the registered
# instance. A tool built before the lifespan (a CLI script, a unit test)
# gets a lazily-created tracker; the lifespan then replaces it, which is
# fine because nothing has acquired a slot yet.

_shared_tracker: Optional["ActiveConcurrencyTracker"] = None
_shared_tracker_lock = threading.Lock()


def get_shared_tracker() -> "ActiveConcurrencyTracker":
    """Return the process-wide :class:`ActiveConcurrencyTracker`.

    Creates one on first call so a tool constructed outside the FastAPI
    lifespan still has a working (if private) capacity surface rather
    than an unenforced ``None``.
    """
    global _shared_tracker
    if _shared_tracker is None:
        with _shared_tracker_lock:
            if _shared_tracker is None:
                _shared_tracker = ActiveConcurrencyTracker()
    return _shared_tracker


def set_shared_tracker(tracker: Optional["ActiveConcurrencyTracker"]) -> None:
    """Install ``tracker`` as the process-wide instance.

    Called by the FastAPI lifespan with ``RuntimeState.dynamic_tracker``
    so the scene-routed dispatch path and the executor's
    ``_load_provider_info`` path share one slot-count surface. Passing
    ``None`` resets the handle (tests).
    """
    global _shared_tracker
    with _shared_tracker_lock:
        _shared_tracker = tracker


@contextmanager
def acquired_provider_slot(
    provider: str,
    tracker: ActiveConcurrencyTracker,
) -> Iterator[None]:
    """Context manager that holds one slot on ``provider`` for its body.

    Increments :meth:`ActiveConcurrencyTracker.acquire` on entry and
    :meth:`ActiveConcurrencyTracker.release` on exit. The release runs
    on the way out even when the inner block raises — the counter must
    not leak, because a leaked slot permanently shrinks a provider's
    usable capacity and eventually sends every dispatch to the
    parent-process fallback.

    Args:
        provider: Provider reference to acquire a slot on.
        tracker: The shared :class:`ActiveConcurrencyTracker`.

    Yields:
        ``None``. The context manager exists for its side effects on
        ``tracker``.

    Example::

        with acquired_provider_slot("vendor-a-pro", tracker):
            result = coding_tool.query(prompt)
        # tracker.current("vendor-a-pro") is back to its pre-entry value
    """
    tracker.acquire(provider)
    try:
        yield
    finally:
        tracker.release(provider)


__all__ = [
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_MAX_CONCURRENCY_AT_FULL",
    "DEFAULT_QUOTA_COOLDOWN_SEC",
    "QUOTA_ERROR_MARKERS",
    "ActiveConcurrencyTracker",
    "DynamicProviderSelectionError",
    "ProviderCooldown",
    "acquire_provider_with_dynamic_capacity",
    "acquired_provider_slot",
    "canonical_capacity_key",
    "compute_dynamic_limit",
    "get_shared_cooldown",
    "get_shared_tracker",
    "looks_like_quota_exhaustion",
    "round_half_up",
    "select_provider_with_dynamic_capacity",
    "set_shared_cooldown",
    "set_shared_tracker",
]