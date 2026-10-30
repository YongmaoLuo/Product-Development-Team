"""The base-drift check against a **real** ``git worktree``.

The unit tests around :mod:`base_drift` build their repositories by hand
(``git init`` + ``git clone --bare`` + ``git checkout -b``) and watch the
git calls through a recording runner.  That covers the decision tree, but
it never answers the question a developer actually has: *does the check
do the right thing in the layout we ship?* — a branch checked out in a
second working directory of the same repository, whose base is a
remote-tracking ref shared with the main checkout.

This file answers that question on a real worktree:

* **Scenario A — the branch is behind.**  ``main`` moves on, the branch
  does not.  The check must fetch the base, merge it in as a **real merge
  commit**, and leave the base's new files in the work tree's working
  directory — while the main checkout stays where it was.
* **Scenario B — the branch is up to date.**  The check must change
  *nothing*: the same ``HEAD``, the same commit count, an empty
  ``git status --porcelain``.  A check that "repairs" a branch that was
  never stale would put a merge commit on a branch that had nothing to
  merge — the cheapest way to make a verification round lie about what it
  measured.

Every scenario builds its own repository in its own temporary directory;
nothing is shared between them.  The remote is a **local bare
repository** (``git clone --bare``) reached through a path on disk, so
the fetch inside the check really runs and never touches the network.

The identity every commit carries is passed with ``-c`` and configured
**locally** in the repository under test, never globally: the suite runs
with ``$HOME`` pointed at a scratch directory where no global git
identity exists — and the merge commit the check creates needs one.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
from base_drift import STATUS_MERGED, STATUS_NONE  # noqa: E402

pytestmark = pytest.mark.integration


GIT_IDENTITY = (
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
)
"""The identity every commit in these tests carries.

It is passed with ``-c`` instead of being configured globally: the suite
runs with ``$HOME`` pointed at a scratch directory, where ``user.email``
is not set, and a commit without an identity fails outright.
"""

BASE_FILE = "base.txt"
"""The file the base branch starts with."""

UPSTREAM_FILE = "upstream.txt"
"""The file ``main`` gains while the branch under test stands still."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo`` and fail loudly if it errors."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        text=True,
        capture_output=True,
    )


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    """Write a file, commit it, and return the new commit's SHA."""
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _porcelain(repo: Path) -> str:
    """The working tree's ``git status --porcelain`` listing."""
    return _git(repo, "status", "--porcelain").stdout


def _behind(repo: Path) -> int:
    """How many commits ``HEAD`` is behind ``origin/main``.

    ``git rev-list --left-right --count HEAD...origin/main`` prints the
    left side first — the commits only ``HEAD`` has (*ahead*) — and the
    right side second: the commits only the base has (*behind*).
    """
    counts = _git(
        repo, "rev-list", "--left-right", "--count", "HEAD...origin/main"
    ).stdout.split()
    assert len(counts) == 2, counts
    return int(counts[1])


def _evidence(report: object) -> str:
    """One line carrying everything the assertions below are about.

    A failing assertion prints this line, so the failure says what
    happened (``status``), what the base was (``base_sha``), how far the
    branch had drifted (``behind``) and what came of it
    (``merge_commit``) without the reader having to go looking.
    """
    return (
        f"status={report.status} base_sha={report.base_sha} "
        f"behind={report.behind} ahead={report.ahead} "
        f"head_before={report.head_before} head_after={report.head_after} "
        f"merge_commit={report.merge_commit} detail={report.detail}"
    )


@dataclass
class _World:
    """The three directories one scenario needs, plus what they hold.

    ``repo``     the main checkout — ``main`` is checked out here,
    ``bare``     ``origin.git`` — what the work tree fetches from,
    ``work``     the work tree — the branch under test.
    """

    repo: Path
    bare: Path
    work: Path
    base_sha: str
    upstream_sha: str

    def close(self) -> None:
        """Take the work tree down again.

        ``git worktree remove --force`` is what the scenario owes the
        machine it runs on: a work tree left behind keeps its branch
        checked out, and the next scenario would meet a repository that
        is not the one it built.
        """
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "remove",
                "--force",
                str(self.work),
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            check=False,
            text=True,
            capture_output=True,
        )


