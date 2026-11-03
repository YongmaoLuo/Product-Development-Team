"""One daemon per root, and a ceiling on the machine.

The reaper's singleton guarantee is a lock on a file under the plan
root.  That lock answers one question — "is a daemon holding *this*
root?" — and it answers it honestly, because the kernel drops an
``flock`` when the holder dies.

It does not answer the two questions that actually mattered on
2026-11-02, when this machine was running 145 reaper daemons at once:

* **"Is somebody *else* already watching this root?"**  The lock
  cannot say, because deleting the lock file and starting a new daemon
  gives the new daemon a *fresh inode* to lock.  The old daemon still
  holds the old one.  Two daemons, one root, both reaping.
* **"How many daemons is this machine running?"**  The lock is scoped
  to one root, so a daemon on another root neither excludes this one
  nor is excluded by it.  Nothing stopped them from accumulating.

So the census (the machine's own process table) is what answers both,
and this file pins what it says:

1. a daemon that finds another daemon already serving its root stands
   down — the lock did not catch that one, the census does;
2. a daemon that would push the machine past its ceiling stands down
   as well — the ceiling is on the machine, not on one root;
3. a daemon watching a root that is **gone** does not take a slot: it
   is watching nothing, and it is on its way out;
4. a ceiling of zero means no ceiling at all.

What is *not* pinned here is the scan loop, and — deliberately — the
machine the tests run on.  Every test says what the census must say
by handing it a process table, so a developer with ninety daemons
running on their box and a CI runner with none get the same answer.

The two questions are also asked of two different populations, and the
last section pins that too: "is somebody else already watching this
root?" is a correctness question and is asked of every daemon, while
"is the machine full?" is a bound on the machine's *resident* daemons
— the ones that are processes of their own and keep running after
whoever started them is gone — and is asked of a daemon that is
starting on its own.  A daemon whose lifetime its caller owns leaves
with the caller, is not in the census the quota counts, and is not
charged to the quota.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import ProcInfo, ReaperConfig  # noqa: E402
from tests.reaper_daemon_guard import reclaim_new_daemons  # noqa: E402


#: The command line of a reaper daemon, as the census reads it.
DAEMON_ARGV = "python -m backend.orphan_reaper --daemon --interval 30 --plans-root %s"


def _daemon_proc(pid: int, root: Path) -> ProcInfo:
    """Return a process snapshot of a reaper daemon serving *root*."""
    return ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid,
        uid=os.getuid(),
        rss_kb=0,
        argv=DAEMON_ARGV % root,
    )


def _daemon(pid: int, root: Path) -> "reaper.ReaperDaemon":
    """Return a daemon record for a root that is still on disk."""
    return reaper.ReaperDaemon(
        pid=pid,
        ppid=1,
        plans_root=str(root),
        started_at="990.0",
        age_seconds=1.0,
        plans_root_exists=True,
    )


def _live_roots(tmp_path: Path, count: int) -> "tuple[list[Path], list[ProcInfo]]":
    """Return *count* existing roots and one daemon process for each."""
    roots: list[Path] = []
    procs: list[ProcInfo] = []
    for index in range(count):
        root = tmp_path / ("plans%d" % index)
        root.mkdir()
        roots.append(root)
        procs.append(_daemon_proc(100 + index, root))
    return roots, procs


@pytest.fixture
def known_start(monkeypatch):
    """Make every process report the same known start time.

    The census reads the start time from the operating system, which
    for a pid that does not exist is nothing at all.  Pinning it here
    keeps the tests about the census rather than about ``ps``.
    """
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid: "990.0")


# ---------------------------------------------------------------------------
# One daemon per root
# ---------------------------------------------------------------------------


def test_duplicate_root_stands_down(tmp_path, known_start):
    """A second daemon on the same root is a duplicate, and is named.

    The lock did not catch this one — the lock file was recreated, so
    the new daemon locked a fresh inode while the old one kept the old
    one.  The census does catch it: both command lines name the same
    root.  The pid comes back because "somebody else is already
    watching this root" is a fact the operator wants to see.
    """
    root = tmp_path / "plans"
    root.mkdir()

    assert (
        reaper._another_daemon_for_root(
            root,
            procs=[_daemon_proc(9, root)],
            self_pid=7,
        )
        == 9
    )


def test_different_root_allowed(tmp_path, known_start):
    """A daemon on another root is not a duplicate.

    Two daemons on two roots are two different services: they reap
    different plans and they do not exclude each other.  Only the
    machine-wide ceiling (see below) limits how many of those there
    may be.
    """
    root = tmp_path / "plans"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    assert (
        reaper._another_daemon_for_root(
            root,
            procs=[_daemon_proc(9, other)],
            self_pid=7,
        )
        is None
    )


def test_the_caller_is_not_its_own_duplicate(tmp_path, known_start):
    """A daemon asking about its own root does not find itself.

    The census leaves the caller out by default, and the caller has to
    be able to leave it out explicitly too: a daemon started with
    ``--daemon`` on the command line is a daemon to the census, and
    would otherwise stand down against itself.
    """
    root = tmp_path / "plans"
    root.mkdir()

    assert (
        reaper._another_daemon_for_root(
            root,
            procs=[_daemon_proc(7, root)],
            self_pid=7,
        )
        is None
    )


# ---------------------------------------------------------------------------
# The machine-wide ceiling
# ---------------------------------------------------------------------------


def test_budget_exhausted_at_limit(tmp_path, known_start):
    """A machine already running its quota of daemons refuses another.

    The ceiling is on the machine, not on one root: a dozen daemons on
    a dozen roots is still a dozen daemons walking the whole process
    table every thirty seconds, and the machine they share is the one
    that runs out of memory.
    """
    _roots, procs = _live_roots(tmp_path, 8)
    config = ReaperConfig(plans_root=tmp_path, max_daemons=8)

    assert reaper._daemon_budget_exhausted(config, procs=procs) is True


def test_budget_zero_is_unlimited(tmp_path, known_start):
    """A ceiling of zero is no ceiling at all.

    Some deployments want a daemon on every root and are willing to
    pay for it; zero is how they say so.
    """
    _roots, procs = _live_roots(tmp_path, 8)
    config = ReaperConfig(plans_root=tmp_path, max_daemons=0)

    assert reaper._daemon_budget_exhausted(config, procs=procs) is False


def test_budget_not_exhausted_below_the_limit(tmp_path, known_start):
    """A machine below its ceiling takes another daemon.

    Seven daemons and a ceiling of eight is a machine that has room.
    """
    _roots, procs = _live_roots(tmp_path, 7)
    config = ReaperConfig(plans_root=tmp_path, max_daemons=8)

    assert reaper._daemon_budget_exhausted(config, procs=procs) is False


def test_a_dead_root_does_not_take_a_slot(tmp_path, known_start):
    """A daemon watching a root that is gone does not occupy a slot.

    It is watching nothing, and the daemon leaves by itself as soon as
    it notices.  Making it take a slot would let a machine full of
    dying daemons refuse to start a live one.
    """
    roots, procs = _live_roots(tmp_path, 8)
    for root in roots:
        shutil.rmtree(root)

    config = ReaperConfig(plans_root=tmp_path, max_daemons=8)

    assert reaper._daemon_budget_exhausted(config, procs=procs) is False


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------


def test_max_daemons_comes_from_the_environment():
    """The ceiling is configurable, and a typo falls back to the default.

    A reaper that refuses to start over a typo would leave the memory
    it exists to reclaim unreclaimed — so a value that cannot be read
    is replaced by the default rather than raised.
    """
    assert reaper.config_from_env({reaper.ENV_MAX_DAEMONS: "3"}).max_daemons == 3
    assert (
        reaper.config_from_env({reaper.ENV_MAX_DAEMONS: "0"}).max_daemons == 0
    )
    assert (
        reaper.config_from_env({reaper.ENV_MAX_DAEMONS: "many"}).max_daemons
        == reaper.DEFAULT_MAX_DAEMONS
    )
    assert (
        reaper.config_from_env({reaper.ENV_MAX_DAEMONS: "-1"}).max_daemons
        == reaper.DEFAULT_MAX_DAEMONS
    )
    assert reaper.config_from_env({}).max_daemons == reaper.DEFAULT_MAX_DAEMONS


# ---------------------------------------------------------------------------
# What the daemon does about it
# ---------------------------------------------------------------------------


def test_run_daemon_stands_down_when_the_root_is_taken(
    tmp_path, monkeypatch, caplog
):
    """The daemon leaves without scanning when its root is already served.

    It returns zero — the same answer as every other way a daemon
    leaves — and it says why, because "the daemon did not start" is
    otherwise a mystery an operator has to guess at.
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(
        reaper,
        "list_reaper_daemons",
        lambda **kwargs: [_daemon(9, root)],
    )

    stop = threading.Event()
    scans = []

    def fake_scan(_config):
        scans.append(1)
        stop.set()
        return reaper.ScanResult(verdicts=[], owners_alive=0)

    with caplog.at_level(logging.INFO, logger="orphan_reaper"):
        code = reaper.run_daemon(
            ReaperConfig(plans_root=root),
            stop=stop,
            clock=lambda: 0.0,
            scan_fn=fake_scan,
        )

    assert code == 0
    assert scans == []
    assert "another reaper is already watching" in caplog.text


