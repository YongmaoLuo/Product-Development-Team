"""
End-to-end depends_on coverage for the 4 layers introduced by tasks 1-4.

Background
----------
This is the consolidated test file for the ``depends_on`` hardening
chain added in tasks 1 → 4:

  task 1  Diagnose why LLM-emitted tasks.json misses ``depends_on``.
  task 2  Force the LLM to emit ``depends_on`` via
          ``TASKS_SYSTEM_PROMPT`` ("depends_on 字段（CRITICAL）" + 2 examples).
  task 3  Post-process fallback: if the LLM still drops the field,
          inject from ``desc`` ("前置条件/依赖：任务 X"), else from
          the hierarchical task id.
  task 4  Load-time consistency check: a plan whose ``desc`` mentions
          a task id absent from ``depends_on`` is rejected by
          ``agent._load_tasks`` with a fail-fast ``ValueError``.

The four tests below pin one contract from each layer:

  1. ``test_prompt_requires_depends_on``
     TASKS_SYSTEM_PROMPT contains the literal substring
     ``"depends_on 字段（CRITICAL）"`` so the LLM is told (in a way
     it cannot silently skip) to emit a non-empty ``depends_on``
     array on every task. This is the layer-1 contract.

  2. ``test_postprocess_extract_from_desc``
     A mock LLM emits a task whose ``description`` says
     ``"前置条件：任务 5 已完成"`` but whose ``depends_on`` is empty
     or missing. After ``TasksGenerator.generate()`` runs, the
     fallback at layer 2 has filled in ``depends_on = ["5"]``.

  3. ``test_postprocess_infer_from_id``
     A mock LLM emits a task with hierarchical id ``"2-3-1"``,
     no ``desc`` hint, and no explicit ``depends_on``. The
     layer-3 fallback (id inference) yields ``["2-3"]``.

  4. ``test_load_tasks_desc_consistency``
     A tasks.json that has ``description="前置条件：任务 1 已完成"``
     but ``depends_on=[]`` is rejected by ``AutonomousAgent._load_tasks``
     with a ``ValueError``. The error message names the offending
     task and the missing dep so operators can grep tasks.json
     directly.
"""

import json
import re
import sys
import subprocess
from pathlib import Path
from typing import Optional

import pytest


# Ensure backend/ is on sys.path so ``import tasks_generator`` /
# ``import agent`` work regardless of which test runner entry point
# is used. Mirrors the bootstrap used by ``test_agent_load.py`` /
# ``test_tasks_generator.py``.
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
# Shared fixtures
# ---------------------------------------------------------------------------


class _StubCodingTool:
    """Minimal coding tool stub that returns a caller-supplied payload.

    Mirrors the stub used by ``test_tasks_generator.py`` so the
    post-processing tests in this file are independent of any real
    LLM call. The test never inspects the prompt; it only checks
    that the post-processing loop in ``TasksGenerator.generate``
    fills in ``depends_on`` correctly on the way out.
    """

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return self._response


@pytest.fixture
def plan_dir(tmp_path):
    """A minimal plan_dir: ``prd.md`` exists so ``_load_prd()`` does not fail.

    The other ``_load_*`` readers are tolerant of missing files
    (they return None). Only ``_load_prd`` is strict.
    """
    pd = tmp_path / "plan"
    pd.mkdir(parents=True)
    (pd / "prd.md").write_text(
        "# PRD\nMinimal PRD for unit test.\n", encoding="utf-8"
    )
    return pd


# ---------------------------------------------------------------------------
# Layer 1: TASKS_SYSTEM_PROMPT contains the depends_on mandate
# ---------------------------------------------------------------------------


def test_prompt_requires_depends_on():
    """``TASKS_SYSTEM_PROMPT`` contains 'depends_on 字段（CRITICAL）'.

    Layer 1 of the depends_on hardening chain: the LLM is told,
    in unmistakable wording, that every task MUST output a
    ``depends_on`` array. The marker substring
    ``"depends_on 字段（CRITICAL）"`` is the only line in the prompt
    that uses the field's name together with the CRITICAL
    annotation, so a future refactor that drops the wording (or
    moves the section header) is caught immediately.

    A rephrase like "every task must declare dependencies" without
    the field name would still be a regression — the model has
    no way to map prose to a JSON key without seeing the key
    name. The contract this test pins is therefore:
      1. The substring is present in the prompt template.
      2. The substring is a section header (a ``##`` line) so the
         LLM is structurally cued to treat it as a hard rule, not
         a description.
    """
    from tasks_generator import TASKS_SYSTEM_PROMPT

    needle = "depends_on 字段（CRITICAL）"
    assert needle in TASKS_SYSTEM_PROMPT, (
        f"TASKS_SYSTEM_PROMPT must contain the literal substring "
        f"{needle!r} so the LLM is told to emit depends_on on every "
        f"task. The current prompt has it on the line starting at "
        f"the '##' section header."
    )

    # Belt-and-suspenders: the line that carries the needle must be
    # a Markdown ``##`` header so the LLM treats it as a rule
    # rather than a paragraph. We anchor the match to the line
    # boundary so accidental embedding inside a longer sentence is
    # detected.
    needle_line_re = re.compile(
        rf"^## {re.escape(needle)}\s*$",
        re.MULTILINE,
    )
    assert needle_line_re.search(TASKS_SYSTEM_PROMPT), (
        f"the depends_on mandate must appear on a '## ...' section "
        f"header line, not embedded in prose. Current prompt header "
        f"check failed for needle {needle!r}."
    )


