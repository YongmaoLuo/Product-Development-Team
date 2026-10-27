"""The token-convergence acceptance record must exist, and it must stay true
to the tree it describes.

Why this gate exists
--------------------
The convergence work is judged by six hard acceptance conditions, and the
judgement is delivered as a document: one row per condition, the test paths
behind it, the command that reproduces it, and the verdict that command
produced. A document is the right shape for that judgement — the reader
needs the whole argument on one page, not a pile of log files.

But a document has no way of failing on its own. It cannot be wrong
loudly; it just goes stale, and a stale acceptance record is *worse* than
no record at all, because it is read as evidence. Four things go stale
first, and all four are checked here:

1. **The conditions.** A row that loses its evidence column, or a verdict
   that no longer matches what the condition's own command produced, is a
   condition that has quietly stopped being met while the table still says
   it is met.
2. **The commands.** A command without the directory it runs in and the
   worker count it runs with is not reproducible. ``pytest tests/unit``
   says nothing about *where*; ``-n 8`` is the number CI actually uses,
   and a reader who runs the command at the wrong width gets a different
   wall clock and a different shard split than the one the verdict came
   from.
3. **The paths.** A test path that was renamed or deleted leaves the
   record pointing at a file that is no longer there, and a reader cannot
   tell that apart from a condition that never had a test at all.
4. **The verdicts that decide the merge.** The record ends with the
   criteria that decide whether the work may land, and with the table of
   container-gate job classes behind the second one.

The fifth thing this gate exists for is the one that matters most, and it
is the reason the record carries a run status at all: **a record may not
claim a verdict nobody produced.** An acceptance table whose cells read
"0 failed" is indistinguishable from one whose cells were filled in
because somebody wanted the page to look finished. So the page states
plainly whether the gate has been run, and while it says it has not, no
cell in it may claim a result. The switch is one line, the cells are
many, and the gate fails the moment the two disagree — which is exactly
the moment a fabricated record would otherwise slip through.

Both locales are checked. The record exists in English and in Chinese,
and the Chinese one is the one a Chinese reader is given — a gate that
only polices the English copy lets the copy the reader actually has rot
in peace.

What it pins
------------
1. :func:`test_doc_lists_six_acceptance_items` — exactly six numbered
   conditions (``AC-1`` … ``AC-6``), each with a non-empty condition
   statement, at least one named test path, and a verdict drawn from the
   vocabulary a record may use.
2. :func:`test_doc_commands_are_runnable` — every command in the document
   carries a directory that exists in this tree and a worker count, and
   every one of the six conditions has a command of its own.
3. :func:`test_doc_names_target_paths` — every repository path the
   document names exists, and every condition names at least one test
   path under ``backend/tests/``.
4. :func:`test_doc_records_the_merge_criteria_verdicts` — the run status,
   the merge criteria, and the container-gate record agree with each other,
   and no cell claims a result the page says nobody has produced.

Scope, stated honestly
----------------------
This gate reads a Markdown file. It does not run the suite, it does not
re-measure anything, and it does not check that the verdicts in the table
are the verdicts the commands would produce today. What a green run here
means is "the acceptance record is well-formed, it still points at files
that exist, and it does not claim a result it has not earned" — not "the
acceptance conditions hold". The holding is established by running the
commands the document names, which is what the document is for.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOC_DIR = _REPO_ROOT / "docs" / "development"

#: The record exists in two locales. Both are checked: the pairing gate
#: (``test_docs_are_bilingual``) proves a Chinese sibling *exists*, not
#: that it says the same thing.
_DOC_NAMES = ("token-convergence-acceptance.md", "token-convergence-acceptance.zh.md")

#: The acceptance table's header row, pinned verbatim. The table is the
#: record; a header that changes shape means a reader (or another tool)
#: is looking at a table nobody defined.
_TABLE_HEADER = "| 编号 | 条件 | 证据（测试路径 / 命令） | 结果 |"

#: How many numbered conditions the acceptance record carries, and which
#: identifiers they use.
_EXPECTED_CONDITIONS = 6
_EXPECTED_IDS = tuple(f"AC-{i}" for i in range(1, _EXPECTED_CONDITIONS + 1))

#: The verdict of a condition whose command has not been run. The record
#: is written before the gate runs, so this is what its cells say first,
#: and it stays the right answer until somebody runs the command.
_PENDING = "PENDING(operator)"

#: The verdict of a condition whose command was run and found nothing
#: failed.
_VERDICT = "0 failed"

#: The verdict of a condition whose command was run and found something
#: failed. It is in the vocabulary because a record that cannot say "this
#: one did not hold" can only be right by accident.
_FAILED = "failed"

#: Every value a verdict cell may carry. A cell outside this set is prose
#: pretending to be a verdict.
_ALLOWED_VERDICTS = (_PENDING, _VERDICT, _FAILED)

#: The two states of the page's run status. ``PENDING(operator)`` — the
#: gate has not been run — forbids every claim of a result. ``RUN`` — the
#: gate has been run and the cells below have been filled in from it —
#: allows them.
_RUN_PENDING = _PENDING
_RUN_DONE = "RUN"

#: The run-status line. Both locales spell it their own way and the
#: value is the same in both, so one pattern reads both.
_RUN_STATUS_RE = re.compile(
    r"^(?:Run status|运行状态)\s*[:：]\s*(?P<value>[A-Za-z()]+)\s*$",
    re.MULTILINE,
)

#: Fenced shell blocks — the commands the document tells a reader to run.
_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)

#: The opening of a fenced block, with its language.
_FENCE_OPEN_RE = re.compile(r"^```(?P<lang>[A-Za-z0-9_+.-]*)\s*$")

#: A Markdown heading, with its title.
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")

#: The identifier at the start of an ``AC-n`` heading.
_AC_HEADING_RE = re.compile(r"^(?P<id>AC-\d+)\b")

#: A command that names where it runs and how wide it runs.
_COMMAND_RE = re.compile(
    r"^cd\s+(?P<dir>[^\s&|]+)\s*&&"
    r".*?(?:-n\s+(?P<workers>\d+))"
    r"(?:\s|$)"
)

#: A repository-relative path written as inline code.
_PATH_RE = re.compile(r"`(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|json|yml|yaml|ini|md))`")

#: Path prefixes a named path must live under to be checked against the
#: tree. A path outside these is prose, not a claim about the repository.
_CHECKED_PREFIXES = ("backend/", "docs/", ".github/", "scripts/")

#: The merge-criteria table's header row.
_MERGE_TABLE_HEADER = "| 判据 | 证据 | 状态 |"

#: The local container gate's record table header. Both locales spell the
#: column names the same way, so one constant finds both.
_GATE_TABLE_HEADER = "| job 类 | 结果 |"

#: The one verdict that is not a test verdict: the macOS keychain lane
#: runs on a macOS runner against a real keychain and cannot be
#: reproduced inside the Linux container.
_NOT_REPRODUCIBLE = "不在容器内复刻"

#: The job class the container cannot reproduce.
_MACOS_JOB = "macOS"

#: The job classes the local container gate runs. The record must carry a
#: row for each of them.
_EXPECTED_GATE_JOBS = (
    "lint",
    "grep-guard",
    "static-gates",
    "commit-hygiene",
    "commit-range-privacy",
    "pr-metadata-privacy",
    "json-cleanup",
    "docs",
    "unit",
    "root",
    "misc",
    "coverage-gate",
    "integration",
    "e2e",
    "e2e-verification",
    "verification-e2e",
    "verification-perf",
    "time-sensitive",
    "negctl",
    "e2e-keychain",
)

#: The states the merge criterion may carry. The gate cannot check a pull
#: request — it is a person's call and happens outside the tree — so this
#: one is the operator's to write, and the honest value until then is the
#: pending marker.
_PR_STATUSES = (_PENDING, "未执行", "PR 已开，未合入", "已合入")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_paths() -> list[Path]:
    """Every locale of the acceptance record."""
    return [_DOC_DIR / name for name in _DOC_NAMES]


def _doc_text(path: Path) -> str:
    """The acceptance document, or a failure that says it is missing."""
    assert path.exists(), (
        f"{path.relative_to(_REPO_ROOT)} is missing — the six acceptance "
        "conditions have no written record, so a reader cannot tell "
        "whether they hold."
    )
    return path.read_text(encoding="utf-8")


def _rows_after_header(text: str, header: str) -> list[list[str]]:
    """The body rows of the table whose header row is exactly ``header``."""
    lines = text.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if line.strip() == header),
        None,
    )
    assert header_index is not None, (
        f"the document has no table headed\n  {header}\n"
        "so the rows it is supposed to carry are nowhere to be found"
    )

    rows: list[list[str]] = []
    for line in lines[header_index + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        # The separator row (``| --- | --- |``) is not a data row.
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows


def _acceptance_rows(text: str) -> list[list[str]]:
    """The body rows of the acceptance table, as lists of cells."""
    return _rows_after_header(text, _TABLE_HEADER)


def _bash_blocks(text: str) -> list[tuple[str | None, list[str]]]:
    """Every fenced bash block, with the condition heading it sits under.

    A heading that is not an ``AC-n`` heading ends the current condition,
    so a command that belongs to nobody is not silently credited to the
    last condition that happened to be read. A fence whose language is
    not ``bash`` carries prose or a transcript, not a command, and is
    skipped.
    """
    blocks: list[tuple[str | None, list[str]]] = []
    current: str | None = None
    fence_lang: str | None = None
    commands: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if fence_lang is not None:
            if stripped.startswith("```"):
                if fence_lang == "bash":
                    blocks.append((current, commands))
                commands = []
                fence_lang = None
            elif fence_lang == "bash" and stripped:
                commands.append(stripped)
            continue
        opening = _FENCE_OPEN_RE.match(stripped)
        if opening:
            fence_lang = opening.group("lang")
            commands = []
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            title = heading.group("title")
            ac = _AC_HEADING_RE.match(title)
            current = ac.group("id") if ac else None
    return blocks


def _command_sections(text: str) -> dict[str, list[str]]:
    """Map each ``### AC-n ...`` heading to the commands beneath it."""
    sections: dict[str, list[str]] = {}
    for section_id, commands in _bash_blocks(text):
        if section_id is None:
            continue
        sections.setdefault(section_id, []).extend(commands)
    return sections


