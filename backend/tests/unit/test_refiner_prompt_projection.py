"""
The refiner is told only what it is refining, and returns only a delta.

Background (a 2026-10-14 production plan, that plan's repair round):

  Context size: prompt=103394 sys=952 total=104346 tokens (~310182 chars)

of which ``json.dumps(tasks, indent=2)`` was 298,664 chars -- 96%. Every
one of those 52 tasks was in the prompt because the old system prompt
said "You MUST include ALL tasks in your output ... NEVER drop tasks
that are unrelated to the failure", i.e. the refiner was handed a table
it was forbidden to edit and then required to transcribe it back. The
exposure was not the token count: it was that 52 unrelated rows were
one transcription slip away from being silently rewritten.

So the contract is now the other way round:

  * ``Current Tasks`` carries the failed task in full and reduces every
    other task to ``id``/``title``/``status``/``depends_on``.
  * The reply is a **delta**. Tasks the reply does not mention are
    preserved by :meth:`TaskRefiner._merge_refiner_delta`, which runs
    inside ``refine()`` -- so ``refine()`` still returns the FULL list
    and ``plan_refiner_structure`` / ``TaskOutputValidator`` /
    ``agent.py`` are untouched.

Both reply shapes must keep working: a delta, and the old full-table
echo (an old prompt, an older provider, or a test double).
"""

import copy
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_DIR = Path(__file__).resolve().parents[2] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from config_registry import ConfigRegistry  # noqa: E402
from refiner import TaskRefiner  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: the plan shape, at the plan size.
# ---------------------------------------------------------------------------

#: Only ever present in the description of task "42", which is unrelated
#: to the failure. If it shows up in a prompt, an unrelated task's body
#: leaked in -- the one thing this suite exists to prevent.
UNRELATED_SENTINEL = "SENTINEL_UNRELATED_TASK_BODY_DO_NOT_SEND_9f3a2b7c"

N_TASKS = 52
FAILED_TASK_ID = "20"
SENTINEL_TASK_ID = "42"

#: Ceilings measured against the pre-fix 301,547 / 305,022 chars.
MAX_CURRENT_TASKS_CHARS = 20_000
MAX_WHOLE_PROMPT_CHARS = 40_000


def _make_task(task_id: str, **overrides) -> dict:
    body = "".join(
        f"Detail line {j} for task {task_id}. " for j in range(184)
    )
    task = {
        "id": task_id,
        "title": f"Implement module {task_id}",
        "description": f"Task {task_id} description. " + body,
        "test_command": f"pytest tests/test_{task_id}.py -v",
        "test_commands": [f"pytest tests/test_{task_id}.py -v"],
        "status": "pending",
        "updated_time": "2026-10-14 10:00:00",
        "failure_reason": None,
        "project_dir": "/tmp/sample-project",
        "model_type": "medium",
        "depends_on": [],
        "breakdown_count": 0,
        "provider": None,
        "task_group": None,
        "files_to_modify": [f"src/module_{task_id}.py"],
        "verification_only": False,
        "commit_sha": None,
    }
    task.update(overrides)
    return task


def _make_plan(n: int = N_TASKS) -> list:
    """52 tasks. Task 20 failed; task 42 carries the sentinel.

    No task depends on task 20 on purpose: that lets the "untouched
    tasks are byte-identical" assertion in
    :func:`test_delta_reply_preserves_every_unrelated_task` be a plain
    equality check, with the ``depends_on`` rewrite that a split DOES
    legitimately cause covered on its own in
    :func:`test_split_still_rewrites_stale_depends_on`.
    """
    tasks = [_make_task(str(i)) for i in range(1, n + 1)]
    for i in range(1, 20):
        tasks[i - 1]["status"] = "completed"
    tasks[19]["status"] = "failed"
    tasks[19]["failure_reason"] = "AssertionError: expected 3, got 2"
    # The task being worked on carries the big body (9,600 chars), the
    # way that plan's failure did. It is sent in full, so it is in the
    # budget below -- the projection is not allowed to shrink the one
    # task that matters.
    tasks[19]["description"] = "Huge failing task body. " * 400
    # An UNRELATED task carries both the sentinel and a body just as
    # large. Neither may reach the prompt.
    tasks[41]["description"] = (
        f"{UNRELATED_SENTINEL} " + ("unrelated body text. " * 380)
    )
    return tasks


