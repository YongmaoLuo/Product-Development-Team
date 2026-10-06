"""A recorded pytest reading has to name the run that produced it.

Why this gate exists
--------------------
The whole-delivery E2E at
``tests/e2e/test_keychain_full_delivery_macos.py`` opens with a module
docstring several hundred lines long, and a large part of it is a
*record of runs*: four readings taken on one machine on one date, one
of them a red that was kept on purpose, plus a fifth demonstration
whose two summary lines come out of a child pytest rather than out of a
command a reader can type.

The defect this gate looks for is not a wrong number. It is a number
that has lost its conditions. A count written into a docstring reads as
a property of the code; the reading it came from is a property of one
machine, one switch position, one day. Nothing in the text separates
the two, so a count that was transcribed instead of taken sits in the
file looking exactly like a count that was taken, and a reader who
comes along later has no way to tell the two apart. That is the way an
observation nobody made turns into a piece of evidence.

Four shapes, and what each one is
---------------------------------
1. **A reading is attached.** Every count presented as something a run
   printed has to name the command that printed it, and the position
   that command ran in. A count sitting in a block with no command is a
   number with nowhere to have come from.
2. **A red that is gone is labelled.** A run that no longer reproduces
   may stay written down — deleting a real observation is a different
   failure, and a suite that forbids reds loses the ones worth having
   — but it may not be presented as the state of the tree now. So the
   gate requires the *label*, not the absence.
3. **A reading matches what was re-measured.** The counts this plan
   took are held here as a table, and a docstring count that disagrees
   with its table entry is reported with both numbers quoted. This is
   the check that catches an unobserved edit: nobody has to notice the
   number changed, because the number is compared rather than trusted.
4. **A claim of non-execution is paired with a captured refusal.** "the
   tool did not run" is only evidence if the error is quoted. An
   assertion of non-execution with no captured error is a claim about a
   machine that was never asked.

What this gate cannot see
-------------------------
It reads one docstring. It does not run anything, so it cannot tell a
reading that was *taken* from a reading that was *copied* — only
whether the copy is attached to a command, labelled, and in agreement
with the table below.

It also cannot keep the table fresh, and one entry cannot stay fresh by
itself. Reading 3 counts ``tests/static_gates/`` — the suite this file
is a case of. The figure in the table is what the re-measurement took;
it is not re-derived here, and this file's own cases are not in it, so
the command in that reading now collects more than the recorded
number. The gate does not compare the two, because a gate that counted
its own cases would be asserting a number about itself. Re-taking the
reading is what moves it, and the recorded figure says which reading
that is rather than standing in for it.

The summary line is not the evidence
-----------------------------------
The suite prints its own ``TEST_RESULT:`` block. ``pytest_terminal_summary``
in ``tests/conftest.py`` writes it at the end of every session, and it
keys on one thing — the session's exit status::

    if exitstatus == 0:
        total = terminalreporter._session.testscollected
        terminalreporter.write_line(f"TEST_RESULT: PASSED")
        terminalreporter.write_line(f"REASON: {total} tests collected, all passed. Exit code: 0")

``testscollected`` is a count of what was *found*, and a session that
collected twenty-five cases and ran none of them is still a session that
exited zero. So the block reads ``TEST_RESULT: PASSED`` over a run where
no assertion was ever evaluated — ``--collect-only`` produces it, and so
does any invocation that fails before the first test body: a collection
error, a bad ``-k``, an import that dies at module scope. The word
"passed" in that line is not a claim about the code, and the count
beside it is not a count of executions.

The ``collect_only`` entry in the table above is exactly this shape. It
is a legitimate reading and it is in the table for the right reason — it
is what a dry run of the lane's own file prints — but it carries no
execution evidence at all, and an acceptance that quoted its
``TEST_RESULT`` line would be quoting a run that executed zero cases.
This is worth stating here because the block is the single most
quotable line in the output and therefore the one most likely to be
copied into a report as the verdict.

So the summary line is not a verdict and the marker is not a verdict:
the evidence is the exit code together with a count that came from the
progress line, where each case contributes one character and the four
non-pass characters are distinguishable from the passing one. ``25
collected`` with twenty-five dots in the progress column is twenty-five
executions. ``25 collected`` with a ``TEST_RESULT: PASSED`` line and
nothing else is zero. The distinction is the whole finding, and it is
visible in the output without running anything — which is why it is
written down rather than left to the next reader of a pasted summary.

Why the counts are parsed and not grepped
-----------------------------------------
The distinction this gate needs — a count in a transcript against a
count being talked about — turns on inline-code delimiters, and a
transcript has to be located by its blank-line-bounded block. Both are
properties of the text as laid out, so the docstring is read through
``ast`` and the line numbers reported are the module's own.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: The module whose docstring is the subject, as a path from the
#: repository root, resolved through this file's own location.
_MODULE = (
    Path(__file__).resolve().parents[3]
    / "backend"
    / "tests"
    / "e2e"
    / "test_keychain_full_delivery_macos.py"
)

# ---------------------------------------------------------------------------
# The counts this plan re-measured, keyed by the command that printed them.
# ---------------------------------------------------------------------------

#: ``signature -> (summary line, exit status)``. The four readings the
#: docstring records as run on the machine that produced them, with the
#: numbers that run actually printed. A docstring count that disagrees
#: with the entry for its command is the violation this gate exists to
#: catch, and the report quotes both numbers.
_REMEASURED: dict[str, tuple[str, str]] = {
    # the default position: the same file, no switch
    "default": ("25 passed", "0"),
    # the strict position: the same file, switch set in the environment
    "strict": ("25 passed", "0"),
    # the workflow-gate suite, read out of its own text
    "static_gates": ("258 passed", "0"),
    # the lane's own run line with a dry run appended
    "collect_only": ("25 tests collected", "0"),
}

#: The reds the same runs printed before their defects were repaired,
#: kept deliberately. Held here so the gate can insist they are *still
#: written down* — a red that is quietly deleted is the other half of
#: this gate's failure, and it is invisible to a gate that only checks
#: labels.
_RED_RECORDED: dict[str, tuple[str, str]] = {
    "static_gates": ("1 failed, 257 passed", "1"),
}

# ---------------------------------------------------------------------------
# The analyzer — the half under test
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocLine:
    """One line of the module's own docstring, with its own line number."""

    lineno: int
    text: str


