"""Plan-scoped cooperative stop for in-flight dispatches (2026-11-20).

Why this file exists
--------------------
``_LIVE_DISPATCH_TOOLS`` was a global table with no plan attribution:
the only way to stop a dispatch was
:func:`coding_tool.stop_all_live_dispatches`, which stops **every**
registered child.  Stopping one plan therefore took every other plan's
dispatches down with it — a stop that was supposed to be scoped to one
plan was, in fact, global, and a plan that was still working lost its
subprocesses because a different plan was stopped.

So the registry now carries the plan a dispatch belongs to, and
:func:`coding_tool.stop_live_dispatches_for_plan` stops only the
dispatches of one plan.  The stop sequence itself is unchanged, and it
is still the cooperative one:

    stdout.close() -> wait(0.35 x budget) -> SIGTERM the group
    -> wait(0.25 x budget) -> SIGKILL the group -> wait(0.10 x budget)

Two properties are pinned here, and both of them are properties a
*global* stop does not have:

* **cooperative first** — a child that leaves on its own once its
  stdout is closed is never signalled at all.  A stop that signalled
  first would turn a resumable stop into a hard kill.
* **plan isolation** — stopping plan ``p1`` leaves plan ``p2``'s
  dispatch running, registered and untouched.

The tests do not touch real processes: a fake process records the
timeouts it was waited with and whether it is still alive, and the fake
``kill_process_group`` records the signals the group was sent.  The
assertions land on those recorded values, not on the implementation's
own counters.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool


class FakeStdout:
    """``process.stdout``'s stand-in: records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """``subprocess.Popen``'s stand-in, with a controllable ending.

    Two switches decide how the child ends:

    * ``exit_on_stdout_close`` — the child leaves on its own once its
      stdout is closed (the cooperative exit).  ``False`` makes it
      ignore the closed pipe, which is what forces the escalation.
    * ``dies_on_sigkill`` — the child leaves when its process group is
      SIGKILLed.  ``False`` makes it outlive the whole sequence, which
      is what a survivor looks like.

    ``wait`` records every timeout it was given: the budget is the only
    hard constraint on this path, and "it was quick anyway" must not be
    allowed to stand in for it.
    """

    def __init__(
        self,
        *,
        exit_on_stdout_close: bool = True,
        dies_on_sigkill: bool = True,
    ) -> None:
        self.pid = 515151
        self.stdout = FakeStdout()
        self.wait_timeouts: List[Optional[float]] = []
        self.exit_on_stdout_close = exit_on_stdout_close
        self.dies_on_sigkill = dies_on_sigkill
        self._exit_code: Optional[int] = None

    # -- the child's own state -------------------------------------------

    def _alive(self) -> bool:
        if self._exit_code is not None:
            return False
        if self.exit_on_stdout_close and self.stdout.closed:
            return False
        return True

    def kill_group(self) -> None:
        """What a real SIGKILL to the child's process group would do."""
        if self.dies_on_sigkill:
            self._exit_code = -9

    # -- the ``Popen`` surface the stop path uses ------------------------

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._alive():
            raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=timeout)
        return self._exit_code or 0

    def poll(self):
        return None if self._alive() else (self._exit_code or 0)


class FakeTool:
    """``ClaudeCodingTool``'s stand-in: the two fields the stop path uses."""

    def __init__(self, process: FakeProcess) -> None:
        self._current_process = process
        self._process_lock = threading.Lock()


@pytest.fixture
def sent_signals(monkeypatch) -> List[int]:
    """Replace ``kill_process_group`` with a recorder; return the signals."""
    sent: List[int] = []

    def _fake_kill_process_group(proc, *, sig=signal.SIGKILL, wait_timeout=5.0):
        sent.append(sig)
        if sig == signal.SIGKILL:
            proc.kill_group()
        return None

    monkeypatch.setattr(coding_tool, "kill_process_group", _fake_kill_process_group)
    return sent