def test_run_daemon_stands_down_when_the_budget_is_gone(
    tmp_path, monkeypatch, caplog
):
    """A resident daemon leaves without scanning when the machine is full.

    The daemons already running are on *other* roots — otherwise the
    duplicate check would have spoken first — so what stops this one
    is the machine-wide ceiling.

    The daemon here is a **resident** one: no stop event was handed
    in, so it is a process of its own that keeps running after
    whoever started it is gone.  That is the population the ceiling
    bounds.

    The clock and the lifetime are pinned so that a daemon that
    wrongly reached its scan loop would come back quickly with a
    scan to show for itself, rather than sitting in the loop until
    the test times out.
    """
    root = tmp_path / "plans"
    root.mkdir()
    others, _procs = _live_roots(tmp_path, 8)
    monkeypatch.setattr(
        reaper,
        "list_reaper_daemons",
        lambda **kwargs: [
            _daemon(100 + index, other) for index, other in enumerate(others)
        ],
    )

    scans = []
    ticks = iter([0.0, 10.0, 1000.0])

    def fake_scan(_config):
        scans.append(1)
        return reaper.ScanResult(verdicts=[], owners_alive=0)

    with caplog.at_level(logging.INFO, logger="orphan_reaper"):
        code = reaper.run_daemon(
            ReaperConfig(
                plans_root=root,
                max_daemons=8,
                interval=0,
                max_lifetime=100,
            ),
            clock=lambda: next(ticks, 1000.0),
            scan_fn=fake_scan,
        )

    assert code == 0
    assert scans == []
    assert "daemon budget 8 exhausted" in caplog.text


