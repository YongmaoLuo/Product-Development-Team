"""The scheduler over HTTP: one read model, one capacity knob, and four
operator actions.

What this file is
-----------------
``GET /api/scheduler/state`` — the whole scheduler state, in one
document.  The operator panel, the LUI and anything else that wantsto know "what is running, what is waiting, and why is it waiting" read
this one endpoint, so that they cannot disagree with each other.

``GET /api/scheduler/plan/{plan_id}/detail`` — one plan's own progress:
where it is, how many tasks are done, and what is running this moment.
The wording is the Feishu card's own — the panel and the card cannot
disagree about what a plan is doing — and asking for it is a **read**:
it writes nothing.

``GET /api/scheduler/finishing`` — the plans that are being **finished**:
the ones in the finishing stage, with the phase they are in and the
questions they are waiting on.  They are in no queue and in no running
line, so the read model below cannot name them — a plan being finished
holds no slot and waits for no capacity.

``POST /api/scheduler/capacity`` — the operator's capacity knob: how
many plans may run at the same time, written while the scheduler runs.

``POST /api/plan/{plan_id}/scheduler/{promote|pause|resume|start}`` —
the four actions an operator can take on a plan.  Each one is a
**command**: it goes into the engine's mailbox, the worker carries it
out between two ticks, and the answer is **the effect the action had** —
not "accepted", and not a promise.  The response body says what happened
(``outcome``), which command did it (``command_id``), and the detail that
says how (``detail``).

``POST /api/scheduler/{plan_id}/waits/{item_id}/{extend|terminate}`` —
the two actions the operator panel offers on a **waiting row**: push the
deadline out, or give up on the wait.  Both change the plan's own
finishing state, and both are carried out through the one lifecycle that
owns a wait — the answer says what the change did to the wait, not that
the change was accepted.

The document is not assembled here
----------------------------------
:func:`plan_scheduler.snapshot.build` assembles the read model, from the
state of one tick, with the same comparator the admission check sorts by.
This module does not sort, does not count, does not decide and does not
read the database: it asks the engine for its snapshot and hands the
answer on.  A second reader would be a second answer to the same
question, and the only way of telling the two apart would be to watch a
plan start in the wrong order.

The finishing endpoint is the one place that reads a row of its own:
the plans that are being finished are not in the read model at all,
and their phase lives in the routing table — the same table the plans
listing reads, so the phase here and the phase in the sidebar are the
same fact.

The same goes for the actions.  The **policy** — what a promotion is,
who may be interrupted, what a start does when the capacity is full —
belongs to the command executor
(:mod:`plan_scheduler.commands`), and this module does not have an
opinion about it.  What this module owns is the **translation**: the
command's own vocabulary of outcomes becomes an HTTP status code and a
document, and the rules of that translation are the same for all four
actions.

The eight keys
--------------
``capacity``, ``queue_length``, ``over_capacity``, ``queue``,
``running``, ``waiting``, ``parked`` and ``generated_at`` — the document
the panel renders andthe LUI reports.  The list lives in
:func:`plan_scheduler.snapshot.build`, which is the only place it is
written down; this module does not repeat it, because a second copy of
the key set is a second thing to keep in step with the first.

The ``parked`` list is the plans that stopped for want of provider
capacity.  It holds no slot and is in no line, so the panel would not
see those plans at all unless the read model named them — and its rows
carry a phase, so they carry the label of that phase like every other
row does.

The route's own published description still says "the six keys": it is
the endpoint's OpenAPI description, and that description is frozen by
the approved baseline (``tests/api/openapi_snapshot.json``).  It is
refreshed when that baseline is re-taken, not here.

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

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field

from config_paths import resolve_plans_dir, resolve_state_db_path
from file_lock_protocol import locks_dir_for_plan
from hitl.store import HitlStore
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
from plan_scheduler.snapshot import build, postprocess_summary

# The finishing stage's waiting lifecycle.  The registry is the one
# thing that may move a deadline, append an audit entry or give up on
# a wait, and the progress module is the one thing that may write the
# record it keeps.  Both are imported here rather than reached
# through the orchestrator, because an endpoint that changes a wait
# must not go through the stage that *asks* the question.
from postprocess.progress import (
    PROGRESS_FILENAME,
    ProgressError,
    load_progress,
    progress_path,
)
from postprocess.waits import (
    PENDING,
    WAIT_TYPES,
    WaitConflict,
    WaitError,
    WaitRegistry,
)

# The requirement book — the only input the finishing stage judges
# against.  The loader is imported here so that an edit made through
# the panel is judged by the very rules the stage itself applies: a
# book the stage would refuse must not be written back.
from postprocess.spec import SPEC_FILENAME, Spec, SpecError, freeze, load_spec

# Late binding into the application module: ``server`` owns the shared
# helpers (notably ``_plan_dir``, the one place a request-supplied plan id
# is allowed to become a path).  The suite monkeypatches them as
# ``server.<name>``.  See ``routes/plan_lifecycle.py`` for the rule.
import server as _server

# Late binding into the plans module: the phase **labels** live in one
# place — ``plan_state.PHASE_LABELS`` — and ``routes.plans._phase_label``
# is the one function that reads it, so the same phase cannot be
# labelled two ways in two endpoints.  The **module** is imported rather
# than the function: the attribute is looked up when the label is
# computed, so a helper that is replaced is seen here.
from routes import plans as _routes_plans

# Late binding into the notifications package: the **progress wording**
# lives in the card's own builder (``notifications.cards``), and the
# plan-detail endpoint hands that wording on rather than growing a
# second phrasing of it.  The **module** is imported rather than the
# function: the attribute is looked up when the lines are built, so a
# builder that is replaced is seen here.
from notifications import cards as _cards

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
    idle document has the very same eight keys as a busy one — a caller
    does not have to branch on "is there anything to show" before it
    can read the answer.
    """
    return build({})


#: The three lists of the read model whose rows carry a phase.
#:
#: The parked plans are in it too: a plan parked for quota holds no
#: slot and is in no line, so the panel would not see it at all unless
#: the read model named it.  Its row carries a phase, so it carries
#: the label of that phase like every other row does.
_ROW_LISTS = ("queue", "running", "parked")


