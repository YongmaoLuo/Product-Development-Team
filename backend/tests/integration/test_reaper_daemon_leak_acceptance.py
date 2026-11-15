"""The negative controls for the reaper daemon leak.

What this file is for
---------------------
The reaper daemon leaked.  On 2026-11-02 one plan root carried fifteen
daemons at once and one machine carried a hundred and forty-five of
them.  Every one of those walks the whole process table every thirty
seconds, and not one of them belongs to anybody: the lock file they
shared was deleted and re-created underneath them, so each of them
believed it was the only one.

The mechanisms that close the leak are already in place -- the census
that reads the machine, the one-daemon-per-root rule, the ceiling on
the machine, the daemon that leaves when the root it watches is
replaced, and the guard the suite installs to take its own daemons
back.  What was missing was the *evidence*: controls that fail when one
of those mechanisms stops working, and that can be re-run to show it.

The four controls
-----------------
``test_five_launches_leave_one_daemon``
    The leak itself.  The same root is asked for a daemon five times and
    one daemon is left.

    The lock file is removed between the launches, because that is what
    the machine did on 2026-11-02: the lock was deleted and re-created,
    so the next daemon took a **fresh inode** to lock while the daemon
    already running kept the old one, and the singleton was broken.
    With the lock gone the singleton cannot be the reason only one
    daemon is left -- the machine-wide census is, and the census is what
    this control exists to pin.  A control that left the lock alone
    would be measuring the lock.

``test_status_lists_running_daemon``
    The observability an operator needs: ``--status`` says which
    daemons are running, on which root, and for how long.

``test_suite_guard_is_installed``
    The suite's own guard: an autouse fixture that takes back the
    daemons the run started, and only those.

``test_the_census_says_when_the_machine_could_not_be_read``
    The census the leak was closed by, and the fact it has to report
    besides the daemons: whether the machine could be read at all.  A
    machine that cannot be read is not a machine with no daemons on it,
    and the control that counts the daemons must not turn the first
    into the second.

    On 2026-11-06 a machine with a load average of 39 did not answer
    the read, and the census reported "no daemons" for a machine that
    was running five of them.  The count was never made.

The rules
---------
* **Nothing this file starts may outlive it.**  Every case cleans up in
  a ``finally``, so a case that fails half way through leaves no daemon
  behind either.
* **Nothing this file starts may touch the machine.**  The daemons are
  started in dry-run mode: they decide, they never signal.
* **The machine's own daemons are not this file's.**  Every assertion
  is filtered by the plan root the case made, so a developer with a
  reaper of their own gets the same answer as a runner with none.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402

pytestmark = pytest.mark.integration

logger = logging.getLogger(__name__)


#: How many times the same root is asked for a daemon.
LAUNCHES = 5

#: The pause between two launches.  It gives a daemon that has just
#: stood down the time to leave, and the next one the time to find the
#: lock it is going to break.
LAUNCH_GAP = 0.2

#: How often the census is asked while waiting for something.
POLL_INTERVAL = 0.2

#: How long the first daemon has to take the lock before the next one
#: asks for the same root.
LOCK_TIMEOUT = 60.0

#: How long a daemon that is going to stand down has to leave.
#:
#: A daemon that is going to stand down has left within a second or
#: two -- it stands down before it scans anything -- so this is a bound
#: for the *failing* case rather than a wait it normally spends.
STAND_DOWN_TIMEOUT = 30.0

#: How long a daemon has to show up in the census.
DAEMON_TIMEOUT = 60.0

#: The module the daemons are started as -- and the name the census
#: recognises them by.
DAEMON_MODULE = "backend.orphan_reaper"

#: The name the suite's guard is registered under.
GUARD_FIXTURE = "_reclaim_reaper_daemons"


# ---------------------------------------------------------------------------
# The environment a daemon of this file is started with
# ---------------------------------------------------------------------------


def _daemon_env() -> Dict[str, str]:
    """Return the environment the daemons of this file are started with.

    ``PDT_ORPHAN_REAPER_MAX_DAEMONS=0`` turns the machine-wide ceiling
    **off**: a ceiling of zero is no ceiling.  With the ceiling left at
    its default, a machine that already runs eight daemons would refuse
    to start any daemon at all, and the case would be measuring the
    ceiling instead of the one-daemon-per-root rule it is here to pin.

    ``PDT_ORPHAN_REAPER_DRY_RUN=1`` makes a daemon decide and never
    signal.  A daemon that scans the machine for real is entitled to
    kill the orphans it finds, and this file is not entitled to kill
    anything on the developer's machine.
    """
    env = dict(os.environ)
    env[reaper.ENV_MAX_DAEMONS] = "0"
    env[reaper.ENV_DRY_RUN] = "1"
    return env


# ---------------------------------------------------------------------------
# Reading the machine
# ---------------------------------------------------------------------------


def _resolved(path: object) -> Optional[str]:
    """Return *path* as one absolute path, or ``None``.

    The comparison is made after resolution, because a root can be
    written down in more than one way -- through a symlink, relatively,
    with a trailing separator -- and all of them name one directory.
    The reaper's own rule for "is that the same root?" is the one used
    here, so that the case filters the census the way the daemon does.
    """
    try:
        return str(Path(str(path)).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _daemons_for_root(root: Path) -> Tuple[List[Any], bool]:
    """Return the live reaper daemons serving *root*, and whether the
    machine could be read.

    The machine's own daemons are not this file's business, so the
    answer is filtered by the root: a developer with a reaper of their
    own gets the same answer as a runner with none.

    The second half of the answer is the census's own: "there is no
    daemon on this root" and "I could not look" are different facts.
    A machine too busy to answer gives the second one, and it says
    nothing about the daemons on it.
    """
    target = _resolved(root)
    if target is None:
        return [], True

    daemons, readable = reaper.census_reaper_daemons()
    found: List[Any] = []
    for daemon in daemons:
        served = getattr(daemon, "plans_root", None)
        if not served:
            continue
        if _resolved(served) == target:
            found.append(daemon)
    return found, readable


def _entry_for(status: Dict[str, Any], pid: int) -> Optional[Dict[str, Any]]:
    """Return the census entry for *pid*, or ``None``."""
    for entry in status.get("daemons", []) or []:
        if entry.get("pid") == pid:
            return entry
    return None


def _entries_for_root(status: Dict[str, Any], root: Path) -> List[Dict[str, Any]]:
    """Return the census entries that serve *root*.

    The machine's own daemons are not this file's business, so the
    entries are filtered by the root they serve -- the same way the
    daemons themselves are filtered.
    """
    target = _resolved(root)
    if target is None:
        return []

    found: List[Dict[str, Any]] = []
    for entry in status.get("daemons", []) or []:
        served = entry.get("plans_root")
        if not served:
            continue
        if _resolved(served) == target:
            found.append(entry)
    return found


def _wait_for_a_daemon(
    root: Path, timeout: float = DAEMON_TIMEOUT
) -> Tuple[Optional[Any], bool]:
    """Wait until the census reports a daemon serving *root*.

    The daemon is ``None`` when the census never reported one, and the
    second half of the answer says whether the census could read the
    machine at all: a machine that could not be read reports no daemon
    and does not know that there is none.
    """
    deadline = time.monotonic() + timeout
    while True:
        live, readable = _daemons_for_root(root)
        if live:
            return live[0], True
        if time.monotonic() >= deadline:
            return None, readable
        time.sleep(POLL_INTERVAL)


def _wait_for_the_lock(root: Path, timeout: float = LOCK_TIMEOUT) -> bool:
    """Wait until a daemon holds the lock of *root*.

    The lock is the daemon's own answer to "is somebody already
    watching this root?", and it is the answer the next launch has to
    run into: a case that launched the next daemon before the first one
    had taken the lock would be measuring which daemon won the race.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if reaper.daemon_alive(root):
                return True
        except Exception:  # noqa: BLE001 - the lock may not be readable yet
            logger.warning("the reaper lock could not be read", exc_info=True)
        time.sleep(POLL_INTERVAL)
    return False