# ---------------------------------------------------------------------------
# Layer 2: post-process fallback (desc extraction) at generate() time
# ---------------------------------------------------------------------------


def test_postprocess_extract_from_desc(plan_dir):
    """Mock LLM drops depends_on; desc says '前置条件：任务 5' → depends_on=['5'].

    Layer 2 of the depends_on hardening chain: the LLM emitted a
    task with no ``depends_on`` field at all, but the description
    contains the canonical Chinese prereq phrase
    ``"前置条件：任务 5 已完成"``. The post-processing loop in
    ``TasksGenerator.generate()`` runs the
    ``_postprocess_extract_from_desc`` helper and writes
    ``depends_on = ["5"]`` onto the task before saving.

    The end-to-end shape is the one the spec pins: a mock
    ``coding_tool`` returns a task dict without ``depends_on``;
    after ``generate()`` the returned (and on-disk) task MUST
    carry ``depends_on = ["5"]``. The check is run through the
    public ``generate()`` entry point — not the static helper
    directly — so the wiring (field setdefault, fallback chain)
    is exercised in the same way the production code path does.
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "depends_on desc-extraction end-to-end",
        "tasks": [
            {
                "id": "5-1",
                "title": "uses desc to recover depends_on",
                "description": "本任务 X。前置条件：任务 5 已完成。",
                "test_command": "echo 5-1",
                "status": "pending",
                # LLM intentionally dropped the field — the fallback
                # in TasksGenerator.generate must surface ["5"].
            },
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    assert len(result["tasks"]) == 1, (
        f"expected exactly 1 task in the LLM response, got "
        f"{len(result['tasks'])}"
    )
    task = result["tasks"][0]
    assert task.get("id") == "5-1", (
        f"unexpected task id from the mock LLM: {task.get('id')!r}"
    )
    # The post-processing contract: depends_on must be present and
    # equal to ["5"] (the id parsed from the description).
    assert "depends_on" in task, (
        "depends_on must be present on every task after generate(); "
        "the post-processing loop setdefault guarantees the field."
    )
    assert task["depends_on"] == ["5"], (
        f"desc-extraction fallback must produce ['5'] from "
        f"'前置条件：任务 5 已完成。', got {task['depends_on']!r}"
    )

    # The on-disk tasks.json must match the in-memory result so a
    # cross-process recovery read-back sees the same value.
    on_disk = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    assert on_disk["tasks"][0]["depends_on"] == ["5"], (
        f"on-disk tasks.json depends_on must match in-memory result; "
        f"got {on_disk['tasks'][0].get('depends_on')!r}"
    )


# ---------------------------------------------------------------------------
# Layer 3: post-process fallback (id inference) at generate() time
# ---------------------------------------------------------------------------


def test_postprocess_infer_from_id(plan_dir):
    """id='2-3-1', no desc, no depends_on → depends_on=['2-3'].

    Layer 3 of the depends_on hardening chain: the LLM emitted a
    task with hierarchical id ``"2-3-1"`` but no ``desc`` hint
    and no explicit ``depends_on``. The desc parser cannot help
    (the description does not contain a trigger phrase), so the
    final-tier fallback ``_postprocess_infer_from_id`` returns the
    task's direct parent: ``"2-3"`` (NOT the full ancestor chain
    ``["2", "2-3"]`` — only the direct parent).

    The id ``"2-3-1"`` is deliberately NOT ``"1-2"`` (the canonical
    example in the spec) to pin that the inference rule applies
    to arbitrary depth, not just one level. The contract is
    "direct parent only" so an executor running
    ``agent._build_layers`` can rely on the explicit declaration
    being minimal; transitive closure is recovered by the
    topological sort.
    """
    from tasks_generator import TasksGenerator

    response = {
        "requirement": "depends_on id-inference end-to-end",
        "tasks": [
            {
                "id": "2-3-1",
                "title": "deep hierarchy, no desc, no explicit dep",
                # Description without a trigger word — id-inference
                # fallback must take over.
                "description": "本任务无任何前置条件描述。",
                "test_command": "echo 2-3-1",
                "status": "pending",
                # LLM dropped the field.
            },
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    result = gen.generate()

    assert len(result["tasks"]) == 1
    task = result["tasks"][0]
    assert task.get("id") == "2-3-1"
    # The id-inference contract: direct parent only ("2-3"), not
    # the full ancestor chain. The prompt explicitly says
    # `depends_on` should be minimal, and the executor's layer
    # builder walks transitive closures anyway.
    assert task.get("depends_on") == ["2-3"], (
        f"id-inference fallback for id='2-3-1' must yield ['2-3'] "
        f"(direct parent only), got {task.get('depends_on')!r}"
    )

    # Mirror the on-disk check so cross-process recovery matches.
    on_disk = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    assert on_disk["tasks"][0]["depends_on"] == ["2-3"], (
        f"on-disk tasks.json depends_on must match in-memory result; "
        f"got {on_disk['tasks'][0].get('depends_on')!r}"
    )


# ---------------------------------------------------------------------------
# Layer 4: agent._load_tasks fail-fast consistency check
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Initialise a real git repo at ``project_dir``.

    ``GitManager`` uses ``search_parent_directories=True`` and would
    otherwise walk up to a sibling checkout.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


class _DummyCodingTool:
    """Stub coding tool — ``__init__`` is the only thing the
    ``AutonomousAgent`` constructor calls. We never reach an
    LLM call in this test because we exercise ``_load_tasks``
    directly.
    """

    def __init__(self, *args, **kwargs):
        pass


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    """Write a tasks.json with the given task dicts and return the path."""
    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for depends_on load-time consistency",
        "tasks": tasks,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _build_agent(project_dir: Path):
    """Build a minimal AutonomousAgent bound to ``project_dir``.

    The ``__init__`` instantiates ``BackgroundManager`` /
    ``RetryManager`` / ``RollbackManager`` — these have no I/O side
    effects so they are safe to run inside a tmp_path.
    """
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for depends_on load-time consistency",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
    )


@pytest.fixture
def project_dir(tmp_path):
    """A real project_dir with a real git repo so GitManager can bind."""
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


def test_load_tasks_desc_consistency(project_dir):
    """desc says '前置条件：任务 1' but depends_on=[] → ValueError at load.

    Layer 4 of the depends_on hardening chain: even after the
    layer-1 (prompt) and layer-2/3 (post-process) fixes, the
    planner can still drift. The LLM may emit a non-empty
    description line ``"前置条件：任务 1 已完成"`` but supply a
    ``depends_on`` array that does NOT include ``"1"``. The result
    is a plan whose prose and whose data disagree — the layer
    builder walks the data path and ignores the desc, so the
    task would be scheduled before its prerequisite finishes.

    ``agent._load_tasks`` reuses the desc-parsing logic and
    rejects the plan with a fail-fast ``ValueError`` whose
    message names BOTH the offending task id and the missing
    dependency. The test pins:

      * The plan is rejected (ValueError raised).
      * The error message names the offending task id
        (the task carrying the bad description).
      * The error message names the missing dependency
        (``"1"``) so the operator can grep tasks.json directly.

    The example is the exact wording pinned by the task spec:
    ``description = "前置条件：任务 1 已完成"`` with
    ``depends_on = []``. The task id is arbitrary; we use
    ``"task-A"`` so the operator can distinguish it from
    prerequisite id ``"1"`` in the error message.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "1",
                "title": "prereq",
                "description": "",
                "test_command": "echo 1",
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "task-A",
                "title": "downstream with desc/depends_on drift",
                "description": "前置条件：任务 1 已完成。",
                "test_command": "echo task-A",
                "status": "pending",
                # PROBLEM: the desc says task 1 is a prereq but
                # depends_on is empty. _load_tasks must reject.
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)

    with pytest.raises(ValueError) as exc_info:
        agent._load_tasks()

    msg = str(exc_info.value)
    # The error must name the offending task id (the task
    # carrying the bad description).
    assert "task-A" in msg, (
        f"expected offending task id 'task-A' in error message, "
        f"got: {msg!r}"
    )
    # The error must name the missing dependency id ("1") so the
    # operator can grep tasks.json directly.
    assert "1" in msg, (
        f"expected missing dep '1' in error message, got: {msg!r}"
    )
    # The error should hint at the source of the discrepancy.
    assert "desc" in msg.lower() or "前置" in msg, (
        f"error should mention 'desc' or '前置' as the source of the "
        f"discrepancy, got: {msg!r}"
    )