def _all_commands(text: str) -> list[str]:
    """Every bash command in the document, whichever section it sits in."""
    return [command for _section, commands in _bash_blocks(text) for command in commands]


def _named_paths(text: str) -> list[str]:
    """Every repository path the document names, sorted and de-duplicated."""
    return sorted(
        {
            match.group("path")
            for match in _PATH_RE.finditer(text)
            if match.group("path").startswith(_CHECKED_PREFIXES)
        }
    )


def _run_status(text: str) -> str:
    """The page's run status: has the gate been run yet?"""
    match = _RUN_STATUS_RE.search(text)
    assert match is not None, (
        "the record does not say whether the gate has been run — without "
        f"that line a reader cannot tell a verdict from a claim. It must "
        f"carry a line reading `Run status: {_RUN_DONE}` (or "
        f"`{_RUN_PENDING}` before the gate runs)."
    )
    status = match.group("value")
    assert status in (_RUN_PENDING, _RUN_DONE), (
        f"the run status must be {_RUN_PENDING!r} or {_RUN_DONE!r}, found "
        f"{status!r}"
    )
    return status


def _gate_verdicts(text: str) -> dict[str, str]:
    """The container-gate record, as ``job class -> verdict``."""
    recorded: dict[str, str] = {}
    for row in _rows_after_header(text, _GATE_TABLE_HEADER):
        assert len(row) == 2, (
            "a container-gate row must have two columns (job class | "
            f"verdict), found {len(row)}: {row!r}"
        )
        job, verdict = row
        identifier = re.match(r"^[A-Za-z0-9_.-]+", job)
        assert identifier is not None, (
            f"a container-gate row does not name a job class: {job!r}"
        )
        recorded[identifier.group(0)] = verdict
    return recorded


