"""The whole-delivery E2E is still a test module, and not a report about one.

Why this file exists
--------------------
``backend/tests/e2e/test_keychain_full_delivery_macos.py`` is the largest
module in the suite, and it has repeatedly been found standing on report
prose where its own source belongs: a heading, a list of changed files, a
verdict about a run, written over the run itself. The sibling gate
``test_keychain_macos_job_is_a_merge_gate.py`` already turns that
condition into a named failure at the moment the parser refuses the file.

This file covers the shape the sibling cannot. A clobber is not always a
syntax error. Prose that happens to parse — a heading the interpreter
reads as comments, a stray triple-quoted block, a file that keeps its
docstring and loses everything under it — reaches every other gate as a
clean ``ast.parse`` and a suite that has quietly stopped running its
cases. Three signatures catch that here: the opening lines, the line
count, and the number of cases the module still defines.

What this pins
--------------
1. The module's first statement is its own docstring, and that docstring
   is substantial rather than a placeholder.
2. The opening lines carry no diff-summary markers. A copied ``git
   diff`` summary lands there, because a summary is a single line by
   construction.
3. The module has not collapsed in size.
4. The module still defines its cases, which is the property a
   syntax-clean truncation loses first and most quietly.
5. Each floor clears what the module in this tree actually contains, so
   the floors are checks rather than decoration.
6. The sibling evidence gate still defines the checks it exists to
   provide, by name. The floors above are thresholds, and a gate that
   keeps its docstring and its size while dropping checks under it
   clears all of them.
7. The commit-hygiene gate does the same, for the same reason and by the
   same method. It is itself a parse-and-floor check, so the condition
   is the one it cannot see in itself.

Why the floors are calibrated
-----------------------------
Every threshold here is derived from a recorded reading by an operation
stated in the source, not written as a bare integer. A gate constant that
nobody can reconstruct is indistinguishable from one copied out of a
different file, and this subtree exists because unobserved numbers in
gates were read as measurements. Each derived constant carries the
reading that produced it and the slack that separates it from the
floor.

The slack is bounded by observations rather than chosen by taste: the
largest net line change a docstring edit has made to this module inside
the plan that produced this file, and the line counts corrupted copies
of it have been caught at. Both are recorded at their definitions, and
the tests below assert the relationships hold — so if either observation
is superseded, this file goes red rather than quietly asserting a stale
margin.

The repair
----------
``HEAD`` holds an intact copy of the module, so the file is one command
away from whole::

    git checkout HEAD -- backend/tests/e2e/test_keychain_full_delivery_macos.py

Restore it, then read the diff against HEAD before anything else is
committed. Editing the module until the check passes is how the evidence
of what happened gets destroyed: the working tree is the only place that
copy of the damage exists.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The sibling reader and its recovery command are imported rather than
# restated. This gate and that one meet the same condition, and two copies
# of a path are two things that rot independently — one of them would keep
# offering a restore of a file the other no longer reads. The equality is
# asserted rather than assumed, because an import proves only that the two
# names resolve, not that they mean the same thing forever.
from test_keychain_macos_job_is_a_merge_gate import (  # noqa: E402
    _E2E_RECOVERY_COMMAND as _SIBLING_RECOVERY_COMMAND,
)
from test_keychain_macos_job_is_a_merge_gate import (  # noqa: E402
    _parse_e2e_module as _SIBLING_READER,
)

# ---------------------------------------------------------------------------
# The module under guard
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]

_E2E_NAME = "test_keychain_full_delivery_macos.py"
_E2E_PATH = _REPO_ROOT / "backend" / "tests" / "e2e" / _E2E_NAME

_E2E_RECOVERY_COMMAND = (
    "git checkout HEAD -- backend/tests/e2e/test_keychain_full_delivery_macos.py"
)

#: What both coupled gates put in a failure, so one search finds either.
#: The first names the condition; the second is the remedy, and it has to
#: be the same remedy in both because the reader pastes whichever message
#: they met first.
_SHARED_ANCHORS = ("report prose", _E2E_RECOVERY_COMMAND)

# ---------------------------------------------------------------------------
# The readings every floor below is derived from
# ---------------------------------------------------------------------------

#: Lines in the module as this tree holds it, from ``wc -l``. The reading
#: survives the plan's docstring edits and is unchanged across commits
#: ``7e213e4`` and ``c3172c6``, so a reader can re-run it against either
#: and get this number back. It is what the line floor is derived from
#: rather than guessed at.
_E2E_MEASURED_LINES = 3462

#: The largest net line change one of those docstring edits made to the
#: module: +4 (commit ``fb419b5``, 7 insertions against 3 deletions). It
#: is what the slack has to absorb, so it is recorded rather than assumed
#: to be zero — a docstring edit that only ever appends is a claim, not
#: a bound.
_E2E_LARGEST_DOCSTRING_EDIT = 4

#: Slack between the reading and the line floor. Two orders of magnitude
#: above the largest edit observed above, and more than two orders of
#: magnitude below the floor itself: the corruptions this gate is for sat
#: at 5, 12, 20 and 34 lines, so no amount of legitimate editing reaches
#: from one side to the other.
_E2E_FLOOR_SLACK = 500

#: The narrowest gap between reading and floor this gate accepts. Derived
#: so it moves with the edits it exists to tolerate: tightening the
#: tolerance below what a legitimate edit has already needed would turn
#: the next docstring edit into a reported corruption, and a guard that
#: cries wolf over its own file is a guard that gets ignored.
_E2E_MIN_FLOOR_GAP = _E2E_LARGEST_DOCSTRING_EDIT * 50

_E2E_MIN_LINES = _E2E_MEASURED_LINES - _E2E_FLOOR_SLACK

#: Module-level ``def test_…`` functions in the module as this tree holds
#: it. Fewer than the cases pytest collects, because one of them is
#: parametrized; the floor is on definitions, which is the thing a
#: truncation removes.
_E2E_MEASURED_TEST_FUNCTIONS = 24
_E2E_MIN_TEST_FUNCTIONS = _E2E_MEASURED_TEST_FUNCTIONS // 2

#: Characters in the module's docstring as this tree holds it. Recorded so
#: the floor below is visibly two orders of magnitude under the reading
#: rather than a number that arrived from nowhere.
_E2E_MEASURED_DOCSTRING_CHARS = 47638
_E2E_MIN_DOCSTRING_CHARS = 200

#: Line counts this path has actually held in this repository while
#: standing on prose instead of tests. Every threshold above clears all of
#: them by orders of magnitude; the test below asserts it, so a later
#: tuning that narrows the floor cannot quietly reclassify one of them as
#: a legitimate size.
_CORRUPTED_LINE_COUNTS = (5, 12, 20, 34)

# ---------------------------------------------------------------------------
# The corruption signature in the opening lines
# ---------------------------------------------------------------------------

#: How many lines are read. A report's diff summary is a single line by
#: construction, so it lands at the top; five is enough to hold a heading
#: and the first bullets under it, and short enough that a legitimate
#: docstring cannot trip it by discussing the failure.
_HEADER_LINES = 5

#: Assembled from fragments so this file's own source does not carry a
#: line of report prose — which is the one thing it exists to keep out of
#: the repository, and a committed sample would be a sample in it.
_MARKER_HEADLINE = "Full diff" + ":"
_MARKER_INSERTIONS = "insertions" + "(+)"
_MARKER_DELETIONS = "deletions" + "(-)"
_MARKER_FILE_PREFIX = "FILE" + ":"
_HEADER_MARKERS = (
    _MARKER_HEADLINE,
    _MARKER_INSERTIONS,
    _MARKER_DELETIONS,
    _MARKER_FILE_PREFIX,
)

#: The summary line ``git diff`` prints, and the bullets a report wraps
#: around it. Both assembled, for the reason above.
_DIFF_SUMMARY = (
    "1 file changed, 4 " + _MARKER_INSERTIONS + ", 3457 " + _MARKER_DELETIONS
)
_REPORT_BODY = (
    "- the module now holds this instead of its cases\n"
    "- every gate that reads it is blind"
)
_MARKDOWN_REPORT = (
    "## Summary\n\n"
    "- files modified: backend/tests/static_gates/\n"
    "- test result: PASSED\n"
)

#: Filler long enough to clear the docstring floor above. Every sample
#: below wraps it in a docstring, so a sample is rejected for the
#: signature it is about rather than for having a short opening: the
#: corruption this gate catches replaced a long paragraph with a sentence,
#: and a sample that tripped two checks at once would prove nothing about
#: either.
_FILLER = (
    "Every case named below was executed and every case passed. The "
    "checks that read this module are still green, and they are green "
    "for the reason they have always been green: they are no longer "
    "reading the module they exist to read.\n"
)


def _as_docstring(body: str) -> str:
    """*body*, wrapped in a docstring a clobbered module would plausibly open with."""
    return (
        '"""Evidence recovered from one run, and nothing else.\n\n' + body + '"""\n'
    )


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def _truncate(line: str) -> str:
    """*line*, cut to 120 characters with the cut marked.

    A clobber can open with a single enormous line, and a message that
    quotes all of it is a message nobody reads. The same limit and the
    same marker the sibling gate uses, so one quoted line looks the same
    whichever gate produced it.
    """
    return line if len(line) <= 120 else line[:117] + "..."


def _header_markers(source: str) -> tuple[str, ...]:
    """The corruption markers present in the opening lines of *source*."""
    head = source.splitlines()[:_HEADER_LINES]
    found = [
        marker
        for marker in _HEADER_MARKERS
        if any(marker in line for line in head)
    ]
    if any(line.lstrip().startswith(_MARKER_FILE_PREFIX) for line in head):
        found.append(_MARKER_FILE_PREFIX)
    return tuple(found)


def _module_test_function_names(tree: ast.Module) -> frozenset[str]:
    """The names of the module-level functions in *tree* that read as cases.

    Module level only, and by definition rather than by collection: the
    property a truncation removes is the definition, and one nested in a
    class or a helper is not something pytest walks into on its own.
    """
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def _test_function_count(tree: ast.Module) -> int:
    """How many module-level functions in *tree* are named ``test_…``."""
    return len(_module_test_function_names(tree))


def _clobber(
    path: Path,
    lines: list[str],
    condition: str,
    cause: BaseException | None = None,
) -> AssertionError:
    """The failure for a module that is not the module it is supposed to be.

    Four things, in this order: what happened, what was observed so the
    reader can confirm it without opening the file, why reading it will
    not be enough, and the command that undoes it. The parse error, when
    there is one, is chained rather than restated — the text here is this
    gate's reading and the ``SyntaxError`` is the fact underneath it, so a
    reader who disbelieves the first still has the second.
    """
    head = lines[0].strip() if lines else ""
    error = AssertionError(
        f"{path} is not a test module: {condition}. That is what a module "
        f"overwritten with report prose looks like — a summary, a list of "
        f"files changed, a verdict about a run. Report prose is text about "
        f"the work; this path is where the work is. Observed: {len(lines)} "
        f"lines, first line {_truncate(head)!r}. Read the diff against HEAD "
        f"before restoring, so the copy of the damage survives the "
        f"decision. Restore the module and run this again:\n\n"
        f"    {_E2E_RECOVERY_COMMAND}\n"
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def _read_e2e_module(path: Path) -> ast.Module:
    """The AST at *path*, or a failure naming what the file turned out to be.

    Five checks, in the order a reader meets them. Parse first, because
    nothing below is meaningful for a file the interpreter refuses. Then
    the first statement, then the opening lines, then the two floors — the
    order a report's own residue appears in, from the most obvious
    evidence to the least.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise _clobber(
            path,
            lines,
            f"ast.parse refused it at line {exc.lineno}, column {exc.offset} "
            f"({exc.msg})",
            cause=exc,
        ) from exc

    first = tree.body[0] if tree.body else None
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        raise _clobber(
            path,
            lines,
            "its first statement is not a docstring, so what documents the "
            "module is not what opens it",
        )

    docstring = ast.get_docstring(tree) or ""
    if len(docstring) < _E2E_MIN_DOCSTRING_CHARS:
        raise _clobber(
            path,
            lines,
            f"its docstring is {len(docstring)} characters, under this "
            f"gate's floor of {_E2E_MIN_DOCSTRING_CHARS}",
        )

    markers = _header_markers(source)
    if markers:
        raise _clobber(
            path,
            lines,
            f"its opening {_HEADER_LINES} lines carry {list(markers)}",
        )

    if len(lines) < _E2E_MIN_LINES:
        raise _clobber(
            path,
            lines,
            f"it holds {len(lines)} lines, under this gate's floor of "
            f"{_E2E_MIN_LINES}, derived from the {_E2E_MEASURED_LINES} this "
            f"tree holds",
        )

    cases = _test_function_count(tree)
    if cases < _E2E_MIN_TEST_FUNCTIONS:
        raise _clobber(
            path,
            lines,
            f"it defines {cases} module-level test functions, under this "
            f"gate's floor of {_E2E_MIN_TEST_FUNCTIONS}, derived from the "
            f"{_E2E_MEASURED_TEST_FUNCTIONS} this tree holds",
        )

    return tree


