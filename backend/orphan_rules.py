"""Shared rules for reconciling ``plan_tasks`` orphan rows.

An **orphan** is a ``plan_tasks`` row whose ``task_id`` is not in the
on-disk ``tasks.json``. The refiner rewrites the task list, so split or
replaced rows stay behind in the database.

Two independent loaders meet orphan rows and each has to decide, per
row, between *merging* it (build a runnable ``SubTask`` from the row's
real fields) and substituting a *placeholder* (synthesise a
content-free ``SubTask`` so the row is still visible):

  * ``AutonomousAgent._load_tasks`` Phase 2 (``agent.py``)
  * ``TaskManager._hydrate_db_only_orphans`` (``task_manager.py``)

The predicate lives here rather than in either caller so the two cannot
drift apart — they already did once, and the cost was a task whose real
description was replaced by a placeholder (see
:data:`MERGE_REQUIRED_FIELDS`).
"""

from typing import Any, Iterable

#: Fields an orphan row must carry for a loader to merge its REAL content
#: instead of substituting a content-free placeholder.
#:
#: ``test_command`` is deliberately absent. It used to be required, which
#: meant an orphan that had a title and a full description but no command
#: was overwritten by the placeholder's
#: ``"recovered from plan_tasks DB (no static fields on disk...)"`` text:
#: the real content was destroyed by the loader, and the subagent then
#: reported — accurately, about the placeholder it had been handed — that
#: it had "no description, no files_to_modify, and no test_command". The
#: an earlier plan lost repair tasks carrying a full description and a
#: real test command that way.
#:
#: A missing command is a real gap, but it is not grounds for discarding
#: a runnable task: ``_cross_verify_test_result`` already degrades
#: gracefully for a task with no command (``test_cross_verify_unverified``
#: plus the audit second pass). Losing the task entirely is strictly
#: worse than running it with a weaker second signal.
MERGE_REQUIRED_FIELDS = ("title", "description")

#: Deprecated alias kept so the existing import in ``agent.py`` and its
#: tests keep working. Prefer :data:`MERGE_REQUIRED_FIELDS`.
_ORPHAN_MERGE_REQUIRED_FIELDS = MERGE_REQUIRED_FIELDS


def has_mergeable_content(entry: Any) -> bool:
    """True when an orphan row carries enough to run as a real task.

    Whitespace-only values do not count — a title of ``"   "`` is no more
    actionable than a missing one. A malformed entry (``None``, or
    something that is not a mapping) is not mergeable rather than an
    error: orphan reconciliation is best-effort and must not be able to
    bring down ``_load_tasks``.
    """
    try:
        return all(
            str(entry.get(f) or "").strip()
            for f in MERGE_REQUIRED_FIELDS
        )
    except AttributeError:
        return False


def missing_merge_fields(entry: Any) -> Iterable[str]:
    """The required fields ``entry`` does not supply — for logging."""
    try:
        return [
            f for f in MERGE_REQUIRED_FIELDS
            if not str(entry.get(f) or "").strip()
        ]
    except AttributeError:
        return list(MERGE_REQUIRED_FIELDS)


def merged_subtask_kwargs(entry: Any) -> dict:
    """The ``SubTask`` keyword arguments to build a merged orphan.

    Only the static (identity + intent) fields are carried. Runtime state
    — ``status``, ``end_ts``, ``failure_reason`` — is added by the caller
    so each loader keeps ownership of how it sources those.
    """
    return {
        k: entry.get(k)
        for k in (
            "id", "title", "description", "test_command",
            "files_to_modify", "depends_on", "model_type",
            "project_dir", "provider",
            # 2026-09-16: carried so a merged orphan keeps the two
            # behaviour-bearing flags its row declares. ``task_group``
            # in particular decides whether the refiner is allowed to
            # delete the task at all — an orphan merged without it
            # became deletable.
            "task_group", "verification_only",
        )
        if entry.get(k) is not None
    }
