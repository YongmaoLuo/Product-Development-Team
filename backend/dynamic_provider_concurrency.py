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
  * :func:`provider_is_eligible` — the single "may this be dispatched
    to right now" predicate every walk shares: live quota verdict, park
    list, local capacity.
  * :func:`provider_has_capacity` — the "could this provider serve at
    all" predicate: live quota verdict, park list, quota-scaled cap.
  * :func:`chain_has_dispatchable_provider` — the whole-chain question,
    asked member by member: is anybody left to dispatch to?
  * :func:`select_provider_with_dynamic_capacity` — walks the fallback
    chain and returns the first provider with spare capacity, or ``None``.
  * :func:`acquired_provider_slot` — context manager that increments the
    tracker on enter and decrements on exit (even when the inner block
    raises).

Two kinds of "not now", kept apart
-----------------------------------
A provider can be unavailable because its **window** is empty or because
its **slots** are held. The first is measured in hours and the second in
seconds, so collapsing them is not a simplification — it is a wrong
answer delivered slowly. :func:`acquire_provider_with_dynamic_capacity`
filters everything that cannot serve *at all* before it waits for
anything, and returns ``None`` immediately when nothing survives;
waiting is reserved for held slots, which come back on their own.

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
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import provider_capacity
from provider_order import ProviderQuota, _WEEKLY_TIER


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


# ---------------------------------------------------------------------------
# Eligibility — the one predicate every dispatch walk shares
# ---------------------------------------------------------------------------
#
# "Can this provider serve the call right now" is one question with four
# independent filters behind it, and each of the three walks that answer
# it — the reporting one, the slot-taking one, and the subagent-config
# one — used to answer it slightly differently. A provider that was
# reachable, within quota and inside its park under one walk could be
# invisible under another, so the fleet's real capacity was whichever
# walk the dispatcher happened to use. They share this predicate instead.
#
#   1. quota verdict      — the live reading says there is no budget
#   2. park               — a refusal during this run put it on hold
#   3. local capacity     — it is below its (quota-scaled) cap
#
# The order is a cost order, cheapest and most decisive first, and it is
# not a preference order: all three must hold. Nothing here ranks.


def _weekly_window_lost(quota: Any) -> bool:
    """True when the reading is missing a weekly window the provider has had.

    A reading that does not carry the weekly window means two different
    things, and only the provider's history tells them apart:

    * the provider has never been seen to carry one — it is on a metered
      API product and has no week to exhaust in the first place, so
      there is nothing to lose and the gate must stay open;
    * the provider *was* seen to carry one and this reading stopped
      reporting it — the week has gone missing, which is treated as an
      exhausted week.

    The second case is the one this function answers for. History comes
    from :attr:`provider_order.ProviderQuota.weekly_tier_seen`, which
    :mod:`quota_tier_memory` carries across processes.

    Every field is read with :func:`getattr` and a default: the quota
    map is not typed, and a stand-in reading from before these fields
    existed must be read as "no week to lose" rather than raising.
    """
    if not getattr(quota, "weekly_tier_seen", False):
        return False
    missing = getattr(quota, "tiers_missing", ()) or ()
    try:
        return _WEEKLY_TIER in missing
    except TypeError:  # pragma: no cover - defensive
        return False


def quota_allows_dispatch(quota: Optional[ProviderQuota]) -> bool:
    """True unless the reading *affirmatively* shows no budget left.

    ``None`` — no reading for this provider — is allowed. That is the
    fail-open half of the contract, and it is a deliberate asymmetry:
    the two ways of knowing nothing are not the same thing. A reading
    of "5-hour window at 100%" is evidence. A reading that does not
    exist is not, and CC Switch cannot meter some providers at all while
    those providers serve traffic perfectly well. A provider that is
    genuinely uncallable is caught by its own refusal at dispatch time,
    which is an observation rather than an inference from a gap in a
    payload.

    Two things count as "no budget left":

    * the reading says the provider is exhausted, and
    * the reading's weekly window went missing on a provider that is
      known to have one (:func:`_weekly_window_lost`) — a week that
      stopped being reported is a week that cannot be dispatched to,
      and treating it as "the burst window is fine, keep going" is how
      a provider with an exhausted account stays on the rotation.

    The second is checked *after* the first, so an exhausted reading is
    refused either way.
    """
    if quota is None:
        return True
    if getattr(quota, "exhausted", False):
        return False
    return not _weekly_window_lost(quota)


