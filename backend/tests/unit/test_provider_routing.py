"""Unit tests for ``provider_routing`` — the scene→tier→provider routing core.

Spec (2026-09-13, ):
  * Configuration lives in ``backend/configs/provider_routing.yaml`` and is
    hot-reloadable (mtime-based re-read; ``PDT_PROVIDER_ROUTING_FILE`` env
    var overrides the path).
  * ``tiers`` maps a tier name (``high`` / ``medium`` / ``low``) to a
    list of regex patterns matched against CC Switch provider display
    names (and their canonical kebab-case IDs). The list declares
    tier MEMBERSHIP — its order carries no ranking meaning (2026-09-14
    change, see the ordering bullet below).
  * ``scenes`` maps a workflow scene (``prd``, ``execution``, ``refiner``,
    …) to a tier. Unknown scenes fall back to ``default_tier``.
  * ``resolve_provider_chain(scene)`` returns the ordered list of CC Switch
    display names for that scene — the caller's dispatch loop can ONLY
    pick providers from this chain, so cross-tier fallback is structurally
    impossible.
  * Regex matching uses ``re.search`` semantics (anchor explicitly with
    ``^`` / ``$``). A pattern that fails to compile is skipped with a
    warning — one bad regex must not break the whole chain.
  * EVERY provider matched by ANY pattern of the tier is ranked by the
    live provider-order chain (``provider-order.json`` order); names
    that chain does not rank follow, in DB row order (2026-09-14: the
    live order wins, not the ordering inside the high/medium/low
    arrays). Config
    pattern order is therefore membership-only — reordering the patterns
    inside a tier must not change the chain.
  * Fallback semantics: missing yaml → empty chain (caller falls back to
    the legacy behaviour); missing scene → default_tier; missing tier →
    empty chain; zero regex hits → empty chain.

All tests use a tempdir yaml + a stubbed DB surface so no real CC Switch
state is touched.
"""

