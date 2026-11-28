"""框架修复 lane 的抢占顺序（受害者选择）与展示装饰。

这个模块要解决什么
------------------
PRD 决策点 5：框架修复 plan 创建后**立即**取得执行路径；容量不足时由调度器
**抢占**一个正在运行的普通 plan；被抢占者的顺序必须确定；修复 plan 自身
**不可被抢占**（两个修复 plan 互相抢占会形成活锁）。

于是这里给三个纯函数::

    select_victim(running: list[dict]) -> dict | None
    detect_deadlock(now, *, stalled_since, threshold_s=600) -> bool
    decorate_plans(plans, *, registry_rows, leases) -> list[dict]

被抢占者的顺序
--------------
::

    priority 降序 → attach_blocked_count 升序 → elapsed 升序 → plan_id 升序

* ``priority`` 降序 —— 优先值**大**的先被抢占。队列按优先值升序站队，数字
  最大的那个站在队尾，也就是调度器最捨得打断的那一个（PRD：被抢占者顺序
  为「优先级最低」）；
* ``attach_blocked_count`` 升序 —— 挂在这个 plan 上的阻塞工作**少**的先被
  抢占（牵连面越小，越先让路）；
* ``elapsed`` 升序 —— 运行得短的先被抢占（已完成的工作最少）；
* ``plan_id`` 升序 —— 兜底位。它让顺序成为**全序**，于是「被抢占者是谁」
  与输入的排列无关：同一份输入永远给出同一个答案（稳定排序）。

这个顺序与调度器**自己**的驱逐顺序（``plan_scheduler.policy.select_victim``）
同向：那边也是「优先值大的先被抢、运行得短的先被抢」。两处说的是同一件事
—— 谁最捨得打断 —— 所以它们必须给出同一个答案。

不读 ``min_runtime_seconds``
----------------------------
那个保护窗口 ``M`` 保护的是 **operator 触发的驱逐**与超容量收敛。框架修复
的抢占是**硬需求**（PRD：修复 plan 创建后立即取得执行路径），不受它约束 ——
一个刚跑起来的普通 plan 照样可以被抢占。所以本模块没有这个参数，也**不读**
入参里的同名键。

没有受害者
----------
没有可抢占的普通 plan 时答案是 ``None``：调用方**等待**，不扩容量。框架修复
plan 永远不计入候选集，所以「只有修复 plan 在跑」也是 ``None``。

死锁
----
修复 plan 创建后**立即**取得执行路径，所以「等了很久还没有执行路径」不是
「还在排队」，而是**没有人会被叫醒** —— 计划会一直等下去，等到有人来看。
:func:`detect_deadlock` 判定这一刻：等过了十分钟（:data:`DEADLOCK_THRESHOLD_SECONDS`）
就是死锁，调用方据此写原因词并发通知事件。

「从什么时候开始等」是入参而不是读出来的：读不出来时答案是 ``False`` ——
没有起点就没有「等了多久」这回事，猜一个比说「不知道」更糟。

展示装饰
--------
``decorate_plans`` 给计划条目补上框架修复的事实，供调度页与 API 读模型使用
（架构决策点 6）：

* ``display_state`` —— 五状态投影，取自
  :func:`backend.framework_fix.read_model.project_display_state`；
* ``protected`` / ``protected_reason`` —— 是否受保护，受保护时给出
  ``protected_framework_fix``；
* ``merge_slot`` / ``merge_slot_reason`` —— 本仓的合并槽是 ``held``（持有）
  还是 ``waiting``（被**别人**持有，等它释放，理由词 ``waiting_merge_slot``）；
* ``fingerprint`` / ``repo`` / ``pr_url`` —— 这条缺陷本身的事实。

纯函数
------
只读入参：不读时钟、不读文件、不发请求、不看数据库。同一份输入在任何进程、
任何时刻给出同一个结论 —— 抢占顺序如果依赖时钟，被抢占者就不可复算。

包形态
------
与同包的 ``read_model.py`` 一致：``backend/framework_fix/`` 是 PEP 420 的
隐式命名空间包，没有 ``__init__.py``。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from framework_fix.read_model import project_display_state
except ImportError:  # pragma: no cover
    from backend.framework_fix.read_model import project_display_state  # type: ignore

__all__ = [
    "DEADLOCK_THRESHOLD_SECONDS",
    "FRAMEWORK_FIX_KIND",
    "PREEMPTED_REASON",
    "PROTECTED_REASONS",
    "decorate_plans",
    "detect_deadlock",
    "select_victim",
]


# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

#: 框架修复计划的种类词。与 ``plan_scheduler.queue.FRAMEWORK_FIX_KIND`` 是
#: 同一个词（两个包互不导入 —— ``plan_scheduler`` 在生产里是顶层包，
#: ``framework_fix`` 是 ``backend`` 下的命名空间包 —— 所以同一个词在这里
#: 复述一次；``plan_scheduler.policy`` 那边也复述了一次，三处必须一致）。
FRAMEWORK_FIX_KIND = "framework_fix"

#: 受保护的原因词闭集 —— 只有这一个词：框架修复计划不可被抢占。
#: 它也在调度器的原因闭集 ``plan_scheduler.policy.REASONS`` 里（面板、dry
#: run 与 API 画的都是同一批词，闭集外的词画不出来）。
PROTECTED_REASONS: FrozenSet[str] = frozenset({"protected_framework_fix"})

#: 被抢占时挂上的原因词 —— 在 ``REASONS`` 闭集里。
PREEMPTED_REASON = "preempted_by_framework_fix"

#: 合并槽被别人持有时的原因词 —— 也在 ``REASONS`` 闭集里。
WAITING_MERGE_SLOT_REASON = "waiting_merge_slot"

#: 死锁的阈值 —— 等了这么久还没有执行路径，就当是死锁（十分钟）。
#:
#: 十分钟是架构给的数：修复计划创建后**立即**取得执行路径，所以等了十分钟
#: 就不是「还在排队」，而是**没有人会被叫醒** —— 计划会一直等下去。
DEADLOCK_THRESHOLD_SECONDS = 600


# ---------------------------------------------------------------------------
# 读入参
# ---------------------------------------------------------------------------
def _field(row: Any, name: str, default: Any = None) -> Any:
    """读 ``row`` 上的 ``name``，读不到时给 ``default``。

    一行是映射就按映射读，是对象就按属性读 —— 调用方不必把行整理成同一种
    形态。值存在但为 ``NULL`` 时按「没有这个值」处理。
    """
    getter = getattr(row, "get", None)
    if callable(getter):
        value = getter(name, default)
    else:
        value = getattr(row, name, default)
    return default if value is None else value


def _number(value: Any, default: float) -> float:
    """把 ``value`` 读成数字；读不成数字就给 ``default``。

    读不出来的数字是「不知道」，猜一个比说「不知道」更糟。
    """
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _flag(value: Any) -> bool:
    """一个布尔标记，按「真」语义读。

    SQLite / JSON 往返会把布尔值变成 ``0`` / ``1`` / ``"true"`` / ``"false"``，
    用 ``bool()`` 直接判会把字符串 ``"false"`` 读成真。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in (
            "",
            "0",
            "false",
            "no",
            "none",
            "null",
            "off",
        )
    return bool(value)