def quota_remaining_pct(quota: Optional[ProviderQuota]) -> Optional[float]:
    """The reading's own headroom in ``[0, 100]``, or ``None`` if it has none.

    ``None`` is not "100%": it is "this reading does not say", and the
    caller decides what to do without it. Keeping the two apart is what
    stops a meter-less provider from being scaled to a full cap by
    accident while still being eligible.

    A reading whose weekly window went missing reports ``0.0``, the
    same answer :func:`quota_allows_dispatch` gives it: the provider is
    out of week, so it has no room to offer. ``0.0`` rather than
    ``None`` is what makes the cap scaling follow — ``None`` would let
    the caller fall back to another source and hand the provider slots
    it may not use.
    """
    if quota is None:
        return None
    if _weekly_window_lost(quota):
        return 0.0
    pct = getattr(quota, "remaining_pct", None)
    if pct is None or isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return max(0.0, min(100.0, float(pct)))


def provider_headroom_pct(
    provider: str,
    usage_map: Mapping[str, float],
    quota_map: Optional[Mapping[str, ProviderQuota]] = None,
) -> float:
    """The percentage used to scale ``provider``'s concurrency cap.

    The live reading wins when it carries a number, because it is the
    one that accounts for the 5-hour burst window and for a currency
    balance — neither of which the legacy ``{provider: pct}`` file can
    express. What that number deliberately does *not* carry is the
    weekly window: the week is an independent budget that decides
    eligibility (:attr:`ProviderQuota.exhausted`, checked by
    :func:`quota_allows_dispatch`), and scaling the cap by it as well
    would hold a provider down twice for the same seven days. The one
    exception is a week that went *missing*
    (:func:`_weekly_window_lost`): there is no week left to scale by,
    so the reading answers ``0`` and the cap goes to zero with it. The
    legacy map is the fallback for callers that still pass it and for
    providers the live reading does not cover, and 100 is the last
    resort, because an unknown budget is not a zero budget.
    """
    if quota_map is not None:
        live = quota_remaining_pct(quota_map.get(provider))
        if live is not None:
            return live
    try:
        legacy = usage_map.get(provider, 100.0)
    except AttributeError:  # pragma: no cover - defensive
        legacy = 100.0
    if isinstance(legacy, bool) or not isinstance(legacy, (int, float)):
        return 100.0
    return max(0.0, min(100.0, float(legacy)))


def provider_is_eligible(
    provider: str,
    tracker: ActiveConcurrencyTracker,
    usage_map: Mapping[str, float],
    quota_map: Optional[Mapping[str, ProviderQuota]] = None,
    *,
    cooldown: Optional["ProviderCooldown"] = None,
) -> bool:
    """True when ``provider`` may be dispatched to *right now*.

    Args:
        provider: Provider reference, any spelling. ``"parent"`` is
            never eligible — it is the caller's explicit fallback, not a
            member of the pool.
        tracker: The shared slot counter.
        usage_map: Legacy ``{provider: remaining pct}``. Used for the
            cap only when the live reading has no number.
        quota_map: The live reading from
            :func:`provider_order.load_provider_quota_map`. ``None`` or
            an absent entry means "no reading", which is fail-open.
        cooldown: The park list. ``None`` consults the shared instance.

    Returns:
        ``False`` for anything this process has observed to be out of
        budget, in cooldown, or at its local cap; ``True`` otherwise.
        There is no third answer and no ranking — callers take the first
        eligible entry *in the order they were given*, and an empty
        result means "dispatch to none of these", never "dispatch to the
        least bad one".
    """
    if not provider or provider == "parent":
        return False
    quota = quota_map.get(provider) if quota_map is not None else None
    if not quota_allows_dispatch(quota):
        return False
    park = cooldown if cooldown is not None else get_shared_cooldown()
    if park.is_parked(provider):
        return False
    limit = compute_dynamic_limit(
        provider, provider_headroom_pct(provider, usage_map, quota_map)
    )
    return tracker.current(provider) < limit


