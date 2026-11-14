"""The executor outlives the backend that started it.

Background
----------
The executor's output reaches the plan directory's log through a pipe, so
the log can be bounded (rotated) instead of growing until the disk says
stop.  A pipe needs a reader, and the reader used to be the backend.

A backend that dies takes its reader with it.  The pipe's write end then
has nobody on the other side, and the executor's next write fails with
``EPIPE`` — which kills an executor that writes a heartbeat every hundred
milliseconds.  So a ``kill -9`` of the backend took every run that was
working at the time with it, and a run that was supposed to outlive the
backend did not: the scheduler found no process to adopt, and the plan
sat in ``stopped`` while the work it was doing was gone.

The reader that is not the backend is the :mod:`log_guardian` guardian.

What the test below pins
------------------------
* **the executor survives the death of the backend** — the run that was
  working is still working afterwards;
* **and it keeps being logged** — the bytes the executor writes after
  the backend is gone still land in the plan's log, through the bounded
  writer;
* **and the log stays bounded** — the log is still rotated under its
  cap while nobody but the guardian is reading the pipe.

The shape of the case
---------------------
The backend here is a **real process**, not a simulation of one: a
child that starts the executor through the production spawn
(``routes.execution._spawn_executor_subprocess`` — the one and only
place the executor is started) and then does nothing but wait.  The test
kills *that* process with ``SIGKILL``, exactly as a crash kills a
backend: no cleanup, no chance to put anything in order.

A test that called the guardian directly would prove something much
weaker — that a helper process can hold a pipe open.  The failure being
prevented here is a signal delivered to a real process, and only a real
backend dying can deliver it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


#: ``<repo>/backend`` — the directory the application lives in.
BACKEND_DIR: Path = Path(__file__).resolve().parents[2]

#: ``<repo>`` — the checkout the launcher is booted from.
REPO_ROOT: Path = BACKEND_DIR.parent


#: A writer that keeps writing, for as long as it is left alone.
#:
#: It writes to its real stdout and flushes every line: the flush is
#: what makes a blocked write a *blocked write* rather than a buffered
#: one, and a blocked write into a pipe nobody reads is what the
#: ``EPIPE`` turns into a dead executor.
WRITER = """
import sys
import time

index = 0
while True:
    sys.stdout.write("heartbeat %d\\n" % index)
    sys.stdout.flush()
    index += 1
    time.sleep(0.02)
"""


#: The backend stand-in.
#:
#: It boots the application the way the server does — ``server`` first,
#: because the route module late-binds into the application module and
#: ``server`` seeds ``sys.modules['server']`` before importing it —
#: starts the executor through the production spawn, writes down what it
#: started, and then waits to be killed.
LAUNCHER = r'''
import json
import os
import sys
import time
from pathlib import Path

backend_dir = Path(sys.argv[1])
plan_id = sys.argv[2]
log_path = Path(sys.argv[3])
info_path = Path(sys.argv[4])
project_dir = Path(sys.argv[5])
tasks_file = Path(sys.argv[6])
max_bytes = int(sys.argv[7])

sys.path.insert(0, str(backend_dir))
sys.path.insert(0, str(backend_dir.parent))

import bounded_log

# The bound is shrunk before the spawn, the way the other bounded-log
# cases shrink it: 10MB of heartbeat is more than a case should write
# to prove that a rotation happened.  The sink reads the bound when it
# is asked for it, and the guardian is handed the same number, so the
# cap the guardian enforces is this one.
bounded_log.MAX_PLAN_LOG_BYTES = max_bytes

import server  # noqa: F401 - seeds sys.modules for the route module
from routes import execution as routes_execution

process, returned_log = routes_execution._spawn_executor_subprocess(
    plan_id=plan_id,
    backend_dir=backend_dir,
    project_dir=project_dir,
    tasks_file=tasks_file,
    tool=None,
    extra_args=None,
    log_path=log_path,
)

info_path.write_text(
    json.dumps({"pid": process.pid, "log": str(returned_log)}),
    encoding="utf-8",
)

# The backend does nothing but wait.  It has no shutdown handler: the
# test kills it, and a ``kill -9`` is exactly the case the guardian
# exists for.
while True:
    time.sleep(3600)
'''


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


def _wait_for_file(path: Path, timeout: float = 30.0) -> Dict[str, Any]:
    """Wait until the launcher has written down what it started."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    raise AssertionError(
        f"the launcher wrote nothing to {path} within {timeout}s"
    )