# ---------------------------------------------------------------------------
# Assertions the negative controls share
# ---------------------------------------------------------------------------


def _assert_names_the_clobber(
    error: AssertionError, path: Path, source: str
) -> None:
    """Whatever signature produced this, the message has to name it.

    Asserted once and called from every control, because the controls are
    about *which* signature fires while this is about what the reader is
    left holding. Checking it in each of them would restate the same four
    assertions five times and let them drift apart in the process.
    """
    lines = source.splitlines()
    message = str(error)
    assert str(path) in message, (
        f"the message does not name the file it read: {message!r}. A reader "
        f"who cannot tell which file is broken cannot decide which one to "
        f"restore."
    )
    for anchor in _SHARED_ANCHORS:
        assert anchor in message, (
            f"the message carries no {anchor!r}. Message was: {message!r}"
        )
    assert f"{len(lines)} lines" in message, (
        f"the message does not report the {len(lines)} lines that were "
        f"actually there, so the reader is asked to take the diagnosis on "
        f"trust. Message was: {message!r}"
    )
    assert repr(_truncate(lines[0].strip())) in message, (
        f"the message does not quote what the first line contained, which "
        f"is what lets a reader confirm the diagnosis without opening a "
        f"file they are about to restore. Message was: {message!r}"
    )