def provider_has_capacity(
    provider: str,
    quota: Optional[ProviderQuota],
    *,
    parked: bool = False,
) -> bool:
    """True when this provider could serve a dispatch *at all*.

    A provider stops being able to serve for three separate reasons, and
    this function asks all three of them, in the order that is cheapest
    and most decisive first:

    1. it is **parked** — a refusal earlier in this run put it on hold;
    2. it has **no budget** — the live reading says the window is empty
       (:func:`quota_allows_dispatch`);
    3. its **cap is zero** — the quota-scaled cap it would serve under
       comes out at nothing (:func:`compute_dynamic_limit`).

    This is the "could this provider ever take work?" half of
    :func:`provider_is_eligible`. The other half — "does it have a free
    slot *right now*" — is a fact about in-flight work, and it changes
    second to second; this one changes when the provider's budget or its
    park changes, which is what an "is the chain finished?" question is
    actually about.

    The fail-open half of the contract is kept: a provider with **no
    reading** is admitted, because a missing reading is not evidence of
    an empty window. CC Switch cannot meter every provider, and those
    providers serve traffic perfectly well. Only an affirmative
    reading, an affirmative park, or a zero cap refuses a dispatch.

    Args:
        provider: The provider reference. ``"parent"`` is never a
            provider: it is the caller's own fallback and has no row,
            no reading and no cap of its own.
        quota: The live reading for this provider, or ``None`` when
            there is none.
        parked: Whether the provider is currently on the park list.
            The caller resolves the park itself, so this function stays
            a pure question about one provider.

    Returns:
        ``True`` when the provider could serve a dispatch.
    """
    if not isinstance(provider, str) or not provider.strip():
        return False
    if provider == "parent":
        return False
    if parked:
        return False
    if not quota_allows_dispatch(quota):
        return False
    headroom = quota_remaining_pct(quota)
    if headroom is None:
        # The reading carries no number for the window. An unknown
        # budget is not a zero budget, so the provider keeps its full
        # cap rather than being scaled to nothing.
        headroom = 100.0
    return compute_dynamic_limit(provider, headroom) > 0


def chain_has_dispatchable_provider(
    chain: Sequence[str],
    quota_map: Optional[Mapping[str, ProviderQuota]] = None,
    cooldown: Optional["ProviderCooldown"] = None,
) -> bool:
    """True when at least one member of ``chain`` could serve a dispatch.

    The whole-chain question, asked member by member: every name in
    ``chain`` is put to :func:`provider_has_capacity`, and the chain is
    dispatchable as soon as one member survives. Nothing here ranks the
    members — the chain's own order says which one is tried first, and
    this function only answers whether the chain still has anybody left
    to try.

    The empty chain is not dispatchable. "Nobody to dispatch to" and
    "everybody is out of budget" are different answers, and only the
    second one is a reason to go and wait for a window to reopen.

    Args:
        chain: The provider names to ask about, in the order the
            dispatcher would try them.
        quota_map: The live reading, provider name → quota row.
            ``None`` or an absent entry means "no reading", which is
            fail-open: the park list is then the only thing that can
            refuse a dispatch.
        cooldown: The park list. ``None`` consults the shared instance.

    Returns:
        ``True`` when some member of the chain could serve.
    """
    if not chain:
        return False
    park = cooldown if cooldown is not None else get_shared_cooldown()
    for name in chain:
        if not isinstance(name, str) or not name.strip():
            continue
        quota = None
        if quota_map is not None:
            try:
                quota = quota_map.get(name)
            except Exception:
                # A quota map that cannot be read must not turn into a
                # refusal: an unreadable reading is not evidence of an
                # empty window.
                quota = None
        if provider_has_capacity(name, quota, parked=park.is_parked(name)):
            return True
    return False


@dataclass(frozen=True)
class FleetCapacity:
    """How much concurrent work a set of providers can serve right now.

    ``total`` is the sum of ``per_provider``, and ``per_provider`` holds
    only the providers that contribute — a provider that is shut, parked,
    or scaled to zero is in ``blocked`` instead, because "this pool is
    not serving anyone right now" and "this pool serves ten" are answers
    an operator needs separately. Keeping both is what lets a caller say
    *why* a number went down rather than only that it did.
    """

    total: int
    per_provider: Dict[str, int]
    blocked: Tuple[str, ...]


