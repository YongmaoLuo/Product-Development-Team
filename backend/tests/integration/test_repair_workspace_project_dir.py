"""The repair round happens in the verification worktree — one task per defect.

What this pins
--------------
A verification run that fails leaves *confirmed defects* behind, and
every one of them has to become exactly one repair task, in the tree the
verification branch owns.  Three properties, each one a way to lose the
run rather than a way to fail loudly:

  1. **One defect, one task.**  Two failed verification points produce
     two repair tasks, each naming one of them.  Two defects sharing a
     task put both fixes in one commit; one defect getting two tasks
     puts one fix in two commits.  Both are what "一缺陷一提交"
     (test decision 7/8) exists to rule out.
  2. **The repair happens in the worktree.**  The task carries the
     verification worktree's path, and that path is the one the plan's
     execution row names.  A repair task pointing anywhere else would
     have its fix land outside the tree the verification branch owns —
     in the pull request head, or in the main checkout.
  3. **No failure, no task.**  A round with nothing confirmed produces
     no repair task at all, and asks no model: "nothing to repair" and
     "the generator ran" have to look the same on disk, or a converged
     plan keeps generating work.

The worktree is the one ``verification_workspace.ensure_workspace``
materialised and ``verification_workspace.register_project_dir``
registered — the same handover the production flow performs, and the
same field (``plan_execution.project_dir``) the execution chain reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from orphan_rules import merged_subtask_kwargs
from repair_generator import RepairTaskAssembler, RepairTaskGenerator
from task import SubTask
from verification_workspace import register_project_dir

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class _ScriptedRepairTool:
    """A coding-tool double that answers the repair-content call.

    The rows are handed back verbatim, so a test can make the model
    misbehave in exactly the way it wants to pin — here, proposing the
    same defect twice.

    The answer carries both envelope keys the two calls in this module
    use (``tasks`` for the content call, ``commands`` for the bounded
    fill loop), so the double answers whichever prompt it is handed
    without having to recognise it.
    """

    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = [dict(row) for row in rows]
        self.calls = 0

    def query_json(self, prompt, system_instruction, **kwargs):
        self.calls += 1
        return {
            "tasks": [dict(row) for row in self.rows],
            "commands": [],
        }


def _row(vp_id: str, **overrides) -> Dict[str, Any]:
    """One repair-content row, as the model would answer with it."""
    slug = vp_id.lower().replace("-", "_")
    row = {
        "failed_vp_id": vp_id,
        "title": f"修复 {vp_id}",
        "description": f"{vp_id} 的实际结果与预期不符，需要修复其根因。",
        "acceptance_criteria": f"pytest tests/test_regression_{slug}.py 退出 0",
        "files_to_modify": ["backend/service.py"],
        "test_command": f"pytest tests/test_regression_{slug}.py",
        "priority_hint": "high",
        "execution_group_hint": 0,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_falsifiability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the pre-repair probe.

    The probe answers "does this command fail before the repair?", which
    is a question about the *tree* rather than about the repair round.
    Running it here would make the test depend on a pytest run inside a
    throwaway worktree — slow, and failing for reasons that have
    nothing to do with what is being pinned.
    """
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def plan_document(verification_plan):
    """The plan's verification document — the two points under test."""
    document = {
        "verification_points": [
            {
                "id": "VP-001",
                "title": "登录接口返回 200",
                "verification_method": "code_test",
                "evidence_command": "pytest tests/test_regression_vp_001.py",
            },
            {
                "id": "VP-002",
                "title": "退出登录后拒绝访问",
                "verification_method": "code_test",
                "evidence_command": "pytest tests/test_regression_vp_002.py",
            },
        ],
    }
    path = verification_plan.plan_dir / "verification_plan.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


@pytest.fixture
def failed_vps() -> List[Dict[str, Any]]:
    """Two confirmed defects, as the verification report hands them over."""
    return [
        {
            "id": "VP-001",
            "title": "登录接口返回 200",
            "priority": "high",
            "actual_result": "AssertionError: expected 200, got 500",
            "evidence": "reports/round-1.json",
            "test_command": "pytest tests/test_regression_vp_001.py",
        },
        {
            "id": "VP-002",
            "title": "退出登录后拒绝访问",
            "priority": "medium",
            "actual_result": "AssertionError: expected 401, got 200",
            "evidence": "reports/round-1.json",
            "test_command": "pytest tests/test_regression_vp_002.py",
        },
    ]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _execution_row(plan_id: str) -> dict:
    """The plan's ``plan_execution`` row, read through the repository."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _persist_tasks(plan_id: str, tasks: List[dict]) -> None:
    """Write the repair tasks to ``plan_tasks`` — the orchestrator's step.

    The orchestrator persists the assembled tasks through
    :class:`PlanTaskRepository` and never through ``tasks.json``, so
    this is the write the executor's own loader reads back.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        for task in tasks:
            # ``status`` is a runtime field the repository owns.
            repo.add_task(
                plan_id,
                {k: v for k, v in task.items() if k != "status"},
            )
    finally:
        conn.close()


