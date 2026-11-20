"""报告 JSON 里「test 模式成本门跳过」清单与计数的契约测试。

背景
----
``verification-report.md`` 已经把 test 模式下被成本门挡下的验证点单列成
一节（任务 8）。但 JSON 报告是**机器读**的那一半 —— 桥接界面、状态接口、
后续轮次的判读都要从 JSON 里知道「哪些验证点是因为 test 模式省下了钱
没跑的」，不该去解析 Markdown。

契约
----
  * ``_normalize_report`` 写出 ``cost_gate_skipped_test_mode``（VP id 清单）
    与 ``cost_gate_skipped_test_mode_count``（计数）；
  * 两个字段**始终存在** —— 没有标记时是 ``[]`` 与 ``0``，读取方不用判空；
  * 同一条 VP 出现在报告的几个字段里 → 去重，按 id 排序；
  * 普通 ``DEFERRED``（没有 ``plan_mode`` 标记）不计入；
  * 判定复用 ``verification_report_md`` 的 ``cost_deferred_entries`` +
    ``is_test_mode_cost_skip``，字段表不重写。

本任务只测 ``_normalize_report()`` 的纯函数行为（传 dict、读返回值）；
落盘组合路径由后续任务覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_persistence import VerificationPersistenceManager  # noqa: E402


def _manager(tmp_path: Path) -> VerificationPersistenceManager:
    """一台只用来归一化报告的管理器。"""
    return VerificationPersistenceManager(tmp_path / "plan")


def _test_mode_entry(vp_id: str = "VP-1") -> dict:
    """一条 test 模式下被成本门挡下的结论。"""
    return {
        "id": vp_id,
        "status": "DEFERRED",
        "cost": "llm_quota",
        "evidence": {"cost": "llm_quota", "plan_mode": "test"},
    }


def test_normalize_writes_skip_list_and_count(tmp_path):
    """一条 test 模式的 DEFERRED → 清单一条、计数 1。"""
    report = {
        "verification_results": [
            {
                "id": "VP-1",
                "status": "DEFERRED",
                "evidence": {"cost": "llm_quota", "plan_mode": "test"},
            }
        ]
    }

    normalized = _manager(tmp_path)._normalize_report(report)

    assert normalized["cost_gate_skipped_test_mode"] == ["VP-1"]
    assert normalized["cost_gate_skipped_test_mode_count"] == 1


def test_dedupes_same_vp_across_fields(tmp_path):
    """同一条 VP 出现在几个字段里 → 去重，并按 id 排序。"""
    report = {
        "verification_results": [_test_mode_entry("VP-1")],
        "results": [_test_mode_entry("VP-1"), _test_mode_entry("VP-2")],
    }

    normalized = _manager(tmp_path)._normalize_report(report)

    assert normalized["cost_gate_skipped_test_mode"] == ["VP-1", "VP-2"]
    assert normalized["cost_gate_skipped_test_mode_count"] == 2


def test_no_marker_yields_empty_and_zero(tmp_path):
    """没有 test 模式标记 → 空清单、零计数，字段仍然存在。"""
    manager = _manager(tmp_path)

    normalized = manager._normalize_report(
        {"verification_results": [{"id": "VP-1", "status": "passed"}]}
    )

    assert normalized["cost_gate_skipped_test_mode"] == []
    assert normalized["cost_gate_skipped_test_mode_count"] == 0

    # 报告里压根没有验证点 → 同样两个字段在，值为空。
    empty = manager._normalize_report({})

    assert empty["cost_gate_skipped_test_mode"] == []
    assert empty["cost_gate_skipped_test_mode_count"] == 0


def test_plain_deferred_not_counted(tmp_path):
    """普通 DEFERRED（没有 plan_mode 标记）不计入 test 模式清单。"""
    report = {
        "verification_results": [
            {"id": "VP-1", "status": "DEFERRED", "cost": "llm_quota"},
            {
                "id": "VP-2",
                "status": "DEFERRED",
                "cost": "llm_quota",
                "evidence": {"cost": "llm_quota", "plan_mode": "production"},
            },
        ]
    }

    normalized = _manager(tmp_path)._normalize_report(report)

    assert normalized["cost_gate_skipped_test_mode"] == []
    assert normalized["cost_gate_skipped_test_mode_count"] == 0
