"""VP-017 acceptance gate: 9 consumer modules must each go through
the repository layer directly - no cross-consumer aggregation facade
allowed.

Background
----------
The state-machine refactor (task-11+) pins SQLite as the single
source of truth.  Every consumer that reads plan state must import
one of the four repositories (RoutingRepository /
ExecutionRepository / VerificationRepository / ArtifactRepository)
directly.  No consumer is allowed to introduce a NEW
"cross-consumer aggregation facade" that wraps multiple repositories
into one shim layer (e.g. ``state_accessor.py``, ``plan_facade.py``,
``state_aggregator.py``).

Architecture decision point 5 forbids introducing an "in-memory
aggregation facade" - every consumer MUST go through the four
repositories.

This is the **acceptance criterion 1** static gate for VP-017:

  * Static import graph assertion: the 9 consumer modules must not
    import any cross-consumer aggregation facade module name.
  * JSON path constant assertion: the same 9 consumer modules must
    not contain the 5 forbidden JSON state filename constants.

The 9 consumer modules are (production-only; tests excluded):

  1.  backend/server.py
  2.  backend/agent.py
  3.  backend/verification_agent.py
  4.  backend/verification_executor.py
  5.  backend/verification_subagent.py
  6.  backend/executor.py
  7.  backend/task_manager.py
  8.  backend/state_machine/services/scheduler_support.py
  9.  backend/preflight_review.py

The forbidden cross-consumer facade module names (any one of these
appearing as an import target in a consumer module = violation):

  * state_accessor
  * plan_facade
  * state_aggregator
  * state_manager_facade
  * plan_state_service
  * state_composite
  * plan_aggregator

The 5 forbidden JSON state filenames:

  * plan_state.json
  * execution.json
  * verification_runtime_state.json
  * verification_executor_state.json
  * verification_progress_state.json

What the test does
------------------
1. Walk the 9 consumer module files.
2. For each, parse its imports and check that none of the
   FORBIDDEN_FACADE_MODULE_NAMES appear as an import target.
3. For each, scan all lines for the 5 FORBIDDEN_JSON_FILENAMES
   (whole-word match).
4. Assert zero violations across all 9 consumers.
"""

from __future__ import annotations

import ast
import io
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

PROJECT_ROOT = BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# The 9 consumer modules (relative to PROJECT_ROOT).
# Each consumer MUST reach the state machine through one of the four
# repositories directly - not through a wrapper / facade.
# ---------------------------------------------------------------------------
CONSUMER_MODULES: tuple[str, ...] = (
    "backend/server.py",
    "backend/agent.py",
    "backend/verification_agent.py",
    "backend/verification_executor.py",
    "backend/verification_subagent.py",
    "backend/executor.py",
    "backend/task_manager.py",
    "backend/state_machine/services/scheduler_support.py",
    "backend/preflight_review.py",
)

# ---------------------------------------------------------------------------
# Forbidden cross-consumer aggregation facade module names.
# Any consumer module whose import graph references one of these is
# violating the architecture decision point 5 contract.
# ---------------------------------------------------------------------------
FORBIDDEN_FACADE_MODULE_NAMES: tuple[str, ...] = (
    "state_accessor",
    "plan_facade",
    "state_aggregator",
    "state_manager_facade",
    "plan_state_service",
    "state_composite",
    "plan_aggregator",
)

# ---------------------------------------------------------------------------
# Forbidden JSON state filename constants - same five as in
# ``test_no_json_state_filename_in_backend_source.py``.
# ---------------------------------------------------------------------------
FORBIDDEN_JSON_FILENAMES: tuple[str, ...] = (
    "plan_state.json",
    "execution.json",
    "verification_runtime_state.json",
    "verification_executor_state.json",
    "verification_progress_state.json",
)

#: Sanctioned ``plan_state.json`` literal.  Mirrors the allowlist in
#: ``test_no_json_state_filename_in_backend_source.py``: the bare
#: filename constant is pinned by
#: ``backend/tests/test_plan_state_filename_constant.py`` and
#: ``plans/{id}/plan_state.json`` remains a current workflow
#: phase-state artifact.  Any OTHER executable occurrence is still a
#: violation.
_PLAN_STATE_LITERAL_RE = re.compile(
    r'^\s*PLAN_STATE_FILENAME:\s*str\s*=\s*"plan_state\.json"\s*$'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_only_text(source: str) -> str:
    """Return ``source`` with non-executable text blanked to spaces.

    Mirror of the helper in
    ``test_no_json_state_filename_in_backend_source.py`` — keep the
    two in sync.  The gate pins "no runtime read path for the five
    legacy JSON state files"; docstrings, ``#`` comments, and ``--``
    SQL comments inside string constants are prose, not read paths,
    and are blanked so the filename scan covers executable code only.
    """
    tree = ast.parse(source)
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    spans: list[tuple[int, int]] = []
    lines = source.splitlines(keepends=True)
    line_offsets: list[int] = []
    off = 0
    for line in lines:
        line_offsets.append(off)
        off += len(line)

    def abs_pos(row: int, col: int) -> int:
        return line_offsets[row - 1] + col

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        start = abs_pos(node.lineno, node.col_offset)
        end = abs_pos(node.end_lineno, node.end_col_offset)
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


def _consumer_paths() -> list:
    """Return absolute Paths for every entry in :data:`CONSUMER_MODULES`.

    Silently drop entries whose file does not exist on disk - this lets
    the test stay accurate if a consumer is renamed/moved in a future
    refactor without the gate failing on the renamed file alone (the
    other gate functions still flag it via ``test_each_consumer_*``).
    """
    paths: list = []
    for rel in CONSUMER_MODULES:
        full = (PROJECT_ROOT / rel).resolve()
        if full.exists() and full.is_file():
            paths.append(full)
    return paths


def _extract_import_module_names(source: str) -> set:
    """Return the set of dotted module names referenced by
    ``import`` / ``from ... import`` statements in ``source``.

    Only static ``ast.Import`` and ``ast.ImportFrom`` nodes are
    inspected; dynamic ``__import__`` / ``importlib`` calls are
    ignored (they would require executing the module).
    """
    names: set = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _scan_for_filename(path: Path, filename: str) -> list:
    """Return ``(line_no, line_text)`` for each whole-word match of
    ``filename`` in the *executable code* of ``path`` (docstrings /
    comments / SQL-DDL comments blanked — see :func:`_code_only_text`).

    The sanctioned pinned literal ``PLAN_STATE_FILENAME: str =
    "plan_state.json"`` is skipped for ``plan_state.json`` only; see
    the mirror gate in
    ``test_no_json_state_filename_in_backend_source.py`` for the
    rationale (recorded 2026-09-14 scan-scope narrowing).
    """
    hits: list = []
    pattern = re.compile(rf"\b{re.escape(filename)}\b")
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    code_only = _code_only_text(source)
    for line_no, line in enumerate(code_only.splitlines(), start=1):
        if not pattern.search(line):
            continue
        if filename == "plan_state.json" and _PLAN_STATE_LITERAL_RE.match(line):
            continue
        hits.append((line_no, source.splitlines()[line_no - 1]))
    return hits


# ---------------------------------------------------------------------------
# The acceptance gate
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.acceptance_vp017,
]


