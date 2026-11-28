"""框架修复的通知事件与提醒节奏 —— 八类事件、30 分钟合并提醒、22:00–08:00 静默。

这个模块要解决什么
------------------
PRD 要求「命中框架缺陷必须通知到人」：八类事件各自有一个确定的触发时机，
合并审批在就绪满 30 分钟仍未批复时重发，夜间按 quiet hours 静默。此前这件事
在代码里没有任何表达 —— 命中框架缺陷不会通知任何人，而「没有人被通知」和
「通知了但没人看见」在日志里长得一模一样。

本模块把三件事收成一处：

* :data:`EVENTS` —— 八类事件的**闭集**。集合外的词抛 ``ValueError``：拼错的
  事件名在**发出之前**就被挡住，而不是投出去之后没人认得。
* :func:`emit` —— 投递一条事件。静默窗口内**不投递但记录**；投递失败
  **记录错误而不抛出** —— 通知不是主流程，通知失败不能把修复流程带崩。
* :func:`due_reminders` —— 哪些合并租约该发 30 分钟提醒了。

八类事件（架构决策点 6）
----------------------

============================================ ==================================
事件                                          触发时机
============================================ ==================================
``discovered``                               首次发现并创建修复 plan（一次）
``merge_ready``                              CI 通过，消息带 PR 号、CI 结论与
                                             approve 指引
``merge_reminder_30m``                       08:00–22:00 未批复满 30 分钟重发
``waiting_on_user_merge``                    未批复满 2 小时，状态升级
``merged``                                   修复合入（一次）
``reopened``                                 合入后重跑仍失败，同一 plan 继续
``exhausted``                                轮次耗尽，挂接 plan 解挂
``scheduler_deadlock_suspected``             10 分钟无执行路径
============================================ ==================================

提醒是纯函数
------------
:func:`due_reminders` 不读文件、不发请求、不看数据库 —— 它给定「此刻」与租约
列表，答案只有一个。所有提醒由调度器 tick 调用，不新增 cron 或 daemon。

租约的样子
----------
一条租约是一个映射，至少带 ``plan_id``（租约属于哪个修复 plan；注册表里的
``fix_plan_id`` 是同一个字段的另一个写法）。另外两个字段是可选的，但含义是
确定的：

* ``merge_ready_at`` —— 什么时候就绪的（可以是一个 ``datetime``，也可以是
  ISO 字符串）。**没有它就没有提醒**：没有「就绪」这个起点，就没有「满了
  30 分钟」这回事。
* 已提醒过的标记 —— ``notified_events`` 之类的集合里已经有过
  ``merge_reminder_30m``，或者 ``merge_reminder_30m`` / ``reminder_sent`` 这类
  布尔标记为真，或者 ``last_reminder_at`` 有值。**已提醒过的不重复**：同一个
  租约的提醒只发一次。

静默窗口
--------
本地时间 22:00–08:00 不投递。窗口是**给人看的**，所以按本地时间判断：带时区
的时刻先换算到本机本地时间，没有时区信息的时刻按字面读（调用方说的就是
本地时间）。窗口内**不投递但记录** —— 静默是「现在不打扰」，不是「没发生」：
一条没发出去的提醒必须留下痕迹，否则第二天没人知道它被吞了。

加急合并走 HITL
---------------
``merge_ready`` 不是一条贴在墙上的消息，它是一条**等人批准**的问题：PR 号、
CI 结论与「回复 approve 合入」的问句落在 HITL 的决定表里，用户答了才算数
（PRD 决策点 7：没有用户批准就不合入）。这条通道是既有的 HITL 投递通道 ——
出口（``hooks.dispatcher``）本来就是 HITL 送消息用的那个。

包形态
------
``backend/framework_fix/`` 是 PEP 420 的隐式命名空间包，没有 ``__init__.py``，
与 :mod:`backend.framework_fix.read_model` 同处一室。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set

__all__ = [
    "EVENTS",
    "EVENT_PREFIX",
    "MERGE_EVENTS",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
    "REMINDER_EVENT",
    "clear_records",
    "due_reminders",
    "emit",
    "in_quiet_hours",
    "records",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 事件的词表
# ---------------------------------------------------------------------------

#: 通知事件的闭集。八类，一类不多一类不少 —— 集合外的词不是「新语义」，
#: 是拼错了。
EVENTS: FrozenSet[str] = frozenset(
    {
        "discovered",
        "merge_ready",
        "merge_reminder_30m",
        "waiting_on_user_merge",
        "merged",
        "reopened",
        "exhausted",
        "scheduler_deadlock_suspected",
    }
)

#: 投递给出口时事件名的前缀。出口只认「一个事件名 + 一份 body」，事件名是
#: 这一层的，所以带上前缀 —— 出口上的事件名不会与别的模块撞车。
EVENT_PREFIX = "framework_fix_"

#: 提醒事件的名字。去重按它算：一条租约的 ``merge_reminder_30m`` 只发一次。
REMINDER_EVENT = "merge_reminder_30m"

#: 需要人批准才能合入的事件 —— 它们走 HITL 的加急询问，而不是贴在墙上。
MERGE_EVENTS: FrozenSet[str] = frozenset({"merge_ready"})

#: 静默窗口的起点（本地小时，含）。
QUIET_HOURS_START = 22

#: 静默窗口的终点（本地小时，不含）。
QUIET_HOURS_END = 8


# ---------------------------------------------------------------------------
# 投递记录
# ---------------------------------------------------------------------------

#: 进程内保留的投递记录。每条记录说明「这条事件怎么了」：投出去了没有、被
#: 静默窗口吞了没有、失败了没有。
_RECORDS: List[Dict[str, Any]] = []

#: 记录条数的上限。后端是长跑的进程，记录不能无限长 —— 留最近的就够回答
#: 「刚才那条通知到底发出去没有」。
_RECORDS_LIMIT = 500


def records() -> List[Dict[str, Any]]:
    """到目前为止的投递记录（最新的在最后）。"""
    return [dict(record) for record in _RECORDS]


def clear_records() -> None:
    """清空投递记录。"""
    _RECORDS.clear()


def _remember(record: Dict[str, Any]) -> None:
    """记下一条投递记录。"""
    _RECORDS.append(record)
    if len(_RECORDS) > _RECORDS_LIMIT:
        del _RECORDS[: len(_RECORDS) - _RECORDS_LIMIT]


# ---------------------------------------------------------------------------
# 时刻与标记的归一
# ---------------------------------------------------------------------------

#: 字符串形态的假值。JSON 与 SQLite 往返会把布尔值变成 ``"false"``，
#: 而 ``"false"`` 在 Python 里是真值 —— 不认它，``reminder_sent: "false"``
#: 会被当成「已经提醒过了」。
_FALSE_TOKENS = frozenset({"", "0", "false", "no", "none", "null", "off"})


def _truthy(value: Any) -> bool:
    """一个布尔标记，按「真」语义读。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TOKENS
    return bool(value)


