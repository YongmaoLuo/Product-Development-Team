"""收敛率与失败分类（纯函数）。

这个模块解决什么
----------------
「80% 的计划能自我收敛」这句话，此前没有任何度量支撑 —— 没有分母、没有分子、
没有失败分类，只有一句愿望。

上游这几轮把「为什么不收敛」的原因逐个定名了：

* ``verdict_unavailable``（判定轮拿不到可用结论）
* ``repair_batch_not_drained`` / ``repair_gate_red``（修复批次没排空 / 门禁红）
* ``repair_never_drained``（修复批次没排空就被重派）

这些名字散在各个模块里，谁也没有把它们汇成一张表。这个模块就是那张表，
以及这张表上的两个数：**分母**和**分子**。

分母与分子
----------
分母 = 「进入过验证、已经有结论、且既不是人叫停也不是环境挡住」的计划。

两条排除项的理由是**它们不构成关于产品是否收敛的证据**：

* ``user_stopped`` —— 人叫停的。人本来就不指望它自己收敛。
* ``env_blocked`` —— 环境挡住的（服务没起来、依赖装不上）。把环境的问题
  算进「产品收敛率」，量出来的其实是这台机器的健康度。

分子 = 分母里 ``passed`` 的那些。

分母为 0 时 ``rate`` 返回 **None**，不返回 ``0.0``：「没有可测量的对象」和
「全都失败了」是两件不同的事，返回 0.0 会把前者伪装成后者。

六种分类
--------
``passed``
    验证通过 —— 收敛了。
``user_stopped``
    人叫停。
``env_blocked``
    环境挡住（``service_preflight_failed`` / ``environment_blocked``）。
``infra_flake``
    基础设施不稳（判定不可用/解析失败/结论不明确/过期/超时，以及修复批次
    没排空的那几种）。
``code_defect``
    代码本身不对（``repair_gate_red`` / ``repair_task_failed``）。
``unclassified``
    stop_reason 谁都没枚举过。**单独成桶**，不并进邻近分类 —— 分类表上的
    缺口正是「80% 收敛」这句话需要暴露的东西，藏起来等于把度量变成安慰剂。

判定表的顺序（先匹配先得）
--------------------------
1. ``status == 'passed'`` → ``passed``
2. ``status == 'user_stopped'``，或 ``stop_reason`` 含 ``user_stopped``
   → ``user_stopped``
3. 其余状态按 ``stop_reason`` 依次匹配：

   * 含 ``service_preflight_failed`` / ``environment_blocked`` → ``env_blocked``
   * 含 ``verdict_unavailable`` / ``verdict_parse_error`` / ``inconclusive``
     / ``stale`` / ``timeout`` → ``infra_flake``
   * 含 ``repair_never_drained`` / ``repair_batch_not_drained``
     / ``repair_exited_with_unfinished_tasks`` / ``repair_non_zero_exit``
     → ``infra_flake``
   * 含 ``repair_gate_red`` / ``repair_task_failed`` → ``code_defect``
   * 其余 → ``unclassified``

为什么 ``user_stopped`` 要从 ``stop_reason`` 也认一遍
----------------------------------------------------
生产里写的是 ``status='loop_stopped'`` + ``stop_reason='user_stopped'``
（``VerificationRepository.mark_stopped`` 就是这么落库的）。只认 status
的话，人叫停的计划会掉进 ``unclassified``，既污染分母（它本来该被排除），
又在「未收敛」清单里冒出来 —— 报告会把人自己的决定算成系统的失败。

大小写
------
所有状态与 stop_reason 一律 ``str(x).lower()`` 后比较：这些值来自 SQLite，
大小写取决于当初写它的人。归一化之后再比，分类才与写法无关。

为什么是纯函数
--------------
不读 DB、不导入 ``server``、不看环境变量。唯一的外部输入是调用方注入的
``now_iso``（给测试用）与 ``rows``。同样的输入永远给同样的输出 —— 否则
「这批到底收敛了几个」就成了一个没法复算的问题，跨进程、跨重试更是无从比对。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

__all__ = [
    "CATEGORY_PASSED",
    "CATEGORY_USER_STOPPED",
    "CATEGORY_ENV_BLOCKED",
    "CATEGORY_INFRA_FLAKE",
    "CATEGORY_CODE_DEFECT",
    "CATEGORY_UNCLASSIFIED",
    "TERMINAL_STATUSES",
    "classify_plan",
    "summarize",
]


# ---------------------------------------------------------------------------
# 六个分类
# ---------------------------------------------------------------------------

#: 验证通过 —— 收敛了。
CATEGORY_PASSED = "passed"

#: 人叫停。
CATEGORY_USER_STOPPED = "user_stopped"

#: 环境挡住。
CATEGORY_ENV_BLOCKED = "env_blocked"

#: 基础设施不稳：判定不可用 / 修复批次没排空。
CATEGORY_INFRA_FLAKE = "infra_flake"

#: 代码本身不对。
CATEGORY_CODE_DEFECT = "code_defect"

#: stop_reason 谁都没枚举过 —— 单独成桶，不并进邻近分类。
CATEGORY_UNCLASSIFIED = "unclassified"


#: 有终态的 verification_status。``passed`` / ``failed`` / ``loop_stopped``
#: 是生产里实际落库的终态（``backend/verification_loop.py`` 的
#: ``_TERMINAL_VERIFICATION_STATUSES``），``user_stopped`` 是这个模块额外
#: 认的一种终态写法。
#:
#: 不在里面的（``running`` / ``pending`` …）一律算「还在跑」：没出结论的
#: 计划既不进分母，也不进 by_category，更不该出现在「未收敛」清单里。
TERMINAL_STATUSES = frozenset(
    {"passed", "failed", "loop_stopped", "user_stopped"}
)


#: 有序判定表：先匹配先得。每一项是 (分类, [stop_reason 子串, ...])。
_STOP_REASON_RULES = (
    (
        CATEGORY_ENV_BLOCKED,
        ("service_preflight_failed", "environment_blocked"),
    ),
    (
        CATEGORY_INFRA_FLAKE,
        (
            "verdict_unavailable",
            "verdict_parse_error",
            "inconclusive",
            "stale",
            "timeout",
        ),
    ),
    (
        CATEGORY_INFRA_FLAKE,
        (
            "repair_never_drained",
            "repair_batch_not_drained",
            "repair_exited_with_unfinished_tasks",
            "repair_non_zero_exit",
        ),
    ),
    (
        CATEGORY_CODE_DEFECT,
        ("repair_gate_red", "repair_task_failed"),
    ),
)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    """把一个状态 / stop_reason 归一化成可比较的小写串。

    ``None`` → 空串（不是 ``'none'`` —— 那会让一个空值看起来像一个叫
    ``none`` 的理由）。其余一律 ``str(x).strip().lower()``。
    """
    if value is None:
        return ""
    return str(value).strip().lower()


def _parse_dt(value: Any) -> Optional[datetime]:
    """解析一个 ISO 时间戳；解析不了返回 ``None``。

    没有时区的一律当 UTC —— 行里的时间戳来自 SQLite，生产侧写的就是
    UTC。带时区偏移的保留偏移（比较时会统一到同一时点）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now(now_iso: Optional[str]) -> datetime:
    """当前时刻；``now_iso`` 供测试注入。"""
    if now_iso is None:
        return datetime.now(timezone.utc)
    parsed = _parse_dt(now_iso)
    if parsed is None:
        raise ValueError(f"now_iso is not a parsable timestamp: {now_iso!r}")
    return parsed


