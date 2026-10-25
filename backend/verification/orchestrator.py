"""
Verification Orchestrator — Manages Verification Cycles and State Transitions
============================================================================

Core responsibilities:
- Drive the verification loop via VerificationAgent
- Apply 3-layer loop control: round limits, same-failure detection, user intervention
- Coordinate RepairTaskGenerator when verification fails
- Persist all state transitions through PlanState
"""

import hashlib
import inspect
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, Tuple

from coding_tool import CodingTool, ClaudeCodingTool
from config_paths import resolve_state_db_path
from plan_state import PlanState
from verification_agent import VerificationAgent
from repair_generator import (
    RepairTaskGenerator,
    # 2026-10-23: the plan's own constraints. ``_plan_intent`` reads the
    # constraint sentences the plan states about itself, and
    # ``apply_intent_gate`` keeps a repair row that crosses one of them
    # from becoming a repair task.
    _plan_intent,
    apply_intent_gate,
)
# 2026-10-22: the type a cancelled round raises. Dependency-free, so a
# top-level import carries no cycle risk.
from verification_cancel import VerificationCancelled
from verification.failure_triage import (
    CRITERION_DEFECT,
    DELIVERABLE_DEFECT,
    Disposition,
    disposition_for,
    is_not_looser,
)
from verification.triage_agent import VPTriageJudge
from verification_plan_delta import (
    OBSOLETE_FIELD,
    OBSOLETE_REASON_FIELD,
    OBSOLETE_ROUND_FIELD,
    PlanDelta,
    PlanDeltaPersistError,
    REPLACEMENT_FOR_FIELD,
    apply_delta_persisted,
)


#: The version of ``verification_loop_tracking.json`` this module writes.
#:
#: v1 stored the failed verification-point ids and one counter for the
#: whole set, so a point that kept failing for a *different* reason
#: still read as "the same failure" as the round before. v2 stores the
#: failed ``(point, failure class)`` pairs and counts each pair on its
#: own.
_LOOP_TRACKING_VERSION = 2

#: The separator between a point id and its failure class in a decision
#: key — the same spelling :func:`verification.failure_triage.decision_key`
#: uses, so a key reads the same in a log line as it does on disk.
_DECISION_KEY_SEPARATOR = "|"


def _decision_key_str(key: Tuple[str, str]) -> str:
    """The on-disk spelling of one ``(point, failure class)`` pair."""
    vp_id, failure_class = key
    return f"{vp_id}{_DECISION_KEY_SEPARATOR}{failure_class}"


def _parse_decision_key(text: Any) -> Optional[Tuple[str, str]]:
    """Read one decision key back, or ``None`` when it is malformed.

    A key without the separator names a point but not a class, and a
    class is not something this module may guess — so the entry is
    dropped rather than completed.
    """
    if not isinstance(text, str):
        return None
    vp_id, separator, failure_class = text.partition(_DECISION_KEY_SEPARATOR)
    if not separator or not vp_id:
        return None
    return (vp_id, failure_class)


def _is_counted_failure(key: Tuple[str, str]) -> bool:
    """Whether this failure may move the same-cause counter.

    Only the failures a human should see move it: an infrastructure
    blip is not evidence that the work is wrong, and an out-of-scope
    check is not evidence of anything. Counting either would escalate a
    healthy run to a human and would make the counter report "same
    cause, twice" about two failures that have nothing in common.

    The rule is :func:`verification.failure_triage.is_counted` — the
    same one the failure-disposition chain uses — so the counter and
    the disposition never disagree about what counts.
    """
    from verification import failure_triage

    return failure_triage.is_counted(key[1])


#: The reason the loop stops when the triage could not name the failure.
#:
#: The loop knows it is stuck on a failure and knows the failure is not
#: one it can fix by itself — but it does not know *what* the failure
#: is, because the judge could not check its own answer. A human has
#: to look, and the reason says so in a shape a card can render.
STOP_REASON_NO_TRIAGE_CONCLUSION = "same_failure_no_triage_conclusion"

#: The reason a round carries when the triage itself could not deliver
#: an answer.
#:
#: The provider blew up, the payload was not a JSON object, or the
#: quote the answer carried could not be verified against the recorded
#: failure. None of those is a statement about the failure — they are
#: statements about the triage — so the round is *not* stopped on it:
#: the repair path gets its chance and the same-cause counter is left
#: alone. Stopping here would void a criterion over a triage that
#: never classified anything.
#:
#: 2026-10-23 (VP-019): before this reason existed the two failures
#: were indistinguishable — the shipped judge answers with a
#: conclusion instead of raising, so every technical failure landed in
#: the "no conclusion" branch and stopped the chain with an empty
#: ``repair_tasks``.
REASON_TRIAGE_UNAVAILABLE = "triage_unavailable"

#: The reason the loop stops when the triage named a broken criterion.
#:
#: A criterion defect cannot be repaired by reworking the deliverable:
#: the check itself is wrong, so the loop stops and a human fixes the
#: check.
#:
#: 2026-10-22: the loop stops here only when the triage named the broken
#: criterion **without saying what should replace it**. When the triage
#: does propose a replacement, the loop replaces the criterion itself
#: (see :meth:`VerificationOrchestrator._apply_criterion_replacement`)
#: and keeps going — see :data:`REASON_CRITERION_REPLACED`.
STOP_REASON_CRITERION_DEFECT = "same_failure_criterion_defect"

#: The reason a round carries when the triage's proposed replacement was
#: used: the broken criterion was obsoleted and a new one took its place
#: on disk, so the loop continues with the *new* criterion.
REASON_CRITERION_REPLACED = "criterion_replaced"

#: The source a question asked from the verification side carries.
#:
#: The verification loop waits for the questions it recognises by this
#: name (``verification_loop.VERIFICATION_DECISION_SOURCE``), so a
#: question asked from here is one the round will wait for.
VERIFICATION_DECISION_SOURCE = "verification"

#: The answers an out-of-scope question offers.
#:
#: The check asked for something nobody scoped. The two answers are the
#: two things a human can do about it: drop the check, or agree to
#: build what it asked for. There is no third — the plan's scope is
#: the plan's scope.
OUT_OF_SCOPE_OPTIONS = ("skip_the_check", "bring_it_into_scope")

#: The answer an out-of-scope question falls back to when nobody replies.
#:
#: ``auto`` mode needs one, and this is the conservative reading: the
#: check asked for something the plan never agreed to build, so the
#: default is to leave the plan alone.
OUT_OF_SCOPE_DEFAULT_OPTION = "skip_the_check"


#: The module logger.
#:
#: The orchestrator's ``self.logger`` is injected by the callers that
#: have one — and nothing in the backend injects it, so every line
#: written through it alone was dropped on the floor. This logger is
#: always there, so an audit line that matters (a refused replacement,
#: a write that failed) is never silently lost.
logger = logging.getLogger(__name__)


#: The synthetic point id the plan-delta audit events are filed under.
#:
#: The event is about the **plan**, not about one verification point: a
#: refused replacement touches the plan file, so it cannot be filed
#: under the point whose criterion was to be replaced — that point's own
#: events say what happened to the check, not to the plan.
PLAN_DELTA_AUDIT_VP_ID = "__plan_delta__"

#: The event written when the plan delta could not be written to disk.
EVENT_PLAN_DELTA_PERSIST_FAILED = "plan_delta_persist_failed"

#: The event written when the proposed replacement was refused on its
#: merits — it checks less than the criterion it replaces, or it is
#: unusable as it stands.
EVENT_PLAN_DELTA_REPLACEMENT_REJECTED = "plan_delta_replacement_rejected"

#: Why a proposed replacement was not used: the write did not land.
#:
#: The plan on disk still says what it said before, so the failure is
#: downgraded to a deliverable defect and the loop keeps repairing.
REPLACEMENT_REJECTED_PERSIST_FAILED = "persist_failed"

#: Why a proposed replacement was not used: it checks less than the
#: criterion it replaces (a rubber stamp is not a replacement).
REPLACEMENT_REJECTED_WEAKER_METHOD = "weaker_method"

#: Why a proposed replacement was not used: the proposal itself was
#: unusable (it carried no title, or the plan's own guard dropped it).
REPLACEMENT_REJECTED_INVALID_PROPOSAL = "invalid_proposal"

#: The audit event each rejection reason is filed under.
_REJECTION_EVENTS: Dict[str, str] = {
    REPLACEMENT_REJECTED_PERSIST_FAILED: EVENT_PLAN_DELTA_PERSIST_FAILED,
    REPLACEMENT_REJECTED_WEAKER_METHOD: EVENT_PLAN_DELTA_REPLACEMENT_REJECTED,
    REPLACEMENT_REJECTED_INVALID_PROPOSAL: (
        EVENT_PLAN_DELTA_REPLACEMENT_REJECTED
    ),
}


class TriageStopReason(str):
    """A stop reason that is a code to the machine and a verdict to a human.

    Two contracts meet at the stop reason, and neither fits into the
    other's value:

    * The **state machine's vocabulary** is a closed set of strings, and
      the whole verification stack compares a stop reason against it:
      the auto-loop's convergence guard, the terminal write's
      chain-ending decision, the ``plan_verification`` column, the API
      response model. A *mapping* that reached that stack would raise
      on the first membership test — ``dict`` is unhashable, so
      ``reason in frozenset`` is a ``TypeError`` — and the plan it
      belonged to would be stranded inside the verification family: a
      silent failure that looks like a plan that simply never finishes.
    * The **triage's verdict** is a mapping: which point failed, which
      failure class it was, and what the triage said about it. A bare
      string says only *that* the loop stopped, which is the very thing
      the triage was introduced to fix.

    So the reason *is* the code — a string, and therefore hashable,
    bindable, serialisable and comparable everywhere the vocabulary
    expects one — and the verdict travels with it. The verdict is read
    three ways, so every way a caller might reach for it works:
    through :attr:`verdict`, by subscript (``reason["vp_id"]``), and by
    comparison (``result["stop_reason"] == {...}``).
    """

    __slots__ = ("verdict",)

    def __new__(cls, code: str, verdict: Optional[Mapping] = None):
        obj = super().__new__(cls, code or "")
        obj.verdict = dict(verdict) if verdict else {}
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, TriageStopReason):
            # Two reasons are the same reason when they say the same
            # thing: the same code *and* the same verdict. Comparing
            # the verdicts alone would let two different codes with
            # one verdict pass for each other.
            return (
                str.__eq__(self, other) is True
                and self.verdict == other.verdict
            )
        if isinstance(other, Mapping):
            return self.verdict == dict(other)
        return str.__eq__(self, other)

    def __ne__(self, other: Any) -> bool:
        verdict = self.__eq__(other)
        if verdict is NotImplemented:
            return verdict
        return not verdict

    def __getitem__(self, key: str) -> Any:
        """Read the verdict by key, the way a mapping would.

        The reason is a code with a verdict attached; when a caller
        writes ``reason["vp_id"]`` it means the verdict.
        """
        return self.verdict[key]

    def get(self, key: str, default: Any = None) -> Any:
        """The verdict's ``get`` — a key the verdict does not carry is
        not an error, it is the default."""
        return self.verdict.get(key, default)

    # Defining ``__eq__`` sets ``__hash__`` to None; the reason must
    # stay hashable — the vocabulary is a set.
    __hash__ = str.__hash__


def stop_reason_code(result: Mapping) -> str:
    """The machine-facing spelling of a round's stop reason.

    The orchestrator's stop reason carries the triage's verdict (see
    :class:`TriageStopReason`), and the state machine's vocabulary is a
    closed set of strings. This helper is the seam between the two: it
    always yields a string, so a verdict that reaches the vocabulary
    can never raise on it.

    A *string* reason wins over the code: the auto-loop rewrites the
    reason in a few branches (``pending_repair_tasks``), and that
    rewrite is the more specific statement of what happened.

    Args:
        result: A round's result payload.

    Returns:
        The code, or ``""`` when the payload carries no reason.
    """
    reason = result.get("stop_reason")
    if isinstance(reason, str) and reason:
        return str(reason)
    code = result.get("stop_reason_code")
    if isinstance(code, str) and code:
        return str(code)
    if isinstance(reason, Mapping):
        return str(reason.get("reason") or "")
    return ""


def _read_triage_conclusion(
    conclusion: Any,
) -> Optional[Tuple[bool, str, str, bool]]:
    """The ``(conclusive, failure_class, rationale, available)`` of a verdict.

    The judge answers with a
    :class:`verification.triage_agent.TriageConclusion`; a double that
    answers with a mapping carrying the same fields is read the same
    way. Anything else — ``None``, a string, an object without a
    ``conclusive`` field — is not a verdict at all, and ``None`` says
    so: the caller must not act on it.

    ``available`` is the difference between a triage that malfunctioned
    and a triage that ran without being able to characterise the
    failure — see :func:`_read_triage_availability`.

    Args:
        conclusion: Whatever the judge returned.

    Returns:
        The four fields, or ``None`` when the answer is not a verdict.
    """
    if isinstance(conclusion, Mapping):
        if "conclusive" not in conclusion:
            return None
        return (
            bool(conclusion.get("conclusive")),
            str(conclusion.get("failure_class") or ""),
            str(conclusion.get("rationale") or ""),
            _read_triage_availability(conclusion),
        )
    if not hasattr(conclusion, "conclusive"):
        return None
    return (
        bool(getattr(conclusion, "conclusive")),
        str(getattr(conclusion, "failure_class", "") or ""),
        str(getattr(conclusion, "rationale", "") or ""),
        _read_triage_availability(conclusion),
    )


def _read_triage_availability(conclusion: Any) -> bool:
    """Whether the triage itself delivered an answer at all.

    A verdict that does not say is taken at its word — *available* —
    because that is the shape every conclusion had before the
    distinction existed, and a conclusion nobody could act on is not
    evidence about the failure. Only an explicit ``False`` says the
    triage malfunctioned, and only then does the caller keep the loop
    running instead of stopping on it.

    Args:
        conclusion: Whatever the judge returned.

    Returns:
        ``True`` when the triage delivered an answer, ``False`` when it
        malfunctioned.
    """
    if isinstance(conclusion, Mapping):
        value = conclusion.get("available", True)
    else:
        value = getattr(conclusion, "available", True)
    if value is None:
        return True
    return bool(value)


def _read_triage_replacement(conclusion: Any) -> Optional[Dict[str, Any]]:
    """The proposed replacement criterion of a triage verdict, if any.

    A :class:`verification.triage_agent.TriageConclusion` carries the
    replacement the judge proposed for a broken criterion; a double that
    answers with a mapping carrying the same field is read the same way.
    A verdict without a replacement — or with one that is not a mapping
    — proposes nothing, and ``None`` says so: the caller must not act on
    it.

    Args:
        conclusion: Whatever the judge returned.

    Returns:
        The replacement as a plain dict, or ``None`` when the verdict
        proposes no replacement.
    """
    if isinstance(conclusion, Mapping):
        replacement = conclusion.get("replacement")
    else:
        replacement = getattr(conclusion, "replacement", None)
    if not isinstance(replacement, Mapping):
        return None
    return dict(replacement)


def _undefined_vp_operator_action(vp_ids: Any) -> Optional[str]:
    """What the operator has to do about the points the plan never defined.

    A point the plan never wrote down has no criterion: there is nothing
    to check it against, and no rework of the deliverable can satisfy it.
    The executor must not invent one — so the plan's **owner** has to void
    the point or express its criterion. That is the action this string
    names, and it is what the round hands to whoever reads the verdict.

    The wording is deliberately an action and not a verdict: the judge
    layer did not judge this point, so it must not dress the record up as
    a pass either — it says what it is and who has to act on it.

    Args:
        vp_ids: The points that are in the report but not in the plan.

    Returns:
        The action to take, or ``None`` when there is nothing to void.
    """
    ids = sorted({str(vp_id).strip() for vp_id in vp_ids or () if str(vp_id).strip()})
    if not ids:
        return None
    return (
        f"{', '.join(ids)} 在验证计划里没有定义，"
        "须由计划属主作废或重新表达它的判据"
    )


