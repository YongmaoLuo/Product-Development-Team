"""框架修复的读模型 —— 五种可见状态的**唯一**投影口径。

这个模块要解决什么
------------------
PRD 要求「对外只出现五种可见状态」。这五种状态今天分散在好几个载体上：

* 计划相位 ``current_phase``（全部剩余工作被阻塞时的 ``blocked_on_framework_fix``）
* 计划状态里的 ``framework_fix`` JSON 块（``waiting_on_user_merge`` /
  ``exhausted`` / ``scheduler_deadlock_suspected`` / ``reopened``）
* 注册表行的 ``status`` 列（``active`` / ``fixed`` / ``reopened``）

调度页、通知卡片、API 各自读其中一处 —— 于是同一个事实在三个地方各有一份
口径，第三个调用点写错时没人会发现（而且没有任何测试会发现）。

投影函数把这一堆载体收敛成**一个**结论：某个计划此刻对外显示成什么状态。

    project_display_state(plan_state, registry_row) -> str | None

返回 ``None`` 表示「没有框架修复相关的可见状态」，调用方沿用原来的相位。
返回其它值时，那个值一定在 :data:`DISPLAY_STATES` 里 —— 这不是约定，是
函数的最后一道闸（见 ``_ALLOWED``）。

五种状态（架构决策点 6）
------------------------

============================ ==========================================
状态                         语义
============================ ==========================================
``blocked_on_framework_fix`` 本计划的工作被框架缺陷挂起
``waiting_on_user_merge``   修复已就绪，等用户批准合并
``framework_fix_exhausted`` 同一签名轮次耗尽，需人工决策
``scheduler_deadlock_suspected`` 修复 plan 创建后仍无执行路径
``reopened``                修复合入后重跑仍失败，同一 plan 继续
============================ ==========================================

被否决的第六个状态
------------------
``framework_fix_conflict`` **不存在**。PRD 决策点 8 与架构决策点 4/6 都写明：
冲突走既有 git/PR 流程，不新增状态。这个不变量不写在文档里就算数 ——
返回值被限制在 :data:`DISPLAY_STATES` 内，而那个集合里没有它。

优先级
------
几个载体可能同时成立（挂起 + 等合并 + 注册表说 reopened）。投影必须是**纯
函数**，所以顺序写死在代码里，不靠调用方的判断：

1. ``framework_fix_exhausted`` —— 终态，需人工决策，不再自动重试同一签名；
2. ``scheduler_deadlock_suspected`` —— 无执行路径，需升级，比「在跑第 2 轮」
   更紧急；
3. ``reopened`` —— 修复合入后重新命中，同一 plan 继续跑下一轮；
4. ``waiting_on_user_merge`` —— 修复就绪，等人批准合并（比「被挂起」更具体：
   它告诉人下一步动作是什么）；
5. ``blocked_on_framework_fix`` —— 挂起本身；
6. ``None`` —— 没有框架修复相关的可见状态，沿用原相位。

纯函数
------
只读入参：不读时钟、不读文件、不发请求、不看数据库。同一份输入在任何进程、
任何时刻给出同一个结论 —— 展示状态如果依赖时钟，它就不再是状态。

包形态
------
与同包的 ``fingerprint.py`` 一致：``backend/framework_fix/`` 是 PEP 420 的
隐式命名空间包，没有 ``__init__.py``。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

__all__ = [
    "DISPLAY_STATES",
    "project_display_state",
]


# ---------------------------------------------------------------------------
# 状态的词表
# ---------------------------------------------------------------------------

#: 本计划的工作被框架缺陷挂起。
BLOCKED = "blocked_on_framework_fix"

#: 修复已就绪，等用户批准合并。
WAITING_MERGE = "waiting_on_user_merge"

#: 同一签名的轮次耗尽，需人工决策。
EXHAUSTED = "framework_fix_exhausted"

#: 修复 plan 创建后仍无执行路径。
DEADLOCK = "scheduler_deadlock_suspected"

#: 修复合入后重跑仍失败，同一 plan 继续。
REOPENED = "reopened"

#: 对外可见状态的闭集。调用方拿返回值去渲染徽章 —— 集合外的词渲染不出来，
#: 所以集合必须小而封闭。
DISPLAY_STATES: FrozenSet[str] = frozenset(
    {
        BLOCKED,
        WAITING_MERGE,
        EXHAUSTED,
        DEADLOCK,
        REOPENED,
    }
)

#: 被明确否决的状态。它不在 :data:`DISPLAY_STATES` 里，因此投影函数不可能
#: 返回它 —— 这条不变量由返回值闸门保证，不靠「记得别写这个词」。
REJECTED_STATE = "framework_fix_conflict"

#: 返回值闸门。投影的每个分支都返回 :data:`DISPLAY_STATES` 的成员，这里
#: 再兜一次底：返回值要么是 ``None``，要么在闭集内。
_ALLOWED: FrozenSet[str] = DISPLAY_STATES


# ---------------------------------------------------------------------------
# 词表归一
# ---------------------------------------------------------------------------

#: 各载体上「轮次耗尽」的写法。
_EXHAUSTED_STATUSES = frozenset({"exhausted", "framework_fix_exhausted"})

#: 各载体上「调度器死锁疑似」的写法。
_DEADLOCK_STATUSES = frozenset(
    {"scheduler_deadlock_suspected", "deadlock_suspected", "deadlock"}
)

#: 各载体上「合并后重新命中」的写法。
_REOPENED_STATUSES = frozenset({"reopened", "framework_fix_reopened"})

#: 各载体上「等用户合并」的写法。
_WAITING_MERGE_STATUSES = frozenset(
    {
        "waiting_on_user_merge",
        "waiting_merge",
        "waiting_for_merge",
        "framework_fix_waiting_on_user_merge",
    }
)

#: 注册表上「修复已完成」的写法。
_FIXED_STATUSES = frozenset({"fixed"})

#: 注册表上「已合入」的写法。
_MERGED_STATUSES = frozenset({"merged"})

#: 字符串形态的假值。JSON 从 SQLite 读出来的是字符串，``"false"`` 在
#: Python 里是真值 —— 不认它，``hold: "false"`` 会被当成挂起。
_FALSE_TOKENS = frozenset({"", "0", "false", "no", "none", "null", "off"})


def _normalize(value: Any) -> str:
    """一个状态词，归一成可比的小写形式。"""
    if value is None:
        return ""
    return " ".join(str(value).split()).lower()


def _flag(value: Any) -> bool:
    """一个布尔标记，按「真」语义读。

    SQLite / JSON 往返会把布尔值变成 ``0`` / ``1`` / ``"true"`` / ``"false"``，
    四种形态都出现过。用 ``bool()`` 直接判会把字符串 ``"false"`` 读成真。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TOKENS
    return bool(value)


