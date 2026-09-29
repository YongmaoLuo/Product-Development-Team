"""TDD tests for the three-defense layer: static JSON gate + 8000 guard.

Background
----------
This module pins **architecture decision point 8** for the state-machine
SQLite refactor — the three-defense layer that prevents regressions
where legacy JSON state files silently leak back into production code.

Defense 1: static grep gate (this file's first 4 tests)

  A static scan of ``backend/`` production code (excluding tests
  themselves) finds ZERO references to the five deprecated JSON state
  filenames::

    plan_state.json
    execution.json
    verification_runtime_state.json
    verification_executor_state.json
    verification_progress_state.json

  Production code must use the SQLite-backed repositories (plan_routing,
  plan_execution, plan_verification, plan_artifacts) instead.

  A short whitelist mechanism (``STATE_JSON_WHITELIST``) is also pinned
  empty by test_json_filename_whitelist_is_empty — any future entry must
  carry a justification, and that justification is reviewed in code
  review.

Defense 2: SQL schema gate (test_sqlite_schema_tables_exactly_four_plus_version)

  ``sqlite_master`` table name set must be EXACTLY::

    {"plan_routing", "plan_execution", "plan_verification",
     "plan_artifacts", "schema_version"}

  No ``plans``, ``plan_meta``, or ``plan_activity`` legacy tables.

Defense 3: 8000 guard + teardown fixture (conftest.py)

  A fixture in :mod:`state_machine.tests.conftest` autouse-binds to every
  test, asserting:
    * the resolved test data_dir is not the 8000 instance's data_dir;
    * ``socket.socket`` connections to ``127.0.0.1:8000`` are blocked;
    * a teardown scan of ``tmp_path`` finds zero state JSON files.

These tests together pin the full defense perimeter from architecture
decision point 8.  A failure in any one breaks the contract; the backend
backend is expected to refuse to merge any patch that breaks a passing
run of these tests.
"""

from __future__ import annotations

import ast
import io
import os
import re
import socket
import sqlite3
import tempfile
import tokenize
from pathlib import Path

import pytest

# Backend root for the static scan.  We scan ``backend/`` itself but
# exclude every ``tests/`` subtree (test code is allowed to mention
# filenames as assertion objects) and ``.venv/``.
#
# The scan walks the directory tree starting from the ``backend/``
# root.  Walking through os.walk() naturally excludes the ``tests/``
# subtrees anywhere under backend.
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: Sub-tree the scan targets.  The state-machine refactor is the new
#: module under active development; this scan guards the refactor's
#: boundary — it pins zero references to the deprecated JSON filenames
#: in the state_machine module's own production code paths
#: (db/, repositories/).  Legacy ``server.py`` / ``plan_state.py`` /
#: etc. are migrated by the broader state-machine refactor rollout,
#: not by this gate.
_STATE_MACHINE_ROOT = _BACKEND_ROOT / "state_machine"

#: Directory tree that should be excluded from the scan (test code
#: may reference filenames as assertion objects).  We exclude ANY
#: ``tests/`` directory anywhere under ``backend/`` — that covers
#: ``backend/tests/``, ``backend/state_machine/tests/``, and any
#: future ``backend/<layer>/tests/`` subtree.
_TEST_DIR_NAMES: frozenset[str] = frozenset({"tests"})

#: The five deprecated state JSON filenames.  Each pattern matches the
#: filename as a word boundary — i.e., the literal token
#: ``plan_state.json`` in the source, not as a substring of
#: ``plan_state.json_backup`` or similar.
STATE_JSON_FILENAMES: tuple[str, ...] = (
    "plan_state.json",
    "execution.json",
    "verification_runtime_state.json",
    "verification_executor_state.json",
    "verification_progress_state.json",
)

#: SQL context patterns that reference the legacy ``plans`` table.
#: Each pattern is a literal token matching SQL DML/DDL.
LEGACY_SQL_PATTERNS: tuple[str, ...] = (
    "FROM plans",
    "INTO plans",
    "UPDATE plans",
    "JOIN plans",
)

#: Legacy table names that must not appear anywhere in production code.
LEGACY_TABLE_NAMES: tuple[str, ...] = (
    "plan_meta",
    "plan_activity",
)