# ---------------------------------------------------------------------------
# Reading the docstring
# ---------------------------------------------------------------------------

#: A pytest summary count, in either of the two spellings the module
#: quotes: the outcome nouns, and the collection form pytest prints
#: under ``--collect-only``.
_COUNTS = re.compile(
    r"\b\d+\s+(?:passed|failed|skipped|deselected|xfailed|xpassed|errors?)\b"
    r"|\b\d+\s+tests?\s+collected\b"
)

#: Inline-code delimiters. A count inside one of these is prose about
#: the *shape* of a summary line, not a transcript of one.
_INLINE_CODE = re.compile(r"``.*?``")

#: The exit status a run printed. It is what lifts a recorded red out
#: of prose: that line quotes both its counts and its status inside
#: code delimiters, and the status is the part that means the numbers
#: came off a run rather than off a description of one.
_EXIT_STATUS = re.compile(r"\bexit\s+([0-9]+)\b")

#: Something the module's own probe can execute: a shell prompt, or an
#: invocation of pytest.
_COMMAND = re.compile(r"(?m)^\s*\$\s+\S|-m pytest")

#: A ``test_``-shaped name. On its own this is not a command — the
#: docstring names the gate files that went red as well as the cases
#: that ran — so it counts as one only where the text also says the
#: counts came out of that case.
_CASE_NAME = re.compile(r"\btest_[a-z0-9_]+\b")
_PRODUCED_BY_A_CASE = re.compile(r"pytest|\bchild\b|\bcase\b")

#: The position a command ran in. The four readings split into two that
#: belong to a position and two that are deliberately outside one, so
#: the outside ones are named rather than left to the position list.
_POSITION = re.compile(
    r"\bdefault\b|\bstrict\b|PDT_REQUIRE_KEYCHAIN_E2E|e2e-keychain|macos-latest"
)
_NON_POSITION = re.compile(
    r"workflow's own gates|lane's own run line|collection under"
)

