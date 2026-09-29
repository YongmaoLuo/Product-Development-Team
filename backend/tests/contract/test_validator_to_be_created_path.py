"""Contract: step-3 accepts a file the task will CREATE (2026-09-14).

Background
----------
``files_to_modify`` is a conflict key and a prompt hint: the dispatcher
builds its parallel-dispatch graph from the shared paths between tasks
(``agent._build_layers``), and the task prompt hands the list to the
executor subagent as a hint. A subtask may legitimately CREATE a file,
and that path cannot exist yet — but before this change the only two
ways to satisfy step-3 were "the file already exists" and "git history
deleted it". A create-task therefore had **no expressible**
``files_to_modify``: the generator's only option was to mislabel it
read-only. That mislabel also drops the task's conflict key, so it races
with the tasks that really do touch those files.

The rule (``framework.task_output_validator.is_to_be_created_path``)
keeps three conditions, all required, because an existence check that
accepts every missing path is a no-op:

  * the RAW entry names a path with a parent (``a/b.py``). A bare
    ``ghost.py`` is rejected: it and a genuine new ``ghost.py`` at the
    project root are the same string, so there is no evidence to
    distinguish a typo from an intent. This keeps the repo's pinned
    "never existed ⇒ still fails" contract intact
    (``test_validator_git_history_fallback.py``).
  * the path resolves inside ``project_dir`` — an out-of-project
    absolute path and a ``..`` traversal are still rejected.
  * the FIRST segment is a real directory of the project
    (``backend/``, ``native_ext/``, ``tests/``) — an invented top-level
    tree (``no_such_dir/ghost.py``) is still rejected.

2026-09-21 update: the parent directory no longer has to exist.
Requiring it never caught
the hallucination it claimed to catch (a typo'd FILE NAME inside a real
directory passed) while it hard-rejected the ordinary shape "this task
creates the first file in a new subdirectory" — a live plan
(a production plan) died on exactly that at the pre-run
gate, 0.4s after a successful ``/start``.

Since 2026-09-21 the predicate is used by the GENERATION-time gate
(``TasksGenerator.harden_for_execution``) — the execution-side pre-run
gate that carried the same check was removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework.task_output_validator import (  # noqa: E402
    TaskOutputValidator,
    is_to_be_created_path,
)
from task import SubTask  # noqa: E402


def _task(files_to_modify) -> SubTask:
    return SubTask(
        id="to-create",
        title="Create a new module",
        description="",
        test_command="echo noop",
        files_to_modify=files_to_modify,
        depends_on=[],
    )


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


class TestIsToBeCreatedPath:
    def test_relative_path_in_existing_dir_is_accepted(self, tmp_path: Path):
        (tmp_path / "backend" / "api").mkdir(parents=True)
        raw = "backend/api/routers.py"
        assert is_to_be_created_path(raw, tmp_path / raw, tmp_path) is True

    def test_bare_filename_is_rejected(self, tmp_path: Path):
        assert is_to_be_created_path("ghost.py", tmp_path / "ghost.py", tmp_path) is False

    def test_invented_top_level_directory_is_rejected(self, tmp_path: Path):
        """The surviving hallucination guard: ``no_such_dir`` is not a
        real entry of the project, so the whole path is suspect."""
        raw = "no_such_dir/ghost.py"
        assert is_to_be_created_path(raw, tmp_path / raw, tmp_path) is False

    def test_missing_parent_below_a_real_top_level_is_accepted(self, tmp_path: Path):
        """The 2026-09-21 relaxation — the shape that killed plan
        a production plan: the task creates the first
        file in a subdirectory that does not exist yet."""
        (tmp_path / "native_ext").mkdir()
        raw = "native_ext/docs/audit-0f0f0f0f.md"
        assert is_to_be_created_path(raw, tmp_path / raw, tmp_path) is True

    def test_deeply_nested_new_directories_are_accepted(self, tmp_path: Path):
        (tmp_path / "tests").mkdir()
        raw = "tests/integration/golden/golden_outputs.json"
        assert is_to_be_created_path(raw, tmp_path / raw, tmp_path) is True

    def test_absolute_path_inside_project_is_accepted(self, tmp_path: Path):
        (tmp_path / "backend").mkdir()
        target = tmp_path / "backend" / "api" / "routers.py"
        assert is_to_be_created_path(str(target), target, tmp_path) is True

    def test_out_of_project_parent_is_rejected(self, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        raw = str(outside / "new.py")
        assert is_to_be_created_path(raw, Path(raw), project) is False

    def test_parent_that_is_a_file_is_rejected(self, tmp_path: Path):
        blocker = tmp_path / "blocker.py"
        blocker.write_text("x", encoding="utf-8")
        raw = "blocker.py/nested.py"
        assert is_to_be_created_path(raw, tmp_path / raw, tmp_path) is False

    def test_out_of_root_traversal_is_rejected(self, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        raw = "../escape.py"
        assert is_to_be_created_path(raw, project / raw, project) is False


# ---------------------------------------------------------------------------
# The dispatcher-side gate
# ---------------------------------------------------------------------------


class TestStep3AcceptsToBeCreated:
    def test_new_file_in_existing_package_is_accepted(self, tmp_path: Path):
        (tmp_path / "backend" / "api").mkdir(parents=True)
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate([_task(["backend/api/routers.py"])])
        assert report.status == "passed", report.reasons

    def test_new_file_mixed_with_existing_file_is_accepted(self, tmp_path: Path):
        (tmp_path / "backend" / "api").mkdir(parents=True)
        (tmp_path / "backend" / "server.py").write_text("x", encoding="utf-8")
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate(
            [_task(["backend/server.py", "backend/api/routers.py"])]
        )
        assert report.status == "passed", report.reasons

    def test_bare_filename_still_fails(self, tmp_path: Path):
        """The pinned "never existed ⇒ still fails" contract survives."""
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate([_task(["never_existed_xyz.py"])])
        assert report.status == "failed"
        assert 3 in report.failed_steps
        assert any(
            "never_existed_xyz.py" in r for r in report.reasons
        ), report.reasons

    def test_new_file_in_missing_subdirectory_is_accepted(self, tmp_path: Path):
        """End-to-end shape from a production plan:
        ``native_ext/`` exists, ``native_ext/docs/`` does not."""
        (tmp_path / "native_ext").mkdir()
        (tmp_path / "native_ext" / "src").mkdir()
        (tmp_path / "native_ext" / "src" / "core.rs").write_text("x", encoding="utf-8")
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate(
            [_task(["native_ext/docs/audit-0f0f0f0f.md", "native_ext/src/core.rs"])]
        )
        assert report.status == "passed", report.reasons

    def test_missing_directory_still_fails(self, tmp_path: Path):
        """Only the *top-level* segment is load-bearing now; an invented
        one is still rejected."""
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate([_task(["no_such_dir/ghost.py"])])
        assert report.status == "failed"
        assert 3 in report.failed_steps

    def test_read_only_sentinel_still_accepted(self, tmp_path: Path):
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate([_task(["__NO_FILE_CHANGES__"])])
        assert report.status == "passed", report.reasons

    def test_unknown_sentinel_still_rejected(self, tmp_path: Path):
        """The fill-loop trigger must not be weakened by this change."""
        validator = TaskOutputValidator(project_dir=tmp_path)
        report = validator.validate([_task(["__UNKNOWN_MODIFICATIONS__"])])
        assert report.status == "failed"
        assert 3 in report.failed_steps
