"""The conflict path of the base-drift check.

A merge that conflicts is the one outcome that stops a verification
round, so it has to be reported honestly:

* the **conflicting files** are listed, each with the number of
  conflict blocks in it — a path alone does not say whether the
  conflict was one line or forty,
* the merge is **aborted**: the working tree goes back to the state it
  was in before the merge attempt and no ``MERGE_HEAD`` is left
  behind, so the next round starts from a clean tree,
* **no local commit is lost** — the abort rolls back the merge
  attempt, not the branch,
* the branch stays **behind** its base, so the next round sees the
  same conflict again.  The check keeps no memory of the attempt: a
  conflict that was reported once is still a conflict until somebody
  resolves it.

The conflicting paths are read **NUL separated** (``git diff --name-only
--diff-filter=U -z``), so a path with a space in it survives the trip
intact.

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
    STATUS_CONFLICTS,
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

#: How many lines ``app.py`` has.  Long enough that the two changes
#: both sides make land in **two separate hunks** — a hunk and a
#: neighbour less than six lines apart would be merged into one, and
#: the file would carry one conflict block instead of two.
APP_LINES = 12

#: The lines both sides change in ``app.py``.  Far enough apart to
#: stay two conflict blocks.
APP_CHANGED_LINES = (2, 11)


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


def _app_text(changed: dict) -> str:
    """The ``app.py`` content, with the given 1-based lines replaced."""
    lines = [f"line{index:02d}" for index in range(1, APP_LINES + 1)]
    for lineno, text in changed.items():
        lines[lineno - 1] = text
    return "\n".join(lines) + "\n"


class _Repos:
    """The three repositories a conflict test needs.

    ``source``   where the base is authored,
    ``bare``     ``origin.git`` — what the work tree fetches from,
    ``work``     the branch under test.
    """

    def __init__(self, source: Path, bare: Path, work: Path) -> None:
        self.source = source
        self.bare = bare
        self.work = work


def _conflicting_repos(tmp_path: Path) -> _Repos:
    """Build a work tree whose merge with its base conflicts.

    Both sides changed the *same* lines:

    * ``app.py`` — two changes, far enough apart to stay two conflict
      blocks,
    * ``other.py`` — one change, one conflict block,
    * ``with space.py`` — one change in a path with a space in it.

    The base is published to a local bare repository, so the fetch
    inside the check really runs — against a path on disk.
    """
    source = _init_repo(tmp_path / "source")
    (source / "app.py").write_text(_app_text({}), encoding="utf-8")
    (source / "other.py").write_text("other01\nother02\nother03\n", encoding="utf-8")
    (source / "with space.py").write_text("space01\nspace02\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "initial content")

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
    _git(work, "fetch", "origin", "main:refs/remotes/origin/main")
    _git(work, "checkout", "-b", "feat", "refs/remotes/origin/main")

    # The branch changes the lines the base is about to change.
    (work / "app.py").write_text(
        _app_text({lineno: f"line{lineno:02d} feat" for lineno in APP_CHANGED_LINES}),
        encoding="utf-8",
    )
    (work / "other.py").write_text("other01 feat\nother02\nother03\n", encoding="utf-8")
    (work / "with space.py").write_text("space01 feat\nspace02\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "feat change")

    # And so does the base.
    (source / "app.py").write_text(
        _app_text({lineno: f"line{lineno:02d} base" for lineno in APP_CHANGED_LINES}),
        encoding="utf-8",
    )
    (source / "other.py").write_text("other01 base\nother02\nother03\n", encoding="utf-8")
    (source / "with space.py").write_text("space01 base\nspace02\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "base moved on")
    subprocess.run(
        ["git", "-C", str(source), "push", str(bare), "main:main"],
        check=True,
        text=True,
        capture_output=True,
    )

    _git(work, "fetch", "origin", "main")
    return _Repos(source, bare, work)


def _recording_runner(calls: list):
    """A git runner that records every call and runs it for real."""

    def runner(project_dir, args, timeout_sec):
        calls.append([str(arg) for arg in args])
        return base_drift.run_git(project_dir, args, timeout_sec)

    return runner


def test_conflicting_merge_reports_conflicts(tmp_path):
    """A merge that conflicts is reported as a conflict — with its files.

    The report names every path the merge left unmerged, so whoever
    reads the event log knows *what* has to be resolved, not merely
    that something has.  The list is read NUL separated, which is what
    keeps a path with a space in it in one piece.

    The excerpt carries git's own complaint — the ``CONFLICT (content)``
    line — cut to 200 characters.
    """
    repos = _conflicting_repos(tmp_path)

    calls: list = []
    report = base_drift.check_and_merge_base(
        repos.work, config=BaseDriftConfig(), git_runner=_recording_runner(calls)
    )

    assert report.status == STATUS_CONFLICTS
    assert report.event == "base_drift_conflicts"
    assert report.blocked is True

    # Every conflicting path is named — including the one with a space.
    paths = {entry["path"] for entry in report.conflict_files}
    assert paths == {"app.py", "other.py", "with space.py"}, report.conflict_files
    for entry in report.conflict_files:
        assert set(entry) == {"path", "conflict_blocks"}, entry

    # Git's own complaint about the conflict, cut to 200 characters.
    assert report.stderr_excerpt.startswith("CONFLICT (content)"), report.stderr_excerpt
    assert len(report.stderr_excerpt) <= 200

    # The list of unmerged paths was read NUL separated.
    diffs = [call for call in calls if call and call[0] == "diff"]
    assert diffs, calls
    assert "--diff-filter=U" in diffs[0], diffs
    assert "-z" in diffs[0], diffs


def test_conflict_blocks_counted_per_file(tmp_path):
    """Each conflicting file carries its own number of conflict blocks.

    The path alone does not say how much has to be resolved: a file
    with one conflict block and a file with two are very different
    pieces of work.  The count is the number of conflict markers in
    the file — and it is taken **before** the merge is aborted, because
    afterwards the markers are gone.

    ``base_drift.conflict_files`` is the same count, available on its
    own.
    """
    repos = _conflicting_repos(tmp_path)

    # The same count, taken by hand while the merge is still in flight.
    conflicted = subprocess.run(
        ["git", "-C", str(repos.work), "merge", "--no-ff", "-m", "manual", "origin/main"],
        text=True,
        capture_output=True,
    )
    assert conflicted.returncode != 0, conflicted.stdout

    listed = base_drift.conflict_files(repos.work)
    counted = {entry["path"]: entry["conflict_blocks"] for entry in listed}
    assert counted == {"app.py": 2, "other.py": 1, "with space.py": 1}, listed
    for entry in listed:
        assert isinstance(entry["path"], str), entry
        assert isinstance(entry["conflict_blocks"], int), entry

    _git(repos.work, "merge", "--abort")

    # And the same count in the report.
    report = base_drift.check_and_merge_base(
        repos.work, config=BaseDriftConfig()
    )
    assert report.status == STATUS_CONFLICTS
    counted = {entry["path"]: entry["conflict_blocks"] for entry in report.conflict_files}
    assert counted == {"app.py": 2, "other.py": 1, "with space.py": 1}, report.conflict_files


def test_merge_aborted_and_tree_clean(tmp_path):
    """The conflicted merge is aborted — the tree goes back to clean.

    A merge that was left in flight would leave ``MERGE_HEAD`` behind
    and the working tree full of conflict markers: the next round
    would start on a tree nobody authored, and the merge would have to
    be undone by hand.  So the merge is aborted right after the
    conflicts were counted, and the tree is clean again.
    """
    repos = _conflicting_repos(tmp_path)

    report = base_drift.check_and_merge_base(repos.work, config=BaseDriftConfig())
    assert report.status == STATUS_CONFLICTS

    # No merge is in flight any more.
    assert not (repos.work / ".git" / "MERGE_HEAD").exists()

    # And the working tree is clean — no conflict markers left in it.
    assert _git(repos.work, "status", "--porcelain").stdout == ""
    assert "<<<<<<<" not in (repos.work / "app.py").read_text(encoding="utf-8")


def test_abort_keeps_local_commits(tmp_path):
    """The abort rolls back the merge attempt — not the branch.

    The branch's own commits are the work the round is about; losing
    them to a merge that could not be finished would be the worst
    outcome the check could produce.  So the abort restores the tree
    and leaves the branch where it was.

    The branch stays behind its base, so the next round runs into the
    same conflict again — the check keeps no memory of the attempt.
    """
    repos = _conflicting_repos(tmp_path)
    head_before = _git(repos.work, "rev-parse", "HEAD").stdout.strip()
    log_before = _git(repos.work, "log", "--oneline").stdout

    report = base_drift.check_and_merge_base(repos.work, config=BaseDriftConfig())
    assert report.status == STATUS_CONFLICTS

    # The branch did not move, and its commits are all still there.
    assert _git(repos.work, "rev-parse", "HEAD").stdout.strip() == head_before
    log_after = _git(repos.work, "log", "--oneline").stdout
    assert log_after == log_before, log_after
    assert "feat change" in log_after

    # The branch is still behind — the conflict is not "handled".
    behind = _git(repos.work, "rev-list", "--count", "HEAD..origin/main").stdout.strip()
    assert int(behind) >= 1, behind

    # And the next round sees it again.
    again = base_drift.check_and_merge_base(repos.work, config=BaseDriftConfig())
    assert again.status == STATUS_CONFLICTS
    assert again.conflict_files


def test_conflict_event_and_base_fields(tmp_path):
    """The conflict report names the base it is about.

    A conflict is only actionable against a base: which ref, which
    commit, how far behind.  The event name is the stable handle the
    log is grepped by, and the mode says how the check was asked to
    behave — ``report`` and ``repair`` are the same outcome here,
    because this task only reports.
    """
    repos = _conflicting_repos(tmp_path)

    report = base_drift.check_and_merge_base(repos.work, config=BaseDriftConfig())

    assert report.event == "base_drift_conflicts"
    assert report.status == STATUS_CONFLICTS
    assert report.base_ref == "origin/main"
    assert len(report.base_sha) == 40, report.base_sha
    assert report.base_sha == _git(repos.work, "rev-parse", "origin/main").stdout.strip()
    assert report.behind >= 1, report.behind
    assert report.ahead >= 1, report.ahead
    assert report.mode == "report"

    # The payload carries the conflicts too.
    payload = report.to_dict()
    assert payload["status"] == STATUS_CONFLICTS
    assert payload["conflict_files"] == report.conflict_files
    assert payload["base_sha"] == report.base_sha

    # ``repair`` is only a label in this task — the outcome is the same.
    repaired = base_drift.check_and_merge_base(
        repos.work, config=BaseDriftConfig(mode="repair")
    )
    assert repaired.status == STATUS_CONFLICTS
    assert repaired.mode == "repair"
    assert repaired.conflict_files