def _as_datetime(value: Any) -> Optional[datetime]:
    """一个时刻 —— ``datetime``、ISO 字符串或 epoch 秒；认不出来就是 ``None``。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _local_moment(moment: datetime) -> datetime:
    """把时刻换算到本地时间 —— 静默窗口是给人看的。"""
    if moment.tzinfo is None:
        # 没有时区信息：调用方说的就是本地时间，按字面读。
        return moment
    return moment.astimezone()


def _comparable(moment: datetime, other: datetime) -> datetime:
    """把 ``other`` 换算到与 ``moment`` 可比的形式。

    一个带时区、一个不带时区时，按**带时区的那个**读 —— 无时区的时刻被当成
    「就在那个时区里」，而不是被当成 UTC（那会把提醒算早八小时）。
    """
    if moment.tzinfo is None and other.tzinfo is not None:
        return other.replace(tzinfo=None)
    if moment.tzinfo is not None and other.tzinfo is None:
        return other.replace(tzinfo=moment.tzinfo)
    return other


def in_quiet_hours(moment: datetime) -> bool:
    """此刻是不是落在 22:00–08:00 的静默窗口里。"""
    local = _local_moment(moment)
    return local.hour >= QUIET_HOURS_START or local.hour < QUIET_HOURS_END


# ---------------------------------------------------------------------------
# 提醒的计算
# ---------------------------------------------------------------------------

#: 「这条租约已经提醒过了」的布尔标记。
_REMINDER_FLAG_KEYS = (
    "merge_reminder_30m",
    "reminder_30m_sent",
    "reminder_sent",
    "reminded",
    "reminded_30m",
)

#: 「这条租约已经发过哪些事件」的集合。
_REMINDER_COLLECTION_KEYS = (
    "notified_events",
    "emitted_events",
    "delivered_events",
    "events",
)

#: 「这条租约上次是什么时候提醒的」的时间戳。
_REMINDER_STAMP_KEYS = (
    "last_reminder_at",
    "reminder_sent_at",
    "reminded_at",
    "last_reminded_at",
    "reminder_30m_sent_at",
    "merge_reminder_30m_at",
)

#: 租约的有效期 —— 过了就不再提醒。
_EXPIRY_KEYS = ("expires_at", "lease_expires_at", "expire_at", "valid_until")


def _iter_leases(leases: Any) -> List[Mapping[str, Any]]:
    """租约列表 —— 单条租约也收。"""
    if leases is None:
        return []
    if isinstance(leases, Mapping):
        if "plan_id" in leases or "fix_plan_id" in leases:
            return [leases]
        return [lease for lease in leases.values() if isinstance(lease, Mapping)]
    if isinstance(leases, (str, bytes)):
        return []
    return [lease for lease in leases if isinstance(lease, Mapping)]


def _plan_id_of(lease: Mapping[str, Any]) -> str:
    """租约属于哪个修复 plan。注册表里的写法是 ``fix_plan_id``。"""
    for key in ("plan_id", "fix_plan_id"):
        value = lease.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _already_reminded(lease: Mapping[str, Any]) -> bool:
    """这条租约是不是已经提醒过了。"""
    for key in _REMINDER_FLAG_KEYS:
        if _truthy(lease.get(key)):
            return True
    for key in _REMINDER_COLLECTION_KEYS:
        value = lease.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            if REMINDER_EVENT in value:
                return True
    for key in _REMINDER_STAMP_KEYS:
        if lease.get(key):
            return True
    notified = lease.get("notified")
    if isinstance(notified, Mapping) and _truthy(notified.get(REMINDER_EVENT)):
        return True
    return False


def _is_expired(lease: Mapping[str, Any], moment: datetime) -> bool:
    """租约的有效期是不是已经过了。"""
    for key in _EXPIRY_KEYS:
        expiry = _as_datetime(lease.get(key))
        if expiry is None:
            continue
        if moment >= _comparable(moment, expiry):
            return True
    return False


def due_reminders(
    now: datetime,
    leases: Iterable[Mapping[str, Any]],
    threshold_minutes: int = 30,
) -> List[str]:
    """哪些合并租约该发提醒了。

    纯函数：给定「此刻」与租约列表，答案只有一个 —— 不读文件、不发请求、
    不看数据库。所有提醒由调度器 tick 调用（架构决策点 6：提醒由 tick 基于
    ``merge_ready_at`` 计算，不新增 cron 或 daemon）。

    Args:
        now: 此刻。
        leases: 租约列表。每条至少带 ``plan_id``；``merge_ready_at`` 说
            什么时候就绪的；提醒标记说这条租约是不是已经提醒过了。
        threshold_minutes: 阈值，默认 30 分钟 —— 架构给出的重发间隔。

    Returns:
        到期的租约的 plan id，按租约的先后顺序，不重复。

    Examples:
        >>> now = datetime(2026, 11, 23, 12, 0)
        >>> due_reminders(now, [{'plan_id': 'fx1',
        ...                      'merge_ready_at': now - timedelta(minutes=30)}])
        ['fx1']
    """
    moment = _as_datetime(now)
    if moment is None:
        return []
    threshold = timedelta(minutes=threshold_minutes)

    due: List[str] = []
    seen: Set[str] = set()
    for lease in _iter_leases(leases):
        plan_id = _plan_id_of(lease)
        if not plan_id or plan_id in seen:
            continue
        ready_at = _as_datetime(lease.get("merge_ready_at"))
        if ready_at is None:
            # 无有效期 —— 没有「就绪」这个起点，就没有提醒。
            continue
        if _is_expired(lease, moment):
            continue
        if _already_reminded(lease):
            # 已提醒过 —— 不重复。
            continue
        if moment - _comparable(moment, ready_at) < threshold:
            continue
        seen.add(plan_id)
        due.append(plan_id)
    return due


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


def _deliver_outlet(event_type: str, body: Mapping[str, Any], plan_dir: Any) -> bool:
    """经出口投递 —— 出口就是 HITL 送消息用的那个通道。"""
    from hooks.dispatcher import dispatch

    report = dispatch(EVENT_PREFIX + event_type, body, plan_dir)
    return report.delivered > 0


def _deliver_merge(body: Mapping[str, Any], plan_dir: Any) -> bool:
    """加急合并 —— 落一条等人批准的决定。"""
    from hitl.service import create_merge_decision

    pr_url = str(body.get("pr_url") or "").strip()
    if not pr_url:
        # 没有 PR 号就问不出「批不批准这个 PR」—— 退回普通投递。
        return _deliver_outlet("merge_ready", body, plan_dir)

    decision = create_merge_decision(
        str(body.get("plan_id") or "").strip(),
        pr_url,
        str(body.get("ci_conclusion") or ""),
        plan_dir,
    )
    return decision is not None


def _deliver(event_type: str, body: Mapping[str, Any], plan_dir: Any) -> bool:
    """把一条事件送出去 —— 投出去了没有。"""
    if not plan_dir:
        raise ValueError("the payload carries no plan_dir: nowhere to deliver to")
    if event_type in MERGE_EVENTS:
        return _deliver_merge(body, plan_dir)
    return _deliver_outlet(event_type, body, plan_dir)


def emit(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """发一条框架修复的通知事件。

    三条边界，都是「通知不是主流程」这条规则的推论：

    * **事件类型必须在闭集内** —— 集合外的词抛 ``ValueError``。这是调用方的
      错误（拼错了事件名），不是系统故障，所以当场报出来，不投出去。
    * **静默窗口内不投递但记录** —— 22:00–08:00 不打扰人，但事件仍然记下来。
    * **投递失败只记录，不抛出** —— 出口挂了、数据库打不开，这些都不该把
      修复流程带崩。错误写进记录与日志，主流程继续。

    Args:
        event_type: 八类事件之一（:data:`EVENTS`）。
        payload: 事件的内容。``plan_dir`` 说的是送到哪个计划的出口；其余字段
            原样带给出口（``merge_ready`` 还会读 ``pr_url`` 与
            ``ci_conclusion``）。
        now: 此刻。注入是为了让静默窗口可测 —— 默认取系统时钟。

    Returns:
        这条事件的记录 —— ``{"event", "at", "payload", "delivered",
        "suppressed", "error"}``。调用方靠它分辨三种结局：**送到了**
        （``delivered`` 为真）、**被静默窗口吞了**（``suppressed`` 是
        ``"quiet_hours"``）、**出错了**（``error`` 有值）。

        这三种结局对调用方不是一回事：一条「就绪」的消息被静默吞掉时，
        调用方**不能**写下「已经问过了」的标记 —— 那样这条问题就永远
        没人会收到。

    Raises:
        ValueError: ``event_type`` 不在 :data:`EVENTS` 里。
        TypeError: ``payload`` 不是映射。
    """
    if event_type not in EVENTS:
        raise ValueError(
            f"unknown framework fix event: {event_type!r}; "
            f"expected one of {sorted(EVENTS)}"
        )
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"the payload of a framework fix event must be a mapping; "
            f"got {type(payload).__name__}"
        )

    moment = _as_datetime(now) if now is not None else None
    if moment is None:
        moment = datetime.now(timezone.utc)
    body = dict(payload)
    plan_dir = body.pop("plan_dir", None)

    record: Dict[str, Any] = {
        "event": event_type,
        "at": moment.isoformat(),
        "payload": body,
        "delivered": False,
        "suppressed": None,
        "error": None,
    }

    if in_quiet_hours(moment):
        record["suppressed"] = "quiet_hours"
        _remember(record)
        logger.info(
            "framework_fix_notify_suppressed: %s: quiet hours, not delivered",
            event_type,
        )
        return record

    try:
        record["delivered"] = _deliver(event_type, body, plan_dir)
    except Exception as exc:  # noqa: BLE001 - 通知失败不影响主流程
        record["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "framework_fix_notify_failed: %s: %s: %s",
            event_type,
            type(exc).__name__,
            exc,
        )
    _remember(record)
    return record
