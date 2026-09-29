"""Pin that the example configuration templates are deployment-neutral.

Why this gate exists
--------------------
``example/provider_routing.yaml.example`` and
``example/provider_capacity.yaml.example`` ship into the public
repository; the live ``.config/*.yaml`` files do not (``.config/`` is
gitignored). The two surfaces are easy to mix up — a copy-paste from
the operator's live file into the template would publish one
deployment's provider set as if it were everyone's, and the leaked
content would not look like a credential to any scanner.

The gate pins the rule at three layers:

1. **Templates carry placeholders only.** Every ``tiers.*`` entry in
   the routing example and every ``providers.*.pattern`` entry in the
   capacity example is a ``^Example `` prefixed name. A concrete
   deployment name (e.g. ``^Vendor A``, ``^Vendor B``) inside the
   template would compile one install's providers into every checkout.

2. **Templates cannot accidentally match a live deployment.** When the
   operator's ``.config/provider_routing.yaml`` is present, the test
   walks its ``tiers`` lists and confirms no template pattern (a
   Python regex) matches any live pattern's source text. The check
   uses the live list — read at runtime, not hardcoded — so adding a
   new provider does not require updating this test.

3. **``.config/`` is gitignored.** The templates only stay neutral if
   the live files do not also leak into source control. The check is
   pinned against ``git check-ignore`` so a future ``.gitignore``
   edit that drops the rule trips the test immediately.

The gate has no hardcoded provider name. Pinning a literal name here
(e.g. ``assert "^Vendor A" not in live``) would itself be the leak the
gate exists to prevent — and would silently pass on a different
operator's setup. The placeholder prefix and the runtime intersection
are how the gate stays neutral on every machine.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

import pytest
import yaml

# Locate the repository root. This file lives at
# ``backend/tests/unit/test_config_placeholders_are_neutral.py``,
# so parents[3] is the repo root (unit/ -> tests/ -> backend/ -> root).
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two template files the gate inspects. Both are tracked in the
#: repository; copying them side-by-side at startup keeps the test
#: independent of the loader's hot-reload behaviour.
_ROUTING_TEMPLATE = _REPO_ROOT / "example" / "provider_routing.yaml.example"
_CAPACITY_TEMPLATE = _REPO_ROOT / "example" / "provider_capacity.yaml.example"

#: Path to the operator's live routing config. Gitignored
#: (``/.config/`` in ``.gitignore``); absent on a clean clone.
_LIVE_ROUTING = _REPO_ROOT / ".config" / "provider_routing.yaml"

#: Prefix that every pattern in the example templates MUST carry. The
#: rule is prefix-only — the suffix is whatever the template author
#: used to illustrate the shape (Strong / Balanced / Cheap / Metered).
#: Pinning a specific suffix here would make the gate fail on any
#: future placeholder rename, which is exactly the drift the
#: placeholder rule is supposed to avoid.
PLACEHOLDER_PREFIX = "^Example "


def _load_yaml(path: Path) -> dict:
    """Read and parse a YAML file from the repo root.

    Returned dict is the top-level mapping. Parsing failure surfaces
    as the loader's own exception — the template MUST still parse, by
    contract; if it does not, this gate fails loudly rather than
    silently passing on a missing key.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _routing_template_patterns() -> list[str]:
    """Flatten every regex pattern from the routing example's tiers."""
    data = _load_yaml(_ROUTING_TEMPLATE)
    tiers = data.get("tiers") or {}
    patterns: list[str] = []
    for tier_patterns in tiers.values():
        if not tier_patterns:
            continue
        for pattern in tier_patterns:
            patterns.append(pattern)
    return patterns


def _capacity_template_patterns() -> list[str]:
    """Flatten every regex pattern from the capacity example's providers."""
    data = _load_yaml(_CAPACITY_TEMPLATE)
    providers = data.get("providers") or []
    patterns: list[str] = []
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        if pattern:
            patterns.append(pattern)
    return patterns


