"""Per-provider concurrency caps, declared as regex rules over provider names.

Why a config file and not a table
---------------------------------
A cap describes *one operator's* provider rows — how many parallel
sub-agents their plan with that vendor tolerates. A name-keyed table in
source therefore compiles one deployment's provider set into everybody's
install, and the names it carries are the operator's own. The rules
belong in a config file the operator owns, exactly like
``provider_routing.yaml`` does for scene → tier membership.

Config shape (``provider_capacity.yaml``)
-----------------------------------------
.. code-block:: yaml

    version: 1
    default_max_concurrency: 5
    providers:
      - pattern: "^vendor-a"
        max_concurrency: 10
      - pattern: "^vendor-b"
        max_concurrency: 3

``pattern`` is a Python regex, searched case-insensitively. Rules are
tried in order and the **first** match wins, so put narrow patterns
above broad ones. A provider no rule matches gets
``default_max_concurrency``.

Matching both spellings
-----------------------
Two naming schemes reach the dispatch layer: CC Switch display names
(``"Vendor A Pro"``, from the scene path) and the kebab-case logical ids
built from them (``"vendor-a-pro"``, from the executor path). A rule is
tested, case-insensitively, against the name as given **and** against
two normalized renderings of it — lower-cased with spaces/underscores
folded to hyphens, and that form with the hyphens back to spaces. One
pattern therefore covers a provider however it was spelled when it
arrived: ``^vendor a`` and ``^vendor-a`` both reach it. A pattern that
deliberately distinguishes the two spellings (an anchored ``^vendor-a$``)
still can.

Degradation
-----------
Every config-level failure — missing file, unreadable file, malformed
yaml, a rule that is not a mapping, a non-positive cap, an uncompilable
regex — drops the offending rule (or the whole file) and logs a WARNING.
The module then answers with the default cap. A bad capacity file must
never crash a workflow phase: the worst acceptable outcome is that
everything runs at the conservative default.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config_paths import resolve_provider_capacity_file

log = logging.getLogger("provider_capacity")

#: Cap for any provider no rule matches.
DEFAULT_MAX_CONCURRENCY: int = 5

#: Top-level key holding the per-rule cap.
_RULES_KEY = "providers"
_FALLBACK_KEY = "default_max_concurrency"

# Cache discipline mirrors ``provider_routing``: one entry keyed by the
# resolved path, invalidated on an (mtime_ns, size) change.
_cache_lock = threading.Lock()
_cached_path: Optional[str] = None
_cached_fingerprint: Optional[tuple] = None
_cached_rules: Optional[List["CapacityRule"]] = None
_cached_default: Optional[int] = None


@dataclass(frozen=True)
class CapacityRule:
    """One ``pattern → max_concurrency`` entry."""

    pattern: str
    max_concurrency: int
    _compiled: "re.Pattern[str]"

    def matches(self, candidate: str) -> bool:
        return self._compiled.search(candidate) is not None


def default_max_concurrency() -> int:
    """Cap applied to providers no rule matches."""
    _, fallback = _load()
    return fallback


def clear_capacity_cache() -> None:
    """Drop the cached rules; the next call re-reads disk.

    Operator escape hatch after an edit that did not change the file's
    mtime/size fingerprint, and the hook tests use.
    """
    global _cached_path, _cached_fingerprint, _cached_rules, _cached_default
    with _cache_lock:
        _cached_path = None
        _cached_fingerprint = None
        _cached_rules = None
        _cached_default = None


def _capacity_path(config_path: Optional[Path] = None) -> Path:
    if config_path is not None:
        return Path(config_path)
    return resolve_provider_capacity_file()


def _coerce_positive_int(value: object) -> Optional[int]:
    """``value`` as a positive int, or ``None`` if it is not one.

    ``bool`` is rejected explicitly: ``True`` is an ``int`` in Python and
    would silently become a cap of 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _parse_rules(data: object, source: str) -> Tuple[List[CapacityRule], int]:
    """Turn a parsed yaml document into ``(rules, default_cap)``.

    Malformed pieces are dropped individually with a WARNING rather than
    discarding the whole file, so one typo does not silently move every
    provider onto the default cap.
    """
    fallback = DEFAULT_MAX_CONCURRENCY
    if not isinstance(data, dict):
        log.warning(
            "%s: top level is %s, not a mapping; using defaults",
            source, type(data).__name__,
        )
        return [], fallback

    declared_default = _coerce_positive_int(data.get(_FALLBACK_KEY))
    if declared_default is not None:
        fallback = declared_default
    elif _FALLBACK_KEY in data:
        log.warning(
            "%s: %s=%r is not a positive integer; using %d",
            source, _FALLBACK_KEY, data.get(_FALLBACK_KEY), DEFAULT_MAX_CONCURRENCY,
        )

    raw_rules = data.get(_RULES_KEY)
    if raw_rules is None:
        log.warning(
            "%s: no '%s' key; every provider will use the %d default",
            source, _RULES_KEY, fallback,
        )
        return [], fallback
    if not isinstance(raw_rules, list):
        log.warning(
            "%s: '%s' is %s, not a list; using defaults",
            source, _RULES_KEY, type(raw_rules).__name__,
        )
        return [], fallback

    rules: List[CapacityRule] = []
    for index, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            log.warning("%s: %s[%d] is not a mapping; skipped", source, _RULES_KEY, index)
            continue
        pattern = entry.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            log.warning("%s: %s[%d] has no usable 'pattern'; skipped", source, _RULES_KEY, index)
            continue
        cap = _coerce_positive_int(entry.get("max_concurrency"))
        if cap is None:
            log.warning(
                "%s: %s[%d] (pattern %r) has no positive 'max_concurrency'; skipped",
                source, _RULES_KEY, index, pattern,
            )
            continue
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            log.warning(
                "%s: %s[%d] pattern %r does not compile (%s); skipped",
                source, _RULES_KEY, index, pattern, exc,
            )
            continue
        rules.append(CapacityRule(pattern=pattern, max_concurrency=cap, _compiled=compiled))

    return rules, fallback


