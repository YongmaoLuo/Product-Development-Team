"""Zero impact: an unmarked process is never touched, and the log proves it.

Background
----------
§10 of the acceptance contract asks for *evidence*, not for a promise:
an unmarked process must suffer **no effect whatsoever** while the
reaper runs, and a single full scan's skipped statistics must prove it.

Two claims hide inside that sentence, and each has its own way of
being wrong:

  * **"no effect"** — the reaper might judge an unmarked process
    ``env_unreadable`` and skip it for the *wrong* reason.  That is a
    different claim: it would prove "a process whose environment
    cannot be read is not killed", not "a process that carries no
    marker is not killed".  Only the second is the property PDT
    actually relies on, because a *readable* unmarked process is the
    common case — every process on the machine that PDT did not start.

  * **"proved by statistics"** — a scan that found nothing at all
    proves nothing.  The skipped distribution has to name the three
    children as ``unmarked``, and every kill the scan decided on has to
    carry a marker.  That is what makes "unmarked" a *reason to skip*
    rather than a reason that happens to be unused.

The fixture therefore uses **this interpreter**, never ``/bin/sleep``:
an Apple platform binary is protected by SIP, ``ps -E`` shows its
command line but not its environment, and the verdict would come back
``env_unreadable`` — the wrong reason, which is exactly the failure
mode this file exists to rule out.

What each test pins
-------------------
``test_unmarked_processes_survive_scan``
    A real, scoped pass judges the three children and leaves them
    running: ``poll()`` is ``None`` for all three afterwards.

``test_unmarked_never_reach_kill_decision``
    A full, unscoped, dry-run pass puts the three children in the
    skipped distribution as ``unmarked`` — never ``env_unreadable`` —
    and every kill it decided on carries a marker.

``test_no_kill_events_written``
    The pass writes down what it decided, and no record in it says a
    signal went out.

``test_the_suite_stops_the_resident_daemons_it_leaked``
    A resident reaper daemon this session started is stopped when the
    session ends, instead of outliving it on the machine.

``test_the_sweep_leaves_the_daemons_of_other_sessions_alone``
    A daemon that belongs to another session is not touched.

The scope of the blast radius
-----------------------------
The unscoped pass (the second test) is a **dry run**: it decides and
stops, and not one signal leaves the process.  The real pass (the first
and the third) is **scoped** to the three children, which are
unmarked, so the ladder skips them before the reaping stage sees
anything to act on.  Nothing outside ``tmp_path`` — the plan-artifact
root — is ever written.
"""

from __future__ import annotations

import collections
import json
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
from orphan_reaper import (  # noqa: E402
    DECISION_KILL,
    DECISION_SKIP,
    REASON_ENV_UNREADABLE,
    REASON_ORPHAN,
    REASON_UNMARKED,
    SIGNAL_KILL,
    SIGNAL_TERM,
    SPAWN_KIND,
    SPAWN_OWNER_PID,
    SPAWN_OWNER_START,
    ReaperConfig,
    events_path,
    read_process_environ,
)

pytestmark = pytest.mark.integration


#: How many unmarked children the fixture spawns.
CHILD_COUNT = 3

#: The children's program.  The sleep has to outlast the **slowest**
#: pass in this file — the unscoped one — and that pass is slow for a
#: reason that has nothing to do with the code under test: it reads the
#: environment of *every* process on the machine, one ``ps -E`` fork
#: per process.  On a busy box that is tens of seconds, and a child
#: that dies half-way through the scan comes back as
#: ``env_unreadable`` — a verdict about the *scan*, not about the
#: child, and a completely different claim from the one this file
#: exists to make.  The child therefore has to stay alive for the
#: whole scan; the fixture stops it the moment the test is over, so the
#: long sleep costs nothing at the end.
CHILD_CODE = "import time; time.sleep(300)"

