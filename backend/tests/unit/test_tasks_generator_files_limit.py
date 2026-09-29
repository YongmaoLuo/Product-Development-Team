"""
Tests for ``TasksGenerator._enforce_files_to_modify_limit`` and the
end-to-end call site inside ``TasksGenerator.generate()``.

Background
----------
Audit 2026-07-18: an LLM emitted a single task with 30 files in
``files_to_modify``; a test failure on that one task rolled back
all 30 file changes together, and the recovery was painful
because the diff had to be manually partitioned. To guarantee
each task is small enough to fail/retry independently, a
post-processing step splits any task with > 5 files into child
tasks using the existing ``<parent>-N`` id convention.

This file pins the contract:

  1. ``test_split_task_with_6_files``
     A task with 6 files in ``files_to_modify`` is split into 2
     children: first carries files 1-5, second carries file 6.
     The children's ids are ``<parent>-1`` and ``<parent>-2``.

  2. ``test_split_task_with_30_files``
     A task with 30 files is split into 6 children of 5 files
     each. Total file count across children equals the parent's
     file count; no file is duplicated or lost.

  3. ``test_no_split_when_at_or_under_limit``
     A task with exactly 5 files (the limit) is NOT split. A
     task with 3 files is NOT split. These are the common cases.

  4. ``test_inheritance_of_other_fields``
     A child task inherits ``title`` (with ``[part N/M]`` suffix),
     ``description``, ``test_command``, ``depends_on``,
     ``project_dir``, and any other field from the parent.

  5. ``test_children_do_not_depend_on_each_other``
     Two children of the same parent do NOT have each other in
     their ``depends_on`` array — they are siblings, not
     sequential. They DO inherit the parent's original
     ``depends_on`` so upstream deps are preserved.

  6. ``test_hierarchical_parent_id``
     A parent with hierarchical id ``"2-3"`` produces children
     ``"2-3-1"`` and ``"2-3-2"``. A parent with id ``"5"``
     produces ``"5-1"`` and ``"5-2"``. This matches the
     existing id scheme used by the executor's layer builder.

  7. ``test_missing_or_empty_files_to_modify``
     A task with no ``files_to_modify`` field, or with
     ``files_to_modify = []``, passes through unchanged.

  8. ``test_end_to_end_via_generate``
     The end-to-end path: a mock LLM emits a single task with 7
     files; after ``TasksGenerator.generate()`` runs, the
     returned (and on-disk) tasks.json contains 2 children, each
     with ≤ 5 files.

  9. ``test_non_list_files_to_modify_passes_through``
     Defensive: a task whose ``files_to_modify`` is a string
     (malformed LLM output) passes through unchanged. Field
     validation is the responsibility of ``agent._load_tasks``,
     not the post-processor.
"""

import copy
import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture(autouse=True)
def _patch_tasks_self_review(monkeypatch):
    """Stub the mandatory second-pass self-review with a no-op
    so the legacy ``_StubCodingTool`` (which only implements
    ``query_json``) does not break.
    """
    sys.path.insert(0, str(_BACKEND_DIR))
    try:
        import tasks_generator as tg
    finally:
        if str(_BACKEND_DIR) in sys.path:
            sys.path.remove(str(_BACKEND_DIR))

    def _no_self_review(self, tasks_data, falsifiability=None):
        return None

    monkeypatch.setattr(tg.TasksGenerator, "_run_tasks_self_review", _no_self_review)
    return monkeypatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubCodingTool:
    """Minimal coding tool stub that returns a caller-supplied payload.

    Mirrors the stub used by ``test_tasks_generator.py`` and
    ``test_tasks_generator_depends_on.py`` so the tests in this file
    are independent of any real LLM call. The test never inspects the
    prompt; it only checks that the post-processing pipeline in
    ``TasksGenerator.generate`` splits over-sized tasks.
    """

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return self._response


@pytest.fixture
def plan_dir(tmp_path):
    """A minimal plan_dir: ``prd.md`` exists so ``_load_prd()`` does not fail."""
    pd = tmp_path / "plan"
    pd.mkdir(parents=True)
    (pd / "prd.md").write_text(
        "# PRD\nMinimal PRD for unit test.\n", encoding="utf-8"
    )
    return pd