def _phase_label_for(phase: Any) -> str:
    """Return the label of ``phase`` — the empty string when it has none.

    The label is worked out by ``routes.plans._phase_label``: the one
    function that reads the one table (``plan_state.PHASE_LABELS``), so
    the same phase cannot be labelled two ways in two endpoints.  The
    **module** is what is imported — the function is looked up when the
    label is computed, not bound when this module is imported.

    A row that carries no phase has no label: the answer is the empty
    string, not a missing key and not an exception — a state written
    before the phase existed is a normal thing to read.
    """
    if not phase:
        return ""
    return str(_routes_plans._phase_label(phase))


def _labeled_row(row: Any) -> Any:
    """Return ``row`` with the label of its phase beside the phase.

    A row that is not a document is handed back as it was: the read
    model's rows are documents, and a row that is not one is the
    engine's business, not this module's.
    """
    if not isinstance(row, Mapping):
        return row

    labeled = dict(row)
    labeled["phase_label"] = _phase_label_for(row.get("phase"))
    return labeled


def _with_phase_labels(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the read model with a ``phase_label`` on every row.

    The phase itself is passed on exactly as it was read — the label is
    a **supplement** to the phase name, never a replacement for it (see
    ``routes.plans._phase_label``).

    Only the three lists change.  The capacity, the queue length, the
    over-capacity flag and the generation stamp are the read model's
    own, and they are handed back untouched.
    """
    labeled = dict(payload)

    for name in _ROW_LISTS:
        rows = payload.get(name)
        if not isinstance(rows, (list, tuple)):
            continue
        labeled[name] = [_labeled_row(row) for row in rows]

    return labeled


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
        return _state_with_waiting(_with_phase_labels(_empty_snapshot()))

    try:
        snapshot = engine.snapshot()
    except Exception:
        # A snapshot that cannot be read is still "nothing is running"
        # as far as the panel is concerned — and the panel must keep
        # answering.  The failure is logged rather than swallowed.
        logger.exception("the scheduler's snapshot could not be read")
        return _state_with_waiting(_with_phase_labels(_empty_snapshot()))

    if not isinstance(snapshot, Mapping):
        # An answer that is not a document is not the read model: the
        # panel reads the eight keys by name, and a document without them
        # leaves it reading keys that are not there.  The idle document
        # is the one answer this route is allowed to give on its own,
        # so the panel keeps polling and keeps getting the read model.
        logger.warning(
            "the scheduler's snapshot is not a document (%s); "
            "answering the idle read model",
            type(snapshot).__name__,
        )
        return _state_with_waiting(_with_phase_labels(_empty_snapshot()))

    return _state_with_waiting(_with_phase_labels(dict(snapshot)))


# ---------------------------------------------------------------------------
# The finishing plans
# ---------------------------------------------------------------------------
#
# A plan that has a requirement book enters the finishing stage
# (``post_processing``) and leaves it as ``completed`` — or stays in
# ``postprocess_failed`` when a required item did not pass.  Neither
# phase holds a slot and neither is a run: a plan that is being
# finished is in no queue and in no running line, so the two blocks
# above cannot show it, and an operator cannot see that a plan has
# reached the finishing stage at all.
#
# The phase is read from the **routing table** — the same table the
# plans listing reads (``plan_routing.current_phase``) — and not from
# a scan of the plans directory: the phase is the workflow's own
# answer, and it lives in exactly one place.


#: The phases that mean "this plan is being finished".
FINISHING_PHASES = ("post_processing", "postprocess_failed")

#: The label each of those phases is drawn with.  The words are the
#: panel's, and they are said once here: the same phase must not be
#: labelled two ways in two places.
FINISHING_PHASE_LABELS = {
    "post_processing": "收尾中",
    "postprocess_failed": "收尾失败",
}


def _finishing_rows(request: Request) -> List[Dict[str, Any]]:
    """The routing rows of the plans that are being finished.

    The rows come from the routing table — the one the plans listing
    reads — so the phase in this answer and the phase in the sidebar
    are the same fact and cannot tell two different stories.

    A routing table that cannot be opened is not a failure of the
    panel: the endpoint answers with no finishing plans and the panel
    shows its empty state.  The panel polls this endpoint every five
    seconds, so a refusal here would be a refusal five times a minute
    for as long as the operator sat there watching.
    """
    try:
        sm = _server._open_state_machine(request)
    except Exception:  # noqa: BLE001 — the panel must keep answering
        logger.exception("the state machine could not be opened")
        return []
    try:
        if sm is None:
            return []
        # ``data_dir`` is left out on purpose: the archived section of
        # the listing carries no phase at all (an archived plan is not
        # in the state machine), so it can never be a finishing plan,
        # and scanning the plans tree on every poll would be a cost
        # paid for an answer that cannot change the result.
        rows = sm[1].list_all()
    except Exception:  # noqa: BLE001 — the panel must keep answering
        logger.exception("the routing table could not be read")
        return []
    finally:
        _server._close_state_machine(sm)

    return [
        row
        for row in rows
        if str(row.get("current_phase") or "").strip() in FINISHING_PHASES
    ]


def _finishing_waits(plan_id: str) -> List[Dict[str, Any]]:
    """One plan's pending waiting rows.

    The rows are the panel's own rows — the very ones the waiting
    table draws — so the finishing block and the waiting block cannot
    tell two different stories about what a plan is waiting for.

    A plan whose wait records cannot be read is listed with no waits
    rather than not listed at all: the plan **is** being finished, and
    that is the fact this section is here to answer.
    """
    try:
        return plan_waiting_rows(plan_id)
    except Exception:  # noqa: BLE001 — one bad plan must not 500 the page
        logger.warning(
            "the waiting rows of plan %s could not be read", plan_id
        )
        return []


def _finishing_summary(plan_id: str) -> Optional[Dict[str, Any]]:
    """One plan's terminal summary, or ``None`` when it has none.

    A plan that is still being finished has no terminal report yet —
    that is an ordinary state, not an error — and a report that cannot
    be read is a plan whose verdicts were destroyed, which is also not
    a reason to fail the whole answer.
    """
    try:
        return postprocess_summary(plan_id, plans_dir=resolve_plans_dir())
    except Exception:  # noqa: BLE001 — one bad report must not 500 the page
        logger.warning(
            "the terminal report of plan %s could not be read", plan_id
        )
        return None


@router.get("/api/scheduler/finishing")
def get_scheduler_finishing(request: Request) -> Dict[str, Any]:
    """Answer with the plans that are being finished.

    The finishing stage is a phase of a plan's life, and it is a phase
    the read model above does not carry: a plan being finished holds
    no slot and waits for no capacity, so it is in neither the queue
    nor the running list.  This endpoint is where those plans are
    named, and it names them with the two things an operator needs
    about each of them:

    * the **phase** — 收尾中 or 收尾失败 — so a plan whose finishing
      stage failed is not mistaken for a plan that is still working;
    * the **waiting rows** — the questions the stage is waiting on,
      with the three operations the waiting table offers.

    The answer is a **read**: it starts no subprocess, opens no
    database of its own beyond the routing table it is asked about,
    and writes nothing.  A plan that is not being finished is not in
    it, and no finishing plan at all is an empty list rather than an
    error — the panel polls this endpoint, and an endpoint that fails
    when there is nothing to report leaves the operator with a broken
    panel and no way to tell an idle stage from a broken one.
    """
    plans: List[Dict[str, Any]] = []

    for row in _finishing_rows(request):
        plan_id = str(row.get("plan_id") or "").strip()
        phase = str(row.get("current_phase") or "").strip()
        if not plan_id:
            continue
        plans.append(
            {
                "plan_id": plan_id,
                "phase": phase,
                "phase_label": FINISHING_PHASE_LABELS.get(phase, phase),
                "postprocess": _finishing_summary(plan_id),
                "waits": _finishing_waits(plan_id),
                "updated_at": str(row.get("updated_at") or ""),
            }
        )

    return {"plans": plans}


# ---------------------------------------------------------------------------
# The plan detail
# ---------------------------------------------------------------------------


#: The answer a plan that is not there is given.
#:
#: The detail endpoint has one refusal of its own, and it says what it
#: is about in the panel's own words: the plan is not there.  The
#: wording is this route's — the plans route's own 404 body carries
#: diagnostics about the instance that answered, and those are about
#: **its** question, not about this one.
_PLAN_NOT_FOUND_DETAIL = "plan not found"


def _stamp_text(value: Any) -> str:
    """The stamp ``value`` carries, as text — ``""`` when it carries none.

    Only a string is a stamp: a number, a ``None`` and a boolean are
    not times, and reading one as a time would produce a duration
    nobody can explain.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


def _parse_stamp(value: str) -> Optional[datetime]:
    """The moment ``value`` names, in UTC — ``None`` when it names none.

    A **naive** stamp is read as UTC, because that is what the producer
    writes (``datetime.utcnow().isoformat()``): mixing it with a local
    clock does not fail, it silently answers a wrong number — a run of
    ten minutes reads as two hours on a UTC+8 machine, and an operator
    reading that has every reason to conclude the plan is stuck.

    A trailing ``Z`` is spelled out as an offset, so the answer is the
    same on every interpreter this code runs on.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _detail_running_sec(payload: Any) -> Optional[int]:
    """How long the plan's run has been going, in seconds.

    ``None`` when the run's age is unknown: "the run's age is unknown"
    and "the run's age is zero" are different statements, and only the
    first one is true when the run never started.

    The stamps are read off the plan status' **execution** block, and
    off the summary's own execution block when the first one carries
    none — the status carries the run's stamps in two places, and the
    one that has them wins.

    A run that has not ended yet is measured against the current time.
    A duration that comes out negative (a stamp from the future, or a
    clock that moved backwards) is clamped to zero: a negative number
    of seconds is not a fact about a run.
    """
    if not isinstance(payload, Mapping):
        return None

    blocks: List[Mapping] = []
    execution = payload.get("execution")
    if isinstance(execution, Mapping):
        blocks.append(execution)
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        nested = summary.get("execution")
        if isinstance(nested, Mapping):
            blocks.append(nested)

    started_at = ""
    ended_at = ""
    for block in blocks:
        if not started_at:
            started_at = _stamp_text(block.get("started_at"))
        if not ended_at:
            ended_at = _stamp_text(block.get("ended_at"))

    if not started_at:
        return None

    start = _parse_stamp(started_at)
    if start is None:
        return None

    if ended_at:
        end = _parse_stamp(ended_at)
        if end is None:
            return None
    else:
        end = datetime.now(timezone.utc)

    seconds = int((end - start).total_seconds())
    return seconds if seconds > 0 else 0


@router.get("/api/scheduler/plan/{plan_id}/detail")
def get_scheduler_plan_detail(plan_id: str) -> Any:
    """Answer with one plan's progress — the card's own wording.

    The scheduler's read model says **which** plans are queued and
    which are running.  When the operator opens one of them, the panel
    asks a second question — "what is this plan doing right now?" — and
    that answer already exists: it is the Feishu progress card's body,
    the same three sections (where the plan is, how the tasks are
    counted, what is running this moment), worded by the same builders.

    So this endpoint grows no second phrasing of it.  It asks the plan's
    status for where the plan is, asks the card's builder
    (``cards.build_progress_lines``) for the wording, and hands both on.
    A panel and a card that read the same lines cannot disagree about
    what a plan is doing.

    It is a **read**, and it reads one thing only: the routing row's
    phase is the same before the call and after it.  A detail endpoint
    that stamped a phase would be a second writer of the plan's state.

    Two refusals, and they are told apart:

    * a **malformed** id is a bad request — the shared guard's own 400,
      left alone;
    * a plan that is **not there** is a 404 that says so.

    A refusal of the third kind — this instance may not say whether the
    plan exists — travels on unchanged: it is the plans route's answer
    about **its** data plane, and re-wording it here would replace a
    true statement with a vaguer one.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)

    status = _routes_plans.get_plan_status(plan_id)

    if isinstance(status, JSONResponse):
        # The plans route refused.  A 404 becomes this route's own
        # answer; anything else travels on as it came.
        if status.status_code == 404:
            raise HTTPException(404, _PLAN_NOT_FOUND_DETAIL)
        return status

    payload = dict(status) if isinstance(status, Mapping) else {}

    phase = str(payload.get("current_phase") or payload.get("phase") or "")
    phase_label = _phase_label_for(phase)
    running_sec = _detail_running_sec(payload)

    # The answer carries the card's facts in **both** shapes: the
    # ``lines`` a renderer prints, and the ``sections`` a panel draws
    # (a status header with a colour and an elapsed time, a phase
    # banner, a progress bar, the running tasks).  Both are the card's
    # own builders' output, so the panel and the card cannot disagree
    # about the same plan.
    #
    # The phase's label is passed to the sections' builder **explicitly**
    # (:func:`_phase_label_for`, the shared table's): the builder's own
    # table spells some phases differently, and a response that carried
    # one spelling at the top level and another in the status section
    # would make the panel guess which one to draw.
    #
    # The route's docstring is the published HTTP contract's (the
    # OpenAPI baseline freezes it), so the new keys are described here.
    return {
        "plan_id": plan_id,
        "phase": phase,
        "phase_label": phase_label,
        "running_sec": running_sec,
        "sections": _cards.build_progress_sections(
            payload, running_sec, phase_label,
        ),
        "lines": _cards.build_progress_lines(payload)["lines"],
    }


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


