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

import json
import logging
import queue
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .commands import CommandRejected, CommandResult
from .config import (
    DEFAULT_CAPACITY,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_MIN_RUNTIME_SECONDS,
    DEFAULT_TICK_SECONDS,
    SchedulerConfig,
)
from .facts import QUEUED_PHASE
from .policy import admission_verdict, converge
from .snapshot import (
    CAPACITY_SOURCE_CONFIG,
    CAPACITY_SOURCE_OVERRIDE,
    build,
)
from .store import CAPACITY_OVERRIDE_KEY

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "DONE",
    "ENGINE_OUTCOMES",
    "FAILED",
    "PENDING",
    "PENDING_ANSWER",
    "SCHEDULER_INTENT_FILE",
    "TICK_PHASES",
    "THREAD_NAME",
    "EngineCommandResult",
    "EngineParts",
    "PlanSchedulerEngine",
]


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
    """

    store: Any
    facts: Any
    ledger: Any
    commands: Any
    sink: Any
    conn: Any


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

#: The item that tells the worker to stop.
_SHUTDOWN = object()


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


def _stamp(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


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

    # ------------------------------------------------------------------
    # The recovery
    # ------------------------------------------------------------------

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

        The recovery decides nothing and starts nothing: the first tick
        after it is an ordinary tick.  A plan that is already working
        is not in ``queued``, so it cannot be admitted a second time.

        The whole reconciliation is one transaction: the queue and the
        slots are one fact about the plans that are waiting and the
        plans that are working, and a half-applied recovery is a
        scheduler that counts a plan it has no record of.

        Returns:
            A summary of what the recovery did: ``queued`` (the line
            as it stands now), ``backfilled`` (the plans that joined the
            tail), ``adopted`` (the plans that were timed from now) and
            ``removed_slots`` (the rows nobody held any more).
        """
        stamp = _stamp(self._now())
        summary: Dict[str, Any] = {
            "queued": [],
            "backfilled": [],
            "adopted": [],
            "removed_slots": [],
        }
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
        for slot in self._store.list_slots(self._conn):
            plan_id = str(slot.get("plan_id") or "")
            if not plan_id or plan_id in working:
                continue
            self._store.delete_slot(self._conn, plan_id)
            removed.append(plan_id)

        adopted: List[str] = []
        for plan_id in sorted(working):
            # The old row goes first: writing a slot does not restart
            # its clock, and the clock is the thing being reset.
            self._store.delete_slot(self._conn, plan_id)
            self._store.upsert_slot(self._conn, plan_id, stamp)
            adopted.append(plan_id)
        return adopted, removed

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
        # comes with it.
        if request.recorded_event.wait(timeout=limit):
            command_id = request.command_id
        else:
            # The worker has not even reached the command yet.  It will
            # write it down when it does, and the caller can come back
            # for the outcome; there is no id to hand out yet.
            command_id = None

        # The answer, once, with the bound.
        if not request.result_event.wait(timeout=limit):
            return EngineCommandResult(
                outcome=PENDING_ANSWER,
                command_id=command_id,
                detail="the command is still in the mailbox",
                plan_id=plan_id,
                action=action,
            )
        if request.result is None:  # pragma: no cover - defensive
            return EngineCommandResult(
                outcome=PENDING_ANSWER,
                command_id=command_id,
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

        A scheduler that has never been bound has no state to read:
        it answers with the idle document, which is what the panel
        shows for a scheduler that is not running.
        """
        if not self._bound:
            return build({})
        return build(self._read_state())

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
        self._bound = True
        self._owns_conn = True
        # The recovery is the worker's first job, and it can only run
        # once the engine knows the world it is recovering.
        self._recover_safely()
        return True

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
        """
        state = self._state
        if state is None:
            return
        waiting = {str(row["plan_id"]) for row in state.get("waiting", [])}
        existing = {str(row["plan_id"]) for row in state.get("queue", [])}

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
        """
        state = self._state
        if state is None:
            return
        self._release_hitl_slots()
        self._ledger.reconcile()
        state["used"] = self._ledger.used()
        state["running"] = self._running_view(
            self._store.list_slots(self._conn), self._now()
        )

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

        The head of the queue is the plan the next admission is about,
        and the policy says whether it may start: it has to be due, its
        workspace has to be free, and a slot has to be waiting.  A
        head that may not start holds the queue — the line is a line,
        and the scheduler does not skip anybody.
        """
        state = self._state
        if state is None:
            return
        held: set = set()
        while True:
            if int(state["used"]) >= int(state["capacity"]):
                return
            queued = state["queued"]
            if not queued:
                return
            if admission_verdict(state) != "running":
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
            self._admit(state, plan_id)
            if key is not None:
                held.add(key)
            state["queued"] = queued[1:]

    def _phase_execute(self) -> None:
        """Carry the decisions out.

        The sink is the exit: an execute sink starts and stops the
        plans through the lifecycle, a record sink writes the
        decisions down and touches nothing else.  The tick itself
        changes nothing here — it hands the decisions over.
        """
        self._sink.apply(list(self._tick_decisions))

    def _phase_record(self) -> None:
        """Write the decisions down.

        The record is the same four fields the dry run writes, so the
        two exits can be lined up against each other.
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
        victims = converge(state, capacity)
        for victim in victims:
            self._decide("stop", victim, "over_capacity")
        return victims

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

    def _admit(self, state: Dict[str, Any], plan_id: str) -> None:
        """Give a plan its slot and decide that it starts.

        The slot is written before the decision is made, and it carries
        the moment the plan took it: the ledger keeps that moment once
        the plan is working, and a plan that looked young because its
        slot was stamped a tick late would lose its protection.  A
        store that refuses the write must not produce a decision that
        nothing backs.

        Args:
            state: the state the tick is deciding on — its ``used``
                count goes up by one.
            plan_id: the plan that is admitted.
        """
        self._store.upsert_slot(self._conn, plan_id, _stamp(self._now()))
        self._decide("admit", plan_id, "running")
        state["used"] = int(state["used"]) + 1

    # ------------------------------------------------------------------
    # The state
    # ------------------------------------------------------------------

    def _read_state(self) -> Dict[str, Any]:
        """Read the state the tick decides on.

        Every answer is derived from what is in the database at this
        moment: the plan facts from the reader, the queue and the slots
        from the store.  Nothing is cached between the ticks.

        The capacity comes with the word that says where it came from,
        so the read model may show the operator that the number in
        force is an override and not the configured capacity.
        """
        now = self._now()
        waiting = [
            {
                "plan_id": str(row.get("plan_id") or ""),
                "priority": int(row.get("priority") or 0),
                "seq": row.get("seq"),
                "due": bool(row.get("due", True)),
            }
            for row in self._facts.queued_plans()
            if str(row.get("plan_id") or "")
        ]

        capacity, capacity_source = self._capacity_and_source()

        state: Dict[str, Any] = {
            "now": now,
            "capacity": capacity,
            "capacity_source": capacity_source,
            "used": self._ledger.used(),
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
                self._store.list_slots(self._conn), now
            ),
        }
        self._rebuild_line(state)
        return state

    def _rebuild_line(self, state: Dict[str, Any]) -> None:
        """Rebuild the line from the queue table and the plan facts.

        The queue table says **where** the plans stand; the facts say
        **whether** each of them may start.  The line is the two
        together, in the queue table's order — the order the shared
        comparator gives, which is the order the panel shows and the
        order the admission check reads.
        """
        waiting = {
            str(row["plan_id"]): row for row in state.get("waiting", [])
        }
        queued: List[Dict[str, Any]] = []
        for row in self._store.list_queue(self._conn):
            plan_id = str(row.get("plan_id") or "")
            fact = waiting.get(plan_id)
            if fact is None:
                continue
            queued.append(
                {
                    "plan_id": plan_id,
                    "priority": int(row.get("priority") or 0),
                    "seq": row.get("seq"),
                    "due": bool(fact.get("due", True)),
                    # A plan that is in the queue is waiting, and a
                    # waiting plan is neither paused nor parked.
                    "paused": False,
                    "hitl_pending": bool(
                        self._facts.has_pending_decision(plan_id)
                    ),
                    "workspace_busy": bool(
                        self._ledger.workspace_busy(
                            self._facts.workspace_key(plan_id)
                        )
                    ),
                }
            )
        state["queued"] = queued
        state["queue"] = self._queue_view(state)

    def _queue_view(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The queue as the panel reads it.

        The rows come back in the shared comparator's order, and the
        head carries the reason the policy gave for it — the same
        reason the admission check reached.

        The plans behind the head carry their **own** reason, and the
        one that matters to them is the workspace: a plan whose
        directory is held is waiting for a directory, not for a slot,
        and saying otherwise would send the operator looking in the
        wrong place.  The head's verdict is the policy's and says
        nothing about the plans behind it — so each of them is asked
        about the directory it works in.
        """
        rows = self._store.list_queue(self._conn)
        verdict = admission_verdict(state)
        queued = {
            str(entry.get("plan_id") or ""): entry
            for entry in state.get("queued", [])
        }
        view: List[Dict[str, Any]] = []
        for index, row in enumerate(rows):
            entry = dict(row)
            if index == 0:
                entry["reason"] = verdict
            else:
                entry["reason"] = self._reason_behind(
                    queued.get(str(row.get("plan_id") or ""))
                )
            view.append(entry)
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
    ) -> List[Dict[str, Any]]:
        """The plans that hold compute, as the policy reads them.

        The **facts** say who is working; the slots say when each of
        them took its slot.  A slot row for a plan that is not working
        is a reservation the ledger has not reconciled away yet, and it
        is not a plan that is running: the panel and the eviction rule
        both talk about the plans that are working, and a plan that
        has been admitted but has not started yet is neither.

        The facts also say when the plan really started, and that
        stamp is passed on as it is.  The slot's moment is the
        scheduler's clock — it starts again from zero on every
        restart — so the plan's own start time travels with the row
        for whoever wants to say how long the plan has been at it.
        """
        acquired: Dict[str, float] = {}
        for slot in slots:
            plan_id = str(slot.get("plan_id") or "")
            if not plan_id:
                continue
            moment = self._epoch(slot.get("acquired_at"))
            acquired[plan_id] = moment if moment is not None else now

        running: List[Dict[str, Any]] = []
        for row in self._facts.active_plans():
            plan_id = str(row.get("plan_id") or "")
            if not plan_id:
                continue
            moment = acquired.get(plan_id, now)
            running.append(
                {
                    "plan_id": plan_id,
                    "acquired_at": moment,
                    "elapsed": max(0.0, now - moment),
                    "workspace": row.get("workspace"),
                    "started_at": row.get("started_at"),
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

    def _capacity_override(self) -> Optional[int]:
        """The capacity override in force, if there is one.

        The override is a row in the state database, and it is read
        **again on every call**: it is a knob the operator turns while
        the scheduler runs, so a value kept in the configuration
        snapshot would only take effect after a restart — which is
        exactly what the override is not for.

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
        try:
            raw = self._store.get_setting(self._conn, CAPACITY_OVERRIDE_KEY)
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

    def _capacity_and_source(self) -> Tuple[int, str]:
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
        """
        override = self._capacity_override()
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