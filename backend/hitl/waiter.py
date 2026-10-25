"""Waiting for a human: the poll loop, and the heartbeat that keeps it alive.

Why this module exists
----------------------
A question is asked by one actor and answered by another — a human,
minutes later, possibly on another machine.  The thread that asked it
therefore has to *wait*, and a wait is a state the backend has to
support rather than a fault it has to diagnose.  Two things make that
wait safe.

**The wait must not look like a hang.**  The watchdog judges a thread
that has produced nothing for 900 seconds to be dead
(``VERIFICATION_WATCHDOG_STALENESS_SECONDS``).  A thread blocked on a
human produces nothing *by definition* — it is waiting, not working —
so a wait with no heartbeat is a wait the watchdog kills, and the
question dies with it.  The waiter therefore beats every 60 seconds:
well inside the 900-second window, and often enough that a single
missed beat is not a missed window.  The beat writes the wait's own
liveness stamp — ``heartbeat_at`` on the row — because that stamp is
what makes the wait visible; the caller's callback is *additional*
bookkeeping, the thing the owner of the thread does with the beat
(marking a sub-agent handle as progressing, for instance).

**The wait must end when the question is settled.**  The answer comes
from somewhere else, so the waiter cannot be told; it has to look.
Every ``poll_interval`` seconds it re-reads the row and reads the
status.  The row is the same row every other actor reads, so what the
waiter returns is what everybody else sees.

Who resolves a question is not the waiter's business
----------------------------------------------------
The waiter never writes a decision.  A missed deadline is resolved by
:func:`hitl.service.expire_due` — the service owns the transition and
the audit line that records it — and the waiter's part is to notice
that it happened.  So the waiter calls the service when the deadline
arrives and then reads the result, rather than writing the row
itself: a waiter that resolved the question behind the service's back
would leave an audit trail nobody authorised.

The deadline is therefore the *only* thing the waiter acts on.  It
does not answer a question, and it does not give up on one: a question
that is still ``pending`` is a question somebody may still answer,
however long that takes.

Time is injected, never slept
------------------------------
``now_fn`` is the waiter's clock, so "ten minutes of waiting" is
asserted by moving a clock rather than by waiting ten minutes.  Two
clocks are accepted: an aware :class:`datetime` — what the store and
the service exchange — and a number of seconds, what a monotonic clock
speaks.  A numeric clock is *projected* onto the wall clock (see
:class:`_Clock`), which is what lets the deadline comparison work on
one.

What is not here
----------------
* **No HTTP, no routes, no card.**  The question is asked and answered
  through :mod:`hitl.service` and :mod:`hitl.routes.hitl`; this
  module is the thread-side half.
* **No timeout of its own.**  The decision carries its own deadline
  (``timeout_sec``, frozen into the row when the question was asked);
  the waiter does not invent a second one.
"""

from __future__ import annotations

import inspect
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, NamedTuple, Optional

from hitl.models import Decision, DecisionStatus
from hitl.service import expire_due, heartbeat_decision
from hitl.store import HitlStore

__all__ = [
    "DecisionWaiter",
    "WaiterReleased",
    "wait_for_decision",
]

logger = logging.getLogger(__name__)

#: The statuses that end a wait.  ``answered`` and ``defaulted`` are
#: the two a caller waits for: somebody answered, or nobody did and
#: the stored default was used.  ``paused`` and ``unroutable`` end the
#: wait as well — the question is no longer one anybody is waiting on,
#: and holding a thread for it would be holding a thread for nothing.
SETTLED_STATUSES = (
    DecisionStatus.ANSWERED.value,
    DecisionStatus.DEFAULTED.value,
    DecisionStatus.PAUSED.value,
    DecisionStatus.UNROUTABLE.value,
)

#: How long :func:`wait_for_decision` waits for its own worker to
#: notice the release before it gives up on it.  The release wakes the
#: worker immediately, so this is a backstop against a worker stuck in
#: a database call — not a normal wait.
_JOIN_TIMEOUT = 5.0