#: The program of the stand-in for a **resident reaper daemon**.  It
#: sleeps like the children above, but its command line has to look like
#: a daemon's: the sweep that stops leaked daemons finds them by their
#: command line, so a stand-in without one would prove nothing.
DAEMON_CODE = "import time; time.sleep(300)"

#: The command line of the stand-in.  The real daemon is started as
#: ``python -m backend.orphan_reaper --daemon --interval 30
#: --plans-root <dir>``; the arguments after the program are what the
#: sweep reads.
DAEMON_ARGV = ("backend.orphan_reaper", "--daemon", "--interval", "30")

#: The environment the children are *launched* with: ``PATH`` and
#: nothing else.  It is built from scratch rather than inherited so
#: that no ``PDT_SPAWN_*`` marker can reach a child — not even one the
#: test runner itself happens to carry, which it does whenever the
#: runner is itself a PDT-spawned process.
CHILD_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

#: The markers that make a process *not* unmarked.
MARKER_NAMES = (SPAWN_OWNER_PID, SPAWN_OWNER_START, SPAWN_KIND)

#: How long to wait for a child to become visible to ``ps`` (or
#: ``/proc``) with a readable environment.
READY_TIMEOUT = 30.0

#: How long to wait for a terminated child to go away.
TERMINATE_TIMEOUT = 15.0

#: How long to let a signal that was sent a moment ago land, before
#: asking "is it still running?".
#:
#: Without this the survival check would answer the wrong question: a
#: child that was signalled but has not died *yet* still reports
#: ``poll() is None``, so "still running" would be indistinguishable
#: from "not signalled".  A ``SIGTERM`` to a sleeping child lands in
#: milliseconds; two seconds is the margin.
SETTLE_SECONDS = 2.0


class _UnmarkedChildren:
    """The children the fixture spawned, and what each was seen carrying.

    ``procs`` are the live :class:`subprocess.Popen` objects — ``poll()``
    on them answers "is it still running?".  ``envs`` maps every pid to
    the environment the reaper's own reader saw, which is the evidence
    that the children are *readable* (their verdict can be ``unmarked``
    rather than ``env_unreadable``) and *unmarked* (they carry no
    ``PDT_SPAWN_*`` marker at all).
    """

    def __init__(self, procs, envs):
        self.procs = procs
        self.envs = envs

    @property
    def pids(self):
        """The pids of the children, in the order they were spawned."""
        return tuple(proc.pid for proc in self.procs)


def _wait_until_its_environment_can_be_read(pid, timeout=READY_TIMEOUT):
    """Return *pid*'s environment, or fail the test if it cannot be read.

    A child becomes visible to ``ps`` (or ``/proc``) a moment after it
    is spawned; the wait covers that window only.

    The environment has to come back **readable** — an unreadable one
    would be judged ``env_unreadable``, which is a different claim from
    the one this file exists to prove.  It also has to come back
    **without a marker**: a child that carried one would not be an
    unmarked process, and the whole test would be measuring nothing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        env = read_process_environ(pid)
        if env is not None:
            for name in MARKER_NAMES:
                assert name not in env, (
                    "the child %d carries %s, so it is not an unmarked "
                    "process" % (pid, name)
                )
            return env
        time.sleep(0.05)
    raise AssertionError(
        "the child %d never became readable; an unreadable environment "
        "would be judged 'env_unreadable', which proves nothing about "
        "unmarked processes" % pid
    )


def _terminate_all(procs):
    """Stop every child and wait for it — a fixture must not leak processes."""
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=TERMINATE_TIMEOUT)


def _settle(seconds=SETTLE_SECONDS):
    """Let a signal that was just sent land, so "still running" means it.

    ``poll()`` answers "has the process exited *yet*", and a process that
    was signalled a millisecond ago has not exited yet.  Waiting a
    moment first is what turns "still running" into "was not signalled".
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)


