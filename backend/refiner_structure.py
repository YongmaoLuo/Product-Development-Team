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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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

#: The fields that make up a task's **judging command** — the objective
#: half of the dual-criterion completion rule.
#:
#: Deliberately separate from ``_PROTECTED_COMPARE_FIELDS``, because the
#: rule they carry is universal rather than group-scoped. A task that can
#: rewrite its own judge is grading its own homework whether or not it
#: belongs to the repair group, and the fields the refiner may
#: legitimately refine (``title``, ``description``, ``depends_on``,
#: ``files_to_modify``) are exactly the ones that must NOT be frozen.
_JUDGE_FIELDS: Tuple[str, ...] = ("test_command", "test_commands")


def _split_parent_id(
    task_id: str,
    candidates: Mapping[str, Any],
) -> Optional[str]:
    """The id ``task_id`` is a split child of, if any.

    The convention is ``{parent}-{n}``. Ids are hierarchical, so the
    matching parent is the **longest** candidate the child extends: with
    both ``1`` and ``1-1`` present, ``1-1-2`` is a child of ``1-1``, not
    of ``1``. A bare ``startswith`` on the id would also read
    ``40-10``… as a child of ``4``, which is why the separator is part of
    the test (the same trap ``test_sibling_prefix_is_not_mistaken_for_a_split``
    documents for the parent→children direction).
    """
    best: Optional[str] = None
    for candidate in candidates:
        if not candidate or candidate == task_id:
            continue
        if task_id.startswith(str(candidate) + "-") and (
            best is None or len(str(candidate)) > len(best)
        ):
            best = str(candidate)
    return best