def _maybe_number(value: Any) -> Optional[float]:
    """把 ``value`` 读成数字；读不成数字就是「不知道」（``None``）。

    与 :func:`_number` 的区别只在一处：这里不替调用方猜一个默认值 ——
    「不知道等了多久」和「等了零秒」是两回事，死锁的判定分得清它们。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN —— 不是数
        return None
    return number


def _entries(rows: Any) -> List[Any]:
    """把入参读成一份列表；读不成列表就是空的。"""
    if rows is None:
        return []
    try:
        return list(rows)
    except TypeError:
        return []


def _plan_id(entry: Any) -> str:
    """这个条目的计划号。"""
    return str(_field(entry, "plan_id", ""))


def _priority(entry: Any) -> float:
    """优先值。读不成数字就是 ``0``（谁都没有特殊待遇）。"""
    return _number(_field(entry, "priority", 0), 0.0)


def _attach_blocked_count(entry: Any) -> float:
    """挂在这个计划上的阻塞工作数。读不成数字就是 ``0``。"""
    return _number(_field(entry, "attach_blocked_count", 0), 0.0)


def _elapsed(entry: Any) -> float:
    """已经跑了多少秒。

    两种写法都认：``elapsed_s``（本 lane 的写法）与 ``elapsed``（调度器
    snapshot 的写法）。读不成数字就是 ``0``。
    """
    value = _field(entry, "elapsed_s", None)
    if value is None:
        value = _field(entry, "elapsed", 0)
    return _number(value, 0.0)


def _is_framework_fix(entry: Any) -> bool:
    """这个条目是不是一个框架修复计划。"""
    return str(_field(entry, "plan_kind", "")) == FRAMEWORK_FIX_KIND


def _is_protected(entry: Any) -> bool:
    """这个运行中的条目是不是**受保护**（不可被抢占）。

    框架修复计划恒定受保护 —— 两个修复计划互相抢占会形成活锁。快照也可以
    直接说 ``protected: true`` 或 ``preemptable: false``，那是同一件事的
    另一种写法，调度器照它执行。
    """
    if _is_framework_fix(entry):
        return True
    if _flag(_field(entry, "protected", False)):
        return True
    preemptable = _field(entry, "preemptable", None)
    if preemptable is not None and not _flag(preemptable):
        return True
    return False


# ---------------------------------------------------------------------------
# 抢占 —— 受害者选择
# ---------------------------------------------------------------------------
def _victim_key(entry: Any) -> Tuple[float, float, float, str]:
    """被抢占者的排序键。

    顺序是 ``priority`` **降序** → ``attach_blocked_count`` **升序** →
    ``elapsed`` 升序 → ``plan_id`` 升序。末位是 ``plan_id``，所以这是一个
    全序：答案与入参的排列无关。
    """
    return (
        -_priority(entry),
        _attach_blocked_count(entry),
        _elapsed(entry),
        _plan_id(entry),
    )


def select_victim(running: Iterable[Any]) -> Optional[Dict[str, str]]:
    """在运行中的计划里挑一个**可抢占**的受害者。

    Args:
        running: 正在运行的计划。每一项至少带 ``plan_id``；可选的有
            ``priority``（优先值）、``attach_blocked_count``（挂在这个计划上
            的阻塞工作数）、``elapsed_s`` 或 ``elapsed``（已经跑了多少秒）、
            ``plan_kind``（``framework_fix`` 表示这是一个框架修复计划）。

            ``min_runtime_seconds`` **不读** —— 那个保护窗口属于 operator 触
            发的驱逐与超容量收敛；框架修复的抢占是硬需求，不受它约束。

    Returns:
        ``{"plan_id": ..., "reason": "preempted_by_framework_fix"}``，或者
        ``None`` —— 没有可抢占的普通 plan 时。``None`` 是答案而不是错误：
        调用方**等待**，不扩容量。

    Examples:
        >>> select_victim([
        ...     {"plan_id": "a", "priority": 5, "attach_blocked_count": 0, "elapsed_s": 100},
        ...     {"plan_id": "b", "priority": 3, "attach_blocked_count": 2, "elapsed_s": 50},
        ... ])
        {'plan_id': 'a', 'reason': 'preempted_by_framework_fix'}
    """
    candidates = [entry for entry in _entries(running) if not _is_protected(entry)]
    if not candidates:
        return None
    victim = min(candidates, key=_victim_key)
    return {"plan_id": _plan_id(victim), "reason": PREEMPTED_REASON}


# ---------------------------------------------------------------------------
# 死锁的判定
# ---------------------------------------------------------------------------
def detect_deadlock(
    now: Any,
    *,
    stalled_since: Any,
    threshold_s: float = DEADLOCK_THRESHOLD_SECONDS,
) -> bool:
    """这个修复计划是不是已经等成了死锁。

    框架修复是硬需求：修复计划创建后**立即**取得执行路径。所以「等了很久
    还没有执行路径」不是「还在排队」，而是**没有人会被叫醒** —— 计划会一直
    等下去，等到有人来看。这一刻要有人知道，所以它升级成死锁：写一个原因词
    （``scheduler_deadlock_suspected``，在调度器的原因闭集里），并发一条通知
    事件。

    纯函数：给定「此刻」与「从什么时候开始等」，答案只有一个 —— 不读时钟、不
    读文件、不看数据库。

    Args:
        now: 此刻（epoch 秒）。
        stalled_since: 从什么时候开始等（epoch 秒）。``None`` 表示**不知道**从
            什么时候开始等 —— 没有起点就没有「等了多久」这回事,猜一个比说
            「不知道」更糟。
        threshold_s: 阈值，默认十分钟（:data:`DEADLOCK_THRESHOLD_SECONDS`）。

    Returns:
        ``True`` —— 等过了阈值，这是死锁；``False`` —— 还没有到阈值，或者根本
        说不准。

    Examples:
        >>> detect_deadlock(1000.0, stalled_since=400.0)
        True
        >>> detect_deadlock(1000.0, stalled_since=401.0)
        False
    """
    moment = _maybe_number(now)
    since = _maybe_number(stalled_since)
    if moment is None or since is None:
        return False
    threshold = _maybe_number(threshold_s)
    if threshold is None:
        threshold = float(DEADLOCK_THRESHOLD_SECONDS)
    waited = moment - since
    if waited < 0:
        # 时钟往回走了 —— 这是读错了，不是「等了负十分钟」。
        return False
    return waited >= threshold


# ---------------------------------------------------------------------------
# 展示装饰
# ---------------------------------------------------------------------------
def _as_dict(value: Any) -> Dict[str, Any]:
    """只接受 dict；其余形态当作空。"""
    return value if isinstance(value, dict) else {}


def _framework_fix_block(plan: Any) -> Dict[str, Any]:
    """计划条目上的 ``framework_fix`` 块。"""
    return _as_dict(_field(plan, "framework_fix", None))


def _index_registry(
    registry_rows: Iterable[Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把注册表行按「修复 plan」与「指纹」各索引一份。"""
    by_fix_plan: Dict[str, Any] = {}
    by_fingerprint: Dict[str, Any] = {}
    for row in _entries(registry_rows):
        fix_plan_id = _field(row, "fix_plan_id", None)
        if fix_plan_id:
            by_fix_plan.setdefault(str(fix_plan_id), row)
        fingerprint = _field(row, "fingerprint", None)
        if fingerprint:
            by_fingerprint.setdefault(str(fingerprint), row)
    return by_fix_plan, by_fingerprint


