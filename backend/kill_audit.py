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
  its own, so a reader can take the file line by line.
* **The audit never blocks a signal.**  A record that cannot be written
  is a record nobody will read; it must not cost the signal it was
  meant to describe.  Every failure here is swallowed.

The directory the records go to is ``$PDT_KILL_AUDIT_DIR`` when it is
set, and a fixed directory under the platform's temporary directory when
it is not -- so every process of one run can be pointed at one place by
the environment, and a process that runs on its own still writes
somewhere.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

#: The environment variable that says where the audit records go.
KILL_AUDIT_DIR_ENV = "PDT_KILL_AUDIT_DIR"

#: The name of the directory the records go to when the environment
#: does not name one.
KILL_AUDIT_DIR_NAME = "pdt-kill-audit"

#: The prefix of the file one process writes its records into.
KILL_AUDIT_FILE_PREFIX = "kill-audit-"

#: How many records one process writes.  A cap, not a budget: the audit
#: is evidence for the last kill, and a runaway process must not fill
#: the disk while the run it is in is still going.
KILL_AUDIT_MAX_RECORDS = 20000

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


_kill_audit_lock = threading.Lock()
_kill_audit_count = 0
_kill_audit_installed = False


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
        directory = kill_audit_dir()
        path = directory / f"{KILL_AUDIT_FILE_PREFIX}{os.getpid()}.jsonl"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # The lock is held across the write, so two threads of this
            # process cannot put half a record into the file: a record
            # that is not a whole line is a record nobody can read.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # A record that cannot be written is a record nobody will
            # read, and it must not cost the signal it was meant to
            # describe.
            pass


def write_kill_audit(kind: str, target: int, sig: int) -> None:
    """Write one kill record down.  Never raises."""
    try:
        record = {
            "kind": str(kind),
            "target": int(target),
            "signal": int(sig),
            "outcome": OUTCOME_ATTEMPTED,
            "pid": os.getpid(),
            "ts": datetime.now().isoformat(),
            "stack": _kill_audit_stack(),
        }
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
    knows them — so it writes them here.

    Args:
        kind: ``"kill"`` or ``"killpg"`` — which call was made.
        target: The pid (or group id) that was signalled.
        sig: The signal number.
        outcome: One of the ``OUTCOME_*`` values.
        errno: The errno the kernel answered with, when it answered
            with one.
    """
    try:
        record = {
            "kind": str(kind),
            "target": int(target),
            "signal": int(sig) if isinstance(sig, int) else -1,
            "outcome": str(outcome),
            "errno": int(errno) if isinstance(errno, int) else None,
            "pid": os.getpid(),
            "ts": datetime.now().isoformat(),
            "stack": _kill_audit_stack(),
        }
    except Exception:  # noqa: BLE001 - the audit never blocks a signal
        return
    _append_record(record)


def install_kill_audit() -> None:
    """Record every signal this process sends.  Idempotent.

    An audit hook rather than a call-site patch: the signal can be sent
    from a thread, from a library, or from a module that has no idea a
    gate is running, and the hook sees all of them.  It sees the *real*
    calls only -- a signal sent through a test double never reaches the
    kernel, and the double is not what killed the run.
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
        try:
            write_kill_audit(kind, target, sig)
        except Exception:  # noqa: BLE001 - the audit never blocks a signal
            pass

    sys.addaudithook(_hook)


install_kill_audit()