def _state_db_path_for_orchestrator() -> Path:
    """Resolve the backend state-machine SQLite path.

    Kept as a named function rather than inlined because ``server.py``
    must not be imported here (circular dep: server → this module).
    :func:`config_paths.resolve_state_db_path` has no such constraint —
    it imports only the standard library — so this now delegates to it
    instead of re-deriving the path locally, which is what every other
    caller does too.

    **Why delegation, and not the previous local derivation.** This
    function used to resolve its default from its own ``__file__``, and
    for a while used ``.parent.parent``: that reached
    ``<repo>/backend/state.db`` — a stale 40 KB database from
    2026-08-27 with no ``plan_tasks`` table. Every ``add_task`` call
    therefore wrote to a DB no other process reads, silently losing the
    RP-* repair rows that the executor's Phase 2 reconcile needs. A
    third ``.parent`` made it correct *on that day*; delegating makes it
    correct on the next move as well, and that is the property worth
    having.
    """
    return resolve_state_db_path()


def _accepts_plan_intent(generator: Any) -> bool:
    """Whether ``generator.generate_repair_contents`` takes ``plan_intent``.

    The orchestrator hands the plan's own constraints to the repair
    generator so the model can *avoid* the conflict instead of being
    caught by it (2026-10-23). That is a courtesy to the model, not a
    precondition for the round: a collaborator whose signature predates
    the parameter still has to be callable, because a round that dies on
    a keyword argument produces no repair tasks at all — which the
    auto-loop reads as "nothing to repair", the exact failure this
    codebase has fought hardest against.

    A callable whose signature cannot be read is assumed to accept the
    parameter: the modern generator is the common case, and silently
    withholding the constraints would weaken the prompt for everybody.
    The deterministic intent gate downstream (``apply_intent_gate``) is
    what actually holds the line, so a generator that never saw the
    constraints loses the courtesy, not the protection.
    """
    try:
        parameters = inspect.signature(
            generator.generate_repair_contents
        ).parameters
    except (AttributeError, TypeError, ValueError):
        return True
    if "plan_intent" in parameters:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in parameters.values()
    )


