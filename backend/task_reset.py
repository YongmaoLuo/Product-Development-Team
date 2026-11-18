"""重置单个任务的**执行记账** —— 纯逻辑层（2026-11-12）。

这个模块只做一件事：把一条失败（或已完成）的执行任务重新排队的
**记账部分**擦干净，让它重新变成 ``pending``。它不碰路由、不碰
SQLite、也不碰 ``tasks.json`` —— 只有纯函数 + 一个审计日志的读写，
所以它可以被单测钉住（见 ``backend/tests/unit/test_task_reset.py``）。

重置写什么 / 不写什么
----------------------
**写（执行记账，5 个字段）**::

    status          → "pending"
    failure_reason  → None
    end_ts          → None
    commit_sha      → None
    attempt         → 0        （把重试预算还回去）

**不写（规格，一个字节都不动）**::

    title / description / files_to_modify / test_command /
    depends_on / project_dir

为什么这不违反「task 数据只能被 voided 或 created」那条约束
-----------------------------------------------------------
仓库里有一条成文的约束：**task 数据只能被 voided 或 created，不能被
改写**。它写在 ``backend/routes/verification.py`` 里 verification reset
那个端点的 docstring 表格中（``plan_tasks`` 那行：pending ``RP-*`` →
``voided``，never deleted: task data may only be voided or created,
never rewritten）。

这条约束保护的是任务的**规格** —— 标题、描述、要改哪些文件、测试命令。
规格一旦写定，审计链就靠它对得上号：一条任务此前挣到的 ``completed``
结论、commit checkpoint、失败原因，全都要能和"它当时要做什么"对上。
把规格改掉，等于让历史结论悬空。

而 ``status`` / ``failure_reason`` / ``end_ts`` / ``commit_sha`` /
``attempt`` 是**执行记账**，不是规格。记账描述的是"上一次跑成了什么"，
重置它们**不改变这个任务要做什么** —— 要做什么由上面那 6 个规格字段
决定，一字节未动。因此重置执行记账**不违反**那条约束：被 voided 或
created 说的是"任务实体本身"，而这里改的是任务身上的一次运行留下的
痕迹。

这也是为什么 :func:`build_reset_fields` 只返回 5 个键、并且
:data:`SPEC_FIELDS` 与它没有任何交集 —— 那条"一个字节都不动"是代码
结构保证的，不是靠调用方自觉。

审计
----
每次成功重置往 ``plans/<plan_id>/task_reset_log.json`` 追加一条::

    {at, task_id, from_status, to_status, reason, caller, forced}

这个文件**只增不改**，和 ``task_delta_log.json`` 并列。写入用
:func:`utils.atomic_io.atomic_write_json` 整表重写（原子替换），
``reraise=True`` —— 审计写不进去必须让调用方知道，不能静默吞掉
``OSError``（静默吞掉 = 一次没有记录的重置）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator

from utils.atomic_io import atomic_write_json

#: 审计日志文件名（落在 plan 目录下）。
RESET_LOG_FILENAME = "task_reset_log.json"

#: **执行记账**字段 —— 重置只写这些。
BOOKKEEPING_FIELDS = (
    "status",
    "failure_reason",
    "end_ts",
    "commit_sha",
    "attempt",
)

#: **规格**字段 —— 重置一个字节都不许动（见模块 docstring）。
SPEC_FIELDS = (
    "title",
    "description",
    "files_to_modify",
    "test_command",
    "depends_on",
    "project_dir",
)

#: ``reason`` 的最大长度。超出部分截掉，不报错 —— 理由是给人看的，
#: 不是要把它当成数据通道。
MAX_REASON_LEN = 800


def build_reset_fields() -> Dict[str, Any]:
    """返回重置要写入的执行记账字段。

    恰好 5 个键，全部落在 :data:`BOOKKEEPING_FIELDS` 里，与
    :data:`SPEC_FIELDS` 无交集。``attempt`` 归零是为了把完整的重试
    预算还给这条任务 —— 否则它会带着上一轮的失败计数直接出局。
    """
    return {
        "status": "pending",
        "failure_reason": None,
        "end_ts": None,
        "commit_sha": None,
        "attempt": 0,
    }


def cleared_payload(before: Dict[str, Any]) -> Dict[str, Any]:
    """回显重置**之前**被清掉的那些字段的值。

    响应里要告诉操作者"到底清掉了什么"，所以这里返回重置前那一行的
    记账字段原值。缺失的键读作 ``None`` —— 不抛 ``KeyError``：
    一条从没跑过的任务本来就没有 ``end_ts``，那不是错误。

    只回显 :data:`BOOKKEEPING_FIELDS` 这 5 个键；规格字段不在回显里，
    因为它们没有被改动。
    """
    source = before if isinstance(before, dict) else {}
    return {field: source.get(field) for field in BOOKKEEPING_FIELDS}


def _utc_now() -> str:
    """当前 UTC 时间，``2026-11-12T01:00:00Z`` 这种形状。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_raw_entries(plan_dir: Path, *, strict: bool = False) -> List[Any]:
    """读审计日志的原始 list（不过滤条目类型）。

    文件不存在、不是 JSON、或者顶层不是 list —— 一律当作"空日志"。
    这是给**写入**路径用的：重写整表时不该把文件里已有的条目悄悄
    丢掉，所以这里保留原样，交给 :func:`read_reset_log` 去决定哪些
    条目算"可读的审计记录"。

    ``strict=True`` 是给**追加**路径用的：文件在、却读不出来（权限、
    IO 错误）时必须抛给调用方 —— 读不出来就意味着"历史可能还在"，
    这时候把它当成空日志重写，等于把审计历史抹了。
    """
    path = Path(plan_dir) / RESET_LOG_FILENAME
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        if strict:
            raise
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        # JSONDecodeError 与 UnicodeDecodeError 都是 ValueError。
        # 坏 JSON 不是日志 —— 没有可保全的历史，按空日志重来。
        return []
    return list(data) if isinstance(data, list) else []


