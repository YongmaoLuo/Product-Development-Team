"""Unit tests for the base-drift merge scene's routing contract.

Spec (2026-10-31, task 2):

  * ``base_drift.BASE_DRIFT_MERGE_SCENE`` names the very key the
    routing template declares — the constant and the config key are
    one contract, not two that happen to look alike.
  * The public template ``example/provider_routing.yaml.example``
    maps that scene to the ``high`` tier.
  * The template gate's own ``EXPECTED_SCENES`` table carries the same
    entry, so a future re-tiering has to change both places at once.
  * ``provider_routing.resolve_scene_tier`` reads ``high`` back out of
    the template — the template is not decoration, it is what the
    resolver actually consults.
  * The source of ``base_drift`` declares NO tier name at all.

Why the last one matters
------------------------
The merge tier is a **configuration** decision: ``base_drift`` ships
the scene NAME only, and the operator's ``provider_routing.yaml``
decides which tier that name resolves to.  A tier literal inside
``base_drift.py`` would silently override the operator's configuration
and make the published template a lie — the file would advertise one
tier while the code forced another.

The tier names are matched with WORD BOUNDARIES on purpose.  A bare
substring search would fire on ``below`` and ``.lower()``, which are
ordinary code, not tier declarations.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import base_drift  # noqa: E402
import provider_routing  # noqa: E402

# Locate the repository root. This file lives at
# ``backend/tests/unit/test_base_drift_merge_scene_config.py``, so
# parents[3] is the repo root (unit/ -> tests/ -> backend/ -> root).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE = _REPO_ROOT / "example" / "provider_routing.yaml.example"
_GATE_MODULE = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "unit"
    / "test_provider_routing_config_template.py"
)

#: The tier the base-drift merge scene is published at.
EXPECTED_TIER = "high"

#: The scene key the merge runs under.
SCENE_KEY = "base_drift_merge"

#: The tier names the routing vocabulary uses.
TIER_NAMES = ("max", "ultra", "high", "medium", "low")


def _template_scenes() -> dict:
    """The ``scenes`` map of the public routing template."""
    data = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        f"{_TEMPLATE} did not parse to a mapping; got {type(data).__name__}"
    )
    return data.get("scenes") or {}


def _gate_scene_table() -> dict:
    """Load ``EXPECTED_SCENES`` from the routing-template gate module.

    The gate module is loaded BY PATH rather than by package name: the
    repository root also carries a ``tests/`` directory, so a plain
    ``import tests.unit...`` resolves differently depending on whether
    the repo-root ``pytest.ini`` (which puts the root first on
    ``sys.path``) or the backend-local one (which puts ``backend/``
    first) governs the run.  Loading by path answers the same either
    way.
    """
    spec = importlib.util.spec_from_file_location(
        "_provider_routing_template_gate", _GATE_MODULE
    )
    assert spec is not None and spec.loader is not None, (
        f"could not load the routing-template gate module at {_GATE_MODULE}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EXPECTED_SCENES


# ---------------------------------------------------------------------------
# The scene name
# ---------------------------------------------------------------------------


def test_scene_constant_matches_the_config_key() -> None:
    """The source constant names the key the config declares.

    ``base_drift`` publishes a scene NAME; the routing config declares
    a scene KEY.  If the two drift apart the merge silently routes to
    ``default_tier`` — a wrong tier that nobody notices, because a
    missing scene is a legal (and quiet) configuration state.
    """
    assert base_drift.BASE_DRIFT_MERGE_SCENE == SCENE_KEY, (
        "base_drift.BASE_DRIFT_MERGE_SCENE must be the scene key the "
        f"routing config declares; got {base_drift.BASE_DRIFT_MERGE_SCENE!r}"
    )
    assert SCENE_KEY in _template_scenes(), (
        f"the routing template does not declare the scene {SCENE_KEY!r}; "
        f"declared scenes: {sorted(_template_scenes())}"
    )


# ---------------------------------------------------------------------------
# The tier the template publishes
# ---------------------------------------------------------------------------


def test_template_maps_the_scene_to_high() -> None:
    """The template maps the merge scene to the ``high`` tier."""
    scenes = _template_scenes()
    assert scenes.get(SCENE_KEY) == EXPECTED_TIER, (
        f"the routing template must map {SCENE_KEY!r} to {EXPECTED_TIER!r}; "
        f"got {scenes.get(SCENE_KEY)!r}"
    )


def test_template_gate_table_carries_the_scene() -> None:
    """The template gate's own table carries the scene at the same tier.

    The gate pins the template's ``scenes`` map BY EQUALITY.  An entry
    the gate does not know about fails that equality — so the gate
    table and the template can never drift apart silently.
    """
    expected = _gate_scene_table()
    assert expected.get(SCENE_KEY) == EXPECTED_TIER, (
        "the routing-template gate table must carry "
        f"{SCENE_KEY!r} -> {EXPECTED_TIER!r}; got {expected.get(SCENE_KEY)!r}"
    )


# ---------------------------------------------------------------------------
# The resolver reads it back
# ---------------------------------------------------------------------------


def test_resolver_reads_high_from_the_template() -> None:
    """``resolve_scene_tier`` returns ``high`` for the merge scene.

    The published template is what an operator copies, so the resolver
    has to agree with it: a template that says ``high`` while the
    resolver says something else would make the documented contract
    untestable.
    """
    tier = provider_routing.resolve_scene_tier(SCENE_KEY, config_path=_TEMPLATE)
    assert tier == EXPECTED_TIER, (
        f"resolve_scene_tier({SCENE_KEY!r}) must read {EXPECTED_TIER!r} out "
        f"of the published template; got {tier!r}"
    )


def test_resolver_falls_back_to_the_default_tier_for_an_unknown_scene() -> None:
    """A scene the template does not declare falls back to the default.

    A missing scene is not an error: the resolver falls back to
    ``default_tier`` (medium) so a config that predates the merge
    scene still works.  This is the property that lets the merge scene
    be ADDED to a deployed config without a coordinated restart.
    """
    tier = provider_routing.resolve_scene_tier(
        "base_drift_merge_scene_that_does_not_exist", config_path=_TEMPLATE
    )
    assert tier == "medium", (
        "an undeclared scene must fall back to the config's default_tier "
        f"(medium); got {tier!r}"
    )


# ---------------------------------------------------------------------------
# The source declares no tier
# ---------------------------------------------------------------------------


def test_source_declares_no_tier_for_the_scene() -> None:
    """``base_drift`` names no tier — the config owns that decision.

    Matched with WORD BOUNDARIES: a bare substring search would fire on
    ``below`` and ``.lower()``, which are ordinary code, not tier
    declarations.
    """
    source = inspect.getsource(base_drift)
    hits = re.findall(r"\b(?:max|ultra|high|medium|low)\b", source)
    assert not hits, (
        "base_drift must not name a routing tier; the tier is a "
        f"configuration decision. Offending occurrences: {hits!r}"
    )
