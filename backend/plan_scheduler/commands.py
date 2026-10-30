"""backend.plan_scheduler.commands — the four operator actions, as facts.

Background
----------
The operator has four things they can do to a plan from the scheduler
panel: **promote** it, **pause** it, **resume** it and **start** it.
The four mean four different things, and only one of them may ever
interrupt somebody else's work.

This module is the translation layer.  It takes the four actions and
turns them into **queue facts** — the queue row, the promotions — plus
**lifecycle calls** — stop, requeue.  It is deliberately not a second
policy: the rules it applies are the ones the rest of the scheduler
already speaks, and each of them is asked of the component that owns
it.

===================== ========================================= ==========
action                what it does                               may it
                                                                   interrupt
===================== ========================================= ==========
``promote``           moves the plan ahead in the queue           no
``pause``             parks the plan: it stops and gives its      no
                      slot back
``resume``            puts a stopped plan back at the head of     no
                      the queue
``start``             makes the plan the next one to run; frees   **yes**
                      a slot if the capacity is full
===================== ========================================= ==========

Who answers what
----------------
Every question this module asks has exactly one owner, and it asks the
owner rather than working the answer out a second time:

* **"is this plan running?"** — the facts reader.  Who holds compute
  is a plan fact, and the reader derives it from the plan tables.  The
  executor never keeps a copy of it, and never guesses it from the
  phase: a plan in ``executing`` whose process is gone is not running.
* **"how many plans hold compute, and who may be interrupted?"** — the
  capacity ledger.  The candidate set is the ledger's: a plan that is
  working *and* has been running for at least ``M`` seconds.
* **"which of them do I interrupt?"** — the policy.  Among the
  candidates it is the **youngest** one: the plan that took its slot
  most recently, and therefore the one least likely to be wrapping up.
  The same rule the tick's convergence uses, so the two triggers that
  may interrupt somebody share one candidate set and one choice.
* **"may this plan move to another phase?"** — the lifecycle.  It owns
  the edge table; the executor asks it and takes the answer it gets.
  A second copy of the edges would be a second table that drifts.
* **"what is in the queue?"** — the store, which is the only writer of
  the queue table.
* **"how many plans may run, and how long is a young run protected?"**
  — the configuration.  Both numbers come out of **one** snapshot, and
  the snapshot is read at the moment the decision is made: it is the
  same reader the tick uses, so the two triggers that may interrupt
  somebody reach the same answer at the same moment instead of two
  answers from two snapshots.

The phase is never written here
-------------------------------
The phase is the state machine's to move, and the executor only ever
*asks* the lifecycle to move one.  A write of its own would be a second
writer of the plan's workflow state, and it would be a write that skips
the edge table — the very table that says which moves are legal.

The outcomes
------------
The command answers with one of five words, and the set is closed:

``applied``
    The action was carried out.  The queue or the plan's state says so
    now.
``started``
    The start was carried out: the plan is at the head of the queue and
    the scheduler will run it — at once when a slot was free, and at
    the next tick once the interrupted plan's slot is really free.  A
    slot is released when the ledger *observes* that the plan is no
    longer working, never before, so the plan that asked to start waits
    for the slot to be really empty rather than for the stop request to
    be answered.
``blocked_by_capacity``
    The start could not be carried out: the capacity is full and there
    is nobody the rules allow to interrupt.  The plan stays at the head
    of the queue, waiting.
``conflict``
    The action does not apply to the plan as it is — it is running, or
    it is in a phase the action has no edge from.  Nothing happened.
``not_found``
    The scheduler does not know the plan.

A refusal is not one of the five
--------------------------------
The lifecycle answers 400, 404 and 409 in its own words, and those
words are the **caller's** answer — not a failure of the scheduler.  So
a refusal is not one of the five outcomes: it travels as
:class:`CommandRejected`, which carries the status, the detail and the
exception it came from, and the route turns it back into the status it
came with.  A fault that is not a refusal stays a fault: it is the
scheduler's own, and it is the scheduler's own word for it.

The transaction boundary
------------------------
The lifecycle calls open their own connection and write to the same
database, so the executor **never holds a write transaction across one**
— the facade's own write would block against it.  The queue writes and
the lifecycle calls are therefore separate transactions, in that order:
the facts are changed first, and the ledger is reconciled afterwards so
that "who holds compute" says what the plan facts say.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from .capacity import CapacityLedger
from .config import DEFAULT_CAPACITY, DEFAULT_MIN_RUNTIME_SECONDS
from .facts import QUEUED_PHASE
from .policy import select_victim
from .store import SqliteSchedulerStore

log = logging.getLogger(__name__)

__all__ = [
    "APPLIED",
    "BLOCKED_BY_CAPACITY",
    "CONFLICT",
    "NOT_FOUND",
    "OUTCOMES",
    "STARTED",
    "CommandExecutor",
    "CommandRejected",
    "CommandResult",
]


#: The action was carried out.
APPLIED = "applied"

#: The start was carried out: the plan is the next one to run.
STARTED = "started"

#: The start could not be carried out: the capacity is full and there is
#: nobody the rules allow to interrupt.
BLOCKED_BY_CAPACITY = "blocked_by_capacity"

#: The action does not apply to the plan as it is.
CONFLICT = "conflict"

#: The scheduler does not know the plan.
NOT_FOUND = "not_found"

#: Every outcome a command may report.  The set is closed on purpose: a
#: new word is a new situation, and a new situation needs a decision
#: nobody has made yet.
OUTCOMES = frozenset(
    {APPLIED, STARTED, BLOCKED_BY_CAPACITY, CONFLICT, NOT_FOUND}
)


class CommandRejected(Exception):
    """A refusal the lifecycle gave, on its way to the caller.

    The lifecycle answers 400, 404 and 409 in its own words, and those
    words are the **caller's** answer — not a failure of the scheduler.
    So a refusal is not swallowed, and it is not rewritten into an
    ambiguous word of the command's own vocabulary: it travels as this
    small translated form, and the route turns it back into the status
    it came with.

    It is deliberately **not** one of the five outcomes: an outcome says
    what an *action* did, and a refusal says that the action was not
    carried out at all.  The status carries the difference between "you
    sent something wrong", "there is no such plan" and "not now" —
    a difference a 500 would take away.

    Attributes:
        status_code: the status the refusal is answered with.
        detail: the body the refusal carries, unchanged.
        cause: the exception the refusal came from — the lifecycle's own.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        """Hold the refusal's three parts.

        Args:
            status_code: the status the refusal is answered with.
            detail: the body the refusal carries, unchanged.
            cause: the exception the refusal came from.
        """
        self.status_code = int(status_code)
        self.detail = detail
        self.cause = cause
        super().__init__(
            "the lifecycle refused the command: {0} {1!r}".format(
                self.status_code, detail
            )
        )


# ---------------------------------------------------------------------------
# The configuration the decisions run with
# ---------------------------------------------------------------------------


def _config_provider_of(provider: Any) -> Optional[Callable[[], Any]]:
    """Return the callable that hands out the current configuration.

    Two shapes are accepted: a callable that returns a snapshot, and a
    **loader** — anything with a ``current()`` method, which is what
    :class:`~plan_scheduler.config.SchedulerConfigLoader` is.  A loader
    is asked for its snapshot rather than called, because the loader is
    the thing that knows whether the file changed since last time.

    ``None`` means "there is no configuration to read": the numbers the
    executor was built with stand in for it.
    """
    if provider is None:
        return None
    current = getattr(provider, "current", None)
    if callable(current):
        return current
    if callable(provider):
        return provider
    raise ValueError(
        "the command executor's configuration provider must be a "
        "callable or a loader with a current() method"
    )


def _setting_of(snapshot: Any, name: str) -> Any:
    """Read one setting out of a configuration snapshot.

    A snapshot is read by attribute — that is how
    :class:`~plan_scheduler.config.SchedulerConfig` carries its
    settings — and by key when it happens to be a mapping.  A setting
    the snapshot does not carry reads as ``None``: "not set", which is
    not the same as "set to nothing".
    """
    if snapshot is None:
        return None
    value = getattr(snapshot, name, None)
    if value is None:
        getter = getattr(snapshot, "get", None)
        if callable(getter):
            value = getter(name)
    return value


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """What one command did.

    Attributes:
        outcome: one of :data:`OUTCOMES` — the closed vocabulary above.
        command_id: the id of the row in ``scheduler_command`` that
            records this command.  The id space is never reused, so the
            command the operator issued at 14:03 stays unambiguous.
        detail: what the command did, in a short and stable wording —
            which plan was interrupted, which edge the plan took.  It
            explains the outcome; it does not replace it.
        plan_id: the plan the command was about.
        action: the action that was carried out — ``promote``, ``pause``,
            ``resume`` or ``start``.
    """

    outcome: str
    command_id: Optional[int] = None
    detail: Optional[str] = None
    plan_id: Optional[str] = None
    action: Optional[str] = None

    def __post_init__(self) -> None:
        """Refuse an outcome that is not one of the five words."""
        if self.outcome not in OUTCOMES:
            raise ValueError(
                "unknown command outcome: {0!r} (the vocabulary is {1!r})".format(
                    self.outcome, sorted(OUTCOMES),
                )
            )


# ---------------------------------------------------------------------------
# Reading the collaborators
# ---------------------------------------------------------------------------


def _connection_of(source: Any) -> Optional[sqlite3.Connection]:
    """Return the connection the collaborator reads on.

    The ledger, the facts reader and the executor have to see the same
    database: a ledger that reconciled another file would be
    reconciling a world of its own, and a command that wrote its queue
    row into another file would be commanding a queue nobody reads.
    """
    for name in ("conn", "_conn"):
        conn = getattr(source, name, None)
        if isinstance(conn, sqlite3.Connection):
            return conn
    return None


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class CommandExecutor:
    """The four operator actions, translated into facts.

    Attributes:
        store: the scheduler's own store — the only writer of the queue,
            the slots and the command log.
        ledger: the capacity ledger — the authority on who holds
            compute and on who may be interrupted.
        lifecycle: the lifecycle facade — the only thing that stops a
            plan or moves it back to the waiting phase.
        facts: the reader that derives the plan facts.  It is the
            ledger's own reader unless another one is given: one truth,
            not two that can drift apart.
    """

    def __init__(
        self,
        store: SqliteSchedulerStore,
        ledger: CapacityLedger,
        lifecycle: Any,
        *,
        facts: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        capacity: int = DEFAULT_CAPACITY,
        min_runtime_seconds: float = DEFAULT_MIN_RUNTIME_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        config_provider: Any = None,
    ) -> None:
        """Bind the executor to its collaborators.

        Args:
            store: the scheduler's own store.
            ledger: the capacity ledger.
            lifecycle: the lifecycle facade.
            facts: the reader that derives the plan facts.
            conn: the connection to write on.
            capacity: how many plans may run at the same time.  It is
                the **fallback**: with a configuration provider it is
                read from the configuration instead, and this number
                only stands in when there is no provider.
            min_runtime_seconds: how long a young run is protected for
                — ``M``.  The fallback, in the same sense.
            clock: the source of "now", in epoch seconds.
            config_provider: the reader of the current configuration —
                a callable that returns a snapshot, or a loader with a
                ``current()`` method.  It is the **same** reader the
                tick uses, so the two triggers that may interrupt
                somebody read the same snapshot at the same moment and
                pick their victim from the same candidate set.
        """
        self._store = store
        self._ledger = ledger
        self._lifecycle = lifecycle
        self._facts = facts if facts is not None else getattr(
            ledger, "_facts", None
        )
        if self._facts is None:
            raise ValueError(
                "the command executor needs the facts reader: it is the "
                "only thing that knows which plans exist and which of "
                "them are working"
            )
        self._conn = conn if conn is not None else _connection_of(ledger)
        if self._conn is None:
            raise ValueError(
                "the command executor needs a connection: it writes the "
                "queue and the command log"
            )
        self._config_provider = _config_provider_of(config_provider)
        self._capacity = max(0, int(capacity))
        self._min_runtime = float(min_runtime_seconds)
        self._clock = clock if clock is not None else time.time

    # ------------------------------------------------------------------
    # The four actions
    # ------------------------------------------------------------------

    def promote(
        self, plan_id: str, *, command_id: Optional[int] = None
    ) -> CommandResult:
        """Move a plan ahead in the queue — and nothing else.

        A promotion is a **change of order**: it evicts nobody, it takes
        no slot, and it does not move the plan's phase.  That is what
        makes it safe to offer on a plan that is working — but a plan
        that *is* working cannot be promoted either, because it is not
        waiting in the queue and there is no order to move it in.  That
        is a conflict.

        Args:
            plan_id: the plan to promote.
            command_id: the command row to finish; a new one is written
                when it is omitted.

        Returns:
            The :class:`CommandResult`.
        """
        return self._command("promote", plan_id, command_id, self._do_promote)

    def pause(
        self, plan_id: str, *, command_id: Optional[int] = None
    ) -> CommandResult:
        """Park a plan: stop it, and let it give its slot back.

        A running plan is stopped through the lifecycle's existing stop
        path, and a plan that is waiting is taken out of the queue and
        parked — in both cases the plan ends up in ``stopped`` and it
        stops holding compute.  The phase is the lifecycle's to move; the
        executor only asks.

        Args:
            plan_id: the plan to pause.
            command_id: the command row to finish; a new one is written
                when it is omitted.

        Returns:
            The :class:`CommandResult`.
        """
        return self._command("pause", plan_id, command_id, self._do_pause)

    def resume(
        self, plan_id: str, *, command_id: Optional[int] = None
    ) -> CommandResult:
        """Put a stopped plan back at the head of the queue.

        A resume is a **wait**, not a take: the plan comes back first,
        and it comes back waiting for a slot.  It interrupts nobody and
        it takes no slot, so the only things it changes are the plan's
        phase — through the lifecycle — and its place in the queue.

        Args:
            plan_id: the plan to resume.
            command_id: the command row to finish; a new one is written
                when it is omitted.

        Returns:
            The :class:`CommandResult`.
        """
        return self._command("resume", plan_id, command_id, self._do_resume)

    def start(
        self, plan_id: str, *, command_id: Optional[int] = None
    ) -> CommandResult:
        """Make a plan the next one to run.

        A start is the **only** action that may interrupt somebody, and
        it does so only when the capacity is full and there is a victim
        the rules allow: a plan that has been running for at least ``M``
        seconds, and among those the youngest one.  With no such victim
        the start does not happen — the plan stays at the head of the
        queue and the command answers ``blocked_by_capacity``.

        A plan that is parked is put back into the queue first, through
        the lifecycle's own edge.  The lifecycle accepts ``stopped``;
        the other parked phases the panel offers have no edge the
        lifecycle knows yet, and a plan in one of them is a conflict
        rather than a start that pretends to have happened.

        Args:
            plan_id: the plan to start.
            command_id: the command row to finish; a new one is written
                when it is omitted.

        Returns:
            The :class:`CommandResult`.
        """
        return self._command("start", plan_id, command_id, self._do_start)

    # ------------------------------------------------------------------
    # The actions themselves
    # ------------------------------------------------------------------

    def _do_promote(self, plan_id: str) -> Tuple[str, Optional[str]]:
        """Carry out a promotion; return the outcome and the detail."""
        phase = self._phase_of(plan_id)
        if phase is None:
            return NOT_FOUND, "no_such_plan"
        if self._is_running(plan_id):
            # A plan that is working is not waiting in the queue, so
            # there is no order to move it in.
            return CONFLICT, "running"
        if phase != QUEUED_PHASE:
            return CONFLICT, "phase:{0}".format(phase)

        with self._store.transaction(self._conn):
            self._ensure_queued(plan_id)
            order = self._store.promote(self._conn, plan_id)
        if order is None:
            return CONFLICT, "not_queued"
        return APPLIED, "promote_order:{0}".format(order)

    def _do_pause(self, plan_id: str) -> Tuple[str, Optional[str]]:
        """Carry out a pause; return the outcome and the detail."""
        if self._phase_of(plan_id) is None:
            return NOT_FOUND, "no_such_plan"

        try:
            result = self._lifecycle.pause_plan(plan_id)
        except Exception as exc:  # noqa: BLE001 — the type decides
            if self._is_not_found(exc):
                return NOT_FOUND, "no_such_plan"
            rejection = self._as_rejection(exc)
            if rejection is not None:
                # The lifecycle refused: the refusal is the caller's
                # answer, and it travels on as it came.
                raise rejection from exc
            raise
        if not getattr(result, "ok", True):
            return CONFLICT, getattr(result, "detail", None) or "conflict"

        # The plan gave its slot back.  The ledger is the authority on
        # who holds compute, and it reads the plan facts — which the stop
        # has just changed.  Reconciling here is what makes the ledger
        # say what the facts say, instead of the next tick having to.
        self._ledger.reconcile()
        return APPLIED, getattr(result, "detail", None) or "paused"

    def _do_resume(self, plan_id: str) -> Tuple[str, Optional[str]]:
        """Carry out a resume; return the outcome and the detail."""
        if self._phase_of(plan_id) is None:
            return NOT_FOUND, "no_such_plan"
        if self._is_running(plan_id):
            # A plan that is working has no "put it back to wait" to do.
            return CONFLICT, "running"

        try:
            result = self._lifecycle.requeue_plan(plan_id)
        except Exception as exc:  # noqa: BLE001 — the type decides
            if self._is_not_found(exc):
                return NOT_FOUND, "no_such_plan"
            rejection = self._as_rejection(exc)
            if rejection is not None:
                # The lifecycle refused: the refusal is the caller's
                # answer, and it travels on as it came.
                raise rejection from exc
            raise
        if not getattr(result, "ok", True):
            return CONFLICT, getattr(result, "detail", None) or "conflict"

        # Back to the head of the queue: the plan is waiting again, and
        # it is the next one to run.
        with self._store.transaction(self._conn):
            self._ensure_queued(plan_id)
            self._store.promote(self._conn, plan_id)
        return APPLIED, "resumed"

    def _do_start(self, plan_id: str) -> Tuple[str, Optional[str]]:
        """Carry out a start; return the outcome and the detail."""
        phase = self._phase_of(plan_id)
        if phase is None:
            return NOT_FOUND, "no_such_plan"
        if self._is_running(plan_id):
            return CONFLICT, "running"

        if phase != QUEUED_PHASE:
            # The plan is parked.  The only way back into the queue is
            # the lifecycle's own edge, and the lifecycle owns the edge
            # table — so ask it and take the answer it gives.
            try:
                result = self._lifecycle.requeue_plan(plan_id)
            except Exception as exc:  # noqa: BLE001 — the type decides
                if self._is_not_found(exc):
                    return NOT_FOUND, "no_such_plan"
                rejection = self._as_rejection(exc)
                if rejection is not None:
                    # The lifecycle refused: the refusal is the caller's
                    # answer, and it travels on as it came.
                    raise rejection from exc
                raise
            if not getattr(result, "ok", True):
                return CONFLICT, getattr(result, "detail", None) or "conflict"

        # The plan is waiting, and it is the next one to run.
        with self._store.transaction(self._conn):
            self._ensure_queued(plan_id)
            self._store.promote(self._conn, plan_id)

        # The capacity.  The start is the only action that may interrupt
        # somebody, and it does so only when the capacity is full and
        # there is a victim the rules allow.  The capacity and ``M``
        # are read from the configuration **as it is now**: the tick
        # reads the same snapshot, and the two triggers that may
        # interrupt somebody have to pick their victim out of the same
        # candidate set at the same moment.
        capacity, min_runtime = self._current_settings()
        if self._ledger.used() < capacity:
            return STARTED, "slot_free"

        victim = self._select_victim(min_runtime)
        if victim is None:
            # Everybody is inside the protected window.  The plan waits
            # at the head of the queue rather than interrupting work that
            # has not had its turn yet.
            return BLOCKED_BY_CAPACITY, "no_eligible_victim"

        self._evict(victim)
        return STARTED, "evicted:{0}".format(victim)

    # ------------------------------------------------------------------
    # The eviction
    # ------------------------------------------------------------------

    def _select_victim(self, min_runtime: float) -> Optional[str]:
        """Return the plan that may be interrupted, or ``None``.

        The candidate set is the ledger's — a plan that is working and
        has been running for at least ``M`` seconds — and the choice
        among them is the policy's: the **youngest** one.  The tick's
        convergence asks the same two questions, so the two triggers
        that may interrupt somebody share one candidate set and one
        choice.

        The candidates are handed to the policy as the ledger gives
        them.  The executor does not rebuild the running set out of the
        slot rows: those rows carry no creation time, and the creation
        time is what tells the youngest plan from the others — so a
        rebuilt set would pick the victim by when a slot was stamped
        rather than by when the plan was made.  The slots are read once,
        by the ledger, and the whole answer comes off that one read.

        Args:
            min_runtime: ``M`` — the runtime a plan is protected for.
                It is handed in rather than read here, so that the
                threshold and the capacity the caller decided with come
                out of the **same** snapshot.
        """
        now = self._now()
        candidates = self._ledger.evictable(now, min_runtime)
        return select_victim({"running": candidates}, min_runtime)

    def _evict(self, victim: str) -> None:
        """Interrupt a running plan so that the plan that asked may run.

        The victim is stopped through the lifecycle's own stop path with
        ``requeue=True``: it is stopped **and** put back into the queue,
        so it keeps a place in the line instead of losing it.  Its queue
        row is kept — the number it holds is its place — and its
        promotion is spent, so an interrupted plan does not keep jumping
        the queue after it was interrupted.

        The victim's slot is **not** freed here.  A slot is released
        when the ledger observes that the plan is no longer working, and
        the ledger is the only thing that says who is working.
        """
        try:
            self._lifecycle.stop_plan(victim, requeue=True)
        except Exception as exc:  # noqa: BLE001 — the type decides
            rejection = self._as_rejection(exc)
            if rejection is not None:
                # The lifecycle refused to stop the victim: that is the
                # lifecycle's answer, and it travels on as it came.
                raise rejection from exc
            raise
        with self._store.transaction(self._conn):
            self._ensure_queued(victim)
            self._store.consume_promotion(self._conn, victim)
        self._ledger.reconcile()

    # ------------------------------------------------------------------
    # The settings the decision runs with
    # ------------------------------------------------------------------

    def _current_settings(self) -> Tuple[int, float]:
        """Return the capacity and ``M`` this decision runs with.

        Both numbers come out of **one** snapshot.  A reader that saw a
        new capacity next to an old minimum runtime would be deciding
        with two truths at once, and the tick — which reads the same
        snapshot — would be deciding with a third.  The two triggers
        that may interrupt somebody have to reach the same answer at
        the same moment, so they read the same snapshot.

        The numbers the executor was built with stand in when there is
        no provider: they are the settings as they were when it was
        built, and a caller with no configuration file has nothing
        better.  They also stand in for a setting the snapshot does not
        carry — a snapshot that says nothing about ``M`` leaves the
        previous ``M`` in place rather than guessing one.

        Returns:
            The capacity and the minimum runtime, in that order.
        """
        provider = self._config_provider
        if provider is None:
            return self._capacity, self._min_runtime

        try:
            snapshot = provider()
        except Exception as exc:  # noqa: BLE001 — the old settings stay
            log.warning(
                "the scheduler's configuration could not be read (%s); "
                "the previous settings are kept",
                exc,
            )
            return self._capacity, self._min_runtime

        capacity = _setting_of(snapshot, "capacity")
        if capacity is not None:
            try:
                self._capacity = max(0, int(capacity))
            except (TypeError, ValueError):
                log.warning(
                    "the scheduler's capacity (%r) is not a number; the "
                    "previous capacity is kept",
                    capacity,
                )
        min_runtime = _setting_of(snapshot, "min_runtime_seconds")
        if min_runtime is not None:
            try:
                self._min_runtime = float(min_runtime)
            except (TypeError, ValueError):
                log.warning(
                    "the scheduler's minimum runtime (%r) is not a number; "
                    "the previous minimum runtime is kept",
                    min_runtime,
                )
        return self._capacity, self._min_runtime

    # ------------------------------------------------------------------
    # The facts the executor reads
    # ------------------------------------------------------------------

    def _phase_of(self, plan_id: str) -> Optional[str]:
        """Return the phase the plan is in, or ``None`` when it has none.

        The phase is read from the plan facts and never written: it
        belongs to the state machine, and the only thing that moves one
        is the lifecycle.  ``None`` means the scheduler does not know
        this plan.
        """
        spec = self._facts.start_spec(plan_id)
        phase = spec.get("phase") if isinstance(spec, dict) else None
        return None if phase is None else str(phase)

    def _is_running(self, plan_id: str) -> bool:
        """Return whether the plan is holding compute right now."""
        for row in self._facts.active_plans():
            if str(row.get("plan_id")) == plan_id:
                return True
        return False

    def _is_queued(self, plan_id: str) -> bool:
        """Return whether the plan has a row in the scheduler queue."""
        for row in self._store.list_queue(self._conn):
            if str(row["plan_id"]) == plan_id:
                return True
        return False

    def _ensure_queued(self, plan_id: str) -> None:
        """Give the plan a place in the queue if it has none.

        A plan that is already queued keeps its number **and its
        priority**: the queue row is the plan's place in the line, and
        re-queuing it must neither push it back nor demote it.
        """
        if self._is_queued(plan_id):
            return
        self._store.enqueue(self._conn, plan_id)

    # ------------------------------------------------------------------
    # The command log
    # ------------------------------------------------------------------

    def _command(
        self,
        action: str,
        plan_id: str,
        command_id: Optional[int],
        body: Callable[[str], Tuple[str, Optional[str]]],
    ) -> CommandResult:
        """Carry out an action and write the command down.

        The command is written with the outcome it got: the executor
        runs the command synchronously, so there is no window in which
        the command was accepted but not yet carried out.  A caller
        that recorded the request itself — the mailbox, which has to
        answer with a command id before the command has run — passes
        that id in, and the row is finished instead of written twice.
        """
        outcome, detail = body(plan_id)
        if command_id is None:
            command_id = self._store.record_command(
                self._conn, plan_id, action, outcome, detail,
            )
        else:
            self._store.finish_command(self._conn, command_id, outcome, detail)
        return CommandResult(
            outcome=outcome,
            command_id=command_id,
            detail=detail,
            plan_id=plan_id,
            action=action,
        )

    # ------------------------------------------------------------------
    # The small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_not_found(exc: BaseException) -> bool:
        """Return whether the lifecycle said the plan is not there.

        The exception is the state machine's own, and it is imported
        here rather than at the top of the module: the command layer
        only needs the one class, and importing the whole repository
        layer to catch it would tie the scheduler package to the state
        machine for the sake of a single ``except``.
        """
        from state_machine.repositories.routing_repository import (
            PlanNotFoundError,
        )

        return isinstance(exc, PlanNotFoundError)

    @staticmethod
    def _as_rejection(exc: BaseException) -> Optional[CommandRejected]:
        """Return the refusal an exception carries, or ``None``.

        Two refusals are recognised, and they are the two the lifecycle
        speaks with a status of its own:

        * the ``HTTPException`` its handlers raise — the 400 and the 409
          they refused with, in their own words; and
        * the state machine's ``ConflictError`` — a CAS conflict, which
          is a 409.

        The third one — ``PlanNotFoundError`` — is **not** here: the
        executor answers it with its own word, ``not_found``, which the
        route already turns into a 404.

        Anything else is not a refusal: it is a fault, and a fault is
        not an answer.  The fault stays a fault.

        The two classes are imported here rather than at the top of the
        module, for the same reason :meth:`_is_not_found` imports its
        one: the command layer needs the classes to *recognise* a
        refusal, and nothing more.

        Args:
            exc: the exception the lifecycle raised.

        Returns:
            The :class:`CommandRejected` the exception carries, or
            ``None`` when it carries no refusal.
        """
        from fastapi import HTTPException
        from state_machine.repositories.routing_repository import ConflictError

        if isinstance(exc, HTTPException):
            return CommandRejected(exc.status_code, exc.detail, exc)
        if isinstance(exc, ConflictError):
            reason = getattr(exc, "reason", None) or str(exc)
            return CommandRejected(409, reason, exc)
        return None

    def _now(self) -> float:
        """The current moment, in epoch seconds."""
        value = self._clock() if callable(self._clock) else self._clock
        try:
            return float(value)
        except (TypeError, ValueError):
            return time.time()
