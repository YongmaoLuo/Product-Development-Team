"""Bounded, rotating log files for the plan directory.

Why this module exists
----------------------
The plan directory's logs were bare files.  ``logs/repair_*.log`` is
the executor subprocess's stdout redirected into a file, and nothing
ever bounded it: one long run grew a single log past **5.4 GB**.  The
damage is not only the disk.  A reader that has to find one line in a
5.4 GB file has to read 5.4 GB to find it, and the readers of these
logs sit on paths that render a status card — so the plan directory
grew until the thing that was supposed to explain what the plan was
doing became the slowest thing in the request.

So the writer is bounded.  The live log is rotated aside before it
would grow past ``MAX_PLAN_LOG_BYTES``, the rotated copies are numbered
``<name>.1`` … ``<name>.N``, the **highest number is the oldest**, and
the copy that falls past ``PLAN_LOG_BACKUP_COUNT`` is dropped.  A log
directory is then a fixed-size thing: ``MAX_PLAN_LOG_BYTES`` times
``PLAN_LOG_BACKUP_COUNT + 1``, whatever the run does.

The layout is not this module's invention, and that matters more than
the bound itself
-------------------------------
``ExecutionLogger`` has rotated ``execution.log`` this way since it
was written, and ``pause_state._rotated_log_paths`` reads the rotated
copies back in the same order — oldest first, so that a line written
after a rotation wins over the one it replaced (a rotation only moves
*old* lines out of the way).  A writer that numbered its copies
differently would silently lose the task→session bindings a resume
needs: a binding that is not found looks exactly like a task that was
never bound, and the resume starts a task from scratch instead of
re-opening it.

So the names here are the names the reader already expects, and the
defaults are the defaults ``ExecutionLogger`` already uses.  A writer
with a different layout is not a new feature, it is a second dialect
of the same file.

The contract
------------
* **The bound holds per file.**  The live log and every rotated copy
  are at most ``max_bytes``.  A single write that is bigger than the
  bound is truncated to it — otherwise one write could put the file
  over the bound on its own, which is the case the bound exists for.
* **An empty write changes nothing.**  Not the file, not the copies:
  a rotation is a visible side effect and it must not be the side
  effect of a no-op.
* **The defaults are read when the call happens**, not captured at
  import time.  ``MAX_PLAN_LOG_BYTES`` and ``PLAN_LOG_BACKUP_COUNT``
  are read inside the call, so a test can change the bound without
  re-importing the module and a configuration change takes effect
  without a reload.
* **Writers that share a path share a lock.**  The lock is per path
  and lives in this process, so ``append_bounded_line`` and a
  ``RotatingLogSink`` on the same file are serialised against each
  other: two writers without one lock interleave, and the line being
  written when the other writer rotates lands in the rotated copy — or
  nowhere.  Two *processes* writing the same plan directory are not
  covered by this; that is what the plan-level single-writer
  arrangement is for.
* **A failed write costs that chunk and nothing else.**
  ``pump_stream_to_sink`` drops the chunk it could not write and
  keeps reading the pipe.  A child blocked writing into a pipe nobody
  drains never finishes, so a full disk must not become a hang.

Usage
-----
::

    sink = RotatingLogSink(plan_dir / "logs" / "repair_x.log")
    pump_stream_to_sink(process.stdout, sink)
    sink.close()

    # or, for one line at a time:
    append_bounded_line(plan_dir / "logs" / "verification_1.log", line)
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

#: The largest a single log file may grow before it is rotated aside.
#: 10 MB — the same bound ``ExecutionLogger`` uses for ``execution.log``.
MAX_PLAN_LOG_BYTES = 10 * 1024 * 1024

#: How many rotated copies are kept next to the live log.  The copy
#: that falls past this number is dropped, so the whole set is
#: ``MAX_PLAN_LOG_BYTES * (PLAN_LOG_BACKUP_COUNT + 1)`` at the worst.
PLAN_LOG_BACKUP_COUNT = 3

PathLike = Union[str, "os.PathLike[str]"]

#: The per-path locks.  Keyed by the resolved path, so two writers that
#: name the same file differently (``logs/x.log`` and an absolute path)
#: still serialise against each other.
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[str, threading.Lock] = {}


# ---------------------------------------------------------------------------
# The per-path lock
# ---------------------------------------------------------------------------

def _lock_key(path: PathLike) -> str:
    """The key two writers of the same file agree on.

    The path is resolved, so the same file named two ways (relative vs
    absolute, through a symlink) gets the same key — and therefore the
    same lock.  A path that cannot be resolved falls back to its
    literal text: a lock that is too coarse is harmless, a lock that is
    missing is not.
    """
    try:
        return os.path.realpath(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        return str(path)


def _lock_for(path: PathLike) -> threading.Lock:
    """The lock that serialises the writers of ``path``.

    One lock per path, shared by every writer in this process: the
    ``RotatingLogSink``, ``append_bounded_line`` and any other sink
    that is pointed at the same file.
    """
    key = _lock_key(path)
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


# ---------------------------------------------------------------------------
# The rotation
# ---------------------------------------------------------------------------

def _size_of(path: Path) -> int:
    """The size of ``path``, or 0 when it is not there.

    A log that cannot be measured is treated as empty: the write that
    follows then starts a fresh file rather than refusing to happen.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unlink_quietly(path: Path) -> None:
    """Remove ``path`` if it is there; a failure is not the writer's problem."""
    try:
        path.unlink()
    except OSError:
        pass


