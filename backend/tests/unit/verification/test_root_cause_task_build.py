"""TDD spec — 根因任务与唯一实现任务的构建（任务 14）。

背景
----
「根因演进」被判定之后（任务 13 的 ``needs_root_cause_task``），框架必须
先派一个**只读**的根因任务把当前根因定位清楚，再派**一个**实现任务按
根因报告改代码。这两类任务不是普通的 repair：它们有各自的 ``kind``，
根因任务没有 ``files_to_modify``（它不许改代码），实现任务的
``files_to_modify`` 来自根因报告里的路径集合。

编号
----
两类新任务的 id **复用既有 repair 序列分配器**
（``next_repair_seq_base`` / ``parse_repair_task_id``），不引入第二套编号
空间 —— 第二套编号空间意味着同一个 plan 里出现两个都叫「第 3 号」的
任务，而 ``plan_tasks`` 是按 ``(plan_id, task_id)`` 定位的。

TDD contract — 4 cases
-----------------------
1. ``test_root_cause_task_shape`` —— 字段齐全、``read_only`` 为真、
   ``report_path`` 精确匹配。
2. ``test_implementation_task_requires_files`` —— 根因报告无路径 → 抛错。
3. ``test_task_id_global_unique`` —— 与既有 repair 序列不冲突。
4. ``test_kind_field_distinguishes`` —— 两类任务 ``kind`` 不同。

边界条件
--------
* 根因任务必须 ``read_only=True``，且**不含** ``files_to_modify``。
* 实现任务的 ``files_to_modify`` 取自根因报告；为空则拒绝生成并报错
  （宁可不派任务，也不要派一个没有改动范围的任务）。
* ``select_repairable_failed_vps`` 的筛选语义不变（根因任务不经过它）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from repair_generator import (  # noqa: E402
    RepairTaskGenerator,
    next_repair_seq_base,
    parse_repair_task_id,
)
from root_cause_task import (  # noqa: E402
    IMPLEMENTATION_KIND,
    ROOT_CAUSE_KIND,
    build_implementation_task,
    build_root_cause_task,
)


_EVIDENCE = {"command": "pytest tests/x.py", "error": "AssertionError"}


class _FixedRepairTaskGenerator(RepairTaskGenerator):
    """Minimal stub bypassing __init__ (no real Persistence / CodingTool)."""

    def __init__(self, plan_dir: Path, project_dir: Path):
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        self.coding_tool = None
        self.persistence = None
        self.prd_file = plan_dir / "prd.json"
        self.prd_md_file = plan_dir / "prd.md"
        self.arch_file = plan_dir / "arch-design.md"
        self.test_file = plan_dir / "test-design.md"
        self.verification_report_file = plan_dir / "verification_report.json"


@pytest.fixture
def tmp_plan(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "exec"
    project_dir.mkdir()
    return plan_dir, project_dir


# ---------------------------------------------------------------------------
# 1. 根因任务的形状
# ---------------------------------------------------------------------------
def test_root_cause_task_shape():
    """根因任务字段齐全、只读、report_path 精确匹配。"""
    task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
    )

    assert task["kind"] == ROOT_CAUSE_KIND
    assert task["read_only"] is True
    assert task["report_path"] == "plans/p1/reports/root-cause-VP-3.md"
    assert task["deliverable"] == [
        "确定性复现步骤",
        "完整因果链",
        "一次覆盖所有层的修复方案",
        # 2026-10-29 — 诊断书写的验收方式必须是「这次失败的那些用例」，
        # 不是整仓门禁：门禁一轮 45-70 分钟，修复要迭代 5-10 轮。
        "验收方式（只选中这次失败的用例）",
        # 2026-10-30 — 改动范围是根因链的交接面：没有它，下一个实现任务
        # 的 files_to_modify 就填不出来，根因链停在
        # root_cause_report_without_scope。
        "改动范围（要改动的文件，相对仓库根的路径）",
    ]
    # 只读任务不得声明改动范围。
    assert "files_to_modify" not in task
    # 任务得能派出去：标题、描述、状态、归属轮次都在。
    assert task["title"]
    assert task["description"]
    assert task["failed_vp_id"] == "VP-3"
    assert task["round"] == 3
    assert task["status"] == "pending"
    # 证据随任务带过去，根因任务才知道要复现什么。
    assert task["evidence"] == _EVIDENCE


# ---------------------------------------------------------------------------
# 2. 实现任务必须有改动范围
# ---------------------------------------------------------------------------
def test_implementation_task_requires_files():
    """根因报告没有可用路径 → 拒绝生成实现任务并报错。"""
    with pytest.raises(ValueError) as empty:
        build_implementation_task(
            {"paths": []}, plan_id="p1", vp_id="VP-3", round_no=3,
        )
    assert "path" in str(empty.value).lower() or "路径" in str(empty.value)

    # 报告里压根没有路径这个键，同样拒绝。
    with pytest.raises(ValueError):
        build_implementation_task(
            {"root_cause": "解析器把空响应当成成功"},
            plan_id="p1", vp_id="VP-3", round_no=3,
        )

    # 报告里有路径 → 生成成功，改动范围取自报告。
    task = build_implementation_task(
        {"paths": ["backend/parser.py", "backend/reader.py"]},
        plan_id="p1", vp_id="VP-3", round_no=3,
    )
    assert task["kind"] == IMPLEMENTATION_KIND
    assert task["files_to_modify"] == ["backend/parser.py", "backend/reader.py"]


# ---------------------------------------------------------------------------
# 3. 任务 id 复用既有 repair 序列
# ---------------------------------------------------------------------------
def test_task_id_global_unique():
    """新任务的 id 在既有 repair 序列里，不另起一套编号。"""
    existing = ["repair-r3-01", "repair-r3-02"]

    root_task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
        existing_task_ids=existing,
    )

    # 不与既有 id 冲突。
    assert root_task["id"] not in existing
    # 且它占的是 repair 序列里的一个号（能被既有解析器读出来）。
    assert parse_repair_task_id(root_task["id"]) == (3, 3)
    # 序列分配器看得见它 —— 下一个任务不会复用这个号。
    assert next_repair_seq_base([root_task["id"]], 3) == 4

    impl_task = build_implementation_task(
        {"paths": ["backend/parser.py"]},
        plan_id="p1", vp_id="VP-3", round_no=3,
        existing_task_ids=existing + [root_task["id"]],
    )
    assert impl_task["id"] != root_task["id"]
    assert impl_task["id"] not in existing + [root_task["id"]]
    assert parse_repair_task_id(impl_task["id"]) == (3, 4)


# ---------------------------------------------------------------------------
# 4. 两类任务的 kind 不同
# ---------------------------------------------------------------------------
def test_kind_field_distinguishes():
    """根因任务与实现任务靠 kind 区分。"""
    root_task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
    )
    impl_task = build_implementation_task(
        {"paths": ["backend/parser.py"]},
        plan_id="p1", vp_id="VP-3", round_no=3,
    )

    assert root_task["kind"] == ROOT_CAUSE_KIND == "root_cause"
    assert impl_task["kind"] == IMPLEMENTATION_KIND == "implementation"
    assert root_task["kind"] != impl_task["kind"]
    # 根因任务没有改动范围，实现任务有 —— 这是两者最实质的差别。
    assert "files_to_modify" not in root_task
    assert impl_task["files_to_modify"]


# ---------------------------------------------------------------------------
# 追加路径：两类新任务走同一个入口，且与既有 repair 共用一个编号空间
# ---------------------------------------------------------------------------
def test_append_path_accepts_new_kinds(tmp_plan):
    """RepairTaskGenerator 的追加路径接收两类新任务，并保留它们的 kind。"""
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)

    root_task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
    )
    impl_task = build_implementation_task(
        {"paths": ["backend/parser.py"]},
        plan_id="p1", vp_id="VP-3", round_no=3,
    )
    # 追加时没有 id —— 入口自己从 repair 序列里分配。
    root_task.pop("id")
    impl_task.pop("id")

    gen.append_to_tasks([root_task, impl_task], round_number=3)

    with open(project_dir / "tasks.json", "r", encoding="utf-8") as f:
        payload = json.load(f)

    assert len(payload) == 2
    assert payload[0]["kind"] == ROOT_CAUSE_KIND
    assert payload[1]["kind"] == IMPLEMENTATION_KIND
    # 两个 id 都落在 repair 序列里，且互不相同。
    assert parse_repair_task_id(payload[0]["id"]) == (3, 1)
    assert parse_repair_task_id(payload[1]["id"]) == (3, 2)
    assert payload[0]["id"] != payload[1]["id"]


def test_append_path_keeps_plain_repair_ids(tmp_plan):
    """既有 repair 任务的 id 形状不变（R{round}-{i}）。"""
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)

    gen.append_to_tasks(
        [{"title": "修复 VP-007", "description": "占位符替换"}],
        round_number=1,
    )

    with open(project_dir / "tasks.json", "r", encoding="utf-8") as f:
        payload = json.load(f)

    assert payload[0]["id"] == "R1-1"


def test_append_path_rejects_writable_root_cause_task(tmp_plan):
    """带 files_to_modify 的根因任务不得进入任务列表。"""
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)

    bad = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
    )
    bad["files_to_modify"] = ["backend/parser.py"]

    with pytest.raises(ValueError):
        gen.append_to_tasks([bad], round_number=3)


def test_append_path_rejects_implementation_without_files(tmp_plan):
    """没有改动范围的实现任务不得进入任务列表。"""
    plan_dir, project_dir = tmp_plan
    gen = _FixedRepairTaskGenerator(plan_dir, project_dir)

    bad = build_implementation_task(
        {"paths": ["backend/parser.py"]},
        plan_id="p1", vp_id="VP-3", round_no=3,
    )
    bad["files_to_modify"] = []

    with pytest.raises(ValueError):
        gen.append_to_tasks([bad], round_number=3)