def _log_lines(path: Path) -> int:
    """How many lines the log holds, or 0 when it does not exist yet."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


#: The bound the cases run against.
#:
#: Small enough that a heartbeat fills it in a couple of seconds, big
#: enough that a rotation is a real rotation and not a truncation of
#: every line.
MAX_BYTES = 2048


@pytest.fixture
def backend(tmp_path):
    """A running backend, its executor, and the handles to kill both.

    Yields a dict with the launcher's pid, the executor's pid, the
    plan's log path and the bound the log is held to.  Everything it
    started is killed when the case ends: a test that leaves an
    executor behind is a test that changes the machine it ran on.
    """
    plan_id = "p-outlives-backend"
    log_path = tmp_path / "logs" / "executor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    info_path = tmp_path / "started.json"

    writer = tmp_path / "writer.py"
    writer.write_text(WRITER, encoding="utf-8")

    project_dir = tmp_path / "worktree"
    project_dir.mkdir(exist_ok=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")

    env = dict(os.environ)
    env["PDT_EXECUTOR_CMD"] = " ".join([sys.executable, str(writer)])

    process = subprocess.Popen(
        [
            sys.executable,
            str(launcher),
            str(BACKEND_DIR),
            plan_id,
            str(log_path),
            str(info_path),
            str(project_dir),
            str(tasks_file),
            str(MAX_BYTES),
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    executor_pid: Optional[int] = None
    try:
        info = _wait_for_file(info_path)
        executor_pid = int(info["pid"])

        # The executor has to be writing before the backend is killed:
        # a run that never produced a line proves nothing about the
        # writes that come after the kill.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _log_lines(log_path) < 3:
            time.sleep(0.1)
        assert _log_lines(log_path) >= 3, (
            "the executor never wrote to its log; the case would prove "
            "nothing"
        )

        yield {
            "launcher": process,
            "launcher_pid": process.pid,
            "executor_pid": executor_pid,
            "log_path": log_path,
            "max_bytes": MAX_BYTES,
        }
    finally:
        if executor_pid:
            try:
                os.kill(executor_pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def test_the_executor_outlives_the_backend(backend):
    """The run that was working is still working after the backend dies.

    Without the guardian, closing the pipe's read end is what a dying
    backend does, and the executor's next write is a
    ``BrokenPipeError`` — the executor is dead a moment after the
    backend, and the scheduler has nothing left to adopt.
    """
    launcher_pid = backend["launcher_pid"]
    executor_pid = backend["executor_pid"]

    # The backend dies the way a crash kills it: SIGKILL, no cleanup.
    os.kill(launcher_pid, signal.SIGKILL)
    try:
        os.waitpid(launcher_pid, 0)
    except ChildProcessError:
        pass

    time.sleep(1.5)

    assert _alive(executor_pid), (
        f"the executor (pid {executor_pid}) died with the backend "
        f"(pid {launcher_pid}); the pipe broke under it"
    )


def test_the_executor_keeps_being_logged_after_the_backend_dies(backend):
    """The bytes written after the backend is gone still reach the log.

    The guardian does not only hold the pipe open: it takes the
    draining over, so the run that outlived the backend is still being
    logged — into the same bounded log.
    """
    launcher_pid = backend["launcher_pid"]
    log_path = backend["log_path"]

    before = _log_lines(log_path)
    assert before >= 3, "the executor never wrote to its log"

    os.kill(launcher_pid, signal.SIGKILL)
    try:
        os.waitpid(launcher_pid, 0)
    except ChildProcessError:
        pass

    deadline = time.monotonic() + 15.0
    after = before
    while time.monotonic() < deadline:
        time.sleep(0.2)
        after = _log_lines(log_path)
        if after > before + 3:
            break

    assert after > before + 3, (
        f"the log did not grow after the backend died: {before} lines "
        f"before, {after} after — nobody was reading the pipe"
    )


def test_the_log_stays_bounded_while_the_guardian_reads(backend):
    """The log is still rotated under its cap with the guardian reading.

    The bound is the reason the executor's output goes through a pipe
    at all.  The guardian is the reader now, and the bound has to hold
    while it reads — a bounded writer nobody rotates is the defect the
    pipe was introduced to fix.
    """
    launcher_pid = backend["launcher_pid"]
    log_path = backend["log_path"]
    cap = backend["max_bytes"]

    os.kill(launcher_pid, signal.SIGKILL)
    try:
        os.waitpid(launcher_pid, 0)
    except ChildProcessError:
        pass

    # The writer the guardian uses bounds the log at the number the
    # spawn was given.  The log is written past that bound here, so the
    # rotation has to have happened.
    deadline = time.monotonic() + 30.0
    rotated = False
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if log_path.with_name(log_path.name + ".1").exists():
            rotated = True
            break

    assert rotated, (
        f"the log never rotated under the {cap}B cap while the guardian "
        f"was reading it"
    )
    for path in sorted(log_path.parent.glob(log_path.name + "*")):
        size = path.stat().st_size
        assert size <= cap, f"{path.name} is {size} bytes, over the {cap}B cap"
