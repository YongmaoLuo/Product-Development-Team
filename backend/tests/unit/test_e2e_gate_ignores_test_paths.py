"""Regression tests: the E2E gate must not treat test files as a surface.

Background
----------
``TasksGenerator._enforce_e2e_task_gate`` decides whether a plan touches
a user-facing surface. Besides the requirement text it has a path
heuristic (``surface_path_hints``): a task whose ``files_to_modify``
mentions ``frontend/``, ``/api/``, ``routes.py``, ``page.tsx``,
``app.py``, ``api_server``, ``handlers/`` or ``routers/`` is a "surface"
plan and MUST end with a Playwright E2E task.

The heuristic used to match **every** file, including test files. A
plan that only touched ``backend/tests/integration/api/test_x.py`` hit the
``/api/`` substring (the ``api/`` *directory name* of the test tree) and
was forced to end with an E2E task -- a plan that has no user-facing API
at all, so the requirement was unsatisfiable and the whole generation
aborted with ``TasksGenerationError``.

The fix: test paths never participate in the surface decision. Where a
test file lives says nothing about whether the *product* has a user
surface.

The gate is reached from the production entry point
``POST /api/tasks/{plan_id}/generate`` -> ``TasksGenerator.generate()``
-> ``_generate_with_prompt()`` -> ``_enforce_e2e_task_gate()``, so the
last two tests in this file drive the whole thing through ``generate()``
with a stubbed LLM (no model call).
"""

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------


class _StubCodingTool:
    """Minimal coding tool stub -- no LLM, no network.

    Mirrors the stub used by ``tests/unit/test_tasks_generator.py``:
    ``query_json`` returns a canned response and records the calls.
    """

    def __init__(self, response: dict):
        self._response = response
        self.calls: list = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.calls.append(
            {"prompt": prompt, "system_instruction": system_instruction}
        )
        return self._response


@pytest.fixture(autouse=True)
def _patch_tasks_self_review(monkeypatch):
    """Stub the mandatory second-pass self-review.

    ``generate()`` calls ``coding_tool.query()`` for the second LLM
    pass; the stub above only implements ``query_json``. This replaces
    the second-pass entry point itself with a benign no-op.
    """
    import tasks_generator as tg

    monkeypatch.setattr(
        tg.TasksGenerator,
        "_run_tasks_self_review",
        lambda self, tasks_data, falsifiability=None, **_ignored: None,
    )
    return monkeypatch


@pytest.fixture
def plan_dir(tmp_path):
    """A minimal plan_dir: ``prd.md`` must exist for ``_load_prd()``."""
    pd = tmp_path / "plan"
    pd.mkdir(parents=True)
    (pd / "prd.md").write_text(
        "# PRD\nMinimal PRD for unit test.\n", encoding="utf-8"
    )
    return pd


#: Requirement text free of every trigger keyword (接入 / 数据源 / 端点 /
#: 前端 / 页面 / 图表 / 信号 / 数据 / frontend / endpoint, plus the
#: word-boundary short ones api / ui / 路由 / route).
_NO_SURFACE_REQUIREMENT = "adjust the pause semantics inside the stop flow"


#: The exact "missing keyword groups" the gate reports today. Pinned so a
#: future edit that loosens (or reorders) the gate's error surfaces here.
_EXPECTED_MISSING_GROUPS = (
    "Missing keyword groups: ['端到端/end-to-end/E2E', "
    "'Playwright/Puppeteer', '断言/assert', "
    "'模拟用户输入 (user input / fill / click / 输入)']."
)


