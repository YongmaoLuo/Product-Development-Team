"""报告里「test 模式下因成本门跳过」小节的契约测试。

背景
----
成本闸把「执行它会让被验证产品真的调用 provider」的验证点挡下，记一条
``DEFERRED`` 裁决。``DEFERRED`` 的意思是「这轮没跑」，**不是**「跑挂了」。

但 ``DEFERRED`` 有两种来路，操作者关心的程度完全不同：

  * **test 模式**下被成本门挡下 —— 这轮本来就不该花钱，跳过是**预期**的；
  * 别的来路（运行器自己挡下的、计划侧声明的）—— 需要人看一眼。

两种混在一节里，读报告的人得自己逐条去看 ``evidence`` 才知道哪些是
「test 模式省下来的钱」。所以报告必须**单列**一节放 test 模式的成本门
跳过，并写清有几条。

契约
----
  * 一条 test 模式的 DEFERRED → 有自己的一节，标题带**计数**；
  * 普通「因成本跳过（DEFERRED）」小节**不再**包含 test 模式条目；
    全被排除时那一节整节省略；
  * 没有 test 模式条目 → 那一节整节省略，**旧输出逐字节不变**；
  * 判定只看「状态是 DEFERRED」+「``plan_mode`` 标成 test」
    （条目顶层或 ``evidence`` 里各算一处）；
  * 原因文本里的 ``|`` 与换行照旧转义，表格仍可解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_report_md import (  # noqa: E402
    COST_GATE_TEST_MODE_SECTION_TITLE,
    cost_deferred_entries,
    is_test_mode_cost_skip,
    render,
)

#: 普通「因成本跳过」小节的标题。
_PLAIN_SECTION_TITLE = "## 因成本跳过（DEFERRED）"

#: test 模式成本门跳过小节的标题（带计数）。
_TEST_SECTION_TITLE = "## " + COST_GATE_TEST_MODE_SECTION_TITLE

#: 没有 test 模式条目时，报告必须逐字节等于这个值。
_GOLDEN_REPORT = {
    "final_verdict": "passed",
    "verification_results": [
        {"id": "VP-1", "status": "passed"},
        {
            "id": "VP-2",
            "status": "DEFERRED",
            "cost": "llm_quota",
            "reasons": ["cost: llm_quota"],
            "evidence": {"cost": "llm_quota", "plan_mode": "production"},
        },
    ],
}

_GOLDEN_MARKDOWN = (
    "# 验证报告\n"
    "\n"
    "## 最终结论\n"
    "passed\n"
    "\n"
    "## 验证点结果\n"
    "| VP | 计划书条目 | 状态 | 说明 |\n"
    "| --- | --- | --- | --- |\n"
    "| VP-1 | VP-1 | 通过 | — |\n"
    "| VP-2 | VP-2 | DEFERRED | — |\n"
    "\n"
    "## 因成本跳过（DEFERRED）\n"
    "| VP | 成本 | 原因 |\n"
    "| --- | --- | --- |\n"
    "| VP-2 | llm_quota | cost: llm_quota |\n"
)


def _cells(line: str):
    """按**未转义**的竖线切分一行表格。"""
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


def _section(markdown: str, title: str) -> str:
    """取某一节的内容（标题行之后、下一个标题之前）。"""
    body = markdown.split(title, 1)[1]
    return body.split("\n## ", 1)[0]


def _test_mode_entry(vp_id: str = "VP-1") -> dict:
    """一条 test 模式下被成本门挡下的结论。"""
    return {
        "id": vp_id,
        "status": "DEFERRED",
        "cost": "llm_quota",
        "reasons": ["cost: llm_quota"],
        "evidence": {"cost": "llm_quota", "gate": "llm_quota", "plan_mode": "test"},
    }


def test_test_mode_skip_gets_own_section_with_count():
    """一条 test 模式的 DEFERRED → 有自己的一节，标题带计数。"""
    report = {"verification_results": [_test_mode_entry("VP-1")]}

    markdown = render(report)

    heading = _TEST_SECTION_TITLE + "（共 1 条）"
    assert heading in markdown

    section = _section(markdown, heading)
    rows = [line for line in section.splitlines() if line.startswith("| VP-1")]
    assert len(rows) == 1
    assert _cells(rows[0]) == ["VP-1", "llm_quota", "cost: llm_quota"]


def test_plain_section_excludes_test_mode_skip():
    """普通「因成本跳过」小节不再装 test 模式条目。"""
    report = {
        "verification_results": [
            _test_mode_entry("VP-1"),
            {
                "id": "VP-2",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "reasons": ["cost: llm_quota"],
                "evidence": {"cost": "llm_quota", "plan_mode": "production"},
            },
        ]
    }

    markdown = render(report)

    plain = _section(markdown, _PLAIN_SECTION_TITLE)
    assert "VP-1" not in plain
    assert "VP-2" in plain

    # 全被排除时，普通小节整节省略（空节不携带信息）。
    only_test = render({"verification_results": [_test_mode_entry("VP-1")]})
    assert _PLAIN_SECTION_TITLE not in only_test
    assert _TEST_SECTION_TITLE in only_test


def test_no_test_mode_skip_leaves_output_unchanged():
    """没有 test 模式条目 → 新小节整节省略，旧输出逐字节不变。"""
    markdown = render(_GOLDEN_REPORT)

    assert _TEST_SECTION_TITLE not in markdown
    assert markdown == _GOLDEN_MARKDOWN


def test_predicate_requires_deferred_status():
    """判定要「状态是 DEFERRED」+「模式标成 test」。"""
    # 证据里的标记。
    assert is_test_mode_cost_skip(
        {"id": "VP-1", "status": "DEFERRED", "evidence": {"plan_mode": "test"}}
    )
    # 条目顶层的标记。
    assert is_test_mode_cost_skip({"id": "VP-2", "status": "DEFERRED", "plan_mode": "test"})
    # 不是 DEFERRED → 不是成本门跳过（哪怕模式标成 test）。
    assert not is_test_mode_cost_skip(
        {"id": "VP-3", "status": "PASSED", "evidence": {"plan_mode": "test"}}
    )
    assert not is_test_mode_cost_skip({"id": "VP-4", "status": "passed", "plan_mode": "test"})
    # 模式不是 test → 不是 test 模式跳过。
    assert not is_test_mode_cost_skip(
        {"id": "VP-5", "status": "DEFERRED", "evidence": {"plan_mode": "production"}}
    )
    # 压根没有模式标记 → 不是 test 模式跳过。
    assert not is_test_mode_cost_skip({"id": "VP-6", "status": "DEFERRED"})


def test_cost_deferred_entries_is_public_and_aliased():
    """``cost_deferred_entries`` 是公开名，旧名保留为别名。"""
    import verification_report_md as module

    assert module.cost_deferred_entries is module._cost_deferred_entries

    report = {
        "verification_results": [
            _test_mode_entry("VP-1"),
            {"id": "VP-2", "status": "passed"},
            {"id": "VP-3", "status": "DEFERRED"},
        ]
    }
    entries = cost_deferred_entries(report)
    assert [entry.get("id") for entry in entries] == ["VP-1", "VP-3"]
