"""等待相位迁移（``ready`` → ``queued``）的行为契约。

被钉住的行为
------------
1. **两处一起改**：``plan_routing.current_phase`` 与遗留的相位快照文件
   都从 ``ready`` 变成 ``queued``。
2. **幂等**：第二次调用两个计数都是 0，数据不再被改动。
3. **verification\\* 一律不动**：等待相位的清扫不能把正在验证的计划拽回
   等待队列。
4. **部分失败可收敛**：数据库改了但文件写失败时，只留一条 warning；下次
   调用按文件当前的值补齐 —— 补齐，不是重复。
5. **边界不炸**：``state.db`` 不存在、或 ``plan_routing`` 表不存在时返回
   零计数，不建表、不建库、不抛异常；非法 JSON 的文件被跳过。
6. **SQL 与词汇模块不漂移**：迁移用的 UPDATE 语句里那两个值必须就是
   ``QUEUED`` / ``LEGACY_READY``。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import phase_migration
from phase_migration import migrate_ready_to_queued
from phase_vocabulary import LEGACY_READY, QUEUED
from state_machine.db.schema import migrate as migrate_schema

#: 遗留相位快照的文件名（测试侧可以直接写文件名）。
_STATE_FILENAME = "plan_state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(db_path: Path, rows: dict) -> None:
    """建一个带真实 schema 的 ``state.db``，并插入若干 ``plan_routing`` 行。

    用真实的 ``migrate()`` 建表，而不是手写 DDL —— 这样列名/约束跟生产
    一致，测试不会在「测试里的表」和「真正的表」之间漂移。
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        migrate_schema(conn)
        for plan_id, phase in rows.items():
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES (?, ?, ?)",
                (plan_id, phase, "2026-10-25T00:00:00Z"),
            )
    finally:
        conn.close()


def _make_state_file(plans_dir: Path, plan_id: str, phase: str) -> Path:
    """在 ``plans_dir/plan_id/`` 下写一个遗留相位快照文件。"""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / _STATE_FILENAME
    path.write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}, indent=2),
        encoding="utf-8",
    )
    return path


def _routing_phase(db_path: Path, plan_id: str) -> str:
    """读回 ``plan_routing`` 里某行的 ``current_phase``。"""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no plan_routing row for {plan_id}"
    return row[0]


def _file_phase(state_file: Path) -> str:
    """读回相位快照文件里的 ``current_phase``。"""
    return json.loads(state_file.read_text(encoding="utf-8"))["current_phase"]


# ---------------------------------------------------------------------------
# 1. 两处一起改
# ---------------------------------------------------------------------------


def test_migrates_routing_row_and_state_file(tmp_path: Path) -> None:
    """数据库行与相位快照文件都从 ``ready`` 变成 ``queued``。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_db(db_path, {"plan-a": LEGACY_READY})
    state_file = _make_state_file(plans_dir, "plan-a", LEGACY_READY)

    result = migrate_ready_to_queued(db_path, plans_dir)

    assert result == {"rows_migrated": 1, "state_files_migrated": 1}
    assert _routing_phase(db_path, "plan-a") == QUEUED
    assert _file_phase(state_file) == QUEUED


# ---------------------------------------------------------------------------
# 2. 幂等
# ---------------------------------------------------------------------------


def test_second_run_is_noop(tmp_path: Path) -> None:
    """第二次调用两个计数都是 0，数据保持 ``queued``。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_db(db_path, {"plan-a": LEGACY_READY})
    state_file = _make_state_file(plans_dir, "plan-a", LEGACY_READY)

    first = migrate_ready_to_queued(db_path, plans_dir)
    assert first == {"rows_migrated": 1, "state_files_migrated": 1}

    second = migrate_ready_to_queued(db_path, plans_dir)

    assert second == {"rows_migrated": 0, "state_files_migrated": 0}
    assert _routing_phase(db_path, "plan-a") == QUEUED
    assert _file_phase(state_file) == QUEUED


# ---------------------------------------------------------------------------
# 3. verification* 一律不动
# ---------------------------------------------------------------------------