def _reset_registry() -> None:
    """Empty the live-dispatch registry.

    ``unregister_live_dispatch`` is used rather than a bare
    ``dict.clear()`` because it is the one place that drops the plan
    attribution together with the tool — clearing the tool table alone
    would leave the attribution behind and the next test would inherit
    it.
    """
    for tool in list(coding_tool._LIVE_DISPATCH_TOOLS.values()):
        coding_tool.unregister_live_dispatch(tool)
    coding_tool._LIVE_DISPATCH_TOOLS.clear()


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is module-level state — no test may leak into another."""
    _reset_registry()
    yield
    _reset_registry()


# ---------------------------------------------------------------------------
# The stop itself
# ---------------------------------------------------------------------------


def test_cooperative_exit_sends_no_sigterm(sent_signals):
    """A child that leaves on its own is never signalled.

    The cooperative order is the whole point: the child's stdout is
    closed first, and the child leaves on its own terms.  A signal
    would make the stop a hard kill, and a hard kill is what turns a
    resumable stop into a full rerun.
    """
    proc = FakeProcess(exit_on_stdout_close=True)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p1", total_seconds=45.0,
    )

    assert proc.stdout.closed is True
    assert sent_signals == [], (
        "a child that leaves after its stdout is closed must not be "
        "signalled at all"
    )
    assert proc.poll() is not None
    assert survivors == []


def test_escalates_to_sigterm_group(sent_signals):
    """A child that ignores the closed stdout is escalated to.

    SIGTERM goes to the child's **process group** at 35% of the
    budget — the child leads its own group, so a signal to the process
    alone would leave its own children behind — and SIGKILL follows
    25% of the budget later.
    """
    proc = FakeProcess(exit_on_stdout_close=False)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p1", total_seconds=45.0,
    )

    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]
    budget = 45.0
    assert proc.wait_timeouts == pytest.approx([
        budget * 0.35,  # stdout closed -> SIGTERM
        budget * 0.25,  # SIGTERM ignored -> SIGKILL
        budget * 0.10,  # SIGKILL -> reaped
    ])
    assert proc.poll() is not None
    assert survivors == []


def test_other_plan_dispatch_survives(sent_signals):
    """Stopping one plan leaves the other plan's dispatch alone.

    This is the property the global stop did not have: before it, a
    stop of ``p1`` walked the whole registry and took ``p2``'s child
    down with it.
    """
    proc_a = FakeProcess(exit_on_stdout_close=True)
    proc_b = FakeProcess(exit_on_stdout_close=True)
    tool_a = FakeTool(proc_a)
    tool_b = FakeTool(proc_b)
    coding_tool.register_live_dispatch(tool_a, "p1")
    coding_tool.register_live_dispatch(tool_b, "p2")

    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p1", total_seconds=45.0,
    )

    # p1's dispatch is gone ...
    assert proc_a.stdout.closed is True
    assert proc_a.poll() is not None
    # ... and p2's is untouched: not closed, not signalled, still alive
    # and still registered.
    assert proc_b.stdout.closed is False
    assert proc_b.poll() is None
    assert sent_signals == []
    assert id(tool_b) in coding_tool._LIVE_DISPATCH_TOOLS
    assert survivors == []


def test_unknown_plan_is_noop(sent_signals):
    """A plan with no registered dispatch stops nothing."""
    proc = FakeProcess(exit_on_stdout_close=True)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p2", total_seconds=45.0,
    )

    assert survivors == []
    assert proc.stdout.closed is False
    assert sent_signals == []


# ---------------------------------------------------------------------------
# The edges around the plan attribution
# ---------------------------------------------------------------------------


def test_legacy_entry_without_plan_is_not_stopped(sent_signals):
    """A dispatch registered without a plan belongs to no plan.

    "Belongs to no plan" is not "belongs to every plan": a plan-scoped
    stop must not reach it.
    """
    proc = FakeProcess(exit_on_stdout_close=True)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool)

    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p1", total_seconds=45.0,
    )

    assert survivors == []
    assert proc.stdout.closed is False
    assert sent_signals == []


def test_empty_plan_id_is_noop(sent_signals):
    """An empty plan id names no plan."""
    proc = FakeProcess(exit_on_stdout_close=True)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    assert coding_tool.stop_live_dispatches_for_plan("") == []
    assert proc.stdout.closed is False
    assert sent_signals == []


def test_unregister_drops_the_plan_attribution(sent_signals):
    """Unregistering a tool takes its plan attribution with it.

    An attribution that outlived its tool would keep a plan-scoped stop
    taking on a child that is already gone.
    """
    proc = FakeProcess(exit_on_stdout_close=True)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    coding_tool.unregister_live_dispatch(tool)
    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p1", total_seconds=45.0,
    )

    assert survivors == []
    assert proc.stdout.closed is False
    assert sent_signals == []


def test_re_registration_moves_the_attribution(sent_signals):
    """Re-registering a tool under another plan moves it there."""
    proc = FakeProcess(exit_on_stdout_close=True)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")
    coding_tool.register_live_dispatch(tool, "p2")

    coding_tool.stop_live_dispatches_for_plan("p1", total_seconds=45.0)
    assert proc.stdout.closed is False, (
        "the dispatch belongs to p2 now — a stop of p1 must not reach it"
    )
    assert sent_signals == []

    coding_tool.stop_live_dispatches_for_plan("p2", total_seconds=45.0)
    assert proc.stdout.closed is True


def test_survivor_is_reported(sent_signals):
    """A child that outlives the whole sequence is reported, not hidden."""
    proc = FakeProcess(exit_on_stdout_close=False, dies_on_sigkill=False)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    survivors = coding_tool.stop_live_dispatches_for_plan(
        "p1", total_seconds=45.0,
    )

    assert survivors == [tool]
    assert proc.poll() is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [(5.0, 30.0), (45.0, 45.0), (100.0, 60.0)],
    ids=["below-min", "in-range", "above-max"],
)
def test_budget_is_clamped_to_30_60_seconds(sent_signals, given, expected):
    """The plan-scoped budget is clamped into [30, 60] seconds.

    Too small a budget turns a cooperative exit into a SIGKILL; too
    large a one parks the stop path for minutes.
    """
    proc = FakeProcess(exit_on_stdout_close=False)
    tool = FakeTool(proc)
    coding_tool.register_live_dispatch(tool, "p1")

    coding_tool.stop_live_dispatches_for_plan("p1", total_seconds=given)

    waited = sum(t for t in proc.wait_timeouts if t)
    assert waited == pytest.approx(0.7 * expected)


def test_global_stop_still_stops_every_plan(sent_signals):
    """The global stop is unchanged: it takes on every registered dispatch.

    The plan-scoped stop is an addition, not a replacement: the
    executor's own signal handler still has to stop everything it
    started.
    """
    proc_a = FakeProcess(exit_on_stdout_close=True)
    proc_b = FakeProcess(exit_on_stdout_close=True)
    tool_a = FakeTool(proc_a)
    tool_b = FakeTool(proc_b)
    coding_tool.register_live_dispatch(tool_a, "p1")
    coding_tool.register_live_dispatch(tool_b, "p2")

    stopped = coding_tool.stop_all_live_dispatches(10.0)

    assert stopped == 2
    assert proc_a.stdout.closed is True
    assert proc_b.stdout.closed is True