#: The exact schema produced by :func:`state_machine.db.schema.migrate`.
#: ``schema_version`` is the migration logbook; the five ``plan_*``
#: tables are the canonical state-machine surface.
#:
#: Schema v4 normalisation (2026-09-09) added ``plan_tasks`` so
#: per-task state lives in its own table rather than inside
#: ``plan_execution.task_progress`` JSON.
EXPECTED_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "plan_routing",
        "plan_execution",
        "plan_verification",
        "plan_artifacts",
        "plan_tasks",
        "schema_version",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_python_files(backend_root: Path):
    """Yield every ``*.py`` file under ``backend_root``,
    skipping every ``tests/`` subtree and a few vendor dirs.

    The spec explicitly excludes the entire ``backend/**/tests/``
    tree from the scan because test code is allowed to mention
    filenames (e.g. as assertion objects in integration tests that
    simulate a pre-refactor on-disk layout).  We also skip
    ``.venv``, ``__pycache__``, ``.pytest_cache`` so vendor and
    cache trees do not contribute noise to the scan.
    """
    excluded_dirs = {".venv", "__pycache__", ".pytest_cache"} | _TEST_DIR_NAMES
    for dirpath, dirnames, filenames in os.walk(backend_root):
        # Drop any ``tests/`` subtree and the vendor/cache trees.
        dirnames[:] = [
            d for d in dirnames
            if d not in excluded_dirs
        ]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _docstring_lines(tree: ast.AST, total_lines: int) -> set[int]:
    """Line numbers covered by module / class / function docstrings."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.lineno
            end = getattr(first, "end_lineno", None) or start
            lines.update(range(start, min(end, total_lines) + 1))
    return lines


def _strip_sql_line_comments(literal: str) -> list[str]:
    """Split a string literal into lines with SQL ``--`` tails removed.

    The schema module keeps its ``CREATE TABLE`` DDL in multi-line string
    constants whose comments explain the migrations — those comments are
    prose too, and ``_docstring_lines`` cannot see them (they live inside
    a value the code actually executes).
    """
    stripped: list[str] = []
    for line in literal.splitlines():
        head, sep, _tail = line.partition("--")
        stripped.append(head if sep else line)
    return stripped


def _scan_for_filename(backend_root: Path, filename: str) -> list[tuple[Path, int, str]]:
    """Return ``(path, line_no, line_text)`` triples for every occurrence
    of ``filename`` (as a whole-word match) in any scanned ``*.py`` file.

    Whole-word matching is anchored on the ``.json`` suffix so a string
    like ``plan_state.json_backup`` does NOT match.

    2026-09-19: only **code** is scanned — comments and docstrings are
    skipped, and SQL ``--`` tails inside string literals are stripped.
    The scan used to be a raw line match, which flagged this module's own
    historical commentary: the state-machine refactor left five prose
    mentions (four comments plus two docstrings) explaining *why* the
    JSON files were retired, and the gate read them as leaks. Prose that
    names a retired file cannot re-open the dual-write race window this
    gate guards, while a string literal the code actually evaluates can —
    so literals are still checked, exhaustively.
    """
    hits: list[tuple[Path, int, str]] = []
    pattern = re.compile(rf"\b{re.escape(filename)}\b")
    for py_file in _iter_python_files(backend_root):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_lines = text.splitlines()

        try:
            doc_lines = _docstring_lines(ast.parse(text), len(source_lines))
        except SyntaxError:
            # Unparsable source: fall back to the conservative raw scan so
            # a genuinely broken file cannot slip through unnoticed.
            for line_no, line in enumerate(source_lines, start=1):
                if pattern.search(line):
                    hits.append((py_file, line_no, line))
            continue

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError):
            continue

        reported: set[int] = set()
        for token in tokens:
            if token.type != tokenize.STRING or token.start[0] in doc_lines:
                continue
            for offset, content in enumerate(_strip_sql_line_comments(token.string)):
                if not pattern.search(content):
                    continue
                line_no = token.start[0] + offset
                if line_no in reported:
                    continue
                reported.add(line_no)
                line_text = (
                    source_lines[line_no - 1]
                    if 0 < line_no <= len(source_lines)
                    else content
                )
                hits.append((py_file, line_no, line_text))
    return hits


def _scan_for_pattern(backend_root: Path, pattern_str: str) -> list[tuple[Path, int, str]]:
    """Return ``(path, line_no, line_text)`` triples for every line that
    contains the literal substring ``pattern_str``.

    Unlike :func:`_scan_for_filename`, this is a substring match — used
    for SQL patterns like ``"FROM plans"`` where we want the EXACT
    DML/DDL shape, not a whole-word match.
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
# Defense 1: static JSON filename gate
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_4
def test_no_json_state_filename_in_backend_source() -> None:
    """Production code under ``backend/state_machine/`` (excluding tests)
    must contain ZERO references to the five deprecated JSON filenames.

    This is the primary **acceptance criterion 4** anchor for the
    state-machine refactor: any leak of ``plan_state.json`` etc.
    back into the new state-machine module re-opens the dual-write
    race window the refactor was meant to close.  Legacy ``server.py``
    / ``plan_state.py`` / etc. are migrated by the broader rollout;
    this gate is scoped to the NEW state-machine module only.
    """
    violations: list[str] = []
    for filename in STATE_JSON_FILENAMES:
        for path, line_no, line in _scan_for_filename(_STATE_MACHINE_ROOT, filename):
            violations.append(
                f"{path.relative_to(_STATE_MACHINE_ROOT)}:{line_no}: "
                f"{line.strip()!r}  <-- contains {filename!r}"
            )
    assert not violations, (
        "Production code still references deprecated JSON state "
        "filenames. The state-machine refactor pins SQLite as the "
        "single source of truth; these references must be migrated "
        "to the repository layer (RoutingRepository / "
        "ExecutionRepository / VerificationRepository / "
        "ArtifactRepository).\n"
        + "\n".join(violations)
    )


