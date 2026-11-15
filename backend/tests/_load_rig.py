"""A CPU load rig the tests can start — and, more importantly, stop.

Why this module exists
----------------------
A case that needs the host to be busy used to build its own busy loop: a
shell pipeline with ``while :; do :; done`` behind it, started from the
test and never reaped.  That shape has no owner.  When the case failed,
the loop kept running; when several of them ran at once, the host was so
busy that the rest of the suite measured the load instead of the code
under test.  The load had become the incident.

So the load comes from a packaged rig instead:

* every worker is a real child process, so the parent can take it back;
* every worker owns an eight-byte counter slot in a shared file, so the
  rig can report what the load actually did rather than how long it was
  asked to run;
* every worker is reclaimed when the block ends — including when the
  block ends by raising — and a worker that ignores ``SIGTERM`` is
  killed.

Why the counters live in a file
-------------------------------
The rig has to answer "did the workers really spin?" without asking the
workers, because a worker that is stuck cannot answer anything.  A
memory-mapped counter file answers it from the parent: each worker owns
one eight-byte slot and increments it in a loop, and the parent reads the
slots with ``struct.unpack_from``.  The file is removed when the rig is
torn down, so nothing is left behind on disk either.

Why ``subprocess.Popen`` is called through the module
-----------------------------------------------------
The rig calls ``subprocess.Popen(...)`` rather than importing ``Popen``
by name.  The difference is what a test can do: replacing the attribute
on the module replaces the call, so a test can make the k-th spawn fail
and watch the rig take the workers that did start back with it.  A name
bound at import time could not be replaced that way, and the failure
path would then be untestable.

Why the host is asked before a worker is started
------------------------------------------------
The rig makes the host busy on purpose, so the first question is whether
the host is already busy.  A rig that loaded a machine that was already
carrying someone else's work would make the incident worse, and the
measurement it exists to support would measure the machine instead of
the code under test.

So the rig asks the suite's one gate — ``tests._host_load`` — before it
spawns anything, and stands down where that gate stands down: a CI
runner's load is not the runner's own doing.  The gate is asked *before*
the first spawn rather than alongside it, because a refusal that has
already started a worker is not a refusal.

The workers need nothing installed: the rig exists so that a test does
not need a load generator on the machine to make it busy.
"""

from __future__ import annotations

import contextlib
import mmap
import os
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from typing import List, Optional

import pytest

from tests import _host_load


__all__ = ["LoadRig", "cpu_load"]


#: The source of one worker.  It is a string rather than a module because
#: the worker is a fresh interpreter: it must not import anything from
#: this tree, and it must not be able to reach the test process's state.
#:
#: Each worker owns one eight-byte slot of the counter file and increments
#: it forever.  The loop is deliberately tight: the point is to keep a core
#: busy, not to do any work.
_WORKER_SOURCE = (
    "import mmap,os,struct,sys\n"
    "fd=os.open(sys.argv[1],os.O_RDWR)\n"
    "mm=mmap.mmap(fd,0)\n"
    "off=int(sys.argv[2])*8\n"
    "while True:\n"
    "    mm[off:off+8]=struct.pack('<Q',struct.unpack('<Q',mm[off:off+8])[0]+1)\n"
)

#: The size of one worker's counter slot.
_SLOT_BYTES = 8

#: How long a worker gets to answer ``SIGTERM`` before it is killed.
_RECLAIM_TIMEOUT = 5


class LoadRig:
    """The handle to a running rig: the workers' pids and what they counted.

    ``pids`` holds the worker pids in the order the workers were started.
    ``iterations()`` is the total number of counter increments across the
    workers, which is the rig's answer to "is the host actually busy right
    now?".
    """

    #: The worker pids, in the order the workers were started.
    pids: List[int]

    def __init__(
        self,
        procs: List[subprocess.Popen],
        path: str,
        workers: int,
    ) -> None:
        self.pids = [proc.pid for proc in procs]
        self._path = path
        self._workers = workers

    def iterations(self) -> int:
        """The number of counter increments the workers have made so far.

        Zero once the rig has been torn down and its counter file removed:
        there is no load left to count, and that is not an error.
        """
        try:
            with open(self._path, "rb") as handle:
                with mmap.mmap(handle.fileno(), 0, prot=mmap.PROT_READ) as slots:
                    return sum(
                        struct.unpack_from("<Q", slots, index * _SLOT_BYTES)[0]
                        for index in range(self._workers)
                    )
        except FileNotFoundError:
            return 0


def _counter_file(workers: int) -> str:
    """Create the counter file and return its path.

    One eight-byte slot per worker, zeroed.  The file is created with the
    size already set, so a worker that maps it never sees a short mapping.
    """
    handle, path = tempfile.mkstemp(prefix="pdt-load-rig-", suffix=".counters")
    try:
        os.ftruncate(handle, workers * _SLOT_BYTES)
    finally:
        os.close(handle)
    return path


def _reclaim(procs: List[subprocess.Popen]) -> None:
    """Stop every worker and take it back.

    ``SIGTERM`` first, so a worker that could clean up after itself gets
    the chance; ``SIGKILL`` for the one that did not answer within the
    timeout.  Every worker is waited for, because a worker that was
    stopped but not reaped is still a pid the system is holding.
    """
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=_RECLAIM_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=_RECLAIM_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass


@contextlib.contextmanager
def cpu_load(
    workers: int = 8,
    *,
    load: Optional[float] = None,
    limit: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Iterator[LoadRig]:
    """Run *workers* busy processes for the length of the block.

    The workers are reclaimed when the block ends, whatever ends it — the
    normal end of the block, an exception raised inside it, or a worker
    that failed to start at all.  Nothing is left running afterwards.

    The argument is refused before anything is started if it is not a
    positive number of workers.

    The host is asked whether it is idle before any worker is started:
    ``load``, ``limit`` and ``env`` are handed to ``tests._host_load``,
    and ``None`` means "read the real thing".  A host the gate refuses is
    skipped with the gate's own message, and not one worker is started.
    """
    # The argument is checked before the gate: a request that is wrong
    # whatever the machine is doing is wrong on a busy host too, and a
    # skip there would turn a refused call into a silent pass.
    if workers <= 0:
        raise ValueError(f"workers must be positive, got {workers}")

    # The gate is asked before the first spawn, and before the counter
    # file exists, so a refused host is left exactly as it was found.
    reason = _host_load.skip_reason(load=load, limit=limit, env=env)
    if reason is not None:
        pytest.skip(reason)

    path = _counter_file(workers)
    procs: List[subprocess.Popen] = []
    try:
        for index in range(workers):
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", _WORKER_SOURCE, path, str(index)]
                )
            )
        yield LoadRig(procs, path, workers)
    finally:
        # A spawn that failed leaves the workers before it to be reclaimed
        # here, before the failure reaches the caller.
        _reclaim(procs)
        try:
            os.unlink(path)
        except OSError:
            pass
