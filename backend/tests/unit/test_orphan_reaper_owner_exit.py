"""A reaper daemon has to leave when the process that started it is gone.

Why this file exists
--------------------
The reaper daemon had three doors out: the machine went quiet, the
daemon reached its maximum lifetime, and the plan-artifact root it
watches was deleted.  None of them is about the thing that actually
leaks daemons.  On 2026-11-02 this machine was running 145 reaper
daemons, most of them re-parented to ``pid 1`` -- their parent was
gone, and they went on walking the whole process table every thirty
seconds, for ever.

A daemon whose parent is gone has nothing left to do: the parent was
the reason it existed.  The parent was a test fixture, a verification
round, a shell -- something that is over.  The daemon outliving it is
not resilience, it is a leak.

The one exception
-----------------
The daemon the **server** starts has to outlive the server: "the
backend died" is precisely the situation it exists for, so it is
exempted explicitly (``--survive-owner``) rather than by accident.

The judgement is the same judgement the reaper makes everywhere else
------------------------------------------------------------------------
A pid on its own is not an identity -- the kernel recycles pids.  So
the owner is recorded as a pid **and** a start time, and a live pid
whose start time differs from the recorded one is somebody else's
process that happens to carry the same number: the owner is gone.

Uncertainty always resolves towards *staying*.  An owner that cannot
be checked, an owner whose start time could not be recorded, an owner
whose start time cannot be read now -- none of them is a dead owner,
and a daemon that leaves on a guess stops reclaiming memory on a
machine that needs it.

Everything that touches the machine is injected, so none of these
tests sends a signal or reads a real process table.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import ReaperConfig, ScanResult  # noqa: E402

#: A pid that is certainly not running.
DEAD_PID = 999_999


class _Loop:
    """A stop event that never stops and never blocks.

    The daemon has to be testable without waiting for a real interval
    and without a real signal, so the loop is driven by a stand-in
    that answers immediately.
    """

    def __init__(self, stop_after_waits: int = 0) -> None:
        self.stopped = False
        self.waits = 0
        self.stop_after_waits = stop_after_waits

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout=None) -> bool:
        self.waits += 1
        if self.stop_after_waits and self.waits >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


def _ticks(values):
    """A clock that walks *values* and then stands still.

    The daemon reads its clock once before the loop and once per pass;
    a clock that ran out would otherwise raise, and the daemon's reader
    turns a raising clock into the previous reading.
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


def _no_reaping(verdicts, config):
    """A reaping stage that signals nothing.

    The daemon's own cleanup would otherwise act on the fixture
    verdicts, and a fixture pid is a real pid on the machine the test
    runs on.
    """
    return []


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


def test_owner_gone_when_pid_dead(monkeypatch):
    """The owner is gone as soon as the kernel says so.

    The kernel answering "no such process" is conclusive: the process
    that started this daemon is not running any more, and the daemon
    has outlived the only reason it existed.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: False)

    config = ReaperConfig(owner_pid=DEAD_PID, owner_start="990.0", survive_owner=False)

    assert reaper._owner_gone(config) is True


def test_owner_reused_pid_is_gone(monkeypatch):
    """A live pid whose start time differs is somebody else's process.

    The kernel recycles pids, so a pid that is alive is not proof that
    the owner is alive.  The recorded start time is what tells the two
    apart -- and a mismatch means the owner is gone and the number
    belongs to somebody else now.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid=None: "123.0")

    config = ReaperConfig(owner_pid=4242, owner_start="990.0", survive_owner=False)

    assert reaper._owner_gone(config) is True


def test_survive_owner_never_exits(monkeypatch):
    """The exemption wins over everything else.

    The daemon the server starts has to outlive the server, so the
    owner is not consulted at all -- not even to confirm that it is
    gone.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: False)

    config = ReaperConfig(owner_pid=DEAD_PID, owner_start="990.0", survive_owner=True)

    assert reaper._owner_gone(config) is False


def test_a_live_owner_is_not_gone(monkeypatch):
    """The owner is still there, so the daemon stays.

    The same start time, the same pid, still running: this is the
    ordinary case, and the daemon has to keep working.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid=None: "990.0")

    config = ReaperConfig(owner_pid=4242, owner_start="990.0")

    assert reaper._owner_gone(config) is False


def test_an_owner_that_cannot_be_checked_is_not_gone(monkeypatch):
    """An owner that could not be checked is not a dead owner.

    The kernel answering with something other than "no such process"
    means *unknown*.  Leaving on that would stop the daemon on a
    machine that is still busy.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: None)

    config = ReaperConfig(owner_pid=4242, owner_start="990.0")

    assert reaper._owner_gone(config) is False


def test_an_owner_without_a_start_time_is_not_gone(monkeypatch):
    """A live owner whose start time was never recorded is not gone.

    Without the recorded start time there is nothing to compare the
    owner against, so a live pid cannot be told apart from a recycled
    one -- and the uncertainty resolves towards staying.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: True)

    config = ReaperConfig(owner_pid=4242, owner_start="")

    assert reaper._owner_gone(config) is False