def _index_leases(leases: Iterable[Any]) -> Dict[str, Any]:
    """把合并租约按仓库索引（一个仓库一个槽）。"""
    by_repo: Dict[str, Any] = {}
    for row in _entries(leases):
        repo = _field(row, "repo", None)
        if repo:
            by_repo.setdefault(str(repo), row)
    return by_repo


def _registry_row_for(
    plan: Any,
    plan_id: str,
    by_fix_plan: Dict[str, Any],
    by_fingerprint: Dict[str, Any],
) -> Optional[Any]:
    """这个计划对应注册表里的哪一行。

    * 修复 plan —— 它**自己**修的那条缺陷（``fix_plan_id`` 就是它）；
    * 被挂起的普通 plan —— 它等的那条缺陷（``framework_fix.fingerprint`` 或
      ``framework_fix.paused_by.fix_plan_id`` 指过去）。

    都没有就返回 ``None``：这个计划与框架修复无关。
    """
    block = _framework_fix_block(plan)

    fingerprint = _field(block, "fingerprint", None)
    if fingerprint:
        row = by_fingerprint.get(str(fingerprint))
        if row is not None:
            return row

    paused_by = _field(block, "paused_by", None)
    fix_plan_id = _field(paused_by, "fix_plan_id", None) if paused_by else None
    if not fix_plan_id:
        fix_plan_id = _field(block, "fix_plan_id", None)
    if fix_plan_id:
        row = by_fix_plan.get(str(fix_plan_id))
        if row is not None:
            return row

    return by_fix_plan.get(plan_id)


