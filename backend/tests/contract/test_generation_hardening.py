"""Contract: task validation happens at GENERATION time (2026-09-21).

Everything must be validated at task-generation time: there is no
pre-run gate. Once a plan is executable, starting it should succeed
immediately.

``TasksGenerator.harden_for_execution`` is now the ONLY place the 4-step
``TaskOutputValidator`` runs. It validates every task against its OWN
``project_dir`` — the execution side used a single run-level directory for
the whole snapshot, which is a different (and wrong) question.

What it must guarantee:

  * a task that CREATES a file in a directory that does not exist yet is
    fine (a production plan died on five of those);
  * a task whose declared paths do not resolve under its own
    ``project_dir`` is a HARD failure — the operator fixes it and
    re-generates, instead of the executor discovering it 0.4 s after
    ``/start``;
  * the unresolved-files sentinel is reported, not fatal (resolving it
    needs repo inspection, which no longer happens at execution time);
  * ``auto_fix`` results are copied back without clobbering the
    bookkeeping keys the generator added to each dict.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tasks_generator import TasksGenerationError, TasksGenerator  # noqa: E402


def _generator() -> TasksGenerator:
    """``harden_for_execution`` is pure — no coding tool needed."""
    return TasksGenerator.__new__(TasksGenerator)


def _task(task_id: str, project_dir: str, files: list, **extra) -> dict:
    task = {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "",
        "test_command": "echo noop",
        "files_to_modify": files,
        "depends_on": [],
        "project_dir": project_dir,
    }
    task.update(extra)
    return task


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "dev-checkout"
    (root / "native_ext" / "src").mkdir(parents=True)
    (root / "native_ext" / "src" / "core.rs").write_text("x", encoding="utf-8")
    (root / "frontend-app" / "components").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The 0921 shape: create a file in a directory that does not exist yet
# ---------------------------------------------------------------------------


def test_to_be_created_file_in_missing_directory_passes(repo: Path):
    data = {"tasks": [
        _task(
            "1",
            str(repo),
            ["native_ext/docs/audit-0f0f0f0f.md"],  # docs/ does not exist
        ),
    ]}
    out = _generator().harden_for_execution(data)  # must NOT raise
    assert out["tasks"][0]["files_to_modify"] == [
        "native_ext/docs/audit-0f0f0f0f.md"
    ]


# ---------------------------------------------------------------------------
# project_dir / files_to_modify mismatch is fatal
# ---------------------------------------------------------------------------


def test_paths_that_do_not_resolve_under_own_project_dir_are_fatal(repo: Path):
    """The shape ``harden_for_execution`` catches on the real 0921 plan:
    root-relative paths declared against a sub-package ``project_dir``."""
    data = {"tasks": [
        _task(
            "3-4",
            str(repo / "native_ext"),          # sub-package
            ["scripts/run_tests.sh"],          # actually at the repo root
        ),
    ]}
    with pytest.raises(TasksGenerationError) as excinfo:
        _generator().harden_for_execution(data)

    payload = excinfo.value.error_payload
    assert payload["error_type"] == "tasks_not_runnable"
    assert payload["failures"][0]["task_ids"] == ["3-4"]
    assert any("step 3" in r for r in payload["failures"][0]["reasons"])


def test_missing_project_dir_is_fatal(repo: Path):
    data = {"tasks": [
        _task("1", str(repo / "does-not-exist"), ["native_ext/src/core.rs"]),
    ]}
    with pytest.raises(TasksGenerationError) as excinfo:
        _generator().harden_for_execution(data)
    reasons = excinfo.value.error_payload["failures"][0]["reasons"]
    assert any("does not exist" in r for r in reasons)


# ---------------------------------------------------------------------------
# The unresolved sentinel is a warning, never a hard failure
# ---------------------------------------------------------------------------


def test_unresolved_files_sentinel_is_a_warning(repo: Path):
    data = {"tasks": [
        _task("1", str(repo), ["__UNKNOWN_MODIFICATIONS__"]),
        _task("2", str(repo), ["native_ext/src/core.rs"]),
    ]}
    out = _generator().harden_for_execution(data)  # must NOT raise
    assert out["harden_warnings"] == {
        "unresolved_files_to_modify": ["1"]
    }


# ---------------------------------------------------------------------------
# auto_fix copy-back
# ---------------------------------------------------------------------------


def test_auto_fix_depends_on_is_copied_back_without_clobbering_extras(repo: Path):
    """``auto_fix`` appends a dependency the description names; the
    original dict must gain it while keeping the generator's own keys
    (``_workspace_validated`` et al. are dropped by ``model_dump``)."""
    data = {"tasks": [
        _task(
            "2",
            str(repo),
            ["native_ext/src/core.rs"],
            description="depends on task 1",
            depends_on=[],
            _workspace_validated=True,
        ),
        _task("1", str(repo), ["native_ext/src/core.rs"]),
    ]}
    out = _generator().harden_for_execution(data)

    first = out["tasks"][0]
    assert first["depends_on"] == ["1"], first["depends_on"]
    assert first["_workspace_validated"] is True, (
        "the surgical copy-back must not replace the caller's dict with "
        "a model_dump()"
    )


# ---------------------------------------------------------------------------
# Directory resolution
# ---------------------------------------------------------------------------


def test_single_declared_directory_is_inherited(repo: Path):
    """A task that did not repeat the plan's only workspace still gets
    validated instead of being skipped."""
    data = {"tasks": [
        _task("1", str(repo), ["native_ext/src/core.rs"]),
        {**_task("2", str(repo), ["native_ext/src/core.rs"]),
         "project_dir": None},
    ]}
    out = _generator().harden_for_execution(data)
    assert len(out["tasks"]) == 2


def test_task_without_any_resolvable_directory_is_reported(repo: Path):
    """With two distinct declared workspaces there is no single directory
    to inherit, so a task that declares none is reported rather than
    validated against a guess."""
    other = repo.parent / "somewhere-else"
    other.mkdir()
    (other / "x").mkdir()
    (other / "x" / "y.py").write_text("x", encoding="utf-8")
    data = {"tasks": [
        _task("1", str(repo), ["native_ext/src/core.rs"]),
        _task("2", str(other), ["x/y.py"]),
        {**_task("3", str(other), ["x/y.py"]), "project_dir": None},
    ]}
    out = _generator().harden_for_execution(data)
    assert out["harden_warnings"]["tasks_without_project_dir"] == ["3"]
