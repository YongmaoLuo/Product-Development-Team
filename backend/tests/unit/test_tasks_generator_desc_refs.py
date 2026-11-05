"""
Tests for the generator's description-reference parser.

Background
----------
The **validator** (``backend/agent.py``) parses a task's description
into the prerequisite ids it names — and a *range* ("1-4", "1–4",
"1~4") is expanded into the ids it spans.  That was PRD defect 2: a
bare range used to be read as one literal id ("1-4"), an id that
exists in no plan, and the whole plan was rejected at load time.

The **generator** (``backend/tasks_generator.py``) is the other half of
that contract: it is the component that *injects* ``depends_on`` from a
task's description when the LLM dropped the field.  If the two parsers
disagree, the generator creates the very dependency the validator was
built to tolerate — and, worse, a bare range written as ``1-4`` becomes
a literal id that exists in no plan.

So the generator mirrors the validator verbatim:

  * ``{1-4}`` with the plan's ids known → ``['1', '2', '3', '4']``
  * a bare ``1-4`` with the plan's ids known → ``['1', '2', '3', '4']``
    (same expansion, no braces needed)
  * a bare ``1`` → ``['1']``
  * ``{1-2}`` when the plan knows the hierarchical id ``1-2`` →
    ``['1-2']`` (kept whole, not expanded)
  * ``{1-2}`` with no id set at all → ``['1-2']`` (nothing to tell a
    hierarchy from a range, so every token stays literal)

TDD spec — the contract tests
------------------------------
1. ``test_generator_expands_braced_range``:
   ``{1-4}`` → ``['1', '2', '3', '4']``.
2. ``test_generator_expands_bare_range``:
   bare ``1-4`` → ``['1', '2', '3', '4']``.
3. ``test_generator_and_validator_agree``:
   the generator and the validator return the same ids for the same
   description.
4. ``test_generated_task5_deps``:
   after the post-processing, task 5's ``depends_on`` is ``1..4``.
5. ``test_missing_known_ids_keeps_hierarchy``:
   no id set, ``{1-2}`` → ``['1-2']``.
"""

import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so ``import tasks_generator`` works
# regardless of which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import _extract_desc_dep_ids  # noqa: E402
from tasks_generator import TasksGenerator  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_tasks_self_review(monkeypatch):
    """Stub the mandatory second-pass self-review with a no-op.

    The stub coding tool below only implements ``query_json``; the
    self-review pass would call the LLM a second time.
    """
    import tasks_generator as tg

    def _no_self_review(self, tasks_data, falsifiability=None, **_ignored):
        # ``**_ignored`` absorbs the reusable-session handle
        # ``generate()`` binds; this stub only suppresses the call.
        return None

    monkeypatch.setattr(tg.TasksGenerator, "_run_tasks_self_review", _no_self_review)
    return monkeypatch


@pytest.fixture
def plan_dir(tmp_path):
    """A minimal plan_dir: ``prd.md`` exists so ``_load_prd()`` does not fail."""
    pd = tmp_path / "plan"
    pd.mkdir(parents=True)
    (pd / "prd.md").write_text(
        "# PRD\nMinimal PRD for unit test.\n", encoding="utf-8"
    )
    return pd


class _StubCodingTool:
    """Minimal coding tool stub that returns a caller-supplied payload."""

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return self._response


#: The id set of a five-task plan — the ``known_ids`` handed to the
#: parser so it can tell a hierarchical id from a range.
KNOWN_IDS = {"1", "2", "3", "4", "5"}


# ---------------------------------------------------------------------------
# Test 1: a braced range expands into its members
# ---------------------------------------------------------------------------