def _message_from(reader, path: Path) -> str:
    """The text *reader* produces for the clobber at *path*."""
    with pytest.raises(AssertionError) as caught:
        reader(path)
    return str(caught.value)


# ---------------------------------------------------------------------------
# The floors clear what the tree contains, and stay far enough below it to
# mean something
# ---------------------------------------------------------------------------


def test_the_module_under_guard_opens_with_its_own_docstring() -> None:
    """The healthy path, and the one every negative control below departs from.

    Both halves are checked because either can go alone. A file can keep
    a docstring and lose the module, and it can keep the module and open
    with something that is not its docstring — the first is what a
    truncation looks like, the second is what an overwrite looks like.
    """
    tree = _read_e2e_module(_E2E_PATH)
    assert isinstance(tree, ast.Module), (
        f"{_E2E_NAME} returned {type(tree).__name__} from a reader built to "
        f"be total. A helper that can return something other than a module "
        f"is a helper whose callers cannot tell a clean read from a "
        f"swallowed failure."
    )
    docstring = ast.get_docstring(tree) or ""
    assert len(docstring) >= _E2E_MIN_DOCSTRING_CHARS, (
        f"{_E2E_NAME} opens with a docstring of {len(docstring)} characters, "
        f"below this gate's floor of {_E2E_MIN_DOCSTRING_CHARS}. A module "
        f"whose opening paragraph was written out carries the same first "
        f"statement as one that was not, so the shape check alone passes "
        f"for a file whose evidence is gone."
    )


