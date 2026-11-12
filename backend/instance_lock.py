"""One PDT server per data root — a start-up level instance lock.

The problem this exists for
---------------------------
A PDT server owns a *data root*: the plans tree it writes artifacts into
and the SQLite state database it records progress in.  Two servers on the
same data root do not merely duplicate each other — they interleave
writes into the same plan directories, fight over the same state rows,
and each one's shutdown sweep reaps the other's children.  The failure
shows up as "a plan that is running and not running", which is far more
expensive to diagnose than to prevent.

This module is the guard: two ``flock`` locks taken at start-up, one on
``<plans_dir>/.pdt-instance.lock`` and one on
``<state_db_path>.instance.lock``.  Both are taken before the server
does anything else, and both are released after the shutdown sweep has
finished.

Why ``flock`` and not a pid file
--------------------------------
A pid file answers "who holds this?" by asking the operating system
whether a pid is alive, and that question has a bad failure mode: the
answer is only as fresh as the last time somebody looked, and the
process that has to look is the one deciding whether to start.  Between
"the holder died" and "the holder was killed" there is a window in which
a stale pid file either blocks a root forever (nobody reaps it) or
admits a second server (somebody reaps it too eagerly).  Both failure
modes are silent, and both are expensive.

``flock`` has neither problem.  The kernel owns the lock: it is released
when the holder's file descriptor is closed, and the kernel closes that
descriptor when the process dies — for any reason, including ``SIGKILL``
that leaves no chance to run a cleanup handler.  So "the holder died" is
not a state this guard has to detect or repair.  It is the *absence* of a
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
locks this process holds.  A second :func:`acquire_instance_locks` for
the same root raises :class:`InstanceLockHeld` on every platform, and
the holder it names is *this* process.

What the lock file contains
---------------------------
Only diagnostics: the holder's pid, start time, working directory, the
two roots it owns, the project directory and its argv.  **The decision
to refuse is made by ``flock`` alone** — the file's content is never
consulted to decide anything.  A file left behind by a process that was
``SIGKILL``ed therefore blocks nobody: the kernel released the lock
when the process died, and the next acquirer simply overwrites the
diagnostics with its own.

Contract
--------
* :func:`acquire_instance_locks` returns an :class:`InstanceLocks`, or
  raises :class:`InstanceLockHeld` when the data root is already taken.
* :meth:`InstanceLocks.release` unlocks and closes; it never unlinks,
  and it is idempotent.
* :func:`instance_lock_paths` is the one place that knows where the two
  lock files live.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

__all__ = [
    "InstanceLockHeld",
    "InstanceLocks",
    "acquire_instance_locks",
    "instance_lock_paths",
]

#: The lock file inside the plans tree.
PLANS_LOCK_FILENAME = ".pdt-instance.lock"

#: The suffix appended to the state database's *name* — the lock sits
#: next to ``state.db`` rather than inside it (a directory cannot be
#: locked, and the database file itself must stay a plain SQLite file).
STATE_LOCK_SUFFIX = ".instance.lock"

#: The environment variable that says how long a refused start waits
#: before it gives up.
WAIT_SEC_ENV = "PDT_INSTANCE_LOCK_WAIT_SEC"

#: The wait used when the environment does not say otherwise.
DEFAULT_WAIT_SEC = 20.0

#: How often a contended acquire retries while it waits.
RETRY_INTERVAL_SEC = 0.2

#: The message appended to every refusal, so the operator is told what
#: to do about it and not only that it happened.
_REFUSAL_ADVICE = (
    "另一个 PDT server 正在使用这个数据根；先停掉它，"
    "或给本实例换一个自己的根（PDT_PLANS_DIR / PDT_STATE_DB_PATH）。"
)

# Serialises the registry below, so a release and a concurrent acquire
# cannot interleave into a window where both think they hold the lock.
_REGISTRY_LOCK = threading.Lock()

# Resolved lock path -> the :class:`InstanceLocks` this process holds.
# The key is the *resolved* path so that two spellings of the same root
# (``/tmp/x`` and ``/private/tmp/x``) are one lock.
_HELD: Dict[str, "InstanceLocks"] = {}


class InstanceLockHeld(RuntimeError):
    """Raised when the data root is already owned by another server.

    The message names the holder (pid, start time, working directory)
    so the operator can find the process that has to be stopped, and it
    names the two roots so the operator can see *which* root is busy.
    """


def instance_lock_paths(
    plans_dir: Path, state_db_path: Path
) -> Tuple[Path, Path]:
    """Return the two lock files for a data root.

    1. ``<plans_dir>/.pdt-instance.lock``
    2. ``<state_db_path>.instance.lock`` — next to the database, e.g.
       ``.pdt/state.db.instance.lock``.

    The paths are built from the arguments as given (only ``~`` is
    expanded); the *resolved* form is used as the in-process registry
    key, so two spellings of one root are one lock there.  Two spellings
    of one root also name one file on disk, so the kernel's ``flock``
    arbitrates them the same way it arbitrates two processes.
    """
    plans_path = Path(plans_dir).expanduser() / PLANS_LOCK_FILENAME
    state_path = Path(state_db_path).expanduser()
    state_path = state_path.with_name(state_path.name + STATE_LOCK_SUFFIX)
    return plans_path, state_path


def acquire_instance_locks(
    plans_dir: Path,
    state_db_path: Path,
    *,
    wait_sec: Optional[float] = None,
) -> "InstanceLocks":
    """Take both instance locks for a data root.

    Returns an :class:`InstanceLocks` holding two locked file
    descriptors.  Raises :class:`InstanceLockHeld` when the root is
    already taken — by this process or by another one.

    ``wait_sec`` says how long a contended acquire keeps trying before
    it gives up.  ``None`` means "ask the environment"
    (:data:`WAIT_SEC_ENV`); an unset or unparsable value means
    :data:`DEFAULT_WAIT_SEC`.  ``wait_sec=0`` refuses immediately.

    The wait exists for the restart case — a server that is being
    replaced by another one on the same root — and not for the
    "two servers on one root" case, which is a configuration mistake
    that should be reported rather than waited out.
    """
    wait = _resolve_wait_sec(wait_sec)
    plans_path, state_path = instance_lock_paths(plans_dir, state_db_path)
    plans_key = _registry_key(plans_path)
    state_key = _registry_key(state_path)

    # The in-process rule, checked before anything touches the disk:
    # one holder per root per process, on every platform, rather than on
    # the kernel's rules about two file descriptions in one process.
    with _REGISTRY_LOCK:
        for key in (plans_key, state_key):
            held = _HELD.get(key)
            if held is not None:
                raise InstanceLockHeld(
                    _refusal_message(plans_dir, state_db_path, held.holder)
                )

    locks = InstanceLocks(plans_path, state_path, plans_key, state_key)
    # The diagnostics are built *before* the locks are taken, so a
    # refusal that happens while this process is still registering can
    # already name this process as the holder.
    locks.holder = _holder_payload(plans_dir, state_db_path)
    try:
        locks.plans_file = _lock_one(
            plans_path, wait, plans_dir, state_db_path
        )
        with _REGISTRY_LOCK:
            _HELD[plans_key] = locks
        locks.state_file = _lock_one(
            state_path, wait, plans_dir, state_db_path
        )
        with _REGISTRY_LOCK:
            _HELD[state_key] = locks
    except BaseException:
        # Nothing half-held survives a refusal: the plans lock is
        # released, and the registry forgets the root.
        locks.release()
        raise

    _write_holder(locks.plans_file, locks.holder)
    # The state lock file gets the same diagnostics, so a refusal on
    # *that* lock can name its holder too — the file content is only
    # ever read for diagnostics, never to decide anything.
    _write_holder(locks.state_file, locks.holder)
    return locks


class InstanceLocks:
    """The two locks one PDT server holds on its data root.

    The locks live on the *open file descriptions* the two file objects
    own, so the descriptors must not be closed behind their back —
    that would release the locks early.  :meth:`release` does both, in
    the right order, and is safe to call more than once.
    """

    def __init__(
        self,
        plans_path: Path,
        state_path: Path,
        plans_key: str,
        state_key: str,
    ) -> None:
        self.plans_path = plans_path
        self.state_path = state_path
        self.plans_key = plans_key
        self.state_key = state_key
        #: The diagnostics written into the lock files.
        self.holder: Dict[str, object] = {}
        self.plans_file = None
        self.state_file = None
        self._released = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<InstanceLocks plans={self.plans_path} "
            f"state={self.state_path} pid={os.getpid()}>"
        )

    @property
    def released(self) -> bool:
        """Whether :meth:`release` has already run."""
        return self._released

    def release(self) -> None:
        """Release both locks. Idempotent.

        The lock files are **not** unlinked — see the module docstring:
        the lock lives on the inode, so deleting the file would leave
        the holder locking something nobody can reach and let the next
        acquirer create a fresh inode and lock that instead.
        """
        with _REGISTRY_LOCK:
            if self._released:
                return
            self._released = True
            for handle in (self.plans_file, self.state_file):
                if handle is None:
                    continue
                try:
                    fd = handle.fileno()
                except (OSError, ValueError):
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    # Closing the descriptor below releases the lock
                    # anyway, so a failed unlock is not a reason to keep
                    # the fd.
                    pass
                try:
                    handle.close()
                except OSError:
                    pass
            self.plans_file = None
            self.state_file = None
            _HELD.pop(self.plans_key, None)
            _HELD.pop(self.state_key, None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _registry_key(path: Path) -> str:
    """The in-process registry key for a lock path."""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:  # pragma: no cover - defensive
        return str(Path(path).expanduser())


def _resolve_wait_sec(wait_sec: Optional[float]) -> float:
    """The number of seconds a contended acquire keeps trying."""
    if wait_sec is not None:
        try:
            value = float(wait_sec)
        except (TypeError, ValueError):
            return DEFAULT_WAIT_SEC
        if not math.isfinite(value) or value < 0:
            return DEFAULT_WAIT_SEC
        return value
    raw = os.environ.get(WAIT_SEC_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WAIT_SEC
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SEC
    if not math.isfinite(value) or value < 0:
        return DEFAULT_WAIT_SEC
    return value


def _lock_one(
    path: Path,
    wait: float,
    plans_dir: Path,
    state_db_path: Path,
):
    """Take the ``flock`` on one lock file, waiting up to ``wait`` seconds.

    Raises :class:`InstanceLockHeld` when the wait runs out; the
    message names the holder read out of the lock file (best effort —
    a file that cannot be parsed is not an error, it only means the
    holder cannot be described).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        handle = None
        try:
            handle = open(path, "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if exc.errno not in (
                errno.EAGAIN,
                errno.EWOULDBLOCK,
                errno.EACCES,
            ):
                raise
            # Somebody else holds it.  The kernel refuses immediately —
            # that is the entire point of this guard; the wait below is
            # only for the restart case.
            if time.monotonic() >= deadline:
                raise InstanceLockHeld(
                    _refusal_message(
                        plans_dir, state_db_path, _read_holder(path)
                    )
                ) from exc
            time.sleep(RETRY_INTERVAL_SEC)
            continue
        return handle


