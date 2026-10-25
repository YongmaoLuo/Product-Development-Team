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

import os
from pathlib import Path
from typing import List

from filelock import FileLock, Timeout

from file_lock_protocol import fallback_locks_dir, lock_file_path


def domains_overlap(a: str, b: str) -> bool:
    """Return whether two write domains claim any of the same files.

    A domain is a plan-declared path: a task may claim a whole directory
    (``src/a``) where another claims one file inside it (``src/a/b.py``).
    Those are the same bytes, so the plan gate must read them as a
    conflict. Equality alone does not — the strings differ, so a pair that
    edits one file through two spellings sails through.

    The comparison is **lexical**, deliberately: no ``Path.resolve()``, no
    ``os.stat``. The domains come from a plan, not from a checkout, and a
    task routinely claims a directory that does not exist yet — asking the
    filesystem would both cost a syscall per pair and answer the wrong
    question (``..`` in a plan is a claim about paths, not about links on
    this particular disk). ``os.path.normpath`` collapses ``.``/``..``/
    duplicate separators purely textually.

    The prefix test anchors on a separator rather than on a bare
    ``str.startswith``: ``src/ab/c.py`` starts with the characters
    ``src/a``, but it is a different directory, and refusing that pair
    would serialise two genuinely independent tasks behind each other.

    Args:
        a: First domain, relative to the project or absolute.
        b: Second domain, same conventions.

    Returns:
        ``True`` if the two domains are equal or one is an ancestor
        directory of the other. ``False`` if either is empty — an empty
        domain constrains nothing, so it conflicts with nothing.
    """
    if not a or not b:
        return False

    norm_a = os.path.normpath(a)
    norm_b = os.path.normpath(b)
    if norm_a == norm_b:
        return True

    sep = os.sep
    # A filesystem-root domain (POSIX "/" or a Windows drive root) is the
    # ancestor of everything, and it carries no trailing separator for the
    # prefix test above to append.
    if norm_a == sep or norm_b == sep:
        return True

    return norm_b.startswith(norm_a + sep) or norm_a.startswith(norm_b + sep)


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