def test_the_floor_clears_what_this_tree_measures() -> None:
    """The floor is a check against the module in this tree, not a constant.

    Reading the module and comparing it against the floor is the only
    thing that makes the floor mean anything. Asserting that the floor is
    a positive number would pass identically for a module overwritten
    with four lines of prose, which is the exact state this gate exists
    to make red.
    """
    source = _E2E_PATH.read_text(encoding="utf-8")
    lines = len(source.splitlines())
    assert lines >= _E2E_MIN_LINES, (
        f"{_E2E_NAME} holds {lines} lines; this gate's floor is "
        f"{_E2E_MIN_LINES}, derived from the {_E2E_MEASURED_LINES} this tree "
        f"contains. A module that lost its body is what that floor is for "
        f"— restore it rather than editing it:\n\n"
        f"    {_E2E_RECOVERY_COMMAND}\n"
    )
    cases = _test_function_count(ast.parse(source, filename=str(_E2E_PATH)))
    assert cases >= _E2E_MIN_TEST_FUNCTIONS, (
        f"{_E2E_NAME} defines {cases} module-level test functions; this "
        f"gate's floor is {_E2E_MIN_TEST_FUNCTIONS}, derived from the "
        f"{_E2E_MEASURED_TEST_FUNCTIONS} this tree contains. A truncation "
        f"survives a parser that a parse check cannot see, so a green parse "
        f"is not evidence that anything is still collected."
    )


def test_the_floor_sits_far_enough_below_the_reading_to_mean_something() -> None:
    """A floor pinned to the reading is a tautology.

    Such a floor holds for the module exactly as it is and for nothing
    else — including every docstring edit this plan made to it. The whole
    argument for a floor over an exact count lives in the gap between the
    two, so the gap is what gets asserted: a reader has to be able to
    see that the constant is derived from a reading rather than taken
    from its last digit.
    """
    gap = _E2E_MEASURED_LINES - _E2E_MIN_LINES
    assert gap >= _E2E_MIN_FLOOR_GAP, (
        f"the line floor ({_E2E_MIN_LINES}) sits {gap} lines below the "
        f"recorded reading ({_E2E_MEASURED_LINES}), and this gate requires "
        f"at least {_E2E_MIN_FLOOR_GAP}. A floor pinned to the reading "
        f"cannot absorb a docstring edit, so the moment one lands the gate "
        f"reports corruption for an intact module — which is how a guard "
        f"gets learned to be ignored."
    )


def test_the_floor_sits_above_every_corrupted_copy_seen_here() -> None:
    """The same margin, in the direction that catches.

    The corruptions this gate exists for were not all the same size, and
    a floor tuned to the smallest of them does nothing about the largest.
    Each count below is a line count this path has actually held in this
    repository, so the floor has to clear every one of them.
    """
    at_or_below = sorted(
        count for count in _CORRUPTED_LINE_COUNTS if _E2E_MIN_LINES <= count
    )
    assert not at_or_below, (
        f"the line floor is {_E2E_MIN_LINES}, which does not sit above every "
        f"corrupted copy of this module this repository has held "
        f"({sorted(_CORRUPTED_LINE_COUNTS)}; at or below the floor: "
        f"{at_or_below}). A floor that a known corruption clears is a floor "
        f"already shown not to catch it."
    )


# ---------------------------------------------------------------------------
# The negative controls: each signature is handed to the reader on its own
# ---------------------------------------------------------------------------


def test_a_diff_summary_where_the_docstring_belongs_is_a_clobber(
    tmp_path: Path,
) -> None:
    """The shape a report takes when it overwrites a module.

    The first line is the summary line ``git diff`` prints, copied
    verbatim, which is why it is the first thing a reader looks at after
    the failure names the file. Assembled from fragments at run time so
    this file's own source does not carry a report.
    """
    source = _DIFF_SUMMARY + "\n" + _REPORT_BODY + "\n"
    clobbered = tmp_path / _E2E_NAME
    clobbered.write_text(source, encoding="utf-8")

    # The sample has to be what this test claims, or every assertion below
    # holds for a reason that has nothing to do with the header check.
    first_line = source.splitlines()[0]
    assert _MARKER_INSERTIONS in first_line and _MARKER_DELETIONS in first_line, (
        f"the sample's first line is {first_line!r} and no longer carries "
        f"the markers this test drives, so the reader could reach the "
        f"right verdict for the wrong reason."
    )

    with pytest.raises(AssertionError) as caught:
        _read_e2e_module(clobbered)

    _assert_names_the_clobber(caught.value, clobbered, source)


def test_report_prose_behind_a_real_docstring_is_a_clobber(
    tmp_path: Path,
) -> None:
    """A docstring that quotes a diff summary is still a clobber.

    The signature check reads the opening lines rather than the first
    statement alone, and this sample is what makes that difference
    observable: the file parses, its first statement is a docstring, and
    its second line names a mass deletion. A reader that stopped at the
    first statement would pass it.
    """
    source = _as_docstring(
        _DIFF_SUMMARY + "\n" + _MARKER_HEADLINE + "\n" + _FILLER
    )
    clobbered = tmp_path / _E2E_NAME
    clobbered.write_text(source, encoding="utf-8")

    # Parses, and opens with a docstring: asserted, not assumed, because
    # those are the two things this sample is supposed to get past.
    parsed = ast.parse(source)
    assert isinstance(parsed.body[0], ast.Expr), (
        "the sample no longer opens with a docstring, so passing it would "
        "prove nothing about the opening-lines check"
    )

    with pytest.raises(AssertionError) as caught:
        _read_e2e_module(clobbered)

    _assert_names_the_clobber(caught.value, clobbered, source)


