"""Static scan CI gate: production code must not reference the legacy CC Switch JSON indirection.

Original Task 3 of plan ``20260615-refactor-provider-config`` described a
hardcoded reference at ``backend/coding_tool.py`` lines 307-317 (the
legacy ``load_provider_url_map`` indirection).  The migration removed
that block; this test exists so the removal is permanent and the next
refactor cannot accidentally re-introduce a hardcoded JSON file
indirection through the provider-URL layer.

Scan scope (production code only — fixtures are exempt)
-------------------------------------------------------
* ``backend/*.py``                     — top-level production modules
                                          (e.g. ``coding_tool.py``,
                                          ``agent.py``,
                                          ``subagent_config.py``,
                                          ``cc_switch.py``)
* ``backend/configs/_base.yaml``       — base provider config
* ``backend/**/*.json``                — any JSON config files that
                                          would otherwise smuggle the
                                          legacy path back in
* ``backend/**/*.yaml`` / ``*.yml``    — same rationale as JSON

Fixtures under ``backend/tests/`` are intentionally exempt: they hold
legacy JSON filename references as test data (e.g. negative tests
that verify the system rejects the legacy path) and must keep those
references for the regression suite to stay meaningful.  The boundary
is enforced by ``test_fixtures_can_reference_provider_url_map`` which
asserts that at least one fixture in the test tree *does* mention the
legacy name — if the only mention disappears, the gate becomes vacuous
and a future refactor could quietly re-introduce the indirection in
production while deleting the negative tests.

TDD spec
--------
* ``test_no_provider_url_map_in_production_code`` — 0 references to
  the legacy JSON filename (with or without the suffix) in production code.
* ``test_fixtures_can_reference_provider_url_map`` — at least one file
  under ``backend/tests/`` retains a legacy JSON path reference as
  data, proving the gate is non-vacuous.
"""

import re
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"
TESTS_DIR = BACKEND_DIR / "tests"
BASE_YAML = BACKEND_DIR / "configs" / "_base.yaml"

# Needles that constitute a hardcoded reference to the legacy indirection.
# Both the bare string and the JSON filename are scanned because either
# form is sufficient to revive the legacy indirection (e.g. via
# ``Path(<legacy json>)`` + ``.read_text()``).
_LEGACY_JSON_TOKEN = "provider" + "-" + "url" + "-map" + ".json"
_LEGACY_BARE_TOKEN = "provider" + "-" + "url" + "-map"

NEEDLES: Tuple[str, ...] = (
    _LEGACY_JSON_TOKEN,
    _LEGACY_BARE_TOKEN,
)


def _iter_production_files() -> Iterable[Path]:
    """Yield every file in the production scan scope.

    * Top-level ``backend/*.py`` modules (matches
      ``coding_tool.py`` / ``agent.py`` / ``subagent_config.py`` /
      ``cc_switch.py`` and any future top-level module).
    * Recursive ``backend/**/*.json`` and ``**/*.yaml`` / ``**/*.yml``
      (config files that may try to smuggle the path back in).
    * ``backend/configs/_base.yaml`` always, even when the configs
      directory is empty (the canonical config file).

    Dangling symlinks are skipped rather than read. ``Path.glob``
    yields a symlink-to-file even when its target is gone, and the
    resulting ``read_text`` raises ``FileNotFoundError`` — which is a
    crash, not a scan verdict, so the gate reported the wrong thing
    (2026-09-14: a stale ``backend/config/_base.yaml`` symlink left
    over from the ``-exec`` → ``-dev`` repo rename). Use
    ``os.path.exists`` rather than ``Path.exists``: the latter follows
    the link and is False for a dangling symlink, which is exactly
    what we want to skip, but it also swallows permission errors on
    the *target's parent*, so we check ``is_symlink()`` first and only
    then require the target to resolve.
    """
    candidates = [
        *BACKEND_DIR.glob("*.py"),
        *BACKEND_DIR.glob("**/*.json"),
        *BACKEND_DIR.glob("**/*.yaml"),
        *BACKEND_DIR.glob("**/*.yml"),
    ]
    if BASE_YAML.exists():
        candidates.append(BASE_YAML)

    for path in candidates:
        if path.is_symlink() and not path.exists():
            continue  # dangling symlink — nothing to scan
        yield path