class VerificationOrchestrator:
    """Manages the verification loop and state transitions."""

    def __init__(
        self,
        plan_dir: Path,
        project_dir: Path,
        coding_tool: Optional[CodingTool] = None,
        verif_repo: Optional[Any] = None,
        triage_judge: Optional[Any] = None,
    ):
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)
        self.plan_state = PlanState(self.plan_dir)
        self.coding_tool = coding_tool or ClaudeCodingTool()
        # No ``max_parallel``. The round has no width to pass: the
        # provider slot pool decides how many verification points run at
        # once, per dispatch, so every orchestrator — including the fresh
        # one the post-repair callback builds in ``verification_loop`` —
        # runs the same way by construction rather than by remembering
        # what to pass.
        self.verification_agent = VerificationAgent(
            self.plan_dir, self.project_dir, self.coding_tool,
            verif_repo=verif_repo,
        )
        self.repair_generator = RepairTaskGenerator(
            self.coding_tool, self.plan_dir, self.project_dir
        )
        # 2026-09-14: kept so the VP-split path can persist each parent's
        # SPLIT verdict into ``plan_verification.verdicts`` (the row a
        # resumed round reads to skip the superseded parent).
        self.verif_repo = verif_repo
        self._current_vp_splits: List[Dict[str, Any]] = []
        # 2026-10-22 (v2): the same-cause state is per
        # ``(verification point, failure class)`` pair, not per failed
        # point. ``_previous_failed_keys`` holds the pairs that failed in
        # the last recorded round; ``_same_failure_counts`` holds how
        # many rounds each failure has repeated (one on the round it
        # first failed) — failures that do not count (``infra``,
        # ``out_of_scope``) are absent from it. The two legacy
        # attributes (``_previous_failed_ids`` /
        # ``_consecutive_same_failure_rounds``) are views over these two
        # — see the properties below.
        self._previous_failed_keys: set = set()
        self._same_failure_counts: Dict[str, int] = {}
        self._last_round: int = 0
        # 2026-09-12: terminating the chain after 1
        # same-failure repeat (old behaviour) is too violent — but
        # giving the agent unlimited retries without feedback lets it
        # repeat the same failing approach. The right design:
        #
        # 1. Don't terminate on the first repeat. Bump counter and
        #    keep generating repair_tasks so the executor gets
        #    another shot.
        # 2. Inject previous-round failure evidence into the
        #    repair-task-generation prompt so the LLM agent sees
        #    "上次方案完全无效" and is steered away from repeating
        #    it. We accumulate feedback per-VP across rounds (full
        #    history, not just last round) so the agent can see
        #    what each previous attempt observed.
        # 3. Hard cap ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` as
        #    a safety net — enough chances for feedback to land, but
        #    not so many that a truly broken VP burns tokens forever.
        #
        # 2026-09-19（**推翻** 2026-09-12 的"第一次重复不终止"）：两轮失败
        # 集合相同就停。
        #
        # 2026-09-12 当时的理由是"第一次重复就终止太暴力，真 bug 一轮修不
        # 完"；2026-09-19 实测推翻了它：某一轮的 round 2 与 round 3 的失败
        # 集合完全相同仍继续跑，白白多烧一整轮，而且真正的上限在服务端
        # auto-loop（`for round_num in range(start_round, max_rounds+1)`），
        # 所以阈值 3 在 `max_rounds=3` 下**数学上不可达**——这条停止条件从来
        # 没生效过，每次都以 `max_rounds_reached` 收场。
        #
        # 2026-10-22 (v2): 比较粒度从"失败点集合"变成
        # `(vp_id, failure_class)`，计数也从"整集合"变成"逐 key"。文件里的
        # `same_failure_counts[key]` 是**该失败已经连续重复了几轮**（首次
        # 失败那轮记 1）。
        #
        # 2026-10-22 (triage): 阈值从 1 改成 2，而且**达到阈值不再直接终止**
        # ——先做一次语义分诊（`VPTriageJudge.judge()`，一次无工具的 LLM
        # 调用），由分诊结论决定"终止"还是"分流"：
        #
        # * `criterion_defect`（判据本身坏了）→ 终止：再怎么返工也满足不
        #   了这条判据，只能由人改判据；
        # * `deliverable_defect`（产物真的错了）→ 分流：继续修，并且给这个
        #   失败重新发一轮预算；
        # * 分诊给不出可核验的结论 → 终止，但终止原因是**结构化**的（点名
        #   哪个点、哪一类失败、分诊自己说了什么）；
        # * 分诊本身炸了 → 既不终止也不作废计数：坏的分诊不是关于失败的证据。
        #
        # 计数的单位：阈值 2 = **同一个 `(点, 失败类别)` 连续失败两轮**就
        # 触发分诊（首次失败那轮记 1，第二轮记 2）。这是"轮数"，不是
        # "重复次数"——后者见 `_consecutive_same_failure_rounds` 的
        # docstring，两者相差一轮。
        self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 2
        # ``vp_id -> list of {round, actual_result, evidence}`` —
        # one entry per round where the VP failed. The list grows
        # monotonically and is cleared only when the VP passes.
        # 2026-09-14: the per-VP failure history
        # used to live ONLY in this instance's memory, but the
        # repair→execute→re-verify chain builds a FRESH orchestrator every
        # round (server.py ``_on_repair_complete``), so
        # ``previous_failure_feedback`` was always empty in production and
        # the "上次修复尝试未生效" steering never reached the repair
        # prompt. It is now loaded from
        # ``plans/{id}/verification_failure_history.json`` (see
        # ``verification.failure_history``), with a one-time seed from
        # state.db's accumulated verdicts so plans already mid-flight get
        # feedback without waiting a round.
        self._failure_history: Dict[str, List[Dict[str, Any]]] = (
            self._load_failure_history()
        )
        self._failure_history_seeded = False
        # 2026-09-19: the same-failure tracking used to live ONLY in this
        # instance's memory — precisely the hole the paragraph above
        # describes for the failure history, and it was never closed for
        # it. Because ``_on_repair_complete`` builds a FRESH orchestrator
        # for every post-repair round, the tracking reset to ``None`` /
        # ``0`` at the start of each round — the same-failure branch
        # could never fire at all: consecutive rounds fail the identical
        # VP set and the loop still ends with ``max_rounds_reached``.
        # Persisted next to the failure history so the stop decision
        # survives the rebuild.
        # 2026-10-22 (v2): the persisted state is per
        # ``(verification point, failure class)`` pair, so a point that
        # changed its failure class is not counted as the same cause.
        (
            self._previous_failed_keys,
            self._same_failure_counts,
            self._last_round,
        ) = self._load_loop_tracking()
        self._current_repair_tasks: List[Dict] = []
        self._waiting_for_user = False
        # 2026-10-22 (triage): the semantic judge that names the nature of
        # a failure the loop is stuck on. Injected in tests; built on first
        # use in production (the judge is only needed once the same-cause
        # threshold is reached, so building it lazily keeps the common
        # path free of it).
        self._triage_judge = triage_judge

    #: Loop-progress tracking that must outlive a single orchestrator (see
    #: the 2026-09-19 note in ``__init__``).
    _LOOP_TRACKING_FILENAME = "verification_loop_tracking.json"

    def _loop_tracking_path(self) -> Path:
        return Path(self.plan_dir) / self._LOOP_TRACKING_FILENAME

    # ------------------------------------------------------------------
    # The legacy views of the v2 state
    # ------------------------------------------------------------------
    @property
    def _previous_failed_ids(self) -> Optional[set]:
        """The points that failed in the last recorded round, ids only.

        A view over the v2 keys, for the callers that only care about
        *which* points failed (the failure-history prune, the audit
        surface). ``None`` means "no round with failures has been
        recorded" — the shape the pre-v2 code used.
        """
        if not self._previous_failed_keys:
            return None
        return {vp_id for vp_id, _ in self._previous_failed_keys}

    @_previous_failed_ids.setter
    def _previous_failed_ids(self, value) -> None:
        if value is None:
            self._previous_failed_keys = set()
            return
        # The ids carry no class. An empty class records exactly that —
        # "the class was not recorded" — and never matches a real class,
        # so a legacy assignment cannot be mistaken for a classified
        # failure.
        self._previous_failed_keys = {(str(vp_id), "") for vp_id in value}

    @property
    def _consecutive_same_failure_rounds(self) -> int:
        """How many consecutive rounds the most-repeated failure repeated.

        The v2 count is the number of rounds a failure has repeated (one
        on the round it first failed); this view reports the *repeats* —
        the rounds after the first — because that is what the pre-v2
        counter counted and what the loop-control threshold is still
        expressed in. The two differ by one round, so the stop point
        of the loop is unchanged by the v2 upgrade.

        Only the failures of the last recorded round are considered:
        a count left over from an earlier round describes a failure
        that is not being made again.
        """
        repeats = 0
        for key in self._previous_failed_keys:
            count = self._same_failure_counts.get(_decision_key_str(key), 0)
            repeats = max(repeats, count - 1)
        return max(0, repeats)

    @_consecutive_same_failure_rounds.setter
    def _consecutive_same_failure_rounds(self, value) -> None:
        count = (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else 0
        )
        count = max(0, count)
        if count == 0:
            self._same_failure_counts = {}
            return
        # The aggregate is spread over the keys of the last recorded
        # round — the only failures it can be about. The count is the
        # number of rounds the failure repeated, which is one more than
        # the repeats this attribute reports.
        self._same_failure_counts = {
            _decision_key_str(key): count + 1
            for key in self._previous_failed_keys
        }

    def _load_loop_tracking(self) -> Tuple[set, Dict[str, int], int]:
        """Return ``(failed_keys, same_failure_counts, last_round)``.

        v2 (2026-10-22): the tracking file counts failures per
        ``(verification point, failure class)`` pair, so a point that
        keeps failing for a *different* reason is a different failure
        and no longer counts as the same cause. The file shapes:

        * v2 — ``{"version": 2, "previous_failed_keys": [...],
          "same_failure_counts": {...}, "last_round": N}``
        * v1 — ``{"previous_failed_ids": [...],
          "consecutive_same_failure_rounds": N}``

        ``same_failure_counts`` holds, per decision key, the number of
        rounds that failure has repeated — one on the round it first
        failed. Failures that do not count (``infra``,
        ``out_of_scope``) have no entry at all.

        A v1 file is read as an empty state. It names the points that
        failed but not *why*, and inventing a class for them would
        fabricate the very distinction v2 exists to make — a v1 file
        therefore contributes no keys and no counts, and the next write
        upgrades the file to v2.

        Missing / unreadable / malformed file → ``(set(), {}, 0)``, i.e.
        the old in-memory behaviour. A file left over from a previous
        plan generation is not a correctness problem: the first
        comparison would simply be against a set of keys that no longer
        matches, which resets the counter instead of stopping the loop.

        ``verification/reset`` deletes this file, so an operator restart
        starts the counting from scratch rather than inheriting a
        verdict set from the run that was just discarded.
        """
        try:
            with self._loop_tracking_path().open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return set(), {}, 0
        if not isinstance(payload, dict):
            return set(), {}, 0

        failed_keys: set = set()
        raw_keys = payload.get("previous_failed_keys")
        if isinstance(raw_keys, list):
            for item in raw_keys:
                parsed = _parse_decision_key(item)
                if parsed is not None:
                    failed_keys.add(parsed)

        counts: Dict[str, int] = {}
        raw_counts = payload.get("same_failure_counts")
        if isinstance(raw_counts, dict):
            for name, value in raw_counts.items():
                if not isinstance(name, str):
                    continue
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                counts[name] = max(0, value)

        raw_round = payload.get("last_round")
        last_round = (
            raw_round
            if isinstance(raw_round, int) and not isinstance(raw_round, bool)
            else 0
        )
        return failed_keys, counts, max(0, last_round)

    def _save_loop_tracking(self) -> None:
        """Persist the loop-progress tracking (v2).

        The payload names the decision keys of the last recorded round
        and, per key, how many rounds that failure has repeated — the
        tally the next round's fresh orchestrator continues from.

        The write is atomic: the payload goes to a temporary file next
        to the target and is then moved into place with ``os.replace``.
        A failure therefore leaves the previous file whole — a
        half-written JSON would be read as a corrupt file, which resets
        the counter and lets the loop run on forever.

        Best-effort: a write failure must never abort a verification
        round — the worst case is that the next (fresh) orchestrator
        behaves as it did before 2026-09-19.
        """
        payload = {
            "version": _LOOP_TRACKING_VERSION,
            "previous_failed_keys": sorted(
                _decision_key_str(key) for key in self._previous_failed_keys
            ),
            "same_failure_counts": {
                name: count
                for name, count in sorted(self._same_failure_counts.items())
                if count > 0
            },
            "last_round": int(self._last_round),
        }
        path = self._loop_tracking_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            # The target is untouched; drop the half-written temporary
            # file so it cannot be mistaken for the tracking file.
            try:
                tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # The same-failure triage
    # ------------------------------------------------------------------
    #
    # The loop control used to end the chain the moment the same failure
    # repeated, with a bare string as the reason and no repair tasks. The
    # triage replaced that: reaching the threshold buys ONE semantic
    # triage call, and the triage's verdict decides whether the loop
    # stops (a human has to look) or diverts into the repair path.

    #: The file the triage's verdict is written to.
    #:
    #: The verdict answers *why* the chain stopped, and the chain stops
    #: in a process that ends right there — the in-memory verdict dies
    #: with the round. The plan's own directory is where the other
    #: round-scoped records live (``verification_loop_tracking.json``,
    #: ``verification_failure_history.json``), so the verdict goes
    #: there too: the human who opens the plan can read what the
    #: triage said, instead of a bare code that says only that the
    #: chain gave up.
    _TRIAGE_VERDICT_FILENAME = "verification_triage_verdict.json"

    def _triage_verdict_path(self) -> Path:
        return Path(self.plan_dir) / self._TRIAGE_VERDICT_FILENAME

    def _save_triage_verdict(
        self, verdict: Mapping, round_number: int,
    ) -> None:
        """Write the triage's verdict to the plan's directory.

        Best-effort, like every other record the orchestrator keeps: a
        write failure must never abort a round that has already reached
        its verdict. The write is atomic (a temporary file next to the
        target, moved into place) so a reader never sees half a file.
        """
        payload = {
            "round": int(round_number),
            "verdict": dict(verdict),
        }
        path = self._triage_verdict_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _stuck_failure_keys(self) -> List[Tuple[str, str]]:
        """The decision keys whose same-cause count has reached the cap.

        A key is a ``(verification point, failure class)`` pair, and its
        count is the number of rounds that failure has repeated — one on
        the round it first failed. The cap is
        ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` **rounds**, so a cap of
        2 fires on the round the same failure repeats for the second
        time.

        Only the current round's keys are considered: a count left over
        from an earlier round describes a failure that is not being made
        again. And a failure that does not count (``infra``,
        ``out_of_scope``) has no count at all, so it can never be here —
        two rounds of an environment blip buy no triage call.
        """
        cap = self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS
        return sorted(
            key
            for key in self._previous_failed_keys
            if self._same_failure_counts.get(_decision_key_str(key), 0) >= cap
        )

    def _triage_judge_for(self) -> Any:
        """The judge to ask — the injected one, or one built on first use.

        The judge makes one tool-less LLM call, so it is built only when
        a failure is actually stuck; the common path never pays for it.
        """
        if self._triage_judge is None:
            self._triage_judge = VPTriageJudge(coding_tool=self.coding_tool)
        return self._triage_judge

    def _report_failure_entries(self, report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every result entry of the report, the freshest first.

        The same two lists :meth:`_extract_failed_ids` walks: the
        top-level results and the last round's snapshot.
        """
        entries: List[Dict[str, Any]] = []
        top_level = report.get("verification_results") or []
        if isinstance(top_level, list):
            entries.extend(e for e in top_level if isinstance(e, dict))
        rounds_field = report.get("rounds") or []
        if isinstance(rounds_field, list) and rounds_field:
            last_round = (
                rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
            )
            last_results = last_round.get("results") or []
            if isinstance(last_results, list):
                entries.extend(e for e in last_results if isinstance(e, dict))
        return entries

    def _route_failed_vps(
        self, failed_vps: List[Dict[str, Any]], round_number: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split the failed VPs by the remedy their failure class buys.

        The four classes are not four flavours of "the deliverable is
        wrong" — each one wants a different remedy, and the table that
        says which is :func:`verification.failure_triage.disposition_for`
        (the same table the same-cause counter reads, so the two can
        never disagree about what a failure is):

        * ``deliverable_defect`` → the deliverable is wrong: a repair
          task. This is the only class that reaches the repair model.
        * ``infra`` → the attempt malfunctioned: **no** repair task.
          Reworking a deliverable does not fix a rate limit, and the
          attempt is worth another try — the next round runs it again.
        * ``criterion_defect`` → the check itself is broken: **no**
          repair task. The criterion is retired by the triage
          (:meth:`_apply_criterion_replacement`), not by a rework.
        * ``out_of_scope`` → the check asks for something nobody scoped:
          **no** repair task and **no** criterion delta — the question
          goes to a human (:meth:`_ask_a_human_about`).

        The split happens *before* the repair model is asked anything,
        so a failure that must not be repaired never reaches the model
        at all — not even as a row the parser has to drop.

        A record that names no class reads as ``deliverable_defect``:
        the classification is newer than most verdicts, and "nobody said
        otherwise" has always meant "repair it".

        Returns:
            ``(repairable, deferred)``. ``repairable`` holds the
            records that may become repair tasks; ``deferred`` holds
            one entry per failure that got none, naming the point, its
            class, the disposition and — for an out-of-scope check — the
            question that was put to a human.
        """
        from verification.failure_history import failure_class_of

        repairable: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        for vp in failed_vps or []:
            if not isinstance(vp, dict):
                continue
            failure_class = failure_class_of(vp)
            disposition = disposition_for(failure_class)
            if disposition == Disposition.RETRY_COUNTED:
                repairable.append(vp)
                continue
            record: Dict[str, Any] = {
                "id": str(vp.get("id") or ""),
                "failure_class": failure_class,
                "disposition": disposition,
            }
            if disposition == Disposition.SKIP_NOT_COUNTED:
                # The check asks for something nobody scoped: the only
                # remedy is a human's answer, so the question is asked
                # here and now — not after the repair path has run.
                record["decision_id"] = self._ask_a_human_about(
                    vp, failure_class, round_number,
                )
            deferred.append(record)
        return repairable, deferred

    def _ask_a_human_about(
        self, vp: Dict[str, Any], failure_class: str, round_number: int,
    ) -> str:
        """Put an out-of-scope check to a human, and report the question.

        The question is asked through :func:`hitl.service.create_decision`
        — the only writer of ``plan_decisions`` — so it lands in the
        table the verification loop waits on
        (``verification_loop._open_verification_decisions``) and in the
        audit mirror every other question lands in.

        One question per point: a failure that repeats while the first
        question is still open does not pile up questions nobody can
        answer.

        Returns:
            The decision id, or ``""`` when no question was asked (one
            was already open, or the question could not be created).
        """
        from hitl.models import DecisionRequest
        from hitl.service import create_decision

        plan_id = self.plan_dir.name
        vp_id = str(vp.get("id") or "")
        if self._open_decision_for(plan_id, vp_id):
            return ""

        summary = str(
            vp.get("actual_result_summary") or vp.get("evidence_summary") or ""
        )
        request = DecisionRequest(
            plan_id=plan_id,
            source=VERIFICATION_DECISION_SOURCE,
            vp_id=vp_id,
            failure_class=failure_class,
            round=round_number,
            question=(
                f"the verification check {vp_id} asks for something this "
                f"plan never scoped: {summary}"
            ),
            options=list(OUT_OF_SCOPE_OPTIONS),
            default_option=OUT_OF_SCOPE_DEFAULT_OPTION,
        )
        try:
            decision = create_decision(request, self.plan_dir)
        except Exception as exc:  # noqa: BLE001 — a question is not a verdict
            # A decision that could not be created must not take the
            # round down with it: the failure is still recorded, still
            # not repaired, and the operator sees why in the audit.
            self._log_audit(
                "verification_out_of_scope_decision_failed",
                f"the out-of-scope question for {vp_id} could not be asked: "
                f"{type(exc).__name__}: {exc}",
                data={
                    "plan_id": plan_id,
                    "vp_id": vp_id,
                    "failure_class": failure_class,
                    "round": round_number,
                },
            )
            return ""
        return str(getattr(decision, "decision_id", "") or "")

    def _open_decision_for(self, plan_id: str, vp_id: str) -> bool:
        """Whether an open question about this point is already waiting.

        "Open" is the pair of states the verification loop waits on
        (``pending`` / ``paused``): a question nobody answered yet.
        A settled question — answered, defaulted, expired, unroutable —
        is not open, so the next round may ask again.
        """
        from hitl.models import DecisionStatus
        from hitl.service import list_decisions

        open_statuses = (
            DecisionStatus.PENDING.value,
            DecisionStatus.PAUSED.value,
        )
        try:
            decisions = list_decisions(plan_id)
        except Exception as exc:  # noqa: BLE001 — the store may be gone
            self._log_audit(
                "verification_out_of_scope_decision_lookup_failed",
                f"the open questions about {vp_id} could not be read: "
                f"{type(exc).__name__}: {exc}",
                data={"plan_id": plan_id, "vp_id": vp_id},
            )
            return False
        return any(
            decision.status in open_statuses
            and str(getattr(decision, "vp_id", "") or "") == vp_id
            for decision in decisions
        )

    def _read_verification_plan(self) -> Optional[Dict[str, Any]]:
        """The verification plan file's payload — ``None`` when it cannot be read.

        Two facts a caller must not confuse:

        * ``None`` — the file is missing, unreadable, malformed, or does
          not hold a plan object. The plan's wishes are **unknown**.
        * a payload that declares no verification point — the plan is
          readable but asks for nothing.

        The distinction matters wherever the *report* is compared against
        the *plan* (2026-10-25, VP-031): an unreadable plan must never be
        read as "the plan asks for nothing", and a plan that declares no
        point must never be read as "every point in the report is
        undefined" — that reading would void a whole round's failures and
        pass a plan whose criteria were never written down (the boundary
        ``e394a52`` established).
        """
        path = Path(self.plan_dir) / "verification_plan.json"
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _verification_point_definitions(self) -> Dict[str, Dict[str, Any]]:
        """The verification plan's points, by id.

        The plan is the only place that knows what a point *asked for*
        (its title, expected result, verification method and test
        command) — the report only knows how it went.
        """
        payload = self._read_verification_plan()
        if payload is None:
            return {}
        return self._verification_points_of(payload)

    def _verification_points_of(
        self, payload: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        """The plan payload's points, by id."""
        points = payload.get("verification_points") or []
        definitions: Dict[str, Dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            vp_id = str(point.get("id") or "").strip()
            if vp_id:
                definitions[vp_id] = point
        return definitions

    def _obsolete_vp_ids(self) -> Set[str]:
        """The points the plan itself retired.

        A point the plan marked ``obsolete`` — or gave an
        ``obsolete_reason`` — is a criterion the plan no longer
        requires: the executor skips such a point
        (:meth:`verification_executor.VerificationExecutor._build_items_from_plan`),
        because running a check the plan dropped would manufacture a
        failure for something nobody asked for.

        The only trace left of such a point in a round is the
        **report** entry that still lists it as FAILED — the round that
        retired it is the round that saw it fail. Left alone, that
        entry produces a repair task for a judgement nobody can
        satisfy, and it feeds the same-cause counter, so a criterion
        that is already gone ends the chain with "the same failure,
        again".

        The predicate reads the **plan file's** points
        (``verification_points`` — the agent schema), not the
        executor's converted list: the conversion
        (:meth:`verification_agent.VerificationAgent._convert_plan_to_executor_schema`)
        forwards ``obsolete`` but drops ``obsolete_reason``, and a
        reason alone is enough to retire a point.
        """
        retired: Set[str] = set()
        for vp_id, point in self._verification_point_definitions().items():
            reason = str(point.get(OBSOLETE_REASON_FIELD) or "").strip()
            if point.get(OBSOLETE_FIELD) or reason:
                retired.add(vp_id)
        return retired

    def _undefined_vp_ids(self, report: Dict[str, Any]) -> Set[str]:
        """The points the report failed but the plan never defined.

        The twin of :meth:`_obsolete_vp_ids`, for the other way a
        criterion can be gone: the plan **retired** a point (it says so
        with ``obsolete`` / ``obsolete_reason``), or the plan **never
        wrote the point down at all**. The retired-point rule can only
        reach the first — a point the plan never defined cannot be marked
        retired, so the report's FAILED entry for it survives every round,
        is counted as a failure every round, and produces a repair task
        for a judgement nobody can satisfy (``VP-031``, 2026-10-25).

        A point with an id but no criterion is a broken *criterion*, not a
        broken deliverable: nothing in the plan says what "passing" would
        mean, so no rework of the deliverable can satisfy it. The
        executor must not invent one, and the plan's owner has to void it
        or write its criterion down.

        **The two ways of not knowing are kept apart** — and both void
        nothing:

        * the plan file is missing / unreadable / malformed → the plan's
          wishes are unknown, and an unknown plan cannot say that a point
          is undefined. "The plan does not ask for it" and "we could not
          read what the plan asks for" are different facts.
        * the plan is readable but declares no verification point → a
          plan that never wrote a criterion down cannot have one voided
          by the judge, and the round must not pass on the strength of an
          empty plan (the boundary ``e394a52`` established).

        The classification is not invented here: an undefined point is
        ``criterion_defect`` by the explicit mapping in
        :data:`verification.failure_triage.SIGNAL_CLASSES`
        (``vp_definition_missing``) — the same criterion the dispatch
        guard (:func:`verification_agent.vp_dispatch_gaps`) and the repair
        gate use.
        """
        payload = self._read_verification_plan()
        if payload is None:
            # The plan could not be read: nothing is voided.
            return set()
        definitions = self._verification_points_of(payload)
        if not definitions:
            # The plan declares no point at all: nothing is voided.
            return set()
        failed_ids = self._extract_failed_ids(report)
        # The ids are compared as the plan spells them (``str`` + stripped)
        # — the same spelling :meth:`_obsolete_vp_ids` uses for the plan's
        # own ids, so the void sets and the failure count talk about the
        # same points.
        return {
            str(vp_id).strip() for vp_id in failed_ids
            if str(vp_id).strip() and str(vp_id).strip() not in definitions
        }

    def _triage_vp_payload(self, vp_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        """The verification point as the judge should see it.

        The judge's prompt reads the point's *definition* (title,
        expected result, verification method, test command) and may only
        quote from its *recorded failure* (actual result, evidence). The
        definition comes from ``verification_plan.json`` and the
        recorded failure from the report — the two halves of what the
        judge is allowed to know.
        """
        payload: Dict[str, Any] = {"id": vp_id}
        definition = self._verification_point_definitions().get(vp_id)
        if definition:
            payload.update(definition)
        for entry in self._report_failure_entries(report):
            if str(entry.get("id") or "").strip() != vp_id:
                continue
            payload.update(entry)
            break
        return payload

    def _triage_failure_history(
        self, vp_id: str, report: Dict[str, Any], round_number: int,
    ) -> List[Dict[str, Any]]:
        """The recorded failures of one point — this round included.

        The stored history holds the *previous* rounds (this round's
        entries are merged further down, after the loop-control
        decision), so the current round's record is appended here: the
        judge has to see the failure it is asked to classify.
        """
        history: List[Dict[str, Any]] = [
            dict(entry)
            for entry in (self._failure_history.get(vp_id) or [])
            if isinstance(entry, dict)
        ]
        for entry in self._report_failure_entries(report):
            if str(entry.get("id") or "").strip() != vp_id:
                continue
            history.append({
                "round": round_number,
                "actual_result": str(entry.get("actual_result", "") or ""),
                "evidence": str(entry.get("evidence", "") or ""),
                "failure_class": str(entry.get("failure_class", "") or ""),
            })
            break
        return history

    def _log_audit(
        self, event: str, message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """One structured line about the audit — never a reason to fail a round.

        Two loggers may be reachable: the injected ``self.logger`` (a
        structured logger that takes the event name and the payload as
        separate arguments) and the module logger (always present, but
        a plain :mod:`logging` logger that takes one format string).
        They are called in their own dialect, because the structured
        one cannot be called the stdlib way without losing the payload.

        Before this existed the orchestrator wrote its audit lines
        through ``self.logger`` only — an attribute nothing ever
        assigns — so every one of those lines was dropped on the floor.
        The module logger is the floor that is never missing.
        """
        payload = dict(data or {})
        for target in (getattr(self, "logger", None), logger):
            if target is None:
                continue
            try:
                if isinstance(target, logging.Logger):
                    target.warning(
                        "%s: %s", event, message,
                        extra={"event": event, "data": payload},
                    )
                else:
                    target.warning(event, message, data=payload)
                return
            except Exception:
                continue

    def _log_triage(self, vp_id: str, failure_class: str, message: str) -> None:
        """One log line about the triage — never a reason to fail a round."""
        self._log_audit(
            "verification_same_failure_triage",
            f"same-failure triage for {vp_id}: {message}",
            data={
                "plan_id": getattr(self.plan_dir, "name", "?"),
                "vp_id": vp_id,
                "failure_class": failure_class,
            },
        )

    def classify_cycle(
        self,
        key: Tuple[str, str],
        report: Dict[str, Any],
        round_number: int,
    ) -> Dict[str, Any]:
        """Ask the judge what the stuck failure *is*, and decide.

        One call, one answer, and the answer decides:

        * **A criterion defect with a usable replacement** → the
          criterion is replaced on disk (the broken one is obsoleted, a
          new one is added) and the loop continues with the new
          criterion. The replacement is only used when it checks at
          least as much as the criterion it replaces.
        * **A criterion defect without a replacement** → the loop
          stops: no amount of rework can satisfy a broken check, and
          nobody said what the check should be, so a human has to fix
          the criterion.
        * **Anything else** → the loop diverts into the repair path
          with a fresh budget for this failure (the triage HAS decided,
          so the same-cause counter starts over).
        * **A triage that malfunctioned** → the loop does *not* stop:
          the provider blew up, the answer was unreadable, or the quote
          it carried could not be verified. None of that is a
          statement about the failure, so the repair path gets its
          chance and the counter is left alone.
        * **A triage that ran and could not characterise the failure**
          → the loop stops with a structured reason: the loop cannot
          tell what to do with the failure, so a human has to look.

        The verdict it returns is *structured* — it names the point, the
        failure class and the triage's own words — because a human is
        the one who has to act on it. The machine-facing code travels
        with it (see :class:`TriageStopReason`): the state machine's
        vocabulary is a closed set of strings, and the chain-ending
        decision turns on the code, not on the verdict.

        Returns:
            ``{"should_stop": bool, "stop_reason": dict | None,
            "reason": str, "void_counter": bool}``. ``reason`` is the
            precise code of this verdict (the triage's own conclusion);
            the *coarse* one — "the chain converged" — is the code the
            reason carries to the state machine.
        """
        vp_id, failure_class = key
        try:
            judge = self._triage_judge_for()
            conclusion = judge.judge(
                vp=self._triage_vp_payload(vp_id, report),
                failure_history=self._triage_failure_history(
                    vp_id, report, round_number,
                ),
                round_number=round_number,
            )
        except Exception as exc:  # noqa: BLE001 — a broken judge is not a verdict
            self._log_triage(
                vp_id, failure_class, f"judge failed: {type(exc).__name__}: {exc}",
            )
            return {
                "should_stop": False,
                "stop_reason": None,
                "reason": REASON_TRIAGE_UNAVAILABLE,
                "void_counter": False,
            }

        parsed = _read_triage_conclusion(conclusion)
        if parsed is None:
            self._log_triage(
                vp_id, failure_class, "judge returned no usable conclusion",
            )
            return {
                "should_stop": False,
                "stop_reason": None,
                "reason": REASON_TRIAGE_UNAVAILABLE,
                "void_counter": False,
            }

        conclusive, concluded_class, rationale, available = parsed
        if not available:
            # 2026-10-23 (VP-019): the triage itself malfunctioned — the
            # provider blew up, the answer was unreadable, or the quote
            # it carried could not be verified against the recorded
            # failure. That is a statement about the triage, not about
            # the failure, so the loop must not stop on it: the repair
            # path gets its chance and the same-cause counter is left
            # alone. Stopping here would void a criterion over a triage
            # that never classified anything.
            self._log_triage(
                vp_id, failure_class, f"triage unavailable — {rationale}",
            )
            return {
                "should_stop": False,
                "stop_reason": None,
                "reason": REASON_TRIAGE_UNAVAILABLE,
                "void_counter": False,
            }

        if not conclusive:
            self._log_triage(vp_id, failure_class, "no conclusion — stopping")
            return {
                "should_stop": True,
                "stop_reason": {
                    "reason": STOP_REASON_NO_TRIAGE_CONCLUSION,
                    "vp_id": vp_id,
                    "failure_class": failure_class,
                    "triage_rationale": rationale,
                },
                "reason": STOP_REASON_NO_TRIAGE_CONCLUSION,
                "void_counter": False,
            }

        if concluded_class == CRITERION_DEFECT:
            # 2026-10-22 (criterion replacement): a broken criterion is
            # only a dead end when nobody knows what should replace it.
            # The judge is asked for exactly that, so the branch has
            # three outcomes, in this order:
            #
            # * the proposal was usable → the criterion is replaced on
            #   disk (the old one obsoleted, a new one added) and the
            #   loop continues with the new criterion;
            # * the proposal existed but could not be used (a weaker
            #   method, a write that failed) → the criterion stays and
            #   the failure is downgraded to a deliverable defect, so
            #   the loop keeps working instead of stopping on something
            #   it cannot repair;
            # * no proposal at all → a human has to decide what the
            #   criterion should be, so the loop stops.
            outcome = self._apply_criterion_replacement(
                vp_id, conclusion, round_number=round_number,
            )
            if outcome.get("applied"):
                record = dict(outcome.get("record") or {})
                self._log_triage(
                    vp_id, failure_class,
                    f"criterion defect — replaced by {record['replaced_by']}",
                )
                return {
                    "should_stop": False,
                    "stop_reason": None,
                    "reason": REASON_CRITERION_REPLACED,
                    "void_counter": True,
                    "obsoleted_vp_ids": [str(vp_id)],
                    "criterion_replacement": record,
                }
            if _read_triage_replacement(conclusion) is None:
                self._log_triage(
                    vp_id, failure_class,
                    "criterion defect — no replacement proposed, stopping "
                    "for a human",
                )
                return {
                    "should_stop": True,
                    "stop_reason": {
                        "reason": STOP_REASON_CRITERION_DEFECT,
                        "vp_id": vp_id,
                        "failure_class": failure_class,
                        "triage_rationale": rationale,
                    },
                    "reason": STOP_REASON_CRITERION_DEFECT,
                    "void_counter": False,
                }
            # A proposal that could not be used: the criterion stays as
            # it is, and the failure is treated as a deliverable defect
            # so the loop keeps repairing instead of stopping.
            #
            # 2026-10-23 (VP-020): the refusal is recorded, not just
            # survived. The reason travels with the round's decision, so
            # the operator can tell "the write failed" from "the
            # proposal checked less than the criterion it replaces" —
            # and the criterion is *not* treated as obsoleted, because
            # the plan on disk still says it is there.
            rejected_reason = str(
                outcome.get("replacement_rejected_reason") or ""
            )
            self._log_triage(
                vp_id, failure_class,
                "criterion defect — replacement refused, diverting into "
                "repair",
            )
            return {
                "should_stop": False,
                "stop_reason": None,
                "reason": DELIVERABLE_DEFECT,
                "void_counter": True,
                "replacement_rejected_reason": rejected_reason,
                "replacement_rejected_error": str(outcome.get("error") or ""),
            }

        # A deliverable defect (or a class the loop may retry / skip):
        # the loop keeps working on it — with a fresh budget, because
        # the triage HAS decided what the failure is.
        self._log_triage(
            vp_id, failure_class, f"{concluded_class} — diverting into repair",
        )
        return {
            "should_stop": False,
            "stop_reason": None,
            "reason": concluded_class,
            "void_counter": True,
        }

    def _criterion_replacement_guard(
        self, candidate: Dict[str, Any], summary: Dict[str, Any],
    ) -> None:
        """The guard that runs on the candidate plan **before** it lands.

        Two rules, and both of them are about the plan not losing a
        criterion:

        1. **The replacement must be runnable.** A method the runner
           does not know produces a point that can never produce a
           verdict — a criterion that is checked by nobody. Such a
           replacement is dropped.
        2. **A dropped replacement revives the original.** The obsolete
           mark is withdrawn, because "the old criterion died" and "the
           new criterion was refused" cannot both be true: the plan has
           to keep the criterion it had rather than lose it.

        The summary is left describing what will actually be on disk —
        :func:`verification_plan_delta._prune_summary` prunes it further
        against the candidate, so an emptied summary means "nothing to
        write" and the plan file is not touched at all.
        """
        added = [
            entry for entry in summary.get("added") or []
            if isinstance(entry, dict)
        ]
        obsoleted = [
            entry for entry in summary.get("obsoleted") or []
            if isinstance(entry, dict)
        ]
        by_id = {
            str(vp.get("id")): vp
            for vp in candidate.get("verification_points") or []
            if isinstance(vp, dict)
        }

        if not added:
            # Nothing to add: the criterion must not die either.
            self._withdraw_obsolete_marks(candidate, obsoleted)
            summary["obsoleted"] = []
            return

        from verification_subagent import SUPPORTED_METHODS

        rejected = set()
        for entry in added:
            vp = by_id.get(str(entry.get("id")))
            if vp is None:
                continue
            method = str(vp.get("verification_method") or "")
            if method not in SUPPORTED_METHODS:
                rejected.add(str(entry.get("id")))
        if rejected:
            candidate["verification_points"] = [
                vp for vp in candidate.get("verification_points") or []
                if str(vp.get("id")) not in rejected
            ]
            summary["added"] = [
                entry for entry in summary.get("added") or []
                if str(entry.get("id")) not in rejected
            ]
            summary["rejected_by_guard"] = sorted(rejected)
            # The replacement is gone → the original criterion stays.
            self._withdraw_obsolete_marks(candidate, obsoleted)
            summary["obsoleted"] = []
            return

        # The replacement landed: name it in the obsolete reason, so the
        # plan alone answers "this criterion is gone — that one took its
        # place" (the audit chain the delta module asks for).
        replacement_id = str(added[0].get("id"))
        for entry in obsoleted:
            old = by_id.get(str(entry.get("id")))
            if old is None:
                continue
            reason = str(old.get(OBSOLETE_REASON_FIELD) or "").strip()
            old[OBSOLETE_REASON_FIELD] = (
                f"{reason} (replaced by {replacement_id})".strip()
            )

    @staticmethod
    def _withdraw_obsolete_marks(
        candidate: Dict[str, Any], obsoleted: List[Dict[str, Any]],
    ) -> None:
        """Take the obsolete mark back off the points a delta wanted to retire.

        The mark is what makes the executor skip a point, so a mark that
        survives without a replacement does not retire anything — it
        just hides a criterion that is still there and still failing.
        """
        ids = {str(entry.get("id")) for entry in obsoleted or []}
        if not ids:
            return
        for vp in candidate.get("verification_points") or []:
            if not isinstance(vp, dict):
                continue
            if str(vp.get("id")) not in ids:
                continue
            vp.pop(OBSOLETE_FIELD, None)
            vp.pop(OBSOLETE_REASON_FIELD, None)
            vp.pop(OBSOLETE_ROUND_FIELD, None)

    def _audit_plan_delta(
        self, event: str, vp_id: str, error: str,
        replacement_rejected_reason: str,
    ) -> None:
        """Write one plan-delta audit event where it cannot be lost.

        The decision this event records is one the operator has to be
        able to read afterwards: the criterion was **not** replaced, and
        why. It therefore goes to disk — the round's own audit trail
        (the verification agent's persistence, the same
        ``write_verification_point_log`` shape every other per-point
        event uses) and the plan's durable audit file, which does not
        depend on a round log being open.

        The event is filed under :data:`PLAN_DELTA_AUDIT_VP_ID`: it is
        about the plan, not about one check. The point whose criterion
        was to be replaced travels in the payload.

        Args:
            event: The event name (e.g. ``plan_delta_persist_failed``).
            vp_id: The point whose criterion was to be replaced.
            error: What went wrong, in words.
            replacement_rejected_reason: The machine-readable reason
                (e.g. ``persist_failed``).
        """
        payload = {
            "plan_id": getattr(self.plan_dir, "name", "?"),
            "replaced_vp_id": str(vp_id),
            "event": event,
            "error": error,
            "replacement_rejected_reason": replacement_rejected_reason,
        }
        # (1) The round's own audit trail — the same per-point event
        #     shape every other verification event uses.
        agent = getattr(self, "verification_agent", None)
        writer = getattr(agent, "_safe_log_freshness_event", None)
        if callable(writer):
            try:
                writer(PLAN_DELTA_AUDIT_VP_ID, event, dict(payload))
            except Exception:
                pass
        # (2) The plan's durable audit file — independent of an open
        #     round log, so the event cannot be lost because the round
        #     was not running.
        try:
            from verification_persistence import write_audit_event

            write_audit_event(
                self.plan_dir, PLAN_DELTA_AUDIT_VP_ID, event, dict(payload),
            )
        except Exception:
            pass
        # (3) The structured log line.
        self._log_audit(event, error, data=payload)

    def _refuse_replacement(
        self, vp_id: str, reason: str, error: str,
    ) -> Dict[str, Any]:
        """Record a refused replacement and say what it cost.

        A refusal is not a no-op: the criterion was supposed to be
        replaced and was not, so the refusal leaves an audit event on
        disk and a reason in the round's record. The plan itself is
        untouched — the refusal never claims a change that did not
        happen.

        Args:
            vp_id: The point whose criterion was to be replaced.
            reason: The machine-readable reason (see
                :data:`_REJECTION_EVENTS`).
            error: What went wrong, in words.

        Returns:
            The outcome the caller reads: nothing was written, and
            this is why.
        """
        event = _REJECTION_EVENTS.get(
            reason, EVENT_PLAN_DELTA_REPLACEMENT_REJECTED,
        )
        self._audit_plan_delta(event, vp_id, error, reason)
        self._log_triage(vp_id, CRITERION_DEFECT, f"replacement refused — {error}")
        return {
            "applied": False,
            "record": None,
            "replacement_rejected_reason": reason,
            "error": error,
        }

    def _apply_criterion_replacement(
        self,
        vp_id: str,
        conclusion: Any,
        expected_digest: Optional[str] = None,
        round_number: int = 0,
    ) -> Dict[str, Any]:
        """Replace a broken criterion with the one the triage proposed.

        The whole branch is one promise: **the disk and the verdict
        store must agree**. The plan file says the old criterion is
        obsolete and the new one replaced it; the verdict store records
        the same fact. Neither may claim something the other does not.

        The order is what makes that promise hold:

        1. the proposal is read and checked — a replacement that checks
           less than the criterion it replaces
           (:func:`verification.failure_triage.is_not_looser`) is
           refused, and a refused replacement touches **nothing**;
        2. the delta is applied to a copy and persisted atomically
           (:func:`verification_plan_delta.apply_delta_persisted`), so a
           failed write leaves the plan exactly as it was;
        3. only **after** the write succeeded is the OBSOLETE verdict
           appended — a verdict that outlives the change it describes
           would be worse than no verdict at all.

        A refusal at any step writes **nothing** and says why: the
        outcome carries ``replacement_rejected_reason`` and an audit
        event has landed on disk (see :meth:`_refuse_replacement`). The
        caller must then treat the failure as something else (see
        :meth:`classify_cycle`).

        Args:
            vp_id: The point whose criterion is broken.
            conclusion: The triage verdict (a
                :class:`verification.triage_agent.TriageConclusion` or a
                mapping carrying the same fields). Its ``replacement``
                is the proposal.
            expected_digest: The sha256 of the plan file the caller
                based its decision on. When it does not match the file
                on disk, the plan changed underneath and the
                replacement is refused rather than overwriting whoever
                wrote it.
            round_number: The round the replacement belongs to (written
                into the plan as ``obsoleted_round`` / ``added_round``).

        Returns:
            The outcome of the attempt: ``{"applied": bool, "record":
            dict | None, "replacement_rejected_reason": str, "error":
            str}``. On success ``record`` is the replacement's record
            — ``{"vp_id", "failure_class", "replaced_by"}`` — and the
            reason is empty; on a refusal ``record`` is ``None`` and
            the reason says why nothing was written.
        """
        replacement = _read_triage_replacement(conclusion)
        if not replacement:
            # Nothing was proposed, so nothing was refused: a human
            # has to say what the criterion should be.
            return {
                "applied": False,
                "record": None,
                "replacement_rejected_reason": "",
                "error": "the triage proposed no replacement",
            }

        plan_file = Path(self.plan_dir) / "verification_plan.json"
        try:
            raw = plan_file.read_bytes()
        except OSError as exc:
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                f"the plan file could not be read: {exc}",
            )
        digest = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None and expected_digest != digest:
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                "the plan changed underneath the replacement",
            )
        try:
            plan_data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                f"the plan file is not readable JSON: {exc}",
            )
        if not isinstance(plan_data, dict):
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                "the plan file does not hold a plan object",
            )

        original = None
        for vp in plan_data.get("verification_points") or []:
            if isinstance(vp, dict) and str(vp.get("id")) == str(vp_id):
                original = vp
                break
        if original is None:
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                "the point is not in the plan",
            )

        original_method = str(original.get("verification_method") or "")
        replacement_method = str(replacement.get("verification_method") or "")
        if not is_not_looser(original_method, replacement_method):
            # A weaker check is a rubber stamp. Refuse it and touch
            # nothing — the plan stays byte-for-byte what it was.
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_WEAKER_METHOD,
                f"{replacement_method!r} is not at least as strong as "
                f"{original_method!r}",
            )

        title = str(replacement.get("title") or "").strip()
        if not title:
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_INVALID_PROPOSAL,
                "the proposal carries no title",
            )

        rationale = ""
        if isinstance(conclusion, Mapping):
            rationale = str(conclusion.get("rationale") or "")
        else:
            rationale = str(getattr(conclusion, "rationale", "") or "")

        added_item: Dict[str, Any] = {
            "title": title,
            "verification_method": replacement_method,
            "reason": (
                f"replaces {vp_id} — the triage named the criterion broken"
                + (f": {rationale}" if rationale else "")
            ),
            REPLACEMENT_FOR_FIELD: str(vp_id),
        }
        # The replacement stands in for the same criterion, so it keeps
        # the criterion's place in the plan: the PRD criterion it
        # answers for, the phase it runs in, and the payload the runner
        # needs. Anything the proposal states wins; anything it leaves
        # out is inherited rather than invented.
        for key in ("related_prd_criteria", "verification_phase"):
            if key in replacement:
                added_item[key] = replacement[key]
            elif original.get(key) is not None:
                added_item[key] = original[key]
        for key in (
            "request", "assertions", "target_url", "timeout_seconds",
            "execution_group",
        ):
            if key in replacement:
                added_item[key] = replacement[key]

        delta = PlanDelta(
            added=[added_item],
            obsoleted=[{
                "id": str(vp_id),
                "reason": (
                    "criterion defect — the triage named this criterion "
                    "broken and proposed a replacement"
                ),
            }],
        )
        try:
            summary = apply_delta_persisted(
                plan_file, plan_data, delta, round_number,
                post_apply=self._criterion_replacement_guard,
                expected_digest=digest,
            )
        except PlanDeltaPersistError as exc:
            # The plan on disk is untouched; nothing may be claimed.
            # The failure is audited, because "the plan is unchanged"
            # is not the same as "nothing happened".
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                f"the delta did not land: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — a failed delta is a refusal
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                f"{type(exc).__name__}: {exc}",
            )

        if not summary.get("added") or not summary.get("obsoleted"):
            # The guard refused the replacement: the plan was not
            # written, so nothing may be claimed about it.
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_INVALID_PROPOSAL,
                "the guard rejected the new criterion",
            )
        obsoleted_id = str(summary["obsoleted"][0].get("id"))
        if obsoleted_id != str(vp_id):
            return self._refuse_replacement(
                vp_id, REPLACEMENT_REJECTED_PERSIST_FAILED,
                f"the delta obsoleted {obsoleted_id!r} instead of {vp_id!r}",
            )
        replaced_by = str(summary["added"][0].get("id"))

        # The plan file says it. Only now may the verdict store say it
        # too.
        verdict = {
            "vp_id": str(vp_id),
            "failure_class": CRITERION_DEFECT,
            "replaced_by": replaced_by,
        }
        if self.verif_repo is not None:
            try:
                self.verif_repo.append_verdict(self.plan_dir.name, verdict)
            except Exception as exc:  # noqa: BLE001 — best-effort record
                self._log_triage(
                    vp_id, CRITERION_DEFECT,
                    f"replacement landed but the verdict was not recorded: {exc}",
                )
        else:
            self._log_triage(
                vp_id, CRITERION_DEFECT,
                "replacement landed but there is no verdict store to "
                "record it in",
            )
        return {
            "applied": True,
            "record": dict(verdict),
            "replacement_rejected_reason": "",
            "error": "",
        }

    def _load_failure_history(self) -> Dict[str, List[Dict[str, Any]]]:
        """Per-VP failure history for this plan.

        Disk file (``verification_failure_history.json``) is
        authoritative once written — that is what makes the history
        survive the fresh orchestrator the auto-loop builds for every
        post-repair round. When the file is absent (a plan already
        mid-flight when this feature shipped), seed once from the
        accumulated ``plan_verification.verdicts`` list so the very next
        repair generation already knows how each VP failed before.
        """
        from verification import failure_history as _fh

        history = _fh.load(self.plan_dir)
        if history:
            return history
        verdicts = None
        try:
            if self.verif_repo is not None:
                summary = self.verif_repo.summary(self.plan_dir.name)
                if isinstance(summary, dict):
                    verdicts = summary.get("verdicts")
        except Exception:
            verdicts = None
        if not verdicts:
            return {}
        seeded = _fh.seed_from_verdicts(verdicts)
        if seeded:
            print(
                f"[Orchestrator] seeded failure history from verdicts for "
                f"{len(seeded)} VP(s)"
            )
        return seeded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_verification_cycle(self, round_number: int = 1, force: bool = False, resume: bool = False) -> dict:
        """
        Start a new verification cycle.

        Args:
            round_number: Current verification round number (default: 1)
            force: If True, always re-run verification even if a report exists.
            resume: If True, skip the verdict-cache clear so already-PASSED
                VPs (per the on-disk ``verification-executor sidecar``
                verdict map) are filtered out by
                :meth:`BaseExecutor.run`'s ``completed_set`` check. The
                default ``False`` keeps the historic "always start from
                a clean slate" behaviour that protects against stale
                verdicts from a previous ``verification_plan.json``.
        """
        # 2026-08-19 audit: the orchestrator's ``PlanState`` instance
        # is long-lived but its in-memory ``_state`` cache can drift
        # from the SQLite row whenever another writer (the ``/start``
        # handler, the auto-loop, ``begin_verification``) updates the
        # plan. Reload BEFORE every cycle so the cycle's
        # ``transition_to`` calls see the latest persisted phase and
        # do not raise ``Illegal transition`` (which the callers
        # silently swallow, leaving the plan stuck in
        # ``verification_running``).
        self.plan_state.reload()
        self._waiting_for_user = False
        current = self.plan_state.get_current_phase()
        if current in ("executing", "ready", "completed", "verification_repairing"):
            self.plan_state.transition_to("verification")
        # After a closed-out cycle (``verification_passed`` /
        # ``verification_failed`` / ``verification_loop_stopped`` /
        # the top-level ``failed`` / ``stopped`` / ``completed``
        # terminals) the orchestrator may want to re-run verification
        # from scratch — the user just confirmed a repair, hit
        # ``POST /api/verification/{id}/start`` after a previous run
        # crashed mid-round, or a manual force-verify is in flight.
        # ``transition_to`` refuses terminals by design (they are the
        # canonical end states), so we call ``force_set_phase`` to
        # drop back to ``verification`` and let the next line advance
        # into ``verification_running``.
        #
        # The security boundary is preserved at the plan_state layer
        # (no edge from ``verification_passed`` to ``completed``); the
        # orchestrator simply chooses to leave the terminal before
        # crossing that boundary. The dispatcher only treats a plan
        # as truly closed when the orchestrator writes a fresh
        # ``verification_passed`` afterwards; an intermediate
        # ``force_set_phase("verification")`` round-trip is fine.
        #
        # 2026-09-07 fix: previously only the three ``verification_*``
        # terminals were force_set back to ``verification``. A plan
        # whose previous run crashed mid-round and left
        # ``current_phase=failed`` (the general-purpose terminal, not
        # the verification-specific one) would then hit the direct
        # ``transition_to("verification_running")`` below and raise
        # ``Illegal transition from 'failed' to 'verification_running'``,
        # which the caller silently swallows and strands the plan.
        # Widening the catch-all to all terminal phases here is safe
        # because the routing CAS at the ``/start`` HTTP entry already
        # vetted that this call is a legitimate re-entry (source
        # stages include ``terminal_failed``, ``terminal_done``,
        # ``verification_idle``, ``verification_passed``,
        # ``verification_failed``, ``verification_loop_stopped``).
        if current in (
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
            "completed",
            "failed",
            "stopped",
        ):
            self.plan_state.force_set_phase("verification")
        # verification_rerunning -> verification_running directly (skip verification phase)
        self.plan_state.transition_to("verification_running")

        # Short-circuit: if the plan has no actionable tasks, there is
        # nothing to verify. Returning a passed report immediately prevents
        # the verification pipeline from burning LLM tokens / hanging on VPs
        # that only make sense when real tasks exist. This keeps empty-task
        # boundary tests fast and deterministic.
        tasks_file = self.plan_dir / "tasks.json"
        if tasks_file.exists():
            try:
                tasks_data = json.loads(tasks_file.read_text(encoding="utf-8"))
                tasks = tasks_data.get("tasks") if isinstance(tasks_data, dict) else tasks_data
                if not tasks:
                    self.plan_state.verification_passed()
                    return {
                        "overall_status": "PASSED",
                        "verification_results": [],
                        "requirement_deviations": [],
                        "summary": "tasks.json is empty; no tasks to verify.",
                        "execution_profile": {"mode": "short_circuit_empty_tasks"},
                    }
            except Exception:
                # Corrupt tasks.json is not fatal; fall through to normal
                # verification so any issues are surfaced as verification failures.
                pass

        try:
            # NOTE: run_full_verification() doesn't accept a force kwarg in this version.
            # The "force" semantics is already handled at the API layer (manual-verification
            # loop always runs all 3 rounds when force=True). Drop the kwarg here.
            #
            # 2026-07-19: ``resume`` plumbs through to the agent which
            # then skips the "clear verdict cache" guard. That lets
            # :meth:`BaseExecutor.run` filter out VPs whose
            # ``_completed_items`` is already populated from the
            # verdict cache (see base_executor.py:344). The default
            # False preserves the historic "always re-run" safety
            # net for first-time starts; the orchestrator flips it to
            # True when resuming a mid-run cycle so we don't burn
            # LLM tokens re-running VPs whose verdicts are already
            # cached on disk.
            #
            # 2026-09-08: snapshot the previous round's
            # ``verification_results`` into ``rounds[N-1]`` and clear
            # the top-level ``verification_results`` so the Feishu
            # card doesn't show stale FAILED entries from a prior
            # round while round N is running. This is the single-line
            # fix for the VP-034 stale-data bug (round 1 showed
            # round 0's FAILED entry in the card even though round 1
            # was actively re-running VP-034).
            from verification.verification_report_reader import (
                snapshot_round_results,
                clear_round_results,
            )
            report_path = self.plan_dir / "verification_report.json"
            if report_path.exists() and not resume:
                try:
                    snapshot_round_results(report_path, round_number)
                    clear_round_results(report_path)
                except Exception as snapshot_exc:
                    # Best-effort — never crash round start over a
                    # snapshot/clear failure. The card may show stale
                    # entries but the round still runs.
                    if self.logger:
                        self.logger.warning(
                            "round_start_snapshot_failed",
                            f"could not snapshot/clear verification_results "
                            f"at round {round_number} start: {snapshot_exc}",
                        )

            report = self.verification_agent.run_full_verification(
                round_number, resume=resume
            )
            return report
        except Exception:
            self.plan_state.verification_failed()
            raise

    def check_cycle_conditions(self, report: dict, round_number: int = 1) -> dict:
        """
        Evaluate the verification report and decide the next step.

        Returns a dict with keys:
        - should_continue: bool
        - should_stop: bool
        - stop_reason: Optional[str]
        - repair_tasks: List[Dict]
        - waiting_for_user: bool
        - status: str   # passed | loop_stopped | verification_failed
        - intent_conflicts: List[Dict]   # 2026-10-23: the repair rows
          that crossed the plan's own constraints and were dropped
        - needs_operator_decision: bool   # 2026-10-23: True when
          ``intent_conflicts`` is non-empty — only an operator can say
          whether the constraint or the requirement gives way
        """
        overall_status = report.get("overall_status", "FAILED")

        # 2026-08-19 audit: reload BEFORE classifying the cycle so the
        # subsequent ``transition_to`` reflects the latest SQLite state.
        # Without this, a Phase 3 LLM verdict that takes >30s can race
        # with a parallel writer and the orchestrator's cached
        # ``current_phase`` is stale, causing ``transition_to`` to
        # raise ``Illegal transition`` that the caller silently
        # swallows — the plan then stays in ``verification_running``
        # even though the verdict was ``PASSED``.
        self.plan_state.reload()

        # 2026-10-25 (VP-031): a criterion that is **gone** must not pin
        # the plan at FAILED. There are two ways a criterion can be gone:
        # the plan retired the point (``obsolete`` / ``obsolete_reason``),
        # or the plan never wrote the point down at all. The retired-point
        # rule could only reach the first — a point the plan never defined
        # cannot be marked retired, so its FAILED entry survived every
        # round, was counted as a failure every round, and produced a
        # repair task for a judgement nobody can satisfy.
        #
        # Both sets are taken out **before** the failure count is read, so
        # the round's verdict is decided by the criteria the plan actually
        # asks for. The report keeps its entries — they are the evidence,
        # and the operator needs to see them.
        obsolete_vp_ids = self._obsolete_vp_ids()
        undefined_vp_ids = self._undefined_vp_ids(report)
        void_vp_ids = obsolete_vp_ids | undefined_vp_ids
        # What the operator has to do about the points the plan never
        # defined — void them, or express their criterion. The judge layer
        # did not judge those points, so it says so rather than dressing
        # the record up as a pass.
        operator_action = _undefined_vp_operator_action(undefined_vp_ids)
        if operator_action:
            print(f"[Verification] round {round_number}: {operator_action}")

        # Count actual failures (excluding SKIPPED/manual_check)
        # The voided points are compared with the ids as the plan spells
        # them (stripped) — the same spelling the void sets hold.
        failed_count = sum(
            1 for r in report.get("verification_results", [])
            if r.get("status") == "FAILED"
            and str(r.get("id") or "").strip() not in void_vp_ids
        )
        deviation_count = len(report.get("requirement_deviations", []))

        # PASSED or PARTIAL with no real failures → treat as passed
        if overall_status == "PASSED" or (overall_status == "PARTIAL" and failed_count == 0 and deviation_count == 0):
            self._previous_failed_ids = None
            self._consecutive_same_failure_rounds = 0
            self._last_round = round_number
            self._save_loop_tracking()
            # All failures resolved — drop the per-VP feedback history
            # so the next plan (if this orchestrator is reused) starts
            # with a clean slate. Persist the empty state too: a later
            # post-repair round builds a fresh orchestrator that would
            # otherwise resurrect the pre-pass history from disk.
            self._failure_history.clear()
            try:
                from verification import failure_history as _fh_clear
                _fh_clear.save(self.plan_dir, {})
            except Exception:
                pass
            self.plan_state.verification_passed()
            return {
                "should_continue": False,
                "should_stop": False,
                "stop_reason": None,
                "repair_tasks": [],
                # 2026-10-23 — the gate never ran on a passing round, so
                # there is nothing to report. The fields are present on
                # every return path so a consumer can read them without
                # guarding for their absence.
                "intent_conflicts": [],
                "needs_operator_decision": False,
                "waiting_for_user": False,
                "status": "passed",
                # 2026-10-25 (VP-031): the points this round voided. A
                # round that passes while a point the plan never defined
                # is still in the report must say so — the pass is not a
                # judgement about that point, and the operator has to void
                # it or express its criterion.
                "void_vp_ids": sorted(void_vp_ids),
                "undefined_vp_ids": sorted(undefined_vp_ids),
                "operator_action": operator_action,
            }

        # --- Failure path ---
        # 2026-10-22 (retired criteria): a point the plan retired is
        # not a failure any more. The report still lists it — the round
        # that retired it is the round that saw it fail — so the
        # retired points are taken out of the failure set *here*, before
        # the same-cause counter reads it and before the repair path
        # builds tasks from it. Otherwise a criterion that no longer
        # exists keeps the counter moving (and ends the chain on "the
        # same failure, again") and keeps producing repair tasks for a
        # judgement nobody can satisfy.
        #
        # 2026-10-25 (VP-031): the same holds for a point the plan never
        # defined (``_undefined_vp_ids``) — it is taken out of the
        # failure set here too, so the same-cause counter is not moved by
        # a criterion that does not exist and the repair path never sees
        # it.
        current_failed_ids = (
            self._extract_failed_ids(report) - void_vp_ids
        )
        # 2026-10-22 (v2): the same-cause comparison is per
        # ``(verification point, failure class)`` pair. The pre-v2 code
        # compared the whole failed-point set, so a point that kept
        # failing for a *different* reason counted as the same cause —
        # the very distinction this file exists to make.
        current_failed_keys = {
            key for key in self._extract_failed_keys(report)
            if key[0] not in void_vp_ids
        }

        # (2) Same-failure detection — 2026-09-12 fix: don't terminate
        # the chain on the first repeat. Real bugs (e.g. VP-006
        # needs an actual code change to remove VP-013 instrumentation;
        # VP-023 needs docker compose setup; VP-027 needs coverage work)
        # can't be fixed in one round. The chain is meant to run: if a VP
        # fails, generate repair tasks, route to executor, keep
        # iterating.
        #
        # Old behaviour: same failures on two consecutive rounds →
        # return empty repair_tasks → auto-loop records
        # ``no_repair_tasks`` terminal → chain dies (this is the bug
        # we are fixing right now).
        #
        # New behaviour: bump ``_consecutive_same_failure_rounds``;
        # only terminate after ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS``
        # rounds with the same failure set AND no executor pass
        # intervened. Otherwise still generate repair_tasks so the
        # executor gets another shot.
        #
        # 2026-10-22 (v2): the count is per decision key, so a key that
        # failed in the previous round *and* in this one repeated, while
        # a key that failed for the first time (or that changed its
        # failure class) starts over.
        #
        # The count is the number of rounds the failure has repeated —
        # one on the round it first failed. The counts of the failures
        # that stopped are kept: the file is a tally of what each
        # failure has cost, not just of what failed last.
        previous_failed_keys = self._previous_failed_keys
        counts = dict(self._same_failure_counts)
        for key in current_failed_keys:
            name = _decision_key_str(key)
            if not _is_counted_failure(key):
                # An infrastructure blip (or an out-of-scope check) is
                # not evidence that the work is wrong — it must not
                # move the same-cause counter.
                continue
            counts[name] = (
                self._same_failure_counts.get(name, 0) + 1
                if key in previous_failed_keys
                else 1
            )
        self._same_failure_counts = counts
        # The keys of *this* round — the counts above are now the
        # current round's view of what repeated.
        self._previous_failed_keys = current_failed_keys

        # 2026-10-22 (triage): reaching the cap no longer terminates the
        # chain on its own. It buys ONE semantic triage call, and the
        # triage's verdict decides between stopping (a human has to
        # look) and diverting into the repair path.
        #
        # 2026-10-22 (criterion replacement): a third outcome — the
        # triage's proposal was used, and the criterion was replaced on
        # disk. The points that were obsoleted this round are recorded
        # here, because the report still lists them as failed: their
        # criterion no longer exists, so reworking the deliverable for
        # them is work nobody asked for.
        obsoleted_vp_ids: List[str] = []
        criterion_replacements: List[Dict[str, Any]] = []
        # 2026-10-23 (VP-020): why a proposed replacement was not used
        # (a write that failed, a proposal that checked less). The
        # round's record carries it, so the operator can read *why* the
        # criterion was not replaced instead of finding no replacement
        # and no explanation.
        replacement_rejected_reason: Optional[str] = None
        stuck_keys = self._stuck_failure_keys()
        if stuck_keys:
            decision = self.classify_cycle(
                stuck_keys[0], report, round_number,
            )
            obsoleted_vp_ids = [
                str(vp_id) for vp_id in decision.get("obsoleted_vp_ids") or []
            ]
            if decision.get("criterion_replacement"):
                criterion_replacements.append(
                    dict(decision["criterion_replacement"]),
                )
            if decision.get("replacement_rejected_reason"):
                replacement_rejected_reason = str(
                    decision["replacement_rejected_reason"]
                )
            if decision.get("should_stop"):
                # Genuinely stuck, and the triage said why. The
                # machine-facing code (the state machine's vocabulary is
                # closed) is the convergence verdict; the structured
                # triage verdict below is what the caller — and the
                # human reading the card — needs.
                self._safe_phase_call(
                    "verification_failed", "verification_failed",
                )
                stop_payload = {
                    "should_continue": False,
                    "should_stop": True,
                    # The machine-facing code, spelled out as a literal
                    # on purpose: the vocabulary guard in
                    # ``tests/unit/test_verification_terminal_state_machine.py``
                    # reads this function's return literals to prove that
                    # every reason it can return is classified as
                    # chain-ending. It is the one spelling of this code
                    # in the function — the reason below is built from
                    # it. The *precise* reason — a broken criterion, or a
                    # triage that could not conclude — is the verdict
                    # the reason carries.
                    "stop_reason": "same_failure_repeated_after_max_attempts",
                    "repair_tasks": [],
                    # 2026-10-23 — the gate never ran on this path.
                    "intent_conflicts": [],
                    "needs_operator_decision": False,
                    "waiting_for_user": False,
                    "status": "loop_stopped",
                    # 2026-10-25 (VP-031): the points this round voided —
                    # the operator still has to act on them, and the
                    # terminal verdict has to say which ones.
                    "void_vp_ids": sorted(void_vp_ids),
                    "undefined_vp_ids": sorted(undefined_vp_ids),
                    "operator_action": operator_action,
                }
                # The reason is the code *and* the verdict: a string to
                # every consumer that compares a stop reason against the
                # state machine's vocabulary, and the triage's own
                # words to the human who has to act on it.
                stop_payload["stop_reason"] = TriageStopReason(
                    stop_payload["stop_reason"],
                    decision.get("stop_reason") or {},
                )
                self.plan_state.stop_verification_loop(
                    stop_reason_code(stop_payload)
                )
                self._save_triage_verdict(
                    decision.get("stop_reason") or {}, round_number,
                )
                return stop_payload
            if decision.get("void_counter"):
                # The triage decided what this failure is, so the
                # same-cause counter starts over for it: the loop has
                # earned a fresh budget instead of a stop.
                self._same_failure_counts[
                    _decision_key_str(stuck_keys[0])
                ] = 0
        # Not yet at the cap — log and continue, generating
        # repair tasks again so the executor can try once more.
        _logger = getattr(self, "logger", None)
        if _logger is not None and self._consecutive_same_failure_rounds > 0:
            try:
                _logger.warning(
                    "verification_same_failures_persisting",
                    f"Same failures persisted for "
                    f"{self._consecutive_same_failure_rounds}/"
                    f"{self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS} rounds — "
                    f"regenerating repair tasks",
                    data={
                        "plan_id": getattr(self.plan_dir, "name", "?"),
                        "failed_vps": sorted(current_failed_ids),
                        "repeated_keys": sorted(
                            _decision_key_str(key)
                            for key in current_failed_keys
                            if self._same_failure_counts.get(
                                _decision_key_str(key), 0
                            ) > 1
                        ),
                        "consecutive_rounds": self._consecutive_same_failure_rounds,
                        "max_consecutive_rounds": self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS,
                    },
                )
            except Exception:
                pass

        # (1) Round limit — soft only. Verification must keep
        # iterating until all failures are fixed (or same-failure-repeated
        # termination above fires). max_rounds is now a *soft* limit used
        # only for logging/observability, not for stopping the loop.
        current_round = self.plan_state.get_verification_round()
        max_rounds = self.plan_state.get_verification_max_rounds()
        if current_round >= max_rounds:
            # Soft cap: log a warning but keep going. Only same-failure-repeated
            # above (and explicit user_stop_verification) actually terminate.
            print(
                f"[Verification] WARNING: reached max_rounds={max_rounds}, "
                f"but continuing — all failures must be fixed)."
            )

        # Can continue — enter repair phase and generate repair tasks
        # for the executor to attempt a fix.
        #
        # 2026-09-14 (live post-mortem, round 3): this call used to be
        # FATAL. The watchdog had already force-stamped the plan to
        # ``failed`` (a false ``verification_log_stale``), so
        # ``transition_to("verification_failed")`` raised
        # ``Illegal transition from 'failed' to 'verification_failed'`` —
        # the exception unwound the whole auto-loop, and a round whose
        # judgment had COMPLETED (report written, 5 failed VPs) produced
        # no repair tasks and no split decision at all. Phase bookkeeping
        # must never be able to destroy the round's actual work, so it is
        # now best-effort with a force-set fallback.
        self._safe_phase_call("verification_failed", "verification_failed")
        # 2026-10-22 (v2): the recorded state is the round's decision
        # keys (point + failure class), not the bare point ids — those
        # were already set above, where the counts were computed.
        self._last_round = round_number
        # 2026-09-19: record the round's failure set (and the repeat
        # counter that was bumped / reset just above) on disk. This is
        # the only write the comparison at the top of the next round
        # reads, and the next round runs in a FRESH orchestrator — see
        # the ``__init__`` note.
        self._save_loop_tracking()

        # 2026-09-12: track failure history per
        # VP across rounds. When the same VP fails again, the next
        # round's repair-task-generation prompt includes the previous
        # attempts' actual_result + evidence so the LLM agent knows
        # "上次方案完全无效" and is steered away from repeating it.
        # A hard "auto-cap at 3 rounds" was rejected in favour of this —
        # feedback gives the agent more information to learn from, while
        # still capping the chain so a truly stuck situation eventually
        # terminates.
        round_failed_vps = self._extract_failed_vps_with_evidence(report)
        from verification import failure_history as _failure_history_mod

        _failure_history_mod.merge_round(
            self._failure_history, round_failed_vps, round_number,
        )
        # Drop history entries for VPs that have now passed (so the
        # next round's prompt isn't polluted with stale failures), then
        # persist so a freshly-constructed orchestrator (the auto-loop
        # builds one per post-repair round) reads the same history back.
        # 2026-10-25 (VP-031): a point the plan never defined is **not**
        # a failure of the deliverable, but it is still an open item — the
        # plan's owner has to void it or express its criterion. Pruning it
        # away would erase the audit trail of a record nobody may repair,
        # so it stays in the history: ``save`` settles it to
        # ``criterion_defect`` and marks it reconciled, and the file keeps
        # it for the operator to look at.
        _failure_history_mod.prune(
            self._failure_history, current_failed_ids | undefined_vp_ids,
        )

        _failure_history_mod.save(self.plan_dir, self._failure_history)

        # Build feedback dict: vp_id -> previous attempts (excluding
        # the current round). Empty dict on first occurrence.
        previous_failure_feedback: Dict[str, List[Dict[str, Any]]] = {}
        for vp_id, history in self._failure_history.items():
            prior = [h for h in history if h.get("round") != round_number]
            if prior:
                previous_failure_feedback[vp_id] = prior

        # Phase bookkeeping only — see ``_safe_phase_call`` (2026-09-14:
        # a watchdog force-stamp must not be able to abort the repair /
        # split decision).
        self._safe_phase_call("start_verification_repair", "verification_repairing")
        # 2026-09-07: switch from the legacy three-step LLM chain
        # (``generate_verification_tasks`` →
        # ``understand_requirements`` + ``collect_failure_evidence`` +
        # ``_generate_repair_tasks``) to the single-call content flow
        # + :class:`RepairTaskAssembler` split. The legacy chain
        # produced zero tasks for any plan whose evidence didn't
        # perfectly align with PRD criteria, leaving the auto-loop
        # stuck in ``verification_failed`` with no path forward. The
        # new flow reads ``verification_report.json`` directly via
        # :func:`extract_failed_vps_with_paths` and stamps
        # ``id / priority / depends_on`` locally so the state machine
        # can recognise the tasks regardless of LLM behaviour.
        #
        # 2026-09-13: switched from ``extract_failed_vps_from_report`` to
        # the path-based sibling. The inlining reader truncated each VP's
        # ``actual_result`` / ``evidence`` to 1000 chars — for a VP whose
        # whole point is "which of the 245 failing tests", that dropped
        # exactly the actionable part. The path-based shape carries a
        # bounded summary plus the file paths the repair agent Reads on
        # demand, so the prompt stays bounded and nothing is lost.
        from verification.verification_report_reader import (
            extract_failed_vps_with_paths,
            extract_vp_test_commands,
        )
        from repair_generator import RepairGenerationError, RepairTaskAssembler

        failed_vps = extract_failed_vps_with_paths(
            self.plan_dir / "verification_report.json",
            self.plan_dir / "verification_plan.json",
        )
        # 2026-10-22 (retired criteria): the same filter the failure
        # set got above — a point the plan retired produces no repair
        # task. Its criterion is gone, so reworking the deliverable for
        # it is work nobody asked for.
        retired_failed_vps = [
            vp for vp in failed_vps
            if str(vp.get("id") or "") in obsolete_vp_ids
        ]
        if retired_failed_vps:
            failed_vps = [
                vp for vp in failed_vps
                if str(vp.get("id") or "") not in obsolete_vp_ids
            ]
        # 2026-10-22 (criterion replacement): a point whose criterion was
        # replaced this round is gone — the criterion it failed against
        # does not exist any more, so a repair task for it would be work
        # nobody asked for. The report still lists it (the round that
        # replaced it is the round that saw it fail), so it has to be
        # dropped here, before the split and repair paths see it.
        if obsoleted_vp_ids:
            failed_vps = [
                vp for vp in failed_vps
                if str(vp.get("id") or "") not in obsoleted_vp_ids
            ]
        # 2026-09-14 — repair vs split 判断.
        #
        # 修复任务生成器同时充当判断者：对一个失败的 VP，决定是生成
        # 新的修复任务，还是拆分它。有些失败并不是产品缺陷，只是当前
        # 的 VP 太大 —— 容易超时，拆开就好。
        #
        # 在生成修复内容之前先分流: 判 split 的失败 VP 不生成修复任务，
        # 而是拆成子 VP 写入 verification_plan.json + SPLIT verdict 写
        # 入 state.db，下一轮验证跑子 VP。判 repair 的走原有流程。
        # 任何异常都回退"全部走 repair" —— 拆分是优化路径，绝不能成为
        # 新的搁浅原因。
        # 2026-10-23 (VP-026): the four failure classes each buy a
        # different remedy, and the table that says which is
        # ``verification.failure_triage.disposition_for`` — the same
        # table the same-cause counter reads, so the two can never
        # disagree about what a failure is.
        #
        # The split happens here, before the repair model is asked
        # anything: only a broken deliverable is reworked. An infra
        # failure is retried (another attempt, not another rework), a
        # broken criterion is retired by the triage, and an out-of-scope
        # check is put to a human — the question is asked on the spot,
        # by the product, not by whoever reads the log afterwards.
        repair_candidates, deferred_vps = self._route_failed_vps(
            failed_vps, round_number,
        )
        if deferred_vps:
            print(
                f"[Repair] round {round_number}: "
                f"{len(deferred_vps)} failed VP(s) are not broken "
                f"deliverables — no repair task generated for "
                f"{', '.join(item['id'] for item in deferred_vps)}"
            )
            for item in deferred_vps:
                print(
                    f"[Repair]   {item['id'] or '<unnamed VP>'}: "
                    f"{item['failure_class']} — {item['disposition']}"
                )
        vp_splits: List[Dict[str, Any]] = []
        # ``_apply_vp_split_decisions`` never raises — any failure inside
        # the judge/splitter degrades to "repair everything", so the
        # split path can't become a new stranding cause.
        repair_candidates, vp_splits = self._apply_vp_split_decisions(
            repair_candidates, round_number,
        )

        # 2026-09-14 — a FAILED generation must not masquerade as "no
        # repair tasks needed". ``generate_repair_contents`` now raises
        # ``RepairGenerationError`` when the LLM call dies or returns
        # nothing usable while failures are pending; we catch it here
        # and surface it on the result payload so the auto-loop parks
        # the plan resumable instead of recording a terminal
        # ``no_repair_tasks`` verdict over real failures.
        repair_generation_error: Optional[str] = None
        repair_contents: List[Dict[str, Any]] = []
        # 2026-09-14: 上一轮为该 VP 生成的修复任务**执行后**是什么结果
        # (状态/命令/失败原因) 也要进 prompt —— 只看验证报告只能说
        # "这个 VP 又失败了", 说不了"上轮那个修复任务跑了什么、为什么
        # 没生效"。数据在 state.db 的 plan_tasks (修复任务不在
        # tasks.json 里)。
        repair_outcomes: Dict[str, Dict[str, Any]] = {}
        try:
            from verification.failure_history import load_repair_outcomes
            repair_outcomes = load_repair_outcomes(
                _state_db_path_for_orchestrator(), self.plan_dir.name,
            )
        except Exception as outcomes_exc:  # noqa: BLE001
            print(
                f"[Repair] round {round_number}: repair-outcome lookup "
                f"skipped ({outcomes_exc})"
            )
            repair_outcomes = {}
        # 2026-10-23 — the plan's own constraints, read once and used
        # twice: they go into the prompt (so the model can avoid the
        # conflict rather than be caught by it) and they gate the rows
        # the model produced (so a conflict the model missed still
        # cannot become a repair task). ``_plan_intent`` is
        # best-effort by contract — a plan directory it cannot read
        # contributes no constraints, it is never an error.
        plan_intent = _plan_intent(self.plan_dir)
        if repair_candidates:
            # 2026-10-23 — the plan's constraints go into the prompt so
            # the model can steer clear of the conflict. A generator
            # that predates the parameter is called without it: the
            # round continues, and the intent gate below still refuses
            # the rows that cross a constraint.
            generator_kwargs = {
                "previous_failure_feedback": previous_failure_feedback,
                "repair_outcomes": repair_outcomes,
            }
            if _accepts_plan_intent(self.repair_generator):
                generator_kwargs["plan_intent"] = plan_intent
            try:
                repair_contents = self.repair_generator.generate_repair_contents(
                    repair_candidates, round_number,
                    **generator_kwargs,
                )
            except VerificationCancelled:
                # 2026-10-22 — the operator stopped this plan while the
                # repair-content call was in flight. This is NOT a
                # generation failure: the arm below would record it as
                # one, and the auto-loop would then park the plan on a
                # "the generator broke" verdict for a round the
                # operator paused on purpose. Let it out untouched — no
                # repair tasks, no failure record, no retry.
                raise
            except RepairGenerationError as gen_exc:
                repair_generation_error = str(gen_exc)
                print(
                    f"[Repair] round {round_number}: repair-task generation "
                    f"FAILED ({len(repair_candidates)} failed VP(s) pending) — "
                    f"{gen_exc}"
                )
        else:
            # Every failure was either routed to a split, retired by the
            # plan, or bought a different remedy — there is nothing for
            # the repair LLM to do. Skipping the call (instead of asking
            # for an empty list) keeps the "no repair tasks" outcome
            # unambiguous for the auto-loop's split branch.
            #
            # 2026-10-22 (retired criteria): the two are told apart in
            # the log. "all N routed to split" is a lie about a round
            # whose failures were retired — nothing was split there,
            # and an operator reading the log would go looking for a
            # split that never happened.
            #
            # 2026-10-23 (VP-026): the same is true of a failure that
            # bought another remedy — an out-of-scope check is a
            # question for a human, not a split and not a repair.
            if retired_failed_vps:
                print(
                    f"[Repair] round {round_number}: "
                    f"{len(retired_failed_vps)} failed VP(s) retired by "
                    f"the plan, {len(failed_vps)} routed to split — no "
                    f"repair tasks generated"
                )
            elif deferred_vps:
                print(
                    f"[Repair] round {round_number}: no broken deliverable "
                    f"among the {len(failed_vps)} failed VP(s) — no repair "
                    f"tasks generated"
                )
            else:
                print(
                    f"[Repair] round {round_number}: all "
                    f"{len(failed_vps)} failed VP(s) routed to split — no "
                    f"repair tasks generated"
                )
        # 2026-10-23 — the intent gate (R4). The model was *asked* whether
        # its fix crosses the plan's own constraint, and the answer is
        # only as good as the model's reading of it; the gate re-checks
        # the rows deterministically and drops the ones that must not be
        # executed.
        #
        # A dropped row is a fact about the plan, not an error in the
        # repair path: the round continues with the rows that survived
        # (one conflict does not abort the round), and the conflicts are
        # reported on the result so an operator can decide whether the
        # constraint or the requirement gives way.
        intent_conflicts: List[Dict[str, Any]] = []
        if repair_contents:
            try:
                kept_contents, intent_conflicts = apply_intent_gate(
                    repair_contents, plan_intent,
                )
            except Exception as gate_exc:  # noqa: BLE001
                # The gate is best-effort by contract and is not allowed
                # to strand the round. A broken gate keeps every row (the
                # executor still has the constraint in its prompt) rather
                # than dropping the round's whole repair work — the
                # failure the auto-loop would read as "no repair tasks
                # needed" is the one this codebase has fought hardest
                # against.
                print(
                    f"[Repair] round {round_number}: intent gate failed, "
                    f"keeping every generated row ({gate_exc})"
                )
                kept_contents = list(repair_contents)
                intent_conflicts = []
            repair_contents = kept_contents
        # 2026-10-23 — every conflict is a WARNING: the plan states a
        # constraint, the repair crossed it, and an operator has to look
        # at it. The log is where they look. A missing logger is not a
        # reason to lose the event, so the print is the fallback — and it
        # never raises.
        for conflict in intent_conflicts:
            vp_id = str(conflict.get("vp_id") or "")
            constraint = str(conflict.get("constraint") or "")
            reason = str(conflict.get("reason") or "")
            logged = False
            logger = getattr(self, "logger", None)
            if logger is not None:
                try:
                    logger.warning(
                        "repair_intent_conflict",
                        f"repair content for {vp_id or '(unknown VP)'} "
                        f"crosses the plan's own constraint "
                        f"({constraint}) — dropped (round {round_number})",
                        data={
                            "plan_id": self.plan_dir.name,
                            "round": round_number,
                            "vp_id": vp_id,
                            "constraint": constraint,
                            "reason": reason,
                        },
                    )
                    logged = True
                except Exception as log_exc:  # noqa: BLE001
                    print(
                        f"[Repair] WARNING: repair_intent_conflict could "
                        f"not be logged ({log_exc})"
                    )
            if not logged:
                print(
                    f"[Repair] WARNING: repair_intent_conflict — "
                    f"{vp_id or '(unknown VP)'} 被意图门禁拦下 — "
                    f"{reason or constraint}"
                )
        # 2026-09-14: the repair task's ``test_command`` must be the failed
        # VP's *raw* command from ``verification_plan.json``. The path-based
        # reader above deliberately returns only a bounded summary + a
        # JSON-pointer path, so the runnable string has to be read
        # separately and handed to the assembler. Without it every repair
        # task reached the executor with no command at all and the
        # dual-criterion completion rule silently fell back to the AI's
        # own claim (``test_cross_verify_unverified``).
        _plan_file = self.plan_dir / "verification_plan.json"
        # 2026-09-20 (post-mortem): repair ids must be unique for the
        # life of the plan, not merely within the batch. A counter reset
        # hands the plan a fresh round 1, so the next batch reproduced
        # ``repair-r1-01`` … verbatim and ``plan_tasks`` — keyed on
        # ``(plan_id, task_id)`` — overwrote the previous batch's rows
        # instead of adding to them. Three batches in that run all numbered
        # from ``repair-r1-01``; ``repair-r1-06``'s 2026-09-18 breakdown
        # children are still on disk pointing at the 2026-09-20 task that
        # took its id.
        #
        # So: read what has already been allocated for this (plan, round)
        # from both stores, start above it, and report it if the two
        # stores disagree about what an existing id means.
        seq_base = 1
        try:
            from repair_generator import (
                cross_store_task_conflicts,
                next_repair_seq_base,
            )
            from verification.failure_history import load_task_titles

            # ``getattr``, not ``self.logger``: the attribute is not set in
            # ``__init__``, and an AttributeError here would be swallowed by
            # the ``except`` below — silently reverting to seq_base=1, i.e.
            # to the bug this whole block exists to fix.
            logger = getattr(self, "logger", None)
            db_titles = load_task_titles(
                _state_db_path_for_orchestrator(), self.plan_dir.name,
            )
            disk_titles = self._load_disk_task_titles()
            conflicts = cross_store_task_conflicts(disk_titles, db_titles)
            if conflicts:
                sample = list(conflicts.items())[:3]
                print(
                    f"[Repair] round {round_number}: {len(conflicts)} task "
                    f"id(s) disagree between tasks.json and plan_tasks — "
                    f"{sample}"
                )
                if logger is not None:
                    logger.warning(
                        "repair_task_id_store_conflict",
                        f"{len(conflicts)} task id(s) describe different "
                        f"tasks in tasks.json vs plan_tasks "
                        f"(round {round_number})",
                        data={"conflicts": {k: list(v) for k, v in sample}},
                    )
            seq_base = next_repair_seq_base(
                set(disk_titles) | set(db_titles), round_number,
            )
            if seq_base > 1:
                print(
                    f"[Repair] round {round_number}: {len(db_titles)} prior "
                    f"task row(s) already allocated; starting at seq "
                    f"{seq_base} so no existing id is reused"
                )
        except Exception as id_exc:  # noqa: BLE001
            # Id allocation is an integrity improvement, never a
            # precondition for the repair round. Falling back to seq_base
            # = 1 reproduces the pre-2026-09-20 behaviour exactly.
            print(
                f"[Repair] round {round_number}: repair-id allocation "
                f"check skipped ({id_exc})"
            )
            seq_base = 1
        repair_tasks = RepairTaskAssembler(
            round_number=round_number,
            failed_vps=failed_vps,
            vp_test_commands=extract_vp_test_commands(_plan_file),
            plan_path=str(_plan_file),
            seq_base=seq_base,
        ).assemble(repair_contents)
        self._current_repair_tasks = repair_tasks

        # 2026-10-11 (W5): retire earlier rounds' duplicate repair rows
        # for the VPs this round just re-queued. Without this, the
        # executor's queue (``_get_pending_repair_tasks``) carries both
        # the stale row and its replacement and runs the same fix twice.
        # Best-effort and non-fatal — the round must proceed even if the
        # bookkeeping fails; a missed supersede costs one duplicated
        # repair, whereas a raised exception here would cost the whole
        # round. Deferred import: ``verification_loop`` reaches this
        # module through ``server``, so importing it at module scope
        # here would be circular.
        if repair_tasks:
            try:
                from verification_loop import (
                    _supersede_prior_round_repairs,
                )
                _retired = _supersede_prior_round_repairs(
                    self.plan_dir.name,
                    round_number,
                    [rt.get("failed_vp_id") for rt in repair_tasks],
                )
                if _retired:
                    print(
                        f"[Repair] round {round_number}: retired "
                        f"{len(_retired)} pending repair task(s) from "
                        f"earlier rounds as superseded: "
                        f"{', '.join(sorted(_retired))}"
                    )
            except Exception as supersede_exc:  # noqa: BLE001
                print(
                    f"[Repair] round {round_number}: supersede check "
                    f"skipped ({supersede_exc})"
                )

        # 2026-09-08: single-writer refactor (mirrors the refiner
        # cleanup in agent.py). Previously the auto-loop wrote a
        # ``verification_tasks_round_N.json`` snapshot and the executor
        # spawned by ``_run_repair_execution`` merged it into a sibling
        # ``tasks_with_repair_round_N.json`` via ``_merge_repair_tasks_into_plan``
        # — a second writer that violated ``tasks.json`` being the only
        # canonical seed. The 2026-09-08 single-writer path writes the
        # repair tasks straight to ``state.db`` via
        # :class:`PlanTaskRepository.add_task`; the executor's
        # :meth:`AutonomousAgent._load_tasks` Phase-2 reconcile step then
        # picks them up as state.db orphans and injects them into the
        # DAG without ever touching ``tasks.json``.
        #
        # This block is best-effort: a SQLite error here is logged and
        # the orchestrator still returns ``repair_tasks`` for any caller
        # that wants to inspect them in-memory. The
        # ``_run_auto_verification_loop`` server-side branch reads
        # ``self._current_repair_tasks`` so the legacy in-memory contract
        # is preserved.
        #
        # Note: ``RepairTaskAssembler.assemble`` stamps ``status='pending'``
        # on every output dict. ``add_task`` rejects ``status`` (it is a
        # runtime mirror field owned by ``update_task``), so we strip it
        # before calling — ``add_task`` defaults the new entry to
        # ``status='pending'`` internally, so the end state is the same
        # with a single source of truth.
        if repair_tasks:
            try:
                from state_machine.db.connection import open as _open_db
                from state_machine.db.schema import migrate as _migrate
                from state_machine.repositories.plan_task_repository import (
                    PlanTaskRepository,
                )
                plan_id = self.plan_dir.name
                conn = _open_db(_state_db_path_for_orchestrator())
                try:
                    _migrate(conn)
                    repo = PlanTaskRepository(conn)
                    written = 0
                    for rt in repair_tasks:
                        if not isinstance(rt, dict):
                            continue
                        if not rt.get("id"):
                            continue
                        # ``add_task`` filters keys through
                        # ``ALLOWED_STATIC_TASK_FIELDS``. The repair
                        # fields (task_group / execution_group /
                        # priority / acceptance_criteria / failed_vp_id
                        # / round) are all on the allow-list as of
                        # 2026-09-08, so they pass through verbatim.
                        #
                        # ``status`` is NOT on the allow-list — it
                        # belongs to ``update_task``'s runtime mirror.
                        # :class:`RepairTaskAssembler.assemble` stamps
                        # ``status='pending'`` on every output dict,
                        # which would cause ``add_task`` to raise
                        # ``TaskProgressValidationError``. Strip the
                        # key here so ``add_task`` can apply its own
                        # default (also ``status='pending'`` — same
                        # outcome, single source of truth).
                        static_payload = {
                            k: v for k, v in rt.items() if k != "status"
                        }
                        repo.add_task(plan_id, static_payload)
                        written += 1
                    _logger = getattr(self, "logger", None)
                    if _logger is not None:
                        try:
                            _logger.info(
                                "verification_repair_tasks_persisted",
                                f"Wrote {written} repair tasks to state.db "
                                f"for round {round_number}",
                                data={
                                    "plan_id": plan_id,
                                    "round": round_number,
                                    "written": written,
                                    "task_ids": [
                                        rt.get("id") for rt in repair_tasks
                                        if isinstance(rt, dict)
                                    ],
                                },
                            )
                        except Exception:
                            pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception as persist_exc:
                # Non-fatal: the in-memory ``repair_tasks`` list is
                # still returned for any caller that consumes it.
                # ``_run_repair_execution`` no longer reads
                # ``verification_tasks_round_N.json`` so a state.db
                # write failure leaves no disk artifact. Operators
                # can recover by re-triggering ``check_cycle_conditions``
                # (the next round will re-emit and re-attempt the write).
                _logger = getattr(self, "logger", None)
                if _logger is not None:
                    try:
                        _logger.warning(
                            "verification_repair_tasks_persist_failed",
                            f"Could not persist repair tasks to state.db "
                            f"for round {round_number}: {persist_exc}",
                            data={
                                "plan_id": self.plan_dir.name,
                                "round": round_number,
                                "task_count": len(repair_tasks),
                                "error": str(persist_exc)[:500],
                            },
                        )
                    except Exception:
                        pass
        # The auto-loop never reads ``waiting_for_user`` (verified
        # by grep — only tests assert on it). Keep the field
        # contract intact for backwards compat but default to False
        # so any future dashboard reader doesn't wait on a flag the
        # orchestrator's auto-confirm path will never raise.
        self._waiting_for_user = False

        return {
            "should_continue": False,
            "should_stop": False,
            "stop_reason": None,
            "repair_tasks": repair_tasks,
            # 2026-09-14 — non-None when repair-task generation itself
            # failed (LLM error / unusable reply) with failures pending.
            # Consumers must treat this as "the generator broke", NOT as
            # "the chain converged": ``repair_tasks`` is empty in both
            # cases, but only the latter justifies a terminal
            # ``no_repair_tasks`` verdict.
            "repair_generation_error": repair_generation_error,
            # 2026-09-14 — 本轮因"VP 过大"而被拆分的父 VP 记录。每条:
            # ``{vp_id, child_vp_ids, hint, reason, persisted}``。
            # 非空表示下一轮验证要跑子 VP（父 VP 已 superseded），
            # 且本轮可能因此没有修复任务可执行 —— auto-loop 依据它
            # 跳过执行器、直接进入下一轮验证。
            "vp_splits": vp_splits,
            # 2026-10-23 — the repair rows that crossed the plan's own
            # constraints and were dropped by the intent gate. Each one
            # is ``{vp_id, constraint, reason}``. Non-empty means the
            # plan has a failure this round deliberately did not repair,
            # and only an operator can say whether the constraint or the
            # requirement gives way.
            "intent_conflicts": intent_conflicts,
            "needs_operator_decision": bool(intent_conflicts),
            # 2026-10-23 (VP-026): the failures that got no repair task
            # because their class buys a different remedy. Each entry is
            # ``{"id", "failure_class", "disposition"}`` — plus the
            # ``decision_id`` of the question an out-of-scope check was
            # put to. A failure that got no task is exactly what an
            # operator asks about, so the round says which one it was
            # and why.
            "deferred_vps": deferred_vps,
            # 2026-10-22 (criterion replacement): the criteria this
            # round replaced. Each entry is
            # ``{"vp_id", "failure_class", "replaced_by"}`` — the same
            # shape as the OBSOLETE verdict written to the state
            # machine, so a caller can read the round's decision from
            # either side.
            "criterion_replacements": criterion_replacements,
            # 2026-10-23 (VP-020): why the proposed replacement was not
            # used — ``persist_failed`` (the write did not land),
            # ``weaker_method`` (the proposal checked less than the
            # criterion it replaces) or ``invalid_proposal``. ``None``
            # when no replacement was proposed or the replacement landed.
            "replacement_rejected_reason": replacement_rejected_reason,
            "waiting_for_user": False,
            "status": "verification_failed",
            # 2026-10-25 (VP-031): the points this round voided — the
            # ones the plan never defined. They are not failures of the
            # deliverable, so they got no repair task; the operator still
            # has to void them or express their criterion, and the exit
            # says which ones and what to do about them.
            "void_vp_ids": sorted(void_vp_ids),
            "undefined_vp_ids": sorted(undefined_vp_ids),
            "operator_action": operator_action,
        }

    def _load_disk_task_titles(self) -> Dict[str, str]:
        """``task_id -> title`` from this plan's ``tasks.json``.

        Half of the cross-store consistency check (the other half is
        ``plan_tasks`` via ``verification.failure_history.load_task_titles``).
        Repair ids get folded into ``tasks.json`` by the executor's
        load-time reconcile, so the same id can appear in both stores —
        and after an id collision it shows up describing two different
        tasks.

        Accepts both the ``{"tasks": [...]}`` envelope and a bare list,
        matching how the rest of the codebase reads this file. Returns
        ``{}`` when the file is missing or unreadable — a malformed
        ``tasks.json`` is the executor's problem to report, not a reason
        to abort repair generation.
        """
        path = self.plan_dir / "tasks.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(payload, dict):
            tasks = payload.get("tasks") or []
        else:
            tasks = payload
        if not isinstance(tasks, list):
            return {}
        titles: Dict[str, str] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            titles[task_id] = str(task.get("title") or "")
        return titles

    def _apply_vp_split_decisions(
        self, failed_vps: List[Dict[str, Any]], round_number: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split the failed VPs that the judge routes to ``split``.

        Never raises: an exception anywhere in the judge / splitter /
        persistence degrades to ``(failed_vps, [])`` — "repair
        everything", the pre-2026-09-14 behaviour. Splitting is an
        optimisation; it must not be able to strand a round.
        """
        try:
            return self._judge_and_split(failed_vps, round_number)
        except Exception as exc:  # noqa: BLE001 — conservative fallback
            print(
                f"[Repair] round {round_number}: vp-split judging failed, "
                f"falling back to repair for all {len(failed_vps)} VP(s): "
                f"{exc}"
            )
            return list(failed_vps), []

    def _safe_phase_call(self, method_name: str, force_phase: str = "") -> None:
        """Call a ``PlanState`` phase helper without letting a phase
        conflict abort the round's real work.

        2026-09-14 (live post-mortem, round 3): phase transitions are
        *bookkeeping* — the round's actual products are the report, the
        repair tasks and the split decisions. When the watchdog had
        force-stamped the plan to ``failed`` mid-round, the transition to
        ``verification_failed`` raised ``Illegal transition`` and the
        exception unwound the auto-loop, discarding a completed
        judgment. Any phase conflict here is now logged and
        force-corrected instead of propagated.
        """
        try:
            getattr(self.plan_state, method_name)()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[Verification] phase bookkeeping {method_name}() failed "
                f"({exc}) — forcing phase={force_phase or method_name} and "
                f"continuing"
            )
            if force_phase:
                try:
                    self.plan_state.force_set_phase(force_phase)
                except Exception:
                    pass

    def _judge_and_split(
        self, failed_vps: List[Dict[str, Any]], round_number: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split the failed VPs that the judge routes to ``split``.

        Returns ``(repair_candidates, vp_splits)``:

          * ``repair_candidates`` — the original ``failed_vps`` entries
            that still need a repair task (every entry, when nothing was
            split);
          * ``vp_splits`` — one record per SPLIT parent:
            ``{vp_id, child_vp_ids, hint, reason, persisted}``.

        The split itself is two writes: children + ``superseded_by`` in
        ``verification_plan.json`` (so the next round's VP universe
        contains the children and not the parent) and the parent's
        ``SPLIT`` verdict in state.db (so a resumed round skips the
        parent even when it re-reads a stale plan). A parent whose split
        is declined by the splitter (too few groups, unreadable plan)
        stays in the repair set — declining is always safe.
        """
        from vp_split import ACTION_SPLIT, VpSplitJudge, VpSplitter

        judge = VpSplitJudge(self.coding_tool, self.plan_dir)
        candidates: List[Dict[str, Any]] = []
        originals: Dict[str, Dict[str, Any]] = {}
        for vp in failed_vps:
            vp_id = str(vp.get("id", "") or "")
            if not vp_id:
                continue
            entry = judge.plan_entry(vp_id) or {}
            originals[vp_id] = vp
            candidates.append({
                **vp,
                # The report reader's failed-VP dicts carry bounded
                # summaries, not the raw method/command — both live in
                # the plan entry (the judge's gate reads them).
                "method": entry.get("verification_method")
                or vp.get("method")
                or vp.get("verification_method")
                or "",
                "test_command": entry.get("test_command") or "",
                # 2026-09-14: how many times this VP has failed BEFORE this
                # round, from the same persisted history the repair prompt
                # uses. A VP that keeps failing the same way is evidence
                # it is too big (or too entangled) to fix in one piece —
                # a signal the repair-vs-split judge scores.
                "prior_failure_rounds": len([
                    h for h in self._failure_history.get(vp_id, [])
                    if h.get("round") != round_number
                ]),
            })
        if not candidates:
            return list(failed_vps), []

        decisions = judge.decide(candidates)
        splitter = VpSplitter(self.plan_dir, self.project_dir)
        repair_candidates: List[Dict[str, Any]] = []
        vp_splits: List[Dict[str, Any]] = []
        for candidate in candidates:
            vp_id = str(candidate.get("id"))
            decision = decisions.get(vp_id) or {}
            if decision.get("action") != ACTION_SPLIT:
                repair_candidates.append(originals[vp_id])
                continue
            reason = str(decision.get("reason", "") or "")
            hint = str(decision.get("split_hint", "") or "by_directory")
            children = splitter.split(vp_id, hint, reason=reason)
            if not children:
                # Declined (no useful grouping / unreadable plan) — the
                # VP still needs a repair task.
                repair_candidates.append(originals[vp_id])
                continue
            child_ids = [c["id"] for c in children]
            persisted = VpSplitter.persist_split(
                self.verif_repo, self.plan_dir.name, vp_id, children,
                reason=reason,
            )
            print(
                f"[Repair] round {round_number}: split {vp_id} into "
                f"{len(child_ids)} sub-VP(s) ({', '.join(child_ids)}) — "
                f"hint={hint} db_persisted={persisted}"
            )
            vp_splits.append({
                "vp_id": vp_id,
                "child_vp_ids": child_ids,
                "hint": hint,
                "reason": reason,
                "persisted": persisted,
            })
        self._current_vp_splits = vp_splits
        return repair_candidates, vp_splits

    def confirm_repair_and_rerun(self):
        """User has confirmed repair tasks — advance to ``executing`` phase.

        2026-09-07 fix: ``_waiting_for_user=False`` is now stamped by
        :meth:`check_cycle_conditions` because the auto-loop never reads
        it (and historically the field was a no-op signal that confused
        manual dashboards). But ``confirm_repair_and_rerun`` historically
        gated the ``start_verification_rerun`` call on
        ``self._waiting_for_user`` being True — with that flag now
        False, this method would return early and the plan's phase
        would stay stuck in ``verification_repairing``. Without the
        ``verification_repairing → verification_rerunning`` transition
        the auto-loop's later call to
        ``ps.stop_verification_loop(\"repair_execution_failed\")`` would
        raise ``Illegal transition from 'verification_repairing' to
        'verification_loop_stopped'`` (or — depending on whether the
        executor subprocess flipped the phase to ``executing`` first —
        ``Illegal transition from 'executing' to
        'verification_loop_stopped'``). The auto-loop's blanket
        ``except Exception`` swallowed that error and the plan was
        stranded in ``verification_repairing`` until the watchdog
        noticed.

        The fix: treat this method as the unconditional advance
        trigger. ``check_cycle_conditions`` has already produced the
        repair tasks, so the next round can begin regardless of the
        manual-confirm flag's value.

        2026-09-17: the sentence that used to be here credited the
        manual ``/api/verification/{id}/confirm-repairs`` endpoint as a
        second caller of this path. That endpoint was removed — it never
        called this method, only flipped in-memory flags and left the
        round unstarted (so ``/start`` answered 409 "already running"
        forever after). The auto-loop is the only caller.

        2026-09-12 (state machine closed-loop fix): replace
        ``start_verification_rerun()`` (which routes
        ``verification_repairing → verification_rerunning`` and stays
        inside the verification sub-machine) with
        ``start_repair_execution()`` which routes
        ``verification_repairing → executing``. The executor
        subprocess spawned by ``_run_repair_execution_async`` IS
        genuinely running tasks — the state vocabulary should reflect
        that. The corresponding edge is already declared in
        ``VERIFICATION_PHASE_TRANSITIONS`` at plan_state.py:177, so
        this method is a pure re-routing of the existing legal edge.
        """
        self._waiting_for_user = False
        self.plan_state.start_repair_execution()

    def stop_verification(self):
        """Handle a manual stop request from the user."""
        self._waiting_for_user = False
        current = self.plan_state.get_current_phase()
        if current in ("executing", "ready"):
            self.plan_state.transition_to("verification")
        elif current == "verification_running":
            self.plan_state.verification_failed()
        self.plan_state.stop_verification_loop("user_stopped")

    def is_waiting_for_user(self) -> bool:
        return self._waiting_for_user

    def get_current_repair_tasks(self) -> List[Dict]:
        return list(self._current_repair_tasks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_failed_ids(self, report: dict) -> set:
        """Collect all failed verification-point IDs from the report.

        2026-09-08: include ``timeout`` and ``SPLIT`` statuses in
        the failed set so the auto-loop routes them to repair instead
        of silently dropping them. Previously only ``FAILED`` was
        collected, which meant hard-timeout-split VPs disappeared
        from the repair pipeline.

        2026-09-12: the previous code only read the top-level ``verification_results``
        array. After ``snapshot_round_results`` + ``clear_round_results``
        runs at round start, the top-level array is emptied and the data
        lives in ``rounds[N-1].results``. When the new round ends with
        FAILED VPs the orchestrator may have already snapshotted the
        previous round's results — without falling back to
        ``rounds[-1].results`` the failed-IDs set is empty and the
        same-failure-repeated branch never fires — a stuck terminal can
        show FAILED VPs in ``rounds[0]`` while reporting
        ``no_repair_tasks``.

        Fall back to ``rounds[-1].results`` when ``verification_results``
        is empty so the same-failure detection still works.
        """
        failed_ids: set = set()
        # Union of all result lists we should inspect: top-level + last
        # round snapshot. ``failed_ids`` is a set so dedupe is automatic
        # when the same VP appears in both (during the brief window
        # between generate_verification_report writing top-level and
        # clear_round_results running).
        results_lists: List[List[Dict[str, Any]]] = []
        top_level = report.get("verification_results") or []
        if top_level:
            results_lists.append(top_level)
        rounds_field = report.get("rounds") or []
        if isinstance(rounds_field, list) and rounds_field:
            last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
            last_results = last_round.get("results") or []
            if isinstance(last_results, list) and last_results:
                # Always include the last round snapshot — dedupe happens
                # via the failed_ids set below, so even when top-level
                # and rounds[-1].results share VPs the same-failure
                # detection still works.
                results_lists.append(last_results)

        for results_list in results_lists:
            for r in results_list:
                if not isinstance(r, dict):
                    continue
                # FAILED is the legacy failure; timeout / SPLIT are the
                # 2026-09-08 hard-timeout-auto-split additions.
                if r.get("status") in ("FAILED", "timeout", "SPLIT", "hard_timeout"):
                    vp_id = r.get("id", "")
                    if vp_id:
                        failed_ids.add(vp_id)
        for d in report.get("requirement_deviations", []):
            failed_ids.add(d.get("verification_point_id", ""))
        return {fid for fid in failed_ids if fid}

    def _extract_failed_keys(self, report: dict) -> set:
        """The ``(verification point, failure class)`` pairs that failed.

        2026-10-22 (v2): the same-cause counter is per pair, so the
        class is part of the identity of a failure. A point that fails
        again for a *different* reason is a different failure and must
        not be counted as the same cause.

        The class is the one the verdict carries. A report that names
        none reads as ``deliverable_defect`` — the only class whose
        verdict is safe to act on when nobody has said otherwise (the
        other three either do not count or demand a human). That
        defaulting is :func:`verification.failure_triage.decision_key`'s,
        and this method goes through it rather than re-deriving the
        rule.

        The points walked are the ones :meth:`_extract_failed_ids`
        walks — the top-level results plus the last round's snapshot —
        and the first occurrence of a point wins, because the top-level
        list is the freshest.
        """
        from verification import failure_triage

        classes: Dict[str, str] = {}

        def _class_of(vp_id: str, entry: Dict[str, Any]) -> str:
            key = failure_triage.decision_key(vp_id, entry.get("failure_class"))
            if not key:
                return ""
            _, _, failure_class = key.partition(_DECISION_KEY_SEPARATOR)
            return failure_class

        results_lists: List[List[Dict[str, Any]]] = []
        top_level = report.get("verification_results") or []
        if top_level:
            results_lists.append(top_level)
        rounds_field = report.get("rounds") or []
        if isinstance(rounds_field, list) and rounds_field:
            last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
            last_results = last_round.get("results") or []
            if isinstance(last_results, list) and last_results:
                results_lists.append(last_results)

        for results_list in results_lists:
            for r in results_list:
                if not isinstance(r, dict):
                    continue
                if r.get("status") not in ("FAILED", "timeout", "SPLIT", "hard_timeout"):
                    continue
                vp_id = str(r.get("id") or "").strip()
                if not vp_id or vp_id in classes:
                    continue
                failure_class = _class_of(vp_id, r)
                if failure_class:
                    classes[vp_id] = failure_class

        for d in report.get("requirement_deviations", []) or []:
            if not isinstance(d, dict):
                continue
            vp_id = str(d.get("verification_point_id") or "").strip()
            if not vp_id or vp_id in classes:
                continue
            failure_class = _class_of(vp_id, d)
            if failure_class:
                classes[vp_id] = failure_class

        return {(vp_id, failure_class) for vp_id, failure_class in classes.items()}

    def _extract_failed_vps_with_evidence(self, report: dict) -> Dict[str, Dict[str, Any]]:
        """Like :meth:`_extract_failed_ids` but returns the full
        ``actual_result`` / ``evidence`` text per VP.

        2026-09-12: giving the agent unlimited retries without feedback
        lets it repeat the same failing approach. We collect each round's
        actual evidence so the next round's repair-task-generation
        prompt can show "上次方案完全无效" with the prior round's
        actual_result / evidence verbatim.

        Returns ``{vp_id: {"actual_result": str, "evidence": str}}``.
        When a VP appears in multiple result lists (top-level +
        rounds[-1]), the FIRST occurrence wins so the freshest
        verdict is preferred (top-level is appended first by the
        verifier before snapshot+clear runs).
        """
        out: Dict[str, Dict[str, Any]] = {}
        results_lists: List[List[Dict[str, Any]]] = []
        top_level = report.get("verification_results") or []
        if top_level:
            results_lists.append(top_level)
        rounds_field = report.get("rounds") or []
        if isinstance(rounds_field, list) and rounds_field:
            last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
            last_results = last_round.get("results") or []
            if isinstance(last_results, list) and last_results:
                results_lists.append(last_results)
        for results_list in results_lists:
            for r in results_list:
                if not isinstance(r, dict):
                    continue
                if r.get("status") not in ("FAILED", "timeout", "SPLIT", "hard_timeout"):
                    continue
                vp_id = str(r.get("id", ""))
                if not vp_id:
                    continue
                # First occurrence wins (top-level is freshest)
                if vp_id in out:
                    continue
                out[vp_id] = {
                    "actual_result": str(r.get("actual_result", "") or ""),
                    "evidence": str(r.get("evidence", "") or ""),
                    # 2026-10-23 (VP-026): the class the verdict recorded.
                    # Without it the history records every failure as
                    # ``deliverable_defect`` — the class that counts —
                    # and the triage reads a history that says every
                    # failure was a broken deliverable.
                    "failure_class": str(r.get("failure_class", "") or ""),
                    "failure_class_source": str(
                        r.get("failure_class_source", "") or ""
                    ),
                }
        return out