def test_a_module_that_collapsed_in_size_is_a_clobber(tmp_path: Path) -> None:
    """The signature the sibling gate cannot reach.

    Everything here parses, the first statement is a docstring, and no
    opening line carries a marker — a report whose bullets were all
    comments, or a truncation that kept the header. What is left to notice
    it is the size of the file, which is why that is a floor and not a
    convenience.
    """
    source = _as_docstring(_FILLER) + "\ndef helper() -> None:\n    return None\n"
    clobbered = tmp_path / _E2E_NAME
    clobbered.write_text(source, encoding="utf-8")

    parsed = ast.parse(source)
    assert isinstance(parsed.body[0], ast.Expr), (
        "the sample no longer opens with a docstring, so passing it would "
        "prove nothing about the size floor"
    )
    assert _header_markers(source) == (), (
        f"the sample's opening lines now carry a marker "
        f"{_header_markers(source)}, so passing it would prove nothing "
        f"about the size floor"
    )
    assert len(source.splitlines()) < _E2E_MIN_LINES, (
        "the sample is not below the line floor any more, so passing it "
        "would prove nothing about the size floor"
    )

    with pytest.raises(AssertionError) as caught:
        _read_e2e_module(clobbered)

    _assert_names_the_clobber(caught.value, clobbered, source)


def test_a_module_that_kept_its_size_but_lost_its_cases_is_a_clobber(
    tmp_path: Path,
) -> None:
    """The same collapse, moved: large enough, empty of tests.

    The line floor is a floor, so a clobber that padded itself out to
    clear it is not a shape this gate has to imagine — it is what "edit
    the file until the gate goes quiet" produces when the reader trusts
    the wrong check. The case count is the independent reading, and this
    sample is what says the two are wired independently.
    """
    source = _as_docstring(_FILLER) + ("x = 1\n" * _E2E_MIN_LINES)
    clobbered = tmp_path / _E2E_NAME
    clobbered.write_text(source, encoding="utf-8")

    assert len(source.splitlines()) >= _E2E_MIN_LINES, (
        "the sample is not above the line floor, so passing it would prove "
        "nothing about the case-count floor"
    )

    with pytest.raises(AssertionError) as caught:
        _read_e2e_module(clobbered)

    _assert_names_the_clobber(caught.value, clobbered, source)


def test_a_module_that_does_not_parse_still_fails_by_name(
    tmp_path: Path,
) -> None:
    """The sibling's shape reaches this reader too, and reads the same way.

    A heading the interpreter reads as a comment, bullets on the lines
    below it. The sibling gate names this condition for its own callers;
    a reader who has met one gate's message and then hits the other
    should not have to learn a second vocabulary.
    """
    source = _MARKDOWN_REPORT
    clobbered = tmp_path / _E2E_NAME
    clobbered.write_text(source, encoding="utf-8")
    with pytest.raises(SyntaxError):
        ast.parse(source)

    with pytest.raises(AssertionError) as caught:
        _read_e2e_module(clobbered)

    _assert_names_the_clobber(caught.value, clobbered, source)
    assert isinstance(caught.value.__cause__, SyntaxError), (
        f"the parse error is not chained underneath this gate's message "
        f"({caught.value.__cause__!r}). The message is a reading; the "
        f"SyntaxError is the evidence, and a reader who disbelieves the "
        f"first has to be able to fall back to the second."
    )


# ---------------------------------------------------------------------------
# The two coupled gates have to say the same thing
# ---------------------------------------------------------------------------


def test_both_coupled_gates_name_the_same_condition(tmp_path: Path) -> None:
    """One condition, two readers, one vocabulary.

    This gate and the merge-gate sibling both read this module, and both
    can meet the clobber. When they do, the reader has to be able to
    search for one message and find both — and the recovery command has
    to be the same literal in both, because whichever one the reader meets
    first is the one they will paste into a terminal.

    Driven against one sample through both readers rather than compared as
    strings: two constants that agree today drift apart the day the file
    is renamed, and only driving both shows what each one actually says.
    """
    clobbered = tmp_path / _E2E_NAME
    clobbered.write_text(_MARKDOWN_REPORT, encoding="utf-8")

    ours = _message_from(_read_e2e_module, clobbered)
    theirs = _message_from(_SIBLING_READER, clobbered)

    for anchor in _SHARED_ANCHORS:
        assert anchor in ours, (
            f"this gate's message does not carry the anchor {anchor!r}, so "
            f"the two coupled gates name the same condition in two "
            f"vocabularies. Message was: {ours!r}"
        )
        assert anchor in theirs, (
            f"the sibling gate's message does not carry the anchor "
            f"{anchor!r}. One of the two gates has to change and this test "
            f"is which one: both messages and the anchor list have to "
            f"agree. Message was: {theirs!r}"
        )

    assert _E2E_RECOVERY_COMMAND == _SIBLING_RECOVERY_COMMAND, (
        f"the two coupled gates quote different recovery commands "
        f"({_E2E_RECOVERY_COMMAND!r} and {_SIBLING_RECOVERY_COMMAND!r}). "
        f"Both are literals, which is what makes them pasteable and also "
        f"what makes them rot, so the equality is asserted here rather "
        f"than assumed."
    )


def test_the_recovery_command_names_the_module_this_gate_reads() -> None:
    """A remedy pointing at a renamed file is worse than no remedy.

    The command is a literal, which is what makes it pasteable and also
    what makes it rot. Asserting it against the path this gate actually
    reads turns that rot into a red test instead of a restore that
    quietly brings back nothing.
    """
    relative = _E2E_PATH.relative_to(_REPO_ROOT).as_posix()
    assert _E2E_RECOVERY_COMMAND == f"git checkout HEAD -- {relative}"