import os
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import provider_routing
from provider_routing import (
    ProviderRoutingError,
    clear_routing_cache,
    resolve_provider_chain,
    resolve_scene_tier,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Display names standing in for CC Switch DB rows. ``db_names`` is what
# ``_list_display_names`` (monkeypatched) will report.
DB_NAMES = [
    "Vendor D API",
    "Vendor B Pro",
    "Vendor C App",
    "Vendor A",
    "Vendor A Pro",
    "Vendor A API",
]


@pytest.fixture
def routing_yaml(tmp_path):
    """Write a routing yaml into a tmp_path and point the loader at it.

    Returns a callable ``(yaml_text) -> Path``. Every test that writes a
    new file gets a fresh mtime cache automatically because the path is
    unique per test — but ``clear_routing_cache`` still runs to keep the
    env-var override tests isolated.
    """

    def _write(content: str) -> Path:
        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text(textwrap.dedent(content), encoding="utf-8")
        return cfg

    yield _write
    clear_routing_cache()


STANDARD_YAML = """
    version: 1
    default_tier: medium
    tiers:
      high:   ["^Vendor D", "^Vendor B Pro$", "^Vendor C"]
      medium: ["^Vendor A"]
      low:    ["^Vendor A"]
    scenes:
      prd: high
      execution: medium
"""


@pytest.fixture(autouse=True)
def _stub_db_names(monkeypatch):
    """Stub the display-name enumeration so tests never hit ~/.cc-switch."""
    monkeypatch.setattr(
        provider_routing, "_list_display_names", lambda db_path=None: list(DB_NAMES)
    )
    # Stub the optimizer chain so ranking is deterministic. Deliberately
    # NOT the DB row order — the Vendor A family sits here as
    # Vendor A → API → Vendor A, while ``DB_NAMES`` lists it as
    # Vendor A → Vendor A → API. A chain that tracks the optimizer is
    # therefore distinguishable from one that leaks DB row order.
    monkeypatch.setattr(
        provider_routing,
        "_optimizer_order",
        lambda: [
            "Vendor D API",
            "Vendor B Pro",
            "Vendor C App",
            "Vendor A Pro",
            "Vendor A API",
            "Vendor A",
        ],
    )


# ---------------------------------------------------------------------------
# resolve_scene_tier
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveSceneTier:
    def test_known_scene_maps_to_configured_tier(self, routing_yaml):
        cfg = routing_yaml(STANDARD_YAML)
        assert resolve_scene_tier("prd", config_path=cfg) == "high"

    def test_unknown_scene_falls_back_to_default_tier(self, routing_yaml):
        cfg = routing_yaml(STANDARD_YAML)
        assert resolve_scene_tier("no_such_scene", config_path=cfg) == "medium"

    def test_missing_config_falls_back_to_default_tier(self, tmp_path):
        assert resolve_scene_tier("prd", config_path=tmp_path / "absent.yaml") == "medium"


# ---------------------------------------------------------------------------
# resolve_provider_chain — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveProviderChain:
    def test_high_tier_leads_with_the_optimizers_first_choice(self, routing_yaml):
        """Vendor D API leads because the OPTIMIZER ranks it first — not
        because ``^Vendor D`` is written first in the tier."""
        cfg = routing_yaml(STANDARD_YAML)
        chain = resolve_provider_chain("prd", config_path=cfg)
        assert chain[0] == "Vendor D API"
        assert chain == ["Vendor D API", "Vendor B Pro", "Vendor C App"]

    def test_medium_tier_orders_matches_by_optimizer_order(self, routing_yaml):
        """``^Vendor A`` hits three rows; the order must follow the
        (stubbed) optimizer chain, not DB row order."""
        cfg = routing_yaml(STANDARD_YAML)
        chain = resolve_provider_chain("execution", config_path=cfg)
        assert chain == ["Vendor A Pro", "Vendor A API", "Vendor A"]

    def test_chain_follows_the_optimizer_across_patterns(
        self, routing_yaml, monkeypatch
    ):
        """The headline rule (2026-09-14): CC Switch's order wins over the
        yaml pattern order.

        The tier lists ``^Vendor D`` first and ``^Vendor C`` last, but the
        optimizer puts Vendor C first. The chain must follow the optimizer.
        """
        monkeypatch.setattr(
            provider_routing,
            "_optimizer_order",
            lambda: ["Vendor C App", "Vendor D API", "Vendor B Pro"],
        )
        cfg = routing_yaml(
            """
            version: 1
            default_tier: high
            tiers:
              high: ["^Vendor D", "^Vendor B", "^Vendor C"]
            scenes:
              prd: high
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == [
            "Vendor C App",
            "Vendor D API",
            "Vendor B Pro",
        ]

    def test_pattern_order_is_membership_only(self, routing_yaml):
        """Reordering the patterns inside a tier must not change the chain."""
        forward = routing_yaml(
            """
            version: 1
            default_tier: medium
            tiers:
              high: ["^Vendor D", "^Vendor B"]
            scenes:
              prd: high
            """
        )
        chain_forward = resolve_provider_chain("prd", config_path=forward)
        clear_routing_cache()

        reversed_cfg = routing_yaml(
            """
            version: 1
            default_tier: medium
            tiers:
              high: ["^Vendor B", "^Vendor D"]
            scenes:
              prd: high
            """
        )
        chain_reversed = resolve_provider_chain("prd", config_path=reversed_cfg)

        assert chain_forward == chain_reversed == ["Vendor D API", "Vendor B Pro"]

    def test_unranked_names_keep_db_order_behind_ranked_ones(
        self, routing_yaml, monkeypatch
    ):
        """A provider the optimizer does not rank at all still appears —
        behind every ranked one, in DB row order, so a partial
        ``provider-order.json`` degrades instead of dropping providers."""
        monkeypatch.setattr(
            provider_routing,
            "_optimizer_order",
            lambda: ["Vendor C App"],  # only one of the three ranked
        )
        cfg = routing_yaml(
            """
            version: 1
            default_tier: high
            tiers:
              high: ["^Vendor D", "^Vendor B", "^Vendor C"]
            scenes:
              prd: high
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == [
            "Vendor C App",
            "Vendor D API",  # unranked → DB row order
            "Vendor B Pro",
        ]

    def test_anchor_prevents_substring_match(self, routing_yaml):
        cfg = routing_yaml(
            """
            version: 1
            default_tier: medium
            tiers:
              high: ["^Vendor B Pro$"]
            scenes:
              prd: high
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == ["Vendor B Pro"]

    def test_name_not_in_db_is_dropped(self, routing_yaml):
        """A pattern that matches nothing live yields no candidates."""
        cfg = routing_yaml(
            """
            version: 1
            default_tier: high
            tiers:
              high: ["^Nonexistent"]
            scenes:
              prd: high
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == []


# ---------------------------------------------------------------------------
# resolve_provider_chain — degradation / error semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveProviderChainDegradation:
    def test_missing_config_file_returns_empty_chain(self, tmp_path, caplog):
        with caplog.at_level("WARNING", logger="provider_routing"):
            chain = resolve_provider_chain("prd", config_path=tmp_path / "absent.yaml")
        assert chain == []
        assert any("provider_routing.yaml" in r.message for r in caplog.records)

    def test_invalid_regex_is_skipped_not_fatal(self, routing_yaml, caplog):
        cfg = routing_yaml(
            """
            version: 1
            default_tier: high
            tiers:
              high: ["([bad", "^Vendor D"]
            scenes:
              prd: high
            """
        )
        with caplog.at_level("WARNING", logger="provider_routing"):
            chain = resolve_provider_chain("prd", config_path=cfg)
        assert chain == ["Vendor D API"]
        assert any("invalid" in r.message.lower() for r in caplog.records)

    def test_unknown_tier_returns_empty_chain(self, routing_yaml):
        cfg = routing_yaml(
            """
            version: 1
            default_tier: medium
            tiers:
              medium: ["^Vendor A"]
            scenes:
              prd: high   # tier 'high' not declared
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == []

    def test_empty_tier_list_returns_empty_chain(self, routing_yaml):
        cfg = routing_yaml(
            """
            version: 1
            default_tier: medium
            tiers:
              high: []
              medium: ["^Vendor A"]
            scenes:
              prd: high
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == []

    def test_invalid_yaml_returns_empty_chain(self, tmp_path):
        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text("{not: valid: yaml: [", encoding="utf-8")
        assert resolve_provider_chain("prd", config_path=cfg) == []

    def test_non_dict_yaml_returns_empty_chain(self, tmp_path):
        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert resolve_provider_chain("prd", config_path=cfg) == []

    def test_scene_mapped_to_non_string_tier_returns_empty(self, routing_yaml):
        cfg = routing_yaml(
            """
            version: 1
            default_tier: medium
            tiers:
              medium: ["^Vendor A"]
            scenes:
              prd: 42
            """
        )
        assert resolve_provider_chain("prd", config_path=cfg) == []


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHotReload:
    def test_mtime_change_reloads_config(self, routing_yaml):
        cfg = routing_yaml(STANDARD_YAML)
        assert resolve_provider_chain("prd", config_path=cfg) == ["Vendor D API", "Vendor B Pro", "Vendor C App"]

        # Rewrite with different tier content.
        cfg.write_text(
            textwrap.dedent(
                """
                version: 1
                default_tier: medium
                tiers:
                  high: ["^Vendor A"]
                scenes:
                  prd: high
                """
            ),
            encoding="utf-8",
        )
        # Force a distinct mtime (filesystems have 1s granularity).
        st = cfg.stat()
        os.utime(cfg, (st.st_atime, st.st_mtime + 2))
        clear_routing_cache()  # belt-and-braces: also proves cache_clear works

        chain = resolve_provider_chain("prd", config_path=cfg)
        assert chain and chain[0].startswith("Vendor A")

    def test_env_var_override_changes_path(self, routing_yaml, monkeypatch, tmp_path):
        cfg_a = routing_yaml(STANDARD_YAML)
        cfg_b = tmp_path / "other.yaml"
        cfg_b.write_text(
            textwrap.dedent(
                """
                version: 1
                default_tier: medium
                tiers:
                  medium: ["^Vendor B"]
                scenes:
                  prd: medium
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("PDT_PROVIDER_ROUTING_FILE", str(cfg_b))
        # Explicit config_path still wins over env (precedence contract).
        assert resolve_provider_chain("prd", config_path=cfg_a) == [
            "Vendor D API", "Vendor B Pro", "Vendor C App",
        ]
        # No explicit path → env override takes effect.
        assert resolve_provider_chain("prd") == ["Vendor B Pro"]

    def test_cache_returns_same_object_until_cleared(self, routing_yaml):
        cfg = routing_yaml(STANDARD_YAML)
        a = resolve_provider_chain("prd", config_path=cfg)
        b = resolve_provider_chain("prd", config_path=cfg)
        assert a == b
        clear_routing_cache()
        c = resolve_provider_chain("prd", config_path=cfg)
        assert c == a


# ---------------------------------------------------------------------------
# Public error surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_routing_error_exists():
    """The module exposes a dedicated error type for hard misuse
    (e.g. a caller passing a non-string scene) without depending on
    bare ValueError."""
    assert issubclass(ProviderRoutingError, ValueError)
    with pytest.raises(ProviderRoutingError):
        provider_routing._require_scene(42)


# ---------------------------------------------------------------------------
# resolve_fallback_chains — cross-tier widening (2026-09-22)
# ---------------------------------------------------------------------------
#
# The shipped config declares ``medium: ["^Vendor A"]``, which matches
# exactly two CC Switch rows. On 2026-09-22 both answered 429 and the
# dispatch had nowhere left to go inside the tier; it fell to the parent
# process env (not a tier member) and the task died there. The user's
# rule: the chain only terminates once every provider has been tried.
#
# ``resolve_provider_chain`` itself is unchanged — it still answers "the
# scene's tier". The widening lives in ``resolve_fallback_chains`` so a
# caller that wants more can ask for it.


class TestResolveFallbackChains:
    def test_the_scene_tier_comes_first(self, routing_yaml):
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml(STANDARD_YAML)
        chains = resolve_fallback_chains("execution", config_path=cfg)

        assert chains[0] == ["Vendor A Pro", "Vendor A API", "Vendor A"]
        assert chains[0] == resolve_provider_chain("execution", config_path=cfg)

    def test_the_next_tier_follows(self, routing_yaml):
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml(STANDARD_YAML)
        chains = resolve_fallback_chains("execution", config_path=cfg)

        assert ["Vendor D API", "Vendor B Pro", "Vendor C App"] in chains

    def test_tiers_with_identical_chains_are_not_walked_twice(self, routing_yaml):
        """``medium`` and ``low`` both read ``["^Vendor A"]`` in the
        shipped config, so their chains are identical. Walking the same
        providers a second time buys nothing."""
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml(STANDARD_YAML)
        chains = resolve_fallback_chains("execution", config_path=cfg)

        assert len(chains) == len({tuple(c) for c in chains})
        assert len(chains) == 2

    def test_a_high_scene_widens_into_medium(self, routing_yaml):
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml(STANDARD_YAML)
        chains = resolve_fallback_chains("prd", config_path=cfg)

        assert chains[0] == ["Vendor D API", "Vendor B Pro", "Vendor C App"]
        assert ["Vendor A Pro", "Vendor A API", "Vendor A"] in chains

    def test_an_unknown_scene_uses_the_default_tier(self, routing_yaml):
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml(STANDARD_YAML)
        chains = resolve_fallback_chains("no-such-scene", config_path=cfg)

        assert chains[0] == ["Vendor A Pro", "Vendor A API", "Vendor A"]

    def test_a_missing_config_yields_no_chains(self, tmp_path, monkeypatch):
        from provider_routing import resolve_fallback_chains

        monkeypatch.setattr(
            provider_routing, "_config_path",
            lambda config_path=None: tmp_path / "absent.yaml",
        )
        clear_routing_cache()
        assert resolve_fallback_chains("execution") == []

    def test_a_tier_matching_no_live_provider_is_omitted(self, routing_yaml):
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml("""
            version: 1
            default_tier: medium
            tiers:
              medium: ["^Vendor A"]
              high:   ["^Nobody"]
            scenes:
              execution: medium
        """)
        chains = resolve_fallback_chains("execution", config_path=cfg)

        assert chains == [["Vendor A Pro", "Vendor A API", "Vendor A"]]

    def test_a_non_string_scene_still_raises(self, routing_yaml):
        from provider_routing import resolve_fallback_chains

        cfg = routing_yaml(STANDARD_YAML)
        with pytest.raises(ProviderRoutingError):
            resolve_fallback_chains(42, config_path=cfg)
