"""TDD spec — ``backend/verification/scope_claims.py``, the failing-file
ownership table.

What this module is for
=======================

Section 1 of the plan: when a repair round's gate fails on files that
**no task declared**, the round must not be judged dead outright. The
missing piece is the mapping "failing file -> the task that declares
it". Without it there is no way to tell "task 3 broke" from "nobody
owns this file", and the second case is the one that has to reach the
refiner as work to do rather than as a verdict.

So the module answers one question — *who owns this failing file?* —
with three answers, because "nobody" and "somewhere else entirely" are
different problems:

``claims``
    The failing file, and the ids of the tasks that declare it.
``unclaimed``
    The failing file is inside the project and **no** task declares it.
    This is the list the refiner has to be given.
``out_of_project``
    The failing file is not inside this plan's ``project_dir`` at all —
    another worktree, an absolute path elsewhere, a ``../`` escape. It
    is not this plan's business, and claiming it would be a lie.

Pure function
=============

No I/O, no clock, no dependence on ``hash()`` seeds. The same input
always gives the same table, so the table can be written to disk and
compared across rounds.

TDD contract — 4 cases
-----------------------

1. ``test_failed_files_from_output`` — a FAILED line yields the file
   without its ``::node`` suffix.
2. ``test_claim_marks_unclaimed`` — a failing file no task declares
   lands in ``unclaimed``.
3. ``test_foreign_path_goes_to_out_of_project`` — a path under another
   worktree lands in ``out_of_project`` and nowhere else.
4. ``test_relative_escape_goes_to_out_of_project`` — ``../other/x.py``
   lands in ``out_of_project`` and nowhere else.

The boundary conditions the plan spells out are pinned by the same
file: an empty output gives three empty tables, a declared directory
covers every file under it, and paths are normalised (no ``./``, no
trailing slash, no doubled separator) before they are compared.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
for _path in (BACKEND_DIR, REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from task import UNKNOWN_MODIFICATIONS_SENTINEL  # noqa: E402
from verification.scope_claims import (  # noqa: E402
    claim_failed_files,
    failed_files_from_output,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tasks(*specs):
    """``(("3", "backend/agent.py"), ...)`` -> the task dicts.

    A declared path may be given as one string or as a list of them.
    """
    return [
        {
            "id": task_id,
            "files_to_modify": (
                list(files) if isinstance(files, (list, tuple)) else [files]
            ),
        }
        for task_id, files in specs
    ]


# ---------------------------------------------------------------------------
# 1. the failing files a run output names
# ---------------------------------------------------------------------------
def test_failed_files_from_output():
    out = "FAILED backend/tests/unit/test_a.py::test_x - assert 1 == 2"

    assert failed_files_from_output(out) == ["backend/tests/unit/test_a.py"]


def test_failed_files_from_output_strips_node_and_deduplicates():
    """Two cases in one file are one file to own."""
    out = (
        "FAILED backend/tests/unit/test_a.py::test_x - assert\n"
        "FAILED backend/tests/unit/test_a.py::test_y - assert\n"
        "FAILED backend/tests/unit/test_b.py::test_z - assert"
    )

    assert failed_files_from_output(out) == [
        "backend/tests/unit/test_a.py",
        "backend/tests/unit/test_b.py",
    ]


def test_failed_files_from_output_is_empty_for_empty_output():
    assert failed_files_from_output("") == []


# ---------------------------------------------------------------------------
# 2. the ownership table
# ---------------------------------------------------------------------------
def test_claim_marks_unclaimed(tmp_path):
    """A failing file no task declares is the refiner's business."""
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files(["backend/coding_tool.py"], tasks, str(tmp_path))

    assert result["unclaimed"] == ["backend/coding_tool.py"]
    assert result["claims"] == {}
    assert result["out_of_project"] == []


def test_claim_maps_declared_file_to_task(tmp_path):
    """A failing file a task declares belongs to that task."""
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files(["backend/agent.py"], tasks, str(tmp_path))

    assert result["claims"] == {"backend/agent.py": ["3"]}
    assert result["unclaimed"] == []
    assert result["out_of_project"] == []


