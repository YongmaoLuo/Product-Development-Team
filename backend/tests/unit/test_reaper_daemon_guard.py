"""Giving back the daemons a test run started.

The suite drives real servers, and a real server starts a reaper daemon.
The daemon is written to outlive the process that started it — that is
the whole point of a daemon — so a test that terminates its server leaves
the daemon behind on the machine.  One daemon per test run is litter; a
hundred test runs is the pile of processes this suite exists to prevent.

So a test run has to be able to take its own daemons back.  That takes
three things, and this file pins all three:

* a **snapshot** of the daemons that were already running, so the run
  knows which ones it did not start;
* a **rule** for telling a daemon of this run from a daemon of somebody
  else, which is the pytest temporary directory the run is working in;
* a **reclaim** that stops the new ones and leaves everything else alone.

The rule is deliberately narrow.  A daemon that serves a root pytest made
is a daemon this run started; a daemon on any other root belongs to
something else — a developer's own server, another plan's reaper — and
stopping it would be stopping a service this run never asked for.  The
narrowness is the point: the alternative is a cleanup that reclaims the
machine, which is a much worse bug than the one it fixes.

Everything that touches the machine is injected — the census and the
stopper — so the whole contract can be pinned without a single signal
leaving the test process.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from tests.reaper_daemon_guard import (  # noqa: E402
    is_pytest_plans_root,
    reclaim_new_daemons,
    snapshot_daemons,
)

#: A plan root inside a pytest temporary tree.  The marker is the
#: ``pytest-of-<user>`` component the tmpdir factory creates.
PYTEST_ROOT = (
    "/private/var/folders/xx/yyyy/T/pytest-of-user/pytest-9/test_case0/plans"
)

#: A second pytest tree — a second test, a second worker.
OTHER_PYTEST_ROOT = (
    "/private/var/folders/xx/yyyy/T/pytest-of-user/pytest-9/test_case1/plans"
)

#: A root that belongs to somebody else entirely.
FOREIGN_ROOT = "/tmp/p/plans"


def _daemon(pid: int, plans_root=None, started_at: str = "990.0"):
    """Return a daemon as the census would report it."""
    return reaper.ReaperDaemon(
        pid=pid,
        ppid=1,
        plans_root=plans_root,
        started_at=started_at,
        age_seconds=10.0,
        plans_root_exists=plans_root is not None,
    )


class _Stopper:
    """A stopper that records the roots it was asked to stop.

    It answers with the pids it "stopped", which is what the real helper
    answers with: the pids that were actually signalled.
    """

    def __init__(self, stopped=None):
        self.roots = []
        self._stopped = stopped or {}

    def __call__(self, plans_root):
        self.roots.append(Path(plans_root))
        return list(self._stopped.get(str(plans_root), []))


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def test_snapshot_maps_pid_to_start():
    """Every daemon is mapped to its own start token.

    A pid alone is not an identity — the kernel recycles them — so the
    snapshot records when each daemon started next to which pid it is.
    The map is what tells a later census which of its entries are the
    ones that were already running.
    """
    daemons = [
        _daemon(11, FOREIGN_ROOT, started_at="990.0"),
        _daemon(12, PYTEST_ROOT, started_at="991.5"),
    ]

    snapshot = snapshot_daemons(lister=lambda: daemons)

    assert snapshot == {11: "990.0", 12: "991.5"}


def test_snapshot_of_a_machine_with_no_daemons_is_empty():
    """A machine with no daemons snapshots to nothing."""
    assert snapshot_daemons(lister=lambda: []) == {}


def test_a_failing_census_snapshots_to_nothing():
    """A census that raises costs the snapshot, not the test run.

    The snapshot is taken by a fixture; a raise there would take the
    whole run down over a question the machine declined to answer.
    """
    def _boom():
        raise OSError("the process table is unreadable")

    assert snapshot_daemons(lister=_boom) == {}


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_a_pytest_root_is_recognised():
    """A root inside a pytest temporary tree is ours to reclaim."""
    assert is_pytest_plans_root(PYTEST_ROOT) is True


def test_a_foreign_root_is_not_pytest():
    """A root pytest did not make is somebody else's."""
    assert is_pytest_plans_root(FOREIGN_ROOT) is False


def test_a_root_without_one_is_not_pytest():
    """No root at all is not a pytest root either.

    A daemon whose command line names no root is running on the default
    one, which this suite does not know about — so it is not ours.
    """
    assert is_pytest_plans_root(None) is False
    assert is_pytest_plans_root("") is False


