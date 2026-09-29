"""
Regression tests for ``TasksGenerator.generate()`` writing location.

Background
----------
After the bug fixes for the dual-source ``tasks.json`` problem
(agent.py::_persist_task_status must write to the canonical
``task_manager.tasks_file``; the executor reads from the same
canonical file via ``--tasks-file``), it is critical that NO code
path re-introduces a copy of ``tasks.json`` at
``<project_dir>/tasks.json``.

This file pins the contract:

  1. ``TasksGenerator.generate()`` writes to ``plan_dir/tasks.json``
     and ONLY to that path. It must never create
     ``<project_dir>/tasks.json``.
  2. The public ``generate()`` signature has NO ``project_dir``
     argument. If a future refactor adds it back, raise immediately.

The tests stub out the LLM (``coding_tool.query_json``) and the
``_load_*`` file-readers so the suite runs without network access
or a real PRD document on disk.
"""

import inspect
import json
import re
import sys
from pathlib import Path
from typing import Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture(autouse=True)
def _patch_tasks_self_review(monkeypatch):
    """Legacy tasks-generator tests stub only ``query_json``. The
    mandatory second-pass self-review calls ``coding_tool.query()``,
    which their stub does not implement — so we stub the second-pass
    entry point itself with a benign no-op that returns the canonical
    input unchanged.
    """
    sys.path.insert(0, str(_BACKEND_DIR))
    try:
        import tasks_generator as tg
    finally:
        if str(_BACKEND_DIR) in sys.path:
            sys.path.remove(str(_BACKEND_DIR))

    def _passthrough(*args, **kwargs):
        doc_content = kwargs.get("doc_content") or (
            args[0] if args else ""
        )
        return {
            "doc_type": kwargs.get("doc_type") or "tasks",
            "attempted": True,
            "succeeded": True,
            "rewrote": False,
            "findings": [],
            "fixed_content": doc_content,
            "input_content_hash": "sha256:" + "x" * 64,
            "fixed_content_hash": "sha256:" + "x" * 64,
            "severity_high_count": 0,
            "severity_medium_count": 0,
            "severity_low_count": 0,
            "mandatory": True,
            "error": None,
        }

    monkeypatch.setattr(
        tg.TasksGenerator, "_run_tasks_self_review",
        lambda self, tasks_data, falsifiability=None: None,
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubCodingTool:
    """Minimal coding tool stub.

    The real ``CodingTool`` makes a network call to an LLM. For these
    tests we don't care what the prompt looks like — only that
    ``generate()`` writes the returned dict to ``plan_dir/tasks.json``
    and nowhere else.
    """

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return self._response


@pytest.fixture
def plan_dir(tmp_path):
    """A minimal plan_dir: prd.md exists so _load_prd() doesn't fail.

    The other ``_load_*`` readers are tolerant of missing files
    (they return None). Only ``_load_prd`` is strict.
    """
    pd = tmp_path / "plan"
    pd.mkdir(parents=True)
    (pd / "prd.md").write_text("# PRD\nMinimal PRD for unit test.\n", encoding="utf-8")
    return pd


# ---------------------------------------------------------------------------
# Test 1: generate() writes only to plan_dir/tasks.json
# ---------------------------------------------------------------------------


def test_generate_writes_only_to_canonical_tasks_json(tmp_path, plan_dir):
    """``generate()`` writes to ``plan_dir/tasks.json`` and nowhere else.

    The dual-source bug was: ``generate()`` wrote the canonical
    file AND copied it to ``<project_dir>/tasks.json``, and the
    executor then wrote status updates to a THIRD file (the wrong
    one). The fix to agent.py stopped the third write, but
    ``generate()`` still keeps a stale copy at
    ``<project_dir>/tasks.json`` that nobody updates, so it falls
    further and further out of sync with reality.

    This test plants a few candidate ``project_dir`` paths around
    the plan_dir and asserts that NONE of them get a ``tasks.json``
    after ``generate()`` returns. The canonical file MUST exist and
    carry the LLM's response.
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "stub requirement",
        "tasks": [
            {
                "id": "T-1",
                "title": "stub task",
                "description": "stub",
                "test_command": "echo T-1",
                "status": "pending",
            }
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    # Plant a few ``project_dir`` candidates near plan_dir. We then
    # assert NONE of them acquire a tasks.json after generate().
    sibling_a = plan_dir.parent / "project_a"
    sibling_b = plan_dir.parent / "project_b"
    nested = plan_dir / "nested" / "project"
    for candidate in (sibling_a, sibling_b, nested):
        candidate.mkdir(parents=True, exist_ok=True)
        # Sanity: confirm no tasks.json exists at the candidate yet.
        assert not (candidate / "tasks.json").exists()

    result = gen.generate()

    # 1. Canonical file written.
    canonical = plan_dir / "tasks.json"
    assert canonical.exists(), "canonical tasks.json not written by generate()"
    canonical_data = json.loads(canonical.read_text(encoding="utf-8"))
    assert canonical_data["tasks"][0]["id"] == "T-1"

    # 2. None of the candidate project_dirs got a copy.
    for candidate in (sibling_a, sibling_b, nested):
        rogue = candidate / "tasks.json"
        assert not rogue.exists(), (
            f"generate() created a stray tasks.json at {rogue}. "
            f"This is the dual-source bug — there must be exactly "
            f"ONE tasks.json (the canonical plan_dir one)."
        )

    # 3. Returned dict matches what the LLM produced.
    assert result["tasks"][0]["id"] == "T-1"


# ---------------------------------------------------------------------------
# Test 2: generate() signature has no project_dir parameter
# ---------------------------------------------------------------------------


def test_generate_signature_has_no_project_dir():
    """``generate()`` signature does not accept ``project_dir``.

    This is the API-level contract: callers cannot accidentally
    (or intentionally) pass a ``project_dir`` to ``generate()`` and
    trigger the copy-to-project-root behaviour. If a future refactor
    re-introduces the parameter, this test fails loudly.
    """
    from tasks_generator import TasksGenerator

    sig = inspect.signature(TasksGenerator.generate)
    params = list(sig.parameters.keys())
    assert "project_dir" not in params, (
        f"TasksGenerator.generate() must not accept a project_dir "
        f"parameter (the dual-source tasks.json bug is closed by "
        f"having NO copy at <project_dir>/tasks.json). Got params: "
        f"{params}"
    )


# ---------------------------------------------------------------------------
# Test 3: even if generate() is called with extra kwargs, it doesn't write
#         to a stray path
# ---------------------------------------------------------------------------


def test_prompt_contains_files_to_modify(plan_dir):
    """The task generation prompt MUST describe the ``files_to_modify`` field.

    PRD decision point 5 requires every generated task to list the files it
    will modify. The system prompt template must therefore include the field
    name, a description of its purpose, the relative-path rule, the empty-list
    boundary case, and a concrete example.
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "files_to_modify prompt contract",
        "tasks": [
            {
                "id": "1",
                "title": "stub",
                "description": "stub",
                "test_command": "echo",
                "files_to_modify": [],
            }
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)
    gen.generate()

    system_prompt = tool.calls[0]["system_instruction"]
    assert "files_to_modify" in system_prompt, "prompt template missing files_to_modify field name"
    assert "[]" in system_prompt, "prompt template missing empty-list boundary example"
    assert "相对路径" in system_prompt or "relative" in system_prompt.lower(), (
        "prompt template missing relative-path requirement"
    )


def test_prompt_example_paths_are_relative(plan_dir):
    """All example paths for ``files_to_modify`` in the prompt are relative.

    Absolute paths in the example would encourage the LLM to emit absolute
    paths, which violates the relative-path boundary condition.
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "relative path example contract",
        "tasks": [
            {
                "id": "1",
                "title": "stub",
                "description": "stub",
                "test_command": "echo",
                "files_to_modify": [],
            }
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)
    gen.generate()

    system_prompt = tool.calls[0]["system_instruction"]
    matches = re.findall(
        r'"files_to_modify"\s*:\s*\[(.*?)\]',
        system_prompt,
        re.DOTALL,
    )
    assert matches, "prompt template has no files_to_modify example list"

    example_paths = re.findall(r'"([^"]+)"', matches[0])
    assert example_paths, "files_to_modify example list contains no quoted paths"
    for path in example_paths:
        assert not path.startswith("/"), (
            f"example path {path!r} is absolute; only relative paths are allowed"
        )
        assert "://" not in path, (
            f"example path {path!r} looks like a URL, not a relative file path"
        )


def test_generate_ignores_stray_kwarg_without_crashing(plan_dir):
    """``generate()`` rejects ``project_dir=`` (TypeError, not silent copy).

    Belt-and-suspenders: even if a future refactor re-adds
    ``project_dir`` and a caller passes the old-style
    ``generate(project_dir=...)`` invocation, the test should
    fail loudly rather than silently re-create the bug. (This
    test only documents the current behaviour; it does not replace
    test 2's signature check.)
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "kwarg test",
        "tasks": [{"id": "K", "title": "k", "description": "k",
                    "test_command": "echo", "status": "pending"}],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    with pytest.raises(TypeError):
        gen.generate(project_dir=plan_dir.parent / "should_not_exist")


# ---------------------------------------------------------------------------
# depends_on post-processing (task-3 fix)
#
# Background
# ----------
# The LLM-driven ``generate()`` flow cannot be relied on to emit a
# well-formed ``depends_on`` array on every task — sometimes the field
# is missing, sometimes empty, sometimes the LLM puts the dependency
# hint in the description instead. ``TasksGenerator.generate`` therefore
# runs a 3-tier fallback in the post-processing loop:
#
#   1. LLM emitted a non-empty ``depends_on`` list -> keep it.
#   2. Otherwise parse the description's "前置条件/依赖：任务 X" hint.
#   3. Otherwise infer from the hierarchical task id (1-2 -> ["1"]).
#
# The tests below pin each of these three tiers independently, plus a
# final "every task always has the field" gate.
# ---------------------------------------------------------------------------


class _DependsOnTool:
    """Stub coding tool that returns a caller-supplied tasks payload.

    Tracks the prompts passed to ``query_json`` for inspection if a
    test wants to verify the prompt shape, but the tests below don't
    need that — they care only about the post-processing applied to
    the LLM response.
    """

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return self._response


def test_postprocess_extract_from_desc_simple_qian_zhi_tiao_jian(plan_dir):
    """TDD spec: ``postprocess_extract_from_desc`` 解析 '前置条件：任务 X' → ["X"].

    The simplest positive case: a description that contains
    ``前置条件：任务 1 已完成`` must yield ``["1"]`` from
    ``TasksGenerator._postprocess_extract_from_desc``. The static
    helper is exposed so the test pins the parser independently of
    the full ``generate()`` flow.
    """
    from tasks_generator import TasksGenerator

    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 1 已完成。"
    )
    assert result == ["1"], (
        f"expected ['1'] from simple '前置条件：任务 1' pattern, got {result!r}"
    )


def test_postprocess_extract_from_desc_hierarchical_id(plan_dir):
    """Hierarchical id form: ``前置条件：任务 1-2`` → ``["1-2"]``.

    The parser must keep the full hierarchical id (``1-2``) intact
    rather than splitting it into ``["1", "2"]``. This pins the
    ``\\d+(?:-\\d+)*`` matcher behaviour.
    """
    from tasks_generator import TasksGenerator

    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 1-2 已完成。"
    )
    assert result == ["1-2"], (
        f"expected ['1-2'] for hierarchical id, got {result!r}"
    )


