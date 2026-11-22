"""The Tier A rows of the manifest have to point at tables that are there.

Why this file exists
--------------------

``docs/judgment_points.json`` names the Tier A judgment points — the
functions whose return value decides PASS/FAIL — and, for each of them,
the decision table that pins it::

    {"function": "excess_declared_paths", "tier": "A",
     "table": "backend/tests/unit/test_decision_table_excess_declared_paths.py::CASES"}

That link is the whole of §4.3's second rule: *a table the manifest
names has to exist, parse, and carry at least one row.*  A manifest row
whose ``table`` names a file that was renamed, deleted or emptied is a
claim about a table that is not there — and the claim lives in a
**checked-in artifact**, so nothing else in the tree would notice.  The
gates that read the manifest would go green over a Tier A point with no
table at all, which is the exact shape §4.3 is written against.

This gate closes that hole.  It reads the manifest and, for every
Tier A row, asks four questions:

1. ``test_every_tier_a_point_has_a_table`` — does the row name a
   table at all, in the ``<path>::<name>`` form the census writes?
2. ``test_table_files_exist`` — is the named file on disk?
3. ``test_cases_are_non_empty_literal`` — does the file parse, and is
   the named module-level assignment a **list literal** with at least
   one row in it?
4. ``test_case_ids_are_unique_and_descriptive`` — do the rows carry
   readable ids, and do those ids say what the row pins?  An id that is
   only a counter (``case1`` / ``情况2``) says nothing, so it is
   refused — §4.4's second rule, the same rule
   ``backend/tests/judgment_tables.py`` applies when the table runs.

The tables are read as source, never imported
----------------------------------------------

Nothing here imports a table module.  A table module imports the code
it pins (``from agent import excess_declared_paths``) and builds its
rows at import time, so importing one would run the very code this
gate is inspecting, inside the gate's own process, with whatever side
effects it has.  Reading the file and parsing it with :mod:`ast` says
the same thing — the shape of the table — without executing a line of
it.  ``test_the_tables_are_read_without_being_imported`` pins that
promise, so a later edit that swaps the AST read for an import is
caught here rather than in a lane that runs the code twice.

The negative controls
---------------------

The four checks above are the ones the material names.  The controls
below them are the other half of the job: each one hands a check a
table (or a manifest) that has been broken on purpose and requires the
check to **name the offender**.  Without them, a check that reported
nothing would pass the controls as happily as it passes a correct
manifest — and a gate that cannot go red is not a gate.

Scope, stated honestly: this gate **reads** the manifest and the
tables.  It never writes to either.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

#: The repository root, three parents up from ``backend/tests/static_gates/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest under gate.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The tier that carries a decision table (§4.3).
_TIER = "A"
#: An id that is only a counter says nothing about why the row exists.
#: §4.4 requires every row to carry the *reason* it is there, so these
#: names are refused rather than merely discouraged — the same rule
#: ``backend/tests/judgment_tables.py`` applies when the table runs.
_PLACEHOLDER_ID = re.compile(r"^(case|情况)\d+$")
#: Where the demo tables of the negative controls live, relative to the
#: root they are written under.
_DEMO_TABLE = "backend/tests/unit/test_decision_table_demo.py"


# ---------------------------------------------------------------------------
# Reading the manifest
# ---------------------------------------------------------------------------


def _read_manifest(manifest_path: Path) -> dict:
    """The manifest at ``manifest_path``, parsed."""
    assert manifest_path.is_file(), (
        f"{manifest_path} is missing: there is no manifest to check"
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _points(manifest: dict) -> list[object]:
    """The manifest's rows, or an empty list when it has none."""
    points = manifest.get("points")
    return list(points) if isinstance(points, list) else []


def _tier_a_points(manifest: dict) -> list[dict]:
    """The manifest's Tier A rows — the ones that have to carry a table."""
    return [
        point
        for point in _points(manifest)
        if isinstance(point, dict) and point.get("tier") == _TIER
    ]


def _row_label(point: dict) -> str:
    """How a row is named in a message a reader can act on."""
    module = point.get("module")
    function = point.get("function")
    return f"{module}::{function}"


def _split_ref(ref: object) -> tuple[str, str] | None:
    """Split a ``<path>::<name>`` reference into its two halves.

    ``None`` when the reference is not in that form: a table the
    manifest does not say how to reach is a table it has not really
    named, and the shape of the reference is part of the contract.
    """
    if not isinstance(ref, str) or ref.count("::") != 1:
        return None
    path, _, name = ref.partition("::")
    if not path.strip() or not name.strip():
        return None
    return path, name


# ---------------------------------------------------------------------------
# The four checks
# ---------------------------------------------------------------------------


