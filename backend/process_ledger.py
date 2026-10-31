"""Append-only ledger of the processes a plan owns.

Why this file exists
--------------------
The backend spawns a four-layer process tree — executor, sub-agents,
VP services, helper tools — and every layer detaches from its parent.
The in-memory handles therefore die with the backend process: after a
restart, nothing knows which pids the plan started, and every one of
them is an orphan nobody will ever reap.

The ledger is the durable answer to that question. It is the *only*
answer: there is no second copy to reconcile against, so it has to be
right about three things at once.

**It must be append-only.** The file is JSONL: one JSON object per
line. A crash can leave a torn final line, and it can only ever leave a
torn *final* line, because every write is a single ``O_APPEND`` write
of a complete line. A reader that skips the line it cannot parse still
recovers every event before it. Replacing the file wholesale would make
one bad write destroy the history.

**It must be durable before it is trusted.** A spawn that was recorded
but not flushed is a process nobody owns — exactly the failure this
ledger exists to prevent. So every append is followed by ``fsync``.

**It must distinguish "gone" from "never ours".** A live set is a fold
over the events: a ``spawn`` puts a pid in, an exit takes it out. An
exit for a pid that was never spawned is still recorded (it is evidence
that something happened) but it does not create a phantom live record.

Reading never raises. A missing file is an empty ledger, a corrupt line
is skipped, an unreadable file is an empty ledger. A ledger that cannot
be read must not take the backend down with it — the caller decides
what an unreadable ledger means for *its* decision, and that decision
must not be "crash".

**It must read the old file too.** Plans started before the switch have
a ``managed_services.json`` — one whole-file dict — instead of an
event log. The reader normalises that file into the *same* records the
log produces, so there is one kind of record downstream and one code
path that reaps, instead of a second reader that has to be kept in
step. Once the event log records a spawn, it is the whole truth: a pid
left behind in the old file may have been reaped long ago and may
since have been handed to somebody else.

Scope
-----
This module is the ledger itself: the record schema, the append, the
read, and the fold. It deliberately does **not** spawn, signal or
reap anything — the reaper reads this file and acts on it. Keeping the
writer and the killer apart is what makes the file trustworthy: a
process that is only ever *recorded* here cannot be killed by accident.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Name of the ledger file inside a plan directory.
LEDGER_FILENAME = "process_ledger.jsonl"

#: Name of the *legacy* ledger file inside a plan directory.
#:
#: Ownership used to be kept in one whole-file dict written by
#: ``service_manager``. Plans started before the switch still have only
#: that file on disk, so the reader has to understand it — a backend
#: that cannot read the old file forgets the processes it spawned, and
#: they are never reaped.
LEGACY_LEDGER_FILENAME = "managed_services.json"

#: The role given to a process found in the legacy ledger. Every
#: service the old ledger recorded was a VP service, so there is
#: nothing else it could have been.
_ROLE_VP_SERVICE = "vp_service"

#: The roles a recorded process may have.
#:
#: A closed set on purpose. The reaper decides what to do with a
#: process by its role, and an unknown role would mean the reaper had
#: to guess — so the guess is refused at the door instead, with a
#: ``ValueError`` naming the offending role.
ROLES = (
    "executor",
    "repair_executor",
    "subagent",
    "vp_service",
    "tool",
)

#: Event written when a process is started.
EVENT_SPAWN = "spawn"

#: Default event written when a process ends.
EVENT_EXIT = "exit"

#: Event names that end a process' life, used when an event does not
#: carry an explicit ``terminal`` flag.
#:
#: This set is what keeps a *non*-terminal annotation — a reaper
#: reporting that it could not identify a process, say — from being
#: mistaken for "the process is gone". Only the reaper knows which
#: names it will use, so the list is the shared vocabulary between the
#: two modules.
TERMINAL_EVENTS = frozenset(
    {
        "exit",
        "killed",
        "reaped",
        "terminated",
        "died",
        "gone",
    }
)

#: ``state`` of a process that is still running.
STATE_ALIVE = "alive"

#: ``state`` of a process that has ended.
STATE_EXITED = "exited"

#: The fields a :class:`ProcessRecord` is made of, in the order the
#: contract fixes them.
_RECORD_FIELDS = (
    "plan_id",
    "role",
    "pid",
    "pgid",
    "create_time",
    "cmdline",
    "cwd",
    "owner_pid",
    "owner_role",
    "state",
    "created_at",
    "ended_at",
)

#: Serialises appends from concurrent threads (the backend spawns and
#: reaps from several threads at once). POSIX ``O_APPEND`` already
#: makes each individual line atomic with respect to other writers; the
#: lock additionally keeps the open/append/close sequence indivisible
#: for *this* process, so a crash between two appends cannot leave a
#: half-written line behind.
_ledger_lock = threading.Lock()


def _normalise_cmdline(value: Any) -> Tuple[str, ...]:
    """Coerce a command line into a tuple of strings.

    A bare string is a one-element command line, not a sequence of
    characters — ``"python cli.py"`` is one command, and splitting it
    would compare a command against its own characters.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(part) for part in value)


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion for values read back from disk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float coercion for values read back from disk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ProcessRecord:
    """One process the backend started on a plan's behalf.

    The identity fields (``pid`` + ``create_time`` + ``cmdline``) are
    what make it safe to act on later: pids are reused, so the pid
    alone does not identify a process — the kernel's creation time does.
    ``owner_pid`` / ``owner_role`` say who started it, which is what
    lets a shutdown sweep distinguish "our process" from "somebody
    else's process that happens to have the same pid".
    """

    plan_id: str
    role: str
    pid: int
    pgid: int
    create_time: float
    cmdline: Tuple[str, ...]
    cwd: str
    owner_pid: int
    owner_role: str
    state: str = STATE_ALIVE
    created_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(
                "unknown process role %r (expected one of: %s)"
                % (self.role, ", ".join(ROLES))
            )
        # Normalised here rather than at the call site so that every
        # record — however it was built — carries an immutable command
        # line. A mutable list would let a caller change what the
        # ledger says a process was running after the fact.
        self.cmdline = _normalise_cmdline(self.cmdline)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form of the record."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def ledger_path(plan_dir: Any) -> Path:
    """Path of the ledger file inside ``plan_dir``."""
    return Path(plan_dir) / LEDGER_FILENAME


