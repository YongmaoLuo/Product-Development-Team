"""验证分支的输入路径：读盘上那份已校验的机器计划，不问模型。

背景
----
验证分支的验证点集合**不是**模型生成的。它是人工审核过的验证计划书（十
章节契约）翻译出来的机器格式 JSON，并且在 ``/start`` 管线里与计划书逐条比对
过（等价门禁）。验证回路对它的职责只有一件：**读出来并校验**。

但 ``generate_verification_plan`` 原来只有一条路 —— 问 LLM 生成一份，或者
（开发分支）复用盘上那份。验证分支走进来时它会**重新生成**一批验证点，换掉
审核过的那批。那批新验证点没有经过任何人的审核，于是"被验对象通过了什么"
从此与审核时说的不是同一件事，而没有任何一步会告诉操作者这件事发生过。

所以验证分支必须换一条路：直接读盘上那份 JSON，校验它，返回。读不出来或
校验不过 → **抛错**，不退回 LLM（退回等于悄悄换掉验证点集合）。

三条边界
--------
* 验证分支：模型**一次都不许**被叫。
* 校验不过 / 文件缺失：抛错，不降级。
* 开发分支：仍然问模型生成，行为一个字都不改。

另外两条是「验证分支」这条路的语义本身：

* 审核过的验证点集合是**定死的** —— 不做增量评估，也不把计划书里人写的
  验证方法按框架词表判成退休方法（那会把每一条验证点变成"不验"，验证回路
  于是"跑完了"，一条也没验）。
* 盘上那份是审核过的产物：这一趟只读不写。

测试数据
--------
全部在 tmp 目录里：一个 plan 目录 + 一份机器计划 JSON + 一个脚本化 LLM
（记录调用次数）。不碰任何 provider，不联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_agent import (  # noqa: E402
    VerificationAgent,
    VerificationBranchPlanError,
)

#: 验证分支的机器计划：与 ``/start`` 管线写出的那一份同名同格式。
MACHINE_PLAN_FILENAME = "verification_plan.json"

#: 验证分支的产物：验证计划书。有它、又没有 ``tasks.json`` ⇒ 验证分支。
PLAN_DOC_FILENAME = "verification-plan.md"

#: 开发分支的产物：任务清单。有它、又没有计划书 ⇒ 开发分支。
TASKS_FILENAME = "tasks.json"


# ---------------------------------------------------------------------------
# 脚本化 LLM
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """脚本化 LLM：记录每次调用，回一份固定计划。

    **调用次数是这个文件的核心断言。** 「验证分支不碰模型」这句话只有靠
    「模型一次都没被叫过」才测得出来 —— 只看返回值的话，一份碰巧长得一样
    的模型输出会把这件事盖过去。
    """

    def __init__(self, payload: Dict[str, Any] | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._payload = payload if payload is not None else {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "模型生成的验证点",
                    "related_prd_criteria": "C1: 与审核无关",
                    "verification_method": "code_review",
                    "priority": "medium",
                    "expected_result": "模型说了算",
                },
            ],
        }

    def query_json(
        self,
        prompt: str = "",
        system_instruction: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            **kwargs,
        })
        # 深拷贝：调用方会就地改这份计划，返回同一个 dict 会让第二次调用
        # 看到第一次的修改。
        return json.loads(json.dumps(self._payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 盘上的机器计划
# ---------------------------------------------------------------------------

#: 盘上那份机器计划：``/start`` 管线写出来、且过了等价门禁的那一份。
#:
#: 方法字段用的是**计划书里人写的自由文本**（"单元测试"）—— 这正是翻译器
#: 今天的产出形态：``verification_plan_translator._skeleton`` 原样抄计划书的
#: 「验证方法」一行，而框架的方法词表（``api_test`` / ``code_review`` / …）是
#: 另一套词汇。
DISK_PLAN: Dict[str, Any] = {
    "services": [],
    "verification_points": [
        {
            "id": "VP-001",
            "title": "配额按真实重开时刻睡眠",
            "verification_method": "单元测试",
            "related_prd_criteria": "C1: 配额耗尽时睡到真实重开时刻",
            "expected_result": "E1 | 两条判据全部满足，无超时",
        },
        {
            "id": "VP-002",
            "title": "报告把 skip 条目标注为未验证",
            "verification_method": "以手工核验为主",
            "related_prd_criteria": "C1: skip 条目显示为「未验证」",
            "expected_result": "E1 | 渲染结果与期望一致",
        },
    ],
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def verification_plan_dir(tmp_path) -> Path:
    """一个验证分支的 plan 目录：有计划书，没有任务清单。"""
    plan_dir = tmp_path / "verification-plan-dir"
    plan_dir.mkdir()
    (plan_dir / PLAN_DOC_FILENAME).write_text(
        "# 验证计划书\n\n（内容无关紧要：分支判定只看文件在不在）\n",
        encoding="utf-8",
    )
    return plan_dir


@pytest.fixture
def dev_plan_dir(tmp_path) -> Path:
    """一个开发分支的 plan 目录：有任务清单，没有计划书。"""
    plan_dir = tmp_path / "dev-plan-dir"
    plan_dir.mkdir()
    (plan_dir / TASKS_FILENAME).write_text(
        json.dumps({"tasks": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    return plan_dir


def _agent(plan_dir: Path, tool: _ScriptedTool) -> VerificationAgent:
    """造一个 agent：脚本化模型 + 一个空的项目目录。"""
    project_dir = plan_dir.parent / f"{plan_dir.name}-project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=tool,
    )


def _write_disk_plan(plan_dir: Path, plan: Dict[str, Any]) -> Path:
    path = plan_dir / MACHINE_PLAN_FILENAME
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_verification_branch_reads_json_without_llm(verification_plan_dir):
    """验证分支：返回盘上那份计划，模型一次都没被叫。"""
    plan_path = _write_disk_plan(verification_plan_dir, DISK_PLAN)

    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    plan = agent.generate_verification_plan()

    assert tool.calls == [], (
        f"验证分支不许问模型，实际调用了 {len(tool.calls)} 次"
    )
    assert [vp["id"] for vp in plan["verification_points"]] == [
        "VP-001", "VP-002",
    ]
    assert plan["verification_points"][0]["title"] == "配额按真实重开时刻睡眠"
    assert plan["verification_points"][1]["expected_result"] == "E1 | 渲染结果与期望一致"
    assert plan.get("services") == []
    # 盘上那份是审核过的产物：这一趟不许改写它。
    assert json.loads(plan_path.read_text(encoding="utf-8")) == DISK_PLAN
    # 也不许把它改写成"跑不成"的样子 —— 见下面那条专门的测试。
    assert all("obsolete" not in vp for vp in plan["verification_points"])


def test_verification_branch_keeps_reviewed_method_untouched(verification_plan_dir):
    """验证分支：计划书里人写的验证方法原样留着，不被判成退休方法。

    开发分支的磁盘复用路径会跑一趟方法归一化（``_normalize_legacy_methods``）：
    词表外的方法一律标成 ``obsolete``。那一趟是为「模型生成的计划」准备的 ——
    模型用的是框架的方法词表，词表外就说明它是 2026-09-18 退休的方法。

    验证分支的计划里，方法字段是**计划书里人写的自由文本**。拿退休检查去
    处理它，会把审核过的每一条验证点都标成 ``obsolete`` —— 执行器于是
    一条都不跑，Phase 3 的信封里也就一条验证点都没有，判定环节拿到的是
    一张空白支票。

    留着它，框架跑不动这个方法时会**大声失败**（运行器报「未知验证方法」），
    那才是真的问题（人写的方法与框架词表之间的映射还没接上）。
    """
    _write_disk_plan(verification_plan_dir, DISK_PLAN)

    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    plan = agent.generate_verification_plan()

    assert tool.calls == []
    vp = plan["verification_points"][0]
    assert vp["verification_method"] == "单元测试"
    assert "obsolete" not in vp
    assert "obsolete_reason" not in vp


def test_verification_branch_does_not_evaluate_delta(verification_plan_dir):
    """验证分支：不做增量评估（审核过的验证点集合是定死的）。

    增量条目是「这轮多了什么任务」的产物，而任务清单是**开发分支**的产物。
    验证分支凭空加出来的验证点不在审核过的计划书里 —— 等价门禁下一轮就会
    报出差异，而这一轮已经拿它当审核过的条目跑了。
    """
    _write_disk_plan(verification_plan_dir, DISK_PLAN)

    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    agent.generate_verification_plan()

    assert tool.calls == []
    # 增量评估的记忆文件（``verification_plan_delta`` 的私有常量）。
    assert not (
        verification_plan_dir / "verification_plan_delta_state.json"
    ).exists()


def test_verification_branch_corrupt_json_raises(verification_plan_dir):
    """验证分支：JSON 坏了 → 抛错，不退回 LLM 重新生成。"""
    (verification_plan_dir / MACHINE_PLAN_FILENAME).write_text(
        "{ this is not json",
        encoding="utf-8",
    )

    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    with pytest.raises(VerificationBranchPlanError) as excinfo:
        agent.generate_verification_plan()

    assert tool.calls == [], "校验失败不许退回 LLM 重新生成"
    assert MACHINE_PLAN_FILENAME in str(excinfo.value)


def test_verification_branch_missing_json_raises(verification_plan_dir):
    """验证分支：机器计划压根没有 → 抛错，不退回 LLM 重新生成。"""
    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    with pytest.raises(VerificationBranchPlanError) as excinfo:
        agent.generate_verification_plan()

    assert tool.calls == [], "机器计划缺失不许退回 LLM 重新生成"
    assert MACHINE_PLAN_FILENAME in str(excinfo.value)


def test_verification_branch_rejects_plan_without_points(verification_plan_dir):
    """验证分支：机器计划里没有验证点 → 抛错（空计划跑起来什么也没验）。"""
    _write_disk_plan(verification_plan_dir, {"services": [], "verification_points": []})

    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    with pytest.raises(VerificationBranchPlanError):
        agent.generate_verification_plan()

    assert tool.calls == []


def test_verification_branch_preserves_delta_origin_fields(verification_plan_dir):
    """验证分支：盘上已有的增量验证点保留 ``origin="delta"`` 与 ``added_reason``。"""
    plan = json.loads(json.dumps(DISK_PLAN, ensure_ascii=False))
    plan["verification_points"].append({
        "id": "VP-003",
        "title": "增量新增的验证点",
        "verification_method": "code_review",
        "related_prd_criteria": "C1: 增量",
        "expected_result": "E1 | 增量通过",
        "origin": "delta",
        "added_reason": "第 2 轮新增任务需要一条验证点",
    })
    _write_disk_plan(verification_plan_dir, plan)

    tool = _ScriptedTool()
    agent = _agent(verification_plan_dir, tool)

    result = agent.generate_verification_plan()

    assert tool.calls == []
    delta_vp = [
        vp for vp in result["verification_points"] if vp["id"] == "VP-003"
    ]
    assert len(delta_vp) == 1
    assert delta_vp[0]["origin"] == "delta"
    assert delta_vp[0]["added_reason"] == "第 2 轮新增任务需要一条验证点"


def test_dev_branch_generation_unchanged(dev_plan_dir):
    """开发分支：仍然问模型生成计划，行为不变。"""
    tool = _ScriptedTool()
    agent = _agent(dev_plan_dir, tool)

    plan = agent.generate_verification_plan()

    assert len(tool.calls) == 1, (
        f"开发分支仍然问模型生成计划，实际调用 {len(tool.calls)} 次"
    )
    assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001"]
    assert plan["verification_points"][0]["title"] == "模型生成的验证点"
    assert (dev_plan_dir / MACHINE_PLAN_FILENAME).exists()
