"""The reader that outlives the backend — the executor log pipe's guardian.

Why this module exists
-----------------------
The executor's output reaches the plan directory's log through a pipe:
a pipe that is *drained*, so the log can be bounded (rotated) instead of
growing until the disk says stop.  That is the right shape for a log,
and it has one consequence that has to be answered: **a pipe needs a
reader, and the reader was the backend**.

A backend that dies takes its reader with it.  The pipe's write end then
has nobody on the other side, and the executor's next write fails with
``EPIPE`` — which, in a program that writes a heartbeat every hundred
milliseconds, means the executor is dead a moment after the backend is.
That is not a corner case: it is what a ``kill -9`` of the backend does
to every run that was working at the time, and it is the difference
between "the run went on and the new backend adopted it" and "the run
was interrupted".

So the pipe gets a reader that is not the backend.

What the guardian is
--------------------
A small process, started next to the executor, that

* **holds the pipe's read end open** — so the pipe never breaks, and
  the executor's writes never fail with ``EPIPE``;
* **takes over draining the pipe** when the backend that was draining
  it is gone — so the executor keeps running *and* keeps being logged,
  into the same bounded, rotating log.

The guardian does not read while the backend is reading.  Two readers on
one pipe would split the bytes between them, and the log would be a
mixture of two writers' halves; the guardian waits for the backend to
stop being the reader, and the backend's own pump owns the pipe until
then.

The contract
------------
* **The pipe is never left without a reader while the executor lives.**
  That is the whole point: a pipe whose read end is closed turns the
  executor's next write into an ``EPIPE``, and an executor that dies
  with the backend is a run that cannot be adopted.
* **The guardian never decides anything.**  It reads a pipe and writes
  a log.  It starts no process, it signals no process, and it holds no
  state that outlives the pipe it is guarding.
* **The guardian is not the executor.**  It is not in the executor's
  process group, it is not recorded in the process ledger, and it
  carries none of the ``PDT_SPAWN_*`` markers — so the orphan reaper,
  which only ever looks at marked processes, never sees it.
* **A guardian that cannot start is not a failure.**  The run goes on
  exactly as it did before the guardian existed; the executor simply
  loses the protection the guardian gives it.
* **The guardian ends with the run.**  When the backend's own
  ``wait()`` returns, the guardian is stopped: it has nothing left to
  guard, and a process that outlives its run is residue.

Usage
-----
::

    guardian = spawn_guardian(
        log_fd=process.stdout.fileno(),
        log_path=log_path,
        backend_dir=backend_dir,
        max_bytes=10 * 1024 * 1024,
        child_pid=process.pid,
    )
    ...
    guardian.stop()
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple


#: How often the guardian looks at the pipe it is guarding.
#:
#: The guardian's wait is not a poll in the usual sense — it blocks on
#: the release pipe — but the child liveness check is a poll, and a poll
#: that is too slow keeps a finished run's guardian alive longer than
#: it has any business being.
GUARDIAN_POLL_SECONDS = 0.2


#: The environment variables that must not reach the guardian.
#:
#: The ``PDT_SPAWN_*`` markers say "this process was started by the
#: application, and this is who started it".  The orphan reaper looks
#: for exactly that, and a guardian carrying the markers would be a
#: candidate for the reaper — a reaper that killed the guardian would
#: take the executor's reader away from it, which is the very failure
#: the guardian exists to prevent.
SPAWN_MARKER_ENV = (
    "PDT_SPAWN_OWNER_PID",
    "PDT_SPAWN_OWNER_START",
    "PDT_SPAWN_KIND",
    "PDT_SPAWN_REGISTRY",
)


#: The guardian's own source.
#:
#: It is run as ``python -c <source> <args...>``: a separate process,
#: started with the pipe's read end and the release pipe's read end
#: inherited, and nothing else.  A source string rather than a module
#: of its own, because the guardian has to run in a process that knows
#: nothing about the application it is guarding — it must not import
#: the backend, start a thread, or hold a database connection.
GUARDIAN_SOURCE = r'''
import os
import select
import sys
import time


def _alive(pid):
    """True when the pid still names a process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _released(release_fd):
    """True when the backend is no longer the reader of the pipe.

    The backend holds the write end of the release pipe.  When it dies,
    the pipe ends, and the guardian's read says so.  The backend also
    closes it when it stops reading for a reason of its own — the
    guardian takes the pipe over then too, because a pipe nobody drains
    is a pipe the executor blocks on forever.
    """
    try:
        ready, _, _ = select.select([release_fd], [], [], 0.0)
    except (OSError, ValueError):
        return True
    if not ready:
        return False
    try:
        return not os.read(release_fd, 1)
    except OSError:
        return True


class _LineStream(object):
    """A text stream whose ``read(n)`` hands back one line at a time."""

    def __init__(self, stream):
        self._stream = stream

    def read(self, size=-1):
        return self._stream.readline()


class _PlainSink(object):
    """The last-resort writer: the log without the bound.

    Used only when :mod:`bounded_log` cannot be imported.  A log without
    the bound is a defect; a dead executor is worse, and the guardian's
    job is to keep the pipe from breaking.
    """

    def __init__(self, path):
        self._path = path

    def write(self, text):
        if isinstance(text, (bytes, bytearray, memoryview)):
            payload = bytes(text)
        else:
            payload = str(text).encode("utf-8", "replace")
        with open(self._path, "ab") as handle:
            handle.write(payload)
        return len(payload)

    def close(self):
        pass


def _sink(log_path, max_bytes):
    """The bounded writer, or the plain one when the module is missing.

    Returns the sink and the pump that feeds it — the pump is ``None``
    when :mod:`bounded_log` could not be imported, and the drain falls
    back to its own loop.
    """
    try:
        from bounded_log import RotatingLogSink, pump_stream_to_sink
    except Exception:
        return _PlainSink(log_path), None
    return RotatingLogSink(log_path, max_bytes=max_bytes), pump_stream_to_sink


