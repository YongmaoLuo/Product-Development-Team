"""backend.plan_scheduler.engine — the scheduler's one orchestrator.

Background
----------
Everything else in this package is a piece of a scheduler: the policy is
a set of pure functions, the facts reader derives what is true from the
plan tables, the store writes the three facts the scheduler owns, the
ledger reconciles the slots and the command executor translates the
operator's four actions.  None of them knows what the others do, and
none of them may act alone.

The engine is what puts them in a row.  It owns **one worker thread**
and **one mailbox**, and both the periodic tick and the operator's
commands go through that one thread — so there is exactly one order in
which things happen, and no two of them can be half done at the same
time.

    HTTP thread                    worker thread
    -----------                    -------------
    submit_command()  --mailbox-->  the command, in its turn
    submit_enqueue()  --mailbox-->  the plan, in its turn
    snapshot()                     the tick, in its turn

Why one thread
--------------
The alternative — a tick thread and a lock around every step — was
rejected because a lock only makes two *calls* mutually exclusive.  A
tick is a sequence: read the state, decide, write the queue, write the
slots, start a plan.  A command that lands between two of those steps
would be decided against a state that is already stale, and the
decision log would stop being a log.  One thread makes that
impossible: the tick and the commands share one total order, and the
engine needs no lock of its own to keep it.

The one writer
--------------
The three facts the scheduler owns — the queue, the slots and the
command log — have **one writer**: this thread.  The HTTP handlers do
not write them; they put a job in the mailbox and read a snapshot, and
the worker carries the job out.  That is not a style choice: a second
writer would be a second order in which things happen, and the whole
point of the one thread is that there is only one order.

The caller's intent
-------------------
The **priority** of a plan is the caller's number, and the caller
expresses it once, when it asks for the plan.  It lives in the plan's
own directory, in the artifact the import writes, and the worker reads
it from there when it builds the queue row — the row being the
scheduler's copy of a number that is not the scheduler's to make.  The
copy is made by the worker, so the plan may be imported while the
scheduler is switched off and still join the line with the priority the
caller asked for.

The mailbox
-----------
The mailbox is a standard-library :class:`queue.Queue` — not a message
queue, not a broker, nothing new to deploy.  Its items are four things:
a tick's turn, a command, an internal job that puts a plan in the line,
and the shutdown.  A command is carried out the moment the worker takes
it out of the mailbox; it does not wait for the next tick.

The tick
--------
The tick's steps are fixed, and the order is the architecture's:

===========================  ==============================================
phase                        what it does
===========================  ==============================================
``refresh_config``           read the immutable configuration snapshot
``read_state``               read the plan facts, the queue and the slots
``reconcile_queue``          make the queue table agree with the facts
``reconcile_capacity``       make the slots agree with the facts
``converge``                 interrupt the plans that are over capacity
``fill``                     put the free slots to work
``execute``                  carry the decisions out
``record``                   write the decisions down
===========================  ==============================================

The last phase carries one more thing: a repair that landed is
re-verified there.  The phase list is a fixed contract, so the
re-verification rides on ``record`` instead of adding a phase of its
own — see :meth:`PlanSchedulerEngine._rerun_merged_repairs`.

A phase that raises does not take the tick with it: the failure is
recorded and the tick goes on, and the next tick runs as if nothing
had happened.  A scheduler that stopped because one write failed would
be a scheduler that stops for the rest of the day.

The recovery
------------
Before the first tick, the engine **recovers**: it puts the two facts
it owns — the queue and the slots — back in line with the plan facts,
the way the run that stopped left them.

The recovery is the **worker's first job**: the worker starts, and the
recovery is what it does before the first tick and before any command.
A recovery done by the thread that started the engine would be a second
writer of the queue and the slots, and the invariant that says the
scheduler's facts have one writer would be a claim about the code
rather than about the running process.

The queue is used as it stands.  The rows are neither renumbered nor
reordered, so the line the panel shows after the restart is the line
the scheduler was running before it stopped; a plan that was waiting
without a row joins the tail, in the order the plan facts give, with
the priority the caller gave it.

The slots are reconciled with the plan facts.  A plan that is working
is **adopted**: it gets a slot and is timed from *now*, because a plan
that was just taken over has not had its turn yet and is protected for
a whole M.  A row for a plan that is not working is a reservation
nobody holds, and it is deleted.

A run that **outlived the backend** is adopted before that: the
executor is still running, and the backend's startup reconcile may have
parked the plan all the same — it reads the plan's execution row, and a
row it cannot vouch for is a row it does not trust.  The scheduler
asks the plan's own ledger and the kernel instead, and a pid whose
start time is the recorded start time is the process the plan was
running with, whatever its command line says.

The recovery decides nothing and starts nothing: the first tick after
it is an ordinary tick.

The decisions
-------------
A phase never changes the world by itself: it produces **decisions**,
and the decisions go to the :class:`~plan_scheduler.dryrun.DecisionSink`.
The sink is the exit — an execute sink carries them out through the
lifecycle, a record sink writes them down and touches nothing else —
so a dry run and the live engine run the very same code and differ
only in where the decisions go.

The commands
------------
A command is written down the moment the worker **takes it out of the
mailbox**, with the outcome it has *so far* (``accepted``), and the
record is finished when the command has been carried out: ``done`` when
it was and ``failed`` when it raised, with the detail saying what the
action did.  A command whose action raises is not lost: it is on
record, and the operator can read what happened to it.

A command the **lifecycle refused** is neither of those two things: the
command was carried out, and the action was not.  Its record says so —
``done``, with a detail that carries the status and the reason the
lifecycle gave — and the refusal itself travels back to the caller
**unchanged**, so that the route can answer with the status the
lifecycle chose.  A refusal is not a failure of the scheduler, and it is
never rewritten into one.

The caller is handed the command's id **by the worker**: the worker
writes the row and then tells the caller that the row exists, so the id
the caller is given names a row that is really there.

The caller's wait is **bounded**.  A command that is not answered
within the bound is not withdrawn — it stays in the mailbox and the
worker still carries it out, in the mailbox's order — but the caller is
answered with a *pending* result and the command's id.  Giving up the
wait gives up nothing else: the outcome the worker reaches lands in
the record the id names, and the caller can come back and read it.

The connection
--------------
The engine writes through **one** connection — the one it is given, or
the one its collaborators already hold.  It opens none and closes
none: the connection belongs to whoever made it.  The worker thread
and an HTTP thread reading the snapshot at the same time share that
connection, which SQLite serialises; the scheduler's own writes are
short transactions, so the window in which a reader could join one is
short.
"""

from __future__ import annotations

import copy
import json
import logging
import queue
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .commands import CommandRejected, CommandResult
from .config import (
    DEFAULT_CAPACITY,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_MIN_RUNTIME_SECONDS,
    DEFAULT_TICK_SECONDS,
    SchedulerConfig,
)
from .facts import QUEUED_PHASE
from .policy import (
    FRAMEWORK_FIX_KIND,
    FRAMEWORK_FIX_PRIORITY,
    admission_verdict,
    converge,
)
from .retry import is_db_locked, retry_on_busy
from .snapshot import (
    CAPACITY_SOURCE_CONFIG,
    CAPACITY_SOURCE_OVERRIDE,
    build,
)
from .store import CAPACITY_OVERRIDE_KEY

try:  # pragma: no cover - the two paths are the same module
    from framework_fix.scheduling import (
        DEADLOCK_THRESHOLD_SECONDS,
        PREEMPTED_REASON,
        detect_deadlock,
        select_victim,
    )
except ImportError:  # pragma: no cover
    from backend.framework_fix.scheduling import (  # type: ignore
        DEADLOCK_THRESHOLD_SECONDS,
        PREEMPTED_REASON,
        detect_deadlock,
        select_victim,
    )

try:  # pragma: no cover - the two paths are the same module
    from framework_fix.registry import ATTACH_STATUS_BLOCKED
except ImportError:  # pragma: no cover
    from backend.framework_fix.registry import (  # type: ignore
        ATTACH_STATUS_BLOCKED,
    )

try:  # pragma: no cover - the two paths are the same module
    from framework_fix.notify import due_reminders
    from framework_fix.notify import emit as emit_framework_fix_event
except ImportError:  # pragma: no cover
    from backend.framework_fix.notify import (  # type: ignore
        due_reminders,
    )
    from backend.framework_fix.notify import (  # type: ignore
        emit as emit_framework_fix_event,
    )

try:  # pragma: no cover - the two paths are the same module
    from framework_fix import lifecycle as framework_fix_lifecycle
except ImportError:  # pragma: no cover
    from backend.framework_fix import lifecycle as framework_fix_lifecycle  # type: ignore

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "DEADLOCK_SUSPECTED_REASON",
    "DONE",
    "ENGINE_OUTCOMES",
    "FAILED",
    "FRAMEWORK_FIX_PRIORITY_REASON",
    "PENDING",
    "PENDING_ANSWER",
    "SCHEDULER_INTENT_FILE",
    "TICK_PHASES",
    "THREAD_NAME",
    "WAITING_FOR_SLOT_REASON",
    "EngineCommandResult",
    "EngineParts",
    "PlanSchedulerEngine",
    "is_permanent_failure",
    "rerun_after_merge",
    "tick",
]


def rerun_after_merge(
    fix_plan_id: str,
    *,
    worktree: Optional[Path] = None,
    registry: Any = None,
) -> List[Any]:
    """Re-verify what was waiting on a merged repair.

    The call goes **through the lifecycle module**, so the lifecycle
    stays the authority on what a rerun does, and the entry is looked
    up when the call is made — not when this module was imported.  This
    module keeps the name as well, so there is one place to look for the
    entry and the chain can be watched at either end of it.

    Args:
        fix_plan_id: the repair plan that landed.
        worktree: the tree that carries the repair — where the attached
            verification points are run.
        registry: the registry the verdicts are written to.

    Returns:
        One conclusion per verification point that was re-run.
    """
    return framework_fix_lifecycle.rerun_after_merge(
        fix_plan_id, worktree=worktree, registry=registry
    )


@dataclass(frozen=True)
class EngineParts:
    """What the engine is bound to while it runs.

    The engine is built before it knows whether it may run: the
    configuration says so, and the configuration is read on every tick.
    The parts are therefore bound **when the scheduler may run**, not
    when the engine is constructed — a scheduler that may not run must
    not open the database, because "may not run" has to mean "nothing
    happens", and opening the database is something that happens.

    Attributes:
        store: the scheduler's own store — the only writer of the queue,
            the slots and the command log.
        facts: the reader that derives the plan facts.
        ledger: the capacity ledger.
        commands: the command executor.
        sink: where the tick's decisions go.
        conn: the connection the engine writes on.
        read_conn: the connection the **read model** reads on — a
            second connection to the same database, so that the panel's
            document does not wait for the worker's transaction.
            ``None`` means there is no such connection, and the read
            model reads on ``conn``.
    """

    store: Any
    facts: Any
    ledger: Any
    commands: Any
    sink: Any
    conn: Any
    read_conn: Any = None


#: The file in a plan's own directory that carries the intent the
#: caller expressed when it asked for the plan.
#:
#: It is the **plan's** artifact, not the scheduler's: the number in it
#: is the caller's, and it is written once, by the import, and read by
#: the worker when the plan is put in the line.  The queue row is the
#: scheduler's *copy* of that number, and the copy may only be made by
#: the scheduler's own writer.
SCHEDULER_INTENT_FILE = "scheduler_intent.json"


#: The name the worker thread carries.  It is the name the operator and
#: the teardown look for, so it is one word and it is fixed.
THREAD_NAME = "PlanSchedulerEngine"

#: The phase a plan is in while it is working.
EXECUTING_PHASE = "executing"

#: The terminal phase the startup reconcile parks a plan in when it
#: cannot vouch for the plan's run.
STOPPED_PHASE = "stopped"

#: The role the process ledger files a plan's own run under.  The
#: re-adoption looks for this role and for no other: it is the run the
#: plan was working with, and not one of the processes that ran inside
#: it.
EXECUTOR_ROLE = "executor"

#: The actions a command may ask for.  The set is closed: an action
#: that is not one of these four has no meaning, and a meaning that
#: has not been decided must not be invented by a typo.
ACTIONS: Tuple[str, ...] = ("promote", "pause", "resume", "start")

#: The tick's phases, in the order they run.  The order is the whole
#: point of the tick: a decision is made on a state that was read in
#: the phase before it, and the phase that failed is the phase
#: everybody can name.
TICK_PHASES: Tuple[str, ...] = (
    "refresh_config",
    "read_state",
    "reconcile_queue",
    "reconcile_capacity",
    "converge",
    "fill",
    "execute",
    "record",
)

#: The phase that reads the configuration.  It is the one phase that
#: always runs: the switch it reads is what decides whether the rest of
#: the tick runs at all.
TICK_PHASE_REFRESH_CONFIG = "refresh_config"

#: The outcome of a command that is still in flight — the word the
#: command's record carries between being accepted and being finished.
PENDING = "accepted"

#: The outcome of a command that has been carried out.  It is a word of
#: the **record's** vocabulary: the record says what happened to the
#: command, and "it was carried out" is what happened.  What the action
#: itself did is in the record's detail.
DONE = "done"

#: The outcome of a command that could not be carried out.  It is a
#: word of the **record's** vocabulary: the record says what happened,
#: and "it raised" is what happened.
FAILED = "failed"

#: The answer a caller gets when its bounded wait ran out.  The command
#: is still in flight — the caller may come back for the outcome with
#: the command id it was given — but the caller is not left hanging on
#: a lifecycle call that may never come back.
PENDING_ANSWER = "pending"

#: The detail a command that was still open when the process died is
#: finished with.  The command was **accepted** — the mailbox had it —
#: and the process was killed before the worker carried it out, so the
#: command was never carried out at all.  The detail says so in the
#: words the operator reads.
RESTARTED_COMMAND_DETAIL = (
    "the scheduler restarted before carrying this command out"
)

#: The words the engine adds to the command vocabulary.
ENGINE_OUTCOMES = frozenset({PENDING, DONE, FAILED, PENDING_ANSWER})

#: The command id the engine hands the executor when the engine owns the
#: command's record.
#:
#: The executor finishes the row it is given, and writes a row of its
#: own when it is given none.  The engine owns the record here: it
#: writes the command down before the command is in the mailbox — so
#: that the caller can be answered with an id even when its wait runs
#: out — and it finishes that record itself.  So the executor is handed
#: an id that matches no row: it neither finishes the engine's row nor
#: writes a second one.
NO_COMMAND_ROW = 0

#: How many decisions the engine keeps.  The log is a window onto the
#: recent past, not an archive: an engine that runs for a month must
#: not grow without bound.
DECISION_LOG_SIZE = 512

#: The shortest wait between two ticks.  A configuration that asks for
#: no wait at all must not turn the worker into a spin.
MIN_TICK_WAIT = 0.001

#: How many rounds the execute phase runs in one tick.
#:
#: A round is: hand the decisions over, read the outcomes, and — when
#: a plan was pruned — read the state again and put the free slots to
#: work.  The rounds are **bounded**, because a tick has to end: a
#: queue whose every plan fails for good would otherwise keep the tick
#: pruning and refilling for ever, and the next tick would never come.
#: The bound is a backstop, not a policy — a tick that needs more
#: rounds than this is a tick with a queue that does not behave.
_MAX_EXECUTE_ROUNDS = 8

#: The item that tells the worker to stop.
_SHUTDOWN = object()


# ---------------------------------------------------------------------------
# The permanent-failure predicate
# ---------------------------------------------------------------------------


def is_permanent_failure(exc: Optional[BaseException]) -> bool:
    """Return whether retrying this failure would change nothing.

    Some failures are answers, and an answer does not change by being
    asked again: the plan is not there, the plan is archived, the
    request conflicts with the plan's own state.  Retrying one of
    those a thousand times costs a thousand attempts and buys nothing,
    so the failure has to be recognised as permanent *before* it is
    retried.

    The predicate is a pure function of the exception: it reads the
    exception and nothing else — no clock, no database, no engine
    state — so it can be asked about a failure that happened long ago.

    Three things are permanent:

    * :class:`PlanNotFoundError` — the state machine's own word for
      "there is no such plan".  The class is imported here rather than
      at the top of the module, for the reason the command layer
      imports its own: the scheduler only needs the one class to
      recognise the failure, and importing the repository layer at
      module level would tie the package to the state machine for the
      sake of a single ``isinstance``.
    * ``ArchivedPlanError`` — recognised by its **name**.  The class
      lives in the HTTP layer, and importing that layer from here
      would be a circular import; the name is the whole of the
      contract that crosses the boundary.
    * A 409 or a 410 — refusals with a status of their own, read off
      the exception's ``status_code``.

    Everything else is transient: a 503, a busy database, a fault that
    may go away by itself.  Those are still worth retrying.

    Args:
        exc: the exception to judge, or ``None`` when there is none.

    Returns:
        True when the failure is permanent, and False otherwise — and
        False for ``None``, because "no exception" is not a failure
        that will never go away.
    """
    if exc is None:
        return False

    from state_machine.repositories.routing_repository import (
        PlanNotFoundError,
    )

    if isinstance(exc, PlanNotFoundError):
        return True

    if type(exc).__name__ == "ArchivedPlanError":
        return True

    return getattr(exc, "status_code", None) in (409, 410)


# ---------------------------------------------------------------------------
# The result of a command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineCommandResult(CommandResult):
    """A command result that carries one of the engine's own words.

    The five words of :class:`plan_scheduler.commands.CommandResult`
    describe what an **action** did: applied, started, blocked by the
    capacity, a conflict, or no such plan.  Two more things can happen
    to a command that those five cannot say:

    * it is still in flight when the caller's bounded wait runs out —
      ``pending``, the answer that says "it is on its way, come back
      for it"; and
    * it raised and did not happen — ``failed``.

    Both are words of the **record's** vocabulary, so the row in
    ``scheduler_command`` and the answer the caller got say the same
    thing.  A command that failed is still a command: it is written
    down, it is not lost, and it is not mistaken for an action that
    was carried out.

    One more thing can happen to a command, and it is not a word at
    all: the **lifecycle refused** it.  A refusal carries a status the
    five words cannot carry — a 400, a 409 — and it is not a failure of
    the scheduler either.  So it travels in :attr:`rejection`, which
    is ``None`` for every command that was carried out.
    """

    #: The refusal the lifecycle gave, when it gave one.  It is the
    #: lifecycle's own — the status, the detail and the exception it
    #: came from — and it travels on unchanged.
    rejection: Optional[CommandRejected] = None

    def __post_init__(self) -> None:
        """Accept the engine's own words; the five are checked as usual."""
        if self.outcome not in ENGINE_OUTCOMES:
            super().__post_init__()


# ---------------------------------------------------------------------------
# The mailbox's items
# ---------------------------------------------------------------------------


@dataclass
class _CommandRequest:
    """One command, waiting in the mailbox.

    Attributes:
        action: what was asked for.
        plan_id: the plan it was asked about.
        command_id: the id of the row that records this command.  It is
            handed out by the **worker**, when it writes the row down —
            the caller is told about it through ``recorded_event``.
        recorded_event: set by the worker once the command has been
            written down.  The caller waits on it to learn the id, so
            that the id it is given names a row that exists.
        result_event: set by the worker when the command has been
            carried out.  The caller waits on it once, with a bound.
        result: what the command produced.
    """

    action: str
    plan_id: str
    command_id: Optional[int] = None
    recorded_event: threading.Event = field(default_factory=threading.Event)
    result_event: threading.Event = field(default_factory=threading.Event)
    result: Optional[CommandResult] = None


@dataclass
class _EnqueueRequest:
    """One internal job: put a plan in the scheduler's line.

    It is **not** a command: it is not one of the four actions an
    operator may ask for, and it does not appear in the command log.
    It is the scheduler's own housekeeping — a plan that has just been
    asked for joins the line — and it is carried out by the worker, like
    everything else that touches the queue.

    Attributes:
        plan_id: the plan to put in the line.
        priority: the number the caller sent with the request.
        done_event: set by the worker when the job has been carried
            out.
        done: whether the plan is in the line afterwards.
    """

    plan_id: str
    priority: int = 0
    done_event: threading.Event = field(default_factory=threading.Event)
    done: bool = False


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _connection_of(*sources: Any) -> Optional[sqlite3.Connection]:
    """Return the first connection one of the collaborators holds.

    The engine writes through the same connection its collaborators
    read and write through: a scheduler that wrote to another database
    would be a scheduler whose own tables nobody else can see.
    """
    for source in sources:
        for name in ("conn", "_conn"):
            conn = getattr(source, name, None)
            if isinstance(conn, sqlite3.Connection):
                return conn
    return None