def test_an_embedded_daemon_is_not_charged_to_the_machine_quota(
    tmp_path, monkeypatch
):
    """A daemon whose lifetime its caller owns is not on the machine's books.

    The ceiling is a bound on the machine's **resident** daemons: the
    ones that are processes of their own, that outlive whoever started
    them, and that nothing shuts down.  A caller that brings its own
    stop event runs the loop inside a process it owns — that daemon
    leaves with the caller, it is not in the census the quota counts,
    and it adds nothing to the population the ceiling bounds.  So a
    machine that is over its quota does not stop it.

    The same-root check is asked of every daemon, resident or not:
    two daemons on one root reap the same candidates twice, and that
    is a correctness question rather than a bookkeeping one.
    """
    root = tmp_path / "plans"
    root.mkdir()
    others, _procs = _live_roots(tmp_path, 8)
    monkeypatch.setattr(
        reaper,
        "list_reaper_daemons",
        lambda **kwargs: [
            _daemon(100 + index, other) for index, other in enumerate(others)
        ],
    )

    stop = threading.Event()
    scans = []

    def fake_scan(_config):
        scans.append(1)
        stop.set()
        return reaper.ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=root, max_daemons=8),
        stop=stop,
        clock=lambda: 0.0,
        scan_fn=fake_scan,
    )

    assert code == 0
    assert scans == [1]


