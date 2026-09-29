"""
Tests for the TaskRefiner structural-change contract.

Rule (enforced by subtask): every structural mutation produced by the
refiner -- starting with the ``depends_on`` rewrite after a parent task
is split into children -- must flow through ``TaskRefiner.mutate_structure``.
The refiner is forbidden from writing ``tasks.json`` directly; persistence
is the caller's responsibility (``task_manager.set_tasks`` in the agent).
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import refiner as refiner_mod  # noqa: E402
from refiner import TaskRefiner  # noqa: E402


def _make_child_tasks(prefix: str, n: int) -> list:
    """Build N child tasks with hierarchical IDs like ``"2-1"``."""
    return [{"id": f"{prefix}-{i}", "title": f"child {i}"} for i in range(1, n + 1)]


def test_mutate_structure_rewrites_split_parent_deps():
    """``mutate_structure`` is the canonical place for dependency rewrite."""
    refiner = TaskRefiner(coding_tool=MagicMock())
    tasks = _make_child_tasks("2", 2) + [{"id": "3", "depends_on": ["2"]}]

    result = refiner.mutate_structure(tasks)

    t3 = next(t for t in result if t["id"] == "3")
    assert t3["depends_on"] == ["2-1", "2-2"]


def test_refine_routes_structural_changes_through_mutate_structure():
    """
    When the LLM returns a task list that needs structural rewrite,
    ``refine`` must delegate that rewrite to ``mutate_structure`` rather
    than mutating the list directly.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    returned_tasks = _make_child_tasks("2", 2) + [{"id": "3", "depends_on": ["2"]}]
    refiner.coding_tool.query_json.return_value = {"tasks": returned_tasks}

    original_mutate_structure = refiner.mutate_structure
    calls = []

    def spy_mutate_structure(tasks, task_id=None):
        calls.append([dict(t) for t in tasks])
        return original_mutate_structure(tasks, task_id=task_id)

    refiner.mutate_structure = spy_mutate_structure

    result = refiner.refine(
        requirement="test requirement",
        tasks=[
            {"id": "2", "title": "parent", "status": "pending"},
            {"id": "3", "title": "downstream", "depends_on": ["2"], "status": "pending"},
        ],
        last_coder_response="response",
        last_result="result",
        exit_code=1,
        last_task_id="2",
    )

    assert len(calls) == 1, "mutate_structure should be called exactly once"
    t3 = next(t for t in result if t["id"] == "3")
    assert t3["depends_on"] == ["2-1", "2-2"], f"expected rewritten deps, got {t3['depends_on']}"


# ---------------------------------------------------------------------------
# Regression suite for refiner split + description consistency
#
# Background
# ----------
# The refiner split
# ``task 1`` into ``1-1``/``1-2``/``1-3``/``1-4`` and correctly
# rewrote task 2-1's ``depends_on`` to the four children. However
# task 2-1's ``description`` still said "前置条件：1 已完成" —
# pointing at the now-removed parent id. ``_load_tasks`` ran
# ``validate_desc_consistency`` and rejected the plan with::
#
#     Task 2-1 desc 提到前置条件 1 但 depends_on 是
#     ['1-1', '1-2', '1-3', '1-4']
#     (transitive 闭包 = ['1-1', '1-2', '1-3', '1-4'])
#
# The framework's reload block was failing forever on this mismatch
# until commit 6db42f8 added a cap. The cap treats the symptom;
# this suite fixes the cause.
# ---------------------------------------------------------------------------


