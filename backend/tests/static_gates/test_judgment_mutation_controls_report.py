"""The mutation negative-control report has to name every table, and
every row of it has to say what the probe saw.

Why this gate exists
--------------------
§12.7 of the judgment-surface handoff asks for one negative control per
decision table: flip a branch on purpose, watch the table go red, put
the module back byte for byte, and check the hash.  The probe
(``scripts/judgment_mutation_probe.py``) does the work; the report is
what a reader opens later to find out which tables were measured and
what they saw.

A report that quietly covers eight of the nine tables is the failure
mode this gate is written against.  It reads like a complete audit, and
the one table nobody measured is exactly the one nobody looks at — so
the gate takes the Tier A rows out of the manifest and requires the
report to name every one of them.

What it pins
------------
1. **Completeness.**  Every Tier A table in ``docs/judgment_points.json``
   has exactly one row in the report, and the report has no other rows.
2. **Identity.**  Each row names the table it measured, the function it
   mutated, the change it made, and the command it ran — and the
   command's ``--module`` and ``--test`` are that row's own module and
   table, so a row cannot point at somebody else's run.
3. **The verdict.**  Each row records that the table went **red** and
   that the module's digest was the same before and after the mutation.

Scope, stated honestly: this gate **reads** the manifest and the report.
It never runs the probe — a gate that mutated the modules it audits
would be changing the thing it measures.  It also does not re-hash the
modules against the recorded digests: ``backend/agent.py`` changes every
few weeks, and a report that went red on an unrelated edit would be a
report nobody keeps.  Re-running the probe is how a row is refreshed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: The repository root, three parents up from ``backend/tests/static_gates/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The report under gate.
_REPORT = _REPO_ROOT / "docs" / "development" / "judgment-mutation-controls.md"
#: The manifest that says which Tier A tables there are.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The tier that carries a decision table (§4.3 of the handoff).
_TIER = "A"
#: The probe every row was measured with.
_PROBE = "scripts/judgment_mutation_probe.py"
#: The header the report's table carries, in the order it carries it.
_HEADER = ("table", "function", "mutation", "command", "red", "sha256")
#: The verdict a row has to record: the mutation made the table fail.
_RED = "RED"
#: How a row records the digest check: the probe's own verdict, then the
#: two digests it compared.  Both are written out, because "the hash
#: matched" is a claim about two values and one value cannot show it.
_SHA256 = re.compile(
    r"^sha256 ok (?P<before>[0-9a-f]{64}) == (?P<after>[0-9a-f]{64})$"
)


# ---------------------------------------------------------------------------
# Reading the manifest
# ---------------------------------------------------------------------------


def _manifest() -> dict:
    """The manifest, parsed."""
    assert _MANIFEST.is_file(), (
        f"{_MANIFEST} is missing: there is no manifest to say which tables "
        "the report has to cover"
    )
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _tier_a_points() -> list[dict]:
    """The manifest's Tier A rows — the ones that carry a decision table."""
    points = _manifest().get("points")
    if not isinstance(points, list):
        return []
    return [
        point
        for point in points
        if isinstance(point, dict) and point.get("tier") == _TIER
    ]


def _table_path(point: dict) -> str:
    """The file the point's table lives in.

    The manifest writes it as ``<path>::<name>``; the file is the half
    before the separator.
    """
    ref = point.get("table")
    if not isinstance(ref, str):
        return ""
    return ref.partition("::")[0]


def _bare_function(point: dict) -> str:
    """The function's own name, without the class it hangs on.

    A method is written the way the code writes it — ``_gate_block_reason_for``
    — and the class it belongs to is the manifest's business, not the
    report's.
    """
    name = point.get("function")
    if not isinstance(name, str):
        return ""
    return name.rsplit(".", 1)[-1]


def _point_for_table(table: str) -> dict | None:
    """The Tier A point that names this table, if one does."""
    for point in _tier_a_points():
        if _table_path(point) == table:
            return point
    return None


