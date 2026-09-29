"""
Autonomous Agent
================

Main orchestrator for autonomous software development.
"""

import asyncio
import os
import subprocess
import re
import json
import time
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

# Module-level logger — surfaces provider-fallback resolution to
# backend.log (or any Python logging handler). Distinct from
# ExecutionLogger (per-plan structured log) so that one
# load_fallback_order() call is visible in the global stream for
# grep / monitoring.
log = logging.getLogger("agent")

from coding_tool import CodingTool, OpenCodeCodingTool, ClaudeCodingTool, VendorCCodingTool, ApiError, HardTimeoutError
from bounded_subprocess import run_bounded
from usage_registry import plan_usage_context
from task_manager import TaskManager, CycleInTaskGraph
from executor import Executor
from config_paths import resolve_state_db_path
from git_manager import GitManager
from refiner import TaskRefiner
from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL, NO_FILE_CHANGES_SENTINEL
from test_command_quality import inspect_command as inspect_test_command
from config import AgentConfig
from config_registry import ConfigRegistry
from retry_manager import RetryManager
from background_manager import BackgroundManager
from rollback_manager import RollbackManager
from execution_logger import ExecutionLogger
from provider_concurrency import ProviderConcurrencyController
from dynamic_provider_concurrency import (
    ActiveConcurrencyTracker,
    acquired_provider_slot,
)
from file_lock_broker import (
    DEFAULT_ACQUIRE_TIMEOUT,
    BrokerTaskHandle,
    FileLockBroker,
)
from file_lock_manager import FileLockManager
from file_lock_protocol import (
    ACQUIRED,
    ALREADY_HELD,
    TIMED_OUT,
    fallback_locks_dir,
    locks_dir_for_plan,
)
from task_repository import TaskRepository, ConflictError, ValidationError
from utils.atomic_io import atomic_write_json
from framework.task_graph import DanglingTaskDependency, find_dangling_references
from framework.prompts import INLINE_SPEC_CODE_REVIEW_PROMPT

# Re-export ``INLINE_SPEC_CODE_REVIEW_PROMPT`` so existing call sites
# that wrote ``from agent import INLINE_SPEC_CODE_REVIEW_PROMPT`` keep
# working. The canonical home is :mod:`framework.prompts`; new code
# MUST import from there directly. Both ``agent`` and
# ``verification_agent`` import the constant from the same leaf module
# to break the historical ``verification_agent -> agent`` cycle.
__all__ = ["INLINE_SPEC_CODE_REVIEW_PROMPT"]

# Dependency-injection contract (TC-006 / AC-009 / VP-024): the
# per-process dynamic tracker and in-flight file map are owned by the
# FastAPI lifespan and threaded into :class:`AutonomousAgent` through
# ``__init__`` kwargs. The lifespan constructs a single
# :class:`ActiveConcurrencyTracker` and a single in-flight dict and
# hands them to every agent constructed during the process — so all
# dispatches in this process share one slot-count / file-lock
# accounting surface instead of diverging module-level singletons.
#
# The ``__init__`` defaults build a fresh tracker + empty in-flight
# map when the caller does not pass one (tests, ad-hoc scripts). The
# production call site (``execution_orchestrator``) reads both off
# ``app.state.runtime_state`` and passes them through explicitly.

# Inline spec/code review prompt template (DP3) lives in
# :mod:`framework.prompts` so both ``agent`` and ``verification_agent``
# can import it from the same leaf module. Importing it here keeps the
# historical ``agent.INLINE_SPEC_CODE_REVIEW_PROMPT`` symbol alive for
# any caller that did ``from agent import INLINE_SPEC_CODE_REVIEW_PROMPT``;
# new code SHOULD import directly from ``framework.prompts``.


class _PreAcquiredSlot:
    """Releases a slot that :func:`_load_provider_info` already took.

    ``_load_provider_info(reserve_slot=True)`` takes the slot inside the
    same walk that picks the provider, so that the capacity check and the
    increment cannot be interleaved by a competing dispatcher. The
    resulting object quacks like the :func:`acquired_provider_slot`
    context manager so the caller's ``_release_dispatch_slot`` contract
    is unchanged — but its ``__enter__`` is a no-op, because the slot is
    already held. Re-acquiring here would double-count and silently halve
    every provider's real concurrency.
    """

    def __init__(self, provider_name: str, tracker: ActiveConcurrencyTracker) -> None:
        self._provider_name = provider_name
        self._tracker = tracker

    def __enter__(self) -> "_PreAcquiredSlot":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._tracker.release(self._provider_name)
        return False


def _acquire_dispatch_slot(
    provider_name: str,
    tracker: ActiveConcurrencyTracker,
    *,
    already_reserved: bool = False,
) -> Optional[object]:
    """Acquire a dynamic-cap slot for ``provider_name``.

    Returns the context object the caller MUST release (via
    ``__exit__(None, None, None)``) after the subagent's run
    finishes, or ``None`` when no slot was taken. The empty-string
    case (``provider_name == ""``) is the parent-fallback sentinel:
    no slot is acquired because the parent process's CC Switch proxy
    is the fallback, not a tracked slot.

    ``already_reserved=True`` (2026-09-17) is for the production call
    site, where ``_load_provider_info`` already took the slot atomically
    during its walk. It returns a :class:`_PreAcquiredSlot` — release
    semantics are identical, but no second ``acquire()`` happens. The
    default path (unconditional acquire) is kept for callers that
    resolved the provider some other way; note it does NOT enforce the
    fleet ceiling, so anything reaching production should prefer the
    reserve-in-the-walk form.

    This is split out of :func:`autonomous_coding` so the wiring
    can be tested without going through the full SubagentConfig +
    GitManager + ClaudeCodingTool initialization that the dispatch
    site does after ``_load_provider_info`` returns.
    """
    if not provider_name:
        return None
    if already_reserved:
        return _PreAcquiredSlot(provider_name, tracker)
    ctx = acquired_provider_slot(provider_name, tracker)
    ctx.__enter__()
    return ctx


def _release_dispatch_slot(ctx: Optional[object]) -> None:
    """Release a slot previously acquired via :func:`_acquire_dispatch_slot`.

    ``ctx=None`` is a no-op (the parent-fallback case). Any
    exception from the slot release is swallowed — leaking the slot
    in the tracker is worse than a noisy log line, so we always
    best-effort release.
    """
    if ctx is None:
        return
    try:
        ctx.__exit__(None, None, None)
    except Exception:
        pass


def _load_provider_info(
    provider_priority: List[str],
    tracker: Optional[ActiveConcurrencyTracker] = None,
    now: Optional[datetime] = None,
    *,
    reserve_slot: bool = False,
) -> dict:
    """Resolve the first available provider's base_url / api_key from
    the CC Switch database via ``cc_switch``.

    Selection strategy is dynamic, driven by each provider's 5-hour
    remaining quota (read from ``provider-order.json`` via
    :func:`provider_order.load_provider_5h_usage`):

      * The first provider in ``provider_priority`` that has spare
        capacity under its dynamic cap (per :class:`ActiveConcurrencyTracker`
        and :func:`compute_dynamic_limit`) AND has a valid CC Switch
        DB row wins.
      * Providers at capacity, on vendor-b peak-hour degradation, or
        without a CC Switch DB row are skipped (the next eligible
        provider in the chain is tried).
      * The literal ``"parent"`` entry is NEVER returned — it is the
        caller's explicit fallback (the SubagentConfig inherits the
        parent process's CC Switch proxy config when this function
        returns the empty dict).

    Falls back to empty strings (the SubagentConfig defaults) if the
    chain has no eligible provider. Decision 2/3 contract: the
    ClaudeCodingTool must NEVER fall back to the parent process's
    cc-switch proxy endpoint silently — we always pass an explicit
    provider key + base_url.

    Args:
        provider_priority: Ordered list of logical provider IDs from
            :func:`provider_order.load_fallback_order`. May include the
            ``"parent"`` sentinel at the end (skipped silently here).
        tracker: Active concurrency tracker used to check capacity.
            Required — callers must pass the shared
            :class:`ActiveConcurrencyTracker` from the lifespan's
            :class:`backend.runtime_state.RuntimeState.dynamic_tracker`
            so all dispatches in this process observe the same
            slot-count surface. The historical ``None`` fallback to a
            module-level singleton was removed by the TC-006 DI
            refactor; the module-level singleton no longer exists.
        now: Current datetime for vendor-b peak-hour degradation. ``None``
            defers to :func:`datetime.datetime.now`. Tests pass an
            explicit off-peak time to keep the vendor-b rule stable.
        reserve_slot: When True, the returned provider's slot is taken
            **atomically inside this walk** (:meth:`try_acquire`) rather
            than merely observed, and the caller owns it — it must not
            acquire again, and it must release it when the subagent
            finishes. This is the production contract: it closes the
            check-then-act race between this function and the dispatch
            site.

            With the default ``False`` the function is a pure read (the
            historical behaviour, used by tests and by callers that
            manage their own slot) and is not atomic.

    Returns:
        ``{"provider_name", "base_url", "api_key"}``. With
        ``reserve_slot=True`` a non-empty ``provider_name`` means the
        slot is HELD; an empty one means no slot was taken and the
        caller must not release anything.
    """
    empty = {"provider_name": "", "base_url": "", "api_key": ""}

    from cc_switch import (
        CCSwitchError,
        get_provider,
    )
    from provider_order import load_provider_5h_usage

    if tracker is None:
        # Defensive fallback: a caller that omits ``tracker`` (e.g. an
        # ad-hoc test fixture) gets a fresh per-call tracker so the
        # dispatch path stays correct without resurrecting the
        # module-level singleton the TC-006 refactor removed.
        tracker = ActiveConcurrencyTracker()
    if now is None:
        now = datetime.now()

    usage_map = load_provider_5h_usage()
    # Walk the chain manually so a candidate with capacity but no CC
    # Switch DB row falls through to the next eligible provider. This
    # preserves the legacy "no config = skip" semantic at the
    # dispatch site.
    for name in provider_priority:
        # 1. ``parent`` is the caller's explicit fallback; never pick it.
        if name == "parent":
            continue
        # 2. Dynamic capacity: a provider must have spare room under
        #    its 5h-derived cap to be eligible.
        remaining_pct = usage_map.get(name, 100.0)
        from dynamic_provider_concurrency import compute_dynamic_limit

        # The cap comes from the rules in ``provider_capacity.yaml``,
        # matched case-insensitively against the name as the chain spells
        # it and against its canonical form. There is no id table to
        # translate through: a name that matches no rule gets the single
        # documented default, and one name and its kebab-case form reach
        # the same rule.
        limit = compute_dynamic_limit(name, remaining_pct)
        if reserve_slot:
            # Atomic check-and-take. This is the whole point of the flag:
            # ``tracker.current(name) >= limit`` followed by a separate
            # ``acquire()`` at the dispatch site is a TOCTOU race — two
            # dispatchers both read "1 of 10 in flight" and both proceed,
            # and the cap is exceeded by exactly the amount it exists to
            # prevent.
            #
            # The slot is taken BEFORE the CC Switch row is validated, so
            # the "row missing" fall-through below releases it again.
            if not tracker.try_acquire(name, limit):
                continue
        elif tracker.current(name) >= limit:
            continue
        # 4. CC Switch DB gate: must have a row with base_url + api_key.
        #    The lookup returns ``None`` for an unknown name; only an
        #    unreadable database raises. It cannot raise
        #    ``ProviderNotFoundError`` — that class belonged to the
        #    id-keyed reader deleted 2026-09-24 the class no longer
        #    exists anywhere, and keeping its name in this tuple made a
        #    dead except entry look load-bearing.
        try:
            cfg = get_provider(name)
        except (CCSwitchError, ValueError):
            if reserve_slot:
                tracker.release(name)
            continue
        if cfg is None:
            if reserve_slot:
                tracker.release(name)
            continue
        # "Endpoint present but no credential" is a real shape and not a
        # dispatchable one; ``is_dispatchable`` is the single test for it,
        # shared with the current-provider reader. Re-deriving it here is
        # what let the two readers drift apart.
        if cfg.is_dispatchable():
            return {
                "provider_name": name,
                "base_url": cfg.base_url,
                "api_key": cfg.api_key,
            }
        # 2026-09-25: this branch used to fall through *without* releasing
        # the slot, unlike the two above it — a row that exists but has no
        # credential permanently consumed one of the provider's slots for
        # the life of the process (a leaked slot is never re-observed as
        # free). Same fall-through, same reason to give the slot back.
        if reserve_slot:
            tracker.release(name)
    return empty


