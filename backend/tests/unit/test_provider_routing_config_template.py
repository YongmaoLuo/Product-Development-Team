"""Unit tests for the public routing template and the module docstring.

Spec (2026-10-28, task 3):

  * ``example/provider_routing.yaml.example`` declares exactly the five
    tiers ``max`` / ``ultra`` / ``high`` / ``medium`` / ``low``.
  * Its ``scenes`` map matches the decided 20-entry table EXACTLY, and
    the retired ``breakdown`` scene is gone — from ``scenes`` and from
    the file as a whole, so it cannot creep back in through a comment.
  * At least one tier entry demonstrates the mapping spelling
    ``{pattern, model, effort}`` that carries the per-provider pins.
  * EVERY tier entry is either a non-empty string, or a mapping whose
    ``pattern`` is a non-empty string. Anything else is not a legal
    tier entry and the template must not teach that shape.
  * The template header no longer claims model selection is delegated
    entirely to CC Switch, and ``provider_routing``'s module docstring
    describes the model / effort override semantics instead.
  * The template header AND the module docstring both name the TWO
    landing points of a pinned effort: the subprocess env variable
    ``CLAUDE_CODE_EFFORT_LEVEL`` and the top-level ``effortLevel`` key
    of the per-call settings payload (the file the CLI gets through
    ``--settings``).
  * The template says the two tier-entry spellings (bare regex string /
    ``{pattern, model, effort}`` mapping) may be MIXED inside one tier,
    instead of reading as a per-tier either/or choice.

Why this gate exists
--------------------
The template ships into the public repository, so it IS the format
contract: the next reader copies it, and the next loader implements it.
A template that still says "model selection is delegated entirely to
CC Switch" teaches the reader to delete the model/effort pins the
loader now honours — the documentation would undo the feature.

The scene table is pinned by equality rather than by containment: a
scene that silently keeps its old tier (or a scene that quietly
disappears) is exactly the drift this table exists to catch.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import provider_routing

# Locate the repository root. This file lives at
# ``backend/tests/unit/test_provider_routing_config_template.py``, so
# parents[3] is the repo root (unit/ -> tests/ -> backend/ -> root).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE = _REPO_ROOT / "example" / "provider_routing.yaml.example"

#: The five tiers the routing config declares, strongest first.
EXPECTED_TIERS = {"max", "ultra", "high", "medium", "low"}

#: The decided scene → tier table. Pinned by equality.
EXPECTED_SCENES = {
    # --- Document / planning phases: max tier ---
    "interview": "max",
    "prd": "max",
    "prd_refine": "max",
    "arch": "max",
    "arch_refine": "max",
    "test_design": "max",
    "test_refine": "max",
    "tasks_generation": "max",
    "planning": "max",
    # --- Verification / refinement: ultra tier ---
    "verification": "ultra",
    "verification_command_audit": "ultra",
    "verification_plan_delta": "ultra",
    "refiner": "ultra",
    # --- Second-pass audit: high tier ---
    "repair_generation": "ultra",
    "audit_second_pass": "high",
    # --- Base-drift merge: high tier ---
    "base_drift_merge": "high",
    # --- Execution: medium tier (cost control) ---
    "execution": "medium",
    "repair_execution": "medium",
    "service_restart": "medium",
    # --- Low-stakes auxiliary calls ---
    "summarizer": "low",
}


def _load_template() -> dict:
    """Parse the public routing template into a dict.

    The template MUST parse: a template that does not parse is a
    template nobody can copy, and the loader would reject it at
    startup.
    """
    data = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        f"{_TEMPLATE} did not parse to a mapping; got {type(data).__name__}"
    )
    return data


def _normalized(text: str) -> str:
    """Collapse every whitespace run into single spaces.

    The prose gates below search for a PHRASE. A phrase that a line
    wrap splits across two lines must still be found — otherwise the
    gate is defeated by re-wrapping the sentence, which is exactly the
    kind of cosmetic edit that should not be able to hide a stale
    claim.
    """
    return " ".join(text.split())


def _template_header() -> str:
    """The template's leading comment block, whitespace-normalized.

    The header is everything from the top of the file up to the first
    non-comment, non-blank line (``version: 1``). Comments further
    down belong to the sections they introduce, not to the header.
    """
    lines: list[str] = []
    for line in _TEMPLATE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        # Strip the comment marker itself: a "#" left between two words
        # would split a phrase across the join and hide it.
        lines.append(line.lstrip().lstrip("#"))
    return _normalized(" ".join(lines))


def _tier_entries(template: dict) -> list:
    """Every tier entry of the template, flattened across the tiers."""
    entries: list = []
    for tier_patterns in (template.get("tiers") or {}).values():
        if not tier_patterns:
            continue
        for entry in tier_patterns:
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# The template's tier table
# ---------------------------------------------------------------------------


def test_example_declares_exactly_the_five_tiers() -> None:
    """The template declares exactly max / ultra / high / medium / low.

    The tier NAMES are the routing vocabulary — a scene points at one
    by name, so an extra tier is dead weight and a missing one sends
    its scenes to an empty chain at dispatch time.
    """
    template = _load_template()
    tiers = template.get("tiers") or {}
    assert set(tiers) == EXPECTED_TIERS, (
        "The routing template must declare exactly the five tiers "
        f"{sorted(EXPECTED_TIERS)}; got {sorted(tiers)}"
    )


def test_example_default_tier_stays_medium() -> None:
    """``default_tier`` stays ``medium``.

    A scene that is not listed routes to the default tier; ``low`` is
    only ever used when a scene names it explicitly, so the default
    must not drift.
    """
    template = _load_template()
    assert template.get("default_tier") == "medium", (
        "default_tier must stay 'medium'; got "
        f"{template.get('default_tier')!r}"
    )


# ---------------------------------------------------------------------------
# The template's scene table
# ---------------------------------------------------------------------------


def test_example_scene_map_matches_the_decided_table() -> None:
    """``scenes`` equals the decided 20-entry table, exactly.

    Equality, not containment: a leftover scene or a scene still on
    its old tier is precisely the drift this table pins down.
    """
    template = _load_template()
    scenes = template.get("scenes") or {}
    assert scenes == EXPECTED_SCENES, (
        "The routing template's scene table does not match the decided "
        "table.\n"
        f"  missing:   {sorted(set(EXPECTED_SCENES) - set(scenes))}\n"
        f"  unexpected: {sorted(set(scenes) - set(EXPECTED_SCENES))}\n"
        f"  wrong tier: "
        f"{ {k: (v, scenes[k]) for k, v in EXPECTED_SCENES.items() if k in scenes and scenes[k] != v} }"
    )


def test_example_has_no_breakdown_scene() -> None:
    """The retired ``breakdown`` scene is gone from the template.

    Also checked against the raw file text: the scene must not creep
    back in as a comment or a leftover key.
    """
    template = _load_template()
    scenes = template.get("scenes") or {}
    assert "breakdown" not in scenes, (
        "The 'breakdown' scene was retired; it must not be declared in "
        f"the routing template's scenes (got {scenes.get('breakdown')!r})"
    )

    raw = _TEMPLATE.read_text(encoding="utf-8")
    assert "breakdown" not in raw, (
        "The word 'breakdown' still appears in the routing template; the "
        "retired scene must not be reintroduced anywhere in the file"
    )


# ---------------------------------------------------------------------------
# The template's tier-entry shapes
# ---------------------------------------------------------------------------


def test_example_demonstrates_mapping_entry_syntax() -> None:
    """At least one tier entry uses the ``{pattern, model, effort}`` form.

    The mapping spelling is how a tier pins a model / effort for the
    providers its pattern matches. A template that only shows bare
    strings teaches nobody the form that the loader now honours.
    """
    template = _load_template()
    mapping_entries = [
        entry
        for entry in _tier_entries(template)
        if isinstance(entry, dict) and entry.get("model") and entry.get("effort")
    ]
    assert mapping_entries, (
        "The routing template must demonstrate at least one mapping tier "
        "entry carrying both 'model' and 'effort'; the tier entries are "
        f"{_tier_entries(template)!r}"
    )


def test_example_tier_entries_are_strings_or_mappings_with_string_pattern() -> None:
    """Every tier entry is a non-empty string, or a mapping with a pattern.

    These are the two legal entry shapes the loader accepts. A third
    shape in the template would be skipped by the loader with a
    WARNING — the template would ship a broken example.
    """
    template = _load_template()
    offenders: list = []
    for entry in _tier_entries(template):
        if isinstance(entry, str):
            if not entry.strip():
                offenders.append(entry)
            continue
        if isinstance(entry, dict):
            pattern = entry.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                offenders.append(entry)
            continue
        offenders.append(entry)

    assert not offenders, (
        "Every routing template tier entry must be a non-empty string, "
        "or a mapping whose 'pattern' is a non-empty string. Offending "
        f"entries: {offenders!r}"
    )


# ---------------------------------------------------------------------------
# The prose that describes the semantics
# ---------------------------------------------------------------------------


def test_example_header_no_longer_delegates_model_selection() -> None:
    """The template header no longer hands model selection to CC Switch.

    A tier entry may now pin a model and a reasoning effort; fields
    the entry does not carry still fall through to the CC Switch row.
    The old sentence contradicts that and would make the next reader
    delete the pins.
    """
    raw = _TEMPLATE.read_text(encoding="utf-8")
    header = _template_header()
    assert "delegated entirely to CC Switch" not in header, (
        "The routing template header still claims model selection is "
        "'delegated entirely to CC Switch'; it must describe the "
        "model/effort pin semantics instead"
    )
    assert "delegated entirely to CC Switch" not in _normalized(raw), (
        "The routing template still claims model selection is 'delegated "
        "entirely to CC Switch' somewhere in the file; it must describe "
        "the model/effort pin semantics instead"
    )


def test_module_docstring_describes_model_and_effort_overrides() -> None:
    """``provider_routing``'s docstring states the override semantics.

    The docstring is where the next implementer looks before changing
    the routing layer. It must name the ``effort`` override and must
    not claim the module has no model knowledge.
    """
    docstring = provider_routing.__doc__ or ""
    normalized = _normalized(docstring)
    assert "no model knowledge" not in normalized, (
        "provider_routing's module docstring still claims the module has "
        "'no model knowledge'; it must describe the model/effort override "
        "semantics instead"
    )
    assert "delegated entirely to CC Switch" not in normalized, (
        "provider_routing's module docstring still claims model "
        "management is 'delegated entirely to CC Switch'; the module now "
        "reads per-tier model/effort pins"
    )
    assert "effort" in normalized, (
        "provider_routing's module docstring does not mention 'effort'; "
        "the reasoning-effort override must be documented there"
    )


# ---------------------------------------------------------------------------
# Where a pinned effort lands — TWO places, never one
# ---------------------------------------------------------------------------


def test_example_header_names_both_effort_landing_points() -> None:
    """The template header names BOTH landing points of a pinned effort.

    The dispatch layer writes a pinned effort to the sub-agent subprocess
    env (``CLAUDE_CODE_EFFORT_LEVEL``) AND to the top-level
    ``effortLevel`` key of the per-call settings payload the CLI reads
    through ``--settings``. A template that names only one teaches the
    next reader to "simplify" the other one away — and the channel that
    survives is the one the CLI overwrites with the machine's own value.
    """
    header = _template_header()
    assert "CLAUDE_CODE_EFFORT_LEVEL" in header, (
        "The routing template header must name the subprocess env "
        "variable 'CLAUDE_CODE_EFFORT_LEVEL' as one of the two landing "
        "points of a pinned effort"
    )
    assert "effortLevel" in header, (
        "The routing template header must name the top-level "
        "'effortLevel' key of the per-call settings payload as the other "
        "landing point of a pinned effort"
    )
    assert "per-call settings" in header or "--settings" in header, (
        "The routing template header must say that the settings payload "
        "is the per-call settings file the CLI is handed through "
        "'--settings'"
    )


def test_example_header_allows_mixing_entry_spellings_in_one_tier() -> None:
    """The template says the two entry spellings may share one tier.

    A bare regex string and a ``{pattern, model, effort}`` mapping mean
    the same thing, and one tier may carry both. A template that reads
    as "this tier picks a spelling" makes the next reader normalise the
    config into one shape per tier — and the tier they "converted" to
    bare strings silently loses its model / effort pins.
    """
    normalized = _normalized(_TEMPLATE.read_text(encoding="utf-8"))
    assert re.search(r"mix(?:ed|es|ing)?\b|both spellings", normalized, re.I), (
        "The routing template must say that the bare-string and the "
        "mapping entry spellings may be mixed inside one tier and mean "
        "the same thing"
    )
    assert "the other four show the mapping form" not in normalized, (
        "The routing template must not present the two entry spellings as "
        "a per-tier either/or choice ('the other four show the mapping "
        "form'); they may be mixed inside one tier"
    )
    assert "shows the bare-string form" not in normalized, (
        "The routing template must not present the two entry spellings as "
        "a per-tier either/or choice ('the `max` tier shows the "
        "bare-string form ...; the other four show the mapping form')"
    )


def test_module_docstring_names_both_effort_landing_points() -> None:
    """The module docstring names BOTH landing points of a pinned effort.

    The docstring is where the next implementer looks before touching
    the routing layer, so it has to say where a pinned effort actually
    goes: the subprocess env AND the per-call settings payload. Naming
    one of them is how the other one gets "cleaned up" away.
    """
    normalized = _normalized(provider_routing.__doc__ or "")
    assert "CLAUDE_CODE_EFFORT_LEVEL" in normalized, (
        "provider_routing's module docstring must name the subprocess env "
        "variable 'CLAUDE_CODE_EFFORT_LEVEL' as one of the two landing "
        "points of a pinned effort"
    )
    assert "effortLevel" in normalized, (
        "provider_routing's module docstring must name the top-level "
        "'effortLevel' key of the per-call settings payload as the other "
        "landing point of a pinned effort"
    )
    assert "per-call settings" in normalized or "--settings" in normalized, (
        "provider_routing's module docstring must say that the settings "
        "payload is the per-call settings file the CLI is handed through "
        "'--settings'"
    )
