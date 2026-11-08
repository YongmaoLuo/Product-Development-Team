"""Build, write and read back the scene of an executor crash.

Why this module exists
----------------------
When the executor ends badly -- a non-zero exit, or a signal from the
kernel -- nothing about the death survives.  ``plan_execution.flags``
comes back empty, ``logs/executor_*.log`` stops half way through a
sentence, and there is no traceback anywhere to be found.  The
questions that follow ("which task was running?", "how long had it
been up?", "was it killed or did it fail?") have no answer, and the
answer is not reconstructed from the plan afterwards: the evidence was
never written down.

This module is the writing half of that repair.  It turns facts the
parent process already holds -- a return code, a pid, an argv, the
moment the run started -- into one JSON document, puts that document
under the plan's own directory, and reads the newest one back.

What it deliberately does not do
--------------------------------
It is **not** wired to the executor yet.  What it holds is the two
halves of the repair that can be tested on their own terms: the
primitives that write a report (:func:`write_crash_report` and the
functions around it), and :class:`CrashGuard`, the object that notices
a crash -- an uncaught exception, a signal, a non-zero exit -- and
feeds those primitives the facts only the process itself knows.

It collects **no environment variables** (a crash report is written
while the process is coming apart; an environment dump is a credential
dump), reads **no credentials**, and opens **no ``/proc`` file** (the
report has to be writable from a parent process that is not the one
that crashed, and ``/proc`` describes the wrong process anyway).

Three decisions worth naming
----------------------------
**The report has a fixed set of keys.**  Fifteen of them, and no more:
:data:`REPORT_KEYS`.  A crash report is read by a person under
pressure, minutes after the worst moment of their week; every key that
is not on the list is a key whose meaning nobody has written down.
:func:`build_crash_report` therefore *filters* what it is given -- a
field that is not on the list does not become a key of its own.

**The events are raw.**  ``last_events`` is the tail of
``execution.log`` as it sits on disk: not parsed, not normalised, not
re-serialised.  A forensics document that paraphrases its own evidence
is not evidence.  The one thing that is read out of those lines is
which task was running, and even that is a *guess* from the last line
that names a task -- which is why it is a separate field
(``active_task_id``) rather than a rewrite of the events.

**Writing can be switched off, and switching it off writes nothing.**
:data:`FORENSICS_ENV` is the switch.  Its absence means "on"; the
values ``0``, ``false`` and ``off`` mean "off", and when it is off
:func:`write_crash_report` returns ``None`` without creating the
directory, let alone a file.

Nothing here raises
-------------------
A crash report is written by a process that is already in trouble, and
read by one that is trying to work out what happened.  A report that
cannot be written is a missing report, not a second failure: every
function in this module answers "I have nothing" rather than raising.
"""

from __future__ import annotations

import faulthandler
import json
import os
import signal
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from execution_log_tail import MAX_TAIL_BYTES, read_tail_lines
from utils.atomic_io import atomic_write_json

#: The environment variable that switches crash reporting off.  Its
#: absence means reporting is on.
FORENSICS_ENV = "PDT_CRASH_FORENSICS"

#: The values of :data:`FORENSICS_ENV` that switch crash reporting off.
#: Compared case-insensitively and with surrounding whitespace removed,
#: because ``OFF`` and ``off`` are the same switch.
_DISABLED_VALUES = frozenset({"0", "false", "off"})

#: The keys a crash report carries, in the order it carries them.  This
#: is the whole contract: a report holds these fifteen keys and nothing
#: else.
REPORT_KEYS = (
    "exit_code",
    "signal",
    "signal_name",
    "killed_by",
    "pid",
    "argv",
    "started_at",
    "crashed_at",
    "uptime_seconds",
    "active_task_id",
    "last_events",
    "traceback",
    "faulthandler",
    "source",
    "plan_id",
)

#: How many events the tail of the execution log contributes by default.
DEFAULT_EVENT_LIMIT = 20

#: The prefix of a crash report's file name.  The name sorts
#: chronologically because the timestamp that follows it is fixed-width.
_CRASH_FILE_PREFIX = "executor_crash_"

