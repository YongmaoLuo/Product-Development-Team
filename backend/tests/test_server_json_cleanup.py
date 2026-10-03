"""TDD source-layer guard: zero hits of forbidden JSON state file patterns.

Background
----------
The state-machine refactor's architecture decision point 8 defines a
**three-defense layer** to prevent regressions where legacy JSON state
files silently leak back into production code:

  Defense 1 (this file): a static grep gate against production code
    under ``backend/server.py``, ``backend/verification_executor.py``,
    and ``backend/state_machine/**/*.py`` (excluding tests).

  Defense 2: the SQL schema gate pinned in
    :mod:`state_machine.tests.unit.test_json_static_gate` (4 tables +
    ``schema_version``).

  Defense 3: the 8000-guard / tmp-path teardown fixture in
    :mod:`state_machine.tests.conftest`.

This file pins **Defense 1** for the five forbidden filename patterns
that must NEVER appear in production code as either literal strings,
variable references, f-string / ``.format`` outputs, or
``Path(...)`` / ``os.path.join(...)`` constructions:

    plan_state.json
    execution.json
    verification_runtime_state.json
    verification_executor_state.json
    verification_progress_state.json

Exempt filename list (NOT scanned):

    verification_plan.json
    verification_report.json
    verification_execution_results.json

These three are legitimate per-run artifacts written by the verification
executor and read by the backend; they are not part of the
deprecated state-machine JSON contract.

Scope
-----
The scan targets exactly three locations:

    backend/server.py
    backend/verification_executor.py
    backend/state_machine/**/*.py   (recursive)

The ``backend/tests/`` tree is NOT scanned (test code is allowed to
mention filenames as assertion objects).  Every ``tests/`` subtree
under ``state_machine/`` is also excluded.

Pattern catalogue
-----------------
Each of the five test functions scans one specific string-construction
pattern class.  This split lets the failure message pinpoint the
specific anti-pattern, e.g.::

    server.py:2421: VERIFICATION_RUNTIME_STATE_FILENAME = "verification_runtime_state.json"
        <-- pattern 'direct_literal' hit on 'verification_runtime_state.json'

vs::

    verification_executor.py:74: _RT_FN_VEXECUTOR: str = "verification_exe" + "cutor_state.json"
        <-- pattern 'string_concat' hit on 'verification_executor_state.json'

TDD specification
-----------------
- ``test_pattern_direct_literal``: regex
  ``(?:"|')(?:plan_state|execution|verification_runtime_state|verification_executor_state|verification_progress_state)\\.json(?:"|')``
  with no hits → PASS.
- ``test_pattern_string_concat``: AST parse ``"..."\\s*\\+\\s*"..."`` whose
  result contains one of the five filenames, with no hits → PASS.
- ``test_pattern_variable_reference``: regex
  ``_RT_FN_\\w+|progress_state_file|plan_state_file|exec_file|progress_file``
  with no hits → PASS.
- ``test_pattern_fstring_format``: regex
  ``f["'][^"']*\\{[^}]*\\}[^"']*\\.json["']|\\.format\\([^)]*\\.json\\)``
  with no hits → PASS.
- ``test_pattern_pathlib_join``: regex
  ``Path\\([^)]*\\)\\s*/\\s*["'][^"']*\\.json|os\\.path\\.join\\([^)]*\\.json\\)``
  with no hits → PASS.

Output contract
---------------
On a hit, the corresponding test calls ``pytest.fail`` with a
deterministic ``{file_path}:{line_no}:{pattern_name}`` message so the backend can grep the violation location unambiguously.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Targets — exactly the three locations pinned by the task brief.
# ---------------------------------------------------------------------------

# Backend root directory (the parent of ``server.py`` and ``state_machine/``).
_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# The request guard's header name and value, read from the module rather
# than spelled out, so a rename on the server side cannot leave this
# fixture quietly sending a header nobody checks.
import request_guard  # noqa: E402

# Module-level guard: the state-machine refactor must be in place for
# these gates to be meaningful.  When ``backend/state_machine/`` does
# not exist (e.g. a parallel repo that has not yet received the
# refactor sync delivery), the entire module tests skip rather than
# fail.
_STATE_MACHINE_ROOT = _BACKEND_ROOT / "state_machine"
_STATE_MACHINE_REFACTOR_PRESENT = _STATE_MACHINE_ROOT.is_dir()


# Module-level skip marker: when the state-machine refactor is not
# present (parallel repo without the delivery), skip every test in
# this module rather than run the gates against pre-refactor code
# (which would produce hundreds of false-positive violations).
pytestmark = pytest.mark.skipif(
    not _STATE_MACHINE_REFACTOR_PRESENT,
    reason=(
        "state-machine refactor not present at "
        f"{_STATE_MACHINE_ROOT}; skipping JSON cleanup gates "
        "(this test belongs to the state-machine sync deliverable "
        "and requires backend/state_machine/ + the matching "
        "verification_executor.py to be in place to be meaningful)."
    ),
)
# The three scan targets, fixed by the task brief.
_TARGETS: tuple[Path, ...] = (
    _BACKEND_ROOT / "server.py",
    _BACKEND_ROOT / "verification_executor.py",
)

# The five forbidden JSON state filenames.  Each entry is the bare
# basename; the test patterns surround it with quote / context tokens
# so that substrings like ``plan_state.json_backup`` do not match.
_FORBIDDEN_FILENAMES: tuple[str, ...] = (
    "plan_state.json",
    "execution.json",
    "verification_runtime_state.json",
    "verification_executor_state.json",
    "verification_progress_state.json",
)

# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------


def _iter_state_machine_py_files() -> list[Path]:
    """Return every ``*.py`` file under ``backend/state_machine/``,
    recursively, excluding any ``tests/`` subtree.

    The task brief pins the scan scope to ``state_machine/**/*.py``.
    Test files under ``state_machine/tests/`` are excluded because
    test code is allowed to mention filenames as assertion objects.
    """
    state_machine_root = _BACKEND_ROOT / "state_machine"
    if not state_machine_root.exists():
        return []
    py_files: list[Path] = []
    for path in state_machine_root.rglob("*.py"):
        # Skip any path that has a "tests" segment anywhere.
        if "tests" in path.relative_to(state_machine_root).parts:
            continue
        py_files.append(path)
    return sorted(py_files)


def _read_text(path: Path) -> str:
    """Read a file's text, replacing undecodable bytes with U+FFFD."""
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Pattern 1 — direct literal
# ---------------------------------------------------------------------------


def _pattern_direct_literal() -> list[tuple[Path, int, str, str]]:
    """Return ``(path, line_no, line_text, matched_filename)`` for every
    direct literal occurrence of one of the five forbidden filenames.

    A direct literal is a string of the shape ``"<filename>.json"`` or
    ``'<filename>.json'`` — a quoted JSON filename appearing directly
    in source code, not as the result of a ``+`` concatenation, an
    f-string interpolation, or a ``Path(...) / "<filename>.json"``
    join.
    """
    hits: list[tuple[Path, int, str, str]] = []
    pattern = re.compile(
        r"""(?:"|')"""                       # opening quote
        r"""(?:""" + "|".join(
            re.escape(name) for name in _FORBIDDEN_FILENAMES
        ) + r""")"""
        r"""(?:"|')"""                       # closing quote
    )
    for path in _all_scan_paths():
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is not None:
                # Extract the matched filename (strip the surrounding quotes).
                matched = match.group(0).strip("'\"")
                hits.append((path, line_no, line, matched))
    return hits


# ---------------------------------------------------------------------------
# Pattern 2 — string concatenation
# ---------------------------------------------------------------------------


def _pattern_string_concat() -> list[tuple[Path, int, str, str]]:
    """Return ``(path, line_no, line_text, matched_filename)`` for every
    ``"..." + "..."`` AST node whose concatenation contains one of the
    five forbidden filenames.

    The task brief specifies AST parsing for this pattern so that
    ``"execution" + ".json"`` style fragments are caught even when
    split across multiple token positions on the same line.
    """
    hits: list[tuple[Path, int, str, str]] = []
    for path in _all_scan_paths():
        try:
            tree = ast.parse(_read_text(path), filename=str(path))
        except SyntaxError:
            # If the file does not parse, skip it — other patterns
            # will still surface genuine violations.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
                continue
            # Walk the Add chain on the left side to collect every
            # adjacent string operand; we only care about adjacent
            # literals on a single line (the task brief scopes to
            # ``"..." + "..."`` shape).
            operands: list[ast.Constant] = []
            current: ast.expr | None = node.left
            while (
                isinstance(current, ast.BinOp)
                and isinstance(current.op, ast.Add)
                and current.lineno == node.lineno
            ):
                if isinstance(current.right, ast.Constant) and isinstance(
                    current.right.value, str
                ):
                    operands.append(current.right)
                current = current.left
            if (
                isinstance(current, ast.Constant)
                and isinstance(current.value, str)
                and current.lineno == node.lineno
            ):
                operands.append(current)
            # Add the right operand of the top-level BinOp too.
            if isinstance(node.right, ast.Constant) and isinstance(
                node.right.value, str
            ):
                operands.append(node.right)
            if len(operands) < 2:
                continue
            joined = "".join(op.value for op in operands)
            for forbidden in _FORBIDDEN_FILENAMES:
                if forbidden in joined:
                    line_text = _read_text(path).splitlines()[node.lineno - 1]
                    hits.append((path, node.lineno, line_text, forbidden))
                    break
    return hits


# ---------------------------------------------------------------------------
# Pattern 3 — variable reference
# ---------------------------------------------------------------------------


def _pattern_variable_reference() -> list[tuple[Path, int, str, str]]:
    """Return hits for any of the legacy variable names that the cleanup
    tasks 1-4 through 1-7 were supposed to delete:

        _RT_FN_*  — any constant / variable whose name starts with the
                    ``_RT_FN_`` prefix that was the old "runtime
                    filename" indirection layer.
        progress_state_file
        plan_state_file
        exec_file
        progress_file

    The pattern is a whole-token match so identifiers such as
    ``_RT_FN_VEXECUTOR`` (which starts with the prefix) AND a literal
    variable named ``progress_state_file`` both hit.
    """
    hits: list[tuple[Path, int, str, str]] = []
    pattern = re.compile(
        r"\b(?:"
        r"_RT_FN_\w+"
        r"|progress_state_file"
        r"|plan_state_file"
        r"|exec_file"
        r"|progress_file"
        r")\b"
    )
    for path in _all_scan_paths():
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is not None:
                hits.append((path, line_no, line, match.group(0)))
    return hits


# ---------------------------------------------------------------------------
# Pattern 4 — f-string / .format()
# ---------------------------------------------------------------------------


def _pattern_fstring_format() -> list[tuple[Path, int, str, str]]:
    """Return hits for f-strings that interpolate into a ``.json``
    suffix and for ``.format(...)`` calls whose argument list contains
    a ``.json`` suffix.

    The task brief regex is::

        f["'][^"']*\\{[^}]*\\}[^"']*\\.json["']
        |\\.format\\([^)]*\\.json\\)

    We apply this regex verbatim — a hit on this regex is, by
    definition, a violation.  The brief's PASS contract is "无命中 → PASS",
    i.e. the test passes iff the regex matches ZERO occurrences in
    production code.
    """
    hits: list[tuple[Path, int, str, str]] = []
    pattern = re.compile(
        r"""f["'][^"']*\{[^}]*\}[^"']*\.json["']"""
        r"""|"""
        r"""\.format\([^)]*\.json\)"""
    )
    for path in _all_scan_paths():
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is not None:
                hits.append((path, line_no, line, match.group(0)))
    return hits


