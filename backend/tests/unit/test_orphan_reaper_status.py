"""The daemon census: what the machine says about the reaper daemons.

The reaper's singleton guarantee is a lock on a file under the plan
root.  That lock answers one question — "is a daemon holding *this*
root?" — and it answers it honestly, because the kernel drops an
``flock`` when the holder dies.

It says nothing about the *machine*.  A machine can be running twenty
daemons on twenty roots, or five daemons on one root, and the lock
cannot tell the difference: deleting the lock file makes the next
holder take a different inode, so the "singleton" survives only as long
as the file does.  That is how a hundred daemons accumulate on one
box, each believing it is alone.

So the count has to come from somewhere else — from the process table
and the kernel's own start times.  That is what this file pins: which
processes count as a reaper daemon, what is read off them, and what
the answer looks like when the machine cannot be read at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import ProcInfo  # noqa: E402


def _proc(pid: int, argv: str, ppid: int = 1) -> ProcInfo:
    """Return a process snapshot as the process table would report it."""
    return ProcInfo(
        pid=pid,
        ppid=ppid,
        pgid=pid,
        uid=501,
        rss_kb=0,
        argv=argv,
    )


DAEMON_ARGV = (
    "python -m backend.orphan_reaper --daemon --interval 30 "
    "--plans-root /tmp/p/plans"
)


@pytest.fixture
def known_start(monkeypatch):
    """Make every process report the same known start time.

    The census reads the start time from the operating system, which
    for a pid that does not exist is nothing at all.  Pinning it here
    keeps the tests about the census rather than about ``ps``.

    The pinned token is **epoch seconds**, so the tick-based reading of
    ``/proc`` start times is switched off as well: on Linux the same
    token would otherwise be read as "ticks since boot", and the age
    the test asserts would depend on how long the machine had been up.
    """
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid: "990.0")
    monkeypatch.setattr(reaper, "_PROC_START_UNIT_IS_TICKS", False)


# ---------------------------------------------------------------------------
# Which processes count
# ---------------------------------------------------------------------------


def test_lists_only_daemon_processes(known_start):
    """Only the reaper's own daemons are listed.

    Everything else on the machine is noise for this question: the
    reaper in its one-shot mode, another program that happens to take
    ``--daemon``, and the ordinary processes of the world.  Listing
    them would make the count a count of the machine rather than a
    count of the reaper.
    """
    procs = [
        _proc(7, DAEMON_ARGV),
        _proc(8, "python -m backend.orphan_reaper --once --plans-root /tmp/p"),
        _proc(9, "python -m backend.some_other_service --daemon"),
        _proc(10, "/usr/bin/sleep 100"),
    ]

    daemons = reaper.list_reaper_daemons(procs=procs, now=1000.0)

    assert [daemon.pid for daemon in daemons] == [7]


def test_a_daemon_without_a_plans_root_is_still_listed(known_start):
    """A daemon with no ``--plans-root`` is a daemon, on the default root.

    The flag is optional on the command line, so a daemon started
    without it is running on the default root — which the command line
    does not name.  The root is unknown, not absent: the daemon still
    exists and still has to be counted.
    """
    procs = [_proc(7, "python -m backend.orphan_reaper --daemon")]

    daemons = reaper.list_reaper_daemons(procs=procs, now=1000.0)

    assert len(daemons) == 1
    assert daemons[0].plans_root is None
    assert daemons[0].plans_root_exists is False


def test_the_census_does_not_count_the_caller(known_start):
    """The process asking the question is not one of the answers.

    A reaper that looks at the machine and counts itself would report
    one daemon too many — and the caller is the one process whose
    command line is guaranteed to be about the reaper.
    """
    procs = [_proc(7, DAEMON_ARGV), _proc(11, DAEMON_ARGV)]

    daemons = reaper.list_reaper_daemons(procs=procs, now=1000.0, self_pid=11)

    assert [daemon.pid for daemon in daemons] == [7]


# ---------------------------------------------------------------------------
# What is read off a daemon
# ---------------------------------------------------------------------------


def test_parses_plans_root_both_forms(known_start):
    """``--plans-root V`` and ``--plans-root=V`` name the same root.

    Both spellings are what shells and humans produce; a census that
    understood only one of them would report half the daemons as
    rootless.
    """
    spaced = reaper.list_reaper_daemons(
        procs=[
            _proc(
                7,
                "python -m backend.orphan_reaper --daemon --plans-root /tmp/p/plans",
            )
        ],
        now=1000.0,
    )
    equals = reaper.list_reaper_daemons(
        procs=[
            _proc(
                7,
                "python -m backend.orphan_reaper --daemon --plans-root=/tmp/p/plans",
            )
        ],
        now=1000.0,
    )

    assert spaced[0].plans_root == "/tmp/p/plans"
    assert equals[0].plans_root == "/tmp/p/plans"
    assert spaced[0].plans_root == equals[0].plans_root


def test_reads_the_start_time_and_the_age(known_start):
    """A daemon is reported with when it started and how long it has run.

    The start time is what the kernel says, and the age is what the
    clock says since then — the two answers have to agree for the age
    to mean anything.
    """
    procs = [_proc(7, DAEMON_ARGV)]

    daemons = reaper.list_reaper_daemons(procs=procs, now=1000.0)

    assert daemons[0].started_at == "990.0"
    assert daemons[0].age_seconds == 10.0


def test_an_unknown_start_time_is_not_an_error(monkeypatch):
    """A start time the kernel will not give is reported as unknown.

    The platform may expose neither ``/proc`` nor a usable ``ps``; the
    census then knows a daemon is running but not since when.  That is
    a hole in the answer, not a failure of the census.
    """
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid: "")

    procs = [_proc(7, DAEMON_ARGV)]

    daemons = reaper.list_reaper_daemons(procs=procs, now=1000.0)

    assert len(daemons) == 1
    assert daemons[0].started_at is None
    assert daemons[0].age_seconds is None


def test_one_unreadable_pid_does_not_cost_the_whole_census(monkeypatch):
    """A pid that cannot be read is a gap in the answer, not an error.

    The census runs over every process on the machine; one of them
    refusing to be read must not cost the other seventy-one.
    """

    def _explode(pid):
        if pid == 7:
            raise OSError("cannot read this pid")
        return "990.0"

    monkeypatch.setattr(reaper, "process_start_epoch", _explode)

    procs = [_proc(7, DAEMON_ARGV), _proc(8, DAEMON_ARGV)]

    daemons = reaper.list_reaper_daemons(procs=procs, now=1000.0)

    assert [daemon.pid for daemon in daemons] == [7, 8]
    assert daemons[0].started_at is None
    assert daemons[1].started_at == "990.0"


# ---------------------------------------------------------------------------
# The status
# ---------------------------------------------------------------------------


def test_status_is_json_with_count(known_start):
    """The status is a count and the list it counts.

    The count is the number that answers "how many daemons does this
    machine run?" without the caller having to walk the list, and the
    list is what says which ones they are.
    """
    procs = [
        _proc(7, DAEMON_ARGV),
        _proc(8, "python -m backend.orphan_reaper --daemon --plans-root /tmp/q/plans"),
        _proc(9, "/usr/bin/sleep 100"),
    ]

    status = reaper.daemon_status(procs=procs, now=1000.0)

    assert status["count"] == 2
    assert len(status["daemons"]) == 2
    assert [daemon["pid"] for daemon in status["daemons"]] == [7, 8]
    assert json.loads(json.dumps(status)) == status


def test_status_reports_the_root_and_its_existence(known_start, tmp_path):
    """The root is reported together with whether it is still there.

    A daemon serving a root that has been deleted is the interesting
    case: it is running, it is keeping a lock nobody will look at, and
    it can never be found by the root it serves.
    """
    gone = tmp_path / "gone"
    procs = [
        _proc(7, DAEMON_ARGV.replace("/tmp/p/plans", str(tmp_path / "gone"))),
        _proc(8, DAEMON_ARGV.replace("/tmp/p/plans", str(tmp_path))),
    ]

    status = reaper.daemon_status(procs=procs, now=1000.0)

    by_pid = {daemon["pid"]: daemon for daemon in status["daemons"]}
    assert by_pid[7]["plans_root_exists"] is False
    assert by_pid[8]["plans_root_exists"] is True


def test_status_of_an_empty_machine_is_zero(monkeypatch):
    """A machine with no reaper daemon has a count of zero.

    Not an error, not a missing key: the count is the answer, and zero
    is an answer.
    """
    monkeypatch.setattr(reaper, "list_processes", lambda: [])
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid: "")

    status = reaper.daemon_status(now=1000.0)

    assert status == {"count": 0, "daemons": []}


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_status_flag_prints_json(monkeypatch, capsys):
    """``--status`` prints the census as JSON and exits.

    Machine-readable, because the question is asked by a script far
    more often than by a person — and the answer has to survive being
    piped into one.
    """
    monkeypatch.setattr(reaper, "list_processes", lambda: [_proc(7, DAEMON_ARGV)])
    monkeypatch.setattr(reaper, "process_start_epoch", lambda pid: "990.0")

    assert reaper.main(["--status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["daemons"][0]["pid"] == 7


def test_status_and_once_are_mutually_exclusive():
    """``--status`` is a mode of its own.

    Asking for the census and asking for a pass in the same breath is
    a contradiction, and the parser says so rather than picking one.
    """
    parser = reaper.build_arg_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--status", "--once"])