def _drain(stream, sink, pump):
    """Copy the pipe into the sink until it ends."""
    if pump is not None:
        pump(_LineStream(stream), sink)
        return
    while True:
        chunk = stream.readline()
        if not chunk:
            break
        try:
            sink.write(chunk)
        except Exception:
            pass


def _main(argv):
    log_path = argv[1]
    max_bytes = int(argv[2])
    if max_bytes <= 0:
        max_bytes = None
    backend_dir = argv[3]
    log_fd = int(argv[4])
    release_fd = int(argv[5])
    child_pid = int(argv[6])
    poll_seconds = float(argv[7]) if len(argv) > 7 else 0.2

    if backend_dir and backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    # The wait.  Two things end it: the backend letting go of the pipe
    # (the release pipe ends), or the run ending (the child is gone).
    # The second one is the ordinary end of a run — there is nothing
    # left to guard, and the backend's own pump has the rest of the
    # bytes.
    while True:
        if _released(release_fd):
            break
        if not _alive(child_pid):
            return
        time.sleep(poll_seconds)

    # The backend is gone: the pipe is ours now.  Drain it into the
    # same bounded log the backend was writing, so the executor keeps
    # running and keeps being logged.
    sink, pump = _sink(log_path, max_bytes)
    try:
        stream = os.fdopen(log_fd, "r", encoding="utf-8", errors="replace")
        _drain(stream, sink, pump)
    except Exception:
        pass
    finally:
        try:
            sink.close()
        except Exception:
            pass


if __name__ == "__main__":
    _main(sys.argv)
'''


def _close_quietly(fd: int) -> None:
    """Close a descriptor, ignoring a failure to do so.

    A descriptor that is already closed is not an error worth raising
    from a cleanup path: the caller is about to return ``None`` or a
    guardian, and the difference between "closed" and "never opened" is
    not one that changes what happens next.
    """
    try:
        os.close(fd)
    except OSError:
        pass


class LogGuardian:
    """The process that holds the log pipe open when the backend is gone.

    Attributes:
        pid: the guardian's own pid, or ``0`` when there is no guardian.
    """

    def __init__(
        self,
        process: Optional[subprocess.Popen],
        release_write: Optional[int],
    ) -> None:
        self._process = process
        self._release_write = release_write
        # The pump thread and the thread that waits for the run can
        # both let go of the guardian, and a descriptor closed twice is
        # a descriptor that may have been reused in between.
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        """The guardian's pid, or ``0`` when there is no guardian."""
        return int(getattr(self._process, "pid", 0) or 0)

    def release_reader(self) -> None:
        """Say that the backend is no longer reading the pipe.

        This is what a backend's death looks like from the guardian's
        side: its file descriptors are closed by the kernel, without any
        cleanup code of its own running.  Closing the release pipe is
        the whole of that effect, and it is what tells the guardian to
        take the pipe over.

        The backend also calls this when it stops reading for a reason
        of its own — its pump ended — because a pipe nobody drains is a
        pipe the executor blocks on forever.

        Idempotent, and never raises.
        """
        with self._lock:
            self._release_reader_locked()

    def _release_reader_locked(self) -> None:
        """Close the release pipe — the caller holds the lock."""
        write_fd = self._release_write
        self._release_write = None
        if write_fd is not None:
            _close_quietly(write_fd)

    def stop(self) -> None:
        """The run is over: stop the guardian.

        Idempotent, and never raises: a guardian that cannot be stopped
        is residue, not a reason to fail a run that has already ended.
        """
        with self._lock:
            process = self._process
            self._process = None
            self._release_reader_locked()
        if process is None:
            return
        try:
            process.terminate()
        except Exception:  # noqa: BLE001 - the guardian is residue, not an error
            pass
        try:
            process.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                process.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass


def spawn_guardian(
    *,
    log_fd: int,
    log_path: Path,
    backend_dir: Path,
    max_bytes: Optional[int] = None,
    child_pid: int = 0,
) -> Optional[LogGuardian]:
    """Start the guardian of the log pipe.

    Args:
        log_fd: the read end of the pipe the child writes into.  The
            guardian inherits a copy of it.
        log_path: where the guardian writes the bytes it takes over.
        backend_dir: the directory :mod:`bounded_log` lives in.
        max_bytes: the bound the guardian's writer enforces.  ``None``
            means the bound the log module's own default is.
        child_pid: the pid of the child the pipe belongs to.  The
            guardian stops waiting when that child is gone.

    Returns:
        The guardian, or ``None`` when one could not be started.  A
        guardian that cannot start is not a failure: the run goes on
        exactly as it would have without one.
    """
    if not sys.executable:
        return None
    try:
        release_read, release_write = os.pipe()
    except OSError:
        return None
    env: Dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if key not in SPAWN_MARKER_ENV
    }
    command: Tuple[str, ...] = (
        sys.executable,
        "-c",
        GUARDIAN_SOURCE,
        str(log_path),
        str(int(max_bytes)) if max_bytes else "0",
        str(backend_dir),
        str(int(log_fd)),
        str(int(release_read)),
        str(int(child_pid)),
        str(GUARDIAN_POLL_SECONDS),
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            pass_fds=(int(log_fd), int(release_read)),
            start_new_session=True,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001 - a guardian is a protection, not a gate
        _close_quietly(release_read)
        _close_quietly(release_write)
        return None
    # The child has its own copy of the release pipe's read end; the
    # parent's copy is not needed any more.
    _close_quietly(release_read)
    return LogGuardian(process, release_write)