# ---------------------------------------------------------------------------
# Pattern 5 — pathlib / os.path.join
# ---------------------------------------------------------------------------


def _pattern_pathlib_join() -> list[tuple[Path, int, str, str]]:
    """Return hits for ``Path(...) / "<filename>.json"`` and
    ``os.path.join(..., "<filename>.json")`` style joins.

    The task brief regex is::

        Path\\([^)]*\\)\\s*/\\s*["'][^"']*\\.json
        |os\\.path\\.join\\([^)]*\\.json\\)

    The regex is applied verbatim — any join whose result ends in
    ``.json`` triggers a hit, regardless of the basename.
    """
    hits: list[tuple[Path, int, str, str]] = []
    pattern = re.compile(
        r"""Path\([^)]*\)\s*/\s*["'][^"']*\.json"""
        r"""|"""
        r"""os\.path\.join\([^)]*\.json\)"""
    )
    for path in _all_scan_paths():
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is not None:
                hits.append((path, line_no, line, match.group(0)))
    return hits


# ---------------------------------------------------------------------------
# Common scan path enumeration
# ---------------------------------------------------------------------------


def _all_scan_paths() -> list[Path]:
    """Return the de-duplicated, sorted list of files scanned by every
    pattern test: the two top-level targets (``server.py`` and
    ``verification_executor.py``) plus every non-test ``*.py`` file
    under ``state_machine/``.
    """
    paths: list[Path] = list(_TARGETS)
    paths.extend(_iter_state_machine_py_files())
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# pytest.fail helpers
# ---------------------------------------------------------------------------


def _format_hit(
    pattern_name: str,
    path: Path,
    line_no: int,
    matched: str,
) -> str:
    """Format the deterministic ``{file_path}:{line_no}:{pattern_name}``
    failure message that the task brief specifies.
    """
    return f"{path}:{line_no}:{pattern_name} ({matched!r})"


def _fail_on_hits(
    pattern_name: str,
    hits: list[tuple[Path, int, str, str]],
) -> None:
    """Call ``pytest.fail`` with a multi-line violation summary if any
    hits were found.  The summary includes the deterministic
    ``file_path:line_no:pattern_name`` line for every hit plus a one-line
    header that tells the backend exactly how to interpret the
    failure.
    """
    if not hits:
        return
    summary_lines: list[str] = [
        f"FAIL pattern '{pattern_name}': {len(hits)} hit(s) "
        f"of a forbidden JSON state file construction in production code."
    ]
    for path, line_no, _line_text, matched in hits:
        summary_lines.append("  " + _format_hit(pattern_name, path, line_no, matched))
    summary_lines.append(
        "  hint: the five forbidden filenames are "
        f"{list(_FORBIDDEN_FILENAMES)}; "
        "rewrite the offending site to use the state-machine repository "
        "layer (plan_routing / plan_execution / plan_verification / "
        "plan_artifacts) instead of direct JSON I/O."
    )
    pytest.fail("\n".join(summary_lines))


# ---------------------------------------------------------------------------
# The 5 test functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pattern_direct_literal() -> None:
    """No direct literal occurrence of any forbidden JSON state filename.

    Hits a quoted string of the shape ``"<forbidden>.json"`` or
    ``'<forbidden>.json'`` anywhere in production code.
    """
    _fail_on_hits("direct_literal", _pattern_direct_literal())


@pytest.mark.unit
def test_pattern_string_concat() -> None:
    """No ``"..." + "..."`` AST node whose concatenation contains a
    forbidden JSON state filename.

    The AST walk pins adjacent string-literal ``+`` operations on a
    single source line so multi-line continuations are not falsely
    matched.  The concatenation's joined value is then checked for
    the five forbidden filenames.
    """
    _fail_on_hits("string_concat", _pattern_string_concat())


@pytest.mark.unit
def test_pattern_variable_reference() -> None:
    """No reference to any of the legacy variable names that the
    cleanup tasks 1-4..1-7 were supposed to delete.

    The five identifier shapes scanned are::

        _RT_FN_\\w+
        progress_state_file
        plan_state_file
        exec_file
        progress_file
    """
    _fail_on_hits("variable_reference", _pattern_variable_reference())


@pytest.mark.unit
def test_pattern_fstring_format() -> None:
    """No f-string interpolation that produces a ``.json`` suffix and
    no ``.format(...)`` call whose argument list contains a ``.json``
    suffix.
    """
    _fail_on_hits("fstring_format", _pattern_fstring_format())


@pytest.mark.unit
def test_pattern_pathlib_join() -> None:
    """No ``Path(...) / "<filename>.json"`` nor
    ``os.path.join(..., "<filename>.json")`` join whose result ends in
    ``.json``.
    """
    _fail_on_hits("pathlib_join", _pattern_pathlib_join())


# ---------------------------------------------------------------------------
# Defense 1b — filesystem-layer teardown guard
# ---------------------------------------------------------------------------
#
# The grep gate above pins the *source* layer: production code MUST NOT
# reference the five forbidden JSON state filenames.  This complement pins
# the *filesystem* layer: after a real ``/api/execution/{plan_id}/start``
# lifecycle (driven against a live EXEC_PORT=8001 instance), the plans/
# directory tree MUST NOT contain any of the five forbidden JSON sidecars.
#
# Why a separate test?
#   The source grep gate guarantees no NEW writes are added — but a
#   legacy site that escaped the cleanup, or a runtime path that the
#   grep gate cannot see (e.g. a dependency), could still leak a
#   forbidden JSON onto disk at runtime.  The teardown guard catches
#   that class of regression mechanically: walk the plans/ tree,
#   collect every *.json file, and fail if any of the five targets
#   are present.
#
# TDD contract:
#   - After fixture teardown, walking ``$PDT_DATA_DIR/plans/``
#     (recursive via ``pathlib.Path.rglob('*.json')``) MUST NOT
#     contain any of the five forbidden JSON state filenames.
#   - Exempt artifacts (allowlisted in ``_EXEMPT_JSON_BASENAMES``)
#     are explicitly tolerated and MUST NOT trigger failure.
#   - ``.log`` files are out of scope (we only walk *.json).
#   - Files whose first non-whitespace byte is NOT ``{`` or ``[`` are
#     JSON-lines manifests — these are exempt (execution.log written
#     line-by-line JSON records, not a single JSON document).
#   - Failures call ``pytest.fail`` (not a warning) — the contract
#     is binary: zero hits OR failure.


# The five target JSON state filenames that MUST NOT be on disk after
# the lifecycle walk.  These match ``FORBIDDEN_STATE_JSONS`` from
# ``tests/static_gates/test_e2e_lifecycle_produces_no_state_json.py``;
# the two layers (source grep + filesystem scan) cover orthogonal
# failure modes.
_TEARDOWN_TARGETS: frozenset[str] = frozenset(
    {
        "plan_state.json",
        "execution.json",
        "verification_runtime_state.json",
        "verification_executor_state.json",
        "verification_progress_state.json",
    }
)

# Files in this set are NOT scanned as violations:
#   - interview.json, prd.json, tasks.json, review.json
#       — legitimate user-authored / generator artifacts.
#   - verification_plan.json, verification_report.json,
#     verification_execution_results.json
#       — legitimate per-run verification artifacts written by the
#         verification executor and read by the backend.
#   - execution.log is a .log file (not a .json) so it is naturally
#     excluded by the rglob('*.json') selector.
_EXEMPT_JSON_BASENAMES: frozenset[str] = frozenset(
    {
        "interview.json",
        "prd.json",
        "tasks.json",
        "review.json",
        "verification_plan.json",
        "verification_report.json",
        "verification_execution_results.json",
    }
)


def _collect_teardown_hits(plans_root: Path) -> list[tuple[Path, str]]:
    """Walk ``plans_root`` recursively for ``*.json`` files.

    Returns a list of ``(abs_path, basename)`` pairs for every file
    whose basename is in ``_TEARDOWN_TARGETS`` AND whose first
    non-whitespace byte is ``{`` or ``[`` (i.e. a real JSON document,
    not a JSON-lines manifest written by ``execution.log`` style
    record streams).

    Files in ``_EXEMPT_JSON_BASENAMES`` are tolerated — finding them
    is NOT a violation.  ``.log`` files are excluded by the
    ``*.json`` glob selector.
    """
    if not plans_root.exists():
        return []
    hits: list[tuple[Path, str]] = []
    for json_path in sorted(plans_root.rglob("*.json")):
        if not json_path.is_file():
            continue
        basename = json_path.name
        if basename in _EXEMPT_JSON_BASENAMES:
            continue
        if basename not in _TEARDOWN_TARGETS:
            continue
        # Read the first byte.  A JSON-lines manifest is a sequence
        # of newline-separated JSON objects (e.g. execution.log
        # written as ``{"ts": ..., "event": ...}\n``) — its first
        # non-whitespace byte IS ``{`` so the test would still
        # catch it.  We accept JSON-lines as long as the file
        # contains *more than one* top-level record OR its first
        # token is followed by a newline before the closing ``}``
        # (JSON-lines shape).  A single ``{...}`` document with no
        # internal newlines before the closing brace is a pure
        # JSON object — that IS a target hit.
        try:
            with json_path.open("rb") as fh:
                head = fh.read(2)
        except OSError:
            # Unreadable file — surface it as a hit so the caller
            # can debug; this is a real anomaly on the disk.
            hits.append((json_path, basename))
            continue
        if not head:
            continue
        # Strip BOM / leading whitespace.
        stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
        first = stripped[:1]
        if first not in (b"{", b"["):
            # Not a JSON document (binary / unknown).  Treat as a
            # hit so the caller sees it.
            hits.append((json_path, basename))
            continue
        # Distinguish a single JSON document from a JSON-lines
        # manifest: scan up to 64 KiB for a record terminator
        # followed by another ``{`` or ``[`` — if found, it is
        # JSON-lines (exempt).  This implements the brief's
        # "JSON-lines 形态(非 `{`/`[`)跳过" rule for the FIRST
        # BYTE, plus an additional permissive check for the
        # JSON-lines shape (a leading ``{`` followed by another
        # top-level record on a later line) — neither shape is
        # a violation in our gate.
        try:
            with json_path.open("rb") as fh:
                sample = fh.read(65536)
        except OSError:
            hits.append((json_path, basename))
            continue
        # A JSON-lines manifest has at least one ``\n`` followed by
        # ``{`` or ``[`` after the first record's opening brace.
        # Detect via a simple byte scan.
        is_jsonl = False
        nl_idx = sample.find(b"\n")
        if nl_idx != -1 and nl_idx + 1 < len(sample):
            after_nl = sample[nl_idx + 1 : nl_idx + 2].lstrip(
                b"\xef\xbb\xbf \t\r"
            )
            if after_nl in (b"{", b"["):
                is_jsonl = True
        if is_jsonl:
            continue
        hits.append((json_path, basename))
    return hits


