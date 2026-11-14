"""Who sent the signal?  A kill audit any process can install.

Why this file exists
--------------------
The kill audit in :mod:`service_manager` records the signals the
**service manager** sends -- and it can only record those, because it is
installed by importing that module.

The reaper never imports the service manager.  It is a module of its
own, it is imported on its own, and the signals it sends -- the ones
that end a service the run started -- went to the kernel with nothing
written down about them.  A run that ended with ``Terminated: 15`` on
the console therefore could not say *who* sent it: the only records on
disk were the ones from the processes that happened to import the
service manager, and the sender that mattered was not one of them.

So the audit is a module of its own, and any process that sends signals
can install it.  The records it writes are the same records the service
manager writes -- the same directory, the same file name, the same
fields -- so the two audits can be read together as one account of the
signals a run sent.

The rules the records follow
----------------------------

* **One file per process.**  The file is named ``kill-audit-<pid>.jsonl``
  after the process that wrote it, so two processes never write into the
  same file and their records cannot interleave.
* **One record per line.**  Every record is a JSON object on a line of
  its own, and every line is written with a single write -- a record
  that is not a whole line is a record nobody can read.
* **The audit never blocks a signal.**  A record that cannot be written
  is a record nobody will read; it must not cost the signal it was
  meant to describe.  Every failure here is swallowed.
* **The fields are named, not guessed.**  A send record carries
  exactly ``kind`` (which call was made), ``target`` (the pid or the
  group id that was aimed at), ``signal`` (the signal number),
  ``outcome`` (what became of it), ``pid`` (the process that sent it),
  ``ts`` and ``stack`` -- and an outcome record adds ``errno``, the
  errno the kernel answered with.  The set is exact: the service
  manager's own audit writes the same fields, and the two ledgers are
  read together as one account of the signals a run sent, so a record
  that carried more than the contract names could not be compared with
  the records written next to it.  A reader that has to guess a field
  name cannot query the ledger -- and a query that cannot match
  answers nothing while looking rigorous.
* **A liveness probe is recorded too.**  A kill with signal ``0`` asks
  the kernel whether the pid exists and sends nothing at all — but it is
  still a call that reached the kernel, and this file is the account of
  what this process asked the kernel to do.  It is the most frequent
  call in the system and every record costs a stack extraction and a
  file write, so the records are **capped**
  (:data:`KILL_AUDIT_MAX_RECORDS`) rather than filtered.  A cap keeps
  the newest evidence; a filter would delete a whole class of calls
  from the account, and "signal 0 was never recorded" is exactly the
  blind spot an audit cannot afford (2026-11-08: an early return for
  ``sig == 0`` removed the most common kind of call from the records,
  and the cases that pin "every kill is written down" went red).

The sending side and the receiving side
---------------------------------------
The hook records what this process **sent**.  That is half the
question, and the half that cannot answer "who killed me": the signal
that killed a process was sent by somebody, and when that somebody was
not one of the audited processes, nothing in the sending side of the
ledger says so -- a conclusion drawn from its silence is a conclusion
drawn from an absence.

So the audit can also record the signals this process **receives**
(:func:`install_signal_receipt_audit`): the signal's name, the
receiving process's pid and process group, the second it arrived in,
and the membership of its process group at that moment.  The promise
about that side is narrow, and it is the whole of the safety argument:

* the record is written, and then the **default disposition is put
  back** and the **same signal is sent to this process** -- so the
  process dies of it exactly as it would have died without the audit
  (143 when the signal is SIGTERM);
* no signal is ever sent to any other process, and to no process group
  at all.  The receiving side can only ever point at itself;
* the handler never forks.  The membership of the process group is
  sampled by a background thread and cached; the handler only reads
  that cache.  A signal that interrupts a fork leaves the fork
  half-done, and the process hangs instead of dying.  The sampler
  reads the process table directly when it can, so the census costs a
  millisecond instead of a fork; the host is only asked when the table
  cannot be read at all;
* the handler writes with **its own** lock.  The sending side's lock is
  held by the thread the signal interrupted, and a handler that waited
  for it would leave the process hanging instead of dying;
* a signal that somebody else already owns is **chained**, never
  replaced: the audit records the signal and then hands control back
  to the handler that was already there.  Taking the server's graceful
  shutdown away would turn every shutdown into a 143.

The directory the records go to is ``$PDT_KILL_AUDIT_DIR`` when it is
set, and a fixed directory under the platform's temporary directory when
it is not -- so every process of one run can be pointed at one place by
the environment, and a process that runs on its own still writes
somewhere.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

#: The environment variable that says where the audit records go.
KILL_AUDIT_DIR_ENV = "PDT_KILL_AUDIT_DIR"

#: The environment variable that names *more* signals for the
#: **receiving** side to record.  It holds a comma-separated list of
#: signal names or numbers.  The signals the audit always watches
#: (:data:`DEFAULT_RECEIVED_SIGNALS`) are watched whether it is set or
#: not: a value that happens to be a number names a signal to watch
#: *as well*, it never takes the ones that matter away.
KILL_AUDIT_RECEIVE_ENV = "PDT_KILL_AUDIT_RECEIVE"

#: The environment variable that names a file holding the process-group
#: census to use instead of asking the host for one.  It is the way a
#: run on a loaded host keeps the snapshot cheap and reproducible.
KILL_AUDIT_CENSUS_ENV = "PDT_KILL_AUDIT_GROUP_CENSUS"

#: The name of the directory the records go to when the environment
#: does not name one.
KILL_AUDIT_DIR_NAME = "pdt-kill-audit"

#: The prefix of the file one process writes its records into.
KILL_AUDIT_FILE_PREFIX = "kill-audit-"

#: How many records one process writes.  A cap, not a budget: the audit
#: is evidence for the last kill, and a runaway process must not fill
#: the disk while the run it is in is still going.
KILL_AUDIT_MAX_RECORDS = 20000

#: How long the process-group census may take before it is given up on.
#: The census is evidence; the signal is not.  A host that cannot answer
#: in this many seconds is not going to answer at all.
KILL_AUDIT_CENSUS_TIMEOUT = 2.0

#: How often the process-group census is refreshed.  The census is
#: evidence about a moment that has passed by the time a signal
#: arrives, so it is sampled in the background and the handler reads
#: the last sample.
KILL_AUDIT_CENSUS_INTERVAL = 2.0

#: How often the census is refreshed when the only census available is
#: the one that asks the host (``ps``).  Asking the host forks, and on
#: a loaded host the answer costs the better part of a second -- a
#: census taken that often is not worth what it costs, so it is taken
#: rarely and the record says how old the snapshot is.
KILL_AUDIT_CENSUS_INTERVAL_SLOW = 30.0

#: The name a **send** record carries in the ledgers that carry an
#: event name.  It is the name the tool that reads the ledgers
#: searches for (``scripts/kill_signal_reconciliation.py``), so it is
#: not free to change: a record whose event name the reader does not
#: look for is a record the reader cannot find.  The send records this
#: module writes name themselves by ``kind`` instead -- the field set
#: is exact -- and the tool still finds them, because it recognises
#: ``kill`` and ``killpg`` as well.
EVENT_SENT = "signal.sent"

#: The record names a signal this process received.
EVENT_RECEIVED = "signal.received"

#: The signals the receiving side records unless it is told otherwise.
#: SIGTERM is the signal that ends a run; SIGINT is the one a person
#: sends.  Both are the ones a post-mortem asks about.
DEFAULT_RECEIVED_SIGNALS = (int(signal.SIGTERM), int(signal.SIGINT))

#: The outcome of a signal attempt.
#:
#: The audit hook fires **before** the syscall, so it cannot know which
#: of these happened -- it only knows a signal was *attempted*.  The
#: party that sends the signal is the one that knows the outcome, so it
#: writes that separately, with :func:`write_kill_outcome`.
OUTCOME_ATTEMPTED = "attempted"

#: The signal went out.
OUTCOME_SENT = "sent"

#: The kernel refused the signal (``EPERM``).  On macOS a process
#: group whose leader is a zombie answers this way for the whole group.
OUTCOME_REFUSED = "refused"

#: The signal failed in some other way.
OUTCOME_FAILED = "failed"

#: There was nothing to signal (``ESRCH``) -- the target was already
#: gone, which is a success for the reaper.
OUTCOME_GONE = "gone"


#: The lock the **sending** side writes under.  It is reentrant: a
#: signal can interrupt this process while it holds the lock, and the
#: handler that runs then writes under the same lock on the same
#: thread -- which is exactly what has to happen for the process to be
#: able to die.
_kill_audit_lock = threading.RLock()

#: The lock the **receiving** side writes under.  A separate lock, on
#: purpose: the sending side's lock is held by the thread a signal
#: interrupted, and a handler that waited for it would leave the
#: process hanging instead of letting it die.
_kill_audit_receive_lock = threading.Lock()

_kill_audit_count = 0
_kill_audit_receive_count = 0
_kill_audit_installed = False
_kill_audit_receiver_installed = False

#: The handler this module installed for a signal, per signal.
_receiver_handlers: dict = {}

#: The cached census of this process's group, and the lock it is
#: refreshed under.
_census_lock = threading.Lock()
_census_members: List[dict] = []
_census_error: str = ""
_census_taken_at: Optional[float] = None
_census_backend: str = ""
_census_thread_started = False

#: The module used to read the process table, and whether it has been
#: looked for yet.  ``psutil`` reads the table without forking, which
#: is the whole point of preferring it: the census runs in a thread of
#: a process that may be forking children of its own.
_psutil_module = None
_psutil_probed = False


def _load_psutil():
    """The module that reads the process table, or ``None``.

    It is an **optional** accelerator, not a dependency: this module
    works with the standard library alone, and asks the host for the
    census when the table cannot be read directly.  It is looked for
    once and remembered: the census runs every few seconds for the life
    of the process, and an import that is repeated on every sample is an
    import that is paid for nothing.
    """
    global _psutil_module, _psutil_probed
    if not _psutil_probed:
        _psutil_probed = True
        try:
            import psutil
        except Exception:  # noqa: BLE001 - the census is best-effort
            pass
        else:
            _psutil_module = psutil
    return _psutil_module


def kill_audit_dir() -> Path:
    """Where this process writes its kill audit."""
    raw = str(os.environ.get(KILL_AUDIT_DIR_ENV, "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(tempfile.gettempdir()) / KILL_AUDIT_DIR_NAME


def kill_audit_path(pid: Optional[int] = None) -> Path:
    """The file one process writes its records into."""
    own = os.getpid() if pid is None else int(pid)
    return kill_audit_dir() / f"{KILL_AUDIT_FILE_PREFIX}{own}.jsonl"


def _kill_audit_stack(depth: int = 6) -> str:
    """A short hint at where a signal was sent from.

    The file **name** only, never the full path -- the same rule the
    kill event stream follows.
    """
    frames = traceback.extract_stack()[:-2]
    return " <- ".join(
        f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
        for frame in frames[-depth:]
    )


def _write_line(path: Path, line: str) -> None:
    """Append one whole line to *path*, with a single write.

    One write of one whole line: two threads of this process cannot put
    half a record into the file (a record that is not a whole line is a
    record nobody can read), and the signal this record describes is
    never made to wait for a slower writer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    descriptor = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644,
    )
    try:
        while data:
            written = os.write(descriptor, data)
            if written <= 0:
                break
            data = data[written:]
    finally:
        os.close(descriptor)


