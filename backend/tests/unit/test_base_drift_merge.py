"""The merged path of the base-drift check.

A branch that fell behind its base is not broken — it is *stale*.  The
check repairs that staleness by merging the base in, and this file pins
what "repaired" has to mean:

* the merge happens **only** when the branch is behind and the working
  tree is clean,
* the base is **fetched first** and **re-read afterwards**, so the
  merge names the base the fetch actually brought in,
* the merge is a **real merge commit** (``--no-ff``) of the base
  **SHA** — never a fast-forward, never a moving ref,
* a base that caught up with ``HEAD`` while the fetch was running
  leaves **nothing to merge**: the branch is in sync after all.

Every repository here is built from scratch in a temporary directory.
The base is a **local bare repository** (``git clone --bare``) that the
work tree reaches through a remote pointing at a path on disk — the
fetch inside the check really runs, and it never touches the network.
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
    STATUS_MERGED,
    STATUS_NONE,
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


class _Repos:
    """The three repositories a drift test needs.

    ``source``   where the base is authored,
    ``bare``     ``origin.git`` — what the work tree fetches from,
    ``work``     the branch under test.
    """

    def __init__(self, source: Path, bare: Path, work: Path) -> None:
        self.source = source
        self.bare = bare
        self.work = work


def _repos(tmp_path: Path) -> _Repos:
    """Build a work tree whose ``origin`` is a local bare repository.

    The bare repository stands in for the network: the fetch inside the
    check really runs — against a path on disk.  The work tree carries a
    *local* git identity, because the merge inside the check creates a
    commit and the suite runs with ``$HOME`` pointed at a scratch
    directory where no global identity exists.
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
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(work),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "fetch", "origin", "main")
    _git(work, "reset", "--hard", "origin/main")
    _git(work, "checkout", "-b", "feat")
    return _Repos(source, bare, work)


def _advance_base(repos: _Repos, message: str = "base moved on") -> str:
    """Move the base on by one commit and publish it to the bare remote.

    Returns the SHA the base now points at.
    """
    _commit(repos.source, message)
    subprocess.run(
        ["git", "-C", str(repos.source), "push", str(repos.bare), "main:main"],
        check=True,
        text=True,
        capture_output=True,
    )
    return _git(repos.source, "rev-parse", "HEAD").stdout.strip()


def _fetch_work(repos: _Repos) -> None:
    """Let the work tree see the base's new tip.

    This is what a developer's own fetch does.  Without it the work
    tree's remote-tracking ref is stale and the drift is invisible —
    the check measures the base the work tree knows about.
    """
    _git(repos.work, "fetch", "origin", "main")


def _recording_runner(calls: list):
    """A git runner that records every call and runs it for real."""

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        return base_drift.run_git(project_dir, args, timeout_sec)

    return runner


def _commands(calls: list, name: str) -> list:
    """The recorded calls whose first argument is ``name``."""
    return [call for call in calls if call and call[0] == name]


