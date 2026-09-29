"""
TDD tests for ``decision_point_adder``.

These cover the three adders + the post-write register_new_items hook
on each reviewer + the NO_GAP safety valve + the upstream-skip
constraint.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir_with_arch(tmp_path):
    plan_dir = tmp_path / "plans" / "test-arch-add"
    plan_dir.mkdir(parents=True)
    arch_file = plan_dir / "arch-design.md"
    arch_file.write_text(
        "# 架构设计 — 测试项目\n\n"
        "## 概述\n\n概述段落。\n\n"
        "## 决策点列表\n\n"
        "### 决策点 1: 技术栈选型\n\n"
        "**[C] 背景：** Python + FastAPI 后端。\n\n"
        "**[P] 问题：** 选什么版本。\n\n"
        "**[E] 评估：** 用 3.11。\n\n"
        "**[A] 行动：** 锁定 Python 3.11 + FastAPI 0.110。\n\n"
        "影响范围：全部模块。\n\n"
        "备选方案：① 3.12 ② 3.10\n\n"
        "### 决策点 2: 数据存储\n\n"
        "**[C] 背景：** 需要持久化。\n\n"
        "**[P] 问题：** 用什么存储。\n\n"
        "**[E] 评估：** SQLite。\n\n"
        "**[A] 行动：** 用本地 SQLite + WAL。\n\n"
        "影响范围：数据层。\n\n"
        "备选方案：① DuckDB ② JSON 文件\n\n"
        "## 技术栈总览\n\n- Python 3.11\n",
        encoding="utf-8",
    )
    return plan_dir


@pytest.fixture
def plan_dir_with_prd(tmp_path):
    plan_dir = tmp_path / "plans" / "test-prd-add"
    plan_dir.mkdir(parents=True)
    prd = {
        "title": "测试产品",
        "overview": "概述",
        "constraints": [],
        "acceptance": [],
        "decision_points": [
            {
                "title": "目标用户",
                "context": "C",
                "problem": "P",
                "evidence": "E",
                "action": "A",
                "impact": "I",
                "alternatives": ["a"],
                "category": "requirement",
                "index": 0,
            }
        ],
    }
    (plan_dir / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (plan_dir / "interview.json").write_text(
        json.dumps(
            {
                "product_form": {"form": "software"},
                "dimensions": {"background": "B", "goals": "G"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# Markdown adder (arch + test)
# ---------------------------------------------------------------------------


def test_arch_adder_appends_with_consecutive_numbering(plan_dir_with_arch):
    """Existing DP count = 2; new DP must start at index 2 (i.e. 决策点 3)."""
    coding_tool = MagicMock()
    coding_tool.query.return_value = (
        "### 决策点 99: 监控告警策略\n\n"
        "**[C] 背景：** 系统无监控。\n\n"
        "**[P] 问题：** 异常无人感知。\n\n"
        "**[E] 评估：** 用 logging + 定期巡检。\n\n"
        "**[A] 行动：** 接入 stdout JSON 日志 + 5 分钟 cron 巡检。\n\n"
        "影响范围：运维。\n\n"
        "备选方案：① 第三方 SaaS ② 不监控\n"
    )

    from decision_point_adder import ArchDecisionPointAdder

    adder = ArchDecisionPointAdder(coding_tool, plan_dir_with_arch)
    result = adder.add("缺少监控告警策略的决策点", count=1)

    assert result["added"] and not result["no_gap_reason"]
    added = result["added"][0]
    assert added.index == 2
    assert added.title == "监控告警策略"

    updated = (plan_dir_with_arch / "arch-design.md").read_text(encoding="utf-8")

    # Heading was renumbered to 决策点 3
    assert "### 决策点 3: 监控告警策略" in updated
    assert "### 决策点 99" not in updated

    # Existing content untouched
    assert "### 决策点 1: 技术栈选型" in updated
    assert "### 决策点 2: 数据存储" in updated

    # Inserted BEFORE 技术栈总览 so it stays inside 决策点列表 section
    pos_dp3 = updated.index("### 决策点 3: 监控告警策略")
    pos_tech = updated.index("## 技术栈总览")
    assert pos_dp3 < pos_tech

    # New DP is the last DP before 技术栈总览
    tech_section_start = pos_tech
    dp_section = updated[:tech_section_start]
    assert dp_section.count("### 决策点") == 3


def test_arch_adder_no_gap_short_circuits(plan_dir_with_arch):
    """When LLM says the gap is already covered, no write happens."""
    coding_tool = MagicMock()
    coding_tool.query.return_value = (
        "NO_GAP\n已被决策点 1 覆盖（技术栈选型已经含 logging 相关决策）。"
    )

    from decision_point_adder import ArchDecisionPointAdder

    original = (plan_dir_with_arch / "arch-design.md").read_text(encoding="utf-8")
    adder = ArchDecisionPointAdder(coding_tool, plan_dir_with_arch)
    result = adder.add("用 Python 3.11", count=1)

    assert result["added"] == []
    assert "决策点 1" in (result["no_gap_reason"] or "")
    # File untouched
    assert (plan_dir_with_arch / "arch-design.md").read_text(encoding="utf-8") == original


def test_arch_adder_prompt_includes_existing_dps_and_upstream(plan_dir_with_arch):
    """The LLM must see existing DPs + upstream PRD context (or empty)."""
    coding_tool = MagicMock()
    coding_tool.query.return_value = "### 决策点 3: X\n\n**[A] 行动：** Y\n"

    from decision_point_adder import ArchDecisionPointAdder

    adder = ArchDecisionPointAdder(coding_tool, plan_dir_with_arch)
    adder.add("缺口", count=1)

    prompt = coding_tool.query.call_args.kwargs["prompt"]
    assert "技术栈选型" in prompt
    assert "数据存储" in prompt
    assert "缺口" in prompt
    assert "禁止重复生成" in prompt
    assert "NO_GAP" in prompt


def test_arch_adder_respects_skipped_prd(plan_dir_with_arch):
    """If a PRD DP was skipped, the adder surfaces that as a hard constraint."""
    (plan_dir_with_arch / "review.json").write_text(
        json.dumps(
            {
                "items": [
                    {"index": 0, "title": "重生策略", "status": "skipped", "note": ""}
                ]
            }
        ),
        encoding="utf-8",
    )
    coding_tool = MagicMock()
    coding_tool.query.return_value = "### 决策点 3: Y\n\n**[A] 行动：** Z\n"

    from decision_point_adder import ArchDecisionPointAdder

    adder = ArchDecisionPointAdder(coding_tool, plan_dir_with_arch)
    adder.add("缺口", count=1)
    prompt = coding_tool.query.call_args.kwargs["prompt"]
    assert "SKIP" in prompt
    assert "重生策略" in prompt


def test_arch_adder_rejects_count_out_of_range(plan_dir_with_arch):
    from decision_point_adder import ArchDecisionPointAdder

    coding_tool = MagicMock()
    adder = ArchDecisionPointAdder(coding_tool, plan_dir_with_arch)
    with pytest.raises(ValueError):
        adder.add("x", count=0)
    with pytest.raises(ValueError):
        adder.add("x", count=10)
    coding_tool.query.assert_not_called()


def test_arch_adder_rejects_empty_requirement(plan_dir_with_arch):
    from decision_point_adder import ArchDecisionPointAdder

    coding_tool = MagicMock()
    adder = ArchDecisionPointAdder(coding_tool, plan_dir_with_arch)
    with pytest.raises(ValueError):
        adder.add("", count=1)
    coding_tool.query.assert_not_called()


# ---------------------------------------------------------------------------
# PRD JSON adder
# ---------------------------------------------------------------------------


def test_prd_adder_appends_with_consecutive_index(plan_dir_with_prd):
    coding_tool = MagicMock()
    coding_tool.query.return_value = json.dumps(
        [
            {
                "title": "验收标准",
                "context": "C",
                "problem": "P",
                "evidence": "E",
                "action": "A",
                "impact": "I",
                "alternatives": [],
                "category": "acceptance_criterion",
            }
        ],
        ensure_ascii=False,
    )

    from decision_point_adder import PRDDecisionPointAdder

    adder = PRDDecisionPointAdder(coding_tool, plan_dir_with_prd)
    result = adder.add("补一条验收标准", count=1)

    assert result["added"] and not result["no_gap_reason"]
    added = result["added"][0]
    assert added.index == 1
    assert added.title == "验收标准"

    prd = json.loads((plan_dir_with_prd / "prd.json").read_text(encoding="utf-8"))
    assert len(prd["decision_points"]) == 2
    # Existing DP untouched (still index 0, title 目标用户)
    assert prd["decision_points"][0]["title"] == "目标用户"
    assert prd["decision_points"][0]["index"] == 0
    # New DP got the next index
    assert prd["decision_points"][1]["title"] == "验收标准"
    assert prd["decision_points"][1]["index"] == 1


def test_prd_adder_emits_category_warning(plan_dir_with_prd):
    coding_tool = MagicMock()
    # ``tech_stack`` is NOT in PRD_ALLOWED_CATEGORIES for product_form=software
    coding_tool.query.return_value = json.dumps(
        [
            {
                "title": "技术栈",
                "context": "C",
                "problem": "P",
                "evidence": "E",
                "action": "A",
                "impact": "I",
                "alternatives": [],
                "category": "tech_stack",
            }
        ],
        ensure_ascii=False,
    )
    from decision_point_adder import PRDDecisionPointAdder

    adder = PRDDecisionPointAdder(coding_tool, plan_dir_with_prd)
    result = adder.add("技术选型决策点", count=1)
    assert result["added"]
    assert any("tech_stack" in w for w in result["warnings"])


def test_prd_adder_handles_legacy_markdown_fenced_json(plan_dir_with_prd):
    coding_tool = MagicMock()
    coding_tool.query.return_value = (
        "```json\n"
        + json.dumps(
            [
                {
                    "title": "范围边界",
                    "context": "C",
                    "problem": "P",
                    "evidence": "E",
                    "action": "A",
                    "impact": "I",
                    "alternatives": [],
                    "category": "scope_boundary",
                }
            ],
            ensure_ascii=False,
        )
        + "\n```"
    )

    from decision_point_adder import PRDDecisionPointAdder

    adder = PRDDecisionPointAdder(coding_tool, plan_dir_with_prd)
    result = adder.add("范围", count=1)
    assert result["added"]
    assert result["added"][0].title == "范围边界"


def test_prd_adder_no_gap_short_circuits(plan_dir_with_prd):
    coding_tool = MagicMock()
    coding_tool.query.return_value = "NO_GAP\n用户已经覆盖。"

    from decision_point_adder import PRDDecisionPointAdder

    original = (plan_dir_with_prd / "prd.json").read_text(encoding="utf-8")
    adder = PRDDecisionPointAdder(coding_tool, plan_dir_with_prd)
    result = adder.add("x", count=1)

    assert result["added"] == []
    assert result["no_gap_reason"]
    assert (plan_dir_with_prd / "prd.json").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Test design adder (markdown path — same shape as arch)
# ---------------------------------------------------------------------------


def test_test_adder_appends_with_consecutive_numbering(tmp_path):
    plan_dir = tmp_path / "p"
    plan_dir.mkdir()
    (plan_dir / "arch-design.md").write_text(
        "# Architecture\n\n## 决策点列表\n\n"
        "### 决策点 1: A\n\n**[A] 行动：** 1\n",
        encoding="utf-8",
    )
    test_file = plan_dir / "test-design.md"
    test_file.write_text(
        "# 测试设计\n\n## 概述\n\nX\n\n## 测试策略决策点列表\n\n"
        "### 决策点 1: 单元测试\n\n**[A] 行动：** 用 pytest。\n\n"
        "## 测试矩阵\n\n- 模块 x 单元测试\n",
        encoding="utf-8",
    )

    coding_tool = MagicMock()
    coding_tool.query.return_value = (
        "### 决策点 99: E2E 测试\n\n"
        "**[C] 背景：** 端到端未覆盖。\n\n"
        "**[P] 问题：** 缺 e2e。\n\n"
        "**[E] 评估：** 用 puppeteer。\n\n"
        "**[A] 行动：** 引入 puppeteer e2e。\n\n"
        "测试类型：E2E 测试\n覆盖范围：全部\n备选方案：① playwright ② 跳过\n"
    )

    from decision_point_adder import TestDecisionPointAdder

    adder = TestDecisionPointAdder(coding_tool, plan_dir)
    result = adder.add("缺 E2E 测试策略", count=1)
    assert result["added"]
    assert result["added"][0].index == 1

    updated = test_file.read_text(encoding="utf-8")
    assert "### 决策点 2: E2E 测试" in updated
    pos_new = updated.index("### 决策点 2: E2E 测试")
    pos_matrix = updated.index("## 测试矩阵")
    assert pos_new < pos_matrix


# ---------------------------------------------------------------------------
# register_new_items on the three reviewers
# ---------------------------------------------------------------------------


def test_arch_reviewer_register_new_items_only_appends(plan_dir_with_arch):
    """If a DP with the same index already exists in review.json,
    register_new_items must NOT clobber its status."""
    from arch_reviewer import ArchReviewer

    (plan_dir_with_arch / "arch-review.json").write_text(
        json.dumps(
            {
                "items": [
                    {"index": 0, "title": "技术栈", "status": "accepted", "note": ""},
                    {"index": 1, "title": "数据存储", "status": "pending", "note": ""},
                ],
                "total": 2,
                "accepted": 1,
                "skipped": 0,
            }
        ),
        encoding="utf-8",
    )
    coding_tool = MagicMock()
    reviewer = ArchReviewer(coding_tool, plan_dir_with_arch)

    reviewer.register_new_items(
        [
            {"index": 1, "title": "dup"},  # should be ignored (status preserved)
            {"index": 2, "title": "新 DP"},  # should be added as pending
        ]
    )

    review = json.loads(
        (plan_dir_with_arch / "arch-review.json").read_text(encoding="utf-8")
    )
    indices = {it["index"]: it for it in review["items"]}
    assert indices[0]["status"] == "accepted"
    assert indices[1]["status"] == "pending"  # preserved, not clobbered
    assert indices[2]["status"] == "pending"
    assert indices[2]["title"] == "新 DP"
    assert review["total"] == 3