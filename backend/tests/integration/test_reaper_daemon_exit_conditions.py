"""The reaper daemon's exits, proved on real processes.

The unit suites drive the daemon's loop with a fake clock and a fake
scan: they can say that the loop *would* leave when the root is gone
or when the owner is dead, because they hand the loop the answer.  What
they cannot say is whether the daemon that is really running on this
machine notices.  A daemon is a process that outlives the thing that
started it, so the interesting question is never "does the code say
so" — it is "does the process actually go away".

This file is that other half.  It starts real daemons, with the real
command line, on real plan roots, and watches what the operating
system does to them.

There are three cases, and the third is the control for the second:

  * **The root is deleted.**  A daemon watching a directory that is not
    there is watching nothing, so it leaves — within one interval.
  * **The process that started the daemon is killed.**  The daemon was
    started for that process, and it is over, so the daemon leaves.
  * **The process that started the daemon is killed, and the daemon was
    told to survive its owner.**  It does not leave.  This is the
    daemon the *server* starts: "the backend died" is precisely the
    situation it exists for.

The third case is what makes the second one mean something.  Without
it, "the daemon went away" could be the daemon leaving for a dozen
reasons that have nothing to do with the owner — a crash, a stale
lock, a census it read wrongly.

Five things about the harness are load-bearing
-----------------------------------------------

  * **The daemon is started with the machine's own reaper settings
    removed.**  Every ``PDT_ORPHAN_REAPER*`` variable the ambient
    environment carries is dropped, so an interval, an idle window or
    a lifetime set on this machine cannot decide when the daemon under
    test leaves.  The one setting that is put back is
    ``PDT_ORPHAN_REAPER_MAX_DAEMONS=0``: a ceiling of zero is no
    ceiling, so the daemon starts even on a machine that is already
    running its quota of reaper daemons.  Without that, the test would
    fail on a busy machine for a reason that has nothing to do with
    what it is about.

  * **The daemon is given an empty pid scope.**  A configured scope
    restricts the scan to the pids it names, and an explicitly empty
    scope is an empty scope rather than an absent one — the reaper
    judges nothing at all.  The daemon under test is here to be watched,
    not to be let loose on the developer's machine: a daemon that walks
    the whole process table may find a real orphan and kill it, and a
    test must not take the machine down with it.  The three exits under
    test do not depend on the scan's verdicts at all.

  * **The plan root is not under the pytest tree.**  The suite's own
    session guard reclaims the reaper daemons that serve a root pytest
    made: at the end of a session it stops every daemon that was not
    running when the session took its census and that watches a pytest
    root.  The rule is right for the daemons a test run starts by
    accident — and it would reach into this test, because a daemon
    watching a pytest root is exactly what it is looking for.  Another
    session's guard, ending while this test is watching its daemon,
    would stop the daemon under test, and the test would fail — or
    pass — for a reason that has nothing to do with what it is about.
    So the daemon under test watches a root of its own, outside the
    pytest tree, where no guard reaches it, and this test stops it
    itself.

  * **The deleted root has to stay deleted.**  The daemon writes its
    kill events under the root it watches, and that write creates the
    directories it needs — so a daemon whose root has just been
    deleted re-creates it, and the "the root is gone" exit never
    fires.  A re-created root is a *new* directory, and whether the
    daemon notices that depends on whether the filesystem hands out the
    same inode number again: on some filesystems it does, and then the
    daemon goes on watching a root it was never started for, and this
    test fails for a reason that has nothing to do with what it is
    about.
    So the test deletes the root and then makes the directory that
    holds it read-only: the daemon may no longer create anything
    there, the root stays gone, and what is left for the daemon to
    notice is the thing this test is about.  The one consequence is a
    warning in the daemon's log when a pass overlaps the deletion —
    the daemon could not write its events — which the test reads as
    the expected explanation rather than as a crash.

  * **The wrapper's command line stays clean.**  The reaper's census
    reads the machine's process table and calls a process a reaper
    daemon when its command line names the reaper *and* asks for the
    daemon mode.  A wrapper that passed the daemon's arguments on its
    own command line would therefore look like a second daemon watching
    the same root, and the daemon under test would stand down at
    startup — for the wrong reason, and it would look like the test
    passing.  So the wrapper is handed the daemon's arguments through
    the environment, and its own command line says nothing about the
    reaper.

Nothing here sleeps for a fixed time: every wait polls, and every wait
has a deadline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

#: The checkout the daemon under test is started in.  The daemon is
#: started as ``python -m backend.orphan_reaper``, so it has to be
#: started where the ``backend`` package is.
REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.integration


#: How long a daemon may take to act on one of the three exits.
DEADLINE = 8.0

#: How often a wait asks whether a process is still there.
POLL_STEP = 0.05

#: How long the daemon may take to come up at all.
STARTUP_DEADLINE = 20.0

#: How long the daemon has to stay up before the test believes it is
#: running rather than on its way out.
SETTLE = 0.5

#: How long the daemon is given to answer a ``SIGTERM`` before it is
#: killed outright.
TERM_GRACE = 5.0


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------
#
# The owner of a daemon is, by default, the process that started it.
# To kill that process without killing the daemon, the daemon has to be
# a *child* of a process the test can kill: the daemon has to be one
# step below the test, not one step above it.
#
# The wrapper is that middle process.  It starts the daemon, writes the
# daemon's pid down so the test can watch it, and then waits for it —
# so it stays alive exactly as long as the daemon does, and killing it
# leaves the daemon running with its owner gone.
#
# Its source is kept free of the words the census looks for ("--daemon"
# and the reaper's name): the census reads command lines, and a wrapper
# that carried the daemon's arguments on its own command line would be
# counted as a second daemon on the same root.

_WRAPPER_SOURCE = """
import json, os, subprocess, sys

