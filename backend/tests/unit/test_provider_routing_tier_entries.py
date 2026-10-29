"""Unit tests for the tier-entry shapes ``provider_routing`` accepts.

Spec (2026-10-28):
  * A ``tiers`` entry is either a legacy bare regex string, or a MAPPING
    carrying a ``pattern`` key. The mapping form exists so a tier can
    later pin a ``model`` / ``effort`` per provider — but THIS layer
    reads only ``pattern``: membership is decided by the regex alone.
  * Both forms may be mixed inside one tier, and the resulting chain
    (members AND order) must be identical to the all-string spelling of
    the same tier.
  * The legacy spelling keeps its exact semantics: an all-string tier
    resolves to the same chain, in the same order, as before.
  * An entry that carries no usable pattern (mapping without
    ``pattern``, a non-string ``pattern``, a blank ``pattern``) is
    SKIPPED with a WARNING — the remaining entries of that tier still
    resolve, and nothing raises.
  * A pattern that fails to compile is skipped with a WARNING, for the
    mapping form just as for the legacy string form.

All tests use a tempdir yaml + a stubbed DB surface, so no real CC
Switch state is touched.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import provider_routing
from provider_routing import clear_routing_cache, resolve_provider_chain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Display names standing in for CC Switch DB rows.
DB_NAMES = [
    "Vendor D API",
    "Vendor B Pro",
    "Vendor C App",
    "Vendor A",
    "Vendor A Pro",
    "Vendor A API",
]

# The live optimizer chain (stubbed) — deliberately NOT the DB row order,
# so a chain that tracks the optimizer is distinguishable from one that
# leaks DB row order.
OPTIMIZER_ORDER = [
    "Vendor D API",
    "Vendor B Pro",
    "Vendor C App",
    "Vendor A Pro",
    "Vendor A API",
    "Vendor A",
]


@pytest.fixture
def routing_yaml(tmp_path):
    """Write a routing yaml into a tmp_path and point the loader at it.

    Returns a callable ``(yaml_text) -> Path``. ``clear_routing_cache``
    runs on teardown so the module-level cache never leaks between
    tests.
    """

    def _write(content: str) -> Path:
        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text(textwrap.dedent(content), encoding="utf-8")
        return cfg

    yield _write
    clear_routing_cache()


@pytest.fixture(autouse=True)
def _stub_db_names(monkeypatch):
    """Stub the display-name enumeration so tests never touch real state."""
    monkeypatch.setattr(
        provider_routing, "_list_display_names", lambda db_path=None: list(DB_NAMES)
    )
    monkeypatch.setattr(
        provider_routing, "_optimizer_order", lambda: list(OPTIMIZER_ORDER)
    )


# ---------------------------------------------------------------------------
# Legacy string entries — semantics must not move
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_legacy_string_entries_resolve_unchanged(routing_yaml):
    """An all-string tier resolves to the chain it always resolved to.

    This is the regression guard for the whole task: the mapping form is
    ADDED, the legacy form keeps its members and its order.
    """
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high: ["^Vendor D", "^Vendor B Pro$", "^Vendor C"]
        scenes:
          prd: high
        """
    )
    assert resolve_provider_chain("prd", config_path=cfg) == [
        "Vendor D API",
        "Vendor B Pro",
        "Vendor C App",
    ]


@pytest.mark.unit
def test_single_string_tier_remains_compatible(routing_yaml):
    """A tier written as ONE bare string (not a list) still resolves."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high: "^Vendor D"
        scenes:
          prd: high
        """
    )
    assert resolve_provider_chain("prd", config_path=cfg) == ["Vendor D API"]


# ---------------------------------------------------------------------------
# Mapping entries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dict_entry_pattern_is_used_for_membership(routing_yaml):
    """A mapping entry contributes its ``pattern`` to tier membership."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - pattern: '^Vendor D'
        scenes:
          prd: high
        """
    )
    chain = resolve_provider_chain("prd", config_path=cfg)
    assert "Vendor D API" in chain
    assert chain == ["Vendor D API"]