def test_run_daemon_proceeds_when_the_root_is_free(tmp_path, monkeypatch):
    """A free root and a machine with room: the daemon runs.

    The two checks are a brake, not a wall — the daemon that passes
    them has to reach its scan loop.
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(reaper, "list_reaper_daemons", lambda **kwargs: [])

    stop = threading.Event()
    scans = []

    def fake_scan(_config):
        scans.append(1)
        stop.set()
        return reaper.ScanResult(verdicts=[], owners_alive=0)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=root, interval=1),
        stop=stop,
        clock=lambda: 0.0,
        scan_fn=fake_scan,
    )

    assert code == 0
    assert scans == [1]


# ---------------------------------------------------------------------------
# The event log has a ceiling
# ---------------------------------------------------------------------------
#
# The log is the reaper's record of what it decided.  It was also, for
# a while, the biggest file on the machine: every pass wrote one line
# per process it looked at, every thirty seconds, and the processes it
# looked at were every process on the box.  1.1 GB on 2026-11-02.
#
# The ceiling has two halves, and both are needed.  The skips are
# **counted** rather than listed -- the count is the information, and
# the thousand lines spelling it out are not.  And the file itself has
# a **size** it may not pass, whatever the machine does.


def _unmarked_verdict(pid: int) -> "reaper.Verdict":
    """Return a verdict about a process that never carried our marker.

    These are the bulk of a machine's verdicts: every process the
    reaper looks at and finds nothing of ours in.
    """
    proc = ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid,
        uid=os.getuid(),
        rss_kb=1024,
        argv="/bin/sh -c sleep 60",
        env={},
    )
    return reaper.Verdict(
        proc=proc,
        decision=reaper.DECISION_SKIP,
        reason=reaper.REASON_UNMARKED,
    )


def _marked_verdict(pid: int) -> "reaper.Verdict":
    """Return a verdict about a process that did carry our marker."""
    proc = ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid,
        uid=os.getuid(),
        rss_kb=1024,
        argv="/usr/bin/python3 -m backend.server",
        env={
            "PDT_SPAWN_OWNER_PID": "42",
            "PDT_SPAWN_OWNER_START": "17.5",
            "PDT_SPAWN_KIND": "service",
        },
    )
    return reaper.Verdict(
        proc=proc,
        decision=reaper.DECISION_SKIP,
        reason=reaper.REASON_OWNER_ALIVE,
        owner_pid=42,
        owner_start="17.5",
        kind="service",
    )


def _crowd(size: int) -> list:
    """Return *size* verdicts about processes that were never ours."""
    return [_unmarked_verdict(1000 + index) for index in range(size)]


def test_the_event_log_counts_the_skips_instead_of_listing_them(tmp_path):
    """A pass that skips a crowd writes one line, not one line per process.

    The count is the information.  A thousand lines spelling it out are
    not -- and they are what grew the log to 1.1 GB.
    """
    config = ReaperConfig(plans_root=tmp_path, dry_run=True)
    crowd = _crowd(200)

    for _ in range(20):
        reaper.run_once(
            config,
            scan_fn=lambda config: reaper.ScanResult(verdicts=crowd, owners_alive=0),
        )

    lines = reaper.events_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20, "the log must not grow with the size of the crowd"

    record = json.loads(lines[-1])
    assert record["skipped"] == 200
    assert record["reasons"]["unmarked"] == 200


def test_the_decisions_about_our_own_processes_are_still_spelled_out(tmp_path):
    """The count replaces the noise, not the record.

    A verdict about a process that carried our marker is a decision
    somebody may have to explain later, so it keeps a line of its own.
    """
    config = ReaperConfig(plans_root=tmp_path, dry_run=True)

    reaper.run_once(
        config,
        scan_fn=lambda config: reaper.ScanResult(
            verdicts=[_marked_verdict(4242)], owners_alive=0
        ),
    )

    record = json.loads(reaper.events_path(tmp_path).read_text(encoding="utf-8"))
    assert record["pid"] == 4242
    assert record["reason"] == reaper.REASON_OWNER_ALIVE


def test_the_event_log_has_a_size_ceiling(tmp_path, monkeypatch):
    """The log cannot grow without bound, whatever the machine does.

    The count is the first half of the ceiling; this is the second.
    The live log is rotated aside when the next write would carry it
    past the cap, and one generation is kept -- so the log on disk is
    bounded even when the verdicts are not.
    """
    monkeypatch.setattr(reaper, "MAX_EVENTS_BYTES", 4096)
    config = ReaperConfig(plans_root=tmp_path, dry_run=True)
    crowd = _crowd(200)

    for _ in range(50):
        reaper.run_once(
            config,
            scan_fn=lambda config: reaper.ScanResult(verdicts=crowd, owners_alive=0),
        )

    path = reaper.events_path(tmp_path)
    assert path.stat().st_size <= 4096

    rotated = path.with_name(path.name + ".1")
    assert rotated.exists(), "the log was never rotated"
    assert rotated.stat().st_size <= 4096


# ---------------------------------------------------------------------------
# A daemon that outlives its owner still has to have an end
# ---------------------------------------------------------------------------


class _FakePopen:
    """Records how the daemon was launched instead of launching it."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(pid=self.pid)

    @property
    def only(self):
        assert len(self.calls) == 1, "expected exactly one launch"
        return self.calls[0]


