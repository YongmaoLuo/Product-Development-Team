"""Unit tests for RepairTaskGenerator's deterministic task patterns."""
import json
import sys
from pathlib import Path

import pytest


# Ensure backend is on the import path for these unit tests
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class FakeCodingTool:
    """Stub that returns deterministic JSON for the LLM-driven path."""

    def query_json(self, prompt, system_instruction, timeout=1800):
        return {"tasks": []}


class _FixedRepairTaskGenerator:
    """Subclass that skips __init__ (no need for real Persistence / CodingTool)."""

    def __init__(self, plan_dir: Path, project_dir: Path):
        from repair_generator import RepairTaskGenerator
        # Build a stub instance with the minimum attrs needed.
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        self.coding_tool = FakeCodingTool()
        self.persistence = None
        self.prd_file = plan_dir / "prd.json"
        self.prd_md_file = plan_dir / "prd.md"
        self.arch_file = plan_dir / "arch-design.md"
        self.test_file = plan_dir / "test-design.md"
        self.verification_report_file = plan_dir / "verification_report.json"
        # Bind the methods we want to test.
        self._build_deterministic_repair_tasks = (
            RepairTaskGenerator._build_deterministic_repair_tasks.__get__(self)
        )
        self._get_next_repair_task_id = (
            RepairTaskGenerator._get_next_repair_task_id.__get__(self)
        )


@pytest.fixture
def tmp_plan(tmp_path):
    plan_dir = tmp_path / "test-plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "exec"
    project_dir.mkdir()
    return plan_dir, project_dir


def test_no_deterministic_task_asks_the_agent_to_edit_the_plan(tmp_plan):
    """2026-09-18: the two plan-editing patterns are gone.

    They used to emit repair tasks whose whole deliverable was "change the
    ``-k`` keyword / the shell placeholder in
    ``verification_plan.json``'s ``test_command`` for this VP". That was
    wrong three ways over:

      1. It violated the immutability rule the delta module already stated
         ("绝不修改已有验证点——标题、断言、test_command 都不许改") — the
         thing being graded was rewriting its own criterion;
      2. the deliverable lives in the this repository's ``plans/``, *outside*
         ``project_dir``, so the generator could name no project-relative
         file → ``files_to_modify: []`` → the empty-diff gate failed the
         task every time → forced split → the split could only satisfy the
         gate by inventing a project-side artifact (one run grew
         ``tools/verify_vp_rust_case.py`` four times that way);
      3. VPs have no ``test_command`` at all any more.

    The assertion is behavioural: however broken the evidence, no
    deterministic task may point the agent at the plan file.
    """
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)
    evidence = [
        {
            "vp_id": "VP-015",
            "actual_result": (
                "pytest -k 'multi_lock_ordered_acquire or no_deadlock' collected 0 "
                "of 5 items (5 deselected, exit code 5)"
            ),
            "evidence": "0 tests collected, exit code 5",
            "test_command": (
                "source backend/.venv/bin/activate && pytest "
                "backend/tests/integration/test_file_lock_manager.py -k "
                "'multi_lock_ordered_acquire or no_deadlock' -v"
            ),
        },
        {
            "vp_id": "VP-016",
            "actual_result": (
                "bash: {primary_project_dir}/run.sh: No such file or directory"
            ),
            "evidence": "exit 127, unresolved placeholder {primary_project_dir}",
            "test_command": "bash {primary_project_dir}/run.sh",
        },
    ]

    tasks = gen._build_deterministic_repair_tasks(evidence, round_number=1)

    for task in tasks:
        haystack = f"{task.get('title', '')}\n{task.get('description', '')}"
        assert "verification_plan.json" not in haystack, task["title"]
        assert "test_command" not in haystack, task["title"]



def test_no_deterministic_task_for_real_code_failure(tmp_plan):
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)
    evidence = [{
        "vp_id": "VP-002",
        "actual_result": "AssertionError: expected True but got False in module X",
        "evidence": "production code returned wrong value",
        "test_command": "pytest tests/unit/test_module.py::test_x",
    }]

    tasks = gen._build_deterministic_repair_tasks(evidence, round_number=1)
    assert tasks == [], (
        "real code failures should NOT trigger deterministic repair — "
        "they must go through the normal LLM path"
    )