def test_mutate_structure_rewrites_split_parent_in_description():
    """``mutate_structure`` must rewrite stale parent-id references
    in the description field of downstream tasks, not just in
    ``depends_on``.

    Pre-fix bug: ``_rewrite_split_depends_on`` only touched
    ``depends_on``. If a task's description said
    "前置条件：1 已完成" while depends_on was rewritten to the
    children, ``validate_desc_consistency`` rejected the plan.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    tasks = (
        _make_child_tasks("1", 4)
        + [
            {
                "id": "2-1",
                "title": "downstream of split parent",
                # Stale description pointing at removed parent id.
                "description": "## 背景\n前置条件：1 已完成。",
                "depends_on": ["1-1", "1-2", "1-3", "1-4"],
            }
        ]
    )

    result = refiner.mutate_structure(tasks)
    t21 = next(t for t in result if t["id"] == "2-1")
    desc = t21.get("description", "")
    assert "前置条件：1 " not in desc, (
        f"description still mentions the removed parent id '1'. "
        f"This is the desc/depends_on disagreement that triggered "
        f"the same_id_loop deadlock. "
        f"Got: {desc!r}"
    )
    # Sanity: the rewritten description should reference at least
    # one of the children, not the now-removed parent.
    assert any(
        child_id in desc for child_id in ("1-1", "1-2", "1-3", "1-4")
    ), (
        f"description must reference at least one of the parent's "
        f"children after refactor. Got: {desc!r}"
    )


def test_mutate_structure_rewrites_split_parent_in_description_minimal_change():
    """A minimal case: a single ``id`` mention in description.

    The original description had exactly one parent-id token;
    after rewrite that token should be replaced with the children.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    tasks = (
        _make_child_tasks("1", 2)
        + [
            {
                "id": "2-1",
                "description": "前置条件：1",
                "depends_on": ["1-1", "1-2"],
            }
        ]
    )
    result = refiner.mutate_structure(tasks)
    t21 = next(t for t in result if t["id"] == "2-1")
    desc = t21["description"]
    # Verify the description now references both children.
    assert "1-1" in desc and "1-2" in desc, (
        f"description must reference the children after refactor. "
        f"Got: {desc!r}"
    )
    # Verify the original ``:1`` token no longer appears as a
    # standalone token (i.e. the description doesn't say "前置条件：1 "
    # followed by space — only "前置条件：1-1, 1-2").
    # Use a word-boundary-aware check: split on punctuation/whitespace
    # and assert no token equals "1".
    import re as _re
    tokens = _re.findall(r"\S+", desc.split("前置条件：", 1)[-1])
    assert "1" not in tokens, (
        f"the standalone parent-id token '1' must be removed from "
        f"the description. Tokens after '前置条件：': {tokens}"
    )


def test_refine_routes_description_rewrite_through_mutate_structure():
    """End-to-end: ``refine`` must route description rewrites through
    ``mutate_structure`` (just like depends_on rewrites).

    Ensures future regressions cannot accidentally split the
    rewrite logic into two code paths.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    returned_tasks = (
        _make_child_tasks("1", 2)
        + [
            {
                "id": "2-1",
                "title": "downstream",
                "description": "前置条件：1 已完成。",
                "depends_on": ["1-1", "1-2"],
            }
        ]
    )
    refiner.coding_tool.query_json.return_value = {"tasks": returned_tasks}

    original_mutate_structure = refiner.mutate_structure
    calls = []

    def spy_mutate_structure(tasks, task_id=None):
        # Call the production rewrite first so the in-place
        # mutation has happened, THEN snapshot for inspection.
        result = original_mutate_structure(tasks, task_id=task_id)
        calls.append([dict(t) for t in tasks])
        return result

    refiner.mutate_structure = spy_mutate_structure

    refiner.refine(
        requirement="test",
        tasks=[
            {"id": "1", "title": "parent", "status": "pending"},
            {
                "id": "2-1",
                "title": "downstream",
                "depends_on": ["1"],
                "status": "pending",
            },
        ],
        last_coder_response="r",
        last_result="x",
        exit_code=1,
        last_task_id="1",
    )

    assert len(calls) == 1
    # The mutate_structure call must have rewritten the description.
    t21_in_call = next(
        t for t in calls[0] if t["id"] == "2-1"
    )
    desc = t21_in_call.get("description", "")
    assert "1-1" in desc and "1-2" in desc, (
        f"mutate_structure must have rewritten the description to "
        f"reference the children. Got: {desc!r}"
    )
    # No standalone ``1`` token remains.
    import re as _re
    after_prefix = desc.split("前置条件：", 1)[-1] if "前置条件：" in desc else desc
    tokens = _re.findall(r"\S+", after_prefix)
    assert "1" not in tokens, (
        f"standalone parent-id token '1' must be removed. "
        f"Tokens after '前置条件：': {tokens}"
    )


def test_mutate_structure_leaves_unrelated_description_alone():
    """A description that does NOT mention any split-parent id
    must be left untouched. Verifies the rewrite doesn't
    accidentally clobber unrelated text.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    tasks = (
        _make_child_tasks("1", 2)
        + [
            {
                "id": "2-1",
                "description": "no parent ref here",
                "depends_on": ["1-1", "1-2"],
            }
        ]
    )
    result = refiner.mutate_structure(tasks)
    t21 = next(t for t in result if t["id"] == "2-1")
    assert t21["description"] == "no parent ref here"


