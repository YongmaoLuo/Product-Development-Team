"""A machine that is still in use is not an idle machine.

Why this file exists
--------------------
The reaper daemon leaves a machine it considers idle: nothing to
reclaim for ``idle_exit`` seconds, so it stands down.  That rule has
one end missing.  The daemon used to ignore ``owners_alive`` -- the
number of marked processes whose owner is still running -- and a
machine with 56 live owners on it was still called idle after half an
hour.  The daemon left, and the orphans that appeared afterwards had
nobody to reclaim them: ``ensure_daemon`` is called once, when the
server starts, so nothing brings the reaper back.

The other end of the rule is just as important.  The idle streak used
to be reset by the owners as well, which made it unreachable on any
machine with a PDT instance on it -- twenty-six daemons were running
on one machine for exactly that reason (2026-10-31).  A daemon that
never leaves is a daemon that accumulates.

So both ends are needed, and this file pins them:

* a machine with an owner alive is **in use** -- the idle window does
  not run and the daemon keeps working;
* a machine with no owner alive and nothing to reclaim is **idle** --
  the daemon leaves after its window;
* owners that die make the machine idle again, and the window starts
  over: the children that just lost their owner are exactly the
  orphans this daemon exists to reclaim.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import ReaperConfig, ScanResult  # noqa: E402


class _Loop:
    """A stop event that never stops and never blocks.

    The daemon has to be testable without waiting for a real interval
    and without a real signal, so the loop is driven by a stand-in
    that answers immediately.  ``stop_after_waits`` says how many
    passes the daemon is allowed to make before it is stopped -- the
    stop then arrives the way a real one does, between two passes.
    """

    def __init__(self, stop_after_waits: int = 0, on_wait=None) -> None:
        self.stopped = False
        self.waits = 0
        self.stop_after_waits = stop_after_waits
        self.on_wait = on_wait

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout=None) -> bool:
        self.waits += 1
        if self.on_wait is not None:
            self.on_wait(self.waits)
        if self.stop_after_waits and self.waits >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


def _no_reaping(verdicts, config):
    """A reaping stage that signals nothing.

    The daemon's own cleanup would otherwise act on the fixture
    verdicts, and a fixture pid is a real pid on the machine the test
    runs on.
    """
    return []


def _ticks(values):
    """A clock that walks *values* and then stands still.

    The daemon reads its clock once before the loop and once per pass;
    a clock that ran out would otherwise raise, and the daemon's
    reader turns a raising clock into the previous reading -- which
    would make a test that expects the daemon to leave hang instead.
    """
    remaining = list(values)
    state = {"index": 0}

    def clock():
        index = state["index"]
        state["index"] += 1
        if index < len(remaining):
            return remaining[index]
        return remaining[-1] if remaining else 0.0

    return clock


# --------------------------------------------------------------------------
# A machine that is in use keeps the daemon
# --------------------------------------------------------------------------


def test_live_owners_keep_the_daemon_running(tmp_path):
    """Fifty-six owners and no candidates: the daemon stays.

    This is the machine from the 2026-10-31 incident.  Nothing has
    been reclaimed for half an hour, but 56 owners are alive: PDT is
    in use on this machine, the machine is not idle, and the daemon
    has to keep watching it.  It leaves only when somebody stops it.
    """
    scans = []
    loop = _Loop(stop_after_waits=5)

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=56)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=loop,
        clock=_ticks([0.0, 1000.0, 3000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert loop.stopped is True, (
        "the daemon was still running when it was stopped; it had to "
        "stay on a machine with 56 live owners"
    )
    assert len(scans) == 5, (
        "the daemon left a machine that is still in use; the idle "
        "window must not run while an owner is alive"
    )


# --------------------------------------------------------------------------
# A machine with nothing left on it lets the daemon go
# --------------------------------------------------------------------------


def test_no_work_and_no_owners_lets_the_daemon_leave(tmp_path):
    """Nobody owns the machine and nothing is to be reclaimed: it leaves.

    The other half of the contract.  A machine with no owner alive
    cannot produce work -- nothing on it belongs to PDT, so nothing on
    it can become a candidate -- and after its idle window the daemon
    stands down on its own.
    """
    scans = []
    loop = _Loop(stop_after_waits=5)

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=loop,
        clock=_ticks([0.0, 1000.0, 3000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert loop.stopped is False, (
        "the daemon has to leave on its own; it was still running when "
        "it was stopped"
    )
    assert len(scans) == 2, (
        "the daemon did not leave a machine that has nothing left on it"
    )


# --------------------------------------------------------------------------
# Owners that die
# --------------------------------------------------------------------------


def test_owners_that_die_restart_the_window(tmp_path):
    """The window starts again when the last owner is gone.

    While the owners live the machine is in use and the idle window
    does not run at all.  When they are gone the window starts over:
    the children that just lost their owner are exactly the orphans
    this daemon exists to reclaim, so it has to keep watching for a
    full window before it may call the machine quiet.
    """
    scans = []

    def fake_scan(_config):
        scans.append(1)
        if len(scans) <= 3:
            return ScanResult(verdicts=[], owners_alive=3)
        return ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=_Loop(stop_after_waits=10),
        clock=_ticks([0.0, 1000.0, 2000.0, 3000.0, 4000.0, 7000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert len(scans) == 5, (
        "the daemon did not watch a full idle window after the last "
        "owner was gone -- or it left while an owner was still alive"
    )
