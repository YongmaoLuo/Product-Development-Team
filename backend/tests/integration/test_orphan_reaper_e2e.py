"""The orphan reaper, end to end, on real processes.

The unit suites drive the ladder with a fake process table: every
probe — what is running, what a process carries, who owns it, what it
listens on — is injected.  That is what makes the ladder testable, and
it is also what makes it *unproven* against a real machine.  A fake
table cannot say whether the signal reached the right process group,
whether the grace period really lets a process die, or whether a
process whose owner is still running is left alone.

This file is the other half.  It spawns real processes, marks them
with the real markers, and lets the real reaper decide.

There are three tests: one positive control and two negative ones.

  * **The positive control** — the owner dies, the child becomes an
    orphan, and the reaper reclaims it.
  * **The negative control that matters most** — the owner is alive,
    and the child must survive.  A reaper that reaps a process whose
    owner is still running is not a reaper; it is an accident with a
    log file.
  * **The dry run** — the same scene with the signals turned off: the
    decision is made, and nothing dies.

Two properties of the fixtures are load-bearing and easy to get wrong.

  * The fixtures are launched with ``sys.executable`` — the
    interpreter of the venv that runs the suite — and not with
    ``/bin/sleep``.  On macOS a process' environment is read through
    ``ps -E``, and ``ps -E`` shows nothing for an Apple platform
    binary: ``/bin/sleep`` is protected by SIP.  The child would then
    be judged ``env_unreadable``, skipped, and every assertion below
    would pass for the wrong reason.  Measured on the machine this was
    written on: a marked ``/bin/sleep`` is matched zero times by
    ``ps -E``, a marked venv python once.
  * Every fixture is launched with an explicit environment — the marker
    variables plus ``PATH`` — and inherits nothing from the test
    process.  An ambient ``PDT_SPAWN_*`` would mark the *owner* too,
    and the owner has to be unmarked: it is the difference between
    "the child has a live owner" and "nothing here is marked at all".

Nothing here scans the machine: ``pid_scope`` narrows the reaper to
the two pids this test spawned, and every fixture process is killed
and reaped in the fixture's teardown.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    DECISION_KILL,
    DECISION_SKIP,
    KIND_SERVICE,
    REASON_ORPHAN,
    REASON_OWNER_ALIVE,
    SIGNAL_KILL,
    SIGNAL_NONE,
    SIGNAL_TERM,
    ReaperConfig,
    ScanResult,
    Verdict,
)

pytestmark = pytest.mark.integration


#: The body of every fixture process.  It sleeps long enough that only
#: a signal can end it, and short enough that a leaked fixture dies on
#: its own rather than outliving the suite.
_SLEEP_BODY = "import time; time.sleep(60)"

#: How long the reaper waits between ``SIGTERM`` and ``SIGKILL``.
GRACE = 1.0

#: How long a test waits for a process to disappear before it gives up.
DEADLINE = 5.0

#: How often that wait asks whether the process is gone.
POLL_STEP = 0.05


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


def _base_env() -> Dict[str, str]:
    """The environment a fixture process is launched with.

    Only ``PATH``.  The marker variables are added on top of it for the
    child; the owner must carry none of them, so that it is judged
    *unmarked* rather than *marked with a dead owner*.
    """
    return {"PATH": os.environ.get("PATH", "")}


def _spawn(env: Dict[str, str]) -> subprocess.Popen:
    """Start one fixture process.

    ``sys.executable`` and not ``/bin/sleep``: the reaper reads a
    candidate's environment through the operating system, and on macOS
    that reading is blind to Apple's own binaries.  ``start_new_session``
    gives the process its own group, which is the shape a spawned
    service has — and it is what lets the reaper signal the *group*
    rather than the process alone.
    """
    return subprocess.Popen(
        [sys.executable, "-c", _SLEEP_BODY],
        env=env,
        start_new_session=True,
    )


def _child_env(owner: subprocess.Popen) -> Dict[str, str]:
    """The environment of the child: the marker's owner's identity.

    The owner's start time is part of the mark on purpose.  A pid alone
    cannot tell a live owner from a recycled one, and the whole point
    of the marker is that it can.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(
        reaper.spawn_tags(
            KIND_SERVICE,
            owner_pid=owner.pid,
            owner_start=reaper.process_start_epoch(owner.pid),
        )
    )
    return env


