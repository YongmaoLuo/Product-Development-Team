"""Reading the machine: processes, their environment, their ports, the swap.

The reaper's judgement rests on one piece of evidence — the ``PDT_SPAWN_*``
marker — and that evidence lives in a *process' environment*.  Reading it
is the part of the reaper that is not portable, and this file pins the
portable part of it:

* **Linux** reads ``/proc/<pid>/environ`` — exact, NUL-separated.
* **macOS** has no ``/proc``; ``sysctl kern.procargs2`` does not expose the
  environment either, so ``ps -E`` is the only route.

Two macOS facts shape the whole design, and both are pinned here because
they are the difference between "this process is not ours" and "this
process is *not ours and we cannot tell*":

1. **Apple's own binaries are unreadable.**  ``/bin/sleep``, ``/bin/bash``
   and friends are protected by SIP, and ``ps -E`` shows their command line
   but *not* their environment.  A read that produced *some* output and no
   ``k=v`` segment therefore means **"unreadable"**, never "empty" — an
   empty dict would be read downstream as "this process carries no
   marker", which is a different and wrong claim.
2. **``ps -E`` reports the environment the process was *launched* with.**
   It does not reflect any change made to the environment after launch
   (``ps(1)``: "does not reflect changes in the environment after process
   launch").  A test that stamped a variable into its own ``os.environ``
   and then read it back would therefore prove nothing; only a *child*
   launched with the marker in its environment can.

The negative controls are as load-bearing as the positive ones: a vanished
process, a failing probe and an unreadable environment must all resolve to
"cannot tell", never to a value that could be mistaken for evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    SPAWN_KIND,
    SPAWN_OWNER_PID,
    SPAWN_OWNER_START,
    ProcInfo,
    list_processes,
    listening_ports_by_pid,
    read_process_environ,
    swap_usage_ratio,
)


#: A ``ps -E`` line as macOS prints it: the command line first, then the
#: environment the process was launched with.
PS_ENV_OUTPUT = (
    "  PID TTY           TIME CMD\n"
    " 4321 ??         0:00.03 /usr/bin/python3 -m backend.server "
    "PDT_SPAWN_OWNER_PID=31337 PDT_SPAWN_OWNER_START=1700000000 "
    "PDT_SPAWN_KIND=service PATH=/usr/bin:/bin\n"
)

#: What ``ps -p <pid> -o args=`` prints for the same process: the command
#: line and nothing else.
PS_ARGS_OUTPUT = "/usr/bin/python3 -m backend.server\n"

#: A ``ps -E`` line for a process whose environment could not be read (an
#: Apple platform binary): the command line, and not one ``k=v`` segment.
PS_ENV_UNREADABLE_OUTPUT = (
    "  PID TTY           TIME CMD\n"
    " 4322 ??         0:00.00 /bin/sleep 30\n"
)

#: A fixed ``ps`` process table.
PS_PROCESS_TABLE = (
    "    1     0     1     0  25840 /sbin/launchd\n"
    "  234     1   234   501  44288 /usr/libexec/mobileassetd\n"
    " 4321     1  4321   501 123456 /usr/bin/python3 -m backend.server\n"
)

#: A fixed ``lsof`` listing of listening TCP sockets.
LSOF_OUTPUT = (
    "COMMAND   PID       USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"
    "Python  4321 yongmaoluo    3u  IPv4 0x5c5adf24d2593f7b      0t0  "
    "TCP 127.0.0.1:8000 (LISTEN)\n"
    "Python  4321 yongmaoluo    4u  IPv4 0x5c5adf24d2593f7c      0t0  "
    "TCP *:8001 (LISTEN)\n"
    "Python  9999 yongmaoluo    5u  IPv4 0x5c5adf24d2593f7d      0t0  "
    "TCP 127.0.0.1:9000 (LISTEN)\n"
)

#: A fixed ``ss`` listing of listening TCP sockets (Linux).
SS_OUTPUT = (
    "LISTEN 0      4096         0.0.0.0:8000       0.0.0.0:*    "
    'users:(("python3",pid=4321,fd=3))\n'
    "LISTEN 0      4096        127.0.0.1:9000       0.0.0.0:*    "
    'users:(("python3",pid=9999,fd=5))\n'
)


def _fake_ps(monkeypatch, env_output, args_output=PS_ARGS_OUTPUT):
    """Replace ``subprocess.run`` with a ``ps`` that answers two questions.

    ``ps -E -p <pid>`` returns the command line *and* the environment;
    ``ps -p <pid> -o args=`` returns the command line alone.  The fake
    answers each from its own fixture so the test says which shape it is
    exercising.
    """
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "-E" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=env_output, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=args_output, stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    return calls


# --------------------------------------------------------------------------
# read_process_environ — the macOS shape
# --------------------------------------------------------------------------


def test_read_process_environ_macos_shape(monkeypatch):
    """A ``ps -E`` line yields exactly the markers it carries."""
    _fake_ps(monkeypatch, PS_ENV_OUTPUT)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    env = read_process_environ(4321)

    assert env is not None
    assert env[SPAWN_OWNER_PID] == "31337"
    assert env[SPAWN_OWNER_START] == "1700000000"
    assert env[SPAWN_KIND] == "service"


def test_read_process_environ_macos_shape_survives_a_blunt_fake(monkeypatch):
    """The markers are found even when ``ps`` answers both questions alike.

    The environment segment is located by stripping the command line that
    ``ps -p <pid> -o args=`` reports.  A ``ps`` that answers *both*
    questions with the same line leaves that boundary unlocatable, and the
    markers must still be readable — a marker that is in the output is a
    marker, whatever shape the output arrived in.
    """
    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=PS_ENV_OUTPUT, stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    env = read_process_environ(4321)

    assert env is not None
    assert env[SPAWN_OWNER_PID] == "31337"
    assert env[SPAWN_KIND] == "service"


def test_read_process_environ_unreadable_is_none_not_empty(monkeypatch):
    """Output without a single ``k=v`` segment means *unreadable*.

    Returning ``{}`` here would be a lie with consequences: the empty
    mapping says "this process carries no marker", while the truth is
    "this process's environment could not be read".  The first is a
    judgement, the second is an admission of ignorance — and only the
    second may lead to a skip.
    """
    _fake_ps(monkeypatch, PS_ENV_UNREADABLE_OUTPUT)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    env = read_process_environ(4322)

    assert env is None


def test_read_process_environ_failing_probe_is_none(monkeypatch):
    """A ``ps`` that fails means "cannot tell", not "no environment"."""

    def fake_run(cmd, *args, **kwargs):
        raise OSError("ps is not there")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    assert read_process_environ(4321) is None


# --------------------------------------------------------------------------
# read_process_environ — against a real process
# --------------------------------------------------------------------------


def _read_environ_until(pid, timeout=10.0):
    """Read *pid*'s environment, retrying while the read is not ready yet.

    A child becomes visible to ``ps`` (or ``/proc``) a moment after it is
    spawned; the retry only covers that window, and the last value read is
    the one asserted on.
    """
    deadline = time.time() + timeout
    env = None
    while time.time() < deadline:
        env = read_process_environ(pid)
        if env is not None:
            break
        time.sleep(0.1)
    return env


def test_read_process_environ_real_child():
    """A child launched with the markers carries them in its environment.

    The child is this interpreter — **not** ``/bin/sleep``.  ``ps -E``
    reports the environment a process was *launched* with, so the only
    honest positive control is a process that was launched with the
    markers in its environment in the first place.  An Apple platform
    binary would also have been unreadable, so its use here would have
    proved nothing twice over.
    """
    env = dict(os.environ)
    env[SPAWN_OWNER_PID] = "31337"
    env[SPAWN_OWNER_START] = "1700000000"
    env[SPAWN_KIND] = "service"

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], env=env
    )
    try:
        read = _read_environ_until(child.pid)

        assert read is not None
        assert read[SPAWN_OWNER_PID] == "31337"
        assert read[SPAWN_OWNER_START] == "1700000000"
        assert read[SPAWN_KIND] == "service"
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="SIP protection of Apple platform binaries is a macOS fact",
)
def test_read_process_environ_platform_binary_is_unreadable():
    """An Apple platform binary's environment cannot be read at all.

    ``/bin/sleep`` is protected by SIP: ``ps -E`` gives its command line
    and no environment.  The read must therefore fail — returning an empty
    mapping would dress "cannot tell" up as "carries no marker".
    """
    child = subprocess.Popen(["/bin/sleep", "30"])
    try:
        assert read_process_environ(child.pid) is None
    finally:
        child.kill()
        child.wait()


def test_read_process_environ_reads_the_target_and_not_the_caller():
    """The mapping belongs to the process that was asked about.

    A child launched *without* the markers must not report them — not
    even when the process running the test carries them itself, which is
    the case whenever the reaper (or the test runner) is itself a
    PDT-spawned process.
    """
    env = dict(os.environ)
    for key in (SPAWN_OWNER_PID, SPAWN_OWNER_START, SPAWN_KIND):
        env.pop(key, None)

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], env=env
    )
    try:
        read = _read_environ_until(child.pid)

        assert read is not None
        assert SPAWN_OWNER_PID not in read
        assert SPAWN_KIND not in read
    finally:
        child.kill()
        child.wait()


def test_read_process_environ_gone():
    """A process that no longer exists cannot be read."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()

    assert read_process_environ(child.pid) is None