def _rotate(path: Path, backup_count: int) -> None:
    """Rotate the live log at ``path`` aside.

    ``<name>.<N>`` is dropped, ``<name>.<N-1>`` becomes ``<name>.<N>``,
    … and the live log becomes ``<name>.1``.  The highest number ends
    up holding the oldest lines, which is the order the readers use.

    With no backups at all the live log is simply removed: there is
    nowhere to put it, and keeping it would keep it over the bound.
    """
    count = max(int(backup_count), 0)
    parent = path.parent

    if count <= 0:
        _unlink_quietly(path)
        return

    for index in range(count, 0, -1):
        source = parent / f"{path.name}.{index}"
        if index == count:
            # The copy that falls past the count is the one that goes.
            _unlink_quietly(source)
            continue
        destination = parent / f"{path.name}.{index + 1}"
        if source.exists():
            os.replace(source, destination)

    if path.exists():
        os.replace(path, parent / f"{path.name}.1")


def rotated_log_paths(
    path: PathLike, backup_count: Optional[int] = None
) -> List[Path]:
    """The rotated copies of ``path``, oldest first.

    The highest index holds the oldest lines and ``.1`` the newest of
    the rotated ones, so a descending index is a chronological order —
    the order a reader has to use, because a rotation only moves *old*
    lines out of the way and the live log's line is the newest.

    A copy that does not exist is listed anyway.  Most plans never
    rotate, so most of the candidates a reader looks for are not there,
    and a reader that had to ask "is it there?" before looking would
    need a ``stat()`` per candidate.  The reader decides what to do
    with a missing file; this function only says where they would be.

    ``backup_count`` defaults to ``PLAN_LOG_BACKUP_COUNT``, read when
    the call happens so a test can change the depth without re-importing
    the module.
    """
    target = Path(path)
    count = (
        int(backup_count)
        if backup_count is not None
        else PLAN_LOG_BACKUP_COUNT
    )
    return [
        target.parent / f"{target.name}.{index}"
        for index in range(count, 0, -1)
    ]


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------

