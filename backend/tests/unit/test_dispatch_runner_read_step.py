"""The dispatch's read step: a refused dispatch, and a closed pipe.

Two things can go wrong inside ``_DispatchRunner._collect_events`` --
the step that reads the child's stream -- and they have to leave the
dispatch differently.

**A refused dispatch.**  A plan dispatch that cannot register itself
is refused by the fail-closed registration (2026-11-20): the
registration *raises* instead of returning an empty pair, so a dispatch
that cannot be watched does not run unwatched.  That refusal travels
out of the read step **untouched** -- a replaced exception reads as a
provider failure and gets retried -- and the read step's ``finally``
reaps the child the dispatch had already spawned.  The child stays
visible in ``_current_process``: the reaped child is the evidence that
nothing ran unwatched.

**A closed pipe.**  The child's stdout closed under the dispatch's
feet, and the read raises ``ValueError``.  That one is *not* a crash:
the read step records it in ``read_error`` and answers with no events,
and the dispatch turns a read error that carries no answer into an
idle timeout (``coding_tool.py`` reads ``runner.read_error`` for
exactly that).

Both are driven here through the runner's own entry point --
``_DispatchRunner.run``, the call the dispatch itself makes -- with the
spawn faked out: no child is started, no second is slept, and nothing
is fetched.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import claude_sandbox  # noqa: E402
import coding_tool as ct  # noqa: E402
import harness.runner as harness_runner  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from harness.contract import HarnessInvocation  # noqa: E402
from verification_cancel import VerificationCancelled  # noqa: E402

#: The checkout these tests run in.  Every coding tool built
#: in this file names it as its working root -- the tool cannot be
#: born without one, and a root that is not named runs the child
#: in the backend server's directory instead.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class _ClosedPipe:
    """A stdout that raises ``ValueError`` when it is read.

    This is what a closed pipe looks like from the read side: the
    file object is closed under the reader's feet and every read
    answers with ``ValueError: I/O operation on closed file``.
    """

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        self.closed = True


class _FakeStdin:
    """The child's standard input, as a pipe the dispatch can write to."""

    def __init__(self) -> None:
        self.written = []
        self.closed = False

    def write(self, text) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeChild:
    """A child that was never spawned, standing in for the real one.

    It records what the dispatch did to it -- the waits, the kill --
    so a test can say the child was reaped without starting one.
    """

    def __init__(self, stdout) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout
        self.stderr = io.StringIO("")
        #: The test process's own pid.  The runner refuses to signal
        #: its own process group, so no other process is ever reached
        #: from here -- the child below is the only thing that is
        #: ever "killed".
        self.pid = os.getpid()
        self.waits = []
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.killed = True
        self.returncode = -15


class _RawHarness:
    """A harness with no parser of its own: the raw lines are events."""

    name = "raw"

    def parse_stream(self, lines):
        return list(lines)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No provider detection, no sandbox profile, no real spawn."""
    monkeypatch.setattr(
        ClaudeCodingTool, "_load_provider_from_db", staticmethod(lambda provider_name: {})
    )
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None
    )


def _tool() -> ClaudeCodingTool:
    """A coding tool with no child behind it."""
    return ClaudeCodingTool(cwd=_REPO_ROOT)


def _install_fake_spawn(monkeypatch, child) -> None:
    """Make the runner's spawn hand back ``child`` instead of starting one."""
    def _fake_popen(*args, **kwargs):
        return child

    monkeypatch.setattr(
        harness_runner,
        "subprocess",
        SimpleNamespace(Popen=_fake_popen, PIPE=-1),
    )


def _invocation() -> HarnessInvocation:
    """A single-turn invocation: the runner does not demand a stream."""
    return HarnessInvocation(
        argv=("fake-harness", "--print"),
        env_delta={},
        cwd=str(_REPO_ROOT),
        multi_turn=False,
    )


