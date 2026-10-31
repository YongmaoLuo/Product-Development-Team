"""Positive controls for the process-ownership work: P1-P2.

The negative controls (``backend/tests/integration/test_process_ownership_negative_controls.py``)
prove that the reaper *can* kill a tree it owns.  These prove the other
half of the contract: that it does not kill one more than it owns.  A
reaper that signals too much is worse than one that signals too little,
because the processes it over-kills belong to somebody else — another
plan, the operator's own shell, the test session itself.

The two controls
----------------

P1  A plan that ran to completion leaves nothing behind.  The ledger is
    empty, a sweep over that plan reaps nothing, and the processes that
    are *not* its own — another plan's process, a process no ledger
    knows, the sweeping process's own process group — are not signalled.

P2  A pid the kernel has handed to somebody else is not ours.  The
    recorded start time no longer matches the kernel's, the reaper
    writes an ``identity_mismatch`` event, and the process keeps
    running.

Why real processes
------------------

Every assertion here is made against a process the kernel actually
has.  The reaper's entire safety argument rests on what the kernel
reports about a pid — its start time, its command line, its group — so
a control that ran against a mock would be testing the mock.  The
processes are therefore real children, forked by the helpers below,
carrying a ``pdt-fake-<uuid>`` marker in their command line so that a
stray one can be recognised, and reporting every signal they receive
into a file so that "was not signalled" is a fact rather than an
assumption.

Where the sweep runs
--------------------

The sweep that has to protect the sweeping process's own group runs in
a **process of its own** (see :data:`_REAPER_SOURCE`).  That is not
tidiness: a reaper that signalled its own group would take the whole
pytest session with it, and a test that can destroy the suite it runs
in is not a test.  In its own session the blast radius of that mistake
is one process and one child, and the test still fails — which is the
point.

Two rules hold the file together:

* **The ledger is the only list.**  Nothing here scans the machine.
  The processes that must survive are identified by the plan that owns
  them, and the assertions are made against those pids.
* **Everything started is handed back.**  Every test kills the
  processes it started, in a ``finally``, whether it passed or not.

Nothing here binds a port, starts a container, or reaches a network.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import process_identity
import process_ledger as ledger
import process_reaper


#: ``backend/`` — resolved from this file, because the checkout lives at
#: a different path on every machine.
BACKEND_DIR = Path(__file__).resolve().parents[1]

#: The liveness question the reaper asks.  Captured here so that a
#: test which deliberately fakes an identity does not fake the test's
#: own bookkeeping with it.
_ALIVE = process_reaper._alive

#: How long a child may take to announce itself before the test gives
#: up on it.
_READY_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# The fake children
# ---------------------------------------------------------------------------
#
# The children are written as files rather than piped into the
# interpreter so that their command lines are real: the identity check
# compares command lines, and a ``python -c`` one-liner would not look
# like the processes the reaper has to recognise in production.

_CHILD_SOURCE = r'''
"""A child process that reports the signals it receives.

Run as::

    python fake_child.py <marker> <seconds> <signal-file> <ready-file>

The marker is in the command line so that a stray child can be
recognised in ``ps`` output.  The signal handlers write the number of
the signal into the signal file and exit, so the test can tell "the
process was signalled" from "the process is still running" — a process
that ignored the signal would otherwise make the two look the same.

The ready file says who the child is — its pid and its parent — so the
test can tell a real fork of this process from anything else.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path


def _on_signal(signum, frame):
    Path(sys.argv[3]).write_text(str(signum), encoding="utf-8")
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    Path(sys.argv[4]).write_text(
        json.dumps({
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "pgid": os.getpgrp(),
            "marker": sys.argv[1],
        }),
        encoding="utf-8",
    )
    time.sleep(float(sys.argv[2]))


if __name__ == "__main__":
    main()