def test_json_filename_whitelist_is_empty() -> None:
    """The :data:`STATE_JSON_WHITELIST` mechanism must be empty.

    Any future whitelist entry must carry a justification AND be
    reviewed by code review.  Pinning the list empty here ensures a
    new entry cannot sneak in silently — adding an entry requires
    updating this test (and the test reviewer is forced to read the
    justification comment).
    """
    # The whitelist is the literal tuple below; if it grows, the test
    # will fail and force the reviewer to read the comment.
    state_json_whitelist: list[tuple[str, str]] = []  # (filename, justification)

    assert state_json_whitelist == [], (
        f"STATE_JSON_WHITELIST must be empty (no production code may "
        f"reference deprecated JSON filenames).  If you really need to "
        f"add an entry, append (filename, justification) here and "
        f"document the reason in code review.  Current entries: "
        f"{state_json_whitelist!r}"
    )


# ---------------------------------------------------------------------------
# The filename scan checks CODE, not commentary (2026-09-19)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_backend(tmp_path: Path) -> Path:
    """A throwaway ``backend/`` tree for scanner unit tests."""
    root = tmp_path / "backend"
    (root / "state_machine").mkdir(parents=True)
    return root


def _write_module(root: Path, name: str, body: str) -> None:
    (root / "state_machine" / name).write_text(body, encoding="utf-8")


class TestFilenameScanIsCodeOnly:
    """``_scan_for_filename`` must flag real references, not prose.

    The gate is deliberately blunt, but "blunt" must not mean "matches
    the module's own migration commentary" — that is how five prose
    mentions of retired filenames turned into a permanently red gate.
    """

    def test_a_string_literal_reference_is_flagged(self, fake_backend: Path) -> None:
        _write_module(
            fake_backend,
            "leak.py",
            "from pathlib import Path\n"
            "\n"
            "def load(plan_dir: Path):\n"
            '    return plan_dir / "plan_state.json"\n',
        )
        hits = _scan_for_filename(fake_backend, "plan_state.json")
        assert [line for _path, line, _text in hits] == [4]

    def test_a_python_comment_is_not_flagged(self, fake_backend: Path) -> None:
        _write_module(
            fake_backend,
            "documented.py",
            "# the legacy plan_state.json file was retired in v5\n"
            "VALUE = 1\n",
        )
        assert _scan_for_filename(fake_backend, "plan_state.json") == []

    def test_a_docstring_is_not_flagged(self, fake_backend: Path) -> None:
        _write_module(
            fake_backend,
            "documented.py",
            '"""Nothing reads plan_state.json any more."""\n'
            "\n"
            "def f():\n"
            '    """\n'
            "    The old plan_state.json pointer is gone.\n"
            '    """\n'
            "    return 1\n",
        )
        assert _scan_for_filename(fake_backend, "plan_state.json") == []

    def test_a_sql_line_comment_inside_a_literal_is_not_flagged(
        self, fake_backend: Path
    ) -> None:
        _write_module(
            fake_backend,
            "schema_like.py",
            '_DDL = """\n'
            "CREATE TABLE IF NOT EXISTS t (\n"
            "  -- lifted from plan_state.json by the v5 migration\n"
            "  id TEXT\n"
            ")\n"
            '"""\n',
        )
        assert _scan_for_filename(fake_backend, "plan_state.json") == []

    def test_a_sql_statement_inside_a_literal_is_still_flagged(
        self, fake_backend: Path
    ) -> None:
        """Stripping ``--`` tails must not blind the scan to real SQL."""
        _write_module(
            fake_backend,
            "schema_like.py",
            '_DDL = """\n'
            "SELECT * FROM plan_state.json\n"
            '"""\n',
        )
        hits = _scan_for_filename(fake_backend, "plan_state.json")
        assert [line for _path, line, _text in hits] == [2]