def _build_prompt(tasks: list, last_task_id: str = FAILED_TASK_ID) -> str:
    """The exact string ``refine()`` hands to the LLM."""
    refiner = TaskRefiner(coding_tool=MagicMock())
    return refiner._build_refiner_context(
        requirement="Build a sample analysis platform.",
        tasks=tasks,
        last_coder_response="coder wrote 400 lines, then AssertionError",
        last_result="FAILED: 1 failed, 3 passed",
        exit_code=1,
        last_task_id=last_task_id,
        file_context="",
    )


def _current_tasks_section(prompt: str) -> str:
    start = prompt.index("Current Tasks")
    return prompt[start: prompt.index("\nUse Read/Grep/Glob tools")]


def _whole_prompt(prompt: str) -> str:
    return prompt + ConfigRegistry.get("coding").refiner_system_prompt


def _run_refine(tasks: list, reply, last_task_id: str = FAILED_TASK_ID) -> list:
    """Drive the production entry point with a stubbed LLM reply."""
    refiner = TaskRefiner(coding_tool=MagicMock())
    refiner.coding_tool.query_json.return_value = {"tasks": copy.deepcopy(reply)}
    return refiner.refine(
        requirement="Build a sample analysis platform.",
        tasks=copy.deepcopy(tasks),
        last_coder_response="coder wrote 400 lines, then AssertionError",
        last_result="FAILED: 1 failed, 3 passed",
        exit_code=1,
        last_task_id=last_task_id,
    )


def _child_of(task_id: str, child_id: str, **overrides) -> dict:
    child = _make_task(child_id, **overrides)
    child["description"] = f"Subtask {child_id} of {task_id}: smaller scope."
    child.pop("updated_time", None)
    return child


# ---------------------------------------------------------------------------
# 1. Volume
# ---------------------------------------------------------------------------


def test_current_tasks_section_and_whole_prompt_are_small():
    """The projection is measured, not true by construction.

    Pre-fix, on this fixture, the ``Current Tasks`` section was 301,547
    chars and the whole prompt 305,022.
    """
    tasks = _make_plan()
    prompt = _build_prompt(tasks)
    section = _current_tasks_section(prompt)

    assert len(tasks) == N_TASKS, "fixture must stay at production-plan scale"
    assert max(len(t["description"]) for t in tasks) >= 6_000, (
        "fixture must keep at least one description >= 6,000 chars, "
        "otherwise the projection is not measured against the thing "
        "that was actually too big"
    )

    assert len(section) <= MAX_CURRENT_TASKS_CHARS, (
        f"Current Tasks section is {len(section)} chars; the ceiling is "
        f"{MAX_CURRENT_TASKS_CHARS}. Pre-fix this was 301,547 "
        f"(json.dumps(tasks, indent=2) over the whole plan)."
    )
    assert len(_whole_prompt(prompt)) <= MAX_WHOLE_PROMPT_CHARS, (
        f"whole prompt is {len(_whole_prompt(prompt))} chars; the ceiling "
        f"is {MAX_WHOLE_PROMPT_CHARS}. Pre-fix this was 305,022."
    )


def test_focus_task_is_still_sent_in_full():
    """The projection must not shrink away the task being refined."""
    tasks = _make_plan()
    section = _current_tasks_section(_build_prompt(tasks))
    rows = {r["id"]: r for r in json.loads(section[section.index("["):])}
    failed = next(t for t in tasks if t["id"] == FAILED_TASK_ID)

    assert rows[FAILED_TASK_ID] == failed, (
        "the failed task is the subject of this refinement and must be "
        "sent complete -- its description, test command and "
        "files_to_modify are what the split is decided from"
    )


