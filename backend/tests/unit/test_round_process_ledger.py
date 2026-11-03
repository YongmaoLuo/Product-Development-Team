"""The round-scoped process ledger.

What this file pins
-------------------
The judgement that a verification round is dead has to be followed by
the killing of everything that round left behind.  The sub-agent
handles and the disowned ``pytest`` orphans are covered by
``_cleanup_dead_verification_processes``; the coding tool the
**orchestrator** spawned is not, because nothing outside the tool
knows about its process.

So the processes have to be *attributed* before they can be killed, and
the attribution has to be finer than the plan: the rounds of one plan
share one plan directory, and a kill aimed at the plan would take the
next round's processes with it.  The attribution is the round, named
by its ``round_token``.

The criteria this file pins, one test each:

* a registration belongs to the round that was begun, and to no other
  round and no other plan directory;
* a kill reaches the round it was asked about and nothing else;
* the kill escalates — SIGTERM first, SIGKILL if the process ignores
  it;
* the kill reports what it did: the pid, the signal, the round and the
  reason;
* a registration outside a round is ignored rather than remembered
  somewhere it cannot be reached again;
* ending a round forgets its tools — and forgets them *without*
  killing them, because a round that finished did not leave orphans.

Hermeticity
------------
Everything happens in ``tmp_path``: the ledger is in memory, so the
plan directory is a key and never a file.  The only processes are the
two children this file spawns itself, and they are spawned in their
own session (``start_new_session=True``) so that a group signal
reaches them and only them — the coding tool spawns its child the same
way, and a child in the *test's* group would take the test session
down with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, List

import pytest

import round_process_ledger as ledger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    """A stand-in for a coding tool: it owns at most one subprocess."""

    def __init__(self, process: object = None) -> None:
        self._current_process = process


def _spawn_child(ignore_sigterm: bool = False) -> subprocess.Popen:
    """A real child process, in a session of its own.

    The child that ignores SIGTERM says ``ready`` only *after* the
    handler is installed, so the test cannot race the handler: a
    SIGTERM that arrives before ``signal.signal`` runs would kill the
    child outright, and the escalation it is testing would never
    happen.
    """
    if ignore_sigterm:
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('ready\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
    else:
        code = "import time; time.sleep(60)"
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if ignore_sigterm:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
    return proc


def _spawn_exiting() -> subprocess.Popen:
    """A child that has already done its job and is on its way out."""
    return subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def spawn() -> Callable[..., subprocess.Popen]:
    """Spawn children that are killed again when the test is over."""
    children: List[subprocess.Popen] = []

    def _spawn(ignore_sigterm: bool = False) -> subprocess.Popen:
        proc = _spawn_child(ignore_sigterm)
        children.append(proc)
        return proc

    yield _spawn

    for proc in children:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - defensive
            pass


@pytest.fixture(autouse=True)
def _clean_ledger():
    """The ledger is process-wide state; no test may lend its round to the next."""
    ledger.reset_for_tests()
    yield
    ledger.reset_for_tests()


def _plan_dir(tmp_path: Path, name: str = "plan") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# The criteria
# ---------------------------------------------------------------------------


def test_register_tool_is_scoped_to_the_begun_round(tmp_path):
    """Each round gets the tools that were registered while it was running."""
    plan_dir = _plan_dir(tmp_path)
    tool_a = _FakeTool()
    tool_b = _FakeTool()

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, tool_a)
    ledger.begin_round(plan_dir, "round-b")
    ledger.register_tool(plan_dir, tool_b)

    assert ledger.tools_for_round(plan_dir, "round-a") == [tool_a]
    assert ledger.tools_for_round(plan_dir, "round-b") == [tool_b]


def test_kill_round_kills_only_that_rounds_tools(tmp_path, spawn):
    """The other round's process is still running after the kill."""
    plan_dir = _plan_dir(tmp_path)
    proc_a = spawn()
    proc_b = spawn()
    tool_a = _FakeTool(proc_a)
    tool_b = _FakeTool(proc_b)

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, tool_a)
    ledger.begin_round(plan_dir, "round-b")
    ledger.register_tool(plan_dir, tool_b)

    events = ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    )

    assert proc_a.poll() is not None, "round-a's process was not killed"
    assert proc_b.poll() is None, "round-b's process was killed by mistake"
    assert [event["pid"] for event in events] == [proc_a.pid]


