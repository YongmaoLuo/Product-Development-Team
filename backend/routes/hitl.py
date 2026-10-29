"""Human-in-the-loop decisions over HTTP: ask, list, answer.

What this file is
-----------------
The three doors a human uses to settle a question the backend could
not answer on its own:

``POST /api/hitl/{plan_id}/decisions``
    Ask.  The question is stored and the plan's own configuration
    decides when it stops waiting.
``GET /api/hitl/{plan_id}/decisions``
    List what a plan is waiting for — optionally narrowed to one
    state, which is what a polling UI asks for.
``POST /api/hitl/{plan_id}/decisions/{decision_id}/answer``
    Answer.  The first answer wins; a second one is a conflict.

Why the rules are not here
--------------------------
Every rule that is not about HTTP lives in :mod:`hitl.service`: which
states may still be answered, what a refused creation leaves behind,
which fields are frozen at creation time.  A rule that lived in a route
handler would be a rule that only exists when somebody sends that
request — and the request is the *last* place a rule should be
discovered.  This module parses a request, calls one of the service's
three functions, and turns the result into a response.

One representation of a decision
--------------------------------
The same body is served by all three endpoints: what a decision *is*
right now.  A caller that creates a decision, lists it and answers it
therefore reads one shape, and a UI does not have to learn two.

The two deliberate differences
------------------------------
**An unknown plan lists nothing.**  A list answers "what is
outstanding", not "does the plan exist": a plan that never asked
anything and a plan that does not exist are the same answer, and both
are an empty list.  The plan id is still validated — ``..`` is a bad
id, not a plan.

**A status filter outside the vocabulary is refused.**  ``status`` is a
closed set of five words.  A filter nobody can interpret would
silently return the wrong set — a UI asking for ``"PENDING"`` would be
told nothing is outstanding and would stop showing the question — so it
is a 400 rather than an empty list.

**The error bodies are flat.**  A refusal is raised as an
``HTTPException`` whose detail is a dict with an ``error`` key, and the
application's own handler (``server.custom_http_exception_handler``)
turns such a detail into the response body as it is.  So a client
reads ``body["error"]`` for the rule that refused the request and
``body["detail"]`` for the words — the same shape the request guard's
403 already has, and the same shape a 409 uses for the ``status`` the
decision is actually in.

An answer is also a scheduling signal
-------------------------------------
The plan that asked the question may have been waiting for it, and the
answer is the signal that it may go back to work.  This route does not
decide that: it reads the engine off the application
(``app.state.scheduler_engine`` — the same handle the lifespan hangs
the engine on, and ``None`` while the scheduler is not running) and
hands it to the service, which submits the ``resume`` that puts a
parked plan back at the head of the queue.  A plan that is still
working is left alone, and a scheduler that is not there is not a
reason to refuse an answer.

What this file deliberately does not do
---------------------------------------
* **It does not bypass the request guard.**  The guard is middleware
  over every ``/api/`` path, so these routes are behind it for free —
  a request without ``X-PDT-Request`` is refused before it reaches a
  handler.  Nothing here re-implements, weakens or reorders that.
* **It does not open the database.**  The store belongs to the
  service; the route reads decisions through the service's own
  functions, so the "one writer" rule of the ``plan_decisions`` table
  holds for the HTTP surface too.
* **It does not second-guess the answer.**  The service stores the
  option it was given; this route does not check it against the
  options that were offered, for the same reason — the rule belongs to
  one layer, and it is not this one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hitl.models import Decision, DecisionMode, DecisionRequest
from hitl.service import (
    DecisionRejected,
    answer_decision,
    create_decision,
    list_decisions,
)

# Late binding into the application module: ``server`` owns the shared
# helpers and module globals, and the suite monkeypatches them as
# ``server.<name>``.  See ``routes/phases.py`` for the rule.
import server as _server


router = APIRouter()


#: The ``source`` recorded for a decision nobody else claimed: this
#: route.  It is free-form on purpose — a new caller must not need a
#: schema change to ask a question.
DEFAULT_SOURCE = "api"


class CreateDecisionRequest(BaseModel):
    """Body of ``POST /api/hitl/{plan_id}/decisions``.

    ``question`` and ``options`` are the two things a decision cannot
    be made of: what is being asked, and what the answers on the table
    are.  ``default_option`` is the answer to use when nobody replies
    in time, and it has to be one of the offered options.

    ``mode`` and ``timeout_sec`` are the *request's* own defaults.  The
    plan's ``hitl_config.json`` decides what is actually stored — a
    decision keeps the meaning it was created with even when the plan's
    configuration is rewritten afterwards (see
    :func:`hitl.service.create_decision`).  They are accepted here
    because the service's own rules read them: a request whose own
    timeout is not a positive number of seconds is refused rather than
    stored.

    ``vp_id`` / ``failure_class`` / ``round`` say *what the question is
    about* — the verification point, the class of the failure that
    raised it, and the round of the verification loop it was raised in.
    They are what makes a decision findable by the failure it is
    about, rather than only by the plan it belongs to.

    ``answer_note`` is a remark recorded with the question.  A note
    that comes with an answer replaces it.
    """

    question: str
    options: List[str]
    default_option: Optional[str] = None
    source: str = DEFAULT_SOURCE
    task_id: Optional[str] = None
    vp_id: Optional[str] = None
    failure_class: Optional[str] = None
    round: Optional[int] = None
    answer_note: Optional[str] = None
    mode: Optional[str] = None
    timeout_sec: Optional[int] = None


class AnswerDecisionRequest(BaseModel):
    """Body of ``POST /api/hitl/{plan_id}/decisions/{decision_id}/answer``.

    ``option`` is the answer, ``answered_by`` who gave it and ``note``
    the free-form remark that travels with it into the audit mirror.
    The option is stored as given: which of the offered options it is
    is not this route's business to decide.
    """

    option: str = Field(..., min_length=1)
    answered_by: Optional[str] = None
    note: str = ""


# ---------------------------------------------------------------------------
# The representation
# ---------------------------------------------------------------------------


def _decision_body(decision: Decision) -> Dict[str, Any]:
    """What a decision looks like from outside, as it stands right now.

    The same body is served by all three endpoints, so a caller that
    creates a decision, lists it and answers it reads one shape.
    """
    return {
        "decision_id": decision.decision_id,
        "plan_id": decision.plan_id,
        "status": decision.status,
        "question": decision.question,
        "options": list(decision.options),
        "default_option": decision.default_option,
        "mode": decision.mode,
        "timeout_sec": decision.timeout_sec,
        "answer": decision.answer,
        "answered_by": decision.answered_by,
        "answered_at": decision.answered_at,
        "deadline_at": decision.deadline_at,
        "created_at": decision.created_at,
        "updated_at": decision.updated_at,
        "source": decision.source,
        "task_id": decision.task_id,
        "vp_id": decision.vp_id,
        "failure_class": decision.failure_class,
        "round": decision.round,
        "answer_note": decision.answer_note,
    }


def _stored_decision(plan_id: str, decision_id: str) -> Optional[Decision]:
    """The plan's decision ``decision_id``, or ``None``.

    Read through the service rather than through the store: the store is
    the service's business, and a route that opened the database would
    be a second writer of the ``plan_decisions`` table.
    """
    for decision in list_decisions(plan_id):
        if decision.decision_id == decision_id:
            return decision
    return None


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


@router.post("/api/hitl/{plan_id}/decisions")
def create_hitl_decision(
    plan_id: str, payload: CreateDecisionRequest
) -> Dict[str, Any]:
    """Ask a human, on behalf of ``plan_id``, and store the question.

    The plan's own configuration is read from its directory and the two
    fields it owns — the mode and the timeout — are frozen into the
    stored decision, so a question that is already open keeps the
    meaning it was created with.

    A question that could not become a decision that works — an
    ``auto`` decision with no default answer, a non-positive timeout,
    an empty option set — is refused with a 400 and leaves no row
    behind.
    """
    plan_dir = _server._plan_dir(plan_id)

    try:
        request = DecisionRequest(
            plan_id=plan_id,
            source=payload.source,
            task_id=payload.task_id,
            vp_id=payload.vp_id,
            failure_class=payload.failure_class,
            round=payload.round,
            question=payload.question,
            options=list(payload.options),
            default_option=payload.default_option,
            answer_note=payload.answer_note,
            mode=(
                payload.mode
                if payload.mode is not None
                else DecisionMode.AUTO.value
            ),
            timeout_sec=payload.timeout_sec,
        )
    except ValueError as exc:
        raise HTTPException(
            400,
            {"error": "invalid_request", "detail": str(exc)},
        ) from exc

    try:
        decision = create_decision(request, plan_dir)
    except DecisionRejected as exc:
        raise HTTPException(
            400,
            {
                "error": exc.reason,
                "detail": str(exc),
                "decision_id": exc.decision_id,
            },
        ) from exc

    return _decision_body(decision)


@router.get("/api/hitl/{plan_id}/decisions")
def list_hitl_decisions(
    plan_id: str, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """The plan's decisions, oldest first, optionally narrowed to one state.

    ``status`` is the closed set of five decision states; a value
    outside it is a 400 rather than an empty list, because a filter
    nobody can interpret would silently return the wrong set.

    An unknown plan is not an error but an empty list: the question
    this answers is "what is outstanding", not "does the plan exist".
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function

    try:
        decisions = list_decisions(plan_id, status)
    except ValueError as exc:
        raise HTTPException(
            400,
            {"error": "invalid_status", "detail": str(exc)},
        ) from exc

    return [_decision_body(decision) for decision in decisions]


