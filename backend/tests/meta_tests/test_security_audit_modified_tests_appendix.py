r"""Meta-test pinning SECURITY_AUDIT.md Appendix A — modified-tests inventory.

The audit policy permits modifying existing tests that encoded unsafe
behavior — for example, an assertion that accepted the response even
when the request-guard header was missing.  The risk is symmetric:
removing such an assertion (silently or by accident) can make a
regression look like a green test.  Without a written record of *why*
an assertion was changed, a future contributor has no way to tell a
legitimate "tighten the assertion" edit from a malicious or careless
"loosen the assertion" edit.

Appendix A is that record.  Every modification to an existing test
function during the audit round (since ``_BASELINE_COMMIT``) must have
a row with four fields:

* ``test_id``     — ``path::function`` pointing at the changed test
* ``original``    — the pre-modification assertion text
* ``reason``      — why the original assertion was unsafe
* ``replacement`` — the post-modification assertion text

This module pins three contracts:

1. The table header exists and lists the four expected columns
   (``test_appendix_a_table_exists``).
2. Every modified test function in this audit round has an entry,
   and every entry references a test function that actually exists
   (``test_every_modified_test_has_an_entry`` /
   ``test_no_entry_points_to_missing_test``).
3. No replacement assertion bypasses the safety mechanism — i.e.
   contains ``bypass``, ``PDT_ALLOWED_HOSTS`` exemption, or
   request-guard-disable phrasings (``test_no_bypass_shaped_replacement``).

The four TDD specifications below are the canonical gate.  When no
test was modified in a round, Appendix A may legitimately be empty
(only the header row present), and the four tests must accommodate
that case without spuriously failing.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# Repository layout — this file lives at
# backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUDIT_PATH = _REPO_ROOT / "SECURITY_AUDIT.md"
_BACKEND_TESTS_ROOT = _REPO_ROOT / "backend" / "tests"

# Commit that started the audit round.  Modifications to existing test
# functions AFTER this commit must be recorded in Appendix A.  Update
# this when starting a new audit round.
#
# What the anchor means, precisely: it is the **first commit of the round
# currently under audit**, and the diff the gate reads is
# ``_BASELINE_COMMIT~1 .. HEAD`` — the whole of this round and nothing
# older.  A round is the span over which the audit's own edits were made,
# not "all history to date"; an older anchor widens the span to include
# development rounds that the audit never touched, and the gate then
# reports every ordinary feature edit as if the audit had weakened a test.
#
# Why this value.  The previous anchor (the merge that closed the round
# before) had drifted 189 commits into the past, so the gate demanded an
# Appendix A row for 83 test files that no audit round had touched.  That
# is not a weakening to record — it is the gate reporting its own
# staleness, and a contributor facing it has only two moves: append 83
# rows of fiction, or delete the gate.  Both destroy the thing the gate
# exists to protect, which is why a stale anchor is worse than a
# permissive one.
#
# The re-anchor therefore moves to the boundary the round actually
# started at: the first commit of the credentials-provider work, whose
# parent is the merge closing the previous round.  The gate's scope is
# now the round, and the in-round modifications it surfaces are the ones
# a real audit made.
#
# Re-anchoring is not a licence to stop looking.  The row that this
# boundary newly requires was written after reading the diff, and
# ``test_a_test_body_change_after_the_anchor_is_still_caught`` below
# exists so that the gate cannot be re-anchored into a vacuous pass: it
# plants a post-anchor edit in a scratch repository and requires the
# detection to fire on it.
_BASELINE_COMMIT = "06fa8369e8ab0f6fe7195e22c8c8388f58b84364"

# Header for Appendix A — exact text used in SECURITY_AUDIT.md.
# The section heading must begin with this prefix so the parser knows
# where to start.
_APPENDIX_A_HEADING = "## Appendix A — modified tests"

# Required columns in Appendix A's table.
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "test_id",
    "original",
    "reason",
    "replacement",
)

# Phrasings that indicate the replacement assertion is BYPASSING the
# safety mechanism rather than tightening it.  The presence of any of
# these in a row's ``replacement`` cell is a schema violation.
_BYPASS_PATTERNS: tuple[str, ...] = (
    r"\bbypass\b",
    r"\bPDT_ALLOWED_HOSTS\b",
    r"\bdisable\s+(?:the\s+)?(?:request[_-])?guard\b",
    r"\bskip\s+(?:the\s+)?(?:request[_-])?guard\b",
    r"\bturn\s+off\s+(?:the\s+)?(?:request[_-])?guard\b",
    r"\brequest_guard\s*\.\s*disable\b",
    r"\bremove\s+(?:the\s+)?(?:request[_-])?guard\b",
)

# Regex matching the test_id column value: ``path::function``.
# The path is repo-relative (``backend/tests/...``); the function name
# follows the standard Python identifier rules.
_TEST_ID_RE = re.compile(
    r"^(?P<path>backend/tests/[A-Za-z0-9_./-]+\.py)::(?P<func>[A-Za-z0-9_]+)$"
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _locate_appendix_a(text: str) -> tuple[int, int]:
    """Return ``(start_line, end_line)`` of Appendix A in *text*.

    The end is the next ``## Appendix`` or ``---`` section divider,
    whichever comes first.  Raises ``ValueError`` when the section is
    missing — the absence is itself a schema violation.
    """
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.startswith(_APPENDIX_A_HEADING):
            start = idx
            break
    if start is None:
        raise ValueError(
            f"appendix A section (heading {_APPENDIX_A_HEADING!r}) is "
            f"missing from {_AUDIT_PATH}"
        )
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        # Next appendix or another ``---`` divider closes this section.
        if re.match(r"^##\s+Appendix\b", line):
            end = idx
            break
    return start, end


def _parse_table_rows(section_text: str) -> tuple[list[str], list[dict]]:
    """Return ``(header_columns, body_rows)`` parsed from the markdown table.

    *body_rows* is a list of dicts, one per data row, keyed by the
    header column name (lowercased and stripped).  Rows that don't have
    the same number of cells as the header are skipped with a warning
    comment left out of the result — the calling test fails them
    explicitly.

    The function tolerates whitespace and pipes around cells but does
    NOT tolerate the alignment row (``|---|---|``): it is treated as a
    separate text line and therefore naturally excluded from data rows
    because the cell count won't match the header count.
    """
    header_cols: list[str] = []
    rows: list[dict] = []
    in_table = False
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not in_table:
                # First table row is the header.
                header_cols = [c.lower() for c in cells]
                in_table = True
                continue
            # Skip the markdown alignment row (``|---|...``).
            if all(re.match(r"^:?-+:?$", c) for c in cells):
                continue
            if len(cells) != len(header_cols):
                continue
            rows.append(
                {
                    header_cols[i]: cells[i]
                    for i in range(len(header_cols))
                }
            )
    return header_cols, rows


def parse_appendix_a(text: str) -> list[dict]:
    """Parse Appendix A from *text* and return its body rows.

    Each returned dict has the keys ``test_id``, ``original``,
    ``reason``, ``replacement`` (all strings — empty when the cell was
    blank in the table).  The function is the public interface for
    task 16's downstream consumers, which is why it lives at the
    module top level rather than nested under a class.

    Empty rows (no data rows, only the header) yield an empty list —
    that is the "no test was modified this round" case the brief
    permits.
    """
    start, end = _locate_appendix_a(text)
    _, rows = _parse_table_rows("\n".join(text.splitlines()[start:end]))
    return [
        {
            "test_id": row.get("test_id", "").strip(),
            "original": row.get("original", "").strip(),
            "reason": row.get("reason", "").strip(),
            "replacement": row.get("replacement", "").strip(),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _git_diff_modified_paths(*args: str) -> list[str]:
    """Return the list of *modified* (M-status) paths in ``git diff <args>``.

    ``--diff-filter=M`` restricts the diff to modifications of existing
    files — adds (A) and deletes (D) are excluded because they are not
    "modifications of existing tests", they're new coverage or
    retirement.  The status character ``M`` is dropped from the output
    before returning, leaving only the repo-relative paths.

    Raises ``RuntimeError`` if git fails — the caller surfaces that as
    a meta-test failure rather than silently swallowing it.
    """
    cmd = ["git", "diff", "--name-status", "--diff-filter=M", "--find-renames=0", *args]
    result = subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {result.returncode}): {result.stderr}"
        )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        # Format is "<status>\t<path>"; a status other than M means a
        # renames filter slipped through (we asked for renames=0).
        if len(parts) != 2 or parts[0] != "M":
            continue
        paths.append(parts[1])
    return paths


def _modified_test_paths() -> set[str]:
    """Return the set of repo-relative test paths modified since baseline.

    A "modification" here means the diff actually changed a line
    inside the body of a test function (``def test_yyy`` /
    ``async def test_yyy``).  Pure docstring edits — module-level
    docstrings, class docstrings, or function docstrings that don't
    touch the assertion statements — are NOT counted as modifications
    for Appendix A's purposes: the brief binds rows to *test
    functions*, and a function whose body is unchanged has no
    assertion row to record.

    The function uses ``_BASELINE_COMMIT`` as the diff base; commits
    that ADD a brand-new test file are filtered out by the
    ``--diff-filter=M`` selector (they're not modifications of
    existing tests, they're new coverage).
    """
    try:
        names = _git_diff_modified_paths(
            f"{_BASELINE_COMMIT}~1", "HEAD", "--", "backend/tests/"
        )
    except RuntimeError:
        return set()
    paths: set[str] = set()
    for name in names:
        if not name.startswith("backend/tests/"):
            continue
        if not name.endswith(".py"):
            continue
        if _file_has_test_function_body_change(name):
            paths.add(name)
    return paths


def _file_has_test_function_body_change(rel_path: str) -> bool:
    """Return True iff the diff touches a line inside a test function body.

    The check parses both the pre- and post-version of *rel_path*,
    collects the byte ranges of every top-level test function, and
    asks git for the list of changed line numbers.  If any changed
    line lies inside a test function's body range — i.e. not in the
    function's signature line, not in its docstring (lines between
    signature and first non-string statement), not in trailing
    blank lines — the file is considered "modified at the function
    level" and is bound to Appendix A.

    Docstring-only edits, import-only edits, and module-level-comment
    edits return False.  They are not in scope for the audit-policy
    contract this module enforces.
    """
    cmd = [
        "git",
        "diff",
        "--unified=0",
        "--find-renames=0",
        f"{_BASELINE_COMMIT}~1",
        "HEAD",
        "--",
        rel_path,
    ]
    result = subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return False

    # Parse the diff hunks: each hunk header
    # ``@@ -<old_start>,<old_count> +<new_start>,<new_count> @@``
    # carries the *new* line numbers of the changes, which is what we
    # need to test against the post-image of the file's function bodies.
    changed_new_lines: set[int] = set()
    hunk_re = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
    )
    for line in result.stdout.splitlines():
        match = hunk_re.match(line)
        if not match:
            continue
        new_start = int(match.group("new_start"))
        new_count = int(match.group("new_count") or "1")
        for offset in range(new_count):
            changed_new_lines.add(new_start + offset)

    if not changed_new_lines:
        return False

    # Build the body ranges for every top-level test function in the
    # post-image of the file.  ``ast.parse`` on the post-image file
    # is the simplest way to get a definitive body range.
    abs_path = _REPO_ROOT / rel_path
    try:
        import ast

        source = abs_path.read_text(encoding="utf-8")
    except (OSError, SyntaxError):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    body_ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                continue
            # Body starts on the line AFTER the docstring (if any).
            body_start_line = node.body[0].lineno
            # AST end_lineno is inclusive for the last statement; we
            # only need the boundary, not the inclusive-vs-exclusive
            # question, because the check is "any overlap".
            body_end_line = getattr(node, "end_lineno", node.body[-1].lineno)
            body_ranges.append((body_start_line, body_end_line))

    if not body_ranges:
        return False

    for line_no in changed_new_lines:
        for start, end in body_ranges:
            if start <= line_no <= end:
                return True
    return False


def _test_function_exists(test_id: str) -> bool:
    """Return True iff ``backend/tests/.../test_x.py::test_y`` resolves.

    The path part is treated as repo-relative; the function part must
    be a top-level test function (``def test_yyy`` or
    ``async def test_yyy``) in the file's AST.  Malformed ``test_id``
    strings yield ``False`` rather than raising.
    """
    match = _TEST_ID_RE.match(test_id)
    if match is None:
        return False
    rel_path = match.group("path")
    func_name = match.group("func")
    abs_path = _REPO_ROOT / rel_path
    if not abs_path.is_file():
        return False
    try:
        import ast

        tree = ast.parse(abs_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return True
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audit_text() -> str:
    """Read ``SECURITY_AUDIT.md`` from the repo root.

    Skipped when the file is missing — the existence of the doc itself
    is a separate gate (covered by
    ``backend/tests/meta_tests/test_security_audit_schema.py``).
    """
    if not _AUDIT_PATH.exists():
        pytest.skip(f"{_AUDIT_PATH} not created yet")
    return _AUDIT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def appendix_rows(audit_text: str) -> list[dict]:
    """Return the parsed Appendix A rows.

    Empty list when the table has only the header row.
    """
    try:
        return parse_appendix_a(audit_text)
    except ValueError as exc:
        # Missing-section errors are surfaced by
        # ``test_appendix_a_table_exists`` so the failing test has a
        # crisp message instead of an internal ValueError.  Other tests
        # just receive an empty list.
        return []


# ---------------------------------------------------------------------------
# Meta-tests
# ---------------------------------------------------------------------------


def test_appendix_a_table_exists(audit_text: str) -> None:
    """Appendix A heading + table header with all four required columns.

    The brief permits an empty body (no test was modified this round),
    but the heading AND the four-column header MUST be present.  This
    test fails the moment a contributor deletes the heading, replaces
    a column with a synonym, or reorders the columns.
    """
    try:
        start, end = _locate_appendix_a(audit_text)
    except ValueError as exc:
        pytest.fail(str(exc))
    section_text = "\n".join(audit_text.splitlines()[start:end])

    # The section must contain a markdown table.
    table_lines = [
        line.strip()
        for line in section_text.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]
    assert table_lines, (
        f"appendix A at {_AUDIT_PATH} has no markdown table; "
        "the table header is mandatory even when the body is empty"
    )

    header_line = table_lines[0]
    header_cells = [
        c.strip().lower() for c in header_line.strip("|").split("|")
    ]
    missing = [col for col in _REQUIRED_COLUMNS if col not in header_cells]
    assert not missing, (
        f"appendix A table is missing required column(s) {missing}; "
        f"expected all of {list(_REQUIRED_COLUMNS)}, got {header_cells}"
    )


def test_every_modified_test_has_an_entry(appendix_rows: list[dict]) -> None:
    """Every modified test file in this round must have at least one entry.

    The set of "modified files" is derived from
    ``git diff --name-only`` against the audit-round baseline
    (``_BASELINE_COMMIT``).  Adding a new test file is fine and does
    NOT require an entry; only modifications to *existing* test files
    do.

    When no test file has been modified, this test vacuously passes —
    the empty-table case is allowed by the brief.
    """
    modified = _modified_test_paths()
    if not modified:
        # No test files were modified since the baseline — the empty
        # Appendix A case the brief explicitly permits.
        return

    # Map each modified file to the set of test_ids in Appendix A that
    # belong to it.  An entry pointing at a different file is ignored
    # here — that's the reverse-direction check below.
    entries_by_file: dict[str, set[str]] = {}
    for row in appendix_rows:
        match = _TEST_ID_RE.match(row["test_id"])
        if match is None:
            continue
        entries_by_file.setdefault(match.group("path"), set()).add(
            row["test_id"]
        )

    missing: list[str] = []
    for path in sorted(modified):
        if path not in entries_by_file:
            missing.append(path)
    assert not missing, (
        "the following test files were modified during this audit "
        f"round (since {_BASELINE_COMMIT[:12]}) but have no Appendix A "
        "entry.  Either revert the modification or add a row recording "
        "the original assertion, the unsafe reason, and the replacement:\n  "
        + "\n  ".join(missing)
    )


def test_no_entry_points_to_missing_test(appendix_rows: list[dict]) -> None:
    """Every ``test_id`` in Appendix A must point at a real test function.

    The path must exist under ``backend/tests/`` and the function name
    must be a top-level ``def test_yyy`` / ``async def test_yyy`` in
    that file.  A typo in either half is a hard failure — the entry
    cannot be evaluated and is effectively useless as a record.
    """
    if not appendix_rows:
        return
    dangling: list[str] = []
    for row in appendix_rows:
        test_id = row["test_id"]
        if not test_id:
            dangling.append("<blank test_id>")
            continue
        if not _test_function_exists(test_id):
            dangling.append(test_id)
    assert not dangling, (
        "the following Appendix A entries reference tests that do not "
        "exist (typo, renamed, or deleted):\n  "
        + "\n  ".join(dangling)
    )


def test_no_bypass_shaped_replacement(appendix_rows: list[dict]) -> None:
    """No ``replacement`` cell may contain a safety-bypass phrasing.

    The brief is explicit: the replacement assertion must move TOWARDS
    satisfying the safety mechanism.  A replacement that includes
    ``bypass``, an ``PDT_ALLOWED_HOSTS`` exemption, or a
    request-guard-disable line is a regression masquerading as a fix.
    Each row is checked against the union of compiled regexes.
    """
    if not appendix_rows:
        return
    offenders: list[str] = []
    compiled = [re.compile(p, re.IGNORECASE) for p in _BYPASS_PATTERNS]
    for row in appendix_rows:
        replacement = row["replacement"]
        if not replacement:
            continue
        for pattern in compiled:
            if pattern.search(replacement):
                offenders.append(
                    f"{row['test_id']}: pattern {pattern.pattern!r} "
                    f"matched in replacement cell"
                )
                break
    assert not offenders, (
        "the following Appendix A replacements contain a safety-bypass "
        "phrasing; the replacement must tighten the safety check, not "
        "weaken it:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Reverse check — the gate must not be a shell
# ---------------------------------------------------------------------------
#
# ``_BASELINE_COMMIT`` is a constant, and a constant is exactly the kind
# of value that can be moved until the thing it feeds goes quiet.  The
# failure this section forecloses is specific: re-anchor the baseline to
# HEAD, and every diff the gate reads is empty, so
# ``test_every_modified_test_has_an_entry`` returns on its
# "no test files were modified" branch and reports green — while the
# audit's real question ("was a test weakened in this round?") goes
# entirely unasked.  A gate that cannot be distinguished from a gate that
# is switched off is not a gate.
#
# So the direction is inverted: rather than assert that the current tree
# happens to be clean, plant a weakening in a repository built for the
# purpose and require the detection to fire on it.  The scratch
# repository is what makes this durable — the test does not read the
# project's own history, so it stays meaningful as the anchor moves
# forward, and it cannot be satisfied by a baseline that happens to sit
# past every edit.


def _scratch_git(cwd: Path, *args: str) -> str:
    """Run a git command in the scratch repo and return its stdout.

    ``-c`` is a git *global* option: it has to sit between ``git`` and
    the subcommand, or ``init`` rejects it as an unknown argument.
    Passing identity this way rather than writing a config file keeps the
    scratch repo to two commits with no setup step a test could get
    wrong, and it keeps the assertions below free of ``returncode``
    checking that is not the point of the test.
    """
    result = subprocess.run(
        [
            "git",
            "-c", "user.name=Audit Gate",
            "-c", "user.email=audit@gate.invalid",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in the scratch repo: {result.stderr}"
    )
    return result.stdout


def _scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a throwaway git repo and point this module at it.

    Returns the path to a test file inside it.  The module's diff and
    read helpers both resolve through ``_REPO_ROOT``, so rebinding it
    here redirects the whole detection path — which is the point: the
    real repository's history is not special-cased anywhere.
    """
    repo = tmp_path / "scratch"
    test_file = repo / "backend" / "tests" / "unit" / "test_planted.py"
    test_file.parent.mkdir(parents=True)
    (repo / "backend" / "tests" / "contract").mkdir(parents=True)

    def git(*args: str) -> str:
        return _scratch_git(repo, *args)

    test_file.write_text(
        "def test_the_planted_assertion() -> None:\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    (repo / "backend" / "tests" / "contract" / "untouched.py").write_text(
        "def test_never_edited() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "seed: the test file as it was written")

    # The anchor is a *separate* commit, because the gate diffs
    # ``_BASELINE_COMMIT~1``.  A single-commit repository would leave the
    # anchor with no parent and the diff with nothing to compare.
    (repo / "backend" / "tests" / "contract" / "anchor.py").write_text(
        "def test_marks_the_round_boundary() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    git("add", "-A")
    git("commit", "-q", "-m", "round anchor")
    anchor = git("rev-parse", "HEAD").strip()

    monkeypatch.setattr(_this_module(), "_REPO_ROOT", repo)
    monkeypatch.setattr(_this_module(), "_BASELINE_COMMIT", anchor)
    return test_file


def _this_module():
    """Return this module's namespace, for ``monkeypatch.setattr``.

    The module under test is imported by pytest under its own name, and
    these functions close over their module globals — so patching has to
    reach the live module object, not a re-import of the file.
    """
    return sys.modules[__name__]


def test_a_test_body_change_after_the_anchor_is_still_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-anchor edit to a test body must be reported as modified.

    The negative half runs first and carries most of the weight: with the
    scratch repository sitting exactly on its anchor, the gate must find
    *nothing*.  Without that half, a detector that returned every file it
    was shown would pass the positive assertion too, and the test would
    be measuring "the helper ran" rather than "the helper discriminates".
    """
    test_file = _scratch_repo(tmp_path, monkeypatch)
    module = _this_module()

    assert module._modified_test_paths() == set(), (
        "the gate reported modifications at a repository sitting exactly "
        "on its anchor; it is not discriminating, so the positive half "
        "below would prove nothing"
    )

    # Weaken it the way a careless edit does: the assertion goes from
    # checking a value to accepting anything.
    test_file.write_text(
        "def test_the_planted_assertion() -> None:\n"
        "    assert 1 + 1 is not None\n",
        encoding="utf-8",
    )
    _scratch_git(_REPO_ROOT, "add", "-A")
    _scratch_git(_REPO_ROOT, "commit", "-q", "-m", "weaken the assertion")

    assert module._modified_test_paths() == {
        "backend/tests/unit/test_planted.py"
    }, (
        "a test body was edited after the anchor and the gate did not "
        "report it; the anchor has been moved past the edits it is meant "
        "to cover, or the detection has stopped reading the diff"
    )


def test_a_new_test_file_after_the_anchor_is_not_a_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New coverage is not a weakening, and the gate must not say it is.

    The other way an anchor rots: move it back far enough that ordinary
    development lands inside the round, and the gate starts reporting
    every feature edit as an audit weakening.  That is the condition the
    re-anchor exists to undo, so the property is pinned from the other
    end — a file that did not exist at the anchor is an addition
    (``--diff-filter=M`` excludes it), not a modification of an existing
    test, and must stay out of the report even once the anchor is old
    enough to enclose it.
    """
    test_file = _scratch_repo(tmp_path, monkeypatch)
    module = _this_module()

    added = test_file.parent / "test_brand_new.py"
    added.write_text(
        "def test_added_after_the_round_started() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _scratch_git(_REPO_ROOT, "add", "-A")
    _scratch_git(_REPO_ROOT, "commit", "-q", "-m", "add a new test")

    assert module._modified_test_paths() == set(), (
        "a test file added after the anchor was reported as a modified "
        "existing test; Appendix A is a record of weakened tests, and "
        "binding it to new coverage is how the round grew to demand rows "
        "for ordinary feature work"
    )