"""Plan-service lifecycle owner (2026-09-18, C2 of the service-ownership work).

Why this module exists
-----------------------
C1 (:mod:`service_declaration`) gave the plan a single, authoritative
``services`` block and a way to reference those services by name. This
module is what actually *honours* that declaration: it brings each
declared service up **once** before any VP runs, and records who owns
the resulting process so the round can be unwound later (C3).

The behaviour it replaces is documented in
:mod:`service_freshness` — a regex-scraped port list, a freshness rule
that only asked "was the listener started after the last source edit?",
and a restart agent whose fast path re-exec'd whatever cmdline happened
to hold the port. On 2026-09-18 that combination blessed a ``next
start`` production build on the port a VP wanted for ``next dev``,
because "started 43s after the last edit" says nothing about *which
server flavour* is running.

Ownership rules (the part that matters)
---------------------------------------
A declared port can be held by three kinds of process, and the
response to each is deliberately different:

  1. **Ours, from this round** — recorded in the ledger (C3). Fresh →
     adopt. Stale → restart using the *declared* ``start_cmd`` (which
     is authoritative; the captured cmdline of an old process is not).
  2. **A foreign process whose cmdline matches the declaration** — a
     service the operator already had running. Adopt it if fresh;
     restart it if stale. Either way it is *not* killed unless its
     cmdline matches — see rule 3.
  3. **Our own leftover** (2026-10-25) — a process this plan's ledger
     says we started, or one that runs *our* program out of *our*
     workspace.  It is ours, so it is ours to stop: the **declared**
     port is taken back and this round's copy is started on it, so the
     effective declarations the VPs read stay valid.

     This rule exists because rule 4 alone was not enough.  The
     leftover of an earlier round of the same plan was serving another
     data plane, so every request to it answered "404 Plan not found";
     relocating around it moved *our* copy to a free port while the
     round's effective declarations kept pointing at the declared one —
     the VPs talked to the leftover.  Keeping the port stable is the
     fix; recognising the leftover is what makes it possible.
  4. **A foreign process whose cmdline does NOT match** — **relocate**.
     Nothing is killed (the same safety position
     :mod:`service_restart_agent` takes: "never kill a process you
     cannot positively identify", which is what stops this module from
     re-enacting the 2026-09-07 incident where a restart script murdered
     the backend on :8000) — but refusing outright would let someone
     else's process strand the whole round. So we start our own copy on a
     free port and point every VP at it.

     That last part is the 2026-09-18 decision: a process whose cmdline
     matches the declaration is one we started, so it may be killed;
     anything else is unknown — quite possibly started by someone else —
     and must not be touched. Rule 4 is how the round gets out of its
     way. The shape it exists for: an operator's
     ``next-server`` production build on :3000 while the plan wanted
     ``next dev`` — and the old behaviour blocked every dependent VP.

Matching is by *all* of the declaration's normalised command tokens, so
``npx next dev`` matches ``npm exec next dev`` but not ``next-server``.
See :func:`normalized_command_tokens` for why each normalisation is
there.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from credentials import _FD_ENV_PREFIX as _SECRET_FD_ENV_PREFIX
from service_declaration import SERVICE_HOST, ServiceDeclaration
from service_freshness import (
    DATA_PLANE_PROBE_PATH,
    DATA_PLANE_PROBE_TIMEOUT_SECONDS,
    PROTECTED_PORTS,
    PortProbe,
    http_probe,
    http_probe_url,
    newest_project_source_mtime,
    probe_port,
    wait_for_ready,
)

try:  # The reaper's spawn marker.  Optional: the marker is how the
    # reaper recognises a process as ours, never what makes it run, so
    # a missing module degrades to "no marker" and not to a failure.
    import orphan_reaper
except Exception:  # pragma: no cover - the marker is best-effort
    orphan_reaper = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The real ``subprocess.Popen``, captured at import time (2026-10-26,
#: repair-r1-47).
#:
#: The watcher polls a **child process**, and it needs the real thing
#: to do that.  A stand-in for one — the fake a test hands to
#: :func:`start_service` — answers ``poll()`` for ever, and a thread
#: that polls it would never end: the backend's own test gate refuses
#: a worker thread that outlives the test that started it.  Capturing
#: the class here keeps the check working even when
#: ``subprocess.Popen`` itself has been replaced, which is exactly what
#: those tests do.
_REAL_POPEN = subprocess.Popen


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Where the round's "who did we start" record lives, relative to the
#: plan directory. C3 reads it on every workflow exit and on backend
#: startup (a crash leaves it behind on purpose — that is the whole
#: point of persisting it).
LEDGER_FILENAME = "managed_services.json"

#: Where the "which round is running" record lives, relative to the
#: plan directory. See the owner-registry section below.
OWNERS_FILENAME = "service_owners.json"

#: Where the "this round is using these services right now" record
#: lives, relative to the plan directory. See the round-pin section
#: below.
ROUND_PINS_FILENAME = ".round_pins.json"

#: Where the "how did this address come about" record lives, relative to
#: the plan directory (2026-10-25, repair-r1-25).  One JSON object per
#: line, appended — the whole chain of address changes under one run is
#: one file read.
ADDRESS_EVENTS_DIRNAME = "logs"
ADDRESS_EVENTS_FILENAME = "service_address_events.jsonl"

#: The event kinds the address stream carries.  A port is *allocated*,
#: the round is *bound* to it, it was *relocated* away from another
#: one, it was *reused* from a listener that was already there, it was
#: *reaped*, and the round *dispatched* to it.
ADDRESS_EVENT_KINDS = (
    "allocated",
    "bound",
    "relocated",
    "reused",
    "reaped",
    "dispatch_bound",
)

#: How long a pin stays valid without being refreshed.  It has to
#: outlast the round that wrote it — a round's VPs may run for an hour
#: — and it is the backstop for a round that died without cleaning up
#: after itself: an unattributed pin is released when it expires.
DEFAULT_ROUND_PIN_TTL_SECONDS = 7200.0

#: Where the "what happened to the process" record lives, relative to
#: the plan directory (2026-10-26, repair-r1-47).  Same shape as the
#: address stream — one JSON object per line, appended — but a
#: different subject: the address stream says **where** the round
#: ended up, this one says **whether the process is still there**.
PROCESS_EVENTS_FILENAME = "service_process_events.jsonl"

#: The event the watcher writes when the process behind a service is
#: gone.  It is the one event the round could not produce before: on
#: 2026-10-26 a service was recorded as ready, was gone two seconds
#: later, and the round said nothing about it until a VP found the port
#: refused two minutes and forty-four seconds later.
PROCESS_EVENT_EXITED = "service_process_exited"

#: Where the "who asked this service to die" record lives, relative to
#: the plan directory (2026-10-26, repair-r1-49).  Same shape as the
#: two streams above — one JSON object per line, appended — and a
#: different question: the process stream says the process is *gone*,
#: this one says *who asked it to go*.
KILL_EVENTS_FILENAME = "service_kill_events.jsonl"

#: The event written when a signal is about to be sent to a service.
#: It is written **before** the signal goes out, so the request is on
#: the record even if the signal is the thing that takes the round's
#: log down with it.
KILL_EVENT_REQUESTED = "service_kill_requested"

#: How often the watcher asks the child process whether it is still
#: there.  Half a second: fast enough that the round learns about a
#: dead service long before a VP runs into the dead port, slow enough
#: that the watcher costs nothing while the service is healthy.
_PROCESS_WATCH_INTERVAL = 0.5

#: Shell noise that carries no identity: operators, redirections,
#: separators. Dropped before matching so ``a && b`` and ``a; b`` and
#: ``a > log 2>&1`` all normalise the same way.
_SHELL_NOISE_RE = re.compile(r"^(&&|\|\||\||;|&|>|>>|<|2>&1|2>|&>)$")

#: Launcher wrappers that are interchangeable spellings of the same
#: thing. ``npx next dev``, ``npm exec next dev`` and ``pnpm exec next
#: dev`` are the same server; the 2026-09-18 leak was literally a
#: declaration saying ``npx`` and a live process saying ``npm exec``.
_WRAPPER_TOKENS = frozenset({
    "npx", "npm", "pnpm", "yarn", "bun", "exec", "run", "dlx", "bunx",
    "nohup", "sudo", "env", "time", "exec",
})

#: Tokens that must never be dropped even though they are common, and
#: tokens that must always be dropped. ``dev`` / ``start`` / ``serve``
#: are *kept* on purpose — they are exactly the words that distinguish
#: ``next dev`` from ``next start`` — the adoption that must fail.
_KEEP_TOKENS = frozenset({"dev", "start", "serve", "server", "watch"})

#: Flags whose value is the next token, so both must be dropped
#: together (``-p 3000``, ``--port 3000``, ``--host 0.0.0.0``).
_VALUE_FLAGS = frozenset({
    "-p", "--port", "-h", "--host", "-w", "--workers",
    "-c", "--config", "--hostname", "--dir", "-H",
})

_READY_POLL_INTERVAL = 1.0

#: Markers that make a log line look like a real failure. Matching is
#: case-sensitive on purpose: a summary line such as ``0 errors`` must
#: not be mistaken for the error that killed the service.
_ERROR_MARKERS = ("Traceback", "Error", "Exception")

#: How many lines before the first error are kept as context.
_ERROR_CONTEXT_LINES = 5

#: Never read more than this many lines while looking for the first
#: error. The scan runs *after* a readiness timeout, so a service that
#: logged hundreds of thousands of lines must not turn a slow failure
#: into a slower one.
_MAX_ERROR_SCAN_LINES = 20_000

#: Upper bound on the excerpt that goes into ``ServiceRuntime.detail``.
_MAX_EXCERPT_CHARS = 1200

#: The program used to count a process' open descriptors.
_LSOF_BIN = "lsof"

#: How long the descriptor count may take before it is given up on.
_FD_COUNT_TIMEOUT_SECONDS = 5

#: The endpoint a service publishes its **data plane** on: how many
#: ``plan_routing`` rows the database behind it holds, and — in its
#: ``instance`` block — which process is answering.  The path and the
#: timeout come from :mod:`service_freshness`, so the two probes of the
#: endpoint cannot drift apart.
#:
#: A service can be perfectly alive and still be serving nothing: the
#: 2026-10-24 production round was graded on exactly that.  The port
#: answered, ``/health`` said 200 ``status: ok``, and the database
#: behind it held **zero** routing rows — so every later assertion ran
#: against an empty table and the round reported the project as broken.
#: "The service is running" and "the service is serving the round's
#: data" are two different claims, and only the second one is worth
#: anything to a VP.

#: The verdicts the data-plane probe can reach.
#:
#: ``proved``      — the instance answered 200 and reported routing rows.
#: ``empty``       — the instance answered 200 and reported **no** rows: it
#:                   has a data plane and it is empty.  This is the case
#:                   that has to stop the round.
#: ``broken``      — the instance answered, and what it said is unusable:
#:                   a 503, or a 200 that reports ``status: degraded``
#:                   (the endpoint's own way of saying "I cannot read my
#:                   database").
#: ``unreachable`` — the probe could not reach the endpoint at all.  We
#:                   learned **nothing** about the data plane, and
#:                   nothing is not evidence of anything: a project
#:                   service whose health URL is answered by a proxy,
#:                   and a service that publishes no data plane of its
#:                   own, both land here.  It is recorded, never gated.
#: ``absent``      — the instance does not speak the endpoint at all: it
#:                   has no data plane of its own, so it is never gated on
#:                   one.  Not knowing the data plane is not the same as
#:                   knowing it is wrong.
DATA_PLANE_PROVED = "proved"
DATA_PLANE_EMPTY = "empty"
DATA_PLANE_BROKEN = "broken"
DATA_PLANE_UNREACHABLE = "unreachable"
DATA_PLANE_ABSENT = "absent"


# ---------------------------------------------------------------------------
# Command normalisation + matching
# ---------------------------------------------------------------------------


def normalized_command_tokens(start_cmd: str) -> List[str]:
    """The identity-bearing tokens of a start command.

    Drops, in order: shell noise, environment assignments
    (``FOO=bar``), flags and their values (``-p 3000``), ``cd`` targets,
    ``source``/activate prefixes, and launcher wrappers (``nohup``,
    ``npx``, ``npm exec``…). Keeps everything else, **including**
    ``dev``/``start``/``serve`` — those words are precisely what tells
    ``next dev`` apart from ``next start``.

    The result is used as an "all of these must appear in the listener's
    cmdline" set, so the function must over-keep rather than over-drop:
    one dropped identity token turns a mismatched process into an
    adopted one.
    """
    if not isinstance(start_cmd, str):
        return []
    # Normalise path-ish tokens by their basename later; here we only
    # need a token stream.
    tokens = start_cmd.replace("\n", " ").split()
    kept: List[str] = []
    index = 0
    skip_next = False
    while index < len(tokens):
        raw = tokens[index]
        index += 1
        if skip_next:
            skip_next = False
            continue
        if _SHELL_NOISE_RE.match(raw):
            continue
        if raw in ("source", "."):
            # ``source venv1/bin/activate`` — the whole construct is an
            # environment setup, not an identity.
            skip_next = True
            continue
        if raw == "cd":
            skip_next = True
            continue
        if "=" in raw and not raw.startswith("-"):
            head = raw.split("=", 1)[0]
            if head and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", head):
                continue  # FOO=bar env assignment
        if raw.startswith("-"):
            if raw in _VALUE_FLAGS:
                skip_next = True
            continue
        if raw.lower() in _WRAPPER_TOKENS and raw.lower() != "run":
            # ``npm run dev`` keeps ``dev``; ``npm exec`` keeps the rest.
            continue
        # Strip surrounding quotes and take the basename of a path so
        # ``venv1/bin/python`` and ``/usr/bin/python3`` both reduce to
        # an interpreter name that substring-matches either spelling.
        token = raw.strip("'\"")
        if "/" in token and not token.startswith("-"):
            token = token.rstrip("/").rsplit("/", 1)[-1]
        if not token:
            continue
        if raw.lower() in _WRAPPER_TOKENS:
            continue
        kept.append(token)
    # ``run``/``exec`` are wrappers only when they are not the whole
    # command; dropping them unconditionally is safe because a real
    # process name ``run`` does not exist in practice.
    return [t for t in kept if t.lower() != "run" or t in _KEEP_TOKENS]


def matches_declaration(decl: ServiceDeclaration, probe: PortProbe) -> bool:
    """True when the process holding ``probe``'s port looks like the
    service ``decl`` describes.

    Requires **every** identity token of the declared ``start_cmd`` to
    appear (case-insensitively, as a substring) in at least one of the
    listener's cmdlines. Substring rather than token equality so
    ``python`` matches ``/path/to/venv1/bin/python``.

    A declaration with no identity tokens at all (a bare binary name)
    matches nothing — better to block with a reason than to adopt a
    stranger.
    """
    tokens = normalized_command_tokens(decl.start_cmd)
    if not tokens:
        return False
    if not probe.listening:
        return False
    haystack = " ".join(probe.cmdlines).lower()
    if not haystack:
        return False
    return all(token.lower() in haystack for token in tokens)


# ---------------------------------------------------------------------------
# Identity of the process that holds a declared port
# ---------------------------------------------------------------------------
#
# A declaration's ``start_cmd`` is a shell line: setup first (activate a
# virtualenv, make a directory, export the data plane the service must
# serve), the program last.  The running process only carries the
# program — the setup is gone from its command line — so requiring the
# *whole* line to match can never succeed against the process that line
# starts.  That is how our own leftover came to look like a stranger,
# and how the round ended up talking to an instance that could not see
# the plan.


#: The operators a start command's segments are separated by.
_SHELL_SEGMENT_RE = re.compile(r"&&|\|\||[;|]")


#: An environment assignment inside a start command — ``export FOO=bar``
#: or a bare ``FOO=bar`` prefix.
_START_CMD_ASSIGNMENT_RE = re.compile(
    r"(?:^|[\s;&|])(?:export\s+)?([A-Z][A-Z0-9_]*)=(\S+)"
)


def program_tokens(start_cmd: str) -> List[str]:
    """The identity tokens of the *program* a start command runs.

    The last segment of the line is the program; everything before it is
    setup the running process does not carry.  Matching a holder against
    *these* tokens is what tells "our own service, left over from an
    earlier round" apart from "a stranger that happens to hold the
    port".
    """
    if not isinstance(start_cmd, str):
        return []
    segments = [s for s in _SHELL_SEGMENT_RE.split(start_cmd) if s.strip()]
    if not segments:
        return []
    return normalized_command_tokens(segments[-1])


def matches_service_program(decl: ServiceDeclaration, probe: PortProbe) -> bool:
    """True when the holder of the port runs the program the declaration
    starts.

    Deliberately weaker than :func:`matches_declaration`: the
    declaration may spell setup steps the process does not carry, and
    the setup is exactly what made our own leftover unrecognisable.
    """
    tokens = program_tokens(decl.start_cmd)
    if not tokens:
        return False
    if not probe.listening:
        return False
    haystack = " ".join(probe.cmdlines).lower()
    if not haystack:
        return False
    return all(token.lower() in haystack for token in tokens)


def start_cmd_env(start_cmd: str) -> Dict[str, str]:
    """The environment a start command pins for the service it starts.

    The data plane is spelled in the command line: ``export
    PDT_PLANS_DIR=… && PDT_STATE_DB_PATH=… && exec python -m
    backend.server``.  Those names are what tell two instances of the
    *same* program apart — the one this round needs, and the one an
    earlier round left behind serving a different plan tree.
    """
    if not isinstance(start_cmd, str):
        return {}
    pinned: Dict[str, str] = {}
    for match in _START_CMD_ASSIGNMENT_RE.finditer(start_cmd):
        pinned[match.group(1)] = match.group(2)
    return pinned


def _process_cwd(pid: int) -> Optional[str]:
    """The working directory of *pid*, or ``None`` when unknown.

    ``None`` means "not measurable here", not "somewhere else": lsof may
    be missing, may refuse, or may not know the process any more.
    """
    try:
        completed = subprocess.run(
            [_LSOF_BIN, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, check=False,
            timeout=_FD_COUNT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a probe is best-effort
        logger.debug(
            "[service_manager] cwd probe for %s failed: %s", pid, exc,
        )
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        field = line.strip()
        if field.startswith("n") and len(field) > 1:
            return field[1:]
    return None


def _path_within(path: str, root: Path) -> bool:
    """True when *path* is *root* or lives inside it."""
    try:
        resolved = Path(path).resolve()
        base = Path(root).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(base)
    except ValueError:
        return False
    return True


def _plans_tree() -> Path:
    """The plans tree the ledgers live in.

    The env override first (``PDT_PLANS_DIR``), the repository's own
    tree otherwise — the same resolution every other module uses, so
    the ledger this module looks for is the ledger that was written.
    """
    try:
        from config_paths import resolve_plans_dir

        return Path(resolve_plans_dir())
    except Exception:  # noqa: BLE001 - the fallback is the same shape
        return Path(__file__).resolve().parent.parent / "plans"


# ---------------------------------------------------------------------------
# Data plane
# ---------------------------------------------------------------------------
#
# "Something is listening on the port" and "the thing listening is
# serving the round's data" are two different questions, and the round
# used to ask only the first one.  The service manager therefore asks
# the second one too, over the same road a VP's request travels: the
# loopback address, over TCP, carrying the request guard's header.


@dataclass
class DataPlaneEvidence:
    """What the instance answering on a port says about its data.

    ``/health/data`` answers four things: the state of the data plane
    (``status``), how many ``plan_routing`` rows it holds
    (``plan_routing_rows``), and — in its ``instance`` block — which
    process is answering (``boot_id``) and which state database it is
    serving (``state_db_path``).

    The **verdict** is what the round acts on:

      * ``proved`` — the instance is serving a database with routing
        rows.  This is the only verdict that says "yes, this instance
        is serving data".
      * ``empty`` — the instance has a data plane and it is empty.
      * ``broken`` — the instance answered, and what it said is unusable.
      * ``unreachable`` — the probe could not reach the endpoint, so
        nothing is known about the data plane.
      * ``absent`` — the instance does not speak the endpoint at all.
        A project service with no database behind it lands here, and it
        is therefore never gated on a data plane.
    """

    #: The service's own ``status`` field (``ok`` / ``degraded``).
    status: str = ""
    #: ``plan_routing`` rows the instance reported.  ``None`` when it
    #: reported no count at all.
    plan_routing_rows: Optional[int] = None
    #: The boot id of the answering process, when it published one.
    boot_id: str = ""
    #: The state database the instance says it is serving, when it
    #: published one.  This is the **instance's own** answer: the
    #: round has to judge an instance against the database the
    #: instance names, not against the one the prober happens to
    #: have open.  Empty when the instance named none.
    state_db_path: str = ""
    #: One of the ``DATA_PLANE_*`` verdicts.
    verdict: str = DATA_PLANE_ABSENT
    #: Why the verdict came out the way it did.
    error: str = ""

    @property
    def proved(self) -> bool:
        """Did the instance prove it is serving data?"""
        return self.verdict == DATA_PLANE_PROVED

    @property
    def blocked(self) -> bool:
        """Does this evidence refuse the instance as an address?

        ``absent`` and ``unreachable`` never refuse: a service with no
        data plane of its own is not a service serving the wrong data,
        and a probe that learned nothing is not evidence.
        """
        return self.verdict in (DATA_PLANE_EMPTY, DATA_PLANE_BROKEN)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "plan_routing_rows": self.plan_routing_rows,
            "boot_id": self.boot_id,
            "state_db_path": self.state_db_path,
            "verdict": self.verdict,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Runtime record
# ---------------------------------------------------------------------------


@dataclass
class ProcessExit:
    """How the process behind a service ended (2026-10-26, repair-r1-47).

    The facts a reader needs to explain a death: **which** process,
    on **which** port, it ended with **which** outcome, it lived for
    **how long**, and it belonged to **which** round.

    ``returncode`` is what the operating system reports: ``0`` for a
    clean exit, a positive number for an exit code, and a **negative**
    number when the process was killed by a signal (``-15`` is
    SIGTERM).  ``exit_signal`` names the signal in the positive form,
    so a reader does not have to know the convention to ask "was it
    killed?".

    ``started_at`` / ``exited_at`` are epoch seconds — the same clock
    as ``ServiceRuntime.start_epoch``, so the two can be compared
    without a conversion.
    """

    pid: Optional[int] = None
    port: Optional[int] = None
    returncode: Optional[int] = None
    exit_signal: Optional[int] = None
    started_at: float = 0.0
    exited_at: float = 0.0
    uptime_sec: float = 0.0
    #: The round the service belonged to — the same token the address
    #: events carry, so the two streams can be correlated.
    round_token: str = ""
    #: One line saying what the exit was, in words a reader can act on.
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.port,
            "returncode": self.returncode,
            "exit_signal": self.exit_signal,
            "started_at": self.started_at,
            "exited_at": self.exited_at,
            "uptime_sec": self.uptime_sec,
            "round_token": self.round_token,
            "reason": self.reason,
        }


@dataclass
class ServiceRuntime:
    """What the round learned about one declared service."""

    name: str
    port: int
    #: The port the *plan* declared. Differs from ``port`` when the round
    #: had to relocate (see :func:`ensure_services` rule 4) — the ledger
    #: records both so an operator can tell "it moved" from "it failed".
    declared_port: Optional[int] = None
    pid: Optional[int] = None
    pgid: Optional[int] = None
    start_epoch: Optional[float] = None
    start_cmd: str = ""
    cwd: str = "."
    log_path: str = ""
    #: "spawned" (this round started it) | "adopted" (a matching
    #: process was already live and fresh) | "ready_foreign" (a
    #: matching foreign process was restarted) | "failed"
    origin: str = "failed"
    ready: bool = False
    reap_on_exit: bool = True
    detail: str = ""
    #: Which *round* started this service (2026-10-24). The ledger used
    #: to answer "which processes did the backend start for this plan",
    #: which is not the same question: two rounds of one plan share one
    #: plan directory and therefore one ledger, so the older round's
    #: exit-time reap read the newer round's entries out of it and
    #: stopped them. Empty means "nobody claims it" — a ledger written
    #: before this field existed, which is reapable by anyone.
    owner_token: str = ""
    #: True when this round took the **declared** port back from a
    #: leftover of its own (2026-10-25).  The reclaim is what keeps the
    #: effective port stable, so it is recorded rather than left in the
    #: prose of ``detail``: a reader has to be able to tell "we started
    #: this on the declared port" from "we relocated around somebody
    #: else".
    reclaimed: bool = False
    #: Why the reclaim happened — which leftover, which plan, and which
    #: data plane it was serving when it did not serve ours.
    reclaim_reason: str = ""
    #: Which address events this runtime has already written (2026-10-25,
    #: repair-r1-25).  Deliberately **not** part of ``to_dict()``: the
    #: ledger records the address, the event stream records how it came
    #: about, and the two are separate files on purpose.
    address_events: Set[str] = field(
        default_factory=set, repr=False, compare=False,
    )
    #: True when this runtime names **no** address a request may be sent
    #: to (2026-10-25, repair-r1-29).
    #:
    #: ``ready=False`` says "the service did not come up"; ``unusable``
    #: says something stronger: the port this record names is not ours
    #: at all.  A relocation that gave up is the case — its runtime
    #: still carries the *declared* port, and the declared port is held
    #: by somebody else.  A reader that only asks "is it ready?" would
    #: otherwise have an address in its hand.
    unusable: bool = False
    #: Why the runtime is unusable — one line, in the runtime's own
    #: record, so an operator can read the cause without re-running the
    #: round.
    failure_reason: str = ""
    #: Every port this runtime tried (2026-10-25, repair-r1-29).  A
    #: relocation that failed tries three of them; the address event
    #: stream records every one, so "where did this port come from" is
    #: answerable even for the ports that did not work.
    candidate_ports: List[int] = field(
        default_factory=list, repr=False, compare=False,
    )
    #: What the instance on this port said about its data (2026-10-25).
    #: Recorded for **every** runtime, so the round's log can answer
    #: "it came up — but did it come up serving data?" without anybody
    #: having to guess.  ``None`` when the runtime was never probed
    #: (a caller that built the runtime by hand).
    data_plane: Optional[DataPlaneEvidence] = None
    #: How the process behind this service ended (2026-10-26,
    #: repair-r1-47).  ``None`` while the process is still running.
    #:
    #: It is filled in by the watcher that :func:`start_service` starts
    #: for every service it spawns — the thread that notices the child
    #: is gone.  Before this existed, a service that was recorded as
    #: ready and died two seconds later left the round with a runtime
    #: that still said "ready", still carried the port, and still
    #: answered every question as if the address were good.
    exit: Optional[ProcessExit] = None
    #: True when **this round** stopped the service on purpose
    #: (2026-10-26, repair-r1-47).  A reap is not a crash, and the
    #: watcher has to be able to tell the two apart — otherwise every
    #: service the round cleans up after itself would be reported as a
    #: service that died.
    stopped: bool = False

    @property
    def spawned_by_ac(self) -> bool:
        return self.origin == "spawned"

    @property
    def dead(self) -> bool:
        """Is the process behind this service gone?

        The answer is not a guess: it is set by the watcher that saw
        the child process exit.  A runtime whose process is gone is not
        an address, whatever its ``ready`` flag still says.
        """
        return self.exit is not None

    @property
    def alive(self) -> bool:
        """Is the process behind this service still running?

        The mirror of :attr:`dead`, and the question a reader asks
        first: "is the thing I am pointing at still there?".  It is
        answered by the watcher that saw the child process exit, so it
        is not a guess — and it is ``False`` for a runtime whose
        process is gone, whatever its ``ready`` flag still says.
        """
        return not self.dead

    @property
    def usable(self) -> bool:
        """May a request be sent to this runtime's port?

        Only a runtime that says the service came up, was not a
        failure, was not explicitly marked unusable, and carries a port
        may be used as an address.  Everything else — including a
        runtime that is merely "not ready yet" — is not an address.

        A runtime whose **process is gone** is not an address either
        (2026-10-26, repair-r1-47): the port it names is either
        refused or held by somebody else, and a round that keeps
        handing it out sends its VPs into a dead end.
        """
        if self.unusable:
            return False
        if self.dead:
            return False
        if not self.ready:
            return False
        if self.origin == "failed":
            return False
        return bool(self.port)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "declared_port": (
                self.declared_port if self.declared_port is not None else self.port
            ),
            "pid": self.pid,
            "pgid": self.pgid,
            "start_epoch": self.start_epoch,
            "start_cmd": self.start_cmd,
            "cwd": self.cwd,
            "log_path": self.log_path,
            "origin": self.origin,
            "ready": self.ready,
            "reap_on_exit": self.reap_on_exit,
            "detail": self.detail,
            "owner_token": self.owner_token,
            "reclaimed": self.reclaimed,
            "reclaim_reason": self.reclaim_reason,
            "unusable": self.unusable,
            "failure_reason": self.failure_reason,
            "data_plane": (
                self.data_plane.to_dict() if self.data_plane is not None else None
            ),
            # Whether the process behind this service is still there,
            # and — when it is not — how it ended (2026-10-26,
            # repair-r1-47).  The ledger is what a later round reads to
            # decide whether it may reuse this address, so the death
            # has to be in it.
            "dead": self.dead,
            "exit": self.exit.to_dict() if self.exit is not None else None,
        }


@dataclass
class ServiceRuntimeMap:
    """Result of :func:`ensure_services`."""

    runtimes: Dict[str, ServiceRuntime] = field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def ready_names(self) -> List[str]:
        return sorted(n for n, r in self.runtimes.items() if r.ready)

    @property
    def unready_ports(self) -> List[int]:
        return sorted(r.port for r in self.runtimes.values() if not r.ready)

    @property
    def blocked_ports(self) -> List[int]:
        """Ports whose VPs must be BLOCKED — every service that is not
        ready, plus every runtime that failed outright."""
        return self.unready_ports

    def enabled(self) -> bool:
        return bool(self.runtimes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "services": {n: r.to_dict() for n, r in self.runtimes.items()},
            "ready": self.ready_names,
            "unready_ports": self.unready_ports,
            "duration_seconds": round(self.duration_seconds, 2),
            # The data plane of every service, in one place.  This is
            # what the round's ``services_ensured`` event carries: the
            # readiness verdict on its own says "it came up", and this
            # says "it came up **serving data**" — which is the fact
            # the 2026-10-24 round was missing.
            "data_plane": {
                n: (r.data_plane.to_dict() if r.data_plane is not None else None)
                for n, r in sorted(self.runtimes.items())
            },
        }


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _process_group_of(pid: int) -> Optional[int]:
    """*pid*'s process group, or ``None`` if it cannot be determined.

    ``os.getpgid`` rather than a ``ps`` subprocess. The probe is asked for
    immediately after ``Popen`` returns, and that is a race: the child
    exists as a kernel task but ``ps`` can run before the scheduler has
    made it visible in the process table, so the probe answers with empty
    output and the caller records ``pgid=None``. A ``None`` here is not a
    cosmetic gap — ``stop_service`` reads it to decide whether it may
    signal the group at all, and without it the stop degrades to
    single-pid kills that leave the group behind.

    ``getpgid`` is the same lookup the kernel would do for ``kill``, with
    no subprocess to lose. It is also cheaper, which matters because this
    is on the spawn path. ``pgid <= 1`` is refused for the reason
    ``utils.process._signallable_pgid`` documents: 1 is ``kill(-1)``, a
    broadcast, and 0 is the caller's own group.
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        # The process exited between the spawn and this call, or belongs
        # to another user. Either way there is no group of ours to record.
        return None
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return None
    return pgid


