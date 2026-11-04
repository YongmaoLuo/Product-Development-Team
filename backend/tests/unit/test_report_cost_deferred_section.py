"""报告里「因成本跳过（DEFERRED）」小节的契约测试。

背景
----
成本闸（缺陷 B 第 4 条）把「执行这个验证点会让被验证产品真的调用 provider」
的验证点默认挡下，记一条 ``DEFERRED`` 裁决 —— 它**没跑**，所以它不是失败。

``DEFERRED`` 与 ``FAILED`` 的区别，操作者必须一眼看出来：「没花钱」不是
「跑挂了」。混进失败清单，值班的人就得逐条翻日志才发现这条根本没跑。

所以报告必须**单列**一节放这些验证点，并且守住四条：

  * 一条 DEFERRED → 有自己的一节，表格里能看到是哪个 VP、什么成本、
    为什么跳过；
  * DEFERRED **不得**出现在失败 / 未通过的地方；
  * 没有 DEFERRED → 整节省略（旧报告逐字节不变）；
  * 原因里的 ``|`` 与换行不得把表格拆散（表格仍可解析）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_report_md import render  # noqa: E402


#: 这一节在报告里的标题。
_SECTION_TITLE = "## 因成本跳过（DEFERRED）"

#: 表格里表示「这条算失败」的标记。
_FAILURE_MARKERS = ("失败", "未通过", "FAILED", "failed")


def _cells(line: str):
    """按**未转义**的竖线切分一行表格。

    ``\\|`` 是被转义过的竖线，不是分隔符 —— 用普通 ``split("|")`` 切会把
    一个单元格里带竖线的理由数成两个单元格，表格「坏了」也就测不出来。
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


def _section(markdown: str, title: str) -> str:
    """取某一节的内容（标题行之后、下一个标题之前）。"""
    body = markdown.split(title, 1)[1]
    return body.split("\n## ", 1)[0]


def test_deferred_gets_its_own_section():
    """一条 DEFERRED → 有自己的一节，表格里写清 VP、成本与原因。"""
    report = {
        "results": [
            {
                "id": "VP-003",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "reasons": ["cost: llm_quota"],
            },
            {"id": "VP-004", "status": "FAILED", "reasons": ["boom"]},
        ]
    }

    markdown = render(report)

    assert _SECTION_TITLE in markdown

    section = _section(markdown, _SECTION_TITLE)
    rows = [line for line in section.splitlines() if line.startswith("| VP-003")]
    assert len(rows) == 1
    assert _cells(rows[0]) == ["VP-003", "llm_quota", "cost: llm_quota"]


def test_deferred_not_listed_as_failure():
    """DEFERRED 不得出现在失败 / 未通过的地方：「没花钱」不是「跑挂了」。"""
    report = {
        "final_verdict": "failed",
        "verification_points": [
            {"id": "VP-002", "plan_item_id": "ITEM-2", "status": "failed"},
            {
                "id": "VP-003",
                "plan_item_id": "ITEM-3",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "reasons": ["cost: llm_quota"],
            },
        ],
    }

    markdown = render(report)

    failure_rows = [
        line
        for line in _table_lines(markdown)
        if any(marker in line for marker in _FAILURE_MARKERS)
    ]
    # 前提：确实有一条失败行（否则这条断言是空的）。
    assert failure_rows
    assert not any("VP-003" in line for line in failure_rows)

    # DEFERRED 的那条在验证点表里写的是 DEFERRED，不是「失败」。
    vp_section = _section(markdown, "## 验证点结果")
    vp_rows = [line for line in vp_section.splitlines() if line.startswith("| VP-003")]
    assert len(vp_rows) == 1
    assert "失败" not in vp_rows[0]
    assert "DEFERRED" in vp_rows[0]

    # 它仍然有一节自己的，跟失败清单分开列。
    assert _SECTION_TITLE in markdown


def test_no_deferred_no_section():
    """没有 DEFERRED → 整节省略（旧报告逐字节不变）。"""
    report = {
        "final_verdict": "passed",
        "verification_points": [
            {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
            {"id": "VP-002", "plan_item_id": "ITEM-2", "status": "passed"},
        ],
    }

    markdown = render(report)

    assert "因成本跳过" not in markdown
    assert "DEFERRED" not in markdown
    # 既有小节照旧在。
    assert "## 最终结论" in markdown
    assert "## 验证点结果" in markdown


def test_pipe_in_reason_is_escaped():
    """原因里的 ``|`` 与换行不得把表格拆散。"""
    report = {
        "results": [
            {
                "id": "VP-003",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "reasons": ["cost: a | b\nsecond line"],
            }
        ]
    }

    markdown = render(report)

    section = _section(markdown, _SECTION_TITLE)
    rows = [line for line in section.splitlines() if line.startswith("| VP-003")]
    assert len(rows) == 1
    row = rows[0]

    # 三列：一列都没多（竖线被转义了）。
    assert len(_cells(row)) == 3
    assert _cells(row)[0] == "VP-003"
    assert _cells(row)[1] == "llm_quota"

    # 换行被转义成字面的 ``\n``，没有把这一行拆成两行。
    assert "cost: a \\| b\\nsecond line" in row
    assert len([line for line in section.splitlines() if line.startswith("|")]) == 3