def _persisted_tasks(plan_id: str) -> List[dict]:
    """The rows the executor reads back for ``plan_id``.

    Read through ``iter_orphan_tasks`` — the same call the executor's
    Phase-2 reconcile uses — so the entries are shaped the way the
    executor sees them rather than the way this test wrote them.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        return list(repo.iter_orphan_tasks(plan_id, set()))
    finally:
        conn.close()


def _repair_round(
    plan,
    failed_vps: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    *,
    project_dir: Path = None,
) -> Tuple[_ScriptedRepairTool, List[dict], List[dict]]:
    """The repair round, in the order the orchestrator runs it.

    Two steps, in this order, because that is the order the production
    flow uses: the generator produces the *content* of the repairs, and
    the assembler turns that content into the tasks the executor runs.
    """
    tool = _ScriptedRepairTool(rows)
    generator = RepairTaskGenerator(
        tool,
        plan.plan_dir,
        project_dir if project_dir is not None else plan.workspace.path,
    )
    contents = generator.generate_repair_contents(failed_vps, round_number=1)
    tasks = RepairTaskAssembler(
        round_number=1,
        failed_vps=failed_vps,
        vp_test_commands={},
        plan_path=str(plan.plan_dir / "verification_plan.json"),
    ).assemble(contents)
    return tool, contents, tasks


# ---------------------------------------------------------------------------
# 1. One defect, one task.
# ---------------------------------------------------------------------------


def test_one_task_per_defect(verification_plan, plan_document, failed_vps):
    """Two confirmed defects produce two repair tasks — one for each.

    The model answered with three rows: one for each defect, plus a
    second one for the first defect.  A defect that gets two tasks gets
    two commits and two regression runs, and the round's own bookkeeping
    counts one fix as two — so the duplicate row is dropped and the
    first row for a defect wins.
    """
    rows = [
        _row("VP-001"),
        _row("VP-002"),
        _row("VP-001", title="修复 VP-001（另一种写法）"),
    ]

    _tool, _contents, tasks = _repair_round(
        verification_plan, failed_vps, rows,
    )

    defects = [task["failed_vp_id"] for task in tasks]
    assert len(defects) == len(set(defects)), (
        f"a defect must not get two repair tasks; got {defects}"
    )
    assert sorted(defects) == ["VP-001", "VP-002"], (
        f"expected one task per confirmed defect; got {defects}"
    )
    assert len({task["id"] for task in tasks}) == len(tasks)


# ---------------------------------------------------------------------------
# 2. The repair happens in the worktree.
# ---------------------------------------------------------------------------


def test_repair_task_project_dir_is_worktree(
    verification_plan, plan_document, failed_vps,
):
    """The repair task points at the worktree the execution row names.

    The caller handed the generator a *different* directory on purpose
    — the pull request head.  The registered worktree is the tree the
    repair has to happen in: the verification branch owns it, and the
    pull request head and the main checkout do not.  So the task, the
    execution row and the row the executor reads all have to name the
    worktree, and nothing else.
    """
    worktree = verification_plan.workspace.path
    register_project_dir(verification_plan.plan_id, worktree)
    head_before = verification_plan.upstream.head_sha()

    _tool, _contents, tasks = _repair_round(
        verification_plan,
        failed_vps,
        [_row("VP-001"), _row("VP-002")],
        project_dir=verification_plan.upstream.repo,
    )

    assert tasks, "the round produced no repair task"
    for task in tasks:
        assert task.get("project_dir") == str(worktree), (
            f"the repair task must run in the worktree; got {task!r}"
        )

    # The pull request head is not the tree the fix belongs in, and the
    # round must leave it exactly where it was.
    assert verification_plan.upstream.head_sha() == head_before, (
        "the repair round moved the pull request head"
    )

    # The execution row names the same tree.
    row = _execution_row(verification_plan.plan_id)
    assert row is not None, "the registration did not write a plan_execution row"
    assert row["project_dir"] == str(worktree)

    # And so does the row the executor reads back — this is the
    # ``SubTask.project_dir`` the boundary is about.
    _persist_tasks(verification_plan.plan_id, tasks)
    persisted = _persisted_tasks(verification_plan.plan_id)
    assert persisted, "no repair task reached plan_tasks"
    for entry in persisted:
        sub = SubTask(**merged_subtask_kwargs(entry))
        assert sub.project_dir == str(worktree), (
            "the task the executor runs must name the worktree; got "
            f"{sub.project_dir!r}"
        )
        assert sub.project_dir == row["project_dir"], (
            "the task and the execution row must name the same tree"
        )


# ---------------------------------------------------------------------------
# 3. No failure, no task.
# ---------------------------------------------------------------------------


def test_no_failed_vps_no_tasks(verification_plan, plan_document):
    """Nothing confirmed → no repair task, and no model call.

    A round with no failed verification point has nothing to repair.
    Emitting a task anyway would put a repair into the queue for a
    defect nobody confirmed, and the executor would go and "fix" it.
    """
    tool = _ScriptedRepairTool([_row("VP-001")])
    generator = RepairTaskGenerator(
        tool, verification_plan.plan_dir, verification_plan.workspace.path,
    )

    contents = generator.generate_repair_contents([], round_number=1)
    assert contents == [], f"an empty failure set must produce no content; got {contents}"

    tasks = RepairTaskAssembler(
        round_number=1,
        failed_vps=[],
        vp_test_commands={},
        plan_path=str(verification_plan.plan_dir / "verification_plan.json"),
    ).assemble(contents)
    assert tasks == [], f"an empty failure set must produce no task; got {tasks}"

    assert tool.calls == 0, "the model must not be asked when nothing failed"