def test_refine_does_not_call_json_dump():
    """The refiner must never call ``json.dump`` -- persistence is not its job."""
    refiner = TaskRefiner(coding_tool=MagicMock())
    returned_tasks = _make_child_tasks("2", 2) + [{"id": "3", "depends_on": ["2"]}]
    refiner.coding_tool.query_json.return_value = {"tasks": returned_tasks}

    original_dump = refiner_mod.json.dump

    def fake_dump(*args, **kwargs):
        raise AssertionError("refiner must not call json.dump directly")

    refiner_mod.json.dump = fake_dump
    try:
        refiner.refine(
            requirement="test requirement",
            tasks=[
                {"id": "2", "title": "parent", "status": "pending"},
                {"id": "3", "title": "downstream", "depends_on": ["2"], "status": "pending"},
            ],
            last_coder_response="response",
            last_result="result",
            exit_code=1,
            last_task_id="2",
        )
    finally:
        refiner_mod.json.dump = original_dump


# ---------------------------------------------------------------------------
# Regression suite for transitive descendant rewrite
#
# Background (2026-09-04 plan)
# -----------------------------------------------------------------
# The split chain ``4 -> 4-2 -> 4-2-1/4-2-2/4-2-3`` produced a
# tasks.json where task ``4-2-2`` declared ``depends_on: ["4-2-1",
# "4"]``. The intermediate parent ``4-2`` itself was removed during
# the split, so the rewritten ``children_by_parent`` map only linked
# ``"4-2" -> ["4-2-1", "4-2-2", "4-2-3"]`` and was empty for ``"4"``.
# The pre-fix implementation left the stale dep on ``"4"`` dangling;
# the dispatcher's ``_validate_dependencies`` (agent.py:1190) raised
# ``ValueError: Task 4-2-2 depends on missing task 4`` and froze the
# executor. This suite fixes the cause by introducing a transitive
# ``descendants_by_id`` map and falling back to it when
# ``children_by_parent`` does not contain the stale dep.
# ---------------------------------------------------------------------------


