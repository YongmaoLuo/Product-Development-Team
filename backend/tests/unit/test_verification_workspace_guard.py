"""Unit tests for :mod:`verification_workspace` — the verification worktree guard.

What this pins
--------------
A plan's verification runs in its own git worktree: on a branch derived
from the plan id, in a directory that belongs to no repository. Three
properties have to hold for that to be safe, and each of them is a way
to lose a run rather than a way to fail loudly:

  1. **The branch name is derived, never typed.** ``branch_name`` builds
     ``fix/<plan_id>-verification`` from a plan id that has already been
     through :func:`framework.ids.validate_plan_id`, so a plan id that
     could escape a path can never reach a git ref.
  2. **The workspace lives outside every repository.** A worktree inside
     the PDT checkout nests a checkout inside a checkout; a worktree
     inside the repository under test puts the tree being tested inside
     the thing testing it.
  3. **The comparison happens after resolution.** A symlink or a ``..``
     component that leads back into a repository is exactly the escape
     the guard exists to stop, so the guard compares ``Path.resolve()``
     results rather than the strings it was handed.

The cases below walk the plan-id truth table, the two repository
boundaries, the resolution rule, and the environment override.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config_paths import PROJECT_ROOT
from framework.ids import InvalidPlanIdError
from verification_workspace import (
    WORKSPACE_ROOT_ENV,
    WorkspaceRootError,
    branch_name,
    validate_workspace_path,
    workspace_root,
)


# ---------------------------------------------------------------------------
# The branch name is derived from the plan id and from nothing else.
# ---------------------------------------------------------------------------


def test_branch_name_from_plan_id():
    """A legal plan id yields the one branch name this module mints."""
    plan_id = "20261024-verify-plan-mode"

    assert branch_name(plan_id) == f"fix/{plan_id}-verification"


@pytest.mark.parametrize(
    "bad_plan_id",
    [
        "../x",          # parent traversal
        "..",            # traversal component on its own
        "/x",            # absolute path
        "a/b",           # path separator
        "",              # empty
        "with\x00nul",   # NUL byte
        "x" * 65,        # over the length ceiling
    ],
    ids=[
        "parent-traversal",
        "traversal-component",
        "absolute",
        "separator",
        "empty",
        "nul-byte",
        "too-long",
    ],
)
def test_invalid_plan_ids_rejected(bad_plan_id):
    """Every plan id that could escape a path is refused at the door."""
    with pytest.raises(InvalidPlanIdError):
        branch_name(bad_plan_id)


# ---------------------------------------------------------------------------
# The workspace root: default location, environment override, boundaries.
# ---------------------------------------------------------------------------


def test_workspace_root_default_location(monkeypatch):
    """Without an override the root is the per-user ``.pdt`` tree."""
    monkeypatch.delenv(WORKSPACE_ROOT_ENV, raising=False)

    assert workspace_root() == Path.home() / ".pdt" / "verification-workspaces"
    assert workspace_root("plan-alpha") == (
        Path.home() / ".pdt" / "verification-workspaces" / "plan-alpha"
    )


def test_env_override_wins(monkeypatch, tmp_path):
    """The environment variable replaces the default root."""
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(tmp_path))

    assert workspace_root() == tmp_path
    assert workspace_root("plan-alpha") == tmp_path / "plan-alpha"


def test_root_inside_repo_rejected(monkeypatch, tmp_path):
    """A root inside the PDT checkout is refused, not created."""
    inside = PROJECT_ROOT / "verification-workspaces"
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(inside))

    with pytest.raises(WorkspaceRootError):
        workspace_root()


def test_root_symlink_into_repo_rejected(monkeypatch, tmp_path):
    """A root that is a symlink back into the PDT checkout is refused."""
    link = tmp_path / "link-to-pdt"
    link.symlink_to(PROJECT_ROOT, target_is_directory=True)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(link))

    with pytest.raises(WorkspaceRootError):
        workspace_root()


# ---------------------------------------------------------------------------
# validate_workspace_path: the boundary check for a concrete path.
# ---------------------------------------------------------------------------


def test_validate_workspace_path_accepts_outside_path(tmp_path):
    """A workspace outside every repository is accepted, resolved."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace = tmp_path / "workspace"

    resolved = validate_workspace_path(workspace, repo_dir)

    assert resolved == workspace.resolve()
    assert resolved.is_absolute()


def test_validate_workspace_path_rejects_inside_baseline_repo(tmp_path):
    """A workspace inside the repository under test is refused."""
    repo_dir = tmp_path / "repo"
    (repo_dir / "nested").mkdir(parents=True)

    with pytest.raises(WorkspaceRootError):
        validate_workspace_path(repo_dir / "nested", repo_dir)


def test_validate_workspace_path_rejects_inside_pdt_repo(tmp_path):
    """A workspace inside the PDT checkout is refused."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with pytest.raises(WorkspaceRootError):
        validate_workspace_path(PROJECT_ROOT / "somewhere", repo_dir)


def test_validate_workspace_path_resolves_parent_traversal(tmp_path):
    """``..`` is resolved before the comparison, so it cannot slip past."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    outside = tmp_path / "workspace"
    outside.mkdir()

    with pytest.raises(WorkspaceRootError):
        validate_workspace_path(outside / ".." / "repo" / "nested", repo_dir)


def test_validate_workspace_path_rejects_symlink_into_repo(tmp_path):
    """A symlink that leads into the repository under test is refused."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    link = tmp_path / "link-to-repo"
    link.symlink_to(repo_dir, target_is_directory=True)

    with pytest.raises(WorkspaceRootError):
        validate_workspace_path(link / "nested", repo_dir)