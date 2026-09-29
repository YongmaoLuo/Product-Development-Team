"""
Task Manager
=============

Manages task state, persistence, and lifecycle.

Task #3.8 split ``tasks.json`` into a static-only definition file.
Per-task runtime state (``status`` / ``end_ts`` / ``commit_sha`` /
``attempt`` / ``schedule_ts`` / ``_repo_version``) lives in the SQLite
``plan_execution.task_progress`` column. :meth:`TaskManager.load_tasks`
still reads the static envelope from disk; runtime state is supplied
through the ``runtime_overrides`` parameter (defaulted to ``None`` for
backward compatibility) and overlaid onto each :class:`SubTask` after
construction.
"""

import json
import os
from pathlib import Path
from typing import List, Dict, Optional, Iterable
from datetime import datetime

from task import SubTask
from utils.atomic_io import atomic_write_json
from orphan_rules import (
    has_mergeable_content as _orphan_has_mergeable_content,
    merged_subtask_kwargs as _orphan_merged_subtask_kwargs,
)


class CycleInTaskGraph(ValueError):
    """Raised when the task graph contains a cycle.

    Distinct from :class:`ValueError` so callers (e.g. agent dispatcher
    or refiner self-heal) can recognize the cycle case and respond
    with the cycle breaker instead of treating it as a generic
    bad-input. Inherits from ``ValueError`` so existing ``except
    ValueError`` blocks continue to work.
    """

    def __init__(self, members: List[str]):
        self.members = sorted(members)
        super().__init__(
            f"Cycle detected: {', '.join(self.members)}"
        )


# Reserved plan-id directory names that must NEVER be used as the SQLite
# ``plan_id`` key. These are too generic to identify a real plan — using
# them causes cross-plan data collisions in ``plan_routing`` /
# ``plan_execution`` / ``plan_task_repository`` (see plan 2026-09-08
# state.db pollution fix: 22 stale tasks under ``plan_id='project'``
# were rendered into the wrong Feishu card).
_RESERVED_PLAN_ID_NAMES: frozenset = frozenset({
    "project", "projects",
    "tasks", "task",
    "plans", "plan",
    "tmp", "temp", "scratch",
    "test", "tests", "testing",
    "src", "app", "backend", "frontend",
    "root", "home", "workspace",
    "default", "untitled", "new", "demo",
})


def derive_plan_id_from_tasks_file(tasks_file) -> str:
    """Derive the canonical ``plan_id`` from a tasks.json path.

    The contract is: ``tasks_file`` lives at ``plans/<plan_id>/tasks.json``,
    so ``tasks_file.parent.name`` is the canonical id.

    Refuses to derive generic / reserved directory names (e.g. a file at
    ``plans/project/tasks.json`` would otherwise produce ``plan_id='project'``
    and pollute state.db). Callers MUST catch :class:`ValueError` and treat
    it as a misconfiguration — the executor/dispatcher should refuse to
    launch with such a layout.

    Rules (in order):
      1. ``parent.name`` must be non-empty.
      2. ``parent.name`` must not appear in :data:`_RESERVED_PLAN_ID_NAMES`
         (case-insensitive).
      3. ``parent.name`` must be at least 4 characters — guards against
         accidental 1-3 character ids like ``1`` or ``a``.
      4. ``parent.name`` must contain only ``[A-Za-z0-9._-]`` — guards
         against directory names that contain path separators or shell
         metacharacters (defence-in-depth against SQL injection / path
         traversal downstream consumers).

    Returns the canonical ``plan_id`` string (unchanged from
    ``parent.name`` when valid).

    Raises:
        ValueError: with a human-readable explanation when the directory
            name is reserved, too short, or contains forbidden chars.
    """
    name = (tasks_file.parent.name or "").strip()
    if not name:
        raise ValueError(
            f"Refusing to derive plan_id from empty parent dir of "
            f"tasks_file={tasks_file!s}"
        )
    lowered = name.lower()
    if lowered in _RESERVED_PLAN_ID_NAMES:
        raise ValueError(
            f"Refusing to derive plan_id={name!r}: directory name is "
            f"reserved (too generic — would collide with other plans in "
            f"plan_routing/plan_execution SQLite rows). Tasks file must "
            f"live under a per-plan subdirectory like "
            f"`plans/<plan_id>/tasks.json` where <plan_id> is at least "
            f"4 unique characters."
        )
    if len(name) < 4:
        raise ValueError(
            f"Refusing to derive plan_id={name!r}: name too short "
            f"(<4 chars). Use a descriptive id like "
            f"`20260101-my-feature`."
        )
    # 2026-09-09 (unicode letters fix): the previous ASCII-only allowlist
    # (``[A-Za-z0-9._-]``) was correct for English-only plan_ids but
    # rejected every Chinese /
    # CJK / accented plan_id that the backend server happily accepted into
    # ``plan_routing`` / ``plan_execution`` SQLite tables (state.db
    # confirms it — see the 2026-09-04 plan).
    # The original concern was path-traversal / shell-injection
    # defence-in-depth, but Unicode letters are neither path
    # separators nor shell metacharacters. The new allowlist:
    #
    #   * ASCII alphanumeric, ``.``, ``_``, ``-`` (legacy)
    #   * Unicode letters (``unicodedata.category(c)[0] == 'L'``)
    #     — Chinese / CJK / Cyrillic / accented Latin all accepted.
    #   * Anything not in the deny list below.
    #
    # Deny list (path separators + shell metacharacters + control chars).
    # Curated against the ``plan_id`` strings already in state.db
    # (``plan_routing.plan_id`` column is the source of truth — every
    # plan_id accepted there must pass this validation, otherwise
    # ``_persist_status_to_sqlite`` silently skips the reproject and
    # the executor falls into ``same_id_loop`` drift).
    _FORBIDDEN_PLAN_ID_CHARS = frozenset(
        # path separators
        "/\\"
        # shell metacharacters that enable command injection
        ";|&$`"
        # control chars (newline would break command-line parsing)
        + "".join(chr(c) for c in range(32) if chr(c) not in "\t")
        + chr(127)
    )
    import unicodedata as _unicodedata
    bad: list[str] = []
    for c in name:
        if c in _FORBIDDEN_PLAN_ID_CHARS:
            bad.append(c)
            continue
        # Unicode letter (e.g. 仓, é, Я) is always allowed.
        if _unicodedata.category(c)[0] == "L":
            continue
        # Anything else (digits, ASCII punctuation not in deny list,
        # symbols not in deny list) is allowed.
        continue
    if bad:
        raise ValueError(
            f"Refusing to derive plan_id={name!r}: contains forbidden "
            f"characters {bad!r}. Allowed: Unicode letters, digits, "
            f"'.', '_', '-' (path separators and shell metacharacters "
            f"are blocked for defence-in-depth)."
        )
    return name


