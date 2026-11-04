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

交付通道（2026-11-03）
---------------------
根因任务的交付物是**一个文件**，不是 diff。所以：

* ``deliverable`` 装的是交付物的**种类**（``"report"``），不是报告要写
  什么 —— 后者挪到了 ``deliverable_checklist``。``SubTask`` 把
  ``deliverable`` 声明成 ``Literal["code", "report"]``，往里塞一个列表
  会直接 ValidationError，任务连派都派不出去。
* ``deliverable_checklist`` 走不到任何人（``SubTask`` 是
  ``extra="ignore"``，白名单也不收它），所以那五件事**同时**出现在
  ``description`` 里 —— 那是执行器唯一会拼进 brief 的通道，也是唯一能
  到达写报告的人的通道。描述由清单生成，不写两份字面量。
* ``report_path`` 下发的是**绝对路径**，锚在**计划自己的目录**上
  （``<plans 根>/<plan_id>/reports/…``）。执行器把相对地址拼到自己的
  ``project_dir`` 下面，绝对地址原样使用 —— 相对地址会让同一份报告在
  不同的工作目录下指向不同的文件；而锚在工作区那一侧时，链读回的是
  计划目录里的另一份文件，两边对不上。

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
    # 2026-11-03 — 交付物是**一个文件**，所以 ``deliverable`` 装的是交付物
    # 的种类，不是报告要写什么。``SubTask`` 把它声明成
    # ``Literal["code", "report"]``，装一个列表进去会直接 ValidationError，
    # 任务连数据库都进不去。报告要写什么挪到了 ``deliverable_checklist``。
    assert task["deliverable"] == "report"
    # 2026-11-03（main 侧）— ``deliverable`` 是交付物的**类型**（"report"），
    # 这五件事是交付物的**内容**。两者挤在一个键上时，这一行连
    # ``SubTask`` 都构造不出来（``deliverable`` 是
    # ``Literal["code", "report"]``，装不下一个列表），而执行器也因此
    # 把一份报告当成代码任务来判。同一份清单另有一个键名
    # ``report_deliverables`` —— 两个键装的是同一份列表，两个都留着。
    assert task["deliverable_checklist"] == task["report_deliverables"]
    assert task["deliverable_checklist"] == [
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
    # 2026-11-03 — 下发的地址是**绝对**路径。执行器（``agent.py`` 的
    # ``resolve_report_path``）把相对地址拼到任务的 project_dir 下面、
    # 绝对地址原样使用；相对地址会让同一份报告在不同的工作目录下指向
    # 不同的文件，而一个解析不出地址的报告任务根本没有交付物可查。
    assert Path(task["report_path"]).is_absolute()
    assert task["report_path"].endswith("plans/p1/reports/root-cause-VP-3.md")
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
# 5. 交付通道：任务载荷必须能被执行器重新水合成一个「报告任务」
# ---------------------------------------------------------------------------
def test_root_cause_task_is_a_report_task_the_executor_can_dispatch():
    """``SubTask(**payload)`` 必须成立，而且必须仍是一个报告任务。

    这是 2026-11-03 那次修复的核心：``deliverable`` 装错形状时
    ``SubTask(**payload)`` 直接 ValidationError，任务连派都派不出去；
    ``deliverable`` 缺失时它回落成 ``"code"``，执行器的空 diff 闸门就把
    一份写好的报告读成「什么都没产出」（``empty_diff_no_changes``）。
    """
    from agent import is_report_task, resolve_report_path
    from task import SubTask

    task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
    )
    sub = SubTask(**task)

    assert sub.deliverable == "report"
    assert is_report_task(sub) is True
    # 执行器解析出来的地址，必须就是任务下发的那一个 —— 两者不一致时，
    # 闸门读的是另一个文件，报告写得再好也判成「没产出」。
    assert resolve_report_path(sub) == Path(task["report_path"]).resolve()


def test_root_cause_report_path_follows_the_plan_dir(tmp_path):
    """地址锚在**计划自己的目录**上，不是工作区，也不是当前工作目录。

    2026-11-03：锚从 ``project_dir`` 改成了 ``plan_dir``。工作区那一侧
    （``<project_dir>/plans/<plan_id>/reports/…``）与计划目录那一侧
    （``<plans 根>/<plan_id>/reports/…``）是**两个不同的文件**：链读回
    的是计划目录那一份，执行器交付的是工作区那一份，于是链读不到执行器
    交付的东西。

    只有一个锚 —— 调用方没有第二个选项可挑，所以两端不可能分叉。
    """
    plan_dir = tmp_path / "plans" / "p1"
    plan_dir.mkdir(parents=True)
    task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3,
        plan_dir=plan_dir,
    )
    assert task["report_path"] == str(
        plan_dir / "reports/root-cause-VP-3.md"
    )


