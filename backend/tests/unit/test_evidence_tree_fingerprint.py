"""Unit tests for the working-tree fingerprint (import.json 缺陷 1).

Why these exist
---------------
``verify_citations`` reads the **live** working tree.  When several
verification processes share one worktree, a concurrent mutation (a
"变异负控" running in parallel) can rewrite a file between the moment a
verdict was produced and the moment its citations are checked — and the
check then reads a tree that belongs to nobody.

The fingerprint is the round's answer to "is the tree I am about to read
still the tree I read when the round started":  ``HEAD`` plus a hash of
``git status --porcelain``.  It is content-based, so a touched-but-
unchanged file does not move it, while an edited or newly untracked
file does.

The contract pinned here:

  * a clean tree still has a fingerprint (an empty porcelain output is
    a fact, not the absence of one) and two consecutive readings agree;
  * editing a tracked file moves it;
  * committing moves it (HEAD changed), even when the tree is clean
    again afterwards;
  * it is content-based — touching a file without changing it does not;
  * a directory that is not a repository — or is not there at all —
    yields ``None`` instead of raising.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_evidence import (  # noqa: E402
    tree_fingerprint,
    tree_mutated,
)


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``; fail the test if git itself failed."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one committed file and a clean worktree."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # Identity, signing and the global excludes file are configured
    # *locally* so the fixture does not depend on — or leak into — the
    # developer's global git configuration.
    _git(root, "config", "user.email", "fingerprint@example.com")
    _git(root, "config", "user.name", "Fingerprint Test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.excludesFile", str(tmp_path / "no-such-excludes"))
    (root / "tracked.txt").write_text("first line\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------------------
# tree_fingerprint
# ---------------------------------------------------------------------------


def test_clean_tree_fingerprint_is_stable(repo: Path):
    first = tree_fingerprint(repo)
    second = tree_fingerprint(repo)

    assert first is not None, "a clean worktree still has a fingerprint"
    assert first == second


def test_fingerprint_changes_when_tracked_file_modified(repo: Path):
    before = tree_fingerprint(repo)

    (repo / "tracked.txt").write_text("second line\n", encoding="utf-8")

    after = tree_fingerprint(repo)
    assert before is not None and after is not None
    assert before != after


def test_fingerprint_changes_after_commit(repo: Path):
    before = tree_fingerprint(repo)

    (repo / "tracked.txt").write_text("second line\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")

    after = tree_fingerprint(repo)
    # The worktree is clean again, so the only thing that moved is HEAD.
    assert _git(repo, "status", "--porcelain") == ""
    assert before is not None and after is not None
    assert before != after


def test_non_git_dir_returns_none(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert tree_fingerprint(plain) is None


def test_missing_dir_returns_none(tmp_path: Path):
    assert tree_fingerprint(tmp_path / "does-not-exist") is None


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_untracked_file_counts_as_a_change(repo: Path):
    before = tree_fingerprint(repo)

    (repo / "untracked.txt").write_text("scratch\n", encoding="utf-8")

    after = tree_fingerprint(repo)
    assert before is not None and after is not None
    assert before != after


def test_touching_a_file_without_changing_it_keeps_the_fingerprint(repo: Path):
    before = tree_fingerprint(repo)

    target = repo / "tracked.txt"
    stat = target.stat()
    os.utime(target, (stat.st_atime, stat.st_mtime + 60))

    after = tree_fingerprint(repo)
    assert before is not None and after is not None
    assert before == after, "the fingerprint must be content-based, not mtime-based"


# ---------------------------------------------------------------------------
# tree_mutated
# ---------------------------------------------------------------------------


def test_tree_mutated_is_false_without_a_baseline(repo: Path):
    assert tree_mutated(repo, None) is False


def test_tree_mutated_is_false_when_the_tree_cannot_be_read(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert tree_mutated(plain, "some-baseline") is False


def test_tree_mutated_detects_an_edit(repo: Path):
    baseline = tree_fingerprint(repo)
    assert baseline is not None
    assert tree_mutated(repo, baseline) is False

    (repo / "tracked.txt").write_text("second line\n", encoding="utf-8")

    assert tree_mutated(repo, baseline) is True