def test_two_tasks_can_claim_one_file(tmp_path):
    tasks = _tasks(("1", "backend/agent.py"), ("2", "backend/agent.py"))

    result = claim_failed_files(["backend/agent.py"], tasks, str(tmp_path))

    assert result["claims"] == {"backend/agent.py": ["1", "2"]}


def test_declared_directory_covers_files_under_it(tmp_path):
    """A declaration of a directory claims every file below it."""
    tasks = _tasks(("7", "backend/tests/unit"))

    result = claim_failed_files(
        ["backend/tests/unit/test_a.py"], tasks, str(tmp_path)
    )

    assert result["claims"] == {"backend/tests/unit/test_a.py": ["7"]}
    assert result["unclaimed"] == []


def test_paths_are_normalised_before_they_are_compared(tmp_path):
    """``./``, a trailing slash and a doubled separator are spelling."""
    tasks = _tasks(("3", "./backend/agent.py"))

    result = claim_failed_files(["backend//agent.py"], tasks, str(tmp_path))

    assert result["claims"] == {"backend/agent.py": ["3"]}


def test_declared_directory_with_trailing_slash_covers_files_under_it(tmp_path):
    tasks = _tasks(("7", "backend/tests/unit/"))

    result = claim_failed_files(
        ["backend/tests/unit/test_a.py"], tasks, str(tmp_path)
    )

    assert result["claims"] == {"backend/tests/unit/test_a.py": ["7"]}


def test_sentinel_declaration_claims_nothing(tmp_path):
    """``__UNKNOWN_MODIFICATIONS__`` declares no file."""
    tasks = [{"id": "3", "files_to_modify": list(UNKNOWN_MODIFICATIONS_SENTINEL)}]

    result = claim_failed_files(["backend/agent.py"], tasks, str(tmp_path))

    assert result["claims"] == {}
    assert result["unclaimed"] == ["backend/agent.py"]


def test_empty_output_yields_three_empty_tables(tmp_path):
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files(
        failed_files_from_output(""), tasks, str(tmp_path)
    )

    assert result == {"claims": {}, "unclaimed": [], "out_of_project": []}


def test_a_bare_path_string_is_one_file(tmp_path):
    """A string is one path, not a sequence of characters."""
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files("backend/agent.py", tasks, str(tmp_path))

    assert result["claims"] == {"backend/agent.py": ["3"]}


def test_a_bare_task_dict_is_one_task(tmp_path):
    """A dict is one task, not a sequence of its keys."""
    result = claim_failed_files(
        ["backend/agent.py"],
        {"id": "3", "files_to_modify": ["backend/agent.py"]},
        str(tmp_path),
    )

    assert result["claims"] == {"backend/agent.py": ["3"]}


# ---------------------------------------------------------------------------
# 3. paths that are not this plan's business
# ---------------------------------------------------------------------------
def test_foreign_path_goes_to_out_of_project(tmp_path):
    """A path under another worktree is nobody's file here."""
    foreign = "/tmp/other-worktree/backend/tests/unit"
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files([foreign], tasks, str(tmp_path))

    assert result["out_of_project"] == [foreign]
    assert result["claims"] == {}
    assert result["unclaimed"] == []


def test_relative_escape_goes_to_out_of_project(tmp_path):
    """``../other/x.py`` leaves the project even though it looks relative."""
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files(["../other/x.py"], tasks, str(tmp_path))

    assert result["out_of_project"] == ["../other/x.py"]
    assert result["claims"] == {}
    assert result["unclaimed"] == []


def test_absolute_path_inside_the_project_is_claimed(tmp_path):
    """An absolute path that IS inside the project is a normal file."""
    tasks = _tasks(("3", "backend/agent.py"))

    result = claim_failed_files(
        [str(tmp_path / "backend" / "agent.py")], tasks, str(tmp_path)
    )

    assert result["claims"] == {"backend/agent.py": ["3"]}
    assert result["out_of_project"] == []
