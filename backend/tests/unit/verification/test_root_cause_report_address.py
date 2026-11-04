"""TDD spec — 根因报告的地址：写的地方 == 读的地方。

背景
----
一份根因报告有三个消费者，它们必须指向**同一个文件**：

1. **编排器下发**给执行器的那份任务载荷里的 ``report_path``
   （``verification.orchestrator._root_cause_chain_tasks`` →
   ``root_cause_task.build_root_cause_task``）；
2. **链读回**时用的 ``_root_cause_report_file``；
3. **执行器**解析交付物时用的 ``agent.resolve_report_path``。

三者各算各的地址时，链读到的可能不是执行器交付的那一份 —— 报告写得再好，
链也读不到，于是这条验证点被判成「没交付」，而报告其实躺在磁盘上。

2026-11-03 的分叉长这样：锚在**工作区**时，编排器下发的是
``<project_dir>/plans/<plan_id>/reports/…``，链读回的是
``<plans 根>/<plan_id>/reports/…`` —— 两个不同的文件（磁盘上两份都在，
内容还差几行）。修法是把锚换成**计划自己的目录**，并且让两端走**同一个
helper**：构造上就没有第二个锚可选，也就无从分叉。

TDD contract — 2 cases
-----------------------
1. ``test_the_orchestrator_and_the_executor_agree_on_the_report_address``
   —— 三个地址逐字相等，且落在 ``plan_dir/reports/`` 下面。
2. ``test_the_report_address_is_not_anchored_on_the_workspace``
   —— 地址**不**在 ``project_dir/plans/`` 下面（工作区那一侧）。

边界条件
--------
* 三处地址必须来自**同一个**锚。
* 报告名沿用 ``root-cause-<vp_id>.md``，一个验证点一份。
* 诊断任务仍然是只读的：载荷里没有 ``files_to_modify``。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from agent import resolve_report_path  # noqa: E402
from task import SubTask  # noqa: E402
from verification.orchestrator import (  # noqa: E402
    _CHAIN_STAGE_ROOT_CAUSE,
    VerificationOrchestrator,
)


PLAN_ID = "p1"
VP_ID = "VP-3"


def _orchestrator(plan_dir: Path, project_dir: Path) -> VerificationOrchestrator:
    """An orchestrator with only the two directories and the fingerprints.

    The chain's task builder touches nothing else, so the heavyweight
    ``__init__`` (which builds an agent, a repair generator and a plan
    state) is not needed to pin the address.
    """
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_dir = plan_dir
    orch.project_dir = project_dir
    orch._failure_fingerprints = {}
    return orch


def _chain_task(orch: VerificationOrchestrator) -> dict:
    """The root-cause task the orchestrator hands the executor."""
    tasks = orch._root_cause_chain_tasks(
        [{"vp_id": VP_ID, "action": _CHAIN_STAGE_ROOT_CAUSE}],
        round_number=2,
        seq_base=1,
        evidence_by_vp={},
        vp_test_commands={},
    )
    assert len(tasks) == 1
    return tasks[0]


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A plan tree and a workspace tree, side by side and distinct."""
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    return plan_dir, project_dir


# ---------------------------------------------------------------------------
# 1. 三个地址逐字相等
# ---------------------------------------------------------------------------
def test_the_orchestrator_and_the_executor_agree_on_the_report_address(tmp_path):
    """编排器下发的、链读回的、执行器解析的 —— 同一个文件。"""
    plan_dir, project_dir = _tree(tmp_path)
    orch = _orchestrator(plan_dir, project_dir)

    task = _chain_task(orch)

    written = Path(task["report_path"])
    read_back = orch._root_cause_report_file(VP_ID)
    resolved = resolve_report_path(SubTask(**task), project_dir)

    assert written == read_back, (
        "the address the task was handed and the address the chain reads "
        f"back are different files: {written} vs {read_back}"
    )
    assert written == resolved, (
        "the address the task was handed and the address the executor "
        f"resolves are different files: {written} vs {resolved}"
    )
    # 落在计划自己的报告目录下面。
    assert written.parent == plan_dir / "reports"
    assert written.name == f"root-cause-{VP_ID}.md"

    # 诊断任务仍然是只读的。
    assert task["read_only"] is True
    assert "files_to_modify" not in task


# ---------------------------------------------------------------------------
# 2. 地址不锚在工作区那一侧
# ---------------------------------------------------------------------------
def test_the_report_address_is_not_anchored_on_the_workspace(tmp_path):
    """工作区那一侧（``<project_dir>/plans/…``）不是这个地址。"""
    plan_dir, project_dir = _tree(tmp_path)
    orch = _orchestrator(plan_dir, project_dir)

    task = _chain_task(orch)
    written = Path(task["report_path"])

    assert project_dir not in written.parents, (
        "the report address is still anchored on the workspace: "
        f"{written}"
    )
    assert written != (
        project_dir / "plans" / PLAN_ID / "reports" / f"root-cause-{VP_ID}.md"
    )
