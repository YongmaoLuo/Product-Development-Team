"""Hard-restart helper for the autonomous-coding backend.

The ``hard_restart`` function is the entry point used by the orchestrator
to bounce the backend server (kill the old process, start a fresh one, and
wait for the new instance to be ready). It is split into four small
single-purpose functions so each step can be unit-tested in isolation
without touching a real running server:

  * ``find_server_pid``    — locate the PID listening on :8000 via ``lsof``.
  * ``kill_server``        — SIGTERM with SIGKILL escalation after a timeout.
  * ``start_server``       — spawn the new process with ``nohup`` + venv python.
  * ``wait_ready``         — poll the TCP port until the server accepts a
                             connection (or raise ``TimeoutError``).
  * ``hard_restart``       — orchestrate the four steps above.

The functions deliberately avoid external dependencies (psutil, requests,
…) so they can run inside the project's base Python environment during
test bootstrap.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The port the backend listens on. Centralised so tests can mock around
# it without scattering the literal ``8000`` across call sites.
SERVER_PORT = 8000
SERVER_HOST = "127.0.0.1"

# How often the readiness probe retries ``socket.connect`` while waiting
# for the new server to come up. 100ms is the canonical the backend default default
# (matches ``VerificationAgent`` heartbeat cadence).
WAIT_READY_POLL_SECONDS = 0.1

# Project-relative paths. The restart helper is invoked from the project
# root (where ``backend/.venv/`` and ``backend/server.py`` live), so we
# anchor every path on the current working directory rather than on
# ``__file__`` (which would point at ``backend/tests/integration/...``).
PROJECT_ROOT = Path(os.getcwd())
VENV_PYTHON = PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"
SERVER_SCRIPT = PROJECT_ROOT / "backend" / "server.py"


# ---------------------------------------------------------------------------
# PID discovery
# ---------------------------------------------------------------------------


def find_server_pid() -> Optional[int]:
    """Return the PID listening on ``SERVER_PORT`` (``8000``), or ``None``.

    Implementation: ``lsof -ti:8000`` returns one PID per line for every
    process holding the port. We pick the first one. If lsof is missing
    or the port is free, we return ``None`` — callers must treat "no
    PID" as a normal state, not an error (the orchestrator uses it to
    decide whether a kill is even needed).
    """
    try:
        completed = subprocess.run(
            ["lsof", "-ti", f":{SERVER_PORT}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if completed.returncode != 0:
        return None

    raw = (completed.stdout or "").strip()
    if not raw:
        return None

    # lsof may print multiple PIDs (one per line). The first PID is the
    # one that holds the listening socket; the rest are typically
    # children that have inherited the file descriptor.
    first = raw.splitlines()[0].strip()
    try:
        return int(first)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Kill with SIGTERM → SIGKILL escalation
# ---------------------------------------------------------------------------


def _is_alive(pid: int) -> bool:
    """Return ``True`` iff the process ``pid`` is still alive.

    Uses ``os.kill(pid, 0)`` (the no-op signal) which raises
    ``ProcessLookupError`` if the process is gone and ``PermissionError``
    if it belongs to another user. We treat any non-``ProcessLookupError``
    exception as "still alive" — the kill may simply need escalated
    privileges, which we don't try to handle here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def kill_server(pid: int, timeout: float = 5) -> str:
    """Kill ``pid`` with SIGTERM, escalating to SIGKILL after ``timeout`` seconds.

    Returns the signal that actually terminated the process:

      * ``"SIGTERM"``  — process exited within ``timeout`` after the
        first SIGTERM.
      * ``"SIGKILL"``  — process ignored SIGTERM past ``timeout``; we
        escalated to SIGKILL which is unblockable.

    A missing process is treated as "killed by SIGTERM" (it must have
    already terminated between the PID lookup and our kill attempt).
    """
    if not _is_alive(pid):
        return "SIGTERM"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "SIGTERM"
    except PermissionError:
        # Not our process; SIGKILL also won't work, but the orchestrator
        # is expected to surface the PermissionError via its own error
        # path. Here we just return the signal we tried first.
        return "SIGTERM"

    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return "SIGTERM"
        time.sleep(0.05)

    # Grace period elapsed; escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    # Give the kernel a brief moment to reap.
    time.sleep(0.1)
    return "SIGKILL"


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_server(log_path: str) -> int:
    """Start ``backend/server.py`` via the project venv's Python and return its PID.

    The process is launched with ``nohup``, ``stdout`` and ``stderr``
    redirected to ``log_path`` (truncated each call), and a fresh
    process group (``start_new_session=True``) so it survives the
    orchestrator's own exit.

    On POSIX systems ``nohup`` is ``execvp``-style, so the returned
    ``Popen.pid`` is the python interpreter's pid — exactly the value
    the orchestrator needs to track the running server.
    """
    log_file = open(log_path, "w", buffering=1)
    try:
        # ``nohup`` on macOS / Linux replaces the process image with the
        # target via exec, so the final pid reported by Popen is the
        # python interpreter's pid, not nohup's wrapper.
        proc = subprocess.Popen(
            [
                "nohup",
                str(VENV_PYTHON),
                str(SERVER_SCRIPT),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # Popen has dup'd the fd; closing ours here is safe and
        # prevents the parent from leaking a file descriptor.
        log_file.close()

    return int(proc.pid)


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


def wait_ready(timeout: float = 15) -> float:
    """Block until ``SERVER_PORT`` accepts a TCP connection.

    Returns the elapsed wall-clock seconds (float) measured from the
    first probe to the successful one. Raises ``TimeoutError`` if the
    port is still not accepting connections after ``timeout`` seconds.
    """
    start = time.monotonic()
    deadline = start + max(0.0, float(timeout))

    while True:
        try:
            with socket.create_connection(
                (SERVER_HOST, SERVER_PORT), timeout=0.5
            ):
                return time.monotonic() - start
        except OSError:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"server on {SERVER_HOST}:{SERVER_PORT} did not become "
                    f"ready within {timeout}s"
                )
            # Sleep the configured poll interval, but never overshoot
            # the deadline by more than a single poll period.
            remaining = deadline - now
            time.sleep(min(WAIT_READY_POLL_SECONDS, max(0.0, remaining)))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def hard_restart(log_path: str = "/tmp/server.log") -> dict:
    """Kill the old server (if any) and start a fresh one, waiting for ready.

    Returns a dict with four keys:

      * ``old_pid``       — the PID of the killed server, or ``None`` if
        no server was listening when we started.
      * ``new_pid``       — the PID of the freshly-launched server.
      * ``kill_signal``   — ``"SIGTERM"`` / ``"SIGKILL"`` / ``None`` (no
        kill needed because no old server was running).
      * ``ready_in_sec``  — wall-clock seconds from spawn to "port
        accepting connections".

    The function deliberately does not perform a pre-flight plan
    inventory check (that's the orchestrator's job — see
    ``inventory.inventory_safe_to_restart``). It only owns the
    bounce-the-process lifecycle.
    """
    old_pid = find_server_pid()
    if old_pid is None:
        kill_signal: Optional[str] = None
    else:
        kill_signal = kill_server(old_pid, timeout=5)

    new_pid = start_server(log_path)
    ready_in_sec = wait_ready(timeout=15)

    return {
        "old_pid": old_pid,
        "new_pid": new_pid,
        "kill_signal": kill_signal,
        "ready_in_sec": ready_in_sec,
    }
