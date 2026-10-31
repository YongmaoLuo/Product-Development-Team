"""The reaper daemon has to be able to leave.

Why this file exists
--------------------
The reaper daemon is started once per plan-artifact root, and it is
started **detached** -- it has to outlive the backend that started it,
because "the backend died" is precisely the situation it exists for.
But a daemon that cannot leave is a daemon that accumulates: on
2026-10-31 twenty-six of them were running on one machine, the oldest
for close to three hours, each of them walking every process on the
box every thirty seconds and reading each one's environment.

When the machine may be left
----------------------------
A daemon may only leave a machine that has nothing left on it: no
candidate to reclaim **and** no owner alive.  An owner alive means
somebody is still using PDT on that machine, and such an owner can
spawn services -- the very things the daemon is here to reclaim.  So
the idle window is measured on the machine being *unused*, not merely
on there being nothing to reclaim this second.

The window itself
-----------------
The window starts at the first pass that finds the machine unused, and
anything that makes the machine busy again resets it: a candidate to
reclaim, or an owner that is still alive.  The window runs from the
moment the daemon *observed* the machine to be unused -- it does not
inherit the quiet time of the machine it was born into.

One machine is quieter than that
--------------------------------
A machine that has *never* had an owner alive cannot produce work at
all: nothing on it belongs to PDT, so nothing on it can become a
candidate.  Its quietness therefore goes back further -- to the last
time the daemon had something to reclaim -- and the daemon leaves
``idle_exit`` seconds after that.

A machine that *had* owners and lost them is a different machine.  The
children that just lost their owner are exactly the orphans this
daemon exists to reclaim, so its window starts over when the last
owner is gone: the daemon watches the machine for a full window before
it may call it quiet.

Two more ways out
-----------------
A daemon born in a directory that is later deleted (a test's temporary
directory, say) has nothing left to watch, and a daemon of any age has
to have an end.  Both are pinned here, because "it will exit eventually"
is only true if the two doors exist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    DECISION_KILL,
    REASON_ORPHAN,
    ProcInfo,
    ReaperConfig,
    ScanResult,
    Verdict,
)

#: A pid that is certainly not running -- the owner of our fixtures.
DEAD_OWNER_PID = 999_999


class _Loop:
    """A stop event that never stops and never blocks.

    The daemon has to be testable without waiting for a real interval
    and without a real signal, so the loop is driven by a stand-in
    that answers immediately.  ``stop_after_waits`` says how many
    passes the daemon is allowed to make before it is stopped -- the
    stop then arrives the way a real one does, between two passes.
    ``on_wait`` runs after each pass, which is where a test removes
    the thing the daemon is supposed to notice.
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


def _candidate(pid: int = 4321) -> Verdict:
    """A verdict the ladder produced for a process worth reclaiming."""
    proc = ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid,
        uid=os.getuid(),
        rss_kb=213000,
        argv="/usr/bin/python3 -m backend.server",
    )
    return Verdict(
        proc=proc,
        decision=DECISION_KILL,
        reason=REASON_ORPHAN,
        owner_pid=DEAD_OWNER_PID,
        owner_start="17",
        kind="service",
        signal="",
    )


def _no_reaping(verdicts, config):
    """A reaping stage that signals nothing.

    The daemon's own cleanup would otherwise act on the fixture
    verdicts, and a fixture pid is a real pid on the machine the test
    runs on.
    """
    return []


# --------------------------------------------------------------------------
# The idle streak
# --------------------------------------------------------------------------


