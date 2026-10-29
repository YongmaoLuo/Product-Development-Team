"""Unit tests for the per-provider overrides ``provider_routing`` resolves.

Spec (2026-10-28, task 2):

  * ``resolve_provider_overrides(scene, provider_name)`` answers a PURE
    configuration question: for the tier this scene resolves to, which
    tier entries match this provider name, and what ``model`` / ``effort``
    do they pin? It never touches CC Switch and never writes anything —
    an absent override is an empty mapping, so the caller writes not one
    byte.
  * The tier's entry list is walked in DOCUMENT order. The first entry
    that supplies a field wins that field; a later matching entry may
    only fill a field that is still missing.
  * A legacy bare-string entry declares MEMBERSHIP only — matching it
    never produces an override.
  * An invalid ``effort`` (anything outside ``EFFORT_LEVELS``, matched
    case-sensitively after stripping) is dropped with a WARNING while the
    entry's ``model`` still applies. An invalid ``model`` (not a string /
    blank after stripping) is dropped the same way while the entry's
    ``effort`` still applies.
  * A scene that is not listed in ``scenes`` falls back to
    ``default_tier``; a missing tier, a non-mapping ``tiers`` section or
    an unreadable config all yield ``{}`` — never an exception.
  * The config is read through the module's ``(mtime_ns, size)``
    fingerprint, so an edited yaml is picked up by the very next call —
    no cache clear needed.

All tests use a tempdir yaml, so no real operator config is touched.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import provider_routing
from provider_routing import (
    EFFORT_LEVELS,
    clear_routing_cache,
    resolve_provider_overrides,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def routing_yaml(tmp_path):
    """Write a routing yaml into a tmp_path and return its path.

    ``clear_routing_cache`` runs on teardown so the module-level cache
    never leaks between tests.
    """

    def _write(content: str) -> Path:
        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text(textwrap.dedent(content), encoding="utf-8")
        return cfg

    yield _write
    clear_routing_cache()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_and_effort_returned_for_matching_provider(routing_yaml):
    """A matching entry's ``model`` and ``effort`` both come back."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          high:
            - pattern: '^Vendor A'
              model: 'm-1'
              effort: 'max'
          medium:
            - '^Vendor A'
        scenes: {prd: high, execution: medium}
        """
    )

    assert resolve_provider_overrides("prd", "Vendor A Pro", config_path=cfg) == {
        "model": "m-1",
        "effort": "max",
    }


@pytest.mark.unit
def test_non_matching_provider_returns_empty(routing_yaml):
    """A provider no entry of the tier matches produces no override."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          high:
            - pattern: '^Vendor A'
              model: 'm-1'
              effort: 'max'
        scenes: {prd: high}
        """
    )

    assert resolve_provider_overrides("prd", "Vendor D API", config_path=cfg) == {}


@pytest.mark.unit
def test_plain_string_entry_returns_empty(routing_yaml):
    """A legacy bare-string entry matches but never carries an override."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          medium:
            - '^Vendor A'
        scenes: {execution: medium}
        """
    )

    assert resolve_provider_overrides("execution", "Vendor A Pro", config_path=cfg) == {}


# ---------------------------------------------------------------------------
# Invalid values degrade to a WARNING, the other field survives
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_effort_is_dropped_with_warning_and_model_kept(routing_yaml, caplog):
    """``effort: MAX`` is not a legal level: warned about, ``model`` kept."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          high:
            - pattern: '^Vendor A'
              model: 'm-1'
              effort: 'MAX'
        scenes: {prd: high}
        """
    )

    with caplog.at_level("WARNING", logger="provider_routing"):
        overrides = resolve_provider_overrides("prd", "Vendor A Pro", config_path=cfg)

    assert overrides == {"model": "m-1"}
    assert any(
        record.levelname == "WARNING"
        and "effort" in record.message
        and "invalid" in record.message.lower()
        for record in caplog.records
    )


@pytest.mark.unit
def test_invalid_model_is_dropped_with_warning_and_effort_kept(routing_yaml, caplog):
    """``model: 42`` is not a model name: warned about, ``effort`` kept."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          high:
            - pattern: '^Vendor A'
              model: 42
              effort: 'max'
        scenes: {prd: high}
        """
    )

    with caplog.at_level("WARNING", logger="provider_routing"):
        overrides = resolve_provider_overrides("prd", "Vendor A Pro", config_path=cfg)

    assert overrides == {"effort": "max"}
    assert any(
        record.levelname == "WARNING"
        and "model" in record.message
        and "invalid" in record.message.lower()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Document order: first entry wins, later entries fill the gaps
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_matching_entry_wins_and_later_entry_fills_missing_field(routing_yaml):
    """Two matching entries merge: the first supplies model, the second effort."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          high:
            - pattern: '^Vendor A Pro$'
              model: 'm-first'
            - pattern: '^Vendor A'
              effort: 'high'
        scenes: {prd: high}
        """
    )

    assert resolve_provider_overrides("prd", "Vendor A Pro", config_path=cfg) == {
        "model": "m-first",
        "effort": "high",
    }


# ---------------------------------------------------------------------------
# Scene → tier resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scene_without_explicit_mapping_uses_default_tier_overrides(routing_yaml):
    """A scene missing from ``scenes`` uses the ``default_tier`` overrides."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          high:
            - pattern: '^Vendor A'
              model: 'm-high'
          medium:
            - pattern: '^Vendor A'
              model: 'm-medium'
              effort: 'low'
        scenes: {prd: high}
        """
    )

    assert resolve_provider_overrides("execution", "Vendor A Pro", config_path=cfg) == {
        "model": "m-medium",
        "effort": "low",
    }


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hot_reload_picks_up_changed_overrides(routing_yaml):
    """An edited yaml is honoured by the next call — no cache clear."""
    cfg = routing_yaml(
        """
        version: 1
        default_tier: medium
        tiers:
          medium:
            - pattern: '^Vendor A'
              model: 'm-before-edit'
              effort: 'low'
        scenes: {execution: medium}
        """
    )
    assert resolve_provider_overrides("execution", "Vendor A Pro", config_path=cfg) == {
        "model": "m-before-edit",
        "effort": "low",
    }

    # Rewrite with DIFFERENT content AND a different file size, so the
    # (mtime_ns, size) fingerprint changes even within one second.
    cfg.write_text(
        textwrap.dedent(
            """
            version: 1
            default_tier: medium
            tiers:
              medium:
                - pattern: '^Vendor A'
                  model: 'm-after-edit'
                  effort: 'max'
            scenes: {execution: medium}
            """
        ),
        encoding="utf-8",
    )

    assert resolve_provider_overrides("execution", "Vendor A Pro", config_path=cfg) == {
        "model": "m-after-edit",
        "effort": "max",
    }


# ---------------------------------------------------------------------------
# The effort vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_effort_levels_contains_the_five_supported_values():
    """The legal effort levels are exactly the five documented ones."""
    assert EFFORT_LEVELS == {"low", "medium", "high", "xhigh", "max"}
    assert EFFORT_LEVELS == provider_routing.EFFORT_LEVELS