def _judge_commands(task: Any) -> Tuple[Tuple[str, ...], str]:
    """Normalised ``(list_form, single_form)`` view of a task's judge.

    Normalised because the two shapes are interchangeable and an LLM
    echoes them inconsistently: ``test_commands: []`` and an absent
    ``test_commands`` mean the same thing, and so do ``""`` and a missing
    ``test_command``. Comparing raw values would report a rewrite every
    time the model switched shape without changing the command.
    """
    try:
        raw_list = task.get("test_commands") or []
        raw_single = task.get("test_command") or ""
    except AttributeError:
        return (), ""
    commands = tuple(
        str(item).strip() for item in raw_list if str(item or "").strip()
    )
    return commands, str(raw_single).strip()


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

    #: **Any** task — protected or not — whose judging command the
    #: refiner tried to change. The original command was restored and the
    #: rest of the refiner's edit kept. Distinct from
    #: :attr:`reverted_ids` on purpose: that one means "the LLM tried to
    #: edit a repair task", this one means "the LLM tried to rewrite the
    #: test that grades a task". An operator needs to tell those apart —
    #: the second is the signature of a refiner that has decided the
    #: cheapest way to pass is to change the judge.
    judge_rewritten_ids: Tuple[str, ...] = ()

    #: Split children whose judging command was replaced by their
    #: parent's. These are new ids, so :attr:`judge_rewritten_ids` cannot
    #: see them — there is no before-state to compare against — which is
    #: exactly how a refiner gets a fresh, self-authored judge every time
    #: it splits a failed task.
    judge_inherited_ids: Tuple[str, ...] = ()

    @property
    def is_noop(self) -> bool:
        return not (self.removed_ids or self.added_ids or self.reinstated_ids
                    or self.reverted_ids or self.judge_rewritten_ids
                    or self.judge_inherited_ids)

    def to_dict(self) -> Dict[str, Any]:
        """Log payload. Lists, not sets, so the JSON is stable."""
        return {
            "added_ids": list(self.added_ids),
            "removed_ids": list(self.removed_ids),
            "reinstated_ids": list(self.reinstated_ids),
            "reverted_ids": list(self.reverted_ids),
            "judge_rewritten_ids": list(self.judge_rewritten_ids),
            "judge_inherited_ids": list(self.judge_inherited_ids),
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

    # ------------------------------------------------------------------
    # The judging command belongs to the task, not to the refiner.
    #
    # 2026-10-11. The refiner's answer to a failure is almost always
    # "split the failed task in three": the 20261010-CC-Switch-Remote-Aut
    # plan grew 31 → 55 tasks over eleven refinements, every one of them
    # logged as ``+3 added, -1 removed``. The environment defect behind
    # those failures (``cargo`` missing from the executor's PATH) was
    # indistinguishable, from inside the refiner, from a defective
    # command — so it took the shortest path to green and rewrote the
    # test, prefixing 25 commands with ``PATH="$HOME/.cargo/bin:$PATH"``
    # and titling the new tasks "让目标测试命令对 PATH 免疫".
    #
    # Nothing caught it. ``is_protected`` below only guards
    # ``task_group.startswith("repair")``, and every task in that plan
    # had ``task_group = None``, so the protection fired zero times; and
    # the split children were new ids, so no before/after comparison
    # could see the command they invented. Both halves are closed here.
    # ------------------------------------------------------------------

    # (1) A split child inherits its parent's judging command.
    #
    # The parent's command already passed the generation-time shape and
    # falsifiability gates, and it is already RED (the parent failed),
    # so reusing it costs nothing and removes the refiner's only means of
    # authoring a fresh judge. A child whose scope genuinely needs a
    # different command is a *verification-point* defect: the established
    # exit for that is to retire the VP and add a new one
    # (``repair_generator``: "绝不修改已有验证点……test_command 都不许改"),
    # never an in-place rewrite of the thing that grades the task.
    judge_inherited: List[str] = []
    for tid in order:
        if tid in current_by_id:
            # Pre-existing: rule (2) below governs it, not this one.
            continue
        parent_id = _split_parent_id(tid, current_by_id)
        if parent_id is None:
            continue
        child = refiner_by_id.get(tid)
        if not isinstance(child, dict):
            continue
        parent = current_by_id[parent_id]
        if not any(parent.get(f) for f in _JUDGE_FIELDS):
            # A parent with no command has nothing to bequeath; leaving
            # the child's own (or absent) command alone is correct.
            continue
        patched = dict(child)
        changed = False
        for field in _JUDGE_FIELDS:
            if field in parent:
                if patched.get(field) != parent[field]:
                    patched[field] = parent[field]
                    changed = True
            elif field in patched:
                patched.pop(field)
                changed = True
        if changed:
            refiner_by_id[tid] = patched
            judge_inherited.append(tid)

    reinstated: List[str] = []
    reverted: List[str] = []
    effective_by_id: Dict[str, Dict[str, Any]] = dict(refiner_by_id)

    for tid, original in current_by_id.items():
        if not is_protected(original, protected_prefix):
            continue
        # A task the refiner split into ``{tid}-1``/``{tid}-2``/… is
        # superseded by those children. Protection exists so the
        # refiner cannot rewrite a repair task's *content*; it must
        # not keep a split parent alive beside its own children.
        #
        # Dropping the parent here is what puts it in ``removed_ids``
        # and lets the caller delete the row. While a protected parent
        # survived, ``removed_ids`` stayed empty, the row was never
        # deleted, and ``record_task_failure`` then pinned it at
        # ``failed`` beside children that had all completed.
        # 2026-10-05: four repair parents were stranded this way.
        if any(cid.startswith(f"{tid}-") for cid in refiner_by_id):
            effective_by_id.pop(tid, None)
            continue
        replacement = refiner_by_id.get(tid)
        if replacement is None:
            reinstated.append(tid)
        elif _content_of(replacement) != _content_of(original):
            reverted.append(tid)
        # Protected: the pre-refinement dict wins unconditionally.
        effective_by_id[tid] = original

    # (2) The judging command is write-once for EVERY task, not just the
    # protected ones.
    #
    # This runs after the protected loop so it cannot disturb that
    # loop's ``reverted`` verdict, which compares whole content and must
    # keep seeing the refiner's raw entry. For a non-protected task the
    # refiner is free to refine everything that describes the work — its
    # title, its description, its dependencies, which files it touches —
    # but not the command that decides whether the work is done. Only
    # the judge fields are restored; everything else the refiner wrote
    # stands, so a legitimate refinement is not thrown away.
    judge_rewritten: List[str] = []
    for tid, original in current_by_id.items():
        if is_protected(original, protected_prefix):
            continue  # handled above: the whole original dict already won
        replacement = effective_by_id.get(tid)
        if not isinstance(replacement, dict):
            continue
        if _judge_commands(replacement) == _judge_commands(original):
            continue
        patched = dict(replacement)
        for field in _JUDGE_FIELDS:
            if field in original:
                patched[field] = original[field]
            else:
                patched.pop(field, None)
        effective_by_id[tid] = patched
        judge_rewritten.append(tid)

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
        judge_rewritten_ids=tuple(
            sorted(i for i in judge_rewritten if i in effective_ids)
        ),
        judge_inherited_ids=tuple(
            sorted(i for i in judge_inherited if i in effective_ids)
        ),
    )