def test_kill_round_leaves_another_plan_directory_alone(tmp_path, spawn):
    """Two plans can carry the same token; the kill stays in its plan."""
    plan_a = _plan_dir(tmp_path, "plan-a")
    plan_b = _plan_dir(tmp_path, "plan-b")
    proc_a = spawn()
    proc_b = spawn()

    ledger.begin_round(plan_a, "same-token")
    ledger.register_tool(plan_a, _FakeTool(proc_a))
    ledger.begin_round(plan_b, "same-token")
    ledger.register_tool(plan_b, _FakeTool(proc_b))

    events = ledger.kill_round(
        plan_a, "same-token", reason="judged_dead", plan_id="p1",
    )

    assert proc_a.poll() is not None, "the named plan's process was not killed"
    assert proc_b.poll() is None, "another plan's process was killed by mistake"
    assert [event["pid"] for event in events] == [proc_a.pid]


def test_kill_round_sigterms_then_sigkills(tmp_path, spawn, monkeypatch):
    """A process that ignores SIGTERM is escalated to SIGKILL."""
    monkeypatch.setattr(ledger, "SIGTERM_GRACE_SECONDS", 0.5)
    plan_dir = _plan_dir(tmp_path)
    proc = spawn(ignore_sigterm=True)

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, _FakeTool(proc))

    events = ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    )

    assert proc.poll() is not None, "the process survived the SIGKILL"
    assert [event["signal"] for event in events] == ["SIGTERM", "SIGKILL"]
    assert {event["pid"] for event in events} == {proc.pid}


def test_kill_round_records_pid_signal_and_reason(tmp_path, spawn):
    """The event carries the four fields the caller needs to write it down."""
    plan_dir = _plan_dir(tmp_path)
    proc = spawn()

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, _FakeTool(proc))

    events = ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    )

    assert len(events) == 1
    event = events[0]
    assert set(event) == {"pid", "signal", "round_token", "reason"}
    assert event["pid"] == proc.pid
    assert event["signal"] == "SIGTERM"
    assert event["round_token"] == "round-a"
    assert event["reason"] == "judged_dead"


def test_register_tool_without_begun_round_is_ignored(tmp_path):
    """A tool registered outside a round is dropped, not remembered."""
    plan_dir = _plan_dir(tmp_path)
    tool = _FakeTool()

    ledger.register_tool(plan_dir, tool)

    assert ledger.tools_for_round(plan_dir, "round-a") == []
    assert ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    ) == []


def test_end_round_drops_the_rounds_tools(tmp_path, spawn):
    """Ending a round forgets its tools — and does not kill them."""
    plan_dir = _plan_dir(tmp_path)
    proc = spawn()
    tool = _FakeTool(proc)

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, tool)
    assert ledger.tools_for_round(plan_dir, "round-a") == [tool]

    ledger.end_round(plan_dir, "round-a")

    assert ledger.tools_for_round(plan_dir, "round-a") == []
    assert ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    ) == []
    assert proc.poll() is None, "ending a round must not kill its process"


# ---------------------------------------------------------------------------
# The boundaries
# ---------------------------------------------------------------------------


def test_kill_round_with_an_unknown_or_empty_token_kills_nothing(tmp_path, spawn):
    """A token no round holds is not a licence to kill."""
    plan_dir = _plan_dir(tmp_path)
    proc = spawn()

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, _FakeTool(proc))

    assert ledger.kill_round(
        plan_dir, "no-such-round", reason="judged_dead", plan_id="p1",
    ) == []
    assert ledger.kill_round(
        plan_dir, "", reason="judged_dead", plan_id="p1",
    ) == []
    assert proc.poll() is None, "a process was killed for a token nobody began"


def test_kill_round_skips_a_tool_without_a_process(tmp_path):
    """A tool that has not started a subprocess has nothing to kill."""
    plan_dir = _plan_dir(tmp_path)

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, _FakeTool(None))

    assert ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    ) == []


def test_kill_round_records_nothing_for_a_process_that_already_exited(tmp_path):
    """A process that had already gone was never signalled, so nothing is recorded."""
    plan_dir = _plan_dir(tmp_path)
    proc = _spawn_exiting()
    proc.wait(timeout=10)

    ledger.begin_round(plan_dir, "round-a")
    ledger.register_tool(plan_dir, _FakeTool(proc))

    assert ledger.kill_round(
        plan_dir, "round-a", reason="judged_dead", plan_id="p1",
    ) == []


def test_end_round_leaves_the_other_rounds_tools_alone(tmp_path, spawn):
    """Ending one round does not empty the next one."""
    plan_dir = _plan_dir(tmp_path)
    proc_b = spawn()
    tool_b = _FakeTool(proc_b)

    ledger.begin_round(plan_dir, "round-a")
    ledger.begin_round(plan_dir, "round-b")
    ledger.register_tool(plan_dir, tool_b)

    ledger.end_round(plan_dir, "round-a")

    assert ledger.tools_for_round(plan_dir, "round-b") == [tool_b]
