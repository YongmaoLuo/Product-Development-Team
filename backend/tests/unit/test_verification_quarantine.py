"""The quarantine that undoes a verifier's mutation of the tree it verified.

A verification sub-agent is supposed to read the tree it verifies.  When
it writes to it anyway, two things have to happen, in this order:

  * the mutation is **saved** as a git patch under the plan directory, so
    it survives as evidence and can be replayed or looked at;
  * the working tree goes **back to the commit it was on**.

The order is the whole point, and these tests pin it.  A patch that is
produced but not written, or a tree that is restored without the patch
having been saved, is a tree whose mutation is gone for good — that is
the failure mode this module exists to make impossible.

Every repository here is built from scratch in a temporary directory, so
the tests never touch a real worktree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_quarantine  # noqa: E402
from verification_quarantine import (  # noqa: E402
    QuarantineResult,
    quarantine_and_restore,
)

#: The identity every commit in these tests carries.  It is passed with
#: ``-c`` instead of being configured globally: the suite runs with
#: ``$HOME`` pointed at a scratch directory, where ``user.email`` is not
#: set, and a commit without an identity fails outright.
GIT_IDENTITY = (
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo`` and fail loudly if it errors."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _status(repo: Path) -> str:
    """The porcelain status of ``repo`` — empty means a clean tree."""
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one committed file under ``backend/``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "backend").mkdir()
    (root / "backend" / "app.py").write_text("line1\nline2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """The plan directory the patch is supposed to land in."""
    return tmp_path / "plan"


def test_modified_tracked_file_is_saved_and_restored(repo: Path, plan_dir: Path):
    """A changed line is in the patch, and the file is back to its old self."""
    target = repo / "backend" / "app.py"
    target.write_text("line1\nCHANGED\n", encoding="utf-8")

    result = quarantine_and_restore(repo, plan_dir, "round-1")

    assert isinstance(result, QuarantineResult)
    assert result.error == ""
    assert result.failed == []
    assert result.clean is True

    # the mutation was saved …
    assert result.patch_path is not None
    assert result.patch_path.exists()
    patch = result.patch_path.read_text(encoding="utf-8", errors="replace")
    assert "+CHANGED" in patch
    assert "backend/app.py" in patch

    # … and the tree was put back
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"
    assert result.restored == ["backend/app.py"]
    assert _status(repo) == ""


def test_untracked_file_is_in_patch_and_removed(repo: Path, plan_dir: Path):
    """An untracked file the verifier created is saved, then deleted."""
    (repo / "new.py").write_text("brand new\n", encoding="utf-8")

    result = quarantine_and_restore(repo, plan_dir, "round-1")

    assert result.error == ""
    assert result.failed == []
    assert result.clean is True

    assert result.patch_path is not None
    patch = result.patch_path.read_text(encoding="utf-8", errors="replace")
    assert "new.py" in patch
    assert "+brand new" in patch

    assert not (repo / "new.py").exists()
    assert result.restored == ["new.py"]
    assert _status(repo) == ""


def test_deleted_file_is_restored(repo: Path, plan_dir: Path):
    """A file the verifier deleted comes back, and the deletion is on record."""
    (repo / "backend" / "gone.py").write_text("still here\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add gone.py")
    (repo / "backend" / "gone.py").unlink()

    result = quarantine_and_restore(repo, plan_dir, "round-1")

    assert result.error == ""
    assert result.failed == []
    assert result.clean is True

    assert result.patch_path is not None
    patch = result.patch_path.read_text(encoding="utf-8", errors="replace")
    assert "gone.py" in patch

    assert (repo / "backend" / "gone.py").read_text(encoding="utf-8") == "still here\n"
    assert result.restored == ["backend/gone.py"]
    assert _status(repo) == ""


def test_clean_tree_is_noop(repo: Path, plan_dir: Path):
    """A tree with nothing to quarantine produces no patch and touches nothing."""
    result = quarantine_and_restore(repo, plan_dir, "round-1")

    assert result.error == ""
    assert result.patch_path is None
    assert result.restored == []
    assert result.failed == []
    assert result.clean is True

    # nothing was written anywhere — not even the directory
    assert not (plan_dir / "vp_write_scope" / "quarantine").exists()
    assert _status(repo) == ""


def test_non_git_dir_is_refused_without_deleting(tmp_path: Path):
    """A directory that is not a repository is refused, and nothing is lost."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "keep.txt").write_text("do not touch\n", encoding="utf-8")
    plan_dir = tmp_path / "plan"

    result = quarantine_and_restore(plain, plan_dir, "round-1")

    assert result.error != ""
    assert result.patch_path is None
    assert result.restored == []
    assert result.failed == []
    assert result.clean is False

    # the refusal is not a licence to clean the directory
    assert (plain / "keep.txt").read_text(encoding="utf-8") == "do not touch\n"