def test_the_docstring_quotes_the_recovery_command() -> None:
    """A remedy that lives in one run's output is gone when the run is.

    The next reader has this file, not a traceback they can scroll, so
    this is where the command has to be written down.
    """
    docstring = ast.get_docstring(
        ast.parse(Path(__file__).read_text(encoding="utf-8"))
    )
    assert docstring, "this module has no docstring to point a reader at"
    assert _E2E_RECOVERY_COMMAND in docstring, (
        f"this module's docstring does not quote the recovery command. The "
        f"failure message carries it, but a message is a property of one "
        f"run and the docstring is what survives to the next reader."
    )


# ---------------------------------------------------------------------------
# The sibling gate, read the same way — the half a floor cannot reach
# ---------------------------------------------------------------------------

#: The evidence gate, named here rather than imported. It is read
#: through ``ast`` for the same reason the E2E module is: a checker
#: that imports its subject is checking a copy, and the copy passes
#: identically when the file is gone.
_EVIDENCE_NAME = "test_e2e_docstring_labels_its_evidence.py"
_EVIDENCE_PATH = _REPO_ROOT / "backend" / "tests" / "static_gates" / _EVIDENCE_NAME

#: The same remedy in the same shape as the one above, for the file this
#: gate reads next. Two literals, so the equality is asserted below
#: against the path rather than assumed.
_EVIDENCE_RECOVERY_COMMAND = (
    f"git checkout HEAD -- backend/tests/static_gates/{_EVIDENCE_NAME}"
)

#: The checks that gate has to keep providing. A subset, not an
#: equality: an addition later is a gain and stays green, while a
#: deletion, a rename, or a truncation that drops one goes red and says
#: which.
_REQUIRED_EVIDENCE_CHECKS: frozenset[str] = frozenset({
    "test_a_bare_count_with_no_command_is_reported_with_its_line_number",
    "test_a_count_that_disagrees_with_the_re_measurement_quotes_both_numbers",
    "test_a_counts_line_in_inline_code_is_a_mention_not_a_reading",
    "test_a_historical_red_with_no_label_is_a_violation",
    "test_a_non_execution_claim_without_a_captured_refusal_is_a_violation",
    "test_a_reading_attached_to_a_command_is_not_reported",
    "test_every_reading_in_the_module_names_the_command_that_produced_it",
    "test_the_module_quotes_a_refusal_its_own_probe_can_produce",
    "test_the_re_measured_counts_are_all_still_stated",
    "test_the_recorded_red_is_still_written_down",
    "test_the_scan_reaches_the_module_and_finds_its_readings",
})

#: Names no required set mentions, standing in for a check a later
#: commit adds. They are the counterweight to the subset decision: a
#: gate that rejected them would be an equality wearing a subset's
#: name, and the next legitimate addition would go red with it.
_LATER_EVIDENCE_CHECKS: tuple[str, ...] = (
    "test_a_check_added_after_this_gate_was_written",
    "test_another_check_added_later",
    "test_a_third_check_added_later",
    "test_a_fourth_check_added_later",
)


def _evidence_gate_report(tree: ast.Module) -> str:
    """Every required check *tree* no longer defines, as one report.

    A subset, because the check is whether the file still provides the
    checks it exists to provide — not whether it provides only those. A
    twelfth check is a gain and reporting it would make the next
    legitimate addition a red test nobody can act on.

    Each entry names the check, and the report carries the command that
    restores the file, because a reader who has lost a check is better
    served by the repair than by the shape of the problem. The empty
    string is the healthy answer, and it is the only healthy answer this
    report has: a report that cannot be shown to fail is a report that
    says nothing ever.
    """
    missing = sorted(
        _REQUIRED_EVIDENCE_CHECKS - _module_test_function_names(tree)
    )
    if not missing:
        return ""
    return (
        f"{_EVIDENCE_NAME} no longer provides {len(missing)} of the "
        f"{len(_REQUIRED_EVIDENCE_CHECKS)} checks it has to keep defining: "
        + ", ".join(missing)
        + ". No floor above can see this — a file that keeps its docstring, "
        "its size and half its cases clears all of them, and what is left "
        "to notice it is the name of each check. Restore the gate rather "
        "than editing the required set above, or the next deletion is "
        "declared correct:\n\n"
        f"    {_EVIDENCE_RECOVERY_COMMAND}\n"
    )