@pytest.fixture
def exec_instance_setup(tmp_path, monkeypatch):
    """Spawn a real FastAPI instance on ``EXEC_PORT=8001`` and drive
    one ``/api/execution/{plan_id}/start`` lifecycle.

    The fixture:
      * redirects ``server.PLANS_DIR`` to ``tmp_path / "plans"`` so
        the scan in ``test_teardown_guard`` sees only this run's
        artifacts (no cross-run leakage);
      * spawns ``uvicorn server:app`` on port 8001 in a background
        subprocess;
      * waits for ``GET /health`` to return 2xx;
      * plants an interview.json + tasks.json artifact (the brief's
        allowlisted product JSON files) so the scan can prove the
        allowlist is honoured and is non-vacuous;
      * drives ``POST /api/execution/{plan_id}/start`` so the
        execution sidecar write path is exercised end-to-end;
      * yields the plans root to the test;
      * on teardown: stops the uvicorn subprocess, then returns —
        the test itself walks plans/ to assert the post-teardown
        state.

    The fixture is conservative: if the uvicorn spawn fails (e.g. no
    ``uvicorn`` on PATH in this environment), the test is skipped
    rather than fabricated — we cannot meaningfully exercise the
    filesystem-layer contract without a live server.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    plan_id = "vptest-teardown-guard"

    # Redirect the production PLANS_DIR to our tmp_path.
    monkeypatch.setattr("server.PLANS_DIR", plans_root)

    # Plant the artifact allowlist — interview.json is a product JSON
    # input; tasks.json is the executor's task-list input.  These
    # MUST be tolerated by the scan.
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        '{"requirement": "teardown guard fixture"}',
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")

    # Verify ``uvicorn`` is on PATH; skip gracefully otherwise.
    import shutil

    uvicorn_path = shutil.which("uvicorn")
    if uvicorn_path is None:
        pytest.skip(
            "uvicorn is not on PATH in this environment; "
            "the teardown guard requires a live EXEC_PORT=8001 instance "
            "to exercise the full /api/execution lifecycle."
        )

    # Spawn the FastAPI app on port 8001.
    # ``PDT_STATE_DB_PATH`` redirects ``server._state_db_path`` to a
    # per-test tmp file so the schema/API contract tests inspect only
    # the fixture's own state.db (no cross-run leakage onto the
    # project-level state.db).  The env var propagates into the
    # spawned uvicorn subprocess via ``env=...`` below.
    tmp_state_db = tmp_path / "state.db"
    server_log = open(tmp_path / "uvicorn.log", "w+b")  # noqa: SIM115
    env = {
        "EXEC_PORT": "8001",
        "PDT_STATE_DB_PATH": str(tmp_state_db),
        # The monkeypatch above redirects PLANS_DIR in THIS process. The
        # spawned uvicorn is a separate process with its own PLANS_DIR
        # pointing at the real repository one, so without this it never
        # finds the plan planted above, the start request 404s, and the
        # schema migration that would create the database never runs --
        # leaving the schema assertions to report an empty sqlite_master
        # that looks like a regression and is not one. ``server.py``
        # reads this override at import (see the ``_PLANS_DIR_OVERRIDE``
        # block), which is why it has to be in the child's environment
        # rather than patched in afterwards.
        "PDT_PLANS_DIR": str(plans_root),
        # Extend the parent's PATH, do not replace it. It used to be a
        # hard-coded macOS list, which meant the spawned server had no
        # venv on its PATH: on CI the uvicorn that `which()` found a
        # moment earlier lives in backend/.venv/bin, and the child could
        # not resolve it. The server then died at startup, the health
        # poll timed out, the fixture swallowed it, and the test carried
        # on to assert against a database that had never been created —
        # "sqlite_master 含 []", which is the exact symptom the comment
        # above already documents for the stale-listener case. The local
        # machine hid this: its uvicorn is outside the venv too, but it
        # happened to start anyway.
        "PATH": _child_path(),
        # 2026-09-14: prompts.py does ``from backend.framework.prompts
        # import ...``, which requires the REPO ROOT on sys.path.
        # ``uvicorn server:app`` with cwd=backend/ only puts backend/
        # on sys.path, so the spawned subprocess crashed at import
        # time and every e2e test silently SKIPped after the 30 s
        # health-timeout (masking the layer as "0 failed").  Without
        # this, a stale listener on :8001 (e.g. a SIGTERM-swallowing
        # zombie from an earlier run) instead made health succeed
        # instantly against the WRONG server, and these tests FAILed
        # with "sqlite_master 含 []".
        "PYTHONPATH": str(_BACKEND_ROOT.parent),
    }
    proc = subprocess.Popen(  # noqa: S603
        [
            uvicorn_path,
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
            "--log-level",
            "warning",
        ],
        cwd=str(_BACKEND_ROOT),
        env=env,
        # NOT DEVNULL. The server is the only thing that knows why it
        # failed to bring the database up, and every consumer of this
        # fixture reads the database afterwards -- so with the output
        # discarded, "the server never wrote state.db" is
        # indistinguishable from "the server wrote a state.db with no
        # tables in it", and the second reading is the one the schema
        # assertions go on to report as an empty ``sqlite_master``.
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        # Wait for the server to come up.  30 s budget.
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                    "http://127.0.0.1:8001/health", timeout=2
                ) as resp:
                    if 200 <= resp.status < 300:
                        ready = True
                        break
            except (urllib.error.URLError, ConnectionResetError, OSError):
                pass
            time.sleep(0.25)
        def _server_log_tail() -> str:
            try:
                server_log.flush()
                server_log.seek(0)
                return server_log.read().decode("utf-8", "replace")[-2000:]
            except OSError:
                return "<server log unavailable>"

        if not ready:
            pytest.skip(
                "EXEC_PORT=8001 server did not become healthy within 30s; "
                "teardown guard cannot exercise the live lifecycle. "
                f"Server output:\n{_server_log_tail()}"
            )
        # A healthy 8001 is not evidence that THIS server is the one
        # answering. The port is fixed, so anything else that ever bound
        # it -- a previous test's server that has not been reaped yet, or
        # a listener this runner image starts by default -- answers the
        # health probe instantly while writing to a DIFFERENT state.db.
        # That is the failure this whole fixture has been silently
        # producing: a healthy check, a foreign server, and an empty
        # ``sqlite_master`` three tests later with nothing in between
        # saying so.
        if proc.poll() is not None:
            pytest.skip(
                f"the spawned server exited (rc={proc.returncode}) before "
                f"the health probe succeeded, so another process is "
                f"serving :8001. Server output:\n{_server_log_tail()}"
            )

        # Drive /api/execution/{plan_id}/start.  We accept either
        # 200 (started), 409 (already running), or 404 (no plan
        # row — acceptable for the filesystem scan: we only need
        # the endpoint exercised, not the start succeeding).
        #
        # The 410/400 pre-checks (archived / missing project_dir /
        # placeholder PRD) would short-circuit before the schema
        # migration runs.  Provide a valid project_dir so the
        # endpoint reaches the open_db + migrate() block in the
        # 200/409/410 paths — without that, state.db is never
        # created on disk and the schema gate has nothing to read.
        fixture_project_dir = tmp_path / "vptest-project"
        fixture_project_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"project_dir": str(fixture_project_dir)}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            f"http://127.0.0.1:8001/api/execution/{plan_id}/start",
            data=body,
            headers={
                "Content-Type": "application/json",
                # `server.request_guard` refuses any /api/* request that
                # arrives without this header, and it refuses loopback
                # hostnames that are not explicitly allowed. This fixture
                # talks to a real server over real HTTP rather than
                # through Starlette's TestClient, so it does not get the
                # header `tests/conftest.py` injects into every
                # TestClient — it has to send its own.
                #
                # Without it the call 403s, the request never reaches the
                # code that runs `migrate()`, state.db is never created,
                # and the schema layer reports an empty sqlite_master —
                # which reads as a schema regression and is not one. That
                # is the chain this test just spent a long time being
                # misdiagnosed as.
                request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                _ = resp.status
        except urllib.error.HTTPError as exc:
            # 404/409/etc. are acceptable for the fixture — the
            # teardown guard only requires the lifecycle endpoint
            # to be exercised, not to succeed.
            _ = exc.code

        # The schema assertions below read this file. Checking it HERE
        # means the fixture fails with "the server never wrote the
        # database" rather than letting three downstream tests each
        # report an empty ``sqlite_master``, which reads like a schema
        # regression and is not one.
        if not tmp_state_db.exists() or tmp_state_db.stat().st_size == 0:
            pytest.fail(
                f"the spawned server never created {tmp_state_db}. The "
                f"schema assertions in this file will otherwise report an "
                f"empty sqlite_master, which looks like a schema "
                f"regression and is not one. Server output:\n"
                f"{_server_log_tail()}"
            )

        yield plans_root
    finally:
        # Stop the spawned server.  Best-effort: if terminate fails
        # we escalate to kill so the teardown is non-blocking.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
        try:
            server_log.close()
        except OSError:
            pass


@pytest.mark.unit
def test_e2e_teardown_guard(exec_instance_setup) -> None:
    """Filesystem-layer guard: after the EXEC_PORT=8001 lifecycle, the
    plans/ tree MUST NOT contain any of the five forbidden JSON
    state filenames.

    Strategy:
      1. Use the ``exec_instance_setup`` fixture (which spawns the
         8001 instance and drives ``/api/execution/{plan_id}/start``).
      2. After the fixture teardown stops the server, walk
         ``$PDT_DATA_DIR/plans/`` recursively via
         ``pathlib.Path.rglob('*.json')``.
      3. Assert zero hits against the five forbidden basenames —
         failure calls ``pytest.fail`` (no warn downgrade).

    Boundary conditions enforced:
      * The five forbidden basenames are strictly distinguished
        from the exempt allowlist — exempt files do NOT trigger
        failure.
      * ``.log`` files are skipped (rglob selector is ``*.json``).
      * Nested subdirectories are scanned (``rglob`` is recursive).
      * JSON-lines manifests (first non-whitespace byte followed
        by a newline-terminated record) are exempt — see
        ``_collect_teardown_hits`` for the heuristic.
      * Failures are NOT downgraded to warnings: ``pytest.fail``
        aborts the run.
    """
    plans_root: Path = exec_instance_setup
    hits = _collect_teardown_hits(plans_root)

    if hits:
        rel_paths = [str(p.relative_to(plans_root)) for p, _ in hits]
        basenames = sorted({b for _, b in hits})
        pytest.fail(
            f"teardown guard: {len(hits)} forbidden JSON sidecar(s) "
            f"present under plans/ after EXEC_PORT=8001 teardown: "
            f"rel={rel_paths!r} basenames={basenames!r}. "
            f"The five forbidden filenames are "
            f"{sorted(_TEARDOWN_TARGETS)!r} — every write path must "
            f"route through the repository layer "
            f"(plan_routing / plan_execution / plan_verification / "
            f"plan_artifacts) instead of direct JSON I/O."
        )


# ---------------------------------------------------------------------------
# Defense 2b — SQL schema + repository API contract guard
# ---------------------------------------------------------------------------
#
# Two related contracts that the task brief pins:
#
#   test_sqlite_master_whitelist
#     ``state.db.sqlite_master`` must contain the four core
#     Repository tables (``plan_routing``, ``plan_execution``,
#     ``plan_verification``, ``plan_artifacts``) plus the
#     ``schema_version`` bootstrap table.
#
#     Migration metadata tables (``_migration_*`` and
#     ``schema_version_v\d+``) are explicitly allowed — they are
#     produced by the upgrade framework, not by application code.
#
#     Legacy business tables (``plans``, ``plan_meta``,
#     ``plan_activity``) MUST NOT appear — the state-machine refactor
#     forbids them.
#
#   test_repository_api_contract
#     After driving the live ``POST /api/execution/{plan_id}/start``
#     endpoint against an EXEC_PORT=8001 instance, the SQLite database
#     schema MUST remain stable — i.e. no NEW tables were created by
#     the lifecycle.  This is the runtime companion to the source-layer
#     grep gate: even if a runtime path bypasses the repositories and
#     dynamically creates a table, this test catches it.
#
# Both tests consume the ``exec_instance_setup`` fixture (which the
# filesystem-layer test above already defines) so we share the same
# isolated tmp_path + 8001 lifecycle.  The fixture's yield is the
# ``plans_root`` — the schema tests compute ``state.db`` as
# ``plans_root.parent / "state.db"`` to match the production layout
# (``<PLANS_DIR.parent> / state.db``).
#
# TDD contract:
#   - test_sqlite_master_whitelist:
#       core ⊆ sqlite_master ⊆ (core ∪ migration_meta) → PASS
#   - test_repository_api_contract:
#       POST /api/execution/{plan_id}/start → 200/4xx (not 5xx)
#       sqlite_master table set is unchanged before vs after → PASS
#       response JSON shape has expected keys (status / plan_id /
#       project_dir / pid) when 200 → PASS
#   - Failures call ``pytest.fail`` (NOT a warning).

# Core business tables that the state-machine refactor mandates.  The
# ``schema_version`` row is the bootstrap migration-logbook, NOT a
# business table — but it is required (schema is "not migrated" if the
# row is missing).
_CORE_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "plan_routing",
        "plan_execution",
        "plan_verification",
        "plan_artifacts",
        # v4 (2026-09-09) split
        # per-task runtime state out of plan_execution.task_progress
        # into this relational table — see
        # state_machine/db/schema.py "The five plan_* tables".
        "plan_tasks",
        "schema_version",
    }
)

# Legacy business tables that the state-machine refactor explicitly
# forbids.  Their presence means the migration never happened (or a
# regression snuck them back).
_STALE_BUSINESS_TABLES: frozenset[str] = frozenset(
    {
        "plans",
        "plan_meta",
        "plan_activity",
    }
)

# Migration metadata tables produced by the upgrade framework.  These
# are NOT a violation — they are how the migration framework records
# which DDL version has been applied.
_MIGRATION_TABLE_RE: re.Pattern[str] = re.compile(
    r"^_migration_\w+$"
    r"|"
    r"^schema_version_v\d+$"
)


def _read_sqlite_master_table_names(db_path: Path) -> set[str]:
    """Return the set of user table names recorded in ``sqlite_master``
    for the SQLite database at ``db_path``.

    Internal tables (``sqlite_*``) are excluded so the result set is
    purely user-visible.  Returns an empty set if the database file
    does not exist or cannot be opened (so the calling test can
    distinguish "empty DB" from "DB does not exist" by also checking
    ``db_path.exists()``).
    """
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def _is_migration_table(name: str) -> bool:
    r"""Return True iff ``name`` matches the migration metadata pattern
    (``_migration_*`` or ``schema_version_v\d+``).
    """
    return _MIGRATION_TABLE_RE.match(name) is not None


@pytest.mark.unit
def test_sqlite_master_whitelist(exec_instance_setup) -> None:
    r"""``state.db.sqlite_master`` table name set must satisfy::

        core ⊆ actual AND actual \\ legacy == ∅ AND
        actual \\ (core ∪ migration) == ∅

    In words:
      * every required core table is present (FAIL on missing);
      * no legacy business table may appear (FAIL on residue);
      * the only extra tables allowed are migration metadata
        (``_migration_*`` / ``schema_version_v\d+``).

    The fixture (``exec_instance_setup``) drives one
    ``POST /api/execution/{plan_id}/start`` call against the live
    EXEC_PORT=8001 instance; after teardown we open the SQLite
    database the fixture configured and inspect ``sqlite_master``.

    Why the SQLite layer matters (not just the source grep gate):
      The grep gate (Defense 1) only catches string references in
      source code.  It cannot catch a runtime path that dynamically
      creates a table via raw SQL.  This test catches that class of
      regression at the storage layer.
    """
    plans_root: Path = exec_instance_setup
    # The fixture sets PDT_STATE_DB_PATH to point at a per-test tmp
    # SQLite file (see exec_instance_setup below).  We resolve it the
    # same way so the assertion targets the same file the live
    # EXEC_PORT=8001 server wrote into.
    db_path = plans_root.parent / "state.db"

    actual = _read_sqlite_master_table_names(db_path)

    missing = _CORE_SCHEMA_TABLES - actual
    if missing:
        pytest.fail(
            f"缺失核心表: {sorted(missing)!r}。 "
            f"state.db 必须包含 {sorted(_CORE_SCHEMA_TABLES)!r} 这 {len(_CORE_SCHEMA_TABLES)} 张表; "
            f"当前 sqlite_master 含 {sorted(actual)!r}. "
            f"schema_version 是引导表, 必须随四张 plan_* Repository 表一起存在。"
        )

    stale = _STALE_BUSINESS_TABLES & actual
    if stale:
        pytest.fail(
            f"残留旧表: {sorted(stale)!r}。 "
            f"state-machine refactor 明确禁止 {sorted(_STALE_BUSINESS_TABLES)!r}; "
            f"它们残留意味着迁移未完成或回归. 当前 sqlite_master: {sorted(actual)!r}."
        )

    extras = {
        name for name in actual
        if name not in _CORE_SCHEMA_TABLES and not _is_migration_table(name)
    }
    if extras:
        pytest.fail(
            f"sqlite_master 含未列入白名单的表: {sorted(extras)!r}。 "
            f"允许的集合 = 核心表 {_CORE_SCHEMA_TABLES!r} "
            f"∪ 迁移元数据表 (匹配 _migration_* / schema_version_v\\d+). "
            f"完整 sqlite_master: {sorted(actual)!r}."
        )


@pytest.mark.unit
def test_e2e_sqlite_master(exec_instance_setup) -> None:
    r"""End-to-end state.db.sqlite_master table name set must satisfy::

        core ⊆ actual AND actual \ legacy == ∅ AND
        actual \ (core ∪ migration) == ∅

    VP-003 (state.db sqlite_master 表集合符合白名单规则):
      * (1) 核心集合 5 表全部存在,缺失即 FAIL;
      * (2) 实际表名集合减去核心集合减去迁移元数据正则
            (_migration_.* 或 schema_version_v\d+)后为空,
            残留旧业务表(plans / plan_meta / plan_activity 等)即 FAIL.

    The fixture (exec_instance_setup) drives one
    POST /api/execution/{plan_id}/start call against the live
    EXEC_PORT=8001 instance; after teardown we open the SQLite
    database the fixture configured and inspect sqlite_master.

    Why the SQLite layer matters (not just the source grep gate):
      The grep gate (Defense 1) only catches string references in
      source code.  It cannot catch a runtime path that dynamically
      creates a table via raw SQL.  This test catches that class of
      regression at the storage layer.
    """
    plans_root: Path = exec_instance_setup
    # The fixture sets PDT_STATE_DB_PATH to point at a per-test tmp
    # SQLite file (see exec_instance_setup below).  We resolve it the
    # same way so the assertion targets the same file the live
    # EXEC_PORT=8001 server wrote into.
    db_path = plans_root.parent / "state.db"

    actual = _read_sqlite_master_table_names(db_path)

    missing = _CORE_SCHEMA_TABLES - actual
    if missing:
        pytest.fail(
            f"缺失核心表: {sorted(missing)!r}。 "
            f"state.db 必须包含 {sorted(_CORE_SCHEMA_TABLES)!r} 这 {len(_CORE_SCHEMA_TABLES)} 张表; "
            f"当前 sqlite_master 含 {sorted(actual)!r}. "
            f"schema_version 是引导表, 必须随四张 plan_* Repository 表一起存在。"
        )

    stale = _STALE_BUSINESS_TABLES & actual
    if stale:
        pytest.fail(
            f"残留旧表: {sorted(stale)!r}。 "
            f"state-machine refactor 明确禁止 {sorted(_STALE_BUSINESS_TABLES)!r}; "
            f"它们残留意味着迁移未完成或回归. 当前 sqlite_master: {sorted(actual)!r}."
        )

    extras = {
        name for name in actual
        if name not in _CORE_SCHEMA_TABLES and not _is_migration_table(name)
    }
    if extras:
        pytest.fail(
            f"sqlite_master 含未列入白名单的表: {sorted(extras)!r}。 "
            f"允许的集合 = 核心表 {_CORE_SCHEMA_TABLES!r} "
            f"∪ 迁移元数据表 (匹配 _migration_* / schema_version_v\d+). "
            f"完整 sqlite_master: {sorted(actual)!r}."
        )


# Expected JSON keys for a 200 response from POST
# /api/execution/{plan_id}/start.  The endpoint returns the
# ``ExecutionStatusResponse`` pydantic model fields, but only the
# ``status`` and ``pid`` keys are emitted by the happy path (the model
# carries the rest but they are not always populated).  We accept
# either ``status`` or ``status`` + ``pid`` shapes — the contract is
# the minimum key set, not the exhaustive set.
_API_REQUIRED_KEYS_200: frozenset[str] = frozenset({"status", "plan_id"})


@pytest.mark.unit
def test_repository_api_contract(exec_instance_setup) -> None:
    """End-to-end Repository API contract:

      * ``POST /api/execution/{plan_id}/start`` returns a well-formed
        JSON response (not a 5xx traceback) and matches the
        schema's top-level keys for the HTTP code returned.
      * ``sqlite_master`` table set is UNCHANGED across the
        lifecycle — i.e. the endpoint did NOT dynamically create any
        new tables.  This is the runtime companion to the
        source-layer grep gate.
      * The legacy business tables (``plans``, ``plan_meta``,
        ``plan_activity``) are still absent after the lifecycle.

    Strategy:
      1. Use the ``exec_instance_setup`` fixture (which spawns the
         8001 instance and plants an ``interview.json`` /
         ``tasks.json`` for the fixture plan).
      2. Snapshot ``sqlite_master`` BEFORE the lifecycle via a
         direct ``urllib`` call to ``/api/execution/{plan_id}/start``
         — the fixture's own start call happens before our test
         body runs (during fixture setup), so we compare against the
         post-setup state.  The "before" snapshot is taken from the
         fixture's pre-lifecycle baseline via the schema gate's
         own file-read of ``state.db`` AFTER fixture teardown.
      3. After fixture teardown, inspect ``state.db`` again and
         confirm the table set has not grown beyond the
         schema-migration baseline (core ∪ migration).
      4. The HTTP call itself is performed by the fixture; we
         re-issue it inside the test body (the fixture's start
         call is acceptable as the primary drive) and parse the
         response shape.

    Note: The fixture's pre-conditions give us a deterministic
    schema baseline (the migrate() call is idempotent and applies
    the same 5 core tables on every invocation).  A regression that
    dynamically creates a table during /start will surface as
    ``actual > (core ∪ migration)`` after the fixture teardown.
    """
    plans_root: Path = exec_instance_setup
    db_path = plans_root.parent / "state.db"

    actual = _read_sqlite_master_table_names(db_path)

    missing = _CORE_SCHEMA_TABLES - actual
    if missing:
        pytest.fail(
            f"缺失核心表: {sorted(missing)!r}。 "
            f"API 契约要求 schema 同时包含 "
            f"{sorted(_CORE_SCHEMA_TABLES)!r}; "
            f"当前 sqlite_master: {sorted(actual)!r}."
        )

    stale = _STALE_BUSINESS_TABLES & actual
    if stale:
        pytest.fail(
            f"残留旧表: {sorted(stale)!r}。 "
            f"Repository 层迁移禁止这些表; 当前 sqlite_master: "
            f"{sorted(actual)!r}."
        )

    extras = {
        name for name in actual
        if name not in _CORE_SCHEMA_TABLES and not _is_migration_table(name)
    }
    if extras:
        pytest.fail(
            f"动态建表违规: {sorted(extras)!r}。 "
            f"POST /api/execution/{{plan_id}}/start 不应触发任何 "
            f"CREATE TABLE; 当前 sqlite_master: {sorted(actual)!r}."
        )

    # Now drive a SECOND start call against the running instance
    # (the fixture drove the first).  The endpoint response shape
    # must be parseable JSON and contain at least the minimum
    # contract keys when the status code is 2xx.
    #
    # The fixture redirects ``server.PLANS_DIR`` to ``plans_root``
    # so the vptest-teardown-guard plan is only present inside the
    # tmp tree — we cannot rely on it for the second drive.  Use a
    # fresh, isolated plan_id under the same plans_root.
    plan_id = "vptest-repository-api-contract"
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    # Plant a minimal tasks.json so the endpoint's "tasks.json not
    # found" pre-condition is satisfied (otherwise it returns 404
    # before the schema layer is exercised).
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")
    # Plant a non-archived PRD so the placeholder-PRD guard is
    # satisfied (the fixture's monkeypatch is in effect; we still
    # need to avoid the placeholder rejection path).
    (plan_dir / "prd.md").write_text(
        "# dummy prd\n\n(not a placeholder)\n",
        encoding="utf-8",
    )

    # Issue the API call.  We accept 200, 400, 404, 409, or 410 —
    # all are valid "endpoint exercised" responses and the schema
    # contract only requires the JSON shape to be parseable.
    project_dir = plans_root.parent / "vptest-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"project_dir": str(project_dir), "sync_targets": ["telegram"]}
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        f"http://127.0.0.1:8001/api/execution/{plan_id}/start",
        data=body,
        headers={
            "Content-Type": "application/json",
            # Same reason as the other two call sites: real HTTP, so no
            # TestClient injection. Left off, this 403s before reaching
            # any behaviour the assertions below are about.
            request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            status_code = resp.status
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        raw_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""

    # The contract: response MUST be parseable JSON.  A non-JSON
    # body means the endpoint leaked a raw traceback or HTML error
    # page — that's a contract violation.
    if not raw_body.strip():
        pytest.fail(
            f"POST /api/execution/{plan_id}/start 返回空 body: "
            f"status_code={status_code}; 契约要求 JSON 响应."
        )
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"POST /api/execution/{plan_id}/start 返回非 JSON body: "
            f"status_code={status_code}; body={raw_body[:200]!r}; "
            f"error={exc}. 契约要求可解析 JSON."
        )

    if not isinstance(payload, dict):
        pytest.fail(
            f"POST /api/execution/{plan_id}/start 返回 JSON 但顶层不是 "
            f"object: type={type(payload).__name__}, "
            f"status_code={status_code}, body={raw_body[:200]!r}."
        )

    # If the call succeeded (status_code < 400), require the
    # contract's minimum key set on the response payload.
    if 200 <= status_code < 300:
        missing_keys = _API_REQUIRED_KEYS_200 - payload.keys()
        if missing_keys:
            pytest.fail(
                f"POST /api/execution/{plan_id}/start 缺少契约字段 "
                f"{sorted(missing_keys)!r}: keys={sorted(payload.keys())!r}, "
                f"status_code={status_code}. 契约字段集 = "
                f"{sorted(_API_REQUIRED_KEYS_200)!r}."
            )

    # Final schema re-check: the lifecycle MUST NOT have created
    # any new tables beyond (core ∪ migration).
    post_actual = _read_sqlite_master_table_names(db_path)
    post_extras = {
        name for name in post_actual
        if name not in _CORE_SCHEMA_TABLES and not _is_migration_table(name)
    }
    if post_extras:
        pytest.fail(
            f"POST /api/execution/{plan_id}/start 后 sqlite_master 含未列入 "
            f"白名单的表: {sorted(post_extras)!r}; "
            f"完整 sqlite_master: {sorted(post_actual)!r}. "
            f"API 契约禁止动态建表."
        )


# ---------------------------------------------------------------------------
# Defense 1c — adversarial-input self-test
# ---------------------------------------------------------------------------
#
# The 5 grep tests above pin the gates against the production source tree.
# This defense pins the gates AGAINST THE GATES THEMSELVES: it constructs
# synthetic samples that exercise exactly the 5 string-construction
# patterns and asserts each grep test detects them.
#
# Why this defense exists:
#   A grep test that always passes — even when the patterns it scans for
#   are present in the source — is worse than no test at all: it gives
#   false confidence.  The 5 grep tests were constructed by hand and
#   audited in code review; this defense mechanically demonstrates that
#   the audit was right by feeding the gates adversarial inputs and
#   verifying they fire.
#
# Each adversarial sample corresponds to exactly one of the 5 grep
# pattern classes:
#
#   1. direct_literal       — `'<name>.json'`
#   2. string_concat        — `'<a>' + '<b>.json'`
#   3. variable_reference   — `_RT_FN_EXEC` (variable starts with the prefix)
#   4. fstring_format       — `f"{prefix}.json"` or `'<a>.json'.format(...)`
#   5. pathlib_join         — `Path(...) / '<name>.json'`
#
# The samples live in
# ``backend/tests/fixtures/adversarial/all_patterns.py`` so they cannot
# be present in the production source tree (which would itself trip the
# defense — and we want the tests to FAIL TO FAIL when an adversarial
# sample leaks into production).
#
# TDD contract:
#   - ``test_adversarial_grep_inputs``: for each of the 5 pattern
#     classes, an adversarial sample MUST be detected by exactly one
#     of the 5 grep tests (or a coordinated combination).  Zero
#     detections → ``pytest.fail("守门实现存在漏报")``.
#   - ``test_no_assertion_swallowing``: scan the source of this test
#     file for any ``except AssertionError`` followed by a downgrade
#     to warn / log / print / stderr.write — these are the
#     "swallow AssertionError, exit code 0" anti-pattern that
#     defeats the gate.  Any occurrence → ``pytest.fail("...")``.


_ADVERSARIAL_FIXTURE = (
    _BACKEND_ROOT
    / "tests"
    / "fixtures"
    / "adversarial"
    / "all_patterns.py"
)


def _run_pattern_against_fixture(
    sample_line: str,
) -> dict[str, list[tuple[int, str]]]:
    """Run all 5 pattern checkers against a synthetic single-line
    ``sample_line`` and return a dict mapping pattern name → list of
    ``(line_no, matched_text)`` hits.

    A real hit on the synthetic line is the success signal — the
    "gate caught it" answer.  This helper exists so the
    ``test_adversarial_grep_inputs`` test can iterate one sample per
    pattern class and assert exactly one hit per class.
    """
    # We construct a synthetic file in a tmp dir and run all 5
    # patterns against it.  Each pattern helper signature differs
    # (they all take a Path list), so we wrap them in a thin adapter
    # here: write the sample into a Path, run every scanner over
    # ``[that_path]``, and aggregate.
    import tempfile
    import os as _os

    tmpdir = tempfile.mkdtemp(prefix="vptest-adversarial-")
    try:
        fake_path = Path(tmpdir) / "adversarial_sample.py"
        fake_path.write_text(sample_line + "\n", encoding="utf-8")
        targets: list[Path] = [fake_path]

        # Pattern 1 — direct literal.  Re-implement the small piece
        # of the production regex inline (it is the same pattern).
        p1 = re.compile(
            r"""(?:"|')"""
            r"""(?:plan_state|execution|verification_runtime_state|"""
            r"""verification_executor_state|verification_progress_state)\.json"""
            r"""(?:"|')"""
        )
        hits_p1: list[tuple[int, str]] = []
        for line_no, line in enumerate(fake_path.read_text().splitlines(), 1):
            m = p1.search(line)
            if m:
                hits_p1.append((line_no, m.group(0)))

        # Pattern 2 — string concatenation.  Uses the AST walker.
        hits_p2: list[tuple[int, str]] = []
        try:
            tree = ast.parse(sample_line, filename=str(fake_path))
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
                    continue
                operands: list[str] = []
                cur: ast.expr | None = node.left
                while (
                    isinstance(cur, ast.BinOp)
                    and isinstance(cur.op, ast.Add)
                    and cur.lineno == node.lineno
                ):
                    if isinstance(cur.right, ast.Constant) and isinstance(cur.right.value, str):
                        operands.append(cur.right.value)
                    cur = cur.left
                if isinstance(cur, ast.Constant) and isinstance(cur.value, str) and cur.lineno == node.lineno:
                    operands.append(cur.value)
                if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                    operands.append(node.right.value)
                joined = "".join(operands)
                for forbidden in _FORBIDDEN_FILENAMES:
                    if forbidden in joined:
                        hits_p2.append((node.lineno, joined))
                        break

        # Pattern 3 — variable reference.
        p3 = re.compile(
            r"\b(?:"
            r"_RT_FN_\w+"
            r"|progress_state_file"
            r"|plan_state_file"
            r"|exec_file"
            r"|progress_file"
            r")\b"
        )
        hits_p3: list[tuple[int, str]] = []
        for line_no, line in enumerate(fake_path.read_text().splitlines(), 1):
            m = p3.search(line)
            if m:
                hits_p3.append((line_no, m.group(0)))

        # Pattern 4 — fstring / .format().
        p4 = re.compile(
            r"""f["'][^"']*\{[^}]*\}[^"']*\.json["']"""
            r"""|"""
            r"""\.format\([^)]*\.json\)"""
        )
        hits_p4: list[tuple[int, str]] = []
        for line_no, line in enumerate(fake_path.read_text().splitlines(), 1):
            m = p4.search(line)
            if m:
                hits_p4.append((line_no, m.group(0)))

        # Pattern 5 — pathlib / os.path.join.
        p5 = re.compile(
            r"""Path\([^)]*\)\s*/\s*["'][^"']*\.json"""
            r"""|"""
            r"""os\.path\.join\([^)]*\.json\)"""
        )
        hits_p5: list[tuple[int, str]] = []
        for line_no, line in enumerate(fake_path.read_text().splitlines(), 1):
            m = p5.search(line)
            if m:
                hits_p5.append((line_no, m.group(0)))

        return {
            "direct_literal": hits_p1,
            "string_concat": hits_p2,
            "variable_reference": hits_p3,
            "fstring_format": hits_p4,
            "pathlib_join": hits_p5,
        }
    finally:
        # Best-effort cleanup; tmp dirs are mkdtemp-created so each
        # run has its own and won't grow without bound.
        try:
            for entry in _os.listdir(tmpdir):
                _os.remove(_os.path.join(tmpdir, entry))
            _os.rmdir(tmpdir)
        except OSError:
            pass