def _merge_rows(text: str) -> list[list[str]]:
    """The merge-criteria table, as lists of cells."""
    rows = _rows_after_header(text, _MERGE_TABLE_HEADER)
    assert rows, "the merge-criteria table is missing"
    assert len(rows) == 3, (
        "the merge-criteria table must carry three criteria, found "
        f"{len(rows)}"
    )
    for row in rows:
        assert len(row) == 3, (
            "a merge-criteria row must have three columns (判据 | 证据 | "
            f"状态), found {len(row)}: {row!r}"
        )
    return rows


# ---------------------------------------------------------------------------
# 1. The six conditions
# ---------------------------------------------------------------------------


def test_doc_lists_six_acceptance_items() -> None:
    """The record carries six numbered conditions, each with a condition
    statement, evidence, and a verdict from the vocabulary a record may
    use."""
    for path in _doc_paths():
        text = _doc_text(path)
        rows = _acceptance_rows(text)

        assert len(rows) == _EXPECTED_CONDITIONS, (
            f"{path.name}: the acceptance table must carry exactly "
            f"{_EXPECTED_CONDITIONS} numbered conditions, found {len(rows)}"
        )

        numbers = [row[0] for row in rows]
        assert numbers == list(_EXPECTED_IDS), (
            f"{path.name}: the conditions must be numbered {_EXPECTED_IDS}, "
            f"found {numbers}"
        )

        for row in rows:
            assert len(row) == 4, (
                f"{path.name}: every acceptance row must have four columns "
                "(编号 | 条件 | 证据 | 结果), found "
                f"{len(row)}: {row!r}"
            )
            number, condition, evidence, verdict = row

            assert re.fullmatch(r"AC-\d+", number), (
                f"{path.name}: a condition number must look like AC-n, "
                f"found {number!r}"
            )
            assert condition, f"{path.name}: {number} has no condition statement"
            assert evidence, f"{path.name}: {number} names no evidence"
            assert verdict in _ALLOWED_VERDICTS, (
                f"{path.name}: {number} records {verdict!r}, which is not a "
                f"verdict a record may carry — use one of "
                f"{_ALLOWED_VERDICTS}"
            )
            assert _PATH_RE.search(evidence), (
                f"{path.name}: {number} names no test path in its evidence "
                f"column: {evidence!r}"
            )