def _missing_tables(manifest: dict) -> list[str]:
    """The Tier A rows that do not name a table."""
    problems: list[str] = []
    tier_a = _tier_a_points(manifest)
    if not tier_a:
        problems.append(
            "the manifest names no Tier A judgment point at all: a "
            "manifest with no Tier A row has no table to check, and a "
            "gate over an empty room is a gate that cannot fail"
        )
    for point in tier_a:
        ref = point.get("table")
        if not isinstance(ref, str) or not ref.strip():
            problems.append(
                f"Tier A point {_row_label(point)} has no table: it names a "
                "function whose return value decides PASS/FAIL, so it has "
                "to name the table that pins it"
            )
            continue
        if _split_ref(ref) is None:
            problems.append(
                f"Tier A point {_row_label(point)} names the table {ref!r}, "
                "which is not in the '<path>::<name>' form the census writes"
            )
    return problems


def _missing_files(manifest: dict, root: Path) -> list[str]:
    """The named tables whose file is not on disk."""
    problems: list[str] = []
    for point in _tier_a_points(manifest):
        split = _split_ref(point.get("table"))
        if split is None:
            continue  # named by the table check
        path, _ = split
        if not (root / path).is_file():
            problems.append(
                f"Tier A point {_row_label(point)} names the table "
                f"{point.get('table')!r}, but {path} is not a file under "
                "the repository"
            )
    return problems


def _named_tables(manifest: dict, root: Path) -> list[tuple[dict, Path, str]]:
    """The ``(row, path, name)`` of every named table that is on disk.

    A table whose file is missing is left out: that is what the file
    check reports, and reporting it twice would only make the message
    harder to read.
    """
    named: list[tuple[dict, Path, str]] = []
    for point in _tier_a_points(manifest):
        split = _split_ref(point.get("table"))
        if split is None:
            continue
        path, name = split
        table_path = root / path
        if table_path.is_file():
            named.append((point, table_path, name))
    return named


def _parse_table(path: Path) -> ast.Module | None:
    """The table file's AST, or ``None`` when it cannot be read."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def _module_level_list(tree: ast.Module, name: str) -> tuple[bool, ast.List | None]:
    """Find the module-level assignment called ``name``.

    Returns ``(found, rows)``: ``found`` says whether the name is
    assigned at module level at all, and ``rows`` is the assignment's
    value when that value is a **list literal** — ``None`` when it is
    something else (a call, a comprehension, a name), because a table
    whose rows are not written out cannot be read as source.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return True, value if isinstance(value, ast.List) else None
    return False, None


def _string_value(node: ast.expr) -> str | None:
    """The value of a string literal, or ``None`` when it is not one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _element_id(element: ast.expr) -> str | None:
    """The id a row carries, or ``None`` when it carries none we can read.

    A row is written ``Case(id="...", inputs=..., expected=...)``; a
    plain ``{"id": "..."}`` row is accepted as well.  The id has to be
    a literal — an id built by a call cannot be read without running
    the table, which is exactly what this gate does not do.
    """
    if isinstance(element, ast.Call):
        for keyword in element.keywords:
            if keyword.arg == "id":
                return _string_value(keyword.value)
    if isinstance(element, ast.Dict):
        for key, value in zip(element.keys, element.values):
            if isinstance(key, ast.Constant) and key.value == "id":
                return _string_value(value)
    return None


def _empty_tables(manifest: dict, root: Path) -> list[str]:
    """The named tables that do not hold a non-empty list literal."""
    problems: list[str] = []
    for point, path, name in _named_tables(manifest, root):
        label = _row_label(point)
        tree = _parse_table(path)
        if tree is None:
            problems.append(
                f"Tier A point {label} names the table {path}, which does "
                "not parse as Python"
            )
            continue
        found, rows = _module_level_list(tree, name)
        if not found:
            problems.append(
                f"Tier A point {label} names the table {path}, which has no "
                f"module-level {name}"
            )
            continue
        if rows is None:
            problems.append(
                f"Tier A point {label} names the table {path}, whose {name} "
                "is not a list literal: the rows have to be written out so "
                "that they can be read as source"
            )
            continue
        if not rows.elts:
            problems.append(
                f"Tier A point {label} names the table {path}, whose {name} "
                "holds no row: a table with no rows pins nothing"
            )
    return problems


def _bad_case_ids(manifest: dict, root: Path) -> list[str]:
    """The named tables whose row ids are unreadable, repeated or blank."""
    problems: list[str] = []
    for point, path, name in _named_tables(manifest, root):
        label = _row_label(point)
        tree = _parse_table(path)
        if tree is None:
            continue  # named by the empty-table check
        found, rows = _module_level_list(tree, name)
        if not found or rows is None or not rows.elts:
            continue  # named by the empty-table check
        seen: dict[str, int] = {}
        for index, element in enumerate(rows.elts):
            case_id = _element_id(element)
            if case_id is None:
                problems.append(
                    f"Tier A point {label}: row #{index} of {path} carries no "
                    "id we can read: an id has to be a literal, so that the "
                    "rows can be found without running the table"
                )
                continue
            if _PLACEHOLDER_ID.match(case_id):
                problems.append(
                    f"Tier A point {label}: row #{index} of {path} has the "
                    f"placeholder id {case_id!r}: an id has to say what the "
                    "row pins, not how many rows there are"
                )
            if case_id in seen:
                problems.append(
                    f"Tier A point {label}: row #{index} of {path} repeats the "
                    f"id {case_id!r}, which row #{seen[case_id]} already uses: "
                    "ids are how a reader finds a row"
                )
            else:
                seen[case_id] = index
    return problems


# ---------------------------------------------------------------------------
# The four cases the material names
# ---------------------------------------------------------------------------


def test_every_tier_a_point_has_a_table() -> None:
    """Every Tier A point names the table that pins it.

    A Tier A point is a function whose return value decides PASS/FAIL.
    A row that does not name a table leaves that function unpinned, and
    the manifest is the only place that says so — nothing else in the
    tree would notice the row going quiet.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _missing_tables(manifest)

    assert not problems, "\n".join(problems)


