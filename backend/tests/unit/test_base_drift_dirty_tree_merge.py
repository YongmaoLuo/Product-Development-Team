"""Merging into a dirty tree — and reading git's refusal.

Two questions live here, and they are two sides of the same coin.

**Does the merge happen?**  A working tree with uncommitted work is a
normal state of a repository, not a failure.  Git itself knows whether
that work is *in the way*: it refuses to merge only when the merge
would overwrite the uncommitted file.  So the check does not skip the
merge because the tree is dirty — it lets git decide, and a tree whose
uncommitted work does not overlap the merge is merged as usual.

**What did git say when it refused?**  A merge that git refuses to
start is not a conflict.  A conflict means two histories disagree and
somebody has to resolve it; a refusal means the **working tree**
disagrees with the merge — there is uncommitted work in the way, and
git will not touch it::

    error: Your local changes to the following files would be
    overwritten by merge:
    \ta.txt
    Please commit your changes or stash them before you merge.

Both end in a non-zero exit code, so the caller cannot tell them apart
from the exit code alone — it has to read git's own complaint.

The first half of this module pins down the *parsing* of that
complaint, which is a pure function over a string:

* ``None`` — the complaint is not a dirty-tree refusal (a conflict, or
  some other failure).  The caller goes on looking for a conflict.
* ``[]`` — git refused but named no path.  Still a refusal, just
  without names.
* a list of paths — the files git named, in the order it named them.

Nothing in that half runs git or touches a working tree: the complaint
is the whole input.

The second half builds **real repositories** in a temporary directory
and lets the real ``git`` have its say: uncommitted work that the merge
does not touch survives the merge byte for byte, and is still
uncommitted afterwards — and uncommitted work that the merge *would*
overwrite makes git refuse, which the check reports as a dirty tree
with the files git named.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402

#: Git's refusal when a **locally modified** file would be overwritten.
LOCAL_CHANGES_REFUSAL = (
    "error: Your local changes to the following files would be "
    "overwritten by merge:\n"
    "\ta.txt\n"
    "Please commit your changes or stash them before you merge.\n"
    "Aborting\n"
)

#: Git's refusal when an **untracked** file would be overwritten.
UNTRACKED_REFUSAL = (
    "error: The following untracked working tree files would be "
    "overwritten by merge:\n"
    "\tnewfile.txt\n"
    "Please move or remove them before you merge.\n"
)

#: Git's complaint about a merge that hit a conflict — a different
#: failure, which the caller has to tell apart from a refusal.
CONFLICT_COMPLAINT = (
    "Auto-merging a.txt\n"
    "CONFLICT (content): Merge conflict in a.txt\n"
)


def test_parse_refusal_paths_from_local_changes():
    """A refusal over locally modified files names those files."""
    assert base_drift._dirty_refusal_paths(LOCAL_CHANGES_REFUSAL) == ["a.txt"]


def test_parse_refusal_paths_from_untracked_files():
    """A refusal over untracked files names those files."""
    assert base_drift._dirty_refusal_paths(UNTRACKED_REFUSAL) == ["newfile.txt"]


def test_parse_refusal_returns_none_for_conflict():
    """A conflict is not a refusal — the caller must be able to tell."""
    assert base_drift._dirty_refusal_paths(CONFLICT_COMPLAINT) is None
    assert (
        base_drift._dirty_refusal_paths(
            "CONFLICT (content): Merge conflict in a.txt"
        )
        is None
    )


def test_parse_refusal_returns_empty_list_without_paths():
    """A refusal that names no path is still a refusal."""
    refusal_without_paths = (
        "error: Your local changes to the following files would be "
        "overwritten by merge:\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )
    assert base_drift._dirty_refusal_paths(refusal_without_paths) == []


# ---------------------------------------------------------------------------
# Merging into a dirty tree: git decides whether the work is in the way
# ---------------------------------------------------------------------------
#
# The identity every commit in this file carries.  It is passed with
# ``-c`` instead of being configured globally: the suite runs with
# ``$HOME`` pointed at a scratch directory, where ``user.email`` is not
# set, and a commit without an identity fails outright.  The work tree
# carries a *local* identity as well, because the merge inside the
# check creates a commit and that commit is made by a plain ``git``
# call — with no ``-c`` to fall back on.

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
        text=True,
        capture_output=True,
    )


def _repo_behind_by_one_with_files(tmp_path: Path) -> Path:
    """A work tree one commit behind a base that moved on in ``a.txt``.

    Two files are tracked in the base: ``a.txt``, which the base moves
    on from, and ``other.txt``, which the base does not touch.  The
    base lives in a **local bare repository**, so the fetch inside the
    check really runs — against a path on disk, never the network.
    """
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(source),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(source, "config", "user.email", "t@example.com")
    _git(source, "config", "user.name", "t")
    _git(source, "config", "commit.gpgsign", "false")
    (source / "a.txt").write_bytes(b"a\nbase\n")
    (source / "other.txt").write_bytes(b"x\nbase\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "initial")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")

    # The base moves on — and only ``a.txt`` changes with it.
    (source / "a.txt").write_bytes(b"a\nbase moved\n")
    _git(source, "commit", "-am", "base moved on")
    subprocess.run(
        ["git", "-C", str(source), "push", str(bare), "main:main"],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "fetch", "origin", "main")
    return work


def test_dirty_tree_outside_merge_scope_still_merges(tmp_path):
    """Uncommitted work the merge does not touch does not stop the merge.

    The uncommitted edit sits in ``other.txt``; the base moves on in
    ``a.txt``.  Git has no reason to refuse, so the base is merged in —
    and the uncommitted work is left exactly as it was: the same bytes,
    still uncommitted, and not part of the merge commit.
    """
    work = _repo_behind_by_one_with_files(tmp_path)
    (work / "other.txt").write_bytes(b"x\nlocal\n")

    report = base_drift.check_and_merge_base(work)

    assert report.status == base_drift.STATUS_MERGED
    assert report.merge_commit != ""
    assert report.head_before and report.head_after
    assert report.head_after == report.merge_commit
    assert report.head_before != report.head_after

    # The base really was merged in.
    assert (work / "a.txt").read_bytes() == b"a\nbase moved\n"

    # The uncommitted work survived byte for byte ...
    assert (work / "other.txt").read_bytes() == b"x\nlocal\n"
    # ... and is still uncommitted: it did not sneak into the merge.
    assert _git(work, "status", "--porcelain").stdout == " M other.txt\n"


def test_clean_tree_still_merges(tmp_path):
    """A clean tree still merges — the old behaviour is not lost."""
    work = _repo_behind_by_one_with_files(tmp_path)

    report = base_drift.check_and_merge_base(work)

    assert report.status == base_drift.STATUS_MERGED
    assert report.event == "base_drift_merged"
    assert report.merge_commit != ""
    assert (work / "a.txt").read_bytes() == b"a\nbase moved\n"
    assert _git(work, "status", "--porcelain").stdout == ""


# ---------------------------------------------------------------------------
# When the uncommitted work *is* in the way: git refuses, and says so
# ---------------------------------------------------------------------------
#
# The half above merges into a dirty tree.  This half is about the case
# git refuses outright: the base moves on in ``a.txt`` and the working
# tree has its own uncommitted ``a.txt``, so the merge would overwrite
# work that was never committed.  Git does not start the merge — it
# says why, in its own words, naming the file.


def test_dirty_tree_overwritten_by_merge_reports_refusal_paths(tmp_path):
    """A refusal is a dirty tree — and git names the file in the way.

    The base moved ``a.txt`` on; the working tree has an uncommitted
    ``a.txt`` of its own.  Git refuses to overwrite it, so the merge
    does not happen.  The report says what git said: a dirty tree, the
    file that is in the way, and git's own complaint.

    A refusal is **not** a conflict: it leaves no unmerged path behind,
    and it does not stop the round.
    """
    work = _repo_behind_by_one_with_files(tmp_path)
    # The working tree has its own, uncommitted ``a.txt``.
    (work / "a.txt").write_bytes(b"one\nlocal\n")
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    report = base_drift.check_and_merge_base(work)

    # A dirty tree — reported, not a conflict and not a failure.
    assert report.status == base_drift.STATUS_DIRTY_TREE
    assert report.event == "base_drift_dirty_tree"
    assert report.blocked is False

    # The file git named is in the report.
    assert "a.txt" in report.detail
    # And so is git's own complaint.
    assert "would be overwritten by merge" in report.stderr_excerpt

    # Nothing was merged: HEAD did not move ...
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before
    # ... the merge never started, so there is nothing to abort ...
    assert (work / ".git/MERGE_HEAD").exists() is False
    # ... and the uncommitted work is still there, byte for byte.
    assert (work / "a.txt").read_bytes() == b"one\nlocal\n"
    assert _git(work, "status", "--porcelain").stdout == " M a.txt\n"


# ---------------------------------------------------------------------------
# The refusal git words differently: untracked files in the way
# ---------------------------------------------------------------------------


def _repo_behind_adding_untracked_collision(tmp_path: Path) -> Path:
    """A work tree one commit behind a base that *adds* ``newfile.txt``.

    The work tree has its own untracked ``newfile.txt``.  Git refuses
    with the **untracked** variant of the complaint, which is a
    different sentence from the locally-modified one — and the check
    has to recognise both.
    """
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(source),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(source, "config", "user.email", "t@example.com")
    _git(source, "config", "user.name", "t")
    _git(source, "config", "commit.gpgsign", "false")
    (source / "a.txt").write_bytes(b"a\nbase\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "initial")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")

    # The base *adds* a file; the work tree has an untracked file of the
    # same name with different contents.
    (source / "newfile.txt").write_bytes(b"from the base\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "base adds newfile.txt")
    subprocess.run(
        ["git", "-C", str(source), "push", str(bare), "main:main"],
        check=True,
        text=True,
        capture_output=True,
    )
    (work / "newfile.txt").write_bytes(b"local untracked\n")
    _git(work, "fetch", "origin", "main")
    return work


def test_untracked_file_in_the_way_reports_dirty_tree(tmp_path):
    """The untracked variant of the refusal is a dirty tree too.

    Git words it differently ("The following untracked working tree
    files would be overwritten by merge"), so recognising only the
    local-changes sentence would let this refusal fall through to the
    conflict lookup — where it would find nothing and be dressed up as
    a plain failure.  It is neither a conflict nor a failure: the round
    continues, and the file git named is in the report.
    """
    work = _repo_behind_adding_untracked_collision(tmp_path)
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    report = base_drift.check_and_merge_base(work)

    assert report.status == base_drift.STATUS_DIRTY_TREE
    assert report.event == "base_drift_dirty_tree"
    assert report.blocked is False
    assert "newfile.txt" in report.detail
    assert "untracked working tree files would be overwritten" in report.stderr_excerpt

    # Nothing was merged, and the untracked file is untouched.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (work / ".git/MERGE_HEAD").exists() is False
    assert (work / "newfile.txt").read_bytes() == b"local untracked\n"
    assert _git(work, "status", "--porcelain").stdout == "?? newfile.txt\n"


def test_refusal_names_every_path_and_caps_the_detail_line():
    """A refusal names all its paths; the detail line shows ten and "..."."""
    complaint = (
        "error: Your local changes to the following files would be "
        "overwritten by merge:\n"
        + "".join(f"\tfile{i}.txt\n" for i in range(12))
        + "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )
    paths = base_drift._dirty_refusal_paths(complaint)

    assert paths == [f"file{i}.txt" for i in range(12)]

    detail = base_drift._path_list(paths)
    assert "file9.txt" in detail
    assert "file10.txt" not in detail
    assert detail.endswith(", ...")


def test_refusal_paths_keep_subdirectory_paths():
    """A path git names in a subdirectory is kept whole, not split."""
    complaint = (
        "error: Your local changes to the following files would be "
        "overwritten by merge:\n"
        "\tbackend/service_manager.py\n"
        "\tbackend/tests/unit/test_service_manager.py\n"
        "Please commit your changes or stash them before you merge.\n"
    )
    assert base_drift._dirty_refusal_paths(complaint) == [
        "backend/service_manager.py",
        "backend/tests/unit/test_service_manager.py",
    ]