def test_the_evidence_gate_still_defines_every_check_it_exists_to_provide() -> None:
    """The other half of the shape: this subtree's gates stay test modules.

    Every floor above is a threshold, and a threshold is blind to what it
    divides. A sibling gate that keeps its docstring, keeps its size, and
    drops the checks under it clears every one of them, and the suite goes
    quiet over a file that is no longer doing the work it was written
    for. So the checks are pinned by name: the report is empty for the
    gate as this tree holds it, and a module that clears all four floors
    while missing one of them is reported by name and handed the repair.
    """
    relative = _EVIDENCE_PATH.relative_to(_REPO_ROOT).as_posix()
    assert _EVIDENCE_RECOVERY_COMMAND == f"git checkout HEAD -- {relative}", (
        f"the recovery command names {_EVIDENCE_RECOVERY_COMMAND!r} rather "
        f"than the path this gate reads ({relative}). A remedy pointing at a "
        f"renamed file is worse than no remedy, because it is a command that "
        f"restores nothing and reads as a fix."
    )

    report = _evidence_gate_report(
        ast.parse(
            _EVIDENCE_PATH.read_text(encoding="utf-8"),
            filename=str(_EVIDENCE_PATH),
        )
    )
    assert report == "", report

    # The counterweight. A specimen shaped to clear every floor above and
    # missing exactly one required check, plus four names no required set
    # mentions. If the report is driven by the deletion it names that
    # check and nothing else; if the extras were reported, the subset
    # above is an equality and the next legitimate addition goes red.
    dropped = "test_the_recorded_red_is_still_written_down"
    sample = (
        _as_docstring(_FILLER)
        + "".join(
            f"\ndef {name}() -> None:\n    return None\n"
            for name in sorted(
                set(_REQUIRED_EVIDENCE_CHECKS - {dropped})
                | set(_LATER_EVIDENCE_CHECKS)
            )
        )
        + "x = 1\n" * _E2E_MIN_LINES
    )
    parsed = ast.parse(sample)
    first = parsed.body[0]

    assert (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ), (
        "the sample no longer opens with a docstring, so reporting it would "
        "prove nothing about the check it is missing"
    )
    assert len(ast.get_docstring(parsed) or "") >= _E2E_MIN_DOCSTRING_CHARS, (
        "the sample's docstring fell under the floor, so it would be "
        "reported for its opening and never for the check it is missing"
    )
    assert not _header_markers(sample), (
        f"the sample's opening lines now carry a marker "
        f"{_header_markers(sample)}, so it would be reported for its opening"
    )
    assert len(sample.splitlines()) >= _E2E_MIN_LINES, (
        "the sample is under the line floor, so it would be reported for its "
        "size and never for the check it is missing"
    )
    assert _test_function_count(parsed) >= _E2E_MIN_TEST_FUNCTIONS, (
        "the sample defines too few cases to clear the case-count floor, so "
        "it would be reported for its count and never for the check it is "
        "missing"
    )

    sample_report = _evidence_gate_report(parsed)
    assert dropped in sample_report, (
        f"a module that clears every floor and is missing {dropped!r} was "
        f"not reported by name, so the pin above cannot fail and is "
        f"decoration. Report was: {sample_report!r}"
    )
    assert _EVIDENCE_RECOVERY_COMMAND in sample_report, (
        f"the report names the missing check but carries no recovery "
        f"command, so a reader is told the symptom and not the repair. "
        f"Report was: {sample_report!r}"
    )
    for name in _REQUIRED_EVIDENCE_CHECKS - {dropped}:
        assert name not in sample_report, (
            f"{name!r} is present in the sample and is being reported "
            f"anyway, so the required set is being compared for equality. "
            f"Report was: {sample_report!r}"
        )
    for name in _LATER_EVIDENCE_CHECKS:
        assert name not in sample_report, (
            f"{name!r} is not in the required set and is being reported "
            f"anyway, so a check added by a later commit would go red here. "
            f"Report was: {sample_report!r}"
        )


# ---------------------------------------------------------------------------
# The commit-hygiene gate, read the same way — the floor a count cannot reach
# ---------------------------------------------------------------------------

#: The commit-hygiene gate, named here rather than imported, for the reason
#: the evidence gate above is: a checker that imports its subject is
#: checking a copy, and the copy passes identically when the file is gone.
_TRAILER_NAME = "test_commit_messages_are_not_ai_attributed.py"
_TRAILER_PATH = _REPO_ROOT / "backend" / "tests" / "static_gates" / _TRAILER_NAME

#: The same remedy in the same shape as the two above, for this file. A
#: literal, so the equality against the path is asserted below rather
#: than assumed.
_TRAILER_RECOVERY_COMMAND = (
    f"git checkout HEAD -- backend/tests/static_gates/{_TRAILER_NAME}"
)

#: The checks that gate has to keep defining, by name. They are the
#: module's own module-level cases as this tree holds them, walked with
#: ``ast`` — nine of them, which is fewer than the cases pytest collects
#: because two are parametrized, the same gap the floor above records.
#:
#: What each one carries, so a later deletion is legible as the loss it
#: is rather than as a name that stopped appearing: the two parametrized
#: cases are the catch and its narrowness, the next three are the three
#: entry points the single implementation is reached through, and the
#: last four are the history walk, the declared-ref subset, the push
#: rule, and the negative control that says the sweep can fail at all.
#:
#: A subset and not an equality, for the reason the evidence set above
#: gives: a check a later commit adds is a gain, and reporting it would
#: make the next legitimate addition a red test nobody can act on.
_REQUIRED_TRAILER_CHECKS: frozenset[str] = frozenset({
    "test_ci_wires_the_checker_over_the_pushed_range",
    "test_flags_an_ai_attribution",
    "test_leaves_legitimate_messages_alone",
    "test_no_commit_in_this_history_carries_an_ai_trailer",
    "test_no_remote_tracking_ref_retains_the_trailer",
    "test_only_the_declared_refs_retain_the_trailer",
    "test_the_native_installer_covers_the_commit_msg_hook",
    "test_the_pre_commit_config_wires_the_hook",
    "test_the_ref_sweep_reports_a_planted_trailer",
})

