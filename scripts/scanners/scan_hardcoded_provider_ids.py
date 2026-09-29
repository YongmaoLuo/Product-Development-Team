"""Static scanner for hardcoded provider identifiers in source files.

Background
----------
The backend uses CC Switch as the single source of truth for
provider configuration (base URLs, API keys, model names). Production
code MUST read provider data dynamically via
:func:`provider_config_consumer.get_provider_config`, not from
hardcoded dicts, literal URLs/keys, or bare provider ID strings embedded
in the source.

This scanner is the gate that catches regressions. It fails when any
scanned production file contains:

* Hardcoded provider base URLs
  (e.g. ``api.vendor-a.example``, ``vendor-b.example/anthropic``).
* Hardcoded API key literals
  (e.g. ``sk-...``, ``fake-vendor-a-key``).
* Static provider registry dicts
  (``PROVIDER_URLS = {...}``, ``_PROVIDER_REGISTRY = {...}``).
* Quoted legacy provider ID string literals
  (``'vendor-b'``, ``"vendor-a"``, ``'vendor-c-app'``), which should use the
  canonical kebab-case forms instead.

Test fixtures are exempt.

Usage
-----
::

    # Scan the whole tree (default scoped dirs).
    python3 scripts/scanners/scan_hardcoded_provider_ids.py

    # Scan a single file.
    python3 scripts/scanners/scan_hardcoded_provider_ids.py --file backend/agent.py

Exit codes
----------
* ``0`` — no violations (clean tree / clean file).
* ``1`` — at least one production file references a forbidden literal.

Scope and exclusions
--------------------
Directories scanned by default (project-relative):
  * ``backend/``
  * ``tools/``

Directory names skipped anywhere in the walk:
  * ``.git``, ``.venv``, ``node_modules``, ``build``, ``dist``,
    ``__pycache__``

File extensions scanned:
  * ``.py``, ``.yaml``, ``.yml``, ``.json``, ``.md``, ``.sh``

Path-prefix whitelist (any path whose project-relative form starts
with one of these is treated as a fixture and skipped):
  * ``backend/tests/``
  * ``tests/``

The fixture whitelist mirrors the boundary enforced by
``backend/tests/unit/test_static_provider_id_scan.py`` and
``scripts/scanners/scan_provider_url_map_refs.py`` — production files are
everything under ``backend/`` except ``backend/tests/``, plus
everything under ``tools/``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Project root: ``scripts/scanners/scan_hardcoded_provider_ids.py`` lives
# at ``<root>/scripts/scanners/scan_hardcoded_provider_ids.py``, so three
# parents up is the project root. Resolved so a symlinked checkout reports
# the real root (the whitelist check is path-based).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Directories walked when no ``--file`` is given. The gate's job is
# to catch production-code regressions; tests / plans / docs are out
# of scope (the fixture whitelist below documents that boundary).
SCOPED_DIRS: Tuple[str, ...] = ("backend", "tools")

# Directory names pruned from the walk regardless of depth. Mirrors
# the standard VCS / build / dependency folders every Python+Node
# project carries; the scanner must not descend into them.
EXCLUDED_DIR_NAMES: frozenset = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
    }
)

# File extensions worth scanning. Source files only — binary / lock
# files would either be skipped by the text decoder anyway or produce
# noise.
SCANNED_SUFFIXES: frozenset = frozenset(
    {".py", ".yaml", ".yml", ".json", ".md", ".sh"}
)

# Path prefixes treated as test fixtures or runtime artifacts. A file
# whose project-relative path equals or starts with one of these is
# exempt from the scan.
FIXTURE_PREFIXES: Tuple[str, ...] = (
    "backend/tests/",
    "tests/",
)

# Files exempt because they ARE the scanner (so they necessarily
# contain the forbidden literals in docstrings, the constant table,
# and the violation messages). The scanner cannot scan itself without
# false positives.
SELF_EXEMPT_PATHS: frozenset = frozenset(
    {
        "scripts/scanners/scan_hardcoded_provider_ids.py",
        "scripts/scanners/scan_provider_url_map_refs.py",
    }
)

# Provider base URL host fragments. Any production file that hardcodes
# one of these URL fragments is treating the CC Switch DB as a static
# fixture — a regression of the "single source of truth" contract.
#
# The list is deliberately **yours to extend**, not a table of this
# project's vendors. Which hosts count as "a provider endpoint" depends
# on the deployment, so seeding real vendor domains here would both
# describe one operator's setup and miss every deployment that uses a
# different vendor. Two ways to extend it:
#
#   * add host fragments for the vendors you actually route through, or
#   * set ``PDT_SCAN_EXTRA_URL_NEEDLES`` to a comma-separated list, which
#     keeps a private vendor set out of a committed file entirely.
#
# The entries below are placeholders that show the shape.
PROVIDER_URL_NEEDLES: Tuple[str, ...] = (
    "api.vendor-a.example",
    "api.vendor-b.example",
    "api.vendor-c.example",
    "vendor-a.example/anthropic",
    "vendor-b.example/anthropic",
)

# Extra host fragments from the environment, so a deployment can scan
# for its own vendors without naming them in source.
_ENV_EXTRA_URL_NEEDLES = "PDT_SCAN_EXTRA_URL_NEEDLES"

API_KEY_NEEDLES: Tuple[str, ...] = (
    "sk-fake-vendor-a-key",
    "fake-vendor-b-key",
    "sk-vendor-a-test-key",
    "FAKE-vendor-a-pro",
)

# Regex patterns for static provider registry assignments. Catches
# module-level dicts whose name suggests a hardcoded provider table
# (URLs, keys, models, or all-in-one registries). The pattern matches
# the assignment target, not the contents — the contents are scanned
# separately by the URL/key needles above, but the assignment itself
# is a regression marker even when the URL is in a comment.
PROVIDER_REGISTRY_NAME_RE = re.compile(
    r"^\s*(_)?[A-Z_]{0,64}PROVIDER[_A-Z]*\s*=\s*\{",
    re.MULTILINE,
)

# Quoted legacy provider ID string literals.  Production code should use
# the canonical kebab-case IDs read from CC Switch at runtime, not bare
# string constants like ``'vendor-b'`` or ``"vendor-a"``.
#
# The regex matches a single- or double-quoted string whose entire
# content is one of the legacy bare IDs.  This avoids false positives
# on model names (e.g. ``"vendor-a M2"``) and on identifiers that
# contain the provider token (e.g. ``should_degrade_vendor-b``).
LEGACY_LITERAL_RE = re.compile(
    r"(['\"])(?:" + "|".join(re.escape(n) for n in ("vendor-b", "vendor-a", "vendor-c-app")) + r")\1"
)

# Map each legacy bare ID to its canonical kebab-case replacement for
# violation messages.
LEGACY_TO_CANONICAL: dict = {
    "vendor-b": "vendor-b-pro",
    "vendor-a": "vendor-a-pro",
    "vendor-c-app": "vendor-c-app",
}

# ---------------------------------------------------------------------------
# Comment / docstring stripping
# ---------------------------------------------------------------------------


def strip_python_comments_and_docstrings(source: str) -> str:
    """Return ``source`` with ``#`` comments and triple-quoted strings blanked.

    Comments and docstrings are replaced by spaces (newlines preserved)
    so line numbers stay accurate.  Single-quoted and double-quoted
    string literals (non-triple) are **preserved** so the scanner can
    inspect them for hardcoded provider IDs.
    """
    lines = source.splitlines(keepends=True)
    result: List[List[str]] = [list(line) for line in lines]

    in_docstring = False
    docstring_quote = ""
    for lno, line in enumerate(lines):
        i = 0
        while i < len(line):
            ch = line[i]
            if not in_docstring:
                if ch == "#":
                    for j in range(i, len(line)):
                        if line[j] != "\n":
                            result[lno][j] = " "
                    break
                if ch in ('"', "'"):
                    quote = line[i : i + 3]
                    if quote in ('"""', "'''"):
                        in_docstring = True
                        docstring_quote = quote
                        i += 3
                        continue
            else:
                if line[i : i + 3] == docstring_quote:
                    in_docstring = False
                    docstring_quote = ""
                    i += 3
                    continue
                if line[i] != "\n":
                    result[lno][i] = " "
            i += 1

    return "".join("".join(row) for row in result)


