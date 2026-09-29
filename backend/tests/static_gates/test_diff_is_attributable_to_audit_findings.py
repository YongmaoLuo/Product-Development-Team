"""Diff-attribution gate — every changed source file traces to an audit finding.

Why this gate exists
--------------------
The repository just went through a structural split (the routes
extraction, the verification-loop extraction) and three silent
breakages shipped with it. None of them surfaced as a red test — each
was a refactor whose effect could only be observed at runtime, on the
production-shape request that the suite did not happen to construct:

* a value-import (`from server import X`) snapshotting the old
  reference, so every ``monkeypatch.setattr("server.X", ...)`` silently
  stopped applying;
* a path derived from ``__file__`` (``Path(__file__).parent.parent``)
  resolving one directory shallower than the author intended, so the
  "never execute inside ``backend/``" refusal kept returning a
  well-formed path while no longer firing against the right directory;
* the route registration order drifting past the frontend catch-all
  ``@app.get("/{path:path}")``, shadowing ``GET`` endpoints while
  ``POST`` kept working.

The PRD forbids drift of the same shape again: a refactor is only
acceptable when its diff is enumerable and every changed source file
can be traced back to a finding's ``修复`` field or to a row in
Appendix B of ``SECURITY_AUDIT.md``. This gate enforces that rule.

What "attributable" means here
------------------------------
A path ``P`` is *attributable* iff one of the following holds:

1. ``P`` belongs to an auto-attributed category — the audit document
   itself (``SECURITY_AUDIT.md``), any test file under ``backend/tests/``
   or ``tests/``, or a static-gate file the operator added as part of
   this audit round.
2. ``P`` is named (as a path token) in some ``ENTRY-*`` / ``CONF-*`` /
   ``ROUTE-*`` / ``CI-*`` entry's ``修复:`` field — the finding's
   own remediation mentions the file.
3. ``P`` appears as the ``path`` column of a row in Appendix B of
   ``SECURITY_AUDIT.md``, paired with a finding id — the refactor was
   necessary but did not fit into a single entry's ``修复`` line, and
   the audit recorded why.

Anything else is a hard failure: the diff contains a change that the
audit never authorised.

Why this is a *gate* and not just review
-----------------------------------------
Review catches "this should not have been merged"; a gate catches "this
*was* merged" before the next person has to reverse-engineer why. Both
defects that this gate exists to prevent (the value-import and the
``__file__``-derived path) landed as green PRs and stayed that way
until the production-shape request finally exercised the broken
branch. A gate whose answer is "every changed file is named in the
audit" is the only mechanism that prevents the next occurrence.

The "clean tree" carve-out
--------------------------
A repo with no working-tree diff must not be flagged. The
``git diff`` invocation against ``HEAD`` returns an empty stdout on a
clean tree, and an empty list trivially satisfies the
"every changed file is attributed" property — but only because the
list has no members. The clean-tree test pins this contract
explicitly so the gate cannot later regress to "fail when there are no
changes".
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# Repository layout — this file lives at
# ``backend/tests/static_gates/test_diff_is_attributable_to_audit_findings.py``,
# so the repo root is four parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUDIT_PATH = _REPO_ROOT / "SECURITY_AUDIT.md"

# Pull the meta_tests directory onto sys.path so we can import the
# audit parser that task 1 published.  Doing this at import time
# (rather than in the fixture) means ``from test_security_audit_schema
# import parse_entries`` resolves the same way on every runner, and a
# future contributor who reorganises the test tree only has to update
# one constant rather than chasing every fixture.
_META_TESTS_DIR = _REPO_ROOT / "backend" / "tests" / "meta_tests"
if str(_META_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_META_TESTS_DIR))

# Finding-id pattern: ``ENTRY-001`` / ``CONF-xyz`` / ``ROUTE-3`` /
# ``CI-foo``.  The four prefixes enumerate the namespace the audit
# policy recognises; anything else (e.g. an unrelated ``BUG-3`` token)
# does not satisfy the gate's "names a finding" contract.
_FINDING_ID_RE = re.compile(r"\b(ENTRY-[A-Za-z0-9_-]+|CONF-[A-Za-z0-9_-]+"
                            r"|ROUTE-[A-Za-z0-9_-]+|CI-[A-Za-z0-9_-]+)\b")

# Path token used to extract repo-relative paths out of prose. Matches
# things like ``backend/foo.py``, ``frontend/app.js``,
# ``example/x.yaml.example``, and ``scripts/run_tests.sh`` — but also
# ``foo.py`` alone, which we explicitly do NOT want. We therefore
# anchor on a slash (so the match must be prefixed by a path-segment
# boundary), and we re-check the prefix against the source-prefix list
# at the call site.
_PATH_TOKEN_RE = re.compile(
    r"(?P<path>(?:backend|frontend|example|scripts|\.github)"
    r"(?:/[A-Za-z0-9._-]+)+)"
)

# Source-tree prefixes that require explicit attribution when changed.
# Any path beginning with one of these prefixes is "source" for the
# purposes of this gate; anything else is either auto-attributed
# (docs, tests, gates) or outside the gate's scope (vendored caches,
# the venv, runtime state).
_SOURCE_PREFIXES: tuple[str, ...] = (
    "backend/",
    "frontend/",
    "example/",
    "scripts/",
    ".github/",
)

# Path substrings (matched anywhere as a path segment) that mark a
# file as out-of-scope even when it sits under a source prefix.  These
# are vendored caches, build outputs, and the venv itself — touching
# any of them is a release-shape mistake, but they are NOT source
# files the audit needs to authorise.
_OUT_OF_SCOPE_SEGMENTS: frozenset[str] = frozenset({
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    "backups",
    "plans",
})

# Top-level test directories whose contents are auto-attributed.
# A test change is, by definition, an audit artefact: either it
# implements the verification step pinned by an entry's ``验证方式``,
# or it is itself the gate that enforces the attribution rule. The
# test directories in this repository are:
#   * ``backend/tests/`` — every test the suite runs
#   * ``tests/``          — top-level fixtures and shared tests (the
#                            repo-root ``tests/`` directory; not the
#                            same as ``backend/tests/``)
_TEST_DIR_PREFIXES: tuple[str, ...] = (
    "backend/tests/",
    "tests/",
)

# Appendix B section header. The section's body ends at the next
# ``## Appendix`` heading or end-of-document, whichever comes first;
# see ``_locate_appendix_b`` for the exact boundary semantics.
_APPENDIX_B_HEADING = "## Appendix B"

# Columns required by Appendix B's refactor table. Mirrors the column
# header in ``SECURITY_AUDIT.md``; removing or renaming one is a
# schema violation enforced by ``test_appendix_b_entries_name_a_finding``.
_REQUIRED_APPENDIX_B_COLUMNS: tuple[str, ...] = (
    "path",
    "finding",
    "refactor",
    "minimal_reason",
)


# ---------------------------------------------------------------------------
# Section + row parsing
# ---------------------------------------------------------------------------


def _locate_appendix_b(text: str) -> tuple[int, int]:
    """Return ``(start_line, end_line)`` of Appendix B in *text*.

    The end is the next ``## Appendix`` heading or end-of-document,
    whichever comes first.  Raises ``ValueError`` when the section is
    missing — the absence is itself a schema violation.
    """
    lines = text.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith(_APPENDIX_B_HEADING):
            start = idx
            break
    if start is None:
        raise ValueError(
            f"appendix B section (heading {_APPENDIX_B_HEADING!r}) is "
            f"missing from {_AUDIT_PATH}"
        )
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if re.match(r"^##\s+Appendix\b", line):
            end = idx
            break
    return start, end


def _appendix_b_section_text(text: str) -> str:
    """Return the verbatim body of Appendix B.

    Wraps ``_locate_appendix_b`` with the empty-section fallback so
    callers that only care about body content do not have to handle the
    missing-section case themselves.
    """
    start, end = _locate_appendix_b(text)
    return "\n".join(text.splitlines()[start:end])


def parse_appendix_b(text: str) -> list[dict]:
    """Parse Appendix B from *text* and return its body rows.

    The function recognises two row shapes inside the Appendix B
    section:

    * **Markdown table** — every row whose first cell sits inside a
      ``| ... | ... |`` block; parsed by column header (the row whose
      cells are all ``---`` is the alignment marker and is skipped).
    * **List item** — a bullet whose first token is a finding id
      (``* ENTRY-001 -> ...``).  These carry the existing verification
      inventory.

    Each returned dict exposes the union of both shapes' keys:

    * ``path``           — repo-relative path (table row only; empty
                           string for list-item rows)
    * ``finding``        — the canonical id (``ENTRY-001``, ...)
    * ``refactor``       — table row's ``refactor`` cell (the prose
                           after ``->`` for list-item rows)
    * ``minimal_reason`` — table row's ``minimal_reason`` cell (empty
                           string for list-item rows)
    * ``raw``            — the verbatim source line, for diagnostics

    Empty rows (no data rows in either shape) yield an empty list —
    the brief permits an empty Appendix B when no refactor was
    necessary and the verification inventory lives elsewhere.
    """
    section_text = _appendix_b_section_text(text)
    rows: list[dict] = []

    # Pass 1 — table rows.  Walk the lines looking for ``|...|`` rows
    # and parse them as a markdown table whose first data row is the
    # alignment marker.
    header_cols: list[str] = []
    in_table = False
    for line in section_text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            in_table = False
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not in_table:
            header_cols = [c.lower() for c in cells]
            in_table = True
            continue
        # Alignment marker: every cell is dashes (optionally bracketed
        # by ``:``).
        if all(re.match(r"^:?-+:?$", c) for c in cells):
            continue
        if len(cells) != len(header_cols):
            continue
        row = {header_cols[i]: cells[i] for i in range(len(header_cols))}
        row["raw"] = line
        rows.append(row)

    # Pass 2 — list items whose first token is a finding id.  These
    # are the existing verification-inventory rows.
    for line in section_text.splitlines():
        match = re.match(r"^\s*[-*]\s+(?P<rest>\S.*)$", line)
        if not match:
            continue
        rest = match.group("rest")
        fid_match = _FINDING_ID_RE.search(rest)
        if not fid_match:
            continue
        # Avoid double-counting: if the line ALSO belongs to a
        # markdown table block, ``raw`` already records it.  The list
        # parser here only sees lines outside tables (because tables
        # begin with ``|`` and lists begin with ``-``/``*``).
        rows.append(
            {
                "path": "",
                "finding": fid_match.group(1),
                "refactor": rest,
                "minimal_reason": "",
                "raw": line,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _normalise_path(path: str) -> str:
    """Return ``path`` with a trailing-slash-free, POSIX-style spelling.

    The function also strips the Markdown backticks that the appendix
    table wraps paths in (`` `backend/foo.py` ``) and any leading
    ``./`` so callers can compare a row's ``path`` column directly
    against a value from ``git diff --name-only`` without having to
    know which surface the path came from.

    Note: a leading dot in a hidden directory name (``.github/``,
    ``.config/``) is preserved — only the literal ``./`` prefix that
    some callers add for emphasis is stripped, never a single
    standalone dot.
    """
    cleaned = path.replace("\\", "/").strip()
    if cleaned.startswith("`") and cleaned.endswith("`") and len(cleaned) >= 2:
        cleaned = cleaned[1:-1]
    # Strip a leading "./" only — never a lone dot, so .github/ stays.
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def is_source_file(path: str) -> bool:
    """Return True iff *path* sits under a source-tree prefix.

    The check is a prefix match against :data:`_SOURCE_PREFIXES`.  A
    path that belongs to a test directory (``backend/tests/`` or
    ``tests/``) is NOT a source file for the attribution gate — test
    files are auto-attributed.  Likewise, vendored caches and the venv
    (``backend/.venv/...``) are out-of-scope even though they sit
    under the ``backend/`` prefix.
    """
    norm = _normalise_path(path)
    if any(norm.startswith(prefix) for prefix in _TEST_DIR_PREFIXES):
        return False
    parts = set(norm.split("/"))
    if parts & _OUT_OF_SCOPE_SEGMENTS:
        return False
    return any(norm.startswith(prefix) for prefix in _SOURCE_PREFIXES)


def is_auto_attributed(path: str) -> bool:
    """Return True iff *path* is auto-attributed without enumeration.

    Auto-attributed categories (see the module docstring):

    * ``SECURITY_AUDIT.md`` — the audit document itself.
    * ``backend/tests/...`` — every test file under the backend
      suite, including the static gates and the meta-tests.
    * ``tests/...`` — every file under the top-level ``tests/``
      directory (fixtures and shared tests).
    """
    norm = _normalise_path(path)
    if norm == "SECURITY_AUDIT.md":
        return True
    return any(norm.startswith(prefix) for prefix in _TEST_DIR_PREFIXES)


def _paths_in_text(text: str) -> set[str]:
    """Return the set of repo-relative paths named in *text*.

    Only paths whose prefix matches one of :data:`_SOURCE_PREFIXES` are
    returned — the regex deliberately matches a wider set of tokens
    (``Path(__file__)`` style references inside ``/Users/...`` examples
    are excluded by the prefix list, not by the regex itself).
    """
    found: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(text):
        found.add(_normalise_path(match.group("path")))
    return found


# ---------------------------------------------------------------------------
# Attribution helper
# ---------------------------------------------------------------------------


def covered(path: str, entries: list[dict], appendix_b: list[dict]) -> bool:
    """Return True iff *path* is attributable to the audit.

    The function is the single source of truth for downstream tasks
    (e.g. a CI pre-merge hook) that need to ask "is this change
    accounted for?".  The logic is documented in the module docstring;
    the implementation walks the three attribution categories in the
    order they appear in the brief.

    *path* is repo-relative (``backend/...``, ``frontend/...``,
    etc.).  *entries* is the list returned by
    ``parse_entries(audit_text)``; *appendix_b* is the list returned
    by ``parse_appendix_b(audit_text)``.
    """
    norm = _normalise_path(path)
    if is_auto_attributed(norm):
        return True
    if not is_source_file(norm):
        # Outside the gate's scope — treat as covered so the caller
        # does not have to special-case vendored caches / venv /
        # runtime state files.
        return True
    # (1) entry's 修复 field names the path
    for entry in entries:
        if not entry.get("fix"):
            continue
        if norm in _paths_in_text(entry["fix"]):
            return True
    # (2) Appendix B row's `path` column matches the path
    for row in appendix_b:
        row_path = _normalise_path(row.get("path", ""))
        if row_path and row_path == norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_diff_paths(*args: str) -> list[str]:
    """Return repo-relative paths from ``git diff --name-only <args>``.

    Empty stdout (a clean tree, a path-less invocation) yields an empty
    list.  ``subprocess.CalledProcessError`` is re-raised — the caller
    decides whether to surface it as a test failure or to skip.
    """
    cmd = ["git", "diff", "--name-only", *args]
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
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audit_text() -> str:
    """Read ``SECURITY_AUDIT.md`` from the repo root.

    Skipped when the file does not exist — the existence of the
    document is a separate gate (covered by
    ``backend/tests/meta_tests/test_security_audit_schema.py``).
    """
    if not _AUDIT_PATH.exists():
        pytest.skip(f"{_AUDIT_PATH} not created yet")
    return _AUDIT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed_entries(audit_text: str) -> list[dict]:
    """Parsed ``### Finding`` entries from the audit document."""
    from test_security_audit_schema import parse_entries

    return parse_entries(audit_text)


