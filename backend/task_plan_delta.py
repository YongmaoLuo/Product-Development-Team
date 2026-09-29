"""执行任务列表的**增量**调整（人工介入用，2026-09-16）。

解决的问题
----------
验证侧早就有"标记作废 + 新增"的能力
(:mod:`verification_plan_delta`)：VP 可以 ``obsolete``（标记而非删除，
保留审计痕迹）也可以 ``add``，但**修改已有 VP 一律拒绝**。执行侧没有
对应的东西 —— 于是一个失败的执行任务只有两种归宿：

* 被修好（前提是它还能被调度、内容还完整）；或
* 永远挂在卡片上当红字（一个失败的计划里可能同时挂着若干条）。

不要"清掉"问题（那是掩盖），要能**作废 + 新增**——
作废留痕，新增有依据，原任务一个字都不改。

为什么只有两个动作
------------------
和验证侧同理。任务记录是执行痕迹：改写一条已有任务的 title /
description / test_command，会让它此前挣到的 ``completed`` 结论、
commit checkpoint、失败原因全部对不上号，审计链断掉。所以：

* ``add`` —— 新增任务（**必须有 reason**）
* ``obsolete`` —— 标记作废，**不删除**，写 ``reason`` 与轮次

任何一个动作试图改动已有任务 → 记入 ``rejected_modifications``，
不执行：不做人工审批流程、不设数量上限，但必须可回溯。

``obsolete`` 落成 ``plan_tasks.status = "superseded"`` —— 该状态本来
就在 ``_TERMINAL_TASK_STATUSES`` 里，dispatcher 不会再调度它，卡片会
用 ``db_orphan_terminal`` 那条路径把它显示出来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.atomic_io import atomic_write_json

#: 作废原因写在 ``plan_tasks.failure_reason`` 上（表里没有专门的
#: ``obsolete_reason`` 列）。加这个前缀是为了让操作者一眼区分
#: "人工作废" 和 "执行失败" —— 两者都会走 terminal 的展示路径。
OBSOLETE_REASON_PREFIX = "[obsolete] "

#: 作废轮次同样没有列可放，随原因一起写进 ``failure_reason`` 的尾部。
#: 保持可解析，便于以后有列了再迁移。
_OBSOLETE_ROUND_MARKER = " (round={round})"


@dataclass
class TaskDelta:
    """增量调整结果。

    ``rejected_*`` 是审计用：调用方（人或 LLM）试图越界时在这里留痕，
    必须可回溯。
    """

    added: List[Dict[str, Any]] = field(default_factory=list)
    obsoleted: List[Dict[str, Any]] = field(default_factory=list)
    rejected_additions: List[Dict[str, Any]] = field(default_factory=list)
    rejected_obsoletes: List[Dict[str, Any]] = field(default_factory=list)
    rejected_modifications: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.obsoleted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": list(self.added),
            "obsoleted": list(self.obsoleted),
            "rejected_additions": list(self.rejected_additions),
            "rejected_obsoletes": list(self.rejected_obsoletes),
            "rejected_modifications": list(self.rejected_modifications),
        }


# ---------------------------------------------------------------------------
# 解析 + 硬约束
# ---------------------------------------------------------------------------


def parse_task_delta_payload(
    raw: Any,
    existing_task_ids: set,
    already_obsolete: Optional[set] = None,
) -> TaskDelta:
    """把请求体规整成 :class:`TaskDelta`，并在此处执行硬约束。

    就地执行的约束（与验证侧逐条对齐）：

    * ``add`` 带 ``id`` 且该 id 已存在 → 视为"试图修改已有任务"，拒绝，
      记入 ``rejected_modifications``。
    * ``add`` 缺 ``title`` → 拒绝。
    * ``add`` 缺 ``reason`` → 拒绝（"新增必须有依据"）。
    * ``add`` 缺 ``test_command`` → 拒绝。执行侧的完成判定是双信号，
      没有命令就退化成只看模型自述；生成一条注定不可验证的任务不是
      "新增"，是欠债。这是执行侧比 VP 侧更严的一条。
    * ``obsolete`` 指向不存在的 id → 拒绝。
    * ``obsolete`` 已作废 → 幂等跳过。
    * ``obsolete`` 缺 ``reason`` → 拒绝。
    """
    delta = TaskDelta(raw=raw if isinstance(raw, dict) else {})
    if not isinstance(raw, dict):
        return delta

    existing_ids = {str(t) for t in (existing_task_ids or set())}
    obsolete_ids = {str(t) for t in (already_obsolete or set())}

    for item in raw.get("add") or []:
        if not isinstance(item, dict):
            continue
        if not str(item.get("title", "")).strip():
            delta.rejected_additions.append({"item": item, "why": "缺少 title"})
            continue
        item_id = str(item.get("id", "")).strip()
        if item_id and item_id in existing_ids:
            delta.rejected_modifications.append({
                "id": item_id,
                "item": item,
                "why": "试图复用已有 task id（等价于修改已有任务）",
            })
            continue
        if not str(item.get("reason", "")).strip():
            delta.rejected_additions.append(
                {"item": item, "why": "缺少 reason（新增必须有依据）"}
            )
            continue
        if not str(item.get("test_command", "")).strip():
            delta.rejected_additions.append({
                "item": item,
                "why": (
                    "缺少 test_command —— 执行任务的完成判定要交叉验证"
                    "命令退出码，没有命令只能靠模型自述"
                ),
            })
            continue
        delta.added.append(dict(item))

    for item in raw.get("obsolete") or []:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id", "")).strip()
        if not task_id or task_id not in existing_ids:
            delta.rejected_obsoletes.append(
                {"item": item, "why": f"obsolete 指向不存在的任务：{task_id!r}"}
            )
            continue
        if task_id in obsolete_ids:
            continue  # 幂等：已经作废过的不重复记
        if not str(item.get("reason", "")).strip():
            delta.rejected_obsoletes.append(
                {"item": item, "why": "obsolete 缺少 reason"}
            )
            continue
        delta.obsoleted.append({
            "id": task_id, "reason": str(item["reason"]).strip(),
        })

    return delta


def rejection_summary(delta: TaskDelta) -> Dict[str, int]:
    """``apply`` 摘要里那三个计数，单独抽出来便于两处调用点共用。"""
    return {
        "rejected_additions": len(delta.rejected_additions),
        "rejected_obsoletes": len(delta.rejected_obsoletes),
        "rejected_modifications": len(delta.rejected_modifications),
    }


# ---------------------------------------------------------------------------
# id 分配
# ---------------------------------------------------------------------------


def next_manual_task_id(existing_ids: set, round_number: int = 0) -> str:
    """分配一个不与现有任务冲突的新 id。

    形如 ``manual-r0-01``；``round_number`` 为 0 时省略轮次段，写成
    ``manual-01``。只用 ASCII：``plan_tasks.task_id`` 有
    ``_SAFE_TASK_ID_RE`` 校验，且 refiner / refiner-split 的 id 规则
    也都限定在这个字符集内。
    """
    prefix = f"manual-r{round_number}" if round_number else "manual"
    highest = 0
    for tid in existing_ids or set():
        tid = str(tid)
        if not tid.startswith(prefix + "-"):
            continue
        tail = tid[len(prefix) + 1:]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}-{highest + 1:02d}"


def obsolete_failure_reason(reason: str, round_number: int = 0) -> str:
    """渲染写进 ``failure_reason`` 的作废标记。

    前缀是给操作者看的（区分人工作废 vs 执行失败），尾部的 round 段
    是给以后迁移用的。``record_task_failure`` 截断到 1000 字符，这里
    先限长以免前缀被截掉。
    """
    body = (reason or "").strip()[:800]
    text = f"{OBSOLETE_REASON_PREFIX}{body}"
    if round_number:
        text += _OBSOLETE_ROUND_MARKER.format(round=round_number)
    return text


def is_obsolete_reason(value: Any) -> bool:
    """True when ``failure_reason`` 是人工作废标记而非执行失败。"""
    return str(value or "").startswith(OBSOLETE_REASON_PREFIX)


# ---------------------------------------------------------------------------
# 审计记录
# ---------------------------------------------------------------------------

_DELTA_LOG_FILENAME = "task_delta_log.json"


def append_delta_log(plan_dir: Path, entry: Dict[str, Any]) -> None:
    """把一次增量调整追加到 ``task_delta_log.json``。

    与验证侧不同：任务的作废原因和轮次在 ``plan_tasks`` 里没有独立的
    列，只能挤在 ``failure_reason`` 上。完整的审计记录（谁、什么时候、
    依据是什么、新增了什么）落在这里。

    写不进去不影响主流程（与 ``verification_plan_delta.save_delta_state``
    同样的取舍），但会打 stderr。
    """
    import sys
    from datetime import datetime

    path = Path(plan_dir) / _DELTA_LOG_FILENAME
    try:
        existing: List[Any] = []
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        record = dict(entry)
        record.setdefault("ts", datetime.utcnow().isoformat() + "Z")
        existing.append(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[task_plan_delta] could not append {path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def load_delta_log(plan_dir: Path) -> List[Dict[str, Any]]:
    path = Path(plan_dir) / _DELTA_LOG_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


def apply_task_delta(
    *,
    repo: Any,
    plan_id: str,
    plan_dir: Path,
    existing_tasks: List[Dict[str, Any]],
    delta: TaskDelta,
    round_number: int = 0,
    actor: str = "operator",
) -> Dict[str, Any]:
    """把通过硬约束的增量写进 ``plan_tasks`` 与 ``tasks.json``。

    两个动作各自落两处，因为执行期的任务有两个真相来源：

    * ``obsolete`` → ``plan_tasks.status = "superseded"``（终态，
      dispatcher 不再调度）+ ``failure_reason`` 里的作废标记。**不改**
      任务的任何静态字段，也不从 ``tasks.json`` 里删除它 —— 要求
      留痕。``tasks.json`` 不动：那份文件是静态定义，状态归 SQLite。
    * ``add`` → ``repo.add_task``（单写者路径）+ 追加进 ``tasks.json``，
      否则下一次 ``_load_tasks`` 从磁盘重读时新任务就消失了。

    ``repo`` 是已打开的 :class:`PlanTaskRepository`；调用方负责事务与
    连接生命周期。返回可直接写进响应的摘要。
    """
    summary: Dict[str, Any] = {
        "added": [],
        "obsoleted": [],
        **rejection_summary(delta),
    }

    # ---- obsolete -------------------------------------------------------
    for item in delta.obsoleted:
        task_id = item["id"]
        reason = obsolete_failure_reason(item["reason"], round_number)
        repo.update_task(
            plan_id=plan_id,
            task_id=task_id,
            fields={
                "status": "superseded",
                "failure_reason": reason,
            },
            expected_version=0,
        )
        summary["obsoleted"].append({
            "id": task_id, "reason": item["reason"],
        })

    # ---- add ------------------------------------------------------------
    all_ids = {str(t.get("id")) for t in existing_tasks if isinstance(t, dict)}
    all_ids |= {str(a.get("id")) for a in summary["added"] if a.get("id")}

    new_task_dicts: List[Dict[str, Any]] = []
    intra_batch_rejections: List[Dict[str, Any]] = []
    for item in delta.added:
        task_id = str(item.get("id", "")).strip() or next_manual_task_id(
            all_ids, round_number,
        )
        if task_id in all_ids:
            # parse_task_delta_payload 已经拦过一次；这里是第二道，防止
            # 同一批 add 内部撞 id（或自动分配的 id 撞上已有任务）。
            intra_batch_rejections.append({
                "id": task_id,
                "why": "id 与已有任务或本批次内其它新增任务冲突",
            })
            continue

        task_dict: Dict[str, Any] = {
            "id": task_id,
            "title": str(item.get("title", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "test_command": str(item.get("test_command", "")).strip(),
            "files_to_modify": item.get("files_to_modify") or [],
            "depends_on": item.get("depends_on") or [],
            "model_type": item.get("model_type") or "medium",
            "project_dir": item.get("project_dir"),
        }
        if item.get("round") is not None:
            task_dict["round"] = item["round"]
        elif round_number:
            task_dict["round"] = round_number

        # ``project_dir`` 为 None 时 add_task 会写 NULL；SubTask 的
        # 默认工厂随后用 agent 的 project_dir 兜底，与 refiner 新增
        # 任务的行为一致。
        repo.add_task(plan_id=plan_id, task_dict=task_dict, expected_version=0)

        disk_entry = {k: v for k, v in task_dict.items() if v is not None}
        disk_entry.setdefault("status", "pending")
        new_task_dicts.append(disk_entry)
        all_ids.add(task_id)

        summary["added"].append({
            "id": task_id,
            "title": task_dict["title"],
            "reason": str(item.get("reason", "")).strip(),
            "test_command": task_dict["test_command"],
        })

    if new_task_dicts:
        _append_tasks_json(Path(plan_dir) / "tasks.json", new_task_dicts)

    summary["rejected_modifications"] += len(intra_batch_rejections)
    if intra_batch_rejections:
        summary["intra_batch_rejections"] = intra_batch_rejections

    append_delta_log(Path(plan_dir), {
        "actor": actor,
        "round": round_number,
        "summary": {
            "added": summary["added"],
            "obsoleted": summary["obsoleted"],
        },
        "rejected": {
            "additions": delta.rejected_additions,
            "obsoletes": delta.rejected_obsoletes,
            "modifications": (
                delta.rejected_modifications + intra_batch_rejections
            ),
        },
    })

    return summary


def _append_tasks_json(tasks_file: Path, new_tasks: List[Dict[str, Any]]) -> None:
    """把新任务追加进 ``tasks.json``（读不到就从空表开始）。

    与 ``repair_generator._append_to_tasks_json`` 同样的契约：保留已有
    任务，写并集。这里额外做了原子替换，避免写一半崩溃留下截断的文件
    —— 那份文件是 dispatcher 的启动输入。

    临时名必须 **唯一**：早期这里手写了一个固定的
    ``tasks.json.tmp``，和另外两个写者（``task_manager`` /
    ``routes.execution``）用的是同一个名字。两个写者同时走到
    ``replace`` 时，先到的那个已经把临时文件搬走了，后到的那个
    ``replace`` 找不到源文件，抛 ``ENOENT``，把一个本来健康的任务
    判成失败（2026-09-26 任务 4 就是这么死的）。
    ``atomic_write_json`` 用 ``mkstemp`` 生成唯一临时名，各写者互不
    干扰。

    注意：唯一临时名只保证 **不写出半个文件**。它不解决
    read-modify-write 的丢更新——两个并发追加者各读一份旧内容、
    各写一次，后写的会覆盖先写的。真要并发追加，得先收敛到单一写者。
    """
    existing: Dict[str, Any] = {"tasks": []}
    try:
        if tasks_file.exists():
            loaded = json.loads(tasks_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
            elif isinstance(loaded, list):
                existing = {"tasks": loaded}
    except (OSError, json.JSONDecodeError):
        existing = {"tasks": []}

    tasks = existing.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    tasks.extend(new_tasks)
    existing["tasks"] = tasks

    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(tasks_file, existing, indent=2, reraise=True)
