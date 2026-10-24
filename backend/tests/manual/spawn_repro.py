"""Reproduction core for the service-spawn failure.

Why this module exists
----------------------
The defect: a declared service, started the way
``backend/service_manager.py`` starts one, died immediately with
``OSError: [Errno 9] Bad file descriptor`` raised by its own event
loop.  The spawn happened from inside a long-lived, multi-threaded
backend process, so the shape of the spawn -- the shell, the session,
the input, the number of threads in the parent -- was the first thing
to suspect.

The shape production uses has changed since the defect was found: the
service is now started through a wrapper that takes the descriptor
variables out of the environment, calls ``setsid()`` and execs the real
command (``service_manager._detached_argv``).  This module follows
production wherever production goes -- the argv comes from production's
own builder -- so the reproducer cannot end up measuring a shape that
no longer exists.

Three parameters have been *proposed* as the cause, and none of them
has been shown to be causal:

``start_new_session``
    the child is put in a session of its own, with no controlling
    terminal;
``stdin``
    the child's stdin is ``/dev/null`` rather than an inherited handle;
``parent_threads``
    the backend is multi-threaded, and a spawn from a threaded parent
    is a different thing than a spawn from a single-threaded one.

This module is where those three are *switchable*.  One spawn shape,
three parameters, one place that decides how readiness is judged and
how the tree is torn down afterwards -- so the next question ("which
of them, if any, is causal?") is answered by measurement rather than
by argument.

What this module is, and is not
-------------------------------
It is a **core** -- the spawn, the readiness judgement, the cleanup,
the batch accounting -- and, on top of that, a **command line** that
drives those pieces and reports what they measured.  The behaviour of
the core is pinned by ``backend/tests/unit/test_spawn_repro_harness.py``;
the command line is pinned by ``backend/tests/unit/
test_spawn_repro_cli.py``.  Neither is a test file: the measurement
runs against a real service, on a real port, and takes real seconds.

The command line
----------------
::

    cd backend || exit 1
    PY=.venv/bin/python3
    $PY tests/manual/spawn_repro.py --profile raw --target pdt --runs 10 \
        --ready-timeout 8 --assert-failure-rate 0 \
        --json-out .pdt/spawn-repro/baseline.json

The invocation is written **relative to the checkout** on purpose.
``backend/.venv`` is a symlink to the one venv, so ``.venv/bin/python3``
reaches it from any checkout, and a path into one machine's home
directory is a shape this repository does not carry.  The child itself
is started with this process' own interpreter, resolved at run time
rather than written into the source.

Three things can be asked of it:

* measure one configuration and assert a failure rate
  (``--assert-failure-rate``);
* measure the whole candidate matrix -- every candidate with its
  parameter on and off -- and assert that exactly one of them moved
  the failure rate (``--matrix --assert-single-cause``);
* write the measurement to a JSON file (``--json-out``).

Exit codes: ``0`` the assertions held, ``1`` an assertion did not
hold, ``2`` the command line was wrong.

Two rules the module keeps
--------------------------
**The spawn mirrors production.**  The argument list and the keyword
arguments handed to :func:`subprocess.Popen` are the ones
``service_manager.start_service`` hands over, in the same shape, so
that a difference in outcome is a difference in the *candidate* and not
in the harness.  The harness pins that correspondence -- and the pin is
the only thing that notices when production's shape changes and this
module does not follow.

**A ready verdict means two things.**  The port answering is not
enough: a listener that outlived its own process would report a
service that is not serving.  Readiness is therefore "the port answers
*and* our child is still running".

Commands are written relative to the backend directory (``.venv/
bin/python3``) on purpose.  The project venv is reached that way from
every checkout, and an absolute path to one machine's home directory
is a shape this repository does not carry.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: The backend package root -- ``<repo>/backend``.  ``service_freshness``
#: lives there, and a module that cannot import it cannot judge
#: readiness, so the path is put on ``sys.path`` before that import.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from service_freshness import wait_for_ready  # noqa: E402  (path set above)
from service_declaration import ServiceDeclaration  # noqa: E402  (path set above)
import service_manager as sm  # noqa: E402  (path set above)


#: The project root -- ``<repo>``.  The ``pdt`` target runs the real
#: backend from here, because that is how it is run in production.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The tiny service the ``tiny`` target spawns.
_TINY_PATH = Path(__file__).resolve().parent / "tiny_service.py"

#: The service targets the command line can start.
TARGETS: Tuple[str, ...] = ("tiny", "pdt")

#: The spawn profiles the command line can use.  ``raw`` drives
#: :class:`SpawnConfig` directly, so a candidate can be switched;
#: ``service-manager`` goes through ``service_manager.start_service``,
#: so the production shape is followed whatever the candidate says.
PROFILES: Tuple[str, ...] = ("raw", "service-manager")

#: How far apart the "on" and the "off" failure rates have to be before
#: a candidate is called the cause.  A candidate that moves the rate by
#: less than this is noise at the sample sizes this tool runs.
CAUSAL_DELTA = 0.6


#: The candidate parameters this harness switches.  Every one of them is
#: a field of :class:`SpawnConfig`, so a candidate is "off" by writing
#: the production value into the config and "on" by writing the
#: alternative.
MATRIX_CANDIDATES: Tuple[str, ...] = (
    "start_new_session",
    "stdin",
    "parent_threads",
)

#: The candidates that are *not* about the spawn shape but about what the
#: spawn hands over: the environment, the inherited descriptors, and the
#: working directory.  They are a second set rather than more entries in
#: :data:`MATRIX_CANDIDATES` because the first three are the ones the
#: failure was supposed to be found in -- and a candidate that cannot move
#: the failure rate is evidence about the failure, not a failure to
#: measure.  When the first three come back flat, these are what the
#: next question is.
EXTRA_CANDIDATES: Tuple[str, ...] = (
    "clear_env",
    "close_fds",
    "cwd",
)

#: Every candidate this harness knows how to switch.
ALL_CANDIDATES: Tuple[str, ...] = MATRIX_CANDIDATES + EXTRA_CANDIDATES

#: The binary used to count a process' open descriptors.  Overridable so
#: that "``lsof`` is not installed here" is a thing a test can say out
#: loud instead of a thing it has to simulate with a mock.
LSOF_BIN = "lsof"

#: The call in production's wrapper that gives the child its own
#: session.  The matrix's session candidate takes it out; see
#: :func:`_detached_argv`.
_SESSION_CALL = "os.setsid()"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SpawnConfig:
    """One spawn shape.

    Every field that a candidate could toggle is a field here, so the
    matrix is a matter of writing configs rather than of editing code.
    """

    #: The command to run, *without* the shell wrapper -- the harness
    #: adds ``/bin/bash -lc`` itself, the way production does.
    start_cmd: str
    #: The port the service is expected to answer on.
    port: int
    #: The working directory the child is started in.
    cwd: Path
    #: Where the child's stdout and stderr are folded.
    log_path: Path
    #: How long to wait for the port to answer.
    ready_timeout: float = 8.0
    #: Candidate: whether the child gets a session of its own.
    #:
    #: The session is not a keyword argument handed to ``Popen``:
    #: production's wrapper calls ``setsid()`` itself, and a child that
    #: is already a group leader gets ``EPERM`` from its own
    #: ``setsid()``.  Turning the candidate off takes the ``setsid()``
    #: call out of production's own wrapper; see :func:`_detached_argv`.
    start_new_session: bool = True
    #: Candidate: ``devnull`` / ``inherit`` / ``pipe`` -- what the
    #: child's stdin is.
    stdin_mode: str = "devnull"
    #: Candidate: how many busy threads the parent runs while spawning.
    parent_threads: int = 0
    #: How many extra descriptors the parent holds open across the spawn.
    preopen_fds: int = 0
    #: Extra environment variables for the child.
    env_extra: Optional[Dict[str, str]] = None
    #: Candidate: whether the child inherits the parent's descriptors.
    #: ``True`` is what production does -- the default of
    #: :func:`subprocess.Popen`.  ``False`` hands the child every
    #: descriptor the parent holds, which is the opposite of the
    #: production shape and therefore the "on" side of the candidate.
    close_fds: bool = True
    #: Candidate: whether the child is given a clean environment
    #: instead of the parent's.  A clean environment is built from a
    #: short allowlist, so the child keeps the things a service needs
    #: to start (``PATH``, ``HOME``) and loses the rest -- including
    #: any variable that names a descriptor.
    clear_env: bool = False
    #: Candidate: the working directory to start the child in, when it
    #: is not the one the target asks for.  ``None`` means "the
    #: target's own directory".
    cwd_alt: Optional[Path] = None
    #: Builds the environment for *one* run.  When this is set, the run
    #: happens in a fresh scratch directory: the child's log is written
    #: there, this callable is handed that directory so it can point
    #: target-specific paths (a plans directory, a state database) at
    #: it, and the directory is removed when the run is over.  ``None``
    #: means the run happens where the config says, with no scratch.
    #:
    #: Why it exists: two runs that share a plans directory share its
    #: leftovers, and a leftover from a run that failed is exactly what
    #: makes the *next* run look broken.  The scratch directory is what
    #: keeps "this run failed" a statement about this run.
    scratch_env: Optional[Callable[[Path], Dict[str, str]]] = None

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd)
        self.log_path = Path(self.log_path)


@dataclass
class RunResult:
    """What one spawn did."""

    #: Whether the service came up -- port answering *and* child alive.
    ready: bool
    #: The child's pid.
    pid: Optional[int] = None
    #: The child's process group, when it could be determined.
    pgid: Optional[int] = None
    #: The child's exit code, sampled before the tree was torn down.
    rc: Optional[int] = None
    #: How many descriptors the child held open, when that could be
    #: counted.
    fd_count: Optional[int] = None
    #: How long the run took, in seconds.
    duration: float = 0.0
    #: The tail of the child's log.
    log_tail: str = ""


@dataclass
class BatchResult:
    """What a batch of spawns did."""

    #: How many spawns were attempted.
    runs: int = 0
    #: How many of them came up.
    ready: int = 0
    #: How many of them did not.
    failed: int = 0
    #: The individual results, in order.
    results: List[RunResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The spawn
# ---------------------------------------------------------------------------


def stdin_constant(mode: str):
    """The :mod:`subprocess` constant a ``stdin_mode`` names.

    ``devnull`` -> :data:`subprocess.DEVNULL`, ``inherit`` -> ``None``
    (the child keeps the parent's stdin), ``pipe`` ->
    :data:`subprocess.PIPE`.
    """
    if mode == "devnull":
        return subprocess.DEVNULL
    if mode == "inherit":
        return None
    if mode == "pipe":
        return subprocess.PIPE
    raise ValueError(
        f"unknown stdin_mode {mode!r}; expected 'devnull', 'inherit' or "
        f"'pipe'"
    )


#: The variables a service needs to start, and the only ones a cleared
#: environment keeps.  The list is short on purpose: a cleared
#: environment is a measurement, not a configuration -- it exists to
#: answer "does the child still fail when it inherits nothing from the
#: parent", so everything the child can start without is dropped.
_ENV_ALLOWLIST: Tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SHELL",
    "TERM",
    "USER",
    "LOGNAME",
    "TZ",
)


def _child_env(cfg: SpawnConfig) -> Dict[str, str]:
    """The environment the child is started with.

    Two shapes.  The production one is the parent's environment with the
    target's variables written over it -- which is what
    ``service_manager.start_service`` does.  The cleared one keeps only
    the variables in :data:`_ENV_ALLOWLIST`, so a variable that names
    something the child does not own (a descriptor, for instance) does
    not survive into the child.
    """
    if cfg.clear_env:
        env = {
            key: value for key, value in os.environ.items()
            if key in _ENV_ALLOWLIST
        }
    else:
        env = dict(os.environ)
    env.update(cfg.env_extra or {})
    return env


def _detached_argv(start_cmd: str, *, own_session: bool = True) -> List[str]:
    """The argv production starts a service with.

    The argv comes from ``service_manager._detached_argv`` -- the
    production builder itself, not a copy of it.  A copy is a second
    shape, and the second shape drifts: the drift stays invisible until
    a measurement means nothing, and the mirror pin is the only thing
    that notices it.

    The only thing this changes is the session.  With *own_session*
    false, the ``setsid()`` call is taken out of production's own
    wrapper, so the child stays in this process' session: that is the
    "on" side of the session candidate.  The stripping of the descriptor
    variables is left alone -- a cell that changed two things at once
    would not be measuring the candidate it is named after.
    """
    argv = list(sm._detached_argv(start_cmd))
    if own_session:
        return argv
    if len(argv) < 3 or argv[1] != "-c":
        raise ValueError(
            "production's argv is not the wrapper shape this harness "
            f"knows how to switch: {argv!r}"
        )
    lines = argv[2].splitlines(keepends=True)
    kept = [line for line in lines if _SESSION_CALL not in line]
    if len(kept) == len(lines):
        raise ValueError(
            f"production's wrapper no longer calls {_SESSION_CALL!r}; the "
            "session candidate cannot be measured without it"
        )
    argv[2] = "".join(kept)
    return argv


def spawn_child(cfg: SpawnConfig) -> subprocess.Popen:
    """Start the child exactly the way ``service_manager`` starts one.

    The argument list is production's own -- the wrapper that takes the
    descriptor variables out of the environment, makes the session and
    execs the real command -- and the keyword arguments are the
    production ones: same working directory, same environment shape,
    same log folding, same ``stdin``.

    The session is not among them: production's wrapper calls
    ``setsid()`` itself, so the child is already a group leader by the
    time ``Popen`` is asked for a session, and its own ``setsid()``
    would fail with ``EPERM``.
    """
    env = _child_env(cfg)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.log_path, "ab") as log_fp:
        return subprocess.Popen(
            _detached_argv(
                cfg.start_cmd, own_session=cfg.start_new_session,
            ),
            cwd=str(cfg.cwd_alt or cfg.cwd),
            env=env,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=stdin_constant(cfg.stdin_mode),
            close_fds=cfg.close_fds,
        )


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _await_ready(cfg: SpawnConfig, proc: subprocess.Popen) -> bool:
    """Whether the service came up.

    Two conditions, both required.  The port answering says a listener
    exists; the child still running says the listener is *ours*.  A
    child that died on the way up can leave a listener behind it -- and
    a port held by something else answers just as well -- so either
    condition alone would report a service that is not serving.
    """
    if not wait_for_ready(cfg.port, cfg.ready_timeout):
        return False
    return proc.poll() is None


def _process_group_of(pid: int) -> Optional[int]:
    """*pid*'s process group, or ``None`` when it cannot be determined.

    ``os.getpgid`` rather than a ``ps`` subprocess: the call happens
    immediately after the spawn, and a subprocess would race the
    scheduler.
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return None
    return pgid


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def _own_pgid() -> int:
    """This process' process group, or ``-1`` when it cannot be read."""
    try:
        return os.getpgid(0)
    except OSError:
        return -1


def _signalable_pgid(pgid: object) -> Optional[int]:
    """*pgid* if it names a group this process may signal, else ``None``.

    Two refusals, and both of them have bitten this repository before:

    ``0`` and ``1``
        POSIX sentinels -- ``kill(0, sig)`` is the caller's own group
        and ``kill(-1, sig)`` is a broadcast.
    **the caller's own group**
        A child started without ``start_new_session`` shares it.
        Signalling that group would signal the caller, and the caller
        here is a test session.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return None
    if pgid == _own_pgid():
        return None
    return pgid


def _pid_alive(pid: object) -> bool:
    """Whether *pid* still names a process."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: object) -> bool:
    """Whether *pgid* still names a process group."""
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group that exists but cannot be signalled still exists --
        # on some systems a group whose only member is a zombie
        # reports this way rather than "no such process".
        return True
    return True


def _signal(target: Optional[int], pid: Optional[int], sig: int) -> None:
    """Send *sig* to the group, or to the pid alone when there is no group.

    A process that has already gone is the outcome that was wanted, so
    ``ProcessLookupError`` is swallowed.  ``PermissionError`` is
    swallowed too, and the pid is tried afterwards -- the group may
    refuse the signal while the process itself accepts it.
    """
    if target is not None:
        try:
            os.killpg(target, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _reap(pid: object) -> bool:
    """Reap *pid* when it is this process' own child and has exited.

    Returns whether the child was reaped.  A child that has exited but
    has not been reaped is still a member of its process group, and a
    group that still has a member cannot be observed as "gone" -- so
    without this the wait for the tree to disappear would always run
    to its full timeout.

    Reaping consumes the exit status.  A caller that still needs the
    status should pass the :class:`subprocess.Popen` to
    :func:`stop_tree` instead, which reaps through it and keeps the
    status.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return waited == pid


def _wait_for_exit(
    pid: Optional[int],
    target: Optional[int],
    timeout: float,
    *,
    proc: Optional[subprocess.Popen] = None,
) -> bool:
    """Wait up to *timeout* seconds for the tree to disappear.

    Returns whether it did.  When a :class:`subprocess.Popen` is
    supplied it is polled, so the child is reaped through the object
    that owns it and its exit status stays readable.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if proc is not None:
            proc.poll()
        elif pid is not None:
            _reap(pid)
        if not _pid_alive(pid) and not _group_alive(target):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def stop_tree(
    pid: Optional[int],
    pgid: Optional[int],
    grace: float = 3.0,
    *,
    proc: Optional[subprocess.Popen] = None,
) -> None:
    """Terminate the child's process group and wait for it to disappear.

    ``SIGTERM`` first; if the tree is still there after *grace*
    seconds, ``SIGKILL``.  Both are followed by a wait, so that
    "stopped" means stopped rather than "signalled and hoped for".

    ``ProcessLookupError`` is swallowed throughout: a process that has
    already gone is what was wanted.

    Passing *proc* keeps the child's exit status readable -- the
    reaping then happens through the object that owns it.  Without it
    the child is reaped directly, which consumes the status.
    """
    target = _signalable_pgid(pgid)
    _signal(target, pid, signal.SIGTERM)
    if _wait_for_exit(pid, target, grace, proc=proc):
        return
    _signal(target, pid, signal.SIGKILL)
    _wait_for_exit(pid, target, min(grace, 1.0), proc=proc)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def count_fds(pid: int) -> Optional[int]:
    """How many descriptors *pid* holds open, or ``None``.

    ``None`` means "not measurable here", not "zero": ``lsof`` may not
    be installed, may not be permitted to look at that process, or may
    have failed.  A caller that turned that into ``0`` would report a
    descriptor exhaustion that was never observed.
    """
    try:
        completed = subprocess.run(
            [LSOF_BIN, "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    # The first line is the header.
    return len(lines) - 1


def _tail(path: Path, lines: int = 15) -> str:
    """The last *lines* lines of *path*, joined for a one-line report."""
    try:
        content = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(content[-lines:])


# ---------------------------------------------------------------------------
# The parent-side pressure
# ---------------------------------------------------------------------------


def _start_busy_threads(count: int) -> Tuple[Optional[threading.Event], List[threading.Thread]]:
    """Start *count* threads that spin, to model a threaded parent.

    Returns the event that stops them and the threads themselves.
    """
    if count <= 0:
        return None, []
    stop = threading.Event()

    def _spin() -> None:
        while not stop.is_set():
            for _ in range(2000):
                pass

    threads: List[threading.Thread] = []
    for index in range(count):
        thread = threading.Thread(
            target=_spin, name=f"spawn-repro-busy-{index}", daemon=True,
        )
        thread.start()
        threads.append(thread)
    return stop, threads


def _stop_busy_threads(
    stop: Optional[threading.Event], threads: Sequence[threading.Thread],
) -> None:
    """Stop the spinning threads and join them."""
    if stop is not None:
        stop.set()
    for thread in threads:
        thread.join(timeout=2.0)


def _preopen_fds(count: int) -> List[int]:
    """Open *count* extra descriptors in this process before the spawn."""
    if count <= 0:
        return []
    try:
        base = os.open(os.devnull, os.O_RDONLY)
    except OSError:
        return []
    opened: List[int] = []
    try:
        for _ in range(count):
            try:
                opened.append(os.dup(base))
            except OSError:
                break
    finally:
        os.close(base)
    return opened


def _close_fds(fds: Sequence[int]) -> None:
    """Close descriptors opened by :func:`_preopen_fds`."""
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# One run, and a batch of them
# ---------------------------------------------------------------------------


def run_once(cfg: SpawnConfig) -> RunResult:
    """Spawn one child under *cfg*, judge it, and tear it down.

    When the config carries a ``scratch_env``, the run happens in a
    directory of its own: the child's log goes there, the callable is
    handed that directory to point target-specific paths at it, and the
    directory is removed before this returns.  The log tail is read
    before the removal, so a scratch run still says why it failed.
    """
    if cfg.scratch_env is None:
        return _spawn_once(cfg)

    scratch = Path(tempfile.mkdtemp(prefix="pdt-spawn-repro-"))
    try:
        env = dict(cfg.env_extra or {})
        env.update(cfg.scratch_env(scratch))
        run_cfg = replace(
            cfg, log_path=scratch / "svc.log", env_extra=env,
        )
        return _spawn_once(run_cfg)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _spawn_once(cfg: SpawnConfig) -> RunResult:
    """The spawn itself, without the scratch-directory bookkeeping.

    A spawn that fails outright is reported as a failed run rather than
    raised: a batch that stops at the first exception measures nothing.
    The reason goes into the log tail, which is where a reader looks.
    """
    started = time.monotonic()
    stop_flag, threads = _start_busy_threads(cfg.parent_threads)
    extra_fds = _preopen_fds(cfg.preopen_fds)
    proc: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    pgid: Optional[int] = None
    ready = False
    rc: Optional[int] = None
    fd_count: Optional[int] = None
    failure = ""
    try:
        proc = spawn_child(cfg)
        pid = proc.pid
        pgid = _process_group_of(pid)
        ready = _await_ready(cfg, proc)
        # Sampled again after the readiness wait.  The wrapper calls
        # ``setsid()`` itself, so the child's own process group only
        # exists a moment after the spawn -- a sample taken that early
        # names this process' group, and a teardown that signalled
        # that group would signal the caller.
        later_pgid = _process_group_of(pid)
        if later_pgid is not None and later_pgid != _own_pgid():
            pgid = later_pgid
        # Sampled before the teardown: after it, the exit code would be
        # the one the teardown produced, not the one the child did.
        rc = proc.poll()
        fd_count = count_fds(pid)
    except Exception as exc:  # noqa: BLE001 - a batch must not stop here
        failure = f"spawn failed: {type(exc).__name__}: {exc}"
    finally:
        if proc is not None:
            stop_tree(pid, pgid, proc=proc)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        _close_fds(extra_fds)
        _stop_busy_threads(stop_flag, threads)
        duration = time.monotonic() - started
    log_tail = failure or _tail(cfg.log_path)
    return RunResult(
        ready=ready,
        pid=pid,
        pgid=pgid,
        rc=rc,
        fd_count=fd_count,
        duration=duration,
        log_tail=log_tail,
    )


def run_batch(cfg: SpawnConfig, runs: int) -> BatchResult:
    """Run *cfg* *runs* times and count the outcomes."""
    results = [run_once(cfg) for _ in range(max(int(runs), 0))]
    ready = sum(1 for result in results if result.ready)
    return BatchResult(
        runs=len(results),
        ready=ready,
        failed=len(results) - ready,
        results=results,
    )


# ---------------------------------------------------------------------------
# Running the matrix
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _default_config(port: int, log_dir: Path) -> SpawnConfig:
    """The production spawn shape, with a stdlib HTTP server for a port.

    The server is ``http.server`` from the standard library rather than
    something this repository has to ship: the point of the harness is
    the *spawn*, not the service.
    """
    return SpawnConfig(
        start_cmd=(
            f'.venv/bin/python3 -m http.server {port} --bind 127.0.0.1'
        ),
        port=port,
        cwd=_BACKEND_DIR,
        log_path=log_dir / "svc.log",
        ready_timeout=8.0,
        start_new_session=True,
        stdin_mode="devnull",
        parent_threads=0,
        preopen_fds=0,
    )


def _variant(base: SpawnConfig, candidate: str) -> SpawnConfig:
    """*base* with one candidate switched to its alternative."""
    if candidate == "start_new_session":
        return replace(base, start_new_session=False)
    if candidate == "stdin":
        return replace(base, stdin_mode="inherit")
    if candidate == "parent_threads":
        return replace(base, parent_threads=8)
    if candidate == "clear_env":
        return replace(base, clear_env=True)
    if candidate == "close_fds":
        return replace(base, close_fds=False)
    if candidate == "cwd":
        # The command has to keep working from the other directory, so
        # the project root goes on ``PYTHONPATH`` and the working
        # directory is the only thing that differs.
        env_extra = dict(base.env_extra or {})
        env_extra["PYTHONPATH"] = str(_PROJECT_ROOT)
        return replace(base, cwd_alt=_BACKEND_DIR, env_extra=env_extra)
    raise ValueError(f"unknown candidate {candidate!r}")


# ---------------------------------------------------------------------------
# The targets
# ---------------------------------------------------------------------------


def _target_env(port: int, root: Path) -> Dict[str, str]:
    """The environment the real backend reads its wiring from."""
    root = Path(root)
    return {
        "PDT_PORT": str(port),
        "PDT_PLANS_DIR": str(root / "plans"),
        "PDT_STATE_DB_PATH": str(root / "state.db"),
    }


def resolve_target(name: str, port: int, tmpdir: Path) -> SpawnConfig:
    """The spawn config for the service target *name*.

    Two targets, and they differ in more than the command: the tiny
    service takes its port on the command line, while the real backend
    reads its port, its plans directory and its state database from the
    environment.  Both are given a scratch directory of their own for
    every run, so that a run never inherits state from the run before
    it -- a leftover from a run that failed is exactly what makes the
    *next* run look broken.

    The interpreter is this process' own -- the one the tool was started
    with.  It is resolved at run time rather than written into the
    source, because a path into one machine's home directory is a shape
    this repository does not carry.
    """
    tmpdir = Path(tmpdir)
    if name == "tiny":
        return SpawnConfig(
            start_cmd=f"{sys.executable} {_TINY_PATH} --port {port}",
            port=port,
            cwd=_BACKEND_DIR,
            log_path=tmpdir / "svc.log",
            env_extra={},
            scratch_env=lambda scratch: {},
        )
    if name == "pdt":
        return SpawnConfig(
            start_cmd=f"{sys.executable} -m backend.server",
            port=port,
            cwd=_PROJECT_ROOT,
            log_path=tmpdir / "svc.log",
            env_extra=_target_env(port, tmpdir),
            scratch_env=lambda scratch: _target_env(port, scratch),
        )
    raise ValueError(
        f"unknown target {name!r}; expected one of {', '.join(TARGETS)}"
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def _cell(batch: BatchResult) -> Dict[str, Any]:
    """One matrix cell: how many runs, and how many of them failed."""
    return {
        "runs": batch.runs,
        "ready": batch.ready,
        "failed": batch.failed,
        "failure_rate": _rate(batch.failed, batch.runs),
    }


def run_matrix(
    runs_per_cell: int = 5,
    cfg_base: Optional[SpawnConfig] = None,
    candidates: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Measure every candidate with its parameter on and off.

    Each candidate gets two cells: the parameter at its production
    value ("off") and at its alternative ("on").  The candidate whose
    two failure rates differ by at least :data:`CAUSAL_DELTA` is the
    one the failure follows; the others are noise at the sample sizes
    this tool runs.

    Three candidates, two cells each: six batches, and the "off" cells
    are all the same production shape measured three times.  That
    repetition is deliberate -- it is the baseline the "on" cells are
    compared against, and a baseline measured once cannot be compared
    against three different things.

    *candidates* says which ones to measure; it defaults to
    :data:`MATRIX_CANDIDATES`.  The second set -- the ones that are
    about what the spawn hands over rather than about its shape -- is
    measured by naming them here, so a measurement of one set never
    quietly becomes a measurement of the other.
    """
    base = cfg_base if cfg_base is not None else _default_config(
        _free_port(), Path(tempfile.gettempdir()) / "pdt_spawn_repro",
    )
    matrix: Dict[str, Dict[str, Any]] = {}
    for candidate in (candidates if candidates is not None else MATRIX_CANDIDATES):
        on = _cell(run_batch(_variant(base, candidate), runs_per_cell))
        off = _cell(run_batch(base, runs_per_cell))
        delta = abs(on["failure_rate"] - off["failure_rate"])
        matrix[candidate] = {
            "on": on,
            "off": off,
            "verdict": "因果" if delta >= CAUSAL_DELTA else "无关",
        }
    return matrix


# ---------------------------------------------------------------------------
# The production profile
# ---------------------------------------------------------------------------


def _declaration(cfg: SpawnConfig, ready_timeout: float) -> ServiceDeclaration:
    """The service declaration for a config, for the service manager."""
    return ServiceDeclaration(
        name="repro",
        port=cfg.port,
        start_cmd=cfg.start_cmd,
        cwd=str(cfg.cwd),
        health_url="",
        ready_timeout_seconds=max(int(round(ready_timeout)), 1),
        log_path=str(cfg.log_path),
    )


@contextlib.contextmanager
def _env_override(**values: str):
    """Set environment variables for the duration of the block.

    The service manager builds the child's environment from
    ``os.environ`` and has nowhere else to put a target's wiring, so
    this is how the child's environment is shaped.  The previous values
    are put back afterwards, because a measurement that leaks
    ``PDT_PORT`` into the rest of the session is a measurement that
    changes what runs next.
    """
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def run_batch_service_manager(
    decl: ServiceDeclaration,
    project_dir: Path,
    runs: int,
    timeout_seconds: float,
    env_for_run: Optional[Callable[[Path], Dict[str, str]]] = None,
) -> BatchResult:
    """Run *runs* spawns through ``service_manager.start_service``.

    The production shape, exactly: the declaration is handed to the
    service manager, which decides how the child is started.  A
    candidate parameter cannot be switched through this path -- that is
    the point of the profile, and the reason the matrix refuses it.

    Every run still gets a scratch directory of its own, and the
    service is stopped afterwards: the service manager leaves a
    long-lived server running on purpose, and a reproduction that
    leaves ten of them behind is a reproduction that cannot be run
    twice.
    """
    results: List[RunResult] = []
    for _ in range(max(int(runs), 0)):
        scratch = Path(tempfile.mkdtemp(prefix="pdt-spawn-repro-"))
        started = time.monotonic()
        try:
            run_decl = replace(decl, log_path=str(scratch / "svc.log"))
            env = dict(env_for_run(scratch)) if env_for_run else {}
            with _env_override(**env):
                runtime = sm.start_service(
                    run_decl, project_dir,
                    timeout_seconds=max(int(round(timeout_seconds)), 1),
                )
            try:
                results.append(RunResult(
                    ready=bool(runtime.ready),
                    pid=runtime.pid,
                    pgid=runtime.pgid,
                    rc=None,
                    duration=time.monotonic() - started,
                    log_tail=runtime.detail or "",
                ))
            finally:
                stop_tree(runtime.pid, runtime.pgid)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    ready = sum(1 for result in results if result.ready)
    return BatchResult(
        runs=len(results),
        ready=ready,
        failed=len(results) - ready,
        results=results,
    )


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """The command line's arguments.

    The defaults are the ones a manual run uses: the raw profile, the
    tiny target, ten spawns, and an eight-second readiness budget.
    """
    parser = argparse.ArgumentParser(
        prog="spawn_repro.py",
        description=(
            "Reproduce the service-spawn failure: measure a failure "
            "rate for one configuration, or the whole candidate matrix."
        ),
    )
    parser.add_argument(
        "--profile", choices=list(PROFILES), default="raw",
        help="how the child is started: 'raw' drives SpawnConfig "
             "directly, 'service-manager' goes through "
             "service_manager.start_service (default: raw)",
    )
    parser.add_argument(
        "--target", choices=list(TARGETS), default="tiny",
        help="which service to start (default: tiny)",
    )
    parser.add_argument(
        "--runs", type=int, default=10,
        help="spawns per configuration (default: 10)",
    )
    parser.add_argument(
        "--ready-timeout", type=float, default=8.0,
        help="seconds to wait for the port to answer (default: 8)",
    )
    parser.add_argument(
        "--assert-failure-rate", type=float, default=None,
        help="fail unless the measured failure rate reaches this; 0 "
             "means no failure may happen at all",
    )
    parser.add_argument(
        "--assert-single-cause", action="store_true",
        help="fail unless exactly one candidate moved the failure rate "
             "(needs --matrix)",
    )
    parser.add_argument(
        "--matrix", action="store_true",
        help="measure every candidate with its parameter on and off "
             "(needs --profile raw: the service manager does not let a "
             "candidate be switched)",
    )
    parser.add_argument(
        "--runs-per-cell", type=int, default=5,
        help="spawns per matrix cell (default: 5)",
    )
    parser.add_argument(
        "--candidates",
        default=",".join(MATRIX_CANDIDATES),
        help="comma-separated candidates for --matrix (default: %s; "
             "the other set is %s)"
             % (",".join(MATRIX_CANDIDATES), ",".join(EXTRA_CANDIDATES)),
    )
    parser.add_argument(
        "--json-out",
        default=str(Path(".pdt") / "spawn-repro" / "last.json"),
        help="where the measurement is written (default: "
             ".pdt/spawn-repro/last.json)",
    )
    return parser.parse_args(argv)


def _rate(failed: int, runs: int) -> float:
    """The failure rate of *runs* runs, of which *failed* failed."""
    return (failed / runs) if runs else 0.0


def _failure_rate_holds(failed: int, runs: int, threshold: float) -> bool:
    """Whether the measured failure rate satisfies the assertion.

    A threshold of zero is not "no threshold" -- it is the strongest
    assertion there is: not one spawn may fail.
    """
    if threshold > 0:
        return _rate(failed, runs) >= threshold
    return failed == 0


def _result_json(result: RunResult) -> Dict[str, Any]:
    """One run, as it is written to the measurement file."""
    return {
        "ready": result.ready,
        "pid": result.pid,
        "pgid": result.pgid,
        "rc": result.rc,
        "fd_count": result.fd_count,
        "duration": result.duration,
        "log_tail": result.log_tail,
    }


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    """Write the measurement, so it can be looked at after the fact."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _now() -> str:
    """When the measurement was taken."""
    return datetime.now(timezone.utc).isoformat()


def _run_single_mode(args: argparse.Namespace, port: int, tmpdir: Path) -> int:
    """Measure one configuration and judge the failure-rate assertion."""
    cfg = resolve_target(args.target, port, tmpdir)
    cfg = replace(cfg, ready_timeout=args.ready_timeout)

    if args.profile == "raw":
        batch = run_batch(cfg, args.runs)
    else:
        batch = run_batch_service_manager(
            _declaration(cfg, args.ready_timeout),
            cfg.cwd,
            args.runs,
            args.ready_timeout,
            env_for_run=cfg.scratch_env,
        )

    rate = _rate(batch.failed, batch.runs)
    print(
        f"profile={args.profile} target={args.target} runs={batch.runs} "
        f"ready={batch.ready} failed={batch.failed} "
        f"failure_rate={rate:.3f}"
    )
    for result in batch.results:
        print(
            f"    ready={result.ready} pid={result.pid} "
            f"rc={result.rc} fds={result.fd_count} "
            f"tail={result.log_tail[:120]!r}"
        )

    assertions: Dict[str, Any] = {}
    exit_code = 0
    if args.assert_failure_rate is not None:
        ok = _failure_rate_holds(
            batch.failed, batch.runs, args.assert_failure_rate,
        )
        assertions["failure_rate"] = {
            "expected": args.assert_failure_rate,
            "observed": rate,
            "failed": batch.failed,
            "runs": batch.runs,
            "ok": ok,
        }
        if not ok:
            exit_code = 1
            print(
                f"failure rate {rate:.3f} does not meet the assertion "
                f"({args.assert_failure_rate})",
                file=sys.stderr,
            )

    _write_json(args.json_out, {
        "generated_at": _now(),
        "mode": "single",
        "profile": args.profile,
        "target": args.target,
        "runs": batch.runs,
        "ready": batch.ready,
        "failed": batch.failed,
        "failure_rate": rate,
        "assertions": assertions,
        "results": [_result_json(result) for result in batch.results],
    })
    return exit_code


def _run_matrix_mode(args: argparse.Namespace, port: int, tmpdir: Path) -> int:
    """Measure every candidate on and off, and judge the assertions."""
    candidates = [
        name.strip() for name in args.candidates.split(",") if name.strip()
    ]
    unknown = [name for name in candidates if name not in ALL_CANDIDATES]
    if unknown:
        print(
            f"unknown candidate(s): {', '.join(unknown)}; known candidates "
            f"are {', '.join(ALL_CANDIDATES)}",
            file=sys.stderr,
        )
        return 2
    if not candidates:
        print("--candidates named no candidate", file=sys.stderr)
        return 2

    base = resolve_target(args.target, port, tmpdir)
    base = replace(base, ready_timeout=args.ready_timeout)
    matrix = run_matrix(args.runs_per_cell, base, candidates)

    print("| 候选 | 开 | 关 | 结论 |")
    for candidate, cell in matrix.items():
        print(
            f"| {candidate} "
            f"| {cell['on']['failed']}/{cell['on']['runs']} "
            f"| {cell['off']['failed']}/{cell['off']['runs']} "
            f"| {cell['verdict']} |"
        )

    assertions: Dict[str, Any] = {}
    exit_code = 0

    if args.assert_failure_rate is not None:
        # The baseline is the production shape.  Every candidate's
        # "off" cell is that same shape measured again, so the first
        # one is the baseline.
        baseline = matrix[candidates[0]]["off"]
        ok = _failure_rate_holds(
            baseline["failed"], baseline["runs"], args.assert_failure_rate,
        )
        assertions["failure_rate"] = {
            "expected": args.assert_failure_rate,
            "observed": baseline["failure_rate"],
            "failed": baseline["failed"],
            "runs": baseline["runs"],
            "ok": ok,
        }
        if not ok:
            exit_code = 1
            print(
                f"baseline failure rate {baseline['failure_rate']:.3f} does "
                f"not meet the assertion ({args.assert_failure_rate})",
                file=sys.stderr,
            )

    if args.assert_single_cause:
        significant = [
            candidate for candidate, cell in matrix.items()
            if cell["verdict"] == "因果"
        ]
        ok = len(significant) == 1
        assertions["single_cause"] = {
            "significant": significant,
            "ok": ok,
        }
        if not ok:
            exit_code = 1
            if len(significant) > 1:
                print(
                    "more than one candidate moved the failure rate "
                    f"({', '.join(significant)}); narrow the sample before "
                    "claiming a cause",
                    file=sys.stderr,
                )
            else:
                print(
                    "no candidate moved the failure rate; the cause is not "
                    "in the candidate set",
                    file=sys.stderr,
                )

    _write_json(args.json_out, {
        "generated_at": _now(),
        "mode": "matrix",
        "profile": args.profile,
        "target": args.target,
        "runs_per_cell": args.runs_per_cell,
        "candidates": candidates,
        "assertions": assertions,
        "matrix": matrix,
    })
    return exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Measure, assert, and report.

    Exit codes: ``0`` the assertions held, ``1`` an assertion did not
    hold, ``2`` the command line was wrong.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        # argparse has already said what was wrong.
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2

    if args.assert_single_cause and not args.matrix:
        print(
            "--assert-single-cause needs --matrix: without it there is "
            "no candidate table to judge",
            file=sys.stderr,
        )
        return 2
    if args.matrix and args.profile != "raw":
        print(
            "--matrix needs --profile raw: the service manager does not "
            "let a candidate be switched",
            file=sys.stderr,
        )
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="pdt-spawn-repro-"))
    try:
        port = sm.allocate_free_port()
        if port is None:
            print("could not allocate a free port", file=sys.stderr)
            return 2
        if args.matrix:
            return _run_matrix_mode(args, port, tmpdir)
        return _run_single_mode(args, port, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
