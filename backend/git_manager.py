"""
Git Manager
===========

Manages Git operations for version control.
"""


class GitManager:
    """Handles Git operations."""
    
    def __init__(self, repo_path: str):
        import git
        self.repo = git.Repo(repo_path, search_parent_directories=True)
    
    def commit(self, message: str):
        """
        Stage and commit all changes.
        
        Args:
            message: Commit message
        """
        self.repo.git.add(A=True)
        self.repo.index.commit(message)
    
    def get_diff(self) -> str:
        """
        Get unstaged diff.
        
        Returns:
            Git diff as string
        """
        return self.repo.git.diff(None)
    
    def get_untracked_files(self) -> list:
        """
        Get list of untracked files.

        Returns:
            List of untracked file paths
        """
        return self.repo.untracked_files

    def get_changed_files(self) -> list:
        """
        Get all changed files (modified + untracked), excluding common noise.

        Returns:
            List of relative file paths
        """
        exclude = {"tasks.json", "tasks.lock"}
        changed = []
        for item in self.repo.index.diff(None):
            if item.a_path not in exclude:
                changed.append(item.a_path)
        for item in self.repo.untracked_files:
            if item not in exclude:
                changed.append(item)
        return changed

    def rev_parse(self, ref: str = "HEAD") -> str:
        """
        Resolve a revision to its full commit SHA.

        Added 2026-09-20. ``AutonomousAgent._commit_task_changes`` has
        called ``git_manager.rev_parse("HEAD")`` since the 2026-09-11
        plan to write the resulting SHA back into
        ``plan_tasks.commit_sha``, but this method did not exist — every
        call raised ``AttributeError``, the call site's
        ``except Exception`` swallowed it into a
        ``task_commit_sha_rev_parse_failed`` warning, and the writeback
        it was meant to close the loop on never ran. The unit tests
        missed it because they hand ``git_manager`` a ``MagicMock``,
        whose ``rev_parse`` always answers.

        Args:
            ref: Any revision ``git rev-parse`` accepts — a ref name, a
                SHA, ``HEAD~1``. Defaults to ``HEAD``.

        Returns:
            The full 40-character SHA as a string.

        Raises:
            git.exc.GitCommandError: when ``ref`` does not resolve. The
                caller treats any exception as "no SHA available" and
                carries on; the git history stays authoritative.
        """
        return self.repo.git.rev_parse(ref)
