"""启动时的等待相位迁移：把停在 ``ready`` 的计划改写成 ``queued``。

背景
----
R10 让 ``queued`` 成为**唯一**的等待相位（见 :mod:`phase_vocabulary`）。
但存量数据里有两个地方可能还停在旧词汇 ``ready``：

* ``state.db`` 的 ``plan_routing.current_phase``；
* 每个计划目录里的遗留相位快照（``plan_state.json``）。

调度器只认 ``queued``。一个停在 ``ready`` 的计划既不会被调度器捡起来，
也不会出现在任何「在等人」的心智模型里 —— 它既没在跑，也没在等，是一个
黑洞。

为什么单独一个模块
------------------
两个存储位置可能停在不同的相位上（数据库改了、文件没改；或者反过来）。
只改一边会留下「一半」的旧词汇，下一次读到的还是 ``ready``。迁移必须把
两处**一起**收敛。

迁移会在**每次后端启动时**执行，所以它必须满足两条硬约束：

* **幂等**：跑第二遍必须是零改动、零计数。已经收敛过的数据不能被反复
  触碰（反复重写文件 = 反复改 mtime = 无谓地惊动下游的「文件变了」观察者）。
* **可收敛**：一部分失败不能把整体卡死。某个文件写失败了就记一条 warning
  跳过；下次启动按**文件当前的值**再补一遍 —— 是补齐，不是重复。

不做什么
--------
* **不建表、不改 schema**。``state.db`` 不存在、或 ``plan_routing`` 表
  不存在时原样返回零计数：迁移是**数据**层面的清扫，不是 schema 演进的
  替品。没有那张表就没有「计划」这个概念，没有计划的相位快照是无主的数据。
* **不碰 verification\\* 相位**。``verification_running`` / ``verification_*``
  这些值一个字都不动：它们有自己的生命周期，混进等待相位迁移只会把一个
  正在验证的计划拽回等待队列。
* **不动非法 JSON**。损坏的文件跳过并记 warning。它坏在迁移之前，不是
  迁移造成的；「猜」它原本该是什么值，等于往一个已经不可信的文件里写
  不可信的数据。

模块边界
--------
本模块只做**一次性数据改写**：读旧值 → 写新值 → 报计数。它不认识任何
具体 plan 的流程，因此可以在启动路径上被安全地调用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Union

from phase_vocabulary import (
    LEGACY_READY,
    QUEUED,
    normalize_waiting_phase,
)

# 遗留相位快照的文件名。**不**在本模块里再写一遍字面量：仓库里已经有一处
# 规范定义（``plan_state.PLAN_STATE_FILENAME``），而
# ``backend/tests/static_gates/test_no_json_state_filename_in_backend_source.py``
# 把「生产代码里出现该文件名」当成违规 —— 第二份字面量既会污染那个闸门的
# 噪音，也会让「文件名只有一个定义」这件事失去意义。
from plan_state import PLAN_STATE_FILENAME

__all__ = ["migrate_ready_to_queued"]


#: 本模块自己的 logger。调用方传了 ``logger`` 就用调用方的。
_LOGGER = logging.getLogger(__name__)

#: 迁移用的那条 UPDATE 语句。
#:
#: 两个字面量就是 :data:`phase_vocabulary.QUEUED` 与
#: :data:`phase_vocabulary.LEGACY_READY` 的取值 —— 它们必须一致，
#: 否则会出现「数据库改了、文件没改」的半迁移状态。漂移由
#: ``backend/tests/unit/test_phase_migration.py`` 里的
#: ``test_update_sql_matches_phase_vocabulary`` 钉住。
_UPDATE_ROUTING_SQL = (
    "UPDATE plan_routing SET current_phase='queued' WHERE current_phase='ready'"
)


def migrate_ready_to_queued(
    db_path: Union[str, Path],
    plans_dir: Union[str, Path],
    logger: Optional[logging.Logger] = None,
) -> Dict[str, int]:
    """把等待相位的旧词汇 ``ready`` 迁移成现行词汇 ``queued``。

    改写两处存储：

    1. ``state.db`` 的 ``plan_routing.current_phase``；
    2. ``plans/{id}/`` 下遗留的相位快照文件。

    幂等且可收敛：第二次调用返回两个零计数；某次写文件失败只会留下一条
    warning，下次调用按文件当前的值补齐。

    :param db_path: ``state.db`` 的路径。不存在时原样返回零计数，**不会**
        顺手把库建出来。
    :param plans_dir: 计划产物根目录。里面每个计划目录下的遗留相位快照
        都会被检查。
    :param logger: 接收告警的 logger。``None`` 时用本模块的 logger。
    :return: ``{'rows_migrated': int, 'state_files_migrated': int}`` —— 两个
        计数分别是本次改写的数据库行数与状态文件数。
    """
    log = logger or _LOGGER
    counts: Dict[str, int] = {"rows_migrated": 0, "state_files_migrated": 0}

    db_path = Path(db_path)
    plans_dir = Path(plans_dir)

    if not _routing_table_present(db_path):
        # 没有 ``plan_routing`` 表 = 没有「计划」这个概念 = 没有可迁移的
        # 等待相位。既不建表，也不去动状态文件（那会是无主数据的改动）。
        log.info(
            "[phase_migration] no plan_routing table in %s — "
            "nothing to migrate (ready → queued)",
            db_path,
        )
        return counts

    counts["rows_migrated"] = _migrate_routing_rows(db_path, log)
    counts["state_files_migrated"] = _migrate_state_files(plans_dir, log)
    return counts


def _routing_table_present(db_path: Path) -> bool:
    """``state.db`` 存在、且里面确实有 ``plan_routing`` 表吗？

    只读检查，**不建库、不建表**：``sqlite3.connect`` 对不存在的路径会
    顺手把文件建出来，所以先确认文件在，再去问表在不在。
    """
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'plan_routing'"
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def _migrate_routing_rows(db_path: Path, log: logging.Logger) -> int:
    """把 ``plan_routing`` 里 ``current_phase='ready'`` 的行改成 ``queued``。

    返回改动的行数。数据库层面的错误（锁、损坏、只读……）只记 warning：
    它们不该让整个迁移炸掉 —— 数据库会在下次启动时再被试一次，而状态
    文件那一侧是独立的，值得单独推进。
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        log.warning("[phase_migration] cannot open %s: %s", db_path, exc)
        return 0
    try:
        cursor = conn.execute(_UPDATE_ROUTING_SQL)
        rows = cursor.rowcount
        conn.commit()
    except sqlite3.Error as exc:
        log.warning(
            "[phase_migration] cannot migrate plan_routing rows in %s: %s",
            db_path,
            exc,
        )
        return 0
    finally:
        conn.close()
    return rows if rows and rows > 0 else 0


