"""Static scanner for legacy CC Switch JSON indirection references.

Background
----------
The backend used to read the legacy CC Switch JSON indirection file to
look up provider base URLs. An earlier refactor replaced
that file with ``provider-order.json`` (the optimizer's atomic
contract output) plus the CC Switch SQLite database (the single source
of truth for provider metadata). The legacy lookup table is gone.

This scanner is the CI gate that catches regressions: if any
production file under ``backend/`` or ``tools/`` mentions
the legacy JSON filename (or its bare token form),
the scan fails and prints every violation. Test fixtures are
exempt — the very tests that verify the legacy file is gone
legitimately mention its name.

Usage
-----
::

    python3 scripts/scanners/scan_provider_url_map_refs.py
    python3 scripts/scanners/scan_provider_url_map_refs.py --strict

Exit codes
----------
* ``0`` — no violations (clean tree).
* ``1`` — at least one production file references the legacy literal.

``--strict`` is currently a no-op (the scanner is always strict — a
violation always fails the gate). The flag is accepted so CI callers
can write the explicit form, and a future "warning vs error" mode can
keep the existing command line stable.

Scope and exclusions
--------------------
Directories scanned (project-relative):
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
``backend/tests/unit/test_static_provider_id_scan.py`` — production
files are everything under ``backend/`` except ``backend/tests/``,
plus everything under ``tools/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Project root: ``scripts/scanners/scan_provider_url_map_refs.py`` lives
# at ``<root>/scripts/scanners/scan_provider_url_map_refs.py``, so three
# parents up is the project root. Resolved so a symlinked checkout reports
# the real root (the whitelist check is path-based).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Directories walked for source files. Everything else (``tests/`` at
# the project root, ``plans/``, ``docs/``, etc.) is out of scope — the
# gate only needs to catch production code regressions, and pulling in
# extra trees would add false positives from generated content
# (e.g. plan status reports that quote the literal in passing).
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
# noise (e.g. ``package-lock.json`` is technically JSON but is
# machine-generated and never references the legacy token).
SCANNED_SUFFIXES: frozenset = frozenset(
    {".py", ".yaml", ".yml", ".json", ".md", ".sh"}
)

# Path prefixes treated as test fixtures. A file whose project-relative
# path equals or starts with one of these is exempt from the scan. The
# trailing slash matters: we want ``backend/tests/foo.py`` to match
# ``backend/tests/`` but ``backend/tests_foo.py`` to NOT match.
#
# This whitelist is the ONLY mechanism that distinguishes "this is
# production code that must not reference the legacy file" from "this
# is a test that legitimately mentions the legacy file name". Keep it
# tight: any addition here is a hole in the gate.
FIXTURE_PREFIXES: Tuple[str, ...] = (
    "backend/tests/",
    "tests/",
)

# The forbidden literals. Two forms:
#
#   * the legacy JSON filename — the actual filename. Catches both
#     "load this file" code paths and string literals that name the
#     file.
#   * the bare token (without suffix) — the logical name. Catches code
#     that refers to the table by a shorter alias (e.g. a variable
#     named PROVIDER_URL_MAP or a comment that references the legacy table).
#
# The bare token is a substring of the filename, so a single
# A legacy JSON filename occurrence produces two needle matches at
# the same position. ``scan_source`` dedupes by absolute offset so
# each location reports exactly one violation.
_LEGACY_JSON_TOKEN = "provider" + "-" + "url" + "-map" + ".json"
_LEGACY_BARE_TOKEN = "provider" + "-" + "url" + "-map"

FORBIDDEN_NEEDLES: Tuple[str, ...] = (
    _LEGACY_JSON_TOKEN,
    _LEGACY_BARE_TOKEN,
)

# Files exempt because they ARE the scanner (so they necessarily
# contain the literal in docstrings, the constant table above, and
# the violation messages the scanner prints). The scanner cannot
# scan itself without false positives — the same way ESLint's
# ``--ignore-pattern`` exempts its own config file. Project-relative
# paths, no leading slash. Add new files here only when they are
# meta-tools whose very purpose is to encode the rule.
SELF_EXEMPT_PATHS: frozenset = frozenset(
    {
        "scripts/scanners/scan_provider_url_map_refs.py",
    }
)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def iter_scanned_files(root: Path) -> Iterable[Path]:
    """Yield every source file under each scoped directory in ``root``.

    The walker prunes any directory whose name appears in
    ``EXCLUDED_DIR_NAMES`` and skips files whose suffix is not in
    ``SCANNED_SUFFIXES``. Missing scoped directories are silently
    skipped — a checkout that doesn't have ``backend/`` (e.g. a
    frontend-only worktree) should still produce a usable scan over
    ``tools/``.

    Yields absolute ``Path`` objects. The order is deterministic
    (``Path.rglob`` walks alphabetically within each directory) so
    repeated runs on the same tree produce identical violation output.
    """
    for scoped in SCOPED_DIRS:
        scoped_root = root / scoped
        if not scoped_root.is_dir():
            continue
        yield from _walk(scoped_root)


def _walk(directory: Path) -> Iterable[Path]:
    """Depth-first walk that prunes excluded directory names
    and skips symbolic links.

    Implemented manually (rather than ``os.walk``) so the pruned
    directories are never even stat-ed — this matters for
    ``node_modules`` in particular, which can contain tens of
    thousands of files we'd otherwise walk and discard.

    Symlinks are skipped (not followed). A self-referential or
    cyclic symlink (e.g. a leftover ``<pkg>/<pkg>`` link pointing
    back at its own parent) would otherwise spin the walker
    forever — this happened in CI when a stray artifact symlink
    appeared in a working tree. Skipping all
    symlinks is the safe default for a static textual scan; the
    cost is missing violations that live in a symlinked source tree,
    which is acceptable for this gate (the production deploy does
    not depend on symlinked sources).
    """
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            continue
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
    """Return True when ``path`` falls under one of the fixture prefixes.

    The check is project-root relative: a file is a fixture iff its
    project-root-relative path equals a prefix (without the trailing
    slash) or starts with the prefix (with the trailing slash). This
    mirrors the boundary enforced by the existing
    ``backend/tests/unit/test_static_provider_id_scan.py`` so the
    in-process scanner unit tests and this on-disk scanner agree on
    what counts as a fixture.

    Paths outside ``project_root`` return ``False`` — only
    project-relative paths can be whitelisted, since the prefix list
    is project-relative.
    """
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
    """Return True when ``path`` is the scanner itself (or another meta file).

    The scanner cannot scan itself without false positives — its
    docstrings, constants, and violation messages necessarily contain
    the literal. The same applies to any other file whose purpose is
    to encode the rule. ``SELF_EXEMPT_PATHS`` enumerates those
    meta-files by their project-relative path so the exemption is
    auditable and tight (no wildcards, no path prefixes that could
    accidentally cover an unrelated file).
    """
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return False
    return rel in SELF_EXEMPT_PATHS


def scan_source(
    source: str, source_path: str
) -> List[Tuple[int, str]]:
    """Return ``[(line_num, needle), ...]`` for forbidden literals in ``source``.

    Two needles are checked (the legacy JSON filename and the bare
    token form). Matches are deduped by absolute offset so a
    single substring that satisfies both needles still produces only
    one violation per location — otherwise a literal
    The legacy JSON filename literal would be double-counted, since the
    bare form is a strict prefix of the full form.

    Line numbers are 1-based. The ``needle`` field on each returned
    tuple is the *longest* forbidden literal that matched at that
    offset, so callers see the most specific form (the filename, not
    the bare logical name) in their violation output.
    """
    # Longest needle first so we attribute the offset to the most
    # specific form. ``FORBIDDEN_NEEDLES`` happens to be sorted this
    # way already (``.json`` form is longer than the bare form), but
    # sort defensively so reordering the constant can't regress this.
    needles = sorted(FORBIDDEN_NEEDLES, key=len, reverse=True)
    seen_offsets: dict = {}
    for needle in needles:
        start = 0
        while True:
            pos = source.find(needle, start)
            if pos < 0:
                break
            if pos not in seen_offsets:
                seen_offsets[pos] = needle
            start = pos + len(needle)
    # Sort by absolute offset so violations appear in source order.
    return [
        (source[:pos].count("\n") + 1, seen_offsets[pos])
        for pos in sorted(seen_offsets)
    ]


def scan_file(path: Path, project_root: Path = PROJECT_ROOT) -> List[str]:
    """Return ``[violation_message, ...]`` for ``path``.

    Returns ``[]`` when the path is whitelisted (a fixture or a
    self-exempt meta file), when the file cannot be read as UTF-8,
    or when no forbidden literal is present. Each violation message
    is formatted as ``"<rel_path>:<line>: found <needle>"`` so the
    output is greppable and consistent with the existing scanner
    output format.
    """
    if is_fixture_path(path, project_root):
        return []
    if is_self_exempt_path(path, project_root):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Unreadable file: skip rather than fail. The gate's job is
        # to catch string literals, not to police file permissions.
        return []
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        # Path is outside project_root (e.g. a symlink that points
        # into a sibling repo). The scanner cannot meaningfully
        # report a project-relative path for it, and crashing here
        # would block CI for what is effectively a cross-repo
        # symlink rather than a real violation. Skip — the scanner
        # is responsible for catching string literals, not for
        # policing cross-repo symlinks.
        return []
    violations: List[str] = []
    for line_num, needle in scan_source(text, str(path)):
        violations.append(f"{rel}:{line_num}: found {needle!r}")
    return violations


def scan_tree(project_root: Path = PROJECT_ROOT) -> List[str]:
    """Run the scan over every file under each scoped directory.

    Returns a flat list of violation messages in source order across
    the whole tree. Empty list means "no violations" (the gate
    passes).
    """
    all_violations: List[str] = []
    for path in iter_scanned_files(project_root):
        all_violations.extend(scan_file(path, project_root))
    return all_violations


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CLI.

    The ``--strict`` flag is currently a no-op — the scanner always
    treats violations as failures. The flag exists so CI callers can
    write the explicit form (``--strict``) and a future
    "warn vs error" mode doesn't have to change every call site.
    """
    parser = argparse.ArgumentParser(
        prog="scan_provider_url_map_refs",
        description=(
            "Static scan for legacy JSON-path references "
            "under backend/ and tools/. Exits 0 when clean, 1 when "
            "any production file references the legacy literal."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Treat any violation as a failure (this is already the "
            "default; accepted for forward compatibility with future "
            "warn-only modes)."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-violation output (exit code is still set).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the scan and return a process exit code.

    Parameters
    ----------
    argv:
        Optional argv list (defaults to ``sys.argv[1:]``). Tests pass
        an explicit list so the argparse layer is exercised
        independently of the real command line.

    Returns
    -------
    int
        ``0`` when no violations are found.
        ``1`` when at least one production file references a forbidden
        literal. Each violation is printed to stderr as
        ``"<path>:<line>: found <needle>"`` so stdout stays clean
        (callers that pipe stdout don't pick up diagnostic output).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    violations = scan_tree(PROJECT_ROOT)
    if not violations:
        return 0

    if not args.quiet:
        for line in violations:
            print(line, file=sys.stderr)
        print(
            f"error: {len(violations)} legacy JSON-path "
            "reference(s) found in production code; remove them or "
            "move the file under a fixture prefix "
            f"({', '.join(FIXTURE_PREFIXES)})",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
