"""Unit tests for RepairTaskGenerator — VP-013: repair task generation on verification failure."""
import json
import sys
from pathlib import Path

import pytest


# Ensure backend is on the import path for these unit tests
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _StubCodingTool:
    """Returns one deterministic task per evidence item referenced in the prompt."""

    def query_json(self, prompt, system_instruction, timeout=1800):
        import re as _re
        # Count "VP xxxx" lines in the prompt body so we emit one task per failed VP.
        vp_ids = _re.findall(r"VP\s+([A-Za-z0-9_-]+)", prompt or "")
        # De-dupe while preserving order.
        seen = []
        for v in vp_ids:
            if v not in seen:
                seen.append(v)
        tasks = []
        for i, vp in enumerate(seen or ["VP-AAA"], 1):
            tasks.append({
                "id": f"R1-{i}",
                "title": f"修复 {vp}",
                "description": f"修复偏离点 {vp}",
                "test_command": f"pytest tests/test_{vp.lower()}.py",
            })
        return {"tasks": tasks}


class _FixedRepairTaskGenerator:
    """Minimal stub bypassing __init__ (no real Persistence / CodingTool)."""

    def __init__(self, plan_dir: Path, project_dir: Path):
        from repair_generator import RepairTaskGenerator

        self.plan_dir = plan_dir
        self.project_dir = project_dir
        self.coding_tool = _StubCodingTool()
        self.persistence = None
        self.prd_file = plan_dir / "prd.json"
        self.prd_md_file = plan_dir / "prd.md"
        self.arch_file = plan_dir / "arch-design.md"
        self.test_file = plan_dir / "test-design.md"
        self.verification_report_file = plan_dir / "verification_report.json"

        # Bind methods under test.
        self._generate_repair_tasks = (
            RepairTaskGenerator._generate_repair_tasks.__get__(self)
        )
        self._get_next_repair_task_id = (
            RepairTaskGenerator._get_next_repair_task_id.__get__(self)
        )
        self.append_to_tasks = RepairTaskGenerator.append_to_tasks.__get__(self)
        self._build_deterministic_repair_tasks = (
            RepairTaskGenerator._build_deterministic_repair_tasks.__get__(self)
        )
        self._build_repair_task_prompt = (
            RepairTaskGenerator._build_repair_task_prompt.__get__(self)
        )
        self._generate_fallback_repair_tasks = (
            RepairTaskGenerator._generate_fallback_repair_tasks.__get__(self)
        )


@pytest.fixture
def tmp_plan(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "exec"
    project_dir.mkdir()
    return plan_dir, project_dir


def _evidence(vp_id: str, test_command: str = "pytest tests/test_x.py") -> dict:
    return {
        "vp_id": vp_id,
        "actual_result": "some real code failure: AssertionError",
        "evidence": "code returned wrong value",
        "test_command": test_command,
    }


def test_repair_task_generation(tmp_plan):
    """Each failed VP produces exactly one repair task with concrete test_command."""
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)

    # Three different VPs failed → expect three distinct repair tasks.
    evidence = [
        _evidence("VP-001"),
        _evidence("VP-002"),
        _evidence("VP-003"),
    ]
    requirement_context = {"acceptance_criteria": [], "technical_constraints": []}

    tasks = gen._generate_repair_tasks(requirement_context, evidence, round_number=2)

    assert len(tasks) == 3, (
        f"Expected exactly one repair task per failed VP (3 VPs → 3 tasks), got {len(tasks)}"
    )

    # Each task must have a concrete (non-empty) test_command.
    for t in tasks:
        assert t.get("test_command"), f"Repair task missing test_command: {t}"
        assert isinstance(t["test_command"], str) and t["test_command"].strip(), (
            "test_command must be a non-empty string"
        )

    # Round-2 repair tasks tagged with prefix R2-
    for t in tasks:
        assert t["id"].startswith("R2-"), (
            f"Round-2 repair task should have id starting with 'R2-', got {t['id']}"
        )

    # IDs must be unique per VP (no collisions)
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == len(ids), f"Duplicate repair task ids: {ids}"


def test_append_to_tasks(tmp_plan):
    """append_to_tasks preserves existing tasks and appends R{round}- tagged repair tasks."""
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)

    # Seed project_dir/tasks.json with 5 existing tasks (simulating Phase 5 task list).
    existing_tasks = [
        {"id": f"{i}", "title": f"existing-{i}", "status": "completed"}
        for i in range(1, 6)
    ]
    project_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = project_dir / "tasks.json"
    with open(tasks_file, "w", encoding="utf-8") as f:
        json.dump(existing_tasks, f)

    # Build 2 repair tasks (no id) to simulate LLM/fallback output.
    repair_tasks = [
        {
            "title": "修复 VP-007 的 test_command 占位符",
            "description": "占位符替换",
            "test_command": "pytest tests/integration/test_x.py",
        },
        {
            "title": "修复 VP-009 的并发锁",
            "description": "加锁逻辑",
            "test_command": "pytest tests/integration/test_lock.py",
        },
    ]

    result_path = gen.append_to_tasks(repair_tasks, round_number=1)

    # Returns the tasks.json path
    assert result_path == tasks_file

    # Read back the resulting tasks.json
    with open(tasks_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert isinstance(payload, list)
    assert len(payload) == 7, (
        f"Expected 5 existing + 2 appended = 7 tasks, got {len(payload)}"
    )

    # The first 5 entries must be the original existing tasks in order
    assert payload[:5] == existing_tasks, "Existing tasks must be preserved unchanged"

    # The last 2 are the repair tasks with R1- prefix
    appended = payload[5:]
    assert appended[0]["id"] == "R1-1"
    assert appended[1]["id"] == "R1-2"
    for t in appended:
        assert t["id"].startswith("R1-"), f"Expected R1- prefix, got {t['id']}"
        assert t.get("test_command"), "Repair task must carry a concrete test_command"