@pytest.mark.acceptance_vp017
def test_nine_consumer_modules_identified() -> None:
    """There must be exactly 9 consumer modules in the gate.

    If this fails, the architecture has drifted - either a new
    consumer was added (extend the list) or one was removed (shrink
    the list).  Both directions require updating this static gate.
    """
    assert len(CONSUMER_MODULES) == 9, (
        f"VP-017 expects exactly 9 consumer modules; got "
        f"{len(CONSUMER_MODULES)}: {list(CONSUMER_MODULES)!r}"
    )


@pytest.mark.acceptance_vp017
def test_no_cross_consumer_aggregation_facade_imported() -> None:
    """No consumer module may import any cross-consumer aggregation
    facade module.

    This is the **static import-graph assertion** for VP-017.  Any
    import of a forbidden facade name in any of the 9 consumer
    modules is a violation.

    Implementation: walk each consumer module, parse its
    ``import`` / ``from ... import`` statements via :mod:`ast`, and
    assert the resulting dotted-module set does not intersect with
    :data:`FORBIDDEN_FACADE_MODULE_NAMES`.
    """
    violations: list = []
    for path in _consumer_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        imports = _extract_import_module_names(text)
        bad = [name for name in imports
               if any(name == forbidden
                      or name.endswith("." + forbidden)
                      for forbidden in FORBIDDEN_FACADE_MODULE_NAMES)]
        if bad:
            for forbidden_name in bad:
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)} imports "
                    f"{forbidden_name!r} (forbidden cross-consumer "
                    f"aggregation facade)"
                )
    assert not violations, (
        "Consumer modules import a forbidden cross-consumer aggregation "
        "facade.  Architecture decision point 5 forbids introducing an "
        "in-memory aggregation facade; every consumer MUST reach the "
        "state machine through one of the four repositories directly. "
        "Forbidden facade module names: "
        f"{list(FORBIDDEN_FACADE_MODULE_NAMES)!r}\n"
        + "\n".join(violations)
    )


@pytest.mark.acceptance_vp017
def test_no_five_json_path_constants_in_consumer_modules() -> None:
    """No consumer module may contain any of the 5 forbidden JSON
    state filename constants.

    This is the **JSON path constant assertion** for VP-017.  The
    five filenames match the gate in
    ``test_no_json_state_filename_in_backend_source.py``; this test
    restricts the same set to the 9 consumer modules specifically.

    Implementation: whole-word regex search per
    :func:`_scan_for_filename` over each of the 9 consumer module
    files.
    """
    violations: list = []
    for path in _consumer_paths():
        for filename in FORBIDDEN_JSON_FILENAMES:
            for line_no, line in _scan_for_filename(path, filename):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{line_no}: "
                    f"{line.strip()!r}  <-- contains {filename!r}"
                )
    assert not violations, (
        "Consumer modules still embed one or more forbidden JSON "
        "state filename constants.  Migrate to repository access "
        "(RoutingRepository / ExecutionRepository / "
        "VerificationRepository / ArtifactRepository). Forbidden "
        f"filenames: {list(FORBIDDEN_JSON_FILENAMES)!r}\n"
        + "\n".join(violations)
    )


@pytest.mark.acceptance_vp017
def test_each_consumer_module_present_on_disk() -> None:
    """Every entry in :data:`CONSUMER_MODULES` must exist on disk.

    This is a sanity check - if a consumer is renamed/moved, this
    test fails loudly so the gate can be updated to point at the new
    location.  A silent rename would let other test functions skip
    the renamed file and pass falsely.
    """
    missing: list = []
    for rel in CONSUMER_MODULES:
        full = (PROJECT_ROOT / rel).resolve()
        if not (full.exists() and full.is_file()):
            missing.append(rel)
    assert not missing, (
        f"{len(missing)}/{len(CONSUMER_MODULES)} consumer modules are "
        f"missing on disk: {missing!r}.  If a consumer was renamed "
        f"or moved, update CONSUMER_MODULES in this file."
    )