#: The timestamp format of a crash report's file name: UTC, to the
#: microsecond, so two crashes in the same second do not collide.
_CRASH_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def forensics_enabled() -> bool:
    """Is crash reporting switched on?

    On unless :data:`FORENSICS_ENV` says otherwise: ``0``, ``false`` and
    ``off`` (in any case, with surrounding whitespace ignored) switch it
    off.  An absent variable means on -- the switch is there to stop
    the writing on a machine that should not be writing, not to start
    it on a machine that has not heard of it.
    """
    raw = os.environ.get(FORENSICS_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def crash_report_path(plan_dir: Path, now: Optional[datetime] = None) -> Path:
    """Return the path a crash report for ``plan_dir`` is written to.

    The file lives beside the executor's own log, in ``logs/``, and its
    name carries the moment of the crash in UTC to the microsecond::

        <plan_dir>/logs/executor_crash_20261108T120000000000Z.json

    UTC because a crash is read against a timeline, and a timeline that
    shifts with the reader's timezone is not a timeline.  The
    microseconds because two crashes in the same second must not
    overwrite each other -- and because a fixed-width name sorts
    chronologically, which is what makes "the newest report" a name
    comparison rather than a stat().

    ``now`` is accepted so a caller (and a test) can pin the moment;
    the default is the current UTC time.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        # A naive moment is read as UTC, the same convention the log
        # writer uses when it stamps a line.
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime(_CRASH_TIMESTAMP_FORMAT)
    return Path(plan_dir) / "logs" / f"{_CRASH_FILE_PREFIX}{stamp}.json"


def tail_execution_events(
    plan_dir: Path, limit: int = DEFAULT_EVENT_LIMIT
) -> List[str]:
    """Return the last ``limit`` non-empty lines of the plan's log.

    The lines come back exactly as they are on disk: not parsed, not
    normalised, not re-serialised.  At most 256 KiB of the log is read,
    from its end, so the cost of the answer does not grow with the
    length of the run.

    A plan with no ``execution.log``, an empty one, or one that cannot
    be read yields an empty list.  The caller is a process trying to
    work out what happened; "there is no log" is a fact it can use.
    """
    try:
        count = int(limit)
    except (TypeError, ValueError):
        return []
    if count <= 0 or plan_dir is None:
        return []
    try:
        log_path = Path(plan_dir) / "execution.log"
    except TypeError:
        return []
    return read_tail_lines(log_path, max_lines=count, max_bytes=MAX_TAIL_BYTES)


def describe_exit(returncode: int) -> Dict[str, Any]:
    """Describe how a child process ended.

    A process killed by a signal is reported by the operating system as
    a negative return code -- ``-9`` for ``SIGKILL`` -- and a process
    that exited on its own is reported as the exit code it chose.  The
    two are different events and the report says which happened:

    * exited cleanly -- ``{'exit_code': 0, 'signal': None, ...}``;
    * exited with a code -- ``killed_by`` is ``'exit_code'``;
    * killed by a signal -- ``killed_by`` is ``'signal'``, and the
      signal is named (``SIGKILL``, ``SIGTERM``, ``SIGINT``) rather
      than numbered, because a report that says "9" is one more thing
      for the reader to look up.

    A signal number the platform does not know is written as
    ``SIG<n>`` rather than raising: the number is the fact, the name is
    a convenience.
    """
    try:
        code = int(returncode)
    except (TypeError, ValueError):
        return {
            "exit_code": None,
            "signal": None,
            "signal_name": None,
            "killed_by": None,
        }

    if code == 0:
        return {
            "exit_code": 0,
            "signal": None,
            "signal_name": None,
            "killed_by": None,
        }

    if code > 0:
        return {
            "exit_code": code,
            "signal": None,
            "signal_name": None,
            "killed_by": "exit_code",
        }

    number = abs(code)
    try:
        name = signal.Signals(number).name
    except (ValueError, TypeError):
        name = f"SIG{number}"
    return {
        "exit_code": None,
        "signal": number,
        "signal_name": name,
        "killed_by": "signal",
    }


def derive_active_task_id(last_events: Sequence[str]) -> Optional[str]:
    """Return the task the last events name, if any of them name one.

    The search runs backwards: the newest line that names a task is the
    one that was running when the run stopped.  A line that is not JSON,
    or that names no task, is skipped -- the run's log holds both kinds
    of line, and "the last line" is not the same thing as "the last
    line about a task".

    This is a reading of the log, not a certainty: the answer is a
    guess made from the newest evidence, and it is reported as its own
    field rather than by rewriting the events.
    """
    try:
        lines = list(last_events or [])
    except TypeError:
        return None

    for line in reversed(lines):
        if not isinstance(line, str):
            continue
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        task_id = parsed.get("task_id")
        if task_id is None:
            continue
        if isinstance(task_id, str):
            if not task_id.strip():
                continue
            return task_id
        return str(task_id)
    return None


def build_crash_report(**fields: Any) -> Dict[str, Any]:
    """Build a crash report from the facts that are known.

    The report holds exactly the fifteen keys of :data:`REPORT_KEYS`, in
    that order.  A field that was not given is ``None``; a field that is
    not on the list does not become a key of its own, whatever it was
    called.

    Two of the fields can be worked out from the others when the caller
    did not supply them:

    * ``last_events`` -- the tail of the plan's ``execution.log``, read
      from ``plan_dir`` (which is an input to this function, not a key
      of its report);
    * ``active_task_id`` -- the task the newest of those events names.

    Everything else is the caller's to state, because everything else
    is a fact only the parent process knows: the pid, the argv, the
    moment the run started, how long it ran.
    """
    report: Dict[str, Any] = {key: fields.get(key) for key in REPORT_KEYS}

    if "last_events" in fields:
        last_events = fields.get("last_events")
    else:
        plan_dir = fields.get("plan_dir")
        last_events = tail_execution_events(plan_dir) if plan_dir is not None else []
    if not isinstance(last_events, list):
        last_events = []
    report["last_events"] = last_events

    if "active_task_id" not in fields:
        report["active_task_id"] = derive_active_task_id(last_events)

    return report


def write_crash_report(plan_dir: Path, report: Dict[str, Any]) -> Optional[Path]:
    """Write ``report`` under ``plan_dir`` and return the path it took.

    The directory is created if it is not there.  The write is atomic
    (a sibling tempfile, then a rename), because a reader that finds a
    half-written crash report has found nothing at all.

    ``None`` is returned when the report could not be written, and
    nothing is raised: the caller is usually a process that is already
    in trouble, and a second failure on the way out is worse than a
    missing report.  ``None`` is also returned when reporting is
    switched off -- and in that case not even the directory is created.
    """
    if not forensics_enabled():
        return None

    try:
        target = crash_report_path(plan_dir)
        # ``default=str`` keeps a field the caller filled with an
        # unexpected type from costing the whole report.
        payload = json.loads(json.dumps(report, default=str))
    except (TypeError, ValueError, OSError):
        return None

    try:
        atomic_write_json(target, payload, reraise=True)
    except (OSError, TypeError, ValueError):
        return None
    return target


def latest_crash_report(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """Return the newest crash report written under ``plan_dir``.

    The reports are ordered by file name, which is ordered by time, so
    "newest" is the last name in the sort.  A report that cannot be
    parsed is skipped and the search continues with the one before it:
    a truncated file at the top of the list must not hide the report
    underneath it.

    ``None`` means there is no report to read -- no directory, no
    report, or no report that could be read.
    """
    try:
        logs_dir = Path(plan_dir) / "logs"
        candidates = sorted(
            logs_dir.glob(f"{_CRASH_FILE_PREFIX}*.json"), key=lambda item: item.name
        )
    except (OSError, TypeError, ValueError):
        return None

    for path in reversed(candidates):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalise_argv(argv: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Return ``argv`` as a list of strings, or ``None`` if there is none.

    A caller that knows the command line says so; a caller that does
    not leaves the field unknown rather than claiming the process was
    started with an empty command line, which is a different statement.
    """
    if argv is None:
        return None
    if isinstance(argv, str):
        # One command, not a sequence of characters.
        return [argv]
    try:
        return [str(item) for item in argv]
    except TypeError:
        return None


def _iso(moment: datetime) -> str:
    """Return ``moment`` as an ISO-8601 UTC timestamp."""
    try:
        return moment.astimezone(timezone.utc).isoformat()
    except (AttributeError, ValueError, OSError):
        return str(moment)


class CrashGuard:
    """Notice a crash and write down what happened.

    The primitives around it know how to write a report; this object
    knows when a report is owed.  A run can end badly in three ways,
    and each of them is a separate entrance:

    * :meth:`on_exception` -- an exception that was not handled.  The
      evidence is the traceback.
    * :meth:`on_signal` -- a signal from the kernel.  The evidence is
      the Python stack, and the only way to get a stack out of a
      process that is being torn down is ``faulthandler``.
    * :meth:`on_exit_code` -- an ordinary non-zero exit.  The evidence
      is the exit code itself.

    What the guard adds to the primitives is the three facts that only
    the process that died knows: the moment it started, its pid, and
    its command line.  :meth:`install` records them; the ``on_*``
    methods turn them into a report.

    **The guard writes one report and stops.**  A process can trip two
    triggers -- an exception that is caught on the way out and a
    non-zero exit that follows it -- and the second one is a
    continuation of the first, not a second crash.  So the first
    successful write is the last one; every later trigger answers
    ``None``.

    **The guard does not take over the process's ``faulthandler``.**
    ``faulthandler`` is process-global state: pytest installs one, and
    a caller that enabled it chose a file on purpose.  The guard
    installs one only when the process has none, and :meth:`close`
    hands the process back the way it found it.

    Nothing here raises.  Every method of this class is called from a
    process that is already in trouble, and a second failure on the
    way out is worse than a missing report.
    """

    #: The ``source`` every report this guard writes carries.  The
    #: parent's own fallback report says ``'parent'`` instead.
    SOURCE = "executor"

    def __init__(
        self,
        plan_dir: Path,
        plan_id: str,
        argv: Optional[Sequence[str]] = None,
    ) -> None:
        self.plan_dir = Path(plan_dir)
        self.plan_id = plan_id
        self.argv = _normalise_argv(argv)
        # The stack dump file.  It is a temporary file rather than a
        # path under the plan: the guard may be running in a process
        # that is about to die, and it must not leave a half-written
        # file behind for the reader to trip over.
        self._fd = None
        # Whether *this* guard turned the process's ``faulthandler`` on.
        # Only then may :meth:`close` turn it off again.
        self._enabled_by_us = False
        # Set when the platform has no usable ``faulthandler``: the
        # report then carries no stack, but it is still written.
        self._faulthandler_unavailable = False
        self._already_written = False
        self._started_at: Optional[datetime] = None
        self._pid: Optional[int] = None

    # -- the life of the guard ------------------------------------------

    def install(self) -> None:
        """Record the facts only this process knows, and arm the stack dump.

        Three things happen here, and none of them is allowed to
        raise: the start time and the pid are recorded, a temporary
        file is opened for the stack dump, and ``faulthandler`` is
        enabled **only if the process has no ``faulthandler`` of its
        own**.  A process that already has one chose that file on
        purpose -- pytest installs one, the CI watchdog registers a
        SIGUSR1 handler on it -- and replacing it would take away a
        debugging tool the caller is relying on.
        """
        try:
            self._started_at = datetime.now(timezone.utc)
            self._pid = os.getpid()
        except Exception:
            # A guard that cannot state when it started still works; it
            # simply has less to say.
            pass

        try:
            self._fd = tempfile.TemporaryFile()
        except (OSError, ValueError):
            self._fd = None

        try:
            already_enabled = bool(faulthandler.is_enabled())
        except Exception:
            already_enabled = True

        if already_enabled:
            # Somebody else's ``faulthandler``; leave it alone.
            return
        if self._fd is None:
            # Nothing to enable it *to*.  Enabling it to stderr would
            # silently redirect a process-global tool.
            self._faulthandler_unavailable = True
            return
        try:
            faulthandler.enable(file=self._fd)
            self._enabled_by_us = True
        except Exception:
            # No ``faulthandler`` on this platform, or it refused: the
            # report loses its stack and keeps everything else.
            self._enabled_by_us = False
            self._faulthandler_unavailable = True

    def close(self) -> None:
        """Release the stack dump file and hand ``faulthandler`` back.

        The process's ``faulthandler`` state is restored only if this
        guard changed it.  Closing twice is not an error.
        """
        handle = self._fd
        self._fd = None
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        if self._enabled_by_us:
            self._enabled_by_us = False
            try:
                faulthandler.disable()
            except Exception:
                pass

    # -- the three entrances --------------------------------------------

    def on_exception(self, exc: BaseException) -> Optional[Path]:
        """Write the report for an uncaught exception.

        The traceback is the evidence, so it is formatted here rather
        than left to the caller: the caller is a process that is
        already unwinding, and asking it to remember to call
        ``traceback.format_exception`` is asking it to remember
        something at exactly the wrong moment.
        """
        return self._record(
            killed_by="exception",
            exit_code=None,
            signal=None,
            signal_name=None,
            traceback=self._format_traceback(exc),
        )

    def on_exit_code(self, code: int) -> Optional[Path]:
        """Write the report for a non-zero exit.

        A clean exit -- code zero -- is not a crash and writes
        nothing.  The exit code is the whole of the evidence, so
        nothing else is added to it.
        """
        try:
            number = int(code)
        except (TypeError, ValueError):
            return None
        if number == 0:
            return None
        return self._record(
            killed_by="exit_code",
            exit_code=number,
            signal=None,
            signal_name=None,
        )

    def on_signal(self, signum: int) -> Optional[Path]:
        """Write the report for a process killed by a signal.

        The stack is dumped first, while the process is still standing
        there to be dumped, and the text is read back afterwards: the
        dump goes straight to the file descriptor, so the file object
        has to be rewound before its contents can be read as text.

        The signal is *named* rather than numbered, because a report
        that says "15" is one more thing for the reader to look up.
        """
        try:
            number = int(signum)
        except (TypeError, ValueError):
            return None
        described = describe_exit(-number)
        return self._record(
            killed_by="signal",
            exit_code=None,
            signal=described.get("signal"),
            signal_name=described.get("signal_name"),
            faulthandler=self._dump_stack(),
        )

    # -- the parts that do the work -------------------------------------

    @staticmethod
    def _format_traceback(exc: BaseException) -> Optional[str]:
        """Return the formatted traceback of ``exc``, or ``None``."""
        try:
            return "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        except Exception:
            return None

    def _dump_stack(self) -> Optional[str]:
        """Return the stack of every thread, as text.

        ``None`` means there is no stack to report: the guard was
        never installed, the platform has no ``faulthandler``, or the
        dump itself failed.  All three are the same thing to the
        reader -- the report has no stack in it -- and all three are
        worth less than the report itself.
        """
        handle = self._fd
        if handle is None or self._faulthandler_unavailable:
            return None
        try:
            faulthandler.dump_traceback(file=handle, all_threads=True)
        except Exception:
            return None
        try:
            handle.seek(0)
            raw = handle.read()
        except Exception:
            return None
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            return None
        return text or None

    def _record(self, **fields: Any) -> Optional[Path]:
        """Write the report, once, and return the path it took.

        Every fact the report needs is assembled here: the ones the
        guard recorded at :meth:`install`, the ones this trigger
        supplies, and the two timestamps the report is read against.
        """
        try:
            if not forensics_enabled():
                return None
            if self._already_written:
                return None

            now = datetime.now(timezone.utc)
            # A guard that was never installed started at the moment of
            # the crash, so it ran for no time at all.
            started_at = self._started_at if self._started_at is not None else now
            pid = self._pid if self._pid is not None else os.getpid()

            report = build_crash_report(
                plan_dir=self.plan_dir,
                plan_id=self.plan_id,
                source=self.SOURCE,
                pid=pid,
                argv=self.argv,
                started_at=_iso(started_at),
                crashed_at=_iso(now),
                uptime_seconds=(now - started_at).total_seconds(),
                **fields,
            )
            path = write_crash_report(self.plan_dir, report)
        except Exception:
            return None

        if path is not None:
            self._already_written = True
        return path

