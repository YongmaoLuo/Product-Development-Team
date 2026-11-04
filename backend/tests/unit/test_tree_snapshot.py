"""Acceptance suite for :mod:`tree_snapshot` — the measuring primitive.

The write guard behind defect A may only *measure*, never predict. That
means it needs a reusable snapshot of a working tree: a per-file record
of what was there before, a diff that says what changed, and a restore
that puts the tree back.

This module is the acceptance suite for that primitive. It runs against
throw-away git repositories under ``tmp_path`` — the repository the
suite itself lives in is never touched.

The contract the suite pins:

* ``snapshot`` sees tracked files *and* untracked, non-ignored files;
* it does not see ignored files and does not see ``__pycache__``;
* ``diff`` sorts the tree into created / modified / deleted;
* ``restore`` puts a tracked file back, removes a file that was
  created, and resurrects a tracked file that was deleted;
* the paths are relative to the root and POSIX;
* a directory that is not a git working tree is refused with
  ``NotARepositoryError`` — not with a ``RuntimeError``, because the
  caller has to tell "nothing to measure here" apart from "the
  measuring code broke";
* a file whose ``(size, mtime_ns)`` did not change is not hashed again.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import List, Optional

import pytest

# ``backend/`` on sys.path so ``import tree_snapshot`` works no matter
# which entry point runs the suite.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import tree_snapshot  # noqa: E402
from tree_snapshot import (  # noqa: E402
    MutationReport,
    NotARepositoryError,
    diff,
    restore,
    snapshot,
)


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _init_repo(repo: Path) -> Path:
    """Create a throw-away git repository with a usable identity."""
    repo.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "init", "--initial-branch", "main"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        subprocess.run(
            ["git", "init"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str) -> None:
    """Stage everything and commit it."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def test_snapshot_lists_tracked_and_untracked(tmp_path: Path) -> None:
    """A tracked file and an untracked, non-ignored file are both seen."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _commit(repo, "seed")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    snap = snapshot(repo)

    assert "tracked.txt" in snap.entries
    assert "untracked.txt" in snap.entries
    assert snap.entries["tracked.txt"].sha256 == hashlib.sha256(
        b"tracked\n"
    ).hexdigest()
    assert snap.entries["untracked.txt"].size == len("untracked\n")


def test_diff_detects_created_modified_deleted(tmp_path: Path) -> None:
    """One new file, one rewritten file and one removed file are sorted out."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    (repo / "gone.txt").write_text("gone\n", encoding="utf-8")
    _commit(repo, "seed")

    before = snapshot(repo)

    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "kept.txt").write_text("kept but rewritten\n", encoding="utf-8")
    (repo / "gone.txt").unlink()

    after = snapshot(repo)
    report = diff(before, after)

    assert report.created == ["new.txt"]
    assert report.modified == ["kept.txt"]
    assert report.deleted == ["gone.txt"]


def test_restore_reverts_tracked_and_removes_new(tmp_path: Path) -> None:
    """A rewritten tracked file is put back; a created file is removed."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("original content\n", encoding="utf-8")
    _commit(repo, "seed")

    before = snapshot(repo)

    (repo / "tracked.txt").write_text("mutated content\n", encoding="utf-8")
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")

    after = snapshot(repo)
    report = diff(before, after)
    restored = restore(repo, before, report)

    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "original content\n"
    assert not (repo / "new.txt").exists()
    assert "tracked.txt" in restored
    assert "new.txt" in restored


def test_restore_resurrects_deleted_tracked(tmp_path: Path) -> None:
    """A deleted tracked file comes back byte for byte."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "keep.txt").write_bytes(b"keep me\n")
    _commit(repo, "seed")
    original = (repo / "keep.txt").read_bytes()

    before = snapshot(repo)
    (repo / "keep.txt").unlink()

    after = snapshot(repo)
    report = diff(before, after)
    assert report.deleted == ["keep.txt"]

    restored = restore(repo, before, report)

    assert (repo / "keep.txt").exists()
    assert (repo / "keep.txt").read_bytes() == original
    assert "keep.txt" in restored


