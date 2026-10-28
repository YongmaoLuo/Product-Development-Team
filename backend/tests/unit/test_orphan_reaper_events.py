"""The audit trail, the breakers and the alert.

A reaper that leaves no trace is a reaper that cannot be explained, so
every verdict — the ones that were killed *and* the ones that were
skipped — is written to a JSON-lines log.  Two breakers can demand that
the cleanup happen *now* rather than at the next interval: a flood of
candidates, or a machine whose swap is nearly full.  And the alert that
says so must never be able to break the cleanup: a channel that is
missing or broken costs the alert, never the memory it was announcing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    DECISION_KILL,
    DECISION_SKIP,
    REASON_ORPHAN,
    REASON_UNMARKED,
    ProcInfo,
    ReaperConfig,
    ScanResult,
    Verdict,
    alerts_path,
    emit_alert,
    events_path,
    should_force_cleanup,
    write_events,
)

#: A pid that is certainly not running — the owner of our fixtures.
DEAD_OWNER_PID = 999_999

#: The pid of the fixture candidate.  Never a real process: nothing in
#: this file signals anything.
TARGET_PID = 4242


def make_proc(pid=TARGET_PID):
    """Build the process a verdict is about."""
    return ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid,
        uid=os.getuid(),
        rss_kb=213000,
        argv="/usr/bin/python3 -m backend.server",
    )


def make_verdict(pid=TARGET_PID, signal="TERM", **kwargs):
    """Build the verdict the ladder produces for an orphan."""
    kwargs.setdefault("proc", make_proc(pid))
    kwargs.setdefault("decision", DECISION_KILL)
    kwargs.setdefault("reason", REASON_ORPHAN)
    kwargs.setdefault("owner_pid", DEAD_OWNER_PID)
    kwargs.setdefault("owner_start", "17")
    kwargs.setdefault("kind", "fixture")
    kwargs.setdefault("signal", signal)
    return Verdict(**kwargs)


def make_scan(count, signal="TERM"):
    """Build the result of a scan that saw *count* candidates."""
    return ScanResult(
        verdicts=[
            make_verdict(pid=TARGET_PID + index, signal=signal)
            for index in range(count)
        ],
        owners_alive=0,
    )


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------


def test_write_events_appends_jsonl(tmp_path):
    """Two writes leave two lines: the log is appended to, never rewritten.

    A reaper that runs every thirty seconds must not lose the record of
    what it did the last thirty seconds ago, so the second write has to
    land *after* the first one rather than instead of it.
    """
    path = events_path(tmp_path)

    write_events(path, [make_verdict()])
    write_events(path, [make_verdict(pid=TARGET_PID + 1, signal="KILL")])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["ts"]
    assert first["pid"] == TARGET_PID
    assert first["ppid"] == 1
    assert first["pgid"] == TARGET_PID
    assert first["argv"] == "/usr/bin/python3 -m backend.server"
    assert first["rss"] == 213000
    assert first["owner_pid"] == DEAD_OWNER_PID
    assert first["owner_start"] == "17"
    assert first["reason"] == REASON_ORPHAN
    assert first["signal"] == "TERM"

    second = json.loads(lines[1])
    assert second["pid"] == TARGET_PID + 1
    assert second["signal"] == "KILL"


def test_write_events_creates_the_parent_directory(tmp_path):
    """The log's directory does not exist yet on the first run."""
    path = events_path(tmp_path)
    assert not path.parent.exists()

    write_events(path, [make_verdict()])

    assert path.exists()


def test_write_events_stamps_an_injected_timestamp(tmp_path):
    """A caller that wants a fixed stamp passes one."""
    path = events_path(tmp_path)

    write_events(path, [make_verdict()], ts="2026-10-27T10:00:00")

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["ts"] == "2026-10-27T10:00:00"