@router.post("/api/hitl/{plan_id}/decisions/{decision_id}/answer")
def answer_hitl_decision(
    plan_id: str,
    decision_id: str,
    payload: AnswerDecisionRequest,
    request: Request,
) -> Dict[str, Any]:
    """Record a human's answer to one of the plan's decisions.

    The answer is scoped to the plan in the path: a decision of another
    plan is reported as not found, so a stale card cannot answer
    somebody else's question.

    The first answer wins.  A decision that is already settled answers
    with a 409 and keeps what it had — a second answer is a conflict,
    never an overwrite.

    An answer is also the signal that the plan may go back to work, so
    the route hands the scheduler the engine it is running with
    (``app.state.scheduler_engine`` — the same handle the lifespan
    hangs the engine on, and ``None`` while the scheduler is not
    running).  The service submits the ``resume`` that puts a parked
    plan back at the head of the queue; a plan that is still working is
    left alone, and a scheduler that is not there is not a reason to
    refuse the answer.
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function

    engine = getattr(request.app.state, "scheduler_engine", None)

    status, code = answer_decision(
        decision_id,
        payload.option,
        payload.answered_by,
        payload.note,
        plan_id=plan_id,
        engine=engine,
    )

    if code == 404:
        raise HTTPException(
            404,
            {
                "error": "not_found",
                "detail": (
                    f"no decision {decision_id!r} in plan {plan_id!r}; "
                    "there is nothing here to answer"
                ),
            },
        )
    if code == 409:
        raise HTTPException(
            409,
            {
                "error": "conflict",
                "status": status,
                "detail": (
                    f"decision {decision_id!r} is already {status}; a "
                    "second answer is a conflict, not an overwrite"
                ),
            },
        )

    stored = _stored_decision(plan_id, decision_id)
    if stored is None:
        # The answer was taken — the service said so — but the row is
        # not where it was a moment ago.  Report the outcome rather
        # than failing an action that succeeded: a 500 here would
        # invite a retry, and a retry is a second answer.
        return {
            "decision_id": decision_id,
            "plan_id": plan_id,
            "status": status,
        }
    return _decision_body(stored)