def _tasks_data(files_to_modify, requirement=_NO_SURFACE_REQUIREMENT):
    """A two-task plan; the LAST task is deliberately not an E2E task."""
    return {
        "requirement": requirement,
        "tasks": [
            {
                "id": "T-1",
                "title": "update the test",
                "description": "update the integration test expectations",
                "files_to_modify": [files_to_modify[0]],
                "depends_on": [],
            },
            {
                "id": "T-2",
                "title": "wire the route module",
                "description": "adjust the stop flow implementation",
                "files_to_modify": list(files_to_modify[1:]),
                "depends_on": ["T-1"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Test 1 -- no false positive: a test path is not a user-facing surface
# ---------------------------------------------------------------------------


def test_test_path_alone_does_not_trigger_e2e_gate():
    """A plan whose only surface-looking file is a TEST file is not gated.

    ``backend/tests/integration/api/test_x.py`` contains the substring
    ``/api/`` -- but it is a test file, so it must not make the plan look
    like it touches a user-facing API. ``backend/routes/verification.py``
    matches none of the hints. Neither file, nor the requirement text,
    may arm the gate.
    """
    from tasks_generator import TasksGenerator, TasksGenerationError

    data = _tasks_data([
        "backend/tests/integration/api/test_x.py",
        "backend/routes/verification.py",
    ])

    gen = TasksGenerator(coding_tool=None, plan_dir=Path("/tmp"))
    try:
        gen._enforce_e2e_task_gate(data)
    except TasksGenerationError as exc:  # pragma: no cover - failure path
        pytest.fail(
            "test paths must not arm the E2E gate, but it raised: "
            f"{exc}"
        )


def test_test_path_variants_are_all_excluded():
    """Every documented test-path shape is excluded from the surface check.

    Pinned per the gate's own criteria: ``tests/`` directories,
    ``test_*`` names, ``*_test.py`` / ``*.test.ts`` / ``*.test.tsx`` /
    ``*.spec.ts`` endings. Each of these would otherwise match a hint
    (``/api/``, ``page.tsx``, ...) and arm the gate.
    """
    from tasks_generator import TasksGenerator, TasksGenerationError

    test_paths = [
        "backend/tests/integration/api/test_x.py",
        "tests/unit/test_thing.py",
        "backend/foo/test_thing.py",
        "backend/foo/bar_test.py",
        "frontend/app/page.test.ts",
        "frontend/app/page.test.tsx",
        "frontend/app/page.spec.ts",
        "backend\\tests\\integration\\api\\test_x.py",
    ]
    for path in test_paths:
        data = _tasks_data([path, "backend/routes/verification.py"])
        gen = TasksGenerator(coding_tool=None, plan_dir=Path("/tmp"))
        try:
            gen._enforce_e2e_task_gate(data)
        except TasksGenerationError as exc:
            pytest.fail(
                f"test path {path!r} must not arm the E2E gate, but it "
                f"raised: {exc}"
            )


# ---------------------------------------------------------------------------
# Test 2 -- negative control: the gate is NOT relaxed
# ---------------------------------------------------------------------------


def test_frontend_path_still_triggers_e2e_gate():
    """A real surface file still arms the gate, with the same message.

    Same requirement as the no-false-positive case, but the plan now
    touches ``frontend/app/page.tsx``. The gate MUST raise
    ``TasksGenerationError`` and the reported missing keyword groups
    must be exactly the ones the gate reports today -- the fix excluded
    test paths, it did not loosen the gate.
    """
    from tasks_generator import TasksGenerator, TasksGenerationError

    data = _tasks_data([
        "backend/tests/integration/api/test_x.py",
        "frontend/app/page.tsx",
    ])

    gen = TasksGenerator(coding_tool=None, plan_dir=Path("/tmp"))
    with pytest.raises(TasksGenerationError) as excinfo:
        gen._enforce_e2e_task_gate(data)

    message = str(excinfo.value)
    assert _EXPECTED_MISSING_GROUPS in message, (
        "the gate's missing-keyword-group report changed:\n"
        f"{message}"
    )


# ---------------------------------------------------------------------------
# Test 3 -- production entry point (POST /api/tasks/{id}/generate)
# ---------------------------------------------------------------------------


def test_generate_succeeds_for_test_only_surface(plan_dir):
    """``generate()`` -- the production entry -- accepts a test-only plan.

    This is the path the server uses
    (``routes/phases.py::generate_tasks`` -> ``TasksGenerator.generate()``).
    The LLM is stubbed, so no model is called. Before the fix this plan
    aborted with ``TasksGenerationError`` ("missing_e2e_verification_task")
    because the test path matched ``/api/``.
    """
    from tasks_generator import TasksGenerator, TasksGenerationError

    response = {
        "requirement": _NO_SURFACE_REQUIREMENT,
        "tasks": [
            {
                "id": "T-1",
                "title": "update the test",
                "description": "update the integration test expectations",
                "files_to_modify": [
                    "backend/tests/integration/api/test_x.py",
                ],
                "test_command": "pytest backend/tests/integration/api/test_x.py",
                "status": "pending",
            },
            {
                "id": "T-2",
                "title": "wire the route module",
                "description": "adjust the stop flow implementation",
                "files_to_modify": ["backend/routes/verification.py"],
                "test_command": "pytest backend/tests/unit",
                "status": "pending",
            },
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    try:
        result = gen.generate()
    except TasksGenerationError as exc:
        pytest.fail(
            "generate() must not fail the E2E gate for a test-only "
            f"plan, but it raised: {exc}"
        )

    assert [t["id"] for t in result["tasks"]] == ["T-1", "T-2"]
    assert (plan_dir / "tasks.json").exists()


def test_generate_still_fails_for_real_surface(plan_dir):
    """``generate()`` still aborts when the plan really touches the UI.

    The negative control on the production path: the same plan with
    ``frontend/app/page.tsx`` in ``files_to_modify`` must still be
    rejected, and ``tasks.json`` must carry the error payload rather
    than a task list.
    """
    from tasks_generator import TasksGenerator, TasksGenerationError

    response = {
        "requirement": _NO_SURFACE_REQUIREMENT,
        "tasks": [
            {
                "id": "T-1",
                "title": "build the page",
                "description": "add the page component",
                "files_to_modify": ["frontend/app/page.tsx"],
                "test_command": "npm test",
                "status": "pending",
            },
            {
                "id": "T-2",
                "title": "wire the route module",
                "description": "adjust the stop flow implementation",
                "files_to_modify": ["backend/routes/verification.py"],
                "test_command": "pytest backend/tests/unit",
                "status": "pending",
            },
        ],
    }
    tool = _StubCodingTool(response=response)
    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)

    with pytest.raises(TasksGenerationError) as excinfo:
        gen.generate()

    assert _EXPECTED_MISSING_GROUPS in str(excinfo.value)

    # The gate failure is written to tasks.json as an error payload.
    payload = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    assert payload.get("error_type") == "missing_e2e_verification_task"
