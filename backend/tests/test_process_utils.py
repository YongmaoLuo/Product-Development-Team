"""Tests for the shared process-kill helper."""

from __future__ import annotations

import contextlib
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


# ---------------------------------------------------------------------------
# The POSIX sentinels.
#
# Everything above this line guards the *type* of the pid. This guards the
# *value of the resulting group*, and it is the guard that closes the CI
# wedge: `killpg(1, sig)` is `kill(-1, sig)`, a broadcast. The mock that
# caused it resolved to 1 by way of `__index__`, but a stub with
# `pid = 1` is a plain `int` and would pass an isinstance check.
#
# These are written as "killpg was never reached" rather than "killpg was
# reached and refused", because the difference matters: on Linux the
# broadcast succeeds, so a test that asserted on the return value would
# itself be killing the runner it runs on. Asserting at the call boundary
# keeps the whole suite safe to run anywhere.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentinel",
    [
        pytest.param(1, id="one-broadcasts-to-every-same-uid-process"),
        pytest.param(0, id="zero-is-the-callers-own-group"),
        pytest.param(-1, id="negative-one-reaches-init-as-a-plain-pid"),
        pytest.param(-12345, id="arbitrary-negative-pid"),
    ],
)
def test_a_sentinel_pgid_never_reaches_killpg(sentinel: int) -> None:
    """``os.killpg`` must not be called for a reserved ``pgid``.

    ``killpg(1, SIGKILL)`` is the exact statement that wedged the CI
    shard: glibc has no special case, so it becomes ``kill(-1, sig)``
    and SIGKILLs every process the caller may signal — the runner's
    worker, its shell, and the job. Darwin's libc returns ``EPERM``
    before the kernel is reached, which is why this shipped unnoticed:
    the same line is a dud on a Mac and a grenade in CI.
    """
    proc = MagicMock()
    proc.pid = 4242  # a real int, so `_child_pid` lets it through
    with patch("utils.process.os.getpgid", return_value=sentinel):
        with patch("utils.process.os.killpg") as mock_killpg:
            kill_process_group(proc)

    mock_killpg.assert_not_called(), (
        f"killpg({sentinel}, ...) reached os.killpg; on Linux that is a "
        f"broadcast or the caller's own group, not a child of ours"
    )


def test_a_bool_pgid_never_reaches_killpg() -> None:
    """``True`` is ``1`` and ``False`` is ``0`` — both are sentinels.

    ``bool`` subclasses ``int``, so without the explicit check a group
    of ``True`` walks straight past ``isinstance(pgid, int)`` and lands
    on the broadcast.
    """
    for sentinel in (True, False):
        proc = MagicMock()
        proc.pid = 4242
        with patch("utils.process.os.getpgid", return_value=sentinel):
            with patch("utils.process.os.killpg") as mock_killpg:
                kill_process_group(proc)
        mock_killpg.assert_not_called(), (
            f"killpg({sentinel!r}, ...) reached os.killpg"
        )


def test_the_lowest_legitimate_pgid_is_still_signalled() -> None:
    """Pin the boundary: ``2`` is the first signallable group.

    Guards that reject too much are as broken as guards that reject too
    little, and this is the one that catches an off-by-one in the
    sentinel check — ``<= 1`` is correct, ``<= 2`` would be a bug.
    """
    proc = MagicMock()
    proc.pid = 4242
    with patch("utils.process.os.getpgid", return_value=2):
        with patch("utils.process.os.getpgrp", return_value=999999):
            with patch("utils.process.os.killpg") as mock_killpg:
                kill_process_group(proc)

    mock_killpg.assert_called_once_with(2, mock_killpg.call_args[0][1])


# ---------------------------------------------------------------------------
# The conftest tripwire itself.
#
# A guard nobody tests is a guard that quietly stops guarding — usually
# because a refactor moved the call, not because anyone undid the fix.
# These pin the tripwire while the suite is still green; the interesting
# failure mode is the one where these go quiet.
# ---------------------------------------------------------------------------


def _tripwire_reports(request) -> list:
    """The conftest tripwire's report list, reached the way pytest sees it.

    ``import conftest`` does not work: pytest loads the file under a
    dotted name derived from rootdir (``backend.tests.conftest``), and
    the plugin manager registers it under a name of its own, so neither
    ``import conftest`` nor ``get_plugin`` finds it. Looking for the
    loaded module by suffix survives both.
    """
    for name, module in list(sys.modules.items()):
        if name.endswith("conftest") and hasattr(module, "_KILLPG_REPORTS"):
            return module._KILLPG_REPORTS
    raise AssertionError(
        "the conftest tripwire is not installed — no conftest module "
        "exposes _KILLPG_REPORTS, so the fuse would never fire"
    )


def test_the_tripwire_ignores_a_legitimate_group(request) -> None:
    """``pgid >= 2`` must never be reported, or the fuse is noise.

    Uses signal 0 — the existence-check signal, which resolves the target
    and the permission without delivering anything. ``unittest.mock``
    cannot be used here: patching ``os.killpg`` replaces the very call
    the audit hook observes, which is exactly the way to get a green run
    from a broken fuse.
    """
    reports = _tripwire_reports(request)

    before = len(reports)
    with contextlib.suppress(OSError):
        os.killpg(2, 0)
    assert len(reports) == before, (
        "the tripwire flagged pgid=2, which is an ordinary group"
    )


@pytest.mark.parametrize("sentinel", [1, 0], ids=["broadcast", "own-group"])
def test_the_tripwire_catches_a_sentinel(request, sentinel: int) -> None:
    """A sentinel reaching ``killpg`` must be recorded, with its stack.

    Signal 0 again: the point is that the *attempt* is caught, and no
    process on this machine should have to die to prove it. On macOS the
    call is refused by libc anyway; on Linux CI it would succeed, which
    is why the assertion is on the report and not on the return value.
    """
    reports = _tripwire_reports(request)

    before = len(reports)
    with contextlib.suppress(OSError):
        os.killpg(sentinel, 0)
    assert len(reports) == before + 1, (
        f"pgid={sentinel} reached os.killpg and the tripwire did not "
        f"record it — the fuse is not installed or not matching"
    )
    assert "os.killpg" in reports[-1]
    # This call was made on purpose to prove the fuse works. Drop the
    # record so the end-of-session check stays meaningful: it must fire
    # on accidents, not on the tests that verify it.
    del reports[before:]


def test_the_tripwire_stays_quiet_across_the_whole_suite(request) -> None:
    """Nothing in this file may attempt a sentinel kill.

    The unit tests above drive ``os.killpg`` through a mock, which the
    hook cannot see, so this is the check that a real one never happens
    either. It runs last, so a violation surfaces after the rest of the
    session has already been reported.
    """
    reports = _tripwire_reports(request)

    assert reports == [], (
        "a sentinel killpg was attempted somewhere in this module: "
        + "\n".join(reports)
    )