def _runner(tool, harness) -> ct._DispatchRunner:
    """A dispatch runner whose watchers are off (no thread, no waiting)."""
    return ct._DispatchRunner(
        tool=tool,
        harness=harness,
        idle_sec=0,
        total_sec=0,
        plan_id=None,
    )


def test_a_refused_dispatch_leaves_the_reaped_child_visible(monkeypatch):
    """The refusal travels out untouched, and the child is reaped.

    The registration of a plan dispatch is fail-closed: it raises
    instead of swallowing the failure.  The read step re-raises that
    exception as it is -- a swapped exception reads as a provider
    failure and gets retried -- and its ``finally`` reaps the child
    the dispatch had already spawned.

    The child stays in ``_current_process`` after the reap: that is
    the evidence that the dispatch really did stop, and the runner
    clears the reference on the clean path only.
    """
    tool = _tool()
    harness = _RawHarness()
    child = _FakeChild(stdout=_ClosedPipe())
    _install_fake_spawn(monkeypatch, child)

    def _refuse(scoped_tool, plan_id):
        raise VerificationCancelled(
            f"verification cancelled for plan {plan_id!r} while registering "
            "the dispatch"
        )

    monkeypatch.setattr(ct, "_register_dispatch", _refuse)

    runner = _runner(tool, harness)

    with pytest.raises(VerificationCancelled) as excinfo:
        runner.run(harness, _invocation(), stdin_text="hello")

    assert "while registering the dispatch" in str(excinfo.value), (
        "the refusal has to arrive as itself -- a replaced exception "
        "reads as a provider failure and gets retried; it arrived as "
        f"{excinfo.value!r}"
    )
    assert tool._current_process is child, (
        "the reaped child has to stay visible: the runner clears the "
        "reference on the clean path only, and the reaped child is "
        "the evidence that nothing ran unwatched"
    )
    assert child.waits and child.waits[0] is not None, (
        "the child was not reaped: the dispatch's own shutdown waits "
        f"for the child with a timeout; the waits were {child.waits!r}"
    )
    assert id(tool) not in ct._LIVE_DISPATCH_TOOLS, (
        "the live-dispatch entry outlived the dispatch -- a later stop "
        "would count a child that is already dead"
    )


def test_a_closed_pipe_is_a_read_error_and_not_a_crash(monkeypatch):
    """A closed pipe is recorded, not raised.

    The child's stdout closed under the dispatch's feet, and the read
    answers with ``ValueError``.  The read step used to turn that into
    a timeout rather than letting it escape as a crash, and that is
    what it still does: the error is recorded in ``read_error`` and the
    read answers with no events.

    The dispatch reads ``read_error`` further up and turns a read
    error that carries no answer into an idle timeout, so the run ends
    as "the CLI went quiet", not as "the dispatch crashed".
    """
    tool = _tool()
    harness = _RawHarness()
    child = _FakeChild(stdout=_ClosedPipe())
    _install_fake_spawn(monkeypatch, child)

    runner = _runner(tool, harness)

    result = runner.run(harness, _invocation(), stdin_text="hello")

    assert isinstance(runner.read_error, ValueError), (
        "the closed pipe was not recorded as a read error; the dispatch "
        f"reads that field to tell a quiet CLI from a crash; it is "
        f"{runner.read_error!r}"
    )
    assert str(runner.read_error) == "I/O operation on closed file"
    assert result.events == (), (
        "a read that failed carries no events; the dispatch has to be "
        f"able to say so; it carried {result.events!r}"
    )
    assert tool._current_process is None, (
        "the clean path clears the child's reference -- the child was "
        "reaped and nothing is left running"
    )
    assert child.waits and child.waits[0] is not None, (
        "the child was not reaped: the dispatch's own shutdown waits "
        f"for the child with a timeout; the waits were {child.waits!r}"
    )
    assert id(tool) not in ct._LIVE_DISPATCH_TOOLS, (
        "the live-dispatch entry outlived the dispatch -- a later stop "
        "would count a child that is already dead"
    )