def test_index_rows_carry_the_four_fields_and_no_description():
    tasks = _make_plan()
    section = _current_tasks_section(_build_prompt(tasks))
    rows = json.loads(section[section.index("["):])

    assert len(rows) == N_TASKS, "every task must still be visible as a row"
    for row in rows:
        if row.get("id") == FAILED_TASK_ID:
            continue
        assert set(row) <= {"id", "title", "status", "depends_on"}, (
            f"index row {row} leaked a field beyond the four-field index"
        )


# ---------------------------------------------------------------------------
# 2. Unrelated task bodies do not reach the prompt
# ---------------------------------------------------------------------------


def test_unrelated_task_description_never_reaches_the_prompt():
    tasks = _make_plan()
    sentinel_task = next(
        t for t in tasks if UNRELATED_SENTINEL in t["description"]
    )
    assert sentinel_task["id"] == SENTINEL_TASK_ID

    prompt = _build_prompt(tasks)
    assert UNRELATED_SENTINEL not in prompt, (
        "an unrelated task's description body reached the refiner prompt. "
        f"It lives on task {SENTINEL_TASK_ID}, which has nothing to do "
        f"with the failure on task {FAILED_TASK_ID}."
    )
    assert SENTINEL_TASK_ID in prompt, (
        "the unrelated task must still appear as an index row -- dropping "
        "it would let the refiner reuse an id that already exists"
    )


# ---------------------------------------------------------------------------
# 3. Delta reply (the load-bearing case)
# ---------------------------------------------------------------------------


def test_delta_reply_preserves_every_unrelated_task():
    """A reply carrying only X-1/X-2 must not cost the plan 50 tasks."""
    tasks = _make_plan()
    before = copy.deepcopy(tasks)
    children = [
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-1", depends_on=[]),
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-2",
                  depends_on=[f"{FAILED_TASK_ID}-1"]),
    ]

    result = _run_refine(tasks, children)

    by_id = {t["id"]: t for t in result}
    assert f"{FAILED_TASK_ID}-1" in by_id and f"{FAILED_TASK_ID}-2" in by_id, (
        f"the children are missing from the effective list: {sorted(by_id)}"
    )
    assert FAILED_TASK_ID not in by_id, (
        "the split parent survived beside its own children -- the plan "
        "would carry a failed task that X-1/X-2 already supersede"
    )

    for original in before:
        if original["id"] == FAILED_TASK_ID:
            continue
        assert original["id"] in by_id, (
            f"unrelated task {original['id']} was dropped by a delta reply"
        )
        assert by_id[original["id"]] == original, (
            f"unrelated task {original['id']} came back different:\n"
            f"  before: {json.dumps(original, ensure_ascii=False)}\n"
            f"  after:  {json.dumps(by_id[original['id']], ensure_ascii=False)}"
        )

    surviving_before = [t["id"] for t in before if t["id"] != FAILED_TASK_ID]
    surviving_after = [t["id"] for t in result if t["id"] in surviving_before]
    assert surviving_after == surviving_before, (
        "the relative order of the surviving tasks changed; the children "
        "belong where the parent stood, not at the end of the list"
    )
    assert [t["id"] for t in result].index(f"{FAILED_TASK_ID}-1") == \
           [t["id"] for t in before].index(FAILED_TASK_ID), (
        "the children must take the parent's position in the list"
    )


def test_delta_reply_edits_a_task_in_place():
    """An id that already exists is replaced, not duplicated."""
    tasks = _make_plan()
    edited = copy.deepcopy(tasks[5])
    edited["title"] = "Implement module 6 (narrowed scope)"

    result = _run_refine(tasks, [edited])

    ids = [t["id"] for t in result]
    assert ids.count("6") == 1, f"task 6 was duplicated: {ids}"
    assert len(result) == len(tasks), (
        f"an in-place edit changed the plan size: {len(tasks)} -> {len(result)}"
    )
    assert next(t for t in result if t["id"] == "6")["title"] == edited["title"]


