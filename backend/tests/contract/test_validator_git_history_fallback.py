"""
Tests for the validator's git-history fallback in step-3
``files_to_modify`` existence check.

Background (audit 2026-09-04 plan):
Task 25 listed ``files_to_modify: ["tools/scripts/cleanup_optimizer_crons.py"]``
but the file had already been deleted by an earlier task (commit
``a6f5cf22``). The validator rejected the task at load time with
``tools/scripts/cleanup_optimizer_crons.py not found``, forcing
the user to manually edit ``tasks.json`` and replace the entry with
the ``__UNKNOWN_MODIFICATIONS__`` sentinel — even though the work
was already on disk.

The fallback here: when step-3 finds a missing path, query
``git log --diff-filter=D`` and accept the path if any reachable
commit deleted it (the absence is then explained, not treated as
a typo). This contract test pins both branches:
  1. Path deleted in git history → step-3 passes
  2. Path never existed (no git trace) → step-3 still fails
     (the original behaviour — typos / wrong paths still surface)
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from task import SubTask  # noqa: E402
from framework.task_output_validator import TaskOutputValidator  # noqa: E402


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Build a real git repo at ``tmp_path`` with one deleted file
    in history. The repo must be a working ``git log`` target so
    ``TaskOutputValidator._was_path_deleted_in_git`` can run a real
    ``git log`` subprocess against it.

    Two commits:
      * commit A: add ``orphan.py`` and ``kept.py``
      * commit B: delete ``orphan.py`` (--diff-filter=D match)

    After the fixture, ``orphan.py`` is absent from disk but present
    in the ``D`` history; ``kept.py`` is present.
    """
    repo = tmp_path
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)

    (repo / "orphan.py").write_text("# orphan\n")
    (repo / "kept.py").write_text("# kept\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init: add files"], cwd=repo, check=True, env=env)

    (repo / "orphan.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove orphan"], cwd=repo, check=True, env=env)

    return repo


def test_step_3_passes_for_path_deleted_in_git_history(tmp_git_repo: Path) -> None:
    """Regression: plan 2026-09-04 task 25.

    ``files_to_modify`` references ``orphan.py`` — the file is
    absent from disk but was deleted in a reachable commit. The
    validator must accept the path (the absence is intentional)
    rather than reject with ``not found``.
    """
    validator = TaskOutputValidator(project_dir=tmp_git_repo)
    snapshot = [
        SubTask(
            id="delete-orphan",
            title="Phase A10: delete orphan.py (already done in commit X)",
            description="",
            test_command="echo noop",
            files_to_modify=["orphan.py"],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "passed", (
        f"path deleted in git history must NOT trigger step-3 "
        f"failure; got report: {report}"
    )


def test_step_3_still_fails_for_never_existed_path(tmp_git_repo: Path) -> None:
    """A path that was never tracked AND never existed on disk must
    still fail step-3 — the git-log fallback is a per-case exception
    (the file was intentionally deleted), not a blanket bypass.

    Guards against a regression where the fallback silently accepts
    every missing path and turns the existence check into a no-op.
    """
    validator = TaskOutputValidator(project_dir=tmp_git_repo)
    snapshot = [
        SubTask(
            id="typo-path",
            title="Reference to a path that was never real",
            description="",
            test_command="echo noop",
            files_to_modify=["definitely_never_existed_xyz.py"],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "failed"
    assert 3 in report.failed_steps
    assert any(
        "definitely_never_existed_xyz.py" in r for r in report.reasons
    ), f"step-3 reason must identify the missing file; got {report.reasons}"


def test_step_3_accepts_real_existing_file(tmp_git_repo: Path) -> None:
    """Sanity check: the git-log fallback must NOT regress the
    happy path. ``kept.py`` is present on disk and never deleted —
    validator passes step-3 as before.
    """
    validator = TaskOutputValidator(project_dir=tmp_git_repo)
    snapshot = [
        SubTask(
            id="modify-kept",
            title="Modify the kept file",
            description="",
            test_command="echo noop",
            files_to_modify=["kept.py"],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "passed"


def test_step_3_fallback_is_safe_when_not_a_git_repo(tmp_path: Path) -> None:
    """If ``project_dir`` is NOT a git repo (e.g. tests in a temp
    scratch dir), the git-log fallback must return False silently
    and the original step-3 error must surface. Without this
    guard, a transient git failure could silently bypass the
    existence check.
    """
    validator = TaskOutputValidator(project_dir=tmp_path)
    snapshot = [
        SubTask(
            id="missing-no-git",
            title="Missing in non-git dir",
            description="",
            test_command="echo noop",
            files_to_modify=["definitely_missing.py"],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "failed"
    assert 3 in report.failed_steps