def test_postprocess_extract_from_desc_slash_form(plan_dir):
    """Slash form used in the system prompt template.

    ``前置条件/依赖：已完成 任务 1-2。`` is the exact phrasing the
    TASKS_SYSTEM_PROMPT template asks the LLM to emit. The parser
    must accept it.
    """
    from tasks_generator import TasksGenerator

    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件/依赖：已完成 任务 1-2。当前代码在 foo.py 处缺少该功能。"
    )
    assert result == ["1-2"], (
        f"expected ['1-2'] from slash-form trigger, got {result!r}"
    )


def test_postprocess_extract_from_desc_no_trigger_returns_empty(plan_dir):
    """Description without trigger phrase -> ``[]`` (no false positives).

    Boundary: descriptions that never say "前置条件" / "依赖" /
    "前置条件/依赖" must NOT trigger id extraction. The downstream
    fallback (``postprocess_infer_from_id``) takes over instead.
    """
    from tasks_generator import TasksGenerator

    result = TasksGenerator._postprocess_extract_from_desc(
        "本任务实现新功能。无依赖关系。"
    )
    assert result == [], (
        f"description without trigger should yield [], got {result!r}"
    )


def test_postprocess_extract_from_desc_empty_input(plan_dir):
    """Empty / None / non-string description -> ``[]`` (no crash).

    Defensive boundary: the helper must accept ``None`` / ``""`` /
    non-string inputs without raising — the post-processing loop in
    ``generate`` reads ``task.get("description", "") or ""`` so this
    case is reachable.
    """
    from tasks_generator import TasksGenerator

    assert TasksGenerator._postprocess_extract_from_desc("") == []
    assert TasksGenerator._postprocess_extract_from_desc(None) == []