def _make_generator(response: dict, plan_dir):
    from tasks_generator import TasksGenerator

    tool = _StubCodingTool(response=response)
    return TasksGenerator(coding_tool=tool, plan_dir=plan_dir)


# ---------------------------------------------------------------------------
# Unit tests for the helper itself
# ---------------------------------------------------------------------------


def test_split_task_with_6_files(plan_dir):
    """A task with 6 files is split into 2 children (5 + 1)."""
    gen = _make_generator({"tasks": []}, plan_dir)
    task = {
        "id": "5",
        "title": "Stage 2: migrate 6 files",
        "description": "stage 2 work",
        "test_command": "pytest tests/ -v",
        "depends_on": ["4"],
        "files_to_modify": [f"src/f{i}.py" for i in range(6)],
    }

    result = gen._enforce_files_to_modify_limit([task], max_per_task=5)

    assert len(result) == 2, f"expected 2 children, got {len(result)}"
    assert result[0]["id"] == "5-1", f"first child id wrong: {result[0]['id']!r}"
    assert result[1]["id"] == "5-2", f"second child id wrong: {result[1]['id']!r}"
    assert len(result[0]["files_to_modify"]) == 5
    assert len(result[1]["files_to_modify"]) == 1
    # File integrity: parent had 6 files, children together carry 6 files,
    # no duplication, no loss.
    all_files = result[0]["files_to_modify"] + result[1]["files_to_modify"]
    assert all_files == [f"src/f{i}.py" for i in range(6)]


def test_split_task_with_30_files(plan_dir):
    """A task with 30 files is split into 6 children of 5 files each."""
    gen = _make_generator({"tasks": []}, plan_dir)
    task = {
        "id": "1",
        "title": "Stage 1: huge migration",
        "files_to_modify": [f"src/f{i}.py" for i in range(30)],
    }

    result = gen._enforce_files_to_modify_limit([task], max_per_task=5)

    assert len(result) == 6, f"expected 6 children, got {len(result)}"
    for i, child in enumerate(result, start=1):
        assert child["id"] == f"1-{i}", f"child {i} id wrong: {child['id']!r}"
        assert len(child["files_to_modify"]) == 5
    # File integrity: parent's 30 files = sum of children's files.
    all_files = [f for c in result for f in c["files_to_modify"]]
    assert len(all_files) == 30
    assert all_files == [f"src/f{i}.py" for i in range(30)]


def test_no_split_when_at_or_under_limit(plan_dir):
    """Tasks with ≤ 5 files pass through unchanged."""
    gen = _make_generator({"tasks": []}, plan_dir)
    task_at_limit = {
        "id": "1",
        "title": "exactly 5",
        "files_to_modify": [f"src/f{i}.py" for i in range(5)],
    }
    task_under_limit = {
        "id": "2",
        "title": "only 3",
        "files_to_modify": [f"src/f{i}.py" for i in range(3)],
    }
    task_one = {
        "id": "3",
        "title": "just one",
        "files_to_modify": ["src/foo.py"],
    }

    result = gen._enforce_files_to_modify_limit(
        [task_at_limit, task_under_limit, task_one], max_per_task=5
    )

    assert len(result) == 3, "no splitting expected"
    assert result[0] is task_at_limit, "at-limit task should pass through by identity"
    assert result[1] is task_under_limit
    assert result[2] is task_one


def test_inheritance_of_other_fields(plan_dir):
    """Children inherit all parent fields except id, title, files_to_modify."""
    gen = _make_generator({"tasks": []}, plan_dir)
    task = {
        "id": "7",
        "title": "Stage 3: API + tests",
        "description": "Implement the API and add tests for it.",
        "test_command": "pytest tests/api -v",
        "depends_on": ["6"],
        "project_dir": "/some/project",
        "model_type": "sonnet",
        "files_to_modify": [f"src/api/f{i}.py" for i in range(7)],
    }

    result = gen._enforce_files_to_modify_limit([task], max_per_task=5)
    assert len(result) == 2

    for i, child in enumerate(result, start=1):
        # Inherited fields
        assert child["description"] == task["description"]
        assert child["test_command"] == task["test_command"]
        assert child["depends_on"] == ["6"]
        assert child["project_dir"] == "/some/project"
        assert child["model_type"] == "sonnet"
        # Overridden fields
        assert child["id"] == f"7-{i}"
        assert child["title"] == f"Stage 3: API + tests [part {i}/2]"
        assert len(child["files_to_modify"]) == 5 if i == 1 else 2
        # Independence: child's files_to_modify mutation must not bleed.
        child["files_to_modify"].append("INJECTED")
        assert task["files_to_modify"][-1] != "INJECTED"


