"""The scheduler over HTTP: one read model, one capacity knob, and four
operator actions.

What this file is
-----------------
``GET /api/scheduler/state`` — the whole scheduler state, in one
document.  The operator panel, the LUI and anything else that wantsto know "what is running, what is waiting, and why is it waiting" read
this one endpoint, so that they cannot disagree with each other.

``POST /api/scheduler/capacity`` — the operator's capacity knob: how
many plans may run at the same time, written while the scheduler runs.

``POST /api/plan/{plan_id}/scheduler/{promote|pause|resume|start}`` —
the four actions an operator can take on a plan.  Each one is a
**command**: it goes into the engine's mailbox, the worker carries it
out between two ticks, and the answer is **the effect the action had** —
not "accepted", and not a promise.  The response body says what happened
(``outcome``), which command did it (``command_id``), and the detail that
says how (``detail``).

The document is not assembled here
----------------------------------
:func:`plan_scheduler.snapshot.build` assembles the read model, from the
state of one tick, with the same comparator the admission check sorts by.
This module does not sort, does not count, does not decide and does not
read the database: it asks the engine for its snapshot and hands the
answer on.  A second reader would be a second answer to the same
question, and the only way of telling the two apart would be to watch a
plan start in the wrong order.

The same goes for the actions.  The **policy** — what a promotion is,
who may be interrupted, what a start does when the capacity is full —
belongs to the command executor
(:mod:`plan_scheduler.commands`), and this module does not have an
opinion about it.  What this module owns is the **translation**: the
command's own vocabulary of outcomes becomes an HTTP status code and a
document, and the rules of that translation are the same for all four
actions.

The six keys
------------
``capacity``, ``queue_length``, ``over_capacity``, ``queue``,
``running`` and ``generated_at`` — the document the panel renders andthe LUI reports.  The list lives in
:func:`plan_scheduler.snapshot.build`, which is the only place it is
written down; this module does not repeat it, because a second copy of
the key set is a second thing to keep in step with the first.

What an idle scheduler answers
------------------------------
An empty document: an empty queue, nothing running, ``used = 0``.  Nota 404 (this route is registered ahead of the frontend catch-all, so a
GET on it is never answered with a file) and not a 500 — the panel
polls this endpoint, and a panel that breaks when there is nothing to
show cannot tell "nothing is running" from "the scheduler is broken".

The same document is the answer to every way of not having one: no
engine on the app, an engine that raises while reading its state, and
an engine that answers with something that is not a document at all.
Each of those is logged; none of them is a reason to leave the panel
without an answer.

The engine handle
-----------------
``app.state.scheduler_engine`` — the same handle the lifespan hangs
the engine on, and ``None`` while the scheduler is not running.  The
routes read it and nothing else; they never construct an engine and
never start one.  The capacity endpoint writes **through** that engine
— on its store, on its connection — so that the scheduler's facts keep
their one writer and the route opens no database of its own.

The capacity knob
-----------------
``POST /api/scheduler/capacity`` writes the **capacity override** — how
many plans may run at the same time — while the scheduler runs, and
takes it away again on request.  The override is a row in the state
database, and the engine reads that row on every tick, so a number
written here is in force at once and a number removed puts the
configured capacity back.

The answer carries the number that is in force **and the word that
says where it came from** — ``override`` or ``config`` — because a
panel that shows the number without the word cannot tell the
operator's number from the configured one.

Three answers, and what each of them is:

* **200** — the capacity was written, or the configured one was put
  back;
* **400** — the request says something the scheduler cannot run with: a
  capacity that is not a number, a number outside the range, or a
  request that says nothing at all.  The refusal says which of them it
  was;
* **503** — there is no scheduler to write on: the engine is not
  running, or its configuration says the scheduler is switched off.

A number written here is **the** capacity while it is there, and the
engine reads it again on every tick — so the endpoint writes a fact,
not a wish.

The answers the four actions give
---------------------------------
The command's vocabulary is **closed** — the executor's five words and
the engine's two — and each of them is answered with one status:

=============  ======  ====================================================
outcome        status  what it means
=============  ======  ====================================================
``applied``    200     the action was carried out
``started``    200     the action started the plan
``blocked_by_  200     the capacity is full and nobody may be interrupted;
capacity``             the plan waits at the head of the queue
``pending``    202     the command is still in the mailbox
``not_found``  404     there is no such plan
``conflict``   409     the action conflicts with the plan's state
``failed``     500     the command raised
=============  ======  ====================================================

``blocked_by_capacity`` is a 200 and not an error: the caller asked
whether the plan may start now and got an answer, and the answer is
"not yet, and here is why".  A 409 says the plan and the action cannot
be reconciled at all — it is already working, it is in a phase the
action does not apply to — and it carries the **reason**, so that the
panel can say which of the two it was.

``pending`` is the answer to a **bounded** wait: the caller's wait ran
out, but the command is **not** withdrawn — the worker still carries it
out, and the caller can come back for the outcome with the command id
it was given.  That is why every answer, 200 and 202 alike, carries the
command id.

What is outside the vocabulary
------------------------------
The four actions are **fixed paths**, and the vocabulary is closed: an
action outside it has no command and no effect.  A request that matches
no route at all, though, is answered by the frontend's catch-all — whose
answer is a 405, a sentence about the method when the real problem is
the URL.  Two malformed shapes fall through that way: an **empty** plan
id, and one that carries a **path separator** (a traversal), because
the router splits the path before the handler ever sees it.

So there is one more route here, and it only ever **refuses**: an
action outside the four is a 404, and a plan id the router could not
carry is the shared guard's own 400.  It is registered after the four
fixed ones, so the fixed paths stay the only paths that carry out an
action.

The errors are passed through
-----------------------------
The lifecycle answers 400, 404 and 409 in its own words, and those
words come out unchanged: a route that turned any of them into a 500
would take away the difference between "you sent something wrong",
"there is no such plan" and "not now".  The plan id is admitted through
the shared guard (``server._plan_dir``) before anything else happens,
so a malformed id is the guard's own 400 — a bad request, not a missing
plan.

A refusal travels the whole way: the lifecycle raises it, the command
executor recognises it and translates it, the worker writes the command
down as **carried out, action refused**, and the worker hands the
refusal back to the caller **as it came**.  The route reuses the status
and the detail it was given — it does not make a new status out of the
old one, and it does not let a refusal fall through to the 500 that is
reserved for a fault of the scheduler's own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Dict, FrozenSet, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from plan_scheduler.commands import (
    APPLIED,
    BLOCKED_BY_CAPACITY,
    CONFLICT,
    NOT_FOUND,
    STARTED,
    CommandRejected,
)
from plan_scheduler.capacity import CAPACITY_OVERRIDE_KEY
from plan_scheduler.engine import FAILED, PENDING_ANSWER
from plan_scheduler.snapshot import build

# Late binding into the application module: ``server`` owns the shared
# helpers (notably ``_plan_dir``, the one place a request-supplied plan id
# is allowed to become a path).  The suite monkeypatches them as
# ``server.<name>``.  See ``routes/plan_lifecycle.py`` for the rule.
import server as _server

# The state machine's own exceptions.  They are imported here rather than
# from the lifecycle facade because the facade re-exports only one of
# them, and this module needs to tell "there is no such plan" from "the
# row changed under the command".
from state_machine.repositories.routing_repository import (
    ConflictError,
    PlanNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


#: The status each outcome of the command vocabulary is answered with.
#:
#: The mapping is closed on purpose: an outcome that is not in it is a
#: word the route has no answer for, and a route that invented an answer
#: for it would be a second vocabulary.
_STATUS_BY_OUTCOME = {
    APPLIED: 200,
    STARTED: 200,
    BLOCKED_BY_CAPACITY: 200,
    PENDING_ANSWER: 202,
    NOT_FOUND: 404,
    CONFLICT: 409,
    FAILED: 500,
}

#: The four actions, as one set.
#:
#: The command vocabulary is **closed**: the four endpoints above are the
#: four ways to act on a plan, and there is no fifth.  The set is
#: written down here because the refusal route below has to recognise
#: the vocabulary to refuse what is outside it — and a refusal that
#: listed the words in prose would be a second place to keep in step
#: with the four routes.
_ACTIONS: FrozenSet[str] = frozenset({"promote", "pause", "resume", "start"})


def _empty_snapshot() -> Dict[str, Any]:
    """The idle scheduler's read model.

    Assembled by the production builder out of an empty state, so the
    idle document has the very same six keys as a busy one — a caller
    does not have to branch on "is there anything to show" before it
    can read the answer.
    """
    return build({})


@router.get("/api/scheduler/state")
def get_scheduler_state(request: Request) -> Dict[str, Any]:
    """Answer with the scheduler's read model.

    The engine's own snapshot: the six keys, the queue in the
    production order, the numbers the panel shows beside the flags
    that describe them.

    A scheduler that is not running answers the empty document rather
    than an error — the endpoint is what the panel polls, and an
    endpoint that fails when there is nothing to report leaves the
    operator with a broken panel and no way to tell an idle scheduler
    from a broken one.
    """
    engine = getattr(request.app.state, "scheduler_engine", None)

    if engine is None:
        return _empty_snapshot()

    try:
        snapshot = engine.snapshot()
    except Exception:
        # A snapshot that cannot be read is still "nothing is running"
        # as far as the panel is concerned — and the panel must keep
        # answering.  The failure is logged rather than swallowed.
        logger.exception("the scheduler's snapshot could not be read")
        return _empty_snapshot()

    if not isinstance(snapshot, Mapping):
        # An answer that is not a document is not the read model: the
        # panel reads the six keys by name, and a document without them
        # leaves it reading keys that are not there.  The idle document
        # is the one answer this route is allowed to give on its own,
        # so the panel keeps polling and keeps getting the read model.
        logger.warning(
            "the scheduler's snapshot is not a document (%s); "
            "answering the idle read model",
            type(snapshot).__name__,
        )
        return _empty_snapshot()

    return dict(snapshot)


# ---------------------------------------------------------------------------
# The capacity knob
# ---------------------------------------------------------------------------


#: The lowest and the highest capacity the endpoint accepts.
#:
#: A capacity is a number of plans the machine may run at the same
#: time: it has to be a whole number, and it has to be inside the
#: range.  The range is written down here — once — so that the endpoint
#: and the panel cannot drift apart about what may be asked for, and so
#: that the refusal can quote it.
_CAPACITY_MIN = 1
_CAPACITY_MAX = 64


class CapacityRequest(BaseModel):
    """The request body of ``POST /api/scheduler/capacity``.

    Two fields, two meanings:

    * ``total`` — the capacity the operator asks for: a whole number
      between 1 and 64, or ``None`` to put the configured one back;
    * ``reset`` — put the configured capacity back.

    ``total`` is typed ``Any`` and not ``int`` **on purpose**: a model
    that typed it as an integer would quietly turn ``"5"`` into ``5``
    and ``true`` into ``1`` before the route ever saw them — and
    neither is the number that was asked for.  The model here only
    **takes the request in**; whether the number is one the scheduler
    may run with is the route's answer to give, and it gives it with a
    400 that says what was wrong.
    """

    total: Any = Field(
        default=None,
        json_schema_extra={
            "type": ["integer", "null"],
            "minimum": _CAPACITY_MIN,
            "maximum": _CAPACITY_MAX,
            "description": (
                "how many plans may run at the same time; null puts the "
                "configured capacity back"
            ),
        },
    )
    reset: bool = Field(
        default=False,
        description="put the configured capacity back",
    )


def _scheduler_engine(request: Request) -> Any:
    """The engine the request is about — or a refusal.

    The handle is the one the lifespan hangs the engine on, and it is
    ``None`` while the scheduler is not running.  An engine whose
    configuration says the scheduler is switched off is refused the same
    way: nothing is running that could enforce a capacity, so a number
    written on it would be a number nobody keeps.
    """
    engine = getattr(request.app.state, "scheduler_engine", None)
    if engine is None:
        raise HTTPException(503, {"error": "scheduler_unavailable"})

    config = getattr(engine, "_config", None)
    if not bool(getattr(config, "enabled", True)):
        raise HTTPException(503, {"error": "scheduler_unavailable"})

    return engine


def _requested_total(body: CapacityRequest) -> Optional[int]:
    """The number that was asked for — or ``None`` to reset.

    Three questions, in this order:

    * a number was sent → it is the capacity, and it has to be one the
      scheduler may run with;
    * ``total`` was sent as ``null``, or ``reset`` was asked for → put
      the configured capacity back;
    * nothing was said → the request says nothing, and is refused.

    A number wins over ``reset``: a number was asked for, and one
    number is more specific than "put it back".
    """
    if "total" in body.model_fields_set and body.total is not None:
        value = body.total
        # ``bool`` is an ``int`` in Python, so ``true`` would otherwise
        # slip through as the capacity ``1`` — a number nobody wrote.
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                400,
                "total must be a whole number between "
                f"{_CAPACITY_MIN} and {_CAPACITY_MAX}",
            )
        if value < _CAPACITY_MIN or value > _CAPACITY_MAX:
            raise HTTPException(
                400,
                "total must be between "
                f"{_CAPACITY_MIN} and {_CAPACITY_MAX} (got {value})",
            )
        return value

    if body.reset or "total" in body.model_fields_set:
        return None

    raise HTTPException(
        400,
        "the request must carry a total, or reset: true",
    )


def _write_capacity(engine: Any, total: Optional[int]) -> None:
    """Write the operator's capacity — or take it away again.

    The override is a **row in the state database**, and the engine
    reads that row on every tick: a number written here is in force at
    once, and a number removed puts the configured capacity back.

    The row is written through the engine's own store, on the engine's
    own connection.  The route opens no database of its own: there is
    one connection on the state database, and the scheduler's facts
    keep their one writer.
    """
    store = getattr(engine, "_store", None)
    conn = getattr(engine, "_conn", None)
    if store is None or conn is None:
        # An engine that cannot be written on is an engine the capacity
        # cannot be written on either.
        raise HTTPException(503, {"error": "scheduler_unavailable"})

    if total is None:
        store.delete_setting(conn, CAPACITY_OVERRIDE_KEY)
    else:
        store.set_setting(conn, CAPACITY_OVERRIDE_KEY, str(total))


def _capacity_in_force(engine: Any) -> Dict[str, Any]:
    """The capacity that is in force right now, and where it came from.

    Read from the engine's own reader — the one that fills the read
    model's ``capacity`` and its ``source`` — so that the number and
    the word always agree with each other, and so that the answer to
    this call and the answer to ``GET /api/scheduler/state`` cannot
    disagree.
    """
    total, source = engine._capacity_and_source()
    return {"total": total, "source": source}


@router.post("/api/scheduler/capacity")
def set_capacity(body: CapacityRequest, request: Request) -> JSONResponse:
    """Set the capacity the scheduler runs with — or put it back.

    The number the operator writes is the capacity **while it is
    there**: the engine reads the override again on every tick, so it
    takes effect at once and needs no restart.

    The answer says which number is in force and where it came from —
    ``override`` for the operator's number, ``config`` for the
    configured one — so a caller never has to guess whether the number
    it sees is the one it wrote.
    """
    engine = _scheduler_engine(request)
    total = _requested_total(body)

    _write_capacity(engine, total)

    return JSONResponse(
        status_code=200, content={"capacity": _capacity_in_force(engine)}
    )


# ---------------------------------------------------------------------------
# The four actions
# ---------------------------------------------------------------------------


def _body(outcome: str, command_id: Optional[int], detail: Any) -> Dict[str, Any]:
    """The document one action is answered with.

    Three keys are always there — what happened, which command did it,
    and how — and two are added when they say something: a conflict
    carries the **reason**, and a command that is still in the mailbox
    carries the **status** that says so.
    """
    body: Dict[str, Any] = {
        "outcome": outcome,
        "command_id": command_id,
        "detail": detail,
    }
    if outcome == CONFLICT:
        body["reason"] = detail
    if outcome == PENDING_ANSWER:
        body["status"] = "pending"
    return body


def _answer(status: int, outcome: str, command_id: Optional[int], detail: Any) -> JSONResponse:
    """One action's answer: the status, and the document that goes with it."""
    return JSONResponse(
        status_code=status, content=_body(outcome, command_id, detail)
    )