def _build_world(root: Path, *, advance_base: bool) -> _World:
    """Build a repository with a real work tree on a branch of its own.

    ``advance_base`` decides whether ``main`` moves on after the work
    tree was created — that is the only difference between the two
    scenarios, and everything else about them is deliberately identical.
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo),
        check=True,
        text=True,
        capture_output=True,
    )
    # The identity is configured **locally**, in the repository under
    # test: the merge the check performs creates a commit, and the suite
    # runs with $HOME pointed at a scratch directory where no global
    # identity exists.  A work tree shares the repository's common
    # config, so this covers the work tree too.
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")

    base_sha = _commit(repo, BASE_FILE, "base\n", "base commit")

    # The remote stands in for the network: a bare repository on disk.
    bare = root / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main")

    # The branch under test: a real work tree, a real branch of its own.
    work = root / "wt"
    _git(repo, "worktree", "add", "-b", "feat", str(work))

    upstream_sha = ""
    if advance_base:
        # ``main`` moves on and publishes the move.
        upstream_sha = _commit(repo, UPSTREAM_FILE, "upstream\n", "upstream moved on")
        _git(repo, "push", "origin", "main")
        # And the work tree learns about it — this is the fetch a
        # developer working in the work tree would run.  Without it the
        # remote-tracking ref is stale and the drift is invisible.
        _git(work, "fetch", "origin", "main")

    return _World(
        repo=repo,
        bare=bare,
        work=work,
        base_sha=base_sha,
        upstream_sha=upstream_sha,
    )


@pytest.fixture
def repo_factory(tmp_path: Path) -> Callable[..., _World]:
    """Build isolated repositories, and take their work trees down again."""
    worlds: List[_World] = []

    def build(*, advance_base: bool) -> _World:
        root = tmp_path / ("behind" if advance_base else "uptodate")
        root.mkdir()
        world = _build_world(root, advance_base=advance_base)
        worlds.append(world)
        return world

    yield build

    for world in reversed(worlds):
        world.close()


def test_branch_behind_main_gets_merged_on_real_worktree(repo_factory, monkeypatch):
    """A branch that fell behind ``main`` gets the base merged in.

    The merge happens in a real work tree: the base is fetched, merged
    as a **real merge commit** (``--no-ff``) of the base's SHA, and the
    base's new file ends up in the work tree's working directory.  The
    main checkout is not part of that transaction and must not move.

    The report says what happened, so whoever reads the event log can
    see what the check did to the branch: the base it compared against,
    how far the branch had drifted, and the merge commit that resulted.
    """
    # The check reads its configuration from the environment; a stray
    # value would change what it does, so the environment is cleared
    # rather than assumed.
    for name in (
        base_drift.ENV_ENABLED,
        base_drift.ENV_BASE,
        base_drift.ENV_MODE,
    ):
        monkeypatch.delenv(name, raising=False)

    world = repo_factory(advance_base=True)
    work = world.work

    # The precondition, stated before anything runs: the branch really
    # is behind the base it is about to be compared against, and its
    # working tree really is clean.
    assert _behind(work) == 1, "the branch must be behind before the check runs"
    assert _porcelain(work) == ""

    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    report = base_drift.check_and_merge_base(work)
    evidence = _evidence(report)
    print("scenario A:", evidence)

    assert report.status == STATUS_MERGED, evidence
    assert report.event == "base_drift_merged", evidence
    assert report.blocked is False, evidence

    # The drift that was repaired.
    assert report.behind == 1, evidence
    assert report.ahead == 0, evidence
    assert report.base_sha == world.upstream_sha, evidence
    assert report.base_ref == "origin/main", evidence

    # The merge itself: a real merge commit, not a fast-forward.
    assert report.head_before == head_before, evidence
    assert report.head_after == report.merge_commit, evidence
    assert report.merge_commit != report.head_before, evidence
    assert len(report.merge_commit) == 40, evidence

    # And the repository agrees.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == report.merge_commit, evidence
    parents = _git(work, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, (evidence, parents)  # a merge commit has two parents
    assert report.base_sha in parents, (evidence, parents)

    # The merge commit names what it merged.
    subject = _git(work, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "Merge origin/main into feat", (evidence, subject)

    # The base's new file is in the work tree's working directory.
    assert (work / UPSTREAM_FILE).exists(), evidence
    assert (work / UPSTREAM_FILE).read_text(encoding="utf-8") == "upstream\n", evidence
    assert _porcelain(work) == "", evidence

    # And the main checkout was not part of the transaction: it still
    # sits on ``main`` at the commit ``main`` was on.
    assert _git(world.repo, "rev-parse", "HEAD").stdout.strip() == world.upstream_sha, evidence
    assert _git(world.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main", evidence
    assert _porcelain(world.repo) == "", evidence


def test_up_to_date_worktree_has_no_side_effects(repo_factory, monkeypatch):
    """A branch that is in sync with its base is left exactly as it was.

    The check exists to repair staleness.  When there is no staleness,
    "repairing" it would be a change nobody asked for: a merge commit on
    a branch that had nothing to merge, a working tree that no longer
    matches ``HEAD``, and a report that claims work the round did not
    do.

    So the whole observable state of the work tree — its ``HEAD``, its
    history and its working directory — is compared before and after.
    """
    for name in (
        base_drift.ENV_ENABLED,
        base_drift.ENV_BASE,
        base_drift.ENV_MODE,
    ):
        monkeypatch.delenv(name, raising=False)

    world = repo_factory(advance_base=False)
    work = world.work

    # The precondition, stated before anything runs: the branch is in
    # sync with its base.
    assert _behind(work) == 0, "the branch must be in sync before the check runs"

    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    commits_before = _git(work, "rev-list", "--count", "HEAD").stdout.strip()
    log_before = _git(work, "log", "--format=%H").stdout
    status_before = _porcelain(work)
    assert status_before == ""

    report = base_drift.check_and_merge_base(work)
    evidence = _evidence(report)
    print("scenario B:", evidence)

    assert report.status == STATUS_NONE, evidence
    assert report.event == "base_drift_none", evidence
    assert report.blocked is False, evidence
    assert report.behind == 0, evidence
    assert report.ahead == 0, evidence
    assert report.base_sha == world.base_sha, evidence

    # No side effect: the branch did not move.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before, evidence
    assert _git(work, "rev-list", "--count", "HEAD").stdout.strip() == commits_before, evidence
    assert _git(work, "log", "--format=%H").stdout == log_before, evidence

    # No side effect: nothing was left in the working tree.
    assert _porcelain(work) == status_before == "", evidence

    # And no merge was invented.
    assert report.merge_commit == "", evidence
    assert report.head_before == "", evidence
    assert report.head_after == "", evidence