@pytest.mark.unit
def test_mixed_entries_match_the_all_string_equivalent(routing_yaml):
    """A tier mixing strings and mappings resolves like its string twin."""
    mixed = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - '^Vendor D'
            - pattern: '^Vendor A Pro$'
              model: example-model
              effort: high
        scenes:
          prd: high
        """
    )
    mixed_chain = resolve_provider_chain("prd", config_path=mixed)
    clear_routing_cache()

    plain = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high: ["^Vendor D", "^Vendor A Pro$"]
        scenes:
          prd: high
        """
    )
    plain_chain = resolve_provider_chain("prd", config_path=plain)

    assert mixed_chain == plain_chain == ["Vendor D API", "Vendor A Pro"]


@pytest.mark.unit
def test_entry_without_pattern_is_skipped_with_warning(routing_yaml, caplog):
    """A mapping without ``pattern`` is skipped with a WARNING."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - pattern: '^Vendor D'
            - model: 'Vendor B Pro'
              effort: high
        scenes:
          prd: high
        """
    )
    with caplog.at_level("WARNING", logger="provider_routing"):
        chain = resolve_provider_chain("prd", config_path=cfg)

    # The rejected entry contributes nothing — not even the provider its
    # ``model`` key names.
    assert chain == ["Vendor D API"]
    assert "Vendor B Pro" not in chain
    assert any(
        record.levelname == "WARNING" and "pattern" in record.message
        for record in caplog.records
    )


@pytest.mark.unit
def test_entry_with_non_string_pattern_is_skipped(routing_yaml, caplog):
    """A mapping whose ``pattern`` is not a string is skipped + WARNING."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - pattern: '^Vendor D'
            - pattern: 42
              model: example-model
        scenes:
          prd: high
        """
    )
    with caplog.at_level("WARNING", logger="provider_routing"):
        chain = resolve_provider_chain("prd", config_path=cfg)

    assert chain == ["Vendor D API"]
    assert any(
        record.levelname == "WARNING" and "pattern" in record.message
        for record in caplog.records
    )


@pytest.mark.unit
def test_entry_with_blank_pattern_is_skipped(routing_yaml, caplog):
    """A mapping whose ``pattern`` is blank is skipped + WARNING."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - pattern: '^Vendor D'
            - pattern: '   '
              model: example-model
        scenes:
          prd: high
        """
    )
    with caplog.at_level("WARNING", logger="provider_routing"):
        chain = resolve_provider_chain("prd", config_path=cfg)

    assert chain == ["Vendor D API"]
    assert any(
        record.levelname == "WARNING" and "pattern" in record.message
        for record in caplog.records
    )


@pytest.mark.unit
def test_entry_with_extra_keys_does_not_change_membership(routing_yaml):
    """``model`` / ``effort`` keys do not affect membership or order."""
    with_mapping = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - pattern: '^Vendor A Pro$'
              model: example-model
              effort: high
        scenes:
          prd: high
        """
    )
    mapping_chain = resolve_provider_chain("prd", config_path=with_mapping)
    clear_routing_cache()

    plain = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high: ["^Vendor A Pro$"]
        scenes:
          prd: high
        """
    )
    plain_chain = resolve_provider_chain("prd", config_path=plain)

    assert mapping_chain == plain_chain == ["Vendor A Pro"]


@pytest.mark.unit
def test_dict_entry_with_invalid_regex_is_skipped(routing_yaml, caplog):
    """A mapping entry whose regex does not compile is skipped, not fatal."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: high
        tiers:
          high:
            - pattern: '([bad'
            - pattern: '^Vendor D'
        scenes:
          prd: high
        """
    )
    with caplog.at_level("WARNING", logger="provider_routing"):
        chain = resolve_provider_chain("prd", config_path=cfg)

    assert chain == ["Vendor D API"]
    assert any(
        record.levelname == "WARNING" and "invalid" in record.message.lower()
        for record in caplog.records
    )


@pytest.mark.unit
def test_tier_entry_pattern_helper_returns_the_regex_source(routing_yaml):
    """``_tier_entry_pattern`` is the seam the resolver compiles from."""
    assert provider_routing._tier_entry_pattern("^Vendor D", "high") == "^Vendor D"
    assert provider_routing._tier_entry_pattern(
        {"pattern": "^Vendor D"}, "high"
    ) == "^Vendor D"
    assert provider_routing._tier_entry_pattern({"model": "m"}, "high") is None
    assert provider_routing._tier_entry_pattern({"pattern": 42}, "high") is None
    assert provider_routing._tier_entry_pattern({"pattern": "  "}, "high") is None
