"""One executor per plan — a single-instance guard bound to the process.

The problem this exists for
---------------------------
Two executors on the same plan contend for the same file locks, and the
loser does not lose *fast*: it waits out the full acquire timeout (300 s
by default) before it discovers the plan is already being worked on. Two
executors on one plan is a configuration mistake, and a mistake that
costs five minutes of wall clock every time it happens is worth turning
into a microsecond-scale "no".

This module is that guard: one ``flock`` on ``<plan_dir>/executor.lock``.

Why ``flock`` and not a pid file
--------------------------------
A pid file answers "who holds this?" by asking the operating system
whether a pid is alive, and that question has a bad failure mode: the
answer is only as fresh as the last time somebody looked, and the
process that has to look is the one deciding whether to start. Between
"the holder died" and "the holder was killed" there is a window in which
a stale pid file either blocks a plan forever (nobody reaps it) or
admits a second executor (somebody reaps it too eagerly). Both failure
modes are silent, and both are expensive — a plan that runs twice, or a
plan that never runs again.

``flock`` has neither problem. The kernel owns the lock: it is released
when the holder's file descriptor is closed, and the kernel closes that
descriptor when the process dies — for any reason, including ``SIGKILL``
that leaves no chance to run a cleanup handler. So "the holder died" is
not a state this guard has to detect or repair. It is the *absence* of a
holder, and it is true the instant the process is gone.

Two consequences are load-bearing and are pinned by the tests:

  * **The loser finds out immediately.** ``LOCK_NB`` makes a contended
    acquire return at once with ``EWOULDBLOCK``. There is no wait and no
    poll: this guard is a gate, not a queue.
  * **The lock file is never deleted.** The lock lives on the *inode*,
    not on the path. Unlinking the file while it is held leaves the
    holder locking something nobody can reach, and the next acquirer
    creates a fresh inode and locks that instead — two holders, both
    convinced they are alone. So ``release`` unlocks and closes; it
    never unlinks.

The one thing ``flock`` does not give for free
----------------------------------------------
``flock`` conflicts between two *open file descriptions*, and two
separate ``open()`` calls in the same process are two descriptions. But
whether the second ``open()`` in one process is refused by the first
one's lock is not the same on every platform (Linux refuses it, the BSD
flavours have their own rules), and a guard whose "one holder" rule
depends on the kernel's mood is a guard that is one platform away from
being wrong.

So the rule is pinned here instead, in an in-process registry of the
locks this process holds. A second :func:`acquire` of the same plan
returns ``None`` on every platform, and :func:`is_held` answers for the
whole machine rather than for one process.

Contract
--------
* :func:`acquire` returns an :class:`ExecutorLock`, or ``None`` if the
  lock is taken. It never raises for contention and never waits.
* :func:`is_held` reports whether the lock is held *by anyone* — this
  process or another.
* :meth:`ExecutorLock.release` is idempotent and never raises: it is
  called from ``finally`` blocks, where a failure must not mask the
  real outcome.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional

try:  # POSIX only. Elsewhere there is no guard, and acquire says so.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = ["LOCK_FILENAME", "ExecutorLock", "acquire", "is_held"]

#: The lock file's name inside a plan directory.
LOCK_FILENAME = "executor.lock"

#: Serialises the registry below, so a release and a concurrent acquire
#: cannot interleave into a window where both think they hold the lock.
_REGISTRY_LOCK = threading.Lock()

#: Resolved lock path -> the :class:`ExecutorLock` this process holds.
#: The key is the *resolved* path so that two spellings of the same
#: plan directory (``/tmp/x`` and ``/private/tmp/x``) are one lock.
_HELD: Dict[str, "ExecutorLock"] = {}


def _lock_path(plan_dir) -> Path:
    """The path of the lock file for ``plan_dir``."""
    return Path(plan_dir).expanduser().resolve() / LOCK_FILENAME


class ExecutorLock:
    """A held lock on ``<plan_dir>/executor.lock``.

    The lock is held for as long as this object holds the file
    descriptor, so the descriptor must not be closed behind its back —
    that would release the lock early. :meth:`release` does both, in
    the right order, and is safe to call more than once.
    """

    def __init__(self, path: Path, fd: int, pid: int) -> None:
        self.path = path
        #: The pid that holds the lock. Also written into the lock file.
        self.pid = pid
        self._fd: Optional[int] = fd

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ExecutorLock {self.path} pid={self.pid}>"

    def release(self) -> None:
        """Release the lock. Idempotent, and never raises.

        Called from ``finally`` blocks, so a failure here must not mask
        the outcome the caller is actually reporting.
        """
        with _REGISTRY_LOCK:
            fd = self._fd
            if fd is None:
                return
            self._fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor below releases the lock anyway,
                # so a failed unlock is not a reason to keep the fd.
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            _HELD.pop(str(self.path), None)


def acquire(plan_dir) -> Optional[ExecutorLock]:
    """Take the single-instance lock for ``plan_dir``.

    Returns the :class:`ExecutorLock` on success, or ``None`` when the
    lock is already held — by this process or by another. There is no
    waiting and no polling: a contended acquire returns immediately, so
    the caller can go and do something else instead of burning the
    timeout the file-lock layer would have burned.

    Never raises for contention or for an I/O failure on the lock file:
    the return type has no error channel, and a guard that fails open
    (letting a second executor in) would be worse than one that fails
    closed (refusing to start). A malformed ``plan_dir`` — a programming
    error rather than contention — is allowed to raise.
    """
    if fcntl is None:  # pragma: no cover - Windows
        return None

    path = _lock_path(plan_dir)
    key = str(path)

    with _REGISTRY_LOCK:
        # Not reentrant: one holder per plan per process, on every
        # platform, rather than on the kernel's rules about two file
        # descriptions in one process.
        if key in _HELD:
            return None

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return None

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # EWOULDBLOCK: somebody else holds it. The kernel refuses
            # immediately — that is the entire point of this guard.
            try:
                os.close(fd)
            except OSError:
                pass
            return None

        lock = ExecutorLock(path, fd, os.getpid())
        _HELD[key] = lock
        _write_holder(fd, os.getpid())
        return lock


def is_held(plan_dir) -> bool:
    """Whether the lock for ``plan_dir`` is held by anyone.

    This is a query about the whole machine, not about this process: it
    answers ``True`` when another executor holds the plan.

    There is no read-only "is this locked?" call in POSIX ``flock``, so
    the answer is obtained by trying to take the lock and giving it back
    at once. That window is a couple of syscalls wide; a concurrent
    acquirer that loses it is told the lock is busy, which is the
    conservative answer.
    """
    if fcntl is None:  # pragma: no cover - Windows
        return False

    path = _lock_path(plan_dir)
    key = str(path)

    with _REGISTRY_LOCK:
        if key in _HELD:
            return True

    try:
        # No O_CREAT: a query must not create the state it asks about.
        fd = os.open(str(path), os.O_RDWR)
    except FileNotFoundError:
        # No lock file means nobody holds the lock.
        return False
    except OSError:
        # Cannot probe it (not readable, for instance). Report "not held"
        # rather than guess: ``acquire`` is the gate that decides, and it
        # fails closed.
        return False

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _write_holder(fd: int, pid: int) -> None:
    """Record the holder's pid in the lock file.

    Purely for attribution: a log reader looking at a stuck plan can see
    which process took it, instead of inferring it from timestamps. A
    failure to write must not lose the lock — the lock is the guard, the
    content is a convenience.
    """
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid={pid}\n".encode("utf-8"))
    except OSError:
        pass