def _append_record(record: dict) -> None:
    """Append one record to this process's audit file.  Never raises."""
    global _kill_audit_count
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        return
    with _kill_audit_lock:
        if _kill_audit_count >= KILL_AUDIT_MAX_RECORDS:
            return
        _kill_audit_count += 1
        try:
            _write_line(kill_audit_path(), line)
        except OSError:
            # A record that cannot be written is a record nobody will
            # read, and it must not cost the signal it was meant to
            # describe.
            pass


def _signal_name(sig: int) -> str:
    """The name of a signal number ("SIGTERM"), or a best effort."""
    try:
        return signal.Signals(int(sig)).name
    except (ValueError, TypeError):
        return f"SIG{sig}"


def _sent_record(
    kind: str,
    target: int,
    sig: int,
    outcome: str,
    errno: Optional[int] = None,
    with_errno: bool = False,
) -> dict:
    """One record about a signal this process sent.

    The record carries exactly the fields the audit is read by --
    ``{kind, target, signal, outcome, pid, ts, stack}`` -- and the
    outcome record adds the ``errno`` the kernel answered with.  The
    set is exact in both directions: the fields are not guessed, and
    they are not duplicated under a second name.  The service manager's
    own audit writes the same fields into the same kind of file, and
    the two ledgers are read together as one account of the signals a
    run sent -- so a record that carried more than the contract names
    could not be compared with the records written next to it.

    ``kind`` says which call was made: ``kill`` for one pid and
    ``killpg`` for a whole group, whose id is the target.  ``pid`` is
    the process that sent the signal, and it is the process the file
    is named after.
    """
    number = (
        int(sig) if isinstance(sig, int) and not isinstance(sig, bool) else -1
    )
    record = {
        # which call was made
        "kind": str(kind),
        # what was aimed at
        "target": int(target),
        # with which signal
        "signal": number,
        # what became of it
        "outcome": str(outcome),
        # by whom -- the process this record's file is named after
        "pid": os.getpid(),
        # when, and from where
        "ts": datetime.now().isoformat(),
        "stack": _kill_audit_stack(),
    }
    if with_errno:
        record["errno"] = (
            int(errno)
            if isinstance(errno, int) and not isinstance(errno, bool)
            else None
        )
    return record