def test_an_owner_whose_start_time_cannot_be_read_is_not_gone(monkeypatch):
    """A start time that cannot be read now is not a mismatch.

    The platform declined to say when the process started.  That is not
    the same as saying it started at a different moment.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid=None: "")

    config = ReaperConfig(owner_pid=4242, owner_start="990.0")

    assert reaper._owner_gone(config) is False


def test_a_daemon_without_an_owner_is_never_gone():
    """A daemon that was not told who its owner is keeps running.

    Nothing is known about the owner, so nothing can be concluded
    about it.
    """
    assert reaper._owner_gone(ReaperConfig()) is False


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_loop_leaves_after_owner_death(tmp_path, monkeypatch):
    """The daemon leaves on the first pass once its owner is gone.

    The owner check is at the top of the loop, so the daemon does not
    even scan: the machine it was watching is no longer anybody's.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: False)
    scans = []

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=1)

    code = reaper.run_daemon(
        ReaperConfig(
            plans_root=tmp_path,
            interval=1,
            idle_exit=1800,
            owner_pid=DEAD_PID,
            owner_start="990.0",
        ),
        stop=_Loop(stop_after_waits=5),
        clock=_ticks([0.0, 1000.0, 2000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert scans == [], (
        "the daemon went on scanning a machine whose owner was gone"
    )


def test_the_loop_keeps_going_while_the_owner_lives(tmp_path, monkeypatch):
    """A live owner keeps the daemon at work.

    The other half of the contract: the door out is the owner's death,
    not the daemon's boredom.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid=None: "990.0")
    scans = []
    loop = _Loop(stop_after_waits=3)

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=1)

    code = reaper.run_daemon(
        ReaperConfig(
            plans_root=tmp_path,
            interval=1,
            idle_exit=1800,
            owner_pid=4242,
            owner_start="990.0",
        ),
        stop=loop,
        clock=_ticks([0.0, 1000.0, 2000.0, 3000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert len(scans) == 3


def test_an_exempted_daemon_keeps_going(tmp_path, monkeypatch):
    """The daemon the server starts outlives its owner.

    ``--survive-owner`` is what the server passes, and it is the only
    thing that keeps the daemon alive once the server is gone.
    """
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: False)
    scans = []
    loop = _Loop(stop_after_waits=3)

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=1)

    code = reaper.run_daemon(
        ReaperConfig(
            plans_root=tmp_path,
            interval=1,
            idle_exit=1800,
            owner_pid=DEAD_PID,
            owner_start="990.0",
            survive_owner=True,
        ),
        stop=loop,
        clock=_ticks([0.0, 1000.0, 2000.0, 3000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert len(scans) == 3


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _daemon_config(monkeypatch, argv):
    """Run ``main`` in daemon mode and return the config it built."""
    seen = {}

    def fake_run_daemon(config, **kwargs):
        seen["config"] = config
        return 0

    monkeypatch.setattr(reaper, "run_daemon", fake_run_daemon)
    code = reaper.main(argv)
    assert code == 0
    return seen["config"]


def test_the_daemon_records_the_process_that_started_it(tmp_path, monkeypatch):
    """A daemon that was not told its owner uses its own parent.

    The daemon is started by something -- a test fixture, a
    verification round, the server -- and that something is the reason
    the daemon exists.  The parent is the default owner, so the daemon
    does not outlive it by accident.
    """
    monkeypatch.setattr(os, "getppid", lambda: 4242)
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid=None: "990.0")

    config = _daemon_config(monkeypatch, ["--daemon", "--plans-root", str(tmp_path)])

    assert config.owner_pid == 4242
    assert config.owner_start == "990.0"
    assert config.survive_owner is False


def test_pid_one_is_not_an_owner(tmp_path, monkeypatch):
    """A daemon that has already been orphaned has no owner to record.

    ``pid 1`` is the kernel's own reaper, not the process that started
    anything; recording it would make every orphaned daemon wait for
    an owner that can never die.
    """
    monkeypatch.setattr(os, "getppid", lambda: 1)

    config = _daemon_config(monkeypatch, ["--daemon", "--plans-root", str(tmp_path)])

    assert config.owner_pid is None


def test_the_exemption_leaves_the_owner_unset(tmp_path, monkeypatch):
    """``--survive-owner`` does not record an owner at all.

    The daemon the server starts is the one that has to outlive its
    owner, so it neither records one nor watches one.
    """
    monkeypatch.setattr(os, "getppid", lambda: 4242)

    config = _daemon_config(
        monkeypatch, ["--daemon", "--survive-owner", "--plans-root", str(tmp_path)]
    )

    assert config.survive_owner is True
    assert config.owner_pid is None


def test_an_explicit_owner_is_honoured(tmp_path, monkeypatch):
    """``--owner-pid`` and ``--owner-start`` say who the owner is.

    A daemon started by something that cannot be its parent -- a
    supervisor, a wrapper -- is told who its owner is instead.
    """
    monkeypatch.setattr(os, "getppid", lambda: 4242)

    config = _daemon_config(
        monkeypatch,
        [
            "--daemon",
            "--owner-pid",
            "77",
            "--owner-start",
            "123.0",
            "--plans-root",
            str(tmp_path),
        ],
    )

    assert config.owner_pid == 77
    assert config.owner_start == "123.0"


def test_the_parser_takes_the_three_flags():
    """The command line carries the owner's identity and the exemption."""
    parser = reaper.build_arg_parser()

    args = parser.parse_args(
        ["--daemon", "--owner-pid", "77", "--owner-start", "123.0", "--survive-owner"]
    )

    assert args.owner_pid == 77
    assert args.owner_start == "123.0"
    assert args.survive_owner is True


def test_the_owner_is_not_set_outside_daemon_mode(tmp_path, monkeypatch):
    """Only a daemon has an owner.

    A single pass has no owner to outlive -- it is over when it is
    done -- so the parent is not recorded as one.
    """
    seen = {}

    def fake_run_once(config):
        seen["config"] = config
        return 0

    monkeypatch.setattr(reaper, "run_once", fake_run_once)
    monkeypatch.setattr(os, "getppid", lambda: 4242)

    code = reaper.main(["--once", "--plans-root", str(tmp_path)])

    assert code == 0
    assert seen["config"].owner_pid is None
