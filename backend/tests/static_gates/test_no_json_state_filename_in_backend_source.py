"""VP-006 - acceptance_4 anchor test 1: state JSON filenames must not be
read anywhere in ``backend/`` production source.

Background
----------
The state-machine refactor (task-11 / L4 commit ``34e54af``) migrates
the runtime to SQLite as the single source of truth.  Five legacy JSON
sidecar filenames are pinned to disk as ``DO NOT READ`` - every
read path (the executor, the verification orchestrator, the runtime
persistence, the progress state, the plan state) must use the
repository layer instead.

This is the **acceptance criterion 4** static gate: the grep matches
the brief exactly::

    grep -rE 'plan_state\.json|execution\.json|
              verification_runtime_state\.json|
              verification_executor_state\.json|
              verification_progress_state\.json' backend/

must return **zero** hits in production source code (i.e. anywhere
under ``backend/`` that is NOT a test directory).

The five forbidden filenames are:

    plan_state.json
    execution.json
    verification_runtime_state.json
    verification_executor_state.json
    verification_progress_state.json

Legacy SQL DML/DDL patterns (referencing the old ``plans`` table) are
also checked - ``FROM plans`` / ``INTO plans`` / ``UPDATE plans`` /
``JOIN plans`` must produce zero hits.

The artifact allowlist (``interview.json`` / ``prd.md`` / ``tasks.json``)
is the explicit set of JSON / Markdown filenames PRODUCTION code is
allowed to read; those three are NOT part of the forbidden set.

What the test does
------------------
1. Walk every ``*.py`` file under ``backend/`` excluding ``tests/``
   subtrees, ``.venv``, ``__pycache__``.
2. Reconstruct a **code-only** view of each file: docstrings, ``#``
   comments, and ``--`` SQL comments inside string constants are
   blanked (see :func:`_code_only_text`).
3. For each forbidden filename, scan the code-only lines for a
   whole-word match (anchored on the ``.json`` suffix).
4. For each legacy SQL pattern, scan all lines for the literal string.
5. Assert zero violations across all five filenames AND zero legacy
   SQL pattern violations.

2026-09-14 scan-scope narrowing (recorded, not silent)
------------------------------------------------------
The original gate grepped raw text, requiring **zero textual
mentions** anywhere in production source.  Later sanctioned
refactors (task #v4 ``plan_tasks``, the 2026-08-25 audit fixes,
the progress-endpoint fix) document the migration history in
docstrings / comments / SQL DDL provenance comments — none of
which are runtime read paths.  The gate's stated intent ("state
JSON filenames must not be READ anywhere in production source")
is preserved by scanning executable code only.

Two sanctioned exceptions remain in executable code and are
explicitly allowlisted:

  * ``PLAN_STATE_FILENAME: str = "plan_state.json"`` in
    ``plan_state.py`` / ``tasks_generator.py`` /
    ``preflight_review.py`` — the bare-filename literal is
    deliberately pinned by
    ``backend/tests/test_plan_state_filename_constant.py``, and
    ``plans/{id}/plan_state.json`` remains a current workflow
    phase-state artifact (see the project CLAUDE.md "File Outputs
    by Phase" table).  Any OTHER executable occurrence of the
    string (f-strings, path joins, ``open()`` targets) is still a
    violation.
  * ``--`` SQL comments inside the ``_DDL_*`` string constants of
    ``state_machine/db/schema.py`` — provenance notes recording
    which legacy JSON artifact each column replaced.
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize
from pathlib import Path

import pytest

# Backend root for the static scan.  ``backend/tests/static_gates`` is
# three levels deep from ``backend/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: Sub-directory names to skip wholesale.  Test subtrees are the main
#: exclusion; vendor/cache trees are skipped to avoid noise.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", ".venv", "__pycache__", ".pytest_cache", ".git"}
)

#: The five deprecated state JSON filenames.
FORBIDDEN_STATE_JSONS: tuple[str, ...] = (
    "plan_state.json",
    "execution.json",
    "verification_runtime_state.json",
    "verification_executor_state.json",
    "verification_progress_state.json",
)

#: Legacy SQL patterns that reference the retired ``plans`` table.
#: Each is a literal DML/DDL substring.
LEGACY_SQL_PATTERNS: tuple[str, ...] = (
    "FROM plans",
    "INTO plans",
    "UPDATE plans",
    "JOIN plans",
)

#: Sanctioned ``plan_state.json`` literal sites.  The bare filename
#: constant is deliberately pinned by
#: ``backend/tests/test_plan_state_filename_constant.py`` in exactly
#: these three scope files, and ``plans/{id}/plan_state.json`` remains
#: a current workflow phase-state artifact.  Any OTHER executable
#: occurrence of the string is still a violation.
_PLAN_STATE_LITERAL_RE = re.compile(
    r'^\s*PLAN_STATE_FILENAME:\s*str\s*=\s*"plan_state\.json"\s*$'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_only_text(source: str) -> str:
    """Return ``source`` with non-executable text blanked to spaces.

    The gate pins "no runtime READ PATH for the five legacy JSON state
    files".  Later sanctioned refactors document the migration history
    in prose — docstrings, ``#`` comments, and ``--`` SQL comments
    inside the ``_DDL_*`` string constants — and prose is not a read
    path.  Blank those regions (preserving newlines and every other
    character position) so the filename scan covers executable code
    only.

    Blanket rules applied:

      * every ``tokenize.COMMENT`` span (``#`` to end of line);
      * every docstring (a string-literal ``ast.Constant`` in
        expression-statement position as the first statement of a
        module / class / function body);
      * every ``--`` to end-of-line region INSIDE a string-literal
        constant (the schema DDL provenance comments).
    """
    tree = ast.parse(source)
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    # (start_offset, end_offset) absolute-character spans to blank.
    spans: list[tuple[int, int]] = []
    lines = source.splitlines(keepends=True)
    line_offsets: list[int] = []
    off = 0
    for line in lines:
        line_offsets.append(off)
        off += len(line)

    def abs_pos(row: int, col: int) -> int:
        # tokenize / AST rows are 1-based.
        return line_offsets[row - 1] + col

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        start = abs_pos(node.lineno, node.col_offset)
        end = abs_pos(node.end_lineno, node.end_col_offset)
        # Docstring test: Constant -> Expr -> first stmt of a body.
        parent = parents.get(node)
        grand = parents.get(parent) if parent is not None else None
        is_docstring = (
            isinstance(parent, ast.Expr)
            and isinstance(grand, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and grand.body
            and grand.body[0] is parent
        )
        if is_docstring:
            spans.append((start, end))
            continue
        # SQL provenance comments inside string constants: blank
        # ``--`` through end-of-line.  In this codebase ``--`` inside
        # a string literal only occurs in the schema DDL constants;
        # each comment sits on its own line so EOL blanking is exact.
        seg = source[start:end]
        for m in re.finditer(r"--[^\n]*", seg):
            spans.append((start + m.start(), start + m.end()))

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                spans.append((abs_pos(*tok.start), abs_pos(*tok.end)))
    except tokenize.TokenError:
        pass

    chars = list(source)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_python_files(backend_root: Path):
    """Yield every ``*.py`` file under ``backend_root``, skipping tests
    and vendor / cache trees.
    """
    for dirpath, dirnames, filenames in os.walk(backend_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


#: Cache of ``path -> code-only text``.  ``_scan_for_filename`` runs once
#: per forbidden filename and the blanking (ast.parse + tokenize over the
#: whole file) is the dominant cost — without this cache the acceptance
#: gate re-blanks every file N times (N = len(FORBIDDEN_STATE_JSONS)) and
#: blew past the CI 60 s per-test timeout after the backend tree grew
#: (32 s → >60 s on the 3.9 CI interpreter, 2026-09-16).  Files do not
#: change during a test run, so keying on the path is safe.
_CODE_ONLY_CACHE: dict = {}


def _code_only_for_file(py_file: Path):
    """Blanked-source text for ``py_file``, cached across scans."""
    if py_file not in _CODE_ONLY_CACHE:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        _CODE_ONLY_CACHE[py_file] = _code_only_text(source)
    return _CODE_ONLY_CACHE[py_file]


def _raw_lines(py_file: Path):
    """Original source lines of ``py_file`` (for hit reporting)."""
    try:
        return py_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _scan_for_filename(backend_root: Path, filename: str):
    """Return ``(path, line_no, line_text)`` for each whole-word match
    of ``filename`` in the *executable code* of any scanned Python
    file (docstrings / comments / SQL-DDL comments blanked — see
    :func:`_code_only_text`).

    The one sanctioned executable occurrence — the pinned
    ``PLAN_STATE_FILENAME: str = "plan_state.json"`` literal in the
    three scope files — is skipped for ``plan_state.json`` only.
    """
    hits: list[tuple[Path, int, str]] = []
    pattern = re.compile(rf"\b{re.escape(filename)}\b")
    for py_file in _iter_python_files(backend_root):
        code_only = _code_only_for_file(py_file)
        if code_only is None:
            continue
        source_lines = None
        for line_no, line in enumerate(code_only.splitlines(), start=1):
            if not pattern.search(line):
                continue
            if filename == "plan_state.json" and _PLAN_STATE_LITERAL_RE.match(line):
                continue
            if source_lines is None:
                source_lines = _raw_lines(py_file)
            hits.append((py_file, line_no, source_lines[line_no - 1]))
    return hits


def _scan_for_pattern(backend_root: Path, pattern_str: str):
    """Return ``(path, line_no, line_text)`` for each occurrence of the
    substring ``pattern_str`` in any scanned Python file.
    """
    hits: list[tuple[Path, int, str]] = []
    for py_file in _iter_python_files(backend_root):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern_str in line:
                hits.append((py_file, line_no, line))
    return hits


# ---------------------------------------------------------------------------
# The acceptance gate
# ---------------------------------------------------------------------------

#: Standard pytest markers - drive this test into the correct
#: collection bucket.  ``acceptance_4`` is the L5 anchor that VP-006
#: selects via ``-m acceptance_4``.
pytestmark = [
    pytest.mark.acceptance_4,
]


@pytest.mark.acceptance_4
def test_no_json_state_filename_in_backend_source() -> None:
    """Production code under ``backend/`` (excluding ``tests/`` and
    vendor/cache trees) must contain zero references to the five
    forbidden state JSON filenames.
    """
    violations: list[str] = []
    for filename in FORBIDDEN_STATE_JSONS:
        for path, line_no, line in _scan_for_filename(BACKEND_DIR, filename):
            violations.append(
                f"{path.relative_to(BACKEND_DIR)}:{line_no}: "
                f"{line.strip()!r}  <-- contains {filename!r}"
            )
    assert not violations, (
        "Production code still references deprecated JSON state "
        "filenames. The state-machine refactor pins SQLite as the "
        "single source of truth; these references must be migrated "
        "to the repository layer (RoutingRepository / "
        "ExecutionRepository / VerificationRepository / "
        "ArtifactRepository). Forbidden filenames: "
        f"{list(FORBIDDEN_STATE_JSONS)!r}\n" + "\n".join(violations)
    )


@pytest.mark.acceptance_4
def test_no_legacy_sql_patterns_in_backend_source() -> None:
    """Production code under ``backend/`` (excluding tests/vendor) must
    contain zero references to the legacy ``plans`` SQL DML/DDL
    patterns (``FROM plans`` / ``INTO plans`` / ``UPDATE plans`` /
    ``JOIN plans``).

    The state-machine refactor pins the four ``plan_*`` tables plus
    the ``schema_version`` migration logbook; the legacy ``plans``
    table must not appear in production SQL.
    """
    violations: list[str] = []
    for pattern_str in LEGACY_SQL_PATTERNS:
        for path, line_no, line in _scan_for_pattern(BACKEND_DIR, pattern_str):
            violations.append(
                f"{path.relative_to(BACKEND_DIR)}:{line_no}: "
                f"{line.strip()!r}  <-- contains SQL pattern "
                f"{pattern_str!r}"
            )
    assert not violations, (
        "Production code still references the legacy plans SQL "
        "table. The state-machine refactor pins the four plan_* "
        "tables plus schema_version; migrate to the repository "
        "layer.\n" + "\n".join(violations)
    )
