"""The executor-side file-lock broker.

The problem this exists for
---------------------------
The Edit/Write hook is a **short-lived child process** of the sub-agent.
An ``flock`` it takes is released by the kernel the moment that process
exits — which happens before the edit it was guarding even runs. So the
hook cannot be the lock holder, no matter how the hook is written; the
lock has to outlive a single tool call.

The executor, by contrast, is long-lived and already holds OS locks for
its tasks (``FileLockManager``). It is therefore the natural owner: the
broker runs inside it, owns the real ``flock``s, and the hook degrades
into a thin client that asks *"may I write this file, and if not, wait
for me"* over a UNIX socket. Because the locks are kernel-held by a
process, the guarantee is self-healing: if the executor dies, the kernel
releases everything. There is no stale-lock reaper to get wrong.

Second problem it solves: the lock set was previously fixed at task
start from the task's *declared* files. Anything the sub-agent decided
to touch later — the common case when the plan could not anticipate the
change — was unguarded. Here the broker accepts acquisitions at any
point during a task and keys them to the task id, so a file acquired
mid-task is held for the rest of the task (covering the whole
read-then-edit window rather than one tool call) and released with the
declared set when the task ends.

Ordering
--------
Waiters for a key are served **first-come-first-served** through an
explicit queue, not by retry races: a file contended by several
sub-agents is modified in the order they asked, so a task that has been
waiting cannot be starved by one that just arrived.

Cross-process holders are handled too. ``filelock`` conflicts are
per open file description, so a *different* process holding the same
lock file blocks this broker's ``FileLock.acquire``; the waiting task
keeps its place at the head of the queue and polls until its deadline.

Known limitation
----------------
A task that holds key A and waits for key B while another task holds B
and waits for A will deadlock until the acquire timeout expires. Batch
acquisition at task start is sorted (``FileLockManager`` does this) to
keep the common path ordered; incremental mid-task acquisition cannot be
ordered in general. The timeout is the backstop, and the executor treats
an unacquirable declared file as a task failure rather than hanging.

Why this holds raw ``flock`` instead of reusing ``filelock``
-----------------------------------------------------------
``filelock`` was the obvious choice — it is already a dependency and
``FileLockManager`` is built on it — and it is wrong here for a reason
that does not announce itself. In ``filelock`` 4.x a lock is owned by
the **thread** that acquired it: calling ``release()`` from a different
thread returns successfully and does *not* free the lock. Measured
directly: acquire in thread A, release from the main thread, then try to
acquire from thread B — ``release()`` reports OK and thread B times out
anyway.

In this broker the mismatch is structural rather than accidental. Each
request is served on its own connection thread, so a lock is always
acquired on that thread; the executor later releases it from the task's
own thread (``_post_task_lock_hook``, in a ``finally``). Every release
would cross threads, every release would silently no-op, and the file
would stay locked until the executor exited. A test caught it; nothing
in production would have.

``fcntl.flock`` has no such notion: it is per *open file description*,
so any thread may release what any other thread acquired. It is also
the same underlying primitive ``filelock`` uses on POSIX, so a
``FileLockManager`` in another process still contends with these locks
correctly.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:  # POSIX only. Elsewhere the broker reports itself unavailable.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from file_lock_protocol import (
    ACQUIRED,
    ALREADY_HELD,
    TIMED_OUT,
    UNAVAILABLE,
    LockBrokerUnavailable,
    lock_file_path,
    normalize_target,
    request,
    socket_path,
)

#: Default wait when a caller does not name one. Long enough to absorb a
#: busy neighbouring task, short enough that a genuine deadlock surfaces
#: as a failed task inside the executor's own task timeout.
DEFAULT_ACQUIRE_TIMEOUT = 300.0

#: Re-check interval while queued. The condition variable is also
#: notified on every release, so this is only a backstop against a
#: missed wake-up — it does not set the latency, and it is small enough
#: that "queue drain" feels immediate.
_TURN_POLL_SECONDS = 0.25

#: How long a single non-blocking ``flock`` attempt waits before the
#: loop re-checks its own deadline. Short so a cross-process holder does
#: not make us overrun the caller's timeout.
_OS_LOCK_PROBE_SECONDS = 1.0

#: How long the serving thread blocks in ``accept()`` before looping to
#: re-read its stop flag. This is the ONLY thing that ends the thread on
#: Linux, where closing the listening socket does not wake a thread
#: already blocked in ``accept()`` — see the ``settimeout`` call in
#: :meth:`FileLockBroker.start` for the full failure this prevents. It
#: sets the shutdown latency, so it is short; it is not a busy-wait,
#: because the thread spends all but this fraction of its life asleep.
_ACCEPT_POLL_SECONDS = 0.2


def _default_emit(event: str, data: Dict[str, Any]) -> None:  # pragma: no cover
    """No-op sink used when the caller supplies no logger."""


class FileLockBroker:
    """Owns OS file locks on behalf of sub-agents, with a FIFO queue."""

    def __init__(
        self,
        project_dir,
        locks_dir,
        *,
        default_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
        emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        adopting: bool = True,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        #: Where this plan's lock files go. Required rather than derived:
        #: the derivation depends on whether a plan directory exists, and
        #: only the caller (``AutonomousAgent``, which holds the tasks
        #: file) knows that. A wrong guess here would silently put two
        #: processes on different lock files for one target.
        self._locks_dir = Path(locks_dir)
        self.default_timeout = float(default_timeout)
        self._emit = emit or _default_emit
        self._adopting = adopting

        self._cond = threading.Condition()
        #: task id -> {canonical key: open fd holding the flock}
        self._held: Dict[str, Dict[str, int]] = {}
        #: canonical key -> FIFO queue of task ids waiting for it
        self._waiting: Dict[str, List[str]] = {}

        self._sock_path = socket_path(self.project_dir)
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: True when we bound the socket ourselves; an adopted broker is
        #: owned by another process and must not be unlinked by us.
        self._owns_socket = False
        self.available = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> Optional[str]:
        """Bind the socket and serve in a daemon thread.

        Returns the socket path, or ``None`` when an existing live broker
        was adopted instead. Adoption matters because two executors can
        legitimately point at the same project dir (a restarted run whose
        predecessor is still draining): the second must join the first
        rather than unlink its socket and start a rival that hands out
        overlapping locks for the same files.
        """
        if self.available:
            return str(self._sock_path)

        if fcntl is None:
            # No POSIX advisory locking on this platform. Report the
            # broker as unavailable rather than serving a half-guarantee:
            # callers treat "unavailable" as fail-open, which is the
            # honest outcome when the mechanism does not exist.
            self._emit("file_lock_broker_unavailable", {"reason": "no_fcntl"})
            return None

        if self._adopting and self._live_broker_at(self._sock_path):
            # Another process owns this project's locks. Joining it means
            # our tasks' acquisitions are arbitrated against its tasks'
            # too, which is the point — a per-process broker would only
            # serialise tasks inside one executor.
            self.available = True
            self._emit("file_lock_broker_adopted", {"socket": str(self._sock_path)})
            return None

        self._sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self._sock_path.exists():
            # No live broker answered, so this is a leftover from a dead
            # executor. Binding would fail with EADDRINUSE; the OS does
            # not clean up socket files the way it releases flocks.
            try:
                self._sock_path.unlink()
            except OSError:
                pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._sock_path))
        server.listen(64)
        # The serving thread blocks in ``accept()``, and ``stop()`` needs a
        # way to make it return. Closing the socket is NOT that way: on
        # Linux a thread already blocked in ``accept()`` keeps the file
        # description alive through the pending syscall, so ``close()``
        # does not wake it and ``_serve`` never sees an ``OSError``. The
        # thread then outlives its test — the conftest leak guard reports
        # ``pdt-lock-broker`` as still alive after a 10s join, which is
        # exactly what CI reported on the ``unit`` lane.
        #
        # macOS does wake it (the blocked ``accept`` fails ECONNABORTED), so
        # this is invisible on a developer machine and reproducible only on
        # the Linux runner. Hence the poll: a short accept timeout turns
        # "is it time to stop?" into something the loop can answer without
        # depending on a platform-specific socket teardown. ``accept()``
        # returns its socket in blocking mode regardless of this setting
        # (Python 3.7+), so the per-connection handlers are unaffected.
        server.settimeout(_ACCEPT_POLL_SECONDS)
        # The per-user temp dir is shared with every other process the same
        # user runs; 0600 keeps a stray process from connecting and
        # acquiring locks on this checkout's behalf.
        try:
            os.chmod(str(self._sock_path), 0o600)
        except OSError:
            pass
        self._server = server
        self._owns_socket = True
        self._thread = threading.Thread(
            target=self._serve, name="pdt-lock-broker", daemon=True
        )
        self._thread.start()
        self.available = True
        self._emit("file_lock_broker_started", {"socket": str(self._sock_path)})
        return str(self._sock_path)

    def stop(self) -> None:
        """Release every lock and tear the socket down.

        Called on the executor's exit path. The locks would be released
        by the kernel regardless; doing it explicitly keeps the ordering
        deterministic for tests and leaves no half-closed connections.
        """
        self._stop.set()
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self._server = None

        with self._cond:
            for task_id in list(self._held):
                self._release_locked(task_id, None)
            self._cond.notify_all()

        if self._owns_socket:
            try:
                self._sock_path.unlink()
            except OSError:
                pass
        self.available = False

    @staticmethod
    def _live_broker_at(path: Path) -> bool:
        """True when something at ``path`` answers a ``ping``."""
        if not path.exists():
            return False
        try:
            reply = request(path, {"op": "ping"}, timeout=2.0)
        except LockBrokerUnavailable:
            return False
        return bool(reply.get("ok"))

    # ------------------------------------------------------------------
    # serving
    # ------------------------------------------------------------------
    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                # The poll interval, not an error: loop round so the
                # ``_stop`` check above is re-evaluated.
                continue
            except OSError:
                # Socket closed by stop(), or the OS reclaimed it.
                return
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        """Serve one connection. One request per line, replies inline."""
        try:
            with conn:
                stream = conn.makefile("rwb")
                for raw in stream:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        req = json.loads(raw)
                    except ValueError:
                        reply: Dict[str, Any] = {"ok": False, "reason": "bad_json"}
                    else:
                        reply = self._dispatch(req)
                    stream.write((json.dumps(reply) + "\n").encode("utf-8"))
                    stream.flush()
        except OSError:
            # Client vanished mid-conversation (hook killed, sub-agent
            # cancelled). Its task still owns whatever it acquired, and
            # the executor releases on the task's finally path, so there
            # is nothing to unwind here.
            pass

    def _dispatch(self, req: Dict[str, Any]) -> Dict[str, Any]:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "snapshot":
            with self._cond:
                return {
                    "ok": True,
                    "held": {t: sorted(k) for t, k in self._held.items()},
                    "waiting": {k: list(v) for k, v in self._waiting.items()},
                }
        task_id = req.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return {"ok": False, "reason": "missing_task_id"}

        if op == "acquire":
            target = req.get("path")
            if not isinstance(target, str) or not target:
                return {"ok": False, "reason": "missing_path"}
            timeout = req.get("timeout")
            timeout = self.default_timeout if timeout is None else float(timeout)
            state = self.acquire(task_id, target, timeout)
            if state == TIMED_OUT:
                return {"ok": False, "reason": TIMED_OUT}
            if state == UNAVAILABLE:
                return {"ok": False, "reason": UNAVAILABLE}
            return {"ok": True, "state": state}

        if op == "release":
            paths = req.get("paths")
            return {"ok": True, "released": self.release(task_id, paths)}

        return {"ok": False, "reason": f"unknown_op:{op}"}

    # ------------------------------------------------------------------
    # lock operations
    # ------------------------------------------------------------------
    def acquire(self, task_id: str, target, timeout: Optional[float] = None) -> str:
        """Acquire ``target`` for ``task_id``, waiting up to ``timeout``.

        Returns :data:`ACQUIRED`, :data:`ALREADY_HELD` (this task already
        holds it — the mid-task re-edit case, which must not deadlock on
        its own lock), or :data:`TIMED_OUT`.
        """
        timeout = self.default_timeout if timeout is None else float(timeout)
        key = normalize_target(self.project_dir, target)
        deadline = time.monotonic() + timeout

        with self._cond:
            if key in self._held.get(task_id, {}):
                return ALREADY_HELD
            waiters = self._waiting.setdefault(key, [])
            if task_id not in waiters:
                waiters.append(task_id)
            waited = False
            while True:
                holder = self._holder_locked(key)
                if holder is None and waiters[0] == task_id:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._drop_waiter_locked(key, task_id)
                    self._cond.notify_all()
                    self._emit(
                        "file_lock_timeout",
                        {"task_id": task_id, "key": key,
                         "held_by": holder, "waited_s": round(timeout, 3)},
                    )
                    return TIMED_OUT
                if not waited:
                    waited = True
                    self._emit(
                        "file_lock_wait",
                        {"task_id": task_id, "key": key, "held_by": holder},
                    )
                # Notify-driven, with a poll backstop: a missed wake-up
                # would otherwise park the task until its deadline.
                self._cond.wait(min(remaining, _TURN_POLL_SECONDS))

        # Outside the condition: take the real OS lock. The broker's own
        # bookkeeping says the key is free, but another *process* may
        # still hold it — hence a bounded retry loop rather than a single
        # call, and hence keeping our place at the head of the queue
        # while we retry.
        path = lock_file_path(self._locks_dir, self.project_dir, key)
        fd: Optional[int] = None
        while True:
            try:
                fd = _flock_nonblocking(path)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_OS_LOCK_PROBE_SECONDS, remaining))
            except OSError as exc:
                # Cannot even open the lock file (permissions, missing
                # parent, fd exhaustion). That is an I/O failure, not
                # contention — reporting it as "timeout" would block an
                # edit that nobody is competing for.
                with self._cond:
                    self._drop_waiter_locked(key, task_id)
                    self._cond.notify_all()
                self._emit(
                    "file_lock_unavailable",
                    {"task_id": task_id, "key": key,
                     "error": f"{type(exc).__name__}: {exc}"},
                )
                return UNAVAILABLE

        with self._cond:
            self._drop_waiter_locked(key, task_id)
            if fd is not None:
                self._held.setdefault(task_id, {})[key] = fd
            self._cond.notify_all()
        if fd is None:
            self._emit(
                "file_lock_timeout",
                {"task_id": task_id, "key": key, "held_by": "other_process"},
            )
            return TIMED_OUT
        self._emit("file_lock_acquired", {"task_id": task_id, "key": key})
        return ACQUIRED

    def release(self, task_id: str, targets=None) -> int:
        """Release ``targets`` for ``task_id``; ``None`` releases all."""
        with self._cond:
            count = self._release_locked(task_id, targets)
            self._cond.notify_all()
            return count

    def held(self, task_id: str) -> List[str]:
        """Canonical keys currently held by ``task_id`` (sorted)."""
        with self._cond:
            return sorted(self._held.get(task_id, {}))

    def waiting(self, key: str) -> List[str]:
        """Task ids queued for an already-canonical ``key``."""
        with self._cond:
            return list(self._waiting.get(key, []))

    # ------------------------------------------------------------------
    # internals (all called with ``self._cond`` held)
    # ------------------------------------------------------------------
    def _holder_locked(self, key: str) -> Optional[str]:
        for task_id, locks in self._held.items():
            if key in locks:
                return task_id
        return None

    def _drop_waiter_locked(self, key: str, task_id: str) -> None:
        waiters = self._waiting.get(key)
        if not waiters:
            return
        try:
            waiters.remove(task_id)
        except ValueError:
            pass
        if not waiters:
            self._waiting.pop(key, None)

    def _release_locked(self, task_id: str, targets) -> int:
        locks = self._held.get(task_id)
        if not locks:
            return 0
        if targets is None:
            keys = list(locks)
        else:
            wanted = {normalize_target(self.project_dir, t) for t in targets}
            keys = [k for k in locks if k in wanted]
        for key in keys:
            fd = locks.pop(key)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Closing the fd below releases the lock regardless, so
                # a failed LOCK_UN is not a reason to keep the key
                # marked busy.
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            self._emit("file_lock_released", {"task_id": task_id, "key": key})
        if not locks:
            self._held.pop(task_id, None)
        return len(keys)


class BrokerTaskHandle:
    """Release handle for one task's locks, held by the executor.

    Exists so the broker path and the legacy ``FileLockManager`` path are
    interchangeable to the caller: ``_post_task_lock_hook`` only ever
    calls ``release()``, and this satisfies that contract while also
    releasing the files the sub-agent acquired *mid-task* — the set the
    executor cannot know at task start.
    """

    __slots__ = ("_broker", "_task_id")

    def __init__(self, broker: "FileLockBroker", task_id: str) -> None:
        self._broker = broker
        self._task_id = task_id

    @property
    def task_id(self) -> str:
        return self._task_id

    def keys(self) -> List[str]:
        return self._broker.held(self._task_id)

    def release(self) -> None:
        """Release everything this task holds. Never raises.

        Called from a ``finally``, so a release failure must not mask the
        task's real outcome — and the kernel frees the locks when the
        executor exits regardless.
        """
        try:
            self._broker.release(self._task_id)
        except Exception:
            pass


def _flock_nonblocking(path: Path) -> int:
    """Open ``path`` and take an exclusive, non-blocking ``flock``.

    Returns the file descriptor. The caller must keep it open for as long
    as the lock is held: closing the fd is what releases the lock, which
    is precisely the self-healing property the broker depends on when
    the executor dies.

    Raises ``BlockingIOError`` when another process (or another broker)
    holds the lock, and ``OSError`` when the lock file cannot be opened.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A flock lives on the *inode*, not on the path. If something
        # unlinked this file between our open and our flock — a temp
        # cleaner, an operator tidying up — we are now holding an orphan
        # that no later acquirer will ever consult, while both sides
        # believe they are exclusive. Cheaper to detect than to debug, so
        # compare the inode we locked against the one the path resolves
        # to now, and retry on a fresh inode if they differ.
        try:
            current = os.stat(path)
        except FileNotFoundError:
            current = None
        if current is None or os.fstat(fd).st_ino != current.st_ino:
            raise BlockingIOError(f"lock file replaced while acquiring: {path}")
    except OSError:
        os.close(fd)
        raise
    return fd