# ---------------------------------------------------------------------------
# The wait actions
# ---------------------------------------------------------------------------
#
# The finishing stage's waits are the panel's "waiting" rows, and every
# row carries two actions: **extend** the deadline, or **terminate** the
# wait.  Both are changes to a plan's own state, and both are made
# through the one lifecycle that owns a wait
# (:class:`postprocess.waits.WaitRegistry`) — this module does not move
# a deadline by itself, and it keeps no second copy of a wait anywhere.
#
# Where a wait lives
# ------------------
# In the plan's own progress file, under the waiting item's ``wait``
# field (architecture decision point 5).  A week-long wait outlives the
# process that created it, so the endpoint reads the durable record and
# puts it back into a registry before it may change one of them.
#
# The answers
# ----------
# * **200** — the change was carried out;
# * **400** — the item id is not a name: it carries a path separator, or
#   it is a traversal;
# * **404** — there is no such waiting item;
# * **409** — the wait is not pending any more, or the change is being
#   made right now by somebody else;
# * **422** — the number of seconds asked for is not a number of
#   seconds.
#
# The 403 is not on that list on purpose: the request guard is
# middleware over every ``/api/`` path, so a request without the
# header, a request from a foreign origin and a request with a forged
# host are refused before this code runs at all.


#: The longest extension one request may ask for, in seconds.
#:
#: Thirty days.  A wait is due in a week, so an extension is normally an
#: hour or a day; thirty days is the point where "push the deadline out"
#: stops being an extension and becomes a new wait — and a request that
#: asks for a year is a request that does not know what it is asking
#: for.
_MAX_EXTEND_SECONDS: int = 30 * 24 * 60 * 60

