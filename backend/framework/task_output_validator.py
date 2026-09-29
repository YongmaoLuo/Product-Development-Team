"""Stateless task-output validator.

Architecture decision point 4 mandates that this validator hold
**no cross-call state**. Quota counters / per-call bounds that gate
``auto_fix`` invocations live in the dispatcher / refiner modules
(in-memory state), not here. The validator is therefore safe to
share across threads, processes, and call sites.

Public API
----------
* :class:`TaskOutputValidator` — entry point.
* :class:`ValidationReport` — return type of :meth:`validate`.
* :class:`TaskValidationError` — raised by ``auto_fix`` when its
  fix budget is exceeded (the validator itself has no internal
  budget — the caller is responsible for one — but it propagates
  any underlying repair failure as ``TaskValidationError`` for the
  caller's convenience).

Statelessness contract
----------------------
* :meth:`validate` and :meth:`auto_fix` accept a ``list[SubTask]``
  snapshot, take a deep-copy internally, and never mutate the
  caller's list (contract 3).
* Both methods return fresh objects on every call (no ``last_*``
  cache; contracts 1 + 2).
* Neither method writes to disk (contract 4) — disk-side effects
  are exclusively the caller's responsibility (via
  ``TaskRepository``).
* Cross-call order does not affect the final outcome (contract 5):
  the validator is a pure function of its input.

This module re-uses the **shape** of the existing
``tasks_generator.TaskOutputValidator`` (4-step pipeline, same
required fields, same sentinel handling) but it is a fresh, slim
implementation because the original was coupled to a single-task
``dict`` payload and held instance state (``_depends_on_fix_passes``)
that violates architecture decision point 4.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The same sentinel used by ``task.py``. Re-declared locally so this
# module is independent of any future change to ``task.py``'s value.
_LOCAL_UNKNOWN_SENTINEL = "__UNKNOWN_MODIFICATIONS__"


# ---------------------------------------------------------------------------
# To-be-created file paths
# ---------------------------------------------------------------------------
#
# A subtask may legitimately CREATE a file, and the path it will create
# cannot exist yet. Before 2026-09-14 the only two ways to satisfy the
# step-3 existence check were "the file already exists" and "git
# history deleted it", so a create-task had no expressible
# ``files_to_modify`` at all — the generator's only option was to
# mislabel it read-only (observed: task 40-1-3 "创建 5 个 APIRouter
# 子模块空骨架" carried the no-file-changes sentinel), which also drops
# its file-conflict key and lets it race with the tasks that really do
# touch those files.
#
# The parent-directory-must-exist condition was dropped on 2026-09-21.
# It was
# never a real hallucination guard — a typo'd FILE NAME inside a real
# directory passed it — while it hard-rejected the ordinary shape "this
# task creates the first file in a new subdirectory". That rejection
# cost a live run: a production plan declared
# ``native_ext/docs/audit-0f0f0f0f.md`` (and four sibling cases) whose
# parent directories exist in neither checkout, so the pre-run gate
# aborted the executor 0.4s after a successful ``/start``.
#
# Three conditions, all required:
#
#   * the RAW entry must name a path with a parent (``a/b.py``, not a
#     bare ``b.py``). A bare filename carries no structural evidence at
#     all — ``ghost.py`` and a genuine new ``ghost.py`` at the project
#     root are the same string — so the existence check keeps rejecting
#     it. That is exactly the case the repo's pinned contract
#     (``test_step_3_still_fails_for_never_existed_path``) guards. Note
#     this has to look at ``raw``, not at the resolved ``path``:
#     callers have already prefixed ``project_dir``, so ``path.parent``
#     is never the bare ``"."``.
#   * the path must resolve INSIDE ``project_dir``. A ``..`` traversal
#     or an absolute path pointing elsewhere still raises ValueError
#     and is still rejected.
#   * the FIRST path segment must be a real directory of the project
#     (``native_ext/``, ``tests/``, ``frontend-app/``). This is the
#     one surviving hallucination guard: it rejects a wholly invented
#     top-level tree (``no_such_dir/ghost.py``) without forbidding new
#     directories *below* a real one. Intermediate and leaf directories
#     may be created by the task — that is the whole point of this
#     predicate.
#
# Shared by the dispatcher-side validator in this module; the duplicate
# single-task copy in ``tasks_generator`` was deleted on the same date
# (it was never instantiated — see that module's history).
def is_to_be_created_path(raw: str, path: Path, project_dir) -> bool:
    """Is ``raw`` a plausible file the task will CREATE at ``path``?

    ``False`` (never raises) on any OSError so a transient filesystem
    failure cannot silently accept a bad path — the caller then raises
    its original "not found" error.
    """
    try:
        entry = Path(raw)
        if not entry.is_absolute() and entry.parent == Path("."):
            return False                # bare filename — no evidence
        root = Path(project_dir).resolve()
        # Out-of-root targets (``..`` traversal, absolute paths pointing
        # elsewhere) raise ValueError here and are rejected.
        rel = Path(path).resolve().relative_to(root)
        if len(rel.parts) < 2:
            return False                # resolves to the project root
        if not (root / rel.parts[0]).is_dir():
            return False                # invented top-level entry
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskValidationError(Exception):
    """Raised when validation cannot proceed.

    ``step`` is the 1-based step number that failed; ``reason`` is a
    human-readable message safe to log or surface in a
    ``TEST_RESULT: FAILED`` line.
    """

    def __init__(self, step: int, reason: str):
        super().__init__(f"[step {step}] {reason}")
        self.step = step
        self.reason = reason


# ---------------------------------------------------------------------------
# ValidationReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationReport:
    """Immutable result of :meth:`TaskOutputValidator.validate`.

    Fields
    ------
    status : str
        ``"passed"`` if every task in the snapshot is valid,
        ``"failed"`` otherwise.
    failed_steps : tuple[int, ...]
        The step numbers (1..4) that flagged failures. Empty when
        ``status == "passed"``.
    failed_task_ids : tuple[str, ...]
        The ``SubTask.id`` of each task that failed any step.
        Empty when ``status == "passed"``.
    reasons : tuple[str, ...]
        Human-readable failure reasons, one per failed step / task.
    tasks_snapshot : tuple[dict, ...]
        The (immutable) deep-copy of the validated snapshot. Stored
        only when ``status == "failed"`` so the caller can inspect
        the offending tasks without re-fetching them.
    """

    status: str
    failed_steps: Tuple[int, ...] = field(default_factory=tuple)
    failed_task_ids: Tuple[str, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    tasks_snapshot: Tuple[dict, ...] = field(default_factory=tuple)

    def model_dump(self) -> dict:
        """Return a JSON-safe dict suitable for equality comparison."""
        return {
            "status": self.status,
            "failed_steps": list(self.failed_steps),
            "failed_task_ids": list(self.failed_task_ids),
            "reasons": list(self.reasons),
            "tasks_snapshot": list(self.tasks_snapshot),
        }


# ---------------------------------------------------------------------------
# TaskOutputValidator
# ---------------------------------------------------------------------------


class TaskOutputValidator:
    """Stateless validator for a ``list[SubTask]`` snapshot.

    The validator is **stateless across calls**. It holds no
    counters, caches, or last-report fields; callers (dispatcher /
    refiner) are responsible for any auto_fix budget enforcement.

    Pipeline (4 serial steps; first failure short-circuits the rest
    of that task's checks but other tasks still get checked):

      1. ``json_schema_check`` — every required field is present and
         well-typed on every task.
      2. ``depends_on_consistency_check`` — ``depends_on`` is
         consistent with the ``description`` (auto-fixable: any
         description-referenced ``task-N`` token is appended to
         ``depends_on``).
      3. ``files_to_modify_existence_check`` — every entry in
         ``files_to_modify`` is one of: a real file under
         ``project_dir``; a path whose parent directory exists (a
         file the task will CREATE, accepted since 2026-09-14); a
         path deleted in git history; or a sentinel marker — the
         read-only marker is accepted, the unknown-modifications
         marker is rejected on purpose so the dispatcher's subagent
         fill loop fires.
      4. ``test_command_function_name_check`` — every pytest
         selector ``<file>::<func>`` points at a function that
         actually exists in that file (AST-verified).

    The constructor takes only ``project_dir``; per-call state is
    the snapshot argument to ``validate`` / ``auto_fix``.
    """

    def __init__(self, project_dir):
        """Initialise the validator.

        Parameters
        ----------
        project_dir : str | pathlib.Path
            Absolute path to the project root. ``files_to_modify``
            paths are resolved relative to this root.
        """
        self._project_dir = Path(project_dir)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def validate(self, snapshot: Sequence[SubTask]) -> ValidationReport:
        """Validate a snapshot, returning a ``ValidationReport``.

        The validator takes a **deep copy** of every task in the
        snapshot before checking; the caller's list is never
        mutated.

        Returns
        -------
        ValidationReport
            ``status="passed"`` when every task passes every step;
            otherwise ``status="failed"`` with the per-step /
            per-task failure detail.
        """
        # Deep copy so any auto-fixable side effects in
        # ``_depends_on_consistency_check`` cannot leak back to the
        # caller. The validator itself never auto-fixes in
        # ``validate`` — auto-fix lives in ``auto_fix`` — but
        # snapshot mutation is forbidden by contract 3 anyway.
        snapshot_copy = copy.deepcopy(list(snapshot))
        failed_steps: List[int] = []
        failed_task_ids: List[str] = []
        reasons: List[str] = []

        # Snapshot-wide id set, computed once so step 2 can resolve
        # description tokens against the real plan (prefix-aware,
        # matching the runtime resolver). Without this the validator
        # would inject phantom deps for tokens like ``task-1`` that
        # don't exist as literal ids — see an earlier plan postmortem.
        existing_task_ids: set = {t.id for t in snapshot_copy}

        for task in snapshot_copy:
            for step, checker in (
                (1, lambda t: self._json_schema_check(t)),
                (
                    2,
                    lambda t: self._depends_on_consistency_check(
                        t, existing_task_ids,
                    ),
                ),
                (3, lambda t: self._files_to_modify_existence_check(t)),
                (4, lambda t: self._test_command_function_name_check(t)),
            ):
                try:
                    checker(task)
                except TaskValidationError as exc:
                    # 2026-09-09: append ``step``
                    # unconditionally so ``failed_steps`` stays
                    # parallel to ``failed_task_ids`` and
                    # ``reasons``. The previous ``if step not in
                    # failed_steps`` dedup caused ``failed_steps``
                    # to be shorter than the other two lists,
                    # which broke downstream ``zip(...)`` iteration
                    # in ``_identify_healable_tasks`` (a snapshot with 48
                    # step-3 failures collapsed to a single ``[3]`` entry,
                    # so the fill loop saw 1 task and the dispatch gate
                    # raised RuntimeError).
                    # Consumers that only need "is step N present?"
                    # use membership test (``step in
                    # failed_steps``) which still works because
                    # ``step`` appears at least once per task.
                    failed_steps.append(step)
                    failed_task_ids.append(task.id)
                    reasons.append(f"{task.id} [step {exc.step}]: {exc.reason}")
                    # Short-circuit: don't run further steps on a
                    # task that already failed an earlier step.
                    break

        if not failed_steps:
            return ValidationReport(status="passed")
        return ValidationReport(
            status="failed",
            failed_steps=tuple(failed_steps),
            failed_task_ids=tuple(failed_task_ids),
            reasons=tuple(reasons),
            tasks_snapshot=tuple(t.model_dump() for t in snapshot_copy),
        )

    def auto_fix(self, snapshot: Sequence[SubTask]) -> List[SubTask]:
        """Return a fixed copy of ``snapshot``.

        Behaviour
        ---------
        * The caller's list is **not** mutated (contract 3). The
          returned list is a fresh ``list`` of fresh ``SubTask``
          objects.
        * Auto-fix currently covers step 2 (depends_on): any
          ``task-N`` token referenced in ``description`` but absent
          from ``depends_on`` is appended.
        * Steps 3 and 4 are **not** auto-fixable in this validator —
          they require external knowledge (creating files, defining
          functions). They are surfaced via ``validate``; if
          ``validate`` still fails after ``auto_fix`` the caller
          should refuse to write the snapshot to disk.
        * No disk writes (contract 4).
        """
        snapshot_copy = copy.deepcopy(list(snapshot))
        # Snapshot-wide id set so step-2 ``auto_fix`` can refuse to
        # inject phantom deps (description references whose token has
        # no matching task id and no children in the plan). Without
        # this guard, ``auto_fix`` silently buried a ``task-1`` token
        # into a plan whose ids are ``1-1``/``1-2``/``1-3`` and the
        # runtime ``is_dependency_ready`` gate then reported
        # ``missing_upstream:task-1`` forever, freezing the executor.
        existing_task_ids: set = {t.id for t in snapshot_copy}
        for task in snapshot_copy:
            self._auto_fix_depends_on(task, existing_task_ids)
        return snapshot_copy

    # ------------------------------------------------------------------
    # Step 1 — JSON schema
    # ------------------------------------------------------------------

    def _json_schema_check(self, task: SubTask) -> None:
        missing = []
        for key in ("id", "title", "description", "test_command",
                    "files_to_modify", "depends_on"):
            val = getattr(task, key, None)
            if val is None:
                missing.append(key)
        if missing:
            raise TaskValidationError(
                step=1,
                reason=f"missing required field(s): {sorted(missing)}",
            )

        # 2026-09-15: require a non-empty test command.
        #
        # ``SubTask.test_command`` is declared as ``str = ""`` in
        # ``task.py``, so the ``is None`` loop above can never fire for
        # it — a task that omits the command passes step 1 with an empty
        # string. Step 4 then no-ops on empty input (``if not cmd:
        # return``), so nothing downstream notices either, and at
        # dispatch time the completion gate's command-line signal has no
        # command to run. A task that omits it therefore validates
        # anyway, silently disabling half of the dual-signal completion
        # rule.
        #
        # Either form is accepted; ``test_commands`` (list) is the
        # preferred multi-command shape.
        commands = [c for c in (task.test_commands or []) if str(c).strip()]
        if (task.test_command or "").strip():
            commands.append(task.test_command)
        if not commands:
            raise TaskValidationError(
                step=1,
                reason=(
                    "test_command/test_commands is empty — every task must "
                    "declare at least one non-empty test command so the "
                    "completion gate has a command-line signal to run"
                ),
            )
        if not isinstance(task.id, str) or not task.id:
            raise TaskValidationError(
                step=1,
                reason=f"id must be a non-empty string (got {task.id!r})",
            )
        if not isinstance(task.title, str) or not task.title:
            raise TaskValidationError(
                step=1,
                reason=f"title must be a non-empty string (got {task.title!r})",
            )
        if not isinstance(task.files_to_modify, list):
            raise TaskValidationError(
                step=1,
                reason=(
                    f"files_to_modify must be a list "
                    f"(got {type(task.files_to_modify).__name__})"
                ),
            )
        if not isinstance(task.depends_on, list):
            raise TaskValidationError(
                step=1,
                reason=(
                    f"depends_on must be a list "
                    f"(got {type(task.depends_on).__name__})"
                ),
            )

    # ------------------------------------------------------------------
    # Step 2 — depends_on consistency
    # ------------------------------------------------------------------

    _TASK_TOKEN_RE = re.compile(r"task[\s\-]+\d+")

    @staticmethod
    def _extract_task_refs(description: str) -> List[str]:
        """Pull ``task-NN`` / ``task NN`` tokens out of ``description``.

        Returns the tokens in first-appearance order, deduplicated.
        """
        if not description:
            return []
        tokens = TaskOutputValidator._TASK_TOKEN_RE.findall(description)
        seen: set = set()
        out: List[str] = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @staticmethod
    def _normalize_dep_token(token: str) -> str:
        """Normalise a token like ``task 2`` / ``task-2`` to bare ``2``.

        The runtime resolver (``agent.py:is_dependency_ready``) uses
        bare ids (``"1"``, ``"1-1"``, ``"1-1-3"``) — it never sees the
        ``task-`` prefix. Stripping it here keeps validator and runtime
        in the same id namespace, so an injected dep can actually
        resolve at runtime instead of silently turning into
        ``missing_upstream:task-1``.

        Tokens that don't match the ``task[ -]?N`` shape are returned
        verbatim so externally-authored ids (e.g. ``"issue-42"``)
        pass through unchanged.
        """
        m = re.match(r"^task[\s\-]+(\d+(?:-\d+)*)$", token)
        if not m:
            return token
        return m.group(1)

    @staticmethod
    def _resolve_dep_token(
        token: str,
        existing_task_ids: "set[str]",
    ) -> "Optional[str]":
        """Resolve a description token to a concrete ``depends_on``
        entry that ``is_dependency_ready`` will recognise.

        Mirrors the runtime prefix-aware resolver
        (``agent.py:is_dependency_ready``, lines 550-572):

          * If the **normalised** token (``task 1`` -> ``"1"``) is
            itself a task id, use that.
          * Otherwise, if some task id starts with ``token + "-"``
            (children of a split parent), use the parent — the
            runtime gate will then wait for ALL children, which is
            the intended "depends on the whole subtask group" semantics
            the planner is asking for when it writes ``task-1`` in
            the description after splitting ``1`` into ``1-1``,
            ``1-2``, ``1-3``.
          * Otherwise, fall back to the literal token as written
            (``task-7``) — some plan shapes use the ``task-`` prefix
            as the canonical id namespace and the runtime gate
            resolves those literally via ``by_id[id]``.
          * Otherwise return ``None`` — the token references a
            task that does not exist in this plan and has no
            children to roll up to. ``auto_fix`` will skip the
            injection and ``validate`` will surface the dangling
            reference as a step-2 error so the LLM can rewrite the
            description, instead of silently burying a phantom dep
            that makes the whole downstream chain unschedulable.
        """
        norm = TaskOutputValidator._normalize_dep_token(token)
        if norm in existing_task_ids:
            return norm
        prefix = norm + "-"
        for tid in existing_task_ids:
            if tid.startswith(prefix):
                return norm
        # Literal ``task-7`` style ids — kept for backward
        # compatibility with plan shapes that use ``task-`` as the
        # canonical id prefix. The description may use either
        # ``task 7`` (space, normalised to ``7``) or ``task-7``
        # (kebab, normalised to ``7``), but the canonical id is
        # ``task-7``; try the kebab form too.
        kebab = token.replace(" ", "-")
        if kebab in existing_task_ids:
            return kebab
        return None

    def _auto_fix_depends_on(
        self,
        task: SubTask,
        existing_task_ids: "set[str]" = None,  # type: ignore[assignment]
    ) -> None:
        """Append description-referenced task ids to ``depends_on``.

        In-place mutation is OK here because the validator already
        holds a deep copy of the caller's snapshot.

        ``existing_task_ids`` should be the set of all task ids in
        the snapshot being validated. When omitted (legacy callers)
        we fall back to permissive behaviour — inject every token —
        which preserves the original buggy contract for those
        callers. New callers in ``auto_fix`` / ``validate`` always
        pass the id set so we can refuse to inject phantom deps.
        """
        if existing_task_ids is None:
            existing_task_ids = set()
        description = task.description or ""
        tokens = self._extract_task_refs(description)
        existing = {self._normalize_dep_token(d) for d in task.depends_on}
        self_id = self._normalize_dep_token(task.id)
        for t in tokens:
            norm = self._normalize_dep_token(t)
            if norm in existing:
                continue
            # Never inject a self-edge. A task whose description
            # mentions its own id ("task 2-1 rewrites …") would
            # otherwise gain ``depends_on=[itself]``. The runtime gate
            # (``is_dependency_ready``) can never satisfy that — a task
            # is not its own upstream — so the task is deferred forever
            # and the run reports ``same_id_loop_reload_failed``.
            if norm == self_id:
                continue
            # Resolve against the real id set so phantom refs
            # (``task-1`` when no such task exists) are skipped here
            # and surfaced as a step-2 error by
            # ``_depends_on_consistency_check`` instead of being
            # injected as a permanent missing dep.
            if existing_task_ids and self._resolve_dep_token(
                t, existing_task_ids,
            ) is None:
                continue
            task.depends_on.append(norm)
            existing.add(norm)

    def _depends_on_consistency_check(
        self,
        task: SubTask,
        existing_task_ids: "set[str]" = None,  # type: ignore[assignment]
    ) -> None:
        """Step-2 check.

        Mirrors the runtime prefix-aware resolver: a description
        reference is satisfied if the normalised token is itself a
        task id, OR if it has children in the plan (children-only
        case is intentionally NOT auto-satisfied here because the
        runtime gate walks each child separately; we surface it as
        an error so the planner rewrites the description to either
        list the children explicitly or reference the parent id).
        """
        if existing_task_ids is None:
            existing_task_ids = set()
        description = task.description or ""
        tokens = self._extract_task_refs(description)
        existing = {self._normalize_dep_token(d) for d in task.depends_on}
        missing: List[str] = []
        for t in tokens:
            norm = self._normalize_dep_token(t)
            if norm in existing:
                continue
            # Token is not in depends_on — but it might be satisfiable
            # via the runtime's prefix-aware resolver. If the literal
            # id exists in the plan, append it; if children exist,
            # also ok. Otherwise it's a phantom ref and we report it.
            if existing_task_ids and self._resolve_dep_token(
                t, existing_task_ids,
            ) is not None:
                continue
            missing.append(norm)
        if missing:
            raise TaskValidationError(
                step=2,
                reason=(
                    f"depends_on missing description-referenced id(s): "
                    f"{sorted(set(missing))} (token not in plan and has "
                    f"no children — would create a phantom dep that "
                    f"runtime is_dependency_ready can never satisfy)"
                ),
            )

    # ------------------------------------------------------------------
    # Step 3 — files_to_modify existence
    # ------------------------------------------------------------------

    def _files_to_modify_existence_check(self, task: SubTask) -> None:
        paths = task.files_to_modify or []
        # Empty list is allowed because the SubTask default already
        # fills it with the UNKNOWN sentinel — but if a caller has
        # set ``files_to_modify=[]`` explicitly, treat that as
        # missing the sentinel.
        if not paths:
            raise TaskValidationError(
                step=3,
                reason=(
                    "files_to_modify is empty — must contain at least "
                    f"the {_LOCAL_UNKNOWN_SENTINEL!r} sentinel"
                ),
            )

        # 2026-09-09 (two-constant scheme): the two sentinel values have
        # different meanings, and the validator discriminates them.
        #   * ``__NO_FILE_CHANGES__`` → read-only by author intent,
        #     accept directly.
        #   * ``__UNKNOWN_MODIFICATIONS__`` → author intended to
        #     modify files but did not list them; reject so the
        #     dispatcher's subagent fill loop fires and asks the
        #     project layout.
        from task import (
            is_no_file_changes,
            is_unknown_modifications,
        )

        if is_no_file_changes(paths):
            return
        if is_unknown_modifications(paths):
            raise TaskValidationError(
                step=3,
                reason=(
                    "files_to_modify is unknown-modifications sentinel — "
                    "subagent fill loop must determine the actual file "
                    "list. (NO_FILE_CHANGES sentinel is accepted "
                    "directly for read-only tasks.)"
                ),
            )

        for raw in paths:
            if not isinstance(raw, str):
                raise TaskValidationError(
                    step=3,
                    reason=(
                        f"files_to_modify entries must be strings "
                        f"(got {type(raw).__name__}: {raw!r})"
                    ),
                )
            # The sentinel — in either form (string or single-element
            # list from ``UNKNOWN_MODIFICATIONS_SENTINEL``) — is OK.
            if raw in (_LOCAL_UNKNOWN_SENTINEL, str(UNKNOWN_MODIFICATIONS_SENTINEL)):
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = self._project_dir / raw
            if not p.exists():
                # Git-history fallback (audit 2026-09-05 plan
                # 2026-09-04 plan task 25):
                #
                # ``files_to_modify`` references a path that no longer
                # exists on disk. The most common cause is that an
                # EARLIER task in the same plan deleted it (e.g. task
                # 25 was "Phase A10: delete cleanup_optimizer_crons.py"
                # but the file had already been deleted by task 16 in
                # commit a6f5cf22). Rejecting this case at load time
                # forced the user to manually rewrite the broken
                # ``files_to_modify`` entry to the sentinel every
                # time — even though the work was already done.
                #
                # Fall back to ``git log --diff-filter=D`` and accept
                # the path if it has been deleted in any reachable
                # commit. The path's absence is then explained (the
                # file was intentionally deleted) rather than treated
                # as a typo.
                if self._was_path_deleted_in_git(p):
                    continue
                # To-be-created file (2026-09-14, plan
                # 2026-09-04 plan task R1-5):
                #
                # A subtask may legitimately CREATE a file, and the
                # path it will create has no way to exist yet. Before
                # this branch the only two ways to satisfy step-3 were
                # "the file already exists" and "git history deleted
                # it", so a create-task had literally no expressible
                # ``files_to_modify`` — the generator's only option was
                # to mislabel it read-only (observed: task 40-1-3
                # "创建 5 个 APIRouter 子模块空骨架" carried the
                # no-file-changes sentinel), which also drops its
                # file-conflict key and lets it race with the tasks
                # that really do touch those files.
                #
                # Accept it when the entry names a file under a real
                # top-level directory of the project
                # (``backend/api/routers.py``, and equally
                # ``native_ext/docs/audit.md`` when ``docs/`` does not
                # exist yet) — see ``_is_to_be_created`` for the exact
                # rule and for why a bare filename is NOT enough. The
                # typo guard survives where it matters: an invented
                # top-level tree, an absolute path outside the project,
                # and a traversal component are all still rejected. The
                # residual gap — a typo'd file NAME inside a real
                # directory — is acceptable because ``files_to_modify``
                # is a conflict key and a prompt hint, not a write
                # allow-list; the executor's own judgement and its test
                # command remain the real gates.
                if self._is_to_be_created(raw, p):
                    continue
                raise TaskValidationError(
                    step=3,
                    reason=f"{raw} not found",
                )

    # Filesystem existence checks against ``files_to_modify`` can
    # surface "path not found" for tasks whose intent was to DELETE
    # that path. ``_was_path_deleted_in_git`` answers the question
    # "was this path deleted in some reachable git commit?" — if
    # yes, the absence is intentional and the validator should accept it.
    #
    # Implementation: run ``git log --diff-filter=D --name-only`` and
    # check whether any commit's deletion list includes the path.
    # ``--follow`` would also rename-trace but is significantly more
    # expensive (one full history walk per file) and rare in this
    # codebase; the strict delete-only check is enough for the bug
    # we're fixing.
    #
    # Returns ``False`` on any error (no git, no repo, timeout) so
    # a transient git failure doesn't silently accept every missing
    # path. The validator will then raise the original
    # "not found" error and the operator can intervene.
    def _was_path_deleted_in_git(self, path: Path) -> bool:
        import subprocess

        try:
            # Resolve to a path relative to ``self._project_dir`` —
            # ``git log`` with no ``--`` separator would interpret
            # the path as a pathspec (and could match unrelated files
            # whose names happen to be a suffix of our target).
            try:
                rel = path.resolve().relative_to(self._project_dir.resolve())
                rel_str = str(rel)
            except ValueError:
                # Path is outside ``project_dir``; use it as-is and
                # hope git handles it (the ``--`` separator will
                # disambiguate).
                rel_str = str(path)

            proc = subprocess.run(
                [
                    "git",
                    "log",
                    "--diff-filter=D",
                    "--name-only",
                    "--pretty=format:",
                    "--",
                    rel_str,
                ],
                cwd=str(self._project_dir),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0:
                return False
            # ``git log`` prints each deleted path on its own line; an
            # exact name match against ``rel_str`` means at least one
            # commit deleted it.
            for line in proc.stdout.splitlines():
                if line.strip() == rel_str:
                    return True
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _is_to_be_created(self, raw: str, path: Path) -> bool:
        """Step-3 helper: is this a file the task will CREATE?

        Thin delegation to the shared module-level
        :func:`is_to_be_created_path` so this gate and the
        generation-time gate in ``tasks_generator`` cannot drift.
        """
        return is_to_be_created_path(raw, path, self._project_dir)

    # ------------------------------------------------------------------
    # Step 4 — test_command function-name AST check
    # ------------------------------------------------------------------

    def _extract_pytest_targets(
        self, test_command: str,
    ) -> List[Tuple[Path, str]]:
        """Parse ``test_command`` and return ``[(file, func_name), ...]``."""
        if not test_command:
            return []
        if "pytest" not in test_command and "py.test" not in test_command:
            return []
        targets: List[Tuple[Path, str]] = []
        for token in test_command.split():
            if "::" not in token:
                continue
            parts = token.split("::")
            path_str = parts[0]
            func_name = parts[-1]
            if not func_name.startswith("test_") and not func_name.startswith("Test"):
                continue
            p = Path(path_str)
            if not p.is_absolute():
                p = self._project_dir / path_str
            targets.append((p, func_name))
        return targets

    def _test_command_function_name_check(self, task: SubTask) -> None:
        cmd = task.test_command or ""
        if not cmd:
            return
        targets = self._extract_pytest_targets(cmd)
        if not targets:
            return  # non-pytest command — nothing to verify
        for path, func_name in targets:
            if not path.exists():
                raise TaskValidationError(
                    step=4,
                    reason=(
                        f"test_command references missing test file: {path}"
                    ),
                )
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                raise TaskValidationError(
                    step=4,
                    reason=(
                        f"test file {path.name} has SyntaxError "
                        f"(line {exc.lineno}, offset {exc.offset}): "
                        f"{exc.msg}"
                    ),
                ) from exc
            found = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == func_name:
                        found = True
                        break
            if not found:
                raise TaskValidationError(
                    step=4,
                    reason=(
                        f"test_command references undefined function "
                        f"{func_name!r} in {path}"
                    ),
                )


__all__ = [
    "TaskOutputValidator",
    "ValidationReport",
    "TaskValidationError",
]
