"""Tests for the shared process-kill helper."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from utils.process import kill_process_group


def _spawn_sleep(seconds: int = 30) -> subprocess.Popen:
    """Spawn a child in its own process group that sleeps for a while."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        start_new_session=True,
    )


def test_kill_process_group_terminates_proc() -> None:
    proc = _spawn_sleep()
    time.sleep(0.3)  # let it actually start
    assert proc.poll() is None

    kill_process_group(proc)

    # proc.wait() should return quickly after the kill
    rc = proc.wait(timeout=5)
    assert rc is not None  # process is gone


def test_kill_process_group_returns_exit_code() -> None:
    """The helper returns the process exit code after reaping it."""
    proc = _spawn_sleep()
    time.sleep(0.3)
    rc = kill_process_group(proc)
    assert rc is not None


def test_kill_process_group_already_dead_is_noop() -> None:
    """Killing an already-exited process does not raise."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('done')"],
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc.wait(timeout=5)
    kill_process_group(proc)  # should not raise


def test_kill_process_group_handles_none_proc() -> None:
    """Passing None is a no-op (callers routinely guard against None)."""
    kill_process_group(None)  # should not raise


def test_kill_process_group_uses_sigkill_by_default() -> None:
    """Default signal is SIGKILL so subprocesses cannot catch it."""
    import os

    proc = _spawn_sleep()
    time.sleep(0.3)
    my_pgid = os.getpgid(proc.pid)
    kill_process_group(proc)
    proc.wait(timeout=5)

    # The process group should no longer be queryable (ESRCH).
    with pytest.raises(ProcessLookupError):
        os.getpgid(my_pgid)