#: Who the audit entry names.
#:
#: There is no identity to read: the API is loopback-only and every
#: request carries the guard header, so the operator at the keyboard is
#: the operator.  The audit entry says so, because an audit row that
#: says nobody did it is worse than no audit row.
_WAIT_OPERATOR: str = "operator"

#: How long a request waits for the lock that serialises the changes to
#: one plan's waits.
_WAIT_LOCK_TIMEOUT: float = 10.0


class ExtendWaitRequest(BaseModel):
    """The body of ``POST /api/scheduler/{plan_id}/waits/{item_id}/extend``.

    The number of seconds is declared ``Any`` on purpose.  A typed
    ``int`` would let pydantic *coerce* ``"3600"`` into ``3600``, and a
    request that says ``"3600"`` is not a request that says a number of
    seconds — it is a request that says a string, and the answer to it
    is a refusal, not a guess.  The capacity knob above declares its
    number the same way, for the same reason.
    """

    seconds: Any


class _RebuiltRegistry(WaitRegistry):
    """A registry put back together from the records on disk.

    The registry owns the wait lifecycle: it is the only thing that may
    move a deadline, append an audit entry or give up on a wait.  It
    keeps its waits in memory, though, and a wait outlives the process
    that created it — a week-long wait does.  So the endpoint has to put
    the durable records back into a registry before it may change one of
    them.

    ``_persist`` is overridden so the rebuild itself writes nothing: the
    records are already on disk, and writing them again would be a
    second history of the same wait.  Once the rebuild is over the
    registry writes as it always does — that is the whole point of it.
    """

    def __init__(self, plan_id: str, **kwargs: Any) -> None:
        super().__init__(plan_id, **kwargs)
        self.rebuilt: Dict[str, Any] = {}
        self._rebuilding = True

    def _persist(self, wait: Any) -> None:
        """Keep the wait while rebuilding, write it afterwards."""
        if self._rebuilding:
            self.rebuilt[wait.item_id] = wait
            return
        super()._persist(wait)

    def finish_rebuild(self) -> None:
        """The records are back in place: from here on, changes are written."""
        self._rebuilding = False

    def set_deadline(
        self,
        item_id: str,
        deadline: datetime,
        *,
        operator: str,
    ) -> Dict[str, Any]:
        """Put one wait's deadline where the adjustment puts it.

        The registry's own verb for a deadline is ``extend_deadline``,
        which **adds** to it — and adding is not what a dynamic
        adjustment is.  The panel states how much time is left, and
        the deadline becomes that: a wait that is due in a week may be
        cut to two days, or pushed out to a month.  So the deadline is
        **set** here, and the change is written down in the same audit
        shape an extension leaves behind — who changed it, when, and
        what it was before and after.  An audit row that said nothing
        about a deadline that moved would be worse than no audit row.

        Args:
            item_id: The item the wait belongs to.
            deadline: The moment the wait is now due.
            operator: Who asked for the change — the audit entry names
                them.

        Returns:
            The wait record, carrying the new deadline.

        Raises:
            WaitError: When the wait is unknown, or the operator is not
                named.
            WaitConflict: When the wait is not pending any more.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            raise WaitConflict(
                f"等待条目 {item_id} 当前状态为 {wait.status}，"
                "只有 pending 可以调整截止时间"
            )

        who = self._checked_operator(operator, item_id)
        previous = wait.deadline
        wait.deadline = deadline
        wait.extensions.append(
            {
                "operator": who,
                "when": self._now().isoformat(),
                "from": previous.isoformat(),
                "to": deadline.isoformat(),
            }
        )
        self._persist(wait)
        return wait.to_record()


def _checked_item_id(item_id: str) -> str:
    """The item id as a name, or a refusal.

    An item id names a row of the plan's own progress file, so it is a
    **name** and not a path: a separator or a ``..`` in it is refused
    with a 400 rather than answered with whatever it points at.  The
    rule is the one the requirement book itself applies to an item id
    (:mod:`postprocess.spec`).
    """
    if not isinstance(item_id, str) or not item_id.strip():
        raise HTTPException(
            400, {"error": "invalid_item_id", "detail": "the item id is empty"}
        )
    if "/" in item_id or "\\" in item_id or ".." in item_id:
        raise HTTPException(
            400,
            {
                "error": "invalid_item_id",
                "detail": (
                    "the item id names a row of the plan's progress file; "
                    "it may not carry a path separator or '..'"
                ),
            },
        )
    return item_id


def _checked_seconds(seconds: Any) -> int:
    """The number of seconds one extension may ask for.

    A refusal (422) rather than a guess.  A negative number, a
    fraction, a string, a boolean and a number too large to be an
    extension are five different mistakes, and none of them may be
    rounded into an answer: an extension that was not asked for is a
    wait that is longer than the operator agreed to.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise HTTPException(
            422,
            {
                "error": "invalid_seconds",
                "detail": (
                    f"the number of seconds must be a whole number, got "
                    f"{seconds!r}"
                ),
            },
        )
    if seconds <= 0:
        raise HTTPException(
            422,
            {
                "error": "invalid_seconds",
                "detail": (
                    f"the number of seconds must be positive, got {seconds}"
                ),
            },
        )
    if seconds > _MAX_EXTEND_SECONDS:
        raise HTTPException(
            422,
            {
                "error": "invalid_seconds",
                "detail": (
                    f"the number of seconds must not be more than "
                    f"{_MAX_EXTEND_SECONDS}, got {seconds}"
                ),
            },
        )
    return seconds