def test_mutate_structure_rewrites_transitive_grandparent_dep():
    """``mutate_structure`` must rewrite a stale grandparent dep
    (``4-2-2.depends_on = ["4"]``) to the full transitive descendant
    set when the intermediate parent (``4-2``) was itself removed.

    Pre-fix bug: ``_rewrite_split_depends_on`` only knew about direct
    children, so a grand-parent reference like ``"4"`` with no
    direct children in the new list would surface as a dangling dep
    and crash the dispatcher's strict ``_validate_dependencies``
    check.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    # Simulate a two-level split: parent 4 -> 4-2 -> 4-2-1/4-2-2/4-2-3.
    # Note: ``4-2`` itself is NOT in the task list (it was split further).
    grand_children = (
        {"id": "4-2-1", "title": "leaf 1"},
        {"id": "4-2-2", "title": "leaf 2", "depends_on": ["4-2-1", "4"]},
        {"id": "4-2-3", "title": "leaf 3"},
    )
    tasks = list(grand_children)

    result = refiner.mutate_structure(tasks)
    t = next(t for t in result if t["id"] == "4-2-2")
    # ``4`` must resolve to the transitive descendants MINUS the
    # task's own id (the validator rejects self-dependencies).
    assert "4-2-1" in t["depends_on"]
    assert "4-2-3" in t["depends_on"]
    assert "4-2-2" not in t["depends_on"], (
        f"task must not depend on itself — the validator rejects "
        f"self-loops with 'Task X cannot depend on itself'. "
        f"Got: {t['depends_on']}"
    )
    assert "4" not in t["depends_on"], (
        f"stale grandparent dep '4' must be rewritten to the full "
        f"descendant set. Got: {t['depends_on']}"
    )
    # No duplicate entries (rewriter dedupes after rewrite).
    assert len(t["depends_on"]) == len(set(t["depends_on"])), (
        f"rewriter must dedupe after transitive rewrite. "
        f"Got: {t['depends_on']}"
    )


def test_mutate_structure_transitive_rewrite_prefers_direct_children():
    """When a stale dep matches BOTH ``children_by_parent`` AND
    ``descendants_by_id``, the rewriter must prefer the direct
    children (smaller, more semantically faithful replacement).

    Example: tasks ``[2-1, 2-2, 2-1-1, 2-1-2]`` (parent ``2`` was
    split one level into ``2-1``/``2-2``, and ``2-1`` was further
    split into ``2-1-1``/``2-1-2``). A task depending on ``"2"``
    should be rewritten to ``["2-1", "2-2"]``, NOT to
    ``["2-1-1", "2-1-2", "2-2"]`` — preserving the original
    partial-ordering semantic (downstream was waiting for the
    parent to complete, not every leaf).
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    tasks = (
        _make_child_tasks("2", 2)  # 2-1, 2-2
        + _make_child_tasks("2-1", 2)  # 2-1-1, 2-1-2 (further split)
        + [{"id": "3", "depends_on": ["2"]}]
    )

    result = refiner.mutate_structure(tasks)
    t3 = next(t for t in result if t["id"] == "3")
    # Direct children path wins — rewrite is ``["2-1", "2-2"]``.
    assert t3["depends_on"] == ["2-1", "2-2"], (
        f"direct children match must win over transitive descendants. "
        f"Got: {t3['depends_on']}"
    )


def test_mutate_structure_leaves_unknown_dep_alone():
    """A dep that does NOT match ``children_by_parent`` or
    ``descendants_by_id`` (e.g. a typo, or a parent that was
    removed without producing children) must be left untouched
    so the validator can surface it as a real error.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    tasks = (
        _make_child_tasks("2", 2)
        + [{"id": "3", "depends_on": ["9"]}]  # 9 was never in the plan
    )

    result = refiner.mutate_structure(tasks)
    t3 = next(t for t in result if t["id"] == "3")
    assert t3["depends_on"] == ["9"], (
        f"unknown dep must be left untouched so the validator can "
        f"surface it. Got: {t3['depends_on']}"
    )


def test_mutate_structure_strips_self_ref_from_transitive_rewrite():
    """When the transitive rewrite would otherwise produce a
    self-dependency (the task itself is among the descendants of
    the stale grandparent), the rewriter must filter out the
    self-reference.

    Regression for the 2026-09-04:
    task 4-2-2 with depends_on=[``"4-2-1"``, ``"4"``] was rewritten
    by the first cut of the transitive fix to
    ``["4-2-1", "4-2-2", "4-2-3"]`` — but the validator then
    rejected the plan with "Task 4-2-2 cannot depend on itself".
    The rewriter must drop self-refs in the replacement set.
    """
    refiner = TaskRefiner(coding_tool=MagicMock())
    # Same scenario as the grandparent test, but here we make the
    # assertion explicit about self-ref stripping.
    grand_children = (
        {"id": "4-2-1", "title": "leaf 1"},
        {"id": "4-2-2", "title": "leaf 2", "depends_on": ["4"]},
        {"id": "4-2-3", "title": "leaf 3"},
    )
    tasks = list(grand_children)
    result = refiner.mutate_structure(tasks)
    t = next(t for t in result if t["id"] == "4-2-2")
    # Self-ref must be stripped from the rewrite output.
    assert "4-2-2" not in t["depends_on"], (
        f"self-ref '4-2-2' must be stripped after transitive "
        f"rewrite (validator rejects self-loops). "
        f"Got: {t['depends_on']}"
    )
    # The remaining descendants must still be present.
    assert t["depends_on"] == ["4-2-1", "4-2-3"], (
        f"sibling leaves must still be in the rewritten deps. "
        f"Got: {t['depends_on']}"
    )
