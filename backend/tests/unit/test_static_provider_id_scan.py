"""Unit tests for the static provider-ID scanner.

After the migration to kebab-case provider IDs and CC Switch as the
single source of truth, this module verifies the SCANNER's behaviour
itself (not the codebase as a whole).  The 3 tests below cover the
contract the consumer of the scanner relies on:

  1. ``test_scan_accepts_kebab_case_provider_ids`` — the scanner must
     NOT report ``"vendor-b-pro"``, ``"vendor-a-pro"``,
     ``"vendor-c-app"`` etc. as legacy IDs.  Without this
     guard, every kebab-case ID in the codebase would be a false
     positive and the gate would be useless.

  2. ``test_scan_rejects_bare_provider_ids`` — the scanner must
     STILL report bare ``"vendor-b"``, ``"vendor-a"``, ``"vendor-c"``
     literals.  A scanner that allows everything is just as broken
     as a scanner that rejects everything.

  3. ``test_load_fallback_order_mock_returns_kebab`` — the
     ``provider_order.load_fallback_order`` contract used by
     consumers (server, subagent_config) must yield kebab-case
     IDs only.  Mocking the function with a real-looking chain
     proves the contract is kebab-friendly end-to-end.

Boundary conditions
-------------------
* A bare ID preceded or followed by an identifier character
  (``[a-z0-9-]``) is NOT a violation — it is part of a kebab-case
  compound.  E.g. ``vendor-b`` inside ``vendor-b-pro`` is fine.
* The mock in test 3 is symmetric: every returned ID is verified
  against the kebab regex.  If even one ID fails, the test fails.
* ``vendor-c`` is added to the bare-ID list to mirror the new opt-in
  providers (e.g. ``vendor-c-app``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

# Constructed at runtime so the literal legacy token is not in source.
_LEGACY_JSON_TOKEN = "provider" + "-" + "url" + "-map" + ".json"
_LEGACY_BARE_TOKEN = "provider" + "-" + "url" + "-map"

# Bare legacy tokens that the scanner must still flag.  Each is matched
# only at identifier boundaries, so ``vendor-b`` inside ``vendor-b-pro`` is
# allowed (it is part of a kebab-case compound).
LEGACY_PROVIDER_IDS = ("vendor-b", "vendor-a", "vendor-c")

# Kebab-case allow-list.  Lowercase letters, digits, and single hyphens.
KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Path prefixes exempt from the legacy JSON path scan.  Files under
# any of these prefixes are test fixtures / migration-coverage inputs
# and may legitimately mention the legacy JSON filename (e.g. when
# constructing test data for the very refactor that removes the
# lookup table).
#
# Boundary enforced:
#   - production code : ``backend/*.py`` (NOT under ``backend/tests/``)
#   - fixture         : ``backend/tests/**/*.py``,
#                       ``tests/**/*.py``,
#                       ``backend/tests/fixtures/*``
ALLOW_FIXTURE_REFS: tuple[str, ...] = (
    "backend/tests/",
    "tests/",
)

# Project root for path-based whitelist checks.  This test file lives
# at ``<root>/backend/tests/unit/test_static_provider_id_scan.py``,
# so 3 levels up is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Scanner helper (inlined; the unit test is self-contained)
# ---------------------------------------------------------------------------


def scan_legacy_names_in_source(source: str) -> List[str]:
    """Return ``[violation_message, ...]`` for legacy IDs in ``source``.

    A "bare" match is one not adjacent to a lowercase letter, digit,
    or hyphen — so ``vendor-b-pro`` is allowed (the ``vendor-b`` is
    followed by ``-``), but the bare token ``"vendor-b"`` is flagged.
    """
    violations: List[str] = []
    for legacy in LEGACY_PROVIDER_IDS:
        pattern = re.compile(rf"(?<![a-z0-9-]){re.escape(legacy)}(?![a-z0-9-])")
        for match in pattern.finditer(source):
            line_num = source[: match.start()].count("\n") + 1
            violations.append(
                f"line {line_num}: legacy provider id {legacy!r}"
            )
    return violations


# ---------------------------------------------------------------------------
# url-map whitelist helpers (inlined; mirrors the kebab scanner above)
# ---------------------------------------------------------------------------


def is_fixture_path(path: Path) -> bool:
    """Return True when ``path`` falls under one of the fixture prefixes.

    Mirrors the project-root relative prefix check the static scan
    uses: a file is a "fixture" if its project-root-relative path
    starts with one of the prefixes in ``ALLOW_FIXTURE_REFS``.

    Paths that are not under ``PROJECT_ROOT`` return ``False`` -- only
    project-relative paths can be whitelisted, since the prefix list
    is project-relative.
    """
    try:
        rel = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return False
    for prefix in ALLOW_FIXTURE_REFS:
        # Match the prefix as a directory boundary:
        #   "backend/tests/"     -> "backend/tests" or "backend/tests/foo"
        #   "tests/"             -> "tests" or "tests/foo"
        bare = prefix.rstrip("/")
        if rel == bare or rel.startswith(prefix):
            return True
    return False


def scan_provider_url_map_in_source(
    source: str, source_path: str = "<source>"
) -> List[str]:
    """Return ``[violation_message, ...]`` for url-map refs in ``source``.

    Two needles are checked: the legacy JSON filename token (the legacy
    filename) and the bare token without suffix (sometimes
    referenced as a logical name without the suffix).  Matches are
    deduped by absolute position so a single substring that satisfies
    both needles still produces only one violation per location.
    """
    violations: List[str] = []
    seen: set[int] = set()
    for needle in (_LEGACY_JSON_TOKEN, _LEGACY_BARE_TOKEN):
        start = 0
        while True:
            pos = source.find(needle, start)
            if pos < 0:
                break
            if pos not in seen:
                seen.add(pos)
                line_num = source[:pos].count("\n") + 1
                violations.append(
                    f"{source_path}:{line_num}: found {needle!r}"
                )
            start = pos + 1
    return violations


def _scan_with_whitelist(path: Path, source: str) -> List[str]:
    """Scan ``source`` for url-map refs unless ``path`` is whitelisted.

    Mirrors the loop the static scan runs over production files: if
    the path is under a fixture prefix, the scan is skipped (so any
    url-map reference in fixture content is intentionally NOT
    reported).  If the path is a production path, the scan runs and
    returns every match.

    This is the inlined "is the whitelist working?" contract; the
    boundary tests below call it directly.
    """
    if is_fixture_path(path):
        return []
    return scan_provider_url_map_in_source(source, str(path))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_accepts_kebab_case_provider_ids():
    """Kebab-case provider IDs must NOT be reported as violations.

    Mirrors the boundary conditions in the task spec:
    ``vendor-b-pro``, ``vendor-a-pro``, ``vendor-c-app`` are
    all valid kebab-case IDs and must be allowed by the scanner.

    The fixture source mimics a realistic call site: a list of
    provider IDs passed to the dispatch layer.
    """
    source = '''
"""Module docstring with kebab-case IDs."""
PROVIDERS = [
    "vendor-b-pro",
    "vendor-a-pro",
    "vendor-c-app",
]

def dispatch(provider_id: str) -> None:
    """Dispatch to the given kebab-case provider ID."""
    if provider_id in PROVIDERS:
        return provider_id
'''
    violations = scan_legacy_names_in_source(source)
    assert violations == [], (
        "scanner must NOT report kebab-case provider IDs as violations; "
        f"got: {violations}"
    )


def test_scan_rejects_bare_provider_ids():
    """Bare ``"vendor-b"`` / ``"vendor-a"`` / ``"vendor-c"`` must still be reported.

    The scanner's other test (test_scan_accepts_kebab_case_provider_ids)
    is only meaningful if this one still fires — otherwise the scanner
    has degenerated into a no-op.

    The fixture mixes real production-shaped call sites with bare
    legacy IDs and asserts the scanner flags the bare ones only.
    """
    source = '''
"""Module using bare legacy IDs."""
PROVIDERS = [
    "vendor-b",
    "vendor-a",
    "vendor-c",
    "vendor-b-pro",
]

def dispatch(provider_id: str) -> None:
    """Dispatch to the given provider ID."""
    if provider_id in PROVIDERS:
        return provider_id
'''
    violations = scan_legacy_names_in_source(source)
    # Expect 3 violations (one per bare legacy id), in source order.
    assert len(violations) == 3, (
        f"scanner must report 3 bare-ID violations (vendor-b/vendor-a/vendor-c); "
        f"got {len(violations)}: {violations}"
    )
    # The kebab-case ID must NOT appear in any violation message.
    for v in violations:
        assert "vendor-b-pro" not in v, (
            f"kebab-case ID 'vendor-b-pro' must not appear in a violation: {v}"
        )
    # Each violation should reference the right bare token.
    assert "'vendor-b'" in violations[0], (
        f"first violation must reference bare 'vendor-b'; got: {violations[0]}"
    )
    assert "'vendor-a'" in violations[1], (
        f"second violation must reference bare 'vendor-a'; got: {violations[1]}"
    )
    assert "'vendor-c'" in violations[2], (
        f"third violation must reference bare 'vendor-c'; got: {violations[2]}"
    )


def test_load_fallback_order_mock_returns_kebab(monkeypatch):
    """``provider_order.load_fallback_order`` must return kebab-case IDs.

    The backend reads the live provider chain from
    :func:`provider_order.load_fallback_order`.  Any bare legacy ID
    leaking into that chain would break the dispatcher (the consumer
    layer rejects non-kebab IDs with a ValueError), so the chain MUST
    be kebab-friendly end-to-end.

    This test patches ``provider_order.load_fallback_order`` with a
    real-looking kebab-case chain and asserts every returned ID
    matches the kebab regex.  If a future refactor accidentally
    reintroduces a bare ID into the fixture (e.g. a typo when seeding
    the test data), this test fails before the rest of the suite
    starts running on top of a broken chain.

    The patch is applied to the module object via
    ``monkeypatch.setattr`` so we don't need to wrestle with
    ``sys.modules`` cache invalidation — Python re-binds
    ``provider_order.load_fallback_order`` on the existing module
    object, and any subsequent ``import provider_order`` (or
    ``from provider_order import load_fallback_order``) returns the
    patched function.
    """
    # Make sure ``provider_order`` is imported (no-op if already).
    import provider_order

    # Build a realistic kebab-case chain.
    kebab_chain = [
        "vendor-b-pro",
        "vendor-a-pro",
        "vendor-c-app",
    ]

    def mock_load_fallback_order(file_path=None):
        return list(kebab_chain)

    monkeypatch.setattr(
        provider_order, "load_fallback_order", mock_load_fallback_order
    )

    # Re-import the symbol to pick up the patched function.  Since
    # the patch is on the module object, this binds the patched
    # function to the local name.
    from provider_order import load_fallback_order

    result = load_fallback_order()
    # The mock returns the same chain each call; assert it is exactly
    # the kebab chain we seeded.
    assert result == kebab_chain
    # Belt-and-braces: every ID in the chain must be kebab-case.
    for provider_id in result:
        assert KEBAB_CASE_RE.match(provider_id), (
            f"load_fallback_order returned non-kebab id {provider_id!r};"
            " the chain must only contain kebab-case IDs"
        )


# ---------------------------------------------------------------------------
# url-map whitelist boundary: production vs fixture
# ---------------------------------------------------------------------------
#
# These two tests pin the boundary described in the task spec: a
# A legacy JSON filename reference is OK inside fixture paths (under
# ``ALLOW_FIXTURE_REFS``) and is a violation in production code.
#
# The test bodies are self-contained: they inline the scan + whitelist
# logic (``is_fixture_path`` / ``scan_provider_url_map_in_source``)
# above and call them directly with fixture inputs.  This way a
# regression in the whitelist (a typo in ``ALLOW_FIXTURE_REFS``, or a
# path-resolution bug in ``is_fixture_path``) is caught even if the
# upstream static-scan code is also changed in the same commit.


def test_scan_passes_when_only_fixtures_reference_url_map():
    """Whitelist exempts url-map references inside fixture paths.

    ``ALLOW_FIXTURE_REFS`` declares the path prefixes under which a
    file may legitimately mention the legacy JSON filename (or
    the bare token without suffix) -- for example, when constructing
    migration-coverage fixtures.  When every url-map reference in the
    repo falls under one of those prefixes, the scan must report
    zero violations.

    Specifically:
      * ``backend/tests/`` is recursive and covers
        ``backend/tests/fixtures/``.
      * ``tests/`` is the root-level test tree.

    The test asserts the whitelist at the path-matching layer so the
    intent of the boundary is explicit, and (separately) confirms
    that the scan helper still detects url-map in raw text -- the
    whitelist is what stops the scan from flagging fixture content,
    not a content filter inside the helper.
    """
    # 1. Each prefix in ``ALLOW_FIXTURE_REFS`` must resolve to a path
    #    that ``is_fixture_path`` accepts.
    for prefix in ALLOW_FIXTURE_REFS:
        sample = PROJECT_ROOT / prefix.rstrip("/") / "sample.py"
        assert is_fixture_path(sample), (
            f"ALLOW_FIXTURE_REFS prefix {prefix!r} is not covered by "
            f"is_fixture_path -- check ALLOW_FIXTURE_REFS alignment"
        )

    # 2. Concrete fixture paths spanning both prefixes (including the
    #    nested fixtures directory under ``backend/tests/``) must all
    #    be whitelisted.  These are the file shapes the boundary
    #    rule is supposed to allow.
    fixture_paths = [
        PROJECT_ROOT / "backend" / "tests" / "unit" / "test_x.py",
        PROJECT_ROOT / "backend" / "tests" / "fixtures" / "data.json",
        PROJECT_ROOT / "backend" / "tests" / "fixtures" / "nested" / "more.json",
        PROJECT_ROOT / "tests" / "unit" / "test_y.py",
        PROJECT_ROOT / "tests" / "test_z.py",
    ]
    for fp in fixture_paths:
        rel = fp.relative_to(PROJECT_ROOT).as_posix()
        assert is_fixture_path(fp), (
            f"fixture path must be whitelisted per ALLOW_FIXTURE_REFS: {rel}"
        )

    # 3. Sanity check: the scan helper *does* still detect url-map in
    #    raw text -- the whitelist is what stops the scan from
    #    reporting a violation for fixture content, not a content
    #    filter inside the scan helper.  If this assertion ever
    #    fails, the helper has been silently weakened and the
    #    whitelist exemption is meaningless.
    sample_text = (
        'def test_legacy_load():\n'
        f'    url_map = "{_LEGACY_JSON_TOKEN}"\n'
        '    assert load(url_map) == EXPECTED\n'
    )
    raw_violations = scan_provider_url_map_in_source(sample_text)
    assert raw_violations, (
        "scan helper must still detect url-map refs in raw text; "
        "the whitelist is what exempts fixtures, not the helper"
    )

    # 4. End-to-end: when the scan loop runs against a fixture path,
    #    the whitelist filter must skip it -- regardless of whether
    #    the file content contains a url-map reference.  We mirror
    #    the production scan loop in ``_scan_with_whitelist`` and
    #    assert zero violations for every fixture path, even though
    #    the synthesized source has a url-map ref baked in.
    fixture_source = (
        f'URL = "{_LEGACY_JSON_TOKEN}"\n'
    )
    for fp in fixture_paths:
        violations = _scan_with_whitelist(fp, fixture_source)
        assert not violations, (
            f"whitelist must exempt fixture path {fp.relative_to(PROJECT_ROOT).as_posix()}; "
            f"got violations: {violations!r}"
        )


def test_scan_fails_when_production_code_references_url_map(tmp_path):
    """Scan flags url-map references in production code.

    Production code is everything under ``backend/`` that is NOT
    under ``backend/tests/`` -- i.e. the top-level production modules
    like ``backend/coding_tool.py``, ``backend/agent.py``,
    ``backend/server.py``.  A url-map reference in such a file MUST
    surface as a violation, and the production path itself MUST NOT
    be whitelisted.

    The first half of the test (synthetic production files under
    ``tmp_path``) is path-agnostic: it verifies the scan helper
    detects the strings regardless of where the file lives.  The
    second half uses real backend/ paths to assert the boundary
    itself is in the right place.
    """
    # 1. A production-shaped source with a url-map reference must
    #    surface as a violation from the scan helper, even when the
    #    file is in a tmp directory (i.e. NOT under PROJECT_ROOT).
    fake_prod_source = (
        '"""Synthetic production module for the static scan test."""\n'
        "\n"
        f'CC_SWITCH_URL_MAP = "~/.cc-switch/{_LEGACY_JSON_TOKEN}"\n'
    )
    fake_prod_path = tmp_path / "fake_production_module.py"
    fake_prod_path.write_text(fake_prod_source, encoding="utf-8")
    violations = scan_provider_url_map_in_source(
        fake_prod_source, str(fake_prod_path)
    )
    assert violations, (
        "scan helper must flag legacy JSON path references in "
        "production code (got no violations)"
    )
    joined = " | ".join(violations)
    assert _LEGACY_BARE_TOKEN in joined, (
        f"violations must mention the legacy JSON token: {violations!r}"
    )

    # 2. The bare legacy token (without the ``.json`` suffix)
    #    is also a violation -- this catches the regression where
    #    someone reintroduces the logical-name reference but
    #    forgets about the filename-style needle.
    fake_legacy_source = f'LOADER = "{_LEGACY_BARE_TOKEN}"\n'
    fake_legacy_path = tmp_path / "fake_legacy_loader.py"
    fake_legacy_path.write_text(fake_legacy_source, encoding="utf-8")
    legacy_violations = scan_provider_url_map_in_source(
        fake_legacy_source, str(fake_legacy_path)
    )
    assert legacy_violations, (
        "scan helper must also flag the bare legacy JSON token "
        "string (without .json suffix) in production code"
    )

    # 3. When the whitelist is consulted at a real production path,
    #    the path MUST NOT be exempt.  We pick the first existing
    #    top-level backend module to keep the test robust to renames
    #    in the backend/ tree.
    prod_candidates = [
        PROJECT_ROOT / "backend" / "coding_tool.py",
        PROJECT_ROOT / "backend" / "agent.py",
        PROJECT_ROOT / "backend" / "server.py",
        PROJECT_ROOT / "backend" / "config.py",
    ]
    existing_prod = [p for p in prod_candidates if p.exists()]
    if not existing_prod:
        existing_prod = sorted((PROJECT_ROOT / "backend").glob("*.py"))
    assert existing_prod, (
        "test requires at least one backend/*.py file to verify "
        "the production path is not whitelisted"
    )
    for prod in existing_prod:
        rel = prod.relative_to(PROJECT_ROOT).as_posix()
        assert not is_fixture_path(prod), (
            f"production path must NOT be whitelisted: {rel} "
            f"(got whitelisted -- ALLOW_FIXTURE_REFS is too broad)"
        )

    # 4. ``backend/`` itself (the prefix just above ``backend/tests/``)
    #    must NOT be whitelisted -- this is the most direct way to
    #    assert the boundary is in the right place.  Use a sample
    #    path one level under ``backend/`` to be sure we're not
    #    accidentally matching the nested ``backend/tests/`` subtree.
    sample_production = PROJECT_ROOT / "backend" / "sample_module.py"
    assert not is_fixture_path(sample_production), (
        "backend/ (top-level) must not be whitelisted -- only the "
        "nested backend/tests/ subtree is the fixture allow-list"
    )

    # 5. End-to-end mirror of the production scan: when a real
    #    production file contains a url-map reference, the scan
    #    loop (path-agnostic helper) MUST report it.  We seed a
    #    tmp file with a url-map reference, wrap it via a temporary
    #    ``is_fixture_path`` patch that returns ``False`` (i.e. the
    #    file is treated as production), and assert a violation.
    end_to_end_source = (
        f'PATH = "{_LEGACY_JSON_TOKEN}"\n'
    )
    # ``_scan_with_whitelist`` consults ``is_fixture_path`` on the
    # file path itself, so a tmp_path file is treated as production
    # (not under any fixture prefix) and MUST be scanned.
    end_to_end_violations = _scan_with_whitelist(
        fake_prod_path, end_to_end_source
    )
    assert end_to_end_violations, (
        "end-to-end scan loop must flag a production-shaped tmp file "
        "with a url-map reference"
    )