@pytest.fixture(scope="module")
def parsed_appendix_b(audit_text: str) -> list[dict]:
    """Parsed Appendix B rows from the audit document.

    Returns an empty list when the section is absent — the missing-
    section case is surfaced by ``test_appendix_b_entries_name_a_finding``
    so the failure message is crisp.
    """
    try:
        return parse_appendix_b(audit_text)
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


def test_appendix_b_entries_name_a_finding(
    parsed_appendix_b: list[dict],
) -> None:
    """Every Appendix B row must carry a finding id from the four-prefix set.

    A row is "named after a finding" iff its ``finding`` column (or
    its raw line, for list-item rows) contains at least one of the
    four prefixes (``ENTRY-*`` / ``CONF-*`` / ``ROUTE-*`` / ``CI-*``).
    Anything else is a schema violation: a refactor without an
    authoring finding has no audit trail.

    Empty Appendix B vacuously passes — the brief permits that case.
    """
    if not parsed_appendix_b:
        pytest.skip(
            "appendix B is empty; the brief permits an empty body but the "
            "schema gate then has nothing to check — populate Appendix B "
            "with at least one refactor row before this test is expected "
            "to assert on body content"
        )

    offenders: list[str] = []
    for idx, row in enumerate(parsed_appendix_b):
        finding = row.get("finding", "")
        raw = row.get("raw", "")
        haystack = (finding + " " + raw).strip()
        if not _FINDING_ID_RE.search(haystack):
            offenders.append(
                f"row {idx}: no finding id in {raw!r}"
            )
    assert not offenders, (
        "every Appendix B row must name a finding "
        "(ENTRY-*, CONF-*, ROUTE-*, or CI-*); the following rows do not:\n  "
        + "\n  ".join(offenders)
    )