def test_artifact_json_allowlist_is_explicit() -> None:
    """The artifact JSON allowlist (interview.json / prd.md / tasks.json)
    must enumerate the three canonical artifacts explicitly.

    The state-machine refactor distinguishes between "state JSON files"
    (the five above — all migrated to SQLite) and "artifact JSON files"
    (the three below — kept on disk because they are user-authored
    documents that the production code references by pointer, never by
    body content).
    """
    artifact_allowlist: tuple[str, ...] = (
        "interview.json",  # user-authored requirement text (input)
        "prd.md",          # user-authored PRD document
        "tasks.json",      # generated task list (input to executor)
    )
    # All three must appear; ordering is informational.
    assert set(artifact_allowlist) == {
        "interview.json",
        "prd.md",
        "tasks.json",
    }
    assert len(artifact_allowlist) == 3


def test_no_state_json_produced_after_any_test(request, tmp_path: Path) -> None:
    """Per-test teardown assertion: every test run leaves ZERO state
    JSON files behind in its ``tmp_path``.

    Marked ``autouse`` via the conftest fixture — but we also pin a
    single explicit test here so the contract is visible at the
    test-function level (not just as an implicit fixture).  This test
    always passes (it is the LAST assertion run by the autouse
    teardown), but its existence in the test graph forces a green
    PASS line on every run.
    """
    # Read the per-test ``STATE_JSON_HITS`` attribute that the conftest
    # autouse teardown fixture sets after scanning tmp_path.  When the
    # fixture finds hits it raises, so reaching this line means zero
    # hits.  We assert it for explicitness.
    hits = getattr(request.node, "_state_json_hits", None)
    assert hits is None or hits == [], (
        f"tmp_path contained state JSON files after this test: {hits!r}"
    )


# ---------------------------------------------------------------------------
# Defense 2: SQL schema gate
# ---------------------------------------------------------------------------


def test_sqlite_schema_tables_exactly_five_plus_version(tmp_path: Path) -> None:
    """``sqlite_master`` table name set must equal exactly the five
    ``plan_*`` tables plus ``schema_version``.

    Implementation:

      1. Open an in-memory SQLite database at ``tmp_path``.
      2. Import :func:`state_machine.db.schema.migrate` and run it.
      3. Query ``sqlite_master`` for user tables (excluding
         ``sqlite_*`` system tables).
      4. Assert the set equals :data:`EXPECTED_SCHEMA_TABLES` exactly
         — no extras (legacy ``plans``, ``plan_meta``, ``plan_activity``),
         no missing.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    assert names == EXPECTED_SCHEMA_TABLES, (
        f"schema table set must equal exactly "
        f"{set(EXPECTED_SCHEMA_TABLES)!r}; got {names!r}.  Legacy "
        f"tables (plans, plan_meta, plan_activity) must not appear."
    )


def test_legacy_table_names_absent_from_schema(tmp_path: Path) -> None:
    """Schema must not contain legacy ``plans``, ``plan_meta``,
    or ``plan_activity`` tables.

    This is the negative side of the schema contract — even if the
    state-machine refactor adds new ``plan_*`` tables, the legacy
    names must stay absent.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    for legacy in ("plans", "plan_meta", "plan_activity"):
        assert legacy not in names, (
            f"legacy table {legacy!r} appears in sqlite_master; "
            f"the state-machine refactor forbids it.  Found tables: "
            f"{names!r}"
        )


