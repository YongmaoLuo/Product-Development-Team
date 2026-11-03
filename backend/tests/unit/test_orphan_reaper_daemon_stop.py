"""Giving a reaper daemon back: stopping the daemons of one plan root.

The census (:func:`list_reaper_daemons`) says which daemons the machine
is running.  This file pins the other half: how a caller that started a
daemon — a test fixture, a verification round — gives it back without
taking the rest of the machine with it.

The rule is narrow on purpose.  A daemon serves exactly one plan root,
and the caller knows which root it started, so the helper stops the
daemons of *that* root and leaves every other daemon alone.  Two
daemons on two roots are two different services as far as the machine is
concerned, and a fixture that stopped one of them would be stopping
something it did not start.

Everything that touches the machine is injected — the census, the
sender, the liveness probe and the napper — so the escalation ladder
(SIGTERM, the grace period, then SIGKILL) can be pinned without a
single real signal leaving the test process.
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


def _daemon(pid: int, plans_root: str = None) -> reaper.ReaperDaemon:
    """Return a daemon as the census would report it."""
    return reaper.ReaperDaemon(
        pid=pid,
        ppid=1,
        plans_root=plans_root,
        started_at="990.0",
        age_seconds=10.0,
        plans_root_exists=plans_root is not None,
    )


def _wait_for_zombie(pid: int, timeout: float = 10.0) -> bool:
    """Wait until *pid* has exited without being waited for.

    A zombie is a process that has exited but whose parent has not
    collected it yet.  The process table still lists it, and
    ``kill(pid, 0)`` still succeeds — which is the whole point of
    asking ``ps`` for the state instead.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if state.startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def _stop(
    plans_root,
    daemons,
    *,
    signals,
    alive=None,
    timeout=5.0,
    signal_fn=None,
):
    """Call the helper with everything that touches the machine faked.

    *alive* is the liveness of each pid: a mapping of pid to a boolean,
    or a callable.  A daemon that is signalled with ``TERM`` stops being
    alive, so the escalation only happens for a daemon that ignores the
    first signal.
    """
    state = alive if alive is not None else {}

    def _alive(pid):
        if callable(state):
            return state(pid)
        return state.get(pid, True)

    def _signal(pid, sig_name):
        signals.append((pid, sig_name))
        if isinstance(state, dict):
            state[pid] = False

    return reaper.stop_daemons_for_plans_root(
        plans_root,
        timeout=timeout,
        lister=lambda: list(daemons),
        signal_fn=signal_fn if signal_fn is not None else _signal,
        alive_fn=_alive,
        sleep_fn=lambda seconds: None,
    )


# ---------------------------------------------------------------------------
# Only the named root
# ---------------------------------------------------------------------------


def test_stops_matching_root_only():
    """Only the daemon serving the named root is stopped.

    A machine can run a daemon per root, and a caller that started one
    of them knows which one.  Stopping the daemon of a *different* root
    would be stopping a service this caller never started — the whole
    reason the helper takes a root instead of a pid.
    """
    signals = []
    daemons = [
        _daemon(7, "/tmp/p/plans"),
        _daemon(8, "/tmp/q/plans"),
    ]

    stopped = _stop(Path("/tmp/p/plans"), daemons, signals=signals)

    assert stopped == [7]
    assert signals == [(7, "TERM")]


def test_ignores_unrelated_root():
    """A daemon on another root is not signalled at all.

    Not even a probe that looks like one: the helper must not touch a
    process it is not going to stop.  A daemon whose root the command
    line does not name is skipped for the same reason — its root is
    unknown, and an unknown root is never the root that was asked for.
    """
    signals = []
    daemons = [
        _daemon(7, "/tmp/q/plans"),
        _daemon(8, "/tmp/p/plans/other"),
        _daemon(9, None),
    ]

    stopped = _stop(Path("/tmp/p/plans"), daemons, signals=signals)

    assert stopped == []
    assert signals == []


def test_the_root_is_compared_after_resolution(tmp_path):
    """A root reached through a symlink is the root it points at.

    The command line says what the operator typed; the machine says
    where the bytes are.  Two spellings of one directory have to be one
    root, or the helper would miss the daemon it was sent to stop.
    """
    real = tmp_path / "plans"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    signals = []
    daemons = [_daemon(7, str(link))]

    stopped = _stop(real, daemons, signals=signals)

    assert stopped == [7]
    assert signals == [(7, "TERM")]


# ---------------------------------------------------------------------------
# The escalation ladder
# ---------------------------------------------------------------------------


