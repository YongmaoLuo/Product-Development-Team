"""The failure paths of the base-drift check.

A drift check is only useful if it survives the situations that are
*not* "the branch is in sync".  Those situations are exactly the ones
that are easy to get wrong, and every one of them has a way of
pretending to be fine:

* a **dirty working tree** looks like a merge that went through,
* a **failed ``git status``** looks like a clean working tree,
* a **failed fetch** looks like a base that simply has nothing new,
* a **failed merge** looks like a conflict — or worse, like a merge.

This file pins what the check does with each of them.  The rule
throughout: a git call that does not work is *reported*, never
guessed at and never allowed to pass for success.  The one case that
is explicitly **not** an error is the dirty working tree — it means
"there is work in progress here", and the round continues.

Every repository here is built from scratch in a temporary directory.
The base is a **local bare repository** (``git clone --bare``), so the
fetch inside the check really runs — without touching the network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
from base_drift import (  # noqa: E402
    STATUS_DIRTY_TREE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    BaseDriftConfig,
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
        text=True,
        capture_output=True,
    )


def _init_repo(repo: Path) -> Path:
    """Create an empty repository with one commit on ``main``."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    return repo


def _commit(repo: Path, message: str) -> None:
    """Add an empty commit on the current branch."""
    _git(repo, "commit", "--allow-empty", "-m", message)


def _repo_behind_by_one(tmp_path: Path) -> Path:
    """A work tree that is exactly one commit behind its base.

    The base lives in a **local bare repository**; the work tree is a
    clone of it.  The base then moves on by one commit and the work
    tree fetches it, so the drift is real and the fetch inside the
    check has a remote to talk to — a local path, never the network.
    """
    source = _init_repo(tmp_path / "source")
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
    _commit(source, "base moved on")
    subprocess.run(
        ["git", "-C", str(source), "push", str(bare), "main:main"],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "fetch", "origin", "main")
    return work


def _recording_runner(calls: list):
    """A git runner that records every call and runs it for real."""

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        return base_drift.run_git(project_dir, args, timeout_sec)

    return runner


def _commands(calls: list, name: str) -> list:
    """The recorded calls whose first argument is ``name``."""
    return [call for call in calls if call and call[0] == name]


def test_dirty_tree_skips_merge(tmp_path):
    """A dirty working tree is reported — and nothing is merged.

    Merging into a tree with uncommitted work folds that work into
    the merge: the result is neither the branch nor the base, and the
    operator's changes are now part of a merge commit they never saw.
    So the merge is skipped.  The drift itself is still reported —
    the branch *is* behind — and the round is not blocked, because a
    dirty tree is a normal state of a working repository, not a
    failure of the check.
    """
    work = _repo_behind_by_one(tmp_path)
    (work / "untracked.txt").write_text("x")
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    calls: list = []
    report = base_drift.check_and_merge_base(work, git_runner=_recording_runner(calls))

    assert report.status == STATUS_DIRTY_TREE
    assert report.event == "base_drift_dirty_tree"
    assert report.blocked is False
    assert report.behind >= 1
    assert len(report.base_sha) == 40
    assert report.base_ref == "origin/main"

    # Neither the fetch nor the merge may run: the dirty tree is
    # checked *before* the fetch.
    assert not _commands(calls, "fetch"), calls
    assert not _commands(calls, "merge"), calls

    # And no merge commit was produced.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before


def test_fetch_failure_records_stderr(tmp_path):
    """A fetch that fails is reported with git's own complaint.

    The complaint is stripped and cut to 200 characters — enough to
    name the failure, not enough to turn an event field into a log.
    The event still carries the base it was about: a reader has to be
    able to tell *which* base could not be fetched.
    """
    work = _repo_behind_by_one(tmp_path)
    calls: list = []

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        if args and args[0] == "fetch":
            return subprocess.CompletedProcess(
                ["git", *[str(arg) for arg in args]],
                1,
                "",
                "fatal: unable to access 'https://example.invalid/repo.git/': "
                "Could not resolve host\n" + "the remote said no. " * 40,
            )
        return base_drift.run_git(project_dir, args, timeout_sec)

    report = base_drift.check_and_merge_base(work, git_runner=runner)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt.startswith("fatal: unable to access")
    # Stripped and cut to 200 characters — the complaint is named, the
    # transcript is not carried along.
    assert len(report.stderr_excerpt) == 200
    assert report.base_ref == "origin/main"
    assert len(report.base_sha) == 40
    assert report.behind >= 1

    # The merge never runs after a failed fetch.
    assert not _commands(calls, "merge"), calls


