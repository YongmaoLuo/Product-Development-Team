"""验证点重复检测：命令归一化与「命令相同」判据（2026-11-08, R1）。

契约来源：PDT 生成验证计划后**不做重复检测** —— 一份 import.json 里
VP-022（【全量】Nightly CI）与 VP-025（【云端】整仓门禁）跑的是同一条
``bash scripts/ci_gate.sh``，两条重复的整仓门禁照常出厂，前一条白跑一整轮。

本文件钉住三道缝：

* **归一化**把「写法不同、跑起来一样」的写法合成一个 —— 多余空白、反斜杠
  路径分隔符都不该让两条同一条命令的验证点溜过判重。
* **命令字段一个都不能漏** —— ``test_command`` / ``commands`` /
  ``focused_test_command`` / ``ci_entry`` / ``entry_command``。VP 自
  2026-09-18 起没有 ``test_command``（整仓门禁只落在 ``ci_entry``，command
  类 e2e 只落在 ``entry_command``，见
  ``verification_ci_runner.ENTRY_COMMAND_FIELD``）；漏掉任一个字段，都有
  一类重复检不出来。
* **顺序敏感** —— ``["pytest a"]`` 与 ``["pytest b", "pytest a"]`` 不是同一条
  命令：跑的东西不一样，多出来的那一条有它自己的覆盖面。

本模块是**纯函数**（无 IO、无网络），所以判据本身没有"环境不对"这种失败
模式 —— 红灯只可能来自判据写错了。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_dedup import (  # noqa: E402
    commands_of,
    detect_duplicate_points,
    normalize_command,
)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_collapses_whitespace():
    """首尾空白去掉、连续空白合并成一个。"""
    assert normalize_command(" bash  a   b ") == "bash a b"


def test_normalize_unifies_backslash_separator():
    """反斜杠统一成正斜杠 —— 同一个脚本写成两种分隔符也是同一条命令。"""
    assert normalize_command("bash scripts\\ci_gate.sh") == "bash scripts/ci_gate.sh"


def test_normalize_of_blank_is_empty():
    """归一化后为空 = 没有命令，不参与判重。"""
    assert normalize_command("   ") == ""
    assert normalize_command("") == ""


# ---------------------------------------------------------------------------
# 同命令判重
# ---------------------------------------------------------------------------


def test_identical_ci_entry_forms_one_group():
    """两条整仓门禁跑同一条命令 → 一个组，成员是两条 VP 的 id。"""
    points = [
        {
            "id": "VP-022",
            "title": "【全量】Nightly CI",
            "verification_method": "full_ci",
            "ci_entry": "bash  scripts/ci_gate.sh",
        },
        {
            "id": "VP-025",
            "title": "【云端】整仓门禁",
            "verification_method": "full_ci",
            "ci_entry": " bash scripts/ci_gate.sh ",
        },
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-022", "VP-025"]
    assert groups[0]["reason"] == "identical_command: bash scripts/ci_gate.sh"


def test_command_sequence_order_matters():
    """命令**序列**的顺序是判据的一部分 —— ``["pytest a"]`` 与
    ``["pytest b", "pytest a"]`` 跑的东西不一样，不成组。"""
    points = [
        {"id": "VP-010", "commands": ["pytest a"]},
        {"id": "VP-011", "commands": ["pytest b", "pytest a"]},
    ]

    assert detect_duplicate_points(points) == []


def test_single_command_is_not_a_group():
    """只有一条同命令 → 不成组（没有重复可言）。"""
    points = [
        {"id": "VP-001", "ci_entry": "bash scripts/ci_gate.sh"},
        {"id": "VP-002", "ci_entry": "bash scripts/other_gate.sh"},
    ]

    assert detect_duplicate_points(points) == []


def test_every_command_field_is_covered():
    """五个命令字段一个都不能漏 —— 漏一个，就有一类重复检不出来。"""
    for field in (
        "test_command",
        "commands",
        "focused_test_command",
        "ci_entry",
        "entry_command",
    ):
        points = [
            {"id": "VP-A", field: "pytest tests/unit/test_x.py"},
            {"id": "VP-B", field: "pytest  tests/unit/test_x.py "},
        ]
        groups = detect_duplicate_points(points)
        assert len(groups) == 1, f"{field} 没被计入判重"
        assert groups[0]["member_ids"] == ["VP-A", "VP-B"], field


def test_points_without_commands_do_not_participate():
    """没有命令字段、或归一化后为空 → 不参与判重。"""
    points = [
        {"id": "VP-001", "title": "没有命令的验证点"},
        {"id": "VP-002", "ci_entry": "   "},
        {"id": "VP-003", "commands": []},
    ]

    assert detect_duplicate_points(points) == []


def test_malformed_entries_are_skipped():
    """非 dict 条目、缺 id 的条目跳过，且不抛异常。"""
    points = [
        "not-a-point",
        None,
        42,
        {"title": "缺 id"},
        {"id": "", "ci_entry": "pytest a"},
        {"id": "VP-001", "ci_entry": "pytest a"},
        {"id": "VP-002", "ci_entry": "pytest a"},
    ]

    groups = detect_duplicate_points(points)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["VP-001", "VP-002"]


# ---------------------------------------------------------------------------
# commands_of
# ---------------------------------------------------------------------------


def test_commands_of_returns_normalised_commands():
    """``commands_of`` 给出归一化后的命令序列（空的不算）。"""
    point = {
        "id": "VP-022",
        "ci_entry": "  bash scripts/ci_gate.sh ",
        "test_command": "   ",
    }

    assert commands_of(point) == ("bash scripts/ci_gate.sh",)


def test_commands_of_empty_point_is_empty():
    assert commands_of({"id": "VP-001"}) == ()
