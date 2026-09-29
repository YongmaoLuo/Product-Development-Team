"""TDD tests for ``tests.integration.resume_test.restart``.

These tests pin the 4 contracts from the hard-restart spec:

  1. ``test_find_server_pid_via_lsof`` — ``lsof -ti:8000`` output is
     parsed into a single ``int`` PID.
  2. ``test_find_server_pid_none_when_no_process`` — lsof returns no
     PIDs (empty / non-zero exit) → ``None``.
  3. ``test_wait_ready_returns_when_port_open`` — once the socket is
     open, ``wait_ready`` returns the elapsed seconds.
  4. ``test_wait_ready_timeout`` — port never opens within the budget
     → ``TimeoutError`` is raised.

All tests use ``unittest.mock`` to patch the side-effecting boundaries
(``subprocess.run``, ``socket``, ``time.monotonic``) so they do NOT need
a running the backend or a real socket.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# Mirror the same sys.path bootstrap the other resume_test tests use so
# the import works regardless of which pytest rootdir is active.
_INTEGRATION_DIR = Path(__file__).resolve().parent.parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from resume_test import restart  # noqa: E402


# ---------------------------------------------------------------------------
# find_server_pid
# ---------------------------------------------------------------------------


def test_find_server_pid_via_lsof():
    """``lsof`` returning ``'12345\\n'`` → ``find_server_pid`` returns 12345."""
    fake = subprocess.CompletedProcess(
        args=["lsof", "-ti", ":8000"],
        returncode=0,
        stdout="12345\n",
        stderr="",
    )
    with mock.patch.object(restart.subprocess, "run", return_value=fake) as m_run:
        pid = restart.find_server_pid()

    assert pid == 12345
    assert isinstance(pid, int)
    # lsof was invoked with the canonical "-ti:PORT" form.
    assert m_run.call_count == 1
    called_args = m_run.call_args.args[0]
    assert called_args[0] == "lsof"
    assert called_args[1] == "-ti"
    assert called_args[2] == f":{restart.SERVER_PORT}"


def test_find_server_pid_none_when_no_process():
    """lsof empty output OR non-zero exit → ``None``."""
    # Case A: lsof ran cleanly but reported no PIDs.
    empty = subprocess.CompletedProcess(
        args=["lsof", "-ti", ":8000"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with mock.patch.object(restart.subprocess, "run", return_value=empty):
        assert restart.find_server_pid() is None

    # Case B: lsof exit code 1 (port not in use) — also no PID.
    not_found = subprocess.CompletedProcess(
        args=["lsof", "-ti", ":8000"],
        returncode=1,
        stdout="",
        stderr="lsof: no PIDs found",
    )
    with mock.patch.object(restart.subprocess, "run", return_value=not_found):
        assert restart.find_server_pid() is None

    # Case C: lsof binary not installed (FileNotFoundError) — also no PID.
    with mock.patch.object(
        restart.subprocess,
        "run",
        side_effect=FileNotFoundError("lsof missing"),
    ):
        assert restart.find_server_pid() is None


# ---------------------------------------------------------------------------
# wait_ready
# ---------------------------------------------------------------------------


def test_wait_ready_returns_when_port_open():
    """Successful ``socket.connect`` → return elapsed time (float)."""
    fake_socket = mock.MagicMock()
    fake_cm = mock.MagicMock()
    fake_cm.__enter__.return_value = fake_socket
    fake_cm.__exit__.return_value = False

    # Two monotonic readings so the returned delta is deterministic and
    # > 0 regardless of the host's actual clock resolution.
    monotonic_reads = iter([1000.0, 1000.05, 1000.10])
    sleeps: list[float] = []

    with mock.patch.object(
        restart.socket, "create_connection", return_value=fake_cm
    ) as m_connect, mock.patch.object(
        restart.time, "monotonic", side_effect=lambda: next(monotonic_reads)
    ), mock.patch.object(restart.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        elapsed = restart.wait_ready(timeout=15)

    assert isinstance(elapsed, float)
    # Floating-point subtraction of two monotonic readings is not exact
    # (``1000.05 - 1000.0`` ≈ 0.049999999999954525 in IEEE-754), so we
    # accept a 1ms tolerance rather than asserting strict equality.
    assert abs(elapsed - 0.05) < 1e-3
    # First connect attempt succeeded → no sleep.
    assert m_connect.call_count == 1
    # Connection target is the canonical host:port pair.
    assert m_connect.call_args.args[0] == (restart.SERVER_HOST, restart.SERVER_PORT)


def test_wait_ready_timeout():
    """Port stays closed past ``timeout`` → ``TimeoutError`` is raised."""
    fake_socket = mock.MagicMock()
    fake_cm = mock.MagicMock()
    fake_cm.__enter__.return_value = fake_socket
    fake_cm.__exit__.return_value = False

    # Wall-clock progression: start=2000.0, every monotonic() call adds
    # 1s so we blow past any reasonable timeout on the first probe.
    now = [2000.0]

    def monotonic() -> float:
        v = now[0]
        now[0] += 1.0
        return v

    sleeps: list[float] = []

    with mock.patch.object(
        restart.socket,
        "create_connection",
        side_effect=OSError("connection refused"),
    ) as m_connect, mock.patch.object(
        restart.time, "monotonic", side_effect=monotonic
    ), mock.patch.object(restart.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        raised: Exception
        try:
            restart.wait_ready(timeout=2)
        except TimeoutError as exc:
            raised = exc
        else:
            raise AssertionError(
                "expected TimeoutError, but no exception was raised"
            )

    # At least one connect attempt was made before the deadline.
    assert m_connect.call_count >= 1
    # The error message names the host:port and the timeout budget.
    msg = str(raised)
    assert str(restart.SERVER_PORT) in msg
    assert "2" in msg  # timeout value


# Silence unittest discovery if the file is ever loaded with a runner
# that expects TestCase subclasses.
if __name__ == "__main__":  # pragma: no cover
    unittest.main()