def _terminate(proc: subprocess.Popen) -> None:
    """Kill a fixture process and reap it.

    The group is killed as well as the process, but only when the
    process still leads its own group: ``killpg`` on a group the caller
    does not lead takes down processes nobody judged — under pytest,
    the whole session.
    """
    try:
        if proc.poll() is None:
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=DEADLINE)
    except subprocess.TimeoutExpired:  # pragma: no cover - the kill above
        pass


@pytest.fixture
def owner_and_child():
    """A live owner and the marked child it spawned.

    Both are killed and reaped on the way out, whatever the test did
    with them: a fixture process that outlives its test is a defect,
    and on a CI runner it keeps the step's output pipe open.
    """
    owner = _spawn(_base_env())
    child: Optional[subprocess.Popen] = None
    try:
        child = _spawn(_child_env(owner))
        yield owner, child
    finally:
        for proc in (child, owner):
            if proc is not None:
                _terminate(proc)


@pytest.fixture(scope="module", autouse=True)
def _the_machine_can_answer_the_port_question():
    """Skip the module on a machine whose port probe does not work.

    The ladder refuses to kill anything whose listening ports could not
    be read — *unknown* is not *absent* — so on such a machine no
    process is ever a candidate and the positive control below could
    not pass for the right reason.
    """
    if reaper.listening_ports_by_pid() is None:
        pytest.skip(
            "this machine has no working port probe; the ladder cannot "
            "reach a kill decision on it"
        )


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _config(plans_root: Path, *pids: int, dry_run: bool = False) -> ReaperConfig:
    """A configuration pointed at *plans_root* and at *pids* only.

    ``pid_scope`` is what keeps this test off the rest of the machine:
    the reaper judges these pids and no others.
    """
    return ReaperConfig(
        enabled=True,
        grace=GRACE,
        plans_root=plans_root,
        dry_run=dry_run,
        pid_scope=tuple(pids),
    )


def _verdict_for(result: ScanResult, pid: int) -> Verdict:
    """Return the verdict about *pid*."""
    for verdict in result.verdicts:
        if verdict.proc.pid == pid:
            return verdict
    raise AssertionError(
        "no verdict about pid %s; the scan judged %r"
        % (pid, [v.proc.pid for v in result.verdicts])
    )


def _wait_until_gone(proc: subprocess.Popen, timeout: float = DEADLINE) -> bool:
    """Wait for *proc* to disappear, polling rather than sleeping.

    ``poll()`` reaps the process as it goes, so a process that has died
    is seen as gone rather than lingering as a zombie.
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_STEP)


def _assert_the_marker_is_readable(owner: subprocess.Popen, child: subprocess.Popen) -> None:
    """Fail loudly when the child's marker cannot be read.

    The reaper judges a candidate on the environment the operating
    system reports for it.  When that environment cannot be read the
    candidate is skipped — which would make every assertion in this
    file pass for the wrong reason.  The fixtures are launched with
    ``sys.executable`` precisely so that this check succeeds; it is
    here to prove that it does.
    """
    env = reaper.read_process_environ(child.pid)
    assert env is not None, (
        "the environment of pid %s could not be read; the fixture must be "
        "launched with sys.executable, not with an Apple platform binary"
        % child.pid
    )
    assert env.get(reaper.SPAWN_OWNER_PID) == str(owner.pid), (
        "the marker of pid %s names owner %r, not %r"
        % (child.pid, env.get(reaper.SPAWN_OWNER_PID), owner.pid)
    )
    assert env.get(reaper.SPAWN_KIND) == KIND_SERVICE, (
        "the marker of pid %s carries kind %r, not %r"
        % (child.pid, env.get(reaper.SPAWN_KIND), KIND_SERVICE)
    )


def _events(plans_root: Path) -> List[Dict[str, object]]:
    """Return the records the reaper wrote to its event log."""
    path = reaper.events_path(plans_root)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _event_for(plans_root: Path, pid: int) -> Dict[str, object]:
    """Return the event log's record about *pid*."""
    for record in _events(plans_root):
        if record.get("pid") == pid:
            return record
    raise AssertionError(
        "no event about pid %s; the log holds %r"
        % (pid, [r.get("pid") for r in _events(plans_root)])
    )


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_child_survives_while_owner_alive(tmp_path, owner_and_child):
    """The owner is alive, so the child is left alone.

    This is the negative control the whole module exists for.  The
    reaper sees a marked process whose owner is alive and whose owner's
    start time is the one the marker recorded — a process somebody is
    still using — and it must send nothing at all.
    """
    owner, child = owner_and_child
    _assert_the_marker_is_readable(owner, child)

    result = reaper.run_once(
        _config(tmp_path, owner.pid, child.pid),
        swap_ratio=0.0,
    )

    verdict = _verdict_for(result, child.pid)
    assert verdict.decision == DECISION_SKIP
    assert verdict.reason == REASON_OWNER_ALIVE
    assert verdict.signal == "", "a signal was sent while the owner was alive"

    assert child.poll() is None, "the child died while its owner was alive"

    record = _event_for(tmp_path, child.pid)
    assert record["reason"] == REASON_OWNER_ALIVE
    assert record["signal"] == ""