def test_read_process_environ_never_raises():
    """A nonsense pid is answered with ``None``, never with an exception."""
    for pid in (0, -1, 99999999999999):
        assert read_process_environ(pid) is None


# --------------------------------------------------------------------------
# list_processes
# --------------------------------------------------------------------------


def test_parse_list_processes(monkeypatch):
    """A fixed ``ps`` table becomes three :class:`ProcInfo` records.

    Every field is aligned with its column, and the command line keeps
    its spaces — the argv of a running service is the only thing that
    says what it is.
    """
    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=PS_PROCESS_TABLE, stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)

    processes = list_processes()

    assert len(processes) == 3
    assert processes[0] == ProcInfo(
        pid=1,
        ppid=0,
        pgid=1,
        uid=0,
        rss_kb=25840,
        argv="/sbin/launchd",
    )
    assert processes[1] == ProcInfo(
        pid=234,
        ppid=1,
        pgid=234,
        uid=501,
        rss_kb=44288,
        argv="/usr/libexec/mobileassetd",
    )
    assert processes[2] == ProcInfo(
        pid=4321,
        ppid=1,
        pgid=4321,
        uid=501,
        rss_kb=123456,
        argv="/usr/bin/python3 -m backend.server",
    )
    assert processes[2].env is None


def test_list_processes_finds_this_process():
    """The live process table contains the process running the test."""
    processes = list_processes()

    assert any(proc.pid == os.getpid() for proc in processes)