def _sort_key(updated: Optional[datetime]) -> tuple:
    """按时间倒序排序用的键；没有时间的排最后（最旧）。"""
    if updated is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    return (1, updated)


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------


def classify_plan(row: Mapping[str, Any]) -> str:
    """把一行计划归到一个分类里。

    :param row: 至少含 ``verification_status`` 与
        ``verification_stop_reason``（可以缺省为 ``None``）的映射。
    :return: ``passed`` / ``user_stopped`` / ``env_blocked`` /
        ``infra_flake`` / ``code_defect`` / ``unclassified`` 之一。
    """
    status = _norm(row.get("verification_status"))
    reason = _norm(row.get("verification_stop_reason"))

    # 1) 通过 —— 先匹配先得，哪怕它同时带着一个 stop_reason。
    if status == CATEGORY_PASSED:
        return CATEGORY_PASSED

    # 2) 人叫停。status 与 stop_reason 两种写法都认（见模块 docstring）。
    if status == CATEGORY_USER_STOPPED or "user_stopped" in reason:
        return CATEGORY_USER_STOPPED

    # 3) 其余状态按 stop_reason 走有序判定表。
    for category, tokens in _STOP_REASON_RULES:
        for token in tokens:
            if token in reason:
                return category

    # 4) 谁都没枚举过。
    return CATEGORY_UNCLASSIFIED


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def summarize(
    rows: Optional[Iterable[Mapping[str, Any]]],
    window_days: Optional[int],
    recent_n: int = 10,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """算一段窗口内的收敛率与失败分类。

    :param rows: 计划行。每行至少有 ``plan_id`` / ``verification_status``
        / ``verification_stop_reason`` / ``updated_at``。
    :param window_days: 窗口长度（天）。窗口是 ``updated_at >= now -
        window_days``，**边界含**。``None`` 表示不按时间过滤。
    :param recent_n: ``unconverged`` 清单最多保留多少条（按 ``updated_at``
        倒序取最近的这些）。
    :param now_iso: 注入「现在」的时刻（测试用）；``None`` 表示取系统当前
        时间。
    :return: 收敛率 dict，字段见 ``summarize`` 的返回值说明。
    """
    now = _now(now_iso)
    cutoff = None if window_days is None else now - timedelta(days=window_days)

    # --- 窗口过滤：row['updated_at'] >= now - window_days -------------------
    in_window: List[Dict[str, Any]] = []
    for row in rows or []:
        updated = _parse_dt(row.get("updated_at"))
        if cutoff is not None and (updated is None or updated < cutoff):
            # 时间读不出来的行不进窗口：没法证明它落在窗口里，就不拿它
            # 当证据。
            continue
        in_window.append({"row": row, "updated": updated})

    # --- 分类 ---------------------------------------------------------------
    entered = len(in_window)
    concluded = 0
    by_category: Dict[str, int] = {}
    denominator = 0
    numerator = 0
    unconverged: List[Dict[str, Any]] = []

    for item in in_window:
        row = item["row"]
        status = _norm(row.get("verification_status"))
        if status not in TERMINAL_STATUSES:
            # 还在跑：进了验证，但还没出结论。
            continue

        concluded += 1
        category = classify_plan(row)

        # by_category 统计全部已出结论的行（user_stopped / env_blocked
        # 都算），但 passed 不是一种失败分类，不进这张表。
        if category != CATEGORY_PASSED:
            by_category[category] = by_category.get(category, 0) + 1

        if category not in (CATEGORY_USER_STOPPED, CATEGORY_ENV_BLOCKED):
            denominator += 1
            if category == CATEGORY_PASSED:
                numerator += 1

        if category not in (CATEGORY_PASSED, CATEGORY_USER_STOPPED):
            unconverged.append(
                {
                    "plan_id": row.get("plan_id"),
                    "category": category,
                    "_updated": item["updated"],
                }
            )

    # --- 未收敛清单：最近的 recent_n 条 ------------------------------------
    unconverged.sort(key=lambda e: _sort_key(e["_updated"]), reverse=True)
    unconverged_trimmed = [
        {"plan_id": e["plan_id"], "category": e["category"]}
        for e in unconverged[: max(0, int(recent_n))]
    ]

    return {
        "entered": entered,
        "concluded": concluded,
        "running": entered - concluded,
        "denominator": denominator,
        "numerator": numerator,
        # 分母为 0 → 不可知（None），不是 0.0。
        "rate": (numerator / denominator) if denominator else None,
        "by_category": {k: by_category[k] for k in sorted(by_category)},
        "unconverged": unconverged_trimmed,
    }