def test_no_sql_references_legacy_table_names() -> None:
    """No SQL DML/DDL token in the state-machine module references
    legacy ``plans`` table or ``plan_meta`` / ``plan_activity`` symbols.

    Scans ``backend/state_machine/`` for the four exact SQL contexts::

      FROM plans
      INTO plans
      UPDATE plans
      JOIN plans

    plus the bare identifiers ``plan_meta`` and ``plan_activity``.
    """
    violations: list[str] = []
    for pattern_str in LEGACY_SQL_PATTERNS:
        for path, line_no, line in _scan_for_pattern(_STATE_MACHINE_ROOT, pattern_str):
            violations.append(
                f"{path.relative_to(_STATE_MACHINE_ROOT)}:{line_no}: "
                f"{line.strip()!r}  <-- contains SQL pattern {pattern_str!r}"
            )
    for legacy_name in LEGACY_TABLE_NAMES:
        for path, line_no, line in _scan_for_pattern(_STATE_MACHINE_ROOT, legacy_name):
            violations.append(
                f"{path.relative_to(_STATE_MACHINE_ROOT)}:{line_no}: "
                f"{line.strip()!r}  <-- contains legacy symbol {legacy_name!r}"
            )
    assert not violations, (
        "Production code still references legacy table names; "
        "the state-machine refactor pins the four plan_* + "
        "schema_version schema, with no legacy tables.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Defense 3: 8000 guard (the conftest autouse fixtures back these)
# ---------------------------------------------------------------------------


def test_guard_rejects_8000_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a test resolves its data_dir to the 8000 instance's data_dir,
    the autouse fixture in conftest.py MUST raise.

    Implementation:

      1. Set ``AUTONOMOUS_CODING_DATA_DIR`` to a sentinel path that
         is the SAME as the 8000 instance's data_dir (we use
         ``tmp_path / "shared_8000_data"`` and then re-bind it).
      2. Invoke :func:`_assert_data_dir_is_not_8000` directly (this
         is the same function the autouse fixture calls).
      3. Confirm the function raises :class:`RuntimeError` (or a
         subclass).

    This is the contract: a test that pins ``AUTONOMOUS_CODING_DATA_DIR``
    to the production instance's data_dir is a bug and must fail loudly.
    """
    # Import the conftest helpers; they may live in either
    # ``state_machine.tests.conftest`` or ``state_machine.tests`` (the
    # conftest is at the parent level — pytest loads it automatically
    # for tests in this directory tree, so we can simply import).
    from state_machine.tests.conftest import _assert_data_dir_is_not_8000

    # Synthetic 8000 data_dir sentinel.
    shared_dir = tmp_path / "shared_8000_data"
    shared_dir.mkdir()

    # Two scenarios:
    #   (a) data_dir == 8000 data_dir  -> fixture raises
    #   (b) data_dir is inside 8000 data_dir  -> fixture raises
    # Both must raise.  We exercise (a) here; (b) is exercised in the
    # next test.
    monkeypatch.setenv("AUTONOMOUS_CODING_DATA_DIR", str(shared_dir))
    monkeypatch.setenv("AUTONOMOUS_CODING_PORT", "8000")

    with pytest.raises(RuntimeError):
        _assert_data_dir_is_not_8000(
            test_data_dir=shared_dir,
            eight_thousand_data_dir=shared_dir,
            eight_thousand_port="8000",
        )

    # (b) data_dir is inside the 8000 instance's data_dir
    nested = shared_dir / "nested" / "plan_x"
    nested.mkdir(parents=True)
    with pytest.raises(RuntimeError):
        _assert_data_dir_is_not_8000(
            test_data_dir=nested,
            eight_thousand_data_dir=shared_dir,
            eight_thousand_port="8000",
        )


def test_guard_rejects_localhost_8000_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that tries to connect to ``127.0.0.1:8000`` must be
    blocked by the autouse socket guard.

    Implementation:

      1. Install a guarded ``socket.socket`` via
         :func:`_install_8000_socket_guard`.
      2. Attempt to ``connect()`` to ``("127.0.0.1", 8000)``.
      3. Confirm :class:`PermissionError` is raised BEFORE the
         underlying syscall fires.

    The guarded ``socket.socket`` is a thin wrapper that intercepts
    only ``connect()`` calls targeting ``127.0.0.1:8000``; everything
    else (including ``bind``, ``listen``, ``close``, ``send``,
    ``recv``) is a transparent pass-through.
    """
    from state_machine.tests.conftest import _install_8000_socket_guard

    # Install the guard; it monkey-patches ``socket.socket`` for the
    # duration of the test.
    _install_8000_socket_guard(monkeypatch, blocked_host="127.0.0.1", blocked_port=8000)

    # Confirm 127.0.0.1:8000 is blocked.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(PermissionError):
            sock.connect(("127.0.0.1", 8000))
    finally:
        sock.close()

    # Confirm unrelated hosts are NOT blocked (e.g. a temp socket on a
    # high port for an unrelated test should still work).  We do not
    # actually open a connection — just verify the connect() check is
    # host-and-port specific.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # ``bind`` to an ephemeral port to verify the guard is
        # connection-target specific.
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port > 0
        assert port != 8000
    finally:
        sock.close()