def fleet_capacity(
    providers: Sequence[str],
    usage_map: Mapping[str, float],
    quota_map: Optional[Mapping[str, ProviderQuota]] = None,
    *,
    cooldown: Optional["ProviderCooldown"] = None,
) -> FleetCapacity:
    """Sum what ``providers`` can serve concurrently, at this moment.

    This is the same arithmetic :func:`provider_is_eligible` performs,
    summed over the whole set instead of applied to one name — same
    ``quota_map``, same park list, same
    :func:`provider_headroom_pct` and the same
    :func:`compute_dynamic_limit` that scales a provider's declared cap by
    its remaining 5-hour budget. It answers a different question from
    eligibility, though, and the difference is load-bearing:

    Eligibility asks whether *this dispatch, right now* may take a slot,
    and so it also compares the pool's in-flight count against its cap.
    A fleet width must not: it is the cap itself, and a width computed
    from in-flight counts would shrink below the count it is bounding the
    moment it started being used — a fleet that serves ten would admit
    ten, then read its own ten as "full", admit nobody, and never wake
    up. So the in-flight comparison is left to the caller that already
    holds the tracker, and everything upstream of it is identical.

    Args:
        providers: Provider references, in any spelling, duplicates
            allowed. Duplicates that name the same pool collapse to one
            entry, because a pool counted twice would claim a capacity
            it cannot serve; two *different* rows stay two, since two
            rows are two quota pools with two credentials.
        usage_map: Legacy ``{provider: remaining pct}``. Consulted only
            for the providers the live reading does not cover, exactly
            as it is for eligibility.
        quota_map: The live reading. ``None`` or an absent entry is
            fail-open, as it is everywhere else.
        cooldown: The park list. ``None`` uses the shared instance.

    Returns:
        A :class:`FleetCapacity`. ``total == 0`` is a real answer — every
        provider the caller named is shut, parked or scaled to zero —
        and callers must pass it on rather than substituting a number of
        their own. A width invented here is a width that contradicts
        the meters.
    """
    park = cooldown if cooldown is not None else get_shared_cooldown()
    per_provider: Dict[str, int] = {}
    blocked: List[str] = []
    seen: set = set()

    for name in providers or ():
        if not name or name == "parent":
            # The caller's explicit fallback, never a member of the pool.
            continue
        key = canonical_capacity_key(name)
        if key in seen:
            continue
        seen.add(key)

        if park.is_parked(name):
            blocked.append(name)
            continue
        if not quota_allows_dispatch(
            quota_map.get(name) if quota_map is not None else None
        ):
            blocked.append(name)
            continue
        limit = compute_dynamic_limit(
            name, provider_headroom_pct(name, usage_map, quota_map)
        )
        if limit <= 0:
            blocked.append(name)
            continue
        per_provider[name] = limit

    return FleetCapacity(
        total=sum(per_provider.values()),
        per_provider=per_provider,
        blocked=tuple(sorted(blocked)),
    )


def select_provider_with_dynamic_capacity(
    provider_priority: List[str],
    tracker: ActiveConcurrencyTracker,
    usage_map: Dict[str, float],
    now: Optional[datetime] = None,
    quota_map: Optional[Mapping[str, ProviderQuota]] = None,
    *,
    cooldown: Optional["ProviderCooldown"] = None,
) -> Optional[str]:
    """Return the first provider in ``provider_priority`` with capacity.

    Walks ``provider_priority`` in order and returns the first entry
    :func:`provider_is_eligible` admits. **The order is the policy and
    this function does not reorder it**: the chain already carries the
    operator's ranking, and re-scoring it here could only disagree with
    the application that published it. So a provider ranked fifth is
    used before a ranked second is skipped, exactly as ranked.

    If no entry is eligible the function returns ``None``. That is the
    whole answer, and it is deliberately not "the least bad entry": a
    provider with no budget for the next five hours is not a provider
    that serves the call slowly, it is a provider that fails it, and
    picking it converts a clean "not now" into a round-trip that is
    guaranteed to end in a refusal.

    Args:
        provider_priority: Ordered list of logical provider IDs from
            :func:`provider_order.load_fallback_order`. May include the
            ``"parent"`` sentinel at the end.
        tracker: An :class:`ActiveConcurrencyTracker` recording the
            current active count per provider.
        usage_map: Per-provider 5-hour remaining percentage, used for
            the cap when the live reading has no number. Missing entries
            default to 100%.
        now: Current datetime. ``None`` defers to
            :func:`datetime.datetime.now`.
        quota_map: The live reading. ``None`` keeps this function's
            historical behaviour exactly (quota and park are not
            consulted), which is what the legacy call sites pass.
        cooldown: The park list. ``None`` consults the shared instance.

    Returns:
        The selected provider id, or ``None`` when the chain has no
        eligible entry.
    """
    if not provider_priority:
        return None

    if now is None:
        now = datetime.now()

    for provider in provider_priority:
        if provider_is_eligible(
            provider, tracker, usage_map, quota_map, cooldown=cooldown
        ):
            return provider

    return None