# ---------------------------------------------------------------------------
# 2. The commands
# ---------------------------------------------------------------------------


def test_doc_commands_are_runnable() -> None:
    """Every command names the directory it runs in and the width it runs
    at, and every condition has one."""
    for path in _doc_paths():
        text = _doc_text(path)
        sections = _command_sections(text)

        condition_ids = {row[0] for row in _acceptance_rows(text)}
        assert condition_ids <= set(sections), (
            f"{path.name}: these conditions have no command of their own: "
            f"{sorted(condition_ids - set(sections))}"
        )

        for section_id, commands in sorted(sections.items()):
            assert commands, f"{path.name}: {section_id} has a heading but no command"

        for command in _all_commands(text):
            match = _COMMAND_RE.match(command)
            assert match is not None, (
                f"{path.name}: a command must say where it runs and how "
                "wide it runs — `cd <dir> && ... -n <workers>`; found: "
                f"{command!r}"
            )
            directory = match.group("dir")
            assert (_REPO_ROOT / directory).is_dir(), (
                f"{path.name}: the command runs in {directory!r}, which is "
                "not a directory in this tree"
            )
            assert int(match.group("workers")) >= 1, (
                f"{path.name}: the worker count must be at least 1, found "
                f"{match.group('workers')!r}"
            )


# ---------------------------------------------------------------------------
# 3. The paths
# ---------------------------------------------------------------------------


def test_doc_names_target_paths() -> None:
    """Every path the record names exists, and every condition names at
    least one test path."""
    for path in _doc_paths():
        text = _doc_text(path)

        named = _named_paths(text)
        assert named, f"{path.name}: the record names no repository path at all"

        missing = [p for p in named if not (_REPO_ROOT / p).exists()]
        assert not missing, (
            f"{path.name}: the record names paths that do not exist in this "
            "tree — a reader cannot tell a renamed test from a condition "
            "with no test:\n  " + "\n  ".join(missing)
        )

        for row in _acceptance_rows(text):
            number, _condition, evidence, _verdict = row
            test_paths = [
                match.group("path")
                for match in _PATH_RE.finditer(evidence)
                if match.group("path").startswith("backend/tests/")
            ]
            assert test_paths, (
                f"{path.name}: {number} names no test path under "
                f"backend/tests/ in its evidence column: {evidence!r}"
            )


# ---------------------------------------------------------------------------
# 4. The verdicts that decide the merge
# ---------------------------------------------------------------------------