def test_write_events_survives_a_write_failure(tmp_path, caplog):
    """A log that cannot be written costs the log — nothing else.

    The reaper exists to give memory back.  A full disk or a directory
    that is not a directory must not stop it from doing that, so the
    failure is a warning and the call returns.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    path = blocker / "kill_events.jsonl"

    with caplog.at_level(logging.WARNING, logger="orphan_reaper"):
        write_events(path, [make_verdict()])

    assert any("kill events" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# The breakers
# --------------------------------------------------------------------------


def test_burst_breaker():
    """More candidates than the threshold: the cleanup cannot wait.

    The comparison is strict — a candidate count *at* the threshold is
    the last count that is still fine.
    """
    config = ReaperConfig(burst_threshold=50)

    assert should_force_cleanup(make_scan(51), config) == (True, "burst:51")
    assert should_force_cleanup(make_scan(50), config)[0] is False


def test_burst_breaker_counts_a_plain_list_of_verdicts():
    """A caller that hands over the verdicts themselves gets the same answer."""
    config = ReaperConfig(burst_threshold=50)
    verdicts = [make_verdict(pid=TARGET_PID + index) for index in range(51)]

    assert should_force_cleanup(verdicts, config) == (True, "burst:51")


def test_burst_breaker_counts_only_the_decidable_orphans():
    """A crowded machine is not a flood of orphans.

    The scan judges *every* process on the machine and leaves a verdict
    for each one — the ones that were skipped for carrying no marker,
    for belonging to another user, for being the reaper itself.  None of
    those is an orphan.  Counting them would trip the breaker on every
    machine, every interval, and a breaker that is always on is not a
    breaker.
    """
    config = ReaperConfig(burst_threshold=50)
    crowd = [
        make_verdict(
            pid=TARGET_PID + index, decision=DECISION_SKIP, reason=REASON_UNMARKED
        )
        for index in range(500)
    ]

    forced, reason = should_force_cleanup(
        ScanResult(verdicts=crowd, owners_alive=0), config
    )

    assert (forced, reason) == (False, "")


def test_burst_breaker_counts_the_orphans_among_a_crowd():
    """The orphans are counted; the crowd around them is not.

    This is the shape of the incident the breaker was written for: a
    machine carrying a few hundred ordinary processes and, among them, a
    flood of orphans that have to be cleaned up now.
    """
    config = ReaperConfig(burst_threshold=50)
    verdicts = [
        make_verdict(
            pid=TARGET_PID + index, decision=DECISION_SKIP, reason=REASON_UNMARKED
        )
        for index in range(500)
    ]
    verdicts += [make_verdict(pid=9000 + index) for index in range(51)]

    forced, reason = should_force_cleanup(
        ScanResult(verdicts=verdicts, owners_alive=0), config
    )

    assert (forced, reason) == (True, "burst:51")


def test_swap_breaker():
    """Swap that is nearly full is memory that has already been lost."""
    config = ReaperConfig(swap_threshold=90)

    forced, reason = should_force_cleanup(make_scan(1), config, swap_ratio=0.95)
    assert forced is True
    assert reason == "swap:0.95"

    assert should_force_cleanup(make_scan(1), config, swap_ratio=0.90)[0] is False

    # An unreadable swap usage is *unknown*, never "no swap in use".
    assert should_force_cleanup(make_scan(1), config, swap_ratio=None)[0] is False


def test_swap_threshold_is_read_as_a_percentage():
    """The configured threshold is a percent; the reading is a fraction."""
    config = ReaperConfig(swap_threshold=50)

    assert should_force_cleanup(make_scan(1), config, swap_ratio=0.51)[0] is True
    assert should_force_cleanup(make_scan(1), config, swap_ratio=0.50)[0] is False


def test_should_force_cleanup_never_signals(monkeypatch):
    """The breaker decides; it does not act.

    The signal is the business of the reaping stage, and a decision that
    signalled on its own would bypass every refusal that stage makes.
    """
    kills = []
    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(
        reaper.os, "killpg", lambda pgid, sig: kills.append((pgid, sig))
    )

    should_force_cleanup(make_scan(51), ReaperConfig(burst_threshold=50))

    assert kills == []


# --------------------------------------------------------------------------
# The alert
# --------------------------------------------------------------------------


def test_alert_failure_does_not_raise(tmp_path, capsys):
    """A broken alert channel costs the alert, never the cleanup.

    The channel is a courtesy: it tells somebody that the machine is in
    trouble.  The cleanup is the point, and it must not be held hostage
    by a notification that cannot be delivered.
    """
    config = ReaperConfig(plans_root=tmp_path)

    def notify(message):
        raise RuntimeError("the channel is down")

    emit_alert(config, "forced cleanup: burst:51", notify=notify)

    lines = alerts_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "forced cleanup: burst:51" in lines[0]
    assert "forced cleanup: burst:51" in capsys.readouterr().err


def test_alert_without_a_channel_is_logged(tmp_path, capsys):
    """No channel at all is the same situation: write it down."""
    config = ReaperConfig(plans_root=tmp_path)

    emit_alert(config, "forced cleanup: swap:0.93")

    lines = alerts_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "forced cleanup: swap:0.93"
    assert "forced cleanup: swap:0.93" in capsys.readouterr().err


def test_alert_reaches_the_injected_channel(tmp_path):
    """A channel that works is used, and the message arrives."""
    delivered = []
    config = ReaperConfig(plans_root=tmp_path)

    emit_alert(config, "forced cleanup: burst:51", notify=delivered.append)

    assert delivered == ["forced cleanup: burst:51"]


def test_alert_never_raises_even_without_a_place_to_write(capsys):
    """The alert must not raise — not even when there is nowhere to put it.

    "Never raises" is the whole contract: the alert announces the
    cleanup, and an announcement that becomes the failure stops the
    cleanup it was announcing.  A config that carries no plans root is
    the last way that can go wrong, and it still only costs the alert.
    """

    class Bare:
        """A config with nowhere to write."""

    emit_alert(Bare(), "forced cleanup: burst:51")

    assert "forced cleanup: burst:51" in capsys.readouterr().err