def _answer_rejection(rejection: Any, command_id: Optional[int]) -> JSONResponse:
    """Answer with the refusal the lifecycle gave, unchanged.

    A refusal carries a status the command's own vocabulary has no word
    for — a 400, a 404, a 409 — so it is not one of the five outcomes:
    it is the **lifecycle's** answer, and it comes back the way it went
    out.  The status is the status, the detail is the detail, and the
    answer is never a 500: a refusal is not a fault of the scheduler.

    Args:
        rejection: the refusal the scheduler carried back.
        command_id: the id of the command's record, when it has one.

    Returns:
        The answer the refusal deserves.

    :raises fastapi.HTTPException: the lifecycle's own HTTP error, when
        that is what the refusal was.  It is handed back as it came, so
        the body is the body the lifecycle's own error would have had.
    """
    status = int(getattr(rejection, "status_code", 409) or 409)
    detail = getattr(rejection, "detail", None)
    cause = getattr(rejection, "cause", None)
    if isinstance(cause, HTTPException):
        # The lifecycle's own HTTP error: it is not re-worded, it is
        # the very exception the lifecycle raised.
        raise cause
    if status == 404:
        return _answer(404, NOT_FOUND, command_id, detail or "no_such_plan")
    return _answer(status, CONFLICT, command_id, detail or "conflict")


