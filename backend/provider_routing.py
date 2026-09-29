"""Scene → tier → provider routing core (2026-09-13).

User-pinned contract:

  * The backend manages PROVIDERS only. Model management is delegated entirely to
    CC Switch — each provider row in the CC Switch database carries its
    own complete ``ANTHROPIC_*`` env block, which the dispatch layer
    forwards verbatim. This module deliberately has no model knowledge.

  * ``backend/configs/provider_routing.yaml`` maps workflow scenes to
    tiers and tiers to regex patterns over CC Switch provider display
    names. The dispatch caller may ONLY pick providers from the chain
    this module returns, so cross-tier fallback is structurally
    impossible.

  * ORDERING (revised 2026-09-14): the chain follows
    **CC Switch's current provider order**, not the order the patterns
    are written in. A tier's pattern list therefore declares tier
    MEMBERSHIP only. The live order arrives as the optimizer chain
    (``provider-order.json``, which the optimizer keeps in sync with CC
    Switch), so an optimizer rule that demotes a provider — B-PRO during
    its peak window, say — is honoured here for free. Names the
    optimizer does not rank follow, in DB row order.

  * Hot reload: the yaml is re-read whenever its mtime changes (checked
    on every ``resolve_provider_chain`` call — the file is ~1KB so the
    stat cost is negligible). ``PDT_PROVIDER_ROUTING_FILE`` overrides the
    config path (env wins over the bundled default; an explicit
    ``config_path`` argument wins over the env var).

  * Degradation is loud-but-nonfatal: every config-level failure (missing
    file, invalid yaml, unknown tier, bad regex, zero hits) returns an
    EMPTY chain with a WARNING log. The caller decides what an empty
    chain means (legacy behaviour fallback / parent fallback).

All DB-facing helpers are thin functions (``_list_display_names``,
``_optimizer_order``) so tests can monkeypatch them without SQLite or
filesystem fixtures.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("provider_routing")

#: Where the routing table lives. Operator-owned configuration, not a
#: bundled asset: ``<repo>/.config/provider_routing.yaml`` by default, with
#: ``PDT_PROVIDER_ROUTING_FILE`` / ``PDT_CONFIG_DIR`` overriding it. See
#: :func:`config_paths.resolve_provider_routing_file`.
#:
#: This is a *factory*, not a resolved constant, because the config
#: directory can be redirected per-process (tests, alternate deployments)
#: and the answer must follow.
from config_paths import resolve_provider_routing_file as _resolve_routing_file

# Default tier used when a scene is missing from the ``scenes`` map.
# Mirrors the "至少都用 medium" : no scene silently routes
# to ``low``.
_DEFAULT_TIER = "medium"

# Cache discipline: one entry keyed by resolved config path, invalidated
# when the file's (mtime_ns, size) fingerprint changes. A lock guards the
# swap; readers take a snapshot of the parsed config under the lock and
# work on their own copy afterwards.
_cache_lock = threading.Lock()
_cached_path: Optional[str] = None
_cached_fingerprint: Optional[tuple] = None
_cached_config: Optional[Dict] = None


class ProviderRoutingError(ValueError):
    """Raised for hard misuse of the routing API (bad argument types).

    Config-level problems (missing file, bad regex, …) deliberately do
    NOT raise — they degrade to an empty chain with a WARNING, so one
    misconfigured scene can never crash a workflow phase.
    """


def _require_scene(scene: object) -> str:
    if not isinstance(scene, str) or not scene.strip():
        raise ProviderRoutingError(
            f"scene must be a non-empty string, got {scene!r}"
        )
    return scene.strip()


def clear_routing_cache() -> None:
    """Drop the cached parsed config. The next resolve call re-reads disk.

    Operator escape hatch after editing the yaml without an mtime bump
    (e.g. editors that preserve mtime); also used by tests.
    """
    global _cached_path, _cached_fingerprint, _cached_config
    with _cache_lock:
        _cached_path = None
        _cached_fingerprint = None
        _cached_config = None


def _config_path(config_path: Optional[Path] = None) -> Path:
    """Resolve the effective config path.

    Precedence: explicit argument > ``PDT_PROVIDER_ROUTING_FILE`` env >
    ``<config dir>/provider_routing.yaml``. All three are funnelled
    through :func:`config_paths.resolve_provider_routing_file` so that
    ``PDT_CONFIG_DIR`` is honoured in one place.
    """
    if config_path is not None:
        return Path(config_path)
    return _resolve_routing_file()


def _load_config(config_path: Path) -> Optional[Dict]:
    """Read + mtime-cache the routing yaml. Returns None when unreadable.

    A fingerprint of (mtime_ns, size) detects edits; the fingerprint
    avoids re-parsing on every call while still noticing changes within
    the same second that also change the size, and same-size edits that
    land in a later second. Pathological same-second + same-size edits
    can still slip through — ``clear_routing_cache`` is the documented
    operator escape hatch.
    """
    global _cached_path, _cached_fingerprint, _cached_config

    resolved = str(Path(config_path).resolve())
    try:
        st = Path(resolved).stat()
        fingerprint = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _cache_lock:
            # File vanished — drop any cached copy so a later re-creation
            # is picked up.
            if _cached_path == resolved:
                _cached_path = None
                _cached_fingerprint = None
                _cached_config = None
        log.warning(
            "provider_routing.yaml not found at %s; scene routing disabled "
            "(callers fall back to legacy behaviour)",
            resolved,
        )
        return None

    with _cache_lock:
        if (
            _cached_config is not None
            and _cached_path == resolved
            and _cached_fingerprint == fingerprint
        ):
            return _cached_config

    try:
        import yaml
    except ImportError:
        log.warning("PyYAML unavailable; provider routing disabled")
        return None

    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        # yaml.YAMLError is NOT a ValueError subclass — catch it
        # explicitly or a malformed file raises out of the resolver.
        with _cache_lock:
            if _cached_path == resolved:
                _cached_path = None
                _cached_fingerprint = None
                _cached_config = None
        log.warning(
            "provider_routing.yaml invalid at %s (%s); scene routing disabled",
            resolved, exc,
        )
        return None
    except OSError as exc:
        with _cache_lock:
            if _cached_path == resolved:
                _cached_path = None
                _cached_fingerprint = None
                _cached_config = None
        log.warning(
            "provider_routing.yaml unreadable at %s (%s); scene routing disabled",
            resolved, exc,
        )
        return None

    if not isinstance(data, dict):
        log.warning(
            "provider_routing.yaml root is %s, expected mapping; "
            "scene routing disabled",
            type(data).__name__,
        )
        data = None

    with _cache_lock:
        _cached_path = resolved
        _cached_fingerprint = fingerprint
        _cached_config = data if isinstance(data, dict) else None
    return _cached_config


def resolve_scene_tier(
    scene: str, config_path: Optional[Path] = None
) -> str:
    """Return the tier name configured for ``scene``.

    Missing scene → ``default_tier`` from the config (falling back to
    the module-level ``_DEFAULT_TIER`` when the config itself is
    missing/invalid). Never raises for config problems.
    """
    scene = _require_scene(scene)
    config = _load_config(_config_path(config_path))
    if not config:
        return _DEFAULT_TIER

    scenes = config.get("scenes")
    if isinstance(scenes, dict):
        tier = scenes.get(scene)
        if isinstance(tier, str) and tier.strip():
            return tier.strip()

    default = config.get("default_tier")
    if isinstance(default, str) and default.strip():
        return default.strip()
    return _DEFAULT_TIER


def _list_display_names(db_path=None) -> List[str]:
    """Enumerate CC Switch provider display names (monkeypatch seam).

    Delegates to the consumer layer, which owns the SQLite details and
    the schema handling. The production ``providers`` table is keyed by a
    UUID/app id, so display names come from its ``name`` column — the
    same string the CC Switch UI shows and ``provider-order.json`` holds.
    Rows without an ``ANTHROPIC_BASE_URL`` are skipped (they cannot drive
    a sub-agent).

    Failures degrade to ``[]``: routing then yields an empty chain and
    the caller applies its documented fallback, so an unreachable
    database can never crash a workflow phase.
    """
    try:
        # Local import keeps the module import-light.
        from cc_switch import (
            CCSwitchError,
            list_provider_names,
        )

        return list_provider_names(db_path)
    except CCSwitchError as exc:
        log.warning("CC Switch database unavailable (%s); provider chain empty", exc)
        return []
    except Exception as exc:  # sqlite3.Error and friends
        log.warning("CC Switch database read failed (%s); provider chain empty", exc)
        return []


def _optimizer_order() -> List[str]:
    """Return the live optimizer chain (monkeypatch seam).

    Reads ``provider-order.json`` via the existing loader and returns the
    display-name order. Failures degrade to ``[]`` — micro ordering then
    falls back to DB row order.
    """
    try:
        from provider_order import load_fallback_order

        return load_fallback_order()
    except Exception as exc:
        log.warning(
            "optimizer chain unavailable (%s); micro ordering falls back "
            "to DB row order", exc,
        )
        return []


def resolve_provider_chain(
    scene: str,
    config_path: Optional[Path] = None,
    db_path=None,
) -> List[str]:
    """Return the ordered provider display-name chain for ``scene``.

    Resolution order:

      1. ``scene`` → tier via the ``scenes`` map (missing scene →
         ``default_tier``).
      2. tier → regex patterns via the ``tiers`` map. The list declares
         tier MEMBERSHIP only — its order carries no ranking meaning.
      3. Every pattern is matched (``re.search``) against every live CC
         Switch display name; the union of the hits is the candidate set.
      4. The candidate set is ranked by the live optimizer chain
         (``provider-order.json``, which the optimizer keeps in sync with
         CC Switch). Names the optimizer does not rank follow, in DB row
         order.

    An EMPTY return value means "no candidates for this scene" — the
    caller applies its documented fallback (legacy behaviour / parent).
    Config-level failures degrade to empty + WARNING, never an exception,
    so a broken yaml can never crash a workflow phase.
    """
    scene = _require_scene(scene)
    config = _load_config(_config_path(config_path))
    if not config:
        return []

    # An explicit-but-invalid scene mapping (e.g. ``prd: 42``) is a
    # configuration error for THIS scene — return an empty chain rather
    # than silently routing the scene to the default tier.
    scenes = config.get("scenes")
    explicit_tier: Optional[str] = None
    if isinstance(scenes, dict) and scene in scenes:
        raw = scenes.get(scene)
        if isinstance(raw, str) and raw.strip():
            explicit_tier = raw.strip()
        else:
            log.warning(
                "scene=%s has a non-string tier mapping %r in "
                "provider_routing.yaml; chain empty", scene, raw,
            )
            return []

    tier = explicit_tier or resolve_scene_tier(scene, config_path=config_path)
    tiers = config.get("tiers")
    if not isinstance(tiers, dict):
        log.warning(
            "provider_routing.yaml has no valid 'tiers' mapping; "
            "scene=%s chain empty", scene,
        )
        return []
    if tier not in tiers:
        log.warning(
            "scene=%s resolved to tier=%r which is not declared in "
            "provider_routing.yaml tiers; chain empty", scene, tier,
        )
        return []

    db_names = _list_display_names(db_path)
    if not db_names:
        log.warning(
            "no live CC Switch providers with an Anthropic endpoint; "
            "scene=%s chain empty", scene,
        )
        return []
    return _resolve_tier_chain(scene, tier, tiers, db_names)


def _resolve_tier_chain(
    scene: str,
    tier: str,
    tiers: Dict,
    db_names: List[str],
) -> List[str]:
    """Resolve ONE tier's pattern list to ordered provider display names.

    Split out of :func:`resolve_provider_chain` on 2026-09-22 so the
    cross-tier fallback walk (:func:`resolve_fallback_chains`) can reuse
    the membership + ranking rules instead of re-deriving them. The
    caller supplies ``db_names`` (one CC Switch read for the whole walk)
    and owns the empty-config warnings.
    """
    patterns = tiers.get(tier)
    if patterns is None:
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        log.warning(
            "tier=%r has an empty or invalid pattern list; chain empty",
            tier,
        )
        return []

    optimizer = _optimizer_order()
    optimizer_rank = {name: i for i, name in enumerate(optimizer)}
    optimizer_set = set(optimizer)

    # Compile once; a bad regex is skipped with a warning, never fatal.
    compiled: List[re.Pattern] = []
    for raw in patterns:
        if not isinstance(raw, str) or not raw.strip():
            log.warning(
                "tier=%r contains a non-string pattern %r — skipped",
                tier, raw,
            )
            continue
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            log.warning(
                "tier=%r has invalid regex %r (%s) — skipped",
                tier, raw, exc,
            )
    if not compiled:
        return []

    # 1. MEMBERSHIP — every name matched by ANY pattern of the tier, in
    #    DB row order. The pattern list is a set of filters; the order
    #    the patterns happen to be written in carries no meaning.
    hits: List[str] = []
    seen = set()
    for pattern in compiled:
        for name in db_names:
            if name in seen:
                continue
            if pattern.search(name):
                seen.add(name)
                hits.append(name)

    # 2. RANKING — CC Switch's current order decides (2026-09-14): the live order wins,
    #    not the ordering inside the high/medium/low tier arrays. The live
    #    order arrives as the optimizer chain, which the optimizer keeps
    #    in sync with CC Switch via its /api/reorder writeback — so a
    #    rule-driven demotion (e.g. B-PRO during its peak window) is
    #    honoured here for free.
    #
    #    Names the optimizer does not rank stay BEHIND every ranked one,
    #    in DB row order — a partial/stale provider-order.json must
    #    degrade to "these are still candidates", never to "drop them".
    ranked = [n for n in hits if n in optimizer_set]
    ranked.sort(key=lambda n: optimizer_rank[n])
    unranked = [n for n in hits if n not in optimizer_set]
    return ranked + unranked


#: Which tiers a scene's chain widens into once its own tier has no
#: reachable provider left (2026-09-22).
#:
#: The failure that motivated it: a scene whose tier listed two providers
#: fell through to the parent process env as soon as both answered 429,
#: even though other tiers had reachable providers. Reaching the end of
#: one tier is not the same as having nowhere left to go — the chain
#: should keep descending before it gives up.
#:
#: ``medium`` is the tier that actually broke: it declares two providers,
#: so "every provider in the tier failed" is two 429s away and the run
#: had nowhere to go. Widening escalates toward the STRONGER tier first —
#: finishing the work on a pricier model strictly beats dropping the
#: task, and the dispatch logs the widening so the cost is visible.
_TIER_WIDENING: Dict[str, tuple] = {
    "high": ("high", "medium", "low"),
    "medium": ("medium", "high", "low"),
    "low": ("low", "medium", "high"),
}

#: Widening order for a tier name not present in :data:`_TIER_WIDENING`.
_DEFAULT_TIER_WIDENING: tuple = ("medium", "high", "low")


def _call_resolve_provider_chain(
    scene: str,
    config_path: Optional[Path],
    db_path,
) -> List[str]:
    """Call the public resolver, tolerating a ``scene``-only stub.

    ``resolve_provider_chain`` is the documented monkeypatch seam for
    scene routing, and several tests replace it with a one-argument
    lambda (``lambda scene: [...]``). Forwarding ``config_path`` /
    ``db_path`` to such a stub raises ``TypeError``, which the caller
    would swallow into "no chain" and silently fall back to the legacy
    provider walk. A ``scene``-only stub means "ignore the path
    overrides", so retry without them rather than losing the seam.
    """
    try:
        return resolve_provider_chain(
            scene, config_path=config_path, db_path=db_path,
        )
    except TypeError:
        return resolve_provider_chain(scene)


def resolve_fallback_chains(
    scene: str,
    config_path: Optional[Path] = None,
    db_path=None,
) -> List[List[str]]:
    """Return every chain to try for ``scene``, the scene's tier first.

    Element 0 is exactly :func:`resolve_provider_chain`'s answer — the
    scene's own tier, resolved through the same public seam so any
    caller that stubs it still sees its stub. The remaining elements are
    the other declared tiers in widening order
    (:data:`_TIER_WIDENING`), resolved with the same membership +
    ranking rules.

    An EMPTY return means the scene's own tier matched no live provider
   — the scene is not routable, and the caller applies its own
    documented fallback (legacy walk / parent). Widening is deliberately
    NOT applied there: a tier that matched nothing is usually a
    configuration problem, and silently rerouting it would hide that.

    Empty later-tier chains are omitted, and a tier whose chain is
    identical to one already returned is dropped (``medium`` and ``low``
    both read ``["^Vendor A"]`` in the shipped config).
    """
    scene = _require_scene(scene)
    primary = _call_resolve_provider_chain(scene, config_path, db_path)
    if not primary:
        return []

    config = _load_config(_config_path(config_path))
    if not config:
        return [primary]
    tiers = config.get("tiers")
    if not isinstance(tiers, dict):
        return [primary]

    db_names = _list_display_names(db_path)
    if not db_names:
        return [primary]

    primary_tier = resolve_scene_tier(scene, config_path=config_path)
    order = list(_TIER_WIDENING.get(primary_tier, _DEFAULT_TIER_WIDENING))
    # Any tier the file declares but the widening table does not name
    # still gets a turn, in declaration order — a future tier must not
    # silently become unreachable.
    order.extend(t for t in tiers if t not in order)

    chains: List[List[str]] = [primary]
    seen_tiers: set = {primary_tier}
    seen_chains: set = {tuple(primary)}
    for tier in order:
        if tier in seen_tiers or tier not in tiers:
            continue
        seen_tiers.add(tier)
        chain = _resolve_tier_chain(scene, tier, tiers, db_names)
        if not chain:
            continue
        key = tuple(chain)
        if key in seen_chains:
            continue
        seen_chains.add(key)
        chains.append(chain)
    return chains


# Public aliases kept minimal on purpose — this module's surface is the
# three resolve/clear functions plus the error type.
__all__ = [
    "ProviderRoutingError",
    "clear_routing_cache",
    "resolve_fallback_chains",
    "resolve_provider_chain",
    "resolve_scene_tier",
]
