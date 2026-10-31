"""The kernel identity of a PID — the check that stops a kill landing wrong.

PIDs are recycled. A ledger that says "service X is pid 4242" is only
true until the kernel hands 4242 to something else, and a sweep that
signals 4242 on the strength of that record alone murders an unrelated
process. The number is not the identity: the *start time* is, because
two processes that never shared a life cannot share a start time.

This module reads that identity and compares it against what was
recorded, so the caller can refuse to signal on a mismatch:

    ident = probe(4242)
    if ident is None or not ident.matches(
        create_time=recorded.create_time, cmdline=recorded.cmdline
    ):
        refuse_to_signal()

``psutil`` is the primary source because it exposes the start time with
sub-second precision. It is optional, though — it is a dev dependency,
not a runtime one — so there is a ``ps`` fallback behind it. ``ps`` only
resolves the start time to the second, which is why the comparison
tolerates two seconds rather than demanding equality.

Two pids are never matchable: pid 1 (``init``) and this process. They
are excluded here rather than by refusing to signal, so the decision
stays with the caller — this module reports what the kernel says, it
does not authorise anything.

Two limits belong to the sources, and the caller has to know about both.

* ``ps`` reports the command line as one string with the quoting
  already lost, so the fallback splits it on whitespace. The
  executable and its first argument survive that; an argument that
  itself contained spaces cannot be recovered. The fallback is a
  degraded view of the process, not an exact one.
* A process may rewrite its own ``argv[0]`` shortly after it starts —
  on macOS the framework launcher re-executes the interpreter and
  replaces ``argv[0]`` with the resolved path. A command line
  recorded right after a spawn can therefore differ from the same
  command line read a moment later. ``matches`` compares the first
  two entries, so a rewritten ``argv[0]`` reads as a mismatch, which
  is the safe direction: the caller refuses to signal.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)


#: How far apart two start times may be and still name the same process,
#: in seconds. ``ps -o lstart=`` has one-second granularity and the two
#: readings can straddle a tick, so the same process can be reported up
#: to a second apart. Two seconds is the bound
#: ``service_manager._pid_reused`` already uses, so the two subsystems
#: agree on what "recycled" means.
CREATE_TIME_TOLERANCE = 2.0


#: How many leading command-line entries identify a process. The
#: executable and its first argument are the stable part; the rest of
#: the line is whatever the process chose to put there.
CMDLINE_PREFIX_LEN = 2


#: The ``ps`` format the fallback reads. ``lstart`` is the start time in
#: ``Thu Sep  4 09:32:01 2026`` form; ``command`` is the full command
#: line; ``pgid`` is the process group, which is what a kill has to
#: target when the process has children of its own.
_PS_FORMAT = "pid=,pgid=,lstart=,command="


#: The ``lstart`` spellings ``ps`` emits. The day is space-padded
#: ("Sep  4"), which is why both ``%d`` and ``%e`` are tried.
_LSTART_FORMATS = (
    "%a %b %d %H:%M:%S %Y",
    "%a %b %e %H:%M:%S %Y",
    "%Y-%m-%d %H:%M:%S",
)


#: ``psutil`` is optional. The handle is module-level so that a caller
#: (or a test) can clear it and force the ``ps`` fallback.
psutil = None
try:
    import psutil  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - the fallback covers this case
    psutil = None


@dataclass(frozen=True)
class ProcessIdentity:
    """What the kernel says about one PID.

    Attributes:
        pid: The process id that was asked about.
        create_time: Epoch seconds at which the process started, or
            ``None`` when the source could not supply it.
        cmdline: The command line, as the kernel reports it.
        pgid: The process group id, or ``None`` when unknown.
        cwd: The working directory, or ``None`` when the source cannot
            report it (``ps`` cannot).
    """

    pid: int
    create_time: Optional[float] = None
    cmdline: Tuple[str, ...] = ()
    pgid: Optional[int] = None
    cwd: Optional[str] = None

    def __post_init__(self) -> None:
        cmdline: Any = self.cmdline
        if cmdline is None:
            cmdline = ()
        elif isinstance(cmdline, str):
            cmdline = (cmdline,)
        object.__setattr__(self, "cmdline", tuple(str(part) for part in cmdline))
        if self.create_time is not None:
            object.__setattr__(self, "create_time", float(self.create_time))

    def matches(
        self,
        *,
        create_time: Optional[float],
        cmdline: Optional[Sequence[str]],
        tolerance: float = CREATE_TIME_TOLERANCE,
    ) -> bool:
        """True when this identity is the process the caller recorded.

        See :func:`matches` for the rules.
        """
        return matches(
            self, create_time=create_time, cmdline=cmdline, tolerance=tolerance
        )


def probe(pid: int) -> Optional[ProcessIdentity]:
    """Read the kernel identity of ``pid``.

    Returns ``None`` when the pid names no process, or when no source
    could describe it. ``None`` is the answer that stops a kill: a pid
    with no readable identity cannot be shown to be the recorded one.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    module = _resolve_psutil()
    if module is not None:
        try:
            return _probe_with_psutil(module, pid)
        except _no_such_process(module):
            # The pid is gone. Asking ``ps`` would only repeat the
            # answer, and a recycled pid must not be reported as ours.
            return None
        except Exception as exc:  # noqa: BLE001 - a probe is best-effort
            logger.debug(
                "[process_identity] psutil probe failed for pid=%s: %s", pid, exc
            )
    return _probe_with_ps(pid)