def test_children_do_not_depend_on_each_other(plan_dir):
    """Children of the same parent are siblings — no cross-deps.

    Each child inherits the parent's ``depends_on`` (upstream deps)
    but does NOT depend on its siblings. This is what allows the
    executor to run them in parallel.
    """
    gen = _make_generator({"tasks": []}, plan_dir)
    task = {
        "id": "4",
        "title": "split me",
        "depends_on": ["1", "2-3"],
        "files_to_modify": [f"f{i}.py" for i in range(12)],
    }

    result = gen._enforce_files_to_modify_limit([task], max_per_task=5)
    assert len(result) == 3  # 12 files / 5 = 3 children (5+5+2)

    # Every child must keep the parent's upstream deps.
    for child in result:
        assert child["depends_on"] == ["1", "2-3"]
    # No child depends on its siblings.
    sibling_ids = {c["id"] for c in result}
    for child in result:
        for dep in child["depends_on"]:
            assert dep not in sibling_ids, (
                f"child {child['id']} incorrectly depends on its sibling {dep}"
            )


def test_hierarchical_parent_id(plan_dir):
    """Parent id ``"2-3"`` → children ``"2-3-1"``, ``"2-3-2"``."""
    gen = _make_generator({"tasks": []}, plan_dir)
    parent_hierarchical = {
        "id": "2-3",
        "title": "deep child",
        "files_to_modify": [f"f{i}.py" for i in range(7)],
    }
    parent_flat = {
        "id": "5",
        "title": "flat",
        "files_to_modify": [f"g{i}.py" for i in range(6)],
    }

    result = gen._enforce_files_to_modify_limit(
        [parent_hierarchical, parent_flat], max_per_task=5
    )

    assert [c["id"] for c in result[:2]] == ["2-3-1", "2-3-2"]
    assert [c["id"] for c in result[2:]] == ["5-1", "5-2"]


def test_missing_or_empty_files_to_modify(plan_dir):
    """Missing/empty ``files_to_modify`` passes through."""
    gen = _make_generator({"tasks": []}, plan_dir)
    task_no_field = {"id": "1", "title": "no field"}
    task_empty = {"id": "2", "title": "empty", "files_to_modify": []}

    result = gen._enforce_files_to_modify_limit(
        [task_no_field, task_empty], max_per_task=5
    )
    assert result[0] is task_no_field
    assert result[1] is task_empty
    assert len(result) == 2


def test_non_list_files_to_modify_passes_through(plan_dir):
    """Defensive: a non-list ``files_to_modify`` (malformed LLM output) passes through.

    Field validation is the responsibility of ``agent._load_tasks``;
    the post-processor here is intentionally permissive because
    throwing here would mask the LLM's bug behind a less informative
    error.
    """
    gen = _make_generator({"tasks": []}, plan_dir)
    task = {"id": "1", "title": "weird", "files_to_modify": "src/foo.py"}  # string, not list
    result = gen._enforce_files_to_modify_limit([task], max_per_task=5)
    assert len(result) == 1
    assert result[0] is task


def test_empty_input_returns_empty(plan_dir):
    """Empty / None input returns empty / passes through."""
    gen = _make_generator({"tasks": []}, plan_dir)
    assert gen._enforce_files_to_modify_limit([], max_per_task=5) == []
    # None is coerced to [] (the contract: never crash on bad input).
    assert gen._enforce_files_to_modify_limit(None, max_per_task=5) == []


def test_max_per_task_zero_or_negative_treated_as_one(plan_dir):
    """Defensive: ``max_per_task <= 0`` is treated as 1 (worst case split)."""
    gen = _make_generator({"tasks": []}, plan_dir)
    task = {
        "id": "1",
        "title": "x",
        "files_to_modify": [f"f{i}.py" for i in range(3)],
    }
    # With max=1, 3 files become 3 children of 1 file each.
    result = gen._enforce_files_to_modify_limit([task], max_per_task=0)
    assert len(result) == 3
    for i, child in enumerate(result, start=1):
        assert child["id"] == f"1-{i}"
        assert len(child["files_to_modify"]) == 1