def read_reset_log(plan_dir: Path) -> List[Dict[str, Any]]:
    """读审计日志。文件不存在或内容不是 list → ``[]``，不抛异常。

    只返回 dict 形态的条目：非 dict 的条目是坏数据，不是审计记录。
    """
    return [entry for entry in _read_raw_entries(plan_dir) if isinstance(entry, dict)]


def append_reset_log(plan_dir: Path, entry: Dict[str, Any]) -> None:
    """往 ``task_reset_log.json`` 追加一条审计记录。

    整表重写：读出已有条目、append 新条目、原子写回。原子替换
    （``mkstemp`` + ``os.replace``）保证不会出现写了一半的日志文件。

    ``at`` 由这里盖上 UTC 时间戳（调用方自带就保留自带的）。其余
    字段原样落盘 —— 审计记录由调用方负责填齐（``task_id`` /
    ``from_status`` / ``to_status`` / ``reason`` / ``caller`` /
    ``forced``）。

    ``reraise=True``：写不进去必须抛给调用方，不能静默吞掉
    ``OSError`` —— 一次没有落盘的审计等于一次没发生过的重置。
    """
    path = Path(plan_dir) / RESET_LOG_FILENAME
    records = _read_raw_entries(plan_dir, strict=True)
    record = dict(entry)
    record.setdefault("at", _utc_now())
    records.append(record)
    atomic_write_json(path, records, indent=2, reraise=True)


class TaskResetRequest(BaseModel):
    """重置请求体。

    ``reason`` 是操作者给出的理由，写进审计记录；可选，去空白后最多
    保留 800 字符（理由是给人看的，不是数据通道）。

    ``force`` 表示"我知道我在做什么"：重跑一条 ``completed`` 的任务
    是破坏性的，需要显式要求。
    """

    reason: Optional[str] = None
    force: bool = False

    @field_validator("reason", mode="after")
    @classmethod
    def _trim_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip()[:MAX_REASON_LEN]