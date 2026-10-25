"""The HITL decision service — the actions the rest of the backend calls.

What lives here
---------------
:mod:`hitl.models` says what a decision *is*; :mod:`hitl.store` says how
one is written down.  This module says what a decision *means* to the
caller: which plan configuration a new decision inherits, which states
may still be answered, and what an answer looks like from the outside —
a status and an HTTP-shaped code rather than a boolean.

Three of the six actions below are what the HTTP layer calls.  The
other three are called by the runtime: the heartbeat monitor sweeps for
missed deadlines, the executor asks which tasks it may schedule, and
the waiter stamps the question it is blocked on.

Six actions
-----------

``create_decision()``
    Asks a human.  Reads the plan's own ``hitl_config.json`` (through
    :func:`hooks.config.load_for_plan` — the *one* reader of that file)
    and freezes the two fields that decide what happens when the
    deadline passes: ``mode`` and ``timeout_sec``.
``list_decisions()``
    Reports what a plan has been asked, optionally narrowed to one state.
``answer_decision()``
    Records a human's answer and reports what happened as a
    ``(status, code)`` pair: ``("answered", 200)`` when the answer was
    taken, the *current* status with ``409`` when the decision was
    already settled, ``("not_found", 404)`` when there is no such
    decision.
``expire_due()``
    Resolves the ``auto`` decisions whose deadline has passed: the
    stored ``default_option`` becomes the answer and the decision ends
    ``defaulted``.  Idempotent — the same sweep may run a hundred
    times and the question is resolved once.
``blocked_task_ids()``
    Computes which of a plan's tasks must not be scheduled while a
    decision is open: the task the question is about, plus everything
    downstream of it.
``heartbeat_decision()``
    Stamps the liveness of a question somebody is waiting for.  It
    resolves nothing: the stamp says that a thread is still blocked on
    the row, and it is written only while the question is open.

Why a service layer at all
---------------------------
The HTTP layer has to be thin: parse a request, call one of these
functions, turn the result into a response.  Every rule that is *not*
about HTTP — which states may still be answered, what a refused creation
leaves behind, which fields are frozen — lives here, so that it can be
tested without a socket, a router or a request header.  A rule that
lives in a route handler is a rule that only exists when somebody sends
that request.

The plan's configuration is the only source of ``mode``
--------------------------------------------------------
A decision's ``mode`` and ``timeout_sec`` decide what happens when the
deadline passes, so they are read from the plan's own
``hitl_config.json`` — the file that is the plan's, and the only one
there is.  There is no environment variable, no global configuration
and no sibling plan's file in the path: the plan says what happens when
its deadline passes, and nothing else gets a vote.

The two values are **frozen into the row** at creation time.  A decision
that is already open keeps the meaning it was created with, however the
plan's configuration is rewritten afterwards; the new configuration is
what the *next* decision inherits.  The hook list is deliberately not
frozen: it is the plan's event outlet, read when an event is
dispatched, and the decision row carries no hooks at all.

Two refusals, both of them deliberate
-------------------------------------
A request that could not produce a decision that works is refused
rather than stored:

``auto_requires_default``
    ``auto`` means "when the deadline passes, use the default answer".
    A decision with no default answer has no way to end, so it is
    refused at creation time rather than half an hour later, when the
    expiry would discover it.
``invalid_timeout`` / ``no_options``
    A decision whose deadline is already past, or which offers no answer
    to choose from, cannot be acted on.

A refusal leaves no row, so it is written to the audit mirror — a
question that was refused must not disappear silently.

Which states may still be answered
----------------------------------
``pending`` and ``paused`` are open.  A paused decision is one whose
``strict`` deadline passed: the plan stopped waiting, but the question
was never resolved, so it stays answerable.  ``answered`` /
``defaulted`` / ``unroutable`` are settled — a second answer to them is
a conflict (``409``), never an overwrite.

The store's compare-and-set moves *pending* rows only, because that is
the race it was written for: two humans answering at the same second.
Answering a paused row needs the same guarantee over a different set of
states, so the service runs that one statement itself — on the store's
own connection, so that the row, the audit line and the store's
transaction discipline describe one state and not two.

Expiry and the scheduling gate
------------------------------
Two rules outlive the request that asked the question, and both of them
are about *time*:

**A missed deadline resolves an ``auto`` decision, once.**
:meth:`expire_due` moves the ``pending`` ``auto`` decisions whose
deadline has passed to ``defaulted``, with the stored
``default_option`` as their answer.  It is idempotent by construction:
the store's compare-and-set only moves ``pending`` rows, so the second
sweep finds nothing left to move and appends no second audit line.  A
``strict`` decision is not touched — there is no substitute for a human
— and neither is an ``unroutable`` one: nobody was asked, so the
backend must not answer on their behalf.

**An open question holds the schedule shut.**
:meth:`blocked_task_ids` computes — never stores — the tasks a plan
must not schedule while a decision is open: the task the question is
about and everything downstream of it.  No task status changes, so
answering the question opens the gate again by itself, and a plan with
no open decision behaves exactly as it did before.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

from config_paths import resolve_plans_dir, resolve_state_db_path
from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.store import HitlStore
from hooks.config import load_for_plan
from hooks.dispatcher import deliverable_hooks, dispatch

__all__ = [
    "DecisionRejected",
    "answer_decision",
    "blocked_task_ids",
    "create_decision",
    "expire_due",
    "heartbeat_decision",
    "list_decisions",
]

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: The event the outlet carries when a question is asked.  The outlet
#: does not know about HITL — it carries whatever event it is handed —
#: so the name is the service's, and the payload is the service's too.
EVENT_DECISION_CREATED = "hitl_decision_created"

#: The note recorded when the plan's outlet names nobody who can be
#: asked: the hook list is empty, or every entry in it is one the
#: outlet cannot deliver to.
_NOTE_NO_DESTINATION = "no deliverable destination: nobody was asked"

#: The note recorded when the outlet was asked and reached nobody: the
#: destinations that could be delivered to all refused the question.
_NOTE_NO_DELIVERY = "no destination accepted the question: nobody was asked"

#: The states a decision may be in while it is still waiting for an
#: answer.  ``paused`` is open: the plan stopped waiting, but the
#: question was never resolved.
OPEN_STATUSES = (
    DecisionStatus.PENDING.value,
    DecisionStatus.PAUSED.value,
)

#: The states that hold the scheduling gate shut.  ``pending`` is the
#: ordinary wait; ``paused`` is a question whose deadline passed
#: without an answer — the plan stopped waiting, but the question was
#: never resolved, so the tasks it guards must stay out of the
#: schedule.  ``answered`` / ``defaulted`` / ``unroutable`` are
#: settled: the work the question was about may go on.
BLOCKING_STATUSES = (
    DecisionStatus.PENDING.value,
    DecisionStatus.PAUSED.value,
)

#: How many times an answer re-reads the row before giving up.  Each
#: round is one attempt at the compare-and-set; more than one round is
#: only ever needed when somebody else moved the row between the read
#: and the write.
_MAX_ANSWER_ATTEMPTS = 3

#: The compare-and-set for the one transition the store does not own:
#: a paused decision becoming answered.  Same shape as the store's own
#: CAS — one statement, the status in the predicate, the audit counter
#: bumped by the database — with ``paused`` in place of ``pending``.
#:
#: ``answer_note`` carries the note the answer came with into the
#: row, exactly as the store's own answer transition does: the remark
#: that explains an answer is part of the answer, and the jsonl
#: mirror is only a copy of the row.  ``COALESCE`` leaves a note the
#: question was created with in place when the answer carries none.
_ANSWER_PAUSED_SQL = """
UPDATE plan_decisions
   SET status      = :status,
       answer      = :answer,
       answered_by = :answered_by,
       answered_at = :answered_at,
       answer_note = COALESCE(:answer_note, answer_note),
       updated_at  = :updated_at,
       audit_seq   = audit_seq + 1
 WHERE decision_id = :decision_id
   AND status      = :from_status