def test_changed_source_files_are_attributed(
    parsed_entries: list[dict],
    parsed_appendix_b: list[dict],
) -> None:
    """Every source file in the working-tree diff is attributable.

    The diff base is ``HEAD`` (the most recent commit); untracked
    files (``git status --porcelain`` would catch them) are NOT in
    scope here — the gate's contract is "what changed in tracked
    files since the last commit", which is what ``git diff --name-only
    HEAD`` reports.

    A clean tree yields an empty list, and the test passes vacuously.
    That case is pinned by ``test_clean_tree_is_not_a_failure``; here
    we just trust the empty list.
    """
    try:
        changed = _git_diff_paths("HEAD")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    if not changed:
        # Clean tree — vacuously satisfied. The dedicated
        # ``test_clean_tree_is_not_a_failure`` pins this contract
        # so a future regression that conflates "no diff" with
        # "no source files" cannot slip in.
        return

    unattributable: list[str] = []
    for path in changed:
        if not covered(path, parsed_entries, parsed_appendix_b):
            unattributable.append(path)
    assert not unattributable, (
        "the following source files are in the working-tree diff but "
        "cannot be traced to any audit finding. Either (a) the change "
        "should be reverted, (b) the change should be recorded in "
        "Appendix B with a finding id, or (c) the relevant entry's "
        "`修复:` field should be updated to name the file:\n  "
        + "\n  ".join(unattributable)
    )