def _migrate_state_files(plans_dir: Path, log: logging.Logger) -> int:
    """把遗留相位快照里 ``current_phase='ready'`` 的改写成 ``queued``。

    返回成功改写的文件数。单个文件失败（读不了、JSON 非法、写不进去）
    只影响它自己：记一条 warning，继续处理下一个 —— 一个坏文件不能把
    整个迁移变成「什么都没做」。
    """
    if not plans_dir.is_dir():
        return 0

    migrated = 0
    for state_file in _iter_state_files(plans_dir):
        try:
            original = state_file.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "[phase_migration] cannot read %s: %s — skipped", state_file, exc
            )
            continue

        try:
            payload = json.loads(original)
        except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
            log.warning(
                "[phase_migration] %s is not valid JSON (%s) — skipped",
                state_file,
                exc,
            )
            continue

        if not isinstance(payload, dict):
            log.warning(
                "[phase_migration] %s does not hold a JSON object — skipped",
                state_file,
            )
            continue

        phase, aliased = normalize_waiting_phase(payload.get("current_phase"))
        if not aliased:
            # 不是旧词汇：verification* 相位、以及任何其它相位都原样不动。
            continue

        payload["current_phase"] = phase
        try:
            _write_state_file(state_file, payload, original)
        except Exception as exc:  # noqa: BLE001 — 单个文件失败不该中断迁移
            log.warning(
                "[phase_migration] cannot rewrite %s (%s) — the database row "
                "is already migrated; the next run will retry this file",
                state_file,
                exc,
            )
            continue

        migrated += 1
        log.warning(
            "[phase_migration] migrated legacy phase %r → %r in %s",
            LEGACY_READY,
            QUEUED,
            state_file,
        )
    return migrated


def _iter_state_files(plans_dir: Path) -> List[Path]:
    """列出 ``plans_dir`` 下所有遗留相位快照文件（按路径排序，结果稳定）。"""
    return sorted(plans_dir.rglob(PLAN_STATE_FILENAME))


def _write_state_file(path: Path, payload: dict, original: str) -> None:
    """把 *payload* 写回 *path*；写失败时把原文放回去。

    原文末尾有没有换行会被保留 —— 迁移只该改相位值，不该顺手把文件
    的形状也改了。

    回滚这一步是「可收敛」的前提：写坏了一半的文件是**非法 JSON**，而非法
    JSON 在迁移里是被跳过的 —— 那样这个文件就永远停在旧相位上，再也没有
    一次启动能修好它。把原文放回去，下一次启动就能按文件当前的值再试
    一次。
    """
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            if original.endswith("\n"):
                handle.write("\n")
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(original)
        except Exception:  # noqa: BLE001 — 回滚失败也不能盖掉原始异常
            pass
        raise