def _load(config_path: Optional[Path] = None) -> Tuple[List[CapacityRule], int]:
    """Return ``(rules, default_cap)``, reading and caching the yaml."""
    global _cached_path, _cached_fingerprint, _cached_rules, _cached_default

    resolved = str(_capacity_path(config_path).resolve())
    try:
        stat = Path(resolved).stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        with _cache_lock:
            if _cached_path == resolved:
                _cached_path = None
                _cached_fingerprint = None
                _cached_rules = None
                _cached_default = None
        log.warning(
            "provider_capacity.yaml not found at %s; every provider uses the "
            "%d default cap",
            resolved, DEFAULT_MAX_CONCURRENCY,
        )
        return [], DEFAULT_MAX_CONCURRENCY

    with _cache_lock:
        if (
            _cached_rules is not None
            and _cached_path == resolved
            and _cached_fingerprint == fingerprint
        ):
            return _cached_rules, _cached_default if _cached_default else DEFAULT_MAX_CONCURRENCY

    try:
        import yaml
    except ImportError:
        log.warning("PyYAML unavailable; provider capacity falls back to the default cap")
        return [], DEFAULT_MAX_CONCURRENCY

    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        # ``yaml.YAMLError`` is not a ``ValueError`` subclass — catch it
        # by name or a malformed file raises out of the dispatch path.
        log.warning("provider_capacity.yaml at %s is not valid yaml (%s)", resolved, exc)
        return [], DEFAULT_MAX_CONCURRENCY
    except OSError as exc:
        log.warning("provider_capacity.yaml at %s is unreadable (%s)", resolved, exc)
        return [], DEFAULT_MAX_CONCURRENCY

    rules, fallback = _parse_rules(data, resolved)

    with _cache_lock:
        _cached_path = resolved
        _cached_fingerprint = fingerprint
        _cached_rules = rules
        _cached_default = fallback
    return rules, fallback


def _match_candidates(provider: str) -> List[str]:
    """Renderings of one provider reference that a rule is tested against.

    The dispatch layer spells a provider two ways — the CC Switch display
    name (``"Vendor A Pro"``) and its kebab-case form
    (``"vendor-a-pro"``) — and a rule should not have to know which one
    it will meet. Matching a small, closed set of renderings makes that
    true without a name→id table:

    * as given, so a pattern copied from the CC Switch UI works;
    * canonicalised (lower-cased, whitespace and underscores folded to
      hyphens), so ``^vendor-a`` covers both;
    * canonicalised with hyphens back to spaces, so ``^vendor a`` covers
      the kebab spelling too.

    All three are matched case-insensitively. A pattern that
    distinguishes the two spellings on purpose (``^vendor-a$``) still
    can — it simply will not match the display form, which is the
    intended reading of an anchored pattern.
    """
    raw = provider
    try:
        from dynamic_provider_concurrency import canonical_capacity_key

        canonical = canonical_capacity_key(provider)
    except Exception:  # pragma: no cover - an import cycle is a bug, not a state
        canonical = raw.strip().lower()

    candidates = [raw, canonical]
    spaced = canonical.replace("-", " ")
    if spaced not in candidates:
        candidates.append(spaced)
    # De-duplicate while preserving order, so a caller debugging a
    # non-match sees one entry per distinct rendering.
    seen: set[str] = set()
    ordered: List[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def capacity_for(provider: str) -> int:
    """Max concurrency for ``provider``, per the configured rules.

    First matching rule wins; the configured default applies when none
    matches. Never raises — a rule that cannot be compiled was already
    dropped at load time, and there is no ordering dependence between
    this module and the dispatch layer that could fail here.
    """
    if not isinstance(provider, str) or not provider.strip():
        return default_max_concurrency()

    rules, fallback = _load()
    candidates = _match_candidates(provider)
    for rule in rules:
        if any(rule.matches(candidate) for candidate in candidates):
            return rule.max_concurrency
    return fallback


def rules_snapshot() -> List[Dict[str, object]]:
    """The loaded rules, for logging and for tests."""
    rules, _ = _load()
    return [{"pattern": r.pattern, "max_concurrency": r.max_concurrency} for r in rules]


def configured_capacity_ceiling() -> int:
    """Upper bound on the whole fleet's in-flight sub-agents.

    Sums the caps of every *configured* rule. This is a ceiling rather
    than a count: rules can overlap, and providers that match no rule are
    not included, so the true fleet capacity is at most this plus the
    default for each unconfigured provider. It exists to bound plan-wide
    fan-out (``verification_agent``) without reintroducing a hand-written
    fleet number — the bound is derived from what the operator declared.

    Returns 0 when nothing is configured. Callers must treat 0 as "no
    declared capacity" and not as "run nothing".
    """
    rules, _ = _load()
    return sum(rule.max_concurrency for rule in rules)