#: The most heartbeats one poll may emit in a single catch-up.  A
#: burst larger than this means the clock jumped (an injected clock
#: moved by hand, or a process that was frozen for hours), and the
#: honest thing to do is to say "alive" once rather than to replay
#: every missed minute.  With the default clock the burst is never
#: larger than one: a poll is late by at most a poll interval.
_MAX_CATCH_UP_HEARTBEATS = 1000


class WaiterReleased(RuntimeError):
    """The wait was given up before the question was settled.

    A give-up is not an answer.  The caller has to be able to tell the
    two apart — a decision that was released is a decision nobody
    resolved, and reporting it as one would be a lie.
    """


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _default_now() -> datetime:
    """The current time, as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime.

    A naive datetime is read as UTC: every timestamp this codebase
    writes is UTC, so a naive one can only mean that.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _positive_seconds(value: Any, name: str) -> float:
    """Return ``value`` as a positive number of seconds, else raise.

    Zero is refused as firmly as a negative number: a poll interval of
    zero is a busy loop, and a heartbeat interval of zero is a
    heartbeat on every poll.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be a number of seconds; got {value!r}"
        ) from None
    if seconds <= 0:
        raise ValueError(f"{name} must be positive; got {seconds!r}")
    return seconds


class _Reading(NamedTuple):
    """One reading of the waiter's clock, in both of the views it needs.

    ``instant`` is the wall-clock moment the reading stands for (what
    a deadline is compared against); ``elapsed`` is how many seconds
    have passed since the wait started (what the heartbeat schedule is
    counted in).
    """

    instant: datetime
    elapsed: float


class _Clock:
    """The waiter's reading of time.

    Two clocks are accepted, because the backend speaks both:

    * an aware :class:`datetime` — what the store and the service
      exchange (``expire_due(now=...)`` takes one);
    * a number of seconds — what a monotonic clock speaks.

    A numeric clock is *projected* onto the wall clock: the reading
    taken when the wait started is anchored to the wall-clock instant
    the wait started at, so a later reading of ``+1200`` means "1200
    seconds after the wait began".  The projection is what lets the
    deadline comparison work on a numeric clock at all — without it
    there would be no way to say whether an absolute deadline passed.
    """

    def __init__(self, now_fn: Callable[[], Any]) -> None:
        self._now_fn = now_fn
        first = now_fn()
        if isinstance(first, datetime):
            self._numeric = False
            self._origin: datetime = _as_utc(first)
            self._origin_value = 0.0
        else:
            self._numeric = True
            self._origin_value = float(first)
            self._origin = datetime.now(timezone.utc)

    def read(self) -> _Reading:
        """Read the clock once, and report both views of that reading.

        One read, two views: a clock that moves between the two calls
        would make the heartbeat schedule and the deadline comparison
        disagree about what "now" is.
        """
        now = self._now_fn()
        if self._numeric:
            elapsed = float(now) - self._origin_value
            return _Reading(
                instant=self._origin + timedelta(seconds=elapsed),
                elapsed=elapsed,
            )
        moment = _as_utc(now)
        return _Reading(
            instant=moment,
            elapsed=(moment - self._origin).total_seconds(),
        )


# ---------------------------------------------------------------------------
# The heartbeat callback
# ---------------------------------------------------------------------------