def write_kill_audit(kind: str, target: int, sig: int) -> None:
    """Write one kill record down.  Never raises.

    The record says which call was made (``kind``) and what was aimed
    at (``target``): a pid for ``kill``, the group's id for ``killpg``.
    It is written **before** the syscall, so it can only say that the
    signal was attempted; what became of it is written by
    :func:`write_kill_outcome`.
    """
    try:
        record = _sent_record(kind, target, sig, OUTCOME_ATTEMPTED)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        return
    _append_record(record)


def write_kill_outcome(
    kind: str,
    target: int,
    sig: int,
    outcome: str,
    errno: Optional[int] = None,
) -> None:
    """Write the **outcome** of one signal attempt down.  Never raises.

    The hook above records that a signal was *about* to be sent; it
    fires before the syscall, so it cannot say whether the signal went
    out, was refused (``EPERM``) or found nothing (``ESRCH``).  Those
    are the three answers that matter to a reader asking "did that
    kill actually happen", and only the party that sent the signal
    knows them -- so it writes them here.

    The record has the same fields as the one the hook wrote, plus the
    ``errno`` the kernel answered with: it is the second record of one
    signal attempt, and the two are read together.

    Args:
        kind: ``"kill"`` or ``"killpg"`` -- which call was made.
        target: The pid (or group id) that was signalled.
        sig: The signal number.
        outcome: One of the ``OUTCOME_*`` values.
        errno: The errno the signal failed with, when it failed.
    """
    try:
        record = _sent_record(
            kind, target, sig, outcome, errno, with_errno=True,
        )
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        return
    _append_record(record)