def acquire_provider_with_dynamic_capacity(
    providers: Sequence[str],
    tracker: ActiveConcurrencyTracker,
    usage_map: Dict[str, float],
    *,
    timeout: float = 300.0,
    poll_interval: float = 0.5,
    quota_map: Optional[Mapping[str, ProviderQuota]] = None,
    cooldown: Optional["ProviderCooldown"] = None,
) -> Optional[str]:
    """Block until one of ``providers`` has a free slot; return its name.

    Two different reasons to have nothing to dispatch to, and they are
    answered differently on purpose
    --------------------------------
    The candidate set is filtered **before** anything waits, so "no
    candidate survives" and "every candidate is momentarily full" never
    reach the same code:

      * **Nothing survives the filter** — every provider is parked or out
        of budget for this run. There is no lease to wait for and no
        counter that a sleep would move, so this returns ``None``
        immediately. Sleeping here would be the worst of both: the
        caller blocks for its whole timeout and then finds out exactly
        what it already knew. Whether to come back later, and when, is
        the caller's decision and is answered by the park each provider
        carries — a park lasts as long as the window that caused it.
      * **Candidates survive but are momentarily full** — they are
        within quota and serve fine; their slots are simply held by
        agents that are still running. Those return in seconds, so this
        polls for them until ``timeout``.

    Collapsing the two is not a simplification, it is a wrong answer
    delivered slowly: a window that does not open for hours cannot be
    waited out by a poll whose unit is half a second.

    Strict priority across the EFFECTIVE candidates
    ----------------------------------------------
    2026-10-21 — user contract: a tier is consumed **in ranking
    order**. The first provider that still has a free slot takes the
    dispatch, and it keeps taking dispatches for as long as it has
    room. Only a full (or unusable) pool hands over to the next one in
    the chain.

    The previous behaviour (2026-09-22) was an average rotation: work
    was spread in proportion to how many providers a tier could
    actually call. That wasted quota in two ways — every account's
    weekly allowance was left partly unspent when its window reset,
    and a lower-ranked account was drawn down while the higher-ranked
    ones still had budget left. Spending the highest-ranked quota
    first is what the ranking was for.

    The candidate set is still filtered **first** — a parked provider, a
    quota-exhausted one, or one whose computed cap is 0 occupies no
    slot — and a saturated pool still falls through to the next one
    rather than blocking the fleet. The walk never waits on a provider
    that cannot serve at all; only a held slot is worth waiting for.

    Concretely, with two effective providers of cap 10 each, 12
    concurrent dispatches put the first 10 in the first pool and the
    remaining 2 in the second.

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
        usage_map: Per-provider 5-hour remaining percentage, used for
            the cap when the live reading has no number. Missing entries
            default to 100%.
        timeout: Seconds to keep polling before giving up. The default
            (300 s) is well inside the 1800 s executor task timeout and
            the 900 s default total timeout, so queueing cannot by
            itself trip a timeout.
        poll_interval: Seconds between walks. A full fleet is usually
            waiting on an LLM call that takes tens of seconds, so a
            sub-second poll just burns CPU.
        quota_map: The live reading. ``None`` keeps this function's
            historical behaviour for the cap.
        cooldown: The park list. ``None`` uses the shared instance.

    Returns:
        The name from ``providers`` whose slot was taken, or ``None``
        when the chain had no *eligible* provider at all (every entry
        out of budget or parked, or the list was empty) or the wait timed
        out. ``None`` is not an error: it is the explicit "do not
        dispatch to any of these", which the caller turns into whatever
        its own contract says — a refusal, or a documented fallback.
    """
    if not providers:
        return None

    deadline = time.monotonic() + max(0.0, timeout)
    park = cooldown if cooldown is not None else get_shared_cooldown()

    while True:
        # 1. The EFFECTIVE candidate set — what can actually be called.
        #    Filtering first (rather than skipping inside the walk) keeps
        #    a dead provider out of the chain: a chain of three with one
        #    unreachable provider is walked as a chain of two.
        #
        #    Every reason a provider cannot serve *at all* is applied
        #    here, ahead of anything that waits: a refusal earlier in
        #    this run parked it, or the live reading shows the window is
        #    empty. Neither is a slot that will free up, so neither
        #    belongs in a wait loop.
        candidates: List[str] = []
        for name in providers:
            if name == "parent":
                # ``parent`` is the caller's explicit fallback, not a
                # pool, and it must never occupy a candidate slot.
                continue
            if park.is_parked(name):
                # Answered "quota exhausted" earlier in this run. It is
                # reachable and configured — which is exactly why every
                # availability probe says yes and why the run would keep
                # re-selecting it. Skip until the park expires; it is not
                # a "wait for a slot" case.
                continue
            if not quota_allows_dispatch(
                quota_map.get(name) if quota_map is not None else None
            ):
                # The reading says the budget is gone for this window.
                # Also not a "wait for a slot" case: the thing being
                # waited for is a window, not a lease.
                continue
            if compute_dynamic_limit(
                name, provider_headroom_pct(name, usage_map, quota_map)
            ) <= 0:
                # Out of budget by the legacy percentage map.
                continue
            candidates.append(name)

        if not candidates:
            # Nothing to wait for. Spinning until the timeout would burn
            # the caller's whole budget to arrive at an answer the
            # candidate filter already gave, and — worse for the ones
            # whose window is hours out — would hide it behind a delay
            # nobody can shorten. This is the "do not dispatch" answer.
            return None

        # 2. Walk the candidates in chain order. The chain IS the ranking:
        #    the first provider with room takes the dispatch, and only a
        #    full (or unusable) pool hands over to the next.
        for name in candidates:
            limit = compute_dynamic_limit(
                name, provider_headroom_pct(name, usage_map, quota_map)
            )
            if tracker.try_acquire(name, limit):
                return name

        # 3. Candidates remain, and every one of them is at its cap. A
        #    held slot comes back when its agent finishes, so this is the
        #    one case that is genuinely worth waiting on.
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