def test_merge_failure_without_unmerged_is_failed(tmp_path):
    """A merge that fails without leaving conflicts is a failure.

    Git reports several kinds of failed merge.  Only one of them
    leaves unmerged paths behind; the others (a dirty tree, a missing
    object, a hook that refuses) leave nothing behind and must not be
    dressed up as a conflict.  A conflict stops the round — a failed
    merge that is *not* a conflict must not.
    """
    work = _repo_behind_by_one(tmp_path)
    calls: list = []

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        if args and args[0] == "merge":
            return subprocess.CompletedProcess(
                ["git", *[str(arg) for arg in args]],
                1,
                "",
                "error: Your local changes would be overwritten by merge\n",
            )
        return base_drift.run_git(project_dir, args, timeout_sec)

    report = base_drift.check_and_merge_base(work, git_runner=runner)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200
    assert report.base_ref == "origin/main"
    assert len(report.base_sha) == 40

    # The merge was attempted, and the "are there unmerged paths?"
    # question was asked — otherwise this report could have come from
    # any other failure.
    assert _commands(calls, "merge"), calls
    assert _commands(calls, "diff"), calls


def test_status_failure_is_failed(tmp_path):
    """A failing ``git status`` is a failure — never "a clean tree".

    This is the quietest of the traps: an unreadable status leaves
    the check without the information it needs, and treating that as
    "clean" would merge into a tree nobody looked at.  The failure
    must stop the merge, and it must say why.
    """
    work = _repo_behind_by_one(tmp_path)
    calls: list = []

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        if args and args[0] == "status":
            return subprocess.CompletedProcess(
                ["git", *[str(arg) for arg in args]],
                128,
                "",
                "fatal: not a git repository: '/nowhere'\n",
            )
        return base_drift.run_git(project_dir, args, timeout_sec)

    report = base_drift.check_and_merge_base(work, git_runner=runner)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200

    # The status command really was asked — and its failure stopped
    # the merge.
    assert _commands(calls, "status"), calls

    # The tree was never inspected, so nothing may be fetched or merged.
    assert not _commands(calls, "fetch"), calls
    assert not _commands(calls, "merge"), calls


def test_stderr_excerpt_truncated_to_200():
    """The excerpt is stripped and cut to 200 characters.

    A long git complaint must not become a long event field: the
    excerpt exists to name the failure, not to carry the whole
    transcript.
    """
    assert base_drift._excerpt("x" * 500) == "x" * 200
    assert len(base_drift._excerpt("y" * 500)) == 200
    assert base_drift._excerpt("  boom  ") == "boom"
    assert base_drift._excerpt("") == ""
    assert base_drift._excerpt(None) == ""
    assert base_drift._excerpt("abcdef", limit=3) == "abc"


