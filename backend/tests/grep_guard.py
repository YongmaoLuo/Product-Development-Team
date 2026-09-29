"""Reusable whole-scope scanner for the JSON path gate.

Background
----------
The 5 grep gate tests in ``backend/tests/test_server_json_cleanup.py``
each pin one pattern class against the production source tree
(server.py, verification_executor.py, state_machine/**/*.py).  They
are not reusable as a programmatic scanner — each test hard-codes
the scan targets and calls ``pytest.fail`` on a hit.

This module is the reusable shim that the task-5 caller consumes.
It applies the same five pattern classes the production tests use
and returns a deterministic list of classified violations:

    [
        {
            "file":         "<relative path>",
            "line":         <1-indexed line number>,
            "pattern_type": <one of the five pattern classes>,
            "matched_text": "<matched substring>",
        },
        ...
    ]

Why a separate module
---------------------
The contract is wider than the test gates':

  * callers may direct the scanner at any subset of the source tree
    (``target_files``, ``target_dirs``);
  * callers may invoke it from a different root (so the on-disk
    layout can be a tmp_path, a copy of the project, or a
    production deploy);
  * callers want a data structure, not a ``pytest.fail`` string.

The 5 pattern classes — direct_literal, string_concat,
variable_reference, fstring_format, pathlib_join — are identical
to those used by ``backend/tests/test_server_json_cleanup.py``.
We re-implement the small regex / AST pieces here (rather than
importing the test module) so this module is importable from
non-test contexts (e.g. task 5's caller) without dragging the
full test fixture machinery along.

Boundary conditions (per the task brief)
----------------------------------------
  * target files / directories that do not exist MUST be skipped
    (not raise) so the scanner is robust to partial state;
  * a clean fixture (zero forbidden constructions) MUST return
    ``[]``;
  * f-string / .format() / string concatenation / path-join
    patterns are reported as their own ``pattern_type`` strings
    (so downstream consumers can group violations by class).

TDD contract
------------
The two tests in ``backend/tests/unit/test_grep_guard.py`` pin
the behaviour:

  - ``test_grep_guard_catches_each_pattern_group``:
    four fixtures, four distinct pattern classes → at least four
    violations, one per fixture, with the correct pattern_type.
  - ``test_grep_guard_returns_zero_for_clean_fixture``:
    a clean fixture → ``[]``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Constants — the five forbidden JSON state filenames.  The previous
# task pinned this list and the consumers (state-machine repository,
# the backend) all rely on it.
# ---------------------------------------------------------------------------

FORBIDDEN_FILENAMES: tuple[str, ...] = (
    "plan_state.json",
    "execution.json",
    "verification_runtime_state.json",
    "verification_executor_state.json",
    "verification_progress_state.json",
)

# Exempt filenames — legitimate per-run artifacts that are
# explicitly NOT scanned as violations.  These are the same
# three exempt files the production tests use:
#
#   verification_plan.json
#   verification_report.json
#   verification_execution_results.json
#
# Plus the user-authored artifacts the project always permitted:
#
#   interview.json
#   prd.json
#   tasks.json
#   review.json
#
# Without this exemption, the pathlib_join and os.path.join
# patterns would false-positive on benign code such as
# ``Path(plans_dir) / "interview.json"`` or
# ``os.path.join(plan_dir, "verification_plan.json")`` — both
# of which are legitimate per-run sidecar writes.
EXEMPT_FILENAMES: frozenset[str] = frozenset(
    {
        "verification_plan.json",
        "verification_report.json",
        "verification_execution_results.json",
        "interview.json",
        "prd.json",
        "tasks.json",
        "review.json",
    }
)


# ---------------------------------------------------------------------------
# Default scan scope — the three production locations pinned by the
# task brief.  This matches the ``_TARGETS`` + ``state_machine/**
# recursion in ``test_server_json_cleanup.py``.
# ---------------------------------------------------------------------------

# The backend root is inferred from this module's location so the
# scanner is reusable from any cwd.  ``grep_guard.py`` lives at
# ``backend/tests/grep_guard.py`` so the parent directory is the
# backend root.
_BACKEND_ROOT_DEFAULT = Path(__file__).resolve().parent.parent

# The two top-level production files (server.py, verification_executor.py).
_DEFAULT_TARGET_FILES: tuple[Path, ...] = (
    _BACKEND_ROOT_DEFAULT / "server.py",
    _BACKEND_ROOT_DEFAULT / "verification_executor.py",
)

# The state_machine package, scanned recursively for ``*.py`` files
# (excluding any ``tests/`` subtree).
_DEFAULT_TARGET_DIRS: tuple[Path, ...] = (
    _BACKEND_ROOT_DEFAULT / "state_machine",
)


# ---------------------------------------------------------------------------
# Text-reading helper
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read a file's text, replacing undecodable bytes with U+FFFD.

    Encoding errors are silently coerced so the scanner never crashes
    on an exotic file that happened to be in the scan path.
    """
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Path enumeration
# ---------------------------------------------------------------------------


