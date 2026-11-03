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

The three controls
------------------
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


def _daemons_for_root(root: Path) -> List[Any]:
    """Return the live reaper daemons serving *root*.

    The machine's own daemons are not this file's business, so the
    answer is filtered by the root: a developer with a reaper of their
    own gets the same answer as a runner with none.
    """
    target = _resolved(root)
    if target is None:
        return []

    found: List[Any] = []
    for daemon in reaper.list_reaper_daemons():
        served = getattr(daemon, "plans_root", None)
        if not served:
            continue
        if _resolved(served) == target:
            found.append(daemon)
    return found


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


def _wait_for_a_daemon(root: Path, timeout: float = DAEMON_TIMEOUT) -> Optional[Any]:
    """Wait until the census reports a daemon serving *root*."""
    deadline = time.monotonic() + timeout
    while True:
        live = _daemons_for_root(root)
        if live:
            return live[0]
        if time.monotonic() >= deadline:
            return None
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


def _settle(farm: "_DaemonFarm", timeout: float) -> Tuple[List[Any], int]:
    """Wait for the launched daemons to stop standing down.

    Returns the daemons still serving the root and the number of the
    launched processes that have left.  A daemon that stands down has
    to *leave*: a process that is still there is a daemon that did not
    stand down, however polite its log line was.
    """
    deadline = time.monotonic() + timeout
    while True:
        exited = sum(1 for process in farm.processes if process.poll() is not None)
        live = _daemons_for_root(farm.root)
        if exited >= len(farm.processes) - 1 and len(live) == 1:
            return live, exited
        if time.monotonic() >= deadline:
            return live, exited
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

    live, exited = _settle(farm, STAND_DOWN_TIMEOUT)

    assert len(live) == 1, (
        "the root carries %d daemons after %d launches: %r"
        % (len(live), LAUNCHES, [(d.pid, d.plans_root) for d in live])
    )
    assert exited >= LAUNCHES - 1, (
        "%d of the %d launched daemons are still running"
        % (LAUNCHES - exited, LAUNCHES)
    )


def test_status_lists_running_daemon(farm: _DaemonFarm) -> None:
    """``--status`` says which daemons run, where, and for how long.

    The census is how an operator finds out that the machine is running
    a hundred daemons, so it has to name the daemon, the root it
    serves and how long it has been there.  The command line is asked
    as well as the function: the JSON on the terminal is what a person
    or a script actually reads.
    """
    farm.spawn()
    daemon = _wait_for_a_daemon(farm.root)
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
