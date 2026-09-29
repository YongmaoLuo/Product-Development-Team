"""
Task Refiner
============

Analyzes task execution results and refines the task list.
"""

import json
from typing import List, Dict, Optional

from coding_tool import CodingTool
from config import AgentConfig
from config_registry import ConfigRegistry
from task import SubTask
from framework.task_output_validator import TaskOutputValidator
from framework import watchdog_signal


# Maximum number of refinement rounds (initial LLM call + secondary
# corrections).  When a candidate task list is still invalid after
# auto-fix and this many rounds, the refiner raises RefinerExhausted
# and writes a watchdog signal.
MAX_REFINER_ROUNDS = 3


# 2026-09-17 (C3): how many extra attempts a round gets when the model's
# reply is not parseable JSON.
#
# On an earlier plan the refiner answered a large prompt with prose the
# parser could not find any JSON in ("no JSON object / array boundaries
# found"). ``Refiner.refine`` has no retry of its own on that path — the
# exception fell through to the generic handler, the task list came back
# unchanged, and the caller logged ``refine_no_change`` as if the refiner
# had deliberately decided to split nothing. The dispatcher then found no
# schedulable task and stopped the whole execution while four healthy
# tasks were still pending.
#
# A parse failure is a formatting accident, not a decision. Re-asking with
# an explicit "JSON only" instruction costs one cheap call and keeps the
# plan alive.
MAX_REFINER_JSON_RETRIES = 2

#: Appended to the prompt when re-asking after an unparseable reply.
_JSON_ONLY_REMINDER = (
    "\n\nYour previous reply could not be parsed as JSON. "
    "Respond with a SINGLE JSON object containing a 'tasks' key and "
    "nothing else — no prose, no explanation, no markdown code fences."
)