#: A numbered reading: ``**1. The default position ...``. The span from
#: one marker to the next is the evidence for one recorded reading.
_ITEM = re.compile(r"^\s*\*\*\d+\.")

#: Why a run is not the state of the tree now. Each group is a reason a
#: red is allowed to stay without being presented as current; a red in
#: none of them is asserted as what the tree does today.
_GONE = re.compile(
    r"On the day|that day|no longer|gone as of|recorded as a red|used to|"
    r"historically|formerly|now prints"
)
_DRIVEN = re.compile(
    r"\bdrives\b|\bdriven\b|child.{0,12}pytest|sub-?pytest|"
    r"supplies the refusal itself"
)
_OTHER_BRANCH = re.compile(
    r"on a machine that|on that machine|a machine whose policy|"
    r"on the other branch|would report"
)

#: An assertion that a tool did not run.
_NON_EXECUTION = re.compile(
    r"cannot be started here|cannot start|\brefuse[sd]?\b|\bexecve\b|"
    r"\bEPERM\b|Permission denied|does not execute|do not execute|"
    r"did not execute|not executed|will not start"
)

#: A refusal with the error captured, in the form the probe emits it.
_CAPTURED_REFUSAL = re.compile(r"cannot be started here:\s*[A-Z]")


def doc_lines(path: Path) -> list[DocLine]:
    """The module docstring, one entry per line, with the module's own
    line numbers.

    Sliced out of the raw source rather than taken from
    ``ast.get_docstring``, because the reported line number has to be
    the line a reader finds the text on. ``clean=True`` would also run
    ``cleandoc`` and re-indent every line, which loses the very layout
    the block and item lookups depend on.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    first = tree.body[0]
    assert isinstance(first, ast.Expr), "the module has no docstring to read"
    assert isinstance(first.value, ast.Constant), "the module docstring is not a constant"
    raw = source.splitlines()
    body = raw[first.lineno - 1 : first.end_lineno]
    head = body[0]
    body[0] = head[head.index('"""') + 3 :]
    tail = body[-1]
    body[-1] = tail[: tail.rindex('"""')]
    return [DocLine(first.lineno + i, line) for i, line in enumerate(body)]