# ---------------------------------------------------------------------------
# Reading the report
# ---------------------------------------------------------------------------


def _report_text() -> str:
    """The report's text."""
    assert _REPORT.is_file(), (
        f"{_REPORT} is missing: the mutation negative controls have to be "
        "written down where a reader can find them"
    )
    return _REPORT.read_text(encoding="utf-8")


def _unquote(cell: str) -> str:
    """The cell's text, without the backticks it may be written in."""
    text = cell.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1].strip()
    return text


def _cells(line: str) -> list[str]:
    """The cells of one markdown table row."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    """Is this the ``| --- | --- |`` row between the header and the body?"""
    return bool(cells) and all(cell and set(cell) <= set("-: ") for cell in cells)


def _rows(text: str) -> list[list[str]]:
    """The body rows of the report's mutation-control table.

    The table is found by its header, so a paragraph that happens to
    carry a vertical bar does not turn into a row.
    """
    rows: list[list[str]] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if inside:
                break
            continue
        cells = _cells(stripped)
        if not inside:
            if tuple(cell.lower() for cell in cells) == _HEADER:
                inside = True
            continue
        if _is_separator(cells):
            continue
        rows.append(cells)
    return rows


def _option(command: str, name: str) -> str:
    """The value the recorded command gives to ``--name``."""
    match = re.search(rf"--{name}[ =](\S+)", command)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# The two checks
# ---------------------------------------------------------------------------


def _row_identity_problems(
    number: int, row: list[str], point: dict
) -> list[str]:
    """What is wrong with a row's identity: its table, function and command."""
    problems: list[str] = []
    table = _unquote(row[0])
    function = _unquote(row[1])
    mutation = _unquote(row[2])
    command = _unquote(row[3])

    expected_function = _bare_function(point)
    if function != expected_function:
        problems.append(
            f"row {number} names the function {function!r}, but the table "
            f"{table!r} pins {expected_function!r}"
        )

    if not mutation:
        problems.append(
            f"row {number} does not say what was changed: a negative "
            "control without a mutation is nothing"
        )
    else:
        module = point.get("module")
        if isinstance(module, str) and module and module not in mutation:
            problems.append(
                f"row {number} says the change was made to {mutation!r}, "
                f"which does not name the module {module!r} the table pins"
            )

    if _PROBE not in command:
        problems.append(
            f"row {number} does not name the probe {_PROBE!r}, so the "
            "command it records is not a negative control"
        )

    measured = _option(command, "test")
    if measured != table:
        problems.append(
            f"row {number} records a command that ran {measured!r}, not the "
            f"table it names ({table!r})"
        )

    mutated = _option(command, "module")
    module = point.get("module")
    expected_module = f"backend/{module}" if isinstance(module, str) else ""
    if mutated != expected_module:
        problems.append(
            f"row {number} records a command that mutated {mutated!r}, not "
            f"the module the table pins ({expected_module!r})"
        )
    return problems


def _table_problems(rows: list[list[str]]) -> list[str]:
    """Everything wrong with the report's table, as a list of problems."""
    problems: list[str] = []
    points = _tier_a_points()
    if not points:
        problems.append(
            "the manifest names no Tier A judgment point at all: a gate "
            "over an empty room cannot fail"
        )
        return problems
    if not rows:
        problems.append(
            "the report has no mutation-control table: the header "
            f"{' | '.join(_HEADER)} was not found"
        )
        return problems

    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        number = index + 1
        if len(row) != len(_HEADER):
            problems.append(
                f"row {number} has {len(row)} cells, not the {len(_HEADER)} "
                "the header declares"
            )
            continue
        table = _unquote(row[0])
        seen[table] = seen.get(table, 0) + 1
        point = _point_for_table(table)
        if point is None:
            problems.append(
                f"row {number} names the table {table!r}, which is not a "
                "Tier A table of the manifest"
            )
            continue
        problems.extend(_row_identity_problems(number, row, point))

    for point in points:
        table = _table_path(point)
        count = seen.get(table, 0)
        if count == 0:
            problems.append(
                f"the report has no row for the table {table!r}: a report "
                "that quietly skips a table reads like a complete audit"
            )
        elif count > 1:
            problems.append(
                f"the report has {count} rows for the table {table!r}: one "
                "table, one measurement"
            )

    if len(rows) != len(points):
        problems.append(
            f"the report has {len(rows)} row(s) and the manifest has "
            f"{len(points)} Tier A table(s): one row per table"
        )
    return problems