class RefinerExhausted(Exception):
    """Raised when the refiner cannot produce a valid task list."""

    def __init__(self, reason: str, detail: Optional[dict] = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class TaskRefiner:
    """Refines task list based on execution results."""

    def __init__(
        self,
        coding_tool: CodingTool,
        config: Optional[AgentConfig] = None,
        logger=None,
        project_dir: Optional[str] = None,
    ):
        self.coding_tool = coding_tool
        self.config = config or ConfigRegistry.get('coding')
        self.logger = logger
        self.project_dir = project_dir

    def refine(
        self,
        requirement: str,
        tasks: List[Dict],
        last_coder_response: str,
        last_result: str,
        exit_code: int,
        last_task_id: Optional[str] = None,
        file_context: str = "",
        project_dir: Optional[str] = None,
        plan_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        Refine task list based on last execution result.

        Args:
            requirement: Original project requirement
            tasks: Current list of tasks
            last_coder_response: Last AI implementation attempt
            last_result: Test execution result
            exit_code: Exit code from test command
            last_task_id: ID of the last executed task
            file_context: Reserved compatibility parameter. The refiner
                does not receive a repository-wide file snapshot; agents
                explore the codebase on demand with Read/Grep/Glob.
            project_dir: Project root used to validate task outputs.
                If omitted, the constructor's ``project_dir`` is used.
            plan_id: Plan identifier used to write the watchdog signal
                when the refiner exhausts its correction budget.
        """
        base_context = self._build_refiner_context(
            requirement=requirement,
            tasks=tasks,
            last_coder_response=last_coder_response,
            last_result=last_result,
            exit_code=exit_code,
            last_task_id=last_task_id,
            file_context=file_context,
        )

        # Get the refiner prompt from config
        refiner_prompt = self.config.refiner_system_prompt

        # Add domain knowledge if available
        if self.config.domain_knowledge:
            refiner_prompt = f"{self.config.domain_knowledge}\n\n{refiner_prompt}"

        effective_project_dir = project_dir or self.project_dir
        validator = None
        if effective_project_dir:
            validator = TaskOutputValidator(effective_project_dir)

        current_context = base_context
        final_report = None

        try:
            for round_idx in range(MAX_REFINER_ROUNDS):
                if self.logger:
                    self.logger.debug(
                        "refiner_query_started",
                        f"Querying LLM for task refinement (last_task={last_task_id}, round={round_idx + 1})",
                        task_id=last_task_id,
                        data={"round": round_idx + 1},
                    )
                # 2026-09-17 (C3): tolerate an unparseable reply by
                # re-asking before treating the round as a no-op.
                response = None
                json_error: Optional[json.JSONDecodeError] = None
                round_context = current_context
                for json_retry in range(MAX_REFINER_JSON_RETRIES + 1):
                    try:
                        response = self.coding_tool.query_json(
                            round_context, system_instruction=refiner_prompt,
                            scene="refiner",
                        )
                        break
                    except json.JSONDecodeError as exc:
                        json_error = exc
                        # Only announce a re-ask that will actually happen;
                        # the final failure is reported by the caller's
                        # ``refine_exception`` path.
                        if (
                            json_retry < MAX_REFINER_JSON_RETRIES
                            and self.logger
                        ):
                            self.logger.warning(
                                "refiner_json_retry",
                                f"Refiner reply for task [{last_task_id}] was not "
                                f"parseable JSON "
                                f"(retry {json_retry + 1}/{MAX_REFINER_JSON_RETRIES}); "
                                f"re-asking for JSON only",
                                task_id=last_task_id,
                                data={
                                    "round": round_idx + 1,
                                    "json_retry": json_retry + 1,
                                    "max_json_retries": MAX_REFINER_JSON_RETRIES,
                                    "error": str(exc)[:300],
                                },
                            )
                        round_context = current_context + _JSON_ONLY_REMINDER
                if response is None:
                    # Budget exhausted — surface the parse error so the
                    # caller's existing failure path handles it.
                    raise json_error or json.JSONDecodeError(
                        "no JSON object / array boundaries found", "", 0
                    )

                new_tasks = response.get("tasks", tasks)
                if self.logger:
                    self.logger.debug(
                        "refiner_query_completed",
                        f"Refiner returned {len(new_tasks)} tasks",
                        task_id=last_task_id,
                        data={"new_task_count": len(new_tasks), "round": round_idx + 1},
                    )

                new_tasks = self._finalize_candidate(tasks, new_tasks, last_task_id)

                if validator is None:
                    return new_tasks

                snapshot = [SubTask(**t) for t in new_tasks]
                fixed_snapshot = validator.auto_fix(snapshot)
                final_report = validator.validate(fixed_snapshot)

                if final_report.status == "passed":
                    return [t.model_dump() for t in fixed_snapshot]

                # Secondary correction: feed validation errors back to the LLM.
                error_lines = "\n".join(final_report.reasons)
                current_context = (
                    base_context
                    + f"\n\nPrevious attempt {round_idx + 1} produced an invalid task list. "
                    + "Fix these validation errors and return a corrected JSON object with a 'tasks' key:\n"
                    + error_lines
                )

            # Exhausted all rounds without producing a valid task list.
            detail = {
                "rounds": MAX_REFINER_ROUNDS,
                "reasons": list(final_report.reasons) if final_report else [],
                "failed_steps": list(final_report.failed_steps) if final_report else [],
                "failed_task_ids": list(final_report.failed_task_ids) if final_report else [],
            }
            self._write_exhausted_signal(plan_id, detail)
            raise RefinerExhausted(
                f"Refiner exhausted {MAX_REFINER_ROUNDS} rounds without producing a valid task list",
                detail=detail,
            )
        except RefinerExhausted:
            raise
        except Exception as e:
            # 2026-09-17 (C3): this used to be print-only, so a refiner
            # that failed outright (e.g. an unparseable reply after every
            # retry) looked exactly like a refiner that deliberately
            # returned the list unchanged — the caller logged
            # ``refine_no_change`` and the plan stalled with no error
            # event anywhere in execution.log. Log it as an error so the
            # two outcomes are distinguishable.
            print(f"Error during refinement: {e}")
            if self.logger:
                try:
                    self.logger.error(
                        "refine_exception",
                        f"Refiner failed for task [{last_task_id}]; "
                        f"returning the task list unchanged: {e}",
                        task_id=last_task_id,
                        data={
                            "error": str(e)[:500],
                            "error_type": type(e).__name__,
                            "unchanged": True,
                        },
                    )
                except Exception:
                    pass
            return tasks

    def _build_refiner_context(
        self,
        requirement: str,
        tasks: List[Dict],
        last_coder_response: str,
        last_result: str,
        exit_code: int,
        last_task_id: Optional[str],
        file_context: str,
    ) -> str:
        """Build the base prompt context sent to the refiner LLM.

        No-bulk-context contract (a 2026-08-19 production plan):
          * The ``file_context`` argument is **ignored**. The
            refiner is a planner; it must NEVER receive a
            repository-wide code snapshot. The argument is kept
            on the public ``refine()`` signature only for
            backward-compatibility with callers that still pass
            a value (we accept it silently and drop it).
          * The prompt is sized like a planning request:
            requirement + failed task id + last implementation
            attempt + last test result + current task list. No
            codebase snapshot, no ``Current codebase (truncated)``
            section.
        """
        refine_context = f"Requirement: {requirement}\n"
        refine_context += f"Last Task ID: {last_task_id or 'N/A'}\n"
        refine_context += f"Last Task Implementation Attempt:\n{last_coder_response}\n"
        refine_context += f"Last Task Result: {last_result}\n"
        refine_context += f"Current Tasks: {json.dumps(tasks, indent=2)}\n"
        # file_context intentionally NOT included.
        refine_context += "Use Read/Grep/Glob tools to explore the codebase if needed.\n"
        refine_context += "Do NOT inject full file contents into your response."

        if exit_code != 0:
            refine_context += "\n\nCRITICAL INSTRUCTION: The last task FAILED. You must NOT leave the task list as is. You MUST break down the failed task into smaller, simpler subtasks to resolve the error. Do not just retry the same task."
            if last_task_id:
                refine_context += f"\n\nThe failed task ID is '{last_task_id}'. When breaking it down into subtasks, use hierarchical IDs like '{last_task_id}-1', '{last_task_id}-2', etc."

        return refine_context

    def _finalize_candidate(
        self,
        old_tasks: List[Dict],
        new_tasks: List[Dict],
        last_task_id: Optional[str],
    ) -> List[Dict]:
        """Preserve metadata from old tasks and apply structural mutations."""
        existing_tasks_map = {task.get("id"): task for task in old_tasks}
        for new_task in new_tasks:
            task_id = new_task.get("id")
            if task_id in existing_tasks_map:
                existing_task = existing_tasks_map[task_id]
                # Preserve updated_time
                if "updated_time" in existing_task and "updated_time" not in new_task:
                    new_task["updated_time"] = existing_task["updated_time"]
                # Preserve completed status
                if existing_task.get("status") == "completed":
                    new_task["status"] = "completed"

        # depends_on rewrite (PRD DP-7 hardening, audit 2026-07-16):
        #
        # When the LLM splits a parent task into children
        # (``"2"`` → ``["2-1", "2-2", "2-3", "2-4"]``), any other
        # task that previously depended on the old parent still
        # has ``depends_on: ["2"]``. Without rewrite, downstream
        # tasks block forever on a parent that no longer exists
        # (or has been marked ``breakdown_in_progress``); the
        # scheduler treats this as a DAG validation error
        # ("Task 3 depends on missing task 2"); and the executor
        # enters the ``same_id_loop_detected`` infinite loop that
        # produced 6690+ identical events on 2026-07-16 before
        # the Telegram daemon saw stale state.
        return self.mutate_structure(new_tasks, task_id=last_task_id)

    def _write_exhausted_signal(
        self,
        plan_id: Optional[str],
        detail: dict,
    ) -> None:
        """Write a watchdog signal when the refiner exhausts its budget."""
        if not plan_id:
            return
        try:
            watchdog_signal.write_signal(
                plan_id=plan_id,
                source="refiner",
                reason="Refiner exhausted all correction rounds without producing a valid task list",
                detail=detail,
            )
        except Exception:
            # Signal writing is best-effort; the exception that matters is
            # RefinerExhausted, which is raised regardless.
            pass

    def mutate_structure(
        self,
        tasks: List[Dict],
        task_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        Apply structural mutations to a task list.

        This is the single gateway for refiner-side structural changes
        (currently the ``depends_on`` rewrite after a parent task is
        split into children). All structural mutations must flow through
        this method; direct writes to ``tasks.json`` are forbidden here
        because persistence is the caller's responsibility.
        """
        if not tasks:
            return tasks

        existing_tasks_map = {task.get("id"): task for task in tasks}
        rewrites = TaskRefiner._rewrite_split_depends_on(
            tasks, existing_tasks_map, logger=self.logger,
        )
        if rewrites and self.logger:
            try:
                self.logger.info(
                    "depends_on_fixup",
                    f"Refiner split parents; rewrote {rewrites} "
                    "stale depends_on references in surviving tasks",
                    task_id=task_id,
                    data={
                        "rewrites": rewrites,
                        "new_task_count": len(tasks),
                    },
                )
            except Exception:
                pass

        # Description consistency rewrite:
        # when the depends_on rewrite above fires it also rewrites
        # ``depends_on`` entries on downstream tasks but leaves the
        # corresponding ``description`` field untouched. The
        # framework's ``validate_desc_consistency`` then rejects the
        # plan with "Task 2-1 desc 提到前置条件 1 但 depends_on 是
        # ['1-1', '1-2', '1-3', '1-4']" — the description still
        # names the now-removed parent id. Rewriting the
        # description in lockstep with the depends_on rewrite closes
        # the same-id-loop source path entirely.
        desc_rewrites = TaskRefiner._rewrite_split_descriptions(
            tasks, existing_tasks_map, logger=self.logger,
        )
        if desc_rewrites and self.logger:
            try:
                self.logger.info(
                    "description_fixup",
                    f"Refiner split parents; rewrote {desc_rewrites} "
                    "stale parent-id references in task descriptions",
                    task_id=task_id,
                    data={
                        "rewrites": desc_rewrites,
                        "new_task_count": len(tasks),
                    },
                )
            except Exception:
                pass
        return tasks

    @staticmethod
    def _rewrite_split_descriptions(
        new_tasks,
        existing_tasks_map,
        logger=None,
    ):
        """Rewrite stale parent-id references in the description field.

        Mirrors ``_rewrite_split_depends_on`` but operates on
        ``description``: when a refiner split replaces a parent task
        with hierarchical children (e.g. ``"1"`` → ``"1-1", "1-2", ...``),
        any description that mentions the removed parent id would
        later be rejected by ``validate_desc_consistency`` ("Task
        2-1 desc 提到前置条件 1 但 depends_on 是 ['1-1', ...]").
        We rewrite those references so the description matches the
        newly-extended depends_on list.

        Token replacement is intentionally narrow: we only rewrite a
        parent-id token when it appears in a context that names the
        task as a dependency (e.g. "前置条件：1", "depends on
        task 1", "after 1 完成"). Bare numeric tokens that happen to
        match a parent id are left alone (they could mean anything).

        Args:
            new_tasks: The list returned by the LLM, mutated in
                place. Each entry is a task dict.
            existing_tasks_map: Same map passed to
                ``_rewrite_split_depends_on``. Currently unused
                (the rewrite only needs the new list to identify
                children), kept for symmetry with the deps
                rewrite.
            logger: Optional execution logger for diagnostics.

        Returns:
            ``int`` — total rewritten references across all
            descriptions. ``0`` if no rewrite was needed.
        """
        _ = existing_tasks_map  # symmetry with depends_on rewrite
        if not new_tasks:
            return 0

        # Build parent -> children map by hierarchical-id matching.
        # Same rule as the depends_on rewrite: parent = id with the
        # trailing "-<digit>" segment stripped.
        children_by_parent: Dict[str, List[str]] = {}
        for nt in new_tasks:
            nt_id = nt.get("id") or ""
            if not isinstance(nt_id, str) or "-" not in nt_id:
                continue
            parent_id, _, suffix = nt_id.rpartition("-")
            if not suffix.isdigit() or not parent_id:
                continue
            children_by_parent.setdefault(parent_id, []).append(nt_id)

        if not children_by_parent:
            return 0

        # Patterns we rewrite. Each entry is a compiled regex and
        # a replacement callable. Keep this list narrow on purpose:
        # rewriting arbitrary numbers risks clobbering unrelated
        # tokens. The patterns below match concrete phrasings the
        # planner emits today.
        #
        # 1. 前置条件：N (with optional trailing 已完成 / punctuation)
        # 2. depends on task N
        # 3. depends on N
        # 4. after N 完成
        import re
        def _replace(parent_children, m):
            token = m.group(2)
            if token in parent_children:
                # Comma-separated list of children replaces the
                # single parent id, matching the depends_on rewrite
                # convention.
                return m.group(1) + ", ".join(parent_children[token])
            return m.group(0)

        patterns = [
            re.compile(
                r"(前置条件[：:]\s*)([^\s\n,，。；;;]+)"
            ),
            re.compile(
                r"((?:depends on|depends_on|after)\s+(?:task\s+)?)([^\s\n,，。；;;]+)"
            ),
        ]

        rewrites = 0
        for nt in new_tasks:
            desc = nt.get("description")
            if not isinstance(desc, str) or not desc:
                continue
            new_desc = desc
            for pattern in patterns:
                new_desc = pattern.sub(
                    lambda m, _pc=children_by_parent: _replace(_pc, m),
                    new_desc,
                )
            if new_desc != desc:
                nt["description"] = new_desc
                rewrites += 1
        return rewrites

    @staticmethod
    def _rewrite_split_depends_on(
        new_tasks,
        existing_tasks_map,
        logger=None,
    ):
        """Rewrite stale ``depends_on`` references after a refiner split.

        In-place: mutates ``new_tasks[i].depends_on`` entries. For
        every surviving task whose ``depends_on`` references a task
        id that has been **replaced by hierarchical children**
        (``"{parent_id}-<digit>"``), the dep is rewritten to point at
        **all** of the parent's children (preserving the original
        AND partial-ordering semantic — downstream was waiting for
        the parent to fully complete, so it now waits for the full
        set of children to reach terminal state).

        Two flavours of "stale parent" are handled:
          1. **Removed parent** — the parent id was in the
             ``existing_tasks_map`` but is not in ``new_tasks``.
          2. **Never-existed parent** — the parent id is not in
             ``new_tasks`` *or* in ``existing_tasks_map`` (a pure
             stale ref). Example: a ``tasks.json`` written by an
             older version of the refiner that split ``"2"`` but
             never persisted the rewrite, and was committed
             directly to git. The downstream task ``"3"`` still
             points at ``"2"`` even though the file's task set
             only contains ``"2-1"``, ``"2-2"``, ...

        Both flavours produce the same rewrite outcome. We
        intentionally do **not** restrict to ``removed_ids`` (case
        1) so case 2 — the audit 2026-07-16 production failure —
        is also covered.

        Safe for tasks that don't reference any split parent: no
        change. Safe for parents that were removed without
        producing children (LLM dropped the task entirely rather
        than split): silently leaves the dangling ref to surface
        in the validator as a real error.

        Returns the number of dependency references rewritten, so
        callers / tests can assert on the no-op vs rewrite path.

        Args:
            new_tasks: The list returned by the LLM, mutated in
                place. Each entry is a task dict.
            existing_tasks_map: Pre-refiner ``{task_id: task_dict}``
                from ``tasks.json`` at refine time. The rewrite
                itself uses ``new_tasks`` as the live source of
                truth (case 2 above isn't visible in the old map).
            logger: Optional execution logger for diagnostics.

        Returns:
            ``int`` — total rewritten references across all
            surviving tasks. ``0`` if no rewrite was needed.
        """
        if not new_tasks:
            return 0
        # Build parent → children map by hierarchical-id matching.
        # Invert: walk each child id and extract the parent by
        # stripping a trailing ``-<digit>`` segment. The parent
        # id does NOT need to be present in the new list — by
        # definition the parent was split away and replaced by the
        # children. We also don't require the parent to be in
        # ``removed_ids`` (the original implementation did) because
        # a pure stale ref — a parent id that was never in the
        # file at all — also needs the rewrite to fire (audit
        # 2026-07-16 production failure).
        children_by_parent: Dict[str, List[str]] = {}
        for nt in new_tasks:
            nt_id = nt.get("id") or ""
            if not isinstance(nt_id, str) or "-" not in nt_id:
                continue
            parent_id, _, suffix = nt_id.rpartition("-")
            if not suffix.isdigit() or not parent_id:
                continue
            children_by_parent.setdefault(parent_id, []).append(nt_id)
        if not children_by_parent:
            return 0
        # Build a transitive ``descendants_by_id`` map. Whereas
        # ``children_by_parent`` only links a parent to its DIRECT
        # children, ``descendants_by_id`` walks up the full
        # hierarchical chain and registers every ancestor as a key
        # pointing at the leaf id. This handles the
        # ``4 -> 4-2 -> 4-2-1/4-2-2/4-2-3`` case where the
        # intermediate parent (``4-2``) is itself absent from the
        # task list but a task still depends on the grandparent
        # (``4``).
        #
        # Example: tasks ``[4-2-1, 4-2-2, 4-2-3]`` produce
        #   descendants_by_id = {
        #     "4-2":   ["4-2-1", "4-2-2", "4-2-3"],   # direct parent
        #     "4":     ["4-2-1", "4-2-2", "4-2-3"],   # grand-parent
        #   }
        # (Note: we do NOT register ``nt_id`` under itself — a task
        # should never depend on itself, so the rewriter must not
        # emit a self-loop.)
        descendants_by_id = {}
        for nt in new_tasks:
            nt_id = nt.get("id") or ""
            if not isinstance(nt_id, str):
                continue
            cur = nt_id
            # Walk up the ``-<digit>`` chain and register ``nt_id``
            # as a descendant of every ancestor. Skip the leaf
            # itself — see comment above.
            while "-" in cur:
                parent_id, _, suffix = cur.rpartition("-")
                if not suffix.isdigit() or not parent_id:
                    break
                descendants_by_id.setdefault(parent_id, []).append(nt_id)
                cur = parent_id
        # Rewrite stale refs in every surviving task's deps.
        rewrites = 0
        for nt in new_tasks:
            deps = nt.get("depends_on")
            if not isinstance(deps, list) or not deps:
                continue
            # Tasks must never depend on themselves — filter out the
            # current task's own id from any rewritten dep list.
            # Otherwise a transitive rewrite (e.g. "4" →
            # ["4-2-1", "4-2-2", "4-2-3"] applied to task 4-2-2)
            # would leave the task depending on itself, which the
            # validator rejects with "Task X cannot depend on itself".
            own_id = nt.get("id")
            new_deps = []
            touched = False
            for d in deps:
                if not isinstance(d, str):
                    new_deps.append(d)
                    continue
                # Prefer direct children match when available (smaller
                # replacement set = preserves the original partial-
                # ordering semantic). Fall back to transitive
                # descendants if no direct children exist (deeply-
                # split case, e.g. "4" -> "4-2-1/4-2-2/4-2-3").
                if d in children_by_parent:
                    replaced = children_by_parent[d]
                elif d in descendants_by_id:
                    replaced = descendants_by_id[d]
                else:
                    new_deps.append(d)
                    continue
                # Strip the current task's own id from the
                # replacement set (defensive — should not be present
                # in the descendants map because we walk ancestors
                # only, but the dedup pass below would surface it).
                replaced = [r for r in replaced if r != own_id]
                if not replaced:
                    # The rewrite collapsed entirely to self-refs;
                    # drop the stale dep entirely so we don't leave
                    # the task with no dependency where one used to
                    # exist. The validator will catch any other
                    # issues, but losing the dep is preferable to
                    # reintroducing a self-loop.
                    rewrites += 1
                    touched = True
                    continue
                new_deps.extend(replaced)
                rewrites += 1
                touched = True
            if touched:
                # De-duplicate while preserving order (LLMs sometimes
                # emit duplicates when asked to rewrite).
                seen = set()
                deduped = []
                for d in new_deps:
                    if d not in seen:
                        seen.add(d)
                        deduped.append(d)
                nt["depends_on"] = deduped
        return rewrites
