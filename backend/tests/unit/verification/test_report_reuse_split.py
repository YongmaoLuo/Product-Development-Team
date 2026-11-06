"""账本与报告分列「本轮真跑」与「复用」（2026-11-05）。

背景
----
一条结论是「**这一次**复核的结论」：默认每轮真跑，四条判据全真才复用
（判据内核在 :mod:`verification_round_scope`，执行器按它复用）。复用是例外，
而例外必须**说得出凭什么** —— 账本里那条 ``reused_from_round`` 就是凭什么。

没有它，报告里「本轮真跑」与「复用上一轮的结论」长得一模一样：一份 27 条
全绿的报告可能只有 7 个验证点真的跑过，读者无从分辨。预检派发的修复同理 ——
它是**接续**根因链的活，不是这一轮的修复，混在主表里就看不出这一轮到底
做了什么。

本文件钉住三条：

1. ``test_execution_results_carry_reused_from_round`` — 账本带
   ``reused_from_round``，历史轮的结论不进账本。
2. ``test_report_splits_reused_rows`` — 报告把「本轮真跑」与「复用」分开列。
3. ``test_preflight_repairs_listed_separately`` — 预检派发的修复单列成节，
   不重复出现在主表。

测试命令::

    cd backend && python3 -m pytest tests/unit/verification/test_report_reuse_split.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

# ``backend/`` 进 sys.path，``import verification_agent`` 才成立 ——
# 与同目录其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import verification_agent  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402
from verification_report_md import render  # noqa: E402


pytestmark = pytest.mark.unit

#: 复用那节的标题（复用的结论单列，不混在「本轮真跑」里）。
REUSE_SECTION_TITLE = "复用"

#: 预检接续那节的标题（预检派发的修复单列，不重复出现在主表）。
PREFLIGHT_SECTION_TITLE = "预检接续"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _agent() -> VerificationAgent:
    """一个不做任何持久化的代理（账本只需要它投影裁决）。"""
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None
    return agent


class _StubExecutor:
    """只回答账本要问的那几个问题的执行器替身。"""

    def __init__(self, verdicts: List[Dict[str, Any]], round_no: int = 0):
        self._verdicts = [dict(v) for v in verdicts]
        self._round_no = round_no

    def collect_verdicts(self) -> List[Dict[str, Any]]:
        return [dict(v) for v in self._verdicts]

    def _plan_retired_vps(self) -> Dict[str, Any]:
        # 计划没有作废任何验证点。
        return {}

    def _verdict_round_no(self) -> int:
        return self._round_no


def _verdict(
    vp_id: str,
    *,
    status: str = "PASSED",
    round_no: int = 2,
    reused_from: Optional[int] = None,
) -> Dict[str, Any]:
    """一条裁决。"""
    entry: Dict[str, Any] = {
        "vp_id": vp_id,
        "status": status,
        "round": round_no,
        "reasons": ["ok"],
        "evidence": {},
    }
    if reused_from is not None:
        entry["reused_from_round"] = reused_from
    return entry


def _cells(line: str) -> List[str]:
    """按**未转义**的竖线切分一行表格。"""
    cells: List[str] = []
    current: List[str] = []
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


def _rows(section: str, prefix: str) -> List[str]:
    """某一节里以 ``prefix`` 开头的表格行。"""
    return [line for line in section.splitlines() if line.startswith(prefix)]


# ---------------------------------------------------------------------------
# 1. 账本带 reused_from_round
# ---------------------------------------------------------------------------


def test_execution_results_carry_reused_from_round() -> None:
    """复用项带 ``reused_from_round`` —— 账本据此把真跑与复用分开列。

    本轮真跑的验证点**没有**这个标记（``None``）：缺标记就是本轮真跑，
    报告不该替它猜一个来源轮次。
    """
    executor = _StubExecutor(
        [
            _verdict("VP-001", round_no=2),
            _verdict("VP-002", round_no=2, reused_from=1),
        ],
        round_no=2,
    )

    envelope = _agent()._build_execution_results_from_executor({}, executor)

    by_id = {r["id"]: r for r in envelope["execution_results"]}
    assert by_id["VP-002"]["reused_from_round"] == 1
    assert by_id["VP-001"]["reused_from_round"] is None


def test_ledger_keeps_this_round_only() -> None:
    """历史轮的结论不进账本 —— 同一个 VP 的多轮记录只取本轮那条。

    上一轮的 PASSED 混进账本，报告就分不清「这一轮跑过」与「上一轮跑过」。
    """
    executor = _StubExecutor(
        [
            _verdict("VP-001", round_no=2),
            _verdict("VP-002", round_no=1),  # 上一轮的结论
        ],
        round_no=2,
    )

    envelope = _agent()._build_execution_results_from_executor({}, executor)

    assert [r["id"] for r in envelope["execution_results"]] == ["VP-001"]


def test_ledger_keeps_verdicts_when_round_is_unknown() -> None:
    """轮次未知时**不清空**账本 —— 不知道轮次不等于没有结论。

    判据答不上来时的安全一侧是「照旧」，不是「把这一轮的结果全丢掉」。
    """
    executor = _StubExecutor([_verdict("VP-001", round_no=0)], round_no=0)

    envelope = _agent()._build_execution_results_from_executor({}, executor)

    assert [r["id"] for r in envelope["execution_results"]] == ["VP-001"]


# ---------------------------------------------------------------------------
# 2. 报告把真跑与复用分开列
# ---------------------------------------------------------------------------


def test_report_splits_reused_rows() -> None:
    """复用来的结论单列成节，不混在「本轮真跑」的表里。"""
    report = {
        "final_verdict": "passed",
        "verification_points": [
            {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
            {
                "id": "VP-002",
                "plan_item_id": "ITEM-2",
                "status": "passed",
                "reused_from_round": 1,
            },
        ],
    }

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    fresh = _section(markdown, "## 验证点结果")
    reused = _section(markdown, "## " + REUSE_SECTION_TITLE)

    # 本轮真跑的那条在主表里。
    assert len(_rows(fresh, "| VP-001")) == 1
    # 复用的那条**不在**主表里。
    assert not _rows(fresh, "| VP-002")
    # 它在复用那一节里，并写明来自第几轮。
    rows = _rows(reused, "| VP-002")
    assert len(rows) == 1
    assert _cells(rows[0]) == ["VP-002", "通过", "复用自第 1 轮"]


def test_missing_reuse_marker_is_a_fresh_run() -> None:
    """``reused_from_round`` 缺失 → 视为本轮真跑（不猜来源轮次）。"""
    report = {
        "final_verdict": "passed",
        "verification_points": [
            {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
        ],
    }

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    assert len(_rows(_section(markdown, "## 验证点结果"), "| VP-001")) == 1
    assert "## " + REUSE_SECTION_TITLE not in markdown


def test_empty_results_render_empty_table() -> None:
    """没有结果 → 空表，不报错，也不编出行。"""
    markdown = render({"final_verdict": "passed"})

    assert "## 验证点结果" in markdown
    assert "## " + REUSE_SECTION_TITLE not in markdown
    table_lines = [line for line in markdown.splitlines() if line.startswith("|")]
    assert len(table_lines) == 2  # 表头 + 分隔行


def test_report_backfills_reuse_marker_from_ledger() -> None:
    """报告的条目带上账本的复用标记（判断阶段的 LLM 看不到复用）。"""
    report_data = {
        "overall_status": "PASSED",
        "verification_results": [
            {"id": "VP-001", "status": "PASSED"},
            {"id": "VP-002", "status": "PASSED"},
        ],
    }
    execution_results = {
        "execution_results": [
            {"id": "VP-001", "status": "PASSED", "reused_from_round": None},
            {"id": "VP-002", "status": "PASSED", "reused_from_round": 1},
        ],
    }

    updated = _agent()._backfill_reuse_markers(report_data, execution_results)

    entries = {e["id"]: e for e in updated["verification_results"]}
    assert entries["VP-002"]["reused_from_round"] == 1
    assert entries["VP-001"]["reused_from_round"] is None


# ---------------------------------------------------------------------------
# 3. 预检派发的修复单列
# ---------------------------------------------------------------------------


def test_preflight_repairs_listed_separately() -> None:
    """``origin == "root_cause_preflight"`` 的任务单列，不重复出现在主表。"""
    report = {"final_verdict": "failed"}
    repair_rows: List[Mapping[str, Any]] = [
        {
            "task_id": "repair-9",
            "vp_id": "VP-002",
            "status": "pending",
            "origin": "root_cause_preflight",
            "report_path": "/plans/p/reports/root-cause-VP-002.md",
            "files_to_modify": ["backend/verification/orchestrator.py"],
        },
        {"task_id": "repair-3", "vp_id": "VP-001", "status": "pending"},
    ]

    markdown = render(report, plan_items={}, review_state={}, repair_rows=repair_rows)

    preflight = _section(markdown, "## " + PREFLIGHT_SECTION_TITLE)
    repairs = _section(markdown, "## 修复任务")

    # 预检派发的那条在预检接续这一节里。
    rows = _rows(preflight, "| repair-9")
    assert len(rows) == 1
    # 不重复出现在主表里。
    assert not _rows(repairs, "| repair-9")
    # 普通修复任务照旧在主表里（``origin`` 是可选字段，缺失即普通任务）。
    assert len(_rows(repairs, "| repair-3")) == 1


def test_preflight_row_carries_report_path_and_scope_only() -> None:
    """预检那节只写报告路径与改动范围，不内联报告正文。"""
    report = {"final_verdict": "failed"}
    repair_rows: List[Mapping[str, Any]] = [
        {
            "task_id": "repair-9",
            "vp_id": "VP-002",
            "status": "pending",
            "origin": "root_cause_preflight",
            "report_path": "/plans/p/reports/root-cause-VP-002.md",
            "files_to_modify": ["backend/verification/orchestrator.py"],
            "description": "根因正文：执行器把上一轮的结论当本轮的用了",
        },
    ]

    markdown = render(report, plan_items={}, review_state={}, repair_rows=repair_rows)

    preflight = _section(markdown, "## " + PREFLIGHT_SECTION_TITLE)
    row = _rows(preflight, "| repair-9")[0]
    cells = _cells(row)

    assert "root-cause-VP-002.md" in cells[2]
    assert "backend/verification/orchestrator.py" in cells[3]
    # 报告正文不进报告。
    assert "根因正文" not in preflight


def test_report_preflight_repairs_section_reads_report_field() -> None:
    """报告里另有一份预检清单时（执行侧写的），它也进预检接续这一节。"""
    report = {
        "final_verdict": "failed",
        "preflight_repairs": [
            {
                "id": "repair-11",
                "vp_id": "VP-004",
                "report_path": "/plans/p/reports/root-cause-VP-004.md",
                "scope": ["backend/verification_agent.py"],
            },
        ],
    }

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    preflight = _section(markdown, "## " + PREFLIGHT_SECTION_TITLE)
    rows = _rows(preflight, "| repair-11")
    assert len(rows) == 1
    assert "root-cause-VP-004.md" in _cells(rows[0])[2]


# ---------------------------------------------------------------------------
# 4. 库里那一行丢了 origin —— 凭根因报告的地址认出链上的活
# ---------------------------------------------------------------------------


def test_root_cause_task_detected_by_report_reference() -> None:
    """库里那一行没有 ``origin`` —— 凭它指向哪份根因报告认出它是链上的活。

    ``origin`` 在持久化那一步被丢掉了（静态字段白名单里没有它），所以
    库里的行只能凭**自己指向哪份根因报告**认出来 —— 报告文件名是
    ``root-cause-<vp>.md``，这个名字由根因任务那一份算出来，不写第二遍。
    """
    row = {
        "id": "repair-9",
        "title": "实现 VP-002 的根因修复方案",
        "description": (
            "按 plans/p/reports/root-cause-VP-002.md 的根因结论修复 VP-002。"
        ),
        "failed_vp_id": "VP-002",
        "files_to_modify": ["backend/verification/orchestrator.py"],
    }

    assert _agent()._is_root_cause_task(row, "p") is True


def test_ordinary_repair_row_is_not_a_root_cause_task() -> None:
    """普通修复任务不是链上的活 —— 没有根因报告就只是普通任务。"""
    row = {
        "id": "repair-3",
        "title": "修 VP-001",
        "description": "照着失败日志修",
        "failed_vp_id": "VP-001",
        "files_to_modify": ["backend/app/service.py"],
    }

    assert _agent()._is_root_cause_task(row, "p") is False


def test_preflight_repairs_come_from_the_plan_rows(monkeypatch) -> None:
    """预检接续那节的内容来自库里那一行：任务 id、验证点、报告地址、范围。

    只写这四样 —— 根因报告的**正文**不进报告（正文是那份文件的事）。
    """
    rows = {
        "repair-9": {
            "id": "repair-9",
            "title": "实现 VP-002 的根因修复方案",
            "description": (
                "按 plans/p/reports/root-cause-VP-002.md 的根因结论修复 VP-002。"
            ),
            "failed_vp_id": "VP-002",
            "files_to_modify": ["backend/verification/orchestrator.py"],
        },
        "repair-3": {
            "id": "repair-3",
            "title": "修 VP-001",
            "description": "照着失败日志修",
            "failed_vp_id": "VP-001",
            "files_to_modify": ["backend/app/service.py"],
        },
    }
    monkeypatch.setattr(
        verification_agent,
        "_read_plan_task_rows",
        lambda plan_id, **kwargs: (rows, None),
    )

    agent = _agent()
    agent.plan_dir = Path("/plans/p")

    repairs = agent._preflight_repairs()

    assert len(repairs) == 1, f"只有链上的活进这一节: {repairs!r}"
    entry = repairs[0]
    assert entry["id"] == "repair-9"
    assert entry["vp_id"] == "VP-002"
    assert entry["report_path"].endswith("root-cause-VP-002.md")
    assert entry["scope"] == ["backend/verification/orchestrator.py"]