def _holder_payload(
    plans_dir: Path, state_db_path: Path
) -> Dict[str, object]:
    """The diagnostics written into the lock files."""
    return {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cwd": os.getcwd(),
        "plans_dir": str(Path(plans_dir).expanduser().resolve()),
        "state_db": str(Path(state_db_path).expanduser().resolve()),
        "project_dir": _project_dir(),
        "argv": list(sys.argv),
    }


def _project_dir() -> Optional[str]:
    """The repository root this server serves, if it can be told."""
    try:
        from config_paths import PROJECT_ROOT
    except Exception:  # pragma: no cover - config_paths is a leaf
        return None
    return str(PROJECT_ROOT)


def _write_holder(handle, holder: Dict[str, object]) -> None:
    """Record the holder's diagnostics in a lock file.

    Purely for attribution: an operator looking at a refused start can
    see which process took the root, instead of inferring it from
    timestamps.  A failure to write must not lose the lock — the lock
    is the guard, the content is a convenience.
    """
    if handle is None:
        return
    try:
        payload = json.dumps(holder, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return
    try:
        handle.truncate(0)
        handle.seek(0)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    except (OSError, ValueError):
        pass


def _read_holder(path: Path) -> Dict[str, object]:
    """Read a lock file's diagnostics. Best effort.

    A file that is empty, truncated, or not JSON at all yields an empty
    mapping: the content is never used to *decide* anything, so failing
    to parse it must not turn into a second failure.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _refusal_message(
    plans_dir: Path, state_db_path: Path, holder: Dict[str, object]
) -> str:
    """The message every refusal carries.

    The first line names the roots and the holder; the second says what
    the operator can do about it.
    """
    holder = holder or {}
    return (
        "instance lock refused: "
        f"plans_dir={Path(plans_dir).expanduser().resolve()} "
        f"state_db={Path(state_db_path).expanduser().resolve()} "
        f"holder_pid={holder.get('pid', 'unknown')} "
        f"holder_started_at={holder.get('started_at', 'unknown')} "
        f"holder_cwd={holder.get('cwd', 'unknown')}\n"
        f"{_REFUSAL_ADVICE}"
    )