def test_doc_records_the_merge_criteria_verdicts() -> None:
    """The run status, the merge criteria, and the container-gate record
    agree with each other, and nothing claims a result the page says
    nobody has produced.

    The six conditions above say what was built. These three say whether
    it may land, and they are the criteria a reader is asked to trust.
    A cell that claims a result while the page says the gate has not been
    run is a promise dressed as a result, and the gate fails on it.
    """
    for path in _doc_paths():
        text = _doc_text(path)

        status = _run_status(text)

        condition_verdicts = [row[3] for row in _acceptance_rows(text)]
        merge_rows = _merge_rows(text)
        gate_verdicts = _gate_verdicts(text)

        # Every cell that states a verdict speaks the vocabulary.
        #
        # The first two merge criteria are mechanical and speak it. The
        # third is a person's call about a pull request and speaks the
        # pull-request vocabulary instead, which is checked further down.
        # Checking it against both vocabularies would leave every
        # pull-request state but the pending marker unreachable.
        for number, row in zip(_EXPECTED_IDS, _acceptance_rows(text)):
            assert row[3] in _ALLOWED_VERDICTS, (
                f"{path.name}: {number} records {row[3]!r}, which is not a "
                f"verdict a record may carry"
            )
        for row in merge_rows[:2]:
            assert row[2] in _ALLOWED_VERDICTS, (
                f"{path.name}: the criterion {row[0]!r} records "
                f"{row[2]!r}, which is not a verdict a record may carry"
            )
        for job, verdict in gate_verdicts.items():
            if job == _MACOS_JOB:
                continue
            assert verdict in _ALLOWED_VERDICTS, (
                f"{path.name}: the job class {job!r} records {verdict!r}, "
                "which is not a verdict a record may carry"
            )

        # The macOS keychain lane cannot be reproduced in the container,
        # so its row must say so rather than claim a pass it never ran.
        assert _MACOS_JOB in gate_verdicts, (
            f"{path.name}: the container-gate record carries no row for the "
            "macOS keychain lane, so a reader cannot tell it apart from a "
            "job class that was run"
        )
        assert gate_verdicts[_MACOS_JOB].startswith(_NOT_REPRODUCIBLE), (
            f"{path.name}: the macOS keychain lane cannot be reproduced in "
            f"the container, so its row must say {_NOT_REPRODUCIBLE!r}, "
            f"found {gate_verdicts[_MACOS_JOB]!r}"
        )

        # The job classes the local gate runs are all on the page.
        missing = [job for job in _EXPECTED_GATE_JOBS if job not in gate_verdicts]
        assert not missing, (
            f"{path.name}: the container-gate record does not carry a row "
            "for every job class the local gate runs:\n  " + "\n  ".join(missing)
        )

        # Nothing may claim a result while the page says the gate has not
        # been run. This is the rule that keeps a record honest: a cell
        # that says the tests passed, written by somebody who did not run
        # them, is exactly the failure this page must not be able to make.
        if status == _RUN_PENDING:
            claimed = [
                f"{number}: {verdict}"
                for number, verdict in zip(_EXPECTED_IDS, condition_verdicts)
                if verdict != _PENDING
            ]
            claimed += [
                f"merge criterion {row[0]!r}: {row[2]}"
                for row in merge_rows[:2]
                if row[2] != _PENDING
            ]
            claimed += [
                f"job class {job!r}: {verdict}"
                for job, verdict in gate_verdicts.items()
                if job != _MACOS_JOB and verdict != _PENDING
            ]
            assert not claimed, (
                f"{path.name}: the record says the gate has not been run "
                f"({status!r}), yet these cells claim a result:\n  "
                + "\n  ".join(claimed)
                + "\nA verdict may only be written down after the command "
                "behind it has been run; until then the cell says "
                f"{_PENDING!r}."
            )

        # The two mechanical criteria say the same thing as the tables
        # behind them.
        target_status = merge_rows[0][2]
        if all(v == _VERDICT for v in condition_verdicts):
            expected = _VERDICT
        elif any(v == _FAILED for v in condition_verdicts):
            expected = _FAILED
        else:
            expected = _PENDING
        assert target_status == expected, (
            f"{path.name}: the 'target tests' criterion records "
            f"{target_status!r}, but the six conditions record "
            f"{condition_verdicts!r} — the criterion is met only when "
            "every condition is"
        )

        gate_status = merge_rows[1][2]
        gate_cells = [
            verdict
            for job, verdict in gate_verdicts.items()
            if job != _MACOS_JOB
        ]
        if gate_cells and all(v == _VERDICT for v in gate_cells):
            expected = _VERDICT
        elif any(v == _FAILED for v in gate_cells):
            expected = _FAILED
        else:
            expected = _PENDING
        assert gate_status == expected, (
            f"{path.name}: the 'local CI container' criterion records "
            f"{gate_status!r}, but the container-gate record says "
            f"{gate_cells!r} — the criterion is met only when every job "
            "class is"
        )

        # The pull request is a person's call and happens outside the
        # tree, so the gate cannot check it; the honest value until it
        # happens is the pending marker.
        assert merge_rows[2][2] in _PR_STATUSES, (
            f"{path.name}: the merge criterion records {merge_rows[2][2]!r}, "
            f"which is not one of {_PR_STATUSES}"
        )
