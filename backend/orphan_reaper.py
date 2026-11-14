"""Reclaim the processes PDT spawned and then forgot.

The problem this module exists for
----------------------------------
On 2026-10-27 this machine accumulated 770 orphaned ``python -m
backend.server`` processes (all re-parented to ``pid 1``), holding about
158 GiB of memory between them; swap filled, the kernel jetsammed the
machine twice, and the box became unusable.  The processes were *not*
mysterious: PDT had started them.  What was missing was any way to tell
that they were ours once the process that started them was gone.

Two existing mechanisms were supposed to cover that gap, and both fail
in exactly the situation that matters:

* a **ledger** (``<plan_dir>/managed_services.json``) written by the
  backend, which the backend also sweeps; and
* **self-reaping** — a sweep at backend start and one at normal exit.

A ledger only works while its writer is alive.  A SIGKILL (which is what
the jetsam does) means neither the write nor the sweep ever happens, so
the record and the sweeper disappear together.  And a ledger written by
the backend can lose entries even without a crash: when the same service
is started twice in a round, the older record is replaced by the newer
one, so the first process is orphaned *with its record already gone*.

So a layer is needed that does not depend on the reaped process
cooperating, and does not depend on the backend being alive.

How a process is recognised as ours
-----------------------------------
The judgement cannot be a guess.  Matching on the working directory or
on the shape of the command line is not portable: PDT is open source and
runs on other people's machines, where neither the path nor the argv
says anything reliable.  The one thing every PDT-spawned process has is
a marker **PDT itself put there**, which is inherited by every
descendant:

===========================  ===============================================
``PDT_SPAWN_OWNER_PID``      pid of the process that spawned it
``PDT_SPAWN_OWNER_START``    that owner's start time (defeats pid reuse)
``PDT_SPAWN_KIND``           what it is: service / fixture / subprocess /
                             agent / reaper
===========================  ===============================================

Two properties of those markers are what make the whole scheme work:

* The owner is stamped **at every spawn level**, not once at the top.
  The owner of a process is the process that actually started it, so a
  grandchild whose parent has died is an orphan even though the
  top-level backend is still running.
* The owner start time travels with the pid.  A pid alone is not an
  identity — the kernel recycles pids — so the marker records *when*
  the owner started.  A live pid whose start time differs from the
  recorded one is a different process that happens to have the same
  number, and must not be touched.

What this module guarantees (and what it does not)
--------------------------------------------------
The module is the *judgement* half of the reaper: configuration, the
marker vocabulary, the primitives that stamp a marker onto a child
environment, the primitives that read a process back — its command
line, its environment, the ports it holds, the memory it is using —
the **verdict ladder** that turns what was read into a decision, and
the **reaping stage** that carries out the one decision that may act:
``SIGTERM``, the grace period, then ``SIGKILL``.  The entry points
below are therefore still limited to resolving and reporting the
configuration.

The safety boundary is deliberate and one-sided:

* A process **without** ``PDT_SPAWN_*`` markers is never a candidate.
  Anything a user started by hand — their own pytest run, their editor,
  an unrelated service — is invisible to this module, always.
* A process whose owner is still alive is never a candidate, even if
  every other marker looks right.
* A process that owns a protected port (see
  :data:`DEFAULT_PROTECT_PORTS`) is never a candidate.
* Uncertainty resolves towards *not* killing: a marker that cannot be
  read, a start time that cannot be determined, a pid that cannot be
  checked — each of these means "skip", never "kill".

Configuration is entirely environmental, and every default is the safe
value — a deployment that configures nothing can only ever reclaim
processes PDT itself marked:

===========================  =========  ====================================
Variable                     Default    Meaning
===========================  =========  ====================================
``PDT_ORPHAN_REAPER``        ``1``      ``0`` disables the reaper entirely
``PDT_ORPHAN_REAPER_INTERVAL``  ``30``  seconds between scans
``PDT_ORPHAN_REAPER_GRACE``  ``5``      seconds between SIGTERM and SIGKILL
``PDT_ORPHAN_REAPER_IDLE_EXIT``  ``1800``  self-exit after this many idle
                                        seconds (0 disables the self-exit)
``PDT_ORPHAN_REAPER_PROTECT_PORTS``  (none)  extra protected ports, added
                                        to the defaults, comma separated
``PDT_ORPHAN_REAPER_DRY_RUN``  ``0``    decide but never signal
``PDT_ORPHAN_REAPER_BURST_THRESHOLD``  ``50``  candidates above which the
                                        reaper treats the box as a
                                        memory emergency
``PDT_ORPHAN_REAPER_SWAP_THRESHOLD``  ``90``  swap usage percent above
                                        which the same applies
``PDT_ORPHAN_REAPER_PID_SCOPE``  (none)  restrict the scan to these pids
                                        (a testing scope gate)
``PDT_ORPHAN_REAPER_MAX_DAEMONS``  ``8``  most reaper daemons the
                                        machine may run at once (0
                                        disables the ceiling)
===========================  =========  ====================================

A malformed value never raises: it falls back to the default, because a
reaper that refuses to start over a typo would leave the memory it
exists to reclaim unreclaimed.

Running it
----------
The module depends on the standard library only — no sibling backend
module is imported — so it can be run on its own::

    python -m backend.orphan_reaper --plans-root /path/to/plans

which resolves the configuration (environment first, command line
second) and prints it.  Two modes do the work:

``--once``
    one pass over the machine — scan, reap, write the events down,
    print one line per verdict (``pid decision reason`` and the
    marker kind when there is one) — and then leave.

``--daemon``
    the same pass, over and over, every ``interval`` seconds, until
    the daemon is asked to stop (``SIGTERM`` / ``SIGINT``) or until
    the machine has been quiet for ``idle_exit`` seconds: no owner
    alive and no candidate at all.  The daemon is a singleton — it
    holds the reaper's lock while it runs, and a second one stands
    down immediately.

    That lock is scoped to one root and can be defeated by deleting
    the lock file, so the daemon also reads the machine's own process
    table before it starts scanning.  It stands down when **another
    daemon is already serving its root**, and when **the machine is
    already running its quota of daemons**
    (``PDT_ORPHAN_REAPER_MAX_DAEMONS``).  The quota counts the
    daemons that are watching a root which is still there: a daemon
    watching a deleted root is watching nothing, and is on its way
    out.  A quota of zero turns the ceiling off.

    The quota is a bound on the machine's *resident* daemons, so it
    is asked of a daemon that is starting on its own — one that keeps
    running after the process that started it is gone.  A daemon whose
    lifetime its caller owns is not one of those: it leaves with the
    caller, and it does not add to the population the ceiling bounds.

    A daemon also leaves when the process that **started it** is
    gone: it was started for that process, and once that process is
    over there is nothing left for it to do.  The owner is recorded as
    a pid *and* a start time, so that a recycled pid is not mistaken
    for the owner.  The owner is the parent by default
    (``--owner-pid`` / ``--owner-start`` say otherwise), and
    ``--survive-owner`` exempts the daemon entirely — which is what
    the daemon the *server* starts uses, because it exists for the
    case where the server dies.

``--dry-run`` turns the deciding on and the acting off: every verdict
is reached and written down, and not one signal is sent.

``--status``
    the machine-level census of the reaper daemons, printed as JSON:
    how many there are, which root each serves, how long each has been
    running.  The lock says whether *this* root is held; this says
    what the machine is actually running.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    IO,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

try:  # POSIX only.  Without it there is no singleton (see ``acquire_lock``).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The real ``subprocess.Popen``, captured at import time.
#:
#: A test replaces ``subprocess.Popen`` with a recorder to watch the
#: product's own spawns — that is how the shape of a service start is
#: asserted.  The reaper's **probes** must not be seen by such a
#: recorder: a probe is the product asking the operating system a
#: question, and a recorder that answers it would answer with the
#: recorder's own answer rather than the operating system's.  Worse, the
#: recorder overwrites the record it was installed to keep: the probe
#: runs *after* the spawn, so the last thing recorded is the probe.
#:
#: The service manager already keeps the same rule for the same reason
#: (see ``service_manager._REAL_POPEN``).
_REAL_POPEN = subprocess.Popen

#: The real ``subprocess.run``, captured at import time.
#:
#: A fake installed for ``subprocess.run`` is a legitimate seam: it
#: answers the probe with a fixture instead of asking the operating
#: system.  The reaper honours such a fake — and uses it *only* when
#: one is installed, so that the probe is otherwise spawned through
#: :data:`_REAL_POPEN` rather than through whatever ``subprocess.Popen``
#: currently names.
_REAL_RUN = subprocess.run

__all__ = [
    "SPAWN_OWNER_PID",
    "SPAWN_OWNER_START",
    "SPAWN_KIND",
    "KIND_SERVICE",
    "KIND_FIXTURE",
    "KIND_SUBPROCESS",
    "KIND_AGENT",
    "KIND_REAPER",
    "ALL_KINDS",
    "DEFAULT_PROTECT_PORTS",
    "DEFAULT_MAX_DAEMONS",
    "DEFAULT_DETACHED_LEASE",
    "MAX_EVENTS_BYTES",
    "ENV_DETACH_OWNER",
    "ReaperConfig",
    "default_plans_root",
    "config_from_env",
    "owner_marks",
    "install_owner_marks",
    "spawn_tags",
    "apply_spawn_tags",
    "process_start_epoch",
    "spawn_start_epoch",
    "ProcInfo",
    "list_processes",
    "read_process_environ",
    "read_process_environ_bulk",
    "BULK_ENV_MIN_PROCESSES",
    "listening_ports_by_pid",
    "swap_usage_ratio",
    "DECISION_KILL",
    "DECISION_SKIP",
    "REASON_UNMARKED",
    "REASON_ENV_UNREADABLE",
    "REASON_OTHER_UID",
    "REASON_SELF",
    "REASON_SELF_REAPER",
    "REASON_OWNER_ALIVE",
    "REASON_OWNER_ALIVE_UNVERIFIED",
    "REASON_PID_REUSED",
    "REASON_OWNER_UNKNOWN",
    "REASON_PORTS_UNKNOWN",
    "REASON_PROTECTED_PORT",
    "REASON_ORPHAN",
    "REASON_OWNER_REVIVED",
    "SIGNAL_TERM",
    "SIGNAL_KILL",
    "SIGNAL_NONE",
    "POLL_STEP",
    "OwnerState",
    "Verdict",
    "ScanResult",
    "classify",
    "probe_owner",
    "scan",
    "reap_verdicts",
    "REAPER_DIRNAME",
    "LOCK_FILENAME",
    "DAEMON_LOG_FILENAME",
    "EVENTS_FILENAME",
    "ALERTS_FILENAME",
    "events_path",
    "alerts_path",
    "write_events",
    "write_skipped_summary",
    "write_pass_events",
    "should_force_cleanup",
    "emit_alert",
    "lock_path",
    "acquire_lock",
    "daemon_alive",
    "ensure_daemon",
    "ReaperDaemon",
    "list_reaper_daemons",
    "daemon_status",
    "stop_daemons_for_plans_root",
    "run_once",
    "run_daemon",
    "build_arg_parser",
    "main",
]


# --------------------------------------------------------------------------
# Marker vocabulary
# --------------------------------------------------------------------------

#: Pid of the process that spawned this one.
SPAWN_OWNER_PID = "PDT_SPAWN_OWNER_PID"

#: Start time of that owner — distinguishes a live owner from a reused pid.
SPAWN_OWNER_START = "PDT_SPAWN_OWNER_START"

#: What kind of process this is.
SPAWN_KIND = "PDT_SPAWN_KIND"

KIND_SERVICE = "service"
KIND_FIXTURE = "fixture"
KIND_SUBPROCESS = "subprocess"
KIND_AGENT = "agent"
KIND_REAPER = "reaper"

#: Every kind the marker vocabulary recognises.
ALL_KINDS: Tuple[str, ...] = (
    KIND_SERVICE,
    KIND_FIXTURE,
    KIND_SUBPROCESS,
    KIND_AGENT,
    KIND_REAPER,
)

#: Ports whose listeners are never reaped.
#:
#: These are the ports PDT's own long-lived services listen on
#: (backend 8000, supervisor 8002, frontends 3000/3001, one more
#: service on 8651).  A process holding one of them is very likely to
#: be a service somebody is using, so it is protected by default.
#:
#: The set is a *floor*, not the whole policy: whatever is configured
#: through :data:`ENV_PROTECT_PORTS` is added to it, never substituted
#: for it.
DEFAULT_PROTECT_PORTS: FrozenSet[int] = frozenset({8000, 8002, 3000, 3001, 8651})


# --------------------------------------------------------------------------
# Environment variable names
# --------------------------------------------------------------------------

ENV_ENABLED = "PDT_ORPHAN_REAPER"
ENV_INTERVAL = "PDT_ORPHAN_REAPER_INTERVAL"
ENV_PROTECT_PORTS = "PDT_ORPHAN_REAPER_PROTECT_PORTS"
ENV_GRACE = "PDT_ORPHAN_REAPER_GRACE"
ENV_IDLE_EXIT = "PDT_ORPHAN_REAPER_IDLE_EXIT"
ENV_MAX_LIFETIME = "PDT_ORPHAN_REAPER_MAX_LIFETIME"
ENV_DRY_RUN = "PDT_ORPHAN_REAPER_DRY_RUN"
ENV_BURST_THRESHOLD = "PDT_ORPHAN_REAPER_BURST_THRESHOLD"
ENV_SWAP_THRESHOLD = "PDT_ORPHAN_REAPER_SWAP_THRESHOLD"
ENV_PID_SCOPE = "PDT_ORPHAN_REAPER_PID_SCOPE"
ENV_MAX_DAEMONS = "PDT_ORPHAN_REAPER_MAX_DAEMONS"

#: The environment variable that says a daemon may outlive the process
#: that started it.
#:
#: The exemption from the owner rule is a decision somebody has to make
#: out loud.  Made by accident — for every backend on the machine, by a
#: single argument in the source — it left twenty-four daemons walking
#: one machine on 2026-11-02, each of them scanning every process on
#: the box every thirty seconds, for ever.
ENV_DETACH_OWNER = "PDT_ORPHAN_REAPER_DETACH_OWNER"

#: Environment variable that redirects the plan-artifact root.
ENV_PLANS_DIR = "PDT_PLANS_DIR"

# Defaults — each one is the value that cannot do harm.
DEFAULT_INTERVAL = 30
DEFAULT_GRACE = 5.0
DEFAULT_IDLE_EXIT = 1800
DEFAULT_BURST_THRESHOLD = 50
DEFAULT_SWAP_THRESHOLD = 90.0

#: The longest a reaper daemon may live, whatever else happens (24h).
#:
#: The idle streak is about idleness; this is about **age**.  A daemon
#: has to have an end, because a daemon that cannot end is a daemon
#: that accumulates: on 2026-10-31 twenty-six of them were running on
#: one machine, each of them walking every process on the box every
#: thirty seconds.  The ceiling is generous on purpose — a production
#: backend may run for days — and it is the *last* door out, behind
#: the idle streak and behind the "my world has been deleted" check.
DEFAULT_MAX_LIFETIME = 86400

#: The most reaper daemons that may run on one machine at the same time.
#:
#: The lock makes the daemon a singleton *per plan root*, and nothing
#: more: a daemon on another root neither excludes this one nor is
#: excluded by it, and the lock file itself can be recreated — deleting
#: it and starting a new daemon hands the new one a fresh inode to
#: lock, so two daemons end up watching one root.  On 2026-11-02 this
#: machine was running 145 daemons at once, most of them watching
#: temporary directories nobody used any more.
#:
#: A ceiling on the machine as a whole is what stops the accumulation:
#: a daemon that would push the machine past the ceiling stands down
#: instead of starting.  Zero turns the ceiling off.
DEFAULT_MAX_DAEMONS = 8

#: The lease a daemon that may outlive its owner is given, in seconds.
#:
#: A daemon that leaves with the process that started it has an end
#: already: the owner goes, the daemon goes.  A **detached** daemon has
#: no such end — that is the whole point of the exemption — so it is
#: given one here instead.  An hour is long enough to reclaim what a
#: backend leaves behind, and short enough that a machine does not
#: accumulate them.
DEFAULT_DETACHED_LEASE = 3600

#: The ceiling on the live kill-event log, in bytes.
#:
#: The log is the reaper's record of what it decided, and it was also
#: the biggest file on the machine: every pass wrote a line for every
#: process it looked at, every thirty seconds, and the processes it
#: looked at were every process on the box.  1.1 GB on 2026-11-02.
#:
#: The ceiling is a rotation, not a truncation: the live log is moved
#: aside when the next write would carry it past this size, and one
#: generation is kept.  What is on disk is therefore bounded at twice
#: this number, whatever the machine does.
MAX_EVENTS_BYTES = 5 * 1024 * 1024

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


# --------------------------------------------------------------------------
# Process start time
# --------------------------------------------------------------------------

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _proc_start_ticks(pid: int) -> Optional[str]:
    """Return ``/proc/<pid>/stat``'s start time (Linux), or ``None``.

    The value is the process's start time in clock ticks since boot.  It
    is derived from the kernel rather than from the environment, so any
    observer computing it for the same pid gets the same string — which
    is exactly the property the owner-start marker needs.
    """
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None

    # The comm field (2) is parenthesised and may itself contain spaces
    # and parentheses, so the reliable split point is its final ')'.
    try:
        fields = raw[raw.rindex(")") + 1:].split()
    except ValueError:
        return None

    # fields[0] is the state (field 3); starttime is field 22.
    if len(fields) < 20:
        return None
    try:
        return str(int(fields[19]))
    except ValueError:
        return None


def _parse_lstart(text: str) -> Optional[str]:
    """Parse a ``ps -o lstart=`` timestamp into epoch seconds.

    ``ps`` prints e.g. ``Tue Oct 27 11:27:29 2026``.  The month and
    weekday names are parsed from a fixed table rather than through the
    locale-dependent ``strptime`` so that the result does not depend on
    the process locale — the same timestamp has to parse identically in
    the process that stamps the marker and in the process that checks
    it.
    """
    parts = text.split()
    if len(parts) != 5:
        return None
    _weekday, month_name, day, clock, year = parts

    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    try:
        hour, minute, second = (int(part) for part in clock.split(":"))
        stamp = datetime.datetime(
            int(year), month, int(day), hour, minute, second
        )
    except (TypeError, ValueError):
        return None
    return str(int(stamp.timestamp()))


def _ps_binary() -> str:
    """Return the path of the ``ps`` to run.

    Resolved once, and never from a bare name alone: the reaper may be
    started as a daemon with a minimal ``PATH``, and a missing ``ps``
    would silently turn every start time into "unknown" — which means
    the reaper would never be able to tell a live owner from a reused
    pid, and would therefore never reclaim anything.
    """
    global _PS_BINARY
    if _PS_BINARY is None:
        _PS_BINARY = shutil.which("ps") or "/bin/ps"
    return _PS_BINARY


def _run_probe(
    args: List[str],
    timeout: float = 10.0,
    env: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[int, str, str]]:
    """Run a probe command and return ``(returncode, stdout, stderr)``.

    ``None`` means the probe could not be run at all — it is missing, it
    could not be spawned, it timed out.  A probe that ran and failed is
    reported through its return code instead, because "the tool said no"
    and "the tool never ran" mean different things to the caller.

    The probe is spawned through :data:`_REAL_POPEN` rather than through
    the module attribute ``subprocess.Popen``.  ``subprocess.run`` looks
    that attribute up when it spawns, so a test that replaced it would
    capture the reaper's probes as well as the product's spawns — and
    the probe, running *after* the spawn, would overwrite the record
    the test came to read.

    A fake installed for ``subprocess.run`` is honoured instead: it is
    the probe's *answer*, and an answer is a legitimate thing to fake
    (``tests/unit/test_orphan_reaper_procs.py`` fakes it to pin the
    parsers against a fixture).  The two seams cannot be confused: the
    fake replaces the whole call, the recorder replaces only the spawn.
    """
    runner = subprocess.run
    if runner is not _REAL_RUN:
        try:
            completed = runner(
                list(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return (
            int(getattr(completed, "returncode", 0) or 0),
            _as_text(getattr(completed, "stdout", "")),
            _as_text(getattr(completed, "stderr", "")),
        )
    try:
        completed = _REAL_POPEN(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    try:
        out, err = completed.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            completed.kill()
        except OSError:  # pragma: no cover - the child is already gone
            pass
        try:
            completed.communicate(timeout=timeout)
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pass
        return None
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    return (
        int(getattr(completed, "returncode", 0) or 0),
        _as_text(out),
        _as_text(err),
    )


def _ps_start_epoch(pid: int) -> Optional[str]:
    """Return the start time of ``pid`` via ``ps`` (macOS / BSD), or ``None``.

    macOS has no ``/proc``; ``sysctl kern.procargs2.<pid>`` is not
    available on Darwin 25.6 (the oid is unknown), so ``ps`` is the
    remaining source.  The subprocess runs under ``LC_ALL=C`` so the
    output is the English, fixed-width form regardless of the locale
    the reaper itself runs in.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    result = _run_probe(
        [_ps_binary(), "-o", "lstart=", "-p", str(pid)],
        timeout=5,
        env=env,
    )
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode != 0:
        return None
    return _parse_lstart(stdout.strip())


#: How many pids one ``ps`` invocation is asked about.
_PS_PID_BATCH = 256


def _ps_start_epochs(pids: Sequence[int]) -> Optional[Dict[int, str]]:
    """Return the start times of *pids*, read in as few spawns as possible.

    Asking about one pid at a time costs one ``ps`` each, and a machine
    with a hundred live owners costs a hundred spawns — wall clock that
    the boot path pays for.  ``ps`` accepts a list of pids, so the whole
    set is asked about at once.

    ``None`` means the answer is unusable: ``ps`` could not be run, or it
    failed in a way that says nothing about the pids.  An empty mapping
    is a *usable* answer that reports no live pid — which is not the
    same thing, and the caller has to be able to tell them apart.
    """
    wanted: List[int] = []
    seen = set()
    for pid in pids:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        wanted.append(value)
    if not wanted:
        return {}

    epochs: Dict[int, str] = {}
    for start in range(0, len(wanted), _PS_PID_BATCH):
        chunk = wanted[start:start + _PS_PID_BATCH]
        result = _run_tool(
            [
                _ps_binary(),
                "-o",
                "pid=,lstart=",
                "-p",
                ",".join(str(pid) for pid in chunk),
            ],
            timeout=15.0,
        )
        if result is None:
            return None
        returncode, stdout, _stderr = result
        if returncode == 1:
            # ``ps`` reports 1 when none of the pids it was given exists.
            # That is an answer, not a failure.
            continue
        if returncode != 0:
            return None
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            epoch = _parse_lstart(" ".join(parts[1:6]))
            if epoch:
                epochs[pid] = epoch
    return epochs


def process_start_epoch(pid: Optional[int] = None) -> str:
    """Return a stable identifier for *when* ``pid`` started.

    The value is derived from the operating system, not from the
    environment, so an observer that computes it for a live pid gets
    the same string the process itself recorded.  That is what makes
    pid reuse detectable.

    An empty string means "could not be determined" — the platform
    exposes neither ``/proc`` nor a usable ``ps``.  Callers must treat
    that as *unknown*, never as "different": an unknown start time
    means the process is skipped, not that it is killed.
    """
    target = os.getpid() if pid is None else int(pid)
    for source in (_proc_start_ticks, _ps_start_epoch):
        value = source(target)
        if value is not None:
            return value
    return ""


#: Whether this platform reads a process' start time out of ``/proc``.
#:
#: The unit matters.  ``/proc`` answers in clock ticks since boot and
#: ``ps -o lstart=`` answers in epoch seconds; a value recorded in one
#: and compared against the other reads as "a different instant" every
#: time, and the reaper would then treat every live owner as a
#: recycled pid and stop reclaiming anything on the very platform the
#: marker was written for.
_PROC_START_UNIT_IS_TICKS = _proc_start_ticks(os.getpid()) is not None

#: How far apart two readings of one start time may be and still be the
#: same instant, expressed in seconds.
#:
#: ``ps -o lstart=`` has one-second granularity, so a value derived from
#: the spawn clock can land on the other side of a second boundary from
#: the value an observer reads.  Two seconds covers that without making
#: the comparison blind to a recycled pid, which is hours away.
_START_TOLERANCE_SECONDS = 2.0

#: This process's own start time, computed once.
#:
#: It cannot change while the process lives, and computing it is not
#: free — on macOS it costs a ``ps`` subprocess — so it is cached.  An
#: empty result is *not* cached, so a transient failure does not
#: permanently deafen the process to its own identity.  The cache is
#: keyed on the pid, so a forked child does not answer with its
#: parent's start time.
_SELF_START_EPOCH = ""

