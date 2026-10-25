"""
Repair Task Generator — one LLM call for content, local code for schema
======================================================================

What lives here
---------------
:meth:`RepairTaskGenerator.generate_repair_contents` asks the model for
the *content* of a repair — ``title``, ``description``,
``acceptance_criteria``, ``files_to_modify`` and the ``failed_vp_id`` it
addresses — in a single call. :class:`RepairTaskAssembler` then stamps
everything the state machine has to be able to rely on (``id``,
``priority``, ``depends_on``, ``execution_group``) with local code. The
model never sees a task id, so a model that misbehaves still yields
tasks the state machine can recognise.

A second, bounded LLM call fills a missing acceptance command
(:meth:`RepairTaskGenerator._fill_missing_test_commands`); it runs only
for rows the derived sources cannot answer for, and the assembler never
calls a model at all.

Which failures become a repair task at all is decided here, not by the
model: :func:`select_repairable_failed_vps` keeps only the
``deliverable_defect`` failures (the one class whose remedy is "redo the
deliverable") and records why each of the others was refused. The edit
scope of a row that declared none is derived by the shared rule
(:func:`test_command_rules.derive_files_to_modify`) rather than by a
second copy of the path-reading logic kept in this module.

What does not live here, and why
--------------------------------
This module grew a requirement-driven chain first: understand the
requirements, collect the failure evidence, map every failure back to a
criterion, then generate tasks that cite it. It is gone. Its evidence
step asked the model to re-derive a failure that
``verification_report.json`` already stated, and any plan whose evidence
did not line up with the PRD's own wording came back with zero tasks —
which the auto-loop read as "no repair needed" and stopped on, leaving
the plan in ``verification_failed`` with no path forward. The content
call reads the report through
:func:`verification.verification_report_reader.extract_failed_vps_with_paths`
and lets local code supply what the chain used to ask for.
"""

import json
import re
import os
import sys
from pathlib import Path
from typing import Iterator, Optional, List, Dict, Any, Sequence, Tuple

from coding_tool import CodingTool, JsonSession, query_json_in_session
# ``stable_session_id`` is re-exported from ``plan_sessions`` rather than
# defined here: the id belongs to the session, and the task-execution
# path needs it without importing this module's verification stack. The
# name stays importable from here for the callers that already use it.
from plan_sessions import stable_session_id
from test_command_quality import check_falsifiable, inspect_command
from verification_cancel import VerificationCancelled
from test_command_rules import derive_files_to_modify
from verification_persistence import VerificationPersistenceManager


# 2026-10-11: cap on failure text handed to the repair-task LLM.
# Kept local to this module rather than imported from
# ``verification_agent`` to avoid pulling that module (and its prompt
# stack) into every importer of the repair generator.
_MAX_REPAIR_PROMPT_CHARS = 8000


#: One resumable session per plan, shared by that plan's repair-content
#: calls. Keyed by plan directory rather than held on a
#: :class:`RepairTaskGenerator`, because a generator does **not** outlive
#: a round: :class:`~verification.orchestrator.VerificationOrchestrator`
#: builds one in ``__init__``, and the repair-complete callback enters
#: the next round through a *new* orchestrator. A handle on either
#: object would therefore be a handle that is empty again on every round
#: after the first, which is the same shape as having no handle at all —
#: it reads as reuse and pays for a fresh session each round.
#:
#: Round N+1 of a plan's repair is round N plus the evidence round N's
#: tasks failed to clear, and the content prompt names the files to read
#: rather than inlining them, so a fresh session per round re-reads all
#: of them every round.
#:
#: Entries are one small handle per plan directory this process has
#: generated repairs for, and live until the process ends.
_REPAIR_CONTENT_SESSIONS: Dict[str, JsonSession] = {}

#: Sidecar key for the repair-content session (see ``plan_sessions``).
#: The sidecar is ``<plan_dir>/.pdt-sessions.json`` — the per-plan file
#: this codebase already uses to carry a session id across a request
#: boundary, which is exactly what a ``/stop`` + ``/start`` pair is.
_REPAIR_CONTENT_SESSION_KEY = "repair_content"


def repair_content_session(plan_dir: Path) -> JsonSession:
    """The resumable session for ``plan_dir``'s repair-content calls.

    Created on first use. The handle comes back carrying whichever
    session the dispatch actually answered in — including one a provider
    fallback switched to — so the next round resumes a session that
    exists rather than one the caller guessed at.

    2026-10-22 — the handle also has to survive a PROCESS. A ``/stop``
    ends the round in one process and a ``/start`` begins the next one
    in another, and the in-process cache above cannot carry an id
    across that boundary. So the id is written to the plan's session
    sidecar and read back here: a plan whose sidecar carries an id
    resumes that session, a plan whose sidecar does not (or cannot be
    read) starts a session under the id derived from the plan and the
    step — the same id every time, so two processes that both start
    this step for this plan land in the same session rather than in
    two.
    """
    key = str(Path(plan_dir).resolve())
    session = _REPAIR_CONTENT_SESSIONS.get(key)
    if session is None:
        persisted = None
        try:
            from plan_sessions import load_session_id
            persisted = load_session_id(
                plan_dir, _REPAIR_CONTENT_SESSION_KEY,
            )
        except Exception:  # noqa: BLE001 — a missing sidecar is not an error
            persisted = None
        if persisted:
            # The session exists (or did): re-open it rather than
            # creating a new one under the derived id.
            session = JsonSession(persisted)
        else:
            session = JsonSession(
                pinned_session_id=stable_session_id(
                    Path(plan_dir).name, _REPAIR_CONTENT_SESSION_KEY,
                )
            )
        _REPAIR_CONTENT_SESSIONS[key] = session
    return session


def persist_repair_content_session(plan_dir: Path, session: JsonSession) -> None:
    """Record the repair-content session id in the plan's sidecar.

    Called after the dispatch, not before: the id worth resuming is the
    one that produced the answer. Best-effort — a plan whose sidecar
    cannot be written loses the cross-process resume, which costs a
    fresh session and nothing else.
    """
    try:
        from plan_sessions import persist_session
        persist_session(plan_dir, _REPAIR_CONTENT_SESSION_KEY, session)
    except Exception:  # noqa: BLE001 — persistence is an optimisation
        print(
            f"[Repair] could not record the repair-content session id "
            f"for {Path(plan_dir).name}; the next round will start a "
            f"fresh session"
        )


def _clamp_for_prompt(text: Any, limit: int = _MAX_REPAIR_PROMPT_CHARS,
                      label: str = "text") -> str:
    """Clip ``text`` to ``limit`` chars, declaring the cut when one happens.

    The repair agent repairs against this text, so a silent slice does
    not merely lose detail — it makes the agent repair against a
    fragment it cannot tell is a fragment, which is how four rounds
    went off-target on VP-016. Losing the tail is acceptable; losing
    the *fact* that the tail is missing is not, so the annotation
    states the original length too.
    """
    rendered = str(text or "")
    if len(rendered) <= limit:
        return rendered
    notice = (
        f"…[{label} 已截断，原长 {len(rendered)} 字符，"
        f"此处保留前 {limit}]"
    )
    return rendered[:limit] + notice


class RepairGenerationError(RuntimeError):
    """The repair LLM call ran but produced nothing usable.

    2026-09-14 — raised instead of returning an empty list. The two
    outcomes are NOT the same and must not be collapsed:

      * ``[]`` — there were no failed VPs to repair (or the caller
        already holds pending repair tasks). The chain has genuinely
        converged and stopping is correct.
      * ``RepairGenerationError`` — the generator *failed*: the LLM
        call raised (provider outage, hard timeout), or the reply was
        unparseable / contained no usable content while failures were
        pending. Stopping with ``no_repair_tasks`` here reports
        "nothing to fix" for a round that has real failures — on an
        earlier plan that silently terminalised a plan with
        three failed VPs (VP-021 / VP-034 / VP-036) after a bogus
        ``HardTimeoutError``.

    ``check_cycle_conditions`` catches this and surfaces it on the
    result payload as ``repair_generation_error`` so the auto-loop can
    park the plan resumable instead of recording a terminal verdict.
    """

    def __init__(self, message: str, *, round_number: int = 0,
                 failed_vp_ids: Optional[List[str]] = None):
        super().__init__(message)
        self.round_number = round_number
        self.failed_vp_ids = list(failed_vp_ids or [])


# ---------------------------------------------------------------------------
# Which failed VPs may become a repair task
# ---------------------------------------------------------------------------
#
# A repair task reworks the DELIVERABLE. That is the right remedy for one
# failure class and the wrong remedy for the other three:
#
#   * ``deliverable_defect`` — the thing under verification is wrong.
#     Redo it. This is the class a repair task exists for.
#   * ``infra`` — the attempt malfunctioned (rate limit, timeout, an
#     answer that could not be parsed). Another attempt is the remedy; a
#     rework would touch a file that was never the problem.
#   * ``criterion_defect`` — the verification point itself is wrong. No
#     amount of reworking the deliverable satisfies it; a human has to
#     fix the criterion.
#   * ``out_of_scope`` — nobody agreed to build this. The remedy is
#     nothing at all.
#
# The vocabulary is task 1's (``verification.failure_triage``) and the
# "no class stated → the class that counts" rule is
# ``verification.failure_history``'s. Both are imported INSIDE
# :func:`select_repairable_failed_vps`, not at module scope: they live in
# the ``verification`` package, whose ``__init__`` imports the
# orchestrator, and the orchestrator imports this module — a module-level
# import is an import cycle, measured rather than assumed
# (``ImportError: cannot import name 'RepairTaskGenerator' from partially
# initialised module 'repair_generator'``).