def test_postprocess_extract_from_desc_multiple_ids(plan_dir):
    """Comma-separated ids: ``任务 1, 2, 3`` -> ``["1", "2", "3"]``.

    The parser returns every parseable id from the description; the
    caller (the post-processing loop) just stores the list verbatim
    as ``depends_on``.
    """
    from tasks_generator import TasksGenerator

    result = TasksGenerator._postprocess_extract_from_desc(
        "前置条件：任务 1, 2, 3 已完成。"
    )
    assert result == ["1", "2", "3"], (
        f"expected ['1','2','3'] for comma-separated ids, got {result!r}"
    )


def test_postprocess_infer_from_id_hierarchical(plan_dir):
    """TDD spec: ``postprocess_infer_from_id`` 解析 '1-2' → ['1'].

    The hierarchical id convention encodes the dependency edge: a
    task with id ``1-2`` depends on its parent ``1``. The inferrer
    is the last-resort fallback when both the LLM output and the
    description parser fail.
    """
    from tasks_generator import TasksGenerator

    assert TasksGenerator._postprocess_infer_from_id("1-2") == ["1"]


def test_postprocess_infer_from_id_top_level_returns_empty(plan_dir):
    """Top-level id ('1') -> ``[]`` (no parent).

    A flat id has no parent, so the inferred ``depends_on`` is the
    empty list. This is correct: the executor will run it in the
    first layer with no predecessors.
    """
    from tasks_generator import TasksGenerator

    assert TasksGenerator._postprocess_infer_from_id("1") == []
    assert TasksGenerator._postprocess_infer_from_id("2") == []