def test_the_description_carries_the_deliverable_checklist():
    """那五件事必须在 description 里 —— 那是唯一能到达作者的通道。

    ``deliverable_checklist`` 谁也收不到（``SubTask`` 是
    ``extra="ignore"``），执行器拼 brief 用的也只有 description。清单
    落在任何一个别人都收不到的键里，「报告必须含这五项」这条契约就没人
    执行了。
    """
    task = build_root_cause_task(plan_id="p1", vp_id="VP-3", round_no=3)

    assert task["deliverable"] == "report"
    checklist = task["deliverable_checklist"]
    assert len(checklist) >= 5
    description = task["description"]
    for item in checklist:
        assert item in description, (
            f"description 里没有「{item}」：作者看不到自己要写哪几段"
        )
    assert "改动范围" in description


def test_the_checklist_survives_into_the_subtask_the_executor_gets():
    """那五件事必须活到 **SubTask** 上 —— 执行器的 brief 是从那儿拼的。

    载荷 dict 上有那五件事**还不够**：执行器拿到的是 ``SubTask``，它的
    brief 是 ``f"Subtask: {task.description}"``（``agent.py``）。描述要是
    在载荷 → ``SubTask`` 这一步丢了、或者被截断，作者就看不到自己要写
    哪几段 ——「报告必须含这五项（尤其『改动范围』）」这条契约又一次没有
    执行者。

    2026-11-03：这一条是上一轮那五件事进描述的**下游**钉子。上一条钉的
    是载荷 dict（作者拿到的形状）；这一条钉的是执行器真正读的那一个
    （``SubTask.description``）。中间隔着 ``SubTask``（``extra="ignore"``），
    中间那一步没人钉，就等于那条契约只在半路上活着。
    """
    from task import SubTask

    task = build_root_cause_task(plan_id="p1", vp_id="VP-3", round_no=3)
    sub = SubTask(**task)

    checklist = task["deliverable_checklist"]
    assert len(checklist) >= 5
    for item in checklist:
        assert item in sub.description, (
            f"SubTask.description 里没有「{item}」：执行器的 brief 拼的是"
            "SubTask.description，作者看不到自己要写哪几段"
        )
    assert "改动范围" in sub.description

    # 清单只有一个来源：描述里那五件事就是载荷里那五件事（逐字）。
    assert all(item in sub.description for item in checklist)


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
    """新任务的 id 在既有 repair 序列里，不另起一套编号。

    既有 id 是**旧形状**（``repair-r3-02``）—— 那是历史，不迁移、不重写。
    新任务的 id 是新形状（``repair-03``），但号仍然接在旧序列后面：
    分配器扫的是**两种形状**的最大值，所以旧 id 占掉的号不会被重发。
    """
    existing = ["repair-r3-01", "repair-r3-02"]

    root_task = build_root_cause_task(
        plan_id="p1", vp_id="VP-3", round_no=3, evidence=_EVIDENCE,
        existing_task_ids=existing,
    )

    # 不与既有 id 冲突。
    assert root_task["id"] not in existing
    # 且它占的是 repair 序列里的一个号（能被既有解析器读出来）。
    # 新形状不带轮次 —— 轮次用 0 表示「无轮次」（2026-11-02）。
    assert parse_repair_task_id(root_task["id"]) == (0, 3)
    # 序列分配器看得见它 —— 下一个任务不会复用这个号。
    assert next_repair_seq_base([root_task["id"]], 3) == 4

    impl_task = build_implementation_task(
        {"paths": ["backend/parser.py"]},
        plan_id="p1", vp_id="VP-3", round_no=3,
        existing_task_ids=existing + [root_task["id"]],
    )
    assert impl_task["id"] != root_task["id"]
    assert impl_task["id"] not in existing + [root_task["id"]]
    assert parse_repair_task_id(impl_task["id"]) == (0, 4)


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
    assert parse_repair_task_id(payload[0]["id"]) == (0, 1)
    assert parse_repair_task_id(payload[1]["id"]) == (0, 2)
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