def test_generator_expands_braced_range():
    """``{1-4}`` → ``['1', '2', '3', '4']``.

    The brace form is an explicit declaration, so the range names the
    four tasks it spans instead of one literal id ("1-4") that exists
    in no plan.
    """
    desc = "前置条件：任务 {1-4} 已完成。"

    result = TasksGenerator._postprocess_extract_from_desc(
        desc, known_ids=KNOWN_IDS
    )

    assert result == ["1", "2", "3", "4"], (
        f"expected the braced range to expand into 1..4, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: a bare range is expanded, not ignored
# ---------------------------------------------------------------------------


def test_generator_expands_bare_range():
    """A bare ``1-4`` yields ``['1', '2', '3', '4']``.

    This is the exact shape that broke plan ``20261104-CPU``: the desc
    said "前置条件：任务 1-4 已完成" and the generator read ``1-4`` as a
    literal task id that exists in no plan.  The range is expanded
    into the four tasks it spans.
    """
    desc = "前置条件：任务 1-4 已完成。"

    result = TasksGenerator._postprocess_extract_from_desc(
        desc, known_ids=KNOWN_IDS
    )

    assert result == ["1", "2", "3", "4"], (
        f"a bare range must be expanded into its members, got {result!r}"
    )


def test_generator_reads_bare_single_id():
    """A bare id names a prerequisite — the generator injects it."""
    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 1 已完成。", known_ids=KNOWN_IDS
    )

    assert result == ["1"], (
        f"a bare id must be injected, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: the two parsers agree
# ---------------------------------------------------------------------------


def test_generator_and_validator_agree():
    """Generator and validator must return the same ids.

    The generator injects ``depends_on`` from the desc; the validator
    enforces that the desc and the data agree.  A divergence between
    the two parsers lets an inconsistency through the load-time gate,
    so the contract is that they are the same rule.
    """
    samples = [
        "前置条件：任务 {1-4} 已完成。",
        "前置条件：任务 1-4 已完成。",
        "前置条件：任务 1 已完成。",
        "依赖：任务 {2,5} 已完成",
        "前置条件/依赖：已完成 任务 {1-2}。",
        "前置条件：任务 {1-2} 已完成。",
        "不新增依赖、不改 ci.yml（归任务 10）",
        "没有触发词的普通描述，提到了任务 3",
    ]

    for known_ids in (KNOWN_IDS, None):
        for text in samples:
            generator_result = TasksGenerator._postprocess_extract_from_desc(
                text, known_ids=known_ids
            )
            validator_result = _extract_desc_dep_ids(text, known_ids=known_ids)
            assert generator_result == validator_result, (
                f"the two desc parsers disagree on {text!r} "
                f"(known_ids={known_ids!r}): "
                f"generator={generator_result!r} validator={validator_result!r}"
            )


# ---------------------------------------------------------------------------
# Test 4: the post-processed task carries the expanded deps
# ---------------------------------------------------------------------------


def test_generated_task5_deps():
    """After the post-processing, task 5 depends on 1, 2, 3 and 4.

    The LLM dropped ``depends_on`` on task 5 and wrote its
    prerequisite as a braced range; the generator's job is to turn
    that into the four concrete edges.
    """
    tasks = [
        {"id": "1", "description": "第一个任务", "depends_on": []},
        {"id": "2", "description": "第二个任务", "depends_on": []},
        {"id": "3", "description": "第三个任务", "depends_on": []},
        {"id": "4", "description": "第四个任务", "depends_on": []},
        {
            "id": "5",
            "description": "第五个任务。前置条件：任务 {1-4} 已完成。",
            "depends_on": [],
        },
    ]

    TasksGenerator._postprocess_depends_on(tasks)

    assert tasks[4]["depends_on"] == ["1", "2", "3", "4"], (
        f"task 5 must depend on 1..4, got {tasks[4]['depends_on']!r}"
    )


def test_generated_task_with_bare_range_falls_back_to_id_inference():
    """A bare range injects nothing — the id fallback takes over.

    A task with a hierarchical id ("5-1") and no usable reference in
    its desc gets its direct parent, exactly as before.
    """
    tasks = [
        {
            "id": "5-1",
            "description": "本任务 X。前置条件：任务 5 已完成。",
        },
    ]

    TasksGenerator._postprocess_depends_on(tasks)

    assert tasks[0]["depends_on"] == ["5"], (
        f"the id-inference fallback must produce ['5'], "
        f"got {tasks[0]['depends_on']!r}"
    )


def test_existing_depends_on_is_not_overwritten():
    """A non-empty ``depends_on`` survives the post-processing."""
    tasks = [
        {
            "id": "5",
            "description": "第五个任务。前置条件：任务 {1-4} 已完成。",
            "depends_on": ["2"],
        },
    ]

    TasksGenerator._postprocess_depends_on(tasks)

    assert tasks[0]["depends_on"] == ["2"], (
        f"an explicit depends_on must not be overwritten, "
        f"got {tasks[0]['depends_on']!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: without an id set, a hierarchical id stays whole
# ---------------------------------------------------------------------------


def test_missing_known_ids_keeps_hierarchy():
    """No id set → ``{1-2}`` → ``['1-2']``.

    Without the plan's ids there is nothing to tell a hierarchical id
    from a range, so every token is kept literal — the same answer the
    validator gives.
    """
    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 {1-2} 已完成。"
    )

    assert result == ["1-2"], (
        f"a hierarchical id must be kept whole when no ids are known, "
        f"got {result!r}"
    )


def test_known_hierarchical_id_is_not_expanded():
    """``{1-2}`` with ``1-2`` in the plan → ``['1-2']``.

    The id set is what distinguishes a hierarchical id from a range;
    a known id is never expanded into its segments.
    """
    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 {1-2} 已完成。",
        known_ids={"1-2", "1", "2"},
    )

    assert result == ["1-2"], (
        f"a known hierarchical id must not be expanded, got {result!r}"
    )