def test_split_still_rewrites_stale_depends_on():
    """Splitting must keep working: the parent's dependants must follow.

    This is behaviour ``mutate_structure`` already provided; the delta
    merge must not cut it off -- it runs after the merge, on the
    assembled list.
    """
    tasks = _make_plan()
    tasks[20]["depends_on"] = [FAILED_TASK_ID]

    result = _run_refine(
        tasks,
        [
            _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-1", depends_on=[]),
            _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-2",
                      depends_on=[f"{FAILED_TASK_ID}-1"]),
        ],
    )

    dependant = next(t for t in result if t["id"] == "21")
    assert dependant["depends_on"] == ["20-1", "20-2"], (
        f"task 21 still points at the removed parent: {dependant['depends_on']}"
    )


def test_reply_with_an_empty_task_list_does_not_raise():
    """``response["tasks"] == []`` behaved as an empty plan before too.

    Pre-2026-10-14 that value was assigned straight through. Preserved
    on purpose: silently turning a wipe into a no-op would be a
    behaviour change nobody asked for. What must not happen is a raise.
    """
    tasks = _make_plan()
    assert isinstance(_run_refine(tasks, []), list)


def test_reply_without_the_tasks_key_keeps_the_plan():
    """``response.get("tasks", tasks)`` -- the missing-key path is intact."""
    tasks = _make_plan()
    refiner = TaskRefiner(coding_tool=MagicMock())
    refiner.coding_tool.query_json.return_value = {"note": "no tasks here"}

    result = refiner.refine(
        requirement="r",
        tasks=copy.deepcopy(tasks),
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id=FAILED_TASK_ID,
    )
    assert [t["id"] for t in result] == [t["id"] for t in tasks]


def test_delta_never_mutates_the_callers_task_dicts():
    """``mutate_structure`` writes in place; the caller must not feel it.

    ``agent.py`` reads its ``current_tasks_dict`` afterwards as the
    PRE-refinement list for ``plan_refiner_structure``. If the merge
    handed back the caller's own dicts, the depends_on rewrite below
    would rewrite that list and corrupt the reconciliation.
    """
    tasks = _make_plan()
    tasks[20]["depends_on"] = [FAILED_TASK_ID]
    handed_in = copy.deepcopy(tasks)

    # NOTE: ``tasks`` is handed in as-is, NOT through ``_run_refine`` --
    # that helper deep-copies, which would hide exactly the aliasing this
    # test exists to catch.
    refiner = TaskRefiner(coding_tool=MagicMock())
    refiner.coding_tool.query_json.return_value = {"tasks": [
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-1", depends_on=[]),
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-2",
                  depends_on=[f"{FAILED_TASK_ID}-1"]),
    ]}
    refiner.refine(
        requirement="r",
        tasks=tasks,
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id=FAILED_TASK_ID,
    )
    assert tasks == handed_in, (
        "refine() mutated the task list it was given -- the caller uses "
        "that list as the pre-refinement state"
    )


# ---------------------------------------------------------------------------
# 4. Full-table reply (the compatibility case)
# ---------------------------------------------------------------------------


def test_full_table_reply_matches_the_pre_change_result_byte_for_byte():
    """An old-model echo must be indistinguishable from the old code.

    Pre-2026-10-14, ``refine()`` with no ``project_dir`` did exactly:
    ``new_tasks = response.get("tasks", tasks)`` then
    ``_finalize_candidate(tasks, new_tasks, last_task_id)``; with no
    validator there is nothing after that. Reproduced here with the
    same production method rather than a re-implementation.
    """
    tasks = _make_plan()
    # An echo that reorders, re-words one completed task, and splits the
    # failed one -- i.e. a full table that is NOT simply the input, so
    # the comparison has teeth.
    full_table = []
    for task in tasks:
        if task["id"] == FAILED_TASK_ID:
            continue
        noisy = copy.deepcopy(task)
        if task["id"] == "7":
            noisy["description"] = "reworded by the refiner, at length."
        full_table.append(noisy)
    full_table.append(
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-1", depends_on=[])
    )
    full_table.reverse()

    pre_change = TaskRefiner(coding_tool=MagicMock())._finalize_candidate(
        copy.deepcopy(tasks), copy.deepcopy(full_table), FAILED_TASK_ID,
    )
    after_change = _run_refine(tasks, full_table)

    assert json.dumps(after_change, sort_keys=True) == \
           json.dumps(pre_change, sort_keys=True), (
        "a full-table reply no longer produces the pre-change result"
    )
    assert [t["id"] for t in after_change] == [t["id"] for t in full_table], (
        "a full-table reply must be used verbatim, in the model's order"
    )