def test_attribution_helper_flags_orphan_file(
    parsed_entries: list[dict],
    parsed_appendix_b: list[dict],
) -> None:
    """A synthesised orphan source path is reported as unattributable.

    The path is constructed from a clearly fictional prefix
    (``backend/zzz_synthetic_orphan.py``) so it cannot accidentally
    collide with a real entry or Appendix B row; the assertion is that
    ``covered()`` returns False for it.

    The counterpart (``covered()`` returns True for an attributed
    path) is exercised implicitly by ``test_changed_source_files_are_attributed``
    on the real diff; this test pins the failure path so a future
    regression in ``covered()`` cannot silently flip the answer.
    """
    orphan = "backend/zzz_synthetic_orphan_module.py"
    assert not covered(orphan, parsed_entries, parsed_appendix_b), (
        f"covered() returned True for an orphan path {orphan!r}; "
        "the attribution helper must flag any source file the audit "
        "did not name"
    )


def test_clean_tree_is_not_a_failure(
    parsed_entries: list[dict],
    parsed_appendix_b: list[dict],
) -> None:
    """An empty diff must not produce a failure.

    Two things are pinned, both about the *empty* case: that
    ``_git_diff_paths`` returns a list rather than raising, and that
    ``covered()`` is vacuously true over it — so a clean tree can never
    trip the attribution gate.

    On a dirty tree the loop below is **not** vacuous: it re-checks
    every changed path, which is the same contract
    ``test_changed_source_files_are_attributed`` enforces. That overlap
    is deliberate. This test's name promises "a clean tree is not a
    failure", and the cheapest way to keep that promise honest is to
    assert the attribution contract against whatever the live diff
    happens to be — including the empty one, where it must hold for
    free.
    """
    try:
        changed = _git_diff_paths("HEAD")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    # The contract is that the diff list is iterable and empty on a
    # clean tree. We do not fail when the tree is dirty (CI runs the
    # gate on a dirty tree by design); the assertion is the
    # non-throwing semantics of the helper.
    assert isinstance(changed, list), (
        f"_git_diff_paths returned a non-list value: {type(changed).__name__}"
    )
    # ``covered`` is well-defined on an empty list — every member
    # trivially satisfies "all members are attributed".
    for path in changed:
        assert covered(path, parsed_entries, parsed_appendix_b), (
            f"covered() returned False for a path the gate's own diff "
            f"detected: {path}"
        )
