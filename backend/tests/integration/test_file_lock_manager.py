"""Integration tests for ``FileLockManager`` on the real filesystem.

These tests exercise the PRD test-design decision point 3 contract:

  * OS-level mutual exclusion for target files
  * timeout raises ``TimeoutError``
  * multiple locks acquired in lexicographic order to avoid deadlocks
  * lock files are placed by the caller, normally beside the plan
    (see ``file_lock_protocol.locks_dir_for_plan``)

Moved from ``tests/integration/test_file_lock_manager.py`` so that
pytest, when invoked from the ``backend/`` project root, can collect
the tests without resolving ``from backend.file_lock_manager import
FileLockManager`` (which would require ``backend/`` to be on
``sys.path`` as a parent directory).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from file_lock_manager import FileLockManager


pytestmark = pytest.mark.integration


def _lock_dir(project_dir: Path, locks_dir=None) -> Path:
    """Where the lock files for these tests land — asked of production.

    These tests call :meth:`FileLockManager.acquire` without a plan
    directory, so the manager takes its fallback (a workspace-derived
    directory outside the tree). Recomputing that here would pin a
    location production could change without the test noticing.
    """
    from file_lock_protocol import fallback_locks_dir

    return Path(locks_dir) if locks_dir is not None else fallback_locks_dir(project_dir)


def test_single_lock_acquire_release(tmp_path: Path) -> None:
    """A single lock can be acquired and then released."""
    project_dir = tmp_path / "proj"
    flm = FileLockManager()

    flm.acquire(["src/a.py"], str(project_dir))

    # Lock file should have been created under the derived locks dir.
    locks = list(_lock_dir(project_dir).glob("*.lock"))
    assert len(locks) == 1

    flm.release()

    # Releasing leaves the lock file on disk (filelock behaviour) but frees
    # the OS-level lock.  We verify the release by re-acquiring it with a
    # second manager; a held lock would block and then raise TimeoutError.
    flm2 = FileLockManager()
    flm2.acquire(["src/a.py"], str(project_dir), timeout=0.5)
    flm2.release()


def test_multiple_locks_ordered(tmp_path: Path) -> None:
    """Multiple locks are acquired in sorted file order."""
    project_dir = tmp_path / "proj"
    flm = FileLockManager()

    files = ["src/z.py", "src/a.py", "src/m.py"]
    flm.acquire(files, str(project_dir))
    try:
        # All three lock files exist.
        locks = list(_lock_dir(project_dir).glob("*.lock"))
        assert len(locks) == 3
    finally:
        flm.release()

    # The same set of files, given in a different order, must produce the
    # same set of lock files.
    flm2 = FileLockManager()
    files_reordered = ["src/m.py", "src/a.py", "src/z.py"]
    flm2.acquire(files_reordered, str(project_dir))
    try:
        locks2 = list(_lock_dir(project_dir).glob("*.lock"))
        assert {p.name for p in locks2} == {p.name for p in locks}
    finally:
        flm2.release()


def test_acquire_timeout(tmp_path: Path) -> None:
    """Trying to acquire an already-held lock raises TimeoutError."""
    project_dir = tmp_path / "proj"
    flm1 = FileLockManager()
    flm1.acquire(["src/a.py"], str(project_dir))

    try:
        flm2 = FileLockManager()
        with pytest.raises(TimeoutError):
            flm2.acquire(["src/a.py"], str(project_dir), timeout=0.1)
    finally:
        flm1.release()


def test_lock_dir_can_be_pinned_to_the_plan(tmp_path: Path) -> None:
    """Passing a plan's lock directory puts the files there, not in the workspace.

    This is the path the executor takes: ``AutonomousAgent`` resolves
    ``plans/<plan_id>/locks/`` once and hands the same directory to the
    broker and to this manager, so the two cannot disagree about which
    file guards which target.
    """
    from file_lock_protocol import locks_dir_for_plan

    project_dir = tmp_path / "proj"
    plan_dir = tmp_path / "plans" / "20260101-a-plan"
    locks = locks_dir_for_plan(plan_dir)

    flm = FileLockManager()
    flm.acquire(["src/a.py"], str(project_dir), locks)
    try:
        written = list(locks.glob("*.lock"))
        assert len(written) == 1
        assert project_dir not in written[0].parents
    finally:
        flm.release()


def test_lock_file_location(tmp_path: Path) -> None:
    """Lock files are derivable from the project dir, and kept outside it."""
    project_dir = tmp_path / "proj"
    flm = FileLockManager()

    flm.acquire(["src/a.py"], str(project_dir))
    try:
        locks = list(_lock_dir(project_dir).glob("*.lock"))
        assert len(locks) == 1
        # Outside the project tree: the workspace is a git working tree,
        # and ``git add -A`` checkpoints were committing these.
        assert project_dir not in locks[0].parents
        assert locks[0].name.endswith(".lock")
    finally:
        flm.release()


def test_concurrent_acquire_blocks(tmp_path: Path) -> None:
    """A second concurrent acquire on the same file blocks (and times out)
    while a first manager is still holding the lock.

    The contract is:

      * Thread A acquires the lock and holds it for longer than the
        timeout Thread B uses.
      * Thread B's ``acquire`` must NOT return successfully during that
        window — it must remain blocked and ultimately raise
        ``TimeoutError`` after its timeout elapses.
      * Once Thread A releases, Thread B (started fresh) can acquire the
        lock without contention.
    """
    project_dir = tmp_path / "proj"

    holder = FileLockManager()
    holder.acquire(["src/concurrent.py"], str(project_dir))

    outcome: dict = {}
    started_at: list = []

    def contending_acquire() -> None:
        contender = FileLockManager()
        contender_started = time.time()
        started_at.append(contender_started)
        try:
            contender.acquire(
                ["src/concurrent.py"], str(project_dir), timeout=0.3
            )
            outcome["status"] = "acquired"
            outcome["started_at"] = contender_started
            outcome["ended_at"] = time.time()
            contender.release()
        except TimeoutError as exc:
            outcome["status"] = "timeout"
            outcome["started_at"] = contender_started
            outcome["ended_at"] = time.time()
            outcome["exc"] = exc

    try:
        t = threading.Thread(target=contending_acquire)
        t.start()
        # Hold the lock for noticeably longer than the contender's
        # 0.3s timeout so we can observe the block.
        time.sleep(0.8)
        t.join(timeout=5.0)
        assert not t.is_alive(), (
            "contending thread is still alive — acquire did not time out"
        )
    finally:
        holder.release()

    # Outcome 1: the contender blocked and then timed out, exactly as the
    # contract specifies.
    assert outcome.get("status") == "timeout", (
        f"expected contender to time out, got outcome={outcome!r}"
    )
    started = outcome["started_at"]
    ended = outcome["ended_at"]
    elapsed = ended - started
    # The contender's timeout was 0.3s; the holder held for 0.8s. The
    # contender must therefore have spent at least its 0.3s timeout
    # blocked waiting, and must NOT have completed before its own
    # timeout expired.
    assert elapsed >= 0.25, (
        f"contender returned in {elapsed:.2f}s — it did not actually block"
    )
    assert elapsed < 1.5, (
        f"contender took {elapsed:.2f}s to surface TimeoutError; "
        f"expected under 1.5s (timeout=0.3s + small jitter)"
    )

    # Outcome 2: once the holder released, a fresh acquire on the same
    # file succeeds without contention.
    fresh = FileLockManager()
    fresh.acquire(["src/concurrent.py"], str(project_dir), timeout=0.5)
    try:
        locks = list(_lock_dir(project_dir).glob("*.lock"))
        assert len(locks) == 1
    finally:
        fresh.release()