def _wait_records(plan_id: str) -> Dict[str, Dict[str, Any]]:
    """Every wait record of one plan, as the progress file holds them.

    The records live in the plan's own progress file, under each item's
    ``wait`` field — the durable place the finishing stage keeps them.

    Raises:
        HTTPException: 500 when the file cannot be read as a progress
            file.  A damaged file is reported, never read as an empty
            plan: an empty plan means "nothing is waiting", and the
            panel would then show a plan with nothing wrong with it.
    """
    try:
        progress = load_progress(plan_id)
    except ProgressError as exc:
        raise HTTPException(
            500,
            {"error": "progress_unreadable", "detail": str(exc)},
        ) from exc

    records: Dict[str, Dict[str, Any]] = {}
    items = progress.get("items")
    if not isinstance(items, Mapping):
        return records
    for item_id, item in items.items():
        if not isinstance(item, Mapping):
            continue
        record = item.get("wait")
        if isinstance(record, Mapping):
            records[str(item_id)] = dict(record)
    return records


def _status_of(record: Mapping[str, Any]) -> str:
    """The status a wait record carries.

    A record without a status is read as a live wait: the registry
    always writes one, and a record without one is not a settled wait.
    """
    status = record.get("status")
    if not isinstance(status, str) or not status.strip():
        return PENDING
    return status.strip()


def _registry_of(
    plan_id: str,
    records: Mapping[str, Mapping[str, Any]],
    *,
    store: Any = None,
) -> _RebuiltRegistry:
    """The registry of one plan, rebuilt from its durable records.

    A record the lifecycle cannot speak about — a kind of wait outside
    its two-word vocabulary, a deadline that is not a moment — is left
    out: the endpoint then answers "there is no such waiting item",
    which is true of a record the lifecycle does not know.
    """
    registry = _RebuiltRegistry(plan_id, store=store)

    for item_id, record in records.items():
        wait_type = record.get("wait_type")
        if wait_type not in WAIT_TYPES:
            continue
        deadline = record.get("deadline")
        if not isinstance(deadline, str):
            continue
        try:
            moment = datetime.fromisoformat(deadline)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        registry.create_wait(
            {"id": item_id, "wait": {"type": wait_type}},
            question=str(record.get("question") or ""),
        )
        wait = registry.rebuilt.get(item_id)
        if wait is None:
            continue
        wait.deadline = moment
        wait.extensions = [
            dict(entry)
            for entry in (record.get("extensions") or [])
            if isinstance(entry, Mapping)
        ]
        wait.status = _status_of(record)

    registry.finish_rebuild()
    return registry


@contextmanager
def _wait_lock(plan_id: str) -> Iterator[None]:
    """The lock that serialises the changes to one plan's waits.

    The read-modify-write of a deadline is not atomic: the deadline is
    read, moved and written back, and two requests doing that at the
    same time would both read the same deadline — one of the two
    extensions would be lost, and the audit would name an extension the
    deadline does not carry.

    The lock lives in the plan's own ``locks/`` directory, which is
    where the repository's lock protocol puts a plan's runtime state.
    """
    plan_dir = progress_path(plan_id).parent
    locks_dir = locks_dir_for_plan(plan_dir)
    try:
        locks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            500,
            {"error": "lock_unavailable", "detail": str(exc)},
        ) from exc

    lock = FileLock(
        str(locks_dir / "postprocess_waits.lock"), timeout=_WAIT_LOCK_TIMEOUT
    )
    try:
        lock.acquire()
    except Timeout as exc:
        raise HTTPException(
            409,
            {
                "error": "wait_busy",
                "detail": (
                    "another change to this plan's waits is being made right "
                    "now; try again once it is done"
                ),
            },
        ) from exc
    try:
        yield
    finally:
        lock.release()


