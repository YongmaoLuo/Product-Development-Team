"""One plan, one status.

Why this module exists
======================

Every "the card says X but the plan is doing Y" bug in this codebase has
the same shape: *the same fact is stored in several places, and the
reader re-derives it from whichever subset it happened to fetch.*

On 2026-09-23 a production plan had six answers
to "where is it now", all live at the same instant:

===========================  ====================================
store                        answer
===========================  ====================================
``plan_routing.current_phase``          ``completed``
``plan_routing.verification.status``    ``failed``
``plan_verification.verification_status``  ``running``
``plan_tasks``                          a ``repair-r*`` row in progress
``/api/system/active``                  ``execution`` running
the Feishu card header                  "⏸ 暂停（上游阻塞）"
===========================  ====================================

The truth was the fifth one. Nothing was broken about any single store —
they had simply been written by different code paths at different
moments, and the card assembled its verdict by fetching three endpoints
at three instants and then re-deriving an answer from loose fields.

What this module changes
------------------------

``PlanStatus`` is the one object every reader renders from. It is built
once, from one read, by :func:`build_plan_status`. Consumers (the card
header, the operator API, the CLI) become pure functions of it, so
"the card disagrees with the API" stops being expressible.

It also carries :class:`Divergence` — the specific disagreements found
while assembling the snapshot. This matters because every state write in
this codebase is deliberately best-effort (``except Exception: pass``, so
that a failed state write never breaks the workflow). That design choice
is right, but it means a divergence produces *no signal at all*: the
0921 card lied for 80 minutes and the server log recorded nothing. The
compensating control is to check a few invariants where all the stores
are visible at once, and say so.

Purity
------

:func:`find_divergences` is pure — it takes a :class:`PlanStatus` and
returns findings. ``build_plan_status`` (in ``server.py``, which owns the
in-memory execution / verification records) does the I/O and calls it.
That split is what makes the invariants testable without a database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, NamedTuple, Optional, Tuple


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Phases that assert "this workflow is finished and nothing will resume
#: it". ``failed`` is deliberately absent: a watchdog stamp can land while
#: a repair execution is still draining, and that transient is handled by
#: the verification sub-machine rather than being a divergence.
FINISHED_PHASES = (
    "completed",
    "verification_passed",
    "verification_loop_stopped",
    "stopped",
)

#: The one phase in ``plan_state.VALID_PHASES`` whose truth depends on a
#: LIVE EXECUTION rather than on the plan's artefacts on disk.
#:
#: Every other phase ("interview", "prd_review", "ready", "completed", …)
#: says what the plan IS; this one says what is HAPPENING.  That makes it
#: the only phase the execution store can refute, and the only one a reader
#: has to check against it before repeating it as an answer.
#:
#: ``verification_running`` is a near neighbour and is deliberately NOT
#: here: a verification round keeps its liveness in ``plan_verification`` /
#: ``_verification_state``, never in ``plan_execution``, so the absence of
#: an execution row says nothing about whether it is running.
RUNTIME_PHASE = "executing"


#: The task counts every reader of a plan status asks for: how much work
#: the plan is, how much of it is running, how much is still waiting and
#: how much failed.  A status payload that omits one of them answers
#: "where is this plan" with a question mark, so these four are the
#: counts that must never be missing.
REQUIRED_TASK_COUNTS = ("total", "in_progress", "pending", "failed")


def ensure_task_counts(counts: Any) -> Dict[str, int]:
    """``counts`` with the four required counts always present as ints.

    Pure, like the rest of this module.  Two things are guaranteed
    about the result:

    * every key in :data:`REQUIRED_TASK_COUNTS` is present, and is an
      ``int`` — a missing count is reported as ``0`` rather than as a
      key a consumer has to defend against;
    * every key that WAS present is still there, coerced to an ``int``
      when it can be.  A count that cannot be read as a number becomes
      ``0`` instead of raising: a status read must degrade, not fail.

    The point is the difference between "this plan has no pending work"
    and "this plan's pending count could not be read".  The first is a
    fact; the second is a gap, and a consumer that cannot tell them
    apart is exactly the consumer that guesses wrong.
    """
    source: Mapping[str, Any] = counts if isinstance(counts, Mapping) else {}
    result: Dict[str, int] = {}
    for key, value in source.items():
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            result[str(key)] = 0
    for key in REQUIRED_TASK_COUNTS:
        result.setdefault(key, 0)
    return result


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


# ---------------------------------------------------------------------------
# Where the phase comes from
# ---------------------------------------------------------------------------
#
# The phase a reader is shown used to be whatever ``_build_plan_status``
# happened to be holding: the routing row's ``current_phase`` when the
# row could be read, and nothing else when it could not.  That is a
# default living in the implementation — nobody can point at it, and
# the one default that must never exist is the *parked* one, because it
# is global: every plan in the system would claim to be waiting for
# capacity, and the operator would be sent after a wall that only one
# plan ever hit.
#
# So the rule is a function, here, and it says which tier answered.

#: The resting phase: "tasks are generated, a human decides when to
#: start".  It is the answer for a plan whose phase cannot be read —
#: the one answer that is never a lie about capacity.
DEFAULT_PHASE = "ready"

#: The parked-for-capacity phase.
QUOTA_WAIT_PHASE = "waiting_for_quota"

#: The phase came from the routing row's own ``current_phase``.
PHASE_SOURCE_ROUTING = "routing"

#: The phase came from the durable execution row: the plan is parked
#: for capacity and the routing row could not be read.
PHASE_SOURCE_EXECUTION = "execution"

#: The phase was not read from a record — it is the resting fallback.
PHASE_SOURCE_DEFAULT = "default"

#: The fields the executor's capacity marker carries
#: (``agent._stop_reason_for_quota_wait``): which providers had no
#: capacity, whether a human has to top up, how long the run waited and
#: when it gave up.  A detail that parses into an object with none of
#: these is not a capacity fact.
_CAPACITY_MARKER_KEYS = frozenset({
    "providers", "needs_manual_topup", "waited_sec", "at",
})


class DerivedPhase(NamedTuple):
    """A phase and the tier that produced it.

    ``source`` is what makes a fallback visible: a reader that cannot
    tell a recorded phase from a resting default has to guess, and the
    guess is where "the card says X but the plan is doing Y" comes
    from.
    """

    phase: str
    source: str


def _capacity_marker(tasks_payload: Any) -> bool:
    """Does ``tasks.json`` carry a capacity marker that parses?

    The marker is written by the executor when a run parks because
    every provider had no capacity left.  It lives in the *top level*
    of ``tasks.json`` as ``reason_detail`` — a JSON string holding the
    four fields of :data:`_CAPACITY_MARKER_KEYS`.

    False for every shape that is not a capacity fact: no file, no
    ``reason_detail``, a detail that is not JSON, a detail that is not
    an object, an empty object, or an object carrying something else
    entirely (the field is a free-form "why did this stop" slot).
    """
    if not isinstance(tasks_payload, Mapping):
        return False
    raw = tasks_payload.get("reason_detail")
    detail: Any = raw
    if isinstance(raw, str):
        if not raw.strip():
            return False
        try:
            detail = json.loads(raw)
        except (ValueError, TypeError):
            return False
    if not isinstance(detail, Mapping):
        return False
    return any(key in detail for key in _CAPACITY_MARKER_KEYS)


def _execution_row_is_parked(execution_row: Any) -> bool:
    """Does the durable execution row's ``stop_reason`` say "parked"?

    ``stop_reason`` is the field that carries *why* the run stopped,
    and it is the only place the parked verdict survives a restart.
    The status column alone does not promote a plan: a row whose own
    reason says something else is not a parked row, however its status
    column reads.
    """
    if not isinstance(execution_row, Mapping):
        return False
    stop_reason = str(execution_row.get("stop_reason") or "").strip()
    return stop_reason == QUOTA_WAIT_PHASE


def derive_phase(
    routing_phase: Optional[str],
    execution_row: Optional[Mapping[str, Any]] = None,
    tasks_payload: Any = None,
) -> DerivedPhase:
    """The plan's phase, and the tier that answered.

    Pure — no I/O, no clock, no environment.  The three inputs are the
    three things a reader can know about a plan's phase, and the
    priority between them is fixed here rather than in whichever route
    happened to read them first::

        routing row  >  durable execution row  >  "ready"

    **Tier 1 — the routing row.**  ``plan_routing.current_phase`` is
    the workflow-phase authority: it is what ``/execution/start`` CASes
    and what every card renders.  When it is there, it is the answer.

    **Tier 2 — the durable execution row.**  Only consulted when the
    routing row could not be read.  It may answer ``waiting_for_quota``
    when **both** halves of the conjunction hold: the row's
    ``stop_reason`` says the run stopped for capacity, *and* the plan's
    ``tasks.json`` carries a capacity marker that parses.  Either half
    alone is not enough — the row says the run stopped, the marker says
    it stopped for capacity.

    **Tier 3 — the resting default.**  Everything else is
    :data:`DEFAULT_PHASE`, the manual gate.  This is where *every*
    missing-information branch lands: a missing routing row, an
    unreadable marker, a malformed marker, a row that says something
    else.  Never on the parked phase.

    The one exception is a plan with **no records at all** — no routing
    row, no execution row, no ``tasks.json``.  There is no plan there
    to sit at the manual gate, so the answer is the empty phase, which
    is what the snapshot has always said for that case (and what the
    endpoint's ``404`` is about).  It is not the parked phase either: a
    plan that does not exist cannot be waiting for capacity.
    """
    routing = str(routing_phase or "").strip()
    if routing:
        return DerivedPhase(routing, PHASE_SOURCE_ROUTING)

    row = execution_row if isinstance(execution_row, Mapping) else None
    if (
        row is not None
        and _execution_row_is_parked(row)
        and _capacity_marker(tasks_payload)
    ):
        return DerivedPhase(QUOTA_WAIT_PHASE, PHASE_SOURCE_EXECUTION)

    if row is None and tasks_payload is None:
        # No plan at all — see the docstring.
        return DerivedPhase("", PHASE_SOURCE_DEFAULT)

    return DerivedPhase(DEFAULT_PHASE, PHASE_SOURCE_DEFAULT)


# ---------------------------------------------------------------------------
# Divergence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """One specific disagreement found while assembling a snapshot.

    ``code`` is stable and greppable; ``detail`` is for a human reading
    the log line. Neither is an exception — a divergence is a *report*.
    """

    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.code}: {self.detail}"


# ---------------------------------------------------------------------------
# PlanStatus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStatus:
    """The rendered-from-this view of one plan.

    Every reader — card header, operator API, CLI — renders from this and
    nothing else, so two readers cannot disagree: they are looking at the
    same object.

    ``execution_in_flight`` / ``verification_in_flight`` come from the
    liveness predicates (``server._is_execution_in_flight`` /
    ``_is_verification_in_flight``), **not** from task counts. Task counts
    are a proxy that cannot tell "a repair round is running" from "the
    server died with a task stuck at ``in_progress``" — that ambiguity is
    what made the 2026-09-23 fix have to be scoped down to one branch.

    .. warning::

       Both liveness fields are only meaningful **in the serving
       process**. They read ``_execution_state`` / ``_verification_state``,
       which are per-process dicts; a CLI, a sweep, or a unit test that
       rebuilds a snapshot locally will always see ``False``. That is not
       a divergence, it is a missing store. Anything that needs a true
       answer must go through ``GET /api/plan/{id}/status`` so the server
       computes it. (An out-of-process sweep
       reported exactly one "divergence" — itself.)
    """

    plan_id: str

    #: ``plan_routing.current_phase`` — the column ``/execution/start``
    #: CASes, and the only workflow-phase authority.
    phase: str = ""

    #: Which tier produced :attr:`phase`: ``"routing"`` (the routing
    #: row's own ``current_phase``), ``"execution"`` (the durable
    #: execution row says the plan is parked for capacity) or
    #: ``"default"`` (the phase was not read from a record — it is the
    #: resting fallback).  See :func:`derive_phase`.
    phase_source: str = ""

    #: ``plan_verification`` row.
    verification_status: str = ""
    verification_round: int = 0
    verification_max_rounds: int = 0
    verification_stop_reason: Optional[str] = None

    execution_in_flight: bool = False
    verification_in_flight: bool = False

    tasks: Mapping[str, int] = field(default_factory=dict)
    current_task: Optional[Mapping[str, Any]] = None
    next_task: Optional[Mapping[str, Any]] = None

    #: What the executor is doing when no task row says — the refiner
    #: rewriting the task list, a layer boundary, the tail after the last
    #: task. ``current_task`` is authoritative whenever it is set; this
    #: covers the windows where it is ``None`` and the card would
    #: otherwise say "executing" without naming anything. Derived from
    #: ``plans/<id>/execution.log`` by
    #: :meth:`execution_logger.ExecutionLogger.current_activity`, so it
    #: is readable from any process — unlike the two liveness flags
    #: above, which are serving-process only.
    current_activity: Optional[Mapping[str, Any]] = None

    #: What did not line up while assembling this snapshot.
    divergences: Tuple[Divergence, ...] = ()

    # -- derived helpers ---------------------------------------------------

    @property
    def consistent(self) -> bool:
        return not self.divergences

    @property
    def finished(self) -> bool:
        """True iff the workflow has a resting phase and nothing is running."""
        return _norm(self.phase) in FINISHED_PHASES and not self.execution_in_flight

    def count(self, key: str) -> int:
        try:
            return int(self.tasks.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def finished_tasks(self) -> int:
        return self.count("completed") + self.count("failed") + self.count("skipped")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "phase": self.phase,
            "phase_source": self.phase_source,
            "verification_status": self.verification_status,
            "verification_round": self.verification_round,
            "verification_max_rounds": self.verification_max_rounds,
            "verification_stop_reason": self.verification_stop_reason,
            "execution_in_flight": self.execution_in_flight,
            "verification_in_flight": self.verification_in_flight,
            "tasks": dict(self.tasks),
            "current_task": dict(self.current_task) if self.current_task else None,
            "next_task": dict(self.next_task) if self.next_task else None,
            "current_activity": (
                dict(self.current_activity) if self.current_activity else None
            ),
            "divergences": [
                {"code": d.code, "detail": d.detail} for d in self.divergences
            ],
        }


def from_payload(payload: Mapping[str, Any]) -> PlanStatus:
    """Rehydrate a :class:`PlanStatus` from ``GET /api/plan/{id}/status``.

    The counterpart to :meth:`PlanStatus.to_dict`, for consumers in
    **another process**: the notifier renders from the server's
    authoritative snapshot instead of reassembling one from three
    endpoints. ``divergences`` is deliberately *not* rehydrated — it is
    the server's report about its own stores, and a consumer that could
    re-derive it would be re-deriving the very thing this module exists
    to stop.

    Tolerant by design: a missing or malformed field degrades to its
    empty value rather than raising, because a card that renders a
    slightly poorer header still beats a card that is not pushed at all.
    """
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    tasks = payload.get("tasks")
    current = payload.get("current_task")
    nxt = payload.get("next_task")
    activity = payload.get("current_activity")
    return PlanStatus(
        plan_id=str(payload.get("plan_id") or ""),
        phase=str(payload.get("phase") or ""),
        phase_source=str(payload.get("phase_source") or ""),
        verification_status=str(payload.get("verification_status") or ""),
        verification_round=_int(payload.get("verification_round")),
        verification_max_rounds=_int(payload.get("verification_max_rounds")),
        verification_stop_reason=payload.get("verification_stop_reason") or None,
        execution_in_flight=bool(payload.get("execution_in_flight")),
        verification_in_flight=bool(payload.get("verification_in_flight")),
        tasks=dict(tasks) if isinstance(tasks, Mapping) else {},
        current_task=dict(current) if isinstance(current, Mapping) else None,
        next_task=dict(nxt) if isinstance(nxt, Mapping) else None,
        current_activity=(
            dict(activity) if isinstance(activity, Mapping) else None
        ),
    )


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def is_runtime_phase(phase: Optional[str]) -> bool:
    """True iff *phase* is the one phase that asserts a live execution.

    See :data:`RUNTIME_PHASE`.
    """
    return _norm(phase) == RUNTIME_PHASE


def resolve_phase(
    phase: Optional[str],
    *,
    execution_recorded: bool,
    fallback: str = "",
) -> str:
    """The phase a reader may report for this plan.

    Pure — no I/O, no clock, no environment.

    ``phase`` is what the routing row says.  ``fallback`` is what the
    reader reports instead when the recorded phase is
    :data:`RUNTIME_PHASE` and nothing is running behind it.

    Why this lives here
    -------------------
    ``executing`` is the one phase in the vocabulary that does not
    describe a plan's ARTEFACTS — it describes a PROCESS: "the executor
    is running this plan right now".  A routing row can hold such a phase
    with nothing running behind it: a plan directory carrying a legacy
    ``plan_state.json`` is imported once by
    ``plan_state.PlanState._load_state``, which copies the JSON's
    ``current_phase`` into ``plan_routing`` verbatim; an executor killed
    before anyone reconciled the row leaves the same shape.

    Two readers report a plan's phase — the listing (``GET /api/plans``)
    and the single-plan snapshot (``GET /api/plan/{id}/status``) — and
    they used to answer that question independently.  One of them had the
    check and the other did not, so the same plan read ``ready`` in one
    answer and ``executing`` in the other: one plan, two answers.  This
    function is the one judgement both call, so there is one answer.

    Like :func:`find_divergences`, the judgement is pure and the I/O
    stays with the caller: reading the execution record needs a database
    and (in the serving process) the in-memory liveness dict, neither of
    which this module may touch.

    ``execution_recorded`` is "does an execution record exist for this
    plan at all": a ``plan_execution`` row OR an in-memory
    ``_execution_state`` entry.  The executor seeds the in-memory record
    before it stamps the phase and persists the row before it spawns the
    subprocess, so a plan that IS running always has one of the two.

    ``verification_running`` is deliberately NOT included: a
    verification round records its liveness in ``plan_verification`` /
    ``_verification_state``, not in ``plan_execution``, so "no execution
    row" says nothing about it.
    """
    if is_runtime_phase(phase) and not execution_recorded:
        return fallback
    return str(phase or "")


def find_divergences(status: PlanStatus) -> Tuple[Divergence, ...]:
    """Report the ways ``status``'s stores disagree with one another.

    Deliberately conservative. A check that fires on a legitimate
    transient is worse than no check: operators learn to ignore the log
    line, and the signal is gone exactly when it matters. Each rule below
    is one that was observed to be *stably* wrong on real data, not a
    theoretical possibility.

    Pure — no I/O, no clock, no environment.

    .. note::

       These rules were derived by measuring a 169-plan sweep before any
       logging was wired up (2026-09-23). A first cut included
       "``plan_verification`` says ``running`` but nothing is registered
       in memory" — it fired on exactly one plan, and the plan was the
       *reader*: an out-of-process sweep cannot see ``_verification_state``
       at all, so the rule was really testing "am I the server". It was
       dropped rather than shipped, because a check whose only finding is
       its own blind spot is worse than no check.
    """
    found = []

    # Rule 1 — the 2026-09-23 incident, stated as an invariant.
    # "completed" asserts the workflow is finished; a live execution
    # contradicts it. a production plan carried this shape for
    # 80 minutes while a repair round was visibly running on the same
    # card. Only observable in the serving process (see the note on
    # ``PlanStatus``), which is where the endpoint runs.
    if _norm(status.phase) in FINISHED_PHASES and status.execution_in_flight:
        found.append(Divergence(
            "finished_phase_with_live_execution",
            f"phase={status.phase!r} says the workflow is finished, but an "
            f"execution is still in flight",
        ))

    # Rule 2 — a phase that names the verification sub-machine must agree
    # with its verdict. ``phase == verification_running`` is what the
    # status endpoint keys off to force ``status=running``; when the
    # verdict has already landed, the phase is the stale half. This one
    # is DB-vs-DB, so it is meaningful from any process.
    v_status = _norm(status.verification_status)
    if (
        _norm(status.phase).startswith("verification")
        and v_status in ("passed", "failed", "loop_stopped")
        and status.phase != f"verification_{v_status}"
        and not status.execution_in_flight
    ):
        found.append(Divergence(
            "phase_and_verdict_disagree",
            f"phase={status.phase!r} but the verification verdict is "
            f"{status.verification_status!r}",
        ))

    # Rule 3 — the two verification stores, one round behind.
    # ``plan_routing.verification`` is CAS-advanced at every round
    # boundary; ``plan_verification.verification_status`` is written by a
    # separate best-effort path. When the second write does not land, the
    # row keeps the ``running`` value stamped at round start, and since
    # ``/api/plan/{id}/status`` takes its top-level
    # ``verification_status`` from that row, the card header renders
    # "🔄 验证中" above a body naming the repair task that is actually
    # running, with ``verification_in_flight`` already False.
    #
    # Both writers are best-effort by design (every state write in this
    # codebase swallows its exception so a failed write never breaks the
    # workflow), which is exactly why the drift is silent. The
    # reconciliation at the read site fixes the card; this rule is what
    # makes the underlying miss visible instead.
    #
    # ``pending`` is deliberately NOT in the trigger set, though the
    # read-side reconciliation does override it. ``reset`` writes
    # ``pending`` into this column while a plan can legitimately be
    # executing repair work, and a column that says "not started" is not
    # a stale *claim* about anything — alarming on it would fire on every
    # reset plan. The defect this rule exists for is a column still
    # claiming a round is LIVE when no round is.
    if (
        not status.verification_in_flight
        and v_status in ("running", "in_progress")
        and status.execution_in_flight
    ):
        found.append(Divergence(
            "stale_running_verification_during_execution",
            f"plan_verification still reads {status.verification_status!r} "
            f"while an execution is in flight and no verification round "
            f"is live — the round-close write did not land",
        ))

    return tuple(found)