argv = json.loads(os.environ["REAPER_TEST_CHILD_ARGV"])
child = subprocess.Popen(
    argv, stdout=subprocess.DEVNULL, stderr=sys.stderr, start_new_session=True
)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write("%d\\n" % child.pid)
sys.exit(child.wait())
"""


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


def _daemon_env() -> Dict[str, str]:
    """Return the environment the daemon under test is started with.

    The machine's own reaper settings are removed, so that nothing
    outside this file decides when the daemon leaves; the ceiling on
    the number of daemons is lifted, so that a machine already running
    its quota still lets this one start; and the scan is given an empty
    pid scope, so that the daemon judges nothing and cannot kill a real
    process on the developer's machine.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PDT_ORPHAN_REAPER")
    }
    env["PDT_ORPHAN_REAPER_MAX_DAEMONS"] = "0"
    env["PDT_ORPHAN_REAPER_PID_SCOPE"] = ""
    return env


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


def _plan_root() -> Tuple[Path, Path]:
    """Return the directory the daemon under test watches, and its parent.

    The root is a fresh directory of its own, made outside the pytest
    temporary tree, and the directory that holds it is returned with it
    so that the caller can take the whole thing away afterwards.

    It is deliberately **not** under ``tmp_path``: the suite's session
    guard reclaims the reaper daemons that serve a root pytest made, so
    a daemon watching a pytest root is a daemon another session's guard
    is entitled to stop.  See the module docstring.
    """
    base = Path(tempfile.mkdtemp(prefix="pdt-reaper-exit-"))
    root = base / "plans"
    root.mkdir()
    return base, root


# ---------------------------------------------------------------------------
# The root that has to stay deleted
# ---------------------------------------------------------------------------
#
# The daemon writes its kill events under the root it watches, and the
# write creates the directories it needs.  A daemon whose root has been
# deleted therefore re-creates it, and whether the daemon then notices
# that the root is not the one it was started for depends on whether the
# filesystem hands out the same inode number again -- sometimes it does,
# and then the daemon goes on watching a root it was never started for.
#
# The test does not leave that to the filesystem's mood.  It deletes the
# root and then makes the directory that holds it read-only: the daemon
# may no longer create anything there, the root stays gone, and the
# daemon's exit is the one this test is about.


def _seal(path: Path, mode: int) -> None:
    """Make *path* read-only: nothing may be created inside it any more.

    *mode* is the directory's own mode; the write bits are taken away
    and everything else is left as it was.
    """
    os.chmod(path, mode & ~0o222)