def _run_action(request: Request, plan_id: str, action: str) -> JSONResponse:
    """Carry one action out and answer with the effect it had.

    The action is a **command**: it goes into the engine's mailbox and
    the worker carries it out, so the answer is what happened rather
    than what was requested.  The route's own work is the translation —
    the outcome becomes a status, and the status is the one the
    vocabulary says it is.

    The plan id has already been admitted through the shared guard by
    the route that calls this: the guard is stated in **each** route's
    own body, so that the rule "a route that takes a plan id admits it
    through the guard" can be checked by reading the route — and not by
    following a call into a helper.

    :raises fastapi.HTTPException: the 503 for a scheduler that is not
        running.  It is the answer of the thing that raised it.
    """
    engine = getattr(request.app.state, "scheduler_engine", None)
    if engine is None or not engine.enabled:
        # The scheduler is switched off, or it could not be started.
        # There is no command to make, so there is no outcome to
        # report: what the caller gets is the reason there is none.
        #
        # A scheduler that is switched off still has its worker thread —
        # the switch is read on every tick, so it can be turned on
        # without a restart — but it carries out nothing, and an action
        # it carries out nothing with is an action it refuses.
        raise HTTPException(
            503,
            {
                "error": "scheduler_unavailable",
                "detail": "the scheduler is not running",
            },
        )

    try:
        result = engine.submit_command(action, plan_id)
    except HTTPException:
        # The lifecycle's own 400 / 404 / 409, unchanged: the caller
        # needs the difference between "you sent something wrong",
        # "there is no such plan" and "not now".
        raise
    except PlanNotFoundError:
        return _answer(404, NOT_FOUND, None, "no_such_plan")
    except ConflictError as exc:
        # The row changed under the command.  The reason is the
        # lifecycle's own, so it is carried on rather than replaced by
        # a word of the route's own.
        return _answer(
            409, CONFLICT, None, getattr(exc, "reason", None) or "conflict"
        )
    except CommandRejected as rejection:
        # The refusal in the scheduler's own translated form: the status
        # and the detail are the lifecycle's, and they are reused as
        # they came.
        return _answer_rejection(rejection, None)

    outcome = getattr(result, "outcome", None)
    command_id = getattr(result, "command_id", None)
    detail = getattr(result, "detail", None)

    status = _STATUS_BY_OUTCOME.get(outcome)
    if status is None:
        # A word the route has no answer for.  It is a bug in the
        # engine, not a mistake of the caller, so it is logged and
        # answered as the failure it is — never as a success.
        logger.error(
            "the scheduler answered the %s command for %s with an unknown "
            "outcome %r",
            action,
            plan_id,
            outcome,
        )
        return _answer(500, FAILED, command_id, "unknown outcome: {0!r}".format(outcome))

    return _answer(status, outcome, command_id, detail)