def select_repairable_failed_vps(
    failed_vps: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split failed VPs into the ones a repair task may be built from and
    the ones that may not.

    Only ``deliverable_defect`` may become a repair task. The other three
    classes each want a different remedy (see the block above), and a
    repair task built for one of them is work nobody asked for, pointed
    at a file that may not be the problem at all.

    A record that carries no class reads as ``deliverable_defect``: the
    classification is newer than most verdicts, and "nobody said
    otherwise" has always meant "repair it" — that is the historical
    behaviour, kept so a plan whose verdicts predate the classification
    keeps repairing exactly what it used to.

    2026-10-25 (VP-031): a record that says the point has **no
    definition** is refused on that ground alone, even when it carries
    no class. The class is newer than most verdicts, so a record that
    predates it would otherwise fall back to "repair it" — and a point
    that was never written down has nothing a rework could satisfy. The
    refusal says what a **human** has to do (void the point, or have
    the plan's owner re-express the criterion), because the exit of a
    ``verification_failed`` round is read by a person, not by a model.

    Args:
        failed_vps: The failed-VP records, in the shape
            :func:`verification.verification_report_reader.extract_failed_vps_with_paths`
            returns them.

    Returns:
        ``(repairable, filtered)``. ``repairable`` keeps the input order
        and holds the records themselves. ``filtered`` holds one entry per
        refused VP, each carrying its ``id``, the ``failure_class`` it was
        refused for, and a ``reason`` naming what that class wants
        instead.
    """
    from verification.failure_history import failure_class_of
    from verification.failure_triage import (
        CRITERION_DEFECT,
        DELIVERABLE_DEFECT,
        INFRA,
        OUT_OF_SCOPE,
    )

    #: What each non-repairable class wants instead of a rework. The
    #: wording is the operator's: the reason is read in a log line, and a
    #: log line that says only "filtered" sends the reader looking for
    #: the reason somewhere else. It says what a **human** has to do, not
    #: "try again".
    reasons = {
        INFRA: (
            "环境失败（infra）：该重试的是验证本身，不是重做产物；"
            "不生成修复任务"
        ),
        CRITERION_DEFECT: (
            "判据本身有缺陷（criterion_defect）：应由人修判据，重做产物不解决"
        ),
        OUT_OF_SCOPE: (
            "越界检查（out_of_scope）：没有人要求建这个，不生成修复任务"
        ),
    }

    #: The reason for a point that has no definition at all. It is
    #: spelled out rather than reusing the generic ``criterion_defect``
    #: wording, because the action here is concrete: there is no
    #: criterion to fix — the point was never written down.
    def _definition_missing_reason(vp_id: str) -> str:
        return (
            f"{vp_id} 在验证计划里没有定义（definition_missing）："
            f"请作废该验证点，或由计划属主重新表达它的判据；"
            f"执行器不得自行发明判据，重做产物也解决不了没有判据的问题，"
            f"因此不生成修复任务"
        )

    repairable: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    for vp in failed_vps or []:
        if not isinstance(vp, dict):
            continue
        vp_id = str(vp.get("id", "") or "")
        # 2026-10-25 (VP-031): the fact that the point has no
        # definition stands on its own. A record that predates the
        # classification carries no class at all, and the "no class
        # stated → repair it" rule would let it through.
        definition_missing = bool(vp.get("definition_missing"))
        failure_class = (
            CRITERION_DEFECT if definition_missing else failure_class_of(vp)
        )
        if failure_class == DELIVERABLE_DEFECT:
            repairable.append(vp)
            continue
        reason = (
            _definition_missing_reason(vp_id)
            if definition_missing
            else reasons.get(
                failure_class,
                f"失败分类 {failure_class} 不是 deliverable_defect，不生成修复任务",
            )
        )
        filtered.append({
            "id": vp_id,
            "failure_class": failure_class,
            "reason": reason,
            # What a human has to do about it, said as an action rather
            # than as a class name.
            "operator_action": (
                "void_or_re_express"
                if definition_missing
                else {
                    INFRA: "retry_verification",
                    CRITERION_DEFECT: "re_express_criterion",
                    OUT_OF_SCOPE: "decide_scope",
                }.get(failure_class, "review")
            ),
        })
    return repairable, filtered


def _has_declared_scope(raw: Any) -> bool:
    """Whether a content row declared a non-empty edit scope.

    The declared scope is the model's own answer and it wins: the shared
    rule is a fallback for the rows that declared nothing, not an
    override for the rows that did.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return False
    return any(isinstance(entry, str) and entry.strip() for entry in raw)


def _failure_record(failed_vp: Dict[str, Any]) -> Dict[str, Any]:
    """The failed VP in the shape the shared scope rule reads.

    :func:`test_command_rules.derive_files_to_modify` reads a path out of
    a failure's text (``stack`` / ``traceback`` / ``output`` …) or out of
    a field that names the failing file outright. A failed-VP record is
    therefore handed over **as it stands** — a record that already
    carries a ``stack`` or a ``file`` has that read, and dropping it on
    the way to the rule would throw away the one piece of evidence the
    rule was written for.

    What is added is the *report-side* spelling of the same text: the
    report reader puts the failure output under ``actual_result`` /
    ``evidence`` (or their ``_summary`` variants), and the rule reads
    ``stack`` / ``output``. The two are filled in only when the record
    does not already carry them, so a record that says both says the
    same thing twice and the rule sees the record's own wording first.

    Nothing is invented: a record with no failure text at all simply
    yields no path.
    """
    record = dict(failed_vp)
    record.setdefault(
        "stack",
        str(
            failed_vp.get("actual_result")
            or failed_vp.get("actual_result_summary")
            or ""
        ),
    )
    record.setdefault(
        "output",
        str(
            failed_vp.get("evidence")
            or failed_vp.get("evidence_summary")
            or ""
        ),
    )
    return record


def resolve_repair_project_dir(
    plan_dir: Path, fallback: Optional[Path] = None,
) -> Optional[str]:
    """The directory this plan's repairs happen in.

    The execution chain reads exactly one place for "where does this
    plan's work happen": the ``project_dir`` column of
    ``plan_execution``.  A verification run registers its worktree
    there (``verification_workspace.register_project_dir``), so the
    repair round that follows reads the worktree's path out of the same
    row the executor and the verification loop read — which is what
    makes a fix land in the tree the verification branch owns rather
    than in the pull request head or in the main checkout.

    The row wins over ``fallback``.  The row is the field the whole
    chain reads, so a caller that hands the generator a *different*
    directory has not changed where the plan's work happens — it has
    only changed what it believes about it.  ``fallback`` is what a plan
    with no execution row yet gets, and it is what the generator's own
    ``project_dir`` argument is.

    Best-effort by design: a missing row, an unreadable database or a
    locked one must not stop a repair round.  The fallback covers those
    cases, and an absent answer leaves the repair tasks without a
    directory — the same state they were in before this field existed.
    """
    plan_id = Path(plan_dir).name
    if plan_id:
        try:
            from config_paths import resolve_state_db_path
            from state_machine.db.connection import open as open_db
            from state_machine.repositories.execution_repository import (
                ExecutionRepository,
            )

            conn = open_db(resolve_state_db_path())
            try:
                row = ExecutionRepository(conn).summary(plan_id)
            finally:
                conn.close()
            registered = str((row or {}).get("project_dir") or "").strip()
            if registered:
                return registered
        except Exception:  # noqa: BLE001 — the fallback covers this
            pass
    if fallback is not None:
        text = str(fallback).strip()
        if text:
            return text
    return None


def one_row_per_defect(
    contents: List[Dict[str, Any]], round_number: int = 0,
) -> List[Dict[str, Any]]:
    """Keep the first row for each confirmed defect, drop the rest.

    One defect, one task, one commit, one regression test — that is the
    rule the repair round is built on (test decision 7/8).  The model is
    asked for one row per failed verification point and usually answers
    that way, but it does answer with a second row for a defect it
    already covered often enough to matter: two tasks for one defect are
    two commits for one fix, and the round's own bookkeeping then counts
    one fix as two.

    The drop happens *here*, where the model's answer is read, rather
    than in the assembler: the assembler is handed the content and turns
    it into tasks, and a caller may legitimately hand it several rows
    for one defect on purpose.  Refusing it there would refuse the
    caller's rows, not the model's.

    The first row for a defect wins: it is the answer the model gave
    before it started repeating itself.
    """
    kept: List[Dict[str, Any]] = []
    seen: set = set()
    for content in contents or []:
        if not isinstance(content, dict):
            continue
        failed_vp_id = str(content.get("failed_vp_id", "") or "")
        if failed_vp_id and failed_vp_id in seen:
            print(
                f"[Repair] repair_task_dropped_duplicate_defect "
                f"round={round_number} failed_vp_id={failed_vp_id} "
                f"title={str(content.get('title', '') or '')!r} "
                f"reason=the defect already has a repair task in this "
                f"batch — one defect gets one task, one commit and one "
                f"regression test"
            )
            continue
        if failed_vp_id:
            seen.add(failed_vp_id)
        kept.append(content)
    return kept


class RepairTaskGenerator:
    """Generates requirement-driven repair tasks from verification failures."""

    #: How long the pre-repair falsifiability probe may run before it is
    #: treated as inconclusive. Deliberately short: a *vacuous* command
    #: returns in milliseconds (it only reads a file), so anything still
    #: running after a minute is a real test and the probe has already
    #: got its answer. We never wait for a real suite to finish.
    FALSIFIABILITY_TIMEOUT_S = 60

    def __init__(self, coding_tool: CodingTool, plan_dir: Path, project_dir: Path):
        """
        Initialize repair task generator.

        Args:
            coding_tool: Coding tool for LLM queries
            plan_dir: Plan directory containing PRD/arch/test/verification documents
            project_dir: Project directory to verify
        """
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)

        # ``command -> passes the pre-repair probe``. Repair rounds
        # routinely produce several tasks sharing one command shape; the
        # probe runs a subprocess, so it must not be repeated per task.
        self._falsifiability_cache: Dict[str, bool] = {}

        # The failed VPs this generator refused to turn into repair
        # tasks, each with the reason why. Rewritten on every round this
        # generator runs — a generator does not outlive a round (see the
        # note on ``_REPAIR_CONTENT_SESSIONS``), so the attribute always
        # describes the round that produced the tasks in hand. It is the
        # record of *why a failure got no repair*, which is the question
        # an operator asks when a plan's verification failed and no
        # repair task appeared for one of the failures.
        self.filtered_vps: List[Dict[str, Any]] = []

        # File paths
        self.prd_file = self.plan_dir / "prd.json"
        self.prd_md_file = self.plan_dir / "prd.md"
        self.arch_file = self.plan_dir / "arch-design.md"
        self.test_file = self.plan_dir / "test-design.md"
        self.verification_report_file = self.plan_dir / "verification_report.json"

        # Persistence manager for reading verification reports
        self.persistence = VerificationPersistenceManager(self.plan_dir)

    #: How many **extra** model rounds a verification point gets to
    #: produce an acceptance command when none of the three derivation
    #: paths in :meth:`_resolve_repair_test_command` yields one. So the
    #: worst case is this plus the original content call.
    #:
    #: The name is deliberately *not* ``MAX_FILES_FILL_ATTEMPTS``: that
    #: loop lived in the dispatcher's ``files_to_modify`` self-heal and
    #: was deleted outright, and
    #: ``tests/contract/test_no_prerun_gate.py`` asserts the attribute
    #: does not come back. This is the same idea — a bounded re-ask
    #: rather than an unbounded hang — for a different field.
    #:
    #: Why 2: the second round is where the "here is why your last
    #: answer was unusable" feedback actually changes the answer, and a
    #: third round buys very little for a real cost on a round that may
    #: have a dozen command-less verification points. The fill loop asks
    #: for **every** outstanding point in one call per round, so this is
    #: at most two extra calls per round, not per task.
    MAX_TEST_COMMAND_FILL_ATTEMPTS = 2

    def _resolve_repair_test_command(
        self,
        task: Dict[str, Any],
        evidence_items: List[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """Return ``(command, source)`` for a generated repair task.

        The contract is that every repair task ships with a runnable
        command: the completion gate cross-checks the subagent's
        ``TEST_RESULT`` claim against the command's exit code, and with
        no command the verdict falls back to the subagent's word alone.

        Resolution order:

        1. ``llm`` — the model supplied one that passes the command-quality
           check. Preferred: only the model knows what the *fixed* command
           should look like, which may differ from the failing one (e.g. a
           repair whose whole point is that the verification point's
           command was wrong).
        2. ``vp`` — the task named its ``vp_id`` and that verification
           point has a command. Derived, so weaker: re-running the
           failing command may not be the right check if the command
           itself was the defect.
        3. ``single_evidence`` — exactly one verification point failed,
           so the task can only be about that one and its command is
           unambiguous. Same caveat as (2).
        4. ``missing`` — nothing usable could be derived. Returned as an
           empty command plus an explicit source rather than silently
           substituting ``""``, so the caller can surface it.

        A structurally-broken command is treated as absent rather than
        shipped, whichever source proposed it. This is worse than none:
        it guarantees a FALSE failure every time (the chain exits
        non-zero even when the fix is correct), whereas no command
        degrades to the audit second pass, which can actually pass.

        That rule covers sources (2) and (3) as much as (1). Those two
        hand back a command the plan author wrote into
        ``verification_plan.json``, and the author is a human being with
        a shell — an ``ls`` chained ahead of the real runner exits
        non-zero when the path is absent, and the whole chain then
        reports failure for a repair that worked. The assembler takes a
        declared command as already vetted, so without a check here
        that shape reached the completion gate verbatim and failed every
        round it was issued in. A rejected derivation is **not** a
        third outcome: it reads as "no command from this source" and
        resolution continues to the next path, ending at ``missing`` and
        the assembler's own ``audit_style`` / ``missing`` labelling.

        The model-authored candidate additionally has to survive the
        pre-repair probe (:meth:`_passes_falsifiability`): a command the
        LLM wrote that already exits 0 on the current tree proves
        nothing about the repair, so it is discarded the same way a
        structurally-broken one is. Only source (1) is probed — the
        derived fallbacks come from the verification point's own
        command, and a vacuous command *there* means the verification
        point itself is broken, which is a different defect that should
        surface rather than be silently routed around. Probing them would
        also be close to pure cost: the command belongs to a verification
        point that has just failed, so its probe is nearly certain to
        answer "fails as it should" without running anything the repair
        needs. The structural check is different on both counts — it
        executes nothing, and the false-failure argument above holds just
        as well for an author-declared command.
        """
        command = str(task.get("test_command") or "").strip()
        if command:
            issues = inspect_command(command)
            if issues:
                print(
                    f"[Repair] Discarding unusable test_command from the LLM "
                    f"({issues[0].code}): {command[:120]}"
                )
            elif not self._passes_falsifiability(command):
                print(
                    f"[Repair] Discarding vacuous test_command from the LLM "
                    f"(it already passes before the repair): {command[:120]}"
                )
            else:
                return command, "llm"

        by_vp = {
            str(e.get("vp_id")): str(e.get("test_command") or "").strip()
            for e in evidence_items
            if isinstance(e, dict) and e.get("vp_id")
        }

        vp_id = str(task.get("vp_id") or "").strip()
        if vp_id:
            derived = self._structurally_runnable(
                by_vp.get(vp_id, ""), f"vp {vp_id}",
            )
            if derived:
                return derived, "vp"

        usable = [c for c in by_vp.values() if c]
        if len(usable) == 1:
            derived = self._structurally_runnable(
                usable[0], "the round's only evidence command",
            )
            if derived:
                return derived, "single_evidence"

        return "", "missing"

    @staticmethod
    def _structurally_runnable(command: str, origin: str) -> str:
        """``command`` when :func:`inspect_command` accepts it, else ``""``.

        The one rule every derivation in :meth:`_resolve_repair_test_command`
        runs its candidate through, so "structurally broken" means the
        same thing on the ``vp`` path as on the LLM path rather than
        being re-implemented per site.

        A rejection is deliberately *silent about the row*: the caller
        treats ``""`` as "this source had nothing to offer" and carries
        on to the next derivation, so a broken author-declared command
        ends up labelled ``audit_style`` / ``missing`` by the assembler
        instead of being shipped as a false-failure generator. The log
        line names the origin so an operator can tell a plan whose
        commands are all unusable from one that simply has none.

        Only the shape is judged. The RED probe stays on the LLM path
        alone — see :meth:`_resolve_repair_test_command`.
        """
        command = str(command or "").strip()
        if not command:
            return ""
        issues = inspect_command(command)
        if not issues:
            return command
        print(
            f"[Repair] Discarding structurally-broken test_command "
            f"derived for {origin} ({issues[0].code}): {command[:120]}"
        )
        return ""

    # ------------------------------------------------------------------
    # Path A — bounded re-ask for an acceptance command
    # ------------------------------------------------------------------

    def _fill_missing_test_commands(
        self,
        contents: List[Dict[str, Any]],
        failed_vps: List[Dict[str, Any]],
        round_number: int,
    ) -> Dict[str, str]:
        """Re-ask the model for a ``test_command`` on rows that have none.

        Mutates ``contents`` in place, stamping ``test_command`` and
        ``test_command_source`` onto every row this loop rescues.
        Returns ``{vp_id: command}`` for what it added.

        Rows that arrive *with* a command are vetted here before the
        loop starts, and a row whose command does not survive is cleared
        so the loop picks it up like any other outstanding point. That
        is deliberate: a command the model volunteered on the content
        call is still the model's, and
        :meth:`RepairTaskAssembler._resolve_test_command` takes a declared
        command as already vetted, so the two checks that decide whether
        a model-authored command is worth shipping
        (:func:`test_command_quality.inspect_command`, then the RED
        invariant in :meth:`_passes_falsifiability`) have to run on it
        somewhere. This is that somewhere, and it is reached through
        :meth:`_resolve_repair_test_command` — the same call the re-ask
        path makes — so there is one rule rather than two that can drift
        apart. Clearing rather than re-judging keeps the outcome on the
        existing bounded-retry contract instead of introducing a second
        way for a row to lose its command.

        Why this runs here rather than in the assembler: only the
        generator holds a ``coding_tool``. The assembler is local-code
        by construction, and keeping the LLM call on this side preserves
        that split — the assembler still stamps schema fields
        deterministically and never talks to a model.

        What makes a round's worth of retries affordable: every
        outstanding point is asked about in **one** call per attempt, so
        the cap is
        :data:`MAX_TEST_COMMAND_FILL_ATTEMPTS` extra calls for the round
        regardless of how many points are outstanding.

        The loop stops as soon as a row is filled, so a round where the
        model gets it right on the first re-ask costs one call. A row it
        cannot rescue keeps ``test_command`` absent, and the assembler
        labels it ``audit_style`` or ``missing`` — the loop is an
        improvement, never a precondition for emitting the repair.
        """
        evidence_items = _evidence_items_from_failed_vps(failed_vps)
        vp_by_id = {
            str(vp.get("id")): vp for vp in failed_vps if vp.get("id")
        }
        plan_vps = self._verification_plan_points()

        #: ``{vp_id: why the last answer was unusable}`` — fed back into
        #: the next attempt so a retry is a correction, not a reroll.
        reasons: Dict[str, str] = {}
        filled: Dict[str, str] = {}

        # Vet the commands that arrived on the content call, before any
        # attempt is spent. A model that volunteers ``test_command``
        # despite rule 5 of the content prompt gets it carried through
        # verbatim by the parser, and the pending filter below skips any
        # row that already has one — so without this pass such a command
        # would reach the assembler, which takes a declared command as
        # vetted, without either check having run on it.
        #
        # The verdict comes from ``_resolve_repair_test_command``, the same
        # call the filter makes: ``"llm"`` means it passed the structural
        # check and the pre-repair probe. Anything else means it did not,
        # and the row is put back into the state a command-less row is in.
        #
        # A reason is recorded only for a row the loop will actually
        # re-ask, and the loop's own pending filter is what decides
        # that: a row the resolver still answers from a derived path is
        # skipped there and the assembler picks that command up on its
        # own. Recording a reason for such a row is not merely noise —
        # the end-of-loop summary counts ``len(reasons)`` as "points we
        # could not get a command for", so a point that *was* answered
        # by a derivation and labelled normally by the assembler would
        # be reported as a point that was not.
        #
        # Only ``single_evidence`` is reachable from here. The resolver's
        # other derivation reads ``task["vp_id"]``, which a content row
        # does not carry — it carries ``failed_vp_id`` — so a content
        # row can only ever land on ``single_evidence``, ``llm`` or
        # ``missing``. The VP's own command is resolved later and
        # elsewhere, by
        # :meth:`RepairTaskAssembler._resolve_test_command`, from
        # ``vp_test_commands``.
        for content in contents:
            if not str(content.get("test_command") or "").strip():
                continue
            if self._resolve_repair_test_command(content, evidence_items)[1] == "llm":
                continue
            content["test_command"] = ""
            content["test_command_source"] = ""
            vp_id = str(content.get("failed_vp_id", "") or "")
            print(
                f"[Repair] Discarding the test_command the content call "
                f"supplied for {vp_id or 'an unnamed VP'} — it did not "
                f"survive the command-quality / falsifiability checks"
            )
            # The pending filter below re-resolves each command-less row,
            # so that is the question to ask here rather than a
            # look-alike: a row the filter will skip gets no reason,
            # because the loop will never show it one.
            will_be_asked = (
                bool(vp_id)
                and self._resolve_repair_test_command(
                    content, evidence_items,
                )[1] == "missing"
            )
            if vp_id and will_be_asked:
                reasons[vp_id] = (
                    "内容调用直接给出的命令未通过质量检查或修复前的可证伪性检查"
                    "（结构检查不通过，或命令在修复之前就已经退出 0），已作废"
                )

        # One resumable session for this fill loop. Every attempt asks
        # the same question — "give these outstanding points an
        # acceptance command" — and the second attempt's prompt is the
        # first one's plus the reason the last answer was unusable, so
        # the model is correcting its own answer rather than re-rolling
        # it. That holds for the batch, not per point: the call covers
        # every outstanding point at once, and the loop is what repeats.
        session = JsonSession()

        for attempt in range(1, self.MAX_TEST_COMMAND_FILL_ATTEMPTS + 1):
            pending: List[Dict[str, Any]] = []
            for content in contents:
                vp_id = str(content.get("failed_vp_id", "") or "")
                if not vp_id or vp_id in filled:
                    continue
                if str(content.get("test_command") or "").strip():
                    continue
                if self._resolve_repair_test_command(
                    content, evidence_items,
                )[1] != "missing":
                    # One of the derived paths already has an answer
                    # (the VP's own command, or a single usable one
                    # across the batch). The assembler picks those up on
                    # its own; re-asking the model would only invite it
                    # to contradict a command the plan already declares.
                    continue
                pending.append(content)
            if not pending:
                break

            outstanding = [str(c.get("failed_vp_id")) for c in pending]
            prompt = _build_test_command_fill_prompt(
                [vp_by_id.get(vp_id, {}) for vp_id in outstanding],
                plan_vps,
                reasons,
                attempt=attempt,
                max_attempts=self.MAX_TEST_COMMAND_FILL_ATTEMPTS,
                plan_dir=self.plan_dir,
                project_dir=self.project_dir,
            )
            try:
                response = query_json_in_session(
                    self.coding_tool, session,
                    prompt=prompt,
                    system_instruction=TEST_COMMAND_FILL_SYSTEM_PROMPT,
                )
            except VerificationCancelled:
                # 2026-10-22 — the operator stopped this plan. The
                # ``break`` below would swallow it and let the round
                # carry on to the assembler, which would then build
                # repair tasks out of a round the operator had just
                # paused. A cancelled round produces no tasks at all.
                raise
            except Exception as exc:  # noqa: BLE001
                # A failed re-ask is not a failed repair. The rows keep
                # whatever they had (nothing) and the assembler labels
                # them; losing the fill loop is strictly better than
                # losing the round.
                print(
                    f"[Repair] test_command fill attempt "
                    f"{attempt}/{self.MAX_TEST_COMMAND_FILL_ATTEMPTS} "
                    f"failed for {outstanding}: {exc}"
                )
                break

            for content in pending:
                vp_id = str(content.get("failed_vp_id"))
                candidate = _extract_filled_test_command(response, vp_id)
                if not candidate:
                    reasons[vp_id] = "上轮没有为该验证点给出任何命令"
                    continue
                issues = inspect_command(candidate)
                if issues:
                    reasons[vp_id] = (
                        f"命令未通过质量检查（{issues[0].code}）："
                        f"{issues[0].detail}"
                    )
                    print(
                        f"[Repair] Fill rejected a test_command for "
                        f"{vp_id} ({issues[0].code}): {candidate[:120]}"
                    )
                    continue
                if not self._passes_falsifiability(candidate):
                    reasons[vp_id] = (
                        "命令在修复之前就已经退出 0，无法区分「修好了」和"
                        "「什么都没改」"
                    )
                    print(
                        f"[Repair] Fill rejected a vacuous test_command "
                        f"for {vp_id}: {candidate[:120]}"
                    )
                    continue
                content["test_command"] = candidate
                content["test_command_source"] = "llm_retry"
                filled[vp_id] = candidate

            if filled:
                print(
                    f"[Repair] test_command fill attempt "
                    f"{attempt}/{self.MAX_TEST_COMMAND_FILL_ATTEMPTS} "
                    f"rescued {len(filled)} VP(s): "
                    f"{', '.join(sorted(filled))}"
                )

        if reasons and not filled:
            print(
                f"[Repair] no acceptance command obtained after "
                f"{self.MAX_TEST_COMMAND_FILL_ATTEMPTS} fill attempt(s) "
                f"for {len(reasons)} VP(s); the assembler will label them "
                f"by category"
            )
        return filled

    # ------------------------------------------------------------------
    # Path B — the edit scope, derived by the shared rule
    # ------------------------------------------------------------------

    def _fill_missing_files_to_modify(
        self,
        contents: List[Dict[str, Any]],
        failed_vps: List[Dict[str, Any]],
    ) -> None:
        """Give a content row that declared no edit scope a derived one.

        The scope is a hard gate downstream: ``RepairTaskAssembler.assemble``
        refuses a task whose ``files_to_modify`` resolves to ``[]``, because
        an empty scope is read as "touch nothing" and the task then has no
        declared blast radius at all. So a row that declares no scope is a
        task that never gets dispatched — and the failure usually DID name
        the file it came from.

        Reading that file out of the failure is a rule, and the rule
        already exists: :func:`test_command_rules.derive_files_to_modify`,
        shared with the refiner. This calls it rather than keeping a
        second copy of the path-reading logic here, so the two sides
        cannot drift apart on what counts as a file.

        A row that DID declare a scope keeps it, unchanged. The model's
        own answer is the better one, and the shared rule is a fallback
        for the rows that said nothing — not an override of the rows
        that said something.

        Mutates ``contents`` in place. A row nothing can be derived for
        keeps its empty scope, and the assembler drops it as before: the
        rule invents no path (see the "no stack and no diff → no files"
        boundary in ``test_command_rules``).
        """
        by_id = {
            str(vp.get("id")): vp
            for vp in failed_vps
            if isinstance(vp, dict) and vp.get("id")
        }
        for content in contents:
            if _has_declared_scope(content.get("files_to_modify")):
                continue
            vp = by_id.get(str(content.get("failed_vp_id", "") or ""))
            if vp is None:
                continue
            derived = derive_files_to_modify(
                _failure_record(vp), vp.get("diff"),
            )
            if derived:
                content["files_to_modify"] = derived
                print(
                    f"[Repair] no edit scope declared for "
                    f"{content.get('failed_vp_id')}; derived from the "
                    f"failure: {', '.join(derived)}"
                )

    def _verification_plan_points(self) -> Dict[str, Dict[str, Any]]:
        """``{vp_id: verification point}`` from the plan, or ``{}``.

        Read only on the fill path, and only when a command is actually
        missing — the content prompt deliberately does not inline plan
        bodies, and this keeps that property.

        The full declaration matters here because the method field is
        the shape of the whole point: ``request`` + ``assertions`` tell
        the model what the API contract *is*, which is the input a
        runnable check can be written from. A retry prompt that omitted
        them would just re-roll the same answer.
        """
        plan_file = self.plan_dir / "verification_plan.json"
        try:
            raw = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        points = raw.get("verification_points") if isinstance(raw, dict) else None
        if not isinstance(points, list):
            return {}
        return {
            str(p.get("id")): p
            for p in points
            if isinstance(p, dict) and p.get("id")
        }

    #: Set ``PDT_DISABLE_FALSIFIABILITY_PROBE=1`` to skip the pre-repair
    #: probe. It runs each distinct candidate command as a subprocess, so
    #: an operator debugging generation in an environment where the
    #: project cannot execute (missing toolchain, offline) needs a way
    #: out that is not "edit the code".
    FALSIFIABILITY_PROBE_ENV = "PDT_DISABLE_FALSIFIABILITY_PROBE"

    def _passes_falsifiability(self, command: str) -> bool:
        """True when ``command`` fails on the current, pre-repair tree.

        This is the RED invariant. A command that already exits 0 before
        the repair runs cannot distinguish a correct fix from no change
        at all, so shipping it would make the task's completion verdict
        vacuous — the failure mode behind an earlier plan's 142 empty-diff
        commits. ``inspect_command`` cannot see it: the shape is legal,
        only the *outcome* is wrong.

        The probe runs in ``self.project_dir`` and is memoised per
        command string, because repair rounds routinely emit several
        tasks with the same command. A timeout counts as passing (see
        :func:`check_falsifiable`): something real was executing, which
        is all this check needs to know.
        """
        if os.environ.get(self.FALSIFIABILITY_PROBE_ENV) not in (None, "", "0"):
            return True

        cached = self._falsifiability_cache.get(command)
        if cached is not None:
            return cached

        ok, detail = check_falsifiable(
            command,
            cwd=str(self.project_dir),
            timeout=self.FALSIFIABILITY_TIMEOUT_S,
        )
        self._falsifiability_cache[command] = ok
        if not ok:
            print(f"[Repair] Falsifiability probe rejected a command: {detail}")
        return ok

    def _get_next_repair_task_id(self, round_number: int) -> str:
        """Determine the next repair task ID prefix for a verification round."""
        return f"R{round_number}"

    def append_to_tasks(self, repair_tasks: List[Dict[str, Any]], round_number: int) -> Path:
        """Append repair tasks to the existing project tasks.json without overwriting.

        Preserves the existing task list and appends the new repair tasks with
        R{round}- prefixed IDs. If project_dir/tasks.json does not exist, it
        is created with just the repair tasks.

        Args:
            repair_tasks: List of repair task dicts to append.
            round_number: Current verification round (used as ID prefix).

        Returns:
            Path to the updated tasks.json file.
        """
        tasks_file = self.project_dir / "tasks.json"

        existing_tasks: List[Dict[str, Any]] = []
        if tasks_file.exists():
            try:
                with open(tasks_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    existing_tasks = payload
                elif isinstance(payload, dict):
                    existing_tasks = payload.get("tasks", []) or []
            except Exception:
                existing_tasks = []

        prefixed: List[Dict[str, Any]] = []
        prefix = self._get_next_repair_task_id(round_number)
        for i, task in enumerate(repair_tasks, 1):
            task_copy = dict(task)
            task_copy.setdefault("id", f"{prefix}-{i}")
            prefixed.append(task_copy)

        combined = list(existing_tasks) + prefixed

        self.project_dir.mkdir(parents=True, exist_ok=True)
        with open(tasks_file, "w", encoding="utf-8") as f:
            json.dump(combined, f, indent=2, ensure_ascii=False)

        return tasks_file


# ---------------------------------------------------------------------------
# Single-call content flow
# ---------------------------------------------------------------------------
#
# Why this block exists
# ----------------------
# The requirement-driven chain this module grew out of asked the model to
# re-derive each failure from PRD context instead of reading the on-disk
# ``verification_report.json``. ``repair_tasks`` then came back empty for
# any plan whose evidence chain did not line up with the PRD's own
# wording, and the orchestrator's ``no_repair_tasks`` branch terminated
# the auto-loop as failed — leaving the plan stuck in
# ``verification_failed`` with no path forward.
#
# This flow replaces it with a single LLM call that produces *content*
# only (``title``, ``description``, ``acceptance_criteria``,
# ``failed_vp_id``), while :class:`RepairTaskAssembler` stamps the
# schema-stable tags locally (``id``, ``task_group``, ``priority``,
# ``depends_on``, ``status``). The state machine can therefore trust
# the tags even when the LLM misbehaves.

REPAIR_CONTENT_SYSTEM_PROMPT = """你是一位资深技术架构师，专注于把验证失败的 VP 转写成可执行的修复任务。

**严格输出 JSON**：
{
  "tasks": [
    {
      "failed_vp_id": "VP-034",
      "title": "修复 VP-034 的具体偏差",
      "description": "完整描述：包含背景、失败证据、修复建议（指向源码位置或 verification_plan.json 字段）",
      "acceptance_criteria": "TDD 规格：test_xxx 条件 → 预期",
      "files_to_modify": ["相对项目根的路径", "..."],
      "priority_hint": "high|medium|low",
      "execution_group_hint": 0,
      "intent_check": {"violates": false, "constraint": "", "reason": "一句话"}
    }
  ]
}

**约束**：
1. 不要产出 task_id / depends_on — 这些由系统本地代码生成。
2. description 必须引用 PRD 验收标准（不要引用 pytest 错误）。
3. 一个 failed VP 一个 task；不要把多个 VP 合成一个 task。
4. **不要去改验收判据。** VP 的 `request` / `assertions` / `target_url` 等声明字段是不可变的 —— 它们是"被测对象"，不是"可调参数"。修复任务的职责永远是改**源码**去满足判据。判据确实写错时，正确做法是作废该 VP 并新增一条表达正确的（见 `verification_plan_delta` 的"绝不修改已有验证点"）。
5. `files_to_modify` 必须是**相对项目根目录**的路径（不要绝对路径、不要 `..`）。修复任务的验收命令由系统自动带上，你不需要产出 test_command。路径拿不准时给出你最可能改动的那个文件，不要编造不存在的深层目录。
6. 只输出 JSON，不要 markdown 代码块标记。
7. 用户消息里的「原计划的初衷与硬约束」段是**硬约束**，不是建议：每一条都来自这个计划自己
   对改动范围的声明，越过它就等于把计划要做的事换成了另一件事。给修复之前先判断它是否与
   其中任何一条**冲突**。冲突时**不要**生成那条修复任务 —— 在 `description` 里写明与第几条
   冲突、冲突点是什么，**上报**给上层决定；不要自己换一个同样越界的方案。如果同一条失败
   还有不冲突的修法，就用那种修法。
8. 每条 task 必须带 `intent_check`，它是你对上面那条硬约束的**结构化判断**：
   不冲突时 `violates` 为 `false`、`constraint` 与 `reason` 留空字符串；冲突时
   `violates` 为 `true`，`constraint` 写明冲突的是第几条 / 哪句话，`reason` 写明冲突点。
   它只是把第 7 条的结论写清楚，不要为了填这个字段而改写你的判断。
"""


#: System prompt for the bounded fill loop
#: (:meth:`RepairTaskGenerator._fill_missing_test_commands`).
#:
#: It is a *different question* from :data:`REPAIR_CONTENT_SYSTEM_PROMPT`
#: on purpose. That prompt asks for narrative about a failure and
#: explicitly disclaims ``test_command`` (rule 5), because a command for
#: the repair belongs to the plan. This one asks only for the command,
#: and is only sent for verification points that turned out to have no
#: command the framework could reuse — the case the disclaimer above
#: leaves uncovered.
TEST_COMMAND_FILL_SYSTEM_PROMPT = """你是一位资深工程师，为**已经写好的修复任务**补一条验收命令。

背景：修复任务的内容（title / description / acceptance_criteria / files_to_modify）
已经生成完毕，现在只缺一条验收命令。

**严格输出 JSON**：
{
  "commands": [
    {"failed_vp_id": "VP-009", "test_command": "pytest -q tests/test_x.py::test_y"}
  ]
}

**这条命令的语义**：修复**正确**时它必须退出 0；修复**没做**时它必须非 0。
它不是复现失败用的，是**验收**用的 —— 所以它要指向修复后的行为，
而不是"当前是坏的"这个状态本身。

**硬性要求**：
1. 每个 `failed_vp_id` 必须出现，且**只出现一次**。
2. 在**项目根目录**下直接运行；不要 `cd backend`、`cd ~/...` 这类前缀。
3. 不要写只有 `ls` / `cat` / `grep` 的探测链 —— 它对修复与否都退出 0，验不出任何东西。
4. 不要写打印常量、比较字面量、或任何恒真的断言。
5. 命令必须**现在就会失败**。如果它在当前（尚未修复的）代码上已经退出 0，
   它证明不了修复是否生效，会被框架拒绝。
6. `api_test` 类验证点：如果该 API 有对应测试就调用那条测试；没有就写一条
   能打到该契约的真实请求（例如 curl 打到该服务并断言关键字段）。
7. 如果某条验证点**本质上不可能**有可执行命令（例如它的交付物是一份
   citations.json 审查结论），就**不要为它编一条命令**—— 宁可少给一条，
   也不要给一条恒真的假命令。恒真的假命令会把"修好了"显示成"没修也过"。
8. 只输出 JSON，不要 markdown 代码块标记。
"""


#: Matches the part of a generated repair-task id that carries its
#: round and sequence — ``repair-r1-06`` and the breakdown children it
#: spawns (``repair-r1-06-1``, ``repair-r3-03-1-1-2``) all resolve to
#: round 1 / seq 6 and round 3 / seq 3 respectively.
_REPAIR_TASK_ID_RE = re.compile(r"^repair-r(\d+)-(\d+)")


def parse_repair_task_id(task_id: Any) -> Optional[Tuple[int, int]]:
    """``"repair-r1-06"`` → ``(1, 6)``; ``None`` for anything else."""
    match = _REPAIR_TASK_ID_RE.match(str(task_id or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def next_repair_seq_base(
    existing_task_ids: Any, round_number: int,
) -> int:
    """Lowest ``seq`` this round's batch can use without colliding.

    2026-09-20 (post-mortem). Repair ids were ``repair-r{N}-{seq}``
    with ``seq`` always starting at 1, so every *batch* for the same
    round produced the same ids. ``plan_tasks`` is keyed on
    ``(plan_id, task_id)``, so the second batch did not merely duplicate
    the first — it **overwrote** it, taking that row's ``attempt`` /
    ``failure_reason`` / ``commit_sha`` history with it. The breakdown
    children left behind then described a parent whose row had become an
    unrelated task.

    Every counter reset hands the plan a fresh round 1, so batches from
    different rounds end up numbered from ``repair-r1-01`` again; the
    earlier batch's children then sit in ``tasks.json`` under a parent
    written later.

    Returns a base such that seq keeps increasing monotonically for a
    given ``(plan, round)`` forever, so an id is never reused. ``1``
    when nothing has been allocated yet — the pre-existing behaviour,
    which is correct for a plan's first repair batch.
    """
    highest = 0
    for task_id in existing_task_ids or ():
        parsed = parse_repair_task_id(task_id)
        if parsed is None:
            continue
        task_round, seq = parsed
        if task_round != int(round_number):
            continue
        highest = max(highest, seq)
    return highest + 1


#: Halfwidth/fullwidth punctuation pairs the two stores render
#: differently. ``tasks.json`` keeps the LLM's ASCII colon and
#: parentheses; ``plan_tasks`` carries a version normalised to CJK
#: fullwidth. Without folding them, six of the thirteen repair ids
#: looked like conflicts when their titles were identical but for
#: ``:`` vs ``：`` and ``(`` vs ``（``.
_TITLE_PUNCTUATION_FOLD = str.maketrans({
    "：": ":", "（": "(", "）": ")", "，": ",", "；": ";",
    "！": "!", "？": "?", "、": ",", "。": ".",
})


def _normalise_title(title: Any) -> str:
    """Fold the punctuation/whitespace the two stores disagree on."""
    text = str(title or "").translate(_TITLE_PUNCTUATION_FOLD)
    return "".join(text.split())


def cross_store_task_conflicts(
    disk_titles: Any, db_titles: Any,
) -> Dict[str, Tuple[str, str]]:
    """Ids present in both stores whose titles disagree.

    ``plans/<id>/tasks.json`` and
    ``state.db::plan_tasks`` are written by different code paths (the
    executor's load-time reconcile folds DB orphans into ``tasks.json``),
    so an id collision shows up as the *same id describing two different
    tasks* depending on which store you read: a repair row for one
    verification point in ``plan_tasks``, and the breakdown children of an
    unrelated task still hanging off that id in ``tasks.json``.

    Two filters keep the report readable:

    * **A blank title on either side is not a conflict.** Rows that
      predate the ``title`` column being populated carry ``title=NULL``
      in ``plan_tasks``, and flagging every one of them buries the real
      collisions.
    * **Punctuation and whitespace are folded** (see
      :data:`_TITLE_PUNCTUATION_FOLD`). Colliding ids routinely differ
      only in ASCII vs CJK punctuation.

    Returns ``{task_id: (disk_title, db_title)}``. Callers log it;
    nothing here repairs the split, because the fix is to stop
    generating colliding ids in the first place (see
    :func:`next_repair_seq_base`).
    """
    disk = {str(k): str(v or "") for k, v in (disk_titles or {}).items()}
    db = {str(k): str(v or "") for k, v in (db_titles or {}).items()}
    conflicts: Dict[str, Tuple[str, str]] = {}
    for task_id, disk_title in disk.items():
        db_title = db.get(task_id)
        if db_title is None:
            continue
        if not disk_title.strip() or not db_title.strip():
            continue
        if _normalise_title(disk_title) != _normalise_title(db_title):
            conflicts[task_id] = (disk_title, db_title)
    return conflicts


#: The ``verification_method`` values whose **declared deliverable is an
#: evidence artifact** — a findings document the framework then checks —
#: rather than a command that can be replayed or a code diff.
#:
#: This is the criterion :meth:`RepairTaskAssembler._is_audit_style_repair`
#: uses, and it is read straight off the plan's own method table
#: (``prompts``' verification-perspective table): ``api_test`` is judged
#: from ``request`` + ``assertions``, which the framework executes
#: deterministically; ``code_review`` is judged from a ``citations.json``
#: (file / line / verbatim text) and ``ui_validation`` from a
#: ``checkpoints.json`` (selector / expected / actual). The last two are
#: findings someone reads. No shell command reproduces them, which is
#: exactly why :meth:`RepairTaskAssembler._api_replay_command` returns
#: ``""`` for them and no other method can fill the gap.
#:
#: Deliberately a **positive** whitelist. A method this list does not
#: name is "we do not know what this deliverable is", which is a
#: different statement from "this task needs no command", and the two
#: must not be merged — see the two-sentinel rationale in
#: ``backend/task.py``. Anything unrecognised lands on the
#: ``"missing"`` source, which is still emitted, so the cost of guessing
#: wrong is a mislabelled row and never a silent completion.
#:
#: Note what is deliberately *not* used here: the keyword list
#: ``agent._AUDIT_TASK_KEYWORDS``. It cannot work on this input —
#: ``agent._looks_like_audit_task`` short-circuits to ``False`` for any
#: repair task (its own post-mortem records why: the keywords 定位 / 复跑 /
#: 不变量 occur naturally in a repair brief), so a copy of that list
#: would classify nothing while appearing to classify something.
AUDIT_STYLE_VERIFICATION_METHODS = frozenset({"code_review", "ui_validation"})

#: Distinguishes "the plan has not been read yet" from "the plan holds no
#: verification points", which are different answers — the second is a
#: real, cacheable result. A plain ``None`` default cannot express that,
#: and re-reading the plan once per row would turn a batch into
#: filesystem traffic.
_UNSET: Any = object()


class RepairTaskAssembler:
    """Local-code task object assembler.

    Takes the content dicts produced by
    :meth:`RepairTaskGenerator.generate_repair_contents` plus the
    failed-VP list from
    :func:`verification.verification_report_reader.extract_failed_vps_from_report`,
    and stamps every locally-owned field (``id``, ``task_group``,
    ``priority``, ``depends_on``, ``status``) in a deterministic way
    the state machine can recognise.

    The LLM never sees these tags, so a misbehaving LLM cannot
    fabricate a duplicate ``R1-1`` for round 2, an out-of-bounds
    ``priority`` enum, or a circular ``depends_on`` graph.

    Args:
        round_number: Current verification round (1-based). Used as the
            task ID prefix so round 1 → ``repair-r1-01``, round 2 →
            ``repair-r2-01``, etc.
        failed_vps: The list returned by
            :func:`extract_failed_vps_from_report`. Used as the
            priority fallback source and the ``failed_vp_id`` ↔
            priority lookup table.
        seq_base: First sequence number this batch may use. Defaults to
            ``1`` (a plan's first repair batch). Callers that may be
            emitting a *second* batch for the same round must pass
            :func:`next_repair_seq_base`'s result so ids never repeat.
        project_dir: Where the repairs of this batch run. Defaults to
            ``None``, which means "ask the plan's execution row" — the
            field the execution chain reads, and where a verification
            run registers its worktree.  It is the *fallback* for a plan
            with no row, never an override of one.  See
            :func:`resolve_repair_project_dir`.

    Note:
        Task IDs use the ``repair-r{N}-{seq}`` prefix (not the legacy
        ``R{N}-{seq}``). Both forms coexist on disk because the
        executor's TaskManager does not parse IDs — but the new prefix
        lets the dashboard and watchdog grep for repair tasks
        distinctly from the original-stream tasks.

        ``seq`` is **not** the task's position in the batch — it is a
        never-reused allocation counter for ``(plan, round)``. A second
        batch for round 1 might therefore start at ``repair-r1-19``.
        That is deliberate: the number is an identity, not a label.
    """

    #: Where an assembled task's ``test_command`` came from. Recorded in
    #: :attr:`test_command_sources` and echoed to the log, because the
    #: sources carry different confidence and an operator reading a run
    #: needs to tell them apart.
    #:
    #: The first four are *commands*: the row was resolved to something
    #: runnable. The last two are *absences*, and the pair is
    #: deliberately kept apart rather than merged — see below.
    #:
    #:   ``llm``         — the content dict carried a model-authored command
    #:   ``llm_retry``   — ...produced by the bounded fill loop
    #:                     (:data:`RepairTaskGenerator.MAX_TEST_COMMAND_FILL_ATTEMPTS`)
    #:   ``vp``          — the VP's own ``evidence_command``
    #:   ``api_replay``  — derived: replays the VP's request + assertions
    #:   ``audit_style`` — **no command, and this class of task needs none**
    #:                     (see :data:`AUDIT_STYLE_VERIFICATION_METHODS`)
    #:   ``missing``     — **no command, and we do not know that none is
    #:                     needed** — the fill loop ran out of attempts
    #:
    #: The last two are the same two-value split ``backend/task.py``
    #: makes with ``NO_FILE_CHANGES_SENTINEL`` and
    #: ``UNKNOWN_MODIFICATIONS_SENTINEL``: a task that declared itself
    #: read-only is not a task whose file list nobody filled in, and the
    #: framework keeps them distinguishable for exactly the reason that
    #: post-mortem gives — conflating "declared" with "unknown" is what
    #: turns one into a silent failure of the other.
    #:
    ASSEMBLER_TEST_COMMAND_SOURCES = (
        "llm", "llm_retry", "vp", "api_replay", "audit_style", "missing",
    )

    #: Where the repairs of this batch run, when the caller named one.
    #: Empty means "ask the plan's execution row" — see
    #: :func:`resolve_repair_project_dir`.  A fallback, never an
    #: override: the row is the authority.
    #:
    #: A class-level default because an assembler may be built without
    #: running ``__init__`` (the stub helpers in the test suite do), and
    #: the attribute has to be readable either way.
    project_dir: str = ""

    def __init__(
        self,
        round_number: int,
        failed_vps: List[Dict[str, Any]],
        vp_test_commands: Optional[Dict[str, str]] = None,
        plan_path: Optional[str] = None,
        seq_base: int = 1,
        project_dir: Optional[str] = None,
    ):
        if round_number < 1:
            raise ValueError(f"round_number must be >= 1, got {round_number}")
        if seq_base < 1:
            raise ValueError(f"seq_base must be >= 1, got {seq_base}")
        self.round = round_number
        #: First ``seq`` this batch may use. ``1`` unless the caller has
        #: seen prior batches for this ``(plan, round)`` — see
        #: :func:`next_repair_seq_base` for why reusing seq 1 corrupts
        #: the persisted task history.
        self.seq_base = seq_base
        self.failed_vps: Dict[str, Dict[str, Any]] = {
            str(vp.get("id", "")): vp for vp in failed_vps if vp.get("id")
        }
        # Raw ``{vp_id: test_command}`` read from ``verification_plan.json``
        # (see ``extract_vp_test_commands``). The failed-VP dicts the
        # assembler receives only carry a *bounded summary* of the command
        # plus a JSON-pointer path, neither of which is runnable — so the
        # orchestrator hands the raw map in separately.
        #
        # 2026-10-11: the values come from the plan's ``evidence_command``
        # key (legacy alias ``test_command``), not from a key named
        # ``test_command`` — see
        # ``verification.verification_report_reader._vp_runnable_command``.
        self.vp_test_commands: Dict[str, str] = dict(vp_test_commands or {})
        # Generation-time counters. Each one is bumped by
        # :meth:`assemble` next to a single greppable log event, so
        # "this round repaired nothing" / "this round repaired things it
        # cannot check" is a number an operator can read instead of an
        # inference.
        #
        #   ``dropped_empty_files_to_modify`` → ``repair_task_dropped_empty_files_to_modify``
        #   ``unverifiable_no_test_command``  → ``repair_task_unverifiable_no_test_command``
        #
        # The second is a *count of degraded tasks*, not of refusals: a
        # row with no command is still emitted. See :meth:`assemble`.
        self.dropped_empty_files_to_modify: int = 0
        self.unverifiable_no_test_command: int = 0
        #: How many times the bounded fill loop re-asked the model for an
        #: acceptance command before this assembler gave up on the row.
        #: Read back by operators from the per-round log line to tell
        #: "the model never produced one" from "we only asked once".
        self.test_command_fill_attempts: int = 0
        #: ``{task_id: source}`` for the tasks this batch emitted,
        #: using :data:`ASSEMBLER_TEST_COMMAND_SOURCES` as the vocabulary
        #: — the assembler's counterpart to the generator's
        #: ``test_command_source``. It lives on the assembler rather than
        #: on the task dict because ``PlanTaskRepository.add_task``
        #: rejects any key outside ``ALLOWED_STATIC_TASK_FIELDS``.
        self.test_command_sources: Dict[str, str] = {}
        # Used only to build the "was the plan entry actually changed?"
        # guard command for repairs whose target is the plan file itself.
        self.plan_path: str = str(plan_path or "")
        #: Where the repairs of this batch run.  Empty means "ask the
        #: plan's execution row" — see :func:`resolve_repair_project_dir`.
        #: A verification run registers its worktree there, so this is
        #: the tree the fix belongs in; the pull request head and the
        #: main checkout are not.  A fallback for a plan with no row,
        #: not an override of one.
        self.project_dir: str = str(project_dir).strip() if project_dir else ""
        #: Lazy cache for :meth:`_plan_points`. ``_UNSET`` until first read.
        self._plan_points_cache: Any = _UNSET

    def assemble(self, contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Stamps schema-stable fields onto each content dict.

        One generation-time gate runs here, after
        :meth:`_resolve_files_to_modify` / :meth:`_resolve_test_command`
        have had their say and before anything is handed back:

        * ``files_to_modify`` resolves to ``[]`` → the task is **dropped**
          (``repair_task_dropped_empty_files_to_modify``).

        An empty ``test_command`` is **not** a gate; see the block at
        the decision point below for why, and for the two ways out
        (regenerate, then accept-by-category).

        Why refuse rather than warn there: an empty ``files_to_modify``
        is read as "touch nothing" by
        ``agent._declared_modification_files``, so the task has no
        declared blast radius at all. A whole batch can carry exactly
        that shape and still be dispatched task by task, so nothing
        upstream would have caught it.

        ``files_to_modify`` being mandatory is also what makes the
        within-batch parallelism safe now that
        :meth:`_infer_depends_on` no longer chains the batch:
        ``agent._build_micro_layers`` derives its conflict graph from
        ``files_to_modify``, so two repairs touching the same file get
        serialised into different micro layers — but only if that field
        is populated. A ``[]`` reads as "no declared files" and gives the
        conflict graph nothing to work with.

        ``test_command`` resolving to ``""`` is **not** a gate. It used
        to be, and the refusal turned out to be structural rather than
        situational: a verification point written as a declarative API
        test (``verification_method: api_test`` plus ``request`` /
        ``assertions``) carries no ``test_command`` string anywhere, so
        *every* repair task derived from such a VP was refused — the
        round repaired nothing while the generation step reported a
        plausible-looking count of refused rows. A verification point is
        not a shell command, and lacking one is not evidence against
        repairing the failure it describes. Such a task is emitted with
        ``test_command=""``, counted in
        ``unverifiable_no_test_command`` and logged as
        ``repair_task_unverifiable_no_test_command``: the risk is real
        (the completion rule degrades to "believe the sub-agent",
        ``agent._cross_verify`` → ``test_cross_verify_unverified``), so
        it stays visible — but a weaker check beats no repair.

        This gate does not paper over the loss of coverage — a rejected
        task means that VP gets no repair this round. That consequence is
        booked and surfaced by the caller
        (``verification_loop`` refuses to start another verification
        round over an undrained repair queue) rather than absorbed.

        Args:
            contents: Output of
                :meth:`RepairTaskGenerator.generate_repair_contents`.

        Returns:
            List of fully-formed repair-task dicts, each carrying the
            keys consumed by the downstream executor
            (``id``, ``title``, ``description``,
            ``acceptance_criteria``, ``priority``, ``depends_on``,
            ``task_group``, ``execution_group``, ``failed_vp_id``,
            ``round``, ``status``, ``test_command``,
            ``files_to_modify``).

            Empty list if ``contents`` is empty, or if every row was
            refused by the ``files_to_modify`` gate (see
            ``dropped_empty_files_to_modify``). A row with no
            ``test_command`` is *not* on that path — it is emitted and
            counted in ``unverifiable_no_test_command``.

        Two further rules apply to what comes out:

        * **One defect, one task.**  The content this assembler is
          handed already carries at most one row per confirmed defect —
          :meth:`RepairTaskGenerator.generate_repair_contents` drops a
          second row for a defect the round already covers.  One defect
          gets one task, one commit and one regression test; a second
          task for it is a second commit for one fix.
        * **Every task names the directory it runs in.**  The
          ``project_dir`` is the plan's registered workspace — the
          verification worktree — so a repair lands in the tree the
          verification branch owns.  See
          :meth:`_resolve_task_project_dir`.
        """
        if not contents:
            return []
        tasks: List[Dict[str, Any]] = []
        emitted_seq = self.seq_base - 1
        for content in contents:
            if not isinstance(content, dict):
                continue
            failed_vp_id = str(content.get("failed_vp_id", "") or "")
            # Reject entries that name a VP we never saw as FAILED —
            # the LLM hallucinated the ID. The assembler is the last
            # line of defence here: even if ``_parse_repair_contents_response``
            # let it through, we drop it now.
            if failed_vp_id not in self.failed_vps:
                continue
            vp_plan = self.failed_vps[failed_vp_id]
            title = str(content.get("title", "") or "").strip()
            description = str(content.get("description", "") or "").strip()
            # Half-formed content is salvaged rather than dropped. The LLM
            # occasionally returns the right ``failed_vp_id`` with an empty
            # ``title`` / ``description``; discarding the row used to silently
            # shrink the repair round (and, when *every* row came back
            # half-formed, produce "0 repair tasks" for a round that had real
            # failures). A minimal task anchored on the evidence paths is
            # strictly more useful than nothing — the executor can still Read
            # the referenced report/log files and act on them.
            if not title:
                title = f"[{failed_vp_id}] 修复验证失败项 {failed_vp_id}"
            if not description:
                description = self._fallback_description(failed_vp_id, vp_plan)
            priority = (
                str(content.get("priority_hint") or "").strip().lower()
                or str(vp_plan.get("priority", "medium") or "medium")
            )
            if priority not in {"high", "medium", "low"}:
                priority = "medium"
            try:
                exec_group = int(content.get("execution_group_hint", 0) or 0)
            except (TypeError, ValueError):
                exec_group = 0
            acceptance_criteria = str(
                content.get("acceptance_criteria", "") or ""
            ).strip()
            test_command, test_command_source = self._resolve_test_command(
                failed_vp_id, content,
            )
            files_to_modify = self._resolve_files_to_modify(content)

            # --------------------------------------------------------------
            # 2026-10-11 generation-time gates. Both run BEFORE ``emitted_seq``
            # is bumped, so a refused row consumes no id and the returned
            # list's ids stay contiguous over the tasks actually emitted
            # (a gap would read as "a task vanished", which is the exact
            # failure mode these gates exist to stop repeating silently).
            # --------------------------------------------------------------
            if not files_to_modify:
                self.dropped_empty_files_to_modify += 1
                self._log_dropped(
                    "repair_task_dropped_empty_files_to_modify",
                    failed_vp_id, title,
                    reason=(
                        "no files_to_modify resolved — the task would be "
                        "dispatched with no declared blast radius"
                    ),
                )
                continue
            # ``test_command`` is NOT a gate. It was one, and the refusal
            # turned out to be structural rather than situational: a plan
            # whose verification points are declarative API tests carries
            # no ``test_command`` string on any VP, so the model is never
            # asked for one, the VP path has nothing to return and the
            # single-evidence path has zero usable commands rather than
            # one. Every derived task then died here, the round repaired
            # nothing, and the generation step reported a plausible
            # count of refusals rather than an error.
            #
            # The two ways out are tried in order, and neither is
            # "discard":
            #
            #   A. Regenerate. :meth:`RepairTaskGenerator.generate_repair_contents`
            #      already re-asked the model, up to
            #      :data:`MAX_TEST_COMMAND_FILL_ATTEMPTS` times, carrying the
            #      reason the previous attempt was unusable. A command
            #      found that way arrives on the content dict and is
            #      picked up above as the ``llm_retry`` source.
            #   B. Accept by category. What remains is a task whose
            #      deliverable is a findings document rather than a
            #      replayable command — see
            #      :meth:`_is_audit_style_repair`. It is labelled and
            #      emitted, not discarded.
            #
            # What this costs, stated plainly: a task that reaches the
            # executor with no command has no second *objective* signal,
            # so its verdict is the sub-agent's claim plus an independent
            # adversarial review. That is a weaker check, and it is why
            # the degradation is counted (``unverifiable_no_test_command``)
            # and logged rather than absorbed. It is not, however, the
            # single-signal mode the dual-criterion rule exists to
            # prevent: ``agent._audit_task_second_pass`` routes every task
            # without a runnable command through a fresh adversarial
            # ``claude -p`` review, and that review can veto the
            # completion. A repair round that emits nothing cannot
            # converge at all.
            if not test_command:
                self.unverifiable_no_test_command += 1
                self._log_emitted(
                    "repair_task_unverifiable_no_test_command",
                    failed_vp_id, title,
                    reason=(
                        "no test_command resolved and the fill loop is "
                        "exhausted; emitting the repair with an empty "
                        "command, judged by cross_verify + the adversarial "
                        "second pass"
                        if test_command_source == "missing"
                        else
                        "no test_command resolved; this verification "
                        "method's deliverable is an evidence artifact, so "
                        "the task is emitted as audit-style and judged by "
                        "cross_verify + the adversarial second pass"
                    ),
                    source=test_command_source,
                )

            emitted_seq += 1
            task_id = f"repair-r{self.round}-{emitted_seq:02d}"
            # The source is booked on the assembler rather than on the task
            # dict: ``PlanTaskRepository.add_task`` rejects any key outside
            # ``ALLOWED_STATIC_TASK_FIELDS`` and there is no
            # ``test_command_source`` column. Same reason the generator
            # keeps its sources out of the task dict.
            self.test_command_sources[task_id] = test_command_source
            task = {
                "id": task_id,
                "task_group": f"repair-round-{self.round}",
                "execution_group": exec_group,
                "priority": priority,
                "depends_on": self._infer_depends_on(emitted_seq),
                "title": title,
                "description": description,
                "acceptance_criteria": acceptance_criteria,
                # 2026-09-14: the content-only refactor (2026-09-07) dropped
                # ``test_command`` and ``files_to_modify`` from the emitted
                # task, so every repair task reached the executor with no
                # command at all — which made the dual-criterion completion
                # rule silently degrade to "trust the AI claim" (see
                # ``agent._cross_verify`` → ``test_cross_verify_unverified``).
                # ``files_to_modify`` is still a hard gate above. An empty
                # ``test_command`` is emitted and labelled instead, because
                # a verification point is not a shell command and lacking one
                # is no evidence against repairing the failure it describes.
                "test_command": test_command,
                "files_to_modify": files_to_modify,
                "failed_vp_id": failed_vp_id,
                "round": self.round,
                "status": "pending",
            }
            # Where the repair runs.  The plan's registered workspace —
            # the verification worktree — so the fix lands in the tree
            # the verification branch owns and not in the pull request
            # head or the main checkout.  Resolved from the content the
            # generator stamped, from an explicit caller override, or
            # from the plan's execution row; the first two are the same
            # value in production, and the row is the field the whole
            # execution chain reads.
            workspace = self._resolve_task_project_dir(content)
            if workspace:
                task["project_dir"] = workspace
            tasks.append(task)
        return tasks

    def _resolve_task_project_dir(self, content: Dict[str, Any]) -> str:
        """The directory this task's repair runs in.

        The plan's execution row is the authority — it is the one field
        the whole execution chain reads, and where a verification run
        registers its worktree.  Two things carry it here:

        1. the ``project_dir`` the generator stamped on the content it
           produced (:meth:`RepairTaskGenerator.generate_repair_contents`
           resolves the row once per round, so every task of a round
           agrees);
        2. the row itself, read now, for content this assembler was
           handed from somewhere else.

        The caller's own ``project_dir`` is the *fallback* for a plan
        with neither — the same precedence
        :func:`resolve_repair_project_dir` uses, so the two layers of
        the round never disagree about where the repair happens.

        Returns ``""`` when none of them knows — a plan with no
        execution row and no caller-supplied directory.  The task is then
        emitted without the field, which is the state every repair task
        was in before the workspace existed.
        """
        stamped = str(content.get("project_dir") or "").strip()
        if stamped:
            return stamped
        plan_dir = Path(self.plan_path).parent if self.plan_path else None
        if plan_dir is None or not plan_dir.name:
            return self.project_dir
        return resolve_repair_project_dir(
            plan_dir, Path(self.project_dir) if self.project_dir else None,
        ) or ""

    def _plan_points(self) -> Dict[str, Dict[str, Any]]:
        """``{vp_id: verification point}`` from the plan, or ``{}``.

        Loaded once per assembler, lazily, and only from code paths that
        actually need a plan-side field.

        Why this exists at all: the ``failed_vps`` dicts this assembler
        is handed come from the verification *report* side, and those
        entries carry ``id`` / ``title`` / ``priority`` /
        ``actual_result`` / ``evidence`` plus — when the plan had one —
        ``test_command`` and ``expected_result``. They do **not** carry
        ``verification_method``. So a classifier reading
        ``verification_method`` off a failed-VP dict classifies nothing
        and says so with total confidence: every point reads as
        "unrecognised method". That is the same shape as the
        ``evidence_command`` / ``test_command`` split this module's
        comments keep returning to — a reader that checks one key name
        and concludes the field is absent.

        The plan is the authority for what a verification point *is*, so
        it is read from there. A missing or unreadable plan yields
        ``{}`` and both callers fall back to their "unknown" answers,
        which is the safe direction for each.
        """
        if self._plan_points_cache is not _UNSET:
            return self._plan_points_cache
        points: Dict[str, Dict[str, Any]] = {}
        if self.plan_path:
            try:
                raw = json.loads(
                    Path(self.plan_path).read_text(encoding="utf-8"),
                )
            except (OSError, ValueError):
                raw = None
            listed = (
                raw.get("verification_points") if isinstance(raw, dict) else None
            )
            if isinstance(listed, list):
                points = {
                    str(p.get("id")): p
                    for p in listed
                    if isinstance(p, dict) and p.get("id")
                }
        self._plan_points_cache = points
        return points

    def _vp_verification_method(self, failed_vp_id: str) -> str:
        """The VP's declared ``verification_method``, lowercased, or ``""``.

        Report-side entry first (a caller that *does* pass the field
        wins), then the plan. The two-key lookup mirrors the
        report-side reader's own ``verification_method`` / ``method``
        alias pair, so a point written either way classifies the same.
        """
        entry = self.failed_vps.get(str(failed_vp_id)) or {}
        method = str(
            entry.get("verification_method") or entry.get("method") or "",
        ).strip().lower()
        if method:
            return method
        point = self._plan_points().get(str(failed_vp_id)) or {}
        return str(
            point.get("verification_method") or point.get("method") or "",
        ).strip().lower()

    def _is_audit_style_repair(self, failed_vp_id: str) -> bool:
        """True when this VP's declared deliverable is a findings document.

        The question this answers is narrow on purpose: *does this class
        of task have an acceptance command at all?* It is not "is this
        repair difficult" and not "is the task worth doing".

        The basis is the VP's own ``verification_method`` — the plan
        author's declaration of what the deliverable is — against the
        positive whitelist in :data:`AUDIT_STYLE_VERIFICATION_METHODS`.
        Two reasons for that axis and not another:

        * It is **author-declared**, so it does not depend on parsing
          prose. Anything inferred from the title or description can be
          wrong in a way that is invisible after the fact, and a wrong
          "this needs no command" is the one guess that silently
          weakens a completion check.
        * It is the **same axis** :meth:`_api_replay_command` already
          branches on, so the two halves of this class agree by
          construction: ``api_test`` gets a replay command and is never
          audit-style; the artifact methods get neither, and say so.

        Returns ``False`` for every unrecognised method. That is the
        conservative direction the caller needs: a false negative costs
        one retry round (the fill loop has already run, so it costs one
        extra LLM call), whereas a false positive files a task that
        could have been verified under the label "needs no command".
        Either way the row is still emitted — this method chooses the
        *label*, never whether the task exists.
        """
        return self._vp_verification_method(failed_vp_id) in (
            AUDIT_STYLE_VERIFICATION_METHODS
        )

    def _log_dropped(
        self, event: str, failed_vp_id: str, title: str, reason: str,
    ) -> None:
        """Emit one greppable, per-round-countable refusal record.

        The event name is the grep anchor (``repair_task_dropped_*``); the
        line carries the round so refusals can be tallied per round, and
        the offending ``failed_vp_id`` + title so an operator can go read
        that VP's evidence and decide whether to re-run the round by hand.

        Uses ``print`` rather than a module logger because
        ``repair_generator`` is imported by the orchestrator in contexts
        that do not set one up — the existing round/id diagnostics at the
        assembly site already do the same.
        """
        print(
            f"[Repair] {event} round={self.round} "
            f"failed_vp_id={failed_vp_id} title={title!r} reason={reason}"
        )

    def _log_emitted(
        self, event: str, failed_vp_id: str, title: str, reason: str,
        source: str = "",
    ) -> None:
        """Emit one greppable record for a task that went out unchecked.

        The counterpart to :meth:`_log_dropped`, and the distinction is
        the whole point: ``repair_task_dropped_*`` means a row never
        reached the executor, ``repair_task_unverifiable_*`` means one
        did and its completion rests on a weaker pair of signals. Both
        are countable per round, so "this round repaired nothing" and
        "this round repaired things it cannot objectively check" read as
        two different numbers instead of the same silence.

        ``source`` carries the value from
        :data:`ASSEMBLER_TEST_COMMAND_SOURCES`, so a tally can also be
        broken down by how the row came to have no command.
        """
        print(
            f"[Repair] {event} round={self.round} "
            f"failed_vp_id={failed_vp_id} title={title!r} "
            f"source={source} reason={reason}"
        )

    # ------------------------------------------------------------------
    # Command + file-scope resolution (2026-09-14)
    # ------------------------------------------------------------------

    # Phrases that mean "the VP's own test_command is what needs fixing"
    # rather than "the code under test needs fixing". Mirrors the
    # guidance already in ``REPAIR_CONTENT_SYSTEM_PROMPT`` rule 4 and in
    # the legacy deterministic builder.

    def _resolve_test_command(
        self, failed_vp_id: str, content: Dict[str, Any],
    ) -> Tuple[str, str]:
        """``(command, source)`` for this repair — ``""`` is a legal answer.

        The command that must exit 0 for this repair to count as done,
        plus which of :data:`ASSEMBLER_TEST_COMMAND_SOURCES` produced it.
        A command that does not resolve is no longer a refusal; the two
        *absences* it can be are reported as distinct sources
        (``audit_style`` vs ``missing``) and handled by
        :meth:`assemble`.

        Four cases, in order:

        1. **The content dict carries a command** — the model produced one
           and it survived :meth:`RepairTaskGenerator._resolve_repair_test_command`'s
           quality and falsifiability checks, either on the first
           content call or in the bounded fill loop. Source ``llm`` /
           ``llm_retry``.
        2. **The VP has a command** — return it verbatim. Re-running it is
           exactly the proof that the deviation is gone. "The VP's
           command" means its ``evidence_command`` (with ``test_command``
           as the legacy alias) as resolved by
           :func:`~verification.verification_report_reader.extract_vp_test_commands`.
           It is still a *declared* command, so it is held to
           :func:`test_command_quality.inspect_command` before it is
           returned; a rejection falls through to case (3) rather than
           shipping a chain that can only exit non-zero.
        3. **The method yields one** — :meth:`_api_replay_command`
           replays an ``api_test`` VP's own request + assertions. Source
           ``api_replay``. Same shape check, for the same reason: both
           cases (2) and (3) reach the completion gate as a command the
           plan — not this module — wrote.
        4. **Nothing runnable exists.** Return ``""`` with the label that
           says *why*: ``audit_style`` when the VP's declared deliverable
           is an evidence artifact, ``missing`` when it is not and the
           fill loop has run out of attempts.

        A fifth case used to exist here — "the VP's own command is the
        thing being repaired", which emitted a guard command for repairs
        that rewrote ``verification_plan.json``. That code path was
        deleted on 2026-09-18 along with the two repair patterns that
        edited the acceptance criterion itself; repair tasks must not
        edit what judges them.
        """
        declared = str(content.get("test_command") or "").strip()
        if declared:
            source = str(content.get("test_command_source") or "").strip()
            if source not in self.ASSEMBLER_TEST_COMMAND_SOURCES:
                source = "llm"
            return declared, source

        command = (self.vp_test_commands.get(failed_vp_id) or "").strip()
        if command:
            runnable = inspect_command(command)
            if not runnable:
                return command, "vp"
            print(
                f"[Repair] Discarding structurally-broken VP command "
                f"for {failed_vp_id} ({runnable[0].code}): {command[:120]}"
            )
        # 2026-10-11 — semantics of the map this reads.
        #
        # ``self.vp_test_commands`` is populated by
        # :func:`verification.verification_report_reader.extract_vp_test_commands`,
        # which resolves each VP's ``evidence_command`` first and falls
        # back to the legacy ``test_command`` spelling. On the verification
        # side ``evidence_command`` means "the command that produces this
        # VP's evidence" (``verification_ci_runner.py``,
        # ``prompts.py``); that meaning is untouched and nothing here
        # rewrites it.
        #
        # This method reuses it as the repair round's **objective**
        # acceptance criterion, on purpose. What it replaces is not a
        # working command — it is an empty one. A repair task whose
        # ``test_command`` resolves to ``""`` has no objective signal at
        # all, and the closest thing the plan has on disk is the command
        # that exercises the VP's subject.
        #
        # 2026-09-18: when no command resolves, the acceptance command is
        # derived from the *method's* judging basis instead:
        #
        #  * ``api_test`` — replay the VP's own request + assertions. This
        #    is the ideal shape for a repair's gate: the criterion comes
        #    from the requirement (not from the implementation), and the
        #    framework executes it deterministically.
        #  * ``ui_validation`` / ``code_review`` — no command. Their basis
        #    is the evidence artifact (checkpoints / citations), which a
        #    command cannot reproduce. That is the ``audit_style`` case,
        #    and it is emitted rather than discarded.
        #
        # What is gone for good is case 2 of the old behaviour: "the VP's
        # own command is the thing being repaired". Repair tasks must not
        # edit the acceptance criterion — see the module docstring of
        # ``verification_plan_delta`` and the C8 commit message.
        replay = self._api_replay_command(failed_vp_id)
        if replay:
            # Derived like source (2), so it is held to the same shape
            # rule — see
            # :meth:`RepairTaskGenerator._resolve_repair_test_command`.
            # :meth:`_api_replay_command` quotes every interpolated path
            # and its own literals contain no inspection verb, so the
            # command it builds passes the check; the check is here so
            # that stays a property rather than an accident of today's
            # plan paths. A VP whose ``request`` or ``assertions`` are
            # themselves malformed can otherwise put a chain on the
            # completion gate that can only ever exit non-zero.
            runnable = inspect_command(replay)
            if not runnable:
                return replay, "api_replay"
            print(
                f"[Repair] Discarding structurally-broken api_replay "
                f"command for {failed_vp_id} "
                f"({runnable[0].code}): {replay[:120]}"
            )

        if self._is_audit_style_repair(failed_vp_id):
            return "", "audit_style"
        return "", "missing"

    def _api_replay_command(self, failed_vp_id: str) -> str:
        """A runnable command that replays an ``api_test`` VP's assertions.

        Exits 0 only if every assertion still holds, which is exactly the
        question a repair has to answer. Returns "" for VPs of any other
        method.

        The method is read through :meth:`_vp_verification_method` rather
        than straight off the failed-VP dict, and that indirection is
        load-bearing: the report-side entry does not carry
        ``verification_method`` (see :meth:`_plan_points`), so reading it
        from there made this branch return ``""`` for *every* VP — the
        one case it exists to serve included. The method check therefore
        silently never fired, and an ``api_test`` point with no command
        of its own was labelled the same as a method nothing can
        reproduce.
        """
        if self._vp_verification_method(failed_vp_id) != "api_test":
            return ""
        if not self.plan_path:
            return ""
        # The runner lives in the backend, not in the target project, so
        # the command cds there before invoking it. The request URL is
        # absolute (service placeholders resolve to 127.0.0.1:port), so the
        # target project's cwd is irrelevant to the replay itself.
        backend_dir = Path(__file__).resolve().parent
        return (
            f"cd {self._shell_quote(str(backend_dir))} && "
            f"{self._shell_quote(sys.executable)} -m verification_api_runner "
            f"--plan {self._shell_quote(str(Path(self.plan_path).parent))} "
            f"--vp {self._shell_quote(str(failed_vp_id))}"
        )


    @staticmethod
    def _shell_quote(value: str) -> str:
        """POSIX single-quote ``value`` (safe for arbitrary bytes)."""
        return "'" + str(value).replace("'", "'\"'\"'") + "'"

    def _resolve_files_to_modify(self, content: Dict[str, Any]) -> List[str]:
        """Normalised file scope for the repair task.

        Preferred source is the LLM's ``files_to_modify`` hint (the same
        shape the main task generator emits). Entries are normalised and
        filtered by :func:`normalise_declared_files`: relative, no
        ``..`` traversal, no absolute paths, no empty strings. An empty
        list means "scope unknown" — the executor then gives the
        subagent no file hint and lets it discover the paths, which is
        strictly better than a scope pointing at the wrong file.

        The filter itself lives at module level because the banned-region
        fallback needs the same normalisation for the paths it checks
        against the plan's constraints.
        """
        return normalise_declared_files(content.get("files_to_modify"))

    def _fallback_description(
        self, failed_vp_id: str, vp_plan: Dict[str, Any]
    ) -> str:
        """Minimal-but-actionable description for a half-formed LLM row.

        Anchored on what the on-disk evidence already gives us: a summary
        of what was observed, and the paths an agent can Read for the full
        output. Deliberately terse — the executor subagent reads the
        referenced files rather than trusting prose written blind.
        """
        lines = [
            f"验证点 {failed_vp_id} 在最近一轮验证中判定为 FAILED，"
            f"需要修复其根因后重新验证。",
        ]
        summary = str(vp_plan.get("actual_result_summary", "") or "").strip()
        if summary:
            lines.append(f"实际结果摘要：{summary}")
        if vp_plan.get("title"):
            lines.append(f"验证点标题：{vp_plan['title']}")
        paths = vp_plan.get("evidence_paths") or []
        if paths:
            lines.append("证据（用 Read 工具读取）：")
            lines.extend(f"  - {p}" for p in paths)
        command = str(vp_plan.get("test_command_summary", "") or "").strip()
        if command:
            lines.append(f"复现命令：{command}")
        return "\n".join(lines)

    def _infer_depends_on(self, seq: int) -> List[str]:
        """Return the list of task IDs this task must wait for.

        Always ``[]``: one repair batch is a forest of roots.

        ``seq`` is accepted (and ignored) so the signature stays stable
        for the existing callers and tests.

        History — why this used to return ``[f"repair-r{round}-{seq-1:02d}"]``
        ----------------------------------------------------------------
        2026-09-16 (cross-round sentinel, already removed). Round N's
        first task depended on a ``repair-r{round - 1}-99`` sentinel.
        No code path ever assigns that id, so
        ``Agent._validate_dependencies`` rejected the whole DAG at load
        time and every plan with a ≥2 repair round died before running
        a task.

        2026-09-20 (same-round chain — **this is the edge removed on
        2026-10-11**). The within-batch chain
        ``repair-r{round}-{seq-1:02d}`` was justified in this function's
        own docstring by exactly one reason: *"This protects against
        racing two repair tasks against the same
        ``verification_plan.json`` write."*

        That reason stopped being true on 2026-09-18, when the two
        code paths that let a repair agent write
        ``verification_plan.json`` were deleted outright — see the
        ``_resolve_test_command`` note ("Repair tasks must not edit
        the acceptance criterion"). The only two remaining writers of
        that file are ``verification_agent.py`` (bare ``open(...,"w")``)
        and ``vp_split.py`` (``mkstemp`` + ``replace``); both run
        in-process, single-threaded, inside a verification round, and
        are separated from the repair executor's subprocess by a
        process boundary. There is nothing left to race against.

        What the fake edge actually cost: a whole batch of repair tasks,
        serialised into one chain, so only the chain head ever ran. The
        executor's first micro-layer built exactly one schedulable task,
        the head, and when the head failed every later task inherited a
        ``failed`` upstream and was never schedulable — the round ends
        in ``No schedulable micro-layer found`` with the rest of the
        batch left permanently ``pending``.

        Real intra-batch conflicts are still possible (two repairs can
        touch the same source file) and are handled by the mechanism
        that actually models them, not by a fake edge:
        :func:`agent._build_micro_layers` builds a conflict graph from
        ``files_to_modify`` and serialises tasks that share a file into
        separate micro layers. That graph is only meaningful because
        ``files_to_modify`` is populated, which is the whole reason
        :meth:`assemble` now refuses to emit a task without one.

        The dependency contract itself is untouched and stays strict:
        ``agent.is_dependency_ready`` still treats
        ``{"completed", "skipped"}`` as success and ``failed`` /
        ``pending`` / ``in_progress`` as blocking. This function stops
        *manufacturing* edges that were never real; it does not weaken
        the ones that are.
        """
        return []


def _evidence_items_from_failed_vps(
    failed_vps: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Adapt failed-VP dicts to the shape :meth:`_resolve_repair_test_command` reads.

    That method predates the split into contents + assembler and takes
    ``evidence_items`` (``{vp_id, test_command, actual_result,
    evidence}``). The live pipeline hands it
    :meth:`~repair_generator.RepairTaskGenerator.generate_repair_contents`'s
    ``failed_vps`` instead, whose command lands under the same
    ``test_command`` key but whose evidence fields are named
    ``evidence`` / ``actual_result`` — so the mapping is nearly the
    identity and is kept explicit rather than assumed.

    Doing it here (instead of loosening the resolver's expectations) is
    what lets the fill loop reuse the resolver verbatim: the three
    derivation paths, their order and their rejection rules stay exactly
    as they were, and the retry is judged against the same bar as the
    first answer.
    """
    items: List[Dict[str, Any]] = []
    for vp in failed_vps:
        if not isinstance(vp, dict) or not vp.get("id"):
            continue
        items.append({
            "vp_id": str(vp.get("id")),
            "test_command": str(vp.get("test_command") or "").strip(),
            "actual_result": str(vp.get("actual_result") or ""),
            "evidence": str(vp.get("evidence") or ""),
        })
    return items


def _extract_filled_test_command(response: Any, vp_id: str) -> str:
    """Pull ``vp_id``'s command out of a fill response, or ``""``.

    Tolerant on the envelope because the model has been observed writing
    each of these shapes: ``{"commands": [{"failed_vp_id", ...}]}`` as
    asked, a bare list, and a flat ``{vp_id: command}`` map. A response
    that matches none of them yields ``""``, which the caller records as
    a reason and retries — the loop treats an unreadable answer exactly
    like an unusable one, because from the assembler's point of view
    they are the same event.
    """
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, list):
        return _extract_filled_test_command({"commands": response}, vp_id)
    if not isinstance(response, dict):
        return ""
    commands = response.get("commands")
    if isinstance(commands, list):
        for entry in commands:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("failed_vp_id") or "").strip() != vp_id:
                continue
            command = str(entry.get("test_command") or "").strip()
            if command:
                return command
        return ""
    # Flat map, or the command carried at top level for a single VP.
    for key in (vp_id, "test_command", "command"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_test_command_fill_prompt(
    failed_vps: List[Dict[str, Any]],
    plan_vps: Dict[str, Dict[str, Any]],
    reasons: Dict[str, str],
    *,
    attempt: int,
    max_attempts: int,
    plan_dir: Path,
    project_dir: Path,
) -> str:
    """Build the retry prompt for :meth:`_fill_missing_test_commands`.

    Carries the three things a first answer lacked, which is what makes
    a retry a correction rather than a reroll:

    * **the whole verification point** — ``verification_method``,
      ``request``, ``assertions``, ``expected_result``. For an
      ``api_test`` these *are* the contract, so they are what a runnable
      check can be written from. Read from
      ``verification_plan.json`` because the report-side failed-VP dict
      does not carry them.
    * **why the previous attempt was unusable** — a quality-check code,
      a vacuous-command verdict, or silence. Fed back per VP, so a model
      that produced a ``grep`` probe is told *that specific* probe was
      rejected rather than being asked again in the same terms.
    * **how the point actually failed** — ``actual_result`` and the
      evidence pointers, so the command targets the failure rather than
      the module.

    ``plan_dir`` / ``project_dir`` are stated the way the content prompt
    states them, so "relative to the project root" has an unambiguous
    referent in both prompts.
    """
    parts = [
        f"为下面 {len(failed_vps)} 个验证点补验收命令"
        f"（第 {attempt}/{max_attempts} 次尝试）。",
        "",
        f"Plan directory: {plan_dir}",
        f"Project directory: {project_dir}",
        "",
        "每条命令必须在**修复正确**时退出 0、在**未修复**时非 0。",
        "",
    ]
    for vp in failed_vps:
        vp_id = str(vp.get("id", "") or "")
        plan_point = plan_vps.get(vp_id) or {}
        parts.append(f"--- {vp_id} ---")
        parts.append(f"title: {vp.get('title', '') or plan_point.get('title', '')}")
        method = str(
            plan_point.get("verification_method") or plan_point.get("method") or "",
        ).strip()
        if method:
            parts.append(f"verification_method: {method}")
        if plan_point.get("expected_result"):
            parts.append(
                f"expected_result: "
                f"{_clamp_for_prompt(plan_point['expected_result'])}"
            )
        if plan_point.get("request"):
            parts.append(
                f"request: {_clamp_for_prompt(plan_point['request'])}"
            )
        if plan_point.get("assertions"):
            parts.append(
                f"assertions: {_clamp_for_prompt(plan_point['assertions'])}"
            )
        if vp.get("actual_result"):
            parts.append(
                f"actual_result: {_clamp_for_prompt(vp.get('actual_result'))}"
            )
        for path in (vp.get("evidence_paths") or []):
            parts.append(f"evidence_path: {path}")
        if vp_id in reasons:
            parts.append(f"上一次为什么不可用: {reasons[vp_id]}")
        parts.append("")
    parts.append(
        "如果某个验证点本质上没有可执行命令（交付物是一份审查结论 / "
        "检查点记录），不要为它编一条恒真的命令 —— 宁可不给。"
    )
    return "\n".join(parts)


#: Heading of the plan-intent section inside the repair-content prompt.
#: Named here (rather than written inline) because the system prompt
#: refers to this section by name — the two have to stay in sync.
PLAN_INTENT_HEADING = "## 原计划的初衷与硬约束"

#: What the prompt tells the model to do when a proposed fix would
#: cross one of the plan's own constraints.
PLAN_INTENT_CONFLICT_RULES = (
    "在做决定之前，先判断这条修复是否与上面任何一条冲突。",
    "冲突时不要生成修复任务；如果同一条失败还有不冲突的修法，就用那种修法。",
)


def _normalise_plan_intent(plan_intent: Any) -> List[str]:
    """Turn a raw intent sequence into the list of sentences to render.

    Every entry is stringified and stripped; an entry that is blank
    afterwards carries no constraint and is dropped. The order of the
    surviving entries is the order they were handed over in — the
    numbering in the prompt follows it.
    """
    if not plan_intent:
        return []
    if isinstance(plan_intent, (str, bytes)):
        # A bare string is one sentence, not a sequence of characters.
        plan_intent = [plan_intent]
    normalised: List[str] = []
    for entry in plan_intent:
        try:
            text = str(entry).strip()
        except Exception:  # noqa: BLE001 — a broken entry is not a failure
            continue
        if text:
            normalised.append(text)
    return normalised


def _build_repair_contents_prompt(
    failed_vps: List[Dict[str, Any]],
    round_number: int,
    plan_dir: Path,
    project_dir: Path,
    previous_failure_feedback: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    repair_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
    plan_intent: Sequence[str] = (),
) -> str:
    """Build the single LLM prompt for repair-content generation.

    The prompt is intentionally short — the on-disk
    ``verification_report.json`` and ``verification_plan.json``
    already carry every fact the LLM needs, and embedding that data
    inline avoids burning tokens re-deriving it from PRD context. The
    LLM's only job is to turn each failed VP's ``actual_result`` /
    ``evidence`` into a concrete ``title / description /
    acceptance_criteria`` triple.

    2026-09-12: when ``previous_failure_feedback``
    is non-empty, the prompt prepends a "previous attempts" section
    listing each prior round's actual_result + evidence for the same
    VP. This replaced an earlier "cap at 3 rounds" design: feeding the
    failure back gives the generating agent information to learn from,
    whereas a hard cap simply ends the chain without a hint.

    2026-10-23: ``plan_intent`` carries the constraint sentences the
    plan states about itself (see :func:`_plan_intent`). They are
    rendered as their own section *before* the VP list, so the model
    reads the plan's terms of reference before it reads the failures
    it is about to repair. An empty intent renders no section at all —
    a plan that states no constraint gets no constraint section.
    """
    parts = [
        f"Round {round_number}: generate one repair task per FAILED verification point.",
        "",
        f"Plan directory: {plan_dir}",
        f"Project directory: {project_dir}",
        "",
    ]
    intent_items = _normalise_plan_intent(plan_intent)
    if intent_items:
        parts.append(PLAN_INTENT_HEADING)
        parts.append("")
        for index, item in enumerate(intent_items, start=1):
            parts.append(f"{index}. {item}")
        parts.append("")
        parts.extend(PLAN_INTENT_CONFLICT_RULES)
        parts.append("")
    if previous_failure_feedback:
        # Filter to VPs that still appear in this round's failures —
        # otherwise we'd show feedback for already-fixed VPs which
        # would just confuse the agent.
        current_vp_ids = {str(vp.get("id", "")) for vp in failed_vps}
        relevant_feedback = {
            vp_id: history
            for vp_id, history in previous_failure_feedback.items()
            if vp_id in current_vp_ids and history
        }
        if relevant_feedback:
            parts.append(
                "## ⚠️ 上次修复尝试未生效 — 必须尝试新方案"
            )
            parts.append("")
            parts.append(
                "以下 VP 在前几轮已经生成过 repair task 并由 executor 执行，"
                "但仍然失败。每个 VP 下方列出了每次失败时的实际结果和证据。"
                "请基于这些信息重新分析根本原因，**不要重复相同的方案**——"
                "上次那条路已经被验证无效。"
            )
            parts.append("")
            for vp_id, history in relevant_feedback.items():
                parts.append(f"### {vp_id} — 之前 {len(history)} 次失败")
                for h in history:
                    # 2026-09-14: history entries seeded from state.db's
                    # accumulated verdicts carry no round number (the
                    # column has none) — they use an ordinal ``label``
                    # ("既往失败 #2") instead of a fabricated round.
                    heading = h.get("label") or f"Round {h.get('round', '?')}"
                    # 2026-10-11: these two were ``[:600]``. With
                    # ``actual_result`` now carrying every verdict
                    # reason (see
                    # ``Orchestrator._verdict_to_result``) the 600-char
                    # slice removed exactly the tail that states which
                    # criterion failed, so the repair agent could see a
                    # history entry in which every criterion passed.
                    # 8000 + an explicit truncation marker instead.
                    parts.append(
                        f"- **{heading}** "
                        f"actual_result: "
                        f"{_clamp_for_prompt(h.get('actual_result'), label='actual_result')}"
                    )
                    parts.append(
                        f"  evidence: "
                        f"{_clamp_for_prompt(h.get('evidence'), label='evidence')}"
                    )
                parts.append("")
    parts.append(f"共 {len(failed_vps)} 个失败 VP：")
    parts.append("")
    for i, vp in enumerate(failed_vps, start=1):
        parts.append(f"--- VP {i} ({vp.get('id')}) ---")
        parts.append(f"title: {vp.get('title', '')}")
        parts.append(f"priority: {vp.get('priority', 'medium')}")
        if vp.get("evidence_paths"):
            # Path-based shape (2026-09-10 plan). The long-form text is NOT
            # inlined: the previous shape truncated ``actual_result`` to
            # 1000 chars, which for a VP whose whole point is "which of the
            # 245 tests failed" threw away exactly the actionable part.
            # The agent Reads these files instead, so the prompt stays
            # bounded however large the captured output is.
            parts.append("evidence_paths (用 Read 工具读取):")
            for path in vp["evidence_paths"]:
                parts.append(f"  - {path}")
            if vp.get("actual_result_summary"):
                parts.append(
                    f"actual_result_summary: {vp['actual_result_summary']}"
                )
            if vp.get("evidence_summary"):
                parts.append(f"evidence_summary: {vp['evidence_summary']}")
            if vp.get("test_command_path"):
                parts.append(
                    f"test_command_path (用 Read 工具读取): "
                    f"{vp['test_command_path']}"
                )
            if vp.get("test_command_summary"):
                parts.append(
                    f"test_command_summary: {vp['test_command_summary']}"
                )
            if vp.get("expected_result_summary"):
                parts.append(
                    f"expected_result_summary: "
                    f"{vp['expected_result_summary']}"
                )
        else:
            # Legacy inlined shape — still supported so an older
            # ``verification_report.json`` reader (or a caller that has
            # not migrated yet) keeps producing a usable prompt.
            parts.append(
                f"actual_result: "
                f"{_clamp_for_prompt(vp.get('actual_result'), label='actual_result')}"
            )
            parts.append(
                f"evidence: "
                f"{_clamp_for_prompt(vp.get('evidence'), label='evidence')}"
            )
            if vp.get("test_command"):
                parts.append(f"test_command (from plan): {vp.get('test_command')[:500]}")
            if vp.get("expected_result"):
                parts.append(f"expected_result (from plan): {vp.get('expected_result')[:500]}")
        # 2026-09-14:
        # what the repair task GENERATED FOR THIS VP last round actually
        # did when the executor ran it. The VRD only says "the VP failed
        # again"; this says "we already tried `repair-r4-03`, it ran X and
        # ended <status>". Without it the LLM rewrites the same failed
        # remedy with different wording.
        outcome = (repair_outcomes or {}).get(str(vp.get("id", "")))
        if isinstance(outcome, dict) and outcome.get("repair_task_id"):
            status = outcome.get("repair_status") or "?"
            parts.append(
                f"上轮为该 VP 生成的修复任务: "
                f"`{outcome['repair_task_id']}` (执行结果: {status})"
            )
            if outcome.get("repair_task_title"):
                parts.append(f"  任务标题: {outcome['repair_task_title']}")
            if outcome.get("repair_test_command"):
                parts.append(
                    f"  验证命令: {outcome['repair_test_command']}"
                )
            if outcome.get("repair_attempt"):
                parts.append(f"  执行尝试次数: {outcome['repair_attempt']}")
            if outcome.get("repair_failure_reason"):
                parts.append(
                    f"  失败原因: {outcome['repair_failure_reason']}"
                )
            parts.append(
                "  → 该修复已被验证为无效或未完成，请给出**不同的**修复思路。"
            )
        parts.append("")
    parts.append(
        "为每个失败 VP 生成一条修复任务（纯 JSON，不要 markdown 代码块标记）："
    )
    return "\n".join(parts)


class _RepairContentGeneratorMixin:
    """Mixin exposing :meth:`generate_repair_contents` on
    :class:`RepairTaskGenerator` without inheriting from a new base.

    Kept as a thin mixin so the public method lands next to the rest
    of :class:`RepairTaskGenerator`'s API surface, but its
    implementation is small enough to live in one place.
    """

    def generate_repair_contents(
        self,
        failed_vps: List[Dict[str, Any]],
        round_number: int,
        previous_failure_feedback: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        repair_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
        plan_intent: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Single LLM call producing repair content for each failed VP.

        Args:
            failed_vps: The list returned by
                :func:`verification.verification_report_reader.extract_failed_vps_from_report`.
                Each item carries ``id``, ``title``, ``priority``,
                ``actual_result``, ``evidence``, and optionally
                ``test_command`` / ``expected_result`` and
                ``failure_class``.
            round_number: Current verification round number (1-based).
            previous_failure_feedback: 2026-09-12:
                ``{vp_id: [{round, actual_result, evidence}, ...]}``
                carrying the failure history of each VP across prior
                rounds. When a VP has been attempted before (the LLM
                generated a repair task for it but the executor's fix
                didn't take), the prompt includes those prior
                ``actual_result`` / ``evidence`` blocks verbatim with
                a "上次方案完全无效" instruction so the LLM is steered
                away from repeating the same approach. Without this
                feedback the LLM would just regenerate the same
                failing repair task each round.
            repair_outcomes: 2026-09-14: ``{vp_id:
                {repair_task_id, repair_task_title,
                repair_test_command, repair_status,
                repair_failure_reason, repair_attempt}}`` — what the
                repair task generated for this VP in a PREVIOUS round
                actually did when the executor ran it (read from
                state.db ``plan_tasks``; repair tasks are not in
                ``tasks.json``). Rendered under the VP so the LLM stops
                re-proposing a remedy that is already proven ineffective.
            plan_intent: 2026-10-23: the constraint sentences the plan
                states about itself (see :func:`_plan_intent`). ``None``
                means "read them from this plan's directory" — which is
                what every caller that does not care wants. An explicit
                list is used as-is, so a caller that already holds the
                intent does not pay for a second read. The sentences
                reach the prompt as their own section, before the VP
                list, and the system prompt (:data:`REPAIR_CONTENT_SYSTEM_PROMPT`)
                declares them hard constraints: a fix that would cross
                one has to be reported rather than generated.

        Returns:
            A list of content dicts, one per failed VP, each carrying:

            * ``failed_vp_id`` — the VP ID this content addresses
            * ``title`` — short label of the fix
            * ``description`` — full failure → fix narrative
            * ``acceptance_criteria`` — TDD-style acceptance spec
            * ``files_to_modify`` — the LLM's declared edit scope,
              carried through verbatim from the response. 2026-10-11:
              this key was silently dropped by
              :func:`_parse_repair_contents_response`, which rebuilt
              each row from a fixed key set that did not include it —
              so ``_resolve_files_to_modify`` always saw ``None`` and
              every repair task was stamped ``files_to_modify=[]``
              no matter what the LLM returned.
              :meth:`RepairTaskAssembler.assemble` now refuses a task
              with an empty scope outright, so the field has to survive
              the parser for that gate to be passable at all.
            * ``priority_hint`` — optional hint, the assembler
              reconciles with the plan-side ``priority``
            * ``execution_group_hint`` — optional integer, the
              assembler passes it through

            Returns an empty list only when there is genuinely nothing to
            repair: either ``failed_vps`` was empty, or every failed VP
            it carried was refused by the failure-class filter below.

            Raises:
                RepairGenerationError: the LLM call failed, or it
                    returned a shape with no usable content while
                    failures were pending. 2026-09-14 — this used to be
                    collapsed into ``[]``. The caller
                    (``check_cycle_conditions``) then treated "the
                    generator broke" as "no repair tasks available" and
                    recorded a terminal ``no_repair_tasks`` stop reason,
                    silently ending the chain on rounds that had real
                    failures.
        """
        if not failed_vps:
            return []
        # 2026-10-22 (P0-2): only a deliverable defect may become a
        # repair task. The other three classes want a different remedy
        # (see the block above :func:`select_repairable_failed_vps`),
        # and a repair task built for one of them is work nobody asked
        # for. The filter runs BEFORE the prompt is built, so the model
        # is never even asked about a VP that will not get a task, and
        # the parser is handed the filtered list, so a row that names a
        # refused VP is dropped as the hallucination it now is.
        #
        # The refusals are recorded rather than swallowed: a failure that
        # got no repair task is exactly the thing an operator asks about
        # ("why is there no task for VP-1?"), and "no task" is not an
        # answer.
        repairable, filtered = select_repairable_failed_vps(failed_vps)
        self.filtered_vps = filtered
        if filtered:
            print(
                f"[Repair] round {round_number}: {len(filtered)} failed VP(s) "
                f"are not deliverable defects — no repair task generated for "
                f"{', '.join(item['id'] for item in filtered)}"
            )
            for item in filtered:
                print(
                    f"[Repair]   {item['id'] or '<unnamed VP>'}: "
                    f"{item['reason']}"
                )
        if not repairable:
            return []
        failed_vps = repairable
        failed_vp_ids = [
            str(vp.get("id")) for vp in failed_vps if vp.get("id")
        ]

        # 2026-10-23 — the plan's own constraints. ``None`` means the
        # caller has none of its own and wants the plan directory read;
        # an explicit list (possibly empty) is taken as given.
        if plan_intent is None:
            plan_intent = _plan_intent(self.plan_dir)

        prompt = _build_repair_contents_prompt(
            failed_vps,
            round_number,
            self.plan_dir,
            self.project_dir,
            previous_failure_feedback=previous_failure_feedback,
            repair_outcomes=repair_outcomes,
            plan_intent=plan_intent,
        )
        # 2026-10-22 — the session handle is bound before the dispatch so
        # the id can be recorded afterwards, whether the call answered or
        # not: a call that died after dispatching still leaves behind a
        # session that exists and is worth re-asking in.
        session = repair_content_session(self.plan_dir)
        try:
            response = query_json_in_session(
                self.coding_tool,
                session,
                prompt=prompt,
                system_instruction=REPAIR_CONTENT_SYSTEM_PROMPT,
            )
        except VerificationCancelled:
            # 2026-10-22 — the operator stopped this plan. This is not a
            # generation failure: there is nothing to repair *because the
            # round was cancelled*, and turning it into a
            # ``RepairGenerationError`` would have the caller record a
            # failure for a round the operator paused on purpose. Let it
            # through untouched.
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised as a typed error
            print(
                f"[Repair] generate_repair_contents LLM call failed "
                f"(round={round_number}): {exc}"
            )
            raise RepairGenerationError(
                f"repair-content LLM call failed for round "
                f"{round_number}: {exc}",
                round_number=round_number,
                failed_vp_ids=failed_vp_ids,
            ) from exc
        # 2026-10-22 — record the session id in the plan's sidecar so a
        # ``/start`` in another process can re-open this conversation
        # instead of paying for the whole context again.
        persist_repair_content_session(self.plan_dir, session)
        contents = _parse_repair_contents_response(response, failed_vps)
        # One defect, one task.  The model is asked for one row per
        # failed verification point, and it usually answers that way —
        # but a second row for a defect the round already covers is two
        # commits for one fix, and the round's bookkeeping then counts
        # one fix as two.  The drop belongs here, where the model's
        # answer is read: the assembler is handed the content and
        # turns it into tasks, and a caller may hand it several rows
        # for one defect on purpose.
        contents = one_row_per_defect(contents, round_number)
        if not contents:
            # The model answered, but with nothing we can turn into a
            # task. With >=1 failed VP in hand that is a generation
            # failure, not convergence: ``{"tasks": []}`` / a malformed
            # envelope here would otherwise strand the failures.
            #
            # A failure that left nothing behind while the deterministic
            # rows survived is still a failure to report — the caller
            # parks the plan rather than recording "no repair needed"
            # over verification points that have no repair.
            raise RepairGenerationError(
                f"repair-content LLM returned no usable task for "
                f"{len(failed_vps)} failed VP(s) "
                f"({', '.join(str(vp.get('id')) for vp in failed_vps) or 'no ids'}) "
                f"in round {round_number}",
                round_number=round_number,
                failed_vp_ids=failed_vp_ids,
            )
        # Path B: a row that declared no edit scope gets one derived from
        # the failure by the shared rule, so the assembler's
        # "empty scope → refuse the task" gate does not throw the repair
        # away over a scope the model simply did not spell out.
        self._fill_missing_files_to_modify(contents, failed_vps)
        # Path A: a verification point that is not a shell command has no
        # command to copy, so the content prompt — which never asks for
        # one — cannot produce one either. Re-ask for it explicitly
        # before the assembler has to fall back to a label.
        self._fill_missing_test_commands(
            contents, failed_vps, round_number,
        )
        # Where the repair happens.  Stamped here, on the content the
        # assembler is about to turn into tasks, because the assembler
        # is handed the *content* and nothing else: the directory a
        # repair runs in is a property of the round, not of the model's
        # answer, so the generator — which knows the round — puts it
        # there.  The value is the plan's registered workspace (the
        # verification worktree), so the fix lands in the tree the
        # verification branch owns.
        workspace = resolve_repair_project_dir(
            self.plan_dir, self.project_dir,
        )
        if workspace:
            for content in contents:
                content["project_dir"] = workspace
        return contents


def _intent_check_text(value: Any) -> str:
    """Render one ``intent_check`` text field as stripped text.

    A model that has nothing to say sends ``null`` (or omits the key),
    so ``None`` becomes the empty string rather than the text "None".
    """
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 — a broken field is not a failure
        return ""


def _normalise_intent_check(value: Any) -> Dict[str, Any]:
    """Normalise the model's ``intent_check`` block into a fixed shape.

    The block is the machine-readable form of the model's judgement
    about the plan's own hard constraints (see
    :data:`REPAIR_CONTENT_SYSTEM_PROMPT` rule 7). It has to survive
    the parse unchanged enough that a downstream reader can tell
    "the model judged this out of scope" apart from "the model never
    looked" — the two are indistinguishable when the answer only
    exists as prose in ``description``.

    Rules:

    * a missing block, or one that is not a dict, degrades to
      ``{"violates": False, "constraint": "", "reason": ""}`` — "no
      conflict reported" is the safe reading, and a malformed block
      must never raise;
    * ``violates`` is true only for a real boolean ``True`` or the
      strings ``"true"`` / ``"yes"`` (case-insensitive). Models emit
      booleans as strings often enough that accepting only ``True``
      would lose the signal; other truthy values (``1``, ``"maybe"``)
      are *not* violations;
    * ``constraint`` / ``reason`` become stripped strings;
    * keys the model invented are dropped — the shape is fixed so a
      consumer can read it without guarding every field.
    """
    if not isinstance(value, dict):
        return {"violates": False, "constraint": "", "reason": ""}
    raw_violates = value.get("violates", False)
    violates = False
    if isinstance(raw_violates, bool):
        violates = raw_violates
    elif isinstance(raw_violates, str):
        violates = raw_violates.strip().lower() in ("true", "yes")
    return {
        "violates": violates,
        "constraint": _intent_check_text(value.get("constraint")),
        "reason": _intent_check_text(value.get("reason")),
    }


def _parse_repair_contents_response(
    response: Any,
    failed_vps: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalise the LLM response into a content list.

    Defensive against:

    * non-dict responses (``None``, ``list``, ``str``) — returns ``[]``
    * missing ``tasks`` key — returns ``[]``
    * ``tasks`` items missing ``failed_vp_id`` — fills it from the
      position in the input list (positional fallback so a
      well-behaved LLM that forgets the field still produces usable
      tasks)
    * items missing any of ``title`` / ``description`` /
      ``acceptance_criteria`` — drops the item rather than emit a
      half-formed task that would mislead the assembler
    """
    if not isinstance(response, dict):
        return []
    raw_tasks = response.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    failed_by_id: Dict[str, Dict[str, Any]] = {
        str(vp.get("id", "")): vp for vp in failed_vps if vp.get("id")
    }
    parsed: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            continue
        failed_vp_id = str(item.get("failed_vp_id", "") or "")
        if not failed_vp_id and idx < len(failed_vps):
            # Positional fallback: the i-th task addresses the i-th
            # failed VP. Only applies when the LLM omits the field
            # entirely.
            failed_vp_id = str(failed_vps[idx].get("id", ""))
        if not failed_vp_id or failed_vp_id not in failed_by_id:
            # LLM hallucinated a VP ID we don't recognise — skip.
            continue
        title = str(item.get("title", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        criteria = str(item.get("acceptance_criteria", "") or "").strip()
        if not title or not description:
            # Half-formed task — the assembler has no useful content
            # to wrap, and emitting a row with an empty description
            # would mislead the executor. Drop it; the deterministic
            # pattern-match path can still emit a fallback task if
            # the orchestrator chooses to retry.
            continue
        parsed.append({
            "failed_vp_id": failed_vp_id,
            "title": title,
            "description": description,
            "acceptance_criteria": criteria,
            "files_to_modify": item.get("files_to_modify"),
            # Carried verbatim for the same reason as ``files_to_modify``:
            # the bounded fill loop (:meth:`_fill_missing_test_commands`)
            # stamps its answer onto the content dict, and a parser that
            # rebuilds each row from a fixed key set would drop it again
            # on the way out. The content prompt itself never asks for
            # one, so an absent key here is the normal case and simply
            # means "nothing rescued this row".
            "test_command": item.get("test_command"),
            "test_command_source": str(item.get("test_command_source", "") or ""),
            "priority_hint": str(item.get("priority_hint", "") or ""),
            "execution_group_hint": item.get("execution_group_hint", 0),
            # The model's structured judgement about the plan's own hard
            # constraints. Carried through the parser so a downstream
            # reader can tell "the model judged this out of scope" from
            # "the model never looked" — a distinction that is lost the
            # moment the answer only lives in the description prose.
            # A missing / malformed block means "no conflict reported".
            "intent_check": _normalise_intent_check(item.get("intent_check")),
        })
    return parsed


# ---------------------------------------------------------------------------
# Plan intent — the constraints the plan states about itself.
# ---------------------------------------------------------------------------
#
# The repair path decides what to change from the verification evidence
# alone. A plan whose own material says "不碰 Rust" therefore still had
# Rust files rewritten by the auto-repair: the restriction lived in the
# plan directory and nowhere the repair path reads. The three helpers
# below read those restriction sentences back out of the plan directory
# so a caller can carry them into the repair prompt.

#: Plan-directory members that can carry the plan's own wording. The
#: imported material is the fast lane's own document; the PRD is the
#: document the regular lane writes. Both are read; neither is
#: required — a plan directory that has neither contributes nothing.
_PLAN_INTENT_FILES: Tuple[str, ...] = ("import.json", "prd.json", "prd.md")

#: Sentences are separated by these full-width terminators and by any
#: line break. Half-width punctuation is deliberately not a separator:
#: a path or a version number inside a sentence must not cut it in
#: half, and the terminators above are the ones Chinese plan material
#: actually uses.
_INTENT_SENTENCE_SPLIT = re.compile(r"[。！？；\r\n]+")

#: A sentence is a constraint only when it carries one of these markers.
#: They are the ways a plan says "not this, only that" — the shape the
#: restriction takes in plan material.
_INTENT_MARKERS: Tuple[str, ...] = (
    "不碰",
    "不要改",
    "不得",
    "禁止",
    "不要动",
    "不在本期范围",
    "只改",
    "仅限",
)

#: The one ASCII marker, matched case-insensitively because English
#: plan material capitalises it freely.
_INTENT_MARKER_ASCII = "out of scope"

#: Cap on a single collected sentence, so one runaway paragraph cannot
#: fill a prompt.
_MAX_INTENT_SENTENCE_CHARS = 300

#: Cap on how many sentences one plan contributes.
_MAX_INTENT_SENTENCES = 20


def _flatten_json_strings(value: Any) -> List[str]:
    """Collect every string inside a JSON-ish value, in document order.

    The imported material is a JSON *value*, not a string: it can be a
    bare string, an object holding nested objects, or a list. A
    restriction can sit at any depth, so the whole value is walked and
    the strings are kept in the order they appear.

    A string is first tried as JSON. The fast lane's payload is
    regularly a JSON document *encoded inside* a string, and decoding it
    is what makes the nested wording reachable at all. A string that is
    not JSON is kept verbatim — that is the plain-text case, and the
    common one.

    Non-string scalars (numbers, booleans, ``None``) carry no wording
    and are dropped.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            # Not JSON: plain text, kept as it is.
            return [value]
        if isinstance(parsed, str):
            return [parsed]
        return _flatten_json_strings(parsed)
    if isinstance(value, dict):
        collected: List[str] = []
        for item in value.values():
            collected.extend(_flatten_json_strings(item))
        return collected
    if isinstance(value, list):
        collected = []
        for item in value:
            collected.extend(_flatten_json_strings(item))
        return collected
    return []


def _iter_intent_sentences(text: Any) -> Iterator[str]:
    """Yield the constraint sentences inside ``text``.

    A sentence is a stretch of text between two terminators — a
    full-width stop, bang, question mark or semicolon, or a line
    break. Only sentences carrying a constraint marker are yielded:
    what this extractor is for is handing the repair path the plan's
    *restrictions*, and a sentence that states no restriction is noise
    in that prompt.

    The marker is looked for in the whole sentence and the sentence is
    capped afterwards, so a long sentence is still recognised as a
    constraint instead of being judged on its first 300 characters
    alone.
    """
    if not isinstance(text, str):
        return
    for raw in _INTENT_SENTENCE_SPLIT.split(text):
        sentence = raw.strip()
        if not sentence:
            continue
        carries_marker = any(marker in sentence for marker in _INTENT_MARKERS)
        if not carries_marker and _INTENT_MARKER_ASCII not in sentence.lower():
            continue
        yield sentence[:_MAX_INTENT_SENTENCE_CHARS]


def _plan_intent(plan_dir: Path) -> List[str]:
    """Collect the constraint sentences the plan states about itself.

    Reads the plan's own material (see :data:`_PLAN_INTENT_FILES`) and
    returns the restriction sentences found in it, in the order they
    appear, each one at most once.

    Everything here is best-effort by contract: a member that is
    missing or unreadable is skipped, a member whose content is not
    parseable is read as plain text, and no failure of any kind is
    allowed to escape — a plan directory the extractor cannot read is a
    plan directory that contributes no constraints, not an error in the
    repair path.
    """
    collected: List[str] = []
    seen = set()
    try:
        directory = Path(plan_dir)
    except TypeError:
        return collected
    for name in _PLAN_INTENT_FILES:
        try:
            path = directory / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Missing or unreadable: this member simply says nothing.
            continue
        try:
            chunks = _flatten_json_strings(text)
        except (TypeError, ValueError):
            continue
        for chunk in chunks:
            for sentence in _iter_intent_sentences(chunk):
                if sentence in seen:
                    continue
                seen.add(sentence)
                collected.append(sentence)
                if len(collected) >= _MAX_INTENT_SENTENCES:
                    return collected
    return collected


# ---------------------------------------------------------------------------
# Deterministic banned-region fallback (R5)
#
# The model is asked to judge whether a proposed fix crosses one of the
# plan's own constraints (see :data:`REPAIR_CONTENT_SYSTEM_PROMPT` rule
# 7). A judgement is only as good as the model's reading of the
# constraint, and a model that answers ``violates=false`` for a fix
# that rewrites a whole Rust crate is indistinguishable, downstream, from
# a model that read the constraint correctly and found no conflict.
#
# So the constraint is re-read deterministically as well: the plan's own
# constraint sentences are mined for the regions they put off limits,
# and a file the repair wants to touch is checked against those regions
# by path segment. The judgement stays the primary signal; this is the
# floor under it.
# ---------------------------------------------------------------------------

#: Language names that put code off limits without naming a directory.
#: "不碰 Rust" is about the code, not one project's crate folder, so the
#: name is matched against what Rust code looks like on disk: a ``.rs``
#: file, a ``Cargo.toml`` / ``Cargo.lock`` manifest, or a crate directory
#: (a segment ending in ``_rs``). Hard-coding one project's crate name
#: would work for exactly one repository.
_LANGUAGE_REGIONS = frozenset({"rust"})

#: The tokens that can name a region: a backticked span, or a run of
#: path-ish characters. The class stops at CJK punctuation, so
#: "只改 Python（`src/`" yields the tokens "Python" and "src" — never
#: "Python（".
_BANNED_REGION_TOKEN_RE = re.compile(
    r"`(?P<ticked>[^`]+)`"
    r"|(?P<bare>[A-Za-z0-9][A-Za-z0-9._/-]*)"
)

#: Punctuation that closes a token in running text rather than belonging
#: to the name itself.
_REGION_TRAILING_PUNCT = ".,;:!?、，。；：！？)]}）】」』"

#: The characters that separate an identifier from the sentence around
#: it. A bare identifier (one carrying a "-" or a "_") names a
#: region; a plain word like "Python" names a language, not a path, and
#: is not a region.
_REGION_NAME_SEPARATORS = ("-", "_", "/")


def normalise_declared_files(raw: Any) -> List[str]:
    """Normalise a declared file scope into a list of relative paths.

    The same filter the repair task assembler has always applied to the
    LLM's ``files_to_modify`` hint, now a named function so the
    banned-region fallback can put candidate paths through exactly the
    same normalisation before matching them against the plan's
    constraints.

    Entries are stripped, backslashes are folded to forward slashes,
    and an entry is dropped when it is blank, absolute, or walks out of
    the project with ``..``. Duplicates collapse, and the first
    occurrence decides the order.

    A bare string is one entry, not a sequence of characters; anything
    that is not a list carries no scope at all.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    cleaned: List[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        path = entry.strip().replace("\\", "/")
        if not path or path.startswith("/") or ".." in path.split("/"):
            continue
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def _constraint_sentences(texts: Any) -> List[str]:
    """The constraint sentences to read, whatever shape they arrive in.

    A bare string is one sentence, not a sequence of characters; a
    shape that is not a sequence of sentences carries none.
    """
    if texts is None:
        return []
    if isinstance(texts, (str, bytes)):
        texts = [texts]
    if not isinstance(texts, (list, tuple)):
        return []
    sentences: List[str] = []
    for entry in texts:
        try:
            text = str(entry).strip()
        except Exception:  # noqa: BLE001 — a broken entry is not a failure
            continue
        if text:
            sentences.append(text)
    return sentences


def _trim_region_token(token: str) -> str:
    """Strip the punctuation a token carries from the sentence around it.

    "不要改 `api/` 目录" names "api", not "api/" and not "api/ 目录";
    "不碰 Rust." names "Rust", not "Rust.". The trailing slash goes
    too — it is part of how a directory is written, not part of its
    name.
    """
    cleaned = token.strip().strip("`\"'“”‘’")
    while cleaned:
        last = cleaned[-1]
        if last == "/" or last in _REGION_TRAILING_PUNCT:
            cleaned = cleaned[:-1].rstrip()
            continue
        break
    return cleaned


def _extract_banned_regions(texts: Any) -> List[str]:
    """The regions the plan's own constraints put off limits.

    Read off the constraint sentences themselves, so the check does not
    depend on the model having read the constraint correctly. A region
    is named in one of three ways, and all three are taken:

    * a backticked identifier — `` `src/` ``, `` `web-frontend` ``;
    * a token carrying a ``/`` — "不要改 src/ 下的任何文件";
    * a bare identifier carrying a ``-`` or a ``_`` —
      "web-frontend", "engine_rs".

    A plain word names no region: "只改 Python" restricts a language,
    not a path, and the path of a language is not a directory in the
    project. A language the matcher *does* recognise is the exception:
    it is kept as the language it names (see :data:`_LANGUAGE_REGIONS`)
    and matched against the shape of that language's code.

    A language name is kept as the language it names ("Rust" →
    ``rust``) so that a file path can actually hit it — a Rust file is
    Rust whatever the project calls the directory it lives in.

    The regions come back in the order they appear in the text, and a
    region named twice is kept once, at its first place.
    """
    regions: List[str] = []
    seen = set()
    for sentence in _constraint_sentences(texts):
        for match in _BANNED_REGION_TOKEN_RE.finditer(sentence):
            ticked = match.group("ticked")
            if ticked is not None:
                # A backticked span names a region whatever it looks
                # like — but only up to its first blank, so that
                # "`api/ 目录`" names "api" and not "api/ 目录".
                head = ticked.split()
                if not head:
                    continue
                candidate = _trim_region_token(head[0])
            else:
                bare = match.group("bare")
                candidate = _trim_region_token(bare)
                if candidate.lower() in _LANGUAGE_REGIONS:
                    # A language the matcher recognises: keep the
                    # language itself, not a directory that stands in
                    # for it.
                    candidate = candidate.lower()
                elif not any(
                    sep in bare for sep in _REGION_NAME_SEPARATORS
                ):
                    # A plain word — a language, a noun, a word. Not a
                    # region.
                    continue
            if not candidate:
                continue
            if candidate not in seen:
                seen.add(candidate)
                regions.append(candidate)
    return regions


def _path_hits_language_region(path: str, language: str) -> bool:
    """Whether ``path`` is code in ``language``.

    Only ``rust`` is recognised today: a ``.rs`` file, a Cargo manifest,
    or a crate directory (a path segment ending in ``_rs``). The check
    is on the path alone — the plan's constraint is about the code, and
    the code is identified by its own shape.
    """
    segments = [
        segment for segment in str(path or "").replace("\\", "/").split("/")
        if segment
    ]
    if not segments:
        return False
    if language == "rust":
        name = segments[-1]
        return (
            name.endswith(".rs")
            or name in ("Cargo.toml", "Cargo.lock")
            or any(segment.endswith("_rs") for segment in segments)
        )
    return False


def _path_hits_region(path: str, region: str) -> bool:
    """Whether ``path`` falls inside ``region``.

    A region that names a *language* ("rust") is matched by the shape
    of that language's code rather than by path segment — the plan says
    "不碰 Rust", and Rust code is Rust wherever it lives. Every other
    region is matched by path segment: both are read as
    ``/``-separated segments, and the region hits when its segments
    appear *consecutively* in the path — so a file below a region hits
    it ("engine_rs/src/signals.rs" hits "engine_rs"), while a region
    that only shares a word with the path does not.
    """
    if str(region or "").strip().lower() in _LANGUAGE_REGIONS:
        return _path_hits_language_region(path, str(region).strip().lower())
    path_segments = [
        segment for segment in str(path or "").replace("\\", "/").split("/")
        if segment
    ]
    region_segments = [
        segment for segment in str(region or "").replace("\\", "/").split("/")
        if segment
    ]
    if not path_segments or not region_segments:
        return False
    span = len(region_segments)
    for start in range(len(path_segments) - span + 1):
        if path_segments[start:start + span] == region_segments:
            return True
    return False


def _constraint_for_region(region: str, texts: Any) -> str:
    """The constraint sentence that puts ``region`` off limits.

    A sentence names a region either by writing the name out ("不要改
    src 目录") or by naming it the way the plan does ("不碰 Rust" names
    the language ``rust``). Both are recognised, and the
    sentence is reported verbatim so the caller can quote the plan
    itself rather than a paraphrase.
    """
    name = str(region or "").strip()
    if not name:
        return ""
    for sentence in _constraint_sentences(texts):
        if name in sentence:
            return sentence
        if name in _extract_banned_regions([sentence]):
            return sentence
    return ""


# ---------------------------------------------------------------------------
# The intent gate (R4 + R5)
#
# Two signals now say whether a proposed repair crosses one of the
# plan's own hard constraints: the model's structured judgement
# (``intent_check.violates``) and the deterministic path check above.
# This is where they become one gate — a row either signal rejects does
# not become a repair task (R4), and the rejection is recorded as a
# conflict rather than raised as an error (R5): a plan that names a
# constraint the repair cannot respect has to be *reported* to the
# operator, not crashed on.
# ---------------------------------------------------------------------------

#: The reason recorded when the model itself reported the conflict and
#: did not say why.
_MODEL_VIOLATION_REASON = "模型判断违背计划约束"

#: The reason recorded when the model said the fix was fine and the
#: path check said it was not. The wording names the disagreement,
#: because that disagreement is the thing an operator has to look at.
_DETERMINISTIC_MISMATCH_REASON = "模型判断与确定性检查不一致"


def _first_banned_region_hit(raw_files: Any, regions: Sequence[str]) -> str:
    """The first banned region one of the declared files falls into.

    The regions are read in the order the plan's constraints name them,
    so the region reported is the plan's own first word about what is
    off limits — not merely the first path that happens to hit
    something.

    The declared paths are *normalised* before matching (the same
    filter the repair task assembler applies), but they are never
    rewritten: this function only reads them (R6).
    """
    if not regions:
        return ""
    paths = normalise_declared_files(raw_files)
    if not paths:
        return ""
    for region in regions:
        for path in paths:
            if _path_hits_region(path, region):
                return region
    return ""


def apply_intent_gate(
    contents: List[Dict[str, Any]],
    plan_intent: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Filter the repair rows that cross the plan's own constraints.

    Returns ``(kept, conflicts)``:

    * ``kept`` — the rows that may become repair tasks, in their
      original order and *unmodified*;
    * ``conflicts`` — one record per rejected row, each a dict with
      ``vp_id`` / ``constraint`` / ``reason``.

    A row is rejected when either signal says so:

    * the model judged it a violation (``intent_check.violates``) —
      the model is the primary signal, and its own answer is taken at
      face value;
    * a file it declared falls into a region the plan's constraints
      put off limits — the deterministic floor under the model's
      judgement (R5). A model that answers ``violates=false`` for a
      fix that rewrites a whole Rust crate is indistinguishable from a
      model that read the constraint correctly, so the constraint is
      re-read deterministically as well.

    Nothing here raises. A conflict is a fact about the plan, not an
    error in the repair path: the row is dropped, the conflict is
    recorded, and the caller decides what to do about it. A plan with
    no constraints at all keeps every row the model did not reject —
    an empty ``plan_intent`` means there is no region to hit, not that
    every path is forbidden.

    The gate never rewrites or deletes the paths the model declared
    (R6): a rejected row is dropped whole, and a kept row carries
    exactly the scope the model gave it. The paths are normalised only
    for the matching, so a declared path that is absolute or walks out
    of the project cannot be used to slip past the check.

    Every deterministic conflict is printed as a WARNING as well as
    recorded: the model said this fix was fine and the check said it
    was not, and that disagreement has to be visible in the log, not
    only in the returned record.
    """
    if not isinstance(contents, (list, tuple)):
        return [], []

    regions = _extract_banned_regions(plan_intent)
    sentences = _constraint_sentences(plan_intent)
    fallback_constraint = sentences[0] if sentences else ""

    kept: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    for content in contents:
        if not isinstance(content, dict):
            # A row the gate cannot read is not a row it can judge —
            # and it is not a repair task either. The parser upstream
            # is what guarantees the shape; here it is simply not
            # something the gate has an opinion about.
            continue

        vp_id = str(content.get("failed_vp_id") or "")
        check = _normalise_intent_check(content.get("intent_check"))

        if check["violates"]:
            conflicts.append({
                "vp_id": vp_id,
                "constraint": check["constraint"] or fallback_constraint,
                "reason": check["reason"] or _MODEL_VIOLATION_REASON,
            })
            continue

        region = _first_banned_region_hit(
            content.get("files_to_modify"), regions,
        )
        if region:
            reason = (
                f"{_DETERMINISTIC_MISMATCH_REASON}："
                f"files_to_modify 命中禁改区域 {region}"
            )
            conflicts.append({
                "vp_id": vp_id,
                "constraint": _constraint_for_region(region, plan_intent)
                or region,
                "reason": reason,
            })
            print(
                f"[Repair] WARNING: {vp_id or '(未知 VP)'} 被意图门禁拦下 — "
                f"{reason}"
            )
            continue

        kept.append(content)

    return kept, conflicts


# Attach the mixin method to RepairTaskGenerator without breaking
# the rest of the class. Done as a module-level assignment so
# existing tests that introspect ``RepairTaskGenerator.__dict__``
# see both the original methods and ``generate_repair_contents``.
RepairTaskGenerator.generate_repair_contents = (
    _RepairContentGeneratorMixin.generate_repair_contents
)