@contextmanager
def _hitl_store() -> Iterator[Any]:
    """The HITL store, opened for this call and closed after it.

    A terminated wait settles its question: a question that stayed open
    after the operator gave up on it would still accept an answer, and
    that answer would land as if it were asked for.

    A store that cannot be opened is a warning and not a refusal: the
    operator asked to give up on a wait, and that must happen even if
    the question it asked cannot be settled right now.
    """
    try:
        store = HitlStore(resolve_state_db_path())
    except Exception:  # noqa: BLE001 — the wait is terminated either way
        logger.warning(
            "the HITL store could not be opened; the wait's question is "
            "left open",
            exc_info=True,
        )
        yield None
        return
    try:
        yield store
    finally:
        store.close()


def plan_waiting_rows(plan_id: str) -> List[Dict[str, Any]]:
    """The panel's waiting rows for one plan.

    The rows are built by the production read model
    (:func:`plan_scheduler.snapshot.build`) out of the plan's own
    durable wait records — the records the panel's ``waiting[]`` list is
    built from — so the row and the record cannot tell two different
    stories.

    Only the waits that are still **pending** are listed.  A wait that
    was answered or given up on is not waiting any more, and a row for
    it would say "this is waiting" about something that is over.
    """
    rows = [
        {**record, "plan_id": plan_id}
        for record in _wait_records(plan_id).values()
        if _status_of(record) == PENDING
    ]
    snapshot = build({"waits": rows})
    return list(snapshot["waiting"])


# ---------------------------------------------------------------------------
# The waiting rows in the read model
# ---------------------------------------------------------------------------
#
# The engine's snapshot answers "which plans are queued for a slot".
# The panel's waiting block answers a different question — "what is the
# finishing stage waiting for" — and the answer lives in the durable
# wait records of the plans themselves.  These three helpers put that
# answer into the read model the panel polls, so the panel reads one
# document and does not go looking for a second one.


def _plans_with_progress() -> List[str]:
    """The plans that carry a finishing-stage progress file.

    The plans root is read the way every other reader of it reads it —
    through :func:`config_paths.resolve_plans_dir` — so the panel and
    the stage always look at the same tree.

    A directory that does not hold a progress file is not a plan the
    finishing stage knows about: it is left out rather than read and
    found empty.

    A plans root that is not there at all is not a failure either.  The
    panel polls this endpoint every few seconds, so a warning (and the
    traceback that comes with it) per poll would fill the log with a
    fact nobody asked for -- and would keep the process's captured log
    records growing for as long as the panel is open.
    """
    root = resolve_plans_dir()
    if not root.is_dir():
        return []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        logger.warning("the plans root could not be read", exc_info=True)
        return []

    plan_ids: List[str] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if not (entry / PROGRESS_FILENAME).is_file():
            continue
        plan_ids.append(entry.name)
    return plan_ids


def _pending_waiting_rows() -> List[Dict[str, Any]]:
    """Every plan's pending waiting rows, as the panel reads them.

    The rows come from :func:`plan_waiting_rows` — the production read
    model over the durable wait records — so the row and the record
    cannot tell two different stories.

    A plan whose progress file cannot be read is left out and reported:
    the panel must keep answering, and a plan it cannot read is not a
    plan it may claim is waiting.
    """
    rows: List[Dict[str, Any]] = []
    for plan_id in _plans_with_progress():
        try:
            rows.extend(plan_waiting_rows(plan_id))
        except (HTTPException, ValueError):
            logger.warning(
                "the waiting rows of plan %s could not be read",
                plan_id,
                exc_info=True,
            )
            continue
    return rows


