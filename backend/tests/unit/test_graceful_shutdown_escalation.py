"""Escalation contract of ``ClaudeCodingTool._graceful_shutdown``.

The shutdown is three phases: stdin EOF → wait → SIGTERM the process
group → SIGKILL the process group.

The bug this file pins: phase 2 sent SIGTERM to the *group* but phase 3
escalated with ``process.kill()``, which signals only the lead process.
Its children survived the shutdown and the group kept running.  The fix
escalates through the same group helper, so both signals land on the
process group.

The three tests below are the contract:

  * ``test_sigkill_follows_sigterm_when_still_alive`` — SIGTERM did not
    reap the process → SIGKILL must follow, on the group.
  * ``test_no_sigkill_when_sigterm_reaped`` — SIGTERM reaped it → no
    SIGKILL (escalating a dead process is pointless signalling).
  * ``test_stdin_closed_first`` — stdin is closed before any signal is
    sent (stdin EOF is the cheapest way to stop a well-behaved child).
"""

import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from coding_tool import ClaudeCodingTool


def _make_tool() -> ClaudeCodingTool:
    """Build a tool without touching the provider database."""
    with patch.object(ClaudeCodingTool, "_load_provider_from_db", return_value={}):
        return ClaudeCodingTool()


class _FakeStdin:
    """Just enough of ``Popen.stdin`` to observe the close."""

    def __init__(self, events):
        self._events = events
        self.closed = False

    def close(self):
        self.closed = True
        self._events.append("stdin.close")


class _FakeProcess:
    """Minimal ``subprocess.Popen`` stand-in that never exits on its own.

    ``wait()`` always raises ``TimeoutExpired`` — the child is alive, so
    the shutdown has to escalate.  ``kill()`` is recorded rather than
    performed: reaching for the lead process instead of the group is
    exactly the defect under test.
    """

    def __init__(self, events, wait_error=None):
        self._events = events
        self._wait_error = (
            wait_error
            if wait_error is not None
            else subprocess.TimeoutExpired("claude", 120)
        )
        self.stdin = _FakeStdin(events)
        self.killed = False

    def wait(self, timeout=None):
        self._events.append("wait")
        raise self._wait_error

    def kill(self):
        self.killed = True
        self._events.append("process.kill")


def _recording_killer(sigs, result):
    """Build a ``kill_process_group`` stand-in recording the signals."""

    def fake_kill_process_group(proc, *, sig=signal.SIGKILL, wait_timeout=5.0):
        sigs.append(sig)
        return result

    return fake_kill_process_group


def test_sigkill_follows_sigterm_when_still_alive():
    """SIGTERM did not reap the process → SIGKILL follows, on the group."""
    events = []
    proc = _FakeProcess(events)
    tool = _make_tool()
    sigs = []

    with patch(
        "coding_tool.kill_process_group",
        side_effect=_recording_killer(sigs, None),
    ):
        tool._graceful_shutdown(proc)

    assert sigs == [signal.SIGTERM, signal.SIGKILL], (
        "a process that survived SIGTERM must be killed with SIGKILL, "
        f"and both signals go to the process group; got {sigs}"
    )
    assert not proc.killed, (
        "the escalation must go through the group helper, not process.kill() "
        "— the latter only signals the lead process and leaves its children "
        "running"
    )


def test_no_sigkill_when_sigterm_reaped():
    """SIGTERM reaped the process → no SIGKILL is sent."""
    events = []
    proc = _FakeProcess(events)
    tool = _make_tool()
    sigs = []

    with patch(
        "coding_tool.kill_process_group",
        side_effect=_recording_killer(sigs, 0),
    ):
        tool._graceful_shutdown(proc)

    assert sigs == [signal.SIGTERM], (
        "the process was already reaped by SIGTERM; escalating would signal "
        f"a process group that no longer exists; got {sigs}"
    )
    assert not proc.killed


def test_stdin_closed_first():
    """stdin is closed before any signal is sent."""
    events = []
    proc = _FakeProcess(events)
    tool = _make_tool()

    def fake_kill_process_group(proc_arg, *, sig=signal.SIGKILL, wait_timeout=5.0):
        events.append(f"signal:{sig}")
        return None

    with patch("coding_tool.kill_process_group", side_effect=fake_kill_process_group):
        tool._graceful_shutdown(proc)

    assert events[0] == "stdin.close", (
        f"stdin EOF is the cheapest shutdown and must come first; got {events}"
    )
    assert events.index("stdin.close") < events.index(f"signal:{signal.SIGTERM}")


def test_sigkill_attempted_when_process_group_is_gone():
    """A vanished process group still gets a SIGKILL attempt, and never raises.

    ``kill_process_group`` swallows ``ProcessLookupError`` internally and
    answers ``None`` — indistinguishable from "still alive".  The
    shutdown must therefore still try SIGKILL rather than give up.
    """
    events = []
    proc = _FakeProcess(events)
    tool = _make_tool()
    sigs = []

    with patch(
        "coding_tool.kill_process_group",
        side_effect=_recording_killer(sigs, None),
    ):
        tool._graceful_shutdown(proc)  # must not raise

    assert sigs == [signal.SIGTERM, signal.SIGKILL]


def test_wait_failure_other_than_timeout_is_swallowed():
    """A non-timeout ``wait()`` failure is swallowed and the shutdown escalates.

    ``_graceful_shutdown`` is a best-effort cleanup called from ``finally``
    blocks: letting an exception escape would replace the real error with
    a shutdown error and leave the child running.
    """
    events = []
    proc = _FakeProcess(events, wait_error=OSError("wait exploded"))
    tool = _make_tool()
    sigs = []

    with patch(
        "coding_tool.kill_process_group",
        side_effect=_recording_killer(sigs, None),
    ):
        tool._graceful_shutdown(proc)  # must not raise

    assert sigs == [signal.SIGTERM, signal.SIGKILL]