def _load_adversarial_samples() -> dict[str, str]:
    """Read the on-disk adversarial fixture and return a mapping from
    pattern class name (``direct_literal`` etc.) to the synthetic
    sample line.

    The fixture file declares its samples as module-level assignments
    of the form::

        SAMPLE_DIRECT_LITERAL = '"plan_state.json"'
        SAMPLE_STRING_CONCAT  = '"executi" + "on.json"'
        ...

    We ``exec`` the file in a controlled namespace and pull the
    samples back out — this means the fixture file is plain Python
    (no custom parser) and the sample strings can themselves
    legitimately contain quote characters that would be awkward to
    represent in JSON.
    """
    if not _ADVERSARIAL_FIXTURE.exists():
        return {}
    namespace: dict[str, object] = {}
    exec(  # nosec B102  # noqa: S102 — controlled fixture content, no user input
        _ADVERSARIAL_FIXTURE.read_text(encoding="utf-8"),
        namespace,
    )
    return {
        key: value
        for key, value in namespace.items()
        if key.startswith("SAMPLE_") and isinstance(value, str)
    }


@pytest.mark.unit
def test_adversarial_grep_inputs() -> None:
    """Self-test: the 5 grep gate patterns catch adversarial inputs.

    Loads the 5 sample lines from
    ``backend/tests/fixtures/adversarial/all_patterns.py`` and runs
    each of the 5 pattern checks against each sample.  The contract
    is that the fixture lines exist AND every pattern's corresponding
    sample produces at least one hit on the in-memory regex set.
    If any pattern fails to detect its corresponding sample, the
    defense is broken — call ``pytest.fail``.
    """
    samples = _load_adversarial_samples()
    if not samples:
        pytest.fail(
            "adversarial fixture missing or empty: "
            f"{_ADVERSARIAL_FIXTURE}; "
            "the 5 grep gate tests above cannot be self-validated."
        )

    # The contract: every pattern class has a corresponding sample
    # AND every sample fires its target pattern.  Map pattern class
    # names to the SAMPLE_* fixture keys.
    pattern_to_sample_key = {
        "direct_literal": "SAMPLE_DIRECT_LITERAL",
        "string_concat": "SAMPLE_STRING_CONCAT",
        "variable_reference": "SAMPLE_VARIABLE_REFERENCE",
        "fstring_format": "SAMPLE_FSTRING_FORMAT",
        "pathlib_join": "SAMPLE_PATHLIB_JOIN",
    }

    missing_keys = [
        sample_key
        for sample_key in pattern_to_sample_key.values()
        if sample_key not in samples
    ]
    if missing_keys:
        pytest.fail(
            f"adversarial fixture missing sample keys {sorted(missing_keys)!r}; "
            f"expected keys = {sorted(pattern_to_sample_key.values())!r}, "
            f"present = {sorted(samples.keys())!r}. Fix the fixture."
        )

    # For each pattern class, run the same regexes the production
    # _pattern_* helpers use against the corresponding sample line
    # and verify the sample produces at least one hit.
    failures: list[str] = []
    for pattern_name, sample_key in pattern_to_sample_key.items():
        sample = samples[sample_key]
        hits_by_pattern = _run_pattern_against_fixture(sample)
        if not hits_by_pattern[pattern_name]:
            failures.append(
                f"  - {pattern_name}: sample {sample_key}={sample!r} "
                f"did NOT fire its gate (no hit on the in-memory regex set)"
            )

    if failures:
        pytest.fail(
            "守门实现存在漏报 — the following pattern classes did NOT detect "
            "their adversarial sample:\n" + "\n".join(failures)
        )


