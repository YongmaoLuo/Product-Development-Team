"""TDD spec —— repair 写队列方法 ``_persist_repair_tasks``（任务 9）。

背景
----
repair 任务的写入逻辑原本**内联**在 ``check_cycle_conditions`` 里
（``orchestrator.py`` 约 5256-5384 行）：开库、迁移、逐条
``PlanTaskRepository.add_task``、写一条成功日志、失败时吞掉异常并
warning。下游的「根因预检」（任务 10）需要**复用同一条写入路径** ——
两条写路径必然漂移：预检写出来的行少一个字段、或者带了 ``status``，
执行器的孤儿回收就认不出它。

本任务把写入逻辑抽成可复用的方法：

    ``_persist_repair_tasks(repair_tasks: list[dict], round_number: int)
    -> list[str]``

返回**写下去的 id 列表**（没写进去的不在里面）。

边界条件
--------
* **空列表** → 不写、返回 ``[]``（连库都不开）。
* **重复 id** → 跳过并 warning（同一批里同一个 id 只写一次；先到的
  那条赢）。重复的 id 在同一个 plan 里定位的是**同一行**
  （``plan_tasks`` 的主键是 ``(plan_id, task_id)``），写两遍等于把
  同一行改两遍。
* **写入异常** → **抛给调用方，不吞**。这条是本方法与调用方之间的
  契约：预检需要知道「这一批没写进去」，而 ``check_cycle_conditions``
  需要的是「别把这一轮搞崩」。两者的处置不一样，所以**吞不吞由调用方
  决定**，方法本身只负责如实上报。
* 抽出后 ``check_cycle_conditions`` 的写入行为与重构前**逐字节一致**
  ——它自己留着 try/except，失败时仍然只 warning、仍然把内存里的
  ``repair_tasks`` 返回给调用者。

TDD contract —— 4 cases
-----------------------
1. ``test_persist_repair_tasks_writes_rows`` —— 写进 state.db，返回
   写下去的 id。
2. ``test_empty_list_is_noop`` —— 空列表 → 不写、返回 ``[]``。
3. ``test_duplicate_id_skipped`` —— 重复 id → 跳过 + warning。
4. ``test_write_failure_propagates`` —— 写入异常 → 抛给调用方。

测试命令::

    pytest backend/tests/unit/verification/test_persist_repair_tasks_refactor.py -v
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


PLAN_ID = "p1"

#: 重复 id 被跳过时打的那条日志的事件名。
DUPLICATE_EVENT = "verification_repair_task_duplicate_id"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _orchestrator(tmp_path: Path) -> VerificationOrchestrator:
    """An orchestrator with only the directories the write path needs.

    ``_persist_repair_tasks`` touches nothing else: it opens
    ``state.db``, writes the rows and closes the connection.  It needs
    the plan's own directory (the plan id is its name) and the project
    directory (not used by the write path, but every orchestrator has
    one).
    """
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_dir = plan_dir
    orch.project_dir = tmp_path
    return orch


def _rows(plan_id: str = PLAN_ID) -> dict:
    """Every ``plan_tasks`` row of the plan, read back from state.db."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        return PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 写进 state.db，返回写下去的 id
# ---------------------------------------------------------------------------
def test_persist_repair_tasks_writes_rows(tmp_path):
    """任务书给的那条输入 → 写一行，返回那个 id。"""
    orch = _orchestrator(tmp_path)

    written = orch._persist_repair_tasks(
        [{"id": "repair-1", "title": "修 X", "files_to_modify": ["backend/a.py"]}],
        2,
    )

    assert written == ["repair-1"]

    rows = _rows()
    assert set(rows.keys()) == {"repair-1"}, (
        f"the row must be in state.db: {sorted(rows.keys())!r}"
    )
    entry = rows["repair-1"]
    assert entry["title"] == "修 X"
    assert entry["files_to_modify"] == ["backend/a.py"]
    # ``add_task`` 自己盖的默认值 —— 执行器的孤儿回收按它认人。
    assert entry["status"] == "pending"
    assert entry["_repo_version"] == 1


def test_persist_repair_tasks_writes_every_row(tmp_path):
    """一批多条 → 每条都写进去，返回的 id 顺序跟输入一致。"""
    orch = _orchestrator(tmp_path)

    written = orch._persist_repair_tasks(
        [
            {"id": "repair-1", "title": "修 X"},
            {"id": "repair-2", "title": "修 Y"},
        ],
        1,
    )

    assert written == ["repair-1", "repair-2"]
    assert set(_rows().keys()) == {"repair-1", "repair-2"}


def test_persist_repair_tasks_drops_fields_the_table_cannot_hold(tmp_path):
    """表不认识的字段被丢掉，而不是让整批写失败。

    ``add_task`` **拒绝**它不认识的字段（``TaskProgressValidationError``），
    不会替调用方过滤。写入路径因此先把载荷收窄到表自己的白名单。
    """
    orch = _orchestrator(tmp_path)

    written = orch._persist_repair_tasks(
        [{"id": "repair-1", "title": "修 X", "phantom_field": "BAD"}],
        1,
    )

    assert written == ["repair-1"]
    assert set(_rows().keys()) == {"repair-1"}


# ---------------------------------------------------------------------------
# 2. 空列表 → 不写
# ---------------------------------------------------------------------------
def test_empty_list_is_noop(tmp_path):
    """空列表 → 不写一行，返回 ``[]``。"""
    orch = _orchestrator(tmp_path)

    assert orch._persist_repair_tasks([], 1) == []

    assert _rows() == {}, "an empty batch must not write a row"


# ---------------------------------------------------------------------------
# 3. 重复 id → 跳过 + warning
# ---------------------------------------------------------------------------
def test_duplicate_id_skipped(tmp_path, caplog):
    """同一批里同一个 id → 只写一次，重复的那条被跳过并 warning。"""
    orch = _orchestrator(tmp_path)

    with caplog.at_level(logging.WARNING, logger="verification.orchestrator"):
        written = orch._persist_repair_tasks(
            [
                {"id": "repair-1", "title": "修 X"},
                {"id": "repair-1", "title": "修 X（重复）"},
            ],
            1,
        )

    assert written == ["repair-1"], "the duplicate must not be reported as written"

    rows = _rows()
    assert set(rows.keys()) == {"repair-1"}
    # 先到的那条赢 —— 重复的那条不覆盖它。
    assert rows["repair-1"]["title"] == "修 X"
    # 只写过一次：版本号没有因为第二次写而 +1。
    assert rows["repair-1"]["_repo_version"] == 1

    assert DUPLICATE_EVENT in caplog.text, (
        f"a skipped duplicate must be warned about: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# 4. 写入异常 → 抛给调用方
# ---------------------------------------------------------------------------
def test_write_failure_propagates(tmp_path, monkeypatch):
    """写不进去 → **抛出去**。吞不吞由调用方决定，不由方法决定。"""
    from state_machine.repositories import plan_task_repository

    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(
        plan_task_repository.PlanTaskRepository, "add_task", _boom,
    )
    orch = _orchestrator(tmp_path)

    with pytest.raises(RuntimeError, match="disk on fire"):
        orch._persist_repair_tasks([{"id": "repair-1", "title": "修 X"}], 1)