def _verdict_problems(rows: list[list[str]]) -> list[str]:
    """What is wrong with the rows' verdicts: RED, and the digest check."""
    problems: list[str] = []
    for index, row in enumerate(rows):
        number = index + 1
        if len(row) != len(_HEADER):
            problems.append(
                f"row {number} has {len(row)} cells, not the {len(_HEADER)} "
                "the header declares, so it records no verdict"
            )
            continue

        red = _unquote(row[4])
        if red.upper() != _RED:
            problems.append(
                f"row {number} records {red!r}, not {_RED!r}: a negative "
                "control that did not bite is not a negative control"
            )

        verdict = _unquote(row[5])
        match = _SHA256.match(verdict)
        if match is None:
            problems.append(
                f"row {number} records {verdict!r}, which is not a digest "
                "check: the row has to carry the probe's verdict and the "
                "two digests it compared"
            )
            continue
        if match.group("before") != match.group("after"):
            problems.append(
                f"row {number} records two different digests: the module was "
                "not restored, so the run measured a mutant"
            )
    return problems


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_report_lists_all_nine_tables() -> None:
    """The report names every Tier A table, once each, with its own run.

    Nine is today's count; the number the gate demands is the number of
    Tier A tables the manifest has, so a tenth table has to be measured
    and written down before the report is complete again.
    """
    problems = _table_problems(_rows(_report_text()))

    assert not problems, "\n".join(problems)


def test_every_row_records_red_and_hash_match() -> None:
    """Every row records that the table went red and the hash matched.

    The digest check is recorded as the probe's own verdict **and** the
    two digests it compared: "the hash matched" is a claim about two
    values, and one value cannot show it.
    """
    problems = _verdict_problems(_rows(_report_text()))

    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The negative controls: each check has to be able to go red
# ---------------------------------------------------------------------------


def test_a_report_that_lost_a_table_is_named() -> None:
    """A report missing one of the tables is reported, not accepted."""
    rows = _rows(_report_text())

    problems = _table_problems(rows[:-1])

    assert problems, "a report missing one of the tables passed the check"
    assert "no row for the table" in problems[0]


def test_a_report_with_no_table_at_all_is_named() -> None:
    """A report with no table is reported, not accepted.

    A gate over an empty room cannot fail, and this one would go green
    over a report whose table had been deleted.
    """
    problems = _table_problems([])

    assert problems, "a report with no table passed the check"
    assert "no mutation-control table" in problems[0]


def test_a_row_that_did_not_go_red_is_named() -> None:
    """A row that records anything but RED is reported, not accepted."""
    rows = [list(row) for row in _rows(_report_text())]
    rows[0][4] = "GREEN"

    problems = _verdict_problems(rows)

    assert problems, "a row that did not go red passed the check"
    assert _RED in problems[0]


def test_a_row_whose_module_was_not_restored_is_named() -> None:
    """A row whose two digests differ is reported, not accepted.

    Two different digests mean the module was not put back, so the run
    measured a mutant and the table's red says nothing.
    """
    rows = [list(row) for row in _rows(_report_text())]
    rows[0][5] = f"sha256 ok {'0' * 64} == {'1' * 64}"

    problems = _verdict_problems(rows)

    assert problems, "a row whose two digests differ passed the check"
    assert "two different digests" in problems[0]