def read_live_config_names() -> Optional[list[str]]:
    """Return the operator's live routing patterns, or ``None`` if absent.

    Used by :func:`test_templates_do_not_match_live_config_names` to
    decide whether to run the overlap check. ``None`` means the
    operator has not initialised ``.config/`` yet (a fresh clone) —
    the test then skips rather than fabricating a name list.
    """
    if not _LIVE_ROUTING.exists():
        return None
    data = _load_yaml(_LIVE_ROUTING)
    tiers = data.get("tiers") or {}
    patterns: list[str] = []
    for tier_patterns in tiers.values():
        if not tier_patterns:
            continue
        for pattern in tier_patterns:
            patterns.append(pattern)
    return patterns


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_example_templates_use_placeholder_only() -> None:
    """Every template pattern must start with the placeholder prefix.

    Both files are walked independently so the failure message names
    the file that leaked. A pattern that lacks the prefix compiles
    one deployment's provider set into every install that copies the
    template; this is the shape ``test_no_local_home_path_in_first_party``
    also pins but for the configuration surface.
    """
    routing = _routing_template_patterns()
    capacity = _capacity_template_patterns()

    bad_routing = [p for p in routing if not p.startswith(PLACEHOLDER_PREFIX)]
    bad_capacity = [p for p in capacity if not p.startswith(PLACEHOLDER_PREFIX)]

    assert not bad_routing and not bad_capacity, (
        "Example templates contain a non-placeholder pattern. Replace it "
        f"with one that starts with {PLACEHOLDER_PREFIX!r} so the public "
        "repository does not compile a single deployment's providers into "
        "every checkout.\n"
        f"  routing offenders: {bad_routing}\n"
        f"  capacity offenders: {bad_capacity}"
    )


def test_templates_do_not_match_live_config_names() -> None:
    """Template patterns must not overlap with live deployment names.

    The live list is read from ``.config/provider_routing.yaml`` —
    whatever the operator actually configured. ``None`` (file
    absent) skips the check: on a clean clone there is no live list
    to intersect against, and a synthetic one would only re-create
    the leak.

    Overlap is computed by feeding each template pattern (a Python
    regex) into ``re.search`` against every live pattern's source
    text. A hit means the template's pattern would compile into a
    match for one of the live names — i.e. the template is no longer
    deployment-neutral.
    """
    template_patterns = _routing_template_patterns() + _capacity_template_patterns()
    live_patterns = read_live_config_names()
    if live_patterns is None:
        pytest.skip(
            f"{_LIVE_ROUTING} not present on this checkout; "
            "intersection check is undefined without a live list"
        )

    leaks: list[tuple[str, str]] = []
    for template in template_patterns:
        for live in live_patterns:
            if re.search(template, live):
                leaks.append((template, live))

    assert not leaks, (
        "A template pattern matches a live deployment pattern. The "
        "template is no longer deployment-neutral; replace it with a "
        "placeholder that does not overlap any name in "
        f"{_LIVE_ROUTING}.\n  "
        + "\n  ".join(
            f"template={t!r} matches live={l!r}" for t, l in leaks
        )
    )


def test_config_dir_is_git_ignored() -> None:
    """``.config/`` must be matched by ``.gitignore``.

    The templates' neutrality depends on the live config not also
    shipping in source. ``git check-ignore -q .config/`` exits 0 when
    the path is ignored and 1 when it is not; the test fails on 1 so
    a future ``.gitignore`` edit that drops the rule trips the gate
    at PR time, not at the next export.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(_LIVE_ROUTING)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{_LIVE_ROUTING} is not matched by .gitignore; a plain "
        "`git add -A` would commit one deployment's live routing "
        f"config into the public repository. exit={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_example_templates_still_parse() -> None:
    """Both templates must parse and expose a ``version`` field.

    The parsing requirement is separate from the placeholder check:
    YAML that fails to load would short-circuit the loader on every
    dispatch and is treated as a real defect, not a content nit.
    """
    for path in (_ROUTING_TEMPLATE, _CAPACITY_TEMPLATE):
        data = _load_yaml(path)
        assert isinstance(data, dict), (
            f"{path} did not parse to a mapping; got {type(data).__name__}"
        )
        assert "version" in data, (
            f"{path} parsed but has no 'version' field; the loader uses "
            "the field to pick a schema migration and will reject the "
            "file without it"
        )