def strip_yaml_comments(text: str) -> str:
    """Return ``text`` with ``#`` comments removed.

    Only blanks from an unquoted ``#`` to end of line.  A ``#`` inside
    a quoted scalar (single or double quotes) is preserved.  This is
    intentionally simple — sufficient for the provider-ID scan scope.
    """
    lines = text.splitlines(keepends=True)
    result: List[str] = []
    for line in lines:
        hash_pos = line.find("#")
        if hash_pos >= 0:
            prefix = line[:hash_pos]
            if prefix.count('"') % 2 == 0 and prefix.count("'") % 2 == 0:
                line = line[:hash_pos] + ("\n" if line.endswith("\n") else "")
        result.append(line)
    return "".join(result)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def iter_scanned_files(root: Path) -> Iterable[Path]:
    """Yield every source file under each scoped directory in ``root``.

    The walker prunes any directory whose name appears in
    ``EXCLUDED_DIR_NAMES`` and skips files whose suffix is not in
    ``SCANNED_SUFFIXES``. Missing scoped directories are silently
    skipped.
    """
    for scoped in SCOPED_DIRS:
        scoped_root = root / scoped
        if not scoped_root.is_dir():
            continue
        yield from _walk(scoped_root)


def _walk(directory: Path) -> Iterable[Path]:
    """Depth-first walk that prunes excluded directory names."""
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            if entry.name in EXCLUDED_DIR_NAMES:
                continue
            yield from _walk(entry)
        elif entry.is_file():
            if entry.suffix in SCANNED_SUFFIXES:
                yield entry