def test_postprocess_infer_from_id_deep_hierarchy(plan_dir):
    """Deep hierarchy '1-2-3' -> ['1-2'] (only DIRECT parent).

    The inferrer returns ONLY the direct parent, not the full
    ancestor chain. The executor's layer builder walks the closure
    transitively, so adding "1" too would be redundant.
    """
    from tasks_generator import TasksGenerator

    assert TasksGenerator._postprocess_infer_from_id("1-2-3") == ["1-2"]


def test_postprocess_infer_from_id_empty_input(plan_dir):
    """Empty / None id -> ``[]`` (no crash).

    Defensive: the post-processing loop reads
    ``task.get("id", "")`` which can yield ``""`` or ``None`` for
    malformed tasks. The inferrer must accept these gracefully.
    """
    from tasks_generator import TasksGenerator

    assert TasksGenerator._postprocess_infer_from_id("") == []
    assert TasksGenerator._postprocess_infer_from_id(None) == []


def test_postprocess_setdefault_depends_on_present_on_every_task(plan_dir):
    """TDD spec: ``postprocess_setdefault_depends_on`` ensures EVERY
    task has ``depends_on`` after ``generate()`` (even if ``[]``).

    This is the contract gate: after ``generate()`` returns, every
    task in the returned dict's ``tasks`` list MUST have a
    ``depends_on`` field, and that field MUST be a list (possibly
    empty). A missing field is exactly the bug this post-processing
    fix is designed to eliminate.
    """
    from tasks_generator import TasksGenerator

    # LLM response covers all three tiers: explicit, desc-inferred,
    # id-inferred, plus a top-level task with no parent. Every task
    # must end up with a ``depends_on`` list after post-processing.
    response = {
        "requirement": "depends_on post-processing contract",
        "tasks": [
            {
                "id": "1",
                "title": "top level",
                "description": "无依赖。",
                "test_command": "echo 1",
                # LLM omitted depends_on entirely — fallback to id
                # inference yields [].
            },
            {
                "id": "1-2",
                "title": "depends on 1",
                "description": "本任务 X。前置条件：任务 1 已完成。",
                "test_command": "echo 1-2",
                # LLM omitted depends_on — fallback to desc parser
                # yields ["1"].
            },
            {
                "id": "2",
                "title": "depends on 1-2 (explicit)",
                "description": "本任务 Y。",
                "test_command": "echo 2",
                # LLM explicitly says depends_on=["1-2"]; the
                # post-processing loop must keep this verbatim.
                "depends_on": ["1-2"],
            },
            {
                "id": "3",
                "title": "fallback to id inference",
                "description": "本任务 Z。本任务不显式提依赖。",
                "test_command": "echo 3",
                # LLM omitted depends_on and desc has no trigger
                # word — fallback to id inference (no parent → []).
            },
        ],
    }
    tool = _DependsOnTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    # EVERY task in the returned dict must have a ``depends_on`` list.
    for task in result["tasks"]:
        assert "depends_on" in task, (
            f"task {task.get('id')!r} is missing the depends_on field "
            "after generate(); post-processing should guarantee the "
            "field is always present (even if empty)."
        )
        assert isinstance(task["depends_on"], list), (
            f"task {task.get('id')!r} has depends_on of type "
            f"{type(task['depends_on']).__name__!r}; expected list"
        )

    # Spot-check the value-resolution contract per task:
    by_id = {task["id"]: task for task in result["tasks"]}

    # Task 1: top-level, no parent → [] (id-inference fallback).
    assert by_id["1"]["depends_on"] == [], (
        f"task 1 should have depends_on=[], got {by_id['1']['depends_on']!r}"
    )

    # Task 1-2: desc parser must surface ["1"].
    assert by_id["1-2"]["depends_on"] == ["1"], (
        f"task 1-2 should have depends_on=['1'] from desc parser, "
        f"got {by_id['1-2']['depends_on']!r}"
    )

    # Task 2: LLM emitted explicit depends_on=["1-2"] → keep verbatim.
    assert by_id["2"]["depends_on"] == ["1-2"], (
        f"task 2 should keep LLM-emitted depends_on=['1-2'], "
        f"got {by_id['2']['depends_on']!r}"
    )

    # Task 3: top-level, no parent → [] (id-inference fallback).
    assert by_id["3"]["depends_on"] == [], (
        f"task 3 should have depends_on=[], got {by_id['3']['depends_on']!r}"
    )