@router.post("/api/plan/{plan_id}/scheduler/promote")
def promote_plan(plan_id: str, request: Request) -> JSONResponse:
    """Move a plan ahead in the queue — and nothing else.

    A promotion is a change of order: it evicts nobody and it takes no
    slot, so a plan that is already working cannot be promoted — it is
    not waiting in the queue, and there is no order to move it in.  That
    is a conflict.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)
    return _run_action(request, plan_id, "promote")


@router.post("/api/plan/{plan_id}/scheduler/pause")
def pause_plan(plan_id: str, request: Request) -> JSONResponse:
    """Park a plan: stop it, and let it give its slot back.

    A working plan is stopped through the lifecycle's existing stop
    path, and a waiting one is taken out of the queue and parked.  In
    both cases the plan ends up in ``stopped`` and it stops holding
    compute.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)
    return _run_action(request, plan_id, "pause")


@router.post("/api/plan/{plan_id}/scheduler/resume")
def resume_plan(plan_id: str, request: Request) -> JSONResponse:
    """Put a stopped plan back at the head of the queue.

    A resume is a **wait**, not a take: the plan comes back first, it
    waits for a slot, and it interrupts nobody.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)
    return _run_action(request, plan_id, "resume")


@router.post("/api/plan/{plan_id}/scheduler/start")
def start_plan(plan_id: str, request: Request) -> JSONResponse:
    """Make a plan the next one to run.

    A start is the **only** action that may interrupt somebody, and it
    does so only when the capacity is full and there is a victim the
    rules allow.  With no such victim the plan waits at the head of the
    queue and the answer is ``blocked_by_capacity``.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)
    return _run_action(request, plan_id, "start")


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