#: Names no required set above mentions, standing in for a check a later
#: commit adds. The counterweight to the subset decision, and the reason
#: the negative control below builds its sample from this union rather
#: than from the required set alone: a specimen holding only the required
#: names cannot tell a subset from an equality.
_LATER_TRAILER_CHECKS: tuple[str, ...] = (
    "test_a_check_added_after_this_shape_gate_was_written",
    "test_another_check_added_later_to_the_trailer_gate",
    "test_a_third_check_added_later",
    "test_a_fourth_check_added_later",
    "test_a_fifth_check_added_later",
)


def _trailer_gate_report(tree: ast.Module) -> str:
    """Every check *tree* no longer defines, as one report.

    A subset, because the question is whether the file still provides
    the checks it exists to provide — not whether it provides only
    those. Each entry names the check it is missing, so a reader knows
    which enforcement went without re-deriving it, and the report
    carries the command that restores the file, because a reader who
    has lost a check is better served by the repair than by the shape
    of the problem.

    The empty string is the healthy answer, and the only healthy answer
    this report has: a report that cannot be shown to fail is a report
    that says nothing ever.
    """
    missing = sorted(
        _REQUIRED_TRAILER_CHECKS - _module_test_function_names(tree)
    )
    if not missing:
        return ""
    return (
        f"{_TRAILER_NAME} no longer defines {len(missing)} of the "
        f"{len(_REQUIRED_TRAILER_CHECKS)} checks it has to keep defining: "
        + ", ".join(missing)
        + ". No floor above can see this — that gate is a parse-and-floor "
        "check, and a file that keeps its docstring, clears every floor "
        "and keeps its first case passes all of them with the enforcement "
        "gone. Restore the gate rather than editing the required set "
        "above, or the next deletion is declared correct:\n\n"
        f"    {_TRAILER_RECOVERY_COMMAND}\n"
    )


def test_the_trailer_gate_still_defines_every_check_it_exists_to_provide() -> None:
    """The same half of the shape, for the gate that polices commit messages.

    That gate is a parse-and-floor check like the ones above, and that is
    the whole problem: a truncation that keeps its docstring, clears every
    floor, and still defines one case passes all of them, with the
    enforcement gone. So its checks are pinned by name — a subset, because
    a check a later commit adds is a gain, and an equality would turn the
    next legitimate addition into a red test nobody can act on.
    """
    relative = _TRAILER_PATH.relative_to(_REPO_ROOT).as_posix()
    assert _TRAILER_RECOVERY_COMMAND == f"git checkout HEAD -- {relative}", (
        f"the recovery command names {_TRAILER_RECOVERY_COMMAND!r} rather "
        f"than the path this gate reads ({relative}). A remedy pointing at a "
        f"renamed file is worse than no remedy, because it is a command that "
        f"restores nothing and reads as a fix."
    )

    report = _trailer_gate_report(
        ast.parse(
            _TRAILER_PATH.read_text(encoding="utf-8"),
            filename=str(_TRAILER_PATH),
        )
    )
    assert report == "", report

    # The counterweight, and the proof the report can fail. A specimen that
    # clears every floor above and is missing exactly one required check,
    # plus names no required set mentions. Reporting the extras would make
    # the subset an equality; failing to report the deletion would make the
    # pin above decoration.
    dropped = "test_no_remote_tracking_ref_retains_the_trailer"
    sample = (
        _as_docstring(_FILLER)
        + "".join(
            f"\ndef {name}() -> None:\n    return None\n"
            for name in sorted(
                set(_REQUIRED_TRAILER_CHECKS - {dropped})
                | set(_LATER_TRAILER_CHECKS)
            )
        )
        + "x = 1\n" * _E2E_MIN_LINES
    )
    parsed = ast.parse(sample)
    first = parsed.body[0]

    assert (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ), (
        "the sample no longer opens with a docstring, so reporting it would "
        "prove nothing about the check it is missing"
    )
    assert len(ast.get_docstring(parsed) or "") >= _E2E_MIN_DOCSTRING_CHARS, (
        "the sample's docstring fell under the floor, so it would be "
        "reported for its opening and never for the check it is missing"
    )
    assert not _header_markers(sample), (
        f"the sample's opening lines now carry a marker "
        f"{_header_markers(sample)}, so it would be reported for its opening"
    )
    assert len(sample.splitlines()) >= _E2E_MIN_LINES, (
        "the sample is under the line floor, so it would be reported for its "
        "size and never for the check it is missing"
    )
    assert _test_function_count(parsed) >= _E2E_MIN_TEST_FUNCTIONS, (
        "the sample defines too few cases to clear the case-count floor, so "
        "it would be reported for its count and never for the check it is "
        "missing"
    )

    sample_report = _trailer_gate_report(parsed)
    assert dropped in sample_report, (
        f"a module that clears every floor and is missing {dropped!r} was "
        f"not reported by name, so the pin above cannot fail and is "
        f"decoration. Report was: {sample_report!r}"
    )
    assert _TRAILER_RECOVERY_COMMAND in sample_report, (
        f"the report names the missing check but carries no recovery "
        f"command, so a reader is told the symptom and not the repair. "
        f"Report was: {sample_report!r}"
    )
    for name in _REQUIRED_TRAILER_CHECKS - {dropped}:
        assert name not in sample_report, (
            f"{name!r} is present in the sample and is being reported "
            f"anyway, so the required set is being compared for equality. "
            f"Report was: {sample_report!r}"
        )
    for name in _LATER_TRAILER_CHECKS:
        assert name not in sample_report, (
            f"{name!r} is not in the required set and is being reported "
            f"anyway, so a check added by a later commit would go red here. "
            f"Report was: {sample_report!r}"
        )
