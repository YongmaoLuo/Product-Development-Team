"""计划去重报告的落盘与开关（2026-11-09, R3 / R4）。

这个模块解决什么
----------------
重复检测此前只会**说**（判定成立），不会**留痕**（把结论写下来）。判定结果
只活在一次函数调用的返回值里，操作员看不到、下一轮也读不到 —— 于是"整仓
门禁在计划里出现了两次"这件事，除了生成这一轮的人记得，没别的证据。

本模块把结论落成**计划目录里的两份文件**：

* ``verification_plan_dedup.json`` —— 验证点侧的完整报告（重复组 + 空泛判据
  + 一行摘要）；
* ``verification_plan_dedup.txt`` —— 一行摘要，给人 ``cat`` 用的；
* ``tasks_dedup.json`` —— 任务侧的报告（重复组 + 一行摘要）。

为什么**默认开启**
------------------
这类检查的失败模式是"没人看"：关着不写，等于没做。所以开关
:data:`ENV_SWITCH` **默认开启**（没设这个变量 = 开着），只有在明确写出
``0 / false / off / no`` 时才关掉。

为什么**干净的计划不落盘**
------------------------
零重复、零空泛判据 → **不写文件**、返回 ``None``。计划目录里凭空多一份
空报告，会让"目录里有报告"这件事失去意义 —— 读的人分不清"查过了没问题"
与"压根没查"。

为什么**不含时间戳**
--------------------
报告是要被 diff、被审的产物。里面掺一个时间戳，每次 diff 都脏，
"这次改动引入了新的重复"这句话就再也说不清了。序列化口径固定为
``sort_keys=True, indent=2, ensure_ascii=False`` + 末尾换行：键序固定才能
diff，中文不转义才读得懂，同一份输入两次落盘**逐字节相同**。

判定与组装在 :mod:`plan_dedup`（纯函数），写盘与开关在这里。分开是有意的：
判据错了和"要不要写"错了，是两种失败模式，混在一起就没法单独测其中一边。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from plan_dedup import build_tasks_report, build_verification_report

logger = logging.getLogger(__name__)

#: 控制去重检查开关的环境变量名。
ENV_SWITCH = "PDT_PLAN_DEDUP"

#: 这些值表示**关闭**（大小写、首尾空白不敏感）。
#:
#: 只有这四个值关闭；其余一切取值（包括没设、设成空串、设成没见过的词）
#: 都按**开启**处理 —— 这条检查的失败模式是"没人看"，不是"误伤"。
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

#: 这些值表示**开启**。它们是"显式开启"的写法，不是白名单 —— 白名单会把
#: 没见过的取值悄悄变成关闭。
ENABLED_VALUES = frozenset({"1", "true", "on"})

#: 验证点侧报告的文件名。
VERIFICATION_REPORT_JSON = "verification_plan_dedup.json"
VERIFICATION_REPORT_TEXT = "verification_plan_dedup.txt"

#: 任务侧报告的文件名。
TASKS_REPORT_JSON = "tasks_dedup.json"


def dedup_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """去重检查是否开启（**默认开启**）。

    读 :data:`ENV_SWITCH`（``PDT_PLAN_DEDUP``）：

    * **没设** → 开启（这条检查默认跑）；
    * ``1 / true / on`` → 开启；
    * ``0 / false / off / no`` → 关闭。

    大小写与首尾空白不敏感（``"OFF"``、``" 0 "`` 都是关闭）。

    参数 ``environ`` 是给调用方注入环境用的（测试直接喂一个 dict，不必改
    进程环境）；``None`` 表示读进程自己的 ``os.environ``。**每次调用都重新
    读** —— 模块级常量会让这个开关没法在测试里改。
    """
    env = os.environ if environ is None else environ
    try:
        raw = env.get(ENV_SWITCH)
    except AttributeError:  # pragma: no cover - 防御性：非 Mapping 的怪对象
        return True
    if raw is None:
        return True
    value = str(raw).strip().lower()
    if value in DISABLED_VALUES:
        return False
    return True


def serialize_report(report: Mapping[str, Any]) -> str:
    """把报告序列化成**固定的**文本形态。

    口径固定为 ``sort_keys=True, indent=2, ensure_ascii=False`` + 末尾换行：

    * ``sort_keys`` —— 键序固定，报告之间才能 diff；
    * ``indent=2`` —— 人能直接读；
    * ``ensure_ascii=False`` —— 中文不写成 ``\\uXXXX``；
    * 末尾换行 —— 文件以完整行结束（``cat`` / ``tail`` 都友好）。

    **不含时间戳**：同一份输入两次落盘必须逐字节相同。
    """
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _write_text(path: Path, content: str) -> None:
    """写一个 UTF-8 文本文件，行尾固定为 ``\\n``。"""
    path.write_text(content, encoding="utf-8", newline="\n")


def _verification_points(plan_data: Any) -> Sequence[Mapping[str, Any]]:
    """从计划数据里取出验证点列表。

    ``plan_data`` 通常是计划字典（``{"verification_points": [...]}``）；也接受
    裸列表（有些调用方手里就是那一个列表）。``vps`` 是历史计划里的另一个
    键名，一并认掉 —— 漏掉它就有一类计划检不出重复。
    """
    if isinstance(plan_data, Mapping):
        for key in ("verification_points", "vps"):
            points = plan_data.get(key)
            if points:
                return points
        return ()
    if isinstance(plan_data, (list, tuple)):
        return plan_data
    return ()


def _tasks(tasks_data: Any) -> Sequence[Mapping[str, Any]]:
    """从任务数据里取出任务列表。

    ``tasks_data`` 通常是 ``tasks.json`` 的内容（``{"tasks": [...]}``）；也接受
    裸列表。
    """
    if isinstance(tasks_data, Mapping):
        tasks = tasks_data.get("tasks")
        if tasks:
            return tasks
        return ()
    if isinstance(tasks_data, (list, tuple)):
        return tasks_data
    return ()


def _member_ids(report: Mapping[str, Any]) -> List[str]:
    """报告里所有重复组的成员 id（去重、保序）。"""
    collected: List[str] = []
    for group in report.get("duplicate_groups") or ():
        for member_id in group.get("member_ids") or ():
            text = str(member_id).strip() if member_id is not None else ""
            if text and text not in collected:
                collected.append(text)
    return collected


def write_verification_dedup_report(
    plan_data: Mapping[str, Any],
    plan_dir: Path,
) -> Optional[Dict[str, Any]]:
    """把验证点的去重结果写进计划目录，返回报告（或 ``None``）。

    写两个文件：

    * ``verification_plan_dedup.json`` —— 完整报告；
    * ``verification_plan_dedup.txt`` —— 一行摘要。

    * **开关关闭** → 什么都不写，返回 ``None``；
    * **零重复且零空泛判据** → 什么都不写，返回 ``None``；
    * 有发现 → 写盘 + 打**一条** WARNING（含摘要与成员 id），返回报告。

    WARNING 是因为操作员不一定去翻计划目录 —— 日志是唯一一定被看到的地方。
    """
    if not dedup_enabled():
        return None

    report = build_verification_report(_verification_points(plan_data))
    if not report.get("duplicate_groups") and not report.get("vague_criteria"):
        return None

    directory = Path(plan_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_text(directory / VERIFICATION_REPORT_JSON, serialize_report(report))
    _write_text(
        directory / VERIFICATION_REPORT_TEXT,
        str(report.get("summary") or "") + "\n",
    )

    logger.warning(f"[plan_dedup] {report.get('summary') or ''}")
    return report


def write_tasks_dedup_report(
    tasks_data: Mapping[str, Any],
    plan_dir: Path,
) -> Optional[Dict[str, Any]]:
    """把任务的去重结果写进计划目录，返回报告（或 ``None``）。

    写一个文件：``tasks_dedup.json``（重复组 + 一行摘要）。

    * **开关关闭** → 什么都不写，返回 ``None``；
    * **零重复组** → 什么都不写，返回 ``None``；
    * 有重复 → 写盘 + 打**一条** WARNING（含摘要与成员 id），返回报告。

    任务侧没有空泛判据这一段（``expected_result`` 是验证点的字段），所以
    任务侧只出一份 JSON、不出摘要文本文件 —— 摘要已经在 JSON 里。
    """
    if not dedup_enabled():
        return None

    report = build_tasks_report(_tasks(tasks_data))
    if not report.get("duplicate_groups"):
        return None

    directory = Path(plan_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_text(directory / TASKS_REPORT_JSON, serialize_report(report))

    logger.warning(f"[plan_dedup] {report.get('summary') or ''}")
    return report