#: The pid :data:`_SELF_START_EPOCH` was measured for.
_SELF_START_EPOCH_PID = 0

#: Resolved path of the ``ps`` binary (see :func:`_ps_binary`).
_PS_BINARY: Optional[str] = None


def _start_tolerance() -> float:
    """The tolerance, in the unit this platform expresses start times in.

    Where the reading comes from ``/proc`` it is the kernel's own exact
    number, so there is nothing to absorb: two readings that differ
    describe two processes, and any slack here would only widen the
    window in which a recycled pid is mistaken for ours — the one
    mistake this comparison exists to prevent.

    Everywhere else the observation goes through ``ps -o lstart=``,
    whose start time has one-second granularity, so a reading taken a
    moment after the fork can land on the other side of a second
    boundary from the value an observer reads.  Two seconds is that
    single second doubled, expressed in the unit this platform uses.
    """
    if _PROC_START_UNIT_IS_TICKS:
        return 0.0
    return _START_TOLERANCE_SECONDS


def spawn_start_epoch(
    pid: int, *, spawned_at: Optional[float] = None,
) -> str:
    """Return *pid*'s start time, in this platform's unit, without a subprocess.

    The spawn path has to write down when the process it just created
    started — a pid on its own is not an identity — and it has to do it
    **without creating a process to ask**.  Where the start time is
    read from ``/proc``, the kernel's own answer comes out of a file:
    exact, and free of side effects.  Everywhere else the unit is epoch
    seconds, and the spawn clock is within a millisecond of the child's
    own start — close enough that :func:`_same_start` recognises the
    two readings as one instant once the second-granularity tolerance
    is applied.

    An empty string means "could not be determined", which is what an
    empty string means everywhere else in this module: *unknown*, never
    "different".
    """
    if _PROC_START_UNIT_IS_TICKS:
        return _proc_start_ticks(pid) or ""
    if spawned_at is None:
        return ""
    return str(int(spawned_at))


def _self_start_epoch() -> str:
    """Return this process's start time, computed at most once per pid."""
    global _SELF_START_EPOCH, _SELF_START_EPOCH_PID
    pid = os.getpid()
    if not _SELF_START_EPOCH or _SELF_START_EPOCH_PID != pid:
        _SELF_START_EPOCH = process_start_epoch(pid)
        _SELF_START_EPOCH_PID = pid
    return _SELF_START_EPOCH


# Resolve our own start time **here**, at import, rather than at the
# first spawn.  The answer cannot change while the process lives, and on
# a machine without ``/proc`` asking for it costs a ``ps`` subprocess —
# a process the spawn path is not allowed to create.  Resolving it once
# keeps every spawn path (the services, the bounded subprocesses, the
# reaper's own daemon) free of side effects, and they all read the same
# answer.
try:
    _self_start_epoch()
except Exception:  # noqa: BLE001 - an identity probe never blocks an import
    pass


# --------------------------------------------------------------------------
# Spawn tags
# --------------------------------------------------------------------------


def spawn_tags(
    kind: str,
    *,
    owner_pid: Optional[int] = None,
    owner_start: Optional[str] = None,
) -> Dict[str, str]:
    """Build the marker for a child spawned by this process.

    ``owner_pid`` / ``owner_start`` default to *this* process, which is
    the normal case: a spawn point stamps the child with the identity
    of the process doing the spawning.

    Raises :class:`ValueError` for a kind outside :data:`ALL_KINDS` — an
    unrecognised kind would produce a marker no reader could interpret,
    and silently defaulting to a known kind would misdescribe the
    process.
    """
    normalized = str(kind).strip().lower()
    if normalized not in ALL_KINDS:
        raise ValueError(
            "unknown spawn kind %r; expected one of: %s"
            % (kind, ", ".join(ALL_KINDS))
        )

    pid = os.getpid() if owner_pid is None else int(owner_pid)
    start = _self_start_epoch() if owner_start is None else str(owner_start)

    return {
        SPAWN_OWNER_PID: str(pid),
        SPAWN_OWNER_START: str(start),
        SPAWN_KIND: normalized,
    }


def apply_spawn_tags(
    env: MutableMapping[str, str],
    kind: str,
    *,
    owner_pid: Optional[int] = None,
    owner_start: Optional[str] = None,
) -> MutableMapping[str, str]:
    """Stamp ``env`` with the spawn marker and return it.

    The mapping is updated in place — callers build a child environment
    and hand it straight to :class:`subprocess.Popen` — and the same
    mapping is returned so the call can be chained.

    Any marker already present is **overwritten**.  A child inherits
    its parent's marker, and a marker that survived a new owner would
    misattribute the child to an ancestor that may long since be gone.
    """
    env.update(spawn_tags(kind, owner_pid=owner_pid, owner_start=owner_start))
    return env


def owner_marks() -> Dict[str, str]:
    """Return the owner marker for *this* process (without writing it)."""
    return {
        SPAWN_OWNER_PID: str(os.getpid()),
        SPAWN_OWNER_START: _self_start_epoch(),
    }


def install_owner_marks(
    environ: Optional[MutableMapping[str, str]] = None,
) -> Dict[str, str]:
    """Mark this process as the owner of whatever it spawns.

    Writing the owner marker into this process's own environment makes
    every child inherit it, which is how the backend's own subprocesses
    (executors, sub-agents, test runs) become traceable without every
    spawn point having to be taught about the reaper.

    Only the two owner marks are written — :data:`SPAWN_KIND` is left
    alone, because "who spawned this" and "what is this" are different
    questions, and a process that owns children is not thereby a
    service, a fixture or a reaper.  Calling this repeatedly is safe
    and produces the same result.

    Returns the marks that were written.
    """
    env = os.environ if environ is None else environ
    marks = owner_marks()
    env[SPAWN_OWNER_PID] = marks[SPAWN_OWNER_PID]
    env[SPAWN_OWNER_START] = marks[SPAWN_OWNER_START]
    return marks


# --------------------------------------------------------------------------
# The spawn registry
# --------------------------------------------------------------------------
#
# The spawn marker says a process is *ours*.  The registry says a
# process is *this run's* -- and this run is the thing that has to be
# able to give it back.
#
# A declared service is started detached: it calls ``setsid()`` and
# becomes a session leader, so a watchdog that kills the gate's process
# group does not touch it.  That is what makes a service outlive the
# run that started it, and it is how the CI of 2026-10-31 left a
# ``backend.server`` answering ``/health`` a minute after the watchdog
# had fired -- and how twenty-six reaper daemons accumulated on one
# machine, each of them leading a session of its own again.
#
# So every detached process a run starts writes itself down, and every
# cleanup path gives it back:
#
# * the gate, on its way out (its EXIT trap);
# * the CI runner, after the gate returns -- including when the gate
#   was killed and never got to its own cleanup.
#
# The registry is named by an environment variable, so it is inherited
# by everything the run starts: the service, the reaper daemon the
# service starts, the harness that spawns a server.  A process that
# registers itself is one the run can find again.

#: The environment variable that names the registry of this run's
#: detached processes.
SPAWN_REGISTRY_ENV = "PDT_SPAWN_REGISTRY"


# --------------------------------------------------------------------------
# The registry's lock
# --------------------------------------------------------------------------
#
# The registry is written by everybody in the run: the gate writes
# itself down, the CI runner does, and every service writes down the
# reaper daemon it starts.  Those writes used to race -- an append
# against a rewrite -- and a rewrite that lost an append lost a
# *process*: an entry that is not in the registry is a process nobody
# can give back, which is the whole failure the registry exists to
# prevent.
#
# So the registry has a lock.  It is an ``flock`` on a sidecar file
# next to the registry, taken exclusively for a write and shared for a
# read.  The lock covers the read and the write of a read-modify-write
# cycle and nothing else: a cleanup pass that held it while it waited
# for a process to die would keep every other process out of the
# registry for as long as the grace period lasts.
#
# The lock is advisory and it is best-effort.  A platform without
# ``fcntl``, an unwritable directory, or a holder that does not let go
# in time leaves the caller without a lock -- and a caller without a
# lock still writes, it just writes without the guarantee.  Waiting for
# ever is not an option: a registry that cannot be written is a run
# that cannot record what it started.

#: The suffix of the file that carries the registry's lock.
_REGISTRY_LOCK_SUFFIX = ".lock"

#: How long a writer waits for the registry's lock before going on
#: without it.
_REGISTRY_LOCK_TIMEOUT = 5.0

#: The lock this thread holds, if it holds one.
#:
#: ``flock`` is per *open file description*, so taking the lock twice
#: in one thread -- once for the read and once for the write of a
#: read-modify-write cycle -- would wait for a lock the thread itself
#: holds.  The depth counter is what keeps that from happening; the pid
#: is what keeps a forked child from inheriting the claim.
_REGISTRY_LOCK_STATE = threading.local()


def _registry_lock_path(target: Path) -> Path:
    """The file that carries the registry's lock."""
    return target.with_name(target.name + _REGISTRY_LOCK_SUFFIX)


def _take_flock(handle: IO[str], timeout: float, shared: bool) -> bool:
    """Take a ``flock`` on *handle*, waiting at most *timeout* seconds.

    ``False`` when the lock could not be taken: no ``fcntl`` on this
    platform, an error the kernel will not repeat, or a holder that did
    not let go in time.  The caller goes on without the lock rather than
    waiting for ever -- a registry that cannot be written is worse than
    a registry that is written without a guarantee.
    """
    if fcntl is None:  # pragma: no cover - Windows
        return False
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    deadline = time.monotonic() + max(0.0, float(timeout))
    delay = 0.005
    while True:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


@contextlib.contextmanager
def _registry_lock(
    target: Path,
    *,
    timeout: float = _REGISTRY_LOCK_TIMEOUT,
    shared: bool = False,
) -> Iterator[bool]:
    """Hold the registry's lock while the body runs.

    Yields ``True`` when the lock was taken and ``False`` when it was
    not.  ``False`` is not a failure: the caller writes without the
    guarantee rather than blocking for ever.  The lock is taken
    exclusively unless *shared* is asked for, and it is taken for at
    most *timeout* seconds.

    The lock is re-entrant within a thread: a caller that already holds
    it -- the read-modify-write cycle does -- gets it again without
    waiting for itself.
    """
    state = _REGISTRY_LOCK_STATE
    if (
        getattr(state, "depth", 0)
        and getattr(state, "pid", None) == os.getpid()
    ):
        state.depth += 1
        try:
            yield bool(getattr(state, "locked", False))
        finally:
            state.depth -= 1
        return

    lock_path = _registry_lock_path(target)
    handle: Optional[IO[str]] = None
    locked = False
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
        locked = _take_flock(handle, timeout, shared)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            logger.warning(
                "orphan reaper: could not open the spawn registry lock %s "
                "(%s)", lock_path, exc,
            )
        if handle is not None:
            handle.close()
        handle = None
        locked = False
    if handle is not None and not locked:
        logger.warning(
            "orphan reaper: could not take the spawn registry lock %s; "
            "writing without it", lock_path,
        )

    state.depth = 1
    state.pid = os.getpid()
    state.locked = locked
    try:
        yield locked
    finally:
        state.depth = 0
        if handle is not None:
            try:
                if locked:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - the lock is already gone
                pass
            handle.close()


def spawn_registry_path(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """The registry this run writes its processes down in.

    ``None`` means there is none.  A run that does not name a registry
    does not want one -- the backend's own sessions never do -- and
    every call below reads that as "nothing to write down, nothing to
    give back".
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(SPAWN_REGISTRY_ENV, "") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def register_spawned_process(
    pid: int,
    *,
    pgid: Optional[int] = None,
    kind: str = KIND_SERVICE,
    name: str = "",
    path: Optional[Path] = None,
    start_epoch: Optional[str] = None,
) -> Optional[Path]:
    """Write a detached process down, so this run can give it back.

    *start_epoch* is the process' start time in this platform's unit.  A
    caller that already knows it passes it in; otherwise it is read
    here, which on a machine without ``/proc`` costs a ``ps``.

    The entry carries the process' *start time* next to its pid: a pid
    on its own is not an identity, because pids are recycled, and a
    recycled pid signalled by mistake is a process this run never
    started.

    The write is made under the registry's lock, so that it cannot
    land in a file that a cleanup pass is replacing at that very
    moment: a line appended to a file that is about to be replaced is
    a line nobody will ever read.

    Returns the registry the entry went into, or ``None`` when there
    is no registry to write into.
    """
    target = Path(path) if path is not None else spawn_registry_path()
    if target is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 1:
        return None
    entry: Dict[str, Any] = {
        "pid": pid,
        "pgid": int(pgid) if pgid else None,
        "kind": str(kind or ""),
        "name": str(name or ""),
        "owner_pid": os.getpid(),
        # The start time is read *before* the lock is taken.  It costs a
        # ``ps`` on macOS, and the registry's lock is not a place to
        # spend that: everything else in the run is waiting for it.
        # A caller that already knows when the process started passes
        # that in -- the spawn path does, and it knows without asking
        # the operating system, which is the only way a spawn can stay
        # free of extra processes.
        "start_epoch": (
            start_epoch if start_epoch is not None
            else process_start_epoch(pid)
        ),
        "registered_at": time.time(),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _registry_lock(target):
            text = _read_registry_text(target)
            if text and not text.endswith("\n"):
                # The last line has no newline of its own -- a writer
                # that died half way through.  Without this the new
                # line would be appended to it, and the two would
                # become one line that parses as neither.
                text += "\n"
            _replace_spawn_registry(target, text + line)
    except OSError as exc:
        logger.warning(
            "orphan reaper: could not write the spawn registry %s (%s)",
            target, exc,
        )
        return None
    return target


def _read_registry_text(target: Path) -> str:
    """The registry's raw text.

    Every write replaces the file atomically (see
    :func:`_replace_spawn_registry`), so a reader always sees a whole
    file: the old one or the new one, never a mixture of the two.
    """
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_spawn_registry_unlocked(target: Path) -> List[Dict[str, Any]]:
    """The registry's entries, read without taking the lock.

    Reading is safe without the lock because every write replaces the
    file atomically (see :func:`_replace_spawn_registry`): a reader
    sees either the old file or the new one, and never half of either.
    """
    entries: List[Dict[str, Any]] = []
    for line in _read_registry_text(target).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("pid"):
            entries.append(entry)
    return entries


def read_spawn_registry(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The processes this run has written down, oldest first."""
    target = Path(path) if path is not None else spawn_registry_path()
    if target is None:
        return []
    with _registry_lock(target, shared=True):
        return _read_spawn_registry_unlocked(target)


def _write_spawn_registry(target: Path, entries: List[Dict[str, Any]]) -> None:
    """Replace the registry with *entries* (one JSON object per line).

    The entries are written to a temporary file in the same directory
    and then moved into place with ``os.replace``.  A reader that opens
    the registry while the write is in flight therefore sees either the
    old file or the new one -- never half of either.  A registry
    written in place (``open(target, "w")``) is a half-written file for
    as long as the write takes, and a reader that cannot parse a line
    throws it away: a rewrite dropped entries that were perfectly fine.

    The registry's lock is taken here, so the write is serialised
    against the writes of everybody else in the run.  A caller that
    already holds the lock -- a read-modify-write cycle does -- takes it
    again without waiting for itself.
    """
    payload = "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "orphan reaper: could not create the spawn registry's "
            "directory %s (%s)", target.parent, exc,
        )
        return
    with _registry_lock(target):
        _replace_spawn_registry(target, payload)


def _replace_spawn_registry(target: Path, payload: str) -> None:
    """Move *payload* into the registry's place, atomically."""
    tmp_path: Optional[Path] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "orphan reaper: could not create the spawn registry's "
            "directory %s (%s)", target.parent, exc,
        )
        return
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=target.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as exc:
        logger.warning(
            "orphan reaper: could not rewrite the spawn registry %s (%s)",
            target, exc,
        )
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - defensive
                pass


def _reap_if_our_child(pid: int) -> bool:
    """Reap *pid* when it is our own child that has already exited.

    A child of this process that has been killed stays a zombie until
    it is waited for, and a zombie still answers ``kill(pid, 0)``.  So
    a run that waits for its own children to disappear would wait out
    the whole grace period for a process that is already gone.
    """
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return waited == pid


def _probe_registry_alive(pid: int) -> Optional[bool]:
    """Whether *pid* is still there, for the registry's wait loop."""
    if _reap_if_our_child(pid):
        return False
    return _pid_alive(pid)


def _process_is_zombie(pid: int) -> Optional[bool]:
    """Whether *pid* is a zombie -- a process that exited and was not waited for.

    ``True`` -- it is a zombie.  ``False`` -- it is not one (it is
    running, or it is not there at all).  ``None`` -- the platform
    cannot say.
    """
    result = _run_tool(
        [_ps_binary(), "-o", "stat=", "-p", str(pid)], timeout=5.0,
    )
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode == 1:
        # ``ps`` reports 1 for a pid it does not know.  There is no
        # zombie there either.
        return False
    if returncode != 0:
        return None
    state = stdout.strip()
    if not state:
        return False
    return state.startswith("Z")


def _owner_alive(pid: int) -> Optional[bool]:
    """Whether the owner of a registry entry is still there to want it.

    ``True`` -- the owner is running.  ``False`` -- the owner is gone:
    there is nothing left of it that could want the process it started.
    ``None`` -- the platform cannot say.

    A **zombie** owner counts as gone.  It has already exited; all that
    is left of it is a table entry, and a table entry cannot ask for its
    children to be given back.  ``kill(pid, 0)`` cannot tell the two
    apart -- it succeeds for a zombie -- so the state is read from the
    operating system.
    """
    alive = _pid_alive(pid)
    if alive is not True:
        # ``False``: the owner is gone.  ``None``: the platform cannot
        # say -- and "cannot say" is never "gone", it is "do not touch".
        return alive
    zombie = _process_is_zombie(pid)
    if zombie is True:
        return False
    if zombie is None:
        return None
    return True


def _memoised_owner_alive() -> Callable[[int], Optional[bool]]:
    """An owner liveness probe that asks the platform once per owner.

    A registry holds many entries and few owners, and the platform is
    asked through ``ps`` -- one spawn per question.  One owner must
    not get two answers in one pass either: the second answer would be
    the one that decides, and the first one would have been a different
    question.
    """
    answers: Dict[int, Optional[bool]] = {}

    def owner_alive(pid: int) -> Optional[bool]:
        if pid not in answers:
            answers[pid] = _owner_alive(pid)
        return answers[pid]

    return owner_alive


