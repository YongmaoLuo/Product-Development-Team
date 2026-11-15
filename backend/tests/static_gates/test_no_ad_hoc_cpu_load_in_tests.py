"""Static gate: the test tree and ``scripts/`` may not hand-roll CPU load.

Why this gate exists
--------------------
Task 1 gave this repository one sanctioned way to make the host busy:
``backend/tests/_load_rig.py::cpu_load`` -- real child processes, each
owning a counter slot, every one of them reclaimed when the block ends
(including when the block ends by raising).  Before that, a case that
needed a busy host built its own: a shell busy loop -- a ``while`` whose
condition is empty and whose body is the empty command -- started from
the test and never reaped.

On 2026-11-04 three verification sessions did that at once and the
machine's load average reached 109: the executor, the verification round
and the whole-suite gate could no longer run.  The load had become the
incident.

Task 1 fixed the symptom by making the sanctioned path the easy one.
This gate fixes the recurrence.  Without it the next person who needs a
busy host reaches for the shape they already know, and the repository
gains another ownerless loop -- one that a failed test leaves running
and that nothing in the suite can reclaim.  A rule that is only written
down is a rule that is only followed by whoever happens to read the
document.

What counts as a violation
--------------------------
A *shell* busy loop in a file the repository runs: the empty-condition
form (the condition and the body are both the empty command) and the
``true`` form.  Those two shapes are the ones that burn a core for as
long as nobody kills them, and they are the ones that were actually
written.  The Python equivalent is a different shape and is not what
this gate forbids.

Where the shape has to appear to count
--------------------------------------
A literal a file *mentions* is not a loop that *runs*, so the scanner
reports a hit only where the host language would execute it:

* **Python** -- inside a string literal, which is where a shell command
  handed to ``subprocess`` / ``os.system`` lives, but **not** inside a
  docstring.  A docstring is prose.  ``backend/tests/_load_rig.py`` and
  ``backend/tests/integration/test_load_rig_lifecycle.py`` both *name*
  the shape in their module docstrings, to say what the rig replaced;
  those mentions are documentation, and a gate that flagged them would
  push the next author into writing a vaguer docstring rather than into
  not writing the loop.
* **Shell and YAML** -- on a line that is not a comment.
* **Everything else** -- never.  A JSON fixture and a Markdown document
  cannot execute a shell loop, so a shape written into one is data or
  prose rather than load.

The rule is deliberately narrow about *where* it fires and deliberately
broad about *what* it fires on: only the two shapes above, anywhere a
host language would run them.

Scope
-----
This gate is static.  It says nothing about whether the host is free
enough to take the load -- that is the runtime question task 2 answers
in ``tests/_host_load.py`` -- and it deliberately does not depend on
it: a gate that could only run on an idle machine would be a gate that
never ran on the machine that needs it.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

# Locate the shared first-party source walker.  It is this file's own
# neighbour, and every sibling gate imports it the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan  # noqa: E402  (post-sys.path adjustment)


# ---------------------------------------------------------------------------
# The forbidden shapes
# ---------------------------------------------------------------------------

#: The canonical shell busy loop: an empty condition and an empty body.
#: Assembled from two fragments on purpose.  A gate must not contain the
#: literal it forbids -- it would match its own definition, and the two
#: conventions that keep the fragments apart (the pattern below and the
#: sample files the tests write) are what let the pattern and the
#: samples be the *same* constant.
SHELL = "while " + ":; do :; done"

#: The same loop with ``true`` as the condition.
TRUE = "while " + "true; do :; done"

#: The two shapes as compiled patterns.  ``re.escape`` makes each one a
#: literal search: the punctuation in a shell loop is punctuation in the
#: regex sense too, and an unescaped ``;`` would change what matches.
AD_HOC_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(re.escape(shape)) for shape in (SHELL, TRUE)
)

#: The same two shapes as plain strings, for the cheap "is this file
#: worth parsing at all" test that keeps the scan cheap.
_LITERALS: tuple[str, ...] = (SHELL, TRUE)


# ---------------------------------------------------------------------------
# The scan target
# ---------------------------------------------------------------------------

#: The roots the gate scans when the caller does not say otherwise: the
#: test tree (where a case would spawn its own load) and the scripts
#: tree (where a helper would spawn it for one).  Both are
#: repository-relative names, which :func:`source_scan.resolve_root`
#: resolves against the repository root -- never against the process
#: working directory, which CI varies (``working-directory: backend``).
DEFAULT_ROOTS: tuple[Path, ...] = (Path("backend/tests"), Path("scripts"))

#: File types whose contents a shell can run.  ``.sh`` is the shell
#: script itself; ``.yaml`` / ``.yml`` carry the command lines that CI
#: and the local runners execute.
_SHELL_SUFFIXES: frozenset[str] = frozenset({".sh", ".yaml", ".yml"})

#: The node types that own a docstring: the first statement of a module,
#: a class or a function is its docstring when it is a bare string.
_DOCSTRING_OWNERS: tuple[type, ...] = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def iter_scanned_files(roots: tuple[Path, ...] | None = None) -> list[Path]:
    """Every file the gate reads, under *roots*.

    *roots* defaults to :data:`DEFAULT_ROOTS`.  A relative root is
    resolved against the repository root (see
    :func:`source_scan.resolve_root`), and an absolute root is used as it
    stands -- which is how a caller points the scanner at a temporary
    directory holding a sample.

    The walker applies the repository's own exclusions, so ``.venv``,
    ``__pycache__`` and ``plans`` never appear in the result.
    """
    scan_roots = (
        DEFAULT_ROOTS
        if roots is None
        else tuple(Path(root) for root in roots)
    )
    return list(source_scan.iter_first_party_sources(roots=scan_roots))


def find_ad_hoc_busy_loops(
    roots: tuple[Path, ...] | None = None,
) -> list[tuple[Path, int]]:
    """Every hand-rolled busy loop under *roots*, as ``(path, lineno)``.

    The path is the one the walker yielded.  A reader is better served
    by :func:`source_scan.repo_relative`, which is what the gate's
    failure message uses -- the walker's paths carry the whole checkout
    prefix, and a failure message that prints it is noise at best.
    """
    hits: list[tuple[Path, int]] = []
    for path in iter_scanned_files(roots):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Most files do not contain either shape at all.  Reading them
        # is cheap; parsing them is not, and the tree holds thousands of
        # files.
        if not any(literal in text for literal in _LITERALS):
            continue
        for lineno in _violating_lines(path, text):
            hits.append((path, lineno))
    return hits


def _violating_lines(path: Path, text: str) -> list[int]:
    """The 1-based line numbers in *text* where a busy loop would run."""
    suffix = path.suffix
    if suffix == ".py":
        return _python_busy_loop_lines(text)
    if suffix in _SHELL_SUFFIXES:
        return _shell_busy_loop_lines(text)
    # Data and prose cannot execute a shell loop.
    return []


def _python_busy_loop_lines(text: str) -> list[int]:
    """The lines of a Python file where a shell busy loop would run.

    That is inside a string literal -- a bare loop cannot be written in
    Python -- and not inside a docstring, which is prose.
    """
    docstrings = _docstring_ranges(text)
    if docstrings is None:
        # The file does not parse, so its docstrings cannot be told
        # apart.  Fall back to the line-based reading, which is the
        # conservative one: it may report prose, but it cannot miss a
        # loop.
        return _shell_busy_loop_lines(text)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return _shell_busy_loop_lines(text)

    lines: list[int] = []
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        start = token.start[0]
        end = token.end[0]
        if any(start <= doc_end and doc_start <= end for doc_start, doc_end in docstrings):
            continue
        for pattern in AD_HOC_PATTERNS:
            for match in pattern.finditer(token.string):
                lines.append(start + token.string.count("\n", 0, match.start()))
    return lines


def _shell_busy_loop_lines(text: str) -> list[int]:
    """The lines of a shell file where a busy loop would run.

    A comment is prose, so a commented-out loop is not a loop.
    """
    lines: list[int] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        for pattern in AD_HOC_PATTERNS:
            if pattern.search(line):
                lines.append(index)
                break
    return lines


def _docstring_ranges(text: str) -> set[tuple[int, int]] | None:
    """The ``(start, end)`` line ranges of every docstring in *text*.

    ``None`` when the file does not parse -- the caller then cannot tell
    a docstring from any other string and has to be conservative.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    ranges: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            ranges.add((value.lineno, value.end_lineno or value.lineno))
    return ranges


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_is_not_empty() -> None:
    """The scan target must produce at least one file.

    A gate whose scan target has disappeared -- the roots renamed, the
    excluded-directory set swallowing the tree -- passes vacuously.  This
    test fails the moment that happens so the regression is visible.
    """
    files = iter_scanned_files()
    assert files, (
        "iter_scanned_files() returned no files -- the gate would now "
        "pass on an empty result. Either DEFAULT_ROOTS has drifted away "
        "from the real tree, or the walker's root resolution or "
        "exclusion set has."
    )


