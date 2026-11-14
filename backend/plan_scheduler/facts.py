"""backend.plan_scheduler.facts — the plan facts, read and derived.

Background
----------
The scheduler answers five questions over and over: which plans are
waiting, which plans are working, which plan is waiting for a human,
which directory a plan works in, and what starting a plan needs.

None of those answers is the scheduler's to keep.  They are already
written down — the phase in ``plan_routing``, the subprocess and the
working directory in ``plan_execution``, the verification round in
``plan_verification``, the human questions in ``plan_decisions`` — and
the scheduler *derives* what it needs from them, every tick, from the
database.

A copy would be a second answer to the same question, and two answers
drift.  The scheduler would keep saying a plan is queued while the plan
had moved on, and the operator's only way to find out which of the two
was right would be to watch the wrong plan start.  So this module
remembers nothing *about the rows*: every call reads them again, and
every value it returns is a value that is in the database at that
moment.  Restarting the scheduler changes nothing, because there is
nothing to lose.  The two things that are remembered are which plans
have already been complained about, so that a plan whose directory is
missing is reported once rather than on every tick, and a plan's
creation time, which comes from the filesystem and cannot change under
it.

The five questions
------------------
``queued_plans()``
    Which plans are waiting to run.  The *set* comes from
    ``plan_routing`` — a plan whose phase is ``queued`` is a queued
    plan even before it has a queue row — and the order comes from the
    shared comparator in :mod:`plan_scheduler.queue`, so the panel, the
    admission check and the restart recovery cannot disagree about who
    runs next.  Each row carries the ``phase`` the plan is in, as the
    routing row wrote it.
``active_plans()``
    Which plans are holding compute.  Two kinds of work hold compute:
    an execution subprocess, and a verification round.  The two sets are
    unioned and counted per plan, because a plan that does both holds
    one slot, not two.  Each row also carries the ``started_at`` of the
    plan's execution row — the moment the plan really began, which the
    slot clock loses on every restart — and the ``phase`` the plan is
    in, so that a caller can say what the plan is doing and not only
    that it is working.
``has_pending_decision(plan_id)``
    Is a human being asked about this plan?  The answer is a *guard*,
    not a release: a pending decision keeps a plan from being mistaken
    for idle, but it never frees a slot on its own.
``workspace_key(plan_id)``
    Which directory does this plan work in?  One key per directory,
    whatever names that directory goes by.
``start_spec(plan_id)``
    What starting this plan needs — derived, not remembered.

The phase is passed on as it is
-------------------------------
Both kinds of row carry the phase the plan is in, read from
``plan_routing.current_phase`` by the same :meth:`PlanFactsReader._phase`
every other reader of the phase uses.  The facts layer does not map a
phase to a label and does not translate a phase it has never seen: a
label is the routing layer's business, and a second mapping here would
be a second answer to the same question.  A plan with no routing row
has no phase, and the row says ``None`` — the reader has no opinion
about a plan it cannot date, and raising over a row that is allowed to
be absent would take the scheduler down.

The two sides of "is this plan working?"
----------------------------------------

**The execution side** is the subprocess: ``exec_status`` says running
and ``exec_pid`` names a process that is still there.  The pid is
probed with ``kill(pid, 0)`` rather than trusted, because ``exec_pid``
outlives the process it names — the row is cleared when the execution
side writes a terminal status, and a crash never gets that far.  A pid
that is gone means the plan is not working any more.

**The verification side** is the verification thread: the plan is in
one of the five verification phases, ``plan_verification`` says the
round is running, and the row has been updated recently enough to
believe.  The thread beats every 30 seconds, so a row that has not
been updated for 15 minutes belongs to a thread that died — and a
dead thread holds nothing.

A verification round has no subprocess at all, and that is exactly why
it has to be counted separately: it is calling a provider and burning
CPU and memory, so leaving it out would allow "four plans verifying
plus four plans executing" under a limit of four.

When a plan satisfies both sides it is still **one** plan, and it
appears once.  The execution side wins the tie: a live process is the
stronger evidence of compute being held, and releasing the slot while
that process is still running would be a slot that is not free.

The third source: the injected live set
---------------------------------------
The two sides above are read from the rows, and the rows are only as
fresh as the last time somebody wrote them.  A verification round that
is started directly — not by the scheduler's own tick — may run for
longer than the freshness window without refreshing its row, and when
it does, the plan **vanishes** from the active set even though it is
still calling a provider and burning CPU and memory.  A scheduler that
cannot see a working plan admits another one on top of it, and the
limit it enforces is not the limit that is real.

Making the freshness window longer is not the answer: a longer window
is still a guess about how long a live round may be quiet, and it has
to be wrong in the safe direction every time it is wrong.  The answer
is to ask the thing that actually knows — the process and the loop
that are alive — and to take the **union** of that answer with the
rows' answer, so neither source can subtract from the other.

The live set is therefore *injected*: the reader is handed a callback
that answers "which plan ids are alive right now?", and it asks the
callback on every call.  The reader keeps its promise of holding no
state about the plans it reports — the callback is asked every time,
exactly like the rows are read every time.  A callback that raises
degrades to the rows' answer and is reported as a warning: the
callback is foreign code, and foreign code must not be able to take
the scheduler down.

The workspace key
-----------------
Two plans may not work in the same directory at the same time, and the
directory goes by several names: the path itself, a symlink to it, a
symlink to that symlink.  The key is therefore the *real* path —
``realpath`` and ``normpath`` — so the three names are one key and the
constraint survives a restart, when nothing is in memory to compare.

The key is derived from the plan's own facts, in this order: the
execution row's ``project_dir``, then the ``project_dir`` the task
list declares, then the workspace the PRD declares as a constraint.
The first one that says something wins.

The two facts a refusal is made of
--------------------------------
``import_failed_reason(plan_id)``
    Why this plan's import failed, in the import's own words.  The
    import wrote the reason on the plan's execution row and kept no
    other copy, so the row is where the reason is read from — and the
    words are passed through unchanged, because a reason that has been
    reworded is not the reason.
``plan_directory_exists(plan_id)``
    Whether this plan's directory is on disk.  This is the stricter
    question than "where would the directory be?": a directory that
    holds none of the plan's own files is not a plan's directory,
    however much of a directory it is.  The two answers together are
    what lets the command layer tell a plan that is not there apart
    from one that is there and cannot be run.

The one thing that is kept
--------------------------
``plan_created_at()`` answers from the filesystem rather than from the
database — the plan's directory is its own record of when it was made
— and it keeps what it read, so that the directory is stat'ed once per
plan rather than once per tick.

That is not the copy this module refuses to make.  A copy of a *row*
is a second answer to a question the row may change; a directory's
creation time does not change while the directory is there, so the
second answer would be the first answer, and the reader is not
answering about something that can move under it.  Every other answer
is derived again, on every call, from the rows that are in the database
at that moment.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .queue import queue_key

log = logging.getLogger(__name__)

__all__ = [
    "PlanFactsReader",
    "IMPORT_FAILED_PHASE",
    "QUEUED_PHASE",
    "VERIFICATION_PHASES",
    "VERIFICATION_STALE_SECONDS",
]


#: The phase a plan is in while it waits for its turn to run.
QUEUED_PHASE = "queued"

#: The phase a plan is in when its import failed.  The import left the
#: plan's directory on disk and registered the plan with the reason it
#: could not be imported; the plan is never admitted to the queue.
#:
#: The name is the one ``plan_state.VALID_PHASES`` carries — the phase
#: is a fact of the plan, and the scheduler reads it rather than
#: inventing a second word for it.
IMPORT_FAILED_PHASE = "import_failed"

#: The files a plan's own directory carries.  A directory that has one
#: of them holds a plan's material; a directory that has none of them
#: is a directory that happens to be there.
_PLAN_FILE_NAMES = (
    "tasks.json",
    "prd.json",
    "import.json",
    "interview.json",
)

#: The phases in which a plan is being verified.  In each of them the
#: verification thread is the thing working on the plan, and the plan
#: holds compute even though no execution subprocess exists.
VERIFICATION_PHASES = frozenset(
    {
        "verification_running",
        "verify_first_pass",
        "verify_recheck",
        "verification_repairing",
        "verification_rerunning",
    }
)

#: How long a verification row may go without an update before the
#: verification thread behind it is presumed dead.  The thread beats
#: every 30 seconds, so a quarter of an hour of silence is not a pause.
VERIFICATION_STALE_SECONDS = 900.0

#: The ``exec_status`` values that mean "the execution subprocess is
#: running".  The repository clears ``exec_pid`` when the status turns
#: terminal, but a crash never gets that far — so the status says what
#: was intended and the pid says what is still there.
_RUNNING_EXEC_STATUSES = frozenset({"running"})

#: The side a plan is reported on when only the injected live set says
#: it is working.  A plan nobody can see a subprocess for is a plan
#: whose compute is held by a verification round, so that is the side
#: it is reported on.
_LIVE_SIDE = "verification"

#: The sort key a plan without a queue row gets.  It has not been
#: enqueued yet, so it belongs behind everything that has.
_NO_SEQ = 1 << 62

#: The constraint keys a PRD may declare its workspace under, in the
#: order they are read.  The same shapes the task generator reads, so
#: a plan declared one way is understood one way.
_WORKSPACE_CONSTRAINT_KEYS = (
    "target_project_dir",
    "primary_project_dir",
    "project_dir",
)

#: The columns of ``plan_routing`` this module reads.
_ROUTING_COLUMNS = ("plan_id", "current_phase")

#: The columns of ``plan_execution`` this module reads.
_EXECUTION_COLUMNS = (
    "plan_id",
    "exec_pid",
    "exec_status",
    "project_dir",
    "next_run_at",
    "started_at",
)

#: The columns of ``plan_verification`` this module reads.
_VERIFICATION_COLUMNS = ("plan_id", "verification_status", "updated_at")


def _as_dict(columns: Tuple[str, ...], row: Any) -> Dict[str, Any]:
    """Turn a selected row into a plain dict.

    The columns are named here rather than taken from the row, so the
    result does not depend on the connection's ``row_factory`` — the
    production connection hands out plain tuples.
    """
    return dict(zip(columns, tuple(row)))


def _started_at_text(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the start time an execution row holds, or ``None``.

    The stamp is passed through as it was written — a naive local-time
    ISO string — and is not parsed here: parsing it is the caller's
    business, and a reader that guessed a time zone would report an
    elapsed time wrong by exactly the offset it guessed.

    A row without a start time, and a row whose start time is blank,
    both answer ``None``: there is nothing to pass through.
    """
    if row is None:
        return None
    value = row.get("started_at")
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _iso_from_epoch(epoch: float) -> str:
    """Return the epoch seconds as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _epoch_from_iso(text: Any) -> Optional[float]:
    """Return the epoch seconds of an ISO-8601 timestamp.

    ``None`` means "this is not a timestamp I can read" — and a reader
    that cannot read the heartbeat cannot claim the thread is alive.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _pid_is_alive(pid: Any) -> bool:
    """Return whether the process ``pid`` names is still running.

    ``kill(pid, 0)`` asks the kernel the question without touching the
    process: a live pid answers, a dead one raises
    :class:`ProcessLookupError`.  A :class:`PermissionError` means the
    process exists but belongs to somebody else — it is running, so it
    counts.

    A pid that is not a positive number never names a process:
    ``kill(0, 0)`` would ask about *this* process group, and
    ``kill(-1, 0)`` about every process there is.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_json(path: Path) -> Optional[Any]:
    """Read a JSON file, or return ``None`` if it cannot be read.

    A plan's files are written by other processes; one that is missing,
    half-written or not JSON at all must not take the scheduler down —
    the reader answers "I don't know" and the caller decides.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _constraint_texts(constraints: Any) -> List[str]:
    """Flatten a constraints block into a list of ``key=value`` strings.

    The block may be a mapping, a ``";"``-delimited string, or a list
    of either — the shapes a PRD or an interview may carry.
    """
    if isinstance(constraints, dict):
        return [f"{key}={value}" for key, value in constraints.items()]
    if isinstance(constraints, str):
        return [part for part in constraints.split(";") if "=" in part]
    if isinstance(constraints, (list, tuple)):
        texts: List[str] = []
        for item in constraints:
            if isinstance(item, str) and "=" in item:
                texts.append(item)
            elif isinstance(item, dict):
                texts.extend(f"{key}={value}" for key, value in item.items())
        return texts
    return []


