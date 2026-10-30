"""The no-drift path of the base-drift check.

A verification round that stands on a branch 53 commits behind ``main``
verifies a codebase upstream has already moved past: the round fails on
symptoms (a test that no longer matches the current base) instead of on
the cause.  The base-drift check answers the question that has to be
answered *before* the round starts — "is this branch still in sync with
its base?" — and this file pins the answer for the case that needs no
action:

* the check is **switched off** → no git call at all,
* the directory is **not a git work tree** → skipped, not failed,
* the branch is **in sync** with its base → ``base_drift_none``,
* the branch is **ahead** of its base → still in sync (nothing to pull),
* the base ref **does not resolve** → failed, with git's own complaint,
* a bare base name (``main``) means ``origin/main``, never the local
  branch of the same name,
* an in-sync branch **does not fetch** — the round's other behaviour
  stays exactly as it was.

Every repository here is built from scratch in a temporary directory:
``git init -b main`` plus commits that carry an explicit identity, so
the tests never depend on a global git configuration, and the remote
reference is created with ``git update-ref`` instead of a network
fetch.
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
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_NONE,
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


def _set_origin_main(repo: Path, target: str = "HEAD") -> None:
    """Create the ``origin/main`` remote-tracking ref locally.

    A temporary repository has no remote, and the tests must not reach
    the network: ``update-ref`` writes the same ref a fetch would.
    """
    _git(repo, "update-ref", "refs/remotes/origin/main", target)


def _recording_runner(calls: list):
    """A git runner that records every call and runs it for real."""

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        return base_drift.run_git(project_dir, args, timeout_sec)

    return runner


def test_disabled_skips_git(tmp_path):
    """A switched-off check must not touch git at all.

    The runner here raises on the first call: "disabled" has to mean
    *no git whatsoever*, not "git that happens not to change anything".
    """
    repo = _init_repo(tmp_path / "repo")
    calls: list = []

    def exploding_runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        raise AssertionError(f"git must not run while the check is disabled: {args}")

    report = base_drift.check_and_merge_base(
        repo,
        config=BaseDriftConfig(enabled=False, base_ref="origin/main"),
        git_runner=exploding_runner,
    )

    assert report.status == STATUS_DISABLED
    assert report.event == "base_drift_disabled"
    assert report.blocked is False
    assert calls == []


def test_non_git_dir_is_skipped(tmp_path):
    """A directory outside a git work tree is skipped, not failed.

    ``skipped`` is an internal condition — the round continues.  The
    check must not raise either: the caller is a verification round that
    has a job to do regardless.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    report = base_drift.check_and_merge_base(plain)

    assert report.status == STATUS_SKIPPED
    assert report.event == "base_drift_skipped"
    assert report.blocked is False


def test_behind_zero_is_none(tmp_path, monkeypatch):
    """A branch that sits on its base reports "in sync".

    ``base_drift_none`` carries the base's name and SHA — the event log
    has to say *which* base was compared against, not just that
    something matched.
    """
    monkeypatch.delenv("PDT_BASE_DRIFT_ENABLED", raising=False)
    monkeypatch.delenv("PDT_BASE_DRIFT_BASE", raising=False)
    repo = _init_repo(tmp_path / "repo")
    _set_origin_main(repo, "HEAD")

    report = base_drift.check_and_merge_base(repo)

    assert report.status == STATUS_NONE
    assert report.event == "base_drift_none"
    assert (report.behind, report.ahead) == (0, 0)
    assert len(report.base_sha) == 40
    assert report.base_ref == "origin/main"


def test_ahead_only_is_none(tmp_path):
    """A branch that is *ahead* of its base is still in sync.

    Two commits that HEAD has and the base does not are not drift: the
    base has nothing to offer the branch.  Reporting them as "behind"
    would send the round off to merge a base that is behind the branch
    — the classic way a drift check starts merging the wrong way round.
    """
    repo = _init_repo(tmp_path / "repo")
    _set_origin_main(repo, "HEAD")
    _commit(repo, "second")
    _commit(repo, "third")

    report = base_drift.check_and_merge_base(repo)

    assert report.status == STATUS_NONE
    assert report.event == "base_drift_none"
    assert report.behind == 0
    assert report.ahead == 2