def _registry_group_of(pid: int) -> Optional[int]:
    """The process group *pid* leads, if it leads one.

    Only a group the target itself leads is ever signalled.  A group
    led by somebody else holds processes this run never judged, and
    signalling it would take them out as well -- which is how a cleanup
    path becomes a second bug.
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if pgid != pid:
        return None
    return _signallable_pgid(pgid, own_pgid=_own_pgid())


def _reap_registry_entry(
    entry: Dict[str, Any],
    *,
    grace_seconds: float,
    reason: str,
    probe: Optional[Callable[[int], str]] = None,
    alive: Optional[Callable[[int], Optional[bool]]] = None,
    owner_alive: Optional[Callable[[int], Optional[bool]]] = None,
) -> str:
    """Give one registered process back.

    Returns ``"reaped"`` when the process was signalled, ``"gone"``
    when there was nothing left to signal, ``"foreign"`` when the pid
    has been recycled and the process is not ours any more, ``"unknown"``
    when the process' start time could not be established on either
    side, and ``"owned"`` when the process belongs to an owner that is
    still there.

    ``foreign`` and ``unknown`` both mean *not ours to kill*; they
    differ in what the run is entitled to say.  A pid whose start time
    is **known** to be somebody else's does not get touched.  A pid
    whose start time is **unknown** might already be somebody else's,
    and the run cannot tell -- so it does not get touched either.  The
    start time is the only thing that makes a pid an identity, and an
    identity that cannot be established is not an identity.

    ``owned`` says the same thing about a different question.  The
    process *is* the one this run started -- its start time says so --
    but the process that started it is still there, and it is not this
    one.  That process may still want the service it started, and it
    is the run that has to give it back.  A pass that signsalled it
    anyway would take a live service away from a run that is still
    going, which is how a cleanup path becomes a second bug.

    *probe* and *alive* are the injection points: *probe* reads a
    pid's start time (an empty string means *unknown*), *alive* says
    whether a pid is still there.  Both default to the real thing.
    *owner_alive* answers the same question for the entry's owner; when
    it is not given, the pass's own liveness probe answers for the
    owner as well, and the real liveness check answers when there is
    no probe at all.
    """
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return "gone"
    if pid <= 1:
        return "gone"

    alive_fn = alive if alive is not None else _probe_registry_alive
    if alive_fn(pid) is False:
        return "gone"

    probe_fn = probe if probe is not None else process_start_epoch

    recorded = str(entry.get("start_epoch") or "")
    if not recorded:
        # The run never learned when the process started, so it cannot
        # tell its own process from a pid that has been recycled.
        logger.info(
            "orphan reaper: pid %s has no recorded start time; "
            "leaving it alone",
            pid,
        )
        return "unknown"

    observed = str(probe_fn(pid) or "")
    if not observed:
        # The start time cannot be read now, for the same reason.
        logger.info(
            "orphan reaper: could not observe the start time of pid %s; "
            "leaving it alone",
            pid,
        )
        return "unknown"

    if not _same_start(recorded, observed, _start_tolerance()):
        # The number came back: this is somebody else's process.
        logger.info(
            "orphan reaper: pid %s was recycled; leaving it alone",
            pid,
        )
        return "foreign"

    owner_pid = _entry_owner_pid(entry)
    if owner_pid > 0 and owner_pid != os.getpid():
        owner_fn = owner_alive
        if owner_fn is None:
            owner_fn = alive if alive is not None else _owner_alive
        if owner_fn(owner_pid) is not False:
            # The owner is still there -- or the platform cannot say --
            # and it is not us.  "Cannot say" is not "gone": it means
            # the entry is not this pass's to give back.
            logger.info(
                "orphan reaper: pid %s belongs to owner %s, which is "
                "still there; leaving it alone",
                pid, owner_pid,
            )
            return "owned"

    group = _registry_group_of(pid)
    try:
        if group is not None:
            os.killpg(group, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except OSError as exc:
        logger.warning(
            "orphan reaper: could not signal pid %s (%s)", pid, exc,
        )
        return "gone"

    if _wait_for_exit(pid, alive_fn, grace_seconds, time.sleep):
        return "reaped"

    try:
        if group is not None:
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "reaped"
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(
            "orphan reaper: could not kill pid %s (%s)", pid, exc,
        )
    _wait_for_exit(pid, alive_fn, 1.0, time.sleep)
    logger.info(
        "orphan reaper: gave back %s (pid %s, %s)",
        entry.get("name") or entry.get("kind") or "process", pid,
        reason or "the run is over",
    )
    return "reaped"


def _entry_owner_pid(entry: Dict[str, Any]) -> int:
    """The pid of the process that wrote an entry down.

    ``0`` when the entry names no owner -- or names one that is not a
    pid at all.  An entry without an owner cannot be judged against a
    live owner, so the sweep treats it the way it treated it before:
    the entry is the run's to give back.
    """
    try:
        return int(entry.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return 0


def _entry_identity(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    """What makes one registry entry the *same* entry as another.

    A pid on its own is not an identity -- the kernel recycles them --
    so the start time travels with it.  The owner travels too: the same
    process can be written down by two different owners, and only the
    entry this pass acted on may be taken out.
    """
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    owner = _entry_owner_pid(entry)
    return (str(pid), str(entry.get("start_epoch") or ""), str(owner))


def reap_spawned_processes(
    path: Optional[Path] = None,
    *,
    reason: str = "",
    grace_seconds: float = 5.0,
    owner_pid: Optional[int] = None,
    probe: Optional[Callable[[int], str]] = None,
    alive: Optional[Callable[[int], Optional[bool]]] = None,
    owner_alive: Optional[Callable[[int], Optional[bool]]] = None,
) -> List[Dict[str, Any]]:
    """Give back every process the registry names, and forget it.

    SIGTERM first, then SIGKILL after the grace period -- the same
    order the reaper itself uses, because the processes here are the
    same kind of process: a service asked to stop politely first gets
    the chance to close its port.

    The entries that were acted on are removed from the registry, so
    two cleanup paths running one after the other (the gate's trap and
    the CI runner's) do not signal the same process twice.  An entry
    whose process is already gone is removed as well: the registry
    records what the run started, and it must not become a list of
    everything the run ever started.

    An entry whose process was **not** given back stays in the
    registry -- whether the pid turned out to be somebody else's
    ("foreign"), could not be identified at all ("unknown"), or
    belongs to an owner that is still there ("owned").  The run has
    not established that the process is gone, so the record of it has
    to survive the cleanup.

    An entry whose **owner is still there** is not this pass's to give
    back, whoever started it.  The registry is shared by everything a
    run starts, and one run's cleanup path sweeps all of it -- so a
    gate that ends while another run is still going would otherwise
    signal the services of the run that is still going.  The owner is
    the run that started the process, and it is the run that has to
    give it back.  An owner that cannot be found is treated the same
    way: "cannot say" is never "gone".

    The registry is *merged*, not replaced: what is written back is
    what the registry holds now, minus the entries this pass acted on.
    That difference is the whole point -- a pass that waits out a grace
    period can spend seconds doing it, and a process that starts in
    the meantime is written down in exactly that window.  Writing back
    what the pass had read would take that new entry with it, and a
    running process that is not in the registry is a process nobody can
    give back.  So the entries are removed by identity -- the pid, the
    start time and the owner -- and everything else is left alone.

    The lock covers the read and the write, and not the waiting: a
    pass that held the lock while it waited for a process to die would
    keep every other process in the run out of the registry for as long
    as the grace period lasts.

    ``owner_pid`` narrows the sweep to the processes one process
    started -- the harness that spawned a server wants the daemon
    *that server* started, and not the services of the whole run.
    It narrows what the sweep *looks at*; it does not hand the sweep
    the right to signal a process whose owner is still there.

    *probe* and *alive* are the injection points the reaping itself
    uses; both default to the real thing.  *owner_alive* answers
    whether an entry's owner is still there; when it is not given, the
    pass's own liveness probe answers for the owner as well, and the
    real liveness check answers when there is no probe at all.
    """
    target = Path(path) if path is not None else spawn_registry_path()
    if target is None:
        return []
    entries = read_spawn_registry(target)
    if not entries:
        return []

    reaped: List[Dict[str, Any]] = []
    acted_on: set = set()
    owner_alive_fn = owner_alive
    if owner_alive_fn is None and alive is None:
        # The platform is asked through ``ps``, and a registry holds
        # many entries and few owners: one answer per owner per pass.
        owner_alive_fn = _memoised_owner_alive()
    for entry in entries:
        if owner_pid is not None:
            entry_owner = _entry_owner_pid(entry)
            if entry_owner != int(owner_pid):
                # Not this owner's process: it is not this pass's to
                # give back, and it stays in the registry.
                continue
        status = _reap_registry_entry(
            entry, grace_seconds=grace_seconds, reason=reason,
            probe=probe, alive=alive, owner_alive=owner_alive_fn,
        )
        if status == "reaped":
            reaped.append(entry)
        if status in ("reaped", "gone"):
            # Given back, or already gone: either way the registry must
            # not keep pointing at it.  "foreign", "unknown" and
            # "owned" are not in here -- the run has not established
            # that those processes are gone.
            acted_on.add(_entry_identity(entry))

    with _registry_lock(target):
        current = _read_spawn_registry_unlocked(target)
        survivors = [
            entry for entry in current
            if _entry_identity(entry) not in acted_on
        ]
        _write_spawn_registry(target, survivors)
    return reaped


# --------------------------------------------------------------------------
# Process inspection
# --------------------------------------------------------------------------
#
# Reading a process back is the other half of stamping a marker on it.
# The two halves do not meet in the same place on every platform:
#
# * **Linux** has ``/proc/<pid>/environ`` — exact, NUL-separated, and
#   readable for any process the caller may see.
# * **macOS** has no ``/proc``; ``sysctl kern.procargs2`` returns the
#   argument vector but *not* the environment (the oid is unknown on
#   Darwin 25.6), so ``ps -E`` is the only route.  ``ps -E`` prints the
#   command line and then the environment the process was *launched*
#   with, in one line, with nothing between them.
#
# Two consequences of the macOS route shape everything below:
#
# 1. **The command line has to be stripped off first.**  The environment
#    is what follows it, and the only way to know where it starts is to
#    ask a second time (``ps -p <pid> -o args=``) what the command line
#    was.
# 2. **An environment that cannot be read is not an empty one.**  Apple's
#    own binaries are protected by SIP: ``ps -E`` shows their command line
#    and no environment at all.  A read that produced output but not one
#    ``k=v`` segment therefore means *unreadable* — and must be reported
#    as such, never as an empty mapping.  An empty mapping would say
#    "this process carries no marker", which is a judgement this layer
#    is not entitled to make.


#: Whether this platform reads a process' environment through ``ps``.
#:
#: macOS has no ``/proc`` and no usable ``sysctl``; Linux has both.
_IS_DARWIN = sys.platform == "darwin"

#: A legal environment variable name.
#:
#: The macOS route hands back a blob that may still carry the tail of a
#: command line, and ``--cov=backend`` is an argument, not a variable.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The characters that make a blob need the shell lexer.
#:
#: A quote groups, a backslash escapes, and the whitespace characters
#: ``str.split`` treats as separators but ``shlex`` does not — a form
#: feed, a vertical tab, a non-breaking space — would make the two
#: disagree about where one token ends.  A blob with none of these is
#: split by whitespace alone, which is the lexer's own answer.
_NEEDS_SHELL_LEXER = re.compile(r"[^\S \t\r\n]|[\"'\\]")

#: The whitespace the shell lexer splits words on.
_SHLEX_WHITESPACE = " \t\r\n"

#: A run of ordinary characters: no whitespace, no quote, no backslash.
_PLAIN_RUN_RE = re.compile(r"[^ \t\r\n'\"\\]+")

#: A double-quoted section, up to its closing quote.
#:
#: ``\\["\\]`` is an escaped quote or an escaped backslash; ``\\.`` is a
#: backslash before anything else, which POSIX keeps — a ``\n`` inside
#: double quotes is a backslash and an ``n``, not a newline.
_DOUBLE_QUOTED_RE = re.compile(r'"((?:\\["\\]|\\.|[^"\\])*)"', re.DOTALL)

#: The escapes a double-quoted section resolves.
_DOUBLE_ESCAPE_RE = re.compile(r"\\([\"\\])")

#: The columns :func:`list_processes` asks ``ps`` for.
_PS_PROCESS_COLUMNS = "pid=,ppid=,pgid=,uid=,rss=,args="

#: The columns the one-shot environment read asks ``ps`` for.
#:
#: The pid and the command line are enough: the command line is what has
#: to be cut off the line before the environment behind it can be read,
#: and :func:`list_processes` already read it.
_PS_ENV_COLUMNS = "pid=,args="

#: A spawn marker, as the environment carries it: ``NAME=value``.
#:
#: Only the three markers the ladder reads are picked out of an
#: environment.  Every other variable is of no use to it, and reading
#: them all is what made the one-shot read the slowest thing in the boot
#: path: the environments of a whole machine are megabytes of text, and
#: lexing that costs seconds.
_SPAWN_MARKER_RE = re.compile(
    r"(?:^|\s)(?P<name>"
    + "|".join((SPAWN_OWNER_PID, SPAWN_OWNER_START, SPAWN_KIND))
    + r")=(?P<value>[^\s]*)"
)

#: Any environment variable at all -- the sign that an environment was read.
#:
#: A process the platform will not show the environment of (an Apple
#: platform binary, protected by SIP) prints its command line and nothing
#: else, so a segment with no assignment in it is *unreadable*, not empty.
_ENV_ASSIGNMENT_RE = re.compile(r"(?:^|\s)[A-Za-z_][A-Za-z0-9_]*=")

#: The wall-clock budget of one scan, in seconds.
#:
#: A scan has to be bounded.  The reaper runs on the boot path and every
#: thirty seconds in the daemon, so a scan that cannot finish is a scan
#: that keeps the machine busy instead of reclaiming it.  What the budget
#: does not reach is reported as *unreadable* -- a state the ladder
#: already treats as a skip, never as a kill.
SCAN_BUDGET_SECONDS = 10.0

#: The time the one-shot environment read is given whatever the budget has
#: left.
#:
#: It is one spawn whose cost does not grow with the machine, so it is
#: worth waiting for even when the budget is nearly spent: a read that
#: failed turns every process on the machine *unreadable*, and an
#: unreadable process is never a kill -- the reaper would stop reclaiming
#: anything at all.
_BATCH_READ_MIN_SECONDS = 5.0

#: A listening socket, as ``lsof`` prints it.
_LSOF_LISTEN_RE = re.compile(r"TCP\s+(?P<host>\S+):(?P<port>\d+)\s+\(LISTEN\)")

#: The process behind a socket, as ``ss`` prints it.
_SS_PID_RE = re.compile(r"pid=(\d+)")

#: The total and the used amount, as ``vm.swapusage`` prints them.
_SWAP_TOTAL_RE = re.compile(r"\btotal\s*=\s*([0-9.]+)\s*([KMGT]?)", re.IGNORECASE)
_SWAP_USED_RE = re.compile(r"\bused\s*=\s*([0-9.]+)\s*([KMGT]?)", re.IGNORECASE)

#: Byte multipliers for the units ``vm.swapusage`` uses.
_SWAP_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


@dataclass(frozen=True)
class ProcInfo:
    """A snapshot of one process.

    The record is what a scan sees: identity (``pid``), lineage
    (``ppid`` and ``pgid``), ownership (``uid``), memory (``rss_kb``) and
    the command line (``argv``).

    ``env`` is filled in only when the environment was actually read.
    ``None`` means *not read* — which is a different statement from *read
    and empty*, and the difference decides whether a process is merely
    unmarked or merely unknown.
    """

    pid: int
    ppid: int
    pgid: int
    uid: int
    rss_kb: int
    argv: str
    env: Optional[Dict[str, str]] = None


def _as_text(value: object) -> str:
    """Return *value* as text, whatever the tool handed back."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _tool_env() -> Dict[str, str]:
    """The environment a system tool is run with.

    ``LC_ALL=C`` keeps the output English and fixed-width whatever locale
    the reaper itself runs in — the parsers below read columns and month
    names, not prose.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _run_tool(
    args: List[str], timeout: float = 10.0
) -> Optional[Tuple[int, str, str]]:
    """Run *args* and return ``(returncode, stdout, stderr)``.

    ``None`` means the tool could not be run at all — it is missing, it
    could not be spawned, it timed out.  A tool that ran and failed is
    reported through its return code instead, because "the tool said no"
    and "the tool never ran" mean different things to the caller.

    The tool is spawned by :func:`_run_probe`, which keeps it out of
    the sight of a test that replaced ``subprocess.Popen`` to watch the
    product's own spawns.
    """
    result = _run_probe(list(args), timeout=timeout, env=_tool_env())
    if result is None:
        return None
    return result


def _run_ps(args: List[str], timeout: float = 10.0) -> Optional[str]:
    """Run ``ps`` with *args* and return its stdout, or ``None``.

    ``None`` means the answer is unusable: ``ps`` is missing, it failed,
    or it timed out.
    """
    result = _run_tool([_ps_binary()] + list(args), timeout=timeout)
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode != 0:
        return None
    return stdout


def _posix_split(text: str) -> List[str]:
    """Split *text* into shell words, the way ``shlex.split`` does.

    This is the same split as the shell lexer's, written out so that a
    word is built by appending to a list instead of by concatenating a
    string.  The lexer builds a word one character at a time, which is
    quadratic in the length of the word — and an environment is full of
    long values (a ``PATH`` that names a virtual environment is a
    thousand characters), so a machine with a thousand processes spends
    seconds in the lexer.

    The rules are the lexer's own:

    * whitespace separates words, and a run of it is one separator;
    * ``'`` quotes everything up to the next ``'`` literally;
    * ``"`` quotes everything up to the next ``"`` literally, except
      that ``\\"`` is the quote and ``\\\\`` is the backslash — any
      other backslash is kept, as POSIX keeps it;
    * outside quotes, ``\\`` is the escape for the next character;
    * a word that is nothing but an empty quoted string is a word.

    A quote that is never closed, and an escape at the very end, raise
    ``ValueError`` — as the lexer's do.
    """
    tokens: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in _SHLEX_WHITESPACE:
            index += 1
        if index >= length:
            break

        parts: List[str] = []
        while index < length:
            char = text[index]
            if char in _SHLEX_WHITESPACE:
                break
            if char == "'":
                end = text.find("'", index + 1)
                if end < 0:
                    raise ValueError("No closing quotation")
                parts.append(text[index + 1:end])
                index = end + 1
                continue
            if char == '"':
                match = _DOUBLE_QUOTED_RE.match(text, index)
                if match is None:
                    raise ValueError("No closing quotation")
                parts.append(_DOUBLE_ESCAPE_RE.sub(r"\1", match.group(1)))
                index = match.end()
                continue
            if char == "\\":
                if index + 1 >= length:
                    raise ValueError("No escaped character")
                parts.append(text[index + 1])
                index += 2
                continue
            match = _PLAIN_RUN_RE.match(text, index)
            parts.append(match.group())
            index = match.end()
        tokens.append("".join(parts))
    return tokens


def _split_env_blob(text: str) -> List[str]:
    """Split a blob of ``KEY=VALUE`` tokens.

    A blob with no quote, no backslash and no whitespace the lexer and
    ``str.split`` disagree about is split by whitespace alone.  That is
    the lexer's own answer, and it costs a fraction of it: the bulk
    read tokenises one blob per process on the machine, and on a
    thousand-process machine the lexer is the read's dominant cost.

    A blob that does need the lexer is split by :func:`_posix_split`,
    which is the lexer's own split.  A blob with an unbalanced quote —
    an apostrophe inside a value, say — falls back to a plain whitespace
    split rather than raising.
    """
    if _NEEDS_SHELL_LEXER.search(text) is None:
        return text.split()
    try:
        return _posix_split(text)
    except ValueError:
        return text.split()


def _parse_env_pairs(tokens: Iterable[str]) -> Dict[str, str]:
    """Turn ``KEY=VALUE`` tokens into a mapping.

    Only tokens whose key is a legal variable name are kept: the blob may
    still carry the tail of a command line, and ``--cov=backend`` is an
    argument rather than a variable.  The first occurrence of a name
    wins.
    """
    pairs: Dict[str, str] = {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator or not _ENV_NAME_RE.match(key):
            continue
        pairs.setdefault(key, value)
    return pairs


def _read_spawn_markers(text: str) -> Tuple[bool, Dict[str, str]]:
    """Return whether *text* holds an environment, and the markers in it.

    The first half of the answer is the important one.  A segment with no
    variable assignment in it is not an environment that carries no
    marker -- it is an environment that could not be read at all, and the
    ladder treats the two differently: one is a candidate, the other is
    never a kill.  The difference has to survive the one-shot read.

    Only the markers are returned.  The ladder reads no value but a
    marker's, and the rest of an environment is of no use to anybody here.
    """
    if not _ENV_ASSIGNMENT_RE.search(text):
        return False, {}

    markers: Dict[str, str] = {}
    for match in _SPAWN_MARKER_RE.finditer(text):
        markers.setdefault(match.group("name"), match.group("value"))
    return True, markers


def _proc_environ(pid: int) -> Optional[Dict[str, str]]:
    """Read ``/proc/<pid>/environ`` (Linux).

    The file is NUL-separated and exact, so the values are taken as they
    are: no tokenising, no whitespace damage.
    """
    try:
        with open("/proc/%d/environ" % pid, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None

    chunks = []
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        chunks.append(chunk.decode("utf-8", "replace"))

    pairs = _parse_env_pairs(chunks)
    return pairs or None


def _environ_from_darwin_line(
    line: str, args: str
) -> Optional[Dict[str, str]]:
    """Read the environment out of one ``ps -E`` line.

    ``ps -E`` prints the command line and then the environment the
    process was launched with; the two are separated by nothing, so the
    command line is stripped off first.  *args* is that command line —
    the text that has to go — and the environment is whatever follows
    it.

    A process whose environment cannot be read — an Apple platform
    binary, protected by SIP — yields a line with no ``k=v`` segment at
    all.  That is *unreadable*, not *empty*, and it is reported as
    ``None``.
    """
    if "=" not in line:
        # The line carries no "=" at all, so it can hold no variable —
        # not in the segment below, and not in the whole line either.
        # Most process lines on a macOS machine are like this, and the
        # answer is the same whether the blob is tokenised or not.
        return None

    segment = line
    if args:
        index = line.find(args)
        if index >= 0:
            segment = line[index + len(args):]

    pairs = _parse_env_pairs(_split_env_blob(segment))
    if not pairs:
        # The command line could not be located inside the line — a
        # ``ps`` that answered both questions alike, say.  A marker that
        # is in the output is a marker, wherever in the line it sits.
        pairs = _parse_env_pairs(_split_env_blob(line))

    return pairs or None


def _darwin_environ(pid: int) -> Optional[Dict[str, str]]:
    """Read a process' environment through ``ps -E`` (macOS).

    ``ps -E`` prints the command line and then the environment the
    process was launched with; the two are separated by nothing, so the
    command line is stripped off first.  What the command line was is
    read from a second ``ps`` (``-o args=``), which reports exactly the
    text that has to go.

    A process whose environment cannot be read — an Apple platform
    binary, protected by SIP — yields a line with no ``k=v`` segment at
    all.  That is *unreadable*, not *empty*, and it is reported as
    ``None``.
    """
    output = _run_ps(["-E", "-p", str(pid)])
    if not output:
        return None

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[-1]

    args_output = _run_ps(["-p", str(pid), "-o", "args="])
    args = args_output.strip() if args_output else ""

    return _environ_from_darwin_line(line, args)


def _parse_darwin_env_table(text: str) -> Dict[int, str]:
    """Parse a ``ps -E -A`` listing into ``{pid: the line it printed}``.

    Every line is a pid, a tty, a cpu time, and then the command line
    followed by the environment the process was launched with.  The
    header and any line that does not start with a number are skipped:
    one unreadable process must not cost the table the other thousand.
    """
    table: Dict[int, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split(None, 3)
        if len(fields) < 4:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        table[pid] = line
    return table


def _darwin_environ_bulk(
    procs: Sequence["ProcInfo"],
) -> Optional[Dict[int, Dict[str, str]]]:
    """Read many processes' environments with one ``ps -E -A``.

    The per-process read asks ``ps`` twice per process: once for the
    command line and the environment, once for the command line alone.
    On a machine with a thousand processes that is two thousand
    subprocesses, and the scan then takes longer than the processes it
    is judging — by the time it reaches one, that process has exited,
    and its environment reads back as *unreadable* rather than as the
    unmarked environment it was.

    One ``ps -E -A`` answers for every process at the same moment.  The
    command line the process table already carries says where the
    environment starts inside the line, so the environment that comes
    after it belongs to the process that was asked about.

    ``None`` means the table could not be read at all — the caller then
    reads the environments one process at a time.
    """
    output = _run_ps(["-E", "-A"], timeout=30.0)
    if not output:
        return None

    table = _parse_darwin_env_table(output)
    envs: Dict[int, Dict[str, str]] = {}
    for proc in procs:
        line = table.get(proc.pid)
        if line is None:
            # Not in the table: the process is gone, or the platform
            # will not show it.  Either way there is no environment to
            # read, and an absent entry says exactly that.
            continue
        env = _environ_from_darwin_line(line, proc.argv)
        if env:
            envs[proc.pid] = env
    return envs


#: How many processes a scan must be looking at before the bulk read
#: beats the per-process one.
#:
#: One ``ps -E -p <pid>`` plus one ``ps -p <pid> -o args=`` costs about
#: 30 ms each on macOS, so the per-process read costs roughly 60 ms per
#: process; one ``ps -E -A`` for the whole machine costs about 1.7 s
#: (measured on a 940-process machine).  The two cross at about 28
#: processes, and 32 is the round number above that line.  Below it the
#: per-process read is both cheaper and more precise, so it is kept.
BULK_ENV_MIN_PROCESSES = 32


def read_process_environ_bulk(
    procs: Sequence["ProcInfo"],
) -> Optional[Dict[int, Dict[str, str]]]:
    """Read the environments of *procs* in as few probes as possible.

    The returned mapping holds the processes whose environment could be
    **read**: a process that is absent from it carries no environment
    this layer is allowed to report, which is a different statement from
    "carries an empty environment".

    ``None`` means *no bulk read was made* — the caller reads the
    environments one process at a time instead.  That is the answer on
    every platform but macOS (where ``/proc`` reads are cheap and
    exact), and on macOS whenever the scan is looking at few enough
    processes that the per-process read is the cheaper of the two.
    """
    if not _IS_DARWIN:
        return None
    if len(procs) < BULK_ENV_MIN_PROCESSES:
        return None
    return _darwin_environ_bulk(procs)


def read_process_environ(pid: int) -> Optional[Dict[str, str]]:
    """Return the environment *pid* was launched with, or ``None``.

    ``None`` means *could not be read*: the process is gone, the probe
    failed, or the platform will not show it.  It never means "the
    process has no environment" — an empty mapping would be read
    downstream as "carries no marker", which is a judgement this layer
    is not allowed to make.

    What is returned is the environment the process was *launched* with.
    A variable set afterwards does not show up (``ps(1)``: "does not
    reflect changes in the environment after process launch"), which is
    why the positive control has to be a child that was launched with
    the marker in the first place.

    This is the per-process route, and it costs two ``ps`` spawns per
    process on macOS.  A scan that reads the whole machine through it
    pays for every process on the box — which is why the scan reads the
    machine in one spawn instead (see :func:`_darwin_environ_batch`).
    """
    try:
        target = int(pid)
    except (TypeError, ValueError):
        return None
    if target <= 0:
        return None
    try:
        if _IS_DARWIN:
            return _darwin_environ(target)
        return _proc_environ(target)
    except Exception:  # pragma: no cover - the contract is "never raises"
        return None


def _darwin_environ_batch(
    procs: Sequence[ProcInfo],
    *,
    timeout: float = 15.0,
) -> Dict[int, Optional[Dict[str, str]]]:
    """Read the environment of every process on the machine — ONE ``ps``.

    ``ps -E -axo pid=,args=`` prints one line per process: the pid, the
    command line, and then the environment the process was launched with.
    The command line is the very text :func:`list_processes` already
    read, so it can be cut off the line without asking ``ps`` about it a
    second time — which is the whole point of this function.  The
    per-process route cost two spawns per process, so a scan of a machine
    with 833 processes cost 1666 spawns and 47 seconds; and the scan runs
    on the boot path, where every one of those seconds is a second the
    port spends not answering.

    A process whose environment cannot be read — an Apple platform
    binary, protected by SIP — has no ``k=v`` segment at all.  That is
    *unreadable*, not *empty*, and it is reported as ``None``, exactly as
    the per-process route reports it.

    What is returned for a readable environment are the spawn markers and
    nothing else: the ladder reads no value but a marker's, and picking
    them out of megabytes of text is what keeps the read cheap.

    The map is keyed by pid; a pid the batch did not mention is absent
    from it, which the caller reads as unreadable as well.
    """
    output = _run_ps(["-E", "-axo", _PS_ENV_COLUMNS], timeout=timeout)
    if not output:
        return {}

    args_by_pid = {proc.pid: proc.argv for proc in procs}
    envs: Dict[int, Optional[Dict[str, str]]] = {}

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, rest = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue

        argv = args_by_pid.get(pid)
        segment = rest
        if argv:
            index = rest.find(argv)
            if index >= 0:
                segment = rest[index + len(argv):]

        pairs = _read_spawn_markers(segment)
        if not pairs[0]:
            # The command line could not be located inside the line — a
            # ``ps`` that answered both questions alike, say.  A marker
            # that is in the output is a marker, wherever in the line it
            # sits.
            pairs = _read_spawn_markers(rest)

        readable, markers = pairs
        envs[pid] = markers if readable else None

    return envs


def _proc_environ_batch(
    procs: Sequence[ProcInfo],
    *,
    timeout: float,
) -> Dict[int, Optional[Dict[str, str]]]:
    """Read the environment of every process in *procs* — no spawn at all.

    This is the Linux counterpart of :func:`_darwin_environ_batch`: the
    same contract — one pass over the machine, before the walk begins —
    with the platform's own answer.  ``/proc/<pid>/environ`` is a file,
    so the read costs no process at all, and the value it gives is the
    one :func:`_proc_environ` gives, which is the value the marker was
    written from.

    A process whose environment cannot be read is reported as ``None``,
    exactly as the per-process route reports it: *unreadable*, never
    *empty*.  A pid the pass did not reach — the pass ran out of its
    time — is absent from the map, which the caller reads as unreadable
    as well.
    """
    envs: Dict[int, Optional[Dict[str, str]]] = {}
    deadline = time.monotonic() + max(0.0, timeout)
    for proc in procs:
        envs[proc.pid] = _proc_environ(proc.pid)
        if time.monotonic() > deadline:
            break
    return envs


def _default_env_source(
    procs: Sequence[ProcInfo],
    *,
    deadline: float,
) -> Tuple[Callable[[int], Optional[Dict[str, str]]], bool]:
    """Return the per-pid environment reader a scan should use.

    On macOS the whole machine's environments are read in ONE spawn; on
    Linux ``/proc`` answers the whole machine in ONE pass, without a
    spawn at all.  When the batch read is not available the per-process
    route is used: it is slower, and it says the same thing.

    The batch read is given at least :data:`_BATCH_READ_MIN_SECONDS`
    whatever the budget has left: it is ONE pass, its cost does not grow
    with the machine, and a budget that had already been spent by the two
    probes before it must not turn the whole machine *unreadable* — which
    is a skip, so the reaper would stop reclaiming anything.

    The second half of the answer says whether the environments were read
    **before** the scan's walk began.  A source that was read in one go
    answers from what it already holds, so the walk does no work and the
    budget has nothing left to bound; a source that reads on demand is
    bounded by it.  The distinction matters: a budget that expired during
    the setup must not turn environments that *were* read into
    environments that were not.
    """
    remaining = deadline - time.monotonic()
    timeout = max(_BATCH_READ_MIN_SECONDS, remaining)
    if _IS_DARWIN:
        envs = _darwin_environ_batch(procs, timeout=timeout)
    else:
        envs = _proc_environ_batch(procs, timeout=timeout)
    if envs:
        def _from_batch(pid: int) -> Optional[Dict[str, str]]:
            return envs.get(pid)

        return _from_batch, True
    return read_process_environ, False


def _parse_ps_process_table(text: str) -> List[ProcInfo]:
    """Parse the output of ``ps -axo pid=,ppid=,pgid=,uid=,rss=,args=``.

    The command line is the last column and keeps its spaces, so the
    line is split into six fields and no further.  A line that does not
    parse is skipped: one unreadable process must not cost the scan the
    other thousand.
    """
    processes: List[ProcInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        try:
            processes.append(
                ProcInfo(
                    pid=int(fields[0]),
                    ppid=int(fields[1]),
                    pgid=int(fields[2]),
                    uid=int(fields[3]),
                    rss_kb=int(fields[4]),
                    argv=fields[5],
                )
            )
        except ValueError:
            continue
    return processes


def list_processes() -> List[ProcInfo]:
    """Return a snapshot of the processes running on this machine.

    A failing probe yields an empty list, and an empty list means the
    scan finds no candidates — the outcome that reclaims nothing, which
    is the right outcome for a scan that could not see the machine.
    """
    output = _run_ps(["-axo", _PS_PROCESS_COLUMNS], timeout=15.0)
    if not output:
        return []
    return _parse_ps_process_table(output)


def _parse_lsof_listeners(text: str) -> Dict[int, List[int]]:
    """Parse ``lsof`` output into a pid → listening ports mapping."""
    mapping: Dict[int, List[int]] = {}
    for line in text.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 2:
            continue
        try:
            owner = int(fields[1])
        except ValueError:
            continue
        match = _LSOF_LISTEN_RE.search(line)
        if match is None:
            continue
        port = int(match.group("port"))
        ports = mapping.setdefault(owner, [])
        if port not in ports:
            ports.append(port)
    return {owner: sorted(ports) for owner, ports in mapping.items()}


def _darwin_listening_ports() -> Optional[Dict[int, List[int]]]:
    """Map pids to their listening TCP ports using ``lsof`` (macOS)."""
    result = _run_tool(
        [shutil.which("lsof") or "lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
        timeout=15.0,
    )
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode == 1 and not stdout.strip():
        # ``lsof`` exits 1 when nothing matched — an empty result, not a
        # failed probe.
        return {}
    if returncode != 0:
        return None
    return _parse_lsof_listeners(stdout)


def _parse_ss_listeners(text: str) -> Dict[int, List[int]]:
    """Parse ``ss`` output into a pid → listening ports mapping."""
    mapping: Dict[int, List[int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            port = int(fields[3].rsplit(":", 1)[-1])
        except ValueError:
            continue
        match = _SS_PID_RE.search(line)
        if match is None:
            continue
        owner = int(match.group(1))
        ports = mapping.setdefault(owner, [])
        if port not in ports:
            ports.append(port)
    return {owner: sorted(ports) for owner, ports in mapping.items()}


def _linux_listening_ports() -> Optional[Dict[int, List[int]]]:
    """Map pids to their listening TCP ports using ``ss`` (Linux)."""
    result = _run_tool(
        [shutil.which("ss") or "ss", "-Hlnpt"], timeout=15.0
    )
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode != 0:
        return None
    return _parse_ss_listeners(stdout)


def listening_ports_by_pid(
    pid: Optional[int] = None,
) -> Optional[Dict[int, List[int]]]:
    """Return the TCP ports each process listens on, keyed by pid.

    The mapping answers the question a scan has to ask before it kills
    anything: *does this process hold a protected port?*  A process that
    holds one is never a candidate.

    ``None`` means the probe **failed**, and the caller must then fail
    closed — a port that could not be checked is not a port that is
    absent.  An empty mapping means the probe worked and found nothing.

    Passing *pid* narrows the answer to that one process; the shape of
    the return value does not change.
    """
    try:
        if _IS_DARWIN:
            mapping = _darwin_listening_ports()
        else:
            mapping = _linux_listening_ports()
    except Exception:  # pragma: no cover - the contract is "never raises"
        return None

    if mapping is None:
        return None
    if pid is None:
        return mapping
    try:
        wanted = int(pid)
    except (TypeError, ValueError):
        return {}
    ports = mapping.get(wanted)
    return {wanted: ports} if ports else {}


def _darwin_swap_ratio() -> Optional[float]:
    """Read ``vm.swapusage`` (macOS)."""
    result = _run_tool(
        [shutil.which("sysctl") or "sysctl", "-n", "vm.swapusage"], timeout=5.0
    )
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode != 0:
        return None

    total_match = _SWAP_TOTAL_RE.search(stdout)
    used_match = _SWAP_USED_RE.search(stdout)
    if total_match is None or used_match is None:
        return None

    total = float(total_match.group(1)) * _SWAP_UNITS[total_match.group(2).upper()]
    used = float(used_match.group(1)) * _SWAP_UNITS[used_match.group(2).upper()]
    if total <= 0:
        return None
    return used / total


def _meminfo_value(line: str) -> Optional[float]:
    """The numeric value of a ``/proc/meminfo`` line, in kB."""
    parts = line.split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[1])
    except ValueError:
        return None


def _proc_swap_ratio() -> Optional[float]:
    """Read the swap figures from ``/proc/meminfo`` (Linux)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None

    total = None
    free = None
    for line in text.splitlines():
        if line.startswith("SwapTotal:"):
            total = _meminfo_value(line)
        elif line.startswith("SwapFree:"):
            free = _meminfo_value(line)
        if total is not None and free is not None:
            break

    if total is None or free is None or total <= 0:
        return None
    return (total - free) / total


def swap_usage_ratio() -> Optional[float]:
    """Return the fraction of the swap that is in use, or ``None``.

    The value is a fraction between 0 and 1; compare it with the
    configured percentage threshold (:data:`DEFAULT_SWAP_THRESHOLD`) after
    multiplying by 100.  ``None`` means the figure could not be read,
    which the caller has to treat as *unknown* — never as "no swap in
    use".
    """
    try:
        if _IS_DARWIN:
            return _darwin_swap_ratio()
        return _proc_swap_ratio()
    except Exception:  # pragma: no cover - the contract is "never raises"
        return None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def default_plans_root(environ: Optional[Mapping[str, str]] = None) -> Path:
    """Return the plan-artifact root the reaper should work under.

    ``PDT_PLANS_DIR`` wins — it is the escape hatch the rest of the
    backend already honours (see ``config_paths.resolve_plans_dir``) —
    and the repository's own ``plans/`` directory is the fallback.

    The fallback is derived from this file's own location rather than
    from the working directory, so the reaper finds the same root
    whether it is started from the repository root, from ``backend/``,
    or as an installed module.
    """
    env = os.environ if environ is None else environ
    override = env.get(ENV_PLANS_DIR)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return Path(__file__).resolve().parents[1] / "plans"


@dataclass
class ReaperConfig:
    """Everything the reaper needs to decide and to act.

    The defaults are the safe values: enabled but conservative, and
    pointed at a port set that includes every service PDT ships.
    """

    #: Whether the reaper may act at all.
    enabled: bool = True
    #: Seconds between scans.
    interval: int = DEFAULT_INTERVAL
    #: Ports whose listeners are never reaped (defaults + configured).
    protect_ports: FrozenSet[int] = DEFAULT_PROTECT_PORTS
    #: Seconds between SIGTERM and SIGKILL.
    grace: float = DEFAULT_GRACE
    #: Seconds of complete idleness before the daemon exits itself.
    idle_exit: int = DEFAULT_IDLE_EXIT
    #: Hard ceiling on the daemon's own lifetime, in seconds.  Zero
    #: disables the ceiling.
    max_lifetime: int = DEFAULT_MAX_LIFETIME
    #: Where plan artifacts (and the audit log) live.
    plans_root: Path = field(default_factory=default_plans_root)
    #: Decide but never signal.
    dry_run: bool = False
    #: Candidate count above which the box is treated as an emergency.
    burst_threshold: int = DEFAULT_BURST_THRESHOLD
    #: Swap usage percent above which the box is treated as an emergency.
    swap_threshold: float = DEFAULT_SWAP_THRESHOLD
    #: When set, only these pids are considered (a testing scope gate).
    pid_scope: Optional[Tuple[int, ...]] = None
    #: The pid of the process that started this daemon.
    #:
    #: The daemon leaves when that process is gone: a daemon whose
    #: parent has died has outlived the only reason it existed.  It is
    #: ``None`` when the daemon was not told who its owner is.
    owner_pid: Optional[int] = None
    #: The start time of that owner — tells a live owner from a
    #: recycled pid (see :func:`_same_start`).
    owner_start: str = ""
    #: Whether the daemon is allowed to outlive its owner.
    #:
    #: The daemon the *server* starts is the one exception: it exists
    #: for the case where the server dies, so it is exempted
    #: explicitly rather than by accident.
    survive_owner: bool = False
    #: Whether the daemon may outlive the process that started it.
    #:
    #: This is the **decision**; the caller's request is only a
    #: request.  ``None`` — the default — means the configuration does
    #: not speak about it, and the caller's request stands.  ``False``
    #: is a refusal: the daemon leaves with the process that started
    #: it, whatever that process asked for.  ``True`` is the
    #: permission.
    #:
    #: A configuration read from the environment says ``False`` unless
    #: :data:`ENV_DETACH_OWNER` says otherwise.  The default is the
    #: safe one: a daemon that outlives the process that asked for it
    #: has nothing left to do, and the twenty-four daemons that were
    #: walking one machine on 2026-11-02 all began here.
    detach_owner: Optional[bool] = None
    #: The most reaper daemons that may run on this machine at once.
    #:
    #: The lock makes one daemon per *root*; this makes one per
    #: *machine*.  Zero disables the ceiling, which is how a
    #: deployment that wants a daemon on every root says so.
    max_daemons: int = DEFAULT_MAX_DAEMONS


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def _env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _env_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _env_ports(env: Mapping[str, str], name: str) -> FrozenSet[int]:
    """Parse a comma separated port list, ignoring anything unusable."""
    raw = env.get(name)
    if raw is None:
        return frozenset()
    ports = set()
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            port = int(chunk)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return frozenset(ports)


def _env_pid_scope(
    env: Mapping[str, str], name: str
) -> Optional[Tuple[int, ...]]:
    """Parse the pid scope gate.

    ``None`` means "no scope configured" — the reaper looks at
    everything it is allowed to look at.  An explicitly configured scope
    that contains no usable pid is an *empty* scope, not an absent one:
    a typo in a scope must not widen the blast radius.
    """
    raw = env.get(name)
    if raw is None:
        return None
    pids: List[int] = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            pid = int(chunk)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    return tuple(pids)


def config_from_env(environ: Optional[Mapping[str, str]] = None) -> ReaperConfig:
    """Build a :class:`ReaperConfig` from the environment.

    Every value is optional and every malformed value falls back to its
    default rather than raising: a reaper that will not start because
    of a typo would leave the memory it exists to reclaim unreclaimed.

    The configured protect ports are **added** to
    :data:`DEFAULT_PROTECT_PORTS`, never substituted for them — the
    defaults protect PDT's own services, and a deployment adding a port
    is adding protection, not redefining it.
    """
    env = os.environ if environ is None else environ

    return ReaperConfig(
        enabled=_env_bool(env, ENV_ENABLED, True),
        interval=_env_int(env, ENV_INTERVAL, DEFAULT_INTERVAL, minimum=1),
        protect_ports=frozenset(
            DEFAULT_PROTECT_PORTS | _env_ports(env, ENV_PROTECT_PORTS)
        ),
        grace=_env_float(env, ENV_GRACE, DEFAULT_GRACE, minimum=0.0),
        idle_exit=_env_int(env, ENV_IDLE_EXIT, DEFAULT_IDLE_EXIT, minimum=0),
        max_lifetime=_env_int(
            env, ENV_MAX_LIFETIME, DEFAULT_MAX_LIFETIME, minimum=0
        ),
        plans_root=default_plans_root(env),
        dry_run=_env_bool(env, ENV_DRY_RUN, False),
        burst_threshold=_env_int(
            env, ENV_BURST_THRESHOLD, DEFAULT_BURST_THRESHOLD, minimum=1
        ),
        swap_threshold=_env_float(
            env, ENV_SWAP_THRESHOLD, DEFAULT_SWAP_THRESHOLD, minimum=0.0, maximum=100.0
        ),
        pid_scope=_env_pid_scope(env, ENV_PID_SCOPE),
        max_daemons=_env_int(
            env, ENV_MAX_DAEMONS, DEFAULT_MAX_DAEMONS, minimum=0
        ),
        detach_owner=_env_bool(env, ENV_DETACH_OWNER, False),
    )


# --------------------------------------------------------------------------
# The verdict ladder
# --------------------------------------------------------------------------
#
# The ladder is the only thing in this module that can lead to a kill,
# and it is shaped so that **every rung can only stop the kill**.  There
# is no rung that turns a skip into a kill: the only way to reach the
# last rung is to have passed all the others, and the last rung is the
# only one that may lead to a signal.
#
# The rungs, in the order they are walked:
#
# ========================  =============================================
# ``other_uid``             the process belongs to another user
# ``self``                  pid 1, or this very process
# ``env_unreadable``        the environment could not be read
# ``unmarked``              no ``PDT_SPAWN_*`` marker at all
# ``owner_alive``           the owner is alive and is who it claims to be
# ``owner_alive_unverified`` the owner is alive but could not be compared
# ``pid_reused``            the owner pid is alive but is another process
# ``ports_unknown``         the listening ports could not be checked
# ``protected_port``        it holds a protected port
# ``self_reaper``           it is a reaper
# ``orphan``                everything else — the only reason to kill
# ========================  =============================================
#
# Two of those rungs are *degradations* rather than judgements: an
# environment that could not be read is **unknown**, not empty, and a
# port map that could not be read is **unknown**, not empty.  Both
# resolve towards not killing, because "we could not tell" is not
# evidence of anything — and mistaking it for evidence is exactly how
# an unrelated process ends up on the kill list.
#
# Everything here is a **pure** function: it reads what it is given and
# returns a verdict.  It sends no signal, writes no file and mutates no
# global state.  That is what makes the ladder testable at all — the
# positive control (a live owner) and the negative control (a dead one)
# are two lines of fixture rather than two subprocesses.


#: The candidate may be killed (the only kind of verdict that may).
DECISION_KILL = "kill"

#: The candidate is left alone.
DECISION_SKIP = "skip"

#: The candidate carries no ``PDT_SPAWN_*`` marker at all.
REASON_UNMARKED = "unmarked"

#: The candidate's environment could not be read.
REASON_ENV_UNREADABLE = "env_unreadable"

#: The candidate belongs to another user.
REASON_OTHER_UID = "other_uid"

#: The candidate is pid 1, or this very process.
REASON_SELF = "self"

#: The candidate is another reaper.
REASON_SELF_REAPER = "self_reaper"

#: The owner is alive and its start time matches the recorded one.
REASON_OWNER_ALIVE = "owner_alive"

#: The owner is alive, but the two start times could not be compared.
REASON_OWNER_ALIVE_UNVERIFIED = "owner_alive_unverified"

#: The owner pid is alive but belongs to a different process.
REASON_PID_REUSED = "pid_reused"

#: The owner could not be checked at all.
REASON_OWNER_UNKNOWN = "owner_unknown"

#: The candidate's listening ports could not be checked.
REASON_PORTS_UNKNOWN = "ports_unknown"

#: The candidate holds a protected port.
REASON_PROTECTED_PORT = "protected_port"

#: The candidate is an orphan — the only reason that leads to a kill.
REASON_ORPHAN = "orphan"


@dataclass(frozen=True)
class OwnerState:
    """What is known about the process that claimed to have spawned a candidate.

    ``alive`` says whether that pid is still running; ``start_epoch`` is
    the owner's start time as the operating system reports it *now*.
    Together they answer the only question that matters: *is this the
    same process that spawned the candidate, or a different one that
    happens to carry the same number?*

    An empty ``start_epoch`` means the start time could not be
    determined — which is *unknown*, never "different".
    """

    alive: bool = False
    start_epoch: str = ""


@dataclass
class Verdict:
    """The judgement about one candidate process.

    The record is deliberately flat: the process it is about, what was
    decided, why, and the marker that led there.  ``signal`` is empty
    until the signalling stage acts on the verdict — the ladder itself
    never sends anything.
    """

    proc: ProcInfo
    decision: str
    reason: str
    owner_pid: Optional[int] = None
    owner_start: str = ""
    kind: str = ""
    signal: str = ""


@dataclass
class ScanResult:
    """Everything one pass over the machine decided.

    ``owners_alive`` counts the marked processes whose owner is still
    alive — the ones that are *in use*.  It is what tells the daemon
    that PDT is still running, and it is what must reach zero before a
    quiet machine may be left alone.
    """

    verdicts: List[Verdict] = field(default_factory=list)
    owners_alive: int = 0


def _current_uid() -> int:
    """The uid this process runs as (``-1`` where the platform has none)."""
    getter = getattr(os, "getuid", None)
    return -1 if getter is None else getter()


def _markers(env: Mapping[str, str]) -> Tuple[Optional[int], str, str]:
    """Return the ``(owner_pid, owner_start, kind)`` a candidate carries.

    A marker that cannot be read is read as *absent*: an
    ``PDT_SPAWN_OWNER_PID`` that is not a number names no process, and
    the candidate is then simply unmarked — which is a skip, never a
    kill.
    """
    raw_pid = str(env.get(SPAWN_OWNER_PID) or "").strip()
    owner_pid: Optional[int] = None
    if raw_pid:
        try:
            parsed = int(raw_pid)
        except ValueError:
            parsed = 0
        if parsed > 0:
            owner_pid = parsed
    owner_start = str(env.get(SPAWN_OWNER_START) or "").strip()
    kind = str(env.get(SPAWN_KIND) or "").strip().lower()
    return owner_pid, owner_start, kind


def _same_start(recorded: str, observed: str, tolerance: float = 0.0) -> bool:
    """True when two start times describe the same instant.

    The values are epoch seconds rendered as text, so they are compared
    numerically when they can be; anything else is compared literally.
    A comparison that cannot be made is *not* a match — that direction
    only ever leads to a skip.

    *tolerance* is how far apart the two readings may be and still
    describe one instant, expressed in the unit the values are in (see
    :func:`_start_tolerance`).  It is zero by default: a caller holding
    two readings of the same field needs no slack, and a caller that
    does not hold them must not be given any by accident.
    """
    try:
        return abs(float(recorded) - float(observed)) <= tolerance
    except (TypeError, ValueError):
        return recorded == observed


def classify(
    proc: ProcInfo,
    *,
    config: Optional[ReaperConfig] = None,
    owner_state: Optional[OwnerState] = None,
    port_map: Optional[Mapping[int, Iterable[int]]] = None,
    self_uid: Optional[int] = None,
) -> Verdict:
    """Judge one candidate process.  Pure: it decides, it does not act.

    *proc* is the process as the scan saw it, including its environment
    (``proc.env``); ``owner_state`` is what is known about the process
    that claims to have spawned it; ``port_map`` maps pids to the TCP
    ports they listen on.

    Two of the arguments say *unknown* rather than *absent*, and the
    difference decides the verdict: an ``owner_state`` of ``None`` means
    the owner could not be checked, and a ``port_map`` of ``None`` means
    the ports could not be checked.  Both skip.

    The result is a :class:`Verdict`; ``decision`` is ``"kill"`` only
    for ``REASON_ORPHAN``.
    """
    settings = config if config is not None else ReaperConfig()
    uid = _current_uid() if self_uid is None else int(self_uid)

    # -- The gates, before the ladder ------------------------------------
    # The reaper must never kill itself, and it must never kill pid 1.
    if proc.pid <= 1 or proc.pid == os.getpid():
        return Verdict(proc=proc, decision=DECISION_SKIP, reason=REASON_SELF)

    # Only processes that belong to the user we are running as are ours
    # to consider at all.
    if proc.uid != uid:
        return Verdict(proc=proc, decision=DECISION_SKIP, reason=REASON_OTHER_UID)

    # -- The ladder ------------------------------------------------------
    # Rung 1: the environment is the only evidence there is.  An
    # environment that could not be read is *unknown*, not empty.
    env = proc.env
    if env is None:
        return Verdict(proc=proc, decision=DECISION_SKIP, reason=REASON_ENV_UNREADABLE)

    owner_pid, owner_start, kind = _markers(env)
    if owner_pid is None:
        return Verdict(proc=proc, decision=DECISION_SKIP, reason=REASON_UNMARKED)

    # Rung 2: an owner that could not be checked is not a dead owner.
    if owner_state is None:
        return Verdict(
            proc=proc,
            decision=DECISION_SKIP,
            reason=REASON_OWNER_UNKNOWN,
            owner_pid=owner_pid,
            owner_start=owner_start,
            kind=kind,
        )

    if owner_state.alive:
        # Rung 3: the owner is alive.  It is only *our* owner if the
        # recorded start time is the one the owner has now.
        if not owner_start or not owner_state.start_epoch:
            return Verdict(
                proc=proc,
                decision=DECISION_SKIP,
                reason=REASON_OWNER_ALIVE_UNVERIFIED,
                owner_pid=owner_pid,
                owner_start=owner_start,
                kind=kind,
            )
        if not _same_start(owner_start, owner_state.start_epoch):
            # Rung 4: the pid was recycled — this is somebody else's
            # process that happens to carry the same number.
            return Verdict(
                proc=proc,
                decision=DECISION_SKIP,
                reason=REASON_PID_REUSED,
                owner_pid=owner_pid,
                owner_start=owner_start,
                kind=kind,
            )
        return Verdict(
            proc=proc,
            decision=DECISION_SKIP,
            reason=REASON_OWNER_ALIVE,
            owner_pid=owner_pid,
            owner_start=owner_start,
            kind=kind,
        )

    # The owner is dead.  From here on only the remaining rungs can stop
    # the kill.
    #
    # Rung 5: a port map that could not be read is *unknown*, not empty.
    if port_map is None:
        return Verdict(
            proc=proc,
            decision=DECISION_SKIP,
            reason=REASON_PORTS_UNKNOWN,
            owner_pid=owner_pid,
            owner_start=owner_start,
            kind=kind,
        )

    # Rung 6: a process holding a protected port is a service somebody
    # may be using.
    ports = port_map.get(proc.pid) or ()
    if settings.protect_ports.intersection(ports):
        return Verdict(
            proc=proc,
            decision=DECISION_SKIP,
            reason=REASON_PROTECTED_PORT,
            owner_pid=owner_pid,
            owner_start=owner_start,
            kind=kind,
        )

    # Rung 7: a reaper never reaps a reaper.
    if kind == KIND_REAPER:
        return Verdict(
            proc=proc,
            decision=DECISION_SKIP,
            reason=REASON_SELF_REAPER,
            owner_pid=owner_pid,
            owner_start=owner_start,
            kind=kind,
        )

    # Rung 8: everything else is an orphan.
    return Verdict(
        proc=proc,
        decision=DECISION_KILL,
        reason=REASON_ORPHAN,
        owner_pid=owner_pid,
        owner_start=owner_start,
        kind=kind,
    )


def probe_owner(
    pid: int,
    *,
    start_epochs: Optional[Mapping[int, str]] = None,
) -> Optional[OwnerState]:
    """Return the state of the process that claimed to own a candidate.

    ``None`` means *could not be checked* — the caller must then skip
    the candidate, because an unchecked owner is not a dead owner.

    A pid that exists but belongs to somebody else counts as alive:
    the kernel answered, and the process is running.

    *start_epochs* holds start times that were already read — the scan
    reads them for every owner at once, because one ``ps`` per owner is
    one ``ps`` per process on the machine.  The map is authoritative: a
    pid it does not mention is a pid whose start time could not be
    read, and the answer says *unknown* (an empty ``start_epoch``),
    which the ladder treats as a skip and never as a kill.  Without the
    map the start time is read one pid at a time, as it always was.
    """
    try:
        target = int(pid)
    except (TypeError, ValueError):
        return None
    if target <= 0:
        return None
    try:
        os.kill(target, 0)
    except ProcessLookupError:
        return OwnerState(alive=False, start_epoch="")
    except PermissionError:
        # It exists; it simply is not ours to inspect.
        return OwnerState(
            alive=True, start_epoch=_owner_epoch(target, start_epochs)
        )
    except OSError:
        return None
    return OwnerState(alive=True, start_epoch=_owner_epoch(target, start_epochs))


def _owner_epoch(target: int, start_epochs: Optional[Mapping[int, str]]) -> str:
    """The start time of *target*, from the batch when there is one.

    A batch that ran is authoritative: a pid it does not mention is a
    pid whose start time could not be read, and *saying so* is cheaper
    than asking ``ps`` about it again — one ask per pid is the cost the
    batch exists to remove.  Without a batch the start time is read one
    pid at a time, as it always was.
    """
    if start_epochs is not None:
        return start_epochs.get(target, "")
    return process_start_epoch(target)


def _proc_start_ticks_batch(
    pids: Sequence[int],
) -> Optional[Dict[int, str]]:
    """Return the start times of *pids*, read from ``/proc`` in one pass.

    The value is the one :func:`process_start_epoch` returns on Linux —
    the start time in clock ticks since boot — so the comparison the
    ladder makes is between like and like: the marker was written in
    ticks, and the answer is in ticks.  A batch that answered in
    another representation would report every live owner as a *recycled
    pid*, which is a skip: the reaper would stop reclaiming anything on
    the very platform the marker was written for.

    ``None`` means the answer is unusable — ``/proc`` is not there, or
    not readable — and the caller falls back to asking one pid at a
    time, which is the route that wrote the marker in the first place.
    """
    epochs: Dict[int, str] = {}
    for pid in pids:
        value = _proc_start_ticks(pid)
        if value is not None:
            epochs[pid] = value
    return epochs or None


def _read_owner_epochs(
    owner_pids: Sequence[int],
    *,
    deadline: float,
) -> Optional[Dict[int, str]]:
    """Read the start times of every owner the scan heard about.

    The answer has to be in the **same representation** the marker was
    written in, because that is what the ladder compares: on Linux the
    marker carries the start time in clock ticks since boot (what
    ``/proc`` says), and on macOS it carries epoch seconds (what ``ps``
    says).  A batch that answered in the other representation would call
    every live owner a recycled pid, and a recycled pid is a skip — so
    the reaper would reclaim nothing at all, on every platform.

    ``None`` means the answer is unusable and the caller has to fall
    back to reading the start times one pid at a time.  An empty mapping
    is a usable answer that reports no live owner: the owners are then
    judged *unverified*, which is a skip and never a kill.
    """
    if not owner_pids:
        return {}
    if time.monotonic() > deadline:
        logger.warning(
            "orphan reaper: the budget was spent before the owners could "
            "be read; their start times stay unknown"
        )
        return {}
    wanted = sorted(set(owner_pids))
    if _IS_DARWIN:
        return _ps_start_epochs(wanted)
    return _proc_start_ticks_batch(wanted)


def _batched_owner_probe(
    start_epochs: Optional[Mapping[int, str]],
) -> Callable[[int], Optional[OwnerState]]:
    """Return an owner probe that answers from a batch of start times."""

    def _probe(pid: int) -> Optional[OwnerState]:
        return probe_owner(pid, start_epochs=start_epochs)

    return _probe


def scan(
    config: ReaperConfig,
    *,
    list_procs: Callable[[], List[ProcInfo]] = list_processes,
    env_reader: Optional[
        Callable[[int], Optional[Dict[str, str]]]
    ] = None,
    owner_probe: Optional[Callable[[int], Optional[OwnerState]]] = None,
    port_map_provider: Callable[
        [], Optional[Mapping[int, Iterable[int]]]
    ] = listening_ports_by_pid,
    self_uid: Optional[int] = None,
    env_bulk_reader: Optional[
        Callable[[Sequence[ProcInfo]], Optional[Mapping[int, Dict[str, str]]]]
    ] = None,
    budget_seconds: float = SCAN_BUDGET_SECONDS,
) -> ScanResult:
    """Walk the machine once and judge every process it sees.

    Every probe is injected, so a scan can be assembled out of fixtures
    and needs no subprocess at all.  The probes answer three questions
    the ladder needs: what is running (``list_procs``), what a process
    carries (``env_reader``), who owns it (``owner_probe``), and what
    it listens on (``port_map_provider``).

    ``env_reader`` left out is not "no environment": it is the machine's
    own environment read in ONE spawn — the whole point being that the
    per-process route costs two spawns per process, and a scan of a
    machine with a thousand processes costs minutes of wall clock that
    the boot path pays for.  Pass a reader to have the scan ask process
    by process instead.

    ``env_bulk_reader`` is the other machine-wide route: it reads every
    environment in one spawn and hands the whole environment back, not
    only the markers the ladder reads.  It is asked first when the
    caller brought no reader of its own, and it declines by itself where
    it does not apply — a platform that is not macOS, or a machine with
    few enough processes that the per-process route is the cheaper of
    the two.  Where it declines, the machine-wide source above answers.

    ``owner_probe`` left out is not "ask about every owner one at a
    time" either: the start times of every owner the scan heard about
    are read in ONE spawn as well, so the owners of a thousand marked
    processes cost one spawn rather than a thousand.  Pass a probe to
    have the scan ask owner by owner instead.

    The scan is bounded by ``budget_seconds``: what the budget does not
    reach is reported as *unreadable*, which the ladder treats as a
    skip, never as a kill.  A scan that cannot finish is a scan that
    keeps the machine busy instead of reclaiming it.

    The budget bounds the **reading**, not the judging.  The machine-wide
    read is one spawn whose cost does not grow with the machine, so an
    environment it has already read is judged even when the budget ran
    out in the meantime: a budget that expired late must not turn a
    readable machine into an unreadable one, which would stop the
    reaper from reclaiming anything at all.

    The scan **decides**; it does not signal.  A candidate whose
    environment could not be read is reported as such rather than
    quietly treated as unmarked, and a failed port probe skips every
    candidate rather than letting the kill through unchecked.

    A configured ``pid_scope`` restricts the scan to the pids it
    names — a testing gate that narrows the blast radius.
    """
    deadline = time.monotonic() + max(0.0, float(budget_seconds))

    procs = list_procs()

    scope = config.pid_scope
    if scope is not None:
        allowed = frozenset(int(pid) for pid in scope)
        procs = [proc for proc in procs if proc.pid in allowed]

    # The bulk read: one probe for every process on the machine,
    # instead of two per process.  It is used only when the caller did
    # not inject its own per-process reader — an injected reader is a
    # decision, and a decision this layer must not second-guess.
    #
    # The bulk reader is asked first because it is the one route that
    # answers for the whole machine at once AND carries the environment
    # as it was; it declines by itself where it does not apply (a
    # platform that is not macOS, or a machine with few enough
    # processes that the per-process route is the cheaper of the two),
    # and the machine-wide source below is the route that answers then.
    if env_bulk_reader is None and env_reader in (None, read_process_environ):
        env_bulk_reader = read_process_environ_bulk

    env_map: Optional[Mapping[int, Dict[str, str]]] = None
    if env_bulk_reader is not None:
        try:
            env_map = env_bulk_reader(procs)
        except Exception:  # pragma: no cover - the contract is "never raises"
            logger.warning(
                "orphan reaper: the bulk environment read failed",
                exc_info=True,
            )
            env_map = None

    try:
        port_map = port_map_provider()
    except Exception:  # pragma: no cover - the contract is "never raises"
        logger.warning("orphan reaper: the port probe failed", exc_info=True)
        port_map = None

    env_source = env_reader
    # ``pre_read`` says the environments were read in one go, before this
    # walk began: the walk is then a lookup, and the budget -- which
    # bounds the *reading* -- has nothing left to bound.  A source that
    # reads on demand is bounded by it.
    pre_read = False
    if env_map is not None:
        # The bulk read already holds every environment: the walk below
        # is a lookup, so there is nothing left for the budget to bound.
        pre_read = True
    elif env_source is None:
        try:
            env_source, pre_read = _default_env_source(procs, deadline=deadline)
        except Exception:  # pragma: no cover - the contract is "never raises"
            # A machine-wide read that fails in any way costs that read
            # and nothing else: the per-process reader answers the same
            # question, slower.  Without this fallback a failed read
            # would report every process on the machine as *unreadable*
            # — a silent, total loss of the reaper's evidence, dressed
            # up as a scan that ran.
            logger.warning(
                "orphan reaper: the machine-wide environment read failed",
                exc_info=True,
            )
            env_source = read_process_environ

    verdicts: List[Verdict] = []
    owners_alive = 0
    unreadable = 0

    # -- The read phase ----------------------------------------------------
    # Every environment is read first and the owners they name are
    # collected on the way.  The owners are then asked about in ONE
    # spawn: one ``ps`` per owner is one ``ps`` per process on the
    # machine, which is the cost this scan exists to avoid.
    read: List[Tuple[ProcInfo, Optional[Dict[str, str]]]] = []
    owner_pids: List[int] = []

    for proc in procs:
        if env_map is not None:
            # A pid the bulk read did not answer for is a pid whose
            # environment could not be read — the same statement the
            # per-process reader makes when it comes back empty.
            env = env_map.get(proc.pid)
        elif not pre_read and time.monotonic() > deadline:
            # The budget is spent.  The process is reported as unreadable
            # rather than as unmarked: this layer is not entitled to
            # claim that a process it could not read carries no marker.
            env = None
            unreadable += 1
        else:
            try:
                env = env_source(proc.pid)
            except Exception:  # pragma: no cover - the contract is "never raises"
                logger.warning(
                    "orphan reaper: the environment of pid %s could not be read",
                    proc.pid,
                    exc_info=True,
                )
                env = None

        read.append((proc, env))
        owner_pid, _owner_start, _kind = _markers(env or {})
        if owner_pid is not None:
            owner_pids.append(owner_pid)

    # -- The owners --------------------------------------------------------
    # An injected probe is the caller's own business: it answers however
    # it likes and is not asked anything.  The default probe is answered
    # from a batch, so that the owners of a thousand marked processes
    # cost one spawn rather than a thousand.
    if owner_probe is None:
        owner_epochs = _read_owner_epochs(owner_pids, deadline=deadline)
        owner_probe = _batched_owner_probe(owner_epochs)

    # -- The judge phase ---------------------------------------------------
    for proc, env in read:
        marked = replace(proc, env=env)

        owner_pid, _owner_start, _kind = _markers(env or {})
        owner_state: Optional[OwnerState] = None
        if owner_pid is not None:
            try:
                owner_state = owner_probe(owner_pid)
            except Exception:  # pragma: no cover - the contract is "never raises"
                logger.warning(
                    "orphan reaper: the owner %s of pid %s could not be checked",
                    owner_pid,
                    proc.pid,
                    exc_info=True,
                )
                owner_state = None

        verdict = classify(
            marked,
            config=config,
            owner_state=owner_state,
            port_map=port_map,
            self_uid=self_uid,
        )
        verdicts.append(verdict)

        if verdict.reason in (REASON_OWNER_ALIVE, REASON_OWNER_ALIVE_UNVERIFIED):
            owners_alive += 1

    if unreadable:
        logger.warning(
            "orphan reaper: the scan ran out of its budget; %d process(es) "
            "were left unread",
            unreadable,
        )

    return ScanResult(verdicts=verdicts, owners_alive=owners_alive)


# --------------------------------------------------------------------------
# The reaping stage: TERM, the grace period, then KILL
# --------------------------------------------------------------------------
#
# The ladder decides; this stage *acts*.  Acting is where a mistake
# kills a process somebody still needs, so the stage is built around
# three refusals and one re-check:
#
# * a **dry run** decides and stops — not one signal leaves the process;
# * a **group** is only ever signalled when the candidate *leads* it,
#   and never when it is one of the POSIX sentinels or our own;
# * the **reaper itself** and **pid 1** are never signalled;
# * the **owner is re-probed** immediately before the signal, because an
#   owner that was dead at scan time may be alive when the signal goes
#   out — and that is the difference between a reaper and an accident.
#
# Everything that touches the machine is injected (the sender, the
# sleep and the liveness probe), so the stage is testable without a
# real process — which is the only way to test the refusals at all.

#: The signal the reaper sends first, and the one it gives a process
#: time to react to.
SIGNAL_TERM = "TERM"

#: The signal the reaper sends when the grace period runs out.
SIGNAL_KILL = "KILL"

#: What :attr:`Verdict.signal` says when the reaper deliberately sent
#: nothing — a dry run.
SIGNAL_NONE = "none"

#: How often the reaper asks "is it gone yet?" while the grace period
#: runs.  A tenth of a second is short enough to keep the total wait
#: close to the configured grace, and long enough not to spend the
#: machine on ``kill(pid, 0)``.
POLL_STEP = 0.1

#: The reason recorded when the owner came back to life between the
#: scan and the signal.
REASON_OWNER_REVIVED = "owner_revived"


def _own_pgid() -> Optional[int]:
    """The process group this process belongs to, if the platform says."""
    try:
        return os.getpgrp()
    except OSError:  # pragma: no cover - exotic platform
        return None


def _signallable_pgid(pgid: object, own_pgid: Optional[int] = None) -> Optional[int]:
    """``pgid`` if it names a process group this process may signal.

    ``os.killpg(p, sig)`` is ``kill(-p, sig)``, so ``p`` is not a group
    id in the ordinary sense, and two of its values are lethal:

    ``0``
        the caller's own process group.  Signalling it takes down the
        caller — under pytest, the whole session.

    ``1``
        every process the caller may signal.  On Linux that is the CI
        job; on macOS ``libc`` refuses it with ``EPERM``, which is why
        a bug of this shape hides from the machine it was written on.

    ``None`` is refused for the same reason a bool is: ``True`` and
    ``False`` are ints, and they are ``1`` and ``0`` — the two values
    above.

    ``own_pgid`` is the caller's own group.  A group that *is* our own
    is refused for the same reason ``0`` is: it is not a group of
    candidates, it is us.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int):
        return None
    if pgid <= 1:
        return None
    if own_pgid is not None and pgid == own_pgid:
        return None
    return pgid


def _default_send(pid: int, sig_name: str, pgid: Optional[int]) -> None:
    """Send one signal to a process, or to the group it leads.

    ``pgid`` of ``None`` means "the process itself"; anything else means
    "that group".  The two are not interchangeable, so the caller has to
    decide which one it means — see :func:`_signallable_pgid`.
    """
    sig = getattr(signal, "SIG" + sig_name, None)
    if not isinstance(sig, int):
        raise ValueError("unknown signal name: %r" % (sig_name,))
    if pgid is not None:
        os.killpg(pgid, sig)
    else:
        os.kill(pid, sig)


def _pid_alive(pid: int) -> Optional[bool]:
    """Whether *pid* is still running — ``True``, ``False``, or ``None``.

    ``None`` means *could not be checked*: the kernel answered with
    something other than "no such process".  It is never silently
    turned into "alive" or "gone"; the callers decide which way that
    uncertainty is safe, and they decide it differently for the owner
    (skip) and for the target (keep waiting).

    A **zombie** counts as alive, because ``kill(pid, 0)`` on a zombie
    succeeds until the process is waited for.  That never happens to a
    candidate: an orphan's parent is dead, so the orphan was re-parented
    to init, and init reaps it — its pid disappears at once.  A process
    that is still *our* child would linger as a zombie, and the reaper
    would wait out the grace for a process that is already dead before
    sending it a ``SIGKILL`` it cannot receive.  The cost is a wasted
    grace period, not a wrong kill.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it simply is not ours to signal.
        return True
    except OSError:
        return None
    return True


def _probe_alive(
    probe: Callable[[int], Optional[bool]], pid: int
) -> Optional[bool]:
    """Ask *probe* whether *pid* is alive, normalising its failures.

    ``ProcessLookupError`` is the kernel saying "there is no such
    process", which is *gone* — never an error.
    """
    try:
        return probe(pid)
    except ProcessLookupError:
        return False
    except OSError:
        return None


def _wait_for_exit(
    pid: int,
    probe: Callable[[int], Optional[bool]],
    grace: float,
    napper: Callable[[float], None],
) -> bool:
    """Wait up to *grace* seconds for *pid* to be gone.

    ``True`` when it is gone (or cannot be found any more), ``False``
    when it is still there once the grace period is over.  An unknown
    liveness counts as *still there*: the cost of waiting is a few
    tenths of a second, the cost of a wrong "gone" is a killed process.
    """
    elapsed = 0.0
    while True:
        alive = _probe_alive(probe, pid)
        if alive is False:
            return True
        if elapsed >= grace:
            return False
        napper(POLL_STEP)
        elapsed += POLL_STEP


#: The outcome of one attempt to signal a target.
_SENT = "sent"
_GONE = "gone"
_REFUSED = "refused"


def _kill_audit_module():
    """The kill audit module, imported on first use.

    Imported lazily on purpose: importing it installs the audit hook,
    and a process that never signals anything has no business writing
    audit records.  ``None`` when the module cannot be imported — the
    audit is evidence, never a precondition.

    Loaded through :func:`importlib.import_module` rather than a plain
    ``import kill_audit`` **inside** the function, because this module
    is required to import nothing but the standard library (it runs
    standalone as ``python -m backend.orphan_reaper``), and a module-
    level import of a sibling backend module — even a lazy one — is
    exactly what that rule forbids.  The audit module is itself
    standard-library-only, so loading it at signal time costs nothing
    and drags in no server.
    """
    try:
        return importlib.import_module("kill_audit")
    except Exception:  # noqa: BLE001 - the audit is best-effort
        return None


def _write_outcome(audit, kind, target, sig, outcome, errno=None) -> None:
    """Write one signal's outcome into the kill audit.  Never raises."""
    if audit is None:
        return
    try:
        audit.write_kill_outcome(kind, target, sig, outcome, errno=errno)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        pass


def _signal_once(
    sender: Callable[[int, str, Optional[int]], None],
    pid: int,
    sig_name: str,
    pgid: Optional[int],
) -> str:
    """Send one signal and say what happened.

    ``_SENT``
        the signal went out.
    ``_GONE``
        the kernel says there is no such process — the target is
        already gone, which is a success for the reaper, not an error.
    ``_REFUSED``
        the signal was refused or failed.  The reason is written to
        the kill audit, so a refusal is evidence and not just a
        traceback.
    """
    audit = _kill_audit_module()
    sig = getattr(signal, "SIG" + sig_name, None)
    kind = "killpg" if pgid is not None else "kill"
    try:
        sender(pid, sig_name, pgid)
    except ProcessLookupError:
        _write_outcome(audit, kind, pid, sig, "gone")
        return _GONE
    except PermissionError as exc:
        _write_outcome(audit, kind, pid, sig, "refused", errno=exc.errno)
        logger.warning(
            "orphan reaper: the signal to pid %s was refused (errno %s)",
            pid, exc.errno,
        )
        return _REFUSED
    except OSError as exc:
        _write_outcome(audit, kind, pid, sig, "failed", errno=exc.errno)
        logger.warning(
            "orphan reaper: could not signal pid %s", pid, exc_info=True
        )
        return _REFUSED
    _write_outcome(audit, kind, pid, sig, "sent")
    return _SENT


def _reap_one(
    verdict: Verdict,
    config: ReaperConfig,
    *,
    sender: Callable[[int, str, Optional[int]], None],
    napper: Callable[[float], None],
    probe: Callable[[int], Optional[bool]],
) -> Verdict:
    """Reap one verdict, in place, and return it."""
    if verdict.decision != DECISION_KILL:
        return verdict

    proc = verdict.proc
    pid = proc.pid

    # -- The reaper never signals itself, and never signals pid 1.
    if pid <= 1 or pid == os.getpid():
        verdict.decision = DECISION_SKIP
        verdict.reason = REASON_SELF
        verdict.signal = ""
        return verdict

    # -- A dry run decides and stops.  Nothing is probed, nothing is
    #    signalled: the whole point of the mode is that it cannot hurt.
    if config.dry_run:
        verdict.signal = SIGNAL_NONE
        return verdict

    # -- The owner must still be dead.  This is the check the ladder
    #    could not make: it judged the owner minutes ago, and an owner
    #    that has come back owns the candidate again.
    owner_pid = verdict.owner_pid
    if owner_pid is None:
        verdict.decision = DECISION_SKIP
        verdict.reason = REASON_OWNER_UNKNOWN
        verdict.signal = ""
        return verdict

    owner_alive = _probe_alive(probe, owner_pid)
    if owner_alive is None:
        # An unchecked owner is not a dead owner.
        verdict.decision = DECISION_SKIP
        verdict.reason = REASON_OWNER_UNKNOWN
        verdict.signal = ""
        return verdict
    if owner_alive:
        verdict.decision = DECISION_SKIP
        verdict.reason = REASON_OWNER_REVIVED
        verdict.signal = ""
        return verdict

    # -- The target: the group only when the candidate leads it.  A
    #    group led by somebody else holds processes the ladder never
    #    judged, and signalling it would take them out as well.
    pgid = _signallable_pgid(proc.pgid, own_pgid=_own_pgid())
    if pgid is None or pgid != pid:
        pgid = None

    # -- SIGTERM, then the grace period, then SIGKILL.
    #
    # The refusals are three different things, and the difference is
    # the whole point of this stage:
    #
    # ESRCH
    #     the target is already gone — a success, not an error.
    # EPERM
    #     the signal was refused.  On macOS a process group whose
    #     leader is a zombie answers this way for the WHOLE group,
    #     while the process itself usually still answers — so the
    #     group refusal degrades to a single-pid signal instead of
    #     giving up.  This is the case that used to leave a verdict
    #     saying ``decision='kill'`` with an empty signal: the reaper
    #     had decided to kill the orphan and had not killed it.
    # anything else
    #     unknown — recorded, and the escalation continues against the
    #     process itself.
    #
    # Whatever happens, a verdict that says ``decision='kill'`` never
    # carries an empty signal.  The empty string meant "we decided to
    # kill it and then did not", and that is exactly the shape a run
    # leaves behind when a group refuses.
    target_pgid = pgid
    sent = _signal_once(sender, pid, SIGNAL_TERM, target_pgid)
    if sent == _REFUSED and target_pgid is not None:
        # The group refused; the process itself may still answer.
        target_pgid = None
        sent = _signal_once(sender, pid, SIGNAL_TERM, target_pgid)
    if sent == _GONE:
        # Already gone: there is nothing left to escalate to.
        verdict.signal = SIGNAL_TERM
        return verdict
    if sent == _REFUSED:
        # Neither the group nor the process took the signal.  The
        # verdict says exactly that — it does not say "killed nothing"
        # by carrying an empty signal.
        verdict.signal = SIGNAL_NONE
        return verdict
    verdict.signal = SIGNAL_TERM

    if _wait_for_exit(pid, probe, config.grace, napper):
        return verdict

    sent = _signal_once(sender, pid, SIGNAL_KILL, target_pgid)
    if sent == _REFUSED and target_pgid is not None:
        target_pgid = None
        sent = _signal_once(sender, pid, SIGNAL_KILL, target_pgid)
    if sent == _SENT:
        verdict.signal = SIGNAL_KILL
    elif sent == _GONE:
        # Gone between the check and the kill.
        return verdict
    else:
        verdict.signal = SIGNAL_NONE
    return verdict


def reap_verdicts(
    verdicts: Iterable[Verdict],
    config: ReaperConfig,
    *,
    send: Optional[Callable[[int, str, Optional[int]], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    alive_probe: Optional[Callable[[int], Optional[bool]]] = None,
) -> List[Verdict]:
    """Reap the orphans among *verdicts* and report what was done.

    Each verdict is reaped on its own: one that fails is logged and
    skipped, and the others are unaffected.  A reaper that stops at the
    first problem would leave exactly the processes it exists to
    reclaim still holding their memory.

    *send* is called as ``send(pid, "TERM"|"KILL", pgid)``, where a
    ``pgid`` of ``None`` means "the process itself" and anything else
    means "that group"; *sleep* is called with the poll step; and
    *alive_probe* is called with a pid and answers ``True`` (alive),
    ``False`` (gone) or ``None`` (could not be checked).

    The verdicts are updated in place — ``verdict.signal`` says what
    was sent (``"TERM"``, ``"KILL"``, or ``"none"`` for a dry run) —
    and the same objects are returned.
    """
    sender = _default_send if send is None else send
    napper = time.sleep if sleep is None else sleep
    probe = _pid_alive if alive_probe is None else alive_probe

    reaped: List[Verdict] = []
    for verdict in verdicts:
        try:
            reaped.append(
                _reap_one(
                    verdict,
                    config,
                    sender=sender,
                    napper=napper,
                    probe=probe,
                )
            )
        except Exception:  # pragma: no cover - the contract is "never raises"
            logger.warning(
                "orphan reaper: reaping pid %s failed",
                getattr(getattr(verdict, "proc", None), "pid", "?"),
                exc_info=True,
            )
            reaped.append(verdict)
    return reaped


# --------------------------------------------------------------------------
# The singleton lock and the daemon launch
# --------------------------------------------------------------------------

#: Directory under the plan-artifact root that holds the reaper's own state.
#:
#: It sits beside the plan artifacts rather than in a system-wide
#: location because the lock is scoped to *what the reaper reaps*: two
#: reapers pointed at different roots reap different processes and do
#: not exclude each other.
REAPER_DIRNAME = ".orphan_reaper"

#: Name of the singleton lock inside :data:`REAPER_DIRNAME`.
LOCK_FILENAME = "reaper.lock"

#: Name of the daemon's log inside :data:`REAPER_DIRNAME`.
DAEMON_LOG_FILENAME = "daemon.log"

#: The repository root — the parent of the ``backend`` package.
#:
#: Derived from this file's own location, so that the daemon is
#: started with the same working directory whether the backend was
#: started from the repository root, from ``backend/``, or from an
#: installed module.
REPO_ROOT = Path(__file__).resolve().parents[1]


def lock_path(plans_root: Path) -> Path:
    """Return the path of the reaper's singleton lock.

    The lock lives under the plan-artifact root the reaper was told to
    work on, so a reaper pointed at another root neither excludes this
    one nor is excluded by it.
    """
    return Path(plans_root) / REAPER_DIRNAME / LOCK_FILENAME


def acquire_lock(plans_root: Path) -> Optional[IO]:
    """Take the reaper's singleton lock, or return ``None``.

    The lock is an ``flock`` on :func:`lock_path`, taken **non-blocking**
    (``LOCK_EX | LOCK_NB``): a caller that had to wait for it would be a
    caller that can hang the backend on a lock held by a process that
    may never release it.  Refusal is reported as ``None`` rather than
    as an exception, because the most common caller only wants to
    *ask* (see :func:`daemon_alive`) and should not have to catch
    anything in order to ask.

    The returned handle owns the lock: closing it releases the lock,
    and the kernel releases it anyway when the holder dies — which is
    the property that makes this work where a pid file cannot.  A pid
    in a file may have been reused by an unrelated process; a lock
    cannot outlive its holder.

    ``None`` is also returned when the lock cannot be taken *at all* —
    no ``fcntl`` on this platform, an unwritable directory, a failed
    open.  A caller must read that as "not provably alone", never as
    "provably alone": the difference between skipping a launch and
    starting a second daemon.
    """
    if fcntl is None:  # pragma: no cover - Windows
        logger.warning(
            "[orphan_reaper] no fcntl on this platform; the reaper daemon "
            "cannot be made a singleton and will not be started"
        )
        return None

    path = lock_path(plans_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError as exc:
        logger.warning("[orphan_reaper] could not open the lock %s: %s", path, exc)
        return None

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # EWOULDBLOCK / EAGAIN: somebody else holds it.  Closing the
        # handle releases nothing we own.
        handle.close()
        return None

    # Record the holder's pid in the file.  It is the only diagnostic an
    # operator gets for "the daemon did not start", and it costs
    # nothing: the lock is on the inode, not on the content.
    try:
        handle.seek(0)
        handle.truncate()
        handle.write("%d\n" % os.getpid())
        handle.flush()
    except OSError:  # pragma: no cover - the lock is already held
        pass

    return handle


def daemon_alive(plans_root: Path) -> bool:
    """Return whether a reaper daemon is currently running.

    The lock is the only source of truth.  There is no separate state
    that could disagree with it — no pid file to go stale, no "started
    at" timestamp to survive a crash.  If the lock can be taken, nobody
    holds it and there is no daemon; if it cannot, somebody holds it
    and there is one.

    Note the direction of the inference: a lock that cannot be taken
    for an unrelated reason (an unwritable directory, no ``fcntl``)
    also reads as "alive".  That is the safe direction — the cost of a
    wrong "alive" is a daemon that is not started, while the cost of a
    wrong "dead" is two daemons killing the same processes.
    """
    handle = acquire_lock(plans_root)
    if handle is None:
        return True
    try:
        handle.close()
    except OSError:  # pragma: no cover - closing a handle we opened
        pass
    return False


def _detach_is_granted(config: ReaperConfig, requested: bool) -> bool:
    """Decide whether a daemon may outlive the process that started it.

    The caller *requests*; the configuration *decides*.

    * ``None`` — the configuration does not speak about it, so the
      caller's request stands.  This is the answer for a config built
      in code: the caller knows whether its daemon needs to outlive
      it.
    * ``False`` — a refusal.  The daemon leaves with the process that
      started it, whatever that process asked for.  This is the
      answer for a config read from the environment: the exemption
      has to be said out loud (:data:`ENV_DETACH_OWNER`), because the
      cost of granting it by accident is a daemon nobody owns.
    * ``True`` — the permission.  The caller still has to ask, so a
      config that grants the exemption does not detach every daemon
      on the machine.

    The refusal is the one that matters.  On 2026-11-02 a single
    ``detach_owner=True`` in the server left twenty-four daemons
    walking one machine, each scanning every process on the box every
    thirty seconds, for ever: the exemption had never been *decided*,
    it had only been *typed*.
    """
    decision = getattr(config, "detach_owner", None)
    if decision is None:
        return bool(requested)
    return bool(requested) and bool(decision)


def _daemon_lease(config: ReaperConfig, *, detached: bool) -> int:
    """Return how many seconds the daemon is allowed to live.

    A daemon that outlives its owner needs a bound, or "outlives its
    owner" is just a slower way of saying "for ever".  The bound is
    the lease, and it travels to the child in its environment.

    A configured lifetime is the operator's own answer and is kept as
    it is — including an answer of *no ceiling*, for a daemon that
    leaves with its owner anyway: the owner rule is its bound, and
    this function is not here to override what somebody asked for.

    A **detached** daemon is the exception, because it has no owner to
    leave with.  An unbounded detached daemon is the leak — twenty-four
    of them, for ever — so it gets the default lease even where the
    configuration says "no ceiling".
    """
    try:
        lifetime = int(getattr(config, "max_lifetime", DEFAULT_MAX_LIFETIME) or 0)
    except (TypeError, ValueError):  # pragma: no cover - a malformed config
        lifetime = 0
    if lifetime > 0:
        return lifetime
    if detached:
        return DEFAULT_DETACHED_LEASE
    return 0


def ensure_daemon(
    config: ReaperConfig,
    *,
    repo_root: Optional[Path] = None,
    popen: Optional[Callable[..., Any]] = None,
    detach_owner: bool = False,
) -> Optional[int]:
    """Start the reaper daemon unless one is already running.

    Returns the pid of the daemon that was started, or ``None`` when
    nothing was started: the reaper is disabled, a daemon is already
    running, or the launch failed.

    The daemon is started **detached** — in its own session
    (``start_new_session=True``) — because it has to outlive the
    backend that starts it.  The backend dying is precisely the
    situation the daemon exists for: a daemon that died with the
    backend would leave the orphans behind again, and the next backend
    to start would have to notice them from scratch.

    The child carries the ``reaper`` marker, so that the reaper never
    reaps itself: a process marked ``PDT_SPAWN_KIND=reaper`` is not a
    candidate, whatever else is true about it.

    **Who the daemon belongs to** is the caller's decision, and the
    default is the safe one.  By default the daemon is *attached*: it
    is told that this process is its owner (pid **and** start time, so
    that a recycled pid is not mistaken for the owner), and it leaves
    when that owner is gone — a daemon that outlives the process that
    asked for it has nothing left to do.  ``detach_owner=True`` is the
    exemption: the daemon is told to survive its owner, which is what
    the **server** asks for, because "the server died" is the case its
    daemon exists for.

    The request is a request, not a decision.  The **configuration**
    decides: :attr:`ReaperConfig.detach_owner` refuses the exemption
    unless somebody said it out loud (see :data:`ENV_DETACH_OWNER`).
    A caller that asks to be exempted without the switch being set
    gets an attached daemon, because the exemption is the kind of
    decision that has to be made deliberately — the twenty-four
    daemons that walked one machine on 2026-11-02 all began as an
    argument in a source file nobody was reading.

    A daemon that is allowed to outlive its owner is given a **lease**:
    the number of seconds it may live, carried in its environment.  A
    lease is what makes "outlive the owner" a bounded statement rather
    than "for ever".

    Nothing here raises.  The daemon is started as a side effect of
    starting the backend, and a failure to start it must not take the
    backend down with it — the backend dying is the failure this whole
    module exists to survive.  A failure is logged and reported as
    ``None``.

    ``popen`` and ``repo_root`` are seams for the tests: the first
    replaces :class:`subprocess.Popen`, the second replaces the
    working directory the daemon is started in.
    """
    try:
        if not config.enabled:
            return None
        if daemon_alive(config.plans_root):
            return None

        cmd = [
            sys.executable,
            "-m",
            "backend.orphan_reaper",
            "--daemon",
            "--interval",
            str(config.interval),
            "--plans-root",
            str(config.plans_root),
        ]

        # The two answers to "who does this daemon belong to".  An
        # attached daemon records its owner and leaves when that owner
        # is gone; a detached one is exempt.  They are alternatives,
        # not a pair: a daemon that survives its owner must not also
        # record one.
        detached = _detach_is_granted(config, detach_owner)
        if detached:
            cmd.append("--survive-owner")
        else:
            cmd.extend(
                [
                    "--owner-pid",
                    str(os.getpid()),
                    "--owner-start",
                    _self_start_epoch(),
                ]
            )

        env = dict(os.environ)
        apply_spawn_tags(env, KIND_REAPER)
        # The lease travels in the environment rather than on the
        # command line: the command line of a daemon is a public
        # record of what it is, and a lease is not part of that.
        env[ENV_MAX_LIFETIME] = str(_daemon_lease(config, detached=detached))

        directory = Path(config.plans_root) / REAPER_DIRNAME
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / DAEMON_LOG_FILENAME
        log = open(log_path, "a", encoding="utf-8")

        try:
            launcher = popen if popen is not None else subprocess.Popen
            process = launcher(
                cmd,
                env=env,
                cwd=str(repo_root if repo_root is not None else REPO_ROOT),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            # The child holds its own descriptor; ours is not needed.
            log.close()

        # The daemon leads a session of its own, so it outlives the
        # process that started it -- and, without a record of it, the
        # run that started it.  The registry is that record: a run that
        # names one can give the daemon back when it ends.
        register_spawned_process(
            int(process.pid),
            pgid=int(process.pid),
            kind=KIND_REAPER,
            name="orphan-reaper-daemon",
        )

        return int(process.pid)
    except Exception as exc:  # noqa: BLE001 - a reaper must not crash its host
        logger.warning("[orphan_reaper] could not start the reaper daemon: %s", exc)
        return None


# --------------------------------------------------------------------------
# The daemon census
# --------------------------------------------------------------------------
#
# The lock answers one question: "is a daemon holding *this* root?".  It
# answers it honestly — the kernel drops an ``flock`` when the holder
# dies — but it is a question about one root, and the leak is a
# question about the machine.
#
# The lock file is the weak point.  Delete it and the next daemon takes
# a *different* inode: the flock is honoured, the singleton is broken,
# and every daemon believes it is alone.  Nothing in the reaper's own
# bookkeeping notices, because the bookkeeping is exactly what a leaked
# daemon leaves behind.  So the machine-level count has to come from
# the machine — from the process table and the kernel's own start
# times.
#
# Everything below is read from the operating system.  Nothing is read
# from the reaper's own state, because the reaper's own state is what
# cannot be trusted.


@dataclass(frozen=True)
class ReaperDaemon:
    """One live reaper daemon, as the machine reports it.

    ``started_at`` is the platform's own start token for the process:
    epoch seconds where the platform reports epoch seconds, clock ticks
    since boot where it reports those.  It is ``None`` when the kernel
    would not say at all.

    ``age_seconds`` is always real seconds — how long the daemon has
    been running — and is ``None`` only when the start time could not
    be read.

    ``plans_root`` is the root named on the command line, which is
    ``None`` when the daemon was started on the default one.  It is
    ``plans_root_exists`` that says whether that root is still there.

    ``survive_owner`` says whether the daemon was exempted from the
    owner rule — whether it was started with ``--survive-owner``, and
    may therefore outlive the process that started it.  It is the
    batch that leaked on 2026-11-02, and a census that cannot tell it
    apart from the others cannot count it.
    """

    pid: int
    ppid: int
    plans_root: Optional[str]
    started_at: Optional[str]
    age_seconds: Optional[float]
    plans_root_exists: bool
    survive_owner: bool = False


def _argv_tokens(argv: str) -> List[str]:
    """Split a command line into words, tolerating a broken one.

    A command line that never closes a quote is a command line some
    other program wrote; it still has to be read rather than raise,
    because the census runs over every process on the machine.
    """
    if not argv:
        return []
    try:
        return _posix_split(argv)
    except ValueError:
        return argv.split()


def _program_basename(token: str) -> str:
    """Return the bare program name of one command-line word.

    ``backend.orphan_reaper``, ``orphan_reaper`` and
    ``/opt/app/backend/orphan_reaper.py`` all name the same program;
    the census has to recognise every one of them, and none of the
    neighbours (``orphan_reaper_tests``, ``some_other_daemon``).
    """
    name = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.endswith(".py"):
        name = name[:-3]
    return name.rsplit(".", 1)[-1]


def _is_reaper_daemon(argv: str) -> bool:
    """Return whether *argv* is a reaper daemon's command line.

    Two things have to hold: the command line has to name the reaper,
    and it has to ask for the daemon mode.  A process that carries
    ``--daemon`` for some other program is not a reaper daemon, and a
    reaper asked for a single pass is not one either.
    """
    tokens = _argv_tokens(argv)
    if "--daemon" not in tokens:
        return False
    return any(_program_basename(token) == "orphan_reaper" for token in tokens)


def _argv_plans_root(tokens: Sequence[str]) -> Optional[str]:
    """Return the ``--plans-root`` value in *tokens*, if it has one.

    Both spellings are understood — ``--plans-root VALUE`` and
    ``--plans-root=VALUE`` — because both are what shells and humans
    produce, and a census that understood only one of them would
    report half the daemons as rootless.

    A flag with no value behind it has no root: the daemon was started
    on the default one, which the command line does not name.
    """
    for index, token in enumerate(tokens):
        if token == "--plans-root":
            if index + 1 < len(tokens):
                value = tokens[index + 1]
                if value and not value.startswith("-"):
                    return value
            return None
        if token.startswith("--plans-root="):
            return token.split("=", 1)[1] or None
    return None


def _proc_uptime() -> Optional[float]:
    """Return the machine's uptime in seconds, or ``None``."""
    try:
        with open("/proc/uptime", "rb") as handle:
            return float(handle.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None


def _clock_ticks_per_second() -> Optional[float]:
    """Return the kernel's clock frequency, or ``None``."""
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return None


def _start_epoch_seconds(started_at: str, now: float) -> Optional[float]:
    """Return the epoch second a start token refers to, or ``None``.

    The token is epoch seconds where the platform reports epoch
    seconds.  Where it reports clock ticks since boot — Linux reads
    them out of ``/proc`` — the boot time is recovered from the
    machine's uptime, because a tick count is not a time and reading
    one as an epoch would put the daemon's start in the year 1970.
    """
    try:
        raw = float(started_at)
    except (TypeError, ValueError):
        return None
    if not _PROC_START_UNIT_IS_TICKS:
        return raw
    uptime = _proc_uptime()
    ticks_per_second = _clock_ticks_per_second()
    if uptime is None or not ticks_per_second:
        return None
    return (now - uptime) + raw / ticks_per_second


def _plans_root_exists(plans_root: Optional[str]) -> bool:
    """Return whether the root a daemon serves is still on disk."""
    if not plans_root:
        return False
    try:
        return Path(plans_root).expanduser().exists()
    except OSError:
        return False


def list_reaper_daemons(
    procs: Optional[Sequence[ProcInfo]] = None,
    now: Optional[float] = None,
    self_pid: Optional[int] = None,
) -> List[ReaperDaemon]:
    """Return the reaper daemons running on this machine.

    *procs* is the process table to read — the live one is read when it
    is not given.  *now* is the current time in epoch seconds, and
    *self_pid* is the pid to leave out: the caller's own, so that a
    reaper asking about the machine does not count itself.

    A process is a daemon when its command line names the reaper and
    asks for the daemon mode.  A process that cannot be understood is
    skipped rather than guessed at, and a start time that cannot be
    read leaves the daemon listed with an unknown age — the daemon is
    still running, which is the fact that matters.

    Nothing here raises.  A census that cannot be taken is an empty
    census, and the caller reads the count off it — so an empty census
    is a count of zero and never an error.
    """
    if procs is None:
        procs = list_processes()
    if now is None:
        now = time.time()
    if self_pid is None:
        self_pid = os.getpid()

    daemons: List[ReaperDaemon] = []
    for proc in procs:
        if proc.pid == self_pid:
            continue
        if not _is_reaper_daemon(proc.argv):
            continue

        tokens = _argv_tokens(proc.argv)
        plans_root = _argv_plans_root(tokens)
        try:
            started_at = process_start_epoch(proc.pid) or None
        except Exception:  # noqa: BLE001 - one unreadable pid is not a census
            started_at = None
        epoch = _start_epoch_seconds(started_at, now) if started_at else None
        age = None if epoch is None else max(now - epoch, 0.0)

        daemons.append(
            ReaperDaemon(
                pid=proc.pid,
                ppid=proc.ppid,
                plans_root=plans_root,
                started_at=started_at,
                age_seconds=age,
                plans_root_exists=_plans_root_exists(plans_root),
                survive_owner="--survive-owner" in tokens,
            )
        )
    return daemons


def daemon_status(
    procs: Optional[Sequence[ProcInfo]] = None,
    now: Optional[float] = None,
    self_pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the machine's reaper daemon census as plain data.

    The shape is ``{"count": int, "daemons": [...]}`` — the count and
    the list it counts, so that a caller can read the number without
    walking the list and the list without counting it again.
    """
    daemons = list_reaper_daemons(procs=procs, now=now, self_pid=self_pid)
    return {
        "count": len(daemons),
        "daemons": [
            {
                "pid": daemon.pid,
                "ppid": daemon.ppid,
                "plans_root": daemon.plans_root,
                "started_at": daemon.started_at,
                "age_seconds": daemon.age_seconds,
                "plans_root_exists": daemon.plans_root_exists,
                "survive_owner": daemon.survive_owner,
            }
            for daemon in daemons
        ],
    }


# --------------------------------------------------------------------------
# One daemon per root, and a ceiling on the machine
# --------------------------------------------------------------------------
#
# The lock answers "is a daemon holding *this* root?".  It does not
# answer the two questions that matter on a machine that has been
# running PDT for months:
#
# * "is somebody *else* already watching this root?" — the lock cannot
#   say, because deleting the lock file and starting a new daemon gives
#   the new daemon a fresh inode to lock while the old daemon keeps
#   the old one.  Two daemons, one root, both reaping.
# * "how many daemons is this machine running?" — the lock is scoped to
#   one root, so a daemon on another root neither excludes this one
#   nor is excluded by it.  Nothing stopped them from accumulating:
#   145 of them on one machine on 2026-11-02.
#
# The census answers both, because it reads the machine rather than
# one root's directory.


def _another_daemon_for_root(
    plans_root: Path,
    *,
    procs: Optional[Sequence[ProcInfo]] = None,
    self_pid: Optional[int] = None,
) -> Optional[int]:
    """Return the pid of a daemon already serving *plans_root*, if any.

    The lock could not catch that one, so the census does: two
    command lines naming the same root are two daemons on one root,
    whatever the lock file says.  The pid comes back because "somebody
    else is already watching this root" is a fact an operator wants to
    see in the log.

    ``None`` means nobody else is watching it — which includes the
    case of a daemon whose command line names no root at all: an
    unknown root is never the root that was asked for.

    *procs* is the process table to read — the live one when it is not
    given — and *self_pid* is the pid to leave out, so that a daemon
    asking about its own root does not find itself.
    """
    target = _resolved_root(plans_root)
    if target is None:
        return None
    for daemon in list_reaper_daemons(procs=procs, self_pid=self_pid):
        if _daemon_serves_root(daemon, target):
            return daemon.pid
    return None


def _daemon_budget_exhausted(
    config: ReaperConfig,
    *,
    procs: Optional[Sequence[ProcInfo]] = None,
) -> bool:
    """Return whether the machine is already running its quota of daemons.

    The ceiling is on the machine, not on one root: a dozen daemons on
    a dozen roots is still a dozen daemons walking the whole process
    table every thirty seconds, and the machine they share is the one
    that runs out of memory.

    Only daemons that are **watching a root that is still there** are
    counted.  A daemon whose root has been deleted is watching
    nothing, and it leaves by itself as soon as it notices; making it
    occupy a slot would let a machine full of dying daemons refuse to
    start a live one.

    A ceiling of zero is no ceiling: the check is off, and the machine
    may run as many daemons as it has roots to watch.
    """
    limit = getattr(config, "max_daemons", DEFAULT_MAX_DAEMONS)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_DAEMONS
    if limit <= 0:
        return False
    live = [
        daemon
        for daemon in list_reaper_daemons(procs=procs)
        if daemon.plans_root_exists
    ]
    return len(live) >= limit


# --------------------------------------------------------------------------
# Stopping the daemons of one root
# --------------------------------------------------------------------------
#
# The census says what the machine is running; this says how a caller
# that started a daemon gives it back.  A daemon serves exactly one plan
# root, and the caller that started it knows which root that was — so
# the helper is addressed by *root*, not by pid.  It stops the daemons
# of that root and leaves every other daemon on the machine alone: two
# daemons on two roots are two different services, and the whole reason
# the helper takes a root instead of a pid is that a caller must never
# stop something it did not start.
#
# The escalation is the one the reaper itself uses — SIGTERM, the grace
# period, then SIGKILL.  Nothing here decides *whether* a process
# should die: the census has already established that these are reaper
# daemons.  So the helper only has to be careful about which ones it
# touches, and about not falling over when one of them misbehaves.


def _resolved_root(plans_root: object) -> Optional[Path]:
    """Return *plans_root* as one absolute path, or ``None``.

    The comparison is made after resolution, because a root can be
    written down in more than one way — through a symlink, relatively,
    with a trailing separator — and all of them name one directory.
    """
    try:
        return Path(str(plans_root)).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _daemon_serves_root(daemon: ReaperDaemon, target: Path) -> bool:
    """Whether *daemon* serves the root *target*.

    A daemon whose command line names no root is not counted: its root
    is unknown, and an unknown root is never the root that was asked
    for.
    """
    root = getattr(daemon, "plans_root", None)
    if not root:
        return False
    resolved = _resolved_root(root)
    if resolved is None:
        return False
    return resolved == target


def _default_daemon_signal(pid: int, sig_name: str) -> None:
    """Send one signal to one daemon process."""
    _default_send(pid, sig_name, None)


def _child_has_exited(pid: int) -> bool:
    """Whether *pid* is our own child that has already exited.

    A process that has exited but has not been waited for is a
    **zombie**: the kernel keeps its process table entry, so
    ``kill(pid, 0)`` still succeeds and the pid reads as alive.  It is
    not alive — it runs no code and holds no memory; what is left of it
    is the entry.

    Only the parent can tell the difference, and the parent is us
    whenever the daemon being stopped is one this process started —
    which is the case this helper exists for.  ``waitpid`` with
    ``WNOHANG`` answers that without blocking, and it reaps the child
    on the way: a daemon nobody waits for would stay in the process
    table for as long as this process lives, which is the very leak
    the reaper exists to close.

    The price is the exit status, which the caller no longer sees.  A
    caller that needs it should not ask this helper to stop a child it
    wants to wait for itself.
    """
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        # Not our child (or the kernel will not say): nothing to tell.
        return False
    return waited == pid


def _running_probe(
    probe: Callable[[int], Optional[bool]]
) -> Callable[[int], Optional[bool]]:
    """Return a probe that calls an exited child gone.

    The plain liveness probe cannot tell a zombie from a running
    process, and this helper skips dead processes — so the parent is
    asked as well.
    """

    def _probe(pid: int) -> Optional[bool]:
        alive = _probe_alive(probe, pid)
        if alive is not True:
            return alive
        if _child_has_exited(pid):
            return False
        return alive

    return _probe


def _stop_one_daemon(
    pid: int,
    *,
    sender: Callable[[int, str], None],
    probe: Callable[[int], Optional[bool]],
    napper: Callable[[float], None],
    timeout: float,
) -> bool:
    """Stop one daemon, and say whether a signal was sent.

    SIGTERM first, the grace period, then SIGKILL.  A daemon that
    survives the SIGKILL is logged and left alone: the caller asked for
    the daemon to be stopped, not for the helper to fight it.
    """
    try:
        sender(pid, SIGNAL_TERM)
    except ProcessLookupError:
        # Already gone: there is nothing left to signal.
        return False
    except Exception:  # noqa: BLE001 - one daemon must not stop the rest
        logger.warning(
            "orphan reaper: could not signal daemon pid %s", pid, exc_info=True
        )
        return False

    if _wait_for_exit(pid, probe, timeout, napper):
        return True

    try:
        sender(pid, SIGNAL_KILL)
    except ProcessLookupError:
        # Gone between the check and the kill.
        return True
    except Exception:  # noqa: BLE001 - one daemon must not stop the rest
        logger.warning(
            "orphan reaper: could not kill daemon pid %s", pid, exc_info=True
        )
        return True

    if not _wait_for_exit(pid, probe, timeout, napper):
        logger.warning(
            "orphan reaper: daemon pid %s did not exit after SIGKILL", pid
        )
    return True


def stop_daemons_for_plans_root(
    plans_root: Path,
    *,
    timeout: float = 5.0,
    lister: Optional[Callable[[], Sequence[ReaperDaemon]]] = None,
    signal_fn: Optional[Callable[[int, str], None]] = None,
    alive_fn: Optional[Callable[[int], Optional[bool]]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> List[int]:
    """Stop the reaper daemons that serve *plans_root*, and report them.

    The returned list holds the pids that were actually signalled — the
    daemons this call reached.  A daemon that was already gone, one that
    serves another root, and one whose signal failed are all absent from
    it, because none of them was stopped by this call.

    *timeout* is the grace period: how long a daemon may take to react
    to the SIGTERM before it is sent the SIGKILL.  The same window
    bounds the wait after the SIGKILL, so a daemon that ignores even
    that is reported rather than waited on forever.

    The seams are the same ones the reaping stage uses, so the ladder
    can be exercised without a single real signal:

    *lister*
        returns the daemons to consider; the module's own
        :func:`list_reaper_daemons` when it is not given.
    *signal_fn*
        is called as ``signal_fn(pid, "TERM"|"KILL")``.
    *alive_fn*
        is called with a pid and answers ``True`` (alive), ``False``
        (gone) or ``None`` (could not be checked).  A pid that has
        exited but has not been waited for is *gone* — and waiting for
        it is what the helper does on the way out, so the exit status
        of a daemon this process started is consumed here.
    *sleep_fn*
        is called with the poll step.

    Nothing here raises.  A daemon that refuses to be signalled is
    logged and skipped, and one that survives the SIGKILL is logged and
    reported — a caller that is tearing down its own fixtures must not
    be taken down by a process that misbehaves.
    """
    census = list_reaper_daemons if lister is None else lister
    sender = _default_daemon_signal if signal_fn is None else signal_fn
    probe = _running_probe(_pid_alive if alive_fn is None else alive_fn)
    napper = time.sleep if sleep_fn is None else sleep_fn

    target = _resolved_root(plans_root)
    if target is None:
        logger.warning(
            "orphan reaper: cannot resolve plans root %r", plans_root
        )
        return []

    try:
        daemons = list(census())
    except Exception:  # noqa: BLE001 - a census that fails is an empty one
        logger.warning(
            "orphan reaper: could not read the daemon census", exc_info=True
        )
        return []

    stopped: List[int] = []
    seen: set = set()
    for daemon in daemons:
        try:
            if not _daemon_serves_root(daemon, target):
                continue

            pid = getattr(daemon, "pid", None)
            if not isinstance(pid, int) or pid <= 1:
                continue
            if pid == os.getpid():
                # The caller is never one of the daemons it stops.
                continue
            if pid in seen:
                # The same daemon listed twice is one daemon.
                continue
            seen.add(pid)

            if probe(pid) is False:
                # Already gone: there is nothing to stop.
                continue

            if _stop_one_daemon(
                pid,
                sender=sender,
                probe=probe,
                napper=napper,
                timeout=timeout,
            ):
                stopped.append(pid)
        except Exception:  # noqa: BLE001 - one daemon must not stop the rest
            logger.warning(
                "orphan reaper: stopping daemon pid %s failed",
                getattr(daemon, "pid", "?"),
                exc_info=True,
            )
    return stopped


# --------------------------------------------------------------------------
# The audit trail, the breakers and the alert
# --------------------------------------------------------------------------
#
# A reap that leaves no trace is a reap that cannot be explained.  Every
# verdict — the ones that were killed *and* the ones that were skipped —
# is written to a JSON-lines log, so that "what did the reaper decide,
# about which process, and what did it do about it" has an answer that
# does not depend on the memory of the machine it ran on.
#
# Two breakers sit above the ordinary cadence.  A box that suddenly has
# a flood of candidates, or one whose swap is nearly full, is not in a
# state that can wait for the next interval: both say "reclaim what is
# decidable, now, and tell somebody".
#
# Neither the log nor the alert may ever break the cleanup.  A log that
# cannot be written costs the log; an alert channel that is missing or
# broken costs the alert.  Neither may cost a process its memory.

#: Name of the kill-event log inside :data:`REAPER_DIRNAME`.
EVENTS_FILENAME = "kill_events.jsonl"

#: Name of the alert log inside :data:`REAPER_DIRNAME`.
ALERTS_FILENAME = "alerts.jsonl"


def events_path(plans_root: Path) -> Path:
    """Return the path of the reaper's kill-event log.

    The log lives beside the lock and the daemon log, under the same
    plan-artifact root the reaper was told to work on — so a reaper
    pointed at another root keeps its own history.
    """
    return Path(plans_root) / REAPER_DIRNAME / EVENTS_FILENAME


def alerts_path(plans_root: Path) -> Path:
    """Return the path of the reaper's alert log."""
    return Path(plans_root) / REAPER_DIRNAME / ALERTS_FILENAME


def _event_timestamp(ts: Optional[object] = None) -> str:
    """Return the timestamp stamped on a record.

    Without *ts* the current local time is used, to the second — the
    resolution a log is read at.  A caller that wants a fixed stamp
    passes one: a string, or a ``datetime``.
    """
    if ts is None:
        return datetime.datetime.now().isoformat(timespec="seconds")
    if isinstance(ts, datetime.datetime):
        return ts.isoformat(timespec="seconds")
    return str(ts)


def _event_record(verdict: object, stamp: str) -> Dict[str, object]:
    """Render one verdict as the record that is written to the log.

    The record is flat and self-describing: the process it is about
    (identity, lineage, resident size in kB, command line), the owner
    that was believed to have spawned it, and the two things the reaper
    decided — why, and what it did about it.  ``signal`` is empty when
    nothing was sent, which is the case for every verdict that was not a
    kill.
    """
    proc = getattr(verdict, "proc", None)
    return {
        "ts": stamp,
        "pid": getattr(proc, "pid", None),
        "ppid": getattr(proc, "ppid", None),
        "pgid": getattr(proc, "pgid", None),
        "argv": getattr(proc, "argv", ""),
        "rss": getattr(proc, "rss_kb", 0),
        "owner_pid": getattr(verdict, "owner_pid", None),
        "owner_start": getattr(verdict, "owner_start", ""),
        "reason": getattr(verdict, "reason", ""),
        "signal": getattr(verdict, "signal", ""),
    }


def write_events(
    path: Path,
    verdicts: Iterable[Verdict],
    *,
    ts: Optional[object] = None,
) -> None:
    """Append one JSON line per verdict to *path*.

    The log is appended to, never rewritten: a reaper that runs every
    thirty seconds must not lose the record of what it did the last
    thirty seconds ago.  The parent directory is created when it is
    missing.

    The log has a **ceiling**.  It is rotated aside when the next line
    would carry it past :data:`MAX_EVENTS_BYTES`, and one generation is
    kept — so the log on disk is bounded even when the machine is not.
    A reaper that ran every thirty seconds for a month, writing one
    line per process it looked at, had produced 1.1 GB of it on
    2026-11-02; the count is the information, and the file does not
    have to be bigger than the answer.

    A write that fails is a warning and nothing else.  The reaper exists
    to give memory back; a full disk or a directory that is not a
    directory must not stop it from doing that.
    """
    target = Path(path)
    stamp = _event_timestamp(ts)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(_event_record(verdict, stamp)) + "\n" for verdict in verdicts
        ]
        _append_event_lines(target, lines)
    except (OSError, TypeError, ValueError):
        logger.warning(
            "orphan reaper: the kill events could not be written to %s",
            target,
            exc_info=True,
        )


def _events_cap() -> int:
    """Return the ceiling on the event log, in bytes.

    Zero means "no ceiling": a deployment that has to keep every
    record of every pass can say so, and the log then grows the way it
    always did.
    """
    try:
        cap = int(MAX_EVENTS_BYTES)
    except (TypeError, ValueError):  # pragma: no cover - a malformed constant
        return 0
    return cap if cap > 0 else 0


def _events_size(target: Path) -> int:
    """Return the size of the live event log, or zero when there is none."""
    try:
        return target.stat().st_size
    except OSError:
        return 0


def _rotate_events(target: Path) -> None:
    """Move the live event log aside, keeping one generation.

    The older generation is dropped: a log that keeps its own history
    is a log with no ceiling, which is the thing this rotation exists
    to prevent.
    """
    rotated = target.with_name(target.name + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        target.replace(rotated)
    except OSError:  # pragma: no cover - a log that cannot be rotated is a log
        logger.warning(
            "orphan reaper: the event log %s could not be rotated",
            target,
            exc_info=True,
        )


def _append_event_lines(target: Path, lines: Sequence[str]) -> None:
    """Append *lines* to the event log, rotating it when it would grow too big.

    The rotation happens **before** the line that would cross the
    ceiling is written, so the live log never passes it — not even for
    the length of one line.
    """
    cap = _events_cap()
    handle = None
    try:
        for line in lines:
            if handle is None:
                handle = target.open("a", encoding="utf-8")
            if cap:
                size = _events_size(target)
                if size and size + len(line.encode("utf-8")) > cap:
                    handle.close()
                    handle = None
                    _rotate_events(target)
                    handle = target.open("a", encoding="utf-8")
            handle.write(line)
    finally:
        if handle is not None:
            handle.close()


def _is_ours(verdict: object) -> bool:
    """Return whether *verdict* is about a process that carried our marker.

    A verdict about one of our own processes is a decision somebody may
    have to explain later, so it keeps a record of its own.  A verdict
    about a process that never carried the marker is noise: every
    machine has hundreds of those, and the reaper looks at them all,
    every thirty seconds, for ever.
    """
    if getattr(verdict, "owner_pid", None) is not None:
        return True
    return bool(getattr(verdict, "kind", ""))


def write_skipped_summary(
    path: Path,
    verdicts: Iterable[Verdict],
    *,
    ts: Optional[object] = None,
) -> int:
    """Write one line counting the verdicts that were skipped.

    The count is the information.  A thousand lines spelling out that
    a thousand processes were skipped are not — and they are what grew
    the log to 1.1 GB on 2026-11-02.

    The summary line carries no pid, no reason of its own and no
    signal: it is not about any one process, and a reader that is
    looking for a kill must not find one here.  The reasons it counts
    are in ``reasons``, keyed by the same reason codes the individual
    records use.

    The number of skipped verdicts is returned; zero means nothing was
    written.
    """
    target = Path(path)
    counts: Dict[str, int] = {}
    total = 0
    for verdict in verdicts:
        reason = str(getattr(verdict, "reason", "") or "")
        counts[reason] = counts.get(reason, 0) + 1
        total += 1
    if not total:
        return 0

    record = {
        "ts": _event_timestamp(ts),
        "type": "summary",
        "skipped": total,
        "reasons": counts,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _append_event_lines(target, [json.dumps(record) + "\n"])
    except (OSError, TypeError, ValueError):
        logger.warning(
            "orphan reaper: the kill events could not be written to %s",
            target,
            exc_info=True,
        )
    return total


def write_pass_events(
    path: Path,
    verdicts: Iterable[Verdict],
    *,
    ts: Optional[object] = None,
) -> None:
    """Write down what one pass decided.

    The decisions about **our own** processes — the ones that carried
    the marker — are written one by one, because each of them is a
    decision somebody may have to explain later.  The rest are written
    as one count, because they are the same answer over and over: the
    machine is full of processes that are not ours, and the reaper
    looks at every one of them, every thirty seconds, for ever.
    """
    ours = []
    theirs = []
    for verdict in verdicts:
        if _is_ours(verdict):
            ours.append(verdict)
        else:
            theirs.append(verdict)
    if ours:
        write_events(path, ours, ts=ts)
    if theirs:
        write_skipped_summary(path, theirs, ts=ts)


def _swap_threshold_ratio(config: ReaperConfig) -> float:
    """Return the configured swap threshold as a fraction of the swap.

    The configuration speaks in percent — ``PDT_ORPHAN_REAPER_SWAP_THRESHOLD``
    is ``90`` for ninety percent — while the reading is a fraction
    (``0.9``).  A threshold of one or below is already a fraction and is
    taken as one.
    """
    try:
        threshold = float(config.swap_threshold)
    except (AttributeError, TypeError, ValueError):
        threshold = float(DEFAULT_SWAP_THRESHOLD)
    if threshold > 1.0:
        threshold = threshold / 100.0
    return threshold


def _candidate_count(verdicts: Iterable[object]) -> int:
    """Return the number of decidable orphans among *verdicts*.

    A candidate is a process the ladder judged to be an orphan — a
    verdict whose decision is ``kill``.  The processes that were
    skipped are not candidates: they carry no marker, belong to another
    user, or are the reaper itself, and every machine has hundreds of
    them.  Counting those would trip the breaker on every machine at
    every interval, and a breaker that is always on is not a breaker.

    It is also the count that matches what the breaker *does*: it cleans
    up the decidable orphans, so the orphans are what it counts.
    """
    count = 0
    for verdict in verdicts:
        if getattr(verdict, "decision", "") == DECISION_KILL:
            count += 1
    return count


def should_force_cleanup(
    scan_result: ScanResult,
    config: ReaperConfig,
    *,
    swap_ratio: Optional[float] = None,
) -> Tuple[bool, str]:
    """Decide whether the machine is in a state that demands a cleanup now.

    Returns ``(True, reason)`` when a breaker trips and ``(False, "")``
    otherwise.  The reason names the breaker and the figure that tripped
    it (``burst:51``, ``swap:0.93``), so that the alert can say which
    one it was.

    Two breakers exist:

    * **the burst** — more candidates than ``burst_threshold``.  The
      candidates are the orphans the scan decided on: the processes it
      judged to be orphans, which are the ones the cleanup will act on.
      A box that produced a flood of them at once is the very situation
      this module was written for, and waiting for the next interval is
      waiting for the jetsam.
    * **the swap** — the machine's swap is more than ``swap_threshold``
      full.  Swap that is nearly full is memory that has already been
      lost.

    Both comparisons are *strict*: a candidate count exactly at the
    threshold, or a swap usage exactly at the threshold, is the last
    value that is still fine.

    A ``swap_ratio`` of ``None`` means the swap usage could not be read,
    which is *unknown* — never "no swap in use" — so it never trips the
    breaker.

    This function decides.  It never signals: the signal belongs to the
    reaping stage, which is where every refusal lives.
    """
    verdicts = getattr(scan_result, "verdicts", None)
    if verdicts is None:
        # A caller that hands over a plain list of verdicts gets the same
        # answer; anything else is counted as no candidates at all.
        verdicts = scan_result if isinstance(scan_result, (list, tuple)) else []
    candidates = _candidate_count(verdicts)
    try:
        burst_threshold = int(config.burst_threshold)
    except (AttributeError, TypeError, ValueError):
        burst_threshold = DEFAULT_BURST_THRESHOLD
    if candidates > burst_threshold:
        return True, f"burst:{candidates}"

    if swap_ratio is None:
        return False, ""
    try:
        ratio = float(swap_ratio)
    except (TypeError, ValueError):
        return False, ""
    if ratio > _swap_threshold_ratio(config):
        return True, f"swap:{swap_ratio}"
    return False, ""


def _log_alert(config: ReaperConfig, message: str) -> None:
    """Write an alert to the alert log and to stderr.

    Both are the last resort: the log is what an operator reads after
    the fact, and stderr is what a daemon's log file captures.  Neither
    is allowed to raise — an alert that cannot be written must not
    become the failure that stops the cleanup.
    """
    record = {"ts": _event_timestamp(), "message": message}
    plans_root = getattr(config, "plans_root", None)
    try:
        if plans_root is not None:
            path = alerts_path(plans_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
    except Exception:  # pragma: no cover - the contract is "never raises"
        logger.warning(
            "orphan reaper: the alert could not be written to %s",
            plans_root,
            exc_info=True,
        )
    try:
        print(message, file=sys.stderr)
    except Exception:  # pragma: no cover - stderr is the last resort
        logger.warning("orphan reaper: the alert could not be printed")


def emit_alert(
    config: ReaperConfig,
    message: str,
    *,
    notify: Optional[Callable[[str], None]] = None,
) -> None:
    """Deliver an alert, and never fail because delivering it failed.

    *notify* is the channel: a callable that is given the message.  It
    is injected so that the reaper works without any channel at all,
    and so that a deployment can plug in whatever it has.

    When there is no channel, or the channel raises, the alert is
    written to ``alerts.jsonl`` under the reaper's directory and to
    stderr — the two places that are always there.  Nothing is raised:
    an alert that cannot be delivered must not stop the cleanup it was
    announcing, and the cleanup is the whole point.
    """
    text = str(message)
    if notify is not None:
        try:
            notify(text)
            return
        except Exception:
            logger.warning(
                "orphan reaper: the alert channel failed; the alert is logged instead",
                exc_info=True,
            )
    _log_alert(config, text)


# --------------------------------------------------------------------------
# The two run modes
# --------------------------------------------------------------------------
#
# One module, two ways to run: a single pass over the machine
# (``--once``) and a resident scanner (``--daemon``).  Both do the same
# four things in the same order — scan, decide, act, write down — and
# differ only in what happens after the write-down: the single pass
# prints what it found and leaves; the daemon waits, and looks again.
#
# The daemon's life is bounded on purpose.  It leaves when it is told
# to (``SIGTERM`` / ``SIGINT``), and it leaves by itself when the
# machine has been quiet for long enough that there is nothing left for
# it to reclaim: no owner alive and no candidate at all.  A daemon that
# never exits is a daemon that outlives the thing it was started for.

#: Sentinel for a keyword the caller did not inject.  ``None`` cannot
#: play that role: ``None`` is a value several of these keywords have.
_UNSET: Any = object()


def _verdicts_of(result: object) -> List[Any]:
    """Return the verdicts of a scan result.

    A caller that hands over a plain list of verdicts gets the same
    answer; anything else is counted as no verdicts at all.
    """
    verdicts = getattr(result, "verdicts", None)
    if verdicts is None:
        verdicts = result if isinstance(result, (list, tuple)) else []
    try:
        return list(verdicts)
    except TypeError:
        return []


def _verdict_line(verdict: object) -> str:
    """Render one verdict as the line the single pass prints.

    The line is ``pid decision reason`` — plus the marker kind when the
    verdict carries one, because the kind is what tells an operator
    *what* was left behind: a service, a fixture, a subprocess.
    """
    proc = getattr(verdict, "proc", None)
    parts = [
        str(getattr(proc, "pid", "")),
        str(getattr(verdict, "decision", "")),
        str(getattr(verdict, "reason", "")),
    ]
    kind = getattr(verdict, "kind", "") or ""
    if kind:
        parts.append(str(kind))
    return " ".join(parts)


def _accepts_keyword(func: object, name: str) -> bool:
    """Return whether *func* can be called with the keyword *name*."""
    try:
        parameters = inspect.signature(func).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _read_clock(clock: Callable[[], object], fallback: float) -> float:
    """Read the injected clock, falling back when it cannot be read.

    A clock that raises is a clock that cannot be believed; the daemon
    keeps running on the previous reading rather than dying on it.
    """
    try:
        return float(clock())  # type: ignore[arg-type]
    except Exception:  # pragma: no cover - the contract is "never raises"
        return fallback


def _stop_event_is_set(event: object) -> bool:
    """Return whether the stop event says "stop"."""
    checker = getattr(event, "is_set", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:  # pragma: no cover - the contract is "never raises"
        return False


def _wait_for_stop(event: object, timeout: float) -> bool:
    """Wait up to *timeout* seconds; return whether the stop was asked for."""
    waiter = getattr(event, "wait", None)
    if waiter is None:
        try:
            time.sleep(timeout)
        except Exception:  # pragma: no cover - the contract is "never raises"
            pass
        return False
    try:
        return bool(waiter(timeout))
    except Exception:  # pragma: no cover - the contract is "never raises"
        return False


def _install_stop_handlers(event: Any) -> Dict[int, Any]:
    """Make ``SIGTERM`` and ``SIGINT`` set *event*.

    The daemon has to be able to leave gracefully: the signal is what
    an operator (or a service manager) sends, and the loop must notice
    it.  The previous handlers are returned so that they can be put
    back — the daemon is a guest in somebody else's process.
    """
    previous: Dict[int, Any] = {}

    def handler(signum, frame):  # pragma: no cover - exercised by a signal
        try:
            event.set()
        except Exception:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError, AttributeError):
            # Not the main thread, or no such signal on this platform.
            continue
    return previous


def _restore_stop_handlers(previous: Dict[int, Any]) -> None:
    """Put the signal handlers back the way they were found."""
    for sig, old in previous.items():
        try:
            signal.signal(sig, old)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover
            continue


def _cleanup_pass(
    config: ReaperConfig,
    result: ScanResult,
    *,
    reap_fn: Optional[Callable[..., List[Verdict]]] = None,
    alert_fn: Optional[Callable[..., None]] = None,
    send: Optional[Callable[[int, str, Optional[int]], None]] = None,
    swap_ratio: Any = _UNSET,
) -> List[Verdict]:
    """Act on what the scan decided, and write down what was done.

    The order matters.  The breaker is evaluated *before* the cleanup,
    and its alert is delivered *before* the cleanup starts, so that an
    operator told about the flood is told about it while the memory is
    still being given back.

    A broken alert channel costs the alert and nothing else: the
    cleanup goes ahead regardless.  Neither may ever cost a process its
    memory — that is the whole point of the module.
    """
    alerts = emit_alert if alert_fn is None else alert_fn
    reaper = reap_verdicts if reap_fn is None else reap_fn

    ratio = swap_usage_ratio() if swap_ratio is _UNSET else swap_ratio
    try:
        forced, reason = should_force_cleanup(result, config, swap_ratio=ratio)
    except Exception:  # pragma: no cover - the contract is "never raises"
        logger.warning(
            "orphan reaper: the breaker could not be evaluated", exc_info=True
        )
        forced, reason = False, ""
    if forced:
        try:
            alerts(config, "forced cleanup: %s" % reason)
        except Exception:  # pragma: no cover - the contract is "never raises"
            logger.warning(
                "orphan reaper: the alert failed; the cleanup continues",
                exc_info=True,
            )

    verdicts = _verdicts_of(result)
    try:
        if send is not None and _accepts_keyword(reaper, "send"):
            reaped = list(reaper(verdicts, config, send=send))
        else:
            reaped = list(reaper(verdicts, config))
    except Exception:  # pragma: no cover - the contract is "never raises"
        logger.warning("orphan reaper: the reaping stage failed", exc_info=True)
        reaped = verdicts

    write_pass_events(events_path(config.plans_root), reaped)
    return reaped


def run_once(
    config: ReaperConfig,
    *,
    scan_fn: Optional[Callable[[ReaperConfig], ScanResult]] = None,
    reap_fn: Optional[Callable[..., List[Verdict]]] = None,
    alert_fn: Optional[Callable[..., None]] = None,
    send: Optional[Callable[[int, str, Optional[int]], None]] = None,
    swap_ratio: Any = _UNSET,
) -> ScanResult:
    """Make one pass over the machine and report what was found.

    The pass is: scan, reap, write the events down, print one line per
    verdict.  The lines are what a human reads when they ask "what did
    it decide, and about what" — ``pid decision reason`` and, when the
    verdict carries one, the marker kind.

    A reaper that is switched off says so and does nothing:
    ``disabled`` is printed and an empty result is returned.

    The result is the scan's, with the verdicts updated in place by
    the reaping stage — so ``verdict.signal`` says what was sent.
    """
    if not getattr(config, "enabled", True):
        print("disabled")
        return ScanResult()

    scanner = scan if scan_fn is None else scan_fn
    try:
        result = scanner(config)
    except Exception:  # pragma: no cover - the contract is "never raises"
        logger.warning("orphan reaper: the scan failed", exc_info=True)
        return ScanResult()

    _cleanup_pass(
        config,
        result,
        reap_fn=reap_fn,
        alert_fn=alert_fn,
        send=send,
        swap_ratio=swap_ratio,
    )

    for verdict in _verdicts_of(result):
        print(_verdict_line(verdict))

    return result


def _plans_root_gone(config: ReaperConfig) -> bool:
    """True when the daemon's plan-artifact root is no longer there.

    The daemon is started inside a directory — usually the plan-artifact
    root it is meant to watch — and that directory can be deleted while
    the daemon is running (a test's temporary directory is deleted when
    the session ends).  A daemon whose world is gone has nothing to
    reclaim, and the twenty-six daemons that leaked on 2026-10-31 were
    all of that kind.

    A root that cannot be read is **not** treated as gone: an
    unreadable path is unknown, and killing the daemon because a
    directory could not be stat'ed would be the wrong way to fail.
    """
    root = getattr(config, "plans_root", None)
    if root is None:
        return False
    try:
        return not Path(root).exists()
    except OSError:  # pragma: no cover - an unreadable path is not "gone"
        return False


def _plans_root_identity(root: object) -> Optional[Tuple[int, int]]:
    """Return the filesystem's own name for *root*, or ``None``.

    A path is not an identity.  The directory a daemon was started for
    can be deleted and another one created in its place, and the new
    directory has the same path -- so the existence check
    (:func:`_plans_root_gone`) sees nothing wrong while the daemon
    goes on guarding a directory it was never started for.  On
    2026-11-02 that is how one root ended up carrying fifteen daemons:
    ``.orphan_reaper/reaper.lock`` was deleted and recreated, and every
    daemon saw a lock file that was not the one it held.

    What tells two directories apart is the filesystem's own name for
    them: the device they live on and the inode they have.  The pair
    is recorded when the daemon starts and compared on every pass.

    ``None`` means "no identity could be read" -- the root does not
    exist, or it could not be stat'ed.  A daemon holding no identity
    cannot judge anything, and says so rather than guessing.
    """
    if root is None:
        return None
    try:
        st = Path(root).stat()
    except (OSError, TypeError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _plans_root_replaced(root: object, identity: Optional[Tuple[int, int]]) -> bool:
    """True when *root* is no longer the directory that was recorded.

    *identity* is the pair recorded by :func:`_plans_root_identity`
    when the daemon started; ``None`` means no identity was ever
    recorded, and a daemon with nothing to compare against cannot
    conclude that anything was replaced.

    The two failure modes are kept apart on purpose:

    * a root that is **not there** reads as replaced.  That is the
      same thing the existence check reports, and the daemon leaves for
      it in either case -- the two checks have to agree, or the daemon
      would keep running for one of them.
    * a root that **cannot be read** reads as *not* replaced.  The
      filesystem declined to answer, which is unknown, and unknown is
      not a reason to stop reclaiming memory on a machine that still
      needs it.
    """
    if identity is None:
        return False
    try:
        st = Path(root).stat()
    except FileNotFoundError:
        # The root is gone -- the same thing the existence check sees.
        return True
    except (OSError, TypeError, ValueError):
        # Unreadable: unknown, not replaced.
        return False
    return (st.st_dev, st.st_ino) != identity


def _owner_gone(config: ReaperConfig) -> bool:
    """True when the process that started this daemon is gone.

    A daemon is started by something — a test fixture, a verification
    round, the server — and that something is the reason it exists.
    When it is over, the daemon has outlived the only reason it
    existed: the machine is left with daemons re-parented to ``pid
    1``, each of them walking the whole process table every thirty
    seconds for ever.

    The judgement is the judgement the reaper makes everywhere else:

    * A pid on its own is not an identity — the kernel recycles pids.
      The owner is therefore recorded as a pid **and** a start time,
      and a live pid whose start time differs from the recorded one is
      somebody else's process: the owner is gone.
    * Uncertainty resolves towards *staying*.  An owner that cannot be
      checked, an owner whose start time was never recorded, an owner
      whose start time cannot be read now — none of them is a dead
      owner, and a daemon that leaves on a guess stops reclaiming
      memory on a machine that still needs it.
    * The daemon the **server** starts is exempt (``survive_owner``):
      "the backend died" is precisely the situation it exists for, so
      it has to outlive the process that started it.

    A dead pid needs no start time to be conclusive: the kernel said
    "no such process", and that is the end of it.
    """
    if getattr(config, "survive_owner", False):
        return False

    owner_pid = getattr(config, "owner_pid", None)
    if owner_pid is None:
        return False
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError):
        return False
    if owner_pid <= 1:
        # ``pid 1`` is the kernel's own reaper, not a process that
        # started anything -- and it never dies.
        return False

    alive = _pid_alive(owner_pid)
    if alive is None:
        # Could not be checked: unknown, not dead.
        return False
    if alive is False:
        return True

    # The pid is alive.  It is only *our* owner if the recorded start
    # time is the one the pid has now.
    owner_start = getattr(config, "owner_start", "") or ""
    if not owner_start:
        return False
    observed = process_start_epoch(owner_pid)
    if not observed:
        # The platform declined to say when the process started.  That
        # is not the same as saying it started at a different moment.
        return False
    return not _same_start(owner_start, observed, _start_tolerance())


def _daemon_loop(
    config: ReaperConfig,
    *,
    event: Any,
    clock: Optional[Callable[[], object]],
    scan_fn: Optional[Callable[[ReaperConfig], ScanResult]],
    reap_fn: Optional[Callable[..., List[Verdict]]],
    alert_fn: Optional[Callable[..., None]],
    send: Optional[Callable[[int, str, Optional[int]], None]],
    swap_ratio: Any,
) -> int:
    """The daemon's body: scan, reap, wait, and do it again.

    The loop ends in one of seven ways, and every one of them is a
    return of zero:

    * it is asked to stop — the stop event is set, and the loop leaves
      at the next opportunity;
    * it is asked to stop *between* the scan and the cleanup, in which
      case the pending cleanup is dropped rather than started: a daemon
      that was told to stop must not go on killing things;
    * the machine has been quiet for ``idle_exit`` seconds — no
      candidate and no owner alive — in which case it leaves on its
      own, because there is nothing left for it to reclaim;
    * the plan-artifact root it was started for has been deleted, in
      which case it leaves: a daemon watching a directory that is not
      there is watching nothing;
    * the plan-artifact root it was started for has been **replaced**
      -- deleted and re-created, so the path is the same but the
      directory is not -- in which case it leaves too: a daemon
      watching a directory it was never started for is watching the
      wrong thing (see :func:`_plans_root_replaced`);
    * it has reached ``max_lifetime``, in which case it leaves: a
      daemon of any age has to have an end;
    * the process that started it is gone, in which case it leaves:
      the daemon was started for that process, and it is over (see
      :func:`_owner_gone`; the daemon the server starts is exempt).

    The idle streak is reset by the machine being **in use** — by the
    candidates (the processes this daemon would actually reclaim) and
    by ``owners_alive`` (the owners that are still running on it).  A
    machine that is still in use is not an idle machine, so the daemon
    may only leave a machine that has **nothing left on it**: no
    candidate *and* no owner alive.

    Both ends of that rule are needed.  The streak used to be reset by
    the owners as well, and that made the streak unreachable on any
    machine with a PDT instance on it: the count of live owners says
    *somebody is using PDT*, and twenty-six daemons were running on
    one machine for exactly that reason (2026-10-31).  Ignoring the
    owners instead cost the machine its reaper: a daemon that leaves
    while the machine is still in use stops reclaiming the orphans
    that appear after it left, and nothing starts it again —
    ``ensure_daemon`` is called once, when the server starts.

    The streak is measured from the moment the daemon *observed* the
    machine to be quiet — the first pass that found neither a
    candidate nor an owner.  The daemon has to watch a quiet machine
    for the whole idle window before it may call it quiet; it does
    not inherit the quiet time of the machine it was born into.

    The one exception is a machine that has **never had an owner
    alive**: nothing on it belongs to PDT, so nothing on it can produce
    work, and the machine has been quiet since the last time this
    daemon had something to reclaim.  There the streak is dated back to
    that moment — the daemon is watching a machine that cannot even
    start giving it work.

    A machine that *had* owners and lost them is not such a machine:
    the children that just lost their owner are exactly the orphans
    this daemon exists to reclaim, so its streak starts over at the
    first pass that finds the machine unused.

    A scan that fails does not end the loop: the failure is logged and
    the loop goes on.  A daemon that gives up because one pass went
    wrong would stop reclaiming memory exactly when the machine needs
    it most.
    """
    scanner = scan if scan_fn is None else scan_fn
    reader = time.monotonic if clock is None else clock

    try:
        interval = max(0.0, float(getattr(config, "interval", DEFAULT_INTERVAL)))
    except (TypeError, ValueError):  # pragma: no cover - a malformed config
        interval = float(DEFAULT_INTERVAL)
    try:
        idle_limit = float(getattr(config, "idle_exit", DEFAULT_IDLE_EXIT))
    except (TypeError, ValueError):  # pragma: no cover - a malformed config
        idle_limit = float(DEFAULT_IDLE_EXIT)
    try:
        max_lifetime = float(getattr(config, "max_lifetime", DEFAULT_MAX_LIFETIME))
    except (TypeError, ValueError):  # pragma: no cover - a malformed config
        max_lifetime = float(DEFAULT_MAX_LIFETIME)

    # The reference point of the daemon's own age.
    last_now = _read_clock(reader, 0.0)
    started_at = last_now
    # The moment the machine was last busy *for this daemon*: the last
    # time it had something to reclaim, or the last time an owner was
    # alive on it.  It is where the idle streak is dated back to on a
    # machine that cannot produce any more work.
    anchor = started_at
    # The moment the current idle streak began: the first pass that
    # found neither a candidate nor an owner.
    quiet_since: Optional[float] = None
    # Whether an owner has ever been seen alive on this machine while
    # this daemon was watching it.  A machine that has had PDT on it
    # cannot have its quietness dated back: the children that lost
    # their owner are exactly the orphans this daemon exists to
    # reclaim.
    owner_seen = False

    # The identity of the plan-artifact root: the filesystem's own
    # name for the directory this daemon was started for.  Existence
    # is not identity -- a root that is deleted and re-created has the
    # same path but a different inode -- so the daemon records the
    # pair here and compares it on every pass.
    root_identity = _plans_root_identity(getattr(config, "plans_root", None))

    while True:
        if _stop_event_is_set(event):
            return 0

        # The process that started this daemon is gone.  There is
        # nothing left to watch: the daemon was started for that
        # process, and it is over.
        if _owner_gone(config):
            logger.info(
                "orphan reaper: the owner %s is gone; leaving",
                getattr(config, "owner_pid", None),
            )
            return 0

        now = _read_clock(reader, last_now)

        # The world this daemon watches is gone.  It was born inside a
        # directory that has since been deleted (a test's temporary
        # directory, say), and there is nothing left for it to reclaim.
        if _plans_root_gone(config):
            logger.info(
                "orphan reaper: the plan-artifact root %s is gone; leaving",
                getattr(config, "plans_root", None),
            )
            return 0

        # The root is still there -- but it is not the same root.  It
        # was deleted and re-created while this daemon was watching it,
        # so the daemon is now guarding a directory it was never
        # started for, and the lock it holds is a lock on a file that
        # is no longer there.
        if _plans_root_replaced(
            getattr(config, "plans_root", None), root_identity
        ):
            logger.info(
                "orphan reaper: the plan-artifact root %s has been replaced; "
                "leaving",
                getattr(config, "plans_root", None),
            )
            return 0

        # The ceiling on the daemon's own age.
        if max_lifetime > 0 and (now - started_at) >= max_lifetime:
            logger.info(
                "orphan reaper: the daemon has reached its maximum "
                "lifetime of %.0f seconds; leaving",
                max_lifetime,
            )
            return 0

        try:
            result = scanner(config)
        except Exception:  # pragma: no cover - the contract is "never raises"
            logger.warning(
                "orphan reaper: the scan failed; the loop continues", exc_info=True
            )
            result = ScanResult()

        verdicts = _verdicts_of(result)
        candidates = _candidate_count(verdicts)
        try:
            owners_alive = int(getattr(result, "owners_alive", 0) or 0)
        except (TypeError, ValueError):  # pragma: no cover - a malformed result
            owners_alive = 0
        # ``owners_alive`` counts the owners that are still running on
        # this machine: it says "PDT is in use here".  It decides the
        # idle streak together with the candidates -- see below.
        logger.debug(
            "orphan reaper: %s candidate(s), %s owner(s) alive",
            candidates, owners_alive,
        )

        if candidates > 0 or owners_alive > 0:
            # There is work to do, or the machine is still in use: the
            # idle streak starts over.
            #
            # ``owners_alive`` counts the marked processes whose owner
            # is still running.  A machine that is still in use is not
            # an idle machine, and a daemon that leaves one stops
            # reclaiming the orphans that appear after it left — and
            # nothing starts it again: ``ensure_daemon`` is called
            # once, when the server starts.  So the owners count as
            # work now: the streak may only start once the last owner
            # is gone.
            anchor = now
            quiet_since = None
            if owners_alive > 0:
                # This machine has PDT on it.  Remember that: the
                # children of an owner may outlive it, so the
                # quietness of such a machine may not be dated back to
                # a moment before its owners died.
                owner_seen = True
        else:
            if quiet_since is None:
                # The streak is measured from the first pass that
                # actually saw an idle machine, not from the moment the
                # daemon was started: the daemon has to *observe* a
                # quiet machine for ``idle_exit`` seconds before it may
                # call the machine quiet.  A daemon that starts into a
                # busy machine therefore gets its full idle window once
                # the machine goes quiet.
                quiet_since = now
            # A machine that has **never** had an owner alive cannot
            # produce work: nothing on it belongs to PDT, so nothing on
            # it can become a candidate.  The machine has therefore been
            # quiet since the last time this daemon had something to
            # reclaim, and the streak may be dated back to that moment.
            #
            # A machine that *had* owners is a different machine: the
            # children that just lost their owner are exactly the
            # orphans this daemon exists to reclaim, so its streak
            # starts over at the first pass that finds it unused.
            idle_since = quiet_since if owner_seen else anchor
            if idle_limit > 0 and (now - idle_since) >= idle_limit:
                logger.info(
                    "orphan reaper: the machine has been idle for %.0f seconds; "
                    "leaving",
                    idle_limit,
                )
                return 0

        if _stop_event_is_set(event):
            # Asked to stop while the scan was running: drop the pending
            # cleanup rather than start one.
            return 0

        _cleanup_pass(
            config,
            result,
            reap_fn=reap_fn,
            alert_fn=alert_fn,
            send=send,
            swap_ratio=swap_ratio,
        )

        last_now = now
        if _wait_for_stop(event, interval):
            return 0


def run_daemon(
    config: ReaperConfig,
    *,
    stop: Optional[Any] = None,
    clock: Optional[Callable[[], object]] = None,
    scan_fn: Optional[Callable[[ReaperConfig], ScanResult]] = None,
    reap_fn: Optional[Callable[..., List[Verdict]]] = None,
    alert_fn: Optional[Callable[..., None]] = None,
    send: Optional[Callable[[int, str, Optional[int]], None]] = None,
    lock_fn: Optional[Callable[[Path], Optional[IO]]] = None,
    swap_ratio: Any = _UNSET,
) -> int:
    """Stay resident: scan, reap, wait, scan again — until told to stop.

    The daemon is a **singleton**: it takes the reaper's lock before it
    does anything, and if the lock is already held it returns
    immediately.  Two reapers would race over the same candidates and
    apply the same grace period twice, independently.

    The lock is per *root*, though, and it can be defeated by deleting
    the lock file — so the daemon checks the machine as well, after it
    has taken the lock and before it starts scanning: it stands down
    when another daemon is already serving its root, and when the
    machine is already running its quota of daemons
    (:data:`DEFAULT_MAX_DAEMONS`).  Both are answered with the same
    thing the lock is: a return of zero, and a line in the log saying
    why.

    The two checks answer two different questions, and they are asked
    of two different populations.  "Is somebody else already watching
    this root?" is about *correctness*: two daemons on one root reap
    the same candidates and apply the same grace period twice, so it
    is asked of every daemon, embedded or not.  "Is the machine full?"
    is about the machine's resident daemons — the processes of their
    own that outlive whoever started them — so it is asked of a daemon
    that is about to *become* one of them, which is a daemon that was
    given no stop event and therefore keeps running on its own.  A
    caller that brings its own stop event runs the loop inside a
    process it owns: that daemon leaves with the caller, it is not in
    the census the quota counts, and it does not add to the population
    the ceiling exists to bound.

    *stop* is the event the loop watches; when it is not given, the
    daemon makes one and arranges for ``SIGTERM`` and ``SIGINT`` to set
    it.  *clock* is the time the idle streak is measured against.

    The lock is released when the daemon leaves, whatever made it
    leave.
    """
    if not getattr(config, "enabled", True):
        print("disabled")
        return 0

    locker = acquire_lock if lock_fn is None else lock_fn
    try:
        handle = locker(config.plans_root)
    except Exception:  # pragma: no cover - the contract is "never raises"
        logger.warning(
            "orphan reaper: the lock could not be taken", exc_info=True
        )
        return 0
    if handle is None:
        logger.info(
            "orphan reaper: another reaper holds the lock; standing down"
        )
        return 0

    owned = stop is None
    event = threading.Event() if owned else stop

    try:
        # One reading of the machine serves both checks.  A machine
        # that cannot be read yields an empty census, which is the
        # permissive answer: a reaper that refuses to start over an
        # unreadable process table would leave the memory it exists to
        # reclaim unreclaimed.
        procs = list_processes()

        other_pid = _another_daemon_for_root(config.plans_root, procs=procs)
        if other_pid is not None:
            logger.info(
                "another reaper is already watching %s; standing down",
                config.plans_root,
            )
            return 0

        # The quota is a bound on the machine's *resident* daemons: the
        # ones that are processes of their own, that outlive whoever
        # started them, and that nothing shuts down.  A caller that
        # hands in its own stop event is running the loop inside a
        # process it owns -- that daemon is not a member of the
        # population the quota counts, it does not show up in the
        # census, and it leaves together with its caller.  So the
        # quota is checked where that population is: on the daemon that
        # is about to join it.
        if owned and _daemon_budget_exhausted(config, procs=procs):
            logger.info(
                "daemon budget %s exhausted; standing down",
                getattr(config, "max_daemons", DEFAULT_MAX_DAEMONS),
            )
            return 0

        previous = _install_stop_handlers(event) if owned else {}
        try:
            return _daemon_loop(
                config,
                event=event,
                clock=clock,
                scan_fn=scan_fn,
                reap_fn=reap_fn,
                alert_fn=alert_fn,
                send=send,
                swap_ratio=swap_ratio,
            )
        finally:
            _restore_stop_handlers(previous)
    finally:
        try:
            handle.close()
        except Exception:  # pragma: no cover - closing a handle we opened
            pass


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """argparse type for a strictly positive integer.

    The environment path falls back to the default for an unusable
    value; a command line is an explicit instruction, so an unusable one
    is an error the operator gets told about instead of a value silently
    replaced.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("%r is not an integer" % value)
    if number <= 0:
        raise argparse.ArgumentTypeError("%r must be greater than zero" % value)
    return number


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command line parser.

    The environment is read first and the command line is laid over it,
    so a flag always wins over the variable it shadows.
    """
    parser = argparse.ArgumentParser(
        prog="orphan_reaper",
        description=(
            "Reclaim the processes PDT spawned and forgot. The environment is "
            "read first; the flags below override it."
        ),
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--once",
        action="store_true",
        help="scan once, reap what is orphaned, print the verdicts and exit",
    )
    modes.add_argument(
        "--daemon",
        action="store_true",
        help="stay resident and scan every interval until told to stop",
    )
    modes.add_argument(
        "--status",
        action="store_true",
        help=(
            "print the reaper daemons running on this machine as JSON "
            "(pid, root, age) and exit"
        ),
    )
    parser.add_argument(
        "--plans-root",
        default=None,
        help="override the plan-artifact root (default: $PDT_PLANS_DIR or <repo>/plans)",
    )
    parser.add_argument(
        "--interval",
        type=_positive_int,
        default=None,
        help="override the scan interval in seconds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide but never signal",
    )
    parser.add_argument(
        "--owner-pid",
        type=int,
        default=None,
        help=(
            "the pid of the process that started this daemon; the daemon "
            "leaves when that process is gone (default: the parent)"
        ),
    )
    parser.add_argument(
        "--owner-start",
        default="",
        help=(
            "the start time of that owner, so that a recycled pid is not "
            "mistaken for it (default: read from the machine)"
        ),
    )
    parser.add_argument(
        "--survive-owner",
        action="store_true",
        help=(
            "stay resident even when the process that started this daemon "
            "is gone (the daemon the server starts)"
        ),
    )
    return parser


def _config_as_dict(config: ReaperConfig) -> Dict[str, object]:
    """Render a config as plain data (JSON-printable)."""
    return {
        "enabled": config.enabled,
        "interval": config.interval,
        "protect_ports": sorted(config.protect_ports),
        "grace": config.grace,
        "idle_exit": config.idle_exit,
        "plans_root": str(config.plans_root),
        "dry_run": config.dry_run,
        "burst_threshold": config.burst_threshold,
        "swap_threshold": config.swap_threshold,
        "pid_scope": list(config.pid_scope) if config.pid_scope is not None else None,
        "owner_pid": config.owner_pid,
        "owner_start": config.owner_start,
        "survive_owner": config.survive_owner,
        "detach_owner": config.detach_owner,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Run the reaper: one pass, or as a resident daemon.

    The reaper is intentionally runnable on its own — no sibling backend
    module is imported — so that it can be started from a checkout where
    the rest of the backend is unavailable.

    Without a mode the configuration is printed and nothing else
    happens; ``--once`` makes a single pass, ``--daemon`` stays
    resident and ``--status`` prints the census of the reaper daemons
    running on this machine as JSON.  All three answer with an exit
    code of zero: the reaper ran.
    """
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)

    config = config_from_env()
    if args.plans_root:
        config.plans_root = Path(args.plans_root).expanduser()
    if args.interval is not None:
        config.interval = args.interval
    if args.dry_run:
        config.dry_run = True

    if args.status:
        print(json.dumps(daemon_status(), indent=2))
        return 0
    if args.once:
        run_once(config)
        return 0
    if args.daemon:
        config.owner_pid = args.owner_pid
        config.owner_start = args.owner_start or ""
        config.survive_owner = bool(args.survive_owner)
        if not config.survive_owner and config.owner_pid is None:
            # Nobody said who the owner is, so the owner is the process
            # that started this one.  A daemon that has already been
            # orphaned has no owner to record -- ``pid 1`` is the
            # kernel's own reaper, and it never dies.
            parent = os.getppid()
            if parent > 1:
                config.owner_pid = parent
                config.owner_start = process_start_epoch(parent)
        return run_daemon(config)

    print(json.dumps(_config_as_dict(config), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a process
    sys.exit(main())
