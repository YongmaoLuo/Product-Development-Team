"""OS-level file locking utilities for autonomous agent execution.

``FileLockManager`` provides a simple acquire/release wrapper around the
``filelock`` library, so that multiple agents working on the same project
can coordinate access to files without relying on a single-process mutex.

The lock files themselves are **not** kept inside the workspace: see
:func:`file_lock_protocol.locks_dir_for_plan` for why (short version — a
plan's locks are per-plan runtime state, and the workspace is someone
else's git tree, which ``git add -A`` checkpoints were committing them
into). The caller says where they go; this class never guesses a location
inside a project.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from filelock import FileLock, Timeout

from file_lock_protocol import fallback_locks_dir, lock_file_path


class FileLockManager:
    """Manage a set of OS-level file locks for a list of project files.

    Locks are acquired in lexicographic order of the target file paths to
    avoid deadlocks when a caller needs to lock more than one file.  The
    manager tracks the locks it currently holds and can release them all at
    once.
    """

    def __init__(self) -> None:
        self._locks: List[FileLock] = []
        self._lock_files: List[Path] = []

    @staticmethod
    def _lock_file_path(file_path: str, project_dir: str, locks_dir=None) -> Path:
        """Return the canonical lock file path for ``file_path``.

        Delegates to the shared helper rather than repeating the digest:
        the broker takes its locks through the same function, and if the
        two ever disagreed, a broker and a manager in the same process
        would each hold a *different* lock file for one target — mutual
        exclusion that looks like it works and is not.
        """
        if locks_dir is None:
            locks_dir = fallback_locks_dir(project_dir)
        return lock_file_path(locks_dir, project_dir, file_path)

    def acquire(
        self,
        files: List[str],
        project_dir: str,
        locks_dir=None,
        timeout: float = 5.0,
    ) -> None:
        """Acquire OS-level locks for ``files``.

        Args:
            files: Target file paths to lock.  An empty list acquires no
                locks and returns immediately.
            project_dir: The workspace whose files are being guarded. Used
                to canonicalise each target, not as a storage location.
            locks_dir: Where the lock files go. Callers that know their
                plan pass ``file_lock_protocol.locks_dir_for_plan(...)``;
                ``None`` falls back to a workspace-derived directory, which
                is what keeps a caller that does not know its plan from
                writing lock files into the workspace itself.
            timeout: Seconds to wait for each lock before raising
                ``TimeoutError``.

        Raises:
            TimeoutError: If any lock cannot be acquired within ``timeout``
                seconds.
        """
        if not files:
            return

        # Acquire locks in deterministic lexicographic order to avoid
        # deadlocks when multiple callers lock overlapping file sets.
        sorted_files = sorted(files)
        lock_root = (
            Path(locks_dir) if locks_dir is not None
            else fallback_locks_dir(project_dir)
        )
        lock_root.mkdir(parents=True, exist_ok=True)

        self._locks = []
        self._lock_files = []

        try:
            for file_path in sorted_files:
                lock_path = self._lock_file_path(file_path, project_dir, lock_root)
                self._lock_files.append(lock_path)
                lock = FileLock(str(lock_path), timeout=timeout)
                lock.acquire()
                self._locks.append(lock)
        except Timeout as exc:
            # Release anything we already acquired and surface a plain
            # TimeoutError consistent with the module contract.
            self.release()
            raise TimeoutError(
                f"Failed to acquire lock for {exc.lock_file} within {timeout}s"
            ) from exc

    def release(self) -> None:
        """Release all locks currently held by this manager.

        This method is safe to call from ``finally`` blocks: it never raises,
        and it clears the internal tracking state even if an individual lock
        release fails.
        """
        try:
            # Release in reverse acquisition order.
            for lock in reversed(self._locks):
                try:
                    lock.release()
                except Exception:
                    # Ignore locks that were already released or are in an
                    # unexpected state; the goal is to free resources safely.
                    pass
        finally:
            self._locks.clear()
            self._lock_files.clear()