def _ensure_acyclic(tasks: Iterable[SubTask]) -> None:
    """Fail-fast guard: raise if ``tasks`` is not a schedulable DAG.

    Three malformed shapes are distinguished, because they need
    different responses from the caller (2026-09-21 rewrite):

    1. **Self-loop** — ``A`` lists itself in ``depends_on``.
       Raises :class:`CycleInTaskGraph`.
    2. **Dangling reference** — a ``depends_on`` entry names an id that
       is not in the list. Raises
       :class:`framework.task_graph.DanglingTaskDependency` with the
       offending ``<task> -> <missing>`` pair. This is a *bookkeeping*
       bug (the referenced task was renamed / removed / split) and is
       repairable by rewriting the edge.
    3. **Cycle** — a real strongly-connected component of size ≥ 2.
       Raises :class:`CycleInTaskGraph`.

    Order matters: dangling references are checked **before** cycles.
    The previous implementation used Kahn's residual for both and could
    not tell them apart — a single dangling reference made its target's
    entire downstream cone (18 tasks in plan
    a production plan) look like a cycle, and the
    resulting exception had no handler on the refinement path, killing
    the plan. See :mod:`framework.task_graph`.

    Cycle membership is now computed with Tarjan SCC, so nodes that are
    merely *blocked* (downstream of a real cycle) are no longer reported
    as members. ``agent._break_cycle_resilience`` marks every reported
    member ``skipped``, so over-reporting there permanently disables
    healthy tasks.

    Mirrors :func:`agent._validate_dependencies` — both call the same
    ``framework.task_graph`` helpers so the two cannot drift.
    """
    from framework.task_graph import (
        DanglingTaskDependency,
        find_cycle_groups,
        find_dangling_references,
        find_self_loops,
    )

    tasks = list(tasks)
    if not tasks:
        return

    self_loops = find_self_loops(tasks)
    if self_loops:
        raise CycleInTaskGraph(sorted(self_loops))

    dangling = find_dangling_references(tasks)
    if dangling:
        raise DanglingTaskDependency(dangling)

    groups = find_cycle_groups(tasks)
    if groups:
        members = sorted({tid for group in groups for tid in group})
        raise CycleInTaskGraph(members)