def test_patch_can_be_replayed_onto_the_baseline(repo: Path, plan_dir: Path, tmp_path: Path):
    """The saved patch really carries the mutation: it applies to the baseline."""
    (repo / "backend" / "app.py").write_text("line1\nCHANGED\n", encoding="utf-8")
    (repo / "new.py").write_text("brand new\n", encoding="utf-8")

    result = quarantine_and_restore(repo, plan_dir, "round-1")

    assert result.patch_path is not None
    assert result.patch_path.parent == plan_dir / "vp_write_scope" / "quarantine"

    # replay onto a fresh checkout of the same commit
    replay = tmp_path / "replay"
    _git(repo, "clone", "-q", str(repo), str(replay))
    applied = subprocess.run(
        ["git", "-C", str(replay), "apply", "--check", str(result.patch_path)],
        capture_output=True,
        text=True,
    )
    assert applied.returncode == 0, applied.stderr
    subprocess.run(
        ["git", "-C", str(replay), "apply", str(result.patch_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (replay / "backend" / "app.py").read_text(encoding="utf-8") == "line1\nCHANGED\n"
    assert (replay / "new.py").read_text(encoding="utf-8") == "brand new\n"


def test_one_failing_path_does_not_stop_the_others(
    repo: Path, plan_dir: Path, monkeypatch
):
    """A path that refuses to be restored is recorded — the rest still comes back."""
    (repo / "backend" / "app.py").write_text("line1\nCHANGED\n", encoding="utf-8")
    (repo / "new.py").write_text("brand new\n", encoding="utf-8")

    real_git = verification_quarantine._git

    def flaky_git(args, cwd):
        # The untracked file refuses to be deleted …
        if list(args)[:2] == ["clean", "-f"]:
            return subprocess.CompletedProcess(["git"], 1, b"", b"boom")
        return real_git(args, cwd)

    monkeypatch.setattr(verification_quarantine, "_git", flaky_git)

    result = quarantine_and_restore(repo, plan_dir, "round-1")

    assert result.failed == ["new.py"]
    assert result.restored == ["backend/app.py"]
    assert result.error != ""
    assert result.clean is False

    # the file that could not be restored is still there — nothing was lost
    assert (repo / "new.py").read_text(encoding="utf-8") == "brand new\n"
    # … while the tracked file was restored anyway
    assert (repo / "backend" / "app.py").read_text(encoding="utf-8") == "line1\nline2\n"


def test_git_calls_are_bounded_and_never_raise(monkeypatch):
    """A git call that hangs is cut off and reported, not waited on."""
    assert verification_quarantine.GIT_TIMEOUT_SECONDS == 60

    def hanging(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="git", timeout=verification_quarantine.GIT_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(verification_quarantine.subprocess, "run", hanging)

    proc = verification_quarantine._git(["status"], Path("/tmp"))
    assert proc.returncode != 0
    assert "timed out" in proc.stderr.decode("utf-8")