"""The refiner's structural mutation, decided once for both truth sources.

The refiner returns a whole new task list, not a diff. Two stores have
to end up agreeing on it:

  * ``tasks.json`` — the authored, static definition (written by
    :meth:`TaskManager.set_tasks`);
  * ``plan_tasks`` in ``state.db`` — the runtime DAG the dispatcher
    schedules from (written by :class:`PlanTaskRepository`).

Until 2026-09-16 the agent fed the *raw* LLM list to the disk writer and
a *separately derived* id diff to the database writer. The two
derivations disagreed in two ways, both observable:

1. **Repair tasks.** The database path skipped any task whose
   ``task_group`` starts with ``repair`` (the orchestrator owns those;
   the refiner has no knowledge of the VP-failed → repair-round
   mapping), but the disk path had no such guard. A refiner that
   dropped a ``repair-*`` row from its returned list deleted it from
   ``tasks.json`` while the row lived on in SQLite — and on the next
   ``_load_tasks`` the row came back through the orphan-reconcile step.
   The task flickered between stores instead of simply surviving.

2. **Split parents.** The database path deleted the parent row; the
   disk path relied on the whole-file rewrite to drop it. If the two
   ever ran out of step the parent stayed on disk with no row to
   hydrate a terminal status from, came back as ``pending``, and the
   dispatcher re-executed it — the "re-executes the original parent
   task forever" dead-lock.

The fix is to make the decision **once**, here, as a pure function, and
hand the *same* answer to both writers. :func:`plan_refiner_structure`
takes the pre-refinement list and the refiner's returned list and
produces the single list both stores must converge on, plus the ids that
were added, removed, reinstated or reverted — so the caller can also log
what happened instead of leaving an operator to diff two files.

This module has no I/O and imports nothing from the agent, so the rules
can be tested directly (see ``tests/unit/test_refiner_structure.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

#: ``task_group`` prefix marking tasks the refiner does not own. Matched
#: with ``startswith`` so it covers both the legacy ``RP-*`` ids
#: (``task_group="repair-round-N"``) and the post-v9 ``R{number}-{i}``
#: schema. ``agent.AutonomousAgent._TERMINAL_REPAIR_TASK_GROUP_PREFIX``
#: carries the same literal; it is passed in explicitly rather than
#: imported so this module stays dependency-free.
DEFAULT_PROTECTED_TASK_GROUP_PREFIX = "repair"

#: Static fields compared to decide whether the refiner *edited* a
#: protected task. Runtime fields (``status``, ``failure_reason``, …)
#: are deliberately excluded: the LLM echoes stale copies of them and a
#: difference there is not an edit worth reverting.
_PROTECTED_COMPARE_FIELDS: Tuple[str, ...] = (
    "title",
    "description",
    "test_command",
    "test_commands",
    "files_to_modify",
    "depends_on",
    "model_type",
    "project_dir",
)


def is_protected(
    task: Any,
    prefix: str = DEFAULT_PROTECTED_TASK_GROUP_PREFIX,
) -> bool:
    """True when ``task`` belongs to a task group the refiner must not touch."""
    try:
        group = task.get("task_group")
    except AttributeError:
        return False
    return isinstance(group, str) and bool(group) and group.startswith(prefix)


def _content_of(task: Any) -> Dict[str, Any]:
    """The comparable static content of a task dict (see compare fields)."""
    try:
        return {f: task.get(f) for f in _PROTECTED_COMPARE_FIELDS}
    except AttributeError:
        return {}


def unconstructable_entries(
    updated_tasks: Sequence[Any],
) -> List[Tuple[str, str]]:
    """``[(task_id, reason)]`` for refiner entries that cannot become a task.

    The refiner is an LLM, so its returned list is untrusted input. A
    single entry that does not satisfy :class:`task.SubTask` — most
    commonly an absolute ``files_to_modify`` path, which the model
    reaches for when the file it has in mind lives outside the executor's
    ``project_dir`` — used to be discovered only when
    ``TaskManager.set_tasks`` constructed the model, at which point the
    ``pydantic.ValidationError`` propagated out of
    ``_refine_after_failure``, was caught by the generic task handler, and
    surfaced as ``task_error`` followed by ``execution_stopped | No
    schedulable micro-layer found``. That reads like a dispatcher bug and
    strands the plan; it is neither — the refiner returned junk, which is
    a normal thing for an LLM to do and must be rejected like the other
    ``refine_rejected`` cases rather than allowed to crash the loop.

    Validating here rather than inside the writer keeps the check ahead
    of *both* stores, so a rejected refinement leaves the previous good
    task list exactly as it was.
    """
    from task import SubTask

    problems: List[Tuple[str, str]] = []
    for entry in updated_tasks or []:
        if not isinstance(entry, dict):
            problems.append((
                "<non-dict>",
                f"entry is {type(entry).__name__}, expected a mapping",
            ))
            continue
        tid = str(entry.get("id") or "<missing id>")
        try:
            SubTask(**entry)
        except Exception as exc:  # noqa: BLE001 - any validation failure
            problems.append((tid, f"{type(exc).__name__}: {str(exc)[:300]}"))
    return problems


@dataclass(frozen=True)
class RefinerStructurePlan:
    """The single answer both stores must converge on."""

    #: The list to write to ``tasks.json`` **and** to reconcile
    #: ``plan_tasks`` against. Protected tasks are restored to their
    #: pre-refinement content; dropped protected tasks are re-inserted.
    effective: List[Dict[str, Any]]

    #: Present before the refinement, absent from :attr:`effective` —
    #: these are the split/replaced parents, removed from both stores.
    removed_ids: Tuple[str, ...]

    #: Absent before, present in :attr:`effective` — the new children.
    added_ids: Tuple[str, ...]

    #: Protected tasks the refiner dropped and this plan put back.
    reinstated_ids: Tuple[str, ...]

    #: Protected tasks the refiner rewrote; their original content was
    #: restored. Reported separately from :attr:`reinstated_ids` because
    #: the two mean different things to an operator ("the LLM forgot
    #: about the repair round" vs "the LLM tried to edit it").
    reverted_ids: Tuple[str, ...]

    @property
    def is_noop(self) -> bool:
        return not (self.removed_ids or self.added_ids or self.reinstated_ids
                    or self.reverted_ids)

    def to_dict(self) -> Dict[str, Any]:
        """Log payload. Lists, not sets, so the JSON is stable."""
        return {
            "added_ids": list(self.added_ids),
            "removed_ids": list(self.removed_ids),
            "reinstated_ids": list(self.reinstated_ids),
            "reverted_ids": list(self.reverted_ids),
            "effective_count": len(self.effective),
        }


def plan_refiner_structure(
    current_tasks: Sequence[Dict[str, Any]],
    updated_tasks: Sequence[Dict[str, Any]],
    protected_prefix: str = DEFAULT_PROTECTED_TASK_GROUP_PREFIX,
) -> RefinerStructurePlan:
    """Decide the one task list both stores end up with.

    Parameters
    ----------
    current_tasks:
        The pre-refinement list (``SubTask.model_dump()`` of everything
        the dispatcher knew about), including protected repair tasks.
    updated_tasks:
        The refiner's returned list, verbatim.
    protected_prefix:
        ``task_group`` prefix the refiner may not add to, edit or drop.

    Returns
    -------
    RefinerStructurePlan
        See the attribute docs. ``effective`` preserves the refiner's
        ordering for the tasks it returned; reinstated protected tasks
        are appended, because task order is not load-bearing (the DAG
        is layered by ``depends_on``) and appending keeps the refiner's
        own ordering readable.

    Notes
    -----
    A protected task present in :attr:`RefinerStructurePlan.effective`
    is always the **pre-refinement** dict, never the refiner's version.
    Letting the refiner edit a repair task would let it rewrite a task
    whose id is bound to a verification round it never saw.
    """
    current_by_id: Dict[str, Dict[str, Any]] = {}
    for task in current_tasks or []:
        tid = task.get("id") if isinstance(task, dict) else None
        if tid:
            current_by_id[str(tid)] = task

    # Keep the refiner's first-seen order and drop duplicate ids: a list
    # with the same id twice would otherwise be written to both stores
    # twice and lose its runtime state on the second pass. First
    # occurrence wins, so the kept entry is the one whose position in
    # ``order`` is also the one used.
    refiner_by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for task in updated_tasks or []:
        tid = task.get("id") if isinstance(task, dict) else None
        if not tid:
            continue
        tid = str(tid)
        if tid in refiner_by_id:
            continue
        order.append(tid)
        refiner_by_id[tid] = task

    reinstated: List[str] = []
    reverted: List[str] = []
    effective_by_id: Dict[str, Dict[str, Any]] = dict(refiner_by_id)

    for tid, original in current_by_id.items():
        if not is_protected(original, protected_prefix):
            continue
        replacement = refiner_by_id.get(tid)
        if replacement is None:
            reinstated.append(tid)
        elif _content_of(replacement) != _content_of(original):
            reverted.append(tid)
        # Protected: the pre-refinement dict wins unconditionally.
        effective_by_id[tid] = original

    effective: List[Dict[str, Any]] = [
        effective_by_id[tid] for tid in order if tid in effective_by_id
    ]
    for tid in reinstated:
        effective.append(effective_by_id[tid])

    effective_ids = {str(t["id"]) for t in effective}
    return RefinerStructurePlan(
        effective=effective,
        removed_ids=tuple(sorted(set(current_by_id) - effective_ids)),
        added_ids=tuple(sorted(effective_ids - set(current_by_id))),
        reinstated_ids=tuple(sorted(reinstated)),
        reverted_ids=tuple(sorted(reverted)),
    )
