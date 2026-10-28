"""Two run modes and the command line that starts them.

The module is runnable on its own — no sibling backend module is
imported — so it has to answer two questions for whoever starts it:
*what do you decide* and *what do you do about it*.

The single pass (``--once``) answers the first question on the
terminal: one line per verdict, ``pid decision reason`` and the marker
kind when the verdict carries one.  The resident mode (``--daemon``)
answers the second by doing it, over and over, until it is told to
stop.

The daemon's life is bounded, and that is the property this file
pins.  It stands down when another reaper already holds the lock (two
reapers would race over the same candidates and apply the same grace
period twice); it stands down when it is asked to stop; and it stands
down on its own when the machine has been quiet for long enough that
there is nothing left for it to reclaim.  A daemon that never exits is
a daemon that outlives the thing it was started for.

What the daemon must *not* do is die with the first thing that goes
wrong: a scan that fails is logged and the loop goes on, because a
reaper that gives up exactly when the machine needs it is worse than
no reaper at all.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    DECISION_KILL,
    DECISION_SKIP,
    REASON_ORPHAN,
    REASON_PROTECTED_PORT,
    ProcInfo,
    ReaperConfig,
    ScanResult,
    Verdict,
    acquire_lock,
    events_path,
)

#: A pid that is certainly not running — the owner of our fixtures.
DEAD_OWNER_PID = 999_999


def make_verdict(
    pid=1234,
    decision=DECISION_KILL,
    reason=REASON_ORPHAN,
    kind="service",
):
    """Build the verdict the ladder produces for one candidate."""
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
        decision=decision,
        reason=reason,
        owner_pid=DEAD_OWNER_PID,
        owner_start="17",
        kind=kind,
        signal="",
    )


class _Loop:
    """A stop event that never stops and never blocks.

    The daemon has to be testable without waiting for a real interval
    and without a real signal, so the loop is driven by a stand-in
    that answers immediately.  ``stop_after_waits`` says how many
    passes the daemon is allowed to make before it is stopped — the
    stop then arrives the way a real one does, between two passes.
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


# --------------------------------------------------------------------------
# The single pass
# --------------------------------------------------------------------------


def test_once_prints_verdicts(tmp_path, capsys, monkeypatch):
    """Two verdicts come out as two lines, and the exit code is zero.

    The line is what an operator reads when they ask "what did it
    decide, and why" — the pid, the decision, the reason, and the
    marker kind when the verdict carries one.
    """
    verdicts = [
        make_verdict(pid=1234, decision=DECISION_KILL, reason=REASON_ORPHAN, kind="service"),
        make_verdict(
            pid=5678,
            decision=DECISION_SKIP,
            reason=REASON_PROTECTED_PORT,
            kind="",
        ),
    ]
    monkeypatch.setenv("PDT_ORPHAN_REAPER", "1")
    monkeypatch.setattr(
        reaper,
        "scan",
        lambda config: ScanResult(verdicts=list(verdicts), owners_alive=0),
    )

    code = reaper.main(["--once", "--dry-run", "--plans-root", str(tmp_path)])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "1234 kill orphan service" in out
    assert "5678 skip protected_port" in out


def test_once_writes_the_event_log(tmp_path):
    """What the pass decided is written down, so it can be explained later."""
    verdicts = [
        make_verdict(pid=1234),
        make_verdict(pid=5678, decision=DECISION_SKIP, reason=REASON_PROTECTED_PORT),
    ]

    reaper.run_once(
        ReaperConfig(plans_root=tmp_path, dry_run=True),
        scan_fn=lambda config: ScanResult(verdicts=verdicts, owners_alive=0),
    )

    lines = events_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["pid"] == 1234


def test_once_says_disabled_and_does_nothing(tmp_path, capsys):
    """A reaper that is switched off says so and touches nothing."""
    config = ReaperConfig(plans_root=tmp_path, enabled=False)

    def boom(_config):
        raise AssertionError("a disabled reaper must not scan")

    result = reaper.run_once(config, scan_fn=boom)

    assert result.verdicts == []
    assert "disabled" in capsys.readouterr().out
    assert not events_path(tmp_path).exists()


def test_once_disabled_through_the_command_line(tmp_path, capsys, monkeypatch):
    """``PDT_ORPHAN_REAPER=0`` is the same thing from the other side."""
    monkeypatch.setenv("PDT_ORPHAN_REAPER", "0")

    code = reaper.main(["--once", "--plans-root", str(tmp_path)])

    assert code == 0
    assert "disabled" in capsys.readouterr().out