def test_live_owners_keep_the_daemon_working(tmp_path):
    """A machine somebody owns is a machine the daemon has to keep.

    The machine had 56 owners alive and 0 candidates.  There was
    nothing to reclaim *that second*, but the owners can spawn services
    at any moment, and those services are exactly what the daemon
    exists to reclaim.  It stays until it is told to stop -- otherwise
    the orphans that appear after it leaves are never reclaimed.
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
        "the daemon left a machine with live owners on it; it was still "
        "running when it was stopped"
    )
    assert len(scans) == 5, (
        "the daemon walked away on its own; it has to keep working while "
        "somebody owns the machine"
    )


def test_the_idle_window_starts_only_after_the_last_owner_leaves(tmp_path):
    """The window does not run while somebody owns the machine.

    While an owner is alive, work can appear at any moment, so the idle
    window may not run at all: the daemon is born into a machine that
    may have been busy for hours and it does not know that.  The window
    starts at the first pass that finds no owner alive, and only from
    there does the daemon count the seconds.
    """
    scans = []
    loop = _Loop(stop_after_waits=10)

    def fake_scan(_config):
        scans.append(1)
        if len(scans) <= 2:
            return ScanResult(verdicts=[], owners_alive=1)
        return ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=loop,
        clock=_ticks([0.0, 1000.0, 5000.0, 9000.0, 11000.0, 13000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert loop.stopped is False
    assert len(scans) == 4, (
        "the daemon left before the last owner was gone, or it left before "
        "it had watched the machine for the whole idle window"
    )


def test_a_machine_without_owners_is_quiet_from_the_last_work(tmp_path):
    """With nobody owning the machine, the quietness goes back further.

    No owner means no work: nothing on this machine belongs to PDT, so
    nothing on it can become a candidate, and the machine has been
    quiet since the last time this daemon had something to reclaim.  The
    streak is dated back to that moment instead of to the observation.
    """
    scans = []

    def fake_scan(_config):
        scans.append(1)
        if len(scans) == 1:
            return ScanResult(verdicts=[_candidate()], owners_alive=0)
        return ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=_Loop(stop_after_waits=5),
        clock=_ticks([0.0, 1000.0, 2000.0, 3000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert len(scans) == 3, (
        "the daemon did not date the quietness back to the last work it "
        "had on a machine with no owner alive"
    )


def test_candidates_keep_the_daemon_working(tmp_path):
    """A daemon that has something to reclaim stays.

    The other half of the contract: the streak is reset by the work,
    so a daemon that keeps finding candidates keeps running.
    """
    scans = []
    loop = _Loop(stop_after_waits=3)

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[_candidate()], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=loop,
        clock=_ticks([0.0, 1000.0, 2000.0, 3000.0, 4000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert loop.stopped is True
    assert len(scans) >= 3


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
# The two other doors out
# --------------------------------------------------------------------------


def test_the_daemon_leaves_when_its_plans_root_is_gone(tmp_path):
    """A daemon whose world has been deleted has nothing to watch.

    The daemons that leaked were born inside a test's temporary
    directory.  When that directory goes, the daemon it left behind is
    watching a path that does not exist -- and it must not keep
    scanning the machine for it.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    scans = []
    loop = _Loop(stop_after_waits=5, on_wait=lambda n: _remove(plans_root))

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=plans_root, interval=1, idle_exit=1800),
        stop=loop,
        clock=lambda: 0.0,
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert loop.stopped is False
    assert len(scans) == 1


def _remove(path: Path) -> None:
    """Remove *path* and everything under it."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def test_the_daemon_leaves_when_it_reaches_its_maximum_lifetime(tmp_path):
    """A daemon of any age has to have an end.

    The idle streak is about idleness; this is about age.  A daemon
    that has been alive for its maximum lifetime leaves even when the
    machine keeps giving it something to do.
    """
    scans = []
    loop = _Loop(stop_after_waits=10)

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(
            plans_root=tmp_path,
            interval=1,
            idle_exit=100_000,
            max_lifetime=250,
        ),
        stop=loop,
        clock=_ticks([0.0, 100.0, 200.0, 300.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert loop.stopped is False
    assert len(scans) == 2


def test_the_maximum_lifetime_is_finite_by_default():
    """The ceiling exists whether or not anybody configured it."""
    assert reaper.DEFAULT_MAX_LIFETIME > 0
    assert ReaperConfig().max_lifetime == reaper.DEFAULT_MAX_LIFETIME


def test_the_maximum_lifetime_can_be_configured():
    """The ceiling is configurable, and an unusable value falls back."""
    assert reaper.config_from_env(
        {reaper.ENV_MAX_LIFETIME: "600"}
    ).max_lifetime == 600

    assert reaper.config_from_env(
        {reaper.ENV_MAX_LIFETIME: "not a number"}
    ).max_lifetime == reaper.DEFAULT_MAX_LIFETIME
