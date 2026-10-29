"""Tests for the deterministic executor fixture.

Background
----------
The scheduler's end-to-end tests need something to stand in for the real
executor: a process with a **real pid**, a **real exit code**, a **real
process group**, and an exit that happens at a **time the test chose**.
A mock cannot supply any of those — a mocked pid is not a pid, a mocked
exit code is not an exit code, and a mocked process group is not
something a teardown can signal.

``backend/tests/e2e/fixtures/deterministic_executor.py`` is that stand-in.
It is a real process, driven entirely by environment variables:

===========================  =========================================
``PDT_E2E_EXIT_AFTER``       Seconds to stay alive before exiting.
                             Unset means *stay alive until killed*.
``PDT_E2E_EXIT_CODE``        The exit code to exit with (default 0).
``PDT_E2E_SPAWN_GRANDCHILD`` ``"1"`` forks one extra sleeping
                             grandchild into the same process group.
``PDT_E2E_PID_FILE``         Where to write ``pid`` and ``pgid``.
===========================  =========================================

It is installed into the real spawn path through the
``PDT_EXECUTOR_CMD`` port (``routes.execution._executor_command``), so the
process these tests observe is the process the production code would
have observed — same pid, same group, same log file.

What is observed, and how
--------------------------
The contract under test is a *process*, so the tests observe it the way
an operating system observes a process: through its exit status, through
the files it writes, and through the process table.  The process group
is read with ``ps -A -o pid=,pgid=`` and filtered by the group id the
executor itself reported — the group is a real group, and the test does
not take the executor's word for it.

Every test starts the executor with ``start_new_session=True``, which is
what the production spawn does too.  That gives the executor a process
group of its own, so counting the members of that group counts the
executor and nothing else — and the extra member that appears when the
grandchild is requested is the grandchild, seen from the outside.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

#: The fixture under test.  Resolved through this file's own location so
#: the path is right on every checkout — no absolute path is written down
#: anywhere, and the file is inside the repository.
EXECUTOR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "deterministic_executor.py"
)

#: The first token of every heartbeat line.  The grandchild prints
#: nothing, so a line starting with this token came from the executor.
HEARTBEAT = "heartbeat"

#: How long a helper waits for the executor to reach a state before it
#: gives up and reports what it saw.  Generous on purpose: the machine
#: running the test may be busy, and a timeout here means "not yet", not
#: "never".
SETTLE_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Executor:
    """A running executor: its process, its log, and its pid file."""

    def __init__(
        self,
        process: subprocess.Popen,
        log_path: Path,
        pid_file: Path,
    ) -> None:
        self.process = process
        self.log_path = log_path
        self.pid_file = pid_file
        self.pgid: Optional[int] = None

    @property
    def output(self) -> str:
        """Everything the executor wrote to stdout (and stderr)."""
        return self.log_path.read_text(encoding="utf-8")

    @property
    def heartbeats(self) -> List[str]:
        """The heartbeat lines, in the order they were written."""
        return [
            line
            for line in self.output.splitlines()
            if line.split()[:1] == [HEARTBEAT]
        ]

    def wait_for_pid_file(self) -> Tuple[int, int]:
        """Wait for the pid file and return the ``(pid, pgid)`` it names."""
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while time.monotonic() < deadline:
            if self.pid_file.exists():
                fields = self.pid_file.read_text(encoding="utf-8").split()
                if len(fields) == 2:
                    self.pgid = int(fields[1])
                    return int(fields[0]), self.pgid
            time.sleep(0.02)
        raise AssertionError(
            f"the executor did not write {self.pid_file} within "
            f"{SETTLE_TIMEOUT}s; its output was:\n{self.output}"
        )

    def kill_group(self) -> None:
        """Kill the executor's whole process group.

        This is the teardown the grandchild exists for: the group is
        signalled, not the single process, so a child that outlived its
        parent goes down with it.

        The group id is the one the executor *wrote down*, not one looked
        up from the process: a group outlives its leader, so the leader
        can be gone by the time the teardown runs — and looking the group
        up from a dead pid is exactly how a teardown silently stops
        reaching the residue it was written to remove.
        """
        pgid = self.pgid
        if pgid is None:
            try:
                pgid = os.getpgid(self.process.pid)
            except (ProcessLookupError, PermissionError):
                return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _start(tmp_path: Path, **env_overrides: str) -> _Executor:
    """Start the executor with *env_overrides* and return a handle.

    ``PDT_E2E_*`` is stripped from the inherited environment first: a
    value that happened to be set in the shell the test runs under must
    not decide what the fixture does.
    """
    env: Dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PDT_E2E_")
    }
    env.update(env_overrides)

    tmp_path.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "executor.log"
    pid_file = tmp_path / "executor.pid"
    env["PDT_E2E_PID_FILE"] = str(pid_file)

    with open(log_path, "wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", str(EXECUTOR)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            # The production spawn does the same, and it is what gives
            # the executor a process group of its own.
            start_new_session=True,
        )

    return _Executor(process, log_path, pid_file)


def _group_members(pgid: int) -> List[int]:
    """The pids of every process in the process group *pgid*.

    Read from the process table rather than from anything the executor
    said about itself: the group is a real group, and the count is
    taken from where the operating system keeps it.
    """
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    members: List[int] = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        pid, group = int(fields[0]), int(fields[1])
        if group == pgid:
            members.append(pid)
    return members


def _wait_for_group_size(pgid: int, size: int) -> List[int]:
    """Wait until the group *pgid* has *size* members; return them.

    Returns whatever the group holds when the wait is over, so a caller
    that expected a different size can say what it actually saw.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    members: List[int] = []
    while time.monotonic() < deadline:
        members = _group_members(pgid)
        if len(members) >= size:
            return members
        time.sleep(0.02)
    return members


