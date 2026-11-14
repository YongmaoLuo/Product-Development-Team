"""The executor's log pipe has a reader that outlives the backend.

Background
----------
The executor's output reaches the plan directory's log through a pipe, so
the log can be bounded (rotated) instead of growing until the disk says
stop.  A pipe needs a reader, and the reader was the backend.

A backend that dies takes its reader with it.  The pipe's write end then
has nobody on the other side, and the executor's next write fails with
``EPIPE`` — which kills an executor that writes a heartbeat every
hundred milliseconds.  So a ``kill -9`` of the backend took every run
that was working at the time with it, and a run that was supposed to
outlive the backend did not.

The guardian is the reader that is not the backend: it holds the pipe's
read end open, so the pipe never breaks, and it takes the draining over
when the backend is gone.

What the tests below pin
------------------------
* the executor survives the reader's death — the whole point of the
  guardian;
* the guardian keeps the log: the bytes the executor wrote after the
  reader died land in the bounded log;
* the guardian does not steal bytes from a live reader — while the
  backend is alive the guardian reads nothing;
* the guardian carries none of the ``PDT_SPAWN_*`` markers, so the
  orphan reaper — which only ever looks at marked processes — never
  takes the executor's reader away from it;
* a run that ends leaves no guardian behind.

The executor is a real subprocess in every case here: the failure being
prevented is a signal delivered to a real process, and a fake would
prove nothing about it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from log_guardian import SPAWN_MARKER_ENV, spawn_guardian


#: The backend directory — where :mod:`bounded_log` lives.
BACKEND_DIR = Path(__file__).resolve().parents[2]


#: A writer that keeps writing.  It is a real program with a real
#: stdout, and a real ``BrokenPipeError`` kills it.
WRITER = """
import sys
import time

for index in range(600):
    sys.stdout.write("line %d %s\\n" % (index, "y" * 60))
    sys.stdout.flush()
    time.sleep(0.02)
"""


def _spawn_writer():
    """Start a writer whose stdout is a pipe; return (child, read end)."""
    read_fd, write_fd = os.pipe()
    child = subprocess.Popen(
        [sys.executable, "-c", WRITER],
        stdout=write_fd,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    os.close(write_fd)
    return child, read_fd


def _spawn_guardian(read_fd: int, log_path: Path, child_pid: int):
    """Start the guardian of *read_fd*, as the spawn site does."""
    return spawn_guardian(
        log_fd=read_fd,
        log_path=log_path,
        backend_dir=BACKEND_DIR,
        max_bytes=None,
        child_pid=child_pid,
    )


def test_the_executor_survives_the_readers_death(tmp_path):
    """The executor is still running after the reader is gone.

    Without the guardian, closing the read end of the pipe is what a
    dying backend does, and the executor's next write is a
    ``BrokenPipeError``.  The guardian holds the read end open, so the
    pipe never breaks.
    """
    log_path = tmp_path / "executor.log"
    child, read_fd = _spawn_writer()
    guardian = _spawn_guardian(read_fd, log_path, child.pid)
    assert guardian is not None, "the guardian did not start"

    try:
        # The backend dies: its descriptors are closed by the kernel,
        # and its reader goes with it.
        os.close(read_fd)
        guardian.release_reader()

        time.sleep(1.0)
        assert child.poll() is None, (
            f"the executor died with the reader (exit={child.poll()}); the "
            "pipe broke under it"
        )
    finally:
        try:
            child.kill()
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        guardian.stop()


def test_the_guardian_keeps_the_log(tmp_path):
    """The bytes written after the reader died land in the log.

    The guardian does not only hold the pipe open: it takes the
    draining over, so the run that outlived the backend is still being
    logged.
    """
    log_path = tmp_path / "executor.log"
    child, read_fd = _spawn_writer()
    guardian = _spawn_guardian(read_fd, log_path, child.pid)
    assert guardian is not None, "the guardian did not start"

    try:
        os.close(read_fd)
        guardian.release_reader()

        deadline = time.monotonic() + 10.0
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="replace")
            if "line " in text:
                break
        assert "line " in text, (
            f"the guardian wrote nothing into the log: {text[:200]!r}"
        )
    finally:
        try:
            child.kill()
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        guardian.stop()


def test_the_guardian_does_not_steal_from_a_live_reader(tmp_path):
    """While the backend is alive the guardian reads nothing.

    Two readers on one pipe split the bytes between them: the log would
    be a mixture of two writers' halves.  The guardian waits for the
    backend to go, and the backend's own pump owns the pipe until then.
    """
    log_path = tmp_path / "executor.log"
    child, read_fd = _spawn_writer()
    guardian = _spawn_guardian(read_fd, log_path, child.pid)
    assert guardian is not None, "the guardian did not start"

    try:
        time.sleep(1.0)
        assert not log_path.exists() or log_path.stat().st_size == 0, (
            "the guardian wrote into the log while the backend was alive"
        )
    finally:
        os.close(read_fd)
        try:
            child.kill()
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        guardian.stop()


def test_the_guardian_carries_no_spawn_markers(tmp_path):
    """The guardian is not a candidate for the orphan reaper.

    The reaper only ever looks at processes carrying the ``PDT_SPAWN_*``
    markers.  A guardian carrying them would be one — and a reaper that
    killed the guardian would take the executor's reader away from it,
    which is the very failure the guardian exists to prevent.
    """
    log_path = tmp_path / "executor.log"
    child, read_fd = _spawn_writer()
    guardian = _spawn_guardian(read_fd, log_path, child.pid)
    assert guardian is not None, "the guardian did not start"

    try:
        guardian_pid = guardian.pid
        assert guardian_pid > 0
        environ = Path(f"/proc/{guardian_pid}/environ")
        if environ.exists():  # noqa: SIM108 - Linux only; the assert is optional
            raw = environ.read_bytes().decode("utf-8", errors="replace")
            for marker in SPAWN_MARKER_ENV:
                assert marker not in raw, (
                    f"the guardian carries {marker}; the orphan reaper would "
                    "treat it as one of ours"
                )
    finally:
        os.close(read_fd)
        try:
            child.kill()
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        guardian.stop()


def test_stopping_the_guardian_leaves_nothing_behind(tmp_path):
    """A run that ends leaves no guardian behind."""
    log_path = tmp_path / "executor.log"
    child, read_fd = _spawn_writer()
    guardian = _spawn_guardian(read_fd, log_path, child.pid)
    assert guardian is not None, "the guardian did not start"
    guardian_pid = guardian.pid

    try:
        child.kill()
        child.wait(timeout=5)
    finally:
        guardian.stop()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _alive(guardian_pid):
            break
        time.sleep(0.1)
    assert not _alive(guardian_pid), (
        f"the guardian {guardian_pid} is still running after the run ended"
    )


def _alive(pid: int) -> bool:
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