# ---------------------------------------------------------------------------
# The reclaim
# ---------------------------------------------------------------------------


def test_reclaim_only_new_pytest_daemons():
    """Only the daemons this run started, on a pytest root, are stopped.

    The daemon that was already running belongs to somebody else and
    stays.  The daemon on a root pytest did not make belongs to somebody
    else and stays.  The daemon this run started, under a pytest
    temporary directory, goes.
    """
    before = {41: "990.0"}
    daemons = [
        _daemon(41, PYTEST_ROOT),
        _daemon(2, OTHER_PYTEST_ROOT),
        _daemon(3, FOREIGN_ROOT),
    ]
    stopper = _Stopper({str(Path(OTHER_PYTEST_ROOT).resolve()): [2]})

    reclaimed = reclaim_new_daemons(
        before,
        lister=lambda: daemons,
        stopper=stopper,
    )

    assert reclaimed == [2]
    assert stopper.roots == [Path(OTHER_PYTEST_ROOT).resolve()]


def test_non_pytest_root_is_left_alone():
    """A daemon on a root pytest did not make is not stopped.

    The developer's own server, another plan's reaper, a daemon left by
    an earlier run — none of them was started by this run, and a
    cleanup that stopped them would be stopping services it never
    asked for.
    """
    before = {}
    daemons = [_daemon(7, FOREIGN_ROOT)]
    stopper = _Stopper()

    reclaimed = reclaim_new_daemons(
        before,
        lister=lambda: daemons,
        stopper=stopper,
    )

    assert reclaimed == []
    assert stopper.roots == []


def test_a_daemon_without_a_root_is_left_alone():
    """A daemon whose root is unknown is not stopped.

    An unknown root is never the root the run is working in, and the
    whole rule is about roots.
    """
    before = {}
    daemons = [_daemon(7, None)]
    stopper = _Stopper()

    reclaimed = reclaim_new_daemons(
        before,
        lister=lambda: daemons,
        stopper=stopper,
    )

    assert reclaimed == []
    assert stopper.roots == []


def test_a_root_that_already_had_a_daemon_is_left_alone():
    """A root that already served a daemon is not reclaimed.

    The stopper stops every daemon of a root, so a root that already had
    one before the snapshot would take that older daemon with it.  The
    older daemon is somebody else's — the snapshot says so — and it
    survives, which means the root is left entirely alone.
    """
    before = {41: "990.0"}
    daemons = [
        _daemon(41, PYTEST_ROOT),
        _daemon(2, PYTEST_ROOT),
    ]
    stopper = _Stopper()

    reclaimed = reclaim_new_daemons(
        before,
        lister=lambda: daemons,
        stopper=stopper,
    )

    assert reclaimed == []
    assert stopper.roots == []


def test_a_failing_census_reclaims_nothing():
    """A census that raises costs the reclaim, not the test run."""
    def _boom():
        raise OSError("the process table is unreadable")

    stopper = _Stopper()

    reclaimed = reclaim_new_daemons(
        {},
        lister=_boom,
        stopper=stopper,
    )

    assert reclaimed == []
    assert stopper.roots == []


def test_a_failing_stopper_does_not_stop_the_others():
    """A root that refuses to be stopped does not stop the rest.

    Each root is reclaimed on its own.  A stopper that raises on one
    root — a daemon that will not take a signal, a machine that will not
    answer — must not leave the other roots' daemons running, because
    the point of the call is that none of them are left behind.
    """
    before = {}
    daemons = [
        _daemon(2, PYTEST_ROOT),
        _daemon(3, OTHER_PYTEST_ROOT),
    ]

    def _stop(plans_root):
        if Path(plans_root) == Path(PYTEST_ROOT).resolve():
            raise OSError("no such process")
        return [3]

    reclaimed = reclaim_new_daemons(
        before,
        lister=lambda: daemons,
        stopper=_stop,
    )

    assert reclaimed == [3]


def test_the_reclaim_reads_the_machine_when_no_lister_is_given(monkeypatch):
    """Without a lister the reclaim reads the machine's own census.

    The census is the module's own answer to "which daemons are
    running"; a caller that has no better list has to use it.
    """
    monkeypatch.setattr(
        reaper,
        "list_reaper_daemons",
        lambda: [_daemon(2, PYTEST_ROOT)],
    )
    stopper = _Stopper({str(Path(PYTEST_ROOT).resolve()): [2]})

    reclaimed = reclaim_new_daemons({}, stopper=stopper)

    assert reclaimed == [2]
