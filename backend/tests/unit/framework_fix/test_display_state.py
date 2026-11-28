"""TDD spec — backend/framework_fix/read_model.py 的五状态投影。

这个模块要解决什么
------------------
PRD 要求**对外只出现五种可见状态**。这五种状态今天分散在好几个载体上：
计划相位（``current_phase``）、计划状态里的 ``framework_fix`` JSON 块、
注册表行的 ``status`` 列。调度页、通知卡片、API 各自读其中一处 —— 于是
同一个事实在三个地方各有一份口径，第三个调用点写错时没人会发现。

投影函数把这一堆载体收敛成**一个**结论：某个计划此刻对外显示成什么状态。

    project_display_state(plan_state, registry_row) -> str | None

返回值 ``None`` 表示「没有框架修复相关的可见状态」，调用方沿用原来的相位。
返回其它值时，那个值一定在 :data:`DISPLAY_STATES` 里。

五种状态（架构决策点 6）：

============================ ==========================================
状态                         语义
============================ ==========================================
``blocked_on_framework_fix`` 本计划的工作被框架缺陷挂起
``waiting_on_user_merge``   修复已就绪，等用户批准合并
``framework_fix_exhausted`` 同一签名轮次耗尽，需人工决策
``scheduler_deadlock_suspected`` 修复 plan 创建后仍无执行路径
``reopened``                修复合入后重跑仍失败，同一 plan 继续
============================ ==========================================

被明确否决的第六个状态
----------------------
``framework_fix_conflict`` **不存在**。PRD 决策点 8 与架构决策点 4/6 都
写明：冲突走既有 git/PR 流程，不新增状态。这个不变量不能只写在文档里 ——
``test_no_conflict_state`` 用一组输入把返回值全部枚举一遍，任何一个输入
都不许产出它。

TDD contract — 5 cases
-----------------------
1. ``test_blocked`` —— hold + 注册表 open -> ``blocked_on_framework_fix``。
2. ``test_waiting_merge`` —— 注册表 fixed 但未合并 -> ``waiting_on_user_merge``。
3. ``test_exhausted`` —— 轮次耗尽 -> ``framework_fix_exhausted``。
4. ``test_reopened`` —— 合并后重新命中 -> ``reopened``。
5. ``test_no_conflict_state`` —— 全部输入都不产出 ``framework_fix_conflict``。

同文件覆盖的边界条件：

* 无 hold 且无注册表 -> ``None``（沿用原相位）；
* 调度器死锁标记 -> ``scheduler_deadlock_suspected``；
* ``DISPLAY_STATES`` 恰好是那五个词。

纯函数：只读入参，不读时钟、不读文件、不发请求。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.framework_fix.read_model import (  # noqa: E402
    DISPLAY_STATES,
    project_display_state,
)

#: 被明确否决的状态：它不在状态集合里，任何输入都不得产出它。
REJECTED_STATE = "framework_fix_conflict"


# ---------------------------------------------------------------------------
# 1. 挂起 —— 计划被框架缺陷挡住
# ---------------------------------------------------------------------------
def test_blocked():
    """hold + 注册表 open -> blocked_on_framework_fix。

    这是 PRD 的原始例子：命中框架缺陷后，受影响的 VP/任务进入
    ``blocked_on_framework_fix``，不判失败、不消耗重试预算、不可 skip。
    """
    plan_state = {"framework_fix": {"hold": True}}
    registry_row = {"status": "open"}

    assert project_display_state(plan_state, registry_row) == "blocked_on_framework_fix"


def test_blocked_via_paused_by():
    """挂起也可以由 ``paused_by`` 承载 —— 挂起信息不只一个字段。"""
    plan_state = {
        "framework_fix": {"paused_by": {"fix_plan_id": "fx-1", "at": "2026-11-23T10:00:00Z"}}
    }

    assert project_display_state(plan_state, None) == "blocked_on_framework_fix"


def test_blocked_via_phase():
    """全部剩余工作被阻塞时，计划相位就是 ``blocked_on_framework_fix``。"""
    plan_state = {"current_phase": "blocked_on_framework_fix", "framework_fix": {}}

    assert project_display_state(plan_state, None) == "blocked_on_framework_fix"


# ---------------------------------------------------------------------------
# 2. 等用户合并
# ---------------------------------------------------------------------------
def test_waiting_merge():
    """注册表 fixed 但修复还没合并 -> waiting_on_user_merge。

    修复的 PR 已经开好、CI 已过，但还没人批准合并 —— 这一段时间里计划对外
    显示的是「等合并」，不是「等失败」。
    """
    plan_state = {"framework_fix": {"pr_url": "https://example.invalid/pr/1"}}
    registry_row = {"status": "fixed", "merged_at": None, "merge_commit": None}

    assert project_display_state(plan_state, registry_row) == "waiting_on_user_merge"


def test_waiting_merge_via_plan_block_status():
    """同一状态也可以由计划自己的 ``framework_fix.status`` 承载。"""
    plan_state = {"framework_fix": {"status": "waiting_on_user_merge"}}

    assert project_display_state(plan_state, {"status": "fixed"}) == "waiting_on_user_merge"


def test_merged_fix_is_not_waiting_merge():
    """修复已经合并进树里 —— 不再等合并。"""
    plan_state = {"framework_fix": {"pr_url": "https://example.invalid/pr/1"}}
    registry_row = {"status": "fixed", "merged_at": "2026-11-23T12:00:00Z"}

    assert project_display_state(plan_state, registry_row) is None


# ---------------------------------------------------------------------------
# 3. 轮次耗尽
# ---------------------------------------------------------------------------
def test_exhausted():
    """同一签名轮次耗尽 -> framework_fix_exhausted。

    耗尽是终态：不再自动重试同一签名，解挂后交人工决策。
    """
    plan_state = {"framework_fix": {"status": "exhausted"}}

    assert project_display_state(plan_state, {"status": "active"}) == "framework_fix_exhausted"


def test_exhausted_beats_hold():
    """挂起与耗尽同时成立时，耗尽优先 —— 它是需要人工决策的那一个。"""
    plan_state = {"framework_fix": {"hold": True, "status": "exhausted"}}

    assert project_display_state(plan_state, None) == "framework_fix_exhausted"


# ---------------------------------------------------------------------------
# 4. 合并后重新命中
# ---------------------------------------------------------------------------
def test_reopened():
    """修复合入后重跑仍失败 -> reopened（同一 plan 继续跑下一轮）。"""
    plan_state = {"framework_fix": {"status": "reopened"}}
    registry_row = {"status": "reopened", "merged_at": "2026-11-23T12:00:00Z"}

    assert project_display_state(plan_state, registry_row) == "reopened"


def test_reopened_from_registry_only():
    """注册表说 reopened 时，计划侧没有额外标记也要投影出来。"""
    plan_state = {"framework_fix": {}}

    assert project_display_state(plan_state, {"status": "reopened"}) == "reopened"


# ---------------------------------------------------------------------------
# 5. 调度器死锁疑似
# ---------------------------------------------------------------------------
def test_deadlock_suspected():
    """修复 plan 创建后仍无执行路径 -> scheduler_deadlock_suspected。"""
    plan_state = {"framework_fix": {"deadlock_suspected_at": "2026-11-23T10:10:00Z"}}

    assert project_display_state(plan_state, None) == "scheduler_deadlock_suspected"


# ---------------------------------------------------------------------------
# 6. 回退
# ---------------------------------------------------------------------------
def test_no_hold_no_registry_returns_none():
    """无 hold 且无注册表 -> None，调用方沿用原相位。"""
    assert project_display_state({}, None) is None
    assert project_display_state({"current_phase": "executing"}, None) is None
    assert project_display_state({"framework_fix": {}}, {"status": "active"}) is None


# ---------------------------------------------------------------------------
# 7. 被否决的状态
# ---------------------------------------------------------------------------
def test_no_conflict_state():
    """任何输入都不得产出 ``framework_fix_conflict``。

    这一条不能只靠「函数里没写这个词」来保证：状态集合是闭集，返回值必须
    落在这个集合里。测试把一组输入的返回值全枚举一遍，逐个断言。
    """
    plan_states = [
        {},
        {"framework_fix": {}},
        {"framework_fix": {"hold": True}},
        {"framework_fix": {"hold": False}},
        {"framework_fix": {"status": "active"}},
        {"framework_fix": {"status": "waiting_on_user_merge"}},
        {"framework_fix": {"status": "exhausted"}},
        {"framework_fix": {"status": "reopened"}},
        {"framework_fix": {"deadlock_suspected_at": "2026-11-23T10:10:00Z"}},
        {"framework_fix": {"paused_by": {"fix_plan_id": "fx-1"}}},
        {"current_phase": "blocked_on_framework_fix"},
    ]
    registry_rows = [
        None,
        {},
        {"status": "open"},
        {"status": "active"},
        {"status": "fixed"},
        {"status": "fixed", "merged_at": "2026-11-23T12:00:00Z"},
        {"status": "reopened"},
        {"status": "exhausted"},
    ]

    for plan_state in plan_states:
        for registry_row in registry_rows:
            state = project_display_state(plan_state, registry_row)
            assert state != REJECTED_STATE, (plan_state, registry_row)
            assert state is None or state in DISPLAY_STATES, (plan_state, registry_row)


def test_display_states_is_the_five_state_closed_set():
    """状态集合恰好是 PRD 批准的那五个词。"""
    assert DISPLAY_STATES == frozenset(
        {
            "blocked_on_framework_fix",
            "waiting_on_user_merge",
            "framework_fix_exhausted",
            "scheduler_deadlock_suspected",
            "reopened",
        }
    )
    assert REJECTED_STATE not in DISPLAY_STATES
    assert isinstance(DISPLAY_STATES, frozenset)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