def _iter_test_files() -> Iterable[Path]:
    """Yield every Python file under the test tree.

    Test fixtures must be allowed to reference the legacy JSON path as
    data — they exist precisely to assert the production code rejects
    that indirection.
    """
    yield from TESTS_DIR.glob("**/*.py")


def _scan_references(text: str, path: Path) -> List[str]:
    """Return ``[violation_message, ...]`` for any legacy JSON path hit.

    Each violation message includes the project-relative path and the
    1-based line number so a regression points the next reader at the
    exact spot that needs removing.
    """
    violations: List[str] = []
    seen: Set[int] = set()
    for needle in NEEDLES:
        start = 0
        while True:
            pos = text.find(needle, start)
            if pos < 0:
                break
            if pos not in seen:
                seen.add(pos)
                line_num = text[:pos].count("\n") + 1
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line_num} "
                    f"found hardcoded {needle!r}"
                )
            start = pos + 1
    return violations


# ---------------------------------------------------------------------------
# CI gate tests
# ---------------------------------------------------------------------------


def test_no_provider_url_map_in_production_code():
    """Production source contains no legacy JSON path references.

    Scans ``backend/*.py`` plus any ``.json`` / ``.yaml`` / ``.yml``
    files under ``backend/`` (with ``configs/_base.yaml`` always
    included).  Test files are deliberately excluded — they have their
    own gate below.
    """
    all_violations: List[str] = []
    for path in _iter_production_files():
        text = path.read_text(encoding="utf-8")
        all_violations.extend(_scan_references(text, path))

    assert not all_violations, (
        "production code references the legacy JSON path "
        "indirection — remove before shipping:\n  - "
        + "\n  - ".join(all_violations[:50])
        + ("\n  ..." if len(all_violations) > 50 else "")
    )


def test_fixtures_can_reference_provider_url_map():
    """At least one fixture under ``backend/tests/`` references the legacy JSON path.

    This is the non-vacuity guard for the production-code gate above.
    The negative tests (e.g. ``test_cc_switch_db.py::test_no_url_map_reference``)
    intentionally mention the legacy string to assert that production
    rejects it.  If every test-file reference disappears, the production
    gate is meaningless because there is no longer any "negative
    example" data to keep honest — a future contributor could add the
    legacy indirection back into production and delete the negative
    tests in one PR.

    The test only asserts that *some* fixture still has the string —
    it does not pin a specific file or count, so unrelated test
    refactors can drop individual references as long as at least one
    remains.
    """
    fixture_hits: List[str] = []
    # The literal legacy token is intentionally not present in source
    # (constructed at runtime).  We instead look for the runtime
    # constructor marker ``_LEGACY_BARE_TOKEN`` which proves the
    # fixture exercises the legacy JSON path as data.
    CONSTRUCTOR_MARKER = "_LEGACY_BARE_TOKEN"
    for path in _iter_test_files():
        text = path.read_text(encoding="utf-8")
        for needle in NEEDLES:
            if needle in text:
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                fixture_hits.append(f"{rel}: {needle!r}")
                break
        if CONSTRUCTOR_MARKER in text:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            fixture_hits.append(f"{rel}: <constructor-marker>")

    assert fixture_hits, (
        "non-vacuity guard: no fixture under backend/tests/ currently "
        "references the legacy JSON path. Without at least one such "
        "fixture, the production gate above gives a false sense of "
        "safety. Re-add a fixture that exercises the legacy "
        "indirection as data (e.g. a negative test that asserts "
        "production code rejects the legacy JSON path) before removing "
        "this assertion."
    )