# ---------------------------------------------------------------------------
# Whitelist + scan helpers
# ---------------------------------------------------------------------------


def is_fixture_path(path: Path, project_root: Path = PROJECT_ROOT) -> bool:
    """Return True when ``path`` falls under one of the fixture prefixes."""
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return False
    for prefix in FIXTURE_PREFIXES:
        bare = prefix.rstrip("/")
        if rel == bare or rel.startswith(prefix):
            return True
    return False


def is_self_exempt_path(path: Path, project_root: Path = PROJECT_ROOT) -> bool:
    """Return True when ``path`` is the scanner itself (or another meta file)."""
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return False
    return rel in SELF_EXEMPT_PATHS


def scan_source(
    source: str, source_path: str
) -> List[Tuple[int, str]]:
    """Return ``[(line_num, needle), ...]`` for forbidden literals in ``source``.

    Combines three signal sources:

      * ``PROVIDER_URL_NEEDLES`` — hardcoded provider base URLs.
      * ``API_KEY_NEEDLES`` — hardcoded fake-key literals.
      * ``PROVIDER_REGISTRY_NAME_RE`` — static provider registry assignments.
      * ``LEGACY_LITERAL_RE`` — quoted string literals that hardcode a
        legacy bare provider ID (``'vendor-b'``, ``"vendor-a"``, etc.).

    Comments and docstrings are stripped before scanning so the gate
    focuses on real code tokens (a quoted example in a docstring is
    documentation, not a hardcoded runtime value).

    Matches are deduped by absolute offset so multiple violations on
    the same line are all reported.
    """
    suffix = Path(source_path).suffix
    if suffix == ".py":
        cleaned = strip_python_comments_and_docstrings(source)
    elif suffix in (".yaml", ".yml"):
        cleaned = strip_yaml_comments(source)
    else:
        cleaned = source

    seen_offsets: dict = {}

    # URL and key needles — the committed placeholders plus whatever the
    # deployment adds for its own vendors via the environment.
    extra_urls = tuple(
        fragment.strip()
        for fragment in os.environ.get(_ENV_EXTRA_URL_NEEDLES, "").split(",")
        if fragment.strip()
    )
    needles = sorted(
        list(PROVIDER_URL_NEEDLES) + list(API_KEY_NEEDLES) + list(extra_urls),
        key=len,
        reverse=True,
    )
    for needle in needles:
        start = 0
        while True:
            pos = cleaned.find(needle, start)
            if pos < 0:
                break
            if pos not in seen_offsets:
                seen_offsets[pos] = needle
            start = pos + len(needle)

    # Static registry assignment pattern: catches the ``=`` line even
    # when the dict body itself has no URL/key literal (e.g. the dict
    # is empty, or its values are variables).
    for m in PROVIDER_REGISTRY_NAME_RE.finditer(cleaned):
        if m.start() not in seen_offsets:
            seen_offsets[m.start()] = "<PROVIDER_REGISTRY assignment>"

    # Quoted legacy provider ID literals.
    for m in LEGACY_LITERAL_RE.finditer(cleaned):
        if m.start() not in seen_offsets:
            legacy_id = m.group(0).strip("'\"")
            canonical = LEGACY_TO_CANONICAL[legacy_id]
            seen_offsets[m.start()] = (
                f"{m.group(0)!r} (canonical: {canonical!r})"
            )

    return [
        (cleaned[:pos].count("\n") + 1, seen_offsets[pos])
        for pos in sorted(seen_offsets)
    ]