def _state_with_waiting(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """The read model with the finishing stage's waits in it.

    The rows the engine already carries are kept: they are the same
    rows, read from the engine's own state, and a row that is in both
    places is one row.  A row is identified by its plan and its item —
    the pair the panel draws the row under and the pair the three
    actions address.
    """
    document = dict(snapshot)
    rows: List[Dict[str, Any]] = [
        dict(row)
        for row in (document.get("waiting") or [])
        if isinstance(row, Mapping)
    ]
    known = {(str(row.get("plan_id")), str(row.get("item_id"))) for row in rows}

    for row in _pending_waiting_rows():
        key = (str(row.get("plan_id")), str(row.get("item_id")))
        if key in known:
            continue
        known.add(key)
        rows.append(row)

    document["waiting"] = rows
    return document


def _row_of(plan_id: str, item_id: str) -> Optional[Dict[str, Any]]:
    """The panel's row for one waiting item, or ``None``."""
    for row in plan_waiting_rows(plan_id):
        if row.get("item_id") == item_id:
            return row
    return None


@router.post("/api/scheduler/{plan_id}/waits/{item_id:path}/extend")
def extend_wait(plan_id: str, item_id: str, body: ExtendWaitRequest) -> JSONResponse:
    """Push one waiting item's deadline out.

    The extension is the operator saying "this may take longer", and the
    answer says how much longer: the seconds that are left, and how many
    times the deadline has been pushed out.  Both are read out of the
    panel's own row, so the answer and the panel cannot disagree.

    A wait that is over cannot be extended.  That is a 409 and not a
    404: the item is known, and the wait is simply not pending any
    more.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)

    seconds = _checked_seconds(body.seconds)
    item = _checked_item_id(item_id)

    with _wait_lock(plan_id):
        records = _wait_records(plan_id)
        record = records.get(item)
        if record is None:
            raise HTTPException(
                404,
                {
                    "error": "no_such_wait",
                    "detail": f"there is no waiting item {item!r} in this plan",
                },
            )
        if _status_of(record) != PENDING:
            raise HTTPException(
                409,
                {
                    "error": "wait_not_pending",
                    "detail": (
                        f"the wait {item!r} is {record.get('status')!r}, and "
                        "only a pending wait can be extended"
                    ),
                },
            )

        registry = _registry_of(plan_id, records)
        registry.extend_deadline(item, seconds, operator=_WAIT_OPERATOR)

    row = _row_of(plan_id, item)
    if row is None:
        # Unreachable in practice: the wait was pending a moment ago and
        # an extension does not settle it.  It is answered rather than
        # left open, so that a future change cannot turn this into a
        # route that answers 200 without an answer in it.
        raise HTTPException(
            500,
            {
                "error": "wait_row_missing",
                "detail": f"the wait {item!r} has no row in the read model",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "plan_id": row["plan_id"],
            "item_id": row["item_id"],
            "remaining_seconds": row["remaining_seconds"],
            "extensions_count": row["extensions_count"],
        },
    )


@router.post("/api/scheduler/{plan_id}/waits/{item_id:path}/terminate")
def terminate_wait(plan_id: str, item_id: str) -> JSONResponse:
    """Give up on one waiting item.

    The wait is over: it leaves the panel's waiting list, the question
    it asked is settled, and an answer that arrives afterwards is
    refused rather than accepted.

    A wait that is over cannot be given up on twice.  That is a 409: the
    operator changed their mind, and the endpoint does not — a second
    termination would leave two of them in the audit for one wait.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)

    item = _checked_item_id(item_id)

    with _wait_lock(plan_id):
        records = _wait_records(plan_id)
        record = records.get(item)
        if record is None:
            raise HTTPException(
                404,
                {
                    "error": "no_such_wait",
                    "detail": f"there is no waiting item {item!r} in this plan",
                },
            )
        if _status_of(record) != PENDING:
            raise HTTPException(
                409,
                {
                    "error": "wait_not_pending",
                    "detail": (
                        f"the wait {item!r} is {record.get('status')!r}, and "
                        "only a pending wait can be terminated"
                    ),
                },
            )

        with _hitl_store() as store:
            registry = _registry_of(plan_id, records, store=store)
            registry.terminate(item, operator=_WAIT_OPERATOR)

    return JSONResponse(
        status_code=200,
        content={
            "plan_id": plan_id,
            "item_id": item,
            "status": "terminated",
        },
    )


# ---------------------------------------------------------------------------
# The requirement book, and the deadline the panel adjusts
# ---------------------------------------------------------------------------
#
# The finishing stage judges a plan against a **requirement book** —
# ``plans/{id}/postprocess.json`` — and the operator panel needs two
# ways into it: read the book and write it back, and put a waiting
# item's deadline where the operator says it should be.
#
# The book is written by one file and one file only
# -------------------------------------------------
# The plan's own directory holds more than the book: the progress file
# of a round that is running, the free-form companion, the plan's own
# tasks.  Every one of them belongs to somebody else, and the
# architecture gives the book exactly one file (decision point 3).  A
# write that reached past that file would change the criteria of a
# round that is already running — so the write-back is limited to
# ``postprocess.json``, and a book the loader would refuse is not
# written at all.
#
# An edit during a round belongs to the next round
# -------------------------------------------------
# The stage freezes the book when it starts: this round's criteria are
# the book as it stood at that moment, and the fingerprint the progress
# file carries does not move because a file next to it changed.  The
# next freeze reads the new content.
#
# The deadline
# ------------
# The deadline is **one field**, and the panel has two verbs for it:
# ``extend`` pushes it out, and the adjustment below puts it where the
# operator says it should be.  Both write the same field of the same
# durable record, both are refused for a wait that is not pending any
# more (409), and both refuse a number of seconds that is not a number
# of seconds (422).
#
# Why the plan id is carried whole
# --------------------------------
# The routes below take the plan id as a **path** segment, so that a
# malformed id reaches the handler and is refused by the shared guard
# with its own 400 — a bad request, not a missing endpoint.  A route
# that could not carry the id would answer 404, and "there is no such
# endpoint" is the wrong sentence for "that is not a plan id".


#: The longest remaining time one adjustment may ask for, in seconds.
#:
#: The same thirty days an extension is capped at.  A wait is due in a
#: week, so an adjustment is normally an hour or a day; thirty days is
#: the point where "give this wait more time" stops being an adjustment
#: and becomes a new wait.
_MAX_REMAINING_SECONDS: int = _MAX_EXTEND_SECONDS


class SetDeadlineRequest(BaseModel):
    """The body of ``PATCH /api/scheduler/{plan_id}/waits/{item_id}/deadline``.

    The number of seconds is declared ``Any`` on purpose, for the same
    reason the extension's is: a typed ``int`` would let pydantic
    *coerce* ``"3600"`` into ``3600``, and a request that says
    ``"3600"`` is not a request that says a number of seconds — it is a
    request that says a string, and the answer to it is a refusal, not
    a guess.
    """

    remaining_seconds: Any