def test_child_reaped_after_owner_killed(tmp_path, owner_and_child):
    """The owner dies, the child becomes an orphan, and it is reclaimed.

    The positive control.  The child is marked, its owner is gone, it
    holds no protected port — so the ladder decides *kill*, the
    reaping stage signals the process group, and the child is gone
    once the grace period has run.
    """
    owner, child = owner_and_child
    _assert_the_marker_is_readable(owner, child)

    owner.kill()
    owner.wait()

    result = reaper.run_once(
        _config(tmp_path, owner.pid, child.pid),
        swap_ratio=0.0,
    )

    verdict = _verdict_for(result, child.pid)
    assert verdict.decision == DECISION_KILL
    assert verdict.reason == REASON_ORPHAN

    # The candidate leads its own group, which is the only shape in
    # which the group may be signalled at all — a group led by
    # somebody else holds processes the ladder never judged.
    assert verdict.proc.pgid == child.pid, (
        "the fixture does not lead its own group (pgid=%r, pid=%r)"
        % (verdict.proc.pgid, child.pid)
    )

    # Either signal counts: the process was reached, and the stage
    # stopped there.  Which one it was depends on the platform.  The
    # fixture is a child of the test process, so it lingers as a zombie
    # after SIGTERM until it is waited for — and macOS refuses to
    # signal a group whose leader is a zombie (``killpg`` answers
    # EPERM), so the escalation to SIGKILL fails there while the
    # process is already gone.  In production an orphan's parent is
    # dead, so the orphan is re-parented to init and its pid vanishes
    # at once: no zombie, no failed escalation.
    assert verdict.signal in (SIGNAL_TERM, SIGNAL_KILL), (
        "the orphan was decided upon but nothing was signalled (signal=%r)"
        % verdict.signal
    )

    assert _wait_until_gone(child), "the orphan was not reaped"

    record = _event_for(tmp_path, child.pid)
    assert record["reason"] == REASON_ORPHAN


def test_dry_run_does_not_kill(tmp_path, owner_and_child):
    """A dry run decides and stops: the orphan is left running.

    The same scene as the positive control, with the signals turned
    off.  The decision is still made — the verdict says *kill* — and
    the process is still there afterwards, which is the whole promise
    of the mode.
    """
    owner, child = owner_and_child
    _assert_the_marker_is_readable(owner, child)

    owner.kill()
    owner.wait()

    result = reaper.run_once(
        _config(tmp_path, owner.pid, child.pid, dry_run=True),
        swap_ratio=0.0,
    )

    verdict = _verdict_for(result, child.pid)
    assert verdict.decision == DECISION_KILL
    assert verdict.reason == REASON_ORPHAN
    assert verdict.signal == SIGNAL_NONE

    assert child.poll() is None, "a dry run killed the orphan"

    record = _event_for(tmp_path, child.pid)
    assert record["reason"] == REASON_ORPHAN
    assert record["signal"] == SIGNAL_NONE