def test_a_detached_daemon_is_given_a_lease(tmp_path):
    """The exemption from the owner rule is not an exemption from every rule.

    A daemon that may outlive its owner is given a lease, so that a
    machine cannot accumulate them for ever -- which is exactly what
    twenty-four daemons on one machine did on 2026-11-02.
    """
    fake = _FakePopen()

    reaper.ensure_daemon(
        ReaperConfig(plans_root=tmp_path, max_lifetime=0),
        repo_root=tmp_path,
        popen=fake,
        detach_owner=True,
    )

    lease = int(fake.only[1]["env"][reaper.ENV_MAX_LIFETIME])
    assert 0 < lease <= reaper.DEFAULT_DETACHED_LEASE


# ---------------------------------------------------------------------------
# The daemons a session started are that session's to stop
# ---------------------------------------------------------------------------


class _Daemon:
    """A daemon as the census reports it."""

    def __init__(self, pid: int, plans_root: Path, survive_owner: bool = True):
        self.pid = pid
        self.ppid = 1
        self.plans_root = str(plans_root)
        self.started_at = "990.0"
        self.age_seconds = 1.0
        self.plans_root_exists = True
        self.survive_owner = survive_owner


def test_the_session_reclaims_the_daemons_it_started(monkeypatch):
    """The daemons this session started are this session's to stop.

    The rule used to be "a root pytest made", which missed the daemons
    the **backend** starts: they serve the session's own plan root --
    a temporary tree, but not one pytest made -- and they are started
    with ``--survive-owner``, so they outlive the server that started
    them.  Twenty-four of them were walking one machine on 2026-11-02.

    The tree is made with ``tempfile`` rather than with pytest's own
    ``tmp_path``: a pytest directory is pytest's, and this case is
    about the rule that is *not* about pytest.
    """
    state = Path(tempfile.mkdtemp(prefix="ac-test-state-"))
    try:
        session_root = state / "gw0"
        plans_root = session_root / "plans"
        plans_root.mkdir(parents=True)
        monkeypatch.setenv("PDT_TEST_SESSION_ROOT", str(session_root))

        stopped = []

        def _stop(root):
            stopped.append(Path(root))
            return [4242]

        reclaimed = reclaim_new_daemons(
            {},
            lister=lambda: [_Daemon(4242, plans_root)],
            stopper=_stop,
        )

        assert reclaimed == [4242]
        assert stopped == [plans_root.resolve()]
    finally:
        shutil.rmtree(state, ignore_errors=True)


def test_a_worker_leaves_another_workers_daemons_alone(monkeypatch):
    """Each worker reclaims its own daemons.

    The workers of one run share a machine and each has its own
    temporary tree.  A worker that stopped another worker's daemon would
    be stopping a service it never asked for -- the very mistake the
    narrow rule exists to prevent.

    The trees are made with ``tempfile`` rather than with pytest's own
    ``tmp_path``: a pytest directory is pytest's, and the rule that
    keeps this test honest is the one about *workers*.
    """
    state = Path(tempfile.mkdtemp(prefix="ac-test-state-"))
    try:
        mine = state / "gw0"
        theirs = state / "gw1"
        (mine / "plans").mkdir(parents=True)
        (theirs / "plans").mkdir(parents=True)
        monkeypatch.setenv("PDT_TEST_SESSION_ROOT", str(mine))

        stopped = []

        def _stop(root):
            stopped.append(Path(root))
            return [4242]

        reclaimed = reclaim_new_daemons(
            {},
            lister=lambda: [_Daemon(4242, theirs / "plans")],
            stopper=_stop,
        )

        assert reclaimed == []
        assert stopped == []
    finally:
        shutil.rmtree(state, ignore_errors=True)


def test_a_worker_leaves_another_workers_pytest_tree_alone(monkeypatch):
    """A pytest tree is per worker too, and the rule says so.

    Under xdist every worker gets its own directory under pytest's
    temporary root.  A daemon serving another worker's tree belongs to
    that worker, even though the tree is pytest's.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    theirs = "/tmp/pytest-of-user/pytest-9/gw1/test_case0/plans"

    stopped = []

    def _stop(root):
        stopped.append(Path(root))
        return [4242]

    reclaimed = reclaim_new_daemons(
        {},
        lister=lambda: [_Daemon(4242, Path(theirs))],
        stopper=_stop,
    )

    assert reclaimed == []
    assert stopped == []