def _unseal(path: Path, mode: int) -> None:
    """Give *path* its permissions back, if it is still there."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _delete_root(base: Path, root: Path, mode: int, attempts: int = 5) -> bool:
    """Delete the daemon's plan root and make sure it stays deleted.

    The root is deleted first -- that needs a writable parent -- and the
    parent is sealed right afterwards, so that the daemon cannot put the
    root back.  A daemon whose pass overlaps the two steps may still
    re-create the root in between; the loop then removes it again, and
    this time the sealed parent keeps it gone.

    Returns whether the root is gone at the end.
    """
    for _ in range(attempts):
        shutil.rmtree(root, ignore_errors=True)
        _seal(base, mode)
        if not root.exists():
            return True
        _unseal(base, mode)
    return not root.exists()


def _daemon_argv(root: Path, *extra: str) -> List[str]:
    """Return the command line the daemon under test is started with."""
    return [
        sys.executable,
        "-m",
        "backend.orphan_reaper",
        "--daemon",
        "--interval",
        "1",
        "--plans-root",
        str(root),
        *extra,
    ]


def _spawn_daemon(root: Path, env: Dict[str, str], log: Path) -> subprocess.Popen:
    """Start the daemon as a child of this test process."""
    with open(log, "wb") as handle:
        return subprocess.Popen(  # noqa: S603 - the argv is built above
            _daemon_argv(root),
            cwd=str(REPO_ROOT),
            env=env,
            stdout=handle,
            stderr=handle,
        )


def _spawn_wrapper(
    root: Path, env: Dict[str, str], log: Path, pid_file: Path, *extra: str
) -> subprocess.Popen:
    """Start the wrapper that starts the daemon."""
    child_env = dict(env)
    child_env["REAPER_TEST_CHILD_ARGV"] = json.dumps(_daemon_argv(root, *extra))
    with open(log, "wb") as handle:
        return subprocess.Popen(  # noqa: S603 - the argv is built above
            [sys.executable, "-c", _WRAPPER_SOURCE, str(pid_file)],
            cwd=str(REPO_ROOT),
            env=child_env,
            stdout=handle,
            stderr=handle,
        )


# ---------------------------------------------------------------------------
# The waits
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    """Whether *pid* is a process that is still running.

    A zombie counts as gone: the kernel has finished it and only the
    reaping is left, and ``kill(pid, 0)`` says a zombie is alive.  The
    state is therefore read as well — otherwise a daemon that has
    already left would keep the test waiting for a pid that is only
    waiting to be reaped.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    state = completed.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_until_gone(pid: int, timeout: float = DEADLINE) -> bool:
    """Wait for *pid* to disappear, polling rather than sleeping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(POLL_STEP)
    return not _alive(pid)


def _wait_until_running(pid: int, timeout: float = STARTUP_DEADLINE) -> bool:
    """Wait until *pid* has been alive for :data:`SETTLE` seconds.

    A daemon stands down at startup when the census tells it that
    somebody else is already watching its root, and it leaves for the
    owner and for the root inside its first pass.  A daemon that is
    still there a moment after it came up has got past those checks,
    so this is the question the test asks before it does anything to
    the daemon.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while True:
        if not _alive(pid):
            return False
        now = time.monotonic()
        if now - started >= SETTLE:
            return True
        if now >= deadline:
            return False
        time.sleep(POLL_STEP)


def _wait_for_pid_file(path: Path, timeout: float = STARTUP_DEADLINE) -> Optional[int]:
    """Wait for the wrapper to write the daemon's pid down."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            try:
                return int(text)
            except ValueError:
                return None
        time.sleep(POLL_STEP)
    return None


# ---------------------------------------------------------------------------
# The teardown
# ---------------------------------------------------------------------------


def _stop_process(proc: Optional[subprocess.Popen]) -> None:
    """Stop a process this test started: ``SIGTERM``, then ``SIGKILL``."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=TERM_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=TERM_GRACE)
    except (OSError, subprocess.SubprocessError):
        pass