def scan_file(path: Path, project_root: Path = PROJECT_ROOT) -> List[str]:
    """Return ``[violation_message, ...]`` for ``path``.

    Returns ``[]`` when the path is whitelisted (a fixture or a
    self-exempt meta file), when the file cannot be read as UTF-8,
    or when no forbidden literal is present.
    """
    if is_fixture_path(path, project_root):
        return []
    if is_self_exempt_path(path, project_root):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        rel = str(path)
    violations: List[str] = []
    for line_num, needle in scan_source(text, str(path)):
        violations.append(f"{rel}:{line_num}: found {needle}")
    return violations


def scan_tree(project_root: Path = PROJECT_ROOT) -> List[str]:
    """Run the scan over every file under each scoped directory."""
    all_violations: List[str] = []
    for path in iter_scanned_files(project_root):
        all_violations.extend(scan_file(path, project_root))
    return all_violations


def scan_single_file(file_arg: str, project_root: Path = PROJECT_ROOT) -> List[str]:
    """Run the scan over a single file.

    Resolves ``file_arg`` relative to ``project_root`` when it is not
    absolute. The fixture / self-exempt whitelist still applies, so a
    caller that points ``--file`` at a test fixture gets an empty
    violation list (the gate is consistent between single-file and
    full-tree modes).
    """
    raw = Path(file_arg)
    target = raw if raw.is_absolute() else (project_root / raw).resolve()
    if not target.is_file():
        return [f"error: --file target does not exist: {target}"]
    return scan_file(target, project_root)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="scan_hardcoded_provider_ids",
        description=(
            "Static scan for hardcoded provider URLs, API keys, "
            "static provider registry assignments, and quoted legacy "
            "provider ID literals (vendor-b, vendor-a, vendor-c-app). "
            "Exits 0 when clean, 1 when any production file contains "
            "a forbidden literal."
        ),
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "Scan a single file (project-relative or absolute). When "
            "omitted, the scan walks backend/ and tools/."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-violation output (exit code is still set).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Alias for the default behaviour: exit non-zero when any "
            "violation is found. Accepted for compatibility with "
            "callers that pass --strict explicitly; the scanner is "
            "always strict (the soft mode was removed when "
            "HARDCODED_CHAIN was deleted)."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the scan and return a process exit code.

    Returns ``0`` when no violations are found, ``1`` when at least one
    production file references a forbidden literal. Each violation is
    printed to stderr as
    ``"<path>:<line>: found <needle>"`` so stdout stays clean.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.file:
        violations = scan_single_file(args.file, PROJECT_ROOT)
    else:
        violations = scan_tree(PROJECT_ROOT)

    if not violations:
        return 0

    if not args.quiet:
        for line in violations:
            print(line, file=sys.stderr)
        scope = (
            f"file {args.file!r}"
            if args.file
            else "production code under backend/ and tools/"
        )
        print(
            f"error: {len(violations)} hardcoded provider "
            f"identifier(s) found in {scope}; move the data into the "
            "CC Switch DB or a fixture under tests/",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())