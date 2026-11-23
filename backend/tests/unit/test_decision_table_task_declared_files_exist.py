"""Table 6 — may a task with an empty diff still complete?

Why this table exists
---------------------
An empty ``changed_files`` is not automatically a failure.  A re-run of
a task whose deliverable was already committed in an earlier attempt
legitimately produces no new diff, and blocking it would burn tokens
re-running work that is already done.  But the opposite mistake is the
expensive one: a subagent that produced *nothing* must not be allowed
to complete on an empty diff.

:meth:`agent.AutonomousAgent._task_declared_files_exist` is the
judgment that tells the two apart.  It is a Tier A point: its value
decides whether the empty-output gate opens or shuts, so a wrong
``True`` lets a task that produced nothing reach ``completed``.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``all-declared-exist`` — every file the task declares it will modify
  is on disk, so the work is genuinely done and the empty diff is a
  legitimate no-op;
* ``one-missing-file-fails`` — ONE missing deliverable is enough: every
  declared file has to be there, not just some of them;
* ``sentinel-without-commit-fails`` — a sentinel declares no concrete
  file, so the answer comes from git history, and a task that never
  committed anything has no history to point at;
* ``sentinel-with-prior-commit-passes`` — the same task after an
  earlier attempt committed its deliverable: the empty re-run diff is
  a legitimate no-op and may complete;
* ``missing-project-dir-fails`` — without a tree to look in, the gate
  cannot see any deliverable, so it says no;
* ``oserror-while-probing-is-a-failure`` — a declared path the
  filesystem refuses even to name makes the probe raise ``OSError``;
  the gate answers "the deliverable is not there" instead of letting
  the error escape and take the run down with it.

The subject files
-----------------
``a.py`` and ``b.py`` are created by the test under pytest's
``tmp_path``.  They exist only while the test runs and are never written
into the repository.

Scope, stated honestly: this file **reads** the method it pins.  It
never writes to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task import UNKNOWN_MODIFICATIONS_SENTINEL  # noqa: E402
from tests.judgment_tables import Case, assert_decision_table  # noqa: E402

#: The repository root — three parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest that records which table pins which judgment point.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The manifest's row for the point this table pins.
_MANIFEST_ROW = ("agent.py", "AutonomousAgent._task_declared_files_exist")
#: The value that row's ``table`` field has to carry.
_MANIFEST_TABLE = (
    "backend/tests/unit/test_decision_table_task_declared_files_exist.py"
    "::CASES"
)

#: The sentinel that says "this task's modifications are unknown".
_UNKNOWN_SENTINEL = list(UNKNOWN_MODIFICATIONS_SENTINEL)

#: A file name no filesystem can hold: POSIX caps a single path
#: component at 255 bytes, so a 5000-character component makes the
#: stat fail with ``ENAMETOOLONG`` — an ``OSError`` that pathlib does
#: not swallow, and therefore the one way the ``except OSError`` arm of
#: the pinned method is reachable.
_HOSTILE_NAME = "x" * 5000


class _Tree:
    """The tree the table's paths resolve against.

    The table is a module-level list, so the tree it talks about has
    to travel through the rows.  This holder carries it: the test
    fills in the path before the rows run, and the method under test
    reads it through ``__fspath__`` — which is what :class:`pathlib.Path`
    accepts.
    """

    def __init__(self) -> None:
        self.path: Optional[Path] = None

    def __fspath__(self) -> str:
        return "" if self.path is None else str(self.path)


#: The tree the declared files live in.
_TREE = _Tree()

#: A tree that is not there — the gate cannot see a deliverable
#: without a tree to look in.
_ABSENT_TREE = _Tree()

#: The agent stand-in: it carries the tree the task does not name and
#: the answer to "has this task committed anything before?".  It is a
#: :class:`types.SimpleNamespace` rather than a real agent because the
#: method under test is called unbound, with the stand-in as ``self``.
_NO_PRIOR_COMMIT = SimpleNamespace(
    project_dir=_TREE,
    _task_has_prior_commit=lambda *args: False,
)
_PRIOR_COMMIT = SimpleNamespace(
    project_dir=_TREE,
    _task_has_prior_commit=lambda *args: True,
)

#: A task whose declared deliverables are both on disk.
_TASK_ALL_EXIST = SimpleNamespace(files_to_modify=["a.py", "b.py"])
#: A task one of whose declared deliverables is missing.
_TASK_ONE_MISSING = SimpleNamespace(files_to_modify=["a.py", "missing.py"])
#: A task that declares no concrete file at all.
_TASK_SENTINEL = SimpleNamespace(files_to_modify=list(_UNKNOWN_SENTINEL))
#: A task whose tree is not on disk.
_TASK_ABSENT_TREE = SimpleNamespace(
    files_to_modify=["a.py"], project_dir=_ABSENT_TREE,
)
#: A task whose declared deliverable the filesystem cannot even name.
_TASK_HOSTILE_NAME = SimpleNamespace(files_to_modify=[_HOSTILE_NAME])


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the method.
CASES = [
    Case(
        id="all-declared-exist",
        inputs=(_NO_PRIOR_COMMIT, _TASK_ALL_EXIST),
        expected=True,
    ),
    Case(
        id="one-missing-file-fails",
        inputs=(_NO_PRIOR_COMMIT, _TASK_ONE_MISSING),
        expected=False,
    ),
    Case(
        id="sentinel-without-commit-fails",
        inputs=(_NO_PRIOR_COMMIT, _TASK_SENTINEL),
        expected=False,
    ),
    Case(
        id="sentinel-with-prior-commit-passes",
        inputs=(_PRIOR_COMMIT, _TASK_SENTINEL),
        expected=True,
    ),
    Case(
        id="missing-project-dir-fails",
        inputs=(_NO_PRIOR_COMMIT, _TASK_ABSENT_TREE),
        expected=False,
    ),
    Case(
        id="oserror-while-probing-is-a-failure",
        inputs=(_NO_PRIOR_COMMIT, _TASK_HOSTILE_NAME),
        expected=False,
    ),
]


def _subject_tree(tmp_path: Path) -> Path:
    """A tree with the two files the table talks about.

    Both live under pytest's ``tmp_path``, so they exist only for this
    test and are never written into the repository.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (project / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    return project


def test_decision_table(tmp_path: Path) -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of
    :meth:`agent.AutonomousAgent._task_declared_files_exist`.  A table
    that left a branch unreached would be a table that let a new branch
    in unnoticed.
    """
    project = _subject_tree(tmp_path)
    _TREE.path = project
    _ABSENT_TREE.path = project / "no-tree-here"

    assert_decision_table(
        AutonomousAgent._task_declared_files_exist, CASES
    )


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
