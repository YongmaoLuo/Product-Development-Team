"""The attempt-level rollback patch must be evidence that can be applied back.

Background
----------
A sub agent that oversteps its declared file scope sometimes **commits** its
own work.  The attempt then ends with an empty diff (the change is already
in a commit), the failure is attributed to the wrong cause, and the next
attempt starts from a tree that still carries the overstep.  Rolling that
attempt back needs two things: the paths that were touched, and the
**content** of what was touched — captured *before* the rollback, because
after the rollback the difference no longer exists.

This file pins the evidence half of that: :func:`write_rollback_patch`
writes a patch relative to the attempt baseline, and that patch has to be a
patch ``git apply`` accepts — otherwise the evidence is a story rather than
a fact.

TDD contract — 7 test cases
---------------------------
1. ``test_patch_captures_modified_tracked_file`` — a tracked file the
   attempt edited: the patch holds the hunk.
2. ``test_patch_captures_new_untracked_file`` — a file the attempt
   created: the patch holds a ``new file mode`` entry.
3. ``test_patch_captures_committed_since_baseline`` — the attempt
   **committed** its overstep: the patch still holds the change, because
   the baseline is the commit the attempt started from and not HEAD.
4. ``test_patch_is_reappliable`` — after the tree is put back, ``git
   apply --check`` accepts the patch and applying it restores the content
   byte for byte.
5. ``test_unsafe_path_is_reported_without_raising`` — a path that escapes
   the working tree (or points into ``.git/``) is **reported**, not
   raised: one bad path must not cost the evidence for the good ones.
6. ``test_unusable_baseline_fails_every_path`` — a baseline that names no
   commit captures nothing, and says so, rather than claiming the files
   are new.
7. ``test_non_repository_tree_fails_every_path`` — a directory that is not
   a repository has no baseline to diff against, and says so.
8. ``test_git_internal_path_in_another_case_is_refused`` — a ``.git``
   segment is refused however it is spelled, and the patch carries no
   diff for it.  The evidence half and the restore half judge a path the
   same way; when they drift, the patch carries the repository's own
   configuration and the restore then deletes it.

Boundary conditions
-------------------
* Everything happens under ``tmp_path``: a throw-away git repository and a
  throw-away evidence directory.  The repository the suite runs in is
  never touched.
* The tests need a real ``git`` binary (they drive a real repository) —
  they skip when there is none rather than pretending to pass.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from attempt_rollback import RollbackResult, write_rollback_patch  # noqa: E402

#: ``git`` refuses to commit without an identity; the identity belongs to
#: the throw-away repository, never to the machine's global config.
_GIT_IDENTITY = (
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=PDT Test",
)


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary is not available")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo``."""
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> str:
    """Create a repository holding one commit; return that commit's sha.

    The sha is the **baseline**: the commit the attempt starts from.
    """
    _require_git()
    repo.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    (repo / "tracked.txt").write_text("line1\nline2\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    commit = _git(repo, "commit", "-m", "initial")
    assert commit.returncode == 0, commit.stderr
    head = _git(repo, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    return head.stdout.strip()


def test_patch_captures_modified_tracked_file(tmp_path: Path) -> None:
    """A tracked file the attempt edited shows up as a hunk."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(repo, baseline, ["tracked.txt"], patch)

    assert failed == []
    text = patch.read_text(encoding="utf-8")
    assert "diff --git a/tracked.txt b/tracked.txt" in text
    assert "@@" in text
    assert "+line3" in text


def test_patch_captures_new_untracked_file(tmp_path: Path) -> None:
    """A file the attempt created shows up as a new file."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    (repo / "added.txt").write_text("brand new\n", encoding="utf-8")

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(repo, baseline, ["added.txt"], patch)

    assert failed == []
    text = patch.read_text(encoding="utf-8")
    assert "new file mode" in text
    assert "added.txt" in text
    assert "+brand new" in text


def test_patch_captures_committed_since_baseline(tmp_path: Path) -> None:
    """A committed overstep is still captured: the baseline is not HEAD."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    commit = _git(repo, "commit", "-m", "sub agent commit")
    assert commit.returncode == 0, commit.stderr

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(repo, baseline, ["tracked.txt"], patch)

    assert failed == []
    text = patch.read_text(encoding="utf-8")
    assert "diff --git a/tracked.txt b/tracked.txt" in text
    assert "+line3" in text


def test_patch_is_reappliable(tmp_path: Path) -> None:
    """The patch applies back onto the tree it was taken from."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (repo / "added.txt").write_text("brand new\n", encoding="utf-8")

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(repo, baseline, ["tracked.txt", "added.txt"], patch)
    assert failed == []

    # Put the tree back to the baseline, then re-apply the evidence.
    restore = _git(repo, "checkout", baseline, "--", "tracked.txt")
    assert restore.returncode == 0, restore.stderr
    (repo / "added.txt").unlink()
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "line1\nline2\n"

    check = _git(repo, "apply", "--check", str(patch))
    assert check.returncode == 0, check.stderr
    applied = _git(repo, "apply", str(patch))
    assert applied.returncode == 0, applied.stderr

    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "line1\nline2\nline3\n"
    assert (repo / "added.txt").read_text(encoding="utf-8") == "brand new\n"


def test_unsafe_path_is_reported_without_raising(tmp_path: Path) -> None:
    """A path outside the working tree is reported, not raised."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(
        repo, baseline, ["../escape.txt", ".git/config"], patch
    )

    assert "../escape.txt" in failed
    assert ".git/config" in failed
    assert patch.exists()


def test_git_internal_path_in_another_case_is_refused(tmp_path: Path) -> None:
    """``.GIT/`` is refused by the evidence half too.

    The evidence half and the restore half judge a path the same way.
    When they drift, the patch ends up carrying the repository's own
    metadata — and the restore then deletes it.
    """
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(repo, baseline, [".GIT/config"], patch)

    assert failed == [".GIT/config"]
    assert "diff --git" not in patch.read_text(encoding="utf-8")


def test_unusable_baseline_fails_every_path(tmp_path: Path) -> None:
    """A baseline that is not a commit cannot produce a difference.

    Falling back to "everything is new" would write a patch that *claims*
    the file did not exist at the baseline — a false statement about the
    attempt, which is worse than no evidence at all.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(repo, "not-a-commit", ["tracked.txt"], patch)

    assert failed == ["tracked.txt"]
    assert "diff --git" not in patch.read_text(encoding="utf-8")


def test_non_repository_tree_fails_every_path(tmp_path: Path) -> None:
    """A directory that is not a repository has no baseline to diff against."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "tracked.txt").write_text("line1\n", encoding="utf-8")

    patch = tmp_path / "evidence" / "1.patch"
    failed = write_rollback_patch(plain, "a" * 40, ["tracked.txt"], patch)

    assert failed == ["tracked.txt"]
    assert "diff --git" not in patch.read_text(encoding="utf-8")