def _stop_pid(pid: Optional[int]) -> None:
    """Stop a process this test does not own: ``SIGTERM``, then ``SIGKILL``.

    The daemon of the wrapper case is not a child of this test — its
    parent was killed, and the machine took it over — so it cannot be
    waited for.  It can still be signalled, and it still has to go.
    """
    if pid is None or not _alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if _wait_until_gone(pid, timeout=TERM_GRACE):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _log_text(path: Path) -> str:
    """Return what the daemon wrote to its log, or the empty string."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


#: The exceptions the seal is expected to cause.  They are two faces of
#: one failure: the directory the daemon wanted to create is gone, and
#: the directory that holds it may not be written to.
SEAL_EXCEPTIONS = frozenset({"FileNotFoundError", "PermissionError"})

#: The line a Python traceback ends with: the exception and its message.
#: It starts at the left margin; the frames above it are indented.
_EXCEPTION_LINE = re.compile(
    r"^([A-Z][A-Za-z0-9_]*(?:Error|Exception|Exit|Interrupt))\b"
)


def _unexpected_exceptions(text: str) -> List[str]:
    """Return the exceptions in *text* that the sealed directory does not explain.

    The daemon could not write its kill events into a root whose parent
    the test sealed, and it says so in the log: the directory it wanted
    to create is gone, and the directory that holds it may not be
    written to.  Those two are expected -- they are the seal doing its
    job.  Every other exception in the log is a crash, and a crash is
    not an exit.
    """
    unexpected: List[str] = []
    for line in text.splitlines():
        match = _EXCEPTION_LINE.match(line)
        if match is None:
            continue
        if match.group(1) in SEAL_EXCEPTIONS:
            continue
        unexpected.append(line.strip())
    return unexpected


# ---------------------------------------------------------------------------
# The three exits
# ---------------------------------------------------------------------------


def test_daemon_exits_when_plans_root_deleted(tmp_path):
    """A daemon whose plan root is deleted leaves within one interval.

    The daemon was started inside a directory, and that directory can
    be deleted while it runs — a test's temporary tree is deleted when
    the session ends.  A daemon whose world is gone has nothing to
    reclaim, and the daemons that leaked on 2026-10-31 were all of
    that kind.
    """
    root_base, root = _plan_root()
    mode = root_base.stat().st_mode & 0o777
    log = tmp_path / "daemon.log"
    proc = _spawn_daemon(root, _daemon_env(), log)
    try:
        assert _wait_until_running(proc.pid), (
            "the daemon never settled into its loop; it left at startup"
        )

        # The root is deleted, and the directory that holds it is sealed
        # so that the daemon's own event write cannot put it back: what
        # is left for the daemon to notice is the root being gone.
        assert _delete_root(root_base, root, mode), (
            "the daemon kept re-creating the root it was watching"
        )

        assert _wait_until_gone(proc.pid, DEADLINE), (
            "the daemon outlived the root it was watching"
        )
        # It left through one of its own doors, not through a crash.
        assert proc.poll() == 0, "the daemon exited with %r" % (proc.poll(),)
        unexpected = _unexpected_exceptions(_log_text(log))
        assert not unexpected, (
            "the daemon died of an exception:\n%s" % "\n".join(unexpected)
        )
    finally:
        _stop_process(proc)
        _unseal(root_base, mode)
        shutil.rmtree(root_base, ignore_errors=True)


def test_daemon_exits_when_owner_killed(tmp_path):
    """A daemon whose owner is killed leaves within one interval.

    The daemon was started for that process: the server started it, a
    fixture started it, a test round started it.  When the process that
    started it is gone, the daemon has outlived the only reason it
    existed.
    """
    root_base, root = _plan_root()
    log = tmp_path / "daemon.log"
    pid_file = tmp_path / "daemon.pid"
    wrapper = _spawn_wrapper(root, _daemon_env(), log, pid_file)
    daemon_pid: Optional[int] = None
    try:
        daemon_pid = _wait_for_pid_file(pid_file)
        assert daemon_pid is not None, "the wrapper never started the daemon"
        assert _wait_until_running(daemon_pid), (
            "the daemon never settled into its loop; it left at startup"
        )

        # The owner is killed, not asked to leave: the daemon has to
        # notice on its own that the process which started it is gone.
        wrapper.send_signal(signal.SIGKILL)
        wrapper.wait(timeout=DEADLINE)

        assert _wait_until_gone(daemon_pid, DEADLINE), (
            "the daemon outlived the process that started it"
        )
        assert "Traceback" not in _log_text(log), (
            "the daemon died of an exception:\n%s" % _log_text(log)
        )
    finally:
        _stop_pid(daemon_pid)
        _stop_process(wrapper)
        shutil.rmtree(root_base, ignore_errors=True)


def test_detached_daemon_survives_owner_kill(tmp_path):
    """A daemon told to survive its owner outlives it.

    This is the daemon the *server* starts.  "The backend died" is
    precisely the situation it exists for, so it has to keep running
    after the process that started it is gone — and it does so for the
    whole window the other case allows for leaving.
    """
    root_base, root = _plan_root()
    log = tmp_path / "daemon.log"
    pid_file = tmp_path / "daemon.pid"
    wrapper = _spawn_wrapper(
        root, _daemon_env(), log, pid_file, "--survive-owner"
    )
    daemon_pid: Optional[int] = None
    try:
        daemon_pid = _wait_for_pid_file(pid_file)
        assert daemon_pid is not None, "the wrapper never started the daemon"
        assert _wait_until_running(daemon_pid), (
            "the daemon never settled into its loop; it left at startup"
        )

        wrapper.send_signal(signal.SIGKILL)
        wrapper.wait(timeout=DEADLINE)

        # It has to still be there at the end of the window the other
        # case allows for leaving: the window is what makes "still
        # alive" mean more than "alive for a moment".
        deadline = time.monotonic() + DEADLINE
        while time.monotonic() < deadline:
            assert _alive(daemon_pid), (
                "the daemon left even though it was told to survive "
                "its owner"
            )
            time.sleep(POLL_STEP)

        assert _alive(daemon_pid), "the daemon left although it was exempt"
    finally:
        _stop_pid(daemon_pid)
        _stop_process(wrapper)
        shutil.rmtree(root_base, ignore_errors=True)