def _takes_an_argument(callback: Callable[..., Any]) -> bool:
    """True when ``callback`` accepts one positional argument.

    The heartbeat callback may be written either way — ``mark()`` for
    a closure that already knows the decision, or ``mark(decision)``
    for one that wants to be told what it is beating for — so the
    waiter asks the callback which it is rather than making the
    caller guess.  A callback whose signature cannot be read (a C
    function, a builtin) is called bare.
    """
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind in (
            parameter.POSITIONAL_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.VAR_POSITIONAL,
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# The waiter
# ---------------------------------------------------------------------------


class DecisionWaiter:
    """One decision, one worker thread, one heartbeat schedule.

    The worker polls the store and beats; the caller waits for the
    result.  The split exists because the two have different jobs: the
    caller wants a decision, and the worker wants a schedule it can
    keep to while nobody is looking.

    Parameters
    ----------
    decision_id:
        The decision to wait for.
    store:
        The store the row is read from.  The waiter writes one thing
        through it — the heartbeat stamp — and resolves nothing: the
        *service* resolves a question, the waiter only watches one.
    on_heartbeat:
        Called every ``heartbeat_interval`` seconds of waiting, after
        the wait's own stamp has been written.  It may take no
        argument, or the decision being waited for.  An exception it
        raises is logged and the wait continues: the callback is the
        caller's code, and its failure is not a reason to abandon a
        question somebody is about to answer.
    poll_interval:
        How often the row is re-read, in real seconds.  This is the
        granularity of "the answer has arrived" — the answer itself
        takes effect the moment it is stored, so this is how long the
        waiter may take to *notice* it.
    heartbeat_interval:
        How much waiting passes between two heartbeats, measured on
        ``now_fn``'s clock.  With the default clock this is real
        time; with an injected one it is the clock's time, which is
        what lets a test assert a minute-long schedule without
        spending a minute.
    now_fn:
        The clock.  An aware :class:`datetime` or a number of seconds;
        see :class:`_Clock`.

    Attributes
    ----------
    decision_id:
        The decision being waited for.
    finished:
        True once the wait ended — resolved, released, or failed.
    decision:
        The resolved decision, once there is one.
    thread:
        The worker thread.  It is joined by :meth:`join`; a waiter
        that outlives its wait is a bug, not a detail.
    """

    def __init__(
        self,
        decision_id: str,
        *,
        store: HitlStore,
        on_heartbeat: Optional[Callable[..., Any]] = None,
        poll_interval: float = 2.0,
        heartbeat_interval: float = 60.0,
        now_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError(
                f"decision_id must be a non-empty string; got {decision_id!r}"
            )
        self.decision_id = decision_id
        self._store = store
        self._on_heartbeat = on_heartbeat
        self._poll_interval = _positive_seconds(poll_interval, "poll_interval")
        self._heartbeat_interval = _positive_seconds(
            heartbeat_interval, "heartbeat_interval"
        )
        self._now_fn = now_fn if now_fn is not None else _default_now
        self._heartbeat_takes_decision = (
            on_heartbeat is not None and _takes_an_argument(on_heartbeat)
        )
        # The release event ends the wait; the finished event says the
        # wait is over.  Two events, because "stop waiting" and "there
        # is a result" are two different facts.
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._decision: Optional[Decision] = None
        self._error: Optional[BaseException] = None
        self._started = False
        self._clock: Optional[_Clock] = None
        self._thread = threading.Thread(
            target=self._run, name=f"hitl-wait-{decision_id}", daemon=True
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "DecisionWaiter":
        """Start the wait.  Idempotent.

        The clock is read *here*, in the caller's thread, and not in
        the worker: the heartbeat schedule is anchored at the start of
        the wait, and a schedule anchored at whatever moment the worker
        thread happened to be scheduled would make the number of
        heartbeats a fact about the scheduler rather than a fact about
        the clock.
        """
        if not self._started:
            self._started = True
            self._clock = _Clock(self._now_fn)
            self._thread.start()
        return self

    def release(self) -> None:
        """Give the wait up.  Idempotent, and safe from a teardown.

        The wait ends at the next wake-up rather than at the next
        poll: the release event interrupts the sleep, so a wait with a
        30-second poll interval is still given up at once.
        """
        self._stop.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the worker to finish."""
        self._thread.join(timeout)

    # -- the result --------------------------------------------------------

    def result(self, timeout: Optional[float] = None) -> Decision:
        """The resolved decision; blocks until there is one.

        Raises
        ------
        TimeoutError
            When ``timeout`` passes without a decision.
        WaiterReleased
            When the wait was given up before the question was settled.
        LookupError
            When the decision does not exist.
        """
        if not self._started:
            raise RuntimeError("the waiter was never started")
        if not self._finished.wait(timeout):
            raise TimeoutError(
                f"decision {self.decision_id!r} was not resolved within "
                f"{timeout} seconds"
            )
        if self._error is not None:
            raise self._error
        if self._decision is None:
            raise WaiterReleased(
                f"the wait for decision {self.decision_id!r} was released "
                "before the question was settled"
            )
        return self._decision

    @property
    def finished(self) -> bool:
        """True once the wait ended — resolved, released, or failed."""
        return self._finished.is_set()

    @property
    def decision(self) -> Optional[Decision]:
        """The resolved decision, once there is one."""
        return self._decision

    @property
    def thread(self) -> threading.Thread:
        """The worker thread."""
        return self._thread

    # -- the worker --------------------------------------------------------

    def _run(self) -> None:
        """The worker's body: wait, and report how it ended."""
        try:
            decision = self._wait()
        except BaseException as exc:  # noqa: BLE001 - the worker reports, never raises
            self._error = exc
        else:
            self._decision = decision
        finally:
            self._finished.set()

    def _wait(self) -> Decision:
        """Poll until the question is settled.

        The loop has three jobs and no more: read the row, notice the
        deadline, and keep the heartbeat schedule.  It resolves nothing
        — the service resolves questions, this one watches them being
        resolved — and the only thing it writes is the heartbeat stamp
        that says the wait is still going.
        """
        clock = self._clock
        if clock is None:
            raise RuntimeError("the waiter was never started")
        # The first heartbeat is due one interval from now, so the
        # schedule is "one beat per interval", counted from the start
        # of the wait.
        next_heartbeat = self._heartbeat_interval
        expiry_attempted = False
        while True:
            if self._stop.is_set():
                raise WaiterReleased(
                    f"the wait for decision {self.decision_id!r} was released "
                    "before the question was settled"
                )
            decision = self._store.get(self.decision_id)
            if decision is None:
                raise LookupError(
                    f"no decision {self.decision_id!r} to wait for"
                )
            if decision.status in SETTLED_STATUSES:
                return decision

            reading = clock.read()
            if not expiry_attempted and self._is_due(decision, reading.instant):
                # The deadline has arrived.  The *service* resolves the
                # question — it owns the transition and the audit line
                # — so the waiter asks it to and then reads the row
                # again.  One attempt only: a decision the sweep
                # cannot settle (a strict one, or one without a
                # default) is not going to be settled by asking again.
                expiry_attempted = True
                expire_due(now=reading.instant, store=self._store)
                continue

            next_heartbeat = self._beat(decision, reading.elapsed, next_heartbeat)

            if self._stop.wait(self._poll_interval):
                raise WaiterReleased(
                    f"the wait for decision {self.decision_id!r} was released "
                    "before the question was settled"
                )

    def _beat(
        self, decision: Decision, elapsed: float, next_heartbeat: float
    ) -> float:
        """Emit the heartbeats the elapsed time owes, and report the next one due.

        One beat per *interval*, not per poll: a poll that was late by
        a minute still owes exactly one beat for that minute, and one
        that was late by an hour owes an hour's worth.  Counting the
        intervals rather than the polls is what keeps the number of
        beats a fact about the clock instead of a fact about how busy
        the loop happened to be.
        """
        beats = 0
        while elapsed >= next_heartbeat:
            self._emit_heartbeat(decision)
            next_heartbeat += self._heartbeat_interval
            beats += 1
            if beats >= _MAX_CATCH_UP_HEARTBEATS:
                logger.warning(
                    "hitl_wait_heartbeat_flood: decision %s is more than %s "
                    "heartbeats behind; the clock jumped",
                    self.decision_id,
                    _MAX_CATCH_UP_HEARTBEATS,
                )
                break
        return next_heartbeat

    def _emit_heartbeat(self, decision: Decision) -> None:
        """Say "alive" once, in both of the places that mean it.

        Two things happen on a beat, and they are two different facts.

        The **stamp** is written to the row: ``heartbeat_at`` says when
        the wait last said something.  The waiter writes it itself,
        because a heartbeat the caller has to remember to send is a
        heartbeat that is missing the first time the caller is busy —
        and the one time it is missing is the one that matters.  The
        stamp is a compare-and-set over the *open* states, so a beat
        that lands after the answer writes nothing.  The stamp is
        written whether or not the caller asked for a callback: it is
        the wait's own liveness, not the caller's bookkeeping.

        The **callback** is the caller's own bookkeeping — marking a
        sub-agent handle as progressing, for instance.  It may fail for
        reasons that have nothing to do with the question, and the wait
        is still a wait: the thread is still alive, so the right answer
        is to log the failure and keep going, not to unwind a wait that
        a human is about to answer.
        """
        self._stamp_heartbeat(decision)
        if self._on_heartbeat is None:
            return
        try:
            if self._heartbeat_takes_decision:
                self._on_heartbeat(decision)
            else:
                self._on_heartbeat()
        except Exception as exc:  # noqa: BLE001 - a failing beat must not end the wait
            logger.warning(
                "hitl_wait_heartbeat_failed: the heartbeat callback for "
                "decision %s raised %s; the wait continues",
                self.decision_id,
                exc,
                exc_info=True,
            )

    def _stamp_heartbeat(self, decision: Decision) -> None:
        """Write the wait's own liveness stamp onto the row.

        Best-effort by design.  The stamp is what keeps the wait
        visible to the watchdog, but a store that cannot be written is
        a store that is broken, and a broken store is not a reason to
        abandon a question somebody is about to answer: the wait goes
        on, and the failure is logged so an operator can see it.
        """
        try:
            heartbeat_decision(decision.decision_id, store=self._store)
        except Exception as exc:  # noqa: BLE001 - a failing stamp must not end the wait
            logger.warning(
                "hitl_wait_heartbeat_stamp_failed: the heartbeat for "
                "decision %s could not be stamped (%s); the wait continues",
                self.decision_id,
                exc,
                exc_info=True,
            )

    @staticmethod
    def _is_due(decision: Decision, instant: datetime) -> bool:
        """True when ``decision``'s deadline has arrived by ``instant``.

        The deadline is the *end* of the waiting window, so the window
        is half-open: before it the question is still open, at it the
        answer is late.  That is the convention the freshness windows
        elsewhere in this backend use, and the one
        :func:`hitl.service.expire_due` uses.

        A deadline that cannot be read is a deadline that cannot be
        compared, and an undecidable deadline is not an expired one:
        the decision waits rather than resolving itself on a guess.
        """
        raw = decision.deadline_at
        if not raw:
            return False
        text = str(raw).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            deadline = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            logger.warning(
                "hitl_wait_unreadable_deadline: decision %s carries a "
                "deadline that cannot be read (%r); it waits",
                decision.decision_id,
                raw,
            )
            return False
        return _as_utc(deadline) <= instant


# ---------------------------------------------------------------------------
# The one-call form
# ---------------------------------------------------------------------------


def wait_for_decision(
    decision_id: str,
    *,
    store: HitlStore,
    on_heartbeat: Optional[Callable[..., Any]] = None,
    poll_interval: float = 2.0,
    heartbeat_interval: float = 60.0,
    now_fn: Optional[Callable[[], Any]] = None,
) -> Decision:
    """Wait for a decision to be settled, and return it.

    The convenience form of :class:`DecisionWaiter`: it starts the
    wait, blocks until the question is settled, and returns the
    decision — ``answered`` when somebody answered, ``defaulted`` when
    the service resolved it at the deadline.

    The wait is released and its worker joined before this returns, so
    a finished wait leaves no thread behind: a waiter that outlived
    its wait would outlive its store.

    Parameters
    ----------
    decision_id:
        The decision to wait for.
    store:
        The store the row is read from.
    on_heartbeat:
        Called every ``heartbeat_interval`` seconds of waiting.  See
        :class:`DecisionWaiter`.
    poll_interval:
        How often the row is re-read, in real seconds.
    heartbeat_interval:
        How much waiting passes between two heartbeats.
    now_fn:
        The clock.  See :class:`DecisionWaiter`.

    Returns
    -------
    Decision
        The settled decision.
    """
    waiter = DecisionWaiter(
        decision_id,
        store=store,
        on_heartbeat=on_heartbeat,
        poll_interval=poll_interval,
        heartbeat_interval=heartbeat_interval,
        now_fn=now_fn,
    )
    waiter.start()
    try:
        return waiter.result()
    finally:
        # The release wakes the worker out of its sleep, so the join
        # is immediate; the timeout is a backstop against a worker
        # stuck in a database call, not a normal wait.
        waiter.release()
        waiter.join(timeout=_JOIN_TIMEOUT)
        if waiter.thread.is_alive():
            logger.warning(
                "hitl_wait_thread_stuck: the worker for decision %s did "
                "not stop within %s seconds",
                decision_id,
                _JOIN_TIMEOUT,
            )
