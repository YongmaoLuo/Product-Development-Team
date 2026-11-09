"""任务级重复检测：``files_to_modify`` + 测试命令（R4，2026-11-09）。

契约来源：需求 R4「接进任务生成」—— 同一套规则用于 ``tasks.json`` 的任务：
两条任务的 ``files_to_modify`` 集合相同、且 ``test_command`` 归一化后相同 → 判重复。

本文件钉住三道缝：

* **``files_to_modify`` 是集合** —— 顺序无关。``["a.py", "b.py"]`` 与
  ``["b.py", "a.py"]`` 指的是同一片文件，是同一个东西的两种写法。
* **``test_commands`` 与 ``test_command`` 都读** —— 仓库实际字段是
  ``test_commands`` 数组（``tasks_generator`` 强制「唯一形式，永远用数组」），
  但历史计划与手工编辑过的计划里还有 ``test_command`` 单数字段；只读一个，
  就有一类重复检不出来。
* **空覆盖面不支撑判定** —— ``files_to_modify`` 为空、或只含占位哨兵
  ``__UNKNOWN_MODIFICATIONS__``（作者没列出改哪些文件）时，两条任务"改一样的
  文件"这件事根本没被声明过，不能据此判重（R5 第 3 条的同一条道理）。

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
    detect_duplicate_tasks,
)


# ---------------------------------------------------------------------------
# 同文件 + 同命令 → 成组
# ---------------------------------------------------------------------------


def test_same_files_and_command_forms_group():
    """覆盖面相同 + 命令相同 → 一个组；``files_to_modify`` 顺序无关。"""
    tasks = [
        {
            "id": "1",
            "files_to_modify": ["backend/a.py", "backend/b.py"],
            "test_commands": ["pytest backend/tests/unit/test_a.py -v"],
        },
        {
            "id": "2",
            "files_to_modify": ["backend/b.py", "backend/a.py"],
            "test_command": "pytest  backend/tests/unit/test_a.py -v",
        },
    ]

    groups = detect_duplicate_tasks(tasks)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["1", "2"]
    assert groups[0]["reason"] == (
        "identical_files_and_command: 2 file(s) + pytest backend/tests/unit/test_a.py -v"
    )


def test_test_commands_and_test_command_are_both_read():
    """``test_commands``（数组）与 ``test_command``（字符串）都要计入判重 —— 只读
    一个，就有一类重复检不出来。"""
    for field in ("test_commands", "test_command"):
        tasks = [
            {"id": "T-A", "files_to_modify": ["backend/a.py"], field: "pytest t.py -v"},
            {"id": "T-B", "files_to_modify": ["backend/a.py"], field: "pytest  t.py -v"},
        ]
        groups = detect_duplicate_tasks(tasks)
        assert len(groups) == 1, f"{field} 没被计入判重"
        assert groups[0]["member_ids"] == ["T-A", "T-B"], field


def test_command_sequence_order_matters():
    """命令**序列**的顺序是判据的一部分 —— ``["pytest a"]`` 与
    ``["pytest b", "pytest a"]`` 跑的东西不一样，不成组。"""
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["pytest a"],
        },
        {
            "id": "T-B",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["pytest b", "pytest a"],
        },
    ]

    assert detect_duplicate_tasks(tasks) == []


def test_single_task_is_not_a_group():
    """只有一条 → 不成组（没有重复可言）。"""
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["pytest a"],
        },
    ]

    assert detect_duplicate_tasks(tasks) == []


# ---------------------------------------------------------------------------
# 空覆盖面不支撑判定（R5 第 3 条的同一条道理）
# ---------------------------------------------------------------------------


def test_empty_files_to_modify_never_groups():
    """``files_to_modify`` 为空、或只含 ``__UNKNOWN_MODIFICATIONS__`` 哨兵 → 不判重。

    哨兵的意思是"作者没列出改哪些文件"，不是"这条任务改这个文件"；两条都写
    哨兵的任务，"改一样的文件"这件事根本没被声明过。
    """
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": [],
            "test_commands": ["pytest a"],
        },
        {
            "id": "T-B",
            "files_to_modify": [],
            "test_commands": ["pytest a"],
        },
        {
            "id": "T-C",
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
            "test_commands": ["pytest a"],
        },
        {
            "id": "T-D",
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
            "test_commands": ["pytest a"],
        },
    ]

    assert detect_duplicate_tasks(tasks) == []


def test_missing_files_to_modify_never_groups():
    """缺 ``files_to_modify`` 字段 → 不判重（同样没有声明覆盖面）。"""
    tasks = [
        {"id": "T-A", "test_commands": ["pytest a"]},
        {"id": "T-B", "test_commands": ["pytest a"]},
    ]

    assert detect_duplicate_tasks(tasks) == []


# ---------------------------------------------------------------------------
# 命令不同 → 不成组
# ---------------------------------------------------------------------------


def test_different_command_never_groups():
    """覆盖面相同但命令不同 → 不成组。"""
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["pytest backend/tests/unit/test_a.py -v"],
        },
        {
            "id": "T-B",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["pytest backend/tests/unit/test_b.py -v"],
        },
    ]

    assert detect_duplicate_tasks(tasks) == []


def test_different_files_never_groups():
    """命令相同但覆盖面不同 → 不成组。"""
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["pytest a"],
        },
        {
            "id": "T-B",
            "files_to_modify": ["backend/b.py"],
            "test_commands": ["pytest a"],
        },
    ]

    assert detect_duplicate_tasks(tasks) == []


def test_empty_command_never_groups():
    """命令为空（或归一化后为空）→ 不参与判重。"""
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": ["backend/a.py"],
            "test_commands": [],
        },
        {
            "id": "T-B",
            "files_to_modify": ["backend/a.py"],
            "test_commands": ["   "],
        },
        {
            "id": "T-C",
            "files_to_modify": ["backend/a.py"],
        },
    ]

    assert detect_duplicate_tasks(tasks) == []


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_paths_are_normalised_like_commands():
    """路径归一化与 ``normalize_command`` 一致：反斜杠统一为 ``/``、去首尾空白；
    尾部 ``/`` 不影响指向的是不是同一片文件。"""
    tasks = [
        {
            "id": "T-A",
            "files_to_modify": [" backend\\a.py ", "backend/b.py"],
            "test_commands": ["pytest a"],
        },
        {
            "id": "T-B",
            "files_to_modify": ["backend/a.py", "backend/b.py/"],
            "test_commands": ["pytest a"],
        },
    ]

    groups = detect_duplicate_tasks(tasks)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["T-A", "T-B"]
    assert groups[0]["reason"] == "identical_files_and_command: 2 file(s) + pytest a"


# ---------------------------------------------------------------------------
# 脏输入
# ---------------------------------------------------------------------------


def test_malformed_entries_are_skipped():
    """非 dict 条目、缺 id 的条目跳过，且不抛异常。"""
    tasks = [
        "not-a-task",
        None,
        42,
        {"title": "缺 id", "files_to_modify": ["backend/a.py"]},
        {"id": "", "files_to_modify": ["backend/a.py"], "test_commands": ["pytest a"]},
        {"id": "T-A", "files_to_modify": ["backend/a.py"], "test_commands": ["pytest a"]},
        {"id": "T-B", "files_to_modify": ["backend/a.py"], "test_commands": ["pytest a"]},
    ]

    groups = detect_duplicate_tasks(tasks)

    assert len(groups) == 1
    assert groups[0]["member_ids"] == ["T-A", "T-B"]


def test_empty_task_list_returns_empty():
    assert detect_duplicate_tasks([]) == []