def test_escalates_to_sigkill():
    """A daemon that ignores SIGTERM is sent SIGKILL.

    The daemon is a Python process that may be stuck in a C call it
    cannot be interrupted from, so a TERM alone is not a way to stop it.
    The ladder is TERM, the grace period, then KILL — and the grace
    period is honoured rather than skipped.
    """
    signals = []
    daemons = [_daemon(7, "/tmp/p/plans")]

    stopped = _stop(
        Path("/tmp/p/plans"),
        daemons,
        signals=signals,
        alive=lambda pid: True,  # never exits
        timeout=0.2,
    )

    assert stopped == [7]
    assert signals == [(7, "TERM"), (7, "KILL")]


def test_a_daemon_that_is_already_gone_is_skipped():
    """A dead daemon is not signalled, and is not reported as stopped.

    The census can be a moment out of date; a daemon that died on its
    own between the census and the signal is not something to signal,
    and it is not something to report.
    """
    signals = []
    daemons = [_daemon(7, "/tmp/p/plans")]

    stopped = _stop(
        Path("/tmp/p/plans"),
        daemons,
        signals=signals,
        alive=lambda pid: False,
    )

    assert stopped == []
    assert signals == []


def test_a_daemon_that_survives_sigkill_is_recorded_but_not_raised():
    """A daemon that will not die does not make the helper raise.

    The helper is called from a fixture's teardown: a raise there would
    take the test down over a process that is merely stubborn.  The
    pid is still reported, because the caller has to know that the
    daemon it started is still running.
    """
    signals = []
    daemons = [_daemon(7, "/tmp/p/plans")]

    stopped = _stop(
        Path("/tmp/p/plans"),
        daemons,
        signals=signals,
        alive=lambda pid: True,
        timeout=0.2,
    )

    assert stopped == [7]


def test_a_zombie_child_counts_as_gone(tmp_path):
    """A child that has exited but not been waited for is dead.

    The daemon a fixture starts is that fixture's own child, so after
    it exits it lingers as a zombie — and a zombie answers
    ``kill(pid, 0)``, so the plain liveness probe calls it alive.
    Treating it as alive would make the helper wait out the whole grace
    period and then signal a process that is already dead.

    The helper waits for the child on the way out, so the zombie does
    not stay behind either: a daemon nobody collects would sit in the
    process table for as long as the fixture lives.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert _wait_for_zombie(child.pid), (
        "the child should have exited without being waited for"
    )

    signals = []
    daemon = _daemon(child.pid, str(tmp_path))

    stopped = reaper.stop_daemons_for_plans_root(
        tmp_path,
        lister=lambda: [daemon],
        signal_fn=lambda pid, sig: signals.append((pid, sig)),
        sleep_fn=lambda seconds: None,
    )

    assert stopped == []
    assert signals == []

    # The helper collected the child: there is nothing left to wait for.
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)


def test_one_failing_signal_does_not_stop_the_others():
    """A signal that raises costs that daemon, not the rest.

    Each daemon is stopped on its own: one that refuses to be signalled
    must not leave the others running, because the point of the call is
    that none of them are left behind.
    """
    signals = []
    alive = {7: True, 8: True}
    daemons = [
        _daemon(7, "/tmp/p/plans"),
        _daemon(8, "/tmp/p/plans"),
    ]

    def _signal(pid, sig_name):
        if pid == 7:
            raise OSError("no such process")
        signals.append((pid, sig_name))
        alive[pid] = False

    stopped = _stop(
        Path("/tmp/p/plans"),
        daemons,
        signals=signals,
        alive=alive,
        signal_fn=_signal,
    )

    assert stopped == [8]
    assert signals == [(8, "TERM")]


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


def test_the_census_is_read_when_no_lister_is_given(monkeypatch):
    """Without a lister the helper reads the machine's own census.

    The census is the module's own answer to "which daemons are
    running"; a caller that has no better list has to use it, or the
    helper would be stopping daemons nobody asked it to stop.
    """
    signals = []
    alive = {7: True}
    monkeypatch.setattr(
        reaper,
        "list_reaper_daemons",
        lambda: [_daemon(7, "/tmp/p/plans")],
    )

    def _signal(pid, sig_name):
        signals.append((pid, sig_name))
        alive[pid] = False

    stopped = reaper.stop_daemons_for_plans_root(
        Path("/tmp/p/plans"),
        lister=None,
        signal_fn=_signal,
        alive_fn=lambda pid: alive.get(pid, True),
        sleep_fn=lambda seconds: None,
    )

    assert stopped == [7]
    assert signals == [(7, "TERM")]


def test_the_helper_is_exported():
    """The helper is part of the module's public surface.

    The fixtures that call it are outside this module, so the name has
    to be one the module promises.
    """
    assert "stop_daemons_for_plans_root" in reaper.__all__