def _wait_until_gone(pgid: int) -> List[int]:
    """Wait until the group *pgid* is empty; return the last members seen."""
    deadline = time.monotonic() + SETTLE_TIMEOUT
    members: List[int] = []
    while time.monotonic() < deadline:
        members = _group_members(pgid)
        if not members:
            return members
        time.sleep(0.02)
    return members


# ---------------------------------------------------------------------------
# 1. The heartbeat reaches stdout
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_heartbeat_lines_written(tmp_path):
    """A running executor writes a heartbeat line to stdout.

    The heartbeat is how the test knows the executor is *alive* rather
    than merely started: it is written to the real stdout, so the
    production drain — which sends stdout to the executor's log file —
    picks it up, and the log is what the status route tails.

    The count is a rate, not a wall-clock guess: the fixture emits one
    line per 100 ms slot and catches up rather than dropping a slot
    when the machine is busy, so 0.4 seconds of life is at least three
    lines however the scheduler treated the process.
    """
    executor = _start(tmp_path, PDT_E2E_EXIT_AFTER="0.4")

    started = time.monotonic()
    returncode = executor.process.wait(timeout=SETTLE_TIMEOUT)
    elapsed = time.monotonic() - started

    assert returncode == 0, (
        f"the executor exited with {returncode}, not 0; its output was:\n"
        f"{executor.output}"
    )

    beats = executor.heartbeats
    assert len(beats) >= 3, (
        f"expected at least 3 heartbeat lines from 0.4 seconds of life, "
        f"got {len(beats)}:\n{executor.output}"
    )
    assert elapsed >= 0.35, (
        f"the executor must have stayed alive for the requested 0.4s; "
        f"it exited after {elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# 2. The exit code is the one that was asked for
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_exit_code_honored(tmp_path):
    """The exit code is ``PDT_E2E_EXIT_CODE``, not a default.

    A stand-in that always exited 0 could not tell a scheduler apart
    from one that finished its work: "succeeded" and "failed" are the
    same event unless the exit code carries the difference.  The code
    asked for has to come back, and it has to come back from a real
    process — the shell's own view of the exit status.
    """
    executor = _start(
        tmp_path,
        PDT_E2E_EXIT_AFTER="0.3",
        PDT_E2E_EXIT_CODE="7",
    )

    returncode = executor.process.wait(timeout=SETTLE_TIMEOUT)

    assert returncode == 7, (
        f"the executor exited with {returncode}, not the requested 7; "
        f"its output was:\n{executor.output}"
    )


# ---------------------------------------------------------------------------
# 3. The pid file names the pid and the group
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_pid_file_written(tmp_path):
    """The pid file holds the executor's own pid and its process group.

    Two integers, because a teardown needs both: the pid to check that
    the process is gone, and the group to signal everything that was
    left behind with it.  The pid is checked against the pid the test
    itself holds — the number in the file has to be the number of the
    process that wrote it, not a number the fixture made up.
    """
    executor = _start(tmp_path, PDT_E2E_EXIT_AFTER="0.5")

    pid, pgid = executor.wait_for_pid_file()

    assert pid == executor.process.pid, (
        f"the pid file names {pid}, but the process that wrote it is "
        f"{executor.process.pid}"
    )
    assert pgid == pid, (
        f"the executor was started in its own session, so its group id "
        f"must be its own pid; the file says pgid={pgid}, pid={pid}"
    )

    executor.process.wait(timeout=SETTLE_TIMEOUT)


# ---------------------------------------------------------------------------
# 4. The grandchild is a real extra process
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_grandchild_spawned(tmp_path):
    """``PDT_E2E_SPAWN_GRANDCHILD=1`` adds one more process to the group.

    The grandchild is the teardown's target.  A scheduler that stops a
    run by signalling the *group* is only correct if everything the run
    started went down with it, and the only way to show that is to
    leave something behind on purpose and then check that it is gone
    afterwards.

    So the test asks for the grandchild, finds it in the process table
    as a second member of the executor's group, waits for the executor
    to exit — which the grandchild outlives — and then kills the group
    and checks that nothing is left in it.
    """
    # Without the flag the group holds the executor and nothing else.
    plain = _start(tmp_path / "plain", PDT_E2E_EXIT_AFTER="1.0")
    plain_pid, plain_pgid = plain.wait_for_pid_file()
    plain_members = _wait_for_group_size(plain_pgid, 1)
    plain.kill_group()
    plain.process.wait(timeout=SETTLE_TIMEOUT)

    assert plain_members == [plain_pid], (
        f"without the grandchild the group must hold the executor alone; "
        f"it held {plain_members}"
    )

    # With the flag there is one more member, and it is not the executor.
    spawned = _start(
        tmp_path / "spawned",
        PDT_E2E_EXIT_AFTER="1.0",
        PDT_E2E_SPAWN_GRANDCHILD="1",
    )
    spawned_pid, spawned_pgid = spawned.wait_for_pid_file()
    members = _wait_for_group_size(spawned_pgid, 2)

    assert len(members) == 2, (
        f"the grandchild must add exactly one process to the group; "
        f"the group held {members}"
    )
    assert spawned_pid in members, (
        f"the executor {spawned_pid} is not among the group members "
        f"{members}"
    )
    grandchild = [pid for pid in members if pid != spawned_pid]
    assert len(grandchild) == 1, (
        f"expected one member besides the executor; got {grandchild}"
    )
    grandchild_pid = grandchild[0]

    # The grandchild outlives the executor — that is what makes it
    # residue, and residue is what the teardown has to remove.
    spawned.process.wait(timeout=SETTLE_TIMEOUT)
    assert grandchild_pid in _group_members(spawned_pgid), (
        f"the grandchild {grandchild_pid} must still be running after "
        f"the executor exited; the group holds {_group_members(spawned_pgid)}"
    )

    # Killing the group takes the grandchild with it.
    spawned.kill_group()
    remaining = _wait_until_gone(spawned_pgid)
    assert remaining == [], (
        f"the process group still holds {remaining} after it was killed; "
        f"the grandchild {grandchild_pid} was left behind"
    )


# ---------------------------------------------------------------------------
# 5. Without a deadline, the executor stays alive
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_parks_when_exit_after_is_unset(tmp_path):
    """No ``PDT_E2E_EXIT_AFTER`` means the executor does not exit.

    The scheduler's tests need an executor that is still running when
    the test looks at it — one that stops on its own would turn every
    observation into a race.  So "no deadline" has to mean "no exit",
    and the only way to stop it is to signal it.
    """
    executor = _start(tmp_path)

    executor.wait_for_pid_file()
    time.sleep(0.5)

    assert executor.process.poll() is None, (
        f"the executor exited on its own with {executor.process.returncode}; "
        f"without a deadline it must stay alive"
    )
    assert len(executor.heartbeats) >= 3, (
        f"a parked executor must keep writing heartbeats; got "
        f"{len(executor.heartbeats)}:\n{executor.output}"
    )

    executor.kill_group()
    executor.process.wait(timeout=SETTLE_TIMEOUT)