def _read_twin_ledger(
    ledger: Any, facts: Any, conn: sqlite3.Connection
) -> Any:
    """Return the ledger that reads through ``conn``.

    The ledger holds no state, so the twin is the ledger with its
    facts replaced by the read-side ones and its connection replaced by
    the read one.  A ledger that cannot be twinned is refused rather
    than used as it is: a ledger that still reads through the write
    connection would count the plans of the write database, and the
    number on the panel would be the number of another database.
    """
    factory = getattr(ledger, "with_connection", None)
    if callable(factory):
        return factory(conn)
    twin = copy.copy(ledger)
    try:
        twin._facts = facts
        twin._conn = conn
    except AttributeError as exc:
        raise RuntimeError(
            "the capacity ledger cannot be read through another "
            "connection; the read model would read the write connection "
            "instead"
        ) from exc
    return twin


def _stamp(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _rerun_summary(outcomes: Sequence[Any]) -> Dict[str, Any]:
    """What the re-verification of a landed repair came out as.

    The summary is small on purpose: how many verification points were
    re-run, and how many of them passed.  It travels with the merge
    announcement -- "the fix landed" is worth more when it says what the
    re-run said about it.
    """
    passed = 0
    failed = 0
    for outcome in outcomes or ():
        status = ""
        if isinstance(outcome, dict):
            status = str(outcome.get("new_status") or "").strip().lower()
        else:
            status = str(getattr(outcome, "new_status", "") or "").strip().lower()
        if status in ("passed", "skipped"):
            passed += 1
        else:
            failed += 1
    return {
        "verdicts": len(outcomes or ()),
        "passed": passed,
        "failed": failed,
    }


def _same_invocation(
    live: Sequence[str], recorded: Sequence[str]
) -> bool:
    """True when two command lines are the same invocation.

    The **first** entry is the program, and it is not compared: a
    process may rewrite its own ``argv[0]`` shortly after it starts —
    on macOS the launcher replaces it with the resolved path — so a
    command line read a moment after the spawn may name the same
    program under a different first entry.  The rest of the line is
    compared: the arguments are what the program was started with, and
    a program that was started with different arguments is not the run
    the ledger recorded.

    Args:
        live: the command line the kernel reports now.
        recorded: the command line the ledger wrote down.

    Returns:
        True when the two lines are the same invocation.
    """
    live_parts = [part for part in live if part]
    recorded_parts = [part for part in recorded if part]
    if len(recorded_parts) < 2:
        # A line with nothing but the program carries no arguments to
        # agree on, and a line with nothing at all is no evidence.
        return False
    tail = recorded_parts[1:]
    # The live line may carry entries the recorded one did not — a
    # wrapper may add arguments of its own — so the comparison is on
    # the recorded entries, in order, and not on the length.
    return [part for part in live_parts[1:] if part in tail] == tail

def _safe_priority(slot: Any) -> int:
    """Return the priority the slot carries, or ``0`` when it carries none.

    A priority is a whole number.  A value that is missing, ``NULL``,
    a boolean, a string or a number that is not whole reads as ``0`` —
    the weight every plan carries when nothing says otherwise.

    The reading never lifts a plan to the front of the order by
    accident: a priority the engine cannot read leaves the plan at the
    ordinary weight, and the runtime decides among the plans that
    share it.  A boolean is not a number here (``True`` is not ``1``),
    and a string is not a number either — the priority is written down
    as a number, and anything else is a value the engine does not know
    how to compare.
    """
    value = slot.get("priority", 0) if hasattr(slot, "get") else 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def _routing_table_is_missing(exc: BaseException) -> bool:
    """Whether one of the lane's tables is simply not there.

    A deployment without the framework-fix migration has no routing
    table (and no attachment table) at all.  That is not a fault to
    shout about — the lane sees no plans and the ordinary scheduler
    carries on — so the reader treats it as "no facts" rather than as
    a failure.
    """
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "no such table" in str(exc)
    )


def _repo_of_block(raw: Any) -> str:
    """Return the repository a plan's framework-fix block names.

    The block is the JSON the routing row carries: ``{"repo": ...,
    "fingerprint": ...}``.  A block that cannot be read names no
    repository, and a plan that names none is bound by the lane's
    global ceiling only.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        block: Any = raw
    else:
        try:
            block = json.loads(str(raw))
        except (TypeError, ValueError):
            return ""
    if not isinstance(block, dict):
        return ""
    repo = block.get("repo")
    if repo is None:
        return ""
    return str(repo).strip()


# ---------------------------------------------------------------------------
# The framework-fix lane
# ---------------------------------------------------------------------------
#
# The lane is a **hard requirement**: a repair plan takes an execution
# path as soon as it exists — a free slot, or an ordinary plan that may
# be pushed aside for it.  Two rules come out of that:
#
# * the lane goes **before** the ordinary queue is filled, because the
#   ordinary queue would otherwise take the room the repair plan needs;
# * a repair plan is **never** a victim — not even for another repair
#   plan, because two repair plans interrupting each other would be a
#   livelock.
#
# The function below is the lane's own decision, and it is **pure**: it
# reads its arguments and nothing else.  The clock comes in as an
# argument (so a case may move it by hand), the ledger comes in as a
# callable (so the lane's ceilings stay the ledger's business), and the
# notice outlet comes in as a callable (so the caller decides how a
# notice is delivered).  The engine owns the writing.

#: The reason a plan carries when the framework-fix lane gave it an
#: execution path.  It is one of the words in
#: :data:`plan_scheduler.policy.REASONS`, so the panel, the dry run and
#: the API all draw it.
FRAMEWORK_FIX_PRIORITY_REASON = "framework_fix_priority"

#: The reason a plan carries when its wait has gone on long enough to
#: look like a deadlock.  It is one of the words in
#: :data:`plan_scheduler.policy.REASONS` too.
DEADLOCK_SUSPECTED_REASON = "scheduler_deadlock_suspected"

#: The reason a plan carries while it waits for a slot.  It is one of
#: the words in :data:`plan_scheduler.policy.REASONS`.
WAITING_FOR_SLOT_REASON = "waiting_for_slot"

#: The phase a repair plan reaches when its repair is in the tree.
#:
#: It is the plan's own phase, not a word of the scheduler's: the repair
#: plan finishes, and finishing it is what puts the fix in the tree.
REPAIR_MERGED_PHASE = "completed"

#: The key the plan's framework-fix block carries once the repair it
#: brought has been re-verified on the tree that carries the fix.
#:
#: It is a **mark**, not a state: it says the re-verification has been
#: done, so a tick does not do it again.  The mark is cleared when the
#: plan is no longer merged — a repair that is reopened and merged again
#: is re-verified again.
RERUN_AFTER_MERGE_MARKER = "rerun_after_merge_at"

#: The key the plan's framework-fix block carries once the merge of its
#: repair has been announced to a human.
#:
#: It is a **mark**, not a state: it says the announcement has gone out,
#: so a tick does not say it again.  The mark is written only when the
#: notice was really delivered -- a notice swallowed by the quiet hours
#: was never heard by anyone, and marking it would lose it for good.
#:
#: The mark is cleared when the plan is no longer merged, so a repair that
#: is reopened and merged again is announced again.
MERGED_NOTIFIED_MARKER = "merged_notified_at"

# ---------------------------------------------------------------------------
# The merge approval
# ---------------------------------------------------------------------------

#: Where the framework-fix lane keeps its reports inside the tree it
#: works in.  It is the same place the spawner tells the fix plan to
#: write them.
FRAMEWORK_FIX_REPORT_DIR = (".pdt", "framework_fix")

#: The report the open-PR task writes.  It is the only place the PR
#: address and the CI conclusion are written down, so it is the only
#: place the merge question can be read from.
FRAMEWORK_FIX_OPEN_PR_REPORT = "open_pr.md"

#: The machine-readable field that carries the PR address.
PR_URL_FIELD = "pr_url"

#: The machine-readable field that carries the CI conclusion.
CI_CONCLUSION_FIELD = "ci_conclusion"

#: The CI conclusions that mean "green" — the only ones that make a
#: merge question worth asking.  A question about a red PR is a
#: question nobody can answer yet.
GREEN_CI_CONCLUSIONS = frozenset({"passed", "success"})

#: The status a repair plan carries while it waits for the operator to
#: approve its merge.  The read model projects it, so the panel shows
#: the wait instead of a plan that looks stuck.
WAITING_ON_USER_MERGE_STATUS = "waiting_on_user_merge"


def _report_fields(text: str) -> Dict[str, str]:
    """The machine-readable fields of a fix plan's report.

    A field is a line of the form ``name: value`` — the name is one of
    the two the merge approval needs, the value is everything after the
    first colon.  The parser is deliberately narrow: a line that is not
    a field is ignored, and a field nobody asked about is ignored too.
    A report is written by a person or by a model, so it carries prose
    as well; the prose is not data.
    """
    fields: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip().lstrip("-*# ").strip().lower()
        if key not in (PR_URL_FIELD, CI_CONCLUSION_FIELD):
            continue
        fields[key] = value.strip()
    return fields


def _lane_field(entry: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an entry, whatever shape the entry has.

    A mapping answers ``get``; anything else answers by attribute.  A
    value that is there but ``None`` reads as the default — for the
    lane's purposes "no value" and "an empty value" mean the same
    thing.
    """
    getter = getattr(entry, "get", None)
    if callable(getter):
        value = getter(name, default)
    else:
        value = getattr(entry, name, default)
    return default if value is None else value


def _lane_kind(entry: Any) -> str:
    """The kind of plan an entry is — ``framework_fix`` or something else."""
    return str(_lane_field(entry, "plan_kind", "") or "")


def _lane_repo(entry: Any) -> str:
    """The repository an entry repairs.

    The repository is named by the entry itself (``repo``) or by its
    framework-fix block (``framework_fix.repo``) — the block is where
    the routing row keeps it.
    """
    for name in ("repo", "repository"):
        value = _lane_field(entry, name, None)
        if value:
            return str(value)
    block = _lane_field(entry, "framework_fix", None)
    if isinstance(block, dict):
        value = block.get("repo")
        if value:
            return str(value)
    return ""