#: Substrings that mark a failure as "out of budget" rather than "slow
#: down". Matched case-insensitively against the provider's error text.
#: The list spans languages because CC Switch surfaces vendor text
#: verbatim, and vendors do not answer in one.
#:
#: The vendor's plan name is deliberately not among them. Both of that
#: vendor's refusals name the plan — the spent-plan one, which carries
#: the code ``2056``, and the rate-limit one, which carries ``2062`` —
#: so the plan name cannot tell the two apart. What separates them is
#: the wording the vendor puts next to it, and that wording is what
#: this set and :data:`RATE_LIMIT_ERROR_MARKERS` hold.
#:
#: ``plan 用量`` is that kind of wording rather than a plan name: it
#: says the plan's *usage* is gone, and it does so without any code.
#: A refusal that only says this used to fall through both text checks
#: and land on the status check, where a ``429`` reads as a rate limit
#: — a refusal that clears itself in seconds. The wording is the
#: verdict, so a spent plan that names no code is still a spent plan.
EXHAUSTION_ERROR_MARKERS: tuple[str, ...] = (
    "用量上限",
    "2056",
    "quota",
    "insufficient",
    "balance",
    "billing",
    "payment",
    "credit",
    "plan limit",
    "exceeded your current quota",
    "plan 用量",
)

#: Substrings that mark a failure as "slow down, ask again shortly".
#: A rate limit clears on its own in minutes, so it earns the short
#: park rather than the quota window.
RATE_LIMIT_ERROR_MARKERS: tuple[str, ...] = (
    "速率限制",
    "2062",
    "rate limit",
    "too many requests",
)

#: The previous name of :data:`EXHAUSTION_ERROR_MARKERS`. Kept because
#: the name is part of this module's exported surface; the set behind
#: it is the exhaustion set and nothing else.
QUOTA_ERROR_MARKERS: tuple[str, ...] = EXHAUSTION_ERROR_MARKERS