def alive(pid: int) -> bool:
    """True when ``pid`` still names a running process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return probe(pid) is not None


def matches(
    identity: Optional[ProcessIdentity],
    *,
    create_time: Optional[float],
    cmdline: Optional[Sequence[str]],
    tolerance: float = CREATE_TIME_TOLERANCE,
) -> bool:
    """True when ``identity`` is the process the caller recorded.

    The rules, in the order they are applied:

    * no identity, no match — an unreadable pid is never a match;
    * pid 1 and this process never match — the caller decides whether
      to signal, and these are never the intended target;
    * both sides must have a start time, and they must be within
      ``tolerance`` seconds — a pid that has been recycled reports a
      start time from a different life;
    * the command lines must be the same length and their first two
      entries must agree — empty entries are padding and are ignored.

    Every check can only *prevent* a match. There is no reading of the
    arguments that makes a mismatched identity match.
    """
    if identity is None:
        return False

    pid = identity.pid
    if pid is None or pid <= 1:
        return False
    if pid == os.getpid():
        return False

    if identity.create_time is None or create_time is None:
        return False
    if abs(float(identity.create_time) - float(create_time)) > float(tolerance):
        return False

    return _cmdline_matches(identity.cmdline, cmdline)


# ---------------------------------------------------------------------------
# psutil
# ---------------------------------------------------------------------------

def _resolve_psutil() -> Optional[Any]:
    """Return the ``psutil`` module to use, or ``None`` when there is none.

    ``psutil`` is optional, so "not available" has to be a state this
    module can be in. It gets there three ways, and all three land on
    the ``ps`` fallback:

    * the import at module scope failed — psutil is not installed;
    * the module-level handle was cleared to simulate that;
    * ``sys.modules["psutil"]`` was poisoned, which is how a test makes
      a fresh import fail without uninstalling the package.
    """
    global psutil
    if psutil is None:
        return None

    installed = sys.modules.get("psutil")
    if installed is None:
        return None
    if installed is not psutil:
        # A caller swapped in a stub; the stub is the truth here.
        return psutil

    try:
        import psutil as fresh  # noqa: PLC0415 - deliberate late import
    except Exception:  # noqa: BLE001 - any import failure means "no psutil"
        return None
    return fresh


def _no_such_process(module: Any) -> tuple:
    """The exception class ``module`` raises for a vanished process.

    An empty tuple matches nothing, so a module without the attribute
    simply never takes that branch.
    """
    candidate = getattr(module, "NoSuchProcess", None)
    if candidate is None:
        return ()
    if isinstance(candidate, tuple):
        return candidate
    return (candidate,)


def _probe_with_psutil(module: Any, pid: int) -> ProcessIdentity:
    """Read the identity of ``pid`` through ``psutil``."""
    proc = module.Process(pid)
    create_time = float(proc.create_time())
    cmdline = tuple(str(part) for part in (proc.cmdline() or ()))

    try:
        pgid: Optional[int] = os.getpgid(pid)
    except OSError:
        pgid = None

    try:
        cwd: Optional[str] = proc.cwd()
    except Exception:  # noqa: BLE001 - cwd is a nicety, not the identity
        cwd = None

    return ProcessIdentity(
        pid=pid, create_time=create_time, cmdline=cmdline, pgid=pgid, cwd=cwd
    )


# ---------------------------------------------------------------------------
# the ps fallback
# ---------------------------------------------------------------------------

def _probe_with_ps(pid: int) -> Optional[ProcessIdentity]:
    """Read the identity of ``pid`` through ``ps``."""
    raw = _run_ps(pid)
    if not raw:
        return None
    return _parse_ps_identity(raw, pid)


def _run_ps(pid: int) -> Optional[str]:
    """Ask ``ps`` about one pid. Never raises."""
    try:
        completed = subprocess.run(
            ["ps", "-o", _PS_FORMAT, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - a probe is best-effort
        logger.debug("[process_identity] ps failed for pid=%s: %s", pid, exc)
        return None

    if isinstance(completed, str):
        output = completed
    else:
        output = getattr(completed, "stdout", "") or ""
    return str(output).strip() or None


def _parse_ps_identity(raw: str, pid: int) -> Optional[ProcessIdentity]:
    """Parse one ``ps -o pid=,pgid=,lstart=,command=`` line.

    The line is ``pid``, ``pgid``, the five whitespace-separated tokens
    of ``lstart``, then the command line. The command is taken as the
    remainder of the line, so its internal spacing survives.
    """
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split(None, 7)
        if len(parts) < 7:
            continue
        try:
            int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue

        lstart = " ".join(parts[2:7])
        command = parts[7] if len(parts) > 7 else ""

        return ProcessIdentity(
            pid=pid,
            create_time=_parse_ps_lstart(lstart),
            cmdline=tuple(command.split()),
            pgid=pgid,
            cwd=None,  # ps cannot report the working directory
        )
    return None


def _parse_ps_lstart(raw: str) -> Optional[float]:
    """Parse a ``ps -o lstart=`` reading into epoch seconds."""
    raw = " ".join(raw.split())
    if not raw:
        return None
    for fmt in _LSTART_FORMATS:
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# command-line comparison
# ---------------------------------------------------------------------------

def _normalize_cmdline(cmdline: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """The command line as a tuple, without the empty entries.

    An empty entry is padding — some launchers leave one behind — so it
    carries no identity and is ignored on both sides of the comparison.
    """
    if cmdline is None:
        return ()
    if isinstance(cmdline, str):
        parts: Sequence[str] = cmdline.split()
    else:
        parts = tuple(str(part) for part in cmdline)
    return tuple(part for part in parts if part)


def _cmdline_matches(
    live: Optional[Sequence[str]], expected: Optional[Sequence[str]]
) -> bool:
    """True when two command lines describe the same invocation.

    The lengths have to agree and the first two entries have to be
    equal. The rest of the line is not compared: a wrapper may add
    arguments of its own between the recording and the reading, and the
    executable plus its first argument are what identify the program.
    """
    live_parts = _normalize_cmdline(live)
    expected_parts = _normalize_cmdline(expected)
    if len(live_parts) != len(expected_parts):
        return False
    return (
        list(live_parts[:CMDLINE_PREFIX_LEN])
        == list(expected_parts[:CMDLINE_PREFIX_LEN])
    )
