"""The undeclared-write gate must take its baseline WITHOUT spawning git.

The gate reads the HEAD the attempt started on, before the attempt is
dispatched.  That read used to be ``git rev-parse HEAD`` — a
subprocess — and a quota park is a path that must start no subprocess at
all ("a quota park spawns nothing — it waits and stops").  The baseline
is taken on that path, so the read has to be pure Python.

This file pins that:

* :meth:`AutonomousAgent._capture_attempt_baseline` starts no process
  (``subprocess.run`` and ``subprocess.Popen`` are both watched) and
  still answers the HEAD it is standing on;
* the empty answer is unchanged — a directory that is not a repository
  answers ``""``, and every caller already reads ``""`` as "no
  baseline";
* the pure-Python reader understands the layouts git actually writes:
  a normal repository, a detached HEAD, a branch in the loose ref, a
  branch only present in ``packed-refs``, and a linked worktree whose
  ``.git`` is a file and whose refs live under ``commondir``.

The repository layouts here are written by hand: the point of the file
is that the answer comes from reading files, so the test does not need
a git binary to build its fixtures.
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

import agent  # noqa: E402
from agent import AutonomousAgent  # noqa: E402

_SHA = "1" * 40
_OTHER_SHA = "2" * 40


class _Task:
    """The only thing the baseline reads off a task is ``project_dir``."""

    def __init__(self, project_dir):
        self.project_dir = project_dir


def _agent_for(project_dir: Path) -> AutonomousAgent:
    """An executor bound to ``project_dir``.

    The real constructor builds a ``GitManager``, and GitManager opens
    the repository with GitPython — which is process work this file is
    not about.  The baseline method reads nothing but ``project_dir``,
    so the instance is built without the constructor.
    """
    instance = object.__new__(AutonomousAgent)
    instance.project_dir = project_dir
    return instance


@pytest.fixture
def no_spawn(monkeypatch):
    """Make any process start an immediate, loud failure."""
    started = []

    def _forbidden(name):
        def _call(*args, **kwargs):
            started.append((name, args, kwargs))
            raise AssertionError(
                f"{name} was called — the baseline must be read in pure Python"
            )

        return _call

    monkeypatch.setattr(subprocess, "run", _forbidden("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _forbidden("subprocess.Popen"))
    monkeypatch.setattr(agent.subprocess, "run", _forbidden("agent.subprocess.run"))
    monkeypatch.setattr(agent.subprocess, "Popen", _forbidden("agent.subprocess.Popen"))
    return started


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _normal_repo(root: Path, head: str) -> Path:
    """A repository whose ``.git`` is a directory."""
    git_dir = root / ".git"
    _write(git_dir / "HEAD", head)
    return git_dir


def test_baseline_reads_head_without_starting_a_process(tmp_path, no_spawn):
    """The HEAD is answered from the files, not from a ``git`` process."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _normal_repo(repo, "ref: refs/heads/main\n")
    _write(repo / ".git" / "refs" / "heads" / "main", f"{_SHA}\n")

    baseline = _agent_for(repo)._capture_attempt_baseline(_Task(repo))

    assert baseline == _SHA
    assert no_spawn == [], f"a process was started: {no_spawn!r}"


def test_baseline_reads_a_detached_head(tmp_path, no_spawn):
    """A detached HEAD holds the commit id itself."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _normal_repo(repo, f"{_SHA}\n")

    baseline = _agent_for(repo)._capture_attempt_baseline(_Task(repo))

    assert baseline == _SHA
    assert no_spawn == []


def test_baseline_reads_a_branch_from_packed_refs(tmp_path, no_spawn):
    """A branch with no loose ref is answered from ``packed-refs``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _normal_repo(repo, "ref: refs/heads/main\n")
    _write(
        repo / ".git" / "packed-refs",
        "# pack-refs with: peeled fully-peeled sorted \n"
        f"{_OTHER_SHA} refs/heads/other\n"
        f"{_SHA} refs/heads/main\n"
        f"{_OTHER_SHA} refs/tags/v1\n",
    )

    baseline = _agent_for(repo)._capture_attempt_baseline(_Task(repo))

    assert baseline == _SHA
    assert no_spawn == []


def test_baseline_reads_a_worktree_head_through_commondir(tmp_path, no_spawn):
    """A linked worktree keeps HEAD in its own gitdir and refs outside."""
    main = tmp_path / "main"
    main.mkdir()
    main_git = _normal_repo(main, "ref: refs/heads/main\n")
    _write(main_git / "refs" / "heads" / "main", f"{_SHA}\n")

    worktree_git = main_git / "worktrees" / "wt"
    _write(worktree_git / "HEAD", "ref: refs/heads/main\n")
    _write(worktree_git / "commondir", "../..\n")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write(worktree / ".git", f"gitdir: {worktree_git}\n")

    baseline = _agent_for(worktree)._capture_attempt_baseline(_Task(worktree))

    assert baseline == _SHA
    assert no_spawn == []


def test_baseline_reads_a_worktree_head_with_a_relative_gitdir(tmp_path, no_spawn):
    """A ``gitdir:`` pointer relative to the worktree is resolved."""
    main = tmp_path / "main"
    main.mkdir()
    main_git = _normal_repo(main, "ref: refs/heads/main\n")
    _write(main_git / "refs" / "heads" / "main", f"{_SHA}\n")

    worktree_git = main_git / "worktrees" / "wt"
    _write(worktree_git / "HEAD", f"{_OTHER_SHA}\n")
    _write(worktree_git / "commondir", "../..\n")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write(worktree / ".git", "gitdir: ../main/.git/worktrees/wt\n")

    baseline = _agent_for(worktree)._capture_attempt_baseline(_Task(worktree))

    assert baseline == _OTHER_SHA
    assert no_spawn == []


def test_baseline_is_empty_outside_a_repository(tmp_path, no_spawn):
    """No repository means no baseline — and no process to find out."""
    plain = tmp_path / "plain"
    plain.mkdir()

    baseline = _agent_for(plain)._capture_attempt_baseline(_Task(plain))

    assert baseline == ""
    assert no_spawn == []


def test_baseline_is_empty_when_head_is_not_a_commit_id(tmp_path, no_spawn):
    """A HEAD that is not a commit id answers ``""``, not garbage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _normal_repo(repo, "ref: refs/heads/main\n")
    _write(repo / ".git" / "refs" / "heads" / "main", "not-a-sha\n")

    baseline = _agent_for(repo)._capture_attempt_baseline(_Task(repo))

    assert baseline == ""
    assert no_spawn == []


def test_baseline_matches_git_rev_parse_head_in_a_real_repository(tmp_path):
    """The pure-Python reader agrees with git on a real repository.

    This is the cross-check against the implementation this replaced:
    git is run here, by the test, on purpose — the point is that the two
    answers are the same.
    """
    if shutil.which("git") is None:
        pytest.skip("no git binary available")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "file.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@e", "-c", "user.name=t",
            "commit", "-q", "-m", "one",
        ],
        check=True,
    )
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    baseline = _agent_for(repo)._capture_attempt_baseline(_Task(repo))

    assert baseline == expected