'''


#: A leader that keeps a child inside its own process group.
#:
#: The point of this fixture is the *group*: the child is started
#: without a new session, so it stays in the leader's group and a
#: signal addressed to the group reaches both of them.  That is the
#: shape in which a group signal is not safe — the check vouches for
#: a pid, and a group signal reaches every member of the group.
_GROUP_LEADER_SOURCE = r'''
"""A leader that keeps its child inside its own process group.

Run as::

    python group_leader.py <child-script> <marker> <seconds> <ready-file>

The child is started *without* a new session, so it stays in the
leader's process group: a signal addressed to the group reaches both
of them.  The leader announces the child's pid, so the test can put a
record for it into the ledger.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    child_script, marker, seconds, ready_file = sys.argv[1:5]
    ready = Path(ready_file)
    child = subprocess.Popen(
        [
            sys.executable, child_script, marker, seconds,
            str(ready.with_suffix(".child.sig")),
            str(ready.with_suffix(".child.ready")),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(ready.parent),
    )
    ready.write_text(
        json.dumps({
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "pgid": os.getpgrp(),
            "child_pid": child.pid,
        }),
        encoding="utf-8",
    )
    time.sleep(float(seconds))


if __name__ == "__main__":
    main()
'''


#: The reaper runs in a process of its own, so that a sweep which
#: signalled its own group could not take the test session with it.
_REAPER_SOURCE = r'''
"""A reaper that runs in a process of its own.

Run as::

    python fake_reaper.py <config.json>

The reaper is the process that does the sweeping, so the sweeping is
done *here* rather than in the test: a reaper that signalled its own
process group would take the pytest session with it if the sweep ran
in-process.  In a session of its own the blast radius of that mistake
is one process and one child — and the test still fails, which is the
point.

The reaper records two processes in the plan's ledger and then sweeps
the plan:

  * **itself** — the process doing the sweeping.  A sweep must never
    signal the caller: that is how a shutdown turns into an outage.
  * **a child in its own process group** — the group case.  The child
    is alive, its identity matches the record, and signalling it
    would signal the reaper with it.

