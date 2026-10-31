"""Unit tests for the process-identity probe.

PIDs are recycled. Signalling a PID because the ledger says so can
murder an unrelated process that inherited the number, so the identity
of a PID has to be read from the kernel and compared against what was
recorded before any signal is sent.

Covers:
  * ``probe`` — a real child, a vanished pid, the ``ps`` fallback.
  * ``alive`` — the same, through the liveness shortcut.
  * ``matches`` — pid reuse, command-line drift, the create-time
    tolerance, and the pids that must never be signalled.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from datetime import datetime
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import process_identity  # noqa: E402
from process_identity import ProcessIdentity, alive, matches, probe  # noqa: E402


#: A ``ps -o lstart=`` reading, and the epoch it stands for. The day is
#: double-padded in real ``ps`` output ("Sep  4"), which is why the
#: parser has to cope with the extra space.
_LSTART = "Fri Sep  4 09:32:01 2026"
_LSTART_EPOCH = datetime(2026, 9, 4, 9, 32, 1).timestamp()


def _spawn_child() -> subprocess.Popen:
    """A real child process that stays alive until it is killed."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def test_probe_real_child_matches_its_identity():
    """A live child has a readable identity, and it matches itself."""
    child = _spawn_child()
    try:
        ident = probe(child.pid)

        assert ident is not None
        assert ident.pid == child.pid
        assert ident.create_time is not None
        assert ident.create_time <= time.time()
        # ``argv[0]`` is deliberately not compared against
        # ``sys.executable``: on macOS the framework launcher re-executes
        # the interpreter and rewrites ``argv[0]`` to the resolved path,
        # so a reading taken right after the spawn can catch either
        # spelling. Everything after it is stable.
        assert len(ident.cmdline) == 3
        assert os.path.isabs(ident.cmdline[0])
        assert "python" in ident.cmdline[0].lower()
        assert ident.cmdline[1] == "-c"
        assert ident.cmdline[2] == "import time; time.sleep(30)"
        assert ident.pgid == os.getpgid(child.pid)
        assert ident.cwd == os.getcwd()

        # The identity the kernel reports is the identity the caller
        # recorded, so it has to match itself.
        assert ident.matches(
            create_time=ident.create_time, cmdline=ident.cmdline
        ) is True
        assert matches(
            ident, create_time=ident.create_time, cmdline=ident.cmdline
        ) is True
    finally:
        child.kill()
        child.wait(timeout=10)


def test_missing_pid_probe_returns_none():
    """A pid that is gone has no identity, and is not alive."""
    child = _spawn_child()
    pid = child.pid
    child.kill()
    child.wait(timeout=10)

    assert probe(pid) is None
    assert alive(pid) is False


def test_probe_rejects_a_pid_that_cannot_name_a_process():
    assert probe(0) is None
    assert probe(-1) is None
    assert alive(0) is False
    assert alive(-1) is False


# ---------------------------------------------------------------------------
# alive
# ---------------------------------------------------------------------------

def test_alive_is_true_for_a_running_child():
    child = _spawn_child()
    try:
        assert alive(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=10)


# ---------------------------------------------------------------------------
# matches — pid reuse
# ---------------------------------------------------------------------------

def test_pid_reuse_with_different_create_time_does_not_match():
    """The same pid, started at a different time, is a different process."""
    child = _spawn_child()
    try:
        ident = probe(child.pid)
        assert ident is not None

        # A recycled pid reports a start time from a later epoch.
        recycled = ident.create_time + 3600.0
        assert matches(
            ident, create_time=recycled, cmdline=ident.cmdline
        ) is False
        assert ident.matches(
            create_time=recycled, cmdline=ident.cmdline
        ) is False

        # The tolerance is two seconds, the same bound
        # ``service_manager._pid_reused`` uses: a reading taken on
        # either side of a one-second tick still names the same process.
        assert matches(
            ident, create_time=ident.create_time + 1.9, cmdline=ident.cmdline
        ) is True
        assert matches(
            ident, create_time=ident.create_time + 2.5, cmdline=ident.cmdline
        ) is False
    finally:
        child.kill()
        child.wait(timeout=10)


def test_matches_needs_a_create_time_on_both_sides():
    ident = ProcessIdentity(
        pid=200, create_time=1700000000.12, cmdline=("python", "cli.py")
    )
    assert matches(ident, create_time=None, cmdline=ident.cmdline) is False
    assert matches(
        ProcessIdentity(pid=200, create_time=None, cmdline=("python", "cli.py")),
        create_time=1700000000.12,
        cmdline=("python", "cli.py"),
    ) is False