# ---------------------------------------------------------------------------
# The receiving side
# ---------------------------------------------------------------------------


def _census_rows(text: str, pgid: int) -> List[dict]:
    """The rows of a ``ps`` census that belong to *pgid*.

    A census is ``pid ppid pgid command`` per line.  Anything that is
    not such a line is not a row -- the header line and the section
    markers of a sampled census are not rows either.
    """
    members: List[dict] = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            row_pgid = int(parts[2])
        except ValueError:
            continue
        if row_pgid != pgid:
            continue
        members.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": row_pgid,
            "command": parts[3].strip() if len(parts) > 3 else "",
        })
    return members


def _census_psutil(pgid: int) -> Optional[List[dict]]:
    """The members of *pgid*, read from the process table directly.

    ``None`` means the process table could not be read at all -- the
    caller then asks the host instead.  An **empty** list means the
    table was read and the group had nobody in it; those are two
    different facts, and only one of them is evidence.
    """
    psutil = _load_psutil()
    if psutil is None:
        return None
    try:
        pids = list(psutil.pids())
    except Exception:  # noqa: BLE001 - a census is best-effort
        return None
    members: List[dict] = []
    for pid in pids:
        if pid <= 0:
            # pid 0 is not a process of any group: ``getpgid(0)``
            # answers with the *caller's* group, so asking about it
            # invents a member.
            continue
        try:
            row_pgid = os.getpgid(pid)
        except OSError:
            # The process is gone between the two reads; it is not a
            # member of anything any more.
            continue
        if row_pgid != pgid:
            continue
        member = {"pid": pid, "ppid": None, "pgid": row_pgid, "command": ""}
        try:
            process = psutil.Process(pid)
            member["ppid"] = process.ppid()
            member["command"] = process.name()
        except Exception:  # noqa: BLE001 - a member without a name is a member
            pass
        members.append(member)
    return members