def test_table_files_exist() -> None:
    """Every table the manifest names is a file in the repository.

    A name that does not resolve names nothing: the reader who follows
    it finds no table, and the row is a claim about a file that is not
    there.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _missing_files(manifest, _REPO_ROOT)

    assert not problems, "\n".join(problems)


def test_cases_are_non_empty_literal() -> None:
    """Every named table holds a non-empty list literal of rows.

    A table with no rows pins nothing, and a table whose rows are built
    by a call cannot be read as source — which is what this gate reads.
    Both are the same failure: the manifest names a table, and there is
    no table there to run.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _empty_tables(manifest, _REPO_ROOT)

    assert not problems, "\n".join(problems)


def test_case_ids_are_unique_and_descriptive() -> None:
    """Every row carries an id that is unique and says what the row pins.

    The ids are how a reader finds a row, so a repeated id makes the
    second row unreachable by name, and an id that is only a counter
    (``case1`` / ``情况2``) says nothing about why the row is there.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _bad_case_ids(manifest, _REPO_ROOT)

    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The negative controls: each check has to be able to go red
# ---------------------------------------------------------------------------


def _demo_table_source(case_ids: list[str]) -> str:
    """The source of a demo table whose rows carry ``case_ids``."""
    rows = "\n".join(
        f"    Case(id={json.dumps(case_id)}, inputs=(), expected=None),"
        for case_id in case_ids
    )
    return "CASES = [\n" + rows + "\n]\n"


def _fixture(root: Path, table_source: str, table_ref: str | None = None) -> dict:
    """Write a one-row manifest and its table under ``root``.

    ``table_ref`` overrides the reference the row carries — that is how
    a control points at a table which is not there.
    """
    table_path = root / _DEMO_TABLE
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(table_source, encoding="utf-8")
    return {
        "schema_version": 1,
        "points": [
            {
                "module": "demo.py",
                "function": "demo_judgment",
                "tier": _TIER,
                "table": table_ref or f"{_DEMO_TABLE}::CASES",
                "return_shape": "bool",
                "pinned_by": None,
                "covers_verdict_write": True,
            }
        ],
    }


def test_a_tier_a_point_without_a_table_is_named(tmp_path: Path) -> None:
    """A Tier A row with no table is reported, not accepted."""
    manifest = _fixture(tmp_path, _demo_table_source(["one-row"]))
    manifest["points"][0]["table"] = None

    problems = _missing_tables(manifest)

    assert problems, "a Tier A point with no table passed the table check"
    assert "no table" in problems[0]


def test_a_tier_a_point_with_a_malformed_table_reference_is_named(
    tmp_path: Path,
) -> None:
    """A Tier A row naming a table it cannot say how to reach is reported."""
    manifest = _fixture(tmp_path, _demo_table_source(["one-row"]))
    manifest["points"][0]["table"] = "backend/tests/unit/test_decision_table_demo.py"

    problems = _missing_tables(manifest)

    assert problems, "a malformed table reference passed the table check"
    assert "'<path>::<name>'" in problems[0]


def test_a_manifest_with_no_tier_a_row_is_named(tmp_path: Path) -> None:
    """A manifest that names no Tier A point at all is reported.

    A gate over an empty room cannot fail, and this one would go green
    over a manifest where every Tier A row had been demoted.
    """
    manifest = _fixture(tmp_path, _demo_table_source(["one-row"]))
    manifest["points"][0]["tier"] = "B"

    problems = _missing_tables(manifest)

    assert problems, "a manifest with no Tier A row passed the table check"
    assert "no Tier A" in problems[0]


def test_a_table_file_that_is_missing_is_named(tmp_path: Path) -> None:
    """A manifest naming a table that is not on disk is reported."""
    manifest = _fixture(
        tmp_path,
        _demo_table_source(["one-row"]),
        table_ref="backend/tests/unit/test_decision_table_absent.py::CASES",
    )

    problems = _missing_files(manifest, tmp_path)

    assert problems, "a manifest naming a missing table file passed the check"
    assert "test_decision_table_absent.py" in problems[0]


def test_a_table_without_a_case_list_is_named(tmp_path: Path) -> None:
    """A table file that has no module-level ``CASES`` is reported."""
    manifest = _fixture(tmp_path, "SOMETHING_ELSE = [1]\n")

    problems = _empty_tables(manifest, tmp_path)

    assert problems, "a table file with no CASES passed the empty-table check"
    assert "module-level CASES" in problems[0]


def test_a_table_whose_cases_are_not_module_level_is_named(
    tmp_path: Path,
) -> None:
    """A ``CASES`` that is not at module level is reported.

    A table built inside a function is a table the manifest cannot
    point at: the reference names a module-level name, and there is
    none.
    """
    manifest = _fixture(
        tmp_path,
        "def _table():\n    CASES = [Case(id='row-one')]\n    return CASES\n",
    )

    problems = _empty_tables(manifest, tmp_path)

    assert problems, "a table whose CASES is not module level passed"
    assert "module-level CASES" in problems[0]


def test_an_empty_case_list_is_named(tmp_path: Path) -> None:
    """A table whose case list holds no row is reported.

    A table with no rows pins nothing, and the manifest still says the
    point is pinned.
    """
    manifest = _fixture(tmp_path, "CASES = []\n")

    problems = _empty_tables(manifest, tmp_path)

    assert problems, "a table with an empty case list passed the check"
    assert "holds no row" in problems[0]


def test_a_case_list_that_is_not_a_literal_is_named(tmp_path: Path) -> None:
    """A table whose rows are built by a call is reported.

    The rows cannot be read as source, so the ids cannot be read
    either — and the manifest's claim that this point is pinned cannot
    be checked at all.
    """
    manifest = _fixture(
        tmp_path,
        "def _rows():\n    return []\n\n\nCASES = _rows()\n",
    )

    problems = _empty_tables(manifest, tmp_path)

    assert problems, "a table whose rows are not a literal passed the check"
    assert "not a list literal" in problems[0]


def test_a_duplicate_case_id_is_named(tmp_path: Path) -> None:
    """Two rows with the same id are reported."""
    manifest = _fixture(tmp_path, _demo_table_source(["same-id", "same-id"]))

    problems = _bad_case_ids(manifest, tmp_path)

    assert problems, "a table with a repeated id passed the id check"
    assert "repeats the id" in problems[0]


def test_a_placeholder_case_id_is_named(tmp_path: Path) -> None:
    """An id that is only a counter is reported."""
    manifest = _fixture(tmp_path, _demo_table_source(["case1"]))

    problems = _bad_case_ids(manifest, tmp_path)

    assert problems, "a table with a placeholder id passed the id check"
    assert "placeholder id" in problems[0]


def test_the_tables_are_read_without_being_imported() -> None:
    """The gate reads the tables as source; it does not import them.

    A table module imports the code it pins and builds its rows at
    import time, so importing one would run the code under inspection
    inside the gate's own process.  This test runs every check and
    then asks the interpreter what it loaded.
    """
    before = set(sys.modules)

    manifest = _read_manifest(_MANIFEST)
    _missing_tables(manifest)
    _missing_files(manifest, _REPO_ROOT)
    _empty_tables(manifest, _REPO_ROOT)
    _bad_case_ids(manifest, _REPO_ROOT)

    table_paths = set()
    for point in _tier_a_points(manifest):
        split = _split_ref(point.get("table"))
        if split is None:
            continue
        table_paths.add(str((_REPO_ROOT / split[0]).resolve()))

    imported = []
    for name in set(sys.modules) - before:
        module = sys.modules.get(name)
        file = getattr(module, "__file__", None)
        if file and str(Path(file).resolve()) in table_paths:
            imported.append(name)

    assert not imported, f"the gate imported the table modules: {imported}"