def test_dry_run_decides_without_signalling(tmp_path):
    """A dry run is the whole point of the mode: nothing is signalled."""
    sent = []

    reaper.run_once(
        ReaperConfig(plans_root=tmp_path, dry_run=True),
        scan_fn=lambda config: ScanResult(verdicts=[make_verdict()], owners_alive=0),
        send=lambda pid, sig, pgid: sent.append((pid, sig)),
    )

    assert sent == []


def test_dry_run_only_flips_the_dry_run_flag(tmp_path, monkeypatch):
    """``--dry-run`` changes the mode and nothing else."""
    captured = {}

    def fake_run_once(config, **kwargs):
        captured["config"] = config
        return ScanResult()

    monkeypatch.setenv("PDT_ORPHAN_REAPER", "1")
    monkeypatch.delenv("PDT_ORPHAN_REAPER_DRY_RUN", raising=False)
    monkeypatch.setattr(reaper, "run_once", fake_run_once)

    code = reaper.main(["--once", "--dry-run", "--plans-root", str(tmp_path)])

    assert code == 0
    assert captured["config"].dry_run is True
    assert captured["config"].plans_root == tmp_path
    assert captured["config"].enabled is True


# --------------------------------------------------------------------------
# The resident mode
# --------------------------------------------------------------------------


def test_daemon_exits_when_lock_held(tmp_path):
    """One reaper at a time: a second one stands down immediately.

    Two reapers would race over the same candidates, and the grace
    period between ``SIGTERM`` and ``SIGKILL`` would be applied twice
    by two independent actors.
    """
    handle = acquire_lock(tmp_path)
    assert handle is not None

    def boom(_config):
        raise AssertionError("the daemon must not scan when it is not the singleton")

    try:
        assert reaper.run_daemon(ReaperConfig(plans_root=tmp_path), scan_fn=boom) == 0
    finally:
        handle.close()


def test_daemon_idle_exit(tmp_path):
    """A quiet machine for long enough and the daemon leaves on its own.

    The clock is injected: the daemon must be able to prove that the
    machine has been idle for ``idle_exit`` seconds without waiting for
    half an hour to find out.
    """
    ticks = iter([0.0, 3600.0])
    config = ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800)

    code = reaper.run_daemon(
        config,
        stop=_Loop(),
        clock=lambda: next(ticks),
        scan_fn=lambda config: ScanResult(verdicts=[], owners_alive=0),
    )

    assert code == 0


def test_daemon_does_not_leave_while_an_owner_is_alive(tmp_path):
    """A machine PDT is still using is not an idle machine.

    The daemon keeps running while an owner is alive, however long the
    clock says it has been quiet.
    """
    loop = _Loop(stop_after_waits=2)
    ticks = iter([0.0, 3600.0, 7200.0, 10800.0])
    scans = []

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=1)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, idle_exit=1800),
        stop=loop,
        clock=lambda: next(ticks),
        scan_fn=fake_scan,
    )

    assert code == 0
    assert len(scans) == 2


def test_daemon_sigterm_stops(tmp_path):
    """Asked to stop, the daemon stops — and starts nothing new.

    The stop event is set while the scan is running.  The daemon must
    leave without starting the cleanup it was about to start: a daemon
    that was told to stop must not go on killing things.
    """
    stop = threading.Event()
    sent = []

    def fake_scan(_config):
        stop.set()
        return ScanResult(verdicts=[make_verdict()], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1),
        stop=stop,
        clock=lambda: 0.0,
        scan_fn=fake_scan,
        send=lambda pid, sig, pgid: sent.append((pid, sig)),
    )

    assert code == 0
    assert sent == []


def test_daemon_releases_the_lock_when_it_leaves(tmp_path):
    """The lock belongs to the daemon only while it is running."""
    loop = _Loop(stop_after_waits=1)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1),
        stop=loop,
        clock=lambda: 0.0,
        scan_fn=lambda config: ScanResult(verdicts=[], owners_alive=0),
    )

    assert code == 0
    handle = acquire_lock(tmp_path)
    assert handle is not None
    handle.close()