# ---------------------------------------------------------------------------
# 5. Downstream contract unchanged
# ---------------------------------------------------------------------------


def test_refine_still_returns_a_full_task_list():
    """``refine()``'s return shape is the boundary this change must not move.

    ``agent.py:6956`` feeds it to ``plan_refiner_structure``, and
    ``refiner.py`` feeds it to ``TaskOutputValidator`` as
    ``[SubTask(**t) for t in new_tasks]``. A delta that escaped
    ``refine()`` would break both.
    """
    tasks = _make_plan()
    result = _run_refine(
        tasks,
        [
            _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-1", depends_on=[]),
            _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-2",
                      depends_on=[f"{FAILED_TASK_ID}-1"]),
        ],
    )

    assert isinstance(result, list)
    assert all(isinstance(t, dict) for t in result)
    assert len(result) == len(tasks) + 1, (
        f"52 tasks, one split into two, must come back as 53: {len(result)}"
    )

    from task import SubTask
    snapshot = [SubTask(**t) for t in result]
    assert len(snapshot) == len(result), (
        "every returned task must survive the downstream SubTask(**t) "
        "construction -- a partial dict would raise here"
    )


def test_plan_refiner_structure_still_reconciles_a_delta_refiner_result():
    """The downstream call shape still works on what ``refine()`` returns."""
    from refiner_structure import plan_refiner_structure

    tasks = _make_plan()
    children = [
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-1", depends_on=[]),
        _child_of(FAILED_TASK_ID, f"{FAILED_TASK_ID}-2",
                  depends_on=[f"{FAILED_TASK_ID}-1"]),
    ]
    current = copy.deepcopy(tasks)
    updated = _run_refine(tasks, children)

    plan = plan_refiner_structure(current, updated)

    assert not plan.is_noop, "the split must register as a structural change"
    assert set(plan.added_ids) == {f"{FAILED_TASK_ID}-1",
                                   f"{FAILED_TASK_ID}-2"}, (
        f"added_ids were {plan.added_ids}"
    )
    assert FAILED_TASK_ID in plan.removed_ids, (
        "the split parent must land in removed_ids so agent.py deletes "
        f"its row; got {plan.removed_ids}"
    )


# ---------------------------------------------------------------------------
# 6. The output contract in the shipped system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_states_the_delta_contract():
    """The reply shape is agreed in the system prompt, so pin it there.

    The merge in ``refine()`` accepts both shapes, which means no test
    would notice if this prompt drifted back to "include ALL tasks" --
    the model would simply start echoing 300k chars again and nothing
    downstream would fail. It has to be pinned.
    """
    prompt = ConfigRegistry.get("coding").refiner_system_prompt

    assert "You MUST include ALL tasks" not in prompt, (
        "the refiner system prompt still demands the whole task list be "
        "echoed back -- that is the rule that put 298k chars of "
        "unrelated tasks into a 310k char prompt"
    )
    assert "preserved exactly as it is by the framework" in prompt, (
        "the system prompt must tell the model that unmentioned tasks "
        "are preserved by the framework; without that sentence the model "
        "has no way to know it may answer with a delta"
    )
    assert "OMIT the parent" in prompt, (
        "the system prompt must say the split parent is dropped, or the "
        "refiner will return X alongside X-1/X-2"
    )