def test_git_timeout_never_raises(tmp_path):
    """A git call that times out or cannot be run is reported, not raised.

    The check runs inside a verification round that has a job to do
    whether or not the drift is known.  A timeout is a fact about the
    network, not about the code under test — it must come back as a
    report.
    """
    work = _repo_behind_by_one(tmp_path)
    calls: list = []

    def timing_out(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        if args and args[0] == "status":
            raise subprocess.TimeoutExpired(cmd="git status", timeout=timeout_sec)
        return base_drift.run_git(project_dir, args, timeout_sec)

    report = base_drift.check_and_merge_base(work, git_runner=timing_out)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.stderr_excerpt
    # The timeout really happened on the status call — this report is
    # not just "something failed".
    assert _commands(calls, "status"), calls

    def not_executable(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        if args and args[0] == "fetch":
            raise OSError("git could not be executed")
        return base_drift.run_git(project_dir, args, timeout_sec)

    report = base_drift.check_and_merge_base(work, git_runner=not_executable)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.stderr_excerpt
    assert _commands(calls, "fetch"), calls


# ---------------------------------------------------------------------------
# The first git call: "not a work tree" is not the only thing it can mean
# ---------------------------------------------------------------------------
#
# ``git rev-parse --is-inside-work-tree`` exits non-zero in two very
# different situations: the directory is not in a repository (a fact
# about the directory) and the git call itself did not work (a fact
# about the machine).  Both used to be reported as ``skipped``, with
# git's complaint thrown away — so a checkout whose git was missing,
# timing out or broken looked exactly like a directory with nothing to
# check.  The round carried on either way, but the event log said
# "nothing to do" where the truth was "the check could not run".

def test_missing_git_binary_is_failed_not_skipped(tmp_path, monkeypatch):
    """A git that cannot be run is a failure — and it says so.

    The directory here *is* a repository: ``.git`` sits right there in
    it.  So the check is entitled to say what it knows — the git call
    failed, and git's own complaint is the reason.  Reporting this as
    "not a git work tree" would be a conclusion the check reached
    without asking anybody, and it would throw away the one line that
    explains the failure.
    """
    repo = _init_repo(tmp_path / "repo")

    # From here on git is not on PATH: every git call fails to start.
    monkeypatch.setenv("PATH", "")

    report = base_drift.check_and_merge_base(repo, config=BaseDriftConfig())

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200
    # git's own words — not a sentence the check made up.
    assert "git could not be executed" in report.stderr_excerpt

    # The event still names the base it was about.
    payload = report.to_dict()
    assert payload["base_ref"] == "origin/main"
    assert "base_sha" in payload


def test_first_git_call_timeout_is_failed_not_skipped(tmp_path):
    """A timeout on the very first git call is a failure, not a skip.

    The timeout is the loudest case of the same trap: git never
    answered, so nothing at all was learned about the directory — and
    "skipped" claims the opposite, that the check looked and found
    nothing to do.
    """
    repo = _init_repo(tmp_path / "repo")

    def timing_out(project_dir, args, timeout_sec):
        raise subprocess.TimeoutExpired(cmd="git", timeout=timeout_sec)

    report = base_drift.check_and_merge_base(repo, git_runner=timing_out)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200


def test_broken_git_directory_keeps_gits_complaint(tmp_path):
    """A repository whose git metadata is broken is a failure.

    The directory carries a ``.git`` entry, so it is *meant* to be a
    repository — and git's complaint is the answer to a question about
    that repository.  The complaint belongs in the event: it is the
    only thing that says what is actually wrong with the checkout.
    """
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").mkdir()

    report = base_drift.check_and_merge_base(broken)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt.startswith("fatal: not a git repository")
    assert len(report.stderr_excerpt) <= 200


def test_bare_repository_is_failed(tmp_path):
    """A bare repository has no work tree — and that is a failure.

    git answers ``false`` to ``--is-inside-work-tree`` there: it knows
    the directory, and it says the directory is not a work tree.  The
    check merges a base *into a work tree*; there is none here, so the
    answer is not "nothing to do" but "this cannot be done".
    """
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )

    report = base_drift.check_and_merge_base(bare)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200


def test_directory_outside_any_repository_is_still_skipped(tmp_path):
    """A directory that is in no repository at all is still skipped.

    ``skipped`` keeps its meaning — "there is nothing here to check",
    an internal condition, and the round continues.  The excerpt stays
    empty: there is no complaint to quote, because nothing failed.
    """
    plain = tmp_path / "plain"
    plain.mkdir()

    report = base_drift.check_and_merge_base(plain)

    assert report.status == STATUS_SKIPPED
    assert report.event == "base_drift_skipped"
    assert report.blocked is False
    assert report.stderr_excerpt == ""