def _wait_until_the_daemon_is_visible(pid, timeout=READY_TIMEOUT):
    """Wait until *pid* shows up in the process listing, or give up.

    A process is a moment away from being listed; the wait covers that
    window only.  The answer is a boolean rather than an assertion,
    because the *caller* is the thing that has to fail when the daemon
    never shows up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for line in listing.splitlines():
            if line.strip().split(" ", 1)[0] == str(pid):
                return True
        time.sleep(0.05)
    return False


def _spawn_a_stand_in_daemon(plans_root):
    """Start a process whose command line looks like a resident daemon.

    It is not a daemon — it never reaps anything — but it is *shaped*
    like one, which is all the sweep looks at.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            DAEMON_CODE,
            *DAEMON_ARGV,
            "--plans-root",
            str(plans_root),
        ],
        env=dict(CHILD_ENV),
    )


def _read_event_records(path):
    """The records in the kill-event log, or an empty list if there is none."""
    target = Path(path)
    if not target.exists():
        return []
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def unmarked_children():
    """Three real processes, launched without a marker, alive for the test."""
    procs = []
    envs = {}
    try:
        for _ in range(CHILD_COUNT):
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", CHILD_CODE], env=dict(CHILD_ENV)
                )
            )
        for proc in procs:
            envs[proc.pid] = _wait_until_its_environment_can_be_read(proc.pid)
        yield _UnmarkedChildren(procs, envs)
    finally:
        _terminate_all(procs)


def test_unmarked_processes_survive_scan(tmp_path, unmarked_children):
    """A real, scoped pass leaves an unmarked process exactly where it was.

    The scope is the gate that keeps the pass honest: it judges these
    three pids and nothing else.  Two things have to hold afterwards —
    the pass did not decide to kill them, and it did not kill them
    anyway.  The second is the one that matters; the first is only how
    it ought to have happened.
    """
    pids = unmarked_children.pids

    result = reaper.run_once(ReaperConfig(plans_root=tmp_path, pid_scope=pids))

    verdicts = {verdict.proc.pid: verdict for verdict in result.verdicts}
    for pid in pids:
        assert pid in verdicts, "the pass did not see pid %d at all" % pid
        assert verdicts[pid].decision == DECISION_SKIP
        assert verdicts[pid].reason == REASON_UNMARKED

    # Give a signal that was sent a moment ago the time to land, so
    # "still running" means "not signalled" and not "has not died yet".
    _settle()
    assert [proc.poll() for proc in unmarked_children.procs] == [None] * CHILD_COUNT


def test_unmarked_never_reach_kill_decision(tmp_path, unmarked_children):
    """A full, unscoped, dry-run pass: no unmarked process is ever a kill.

    This is the scan whose skipped statistics §10 asks for, and it
    asserts two separate things:

    * the three children are in the distribution as ``unmarked`` — and
      **not** as ``env_unreadable``, which would prove a different
      thing entirely (that an unreadable process is spared, not that an
      unmarked one is);
    * every kill the scan decided on carries a marker, so "unmarked" can
      never be a reason to kill.
    """
    pids = unmarked_children.pids

    result = reaper.run_once(ReaperConfig(plans_root=tmp_path, dry_run=True))

    verdicts = {verdict.proc.pid: verdict for verdict in result.verdicts}
    for pid in pids:
        assert pid in verdicts, "the full pass did not see pid %d at all" % pid
        verdict = verdicts[pid]
        assert verdict.reason == REASON_UNMARKED
        assert verdict.reason != REASON_ENV_UNREADABLE
        assert verdict.decision == DECISION_SKIP

    for verdict in result.verdicts:
        if verdict.decision != DECISION_KILL:
            continue
        assert verdict.owner_pid is not None and verdict.owner_pid > 0, (
            "pid %s was decided to kill without carrying a marker"
            % verdict.proc.pid
        )
        assert verdict.reason != REASON_UNMARKED

    distribution = collections.Counter(
        verdict.reason
        for verdict in result.verdicts
        if verdict.decision == DECISION_SKIP
    )
    print(
        "skipped reason distribution: %s"
        % json.dumps(dict(distribution), sort_keys=True)
    )
    assert distribution.get(REASON_UNMARKED, 0) >= CHILD_COUNT