def test_postprocess_setdefault_depends_on_when_llm_omits_field_completely(plan_dir):
    """Even when LLM omits ``depends_on`` from EVERY task, the field
    is always present after ``generate()`` returns.

    Regression-pin for the original bug: the previous post-processing
    loop only injected ``status``, ``updated_time``, ``failure_reason``
    — ``depends_on`` was never added, so every task ended up
    ``KeyError: 'depends_on'`` downstream in ``agent._build_layers``.
    After the fix, the field is set on every task regardless of what
    the LLM returned (or didn't).
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "depends_on missing entirely",
        "tasks": [
            {
                "id": "1",
                "title": "alpha",
                "description": "无依赖。",
                "test_command": "echo 1",
                # depends_on intentionally omitted.
            },
            {
                "id": "2",
                "title": "beta",
                "description": "无依赖。",
                "test_command": "echo 2",
                # depends_on intentionally omitted.
            },
        ],
    }
    tool = _DependsOnTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    for task in result["tasks"]:
        assert "depends_on" in task, (
            f"task {task.get('id')!r} still missing depends_on after "
            "post-processing; this is the original bug."
        )
        assert task["depends_on"] == [], (
            f"task {task.get('id')!r} has unexpected depends_on "
            f"{task['depends_on']!r}; expected [] (no parent, no desc hint)"
        )


def test_postprocess_setdefault_depends_on_keeps_llm_explicit_output(plan_dir):
    """Boundary: LLM-emitted non-empty ``depends_on`` is preserved as-is.

    The post-processing loop's first-tier check is "if LLM emitted a
    non-empty list, keep it" — never overwrite with desc-parser or
    id-inference results. This test pins that boundary explicitly.
    """
    from tasks_generator import TasksGenerator

    # The description ALSO contains "前置条件：任务 9 已完成" — which
    # would normally trigger the desc parser to inject ["9"]. But
    # because the LLM's explicit ["1", "2"] is already non-empty, the
    # loop MUST keep ["1", "2"] and discard the desc-parser candidate.
    response = {
        "requirement": "boundary: LLM output wins over desc parser",
        "tasks": [
            {
                "id": "3",
                "title": "explicit wins",
                "description": "前置条件：任务 9 已完成。",
                "test_command": "echo 3",
                "depends_on": ["1", "2"],
            },
        ],
    }
    tool = _DependsOnTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    assert result["tasks"][0]["depends_on"] == ["1", "2"], (
        f"LLM-emitted depends_on should be preserved verbatim, "
        f"got {result['tasks'][0]['depends_on']!r}"
    )


def test_postprocess_setdefault_depends_on_overrides_empty_llm_list(plan_dir):
    """Boundary: LLM-emitted EMPTY list is NOT preserved — the
    fallback chain re-runs and may yield a non-empty result.

    This pins the "non-empty" qualifier in the first-tier check.
    An empty ``depends_on == []`` from the LLM must NOT short-circuit
    the fallback — the desc parser and id inferrer still get a
    chance to produce a better answer.
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "boundary: empty LLM list does NOT block fallback",
        "tasks": [
            {
                "id": "1-2",
                "title": "empty depends_on from LLM",
                "description": "前置条件：任务 1 已完成。",
                "test_command": "echo 1-2",
                "depends_on": [],  # LLM emitted empty list
            },
        ],
    }
    tool = _DependsOnTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    # The desc parser should pick up "前置条件：任务 1" and overwrite
    # the empty list with ["1"].
    assert result["tasks"][0]["depends_on"] == ["1"], (
        f"empty LLM list must be overridden by desc-parser fallback; "
        f"got {result['tasks'][0]['depends_on']!r}"
    )