class RotatingLogSink:
    """A file-like sink that writes into a bounded, rotating log.

    The sink quacks like a file object — ``write`` / ``flush`` /
    close`` — so it can stand in for one wherever a caller opens a log
    file, and it is bounded wherever a plain file would not be.

    The file is opened, appended to and closed on every write rather
    than held open.  That is what makes the bound and the rotation
    correct when two writers share a path: a held handle would keep
    writing into the file the other writer just rotated aside, so the
    line would land in the copy instead of the live log.  The cost is
    one open per write, which is nothing next to the write itself.
    """

    def __init__(
        self,
        path: PathLike,
        *,
        max_bytes: Optional[int] = None,
        backup_count: Optional[int] = None,
    ):
        """
        Parameters
        ----------
        path:
            The live log.  Its rotated copies are ``<name>.1`` …
            ``<name>.N`` next to it.
        max_bytes:
            The bound.  ``None`` means ``MAX_PLAN_LOG_BYTES``, read
            when the write happens rather than captured here.
        backup_count:
            How many rotated copies to keep.  ``None`` means
            ``PLAN_LOG_BACKUP_COUNT``, read the same way.
        """
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._lock = _lock_for(self._path)

    # -- The configuration, resolved at call time --

    @property
    def path(self) -> Path:
        """The live log this sink writes to."""
        return self._path

    @property
    def max_bytes(self) -> int:
        """The bound in force for the next write."""
        if self._max_bytes is not None:
            return int(self._max_bytes)
        return MAX_PLAN_LOG_BYTES

    @property
    def backup_count(self) -> int:
        """How many rotated copies are kept."""
        if self._backup_count is not None:
            return int(self._backup_count)
        return PLAN_LOG_BACKUP_COUNT

    # -- The file-like surface --

    def write(self, text: Any) -> int:
        """Append ``text`` to the live log, rotating it if need be.

        Accepts ``str`` or ``bytes`` — a pipe hands over bytes and a
        caller usually has a string.  Returns the number of bytes
        written, which is less than the input when the input was
        bigger than the bound.

        An empty write does nothing at all: not the file, not the
        copies.  A rotation is a visible side effect and it must not be
        the side effect of a no-op.
        """
        limit = self.max_bytes
        payload = self._capped_payload(text, limit)
        if not payload:
            return 0

        with self._lock:
            current = _size_of(self._path)
            if current > 0 and current + len(payload) > limit:
                _rotate(self._path, self.backup_count)

            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "ab") as handle:
                handle.write(payload)
        return len(payload)

    def flush(self) -> None:
        """Nothing to flush: every write is on disk before it returns.

        The method exists because the sink stands in for a file object,
        and a caller that flushes a file it opened should not have to
        know that this one is already durable.
        """

    def close(self) -> None:
        """Nothing to close: the sink holds no file handle.

        The method exists for the same reason ``flush`` does, and it is
        idempotent — a later write simply opens the file again.
        """

    # -- The bound --

    def _capped_payload(self, text: Any, limit: int) -> bytes:
        """``text`` as bytes, truncated to ``limit``.

        A write bigger than the bound is truncated to it: the bound is
        what keeps the disk from filling up, and one write that
        overshoots would defeat it.

        Text is cut on a character boundary — a half-written character
        at the end of the file would be a broken file.  Bytes are cut
        on a byte boundary, because a byte is not half of anything.
        """
        limit = max(int(limit), 0)
        if isinstance(text, (bytes, bytearray, memoryview)):
            payload = bytes(text)
            return payload[:limit]

        encoded = str(text).encode("utf-8", "replace")
        if len(encoded) <= limit:
            return encoded
        payload = encoded[:limit]
        while payload:
            try:
                payload.decode("utf-8")
                break
            except UnicodeDecodeError:
                payload = payload[:-1]
        return payload


def append_bounded_line(path: PathLike, line: Any) -> int:
    """Append one line to ``path``, keeping the file within its bound.

    The verification writers append a line at a time; they must not
    have to own a sink object to get the bound.  The line is
    newline-terminated if it is not already, and the same per-path lock
    the sink uses is taken, so a line appended here cannot be torn by a
    rotation happening in the sink.

    Returns the number of bytes written.
    """
    target = Path(path)
    if isinstance(line, (bytes, bytearray, memoryview)):
        text = bytes(line).decode("utf-8", "replace")
    else:
        text = str(line)
    if not text.endswith("\n"):
        text += "\n"
    return RotatingLogSink(target).write(text)


# ---------------------------------------------------------------------------
# The pump
# ---------------------------------------------------------------------------

def pump_stream_to_sink(
    stream: Any, sink: Any, chunk_size: int = 65536
) -> int:
    """Copy ``stream`` into ``sink`` until the stream ends.

    The pipe has to be drained whatever happens to the log: a child
    blocked writing into a pipe nobody reads never finishes, so a log
    that cannot be written must not become a hang.  A chunk whose write
    fails is therefore dropped — the log loses those bytes, the run
    keeps going.

    Returns the number of bytes successfully handed to the sink.
    """
    written = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        try:
            sink.write(chunk)
        except Exception:
            # The chunk is lost.  The stream is not: it keeps being
            # drained, because a stuck child is worse than a hole in a
            # log.
            continue
        written += len(chunk)
    return written