"""Unit tests for :mod:`project_dir_guard` — the single ``project_dir`` validator.

Background
----------
The start-execution route used to ``mkdir(parents=True, exist_ok=True)`` the
requested ``project_dir``, which turned a *configuration error* (a typo, a
worktree that was never created, a path pointing at a regular file) into a
silently created empty directory that the executor then "works" in. The
repair dispatch path had no validation at all.

``project_dir_guard`` is the single reusable check both paths call. It
refuses anything that is not an existing directory and never creates one.

These tests pin the three contract points:

  1. a missing path raises, and the message names the offending path;
  2. a regular file raises with reason ``not a directory``;
  3. an existing directory is returned, resolved to an absolute path.
"""

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from project_dir_guard import ProjectDirInvalidError, validate_project_dir


def test_missing_path_raises_with_path_in_message(tmp_path):
    """A path that does not exist is refused, and the error names it."""
    missing = tmp_path / "definitely-missing-worktree"

    with pytest.raises(ProjectDirInvalidError) as excinfo:
        validate_project_dir(str(missing))

    exc = excinfo.value
    assert str(missing) in str(exc), (
        f"error message must name the offending path; got {str(exc)!r}"
    )
    assert exc.project_dir == str(missing)
    assert exc.reason == "does not exist"
    # The validator must not paper over the mistake by creating the path.
    assert not missing.exists(), (
        "validate_project_dir must not create the missing directory"
    )


def test_regular_file_is_rejected(tmp_path):
    """An existing path that is not a directory is refused."""
    regular_file = tmp_path / "not-a-directory.txt"
    regular_file.write_text("not a worktree", encoding="utf-8")

    with pytest.raises(ProjectDirInvalidError) as excinfo:
        validate_project_dir(str(regular_file))

    exc = excinfo.value
    assert exc.reason == "not a directory"
    assert str(regular_file) in str(exc)


def test_existing_directory_is_returned_resolved(tmp_path):
    """An existing directory is returned as a resolved absolute path."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    resolved = validate_project_dir(str(workdir))

    assert isinstance(resolved, Path)
    assert resolved == workdir.resolve()
    assert resolved.is_absolute()


def test_tilde_prefix_is_expanded(tmp_path, monkeypatch):
    """``~`` is expanded before the existence check."""
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "home-workdir"
    workdir.mkdir()

    resolved = validate_project_dir("~/home-workdir")

    assert resolved == workdir.resolve()


def test_relative_path_is_resolved_to_absolute(tmp_path, monkeypatch):
    """A relative path is resolved against the cwd, not returned as-is."""
    workdir = tmp_path / "relative-workdir"
    workdir.mkdir()
    monkeypatch.chdir(tmp_path)

    resolved = validate_project_dir("relative-workdir")

    assert resolved == workdir.resolve()
    assert resolved.is_absolute()