def _workspace_from_constraints(constraints: Any) -> Optional[str]:
    """Return the workspace a constraints block declares, if it declares one.

    The declared keys are read in priority order, and any other
    ``*_project_dir`` key is read after them.
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for text in _constraint_texts(constraints):
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip()
        if not value or key in seen:
            continue
        if key in _WORKSPACE_CONSTRAINT_KEYS or key.endswith("_project_dir"):
            pairs.append((key, value))
            seen.add(key)

    def _rank(pair: Tuple[str, str]) -> int:
        if pair[0] in _WORKSPACE_CONSTRAINT_KEYS:
            return _WORKSPACE_CONSTRAINT_KEYS.index(pair[0])
        return len(_WORKSPACE_CONSTRAINT_KEYS)

    if not pairs:
        return None
    pairs.sort(key=_rank)
    return pairs[0][1]


class PlanFactsReader:
    """Read the plan facts and derive what the scheduler needs from them.

    The reader holds no state about the plans it reports.  It has a
    connection it reads through and the directory the plans live in,
    and every answer it gives is derived from the rows that are in the
    database at that moment.  The two things it keeps are which plans
    it has already complained about, so that a plan whose directory is
    missing is reported once rather than on every tick, and a plan's
    creation time, which comes from the filesystem and cannot change
    under it.

    Attributes:
        conn: the connection to read on — the one the scheduler opens
            in production (autocommit, WAL, ``busy_timeout``).
        project_root: the plans root: the directory that holds one
            subdirectory per plan.  A repository root that holds a
            ``plans`` directory is understood as well.
        clock: the source of "now", in epoch seconds.  It is a
            parameter so that the freshness rule can be exercised
            without waiting fifteen minutes for it.
        live_plan_ids: the callback that names the plans which are
            alive right now, or ``None`` when there is no such source.
            It is asked on every call to :meth:`active_plans`, and its
            answer is unioned with the rows' answer rather than
            replacing it.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        project_root: Any,
        clock: Optional[Callable[[], float]] = None,
        live_plan_ids: Optional[Callable[[], set[str]]] = None,
    ) -> None:
        """Bind the reader to a connection and to the plans tree."""
        self._conn = conn
        self._project_root = Path(project_root)
        self._clock = clock if clock is not None else time.time
        #: The callback that names the plans which are alive right
        #: now.  ``None`` means there is no such source, and the
        #: reader answers from the rows alone.
        self._live_plan_ids_source = live_plan_ids
        self._missing_dir_warned: set[str] = set()
        #: The creation time of each plan, read once.  It is a fact
        #: about the filesystem that cannot drift — a directory's
        #: creation time does not change while the directory is there.
        self._created_at_cache: Dict[str, Optional[float]] = {}

    # ------------------------------------------------------------------
    # The queued plans
    # ------------------------------------------------------------------

    def queued_plans(self) -> List[Dict[str, Any]]:
        """Return the plans that are waiting to run, in the queue's order.

        A plan is waiting when its *own* phase is ``queued`` — the phase
        lives in ``plan_routing``, and a plan that was imported before
        the scheduler's first tick is waiting even though it has no
        queue row yet.  The scheduler's queue table then says where in
        the line the plan stands: ``seq`` and ``priority`` come from it,
        and a plan without a row has neither (``seq`` is ``None``: it
        has not been enqueued yet, and inventing a number would be a
        number nobody handed out).

        ``due`` says whether the plan may start now.  A plan with no
        ``next_run_at`` is due — nothing has been scheduled for it, so
        nothing holds it back — and a plan whose ``next_run_at`` has
        passed is due; a plan scheduled for later is not.

        A plan whose directory is not under the plans root is not a
        plan any more, so it is not waiting either: it is dropped from
        the answer, and the operator is warned about it once.  A ghost
        left at the head of the queue would block every plan behind it
        for as long as the scheduler runs.

        Each row also carries the ``phase`` the plan is in — the
        routing row's own value, passed on as it is.  The facts layer
        does not map a phase to a label: a label is the routing
        layer's business, and a second mapping here would be a second
        answer to the same question.

        Returns:
            The queued plans, each a dict of ``plan_id``, ``priority``,
            ``seq``, ``due`` and ``phase``, in the order the shared
            comparator gives.  Empty when nothing is waiting.
        """
        rows = self._conn.execute(
            "SELECT r.plan_id, q.seq, q.priority, q.promote_order, "
            "       e.next_run_at "
            "FROM plan_routing r "
            "LEFT JOIN scheduler_queue q ON q.plan_id = r.plan_id "
            "LEFT JOIN plan_execution e ON e.plan_id = r.plan_id "
            "WHERE r.current_phase = ?",
            (QUEUED_PHASE,),
        ).fetchall()

        now = self._now_iso()
        entries: List[Tuple[Tuple[bool, int, int, int], Dict[str, Any]]] = []
        for row in rows:
            plan_id, seq, priority, promote_order, next_run_at = tuple(row)
            if self._plan_dir(plan_id) is None:
                self._warn_missing_plan_dir(plan_id)
                continue
            entries.append(
                (
                    _queue_sort_key(seq, priority, promote_order),
                    {
                        "plan_id": plan_id,
                        "priority": int(priority) if priority is not None else 0,
                        "seq": int(seq) if seq is not None else None,
                        "due": _is_due(next_run_at, now),
                        # The phase the plan is in, read from the
                        # routing row and passed on as it is.
                        "phase": self._phase(plan_id),
                    },
                )
            )

        entries.sort(key=lambda entry: entry[0])
        return [payload for _, payload in entries]

    # ------------------------------------------------------------------
    # The active plans
    # ------------------------------------------------------------------

    def active_plans(self) -> List[Dict[str, Any]]:
        """Return the plans that are holding compute, one row per plan.

        The active set is the union of the execution side (a running
        subprocess) and the verification side (a running verification
        round).  A plan that satisfies both is **one** plan and appears
        once — it holds one slot, not two.

        The injected live set joins that union.  It names the plans
        that are alive right now even when their rows have gone quiet,
        so a verification round that outlasts the freshness window
        does not disappear from the answer and hand its slot to
        somebody else.  A plan that both the rows and the callback name
        is still **one** plan: the rows win the tie, because a running
        subprocess is the stronger evidence of compute being held.

        Each row carries the ``side`` it is active on — ``execution`` or
        ``verification`` — and the ``workspace`` key it works in, so a
        caller can tell which rule releases it: the execution side is
        released when the process is gone, the verification side when
        the round stops.  A plan that only the callback names is
        reported on the verification side: it holds compute, but no
        subprocess holds it.

        Each row also carries the ``started_at`` of the plan's
        execution row: the moment the plan really started, which the
        slot clock cannot say — a restart re-acquires every slot and
        starts the clock again.  The stamp is passed through as it was
        written; a plan with no execution row — one that is only
        verifying, or one that only the callback knows about — has no
        start time and says ``None``.

        And each row carries the ``phase`` the plan is in — the
        routing row's own value, passed on as it is, so that the
        caller can say what the plan is doing rather than only that it
        is working.  A plan with no routing row has no phase and says
        ``None``; a phase nobody has a label for is still passed on
        as it is, because the label is the routing layer's business.

        Returns:
            The active plans, each a dict of ``plan_id``, ``side``,
            ``workspace``, ``started_at`` and ``phase``, ordered by
            plan id.  Empty when nothing is working.
        """
        routing = {
            row["plan_id"]: row["current_phase"]
            for row in (
                _as_dict(_ROUTING_COLUMNS, raw)
                for raw in self._conn.execute(
                    "SELECT plan_id, current_phase FROM plan_routing"
                ).fetchall()
            )
        }
        execution = {
            row["plan_id"]: row
            for row in (
                _as_dict(_EXECUTION_COLUMNS, raw)
                for raw in self._conn.execute(
                    "SELECT plan_id, exec_pid, exec_status, project_dir, "
                    "next_run_at, started_at FROM plan_execution"
                ).fetchall()
            )
        }
        verification = {
            row["plan_id"]: row
            for row in (
                _as_dict(_VERIFICATION_COLUMNS, raw)
                for raw in self._conn.execute(
                    "SELECT plan_id, verification_status, updated_at "
                    "FROM plan_verification"
                ).fetchall()
            )
        }
        live = self._live_plan_ids()

        active: List[Dict[str, Any]] = []
        for plan_id in sorted(
            set(routing) | set(execution) | set(verification) | live
        ):
            side = self._execution_side(execution.get(plan_id))
            if side is None:
                side = self._verification_side(
                    routing.get(plan_id), verification.get(plan_id)
                )
            if side is None and plan_id in live:
                # The rows do not say this plan is working, but the
                # live set does.  It holds compute all the same, and
                # the round that holds it is a verification round.
                side = _LIVE_SIDE
            if side is None:
                continue
            active.append(
                {
                    "plan_id": plan_id,
                    "side": side,
                    "workspace": self.workspace_key(plan_id),
                    "started_at": _started_at_text(execution.get(plan_id)),
                    # The phase the plan is in, read from the routing
                    # row and passed on as it is.
                    "phase": self._phase(plan_id),
                }
            )
        return active

    def _live_plan_ids(self) -> set[str]:
        """Return the plan ids the injected callback says are alive.

        The callback is somebody else's code, and somebody else's code
        raises.  When it does, the answer degrades to the rows' answer
        and the failure is reported as a warning: the scheduler must
        not lose every plan it is watching over a helper's failure,
        and it must not quietly count fewer plans than are running
        either — that is the very mistake the live set exists to fix.

        A callback that returns nothing at all — ``None``, an empty
        set — says that nothing is alive, which is a legitimate
        answer and not a failure.
        """
        source = self._live_plan_ids_source
        if source is None:
            return set()
        try:
            live = source()
        except Exception as exc:  # the callback is foreign code
            log.warning(
                "the live plan set could not be read (%s); "
                "falling back to the database's facts",
                exc,
            )
            return set()
        if live is None:
            return set()
        if isinstance(live, str):
            return {live}
        try:
            return {str(plan_id) for plan_id in live}
        except TypeError as exc:
            log.warning(
                "the live plan set is not a collection of plan ids (%s); "
                "falling back to the database's facts",
                exc,
            )
            return set()

    def _execution_side(self, row: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return ``"execution"`` if the plan's subprocess is running.

        Both halves have to hold: the status says the run was started,
        and the pid says the process is still there.  A pid that is gone
        means the plan is not working any more, whatever the row says.
        """
        if row is None:
            return None
        status = str(row.get("exec_status") or "").strip().lower()
        if status not in _RUNNING_EXEC_STATUSES:
            return None
        if not _pid_is_alive(row.get("exec_pid")):
            return None
        return "execution"

    def _verification_side(
        self,
        phase: Optional[str],
        row: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Return ``"verification"`` if the plan's verification is running.

        Three conditions, all of them needed: the plan is in one of the
        verification phases, the round says it is running, and the row
        has been updated recently enough to believe.  The last one is
        what separates a live thread from a thread that died with its
        row still saying "running".
        """
        if phase not in VERIFICATION_PHASES:
            return None
        if row is None:
            return None
        status = str(row.get("verification_status") or "").strip().lower()
        if status != "running":
            return None
        if self._is_stale(row.get("updated_at")):
            return None
        return "verification"

    def _is_stale(self, updated_at: Any) -> bool:
        """Return whether the verification row is too old to believe.

        A row whose heartbeat cannot be read is treated as stale: a
        reader that cannot read the heartbeat cannot claim the thread
        is alive, and a slot that nobody can release is worse than a
        slot that is given back one tick early.
        """
        stamp = _epoch_from_iso(updated_at)
        if stamp is None:
            return True
        return (self._now_epoch() - stamp) > VERIFICATION_STALE_SECONDS

    # ------------------------------------------------------------------
    # The pending decision
    # ------------------------------------------------------------------

    def has_pending_decision(self, plan_id: str) -> bool:
        """Return whether a human is being asked about this plan.

        A decision that was answered, that fell back to its default or
        that was abandoned is history: nobody is waiting for anybody
        any more.  Only ``pending`` counts.

        Args:
            plan_id: the plan to ask about.

        Returns:
            ``True`` when at least one pending decision names the plan.
        """
        row = self._conn.execute(
            "SELECT 1 FROM plan_decisions "
            "WHERE plan_id = ? AND status = 'pending' LIMIT 1",
            (plan_id,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # The workspace key
    # ------------------------------------------------------------------

    def workspace_key(self, plan_id: str) -> Optional[str]:
        """Return the one key of the directory this plan works in.

        The key is the *real* path — ``realpath`` and then ``normpath``
        — so the directory, a symlink to it and a symlink to that
        symlink are one key.  Two plans may not work in the same
        directory at the same time, and that rule has to survive a
        restart, when there is nothing in memory to compare.

        The directory itself is derived from the plan's own facts, in
        this order: the execution row's ``project_dir``, the
        ``project_dir`` the task list declares, and the workspace the
        PRD declares as a constraint.

        Args:
            plan_id: the plan to ask about.

        Returns:
            The key of the plan's directory, or ``None`` when no fact
            names one.
        """
        declared = self._declared_workspace(plan_id)
        if declared is None:
            return None
        return os.path.normpath(os.path.realpath(declared))

    def _declared_workspace(self, plan_id: str) -> Optional[str]:
        """Return the raw directory this plan works in, as the facts say."""
        row = self._conn.execute(
            "SELECT project_dir FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is not None:
            value = str(row[0] or "").strip()
            if value:
                return value

        plan_dir = self._plan_dir(plan_id)
        if plan_dir is None:
            return None

        value = self._tasks_project_dir(plan_dir)
        if value:
            return value
        return self._prd_workspace(plan_dir)

    def _plan_dir(self, plan_id: str) -> Optional[Path]:
        """Return the plan's directory under the plans root, if it has one.

        The root may be the plans tree itself or a repository root that
        holds one, so both are tried; a directory that carries the
        plan's own files is the one that is meant.
        """
        candidates = self._plan_dir_candidates(plan_id)
        for candidate in candidates:
            if (candidate / "prd.json").exists() or (
                candidate / "tasks.json"
            ).exists():
                return candidate
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return None

    def _plan_dir_candidates(self, plan_id: str) -> Tuple[Path, Path]:
        """Return the two places this plan's directory may be.

        The root may be the plans tree itself or a repository root that
        holds one, so both are tried — in that order, because the first
        is where a plan of this tree really lives.
        """
        return (
            self._project_root / plan_id,
            self._project_root / "plans" / plan_id,
        )

    # ------------------------------------------------------------------
    # The plan that is on disk but not in the tables
    # ------------------------------------------------------------------

    def import_failed_reason(self, plan_id: str) -> Optional[str]:
        """Return why this plan's import failed, or ``None``.

        The reason is the plan's own: the import wrote it on the plan's
        execution row, word for word, so that the plan carries the
        evidence of its own failure instead of the caller having to
        keep a copy of a response body nobody saved.

        A plan that never failed to import has no reason, and a row
        whose reason is blank has none either — there is nothing to
        pass through.
        """
        row = self._conn.execute(
            "SELECT stop_reason FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        value = row[0]
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def plan_directory_exists(self, plan_id: str) -> bool:
        """Return whether this plan's directory is on disk.

        A directory that carries one of the plan's own files is a plan's
        directory: it holds the material the plan was built from.  A
        directory that carries none of them is not a plan — it is a
        directory that happens to be there, and it must not be answered
        about as if a plan were in it.

        The lookup does not go through :meth:`_plan_dir`, which answers
        "where would this plan's directory be?" for **any** directory
        that exists.  The question here is the stricter one — "is there
        a plan here?" — and it is asked of the files themselves.
        """
        for candidate in self._plan_dir_candidates(plan_id):
            if not candidate.is_dir():
                continue
            if any(
                (candidate / name).is_file() for name in _PLAN_FILE_NAMES
            ):
                return True
        return False

    def _warn_missing_plan_dir(self, plan_id: str) -> None:
        """Say once that a queued plan has no directory under the root.

        The scheduler reads the queue on every tick, so a warning that
        repeated would say the same thing over and over and drown the
        log.  The set of plans already complained about is what makes
        the warning a report rather than a heartbeat.
        """
        if plan_id in self._missing_dir_warned:
            return
        self._missing_dir_warned.add(plan_id)
        log.warning(
            "queued plan %s is ignored: no plan directory under %s",
            plan_id,
            self._project_root,
        )

    # ------------------------------------------------------------------
    # The plan's age
    # ------------------------------------------------------------------

    def plan_created_at(self, plan_id: str) -> Optional[float]:
        """Return when this plan was created, in epoch seconds.

        The plan's directory is the plan's own record of when it came
        into existence, so the directory's timestamp is the answer and
        the plan id is only the fallback.  The timestamp is the
        directory's *creation* time, because the change time moves
        every time the plan writes a file; where the filesystem keeps
        no creation time at all, the change time stands in for it.

        A plan with no directory has no directory to ask, and its id
        still says when it was made: an id that begins with eight
        digits names its date, and that date at local midnight is the
        closest thing to a creation time that is left.

        The answer is read once per plan and kept, because a
        directory's creation time does not change while the directory
        is there.  Unlike the rows this reader derives the rest of its
        answers from, this is a fact about the filesystem that cannot
        drift, so keeping it cannot make the reader wrong.

        Args:
            plan_id: the plan to ask about.

        Returns:
            The creation time in epoch seconds, or ``None`` when
            neither the directory nor the id says anything.
        """
        if plan_id in self._created_at_cache:
            return self._created_at_cache[plan_id]
        created = self._read_plan_created_at(plan_id)
        self._created_at_cache[plan_id] = created
        return created

    def _read_plan_created_at(self, plan_id: str) -> Optional[float]:
        """Return the plan's creation time, from its directory or its id."""
        plan_dir = self._plan_dir(plan_id)
        if plan_dir is not None:
            try:
                stat = plan_dir.stat()
            except OSError:
                # The directory went away between the check and the
                # read; the id is what is left.
                pass
            else:
                birthtime = float(getattr(stat, "st_birthtime", 0.0) or 0.0)
                return birthtime if birthtime else float(stat.st_ctime)
        return self._created_at_from_id(plan_id)

    def _created_at_from_id(self, plan_id: str) -> Optional[float]:
        """Return the creation time the plan's id names, if it names one."""
        match = re.match(r"^(\d{8})", plan_id)
        if match is None:
            return None
        try:
            moment = datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError:
            # Eight digits that are not a date: the id names no day,
            # and a plan with no day has no creation time.
            return None
        return moment.timestamp()

    def _tasks_project_dir(self, plan_dir: Path) -> Optional[str]:
        """Return the working directory the plan's task list declares."""
        data = _read_json(plan_dir / "tasks.json")
        if not isinstance(data, dict):
            return None

        direct = data.get("project_dir")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        tasks = data.get("tasks")
        if isinstance(tasks, list):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                value = task.get("project_dir")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _prd_workspace(self, plan_dir: Path) -> Optional[str]:
        """Return the workspace the plan's PRD declares as a constraint."""
        for name in ("prd.json", "interview.json"):
            data = _read_json(plan_dir / name)
            if not isinstance(data, dict):
                continue

            value = _workspace_from_constraints(data.get("constraints"))
            if value:
                return value

            dimensions = data.get("dimensions")
            if isinstance(dimensions, dict):
                value = _workspace_from_constraints(
                    dimensions.get("constraints")
                )
                if value:
                    return value
        return None

    # ------------------------------------------------------------------
    # The start spec
    # ------------------------------------------------------------------

    def start_spec(self, plan_id: str) -> Dict[str, Any]:
        """Return what starting this plan needs.

        A fresh execution has to be given the directory it works in, so
        the spec carries it — the workspace key, which is the one name
        the directory may be reached by.  The phase and the "is it due"
        answer come with it, because they are what says whether the
        plan may start at all.

        Args:
            plan_id: the plan to be started.

        Returns:
            A dict of ``plan_id``, ``project_dir``, ``phase`` and
            ``due``.  A plan nothing is known about gets ``None`` for
            the directory and the phase rather than a guess.
        """
        return {
            "plan_id": plan_id,
            "project_dir": self.workspace_key(plan_id),
            "phase": self._phase(plan_id),
            "due": self._is_due_plan(plan_id),
        }

    def _phase(self, plan_id: str) -> Optional[str]:
        """Return the phase the plan is in, or ``None`` if it has none."""
        row = self._conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]

    def _is_due_plan(self, plan_id: str) -> bool:
        """Return whether the plan may start now."""
        row = self._conn.execute(
            "SELECT next_run_at FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return True
        return _is_due(row[0], self._now_iso())

    # ------------------------------------------------------------------
    # The clock
    # ------------------------------------------------------------------

    def _now_epoch(self) -> float:
        """Return "now" in epoch seconds."""
        return float(self._clock())

    def _now_iso(self) -> str:
        """Return "now" as the state database writes timestamps."""
        return _iso_from_epoch(self._now_epoch())


def _queue_sort_key(
    seq: Any, priority: Any, promote_order: Any
) -> Tuple[bool, int, int, int]:
    """Return the key a queued plan is ordered by.

    The comparator is the one the whole scheduler shares
    (:func:`plan_scheduler.queue.queue_key`): promoted plans first, then
    by promotion order, then by priority, then by arrival.  A plan
    without a queue row has not been enqueued yet, so it is ordered
    behind everything that has.
    """
    if seq is None:
        return (True, 0, int(priority) if priority is not None else 0, _NO_SEQ)
    return queue_key(
        {
            "promote_order": promote_order,
            "priority": int(priority) if priority is not None else 0,
            "seq": int(seq),
        }
    )


def _is_due(next_run_at: Any, now_iso: str) -> bool:
    """Return whether a plan's scheduled time has arrived.

    A plan with no ``next_run_at`` is due: nothing was scheduled for
    it, so nothing holds it back.  The comparison is the one the state
    database makes — the timestamps are written in the same format, so
    they compare as strings.
    """
    if next_run_at is None:
        return True
    stamp = str(next_run_at).strip()
    if not stamp:
        return True
    return stamp <= now_iso