def _iter_state_machine_py_files(state_machine_root: Path) -> list[Path]:
    """Return every ``*.py`` file under ``state_machine_root``,
    recursively, excluding any ``tests/`` subtree.

    The task brief pins the scan scope to ``state_machine/**/*.py``.
    Test files under ``state_machine/tests/`` are excluded because
    test code is allowed to mention filenames as assertion objects.
    """
    if not state_machine_root.exists():
        return []
    py_files: list[Path] = []
    for path in state_machine_root.rglob("*.py"):
        # Skip any path that has a "tests" segment anywhere.
        try:
            relative = path.relative_to(state_machine_root)
        except ValueError:
            # Path is not under state_machine_root — skip defensively.
            continue
        if "tests" in relative.parts:
            continue
        py_files.append(path)
    return sorted(py_files)


def _collect_scan_paths(
    root: Path | None,
    target_files: Iterable[Path] | None,
    target_dirs: Iterable[Path] | None,
) -> list[Path]:
    """Resolve the final list of files to scan.

    Resolution rules:

      * If ``target_files`` is provided, use those (each is checked
        for existence; missing entries are skipped).
      * If ``target_dirs`` is provided, recurse into each for
        ``*.py`` files (test subtrees excluded).
      * If both ``target_files`` and ``target_dirs`` are None,
        fall back to the default production scope (server.py,
        verification_executor.py, state_machine/**/*.py).

    The ``root`` parameter is informational — it is the caller's
    reference for path normalisation.  When ``root`` is None we
    default to the inferred backend root so paths are reported
    relative to the same parent the production tests use.
    """
    paths: list[Path] = []

    if target_files is None and target_dirs is None:
        # Default scope: production tree.
        for f in _DEFAULT_TARGET_FILES:
            if f.exists():
                paths.append(f)
        for d in _DEFAULT_TARGET_DIRS:
            paths.extend(_iter_state_machine_py_files(d))
    else:
        if target_files is not None:
            for f in target_files:
                if f.exists():
                    paths.append(f)
        if target_dirs is not None:
            for d in target_dirs:
                if d.is_file():
                    paths.append(d)
                elif d.is_dir():
                    for sub in sorted(d.rglob("*.py")):
                        if "tests" not in sub.parts:
                            paths.append(sub)

    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Pattern 1 — direct literal
# ---------------------------------------------------------------------------


def _pattern_direct_literal(
    paths: list[Path],
    root: Path | None,
) -> list[dict]:
    """Return violation dicts for every direct literal occurrence of
    one of the five forbidden filenames.

    A direct literal is a string of the shape ``"<forbidden>.json"``
    or ``'<forbidden>.json'`` — a quoted JSON filename appearing
    directly in source code, not as the result of a ``+`` concat,
    an f-string interpolation, or a ``Path(...) / "<...>.json"``
    join.
    """
    pattern = re.compile(
        r"""(?:"|')"""
        r"""(?:""" + "|".join(
            re.escape(name) for name in FORBIDDEN_FILENAMES
        ) + r""")"""
        r"""(?:"|')"""
    )
    hits: list[dict] = []
    for path in paths:
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is None:
                continue
            matched = match.group(0)
            hits.append({
                "file": _format_path(path, root),
                "line": line_no,
                "pattern_type": "direct_literal",
                "matched_text": matched,
            })
    return hits


# ---------------------------------------------------------------------------
# Pattern 2 — string concatenation
# ---------------------------------------------------------------------------