def test_behind_branch_gets_merged(tmp_path):
    """A branch that is behind its base gets the base merged in.

    The merge is a real merge commit: the base's history becomes part of
    the branch's history, with both sides kept.  The report says so —
    the SHA of the merge commit, and the HEAD before and after it — so
    whoever reads the event log can see what the check did to the
    branch.
    """
    repos = _repos(tmp_path)
    _advance_base(repos)
    _fetch_work(repos)

    head_before = _git(repos.work, "rev-parse", "HEAD").stdout.strip()
    report = base_drift.check_and_merge_base(repos.work)

    assert report.status == STATUS_MERGED
    assert report.event == "base_drift_merged"
    assert report.blocked is False

    # The drift that was repaired.
    assert report.behind == 1
    assert report.ahead == 0
    assert len(report.base_sha) == 40
    assert report.base_ref == "origin/main"

    # The merge itself.
    assert report.head_before == head_before
    assert report.head_after != report.head_before
    assert len(report.merge_commit) == 40
    assert report.merge_commit == report.head_after

    # And the repository agrees.
    assert _git(repos.work, "rev-parse", "HEAD").stdout.strip() == report.merge_commit
    parents = _git(repos.work, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, parents  # a merge commit has two parents
    assert report.base_sha in parents


def test_merge_uses_no_ff_and_base_sha(tmp_path):
    """The merge names the base *commit*, and it is not a fast-forward.

    A ref that moves under a concurrent fetch would merge something
    other than the base this report is about — so the SHA the report
    carries is the SHA that gets merged.  ``--no-ff`` keeps the base's
    history visible in the branch's history instead of sliding the
    branch pointer forward.
    """
    repos = _repos(tmp_path)
    _advance_base(repos)
    _fetch_work(repos)

    calls: list = []
    report = base_drift.check_and_merge_base(
        repos.work, git_runner=_recording_runner(calls)
    )

    merges = _commands(calls, "merge")
    assert len(merges) == 1, calls
    merge = merges[0]

    assert "--no-ff" in merge, merge
    assert merge[-1] == report.base_sha, merge
    assert len(merge[-1]) == 40, merge
    assert merge[-1] != "origin/main", merge

    # The message names the base and the branch the merge landed on.
    message = merge[merge.index("-m") + 1]
    assert message == "Merge origin/main into feat", message


def test_fetch_runs_before_merge(tmp_path):
    """The base is fetched before it is merged.

    Merging a base that was never fetched merges whatever the
    remote-tracking ref happened to point at — which may be weeks old.
    The fetch comes first, and the merge names the base the fetch
    brought in.
    """
    repos = _repos(tmp_path)
    _advance_base(repos)
    _fetch_work(repos)

    calls: list = []
    report = base_drift.check_and_merge_base(
        repos.work, git_runner=_recording_runner(calls)
    )

    assert report.status == STATUS_MERGED

    fetches = _commands(calls, "fetch")
    merges = _commands(calls, "merge")
    assert fetches, calls
    assert merges, calls
    assert calls.index(fetches[0]) < calls.index(merges[0]), calls

    # The fetch names the remote and the branch of the *resolved* base.
    assert fetches[0] == ["fetch", "origin", "main"], calls


def test_fetch_moving_base_to_zero_is_none(tmp_path):
    """A base that catches up with ``HEAD`` while the fetch runs: no merge.

    The drift was measured against a base that has since moved — here
    upstream rewound ``main`` to the commit ``HEAD`` already has.  The
    fetch brings that rewrite in, and the branch turns out to be in sync
    after all.  Merging anyway would put a merge commit into a branch
    that had nothing to merge.
    """
    repos = _repos(tmp_path)
    _advance_base(repos)
    _fetch_work(repos)

    head = _git(repos.work, "rev-parse", "HEAD").stdout.strip()
    _git(repos.bare, "update-ref", "refs/heads/main", head)

    calls: list = []
    report = base_drift.check_and_merge_base(
        repos.work, git_runner=_recording_runner(calls)
    )

    assert report.status == STATUS_NONE
    assert report.event == "base_drift_none"
    assert report.blocked is False
    assert report.behind == 0

    # Nothing was merged, and the branch did not move.
    assert not _commands(calls, "merge"), calls
    assert _git(repos.work, "rev-parse", "HEAD").stdout.strip() == head


def test_merge_that_does_not_move_head_has_no_merge_commit(tmp_path):
    """A merge that reports success without moving HEAD has no commit.

    Git answers some merges with "Already up to date" and a zero exit
    code — nothing was merged, so there is no merge commit to name.
    Reporting the unchanged HEAD as a merge commit would put a SHA in
    the event log that points at the branch's own tip.
    """
    repos = _repos(tmp_path)
    _advance_base(repos)
    _fetch_work(repos)

    head_before = _git(repos.work, "rev-parse", "HEAD").stdout.strip()

    def runner(project_dir, args, timeout_sec):
        if args and args[0] == "merge":
            # Succeed without touching the repository.
            return subprocess.CompletedProcess(
                ["git", *[str(arg) for arg in args]], 0, "Already up to date.\n", ""
            )
        return base_drift.run_git(project_dir, args, timeout_sec)

    report = base_drift.check_and_merge_base(repos.work, git_runner=runner)

    assert report.status == STATUS_MERGED
    assert report.merge_commit == ""
    assert report.head_before == head_before
    assert report.head_after == head_before
    assert _git(repos.work, "rev-parse", "HEAD").stdout.strip() == head_before


def test_reparse_after_fetch_uses_resolved_ref(tmp_path):
    """A bare base name is normalised before the fetch *and* after it.

    The local branch ``main`` sits one commit behind ``origin/main``.
    Falling back to it after the fetch would report "in sync" and skip
    the merge — the very blindness this check was added to remove.  So
    the base is resolved again, from the same normalised ref, after the
    fetch has run.
    """
    repos = _repos(tmp_path)
    _advance_base(repos)
    _fetch_work(repos)

    calls: list = []
    report = base_drift.check_and_merge_base(
        repos.work,
        config=BaseDriftConfig(base_ref="main"),
        git_runner=_recording_runner(calls),
    )

    assert report.status == STATUS_MERGED
    assert report.base_ref == "origin/main"
    assert report.behind == 1

    # The base is the remote-tracking commit, not the local branch.
    assert report.base_sha == _git(repos.work, "rev-parse", "origin/main").stdout.strip()
    assert report.base_sha != _git(repos.work, "rev-parse", "main").stdout.strip()
    assert len(report.merge_commit) == 40

    # The merge names that same SHA.
    merges = _commands(calls, "merge")
    assert len(merges) == 1, calls
    assert merges[0][-1] == report.base_sha, calls

    # And the base is resolved again *after* the fetch, from the
    # normalised ref.
    fetch_index = calls.index(_commands(calls, "fetch")[0])
    reparses = [
        index
        for index, call in enumerate(calls)
        if call[:2] == ["rev-parse", "--verify"]
    ]
    assert reparses, calls
    assert max(reparses) > fetch_index, calls
    assert all("origin/main" in calls[index][-1] for index in reparses), calls