def _as_dict(value: Any) -> Dict[str, Any]:
    """只接受 dict；其余形态当作空。"""
    return value if isinstance(value, dict) else {}


def _framework_fix_block(plan_state: Any) -> Dict[str, Any]:
    """计划状态里的 ``framework_fix`` 块。"""
    plan = _as_dict(plan_state)
    return _as_dict(plan.get("framework_fix"))


def _is_merged(registry: Dict[str, Any]) -> bool:
    """修复是否已经合入树里。

    依据是注册表行上的合并信息（``merged_at`` / ``merge_commit``），或者
    ``status`` 直接写成 ``merged``。没有合并信息就当没合入 —— 那正是
    「等用户合并」的状态。
    """
    if registry.get("merged_at") or registry.get("merge_commit"):
        return True
    return _normalize(registry.get("status")) in _MERGED_STATUSES


def _is_held(plan_state: Any, block: Dict[str, Any]) -> bool:
    """本计划的工作是否被框架缺陷挂起。

    挂起有三个载体：``framework_fix.hold`` 标记、``framework_fix.paused_by``
    （挂到哪个修复 plan 上）、以及全部剩余工作被阻塞时的计划相位
    ``blocked_on_framework_fix``。
    """
    if _flag(block.get("hold")):
        return True
    paused = block.get("paused_by")
    if isinstance(paused, dict):
        if paused.get("fix_plan_id") or paused.get("at"):
            return True
    elif paused:
        return True
    phase = _normalize(_as_dict(plan_state).get("current_phase"))
    return phase == BLOCKED


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------
def project_display_state(
    plan_state: Optional[Dict[str, Any]],
    registry_row: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """一个计划此刻对外显示成什么状态。

    返回值要么是 ``None``，要么是 :data:`DISPLAY_STATES` 的成员 —— 这条由
    返回值闸门保证（``_ALLOWED``），不靠每个分支都记得写对。

    Args:
        plan_state: 计划状态字典。可用的部分：

            * ``framework_fix`` 块 —— ``status``（``active`` /
              ``waiting_on_user_merge`` / ``exhausted`` / ``reopened``）、
              ``hold``、``paused_by``、``deadlock_suspected_at``、
              ``merge_ready_at``；
            * ``current_phase`` —— 全部剩余工作被阻塞时是
              ``blocked_on_framework_fix``。

        registry_row: 该缺陷在注册表里的那一行。可用的部分：
            ``status``（``active`` / ``fixed`` / ``reopened`` / ``exhausted``）、
            ``merged_at`` / ``merge_commit``。没有这一行时按「注册表里没有
            记录」处理。

    Returns:
        :data:`DISPLAY_STATES` 里的一个状态词，或 ``None`` —— 后者表示「没有
        框架修复相关的可见状态」，调用方沿用原来的相位。

        永远不会返回 ``framework_fix_conflict``：该状态被 PRD 明确否决，
        冲突走既有 git/PR 流程。

    Examples:
        >>> project_display_state({'framework_fix': {'hold': True}}, {'status': 'open'})
        'blocked_on_framework_fix'
        >>> project_display_state({'framework_fix': {}}, {'status': 'active'}) is None
        True
    """
    state = _project_state(plan_state, registry_row)
    if state is not None and state not in _ALLOWED:
        return None
    return state


def _project_state(
    plan_state: Optional[Dict[str, Any]],
    registry_row: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """投影本体 —— 返回候选状态词；闸门由 :func:`project_display_state` 兜。"""
    plan = _as_dict(plan_state)
    block = _framework_fix_block(plan)
    registry = _as_dict(registry_row)

    block_status = _normalize(block.get("status"))
    registry_status = _normalize(registry.get("status"))

    # 1. 轮次耗尽 —— 终态，需人工决策。
    if (
        block_status in _EXHAUSTED_STATUSES
        or registry_status in _EXHAUSTED_STATUSES
        or _flag(block.get("exhausted"))
        or _flag(registry.get("exhausted"))
    ):
        return EXHAUSTED

    # 2. 调度器死锁疑似 —— 修复 plan 创建后仍无执行路径。
    if (
        block_status in _DEADLOCK_STATUSES
        or registry_status in _DEADLOCK_STATUSES
        or _flag(block.get("deadlock"))
        or _flag(registry.get("deadlock"))
        or block.get("deadlock_suspected_at")
    ):
        return DEADLOCK

    # 3. 合并后重新命中 —— 同一 plan 继续跑下一轮。
    if (
        block_status in _REOPENED_STATUSES
        or registry_status in _REOPENED_STATUSES
        or _flag(block.get("reopened"))
        or _flag(registry.get("reopened"))
    ):
        return REOPENED

    # 4. 等用户合并 —— 修复就绪，还没合入。
    if (
        block_status in _WAITING_MERGE_STATUSES
        or registry_status in _WAITING_MERGE_STATUSES
    ):
        return WAITING_MERGE
    if registry_status in _FIXED_STATUSES and not _is_merged(registry):
        return WAITING_MERGE
    if block.get("merge_ready_at") and not _is_merged(registry):
        return WAITING_MERGE

    # 5. 挂起 —— 本计划的工作被框架缺陷挡住。
    if _is_held(plan, block):
        return BLOCKED

    # 6. 没有框架修复相关的可见状态 —— 沿用原相位。
    return None