@router.post("/api/plan/{plan_id:path}/scheduler/{action:path}")
def scheduler_action_outside_the_vocabulary(
    plan_id: str, action: str
) -> JSONResponse:
    """Refuse what the four fixed endpoints cannot answer.

    The four actions are **fixed paths**, and nothing outside them runs:
    the vocabulary is closed, so an action that is not one of the four
    has no command, no plan and no effect — it is refused with a 404
    that says exactly that.

    This route exists because the four fixed endpoints **cannot match**
    every malformed request, and a request that matches no route at all
    is answered by the frontend's catch-all — whose answer is a 405
    ("this method is not allowed here"), a sentence about the *method*
    when the real problem is the *URL*.  Two shapes fall through:

    * an **empty** plan id — ``/api/plan//scheduler/promote``.  The
      router's ``{plan_id}`` needs at least one character, so the
      segment is simply not there;
    * a plan id that carries a **path separator** — a traversal such as
      ``../../etc/passwd``.  The router splits the path on ``/`` before
      the handler ever sees it, so such an id arrives as *several*
      segments and the fixed route stops matching.

    Both are refusals, and both are answered here:

    * an action **outside** the closed four → 404: there is no such
      endpoint, and nothing is executed;
    * an action **inside** it, with a plan id the router could not
      carry → the shared guard's own 400, unchanged.  The guard is
      ``server._plan_dir``, the one place a request-supplied id may
      become a path, and it refuses an empty id and an id with a
      separator alike.

    The route is registered **after** the four fixed ones, so a request
    that one of them matches is answered by that one: the four fixed
    paths stay the only paths that carry out an action.
    """
    if action not in _ACTIONS:
        return JSONResponse(
            status_code=404,
            content={
                "error": "no_such_action",
                "detail": (
                    "the scheduler knows four actions — promote, pause, "
                    "resume and start — and this is not one of them"
                ),
            },
        )

    # The action is one of the four, so the request is addressed to the
    # scheduler — but no fixed route matched it, which means the plan id
    # is one the router could not carry (empty, or holding a separator).
    # The shared guard answers that, in its own words: a malformed id
    # is a bad request, not a missing plan.
    _server._plan_dir(plan_id)

    # Unreachable in practice: a plan id the fixed route could not
    # match is refused by the guard above.  It is answered rather than
    # left open, so that a future change to the guard cannot turn this
    # into a route that falls through to something else.
    return JSONResponse(
        status_code=404,
        content={
            "error": "no_such_action",
            "detail": "the scheduler knows four actions, and this is not one of them",
        },
    )