def test_reversed_range_keeps_literal_token():
    """``{5-2}`` is not a range → the literal token is kept."""
    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 {5-2} 已完成。", known_ids=KNOWN_IDS
    )

    assert result == ["5-2"], (
        f"a reversed range must keep its literal token, got {result!r}"
    )


def test_over_wide_range_keeps_literal_token():
    """``{1-500}`` is wider than the span limit → literal token."""
    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 {1-500} 已完成。", known_ids=KNOWN_IDS
    )

    assert result == ["1-500"], (
        f"a range wider than the span limit must keep its literal token, "
        f"got {result!r}"
    )


def test_empty_description_returns_empty():
    """Empty / ``None`` input → ``[]`` (no crash)."""
    assert TasksGenerator._postprocess_extract_from_desc("") == []
    assert TasksGenerator._postprocess_extract_from_desc(None) == []


# ---------------------------------------------------------------------------
# Test 6: the real generate() path injects the expanded deps
# ---------------------------------------------------------------------------


def test_generate_injects_expanded_braced_range(plan_dir):
    """End-to-end: ``generate()`` gives task 5 the deps 1..4.

    The seam tests above pin the parser; this one pins the *wiring*:
    ``_generate_with_prompt`` must hand the parser the plan's full id
    set, otherwise the braced range "1-4" cannot be told apart from a
    hierarchical id and the four edges are never created.
    """
    tasks = []
    for task_id in ("1", "2", "3", "4"):
        tasks.append(
            {
                "id": task_id,
                "title": f"task {task_id}",
                "description": f"第 {task_id} 个任务。",
                "test_command": f"echo {task_id}",
                "status": "pending",
            }
        )
    tasks.append(
        {
            "id": "5",
            "title": "task 5",
            "description": "第五个任务。前置条件：任务 {1-4} 已完成。",
            "test_command": "echo 5",
            "status": "pending",
            # LLM dropped the field on purpose.
        }
    )

    tool = _StubCodingTool(
        response={"requirement": "desc refs end-to-end", "tasks": tasks}
    )
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    task5 = [t for t in result["tasks"] if t.get("id") == "5"][0]
    assert task5["depends_on"] == ["1", "2", "3", "4"], (
        f"generate() must inject 1..4 for the braced range, "
        f"got {task5['depends_on']!r}"
    )