# ---------------------------------------------------------------------------
# matches — command line
# ---------------------------------------------------------------------------

def test_cmdline_mismatch_does_not_match():
    ident = ProcessIdentity(
        pid=200,
        create_time=1700000000.12,
        cmdline=("python", "cli.py"),
        pgid=200,
        cwd="/tmp/p1",
    )

    assert matches(
        ident, create_time=1700000000.12, cmdline=("python", "cli.py")
    ) is True

    # A different first argument — a different program.
    assert matches(
        ident, create_time=1700000000.12, cmdline=("python", "other.py")
    ) is False
    # A different interpreter — a different program.
    assert matches(
        ident, create_time=1700000000.12, cmdline=("node", "cli.py")
    ) is False
    # A different length — the command line is not the recorded one.
    assert matches(
        ident, create_time=1700000000.12, cmdline=("python", "cli.py", "--flag")
    ) is False
    assert matches(
        ident, create_time=1700000000.12, cmdline=("python",)
    ) is False


def test_cmdline_comparison_ignores_empty_strings():
    """Empty entries are padding, not part of the command line."""
    ident = ProcessIdentity(
        pid=200, create_time=1700000000.12, cmdline=("python", "cli.py")
    )

    assert matches(
        ident, create_time=1700000000.12, cmdline=("python", "cli.py", "")
    ) is True
    assert matches(
        ident, create_time=1700000000.12, cmdline=("", "python", "cli.py")
    ) is True
    assert matches(
        ident, create_time=1700000000.12, cmdline=("", "python", "", "cli.py", "")
    ) is True


# ---------------------------------------------------------------------------
# matches — the pids that must never be signalled
# ---------------------------------------------------------------------------

def test_never_matches_pid_one_or_our_own_pid():
    """init and this process are never the target; the caller refuses."""
    ours = ProcessIdentity(
        pid=os.getpid(), create_time=time.time(), cmdline=("python", "backend")
    )
    assert ours.matches(
        create_time=ours.create_time, cmdline=ours.cmdline
    ) is False

    init = ProcessIdentity(pid=1, create_time=time.time(), cmdline=("sbin", "init"))
    assert init.matches(create_time=init.create_time, cmdline=init.cmdline) is False

    # The same refusal through the module-level entry point.
    assert matches(ours, create_time=ours.create_time, cmdline=ours.cmdline) is False


def test_matches_of_nothing_is_false():
    assert matches(None, create_time=1700000000.12, cmdline=("python",)) is False


# ---------------------------------------------------------------------------
# the ps fallback
# ---------------------------------------------------------------------------

def test_ps_fallback_parses_ps_output(monkeypatch):
    """Without psutil the whole identity has to come out of ``ps``."""
    monkeypatch.setattr(process_identity, "psutil", None)

    fake_output = (
        f"  4242 4242 {_LSTART} /usr/bin/python cli.py\n"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=fake_output, stderr="")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    ident = probe(4242)

    assert ident is not None
    assert ident.pid == 4242
    assert ident.pgid == 4242
    assert ident.create_time == pytest.approx(_LSTART_EPOCH)
    assert ident.cmdline == ("/usr/bin/python", "cli.py")
    assert ident.cwd is None  # ps cannot report the working directory

    # The format string is the one the fallback is specified against.
    assert calls
    assert calls[0][0] == "ps"
    assert "pid=,pgid=,lstart=,command=" in calls[0]
    assert "4242" in calls[0]


def test_ps_fallback_returns_none_when_ps_fails(monkeypatch):
    """No psutil and no ps: there is no identity to report."""
    monkeypatch.setattr(process_identity, "psutil", None)

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert probe(4242) is None
    assert alive(4242) is False


def test_ps_fallback_returns_none_when_ps_reports_nothing(monkeypatch):
    """``ps`` exits non-zero for a pid it cannot find."""
    monkeypatch.setattr(process_identity, "psutil", None)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(process_identity.subprocess, "run", fake_run)

    assert probe(4242) is None
    assert alive(4242) is False


def test_probe_returns_none_when_psutil_reports_the_process_is_gone(monkeypatch):
    """``NoSuchProcess`` means the pid is gone — not "ask ps instead"."""

    class NoSuchProcess(Exception):
        pass

    class _Vanished:
        def __init__(self, pid):
            raise NoSuchProcess(pid)

    stub = types.SimpleNamespace(NoSuchProcess=NoSuchProcess, Process=_Vanished)
    monkeypatch.setattr(process_identity, "psutil", stub)

    assert probe(4242) is None
    assert alive(4242) is False