def test_snapshot_excludes_gitignored_and_pycache(tmp_path: Path) -> None:
    """Ignored files and bytecode caches are not side effects."""
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("ignored.txt\nignored_dir/\n", encoding="utf-8")
    _commit(repo, "seed")

    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "inner.txt").write_text("inner\n", encoding="utf-8")
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-311.pyc").write_bytes(b"\x00\x01\x02")
    (repo / "kept.py").write_text("value = 1\n", encoding="utf-8")

    snap = snapshot(repo)

    assert "ignored.txt" not in snap.entries
    assert "ignored_dir/inner.txt" not in snap.entries
    assert not any("__pycache__" in path for path in snap.entries)
    assert "kept.py" in snap.entries


def test_snapshot_refuses_a_plain_directory(tmp_path: Path, monkeypatch) -> None:
    """A directory that is not a working tree is refused, loudly."""
    plain = tmp_path / "plain"
    plain.mkdir()
    # Stop git from walking up into a repository above ``tmp_path``.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    with pytest.raises(NotARepositoryError):
        snapshot(plain)

    # Not a RuntimeError on purpose: "nothing to measure here" must not
    # look like "the measuring code broke".
    assert not issubclass(NotARepositoryError, RuntimeError)


def test_empty_tree_snapshot_and_diff_are_empty(tmp_path: Path) -> None:
    """A repository without files snapshots to nothing and diffs to nothing."""
    repo = _init_repo(tmp_path / "repo")

    snap = snapshot(repo)
    assert snap.entries == {}

    report = diff(snap, snapshot(repo))
    assert report.created == []
    assert report.modified == []
    assert report.deleted == []


def test_snapshot_reuses_the_hash_of_an_unchanged_file(
    tmp_path: Path, monkeypatch
) -> None:
    """A file whose size and mtime did not change is not hashed again."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "seed")

    first = snapshot(repo)
    assert first.entries["a.txt"].sha256 is not None

    calls: List[str] = []
    real_hash = tree_snapshot._sha256_file

    def spy(path: Path) -> Optional[str]:
        calls.append(str(path))
        return real_hash(path)

    monkeypatch.setattr(tree_snapshot, "_sha256_file", spy)

    second = snapshot(repo)
    assert calls == []
    assert second.entries["a.txt"].sha256 == first.entries["a.txt"].sha256

    # A touched file is measured again — and its content still decides.
    path = repo / "a.txt"
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    third = snapshot(repo)
    assert len(calls) == 1
    assert third.entries["a.txt"].sha256 == first.entries["a.txt"].sha256

    # Changed content is measured again and shows a different hash.
    (repo / "a.txt").write_text("b\n", encoding="utf-8")
    fourth = snapshot(repo)
    assert len(calls) == 2
    assert fourth.entries["a.txt"].sha256 != first.entries["a.txt"].sha256


def test_snapshot_takes_the_previous_snapshot_for_incremental_hashing(
    tmp_path: Path, monkeypatch
) -> None:
    """An explicitly passed snapshot supplies the hash of an unchanged file."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "seed")

    first = snapshot(repo)
    # Forget what the module's own memo remembers, so the explicitly
    # passed snapshot is the only possible source of the hash.
    monkeypatch.setattr(tree_snapshot, "_MEMO", {})

    calls: List[str] = []
    real_hash = tree_snapshot._sha256_file

    def spy(path: Path) -> Optional[str]:
        calls.append(str(path))
        return real_hash(path)

    monkeypatch.setattr(tree_snapshot, "_sha256_file", spy)

    second = snapshot(repo, previous=first)

    assert calls == []
    assert second.entries["a.txt"].sha256 == first.entries["a.txt"].sha256


def test_paths_are_relative_posix_and_sorted(tmp_path: Path) -> None:
    """Every key is a relative POSIX path, and the keys come back sorted."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "top.txt").write_text("top\n", encoding="utf-8")
    _commit(repo, "seed")

    snap = snapshot(repo)

    assert list(snap.entries) == sorted(snap.entries)
    assert "pkg/mod.py" in snap.entries
    for path in snap.entries:
        assert not path.startswith("/")
        assert "\\" not in path
        assert ".." not in PurePosixPath(path).parts


def test_mutation_report_is_built_from_the_three_lists() -> None:
    """The report is the plain data the guard hands to the restore step."""
    report = MutationReport(created=["new.txt"], modified=[], deleted=[])
    assert report.created == ["new.txt"]
    assert report.modified == []
    assert report.deleted == []