def _settle(farm: "_DaemonFarm", timeout: float) -> Tuple[List[Any], int, bool]:
    """Wait for the launched daemons to stop standing down.

    Returns the daemons still serving the root, the number of the
    launched processes that have left, and whether the census could read
    the machine.  A daemon that stands down has to *leave*: a process
    that is still there is a daemon that did not stand down, however
    polite its log line was.

    A census that could not read the machine is retried until the
    deadline: a machine that is briefly too busy to answer is not a
    machine that cannot be read at all.  What it must never become is
    an answer of zero.
    """
    deadline = time.monotonic() + timeout
    while True:
        exited = sum(1 for process in farm.processes if process.poll() is not None)
        live, readable = _daemons_for_root(farm.root)
        if readable and exited >= len(farm.processes) - 1 and len(live) == 1:
            return live, exited, True
        if time.monotonic() >= deadline:
            return live, exited, readable
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# The daemons a case starts
# ---------------------------------------------------------------------------


class _DaemonFarm:
    """The daemons one case started, and the root they serve.

    The farm owns the two things a case must not forget: the processes
    it started and the root they serve.  Giving them back is the farm's
    job, and it is done in a ``finally`` -- a case that fails half way
    through leaves no daemon behind either.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.processes: List[subprocess.Popen] = []
        self.log_path = root / reaper.REAPER_DIRNAME / reaper.DAEMON_LOG_FILENAME

    def spawn(self) -> subprocess.Popen:
        """Start one daemon on the farm's root, and return it."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            DAEMON_MODULE,
            "--daemon",
            "--interval",
            "30",
            "--plans-root",
            str(self.root),
        ]
        with open(self.log_path, "ab") as log:
            process = subprocess.Popen(
                cmd,
                env=_daemon_env(),
                cwd=str(REPO_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(process)
        return process

    def break_the_lock(self) -> None:
        """Delete the singleton's lock file.

        This is what the machine did on 2026-11-02, and it is the whole
        point of the case: with the lock file gone, the next daemon
        takes a **fresh inode** to lock while the daemon already running
        keeps the old one -- and the singleton is broken.  The next
        daemon therefore has to be stopped by the census, which is the
        thing the leak was about.
        """
        try:
            reaper.lock_path(self.root).unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - an unwritable root
            logger.warning("the reaper lock could not be removed", exc_info=True)

    def stop(self) -> None:
        """Take every daemon of this root back.

        The stop is the module's own helper -- the one that addresses a
        daemon by the root it serves -- and it is followed by a wait on
        the processes this farm started, so that nothing is left behind
        and nothing is left as a zombie.
        """
        try:
            reaper.stop_daemons_for_plans_root(self.root)
        except Exception:  # noqa: BLE001 - cleanup must not fail the case
            logger.warning(
                "the reaper daemons of %s could not be stopped", self.root,
                exc_info=True,
            )
        for process in self.processes:
            try:
                process.wait(timeout=15)
            except Exception:  # noqa: BLE001 - the process may be stubborn
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:  # noqa: BLE001 - nothing more can be done
                    logger.warning(
                        "the reaper daemon %s could not be reaped", process.pid,
                        exc_info=True,
                    )


@pytest.fixture
def farm(tmp_path: Path) -> Iterator[_DaemonFarm]:
    """A plan root of this run's own, and the daemons started on it.

    The root lives inside pytest's temporary tree, which is what makes
    it this run's: the suite's guard reclaims the daemons of a root
    pytest made, and leaves everybody else's alone.
    """
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    created = _DaemonFarm(root)
    try:
        yield created
    finally:
        created.stop()


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------


def _assert_the_leak_is_closed(
    live: List[Any], exited: int, readable: bool
) -> None:
    """The control's own verdict on the census it read.

    The count of daemons is only an answer when the machine could be
    read to count them.  An unreadable machine is reported as
    unreadable -- the answer is unknown, not zero -- and the case
    still fails: a control that cannot measure the leak must not pass
    it either.

    Whether the *gate* books that failure against the code is a
    separate question, and it is answered by the shape of the message:
    a machine that could not be read is the machine's shape, so the
    failure is not turned into a repair task against a delivery that
    did not fail.
    """
    assert readable, (
        "the machine could not be read for this census: the daemons on "
        "the root could not be counted, so the answer is unknown rather "
        "than zero"
    )
    # The wording names the wait that ran out, and not only the answer:
    # the gate reads the *shape* of a failure to decide whether the
    # machine or the code owns it, and "it waited and the wait ran out"
    # is the machine's shape.  A message that said only "the root
    # carries N daemons" would be indistinguishable from a code
    # failure, and a busy host would turn this control into a repair
    # task against a delivery that did not fail.
    assert len(live) == 1, (
        "the daemon count did not settle within 30s: the root carries %d "
        "daemons after %d launches: %r"
        % (len(live), LAUNCHES, [(d.pid, d.plans_root) for d in live])
    )
    assert exited >= LAUNCHES - 1, (
        "%d of the %d launched daemons are still running"
        % (LAUNCHES - exited, LAUNCHES)
    )


def test_five_launches_leave_one_daemon(farm: _DaemonFarm) -> None:
    """Five launches on one root leave one daemon.

    This is the leak, reproduced and closed.  The root is asked for a
    daemon five times; the lock is broken between the launches, so the
    singleton cannot be what leaves one daemon standing; and the answer
    has to be exactly one.

    The daemons the machine already ran are not this case's business:
    the answer is read off the root the case made.
    """
    for index in range(LAUNCHES):
        if index:
            # The lock the previous daemon took is removed before the
            # next one asks for the root: the next daemon gets a fresh
            # inode to lock, and the singleton is broken.
            farm.break_the_lock()
            time.sleep(LAUNCH_GAP)
        farm.spawn()
        if index == 0:
            # The first daemon has to hold the lock before the next one
            # asks for the same root -- otherwise the case would be
            # measuring which daemon won the race for it.
            _wait_for_the_lock(farm.root)

    live, exited, readable = _settle(farm, STAND_DOWN_TIMEOUT)

    _assert_the_leak_is_closed(live, exited, readable)


def test_status_lists_running_daemon(farm: _DaemonFarm) -> None:
    """``--status`` says which daemons run, where, and for how long.

    The census is how an operator finds out that the machine is running
    a hundred daemons, so it has to name the daemon, the root it
    serves and how long it has been there.  The command line is asked
    as well as the function: the JSON on the terminal is what a person
    or a script actually reads.
    """
    farm.spawn()
    daemon, readable = _wait_for_a_daemon(farm.root)
    assert readable, (
        "the machine could not be read for this census: the daemon on %s "
        "could not be found, so the answer is unknown rather than absent"
        % (farm.root,)
    )
    assert daemon is not None, "the daemon never showed up in the census"

    status = reaper.daemon_status()
    entry = _entry_for(status, daemon.pid)
    assert entry is not None, (
        "the census does not mention the daemon %s: %r" % (daemon.pid, status)
    )
    assert _resolved(entry.get("plans_root")) == _resolved(farm.root), (
        "the daemon is reported on the wrong root: %r" % (entry.get("plans_root"),)
    )
    age = entry.get("age_seconds")
    assert age is not None, "the census does not say how old the daemon is"
    assert 0 <= float(age) < 600, (
        "the daemon cannot be %s seconds old: it was started a moment ago"
        % (age,)
    )

    # The count is the number that answers "how many daemons does this
    # machine run?" without walking the list -- so it has to be the
    # number of entries the list holds.
    assert status.get("count") == len(status.get("daemons", []) or []), (
        "the count does not match the list it counts: %r" % (status,)
    )
    # And the root the case made carries one daemon -- the one that was
    # launched.  The machine's own daemons are in the list too, but they
    # are not on this root.
    on_root = _entries_for_root(status, farm.root)
    assert len(on_root) == 1, (
        "the census reports %d daemons on %s: %r"
        % (len(on_root), farm.root, [(d.get("pid"), d.get("plans_root")) for d in on_root])
    )

    payload = _status_json()
    listed = _entry_for(payload, daemon.pid)
    assert listed is not None, (
        "--status does not mention the daemon %s: %r" % (daemon.pid, payload)
    )
    assert _resolved(listed.get("plans_root")) == _resolved(farm.root), (
        "--status reports the daemon on the wrong root: %r"
        % (listed.get("plans_root"),)
    )
    assert listed.get("age_seconds") is not None, (
        "--status does not say how old the daemon is"
    )
    assert payload.get("count") == len(payload.get("daemons", []) or []), (
        "--status does not print a count that matches its list: %r" % (payload,)
    )
    on_root = _entries_for_root(payload, farm.root)
    assert len(on_root) == 1, (
        "--status reports %d daemons on %s: %r"
        % (len(on_root), farm.root, [(d.get("pid"), d.get("plans_root")) for d in on_root])
    )


def test_suite_guard_is_installed(request: pytest.FixtureRequest) -> None:
    """The suite registers an autouse fixture that takes its daemons back.

    A daemon is written to outlive the process that started it, so a
    suite that drives real servers leaves one behind per server.  The
    suite's guard is what takes them back: an **autouse** fixture, so
    that it runs whether or not a case asks for it, and a **session**
    fixture, so that it covers the whole run.

    The guard has to be narrow -- it reclaims the daemons of a root
    pytest made, and leaves the developer's own daemon alone -- and that
    narrowness lives in the helper the fixture calls, so the fixture is
    checked for the call it makes.
    """
    conftest = _suite_conftest()
    assert _is_the_suite_conftest(conftest), (
        "the guard was found in %r, which is not this suite's conftest"
        % (getattr(conftest, "__file__", None),)
    )

    fixture = getattr(conftest, GUARD_FIXTURE, None)
    assert fixture is not None, (
        "the suite's conftest does not define %r" % (GUARD_FIXTURE,)
    )

    marker = _fixture_marker(fixture)
    assert marker is not None, (
        "%r is not registered as a pytest fixture" % (GUARD_FIXTURE,)
    )
    assert getattr(marker, "autouse", False) is True, (
        "%r is not autouse: a case that did not ask for it would leave "
        "its daemon behind" % (GUARD_FIXTURE,)
    )
    assert getattr(marker, "scope", None) == "session", (
        "%r is not a session fixture: it would cover one case instead of "
        "the whole run" % (GUARD_FIXTURE,)
    )

    # The fixture has to be *registered*, not merely written down: asking
    # the running session for it is the only way to know that pytest
    # installed it.
    try:
        request.getfixturevalue(GUARD_FIXTURE)
    except pytest.FixtureLookupError:
        pytest.fail(
            "the running session does not know the fixture %r" % (GUARD_FIXTURE,)
        )

    source = inspect.getsource(_fixture_function(fixture))
    assert "reclaim_new_daemons" in source, (
        "%r does not reclaim anything" % (GUARD_FIXTURE,)
    )
    assert "snapshot_daemons" in source, (
        "%r does not take a census of the machine first, so it cannot "
        "tell its own daemons from somebody else's" % (GUARD_FIXTURE,)
    )


# ---------------------------------------------------------------------------
# The census the controls read
# ---------------------------------------------------------------------------


class _FakeProcess:
    """One process, as ``psutil`` would describe it.

    A command line of ``None`` is the case that matters: the kernel
    refused to hand it over, which is what happens to another user's
    process on macOS.
    """

    def __init__(self, pid: int, argv: Optional[List[str]]) -> None:
        self.pid = pid
        self._argv = argv

    def cmdline(self) -> List[str]:
        if self._argv is None:
            raise PermissionError(13, "the command line was refused")
        return list(self._argv)

    def ppid(self) -> int:
        return 1

    def pgid(self) -> int:
        return self.pid

    def uids(self) -> Any:
        return SimpleNamespace(effective=501, real=501)

    def memory_info(self) -> Any:
        return SimpleNamespace(rss=1024 * 64)


class _FakePsutil:
    """The ``psutil`` module, as the census uses it."""

    def __init__(self, processes: List[_FakeProcess]) -> None:
        self._processes = processes

    def process_iter(self) -> Iterator[_FakeProcess]:
        return iter(self._processes)


def test_the_census_says_when_the_machine_could_not_be_read(monkeypatch) -> None:
    """A machine that cannot be read is not a machine with no daemons.

    The census answers two questions at once: which daemons are
    running, and whether the machine could be read to find out.  The
    second one is what a busy machine gives.

    On 2026-11-06 a machine with a load average of 39 did not answer
    the read, and the census reported "no daemons" for a machine that
    was running five of them.  The count was never made, and a count
    that was never made is not a count of zero.
    """
    monkeypatch.setattr(reaper, "_resolve_psutil", lambda: None)
    monkeypatch.setattr(reaper, "_run_ps", lambda *args, **kwargs: None)

    processes, readable = reaper.census_processes()

    assert readable is False, (
        "the census reported a machine it could not read as a machine it "
        "had read"
    )
    assert processes == [], (
        "the census invented processes on a machine it could not read: %r"
        % (processes,)
    )


def test_the_census_asks_psutil_before_it_forks_ps(monkeypatch) -> None:
    """The census reads the machine without forking when it can.

    ``ps`` is a fork, and a fork on a machine that is already busy is a
    fork that does not come back.  ``psutil`` reads the machine
    directly, so it is asked first; ``ps`` is the fallback for a
    machine without it.
    """
    forked: List[Any] = []

    def fake_run(args, **kwargs):
        forked.append(list(args))
        raise OSError("ps must not be run while psutil can read the machine")

    monkeypatch.setattr(
        reaper,
        "_resolve_psutil",
        lambda: _FakePsutil(
            [_FakeProcess(4242, ["python", "-m", "backend.orphan_reaper"])]
        ),
    )
    monkeypatch.setattr(reaper, "_run_ps", fake_run)

    processes, readable = reaper.census_processes()

    assert readable is True
    assert [proc.pid for proc in processes] == [4242]
    assert forked == [], (
        "the census forked ps while psutil could read the machine: %r"
        % (forked,)
    )


def test_the_census_falls_back_to_ps_when_psutil_cannot_describe_a_process(
    monkeypatch,
) -> None:
    """A process the kernel refuses to describe makes the census fall back.

    ``psutil`` cannot read the command line of another user's process
    on macOS, and a census that cannot classify a process cannot claim
    to have read the machine -- a daemon it could not describe is a
    daemon it could not count.  The ``ps`` fallback is the complete
    read.
    """
    ps_table = (
        "  11  1  11  501  1024 /usr/bin/python3 -m backend.orphan_reaper"
        " --daemon\n"
    )
    monkeypatch.setattr(
        reaper,
        "_resolve_psutil",
        lambda: _FakePsutil([_FakeProcess(4242, None)]),
    )
    monkeypatch.setattr(reaper, "_run_ps", lambda *args, **kwargs: ps_table)

    processes, readable = reaper.census_processes()

    assert readable is True
    assert [proc.pid for proc in processes] == [11]
    assert processes[0].argv.endswith("--daemon")


def test_the_census_retries_the_ps_fallback(monkeypatch) -> None:
    """The ``ps`` fallback is tried more than once, and a bounded number
    of times.

    A machine that is briefly too busy to answer is not a machine that
    cannot be read at all -- but a census that waits forever is no
    better than one that gives up at once.
    """
    attempts: List[Any] = []

    def fake_run(args, **kwargs):
        attempts.append(list(args))
        return None

    monkeypatch.setattr(reaper, "_resolve_psutil", lambda: None)
    monkeypatch.setattr(reaper, "_run_ps", fake_run)
    monkeypatch.setattr(reaper, "CENSUS_READ_RETRY_DELAY_SEC", 0.0)

    processes, readable = reaper.census_processes()

    assert readable is False
    assert len(attempts) == reaper.CENSUS_READ_ATTEMPTS, (
        "the census asked ps %d times; it is supposed to try more than "
        "once and no more than %d times"
        % (len(attempts), reaper.CENSUS_READ_ATTEMPTS)
    )


def test_the_control_reports_an_unreadable_machine_as_unreadable() -> None:
    """An unreadable machine is reported as unreadable, not as zero daemons.

    The control reads the machine and counts the daemons it finds.  A
    machine too busy to answer gives no count at all, and the control
    has to say so: "the root carries 0 daemons" would be a statement
    about the machine that was never made.  The case still fails -- a
    control that cannot measure the leak must not pass it either.
    """
    with pytest.raises(AssertionError) as failure:
        _assert_the_leak_is_closed([], 0, False)

    message = str(failure.value)
    assert "could not be read" in message, message
    assert "0 daemons" not in message, message


# ---------------------------------------------------------------------------
# The helpers the controls read
# ---------------------------------------------------------------------------


def _status_json() -> Dict[str, Any]:
    """Return what ``--status`` prints, as data."""
    completed = subprocess.run(
        [sys.executable, "-m", DAEMON_MODULE, "--status"],
        env=dict(os.environ),
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=DAEMON_TIMEOUT,
    )
    assert completed.returncode == 0, (
        "--status failed: %s" % (completed.stderr.strip(),)
    )
    return json.loads(completed.stdout)


def _suite_conftest() -> Any:
    """Return the conftest module the suite runs under.

    The module pytest itself imported is preferred: it is the one whose
    fixtures are registered in this session.  Its name depends on where
    the run was started from -- ``tests.conftest`` from ``backend/`` and
    ``backend.tests.conftest`` from the repository root -- so it is
    looked for by the guard it defines rather than by one spelling.
    """
    for name, module in list(sys.modules.items()):
        parts = name.split(".")
        if parts[-2:] != ["tests", "conftest"] and name != "conftest":
            continue
        if hasattr(module, GUARD_FIXTURE):
            return module

    import tests.conftest as module  # noqa: PLC0415 - imported on demand

    return module


def _is_the_suite_conftest(module: Any) -> bool:
    """Return whether *module* is this suite's own conftest."""
    path = getattr(module, "__file__", None)
    if not path:
        return False
    try:
        return Path(path).resolve() == (BACKEND_DIR / "tests" / "conftest.py").resolve()
    except (OSError, ValueError):  # pragma: no cover - an unreadable path
        return False


def _fixture_marker(fixture: Any) -> Optional[Any]:
    """Return the marker pytest registered *fixture* with, or ``None``.

    pytest has moved the marker around over the years: it used to hang
    off the function as ``_pytestfixturefunction`` and now hangs off the
    definition object as ``_fixture_function_marker``.  Both spellings
    are read, and a fixture with neither is not a fixture.
    """
    for attribute in ("_pytestfixturefunction", "_fixture_function_marker"):
        marker = getattr(fixture, attribute, None)
        if marker is not None:
            return marker
    return None


def _fixture_function(fixture: Any) -> Any:
    """Return the function behind a pytest fixture."""
    for attribute in ("_fixture_function", "__wrapped__"):
        candidate = getattr(fixture, attribute, None)
        if candidate is not None:
            return candidate
    return fixture