Both must be skipped, and the result — what was skipped and why, and
whether either process is still running — is written into the result
file for the test to read.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _settled_identity(process_identity, pid, tries=60, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    The launcher rewrites ``argv[0]`` shortly after the exec, so a
    reading taken the instant a process is spawned can spell the
    command line differently from a reading taken a moment later.  The
    identity check compares command lines, so the *recorded* one has
    to be the settled one.
    """
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def main():
    config_path = sys.argv[1]
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    backend_dir = str(config["backend_dir"])
    sys.path.insert(0, backend_dir)
    sys.path.insert(0, str(Path(backend_dir).parent))

    import process_identity
    import process_ledger as ledger
    import process_reaper

    plan_dir = Path(config["plan_dir"])
    plan_id = config["plan_id"]
    work_dir = Path(config["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)

    # The group-mate: a child of THIS process that stays in THIS
    # process's group.  It is the process the reaper must refuse to
    # signal, because signalling it would signal the reaper.
    sig_file = work_dir / "groupmate.sig"
    ready_file = work_dir / "groupmate.ready"
    child = subprocess.Popen(
        [
            sys.executable, str(config["child_script"]),
            config["marker"], "300", str(sig_file), str(ready_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(work_dir),
    )
    deadline = time.monotonic() + 30.0
    while not ready_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    identity = _settled_identity(process_identity, child.pid)
    if identity is None:
        sys.stderr.write("the group-mate has no readable identity\n")
        sys.exit(3)

    # The group-mate, recorded the way the backend records a child.
    ledger.record_spawn(
        plan_dir,
        ledger.ProcessRecord(
            plan_id=plan_id,
            role="subagent",
            pid=child.pid,
            pgid=identity.pgid or child.pid,
            create_time=identity.create_time or 0.0,
            cmdline=list(identity.cmdline),
            cwd=identity.cwd or str(work_dir),
            owner_pid=os.getpid(),
            owner_role="backend",
        ),
    )

    # The reaper itself, recorded the way the backend records the
    # executor it is running as.
    own = _settled_identity(process_identity, os.getpid())
    ledger.record_spawn(
        plan_dir,
        ledger.ProcessRecord(
            plan_id=plan_id,
            role="executor",
            pid=os.getpid(),
            pgid=os.getpgrp(),
            create_time=(own.create_time or 0.0) if own else 0.0,
            cmdline=list(own.cmdline) if own else [],
            cwd=os.getcwd(),
            owner_pid=os.getpid(),
            owner_role="backend",
        ),
    )

    outcome = process_reaper.reap_plan(
        plan_dir, plan_id, reason="p1_positive_control",
    )

    result = {
        "outcome": outcome.to_dict(),
        "child_pid": child.pid,
        "child_pgid": identity.pgid,
        "own_pid": os.getpid(),
        "own_pgid": os.getpgrp(),
        "child_alive": process_reaper._alive(child.pid),
        "signal_seen": sig_file.exists(),
    }
    Path(config["result"]).write_text(
        json.dumps(result), encoding="utf-8",
    )

    # The group-mate is deliberately left running: the test checks that
    # it survived the sweep, and a process that dies with its parent
    # could not be checked at all.  The test kills it.


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _marker() -> str:
    """A marker every fake process carries in its command line."""
    return "pdt-fake-" + uuid.uuid4().hex[:12]


def _plan_id(prefix: str) -> str:
    """A plan id of this test's own."""
    return "20261030-pc-{}-{}".format(prefix, uuid.uuid4().hex[:8])


def _settled_identity(pid: int, tries: int = 60, delay: float = 0.05):
    """Probe ``pid`` until two consecutive readings agree.

    The launcher rewrites ``argv[0]`` shortly after the exec, so a
    reading taken the instant a process is spawned can spell the
    command line differently from a reading taken a moment later.  The
    identity check compares command lines, so the *recorded* one has
    to be the settled one.
    """
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _write_child_script(work_dir: Path) -> Path:
    """Write the fake child's script into the work directory."""
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "fake_child.py"
    script.write_text(_CHILD_SOURCE, encoding="utf-8")
    return script


def _spawn_child(
    work_dir: Path,
    marker: str,
    *,
    seconds: float = 300.0,
    new_session: bool = True,
    name: str = "child",
) -> Tuple[subprocess.Popen, Path, Path]:
    """Start a fake child.

    Returns the process, the file the child writes into when it is
    signalled, and the file it writes when it is ready.  The child is
    waited for until it is ready, so the caller may probe its identity
    straight away.
    """
    script = _write_child_script(work_dir)
    sig_file = work_dir / ("%s.sig" % name)
    ready_file = work_dir / ("%s.ready" % name)
    proc = subprocess.Popen(
        [
            sys.executable, str(script), marker, str(seconds),
            str(sig_file), str(ready_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
        cwd=str(work_dir),
    )
    _wait_for_ready(ready_file, proc)
    return proc, sig_file, ready_file


def _spawn_group_leader(
    work_dir: Path,
    marker: str,
    *,
    seconds: float = 300.0,
) -> Tuple[subprocess.Popen, Dict[str, Any], int, Path]:
    """Start a leader that keeps a child inside its own process group.

    Returns the leader, what it announced, the pid of the child it
    started and the file the child writes into when it is signalled.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    child_script = work_dir / "fake_child.py"
    child_script.write_text(_CHILD_SOURCE, encoding="utf-8")
    leader_script = work_dir / "group_leader.py"
    leader_script.write_text(_GROUP_LEADER_SOURCE, encoding="utf-8")
    ready_file = work_dir / "leader.ready"
    proc = subprocess.Popen(
        [
            sys.executable, str(leader_script), str(child_script), marker,
            str(seconds), str(ready_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(work_dir),
    )
    ready = _wait_for_ready(ready_file, proc)
    _wait_for_ready(work_dir / "leader.child.ready", proc)
    return proc, ready, int(ready["child_pid"]), work_dir / "leader.child.sig"


def _wait_for_ready(ready_file: Path, proc: subprocess.Popen) -> Dict[str, Any]:
    """Wait for a child to announce itself; return what it said."""
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if ready_file.exists():
            try:
                return json.loads(ready_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if proc.poll() is not None:
            raise AssertionError(
                "the child exited before it was ready (code {})".format(
                    proc.returncode
                )
            )
        time.sleep(0.05)
    raise AssertionError("{} never appeared".format(ready_file))


def _record_spawn(
    plan_dir: Path,
    proc: subprocess.Popen,
    *,
    role: str,
    plan_id: str,
    create_time: Optional[float] = None,
) -> ledger.ProcessRecord:
    """Write a spawn record for ``proc`` into the plan's ledger.

    This is what the backend does for the processes it starts, and
    what the test does for the ones it starts itself.  ``create_time``
    overrides the kernel's answer — the way to fake a recycled pid.
    """
    identity = _settled_identity(proc.pid)
    assert identity is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=identity.pgid or proc.pid,
        create_time=(
            identity.create_time or 0.0
            if create_time is None else float(create_time)
        ),
        cmdline=list(identity.cmdline),
        cwd=identity.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    """Kill a child and collect it."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001 — cleanup must not fail the test
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _kill_pid(pid: int) -> None:
    """Kill a pid that is not a child of this process."""
    if pid <= 1:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _ready_pid(ready_file: Path) -> int:
    """The pid a child announced, or ``0`` when it never announced."""
    try:
        payload = json.loads(ready_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    try:
        return int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _event_names(plan_dir: Path, pid: Optional[int] = None) -> List[str]:
    """The names of the events the ledger holds, oldest first."""
    names = []
    for event in ledger.read_events(plan_dir):
        if pid is not None and event.get("pid") != pid:
            continue
        names.append(str(event.get("event")))
    return names


def spawn_and_finish_plan(
    plan_dir: Path,
    *,
    plan_id: str = "",
    count: int = 3,
    work_dir: Optional[Path] = None,
) -> List[ledger.ProcessRecord]:
    """Spawn ``count`` processes, record them, and let them finish.

    The three processes are the shape of a plan that ran to completion:
    a sub-agent, a tool and a VP service.  Each one is recorded in the
    ledger the moment it is spawned and its exit is recorded when it
    ends — which is what a healthy run leaves behind.

    Returns the records, so a test can look at what was written.
    """
    plan_dir = Path(plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_id = plan_id or plan_dir.name
    work_dir = Path(work_dir) if work_dir else plan_dir / "work"
    roles = ("subagent", "tool", "vp_service")

    marker = _marker()
    children: List[Tuple[subprocess.Popen, Path]] = []
    records: List[ledger.ProcessRecord] = []
    try:
        for index in range(count):
            proc, sig_file, _ = _spawn_child(
                work_dir, marker, seconds=1.0, name="finished-%d" % index,
            )
            children.append((proc, sig_file))
            records.append(
                _record_spawn(
                    plan_dir, proc,
                    role=roles[index % len(roles)], plan_id=plan_id,
                )
            )
        for proc, _ in children:
            assert proc.wait(timeout=30) == 0, (
                "a child of a finished plan exited with %r" % proc.returncode
            )
            ledger.record_exit(
                plan_dir, proc.pid, ledger.EVENT_EXIT,
                exit_code=proc.returncode,
            )
    finally:
        for proc, _ in children:
            _terminate(proc)
    return records


# ---------------------------------------------------------------------------
# P1 — a plan that ran to completion owns nothing that is still running
# ---------------------------------------------------------------------------


def test_p1_finished_plan_has_no_live_processes(tmp_path):
    """A plan whose processes all ended leaves an empty ledger.

    The ledger is the memory of what a plan owns.  A plan that ran to
    completion owns nothing, so the ledger has to say so: three
    processes started, three processes ended, and what is left is an
    empty live set.  A sweep over that plan then has nothing to reap
    and — this is the part that matters — signals nothing at all.
    """
    plan_id = _plan_id("p1-finished")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()

    records = spawn_and_finish_plan(plan_dir, plan_id=plan_id)

    assert len(records) == 3, "the plan was supposed to run three processes"
    assert [record.pid for record in records] == sorted(
        {record.pid for record in records}
    ), "the plan started the same process twice"

    # Every one of them really ran and really ended.
    for record in records:
        assert not _ALIVE(record.pid), (
            "a process of a finished plan is still running"
        )

    # The live set of a finished plan is empty — the example in the
    # brief: ``live_records(plan_dir)`` is ``{}``.
    assert ledger.live_records(plan_dir) == {}

    # And a sweep over it has nothing to do: nothing is reaped, and
    # nothing is signalled.
    outcome = process_reaper.reap_plan(
        plan_dir, plan_id, reason="p1_finished_plan",
    )
    assert outcome.reaped == []
    assert outcome.skipped == []
    assert outcome.failed == []
    assert outcome.unknown == []
    assert "killed" not in _event_names(plan_dir)


def test_p1_other_plan_and_protected_processes_are_untouched(tmp_path):
    """A sweep of one plan reaches nothing but that plan's processes.

    Three things must survive a sweep untouched, and each of them
    breaks in a different way if the reaper is wrong:

    * **another plan's process** — known to the system, owned by
      somebody else.  The ledger of the plan being swept does not
      mention it, so the sweep must not signal it.
    * **a process no ledger knows** — a daemon the operator started.
      Nothing says it is ours, so nothing may be done about it.
    * **the sweeping process's own group** — signalling it would
      signal the sweeper, which is how a shutdown turns into an
      outage.

    The sweep runs in a process of its own (see the module docstring),
    so a mistake here cannot take the test session with it.
    """
    plan_id = _plan_id("p1-untouched")
    other_plan_id = _plan_id("p1-other")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    other_plan_dir = plans_dir / other_plan_id
    plan_dir.mkdir()
    other_plan_dir.mkdir()

    work_dir = tmp_path / "work"
    marker = _marker()

    # --- the processes that have to survive -------------------------
    # Another plan's process: recorded in that plan's ledger, alive.
    other_proc, other_sig, _ = _spawn_child(
        work_dir, marker, seconds=300.0, name="other-plan",
    )
    _record_spawn(
        other_plan_dir, other_proc, role="vp_service", plan_id=other_plan_id,
    )

    # A daemon no ledger knows about.
    daemon, daemon_sig, _ = _spawn_child(
        work_dir, marker, seconds=300.0, name="daemon",
    )

    # --- a process of the plan being swept, already gone ------------
    # A process that ended without recording its exit: the ledger still
    # lists it, and the sweep has to retire it without signalling it.
    stale, stale_sig, _ = _spawn_child(
        work_dir, marker, seconds=0.2, name="stale",
    )
    _record_spawn(plan_dir, stale, role="tool", plan_id=plan_id)
    stale.wait(timeout=30)

    reaper = None
    try:
        assert _ALIVE(other_proc.pid)
        assert _ALIVE(daemon.pid)

        # --- the sweep, in a process of its own ----------------------
        reaper_dir = tmp_path / "reaper"
        reaper_dir.mkdir()
        reaper_script = reaper_dir / "fake_reaper.py"
        reaper_script.write_text(_REAPER_SOURCE, encoding="utf-8")
        result_file = reaper_dir / "result.json"
        config = {
            "backend_dir": str(BACKEND_DIR),
            "plan_dir": str(plan_dir),
            "plan_id": plan_id,
            "work_dir": str(reaper_dir),
            "child_script": str(_write_child_script(reaper_dir)),
            "marker": marker,
            "result": str(result_file),
        }
        config_file = reaper_dir / "config.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")

        reaper = subprocess.Popen(
            [sys.executable, str(reaper_script), str(config_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(reaper_dir),
        )
        assert reaper.wait(timeout=120) == 0, (
            "the reaper did not finish cleanly — it was signalled"
        )
        result = json.loads(result_file.read_text(encoding="utf-8"))

        # --- the sweep signalled nothing ----------------------------
        outcome = result["outcome"]
        assert outcome["reaped"] == [], (
            "the sweep reaped something: %r" % (outcome["reaped"],)
        )
        assert outcome["failed"] == []
        assert outcome["unknown"] == []
        # A record the sweep *collected* — one whose process had already
        # ended — leaves a named event of its own, and that event says
        # that nothing was signalled.  So the name alone does not say
        # that a signal went out; the detail does.
        for event in ledger.read_events(plan_dir):
            if event.get("event") == "killed":
                assert "nothing was signalled" in (event.get("detail") or ""), (
                    "the sweep signalled something: %r" % (event,)
                )

        # The two processes of the swept plan were both skipped, each
        # for the reason that applies to it.
        skipped = {
            int(entry["pid"]): entry["reason"]
            for entry in outcome["skipped"]
        }
        assert skipped.get(int(result["own_pid"])) == "own_pid", (
            "the reaper did not refuse to signal itself: %r" % (outcome,)
        )
        assert skipped.get(int(result["child_pid"])) == "own_process_group", (
            "the reaper did not refuse to signal its own process group: %r"
            % (outcome,)
        )

        # The group-mate is in the reaper's group and it survived.
        assert result["child_alive"] is True
        assert result["signal_seen"] is False
        assert not (reaper_dir / "groupmate.sig").exists()

        # --- the processes that are not ours -------------------------
        assert _ALIVE(other_proc.pid), (
            "the sweep killed another plan's process"
        )
        assert not other_sig.exists(), (
            "another plan's process was signalled"
        )
        assert other_proc.poll() is None
        assert other_proc.pid in ledger.live_records(other_plan_dir), (
            "another plan's process left its own plan's live set"
        )

        assert _ALIVE(daemon.pid), (
            "the sweep killed a process no ledger knows"
        )
        assert not daemon_sig.exists(), (
            "a process no ledger knows was signalled"
        )
        assert daemon.poll() is None
    finally:
        _terminate(reaper)
        _terminate(other_proc)
        _terminate(daemon)
        _terminate(stale)
        _kill_pid(other_proc.pid)
        _kill_pid(daemon.pid)
        _kill_pid(stale.pid)
        # The group-mate belongs to the reaper, not to this test, so it
        # is not a child this test can wait for.  Its pid is known from
        # the file it wrote when it was ready — and it has to be killed
        # here, because a sweep that went wrong takes the reaper down
        # before it can clean up after itself.
        _kill_pid(_ready_pid(tmp_path / "reaper" / "groupmate.ready"))


# ---------------------------------------------------------------------------
# P2 — a recycled pid is not ours
# ---------------------------------------------------------------------------


def test_p2_reused_pid_is_not_killed_and_records_identity_mismatch(tmp_path):
    """A pid whose start time no longer matches is left alone.

    Pids are recycled.  The ledger says "pid 4242 is ours", but the
    kernel says 4242 started an hour ago, and the process the ledger
    recorded started an hour before that.  The two cannot be the same
    process, so the reaper has to write down that it found a mismatch
    and send nothing at all: a stranger is worse than an orphan.

    The forged start time is the only thing wrong with the record — the
    command line is the real one — so the mismatch is attributable to
    the start time alone.
    """
    plan_id = _plan_id("p2-reused")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()
    work_dir = tmp_path / "work"

    marker = _marker()
    proc, sig_file, _ = _spawn_child(
        work_dir, marker, seconds=300.0, name="recycled",
    )
    try:
        identity = _settled_identity(proc.pid)
        assert identity is not None
        assert identity.create_time, "the kernel reported no start time"

        # The record says the process was born an hour later than the
        # kernel says: this is what a recycled pid looks like.
        forged = (identity.create_time or 0.0) + 3600.0
        record = _record_spawn(
            plan_dir, proc, role="vp_service", plan_id=plan_id,
            create_time=forged,
        )
        assert record.create_time == forged

        outcome = process_reaper.reap_plan(
            plan_dir, plan_id, reason="p2_reused_pid",
        )

        # Nothing was reaped, and the process was skipped for the one
        # reason that fits.
        assert outcome.reaped == []
        assert outcome.skipped == [{
            "pid": proc.pid,
            "role": "vp_service",
            "reason": "identity_mismatch",
        }], outcome.skipped

        # The process is untouched: still running, never signalled.
        assert _ALIVE(proc.pid), (
            "the reaper killed a process it could not identify"
        )
        assert proc.poll() is None
        assert not sig_file.exists(), (
            "the process was signalled despite the mismatch"
        )

        # And the ledger says what happened, by name.
        events = [
            event for event in ledger.read_events(plan_dir)
            if event.get("event") == process_reaper.EVENT_IDENTITY_MISMATCH
        ]
        assert events, "no identity_mismatch event was written"
        assert events[-1].get("pid") == proc.pid
        assert events[-1].get("reason") == "identity_mismatch"

        # A mismatch is not an ending: the record stays in the live
        # set, because the process is still running.
        assert proc.pid in ledger.live_records(plan_dir)
        assert "killed" not in _event_names(plan_dir)
    finally:
        _terminate(proc)
        _kill_pid(proc.pid)


def _plan_with_a_recycled_group_member(
    tmp_path: Path,
    plan_id: str,
) -> Dict[str, Any]:
    """A plan whose process group holds a pid the ledger does not vouch for.

    Two real processes share one process group:

    * the **leader** — recorded the way the backend records a process,
      with the kernel's own start time and command line, so the ledger
      vouches for it;
    * a **child** inside the leader's group — recorded with a start
      time 999 seconds later than the kernel's, which is what a
      recycled pid looks like: the number is ours, the process is
      somebody else's.

    The child is in the leader's group on purpose: a signal addressed
    to the group reaches every member of it, so this is the shape in
    which "the identity check refused to signal that pid" is not
    enough — the group signal would take it anyway.

    Returns what the two tests need: the plan's directory, the leader
    and its pid, the child's pid and the file the child writes into
    when it is signalled.  The caller kills both when it is done.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(exist_ok=True)
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(exist_ok=True)
    work_dir = tmp_path / "work"

    marker = _marker()
    leader, ready, child_pid, child_sig = _spawn_group_leader(
        work_dir, marker,
    )
    leader_pid = int(ready["pid"])
    pgid = int(ready["pgid"])
    assert pgid == leader_pid, (
        "the leader does not lead the group it was put in"
    )
    assert os.getpgid(child_pid) == pgid, (
        "the child is not in the leader's group — the control would "
        "prove nothing"
    )

    # The leader is ours: the record matches what the kernel says.
    leader_record = _record_spawn(
        plan_dir, leader, role="executor", plan_id=plan_id,
    )
    assert leader_record.pgid == pgid

    # The child is a pid the ledger recorded 999 seconds before the
    # process actually started.  The command line is the real one, so
    # the mismatch is the start time alone.
    child_identity = _settled_identity(child_pid)
    assert child_identity is not None, "the child has no readable identity"
    forged = (child_identity.create_time or 0.0) + 999.0
    ledger.record_spawn(
        plan_dir,
        ledger.ProcessRecord(
            plan_id=plan_id,
            role="subagent",
            pid=child_pid,
            pgid=pgid,
            create_time=forged,
            cmdline=list(child_identity.cmdline),
            cwd=child_identity.cwd or "",
            owner_pid=os.getpid(),
            owner_role="backend",
        ),
    )

    return {
        "plan_dir": plan_dir,
        "leader": leader,
        "leader_pid": leader_pid,
        "child_pid": child_pid,
        "child_sig": child_sig,
    }


def test_identity_mismatched_group_member_is_not_killed_by_the_group_signal(
    tmp_path,
):
    """A pid the ledger does not vouch for gets no signal at all.

    The identity check vouches for a *pid*, but a group signal is
    addressed to a *group* — and it reaches every member of it.  So a
    plan whose group also holds a recycled pid is a plan whose group
    signal would kill a stranger, even though the check refused to
    signal that stranger on its own.  "Not known to be ours" is not a
    licence to kill, and that has to hold for the signal that arrives
    by the back door of the process group.
    """
    plan_id = _plan_id("vp010-mismatch")
    fixture = _plan_with_a_recycled_group_member(tmp_path, plan_id)
    plan_dir = fixture["plan_dir"]
    child_pid = fixture["child_pid"]
    try:
        process_reaper.reap_plan(
            plan_dir, plan_id, reason="vp010_group_member",
        )

        # The mismatched pid is still running: it was in the group the
        # signal was addressed to, and a group signal reaches every
        # member of the group.
        assert _ALIVE(child_pid), (
            "the sweep killed a pid it does not vouch for — the group "
            "signal reached a member the identity check refused"
        )
        assert not fixture["child_sig"].exists(), (
            "the pid that is not vouch'd for was signalled"
        )

        # And the ledger says what happened, by name.
        mismatches = [
            event for event in ledger.read_events(plan_dir)
            if event.get("event") == process_reaper.EVENT_IDENTITY_MISMATCH
            and event.get("pid") == child_pid
        ]
        assert mismatches, (
            "no identity_mismatch event was written for the recycled pid"
        )

        # A mismatch is not an ending: the child is still running, so
        # its record stays in the live set.
        assert child_pid in ledger.live_records(plan_dir)
    finally:
        _terminate(fixture["leader"])
        _kill_pid(child_pid)


def test_vouched_group_member_is_still_reaped(tmp_path):
    """The pid the ledger *does* vouch for is still collected.

    The fix must not turn the tree's collection off along the way.  In
    the same group, with the same recycled pid sitting in it, the
    process the ledger vouches for is still reaped — and the ledger
    says it was, by name.
    """
    plan_id = _plan_id("vp010-vouched")
    fixture = _plan_with_a_recycled_group_member(tmp_path, plan_id)
    plan_dir = fixture["plan_dir"]
    leader_pid = fixture["leader_pid"]
    try:
        outcome = process_reaper.reap_plan(
            plan_dir, plan_id, reason="vp010_vouched_member",
        )

        assert leader_pid in outcome.reaped, (
            "the sweep did not reap the process it vouched for: %r"
            % (outcome,)
        )
        assert not _ALIVE(leader_pid), (
            "the process the ledger vouches for is still running"
        )

        killed = [
            event for event in ledger.read_events(plan_dir)
            if event.get("event") == process_reaper.EVENT_KILLED
            and event.get("pid") == leader_pid
        ]
        assert killed, (
            "the ledger has no killed event for the process it reaped"
        )
    finally:
        _terminate(fixture["leader"])
        _kill_pid(fixture["child_pid"])


# ---------------------------------------------------------------------------
# the controls themselves
# ---------------------------------------------------------------------------

def test_positive_controls_run_against_real_forked_processes(tmp_path):
    """The controls above are about real processes, and this says so.

    A control that ran against a mock would prove nothing about the
    kernel, and the kernel is what the reaper's safety argument rests
    on.  So this pins the three facts the other controls depend on:

    * the helper forks a **real** process — the kernel has one, it is
      a child of this one, and it carries the marker in its command
      line;
    * the identity the ledger records is the **kernel's**, not a value
      the test made up;
    * the identity check is a **real** check — the same process with a
      different start time does not match.
    """
    plan_id = _plan_id("meta")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()
    work_dir = tmp_path / "work"

    marker = _marker()
    proc, sig_file, ready_file = _spawn_child(
        work_dir, marker, seconds=300.0, name="meta",
    )
    try:
        # A real process: the kernel knows it, this process is its
        # parent, and it is marked.
        assert isinstance(proc, subprocess.Popen)
        ready = json.loads(ready_file.read_text(encoding="utf-8"))
        assert ready["pid"] == proc.pid
        assert ready["ppid"] == os.getpid(), (
            "the child is not a fork of this process"
        )
        assert ready["marker"] == marker

        identity = process_identity.probe(proc.pid)
        assert identity is not None, "the kernel has no identity for the child"
        assert marker in " ".join(identity.cmdline), (
            "the control process does not carry the marker — the control "
            "would prove nothing"
        )
        assert identity.create_time, "the kernel reported no start time"
        assert identity.pgid, "the kernel reported no process group"

        # The identity the ledger records is the kernel's.
        record = _record_spawn(
            plan_dir, proc, role="tool", plan_id=plan_id,
        )
        stored = ledger.live_records(plan_dir)[proc.pid]
        assert stored.cmdline == tuple(record.cmdline)
        assert stored.create_time == pytest.approx(identity.create_time, abs=2.0)
        assert stored.pgid == (identity.pgid or proc.pid)

        # The check itself is real: the same process with a different
        # start time does not match, and with the right one it does.
        assert process_identity.matches(
            identity,
            create_time=identity.create_time,
            cmdline=identity.cmdline,
        )
        assert not process_identity.matches(
            identity,
            create_time=identity.create_time + 3600.0,
            cmdline=identity.cmdline,
        )

        # And the control's own child is alive and unsignalled, so the
        # assertions above were made about a running process.
        assert _ALIVE(proc.pid)
        assert not sig_file.exists()
    finally:
        _terminate(proc)
        _kill_pid(proc.pid)