def module_string_literals(path: Path) -> frozenset[str]:
    """Every string constant in the module.

    Read through the tree rather than with a search, so a refusal the
    docstring quotes can be checked against a message the code really
    holds rather than against a shape written down here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def is_reading(text: str) -> bool:
    """Whether a line records a count that a run printed.

    A count written bare is a transcript. A count inside inline-code
    delimiters is prose about what a summary line looks like, and the
    docstring is full of sentences of that kind — with one exception:
    the recorded red quotes both its counts and its exit status, and
    the status is what lifts it out of prose and back into a
    transcript.
    """
    if _EXIT_STATUS.search(text) and _COUNTS.search(text):
        return True
    return bool(_COUNTS.search(_INLINE_CODE.sub(" ", text)))


def _names_a_command(text: str) -> bool:
    """Whether a piece of text says what printed the numbers."""
    if _COMMAND.search(text):
        return True
    return bool(_CASE_NAME.search(text)) and bool(_PRODUCED_BY_A_CASE.search(text))


def _runs(lines: Sequence[DocLine]) -> list[tuple[int, int]]:
    """The blank-line-bounded runs, as ``[start, end)`` index pairs."""
    runs: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(lines) + 1):
        if i == len(lines) or not lines[i].text.strip():
            if start < i:
                runs.append((start, i))
            start = i + 1
    return runs


def _run_of(runs: Sequence[tuple[int, int]], idx: int) -> int:
    for k, (start, end) in enumerate(runs):
        if start <= idx < end:
            return k
    raise LookupError(f"line {idx} is in no run")


def _items(lines: Sequence[DocLine]) -> list[int]:
    """Indices of the numbered readings, in order."""
    return [i for i, line in enumerate(lines) if _ITEM.match(line.text)]


def _text_of(lines: Sequence[DocLine], span: Sequence[int]) -> str:
    return "\n".join(lines[i].text for i in span)


def _is_literal_run(lines: Sequence[DocLine], start: int) -> bool:
    """Whether a run is an indented transcript rather than prose.

    The docstring's prose is flush with the margin and its transcripts
    are indented, so the indentation is what tells a printed block from
    a paragraph about one.
    """
    return lines[start].text.startswith(" ")


#: How far past a transcript its explanation is read. A transcript is
#: often followed by the paragraph that says what produced it, and that
#: paragraph is not always the next one — but the window stops at the
#: next transcript, so a count cannot borrow another reading's command.
_FOLLOW_RUNS = 3
_FOLLOW_LINES = 40


def _window(
    lines: Sequence[DocLine], runs: Sequence[tuple[int, int]], k: int
) -> list[int]:
    """The evidence around a transcript: its block, its lead-in, and
    the prose that follows it.

    The lead-in names the position the command ran in. The prose that
    follows names what produced the numbers, and it is not always the
    immediately next paragraph — the two counts driven out of a child
    pytest are attributed two paragraphs after the block that prints
    them. Both are needed, and both are bounded: the walk stops at the
    next transcript, so a count is never credited with a command that
    printed a different number.
    """
    start, end = runs[k]
    indices = list(range(start, end))
    if not _is_literal_run(lines, start):
        return indices
    if k > 0:
        indices[:0] = range(runs[k - 1][0], runs[k - 1][1])
    absorbed = 0
    for j in range(k + 1, len(runs)):
        if absorbed >= _FOLLOW_RUNS or _is_literal_run(lines, runs[j][0]):
            break
        if absorbed + runs[j][1] - runs[j][0] > _FOLLOW_LINES:
            break
        indices += list(range(runs[j][0], runs[j][1]))
        absorbed += 1
    return indices


def _signature(text: str) -> str | None:
    """Which recorded reading a piece of evidence is about.

    Ordered so the most specific marker wins: a dry run of the E2E
    file carries that file's path as well, and the strict run carries
    the switch, so the narrower keys are tested first.
    """
    if "--collect-only" in text:
        return "collect_only"
    if "PDT_REQUIRE_KEYCHAIN_E2E" in text:
        return "strict"
    if "tests/static_gates" in text:
        return "static_gates"
    if "test_keychain_full_delivery_macos.py" in text:
        return "default"
    return None


def _stated_summary(text: str, *, strip_inline: bool) -> str:
    source = _INLINE_CODE.sub(" ", text) if strip_inline else text
    return ", ".join(match.group(0) for match in _COUNTS.finditer(source))


def _provenance_of(lines: Sequence[DocLine], runs, idx: int) -> bool:
    """Whether the reading at ``idx`` is labelled as a run that is over.

    Split out because the same question is asked twice — once about the
    reading under examination, and once about every other reading in
    the item it belongs to — and the two answers have to agree about
    what counts as provenance.
    """
    k = _run_of(runs, idx)
    block_text = _text_of(lines, list(range(runs[k][0], runs[k][1])))
    if _is_literal_run(lines, runs[k][0]) and k > 0:
        block_text += _text_of(lines, list(range(runs[k - 1][0], runs[k - 1][1])))
    return bool(_GONE.search(block_text))


def find_violations(
    lines: Sequence[DocLine],
    table: dict[str, tuple[str, str]],
) -> list[str]:
    """Every recorded reading that is unattached, unlabelled or off the record."""
    runs = _runs(lines)
    markers = _items(lines)
    out: list[str] = []

    for idx, line in enumerate(lines):
        if not is_reading(line.text):
            continue
        k = _run_of(runs, idx)
        block = list(range(runs[k][0], runs[k][1]))
        window = _window(lines, runs, k)
        window_text = _text_of(lines, window)
        block_text = _text_of(lines, block)

        item: list[int] = []
        for m, marker in enumerate(markers):
            stop = markers[m + 1] if m + 1 < len(markers) else len(lines)
            if marker <= idx < stop:
                item = list(range(marker, stop))
                break
        item_text = _text_of(lines, item) if item else ""

        # Whether this run is over is read from the transcript and the
        # line introducing it, never from the paragraph after it: the
        # paragraph that explains the *recorded red* also sits next to
        # the green above it, and taking the fall-out as provenance
        # would turn the green into the red.
        provenance = block_text + (
            _text_of(lines, list(range(runs[k - 1][0], runs[k - 1][1])))
            if _is_literal_run(lines, runs[k][0]) and k > 0
            else ""
        )
        historical = bool(_GONE.search(provenance))

        # A run that is over is about an earlier run of a command
        # recorded elsewhere, so it may take its command and its
        # position from the item it belongs to rather than from a
        # transcript of its own.
        evidence = item_text if (historical and item) else window_text

        if not _names_a_command(evidence):
            out.append(
                f"line {line.lineno}: a recorded reading with no command "
                f"anywhere near it — {line.text.strip()!r}. A count has to "
                f"name the run that printed it."
            )
        elif historical and not item:
            out.append(
                f"line {line.lineno}: a reading presented as historical with "
                f"no numbered reading it could be the earlier run of."
            )

        # The position is read from the transcript and the line that
        # introduces it, not from the prose that follows. The prose
        # explains the *next* reading as often as this one, and a
        # position borrowed from there is a number attributed to the
        # wrong command. A historical red borrows from its item
        # instead, because it is an earlier run of that item's command.
        position_evidence = provenance + (item_text if historical else "")
        if not (
            _POSITION.search(position_evidence)
            or _NON_POSITION.search(position_evidence)
        ):
            out.append(
                f"line {line.lineno}: a recorded reading that names no "
                f"position — {line.text.strip()!r}. Which switch it ran "
                f"under is part of the number."
            )

        if "failed" in line.text and not (
            _GONE.search(window_text)
            or _DRIVEN.search(window_text)
            or _OTHER_BRANCH.search(window_text)
        ):
            out.append(
                f"line {line.lineno}: a red presented as the state of the "
                f"tree with nothing saying it is over, driven, or another "
                f"machine's — {line.text.strip()!r}."
            )
        if historical and not any(
            is_reading(lines[j].text) and not _provenance_of(lines, runs, j)
            for j in item
        ):
            out.append(
                f"line {line.lineno}: a reading labelled historical with no "
                f"current reading beside it in the same item, so the item "
                f"states no current state at all."
            )

        # The signature is read from the transcript itself, so a count
        # is only compared against the table when its own block says
        # which run it came from.
        signature = _signature(block_text)
        expected = _RED_RECORDED.get(signature) if historical else table.get(
            signature or ""
        )
        if expected is not None:
            expected_summary, expected_status = expected
            stated = _stated_summary(line.text, strip_inline=not historical)
            if stated != expected_summary:
                out.append(
                    f"line {line.lineno}: the docstring states {stated!r} "
                    f"for the {signature} run; the re-measurement recorded "
                    f"{expected_summary!r}. One of the two is not a reading."
                )
            status = _EXIT_STATUS.search(block_text)
            if status and status.group(1) != expected_status:
                out.append(
                    f"line {line.lineno}: the docstring states exit "
                    f"{status.group(1)} for the {signature} run; the "
                    f"re-measurement recorded exit {expected_status}."
                )
    return out


def non_execution_findings(
    lines: Sequence[DocLine],
    literals: frozenset[str],
) -> list[str]:
    """Assertions that a tool did not run, with no error captured.

    A claim that a binary was refused is evidence about a machine, and
    the only thing that makes it evidence is the refusal itself — the
    path and the errno the kernel returned. A docstring that states one
    without the other is asserting a fact nobody checked, which is the
    claim this subtree was burned by.
    """
    claims = [line for line in lines if _NON_EXECUTION.search(line.text)]
    if not claims:
        return []
    # Joined, because a quoted message wraps: the refusal is quoted
    # across two lines in the docstring and reading it line by line
    # would find the halves and neither.
    joined = " ".join(line.text for line in lines)
    if not _CAPTURED_REFUSAL.search(joined):
        first = claims[0]
        return [
            f"line {first.lineno}: the docstring says a tool did not execute "
            f"and quotes no refusal — {first.text.strip()!r}. A refusal the "
            f"probe actually produced is what makes that a reading."
        ]
    if not any("cannot be started here" in literal for literal in literals):
        return [
            f"line {claims[0].lineno}: the docstring quotes a 'cannot be "
            f"started here' refusal that the module's own source does not "
            f"hold, so the quoted error is not one this probe can emit."
        ]
    return []


def _lines_from_text(text: str, first: int = 100) -> list[DocLine]:
    """A synthetic docstring, numbered from ``first``.

    Line numbers are the module's own in the real file; the sensitivity
    samples below are assembled here so a violation can be reported
    against a line number a reader can go and look at.
    """
    return [DocLine(first + i, line) for i, line in enumerate(text.split("\n"))]


# ---------------------------------------------------------------------------
# 1. Sensitivity — the analyzer has to catch the shape it claims to catch
# ---------------------------------------------------------------------------


def test_a_bare_count_with_no_command_is_reported_with_its_line_number() -> None:
    """A summary line with no command above it is a violation.

    This is the shape the gate is named for, so it is checked against a
    synthesized docstring rather than trusted to appear in the real one.
    """
    sample = _lines_from_text(
        "\n".join(
            [
                "**1. The default position.**::",
                "",
                "    12 passed",
                "    exit 0",
            ]
        )
    )
    violations = find_violations(sample, _REMEASURED)
    assert violations, "a transcript with no command was not reported"
    assert any("102" in v for v in violations), (
        f"the violation does not carry the line number: {violations}"
    )


def test_a_reading_attached_to_a_command_is_not_reported() -> None:
    """The counterweight: the shape the real docstring uses is legal."""
    sample = _lines_from_text(
        "\n".join(
            [
                "**1. The default position — an execution.**::",
                "",
                "    $ ./.venv/bin/python3 -m pytest tests/e2e/x.py",
                "    12 passed",
                "    exit 0",
            ]
        )
    )
    assert not find_violations(sample, _REMEASURED), (
        f"a correctly attributed transcript was reported: "
        f"{find_violations(sample, _REMEASURED)}"
    )


def test_a_counts_line_in_inline_code_is_a_mention_not_a_reading() -> None:
    """A count in double backticks is a shape being discussed.

    The docstring says things like "a summary line reading ``6 passed``
    is an execution", which is a statement about what a reader's future
    run will look like. Reading those as transcripts would flag the
    prose that most carefully avoids claiming a run.
    """
    assert not is_reading("because ``6 passed`` and ``6 skipped`` differ"), (
        "an inline-code count was classified as a recorded reading"
    )
    assert is_reading("25 passed"), "a bare count was not classified as a reading"
    assert is_reading("``1 failed, 257 passed`` / ``exit 1``"), (
        "a recorded red carrying an exit status was classified as a mention"
    )


def test_a_historical_red_with_no_label_is_a_violation() -> None:
    """A red presented as the state of the tree now.

    The label is what this asks for, not the absence of the red: the
    same numbers with no word saying the run is over read as a claim
    about the present.
    """
    sample = _lines_from_text(
        "\n".join(
            [
                "**1. The default position.**::",
                "",
                "    $ ./.venv/bin/python3 -m pytest tests/e2e/x.py",
                "    3 failed, 9 passed",
                "    exit 1",
                "",
                "That is what the command prints.",
            ]
        )
    )
    violations = find_violations(sample, _REMEASURED)
    assert any("failed" in v for v in violations), (
        f"an unlabelled red was not reported: {violations}"
    )


def test_a_count_that_disagrees_with_the_re_measurement_quotes_both_numbers() -> None:
    """A drift between the docstring and the table names each number.

    Without both numbers the report cannot be acted on: a reader needs
    to know which figure is the tree's and which is the record's.
    """
    sample = _lines_from_text(
        "\n".join(
            [
                "**1. The default position — an execution.**::",
                "",
                "    $ ./.venv/bin/python3 -m pytest "
                "tests/e2e/test_keychain_full_delivery_macos.py",
                "    26 passed",
                "    exit 0",
            ]
        )
    )
    violations = find_violations(sample, _REMEASURED)
    assert violations, "a count that disagrees with the table was not reported"
    assert any("26 passed" in v and "25 passed" in v for v in violations), (
        f"the report does not quote both numbers: {violations}"
    )


def test_a_non_execution_claim_without_a_captured_refusal_is_a_violation() -> None:
    """"The tool did not run" is evidence only with the error quoted.

    The probe in the module produces a message naming the path and the
    errno. A docstring that asserts non-execution without one of those
    messages is asserting a fact about a machine nobody asked.
    """
    sample = _lines_from_text(
        "\n".join(
            [
                "A policy refuses execve of /usr/bin/security and /bin/ps,",
                "so the six do not execute on a machine like that.",
            ]
        )
    )
    findings = non_execution_findings(
        sample, frozenset({"{} cannot be started here: {}"})
    )
    assert findings, "a non-execution claim with no quoted refusal was accepted"

    paired = _lines_from_text(
        "\n".join(
            [
                "A policy refuses execve of /usr/bin/security, and the probe",
                "answers: ``.../refused cannot be started here: Permission",
                "denied``. So the six do not execute on a machine like that.",
            ]
        )
    )
    assert not non_execution_findings(
        paired, frozenset({"{} cannot be started here: {}"})
    ), "a claim paired with a captured refusal was reported"


# ---------------------------------------------------------------------------
# 2. The module itself
# ---------------------------------------------------------------------------


def test_the_scan_reaches_the_module_and_finds_its_readings() -> None:
    """Anti-vacuity: the docstring parsed, and the readings were found.

    A gate that stops matching because the wording changed would report
    a clean file and mean nothing, so the count of readings is asserted
    against a floor rather than left implicit.
    """
    lines = doc_lines(_MODULE)
    assert len(lines) > 100, (
        f"the module's docstring parsed to {len(lines)} lines — the "
        f"readings are not where this gate looks for them"
    )
    readings = [line for line in lines if is_reading(line.text)]
    assert len(readings) >= 7, (
        f"only {len(readings)} recorded readings found; this gate reads a "
        f"docstring that records at least seven"
    )


def test_every_reading_in_the_module_names_the_command_that_produced_it() -> None:
    """Check 1 over the real docstring.

    A red that is no longer reproducible may take its command from the
    item it belongs to rather than from its own block, because it is by
    definition about an earlier run of a command recorded elsewhere.
    """
    assert find_violations(doc_lines(_MODULE), _REMEASURED) == []


def test_the_recorded_red_is_still_written_down() -> None:
    """Deleting the red is the other failure, and it is not silent.

    The gate checks labels, so a red that was removed passes every other
    test in this file. This one fails instead, which is what keeps the
    record honest in the direction that costs a reading rather than one
    that costs a re-measurement.
    """
    text = "\n".join(line.text for line in doc_lines(_MODULE))
    deleted = [
        summary
        for summary, _status in _RED_RECORDED.values()
        if summary not in text
    ]
    assert not deleted, (
        f"{deleted} is no longer in the docstring. A red that is no longer "
        f"reproducible may stay written down; removing it takes the evidence "
        f"with it, and this gate will not treat that as tidying."
    )


def test_the_re_measured_counts_are_all_still_stated() -> None:
    """Every reading in the table has a current claim in the docstring.

    The table is the record; a reading dropped from the docstring is a
    measurement that no longer has a place it is reported from, and the
    comparison in the other test would pass vacuously without it.
    """
    text = "\n".join(line.text for line in doc_lines(_MODULE))
    missing = [
        signature
        for signature, (summary, _status) in _REMEASURED.items()
        if summary not in text
    ]
    assert not missing, (
        "the docstring no longer states a count for: " + ", ".join(missing)
    )


def test_the_module_quotes_a_refusal_its_own_probe_can_produce() -> None:
    """Check 4 over the real docstring, against the module's own literals.

    The quoted refusal is matched against the string constants in the
    module, not against a shape remembered here: a docstring that
    quoted a plausible-sounding error the probe never emits would be
    exactly the fabricated evidence this check is for.
    """
    literals = module_string_literals(_MODULE)
    refusal_templates = {
        literal
        for literal in literals
        if "cannot be started here" in literal
    }
    assert refusal_templates, (
        "the module's own source has no 'cannot be started here' message, "
        "so a docstring quoting one could not be quoting this probe"
    )
    assert not non_execution_findings(doc_lines(_MODULE), frozenset(literals)), (
        "the docstring asserts a tool did not execute without quoting the "
        "refusal the probe produces"
    )
