"""
Rollback Manager
================

Manages task checkpoints and rollback operations.
"""

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class TaskCheckpoint:
    """Represents a task checkpoint in git history."""
    task_id: str
    commit_hash: str
    timestamp: str
    message: str


class RollbackManager:
    """Manages task checkpoints and rollback operations."""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir

    def get_checkpoints(self) -> List[TaskCheckpoint]:
        """
        Get all task checkpoints from git history.

        Returns:
            List of TaskCheckpoint objects, most recent first
        """
        try:
            result = subprocess.run(
                ['git', 'log', '--grep=^\\[task-', '--oneline', '--format=%H|%ci|%s'],
                capture_output=True,
                text=True,
                cwd=self.project_dir
            )

            checkpoints = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = line.split('|')
                    if len(parts) >= 3:
                        msg = parts[2]
                        task_id = self._extract_task_id(msg)
                        checkpoints.append(TaskCheckpoint(
                            task_id=task_id,
                            commit_hash=parts[0],
                            timestamp=parts[1],
                            message=msg
                        ))
            return checkpoints
        except Exception as e:
            print(f"Error getting checkpoints: {e}")
            return []

    def rollback_to_task(self, task_id: str, keep_changes: bool = False) -> bool:
        """
        Rollback to the state after a specific task completed.

        Args:
            task_id: The task ID to rollback to
            keep_changes: If True, stash changes; if False, discard them

        Returns:
            True if rollback succeeded
        """
        checkpoints = self.get_checkpoints()
        target = next((c for c in checkpoints if c.task_id == task_id), None)

        if not target:
            print(f"No checkpoint found for task {task_id}")
            return False

        try:
            if keep_changes:
                # Check if there are changes to stash
                status_result = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True,
                    text=True,
                    cwd=self.project_dir
                )
                if status_result.stdout.strip():
                    subprocess.run(
                        ['git', 'stash', 'push', '-m', f'Pre-rollback stash for task {task_id}'],
                        cwd=self.project_dir
                    )
                    print("Changes stashed.")

            result = subprocess.run(
                ['git', 'reset', '--hard', target.commit_hash],
                capture_output=True,
                text=True,
                cwd=self.project_dir
            )

            if result.returncode == 0:
                print(f"Successfully rolled back to task {task_id} (commit {target.commit_hash[:8]})")
                return True
            else:
                print(f"Rollback failed: {result.stderr}")
                return False
        except Exception as e:
            print(f"Rollback error: {e}")
            return False

    def rollback_to_previous(self, keep_changes: bool = False) -> bool:
        """
        Rollback to the previous task checkpoint.

        Args:
            keep_changes: If True, stash changes; if False, discard them

        Returns:
            True if rollback succeeded
        """
        checkpoints = self.get_checkpoints()
        if len(checkpoints) < 2:
            print("No previous checkpoint to rollback to.")
            return False

        return self.rollback_to_task(checkpoints[1].task_id, keep_changes)

    def get_current_task_checkpoint(self) -> Optional[TaskCheckpoint]:
        """
        Get the most recent task checkpoint.

        Returns:
            TaskCheckpoint or None if no checkpoints exist
        """
        checkpoints = self.get_checkpoints()
        return checkpoints[0] if checkpoints else None

    def list_rollback_points(self, limit: int = 20) -> str:
        """
        Get formatted list of rollback points.

        Args:
            limit: Maximum number of rollback points to show

        Returns:
            Formatted string of rollback points
        """
        checkpoints = self.get_checkpoints()
        lines = ["Available rollback points:"]

        if not checkpoints:
            lines.append("  (No task checkpoints found)")
            return '\n'.join(lines)

        for i, cp in enumerate(checkpoints[:limit], 1):
            # Truncate message if too long
            msg_display = cp.message[:60] + "..." if len(cp.message) > 60 else cp.message
            lines.append(f"  {i}. [{cp.task_id}] {cp.timestamp[:10]} - {msg_display}")

        if len(checkpoints) > limit:
            lines.append(f"  ... and {len(checkpoints) - limit} more")

        return '\n'.join(lines)

    def create_task_commit(self, task_id: str, title: str, description: str,
                          test_command: str, changed_files: List[str]) -> str:
        """
        Create a commit message in the task checkpoint format.

        Args:
            task_id: Task identifier
            title: Task title
            description: Task description
            test_command: Test command used
            changed_files: List of changed file paths

        Returns:
            Formatted commit message
        """
        files_section = "\n".join(f"- {f}" for f in changed_files) if changed_files else "(No files changed)"

        commit_message = f"""[task-{task_id}] {title}

Task ID: {task_id}
Description: {description}
Test Command: {test_command}

Files changed:
{files_section}

Co-Authored-By: Autonomous Agent <agent@autonomous.dev>
"""
        return commit_message

    def _extract_task_id(self, message: str) -> str:
        """
        Extract task ID from commit message.

        Args:
            message: Commit message

        Returns:
            Task ID string or "unknown"
        """
        match = re.search(r'\[task-([^\]]+)\]', message)
        return match.group(1) if match else "unknown"