def _checked_remaining_seconds(value: Any) -> int:
    """The number of seconds one adjustment may ask for.

    A refusal (422) rather than a guess.  A negative number, a
    fraction, a string, a boolean and a number too large to be a wait
    are five different mistakes, and none of them may be rounded into
    an answer: a deadline that was not asked for is a wait that lasts
    longer than the operator agreed to.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            422,
            {
                "error": "invalid_remaining_seconds",
                "detail": (
                    f"the number of seconds must be a whole number, got "
                    f"{value!r}"
                ),
            },
        )
    if value <= 0:
        raise HTTPException(
            422,
            {
                "error": "invalid_remaining_seconds",
                "detail": (
                    f"the number of seconds must be positive, got {value}"
                ),
            },
        )
    if value > _MAX_REMAINING_SECONDS:
        raise HTTPException(
            422,
            {
                "error": "invalid_remaining_seconds",
                "detail": (
                    f"the number of seconds must not be more than "
                    f"{_MAX_REMAINING_SECONDS}, got {value}"
                ),
            },
        )
    return value


def _checked_spec(document: Any) -> Spec:
    """The requirement book as it stands, or a refusal.

    The book is judged by the loader the finishing stage itself reads
    (:func:`postprocess.spec.load_spec`), so an edit the stage would
    refuse is refused here too — and the refusal is the loader's own
    message, which names the entry that is wrong.  A book has many
    entries and the loader rejects exactly one of them at a time, so
    "the book is illegal" would tell the author nothing.

    The candidate is loaded from a **temporary directory**, so a book
    that is refused never reaches the plan's own file: the file on
    disk is still the book the running round froze.

    Args:
        document: The book as the request carried it.

    Returns:
        The loaded book — its entries, validated.

    Raises:
        HTTPException: 422 when the document is not a legal
            requirement book.
    """
    with tempfile.TemporaryDirectory(prefix="pdt-spec-check-") as scratch:
        candidate = Path(scratch) / SPEC_FILENAME
        candidate.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        try:
            return load_spec(scratch)
        except SpecError as exc:
            raise HTTPException(
                422,
                {"error": "invalid_spec", "detail": str(exc)},
            ) from exc


def _write_spec(path: Path, document: Any) -> None:
    """Write the requirement book, atomically.

    The book goes to a temporary file **in the plan's own directory**
    and is then renamed over the target with ``os.replace``.  Two
    things follow from that, and both are the point: a reader sees
    either the previous book or the new one — never half of one — and
    a write that fails leaves the previous book in place, complete and
    parseable.

    Args:
        path: The book's own path.
        document: The book, as it is to be written.
    """
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # The rename never happened: the previous book is untouched.
        # The temporary file is best-effort cleanup — its absence must
        # not mask the failure that got us here.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


@router.get("/api/scheduler/{plan_id:path}/postprocess-spec")
def get_postprocess_spec(plan_id: str) -> JSONResponse:
    """Show one plan's requirement book.

    The book is shown **as it is**: the editor is how an illegal book
    gets fixed, so the answer is the file's own content — not a
    prettified copy of it, and not the criteria the loader happens to
    understand.  A plan with no book answers 404: there is nothing to
    show, and that is not a failure of the server.
    """
    path = _server._plan_dir(plan_id) / SPEC_FILENAME

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(
            404,
            {
                "error": "no_such_spec",
                "detail": f"the plan {plan_id!r} has no requirement book",
            },
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(
            500,
            {"error": "spec_unreadable", "detail": str(exc)},
        ) from exc

    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            500,
            {
                "error": "spec_unreadable",
                "detail": f"the requirement book is not valid JSON: {exc}",
            },
        ) from exc

    return JSONResponse(status_code=200, content=document)


@router.put("/api/scheduler/{plan_id:path}/postprocess-spec")
def put_postprocess_spec(plan_id: str, body: Any = Body(...)) -> JSONResponse:
    """Write one plan's requirement book back.

    The book is judged by the loader the finishing stage itself uses, so
    an edit the stage would refuse is refused here — and a refused edit
    writes nothing, which leaves the book on disk exactly as the
    running round froze it.

    The write is limited to **one file**: ``postprocess.json``.  The
    progress file of a running round, the free-form companion and the
    plan's own tasks are none of this endpoint's business, and the
    write-back does not touch them.

    The answer carries the fingerprint of the book that was written —
    the version the **next** round will freeze.  It says nothing about
    the round that is running: that one kept the criteria it started
    with.
    """
    plan_path = _server._plan_dir(plan_id)

    spec = _checked_spec(body)
    _write_spec(plan_path / SPEC_FILENAME, body)

    frozen = freeze(spec)
    return JSONResponse(
        status_code=200,
        content={
            "plan_id": plan_id,
            "spec_sha256": frozen.spec_sha256,
            "items": len(frozen.spec.items),
        },
    )


@router.patch("/api/scheduler/{plan_id:path}/waits/{item_id:path}/deadline")
def set_wait_deadline(
    plan_id: str, item_id: str, body: SetDeadlineRequest
) -> JSONResponse:
    """Put one waiting item's deadline where the operator says.

    The extension pushes the deadline **out**; this puts it where the
    operator says it should be — a wait that is due in a week may be cut
    to two days, or pushed out to a month.  Both are the same field of
    the same durable record, so the second one starts from what the
    first one wrote.

    The answer says the deadline that was written down and how much
    time is left on it, measured from the moment the deadline was set.

    A wait that is over cannot have its deadline moved.  That is a 409
    and not a 404: the item is known, and the wait is simply not
    pending any more.
    """
    # The id is admitted through the shared guard: ``_plan_dir`` is the
    # one place a request-supplied id may become a path, and it refuses
    # a traversal with 400.  That answer is the guard's own and it is
    # left alone — a malformed id is a bad request, not a missing plan.
    _server._plan_dir(plan_id)

    seconds = _checked_remaining_seconds(body.remaining_seconds)
    item = _checked_item_id(item_id)

    with _wait_lock(plan_id):
        records = _wait_records(plan_id)
        registry = _registry_of(plan_id, records)
        # The registry is the lifecycle's own view of the plan, and it
        # leaves out a record it cannot speak about — a kind of wait
        # outside its vocabulary, a deadline that is not a moment.  Such
        # a record is not a waiting item, so it is answered the way an
        # item that was never created is answered.
        try:
            record = registry.get(item)
        except WaitError:
            raise HTTPException(
                404,
                {
                    "error": "no_such_wait",
                    "detail": f"there is no waiting item {item!r} in this plan",
                },
            ) from None
        if record["status"] != PENDING:
            raise HTTPException(
                409,
                {
                    "error": "wait_not_pending",
                    "detail": (
                        f"the wait {item!r} is {record['status']!r}, and "
                        "only a pending wait can have its deadline moved"
                    ),
                },
            )

        # One moment, one deadline: the time left on the answer is the
        # number of seconds that were asked for, not a second reading
        # of a clock that has moved on since.
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=seconds)
        registry.set_deadline(item, deadline, operator=_WAIT_OPERATOR)

    return JSONResponse(
        status_code=200,
        content={
            "item_id": item,
            "deadline": deadline.isoformat(),
            "remaining_seconds": seconds,
        },
    )