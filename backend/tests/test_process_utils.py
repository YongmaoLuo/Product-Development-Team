"""Tests for the shared process-kill helper."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    proc = _spawn_sleep()
    time.sleep(0.3)
    my_pgid = os.getpgid(proc.pid)
    kill_process_group(proc)
    proc.wait(timeout=5)

    # The process group should no longer be queryable (ESRCH).
    with pytest.raises(ProcessLookupError):
        os.getpgid(my_pgid)


# ---------------------------------------------------------------------------
# The two groups this helper must never signal
# ---------------------------------------------------------------------------


def test_a_mocked_process_is_never_resolved_to_a_group() -> None:
    """A ``MagicMock`` proc must not become a ``killpg`` target.

    ``os.getpgid`` takes ``__index__``, not ``int``, and ``MagicMock``
    answers ``__index__`` with ``1``. So the old ``getattr(proc, "pid")``
    guard let the mock through, ``getpgid`` returned the group of PID 1,
    and the helper sent that group a SIGKILL — from every provider
    fallback in ``coding_tool``, all six of which run against a mock.

    The signal is invisible on a developer machine (the group holds
    ``init``, which an unprivileged process may not signal, and the
    ``EPERM`` is swallowed by the best-effort handler). Assert on the
    *call*, not on the outcome: whether it lands depends on privilege,
    and the call is the defect.
    """
    with patch("utils.process.os.killpg") as mock_killpg:
        kill_process_group(MagicMock())

    mock_killpg.assert_not_called(), (
        "a MagicMock answered __index__ with 1, so the mock's 'process "
        "group' resolved to PID 1's and the helper SIGKILLed it"
    )


def test_a_mock_with_an_explicit_pid_is_still_not_a_target() -> None:
    """Same, for the mocks that do set ``.pid`` — to a non-integer.

    Several suites set ``proc.pid = 12345``, which is safe by accident
    (no such process). Others leave it a ``MagicMock``, which is not.
    The guard is the type, so both are covered by one rule.
    """
    proc = MagicMock()
    proc.pid = "12345"  # a string pid: truthy, not an int
    with patch("utils.process.os.killpg") as mock_killpg:
        kill_process_group(proc)
    mock_killpg.assert_not_called()


def test_our_own_process_group_is_never_signalled() -> None:
    """A child in the caller's group must not take the caller with it.

    ``coding_tool`` spawns with ``start_new_session=True`` so its children
    get their own group, but a caller that forgets leaves the child in
    ours — and killing *that* group kills the backend, or under pytest the
    entire session, with nothing in the log to say why. ``bounded_subprocess``
    has refused this since it was written; this helper had not.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        # deliberately NOT start_new_session — the mistake being guarded
    )
    try:
        assert os.getpgid(proc.pid) == os.getpgrp(), (
            "fixture must leave the child in our own group, or the test "
            "proves nothing"
        )
        with patch("utils.process.os.killpg") as mock_killpg:
            kill_process_group(proc)
        mock_killpg.assert_not_called()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_a_real_child_is_still_killed_after_the_guards() -> None:
    """The guards must not have quietly disabled the function.

    Both new guards reject inputs; this pins that a genuine
    ``subprocess.Popen`` still gets its group killed, which is the only
    reason any of this is safe to add.
    """
    proc = _spawn_sleep()
    time.sleep(0.3)
    pgid = os.getpgid(proc.pid)
    assert pgid != os.getpgrp(), "fixture must have its own group"

    kill_process_group(proc)

    with pytest.raises(ProcessLookupError):
        os.getpgid(pgid)
