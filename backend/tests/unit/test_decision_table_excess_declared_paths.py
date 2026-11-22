"""Table 5 — the paths that changed without ever being declared.

Why this table exists
---------------------
Material R1 asks the executor to compare what git actually reports as
changed against the task's ``files_to_modify`` before the task is
allowed to complete.  The Edit/Write guard cannot answer that question:
it only sees tool calls, so a write performed by a shell command slips
past it entirely.

:func:`agent.excess_declared_paths` is the pure rule that comparison
needs — no git, no filesystem, no IO; the caller supplies the change
set and the function only judges it.  It is a Tier A point: what it
returns decides whether the undeclared-write gate fires, and a wrong
``[]`` lets a task that wrote outside its declaration complete.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``undeclared-path-reported`` — a path that changed and was not
  declared is named;
* ``declared-only-clean`` — a change set the declaration covers reports
  nothing;
* ``missing-declaration-disables`` — ``None`` means "the task declared
  nothing", which is NOT "the task declared the empty set": the check
  is off, not "every changed path is excess";
* ``empty-declaration-disables`` — the empty list is the same unusable
  declaration;
* ``unknown-sentinel-disables`` — the "unknown modifications" sentinel
  is unusable too;
* ``no-changes-sentinel-disables`` — so is the "no file changes"
  sentinel;
* ``runtime-artifacts-ignored`` — plan bookkeeping, scratch space,
  PDT's own state, lock files and log files belong to the runtime, not
  to the task's deliverable: flagging them would make the gate fire on
  the runtime's own bookkeeping;
* ``duplicates-collapsed-sorted`` — one file written three ways is one
  path, and the answer comes back in a fixed order;
* ``windows-separator-matches-the-declaration`` — a path that came back
  from a shell with a backslash in it is the file the declaration
  names;
* ``blank-path-is-not-a-path`` — an empty entry in the change set names
  no file, so there is nothing to report;
* ``no-changes-reports-nothing`` — nothing changed, so there is nothing
  to report.

Scope, stated honestly: this file **reads** the function it pins.  It
never writes to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import excess_declared_paths  # noqa: E402
from task import NO_FILE_CHANGES_SENTINEL, UNKNOWN_MODIFICATIONS_SENTINEL  # noqa: E402
from tests.judgment_tables import Case, assert_decision_table  # noqa: E402

#: The repository root — three parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest that records which table pins which judgment point.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The manifest's row for the point this table pins.
_MANIFEST_ROW = ("agent.py", "excess_declared_paths")
#: The value that row's ``table`` field has to carry.
_MANIFEST_TABLE = (
    "backend/tests/unit/test_decision_table_excess_declared_paths.py::CASES"
)

#: The sentinel that says "this task's modifications are unknown".
_UNKNOWN_SENTINEL = list(UNKNOWN_MODIFICATIONS_SENTINEL)
#: The sentinel that says "this task changes no file".
_NO_CHANGES_SENTINEL = list(NO_FILE_CHANGES_SENTINEL)


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the function.
CASES = [
    Case(
        id="undeclared-path-reported",
        inputs=(
            ["a.py", "b.py"],
            ["a.py"],
        ),
        expected=["b.py"],
    ),
    Case(
        id="declared-only-clean",
        inputs=(
            ["a.py"],
            ["a.py"],
        ),
        expected=[],
    ),
    Case(
        id="missing-declaration-disables",
        inputs=(
            ["a.py"],
            None,
        ),
        expected=[],
    ),
    Case(
        id="empty-declaration-disables",
        inputs=(
            ["a.py"],
            [],
        ),
        expected=[],
    ),
    Case(
        id="unknown-sentinel-disables",
        inputs=(
            ["a.py"],
            list(_UNKNOWN_SENTINEL),
        ),
        expected=[],
    ),
    Case(
        id="no-changes-sentinel-disables",
        inputs=(
            ["a.py"],
            list(_NO_CHANGES_SENTINEL),
        ),
        expected=[],
    ),
    Case(
        id="runtime-artifacts-ignored",
        inputs=(
            [
                "plans/P-1/tasks.json",
                "tmp/scratch.py",
                ".pdt/state.json",
                "run.lock",
                "run.log",
            ],
            ["a.py"],
        ),
        expected=[],
    ),
    Case(
        id="duplicates-collapsed-sorted",
        inputs=(
            ["b.py", "a.py", "./b.py"],
            ["z.py"],
        ),
        expected=["a.py", "b.py"],
    ),
    Case(
        id="windows-separator-matches-the-declaration",
        inputs=(
            ["a\\b.py"],
            ["a/b.py"],
        ),
        expected=[],
    ),
    Case(
        id="blank-path-is-not-a-path",
        inputs=(
            ["", "   "],
            ["a.py"],
        ),
        expected=[],
    ),
    Case(
        id="no-changes-reports-nothing",
        inputs=(
            [],
            ["a.py"],
        ),
        expected=[],
    ),
]


def test_decision_table() -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of :func:`agent.excess_declared_paths`.  A table
    that left a branch unreached would be a table that let a new branch
    in unnoticed.
    """
    assert_decision_table(excess_declared_paths, CASES)


def test_manifest_points_at_this_table() -> None:
    """The manifest names this table as the one that pins the point.

    A manifest row whose ``table`` field points at a file that does not
    exist — or at a table that does not carry the case list — reads as
    "this point is pinned" while nothing pins it.  The link is checked
    here, where the table it names lives, so a rename of either side
    shows up as a red test instead of as a manifest nobody reads.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    rows = [
        point
        for point in manifest["points"]
        if (point["module"], point["function"]) == _MANIFEST_ROW
    ]

    assert len(rows) == 1, (
        f"the manifest holds {len(rows)} rows for "
        f"{_MANIFEST_ROW[0]}::{_MANIFEST_ROW[1]}; it has to hold exactly one"
    )
    assert rows[0]["table"] == _MANIFEST_TABLE, (
        f"the manifest's row for {_MANIFEST_ROW[0]}::{_MANIFEST_ROW[1]} "
        f"points at {rows[0]['table']!r}; it has to point at "
        f"{_MANIFEST_TABLE!r}"
    )