def test_repository_has_no_ad_hoc_cpu_load() -> None:
    """Full-tree scan: zero hand-rolled busy loops."""
    hits = find_ad_hoc_busy_loops()
    assert not hits, "\n".join(
        f"{source_scan.repo_relative(path)}:{lineno} hand-rolled CPU loop; "
        "use tests/_load_rig.py::cpu_load"
        for path, lineno in hits
    )


def test_scanner_matches_the_shell_shape(tmp_path: Path) -> None:
    """The canonical shell busy loop is reported."""
    sample = tmp_path / "x.sh"
    sample.write_text(SHELL + "\n", encoding="utf-8")

    hits = find_ad_hoc_busy_loops(roots=(tmp_path,))

    assert hits == [(sample, 1)]


def test_scanner_matches_the_true_variant(tmp_path: Path) -> None:
    """The same loop with ``true`` as the condition is reported."""
    sample = tmp_path / "x.sh"
    sample.write_text(TRUE + "\n", encoding="utf-8")

    hits = find_ad_hoc_busy_loops(roots=(tmp_path,))

    assert hits == [(sample, 1)]


def test_scanner_ignores_ordinary_loops(tmp_path: Path) -> None:
    """A Python loop is not a shell busy loop, and is not reported.

    The negative case exists so the gate cannot be "fixed" later by
    widening it into every loop-shaped thing in the tree: the rule is
    about the shell shape that burns a core with no owner, not about
    loops in general.
    """
    sample = tmp_path / "ordinary.py"
    sample.write_text("while True:\n    pass\n", encoding="utf-8")

    assert find_ad_hoc_busy_loops(roots=(tmp_path,)) == []


def test_scanner_reports_a_shell_loop_inside_a_python_string(tmp_path: Path) -> None:
    """A shell command built in Python is where the shape really lives.

    This is the form a test actually writes -- ``subprocess`` /
    ``os.system`` take the command as a string -- so a scanner that
    only understood shell scripts would miss the case the gate exists
    for.
    """
    sample = tmp_path / "runner.py"
    sample.write_text('cmd = "' + SHELL + '"\n', encoding="utf-8")

    hits = find_ad_hoc_busy_loops(roots=(tmp_path,))

    assert hits == [(sample, 1)]


def test_scanner_ignores_a_mention_in_a_docstring(tmp_path: Path) -> None:
    """A shape named in prose is not a shape that runs.

    The repository's own load rig names the shape in its docstring, to
    say what it replaced.  Reporting that would make the gate punish
    the explanation and leave the loop.
    """
    sample = tmp_path / "notes.py"
    sample.write_text('"""Never write ' + SHELL + ' in a test."""\n', encoding="utf-8")

    assert find_ad_hoc_busy_loops(roots=(tmp_path,)) == []