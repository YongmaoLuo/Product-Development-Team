"""The reaping stage: ``SIGTERM``, the grace period, then ``SIGKILL``.

The ladder decides; this stage **acts**.  Acting is where a mistake
kills a process somebody still needs, so the stage is built around three
properties, and each has a test below.

**Nothing is signalled that should not be.**

* A verdict that is not a *kill* is not a candidate at all.
* A dry run decides and stops — not one signal leaves the process.
* The reaper never signals itself and never signals pid 1.

**Nothing is signalled in a way that reaches further than the verdict.**

* A process *group* is only ever signalled when the candidate **leads**
  it (``pgid == pid``).  A group led by somebody else holds processes
  the ladder never judged, and signalling it would take them out too.
* The POSIX sentinels are refused: ``pgid == 0`` is our own group,
  ``pgid == 1`` is *every* process we may signal.  Both are lethal in
  one direction and harmless in the other, which is exactly how such a
  bug hides from the machine it was written on.
* Our own group is refused for the same reason ``kill_process_group``
  refuses it: signalling it takes down the caller — under pytest, the
  whole session.

**The judgement is re-checked immediately before the signal.**

Scanning and signalling are separated by time, and an owner that was
dead when the machine was scanned can be alive when the signal goes
out.  A second probe sits between the two: an owner that came back
means no signal, and the verdict says why (``owner_revived``).

The stage takes its sender, its sleep and its liveness probe as
arguments, so all of that is testable without touching a real process.
The one test that *does* use a real process
(:func:`test_dry_run_sends_no_signal`) does so on purpose: "it did not
signal" only means something if there was something there to signal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    DECISION_KILL,
    DECISION_SKIP,
    REASON_ORPHAN,
    REASON_OWNER_REVIVED,
    REASON_SELF,
    SPAWN_KIND,
    SPAWN_OWNER_PID,
    SPAWN_OWNER_START,
    OwnerState,
    ProcInfo,
    ReaperConfig,
    Verdict,
    classify,
    reap_verdicts,
)

#: A pid that is certainly not running — the owner of our fixtures.
DEAD_OWNER_PID = 999_999

#: The pid of the fixture candidate.  Never a real process: every test
#: that touches it injects its sender, so nothing is ever signalled.
TARGET_PID = 4242


def make_proc(pid=TARGET_PID, pgid=None, env=None):
    """Build a candidate process.

    ``pgid`` defaults to the pid — the candidate leads its own group,
    which is the shape a spawned service has and the only shape in
    which the group may be signalled at all.
    """
    return ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid if pgid is None else pgid,
        uid=os.getuid(),
        rss_kb=1024,
        argv="/usr/bin/python3 -m backend.server",
        env=env,
    )


def make_verdict(proc=None, **kwargs):
    """Build the verdict the ladder produces for an orphan."""
    kwargs.setdefault("proc", proc or make_proc())
    kwargs.setdefault("decision", DECISION_KILL)
    kwargs.setdefault("reason", REASON_ORPHAN)
    kwargs.setdefault("owner_pid", DEAD_OWNER_PID)
    kwargs.setdefault("owner_start", "17")
    kwargs.setdefault("kind", "fixture")
    return Verdict(**kwargs)


def recorder():
    """A sender that only remembers which signals it was asked for."""
    calls = []

    def send(pid, sig_name, pgid):
        calls.append(sig_name)

    return calls, send


def owner_dead_target_alive(owner_pid=DEAD_OWNER_PID, target_pid=TARGET_PID):
    """A probe that says: the owner is gone, the target is not."""

    def probe(pid):
        return pid != owner_pid

    return probe


# --------------------------------------------------------------------------
# The dry run
# --------------------------------------------------------------------------


def test_dry_run_sends_no_signal():
    """A dry run decides and never acts.

    The target is a **real** process — a real child of this test,
    carrying the PDT markers from the moment it was started — because
    "no signal was sent" is only a statement about a process that
    could have been signalled.

    ``/bin/sleep`` would be the obvious fixture and the wrong one: on
    macOS a platform binary is protected by SIP, so its environment
    cannot be read back, and the candidate would be classified
    ``env_unreadable`` instead of ``orphan`` — it would never reach the
    reaping branch at all, and the test would pass without testing
    anything.  ``sys.executable`` is a plain binary the reaper can read.
    """
    env = dict(os.environ)
    env[SPAWN_OWNER_PID] = str(DEAD_OWNER_PID)
    env[SPAWN_OWNER_START] = "17"
    env[SPAWN_KIND] = "fixture"

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        proc = ProcInfo(
            pid=child.pid,
            ppid=os.getpid(),
            pgid=os.getpgrp(),
            uid=os.getuid(),
            rss_kb=0,
            argv=" ".join([sys.executable, "-c", "import time; time.sleep(30)"]),
            env=env,
        )
        config = ReaperConfig(dry_run=True)

        # The fixture is a genuine orphan: the ladder says so before
        # the reaping stage is ever asked to do anything.
        verdict = classify(
            proc,
            config=config,
            owner_state=OwnerState(alive=False),
            port_map={},
        )
        assert verdict.decision == DECISION_KILL
        assert verdict.reason == REASON_ORPHAN

        out = reap_verdicts([verdict], config)

        assert out[0].signal == "none"
        assert child.poll() is None, "a dry run must not signal anything"
    finally:
        child.kill()
        child.wait()


def test_dry_run_leaves_non_kill_verdicts_alone():
    """A dry run touches nothing that was not going to be signalled."""
    calls, send = recorder()
    verdict = make_verdict(decision=DECISION_SKIP, reason=REASON_SELF)

    out = reap_verdicts(
        [verdict],
        ReaperConfig(dry_run=True),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert calls == []
    assert out[0].signal == ""
    assert out[0].decision == DECISION_SKIP


# --------------------------------------------------------------------------
# TERM, the grace period, KILL
# --------------------------------------------------------------------------


def test_term_then_kill_after_grace():
    """A target that ignores ``SIGTERM`` gets ``SIGKILL`` once the grace
    period is up.

    The grace is what makes the kill *polite*: a process that needs a
    moment to close its files gets that moment, and only a process that
    outlived the grace is killed outright.
    """
    calls, send = recorder()

    out = reap_verdicts(
        [make_verdict()],
        ReaperConfig(grace=0.3),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert calls == ["TERM", "KILL"]
    assert out[0].signal == "KILL"


def test_term_only_when_the_target_exits_within_the_grace():
    """A target that exits after ``SIGTERM`` is never killed."""
    calls, send = recorder()

    def probe(pid):
        # The owner is dead; the target is gone as soon as it is told to.
        return False if pid == DEAD_OWNER_PID else False

    out = reap_verdicts(
        [make_verdict()],
        ReaperConfig(grace=5.0),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=probe,
    )

    assert calls == ["TERM"]
    assert out[0].signal == "TERM"


def test_process_lookup_error_counts_as_gone():
    """``ProcessLookupError`` means the target is already gone.

    The kernel answering "no such process" is a *success* for the
    reaper, not an error: there is nothing left to kill.
    """
    calls, send = recorder()

    def probe(pid):
        if pid == DEAD_OWNER_PID:
            return False
        raise ProcessLookupError(pid)

    out = reap_verdicts(
        [make_verdict()],
        ReaperConfig(grace=1.0),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=probe,
    )

    assert calls == ["TERM"]
    assert out[0].signal == "TERM"


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("pgid_kind", ["zero", "init", "own"])
def test_refuses_own_and_init_group(monkeypatch, pgid_kind):
    """``pgid`` of 0, 1 or our own group never becomes a ``killpg``.

    ``killpg(p, sig)`` is ``kill(-p, sig)``, so ``p`` is not a group id
    in the ordinary sense:

    ``p == 0``
        our own process group — signalling it takes down the caller,
        and under pytest, the whole session.
    ``p == 1``
        every process we may signal.  On Linux that is the CI job; on
        macOS ``libc`` refuses it, which is why this bug hides from the
        machine it was written on.

    Both degrade to a plain single-pid signal, which is what the test
    below asserts: the target is signalled, the group never is.
    """
    pgid = {"zero": 0, "init": 1, "own": os.getpgrp()}[pgid_kind]

    kills = []
    killpgs = []
    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(
        reaper.os, "killpg", lambda pgid, sig: killpgs.append((pgid, sig))
    )

    reap_verdicts(
        [make_verdict(make_proc(pgid=pgid))],
        ReaperConfig(grace=0.2),
        send=None,
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert killpgs == [], "the group must never be signalled"
    assert kills, "the process itself must still be signalled"
    assert {pid for pid, _sig in kills} == {TARGET_PID}


def test_group_is_signalled_when_the_candidate_leads_it(monkeypatch):
    """The positive control for the group path.

    When the candidate leads its own group, the group holds the
    processes it started — the ones the ladder judged together with it
    — so the group is the right target, and the group is signalled.
    """
    kills = []
    killpgs = []
    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(
        reaper.os, "killpg", lambda pgid, sig: killpgs.append((pgid, sig))
    )

    reap_verdicts(
        [make_verdict(make_proc(pgid=TARGET_PID))],
        ReaperConfig(grace=0.2),
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert killpgs, "the group the candidate leads must be signalled"
    assert {pgid for pgid, _sig in killpgs} == {TARGET_PID}
    assert kills == [], "the group is the target; the pid is not signalled too"


def test_group_is_never_signalled_when_another_process_leads_it(monkeypatch):
    """A group led by somebody else holds processes we never judged."""
    kills = []
    killpgs = []
    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(
        reaper.os, "killpg", lambda pgid, sig: killpgs.append((pgid, sig))
    )

    reap_verdicts(
        [make_verdict(make_proc(pgid=TARGET_PID + 1))],
        ReaperConfig(grace=0.2),
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert killpgs == []
    assert {pid for pid, _sig in kills} == {TARGET_PID}


def test_never_signals_itself_or_pid_one():
    """The reaper is not a candidate for itself, and pid 1 never is."""
    calls, send = recorder()

    out = reap_verdicts(
        [
            make_verdict(make_proc(pid=os.getpid())),
            make_verdict(make_proc(pid=1)),
        ],
        ReaperConfig(grace=0.2),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert calls == []
    assert all(v.signal == "" for v in out)
    assert all(v.decision == DECISION_SKIP for v in out)
    assert all(v.reason == REASON_SELF for v in out)


# --------------------------------------------------------------------------
# The TOCTOU re-check
# --------------------------------------------------------------------------


def test_skips_when_owner_revived():
    """An owner that came back between the scan and the signal: no signal.

    This is the race the whole stage exists to close.  The scan said
    "orphan"; by the time the signal was about to go out, the owner was
    running again — which means the candidate is in use, and killing it
    would be a mistake that cannot be undone.
    """
    calls, send = recorder()
    probed = []

    def probe(pid):
        probed.append(pid)
        return True  # everything is alive, the owner included

    verdict = make_verdict()

    out = reap_verdicts(
        [verdict],
        ReaperConfig(grace=0.2),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=probe,
    )

    assert calls == [], "a revived owner means no signal at all"
    assert DEAD_OWNER_PID in probed, "the owner must be re-probed"
    assert out[0].reason == REASON_OWNER_REVIVED
    assert out[0].decision == DECISION_SKIP
    assert out[0].signal == ""


# --------------------------------------------------------------------------
# One failure does not stop the rest
# --------------------------------------------------------------------------


def test_one_failure_does_not_stop_the_others():
    """A verdict that blows up is contained; the others still get reaped."""
    calls, send = recorder()

    def probe(pid):
        if pid == 999_998:  # the second verdict's owner
            raise RuntimeError("the probe exploded")
        return pid != DEAD_OWNER_PID

    good = make_verdict()
    bad = make_verdict(
        make_proc(pid=4243),
        owner_pid=999_998,
    )

    out = reap_verdicts(
        [bad, good],
        ReaperConfig(grace=0.2),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=probe,
    )

    assert len(out) == 2
    assert calls == ["TERM", "KILL"], "the healthy verdict is still reaped"
    assert out[1].signal == "KILL"


def test_a_failing_sender_does_not_stop_the_others():
    """A sender that raises is contained too."""
    calls = []

    def send(pid, sig_name, pgid):
        if pid == 4243:
            raise PermissionError("nope")
        calls.append(sig_name)

    out = reap_verdicts(
        [
            make_verdict(make_proc(pid=4243)),
            make_verdict(),
        ],
        ReaperConfig(grace=0.2),
        send=send,
        sleep=lambda seconds: None,
        alive_probe=owner_dead_target_alive(),
    )

    assert len(out) == 2
    assert calls == ["TERM", "KILL"]
