"""One executor per plan — the guard that makes a second one lose fast.

Why this exists
---------------
Two executors on the same plan contend for the same file locks, and the
loser does not lose fast: it waits out the full acquire timeout (300 s
by default) before it discovers the plan is already being worked on. Two
executors on one plan is a configuration mistake, and a mistake that
costs five minutes of wall clock every time it happens is worth
guarding before the first second of it is spent.

The guard is a single ``flock`` on ``<plan>/executor.lock``, because two
properties of that primitive are exactly what the guard needs:

  * it is **immediate** — a second acquirer gets ``EWOULDBLOCK`` at once
    instead of waiting, so the loser learns it lost in microseconds; and
  * it is **bound to the process lifecycle** — the kernel releases the
    lock when the holder dies, whoever killed it and however. There is
    no stale lock to reap, no pid to poll for liveness, and no timeout
    to tune.

The lock file is never deleted. Unlinking it is the classic way to
break ``flock``: the lock lives on the *inode*, so deleting the path
leaves the holder locking a file nobody can reach, and the next
acquirer creates a fresh inode and locks that instead — two holders,
both convinced they are alone.

What is pinned here
-------------------
The four behaviours the guard is bought for: one holder at a time,
across processes; a loser that finds out immediately; a lock that dies
with its holder; and a lock file that records *who* held it so a log
reader can attribute a stuck plan without guessing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from executor_lock import (  # noqa: E402
    LOCK_FILENAME,
    ExecutorLock,
    acquire,
    is_held,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; the guard has no Windows implementation",
)

#: Runs in a child process: take the lock, announce it by creating the
#: marker file, then stay alive until the parent kills us. The lock
#: lives in this process, so the parent's own attempts are the probe.
_HOLDER_SCRIPT = """
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
from executor_lock import acquire

plan_dir = sys.argv[2]
marker = sys.argv[3]

lock = acquire(plan_dir)
if lock is None:
    sys.stderr.write("holder failed to acquire the lock\\n")
    sys.exit(3)

with open(marker, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))

# Stay alive: the guard is only meaningful while this process runs.
while True:
    time.sleep(3600)
"""


def _spawn_holder(plan_dir: Path, marker: Path) -> subprocess.Popen:
    """Start a child process that holds the plan's lock."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_BACKEND_DIR), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER_SCRIPT,
            str(_BACKEND_DIR),
            str(plan_dir),
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _wait_for(marker: Path, timeout: float = 30.0) -> bool:
    """Wait until the marker file appears (the child holds the lock)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(0.05)
    return False


def _reap(proc: subprocess.Popen) -> None:
    """Make sure the child is gone — a leaked holder would hold the lock
    for every later test in this file."""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill always wins
        pass


def test_acquire_then_release(tmp_path):
    """The happy path: one acquire, one release, and the plan is free."""
    plan_dir = tmp_path / "plan"

    lock = acquire(plan_dir)
    assert isinstance(lock, ExecutorLock)
    assert lock.path == (plan_dir / LOCK_FILENAME).resolve()
    assert (plan_dir / LOCK_FILENAME).exists()
    assert is_held(plan_dir) is True

    lock.release()

    assert is_held(plan_dir) is False
    # Releasing does not delete the lock file: the flock lives on the
    # inode, so an unlinked path would let a second holder through.
    assert (plan_dir / LOCK_FILENAME).exists()

    # And the plan can be taken again once released.
    again = acquire(plan_dir)
    assert again is not None
    again.release()


def test_second_acquire_in_same_process_returns_none(tmp_path):
    """The guard is not reentrant: one holder per process."""
    plan_dir = tmp_path / "plan"

    lock = acquire(plan_dir)
    try:
        assert acquire(plan_dir) is None
        assert acquire(plan_dir) is None
        # The loser did not take the lock and did not steal it.
        assert is_held(plan_dir) is True
    finally:
        lock.release()

    # Release frees it for this process too — the guard is about
    # concurrent holders, not about a once-per-process lock.
    again = acquire(plan_dir)
    assert again is not None
    again.release()


def test_lock_is_held_across_processes(tmp_path):
    """A child process holding the lock makes the parent's acquire fail."""
    plan_dir = tmp_path / "plan"
    marker = tmp_path / "holder.pid"

    proc = _spawn_holder(plan_dir, marker)
    try:
        assert _wait_for(marker), "child never took the lock"
        assert marker.read_text(encoding="utf-8").strip() == str(proc.pid)

        # The parent is a different process: the kernel refuses it, at
        # once, without any waiting.
        assert acquire(plan_dir) is None
        assert is_held(plan_dir) is True
    finally:
        _reap(proc)

    # Once the holder is gone the lock is free again.
    assert is_held(plan_dir) is False
    lock = acquire(plan_dir)
    assert lock is not None
    lock.release()


def test_lock_released_when_holder_dies(tmp_path):
    """The lock is bound to the process, not to a cleanup handler.

    A holder killed with SIGKILL never runs any cleanup — and the kernel
    frees the lock anyway. That is the whole reason the guard is an
    ``flock`` and not a pid file with a liveness check.
    """
    plan_dir = tmp_path / "plan"
    marker = tmp_path / "holder.pid"

    proc = _spawn_holder(plan_dir, marker)
    try:
        assert _wait_for(marker), "child never took the lock"
        assert acquire(plan_dir) is None

        proc.kill()
        proc.wait(timeout=10)

        deadline = time.monotonic() + 10.0
        lock = None
        while time.monotonic() < deadline:
            lock = acquire(plan_dir)
            if lock is not None:
                break
            time.sleep(0.05)
        assert lock is not None, "the kernel did not release the dead holder's lock"
        lock.release()
    finally:
        _reap(proc)


def test_lock_file_records_holder_pid(tmp_path):
    """The holder's pid is in the lock file, so a log reader can attribute
    a stuck plan to a process instead of guessing."""
    plan_dir = tmp_path / "plan"

    lock = acquire(plan_dir)
    try:
        assert lock.pid == os.getpid()
        content = (plan_dir / LOCK_FILENAME).read_text(encoding="utf-8")
        assert str(os.getpid()) in content
    finally:
        lock.release()


def test_release_is_idempotent(tmp_path):
    """Releasing twice is not an error — the holder may not know whether
    the lock was already released by an earlier cleanup path."""
    plan_dir = tmp_path / "plan"

    lock = acquire(plan_dir)
    lock.release()
    lock.release()

    assert is_held(plan_dir) is False
    again = acquire(plan_dir)
    assert again is not None
    again.release()