def select_provider(
    provider_priority: List[str],
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return the first provider in ``provider_priority``.

    The ORDER is the policy: it comes from the optimizer's chain
    (``provider-order.json``), whose rule engine already applied any
    peak-hour demotion. This function adds no ranking of its own.
    """
    if now is None:
        now = datetime.now()
    for provider in provider_priority:
        return provider
    return None


# 2026-09-13: _flatten_model_map_for_subagent REMOVED.
# Model management is delegated entirely to CC Switch — the SubagentConfig
# no longer carries a tiered model_map, and the selected provider row's
# own ANTHROPIC_* model env block is forwarded by the dispatch walk.


# Terminal task statuses (PRD acceptance case 5 / cross-process recovery).
# A task with any of these statuses is excluded from the DAG returned
# by :meth:`AutonomousAgent._load_tasks` so a recovered run does not
# re-execute work that has already completed or definitively failed.
#
# Excluded: ``completed``, ``failed``, ``skipped``.
# NOT excluded: ``pending`` (needs scheduling), ``in_progress`` (a crash
# recovery point that may still finish), ``breakdown_in_progress`` (a
# parent task whose children have just been inserted into tasks.json
# and must remain in the DAG so the children can be scheduled).
#
# PRD decision point 4: ``dependency_failed`` is intentionally NOT in
# this set. The "downstream tasks continue running" rule means there
# is no propagation-driven terminal status: a failed leaf stays
# ``failed`` (and is detected by :func:`_is_plan_failed`) but its
# downstream tasks keep their own lifecycle, not a synthesised
# ``dependency_failed`` label.
_TERMINAL_TASK_STATUSES = frozenset({
    "completed",
    "failed",
    "skipped",
    "superseded",  # 2026-09-08: refiner-orphan residue that
                   # must NOT be re-executed. Set by ``_load_tasks``
                   # when a state.db entry is not in ``tasks.json``
                   # and either has no full fields (so it cannot be
                   # reconciled into the DAG) or is already in a
                   # terminal state (so re-running would re-write
                   # a finished task). See
                   # :meth:`PlanTaskRepository.iter_orphan_tasks`
                   # and the ``task_orphans_reconciled`` audit event.
})

# Orphan-reconcile rules are shared with
# ``TaskManager._hydrate_db_only_orphans`` — the two loaders must agree
# on what counts as mergeable, and they drifted once already (see the
# module docstring of ``orphan_rules``). Re-exported here so existing
# callers/tests that import from ``agent`` keep working.
from orphan_rules import (  # noqa: E402
    MERGE_REQUIRED_FIELDS as _ORPHAN_MERGE_REQUIRED_FIELDS,
    has_mergeable_content as _orphan_has_mergeable_content,
    merged_subtask_kwargs as _orphan_merged_subtask_kwargs,
)


def strip_framework_result_trailer(text: str) -> str:
    """Remove the framework-mandated trailing ``TEST_RESULT:`` block.

    Every executor subagent is instructed to end its report with::

        TEST_RESULT: PASSED
        or
        TEST_RESULT: FAILED
        REASON: <brief explanation>

    (see ``test_instruction`` in ``_execute_task_with_retry``). That
    block is *machine protocol* — the executor parses it to cross-check
    the subagent's claim against the real ``test_command`` exit code. It
    carries no evaluation content.

    The audit second pass is a different consumer: it judges whether the
    answer satisfies the spec's acceptance criteria. Handing it the
    trailer lets a spec clause such as "绝对禁止输出任何自称通过字样"
    turn the framework's own required line into a fatal spec violation —
    the subagent cannot satisfy both, so every attempt fails. Observed
    live on a production plan (2026-09-22): the
    refiner added that prohibition in response to a failure reason
    *mentioning* the trailer, the reviewer then enforced it literally,
    and the task split 10 levels deep before the plan stalled.

    Only a trailer that is genuinely at the END of the report is
    removed; a mid-text mention is left alone (the caller also tells the
    reviewer how to read it).

    ``str`` in, ``str`` out; ``None``/empty passes through unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    lines = text.rstrip().split("\n")
    if lines and re.match(r"^\s*[*_>`\-\s]*REASON\s*:", lines[-1], re.IGNORECASE):
        lines.pop()
    if lines and re.match(
        r"^\s*[*_>`\-\s]*TEST_RESULT\s*:", lines[-1], re.IGNORECASE
    ):
        lines.pop()
    return "\n".join(lines).rstrip()


def is_dependency_ready(task, all_tasks) -> tuple[bool, str]:
    """Decide whether ``task`` is ready to be scheduled, given the
    status of every upstream task referenced in its ``depends_on``.

    Contract — readiness gate (PRD decision point 4 + the new
    scheduler DAG gate):

      * A task with no ``depends_on`` (or an empty ``depends_on``)
        is **ready** by definition — it is a root in the DAG.
      * Otherwise, the task is ready iff **every** entry in
        ``depends_on`` refers to an upstream task whose ``status``
        is in ``{"completed", "skipped"}``. Both terminal-success
        statuses unlock downstream execution; ``failed``,
        ``in_progress``, ``pending`` all block it.
      * If any upstream is in a non-success terminal status, or is
        still pending / in_progress, the function returns
        ``(False, "upstream_<status>:<upstream_id>")``. The reason
        is machine-readable so the dispatcher can surface it via
        the watchdog signal without further parsing.
      * If any ``depends_on`` entry refers to a task that does not
        exist in ``all_tasks`` (defensive guard against a plan-load
        bug), the function returns
        ``(False, "missing_upstream:<upstream_id>")``.
      * Self-dependency (a task references its own id in
        ``depends_on``) is treated as a missing/non-terminal upstream
        rather than a crash — the cycle prevents the task from ever
        observing its own completion.

    Algorithm — O(M + K) where M = ``len(depends_on)`` and
    K = ``len(all_tasks)``:

      * Index ``all_tasks`` by ``id`` in one pass (a dict). K.
      * Walk ``task.depends_on`` in declaration order; on the first
        missing or non-success upstream, return ``(False, reason)``.
      * If every upstream passes, return ``(True, "")``.

    The structured reason is what the dispatcher's per-tick loop
    emits in the watchdog signal when a task is rejected; it is
    intentionally a single token so the operator can grep it.

    :param task: The candidate task (a :class:`SubTask`-shaped
        object). Only ``id`` and ``depends_on`` are read.
    :param all_tasks: The full plan task list. Iterated once to
        build the id → status index.
    :return: ``(ready, reason)``. ``ready`` is ``True`` iff every
        upstream is in ``{"completed", "skipped"}`` and present in
        ``all_tasks``. ``reason`` is the empty string on success,
        or a machine-readable token naming the offending upstream
        (e.g. ``"upstream_failed:1-1"``,
        ``"upstream_in_progress:2"``,
        ``"upstream_pending:3"``,
        ``"missing_upstream:ghost"``) on failure.
    """
    # A missing ``depends_on`` attribute is treated as no upstream
    # (matches the contract used by ``_validate_dependencies`` and
    # ``_build_layers``).
    deps = getattr(task, "depends_on", None) or []

    if not deps:
        return True, ""

    by_id = {t.id: t for t in all_tasks}

    for upstream_id in deps:
        upstream = by_id.get(upstream_id)
        # Self-dependency cycle guard fires regardless of whether
        # the resolver finds children: a task depending on itself
        # is a cycle the upstream task can never break, no matter
        # how many sub-tasks exist.
        if upstream_id == task.id:
            return False, f"missing_upstream:{upstream_id}"

        # Prefix-aware resolver: if the
        # ``upstream_id`` is not present as a literal task id, the
        # refiner may have split the original parent task into a
        # ``<upstream_id>-N`` hierarchy. Expand the dep to cover
        # every child whose id starts with ``upstream_id + "-"``.
        # If the parent id IS present, we still require it to be
        # in a success terminal status — the dep is logically
        # satisfied by the parent running and completing.
        candidates: list = []
        if upstream is not None:
            candidates.append(upstream)
        else:
            # Build children list dynamically — the plan can shrink
            # or grow between dispatcher passes and we want the
            # children match to reflect the current plan state.
            prefix = upstream_id + "-"
            children_matching = [
                t for tid, t in by_id.items()
                if tid.startswith(prefix)
            ]
            if children_matching:
                candidates.extend(children_matching)
            # If no children matched, fall through to the existing
            # missing-upstream error below.

        if not candidates:
            # Missing upstream: id is not in the plan and has no
            # surviving children. The plan-load validator catches
            # this at load time, but the runtime gate must still
            # be safe.
            return False, f"missing_upstream:{upstream_id}"

        # All candidates (parent + every child) must be in a
        # success terminal state. Any failure or non-terminal
        # candidate blocks the downstream task.
        for candidate in candidates:
            candidate_status = getattr(candidate, "status", None)
            if candidate_status in ("completed", "skipped"):
                # Success: keep walking the rest of the candidates
                # for this dep.
                continue
            if candidate_status == "failed":
                return False, f"upstream_failed:{candidate.id}"
            if candidate_status == "in_progress":
                return False, f"upstream_in_progress:{candidate.id}"
            if candidate_status == "pending":
                return False, f"upstream_pending:{candidate.id}"
            # Any other status (e.g. ``breakdown_in_progress``, a
            # non-standard label introduced by a future feature) is
            # treated as "not yet ready" with a generic token so
            # the dispatcher can surface it without a special case.
            return False, f"upstream_not_ready:{candidate.id}"

    return True, ""


def _is_plan_failed(tasks: list) -> bool:
    """Return True if any leaf task in the input is in ``status="failed"``.

    PRD decision point 4 — plan-level failure detection contract:

      * Failure does NOT propagate to downstream tasks. A failed leaf
        stays ``failed`` but its downstream tasks keep their own
        lifecycle (``pending`` → ``in_progress`` → terminal). The
        earlier ``_propagate_dependency_failure`` helper, which marked
        every downstream task ``dependency_failed`` when an upstream
        leaf failed, has been removed: this function is the entire
        plan-failure surface, and the caller (the executor's main
        loop) decides how to interpret the result.
      * The plan as a whole is considered FAILED iff at least one
        leaf task is in ``status="failed"``. Non-leaf failures
        (parents whose children collapsed to ``failed`` via
        :meth:`AutonomousAgent._aggregate_breakdown_verdict`) are
        already represented as leaf failures once the aggregation
        rolls them up, so a leaf-only scan is the correct lens.
      * The function MUST be called only on a terminal snapshot of
        the task list — i.e. every task is either terminal
        (``completed``/``failed``/``skipped``) or about to be
        skipped. The dispatcher in :meth:`AutonomousAgent._run_async`
        is the only intended caller; calling it mid-scheduling (when
        some tasks are still ``in_progress`` or ``pending``) would
        return a meaningless answer. The signature is intentionally
        permissive (no enforcement) so unit tests can drive the
        function with synthesised lists.

    Algorithm — O(N) single pass over the input:

      * Iterate ``tasks`` exactly once.
      * For each task, check ``status == "failed"`` AND that the task
        is a leaf in the breakdown tree (no other task has an id
        starting with ``task.id + "-"``).
      * Return True on the first match; False after the loop.

    Edge cases (mirrored by ``tests/unit/test_agent_plan.py``):

      * Empty input → False. There is no failure if there are no
        tasks.
      * All leaves ``completed`` → False. The plan passed.
      * Any leaf ``failed`` → True. The plan failed.
      * Non-leaf ``failed`` (parent whose own status is ``failed``
        but whose children exist) — the function looks ONLY at
        leaves, so the parent failure is NOT counted. This is
        intentional: a non-leaf ``failed`` is a transient shape
        that the breakdown/aggregation flow will resolve. By the
        time the caller invokes this function, the leaves
        carry the verdict.

    Args:
        tasks: List of :class:`SubTask` objects (or any object with
            ``.id`` and ``.status`` attributes). Read-only.

    Returns:
        bool: True iff at least one leaf task in the input has
            ``status == "failed"``.
    """
    if not tasks:
        return False
    all_ids = {t.id for t in tasks}
    for t in tasks:
        if t.status != "failed":
            continue
        if not any(other.startswith(t.id + "-") for other in all_ids):
            return True
    return False


# Maximum number of times a single parent task may be broken down into
# subtasks before the executor stops re-breaking it and marks it failed.
# PRD decision point: at depth 5 the executor has tried four prior
# breakdowns; if the fifth still fails to converge, automated breakdown
# is unlikely to help and human inspection is required. See
# :meth:`AutonomousAgent._breakdown_task` for the enforcement site.
EXECUTOR_BREAKDOWN_MAX = 5


def _filter_terminal_tasks(tasks: list[SubTask]) -> tuple[list[SubTask], int]:
    """Partition tasks into ``(active, terminal_count)`` by terminal status.

    Pure helper backing the cross-process recovery contract in
    :meth:`AutonomousAgent._load_tasks` (PRD acceptance case 5). Tasks
    whose ``status`` is in :data:`_TERMINAL_TASK_STATUSES` are
    considered already finished and are NOT included in the returned
    active list; their count is returned separately so the caller can
    log / report it without a second pass.

    Status semantics (mirror :data:`_TERMINAL_TASK_STATUSES`):

      * ``completed`` / ``failed`` / ``skipped`` → **terminal**, excluded
        from the active list.
      * ``pending`` / ``in_progress`` / ``breakdown_in_progress`` →
        **active**, kept.
      * ``breakdown_in_progress`` is intentionally not terminal: when a
        task is being broken down into subtasks, the children have
        already been written to ``state.db`` — by the refiner, which owns
        that write since the dispatcher's own split path was removed —
        and ``_load_tasks`` reconciles them into the DAG on the next
        load, so they must remain reachable from :func:`_build_layers`.

    Edge cases (observable in ``tests/unit/test_agent_load.py``):

      * Empty input → ``([], 0)``.
      * Every task terminal → ``([], N)``.
      * Every task active → ``(tasks, 0)`` (preserves input order).
      * Mixed → active tasks in input order, count of terminals only.

    Args:
        tasks: List of :class:`SubTask` objects. Read-only — the input
            list is never mutated.

    Returns:
        ``(active_tasks, terminal_count)`` where ``active_tasks`` is a
        fresh list of non-terminal :class:`SubTask` objects in their
        original input order, and ``terminal_count`` is the integer
        number of tasks excluded.
    """
    active: list[SubTask] = []
    terminal_count = 0
    for task in tasks:
        if task.status in _TERMINAL_TASK_STATUSES:
            terminal_count += 1
        else:
            active.append(task)
    return active, terminal_count


def _compute_leaf_tasks(tasks: list[SubTask]) -> set[str]:
    """Return the set of task ids that have no breakdown children.

    A task is a **leaf** in the breakdown tree iff no other task in
    the input has an id that starts with ``task.id + "-"``. This is
    the "bottom of the breakdown tree" view: a task with no children
    is a leaf, regardless of its own status.

    Used by :meth:`AutonomousAgent._load_tasks` to initialise
    ``self._leaf_tasks`` from the on-disk task list. The
    breakdown / aggregation flows maintain the set incrementally
    (discard parent / add children, then discard children / add
    parent) so the cost of recomputing it from scratch only happens
    at load time.

    Pure helper — the input list is never mutated.

    Edge cases:

      * Empty input → empty set.
      * Every task has children → empty set (impossible in a valid
        plan, but the helper is tolerant).
      * Single task with no children → ``{id}``.
      * Mixed: parent + 2 children → ``{child1_id, child2_id}`` (the
        children are leaves, the parent is not).

    Args:
        tasks: List of :class:`SubTask` objects. Read-only.

    Returns:
        A fresh ``set[str]`` of leaf task ids (sets are unordered).
    """
    if not tasks:
        return set()
    all_ids = {t.id for t in tasks}
    leaves: set[str] = set()
    for tid in all_ids:
        if not any(other.startswith(tid + "-") for other in all_ids):
            leaves.add(tid)
    return leaves


def _build_layers(tasks: list) -> list:
    """Build a sequence of parallel-execution layers from a task list.

    Each *outer* layer is a topological batch — every task in outer
    layer ``N+1`` depends only on tasks in outer layers ``0..N``.
    Within one outer layer, tasks are further grouped into *micro
    layers*: tasks that modify disjoint files run concurrently in the
    same micro layer, while tasks that share a ``files_to_modify``
    entry are serialised across separate micro layers. This prevents
    concurrent agents from overwriting the same source file.

    Sibling-merge (sibling subtasks under the same ``parent_id``):

      Sibling subtasks — tasks whose id shares a parent prefix (e.g.
      ``"1-1"``, ``"1-2"``, ``"1-3"`` all roll up to ``"1"``) — are
      intentionally allowed to run in the same outer layer when they
      have no *cross-parent* data dependency. Without this, a chain of
      sibling subtasks (``1-1`` -> ``1-2`` -> ``1-3`` ...) would each
      land in its own outer layer, and ``asyncio.gather`` would end
      up running one coroutine at a time — defeating the whole point
      of parallel execution.

      The merge rule:

        * Sibling group = tasks whose id splits on ``-`` to a common
          parent prefix (and that prefix also exists as a task id, or
          the task itself declares ``parent_id`` matching it).
        * Sibling subtasks are scheduled in the same outer layer as
          the parent task, immediately after the parent, provided
          none of them has a cross-parent ``depends_on`` (i.e. every
          dependency stays within the sibling group).
        * Within the merged layer, subtasks are emitted in
          ``in_degree`` ascending order so the start order is
          deterministic and respects whatever minimal ordering their
          declared ``depends_on`` chain implies.

    Micro-layer construction (per outer layer):

      1. Build an undirected conflict graph where two tasks are
         adjacent if they share any ``files_to_modify`` entry.
      2. Tasks that declare the unknown-modification sentinel
         (``["__UNKNOWN_MODIFICATIONS__"]``) do NOT conflict with each
         other — the sentinel means "files unknown", not "touches every
         file" — so they are coalesced into the shared singleton micro
         layer and run concurrently (fixed 2026-08-19: previously they
         were serialised, killing all parallelism for sentinel plans).
      3. Each connected component of the conflict graph is serialised
         into one micro layer per task, preserving input order.
      4. Single-task components are concatenated into a single micro
         layer so non-conflicting tasks still run concurrently.

    Implemented with Kahn's algorithm (BFS variant, ``O(V + E)``) for
    the outer dependency layers, plus a linear conflict-graph traversal
    for the micro layers.

    Edge cases:

      * Empty input returns ``[]`` so callers can iterate uniformly.
      * A task missing the ``depends_on`` attribute is treated as having
        no upstream constraints (the field is opt-in).
      * When the input forms a cycle, the cycle members never reach
        in-degree 0 and are absent from every emitted layer.

    Args:
        tasks: List of ``SubTask`` objects. Each may carry
            ``depends_on`` (list of task ids) and ``files_to_modify``
            (list of relative file paths); both attributes are optional.

    Returns:
        list[list[list[SubTask]]] — ``outer layer → micro layer → tasks``.
        The outer list is non-empty; each micro layer contains zero or
        more ``SubTask`` references. Cycle residuals never appear inside
        the returned layers.
    """
    if not tasks:
        return []

    by_id: dict = {}
    in_degree: dict = {}
    reverse: dict = {}

    for task in tasks:
        by_id[task.id] = task
        in_degree[task.id] = 0
        reverse[task.id] = []

    # First pass: resolve depends_on, count in-degree, and build the
    # reverse adjacency (downstream set). Deps that point to ids not
    # present in the input are kept in in_degree — they can never be
    # satisfied, so the dependent task ends up as a cycle residual.
    for task in tasks:
        deps = getattr(task, "depends_on", None) or []
        in_degree[task.id] = len(deps)
        for dep_id in deps:
            if dep_id in reverse:
                reverse[dep_id].append(task.id)

    # Sibling-merge: pre-compute for each task its sibling group and
    # whether it is "internally-dependent" (i.e. all depends_on ids are
    # within the same sibling group, or the task has no depends_on at
    # all). Siblings with cross-parent deps are excluded from the
    # merge so real data dependencies still serialise across layers.
    sibling_parent: dict = {}  # tid -> parent prefix (str) | None
    sibling_group: dict = {}  # parent -> [tid, ...]
    for task in tasks:
        parent = _infer_parent_id(task, by_id)
        sibling_parent[task.id] = parent
        if parent is not None:
            sibling_group.setdefault(parent, []).append(task.id)

    def _has_only_sibling_deps(tid: str) -> bool:
        deps = getattr(by_id[tid], "depends_on", None) or []
        parent = sibling_parent.get(tid)
        if parent is None:
            return False
        return all(dep in sibling_group.get(parent, set()) for dep in deps)

    # Kahn outer-layer construction with sibling-merge tail attachment.
    outer_layers: list[list] = []
    current: list = [tid for tid, deg in in_degree.items() if deg == 0]

    if not current:
        # Every task has at least one unresolved dep — a pure cycle
        # (or all-dangling-deps). Emit a single empty outer layer so the
        # result is non-empty; cycle members are absent from it.
        return [[[]]]

    # Track which siblings have already been emitted so we don't
    # double-schedule them.
    scheduled: set = set()

    # Siblings deferred because their parent hasn't been scheduled yet.
    # A sibling task can reach in_degree 0 in iteration 1 even though
    # its parent won't be ready until a later layer (e.g. "1-5" with no
    # deps while "1" depends on "0"). Deferring prevents the merge tail
    # from prematurely collapsing the sibling group into layer[0].
    deferred: list = []

    while current or deferred:
        # Emit the in_degree-zero tasks of this outer layer. Tasks
        # that are themselves the parent of a sibling group are kept
        # in the layer; their sibling subtasks are pulled in as a
        # merged tail (only when those siblings depend only inside
        # the group and have not been scheduled elsewhere).
        layer_tids: list = []
        next_deferred: list = []
        for tid in current:
            if tid in scheduled:
                continue
            parent = sibling_parent.get(tid)
            # If this is a sibling whose parent has not been scheduled
            # yet, defer it for a later iteration when the parent (or a
            # sibling that triggers the merge) arrives. Without this,
            # a sibling with no depends_on would land in the wrong
            # outer layer — before its parent — and the merge tail
            # would pull in the entire group prematurely.
            if parent is not None and parent not in scheduled:
                next_deferred.append(tid)
                continue
            layer_tids.append(tid)
            scheduled.add(tid)
            # Trigger sibling-merge: pull in all mergeable siblings of
            # ``tid``'s parent group. This fires whether ``tid`` is
            # the parent itself or another sibling that just got
            # scheduled (so cross-iteration merges work for chains).
            if parent is not None:
                siblings = [
                    sid for sid in sibling_group.get(parent, [])
                    if sid != tid and sid not in scheduled
                ]
                mergeable = [sid for sid in siblings if _has_only_sibling_deps(sid)]
                if not mergeable:
                    continue
                # Emit mergeable siblings in ascending in_degree so
                # the start order is deterministic. Sort by
                # (in_degree, id) to break ties stably.
                mergeable.sort(key=lambda s: (in_degree.get(s, 0), s))
                for sid in mergeable:
                    layer_tids.append(sid)
                    scheduled.add(sid)

        # Re-check deferred siblings: any whose parent got scheduled
        # in this iteration can now be emitted (and will trigger the
        # merge tail with the rest of the group). Re-defer the rest.
        for tid in deferred:
            if tid in scheduled:
                continue
            parent = sibling_parent.get(tid)
            if parent is not None and parent not in scheduled:
                next_deferred.append(tid)
                continue
            layer_tids.append(tid)
            scheduled.add(tid)
            if parent is not None:
                siblings = [
                    sid for sid in sibling_group.get(parent, [])
                    if sid != tid and sid not in scheduled
                ]
                mergeable = [sid for sid in siblings if _has_only_sibling_deps(sid)]
                if not mergeable:
                    continue
                mergeable.sort(key=lambda s: (in_degree.get(s, 0), s))
                for sid in mergeable:
                    layer_tids.append(sid)
                    scheduled.add(sid)

        if layer_tids:
            outer_layers.append(_build_micro_layers([by_id[tid] for tid in layer_tids]))

        # Compute nxt from ALL emitted tids (current items + merged
        # siblings), so downstream tasks of merged siblings are
        # correctly enqueued for the next outer layer.
        nxt: list = []
        for tid in layer_tids:
            for downstream_id in reverse.get(tid, []):
                in_degree[downstream_id] -= 1
                if in_degree[downstream_id] == 0:
                    nxt.append(downstream_id)
        # Drop downstream ids that were already merged into this or an
        # earlier outer layer (e.g. siblings pulled in via the merge
        # tail), otherwise the Kahn loop emits a trailing empty outer
        # layer for the residual zero-degree set.
        nxt = [d for d in nxt if d not in scheduled]
        current = nxt
        deferred = next_deferred

    return outer_layers


def _infer_parent_id(task, by_id: dict):
    """Infer a task's parent_id from its id, if any.

    A task is a sibling subtask iff its id contains ``-`` and the
    prefix before the final ``-`` segment exists as another task id
    in the same input. For example, ``"1-2"`` rolls up to ``"1"``
    when ``"1"`` is itself a task.

    Returns the parent id (``str``) or ``None`` when the task has no
    inferred parent (root task, or input lacks the parent entirely).
    """
    task_id = getattr(task, "id", None)
    if not isinstance(task_id, str) or "-" not in task_id:
        return None
    prefix = task_id.rsplit("-", 1)[0]
    if prefix in by_id:
        return prefix
    return None


#: Section of a task description that describes deliverables. Same shape
#: and same rationale as ``TasksGenerator._MOD_SECTION_RE`` /
#: ``_extraction_scope`` in ``backend/tasks_generator.py`` — keep the two
#: in lock-step: the generator scopes its ``files_to_modify`` backfill
#: with this regex, and this module scopes its description fallback with
#: it. Prose outside the section (背景 / 输入示例 / 边界条件) routinely
#: *cites* files the task will not touch; reading those as write claims
#: is what collides with the conflict graph and serialises a whole layer (see
#: :func:`_declared_modification_files`).
_MOD_SECTION_RE = re.compile(r"^##\s*修改位置\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)


def _modification_section(description: str) -> str:
    """Text the description-based file extractors are allowed to scan.

    Falls back to the whole description when the ``## 修改位置`` section
    is absent, preserving the behaviour of descriptions written before
    the section convention.
    """
    m = _MOD_SECTION_RE.search(description or "")
    return m.group(1) if m else (description or "")


def _declared_modification_files(task) -> Optional[list[str]]:
    """The files a task declares it will modify, or ``None`` if unknown.

    Single source of truth for "which files does this task touch". Both
    the layer planner (:func:`_build_micro_layers`) and the runtime
    conflict guard (``AutonomousAgent._extract_target_files``) read it,
    so the two can no longer disagree about whether two tasks conflict.

    ``None`` means the declaration is unusable — ``files_to_modify`` is
    absent or carries one of the two sentinels. Callers decide what that
    implies: the planner gives the task a private conflict key, the
    runtime guard falls back to scanning the description.

    Why this exists (2026-09-23): the planner keyed on
    ``files_to_modify`` while the runtime guard re-derived the same set
    from a regex sweep over the *whole* description. A task that merely
    cited ``native_ext/tests/lifecycle.rs`` as an existing
    artefact it would not touch — its description said the file was only
    being checked against, not rewritten — was read as claiming it, so
    the planner correctly put tasks 1 and 2 in one
    micro layer — their declared files do not overlap — and the runtime
    guard then failed task 2 with "File conflict detected" for a claim
    task 1 never made. The guard fired on exactly the pair the planner
    had deliberately parallelised.
    """
    files = getattr(task, "files_to_modify", None) or []
    if (
        files == UNKNOWN_MODIFICATIONS_SENTINEL
        or files == NO_FILE_CHANGES_SENTINEL
    ):
        return None
    return list(files)


def _build_micro_layers(layer_tasks: list) -> list:
    """Split one dependency layer into serialised micro layers.

    Tasks that share a ``files_to_modify`` entry are placed in the same
    conflict component and serialised across individual micro layers.
    Tasks whose ``files_to_modify`` is the unknown-modification sentinel
    do NOT conflict with each other (sentinel = "files unknown", not
    "touches everything"), so they coalesce into the shared singleton
    micro layer and run in parallel. Non-conflicting tasks (including
    single-task components) are coalesced into one micro layer so they
    can run concurrently.

    Args:
        layer_tasks: Tasks belonging to one outer dependency layer,
            in input order.

    Returns:
        list[list[SubTask]] — micro layers for this outer layer.
    """
    if not layer_tasks:
        return [[]]

    # Index each task's modification files, treating the unknown
    # sentinel as a private conflict key so unknown tasks serialise
    # with each other but not with known-file tasks.
    #
    # 2026-08-19 fix (an earlier audit): the unknown-modification
    # sentinel previously mapped EVERY sentinel task to the SAME shared
    # key ``__UNKNOWN_MODIFICATIONS__``, so all sentinel tasks collided
    # into one connected component and were serialised into per-task
    # micro layers (``layer_size: 1``). A plan whose tasks all carry the
    # sentinel (the common case after the step-3 validator forced a
    # sentinel backfill) therefore lost ALL parallelism. The sentinel
    # means "the exact files are not yet known", NOT "this task touches
    # every file" — two unknown-file tasks do not inherently conflict.
    # Give each sentinel task a UNIQUE synthetic key so sentinel tasks
    # no longer conflict with each other and are coalesced into the
    # shared singleton micro layer (i.e. run in parallel). Genuine
    # conflicts are still detected via concrete shared file paths.
    #
    # 2026-09-09 two-constant scheme update: BOTH sentinel constants
    # (``__UNKNOWN_MODIFICATIONS__`` and ``__NO_FILE_CHANGES__``) are
    # non-real-path markers — neither contributes a file-conflict key.
    # Each task with any sentinel value gets a unique synthetic key
    # so it can run in parallel with other sentinel tasks.
    task_files: list[tuple[int, SubTask, frozenset[str]]] = []
    for idx, task in enumerate(layer_tasks):
        declared = _declared_modification_files(task)
        if declared is None:
            # Unique per-task key ⇒ sentinel tasks never collide
            # with each other, so they batch into one parallel micro
            # layer instead of serialising.
            keys = frozenset([f"__SENTINEL__#{task.id}"])
        else:
            keys = frozenset(declared)
        task_files.append((idx, task, keys))

    # Build an undirected conflict graph: edge between tasks that share
    # at least one file key.
    n = len(layer_tasks)
    adjacency: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if task_files[i][2] & task_files[j][2]:
                adjacency[i].add(j)
                adjacency[j].add(i)

    # Find connected components via DFS/BFS, preserving input order.
    visited = [False] * n
    components: list[list[int]] = []

    for start in range(n):
        if visited[start]:
            continue
        stack = [start]
        visited[start] = True
        component: list[int] = []
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if not visited[neighbor]:
                    visited[neighbor] = True
                    stack.append(neighbor)
        components.append(component)

    micro_layers: list[list[SubTask]] = []
    pending_singletons: list[SubTask] = []

    for component in components:
        # Sort by original input order so the component reflects the
        # order the tasks appeared in the input.
        component.sort()
        if len(component) == 1:
            # Single-task component: batch with other singletons so they
            # can run concurrently in one micro layer.
            pending_singletons.append(task_files[component[0]][1])
        else:
            # Multi-task component: flush any pending singletons first
            # (they are independent of this component), then serialize
            # the component into individual micro layers.
            if pending_singletons:
                micro_layers.append(pending_singletons)
                pending_singletons = []
            for node in component:
                micro_layers.append([task_files[node][1]])

    # Flush trailing singletons — sorted by task id so the within-micro-
    # layer order is deterministic and does not depend on input order.
    if pending_singletons:
        pending_singletons.sort(key=lambda t: t.id)
        micro_layers.append(pending_singletons)

    return micro_layers


def _validate_dependencies(tasks: list[SubTask]) -> None:
    """Fail-fast validation of task ``depends_on`` declarations.

    PRD decision point 5: ``tasks.json`` must be rejected at load time
    if it contains any of three illegal dependency shapes. The check is
    fail-fast — the first violation aborts the load with a
    ``ValueError`` whose message pinpoints the offending ids, so a
    malformed plan never reaches the executor.

    Three classes of violation, checked in this order:

      1. **Missing dependency** — a task's ``depends_on`` references a
         task id that does not exist in the input list. Reporting
         format: ``"Task A depends on missing task Z"``.
      2. **Self-dependency** — a task's ``depends_on`` includes its own
         id. Reporting format: ``"Task B cannot depend on itself"``.
      3. **Cycle** — the dependency graph contains a real cycle: a
         strongly-connected component of size ≥ 2, or a self-loop.
         Reporting format: ``"Cycle detected: A, B, C"`` (sorted,
         comma-separated). Computed with Tarjan SCC
         (:func:`framework.task_graph.find_cycle_groups`), so nodes
         that are merely *blocked* — downstream of a cycle — are not
         named as members. The earlier Kahn-residual implementation
         conflated the two and reported the whole downstream cone.

    A valid DAG returns silently.

    Edge cases (mirroring :func:`_build_layers`):

      * An empty input is a valid DAG (nothing to validate).
      * A task missing the ``depends_on`` attribute is treated as a
        root (the field is opt-in).
      * A single task with no dependencies is a valid DAG.

    Args:
        tasks: List of ``SubTask`` objects. Each may carry a
            ``depends_on`` attribute (list of task ids).

    Raises:
        ValueError: If the dependency graph is malformed. The message
            identifies the violation (missing / self / cycle) and the
            ids involved so the operator can locate the bad task
            quickly in ``tasks.json``.
    """
    if not tasks:
        return

    # 1) Missing dependencies — every depends_on id must resolve to a
    #    task that exists in the input. Raised as the dedicated
    #    ``DanglingTaskDependency`` rather than a bare ``ValueError``
    #    (2026-09-21) so callers can tell it apart from a cycle and
    #    REPAIR it — strip the edge — instead of refusing to load the
    #    plan. A reference to a task that no longer exists carries no
    #    ordering information, so dropping it is strictly better than
    #    stranding every task behind it.
    dangling = find_dangling_references(tasks)
    if dangling:
        raise DanglingTaskDependency(dangling)

    # 2) Self-dependencies — a task must not appear in its own
    #    depends_on list. This is a separate pass from (1) so the
    #    error message is specific ("cannot depend on itself") rather
    #    than generic.
    for task in tasks:
        deps = getattr(task, "depends_on", None) or []
        if task.id in deps:
            raise ValueError(
                f"Task {task.id} cannot depend on itself"
            )

    # 3) Cycle detection over strongly-connected components.
    #
    # 2026-09-21: this used to be Kahn's residual, which answers "which
    # nodes can never be scheduled?" — a superset of "which nodes are in
    # a cycle". It over-reported two ways: nodes merely *downstream* of
    # a cycle were named as members, and a node whose depends_on
    # referenced a missing id silently never surfaced (the in-degree was
    # counted from len(deps) while the reverse adjacency was only built
    # for ids that exist). A production plan died on
    # the second shape — one stale reference to a split parent was
    # reported as an 18-node cycle. Tarjan SCC names only the real
    # members; ``framework.task_graph`` is shared with
    # ``task_manager._ensure_acyclic`` so the two cannot drift.
    from framework.task_graph import find_cycle_groups

    groups = find_cycle_groups(tasks)
    if groups:
        cycle_members = sorted({tid for group in groups for tid in group})
        raise ValueError(
            f"Cycle detected: {', '.join(cycle_members)}"
        )


# Pattern shared with :func:`TasksGenerator._postprocess_extract_from_desc`
# in ``backend/tasks_generator.py``. The two helpers must stay in lock-
# step: every task id parsed from a desc by the generator's post-processor
# must be checked against ``depends_on`` by this validator. The trigger
# accepts any of the three Chinese phrasings the system prompt template
# asks the LLM to emit: "前置条件", "依赖", "前置条件/依赖".
_DESC_TRIGGER_RE = re.compile(r"(?:前置条件\s*/\s*依赖|前置条件|依赖)\s*[：:]")
# Hierarchical-id matcher — same shape as the generator: ``\d+(?:-\d+)*``
# so flat ("1") and hierarchical ("1-2", "1-2-3") ids are captured whole
# rather than split into fragments.
_DESC_ID_RE = re.compile(r"\d+(?:-\d+)*")


def _extract_desc_dep_ids(description: str) -> list[str]:
    """Extract prerequisite task ids mentioned in a task's ``description``.

    Mirror of :func:`TasksGenerator._postprocess_extract_from_desc` —
    the generator runs this logic to inject ``depends_on`` from the
    desc when the LLM omits the field, and the validator runs the
    same logic to enforce that the desc and the data agree. Keeping
    the two parsers byte-identical is essential: a divergence would
    silently let inconsistencies slip through the load-time check.

    Recognised patterns (in priority order):

      - ``前置条件：任务 1 已完成。``
      - ``前置条件：任务 1-2 已完成。``
      - ``依赖：任务 1 已完成``
      - ``前置条件/依赖：已完成 任务 1-2。``
      - ``前置条件：任务 1, 2, 3 已完成``

    Returns the list of task ids parsed from the description, e.g.
    ``['1-2']`` for ``"前置条件：任务 1-2 已完成"``. Returns ``[]``
    when no recognised trigger word is found or no task id can be
    parsed.

    Boundary cases (mirroring the generator):

      * ``description`` is empty / ``None`` / non-string -> ``[]``
      * description has the trigger word but no ``任务`` keyword
        after it -> ``[]``
      * description has the trigger + ``任务`` keyword but no
        parseable id (e.g. just trailing punctuation) -> ``[]``
    """
    if not description or not isinstance(description, str):
        return []

    trigger_match = _DESC_TRIGGER_RE.search(description)
    if not trigger_match:
        return []

    after_trigger = description[trigger_match.end():]
    boundary = re.search(r"[。\n;；]", after_trigger)
    if boundary:
        after_trigger = after_trigger[:boundary.start()]

    task_kw_idx = after_trigger.find("任务")
    if task_kw_idx < 0:
        return []
    ids_section = after_trigger[task_kw_idx + len("任务"):]

    return _DESC_ID_RE.findall(ids_section)


def validate_desc_consistency(tasks: list) -> None:
    """Reject plans whose ``desc`` mentions a prerequisite absent from the
    **graph-reachable** dependency set.

    PRD decision point 5 hardening: even after the post-processing
    fallback that injects ``depends_on`` from ``desc`` whenever the
    LLM omits the field, the planner can still drift. The LLM may
    emit a non-empty ``desc`` line like ``前置条件：任务 1 已完成``
    but supply a ``depends_on`` that does NOT include ``"1"``. The
    result is a plan whose prose and whose data disagree — the
    layer builder walks the data path and ignores the desc, so the
    task is scheduled before the prerequisite finishes.

    Graph-aware comparison (smoke v4 finding):

      The previous validator only checked ``dep_id in depends_on``
      (direct edge). This was too strict: when ``task_3`` has
      ``depends_on=["2"]`` and ``task_2`` has ``depends_on=["1"]``,
      ``task_3`` is *transitively* dependent on ``1`` via ``2``.
      A desc that names both ``1`` and ``2`` is consistent with the
      graph — it is just a complete description of the upstream
      chain, not a new declaration.

      The check therefore compares against the **transitive closure**
      of ``depends_on`` (memoised DFS over the directed graph),
      so ``task_3`` with ``depends_on=["2"]`` is allowed to mention
      both ``"1"`` and ``"2"`` in its desc.

    The check is fail-fast. The first inconsistency aborts the load
    with a ``ValueError`` whose message names both the offending
    task id and the missing dependency, so a malformed plan never
    reaches the executor.

    Three classes of input (per the task spec):

      1. **desc has no trigger word + ``depends_on == []``** —
         a root task whose description does not mention any
         prerequisite. This is the happy path; the validator
         returns silently.
      2. **desc mentions prerequisites, all in the transitive closure
         of ``depends_on``** — the graph has already declared every
         prerequisite the desc hints at (directly or transitively).
         The validator returns silently.
      3. **desc mentions a prerequisite absent from the transitive
         closure of ``depends_on``** — ``ValueError`` is raised.

    Edge cases (mirroring the generator's parser):

      * An empty input is trivially consistent (nothing to check).
      * A task with no ``depends_on`` attribute is treated as a root
        (``[]``) — the field is opt-in.
      * Hierarchical ids (``1-2``, ``1-2-3``) are compared as a
        whole, not as fragments. A desc that says
        ``前置条件：任务 1-2`` requires ``"1-2"`` (or a transitive
        ancestor that itself is a prefix? — see test 4) to be
        reachable from the task. The current implementation only
        treats the exact id as satisfiable; partial-id matching
        would be ambiguous and is intentionally rejected.

    Args:
        tasks: List of :class:`SubTask` objects. Each may carry a
            ``description`` (string) and ``depends_on`` (list of
            task ids).

    Raises:
        ValueError: If any task's ``desc`` mentions a task id that
            is not reachable (transitively) from its declared
            ``depends_on`` graph. The error message identifies the
            offending task id and the missing dependency.
    """
    if not tasks:
        return

    # Build the by-id map once for the closure walks.
    by_id = {getattr(t, "id", None): t for t in tasks if getattr(t, "id", None) is not None}
    closure_cache: dict = {}

    for task in tasks:
        description = getattr(task, "description", "") or ""
        depends_on = set(getattr(task, "depends_on", None) or [])
        # Self-reference in depends_on is already caught by
        # ``_validate_dependencies`` above; we still want to
        # accept it here so the message is specific to the desc
        # inconsistency, not a self-dependency. Therefore the
        # check is purely a closure comparison.
        desc_dep_ids = _extract_desc_dep_ids(description)
        if not desc_dep_ids:
            continue
        reachable = _transitive_deps(getattr(task, "id", None), by_id, closure_cache)
        for dep_id in desc_dep_ids:
            if dep_id not in reachable:
                raise ValueError(
                    f"Task {task.id} desc 提到前置条件 {dep_id} "
                    f"但 depends_on 是 {sorted(depends_on)} "
                    f"(transitive 闭包 = {sorted(reachable)})"
                )


def _transitive_deps(
    task_id: Optional[str],
    by_id: dict,
    cache: dict,
    _path: Optional[set] = None,
) -> set:
    """Return the transitive closure of upstream task ids for ``task_id``.

    The set includes the direct ``depends_on`` entries plus every
    upstream task id reachable through any number of edges. Memoised
    to avoid re-walking the graph for siblings that share ancestors.

    ``task_id`` may be ``None`` or absent from ``by_id`` — in which
    case an empty set is returned and cached, so the caller's
    ``missing`` check is well-defined.

    Self-references (``task_id`` appears in its own ``depends_on``)
    and other cycles are tolerated by the closure walker: a node
    already on the current recursion path is not re-expanded, so
    the walk always terminates. Cycle detection is the job of
    ``_validate_dependencies`` (a separate, layer-builder-side
    check), not this one.
    """
    if task_id in cache:
        return cache[task_id]
    task = by_id.get(task_id)
    if task is None:
        cache[task_id] = set()
        return set()
    if _path is None:
        _path = set()
    if task_id in _path:
        # Cycle guard — return the current path so the caller sees
        # the ids already accounted for, but don't recurse further.
        # This terminates the walk in O(V) per cache miss.
        return set(_path)
    _path = _path | {task_id}
    direct = set(getattr(task, "depends_on", None) or [])
    result: set = set(direct)
    for d in direct:
        result |= _transitive_deps(d, by_id, cache, _path)
    cache[task_id] = result
    return result


#: ``task_group`` prefix the verification repair loop stamps on the tasks
#: it generates. Kept at module level so :func:`is_repair_task` can be
#: used without an ``AutonomousAgent`` instance — the audit classifier is
#: exercised against lightweight stub agents in the test suite.
REPAIR_TASK_GROUP_PREFIX = "repair"

#: Legacy id prefix for repair tasks, from the pre-v9 ``RP-*`` scheme.
_REPAIR_TASK_ID_PREFIXES = ("repair-", "RP-")


def is_repair_task(task: object) -> bool:
    """True when ``task`` was generated by the verification repair loop.

    2026-09-20 (post-mortem). Repair tasks must never be treated as
    audit-style (see :meth:`AutonomousAgent._looks_like_audit_task`), so
    this predicate has to be usable from anywhere — including from a
    partial agent stub, which is why it is a module function rather than
    a method.

    Two signals, deliberately, because neither alone is sufficient:

    * **The id prefix.** Some repair rows carry ``task_group=None``, and
      those slip through the group check alone. The id scheme
      (``repair-r{N}-{seq}`` now, ``RP-*`` before v9) is the only field
      every repair task has always carried.
    * **``task_group``**, via :func:`refiner_structure.is_protected` so
      there is one definition of "what is a repair task" shared with the
      refiner's protection guard.
    """
    task_id = str(getattr(task, "id", "") or "")
    if task_id.startswith(_REPAIR_TASK_ID_PREFIXES):
        return True
    from refiner_structure import is_protected

    dump = getattr(task, "model_dump", None)
    if not callable(dump):
        return False
    try:
        return is_protected(dump(), REPAIR_TASK_GROUP_PREFIX)
    except Exception:  # noqa: BLE001 — classification must never abort a task
        return False


class AutonomousAgent:
    """Configurable autonomous agent for various task types."""

    def __init__(
        self,
        requirement: Optional[str],
        project_dir: Path,
        coding_tool: CodingTool,
        config: Optional[AgentConfig] = None,
        logger: Optional[ExecutionLogger] = None,
        tasks_file: Optional[Path] = None,
        verif_repo: Optional[object] = None,
        guard: Optional[object] = None,
        dispatcher: Optional[object] = None,
        runtime_state: Optional[object] = None,
        in_flight_guard: Optional[object] = None,
        dynamic_tracker: Optional[ActiveConcurrencyTracker] = None,
    ):
        self.requirement = requirement
        self.project_dir = project_dir
        self.coding_tool = coding_tool
        self.config = config or ConfigRegistry.get('coding')
        self.logger = logger

        # Task-11 subagent verdict contract: when a VerificationRepository
        # handle is provided, the executor's subagent callback chain
        # routes per-VP verdicts through ``verif_repo.append_verdict``
        # (a single BEGIN IMMEDIATE + COMMIT) and produces zero
        # ``verification_*_state.json`` files.  The repository is
        # process-shared and lives in the calling context (e.g. the
        # the backend's ``_open_verification_state`` singleton).
        # Default ``None`` keeps the legacy JSON-file persistence path
        # active for callers that have not yet migrated.
        self.verif_repo = verif_repo

        # Dispatcher refactor — ``guard`` / ``dispatcher`` / ``runtime_state``
        # are the three orthogonal collaborators the per-tick execution
        # loop will be split into. They are accepted as optional kwargs
        # and stored on ``self`` so downstream methods can reach them
        # without globals. All three default to ``None`` so existing
        # callers (no kwargs) keep working — the legacy JSON-file
        # persistence path stays the active default.
        #
        #   * ``guard`` — coarse-grained validator (cycle, same-id loop,
        #     file-modify contract) called once per tick before the
        #     dispatcher picks the next task.
        #   * ``dispatcher`` — per-tick loop controller: picks the next
        #     eligible task, invokes the executor, persists the result
        #     via the runtime_state handle.
        #   * ``runtime_state`` — typed wrapper around the
        #     ``plan_verification.runtime_state`` SQLite JSON column
        #     (see ``runtime_state.py``); holds in-flight bookkeeping
        #     (e.g. ``pending_vps``) that both the dispatcher and the
        #     executor read/mutate.
        self.guard = guard
        self.dispatcher = dispatcher
        self.runtime_state = runtime_state

        # TC-006 dependency-injection fields — owned by the FastAPI
        # lifespan's :class:`backend.runtime_state.RuntimeState`
        # instance and threaded through here so :meth:`_pre_task_lock_hook`,
        # :meth:`_post_task_lock_hook`, and the provider-slot path
        # (``_run_task_with_provider_slot`` → ``_load_provider_info``)
        # all reach the same process-wide tracker and in-flight map.
        # A fresh ``ActiveConcurrencyTracker`` and empty in-flight
        # dict are constructed when the caller does not pass them
        # (tests, ad-hoc scripts) so the agent is still self-contained
        # for callers that have not yet migrated.
        self.in_flight_guard = in_flight_guard
        if dynamic_tracker is None:
            dynamic_tracker = ActiveConcurrencyTracker()
        self.dynamic_tracker = dynamic_tracker
        # Per-instance in-flight file map + its protecting lock.
        #
        # This map is the *in-process* half of the conflict contract: two
        # tasks in the same layer that declare the same file must fail
        # fast rather than queue, because a same-layer overlap means the
        # planner's ``files_to_modify`` graph (``_build_micro_layers``)
        # was wrong — serialising here would hide that planning bug
        # instead of surfacing it.
        #
        # The *cross-process* half is the file-lock broker below, which
        # arbitrates between this executor, a sibling executor, and any
        # sub-agent that decides mid-task to edit a file the plan never
        # mentioned. That path queues rather than fails, because a
        # legitimate holder really can be a different process.
        self._in_flight_files: Dict[str, str] = {}
        self._in_flight_lock = threading.Lock()

        # File-lock broker — started lazily on the first task that claims
        # files, because it needs ``project_dir`` to exist and there is no
        # point holding a socket open for a run that never dispatches
        # anything. ``_lock_broker_started`` keeps the lazy start
        # single-shot: two tasks in the same layer reach this concurrently
        # and must not race to bind the same socket.
        self._lock_broker: Optional[FileLockBroker] = None
        self._lock_broker_started = False
        self._lock_broker_lock = threading.Lock()

        # Initialize managers
        self.task_manager = TaskManager(project_dir, tasks_file=tasks_file)
        #: Where this run's lock files go — beside the plan's own artifacts
        #: (``plans/<plan_id>/locks/``), never inside ``project_dir``.
        #: Resolved once, here, because both the broker and the no-broker
        #: fallback must agree on it exactly; a path computed twice from
        #: two different inputs is how two processes end up holding two
        #: different locks for one file.
        self._locks_dir = self._resolve_locks_dir()
        self.background_manager = BackgroundManager()
        self.executor = Executor(str(project_dir), self.background_manager)
        self.git_manager = GitManager(str(project_dir))
        self.retry_manager = RetryManager()
        self.rollback_manager = RollbackManager(str(project_dir))
        self.refiner = TaskRefiner(self.coding_tool, self.config, logger=logger)

        # Architecture decision points 1 & 2 — the dispatcher
        # only modifies runtime state fields, and writes go through
        # a single atomic interface. The PlanTaskRepository is the
        # implementation of that single interface for runtime state
        # (task #3.8): it carries the per-task optimistic-concurrency
        # CAS, the allow-list of writable fields, and atomic
        # BEGIN IMMEDIATE / COMMIT. ``tasks.json`` is now read-only
        # post-plan-generation; the dispatcher never writes to it.
        # ``self.task_repository`` is retained as the static-field
        # reader (used by ``TaskManager``) plus a legacy shim for any
        # external callers that have not yet migrated.
        self.task_repository = TaskRepository(
            self.task_manager.tasks_file
        )

        # Plan id used as the SQLite key by ``PlanTaskRepository``.
        # Derived from ``tasks_file`` parent directory — the
        # canonical layout is ``plans/{plan_id}/tasks.json`` (the
        # executor launches with ``--tasks-file`` pointing at that
        # path). Legacy layouts that pointed the executor at
        # ``<project_dir>/tasks.json`` have a different convention;
        # callers that need a custom id can pass
        # ``plan_id=`` explicitly (none today).
        #
        # 2026-09-08: the strict reserved-name guard lives in
        # ``task_manager._persist_status_to_sqlite`` (the SQLite write
        # path), NOT here. ``AutonomousAgent.__init__`` continues to
        # accept any parent dir name so unit tests with placeholder
        # layouts like ``tmp_path/project/tasks.json`` keep working —
        # their ``_RecordingTaskRepository`` fakes never reach the
        # real SQLite, so production pollution is still prevented at
        # the persistence boundary.
        self.plan_id = self.task_manager.tasks_file.parent.name

        # Session-local count of how many times each task_id has been
        # marked completed in this execution run. ``_run_async`` resets
        # it at the start of a run; the default here matters because
        # ``_load_tasks`` reads it FIRST (it guards against re-running
        # work this session already finished when the runtime row is
        # missing — see the hydration block below), and ``_load_tasks``
        # can run before ``_run_async`` (plan / recovery paths).
        self._session_task_completed_counts: dict = {}

        # Lazy PlanTaskRepository factory — opens a hermetic or
        # canonical SQLite connection on first use and caches the
        # repo handle. Connection lifetime tracks the agent (caller
        # closes it via ``close_task_progress_repository`` if needed).
        self._task_progress_repo = None

        # PRD decision point 6: serialize concurrent tasks.json writes
        # status update on the read-modify-write critical section.
        # The lock is preserved on the agent for backward compat with
        # existing callers; the TaskRepository owns its own internal
        # lock as well, so the two serialise the same critical
        # section from different angles.
        self._persist_lock = threading.Lock()

        # Allow-list of workspace roots for sub-task ``project_dir``.
        # A sub-task's ``project_dir`` (set by the LLM during breakdown
        # or refinement) MUST be one of these roots, or a subdirectory
        # of one of them. Built from the original task list by
        # ``_load_tasks`` — see ``_is_valid_subtask_workspace`` for the
        # enforcement site. This blocks the LLM from hallucinating
        # unrelated paths that don't actually exist on the executor's
        # filesystem.
        self._valid_workspace_roots: set = set()

        # _active_tasks is the DAG entry point for the scheduler — the
        # subset of self._all_tasks that is non-terminal and currently
        # eligible for execution. It is initialised empty (no plan
        # loaded yet) and refreshed by :meth:`_load_tasks` and
        # :meth:`_breakdown_task`. The scheduler reads from this list
        # via :func:`_build_layers` to compute parallel execution
        # layers, so any mutation (e.g. appending child tasks during a
        # breakdown) must keep the list in topological-friendly shape.
        self._active_tasks: list[SubTask] = []
        self._all_tasks: list[SubTask] = []
        # Tasks whose dispatcher-side loop was broken WITHOUT
        # overwriting a previously-set ``status='completed'``. The
        # same-id loop detector (``record_task_failure(
        # 'same_id_re_run_loop')``) used to clobber the task's
        # persisted status to ``failed``, which lost the real
        # completion signal for the dashboard / task-sync card /
        # counts endpoint. Now the dispatcher adds the id here and
        # ``_get_active_tasks_for_scheduling`` excludes it from future
        # layers in this session — the committed-code / task_progress
        # end_ts record stays intact. Initialised in ``__init__`` (not
        # in ``_run_async``) so unit tests can drive the handler
        # directly without spinning up the async main loop.
        self._dispatcher_blocked: set[str] = set()

        # _leaf_tasks is the authoritative source for plan-failure
        # detection: the set of task ids that currently have no
        # breakdown children. It is the read-side companion of
        # :attr:`_active_tasks` — the breakdown flow
        # (:meth:`_breakdown_task`) removes a parent and inserts its
        # children, and the aggregation flow
        # (:meth:`_aggregate_breakdown_verdict`) does the inverse. The
        # full task list lives on :attr:`_all_tasks`; this set is the
        # "next scheduling target" view that the failure-detection
        # layer reads to decide whether the plan has reached a
        # terminal state.
        self._leaf_tasks: set[str] = set()

    @staticmethod
    def _parse_cycle_members(message: str) -> list:
        """Parse cycle member ids out of a ``"Cycle detected: A, B"`` message.

        Contract (pinned by ``tests/contract/test_dispatcher_cycle_resilience.py``):

          * ``"Cycle detected: 26"`` → ``["26"]``
          * ``"Cycle detected: 4-2-2, 4-2-3"`` → ``["4-2-2", "4-2-3"]``
          * any message without the exact ``"Cycle detected: "`` prefix
            (including the empty string and a lowercase variant) → ``[]``
            so the caller falls through to the legacy
            ``task_validation_failed`` path.

        Returns:
            The list of member id strings, or ``[]`` for non-cycle
            messages.
        """
        prefix = "Cycle detected: "
        if not isinstance(message, str) or not message.startswith(prefix):
            return []
        rest = message[len(prefix):].strip()
        if not rest:
            return []
        return [m.strip() for m in rest.split(",") if m.strip()]

    def _break_cycle_resilience(self, tasks: list, member_ids: list) -> list:
        """Break a dependency cycle at load time instead of failing the plan.

        Background — 2026-09-05 incident on the 2026-09-04:
        the refiner
        returned a transient 2-node cycle in the in-memory snapshot.
        ``_validate_dependencies`` raised ``ValueError("Cycle detected:
        ...")`` for the whole micro-layer, the dispatcher propagated
        the error to every task in the layer (including 4 that had
        already executed successfully), and the executor then hit
        ``No schedulable micro-layer found`` — 32 downstream tasks
        stranded with ``executor_exited_with_unfinished_tasks:32``.

        The resilience pass:

          1. Strip the cycle-causing ``depends_on`` edges *between
             members* (``update_task_status`` persists via
             ``save_tasks``, whose acyclicity guard would reject a
             still-cyclic graph, so the strip MUST come first).
             Members remain in ``tasks.json`` so operators can still
             audit them; only the edges change.
          2. Mark each non-terminal cycle member as ``skipped`` in the
             task manager (``update_task_status`` persists the status
             to ``plan_execution.task_progress`` so a future restart
             still sees them as terminal and the cycle cannot
             re-form). Members already in a terminal state
             (``completed`` / ``failed`` / ``skipped``) are left
             untouched — re-marking would generate noisy audit events
             and could overwrite a legitimate completed status.
          3. Re-run ``_validate_dependencies`` on the repaired
             snapshot and return it. A residual cycle at this point is
             a structural bug and propagates.

        Args:
            tasks: The freshly loaded task list (also referenced by
                ``self.task_manager.tasks`` — mutations are shared).
            member_ids: Cycle member ids parsed from the validator's
                ``"Cycle detected: ..."`` message.

        Returns:
            The repaired task list.
        """
        terminal = {"completed", "failed", "skipped"}
        member_set = set(member_ids)

        # 1. Strip member→member edges FIRST: ``update_task_status``
        #    persists via ``save_tasks``, whose acyclicity guard would
        #    otherwise reject the still-cyclic graph.
        for t in tasks:
            deps = getattr(t, "depends_on", None)
            if t.id in member_set and deps:
                t.depends_on = [d for d in deps if d not in member_set]

        # 2. Mark non-terminal members skipped (each
        #    ``update_task_status`` persists the now-acyclic graph).
        for tid in member_ids:
            sub = next((t for t in tasks if t.id == tid), None)
            if sub is None or sub.status in terminal:
                continue
            self.task_manager.update_task_status(tid, "skipped")

        # 3. Re-validate; a residual cycle here is a real structural
        #    bug and must propagate.
        _validate_dependencies(tasks)
        return tasks

    def _strip_dangling_dependencies(self, tasks: list, exc) -> list:
        """Drop ``depends_on`` edges that name a task absent from the list.

        A dangling reference carries no ordering information — the task it
        names does not exist — so keeping it can only make its target's
        entire downstream cone unschedulable. Before 2026-09-21 this shape
        was misreported as a cycle by ``task_manager._ensure_acyclic``
        (its in-degree was counted from ``len(deps)`` while the reverse
        adjacency was only built for ids that exist), and the runner died
        with "No schedulable micro-layer found".

        Repair rather than refuse, on the same principle as
        :meth:`_break_cycle_resilience`: the disk file stays the source of
        truth and the next load sees a fully-resolvable graph.

        Args:
            tasks: The freshly loaded task list (also referenced by
                ``self.task_manager.tasks`` — mutations are shared).
            exc: The :class:`~framework.task_graph.DanglingTaskDependency`
                raised by ``_validate_dependencies``.

        Returns:
            The repaired task list (same objects, edges rewritten).
        """
        pairs = [(str(a), str(b)) for a, b in getattr(exc, "pairs", []) or []]
        missing = {dep for _, dep in pairs}
        for task in tasks:
            deps = getattr(task, "depends_on", None) or []
            if not deps:
                continue
            kept = [d for d in deps if d not in missing]
            if len(kept) != len(deps):
                task.depends_on = kept

        if self.logger is not None:
            try:
                self.logger.warning(
                    "task_dangling_deps_stripped",
                    (
                        f"stripped {len(pairs)} depends_on edge(s) pointing "
                        f"at tasks absent from the list: {exc}"
                    ),
                    data={"pairs": [f"{a} -> {b}" for a, b in pairs]},
                )
            except Exception:
                # Logging must never mask the load path.
                pass

        try:
            self.task_manager.save_tasks()
        except Exception:
            # A failed persist must not block the run — the in-memory
            # graph is already repaired and the next load re-derives it.
            pass
        return tasks

    def _load_tasks(self) -> list[SubTask]:
        """Load tasks from ``tasks.json`` and validate ``depends_on`` declarations.

        Re-reads ``<project_dir>/tasks.json`` and parses it into a list of
        :class:`SubTask` objects. Immediately after parsing, calls
        :func:`_validate_dependencies` so a malformed plan is rejected at
        load time rather than at execution time (PRD decision point 5).

        This is a public re-entry point used both by ``__init__`` (via
        ``TaskManager``) and by recovery flows that need to re-read the
        task list from disk. Calling it twice is safe — the second call
        refreshes ``self.task_manager.tasks`` in place.

        After parsing and validation, this method filters out tasks
        whose status is already terminal (``completed``, ``failed``,
        ``skipped``) and returns only the non-terminal subset to the
        caller. This is the cross-process recovery contract (PRD
        acceptance case 5): when an existing ``tasks.json`` is
        reloaded (e.g. after a crash or when starting a second run on
        the same project), the DAG fed into :func:`_build_layers` must
        not contain tasks that have already reached a terminal state
       — otherwise the executor would re-execute them and double-write
        checkpoints / commit messages.

        The full list (including terminal tasks) is retained on
        ``self._all_tasks`` so callers that need to report on already-
        finished tasks (e.g. progress dashboards that show "X completed,
        Y failed") can still enumerate them.

        This is a public re-entry point used both by ``__init__`` (via
        ``TaskManager``) and by recovery flows that need to re-read the
        task list from disk. Calling it twice is safe — the second call
        refreshes ``self.task_manager.tasks`` and ``self._all_tasks``
        in place.

        Returns:
            The freshly loaded list of *non-terminal* :class:`SubTask`
            objects. Use ``self._all_tasks`` to access the full list
            (including terminal tasks) for status queries.

            When the filter drops at least one terminal task, a
            single INFO line is also written to ``execution.log``
            under the event name ``task_filtered_terminal`` whose
            payload is ``{"filtered_count": N, "terminal_ids": [...]}``,
            so operators can audit the cross-process recovery
            decision after the fact.

        Raises:
            FileNotFoundError: If ``tasks.json`` does not exist.
            ValueError: If the dependency graph is malformed (missing
                dependency, self-dependency, or cycle). The error
                message identifies the offending ids and is also
                written to ``execution.log`` under the event name
                ``task_validation_failed`` so operators can audit
                rejections after the fact.
        """
        tasks_file = self.task_manager.tasks_file
        if not tasks_file.exists():
            raise FileNotFoundError(f"tasks.json not found at {tasks_file}")

        # Architecture decision point 1: the dispatcher does not
        # open ``tasks.json`` directly. The read path goes through
        # :meth:`TaskRepository.load_all`, which normalises the
        # envelope shape (dict vs. legacy bare-list) and returns
        # ``requirement`` / ``stop_reason`` / ``reason_detail`` /
        # ``tasks`` as a single document.
        document = self.task_repository.load_all()
        data = document  # local alias for the self-heal block below
        raw_tasks: list[dict] = list(document["tasks"])

        _TASK_FIELDS = {
            "id", "title", "description", "test_command", "test_commands",
            "status", "updated_time", "failure_reason", "project_dir",
            "model_type", "depends_on", "breakdown_count", "provider",
            "files_to_modify",
            # 2026-09-16: both are behaviour-bearing and were silently
            # dropped here, which turned their consumer into dead code:
            #   * ``task_group`` gates the refiner's repair-task guard
            #     (see ``refiner_structure``) — without it the guard
            #     never matched and the refiner deleted ``repair-*``
            #     rows it did not echo;
            #   * ``verification_only`` is the declared half of the
            #     audit-task exemption in the dual-criterion completion
            #     rule. Dropping it left only the keyword heuristic.
            "task_group", "verification_only",
        }

        # Detect legacy tasks: entries whose on-disk JSON omitted
        # ``files_to_modify`` entirely. ``SubTask``'s default factory
        # substitutes ``UNKNOWN_MODIFICATIONS_SENTINEL`` automatically,
        # so the DAG still loads — but we record the original ids here
        # so the migration-audit log line below can name them without
        # forcing the operator to diff tasks.json against a baseline.
        legacy_ids: list[str] = []

        def _make_task(t: dict) -> SubTask:
            task_dict = {k: v for k, v in t.items() if k in _TASK_FIELDS}
            task_dict.setdefault("updated_time", None)
            task_dict.setdefault("failure_reason", None)
            task_dict.setdefault("model_type", None)
            task_dict.setdefault("depends_on", [])
            task_dict.setdefault("breakdown_count", 0)
            if "files_to_modify" not in t:
                legacy_ids.append(t.get("id", "<missing>"))
            return SubTask(**task_dict)

        # Self-heal pass (audit 2026-07-16):
        #
        # If ``tasks.json`` carries a stale ``depends_on`` ref to a
        # task id that was later split into ``"{id}-N"`` children by
        # an earlier refiner/breakdown run, the validator below
        # raises ``"Task X depends on missing task Y"`` and the
        # executor's same_id_loop_recovery enters a permanent loop.
        # Run the same rewrite pass the refiner uses so a one-shot
        # cleanup makes the file consistent again — and persist the
        # cleaned data back to disk so subsequent ``save_tasks()``
        # calls don't clobber the fix.
        existing_ids = {t.get("id") for t in raw_tasks if t.get("id")}
        rewrites = TaskRefiner._rewrite_split_depends_on(
            raw_tasks,
            existing_tasks_map={tid: {} for tid in existing_ids},
        )
        if rewrites:
            # Persist the fix to disk so the next save_tasks doesn't
            # overwrite it with the broken in-memory state.
            if isinstance(data, dict):
                data["tasks"] = raw_tasks
            else:
                data = raw_tasks
            try:
                # Unique temp name — a fixed one races any
                # concurrent writer of the same file.
                atomic_write_json(tasks_file, data, indent=2, reraise=True)
            except Exception as exc:
                # Persistence failure is non-fatal here — the in-memory
                # list is already cleaned, validation will pass, and
                # ``save_tasks()`` (called on first status change)
                # will re-persist correctly.
                if self.logger is not None:
                    try:
                        self.logger.warning(
                            "load_self_heal_persist_failed",
                            f"Could not write cleaned tasks.json back to "
                            f"disk ({type(exc).__name__}: {exc}); "
                            f"in-memory cleanup still applied",
                            data={"rewrites": rewrites},
                        )
                    except Exception:
                        pass
            if self.logger is not None:
                try:
                    self.logger.info(
                        "load_self_heal",
                        f"Rewrote {rewrites} stale depends_on refs "
                        f"at load time (refiner split, downstream "
                        f"still pointed at parent)",
                        data={"rewrites": rewrites},
                    )
                except Exception:
                    pass

        tasks = [_make_task(t) for t in raw_tasks]

        # Task #3.8 / same-id-loop fix: hydrate each SubTask's
        # ``status`` from ``plan_execution.task_progress.tasks`` so a
        # completed task stays completed across reloads.
        #
        # Background: post task #3.8, ``save_tasks()`` strips
        # ``status`` / ``updated_time`` / ``failure_reason`` /
        # ``breakdown_count`` from the on-disk ``tasks.json`` and
        # writes them only to ``plan_execution.task_progress.tasks``.
        # The disk file is therefore read-only post-plan-generation
        # and every reload sees a fresh ``status="pending"`` from
        # SubTask's default factory. The dispatcher's
        # ``_TERMINAL_TASK_STATUSES`` filter then treats every task
        # as schedulable, the task runs again, gets marked completed
        # a second time, and ``same_id_loop_recovery`` trips the
        # guard on the third pass — producing the
        # ``same_id_loop_detected`` cluster in
        # ``execution.log``. Re-hydrating from SQLite here makes
        # every reload reflect the actual progress written by
        # ``task_manager.update_task_status`` (which already
        # persists to ``plan_execution.task_progress.tasks`` via
        # :class:`PlanTaskRepository`).
        if self.plan_id:
            try:
                task_progress_repo = self._get_task_progress_repository()
                _missing_status: list = []
                for t in tasks:
                    persisted = task_progress_repo.get_task(
                        self.plan_id, t.id,
                    )
                    if persisted and persisted.get("status"):
                        t.status = persisted["status"]
                        if persisted.get("failure_reason") is not None:
                            t.failure_reason = persisted["failure_reason"]
                        continue
                    # 2026-09-22 — no usable runtime row. This used to be
                    # a silent fall-through to ``pending``, which is the
                    # single most expensive bug of the 0921 run: the disk
                    # file carries no status by design (``save_tasks``
                    # strips it), so a missing row is indistinguishable
                    # from "never started" and the dispatcher re-executed
                    # already-finished work. Eight completed tasks ended
                    # the run with ``status IS NULL`` in ``plan_tasks``;
                    # all eight were re-dispatched, burning ~2h of
                    # provider quota across three waves.
                    #
                    # Two guards now:
                    #   1. if this session already completed the task,
                    #      keep it completed (never re-run finished work
                    #      on the strength of a missing row);
                    #   2. a row that EXISTS but carries no status is an
                    #      anomaly worth a loud warning, because the row
                    #      is the only place a terminal status can live.
                    #      A row that is simply absent is NOT reported:
                    #      that is the normal state of every task on the
                    #      first load of a plan, and warning on it would
                    #      drown the real signal.
                    if self._session_task_completed_counts.get(t.id):
                        t.status = "completed"
                        if self.logger:
                            try:
                                self.logger.error(
                                    "task_status_recovered_from_session",
                                    (
                                        f"Task [{t.id}] has no usable runtime "
                                        f"status row but completed in this "
                                        f"session; keeping status=completed "
                                        f"instead of re-running it"
                                    ),
                                    task_id=t.id,
                                    data={
                                        "plan_id": self.plan_id,
                                        "row_present": bool(persisted),
                                    },
                                )
                            except Exception:
                                pass
                        continue
                    if persisted is not None:
                        _missing_status.append(t.id)
                if _missing_status and self.logger:
                    try:
                        self.logger.warning(
                            "task_status_missing_on_reload",
                            (
                                f"{len(_missing_status)} task(s) reloaded "
                                f"without a runtime status row and were "
                                f"treated as pending: "
                                f"{_missing_status[:20]}"
                            ),
                            data={
                                "plan_id": self.plan_id,
                                "task_ids": _missing_status[:50],
                                "count": len(_missing_status),
                            },
                        )
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                # Non-fatal: a stale or missing task_progress row is
                # treated as "no progress yet" — the dispatcher
                # proceeds with the default ``pending`` status and
                # recovers on the next reload. Logged so an operator
                # can correlate a SQLite hiccup with the executor's
                # behaviour.
                if self.logger is not None:
                    try:
                        self.logger.warning(
                            "task_progress_hydrate_failed",
                            f"Could not hydrate task status from "
                            f"plan_execution.task_progress for plan "
                            f"{self.plan_id}: {exc}",
                            data={"error": str(exc)[:500]},
                        )
                    except Exception:
                        pass

        # 2026-09-08: Phase 2 — reconcile orphan tasks.
        #
        # After refiner splits a task (e.g. ``40`` → ``40-1, 40-2``)
        # the children land on disk via ``task_manager.set_tasks`` but
        # the parent's state.db entry stays behind. When the parent
        # itself gets re-split (``40-1`` → ``40-1-1, 40-1-2, 40-1-3``)
        # the original ``40-1`` becomes a second-generation orphan.
        # Without this step, ``_load_tasks`` only reads ``tasks.json``
        # and never sees those stranded entries, so they sit in
        # ``task_progress`` until the plan is archived.
        #
        # Phase 2 walks every state.db entry whose id is NOT in the
        # disk list and decides between:
        #   * ``merge``   — full static fields present, status is
        #                    non-terminal → construct a SubTask and
        #                    inject it into the DAG so the dispatcher
        #                    can finally schedule it.
        #   * ``supersede`` — terminal status, or no full fields (a
        #                    refiner-cleanup residue) → leave the
        #                    state.db entry alone but mark it
        #                    ``superseded`` so the dispatcher treats
        #                    it as already-finished (see
        #                    ``_TERMINAL_TASK_STATUSES``).
        #
        # This block is deliberately wrapped in its own try/except so a
        # SQLite error here does NOT mask the hydrate failure above.
        # Both phases are best-effort: the executor can still run
        # with whatever Phase 1 produced if Phase 2 fails.
        if self.plan_id:
            try:
                task_progress_repo = self._get_task_progress_repository()

                disk_ids = {t.id for t in tasks}
                reconciled: list[str] = []
                superseded: list[str] = []

                # 2026-09-16: ``test_command`` used to be
                # required to merge an orphan, and that silently destroyed
                # the content of every orphan row that had a title and a
                # full description but no command. Rows hit exactly that:
                # the gate fell through
                # to the placeholder branch below, whose
                # ``"recovered from plan_tasks DB (no static fields on
                # disk...)"`` text REPLACED the real description. The
                # subagent then reported "no description, no
                # files_to_modify, and no test_command" — an accurate
                # description of the placeholder — and the task was marked
                # failed for a defect the loader had introduced moments
                # earlier.
                #
                # A missing command is a real gap, but it is not grounds
                # for throwing away a runnable task. Merge on title +
                # description and warn instead; see
                # ``_ORPHAN_MERGE_REQUIRED_FIELDS`` for the full rationale.
                merged_without_command: list[str] = []

                for entry in task_progress_repo.iter_orphan_tasks(
                    self.plan_id, disk_ids,
                ):
                    tid = entry.get("id", "")
                    status = entry.get("status", "")
                    has_fields = _orphan_has_mergeable_content(entry)

                    if status in _TERMINAL_TASK_STATUSES:
                        # Already finished — never re-run.
                        superseded.append(tid)
                        continue
                    if has_fields:
                        # Live orphan with full fields — merge into DAG.
                        if not (entry.get("test_command") or "").strip():
                            merged_without_command.append(tid)
                        sub_task_dict: dict[str, Any] = (
                            _orphan_merged_subtask_kwargs(entry)
                        )
                        sub_task_dict.setdefault("updated_time", None)
                        sub_task_dict.setdefault("failure_reason", None)
                        sub_task_dict.setdefault("model_type", None)
                        sub_task_dict.setdefault("depends_on", [])
                        sub_task_dict.setdefault("breakdown_count", 0)
                        sub_task_dict["status"] = status or "pending"
                        # 2026-09-09: the sentinel value is
                        # self-describing. If the orphan entry did not
                        # include ``files_to_modify``, the SubTask default
                        # factory substitutes the sentinel — which the
                        # validator accepts as read-only. No special
                        # handling needed.
                        tasks.append(SubTask(**sub_task_dict))
                        reconciled.append(tid)
                        continue
                    # Status-only orphan (no static fields on disk).
                    # 2026-09-10: the legacy rule "supersede any
                    # orphan missing required fields" silently dropped
                    # real pending tasks left behind by the v3 → v4 R3-1
                    # race condition — rows whose ``status="pending"``
                    # describe tasks that never finished. Surface them as
                    # placeholder SubTasks tagged ``_origin="db_orphan"``
                    # so the dispatcher can pick them up; the
                    # placeholder carries the persisted status
                    # verbatim. Placeholders are filtered out of
                    # ``save_tasks`` so they never overwrite disk.
                    placeholder = SubTask(
                        id=tid,
                        title=f"[db-orphan:{tid}]",
                        description=(
                            f"recovered from plan_tasks DB (no static "
                            f"fields on disk; status={status!r}, "
                            f"end_ts={entry.get('end_ts')!r})"
                        ),
                        status=status or "pending",
                        updated_time=entry.get("end_ts"),
                        failure_reason=entry.get("failure_reason"),
                        model_type="medium",
                    )
                    placeholder._origin = "db_orphan"  # type: ignore[attr-defined]
                    tasks.append(placeholder)
                    reconciled.append(tid)

                if (reconciled or superseded) and self.logger is not None:
                    try:
                        self.logger.info(
                            "task_orphans_reconciled",
                            (
                                f"Reconciled {len(reconciled)} orphan(s) "
                                f"into DAG; superseded {len(superseded)}"
                            ),
                            data={
                                "reconciled_ids": sorted(reconciled),
                                "superseded_ids": sorted(superseded),
                                "disk_task_count": len(disk_ids),
                            },
                        )
                    except Exception:
                        pass

                # Merged orphans carrying real content but no command —
                # they will run, and their verdict will rest on the audit
                # second pass rather than on an exit code. Surfaced so an
                # operator can tell "task generation dropped the field"
                # apart from "the task genuinely needs no command".
                if merged_without_command and self.logger is not None:
                    try:
                        self.logger.warning(
                            "task_orphan_merged_without_test_command",
                            (
                                f"{len(merged_without_command)} orphan "
                                f"task(s) merged with no test_command; "
                                f"completion will rely on the audit "
                                f"second pass"
                            ),
                            data={
                                "task_ids": sorted(merged_without_command),
                                "disk_task_count": len(disk_ids),
                            },
                        )
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                # Non-fatal: orphan reconcile is best-effort.
                # The executor still has the on-disk DAG; orphan
                # entries will be retried on the next _load_tasks.
                if self.logger is not None:
                    try:
                        self.logger.warning(
                            "task_orphans_reconcile_failed",
                            (
                                f"Orphan reconcile step failed for plan "
                                f"{self.plan_id}: {exc}"
                            ),
                            data={"error": str(exc)[:500]},
                        )
                    except Exception:
                        pass

        # Update the manager so subsequent calls (run, get_next_task, ...)
        # see the freshly validated list.
        self.task_manager.tasks = tasks
        if isinstance(data, dict):
            self.task_manager.requirement = data.get("requirement", "")
            self.task_manager.stop_reason = data.get("stop_reason", None)
            self.task_manager.reason_detail = data.get("reason_detail", None)

        # Migration-audit marker: when the on-disk format is older than
        # the ``files_to_modify`` field, emit a single ``legacy_task_marker``
        # event so downstream auditors can ``grep legacy_task_marker
        # execution.log`` to identify every plan that needs a migration
        # pass. Mirrors the ``task_filtered_terminal`` pattern — single
        # INFO line per load, payload carries the legacy ids in input
        # order. No event is emitted when every task declared the
        # field, to keep the log quiet on the happy path.
        if legacy_ids and self.logger is not None:
            try:
                self.logger.info(
                    "legacy_task_marker",
                    (
                        f"Detected {len(legacy_ids)} legacy task(s) in tasks.json "
                        f"(missing files_to_modify; substituted "
                        f"UNKNOWN_MODIFICATIONS_SENTINEL)"
                    ),
                    data={
                        "legacy_count": len(legacy_ids),
                        "legacy_ids": list(legacy_ids),
                    },
                )
            except Exception:
                # Logging must never block the load — matching the
                # defensive pattern used by ``task_filtered_terminal``
                # below.
                pass

        try:
            _validate_dependencies(tasks)
        except DanglingTaskDependency as exc:
            # 2026-09-21: a dangling reference is a bookkeeping bug, not a
            # structural one. Strip the edge and keep loading — refusing
            # (or, worse, calling it a cycle) strands every task behind
            # it, which is how a production plan died.
            tasks = self._strip_dangling_dependencies(tasks, exc)
        except ValueError as exc:
            member_ids = self._parse_cycle_members(str(exc))
            if member_ids:
                # Cycle-resilience branch (2026-09-05 incident): a
                # transient refiner cycle must not strand the whole
                # plan. Mark the members skipped, strip the cycle
                # edges, persist, and re-validate — see
                # :meth:`_break_cycle_resilience`.
                if self.logger is not None:
                    try:
                        self.logger.error(
                            "task_cycle_broken",
                            (
                                f"dependency cycle at load time; "
                                f"marking members skipped and stripping "
                                f"edges: {exc}"
                            ),
                            data={"members": member_ids},
                        )
                    except Exception:
                        # Logging must never mask the load path.
                        pass
                tasks = self._break_cycle_resilience(tasks, member_ids)
            else:
                # Persist the failure to execution.log so operators have an
                # auditable record. The plan is rejected regardless of the
                # log write succeeding.
                if self.logger is not None:
                    try:
                        self.logger.error(
                            "task_validation_failed",
                            f"tasks.json rejected at load time: {exc}",
                            data={"error": str(exc)},
                        )
                    except Exception:
                        # Logging must never mask the original validation error
                        pass
                raise

        # desc ⇄ depends_on consistency check (task 4 hardening).
        # Even after the post-processing fallback that injects
        # ``depends_on`` from ``desc`` whenever the LLM omits the
        # field, the planner can still drift: the LLM may emit a
        # non-empty desc line like "前置条件：任务 1 已完成" but
        # supply a ``depends_on`` that does NOT include "1". The
        # check is fail-fast — the first inconsistency aborts the
        # load with a ValueError naming both the offending task id
        # and the missing dependency, so a malformed plan never
        # reaches the executor. The error is logged under the
        # same ``task_validation_failed`` event so the existing
        # operator-facing audit trail is preserved.
        try:
            validate_desc_consistency(tasks)
        except ValueError as exc:
            if self.logger is not None:
                try:
                    self.logger.error(
                        "task_validation_failed",
                        f"tasks.json rejected at load time: {exc}",
                        data={"error": str(exc)},
                    )
                except Exception:
                    # Logging must never mask the original error.
                    pass
            raise

        # --- Task validation lives at GENERATION time ---------------
        # Removed 2026-09-21 ( "不应该有 pre-run gate 这种
        # 东西。所有的东西都应该在生成任务的时候去做校验。如果等到能够
        # 执行的时候，启动执行就应该马上成功。").
        #
        # The 4-step ``TaskOutputValidator`` still exists and still runs —
        # but at task-generation time, against each task's own
        # ``project_dir`` (``TasksGenerator.harden_for_execution``), so a
        # ``tasks.json`` that reaches this loader is already known-good.
        #
        # Why the execution-side gate had to go: it validated one snapshot
        # against a single run-level ``project_dir`` while every task
        # carries its own, and when it failed the only outcomes were
        # "abort a run the operator has already been told started" or
        # "run something known-broken". Plan
        # a production plan died 0.4s after a successful
        # ``/start`` on exactly that.
        #
        # What still guards this path: the containment hook in
        # ``coding_tool`` (a sub-agent physically cannot write outside
        # ``PDT_PROJECT_DIR``), the ``_validate_dependencies`` /
        # ``validate_desc_consistency`` checks above, and the per-task
        # ``test_command`` plus cross-verify at run time.

        # Cross-process recovery contract (PRD acceptance case 5):
        # retain the full list (including terminal tasks) for status
        # queries, but return only the non-terminal subset to the caller
        # so :func:`_build_layers` builds a DAG that excludes already-
        # completed / failed / skipped tasks. ``breakdown_in_progress``
        # is intentionally NOT in this set — when a task is being
        # broken down into subtasks, the children have already been
        # written to ``state.db`` by the refiner, and this same load's
        # Phase-2 reconcile pulls them in, so they must remain in the DAG
        # to be scheduled.
        #
        # The actual partition is delegated to the pure helper
        # :func:`_filter_terminal_tasks` so the terminal-status rule
        # has a single, testable source of truth (and so the in-line
        # duplication of that rule does not drift over time).
        #
        # Re-point the manager at the snapshot this load just finalised.
        # ``_load_tasks`` may re-bind ``tasks`` while repairing (the
        # dangling-edge stripper mutates in place today, but a future
        # repair need not), and the assignment earlier in this method
        # happens BEFORE those repairs — so ``task_manager.tasks`` and
        # ``self._all_tasks`` can end up holding different ``SubTask``
        # objects. The executor's completion
        # bookkeeping mutates the manager's objects
        # (``task_manager.update_task_status``), so the dispatcher's
        # stale copies keep reading ``pending`` on the next layer rebuild
        # and re-dispatch work that already finished, re-committing its
        # git checkpoint. Observed in the 20260806 / 20260807 plans:
        # thousands of ``same_id_loop_detected`` events per run, and
        # duplicate first-attempt ``task_started`` entries. Re-pointing
        # here restores object sharing so a status write is visible to
        # the scheduler immediately; the same-id loop guard goes back to
        # being a safety net instead of the routine path.
        self.task_manager.tasks = tasks
        self._all_tasks = list(tasks)
        filtered_tasks, filtered_count = _filter_terminal_tasks(tasks)
        # Mirror the filtered list onto self._active_tasks — the scheduler
        # and :meth:`_breakdown_task` both consume this list. Keeping a
        # separate attribute (rather than re-filtering self._all_tasks
        # on every read) lets the breakdown flow append child tasks
        # without touching the historical/terminal record.
        self._active_tasks = list(filtered_tasks)
        # Initialise the leaf set from the FULL task list (not the
        # filtered active list). The leaf set is the "bottom of the
        # breakdown tree" view: a task is a leaf iff no other task
        # has an id starting with ``task.id + "-"``. Using the full
        # list (rather than the active filter) means already-terminal
        # children of an in-progress parent are correctly counted as
        # leaves — they are the children the aggregation flow needs
        # to see when the last sibling completes. The set is rebuilt
        # (not appended to) so a re-load after a crash does not pick
        # up stale ids from a previous run.
        self._leaf_tasks = _compute_leaf_tasks(self._all_tasks)
        if filtered_count and self.logger is not None:
            # Cross-process recovery diagnostic: when the load filters
            # N terminal tasks out of the returned DAG, operators have
            # no in-band signal of which tasks were skipped unless
            # they diff tasks.json against an earlier snapshot. The
            # ``task_filtered_terminal`` event writes a single INFO
            # line containing the filtered count + the actual ids
            # (in input order) so ``grep task_filtered_terminal
            # execution.log`` is enough to reconstruct the diff.
            #
            # The write is wrapped in try/except (matching the
            # pattern used by ``_persist_task_status``) so a transient
            # FS error on ``execution.log`` never blocks the load
            # itself — the filtered DAG is still returned to the
            # caller.
            terminal_ids = [
                t.id for t in tasks if t.status in _TERMINAL_TASK_STATUSES
            ]
            try:
                self.logger.info(
                    "task_filtered_terminal",
                    f"Filtered {filtered_count} terminal tasks from DAG entry",
                    data={
                        "filtered_count": filtered_count,
                        "terminal_ids": terminal_ids,
                    },
                )
            except Exception:
                # Logging must never mask the load result.
                pass

        # Build the allow-list of workspace roots from the ORIGINAL
        # task list (before filtering). Sub-tasks created during
        # breakdown/refinement must land in one of these trees —
        # see ``_is_valid_subtask_workspace`` and the test suite
        # ``tests/unit/test_workspace_validation.py``.
        roots: set = set()
        try:
            roots.add(Path(self.project_dir).resolve())
        except Exception:
            pass
        for t in tasks:
            pd = getattr(t, "project_dir", None)
            if pd:
                try:
                    roots.add(Path(pd).resolve())
                except Exception:
                    # Malformed path in tasks.json — skip rather
                    # than block the whole load.
                    pass
        self._valid_workspace_roots = roots

        return filtered_tasks

    def _is_valid_subtask_workspace(self, candidate) -> bool:
        """Check whether a candidate ``project_dir`` is acceptable for a sub-task.

        Contract
        --------
        A sub-task's ``project_dir`` (as set by the LLM during breakdown
        or refinement) must satisfy BOTH:

          1. Be a real, resolvable filesystem path.
          2. Be equal to one of the original task ``project_dir``s, OR
             be a sub-directory of one of them.

        The allow-list is built once at plan load time from
        ``self._valid_workspace_roots`` (set in ``_load_tasks``).
        A sub-task created at runtime (e.g. ``1-1`` broken down from
        ``1``) must therefore be on-disk at a path the executor
        already knows about — not a hallucinated directory the LLM
        invented.

        Edge cases
        ----------
          * ``candidate`` is ``None`` or empty string → return ``True``
            (treat as "unset"; the sub-task will inherit the parent's
            ``project_dir`` via ``_enforce_subtask_workspace``).
          * The allow-list is empty (e.g. plan has no project_dir
            anywhere) → return ``True`` to avoid blocking legitimate
            flows.
          * The candidate path itself doesn't exist on disk → still
            acceptable, as long as the path *prefix* matches an
            allow-list root. Sub-tasks may legitimately create new
            sub-directories inside an existing project.

        Args:
            candidate: Path string, ``Path``, or ``None``.

        Returns:
            ``True`` if the candidate is acceptable, ``False`` otherwise.
        """
        if not candidate:
            return True
        if not self._valid_workspace_roots:
            return True
        try:
            cand = Path(candidate).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        for root in self._valid_workspace_roots:
            try:
                root_resolved = root.resolve() if isinstance(root, Path) else Path(root).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if cand == root_resolved:
                return True
            try:
                cand.relative_to(root_resolved)
                return True
            except ValueError:
                continue
        return False

    def _enforce_subtask_workspace(self, new_task: dict, parent_task: "SubTask") -> dict:
        """Enforce the workspace allow-list on a single sub-task.

        Logic
        -----
        Given a sub-task dict produced by breakdown/refinement and
        the parent task it was split from, this method returns a
        *new* dict (does not mutate input) with ``project_dir``
        guaranteed to satisfy the allow-list:

          * If ``new_task["project_dir"]`` is valid → keep it.
          * Otherwise → fall back to the parent's ``project_dir``.
          * If the parent has no ``project_dir`` either → keep the
            LLM's value (best effort; the executor will likely fail
            with a clearer error than our validation).

        A ``breakdown_workspace_corrected`` event is logged when
        the LLM's value had to be replaced, so operators can audit
        how often the LLM hallucinates paths.

        Args:
            new_task: Sub-task dict from the LLM.
            parent_task: The task being broken down / refined.

        Returns:
            New dict with enforced ``project_dir``.
        """
        import copy
        corrected = copy.deepcopy(new_task)
        candidate = corrected.get("project_dir")
        if self._is_valid_subtask_workspace(candidate):
            return corrected
        fallback = getattr(parent_task, "project_dir", None) or str(self.project_dir)
        corrected["project_dir"] = fallback
        if self.logger:
            self.logger.warning(
                "breakdown_workspace_corrected",
                f"Sub-task [{corrected.get('id', '?')}] had invalid "
                f"project_dir {candidate!r}; replaced with parent "
                f"workspace {fallback!r}",
                task_id=parent_task.id,
                data={
                    "subtask_id": corrected.get("id"),
                    "original_project_dir": candidate,
                    "fallback_project_dir": fallback,
                },
            )
        return corrected

    # ------------------------------------------------------------------
    # Architecture decision point 7: dispatcher files_to_modify
    # self-heal helpers. The gate at ``_load_tasks`` calls these to
    # repair step-3 "files_to_modify is empty" failures via a cloud
    # subagent — the validator itself remains strict + stateless
    # (contract 3), the self-heal lives entirely in the dispatcher
    # so the validator never silently accepts an empty list.
    # ------------------------------------------------------------------

    def _persist_task_status(self, task: SubTask) -> None:
        """Atomically update a single task's runtime state in SQLite.

        Task #3.8: per-task runtime state moved off ``tasks.json`` and
        onto ``plan_execution.task_progress.tasks`` via
        :class:`PlanTaskRepository`. The dispatcher writes through
        this repo, never through :class:`TaskRepository`.

        Architecture contract (unchanged):
          1. The dispatcher only writes runtime state fields
             (``status``, ``end_ts``, ``commit_sha``, ``attempt``,
             ``schedule_ts``). Everything else is structural and is
             rejected by :class:`PlanTaskRepository` with
             ``TaskProgressValidationError``.
          2. The write goes through a single atomic interface
             (:meth:`PlanTaskRepository.update_task`) which holds the
             per-task ``_repo_version`` CAS inside ``BEGIN IMMEDIATE``
             so concurrent dispatcher threads cannot lose an update.

        The dispatcher itself no longer touches ``tasks.json`` at
        runtime — the file is static-only and read-only post-plan-
        generation. The repository's :data:`ALLOWED_TASK_FIELDS`
        allow-list is the architectural boundary.

        Conflict handling
        -----------------
        A :class:`ConflictError` from :meth:`update_status` means
        the row's version advanced between our snapshot read and
        the update. The dispatcher re-reads the latest status,
        re-decides whether the update is still needed, and retries
        up to ``MAX_PERSIST_CONFLICT_RETRIES`` times. After the
        budget is exhausted, an entry is written to ``execution.log``
        under the ``task_persist_conflict`` event with the
        ``task_id`` and the conflict reason, and the task is
        aborted (a :class:`RuntimeError` propagates to the caller).

        Why this writes to ``self.task_repository.tasks_file``
        (which is wired to ``self.task_manager.tasks_file``): when
        the backend launches execution via ``cli.py
        --tasks-file <path>`` (which is how ``server.py`` starts
        subprocesses — see ``start_execution`` in server.py), the
        canonical file is ``plans/{plan_id}/tasks.json``, NOT
        ``<project_dir>/tasks.json``. The TaskRepository is built
        once in ``__init__`` against that canonical path, so every
        read and write produced by the dispatcher automatically
        targets the canonical file.

        Args:
            task: The :class:`SubTask` whose runtime state should be
                persisted.

        Raises:
            FileNotFoundError: If ``tasks.json`` does not exist.
            RuntimeError: If the conflict retry budget is exhausted.
            ValidationError: If ``task`` carries a field outside the
                architected allow-list (caller bug, never silently
                dropped).
        """
        # MAX_PERSIST_CONFLICT_RETRIES is the bounded retry budget the
        # dispatcher applies to a persistent version mismatch. After
        # this many confirmed conflicts we stop retrying and
        # surface the failure to ``execution.log`` — silent retry
        # forever is worse than a loud death. The value follows the
        # TaskRepository's CAS contract: every retry re-reads the
        # version, so the budget is bounded in observable time.
        MAX_PERSIST_CONFLICT_RETRIES = 3

        task_id = task.id

        # 2026-09-17: never write runtime state for a task the plan no
        # longer owns. ``PlanTaskRepository.update_task`` is an
        # ``INSERT ... ON CONFLICT DO UPDATE``, so a write here would
        # re-create a row that ``_apply_refiner_structure`` just
        # deleted — and re-create it content-free, because the payload
        # carries runtime fields only. ``self.task_manager.tasks``
        # holds the full on-disk list (``_load_tasks`` assigns it
        # before the terminal filter), so membership is exactly "the
        # plan still contains this task". The alternative signal —
        # "does the row exist" — is not usable: a task can legitimately
        # be on disk with no row yet, and that case must keep writing
        # or the next reload sees it as ``pending`` and re-schedules it
        # forever. Same rule as the two ``TaskManager`` mirrors.
        if task_id not in {t.id for t in self.task_manager.tasks}:
            if self.logger is not None:
                try:
                    self.logger.info(
                        "task_persist_skipped_removed",
                        (
                            f"Skipped runtime-state write for [{task_id}]: "
                            f"the task is no longer in the plan (a "
                            f"refinement removed it); writing it would "
                            f"re-create a content-free row"
                        ),
                        task_id=task_id,
                        data={"status": getattr(task, "status", None)},
                    )
                except Exception:
                    pass
            return

        # ---- Build the allow-list-only fields dict ----
        # The dispatcher translates the in-memory SubTask into the
        # repository's allow-list. Only fields the architecture
        # permits the dispatcher to write are passed through.
        # ``updated_time`` is mapped to ``end_ts`` (the closest
        # semantic match in the allow-list — a completion timestamp
        # for non-running tasks). ``breakdown_count`` is NOT a
        # runtime state field; it is structural metadata about the
        # breakdown history and is intentionally NOT written by the
        # dispatcher through this path.
        fields: dict = {}
        task_status = getattr(task, "status", None)
        if task_status is not None:
            fields["status"] = task_status
        task_updated_time = getattr(task, "updated_time", None)
        if task_updated_time is not None:
            fields["end_ts"] = task_updated_time
        task_commit_sha = getattr(task, "commit_sha", None)
        if task_commit_sha is not None:
            fields["commit_sha"] = task_commit_sha
        task_attempt = getattr(task, "attempt", None)
        if task_attempt is not None:
            fields["attempt"] = task_attempt

        # ---- Retry loop on optimistic-concurrency conflicts ----
        last_reason: str = ""
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
            TaskProgressConflictError,
            TaskProgressNotFound,
            TaskProgressValidationError,
        )
        # Lazy-build the PlanTaskRepository against this agent's
        # ``plan_id`` and the env-var / repo-root state DB.
        task_progress_repo = self._get_task_progress_repository()
        for attempt in range(MAX_PERSIST_CONFLICT_RETRIES + 1):
            try:
                # Snapshot the per-task version BEFORE the update so
                # the repository's CAS predicate has a comparison
                # value.
                expected_version = task_progress_repo.get_version(
                    self.plan_id, task_id,
                )
                task_progress_repo.update_task(
                    plan_id=self.plan_id,
                    task_id=task_id,
                    fields=fields,
                    expected_version=expected_version,
                )
                return
            except TaskProgressConflictError as exc:
                last_reason = str(exc)
                # Loop: re-read on the next iteration. The
                # ``task_progress`` column is consulted by
                # ``get_version`` on the next pass, so the conflict
                # will either resolve (if the side that won the race
                # wrote what we wanted) or persist (different
                # intent, e.g. one writer wants "completed" and
                # another wants "failed"). The retry budget bounds
                # the loop.
                continue
            except TaskProgressValidationError:
                # TaskProgressValidationError is a programming bug —
                # the dispatcher passed a structural field. Refuse
                # to retry; surface it loudly so the operator can
                # diagnose the bug.
                if self.logger is not None:
                    try:
                        self.logger.error(
                            "task_persist_validation",
                            f"PlanTaskRepository.update_task rejected "
                            f"dispatcher write for task_id={task_id}",
                            task_id=task_id,
                            data={"fields": list(fields.keys())},
                        )
                    except Exception:
                        pass
                raise

        # ---- Conflict budget exhausted ----
        # The dispatcher tried K+1 times and the row's version
        # kept advancing underneath us. This is the "still
        # conflicting, abort the task" branch from the architecture
        # decision — we MUST NOT silently swallow it. The execution
        # log entry names both the task_id and the most recent
        # conflict reason so an operator can correlate the failure
        # with the version-drift event that caused it.
        if self.logger is not None:
            try:
                self.logger.error(
                    "task_persist_conflict",
                    (
                        f"TaskRepository.update_status retried "
                        f"{MAX_PERSIST_CONFLICT_RETRIES} times for "
                        f"task_id={task_id!r} and still conflicts; "
                        f"aborting task. last_conflict_reason="
                        f"{last_reason!r}"
                    ),
                    task_id=task_id,
                    data={
                        "retries": MAX_PERSIST_CONFLICT_RETRIES,
                        "last_conflict_reason": last_reason,
                    },
                )
            except Exception:
                # Logging must never mask the original failure.
                pass
        raise RuntimeError(
            f"_persist_task_status: persistent ConflictError on "
            f"task_id={task_id!r} after "
            f"{MAX_PERSIST_CONFLICT_RETRIES} retries "
            f"(last reason: {last_reason!r}); task aborted"
        )

    def _get_task_progress_repository(self):
        """Return a per-thread :class:`PlanTaskRepository` for this agent.

        SQLite connections are not safe to share across threads even
        with ``check_same_thread=False`` + WAL — the Python-level
        cursor / statement state on a single connection is not
        thread-safe, and concurrent ``BEGIN IMMEDIATE`` cycles from
        different threads can interleave in ways that produce
        ``disk I/O error`` / ``cannot commit - no transaction is
        active`` / ``file is not a database`` (see test
        ``test_persist_concurrent_safe`` for the regression history).
        The repo + connection are therefore built once per thread on
        first use and cached on :attr:`_thread_local_repos` so
        subsequent calls from the same thread reuse the same handle.

        Test injection hook: setting :attr:`_task_progress_repo` to
        a recorder / stand-in makes the agent use that object on
        every call (single-threaded tests only — the recorder is not
        thread-safe itself). When :attr:`_task_progress_repo` is
        ``None``, the per-thread SQLite-backed repo is built lazily.

        The connection is resolved by
        :func:`config_paths.resolve_state_db_path` — the same resolver
        ``server._state_db_path`` delegates to, and the same one every
        other writer uses. It honours ``PDT_STATE_DB_PATH`` and
        otherwise falls back to ``<repo>/state.db``.
        """
        if self._task_progress_repo is not None:
            return self._task_progress_repo
        if not hasattr(self, "_thread_local_repos"):
            self._thread_local_repos = threading.local()
        cached = getattr(self._thread_local_repos, "repo", None)
        if cached is not None:
            return cached
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        db_path = resolve_state_db_path()
        conn = _open_db(db_path)
        _migrate(conn)
        repo = PlanTaskRepository(conn)
        self._thread_local_repos.repo = repo
        return repo

    def parse_files_from_response(self, response: str) -> dict:
        """
        Parse file changes from AI response.

        Args:
            response: AI response text

        Returns:
            Dictionary mapping file paths to content
        """
        files = {}
        pattern = r"FILE:\s*(.*?)\n```(?:\w+)?\n(.*?)\n```"
        matches = re.findall(pattern, response, re.DOTALL)
        for path, content in matches:
            path = path.strip()
            path_obj = Path(path)
            if path_obj.is_absolute():
                try:
                    path_obj = path_obj.relative_to(self.project_dir)
                except ValueError:
                    path_obj = Path(path_obj.name)
            files[str(path_obj)] = content
        return files

    def plan(self):
        """Plan tasks from the requirement using domain-specific configuration."""
        if not self.requirement:
            return

        print(f"Planning tasks for: {self.requirement}")
        if self.logger:
            self.logger.info("plan_started", f"Planning tasks for requirement ({len(self.requirement)} chars)",
                             data={"requirement_len": len(self.requirement)})
        try:
            # Use config's planner prompt or default
            planner_prompt = self.config.planner_system_prompt

            # Add domain knowledge if available
            full_prompt = ""
            if self.config.domain_knowledge:
                full_prompt = f"{self.config.domain_knowledge}\n\n"

            full_prompt += f"Requirement: {self.requirement}"

            plan_response = self.coding_tool.query_json(
                full_prompt,
                system_instruction=planner_prompt,
                scene="planning",
            )
            self.task_manager.set_tasks(plan_response["tasks"], requirement=self.requirement)
            print(f"\nPlanned {len(self.task_manager.tasks)} tasks:")
            for task in self.task_manager.tasks:
                print(f"  {task.id}. {task.title}")
            if self.logger:
                self.logger.info("plan_completed", f"Planned {len(self.task_manager.tasks)} tasks",
                                 data={"task_count": len(self.task_manager.tasks)})
        except Exception as e:
            print(f"Failed to plan tasks: {e}")
            if self.logger:
                self.logger.error("plan_failed", f"Failed to plan tasks: {e}",
                                  data={"error": str(e)})
            raise

    def run(self, max_tasks: Optional[int] = None, timeout: Optional[int] = None):
        """
        Execute tasks until completion or max_tasks limit.

        PRD decision points 1 + 2: tasks are scheduled layer-by-layer (built
        from the DAG via :func:`_build_layers`), with all tasks in a single
        layer running concurrently via :func:`asyncio.gather`. Each task
        acquires a per-provider slot through :class:`ProviderConcurrencyController`
        before its synchronous executor body runs in a worker thread.

        Boundary handling:

          * Empty layer → skip and continue to the next layer.
          * Layer-internal task exception → ``return_exceptions=True`` keeps
            the other tasks unaffected; the failing task surfaces in the
            gather result and is reflected on disk via its own
            ``record_task_failure`` path inside :meth:`_execute_task_with_retry`.
          * Breakdown mid-flight → after each layer completes we re-derive
            the active task set from disk + in-memory state, so any newly
            inserted child tasks are picked up on the next layer rebuild.
          * Global semaphore full → subsequent tasks suspend inside
            :meth:`ProviderConcurrencyController.acquire` until a paired
            release frees a slot.

        Args:
            max_tasks: Maximum number of tasks to execute (counts each
                ``_execute_task_with_retry`` invocation, regardless of
                its verdict). The outer dispatcher honours this cap by
                short-circuiting once it is reached.
            timeout: Per-task timeout passed through to the coding tool.
        """
        try:
            # Started here, not on first use, so the broker's lifetime is
            # exactly the run's lifetime. Booting it lazily from
            # ``_pre_task_lock_hook`` meant any caller that used the hook
            # without ``run()`` — tests, embedded use — spawned an
            # acceptor thread nothing ever reclaimed; the test suite's
            # "no worker outlives its test" guard caught exactly that.
            self._ensure_lock_broker()
            asyncio.run(self._run_async(max_tasks=max_tasks, timeout=timeout))
        finally:
            self._stop_lock_broker()
            self.executor.cleanup()
            if self.logger:
                self.logger.info("execution_cleanup", "Executor resources cleaned up")

    async def _run_async(
        self, max_tasks: Optional[int] = None, timeout: Optional[int] = None
    ) -> None:
        """Async layer-iteration dispatcher driving the main scheduling loop.

        Each iteration of the outer ``while True`` processes exactly one
        non-empty layer, then re-derives the active task set so any
        mid-flight breakdown (which appends child tasks to
        ``self._active_tasks`` / ``task_manager.tasks``) is picked up on
        the next layer rebuild. The same loop covers the empty-layer,
        cycle-residual, max-tasks-reached, and duplicate-loop branches.
        """
        total_tasks = len(self.task_manager.tasks)
        completed_tasks = sum(1 for t in self.task_manager.tasks if t.status == "completed")
        task_count = 0

        # Recovery path: when recover=True, _load_tasks() is not called
        # during plan() (which is skipped), so _all_tasks stays empty.
        # Load from disk now so _build_layers has a non-empty DAG.
        if not self._all_tasks and self.task_manager.tasks:
            self._load_tasks()

        if self.logger:
            self.logger.info("execution_started", "Starting task execution",
                             data={"total_tasks": total_tasks, "completed_tasks": completed_tasks,
                                   "max_tasks": max_tasks, "timeout": timeout})

        controller = self._get_provider_controller()

        # Session-local counter: how many times each task_id has been
        # marked completed in this execution session. Combined with the
        # same-id loop guard below, this breaks the re-execute-after-
        # complete dead-lock it causes: the task is re-run without
        # bound because the dispatcher never notices the previous
        # completion. The duplicate-by-title guard above
        # handles different-id-same-title; this handles same-id.
        self._session_task_completed_counts: dict = {}
        # 2nd re-scheduling of an already-completed task = loop. Using 2
        # rather than 1 leaves room for legitimate cross-recovery
        # reruns while still tripping well before the 23+ observed in
        # the wild.
        _SAME_ID_LOOP_THRESHOLD = 2

        while True:
            if max_tasks is not None and task_count >= max_tasks:
                print(f"\nReached maximum task limit: {max_tasks}")
                if self.logger:
                    self.logger.info("execution_stopped", f"Reached max task limit: {max_tasks}",
                                     data={"tasks_executed": task_count})
                break

            # Refresh the active set each iteration so mid-flight breakdowns
            # (which append child tasks to ``self._active_tasks`` /
            # ``task_manager.tasks``) and just-completed tasks (which
            # become terminal) are reflected before the next layer build.
            active_tasks = self._get_active_tasks_for_scheduling()

            if not active_tasks:
                if total_tasks > 0:
                    self.task_manager.set_stop_reason("success")
                    final_completed = sum(1 for t in self.task_manager.tasks if t.status == "completed")
                    print("\nAll tasks completed!")
                    if self.logger:
                        self.logger.info("execution_completed", "All tasks completed",
                                         data={"total": len(self.task_manager.tasks),
                                               "completed": final_completed})
                break

            # Build layers from the FULL task list (including completed
            # tasks) so downstream ``depends_on`` references resolve
            # correctly. If we filtered completed tasks out before the
            # build, a task whose upstream just finished would appear
            # to have dangling deps and the build would return ``[[[]]]``
            # — the dispatcher would then mistakenly exit as if the
            # plan were complete. The completed tasks are filtered out
            # again below when we pick the next micro-layer to run.
            outer_layers = _build_layers(self._all_tasks)

            # Pick the first non-empty micro-layer that contains at least
            # one non-terminal task. ``_build_layers`` now returns
            # ``outer layer -> micro layer -> tasks``; tasks within one
            # micro-layer can run concurrently, but micro-layers inside
            # an outer layer are serialised by the dispatcher (each
            # iteration processes exactly one micro-layer).
            current_micro_layer = None
            deferred_due_to_deps: list[str] = []
            for outer_layer in outer_layers:
                for micro_layer in outer_layer:
                    # Filter out completed tasks; the layer build saw them
                    # for dep resolution, but we must not re-execute them.
                    schedulable = []
                    for t in micro_layer:
                        if t.status in _TERMINAL_TASK_STATUSES:
                            continue
                        # Same-id loop guard: the
                        # ``same_id_loop_detected`` branch above adds
                        # looping tasks to ``_dispatcher_blocked`` so
                        # the dispatcher stops re-scheduling them. The
                        # ``active_tasks`` filter at the top of this
                        # while-True loop already honours that set, but
                        # the micro-layer rebuild below draws from
                        # ``self._all_tasks`` (built from disk, where
                        # ``save_tasks`` strips the ``status`` field),
                        # so the blocker check must be repeated here or
                        # the loop detector fires every iteration.
                        if t.id in self._dispatcher_blocked:
                            continue
                        # Principle 3 (user audit 2026-08-19): hard-gate on
                        # depends_on. ``is_dependency_ready`` returns
                        # ``(False, reason)`` for any non-success upstream
                        # status (pending / in_progress / failed). Without
                        # this filter, a task could be emitted into a layer
                        # before its upstream is actually ``completed`` —
                        # observed in an earlier run when the executor
                        # re-emitted downstream tasks while the upstream
                        # was still in retry (``status="in_progress"``).
                        ready, reason = is_dependency_ready(t, self._all_tasks)
                        if not ready:
                            deferred_due_to_deps.append(t.id)
                            if self.logger:
                                self.logger.debug(
                                    "task_deferred_dependency_not_ready",
                                    f"Task [{t.id}] '{t.title}' deferred: {reason}",
                                    task_id=t.id,
                                    data={"reason": reason},
                                )
                            continue
                        schedulable.append(t)
                    if schedulable:
                        current_micro_layer = schedulable
                        break
                if current_micro_layer is not None:
                    break

            if current_micro_layer is None:
                if self.logger:
                    self.logger.warning(
                        "execution_stopped",
                        "No schedulable micro-layer found",
                        data={
                            "active_task_ids": [t.id for t in active_tasks],
                            "deferred_due_to_deps": deferred_due_to_deps,
                        },
                    )
                break

            current_layer = current_micro_layer

            # Same-id re-run loop guard. The dispatcher previously only
            # caught loops where DIFFERENT task ids shared a title
            # (see ``find_completed_duplicate`` below). It silently
            # re-executed a task whose OWN id had already "completed"
            # in this session, repeatedly and without bound. Trip actions:
            #   1. When the session counter says the work really was
            #      committed, re-sync the persisted row to ``completed``
            #      and block the id — never overwrite legitimate work
            #      with ``failed`` just to stop the loop.
            #   2. Otherwise fall back to ``record_task_failure`` so the
            #      loop still terminates.
            #   3. ``continue`` so the next iteration rebuilds the layer
            #      graph from the refreshed view instead of dying.
            same_id_loop_tasks = [
                t for t in current_layer
                if self._session_task_completed_counts.get(t.id, 0) >= _SAME_ID_LOOP_THRESHOLD
            ]
            if same_id_loop_tasks:
                for t in same_id_loop_tasks:
                    count = self._session_task_completed_counts[t.id]
                    # The original code unconditionally called
                    # ``record_task_failure('same_id_re_run_loop')``,
                    # which overwrites the task's persisted
                    # ``status='completed'`` with ``status='failed'``.
                    # That left a permanent lie in the dashboard /
                    # task-sync card / counts endpoint — the task's
                    # code WAS committed successfully, but the SQLite
                    # ``plan_execution.task_progress`` row shows
                    # failed. Distinguish two cases now:
                    #   * ``status='completed'`` — the task really did
                    #     finish. Add to ``_dispatcher_blocked`` so the
                    #     layer builder stops re-scheduling it, but
                    #     keep the completed status intact.
                    #   * any other status — fall back to the legacy
                    #     ``record_task_failure`` path so the loop
                    #     still terminates.
                    current_status = getattr(t, "status", None)
                    # Principle 1 (user audit 2026-08-19): the in-memory
                    # ``SubTask.status`` may not be ``"completed"`` even
                    # though the session counter says the executor DID
                    # commit successfully — observed in an earlier run
                    # when ``_persist_status_to_sqlite`` failed (best-
                    # effort path) and SQLite hydrate returned
                    # ``"in_progress"`` / ``"pending"`` on the next layer
                    # rebuild. Trust the session counter as a secondary
                    # signal: if we got here via ``_session_task_completed_counts``
                    # the executor already wrote the code to git at least
                    # once. Re-sync the status (don't clobber with
                    # ``record_task_failure``) and add to the blocker set.
                    session_count = self._session_task_completed_counts.get(t.id, 0)
                    if current_status == "completed":
                        preserved_status = "completed"
                        detail = (
                            f"Task [{t.id}] '{t.title}' was already "
                            f"marked completed {count} times in this "
                            f"session and is being re-scheduled. "
                            f"Breaking the same-id re-run loop WITHOUT "
                            f"overwriting the completed status — the "
                            f"code was committed; only the dispatch "
                            f"side needs to stop."
                        )
                    elif session_count >= _SAME_ID_LOOP_THRESHOLD:
                        # In-memory session counter says the task DID
                        # complete at least once. SQLite hydrate drifted
                        # (likely a transient ``_persist_status_to_sqlite``
                        # failure). Re-sync the row to ``completed`` and
                        # block re-scheduling, rather than overwriting
                        # the legitimate completed work with ``failed``.
                        preserved_status = "completed"
                        detail = (
                            f"Task [{t.id}] '{t.title}' was completed "
                            f"{count} time(s) in this session (session "
                            f"counter={session_count}) but the persisted "
                            f"status drifted to {current_status!r}. "
                            f"Re-syncing to completed and breaking the "
                            f"same-id re-run loop WITHOUT marking failed "
                            f"so the legitimate committed work is not "
                            f"clobbered."
                        )
                        try:
                            self.task_manager.update_task_status(
                                t.id, "completed"
                            )
                        except Exception:
                            # Best-effort — the blocker add below is the
                            # primary safety net even if the re-sync
                            # write also fails.
                            pass
                    else:
                        preserved_status = current_status
                        detail = (
                            f"Task [{t.id}] '{t.title}' has been "
                            f"marked completed {count} times in this "
                            f"session but is still being scheduled. "
                            f"Breaking the same-id re-run loop; "
                            f"marking failed so it stops "
                            f"re-scheduling."
                        )
                    print(f"\n{'=' * 60}")
                    print(f"SAME-ID LOOP DETECTED — BREAKING CYCLE")
                    print(f"{'=' * 60}")
                    print(detail)
                    print(f"{'=' * 60}\n")
                    if preserved_status == "completed":
                        self._dispatcher_blocked.add(t.id)
                    else:
                        self.task_manager.record_task_failure(
                            t.id, "same_id_re_run_loop"
                        )
                    if self.logger:
                        self.logger.critical(
                            "same_id_loop_detected",
                            detail,
                            task_id=t.id,
                            data={
                                "completed_count": count,
                                "session_count": session_count,
                                "preserved_status": preserved_status,
                            },
                        )
                # Refresh in-memory from disk so subtasks written by
                # an earlier breakdown pass become schedulable.
                # Without this reload the dispatcher keeps scheduling
                # from the stale parent-only view forever.
                #
                # Same-id-loop fix: cap
                # the reload attempts. Without a cap, a stale
                # desc/depends_on disagreement produces
                # ``same_id_loop_reload_failed`` forever, burning
                # tokens without making progress. After
                # ``_MAX_RELOAD_FAILURES`` consecutive
                # reload failures we set a stop reason and break
                # out of the outer while-True.
                #
                # The retry/cap logic lives in
                # ``_handle_same_id_loop_reload`` so unit tests can
                # exercise it without spinning up the full async
                # main loop.
                if self._handle_same_id_loop_reload(
                    same_id_loop_tasks,
                ):
                    # Cap reached -- outer while-True must exit.
                    break
                failed_ids = {t.id for t in same_id_loop_tasks}
                current_layer = [t for t in current_layer if t.id not in failed_ids]
                if not current_layer:
                    continue

            # Duplicate detection — preserved from the legacy serial loop.
            # If any task in the layer duplicates an already-completed task
            # by title, the system has hit a circular loop it cannot
            # resolve on its own; stop and surface the failure for human
            # inspection.
            duplicate_pair = None
            for task in current_layer:
                duplicate = self.task_manager.find_completed_duplicate(task)
                if duplicate:
                    duplicate_pair = (task, duplicate)
                    break

            if duplicate_pair is not None:
                task, duplicate = duplicate_pair
                detail = (
                    f"Task [{task.id}] '{task.title}' duplicates already-completed "
                    f"Task [{duplicate.id}]. The system applied this fix before but the "
                    f"problem recurred, indicating it cannot be resolved automatically."
                )
                self.task_manager.set_stop_reason("repeated_failure", detail)
                print(f"\n{'=' * 60}")
                print(f"AUTONOMOUS CODING STOPPED — HUMAN INTERVENTION REQUIRED")
                print(f"{'=' * 60}")
                print(f"Task [{task.id}] '{task.title}' was already completed as "
                      f"Task [{duplicate.id}] but the problem has recurred.")
                print(f"\nReason recorded in tasks.json:")
                print(f"  {detail}")
                print(f"\nPlease inspect tasks.json and fix the underlying issue manually.")
                print(f"{'=' * 60}\n")
                if self.logger:
                    self.logger.critical("loop_detected", detail,
                                         task_id=task.id,
                                         data={"duplicate_id": duplicate.id, "title": task.title})
                break

            # Cap the layer size by the remaining max_tasks budget so we
            # don't schedule more tasks than the caller asked for.
            if max_tasks is not None:
                remaining = max_tasks - task_count
                if remaining <= 0:
                    break
                if remaining < len(current_layer):
                    current_layer = current_layer[:remaining]

            layer_task_ids = [t.id for t in current_layer]
            if self.logger:
                self.logger.info(
                    "layer_started",
                    f"Running layer with {len(current_layer)} task(s)",
                    data={"layer_size": len(current_layer), "task_ids": layer_task_ids},
                )

            # Schedule all layer tasks concurrently. Each coroutine
            # acquires a provider slot before running the synchronous
            # executor body in a worker thread, then releases it.
            coros = [
                self._run_task_with_provider_slot(task, controller, timeout)
                for task in current_layer
            ]
            results = await asyncio.gather(*coros, return_exceptions=True)
            task_count += len(coros)

            # Surface intra-layer exceptions to execution.log so they are
            # not swallowed by ``return_exceptions=True``. The task's own
            # ``_execute_task_with_retry`` already records its verdict;
            # this branch only fires for exceptions raised *outside* the
            # retry loop (typically controller / asyncio failures).
            for task, result in zip(current_layer, results):
                if isinstance(result, BaseException):
                    if self.logger:
                        self.logger.error(
                            "task_error",
                            f"Task [{task.id}] dispatcher raised: {result}",
                            task_id=task.id,
                            data={"error": str(result)[:500]},
                        )

            if self.logger:
                self.logger.info(
                    "layer_completed",
                    f"Completed layer with {len(current_layer)} task(s)",
                    data={"layer_size": len(current_layer), "task_ids": layer_task_ids},
                )

            # Roll up breakdown children into parent verdicts. The
            # method is a no-op for tasks whose parent has unfinished
            # siblings, so it's safe to call on every layer member.
            for task in current_layer:
                parent = self._find_parent_task(task)
                if parent is not None:
                    self._aggregate_breakdown_verdict(parent)

            total_tasks = len(self.task_manager.tasks)

    def _get_provider_controller(self) -> ProviderConcurrencyController:
        """Return the agent's lazily-constructed concurrency controller.

        Constructed on first access so tests can inject their own
        controller via ``agent._provider_controller = ...`` before
        calling :meth:`run`. The controller reads ``MAX_PARALLEL_TASKS``
        / ``PROVIDER_LIMITS`` from the environment at construction time
       — see :class:`ProviderConcurrencyController` for the env contract.
        """
        ctrl = getattr(self, "_provider_controller", None)
        if ctrl is None:
            ctrl = ProviderConcurrencyController()
            self._provider_controller = ctrl
        return ctrl

    def _resolve_provider_for_task(self, task: SubTask) -> str:
        """Return the provider name to charge against for ``task``.

        Resolution order (highest priority first):

          1. ``task.provider`` (per-task override set at plan time —
             used by PRD acceptance Case 2 to pin 6 same-layer tasks
             to the same provider so the per-provider semaphore is
             exercised).
          2. ``self.subagent_cfg.provider_name`` (the canonical source
             when the agent was constructed by ``autonomous_coding``).
          3. The ``PDT_PROVIDER_NAME`` env var (for tests / callers that
             skip the SubagentConfig path).
          4. The string ``"default"`` so the controller still applies
             its :data:`DEFAULT_PROVIDER_LIMIT` cap.
        """
        # Per-task override takes priority — a task can declare its
        # own provider even when the agent was constructed with a
        # different one. Empty / None / whitespace-only values are
        # treated as "not set" so the resolver falls through.
        task_provider = getattr(task, "provider", None)
        if isinstance(task_provider, str) and task_provider.strip():
            return task_provider.strip()
        cfg = getattr(self, "subagent_cfg", None)
        if cfg is not None:
            name = getattr(cfg, "provider_name", None)
            if name:
                return name
        env_name = os.environ.get("PDT_PROVIDER_NAME", "").strip()
        if env_name:
            return env_name
        return "default"

    def _handle_same_id_loop_reload(
        self, same_id_loop_tasks: list
    ) -> bool:
        """Run the same-id loop reload block (extracted for testing).

        The block refreshes the in-memory task list from disk so
        refiner-broken tasks become schedulable. It is also the
        source of the ``same_id_loop_reload_failed`` log cluster
        observed in production: a single task fired it repeatedly
        until this cap was added.

        Returns ``True`` if the cap was reached and the outer
        while-True should break; ``False`` otherwise.

        Args:
            same_id_loop_tasks: list of :class:`SubTask` instances
                that the same-id loop detector flagged this
                iteration. Currently unused at the function level
                (kept for forward compatibility with future per-task
                retry logic) but logged for audit.
        """
        _ = same_id_loop_tasks  # currently unused
        self._reload_failure_count = getattr(
            self, "_reload_failure_count", 0,
        )
        self._MAX_RELOAD_FAILURES = int(
            getattr(self, "_MAX_RELOAD_FAILURES", 3),
        )
        try:
            self._load_tasks()
            self._reload_failure_count = 0
            return False
        except Exception as exc:
            self._reload_failure_count += 1
            if self.logger:
                self.logger.error(
                    "same_id_loop_reload_failed",
                    f"Could not reload tasks after breaking "
                    f"same-id loop: {exc}",
                    data={
                        "error": str(exc)[:500],
                        "consecutive_failures":
                            self._reload_failure_count,
                        "max_failures":
                            self._MAX_RELOAD_FAILURES,
                    },
                )
            if self._reload_failure_count >= self._MAX_RELOAD_FAILURES:
                # Cap reached: stop the outer loop instead of
                # looping forever on a broken plan.
                if self.logger:
                    self.logger.critical(
                        "same_id_loop_reload_capped",
                        f"Reload failed "
                        f"{self._reload_failure_count} times "
                        f"consecutively; setting stop reason "
                        f"and breaking the outer loop.",
                        data={
                            "consecutive_failures":
                                self._reload_failure_count,
                        },
                    )
                self.task_manager.set_stop_reason(
                    "same_id_loop_reload_capped",
                    f"reload failed "
                    f"{self._reload_failure_count} times; "
                    f"desc/depends_on disagreement not "
                    f"auto-recoverable.",
                )
                return True
            return False

    def _get_active_tasks_for_scheduling(self) -> List[SubTask]:
        """Return the list of tasks eligible for scheduling, filtered.

        Prefers ``self._active_tasks`` when populated (the canonical
        scheduler view, refreshed by :meth:`_load_tasks` and
        :meth:`_breakdown_task`). Falls back to
        ``self.task_manager.tasks`` so the legacy plan-then-run /
        recover-then-run paths continue to work without an explicit
        ``_load_tasks`` call.

        In both cases the result excludes terminal-status tasks
        (``completed`` / ``failed`` / ``skipped``) so a re-derivation
        after a layer completes does not re-schedule work that just
        finished.
        Tasks in :attr:`_dispatcher_blocked` are also excluded — they
        are ``completed`` (or otherwise terminal) but the dispatcher
        loop detector tripped on them, so we keep the user-visible
        status intact while still preventing this session from
        scheduling them again. Without this filter the layer builder
        would re-emit them on the next iteration and trip the
        ``same_id_loop_detected`` guard every cycle until the user
        manually stops the run.
        """
        if self._active_tasks:
            return [
                t for t in self._active_tasks
                if t.status not in _TERMINAL_TASK_STATUSES
                and t.id not in self._dispatcher_blocked
            ]
        return [
            t for t in self.task_manager.tasks
            if t.status not in _TERMINAL_TASK_STATUSES
            and t.id not in self._dispatcher_blocked
        ]

    def _extract_target_files(self, task: SubTask) -> list[str]:
        """Files a task claims for the runtime conflict-check contract.

        Source order (2026-09-23):

        1. ``task.files_to_modify`` via
           :func:`_declared_modification_files` — the structured
           declaration, and the *same* input :func:`_build_micro_layers`
           uses to decide which tasks may share a micro layer. Reading
           it here keeps "these two conflict" a single definition; the
           helper's docstring records the run where the two
           definitions disagreed and a task was failed for a claim it
           never made.
        2. Only when (1) is unknown (absent / sentinel): explicit
           ``FILE: <path>`` markers, then a heuristic scan for
           relative source-file paths, so tasks that name files inline
           still participate in the conflict-check contract.

        The ``FILE:`` marker is an explicit machine-readable
        declaration and is honoured anywhere in the description. The
        heuristic scan is prose, so it is restricted to the
        ``## 修改位置`` section — mirroring ``TasksGenerator``'s
        ``_extraction_scope`` / ``_backfill_files_to_modify_from_description``
        in ``backend/tasks_generator.py``. A citation elsewhere
        ("由既有 ``tests/x.rs`` 承担，本任务不写") must not be read as a
        write claim; the generator learnt that on 2026-09-15, this
        guard had not.
        """
        declared = _declared_modification_files(task)
        if declared is not None:
            return declared

        files: list[str] = []
        pattern = re.compile(r"^FILE:\s*(.+)$", re.MULTILINE)
        for match in pattern.finditer(task.description or ""):
            path = match.group(1).strip()
            if path:
                files.append(path)
        if files:
            return files

        fallback = re.compile(
            r"\b([a-zA-Z0-9_./-]+\.(?:py|js|ts|jsx|tsx|java|kt|go|rs|c|cpp|h|hpp|cs|rb|php|swift|m|mm|ets|yaml|yml|json|toml|md))\b"
        )
        seen: set[str] = set()
        for match in fallback.finditer(_modification_section(task.description or "")):
            path = match.group(1).strip()
            if path and path not in seen and not path.startswith("http"):
                seen.add(path)
                files.append(path)
        return files

    def _pre_task_lock_hook(
        self, task: SubTask
    ) -> tuple[list[str], FileLockManager]:
        """Memory conflict check + OS lock acquisition for target files.

        The in-memory ``self._in_flight_files`` check runs first so two
        tasks claiming the same file fail fast without blocking on the OS
        lock — a same-layer overlap is a planning defect, and queueing on
        it would hide the defect rather than surface it.

        The declared files are then locked through the **broker** when one
        is running, so that the lock the sub-agent takes mid-task (for a
        file nobody declared) and the lock taken here land in the same
        table and cannot collide. ``FileLockManager`` remains the fallback
        for the paths that have no broker — unit tests constructing an
        agent directly, and standalone callers of this class — where the
        original single-process behaviour is still correct.

        Mixing the two for one file would be a bug rather than a
        redundancy: the broker holds raw ``fcntl`` locks and
        ``FileLockManager`` holds ``filelock`` locks, both on the same
        lock file, and within one process they conflict with each other.

        Returns:
            A tuple of ``(target_files, handle)``. The caller must pass
            these to :meth:`_post_task_lock_hook` in a ``finally`` block.

        Raises:
            RuntimeError: If any target file is already recorded in
                ``self._in_flight_files``, or if a declared file cannot be
                locked within the acquire timeout.
        """
        files = self._extract_target_files(task)
        broker = self._active_lock_broker()

        if not files:
            # A task that declares nothing can still take locks: the
            # Edit/Write hook acquires every file the sub-agent decides
            # to touch, keyed by task id. The handle returned here is the
            # *only* thing that lets the single release point reach them.
            #
            # Returning a bare ``FileLockManager`` (which holds nothing
            # and has never heard of the broker) leaked every such lock
            # for the broker's whole lifetime: a task declaring
            # ``files_to_modify: []`` took locks through the broker and
            # never released them, and its breakdown child then queued
            # behind one of them for the rest of the run. Audit-style
            # tasks are *the* common case for an empty declaration, so
            # this was not an edge case.
            if broker is None:
                return [], FileLockManager()
            return [], BrokerTaskHandle(broker, task.id)

        disable_in_flight_check = os.environ.get("PDT_DISABLE_IN_FLIGHT_CHECK") == "1"
        with self._in_flight_lock:
            if not disable_in_flight_check:
                for file_path in files:
                    if file_path in self._in_flight_files:
                        raise RuntimeError(
                            f"File conflict detected: {file_path} is already being "
                            f"modified by task {self._in_flight_files[file_path]}"
                        )
            for file_path in files:
                self._in_flight_files[file_path] = task.id

        if broker is not None:
            handle = BrokerTaskHandle(broker, task.id)
            try:
                # Sorted, and taken up front rather than one-at-a-time, so
                # the common multi-file case has a deterministic global
                # order — which is what keeps two tasks that claim
                # overlapping sets from deadlocking on each other.
                for file_path in sorted(files):
                    state = broker.acquire(
                        task.id, file_path, self._lock_acquire_timeout()
                    )
                    if state == TIMED_OUT:
                        raise RuntimeError(
                            f"File lock timeout after "
                            f"{self._lock_acquire_timeout():.0f}s: {file_path} "
                            f"is held by another task or process"
                        )
            except Exception:
                handle.release()
                with self._in_flight_lock:
                    for file_path in files:
                        self._in_flight_files.pop(file_path, None)
                raise
            return files, handle

        manager = FileLockManager()
        try:
            manager.acquire(files, str(self.project_dir), self._locks_dir)
        except Exception:
            # Roll back the in-flight memory state so the file is not
            # permanently blocked after a lock acquisition failure.
            with self._in_flight_lock:
                for file_path in files:
                    self._in_flight_files.pop(file_path, None)
            raise
        return files, manager

    def _resolve_locks_dir(self) -> Path:
        """Where this run's lock files live.

        Normally ``<plan_dir>/locks/`` — beside ``tasks.json``,
        ``prd.json`` and ``execution.log``. Two properties make that the
        right home: a plan's locks are per-plan runtime state that is
        meaningless once the plan is done, and the plan directory comes
        from the *server's* plans root, so nothing is written into the
        workspace being worked on. The workspace is someone else's git
        tree; ``git add -A`` checkpoints were committing lock files until
        this moved.

        The legacy executor layout points ``--tasks-file`` straight at
        ``<project_dir>/tasks.json``, so there is no plan directory. That
        case falls back to a workspace-derived directory outside the tree
        rather than creating a ``locks/`` folder in the workspace — the
        one outcome this must never produce.
        """
        plan_dir = Path(self.task_manager.tasks_file).parent
        try:
            same = plan_dir.resolve() == Path(self.project_dir).resolve()
        except OSError:
            same = False
        if same:
            return fallback_locks_dir(self.project_dir)
        return locks_dir_for_plan(plan_dir)

    def _lock_acquire_timeout(self) -> float:
        """Seconds to wait for a contended file lock.

        Read from the environment per call rather than cached so an
        operator can shorten it for a debugging run without restarting
        the executor. ``PDT_LOCK_ACQUIRE_TIMEOUT`` that does not parse is
        ignored in favour of the default: a typo must not turn every
        contended file into an immediate task failure.
        """
        raw = os.environ.get("PDT_LOCK_ACQUIRE_TIMEOUT", "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                value = 0.0
            if value > 0:
                return value
        return DEFAULT_ACQUIRE_TIMEOUT

    def _active_lock_broker(self) -> Optional[FileLockBroker]:
        """The broker if one is running, else ``None``.

        Deliberately does *not* start one. The broker's lifetime is the
        run's lifetime (see :meth:`run`), so a task dispatched outside a
        run — a direct hook call, an embedded use — must use the
        single-process fallback rather than quietly spawning an acceptor
        thread that nothing will reclaim.
        """
        with self._lock_broker_lock:
            broker = self._lock_broker
        if broker is not None and broker.available:
            return broker
        return None

    def _ensure_lock_broker(self) -> Optional[FileLockBroker]:
        """Start the file-lock broker once per agent, or return ``None``.

        ``None`` means the broker is not usable (already tried and failed,
        or the platform has no ``fcntl``); callers fall back to the
        single-process manager. The start is single-shot under a lock
        because every task in a layer reaches this concurrently and two
        binds of the same socket path would leave one of them serving a
        socket nobody connects to.
        """
        with self._lock_broker_lock:
            if self._lock_broker_started:
                return self._lock_broker
            self._lock_broker_started = True
            broker = FileLockBroker(
                self.project_dir,
                self._locks_dir,
                emit=self._emit_lock_event,
            )
            try:
                broker.start()
            except Exception as exc:
                if self.logger:
                    try:
                        self.logger.warning(
                            "file_lock_broker_start_failed",
                            f"File-lock broker unavailable "
                            f"({type(exc).__name__}: {exc}); falling back to "
                            f"per-process file locks",
                            data={"error": str(exc)[:300]},
                        )
                    except Exception:
                        pass
                return None
            self._lock_broker = broker
            return broker

    def _emit_lock_event(self, event: str, data: Dict[str, Any]) -> None:
        """Route a broker event into the execution log.

        Swallows its own failures: a broker that cannot log is still a
        working lock layer, and losing a diagnostic must never abort a
        task. The broker calls this from its acceptor and connection
        threads, so it must not assume the caller's logger is
        thread-safe beyond what the logger itself guarantees.
        """
        if not self.logger:
            return
        try:
            self.logger.info(event, event, data=data)
        except Exception:
            pass

    def _stop_lock_broker(self) -> None:
        """Release every lock this agent's broker holds and tear it down.

        Called from :meth:`run`'s ``finally`` so a finished run does not
        leave an unheld socket for the next process to adopt — an adopted
        broker would be one nobody is serving requests from.
        """
        with self._lock_broker_lock:
            broker = self._lock_broker
            self._lock_broker = None
            self._lock_broker_started = False
        if broker is not None:
            try:
                broker.stop()
            except Exception:
                pass

    def _post_task_lock_hook(
        self,
        task_id: str,
        files: list[str],
        manager: Union[FileLockManager, BrokerTaskHandle],
    ) -> None:
        """Release OS locks and clear in-flight memory state.

        ``manager`` is whatever :meth:`_pre_task_lock_hook` returned — a
        :class:`FileLockManager` on the no-broker fallback path, a
        :class:`BrokerTaskHandle` otherwise. Both expose ``release()``,
        and on the broker path that call also frees the files the
        sub-agent acquired *after* task start, which is the whole reason
        the handle is per-task rather than a plain file list.

        Safe to call from ``finally`` blocks: release failures are
        swallowed and the in-flight map is still cleared. This is the
        single release point for a task's locks — there is deliberately
        no PostToolUse counterpart, so a tool call that crashes leaves
        the lock held (correct: the task is still running) rather than
        dropping it while another edit is mid-flight.
        """
        try:
            manager.release()
        except Exception:
            pass
        with self._in_flight_lock:
            for file_path in files:
                if self._in_flight_files.get(file_path) == task_id:
                    self._in_flight_files.pop(file_path, None)

    async def _run_task_with_provider_slot(
        self,
        task: SubTask,
        controller: ProviderConcurrencyController,
        timeout: Optional[int],
    ) -> bool:
        """Acquire a provider slot + file locks, run the task, release.

        The synchronous :meth:`_execute_task_with_retry` body runs in
        the default thread pool via :func:`loop.run_in_executor`, so a
        slow task does not block the event loop or the other
        coroutines in the same :func:`asyncio.gather`.

        File-lock acquisition uses the pre-task hook (memory conflict
        check + OS-level locks) and the post-task hook releases the
        locks and clears the in-flight memory state. Both run inside
        the ``finally`` so an exception or cancellation still cleans up.
        """
        provider = self._resolve_provider_for_task(task)
        await controller.acquire(provider)
        files: list[str] = []
        lock_manager = FileLockManager()
        try:
            files, lock_manager = self._pre_task_lock_hook(task)

            def _execute_in_task_ctx():
                # Usage-registry attribution (2026-09-21): bind this
                # task's id for every LLM call the executor makes. The
                # plan id resolves via the PDT_PLAN_ID env var that
                # server.py sets on the executor subprocess.
                with plan_usage_context(task_id=task.id):
                    return self._execute_task_with_retry(
                        task,
                        max_retries=self.config.max_retries,
                        timeout=timeout,
                    )

            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _execute_in_task_ctx)
        finally:
            self._post_task_lock_hook(task.id, files, lock_manager)
            controller.release(provider)

    def _build_files_to_modify_hint(self, task: SubTask) -> str:
        """
        Build a concise files-to-modify hint for the coder prompt.

        No-bulk-context contract (a 2026-08-19 production plan):
          * The subagent does NOT receive a repository-wide code
            snapshot. It discovers context on demand.
          * We only emit a small bulleted list of the relative paths
            the task's ``files_to_modify`` declares as in-scope.
          * The unknown-modification sentinel
            (``["__UNKNOWN_MODIFICATIONS__"]``) is rendered as a
            human-readable phrase — never as the raw literal —
            because telling the subagent "you may modify unknown
            files" is meaningless.
          * An explicit empty list ``[]`` emits no hint at all
            (the task is read-only; there is nothing to enumerate).

        Sentinel/empty semantics are preserved exactly as
        :class:`SubTask` models them; this method only decides
        *how* to render the value into a prompt, never *what*
        the value means.
        """
        files = getattr(task, "files_to_modify", None)
        # Defensive: a missing attribute is treated like the
        # sentinel — produce the "no files declared" phrase.
        if files is None:
            return "\nFiles likely in scope: no files declared (use Read/Grep/Glob to explore).\n"
        if files == []:
            # Explicit empty list — the task author signalled
            # read-only. Emit no hint.
            return ""
        if files == UNKNOWN_MODIFICATIONS_SENTINEL or any(
            not isinstance(f, str) or f.startswith("__") for f in files
        ):
            # Sentinel or sentinel-shaped list. Render a
            # human-readable phrase; never leak the literal token.
            return "\nFiles likely in scope: no files declared (use Read/Grep/Glob to explore).\n"
        # Concrete list of paths.
        bullets = "\n".join(f"  - {f}" for f in files)
        return f"\nFiles likely in scope:\n{bullets}\n"

    #: Cap on how much of a task's persisted ``failure_reason`` reaches
    #: the prompt. ``record_task_failure`` already truncates to 1000
    #: chars; this second cap keeps an aggregated parent reason (which
    #: concatenates its children's) from crowding out the task itself.
    _PRIOR_FAILURE_MAX_CHARS = 1200

    def _build_prior_failure_block(self, task: SubTask) -> str:
        """Render the task's persisted ``failure_reason`` for the prompt.

        The first attempt of a re-dispatched
        task used to start with no failure context at all. Two things
        made that blind spot:

          * ``RetryManager`` — the only other carrier of failure
            context — is in-memory, keyed on ``task_id``, and the
            executor consults it only when ``attempt > 0``. So the
            first attempt of a dispatch never saw it, and the state
            died with the process.
          * The dispatcher rebuilds its micro-layers from
            ``self._all_tasks``, which is read from disk where
            ``save_tasks`` strips ``status``. A failed task therefore
            looks ``pending`` again and gets re-scheduled — with a
            clean prompt, as if the previous failure had never
            happened.

        ``task.failure_reason`` is hydrated from ``plan_tasks`` at load
        time (see ``_load_tasks``), so it survives both the disk strip
        and a process restart. Injecting it here is what turns a
        re-dispatch into an informed retry instead of a faithful replay
        of the attempt that already failed.

        Returns an empty string when no failure has been recorded.
        """
        reason = (getattr(task, "failure_reason", None) or "").strip()
        if not reason:
            return ""
        if len(reason) > self._PRIOR_FAILURE_MAX_CHARS:
            reason = reason[: self._PRIOR_FAILURE_MAX_CHARS] + " …[truncated]"
        return (
            "\n\n[PREVIOUS ATTEMPT FAILED]\n"
            "A previous run of this exact task did not complete. The "
            "reason the executor recorded was:\n"
            f"{reason}\n"
            "Address that failure specifically rather than repeating the "
            "approach that produced it.\n"
            "If the recorded reason shows the TASK ITSELF is wrong "
            "(missing scope, a test command that cannot pass, "
            "contradictory requirements), say so explicitly and explain "
            "why — do not claim success to get past it.\n"
        )

    def _execute_task_with_retry(self, task: SubTask, max_retries: int = 5, timeout: Optional[int] = None) -> bool:
        """
        Execute a task with retry mechanism.

        Args:
            task: Task to execute
            max_retries: Maximum number of retry attempts per run
            timeout: Timeout for AI queries

        Returns:
            True if task completed successfully, False if it failed.
        """
        # Per-task SubagentConfig tmpfile regeneration (decisions 2/3 + 4).
        # Each task in the run() loop gets a fresh ``/tmp/subagent_settings_<uuid>.json``
        # so the Claude subprocess launched for THIS task is wired with
        # its own ``--settings`` tmpfile (env block + hooks payload) and
        # ``CLAUDE_SETTINGS_PATH`` env var for hook correlation. Without
        # this, every task in a run would share one tmpfile, breaking
        # cross-process correlation in execution.log when two tasks run
        # close together. ``write_tmp_settings`` is idempotent in
        # producing a new path each call (uuid4).
        if getattr(self, "subagent_cfg", None) is not None:
            try:
                new_settings_path = self.subagent_cfg.write_tmp_settings(
                    logger=self.logger
                )
                self.coding_tool.settings = new_settings_path
            except OSError:
                # /tmp unwritable — fall through with whatever
                # settings were last on the coding_tool. We do not
                # want a transient FS error to abort the task
                # loop; the next attempt will retry the write.
                pass

        task_start_time = time.monotonic()

        # 2026-08-24 dual-criterion rule, per the project's CLAUDE.md:
        # Before spinning up the subagent, run the task's declared
        # ``test_command`` first. If it exits 0 on the current
        # project state, the work the task was supposed to do is
        # already in place — skip the subagent entirely, record the
        # task as completed with ``_skip_reason`` set to
        # ``test_command_preflight_passed``, and let the dispatcher
        # move on. This is what the user calls the "skip-already-done"
        # mechanism: a task whose test gate is green should not pay
        # the cost of another subagent round-trip.
        #
        # The check is intentionally conservative: it skips only when
        # ``test_command`` is non-empty AND ``project_dir`` exists
        # AND the command exits 0 within 60 seconds. Any failure mode
        # falls through to the normal subagent path.
        preflight_skip = self._preflight_test_command_skip(task)
        if preflight_skip is not None:
            # Either skipped (True) or failed (False / Exception).
            if self.logger:
                self.logger.info(
                    "task_preflight_test_command",
                    f"Task [{task.id}] pre-flight test_command "
                    f"{'PASSED' if preflight_skip else 'FAILED'}",
                    task_id=task.id,
                    data={
                        "skip": bool(preflight_skip),
                        "test_command": getattr(task, "test_command", "") or "",
                    },
                )
            if preflight_skip:
                print(
                    f"Pre-flight test_command passed for task "
                    f"[{task.id}]; skipping subagent invocation."
                )
                self.task_manager.update_task_status(task.id, "completed")
                return True
            # preflight failed — fall through to the normal subagent
            # path; the executor will run the test again after the
            # implementation lands.

        for attempt in range(max_retries):
            try:
                print(f"\n--- Processing Task [{task.id}] (Attempt {attempt + 1}/{max_retries}): {task.title} ---")
                if self.logger:
                    self.logger.info("task_started", f"Processing Task [{task.id}] attempt {attempt + 1}/{max_retries}",
                                     task_id=task.id, phase="task_execution",
                                     data={"title": task.title, "attempt": attempt + 1,
                                           "max_retries": max_retries})
                self.task_manager.update_task_status(task.id, "in_progress")

                # Get context and add retry modifier if this is a retry.
                # No-bulk-context contract (a 2026-08-19 production plan):
                # do NOT inject ``get_file_context()`` (a repository-wide
                # snapshot of up to 300K chars) here. The subagent
                # discovers context on demand via its own Read/Grep/Glob
                # tools. We only emit a *concise* hint listing the
                # files the task's ``files_to_modify`` declares as
                # in-scope; the unknown-modification sentinel is
                # rendered as a human-readable phrase, not the raw
                # literal; an explicit empty list emits no hint.
                context = f"Subtask: {task.description}\n"
                context += self._build_files_to_modify_hint(task)
                context += "\nUse Read/Grep/Glob tools to discover the codebase as needed.\n"

                # Failure context, from two different carriers covering
                # two different gaps:
                #
                #   * ``attempt == 0`` — the first attempt of a
                #     *re-dispatched* task. Nothing else supplies the
                #     previous failure here, so a task that already
                #     failed and got re-scheduled replayed the same
                #     attempt blind.
                #   * ``attempt > 0`` — an in-run retry. ``RetryManager``
                #     covers this and is often the richer carrier (it
                #     holds the in-run history and the actionable
                #     empty-diff hint), so the persisted reason is not
                #     duplicated on top of it.
                if attempt == 0:
                    context += self._build_prior_failure_block(task)

                # Add retry context if this is a retry attempt within the current run
                if attempt > 0:
                    # Pass this loop's real bound so the rendered
                    # ``Attempt X of N`` matches the loop. Without it the
                    # text came from ``RetryManager.MAX_RETRIES``, a
                    # hard-coded 5 that disagreed with the 2 the loop
                    # actually runs.
                    retry_modifier = self.retry_manager.get_retry_prompt_modifier(
                        task.id, max_retries
                    )
                    context += retry_modifier

                # Use config's executor prompt
                coder_prompt = self.config.executor_system_prompt

                # Clean test command to remove misleading echo suffixes
                clean_test_cmd = self._clean_test_command(task.test_command)

                # 2026-09-16: surface a structurally-unpassable command
                # BEFORE the attempt runs. A task in an earlier plan
                # spent two retry cycles plus a refiner pass being judged
                # against a probe chain that could never exit 0 — every
                # ``TEST_RESULT: PASSED`` it reported was scored as a lie.
                # Warning-only here: the command already exists and the
                # work still has to happen; refusing to dispatch would
                # strand the task. Generation-time paths
                # (``tasks_generator``, ``repair_generator``) are where a
                # flagged command is actually discarded.
                _cmd_issues = inspect_test_command(clean_test_cmd)
                if _cmd_issues and self.logger:
                    try:
                        self.logger.warning(
                            "task_test_command_unusable",
                            (
                                f"Task [{task.id}] test_command looks "
                                f"structurally unable to exit 0 "
                                f"({_cmd_issues[0].code}): "
                                f"{_cmd_issues[0].detail}"
                            ),
                            task_id=task.id,
                            data={
                                "code": _cmd_issues[0].code,
                                "detail": _cmd_issues[0].detail,
                                "test_command": clean_test_cmd[:500],
                                "attempt": attempt + 1,
                            },
                        )
                    except Exception:
                        pass

                # Instruct the AI to run tests itself and report results.
                #
                # 2026-09-20 (post-mortem): a task with no
                # ``test_command`` used to get this same instruction with
                # an *empty* command slot — "YOU MUST run the test command
                # yourself:  " — which the subagent could only satisfy by
                # inventing a result. Repair tasks are that population
                # (``repair_generator`` stopped emitting test_commands in
                # the 2026-09-07 content-only refactor), and with no
                # command there is also no exit code for
                # ``_cross_verify_test_result`` to agree with, so the only
                # check left was the ``claude -p`` second pass — which on
                # that run saw answers like "the prior round already applied
                # this, EXIT_CODE=0" and rejected them, correctly, six
                # times running.
                #
                # The user's read: an agent that has no command handed to
                # it can still verify its own work. So instead of an empty
                # slot, ask it to construct the check itself and paste the
                # real terminal output. That gives the second pass
                # something falsifiable to review instead of prose.
                if clean_test_cmd:
                    test_instruction = f"""
After implementing the code, YOU MUST run the test command yourself:
  {clean_test_cmd}

Observe the actual test output and determine whether tests passed or failed.
Do NOT rely on any echo statements in the command — look at the actual pytest results.

At the very end of your response, on its own line, output the test result in this exact format:

TEST_RESULT: PASSED

or

TEST_RESULT: FAILED
REASON: <brief explanation of what failed>

CRITICAL: Your response MUST end with a TEST_RESULT line. If you do not include this marker,
the system will assume the tests failed. Do not claim tests passed without actually running the command.
"""
                else:
                    test_instruction = """
This task has NO test_command — no external command will be run to judge your
work, and nothing outside this conversation can confirm it. You must therefore
verify your own work, and show the evidence.

After implementing the change:

  1. Restate, from the task description above, the exact acceptance criteria
     you are claiming to satisfy. One line each.
  2. For EACH criterion, actually run a command that demonstrates it against
     the CURRENT state of the tree — a targeted pytest, a python -c snippet, a
     curl against the running service, a grep with line numbers. Check out the
     changed file again if you need to; do not reason about what you remember
     writing.
  3. Paste the exact command you ran and its real, unedited output. A claim
     without its terminal output does not count as verification.

Two things that do NOT count, and that a reviewer will reject:

  * "This was already applied in a previous round / commit" — say that only
    with the output of a command run just now against the current tree.
  * An exit code with no output. `EXIT_CODE=0` alone proves nothing about
    whether the criterion holds.

At the very end of your response, on its own line, output the test result in this exact format:

TEST_RESULT: PASSED

or

TEST_RESULT: FAILED
REASON: <brief explanation of what could not be demonstrated>

CRITICAL: Your response MUST end with a TEST_RESULT line. If you do not include this marker,
the system will assume the tests failed. Do not claim PASSED without pasted command output.
"""
                context += test_instruction

                print("Coding tool is coding...")
                coder_response = None

                # Determine if we should use background execution
                previous_timeout = self.executor.had_previous_timeout(task.id)

                try:
                    if previous_timeout:
                        print("Previous timeout detected, using background execution...")
                        if self.logger:
                            self.logger.info("task_background_mode", "Using background execution due to previous timeout",
                                             task_id=task.id)
                        coder_response = self.coding_tool.query(
                            context,
                            system_instruction=coder_prompt,
                            # 2026-09-15: the execution-phase
                            # LLM call uses the SAME unified budget as every
                            # other call — 900s of stdout silence / 1800s idle
                            # pipe / the layer's 1-hour ceiling — so no
                            # per-call ``timeout`` is passed on either branch.
                            # ``EXECUTOR_TASK_TIMEOUT`` no longer caps the LLM
                            # call (see the note at its definition).
                            model_type=task.model_type,
                            scene="execution",
                        )
                    else:
                        coder_response = self.coding_tool.query(
                            context,
                            system_instruction=coder_prompt,
                            model_type=task.model_type,
                            scene="execution",
                        )
                except HardTimeoutError as e:
                    # 2026-09-08: inner or outer cap fired on the task.
                    # Same auto-split policy as VPs: any task that hits the
                    # 1-hour cap should be re-planned by the refiner. The
                    # generic ``except TimeoutError`` below keeps handling
                    # non-wall-clock timeouts (e.g. asyncio.wait_for at the
                    # orchestration layer) without auto-split.
                    #
                    # HardTimeoutError IS-A TimeoutError (builtin), so this
                    # branch MUST come BEFORE ``except TimeoutError`` to
                    # intercept it. Otherwise the generic arm would treat
                    # it as a regular retryable timeout.
                    print(f"Task hit HARD TIMEOUT: {e}")
                    if self.logger:
                        self.logger.warning(
                            "task_hard_timeout",
                            f"Task [{task.id}] hit HARD TIMEOUT: {e}",
                            task_id=task.id,
                            data={
                                "timeout_sec": timeout or 300,
                                "attempt": attempt + 1,
                                "kind": "HARD_TIMEOUT",
                                "total_sec": getattr(e, "total_sec", None),
                                "elapsed": getattr(e, "elapsed", None),
                            },
                        )
                    self.executor.record_timeout(task.id, timeout or 300)
                    self.retry_manager.record_attempt(task.id, str(e), False)
                    error_msg = str(e)

                    if attempt < max_retries - 1:
                        print("Retrying...")
                        if self.logger:
                            self.logger.info(
                                "task_retry",
                                f"Retrying task [{task.id}] after HARD TIMEOUT",
                                task_id=task.id,
                                data={"attempt": attempt + 1, "error": str(e)[:200]},
                            )
                        continue

                    print("Max retries exhausted on hard timeout; delegating split to refiner...")
                    try:
                        self._refine_after_failure(
                            task=task,
                            coder_response="",
                            result_context=error_msg,
                            file_context="",
                            exit_code=1,
                        )
                        # 2026-09-08: auto-split succeeded — transition
                        # plan state so the orchestrator re-runs the new
                        # task list on next dispatch. ``executing → ready``
                        # is allowed per PHASE_TRANSITIONS.
                        plan_state = getattr(self, "plan_state", None)
                        if plan_state is not None:
                            try:
                                plan_state.reload()
                                plan_state.transition_to("ready")
                                if self.logger:
                                    self.logger.info(
                                        "task_hard_timeout_ready",
                                        f"task={task.id} auto-split → plan transitioned to ready",
                                        task_id=task.id,
                                    )
                            except Exception as trans_exc:
                                if self.logger:
                                    self.logger.warning(
                                        "task_hard_timeout_ready_failed",
                                        f"could not transition to ready after "
                                        f"task={task.id} split: {trans_exc}",
                                        task_id=task.id,
                                    )
                    except Exception as refine_exc:
                        if self.logger:
                            try:
                                self.logger.warning(
                                    "refine_after_breakdown_failed",
                                    f"Refiner delegation after task [{task.id}] "
                                    f"hard timeout raised: {refine_exc}",
                                    task_id=task.id,
                                    data={"error": str(refine_exc)[:500]},
                                )
                            except Exception:
                                pass
                    self.task_manager.record_task_failure(task.id, error_msg)
                    duration = int(time.monotonic() - task_start_time)
                    if self.logger:
                        self.logger.error(
                            "task_failed",
                            f"Task [{task.id}] failed after {max_retries} hard timeouts",
                            task_id=task.id,
                            data={"error": error_msg[:500], "duration_sec": duration,
                                  "attempts": max_retries},
                        )
                    return False
                except TimeoutError as e:
                    print(f"Task timed out: {e}")
                    if self.logger:
                        self.logger.warning("task_timeout", f"Task [{task.id}] timed out: {e}",
                                            task_id=task.id,
                                            data={"timeout_sec": timeout or 300, "attempt": attempt + 1})
                    self.executor.record_timeout(task.id, timeout or 300)
                    self.retry_manager.record_attempt(task.id, str(e), False)
                    error_msg = str(e)

                    if attempt < max_retries - 1:
                        print("Retrying...")
                        if self.logger:
                            self.logger.info("task_retry", f"Retrying task [{task.id}] after timeout",
                                             task_id=task.id, data={"attempt": attempt + 1, "error": str(e)[:200]})
                        continue

                    print("Max retries reached after timeouts; delegating split to refiner...")
                    # PRD decision point 1: structural task-list changes
                    # (add / remove / reorder) are the refiner's job, not
                    # the dispatcher's. The dispatcher's only contract on
                    # failure is to mark the task failed and let the
                    # refiner re-plan the remaining work. The call below
                    # is intentionally best-effort and never raises — a
                    # refiner failure must not mask the underlying task
                    # failure that already returned False.
                    try:
                        self._refine_after_failure(
                            task=task,
                            coder_response="",
                            result_context=error_msg,
                            file_context="",
                            exit_code=1,
                        )
                    except Exception as refine_exc:
                        if self.logger:
                            try:
                                self.logger.warning(
                                    "refine_after_breakdown_failed",
                                    f"Refiner delegation after task [{task.id}] "
                                    f"failure raised: {refine_exc}",
                                    task_id=task.id,
                                    data={"error": str(refine_exc)[:500]},
                                )
                            except Exception:
                                pass
                    self.task_manager.record_task_failure(task.id, error_msg)
                    duration = int(time.monotonic() - task_start_time)
                    if self.logger:
                        self.logger.error("task_failed", f"Task [{task.id}] failed after {max_retries} timeouts",
                                          task_id=task.id,
                                          data={"error": error_msg[:500], "duration_sec": duration,
                                                "attempts": max_retries})
                    return False

                if coder_response is None:
                    print("No response from coding tool.")
                    error_msg = "No response from coding tool"
                    self.retry_manager.record_attempt(task.id, error_msg, False)
                    if self.logger:
                        self.logger.warning("task_retry", f"No response from coding tool for task [{task.id}]",
                                            task_id=task.id, data={"attempt": attempt + 1})
                    if attempt < max_retries - 1:
                        continue
                    self.task_manager.record_task_failure(task.id, error_msg)
                    if self.logger:
                        self.logger.error("task_failed", f"Task [{task.id}] failed: no response from coding tool",
                                          task_id=task.id, data={"attempts": max_retries})
                    return False

                # 2026-09-17 (C2): a blank response is a provider/infra
                # fault, not a consumed attempt. Re-query before scoring
                # it (see ``_retry_empty_query``).
                if coder_response is not None and not coder_response.strip():
                    coder_response = self._retry_empty_query(
                        context, coder_prompt, task, attempt + 1,
                    )

                # 2026-09-17 (C1): persist the raw subagent conclusion for
                # every attempt, BEFORE any verdict is derived from it.
                # Failed attempts used to discard their 5k-char reports
                # with the executor's memory; the plan-local
                # ``agent_outputs/`` dir is the postmortem source.
                self._persist_agent_output(task.id, attempt + 1, coder_response)

                # Apply file changes
                files_to_write = self.parse_files_from_response(coder_response)
                changed_files = []
                if files_to_write:
                    for path, content in files_to_write.items():
                        full_path = self.project_dir / path
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(full_path, "w") as f:
                            f.write(content)
                        changed_files.append(path)
                        print(f"Wrote {path}")
                else:
                    # Claude Code may have written files directly — detect via git
                    git_changed = self.git_manager.get_changed_files()
                    if git_changed:
                        changed_files = git_changed
                        print(f"Detected {len(git_changed)} files changed by coding tool directly.")
                    else:
                        print("No file changes provided.")

                # Anti-cheat: cross-verify the AI's TEST_RESULT claim by
                # re-running the test command in the task's project_dir.
                # The AI's text claim is taken as advisory only — the
                # ground truth is the actual pytest exit code. This blocks
                # the failure mode where the subagent writes
                # "TEST_RESULT: PASSED" without ever running the test.
                ai_claimed_passed, ai_reason = self._parse_test_result(coder_response)
                tests_passed, test_reason = self._cross_verify_test_result(
                    task=task,
                    ai_claimed_passed=ai_claimed_passed,
                    ai_reason=ai_reason,
                    coder_response=coder_response,
                )

                # 2026-08-24 dual-criterion rule, second-pass audit
                # (the project's CLAUDE.md). For audit-style tasks
                # (declared ``verification_only=True`` or detected
                # by ``_looks_like_audit_task``), the first-pass
                # cross_verify + AI claim can still let a
                # plausible-sounding but wrong answer pass. Run a
                # fresh ``claude -p`` adversarial review against the
                # spec and the subagent's report; if it rejects the
                # answer, downgrade ``tests_passed`` so the task takes
                # the canonical failure path. The second pass is a
                # tightening on top of an already-passing gate — a
                # no-op for non-audit tasks, which is why it is gated
                # on ``tests_passed`` and not worth an LLM round-trip
                # on a task whose tests already failed.
                #
                # 2026-09-20 (post-mortem): this call used to sit
                # INSIDE the ``if tests_passed:`` block below, with the
                # downgrade right beneath it. That made the downgrade a
                # **dead store**: the branch had already been chosen, so
                # ``tests_passed = False`` could not move control flow to
                # the ``else`` that retries, and the empty-output gate in
                # between never reads ``tests_passed`` at all (it reads
                # ``changed_files`` and ``is_audit`` only). A rejection
                # therefore reached the second pass's verdict — the task
                # was refused by it (``task_audit_second_pass_failed``)
                # and still logged ``task_completed``. Hoisted above
                # the branch so a rejection actually fails the task.
                if tests_passed:
                    audit_passed, audit_reason = self._audit_task_second_pass(
                        task=task,
                        coder_response=coder_response,
                    )
                    if not audit_passed:
                        tests_passed = False
                        test_reason = audit_reason

                if tests_passed:
                    print("Tests passed!")

                    # Empty-output gate (2026-08-19, an earlier audit).
                    #
                    # ``tests_passed`` alone is NOT sufficient proof of
                    # completion: for a task with an empty / trivially-true
                    # ``test_command`` (e.g. the read-only investigation
                    # tasks #1/#2/#20-1, whose ``test_command`` was ``""``),
                    # ``_cross_verify_test_result`` falls back to the AI's
                    # self-reported claim and a subagent that produced an
                    # EMPTY diff still sails through to ``completed`` —
                    # then gets re-scheduled, re-runs, and re-completes on
                    # an empty commit, burning tokens until the same-id
                    # loop guard trips. This is the root cause behind both
                    # "review_blocked then completed" and "empty commit
                    # marked completed".
                    #
                    # 2026-08-24 audit: the previous code treated
                    # "no files changed" as a hard-failure signal. That
                    # is wrong for **audit-style tasks** (路径审计,
                    # 内容审计, 路径契约, 定位 grep 行号, smoke 探针,
                    # 性能基准) whose declared deliverable is a written
                    # report / answer rather than a code diff. A task
                    # whose test_command runs to exit 0 on the existing
                    # code is doing exactly the audit it was asked to
                    # perform. Treating "no diff" as failure just makes
                    # the plan strand on a token-burning retry loop.
                    #
                    # New contract (per the project's CLAUDE.md, the
                    # "task completion dual-criterion" rule):
                    #
                    #   1. If the task is audit-style (declared via
                    #      ``verification_only=True`` or auto-detected
                    #      from the description by
                    #      ``_looks_like_audit_task``), an empty diff is
                    #      accepted with a WARNING log. The task is
                    #      completed based on cross_verify + agent
                    #      output alone.
                    #   2. Otherwise, the empty-diff signal is still
                    #      recorded as a WARNING (for operators to grep
                    #      later) but the retry / fail hard path is
                    #      removed. The actual pass/fail signal is the
                    #      same cross_verify + test_command exit code
                    #      that already gates every task.
                    #
                    # 审计类 task 双判断：test_command 退出码 +
                    # Claude -p 二次对比 spec 完成度.
                    is_audit = (
                        getattr(task, "verification_only", False)
                        or self._looks_like_audit_task(task)
                    )
                    if not changed_files and not self._task_declared_files_exist(task):
                        if is_audit:
                            if self.logger:
                                self.logger.info(
                                    "task_audit_no_diff_accepted",
                                    f"Task [{task.id}] is audit-style "
                                    f"(verification_only={getattr(task, 'verification_only', False)}); "
                                    f"empty diff accepted per 2026-08-24 dual-criterion rule. "
                                    f"Completion decided by cross_verify + test_command exit code.",
                                    task_id=task.id,
                                    data={"attempt": attempt + 1, "audit": True},
                                )
                            print(
                                f"Empty output accepted (audit task): "
                                f"task [{task.id}]"
                            )
                            # fall through to cross_verify + test_command
                            # evaluation below — no continue, no retry
                        else:
                            # 2026-09-11:
                            # empty-diff is a real failure, NOT a soft
                            # warning that falls through to cross_verify.
                            # The "late re-run" exception (legitimate re-run
                            # after a prior attempt) is still handled by
                            # ``_task_declared_files_exist`` returning True
                            # above; this branch only runs when the
                            # declared files do NOT exist on disk, i.e.
                            # the subagent produced nothing of value.
                            # The old behaviour was warn + fall through,
                            # which let ``git_manager.commit`` emit an
                            # empty commit + mark the task completed —
                            # the canonical shape: a handful of tasks
                            # deferred by their upstream, each getting an
                            # empty commit.
                            #
                            # Now: ``record_attempt`` so the next retry
                            # sees an actionable hint
                            # (``get_retry_prompt_modifier`` reads the
                            # ``empty_diff_no_changes`` prefix and emits
                            # "use Edit/Write tools" guidance). Stop only
                            # after the configured ``max_retries`` are
                            # exhausted (default 5) so the subagent has a
                            # fair chance to self-correct.
                            error_msg = (
                                f"empty_diff_no_changes: task [{task.id}] "
                                f"produced no file modifications and no "
                                f"prior deliverable exists on disk. You "
                                f"must use Edit/Write tools to actually "
                                f"modify the source files declared in "
                                f"files_to_modify."
                            )
                            self.retry_manager.record_attempt(
                                task.id, error_msg, False,
                            )
                            if self.logger:
                                self.logger.warning(
                                    "task_empty_diff_no_changes",
                                    f"Task [{task.id}] produced no changes; "
                                    f"recording attempt failure and forcing "
                                    f"retry (attempt {attempt + 1}/"
                                    f"{max_retries}).",
                                    task_id=task.id,
                                    data={"attempt": attempt + 1,
                                          "max_retries": max_retries},
                                )
                            print(
                                f"Empty output (retry forced): "
                                f"task [{task.id}] attempt "
                                f"{attempt + 1}/{max_retries}"
                            )
                            if attempt < max_retries - 1:
                                # The next loop iteration will call
                                # ``get_retry_prompt_modifier(task.id,
                                # max_retries)`` and inject the actionable
                                # empty-diff hint into the subagent's
                                # system prompt. We do NOT ``continue``
                                # here because we're inside an inner
                                # ``else`` — the outer ``for attempt in
                                # range(max_retries)`` loop will iterate.
                                continue
                            # All in-run retries exhausted on the
                            # empty-diff path — same downstream treatment
                            # as the test-failed branch: refine the task
                            # list once, mark as failed, return False.
                            self._refine_after_failure(
                                task=task,
                                coder_response="",
                                result_context=error_msg,
                                file_context="",
                                exit_code=1,
                            )
                            self.task_manager.record_task_failure(
                                task.id, error_msg,
                            )
                            duration = int(
                                time.monotonic() - task_start_time,
                            )
                            if self.logger:
                                self.logger.error(
                                    "task_failed",
                                    f"Task [{task.id}] failed after "
                                    f"{max_retries} empty-diff attempts: "
                                    f"{error_msg[:200]}",
                                    task_id=task.id,
                                    data={
                                        "error": error_msg[:500],
                                        "duration_sec": duration,
                                        "attempts": max_retries,
                                        "reason": "empty_diff_no_changes",
                                    },
                                )
                            return False

                    # Inline spec+code self-review (DP3).
                    # The cross-verify layer proves the test command
                    # exits 0, but a subagent can still write code that
                    # "passes" the test while completely omitting the
                    # spec'd behaviour (e.g. "implement password
                    # hashing" → stores plaintext, test only checks
                    # ``register_user`` returns a user object). This
                    # branch is the lightweight adversarial safety net
                    # that catches such semantic deviations before the
                    # code lands in git history.
                    #
                    # Skip the review when the git diff is empty: with
                    # no diff context the LLM can only grade on the
                    # spec text alone, which the test layer has
                    # already proven. Burning a token round-trip on
                    # an empty diff is wasteful and adds latency.
                    git_diff_stat = self._get_git_diff_stat_for_review(task)
                    review_should_block = False
                    review_reason = ""
                    if not git_diff_stat or not git_diff_stat.strip():
                        if self.logger:
                            self.logger.info(
                                "inline_review_skipped",
                                "Skipping inline review: empty git diff",
                                task_id=task.id,
                            )
                    else:
                        try:
                            review = self._inline_spec_code_review(
                                task_desc=task.description,
                                git_diff_stat=git_diff_stat,
                            )
                            review_should_block = bool(
                                review.get("should_block", False)
                            )
                            review_reason = str(review.get("reason", "") or "")
                        except Exception as exc:  # noqa: BLE001 — best-effort
                            # Graceful degradation: an LLM-side failure
                            # (network blip, API error, parse error that
                            # escaped the function's internal try/except)
                            # must NEVER block a legitimate commit. The
                            # review is a safety net, not a gate.
                            review_should_block = False
                            review_reason = f"inline review failed: {exc}"
                            if self.logger:
                                self.logger.warning(
                                    "inline_review_failed",
                                    review_reason,
                                    task_id=task.id,
                                    data={"exception": type(exc).__name__},
                                )

                    if review_should_block:
                        # Revert the task to ``pending`` so the next
                        # run picks it up, and write a
                        # ``failure_reason`` capturing what the LLM
                        # flagged. Do NOT commit — the bad code must
                        # not enter git history.
                        self.task_manager.record_task_failure(
                            task.id, review_reason
                        )
                        self.task_manager.update_task_status(
                            task.id, "pending"
                        )
                        # Persist the reset-to-pending status so the
                        # next ``_load_tasks`` reload reflects it.
                        try:
                            self._persist_task_status(task)
                        except Exception as persist_exc:  # noqa: BLE001
                            if self.logger is not None:
                                try:
                                    self.logger.warning(
                                        "task_persist_skipped",
                                        f"Could not persist task [{task.id}] "
                                        f"status='pending' (inline review "
                                        f"block) to "
                                        f"plan_execution.task_progress: "
                                        f"{type(persist_exc).__name__}: "
                                        f"{persist_exc}",
                                        task_id=task.id,
                                        data={"error": str(persist_exc)[:500]},
                                    )
                                except Exception:
                                    pass
                        if self.logger:
                            self.logger.warning(
                                "inline_review_blocked",
                                f"Task [{task.id}] blocked by inline "
                                f"review: {review_reason[:200]}",
                                task_id=task.id,
                                data={"reason": review_reason[:500]},
                            )
                        print(
                            f"Inline review blocked the commit: "
                            f"{review_reason}"
                        )
                        return False

                    # 2026-09-10 fix: single write path. v4 splits
                    # ``plan_execution.task_progress`` JSON column
                    # into a proper ``plan_tasks`` table; both
                    # ``task_manager.update_task_status`` (which
                    # calls ``_persist_status_to_sqlite``) and
                    # ``_persist_task_status`` (CAS via
                    # ``PlanTaskRepository.update_task``) wrote the
                    # SAME row to ``plan_tasks`` — two independent
                    # connections + transactions racing each other.
                    # The CAS write in particular would silently
                    # fail when its expected_version got bumped by a
                    # concurrent in-progress write from line 4080 of
                    # the next attempt cycle, leaving the row stuck
                    # at ``status="in_progress"`` until the same-id
                    # loop detector re-synced it. Use the single
                    # write path that the v4 plan prescribes: go
                    # through ``task_manager.update_task_status``
                    # (last-writer-wins via ``_persist_status_to_sqlite``)
                    # and let its v4 single-transaction write be the
                    # authoritative source. ``_persist_task_status``
                    # is still defined for legacy callers but no longer
                    # called here.
                    self.task_manager.update_task_status(task.id, "completed")
                    # Same-id loop guard bookkeeping: count how many
                    # times each task_id has completed in this session
                    # so the dispatcher can detect re-execution of an
                    # already-completed task.
                    self._session_task_completed_counts[task.id] = (
                        self._session_task_completed_counts.get(task.id, 0) + 1
                    )
                    self.retry_manager.reset_state(task.id)

                    # Commit with task checkpoint format
                    self._commit_task_changes(task, changed_files)
                    print("Changes committed.")
                    duration = int(time.monotonic() - task_start_time)
                    if self.logger:
                        self.logger.info("task_completed",
                                         f"Task [{task.id}] completed successfully",
                                         task_id=task.id,
                                         data={"changed_files": changed_files,
                                               "duration_sec": duration,
                                               "attempts": attempt + 1})
                    return True
                else:
                    print(f"Tests failed: {test_reason}")
                    error_msg = test_reason or "AI reported test failure"
                    self.retry_manager.record_attempt(task.id, error_msg, False)

                    # Build result context for refinement
                    result_context = f"Task '{task.title}' failed"
                    result_context += f"\nAI test report:\n{test_reason}"

                    if self.logger:
                        self.logger.warning("task_retry",
                                            f"Task [{task.id}] test failed: {error_msg[:200]}",
                                            task_id=task.id,
                                            data={"attempt": attempt + 1, "test_reason": (test_reason or "")[:500]})

                    if attempt < max_retries - 1:
                        print("Retrying with refined approach...")
                        continue

                    # All in-run retries exhausted — refine the task list
                    # (split into children) ONCE, then mark as failed for
                    # cross-run detection. Refining on every failure caused
                    # the task list to balloon in the
                    # 20260615-refactor-provider-config plan (15 -> 35
                    # subtasks); the cross-verify layer already feeds the
                    # failure reason back to the next attempt via
                    # ``get_retry_prompt_modifier`` so subagents can
                    # self-correct during retries. The refine pass is only
                    # needed when retries are exhausted.
                    # No-bulk-context contract: pass file_context=""
                    # to the refiner. The refiner is a planner and
                    # must never receive a repository-wide snapshot.
                    self._refine_after_failure(task, coder_response, result_context, "", 1)

                    print("All retries exhausted for this run.")
                    self.task_manager.record_task_failure(task.id, error_msg)
                    duration = int(time.monotonic() - task_start_time)
                    if self.logger:
                        self.logger.error("task_failed",
                                          f"Task [{task.id}] failed after all retries: {error_msg[:200]}",
                                          task_id=task.id,
                                          data={"error": error_msg[:500], "duration_sec": duration,
                                                "attempts": max_retries})
                    return False

            except ApiError as e:
                # LLM API error. Distinguish two very different cases
                # (2026-09-22):
                #
                #   * CAPACITY — 429 / quota exhausted / gateway
                #     overloaded. This is not a statement about the
                #     task; it is a statement about the provider. The
                #     coding tool has already rotated through every
                #     provider and parked the exhausted ones
                #     (``coding_tool._park_provider_after_error``), so
                #     a fresh attempt walks a different provider than
                #     the one that just refused. Failing the task here
                #     kills work that is perfectly executable —
                #     Vendor A Pro hit its Token Plan cap on
                #     2026-09-22 and task 14-2 died instantly, which
                #     permanently deferred 14-3 and 14-5 (their
                #     upstream) and stranded the whole run at
                #     "No schedulable micro-layer found".
                #
                #   * EVERYTHING ELSE — auth failure, malformed
                #     request. Retrying cannot help; fail immediately.
                from coding_tool import is_capacity_error

                if is_capacity_error(e) and attempt < max_retries - 1:
                    print(
                        "\nPROVIDER CAPACITY ERROR — rotating and "
                        "retrying this task\n"
                    )
                    self.retry_manager.record_attempt(task.id, str(e), False)
                    if self.logger:
                        self.logger.warning(
                            "task_api_error_provider_retry",
                            (
                                f"Task [{task.id}] hit a provider capacity "
                                f"error ([{e.status}]) — retrying attempt "
                                f"{attempt + 2}/{max_retries} on another "
                                f"provider instead of failing the task"
                            ),
                            task_id=task.id,
                            data={
                                "attempt": attempt + 1,
                                "max_attempts": max_retries,
                                "api_status": e.status,
                                "error": str(e)[:300],
                            },
                        )
                    continue

                print(f"\n{'=' * 60}")
                print("LLM API ERROR — RETRYING WILL NOT HELP")
                print(f"{'=' * 60}")
                print(f"Status: {e.status}")
                print(f"Message: {e}")
                if e.retry_after:
                    print(f"Retry after: {e.retry_after} seconds")
                print(f"{'=' * 60}\n")

                error_msg = str(e)
                self.task_manager.record_task_failure(task.id, error_msg)
                if self.logger:
                    self.logger.error("task_api_error",
                                      f"LLM API error for task [{task.id}]: [{e.status}] {e}",
                                      task_id=task.id,
                                      data={"api_status": e.status, "retry_after": e.retry_after})
                return False

            except Exception as e:
                print(f"An unexpected error occurred while processing task: {e}")
                error_msg = str(e)
                self.retry_manager.record_attempt(task.id, error_msg, False)
                if self.logger:
                    self.logger.error("task_error",
                                      f"Unexpected error for task [{task.id}]: {e}",
                                      task_id=task.id,
                                      data={"error": str(e)[:500], "attempt": attempt + 1})

                if attempt < max_retries - 1:
                    print("Retrying after error...")
                    continue

                self.task_manager.record_task_failure(task.id, error_msg)
                duration = int(time.monotonic() - task_start_time)
                if self.logger:
                    self.logger.error("task_failed",
                                      f"Task [{task.id}] failed after unexpected error",
                                      task_id=task.id,
                                      data={"error": str(e)[:500], "duration_sec": duration})
                return False

        return False

    def _clean_test_command(self, test_command: str) -> str:
        """Remove misleading echo suffixes that unconditionally output TEST_RESULT.

        Some test commands append '; echo "TEST_RESULT: PASSED"' which would
        make the shell always output that text regardless of pytest results.
        We strip those suffixes so the AI must observe real test output and
        report the result in its own response.
        """
        import re
        # Match patterns like: ; echo "TEST_RESULT: PASSED" or && echo 'TEST_RESULT: FAILED'
        # The pattern looks for separator (;, &&, ||) followed by echo and TEST_RESULT
        pattern = r'\s*[;|&]+\s*echo\s+["\']?TEST_RESULT:[^"\']*["\']?'
        cleaned = re.sub(pattern, '', test_command)
        # Remove trailing separator chars and whitespace
        cleaned = cleaned.rstrip(' ;&|')
        return cleaned

    # 60 s cap on the pre-flight run. Tasks whose test_command legitimately
    # takes longer than 60s to run on a clean checkout (large suites) are
    # not eligible for skip-via-preflight and always go through the
    # normal subagent path. This is an intentionally conservative cap:
    # the executor's normal task budget is 1800s per attempt, so any
    # test that needs more than 60s is much more expensive to verify
    # via the skip gate than via the subagent's own test_command
    # execution at the end of the attempt. Better to pay the
    # subagent cost than risk falsely skipping an under-tested task.
    _PREFLIGHT_TEST_TIMEOUT_SEC = 60

    def _preflight_test_command_skip(self, task: SubTask) -> Optional[bool]:
        """Run the task's ``test_command`` against the current
        ``project_dir`` BEFORE invoking the subagent.

        Returns ``True`` iff the command exits 0 — meaning the work
        the task is supposed to do is already on disk in a
        passing state, so the subagent would be redundant. In that
        case :meth:`_execute_task_with_retry` short-circuits and
        marks the task completed without spinning up Claude.

        Returns ``False`` iff the command runs and exits non-zero —
        meaning the work is genuinely missing or broken, and the
        subagent must do the real implementation work.

        Returns ``None`` iff the check is not applicable (no
        ``test_command``, no ``project_dir``, OSError, timeout) —
        in which case the caller falls through to the normal
        subagent path; an undeliverable pre-flight is never a
        reason to mark a task done.

        Semantics (2026-08-24 dual-criterion rule, per
        the project's CLAUDE.md):

          * Audit-style tasks (``verification_only=True`` or detected
            by :meth:`_looks_like_audit_task`) skip the pre-flight
            check entirely — their declared deliverable is a
            written answer / line-number finding, not a code diff,
            and running their shell probes on a clean tree is a
            no-op. Return ``None`` so the normal subagent path
            runs and a fresh audit is recorded.
          * All other tasks are subject to the pre-flight check.
        """
        test_command = (getattr(task, "test_command", "") or "").strip()
        if not test_command:
            return None
        if (
            getattr(task, "verification_only", False)
            or self._looks_like_audit_task(task)
        ):
            return None
        project_dir = (
            Path(task.project_dir)
            if getattr(task, "project_dir", None)
            else self.project_dir
        )
        if not project_dir or not Path(project_dir).exists():
            return None
        clean_cmd = self._clean_test_command(test_command)

        # 2026-09-17: never "skip as already done" on the strength of a
        # command that cannot report failure.
        #
        # A full-CI task whose command ended in ``… | tee /tmp/log``
        # could only ever exit 0: a pipeline exits with its LAST stage's
        # status, so the pre-flight declared PASSED, the
        # executor marked the task completed without spawning a
        # subagent, and no CI was ever run. The exit code is the only
        # signal the pre-flight has, so when the inspector says it is
        # meaningless the answer is ``None`` (fall through to the real
        # subagent path), never ``True``.
        #
        # This is deliberately narrower than refusing to run the task:
        # the command may still be worth executing, it just cannot be
        # trusted to *skip* on.
        try:
            _quality_issues = inspect_test_command(clean_cmd)
        except Exception:  # pragma: no cover - inspector is defensive
            _quality_issues = []
        if _quality_issues:
            if self.logger:
                try:
                    self.logger.warning(
                        "task_preflight_skip_refused_unusable_command",
                        f"Task [{task.id}] not skipped by pre-flight: its "
                        f"test_command is structurally unable to report "
                        f"failure ({_quality_issues[0].code})",
                        task_id=task.id,
                        data={
                            "code": _quality_issues[0].code,
                            "detail": _quality_issues[0].detail,
                            "test_command": clean_cmd[:500],
                        },
                    )
                except Exception:
                    pass
            return None

        try:
            proc = run_bounded(
                clean_cmd,
                cwd=str(project_dir),
                text=True,
                timeout=self._PREFLIGHT_TEST_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return proc.returncode == 0

    # 2026-09-17: a subagent's final answer used to live only in the
    # executor's memory. When an attempt failed, the conclusion — often
    # the most valuable artifact for postmortem (e.g. an earlier plan
    # repair-r3-06-3 attempt that spent 24 minutes discovering the task
    # was unsatisfiable) — was discarded with it, and no log or database
    # row pointed at what the agent had actually concluded. Persisting
    # every attempt's raw output under the plan dir makes the executor
    # debuggable after the fact.
    _AGENT_OUTPUTS_DIRNAME = "agent_outputs"

    @staticmethod
    def _safe_output_stem(task_id: str) -> str:
        """Filesystem-safe stem for a task id (keeps [a-zA-Z0-9._-])."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", task_id)[:120] or "unknown"

    def _agent_outputs_dir(self) -> Optional[Path]:
        """Plan-local directory where agent conclusions are persisted.

        Derived from ``TaskManager.tasks_file`` so the artifacts land next
        to the plan's own state regardless of the target project's cwd.

        Returns ``None`` unless ``tasks_file`` is a real path. Test
        harnesses sometimes build a ``TaskManager`` whose ``tasks_file``
        is a ``MagicMock``; ``Path(mock)`` would happily "resolve" to
        ``MagicMock/mock.tasks_file`` and the persistence call would
        create that directory tree inside the repository.
        """
        tasks_file = getattr(self.task_manager, "tasks_file", None)
        if not isinstance(tasks_file, (str, Path)) or not str(tasks_file):
            return None
        return Path(tasks_file).parent / self._AGENT_OUTPUTS_DIRNAME

    def _persist_agent_output(
        self,
        task_id: str,
        attempt: int,
        response: str,
        *,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Write one attempt's raw subagent output to the plan dir.

        Never raises: persistence failures must not break execution.
        Emits ``task_agent_output_persisted`` (or ``..._failed``) so the
        artifact path is discoverable from execution.log.
        """
        out_dir = self._agent_outputs_dir()
        if out_dir is None:
            return None
        response = response or ""
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            claimed, reason = self._parse_test_result(response)
            path = out_dir / (
                f"{self._safe_output_stem(task_id)}_attempt{attempt}.md"
            )
            header = {
                "task_id": task_id,
                "attempt": attempt,
                "persisted_at": datetime.now().isoformat(),
                "response_chars": len(response),
                "parsed_test_result": "PASSED" if claimed else "FAILED",
                "parsed_reason": reason,
            }
            if extra_meta:
                header.update(extra_meta)
            body = (
                "\n".join(f"{k}: {v}" for k, v in header.items())
                + "\n\n---\n\n"
                + (response or "")
            )
            path.write_text(body, encoding="utf-8")
            if self.logger:
                self.logger.info(
                    "task_agent_output_persisted",
                    f"Task [{task_id}] attempt {attempt} output persisted "
                    f"({len(response)} chars) -> {path}",
                    task_id=task_id,
                    data={
                        "path": str(path),
                        "response_chars": len(response),
                        "attempt": attempt,
                    },
                )
            return str(path)
        except Exception as exc:  # pragma: no cover - defensive
            if self.logger:
                try:
                    self.logger.warning(
                        "task_agent_output_persist_failed",
                        f"Could not persist output for task [{task_id}] "
                        f"attempt {attempt}: {exc}",
                        task_id=task_id,
                        data={"error": str(exc)[:300], "attempt": attempt},
                    )
                except Exception:
                    pass
            return None

    def _persist_refiner_output(
        self,
        task_id: str,
        payload: Dict[str, Any],
        *,
        suffix: str = "",
    ) -> Optional[str]:
        """Persist the refiner's conclusion (or its error) next to agent outputs.

        The refiner's raw LLM text is consumed inside ``Refiner.refine``;
        what survives to this layer is the parsed structure (or the
        exception). That is exactly what a postmortem needs to know — did
        the refiner split, keep, or fail — so it is written to disk in
        both cases, never only logged.
        """
        out_dir = self._agent_outputs_dir()
        if out_dir is None:
            return None
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"refiner_{self._safe_output_stem(task_id)}{suffix}.json"
            envelope = {
                "task_id": task_id,
                "persisted_at": datetime.now().isoformat(),
                **payload,
            }
            path.write_text(
                json.dumps(envelope, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            if self.logger:
                self.logger.info(
                    "refiner_output_persisted",
                    f"Refiner output for task [{task_id}] persisted -> {path}",
                    task_id=task_id,
                    data={"path": str(path), "suffix": suffix},
                )
            return str(path)
        except Exception as exc:  # pragma: no cover - defensive
            if self.logger:
                try:
                    self.logger.warning(
                        "refiner_output_persist_failed",
                        f"Could not persist refiner output for [{task_id}]: {exc}",
                        task_id=task_id,
                        data={"error": str(exc)[:300]},
                    )
                except Exception:
                    pass
            return None

    # 2026-09-17 (C2): an empty subagent response is an infrastructure
    # fault, not an attempt.
    #
    # The earlier-plan repair-r3-06-3 attempt 2 completed in 0s with 0 chars
    # — the provider returned nothing at all — and the executor scored it
    # as a normal attempt ("AI did not report test result"), burning the
    # task's last retry slot 6 seconds after attempt 1 had spent 24
    # minutes doing real work. The task then failed, the refiner had
    # nothing to split on, and the plan's whole execution stopped.
    #
    # An empty reply carries no information about the task; retrying it
    # costs a fraction of a real attempt. Bounded so a provider outage
    # cannot spin forever.
    _MAX_EMPTY_OUTPUT_RETRIES = 3

    def _retry_empty_query(
        self,
        context: str,
        coder_prompt: str,
        task: SubTask,
        attempt: int,
    ) -> str:
        """Re-query when the subagent returned an empty/blank response.

        Returns the first non-blank response, or ``""`` when the budget is
        exhausted (the caller then scores it as a failed attempt, as
        before). Each empty reply logs ``task_api_error_empty_output`` so
        operators can see provider flakiness in execution.log.
        """
        last = ""
        for retry in range(self._MAX_EMPTY_OUTPUT_RETRIES):
            try:
                response = self.coding_tool.query(
                    context,
                    system_instruction=coder_prompt,
                    model_type=task.model_type,
                    scene="execution",
                )
            except Exception as exc:
                if self.logger:
                    self.logger.warning(
                        "task_api_error_empty_output_retry_failed",
                        f"Empty-output retry for task [{task.id}] raised: {exc}",
                        task_id=task.id,
                        data={"error": str(exc)[:300], "attempt": attempt},
                    )
                return last
            if response and response.strip():
                if retry and self.logger:
                    self.logger.info(
                        "task_empty_output_recovered",
                        f"Task [{task.id}] recovered on empty-output retry "
                        f"{retry + 1}: {len(response)} chars",
                        task_id=task.id,
                        data={"attempt": attempt, "response_chars": len(response)},
                    )
                return response
            last = response or ""
            if self.logger:
                self.logger.warning(
                    "task_api_error_empty_output",
                    f"Task [{task.id}] attempt {attempt} returned an empty "
                    f"response from the coding tool; retry "
                    f"{retry + 1}/{self._MAX_EMPTY_OUTPUT_RETRIES} "
                    f"(not counted against the attempt budget)",
                    task_id=task.id,
                    data={
                        "attempt": attempt,
                        "empty_retry": retry + 1,
                        "max_empty_retries": self._MAX_EMPTY_OUTPUT_RETRIES,
                    },
                )
        return last

    def _parse_test_result(self, response: str) -> tuple[bool, str]:
        """Parse TEST_RESULT block from AI response.

        Returns:
            (passed, reason) — passed is True if TEST_RESULT: PASSED,
            False otherwise with reason extracted from REASON line.
        """
        import re

        # Look for TEST_RESULT: PASSED or TEST_RESULT: FAILED at start of line
        match = re.search(r'(?:^|\n)\s*TEST_RESULT:\s*(PASSED|FAILED)', response, re.IGNORECASE)
        if not match:
            # AI did not output test result — treat as uncertain failure
            return False, "AI did not report test result"

        status = match.group(1).upper()
        if status == "PASSED":
            return True, ""

        # Extract reason after REASON: at start of line
        reason_match = re.search(r'(?:^|\n)\s*REASON:\s*(.+?)(?:\n|$)', response, re.IGNORECASE | re.DOTALL)
        reason = reason_match.group(1).strip() if reason_match else "AI reported test failure"
        return False, reason

    def _cross_verify_test_result(
        self,
        task: "SubTask",
        ai_claimed_passed: bool,
        ai_reason: str,
        coder_response: str,
    ) -> tuple[bool, str]:
        """Re-run the task's test commands in project_dir and use the
        real pytest exit code as the authoritative verdict.

        The AI's TEST_RESULT text is taken as advisory only. The actual
        ground truth is whether ``pytest`` exited 0. This blocks the
        failure mode where the subagent writes ``TEST_RESULT: PASSED``
        without ever running the test (which previously let broken code
        slip through with a "completed" status).

        Decision table:

        | pytest exit | AI claim   | Verdict | Reason to record              |
        |-------------|------------|---------|-------------------------------|
        | 0           | PASSED     | PASSED  | (none)                        |
        | 0           | FAILED     | PASSED  | "AI reported FAILED but pytest exit 0 — trusting pytest" |
        | 0           | (no claim) | PASSED  | (none)                        |
        | non-zero    | PASSED     | FAILED  | "Subagent lied: claimed PASSED but pytest exit <code>" |
        | non-zero    | FAILED     | FAILED  | (AI reason + actual tail)     |
        | non-zero    | (no claim) | FAILED  | "pytest exit <code> (AI did not report test result)" |

        Args:
            task: SubTask carrying ``project_dir`` and ``get_test_commands()``.
            ai_claimed_passed: What the AI's TEST_RESULT line said.
            ai_reason: AI's REASON text (used as advisory detail only).
            coder_response: Full AI response (logged on disagreement).

        Returns:
            (passed, reason) — authoritative verdict, with reason that
            includes a clear cross-verification note when the AI's claim
            conflicts with the actual exit code.
        """
        import subprocess
        import re

        test_cmds = task.get_test_commands()
        if not test_cmds:
            # No test command defined for this task — there is no
            # second signal to take a majority verdict over, so this
            # branch can only echo the AI's own claim. That is exactly
            # the single-signal failure mode the 2026-08-24
            # dual-criterion rule exists to prevent, so:
            #
            #   * log at WARNING (was DEBUG) with an explicit
            #     ``verified: False`` marker, so an operator can grep
            #     ``test_cross_verify_unverified`` and see how much of
            #     a run rested on a self-report; and
            #   * ``_audit_task_second_pass`` now also triggers for
            #     any task without a runnable command, so "completed"
            #     still rests on an independent adversarial review
            #     instead of the subagent's word alone.
            #
            # The fallback verdict itself is unchanged — hardening a
            # plan's task generation must not strand tasks that
            # legitimately need no command.
            if self.logger:
                self.logger.warning(
                    "test_cross_verify_unverified",
                    "No test_command defined for task; verdict is the AI "
                    "claim alone (second signal unavailable)",
                    task_id=task.id,
                    data={
                        "verified": False,
                        "ai_claimed_passed": ai_claimed_passed,
                        "ai_reason": ai_reason[:200],
                    },
                )
            return ai_claimed_passed, ai_reason

        # The task's project_dir is the authoritative place to run tests:
        # the subagent wrote its code there, so the tests must run there
        # too — not in the framework's own checkout.
        project_dir = Path(task.project_dir) if task.project_dir else self.project_dir
        if not project_dir or not Path(project_dir).exists():
            reason = (
                f"Cannot cross-verify: project_dir {project_dir!r} does not exist. "
                f"Falling back to AI claim (passed={ai_claimed_passed})."
            )
            if self.logger:
                self.logger.warning("test_cross_verify_no_project_dir", reason, task_id=task.id)
            return ai_claimed_passed, reason

        # Run every test command. All must pass for the verdict to be PASSED.
        aggregate_exit = 0
        failure_tail = ""
        for cmd in test_cmds:
            try:
                proc = run_bounded(
                    cmd,
                    cwd=str(project_dir),
                    text=True,
                    timeout=600,  # 10 min per command — pytest can be slow
                )
            except subprocess.TimeoutExpired:
                aggregate_exit = 124  # convention: timeout
                failure_tail = f"[timeout after 600s] {cmd}"
                break
            except Exception as e:
                aggregate_exit = 1
                failure_tail = f"[runner error: {e}] {cmd}"
                break
            if proc.returncode != 0:
                aggregate_exit = proc.returncode
                # Keep last 1000 chars of output for diagnostics
                tail = (proc.stdout or "")[-1000:] + (proc.stderr or "")[-500:]
                failure_tail = f"[exit {proc.returncode}] {cmd}\n{tail}"
                break

        # Decision table
        if aggregate_exit == 0:
            if not ai_claimed_passed:
                # Disagreement: AI said FAILED, pytest said 0. Trust pytest
                # but record the disagreement so the AI's reasoning is
                # preserved in the log.
                reason = "AI reported FAILED but pytest exit 0 — trusting pytest"
                if self.logger:
                    self.logger.warning(
                        "test_cross_verify_disagree_pass",
                        reason,
                        task_id=task.id,
                        data={"ai_reason": ai_reason[:300]},
                    )
            return True, ""

        # pytest exit was non-zero. Whether the AI claimed PASSED or
        # FAILED, the verdict is FAILED — the AI's claim is overridden.
        if ai_claimed_passed:
            # Critical: the AI lied (or was confused). Make this loud.
            reason = (
                f"Subagent test-report mismatch: claimed TEST_RESULT: PASSED but "
                f"actual pytest exit code was {aggregate_exit}. Treating as FAILED. "
                f"Tail of test output:\n{failure_tail}"
            )
            if self.logger:
                self.logger.error(
                    "test_cross_verify_cheat_detected",
                    "Subagent claimed PASSED but pytest failed",
                    task_id=task.id,
                    data={
                        "pytest_exit": aggregate_exit,
                        "ai_claimed_passed": True,
                        "test_output_tail": failure_tail[:1500],
                        "coder_response_tail": coder_response[-500:],
                    },
                )
        else:
            reason = (
                f"pytest exit {aggregate_exit}. AI reason: {ai_reason or '(none)'}. "
                f"Tail of test output:\n{failure_tail}"
            )
            if self.logger:
                self.logger.warning(
                    "test_cross_verify_fail",
                    f"pytest exit {aggregate_exit}",
                    task_id=task.id,
                    data={"pytest_exit": aggregate_exit, "ai_reason": ai_reason[:300]},
                )
        return False, reason

    # 2026-08-24 dual-criterion rule (the project's CLAUDE.md):
    # For audit-style tasks (declared ``verification_only=True`` OR
    # heuristically detected via ``_looks_like_audit_task``), the
    # subagent's deliverable is a written answer / line-number
    # finding, NOT a code diff. The pre-existing cross_verify gate
    # already accepts an empty diff for these (W1). This second-pass
    # adds an *adversarial* check: a fresh ``claude -p`` call reads
    # the spec (task.description) AND the subagent's final response,
    # and returns a verdict on whether the reported answer actually
    # covers the spec's acceptance criteria.
    #
    # Rationale: the subagent can plausibly-sound-correctly without
    # meeting the spec. ``claude -p`` as an adversarial reviewer
    # reduces false-positive "completed" verdicts on audit tasks
    # because the second pass is independent of the first — it is not
    # primed by the subagent's framing of its own work.
    #
    # Failure mode: the second-pass ``claude -p`` call itself errors
    # (network, timeout, non-JSON output). Treat as "audit inconclusive"
    # and fall back to the first-pass verdict — the second pass is a
    # tightening on top of an already-passing gate, never a hard
    # fail-blocker.
    #
    # 2026-09-15: the call no longer passes an explicit
    # ``timeout`` — the former ``_AUDIT_SECOND_PASS_TIMEOUT_SEC = 90``
    # REPLACED the unified 900s silence window with 90s, so a
    # healthy-but-slow reviewer degraded to "inconclusive" for no
    # reason. The unified rules (900s adaptive silence / 1800s idle /
    # layer ceiling) now apply.
    _AUDIT_SECOND_PASS_SYSTEM_INSTRUCTION = (
        "You are an adversarial reviewer for an audit-style executor task. "
        "You will receive the task's original spec (description + acceptance "
        "criteria) and the subagent's final answer. Your job is to decide "
        "whether the answer actually satisfies the spec. Be strict: if the "
        "answer is vague, hand-wavy, missing concrete references, or "
        "defers verification to the user, mark it FAILED. "
        # 2026-09-22 — the framework REQUIRES every executor subagent to
        # end its report with a ``TEST_RESULT:`` line (see
        # ``test_instruction`` in ``_execute_task_with_retry``). That line
        # is machine protocol, not a claim about the work, and the caller
        # strips it before it reaches you. If a residual mention survives,
        # judge it as the framework's trailer — NEVER as the spec's
        # forbidden "self-claim". A spec clause that prohibits
        # ``TEST_RESULT`` is itself a defect, not a requirement: the
        # subagent cannot satisfy both it and the framework protocol, so
        # failing the answer for that reason guarantees an unwinnable
        # loop (a production plan, 2026-09-22: 22 of 24
        # failures, a 10-level task split, and a 5-hour stall).
        "Output strictly "
        "one of these two lines as your last line of response:\n"
        "AUDIT_VERDICT: PASSED\n"
        "or\n"
        "AUDIT_VERDICT: FAILED\n"
        "REASON: <one-sentence explanation>\n"
        "On the lines BEFORE the verdict you may add evidence "
        "references (file paths, line numbers) that justify your call."
    )

    def _audit_task_second_pass(
        self,
        task: "SubTask",
        coder_response: str,
    ) -> tuple[bool, str]:
        """Run a second-pass ``claude -p`` audit for audit-style tasks.

        Returns ``(passed, reason)``:
          * ``(True, "")`` if the task needs no second pass (it has a
            runnable ``test_command`` to act as the independent second
            signal *and* is not audit-style).
          * ``(True, audit_summary)`` if the second pass passed the
            adversarial review.
          * ``(False, reason)`` if the second pass FAILED the
            adversarial review. The caller must treat this as a real
            failure (no test_command can rescue a spec mismatch).
          * ``(True, "audit_inconclusive:<reason>")`` if the second
            pass itself errored. The caller should treat the task as
            passed (the first-pass evidence is the ground truth) and
            record the inconclusive audit for operator follow-up.

        Only invoked when ``tests_passed`` is already True — the
        caller decides whether the task has earned the right to a
        second-pass check.
        """
        # 2026-09-14: a task with no runnable command has no independent
        # second signal at all — ``_cross_verify_test_result`` can only
        # echo the AI's own claim (see ``test_cross_verify_unverified``).
        # Route those through the same adversarial review so a
        # "completed" verdict still rests on two independent judgments.
        # Repair tasks used to land here in bulk: the 2026-09-07
        # content-only refactor stripped ``test_command`` from every
        # generated repair task (fixed at the source on 2026-09-14), and
        # this is the safety net that makes the gap loud instead of
        # silently trusting the subagent.
        has_runnable_command = bool(task.get_test_commands())
        is_audit = (
            getattr(task, "verification_only", False)
            or self._looks_like_audit_task(task)
            or not has_runnable_command
        )
        if not is_audit:
            return True, ""

        spec = (
            f"## Task title\n{getattr(task, 'title', '')}\n\n"
            f"## Task description\n{getattr(task, 'description', '')}"
        )
        # 2026-09-20 (post-mortem): when the task has no runnable
        # command the executor's own instruction asks the subagent to
        # construct and run its verification and paste the terminal
        # output (see the ``else`` branch of ``test_instruction`` in
        # ``_execute_task_with_retry``). Tell the reviewer that is the
        # contract it is judging against — otherwise a PASSED verdict
        # can rest entirely on the subagent's prose, which is the exact
        # shape the second pass exists to reject.
        self_verification_note = ""
        if not has_runnable_command:
            self_verification_note = (
                "\n\n## NOTE — this task has no test_command\n"
                "The subagent was instructed to construct its own "
                "verification: restate the acceptance criteria, run a "
                "command that demonstrates each one against the current "
                "tree, and paste the exact command with its real output. "
                "Judge it against that contract. An answer that asserts "
                "success without pasted terminal output — including one "
                "that only points at a prior round or commit, or that "
                "cites an exit code with no output — must be FAILED, "
                "however plausible it reads."
            )
        audit_prompt = (
            "Compare the spec below to the subagent's final answer "
            "below. Decide whether the answer actually satisfies the "
            "spec's acceptance criteria.\n\n"
            f"## SPEC\n{spec}{self_verification_note}\n\n"
            # 2026-09-22: strip the framework's mandatory ``TEST_RESULT:``
            # trailer before the reviewer sees it. It is machine protocol,
            # not evidence, and a spec clause forbidding it would otherwise
            # make every answer fail (see
            # ``strip_framework_result_trailer``).
            f"## SUBAGENT ANSWER\n"
            f"{strip_framework_result_trailer(coder_response)[-8000:]}"
        )

        try:
            audit_response = self.coding_tool.query(
                audit_prompt,
                system_instruction=self._AUDIT_SECOND_PASS_SYSTEM_INSTRUCTION,
                # 2026-09-15: inherit the unified
                # timeout rules — see the note on the (removed)
                # second-pass timeout constant above.
                # 2026-09-13 provider routing: the legacy haiku-tier hint
                # was dead (normalized to medium, no routing effect).
                # The audit scene now selects the provider tier instead.
                scene="audit_second_pass",
            )
        except (TimeoutError, Exception) as exc:
            # Audit layer errors must NOT block the first-pass verdict.
            # The first-pass already passed; treat the audit as
            # inconclusive and let the operator see the gap in the log.
            inconclusive = (
                f"audit_inconclusive:second_pass_error:{type(exc).__name__}"
            )
            if self.logger:
                self.logger.warning(
                    "task_audit_second_pass_inconclusive",
                    f"Audit second pass errored for [{task.id}]; "
                    f"falling back to first-pass verdict.",
                    task_id=task.id,
                    data={"error": str(exc)[:300]},
                )
            return True, inconclusive

        # Parse AUDIT_VERDICT: ... from the tail of the response.
        # We walk backwards through the response: AUDIT_VERDICT comes
        # last in the audit agent's output, with REASON on the line
        # immediately before it. Some agents emit other lines after
        # the verdict (free-form commentary) which we ignore.
        tail = audit_response or ""
        verdict = None
        reason = ""
        for line in reversed(tail.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("AUDIT_VERDICT:"):
                verdict = stripped.split(":", 1)[1].strip().upper()
                break
            if stripped.startswith("REASON:"):
                reason = stripped.split(":", 1)[1].strip()
                # Don't break — keep scanning backwards so we still
                # find the AUDIT_VERDICT line above the REASON line.
                continue
            # Free-form commentary after the verdict — skip.
            continue

        if verdict == "PASSED":
            if self.logger:
                self.logger.info(
                    "task_audit_second_pass_passed",
                    f"Audit second pass confirmed [{task.id}]",
                    task_id=task.id,
                )
            return True, ""

        if verdict == "FAILED":
            if self.logger:
                self.logger.warning(
                    "task_audit_second_pass_failed",
                    f"Audit second pass rejected [{task.id}]: {reason}",
                    task_id=task.id,
                    data={"reason": reason[:500], "audit_response_tail": tail[-1500:]},
                )
            return False, f"audit_second_pass_failed: {reason or '(no reason given)'}"

        # No clear verdict — fall back to first-pass.
        inconclusive = (
            f"audit_inconclusive:no_verdict:tail={tail[-200:]!r}"
        )
        if self.logger:
            self.logger.warning(
                "task_audit_second_pass_inconclusive",
                f"Audit second pass for [{task.id}] did not emit a clear verdict",
                task_id=task.id,
                data={"audit_response_tail": tail[-1500:]},
            )
        return True, inconclusive

    def _commit_task_changes(self, task: SubTask, changed_files: List[str]):
        """
        Commit changes with task checkpoint format.

        Args:
            task: Completed task
            changed_files: List of changed file paths

        Why the trailing ``rev_parse`` + ``update_task_commit_sha``
        block exists (2026-09-11 plan): previously this method only
        ran ``git_manager.commit()`` and returned; nothing recorded
        the resulting commit SHA back into ``plan_tasks.commit_sha``.
        Every completed task across all plans therefore had
        ``commit_sha IS NULL`` in state.db (108/108 = 100% missing),
        which silently broke any audit / verification / card join
        that relied on the per-task commit link. Closing the loop
        here means the per-task row knows which commit it produced.
        Failures are non-fatal — the in-memory ``tasks.json`` and the
        on-disk git history are still authoritative.
        """
        commit_message = self.rollback_manager.create_task_commit(
            task_id=task.id,
            title=task.title,
            description=task.description,
            test_command=task.test_command,
            changed_files=changed_files
        )
        self.git_manager.commit(commit_message)
        # Write back the resulting commit SHA so the per-task row in
        # ``plan_tasks`` (and ``runtime_overrides``) reflects the git
        # reality. This was the missing piece that left 108/108
        # completed rows with ``commit_sha IS NULL``.
        try:
            sha = self.git_manager.rev_parse("HEAD")
        except Exception as rev_exc:  # noqa: BLE001
            sha = None
            if self.logger:
                self.logger.warning(
                    "task_commit_sha_rev_parse_failed",
                    f"Could not read HEAD commit SHA for task [{task.id}]: {rev_exc}",
                    task_id=task.id,
                    data={"error": str(rev_exc)[:200]},
                )
        if sha:
            try:
                self.task_manager.update_task_commit_sha(task.id, sha)
            except Exception as writeback_exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning(
                        "task_commit_sha_writeback_failed",
                        f"Could not write back commit SHA for task [{task.id}]: {writeback_exc}",
                        task_id=task.id,
                        data={"error": str(writeback_exc)[:200]},
                    )

    # ------------------------------------------------------------------
    # Inline spec+code self-review (DP3)
    # ------------------------------------------------------------------

    def _task_declared_files_exist(self, task: SubTask) -> bool:
        """Return True when every file the task declares it will modify
        already exists on disk under ``project_dir``.

        2026-08-19 fix (an earlier audit): an empty ``changed_files``
        (no diff produced this run) is NOT automatically a failure — a
        re-run of a task whose deliverable was already committed in a
        previous attempt (e.g. ``plans/.../etf_root_cause_report.json``
        from an earlier ``[task-1]`` commit) legitimately produces no new
        diff. We only treat empty output as "the work is genuinely done"
        when the task's declared ``files_to_modify`` targets already exist
        on disk. If they do NOT exist, the empty diff means the subagent
        produced nothing and the task must NOT be marked completed.

        The unknown-modification sentinel never matches a real file, so a
        sentinel-only task returns False here — an empty diff on a
        sentinel task is treated as "no output" and blocked from
        completing.

        Args:
            task: The :class:`SubTask` whose declared files are checked.

        Returns:
            True iff the task declares at least one concrete (non-sentinel)
            relative path AND every such path exists under ``project_dir``.
        """
        files = getattr(task, "files_to_modify", None) or []
        concrete = [
            f
            for f in files
            if isinstance(f, str)
            and f
            and f != "__UNKNOWN_MODIFICATIONS__"
            and not f.startswith("__")
        ]
        project_dir = (
            Path(task.project_dir)
            if getattr(task, "project_dir", None)
            else self.project_dir
        )
        if not concrete:
            # No concrete declared files (empty / sentinel-only). Fall back
            # to git history: if this task already produced a ``[task-id]``
            # checkpoint commit in a previous attempt, its deliverable is
            # already committed, so an empty re-run diff is a legitimate
            # no-op — NOT a genuine empty output.
            return self._task_has_prior_commit(task, project_dir)
        if not project_dir or not Path(project_dir).exists():
            return False
        try:
            return all((Path(project_dir) / rel).exists() for rel in concrete)
        except OSError:
            return False

    def _task_has_prior_commit(self, task: SubTask, project_dir) -> bool:
        """Return True when git history already contains a ``[task-id]``
        checkpoint commit for this task (i.e. its deliverable was committed
        in a previous attempt).

        This lets a legitimate no-op re-run (deliverable already committed)
        pass the empty-output gate, while a first-run task that produced
        nothing is still blocked.

        Args:
            task: The :class:`SubTask` being checked.
            project_dir: The repo to inspect (``Path`` or ``None``).

        Returns:
            True iff a commit whose message starts with ``[task-<id> ]``
            exists in ``project_dir``'s git history.
        """
        import subprocess

        if not project_dir or not Path(project_dir).exists():
            return False
        try:
            # ``--fixed-strings`` (``-F``) is required: the ``[task-<id>]``
            # prefix contains ``[`` ``]`` and ``-`` which ``git log
            # --grep`` would otherwise parse as a regex character class
            # with an invalid range, raising ``fatal: ... invalid
            # character range``.
            proc = subprocess.run(
                ["git", "log", "--oneline", "-F", "--grep", f"[task-{task.id}]", "-i", "-1"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        if proc.returncode != 0:
            return False
        return bool(proc.stdout and proc.stdout.strip())

    # ------------------------------------------------------------------
    # Audit-style task detection (2026-08-24 dual-criterion rule)
    # ------------------------------------------------------------------

    # Keywords that identify "this task's deliverable is a written
    # answer / report / line-number finding, NOT a code diff".
    # The list is intentionally narrow — these are the patterns that
    # appeared in real plan tasks that
    # produced legitimate empty diffs and were wrongly hard-failed
    # by the pre-2026-08-24 ``task_empty_output_blocked`` gate.
    _AUDIT_TASK_KEYWORDS = (
        "审计",
        "路径契约",
        "路径审计",
        "内容审计",
        "定位",
        "行号定位",
        "嗅探",
        "import 验证",
        "可导入性",
        "import 三个",
        "无前置依赖",
        "真实验证",
        "性能基准",
        "性能基线",
        "复跑",
        "真实复跑",
        "已存在于",
        "checkpoint",
        "invasiveness",
        "不变量",
    )

    def _looks_like_audit_task(self, task: SubTask) -> bool:
        """Heuristically decide whether ``task`` is audit-style.

        Audit-style tasks are those whose declared deliverable is a
        written report, a line-number finding, an import-shape
        verification, or a no-code-change benchmark — i.e. their
        spec says "verify X exists" or "report Y" rather than
        "implement Z". The :class:`SubTask` already exposes the
        explicit ``verification_only`` flag for the obvious cases;
        this heuristic catches the rest by scanning the title,
        description, and the test_command string for known
        audit-style keywords.

        The list is a frozen set of patterns observed in real plan
        tasks. False positives are acceptable: an audit task wrongly
        *not* detected is downgraded to the "non-audit" branch
        which still no longer hard-fails (see
        :meth:`_execute_task_with_retry` post-completion gate) —
        it just loses the explicit "task_audit_no_diff_accepted"
        log line, which is a debug-only difference.

        Args:
            task: The :class:`SubTask` being classified.

        Returns:
            True iff the title/description/test_command contains any
            of the audit-style keywords.

        2026-09-20 (post-mortem): a repair task can never be
        audit-style, so :func:`is_repair_task` short-circuits to False
        before the keyword scan. The keyword list contains 「定位」,
        「复跑」, 「不变量」 and 「checkpoint」 — all of which appear
        naturally in a repair brief ("【根因（代码定位）】", "异地复跑",
        "核心不变量"). Those words are common in a repair brief, so the
        heuristic classified repair tasks as audit-style; for an audit
        task an empty diff is accepted rather than retried, so the tasks
        produced an empty git diff, were marked ``completed``, and left
        the failure set byte-identical for the next round — which the
        loop then, quite correctly, reported as convergence.

        A repair task's entire purpose is to change code in response to
        a failed verification point. Keywords in its *brief* describe
        what to look at, never what to deliver.

        ``is_repair_task`` is a module-level function (not a method) so
        that the audit-task classifier can be exercised from lightweight
        stub agents in tests — see ``test_pipeline_exit_code_guard.py``
        where the call site predates the full ``__init__``.
        """
        if is_repair_task(task):
            return False
        haystack = " ".join(
            str(s) if s is not None else ""
            for s in (
                getattr(task, "title", "") or "",
                getattr(task, "description", "") or "",
                getattr(task, "test_command", "") or "",
            )
        )
        return any(kw in haystack for kw in self._AUDIT_TASK_KEYWORDS)

    def _get_git_diff_stat_for_review(self, task: SubTask) -> str:
        """Return a concise ``git diff --stat`` snapshot for the review.

        The LLM in :meth:`_inline_spec_code_review` is shown the diff
        stat (file list + insert/delete counts) as the basis for its
        "code quality" judgement. Full diffs would blow the context
        budget; the stat is the right granularity.

        The snapshot is taken against ``task.project_dir`` when set,
        otherwise ``self.project_dir``. If the directory is not a
        git repo or the diff raises, the helper returns an empty
        string — the review still runs on the spec dimension only.

        Args:
            task: The :class:`SubTask` being reviewed.

        Returns:
            A ``git diff --stat``-style string, or ``""`` on error.
        """
        import subprocess

        project_dir = (
            Path(task.project_dir)
            if getattr(task, "project_dir", None)
            else self.project_dir
        )
        if not project_dir or not Path(project_dir).exists():
            return ""

        try:
            # Include untracked + staged + unstaged changes against HEAD
            # so a freshly-written file (not yet git-added) shows up in
            # the review prompt. ``git diff HEAD`` alone skips untracked
            # files, which would silently bypass the review when the
            # coder writes a new file and the commit step has not yet
            # staged it.
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )
            proc = subprocess.run(
                ["git", "diff", "HEAD", "--stat"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""

        if proc.returncode != 0:
            return ""
        # Keep the last 4000 chars so the prompt never gets bloated
        # on huge diffs — the LLM only needs the headline stat.
        return (proc.stdout or "")[-4000:]

    def _inline_spec_code_review(
        self,
        task_desc: str,
        git_diff_stat: str = "",
        max_tokens: int = 512,
    ) -> dict:
        """Lightweight adversarial self-review for a task's implementation.

        The function is invoked between ``_cross_verify_test_result``
        (which proves the test command exited 0) and the git
        checkpoint commit. It asks the LLM to grade the implementation
        along two dimensions and emit a ``should_block`` flag. The
        caller (``_execute_task_with_retry``) is responsible for
        translating the verdict into a commit / no-commit decision.

        Verdict shape (always a dict; never raises):

          {
            "spec_compliance": "high" | "medium" | "low",
            "code_quality":    "high" | "medium" | "low",
            "should_block":    bool,
            "reason":          str,
          }

        Boundary conditions:

          * LLM call raises (network, API error, JSON parse error)
            -> the function returns
            ``{"should_block": False, "spec_compliance": "low",
            "code_quality": "low", "reason": "..."}`` so the caller
            commits normally. The reviewer is best-effort; outages
            must NEVER take down the commit flow. A second layer of
            try/except at the call site also handles the case where
            THIS function itself raises (defence in depth).
          * LLM returns unparsable text
            -> same degraded mode.
          * LLM returns a well-formed verdict with
            ``spec_compliance != "high"`` AND
            ``code_quality != "high"`` -> ``should_block`` is False;
            the caller commits normally. (The LLM is instructed to
            set ``should_block=False`` in this case so the rule is
            self-enforcing from the prompt side.)
          * ``should_block`` is True
            -> the caller reverts the task to ``pending`` and writes
            ``failure_reason``. The commit is skipped.

        Args:
            task_desc: The task's natural-language description (the
                "spec" the implementation should match).
            git_diff_stat: ``git diff --stat``-style summary of the
                change set. Empty string is acceptable (the LLM
                grades only on the spec dimension in that case).
            max_tokens: Token budget for the review reply. The
                reviewer is constrained to short JSON to keep cost
                down; an over-long reply is a soft signal of the
                LLM not following instructions and is treated as
                degraded mode.

        Returns:
            The verdict dict described above. Always returns a dict;
            never raises.
        """
        import json as _json
        import re

        prompt = INLINE_SPEC_CODE_REVIEW_PROMPT.format(
            task_desc=task_desc,
            git_diff_stat=git_diff_stat or "(无 diff 摘要)",
        )

        def _degraded(reason: str) -> dict:
            return {
                "spec_compliance": "low",
                "code_quality": "low",
                "should_block": False,
                "reason": reason,
            }

        # -- 1) LLM call (best-effort) ---------------------------------
        try:
            response = self.coding_tool.query(
                prompt=prompt,
                # 2026-09-15: no explicit ``timeout`` —
                # this used to pass 60s, which REPLACED the unified 900s
                # silence window (a healthy-but-slow review degraded to
                # "best-effort failed" for no reason). Inherit instead.
                # 2026-09-22: inline review is useful
                # but non-critical — it must NOT use the high tier. Pin
                # it to ``execution`` (Vendor A, medium tier) so a missing
                # ANTHROPIC_MODEL in the inherited settings can never
                # fall through to opus / vendor-d-pro (the 2026-09-22
                # billing incident: 215k tokens at 3x price).
                scene="execution",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            # Network blip, API error, auth failure, etc. Degrade.
            if self.logger:
                self.logger.warning(
                    "inline_review_llm_error",
                    f"_inline_spec_code_review LLM call failed: "
                    f"{type(exc).__name__}: {exc}",
                    data={"exception": type(exc).__name__},
                )
            return _degraded(f"LLM call failed: {type(exc).__name__}: {exc}")

        if not isinstance(response, str) or not response.strip():
            return _degraded("LLM returned empty response")

        # -- 2) Parse JSON from the LLM's reply ------------------------
        # The LLM sometimes wraps JSON in ```json ... ``` fences. Strip
        # the fences and try json.loads on the largest {...} block.
        text = response.strip()
        # Strip code fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        # Pull out the first JSON object (greedy to handle nested)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = match.group(0) if match else text

        try:
            verdict = _json.loads(candidate)
        except (ValueError, TypeError) as exc:
            return _degraded(
                f"LLM reply not parseable as JSON: {exc}; "
                f"raw={response[:200]!r}"
            )

        if not isinstance(verdict, dict):
            return _degraded(
                f"LLM reply is not a JSON object: {type(verdict).__name__}"
            )

        # -- 3) Normalise the verdict ----------------------------------
        spec = str(verdict.get("spec_compliance", "low")).lower()
        if spec not in ("high", "medium", "low"):
            spec = "low"
        quality = str(verdict.get("code_quality", "low")).lower()
        if quality not in ("high", "medium", "low"):
            quality = "low"
        should_block_raw = verdict.get("should_block", False)
        if isinstance(should_block_raw, str):
            should_block = should_block_raw.strip().lower() in (
                "true", "1", "yes",
            )
        else:
            should_block = bool(should_block_raw)
        reason = str(verdict.get("reason", "") or "")[:500]

        # Enforce the contract: should_block is True ONLY when at
        # least one dimension is "high". This is a defence in depth
        # check — the prompt tells the LLM to follow this rule, but
        # if the LLM drifts we still cap should_block at the
        # documented boundary.
        if spec != "high" and quality != "high":
            should_block = False

        return {
            "spec_compliance": spec,
            "code_quality": quality,
            "should_block": should_block,
            "reason": reason,
        }

    def _find_parent_task(self, task: SubTask) -> Optional[SubTask]:
        """Locate the parent of a breakdown child by hierarchical id prefix.

        Subtask ids follow the ``<parent>-<index>`` convention (e.g.
        ``"1-2"`` is a child of ``"1"``; ``"1-2-3"`` is a grandchild
        of ``"1-2"``). The "parent" here is the immediate
        breakdown parent — the task whose id is the longest common
        prefix of ``task.id`` when stripped of the trailing
        ``-<index>`` segment.

        Args:
            task: The :class:`SubTask` whose parent to find.

        Returns:
            The matching parent :class:`SubTask` from
            ``self._all_tasks``, or ``None`` if ``task`` is a
            top-level task (no ``-`` in its id) or the parent id is
            not present in the loaded task list. Returning ``None``
            (rather than raising) keeps the call site in
            :meth:`run` trivial — there is no aggregation candidate
            for top-level tasks.
        """
        if "-" not in task.id:
            return None
        parent_id = task.id.rsplit("-", 1)[0]
        for candidate in self._all_tasks:
            if candidate.id == parent_id:
                return candidate
        return None

    def _aggregate_breakdown_verdict(self, parent: SubTask) -> None:
        """Aggregate child task verdicts into a parent's terminal status.

        When a parent task has been broken down into 2-3 children
        (:meth:`_breakdown_task`), the children execute in parallel
        and reach terminal states (``completed`` / ``failed`` /
        ``skipped``) at their own pace. This method checks whether
        **all** of the parent's children are now terminal, and if so
        collapses them into a single verdict on the parent:

          * Any child ``failed`` → ``parent.status = "failed"``. The
            first child's ``failure_reason`` (when present) is
            surfaced onto the parent so the operator can read a
            single line in ``tasks.json`` instead of hunting
            through children.
          * All children ``skipped`` (no failures, no completions)
            → ``parent.status = "skipped"``.
          * Otherwise (every child ``completed``) →
            ``parent.status = "completed"``.

        The leaf set is updated in lockstep: every child is removed
        and the parent is re-added. This is the inverse of the leaf
        mutation in :meth:`_breakdown_task` — together the two
        methods keep ``self._leaf_tasks`` in sync with the DAG.

        Idempotence / no-op rules (all observable in
        ``tests/unit/test_agent_breakdown.py``):

          * Parent has no children in ``self._all_tasks`` (id prefix
            does not match anything) → return silently. This is the
            top-level-task case where there is nothing to
            aggregate.
          * At least one child is still non-terminal → return
            silently. The method is safe to call after every task
            completion; it only acts when all children are done.
          * Parent is already in a terminal status → return
            silently. Re-aggregation is a no-op (the call has
            already been made for this parent in this run).

        Side effects (in order):

          1. Set ``parent.status`` and ``parent.updated_time``.
          2. Set ``parent.failure_reason`` when aggregating a
             failure.
          3. Persist via :meth:`_persist_task_status` (atomic,
             under the lock). The persist is wrapped in
             ``try / except`` so a missing tasks.json (test
             fixtures) does not crash the aggregation.
          4. Update :attr:`_leaf_tasks`.

        Args:
            parent: The :class:`SubTask` whose children to
                aggregate. Mutated in place: ``status``,
                ``updated_time``, and (on failure) ``failure_reason``
                are updated.
        """
        # Already terminal — re-aggregation is a no-op. The
        # ``breakdown_in_progress`` status set by
        # :meth:`_breakdown_task` is NOT terminal, so the first
        # call after all children complete will take the
        # aggregation path.
        if parent.status in _TERMINAL_TASK_STATUSES:
            return

        # Children are identified by id prefix: a child of parent
        # "P" has id "P-1", "P-2", etc. The prefix "P-" is
        # sufficient because the SubTask id scheme is strictly
        # hierarchical — a child of "P" cannot accidentally share
        # the prefix with a sibling of "P".
        child_prefix = parent.id + "-"
        children = [t for t in self._all_tasks if t.id.startswith(child_prefix)]
        if not children:
            return

        # Wait until every child has reached a terminal status
        # before collapsing them. The first non-terminal child
        # means the parent is not ready to be aggregated yet —
        # the method will be re-invoked when that child
        # completes.
        non_terminal = [
            c for c in children if c.status not in _TERMINAL_TASK_STATUSES
        ]
        if non_terminal:
            return

        # Aggregate the verdict. The ordering of these branches
        # is significant: "failed" wins over "skipped" wins over
        # "completed" (the most pessimistic verdict propagates
        # upward, so a single failure in a tree of 3 children
        # surfaces as a failure on the parent).
        failed_children = [c for c in children if c.status == "failed"]
        if failed_children:
            parent.status = "failed"
            # Surface the first child's failure reason for
            # diagnostics — without this, an operator would have
            # to open each child row to see why the parent
            # failed. We use the first reason we find (input
            # order) so the surfaced text is deterministic.
            if not getattr(parent, "failure_reason", None):
                first_reason = next(
                    (
                        c.failure_reason
                        for c in failed_children
                        if getattr(c, "failure_reason", None)
                    ),
                    None,
                )
                if first_reason:
                    parent.failure_reason = (
                        f"Aggregated from {len(failed_children)} failed child(ren); "
                        f"first reason: {first_reason[:500]}"
                    )
        elif all(c.status == "skipped" for c in children):
            parent.status = "skipped"
        else:
            parent.status = "completed"

        parent.updated_time = datetime.utcnow().isoformat()

        # Persist the aggregated verdict. The persist is wrapped
        # in try/except so a missing plan_execution row (test
        # fixtures that build an agent without seeding the SQLite
        # row yet) does not crash the aggregation — the in-memory
        # state is the source of truth, and the disk write is a
        # best-effort durability step.
        from state_machine.repositories.plan_task_repository import (
            TaskProgressNotFound,
        )

        try:
            self._persist_task_status(parent)
        except TaskProgressNotFound:
            pass

        # Update the leaf set: children are no longer leaves
        # (they have all reached terminal status), the parent is
        # once again a leaf (its verdict has been decided). This
        # is the inverse of the leaf mutation in
        # :meth:`_breakdown_task`.
        for child in children:
            self._leaf_tasks.discard(child.id)
        self._leaf_tasks.add(parent.id)

    def _refine_after_failure(self, task: SubTask, coder_response: str, result_context: str,
                              file_context: str, exit_code: int):
        """
        Refine task list after a task failure.

        Args:
            task: Failed task
            coder_response: Last AI implementation attempt
            result_context: Test execution result
            file_context: Accepted for backward-compat only. The
                no-bulk-context contract (a 2026-08-19 production plan)
                forbids forwarding any repository-wide snapshot to
                the refiner; this argument is silently dropped and
                the refiner is invoked with ``file_context=""``.
            exit_code: Exit code from test command
        """
        print("Refining task list...")
        if self.logger:
            self.logger.info("refine_started", f"Refining tasks after [{task.id}] failure",
                             task_id=task.id,
                             data={"title": task.title, "result_preview": result_context[:200]})
        current_tasks_dict = [t.model_dump() for t in self.task_manager.tasks]
        try:
            updated_tasks = self.refiner.refine(
                requirement=self.task_manager.requirement,
                tasks=current_tasks_dict,
                last_coder_response=coder_response,
                last_result=result_context,
                file_context="",  # no-bulk-context: drop the snapshot
                exit_code=exit_code,
                last_task_id=task.id
            )
            # 2026-09-17 (C1): the refiner's parsed conclusion is an
            # auditable artifact, not just an in-memory return value.
            self._persist_refiner_output(
                task.id,
                {
                    "kind": "refiner_result",
                    "exit_code": exit_code,
                    "last_result_preview": result_context[:500],
                    "coder_response_chars": len(coder_response or ""),
                    "updated_tasks": updated_tasks,
                },
            )
        except Exception as e:
            self._persist_refiner_output(
                task.id,
                {
                    "kind": "refiner_error",
                    "exit_code": exit_code,
                    "last_result_preview": result_context[:500],
                    "error": str(e)[:1000],
                },
                suffix="_error",
            )
            if self.logger:
                self.logger.error("refine_failed", f"Refiner threw exception for task [{task.id}]: {e}",
                                  task_id=task.id, data={"error": str(e)[:500]})
            return

        old_tasks_repr = json.dumps(current_tasks_dict, sort_keys=True)
        new_tasks_repr = json.dumps(updated_tasks, sort_keys=True)

        if old_tasks_repr != new_tasks_repr:
            # Safety check: refiner should never drop unrelated pending tasks
            old_pending_ids = {t["id"] for t in current_tasks_dict if t.get("status") == "pending"}
            new_ids = {t["id"] for t in updated_tasks}
            dropped = old_pending_ids - new_ids
            if dropped and len(updated_tasks) < len(current_tasks_dict) * 0.5:
                print(f"Warning: Refiner dropped {len(dropped)} pending tasks. Rejecting refinement to prevent data loss.")
                if self.logger:
                    self.logger.warning("refine_rejected",
                                        f"Refiner dropped {len(dropped)} pending tasks, rejecting",
                                        task_id=task.id,
                                        data={"dropped_ids": list(dropped)})
                return

            # Enforce the workspace allow-list on NEW sub-tasks only
            # (siblings / pre-existing tasks keep their own
            # project_dir; only freshly-added sub-tasks get the
            # LLM-hallucination check). An "id" not present in the
            # pre-refinement list is a sub-task the refiner just
            # invented.
            old_ids = {t.get("id") for t in current_tasks_dict}
            for ut in updated_tasks:
                if ut.get("id") not in old_ids:
                    corrected = self._enforce_subtask_workspace(ut, task)
                    ut.clear()
                    ut.update(corrected)

            # 2026-09-16 (D1): ONE decision, TWO stores.
            #
            # This used to be two independent derivations of the same
            # thing: ``set_tasks(updated_tasks)`` rewrote ``tasks.json``
            # from the refiner's raw list, and the block below then
            # computed its own ``removed_ids`` / new-id set and applied
            # THAT to ``state.db``. The two disagreed twice over —
            # repair tasks were protected on the database side only, and
            # a split parent was dropped from disk by the whole-file
            # rewrite while its row was deleted separately. Any drift
            # between them resurrects the parent: it stays on disk with
            # no row to hydrate a terminal status from, reloads as
            # ``pending``, and the dispatcher re-executes it forever.
            #
            # ``_apply_refiner_structure`` now decides once
            # (:mod:`refiner_structure`), hands the same list to both
            # writers, and reports the delta for the audit log.
            self._apply_refiner_structure(
                current_tasks_dict, updated_tasks, task,
            )

            # CRITICAL: sync the in-memory scheduler view. Without this,
            # the next _get_active_tasks_for_scheduling() call still returns
            # the pre-refinement list, and _build_layers() uses the stale
            # self._all_tasks — so child subtasks are written to disk but
            # never scheduled, and the dispatcher re-executes the original
            # parent task forever (the dead-lock we hit in the project
            # 20260610 plan). _load_tasks() re-reads the now-fresh
            # tasks.json (written by ``_apply_refiner_structure``
            # above), so the next layer pick sees the children. Re-runs
            # also re-validate deps and leaf tasks.
            #
            # If _load_tasks() raises (e.g., refiner produced child tasks
            # with invalid deps that _validate_dependencies rejects),
            # both stores are already updated by
            # ``_apply_refiner_structure`` above. We must NOT propagate —
            # that would leave the dispatcher with a stale in-memory view
            # and a freshly-rewritten tasks.json, the worst-of-both-worlds
            # dead-lock. Instead, log loudly and let the retry loop
            # rebuild the view on the next attempt; the disk file is the
            # durable source of truth and will be re-read on the next
            # crash recovery.
            try:
                self._load_tasks()
            except (ValueError, FileNotFoundError, KeyError) as exc:
                if self.logger:
                    self.logger.error(
                        "refine_load_tasks_failed",
                        f"Failed to re-load tasks after refine of [{task.id}]; "
                        f"disk has been updated but in-memory view is stale. "
                        f"The next retry should recover via _load_tasks() in "
                        f"_run_async(). Error: {exc}",
                        task_id=task.id,
                        data={"error": str(exc)[:500]},
                    )
                # Re-raise only if this is the first attempt — the
                # retry will rebuild the view from disk anyway.
                # For now, swallowing keeps the dispatcher alive
                # without losing the disk write.
                return
            old_count = len(current_tasks_dict)
            new_count = len(self.task_manager.tasks)
            print(f"Task list updated. Total tasks: {new_count}")
            if self.logger:
                self.logger.info("refine_completed",
                                 f"Task list refined: {old_count} -> {new_count} tasks",
                                 task_id=task.id,
                                 data={"old_count": old_count, "new_count": new_count,
                                       "delta": new_count - old_count})
        else:
            if self.logger:
                self.logger.info("refine_no_change",
                                 f"Refiner returned no changes for task [{task.id}]",
                                 task_id=task.id)

    #: ``task_group`` prefix marking tasks the refiner does not own.
    #:
    #: The orchestrator writes repair tasks directly to ``state.db``
    #: (a verification point failed → repair round N), and the refiner
    #: has no knowledge of that mapping. So the refiner must not add,
    #: edit or drop them — see :mod:`refiner_structure`. Matched with
    #: ``startswith`` so it covers both legacy ``RP-*`` ids
    #: (``task_group="repair-round-N"``) and the post-v9
    #: ``R{number}-{i}`` schema.
    #:
    #: Aliased to the module constant rather than re-spelled, so the
    #: refiner guard and the audit classifier (:func:`is_repair_task`)
    #: can never drift apart on what counts as a repair task.
    _TERMINAL_REPAIR_TASK_GROUP_PREFIX = REPAIR_TASK_GROUP_PREFIX

    def _is_refiner_protected(self, task_dict: Dict[str, Any]) -> bool:
        """True when ``task_dict`` belongs to a group the refiner must not touch."""
        from refiner_structure import is_protected

        return is_protected(
            task_dict, self._TERMINAL_REPAIR_TASK_GROUP_PREFIX,
        )

    def _apply_refiner_structure(
        self,
        current_tasks_dict: List[Dict[str, Any]],
        updated_tasks: List[Dict[str, Any]],
        task: SubTask,
    ) -> None:
        """Apply the refiner's structural mutation to BOTH truth sources.

        The refiner returns a whole new task list rather than a diff, and
        two stores have to end up agreeing on it: ``tasks.json`` (the
        authored, static definition) and ``plan_tasks`` in ``state.db``
        (the runtime DAG). :func:`refiner_structure.plan_refiner_structure`
        decides once which list that is; this method is the thin writer
        that hands the *same* answer to both.

        2026-09-16 (D1). The previous shape derived the disk list and the
        database deltas separately, and the two disagreed:

          * repair tasks were protected on the database side only — a
            refiner that dropped a ``repair-*`` row from its output
            deleted it from ``tasks.json`` while the row lived on in
            SQLite, and the next ``_load_tasks`` re-merged it as an
            orphan;
          * a split parent was removed from disk by the whole-file
            rewrite and from SQLite by a separate delete. Any drift left
            the parent on disk with no row to hydrate a terminal status
            from, so it reloaded as ``pending`` and the dispatcher
            re-executed it — the "re-executes the parent forever"
            dead-lock.

        Order: **disk first, then database.** Both orders leave a window
        (a file and a SQLite table cannot be committed together), so the
        one that self-heals wins. Disk-first leaves, at worst, stale
        non-terminal rows in ``plan_tasks``. The refiner only runs after
        a task FAILED, so those rows are terminal and
        ``_load_tasks`` Phase 2 supersedes them instead of re-scheduling
        them. Database-first would instead leave a removed parent on
        disk with its row already gone — reloaded as ``pending``, which
        is the dead-lock. If the database write fails outright the disk
        write is rolled back so the two do not sit out of step until the
        next reload.

        Static edits the refiner makes to *pre-existing* tasks still
        propagate (the disk file is authoritative for static content;
        ``_load_tasks`` hydrates only ``status`` / ``failure_reason``
        from SQLite). The matching ``plan_tasks`` row keeps its original
        static text — ``update_task`` accepts runtime fields only, by
        design. Nothing reads a live task's static content from that row.

        Raises
        ------
        CycleInTaskGraph
            If the refiner introduced a dependency cycle. Raised by
            ``TaskManager.set_tasks`` *before* either store is touched,
            so a bad refinement cannot persist.

        Returns
        -------
        bool
            ``True`` when the refinement was applied, ``False`` when it
            was rejected as unconstructable and both stores were left
            untouched.
        """
        from refiner_structure import (
            plan_refiner_structure,
            unconstructable_entries,
        )

        # Reject before touching either store: the refiner is an LLM and
        # its list is untrusted. An entry that cannot become a
        # ``SubTask`` (an absolute ``files_to_modify`` path is the common
        # one) used to raise ``ValidationError`` from ``set_tasks`` and
        # escape as ``task_error`` + "No schedulable micro-layer found",
        # which looks like a dispatcher bug and strands the plan. Reject
        # it the same way the "dropped too many pending tasks" check
        # above does, and keep the previous good list.
        problems = unconstructable_entries(updated_tasks)
        if problems:
            if self.logger:
                self.logger.error(
                    "refine_rejected",
                    (
                        f"Refiner returned {len(problems)} task(s) that "
                        f"cannot be constructed; refinement rejected and "
                        f"the previous task list kept"
                    ),
                    task_id=task.id,
                    data={"unconstructable": [
                        {"task_id": tid, "why": why} for tid, why in problems
                    ]},
                )
            return False

        plan = plan_refiner_structure(
            current_tasks_dict,
            updated_tasks,
            self._TERMINAL_REPAIR_TASK_GROUP_PREFIX,
        )
        if plan.is_noop:
            return True

        # Snapshot the live SubTask objects (not a model_dump) so the
        # rollback below can restore them — including the
        # ``_origin="db_orphan"`` tag that ``model_dump()`` drops and
        # that keeps content-free placeholders off disk.
        before_tasks = list(self.task_manager.tasks)

        # --- 1. tasks.json -------------------------------------------------
        # A malformed refinement must never kill the plan. Two shapes
        # reach this call (both raised from ``set_tasks``/``save_tasks``
        # before either store is touched):
        #
        #   * ``DanglingTaskDependency`` — the refiner removed a split
        #     parent but left a ``depends_on`` edge pointing at it;
        #   * ``CycleInTaskGraph`` — a genuine cycle.
        #
        # Before 2026-09-21 neither was caught here. The exception
        # escaped ``_apply_refiner_structure`` into
        # ``_execute_task_with_retry``'s generic handler, which
        # re-labelled it for every task in the layer — including tasks
        # that had already completed successfully — and the run ended
        # with "No schedulable micro-layer found". A single bad
        # refinement took down the whole plan
        # (a production plan, 2026-09-21).
        #
        # Rejecting keeps the previous, known-good list: the failed task
        # stays failed and the rest of the plan proceeds. The refinement
        # itself is lost, which is the correct trade — an unschedulable
        # graph is strictly worse than no refinement.
        try:
            self.task_manager.set_tasks(plan.effective)
        except (CycleInTaskGraph, DanglingTaskDependency) as exc:
            if self.logger:
                self.logger.error(
                    "refine_rejected_graph",
                    (
                        f"Refiner produced an unschedulable task graph "
                        f"({type(exc).__name__}: {exc}); refinement "
                        f"rejected and the previous task list kept"
                    ),
                    task_id=task.id,
                    data={
                        "error": str(exc)[:500],
                        "error_type": type(exc).__name__,
                        "added_ids": list(plan.added_ids),
                        "removed_ids": list(plan.removed_ids),
                    },
                )
            return False

        # --- 2. state.db ---------------------------------------------------
        if self.plan_id:
            try:
                from state_machine.repositories.plan_task_repository import (
                    ALLOWED_STATIC_TASK_FIELDS,
                    TaskProgressConflictError,
                    TaskProgressNotFound,
                    TaskProgressValidationError,
                )
                task_progress_repo = self._get_task_progress_repository()

                # 2a. Delete the removed parents (e.g. ``40`` after the
                #     refiner split it into ``40-1``/``40-2``/``40-3``).
                #     Repair tasks cannot appear here — the plan
                #     reinstates them.
                for tid in plan.removed_ids:
                    try:
                        task_progress_repo.delete_task(self.plan_id, tid)
                    except (
                        TaskProgressNotFound,
                        TaskProgressValidationError,
                    ):
                        # Idempotent — a missing row is fine.
                        pass

                # 2b. Insert the new children.
                current_ids = {
                    str(t.get("id"))
                    for t in current_tasks_dict
                    if t.get("id")
                }
                for entry in plan.effective:
                    tid = str(entry.get("id"))
                    if tid in current_ids:
                        # Pre-existing task — its row already exists and
                        # carries the runtime state. Never re-create it:
                        # ``add_task`` resets ``status`` to ``pending``.
                        continue
                    sub_task_payload = {
                        k: entry.get(k)
                        for k in ALLOWED_STATIC_TASK_FIELDS
                        if k != "id" and entry.get(k) is not None
                    }
                    sub_task_payload["id"] = tid
                    try:
                        task_progress_repo.add_task(
                            self.plan_id, sub_task_payload,
                        )
                    except (
                        TaskProgressConflictError,
                        TaskProgressNotFound,
                        TaskProgressValidationError,
                    ) as exc:
                        # A conflict on a brand-new sub-task means a
                        # concurrent refiner round already wrote it —
                        # fine, keep the existing entry. Other errors are
                        # logged and skipped: the refiner must not abort
                        # on a single sub-task.
                        if self.logger is not None:
                            try:
                                self.logger.warning(
                                    "refine_add_subtask_skipped",
                                    f"Skipped add of sub-task {tid!r}: {exc}",
                                    data={
                                        "task_id": tid,
                                        "error": str(exc)[:300],
                                    },
                                )
                            except Exception:
                                pass
            except Exception as exc:  # noqa: BLE001
                # The two stores would sit out of step until the next
                # reload, so undo the disk write rather than leave it.
                # A crash here is the only remaining window; it leaves
                # the disk file post-refinement and the stale rows
                # terminal (see the ordering note above).
                try:
                    self.task_manager.tasks = before_tasks
                    self.task_manager.save_tasks()
                except Exception as rollback_exc:  # noqa: BLE001
                    if self.logger is not None:
                        try:
                            self.logger.error(
                                "refine_structure_rollback_failed",
                                (
                                    f"Could not roll tasks.json back after a "
                                    f"state.db write failure for plan "
                                    f"{self.plan_id}: {rollback_exc}. The two "
                                    f"stores are out of step until the next "
                                    f"_load_tasks()."
                                ),
                                data={"error": str(rollback_exc)[:500]},
                            )
                        except Exception:
                            pass
                if self.logger is not None:
                    try:
                        self.logger.error(
                            "refine_structure_rolled_back",
                            (
                                f"Refiner state.db write failed for plan "
                                f"{self.plan_id}: {exc}. tasks.json was "
                                f"rolled back to the pre-refinement list."
                            ),
                            task_id=task.id,
                            data={"error": str(exc)[:500]},
                        )
                    except Exception:
                        pass
                return False

        # --- 3. audit ------------------------------------------------------
        if self.logger is not None:
            try:
                self.logger.info(
                    "refine_structure_applied",
                    (
                        f"Refiner restructured the task list: "
                        f"+{len(plan.added_ids)} added, "
                        f"-{len(plan.removed_ids)} removed "
                        f"({len(plan.effective)} tasks in the DAG)"
                    ),
                    task_id=task.id,
                    data=plan.to_dict(),
                )
            except Exception:
                pass

            if plan.reinstated_ids or plan.reverted_ids:
                # The refiner is not supposed to know repair tasks
                # exist. Dropping one is a forgotten row; rewriting one
                # is an attempt to edit a task bound to a verification
                # round it never saw. Both were undone — surfaced so the
                # pattern is visible instead of silent.
                try:
                    self.logger.warning(
                        "refine_touched_protected_tasks",
                        (
                            f"Refiner touched {len(plan.reinstated_ids)} "
                            f"dropped and {len(plan.reverted_ids)} edited "
                            f"repair task(s); all restored from the "
                            f"pre-refinement list"
                        ),
                        task_id=task.id,
                        data={
                            "reinstated_ids": list(plan.reinstated_ids),
                            "reverted_ids": list(plan.reverted_ids),
                        },
                    )
                except Exception:
                    pass

        return True


def autonomous_coding(
    requirement: str,
    project_dir: str,
    recover: bool = False,
    max_tasks: Optional[int] = None,
    config_name: Optional[str] = None,
    tool: Optional[str] = None,
    logger: Optional[ExecutionLogger] = None,
    verification_tasks_file: Optional[str] = None,
    tasks_file: Optional[Path] = None,
    dynamic_tracker: Optional[ActiveConcurrencyTracker] = None,
    in_flight_guard: Optional[object] = None,
):
    """
    Fully autonomous software development from requirement to completion.

    Args:
        requirement: High-level requirement description
        project_dir: Target project directory
        recover: Whether to skip planning and recover from crash
        max_tasks: Maximum number of tasks to execute
        config_name: Name of configuration to use (e.g., 'coding', 'harmonyos')
        tool: Coding tool to use ('opencode' or 'claude', default: 'opencode')
        logger: Optional ExecutionLogger for structured logging
        dynamic_tracker: Optional :class:`ActiveConcurrencyTracker`
            from the FastAPI lifespan's
            :class:`backend.runtime_state.RuntimeState`. When ``None``
            (e.g. CLI invocation without a lifespan) a fresh
            process-local tracker is constructed so the dispatch
            path remains correct. The production backend threads
            the lifespan's tracker through here so all in-process
            dispatches share one slot-count surface.
        in_flight_guard: Optional
            :class:`scheduling.guard.InFlightFileGuard` from the
            FastAPI lifespan's ``RuntimeState``. Currently unused
            inside ``autonomous_coding`` directly — kept on the
            signature so the orchestrator passes the same handle
            down to ``AutonomousAgent`` for in-process task
            coordination. ``None`` is acceptable for ad-hoc
            invocations.
    """
    project_path = Path(project_dir).resolve()

    # Resolve the per-process dynamic tracker. The TC-006 refactor
    # removed the module-level ``_DYNAMIC_TRACKER`` singleton; the
    # lifespan's tracker (passed in via ``dynamic_tracker``) is the
    # new single source of truth. CLI invocations that bypass the
    # backend get a fresh per-call tracker.
    runtime_state_dynamic_tracker: ActiveConcurrencyTracker
    if dynamic_tracker is None:
        runtime_state_dynamic_tracker = ActiveConcurrencyTracker()
    else:
        runtime_state_dynamic_tracker = dynamic_tracker

    # Load configuration FIRST so the coding tool can read its model_map.
    # (Per-task complex/medium model selection is driven by config.model_map
    # + task.model_type; see coding_tool._resolve_model.)
    config = None
    if config_name:
        from config_loader import load_config_by_name
        config = load_config_by_name(config_name)
        if config:
            print(f"Using configuration: {config_name}")
        else:
            print(f"Configuration '{config_name}' not found, using default")
            config = ConfigRegistry.get('coding')
    else:
        config = ConfigRegistry.get('coding')

    model_map = getattr(config, "model_map", None) or {}

    tool = (tool or "claude").lower()
    if tool == "claude":
        # Build a SubagentConfig (decisions 2/3/4) that unifies the
        # 7+ scattered kwargs previously passed to ClaudeCodingTool
        # (provider, model, base_url, api_key, auth_token, settings,
        # hook_scripts, ...). The same dataclass instance can be
        # serialized to a tmpfile via write_tmp_settings() (becomes the
        # --settings flag) and used to inject PostToolUse hook scripts
        # with task_type / task_summary pass-through.
        from subagent_config import SubagentConfig

        # Resolve the live provider fallback chain through the
        # provider_order loader (reads ``provider-order.json`` and
        # filters the ``order`` list through the CC Switch consumer
        # layer). There is no hard-coded list and no YAML fallback
        # anymore — the contract file is the single source of truth.
        # The PDT_PROVIDER_PRIORITY env var still wins when set,
        # preserving the operator escape hatch.
        # Inherit mode (2026-09-24): CC Switch is absent, so there is no
        # contract file to read, no row to look up, no chain to order and
        # no pool to draw a slot from. Skipping the walk is not merely an
        # optimisation — ``load_fallback_order()`` *raises* when the
        # provider-order file is missing, so on a machine with neither CC
        # Switch nor an optimizer this whole dispatch used to die before
        # reaching the model. That is the bug inherit mode exists for.
        inherit_env = ClaudeCodingTool._detect_inherit_mode()
        if inherit_env:
            provider_priority_list = []
            provider_info = {"provider_name": "", "base_url": "", "api_key": ""}
            log.info(
                "provider inherit mode: CC Switch not found — sub-agents will "
                "resolve provider configuration from Claude Code's own settings"
            )
        else:
            env_override = os.environ.get("PDT_PROVIDER_PRIORITY", "").strip()
            if env_override:
                provider_priority_list = [
                    p.strip() for p in env_override.split(",") if p.strip()
                ]
                log.info(
                    "using PDT_PROVIDER_PRIORITY env override: %s",
                    provider_priority_list,
                )
            else:
                from provider_order import load_fallback_order

                provider_priority_list = load_fallback_order()
                log.info(
                    "resolved provider fallback chain via load_fallback_order(): %s "
                    "(source: using provider-order.json or fallback to config.yaml)",
                    provider_priority_list,
                )

            provider_info = _load_provider_info(
                provider_priority_list,
                tracker=runtime_state_dynamic_tracker,
                # 2026-09-17 — take the slot INSIDE the walk so the capacity
                # check and the increment are one atomic step, and so the
                # fleet-wide ceiling is consulted at all. The previous shape
                # read ``current()`` here and acquired at the dispatch site
                # below, which is racy under concurrent dispatches and
                # ignored the global cap entirely.
                reserve_slot=True,
            )
        provider_name = provider_info["provider_name"]
        base_url = provider_info["base_url"]
        api_key = provider_info["api_key"]
        log.info(
            "selected provider for sub-agent: %s (base_url=%s)",
            provider_name,
            base_url,
        )

        # Dynamic 5h-cap slot bookkeeping. ``_load_provider_info`` above
        # already TOOK the slot during its walk (``reserve_slot=True``),
        # so this only wraps it in the releasable context the caller's
        # ``finally`` expects — ``already_reserved`` prevents a second
        # ``acquire()``, which would double-count and halve every
        # provider's effective concurrency. When the chain had no
        # eligible provider (empty ``provider_name``), no slot exists —
        # the parent process's CC Switch proxy is the fallback, not a
        # tracked slot.
        _slot_ctx = _acquire_dispatch_slot(
            provider_name, runtime_state_dynamic_tracker,
            already_reserved=True,
        )

        hook_scripts = [
            Path("backend/coding_tool_hooks/pre_tool_use.sh"),
            Path("backend/coding_tool_hooks/post_tool_use.sh"),
        ]

        subagent_cfg = SubagentConfig(
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            auth_token=api_key,
            # 2026-09-13: no model_map here — model env comes from the
            # CC Switch provider row via the dispatch walk.
            hook_scripts=hook_scripts,
            task_type=os.environ.get("PDT_TASK_TYPE", "general"),
            task_summary=requirement or "",
            # Inherit mode: the settings file carries hooks and nothing
            # else. Without this the file would still be written with an
            # empty ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN block — and
            # because --settings outranks ~/.claude/settings.json, those
            # empty values would *replace* the user's working Claude Code
            # configuration rather than leave it alone.
            inherit_env=inherit_env,
        )

        try:
            tmp_settings_path = subagent_cfg.write_tmp_settings(logger=logger)
        except OSError:
            # /tmp unwritable or write failed — do NOT construct
            # ClaudeCodingTool with a stale or missing tmpfile.
            raise

        hook_stdin = {
            "task_type": subagent_cfg.task_type,
            "task_summary": subagent_cfg.task_summary,
        }

        coding_tool = ClaudeCodingTool(
            cwd=str(project_path),
            logger=logger,
            model_map=model_map,
            settings=tmp_settings_path,
            hook_stdin=hook_stdin,
            base_url=base_url,
            auth_token=api_key,
        )
        print("Using Claude as the coding tool.")
    elif tool == "vendor-c":
        coding_tool = VendorCCodingTool()
        print("Using Vendor C Code as the coding tool.")
    else:
        coding_tool = OpenCodeCodingTool()

    agent = AutonomousAgent(
        requirement=requirement,
        project_dir=project_path,
        coding_tool=coding_tool,
        config=config,
        logger=logger,
        tasks_file=tasks_file,
    )
    # Expose SubagentConfig on the agent so ``_execute_task_with_retry``
    # can regenerate the settings tmpfile per task (decisions 2/3 + 4).
    # Each task gets its own ``/tmp/subagent_settings_<uuid>.json`` so
    # the Claude subprocess launched for that task is wired with a
    # unique ``--settings`` path; see ``_execute_task_with_retry``.
    agent.subagent_cfg = subagent_cfg

    if requirement and not recover:
        agent.plan()

    # Per-task timeout handed to the executor loop.
    #
    # 2026-09-15: the LLM call itself NO LONGER takes this
    # value — the execution-phase call was unified with every other call
    # (900s of stdout silence → adaptive watcher, 1800s idle pipe, 1-hour
    # layer ceiling), so an explicit number here would REPLACE that window
    # rather than nest inside it. The value is still threaded through
    # ``run`` / ``_run_task_with_provider_slot`` / ``_execute_task_with_retry``
    # so existing callers and tests keep working, and the background-mode
    # branch keeps its "previous timeout → background" heuristic.
    executor_task_timeout = int(os.getenv("EXECUTOR_TASK_TIMEOUT", "1800"))
    try:
        if max_tasks:
            agent.run(max_tasks=max_tasks, timeout=executor_task_timeout)
        else:
            agent.run(timeout=executor_task_timeout)
    finally:
        # Release the dynamic-cap slot regardless of how ``agent.run``
        # returns (success, exception, or ``KeyboardInterrupt``). The
        # tracker would otherwise leak a slot per crash and
        # permanently reduce that provider's effective cap.
        _release_dispatch_slot(_slot_ctx)