"""


class DecisionRejected(ValueError):
    """Raised when a request cannot become a decision.

    A :class:`ValueError` on purpose: the *request* is wrong, not the
    system, and the HTTP layer turns this into a 4xx without having to
    know about a HITL-specific exception type.

    Attributes
    ----------
    reason:
        A short, stable slug naming the rule that refused the request
        (``auto_requires_default`` / ``invalid_timeout`` /
        ``no_options``).  The slug is what the audit mirror records, so
        it has to be greppable rather than prose.
    decision_id:
        The identity the question would have had.  It is reported so a
        caller can tie a refusal to the question it asked; no row exists
        under it.
    """

    def __init__(self, reason: str, message: str, *, decision_id: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.decision_id = decision_id


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _store_of(store: Optional[HitlStore]) -> Iterator[HitlStore]:
    """Yield ``store``, or a store opened for the call and closed after.

    A caller that already has a store — a test, a long-lived worker —
    passes it in; everybody else gets one on the state database the rest
    of the backend uses, which is resolved through
    :func:`config_paths.resolve_state_db_path` so the operator's
    override is honoured.
    """
    if store is not None:
        yield store
        return
    opened = HitlStore(resolve_state_db_path())
    try:
        yield opened
    finally:
        opened.close()


def _now_iso() -> str:
    """The current UTC time, in the spelling the store writes."""
    return datetime.now(timezone.utc).isoformat()


def _connection_of(store: HitlStore) -> sqlite3.Connection:
    """The store's own connection.

    ``HitlStore`` keeps its connection private on purpose: it owns
    opening the file, the WAL contract and the migration.  This is the
    one place that reaches for it, and it does so for a specific
    reason: the paused-decision compare-and-set has to run on the same
    connection the store reads and writes through, so that the row, the
    audit line and the store's transaction discipline describe one
    state and not two.
    """
    return store._conn  # noqa: SLF001 - see the docstring


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_decision(
    request: DecisionRequest,
    plan_dir: PathLike,
    *,
    store: Optional[HitlStore] = None,
    now: Optional[datetime] = None,
) -> Decision:
    """Ask a human, on behalf of ``request``, and store the question.

    The plan's configuration is read from ``plan_dir`` and the two fields
    it owns — ``mode`` and ``timeout_sec`` — are frozen into the stored
    decision.  Rewriting ``hitl_config.json`` afterwards changes what
    the *next* decision inherits, never what this one means.

    The question is written down **before** anybody is told about it,
    and the plan's outlet is told afterwards: a decision that exists
    but was never announced is a fact the audit mirror can show, while a
    decision that was announced but never stored is a question a human
    is answering into the void.

    Parameters
    ----------
    request:
        The question as asked.  Its ``mode`` and ``timeout_sec`` are the
        caller's defaults; the plan's configuration is what actually
        decides, because the plan is what the deadline belongs to.
    plan_dir:
        The plan's directory — the one that holds its
        ``hitl_config.json``.
    store:
        The store to write through.  One is opened for the call when
        not given.
    now:
        The clock reading used for the timestamps and the deadline.
        Injected so a test can assert a deadline without waiting for
        it.

    Returns
    -------
    Decision
        The stored question.  ``pending`` when somebody was asked (or
        is about to be — the delivery runs on its own thread), and
        ``unroutable`` when the plan's outlet names nobody who can be
        asked: nobody was asked, so there is no question to wait for.

    Raises
    ------
    DecisionRejected
        When the request cannot become a decision that works — an
        ``auto`` decision with no default answer, a non-positive
        timeout, an empty option set.  The refusal is written to the
        audit mirror before the exception is raised.
    """
    config = load_for_plan(plan_dir)
    mode = config["mode"]
    timeout_sec = config["timeout_sec"]

    with _store_of(store) as opened:
        reason = _refusal_reason(request, mode, timeout_sec)
        if reason is not None:
            # The refused question never becomes a row, so it is
            # identified by an id of its own — minted here rather than
            # by the model, because building the model would police the
            # very request that is being refused.
            decision_id = uuid.uuid4().hex
            _record_refusal(opened, request.plan_id, decision_id, reason)
            raise DecisionRejected(
                reason,
                _refusal_message(reason, request),
                decision_id=decision_id,
            )

        decision = _build(request, mode, timeout_sec, now=now)
        opened.insert(decision)
        return _ask_the_human(
            opened, decision, plan_dir, config["hooks"], store_for_later=store
        )


def _ask_the_human(
    store: HitlStore,
    decision: Decision,
    plan_dir: PathLike,
    hooks: List[Dict[str, Any]],
    *,
    store_for_later: Optional[HitlStore],
) -> Decision:
    """Tell the plan's outlet that a question was asked, and settle the
    question when there is nobody to ask.

    Two shapes, and the difference is whether anybody can be asked at
    all:

    **Somebody can be asked.**  The delivery runs on its own thread —
    the architecture is explicit that a hung destination must not be
    able to hold the request path hostage — and the question stays
    ``pending``.  When the delivery is over and it reached nobody, the
    question is settled as ``unroutable``: it was asked of a channel
    that did not take it, so no human is going to answer it.

    **Nobody can be asked.**  The plan's outlet names no destination
    that can be delivered to, so the delivery is free — no destination
    is attempted — and the question is settled as ``unroutable`` right
    away.  A question that nobody was asked must not sit in the table
    as ``pending``: it would hold the schedule shut (the task it is
    about and everything downstream of it) over a question no human
    ever received.
    """
    payload = _event_payload(decision)
    if deliverable_hooks(hooks):
        _dispatch_in_background(
            decision, plan_dir, payload, store=store_for_later
        )
        return decision

    report = dispatch(EVENT_DECISION_CREATED, payload, plan_dir)
    if report.delivered == 0:
        store.mark_unroutable(decision.decision_id, note=_NOTE_NO_DESTINATION)
        settled = store.get(decision.decision_id)
        if settled is not None:
            return settled
    return decision


def _event_payload(decision: Decision) -> Dict[str, Any]:
    """The body the outlet carries for a question that was asked.

    The fields are the ones the architecture names: the decision's
    identity, the task it is about, the answers on the table and the
    deadline a human is racing.  A decision's *kind* travels in its
    ``source`` field — the plan's four disposition kinds — so that is
    what ``kind`` carries here.
    """
    return {
        "plan_id": decision.plan_id,
        "decision_id": decision.decision_id,
        "task_id": decision.task_id,
        "kind": decision.source,
        "options": list(decision.options),
        "default_option": decision.default_option,
        "deadline_at": decision.deadline_at,
    }


def _dispatch_in_background(
    decision: Decision,
    plan_dir: PathLike,
    payload: Dict[str, Any],
    *,
    store: Optional[HitlStore],
) -> None:
    """Deliver the question on a worker thread, and settle it if it
    reaches nobody.

    The thread is the point: the caller is answered when the question
    is written down, not when the slowest destination answers.  The
    worker owns the delivery *and* the settlement, because the
    settlement is a fact about the delivery — a question is only
    ``unroutable`` once the outlet has said that it reached nobody.

    The store the worker settles through is the caller's when there is
    one (the caller owns its lifetime) and an own store otherwise (the
    call's temporary store is closed when the call returns).  Either
    way it is the same database: the state database the rest of the
    backend uses.
    """
    def _run() -> None:
        owned: Optional[HitlStore] = None
        try:
            target = store
            if target is None:
                owned = HitlStore(resolve_state_db_path())
                target = owned
            report = dispatch(EVENT_DECISION_CREATED, payload, plan_dir)
            if report.delivered == 0:
                target.mark_unroutable(
                    decision.decision_id, note=_NOTE_NO_DELIVERY
                )
        except Exception as exc:  # noqa: BLE001 - a delivery must not raise
            logger.warning(
                "hitl_dispatch_failed: decision %s: %s", decision.decision_id, exc
            )
        finally:
            if owned is not None:
                owned.close()

    threading.Thread(
        target=_run, name="hitl-dispatch", daemon=True
    ).start()


def _build(
    request: DecisionRequest,
    mode: str,
    timeout_sec: Optional[int],
    *,
    now: Optional[datetime],
) -> Decision:
    """The stored form of ``request`` under the plan's configuration."""
    effective = replace(request, mode=mode, timeout_sec=timeout_sec)
    return Decision.from_request(effective, now=now)


def _refusal_reason(
    request: DecisionRequest,
    mode: str,
    timeout_sec: Optional[int],
) -> Optional[str]:
    """Why this request must not become a decision, or ``None``.

    The three rules are checked in the order a reader would meet them:
    the question has to be answerable at all (options), the deadline
    has to be in the future (timeout), and the mode has to have
    somewhere to fall back to (default answer).
    """
    if not request.options:
        return "no_options"
    if request.timeout_sec is not None and request.timeout_sec <= 0:
        return "invalid_timeout"
    if timeout_sec is not None and timeout_sec <= 0:
        return "invalid_timeout"
    if mode == DecisionMode.AUTO.value and request.default_option is None:
        return "auto_requires_default"
    return None


def _refusal_message(reason: str, request: DecisionRequest) -> str:
    """The sentence a caller reads when its request is refused."""
    if reason == "auto_requires_default":
        return (
            "an auto decision needs a default_option to fall back to when "
            "the deadline passes; none was given"
        )
    if reason == "invalid_timeout":
        return (
            f"timeout_sec must be a positive number of seconds; got "
            f"{request.timeout_sec!r}"
        )
    return "a decision needs at least one option to choose from; none was given"


def _record_refusal(
    store: HitlStore,
    plan_id: str,
    decision_id: str,
    reason: str,
) -> None:
    """Write a refused creation to the audit mirror.

    A refusal leaves no row, so the mirror is the only place the
    refused question exists.  The line says what was refused and why;
    the question text is not mirrored (the mirror's field whitelist
    deliberately drops it), so the record identifies the refusal by plan,
    time and reason.
    """
    try:
        store.append_audit(
            {
                "event": "rejected",
                "decision_id": decision_id,
                "plan_id": plan_id,
                "status": "rejected",
                "answer": None,
                "answered_by": None,
                "note": reason,
                "audit_seq": 0,
            }
        )
    except OSError as exc:
        # The database is the source of truth; a mirror that cannot be
        # written must not turn a refusal into something else.
        logger.warning(
            "hitl audit mirror write failed for refused decision %s: %s",
            decision_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_decisions(
    plan_id: str,
    status: Optional[Union[str, DecisionStatus]] = None,
    *,
    store: Optional[HitlStore] = None,
) -> List[Decision]:
    """Return a plan's decisions, oldest first, optionally by status.

    ``status`` narrows to one state — the ``pending`` list is what a
    plan's UI polls for.  An unknown plan is not an error but an empty
    list: the question this answers is "what is outstanding", not "does
    the plan exist".

    Raises
    ------
    ValueError
        When ``status`` is outside the closed vocabulary.  The refusal
        is deliberate: a filter nobody can interpret would silently
        return the wrong set.
    """
    narrowed = _canonical_status(status)
    with _store_of(store) as opened:
        return opened.list_by_plan(plan_id, status=narrowed)


def _canonical_status(
    status: Optional[Union[str, DecisionStatus]],
) -> Optional[str]:
    """The canonical status string, or ``None`` for "every status"."""
    if status is None:
        return None
    try:
        return DecisionStatus(status).value
    except ValueError:
        raise ValueError(
            f"status must be one of {[s.value for s in DecisionStatus]!r}; "
            f"got {status!r}"
        ) from None


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def answer_decision(
    decision_id: str,
    option: Any,
    answered_by: Optional[str] = None,
    note: str = "",
    *,
    plan_id: Optional[str] = None,
    store: Optional[HitlStore] = None,
) -> Tuple[str, int]:
    """Record a human's answer and report what happened.

    Returns a ``(status, code)`` pair:

    ``("answered", 200)``
        The answer was taken.  The decision is now ``answered``.
    ``(<current status>, 409)``
        The decision was already settled — ``answered``,
        ``defaulted`` or ``unroutable``.  Nothing was written: a second
        answer is a conflict, never an overwrite.
    ``("not_found", 404)``
        There is no such decision, or it belongs to another plan.

    Parameters
    ----------
    decision_id:
        The decision being answered.
    option:
        The answer.  It is stored as given; the service does not
        second-guess which of the offered options it is.
    answered_by:
        Who answered.
    note:
        A free-form note.  It is stored in the row's ``answer_note``
        column — the database is the fact source — and mirrored into
        the audit line.
    plan_id:
        When given, the decision must belong to this plan — a decision
        of another plan is reported as not found, so a stale card cannot
        answer somebody else's question.
    store:
        The store to write through.  One is opened for the call when
        not given.
    """
    with _store_of(store) as opened:
        for _ in range(_MAX_ANSWER_ATTEMPTS):
            decision = opened.get(decision_id)
            if decision is None:
                return ("not_found", 404)
            if plan_id is not None and decision.plan_id != plan_id:
                return ("not_found", 404)

            if decision.status == DecisionStatus.PENDING.value:
                if opened.answer_if_pending(
                    decision_id, option, answered_by, note
                ):
                    return (DecisionStatus.ANSWERED.value, 200)
                # Somebody else moved it first; look again.
                continue
            if decision.status == DecisionStatus.PAUSED.value:
                if _answer_paused(
                    opened, decision, option, answered_by, note
                ):
                    return (DecisionStatus.ANSWERED.value, 200)
                continue
            return (decision.status, 409)

        # The row kept moving under us; report where it ended up.
        decision = opened.get(decision_id)
        if decision is None:
            return ("not_found", 404)
        return (decision.status, 409)


def _answer_paused(
    store: HitlStore,
    decision: Decision,
    option: Any,
    answered_by: Optional[str],
    note: str,
) -> bool:
    """Move a paused decision to ``answered``.

    The store's compare-and-set moves *pending* rows only — that is the
    race it was written for.  A paused decision is open in a different
    sense (its ``strict`` deadline passed, but nobody answered), so it
    needs the same guarantee over a different set of states.  The
    statement below is that guarantee: one atomic update whose predicate
    is the status, so two operators answering the same paused decision
    produce one stored answer.

    Returns ``True`` when this call is the one that resolved the
    decision and ``False`` when it did not.
    """
    now = _now_iso()
    cursor = _connection_of(store).execute(
        _ANSWER_PAUSED_SQL,
        {
            "status": DecisionStatus.ANSWERED.value,
            "answer": _encode_answer(option),
            "answered_by": answered_by,
            "answered_at": now,
            "answer_note": note or None,
            "updated_at": now,
            "decision_id": decision.decision_id,
            "from_status": DecisionStatus.PAUSED.value,
        },
    )
    if cursor.rowcount != 1:
        return False

    row = _connection_of(store).execute(
        "SELECT audit_seq FROM plan_decisions WHERE decision_id = ?",
        (decision.decision_id,),
    ).fetchone()
    audit_seq = row["audit_seq"] if row is not None else 0
    try:
        store.append_audit(
            {
                "event": "answered",
                "decision_id": decision.decision_id,
                "plan_id": decision.plan_id,
                "status": DecisionStatus.ANSWERED.value,
                "answer": option,
                "answered_by": answered_by,
                "note": note or None,
                "audit_seq": audit_seq,
            }
        )
    except OSError as exc:
        # The row is committed and authoritative; a mirror that cannot
        # be written is logged rather than raised at the caller.
        logger.warning(
            "hitl audit mirror write failed for decision %s: %s",
            decision.decision_id,
            exc,
        )
    return True


def _encode_answer(answer: Any) -> Optional[str]:
    """JSON-encode a stored answer; ``None`` stays ``NULL``.

    The same spelling :mod:`hitl.store` writes, so an answer read back
    through the store decodes to the value that was given.
    """
    if answer is None:
        return None
    return json.dumps(answer, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def expire_due(
    now: Optional[datetime] = None,
    *,
    store: Optional[HitlStore] = None,
) -> int:
    """Resolve the ``auto`` decisions whose deadline has passed.

    A missed deadline is a decision in its own right: an ``auto``
    question nobody answered resolves to the ``default_option`` that
    was stored with it, and the decision ends ``defaulted``.  A
    ``strict`` question is not touched — there is no substitute for a
    human — and neither is an ``unroutable`` one: nobody was asked, so
    the backend must not answer on their behalf.

    The function is **idempotent**.  The store's compare-and-set only
    moves ``pending`` rows, so the second sweep finds nothing left to
    move, reports ``0`` and appends no second audit line.  An answer
    that arrives at the same moment is decided by the same
    compare-and-set: whichever of the two gets there first wins, and
    the other writes nothing.

    Parameters
    ----------
    now:
        The clock reading the deadlines are compared against.  Injected
        so a test can assert an expiry without waiting for one.
    store:
        The store to write through.  One is opened for the call when
        not given.

    Returns
    -------
    int
        How many decisions this call moved to ``defaulted``.
    """
    moment = _as_utc(now if now is not None else datetime.now(timezone.utc))
    if store is None and not resolve_state_db_path().exists():
        # The sweep never creates the database it sweeps.  A server whose
        # ``state.db`` is absent has no decisions to expire, so there is
        # nothing to look for -- and a heartbeat tick that materialises an
        # empty database as a side effect of looking is a write nobody
        # asked for.  It also hides the state from every reader that asks
        # the filesystem: ``/api/plan/{id}/state-from-db`` answers 503
        # while the file is missing and 404 the moment a tick has created
        # it, so "the state database is gone" becomes unobservable.
        return 0
    with _store_of(store) as opened:
        migrated = 0
        for decision in _expired_auto_decisions(opened, moment):
            if decision.default_option is None:
                # An ``auto`` decision always has a default — creation
                # refuses one without it — so this row was written by
                # something else.  There is nothing to fall back to,
                # and guessing one would answer a question nobody was
                # asked.  Leave it pending and say so.
                logger.warning(
                    "hitl_expire_skipped: decision %s is auto but carries no "
                    "default_option; leaving it pending",
                    decision.decision_id,
                )
                continue
            if opened.mark_defaulted(decision.decision_id):
                migrated += 1
        return migrated


def heartbeat_decision(
    decision_id: str,
    *,
    store: Optional[HitlStore] = None,
    at: Optional[str] = None,
) -> bool:
    """Stamp the liveness of a question somebody is waiting for.

    A heartbeat is a fact about the *wait*, not about the question: it
    says that a thread is still blocked on this row.  It resolves
    nothing — the status, the answer and the answerer are left alone —
    and it is written only while the question is **open**
    (``pending`` or ``paused``), because a settled question has nobody
    waiting for it any more.

    The function is idempotent in the sense that matters here: the
    stamp is a compare-and-set over the open states, so a beat that
    lands after the answer writes nothing and says so by returning
    ``False``.

    Parameters
    ----------
    decision_id:
        The decision being waited for.
    store:
        The store to write through.  One is opened for the call when
        not given.
    at:
        The instant to stamp, as an ISO-8601 string.  Defaults to now.

    Returns
    -------
    bool
        ``True`` when the stamp was written, ``False`` when the
        decision is unknown or already settled.
    """
    with _store_of(store) as opened:
        return opened.mark_heartbeat(decision_id, at)


def _expired_auto_decisions(
    store: HitlStore,
    moment: datetime,
) -> List[Decision]:
    """The ``pending`` ``auto`` decisions whose deadline has passed.

    The rows are read through the store's own connection rather than
    through a new query on a new one, so the sweep sees the same
    database the answers are written to.
    """
    rows = _connection_of(store).execute(
        "SELECT * FROM plan_decisions "
        "WHERE status = ? AND deadline_at IS NOT NULL",
        (DecisionStatus.PENDING.value,),
    ).fetchall()
    expired: List[Decision] = []
    for row in rows:
        if row["mode"] != DecisionMode.AUTO.value:
            continue
        try:
            decision = Decision.from_row(row)
        except ValueError:
            # A row this module cannot read — a value outside a closed
            # vocabulary, say — must not stop the sweep: the decisions
            # that *are* readable still have to be resolved.
            logger.warning(
                "hitl_expire_unreadable: skipping an unreadable plan_decisions "
                "row (decision_id=%r)",
                row["decision_id"],
            )
            continue
        if _is_past_deadline(decision, moment):
            expired.append(decision)
    return expired


def _is_past_deadline(decision: Decision, moment: datetime) -> bool:
    """True when ``decision``'s deadline has arrived by ``moment``.

    The deadline is the *end* of the waiting window, so the window is
    half-open: before it the question is still open, at it the answer
    is late.  That is the convention the freshness windows elsewhere
    in this backend use — a thing is stale once its age reaches the
    window, not one tick before.

    A decision with no deadline never expires: it waits for a human
    for as long as it takes.
    """
    deadline = _parse_timestamp(decision.deadline_at)
    if deadline is None:
        return False
    return deadline <= moment


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """Parse a stored ISO-8601 timestamp into an aware UTC datetime.

    ``None`` for anything that cannot be read — a missing value, a
    torn one, a value in a shape this module does not know.  A
    timestamp that cannot be read is a deadline that cannot be
    compared, and an undecidable deadline is not an expired one: the
    decision waits rather than resolving itself on a guess.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return _as_utc(moment)


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime.

    A naive datetime is read as UTC: every timestamp this codebase
    writes is UTC, so a naive one can only mean that.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The scheduling gate
# ---------------------------------------------------------------------------


def blocked_task_ids(
    plan_id: str,
    *,
    store: Optional[HitlStore] = None,
) -> Set[str]:
    """The tasks of ``plan_id`` that must not be scheduled right now.

    While a question is open, the task it is about — and everything
    downstream of it — has to stay out of the schedule: the work would
    run on a premise the human has not confirmed yet.  Tasks that are
    not downstream of an open question keep running.

    The gate is **computed, never stored**.  No task status changes, so
    answering the question opens the gate again by itself, and a plan
    without an open question schedules exactly what it scheduled
    before.

    Parameters
    ----------
    plan_id:
        The plan whose decisions and task graph are read.
    store:
        The store to read through.  One is opened for the call when
        not given.

    Returns
    -------
    Set[str]
        The task ids to keep out of the schedule.  Empty when nothing
        is open.
    """
    with _store_of(store) as opened:
        roots = {
            decision.task_id
            for decision in opened.list_by_plan(plan_id)
            if decision.status in BLOCKING_STATUSES and decision.task_id
        }
        if not roots:
            return set()
        return _downstream_closure(roots, _dependency_graph(plan_id, opened))


def _dependency_graph(plan_id: str, store: HitlStore) -> Dict[str, Set[str]]:
    """``{task_id: {the ids it depends on}}`` for ``plan_id``'s tasks.

    The graph is read from both places it is written: the plan's own
    ``tasks.json`` (the definition file the dispatcher schedules from)
    and the ``plan_tasks`` table (the runtime mirror the refiner and
    the verification repair write).  A plan whose graph exists in only
    one of the two is still described correctly — the edges of the two
    are merged, never one source's answer overriding the other's.
    """
    graph: Dict[str, Set[str]] = {}

    def _add(task_id: Any, depends_on: Any) -> None:
        key = str(task_id or "").strip()
        if not key:
            return
        graph.setdefault(key, set()).update(_as_id_set(depends_on))

    for row in _runtime_task_rows(store, plan_id):
        _add(row["task_id"], row["depends_on"])

    for task in _defined_tasks(plan_id):
        _add(task.get("id"), task.get("depends_on"))

    return graph


def _runtime_task_rows(store: HitlStore, plan_id: str) -> List[Any]:
    """The plan's rows in ``plan_tasks``, or nothing if it cannot be read.

    A database without the table is a database this module cannot
    describe a graph from — not a reason to fail the caller, which is
    asking a question about scheduling, not about the schema.
    """
    try:
        return list(
            _connection_of(store).execute(
                "SELECT task_id, depends_on FROM plan_tasks WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        )
    except sqlite3.Error as exc:
        logger.warning(
            "hitl_gate_graph_unreadable: plan_tasks of plan %s: %s", plan_id, exc
        )
        return []


def _defined_tasks(plan_id: str) -> List[Dict[str, Any]]:
    """The tasks the plan's ``tasks.json`` defines.

    An absent or unreadable file defines no tasks: the file is the
    plan's, and a plan whose file cannot be read is a plan whose
    graph this module has nothing to say about.
    """
    path = resolve_plans_dir() / plan_id / "tasks.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "hitl_gate_graph_unreadable: %s: %s; continuing without it",
            path,
            type(exc).__name__,
        )
        return []
    if isinstance(document, dict):
        tasks = document.get("tasks")
    elif isinstance(document, list):
        tasks = document
    else:
        tasks = None
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def _as_id_set(raw: Any) -> Set[str]:
    """Read a ``depends_on`` value as a set of task ids.

    The column is JSON in the database and a list in the file; both
    spellings mean the same thing, so both are read here.  A value
    that cannot be read contributes no edge — a graph that is missing
    one edge blocks less, and blocking less is the direction that
    lets the plan make progress.
    """
    if raw is None:
        return set()
    value: Any = raw
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return {text.strip()} if text.strip() else set()
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, (list, tuple, set)):
        return {
            str(item).strip()
            for item in value
            if isinstance(item, (str, int)) and str(item).strip()
        }
    return set()


def _downstream_closure(
    roots: Set[str],
    graph: Dict[str, Set[str]],
) -> Set[str]:
    """``roots`` plus every task that transitively depends on them.

    ``depends_on`` points *upstream*, so the walk goes the other way:
    a task is blocked when something it depends on is blocked.  The
    roots are blocked themselves — the task the question is about may
    not run either.
    """
    children: Dict[str, Set[str]] = {}
    for task_id, dependencies in graph.items():
        for dependency in dependencies:
            children.setdefault(dependency, set()).add(task_id)

    blocked = set(roots)
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child not in blocked:
                blocked.add(child)
                frontier.append(child)
    return blocked