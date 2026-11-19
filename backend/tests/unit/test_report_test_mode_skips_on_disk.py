"""落盘后的两份产物：JSON 的清单/计数 与 Markdown 的小节必须说同一件事。

背景
----
``verification_report.json`` 是机器读的那一半（任务 9 派生出
``cost_gate_skipped_test_mode`` 清单与 ``cost_gate_skipped_test_mode_count``
计数），``verification-report.md`` 是人读的那一半（任务 8 单列「test 模式下
因成本门跳过」小节）。

两个任务各自测的都是**纯函数**：任务 9 测 ``_normalize_report()`` 返回的
dict，任务 8 测 ``render()`` 返回的字符串。**落盘那一条组合路径**
（``update_report()`` → 归一化 → 渲染 → 写 JSON → 写 Markdown）当时没有
用例 —— 两处字段名／小节标题说不到一起时，不会有任何测试报警。

契约
----
  * 一次 ``update_report()`` 之后，磁盘上的 JSON 带清单与计数；
  * 磁盘上的 Markdown 同时有 test 模式小节（VP-1）与普通 DEFERRED 小节
    （VP-2），两条不互相串门；
  * JSON 的计数 == Markdown 里 test 模式小节的数据行数；
  * **不 mock 渲染** —— 走真实 ``render``，否则字段名不一致测不出来；
  * 计划目录可以是空的：产物必须由这一次 ``update_report()`` 自己造出来，
    不依赖「先跑过一轮验证」。

本任务是 ``update_report()`` 落盘组合路径的唯一归属；任务 9 只测归一化，
任务 8 只测渲染。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_persistence import VerificationPersistenceManager  # noqa: E402
from verification_report_md import (  # noqa: E402
    COST_GATE_TEST_MODE_SECTION_TITLE,
    render as render_markdown,
)

#: 普通「因成本跳过（DEFERRED）」小节的标题。
_PLAIN_SECTION_TITLE = "## 因成本跳过（DEFERRED）"

#: test 模式成本门跳过小节的标题（带计数）。
_TEST_SECTION_TITLE = "## " + COST_GATE_TEST_MODE_SECTION_TITLE

#: 磁盘上的两份产物。
_JSON_NAME = "verification_report.json"
_MD_NAME = "verification-report.md"


def _report() -> dict:
    """一份同时含 test 模式与普通 DEFERRED 的报告。

    VP-1 是 test 模式下被成本门挡下的（跳过是预期的）；VP-2 是别的来路
    挡下的（人要看一眼）。两条必须落在**不同**的小节里。
    """
    return {
        "final_verdict": "deferred",
        "verification_results": [
            {
                "id": "VP-1",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "reasons": ["cost: llm_quota"],
                "evidence": {"cost": "llm_quota", "plan_mode": "test"},
            },
            {
                "id": "VP-2",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "reasons": ["cost: llm_quota"],
                "evidence": {"cost": "llm_quota", "plan_mode": "production"},
            },
        ],
    }


def _write(tmp_path: Path) -> tuple[Path, Path]:
    """跑一次真实的落盘，返回 JSON 与 Markdown 两条路径。

    计划目录是**空的**：只 new 一台管理器就调 ``update_report()`` —— 不跑
    验证、不起轮次。产物必须由这一次调用自己造出来。
    """
    plan_dir = tmp_path / "plan"
    json_path = plan_dir / _JSON_NAME
    md_path = plan_dir / _MD_NAME

    manager = VerificationPersistenceManager(plan_dir)
    # 落盘之前，产物一个都不在。
    assert not json_path.exists()
    assert not md_path.exists()

    manager.update_report(_report())

    return json_path, md_path


def _section(markdown: str, title: str) -> str:
    """取某一节的内容（标题行之后、下一个标题之前）。"""
    assert title in markdown, f"报告里没有这一节：{title}"
    body = markdown.split(title, 1)[1]
    return body.split("\n## ", 1)[0]


def _rows(section: str) -> list[str]:
    """取一节里的数据行（表格里以 ``| VP-`` 开头的那几行）。"""
    return [line for line in section.splitlines() if line.startswith("| VP-")]


def test_json_on_disk_has_skip_fields(tmp_path):
    """磁盘上的 JSON 带清单与计数，且清单只收 test 模式的那条。"""
    json_path, _ = _write(tmp_path)

    data = json.loads(json_path.read_text(encoding="utf-8"))

    assert data["cost_gate_skipped_test_mode"] == ["VP-1"]
    assert data["cost_gate_skipped_test_mode_count"] == 1


def test_markdown_on_disk_has_both_sections(tmp_path):
    """磁盘上的 Markdown 同时有两节，且两条各归各位。"""
    json_path, md_path = _write(tmp_path)
    markdown = md_path.read_text(encoding="utf-8")

    test_rows = _rows(_section(markdown, _TEST_SECTION_TITLE))
    assert [row.split("|")[1].strip() for row in test_rows] == ["VP-1"]

    plain_rows = _rows(_section(markdown, _PLAIN_SECTION_TITLE))
    assert [row.split("|")[1].strip() for row in plain_rows] == ["VP-2"]

    # 两份产物说的是同一份报告：Markdown 就是磁盘上这份 JSON 的派生视图。
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert markdown == render_markdown(data, repair_rows=data["repair_records"])


def test_count_matches_rendered_rows(tmp_path):
    """JSON 的计数 == Markdown 里 test 模式小节的数据行数。"""
    json_path, md_path = _write(tmp_path)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")

    rows = _rows(_section(markdown, _TEST_SECTION_TITLE))

    assert rows, "test 模式小节必须有数据行，否则计数对不上只是因为都没写"
    assert data["cost_gate_skipped_test_mode_count"] == len(rows)
    # 清单里的 id 与小节里的行也逐条对得上（不只是数量相等）。
    assert data["cost_gate_skipped_test_mode"] == [
        row.split("|")[1].strip() for row in rows
    ]
