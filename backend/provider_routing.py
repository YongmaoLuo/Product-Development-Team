"""Scene → tier → provider routing core (2026-09-13).

User-pinned contract:

  * The backend manages PROVIDERS; the MODEL and the REASONING EFFORT
    come from the routing yaml whenever a tier entry pins them. A tier
    entry's ``model`` / ``effort`` pins are CONFIGURATION the operator
    wrote (see the override bullet below) — this module has no model
    catalogue of its own and never guesses. A field the entry does not
    pin is left ALONE: the provider row's own complete ``ANTHROPIC_*``
    env block is forwarded verbatim to the sub-agent subprocess, byte
    for byte, exactly as CC Switch configured it.

  * ``backend/configs/provider_routing.yaml`` maps workflow scenes to
    tiers and tiers to regex patterns over CC Switch provider display
    names. A tier entry is either a bare regex string (legacy) or a
    MAPPING whose ``pattern`` key carries the regex; both spellings may
    be mixed in one tier and mean the same thing. The dispatch caller
    may ONLY pick providers from the chain this module returns, so
    cross-tier fallback is structurally impossible.

  * A tier entry may ALSO pin a ``model`` / ``effort`` for the providers
    its pattern matches. That pin is CONFIGURATION, read through
    :func:`resolve_provider_overrides` — a pure lookup answering "does
    this scene pin a model or a reasoning effort for this provider?"
    with an EMPTY mapping when nothing is pinned. The function changes no
    provider's env block by itself; the dispatch layer owns what to do
    with the answer — and it lands a pinned effort in BOTH places at
    once: the sub-agent subprocess env variable
    ``CLAUDE_CODE_EFFORT_LEVEL`` AND the top-level ``effortLevel`` key
    of the per-call settings payload, i.e. the settings file the CLI is
    handed through ``--settings``. Writing only one of the two would let
    the other channel overwrite it. The effort is deliberately NOT written
    into the settings file's ``env`` block, which is the ``ANTHROPIC_*``
    endpoint/credential channel. A pinned ``model`` fills all FOUR model
    variables of the subprocess env — ``ANTHROPIC_MODEL``,
    ``ANTHROPIC_DEFAULT_SONNET_MODEL``,
    ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` and
    ``ANTHROPIC_DEFAULT_OPUS_MODEL`` — so the CLI cannot hold two
    different answers about which model the call runs on.

  * A TIER IS A POOL, and a pool is not a suggestion to look
    elsewhere. Inside the scene's own tier the caller may move between
    providers freely — that is what "the pool has room" means — but
    reaching the end of the pool ends the search for candidates *here*.
    A tier whose providers are all busy, parked or out of budget is a
    reason to WAIT for that tier to come back, never to serve the call
    from a tier nobody put this scene in. There is therefore exactly
    one resolver below, :func:`resolve_provider_chain`, and it answers
    for one tier.

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
    EMPTY chain with a WARNING log, never an exception — a broken yaml
    must not crash a workflow phase from inside a dispatcher.

  * What an empty chain MEANS is the dispatcher's policy, and this
    module is written to make that policy possible. The tier table is
    an ALLOWLIST: a provider no tier names is not approved for that
    scene, so it is not a fallback. A scene-active dispatch therefore
    fails on an empty chain rather than reaching for a provider outside
    the table — and because the config-level failures above feed the
    same empty chain, the failure names the config problem too.

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

#: The reasoning-effort levels a tier entry may pin. The vocabulary is
#: closed and case-SENSITIVE: ``MAX`` is not ``max``, and an unknown
#: level is dropped with a WARNING rather than passed through.
EFFORT_LEVELS = frozenset({'low', 'medium', 'high', 'xhigh', 'max'})

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

    Failures degrade to ``[]``: routing then yields an empty chain,
    which is a routing answer (nothing matches) rather than an error,
    so an unreachable database can never crash a workflow phase. The
    dispatcher decides what to do with that answer; this function only
    declines to raise.
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

    An EMPTY return value means "no candidates for this scene". It is a
    routing answer, not an exception, and the caller owns the policy for
    it. Config-level failures degrade to empty + WARNING for the same
    reason — a broken yaml must never crash a workflow phase.
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


def _tier_entry_pattern(entry: object, tier: str) -> Optional[str]:
    """Return the regex source carried by ONE tier entry, or None.

    A tier entry is either a legacy bare regex string, or a MAPPING
    whose ``pattern`` key carries the regex. The mapping form exists so a
    tier can pin per-provider settings (model / effort) alongside the
    pattern; THIS layer reads only ``pattern`` — the other keys are
    ignored, so membership is decided by the regex alone.

    The returned string is the pattern verbatim (not stripped, not
    rewritten): the regex keeps its original semantics.

    None means "this entry carries no usable pattern" — a mapping
    without ``pattern``, a non-string ``pattern``, a blank one, or an
    entry that is neither a string nor a mapping. The caller skips such
    an entry with a WARNING; the remaining entries of the tier still
    resolve. Nothing here raises.
    """
    if isinstance(entry, dict):
        pattern = entry.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            return pattern
        log.warning(
            "tier=%r contains a mapping entry without a usable 'pattern' "
            "value %r — skipped",
            tier, entry,
        )
        return None

    if isinstance(entry, str) and entry.strip():
        return entry

    # Legacy shapes: a bare blank string and any non-string entry were
    # both skipped before the mapping form existed — keep that wording.
    log.warning(
        "tier=%r contains a non-string pattern %r — skipped",
        tier, entry,
    )
    return None


def _resolve_tier_chain(
    scene: str,
    tier: str,
    tiers: Dict,
    db_names: List[str],
) -> List[str]:
    """Resolve ONE tier's pattern list to ordered provider display names.

    One tier, never two: the membership + ranking rules below are the
    pool's, and a caller that wanted a different tier's providers asked
    the wrong scene. ``db_names`` is supplied by the caller so one CC
    Switch read answers for the whole walk, and the empty-config
    warnings belong to the caller too.
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
    # An entry is either a bare pattern string or a mapping carrying a
    # ``pattern`` key — both resolve to the same regex source here.
    compiled: List[re.Pattern] = []
    for raw in patterns:
        pattern = _tier_entry_pattern(raw, tier)
        if pattern is None:
            continue
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            log.warning(
                "tier=%r has invalid regex %r (%s) — skipped",
                tier, pattern, exc,
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


def _scene_tier_for_overrides(scene: str, config: Dict) -> Optional[str]:
    """Resolve ``scene`` → tier for the override lookup.

    Mirrors :func:`resolve_provider_chain`'s rule: an explicit scene
    mapping wins, and an explicit-but-invalid one (``prd: 42``) is a
    configuration error for THAT scene — ``None`` means "no overrides at
    all", not "fall back to the default tier". A scene that is simply
    absent falls back to ``default_tier`` and then to the module-level
    ``_DEFAULT_TIER``.
    """
    scenes = config.get("scenes")
    if isinstance(scenes, dict) and scene in scenes:
        raw = scenes.get(scene)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        log.warning(
            "scene=%s has a non-string tier mapping %r in "
            "provider_routing.yaml; overrides empty", scene, raw,
        )
        return None

    default = config.get("default_tier")
    if isinstance(default, str) and default.strip():
        return default.strip()
    return _DEFAULT_TIER


def _apply_entry_overrides(
    overrides: Dict[str, str],
    entry: Dict,
    tier: str,
    provider_name: str,
) -> None:
    """Fold ONE matching tier entry's ``model`` / ``effort`` into ``overrides``.

    A field already present keeps its value — the first matching entry
    wins it — so only a still-missing field is considered here. An
    illegal value is dropped with a WARNING and leaves the OTHER field
    of the same entry untouched: one bad key never voids the entry.
    """
    if "model" not in overrides and "model" in entry:
        raw_model = entry.get("model")
        if isinstance(raw_model, str) and raw_model.strip():
            overrides["model"] = raw_model.strip()
        else:
            log.warning(
                "tier=%r entry matching provider %r has an invalid 'model' "
                "value %r — ignored", tier, provider_name, raw_model,
            )

    if "effort" not in overrides and "effort" in entry:
        raw_effort = entry.get("effort")
        if isinstance(raw_effort, str) and raw_effort.strip() in EFFORT_LEVELS:
            overrides["effort"] = raw_effort.strip()
        else:
            log.warning(
                "tier=%r entry matching provider %r has an invalid 'effort' "
                "value %r — ignored (expected one of %s)",
                tier, provider_name, raw_effort, ", ".join(sorted(EFFORT_LEVELS)),
            )


def resolve_provider_overrides(
    scene: str,
    provider_name: str,
    config_path: Optional[Path] = None,
) -> Dict[str, str]:
    """Return the ``model`` / ``effort`` this scene pins for a provider.

    A PURE configuration read — it never touches CC Switch and never
    writes anything. The lookup is:

      1. ``scene`` → tier via the ``scenes`` map (missing scene →
         ``default_tier``, then the module-level ``_DEFAULT_TIER``).
      2. tier → its entry list, walked in DOCUMENT order. Every entry
         whose ``pattern`` matches ``provider_name`` may contribute the
         ``model`` / ``effort`` it carries.
      3. The FIRST matching entry that supplies a field wins that field;
         a later matching entry may only fill a field that is still
         missing. A legacy bare-string entry declares MEMBERSHIP only —
         it never contributes a field.

    An empty mapping means "nothing is configured for this provider" —
    the caller then writes not one byte. Config-level failures (missing
    file, invalid yaml, unknown tier, bad regex, an illegal ``model`` /
    ``effort`` value) degrade to that same empty mapping, with a
    WARNING where the operator can act on it, and never raise: a broken
    yaml must not crash a workflow phase from inside a dispatcher.

    The config is read through the module's ``(mtime_ns, size)``
    fingerprint, so an edited yaml is honoured by the very next call.
    """
    if not isinstance(provider_name, str) or not provider_name.strip():
        return {}
    if not isinstance(scene, str) or not scene.strip():
        log.warning(
            "resolve_provider_overrides called with a non-string scene %r; "
            "no overrides", scene,
        )
        return {}

    config = _load_config(_config_path(config_path))
    if not config:
        return {}

    tier = _scene_tier_for_overrides(scene.strip(), config)
    if tier is None:
        return {}

    tiers = config.get("tiers")
    if not isinstance(tiers, dict):
        log.warning(
            "provider_routing.yaml has no valid 'tiers' mapping; "
            "scene=%s overrides empty", scene,
        )
        return {}
    if tier not in tiers:
        log.warning(
            "scene=%s resolved to tier=%r which is not declared in "
            "provider_routing.yaml tiers; overrides empty", scene, tier,
        )
        return {}

    entries = tiers.get(tier)
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        log.warning(
            "tier=%r has an empty or invalid pattern list; overrides empty",
            tier,
        )
        return {}

    overrides: Dict[str, str] = {}
    for entry in entries:
        pattern = _tier_entry_pattern(entry, tier)
        if pattern is None:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            log.warning(
                "tier=%r has invalid regex %r (%s) — skipped",
                tier, pattern, exc,
            )
            continue
        if not compiled.search(provider_name):
            continue
        if isinstance(entry, dict):
            _apply_entry_overrides(overrides, entry, tier, provider_name)
        if len(overrides) >= 2:
            # Both fields are settled — no later entry can change them.
            break
    return overrides


# Public aliases kept minimal on purpose — this module's surface is the
# two resolve functions, the override lookup, the cache clear, and the
# error type. One tier resolves one chain; there is no second entry
# point that answers for a different tier.
__all__ = [
    "EFFORT_LEVELS",
    "ProviderRoutingError",
    "clear_routing_cache",
    "resolve_provider_chain",
    "resolve_provider_overrides",
    "resolve_scene_tier",
]