def test_verification_phase_untouched(tmp_path: Path) -> None:
    """``verification_running`` 既不进数据库也不进文件。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_db(db_path, {"plan-v": "verification_running"})
    state_file = _make_state_file(plans_dir, "plan-v", "verification_running")

    result = migrate_ready_to_queued(db_path, plans_dir)

    assert result == {"rows_migrated": 0, "state_files_migrated": 0}
    assert _routing_phase(db_path, "plan-v") == "verification_running"
    assert _file_phase(state_file) == "verification_running"


# ---------------------------------------------------------------------------
# 4. 部分失败可收敛
# ---------------------------------------------------------------------------


def test_partial_failure_converges_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件写失败后重跑能补齐：数据库已改，文件按当前值再迁一次。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_db(db_path, {"plan-a": LEGACY_READY})
    state_file = _make_state_file(plans_dir, "plan-a", LEGACY_READY)

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(phase_migration, "_write_state_file", boom)

    first = migrate_ready_to_queued(db_path, plans_dir)

    # 数据库那一步已经成功，文件那一步失败 —— 半迁移状态。
    assert first == {"rows_migrated": 1, "state_files_migrated": 0}
    assert _routing_phase(db_path, "plan-a") == QUEUED
    assert _file_phase(state_file) == LEGACY_READY

    monkeypatch.undo()

    second = migrate_ready_to_queued(db_path, plans_dir)

    # 第二次：数据库那一步无事可做（已经是 queued），文件按当前值补齐。
    assert second == {"rows_migrated": 0, "state_files_migrated": 1}
    assert _file_phase(state_file) == QUEUED


def test_failed_write_leaves_state_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写坏了不能留下半个文件 —— 半个文件是非法 JSON，会被永远跳过。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_db(db_path, {"plan-a": LEGACY_READY})
    state_file = _make_state_file(plans_dir, "plan-a", LEGACY_READY)
    original = state_file.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(json, "dump", boom)

    result = migrate_ready_to_queued(db_path, plans_dir)

    assert result["state_files_migrated"] == 0
    assert state_file.read_text(encoding="utf-8") == original
    assert _file_phase(state_file) == LEGACY_READY


# ---------------------------------------------------------------------------
# 5. 边界条件
# ---------------------------------------------------------------------------


def test_missing_db_returns_zero_counts_without_creating_it(tmp_path: Path) -> None:
    """``state.db`` 不存在 → 零计数，而且不把库建出来。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_state_file(plans_dir, "plan-a", LEGACY_READY)

    result = migrate_ready_to_queued(db_path, plans_dir)

    assert result == {"rows_migrated": 0, "state_files_migrated": 0}
    assert not db_path.exists()


def test_missing_routing_table_returns_zero_counts(tmp_path: Path) -> None:
    """``plan_routing`` 表不存在 → 零计数，既不建表也不动状态文件。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    sqlite3.connect(str(db_path)).close()  # 库在，表不在
    state_file = _make_state_file(plans_dir, "plan-a", LEGACY_READY)

    result = migrate_ready_to_queued(db_path, plans_dir)

    assert result == {"rows_migrated": 0, "state_files_migrated": 0}
    assert _file_phase(state_file) == LEGACY_READY


def test_invalid_json_state_file_is_skipped(tmp_path: Path) -> None:
    """非法 JSON 的文件被跳过，不抛异常，也不影响其它文件。"""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    _make_db(db_path, {"plan-a": LEGACY_READY})
    broken = _make_state_file(plans_dir, "plan-broken", LEGACY_READY)
    broken.write_text("{not json at all", encoding="utf-8")
    healthy = _make_state_file(plans_dir, "plan-a", LEGACY_READY)

    result = migrate_ready_to_queued(db_path, plans_dir)

    assert result == {"rows_migrated": 1, "state_files_migrated": 1}
    assert broken.read_text(encoding="utf-8") == "{not json at all"
    assert _file_phase(healthy) == QUEUED


# ---------------------------------------------------------------------------
# 6. SQL 与词汇模块不漂移
# ---------------------------------------------------------------------------


def test_update_sql_matches_phase_vocabulary() -> None:
    """迁移用的 UPDATE 语句里的两个字面量必须就是词汇模块的取值。"""
    sql = phase_migration._UPDATE_ROUTING_SQL
    assert f"current_phase='{QUEUED}'" in sql
    assert f"current_phase='{LEGACY_READY}'" in sql