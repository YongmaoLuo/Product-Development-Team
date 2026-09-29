"""Naming / ordering guards for ``backend/configs/_base.yaml``.

The base configuration must stay free of:

1. Any *hardcoded* provider fallback order. The fallback chain is
   supplied at runtime by the provider optimizer (and the CC Switch DB
   read on top of it), not declared statically in YAML. Declaring an
   order here would silently override the runtime decision and re-
   introduce the "two sources of truth" bug the migration eliminated.

2. Any *bare* legacy provider names (``vendor-a`` / ``vendor-b``). All
   provider identifiers in YAML must be in kebab-case form
   (``vendor-a-pro`` / ``vendor-b-pro``). The decision of which kebab-
   case form to use is made at runtime by the consumer
   (``cc_switch``); the base file is provider-neutral
   and only declares generic provider definitions.

These two rules together guarantee that ``_base.yaml`` stays a
"placeholder" that downstream code can fill in dynamically. The
``model_map`` block was removed entirely on 2026-09-13 (model
management is delegated to CC Switch); the removal banner and the
absence of a static ``model_map`` key are pinned by
``test_base_yaml_model_map_removed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Set, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"
BASE_YAML = BACKEND_DIR / "configs" / "_base.yaml"

# Keys that would indicate a hardcoded provider fallback / priority /
# order list. The migration removed all of these from ``_base.yaml``;
# the order is now supplied dynamically by an external producer
# (see ``backend/cc_switch.py``).
FORBIDDEN_PROVIDER_ORDER_KEYS: Tuple[str, ...] = (
    "provider_order",
    "provider_priority",
    "provider_fallback_order",
    "provider_fallback_chain",
    "provider_routing",
    "provider_chain",
)

# Bare legacy provider IDs. The full kebab-case IDs
# (``vendor-a-pro`` / ``vendor-b-pro``) are allowed; only the
# un-hyphenated forms are forbidden.
LEGACY_BARE_PROVIDER_IDS: Tuple[str, ...] = (
    "vendor-a",
    "vendor-b",
)


def _yaml_or_skip() -> Any:
    """Return the parsed YAML document, skipping the test if PyYAML
    is not installed or the file is missing.

    The tests in this module are *parsing-driven* — we want to assert
    against the *structured* YAML tree, not a raw-text scan. A raw
    scan would also match substrings inside URL strings (e.g.
    ``https://api.vendor-a.chat``) which are not provider names at
    all. Parsing first lets the tests reason about keys and values
    on their own merits.
    """
    if not BASE_YAML.exists():
        pytest.skip(f"{BASE_YAML} does not exist")
    try:
        import yaml
    except ImportError:  # pragma: no cover - guard
        pytest.skip("PyYAML not installed")
    return yaml.safe_load(BASE_YAML.read_text(encoding="utf-8"))


def _walk_keys(node: Any) -> Iterable[str]:
    """Yield every dict key reachable from ``node`` (recursive)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def test_base_yaml_no_legacy_provider_order() -> None:
    """``_base.yaml`` does not declare a hardcoded provider order list.

    The provider fallback chain is supplied at runtime by the
    optimizer + CC Switch DB read. Any of the following keys would
    re-introduce a static order and break that contract:

    * ``provider_order``
    * ``provider_priority``
    * ``provider_fallback_order``
    * ``provider_fallback_chain``
    * ``provider_routing``
    * ``provider_chain``

    This test fails immediately if any of them appears anywhere in
    the parsed YAML tree (top level *or* nested), with a message
    that names the offending key and the path that led to it.
    """
    data = _yaml_or_skip()

    forbidden: Set[str] = set(FORBIDDEN_PROVIDER_ORDER_KEYS)

    def _find(node: Any, trail: Tuple[str, ...]) -> List[Tuple[Tuple[str, ...], str]]:
        out: List[Tuple[Tuple[str, ...], str]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                key_str = str(key)
                child_trail = trail + (key_str,)
                if key_str in forbidden:
                    out.append((child_trail, key_str))
                out.extend(_find(value, child_trail))
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                child_trail = trail + (f"[{idx}]",)
                out.extend(_find(item, child_trail))
        return out

    violations = _find(data, ())
    if violations:
        rendered = "\n  - ".join(
            f"{'.'.join(trail) or '<root>'}  -> forbidden key {key!r}"
            for trail, key in violations
        )
        pytest.fail(
            "_base.yaml declares a hardcoded provider order, which "
            "is owned by the runtime optimizer, not the base config:\n"
            f"  - {rendered}"
        )


def test_base_yaml_no_legacy_names() -> None:
    """No bare ``vendor-a`` / ``vendor-b`` keys appear in ``_base.yaml``.

    Provider identifiers in YAML must be in kebab-case form
    (``vendor-a-pro``, ``vendor-b-pro``). The choice of kebab-case form
    is the responsibility of the runtime consumer
    (``cc_switch``) — the base file is provider-
    neutral and should not bake in a specific naming decision.

    This test inspects *keys only* (not values), so URL strings that
    happen to contain the substring ``vendor-a`` (e.g.
    ``https://api.vendor-a.chat``) are deliberately not flagged: they
    are URL values, not provider IDs.
    """
    data = _yaml_or_skip()

    forbidden: Set[str] = set(LEGACY_BARE_PROVIDER_IDS)

    bad: List[Tuple[Tuple[str, ...], str]] = []

    def _walk(node: Any, trail: Tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_str = str(key)
                child_trail = trail + (key_str,)
                if key_str in forbidden:
                    bad.append((child_trail, key_str))
                _walk(value, child_trail)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _walk(item, trail + (f"[{idx}]",))

    _walk(data, ())

    if bad:
        rendered = "\n  - ".join(
            f"{'.'.join(trail) or '<root>'}  -> bare legacy provider id {key!r}"
            for trail, key in bad
        )
        pytest.fail(
            "_base.yaml contains bare legacy provider IDs; the base "
            "file must be provider-neutral and use kebab-case IDs:\n"
            f"  - {rendered}"
        )


def test_base_yaml_parses_cleanly() -> None:
    """Sanity: the base YAML loads as a non-empty mapping.

    Kept here so that the two contract tests above are not run on a
    corrupted/empty file without an obvious failure mode — pytest
    would otherwise report the contract assertion as the source of
    the failure and hide the parse error.
    """
    data = _yaml_or_skip()
    assert isinstance(data, dict), (
        f"_base.yaml should be a mapping at the top level, got {type(data).__name__}"
    )
    assert data, "_base.yaml parsed to an empty mapping"


def test_base_yaml_model_map_removed() -> None:
    """The ``model_map`` section was removed from ``_base.yaml``.

    2026-09-13 contract: model management is delegated ENTIRELY to CC
    Switch — each provider row in the CC Switch DB carries its own
    complete ANTHROPIC_* env block, which the dispatch walk forwards
    verbatim. The old tiered ``model_map`` block (complex/medium →
    provider → model) was deleted with a "REMOVED (2026-09-13)"
    banner so maintainers are pointed at
    ``configs/provider_routing.yaml`` / ``provider_routing.py``
    instead of re-adding a static model binding.

    We assert (a) ``model_map`` is NOT a top-level key in the parsed
    YAML and (b) the raw text carries the REMOVED banner so a future
    refactor that silently drops the documentation (or re-adds the
    block) fails CI before shipping.
    """
    if not BASE_YAML.exists():
        pytest.skip(f"{BASE_YAML} does not exist")

    data = _yaml_or_skip()
    assert "model_map" not in data, (
        "_base.yaml must NOT declare a 'model_map' top-level key — "
        "model management is delegated to CC Switch (2026-09-13). "
        "Re-adding it would silently re-enable tiered model binding "
        "inside the workflow."
    )

    raw = BASE_YAML.read_text(encoding="utf-8")
    assert "REMOVED (2026-09-13)" in raw, (
        "The _base.yaml model_map removal banner must stay in place so "
        "future maintainers find the pointer to provider_routing.yaml "
        "instead of re-adding a static model_map block."
    )


# Allow ``test_base_yaml_naming`` to be invoked as a module name
# (e.g. ``pytest -k naming``) without listing the four tests
# explicitly.
__all__ = [
    "test_base_yaml_no_legacy_provider_order",
    "test_base_yaml_no_legacy_names",
    "test_base_yaml_parses_cleanly",
    "test_base_yaml_model_map_removed",
]