def test_unresolvable_base_is_failed(tmp_path):
    """A base ref that does not resolve fails with git's own complaint.

    The repository has a *local* ``main`` branch, and the base is
    ``origin/main`` — which does not exist.  Resolving the base must
    not quietly fall back to the local branch of the same name: that
    fallback is what makes a branch that never fetched look perfectly
    in sync.
    """
    repo = _init_repo(tmp_path / "repo")

    report = base_drift.check_and_merge_base(repo)

    assert report.status == STATUS_FAILED
    assert report.event == "base_drift_failed"
    assert report.blocked is False
    assert report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200


def test_bare_base_ref_normalized_to_origin(tmp_path):
    """A bare base name means ``origin/<name>``, never the local branch.

    The local ``main`` sits where HEAD sits; ``origin/main`` is one
    commit further ahead.  Comparing against the local branch would
    report "no drift" — so the expected count (one commit behind)
    proves which of the two refs was used.
    """
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-b", "base-work")
    _commit(repo, "base-moved-on")
    _set_origin_main(repo, "base-work")
    _git(repo, "checkout", "main")

    calls: list = []
    report = base_drift.check_and_merge_base(
        repo,
        config=BaseDriftConfig(base_ref="main"),
        git_runner=_recording_runner(calls),
    )

    assert report.base_ref == "origin/main"
    assert report.behind == 1
    assert report.ahead == 0

    rev_list_calls = [call for call in calls if "rev-list" in call]
    assert rev_list_calls, f"no rev-list call was made: {calls}"
    assert any("origin/main" in " ".join(call) for call in rev_list_calls), calls


def test_none_path_does_not_fetch(tmp_path):
    """A branch in sync with its base must not fetch.

    "Behind is 0" means the rest of the round behaves exactly as it
    did before the base-drift check existed — no network, no ref
    movement, no side effect on the repository.
    """
    repo = _init_repo(tmp_path / "repo")
    _set_origin_main(repo, "HEAD")

    calls: list = []
    report = base_drift.check_and_merge_base(repo, git_runner=_recording_runner(calls))

    assert report.status == STATUS_NONE
    assert not [call for call in calls if "fetch" in call], calls


def test_resolve_base_ref_normalizes_bare_names():
    """Only a bare name is rewritten; a name that names a remote is kept.

    ``main`` becomes ``origin/main``; ``origin/main`` and
    ``upstream/main`` are already remote-tracking names and are used
    as they stand.
    """
    assert base_drift.resolve_base_ref("main") == "origin/main"
    assert base_drift.resolve_base_ref("develop") == "origin/develop"
    assert base_drift.resolve_base_ref("origin/main") == "origin/main"
    assert base_drift.resolve_base_ref("upstream/main") == "upstream/main"
    assert base_drift.resolve_base_ref("  main  ") == "origin/main"
    assert base_drift.resolve_base_ref("") == "origin/main"


def test_count_divergence_returns_counts_and_stderr(tmp_path):
    """The counting helper answers with ``(behind, ahead, stderr)``.

    On success the complaint is empty; on failure the counts are zero
    (a number nobody should trust is worse than no number) and the
    complaint says why.
    """
    repo = _init_repo(tmp_path / "repo")
    _set_origin_main(repo, "HEAD")
    _commit(repo, "second")

    behind, ahead, stderr = base_drift.count_divergence(repo, "origin/main")
    assert (behind, ahead) == (0, 1)
    assert stderr == ""

    behind, ahead, stderr = base_drift.count_divergence(repo, "origin/nope")
    assert (behind, ahead) == (0, 0)
    assert stderr
    assert len(stderr) <= 200