def _group_census(pgid: int) -> Tuple[List[dict], str, str]:
    """The members of *pgid* right now, why there are none, and where
    the answer came from.

    The census comes from the file the environment names when there is
    one, from the process table when the table can be read, and from
    the host otherwise.  It is best-effort on purpose: a census that
    could not be taken is recorded as an **error**, never as an empty
    group -- "I saw nobody" and "I could not look" are two different
    facts, and only one of them is evidence.

    Asking the host forks, so it must never be called from a signal
    handler: a signal that interrupts a fork leaves the fork half-done
    and the process hangs instead of dying.  The background sampler
    calls it; the handler reads what the sampler cached.
    """
    override = str(os.environ.get(KILL_AUDIT_CENSUS_ENV, "") or "").strip()
    if override:
        try:
            text = Path(override).expanduser().read_text(
                encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            return [], f"the census file could not be read: {exc}", "file"
        return _census_rows(text, pgid), "", "file"
    members = _census_psutil(pgid)
    if members is not None:
        return members, "", "psutil"
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="],
            capture_output=True, text=True, timeout=KILL_AUDIT_CENSUS_TIMEOUT,
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001 - a census is best-effort
        return [], f"the census could not be taken: {exc}", "ps"
    if completed.returncode != 0:
        return [], f"the census could not be taken: ps exited {completed.returncode}", "ps"
    return _census_rows(completed.stdout, pgid), "", "ps"


def _refresh_census() -> str:
    """Take one census of this process's group and cache it.

    Returns where the census came from, so the sampler can slow down
    when the only census available is the one that asks the host.
    """
    global _census_members, _census_error, _census_taken_at, _census_backend
    members, error, backend = _group_census(os.getpgrp())
    with _census_lock:
        _census_members = members
        _census_error = error
        _census_taken_at = time.time()
        _census_backend = backend
    return backend


def _census_loop() -> None:
    """Refresh the cached census until the process ends."""
    interval = KILL_AUDIT_CENSUS_INTERVAL
    while True:
        backend = ""
        try:
            backend = _refresh_census()
        except Exception:  # noqa: BLE001 - the census is best-effort
            pass
        if backend == "ps":
            # The only census available is the one that asks the host,
            # and asking the host is expensive: take it rarely.
            interval = KILL_AUDIT_CENSUS_INTERVAL_SLOW
        time.sleep(interval)


def _start_census_thread() -> None:
    """Start the background sampler, once per process."""
    global _census_thread_started
    if _census_thread_started:
        return
    _census_thread_started = True
    thread = threading.Thread(
        target=_census_loop, name="pdt-kill-audit-census", daemon=True,
    )
    try:
        thread.start()
    except Exception:  # noqa: BLE001 - the census is best-effort
        _census_thread_started = False


def _cached_census() -> Tuple[List[dict], str, Optional[float], str]:
    """The last census of this process's group, when it was taken, and
    where it came from.

    The handler reads this and nothing else: it never asks the host
    anything, because a signal that interrupts a fork leaves the fork
    half-done.

    When no census has been taken yet, the only member this process can
    name without asking the host is itself -- and the record says the
    membership is incomplete, so it cannot be read as a complete list.
    """
    with _census_lock:
        members = list(_census_members)
        error = _census_error
        taken_at = _census_taken_at
        backend = _census_backend
    if members or error:
        return members, error, taken_at, backend
    return ([{
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "pgid": os.getpgrp(),
        "command": "",
    }], "", None, backend)


def _append_receive_record(record: dict) -> None:
    """Append one record to this process's audit file, from the
    receiving side.  Never raises.

    It writes with **its own** lock and never with the sending side's:
    the sending side's lock is held by the thread a signal interrupted,
    and a handler that waited for it would leave the process hanging
    instead of dying.
    """
    global _kill_audit_receive_count
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        return
    with _kill_audit_receive_lock:
        if _kill_audit_receive_count >= KILL_AUDIT_MAX_RECORDS:
            return
        _kill_audit_receive_count += 1
        try:
            _write_line(kill_audit_path(), line)
        except OSError:
            # A record that cannot be written is a record nobody will
            # read, and it must not cost the signal it was meant to
            # describe.
            pass


def write_signal_received(signum: int) -> None:
    """Write down the signal this process just received.  Never raises.

    The record answers the question the sending side cannot: which
    signal arrived, who received it, in which second, and who else was
    in its process group at that moment.  The sender is **not** in the
    record: the receiving side does not know who sent the signal, and a
    record that guessed would be worse than one that says so.
    """
    try:
        number = int(signum)
    except (TypeError, ValueError):
        return
    try:
        pid = os.getpid()
        pgid = os.getpgrp()
        members, error, taken_at, backend = _cached_census()
        stamp = datetime.now()
        record = {
            "event": EVENT_RECEIVED,
            "signal": number,
            "signal_name": _signal_name(number),
            "receiver_pid": pid,
            "receiver_pgid": pgid,
            "received_at": stamp.isoformat(),
            "received_at_second": stamp.replace(microsecond=0).isoformat(),
            "received_at_epoch": time.time(),
            "group_members": members,
            "group_members_count": len(members),
            # A census that could not be *taken* is not a census: the
            # membership is unknown, so it is not a complete group.
            # "I tried and could not look" is not "I saw nobody".
            "group_members_complete": taken_at is not None and not error,
            # where the membership came from: "psutil" (the process
            # table, read directly), "ps" (the host), "file" (the file
            # the environment names) -- or "none" when no census has
            # been taken yet and the membership is this process alone.
            "group_census_backend": backend or "none",
            "group_snapshot_at": (
                datetime.fromtimestamp(taken_at).isoformat()
                if taken_at else None
            ),
            "group_snapshot_age_seconds": (
                round(max(0.0, time.time() - taken_at), 3)
                if taken_at else None
            ),
            "sender_pid": None,
            "sender_pgid": None,
            "sender_is_self": None,
            "pid": pid,
            "ppid": os.getppid(),
            "pgid": pgid,
            "ts": stamp.isoformat(),
            "ts_epoch": time.time(),
            "stack": _kill_audit_stack(),
        }
        if error:
            record["group_census_error"] = error
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        return
    _append_receive_record(record)


def _received_handler(signum, frame) -> None:
    """Record the signal, put the default disposition back, die of it.

    The order is the whole of the safety argument: the record is
    written **first** (so the evidence survives the death), the default
    disposition is restored **second** (so the signal is not swallowed
    by the handler that recorded it), and the same signal is then sent
    to this process **only** -- so the process ends exactly as it would
    have ended without the audit.
    """
    try:
        write_signal_received(signum)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        pass
    try:
        signal.signal(signum, signal.SIG_DFL)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        pass
    try:
        os.kill(os.getpid(), signum)
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        pass
    # Reached only when the signal was blocked or ignored: the process
    # still has to end, and it ends the way a signalled process ends.
    os._exit(128 + int(signum))


def _install_receiver(signum: int) -> bool:
    """Record *signum* from now on, without taking it away from anyone.

    A signal that already has a handler is **chained**: the audit
    records the signal and then hands control back to the handler that
    was already there.  Replacing it would take the server's graceful
    shutdown away and turn every shutdown into a 143.

    Returns whether this process will record the signal from now on.
    """
    try:
        current = signal.getsignal(signum)
    except (OSError, TypeError, ValueError):
        return False
    if current is _received_handler or current is _receiver_handlers.get(signum):
        return True
    if current in (signal.SIG_DFL, None):
        try:
            signal.signal(signum, _received_handler)
        except (OSError, RuntimeError, ValueError):
            # Not the main thread, or the platform says no: the sending
            # side still works, and this process simply cannot record
            # what it receives.
            return False
        _receiver_handlers[signum] = _received_handler
        return True

    def handler(signum_, frame, _original=current):
        try:
            write_signal_received(signum_)
        except Exception:  # noqa: BLE001 - the audit never blocks a signal
            pass
        return _original(signum_, frame)

    try:
        signal.signal(signum, handler)
    except (OSError, RuntimeError, ValueError):
        return False
    _receiver_handlers[signum] = handler
    return True


def _parse_signal_list(text: str) -> List[int]:
    """The signals a comma-separated list of names or numbers names."""
    wanted: List[int] = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            wanted.append(int(part))
            continue
        except ValueError:
            pass
        name = part.upper()
        if not name.startswith("SIG"):
            name = "SIG" + name
        number = getattr(signal, name, None)
        if isinstance(number, int):
            wanted.append(int(number))
    return wanted


def _requested_signals(signals: Optional[Iterable[int]] = None) -> List[int]:
    """The signals the receiving side should record.

    An explicit argument is the whole answer: it names the signals and
    nothing else.  Without one the environment *adds* to the defaults:
    ``$PDT_KILL_AUDIT_RECEIVE`` holds a comma-separated list of signal
    names or numbers, and the signals the audit exists to account for
    (:data:`DEFAULT_RECEIVED_SIGNALS`) are watched whether it is set or
    not.  A value that happens to be a number names a signal to watch
    *as well*; it never takes the ones that matter away.
    """
    if signals is not None:
        if isinstance(signals, (str, bytes)):
            text = (
                signals.decode("utf-8", "replace")
                if isinstance(signals, bytes) else signals
            )
            return _parse_signal_list(text)
        try:
            return [int(sig) for sig in signals]
        except (TypeError, ValueError):
            return []
    wanted = list(DEFAULT_RECEIVED_SIGNALS)
    for sig in _parse_signal_list(os.environ.get(KILL_AUDIT_RECEIVE_ENV, "")):
        if sig not in wanted:
            wanted.append(sig)
    return wanted


def install_signal_receipt_audit(
    signals: Optional[Iterable[int]] = None,
) -> None:
    """Record every signal this process **receives**.  Idempotent.

    The receiving side records the signal, puts the default disposition
    back and sends the same signal to this process -- so the process
    dies of it exactly as it would have died without the audit, and no
    signal path to any other process is created.

    A signal that somebody else already owns is chained, never
    replaced: the audit writes its record and then hands control back
    to the handler that was already there.
    """
    global _kill_audit_receiver_installed
    if _kill_audit_receiver_installed:
        return
    wanted = _requested_signals(signals)
    if not wanted:
        return
    installed = False
    for signum in wanted:
        if _install_receiver(signum):
            installed = True
    if installed:
        _kill_audit_receiver_installed = True
        _start_census_thread()


#: The name this function had while the receiving side was being
#: written; the callers that use it keep working.
install_signal_receiver_audit = install_signal_receipt_audit


def install_kill_audit() -> None:
    """Record every signal this process sends.  Idempotent.

    An audit hook rather than a call-site patch: the signal can be sent
    from a thread, from a library, or from a module that has no idea a
    gate is running, and the hook sees all of them.  It sees the *real*
    calls only -- a signal sent through a test double never reaches the
    kernel, and the double is not what killed the run.

    The receiving side is installed with it: a ledger that only records
    what this process *sent* cannot answer "who sent the signal that
    killed me", and a run that has to ask for the receiving side
    separately is a run that forgets to.
    """
    global _kill_audit_installed
    if _kill_audit_installed:
        return
    _kill_audit_installed = True

    def _hook(event: str, args) -> None:
        if event == "os.kill":
            kind = "kill"
        elif event == "os.killpg":
            kind = "killpg"
        else:
            return
        target = args[0] if args else None
        sig = args[1] if len(args) > 1 else None
        if isinstance(target, bool) or not isinstance(target, int):
            return
        if isinstance(sig, bool) or not isinstance(sig, int):
            sig = -1
        # Signal 0 is recorded like any other signal.  It asks the
        # kernel whether the pid exists and sends nothing at all, but it
        # is still a call that reached the kernel, and the account of
        # what this process asked the kernel to do does not get to omit
        # a class of calls (2026-11-08: the early return that skipped
        # ``sig == 0`` removed the most common kind of call from the
        # records, and the cases that pin "every kill is written down"
        # went red against it).
        #
        # The cost of writing them is bounded by KILL_AUDIT_MAX_RECORDS,
        # which is a cap rather than a filter: a cap keeps the newest
        # evidence, a filter would delete the evidence entirely.
        try:
            write_kill_audit(kind, target, sig)
        except Exception:  # noqa: BLE001 - the audit never blocks a signal
            pass

    sys.addaudithook(_hook)
    install_signal_receipt_audit()


install_kill_audit()