def legacy_ledger_path(plan_dir: Any) -> Path:
    """Path of the legacy ledger file inside ``plan_dir``."""
    return Path(plan_dir) / LEGACY_LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _append_line(path: Path, payload: Dict[str, Any]) -> None:
    """Append one JSON object as a single line, durably.

    One ``O_APPEND`` write of the whole line, then ``fsync``. The write
    is a single system call so that two writers cannot interleave
    halves of their lines; the ``fsync`` is what makes the line
    survive the machine losing power, which is the case the ledger
    exists for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    ).encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def record_event(
    plan_dir: Any,
    event: str,
    *,
    terminal: bool = False,
    **fields: Any,
) -> Dict[str, Any]:
    """Append an arbitrary event to the ledger.

    The general form of the two named operations below. A non-terminal
    event annotates the ledger without changing the live set — a reaper
    reporting that it could not identify a process belongs here, since
    that process is still running.

    ``terminal=True`` marks the event as ending the process' life;
    without it, an event whose name is not in :data:`TERMINAL_EVENTS`
    leaves the live set untouched.
    """
    payload: Dict[str, Any] = {
        "event": str(event),
        "ts": time.time(),
        "terminal": bool(terminal),
    }
    payload.update(fields)
    with _ledger_lock:
        _append_line(ledger_path(plan_dir), payload)
    return payload


def _coerce_record(record: Any) -> ProcessRecord:
    """Accept a :class:`ProcessRecord` or a mapping describing one."""
    if isinstance(record, ProcessRecord):
        return record
    if isinstance(record, Mapping):
        values = {
            name: record[name]
            for name in _RECORD_FIELDS
            if name in record
        }
        return ProcessRecord(**values)
    raise TypeError(
        "record must be a ProcessRecord or a mapping, got %s"
        % type(record).__name__
    )


def record_spawn(plan_dir: Any, record: Any) -> None:
    """Record that a process was started.

    The event is written before the caller acts on the process, so a
    crash right after the spawn still leaves the pid on record — an
    orphan nobody knows about is the failure this ledger prevents.

    The record is stored as *alive*: a spawn is the statement that the
    process is running, so any stale ``state`` on the object handed in
    is overwritten rather than carried into the ledger.
    """
    live = replace(
        _coerce_record(record),
        state=STATE_ALIVE,
        ended_at=None,
    )
    event = {
        "event": EVENT_SPAWN,
        "ts": time.time(),
        "terminal": False,
        "pid": live.pid,
        "role": live.role,
        "record": live.to_dict(),
    }
    with _ledger_lock:
        _append_line(ledger_path(plan_dir), event)


def record_exit(
    plan_dir: Any,
    pid: int,
    event: str = EVENT_EXIT,
    *,
    exit_code: Optional[int] = None,
    detail: Optional[str] = None,
) -> bool:
    """Record that a process ended.

    Returns ``True`` when the pid was a live record and is now gone,
    ``False`` when the ledger never knew about it. The distinction
    matters: "we started it and it ended" and "something else on this
    machine happens to have that pid" are different facts, and the
    second must not be reported as the first.

    The event is appended either way — an exit for an unknown pid is
    still evidence, and dropping it would leave the ledger unable to
    explain what happened.
    """
    pid = _as_int(pid)
    record = live_records(plan_dir).get(pid)
    ended_at = time.time()

    payload: Dict[str, Any] = {
        "event": str(event),
        "ts": ended_at,
        "terminal": True,
        "pid": pid,
        "exit_code": exit_code,
        "detail": detail,
        "ended_at": ended_at,
    }
    if record is not None:
        payload.update(
            {
                "plan_id": record.plan_id,
                "role": record.role,
                "pgid": record.pgid,
                "cmdline": list(record.cmdline),
                "cwd": record.cwd,
                "create_time": record.create_time,
                "owner_pid": record.owner_pid,
                "owner_role": record.owner_role,
                "started_at": record.created_at,
            }
        )
        if record.create_time > 0:
            payload["duration"] = max(0.0, ended_at - record.create_time)

    with _ledger_lock:
        _append_line(ledger_path(plan_dir), payload)
    return record is not None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _read_jsonl_events(path: Path) -> List[Dict[str, Any]]:
    """Every readable event in an event log, oldest first.

    A missing ledger reads as no events, and a line that cannot be
    parsed is skipped rather than fatal: one torn tail line from a
    crash must not cost the events that came before it.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []

    events: List[Dict[str, Any]] = []
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                continue  # a torn line; the rest of the ledger still reads
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def _legacy_cmdline(value: Any) -> Tuple[str, ...]:
    """Command line from a legacy entry, normalised to a tuple.

    The old ledger wrote the command as a string, but the same field
    turns up as a token list in some places, so both shapes are
    accepted. A bare string is one command — not a sequence of
    characters — so ``"python cli.py"`` stays one element.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value)
    return ()


def _legacy_record(
    entry: Mapping[str, Any], plan_id: str
) -> Optional[ProcessRecord]:
    """Build a record from one entry of the legacy ledger.

    ``None`` when the entry carries no usable pid: a service that never
    came up has no process to act on, and inventing one would put a
    phantom into the live set — a worse failure than a missing entry,
    because a phantom is a pid the reaper may act on.
    """
    pid = _as_int(entry.get("pid"), 0)
    if pid <= 0:
        return None
    return ProcessRecord(
        # The entry names its own plan when it knows it; the file's
        # plan id is the fallback for the entries that do not.
        plan_id=str(entry.get("plan_id") or plan_id),
        role=_ROLE_VP_SERVICE,
        pid=pid,
        # An absent pgid stays unknown (0) rather than being guessed
        # from the pid: killing a group the process is not in is worse
        # than killing the single process.
        pgid=_as_int(entry.get("pgid"), 0),
        create_time=_as_float(entry.get("start_epoch"), 0.0),
        cmdline=_legacy_cmdline(entry.get("start_cmd")),
        cwd=str(entry.get("cwd") or ""),
        # The old file names the process that started the service, so
        # that pid is kept. Its role is the backend's: the backend is
        # what wrote the file in the first place.
        owner_pid=_as_int(entry.get("owner_pid"), 0),
        owner_role="backend",
    )


def _read_legacy_events(plan_dir: Any) -> List[Dict[str, Any]]:
    """Events synthesised from the legacy ledger file.

    Every service in the old file becomes one ``spawn`` event, so the
    fold in :func:`live_records` sees the same shape of history
    whichever file the plan happens to have. Never raises: a damaged
    legacy file must not take the backend down, by the same rule the
    event log follows.
    """
    path = legacy_ledger_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []  # a corrupt file is an empty ledger, never an error
    if not isinstance(payload, Mapping):
        return []
    services = payload.get("services")
    if not isinstance(services, (list, tuple)):
        return []

    plan_id = str(payload.get("plan_id") or "")
    events: List[Dict[str, Any]] = []
    for entry in services:
        if not isinstance(entry, Mapping):
            continue
        record = _legacy_record(entry, plan_id)
        if record is None:
            continue
        events.append(
            {
                "event": EVENT_SPAWN,
                "ts": record.created_at,
                "terminal": False,
                "pid": record.pid,
                "role": record.role,
                "record": record.to_dict(),
            }
        )
    return events


def read_events(plan_dir: Any) -> List[Dict[str, Any]]:
    """Every readable event in the ledger, oldest first.

    The ledger may be in either shape: the append-only event log, or
    the whole-file dict the backend wrote before the switch. Both are
    read, and the legacy file is read only when the event log has
    nothing to say — once the event log records a spawn, it is the
    whole truth, because a pid left behind in the old file may have
    been reaped long ago and may since have been handed to somebody
    else.

    A missing ledger reads as no events, and a line that cannot be
    parsed is skipped rather than fatal: one torn tail line from a
    crash must not cost the events that came before it.
    """
    events = _read_jsonl_events(ledger_path(plan_dir))
    if any(event.get("event") == EVENT_SPAWN for event in events):
        return events

    legacy = _read_legacy_events(plan_dir)
    if not legacy:
        return events
    # The old file is the earlier of the two histories, so its events
    # come first: an exit recorded in the event log still takes a
    # legacy process out of the live set.
    return legacy + events


def _is_terminal(event: Mapping[str, Any]) -> bool:
    """Whether an event ends the life of the pid it names.

    An explicit ``terminal`` flag wins; otherwise the event name is
    matched against :data:`TERMINAL_EVENTS`.
    """
    flag = event.get("terminal")
    if isinstance(flag, bool):
        return flag
    return str(event.get("event") or "") in TERMINAL_EVENTS


def _record_from_event(event: Mapping[str, Any]) -> Optional[ProcessRecord]:
    """Rebuild a record from a spawn event, or ``None`` if unusable.

    A spawn event carries the record under ``record``; an event that
    spells the fields out flat is accepted too. Values are coerced
    rather than trusted — the file is written by another process, and a
    ledger that raises while being read is a ledger nobody can read
    during an incident.
    """
    payload = event.get("record")
    if not isinstance(payload, Mapping):
        payload = event

    ended_at = payload.get("ended_at")
    try:
        return ProcessRecord(
            plan_id=str(payload.get("plan_id") or ""),
            role=str(payload.get("role") or ""),
            pid=_as_int(payload.get("pid")),
            pgid=_as_int(payload.get("pgid")),
            create_time=_as_float(payload.get("create_time")),
            cmdline=payload.get("cmdline") or (),
            cwd=str(payload.get("cwd") or ""),
            owner_pid=_as_int(payload.get("owner_pid")),
            owner_role=str(payload.get("owner_role") or ""),
            state=str(payload.get("state") or STATE_ALIVE),
            created_at=_as_float(payload.get("created_at"), time.time()),
            ended_at=(
                None if ended_at is None else _as_float(ended_at)
            ),
        )
    except (TypeError, ValueError):
        # An unknown role or an unusable field makes the record
        # unreadable; skipping it is safer than raising, and safer
        # than pretending the process is ours.
        return None


def live_records(plan_dir: Any) -> Dict[int, ProcessRecord]:
    """The processes the ledger says are still running, keyed by pid.

    A fold over the event stream: a spawn puts the record in (a later
    spawn of the same pid replaces it), an exit takes it out. The
    result is the answer to "what is still running for this plan" —
    the question that in-memory handles could only answer until the
    backend restarted.
    """
    live: Dict[int, ProcessRecord] = {}
    for event in read_events(plan_dir):
        if event.get("event") == EVENT_SPAWN:
            record = _record_from_event(event)
            if record is None or record.pid <= 0:
                continue
            live[record.pid] = record
            continue
        if not _is_terminal(event):
            continue
        pid = event.get("pid")
        if pid is None:
            nested = event.get("record")
            pid = nested.get("pid") if isinstance(nested, Mapping) else None
        if pid is None:
            continue
        live.pop(_as_int(pid, -1), None)
    return live