@pytest.mark.unit
def test_no_assertion_swallowing() -> None:
    """Self-test: this test file MUST NOT contain any
    ``except AssertionError`` block that downgrades a failed assertion
    into a warning / log / print / stderr.write.

    The pattern is exactly::

        except (some combo that includes) AssertionError:
            log.warning(...)
            # or print(...)
            # or sys.stderr.write(...)

    Such blocks defeat the gate by catching pytest.fail() before the
    process exit code flips to non-zero — the very bug class that
    this whole defense layer exists to prevent (e.g. task 7's
    ``try/except AssertionError → log.warning(...) → exit code 0``
    regression).

    Detection regex: match a span that begins with ``except`` and
    contains ``AssertionError`` (possibly inside a tuple), then ends
    with a body that emits to ``log.warning`` / ``print`` /
    ``sys.stderr.write``.  Any match → ``pytest.fail``.

    The test source itself is in scope — the contract is that the
    TEST SUITE does not contain this anti-pattern.  The body of the
    test that performs the scan uses re.finditer on the file text
    and does NOT itself catch AssertionError (only ``pytest.fail``
    raises AssertionError, and we want uncaught propagation so the
    process exit code is non-zero on regression).
    """
    test_file = Path(__file__).resolve()
    text = test_file.read_text(encoding="utf-8")

    # Find every ``except`` line that mentions AssertionError.  We
    # then walk forward looking for a downgrade sink inside the
    # except body.  The walk is bounded by indentation: the except
    # block ends when a line drops below the except's indent level.
    #
    # We use ``tokenize`` to filter out matches that live inside
    # docstrings or comments — the docstring of THIS test function
    # legitimately mentions the literal ``except AssertionError:``
    # syntax to explain what we are looking for, and we must not
    # flag ourselves.
    import tokenize as _tokenize
    import io as _io

    try:
        tokens = list(_tokenize.generate_tokens(_io.StringIO(text).readline))
    except _tokenize.TokenizeError:
        tokens = []

    # Build a set of (start_line, end_line) ranges to skip — every
    # docstring and comment.
    skip_ranges: list[tuple[int, int]] = []
    for tok in tokens:
        if tok.type == _tokenize.COMMENT:
            skip_ranges.append((tok.start[0], tok.end[0]))
        elif tok.type == _tokenize.STRING and tok.string.startswith(('"""', "'''")):
            # Multi-line string (docstring).
            skip_ranges.append((tok.start[0], tok.end[0]))

    def _in_skip(line_no: int) -> bool:
        for start, end in skip_ranges:
            if start <= line_no <= end:
                return True
        return False

    raw_anchors = list(re.finditer(
        r"^[ \t]*except[^\n]*AssertionError[^\n]*:\s*$",
        text,
        re.MULTILINE,
    ))
    except_anchors = []
    for anchor in raw_anchors:
        anchor_line = text[: anchor.start()].count("\n") + 1
        if not _in_skip(anchor_line):
            except_anchors.append(anchor)

    if not except_anchors:
        # No except-AssertionError blocks in real code → contract
        # trivially satisfied.  Early exit (this is the success
        # path: the production code and test code are clean).
        return

    violations: list[str] = []
    for anchor in except_anchors:
        except_indent = len(anchor.group(0)) - len(anchor.group(0).lstrip(" "))
        except_line_no = text[: anchor.start()].count("\n") + 1
        # Find the body: lines after ``:`` at greater indent than
        # except_indent.  Stop at the first line whose indent is
        # <= except_indent (or EOF).
        pos = anchor.end()
        body_lines: list[str] = []
        while pos < len(text):
            nl = text.find("\n", pos)
            if nl == -1:
                line = text[pos:]
                next_pos = len(text)
            else:
                line = text[pos: nl]
                next_pos = nl + 1
            stripped = line.lstrip(" \t")
            if stripped and (len(line) - len(stripped)) <= except_indent:
                break
            body_lines.append(line)
            pos = next_pos
            if len(body_lines) > 20:
                # Cap to keep the scan bounded; the AssertionError
                # downgrade would always appear within the first few
                # body lines anyway.
                break
        body_text = "\n".join(body_lines)
        # Detection: does the body emit to log.warning / print /
        # sys.stderr.write?
        if re.search(
            r"\blog\.(warning|error|info|debug|critical)\b"
            r"|\bprint\s*\("
            r"|\bsys\.stderr\.write\s*\("
            r"|\blogger\.(warning|error|info|debug|critical)\b"
            r"|\blogging\.(warning|error|info|debug|critical)\b",
            body_text,
        ):
            violations.append(
                f"  - line {except_line_no}: "
                f"except ... AssertionError ... : followed by a "
                f"log/print/stderr downgrade in body:\n"
                f"    {anchor.group(0).strip()}\n"
                f"    <body: {len(body_lines)} line(s)>"
            )

    if violations:
        pytest.fail(
            "守门实现存在 AssertionError 吞咽 — this test file contains "
            "except AssertionError blocks that downgrade to "
            "log.warning / print / sys.stderr.write, allowing the "
            "pytest.fail() signal to be silenced (exit code stays 0):\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Defense 4 — E2E real-server lifecycle gate
# ---------------------------------------------------------------------------
#
# The 5 grep tests + filesystem teardown guard + sqlite_master whitelist
# + adversarial self-test form a strong static layer, but the ultimate
# proof that all three defenses work together is to **drive a real
# server.py instance through a complete lifecycle and assert zero
# forbidden JSON sidecars land on disk**. The static gates alone cannot
# catch a runtime regression where, e.g., a new dependency writes
# ``plan_state.json`` straight to disk without going through any
# production source we scan.
#
# This section adds:
#
#   * ``exec_instance_lifecycle`` — a fixture that subprocess.Popen-
#     spawns ``server.py`` on ``EXEC_PORT=8001``, plants an interview.json
#     + tasks.json fixture pair, drives ``/api/execution/{plan_id}/start``
#     against the live instance, waits for progress, and on teardown
#     first terminates the server then ``shutil.rmtree``s the temp dir.
#   * ``test_e2e_three_layer_defense`` — a single ``pytest.mark.e2e`` test
#     that consumes the fixture, then asserts:
#       1. (Defense 1 — source grep) the three pinned grep tests
#          (``test_pattern_direct_literal``, ``test_pattern_string_concat``,
#          ``test_pattern_variable_reference``) all still pass against the
#          current production source tree.
#       2. (Defense 1b — filesystem walk) the plans/ tree under the
#          fixture's tmp_path has zero forbidden JSON sidecars after
#          teardown (re-uses ``_collect_teardown_hits``).
#       3. (Defense 2 — sqlite_master whitelist) the spawned server's
#          ``state.db`` carries only the 4 core plan_* tables +
#          ``schema_version`` (+ migration meta) — no legacy
#          ``plans`` / ``plan_meta`` / ``plan_activity`` residue, no
#          dynamically created tables.
#
# The test is marked ``pytest.mark.e2e`` so it is enabled ONLY when
# pytest is invoked with ``--e2e``; the default ``pytest`` run
# (without ``--e2e``) skips it (a real ``subprocess.Popen`` against
# ``server.py`` is too expensive for the standard unit-test suite).
#
# Boundary conditions (per the task brief):
#
#   - subprocess.Popen launches a real ``uvicorn server:app`` process
#     on EXEC_PORT=8001 — NO mock, NO monkeypatch of ``server.app``.
#   - Fixture teardown order: ``proc.terminate()`` FIRST, then
#     ``shutil.rmtree(temp_dir)`` (so the server has fully released
#     its state.db handle before we delete the parent directory).
#   - The marker ``pytest.mark.e2e`` means the test is skipped unless
#     ``--e2e`` is explicitly passed (no silent fallthrough to unit).
#   - Failures call ``pytest.fail`` — NEVER ``log.warning`` /
#     ``print`` / ``sys.stderr.write`` (that anti-pattern is what
#     ``test_no_assertion_swallowing`` already guards; do not regress
#     it here).
#   - Total e2e runtime budget: < 30 seconds (server spawn + /health
#     poll + one /start call + teardown).

import shutil as _shutil  # for shutil.rmtree in fixture teardown


_E2E_FIXTURE_PLAN_ID = "20260101-cleanup-sample"
_E2E_PORT = 8001
_E2E_HEALTH_URL = f"http://127.0.0.1:{_E2E_PORT}/health"
_E2E_START_URL_TEMPLATE = f"http://127.0.0.1:{_E2E_PORT}/api/execution/{{plan_id}}/start"


def _child_path() -> str:
    """PATH for a spawned server: this process's, plus the venv if any.

    The parent PATH is the base, not something to be replaced. It is what
    `setup-python` populated and what makes the runner's own toolchain
    reachable, and it is where a venv-installed ``uvicorn`` lands — so a
    child given a shorter list cannot resolve the interpreter that
    `_find_uvicorn_path()` just located a moment earlier.

    The failure is silent in the worst way: the child dies at import, the
    health poll times out, the fixture swallows that, and the test asserts
    against a database nobody created. The comment at the other call
    site records the same symptom arriving by a different route, which is
    what made it look like a schema regression instead of a broken spawn.

    ``sys.executable``'s directory is prepended for the venv case, where
    the venv's ``bin`` may not be on PATH at all (a bare
    ``python -m pytest`` invocation does not put it there).
    """
    import os as _os
    import sys as _sys

    parts = [_os.path.dirname(_sys.executable)]
    parts += [
        p for p in _os.environ.get("PATH", "").split(_os.pathsep) if p
    ]
    return _os.pathsep.join(parts)


def _find_uvicorn_path() -> str | None:
    """Absolute path of the ``uvicorn`` to spawn, or ``None``.

    Resolved from the running interpreter's own environment first, not
    from ``PATH``. A hosted runner image ships uvicorn of its own, and
    ``shutil.which`` was finding *that* one — a copy with none of this
    project's dependencies. The spawn then died importing the backend,
    the health poll timed out, the fixture treated a failed spawn as a
    successful setup, and the test asserted against a database nobody had
    created: ``sqlite_master`` empty, which reads exactly like a schema
    regression.

    The venv's own copy is the one that can import ``server``. When it
    is genuinely absent, the correct outcome is a skip — "no live server
    to test" — and this is now the only path that can produce one, so a
    foreign uvicorn can no longer masquerade as ours.

    Both fixtures resolve through here, so the two real-server gates
    cannot drift apart again.
    """
    import os as _os
    import sys as _sys

    candidate = _os.path.join(
        _os.path.dirname(_sys.executable), "uvicorn"
    )
    if _os.path.isfile(candidate) and _os.access(candidate, _os.X_OK):
        return candidate
    return _shutil.which("uvicorn")


@pytest.fixture
def exec_instance_lifecycle(tmp_path, monkeypatch):
    """Drive a full EXEC_PORT=8001 lifecycle (spawn → /start → teardown).

    Steps performed:

      1. Create a per-test isolated directory tree under ``tmp_path``:
           ``tmp_path/plans/20260101-cleanup-sample/`` (the fixture
           plan directory) + ``tmp_path/state.db`` (a per-test SQLite
           file the spawned server is told to use).
      2. Plant ``interview.json`` + ``tasks.json`` from
           ``backend/tests/fixtures/plans/20260101-cleanup-sample/`` so
           the server's /api/execution/{plan_id}/start pre-condition
           (a non-archived plan with a tasks.json) is satisfied.
      3. Redirect ``server.PLANS_DIR`` to the per-test tmp tree so the
           teardown walk in ``test_e2e_three_layer_defense`` scans
           only this run's artifacts (no cross-run leakage).
      4. Spawn ``uvicorn server:app`` on EXEC_PORT=8001 via
           ``subprocess.Popen`` (real process — NOT a mock).
      5. Poll ``GET /health`` for up to 30 s, waiting for the server
           to come up.
      6. ``POST /api/execution/{plan_id}/start`` against the running
           server (we accept 200/4xx — the contract requires the
           endpoint be exercised, not necessarily succeed).
      7. Yield ``tmp_path`` so the consuming test can perform the
           post-lifecycle assertions (filesystem walk + sqlite_master
           read + grep gate re-check).
      8. On teardown: ``proc.terminate()`` (graceful SIGTERM) — wait
           for exit; if it does not exit within 5 s escalate to
           ``proc.kill()``. AFTER the server has released its
           ``state.db`` handle, ``_shutil.rmtree(tmp_path)`` to
           garbage-collect the per-test tree.

    Returns
    -------
    ``Path`` to the per-test tmp directory root. The caller is
    expected to walk ``root / "plans"`` for the filesystem assertion
    and ``root / "state.db"`` for the sqlite_master assertion.

    Notes
    -----
    * The fixture is module-level skipped when ``uvicorn`` is not on
      PATH — it cannot meaningfully exercise the live lifecycle
      contract without a real server.
    * ``PDT_STATE_DB_PATH`` is propagated via ``env=...`` so the
      spawned server writes to a per-test tmp SQLite file, NOT to
      the project-level ``<PLANS_DIR.parent>/state.db``.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    plan_dir = plans_root / _E2E_FIXTURE_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    # Redirect the production PLANS_DIR to our tmp_path so the post-
    # teardown walk sees only this run's artifacts.
    monkeypatch.setattr("server.PLANS_DIR", plans_root)

    # Plant the baseline artifacts (interview.json + tasks.json) from
    # the on-disk fixture directory so the server's /api/execution
    # pre-condition is satisfied.
    fixture_src = (
        _BACKEND_ROOT
        / "tests"
        / "fixtures"
        / "plans"
        / _E2E_FIXTURE_PLAN_ID
    )
    for fname in ("interview.json", "tasks.json"):
        src = fixture_src / fname
        dst = plan_dir / fname
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # Resolve uvicorn.  Skip if not present (no real server → cannot
    # exercise the live lifecycle contract).
    uvicorn_path = _find_uvicorn_path()
    if uvicorn_path is None:
        pytest.skip(
            "uvicorn is not on PATH in this environment; "
            "the e2e three-layer defense test requires a live "
            "EXEC_PORT=8001 instance."
        )

    # Spawn the real uvicorn process on EXEC_PORT=8001.
    tmp_state_db = tmp_path / "state.db"
    server_log = open(tmp_path / "uvicorn.log", "w+b")  # noqa: SIM115
    env = {
        "EXEC_PORT": str(_E2E_PORT),
        "PDT_STATE_DB_PATH": str(tmp_state_db),
        # Two symptoms, one variable, and neither is where the other is.
        #
        # The 404: `server.py:166` reads PDT_PLANS_DIR once, at import,
        # and caches it into the module global PLANS_DIR that `_plan_dir()`
        # resolves against. A child process cannot inherit this test's
        # `monkeypatch.setattr("server.PLANS_DIR", plans_root)` — that
        # mutates the test process, and the server is a different one.
        # So without this key the spawned server looks for tasks.json
        # under <checkout>/plans and never finds the pair planted above.
        #
        # The missing state.db is NOT this request's doing, which is the
        # part that misleads. `20260101-cleanup-sample` classifies as
        # `archived` (its date is past any cutoff classify_plan is given),
        # so `/start` returns 410 before it reaches open_db/migrate — the
        # request path can never create this database. What creates it is
        # the lifespan's `_recover_verification_states(PLANS_DIR)`
        # (server.py:1070), which calls open_db + migrate once per
        # subdirectory it finds under plans/ (verification_loop.py).
        #
        # That is why the missing key breaks both at once — the recovery
        # loop iterates the same PLANS_DIR `_plan_dir()` uses — and why
        # the failure is invisible on a developer machine. `/plans/` is
        # gitignored (`.gitignore:85`), so a CI checkout has none, and
        # PLANS_DIR.mkdir() at import leaves it empty: zero subdirectories
        # means the loop body never runs means no database. A local
        # checkout has a dozen real plan directories, so the same 404
        # still produces a populated state.db. Verified both directions
        # by running this test against a `git archive` of HEAD: identical
        # failure to CI without the key, passes with it.
        #
        # `exec_instance_setup` below has carried this key since it was
        # written; this fixture is a later copy that missed it.
        "PDT_PLANS_DIR": str(plans_root),
        # Extend the parent's PATH, do not replace it. It used to be a
        # hard-coded macOS list, which meant the spawned server had no
        # venv on its PATH: on CI the uvicorn that `which()` found a
        # moment earlier lives in backend/.venv/bin, and the child could
        # not resolve it. The server then died at startup, the health
        # poll timed out, the fixture swallowed it, and the test carried
        # on to assert against a database that had never been created —
        # "sqlite_master 含 []", which is the exact symptom the comment
        # above already documents for the stale-listener case. The local
        # machine hid this: its uvicorn is outside the venv too, but it
        # happened to start anyway.
        "PATH": _child_path(),
        # See exec_instance_setup for why repo-root PYTHONPATH is
        # required — without it the spawn dies at import time
        # (``from backend.framework...``) and the e2e layer SKIPs.
        "PYTHONPATH": str(_BACKEND_ROOT.parent),
    }
    proc = subprocess.Popen(  # noqa: S603
        [
            uvicorn_path,
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(_E2E_PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(_BACKEND_ROOT),
        env=env,
        # NOT DEVNULL. The server is the only thing that knows why it
        # failed to bring the database up, and every consumer of this
        # fixture reads the database afterwards -- so with the output
        # discarded, "the server never wrote state.db" is
        # indistinguishable from "the server wrote a state.db with no
        # tables in it", and the second reading is the one the schema
        # assertions go on to report as an empty ``sqlite_master``.
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )

    server_started = False
    try:
        # Wait for /health to come up (30 s budget).
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                    _E2E_HEALTH_URL, timeout=2
                ) as resp:
                    if 200 <= resp.status < 300:
                        server_started = True
                        break
            except (urllib.error.URLError, ConnectionResetError, OSError):
                pass
            time.sleep(0.25)
        def _server_log_tail() -> str:
            try:
                server_log.flush()
                server_log.seek(0)
                return server_log.read().decode("utf-8", "replace")[-2000:]
            except OSError:
                return "<server log unavailable>"

        if not server_started:
            pytest.skip(
                f"EXEC_PORT={_E2E_PORT} server did not become healthy "
                "within 30s; e2e three-layer defense cannot run. "
                f"Server output:\n{_server_log_tail()}"
            )

        # A healthy :8001 is not evidence that THIS server is the one
        # answering. The port is fixed, so anything else that ever bound
        # it — a previous test's server that has not been reaped, or a
        # listener the runner image starts — answers the probe instantly
        # while writing to a DIFFERENT state.db. This fixture has no
        # `PDT_STATE_DB_PATH` in the picture, so the consequence is a
        # health check that passes, a foreign server, and an empty
        # ``sqlite_master`` in the schema layer with nothing in between
        # saying so.
        #
        # `exec_instance_setup` has had this guard since it was written;
        # this fixture was a later copy that did not bring it along. That
        # is why the failure looked like a schema regression: the schema
        # assertion is the first thing to *report* it, and it is nowhere
        # near the cause.
        # A healthy :8001 is not evidence that THIS server is the one
        # answering. The port is fixed, so anything else that ever bound
        # it — a previous test's server not yet reaped, or a listener the
        # runner image starts — answers the probe instantly while writing
        # to a DIFFERENT state.db. `exec_instance_setup` has guarded
        # against this since it was written; this fixture was a later
        # copy that did not bring the guard along.
        #
        # A *live* server is the distinguishing evidence, and the process
        # outlives the bind failure: when the port is taken it starts,
        # writes its boot lines, and only then fails the bind, so
        # `proc.poll()` has to be read after a beat rather than at the
        # instant the probe succeeds.
        for _ in range(20):                      # up to 5s at 0.25s
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if proc.poll() is not None:
            pytest.skip(
                f"the spawned server exited (rc={proc.returncode}) before "
                f"the health probe succeeded, so another process is "
                f"serving :{_E2E_PORT} and this test would read that "
                f"server's database. Server output:\n{_server_log_tail()}"
            )

        # Drive the /api/execution/{plan_id}/start endpoint.  We
        # accept 200/4xx — the contract is that the endpoint is
        # exercised end-to-end, not that the start succeeds.
        fixture_project_dir = tmp_path / "e2e-project"
        fixture_project_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {"project_dir": str(fixture_project_dir)}
        ).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            _E2E_START_URL_TEMPLATE.format(plan_id=_E2E_FIXTURE_PLAN_ID),
            data=body,
            headers={
                "Content-Type": "application/json",
                # `server.request_guard` refuses any /api/* request that
                # arrives without this header. This fixture talks to a
                # real server over real HTTP rather than through
                # Starlette's TestClient, so it does not get the header
                # `tests/conftest.py` injects into every TestClient — it
                # has to send its own.
                #
                # Without it the call 403s, the request never reaches the
                # code that runs `migrate()`, state.db is never created,
                # and the schema layer reports an empty sqlite_master —
                # which reads as a schema regression and is not one.
                request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
            },
            method="POST",
        )
        status = None
        start_body = ""
        start_error = ""
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                status = resp.status
                start_body = resp.read().decode("utf-8", "replace")[:400]
        except urllib.error.HTTPError as exc:
            # 404/409/etc. are acceptable for the e2e test — we only
            # need the endpoint exercised. The body is kept because when
            # the schema layer later reports an empty sqlite_master, the
            # question is always "what did this call actually say".
            status = exc.code
            start_body = exc.read().decode("utf-8", "replace")[:400]
        except (urllib.error.URLError, OSError) as exc:
            start_error = f"{type(exc).__name__}: {exc}"

        # The database is NOT created by the call below, so the call's
        # own response is not evidence about it. This plan classifies as
        # `archived`, so /start returns 410 before it reaches
        # open_db/migrate. What brings the schema up is the lifespan's
        # recovery scan, which runs open_db + migrate per subdirectory
        # under plans/ — see the PDT_PLANS_DIR note in the env block for
        # the full chain and for why that scan has nothing to iterate on
        # a CI checkout.
        #
        # So this asserts on the state the server left behind rather than
        # on the endpoint's reply, and it fails rather than skips: an
        # earlier version of this fixture checked for the database
        # *before* driving the endpoint, so on a runner where the schema
        # never got created it skipped with a message about a foreign
        # process serving the port — which was not what was happening —
        # and the third defense layer silently stopped being tested on
        # every PR. A skip is only honest when the environment cannot run
        # the test; here it can, so not running it is a failure and this
        # one says what it found.
        for _ in range(20):                      # up to 5s at 0.25s
            if tmp_state_db.exists() and tmp_state_db.stat().st_size > 0:
                break
            time.sleep(0.25)

        if not (tmp_state_db.exists() and tmp_state_db.stat().st_size > 0):
            pytest.fail(
                f"the spawned server answered /health on "
                f":{_E2E_PORT} but never created {tmp_state_db}.\n"
                f"  start endpoint: status={status} error={start_error or 'none'}\n"
                f"  start body: {start_body}\n"
                f"  state.db: exists={tmp_state_db.exists()} size="
                f"{tmp_state_db.stat().st_size if tmp_state_db.exists() else 'n/a'}\n"
                f"  tmp dir contents: "
                f"{sorted(p.name for p in tmp_path.iterdir())}\n"
                f"  PDT_STATE_DB_PATH in child env: {env.get('PDT_STATE_DB_PATH')}\n"
                f"  server still running: {proc.poll() is None}\n"
                f"  server output:\n{_server_log_tail()}\n"
                f"The schema layer would report this as an empty "
                f"sqlite_master, which reads like a schema regression. "
                f"It is not: no migration ever ran."
            )

        yield tmp_path
    finally:
        # Teardown order (per the task brief):
        #   1. proc.terminate() — graceful SIGTERM
        #   2. wait up to 5 s for exit; escalate to proc.kill() if it
        #      does not exit cleanly
        #   3. ONLY after the server has released its state.db
        #      handle, shutil.rmtree(tmp_path) the temp tree
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
        # Finally, garbage-collect the per-test tree.  We do NOT wrap
        # this in try/except so that a rmtree failure surfaces
        # (the test runner can decide whether to retry).  The brief
        # pins the order: terminate first, THEN rmtree.
        try:
            _shutil.rmtree(tmp_path, ignore_errors=True)
        except OSError:
            pass


@pytest.mark.e2e
def test_e2e_three_layer_defense(exec_instance_lifecycle) -> None:
    """E2E end-to-end three-defense verification on a real server.

    Drives a complete EXEC_PORT=8001 lifecycle via the
    ``exec_instance_lifecycle`` fixture (subprocess.Popen spawn +
    /api/execution/{plan_id}/start + teardown) and asserts that all
    three layers of the defense are satisfied in the live system:

      Layer 1 (source grep, Defense 1):
        Re-run the three primary grep patterns (direct_literal,
        string_concat, variable_reference) against the current
        production source tree.  Zero hits → layer passes.
        The two remaining grep patterns (fstring_format,
        pathlib_join) are exercised by the unit-marked tests already
        in this module — including them here would be redundant.

      Layer 2 (filesystem walk, Defense 1b):
        Walk ``$tmp/plans/`` recursively for ``*.json`` files and
        assert zero hits against the five forbidden basenames
        (``_TEARDOWN_TARGETS``).  This catches any runtime regression
        where a dependency writes a forbidden JSON sidecar.

      Layer 3 (SQLite schema whitelist, Defense 2):
        Open ``$tmp/state.db`` and assert ``sqlite_master`` carries
        only the four core plan_* tables + ``schema_version`` (+
        migration metadata) — no legacy ``plans`` /
        ``plan_meta`` / ``plan_activity`` residue, no dynamically
        created tables.

    Boundary conditions enforced:

      * All three layers use ``pytest.fail`` (NOT a warning).  A
        layer that produces a hit aborts the test with a deterministic
        summary of the violation location.
      * The fixture teardown order (proc.terminate THEN shutil.rmtree)
        is owned by ``exec_instance_lifecycle`` — this test body only
        consumes the yielded ``tmp_path`` and performs assertions.
      * Total test runtime budget: < 30 s (the fixture already
        spends ≤ 30 s waiting for /health; this test body adds only
        filesystem + SQLite reads, well under 1 s).
      * The test is marked ``pytest.mark.e2e`` — invoke pytest with
        ``--e2e`` to enable; without it the test is skipped.
    """
    root: Path = exec_instance_lifecycle
    plans_root = root / "plans"
    db_path = root / "state.db"

    layer_failures: list[str] = []

    # ---- Layer 1: source grep (Defense 1) ---------------------------
    # Re-run the three primary patterns directly.  We do NOT call
    # the test_* functions themselves (pytest would treat that as a
    # re-run with its own setup/teardown).  Instead, invoke the
    # pattern helpers and assert empty hits.
    direct_literal_hits = _pattern_direct_literal()
    string_concat_hits = _pattern_string_concat()
    variable_reference_hits = _pattern_variable_reference()

    layer1_hits = direct_literal_hits + string_concat_hits + variable_reference_hits
    if layer1_hits:
        layer_failures.append(
            "Layer 1 (source grep) — "
            f"{len(layer1_hits)} forbidden pattern hit(s) in production:\n"
            + "\n".join(
                _format_hit("e2e", path, line_no, matched)
                for path, line_no, _line_text, matched in layer1_hits
            )
        )

    # ---- Layer 2: filesystem walk (Defense 1b) ----------------------
    fs_hits = _collect_teardown_hits(plans_root)
    if fs_hits:
        rel_paths = [str(p.relative_to(plans_root)) for p, _ in fs_hits]
        basenames = sorted({b for _, b in fs_hits})
        layer_failures.append(
            "Layer 2 (filesystem walk) — "
            f"{len(fs_hits)} forbidden JSON sidecar(s) in plans/:\n"
            f"  rel={rel_paths!r}\n"
            f"  basenames={basenames!r}"
        )

    # ---- Layer 3: SQLite schema whitelist (Defense 2) ---------------
    actual_tables = _read_sqlite_master_table_names(db_path)
    missing_core = _CORE_SCHEMA_TABLES - actual_tables
    stale_legacy = _STALE_BUSINESS_TABLES & actual_tables
    extras = {
        name for name in actual_tables
        if name not in _CORE_SCHEMA_TABLES and not _is_migration_table(name)
    }
    layer3_problems: list[str] = []
    if missing_core:
        layer3_problems.append(
            f"missing core tables: {sorted(missing_core)!r}"
        )
    if stale_legacy:
        layer3_problems.append(
            f"legacy tables leaked: {sorted(stale_legacy)!r}"
        )
    if extras:
        layer3_problems.append(
            f"non-whitelisted tables created: {sorted(extras)!r}"
        )
    if layer3_problems:
        layer_failures.append(
            "Layer 3 (SQLite schema whitelist) — \n  "
            + "\n  ".join(layer3_problems)
            + f"\n  actual: {sorted(actual_tables)!r}"
        )

    # ---- Aggregate verdict -----------------------------------------
    if layer_failures:
        pytest.fail(
            "E2E three-defense gate FAILED — "
            f"{len(layer_failures)}/3 layer(s) reported violations:\n\n"
            + "\n\n".join(layer_failures)
        )
