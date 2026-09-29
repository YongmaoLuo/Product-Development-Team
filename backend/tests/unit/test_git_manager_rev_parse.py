"""``GitManager.rev_parse`` — the method that did not exist.

2026-09-20 (post-mortem)
-----------------------------

``AutonomousAgent._commit_task_changes`` has called
``self.git_manager.rev_parse("HEAD")`` since the 2026-09-11 plan, to write
the resulting commit SHA back into ``plan_tasks.commit_sha``. It was added
to close a real gap — 108/108 completed rows had ``commit_sha IS NULL``,
which broke any audit or card join keyed on the per-task commit.

``GitManager`` never grew the method. Every call raised
``AttributeError``, the call site's ``except Exception`` folded it into a
``task_commit_sha_rev_parse_failed`` warning nobody reads, and the
writeback silently never happened.

Why ``test_commit_sha_writeback.py`` did not catch it
-----------------------------------------------------

That file hands the agent a ``MagicMock`` git manager, so
``git_manager.rev_parse`` always answers — a mock cannot fail for a
missing method. Its assertions ("rev_parse was called", "its return value
reached update_task_commit_sha") were all true. The gap is between the
mock and the class, so the only test that can see it drives the **real**
``GitManager`` against a **real** repository. That is what this module
does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from git_manager import GitManager


def _git(project_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_init(project_dir: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main"],
        cwd=str(project_dir), check=True, capture_output=True, text=True,
    )
    _git(project_dir, "config", "user.email", "test@example.com")
    _git(project_dir, "config", "user.name", "Test User")
    (project_dir / "README.md").write_text("# baseline\n", encoding="utf-8")
    _git(project_dir, "add", "README.md")
    _git(project_dir, "commit", "-q", "-m", "baseline")


@pytest.fixture
def repo(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _git_init(project_dir)
    return project_dir


class TestRevParse:
    def test_the_method_exists(self):
        """The bug, stated in one line.

        ``GitManager`` is a plain class with no ``__getattr__`` fallback,
        so a missing method is an ``AttributeError`` at call time —
        which the caller swallows.
        """
        assert callable(getattr(GitManager, "rev_parse", None)), (
            "GitManager has no rev_parse; _commit_task_changes calls it on "
            "every completed task and swallows the AttributeError"
        )

    def test_head_resolves_to_the_real_sha(self, repo):
        assert GitManager(str(repo)).rev_parse("HEAD") == _git(repo, "rev-parse", "HEAD")

    def test_default_argument_is_head(self, repo):
        manager = GitManager(str(repo))
        assert manager.rev_parse() == manager.rev_parse("HEAD")

    def test_it_is_the_full_sha_not_an_abbreviation(self, repo):
        sha = GitManager(str(repo)).rev_parse("HEAD")
        assert len(sha) == 40
        assert sha == _git(repo, "rev-parse", "HEAD")

    def test_relative_refs_resolve(self, repo):
        (repo / "second.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "second.txt")
        _git(repo, "commit", "-q", "-m", "second")

        manager = GitManager(str(repo))
        assert manager.rev_parse("HEAD") == _git(repo, "rev-parse", "HEAD")
        assert manager.rev_parse("HEAD~1") == _git(repo, "rev-parse", "HEAD~1")

    def test_an_unresolvable_ref_raises(self, repo):
        """The caller's ``except`` contract: a raise means "no SHA", which
        is a warning, not a crash."""
        with pytest.raises(Exception):
            GitManager(str(repo)).rev_parse("no-such-ref-anywhere")


class TestCommitShaWritebackAgainstARealRepo:
    """The loop the missing method broke, driven end to end.

    ``test_commit_sha_writeback.py`` asserts the same interaction against
    a mock. This one leaves ``git_manager`` real, so the SHA that reaches
    ``update_task_commit_sha`` is one git actually produced.
    """

    @staticmethod
    def _agent(repo: Path):
        from agent import AutonomousAgent
        from task import SubTask

        agent = AutonomousAgent.__new__(AutonomousAgent)
        agent.git_manager = GitManager(str(repo))
        agent.logger = MagicMock()
        agent.rollback_manager = MagicMock()
        agent.rollback_manager.create_task_commit.return_value = "[task-1] fix"
        agent.task_manager = MagicMock()
        task = SubTask(
            id="1", title="fix", description="d", test_command="true",
            files_to_modify=[],
        )
        return agent, task

    def test_the_real_sha_reaches_the_writeback(self, repo):
        agent, task = self._agent(repo)
        (repo / "work.txt").write_text("done\n", encoding="utf-8")

        agent._commit_task_changes(task, ["work.txt"])

        agent.task_manager.update_task_commit_sha.assert_called_once()
        task_id, sha = agent.task_manager.update_task_commit_sha.call_args.args
        assert task_id == "1"
        assert sha == _git(repo, "rev-parse", "HEAD"), (
            "the writeback received something other than the commit git "
            "just created"
        )
        # And the warning the bug produced every single time is absent.
        logged = [c.args[0] for c in agent.logger.warning.call_args_list]
        assert "task_commit_sha_rev_parse_failed" not in logged, (
            "rev_parse still fails against a real repository"
        )