def _process_groups_table() -> Dict[int, Tuple[int, int]]:
    """``pid -> (ppid, pgid)`` for every process the platform reports.

    One ``ps`` call for the whole table, not one call per ancestor.

    ``ps`` rather than ``os.getpgid`` here, on purpose.  ``os.getpgid``
    is the kernel's own answer and it is the right one on the spawn
    path (see :func:`_process_group_of`), but the ancestors of this
    process are not being spawned by it: they are long-lived, and the
    kernel answers about them one call at a time.  The process table
    answers about all of them at once, and — this is the reason it is
    read here and not one ``getpgid`` at a time — it answers about
    *the caller's own* tree.  A caller that replaced ``os.getpgid`` (to
    describe a child process of its own) has that replacement applied
    to its ancestors as well, so the answer it reads back describes the
    process it asked about rather than the ancestor it needed, and the
    group it then refuses is a group nobody is in.

    A platform without ``ps`` contributes nothing, and the caller then
    keeps the behaviour it had.
    """
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    table: Dict[int, Tuple[int, int]] = {}
    for line in (completed.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            pgid = int(fields[2])
        except ValueError:
            continue
        table[pid] = (ppid, pgid)
    return table


def _ancestor_process_groups(max_depth: int = 8) -> Set[int]:
    """The process groups that hold one of this process's ancestors.

    A stop must never group-signal a group that holds an ancestor of
    the caller.  The shape that matters is the one the CI hit: the
    caller lives in a group of its own (a service started with
    ``start_new_session=True``), and the group it is asked to stop holds
    the shell that is waiting for the test session.  Signalling that
    group kills the shell, the shell forwards the signal to the child it
    waits for, and the caller dies of a signal it never sent.

    The walk is bounded and every step of it is optional: a platform
    that cannot answer contributes nothing, and the caller then keeps the
    behaviour it had.
    """
    table = _process_groups_table()
    if not table:
        return set()
    groups: Set[int] = set()
    try:
        pid = int(os.getpid())
    except OSError:
        return groups
    seen: Set[int] = set()
    for _ in range(max_depth):
        entry = table.get(pid)
        if entry is None:
            break
        ppid, pgid = entry
        if pgid > 1:
            groups.add(pgid)
        if ppid <= 1 or ppid in seen:
            break
        seen.add(ppid)
        pid = ppid
    return groups


def _protected_groups() -> Set[int]:
    """The groups a group-signal must never touch.

    Two of them, and they are the same kind of danger:

    * the caller's own group — that is the caller and everything it
      owns;
    * the group of any of the caller's ancestors — the shell that owns
      the test session, the executor that owns the caller.  Signalling
      that group takes the ancestor with it, and an ancestor that
      forwards a signal to its child takes the caller down too.

    The set is computed per call rather than cached: the tree around a
    stop is short-lived, and a cached answer would be an answer about a
    tree that no longer exists.
    """
    groups: Set[int] = set()
    try:
        own = int(os.getpgrp())
    except OSError:
        own = None
    if own is not None and own > 1:
        groups.add(own)
    groups |= _ancestor_process_groups()
    return groups


#: The program a declared service is started through.
#:
#: It does three things, in this order:
#:
#: 1. Drops the environment variables that name a descriptor. A
#:    descriptor number only means something in the process that
#:    published it, and a child inherits the *variable* without
#:    inheriting the pipe behind it -- so the number names something
#:    else in the child. ``credentials.read_secret_fd`` closes whatever
#:    number it is given, and in the child that number is the event
#:    loop's own selector: the service was dying of ``Bad file
#:    descriptor`` before it was ready. The launcher's own handoff is
#:    untouched by this -- it execs the backend directly, where the
#:    numbers are valid, and that path does not go through here.
#: 2. Calls ``setsid()``, so the service runs in a session of its own
#:    and outlives the backend that started it.
#: 3. Execs the real command, unchanged.
_DETACHED_WRAPPER_SOURCE = (
    "import os,sys\n"
    "for _name in [k for k in os.environ if k.startswith("
    f"{_SECRET_FD_ENV_PREFIX!r})]:\n"
    "    del os.environ[_name]\n"
    "os.setsid()\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)


#: How long the spawn path waits for the child to report that it has
#: made itself a process-group leader.
_DETACHED_PGID_TIMEOUT_SECONDS = 5.0


def _detached_argv(start_cmd: str) -> List[str]:
    """The argv a declared service is started with.

    The command is handed to the wrapper as it stands -- one argv
    element, never re-parsed and never re-quoted -- and the wrapper
    execs it through the same shell the service has always been started
    with, so the command sees the shell it expects.
    """
    return [
        sys.executable or "/usr/bin/python3",
        "-c",
        _DETACHED_WRAPPER_SOURCE,
        "/bin/bash",
        "-lc",
        start_cmd,
    ]


def _detached_pgid_of(pid: int) -> Optional[int]:
    """*pid*'s process group, once the child has made itself a leader.

    The child calls ``setsid()`` itself -- see :func:`_detached_argv` --
    so for a moment after ``Popen`` returns it is still a member of
    *our* group. Recording that group would hand :func:`stop_service` a
    pgid that names the caller's own processes, and signalling it would
    take down the backend that asked for the service. So wait for the
    separation, and record nothing rather than record the caller's
    group.
    """
    deadline = time.monotonic() + _DETACHED_PGID_TIMEOUT_SECONDS
    while True:
        pgid = _process_group_of(pid)
        if pgid is None:
            # The child is gone; there is no group left to record.
            return None
        if pgid == pid:
            # It leads its own group now, so the separation has happened.
            return pgid
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _process_start_epoch(pid: int) -> Optional[float]:
    raw = _run(["ps", "-p", str(pid), "-o", "lstart="])
    if not raw:
        return None
    from service_freshness import _parse_ps_lstart  # local: private helper

    return _parse_ps_lstart(raw)


def _run(argv: Sequence[str], timeout: int = 10) -> Optional[str]:
    """Best-effort subprocess helper; never raises."""
    try:
        out = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001 - probes are best-effort
        logger.debug("[service_manager] %s failed: %s", argv, exc)
        return None


# ---------------------------------------------------------------------------
# Kill events — who asked this service to die?
# ---------------------------------------------------------------------------
#
# The 2026-10-26 round brought its api service up at 02:21:49 and
# recorded it as ready.  Two seconds later the service was gone, and the
# only trace of the death was on the **service's** side::
#
#     received SIGTERM pid=43904 verifying_plans=[]
#
# Nothing said *who* sent that signal, and nothing said *why*.  The
# same shape had happened twice before (2026-10-24 at 21:44 and 21:54) —
# three rounds in a row, each losing its service two or three seconds
# after it came up, each finding out from a VP that met a refused
# connection minutes later.
#
# The gap was structural.  The signals were sent from scattered places:
# the round's own reap, the startup sweep, the shutdown sweep, the
# execution-subprocess cleanup.  Each of them was right to send a
# SIGTERM; none of them left a record.  "The service died" had no
# "somebody asked it to" beside it, so the round could not tell a sweep
# from a shutdown from a rival round's reap — it could only see the
# corpse.
#
# So the signals converge on one function — :func:`kill_service` — and
# that function writes the request down **before** the signal goes out.
# The request carries the pid, the port, the caller, the reason, the
# round the service belonged to, and a hint at the call site.  The next
# death of this shape is read out of the round's own event stream
# instead of being reconstructed from timestamps.
#
# Two rules make the stream worth reading:
#
#   * **The request is written before the signal is sent.**  A signal
#     can take the round's log with it; a request written afterwards is
#     a request nobody can trust to describe what happened.
#   * **A broken log never costs a round.**  A failed write is one line
#     on stderr, and the signal goes out anyway: refusing to kill
#     because the log could not be written would trade a recorded death
#     for an unrecorded one.


def kill_events_path(plan_dir: Path) -> Path:
    """Where a plan's kill event stream lives."""
    return Path(plan_dir) / ADDRESS_EVENTS_DIRNAME / KILL_EVENTS_FILENAME


def record_kill_event(plan_dir: Path, payload: Dict[str, Any]) -> bool:
    """Append one kill request to the plan's stream.

    Never raises.  A failed write costs one line on stderr and returns
    ``False`` — a round must not die because its log could not be
    written, and the signal that follows must not be held back by it.
    """
    path = kill_events_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        print(
            "[service_manager] could not write kill event to "
            f"{path}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def read_kill_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """Read a plan's kill event stream.

    A line that is not a JSON object is skipped rather than fatal: one
    torn line must not hide the requests around it.
    """
    path = kill_events_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _kill_stack_hint(depth: int = 5) -> str:
    """A short hint at where a kill was asked for.

    The file **name** only, never the full path: the plan's own log is
    read by people who have no business seeing the operator's home
    directory, and the file name plus the line number is what actually
    identifies the call site.
    """
    frames = traceback.extract_stack()[:-1]
    parts = [
        f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
        for frame in frames[-depth:]
    ]
    return " <- ".join(parts)


#: "The process group has not been decided yet" — distinct from "the
#: process group is ``None``", which means "there is no group, signal
#: the pids one by one".  The distinction matters: a caller that has
#: already resolved the group (and already found it to be *our* own
#: group, which must never be signalled) has to be able to say so.
_UNSET_PGID = object()


def kill_service(
    runtime: ServiceRuntime,
    caller: str,
    reason: str,
    round_token: str = "",
    *,
    plan_dir: Optional[Path] = None,
    sig: int = signal.SIGTERM,
    pids: Optional[Sequence[int]] = None,
    pgid: Any = _UNSET_PGID,
    pids_first: bool = False,
    fallback_to_pids: bool = True,
) -> Dict[str, Any]:
    """Signal a service — and write down who asked for it.

    This is the **only** place in the backend that signals a service.
    Every SIGTERM and every SIGKILL aimed at a declared service goes
    through here, so the round's own event stream always carries the
    request that preceded the death.

    The arguments:

    ``runtime``
        The service being signalled.  Its pid and port are what the
        event records.
    ``caller``
        Who is asking — ``"service_manager.reap_services"``,
        ``"server.startup_sweep"``, …  It is a name, not a number,
        because the number is what the reader wants and the name is
        what the reader recognises.
    ``reason``
        Why, in the words of whoever is asking.  This is the field
        that separates "a sweep found an orphan" from "a rival round
        reaped my service".
    ``round_token``
        The round the service belonged to, so the request can be
        correlated with the round's other events.
    ``pids``
        The individual pids to signal.  Defaults to the runtime's own
        pid.
    ``pgid``
        The process group to signal.  By default it is the runtime's
        group, or the group of the first pid.
    ``pids_first``
        Signal the individual pids **before** the group.  The SIGTERM
        path wants the other order — the group covers the whole
        ``npx next dev`` chain in one signal — but the SIGKILL
        escalation wants the recorded pid first: it is the process the
        round actually knows about, and the group is what catches the
        descendants.
    ``fallback_to_pids``
        Whether to signal the individual pids when the group could not
        be signalled.  ``False`` for a caller that has a group it
        trusts and must not fall back to a pid that might belong to
        somebody else.

    The order of operations is the point of the function: **the request
    is written down first, and only then is the signal sent.**  A
    signal can take the round's log with it.

    Never raises.
    """
    if plan_dir is None:
        plan_dir = _ambient_plan_dir()
    if not round_token:
        round_token = _ambient_round_token()

    targets: List[int] = []
    for pid in (pids if pids is not None else ([runtime.pid] if runtime.pid else [])):
        try:
            value = int(pid)
        except (TypeError, ValueError):
            continue
        if value > 1 and value not in targets:
            targets.append(value)

    if pgid is _UNSET_PGID:
        group = runtime.pgid
        if group is None and targets:
            group = _process_group_of(targets[0])
    else:
        group = pgid
    try:
        group = int(group) if group is not None else None
    except (TypeError, ValueError):
        group = None
    # Never a group that holds the caller or one of its ancestors:
    # signalling it takes the caller down — or, under pytest, the whole
    # session — and the caller is not the one that sent the signal.  A
    # refused group degrades to the individual pids, which are the
    # processes this call is about, when the caller allows that
    # (``fallback_to_pids``); a caller that does not allow it gets no
    # signal at all, which is its own choice and not this one's.
    refused_pgid: Optional[int] = None
    if group is not None and group in _protected_groups():
        refused_pgid = group
        group = None
    if group is not None and group <= 1:
        # 1 is ``kill(-1)``: every process we may signal.  See
        # ``utils.process._signallable_pgid``.
        group = None

    request: Dict[str, Any] = {
        "event": KILL_EVENT_REQUESTED,
        "ts": datetime.now().isoformat(),
        "pid": runtime.pid,
        "port": runtime.port,
        "pgid": group,
        "refused_pgid": refused_pgid,
        "signal": int(sig),
        "caller": str(caller or ""),
        "reason": str(reason or ""),
        "round_token": str(round_token or ""),
        "stack_hint": _kill_stack_hint(),
    }

    # The request goes down **before** the signal goes out.
    recorded = False
    if plan_dir is not None:
        recorded = record_kill_event(Path(plan_dir), request)

    # This stop is **ours** (2026-10-26, repair-r1-47).  The watcher on
    # this service is going to see the process exit, and the exit it
    # writes down has to say "the round stopped it" rather than "the
    # service died" — a stop is not a crash, and a stream that cannot
    # tell the two apart teaches the round to ignore it.
    runtime.stopped = True

    signalled: List[str] = []
    signalled_pids: List[int] = []
    signalled_group = False

    def _signal_group() -> None:
        nonlocal signalled_group
        if not group or group <= 1:
            return
        try:
            os.killpg(group, sig)
            signalled.append(f"pgid {group} {_signal_name(sig)}")
            signalled_group = True
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug(
                "[service_manager] killpg %s failed: %s", group, exc,
            )

    def _signal_pids() -> None:
        for pid in targets:
            try:
                os.kill(pid, sig)
                signalled.append(f"pid {pid} {_signal_name(sig)}")
                signalled_pids.append(pid)
            except (ProcessLookupError, PermissionError, OSError):
                continue

    if pids_first:
        _signal_pids()
        _signal_group()
    else:
        _signal_group()
        if not signalled_group and fallback_to_pids:
            _signal_pids()

    request["signalled"] = list(signalled)
    request["recorded"] = recorded
    return {
        "event": KILL_EVENT_REQUESTED,
        "pid": runtime.pid,
        "port": runtime.port,
        "pgid": group,
        "refused_pgid": refused_pgid,
        "signal": int(sig),
        "caller": str(caller or ""),
        "reason": str(reason or ""),
        "round_token": str(round_token or ""),
        "signalled": signalled,
        "pids": signalled_pids,
        "group_signalled": signalled_group,
        "recorded": recorded,
    }


def _signal_name(sig: int) -> str:
    """``SIGTERM`` for 15, ``SIGKILL`` for 9 — and something sane otherwise."""
    try:
        return signal.Signals(int(sig)).name
    except (ValueError, TypeError):
        return f"SIG{sig}"


def _ambient_plan_dir() -> Optional[Path]:
    """The plan directory of the round that is running right now, if any."""
    context = _ADDRESS_EVENT_CONTEXT.get() or {}
    raw = context.get("plan_dir")
    return Path(raw) if raw else None


def _ambient_round_token() -> str:
    """The round token of the round that is running right now, if any."""
    context = _ADDRESS_EVENT_CONTEXT.get() or {}
    return str(context.get("round_token") or "")


def stop_service(
    runtime: ServiceRuntime,
    decl: Optional[ServiceDeclaration] = None,
    *,
    grace_seconds: float = 10.0,
    caller: str = "service_manager.stop_service",
    reason: str = "",
    round_token: str = "",
    plan_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Stop a service this module (or a previous round) started.

    Signals the **process group** when one is known — an ``npx next
    dev`` chain is three processes (npm exec → node → next-server) and
    killing only the recorded PID leaves the wrapper alive to re-spawn
    or hold the port. Then waits for the port to be released and
    reports it.

    ``caller`` and ``reason`` say **who** is stopping the service and
    **why**; they are written to the plan's kill event stream before
    the signal goes out, so a service that dies is never a mystery
    (2026-10-26, repair-r1-49).  Every caller in this module names
    itself: the reaper says it was the reaper, the startup sweep says
    it was the startup sweep.

    Refuses to touch protected ports. Never raises.
    """
    result: Dict[str, Any] = {
        "name": runtime.name, "port": runtime.port, "killed": [],
        "signalled": [], "port_free": False, "detail": "",
    }
    # Diagnostic (2026-10-24): every stop goes through here, so this line
    # is the one place that names *who* asked for it. A verification round
    # lost its declared service 15s after it became ready and nobody could
    # say which path signalled it; the caller name is what answers that.
    try:
        _caller = sys._getframe(1).f_code.co_name
    except Exception:  # pragma: no cover - diagnostics must not raise
        _caller = "?"
    logger.info(
        "[service_manager] stop_service port=%s name=%s pid=%s pgid=%s caller=%s",
        runtime.port, runtime.name, runtime.pid, runtime.pgid, _caller,
    )
    if runtime.port in PROTECTED_PORTS:
        result["detail"] = (
            f"refusing to stop port {runtime.port}: backend-runtime port"
        )
        return result

    targets: List[int] = []
    if runtime.pid:
        targets.append(runtime.pid)
    # Re-probe: the recorded PID may be gone while a sibling still
    # holds the port (that is exactly the wrapper-survives case).
    if decl is not None:
        probe = probe_port(runtime.port)
        for pid, cmdline in zip(probe.pids, probe.cmdlines or []):
            if pid not in targets and cmdline and matches_declaration(
                decl, PortProbe(
                    port=runtime.port, listening=True, pids=[pid],
                    cmdlines=[cmdline],
                ),
            ):
                targets.append(pid)

    if not targets:
        result["port_free"] = not probe_port(runtime.port).listening
        result["detail"] = "no process to stop"
        return result

    pgid = runtime.pgid or _process_group_of(targets[0])
    # Never our own group: that is the caller, and everything it owns.
    # Signalling it would take down the backend — or, under pytest, the
    # whole session, which is a failure with no trace of its cause. A
    # child that has not made itself a group leader yet sits in it.
    own_pgid = os.getpgrp()
    if pgid is not None and pgid == own_pgid:
        pgid = None
    # This stop is **ours** (2026-10-26, repair-r1-47).  The watcher on
    # this service is going to see the process exit, and the exit it
    # writes down has to say "the round stopped it" rather than "the
    # service died" — a reap is not a crash, and a stream that cannot
    # tell the two apart teaches the round to ignore it.
    runtime.stopped = True
    # The signal goes out through the one function that owns it, so the
    # request is on the record before the process hears about it
    # (2026-10-26, repair-r1-49).
    outcome = kill_service(
        runtime,
        caller,
        reason or f"stop service {runtime.name!r} on port {runtime.port}",
        round_token,
        plan_dir=plan_dir,
        sig=signal.SIGTERM,
        pids=targets,
        pgid=pgid,
    )
    result["signalled"].extend(outcome["signalled"])

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not probe_port(runtime.port).listening:
            result["port_free"] = True
            result["detail"] = "stopped after SIGTERM"
            return result
        time.sleep(0.5)

    # Escalate.  The recorded pid goes first — it is the process the
    # round actually knows about — and the group follows, which is what
    # catches the descendants of an ``npx next dev`` chain.
    escalation = kill_service(
        runtime,
        caller,
        f"{reason or 'stop service'} — SIGKILL escalation",
        round_token,
        plan_dir=plan_dir,
        sig=signal.SIGKILL,
        pids=targets,
        pgid=pgid,
        pids_first=True,
    )
    result["killed"].extend(escalation["pids"])

    # Poll, do not sleep. SIGKILL is asynchronous — `killpg` returning
    # means the signal was delivered, not that the kernel has finished
    # tearing the process down. This branch used to sleep a flat second
    # and then look once, so a runner that was busy when the signal
    # landed reported the port still held after a kill that had in fact
    # worked; on a loaded machine that was a few percent of runs, and
    # always on this path, because it is the only one that does not
    # already wait for the port. The SIGTERM branch above polls, which is
    # why the escalation is the branch that fails.
    deadline = time.monotonic() + max(5.0, grace_seconds)
    while True:
        result["port_free"] = not probe_port(runtime.port).listening
        if result["port_free"] or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    result["detail"] = (
        "stopped after SIGKILL" if result["port_free"]
        else f"port {runtime.port} still held after SIGKILL"
    )
    return result


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------


#: How many different ports to try before giving up on a relocation.
#: A readiness failure after a relocation can mean two things, and both are
#: worth one more try: the port we picked was taken between the probe and
#: the service's bind (a TOCTOU race), or the service ignored our rewrite
#: and tried to bind the declared port (which is occupied).
_RELOCATION_ATTEMPTS = 3


def allocate_free_port() -> Optional[int]:
    """An ephemeral port that nothing currently holds on loopback.

    Binding port 0 and reading back what the kernel chose is the only
    race-free way to ask. The port is released immediately, so a
    relocation still has a TOCTOU window — which is why
    :func:`ensure_services` retries with a fresh port when readiness
    fails.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
    except OSError as exc:
        logger.warning("[service_manager] could not allocate a port: %s", exc)
        return None
    if port in PROTECTED_PORTS:
        return None
    return port


def rewrite_port(text: Any, old_port: int, new_port: int) -> Any:
    """Replace every standalone ``old_port`` literal in ``text``.

    Whole-number matching only (``(?<!\\d)3000(?!\\d)``), so ``8080`` is
    never rewritten inside ``180801`` or ``80801``. Used on a declared
    ``start_cmd`` / ``health_url`` when relocating a service.
    """
    if not isinstance(text, str) or not text:
        return text
    return re.sub(
        rf"(?<!\d){int(old_port)}(?!\d)", str(int(new_port)), text,
    )


def render_declaration(
    decl: ServiceDeclaration, port: int,
) -> ServiceDeclaration:
    """The declaration as it will actually be run, on ``port``.

    Only the port-bearing fields move: the command, the health URL, and
    the log filename (so two relocations of the same service do not
    interleave in one log).
    """
    if port == decl.port:
        return decl
    return ServiceDeclaration(
        name=decl.name,
        port=port,
        start_cmd=rewrite_port(decl.start_cmd, decl.port, port),
        cwd=decl.cwd,
        health_url=rewrite_port(decl.health_url, decl.port, port),
        ready_timeout_seconds=decl.ready_timeout_seconds,
        log_path=decl.log_path,
        reap_on_exit=decl.reap_on_exit,
    )


def service_env(decl: ServiceDeclaration) -> Dict[str, str]:
    """Environment variables a VP can read for this service.

    ``PDT_SVC_<NAME>_URL`` / ``_PORT`` / ``_HOST``. The name is
    upper-cased with non-alphanumerics folded to ``_``, so
    ``api-server`` becomes ``PDT_SVC_API_SERVER_PORT``.

    Injected into the service process *and* advertised in the VP prompt:
    a VP that reads them can never be pointing at a stale port, which is
    the whole reason relocation is safe to do at all.
    """
    key = re.sub(r"[^A-Za-z0-9]+", "_", decl.name).strip("_").upper()
    return {
        f"PDT_SVC_{key}_URL": decl.url,
        f"PDT_SVC_{key}_PORT": str(decl.port),
        f"PDT_SVC_{key}_HOST": SERVICE_HOST,
    }


def _log_path_for(decl: ServiceDeclaration, project_dir: Path) -> Path:
    if decl.log_path:
        return Path(project_dir) / decl.log_path
    return Path(project_dir) / "tmp" / f"pdt_service_{decl.name}.log"


def data_plane_evidence(
    port: int, *, timeout: float = DATA_PLANE_PROBE_TIMEOUT_SECONDS,
) -> DataPlaneEvidence:
    """Ask the instance on *port* which data plane it is serving.

    The request travels the same road a VP's request travels: the
    loopback address, over TCP, carrying the request guard's header.
    Never raises — a probe that fails is *evidence*, not an exception.

    The answer is read as one of five verdicts (see
    :class:`DataPlaneEvidence`): the instance either proves it is
    serving routing rows, proves it is serving **none**, says its own
    data plane is broken, could not be reached at all, or does not
    publish a data plane in the first place.

    This is the **verdict** probe — "is this instance serving usable
    data?".  :func:`service_freshness.probe_data_plane` is the
    **identity** probe — "which instance is that, and is it the one
    the round expects?".  They ask the same endpoint different
    questions, and the round needs both: the verdict decides whether
    the instance may be used as an address at all, the fingerprint says
    whether it is the address the round meant.

    The request travels over a **keep-alive** connection (see
    :func:`service_freshness.http_probe`): a probe must not leave the
    service's own port in ``TIME_WAIT``.  A server that closes first
    leaves that socket on **its** listening port, and the port then
    stops being bindable — the service is stopped, nothing is
    listening on it any more, and a plain ``bind()`` on it still fails
    with ``EADDRINUSE``.
    """
    evidence = DataPlaneEvidence()
    try:
        from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
    except Exception:  # pragma: no cover - defensive
        REQUEST_HEADER, REQUEST_HEADER_VALUE = "X-PDT-Request", "1"
    try:
        status_code, raw = http_probe(
            SERVICE_HOST, int(port), DATA_PLANE_PROBE_PATH,
            headers={REQUEST_HEADER: REQUEST_HEADER_VALUE},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        # The endpoint could not be reached.  That is not a verdict on
        # the data plane — it is the absence of one — so the instance
        # is recorded as "unreachable" and never gated on it.
        evidence.verdict = DATA_PLANE_UNREACHABLE
        evidence.error = f"{type(exc).__name__}: {exc}"
        return evidence

    if status_code == 404:
        # The instance does not publish a data plane: it is a project
        # service with no database behind it, and it is therefore never
        # gated on one.
        evidence.verdict = DATA_PLANE_ABSENT
        evidence.error = "the instance does not publish a data plane"
        return evidence
    # Read the fact sheet out of whatever came back — a 503 carries one
    # too, and the operator wants to see *what* the instance said about
    # its own data plane, not only that the probe failed.
    body: Any = None
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001 - a body that is not JSON is not fatal
        body = None
    if isinstance(body, dict):
        evidence.status = str(body.get("status") or "")
        instance = body.get("instance")
        if isinstance(instance, dict):
            evidence.boot_id = str(instance.get("boot_id") or "")
            # The instance names the database it is serving.  Only a
            # string is a name: a null or a number is no name at all,
            # and it leaves the field empty rather than inventing one.
            reported_db = instance.get("state_db_path")
            if isinstance(reported_db, str):
                evidence.state_db_path = reported_db
        rows = body.get("plan_routing_rows")
        if not isinstance(rows, bool) and isinstance(rows, int):
            evidence.plan_routing_rows = int(rows)

    if status_code != 200:
        # The endpoint answered, and what it said is unusable: 503 is
        # how the PDT backend says "I cannot read my database".
        evidence.verdict = DATA_PLANE_BROKEN
        evidence.error = f"HTTP {status_code}"
        return evidence
    if body is None:
        # A 200 this module cannot read is not a data plane it knows
        # how to judge — a catch-all page, a proxy's HTML — so the
        # instance is not gated on one.
        evidence.verdict = DATA_PLANE_ABSENT
        evidence.error = "the answer was not JSON this module can read"
        return evidence
    if not isinstance(body, dict):
        evidence.verdict = DATA_PLANE_ABSENT
        evidence.error = "the data-plane probe did not answer with an object"
        return evidence
    if evidence.status == "degraded":
        # The service says its own data plane is broken.  It answered
        # 200, but it is telling us it cannot read the database.
        evidence.verdict = DATA_PLANE_BROKEN
        evidence.error = "the instance reported a degraded data plane"
        return evidence
    if evidence.plan_routing_rows is None:
        # A 200 that carries no count is not a data plane this module
        # knows how to read, so the instance is not gated on one.
        evidence.verdict = DATA_PLANE_ABSENT
        evidence.error = "the answer carried no plan_routing_rows count"
        return evidence
    if evidence.plan_routing_rows > 0:
        evidence.verdict = DATA_PLANE_PROVED
    else:
        evidence.verdict = DATA_PLANE_EMPTY
        evidence.error = "the instance reported no plan_routing rows"
    return evidence


def _service_is_answering(decl: ServiceDeclaration) -> bool:
    """Is the service answering at all — its health URL, or the port?

    A declared ``health_url`` is the stronger signal and is what lets a
    service distinguish "listening" from "serving"; without one the
    module falls back to a TCP accept, which is all there is to ask.

    The health URL is asked over a **keep-alive** connection (see
    :func:`service_freshness.http_probe_url`): asking the question
    must not leave the service's own port in ``TIME_WAIT``.
    """
    if not decl.health_url:
        return wait_for_ready(decl.port, timeout_seconds=1)

    try:
        status_code, _body = http_probe_url(
            decl.health_url, timeout=3,
        )
        return 200 <= status_code < 400
    except Exception:  # noqa: BLE001 - any failure means "not yet"
        return False


#: The data-plane observation each readiness wait produced, keyed by
#: the port it asked.  The readiness wait is a boolean question ("is it
#: ready?"), but the answer it reached is worth keeping — "it came up"
#: and "it came up serving data" are two different facts, and only the
#: second one is worth anything to a VP.  The observation is published
#: here rather than returned, because the boolean signature of
#: :func:`wait_until_ready` is a seam that the whole test suite stubs;
#: changing it would change what every caller means by "ready".
_DATA_PLANE_OBSERVATIONS: Dict[int, "DataPlaneEvidence"] = {}
_DATA_PLANE_OBSERVATIONS_LOCK = threading.Lock()


def _remember_data_plane(port: int, evidence: DataPlaneEvidence) -> None:
    """Publish what the readiness wait found on *port*."""
    with _DATA_PLANE_OBSERVATIONS_LOCK:
        _DATA_PLANE_OBSERVATIONS[int(port)] = evidence


def _take_data_plane(port: int) -> Optional[DataPlaneEvidence]:
    """Take (and forget) what the readiness wait found on *port*.

    ``None`` when this module's readiness wait did not run — a caller
    that answered the question some other way (a test's stub, a
    different module's copy) leaves no observation behind.
    """
    with _DATA_PLANE_OBSERVATIONS_LOCK:
        return _DATA_PLANE_OBSERVATIONS.pop(int(port), None)


def wait_until_ready(
    decl: ServiceDeclaration, timeout_seconds: Optional[int] = None,
) -> bool:
    """Is the service ready — alive **and** serving data?

    The boolean answer to :func:`_wait_ready_with_evidence`, with the
    evidence it reached published for the caller that started the
    service (see :func:`_take_data_plane`).
    """
    ready, evidence = _wait_ready_with_evidence(decl, timeout_seconds)
    _remember_data_plane(decl.port, evidence)
    return ready


def _expected_routing_rows() -> Optional[int]:
    """How many routing rows the round's own fixture holds.

    ``None`` when the fixture cannot be read: the row count then
    carries no verdict, and an instance is only asked to prove that it
    *has* a readable data plane.
    """
    try:
        from service_freshness import expected_routing_rows

        return expected_routing_rows()
    except Exception:  # noqa: BLE001 - defensive: the probe never raises
        return None


def _data_plane_is_acceptable(
    evidence: DataPlaneEvidence, expected_rows: Optional[int],
    decl: Optional[ServiceDeclaration] = None,
) -> bool:
    """May the round use this instance as an address?

    Four answers, in order:

      * the instance publishes **no** data plane at all — it is a
        project service with no database behind it, and it is never
        gated on one;
      * the data plane could not be **reached** — nothing was learned,
        and nothing is not evidence of anything;
      * the instance publishes a data plane **with** routing rows — it
        is serving data, and that is what the round needs;
      * the instance publishes an **empty** (or broken) data plane — it
        is serving nothing.  That is only acceptable when the database
        it is serving is empty too: a round that has data to serve must
        not be pointed at an instance that has none, because every
        assertion that follows would be graded against an empty table.

    **Which** database decides that last question is the whole point of
    this function (2026-10-27, fact A).  It used to be the round's own
    fixture — a database that has nothing to do with the database *this
    instance* serves.  A service that declares its own database and
    serves it (a copy relocated onto a free port, reading the copy's
    own empty ``state.db``) was therefore refused for ever: the round's
    fixture held rows the copy never had and never would have.

    The judgement is made against the two statements the service made
    about its own data — the database its **declaration** names
    (:func:`declared_state_db_path`) and the database its **answer**
    names (:attr:`DataPlaneEvidence.state_db_path`):

      * the two name **different** databases → refused: the instance is
        serving data the round did not ask for;
      * the two name the **same** database → the rows **that** database
        holds decide.  An empty one is exactly what an empty service
        was declared to serve, and a database with rows that the
        instance reports as empty is refused;
      * only **one** of the two names anything → that one decides;
      * **neither** names anything → the round's own count decides, as
        it did before.
    """
    if evidence.verdict in (DATA_PLANE_ABSENT, DATA_PLANE_UNREACHABLE):
        return True
    if evidence.verdict == DATA_PLANE_BROKEN:
        return False
    if evidence.verdict != DATA_PLANE_EMPTY:
        return True

    declared = declared_state_db_path(decl) if decl is not None else ""
    reported = str(evidence.state_db_path or "").strip()
    same = _same_state_db(declared, reported)
    if same is False:
        # The instance is serving a database its own declaration never
        # named.  That is the wrong data, whatever the round holds.
        return False

    rows: Optional[int] = None
    if same is True:
        # One file, two spellings: the instance's own answer is the
        # one that names a path this process can open.
        rows = _routing_rows_in(reported)
        if rows is None:
            rows = _routing_rows_in(declared)
    elif reported:
        rows = _routing_rows_in(reported)
    elif declared:
        rows = _routing_rows_in(declared)
    if rows is None:
        # No count of its own: the round's fixture is the only fact
        # left, and it is the judgement this module made before.
        return not (expected_rows or 0) > 0
    return rows == 0


#: The prefixes a start command uses to spell "the directory the
#: service runs in".
_DECLARED_DB_CWD_PREFIXES = ("$(pwd)", "${PWD}", "$PWD")


def _normalize_declared_db(raw: str) -> str:
    """The declared database path, in a shape two names can be compared in.

    The declaration usually spells the path relative to the directory
    the service runs in (``$PWD/.pdt/state.db``).  That prefix is
    dropped, and so is the shell quoting around the whole value: what
    is left is the path **inside** the service's own directory — the
    tail the instance's own answer carries.
    """
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    for prefix in _DECLARED_DB_CWD_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            if text.startswith("/"):
                text = text[1:]
            break
    while text.startswith("./"):
        text = text[2:]
    return text


def declared_state_db_path(decl: ServiceDeclaration) -> str:
    """The state database the service's **own declaration** names.

    ``export PDT_STATE_DB_PATH=$PWD/.pdt/state.db`` is the service
    saying, before it starts, which database it is going to serve.
    It is the only statement about the data plane the service made on
    its own account, and it is therefore the statement an instance has
    to be judged against — the round's own fixture says nothing about
    what *this* service serves.

    ``""`` when the declaration names no database: the caller then has
    only the instance's own answer to go by.
    """
    if decl is None:
        return ""
    try:
        raw = start_cmd_env(str(decl.start_cmd or "")).get(
            "PDT_STATE_DB_PATH", "",
        )
    except Exception:  # noqa: BLE001 - a malformed declaration is no name
        return ""
    return _normalize_declared_db(raw)


def _path_parts(path: str) -> List[str]:
    """The path's components, without the noise (``""``, ``"."``)."""
    return [part for part in Path(path).parts if part not in ("", ".")]


def _same_state_db(declared: str, reported: str) -> Optional[bool]:
    """Do the two names point at the same state database?

    ``True`` — the same file.  ``False`` — different files.
    ``None`` — the question cannot be answered, because one of the two
    names is missing.

    Two absolute names are compared by their **real** path: the same
    file reached by two different routes is the same file.  A relative
    name is compared by its **components**: the declaration says
    ``$PWD/.pdt/state.db`` and the instance says
    ``/abs/worktree/.pdt/state.db`` — the same file, spelled two ways,
    and the relative name is the tail of the absolute one.
    """
    left = str(declared or "").strip()
    right = str(reported or "").strip()
    if not left or not right:
        return None
    if os.path.isabs(left) and os.path.isabs(right):
        try:
            return os.path.realpath(left) == os.path.realpath(right)
        except OSError:  # pragma: no cover - defensive
            return None
    left_parts = _path_parts(left)
    right_parts = _path_parts(right)
    if not left_parts or not right_parts:
        return None
    if len(left_parts) <= len(right_parts):
        return right_parts[-len(left_parts):] == left_parts
    return left_parts[-len(right_parts):] == right_parts


def _routing_rows_in(db_path: str) -> Optional[int]:
    """How many ``plan_routing`` rows the database at *db_path* holds.

    Opened **read-only**: the database belongs to a running instance,
    and a probe must not write to it — not even to switch its journal
    mode.  ``None`` when the database cannot be read at all; the caller
    then has no count of its own and falls back to the round's.

    A database with **no** ``plan_routing`` table holds no routing rows
    and says so with ``0`` rather than with "unknown": that is exactly
    what the instance serving it reports.
    """
    text = str(db_path or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    except Exception:  # noqa: BLE001 - a malformed path is no path
        return None
    try:
        # The operator's live database is off limits to a test run —
        # the same rule the write path enforces (see
        # :func:`state_machine.db.connection.open`).
        from state_machine.db.connection import (
            _refuse_real_state_db_under_pytest,
        )

        _refuse_real_state_db_under_pytest(path)
    except ImportError:
        pass
    except RuntimeError:
        return None
    try:
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0,
        )
    except Exception:  # noqa: BLE001 - an unreadable database is unknown
        return None
    try:
        row = conn.execute("SELECT COUNT(*) FROM plan_routing").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        return None
    except Exception:  # noqa: BLE001 - an unreadable database is unknown
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing never fails the probe
            pass
    return int(row[0]) if row else None


def wait_until_ready_with_evidence(
    decl: ServiceDeclaration, timeout_seconds: Optional[int] = None,
) -> Tuple[bool, DataPlaneEvidence]:
    """Wait for the service to be ready, and report what it is serving.

    See :func:`_wait_ready_with_evidence`; this is the same wait with
    its evidence handed back instead of published.
    """
    return _wait_ready_with_evidence(decl, timeout_seconds)


def _wait_ready_with_evidence(
    decl: ServiceDeclaration, timeout_seconds: Optional[int] = None,
) -> Tuple[bool, DataPlaneEvidence]:
    """Wait for the service to be ready, and report what it is serving.

    Readiness is **two** questions, asked in order:

      1. is the service answering — its declared health URL with 200, or
         the port with a TCP accept?
      2. does the instance publish a data plane, and does that data
         plane hold routing rows?

    The second question is the one that was missing (2026-10-25).  A
    service can answer ``/health`` with 200 and still be serving an
    empty database — the 2026-10-24 production round was graded on
    exactly that: the endpoint answered 200, ``plan_routing_rows`` was
    0, and every later assertion ran against nothing.  "The service is
    up" is not the claim a VP depends on; "the service is serving the
    round's data" is.

    An instance that publishes no data plane at all is **not** gated on
    one — a project service with no database behind it is not a service
    serving the wrong database.  Neither is an instance whose data
    plane could not be reached at all: that is the absence of an
    answer, not a wrong answer.

    The **declaration** goes into the judgement (2026-10-27, fact A).
    A copy relocated onto a free port serves the database it declared
    for itself, and that database is empty where the round's own
    fixture holds rows — so the round's count is not the count that
    says anything about this instance.  The declaration is what the
    service said about its own data before it started, and it is the
    statement the instance has to be judged against.

    A service that answers but proves nothing stays unready and is
    asked again, until the timeout; when the timeout runs out the
    caller fails it exactly as it failed a service that never came up.

    Returns ``(ready, evidence)``.  The evidence is what the caller
    records, so "it came up" and "it came up serving data" stay two
    separately readable facts.
    """
    timeout = timeout_seconds or decl.ready_timeout_seconds
    deadline = time.monotonic() + timeout
    expected_rows = _expected_routing_rows()
    evidence = DataPlaneEvidence()
    while True:
        if _service_is_answering(decl):
            evidence = data_plane_evidence(decl.port)
            if _data_plane_is_acceptable(evidence, expected_rows, decl):
                return True, evidence
        if time.monotonic() >= deadline:
            return False, evidence
        time.sleep(_READY_POLL_INTERVAL)


def _relocate_service(
    decl: ServiceDeclaration,
    project_dir: Path,
    *,
    foreign_cmdline: str = "",
) -> ServiceRuntime:
    """Start the service on a different, free port — without touching the
    process that currently holds the declared one.

    This is the third ownership rule from the module docstring: a port
    held by a process we cannot positively identify is **not ours to
    kill**, but it is also not a reason to strand the round. The shape it
    exists for: the operator had ``next-server`` (a production
    build) on :3000 while the plan wanted ``next dev`` — and the old
    behaviour was to refuse and block every VP that depended on it.

    The declared ``start_cmd`` / ``health_url`` are rewritten to the new
    port, and ``PDT_SVC_<NAME>_*`` is exported into the child so a VP (or
    the service itself) can discover where it landed.

    Readiness is retried on a **fresh** port, because a failure can mean
    either a TOCTOU race (someone took the port between :func:`allocate_free_port`
    and the service's bind) or a service that ignored our rewrite and
    tried to bind the occupied declared port. Both are worth another port.

    When every attempt has failed the runtime that comes back is
    explicitly **unusable** (2026-10-25, repair-r1-29): the port it
    names is the declared one, and the declared one belongs to somebody
    else.  A downstream reader must not be able to mistake that for an
    address, and every candidate port the relocation tried is recorded
    so the question "where did this port come from" has an answer.
    """
    last_detail = ""
    candidates: List[int] = []
    for attempt in range(1, _RELOCATION_ATTEMPTS + 1):
        port = allocate_free_port()
        if port is None or port == decl.port:
            last_detail = "could not allocate a free port"
            continue

        relocated = render_declaration(decl, port)
        # The round that is starting services right now (2026-10-25,
        # repair-r1-25).  The new port stands in for the declared one,
        # so the context says which port the address came from.
        _ctx = _ADDRESS_EVENT_CONTEXT.get() or {}
        with address_event_context(
            _ctx.get("plan_dir"),
            run_id=str(_ctx.get("run_id") or ""),
            round_token=str(_ctx.get("round_token") or ""),
            declared_port=decl.port,
        ):
            runtime = start_service(relocated, project_dir)
        runtime.declared_port = decl.port
        candidates.append(port)
        if runtime.ready:
            runtime.origin = "spawned"
            runtime.detail = (
                f"port {decl.port} held by a process that does not look "
                f"like service {decl.name!r} ({foreign_cmdline}); left it "
                f"alone and started a copy on port {port} instead "
                f"(attempt {attempt}/{_RELOCATION_ATTEMPTS})"
            )
            runtime.candidate_ports = list(candidates)
            # The address moved: the old one, the new one, and why
            # (2026-10-25, repair-r1-25).
            _emit_address_event(
                runtime, _ctx.get("plan_dir"), "relocated",
                run_id=str(_ctx.get("run_id") or ""),
                round_token=str(_ctx.get("round_token") or ""),
                old_port=decl.port, new_port=port, declared_port=decl.port,
                reason=(
                    f"port {decl.port} was held by a process that does not "
                    f"look like service {decl.name!r} ({foreign_cmdline}); "
                    f"left it alone and started a copy on port {port}"
                ),
            )
            return runtime

        # A failed attempt may still have left a half-started process
        # behind — clean it up before trying another port, or the ledger
        # accumulates orphans.
        if runtime.pid:
            stop_service(
                runtime, relocated, grace_seconds=5.0,
                caller="service_manager._relocate_service",
                reason=(
                    "a half-started service is stopped before the next "
                    "port is tried"
                ),
                round_token=str(_ctx.get("round_token") or ""),
                plan_dir=_ctx.get("plan_dir"),
            )
        last_detail = runtime.detail

    failed = ServiceRuntime(
        name=decl.name, port=decl.port, declared_port=decl.port,
        start_cmd=decl.start_cmd, cwd=decl.cwd,
        reap_on_exit=decl.reap_on_exit,
    )
    failed.origin = "failed"
    tried = ", ".join(str(p) for p in candidates) or "none"
    failed.detail = (
        f"port {decl.port} is held by a process that does not look like "
        f"service {decl.name!r} ({foreign_cmdline}), and relocating failed "
        f"after {_RELOCATION_ATTEMPTS} attempt(s) on port(s) {tried}: "
        f"{last_detail}"
    )
    # 2026-10-25 (repair-r1-29): the failure is **explicit**.  The port
    # this runtime names is the declared one, and the declared one is
    # somebody else's — so it is not an address, and no downstream may
    # fall back to it.
    failed.unusable = True
    failed.failure_reason = failed.detail
    failed.candidate_ports = list(candidates)
    return failed


def _await_exit(proc: subprocess.Popen, timeout: float) -> tuple:
    """Wait briefly for *proc* to exit. Returns ``(exited, returncode)``.

    Polling once is not enough where this is used. ``start_service``
    spawns ``/bin/bash -lc <start_cmd>``, so when the command inside
    fails — EADDRINUSE, say — the shell has to notice and unwind before
    ``Popen`` can report a code. The readiness probe, meanwhile, returns
    in milliseconds because *something* is already answering on the port.
    Checking straight after it therefore samples the shell mid-teardown
    and reports a dead service as started.

    The wait is short and bounded: the caller is on the start path, and
    the question being asked — "did the thing we just launched die
    immediately?" — is answered by the first tenth of a second in the
    cases that matter. A service that starts cleanly is still running
    when the window closes, which is the answer we want.
    """
    deadline = time.monotonic() + timeout
    while True:
        code = proc.poll()
        if code is not None:
            return True, code
        if time.monotonic() >= deadline:
            return False, None
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# The spawn marker
# ---------------------------------------------------------------------------

#: The marker keys, spelled out here as well as in :mod:`orphan_reaper`.
#:
#: The names are a wire protocol the reaper reads, and they are named
#: here for one reason: the marker **inherited** from a parent has to
#: be removed even when the reaper module itself could not be imported.
#: A marker that names an owner which did not start the service is
#: worse than no marker at all.
_SPAWN_MARKER_KEYS = (
    "PDT_SPAWN_OWNER_PID",
    "PDT_SPAWN_OWNER_START",
    "PDT_SPAWN_KIND",
)

#: The warnings already spoken about the marker, by topic.
#:
#: The marker is best-effort, so a broken one must not turn into a log
#: line for every service a round starts.
_SPAWN_MARKER_WARNED: Set[str] = set()


def _warn_once(topic: str, message: str, *args: Any) -> None:
    """Say *message* the first time *topic* comes up -- and never again."""
    if topic in _SPAWN_MARKER_WARNED:
        return
    _SPAWN_MARKER_WARNED.add(topic)
    logger.warning(message, *args)


def _spawn_marker_keys() -> Tuple[str, ...]:
    """The marker keys the reaper reads.

    The reaper's own names when it is importable -- those are the wire
    protocol -- and the literal names otherwise, so that a marker
    inherited from a parent can still be removed when the module is
    not there.
    """
    module = orphan_reaper
    if module is not None:
        try:
            return (
                module.SPAWN_OWNER_PID,
                module.SPAWN_OWNER_START,
                module.SPAWN_KIND,
            )
        except AttributeError:  # pragma: no cover - defensive
            pass
    return _SPAWN_MARKER_KEYS


def _spawn_marker_env() -> Dict[str, str]:
    """The marker a service child inherits from this process.

    The service is started detached, so it outlives whatever started
    it -- which is exactly the process the reaper has to recognise as
    an orphan later.  The marker says *who* started it
    (``OWNER_PID``), *when that owner started* (``OWNER_START``, which
    is what tells a live owner from a recycled pid) and *what it is*
    (``KIND``).

    The marker is best-effort and never load-bearing: it is how the
    reaper finds a process, not what makes the process run.  A missing
    reaper module, or a probe that could not be read, therefore
    degrades to a warning and to a marker with an unknown owner start
    -- "unknown" is not "different" (see
    :func:`orphan_reaper.process_start_epoch`), so a service whose
    owner start could not be read is one the reaper recognises and
    leaves alone.
    """
    module = orphan_reaper
    if module is None:
        _warn_once(
            "missing-module",
            "[service_manager] orphan_reaper is not importable; services "
            "are started without a spawn marker",
        )
        return {}
    try:
        return dict(module.spawn_tags(module.KIND_SERVICE))
    except Exception as exc:  # noqa: BLE001 - a marker never blocks a spawn
        _warn_once(
            "spawn-tags",
            "[service_manager] could not build the spawn marker (%s); the "
            "service is started with an unknown owner start", exc,
        )
    try:
        return {
            module.SPAWN_OWNER_PID: str(os.getpid()),
            module.SPAWN_OWNER_START: "",
            module.SPAWN_KIND: module.KIND_SERVICE,
        }
    except Exception as exc:  # noqa: BLE001 - defensive
        _warn_once(
            "marker-keys",
            "[service_manager] the spawn marker has no readable keys (%s); "
            "the service is started without one", exc,
        )
        return {}


def _apply_spawn_marker(env: MutableMapping[str, str]) -> None:
    """Stamp *env* with this process's spawn marker.

    The marker is **replaced**, never merged.  A child inherits its
    parent's marker, and a marker that survived a new owner would
    attribute the service to an ancestor that may long since be gone
    -- the owner of a process is the process that actually started it,
    so the marker is stamped at every spawn level.
    """
    for key in _spawn_marker_keys():
        env.pop(key, None)
    env.update(_spawn_marker_env())


def _register_spawned_service(runtime: ServiceRuntime) -> None:
    """Write the service down, so the run that started it can give it back.

    A service is started detached: it calls ``setsid()`` and leads its
    own session, so a watchdog that kills the gate's process group does
    not touch it.  That is what makes a service outlive the run that
    started it -- the CI of 2026-10-31 left a ``backend.server``
    answering ``/health`` a minute after the watchdog had fired -- and
    without a record of it the run has no way to take it back.

    The record is best-effort.  A run that names no registry gets no
    registry, and a service that cannot be written down is a service
    somebody else has to find.
    """
    module = orphan_reaper
    if module is None:
        return
    try:
        module.register_spawned_process(
            runtime.pid,
            pgid=runtime.pgid,
            kind=module.KIND_SERVICE,
            name=runtime.name,
        )
    except Exception as exc:  # noqa: BLE001 - a record never blocks a start
        logger.debug(
            "[service_manager] could not write the service down: %s", exc,
        )


def start_service(
    decl: ServiceDeclaration, project_dir: Path,
    *, timeout_seconds: Optional[int] = None,
    preserve_data: bool = False,
    plan_dir: Optional[Path] = None,
) -> ServiceRuntime:
    """Launch one declared service detached and wait for readiness.

    Detached so it outlives this process: the wrapper the service is
    started through calls ``setsid()`` for that — which is also why C3
    exists: a detached process nobody reaps is an orphan.

    ``preserve_data`` is the flag that says **this is a restart inside
    a round**: the start command is rendered without the part that
    re-creates the round's data, and the child is told so through
    :data:`SKIP_SEED_ENV`.  See the "restarting without re-seeding"
    section below for why that is a different thing from starting a
    service for the first time.

    The address this call picks is **written down** where it happened
    (2026-10-25, repair-r1-25) — but only when the round that is
    starting services knows its plan directory.  That knowledge is
    ambient (see :func:`address_event_context`) rather than a
    parameter, because the signature of this function is relied on by
    callers and tests that have no reason to know about the event
    stream.  A round that does not know its plan directory gets no
    event here — the code that records the ledger writes the missing
    one instead.
    """
    runtime = ServiceRuntime(
        name=decl.name, port=decl.port, start_cmd=decl.start_cmd,
        cwd=decl.cwd, reap_on_exit=decl.reap_on_exit,
    )
    if decl.port in PROTECTED_PORTS:
        runtime.detail = (
            f"port {decl.port} is an backend-runtime port; not starting"
        )
        return runtime

    workdir = Path(project_dir) / decl.cwd
    if not workdir.is_dir():
        runtime.detail = f"cwd {decl.cwd!r} does not exist"
        return runtime

    # 2026-10-26: a restart **inside** a round must not re-create the
    # round's data.  The start command is rendered without the part
    # that destroys it, and the child is told which kind of start this
    # is through the environment.
    data_paths: List[str] = []
    start_cmd = decl.start_cmd
    state_db_inode_before: Optional[int] = None
    if preserve_data:
        data_paths = declared_data_paths(decl.start_cmd)
        start_cmd = render_start_cmd_preserving_data(decl.start_cmd, data_paths)
        state_db_inode_before = _state_db_inode(data_paths, workdir)

    log_file = _log_path_for(decl, project_dir)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        runtime.detail = f"cannot create log dir: {exc}"
        return runtime
    runtime.log_path = str(log_file)

    try:
        env = dict(os.environ)
        env.update(service_env(decl))
        # 服务是一次性 fixture，不做全盘凭据清扫；主 server 不走这条路径。
        env["PDT_DISABLE_TEMP_SWEEP"] = "1"
        # The spawn marker: this service is started detached, so it
        # outlives this process -- which is exactly the process the
        # reaper has to recognise as an orphan later.  The marker
        # replaces any inherited one; the owner is the process that
        # actually started the service.
        _apply_spawn_marker(env)
        if preserve_data:
            # A start command that knows the convention can skip its
            # own seed; one that does not is rendered seed-free above.
            # Both are told which kind of start this is.
            env[SKIP_SEED_ENV] = "1"
        with open(log_file, "ab") as log_fp:
            proc = subprocess.Popen(
                _detached_argv(start_cmd),
                cwd=str(workdir),
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
    except Exception as exc:  # noqa: BLE001 - never abort the round
        runtime.detail = f"spawn failed: {type(exc).__name__}: {exc}"
        return runtime

    runtime.pid = proc.pid
    runtime.pgid = _detached_pgid_of(proc.pid)
    # The service leads a session of its own, so the run that started
    # it is the only thing that can give it back.  The record is what
    # makes that possible.
    _register_spawned_service(runtime)
    # Our own clock, not the system's. `proc` was created by the line
    # above, so we know when it started without asking anyone, and that
    # matters twice over: the `ps -o lstart=` probe is unavailable or
    # racy on a machine that restricts subprocesses (the unit test
    # environment does), and a `None` here silently disables the PID-reuse
    # guard in `_pid_reused` — a recycled PID would then be signalled as
    # if it were still ours. The comparison tolerates 2 seconds and
    # `ps lstart` only has 1-second granularity, so a wall-clock reading
    # taken microseconds after the fork is well inside it.
    runtime.start_epoch = time.time()
    runtime.origin = "spawned"

    # The address is spoken for from here on: say so, before anything
    # can write it into the ledger (2026-10-25, repair-r1-25).
    _ctx = _ADDRESS_EVENT_CONTEXT.get() or {}
    _emit_address_event(
        runtime, _ctx.get("plan_dir"), "allocated",
        run_id=str(_ctx.get("run_id") or ""),
        round_token=str(_ctx.get("round_token") or ""),
        old_port=None, new_port=runtime.port,
        declared_port=(
            _ctx.get("declared_port")
            if _ctx.get("declared_port") is not None else decl.port
        ),
        reason=f"started service {decl.name!r} on port {runtime.port}",
    )

    # And somebody has to watch it (2026-10-26, repair-r1-47).  The
    # readiness wait below asks "is the service answering?" **once**;
    # everything after that used to be a record of a past observation.
    # On 2026-10-26 the api service was recorded as ready at 02:21:49
    # and was gone two seconds later, and the round said nothing until
    # a VP found the port refused two minutes and forty-four seconds
    # later.  The watcher closes that gap: it polls the child, and the
    # moment the child is gone it records the death and marks the
    # runtime dead.
    #
    # It is started here — before the readiness wait, not after it —
    # because a service that dies while the wait is still running must
    # be seen by the same watcher as one that dies afterwards.
    _start_process_watcher(
        runtime, proc,
        plan_dir=_ctx.get("plan_dir"),
        round_token=str(_ctx.get("round_token") or ""),
    )

    def _record_what_the_restart_kept() -> None:
        """Say what the restart kept — or that it kept nothing.

        Called once the child has had its chance to run its seed: the
        question "did the data survive" is only answerable after the
        seed would have run.
        """
        if not preserve_data:
            return
        _record_restart_preservation(
            plan_dir or _ambient_plan_dir(),
            runtime=runtime,
            data_paths=data_paths,
            workdir=workdir,
            before_inode=state_db_inode_before,
            rendered=start_cmd,
        )

    ready = wait_until_ready(decl, timeout_seconds)
    # The evidence is recorded on the runtime whatever the verdict is:
    # "it came up" and "it came up serving data" are two different
    # facts, and the round's log has to be able to tell them apart.
    # It is the evidence the readiness decision was actually made on —
    # published by the wait itself, because the boolean signature of
    # ``wait_until_ready`` is a seam the whole test suite stubs.
    evidence = _take_data_plane(decl.port)
    runtime.data_plane = evidence
    if ready:
        # "Something answers on this port" is not "our service is up".
        # The two come apart when the port is taken by something else in
        # the window between choosing it and binding it: our child dies
        # with EADDRINUSE, the readiness probe finds the other listener,
        # and the caller is handed `ready=True` for a process that is not
        # serving. That was observed locally with a desktop application
        # holding the port — and the mistake is not harmless downstream,
        # because the ledger then records a pid that never served, and
        # every later reap targets the wrong process.
        #
        # So confirm the holder is ours before claiming readiness.
        #
        # The check has to wait rather than sample once. `proc` is
        # `/bin/bash -lc <start_cmd>`, so a child that dies on
        # EADDRINUSE does not make `proc` exit at the same instant — the
        # shell still has to unwind and exit itself, and on this machine
        # that is the difference between "already dead" and "not yet".
        # Sampling once straight after the readiness probe, which returns
        # in milliseconds, reliably caught the shell before it had exited
        # and reported the dead service as ready.
        exited, rc = _await_exit(proc, timeout=2.0)
        if exited:
            runtime.ready = False
            runtime.detail = _failure_detail(
                pid=proc.pid,
                pgid=runtime.pgid,
                fds=_fd_count(proc.pid),
                timeout_seconds=decl.ready_timeout_seconds,
                rc=rc,
                log_file=log_file,
            )
            _record_what_the_restart_kept()
            return runtime
        runtime.ready = True
        runtime.detail = (
            f"started (pid {proc.pid}); ready within "
            f"{decl.ready_timeout_seconds}s"
        )
        if evidence is not None and evidence.proved:
            runtime.detail += (
                f"; serving {evidence.plan_routing_rows} plan_routing row(s)"
            )
        _record_what_the_restart_kept()
        return runtime

    # Not ready: report what the log says so the operator gets a
    # diagnosable reason instead of a bare timeout. The tail alone is
    # not enough — a service that fails early drowns its own traceback
    # in interpreter-shutdown noise — so the excerpt of the first
    # error comes first, and the tail after it.
    runtime.detail = _failure_detail(
        pid=proc.pid,
        pgid=runtime.pgid,
        fds=_fd_count(proc.pid),
        timeout_seconds=decl.ready_timeout_seconds,
        rc=proc.poll(),
        log_file=log_file,
    )
    runtime.detail += _data_plane_detail(evidence)
    _record_what_the_restart_kept()
    return runtime


# ---------------------------------------------------------------------------
# Restarting a service without re-seeding it (2026-10-26)
# ---------------------------------------------------------------------------
#
# The incident: the round's api service came up at 02:21:49 and was gone
# two seconds later.  The round brought it back — and the bring-back ran
# the service's **declared** start command again, which is also the
# command that **re-creates the round's fixture** from scratch.  In the
# 2026-10-26 round that command was a program handed to the
# interpreter, and the program wiped the plans tree and the state
# database before writing them again::
#
#     for child in list(plans.iterdir()):
#         shutil.rmtree(child)      # the plans tree
#     stale = base / ("state.db" + suffix)
#     stale.unlink()                # the state database
#
# (A shell-level ``rm -rf .pdt/vp_quota/plans/*`` is the same seed in
# another costume; both are recognised.)
#
# The port came back and the data under it was a *different* dataset: a
# new database file (a new inode), a rewritten execution log, an
# in-progress task reset to pending.  A VP that read the service after
# the restart was reading a world the round had never seen — and the
# operator, asking "why did this VP see an empty plan?", was answered
# by a fixture that had been rebuilt underneath the question.
#
# So the rule is about the round, not about the data:
#
#   * the **first** ensure of a round may seed — it is the one that
#     finds nothing to lose;
#   * a restart **inside** a round may not.  The round is using that
#     data right now, and re-creating it changes the ground under the
#     VPs that are still running.
#
# "Inside a round" is a fact about the **ledger**: the plan's ledger
# says which processes the round started, and the owner registry says
# which rounds are still running.  A ledger entry owned by a round that
# is still alive is a service that round is using right now — so
# bringing it back must not re-create the data under it.
#
# Nothing here changes **when** a service is judged ready, or what the
# data-plane gate says about it.  The only thing that changes is
# whether the round's data is re-created when the service comes back.


#: The environment variable a start command may check to skip its own
#: seed segment.  It is set on every restart that goes through
#: :func:`restart_service_preserving_data`, so a start command that
#: knows the convention can skip its seed without this module having to
#: guess where in the script the seed is.
SKIP_SEED_ENV = "PDT_SVC_SKIP_SEED"

#: Where the "this restart kept the round's data" record lives,
#: relative to the plan directory.  Same shape as the kill events and
#: the process events: one JSON object per line, appended.
RESTART_EVENTS_FILENAME = "service_restart_events.jsonl"

#: The event written when a restart did **not** re-seed: the data the
#: round was using is still there, and the record names the inode that
#: proves it.
RESTART_PRESERVED_DATA_EVENT = "service_restart_preserved_data"

#: The event written when a restart **did** re-seed after all — the
#: rendering could not protect the data, and the round has to be able
#: to see that it happened.
RESTART_RESEEDED_EVENT = "service_restart_reseeded"


#: The shell redirection that opens a heredoc.  A here-string
#: (``<<<``) is not one: it has no body, so there is nothing to keep
#: together with the command that opened it.
_HEREDOC_RE = re.compile(
    r"<<-?\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))"
)

#: The operations that **destroy** files or directories.  A start
#: command that runs one of them over the round's data is re-creating
#: that data, and a restart that does that takes the ground out from
#: under the VPs that are still running.
_DESTRUCTIVE_RE = re.compile(
    r"(?:^|[;&|]\s*|\b(?:then|do|else)\s+)\s*rm\s+"      # rm
    r"|(?:^|[;&|]\s*|\b(?:then|do|else)\s+)\s*rmdir\s+"   # rmdir
    r"|\bunlink\b"                                       # unlink(1)
    r"|\bshutil\.rmtree\b"                              # shutil.rmtree
    r"|\bos\.(?:remove|unlink)\b"                       # os.remove
    r"|\.unlink\(\)"                                    # Path.unlink()
    r"|\.rmdir\(\)"                                     # Path.rmdir()
    r"|\btruncate\b"                                    # truncate(1)
    r"|\bdd\s+[^\n]*\bof="                              # dd of=...
    # A truncating redirect to a **path**.  The target has to carry a
    # slash, so a comparison like ``if n > 1.5`` — which is ordinary
    # code in a seed program — is not mistaken for a destruction.
    r"|(?:^|[;&|]\s*|\s)\s*>\s*[^\s>&|]*/"
    , re.IGNORECASE
)

#: An ``export NAME=VALUE`` line in a start command.
_EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _shell_blocks(script: str) -> List[str]:
    """Split a shell script into the commands it runs, in order.

    The split is at **newlines**, because that is where a shell ends a
    command — and because a command's own ``&&`` / ``||`` chain belongs
    to it: ``rm -rf x || exit 1`` is one command, and cutting the
    ``exit 1`` off it would leave a script that stops before it starts.

    A heredoc body belongs to the command that opened it.  That matters
    here more than anywhere else: the seed of a fixture is very often a
    whole program handed to the interpreter, and a script cut in the
    middle of it does not run at all.
    """
    blocks: List[str] = []
    current: List[str] = []
    pending: List[str] = []
    for line in script.splitlines():
        if pending:
            current.append(line)
            if line.strip() == pending[0]:
                pending.pop(0)
                if not pending:
                    # The heredoc is closed: the command that opened it
                    # and its body are one block, and the next line
                    # starts a new one.
                    blocks.append("\n".join(current))
                    current = []
            continue
        stripped = line.strip()
        if not stripped:
            continue
        current.append(line)
        for match in _HEREDOC_RE.finditer(line):
            terminator = match.group(1) or match.group(2) or match.group(3)
            if terminator:
                pending.append(terminator)
        if not pending:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _is_destructive(command: str) -> bool:
    """Does this command destroy files or directories?"""
    return bool(_DESTRUCTIVE_RE.search(command))


def declared_data_paths(start_cmd: str) -> List[str]:
    """The paths a start command says it works with.

    A start command names its data in its own environment: the plans
    directory it serves, the state database it reads.  Those exports
    are the only thing in the script that says "this is the data" —
    the seed that destroys the data is usually a program that computes
    the paths itself, and says nothing about them.

    Only values that look like paths are taken (they carry a
    separator); a bare name (``PYTHONPATH=backend``) is a module path,
    not data.
    """
    paths: List[str] = []
    for line in start_cmd.splitlines():
        match = _EXPORT_RE.match(line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = value.strip()
        if not value or "/" not in value:
            continue
        paths.append(value)
    return paths


def _path_components(raw: str) -> List[str]:
    """The pieces a declared path is made of.

    The ``$(pwd)`` prefix is dropped: it says *where* the path is, not
    *what* it is, and the seed that destroys the data usually spells
    the pieces rather than the whole path.
    """
    text = raw.strip()
    for prefix in ("$(pwd)", "${PWD}", "$PWD"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return [part for part in text.split("/") if part and part != "."]


def _mentions_data(command: str, data_paths: Sequence[str]) -> bool:
    """Does this command touch one of the paths the start command declared?

    The match is by **component**, not by the whole path: the seed is
    usually a program that computes the path itself, so it spells the
    pieces (``".pdt"``, ``"vp_quota"``, ``"plans"``) rather than the
    path.  Every piece of at least one declared path has to appear in
    the command, so a command that merely mentions one word of it — a
    socket cleanup, a temporary file — is not mistaken for the seed.
    """
    for raw in data_paths:
        parts = _path_components(raw)
        if len(parts) < 2:
            continue
        if all(part in command for part in parts):
            return True
    return False


def render_start_cmd_preserving_data(
    start_cmd: str, data_paths: Sequence[str],
) -> str:
    """Render *start_cmd* without the part that re-creates the data.

    The seed is the command that destroys the data the start command
    declared; this drops it and keeps everything else, in order.

    Two commands are left alone on purpose:

      * the **last** command of the script — that is the one that
        starts the service, and a script whose launcher was removed is
        a script that starts nothing;
      * a command that does not destroy anything, whatever it mentions.

    When nothing is dropped the command comes back unchanged: a start
    command that does not seed anything has nothing to preserve, and
    the only thing this module could do about it is break it.
    """
    blocks = _shell_blocks(start_cmd)
    if len(blocks) < 2:
        return start_cmd
    kept: List[str] = []
    dropped = 0
    for index, block in enumerate(blocks):
        is_last = index == len(blocks) - 1
        if (
            not is_last
            and _is_destructive(block)
            and _mentions_data(block, data_paths)
        ):
            dropped += 1
            continue
        kept.append(block)
    if not dropped:
        return start_cmd
    rendered = "\n".join(kept)
    if start_cmd.endswith("\n") and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _resolve_data_path(raw: str, workdir: Path) -> Path:
    """Where a path a start command names actually is.

    The command's own ``$(pwd)`` is the directory it runs in — the
    work directory, not the process's.
    """
    text = raw.strip()
    for prefix in ("$(pwd)", "${PWD}", "$PWD"):
        if text.startswith(prefix):
            rest = text[len(prefix):].lstrip("/")
            return Path(workdir) / rest if rest else Path(workdir)
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    return Path(workdir) / candidate


def _state_db_path(
    data_paths: Sequence[str], workdir: Path,
) -> Optional[Path]:
    """Where the state database the start command names actually is."""
    for raw in data_paths:
        resolved = _resolve_data_path(raw, workdir)
        if resolved.name.endswith(".db"):
            return resolved
    return None


def _state_db_inode(
    data_paths: Sequence[str], workdir: Path,
) -> Optional[int]:
    """The inode of the state database the start command names.

    The inode is the fact that says "this is the same database file" —
    the path alone would not: a seed that re-creates the database
    leaves the same path behind with a different file under it.
    """
    path = _state_db_path(data_paths, workdir)
    if path is None:
        return None
    try:
        return int(path.stat().st_ino)
    except OSError:
        return None


def restart_events_path(plan_dir: Path) -> Path:
    """Where a plan's restart event stream lives."""
    return Path(plan_dir) / ADDRESS_EVENTS_DIRNAME / RESTART_EVENTS_FILENAME


def record_restart_event(plan_dir: Path, payload: Dict[str, Any]) -> bool:
    """Append one restart event to the plan's stream.

    Never raises.  A failed write costs one line on stderr and returns
    ``False`` — a round must not die because its log could not be
    written.
    """
    path = restart_events_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "[service_manager] could not write restart event %s: %s",
            path, exc,
        )
        return False
    return True


def read_restart_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """Read a plan's restart event stream.

    A line that is not a JSON object is skipped rather than fatal: one
    torn line must not hide the events around it.
    """
    path = restart_events_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _record_restart_preservation(
    plan_dir: Optional[Path],
    runtime: ServiceRuntime,
    *,
    data_paths: Sequence[str],
    workdir: Path,
    before_inode: Optional[int],
    rendered: str,
) -> bool:
    """Say what the restart kept — or that it kept nothing.

    The event is written **after** the child had its chance to run its
    seed: "the data survived" is only answerable once the seed would
    have run.  A restart whose data did not survive is recorded under
    its own name, because a record that says "preserved" for a restart
    that re-seeded would teach the round to ignore the stream.
    """
    if plan_dir is None:
        return False
    after_inode = _state_db_inode(data_paths, workdir)
    # The inode is the proof, so the record says whether there **was**
    # one to compare: a service that declares no state database has
    # nothing to preserve, and saying "preserved" about it without
    # saying so would read like a verified fact.
    verified = before_inode is not None
    preserved = (not verified) or after_inode == before_inode
    payload = {
        "event": (
            RESTART_PRESERVED_DATA_EVENT if preserved
            else RESTART_RESEEDED_EVENT
        ),
        "plan_dir": str(plan_dir),
        "state_db_inode": after_inode,
        "previous_state_db_inode": before_inode,
        "verified_by_inode": verified,
        "service": runtime.name,
        "port": runtime.port,
        "pid": runtime.pid,
        "start_cmd": rendered,
        "at": datetime.now().isoformat(),
    }
    return record_restart_event(plan_dir, payload)


@dataclass
class _LiveServiceOwner:
    """A live round that is using one service right now.

    ``plan_dir`` is where that round's bookkeeping lives; ``entry`` is
    the ledger row that says the round started the service.
    """

    plan_dir: Path
    entry: Dict[str, Any]


def _runtime_from_ledger_entry(entry: Dict[str, Any]) -> ServiceRuntime:
    """The runtime a ledger row describes.

    A ledger row is the record of a service the round started: the
    process, the address, the command.  Rebuilding the runtime from it
    is what lets a restart act on **the service the round had** rather
    than on a fresh, empty description of a service.
    """
    runtime = ServiceRuntime(
        name=str(entry.get("name") or ""),
        port=int(entry.get("port") or 0),
        declared_port=(
            int(entry["declared_port"])
            if isinstance(entry.get("declared_port"), int) else None
        ),
        start_cmd=str(entry.get("start_cmd") or ""),
        cwd=str(entry.get("cwd") or "."),
        log_path=str(entry.get("log_path") or ""),
        origin=str(entry.get("origin") or "spawned"),
        ready=bool(entry.get("ready")),
        owner_token=str(entry.get("owner_token") or ""),
    )
    if isinstance(entry.get("pid"), int):
        runtime.pid = int(entry["pid"])
    if isinstance(entry.get("pgid"), int):
        runtime.pgid = int(entry["pgid"])
    return runtime


def _live_round_using_service(
    decl: ServiceDeclaration, project_dir: Path,
    plan_dir: Optional[Path],
) -> Optional[_LiveServiceOwner]:
    """The live round that is using this service, if there is one.

    ``None`` when no live round owns it — which is the case for the
    **first** ensure of a round, and the only case in which a full seed
    is allowed.

    The evidence is the plan's **ledger** (which processes this plan
    started) together with the **owner registry** (which of those
    rounds are still running).  A ledger entry whose owner is still
    alive is a service that round is using right now.

    The plan directory of the caller, when it knows one, is searched
    first; otherwise the ledgers of the plans tree are — which is what
    the round's own recovery path needs, because that path does not
    know which plan it is recovering.
    """
    candidates: List[Path] = []
    if plan_dir is not None:
        candidates.append(Path(plan_dir))
    else:
        tree = _plans_tree()
        try:
            candidates.extend(
                sorted(p.parent for p in tree.glob(f"*/{LEDGER_FILENAME}"))
            )
        except OSError:
            candidates = []
    for candidate in candidates:
        ledger = read_ledger(candidate)
        entries = ledger.get("services")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "") != decl.name:
                continue
            ports: set = set()
            for key in ("port", "declared_port"):
                value = entry.get(key)
                if isinstance(value, int):
                    ports.add(value)
            if decl.port not in ports:
                continue
            workspace = {
                "project_dir": str(ledger.get("project_dir") or ""),
                "cwd": str(entry.get("cwd") or ""),
            }
            if not _evidence_is_our_workspace(workspace, project_dir):
                continue
            owner = str(entry.get("owner_token") or "")
            if owner and _ledger_owner_is_live(candidate, owner):
                return _LiveServiceOwner(
                    plan_dir=candidate, entry=entry,
                )
    return None


def restart_service_preserving_data(
    runtime: ServiceRuntime,
    plan_dir: Path,
    *,
    decl: Optional[ServiceDeclaration] = None,
    project_dir: Optional[Path] = None,
) -> ServiceRuntime:
    """Bring *runtime*'s service back **without** re-creating its data.

    This is the restart a round does for a service that died after the
    preflight — the service that was ready at 02:21:49 and gone two
    seconds later.  The port comes back either way; what must not come
    back with it is a **rebuilt** dataset.

    The start command is rendered without the part that destroys the
    data it declared (see :func:`render_start_cmd_preserving_data`),
    the child is told which kind of start this is through
    :data:`SKIP_SEED_ENV`, and what the restart kept is written to the
    plan's restart event stream.

    The old process is stopped first **only when it is still there**:
    the common case for a round-internal restart is a service that
    already died, and signalling a pid that has since been recycled
    would hit somebody else's process.

    Never raises: a restart that fails is a failed service, and the
    caller decides what to do about it.
    """
    if decl is None:
        decl = ServiceDeclaration(
            name=runtime.name, port=runtime.port,
            start_cmd=runtime.start_cmd, cwd=runtime.cwd,
            log_path=runtime.log_path,
        )
    if project_dir is None:
        recorded = read_ledger(plan_dir).get("project_dir")
        project_dir = Path(recorded) if recorded else Path(runtime.cwd)
    project_dir = Path(project_dir)
    # The old process first: the port has to be free before the new one
    # can take it.  Only when it is *our* process that still holds it.
    if runtime.pid:
        try:
            probe = probe_port(decl.port)
        except Exception:  # noqa: BLE001 - a failed probe is not a verdict
            probe = None
        holders = set(probe.pids or []) if probe is not None else set()
        if probe is not None and probe.listening and runtime.pid in holders:
            stop_service(
                runtime, decl,
                caller="service_manager.restart_service_preserving_data",
                reason=(
                    "the service is being restarted without re-creating "
                    "the data the round is using"
                ),
                plan_dir=plan_dir,
            )
    return start_service(
        decl, project_dir,
        preserve_data=True, plan_dir=Path(plan_dir),
    )


def _data_plane_detail(evidence: Optional[DataPlaneEvidence]) -> str:
    """One line about the data plane, for a service that did not come up.

    A service that answered and served nothing is a different failure
    from a service that never answered, and the operator has to be able
    to tell them apart without opening the log.

    Only an instance that **answered** about its data plane gets a line
    here.  One that could not be reached at all has nothing to say, and
    its absence of an answer is not the reason the service failed.
    """
    if evidence is None or evidence.verdict in (
        DATA_PLANE_PROVED, DATA_PLANE_ABSENT, DATA_PLANE_UNREACHABLE,
    ):
        return ""
    return (
        f"; the data plane was not proved ({evidence.verdict}: "
        f"{evidence.error or 'no plan_routing rows'})"
    )


def _tail(path: Path, lines: int) -> str:
    try:
        content = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(content[-lines:])


def _head_lines(path: Path, limit: int) -> List[str]:
    """The first *limit* lines of *path* (``[]`` when it cannot be read).

    Stops at *limit* instead of reading the whole file: this runs on
    the failure path, where the log may have grown enormous while the
    service was failing.
    """
    lines: List[str] = []
    try:
        with open(path, "r", errors="replace") as fp:
            for line in fp:
                if len(lines) >= limit:
                    break
                lines.append(line.rstrip("\r\n"))
    except OSError:
        return []
    return lines


def _first_error_excerpt(
    lines: list[str], context: int = 5,
) -> Optional[tuple[int, str]]:
    """The first error line in *lines*, together with the lines before it.

    Returns ``(line_number, excerpt)``. ``line_number`` is 1-based —
    the number an operator sees when they open the log — and
    ``excerpt`` is the error line plus up to *context* lines that
    precede it, joined the way the tail is joined.

    ``None`` when no line carries an error marker.
    """
    for index, line in enumerate(lines):
        if not any(marker in line for marker in _ERROR_MARKERS):
            continue
        start = max(0, index - context)
        excerpt = " | ".join(lines[start:index + 1])
        return index + 1, excerpt
    return None


def _truncate(text: str, max_chars: int) -> str:
    """Cap *text* at *max_chars*, saying how much was dropped."""
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - 40)
    dropped = len(text) - keep
    marker = f"…[truncated {dropped} chars]"
    # The marker's own length depends on the number it reports, so
    # shrink the kept part until the two fit together.
    while keep > 0 and len(marker) > max_chars - keep:
        keep -= 1
        dropped = len(text) - keep
        marker = f"…[truncated {dropped} chars]"
    if len(marker) > max_chars:
        return text[:max_chars]
    return text[:keep] + marker


def _diagnosable_log(
    path: Path, tail_lines: int = 15, max_chars: int = _MAX_EXCERPT_CHARS,
) -> str:
    """Render a service log as something an operator can act on.

    The tail alone is not enough. A service that dies during startup
    often writes its traceback early and then floods the log with
    interpreter-shutdown noise, so the last fifteen lines show the
    noise and hide the cause — which is how the real failure (an
    ``OSError`` nine lines in) went unreported.

    So the excerpt leads with the first error line and the five lines
    before it, and keeps the tail after it. When the first error is
    already inside the tail window the tail carries it, and the error
    is not said twice.

    The result is capped at *max_chars*; whatever was dropped is
    reported as ``…[truncated N chars]``. A missing or empty log
    gives ``"(empty)"``.
    """
    head = _head_lines(path, _MAX_ERROR_SCAN_LINES)
    tail = _tail(path, tail_lines)
    if not tail and not any(line.strip() for line in head):
        return "(empty)"

    parts: List[str] = []
    found = _first_error_excerpt(head, _ERROR_CONTEXT_LINES)
    if found is not None:
        line_number, excerpt = found
        error_line = head[line_number - 1].strip()
        # The tail already carries the error — don't say it twice.
        if not error_line or error_line not in tail:
            parts.append(f"first error at line {line_number}: {excerpt}")
    parts.append(f"log tail: {tail or '(empty)'}")
    return _truncate(" | ".join(parts), max_chars)

def _fd_count(pid: int) -> Optional[int]:
    """How many descriptors *pid* holds open, or ``None``.

    ``None`` means "not measurable here", not "zero": ``lsof`` may not
    be installed, may not be permitted to look at that process, or may
    have failed. A caller that turned that into ``0`` would report a
    descriptor exhaustion that was never observed.

    The count is the number of **numbered** descriptors. ``lsof`` also
    reports the current working directory, the executable and the root
    as ``fcwd`` / ``ftxt`` / ``frtd``, which are not descriptors and
    would inflate the count by three.
    """
    try:
        completed = subprocess.run(
            [_LSOF_BIN, "-p", str(pid), "-Fn"],
            capture_output=True, text=True, check=False,
            timeout=_FD_COUNT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a probe is best-effort
        # ``lsof`` may be missing, the process may be gone, the call
        # may have been refused.  None of that may break the failure
        # path this probe is there to explain.
        logger.debug(
            "[service_manager] fd count for %s failed: %s", pid, exc,
        )
        return None
    if completed.returncode != 0:
        return None
    count = 0
    for line in completed.stdout.splitlines():
        field = line.strip()
        if field.startswith("f") and field[1:].isdigit():
            count += 1
    return count or None

def _failure_detail(
    *,
    pid: int,
    pgid: Optional[int],
    fds: Optional[int],
    timeout_seconds: int,
    rc: Optional[int],
    log_file: Path,
) -> str:
    """The one-line diagnosis of a service that did not come up.

    The shape is fixed, because the reader of this line is a human
    with the log open in another window::

        started (pid 24057, pgid 24057, fds 37) but not ready after 8s;
        rc=None; first error at line 9: ...; log tail: ...

    *pid* / *pgid* say which process to look at; *fds* says how many
    descriptors it held when it failed, and is **omitted** when the
    count could not be taken; *rc* is the exit code when the child has
    already exited and ``None`` while it is still running.

    The log half is :func:`_diagnosable_log` and not a second copy of
    it: the first error line, the five lines before it, then the tail,
    capped at ``_MAX_EXCERPT_CHARS``. Two implementations of "where did
    it first go wrong" would drift apart, and the one that drifted
    would be whichever the reader did not have open.
    """
    fd_part = f", fds {fds}" if fds is not None else ""
    return (
        f"started (pid {pid}, pgid {pgid}{fd_part}) but not ready after "
        f"{timeout_seconds}s; rc={rc}; "
        f"{_diagnosable_log(log_file, tail_lines=15)}"
    )




# ---------------------------------------------------------------------------
# Ownership of the process that holds a declared port (2026-10-25)
# ---------------------------------------------------------------------------
#
# Rule 3 of the module docstring: a leftover of our own is ours to stop.
# "Our own" is not a guess — it is answered by evidence, in this order:
#
#   * the **ledger** of a plan in this workspace says we started that
#     exact pid on that exact port;
#   * failing that, the holder runs *our* program out of *our* workspace.
#
# A ledger that names the holder but belongs to another workspace is a
# positive statement that the holder is *not* ours, so the workspace
# fallback does not apply to it.  And a service a **live** round still
# owns is in use, not a leftover — that round may have VPs running
# against it right now.


def _ledger_holder_evidence(
    plan_dir: Path, decl: ServiceDeclaration, probe: PortProbe,
) -> Optional[Dict[str, Any]]:
    """What a plan's ledger says about the holder of the declared port."""
    ledger = read_ledger(plan_dir)
    entries = ledger.get("services")
    if not isinstance(entries, list):
        return None
    live_pids = {int(p) for p in (probe.pids or []) if isinstance(p, int)}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ports: set = set()
        for key in ("port", "declared_port"):
            try:
                ports.add(int(entry.get(key)))
            except (TypeError, ValueError):
                continue
        if decl.port not in ports:
            continue
        try:
            pid = int(entry.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid not in live_pids:
            continue
        if _pid_reused(entry):
            # The number was recycled: the process the ledger recorded is
            # gone and somebody else holds the port now.
            continue
        return {
            "source": "ledger",
            "plan_dir": str(plan_dir),
            "plan_id": str(ledger.get("plan_id") or ""),
            "project_dir": str(ledger.get("project_dir") or ""),
            "pid": pid,
            "pgid": entry.get("pgid"),
            "cwd": str(entry.get("cwd") or ""),
            "start_cmd": str(entry.get("start_cmd") or ""),
            "owner_token": str(entry.get("owner_token") or ""),
        }
    return None


def _ledger_owner_is_live(plan_dir: Path, owner_token: str) -> bool:
    """Whether the round that started the holder is still running."""
    if not owner_token:
        return False
    try:
        return owner_token in set(live_service_owners(plan_dir))
    except Exception:  # noqa: BLE001 - a damaged registry is not a verdict
        return False


def _evidence_is_our_workspace(
    evidence: Dict[str, Any], project_dir: Path,
) -> bool:
    """Whether the evidence ties the holder to this workspace."""
    recorded = str(evidence.get("project_dir") or "")
    if recorded:
        try:
            if Path(recorded).resolve() != Path(project_dir).resolve():
                return False
        except OSError:
            return False
    cwd = str(evidence.get("cwd") or "")
    if cwd:
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            # The ledger records the service's cwd the way the
            # declaration spells it — relative to the project.
            cwd_path = Path(project_dir) / cwd_path
        if not _path_within(str(cwd_path), project_dir):
            return False
    return True


def _workspace_evidence(
    decl: ServiceDeclaration, project_dir: Path, probe: PortProbe,
) -> Optional[Dict[str, Any]]:
    """The holder runs our own program out of our own workspace.

    That is the shape of a leftover: the same program, started where we
    start ours, holding a port we declared.  It is weaker evidence than
    the ledger — no pid was recorded — so it is only used where the
    alternative is relocating the round away from the port it declared.
    """
    if not matches_service_program(decl, probe):
        return None
    pid: Optional[int] = None
    for candidate in (probe.pids or []):
        if isinstance(candidate, int):
            pid = candidate
            break
    if pid is None:
        return None
    cwd = _process_cwd(pid)
    if not cwd or not _path_within(cwd, project_dir):
        return None
    return {
        "source": "workspace",
        "pid": pid,
        "cwd": cwd,
        "project_dir": str(project_dir),
        "plan_id": "",
        "start_cmd": "",
        "owner_token": "",
    }


def _holder_is_pinned(
    plan_dir: Path, evidence: Dict[str, Any], decl: ServiceDeclaration,
) -> bool:
    """Whether a live round has pinned the holder of the declared port.

    The reclaim stops our own leftover to take the declared port back.
    A holder a live round is *using* is not a leftover — stopping it is
    how a running round loses its api server between two VPs (2026-10-25).

    The owner token cannot always tell: on 2026-10-24 the plan's api
    service was recorded with an empty token, so the ledger said "nobody
    claims this" and the round that was using it was invisible.  The
    pin names the process, so it is asked as well.
    """
    pid = evidence.get("pid")
    for pin in live_round_pins(plan_dir):
        if not _pin_is_in_use(pin, plan_dir):
            continue
        if pid and _pin_matches_process(pin, pid):
            return True
        pin_port = pin.get("port")
        if pin_port is not None:
            try:
                if int(pin_port) == int(decl.port):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _holder_evidence(
    decl: ServiceDeclaration,
    project_dir: Path,
    probe: PortProbe,
    *,
    plan_dir: Optional[Path] = None,
    plan_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Evidence that the holder of the declared port is ours — or ``None``.

    ``None`` means the holder is somebody else's, and the caller
    relocates around it exactly as it did before this rule existed.
    """
    candidates: List[Path] = []
    if plan_dir is not None:
        candidates.append(Path(plan_dir))
    else:
        tree = _plans_tree()
        try:
            candidates.extend(
                sorted(p.parent for p in tree.glob(f"*/{LEDGER_FILENAME}"))
            )
        except OSError:
            candidates = []
    ours: Optional[Dict[str, Any]] = None
    foreign = False
    for candidate in candidates:
        evidence = _ledger_holder_evidence(candidate, decl, probe)
        if evidence is None:
            continue
        if _ledger_owner_is_live(candidate, evidence["owner_token"]):
            # A round that is running right now owns that service — any
            # plan's round.  It is **in use**, not a leftover, and this
            # verdict outranks the weaker shape below: a running round's
            # service is never this round's to take, however much it
            # looks like our own leftover.
            return None
        if _holder_is_pinned(candidate, evidence, decl):
            # Same verdict, a stronger witness (2026-10-25): a live
            # round has **pinned** that process.  The pin names the
            # process itself, so it holds even where the owner token
            # was empty — which is what the 2026-10-24 sweep walked
            # straight into.
            return None
        if not _evidence_is_our_workspace(evidence, project_dir):
            # Another workspace's process: a positive "not ours".  It
            # outranks the fallback too — we do not touch what we
            # cannot claim.
            foreign = True
            continue
        if plan_id and evidence["plan_id"] and evidence["plan_id"] != plan_id:
            # Not this plan's ledger — it cannot speak for this plan.
            # It does speak for the workspace, though, so the search
            # continues.
            continue
        if ours is None:
            ours = evidence
    if foreign:
        return None
    if ours is not None:
        return ours
    return _workspace_evidence(decl, project_dir, probe)


def _reclaim_reason(decl: ServiceDeclaration, evidence: Dict[str, Any]) -> str:
    """Why the declared port was taken back — in one sentence."""
    source = str(evidence.get("source") or "")
    if source == "ledger":
        who = (
            f"plan {evidence.get('plan_id') or '?'}'s ledger records pid "
            f"{evidence.get('pid')} on port {decl.port}"
        )
    else:
        who = (
            f"pid {evidence.get('pid')} runs {decl.name}'s own program out "
            f"of {evidence.get('cwd')}"
        )
    theirs = start_cmd_env(str(evidence.get("start_cmd") or ""))
    ours = start_cmd_env(decl.start_cmd)
    planes: List[str] = []
    for key in ("PDT_PLANS_DIR", "PDT_STATE_DB_PATH"):
        their_value = theirs.get(key)
        our_value = ours.get(key)
        if their_value and our_value and their_value != our_value:
            planes.append(
                f"{key} was {their_value}, this round needs {our_value}"
            )
    reason = f"reclaimed the declared port {decl.port}: {who}"
    if planes:
        reason = f"{reason} ({'; '.join(planes)})"
    return reason


def _reclaim_leftover(
    decl: ServiceDeclaration,
    project_dir: Path,
    evidence: Dict[str, Any],
) -> ServiceRuntime:
    """Stop our own leftover and take the declared port back.

    The point is the **declared** port: this round's copy is started on
    it, so the effective declarations the VPs read stay valid.  A
    relocation would move our copy elsewhere while the declarations kept
    pointing here — the VPs would talk to the leftover, which cannot
    see this plan's fixture.
    """
    reason = _reclaim_reason(decl, evidence)
    try:
        pid = int(evidence.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pid = None
    runtime = ServiceRuntime(
        name=decl.name, port=decl.port, start_cmd=decl.start_cmd,
        cwd=decl.cwd, reap_on_exit=decl.reap_on_exit,
    )
    runtime.pid = pid
    try:
        runtime.pgid = (
            int(evidence["pgid"]) if evidence.get("pgid") is not None else None
        )
    except (TypeError, ValueError):
        runtime.pgid = None
    if runtime.pgid is None and pid is not None:
        runtime.pgid = _process_group_of(pid)
    stop_info = stop_service(
        runtime, None, grace_seconds=10.0,
        caller="service_manager._reclaim_leftover",
        reason=reason,
    )
    if not stop_info.get("port_free"):
        runtime.origin = "failed"
        runtime.reclaimed = True
        runtime.reclaim_reason = reason
        runtime.detail = (
            f"{reason}; but the port was not released: "
            f"{stop_info.get('detail')}"
        )
        return runtime
    fresh = start_service(decl, project_dir)
    fresh.declared_port = decl.port
    fresh.reclaimed = True
    fresh.reclaim_reason = reason
    fresh.detail = f"{reason}; {fresh.detail}"
    return fresh


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

#: Where the per-port start locks live, relative to the project the
#: service belongs to.  The lock is per **port**, not per plan: two
#: callers that want to bring up the same port are competing for the
#: same resource, whoever they are.
#:
#: Under ``.pdt/`` because that is where this repository keeps the
#: runtime state a checkout *produces* rather than ships — and a
#: workspace keeps its file locks under ``.pdt/locks/`` (see
#: :mod:`config_paths`).  ``.pdt/`` is gitignored, so a lock file is
#: never something a commit can pick up.
_PORT_LOCK_DIRNAME = ".pdt/service-locks"

#: How long a caller waits for the port lock before it gives up and
#: proceeds without it.  Bounded on purpose — an unbounded wait turns a
#: stuck lock into a stuck round, which is worse than the race the lock
#: exists to prevent.
_PORT_LOCK_TIMEOUT_SECONDS = 300.0


@contextlib.contextmanager
def _port_lock(project_dir: Path, port: int):
    """Serialise the callers that want to bring up *this* port.

    2026-10-24 (repair-r3-16).  ``ensure_services`` is called from the
    round-start preflight **and** from the per-VP recovery, and a round
    runs its VPs concurrently.  Two callers that both find the port free
    both start a service on it: the second one loses the race and exits
    with EADDRINUSE, so the round concludes that the service "cannot
    start" when it very well can.

    The lock is held across the probe **and** the start, so the second
    caller re-probes after the first one finished and finds the service
    the first one started — and adopts it (rule 2 of the ownership
    rules above) instead of starting a second copy on top of it.

    ``flock`` is held per open file description, so two threads of ONE
    process each opening their own descriptor serialise correctly —
    which is the case that matters here, because a round's VPs run on a
    thread pool.

    Never raises: a lock that cannot be created or taken is logged and
    the caller proceeds without it.  The failure mode of *no* lock is
    the one that already existed (two copies fighting over the port);
    the failure mode of a stuck lock would be a stuck round.
    """
    lock_dir = Path(project_dir) / _PORT_LOCK_DIRNAME
    lock_path = lock_dir / f"port-{port}.lock"
    fd = None
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.warning(
            "[service_manager] could not open the port lock %s: %s; "
            "proceeding without it", lock_path, exc,
        )
        yield
        return
    try:
        deadline = time.monotonic() + _PORT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "[service_manager] gave up waiting for the port "
                        "lock %s after %.0fs; proceeding without it",
                        lock_path, _PORT_LOCK_TIMEOUT_SECONDS,
                    )
                    break
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # noqa: BLE001 - unlocking a free lock is a no-op
            pass
        os.close(fd)


def ensure_services(
    project_dir: Path,
    declarations: Dict[str, ServiceDeclaration],
    *,
    round_number: int = 0,
    ignore_freshness: bool = False,
    plan_dir: Optional[Path] = None,
    plan_id: str = "",
) -> ServiceRuntimeMap:
    """Bring every declared service to a live, ready state.

    Order per service:

      1. Port free → start it with the declared ``start_cmd``.
      2. Port held by a process matching the declaration → adopt it,
         unless it predates the newest source edit (then restart it
         with the declared command, not the captured one).
      3. Port held by *our own* leftover → reclaim the declared port:
         stop the leftover, start this round's copy on the declared
         port, and record why.
      4. Port held by anything else → **refuse**: no kill, service
         reported unready with a reason, its VPs get BLOCKED.

    ``plan_dir`` / ``plan_id`` are what let the round recognise its own
    leftovers: the plan's ledger is the record of the processes this
    plan started.  Without them the ledgers of the plans tree are
    searched instead, so a caller that does not know its plan still
    gets the leftover recognised.

    One service at a time, and **one caller at a time per port**: the
    probe and the start happen under the port's lock, so a second
    caller re-probes instead of racing the first one onto the same
    port.  See :func:`_port_lock` for why that matters.

    Never raises; a failure is a per-service ``ready=False``.
    """
    # The round that is starting the services right now (2026-10-25,
    # repair-r1-25): the address events are stamped with its token and
    # written into its plan directory.  It is ambient context rather
    # than a parameter, because the code that *picks* the address is
    # called from several places whose signatures other code relies on.
    round_token = (
        current_service_owner(Path(plan_dir)) if plan_dir is not None else ""
    )
    with address_event_context(
        plan_dir, run_id=plan_id, round_token=round_token,
    ):
        return _ensure_services(
            project_dir, declarations,
            round_number=round_number,
            ignore_freshness=ignore_freshness,
            plan_dir=plan_dir, plan_id=plan_id,
        )


def _ensure_services(
    project_dir: Path,
    declarations: Dict[str, ServiceDeclaration],
    *,
    round_number: int = 0,
    ignore_freshness: bool = False,
    plan_dir: Optional[Path] = None,
    plan_id: str = "",
) -> ServiceRuntimeMap:
    """The body of :func:`ensure_services`, run inside its context."""
    start = time.monotonic()
    result = ServiceRuntimeMap()
    project_dir = Path(project_dir)
    if not declarations:
        result.duration_seconds = time.monotonic() - start
        return result

    round_token = str(
        (_ADDRESS_EVENT_CONTEXT.get() or {}).get("round_token") or ""
    )

    newest_source = newest_project_source_mtime(project_dir)

    for name in sorted(declarations):
        decl = declarations[name]
        runtime = ServiceRuntime(
            name=name, port=decl.port, start_cmd=decl.start_cmd,
            cwd=decl.cwd, reap_on_exit=decl.reap_on_exit,
        )
        if decl.port in PROTECTED_PORTS:
            # Belt-and-braces: the declaration parser already rejects
            # these (they are the backend runtime's own control plane, and
            # the 2026-09-07 incident killed the backend on :8000 this
            # way), but every other function in this area re-checks, so
            # a future caller that bypasses the parser cannot get past
            # here either.
            runtime.detail = (
                f"port {decl.port} is an backend-runtime port; refusing to "
                f"manage service {name!r}"
            )
            result.runtimes[name] = runtime
            continue
        # 2026-10-24 (repair-r3-16, from main): the probe AND the start
        # happen under the port's lock, so a second caller re-probes
        # instead of racing the first one onto the same port.  The
        # branch's own rules (adopt / reclaim our leftover / relocate a
        # foreign holder) run inside that lock, so they see the state
        # the lock leaves behind.
        with _port_lock(project_dir, decl.port):
            try:
                probe = probe_port(decl.port)
            except Exception as exc:  # noqa: BLE001 - probe never raises
                runtime.detail = f"probe failed: {exc}"
                result.runtimes[name] = runtime
                continue

            if probe.listening:
                if not matches_declaration(decl, probe):
                    # Rule 3 first: is the holder a leftover of our own?  If
                    # it is, the declared port comes back to us and the
                    # effective declarations stay valid.
                    evidence = _holder_evidence(
                        decl, project_dir, probe,
                        plan_dir=plan_dir, plan_id=plan_id,
                    )
                    if evidence is not None:
                        runtime = _reclaim_leftover(decl, project_dir, evidence)
                        result.runtimes[name] = runtime
                        continue
                    # Rule 4: the port is held by something we cannot identify,
                    # so we neither adopt it nor kill it — **we get out of the
                    # way**. Start our own copy on a free port and point every
                    # VP at that. (2026-09-18 decision: a process whose
                    # cmdline matches the declaration is ours to kill; anything
                    # else is unknown and must not be touched — the round
                    # gets out of its way instead.)
                    foreign = '; '.join(probe.cmdlines) or 'unknown'
                    runtime = _relocate_service(
                        decl, project_dir, foreign_cmdline=foreign,
                    )
                    result.runtimes[name] = runtime
                    continue

                stale = (
                    not ignore_freshness
                    and newest_source is not None
                    and probe.process_start_epoch is not None
                    and probe.process_start_epoch < newest_source
                )
                if not stale:
                    runtime.origin = "adopted"
                    runtime.ready = True
                    runtime.pid = probe.pids[0] if probe.pids else None
                    runtime.start_epoch = probe.process_start_epoch
                    runtime.pgid = (
                        _process_group_of(runtime.pid) if runtime.pid else None
                    )
                    runtime.detail = "adopted a live, fresh, matching listener"
                    # The address was not allocated by this round — it was
                    # already there.  Say which, so a reader can tell "we
                    # moved" from "we found it" (2026-10-25, repair-r1-25).
                    _emit_address_event(
                        runtime, plan_dir, "reused",
                        run_id=plan_id, round_token=round_token,
                        old_port=None, new_port=runtime.port,
                        declared_port=decl.port,
                        reason=(
                            f"adopted a live, fresh, matching listener already "
                            f"on port {runtime.port} for service {name!r}"
                        ),
                    )
                    result.runtimes[name] = runtime
                    continue

                # Matching but stale: restart it. Its cmdline matched the
                # declaration, so killing it is the identified-process case
                # the safety model allows.
                logger.info(
                    "[service_manager] restarting stale listener port=%s "
                    "pid=%s start_epoch=%s newest_source=%s",
                    decl.port, probe.pids[0] if probe.pids else None,
                    probe.process_start_epoch, newest_source,
                )
                runtime.origin = "ready_foreign"
                runtime.pid = probe.pids[0] if probe.pids else None
                runtime.pgid = (
                    _process_group_of(runtime.pid) if runtime.pid else None
                )
                stop_info = stop_service(
                    runtime, decl,
                    caller="service_manager._ensure_services",
                    reason=(
                        f"the listener on port {runtime.port} matches the "
                        f"declaration but is stale; it is restarted so the "
                        f"round serves the current source"
                    ),
                    round_token=round_token,
                    plan_dir=plan_dir,
                )
                if not stop_info["port_free"]:
                    runtime.ready = False
                    runtime.detail = (
                        f"stale matching listener could not be stopped: "
                        f"{stop_info['detail']}"
                    )
                    result.runtimes[name] = runtime
                    continue
                restarted = start_service(decl, project_dir)
                restarted.origin = "ready_foreign" if restarted.ready else "failed"
                restarted.detail = (
                    f"restarted stale listener (was pid {runtime.pid}); "
                    f"{restarted.detail}"
                )
                result.runtimes[name] = restarted
                continue

            # A restart **inside** a round must not re-create the round's
            # data (2026-10-26).  The port is free, so the service the
            # round started has died and the round is bringing it back —
            # and the command that comes with the declaration is also the
            # command that re-creates the fixture.  Running it here would
            # replace the data the round's VPs are reading with a fresh
            # copy, under their feet.
            #
            # A live round that owns the service is the signal that the
            # data is in use.  With no such round, this is a first start
            # and a full seed is what it needs.
            owner = _live_round_using_service(decl, project_dir, plan_dir)
            if owner is not None:
                runtime = restart_service_preserving_data(
                    _runtime_from_ledger_entry(owner.entry),
                    owner.plan_dir,
                    decl=decl,
                    project_dir=project_dir,
                )
            else:
                runtime = start_service(decl, project_dir)
            result.runtimes[name] = runtime

    # 2026-10-25: every runtime now carries the **data plane** it was
    # found on, so the round's log can answer "it came up — but did it
    # come up serving data?".  The services the round started were
    # already asked during the readiness wait; the ones it *adopted*
    # are asked here.
    #
    # 2026-10-27 (fact A): the gate is given the service's **own
    # declaration** as well, so an instance is judged by the database
    # this service said it would serve.  A runtime whose name is not in
    # ``declarations`` (a service that was renamed on the way in, say)
    # gets ``None`` — the judgement it had before.
    expected_rows = _expected_routing_rows()
    for name, runtime in result.runtimes.items():
        if runtime.data_plane is None:
            _record_data_plane(runtime)
        _apply_data_plane_gate(runtime, expected_rows, declarations.get(name))

    result.duration_seconds = time.monotonic() - start
    return result


def _record_data_plane(runtime: ServiceRuntime) -> ServiceRuntime:
    """Ask the instance on *runtime*'s port what data it is serving.

    The answer is recorded on the runtime so the round's log carries
    it: "the service came up" and "the service came up serving data"
    are two different facts, and only the second one is worth anything
    to a VP.  Never raises — a probe that fails is evidence, not an
    exception.
    """
    try:
        runtime.data_plane = data_plane_evidence(runtime.port)
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        logger.warning(
            "[service_manager] could not probe the data plane on port %s: %s",
            runtime.port, exc,
        )
    return runtime


def _apply_data_plane_gate(
    runtime: ServiceRuntime, expected_rows: Optional[int],
    decl: Optional[ServiceDeclaration] = None,
) -> None:
    """Refuse a runtime whose instance does not serve usable data.

    The readiness wait already applied this rule to the services the
    round **started**.  This is the same rule for the ones it
    **adopted** — a listener that was already there is exactly where
    the 2026-10-24 incident came from: the port answered 200 and the
    database behind it was empty, so every assertion that followed was
    graded against nothing.

    ``decl`` is the service's own declaration, when the caller has it
    (2026-10-27, fact A): an adopted instance that serves a database
    the declaration never named is serving somebody else's data, and
    the round's own count cannot tell that apart from a service that
    simply has nothing yet.

    A runtime that is not ready is left alone: it already carries a
    reason, and a better one.
    """
    if not runtime.ready or runtime.data_plane is None:
        return
    evidence = runtime.data_plane
    if _data_plane_is_acceptable(evidence, expected_rows, decl):
        return
    runtime.ready = False
    runtime.detail = (
        f"the instance on port {runtime.port} does not serve the round's "
        f"data ({evidence.verdict}: "
        f"{evidence.error or 'no plan_routing rows'})"
    )


# ---------------------------------------------------------------------------
# Owner registry — "which round started this?"
# ---------------------------------------------------------------------------
#
# The ledger answers "which processes did the backend start for this
# plan?". It did NOT answer "which *round* started them", and that gap
# is what let one round's exit kill the next round's services: two
# rounds of one plan share one plan directory, so they share one ledger
# file, so the older round's exit-time reap read the newer round's
# entries out of it and stopped them.
#
# Live (2026-10-24): the plan's api service was ready at 15:44:32 (pid
# 61127) and received SIGTERM at 15:45:19, while its own round's first
# api_test did not run until 15:48:43 — so ``/health`` was refused at
# the TCP layer. The same shape had happened the round before (pid
# 22593: started 14:41:58, killed 14:45:37).
#
# The registry is a file beside the ledger rather than a module-level
# set, because the ledger is written by the *project's* copy of this
# module (see ``verification_agent._load_service_manager``) and reaped
# by the harness' copy. Two module instances, one plan directory — the
# only state they share is the disk.


def new_owner_token(prefix: str = "run") -> str:
    """A token that identifies one *round* of service ownership.

    Unique per call. The pid is part of it so a registry left behind by
    a crashed backend can be recognised as stale (see
    :func:`live_service_owners`) — otherwise the startup sweep would
    treat the dead round's services as live and skip them forever.
    """
    return f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def owners_path(plan_dir: Path) -> Path:
    return Path(plan_dir) / OWNERS_FILENAME


def read_owners(plan_dir: Path) -> Dict[str, Any]:
    """Read the owner registry. Returns ``{}`` when absent or corrupt —
    a damaged registry must not block a reap sweep."""
    try:
        data = json.loads(owners_path(plan_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_owners(plan_dir: Path, payload: Dict[str, Any]) -> None:
    path = owners_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning(
            "[service_manager] could not write owner registry %s: %s",
            path, exc,
        )


def _owner_entries(plan_dir: Path) -> List[Dict[str, Any]]:
    owners = read_owners(plan_dir).get("owners")
    if not isinstance(owners, list):
        return []
    return [o for o in owners if isinstance(o, dict)]


def register_service_owner(
    plan_dir: Path,
    owner_token: str,
    *,
    current: bool = False,
    pid: Optional[int] = None,
) -> None:
    """Record that ``owner_token`` is running for this plan.

    ``current=True`` marks the round that is *starting services right
    now* — the one whose ledger entries are written with this token.
    Only the round that starts services may claim that role: a caller
    that merely reaps must not steal it, or the next recorded service
    would be stamped with a token its reaper already owns.
    """
    if not owner_token:
        return
    entries = [
        e for e in _owner_entries(plan_dir)
        if str(e.get("token") or "") != owner_token
    ]
    entries.append({
        "token": owner_token,
        "pid": int(pid if pid is not None else os.getpid()),
        "registered_at": datetime.now().isoformat(),
    })
    existing_current = read_owners(plan_dir).get("current_owner")
    _write_owners(plan_dir, {
        "owners": entries,
        "current_owner": owner_token if current else existing_current,
    })


def release_service_owner(plan_dir: Path, owner_token: str) -> None:
    """Deregister ``owner_token``.

    Its ledger entries become reapable by anyone: the round that owned
    them is over, and nobody else will stop them.
    """
    if not owner_token:
        return
    registry = read_owners(plan_dir)
    entries = [
        e for e in _owner_entries(plan_dir)
        if str(e.get("token") or "") != owner_token
    ]
    current = registry.get("current_owner")
    _write_owners(plan_dir, {
        "owners": entries,
        "current_owner": None if current == owner_token else current,
    })


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` is still a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it simply belongs to somebody else.
        return True
    except OSError:
        return False
    return True


def live_service_owners(plan_dir: Path) -> List[str]:
    """The owner tokens whose owner is still running.

    A registry left behind by a crashed backend names a pid that is
    gone; those tokens are NOT live, so the entries they own are
    reapable. That is what lets the startup sweep clean up after a
    crash instead of skipping the orphans forever.
    """
    live: List[str] = []
    for entry in _owner_entries(plan_dir):
        token = str(entry.get("token") or "")
        if not token:
            continue
        pid = entry.get("pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            continue
        live.append(token)
    return live


def current_service_owner(plan_dir: Path) -> str:
    """The token of the round that is starting services right now.

    ``""`` when there is none, or when the round that claimed it is
    gone. A service recorded without a token belongs to nobody, so
    whoever reaps first may stop it — see :func:`reap_services`, which
    holds those entries back while a round is running.
    """
    token = read_owners(plan_dir).get("current_owner")
    if not isinstance(token, str) or not token:
        return ""
    if token not in live_service_owners(plan_dir):
        return ""
    return token


def release_owners_of_process(
    plan_dir: Path, pid: Optional[int] = None,
) -> List[str]:
    """Deregister the owners this process registered for this plan.

    Returns the tokens that were released. This is the shutdown sweep's
    lever: the process doing the sweeping is going away, so the rounds
    it registered are over — their entries have to become reapable, or
    the backend would exit leaving the services it started holding
    their ports (the C3 promise: no backend-spawned process outlives
    the workflow that spawned it).

    Only the *given* process' owners are released. Another backend
    process running rounds against the same plans tree keeps its
    tokens, and its services stay alive.
    """
    target = int(pid if pid is not None else os.getpid())
    registry = read_owners(plan_dir)
    kept: List[Dict[str, Any]] = []
    released: List[str] = []
    for entry in _owner_entries(plan_dir):
        owner_pid = entry.get("pid")
        if isinstance(owner_pid, int) and owner_pid == target:
            released.append(str(entry.get("token") or ""))
        else:
            kept.append(entry)
    if not released:
        return []
    current = registry.get("current_owner")
    _write_owners(plan_dir, {
        "owners": kept,
        "current_owner": None if current in released else current,
    })
    return released


# ---------------------------------------------------------------------------
# Round pins — "this round is using these services right now"
# ---------------------------------------------------------------------------
#
# The ledger answers "which processes did the backend start for this
# plan?" and the owner registry answers "which *round* started them".
# Both answers are written **after** the services came up, and both are
# keyed on a token, so between them they still left a hole (2026-10-25):
# the plan's api service was started (pid 585, port 58396) and its ledger
# entry came out with ``owner_token: ""`` — the round that started it
# had not registered one — so the next sweep read the entry as an orphan
# and stopped the service three seconds after it became ready, with the
# round's first ``api_test`` still pending.
#
# A **pin** is the same fact written the other way round.  It names the
# processes themselves (pid, process group, boot id, port), it expires
# on its own, and it does not need an owner token to be *effective*: an
# unattributed pin still protects its process until it expires, which is
# exactly the case the owner token could not cover.
#
# The pin file is a sibling of the ledger and of the owner registry, so
# the two module instances (the project's copy of this module and the
# harness' one — see :func:`verification_agent._load_service_manager`)
# share it the same way: through the disk.
#
# The file's shape::
#
#     {
#       "updated_at": "2026-10-25T21:54:08",
#       "pins": [
#         {
#           "name": "api",
#           "pid": 585,
#           "pgid": 585,
#           "boot_id": "585@1750000000",
#           "port": 58396,
#           "declared_port": 18090,
#           "round_token": "run-123-abcdef",
#           "writer_pid": 4242,
#           "created_at": 1750000000.0,
#           "expires_at": 1750007200.0
#         }
#       ]
#     }
#
# ``writer_pid`` is the process that wrote the pin — the round's own
# process.  It is the witness that survives an empty ``round_token``:
# the process is alive, so the service it is using is in use.


def round_pins_path(plan_dir: Path) -> Path:
    """Where a plan's round pins live."""
    return Path(plan_dir) / ROUND_PINS_FILENAME


def read_round_pins(plan_dir: Path) -> List[Dict[str, Any]]:
    """Every pin this plan has, expired ones included.

    A damaged pin file is not a verdict: it reads as "no pins", so a
    corrupt file can never keep a service alive for good.
    """
    try:
        data = json.loads(
            round_pins_path(plan_dir).read_text(encoding="utf-8"),
        )
    except (OSError, json.JSONDecodeError):
        return []
    pins = data.get("pins") if isinstance(data, dict) else None
    if not isinstance(pins, list):
        return []
    return [pin for pin in pins if isinstance(pin, dict)]


def _write_pins(plan_dir: Path, pins: List[Dict[str, Any]]) -> None:
    """Write the pin file. Never raises — a pin that cannot be written
    costs the protection, not the round."""
    path = round_pins_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now().isoformat(),
                    "pins": list(pins),
                },
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning(
            "[service_manager] could not write round pins %s: %s", path, exc,
        )


def _pin_is_live(pin: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """Whether a pin has not expired yet.

    A pin without a readable expiry is **not** live: it cannot be
    trusted to end, and a pin that never ends would keep a service
    alive for good.
    """
    moment = time.time() if now is None else now
    try:
        expires_at = float(pin.get("expires_at"))
    except (TypeError, ValueError):
        return False
    return expires_at > moment


def live_round_pins(
    plan_dir: Path, *, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """The pins of this plan that have not expired."""
    moment = time.time() if now is None else now
    return [
        pin for pin in read_round_pins(plan_dir)
        if _pin_is_live(pin, now=moment)
    ]


def _pin_boot_id(pid: Any, start_epoch: Any = None) -> str:
    """A process identity that survives PID reuse.

    ``pid@start_epoch`` — the same shape the verification agent binds a
    service with.  A pin that carried only a pid would protect a
    stranger that inherited the number.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return ""
    epoch = start_epoch
    if epoch is None:
        epoch = _process_start_epoch(pid_int)
    try:
        return f"{pid_int}@{int(float(epoch))}"
    except (TypeError, ValueError):
        return str(pid_int)


def _runtimes_of(runtimes: Any) -> List[ServiceRuntime]:
    """The runtimes a pin call was given, whatever shape they came in."""
    if runtimes is None:
        return []
    if hasattr(runtimes, "runtimes"):
        runtimes = runtimes.runtimes
    if isinstance(runtimes, dict):
        candidates = list(runtimes.values())
    else:
        candidates = list(runtimes)
    return [r for r in candidates if r is not None]


def pin_services_for_round(
    plan_dir: Path,
    round_token: str,
    runtimes: Any,
    ttl_sec: float = DEFAULT_ROUND_PIN_TTL_SECONDS,
) -> Optional[Path]:
    """Pin the services this round is using, so that no reap stops them.

    Why a pin at all (2026-10-25)
    ---------------------------
    The 2026-10-24 round started the plan's api service (pid 585, port
    58396) and three seconds later a sweep stopped it: the ledger entry
    named the process but carried no owner token, so the sweep read the
    entry as an orphan.  The round's first ``api_test`` then found a
    refused connection.

    A pin says the same thing in a way that does not depend on a token:
    it names the processes themselves, and it expires on its own.  So an
    unattributed pin — one written by a round that never registered an
    owner — still protects the service until it expires.

    ``runtimes`` is a :class:`ServiceRuntimeMap` or any iterable of
    :class:`ServiceRuntime`.  Only the services that are **ready** are
    pinned: a service that failed to come up may have left a half-started
    process behind, and that process is an orphan to reap, not an
    instance in use.

    The pins of the other rounds are kept — the rounds of one plan share
    one plan directory — and the pins of *this* round are replaced, so a
    round that starts services again refreshes its own protection
    instead of stacking a second copy of it.

    Returns the path of the pin file, or ``None`` when there was nothing
    to pin.  Never raises: a pin that cannot be written costs the
    protection, not the round.
    """
    plan_dir = Path(plan_dir)
    token = str(round_token or "")
    now = time.time()
    try:
        ttl = float(ttl_sec)
    except (TypeError, ValueError):
        ttl = DEFAULT_ROUND_PIN_TTL_SECONDS
    expires_at = now + max(ttl, 0.0)

    pins: List[Dict[str, Any]] = []
    for runtime in _runtimes_of(runtimes):
        pid = getattr(runtime, "pid", None)
        if not pid or not getattr(runtime, "ready", False):
            continue
        port = getattr(runtime, "port", None)
        declared = getattr(runtime, "declared_port", None)
        pins.append({
            "name": str(getattr(runtime, "name", "") or ""),
            "pid": int(pid),
            "pgid": getattr(runtime, "pgid", None),
            "boot_id": _pin_boot_id(
                pid, getattr(runtime, "start_epoch", None),
            ),
            "port": int(port) if isinstance(port, int) else None,
            "declared_port": (
                int(declared) if isinstance(declared, int) else None
            ),
            "round_token": token,
            "writer_pid": os.getpid(),
            "created_at": now,
            "expires_at": expires_at,
        })
    if not pins:
        return None

    kept = [
        pin for pin in read_round_pins(plan_dir)
        if str(pin.get("round_token") or "") != token
        and _pin_is_live(pin, now=now)
    ]
    _write_pins(plan_dir, kept + pins)
    return round_pins_path(plan_dir)


def unpin_services_for_round(plan_dir: Path, round_token: str) -> int:
    """Drop the pins one round wrote.  Returns how many were dropped.

    The file is removed when the last pin goes, so a plan that is not
    running services leaves nothing behind.
    """
    plan_dir = Path(plan_dir)
    token = str(round_token or "")
    pins = read_round_pins(plan_dir)
    kept = [
        pin for pin in pins
        if str(pin.get("round_token") or "") != token
    ]
    if len(kept) == len(pins):
        return 0
    path = round_pins_path(plan_dir)
    if not kept:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "[service_manager] could not remove round pins %s: %s",
                path, exc,
            )
        return len(pins) - len(kept)
    _write_pins(plan_dir, kept)
    return len(pins) - len(kept)


def _pin_matches_process(pin: Dict[str, Any], pid: Any) -> bool:
    """Whether the pin names this exact process.

    The boot id has to agree as well: a pid alone is not an identity,
    because the number is recycled.  The comparison carries the same
    2-second tolerance the PID-reuse check uses — ``ps -o lstart=`` has
    1-second granularity, and the two readings can straddle a tick.
    """
    try:
        pin_pid = int(pin.get("pid"))
    except (TypeError, ValueError):
        return False
    try:
        wanted = int(pid)
    except (TypeError, ValueError):
        return False
    if pin_pid != wanted:
        return False
    boot_id = str(pin.get("boot_id") or "")
    if "@" not in boot_id:
        return True
    epoch = _process_start_epoch(pin_pid)
    if epoch is None:
        # The process is gone (or ``ps`` is unavailable) — not evidence
        # that the number was recycled.
        return True
    try:
        pinned_epoch = float(boot_id.split("@", 1)[1])
    except (TypeError, ValueError):
        return True
    return abs(float(epoch) - pinned_epoch) <= 2.0


def _pin_covers_entry(
    entry: Dict[str, Any], pins: List[Dict[str, Any]],
) -> Optional[int]:
    """The index of the pin that names the process this ledger entry
    describes, or ``None``.

    Three ways to match, in order of how much they prove: the pid, the
    process group, and the port.  The order is the point: a pin that
    names the entry's **process** is the one that speaks for it, and a
    pin that only shares the port is the weakest witness — it must not
    win over the pin that names the process.

    It matters because the rounds of one plan share one plan directory:
    the pin of the round before is still in the file, and it names a
    process of its own.  Matched by port first, it would shield the
    next round's service, and that service would outlive the round that
    started it.

    The port is compared with the port — the port the process actually
    serves — and not with the *declared* one: a round that had to
    relocate serves somewhere else, and the pin of the round that holds
    the declared port says nothing about it.
    """
    entry_pid = entry.get("pid")
    entry_pgid = entry.get("pgid")
    try:
        entry_port = int(entry.get("port"))
    except (TypeError, ValueError):
        entry_port = None

    for index, pin in enumerate(pins):
        if entry_pid and _pin_matches_process(pin, entry_pid):
            return index
    for index, pin in enumerate(pins):
        pin_pgid = pin.get("pgid")
        if entry_pgid and pin_pgid:
            try:
                if int(entry_pgid) == int(pin_pgid):
                    return index
            except (TypeError, ValueError):
                pass
    if entry_port is not None:
        for index, pin in enumerate(pins):
            pin_port = pin.get("port")
            if pin_port is None:
                continue
            try:
                if int(pin_port) == entry_port:
                    return index
            except (TypeError, ValueError):
                continue
    return None


def _pin_is_releasable(
    pin: Dict[str, Any],
    plan_dir: Path,
    caller_token: str = "",
) -> bool:
    """Whether this reap may act on a pinned process.

    Four cases, and only four:

      * the process the pin names is **gone**.  A pin protects a
        process; when that process is gone there is nothing left to
        protect, and a spent pin must not go on shielding whatever
        lands on the port next.
      * the reaper **is** the round that pinned the service — its own
        exit reap.  The round is over, so the pin goes with it.
      * the round that pinned the service is **gone**: its token is not
        among the live owners any more.  The pin is then a leftover of a
        finished (or crashed) round, and nothing else would ever
        release it.
      * the pin carries **no** round token, and the process that wrote
        it is dead.  An unattributed pin of a live process stays: that
        is the production case — the round was never attributed to
        anybody, and stopping its service is the 2026-10-25 defect.

    An unattributed pin of a *live* process is releasable by nobody: its
    round cannot be identified, so nobody may claim it.  It expires on
    its own instead.
    """
    pin_pid = pin.get("pid")
    if pin_pid is not None:
        try:
            if not _pid_alive(int(pin_pid)):
                return True
        except (TypeError, ValueError):
            pass
    token = str(pin.get("round_token") or "")
    if token:
        if caller_token and token == caller_token:
            return True
        try:
            live = set(live_service_owners(plan_dir))
        except Exception:  # noqa: BLE001 - a damaged registry is not a verdict
            live = set()
        return token not in live
    writer = pin.get("writer_pid")
    try:
        writer_pid = int(writer)
    except (TypeError, ValueError):
        return False
    return not _pid_alive(writer_pid)


def _pin_is_in_use(
    pin: Dict[str, Any], plan_dir: Path, caller_token: str = "",
) -> bool:
    """Whether this pin still protects its process."""
    return not _pin_is_releasable(pin, plan_dir, caller_token)


def _release_pins(
    plan_dir: Path,
    pins: List[Dict[str, Any]],
    released: set,
    caller_token: str = "",
) -> None:
    """Drop the pins this reap may drop.

    A pin goes when the process it names is being stopped, and when the
    round that wrote it is over — the round's own exit reap drops all
    of that round's pins, so a finished round leaves nothing behind.
    """
    keep: List[Dict[str, Any]] = []
    changed = False
    for index, pin in enumerate(pins):
        if index in released:
            changed = True
            continue
        if caller_token and str(pin.get("round_token") or "") == caller_token:
            changed = True
            continue
        keep.append(pin)
    if not changed:
        return
    if not keep:
        # Nothing is pinned any more — the file goes with it, so a plan
        # that is not running services leaves nothing behind.
        try:
            round_pins_path(plan_dir).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "[service_manager] could not remove round pins %s: %s",
                round_pins_path(plan_dir), exc,
            )
        return
    _write_pins(plan_dir, keep)


# ---------------------------------------------------------------------------
# Address events — "how did this address come about?"
# ---------------------------------------------------------------------------
#
# The ledger records the address a service ended up on.  It does **not**
# record how it got there, and on 2026-10-24 that gap cost a whole
# round: the request went to port 51725, and neither the verification
# log nor the server log said where that number came from.  The declared
# port, the port the round was relocated to and the port it finally used
# each existed for a while, and none of them left a trace — so there was
# no way to tell who rewrote the address and when.
#
# So every address decision is written down as it happens, in one file
# per plan: ``logs/service_address_events.jsonl``.  One JSON object per
# line, appended.  The whole chain under one run is one file read.
#
# Two rules make the file worth reading:
#
#   * **The event is written before the state it describes.**  The
#     ``bound`` event goes to disk before the ledger names the address,
#     so a reader never finds an address in the ledger that the event
#     stream does not explain.
#   * **A broken log never costs a round.**  A failed write is one line
#     on stderr; the round carries on.


def address_events_path(plan_dir: Path) -> Path:
    """Where a plan's address event stream lives."""
    return Path(plan_dir) / ADDRESS_EVENTS_DIRNAME / ADDRESS_EVENTS_FILENAME


#: The round that is starting services right now (2026-10-25,
#: repair-r1-25).  A context variable rather than a parameter: the
#: address events are written by the code that *picks* the address, and
#: that code is reached from several places whose signatures other code
#: (and the tests) rely on.  The context is also per-thread, so two
#: rounds of one plan cannot write into each other's stream.
_ADDRESS_EVENT_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "pdt_service_address_event_context", default=None,
)


@contextmanager
def address_event_context(
    plan_dir: Optional[Path],
    *,
    run_id: str = "",
    round_token: str = "",
    declared_port: Optional[int] = None,
):
    """The round that is starting services right now, as ambient context.

    Yields the context it installed.  ``declared_port`` is the port the
    plan asked for, when it differs from the one the service was
    actually started on — a relocation is the case where it does.
    """
    ctx: Dict[str, Any] = {
        "plan_dir": plan_dir,
        "run_id": str(run_id or ""),
        "round_token": str(round_token or ""),
        "declared_port": declared_port,
    }
    token = _ADDRESS_EVENT_CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _ADDRESS_EVENT_CONTEXT.reset(token)


def record_address_event(
    plan_dir: Path,
    event: str,
    *,
    run_id: str = "",
    round_token: str = "",
    old_port: Optional[int] = None,
    new_port: Optional[int] = None,
    declared_port: Optional[int] = None,
    pid: Optional[int] = None,
    boot_id: str = "",
    reason: str = "",
) -> bool:
    """Append one address event to the plan's stream.

    Never raises.  A failed write costs one line on stderr and returns
    ``False`` — a round must not die because its log could not be
    written.
    """
    payload: Dict[str, Any] = {
        "ts": datetime.now().isoformat(),
        "run_id": str(run_id or ""),
        "round_token": str(round_token or ""),
        "event": str(event or ""),
        "old_port": old_port,
        "new_port": new_port,
        "declared_port": declared_port,
        "pid": pid,
        "boot_id": str(boot_id or ""),
        "reason": str(reason or ""),
    }
    path = address_events_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        print(
            "[service_manager] could not write address event "
            f"{event!r} to {path}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def read_address_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """Read a plan's address event stream.

    A line that is not a JSON object is skipped rather than fatal: one
    torn line must not hide the events around it.
    """
    path = address_events_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def boot_id_of(pid: Any, start_epoch: Any = None) -> str:
    """A process identity that survives PID reuse (``pid@start_epoch``).

    The public face of :func:`_pin_boot_id`, for callers outside this
    module that have to name a process in the address stream.
    """
    return _pin_boot_id(pid, start_epoch)


def _emit_address_event(
    runtime: Optional[ServiceRuntime],
    plan_dir: Optional[Path],
    event: str,
    *,
    run_id: str = "",
    round_token: str = "",
    old_port: Optional[int] = None,
    new_port: Optional[int] = None,
    declared_port: Optional[int] = None,
    pid: Optional[int] = None,
    boot_id: str = "",
    reason: str = "",
) -> bool:
    """Write one event, and remember that this runtime wrote it.

    The runtime's own set is what keeps the stream free of duplicates:
    the same address is written by the code that allocated it and — when
    the ensure step did not know the plan directory — by the code that
    records the ledger.  The second writer sees the first one's mark
    and stays quiet.

    A runtime is marked only when the write actually happened, so a
    failed write can be retried by whoever writes next.
    """
    if plan_dir is None:
        return False
    if runtime is not None:
        if event in runtime.address_events:
            return True
        if pid is None:
            pid = runtime.pid
        if not boot_id:
            boot_id = _pin_boot_id(runtime.pid, runtime.start_epoch)
    ok = record_address_event(
        plan_dir, event,
        run_id=run_id, round_token=round_token,
        old_port=old_port, new_port=new_port, declared_port=declared_port,
        pid=pid, boot_id=boot_id, reason=reason,
    )
    if ok and runtime is not None:
        runtime.address_events.add(event)
    return ok


def _record_round_address_events(
    plan_dir: Path,
    run_id: str,
    round_token: str,
    runtime_map: ServiceRuntimeMap,
) -> None:
    """Write down every address decision this round made.

    Called **before** the ledger is written: the event stream is the
    record of how the address came about, the ledger is the state that
    came out of it, and the state must never be ahead of the record.

    The events the ensure step already wrote (when it knew the plan
    directory) are not written a second time.
    """
    for name in sorted(runtime_map.runtimes):
        runtime = runtime_map.runtimes[name]
        if runtime is None:
            continue
        port = runtime.port
        declared = (
            runtime.declared_port
            if runtime.declared_port is not None else port
        )
        moved = (
            runtime.declared_port is not None
            and runtime.declared_port != port
        )
        # 2026-10-25 (repair-r1-29): every port the relocation tried is
        # recorded — including the ones that did not work.  A
        # relocation that gave up has no pid and no allocation of its
        # own, so this is the only place its chain exists; without it
        # the candidate ports are the ones nobody can account for.
        for candidate in getattr(runtime, "candidate_ports", None) or []:
            key = f"relocate_attempt:{int(candidate)}"
            if key in runtime.address_events:
                continue
            ok = record_address_event(
                plan_dir, "relocate_attempt",
                run_id=run_id, round_token=round_token,
                old_port=None, new_port=int(candidate),
                declared_port=declared,
                pid=None, boot_id="",
                reason=(
                    f"candidate port {candidate} tried for service {name!r} "
                    f"after the declared port {declared} turned out to be "
                    f"held by a process that is not this service"
                ),
            )
            if ok:
                runtime.address_events.add(key)
        if runtime.origin == "adopted":
            _emit_address_event(
                runtime, plan_dir, "reused",
                run_id=run_id, round_token=round_token,
                old_port=None, new_port=port, declared_port=declared,
                reason=(
                    f"adopted a live listener already on port {port} "
                    f"for service {name!r}"
                ),
            )
            continue
        if not runtime.pid:
            # Nothing was started on this address — a service that
            # refused to start has taken no port, and there is no
            # allocation to record.
            continue
        _emit_address_event(
            runtime, plan_dir, "allocated",
            run_id=run_id, round_token=round_token,
            old_port=None, new_port=port, declared_port=declared,
            reason=f"started service {name!r} on port {port}",
        )
        if moved:
            _emit_address_event(
                runtime, plan_dir, "relocated",
                run_id=run_id, round_token=round_token,
                old_port=runtime.declared_port, new_port=port,
                declared_port=declared,
                reason=(
                    f"service {name!r} was moved off the declared port "
                    f"{runtime.declared_port}"
                ),
            )

    # The bound address is the one the ledger is about to name.
    for name in sorted(runtime_map.runtimes):
        runtime = runtime_map.runtimes[name]
        if runtime is None:
            continue
        if not (runtime.spawned_by_ac and runtime.pid):
            continue
        declared = (
            runtime.declared_port
            if runtime.declared_port is not None else runtime.port
        )
        _emit_address_event(
            runtime, plan_dir, "bound",
            run_id=run_id, round_token=round_token,
            old_port=None, new_port=runtime.port, declared_port=declared,
            reason=(
                f"service {name!r} is bound to port {runtime.port} "
                f"for this round"
            ),
        )


# ---------------------------------------------------------------------------
# Process events — the watcher that notices a service is gone
# ---------------------------------------------------------------------------
#
# The address stream above answers "where did this port come from".
# This stream answers a different question: "is the process behind it
# still there?".
#
# Why it had to be added (2026-10-26, repair-r1-47)
# -----------------------------------------------
# The 2026-10-26 round brought its api service up at 02:21:49 and
# recorded it as ready.  Two seconds later the service logged
# ``shutdown complete`` and was gone.  **Nothing in the round recorded
# it.**  The first thing that noticed was a VP at 02:25:01 finding the
# port refused — two minutes and forty-four seconds after the service
# had actually died.  The round before had the same shape (21:44 and
# 21:54 on 2026-10-24), and the round before that one as well: three
# rounds in a row, and not one event anywhere.
#
# The gap was structural, not accidental.  Nothing in this module
# watched the process it had started.  Readiness was a question asked
# **once**, at start-up; after that the runtime was a record of a past
# observation, and it kept saying "ready" long after the process it
# described had exited.  A round that cannot see the death cannot
# report it, cannot explain it, and cannot ask for the service to be
# brought back.
#
# What the watcher does
# ---------------------
# One light thread per spawned service polls the child process, and
# when the child is gone the round:
#
#   * writes a ``service_process_exited`` event carrying the facts
#     that explain the death (pid, port, return code, signal, how long
#     it lived, which round it belonged to);
#   * marks the runtime **dead**, so it stops being an address the
#     moment it stops being one.
#
# What the watcher deliberately does **not** do is keep the service
# alive.  Nothing here can: the process was killed from outside, and
# the honest answer to "the service died" is "the service died", not a
# promise this module cannot keep.
#
# The watcher ends with the process it watches — one thread per
# service, alive exactly as long as the service is, never a resident
# of the backend.


def process_events_path(plan_dir: Path) -> Path:
    """Where a plan's process event stream lives."""
    return Path(plan_dir) / ADDRESS_EVENTS_DIRNAME / PROCESS_EVENTS_FILENAME


def record_process_event(
    plan_dir: Path,
    event: str,
    *,
    pid: Optional[int] = None,
    port: Optional[int] = None,
    returncode: Optional[int] = None,
    exit_signal: Optional[int] = None,
    started_at: float = 0.0,
    exited_at: float = 0.0,
    uptime_sec: float = 0.0,
    round_token: str = "",
    reason: str = "",
) -> bool:
    """Append one process event to the plan's stream.

    Never raises.  A failed write costs one line on stderr and returns
    ``False`` — a round must not die because its log could not be
    written.  The watcher is called from a thread nobody is waiting
    for, so an exception here would be lost, not reported.
    """
    payload: Dict[str, Any] = {
        "ts": datetime.now().isoformat(),
        "event": str(event or ""),
        "pid": pid,
        "port": port,
        "returncode": returncode,
        "exit_signal": exit_signal,
        "started_at": float(started_at or 0.0),
        "exited_at": float(exited_at or 0.0),
        "uptime_sec": round(float(uptime_sec or 0.0), 3),
        "round_token": str(round_token or ""),
        "reason": str(reason or ""),
    }
    path = process_events_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        print(
            "[service_manager] could not write process event "
            f"{event!r} to {path}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def read_process_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """Read a plan's process event stream.

    A line that is not a JSON object is skipped rather than fatal: one
    torn line must not hide the events around it.
    """
    path = process_events_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def exit_signal_of(returncode: Optional[int]) -> Optional[int]:
    """The signal that killed a process, in the positive form.

    ``None`` when the process was not killed by a signal.

    Two conventions are in play, and a service started through
    ``bash -lc`` meets both:

      * the process itself died from the signal — the operating system
        reports a **negative** status, ``-15`` for SIGTERM;
      * the shell that was in charge reported the death the way shells
        report it — as the exit code ``128 + signal``, so ``143`` for
        SIGTERM.

    A reader asking "which signal" should not have to know which of
    the two the shell in front of the service happened to be, so both
    are read as the signal they mean.  The ambiguity is inherent (a
    program that exits with 143 on purpose looks the same), and the
    cost of reading it the other way is a death the log cannot
    explain.
    """
    if returncode is None:
        return None
    try:
        code = int(returncode)
    except (TypeError, ValueError):
        return None
    if code < 0:
        return -code
    if 128 < code < 256:
        return code - 128
    return None


def _describe_exit(returncode: Optional[int], stopped: bool) -> str:
    """One line saying what the exit was, in words."""
    if stopped:
        return "stopped by the round"
    signal_number = exit_signal_of(returncode)
    if signal_number is not None:
        return f"killed by signal {signal_number}"
    if returncode is None:
        return "the process is gone"
    if returncode == 0:
        return "exited with code 0"
    return f"exited with code {returncode}"


def _watch_service_process(
    runtime: ServiceRuntime,
    proc: subprocess.Popen,
    *,
    plan_dir: Optional[Path],
    round_token: str,
    poll_interval: float = _PROCESS_WATCH_INTERVAL,
) -> None:
    """Poll *proc* until it exits, then say how it died.

    Runs in its own thread (see :func:`_start_process_watcher`) and
    never raises: a watcher that crashed would take the observation
    with it, and an observation that cannot be recorded is the thing
    this watcher exists to prevent.

    The exit is recorded **and** applied to the runtime.  The recording
    is what a reader of the round's log sees; the application is what
    stops the dead runtime from being handed out as an address.
    """
    while True:
        try:
            code = proc.poll()
        except Exception:  # noqa: BLE001 - a poll never raises in practice
            code = None
        if code is not None:
            break
        time.sleep(poll_interval)

    exited_at = time.time()
    started_at = float(runtime.start_epoch or exited_at)
    exit_signal = exit_signal_of(code)
    exit_info = ProcessExit(
        pid=runtime.pid,
        port=runtime.port,
        returncode=code,
        exit_signal=exit_signal,
        started_at=started_at,
        exited_at=exited_at,
        uptime_sec=max(0.0, exited_at - started_at),
        round_token=round_token,
        reason=_describe_exit(code, runtime.stopped),
    )
    # The runtime is the state machine: the moment the process is gone,
    # the runtime stops saying the service is ready.  Nothing else has
    # to be told — every reader of the runtime sees the same fact.
    runtime.exit = exit_info
    runtime.ready = False
    if not runtime.failure_reason:
        runtime.failure_reason = exit_info.reason
    if plan_dir is not None:
        record_process_event(
            plan_dir, PROCESS_EVENT_EXITED,
            pid=exit_info.pid, port=exit_info.port,
            returncode=exit_info.returncode, exit_signal=exit_info.exit_signal,
            started_at=exit_info.started_at, exited_at=exit_info.exited_at,
            uptime_sec=exit_info.uptime_sec, round_token=exit_info.round_token,
            reason=exit_info.reason,
        )


def _start_process_watcher(
    runtime: ServiceRuntime,
    proc: subprocess.Popen,
    *,
    plan_dir: Optional[Path],
    round_token: str,
) -> Optional[threading.Thread]:
    """Start the thread that watches *proc* for *runtime*.

    The thread is a daemon and it ends with the process it watches, so
    a round that starts twenty services leaves twenty short-lived
    threads behind — not twenty permanent residents of the backend.

    ``None`` when there is nothing to watch: a handle that is not a
    real child process (see :data:`_REAL_POPEN`) has no process behind
    it, and a thread polling it would never end.
    """
    if not isinstance(proc, _REAL_POPEN):
        return None
    thread = threading.Thread(
        target=_watch_service_process,
        args=(runtime, proc),
        kwargs={
            "plan_dir": plan_dir,
            "round_token": round_token,
        },
        name=f"pdt-service-watch-{runtime.name}-{runtime.pid}",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError as exc:  # pragma: no cover - defensive
        logger.warning(
            "[service_manager] could not start the watcher for %s: %s",
            runtime.name, exc,
        )
        return None
    return thread


# ---------------------------------------------------------------------------
# Ledger (written here, acted upon by C3)
# ---------------------------------------------------------------------------


def ledger_path(plan_dir: Path) -> Path:
    return Path(plan_dir) / LEDGER_FILENAME


def record_runtimes(
    plan_dir: Path,
    plan_id: str,
    runtime_map: ServiceRuntimeMap,
    *,
    project_dir: Path,
    round_number: int = 0,
    owner_token: str = "",
) -> Optional[Path]:
    """Persist who this round started, so it can be reaped later.

    Only services **this process spawned** are recorded. Adopted
    listeners are deliberately excluded: they were running before AC
    touched them, so the backend must not claim ownership of them and C3 must
    not kill them on exit.

    Every entry carries the ``owner_token`` of the round that started
    it (2026-10-24), so a reap can tell "mine" from "a round that is
    still running". When no token is passed, the round that registered
    itself as the current owner of this plan is used — the ledger is
    written by the *project's* copy of this module
    (:func:`verification_agent._load_service_manager`), so the token
    cannot be threaded through every caller; the registry file beside
    the ledger is what the two module instances share.

    Entries another round owns are kept: that owner reaps them when it
    exits. Dropping them here would leave those processes holding their
    ports with nothing left in the ledger to stop them.

    The round also **pins** the services it started (2026-10-25,
    repair-r1-23).  The ledger says *which* processes we started; the
    pin says *that this round is using them right now*, and it does not
    depend on the owner token — which is what was empty on 2026-10-24,
    when a sweep read the plan's api service (pid 585) as an orphan and
    stopped it three seconds after it came up.  The pin is written
    here, right after the ledger, because this is the one place that
    runs immediately after the round's ``ensure`` succeeded and knows
    the plan directory.

    Never raises — a ledger write failure costs the exit-time reap of
    one round, which is strictly better than aborting the round.
    """
    spawned = [
        r for r in runtime_map.runtimes.values()
        if r.spawned_by_ac and r.pid
    ]
    token = owner_token or current_service_owner(plan_dir)
    for runtime in spawned:
        runtime.owner_token = token
    # The address record goes down **before** the ledger (2026-10-25,
    # repair-r1-25).  The ledger says which address the round ended up
    # on; the event stream says how it got there, and the state must
    # never be ahead of the record.  This is also the one place that
    # knows the plan directory in production — the ensure step runs
    # without it — so it is what makes the chain complete there.
    _record_round_address_events(
        plan_dir, plan_id or Path(plan_dir).name, token, runtime_map,
    )
    if not spawned:
        return None
    kept: List[Dict[str, Any]] = []
    existing = read_ledger(plan_dir).get("services")
    if isinstance(existing, list):
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            entry_owner = str(entry.get("owner_token") or "")
            if entry_owner and entry_owner == token:
                # This round's own previous record — replaced by the new
                # one (the same services, recorded again).
                continue
            # Every other entry is somebody else's: a live round that
            # will reap it, or a crashed round whose entry is the only
            # trace of a process that still holds its port. Dropping it
            # here would lose that trace.
            kept.append(entry)
    payload = {
        "plan_id": plan_id,
        "project_dir": str(project_dir),
        "round": round_number,
        "owner_token": token,
        "updated_at": datetime.now().isoformat(),
        "services": kept + [r.to_dict() for r in spawned],
    }
    path = ledger_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning(
            "[service_manager] could not write service ledger %s: %s",
            path, exc,
        )
        return None
    # The round is *using* these services now — pin them, so a sweep
    # that cannot see the round does not stop them mid-verification.
    pin_services_for_round(plan_dir, token, spawned)
    return path


def read_ledger(plan_dir: Path) -> Dict[str, Any]:
    """Read a plan's service ledger. Returns ``{}`` when absent or
    corrupt — a damaged ledger must not block a reap sweep."""
    path = ledger_path(plan_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Reaping (C3) — no backend-spawned process outlives the workflow
# ---------------------------------------------------------------------------


@dataclass
class ReapReport:
    """Outcome of one reaping pass over a plan's ledger."""

    plan_id: str = ""
    reason: str = ""
    reaped: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    #: Ports still held by a process we could NOT identify. Reported,
    #: never killed — see the module docstring's ownership rules.
    foreign_ports: List[int] = field(default_factory=list)
    #: Entries a **live** round has pinned were not reaped (2026-10-25).
    #: They are reported here so an operator can see that a sweep left
    #: a service alone on purpose, not because it failed to see it.
    reap_skipped_pinned: List[Dict[str, Any]] = field(default_factory=list)
    ledger_cleared: bool = False

    @property
    def clean(self) -> bool:
        return not self.failed and not self.foreign_ports

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "reason": self.reason,
            "reaped": list(self.reaped),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "foreign_ports": list(self.foreign_ports),
            "reap_skipped_pinned": list(self.reap_skipped_pinned),
            "ledger_cleared": self.ledger_cleared,
        }


def _declaration_from_ledger_entry(entry: Dict[str, Any]) -> Optional[ServiceDeclaration]:
    """Rebuild just enough of a declaration to identify the process.

    The ledger stores the fields a reap needs (port + start_cmd), not
    the whole declaration — the readiness timeout and health URL are
    meaningless once the service is running.
    """
    try:
        return ServiceDeclaration(
            name=str(entry.get("name") or ""),
            port=int(entry.get("port")),
            start_cmd=str(entry.get("start_cmd") or ""),
            cwd=str(entry.get("cwd") or "."),
            log_path=str(entry.get("log_path") or ""),
        )
    except (TypeError, ValueError):
        return None


def _pid_reused(entry: Dict[str, Any]) -> bool:
    """True when the recorded PID now belongs to a different process.

    PIDs are recycled; killing ``entry["pid"]`` without this check can
    murder an unrelated process that inherited the number. Compares the
    recorded ``start_epoch`` against the live one with a 2-second
    tolerance (``ps -o lstart=`` has 1-second granularity and the two
    readings can straddle a tick).
    """
    pid = entry.get("pid")
    recorded = entry.get("start_epoch")
    if not pid or recorded is None:
        return False
    live = _process_start_epoch(int(pid))
    if live is None:
        return False  # process gone, or ps unavailable — not evidence of reuse
    return abs(float(live) - float(recorded)) > 2.0


def reap_services(
    plan_dir: Path,
    plan_id: str = "",
    *,
    reason: str = "",
    grace_seconds: float = 10.0,
    owner_token: Optional[str] = None,
    caller: str = "service_manager.reap_services",
) -> ReapReport:
    """Stop every service this plan's ledger says the backend started.

    Safety, in order — each check can only *prevent* a kill:

      1. A **live** round has pinned the process → skip, and keep it
         in the ledger (2026-10-25, repair-r1-23). The pin names the
         process itself and does not need an owner token, so it covers
         the case the token could not: the plan's api service came up
         with an empty ``owner_token`` and was stopped three seconds
         later by a sweep that read the entry as an orphan.
      2. The entry belongs to a **live** round other than the caller's
         → skip, and keep it in the ledger (2026-10-24). That round
         reaps its own services when it exits; stopping them here is
         what killed a running round's api server 47s after it came up.
      3. ``reap_on_exit: false`` in the ledger → skip (operator opt-out).
      4. Recorded PID has been recycled → skip. PID reuse is how an
         "orphan sweep" kills something it never started.
      5. The listener is neither the recorded PID, nor in the recorded
         process group, nor a signature match for the declared
         ``start_cmd`` → report as foreign, do not kill. The group is
         what catches ``next dev``, where the port is held by a
         grandchild whose rewritten process title matches nothing.
      6. After stopping, the port is re-probed. Still held, and the
         holder matches the declaration → kill that too (a leaked
         VP-spawned sibling). Still held and unidentified → foreign.

    An entry is reaped when it belongs to the calling round
    (``owner_token``) **or** when its owner is no longer running — the
    second half is what cleans up after a round that died without
    reaping.

    An entry with **no** owner recorded is one this module could not
    attribute: either a ledger written before the ``owner_token`` field
    existed, or one written by a project's own copy of this module that
    predates it (the ledger is written by the *project's*
    ``service_manager``, see
    :func:`verification_agent._load_service_manager`). While a round is
    running, such an entry may belong to that round, so it is left
    alone; once no round is running it is an orphan and gets reaped.

    The ledger is cleared only when nothing failed and no foreign
    holder remains; otherwise the un-reaped entries are written back so
    a later sweep retries them.

    ``caller`` names **who** is doing the reaping — the round's own
    exit, the backend's startup sweep, a manual stop.  It is written to
    the plan's kill event stream before each signal goes out, so a
    service that dies during a reap says on the record that it was a
    reap that stopped it (2026-10-26, repair-r1-49).  Without that
    name, the 2026-10-24 shape — a sweep reading a running round's
    service as an orphan — left nothing behind but a corpse and a
    ``received SIGTERM`` on the service's side.
    """
    plan_dir = Path(plan_dir)
    report = ReapReport(plan_id=plan_id or plan_dir.name, reason=reason)
    ledger = read_ledger(plan_dir)
    entries = ledger.get("services")
    if not isinstance(entries, list) or not entries:
        return report

    caller_token = str(owner_token or "")
    live_owners = set(live_service_owners(plan_dir))
    # The pins of the plan: "this round is using these processes right
    # now".  They are read before anything is killed, because the pin —
    # not the owner token — is what says the service is in use.
    pins = live_round_pins(plan_dir)
    released_pins: set = set()

    def _record_reaped(
        entry: Dict[str, Any], name: str, port: int,
    ) -> None:
        """Write down that an address was given back (2026-10-25).

        The reap is the end of an address's life, and the chain has to
        say so — otherwise a reader sees an address appear and never
        sees it go.
        """
        _emit_address_event(
            None, plan_dir, "reaped",
            run_id=plan_id or plan_dir.name,
            round_token=str(entry.get("owner_token") or caller_token or ""),
            old_port=port, new_port=None,
            declared_port=entry.get("declared_port") or port,
            pid=entry.get("pid"),
            boot_id=_pin_boot_id(
                entry.get("pid"), entry.get("start_epoch"),
            ),
            reason=(
                f"service {name!r} on port {port} was stopped "
                f"(reason={reason or 'reap'})"
            ),
        )

    survivors: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "?")
        port = entry.get("port")
        entry_owner = str(entry.get("owner_token") or "")
        if not entry_owner:
            # Nobody claims this entry. While a round is running it may
            # be that round's service (a project whose own
            # ``service_manager`` predates the owner token writes
            # unattributed entries), so it is left alone; with no round
            # running it is an orphan and is reaped below.
            if live_owners:
                report.skipped.append({
                    "name": name,
                    "reason": "unattributed entry while a round is running",
                })
                survivors.append(entry)
                continue
        elif entry_owner != caller_token and entry_owner in live_owners:
            # Another round is running and owns this service. It reaps
            # it when it exits; killing it here is the 2026-10-24 defect.
            report.skipped.append({
                "name": name,
                "reason": "owned by a live run",
                "owner_token": entry_owner,
            })
            survivors.append(entry)
            continue
        if not entry.get("reap_on_exit", True):
            report.skipped.append(
                {"name": name, "reason": "reap_on_exit=false"}
            )
            survivors.append(entry)
            continue
        if not isinstance(port, int):
            report.skipped.append(
                {"name": name, "reason": "ledger entry has no usable port"}
            )
            continue
        if port in PROTECTED_PORTS:
            report.skipped.append(
                {"name": name, "reason": f"port {port} is an backend-runtime port"}
            )
            continue
        if _pid_reused(entry):
            report.skipped.append(
                {"name": name, "reason": "recorded pid was recycled"}
            )
            continue

        decl = _declaration_from_ledger_entry(entry)
        try:
            probe = probe_port(port)
        except Exception as exc:  # noqa: BLE001 - probe never raises
            report.failed.append({"name": name, "reason": f"probe: {exc}"})
            survivors.append(entry)
            continue

        if not probe.listening:
            report.reaped.append(name)  # already gone; nothing to do
            _record_reaped(entry, name, port)
            continue

        # The process is still there.  Is it **in use**?  A live round
        # has pinned it, and the pin does not depend on the owner token
        # — the plan's api service was recorded with an empty token on
        # 2026-10-24, and the sweep that stopped it three seconds after
        # it came up had nothing else to go on.
        pin_index = _pin_covers_entry(entry, pins)
        if pin_index is not None:
            pin = pins[pin_index]
            if _pin_is_in_use(pin, plan_dir, caller_token):
                report.skipped.append({
                    "name": name,
                    "reason": "reap_skipped_pinned",
                    "pid": pin.get("pid"),
                    "port": pin.get("port"),
                    "round_token": str(pin.get("round_token") or ""),
                })
                report.reap_skipped_pinned.append({
                    "name": name,
                    "pid": pin.get("pid"),
                    "port": pin.get("port"),
                    "round_token": str(pin.get("round_token") or ""),
                    "reason": "reap_skipped_pinned",
                })
                survivors.append(entry)
                continue
            # The pin may be released: the round that wrote it is over.
            released_pins.add(pin_index)

        recorded_pid = entry.get("pid")
        recorded_pgid = entry.get("pgid")
        ours = bool(recorded_pid) and recorded_pid in probe.pids
        if not ours and recorded_pgid:
            # 2026-09-23: the listener is often a DESCENDANT of the
            # process the ledger recorded, and then neither check above
            # can see it. ``npx next dev -p N`` spawns ``npm exec`` →
            # ``node .../.bin/next dev`` → ``next-server``; only
            # ``next-server`` binds the port, and it rewrites its own
            # process title, so ``matches_declaration`` has nothing to
            # match either. All three share the group npm created, and
            # the ledger has carried ``pgid`` all along —
            # ``stop_service`` is already group-aware and kills with
            # ``killpg``, it just never got the chance.
            #
            # Residual risk: a recycled pgid could in principle name a
            # stranger's group. It would also have to have bound this
            # exact port, and the ledger only ever records ports the backend
            # itself started. That is a far smaller exposure than the
            # leak it fixes — port 60129 outlived its plan by 3.4 days
            # and would have outlived it indefinitely.
            ours = any(
                _process_group_of(pid) == recorded_pgid
                for pid in probe.pids
            )
        if not ours and decl is not None:
            ours = matches_declaration(decl, probe)
        if not ours:
            report.foreign_ports.append(port)
            report.failed.append({
                "name": name,
                "reason": (
                    f"port {port} is held by an unidentified process "
                    f"({'; '.join(probe.cmdlines) or 'cmdline unknown'}); "
                    f"not killed"
                ),
            })
            survivors.append(entry)
            continue

        runtime = ServiceRuntime(
            name=name, port=port, pid=recorded_pid,
            pgid=entry.get("pgid"),
            start_cmd=str(entry.get("start_cmd") or ""),
        )
        stop_info = stop_service(
            runtime, decl, grace_seconds=grace_seconds,
            caller=caller,
            reason=reason or "the plan's ledger says this service is ours",
            round_token=entry_owner or caller_token,
            plan_dir=plan_dir,
        )
        if stop_info.get("port_free"):
            report.reaped.append(name)
            _record_reaped(entry, name, port)
            continue

        # Still held. If the holder still matches the declaration it is
        # a leaked sibling of the same service (a VP that started its
        # own copy) — fair game. Anything else is foreign.
        after = probe_port(port)
        if decl is not None and after.listening and matches_declaration(
            decl, after,
        ):
            sibling = ServiceRuntime(
                name=name, port=port,
                pid=after.pids[0] if after.pids else None,
                pgid=_process_group_of(after.pids[0]) if after.pids else None,
                start_cmd=str(entry.get("start_cmd") or ""),
            )
            second = stop_service(
                sibling, decl, grace_seconds=grace_seconds,
                caller=caller,
                reason=(
                    f"{reason or 'the ledger says this service is ours'} "
                    f"— a leaked sibling still holds the port"
                ),
                round_token=entry_owner or caller_token,
                plan_dir=plan_dir,
            )
            if second.get("port_free"):
                report.reaped.append(name)
                _record_reaped(entry, name, port)
                continue
            report.failed.append({
                "name": name,
                "reason": f"still held after a second stop: {second.get('detail')}",
            })
        elif after.listening:
            report.foreign_ports.append(port)
            report.failed.append({
                "name": name,
                "reason": (
                    f"port {port} was taken over by an unidentified "
                    f"process; not killed"
                ),
            })
        else:
            report.failed.append({
                "name": name,
                "reason": str(stop_info.get("detail") or "stop failed"),
            })
        survivors.append(entry)

    report.ledger_cleared = _finalise_ledger(
        plan_dir, ledger, survivors, report,
    )
    # The pins this reap may drop: the ones whose process is being
    # stopped, and the ones of the round that is going away (the
    # caller's own round — its exit reap is the end of that round).
    _release_pins(plan_dir, pins, released_pins, caller_token)
    return report


def _finalise_ledger(
    plan_dir: Path,
    ledger: Dict[str, Any],
    survivors: List[Dict[str, Any]],
    report: ReapReport,
) -> bool:
    """Delete the ledger when the reap was clean, else rewrite it with
    what is left. Returns True when the file is gone."""
    path = ledger_path(plan_dir)
    if not survivors:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning(
                "[service_manager] could not remove ledger %s: %s", path, exc,
            )
            return False
    try:
        payload = dict(ledger)
        payload["services"] = survivors
        payload["updated_at"] = datetime.now().isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning(
            "[service_manager] could not rewrite ledger %s: %s", path, exc,
        )
    return False


def reap_all_plans(
    plans_dir: Path,
    *,
    reason: str = "",
    grace_seconds: float = 10.0,
    release_own_owners: bool = False,
    caller: str = "service_manager.reap_all_plans",
) -> List[ReapReport]:
    """Sweep every plan's ledger. Used on backend startup, so a crash
    that left orphans behind is cleaned up by the next boot.

    A plan whose ledger is missing or empty is skipped silently — the
    common case is that no plan ever started a service.

    ``release_own_owners=True`` is for the *shutdown* sweep: the
    process doing the sweeping is going away, so the rounds it
    registered are over and their entries must be reapable — otherwise
    the backend exits leaving the services it started on their ports.
    The startup sweep leaves them alone, because another backend
    process may still be running rounds against the same plans tree.

    ``caller`` names who is doing the sweeping — ``"server.startup_sweep"``
    or ``"server.shutdown_sweep"``.  It is written to each plan's kill
    event stream before the signal goes out, so a service that is
    stopped by a sweep says on the record that a sweep stopped it
    (2026-10-26, repair-r1-49).
    """
    plans_dir = Path(plans_dir)
    reports: List[ReapReport] = []
    if not plans_dir.is_dir():
        return reports
    for ledger_file in sorted(plans_dir.glob(f"*/{LEDGER_FILENAME}")):
        plan_dir = ledger_file.parent
        if release_own_owners:
            released = release_owners_of_process(plan_dir, os.getpid())
            # The rounds of the process that is going away are over, so
            # their pins go with them — otherwise the services they
            # started would outlive the backend that started them.
            for token in released:
                unpin_services_for_round(plan_dir, token)
        if not read_ledger(plan_dir).get("services"):
            continue
        try:
            reports.append(
                reap_services(
                    plan_dir, reason=reason, grace_seconds=grace_seconds,
                    caller=caller,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad plan must not
            # abort the sweep for the others
            logger.warning(
                "[service_manager] reap failed for %s: %s", plan_dir.name, exc,
            )
    return reports
