"""``verification_report.json`` → Markdown：确定性渲染的契约测试。

背景
----
``verification_report.json`` 是验证结论的**唯一数据源**，Markdown 只是它的
派生视图。所以这个模块的价值全在四条性质上，逐条钉死：

  * **确定性** —— 同一份入参渲染两次，字节相同；
  * **顺序无关** —— 验证点 / 修复行的顺序打乱，输出字节相同；
  * **注入免疫** —— 标题里的 ``|``、换行、HTML 标签不得被解释，表格不得
    被拆散；
  * **skip 语义** —— 跳过的验证点显示「未验证」，**不显示**「通过 / 失败」。

最后一条是语义而不是排版：没验过就是没验过，写成「通过」会让整份报告
在读者眼里变成「全绿」。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_report_md  # noqa: E402
from verification_report_md import render  # noqa: E402


_PLAN_ITEMS = {
    "ITEM-1": "登录接口",
    "ITEM-2": {"title": "退出接口"},
}

_REVIEW_STATE = {
    "ITEM-1": "approved",
    "ITEM-2": {"status": "revised", "note": "补一条用例"},
}

_REPAIR_ROWS = [
    {"task_id": "RT-002", "vp_id": "VP-002", "status": "pending", "title": "修退出接口"},
    {"task_id": "RT-001", "vp_id": "VP-001", "status": "pending", "title": "修登录接口"},
]


def _report(points=None, **overrides):
    """一份典型的报告：两个验证点，一个通过一个失败。"""
    report = {
        "final_verdict": "passed",
        "verification_points": [
            {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
            {"id": "VP-002", "plan_item_id": "ITEM-2", "status": "failed"},
        ],
    }
    if points is not None:
        report["verification_points"] = points
    report.update(overrides)
    return report


def _cells(line: str):
    """按**未转义**的竖线切分一行表格。

    ``\\|`` 是被转义过的竖线，不是分隔符 —— 用普通 ``split("|")`` 切会把
    一个单元格里带竖线的标题数成两个单元格，表格「坏了」也就测不出来。
    """
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    # 首尾各有一个分隔符产生的空串，去掉。
    return cells[1:-1]


def _table_lines(markdown: str):
    """报告里所有表格行（含表头与分隔行）。"""
    return [line for line in markdown.splitlines() if line.startswith("|")]


# ---------------------------------------------------------------------------
# 契约 1：确定性
# ---------------------------------------------------------------------------


def test_render_is_deterministic():
    """同一份入参渲染两次，字节相同。"""
    report = _report()

    first = render(
        report,
        plan_items=dict(_PLAN_ITEMS),
        review_state=dict(_REVIEW_STATE),
        repair_rows=[dict(row) for row in _REPAIR_ROWS],
    )
    second = render(
        report,
        plan_items=dict(_PLAN_ITEMS),
        review_state=dict(_REVIEW_STATE),
        repair_rows=[dict(row) for row in _REPAIR_ROWS],
    )

    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")


def test_render_does_not_mutate_inputs():
    """渲染是只读的：入参渲染前后一模一样。"""
    report = _report()
    plan_items = dict(_PLAN_ITEMS)
    review_state = dict(_REVIEW_STATE)
    repair_rows = [dict(row) for row in _REPAIR_ROWS]

    before = (
        copy.deepcopy(report),
        copy.deepcopy(plan_items),
        copy.deepcopy(review_state),
        copy.deepcopy(repair_rows),
    )
    render(report, plan_items, review_state, repair_rows)

    assert (report, plan_items, review_state, repair_rows) == before


# ---------------------------------------------------------------------------
# 契约 2：顺序无关
# ---------------------------------------------------------------------------


def test_input_order_does_not_matter():
    """验证点 / 修复行的顺序打乱，输出字节相同。"""
    report = _report()
    shuffled_report = _report(points=list(reversed(report["verification_points"])))

    baseline = render(
        report,
        plan_items=dict(_PLAN_ITEMS),
        review_state=dict(_REVIEW_STATE),
        repair_rows=[dict(row) for row in _REPAIR_ROWS],
    )
    shuffled = render(
        shuffled_report,
        plan_items=dict(_PLAN_ITEMS),
        review_state=dict(_REVIEW_STATE),
        repair_rows=[dict(row) for row in reversed(_REPAIR_ROWS)],
    )

    assert baseline == shuffled
    assert baseline.encode("utf-8") == shuffled.encode("utf-8")


def test_verification_points_are_sorted_by_vp_id():
    """验证点按 VP id 排序，不按入参顺序。"""
    report = _report(
        points=[
            {"id": "VP-010", "plan_item_id": "ITEM-1", "status": "passed"},
            {"id": "VP-002", "plan_item_id": "ITEM-2", "status": "failed"},
            {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
        ]
    )

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    order = [line.split("|")[1].strip() for line in _table_lines(markdown)[2:]]
    assert order == ["VP-001", "VP-002", "VP-010"]


# ---------------------------------------------------------------------------
# 契约 3：注入免疫
# ---------------------------------------------------------------------------


def test_markdown_injection_is_escaped():
    """标题里的 ``|`` / 换行 / HTML 标签不得被解释，表格不得被拆散。"""
    nasty = "a | b\n<script>alert('x')</script> **bold**"
    report = _report(
        points=[
            {
                "id": "VP-001",
                "plan_item_id": "ITEM-1",
                "status": "passed",
                "title": nasty,
            }
        ]
    )

    markdown = render(report, plan_items={"ITEM-1": nasty}, review_state={}, repair_rows=[])

    # 原文没有被解释：HTML 标签成了实体，强调标记被转义。
    assert "<script>" not in markdown
    assert "</script>" not in markdown
    assert "**bold**" not in markdown
    assert "&lt;script&gt;" in markdown

    # 竖线被转义，表格没有被拆散：表头 + 分隔行 + 一行数据。
    assert "\\|" in markdown
    assert len(_table_lines(markdown)) == 3
    row = _table_lines(markdown)[2]
    assert len(_cells(row)) == 4
    assert _cells(row)[0] == "VP-001"


def test_escaped_pipe_does_not_add_a_column():
    """一个单元格里的竖线不得让表格多出一列。"""
    report = _report(
        points=[
            {
                "id": "VP-001",
                "plan_item_id": "ITEM-1",
                "status": "passed",
                "title": "左 | 右",
            }
        ]
    )

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    row = _table_lines(markdown)[2]
    assert len(_cells(row)) == 4
    # 竖线仍然是**内容**，不是分隔符。
    assert _cells(row)[-1] == "左 | 右"


# ---------------------------------------------------------------------------
# 契约 4：skip 语义
# ---------------------------------------------------------------------------


def test_skip_rendered_as_unverified():
    """跳过的验证点显示「未验证」，不显示「通过 / 失败」。"""
    report = _report(
        points=[{"id": "VP-001", "plan_item_id": "ITEM-1", "status": "skipped"}],
        final_verdict="skipped",
    )

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    rows = [line for line in _table_lines(markdown) if line.startswith("| VP-001")]
    assert len(rows) == 1
    assert "未验证" in rows[0]
    assert "通过" not in rows[0]
    assert "失败" not in rows[0]
    assert "passed" not in rows[0]
    assert "failed" not in rows[0]

    # 整份报告里也不许出现「通过 / 失败」——最终结论是 skipped。
    assert "通过" not in markdown
    assert "失败" not in markdown


def test_missing_status_is_not_reported_as_pass_or_fail():
    """没有裁决的验证点不算通过，也不算失败。"""
    report = _report(points=[{"id": "VP-001", "plan_item_id": "ITEM-1"}])

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    rows = [line for line in _table_lines(markdown) if line.startswith("| VP-001")]
    assert len(rows) == 1
    assert "通过" not in rows[0]
    assert "失败" not in rows[0]


def test_unknown_status_is_shown_verbatim_not_guessed():
    """看不懂的状态原样显示，不猜成通过 / 失败。"""
    report = _report(
        points=[{"id": "VP-001", "plan_item_id": "ITEM-1", "status": "weird-state"}]
    )

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    assert "weird-state" in markdown
    assert "通过" not in markdown
    assert "失败" not in markdown


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------


def test_malformed_inputs_do_not_crash_or_fabricate():
    """看不懂的入参既不崩，也不编出结论。"""
    report = {
        "verification_points": [
            "not-a-dict",
            {"id": "VP-001", "plan_item_id": "ITEM-1", "status": 42},
        ]
    }

    markdown = render(
        report,
        plan_items={"ITEM-1": {"title": 42, "name": "条目"}},
        review_state={"ITEM-1": 7},
        repair_rows=["not-a-dict"],
    )

    # 没有最终结论就说没有，不兜底成「通过」。
    assert "## 最终结论\n未提供" in markdown
    # 条目 dict 的 title/name 字段都能取到。
    assert "条目" in markdown
    # 状态不是字符串 → 没有结论；不显示通过 / 失败。
    assert "通过" not in markdown
    assert "失败" not in markdown
    # 不是 dict 的行被跳过，不会凭空多出一行。
    vp_rows = [line for line in _table_lines(markdown) if line.startswith("| VP-")]
    assert len(vp_rows) == 1
    assert "## 修复任务" not in markdown


def test_verification_point_without_id_is_rendered_without_fabrication():
    """没有 id 的验证点不编 id 出来。"""
    report = _report(points=[{"status": "passed"}])

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    rows = [line for line in _table_lines(markdown) if line.startswith("| —")]
    assert len(rows) == 1
    assert _cells(rows[0])[0] == "—"


def test_plan_item_title_equal_to_id_is_not_duplicated():
    """标题就是 id 时不重复写一遍。"""
    report = _report(points=[{"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"}])

    markdown = render(report, plan_items={"ITEM-1": "ITEM-1"}, review_state={}, repair_rows=[])

    row = [line for line in _table_lines(markdown) if line.startswith("| VP-001")][0]
    assert _cells(row)[1] == "ITEM-1"


def test_plan_item_title_is_shown_with_its_id():
    """标题与 id 不同时，标题后面跟着 id（能反查回计划书）。"""
    report = _report(points=[{"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"}])

    markdown = render(report, plan_items={"ITEM-1": "登录接口"}, review_state={}, repair_rows=[])

    row = [line for line in _table_lines(markdown) if line.startswith("| VP-001")][0]
    assert _cells(row)[1] == "登录接口（ITEM-1）"


def test_sections_are_rendered_from_all_inputs():
    """最终结论 / 验证点 / 审核状态 / 修复任务四节都在，内容来自入参。"""
    report = _report()

    markdown = render(
        report,
        plan_items=dict(_PLAN_ITEMS),
        review_state=dict(_REVIEW_STATE),
        repair_rows=[dict(row) for row in _REPAIR_ROWS],
    )

    assert markdown.startswith("# 验证报告\n\n## 最终结论\npassed\n\n## 验证点结果\n")
    assert "## 审核状态" in markdown
    assert "## 修复任务" in markdown

    # 计划书条目标题进了表格（dict 形式的条目也能取到 title）。
    assert "登录接口" in markdown
    assert "退出接口" in markdown

    # 审核状态：字符串值与 dict 值都渲染出来，且按键排序。
    assert "approved" in markdown
    assert "revised" in markdown
    assert markdown.index("approved") < markdown.index("revised")

    # 修复行按任务 id 排序（入参里 RT-002 在前）。
    assert markdown.index("RT-001") < markdown.index("RT-002")
    assert "修登录接口" in markdown


def test_empty_optional_sections_are_omitted():
    """没有审核状态 / 修复行时，这两节整节省略。"""
    markdown = render(_report(), plan_items={}, review_state={}, repair_rows=[])

    assert "## 审核状态" not in markdown
    assert "## 修复任务" not in markdown
    assert "## 验证点结果" in markdown


def test_report_without_verification_points():
    """没有验证点时，验证点表只剩表头，不编造行。"""
    report = {"final_verdict": "passed"}

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    assert "## 最终结论\npassed" in markdown
    assert "## 验证点结果" in markdown
    assert len(_table_lines(markdown)) == 2  # 表头 + 分隔行


def test_render_reads_no_clock_environment_or_filesystem():
    """渲染只认入参：不读时钟、不读环境变量、不读文件系统。"""
    source = Path(verification_report_md.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "import os",
        "import time",
        "import datetime",
        "import random",
        "import pathlib",
        "from os",
        "from time",
        "from datetime",
        "from pathlib",
        "os.environ",
        "getenv",
        "datetime.now",
        "time.time",
    ):
        assert forbidden not in source, forbidden