def test_list_processes_failing_probe_is_empty(monkeypatch):
    """A failing ``ps`` yields no processes rather than an exception."""

    def fake_run(cmd, *args, **kwargs):
        raise OSError("ps is not there")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)

    assert list_processes() == []


def test_proc_info_env_defaults_to_none():
    """``env`` is optional: a process record without it says nothing."""
    info = ProcInfo(pid=1, ppid=0, pgid=1, uid=0, rss_kb=1, argv="/x")

    assert info.env is None


# --------------------------------------------------------------------------
# listening_ports_by_pid
# --------------------------------------------------------------------------


def test_listening_ports_by_pid_parses_lsof(monkeypatch):
    """macOS listeners are mapped from pid to the ports they hold."""

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=LSOF_OUTPUT, stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    mapping = listening_ports_by_pid()

    assert mapping is not None
    assert mapping[4321] == [8000, 8001]
    assert mapping[9999] == [9000]


def test_listening_ports_by_pid_parses_ss(monkeypatch):
    """Linux listeners are mapped from pid to the ports they hold."""

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=SS_OUTPUT, stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", False)

    mapping = listening_ports_by_pid()

    assert mapping is not None
    assert mapping[4321] == [8000]
    assert mapping[9999] == [9000]


def test_listening_ports_by_pid_can_be_narrowed_to_one_pid(monkeypatch):
    """Asking about one pid answers about that pid only."""

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=LSOF_OUTPUT, stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    mapping = listening_ports_by_pid(4321)

    assert mapping == {4321: [8000, 8001]}


def test_listening_ports_by_pid_failure_is_none(monkeypatch):
    """A failed probe is ``None``, so the caller can fail closed.

    ``None`` and ``{}`` mean opposite things here: ``None`` is "the ports
    could not be determined" (do not kill anything), ``{}`` is "the ports
    were determined and there are none" (the process may be reaped).
    """

    def fake_run(cmd, *args, **kwargs):
        raise OSError("lsof is not there")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    assert listening_ports_by_pid() is None


def test_listening_ports_by_pid_of_the_live_machine():
    """The live probe answers with a mapping or with ``None`` — never raises."""
    mapping = listening_ports_by_pid()

    assert mapping is None or isinstance(mapping, dict)
    if mapping is not None:
        for pid, ports in mapping.items():
            assert isinstance(pid, int)
            assert all(isinstance(port, int) for port in ports)


# --------------------------------------------------------------------------
# swap_usage_ratio
# --------------------------------------------------------------------------


def test_swap_usage_ratio_parses_the_macos_report(monkeypatch):
    """``vm.swapusage`` is read as a fraction of the total."""

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="total = 8192.00M  used = 4096.00M  free = 4096.00M  (encrypted)\n",
            stderr="",
        )

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    assert swap_usage_ratio() == pytest.approx(0.5)


def test_swap_usage_ratio_failure_is_none(monkeypatch):
    """A swap report that cannot be read is ``None``."""

    def fake_run(cmd, *args, **kwargs):
        raise OSError("sysctl is not there")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)

    assert swap_usage_ratio() is None


def test_swap_usage_ratio_of_the_live_machine():
    """The live reading is a fraction between 0 and 1, or ``None``."""
    ratio = swap_usage_ratio()

    assert ratio is None or 0.0 <= ratio <= 1.0


# --------------------------------------------------------------------------
# process_start_epoch
# --------------------------------------------------------------------------


def test_process_start_epoch_is_stable_and_identifies_the_process():
    """The start time is the process' own, and does not move."""
    first = reaper.process_start_epoch(os.getpid())

    assert first
    assert first == reaper.process_start_epoch(os.getpid())
    assert first.isdigit()