def _lane_stalled_since(entry: Any) -> Optional[float]:
    """When the entry started waiting for an execution path.

    ``None`` means **unknown**: without a starting point there is no
    "how long has it waited" to be had, and a guess is worse than
    saying so.
    """
    for name in ("stalled_since", "created_at"):
        value = _lane_field(entry, name, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _lane_repo_is_running(running: Sequence[Any], repo: str) -> bool:
    """Whether a repair plan of this repository is already running.

    Two repair plans of one repository work on the same code, and
    running them at the same time makes them fight over it.  The one
    behind waits.
    """
    if not repo:
        return False
    for row in running:
        if _lane_kind(row) != FRAMEWORK_FIX_KIND:
            continue
        if _lane_repo(row) == repo:
            return True
    return False


def _lane_running_row(entry: Any, repo: str) -> Dict[str, Any]:
    """The row a repair plan joins the running set with.

    The row says what kind of plan it is, so the lane's own rule — a
    repair plan is never a victim — holds for the plans this tick
    started as well as for the ones it found.
    """
    row: Dict[str, Any] = {
        "plan_id": str(_lane_field(entry, "plan_id", "") or ""),
        "plan_kind": FRAMEWORK_FIX_KIND,
        "repo": repo,
        "elapsed_s": 0.0,
    }
    priority = _lane_field(entry, "priority", None)
    if priority is not None:
        row["priority"] = priority
    return row


def _lane_verdict(
    admit: Optional[Callable[..., Dict[str, Any]]],
    entry: Any,
    plan_id: str,
    repo: str,
    running: Sequence[Any],
    capacity: Optional[int],
    used: Optional[int] = None,
    freed_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask the capacity ledger whether this repair plan may run.

    The ledger owns the lane's ceilings — the merge slot of the
    repository, the ceiling of one repository, the ceiling of the lane
    itself — and the tick does not second-guess them.

    ``used`` is how many plans hold compute as this tick counts them:
    the ledger needs it to tell an ordinary slot from the lane's own
    slot beyond the capacity, and the tick is the only one that knows
    about the plans it started in this very tick.

    ``freed_by`` is the plan this tick is pushing aside: its slot frees
    up for the plan that is asking, so the ledger must not count it as
    occupied — otherwise the lane's own slot beyond the capacity would
    be handed out for a place that is free.

    A ledger that cannot answer is a ledger that says nothing, and the
    safe answer to "nothing" is "not now": the plan waits a tick and
    asks again.
    """
    if admit is None:
        return {
            "admit": True,
            "reason": FRAMEWORK_FIX_PRIORITY_REASON,
            "oversubscribed": 0,
        }
    running_global = sum(
        1 for row in running if _lane_kind(row) == FRAMEWORK_FIX_KIND
    )
    try:
        verdict = admit(
            kind=FRAMEWORK_FIX_KIND,
            repo=repo or None,
            running_global=running_global,
            plan_id=plan_id,
            capacity=capacity,
            used=used,
            freed_by=freed_by,
        )
    except Exception as exc:  # noqa: BLE001 — the tick must survive
        log.warning(
            "[scheduler] the capacity ledger could not be asked about the "
            "framework fix plan %s (%s: %s); the plan waits",
            plan_id,
            type(exc).__name__,
            exc,
        )
        return {"admit": False, "reason": WAITING_FOR_SLOT_REASON}
    if not isinstance(verdict, dict):
        return {"admit": False, "reason": WAITING_FOR_SLOT_REASON}
    return verdict


def _lane_wait(
    notify: Callable[[str, Dict[str, Any]], None],
    now: float,
    entry: Any,
    plan_id: str,
    reason: str,
    reasons: Dict[str, str],
    escalate: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """Record that a repair plan is waiting — and escalate a long wait.

    The reason word is one of the scheduler's own words: the panel, the
    dry run and the API all draw from the same closed vocabulary.

    A wait that has gone on long enough to look like a deadlock
    replaces the reason with the deadlock word and sends a notice: ten
    minutes without an execution path means that nobody is going to
    wake this plan up.

    The notice is not the whole escalation.  A message on a wall needs
    nobody to answer it; a **question** does.  So the deadlock also
    goes to HITL — the one place where a human is asked to decide
    something.  Both happen, and neither replaces the other: the
    notice says what happened, the question asks what to do about it.
    """
    reasons[plan_id] = reason
    stalled_since = _lane_stalled_since(entry)
    if not detect_deadlock(now, stalled_since=stalled_since):
        return
    reasons[plan_id] = DEADLOCK_SUSPECTED_REASON
    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "repo": _lane_repo(entry),
        "stalled_since": stalled_since,
        "waited_s": (
            (now - stalled_since) if stalled_since is not None else None
        ),
        "reason": DEADLOCK_SUSPECTED_REASON,
    }
    plan_dir = _lane_field(entry, "plan_dir", None)
    if plan_dir:
        payload["plan_dir"] = str(plan_dir)
    try:
        notify(DEADLOCK_SUSPECTED_REASON, payload)
    except Exception as exc:  # noqa: BLE001 — a notice must not stop the tick
        log.warning(
            "[scheduler] the framework fix notice for the plan %s could not "
            "be sent (%s: %s)",
            plan_id,
            type(exc).__name__,
            exc,
        )
    if escalate is None:
        return
    try:
        escalate(payload)
    except Exception as exc:  # noqa: BLE001 — an escalation must not stop the tick
        log.warning(
            "[scheduler] the framework fix deadlock of the plan %s could "
            "not be escalated (%s: %s)",
            plan_id,
            type(exc).__name__,
            exc,
        )


#: The source recorded on the question a deadlock escalation asks.  It
#: says **who** is asking, so a reader of the decision table can tell a
#: scheduler deadlock from a merge approval.
DEADLOCK_ESCALATION_SOURCE = "framework_fix_deadlock"

#: The answer a deadlock escalation is asked with.  Answering it says
#: "I know" — and, because an ordinary answer resumes the plan, the
#: plan goes back into the schedule.
DEADLOCK_ESCALATION_OPTION = "ack"


def _lane_escalate_hitl(payload: Dict[str, Any]) -> bool:
    """Ask a human what to do about a repair plan that cannot start.

    Ten minutes without an execution path is a deadlock: nobody is going
    to wake this plan up.  The notice says so; the **question** is what
    makes it an escalation — and the question belongs in HITL, the one
    place where a human is asked to decide something.

    The question is asked **once**.  The tick runs every few seconds,
    and a plan that stays deadlocked would otherwise ask the same
    question over and over — the way a notice would be sent over and
    over.  An open question of the same kind for the same plan is the
    answer to "was it asked already".

    The question carries no task: it is about the plan, not about one
    of its tasks, so answering it does not put the plan's own schedule
    on hold behind itself.
    """
    plan_id = str(payload.get("plan_id") or "")
    if not plan_id:
        return False
    try:
        from hitl.models import DecisionRequest, DecisionStatus
        from hitl.service import create_decision, list_decisions
    except ImportError as exc:  # pragma: no cover — the seam is always there
        log.warning(
            "[scheduler] the human decision service could not be reached "
            "(%s: %s); the deadlock of the plan %s stays a notice",
            type(exc).__name__,
            exc,
            plan_id,
        )
        return False
    try:
        for decision in list_decisions(plan_id):
            if decision.source != DEADLOCK_ESCALATION_SOURCE:
                continue
            if decision.status in (
                DecisionStatus.PENDING.value,
                DecisionStatus.PAUSED.value,
            ):
                # The question is already on the table — asking it again
                # would only make the person ignore it.
                return True
        plan_dir = str(payload.get("plan_dir") or "")
        if not plan_dir:
            from config_paths import resolve_plans_dir

            plan_dir = str(Path(resolve_plans_dir()) / plan_id)
        question = (
            f"[{DEADLOCK_ESCALATION_SOURCE}] 框架修复计划 {plan_id} "
            f"等待执行路径已超过 {DEADLOCK_THRESHOLD_SECONDS // 60} 分钟"
            "（疑似调度死锁）。回复 ack 确认已处理。"
        )
        request = DecisionRequest(
            plan_id=plan_id,
            source=DEADLOCK_ESCALATION_SOURCE,
            task_id=None,
            question=question,
            options=[DEADLOCK_ESCALATION_OPTION],
            default_option=DEADLOCK_ESCALATION_OPTION,
        )
        create_decision(
            request,
            plan_dir,
            # The question has to stay answerable: a plan whose outlet
            # names nobody would otherwise close it as unroutable
            # before anybody could answer it.
            settle_unrouted=False,
            # And it has to wait for a person rather than for the
            # clock: the plan's own configuration would otherwise make
            # it an ``auto`` decision with a deadline, and the expiry
            # sweep would settle it half an hour later — the system
            # answering a question that asks a human what to do.
            require_answer=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the tick must survive
        log.warning(
            "[scheduler] the deadlock of the framework fix plan %s could "
            "not be escalated (%s: %s)",
            plan_id,
            type(exc).__name__,
            exc,
        )
        return False


def tick(
    now: Optional[float] = None,
    *,
    framework_queue: Iterable[Any] = (),
    running: Iterable[Any] = (),
    capacity: Optional[int] = None,
    used: Optional[int] = None,
    admit: Optional[Callable[..., Dict[str, Any]]] = None,
    notify: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    escalate: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """What the framework-fix lane decided this tick.

    The lane's rule is a hard one: a repair plan takes an execution
    path as soon as it exists — a free slot, or an ordinary plan that
    may be pushed aside for it.  The lane does not queue behind the
    ordinary plans, and it does not wait for a turn.

    A plan that cannot take a path **waits**: it does not grow the
    capacity, because a slot is a claim on the machine and a claim
    nobody made is a claim nobody honours.

    Args:
        now: the moment this tick decides on.  Epoch seconds.
        framework_queue: the repair plans that are waiting for an
            execution path.  Each entry names its ``plan_id``, its
            ``plan_kind`` and the ``repo`` it repairs; ``stalled_since``
            says when it started waiting.
        running: the plans that hold compute right now.  Each entry
            names its ``plan_id``; ``plan_kind``, ``priority`` and
            ``elapsed_s`` are read when they are there.
        capacity: how many plans the scheduler may run at the same
            time.  ``None`` means "not known" — then nothing is in the
            way.
        used: how many plans hold compute now.  ``None`` means the
            running set is the answer.
        admit: the capacity ledger's own admission.  It is called with
            ``kind``, ``repo``, ``running_global``, ``plan_id`` and
            ``capacity``, and it answers ``{"admit": bool, "reason":
            str}``.  ``None`` means there is no ledger to ask.
        notify: where a deadlock notice goes.  It is called with the
            event type and the payload.
        escalate: where a deadlock **question** goes.  It is called with
            the same payload, once the wait has gone on long enough to
            look like a deadlock.  ``None`` means the lane's own
            escalation — a question in the human decision table
            (:func:`_lane_escalate_hitl`).

    Returns:
        ``{"started": [...], "preempted": [...], "reasons": {...}}``:
        the plans that took an execution path, the plans that were
        pushed aside for them, and the reason every waiting plan waits
        for.  A plan that took a path carries no reason — it is not
        waiting.

    Examples:
        >>> tick(now=1000.0, framework_queue=[
        ...     {"plan_id": "fx1", "plan_kind": "framework_fix", "repo": "app"},
        ... ], running=[], capacity=2, used=0)
        {'started': ['fx1'], 'preempted': [], 'reasons': {}}
    """
    moment = time.time() if now is None else float(now)
    started: List[str] = []
    preempted: List[str] = []
    reasons: Dict[str, str] = {}
    notifier = notify if notify is not None else _lane_notify
    escalator = escalate if escalate is not None else _lane_escalate_hitl

    running_rows: List[Any] = list(running or ())
    occupied = int(used) if used is not None else len(running_rows)
    limit = int(capacity) if capacity is not None else None
    seen: set = set()

    for entry in framework_queue or ():
        plan_id = str(_lane_field(entry, "plan_id", "") or "")
        if not plan_id or plan_id in seen:
            continue
        seen.add(plan_id)
        repo = _lane_repo(entry)

        if _lane_repo_is_running(running_rows, repo):
            # A repair plan of this repository is already working on
            # this code.  The one behind waits.
            _lane_wait(
                notifier,
                moment,
                entry,
                plan_id,
                WAITING_FOR_SLOT_REASON,
                reasons,
                escalator,
            )
            continue

        victim: Optional[Dict[str, str]] = None
        oversubscribed = False
        verdict: Optional[Dict[str, Any]] = None
        if limit is not None and occupied >= limit:
            # Every slot is taken.  Somebody ordinary may be pushed
            # aside for the repair plan — and only somebody ordinary:
            # the lane's own plans are protected.
            victim = select_victim(running_rows)
            if victim is None:
                # No slot and nobody to make room.  There is exactly one
                # place the scheduler may run past its capacity, and it
                # is the **oversubscribe slot** — and the capacity
                # ledger owns it (one only, and only while the
                # scheduler is not already over its ceiling).  So the
                # lane asks the ledger rather than growing the capacity
                # on its own: a slot is a claim on the machine.
                verdict = _lane_verdict(
                    admit, entry, plan_id, repo, running_rows, limit, occupied
                )
                if not verdict.get("admit"):
                    # The ledger says no — it is the authority on the
                    # ceilings, and the tick does not second-guess them.
                    _lane_wait(
                        notifier,
                        moment,
                        entry,
                        plan_id,
                        str(verdict.get("reason") or WAITING_FOR_SLOT_REASON),
                        reasons,
                        escalator,
                    )
                    continue
                if int(verdict.get("oversubscribed") or 0) != 1:
                    # The ledger says yes — but not to the slot beyond
                    # the ceiling, so there is still no path for this
                    # plan.  It waits for the next tick.
                    _lane_wait(
                        notifier,
                        moment,
                        entry,
                        plan_id,
                        WAITING_FOR_SLOT_REASON,
                        reasons,
                        escalator,
                    )
                    continue
                oversubscribed = True

        if verdict is None:
            # A victim that is pushed aside frees a slot: the plan that
            # takes it must see the room that is about to be made, or
            # the ledger would hand out the lane's own slot beyond the
            # capacity for a place that is free.
            freed_by = str((victim or {}).get("plan_id") or "") or None
            verdict = _lane_verdict(
                admit,
                entry,
                plan_id,
                repo,
                running_rows,
                limit,
                occupied - 1 if freed_by else occupied,
                freed_by,
            )
        if not verdict.get("admit"):
            # The ledger says no — the lane's own ceilings are its
            # business, and the tick does not second-guess them.
            _lane_wait(
                notifier,
                moment,
                entry,
                plan_id,
                str(verdict.get("reason") or WAITING_FOR_SLOT_REASON),
                reasons,
                escalator,
            )
            continue

        if victim is not None:
            victim_id = str(victim.get("plan_id") or "")
            preempted.append(victim_id)
            reasons[victim_id] = str(victim.get("reason") or PREEMPTED_REASON)
            running_rows = [
                row
                for row in running_rows
                if str(_lane_field(row, "plan_id", "")) != victim_id
            ]
        else:
            occupied += 1

        if oversubscribed:
            # The plan runs on the slot beyond the ceiling, so the word
            # that says why it is running goes with it.
            reasons[plan_id] = FRAMEWORK_FIX_PRIORITY_REASON

        started.append(plan_id)
        running_rows.append(_lane_running_row(entry, repo))

    return {"started": started, "preempted": preempted, "reasons": reasons}


def _lane_notify(event: str, payload: Dict[str, Any]) -> None:
    """Send a framework-fix notice through the lane's own outlet.

    The outlet swallows delivery failures on purpose: a notice that
    could not be delivered must not take the tick with it.
    """
    emit_framework_fix_event(event, payload)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class PlanSchedulerEngine:
    """The scheduler's one worker: a tick, a mailbox and a thread.

    Attributes:
        ticks: how many ticks the engine has run.
        errors: the failures the ticks hit, as ``(phase, exception)``
            pairs, in the order they happened.
        decisions: the decisions the engine recorded, in order.
        thread: the worker thread, or ``None`` before it is started.
    """

    def __init__(
        self,
        config_loader: Any,
        store: Any = None,
        facts: Any = None,
        ledger: Any = None,
        commands: Any = None,
        sink: Any = None,
        clock: Optional[Callable[[], float]] = None,
        *,
        conn: Optional[sqlite3.Connection] = None,
        read_conn: Optional[sqlite3.Connection] = None,
        thread_name: str = THREAD_NAME,
        plans_dir: Optional[Any] = None,
        binder: Optional[Callable[[], EngineParts]] = None,
    ) -> None:
        """Bind the engine to its collaborators.

        Args:
            config_loader: the reader of the ``scheduler`` section —
                it hands out an immutable snapshot and reloads it when
                the file changes.
            store: the scheduler's own store — the only writer of the
                queue, the slots and the command log.
            facts: the reader that derives the plan facts.
            ledger: the capacity ledger — the authority on who holds
                compute and on who may be interrupted.
            commands: the command executor — the four operator
                actions, translated into facts.
            sink: where the tick's decisions go: executed, or written
                down and nothing else.
            clock: the source of "now", in epoch seconds.  It is a
                parameter so that a case can move time by hand.
            conn: the connection to write on.  When it is omitted the
                engine uses the one its collaborators already hold.
            read_conn: the connection the **read model** reads on.  It
                is a second connection to the same database: the worker
                writes on ``conn``, and the panel's document reads on
                this one, so that the document does not wait for the
                worker's transaction.  When it is omitted the read
                model reads on ``conn`` — the behaviour this split
                replaced.
            thread_name: the name the worker thread carries.
            plans_dir: the directory the plans live in.  The engine
                reads one file out of it — the plan's own import
                artifact, which carries the priority the caller gave
                when it asked for the plan.  It is the plan's fact and
                not the scheduler's, which is why it lives beside the
                plan rather than in a ``scheduler_*`` table.
            binder: how the engine gets its collaborators **later**.  It
                is called once, the first time the configuration says
                the scheduler may run, and it hands back the parts the
                engine works with.  A binder is what lets the worker
                thread exist while the scheduler is switched off: the
                thread is there, it ticks, and it does nothing at all —
                and it does not even open the database.
        """
        self._config_loader = config_loader
        self._store = store
        self._facts = facts
        self._ledger = ledger
        self._commands = commands
        self._sink = sink
        self._clock = clock if clock is not None else time.time
        self._thread_name = thread_name
        self._plans_dir = Path(plans_dir) if plans_dir is not None else None

        self._conn = conn if conn is not None else _connection_of(
            commands, ledger, facts
        )
        if self._conn is None and binder is None:
            raise ValueError(
                "the scheduler engine needs a connection to write the "
                "queue, the slots and the command log on"
            )

        self._binder = binder
        #: Whether the engine is bound to its collaborators.  An engine
        #: with a binder starts unbound and binds itself the first time
        #: the configuration says it may run.
        self._bound = binder is None
        #: Whether the engine opened the connection itself.  A connection
        #: the engine was given belongs to whoever made it; a connection
        #: the engine opened is the engine's to give back.
        self._owns_conn = False
        #: The connection the read model reads on.  ``None`` means the
        #: read model reads on the write connection.
        self._read_conn = read_conn
        #: The read-side twin of the plan facts — the same reader,
        #: reading on the read connection.
        self._read_facts: Any = None
        #: The read-side twin of the capacity ledger — the same ledger,
        #: answering from the read-side facts.
        self._read_ledger: Any = None
        #: Whether the engine took the read connection over.  A
        #: connection the engine was given belongs to whoever made it;
        #: a connection the binder opened is the engine's to give back.
        self._owns_read_conn = False
        #: The switch as the last tick saw it.  ``None`` means "the
        #: scheduler has not said yet", so the first tick says it.
        self._enabled_state: Optional[bool] = None

        self._mailbox: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._config: Any = SchedulerConfig()
        self._state: Optional[Dict[str, Any]] = None
        self._tick_decisions: List[Dict[str, Any]] = []
        self._decisions: deque = deque(maxlen=DECISION_LOG_SIZE)
        self._errors: List[Tuple[str, BaseException]] = []
        self._ticks = 0
        self._next_tick_at = 0.0
        #: What the routing table says about every plan, read once per
        #: tick: the kind of the plan and the repository it repairs.
        #: The tick reads it from several places, and the table is the
        #: one authority on both.
        self._routing: Dict[str, Dict[str, str]] = {}
        #: What the routing table says about every plan, read once per
        #: **read-model document**: the read side's own copy of the
        #: answer above, read through the read connection.
        #:
        #: The two caches are kept apart on purpose.  The tick reads the
        #: write connection and the read model reads the read one, so
        #: one shared cache would hand the panel a row the worker is
        #: holding a lock on — or the worker a row the panel read.
        self._read_routing: Dict[str, Dict[str, str]] = {}

        # The read-side twin is built **here**, when the engine is
        # built, and not on the first read: a twin that were built
        # lazily could be built from the write connection, and the read
        # model would be back where it started.
        if read_conn is not None:
            self._build_read_twin(read_conn)

    # ------------------------------------------------------------------
    # What the caller can see
    # ------------------------------------------------------------------

    @property
    def ticks(self) -> int:
        """How many ticks the engine has run."""
        return self._ticks

    @property
    def errors(self) -> List[Tuple[str, BaseException]]:
        """The failures the ticks hit, in the order they happened."""
        return list(self._errors)

    @property
    def decisions(self) -> List[Dict[str, Any]]:
        """The decisions the engine recorded, in order."""
        return list(self._decisions)

    @property
    def thread(self) -> Optional[threading.Thread]:
        """The worker thread, or ``None`` before it is started."""
        return self._thread

    @property
    def enabled(self) -> bool:
        """Whether the scheduler may run right now.

        The configuration is read here, not remembered: the switch is
        read on every tick, and a reader that is told "off" while the
        scheduler is on would be told a lie.  A configuration that
        cannot be read keeps the snapshot the last tick had.
        """
        return bool(getattr(self._read_config(), "enabled", False))

    @property
    def bound(self) -> bool:
        """Whether the engine is bound to its collaborators.

        An unbound engine has not opened the database yet: it is a
        scheduler that may not run, and it is waiting for the
        configuration to say that it may.
        """
        return self._bound

    # ------------------------------------------------------------------
    # The lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "PlanSchedulerEngine":
        """Start the worker thread.

        The worker starts, and the **recovery is the worker's first
        job**: the state the scheduler wakes up to has to say what it
        said when the process stopped, and the first tick after it is an
        ordinary tick.  The recovery is not done here, in the caller's
        thread, because a scheduler fact has exactly one writer — the
        worker — and the recovery writes two of them.

        A recovery that fails is recorded and the engine carries on:
        the tick reconciles the queue and the slots on every turn, so a
        scheduler that refused to start would be a scheduler that stops
        for the rest of the day — over a bookkeeping problem the tick
        fixes by itself.

        The worker is a daemon thread: it must never be the reason the
        process stays alive.

        Returns:
            The engine, so that a caller can write
            ``engine = PlanSchedulerEngine(...).start()``.
        """
        if self._thread is not None:
            raise RuntimeError("the scheduler engine is already started")
        # The configuration is read before the worker starts, not by the
        # first tick: a command that is submitted in the first moments
        # of the worker's life must wait for the configured bound and
        # not for the default one, and the default one is a number
        # nobody chose for this scheduler.
        self._config = self._read_config()
        self._thread = threading.Thread(
            target=self._run,
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the worker to stop and wait for it to finish.

        Stopping is a request and joining is the guarantee: the worker
        takes the commands that are already in the mailbox, finishes
        them, and only then stops.  A command that is dropped on the
        floor is a command the operator was told nothing about.

        The connection the engine opened itself is given back here: a
        connection that was opened and never closed is a file descriptor
        that is never returned.  A connection the engine was *given* is
        not the engine's to close — it belongs to whoever made it.

        The same rule holds for the read connection: the one the binder
        opened is the engine's to give back, and the one the engine was
        given is the caller's to close.

        Args:
            timeout: how long to wait for the worker to finish.
        """
        thread = self._thread
        if thread is not None:
            self._mailbox.put(_SHUTDOWN)
            if thread.is_alive():
                thread.join(timeout)
            if thread.is_alive():
                log.error(
                    "the scheduler engine did not stop within %s seconds", timeout
                )
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — the teardown must not raise
                log.exception("the scheduler's connection could not be closed")
            self._conn = None
        if self._owns_read_conn and self._read_conn is not None:
            try:
                self._read_conn.close()
            except Exception:  # noqa: BLE001 — the teardown must not raise
                log.exception(
                    "the scheduler's read connection could not be closed"
                )
            self._read_conn = None
            self._read_facts = None
            self._read_ledger = None

    # ------------------------------------------------------------------
    # The recovery
    # ------------------------------------------------------------------

    def _reconcile_open_commands(self) -> List[Dict[str, Any]]:
        """Finish the commands the previous process left open.

        A command is written down the moment the worker takes it out of
        the mailbox, with the outcome it has *so far* — ``accepted`` —
        and it is turned into ``done`` or ``failed`` only when the
        worker has answered.  A process that is killed between those
        two moments leaves the row behind, and nothing else ever comes
        back for it: the command is not in the mailbox any more (it was
        taken), and no worker is left to answer it.

        Such a row is a **stuck** row, not a request: nobody is going to
        carry it out, and the read model counts it as "stopping" for
        ever — a plan that finished days ago still shows as stopping.

        So the startup reconciliation finishes what the dead process
        left open.  Every open command is failed with a detail that
        says why, and a warning is logged for each of them.

        The reconciliation is safe to repeat: ``finish_command`` only
        updates a row that is still pending, so a command that was
        answered in the meantime is left alone, and a second call does
        nothing.

        It is also safe to run only at startup — which is where it
        runs: at that moment the worker has not taken a command out of
        the mailbox yet, so no command of **this** process can be in
        flight, and every open row belongs to a process that is gone.

        Returns:
            The commands that were finished, as dicts with the
            command's ``id``, ``plan_id`` and ``action``.
        """
        reconciled: List[Dict[str, Any]] = []
        try:
            open_commands = self._store.list_open_commands(self._conn)
        except Exception as exc:  # noqa: BLE001 — the recovery carries on
            self._record_error("command", exc)
            log.exception(
                "[scheduler] the scheduler could not read the commands it "
                "found open"
            )
            return reconciled
        for command in open_commands:
            command_id = int(command.get("id") or 0)
            if not command_id:
                continue
            detail = RESTARTED_COMMAND_DETAIL
            previous = command.get("detail")
            if previous:
                detail = "{0} (the command said: {1})".format(detail, previous)
            try:
                finished = self._store.finish_command(
                    self._conn, command_id, FAILED, detail=detail
                )
            except Exception as exc:  # noqa: BLE001 — the row may be retried later
                self._record_error("command", exc)
                log.exception(
                    "[scheduler] the scheduler could not finish the command "
                    "%s it found open",
                    command_id,
                )
                continue
            if not finished:
                # Somebody else answered it in the meantime.
                continue
            plan_id = str(command.get("plan_id") or "")
            action = str(command.get("action") or "")
            log.warning(
                "[scheduler] the command %s (plan %s, action %s) was still "
                "open when the scheduler restarted; it was not carried out",
                command_id,
                plan_id,
                action,
            )
            reconciled.append(
                {"id": command_id, "plan_id": plan_id, "action": action}
            )
        return reconciled

    def _recover(self) -> Dict[str, Any]:
        """Put the scheduler's own facts back in line after a restart.

        A restart must not change what the scheduler decided before it
        stopped.  Three rules say what "the same" means:

        1. **The queue is used as it stands.**  The rows are loaded as
           they are — not renumbered, not reordered — so the line the
           panel shows after the restart is the line the scheduler was
           running before it stopped.  A restart that renumbered the
           rows would turn the operator's promotion into a demotion.
        2. **The slots are reconciled with the plan facts.**  A plan
           that is working is **adopted**: it gets a slot and is timed
           from *now*.  An adopted plan has not had its turn yet, so it
           is protected for a whole M; carrying an old timestamp over
           would put it into the eviction candidates the moment it was
           taken over.  A row for a plan that is not working is a
           reservation nobody holds, and it is deleted.
        3. **A waiting plan without a queue row joins the tail.**  The
           order is the plan's own — ``updated_at`` and then the plan
           id — so the tail is the same tail on every restart, and the
           number the plan is given is ``MAX(seq) + 1``.
        4. **A command the dead process left open is failed.**  A
           command that was taken out of the mailbox but never answered
           would otherwise stay "stopping" for ever; the restart says
           plainly that it was never carried out.

        The recovery decides nothing and starts nothing: the first tick
        after it is an ordinary tick.  A plan that is already working
        is not in ``queued``, so it cannot be admitted a second time.

        The whole reconciliation is one transaction: the queue and the
        slots are one fact about the plans that are waiting and the
        plans that are working, and a half-applied recovery is a
        scheduler that counts a plan it has no record of.

        The re-adoption stands outside that transaction, and it comes
        first: it writes the plan's own rows, through the repositories
        that own them, and those repositories open a transaction of
        their own.  A plan that is given its row back is working as
        far as the facts are concerned, so the reconciliation below
        sees it and keeps its slot.

        Returns:
            A summary of what the recovery did: ``queued`` (the line
            as it stands now), ``backfilled`` (the plans that joined the
            tail), ``adopted`` (the plans that were timed from now),
            ``removed_slots`` (the rows nobody held any more),
            ``readopted`` (the plans whose run outlived the backend
            and were given their row back) and ``reconciled`` (the
            commands the dead process left open, which were failed).
        """
        stamp = _stamp(self._now())
        summary: Dict[str, Any] = {
            "queued": [],
            "backfilled": [],
            "adopted": [],
            "removed_slots": [],
            "readopted": [],
            "reconciled": [],
        }
        # The re-adoption comes first: a plan that is working again
        # must be working again before the slots are reconciled, or the
        # reconciliation would hand its slot to somebody else.
        summary["readopted"] = self._readopt_surviving_runs()
        # The commands the dead process left open come first, and they
        # are finished on their own: they are the facts that say a plan
        # is still stopping, and a plan that is not stopping any more
        # must not be shown as stopping.  The write is the store's own
        # transaction, and it is a row at a time — a command that
        # cannot be finished must not hold back the queue and the slots.
        summary["reconciled"] = self._reconcile_open_commands()
        with self._store.transaction(self._conn):
            summary["backfilled"] = self._backfill_queue(stamp)
            adopted, removed = self._recover_slots(stamp)
            summary["adopted"] = adopted
            summary["removed_slots"] = removed
        summary["queued"] = [
            str(row.get("plan_id") or "")
            for row in self._store.list_queue(self._conn)
        ]
        return summary

    def _backfill_queue(self, stamp: str) -> List[str]:
        """Put the waiting plans that have no queue row at the tail.

        A plan that is waiting without a queue row was imported before
        the scheduler ever ran: the phase lives in ``plan_routing`` and
        the queue row does not exist yet.  It joins the line behind the
        plans that are already in it, in the order the plan facts give —
        ``updated_at`` and then the plan id — so the tail is the same
        tail on every restart, and the number the plan is given is
        ``MAX(seq) + 1``.

        The number is handed out by the store, which reads
        ``MAX(seq) + 1`` inside the write transaction: a number is never
        handed out twice, and the plan that was waiting longest is the
        one that gets the smaller of the two.

        Args:
            stamp: the timestamp to record on the new rows.

        Returns:
            The plans that were put at the tail, in the order they were
            put there.
        """
        rows = self._conn.execute(
            "SELECT r.plan_id FROM plan_routing r "
            "LEFT JOIN scheduler_queue q ON q.plan_id = r.plan_id "
            "WHERE r.current_phase = ? AND q.plan_id IS NULL "
            "ORDER BY r.updated_at, r.plan_id",
            (QUEUED_PHASE,),
        ).fetchall()

        backfilled: List[str] = []
        for row in rows:
            plan_id = str(row[0] or "")
            if not plan_id:
                continue
            # The priority is the one the caller gave, read out of the
            # plan's own artifact: the plan was imported before the
            # scheduler ever ran, so the row it gets now is the
            # scheduler's copy of a number the caller has already
            # expressed.  A plan that expressed nothing gets the
            # default.
            intended = self._intended_priority(plan_id)
            self._store.enqueue(
                self._conn,
                plan_id,
                priority=0 if intended is None else intended,
                now=stamp,
            )
            backfilled.append(plan_id)
        return backfilled

    def _recover_slots(self, stamp: str) -> Tuple[List[str], List[str]]:
        """Reconcile the slots with the plan facts.

        Who is working is a plan fact, and the facts reader derives it;
        the slots say when each of those plans took its slot.  The two
        are brought back into line here: a plan that is working gets a
        slot timed from *now* — the scheduler has just adopted it — and
        a row for a plan that is not working is deleted.

        The clock of an adopted plan is reset even when the row was
        already there.  The row survived the restart and says the plan
        took its slot an hour ago; carrying that over would put the
        plan into the eviction candidates the moment it was taken
        over, and the takeover would evict the very work it had just
        taken over.

        The **priority** is not reset.  The slot carries the weight the
        caller gave the plan, and that weight is what the eviction rule
        compares the running plans by: a bug plan at ``-10`` that came
        back from the restart as an ordinary plan at ``0`` would give
        way to a feature plan.  So the number the row holds is read
        out before the row is taken over, and it is written back with
        the new clock.

        A plan that holds no row to carry the number over — one that
        was started outside the scheduler — is given the number the
        caller gave, read out of the plan's own artifact; a plan that
        expresses no intent runs at the default, ``0``.

        Args:
            stamp: the timestamp to record on the adopted plans' rows.

        Returns:
            The plans that were adopted and the rows that were deleted.
        """
        working = {
            str(row.get("plan_id") or "")
            for row in self._facts.active_plans()
            if row.get("plan_id")
        }

        removed: List[str] = []
        carried: Dict[str, int] = {}
        for slot in self._store.list_slots(self._conn):
            plan_id = str(slot.get("plan_id") or "")
            if not plan_id:
                continue
            if plan_id in working:
                # The number the row holds, read out before the row
                # is taken over: the takeover rewrites it, and the
                # weight the plan runs at has to survive that.
                carried[plan_id] = int(slot.get("priority") or 0)
                continue
            self._store.delete_slot(self._conn, plan_id)
            removed.append(plan_id)

        adopted: List[str] = []
        for plan_id in sorted(working):
            priority = carried.get(plan_id)
            if priority is None:
                # No row to carry the number over: the caller's own
                # number is the one the plan runs at.
                intended = self._intended_priority(plan_id)
                priority = 0 if intended is None else int(intended)
            # The old row goes first: writing a slot does not restart
            # its clock, and the clock is the thing being reset.
            self._store.delete_slot(self._conn, plan_id)
            self._store.upsert_slot(self._conn, plan_id, stamp, priority=priority)
            adopted.append(plan_id)
        return adopted, removed

    # ------------------------------------------------------------------
    # The re-adoption
    # ------------------------------------------------------------------

    def _readopt_surviving_runs(self) -> List[str]:
        """Give the plans whose run outlived the backend their row back.

        A plan that was working when the backend was killed keeps its
        executor: the process is still there, and it is the plan's own
        process.  The startup reconcile may have parked the plan all
        the same — it reads the plan's execution row, and a row it
        cannot vouch for is a row it does not trust — and a parked plan
        is a plan that is not working, so the scheduler would hand its
        slot to somebody else while the work was still going.

        So the scheduler asks the plan's own ledger and the kernel
        instead.  The ledger says which pids the plan started with; the
        kernel says whether a pid is still the same process.  A pid
        whose start time is the recorded start time is the process the
        plan was running with, whatever its command line says — the
        launcher may have rewritten ``argv[0]`` after the spawn, and a
        rewritten ``argv[0]`` is not a different process.

        Only the plans the scheduler **holds** are asked: a slot is
        what says the scheduler was running this plan, and a plan the
        scheduler never held is not the scheduler's to re-adopt.

        Returns:
            The plans that were given their row back.
        """
        working = {
            str(row.get("plan_id") or "")
            for row in self._facts.active_plans()
            if row.get("plan_id")
        }

        readopted: List[str] = []
        for slot in self._store.list_slots(self._conn):
            plan_id = str(slot.get("plan_id") or "")
            if not plan_id or plan_id in working:
                continue
            pid = self._surviving_executor_pid(plan_id)
            if pid is None:
                continue
            if self._readopt_run(plan_id, pid):
                working.add(plan_id)
                readopted.append(plan_id)
        return readopted

    def _surviving_executor_pid(self, plan_id: str) -> Optional[int]:
        """The pid of the plan's executor, if it is still running.

        Args:
            plan_id: the plan whose ledger is asked.

        Returns:
            The pid of the plan's executor, or ``None`` when the plan
            has no executor, when the executor is gone, or when the
            pid no longer names the process the ledger recorded.
        """
        from process_ledger import live_records

        for candidate in self._plan_dir_candidates(plan_id):
            try:
                records = live_records(candidate)
            except Exception as exc:  # noqa: BLE001 — an unreadable ledger is no evidence
                log.warning(
                    "[scheduler] could not read the ledger of plan %s "
                    "(%s: %s)",
                    plan_id,
                    type(exc).__name__,
                    exc,
                )
                continue
            for pid in sorted(records):
                record = records[pid]
                if str(getattr(record, "role", "") or "") != EXECUTOR_ROLE:
                    continue
                if self._is_the_recorded_process(record):
                    return int(pid)
        return None

    def _is_the_recorded_process(self, record: Any) -> bool:
        """True when the recorded pid is still the process it was.

        The start time is the identity: two processes that never shared
        a life cannot share a start time, so a pid whose start time
        is the recorded one is the process the ledger wrote down — even
        when the command line reads differently, which is what a
        rewritten ``argv[0]`` looks like.

        Args:
            record: the ledger's record of the process.

        Returns:
            True when the pid is still the recorded process.
        """
        from process_identity import CREATE_TIME_TOLERANCE, probe

        try:
            pid = int(record.pid)
        except (AttributeError, TypeError, ValueError):
            return False
        identity = probe(pid)
        if identity is None:
            return False
        recorded_at = getattr(record, "create_time", None)
        if identity.create_time is None or recorded_at is None:
            return False
        if abs(float(identity.create_time) - float(recorded_at)) > float(
            CREATE_TIME_TOLERANCE
        ):
            return False
        return _same_invocation(
            identity.cmdline, getattr(record, "cmdline", ())
        )

    def _readopt_run(self, plan_id: str, pid: int) -> bool:
        """Put the plan's row back: its run is going again.

        The execution row says the run is running again and names the
        pid it is running with; the routing row says the plan is
        executing.  The two are written through the repositories that
        own them, each in a transaction of its own — so this runs
        outside the store's transaction, where a second ``BEGIN`` would
        be refused.

        Args:
            plan_id: the plan to give its row back.
            pid: the pid the plan's executor is running with.

        Returns:
            True when the execution row was written.
        """
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        try:
            ExecutionRepository(self._conn).update_fields(
                plan_id,
                create_if_missing=True,
                exec_status="running",
                exec_pid=int(pid),
                stop_reason=None,
            )
        except Exception as exc:  # noqa: BLE001 — a plan that cannot be re-adopted is left alone
            log.warning(
                "[scheduler] could not re-adopt the run of plan %s "
                "(%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return False

        # The phase is the plan's own, and it says the plan is working
        # again.  A failure here is not fatal: the execution row is
        # what the facts read, and the phase is what the plan's own
        # lifecycle reads.
        try:
            RoutingRepository(self._conn).try_mark_phase(
                plan_id,
                (STOPPED_PHASE, EXECUTING_PHASE),
                EXECUTING_PHASE,
            )
        except Exception as exc:  # noqa: BLE001 — the phase is not the facts
            log.warning(
                "[scheduler] could not move the re-adopted plan %s back to "
                "%s (%s: %s)",
                plan_id,
                EXECUTING_PHASE,
                type(exc).__name__,
                exc,
            )
        return True

    # ------------------------------------------------------------------
    # The commands
    # ------------------------------------------------------------------

    def submit_command(
        self,
        action: str,
        plan_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Put a command in the mailbox and wait for the worker to answer.

        The command is not carried out here: it is carried out by the
        worker, in the worker's turn, between two ticks.  That is what
        makes the mailbox and the tick share one order.

        The wait is bounded.  A command that has not been answered
        within the bound is **not** withdrawn — it stays in the mailbox
        and the worker still carries it out — but the caller is told
        that it is still pending rather than being left hanging on a
        lifecycle call that may never come back.  The answer carries
        the command's id, so the caller can come back for the outcome
        the worker reaches.

        The command is written down **by the worker**, when it takes the
        command out of the mailbox: the command log is the scheduler's
        own fact, and the scheduler's own facts have one writer.  The
        caller waits for that handshake — the worker sets it once the row
        exists — so the id the caller is given names a row that exists:
        the outcome the worker reaches lands in that row, and the caller
        can read it by the id it was handed.

        A handshake that **ran out** does not cost the caller the id.
        The worker writes the row down *before* it carries the action
        out, so a command that is still running is a command whose row
        is already there; the id is therefore read when the answer is
        built, and a slow handshake does not turn an id that exists
        into a missing one.

        There are **two** waits, and each of them is bounded by the same
        bound: the handshake, which says the command has been written
        down, and the answer, which says what the action did.  The
        handshake is answered in microseconds — the worker is either
        idle or a tick away — so in practice the two waits together are
        the one bound the caller was given.

        Args:
            action: what was asked for — one of :data:`ACTIONS`.
            plan_id: the plan it was asked about.
            timeout: how long to wait for the answer; the
                configuration's ``command_timeout_seconds`` when it is
                omitted.

        Returns:
            The :class:`~plan_scheduler.commands.CommandResult` the
            command produced — or a *pending* one, carrying the
            command's id, when the bound ran out first.

        :raises fastapi.HTTPException: the lifecycle's own refusal, when
            the lifecycle raised one.  It is raised **here**, in the
            caller's thread, exactly as the lifecycle raised it: the
            status and the detail are the lifecycle's, and they are not
            rewritten on the way.
        :raises state_machine.repositories.routing_repository.ConflictError:
            the lifecycle's CAS conflict, when it raised one — the same
            way, and for the same reason.
        """
        if action not in ACTIONS:
            raise ValueError(
                "unknown command action: {0!r} (the actions are {1!r})".format(
                    action, list(ACTIONS)
                )
            )
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError(
                "the scheduler engine is not running: a command needs the "
                "worker that carries it out"
            )
        if not self.enabled:
            # The scheduler is switched off: it carries out nothing.  The
            # refusal is the caller's answer, and it is raised here, in
            # the caller's thread, for the same reason the route refuses
            # the action before it reaches the mailbox.
            raise RuntimeError(
                "the scheduler is switched off: it carries out no commands "
                "until the configuration says it may run"
            )
        if threading.current_thread() is self._thread:
            # The worker cannot wait for itself: the command it is
            # waiting for is the command it has to carry out.
            raise RuntimeError(
                "a command cannot be submitted from the worker's own "
                "thread: the worker is the one that carries commands out"
            )

        request = _CommandRequest(
            action=action,
            plan_id=plan_id,
            recorded_event=self._new_recorded_event(),
            result_event=self._new_result_event(),
        )
        self._mailbox.put(request)

        limit = self._command_timeout() if timeout is None else float(timeout)

        # The handshake: the worker writes the command down, and the id
        # comes with it.  The wait is bounded, but a handshake that ran
        # out is **not** a lost id: the worker writes the row down
        # *before* it carries the action out, so the id may well be
        # there by the time the answer is built.  The id is therefore
        # read from the request when the answer is built, and not
        # remembered from the handshake.
        request.recorded_event.wait(timeout=limit)

        # The answer, once, with the bound.
        if not request.result_event.wait(timeout=limit):
            return EngineCommandResult(
                outcome=PENDING_ANSWER,
                command_id=request.command_id,
                detail="the command is still in the mailbox",
                plan_id=plan_id,
                action=action,
            )
        if request.result is None:  # pragma: no cover - defensive
            return EngineCommandResult(
                outcome=PENDING_ANSWER,
                command_id=request.command_id,
                detail="the command produced no result",
                plan_id=plan_id,
                action=action,
            )
        rejection = getattr(request.result, "rejection", None)
        if rejection is not None:
            # The lifecycle refused the command.  The refusal is the
            # caller's answer, and it is handed back **as it came** —
            # the status and the detail the lifecycle chose — so that
            # the route can reuse them.  A refusal is not a failure of
            # the scheduler, and it is not a lost command: the record
            # says "carried out, action refused".
            cause = getattr(rejection, "cause", None)
            if cause is not None:
                raise cause
            raise rejection
        return request.result

    # ------------------------------------------------------------------
    # The line
    # ------------------------------------------------------------------

    def submit_enqueue(
        self,
        plan_id: str,
        priority: int = 0,
        *,
        timeout: Optional[float] = None,
    ) -> bool:
        """Ask the worker to put a plan in the scheduler's line.

        The write is the worker's: the queue is the scheduler's own
        fact, and the scheduler's own facts are written by the worker
        thread and by no other.  The caller only puts the job in the
        mailbox — it never opens the database and never touches the
        queue.

        The priority is the number the caller sent with the request.
        It is **fixed** when the plan joins the line: a plan that is
        already in the line keeps the row it has, priority included,
        because the caller's intent was expressed once, when the plan
        was asked for, and a repeated import of the same plan is not a
        new intent.

        The wait is bounded, like the command's: a scheduler that is
        busy must not hold an HTTP handler for ever.  A job that is not
        carried out in time is **not** withdrawn — it stays in the
        mailbox and the worker still carries it out, in the mailbox's
        order.

        A timeout of ``0`` hands the job over **without waiting**: the
        caller does not need to know when the plan joins the line, and
        the answer it gets back says only that the job was handed over.

        Args:
            plan_id: the plan to put in the line.
            priority: the number the caller sent with the request.
            timeout: how long to wait for the worker to answer; the
                configuration's ``command_timeout_seconds`` when it is
                omitted.

        Returns:
            ``True`` when the worker has put the plan in the line by the
            time the wait ended — or, for a wait of ``0``, that the job
            was handed over.  ``False`` when there is no worker, when
            the wait ran out, or when the worker could not queue the
            plan.
        """
        if self._thread is None or not self._thread.is_alive():
            return False
        if not self.enabled:
            # The scheduler is switched off: it writes nothing, and the
            # queue is one of the things it writes.  The plan is not
            # lost — the tick reconciles the queue when the scheduler is
            # switched on again.
            return False

        request = _EnqueueRequest(plan_id=plan_id, priority=int(priority))
        self._mailbox.put(request)

        limit = self._command_timeout() if timeout is None else float(timeout)
        if limit <= 0.0:
            # Handed over, not waited for: the job is in the mailbox and
            # the worker will carry it out in its own turn.
            return True
        if not request.done_event.wait(timeout=limit):
            return False
        return bool(request.done)

    # ------------------------------------------------------------------
    # The read model
    # ------------------------------------------------------------------

    def _intended_priority(self, plan_id: str) -> Optional[int]:
        """The priority the caller gave this plan, as the plan says.

        The number lives in the plan's **own** directory, in the
        artifact the import writes: it is the caller's number, not the
        scheduler's.  The queue row is the scheduler's *copy* of it,
        and the copy is made by the scheduler's own writer — so the
        original has to live somewhere the writer may read and the
        caller may write, and the plan's own directory is that place.

        A plan with no artifact, an artifact that cannot be read, and an
        artifact that carries no number are all the same plan: one that
        expresses no intent.  The scheduler never guesses a number the
        caller did not give.

        Args:
            plan_id: the plan to read the intent of.

        Returns:
            The priority the caller asked for, or ``None`` when the
            plan expresses none.
        """
        if self._plans_dir is None:
            return None
        for candidate in self._plan_dir_candidates(plan_id):
            path = candidate / SCHEDULER_INTENT_FILE
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            value = data.get("priority")
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            return int(value)
        return None

    def _plan_dir_candidates(self, plan_id: str) -> List[Path]:
        """The places a plan's own directory may be.

        The plans root may be the plans tree itself or a repository
        root that holds one, so both are tried — the same two places
        the plan facts look in, so that the engine and the facts agree
        on where a plan lives.
        """
        root = self._plans_dir
        if root is None:
            return []
        return [root / plan_id, root / "plans" / plan_id]

    def snapshot(self) -> Dict[str, Any]:
        """Return the panel's read model.

        The snapshot is assembled by the same read path and the same
        comparator the tick uses, so the panel cannot show one order
        while the scheduler runs another.

        The read model reads through the **read connection** when the
        engine has one: the panel's document must not wait for the
        worker's transaction.  The tick keeps the write connection —
        the tick decides on what it has just written, and the document
        is the panel's, not the scheduler's.

        A scheduler that has never been bound has no state to read:
        it answers with the idle document, which is what the panel
        shows for a scheduler that is not running.
        """
        if not self._bound:
            return build({})
        # The read side's routing facts belong to one document: the
        # panel's document is a moment, and the routing table is read
        # again for the next one.  The tick keeps its own copy, which
        # lives as long as the tick does.
        self._read_routing = {}
        return build(
            self._read_state(
                conn=self._read_conn,
                facts=self._read_facts,
                ledger=self._read_ledger,
            )
        )

    # ------------------------------------------------------------------
    # The worker
    # ------------------------------------------------------------------

    def _bind(self) -> bool:
        """Bind the engine to its collaborators, once.

        The binder is called the first time the configuration says the
        scheduler may run.  It opens the database and builds the
        collaborators; a binder that raises is recorded and the engine
        stays unbound, and the next tick tries again.

        Returns whether the engine is bound afterwards.
        """
        if self._bound:
            return True
        if self._binder is None:  # pragma: no cover - defensive
            return False
        try:
            parts = self._binder()
        except Exception as exc:  # noqa: BLE001 — the tick tries again
            self._record_error("bind", exc)
            log.exception("the scheduler could not be bound to the database")
            return False
        self._store = parts.store
        self._facts = parts.facts
        self._ledger = parts.ledger
        self._commands = parts.commands
        self._sink = parts.sink
        self._conn = parts.conn
        read_conn = getattr(parts, "read_conn", None)
        try:
            if read_conn is not None:
                self._build_read_twin(read_conn)
        except Exception as exc:  # noqa: BLE001 — the tick tries again
            self._record_error("bind", exc)
            log.exception(
                "the scheduler could not build its read-side twin"
            )
            return False
        self._read_conn = read_conn
        self._owns_read_conn = read_conn is not None
        self._bound = True
        self._owns_conn = True
        # The recovery is the worker's first job, and it can only run
        # once the engine knows the world it is recovering.
        self._recover_safely()
        return True

    def _build_read_twin(self, read_conn: sqlite3.Connection) -> None:
        """Build the read-side twin of the facts and of the ledger.

        The twin reads through the read connection and through nothing
        else.  A read that quietly fell back to the write connection
        would put the lock back where this split took it out, and a
        panel that waits for the worker is a panel that shows nothing
        while the worker works.

        A collaborator that cannot be read through another connection
        is refused rather than used as it is: the read model would then
        be a mixture of two databases, and a mixture is not an answer
        anybody can act on.
        """
        facts = self._facts
        if facts is not None:
            factory = getattr(facts, "with_connection", None)
            if not callable(factory):
                raise RuntimeError(
                    "the plan facts cannot be read through another "
                    "connection; the read model would read the write "
                    "connection instead"
                )
            self._read_facts = factory(read_conn)
        ledger = self._ledger
        if ledger is not None and self._read_facts is not None:
            self._read_ledger = _read_twin_ledger(
                ledger, self._read_facts, read_conn
            )

    def _may_schedule(self) -> bool:
        """Whether this tick may schedule anything.

        The configuration decides, and it is read on every tick: a
        scheduler that is switched off does nothing at all — it starts
        no plan, moves no phase, writes no row.

        A scheduler that may run binds itself first: until it is bound
        it has no database to write on.
        """
        if not bool(getattr(self._config, "enabled", False)):
            return False
        return self._bind()

    def _run(self) -> None:
        """Take the mailbox's items until the engine is stopped.

        The **recovery is the first thing the worker does**: it puts the
        scheduler's own facts back in line before the first tick decides
        on them and before any command is carried out.  It runs here, on
        the worker, because the queue and the slots are the scheduler's
        own facts and they have exactly one writer.

        The worker waits for the next tick only when the mailbox is
        empty: a command that arrives is carried out at once, and the
        tick waits for it.  That is why a tick is "best effort" — it
        runs when there is nothing else to do.
        """
        self._recover_safely()
        while True:
            try:
                item = self._mailbox.get(timeout=self._seconds_until_tick())
            except queue.Empty:
                self._run_tick()
                continue
            if item is _SHUTDOWN:
                break
            if isinstance(item, _EnqueueRequest):
                self._handle_enqueue(item)
                continue
            self._handle_command(item)

    def _recover_safely(self) -> None:
        """Recover, and carry on when the recovery fails.

        A recovery that raises is recorded and the worker goes on: the
        tick reconciles the queue and the slots on every turn, so a
        recovery that failed is a bookkeeping problem the next tick
        fixes by itself.

        An engine that is not bound has no state to recover: the
        recovery runs when the scheduler is bound, which is when it may
        run at all.
        """
        if not self._bound:
            return
        try:
            self._recover()
        except Exception as exc:  # noqa: BLE001 — the tick reconciles too
            self._record_error("recover", exc)
            log.exception("the scheduler could not reconcile its state")

    def _handle_enqueue(self, request: _EnqueueRequest) -> None:
        """Put a plan in the line, on the worker.

        The write is the worker's: the queue is the scheduler's own fact,
        and the scheduler's own facts are written by this thread and by
        no other.  The caller handed the job over; it did not write
        anything.

        The number that goes into the row is the one the **plan** says —
        the caller's intent, read out of the plan's own artifact — and
        the number the caller handed over is only a fallback for a plan
        whose artifact the engine cannot see.  The two paths therefore
        cannot disagree: the row the tick builds and the row this job
        builds carry the same number.

        A plan that is already in the line keeps its row: the store
        keeps the number and the place and writes the priority, and the
        priority that is written is the one the plan's own artifact
        says.  That is what makes the hand-over safe when the tick got
        there first and built the row before the artifact was written.

        The job is answered whatever happens: a job that raised must not
        leave its caller waiting for an answer that is not coming.
        """
        if not self._bound:
            # The scheduler may not run: there is no queue to write on.
            # The plan is not lost — the tick reconciles the queue when
            # the scheduler is switched on again.
            request.done = False
            request.done_event.set()
            return
        try:
            intended = self._intended_priority(request.plan_id)
            priority = request.priority if intended is None else intended
            with self._store.transaction(self._conn):
                self._store.enqueue(
                    self._conn,
                    request.plan_id,
                    priority=priority,
                )
            request.done = True
        except Exception as exc:  # noqa: BLE001 — the answer is the failure
            self._record_error("enqueue", exc)
            log.exception(
                "the scheduler could not put %s in the line", request.plan_id
            )
            request.done = False
        finally:
            request.done_event.set()

    def _seconds_until_tick(self) -> float:
        """How long the worker waits before the next tick.

        The cadence is real time — a tick every ``tick_seconds`` of
        wall clock — while every *moment* the scheduler records comes
        from the injected clock.  The two are different questions: how
        often the scheduler looks is a question about the machine, and
        when a plan took its slot is a question about the world.
        """
        remaining = self._next_tick_at - time.monotonic()
        if remaining <= 0.0:
            return 0.0
        return max(MIN_TICK_WAIT, remaining)

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    def _run_tick(self) -> None:
        """Run the tick's phases, in order.

        The tick reads the configuration first, and the switch in it
        decides what the rest of the tick does: a scheduler that is
        switched off does **nothing** this tick.  It starts no plan, it
        moves no phase, it writes no row — and it leaves the plans that
        are running exactly as it found them.

        A tick that did nothing is still a tick: it is counted, and the
        next one is due the usual number of seconds later.
        """
        self._tick_decisions = []
        self._routing = {}
        for name in TICK_PHASES:
            if name != TICK_PHASE_REFRESH_CONFIG and not self._may_schedule():
                # The scheduler is off: this tick is over, and the rest
                # of its phases are not run.
                break
            self._run_phase(name)
        self._ticks += 1
        self._next_tick_at = time.monotonic() + self._tick_seconds()

    def _run_phase(self, name: str) -> None:
        """Run one phase of the tick.

        A phase that raises does not take the tick with it: the
        failure is recorded, the phase is over, and the tick goes on to
        the next one.  A scheduler that stopped because one write
        failed would be a scheduler that stops for the rest of the day.
        """
        try:
            getattr(self, "_phase_" + name)()
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            self._record_error(name, exc)
            log.exception("the scheduler's %s phase failed", name)

    def _phase_refresh_config(self) -> None:
        """Read the configuration snapshot this tick runs with."""
        self._config = self._read_config()
        self._note_enabled_state()

    def _note_enabled_state(self) -> None:
        """Say once that the switch moved, and nothing at all when it did not.

        The switch is read on every tick.  A line per tick would be a
        flood nobody reads, and a switch that is read is a switch whose
        change has to be visible — a scheduler that was switched off
        and switched on again has to say so.
        """
        enabled = bool(getattr(self._config, "enabled", False))
        if enabled == self._enabled_state:
            return
        self._enabled_state = enabled
        if enabled:
            log.warning(
                "the scheduler is enabled; it may schedule plans again"
            )
        else:
            log.warning(
                "the scheduler is disabled; it schedules nothing and "
                "writes nothing until it is enabled again"
            )

    def _phase_read_state(self) -> None:
        """Read the state this tick decides on."""
        self._state = self._read_state()

    def _phase_reconcile_queue(self) -> None:
        """Make the queue table agree with the plan facts.

        A plan that is waiting without a queue row gets one, and a
        plan that has a queue row but is not waiting any more loses it.
        The queue holds the plans that are **waiting**: a plan that is
        running is not in a line.

        The plans are enqueued in the facts' own order — the order the
        shared comparator gives — so the line the scheduler runs is the
        line the panel shows, even on the very first tick.

        The reconciliation writes to the state database, and the state
        database is shared with every other writer in the process.  A
        write that arrives while somebody else holds the write lock
        comes back as SQLite's ``database is locked``, and that is a
        transient condition rather than a fault of the plan: so the
        phase waits it out (see :meth:`_retry_reconcile`) instead of
        letting one busy moment cost the tick its whole queue.
        """
        state = self._state
        if state is None:
            return
        if not self._retry_reconcile(
            "queue", lambda: self._reconcile_queue_once(state)
        ):
            # The database stayed busy: this phase is deferred to the
            # next tick, and the tick goes on to the next phase.
            return

    def _reconcile_queue_once(self, state: Dict[str, Any]) -> None:
        """Reconcile the queue table with the plan facts.

        This is the work :meth:`_phase_reconcile_queue` does, without
        the waiting: the caller runs it again when the database is
        busy, and every step in it is safe to run twice — a plan that
        is already queued keeps its number and its place, and a plan
        whose row was already taken away is not there to take again.

        Args:
            state: the state this tick decides on.
        """
        waiting = {str(row["plan_id"]) for row in state.get("waiting", [])}
        # The rows that already exist are read from the **table** and
        # not from the view: the view drops the rows the facts do not
        # vouch for, and those rows are exactly the ones this phase
        # has to take away.  A reconcile that read the view would not
        # know the row is there, and the row would stay forever.
        existing = {str(r["plan_id"]) for r in self._store.list_queue(self._conn)}

        for plan_id in sorted(existing - waiting):
            self._store.remove(self._conn, plan_id)
        for row in state.get("waiting", []):
            plan_id = str(row["plan_id"])
            if plan_id in existing:
                continue
            # A plan that is waiting without a queue row is a plan that
            # was imported before the scheduler ever ran.  The number
            # that goes into the row is the one the caller gave, read
            # out of the plan's own artifact — the scheduler's copy of
            # the caller's intent, written by the scheduler's own
            # writer.
            intended = self._intended_priority(plan_id)
            self._store.enqueue(
                self._conn,
                plan_id,
                priority=0 if intended is None else intended,
            )

        self._rebuild_line(state)

    def _phase_reconcile_capacity(self) -> None:
        """Make the slots agree with the plan facts.

        The ledger is the authority on who holds compute, and it reads
        the plan facts — so the slot rows are brought back into line
        with them here, and the count the tick decides on is the
        count the ledger says.

        The parked plans are judged first: a plan that is waiting for
        an answer and has nothing running any more gives its slot
        back, so the release is a decision the tick takes and not a
        side effect of the reconciliation.

        The reconciliation writes to the state database, and the state
        database is shared with every other writer in the process.  A
        write that arrives while somebody else holds the write lock
        comes back as SQLite's ``database is locked``, and that is a
        transient condition rather than a fault of the plan: so the
        phase waits it out (see :meth:`_retry_reconcile`) instead of
        letting one busy moment cost the tick its whole count.
        """
        state = self._state
        if state is None:
            return
        if not self._retry_reconcile(
            "capacity", lambda: self._reconcile_capacity_once(state)
        ):
            # The database stayed busy: this phase is deferred to the
            # next tick, and the tick goes on to the next phase.
            return

    def _reconcile_capacity_once(self, state: Dict[str, Any]) -> None:
        """Reconcile the slots with the plan facts.

        This is the work :meth:`_phase_reconcile_capacity` does, without
        the waiting: the caller runs it again when the database is
        busy, and every step in it is safe to run twice — a slot that
        was already given back is not there to give back again, and the
        ledger's reconciliation is a reconciliation.

        Args:
            state: the state this tick decides on.
        """
        self._release_hitl_slots()
        self._ledger.reconcile()
        state["used"] = self._ledger.used()
        state["running"] = self._running_view(
            self._store.list_slots(self._conn), self._now()
        )

    def _retry_reconcile(self, name: str, work: Callable[[], Any]) -> bool:
        """Run a reconciliation, waiting out a locked database.

        A reconcile writes to the state database, and the state
        database is shared with every other writer in the process.  A
        write that arrives while somebody else holds the write lock
        comes back as SQLite's ``database is locked`` — a transient
        condition, and not a fault of the plan the reconcile was about.

        So the work is run through :func:`retry_on_busy`: a lock is
        waited out with a bounded backoff and the work is tried again.
        When the attempts are spent the phase gives up **quietly**: it
        says it deferred the reconciliation and returns, so the next
        tick tries again.  A deferred reconcile is not a failure: no
        plan is marked failed, nothing is written to the command log,
        and the phases that follow in this tick still run.

        An error that is **not** a lock is raised as it happened.  A
        missing table or a broken row is not going to be fixed by
        waiting, and a reconcile that cannot do its work must not be
        swallowed — the failure is recorded and the tick goes on.

        Args:
            name: what the reconciliation is called — the phase's name.
            work: the reconciliation to run.

        Returns:
            ``True`` when the work was carried out, ``False`` when it
            was deferred because the database stayed busy.
        """
        try:
            retry_on_busy(work)
        except Exception as exc:
            if not is_db_locked(exc):
                # Not a lock: the reconcile is broken, and a broken
                # reconcile must be seen.
                raise
            log.warning("reconcile_%s_deferred", name)
            return False
        return True

    def _phase_converge(self) -> None:
        """Interrupt the plans that are over the capacity.

        The candidates and the choice among them are the policy's:
        the plans that have been running long enough, and among those
        the youngest one.
        """
        state = self._state
        if state is None:
            return
        self._converge_over_capacity(state)

    def _phase_fill(self) -> None:
        """Put the free slots to work.

        The **framework-fix lane goes first**: a repair plan takes an
        execution path as soon as it exists, and the ordinary queue
        would otherwise take the room it needs.

        The head of the ordinary queue is the plan the next admission
        is about, and the policy says whether it may start: it has to
        be due, its workspace has to be free, and a slot has to be
        waiting.  A head that may not start holds the queue — the line
        is a line, and the scheduler does not skip anybody.

        The repair plans are not in that line: the lane owns them, with
        its own rules (the merge slot of the repository, the ceiling of
        one repository, the preemption, the deadlock).  The ordinary
        queue may not admit them — a plan the lane held back must not
        be let in by the back door.
        """
        state = self._state
        if state is None:
            return
        self._fill_framework_fix(state)
        self._advance_merge_approvals()
        held: set = set()
        while True:
            if int(state["used"]) >= int(state["capacity"]):
                return
            queued = self._ordinary_line(state)
            if not queued:
                return
            if admission_verdict(self._ordinary_state(state, queued)) != "running":
                return
            head = queued[0]
            plan_id = str(head["plan_id"])
            key = self._facts.workspace_key(plan_id)
            if self._workspace_is_busy(key, held):
                # The directory is taken — by a plan that is working in
                # it, or by one that was admitted earlier in this very
                # tick.  Only one plan at a time may work in it.
                head["workspace_busy"] = True
                return
            self._admit(state, plan_id, int(head.get("priority") or 0))
            if key is not None:
                held.add(key)
            state["queued"] = [
                row
                for row in state["queued"]
                if str(row.get("plan_id") or "") != plan_id
            ]

    def _ordinary_line(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The ordinary queue — the queue without the lane's own plans.

        A repair plan is the lane's business: it has ceilings of its own
        (the merge slot of the repository, the ceiling of one
        repository, the ceiling of the lane) and rules of its own (the
        preemption, the deadlock).  The ordinary queue may not admit it
        — a plan the lane held back must not be let in by the back
        door, or the lane's answer would be a word nobody keeps.
        """
        return [
            row
            for row in state.get("queued", [])
            if str(row.get("plan_kind") or "") != FRAMEWORK_FIX_KIND
        ]

    def _ordinary_state(
        self, state: Dict[str, Any], queued: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """The state as the ordinary queue reads it.

        The admission check asks about the **head** of the queue, and
        the head of the ordinary queue is the head of the ordinary
        line — the repair plans are not in it.
        """
        ordinary = dict(state)
        ordinary["queued"] = queued
        return ordinary

    # ------------------------------------------------------------------
    # The framework-fix lane
    # ------------------------------------------------------------------

    def _fill_framework_fix(self, state: Dict[str, Any]) -> None:
        """Give the framework-fix lane its execution path.

        The decision is the lane's own (:func:`tick`); the writing is
        the engine's: the slot is written, the decision is recorded,
        and the reason a plan waits for is written down.  A wait nobody
        can see is a wait nobody acts on — so the reason goes into the
        plan's own framework-fix block, which is where the visible
        states live (the read model projects that block).
        """
        queue = self._framework_fix_queue(state)
        if not queue:
            return
        outcome = tick(
            self._now(),
            framework_queue=queue,
            running=self._framework_fix_running(state),
            capacity=int(state["capacity"]),
            used=int(state["used"]),
            admit=getattr(self._ledger, "admit", None),
            notify=self._notify_framework_fix,
        )
        started = set(outcome["started"])
        for victim_id in outcome["preempted"]:
            self._decide(
                "stop",
                victim_id,
                str(outcome["reasons"].get(victim_id) or PREEMPTED_REASON),
            )
        for plan_id in outcome["started"]:
            self._admit(
                state,
                plan_id,
                FRAMEWORK_FIX_PRIORITY,
                reason=FRAMEWORK_FIX_PRIORITY_REASON,
            )
            state["queued"] = [
                row
                for row in state["queued"]
                if str(row.get("plan_id") or "") != plan_id
            ]
        for plan_id, reason in outcome["reasons"].items():
            if plan_id in started:
                # A plan that took a path is not waiting.
                continue
            self._write_pause_reason(plan_id, reason)

    def _framework_fix_queue(
        self, state: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """The repair plans that are waiting for an execution path.

        The queue table says where a plan stands; the routing row says
        what kind of plan it is.  So the lane's queue is the ordinary
        queue, narrowed to the repair plans in it.
        """
        routing = self._routing_facts()
        queue: List[Dict[str, Any]] = []
        for row in state.get("queued", []):
            plan_id = str(row.get("plan_id") or "")
            if not plan_id:
                continue
            fact = routing.get(plan_id) or {}
            if str(fact.get("kind") or "") != FRAMEWORK_FIX_KIND:
                continue
            queue.append(
                {
                    "plan_id": plan_id,
                    "plan_kind": FRAMEWORK_FIX_KIND,
                    "repo": str(fact.get("repo") or ""),
                    "priority": int(row.get("priority") or 0),
                    "stalled_since": self._plan_created_at(plan_id),
                    "plan_dir": self._plan_dir_of(plan_id),
                }
            )
        return queue

    def _framework_fix_running(
        self, state: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """The running plans, as the lane reads them.

        The lane needs two things the ordinary running view does not
        carry: the **kind** of the plan (a repair plan is never a
        victim) and the repository it repairs (two repair plans of one
        repository do not work at the same time).

        It needs a third one, and it is the lane's **second** victim
        key: how many blocked attachments the plan carries.  A plan
        with a lot of work hanging on it is a plan whose preemption
        spreads the widest, so it is preempted later.  That number
        lives in ``framework_fix_attach`` — it is counted here, live,
        and not guessed at.
        """
        routing = self._routing_facts()
        blocked_counts = self._attach_blocked_counts()
        running: List[Dict[str, Any]] = []
        for row in state.get("running", []):
            plan_id = str(row.get("plan_id") or "")
            if not plan_id:
                continue
            fact = routing.get(plan_id) or {}
            running.append(
                {
                    "plan_id": plan_id,
                    "plan_kind": str(fact.get("kind") or ""),
                    "repo": str(fact.get("repo") or ""),
                    "priority": int(row.get("priority") or 0),
                    "elapsed_s": float(row.get("elapsed") or 0.0),
                    "attach_blocked_count": int(blocked_counts.get(plan_id, 0)),
                }
            )
        return running

    def _attach_blocked_counts(self) -> Dict[str, int]:
        """How many blocked attachments each plan carries right now.

        The count is read from ``framework_fix_attach`` on the same
        connection the routing facts are read from — the lane decides
        on what the database says **now**, not on what it said when the
        plan started: an attachment that was resolved a minute ago is
        no longer a reason to keep that plan running.

        Only the ``blocked`` attachments count.  A ``registered`` one
        is a non-blocking remark — it holds no execution and waits for
        the repair to be merged, so it does not make a plan expensive
        to preempt.

        A table that is not there yet is not a fault: the count is
        zero for everybody, and the lane still works — the second key
        simply ties for every plan.
        """
        conn = self._conn
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT plan_id, COUNT(*) FROM framework_fix_attach "
                "WHERE status = ? GROUP BY plan_id",
                (ATTACH_STATUS_BLOCKED,),
            ).fetchall()
        except sqlite3.Error as exc:
            if _routing_table_is_missing(exc):
                log.debug(
                    "[scheduler] there is no attachment table yet; the "
                    "framework fix lane sees no blocked work"
                )
            else:
                log.warning(
                    "[scheduler] the attachment table could not be read "
                    "(%s: %s); the framework fix lane sees no blocked work",
                    type(exc).__name__,
                    exc,
                )
            return {}
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.debug(
                "[scheduler] the attachment table could not be read "
                "(%s: %s); the framework fix lane sees no blocked work",
                type(exc).__name__,
                exc,
            )
            return {}
        counts: Dict[str, int] = {}
        for row in rows:
            plan_id = str(row[0] or "")
            if not plan_id:
                continue
            try:
                counts[plan_id] = int(row[1] or 0)
            except (TypeError, ValueError):
                counts[plan_id] = 0
        return counts

    def _routing_facts(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Dict[str, str]]:
        """What the routing table says about every plan.

        The routing row carries the two things the lane needs: the kind
        of the plan (``framework_fix`` or ``normal``) and its
        framework-fix block, which names the repository it repairs.  The
        routing table is the one authority on both (the cross-repo
        contract says so), so the lane reads it here rather than
        guessing from anywhere else.

        The answer is read once per tick: several places want it, and
        the table does not change under a tick.

        The connection is the one the **caller** reads on.  The tick
        reads the write connection — it decides on what it has just
        written — and the read model reads the read connection, so the
        panel's document does not wait for the worker's transaction.  A
        read that quietly fell back to the write connection would put
        the lock back where the read/write split took it out, and a
        panel that waits for the worker is a panel that shows nothing
        while the worker works.  Each side keeps its own cache, so the
        two never read each other's row.
        """
        conn = self._conn if conn is None else conn
        on_write_side = conn is self._conn
        cache = self._routing if on_write_side else self._read_routing
        if cache:
            return cache
        facts: Dict[str, Dict[str, str]] = {}
        try:
            rows = conn.execute(
                "SELECT plan_id, plan_kind, framework_fix FROM plan_routing"
            ).fetchall()
        except sqlite3.Error as exc:
            if _routing_table_is_missing(exc):
                # A deployment without the framework-fix migration has
                # no routing table at all.  That is not a fault to
                # shout about: the lane sees no plans and the ordinary
                # scheduler carries on.
                log.debug(
                    "[scheduler] there is no routing table yet; the "
                    "framework fix lane sees no plans"
                )
            else:
                log.warning(
                    "[scheduler] the routing table could not be read (%s: %s); "
                    "the framework fix lane sees no plans",
                    type(exc).__name__,
                    exc,
                )
            rows = []
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.debug(
                "[scheduler] the routing table could not be read (%s: %s); "
                "the framework fix lane sees no plans",
                type(exc).__name__,
                exc,
            )
            rows = []
        for row in rows:
            plan_id = str(row[0] or "")
            if not plan_id:
                continue
            facts[plan_id] = {
                "kind": str(row[1] or ""),
                "repo": _repo_of_block(row[2]),
            }
        if on_write_side:
            self._routing = facts
        else:
            self._read_routing = facts
        return facts

    def _plan_created_at(self, plan_id: str) -> Optional[float]:
        """When the plan was created — the start of its waiting.

        The plan's own directory is its own record of when it came into
        existence.  A facts reader that cannot answer says so, and a
        plan whose start is unknown is a plan whose wait cannot be
        measured — which is not the same as a plan that has waited for
        no time at all.
        """
        reader = getattr(self._facts, "plan_created_at", None)
        if not callable(reader):
            return None
        try:
            created = reader(plan_id)
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the creation time of the plan %s could not be "
                "read (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return None
        if created is None:
            return None
        try:
            return float(created)
        except (TypeError, ValueError):
            return None

    def _plan_dir_of(self, plan_id: str) -> Optional[str]:
        """The plan's own directory — where a notice about it goes."""
        for candidate in self._plan_dir_candidates(plan_id):
            if candidate.exists():
                return str(candidate)
        return None

    def _write_pause_reason(self, plan_id: str, reason: str) -> None:
        """Write down why a repair plan is not running.

        The reason is one of the scheduler's own words, and it goes into
        the plan's own framework-fix block: that block is where the
        visible states live, so a reason written anywhere else would be
        a reason nobody sees.

        A wait that looks like a deadlock also says so **in the block's
        own vocabulary** — the status word the read model knows — and
        says when it was noticed.
        """
        log.info(
            "[scheduler] the framework fix plan %s is waiting: %s",
            plan_id,
            reason,
        )
        block = self._framework_fix_block(plan_id)
        wanted = dict(block)
        wanted["pause_reason"] = reason
        if reason == DEADLOCK_SUSPECTED_REASON:
            if not block.get("deadlock_suspected_at"):
                wanted["status"] = DEADLOCK_SUSPECTED_REASON
                wanted["deadlock_suspected_at"] = _stamp(self._now())
        if wanted == block:
            # Nothing changed — a tick that says the same thing again
            # does not write it again.
            return
        self._write_framework_fix_block(plan_id, wanted)

    def _framework_fix_block(self, plan_id: str) -> Dict[str, Any]:
        """The plan's own framework-fix block — empty when there is none."""
        try:
            row = self._conn.execute(
                "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the framework fix block of the plan %s could "
                "not be read (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return {}
        if row is None or not row[0]:
            return {}
        try:
            block = json.loads(str(row[0]))
        except (TypeError, ValueError):
            return {}
        return block if isinstance(block, dict) else {}

    def _write_framework_fix_block(
        self, plan_id: str, block: Dict[str, Any]
    ) -> None:
        """Write the plan's own framework-fix block back."""
        try:
            with self._store.transaction(self._conn):
                self._conn.execute(
                    "UPDATE plan_routing SET framework_fix = ?, "
                    "updated_at = ? WHERE plan_id = ?",
                    (
                        json.dumps(block, ensure_ascii=False),
                        _stamp(self._now()),
                        plan_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the framework fix block of the plan %s could "
                "not be written (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )

    def _notify_framework_fix(
        self, event: str, payload: Dict[str, Any]
    ) -> None:
        """Send a framework-fix notice — once per deadlock.

        The tick runs every few seconds, so a plan that stays deadlocked
        would send a notice every tick, and that is how a person learns
        to ignore the notices.  The plan's own block says whether it has
        already been reported: a notice that has been sent is not sent
        again.
        """
        plan_id = str(payload.get("plan_id") or "")
        if plan_id and self._framework_fix_block(plan_id).get(
            "deadlock_suspected_at"
        ):
            return
        emit_framework_fix_event(event, payload)

    # ------------------------------------------------------------------
    # The merge approval
    # ------------------------------------------------------------------

    def _advance_merge_approvals(self) -> None:
        """Advance the merge approval of every framework-fix plan.

        The rhythm is the PRD's decision point 7, and it has three beats:

        * **The CI went green** -> the operator is asked, once, whether
          the fix may be merged.  The question is a decision that waits
          for an answer, not a message on a wall: without an answer
          nothing is merged.
        * **Nobody answered for ``merge_reminder_minutes``** -> the
          question is asked again.
        * **Nobody answered for ``waiting_on_user_merge_hours``** -> the
          plan is escalated: its status becomes ``waiting_on_user_merge``
          and the escalation goes out.

        Everything is computed from the plan's own block, so a tick that
        says the same thing again says nothing again: ``merge_ready_at``
        is the mark of "asked", ``merge_reminder_30m_at`` the mark of
        "asked again", ``waiting_on_user_merge_at`` the mark of
        "escalated".

        The thresholds come from the configuration file, not from this
        file: a number written where it is used is a number nobody can
        change without reading the code.

        The whole step is **failure-isolated**: a merge question that
        cannot be asked is not a reason to stop scheduling.
        """
        try:
            self._advance_merge_approvals_safely()
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the merge approvals could not be advanced "
                "(%s: %s)",
                type(exc).__name__,
                exc,
            )

    def _advance_merge_approvals_safely(self) -> None:
        """The merge approval itself — the part that may fail."""
        settings = self._framework_fix_settings()
        reminder_minutes = int(settings["merge_reminder_minutes"])
        escalation_hours = int(settings["waiting_on_user_merge_hours"])
        now = datetime.fromtimestamp(self._now(), tz=timezone.utc)

        plan_ids = [plan_id for plan_id, _phase in self._repair_plans()]
        for plan_id in plan_ids:
            self._announce_merge_ready(plan_id, now)

        leases: List[Dict[str, Any]] = []
        for plan_id in plan_ids:
            lease = dict(self._framework_fix_block(plan_id))
            lease["plan_id"] = plan_id
            leases.append(lease)

        for plan_id in due_reminders(
            now, leases, threshold_minutes=reminder_minutes
        ):
            self._remind_merge(plan_id, now)

        for plan_id in plan_ids:
            self._escalate_merge(plan_id, now, escalation_hours)

    def _announce_merge_ready(self, plan_id: str, now: datetime) -> None:
        """Ask the operator whether the fix may be merged — once.

        The question is asked when the CI has gone green and nobody has
        been asked yet.  ``merge_ready_at`` is the mark of "asked": a
        tick that finds it does not ask again.

        The mark is written **only when the question was really
        delivered**.  A question that fell into the quiet hours was
        swallowed — writing the mark anyway would lose it for good, and
        nobody would ever be asked.
        """
        block = self._framework_fix_block(plan_id)
        if block.get("merge_ready_at"):
            return

        report = self._read_open_pr_report(plan_id)
        if report is None:
            return
        fields = _report_fields(report)
        pr_url = str(fields.get(PR_URL_FIELD) or "").strip()
        ci_conclusion = str(fields.get(CI_CONCLUSION_FIELD) or "").strip()
        if not pr_url:
            # No PR address — there is nothing to approve yet.
            return
        if ci_conclusion.lower() not in GREEN_CI_CONCLUSIONS:
            # The CI is not green — a question about a red PR is a
            # question nobody can answer yet.
            return

        plan_dir = self._plan_dir_of(plan_id)
        if not plan_dir:
            return

        self._backfill_merge_lease(plan_id, block, pr_url)

        record = emit_framework_fix_event(
            "merge_ready",
            {
                "plan_id": plan_id,
                "plan_dir": plan_dir,
                "pr_url": pr_url,
                "ci_conclusion": ci_conclusion,
            },
            now=now,
        )
        if not isinstance(record, dict) or not record.get("delivered"):
            # Swallowed by the quiet hours, or the outlet is down: the
            # question was not asked, so the mark is not written and the
            # next tick asks again.
            return

        wanted = dict(self._framework_fix_block(plan_id))
        wanted["merge_ready_at"] = _stamp(self._now())
        wanted["ci_conclusion"] = ci_conclusion
        wanted["pr_url"] = pr_url
        self._write_framework_fix_block(plan_id, wanted)

    def _remind_merge(self, plan_id: str, now: datetime) -> None:
        """Ask again — the question has been waiting for a while.

        The mark is written **only when the reminder was really
        delivered**: a reminder swallowed by the quiet hours is not a
        reminder, and the tick after the window sends it.
        """
        block = self._framework_fix_block(plan_id)
        if block.get("merge_reminder_30m_at"):
            return
        plan_dir = self._plan_dir_of(plan_id)
        if not plan_dir:
            return

        record = emit_framework_fix_event(
            "merge_reminder_30m",
            {
                "plan_id": plan_id,
                "plan_dir": plan_dir,
                "pr_url": str(block.get("pr_url") or ""),
                "ci_conclusion": str(block.get("ci_conclusion") or ""),
            },
            now=now,
        )
        if not isinstance(record, dict) or not record.get("delivered"):
            return

        wanted = dict(self._framework_fix_block(plan_id))
        wanted["merge_reminder_30m_at"] = _stamp(self._now())
        self._write_framework_fix_block(plan_id, wanted)

    def _escalate_merge(
        self, plan_id: str, now: datetime, escalation_hours: int
    ) -> None:
        """Escalate a merge question nobody answered.

        The plan's status becomes ``waiting_on_user_merge`` — the word
        the read model projects — and the escalation goes out.  The
        status is written whether or not the escalation was delivered:
        the state is a fact about the plan, and the notification is only
        an attempt to tell somebody about it.
        """
        block = self._framework_fix_block(plan_id)
        if block.get("waiting_on_user_merge_at"):
            return
        ready_at = self._epoch(block.get("merge_ready_at"))
        if ready_at is None:
            # Not merge-ready yet — there is nothing to escalate.
            return
        deadline = datetime.fromtimestamp(
            ready_at, tz=timezone.utc
        ) + timedelta(hours=escalation_hours)
        if now < deadline:
            return

        wanted = dict(block)
        wanted["status"] = WAITING_ON_USER_MERGE_STATUS
        wanted["waiting_on_user_merge_at"] = _stamp(self._now())
        self._write_framework_fix_block(plan_id, wanted)

        emit_framework_fix_event(
            "waiting_on_user_merge",
            {
                "plan_id": plan_id,
                "plan_dir": self._plan_dir_of(plan_id) or "",
                "pr_url": str(block.get("pr_url") or ""),
                "ci_conclusion": str(block.get("ci_conclusion") or ""),
            },
            now=now,
        )

    def _backfill_merge_lease(
        self, plan_id: str, block: Dict[str, Any], pr_url: str
    ) -> None:
        """Put the PR address on this repair plan's merge lease.

        The lease is taken when the fix plan starts; the PR address is
        only known once the PR is open.  The registry already knows how
        to fill the address into a lease the plan owns, so the lease row
        ends up carrying the address — and the panel, which reads the
        address off the lease, shows which PR is waiting.

        A lease somebody else owns is not touched: the registry says so
        and answers ``False``.
        """
        repo = str(block.get("repo") or "").strip()
        if not repo:
            return
        registry = self._framework_fix_registry()
        if registry is None:
            return
        try:
            acquired = registry.acquire_merge_lease(repo, plan_id, pr_url)
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the merge lease of the repair plan %s could "
                "not be backfilled (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return
        if not acquired:
            log.info(
                "[scheduler] the merge lease of the repository %s is held "
                "by another repair plan; the address of the PR %s was not "
                "written on it",
                repo,
                pr_url,
            )

    def _read_open_pr_report(self, plan_id: str) -> Optional[str]:
        """The open-PR report of the tree this plan worked in.

        The report is written by the fix plan's own open-PR task, in the
        tree the fix was made in — so it is read from there, not from
        the plan's directory.
        """
        project_dir = self._project_dir_of(plan_id)
        if project_dir is None:
            return None
        path = project_dir.joinpath(
            *FRAMEWORK_FIX_REPORT_DIR
        ) / FRAMEWORK_FIX_OPEN_PR_REPORT
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log.debug(
                "[scheduler] the open PR report of the repair plan %s "
                "could not be read (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return None

    def _framework_fix_settings(self) -> Dict[str, Any]:
        """The framework-fix lane's settings, as the configuration says.

        The thresholds are read from the configuration file, not from
        this file: a number written where it is used is a number nobody
        can change without reading the code.

        A configuration that cannot be read falls back to the defaults:
        a tick that stops because of a number in a file is a lane nobody
        repairs.
        """
        try:
            from config import (
                DEFAULT_FRAMEWORK_FIX_CONFIG,
                framework_fix_settings,
            )
        except ImportError:  # pragma: no cover - the two paths are the same
            from backend.config import (  # type: ignore
                DEFAULT_FRAMEWORK_FIX_CONFIG,
                framework_fix_settings,
            )
        try:
            settings = framework_fix_settings()
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the framework-fix settings could not be read "
                "(%s: %s); the defaults apply",
                type(exc).__name__,
                exc,
            )
            settings = {}
        merged = dict(DEFAULT_FRAMEWORK_FIX_CONFIG)
        if isinstance(settings, dict):
            merged.update(settings)
        return merged

    def _phase_execute(self) -> None:
        """Carry the decisions out, and put the free slots to work again.

        The sink is the exit: an execute sink starts and stops the
        plans through the lifecycle, a record sink writes the
        decisions down and touches nothing else.  The tick itself
        changes nothing here — it hands the decisions over.

        The decisions are handed over as a **batch**, and the batch's
        outcomes are read **one by one**: a decision that failed does
        not take the decisions behind it with it.

        The two kinds of failure are told apart:

        * A failure that will never go away
          (:func:`is_permanent_failure`) is **pruned**: the plan is out
          of the line, and the tick then **refills** — the state is
          read again and the free slots are put to work, so the plan
          behind the pruned one does not wait for the next tick.
        * A failure that may go away by itself is only **recorded**:
          the plan keeps its place in the queue and is tried again on
          the next tick.

        The rounds are bounded (:data:`_MAX_EXECUTE_ROUNDS`): a tick
        that keeps pruning keeps filling, and a tick has to end.

        A sink that raises takes the phase with it, exactly as it did
        before: the phase that runs it records the failure, and the
        tick goes on.
        """
        handled = 0
        for _ in range(_MAX_EXECUTE_ROUNDS):
            batch = self._tick_decisions[handled:]
            if not batch:
                return
            outcomes = self._sink.apply(batch) or []
            pruned = False
            for outcome in outcomes:
                if outcome.get("ok", True):
                    continue
                error = outcome.get("error")
                if is_permanent_failure(error):
                    self._prune_failed_plan(outcome)
                    pruned = True
                    continue
                self._record_error("execute", error)
                log.warning(
                    "the scheduler could not carry out the %s of plan %s "
                    "(%s: %s); the plan keeps its place in the queue",
                    outcome.get("action"),
                    outcome.get("plan_id"),
                    type(error).__name__,
                    error,
                )
            handled += len(batch)
            if not pruned:
                return
            self._phase_read_state()
            self._phase_fill()

    def _phase_record(self) -> None:
        """Write the decisions down.

        The record is the same four fields the dry run writes, so the
        two exits can be lined up against each other.

        It also carries the re-verification of a landed repair: the
        tick's phase list is a fixed contract, so the re-verification
        rides on the last phase instead of adding a phase of its own.
        """
        stamp = _stamp(self._now())
        for decision in self._tick_decisions:
            self._decisions.append(
                {
                    "ts": stamp,
                    "action": decision.get("action"),
                    "plan_id": decision.get("plan_id"),
                    "reason": decision.get("reason"),
                }
            )
        self._rerun_merged_repairs()

    def _rerun_merged_repairs(self) -> None:
        """A repair that landed: re-verify what was waiting on it.

        The merge is **observed** here — the repair plan reached its
        terminal phase, so its fix is in the tree.  Everything that was
        waiting on that fix is put back on the tree that carries it and
        re-run there, and the new verdict is **appended**: the old
        verdicts stay where they are, untouched.

        Without this the chain stops at "the fix merged": nobody runs
        the verification points the fix was supposed to repair, so
        nobody finds out whether it did.

        The rerun happens **once per merge**.  The plan's own
        framework-fix block carries the mark, and the mark is cleared
        when the plan is no longer merged — so a repair that is
        reopened and merged again is re-verified again, and a repair
        that stays merged is not verified over and over.

        The merge is also **announced** once per merge: the approval
        chain asks whether the repair may be merged, but nobody says
        whether it **was**.  The announcement carries its own mark, and
        that mark goes away with the merge too.
        """
        for plan_id, phase in self._repair_plans():
            if phase == REPAIR_MERGED_PHASE:
                summary = self._rerun_merged_repair(plan_id)
                self._announce_merged(plan_id, summary)
            else:
                self._rearm_rerun_mark(plan_id)

    def _repair_plans(self) -> List[Tuple[str, str]]:
        """The repair plans and the phase each one is in."""
        try:
            rows = self._conn.execute(
                "SELECT plan_id, current_phase FROM plan_routing "
                "WHERE plan_kind = ?",
                (FRAMEWORK_FIX_KIND,),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.debug(
                "[scheduler] the repair plans could not be read (%s: %s)",
                type(exc).__name__,
                exc,
            )
            return []
        return [
            (str(row[0] or ""), str(row[1] or ""))
            for row in rows
            if str(row[0] or "")
        ]

    def _rearm_rerun_mark(self, plan_id: str) -> None:
        """Clear the marks of a repair that is not merged (any more).

        A repair that is reopened and merged again has to be re-verified
        again — so the mark of the previous merge goes away with the
        merge.  The same goes for the announcement of that merge: two
        merges are two events, and the second one has to be said too.
        """
        block = self._framework_fix_block(plan_id)
        if not block.get(RERUN_AFTER_MERGE_MARKER) and not block.get(
            MERGED_NOTIFIED_MARKER
        ):
            return
        wanted = dict(block)
        wanted.pop(RERUN_AFTER_MERGE_MARKER, None)
        wanted.pop("rerun_after_merge_verdicts", None)
        wanted.pop(MERGED_NOTIFIED_MARKER, None)
        self._write_framework_fix_block(plan_id, wanted)

    def _announce_merged(
        self, plan_id: str, summary: Optional[Dict[str, Any]] = None
    ) -> None:
        """Say once that the repair landed.

        The merge approval chain asks whether the repair may be merged
        (``merge_ready`` -> the 30-minute reminder -> the two-hour
        escalation).  Nobody asks whether it **was** merged: the request
        goes out, the operator approves, the PR lands -- and the fact
        that the work is done is never said out loud.

        The mark is written **only when the notice was really delivered**.
        A notice swallowed by the quiet hours was heard by nobody, and
        marking it would lose it for good.

        A notice that cannot be sent is not a reason to stop the tick.
        """
        block = self._framework_fix_block(plan_id)
        if block.get(MERGED_NOTIFIED_MARKER):
            # The announcement has gone out already.  A tick that says
            # the same thing again says nothing again.
            return

        plan_dir = self._plan_dir_of(plan_id)
        if not plan_dir:
            # Nowhere to send it to -- and no mark, so the next tick tries
            # again.
            return

        payload: Dict[str, Any] = {
            "plan_id": plan_id,
            "plan_dir": plan_dir,
            "fingerprint": str(block.get("fingerprint") or ""),
        }
        if summary:
            payload["rerun"] = summary

        now = datetime.fromtimestamp(self._now(), tz=timezone.utc)
        try:
            record = emit_framework_fix_event("merged", payload, now=now)
        except Exception as exc:  # noqa: BLE001 — a notice must not stop the tick
            log.warning(
                "[scheduler] the merge notice for the repair plan %s could "
                "not be sent (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return
        if not isinstance(record, dict) or not record.get("delivered"):
            # Not delivered (the quiet hours swallowed it, or the outlet
            # is down) -- no mark, so the next tick tries again.
            log.info(
                "[scheduler] the merge notice for the repair plan %s was not "
                "delivered: %s",
                plan_id,
                (record or {}).get("suppressed") if isinstance(record, dict)
                else "no record",
            )
            return

        wanted = dict(block)
        wanted[MERGED_NOTIFIED_MARKER] = _stamp(self._now())
        self._write_framework_fix_block(plan_id, wanted)
        log.info(
            "[scheduler] the repair plan %s was merged and announced",
            plan_id,
        )

    def _rerun_merged_repair(
        self, plan_id: str
    ) -> Optional[Dict[str, Any]]:
        """Re-verify the attachments of one merged repair — once.

        Returns a summary of the re-verification (how many verification
        points were re-run and how they came out), or ``None`` when
        nothing was re-verified this time.  The summary is what the merge
        announcement carries: "the fix landed" is worth more when it says
        what the re-run said.
        """
        block = self._framework_fix_block(plan_id)
        if block.get(RERUN_AFTER_MERGE_MARKER):
            # The merge was already re-verified.  A tick that says the
            # same thing again does not do it again.
            return None

        registry = self._framework_fix_registry()
        if registry is None:
            log.debug(
                "[scheduler] the repair plan %s cannot be re-verified: "
                "there is no state database to write the verdicts to",
                plan_id,
            )
            return None

        worktree = self._project_dir_of(plan_id)
        try:
            outcomes = rerun_after_merge(
                plan_id, worktree=worktree, registry=registry
            )
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the merged repair plan %s could not be "
                "re-verified (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return None

        wanted = dict(block)
        wanted[RERUN_AFTER_MERGE_MARKER] = _stamp(self._now())
        wanted["rerun_after_merge_verdicts"] = len(outcomes)
        self._write_framework_fix_block(plan_id, wanted)
        log.info(
            "[scheduler] the merged repair plan %s was re-verified on %s "
            "verification points",
            plan_id,
            len(outcomes),
        )
        return _rerun_summary(outcomes)

    def _framework_fix_registry(self) -> Optional[Any]:
        """The registry on **this** engine's database — or ``None``.

        The registry writes the verdicts, so it has to write them into
        the database the engine reads: a registry pointed at another
        file would leave the engine's own database without a trace of
        the re-verification.
        """
        path = self._state_db_path()
        if path is None:
            return None
        try:
            from framework_fix.registry import FrameworkFixRegistry
        except ImportError:  # pragma: no cover - the two paths are the same
            from backend.framework_fix.registry import (  # type: ignore
                FrameworkFixRegistry,
            )
        try:
            return FrameworkFixRegistry(path)
        except Exception as exc:  # noqa: BLE001 — a registry that cannot be
            log.warning(  # opened is not a reason to stop the tick
                "[scheduler] the framework-fix registry could not be opened "
                "on %s (%s: %s)",
                path,
                type(exc).__name__,
                exc,
            )
            return None

    def _state_db_path(self) -> Optional[Path]:
        """The file this engine's connection writes to — or ``None``.

        The database knows its own file; asking it is more reliable than
        asking the environment, which may point somewhere else.

        ``PRAGMA database_list`` answers three columns — the sequence
        number, the name and the file — so the name is the **second**
        one and the file the third.
        """
        try:
            rows = self._conn.execute("PRAGMA database_list").fetchall()
        except Exception:  # noqa: BLE001 — the tick must survive
            return None
        for row in rows:
            if len(row) < 3:
                continue
            if str(row[1] or "") == "main":
                path = str(row[2] or "")
                if path:
                    return Path(path)
        return None

    def _project_dir_of(self, plan_id: str) -> Optional[Path]:
        """The tree this plan's work happens in — where its fix lives.

        The execution row is the authority on that: it is where the
        plan was actually worked on, so it is the tree that carries the
        repair once the plan is done.
        """
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )

        try:
            summary = ExecutionRepository(self._conn).summary(plan_id)
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.debug(
                "[scheduler] the project directory of the plan %s could not "
                "be read (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )
            return None
        if not summary:
            return None
        project_dir = str(summary.get("project_dir") or "").strip()
        if not project_dir:
            return None
        return Path(project_dir)

    # ------------------------------------------------------------------
    # The pruning
    # ------------------------------------------------------------------

    def _prune_failed_plan(self, outcome: Dict[str, Any]) -> None:
        """Take a plan that failed for good out of the scheduler.

        Some failures are answers, and an answer does not change by
        being asked again — the plan is not there, the plan is
        archived, the request conflicts with the plan's own state.
        Such a plan is not waiting any more, and a plan that is not
        waiting must not hold the line: the plans behind it would wait
        for ever for a plan that will never run.

        So the plan is **pruned**, and the pruning is three writes and
        a warning:

        * the **queue row** goes — the plan is not in the line any
          more;
        * the **slot** goes — but only when the plan was *admitted*:
          a plan that failed to be *stopped* may still be running, and
          a room somebody is working in is not given back on a
          guess;
        * the **phase** becomes ``stopped`` — the terminal word for a
          plan that will not run again.

        The caller has already decided that the failure is permanent
        (:func:`is_permanent_failure`); a failure that may go away by
        itself keeps its place in the queue and is tried again on the
        next tick.

        Args:
            outcome: what the sink said about the decision — its
                ``action``, its ``plan_id`` and the ``error`` it
                raised.
        """
        plan_id = str(outcome.get("plan_id") or "")
        action = str(outcome.get("action") or "")
        error = outcome.get("error")

        if not plan_id:
            log.warning(
                "the scheduler could not prune a plan it has no id for: %r",
                outcome,
            )
            return

        try:
            self._store.remove(self._conn, plan_id)
        except Exception as exc:  # noqa: BLE001 — the prune must not raise
            log.warning(
                "the scheduler could not take plan %s out of the queue "
                "(%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )

        if action == "admit":
            try:
                self._store.delete_slot(self._conn, plan_id)
            except Exception as exc:  # noqa: BLE001 — the prune must not raise
                log.warning(
                    "the scheduler could not give plan %s its slot back "
                    "(%s: %s)",
                    plan_id,
                    type(exc).__name__,
                    exc,
                )

        self._mark_plan_stopped(plan_id)

        log.warning(
            "the scheduler pruned plan %s after %s failed permanently "
            "(%s: %s)",
            plan_id,
            action,
            type(error).__name__,
            error,
        )

    def _mark_plan_stopped(self, plan_id: str) -> None:
        """Put the plan's phase where it will never move again.

        The phase is the **state machine's** word, so it is written
        through the state machine's own repository and not in SQL of
        the scheduler's own: the transition, the history that goes
        with it and the version the row carries are one write there,
        and the scheduler does not get a second, thinner version of
        them.

        A phase that cannot be written — the plan has no row at all,
        the row carries another phase, the database is busy — is
        logged and nothing more: the plan is not running, and a plan
        that is not running must not stay in the queue because a word
        could not be written about it.

        Args:
            plan_id: the plan to stop.
        """
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        try:
            RoutingRepository(self._conn).try_mark_phase(
                plan_id, (QUEUED_PHASE,), "stopped"
            )
        except Exception as exc:  # noqa: BLE001 — the phase is not the queue
            log.warning(
                "the scheduler could not mark plan %s as stopped (%s: %s)",
                plan_id,
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------
    # The capacity rules
    # ------------------------------------------------------------------

    def _release_hitl_slots(self) -> List[str]:
        """Give the slot back to the plans that are waiting for an answer.

        A plan that is parked in front of a human holds its slot **only
        while its process is alive**.  The process is what occupies the
        machine, and the pending decision is a *guard*: it is what
        stops a parked plan from being read as idle work.  It is never a
        reason to give the slot back on its own — a plan that is still
        running is still running, whoever is waiting to hear about it.

        So the rule is a double condition, and the four quadrants around
        it are:

        * pending and alive — the plan holds the slot;
        * pending and gone — the slot is given back;
        * no pending and alive — the plan holds the slot (it is working);
        * no pending and gone — the slot is given back (the run is over).

        The last two are the ordinary rule, and the ledger's
        reconciliation is what carries them out.  The first two are
        this rule, and the engine judges them itself — so the answer to
        "is this parked plan holding the machine?" is one place, and it
        is the tick's own.

        A slot is only given back when the plan is **not working at
        all**: neither a live execution process nor a live verification
        round.  A slot that is free on paper while the round behind it
        is still running is worse than no slot at all — it is room the
        scheduler believes in and the machine does not have.

        The release reads first and writes second: the plans that may
        give their slot back are worked out before anything is written,
        and a tick with nobody to release does not open a write
        transaction at all.  A scheduler that writes on every turn
        holds the database's write lock on every turn, and everything
        else in the process waits behind that lock.

        Returns:
            The plans whose slot was given back, in the order the slots
            are held.  Empty when there is nobody to release, which is
            an answer and not an error.
        """
        slots = self._store.list_slots(self._conn)
        if not slots:
            # Nobody holds a slot: there is nothing to give back, and
            # the tick does not ask the plan facts about nothing.
            return []
        working = {
            str(row.get("plan_id") or "")
            for row in self._facts.active_plans()
        }
        candidates: List[str] = []
        for slot in slots:
            plan_id = str(slot.get("plan_id") or "")
            if not plan_id or plan_id in working:
                # Something of the plan is still running: it keeps
                # the slot, whatever it is waiting for.
                continue
            if not self._facts.has_pending_decision(plan_id):
                # Nobody is waiting for an answer about this plan:
                # the ledger's own rule is the one that applies.
                continue
            candidates.append(plan_id)
        if not candidates:
            # Nobody is waiting for an answer about a plan that is not
            # working: the tick does not open a write transaction it
            # has no work for.
            return []
        released: List[str] = []
        with self._store.transaction(self._conn):
            for plan_id in candidates:
                self._store.delete_slot(self._conn, plan_id)
                released.append(plan_id)
        return released

    def _converge_over_capacity(self, state: Dict[str, Any]) -> List[str]:
        """Interrupt the plans that are over the capacity.

        The candidates and the choice among them are the policy's: the
        plans that have been running long enough, and among those the
        youngest one.  The loop stops as soon as the scheduler fits
        inside its own limit again — and it stops with an answer when
        nobody is left it may interrupt, because a plan that has not had
        its turn yet is never interrupted for the sake of a number.

        The slot of an interrupted plan is given back when the plan is
        *observed* to be gone, which is a tick later than the decision:
        the scheduler may not count a room as free before the work in
        it has really stopped.

        Args:
            state: the state the tick is deciding on.

        Returns:
            The plans that were interrupted, youngest first.  Empty when
            the scheduler is inside its limit, or when nobody may be
            interrupted.
        """
        capacity = int(state["capacity"])
        if int(state["used"]) <= capacity:
            return []
        victims = converge(self._convergence_view(state), capacity)
        for victim in victims:
            self._decide("stop", victim, "over_capacity")
        if victims:
            log.warning(
                "the scheduler is over its capacity (used %s, capacity %s) "
                "and interrupted %s plan(s): %s",
                state.get("used"),
                capacity,
                len(victims),
                ", ".join(victims),
            )
        return victims

    def _convergence_view(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """The state as the over-capacity rule reads it.

        The running rows of the state do not say what kind of plan each
        one is — and the eviction rule's protection is read from that
        kind (:func:`plan_scheduler.policy.is_protected`): a
        framework-fix plan may not be interrupted, because two repair
        plans making way for each other would be a livelock.

        So the rule reads a **copy** whose rows say the kind.  The rows
        the state itself carries keep their own shape: the state is
        what the panel and the snapshot read, and their shape is not
        the eviction rule's business.
        """
        routing = self._routing_facts()
        if not routing:
            return state
        running: List[Dict[str, Any]] = []
        for row in state.get("running", []):
            plan_id = str(row.get("plan_id") or "")
            kind = str((routing.get(plan_id) or {}).get("kind") or "")
            if not kind:
                running.append(row)
                continue
            annotated = dict(row)
            annotated["plan_kind"] = kind
            running.append(annotated)
        view = dict(state)
        view["running"] = running
        return view

    def _workspace_is_busy(self, key: Any, held: Any) -> bool:
        """Return whether the directory a plan works in is taken.

        Two things can hold a directory: a plan that is working in it
        right now, and a plan that was admitted earlier in this very
        tick and has not started working yet.  The second one is not in
        the ledger — the ledger knows the plans that are *working* — so
        the tick keeps the keys it has handed out and asks about them
        as well.

        A plan with no directory of its own is not blocked: there is
        nothing for it to collide with.

        Args:
            key: the workspace key of the plan, or ``None``.
            held: the keys the tick has handed out in this tick.

        Returns:
            ``True`` when the directory is taken.
        """
        if key is None:
            return False
        if str(key) in set(held or ()):
            return True
        return bool(self._ledger.workspace_busy(key))

    def _admit(
        self,
        state: Dict[str, Any],
        plan_id: str,
        priority: int = 0,
        reason: str = "running",
    ) -> None:
        """Give a plan its slot and decide that it starts.

        The slot is written before the decision is made, and it carries
        the moment the plan took it: the ledger keeps that moment once
        the plan is working, and a plan that looked young because its
        slot was stamped a tick late would lose its protection.  A
        store that refuses the write must not produce a decision that
        nothing backs.

        The slot carries the **priority** as well, and it carries it
        because this is the last moment it can: the queue row the
        priority came from is taken out of the table the moment the
        plan is admitted.  A running plan that was not given its
        priority here can no longer be read for one at all.

        Args:
            state: the state the tick is deciding on — its ``used``
                count goes up by one.
            plan_id: the plan that is admitted.
            priority: the priority the plan runs at — the number the
                queue row carried.  A plan nobody gave a number to
                runs at 0.
            reason: the word the decision carries.  It is one of the
                closed vocabulary the panel, the dry run and the API
                draw from: an ordinary plan is admitted because it is
                ``running``, a repair plan because it is a
                ``framework_fix_priority``.
        """
        self._store.upsert_slot(
            self._conn, plan_id, _stamp(self._now()), priority=int(priority)
        )
        self._decide("admit", plan_id, reason)
        state["used"] = int(state["used"]) + 1

    # ------------------------------------------------------------------
    # The state
    # ------------------------------------------------------------------

    def _read_state(
        self,
        conn: Optional[sqlite3.Connection] = None,
        facts: Any = None,
        ledger: Any = None,
    ) -> Dict[str, Any]:
        """Read the state the tick decides on.

        Every answer is derived from what is in the database at this
        moment: the plan facts from the reader, the queue and the slots
        from the store.  Nothing is cached between the ticks.

        The connection, the facts and the ledger are the ones the
        caller reads on.  The tick reads the write connection — it
        decides on what it has just written — and the read model reads
        the read connection, so that the panel's document does not wait
        for the worker's transaction.  A caller that says nothing reads
        what the engine reads by default: the write connection.

        The capacity comes with the word that says where it came from,
        so the read model may show the operator that the number in
        force is an override and not the configured capacity.

        The parked plans are in the state too: a plan parked for quota
        holds no slot and is in no line, so the read model would not
        see it at all without them.

        The commands that are still in flight are in the state as well:
        a plan that is asked to stop and is not answered yet is in
        neither the running line nor the queue, so the read model has
        to name it somewhere — otherwise the operator cannot tell "the
        pause worked" from "the pause was lost".
        """
        now = self._now()
        conn = self._conn if conn is None else conn
        facts = self._facts if facts is None else facts
        ledger = self._ledger if ledger is None else ledger
        waiting = [
            {
                "plan_id": str(row.get("plan_id") or ""),
                "priority": int(row.get("priority") or 0),
                "seq": row.get("seq"),
                "due": bool(row.get("due", True)),
                # The phase the plan is in, as the facts read it and
                # passed it on.  The value travels as it is: the
                # engine does not map it to a label, because a label
                # is the routing layer's business.
                "phase": row.get("phase"),
            }
            for row in facts.queued_plans()
            if str(row.get("plan_id") or "")
        ]

        capacity, capacity_source = self._capacity_and_source(conn=conn)

        state: Dict[str, Any] = {
            "now": now,
            "capacity": capacity,
            "capacity_source": capacity_source,
            "used": ledger.used(),
            "min_runtime_seconds": self._min_runtime(),
            # The tick never interrupts anybody to make room: the
            # convergence above is the only thing that does, and the
            # explicit start is the only other one.
            "allow_evict": False,
            # The plans that are waiting — the facts' answer.
            "waiting": waiting,
            # The line — the queue table's answer, in the shared order.
            "queued": [],
            "queue": [],
            "running": self._running_view(
                self._store.list_slots(conn), now, facts=facts
            ),
            # The plans parked for quota — the facts' answer.  A parked
            # plan holds no slot and is in no line, so without this the
            # read model could not see the one plan that stopped for
            # want of provider capacity.
            "parked": self._parked_plans(facts=facts),
            # The commands that are still in flight — the command
            # log's answer.  A plan that is asked to stop and is not
            # answered yet is in neither the running line nor the
            # queue, so the read model has to name it somewhere.
            "stopping": self._open_commands(conn=conn),
        }
        self._rebuild_line(state, conn=conn, facts=facts, ledger=ledger)
        return state

    def _open_commands(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        """Return the commands that were accepted and are not answered yet.

        The command log is the scheduler's own fact: a command is
        written the moment the worker takes it, with the outcome it
        has *so far* (``accepted``), and it is turned into ``applied``
        or ``failed`` only once the worker has answered.  A command
        that is still ``accepted`` is therefore a command that is still
        in flight.

        A store that cannot answer — one whose database another writer
        holds the lock on — costs the read model nothing: the answer
        is "no command is in flight", which is the safe direction, and
        the tick itself keeps going.  The failure is logged rather
        than swallowed.

        The command log is read on the connection the caller reads on:
        the tick reads the write connection — it decides on what it
        has just written — and the read model reads the read
        connection, so the document does not wait for the worker's
        transaction.  A command log read on the write connection would
        put the lock the split took out right back.

        The reader is duck-typed on purpose, like the parked plans':
        the engine asks the store what it knows, and a fact the store
        does not carry is not a fact the engine may insist on.
        """
        reader = getattr(self._store, "list_open_commands", None)
        if not callable(reader):
            return []
        conn = self._conn if conn is None else conn
        try:
            rows = reader(conn)
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the open commands could not be read (%s: %s); "
                "reporting none",
                type(exc).__name__,
                exc,
            )
            return []
        try:
            return list(rows or [])
        except TypeError:
            return []

    def _parked_plans(self, facts: Any = None) -> List[Dict[str, Any]]:
        """Return the plans parked for quota, or none when the facts cannot say.

        The parked set is a fact the tick does not need to admit
        anybody: a parked plan holds no slot and is in no line.  So a
        facts reader that cannot answer the question — one that does
        not know how to read the parked set at all, or one whose read
        failed — costs the tick nothing.  The answer degrades to
        "nothing is parked", which is the safe direction: the parked
        plans wait one tick longer and every other plan is admitted
        as usual.

        The reader is duck-typed on purpose: the engine asks the
        facts what they know, and a fact the reader does not carry is
        not a fact the engine may insist on.

        The facts are the ones the caller reads on: the read model
        reads the read-side facts, so the parked set it shows is the
        parked set of the database it reads.
        """
        source = self._facts if facts is None else facts
        reader = getattr(source, "parked_plans", None)
        if not callable(reader):
            return []
        try:
            rows = reader()
        except Exception as exc:  # noqa: BLE001 — the tick must survive
            log.warning(
                "[scheduler] the parked plans could not be read (%s: %s); "
                "reporting none",
                type(exc).__name__,
                exc,
            )
            return []
        try:
            return list(rows or [])
        except TypeError:
            return []

    def _rebuild_line(
        self,
        state: Dict[str, Any],
        conn: Optional[sqlite3.Connection] = None,
        facts: Any = None,
        ledger: Any = None,
    ) -> None:
        """Rebuild the line from the queue table and the plan facts.

        The queue table says **where** the plans stand; the facts say
        **whether** each of them may start.  The line is the two
        together, in the queue table's order — the order the shared
        comparator gives, which is the order the panel shows and the
        order the admission check reads.

        A plan that is waiting **without** a row is in the line as
        well: it is waiting, and the line is the waiting plans.  Its
        row is synthesized for the read model — nothing is written
        here — and it carries the number and the priority the row will
        hold once the reconcile has landed it, so the line does not
        move when the rows arrive.

        The queue table, the facts and the ledger are the ones the
        caller reads on: the tick reads the write connection and the
        read model reads the read one.

        Each row also carries the ``phase`` the plan is in, as the
        facts read it: the line says what each waiting plan is, not
        only that it is waiting.  The value travels as it is — the
        engine does not map it to a label, because a label is the
        routing layer's business.
        """
        conn = self._conn if conn is None else conn
        facts = self._facts if facts is None else facts
        ledger = self._ledger if ledger is None else ledger
        waiting_rows = list(state.get("waiting", []))
        waiting = {
            str(row["plan_id"]): row for row in waiting_rows
        }
        routing = self._routing_facts(conn=conn)
        queued: List[Dict[str, Any]] = []
        existing_ids: Set[str] = set()
        max_seq = 0
        for row in self._store.list_queue(conn):
            plan_id = str(row.get("plan_id") or "")
            existing_ids.add(plan_id)
            # The highest number the table has handed out.  It is read
            # over the whole table and not only over the plans that
            # are still waiting: a number that was handed out is not
            # handed out again, and the reconcile hands the next one
            # out the same way.
            try:
                max_seq = max(max_seq, int(row.get("seq")))
            except (TypeError, ValueError):
                pass
            fact = waiting.get(plan_id)
            if fact is None:
                continue
            queued.append(
                {
                    "plan_id": plan_id,
                    "priority": int(row.get("priority") or 0),
                    "seq": row.get("seq"),
                    # The promotion is the table's to record: a plan
                    # that was never promoted is not promoted now.
                    "promote_order": row.get("promote_order"),
                    "due": bool(fact.get("due", True)),
                    # The kind of the plan, as the routing row says it.
                    # The framework-fix lane's plans are not in the
                    # ordinary line — they have a lane of their own.
                    "plan_kind": str(
                        (routing.get(plan_id) or {}).get("kind") or ""
                    ),
                    # The phase the plan is in, as the facts read it
                    # and passed it on.  The engine does not map it to
                    # a label: the label is the routing layer's job.
                    "phase": fact.get("phase"),
                    # A plan that is in the queue is waiting, and a
                    # waiting plan is neither paused nor parked.
                    "paused": False,
                    "hitl_pending": bool(
                        facts.has_pending_decision(plan_id)
                    ),
                    "workspace_busy": bool(
                        ledger.workspace_busy(
                            facts.workspace_key(plan_id)
                        )
                    ),
                }
            )
        queued.extend(
            self._synthesized_queued_rows(
                waiting_rows,
                existing_ids,
                max_seq,
                facts=facts,
                ledger=ledger,
            )
        )
        state["queued"] = queued
        state["queue"] = self._queue_view(state, conn=conn)

    def _synthesized_queued_rows(
        self,
        waiting: List[Dict[str, Any]],
        existing_ids: Set[str],
        max_seq: int,
        facts: Any = None,
        ledger: Any = None,
    ) -> List[Dict[str, Any]]:
        """Return the rows the waiting plans do not have yet.

        A plan that is waiting without a queue row is in the line: the
        line is the plans that are waiting, and the plan was waiting
        before the table heard of it.  So the read model gives it a row
        of its own — a **synthesized** one, which lives in the read
        model and nowhere else.  Nothing here is written: the row is
        written by the reconcile, in the reconcile's own transaction.

        The rows join the **tail**, in the order the facts give them:
        the plan that has been waiting longer stands in front.  Their
        numbers are the numbers the reconcile hands out — the table's
        highest number plus one, and one more for each row after it —
        and their priorities are the priorities their rows will carry,
        so the line the operator is looking at does not move when the
        rows arrive.

        A plan that has a row of its own is left alone: its row says
        where it stands, and a number that was handed out is not
        handed out twice.

        Args:
            waiting: the plans that are waiting, in the facts' order.
            existing_ids: the plans that already have a queue row.
            max_seq: the highest number the queue table has handed out.
            facts: the facts the caller reads on.
            ledger: the ledger the caller reads on.

        Returns:
            One row per waiting plan that has no row of its own, in the
            waiting order.  Empty when every waiting plan has a row.
        """
        facts = self._facts if facts is None else facts
        ledger = self._ledger if ledger is None else ledger
        rows: List[Dict[str, Any]] = []
        seq = int(max_seq)
        for entry in waiting:
            plan_id = str(entry.get("plan_id") or "")
            if not plan_id or plan_id in existing_ids:
                continue
            seq += 1
            # The priority is the one the caller gave the plan, read
            # out of the plan's own artifact — the number the row will
            # carry.  The facts have no priority to give for a plan
            # that has no row: their answer for such a plan is the
            # default, and a default the row will not carry would move
            # the plan when the row lands.  A plan that expressed no
            # intent runs at the ordinary weight, which is what its
            # row will say as well.
            intended = self._intended_priority(plan_id)
            priority = (
                int(intended)
                if intended is not None
                else int(entry.get("priority") or 0)
            )
            rows.append(
                {
                    "plan_id": plan_id,
                    "priority": priority,
                    "seq": seq,
                    # The promotion is the table's to record: a plan
                    # that was never promoted is not promoted now.
                    "promote_order": None,
                    "due": bool(entry.get("due", True)),
                    # The phase the plan is in, as the facts read it.
                    "phase": entry.get("phase"),
                    # A plan that is in the line is waiting, and a
                    # waiting plan is neither paused nor parked.
                    "paused": False,
                    "hitl_pending": bool(
                        facts.has_pending_decision(plan_id)
                    ),
                    "workspace_busy": bool(
                        ledger.workspace_busy(
                            facts.workspace_key(plan_id)
                        )
                    ),
                }
            )
        return rows

    def _queue_view(
        self,
        state: Dict[str, Any],
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        """The queue as the panel reads it.

        The rows come back in the shared comparator's order, and the
        head carries the reason the policy gave for it — the same
        reason the admission check reached.

        The view is built from the **line** — the state the tick
        decided on — and not from the queue table: the table does not
        know about a plan that is waiting yet, and a view that read
        the table would drop the plan the line holds.  The connection
        is accepted and left alone: the view reads no table at all.

        A row the facts do not vouch for is not in the line at all:
        the queue table may still carry a plan whose directory is
        gone, and the panel must not render a plan that cannot run.
        The row is skipped, and the head of the view is therefore the
        head of the **line** — the first plan the facts answer for.

        The plans behind the head carry their **own** reason, and the
        one that matters to them is the workspace: a plan whose
        directory is held is waiting for a directory, not for a slot,
        and saying otherwise would send the operator looking in the
        wrong place.  The head's verdict is the policy's and says
        nothing about the plans behind it — so each of them is asked
        about the directory it works in.

        Every row carries the phase the plan is in, as the facts read
        it and the line passed it on.  The value travels as it is: the
        engine does not map it to a label, because the label is the
        routing layer's business and a second mapping here would be a
        second answer to the same question.
        """
        verdict = admission_verdict(state)
        view: List[Dict[str, Any]] = []
        for entry in state.get("queued", []):
            row = {
                "plan_id": str(entry.get("plan_id") or ""),
                "seq": entry.get("seq"),
                "priority": int(entry.get("priority") or 0),
                "promote_order": entry.get("promote_order"),
                "due": bool(entry.get("due", True)),
                # The phase the plan is in, as the line read it — the
                # facts' answer, passed on as it is.  The engine does
                # not map it to a label: the label is the routing
                # layer's job, and a second mapping here would be a
                # second answer to the same question.
                "phase": entry.get("phase"),
            }
            if not view:
                row["reason"] = verdict
            else:
                row["reason"] = self._reason_behind(entry)
            view.append(row)
        return view

    def _reason_behind(self, entry: Optional[Dict[str, Any]]) -> str:
        """Return the reason a plan behind the head of the queue waits.

        A plan that is not at the head cannot be running yet, so it is
        waiting for something.  The only thing the tick can say about
        it for certain is whether its directory is taken — and that is
        the reason it gets, because a plan waiting for a directory is
        waiting for a directory, whatever the plan in front of it is
        doing.

        Args:
            entry: what the facts say about the queued plan, or ``None``
                when the queue table holds a plan the facts do not.

        Returns:
            ``workspace_busy`` when a working plan holds the
            directory, and ``waiting_for_slot`` otherwise.
        """
        if entry is not None and bool(entry.get("workspace_busy")):
            return "workspace_busy"
        return "waiting_for_slot"

    def _running_view(
        self,
        slots: Sequence[Dict[str, Any]],
        now: float,
        facts: Any = None,
    ) -> List[Dict[str, Any]]:
        """The plans that hold compute, as the policy reads them.

        The **facts** say who is working; the slots say when each of
        them took its slot.  The facts are the ones the caller reads
        on: the read model hands over its own reader, so the document
        does not fall back on the write connection and wait for the
        worker.  A caller that says nothing reads what the engine
        reads by default — the write-side facts.

        A slot row for a plan that is not working is a reservation
        the ledger has not reconciled away yet, and it is not a plan
        that is running: the panel and the eviction rule both talk
        about the plans that are working, and a plan that has been
        admitted but has not started yet is neither.

        The facts also say when the plan really started, and that
        stamp is passed on as it is.  The slot's moment is the
        scheduler's clock — it starts again from zero on every
        restart — so the plan's own start time travels with the row
        for whoever wants to say how long the plan has been at it.

        The **slot** carries the priority the plan runs at, and the
        row carries it too: the eviction rule compares the running
        plans by the number the caller gave them, and a running row
        without one cannot be compared at all.  A slot that holds no
        priority — a row written before the column existed, or one the
        store could not read — leaves the plan at the ordinary weight,
        and so does a plan that has no slot row at all.

        The phase the plan is in travels with it as well: the facts
        read it from the routing row and the row carries it on,
        unchanged, so that the panel can say what the plan is doing
        and not only that it is working.

        """
        facts = self._facts if facts is None else facts
        held: Dict[str, Dict[str, Any]] = {}
        for slot in slots:
            plan_id = str(slot.get("plan_id") or "")
            if not plan_id:
                continue
            moment = self._epoch(slot.get("acquired_at"))
            held[plan_id] = {
                "acquired_at": moment if moment is not None else now,
                "priority": _safe_priority(slot),
            }

        running: List[Dict[str, Any]] = []
        for row in facts.active_plans():
            plan_id = str(row.get("plan_id") or "")
            if not plan_id:
                continue
            held_slot = held.get(plan_id, {})
            moment = held_slot.get("acquired_at", now)
            running.append(
                {
                    "plan_id": plan_id,
                    "acquired_at": moment,
                    "elapsed": max(0.0, now - moment),
                    "workspace": row.get("workspace"),
                    "started_at": row.get("started_at"),
                    # The weight the caller gave the plan — the
                    # number the eviction rule compares the running
                    # plans by.  Read as it is; a slot without a
                    # usable one leaves the plan at 0.
                    "priority": int(held_slot.get("priority", 0)),
                    # The phase the plan is in, as the facts read it
                    # and passed it on — the value is the plan's own,
                    # not the engine's guess at it.
                    "phase": row.get("phase"),
                }
            )
        return running

    # ------------------------------------------------------------------
    # The commands, in the worker
    # ------------------------------------------------------------------

    def _handle_command(self, request: _CommandRequest) -> None:
        """Carry one command out and answer it.

        The command is written down **here**, on the worker, with the
        outcome it has *so far* (``accepted``) — the command log is the
        scheduler's own fact, and the scheduler's own facts are written
        by this thread and by no other.  The caller is told about the id
        through the request's ``recorded_event``, so the id it is given
        names a row that exists.

        The record is finished when the command is over — whatever the
        outcome is.  A command that raises is finished as ``failed``: it
        is not lost, and it is not mistaken for an action that was carried
        out.

        The command is carried out **once**.  The record that was
        written when the command was received is the record that is
        finished, and the store writes an outcome onto a pending record
        only once: a command that is somehow handled twice changes
        nothing the second time.
        """
        if not self._bound:
            # The scheduler may not run: it carries out nothing.  The
            # command is answered as failed, so that it is neither lost
            # nor mistaken for an action that was carried out — and
            # nothing is written, because there is no database to write
            # on.
            request.recorded_event.set()
            request.result = EngineCommandResult(
                outcome=FAILED,
                command_id=None,
                detail=(
                    "the scheduler is switched off: it carries out no "
                    "commands until the configuration says it may run"
                ),
                plan_id=request.plan_id,
                action=request.action,
            )
            request.result_event.set()
            return

        command_id = request.command_id
        if command_id is None:
            # The command could not be written down when it was
            # received; it is written down now, before it is carried
            # out, so that it is on record either way.
            command_id = self._record_command(request.plan_id, request.action)
        request.command_id = command_id
        # The handshake, whether the row could be written or not: a
        # caller that is left waiting for a record that will never come
        # would wait for the whole bound and learn nothing.
        request.recorded_event.set()

        try:
            result = self._run_action(request, command_id)
        except CommandRejected as rejection:
            # The lifecycle refused the action.  That is not a failure
            # of the scheduler: the command was carried out, the action
            # was not, and the refusal travels back to the caller as it
            # came.
            result = self._rejected_command(request, command_id, rejection)
        except Exception as exc:  # noqa: BLE001 — the failure is the answer
            result = self._failed_command(request, command_id, exc)

        request.result = result
        request.result_event.set()

    def _run_action(
        self, request: _CommandRequest, command_id: Optional[int]
    ) -> CommandResult:
        """Hand the command to the executor and finish its record.

        The executor is handed an id that matches no row: the engine
        owns the command's record — it wrote the command down when it
        took the command out of the mailbox, and it finishes that record
        itself — so the executor neither finishes the engine's record
        nor writes a second one.
        """
        handler = getattr(self._commands, request.action, None)
        if handler is None or not callable(handler):
            raise RuntimeError(
                "the command executor does not know the action {0!r}".format(
                    request.action
                )
            )
        outcome = handler(request.plan_id, command_id=NO_COMMAND_ROW)
        if command_id is not None:
            self._finish_command(command_id, outcome)
        return EngineCommandResult(
            outcome=outcome.outcome,
            command_id=command_id,
            detail=outcome.detail,
            plan_id=request.plan_id,
            action=request.action,
        )

    def _finish_command(self, command_id: int, outcome: CommandResult) -> None:
        """Write the outcome a command reached into its record.

        The record's outcome says what happened to the **command** — it
        was carried out — and the record's detail says what the
        **action** did, so nothing is lost when the two words differ.
        """
        detail = outcome.detail
        if outcome.outcome:
            detail = (
                "{0}: {1}".format(outcome.outcome, detail)
                if detail
                else outcome.outcome
            )
        try:
            self._store.finish_command(
                self._conn, command_id, DONE, detail=detail
            )
        except Exception as finish_exc:  # noqa: BLE001 — the record is the answer
            self._record_error("command", finish_exc)
            log.exception("the scheduler could not finish a command")

    def _rejected_command(
        self,
        request: _CommandRequest,
        command_id: Optional[int],
        rejection: CommandRejected,
    ) -> CommandResult:
        """Finish a command the lifecycle refused, and say that it refused.

        A refusal is **not** a failure of the scheduler: the command was
        carried out and the action was not.  The record says both — it
        is finished as ``done``, and its detail carries the status and
        the reason the lifecycle gave, so that the refusal can be read
        later without asking again.

        The refusal itself travels on the result, unchanged: the caller
        gets the lifecycle's own status and its own detail, and the
        route turns them back into the answer they were.

        Args:
            request: the command that was refused.
            command_id: the id of the command's record, when it has one.
            rejection: the refusal the lifecycle gave.

        Returns:
            The :class:`EngineCommandResult` that carries the refusal.
        """
        detail = "rejected: {0} {1}".format(
            rejection.status_code, rejection.detail
        )
        if command_id is not None:
            try:
                self._store.finish_command(
                    self._conn, command_id, DONE, detail=detail
                )
            except Exception as finish_exc:  # noqa: BLE001 — the record is the answer
                self._record_error("command", finish_exc)
                log.exception("the scheduler could not finish a refused command")
        log.info(
            "the scheduler's %s command for %s was refused: %s",
            request.action,
            request.plan_id,
            detail,
        )
        return EngineCommandResult(
            outcome=DONE,
            command_id=command_id,
            detail=detail,
            plan_id=request.plan_id,
            action=request.action,
            rejection=rejection,
        )

    def _failed_command(
        self,
        request: _CommandRequest,
        command_id: Optional[int],
        exc: BaseException,
    ) -> CommandResult:
        """Finish a command that raised, and say that it failed."""
        detail = "{0}: {1}".format(type(exc).__name__, exc)
        if command_id is not None:
            try:
                self._store.finish_command(
                    self._conn, command_id, FAILED, detail=detail
                )
            except Exception as finish_exc:  # noqa: BLE001 — the record is the answer
                self._record_error("command", finish_exc)
                log.exception("the scheduler could not finish a failed command")
        log.warning(
            "the scheduler's %s command for %s failed: %s",
            request.action,
            request.plan_id,
            detail,
        )
        return EngineCommandResult(
            outcome=FAILED,
            command_id=command_id,
            detail=detail,
            plan_id=request.plan_id,
            action=request.action,
        )

    # ------------------------------------------------------------------
    # The small helpers
    # ------------------------------------------------------------------

    def _new_result_event(self) -> threading.Event:
        """The event a command's answer arrives on.

        It is a method and not a bare ``threading.Event`` so that a
        case can hand the engine an event it can look at: the wait is
        the engine's contract with its caller, and a case that cannot
        see it cannot hold it down.
        """
        return threading.Event()

    def _new_recorded_event(self) -> threading.Event:
        """The event a command's **record** arrives on.

        It is the handshake that tells the caller the command's id: the
        worker sets it once the row exists, so the id the caller is
        given names a row that is really there.  It is a method for the
        same reason :meth:`_new_result_event` is one.
        """
        return threading.Event()

    def _record_command(self, plan_id: str, action: str) -> Optional[int]:
        """Write the command down, so it has an id before it is carried out.

        The command is written the moment the worker takes it out of the
        mailbox, with the outcome it has *so far* — ``accepted``,
        meaning the worker has it and has not answered yet.  That is
        what lets a reader tell "this was requested" apart from "this
        was carried out", and what lets the caller be answered with an id
        even when its wait runs out.

        The write happens **here**, on the worker: the command log is
        the scheduler's own fact, and the scheduler's own facts are
        written by this thread and by no other.

        A record that cannot be written is a failure the engine records
        and carries on from: the command is still carried out, and it
        simply has no id.

        Args:
            plan_id: the plan the command is about.
            action: what was asked for.

        Returns:
            The id of the command that was written, or ``None`` when it
            could not be written.
        """
        try:
            return self._store.record_command(
                self._conn, plan_id, action, outcome=PENDING
            )
        except Exception as exc:  # noqa: BLE001 — the command must not be lost
            self._record_error("command", exc)
            log.exception("the scheduler could not record a command")
            return None

    def _decide(self, action: str, plan_id: str, reason: str) -> None:
        """Make one decision."""
        self._tick_decisions.append(
            {
                "ts": _stamp(self._now()),
                "action": action,
                "plan_id": plan_id,
                "reason": reason,
            }
        )
        log.info(
            "scheduler decision: action=%s plan_id=%s reason=%s",
            action,
            plan_id,
            reason,
        )

    def _record_error(self, phase: str, exc: BaseException) -> None:
        """Remember a failure, so a case can ask what went wrong."""
        self._errors.append((phase, exc))

    def _read_config(self) -> Any:
        """Read the configuration snapshot, keeping the old one on failure."""
        try:
            config = self._config_loader.current()
        except Exception as exc:  # noqa: BLE001 — the old snapshot stays
            self._record_error("refresh_config", exc)
            log.warning(
                "the scheduler's configuration could not be read (%s); "
                "the previous snapshot is kept",
                exc,
            )
            return self._config
        return config if config is not None else self._config

    def _config_value(self, name: str, default: Any) -> Any:
        """Read one setting out of the current snapshot."""
        value = getattr(self._config, name, None)
        return default if value is None else value

    def _capacity_override(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[int]:
        """The capacity override in force, if there is one.

        The override is a row in the state database, and it is read
        **again on every call**: it is a knob the operator turns while
        the scheduler runs, so a value kept in the configuration
        snapshot would only take effect after a restart — which is
        exactly what the override is not for.

        The row is read on the connection the caller reads on: the
        tick reads the write connection and the read model reads the
        read one, so the number on the panel is the number of the
        database the panel reads.

        A row that does not hold a usable capacity is ignored rather
        than obeyed: the number of plans that may run at the same time
        is never negative, and a value that is not a number at all is
        not a number of plans.  Zero *is* usable — it is how the
        operator pauses the scheduling.

        A store that cannot answer is recorded and answered with
        ``None``: the tick goes on with the configured capacity
        instead of stopping over a setting.

        Returns:
            The capacity override in force, or ``None`` when there is
            none to obey.
        """
        conn = self._conn if conn is None else conn
        try:
            raw = self._store.get_setting(conn, CAPACITY_OVERRIDE_KEY)
        except Exception as exc:  # noqa: BLE001 — a tick must not die here
            self._record_error("capacity", exc)
            log.warning(
                "the scheduler's capacity override could not be read (%s); "
                "the configured capacity is used",
                exc,
            )
            return None
        if raw is None:
            return None
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def _capacity_and_source(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> Tuple[int, str]:
        """How many plans may run, and where that number came from.

        The override is read **once**, so the number and the word that
        says where it came from always agree: a capacity read as
        ``override`` is the operator's number, and one read as
        ``config`` is the configured one.  Two reads could disagree —
        the operator may clear the override between them — and a state
        that said ``config`` next to the override's number would be
        telling the panel a lie.

        The configured capacity is the fallback: the override wins
        while it is there, and removing it puts the configured capacity
        back.

        The override is read on the connection the caller reads on: the
        tick reads the write connection and the read model reads the
        read one.
        """
        override = self._capacity_override(conn=conn)
        if override is not None:
            return override, CAPACITY_SOURCE_OVERRIDE
        try:
            capacity = max(
                0, int(self._config_value("capacity", DEFAULT_CAPACITY))
            )
        except (TypeError, ValueError):
            capacity = DEFAULT_CAPACITY
        return capacity, CAPACITY_SOURCE_CONFIG

    def _capacity(self) -> int:
        """How many plans may run at the same time.

        The operator's override wins over the configured capacity while
        it is there; removing it puts the configured capacity back.
        """
        capacity, _source = self._capacity_and_source()
        return capacity

    def _min_runtime(self) -> float:
        """How long a young run is left alone."""
        try:
            return float(
                self._config_value(
                    "min_runtime_seconds", DEFAULT_MIN_RUNTIME_SECONDS
                )
            )
        except (TypeError, ValueError):
            return DEFAULT_MIN_RUNTIME_SECONDS

    def _tick_seconds(self) -> float:
        """How long the worker waits between two ticks."""
        try:
            return float(
                self._config_value("tick_seconds", DEFAULT_TICK_SECONDS)
            )
        except (TypeError, ValueError):
            return DEFAULT_TICK_SECONDS

    def _command_timeout(self) -> float:
        """How long a caller waits for a command to be answered."""
        try:
            return max(
                0.0,
                float(
                    self._config_value(
                        "command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS
                    )
                ),
            )
        except (TypeError, ValueError):
            return DEFAULT_COMMAND_TIMEOUT_SECONDS

    def _now(self) -> float:
        """The current moment, in epoch seconds."""
        value = self._clock() if callable(self._clock) else self._clock
        try:
            return float(value)
        except (TypeError, ValueError):
            return time.time()

    @staticmethod
    def _epoch(value: Any) -> Optional[float]:
        """Return a timestamp as epoch seconds, or ``None`` when it is not one."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            moment = value if value.tzinfo is not None else value.replace(
                tzinfo=timezone.utc
            )
            return moment.timestamp()
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()