#: Statuses that mean "this provider cannot serve the call right now".
#: 429 is the rate-limit / quota case, 402 is a billing rejection, and
#: 503 / 529 are the upstream-overloaded answers that
#: Anthropic-compatible gateways return.
CAPACITY_ERROR_STATUSES: frozenset[str] = frozenset({"429", "402", "503", "529"})

#: Statuses that mean "the request is wrong". No other provider can
#: fix a malformed request, so these are fatal rather than a reason to
#: rotate. Authentication (401 / 403), not-found (404) and conflict
#: (409) are deliberately absent: a bad credential is worth one more
#: provider, not a dead task.
FATAL_ERROR_STATUSES: frozenset[str] = frozenset(
    {"400", "405", "406", "413", "414", "415", "422", "501"}
)


def classify_capacity_failure(
    text: str,
    status: str = "",
    *,
    live_exhausted: bool = False,
) -> str:
    """Classify a provider failure into one of four buckets.

    Returns ``"exhausted"`` (the budget is gone), ``"rate_limited"``
    (slow down, ask again shortly), ``"fatal"`` (the request itself is
    wrong) or ``"other"`` (anything the caller should keep handling the
    way it did before — including rotating to the next provider).

    The order of the checks is the contract, and it never changes:

      1. ``live_exhausted`` — a live usage reading that says the budget
         is gone. The meter is not a guess, so it outranks the prose
         the provider sent alongside it.
      2. text that reads as an exhaustion (the code ``2056``, the phrase
         for a usage cap, or one of the English budget phrases).
      3. text that reads as a rate limit (the code ``2062``, the phrase
         for a rate limit).
      4. a capacity status — 429 / 402 / 503 / 529 — with no text to
         read.
      5. a fatal status — 400 / 405 / 406 / 413 / 414 / 415 / 422 / 501.
      6. everything else.

    The text checks come before the status checks because the provider
    says more than its status does: a 429 that reads as a spent plan
    and a 429 that reads as a rate limit are different failures, and
    only the text tells them apart.
    """
    if live_exhausted:
        return "exhausted"

    lowered = (text or "").casefold()
    if any(marker in lowered for marker in EXHAUSTION_ERROR_MARKERS):
        return "exhausted"
    if any(marker in lowered for marker in RATE_LIMIT_ERROR_MARKERS):
        return "rate_limited"

    normalized = str(status or "").strip()
    if normalized in CAPACITY_ERROR_STATUSES:
        return "rate_limited"
    if normalized in FATAL_ERROR_STATUSES:
        return "fatal"
    return "other"


def looks_like_quota_exhaustion(text: str) -> bool:
    """True when a provider error reads as "out of budget", not "slow down".

    Used to choose between :data:`DEFAULT_COOLDOWN_SEC` and
    :data:`DEFAULT_QUOTA_COOLDOWN_SEC` when parking. A false negative
    only costs a short park; a false positive parks a healthy provider
    for 30 minutes, so the markers are specific phrases rather than the
    bare word "limit".

    The answer comes from :func:`classify_capacity_failure`, so this
    question and the four-way classification can never disagree about
    what a given error text means.
    """
    return classify_capacity_failure(text) == "exhausted"


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
    "CAPACITY_ERROR_STATUSES",
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_MAX_CONCURRENCY_AT_FULL",
    "DEFAULT_QUOTA_COOLDOWN_SEC",
    "EXHAUSTION_ERROR_MARKERS",
    "FATAL_ERROR_STATUSES",
    "QUOTA_ERROR_MARKERS",
    "RATE_LIMIT_ERROR_MARKERS",
    "ActiveConcurrencyTracker",
    "DynamicProviderSelectionError",
    "ProviderCooldown",
    "acquire_provider_with_dynamic_capacity",
    "acquired_provider_slot",
    "canonical_capacity_key",
    "chain_has_dispatchable_provider",
    "classify_capacity_failure",
    "compute_dynamic_limit",
    "get_shared_cooldown",
    "get_shared_tracker",
    "looks_like_quota_exhaustion",
    "provider_headroom_pct",
    "provider_has_capacity",
    "provider_is_eligible",
    "quota_allows_dispatch",
    "quota_remaining_pct",
    "round_half_up",
    "select_provider_with_dynamic_capacity",
    "set_shared_cooldown",
    "set_shared_tracker",
]