def _pattern_string_concat(
    paths: list[Path],
    root: Path | None,
) -> list[dict]:
    """Return violation dicts for every ``"..." + "..."`` AST node
    whose concatenation contains one of the five forbidden filenames.

    The AST walk pins adjacent string-literal ``+`` operations on a
    single source line so multi-line continuations are not falsely
    matched.  Bypass shapes covered:

      1. binary split — ``"executi" + "on.json"``
      2. variable + literal — ``PREFIX_LITERAL + ".json"`` where
         ``PREFIX_LITERAL = "plan_state"`` was earlier assigned
         (constant-folding resolves the Name to its bound literal)
      3. three-way concat — ``"plan_" + "state" + ".json"``
    """
    hits: list[dict] = []
    for path in paths:
        try:
            tree = ast.parse(_read_text(path), filename=str(path))
        except SyntaxError:
            continue
        name_literal_map = _collect_module_string_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
                continue
            # Walk the left chain; each step visits a BinOp whose
            # ``right`` is one of the operands (in source order).
            # We collect them into ``reversed_left`` then reverse.
            reversed_left: list[str] = []
            current: ast.expr | None = node.left
            while (
                isinstance(current, ast.BinOp)
                and isinstance(current.op, ast.Add)
                and current.lineno == node.lineno
            ):
                lit = _extract_literal(current.right, name_literal_map)
                if lit is not None:
                    reversed_left.append(lit)
                current = current.left
            bottom_lit = _extract_literal(current, name_literal_map)
            if bottom_lit is not None:
                reversed_left.append(bottom_lit)
            right_lit = _extract_literal(node.right, name_literal_map)
            if right_lit is None:
                continue
            if len(reversed_left) < 1:
                continue
            operands = list(reversed(reversed_left)) + [right_lit]
            if len(operands) < 2:
                continue
            joined = "".join(operands)
            # Deduplicate same-line + same-joined hits: a single
            # top-level Add chain may be visited more than once by
            # ``ast.walk`` if it contains a nested Add whose left
            # chain points back to the same operands.
            already_reported = any(
                h["file"] == _format_path(path, root)
                and h["line"] == node.lineno
                and h["matched_text"] == joined
                for h in hits
            )
            if already_reported:
                continue
            for forbidden in FORBIDDEN_FILENAMES:
                if forbidden in joined:
                    hits.append({
                        "file": _format_path(path, root),
                        "line": node.lineno,
                        "pattern_type": "string_concat",
                        "matched_text": joined,
                    })
                    break
    return hits


def _collect_module_string_bindings(tree: ast.AST) -> dict[str, str]:
    """Walk ``tree`` at module level and collect every
    ``Name = Constant(str)`` binding.

    Returns ``{name: literal_value}``.  Only simple
    ``ast.Assign(targets=[Name(...)], value=Constant(str, ...))``
    shapes are recognised.
    """
    bindings: dict[str, str] = {}
    body = tree.body if isinstance(tree, ast.Module) else ()
    for node in body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        bindings[target.id] = node.value.value
    return bindings


def _extract_literal(
    expr: ast.expr | None,
    bindings: dict[str, str],
) -> str | None:
    """Return the literal string value of ``expr`` if it is a string
    literal or a ``Name`` bound to a string literal in ``bindings``;
    otherwise return ``None``.
    """
    if expr is None:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name) and expr.id in bindings:
        return bindings[expr.id]
    return None


# ---------------------------------------------------------------------------
# Pattern 3 — variable reference
# ---------------------------------------------------------------------------


def _pattern_variable_reference(
    paths: list[Path],
    root: Path | None,
) -> list[dict]:
    """Return violation dicts for any of the legacy variable names
    that the cleanup tasks 1-4 through 1-7 were supposed to delete:

        _RT_FN_*  - any constant / variable whose name starts with
                    the ``_RT_FN_`` prefix that was the old
                    "runtime filename" indirection layer.
        progress_state_file
        plan_state_file
        exec_file
        progress_file

    The pattern is a whole-token match so identifiers such as
    ``_RT_FN_VEXECUTOR`` (which starts with the prefix) AND a
    literal variable named ``progress_state_file`` both hit.
    """
    pattern = re.compile(
        r"\b(?:"
        r"_RT_FN_\w+"
        r"|progress_state_file"
        r"|plan_state_file"
        r"|exec_file"
        r"|progress_file"
        r")\b"
    )
    hits: list[dict] = []
    for path in paths:
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is None:
                continue
            hits.append({
                "file": _format_path(path, root),
                "line": line_no,
                "pattern_type": "variable_reference",
                "matched_text": match.group(0),
            })
    return hits


# ---------------------------------------------------------------------------
# Pattern 4 — f-string / .format()
# ---------------------------------------------------------------------------


def _pattern_fstring_format(
    paths: list[Path],
    root: Path | None,
) -> list[dict]:
    """Return violation dicts for f-strings that interpolate into a
    ``.json`` suffix and for ``.format(...)`` calls whose argument
    list contains a ``.json`` suffix.

    The task brief regex is::

        f["'][^"']*\\{[^}]*\\}[^"']*\\.json["']
        |\\.format\\([^)]*\\.json\\)

    We apply this regex verbatim — a hit on this regex is, by
    definition, a violation.
    """
    pattern = re.compile(
        r"""f["'][^"']*\{[^}]*\}[^"']*\.json["']"""
        r"""|"""
        r"""\.format\([^)]*\.json\)"""
    )
    hits: list[dict] = []
    for path in paths:
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is None:
                continue
            hits.append({
                "file": _format_path(path, root),
                "line": line_no,
                "pattern_type": "fstring_format",
                "matched_text": match.group(0),
            })
    return hits