def test_no_kill_events_written(tmp_path, unmarked_children):
    """The pass writes down what it decided — and what it decided was no kill.

    The log may legitimately be absent (a scan that failed writes
    nothing); what it may never contain is a kill.  The records about
    the children, when they are there, must say ``unmarked`` and must
    carry no signal.
    """
    pids = unmarked_children.pids

    result = reaper.run_once(ReaperConfig(plans_root=tmp_path, pid_scope=pids))

    # The pass really ran: it judged the children and skipped them.
    verdicts = {verdict.proc.pid: verdict for verdict in result.verdicts}
    for pid in pids:
        assert verdicts[pid].decision == DECISION_SKIP
        assert verdicts[pid].reason == REASON_UNMARKED

    records = _read_event_records(events_path(tmp_path))
    kills = [
        record
        for record in records
        if record.get("signal") in (SIGNAL_TERM, SIGNAL_KILL)
        or record.get("reason") == REASON_ORPHAN
    ]
    assert kills == [], "the pass wrote kill records: %r" % kills

    by_pid = {record.get("pid"): record for record in records}
    for pid in pids:
        record = by_pid.get(pid)
        if record is None:
            continue
        assert record["reason"] == REASON_UNMARKED
        assert record["signal"] == ""


def test_the_suite_stops_the_resident_daemons_it_leaked(tmp_path):
    """A resident reaper daemon of this session does not outlive the session.

    The backend starts a resident reaper daemon when it boots, and a
    test that boots a real backend leaves one behind: a **detached**
    process, still running after the test that made it is over.  It is
    not idle, either — it scans the whole machine every 30 seconds,
    forking ``ps`` once per process on it — so a suite that leaks a
    dozen of them turns a quiet runner into a busy one, and the boot of
    the *next* server waits behind the fork storm of the daemons the
    previous tests left behind.  That is how a green suite starts
    failing on the second run on the same machine.

    Which daemons are "ours" is not a guess: the daemon's plans root
    names the session that started it, and a session may clean up its
    own temporary directory and nothing else.
    """
    from tests.conftest import _stop_reaper_daemons_under

    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    daemon = _spawn_a_stand_in_daemon(plans_root)
    try:
        assert _wait_until_the_daemon_is_visible(daemon.pid), (
            "the stand-in daemon never showed up in the process listing"
        )
        stopped = _stop_reaper_daemons_under(tmp_path)
        assert daemon.pid in stopped, (
            "the daemon under this session's own directory was not "
            "stopped; stopped=%r" % (stopped,)
        )
        _settle()
        assert daemon.poll() is not None, (
            "the daemon was signalled but is still running"
        )
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=TERMINATE_TIMEOUT)


def test_the_sweep_leaves_the_daemons_of_other_sessions_alone(tmp_path):
    """A daemon that belongs to another session is not touched.

    The sweep is allowed to stop the daemons of the session that owns
    the directory it is cleaning, and nothing else: a daemon of a
    concurrent run, of the developer's own backend, or of any other
    tool has to survive it.  The rule is the plans root — the only
    thing on the command line that says whose the daemon is.
    """
    from tests.conftest import _stop_reaper_daemons_under

    elsewhere = tmp_path.parent / "another-session"
    elsewhere.mkdir(exist_ok=True)
    plans_root = elsewhere / "plans"
    plans_root.mkdir(exist_ok=True)
    daemon = _spawn_a_stand_in_daemon(plans_root)
    try:
        assert _wait_until_the_daemon_is_visible(daemon.pid), (
            "the stand-in daemon never showed up in the process listing"
        )
        stopped = _stop_reaper_daemons_under(tmp_path)
        assert daemon.pid not in stopped, (
            "the sweep stopped a daemon that belongs to another session; "
            "stopped=%r" % (stopped,)
        )
        assert daemon.poll() is None, (
            "the daemon of another session was stopped"
        )
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=TERMINATE_TIMEOUT)