class TaskManager:
    """Manages task state and persistence."""

    # Static-only fields a ``tasks.json`` row is allowed to carry
    # post-migration. Anything outside this set is runtime state and
    # must live in SQLite. Used by :meth:`load_tasks` and
    # :meth:`save_tasks` to gate what hits the file.
    _STATIC_TASK_FIELDS = frozenset(
        {
            "id",
            "title",
            "description",
            "test_command",
            "test_commands",
            "project_dir",
            "model_type",
            "depends_on",
            "provider",
            "files_to_modify",
            # 2026-09-16: behaviour-bearing, and previously dropped here
            # (see ``task.SubTask.task_group`` for what the omission
            # cost). Both are static — the orchestrator declares them at
            # write time and nothing rewrites them at runtime.
            "task_group",
            "verification_only",
        }
    )

    def __init__(
        self,
        project_dir: Path,
        tasks_file: Optional[Path] = None,
        runtime_overrides: Optional[Dict[str, Dict]] = None,
    ):
        self.tasks_file = tasks_file or project_dir / "tasks.json"
        self.runtime_overrides: Dict[str, Dict] = runtime_overrides or {}
        self.tasks: List[SubTask] = []
        self.requirement: str = ""
        self.stop_reason: Optional[str] = None
        self.reason_detail: Optional[str] = None
        # 2026-09-10 v4 follow-up: derive plan_id eagerly.
        # ``derive_plan_id_from_tasks_file`` raises ``ValueError`` when
        # ``tasks_file`` is at a non-canonical location (e.g. a
        # root-level ``<project_dir>/tasks.json``); in that case we set
        # ``self.plan_id = None`` and callers in the legacy "no-plan"
        # mode are unaffected.
        #
        # 2026-09-16: this is kept as a LAYOUT VALIDATION (it refuses
        # reserved / too-generic directory names that would collide in
        # ``plan_routing`` / ``plan_runtime``). It no longer gates the
        # removed DB-orphan hydration.
        try:
            self.plan_id: Optional[str] = derive_plan_id_from_tasks_file(
                self.tasks_file,
            )
        except ValueError:
            self.plan_id = None
        self.load_tasks()

    def load_tasks(self):
        """Load tasks from tasks.json file.

        Static-only fields are read from disk; runtime fields
        (``status`` / ``updated_time`` / ``failure_reason`` /
        ``breakdown_count``) are overlaid from
        ``self.runtime_overrides`` if supplied. Without the
        override map, runtime fields fall back to the ``SubTask``
        default (``status="pending"`` / ``updated_time=None`` / etc).
        """
        if not self.tasks_file.exists():
            return

        with open(self.tasks_file, "r") as f:
            data = json.load(f)

        def _make_task(t: dict) -> SubTask:
            task_dict = {k: v for k, v in t.items() if k in self._STATIC_TASK_FIELDS}
            task_dict.setdefault("updated_time", None)
            task_dict.setdefault("failure_reason", None)
            task_dict.setdefault("model_type", None)
            return SubTask(**task_dict)

        if isinstance(data, dict):
            tasks = [_make_task(t) for t in data.get("tasks", [])]
            self.requirement = data.get("requirement", "")
            self.stop_reason = data.get("stop_reason", None)
            self.reason_detail = data.get("reason_detail", None)
        else:
            tasks = [_make_task(t) for t in data]

        # ---- Overlay runtime state from SQLite (task #3.8) ----
        if self.runtime_overrides:
            for t in tasks:
                rt = self.runtime_overrides.get(t.id)
                if not rt:
                    continue
                if rt.get("status"):
                    t.status = rt["status"]
                if rt.get("end_ts"):
                    t.updated_time = rt["end_ts"]
                if rt.get("failure_reason"):
                    t.failure_reason = rt["failure_reason"]
                if rt.get("breakdown_count") is not None:
                    t.breakdown_count = rt["breakdown_count"]

        self.tasks = tasks

        # 2026-09-16: the DB-only orphan hydration that
        # used to run here has been REMOVED.
        #
        # It was the second of two loaders doing the same job — it called
        # the same ``iter_orphan_tasks`` with the same disk-id set as
        # ``AutonomousAgent._load_tasks`` Phase 2, and Phase 2 overwrites
        # ``self.task_manager.tasks`` wholesale a moment later, so this
        # loader's output was discarded in every real flow. Its only
        # observable effect was on the ``execution_started`` log counts.
        #
        # Two copies of the same judgement are what produced that run
        # A production bug: the two drifted, and a task whose row held a real
        # 895-char description but no ``test_command`` was turned into a
        # content-free placeholder that the subagent correctly refused to
        # run. The rules now live once, in ``orphan_rules``, and are
        # applied once, by ``agent._load_tasks`` Phase 2.

    def save_tasks(self):
        """Save tasks to tasks.json file.

        Post task #3.8 only static fields are written — runtime state
        lives in ``plan_execution.task_progress.tasks``. The
        ``migrate_tasks_json_to_progress`` helper runs once at server
        startup to lift any pre-migration runtime fields into SQLite
        and stamp the file accordingly.

        Cycle guard: before writing, run Kahn's residual to surface
        any cycle in the task graph. Cycle writes are rejected
        outright (raising ``CycleInTaskGraph``) so a future LLM
        regeneration that accidentally introduces a back-edge (e.g.
        ``4-2-2 →4-2-3`` while 4-2-3 already depends on 4-2-2,
        the bug that froze plan 2026-09-04 on
        2026-09-05) cannot persist and silently break the next
        executor restart.
        """
        _ensure_acyclic(self.tasks)
        # 2026-09-16: ``agent._load_tasks`` Phase 2 may have appended
        # placeholder SubTask objects to ``self.tasks`` for status-only
        # rows that exist in DB but not on disk.  Placeholders have no
        # static content (title is ``"[db-orphan:{id}]"``) and must NOT be
        # persisted back to ``tasks.json`` — the disk file is
        # the source of truth for static fields.  Filter by the
        # ``_origin`` tag the hydrate step sets.
        persistable = [
            t for t in self.tasks
            if getattr(t, "_origin", None) != "db_orphan"
        ]
        tasks_data = []
        for task in persistable:
            task_dict = task.model_dump(exclude_none=False)
            # Strip runtime fields that no longer belong on disk.
            for field in ("status", "updated_time", "failure_reason",
                          "breakdown_count"):
                task_dict.pop(field, None)
            tasks_data.append(task_dict)

        # Atomic replace (2026-09-16): ``tasks.json`` is the dispatcher's
        # startup input and the authoritative list of static fields. The
        # old plain ``open(w)`` truncated it first, so a crash or a full
        # disk mid-write left a half-written file that the next
        # ``_load_tasks`` would reject — taking the whole plan down.
        #
        # The temp name must be UNIQUE per writer. This used a fixed
        # ``tasks.json.tmp``, and every running sub-agent persists on its
        # own status change — so two concurrent writers truncated the same
        # temp file and the loser's ``os.replace`` raised ``ENOENT``,
        # failing an otherwise healthy task. ``atomic_write_json`` builds
        # the temp file with ``mkstemp``, which is what makes the rename
        # safe under concurrency.
        atomic_write_json(
            self.tasks_file,
            {
                "requirement": self.requirement,
                "stop_reason": self.stop_reason,
                "reason_detail": self.reason_detail,
                "tasks": tasks_data,
            },
            indent=2,
            reraise=True,
        )

    def set_tasks(self, tasks: List[Dict], requirement: Optional[str] = None):
        """
        Set new tasks, preserving metadata from existing tasks.

        Args:
            tasks: List of task dictionaries
            requirement: Optional requirement string
        """
        new_tasks = []
        existing_tasks_map = {t.id: t for t in self.tasks}

        for new_task_dict in tasks:
            task_id = new_task_dict.get("id")

            if task_id in existing_tasks_map:
                existing_task = existing_tasks_map[task_id]
                if "updated_time" not in new_task_dict:
                    new_task_dict["updated_time"] = existing_task.updated_time
            elif "updated_time" not in new_task_dict:
                new_task_dict["updated_time"] = None

            new_task = SubTask(**new_task_dict)

            # 2026-09-16: carry the ``_origin`` tag across a replacement.
            # ``_origin`` is a dynamic attribute, not a model field, so
            # it is absent from the ``model_dump()`` dicts the refiner
            # path feeds in here. Without this, a ``db_orphan``
            # placeholder that the refiner echoed back would lose its
            # tag and ``save_tasks`` would persist its
            # ``"[db-orphan:...]"`` title to ``tasks.json`` as if it
            # were an authored task. The tag is sourced from the live
            # object (``existing_tasks_map``), which still has it.
            origin = getattr(existing_tasks_map.get(task_id), "_origin", None)
            if origin:
                new_task._origin = origin  # type: ignore[attr-defined]

            new_tasks.append(new_task)

        # Cycle guard: fail-fast on any cycle introduced by this
        # replacement, BEFORE swapping ``self.tasks`` (so we don't
        # lose the previous good state if the new payload is bad).
        # See ``save_tasks`` for the same guard rationale.
        _ensure_acyclic(new_tasks)

        self.tasks = new_tasks
        if requirement:
            self.requirement = requirement
        self.save_tasks()
    
    def get_next_task(self) -> Optional[SubTask]:
        """
        Get the next task to process.

        Returns:
            Next pending/in_progress task, or None if all are completed/failed
        """
        for task in self.tasks:
            if task.status in ["pending", "in_progress"]:
                return task
        return None

    def find_completed_duplicate(self, task: SubTask) -> Optional[SubTask]:
        """
        Find a completed task with the same title as the given task.

        A match means the system already applied this fix but the problem recurred,
        indicating a circular loop the agent cannot resolve on its own.

        Args:
            task: The task about to be executed

        Returns:
            The matching completed task, or None if no duplicate found
        """
        for t in self.tasks:
            if t.id != task.id and t.status == "completed" and t.title == task.title:
                return t
        return None

    def update_task_status(self, task_id: str, status: str):
        """
        Update task status and timestamp.

        Args:
            task_id: Task ID to update
            status: New status value
        """
        for task in self.tasks:
            if task.id == task_id:
                task.status = status
                task.updated_time = datetime.utcnow().isoformat()
                # 2026-09-16: a task that just SUCCEEDED has no current
                # failure. The executor now injects ``failure_reason``
                # into the prompt on the first attempt of every
                # re-dispatch (``agent.py:_build_prior_failure_block``),
                # so leaving a reason attached past a successful
                # completion means a later re-dispatch would be fed a
                # failure that was already fixed. The SQLite column is
                # already cleared on any non-``failed`` status (see
                # ``_persist_status_to_sqlite``); this clears the
                # in-memory copy, which is what the executor reads.
                if status == "completed":
                    task.failure_reason = None
                break
        # Mirror the new status into ``self.runtime_overrides`` so a
        # subsequent ``load_tasks`` (or any caller that re-reads
        # ``tasks.json``) sees the new value before the SQLite write
        # round-trips. Without this mirror, ``task_manager.tasks[i]
        # .status`` is the source of truth but a reload would
        # discard it (the on-disk file strips status).
        known_ids = {t.id for t in self.tasks}
        if task_id in known_ids:
            override = {
                "status": status,
                "updated_time": task.updated_time if task else None,
            }
            # 2026-09-13: preserve a previously recorded
            # ``failure_reason`` across a status reset. The
            # inline-review block path records the review reason
            # (``record_task_failure``) and then reverts the task
            # to ``pending`` so the next run picks it up —
            # clobbering the override map here erased the
            # operator-visible reason on the very next
            # ``load_tasks`` rehydrate.
            #
            # 2026-09-16: except on success. ``completed`` is terminal
            # and means the failure was resolved; carrying the reason
            # forward would let ``_build_prior_failure_block`` feed a
            # fixed failure back to the model on a later re-dispatch.
            _prev_override = self.runtime_overrides.get(task_id) or {}
            if status != "completed" and _prev_override.get("failure_reason"):
                override["failure_reason"] = _prev_override["failure_reason"]
            self.runtime_overrides[task_id] = override
        # Task #3.8 / same-id-loop fix: also persist to
        # ``plan_execution.task_progress.tasks`` so a subsequent
        # ``_load_tasks`` (which strips status from ``tasks.json``)
        # can re-hydrate from SQLite. Without this write, every
        # ``_load_tasks`` reload reverts to ``status="pending"`` and
        # the dispatcher re-schedules an already-completed task,
        # tripping the ``same_id_loop_recovery`` guard on the second
        # pass. Non-fatal on failure — the in-memory mirror above is
        # still correct for the current process.
        #
        # 2026-09-17: the SQLite mirror now obeys the SAME membership
        # guard as the ``runtime_overrides`` mirror above.
        # ``PlanTaskRepository.update_task`` is an ``INSERT ... ON
        # CONFLICT DO UPDATE``, so writing runtime state for a task
        # that is no longer in the plan RE-CREATES its row — and a
        # content-free one, because the payload carries runtime fields
        # only. That is exactly the residue a refiner split leaves:
        # ``agent._apply_refiner_structure`` deletes the parent's row,
        # and the "failed after all retries" write 29 ms later put it
        # back with ``title`` / ``description`` / ``test_command`` all
        # NULL. Inert for scheduling (the next ``_load_tasks`` Phase 2
        # sees a terminal orphan and supersedes it) but an entry in the
        # ledger that should not exist — the refiner *deleted* the
        # parent, and it was gone, until this write undid it. A task
        # that is not in ``self.tasks`` is one the plan has
        # deliberately dropped; do not resurrect it.
        if task_id in known_ids:
            self._persist_status_to_sqlite(task_id, status)
        else:
            # 2026-09-22: this skip used to be silent. It is not a
            # harmless no-op — ``tasks.json`` strips ``status`` by
            # design (see ``save_tasks``), so the SQLite row is the
            # ONLY place a terminal status can survive a reload. A
            # silently skipped mirror means the next ``_load_tasks``
            # hydrates the task back to ``pending`` and the dispatcher
            # re-executes work that already finished.
            #
            # On a production plan eight completed
            # tasks ended the run with ``status IS NULL`` in
            # ``plan_tasks``; every one of them was re-dispatched, and
            # the re-run burned ~2h of provider quota.
            self._log_mirror_skipped(task_id, status, known_ids)
        self.save_tasks()

    def record_task_failure(self, task_id: str, error: str):
        """
        Mark a task as failed and record the failure reason.

        Args:
            task_id: Task ID to mark as failed
            error: Error message to record as failure_reason
        """
        for task in self.tasks:
            if task.id == task_id:
                task.status = "failed"
                task.failure_reason = error[:1000]
                task.updated_time = datetime.utcnow().isoformat()
                break
        # Mirror into runtime_overrides + SQLite (same reason as
        # ``update_task_status`` — task #3.8 strips status from
        # ``tasks.json`` so we MUST persist to
        # ``plan_execution.task_progress`` to keep the dispatcher
        # from re-scheduling this failed task).
        #
        # 2026-09-17: both mirrors share one membership guard. This is
        # the write that resurrected a refiner-deleted parent on
        # 2026-09-16 (see ``update_task_status`` for the full
        # timeline) — the in-memory mirror already refused a task the
        # plan no longer contains, the SQLite one did not.
        if task_id in {t.id for t in self.tasks}:
            self.runtime_overrides[task_id] = {
                "status": "failed",
                "failure_reason": error[:1000],
                "updated_time": datetime.utcnow().isoformat(),
            }
            self._persist_status_to_sqlite(task_id, "failed")
        self.save_tasks()

    def update_task_commit_sha(self, task_id: str, sha: str):
        """
        Write back the git commit SHA produced by ``_commit_task_changes``.

        Args:
            task_id: Task ID whose commit_sha is being recorded.
            sha: Full git commit SHA returned by ``git_manager.rev_parse('HEAD')``.

        Why this method exists (2026-09-11 plan): prior to this addition
        every completed task had ``commit_sha IS NULL`` in state.db because
        ``_commit_task_changes`` ran ``git_manager.commit()`` but never
        recorded the resulting SHA back into the per-task row. Any audit
        / verification / card join that relied on ``plan_tasks.commit_sha``
        saw "no commits" for every plan, masking real work. This helper
        closes the loop: commit → record SHA → runtime overlay
        (``plan_tasks.commit_sha``) is now consistent with on-disk
        ``tasks.json`` and ``git log``.

        Mirrors the same in-memory + ``runtime_overrides`` + SQLite
        pattern as ``record_task_failure`` so a subsequent
        ``_load_tasks`` reload sees the new value before the SQLite
        write round-trips. ``commit_sha`` is in
        ``_RUNTIME_OVERLAY_FIELDS`` (``server.py``) so the progress
        endpoint already inherits it onto the task dict it returns.
        """
        if not sha:
            return
        sha = str(sha).strip()
        for task in self.tasks:
            if task.id == task_id:
                task.commit_sha = sha
                task.updated_time = datetime.utcnow().isoformat()
                break
        # Mirror into runtime_overrides so a subsequent ``load_tasks``
        # sees the new value before the SQLite write round-trips.
        if task_id in {t.id for t in self.tasks}:
            self.runtime_overrides[task_id] = {
                **self.runtime_overrides.get(task_id, {}),
                "commit_sha": sha,
                "updated_time": datetime.utcnow().isoformat(),
            }
        # Persist to ``plan_tasks`` (single source of truth for the
        # per-task runtime overlay). ``_persist_status_to_sqlite``
        # already accepts ``status`` + ``end_ts``; for ``commit_sha``
        # we use a small dedicated helper that mirrors the same
        # INSERT ... ON CONFLICT DO UPDATE pattern but writes the
        # commit_sha column instead of ``status`` / ``end_ts``.
        try:
            self._persist_commit_sha_to_sqlite(task_id, sha)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: in-memory mirror + on-disk ``tasks.json``
            # are still authoritative for the current process.
            self._log_persist_failure(
                self.tasks_file.parent.name if self.tasks_file else "?",
                task_id,
                exc,
                0,
                exhausted=False,
            )
        # ``save_tasks()`` is what serialises ``task.commit_sha`` back
        # into ``tasks.json``. Without this, the next process that
        # loads ``tasks.json`` sees ``commit_sha=None`` for this row
        # even though the executor actually wrote one.
        self.save_tasks()

    def _persist_commit_sha_to_sqlite(self, task_id: str, sha: str) -> None:
        """Mirror ``update_task_commit_sha`` into ``plan_tasks.commit_sha``.

        Mirrors the v4 schema normalisation pattern from
        :meth:`_persist_status_to_sqlite` but writes only the
        ``commit_sha`` column. Single ``PlanTaskRepository.update_task``
        call with ``expected_version=0`` (last-writer-wins) is enough —
        SQLite row-level atomicity + the ``INSERT ... ON CONFLICT DO
        UPDATE`` SQL replace the legacy read-modify-write + CAS
        pattern. Failure modes are non-fatal: the in-memory mirror and
        on-disk ``tasks.json`` stay correct for the current process.
        """
        try:
            plan_id = derive_plan_id_from_tasks_file(self.tasks_file)
        except ValueError as exc:
            import sys
            print(
                f"[TaskManager] {exc} — skipping commit_sha mirror",
                file=sys.stderr,
            )
            return
        if not plan_id:
            return
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.repositories.plan_task_repository import (
                PlanTaskRepository,
            )
        except ImportError:
            return
        try:
            from config_paths import resolve_state_db_path

            db_path = resolve_state_db_path()
            conn = _open_db(db_path)
            try:
                from state_machine.db.schema import migrate as _migrate
                _migrate(conn)
                repo = PlanTaskRepository(conn)
                repo.update_task(
                    plan_id=plan_id,
                    task_id=task_id,
                    fields={"commit_sha": sha},
                    expected_version=0,
                )
                return
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            self._log_persist_failure(
                plan_id, task_id, exc, 0, exhausted=False,
            )

    def set_stop_reason(self, reason: str, detail: Optional[str] = None):
        """
        Record why the run stopped, persisted as top-level fields in tasks.json.

        Args:
            reason: One of 'success' or 'repeated_failure'
            detail: Human-readable description of the specific stopping cause
        """
        self.stop_reason = reason
        self.reason_detail = detail
        self.save_tasks()

    # ``add_task`` used to live here: append to ``self.tasks``, then
    # ``save_tasks()``. It went with the dispatcher's self-split path —
    # ``backend/tests/unit/test_agent_no_self_split.py`` is the gate that
    # keeps that path from coming back, because it duplicated the refiner's
    # responsibility and diverged the executor's task list from the
    # refiner's. A new sub-task is persisted by the refiner, straight to
    # ``state.db`` through ``PlanTaskRepository``; the dispatcher never
    # writes the task list. The method is gone rather than left unused so
    # the banned shape is not one call away.

    def _persist_status_to_sqlite(self, task_id: str, status: str) -> None:
        """Mirror ``update_task_status`` into ``plan_tasks`` (v4 schema).

        Task #3.8 stores per-task runtime state (status / updated_time /
        failure_reason) on disk via ``save_tasks()`` AND mirrors it into
        ``plan_tasks`` (v4 schema normalisation).  ``save_tasks()`` strips
        those runtime fields, so ``update_task_status`` MUST also push
        them through
        ``PlanTaskRepository.update_task`` — otherwise the next
        ``_load_tasks`` reload sees ``status="pending"`` (SubTask's
        default) and the dispatcher keeps re-scheduling the
        already-completed task, tripping ``same_id_loop_recovery``.

        The plan_id is derived from ``self.tasks_file.parent.name`` —
        ``tasks_file`` lives at ``plans/{plan_id}/tasks.json``, so
        ``parent.name`` is the canonical id. Same resolution as
        ``server._state_db_path``.

        v4 write path:

          * Single ``PlanTaskRepository.update_task`` call with
            ``expected_version=0`` (last-writer-wins).
          * No ``INSERT OR IGNORE INTO plan_execution ...`` bootstrap
            — ``plan_tasks`` is now an independent table that does not
            require a parent row.
          * No retry loop — SQLite row-level atomic ``INSERT ... ON
            CONFLICT DO UPDATE`` replaces the legacy read-modify-write
            + CAS pattern that caused the silent-fail bug
            (audit 2026-09-09).
          * ``end_ts`` is ISO-8601 UTC with second precision and ``Z``
            suffix (e.g. ``2026-09-09T14:37:36Z``), the canonical
            format consumed by the data viewer.

        Failure modes (all non-fatal — the in-memory mirror and the
        on-disk ``tasks.json`` are still authoritative for the current
        process):
          * ``state_machine`` not importable (test harness without it)
          * SQLite write error → logged via stderr AND structured
            logger ``task_persist_failed``.
        """
        # 2026-09-08: refuse to silently derive a generic /
        # reserved plan_id from ``tasks_file.parent.name``. The legacy
        # line ``plan_id = self.tasks_file.parent.name`` accepted ANY
        # parent dir name, which produced ``plan_id='project'`` for a
        # tasks file at ``plans/project/tasks.json`` — that single
        # oversight caused 22 stale tasks to be written under that
        # namespace in state.db and surfaced in the wrong Feishu card.
        try:
            plan_id = derive_plan_id_from_tasks_file(self.tasks_file)
        except ValueError as exc:
            # Log to stderr (state_machine SQLite layer cannot be
            # assumed available here) and skip the persistence. The
            # in-memory mirror in self.tasks stays correct for the
            # current process; the next caller that fixes the
            # layout will get a real plan_id and the writes will
            # resume.
            import sys
            print(
                f"[TaskManager] {exc} — skipping plan_execution mirror",
                file=sys.stderr,
            )
            return
        # Backwards-compat: legacy callers that did ``if not plan_id:
        # return`` still get the same behaviour on success because
        # ``derive_plan_id_from_tasks_file`` always returns a non-empty
        # string when it does not raise.
        if not plan_id:
            return
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.repositories.plan_task_repository import (
                PlanTaskRepository,
            )
        except ImportError:
            # state_machine unavailable (rare — happens when this
            # module is imported by a unit test that mocks out the
            # storage layer). The in-memory mirror is still correct.
            return
        try:
            from config_paths import resolve_state_db_path

            db_path = resolve_state_db_path()
            # 2026-09-09 schema v4: per-task state lives in ``plan_tasks``,
            # so a single ``INSERT ... ON CONFLICT DO UPDATE`` from
            # ``PlanTaskRepository.update_task`` is enough.  No
            # ``INSERT OR IGNORE INTO plan_execution`` bootstrap, no
            # retry loop — SQLite row-level atomicity + last-writer
            # -wins semantics replace the legacy read-modify-write
            # + CAS pattern.  See
            # ``tests/unit/test_task_persist_no_nested_txn.py``.
            end_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            fields = {"status": status, "end_ts": end_ts}
            # 2026-09-23: ``PlanTaskRepository.update_task`` is now a
            # partial update — it leaves runtime columns the payload
            # omits exactly as they were. This write used to rely on
            # the old full-row overwrite to clear ``failure_reason``
            # on any non-``failed`` status (the in-memory copy is
            # cleared for the same reason in ``update_task_status``,
            # see its 2026-09-16 note). State it explicitly instead of
            # inheriting it from a clamping side effect.
            if status != "failed":
                fields["failure_reason"] = None
            # 2026-09-16: ``failure_reason`` was dropped at this boundary.
            # The docstring above promised the mirror carried it, and
            # ``ALLOWED_TASK_FIELDS`` / the progress API overlay
            # (``server.py`` ``_RUNTIME_OVERLAY_FIELDS``) were built to
            # read it — but the fields dict never included it. The
            # executor subprocess exits right after the run, so the
            # in-memory ``runtime_overrides`` copy died with the process
            # and every failed task rendered "未知原因" on the card
            # (an earlier plan: every failed task NULL in plan_tasks).
            # ``record_task_failure`` sets the reason on the in-memory
            # task AND in ``runtime_overrides`` before calling here, so
            # both lookups are populated by the time a failed status
            # reaches this persist.
            if status == "failed":
                task = next(
                    (t for t in self.tasks if t.id == task_id), None
                )
                reason = (
                    getattr(task, "failure_reason", None)
                    or (self.runtime_overrides.get(task_id) or {}).get(
                        "failure_reason"
                    )
                )
                if reason:
                    fields["failure_reason"] = reason[:1000]

            conn = _open_db(db_path)
            try:
                from state_machine.db.schema import migrate as _migrate
                _migrate(conn)
                repo = PlanTaskRepository(conn)
                # ``expected_version=0`` → last-writer-wins.  The
                # ``update_task`` SQL is one statement and row-level
                # atomic in SQLite, so concurrent writers may race
                # but no write is silently dropped.
                repo.update_task(
                    plan_id=plan_id,
                    task_id=task_id,
                    fields=fields,
                    expected_version=0,
                )
                # 2026-09-22 — read back. ``update_task`` is an
                # ``INSERT ... ON CONFLICT DO UPDATE`` and is documented
                # as last-writer-wins, so a mismatch here means some
                # other writer clobbered the row (a concurrent refiner
                # structure pass is the known one). Without the check
                # the loss is invisible until the next reload silently
                # hydrates the task back to ``pending`` and re-runs it
                # — the 0921 failure mode. One retry, then a loud ERROR.
                if not self._status_roundtripped(repo, plan_id, task_id, status):
                    repo.update_task(
                        plan_id=plan_id,
                        task_id=task_id,
                        fields=fields,
                        expected_version=0,
                    )
                    if not self._status_roundtripped(
                        repo, plan_id, task_id, status,
                    ):
                        self._log_persist_readback_mismatch(
                            plan_id, task_id, status, repo,
                        )
                return  # SUCCESS — in-memory mirror is now durable
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            # Outer catch — anything that escaped the inner try
            # (e.g. _open_db itself failed) still surfaces to stderr
            # so the operator can diagnose. The in-memory mirror and
            # the on-disk tasks.json remain authoritative for the
            # current process.
            self._log_persist_failure(
                plan_id, task_id, exc, 0, exhausted=False,
            )

    @staticmethod
    def _status_roundtripped(repo, plan_id: str, task_id: str, status: str) -> bool:
        """True when ``plan_tasks`` now reports ``status`` for the task.

        Any error reads as "not verified" — the caller retries once and
        then logs; an unreadable row must not be reported as a success.
        """
        try:
            entry = repo.get_task(plan_id, task_id)
        except Exception:
            return False
        return bool(entry) and entry.get("status") == status

    def _log_persist_readback_mismatch(
        self, plan_id: str, task_id: str, status: str, repo
    ) -> None:
        """Log a status write that did not survive its own round-trip.

        Reached only after the write and one retry both failed to read
        back. The in-memory mirror is still correct for this process, so
        this does not raise — but the durable store disagrees, and the
        next ``_load_tasks`` will hydrate whatever the row actually
        holds. Callers that care about correctness across a reload
        cannot rely on ``status`` having stuck.
        """
        import sys
        actual = None
        try:
            entry = repo.get_task(plan_id, task_id)
            actual = (entry or {}).get("status")
        except Exception:
            pass
        msg = (
            f"[TaskManager._persist_status_to_sqlite] read-back MISMATCH for "
            f"plan_id={plan_id!r} task_id={task_id!r}: wrote {status!r}, "
            f"row reports {actual!r}. A concurrent writer is clobbering this "
            f"row; the status will not survive a reload."
        )
        print(msg, file=sys.stderr)
        logger = getattr(self, "logger", None)
        if logger is not None:
            try:
                logger.error(
                    "task_persist_readback_mismatch",
                    msg,
                    task_id=task_id,
                    data={
                        "plan_id": plan_id,
                        "expected_status": status,
                        "actual_status": actual,
                    },
                )
            except Exception:
                pass

    def _log_mirror_skipped(
        self,
        task_id: str,
        status: str,
        known_ids: set,
    ) -> None:
        """Emit a loud record when a status write skips the SQLite mirror.

        Called from :meth:`update_task_status` when ``task_id`` is not in
        ``self.tasks``. That is a legitimate state (the refiner dropped
        the task, and re-creating its row would resurrect a deleted task
        — see ``test_persist_does_not_resurrect_removed_task``), so this
        does not raise. It must still be visible: the disk file carries
        no status, so a skipped mirror is indistinguishable from "this
        task never completed" on the next reload.
        """
        import sys
        msg = (
            f"[TaskManager.update_task_status] SQLite mirror SKIPPED for "
            f"task_id={task_id!r} status={status!r}: the task is not in "
            f"the current plan (known tasks={len(known_ids)}). The status "
            f"will NOT survive a reload — tasks.json does not carry "
            f"runtime state."
        )
        print(msg, file=sys.stderr)
        logger = getattr(self, "logger", None)
        if logger is not None:
            try:
                logger.error(
                    "task_persist_skipped_unknown_task",
                    msg,
                    task_id=task_id,
                    data={
                        "status": status,
                        "known_task_count": len(known_ids),
                        "known_ids_sample": sorted(known_ids)[:20],
                    },
                )
            except Exception:
                pass

    def _log_persist_failure(
        self,
        plan_id: str,
        task_id: str,
        exc: BaseException,
        attempt: int,
        *,
        exhausted: bool,
    ) -> None:
        """Emit a ``task_persist_failed`` event to stderr + structured logger.

        2026-09-09: previously the only signal was
        ``print(msg, file=sys.stderr)``, which agents routed to
        /dev/null via the executor subprocess stdout
        redirect. Now also surface to ``self.logger`` (the
        ``TaskManager`` callers typically do not attach a logger, so
        this is best-effort) AND include the retry-exhausted flag so
        an operator can grep ``task_persist_failed`` and
        ``task_persist_exhausted`` separately.
        """
        import sys
        msg = (
            f"[TaskManager._persist_status_to_sqlite] persist "
            f"failed for plan_id={plan_id!r} task_id={task_id!r}: "
            f"{type(exc).__name__}: {exc} "
            f"(attempt={attempt}, exhausted={exhausted})"
        )
        print(msg, file=sys.stderr)
        logger = getattr(self, "logger", None)
        if logger is not None:
            try:
                event_name = (
                    "task_persist_exhausted" if exhausted
                    else "task_persist_failed"
                )
                logger.warning(
                    event_name,
                    msg,
                    task_id=task_id,
                    data={
                        "plan_id": plan_id,
                        "attempt": attempt,
                        "exhausted": exhausted,
                        "error": str(exc)[:500],
                    },
                )
            except Exception:
                pass