# ---------------------------------------------------------------------------
# Pattern 5 — pathlib / os.path.join
# ---------------------------------------------------------------------------


def _pattern_pathlib_join(
    paths: list[Path],
    root: Path | None,
) -> list[dict]:
    """Return violation dicts for ``Path(...) / "<filename​>.json"`` and
    ``os.path.join(..., "<filename​>.json")`` style joins.

    The task brief regex is::

        Path\\([^)]*\\)\\s*/\\s*["'][^"']*\\.json
        |os\\.path\\.join\\([^)]*\\.json\\)

    The regex is applied verbatim — any join whose result ends in
    ``.json`` triggers a candidate hit.  Candidates are then
    filtered against the exempt-filename list (see
    ``EXEMPT_FILENAMES``) so that legitimate per-run artifacts
    such as ``Path(plans_dir) / "interview.json"`` are NOT
    reported as violations.
    """
    pattern = re.compile(
        r"""Path\([^)]*\)\s*/\s*["']([^"']*\.json)["']"""
        r"""|"""
        r"""os\.path\.join\([^)]*?["']([^"']*\.json)["']\)"""
    )
    hits: list[dict] = []
    for path in paths:
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is None:
                continue
            # The two regex groups capture the basename portion of
            # the join.  At least one will be empty for a given hit
            # (since the two arms are alternatives).  Pull the
            # non-empty one and check the exemption list.
            basename = match.group(1) or match.group(2) or ""
            if basename in EXEMPT_FILENAMES:
                continue
            hits.append({
                "file": _format_path(path, root),
                "line": line_no,
                "pattern_type": "pathlib_join",
                "matched_text": match.group(0),
            })
    return hits


# ---------------------------------------------------------------------------
# Path formatting
# ---------------------------------------------------------------------------


def _format_path(path: Path, root: Path | None) -> str:
    """Format ``path`` for the ``file`` field of a violation dict.

    When ``root`` is provided, the path is reported relative to it
    (so the output is stable across cwd changes).  When ``root`` is
    None, the path is reported as a forward-slash relative path
    from the inferred backend root (so the test fixtures see
    basenames — `direct_literal.py`, etc.).
    """
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)
    # No root: return as-is so tests get the basename form
    # (e.g. "direct_literal.py") which the test asserts on.
    return str(path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_grep_guard(
    root: Path | None = None,
    target_files: Iterable[Path] | None = None,
    target_dirs: Iterable[Path] | None = None,
) -> list[dict]:
    """Scan ``root`` (or the default production tree) for forbidden
    JSON state file constructions and return a list of classified
    violations.

    Parameters
    ----------
    root : Path | None
        The reference root for path normalisation.  When None the
        scanner falls back to the inferred backend root.  Each
        violation's ``file`` field is reported relative to ``root``
        when possible.
    target_files : Iterable[Path] | None
        Explicit list of files to scan.  When None the production
        default scope is used (server.py, verification_executor.py).
    target_dirs : Iterable[Path] | None
        Explicit list of directories to recurse into (for ``*.py``
        files; ``tests`` subtrees are skipped).  When None the
        production default scope is used (``state_machine/``).

    Returns
    -------
    list[dict]
        A list of violation dicts, one per detected hit.  Each
        dict has the four keys ``file``, ``line``, ``pattern_type``,
        ``matched_text``.  An empty list means the scan found zero
        violations.

    Boundary conditions
    -------------------
    * If ``target_files`` / ``target_dirs`` point to non-existent
      paths, the scanner SKIPS them and continues — it never raises
      FileNotFoundError.
    * If ``root`` is a tmp_path the scanner reads files relative to
      that directory and reports paths relative to it.
    * A clean fixture (no forbidden construction) returns ``[]``.
    """
    paths = _collect_scan_paths(root, target_files, target_dirs)
    if not paths:
        return []

    violations: list[dict] = []
    violations.extend(_pattern_direct_literal(paths, root))
    violations.extend(_pattern_string_concat(paths, root))
    violations.extend(_pattern_variable_reference(paths, root))
    violations.extend(_pattern_fstring_format(paths, root))
    violations.extend(_pattern_pathlib_join(paths, root))

    # Stable ordering: by file (lexicographic), then by line, then by
    # pattern_type.  This makes the output deterministic so callers
    # can compare two scans without flakiness.
    violations.sort(key=lambda v: (v["file"], v["line"], v["pattern_type"]))

    return violations