def test_daemon_continues_after_a_scan_failure(tmp_path, caplog):
    """One bad pass does not end the daemon.

    A reaper that gives up because a single scan went wrong stops
    reclaiming memory exactly when the machine needs it most.
    """
    loop = _Loop(stop_after_waits=2)
    scans = []

    def fake_scan(_config):
        scans.append(1)
        if len(scans) == 1:
            raise RuntimeError("the machine is on fire")
        return ScanResult(verdicts=[], owners_alive=0)

    with caplog.at_level(logging.WARNING, logger="orphan_reaper"):
        code = reaper.run_daemon(
            ReaperConfig(plans_root=tmp_path, interval=1),
            stop=loop,
            clock=lambda: 0.0,
            scan_fn=fake_scan,
        )

    assert code == 0
    assert len(scans) == 2
    assert any("scan failed" in record.message for record in caplog.records)


def test_daemon_writes_the_event_log(tmp_path):
    """Every pass leaves a record of what it decided."""
    loop = _Loop(stop_after_waits=1)

    reaper.run_daemon(
        ReaperConfig(plans_root=tmp_path, interval=1, dry_run=True),
        stop=loop,
        clock=lambda: 0.0,
        scan_fn=lambda config: ScanResult(verdicts=[make_verdict()], owners_alive=0),
    )

    lines = events_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["pid"] == 1234


# --------------------------------------------------------------------------
# The breakers above the cadence
# --------------------------------------------------------------------------


def test_breaker_alerts_before_the_cleanup(tmp_path):
    """A flood is announced *before* the memory is given back.

    The alert is what tells an operator that the box is in trouble; it
    has to arrive while the cleanup is still worth announcing.
    """
    order = []
    config = ReaperConfig(plans_root=tmp_path, burst_threshold=1)
    result = ScanResult(
        verdicts=[make_verdict(1234), make_verdict(1235)], owners_alive=0
    )

    def alert(_config, message, **kwargs):
        order.append(("alert", message))

    def reap(verdicts, config, **kwargs):
        order.append(("reap", len(list(verdicts))))
        return list(verdicts)

    reaper.run_once(
        config,
        scan_fn=lambda config: result,
        alert_fn=alert,
        reap_fn=reap,
        swap_ratio=0.0,
    )

    assert order[0][0] == "alert"
    assert order[1][0] == "reap"


def test_a_broken_alert_does_not_stop_the_cleanup(tmp_path):
    """The alert is a courtesy; the cleanup is the point.

    A channel that cannot deliver costs the alert and nothing else —
    the memory is still given back.
    """
    reaped = []

    def alert(_config, _message, **kwargs):
        raise RuntimeError("the channel is down")

    def reap(verdicts, config, **kwargs):
        reaped.extend(list(verdicts))
        return list(verdicts)

    config = ReaperConfig(plans_root=tmp_path, burst_threshold=1)
    result = ScanResult(verdicts=[make_verdict(1234)], owners_alive=0)

    reaper.run_once(
        config,
        scan_fn=lambda config: result,
        alert_fn=alert,
        reap_fn=reap,
        swap_ratio=0.0,
    )

    assert len(reaped) == 1


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def test_main_prints_the_configuration_when_no_mode_is_given(tmp_path, capsys, monkeypatch):
    """Without a mode the reaper only reports how it is configured."""
    monkeypatch.setenv("PDT_ORPHAN_REAPER", "1")

    code = reaper.main(["--plans-root", str(tmp_path)])

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["plans_root"] == str(tmp_path)
    assert printed["dry_run"] is False


def test_main_dispatches_to_the_daemon(tmp_path, monkeypatch):
    """``--daemon`` starts the resident mode, and nothing else."""
    seen = {}

    def fake_run_daemon(config, **kwargs):
        seen["config"] = config
        return 0

    monkeypatch.setenv("PDT_ORPHAN_REAPER", "1")
    monkeypatch.setattr(reaper, "run_daemon", fake_run_daemon)

    code = reaper.main(["--daemon", "--interval", "7", "--plans-root", str(tmp_path)])

    assert code == 0
    assert seen["config"].interval == 7
    assert seen["config"].plans_root == tmp_path


def test_once_and_daemon_are_mutually_exclusive(tmp_path):
    """One mode at a time: the parser refuses the combination."""
    parser = reaper.build_arg_parser()

    try:
        parser.parse_args(["--once", "--daemon"])
    except SystemExit:
        return
    raise AssertionError("the parser accepted --once --daemon")