def test_position_preserved(plan_dir):
    """Children replace the parent at the same list position."""
    gen = _make_generator({"tasks": []}, plan_dir)
    t_small = {"id": "1", "title": "small", "files_to_modify": ["a.py"]}
    t_big = {"id": "2", "title": "big", "files_to_modify": [f"b{i}.py" for i in range(7)]}
    t_after = {"id": "3", "title": "after", "files_to_modify": ["c.py"]}

    result = gen._enforce_files_to_modify_limit([t_small, t_big, t_after], max_per_task=5)

    # t_small is at position 0; t_big's children are at positions 1,2;
    # t_after is at position 3.
    assert result[0] is t_small
    assert result[1]["id"] == "2-1"
    assert result[2]["id"] == "2-2"
    assert result[3] is t_after
    assert len(result) == 4


# ---------------------------------------------------------------------------
# End-to-end: through TasksGenerator.generate()
# ---------------------------------------------------------------------------


def test_end_to_end_via_generate(plan_dir):
    """Mock LLM emits a 7-file task; generate() splits it into 2 children.

    This pins the wiring: the post-processing call site in
    ``TasksGenerator.generate`` runs the helper, and the resulting
    ``tasks.json`` on disk (the canonical source of truth) reflects
    the split.
    """
    response = {
        "requirement": "files_to_modify limit end-to-end",
        "tasks": [
            {
                "id": "1",
                "title": "Stage 1: do 7 things at once",
                "description": "one big task",
                "test_command": "pytest tests/ -v",
                "depends_on": [],
                "files_to_modify": [f"src/f{i}.py" for i in range(7)],
            }
        ],
    }
    gen = _make_generator(response, plan_dir)

    result = gen.generate()

    # In-memory result is split.
    assert len(result["tasks"]) == 2, (
        f"expected 2 children after split, got {len(result['tasks'])}"
    )
    assert result["tasks"][0]["id"] == "1-1"
    assert result["tasks"][1]["id"] == "1-2"
    assert len(result["tasks"][0]["files_to_modify"]) == 5
    assert len(result["tasks"][1]["files_to_modify"]) == 2

    # On-disk tasks.json must match the in-memory result so a
    # cross-process recovery read-back sees the same shape.
    on_disk = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    assert len(on_disk["tasks"]) == 2
    assert on_disk["tasks"][0]["id"] == "1-1"
    assert on_disk["tasks"][1]["id"] == "1-2"
    # File integrity: parent had 7 files, children together carry 7 files.
    all_files = (
        on_disk["tasks"][0]["files_to_modify"]
        + on_disk["tasks"][1]["files_to_modify"]
    )
    assert all_files == [f"src/f{i}.py" for i in range(7)]


def test_end_to_end_mixed_size_tasks(plan_dir):
    """Mixed: one small task (no split) and one big task (split)."""
    response = {
        "requirement": "mixed",
        "tasks": [
            {
                "id": "1",
                "title": "small",
                "depends_on": [],
                "files_to_modify": ["src/a.py"],
            },
            {
                "id": "2",
                "title": "big",
                "depends_on": ["1"],
                "files_to_modify": [f"src/b{i}.py" for i in range(11)],
            },
        ],
    }
    gen = _make_generator(response, plan_dir)

    result = gen.generate()

    # Task 1 is small → 1 entry. Task 2 is split → 3 children (5+5+1).
    assert len(result["tasks"]) == 4
    assert result["tasks"][0]["id"] == "1"
    assert result["tasks"][1]["id"] == "2-1"
    assert result["tasks"][2]["id"] == "2-2"
    assert result["tasks"][3]["id"] == "2-3"
    # Each child carries ≤ 5 files.
    for child in result["tasks"][1:]:
        assert len(child["files_to_modify"]) <= 5
    # depends_on inheritance: every child of task 2 depends on "1".
    for child in result["tasks"][1:]:
        assert child["depends_on"] == ["1"]
