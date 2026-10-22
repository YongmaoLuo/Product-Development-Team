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
from typing import Optional, List, Dict, Any, Tuple

from coding_tool import CodingTool, JsonSession, query_json_in_session
# ``stable_session_id`` is re-exported from ``plan_sessions`` rather than
# defined here: the id belongs to the session, and the task-execution
# path needs it without importing this module's verification stack. The
# name stays importable from here for the callers that already use it.
from plan_sessions import stable_session_id
from test_command_quality import check_falsifiable, inspect_command
from verification_cancel import VerificationCancelled
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
      "execution_group_hint": 0
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

    def __init__(
        self,
        round_number: int,
        failed_vps: List[Dict[str, Any]],
        vp_test_commands: Optional[Dict[str, str]] = None,
        plan_path: Optional[str] = None,
        seq_base: int = 1,
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
        #   ``unverifiable_no_test_command``   → ``repair_task_unverifiable_no_test_command``
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
            tasks.append({
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
            })
        return tasks

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
        filtered: relative, no ``..`` traversal, no absolute paths, no
        empty strings. An empty list means "scope unknown" — the executor
        then gives the subagent no file hint and lets it discover the
        paths, which is strictly better than a scope pointing at the
        wrong file.
        """
        raw = content.get("files_to_modify")
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


def _build_repair_contents_prompt(
    failed_vps: List[Dict[str, Any]],
    round_number: int,
    plan_dir: Path,
    project_dir: Path,
    previous_failure_feedback: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    repair_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
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
    """
    parts = [
        f"Round {round_number}: generate one repair task per FAILED verification point.",
        "",
        f"Plan directory: {plan_dir}",
        f"Project directory: {project_dir}",
        "",
    ]
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
    ) -> List[Dict[str, Any]]:
        """Single LLM call producing repair content for each failed VP.

        Args:
            failed_vps: The list returned by
                :func:`verification.verification_report_reader.extract_failed_vps_from_report`.
                Each item carries ``id``, ``title``, ``priority``,
                ``actual_result``, ``evidence``, and optionally
                ``test_command`` / ``expected_result``.
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

            Returns an empty list only when ``failed_vps`` is empty —
            i.e. there is genuinely nothing to repair.

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
        failed_vp_ids = [
            str(vp.get("id")) for vp in failed_vps if vp.get("id")
        ]

        prompt = _build_repair_contents_prompt(
            failed_vps,
            round_number,
            self.plan_dir,
            self.project_dir,
            previous_failure_feedback=previous_failure_feedback,
            repair_outcomes=repair_outcomes,
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
        # Path A: a verification point that is not a shell command has no
        # command to copy, so the content prompt — which never asks for
        # one — cannot produce one either. Re-ask for it explicitly
        # before the assembler has to fall back to a label.
        self._fill_missing_test_commands(
            contents, failed_vps, round_number,
        )
        return contents


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
        })
    return parsed


# Attach the mixin method to RepairTaskGenerator without breaking
# the rest of the class. Done as a module-level assignment so
# existing tests that introspect ``RepairTaskGenerator.__dict__``
# see both the original methods and ``generate_repair_contents``.
RepairTaskGenerator.generate_repair_contents = (
    _RepairContentGeneratorMixin.generate_repair_contents
)