def decorate_plans(
    plans: Iterable[Any],
    *,
    registry_rows: Iterable[Any],
    leases: Iterable[Any],
) -> List[Dict[str, Any]]:
    """给计划条目补上框架修复的事实。

    Args:
        plans: 计划条目（映射）。原样保留它们的键，再补上框架修复的事实。
        registry_rows: ``framework_fix_registry`` 的行 —— 每行一条缺陷，带
            ``fingerprint`` / ``fix_plan_id`` / ``repo`` / ``status`` /
            合并信息。
        leases: ``framework_fix_merge_lease`` 的行 —— 每行一个仓库的合并槽，
            带 ``repo`` / ``fix_plan_id`` / ``pr_url``。

    Returns:
        同样长度的列表，每一项是入参那一项的拷贝加上：

        * ``display_state`` —— 五状态投影（读模型），没有框架修复状态时是
          ``None``；
        * ``protected`` / ``protected_reason`` —— 是否受保护，受保护时理由词
          是 ``protected_framework_fix``；
        * ``merge_slot`` —— ``held`` / ``waiting`` / ``None``；
        * ``merge_slot_reason`` —— 槽被别人占着时是 ``waiting_merge_slot``；
        * ``fingerprint`` / ``repo`` / ``pr_url`` —— 这条缺陷本身的事实。

    入参的列表与其中的字典**都不被修改**。
    """
    by_fix_plan, by_fingerprint = _index_registry(registry_rows)
    leases_by_repo = _index_leases(leases)

    decorated: List[Dict[str, Any]] = []
    for plan in _entries(plans):
        entry = dict(plan)
        plan_id = _plan_id(plan)
        registry_row = _registry_row_for(plan, plan_id, by_fix_plan, by_fingerprint)

        owns = (
            registry_row is not None
            and str(_field(registry_row, "fix_plan_id", "")) == plan_id
        )
        protected = _is_framework_fix(plan) or owns

        repo = _field(registry_row, "repo", None) if registry_row else None
        fingerprint = (
            _field(registry_row, "fingerprint", None) if registry_row else None
        )

        merge_slot: Optional[str] = None
        merge_slot_reason: Optional[str] = None
        pr_url: Optional[str] = None
        if owns and repo:
            lease = leases_by_repo.get(str(repo))
            if lease is None:
                # 这个仓库的合并槽还没人占 —— 没有槽可等。
                merge_slot = None
            elif str(_field(lease, "fix_plan_id", "")) == plan_id:
                merge_slot = "held"
                pr_url = _field(lease, "pr_url", None)
            else:
                # 槽在**别人**手里：本 plan 得等它释放。
                merge_slot = "waiting"
                merge_slot_reason = WAITING_MERGE_SLOT_REASON

        entry["display_state"] = project_display_state(
            _as_dict(plan), _as_dict(registry_row) if registry_row else None
        )
        entry["protected"] = protected
        entry["protected_reason"] = (
            "protected_framework_fix" if protected else None
        )
        entry["merge_slot"] = merge_slot
        entry["merge_slot_reason"] = merge_slot_reason
        entry["fingerprint"] = fingerprint
        entry["repo"] = repo
        entry["pr_url"] = pr_url
        decorated.append(entry)

    return decorated
