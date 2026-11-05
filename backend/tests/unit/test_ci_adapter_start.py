"""启动通道的 CI 适配脚本门禁（2026-11-05）。

背景
----
导入通道（``POST /api/plan/import``）产生的计划，跑在一个框架并不认识的
项目上：调用方指着**自己的** checkout 进来，希望门禁在那个仓库里跑。如果
那个项目没有可执行的 ``scripts/ci_gate.sh``，这次运行就没有任何东西可以
判定它 —— 跑完了也说不清绿还是红。所以启动被**拒绝**，而不是 spawn 一个
判不了的执行器。

判定规则只有一份定义，在 :mod:`ci_adapter` 里（导入通道与启动通道共用）。
本文件只钉住启动通道的行为：

* 缺适配脚本 → HTTP 400，detail 含 ``ci_adapter_missing``；
* 拒绝**不 spawn、不写 ``plan_execution`` 行、不动相位**（相位不变是
  「拒绝发生在 routing CAS 之前」的可观测后果）；
* 没有 ``import.json`` 的计划**不触发**这道检查 —— PRD 通道与历史计划
  保持既有的启动契约（「无预运行门禁」，见
  ``backend/tests/contract/test_no_prerun_gate.py``）；
* 有可执行的适配脚本 → 照常启动。

判断「这个计划来自导入通道」的依据是**计划目录里有没有 ``import.json``**：
导入通道把上游材料逐字写进 ``plans/<id>/import.json``（见
``fast_lane.store_import_payload``），别的通道不写这个文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import server
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import RoutingRepository


PLAN_ID = "fixture-ci-adapter-start"

#: 计划目录里代表「来自导入通道」的那个成员 —— 导入通道把上游材料逐字
#: 写进 ``plans/<id>/import.json``。门禁按这个名字找那份材料。
IMPORT_PAYLOAD_FILENAME = "import.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _install_adapter(project_dir: Path, mode: int = 0o755) -> Path:
    """在 ``project_dir`` 下装一份 CI 适配脚本，并显式设置权限位。"""
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    adapter = scripts_dir / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(mode)
    return adapter


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    imported: bool = True,
) -> tuple:
    """一份「可以启动」的计划：tasks、plan_state、routing 行。

    故意**不**建 ``plan_execution`` 行 —— 拒绝之后这张表必须仍然是空的，
    那正是「拒绝不写 exec 行」这条契约要看的（拒绝的落点只能是响应体）。

    ``imported=True`` 时计划目录里多一份 ``import.json``：它是「这个计划
    来自导入通道」的唯一凭据。
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "first task"}]}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "queued",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )
    if imported:
        (plan_dir / IMPORT_PAYLOAD_FILENAME).write_text(
            json.dumps({"tasks": [{"title": "imported task"}]}),
            encoding="utf-8",
        )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
    finally:
        conn.close()

    return plan_dir, project_dir


def _read_routing_phase(plan_id: str, db_path: Path) -> Optional[str]:
    """相位行此刻带着的相位。"""
    conn = open_db(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _exec_rows(db_path: Path) -> List[tuple]:
    """``plan_execution`` 表里的全部行。"""
    conn = open_db(db_path)
    try:
        migrate(conn)
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT plan_id, exec_status, exec_pid, stop_reason "
                "FROM plan_execution"
            ).fetchall()
        ]
    finally:
        conn.close()


class _DormantThread:
    """``threading.Thread`` 的替身 —— 从不真的起线程。

    路由在 spawn 之后起一个守护线程盯着执行器。本文件要测的是 spawn
    **之前**的判定，所以那个线程没有事情可做；真的起一个线程只会让它在
    测试结束后继续操作这个计划。
    """

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


class _FakeSpawnedProc:
    """被 spawn 出来的执行器子进程 —— 一个替身，从不是一个真进程。"""

    def __init__(self) -> None:
        self.pid = 5150
        self.returncode = 0

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    """每个用例自己的 PLANS_DIR + state.db。"""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)
    server._execution_state.clear()
    yield plans_dir, db_path
    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def spawns(monkeypatch):
    """记录每一次执行器 spawn；真的一个也不 spawn。

    ``server._spawn_executor_subprocess`` **就是**
    ``routes/execution.py::_spawn_executor_subprocess`` —— 执行器子进程
    唯一诞生的地方。
    """
    calls: List[Dict[str, Any]] = []

    def _spawn(**kwargs):
        calls.append(kwargs)
        return _FakeSpawnedProc(), Path(str(kwargs.get("log_path") or "/dev/null"))

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _spawn)
    return calls


# ---------------------------------------------------------------------------
# 1) 缺适配脚本 → 拒绝启动
# ---------------------------------------------------------------------------


def test_start_refuses_imported_plan_without_adapter(env, client, spawns):
    """导入通道的计划，项目里没有适配脚本 → 400，原因点名缺什么。

    拒绝是 fail-close：跑起来也判不了绿红，就不跑。
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, imported=True)

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "ci_adapter_missing" in detail, resp.text
    assert spawns == [], "the refused start spawned an executor anyway"
    assert _read_routing_phase(PLAN_ID, db_path) == "queued", (
        "the refusal moved the plan's phase; a plan that is not running "
        "must not read as 'executing'"
    )


def test_refused_start_creates_no_exec_row(env, client, spawns):
    """拒绝之后 ``plan_execution`` 表里没有新行。

    拒绝的落点只能是响应体：一次没跑起来的启动不能留下一条「跑过」的
    痕迹，否则每个读这张表的消费者都会以为这个计划跑过。
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, imported=True)
    assert _exec_rows(db_path) == [], "the fixture seeded an execution row"

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 400, resp.text
    assert _exec_rows(db_path) == [], (
        "the refused start wrote an execution row"
    )
    assert spawns == [], "the refused start spawned an executor anyway"


def test_plan_without_import_payload_is_not_checked(env, client, spawns):
    """没有 ``import.json`` 的计划不触发这道检查。

    PRD 通道与导入通道之前的历史计划保持既有的启动契约 —— 「无预运行
    门禁」（``backend/tests/contract/test_no_prerun_gate.py``）。这道门禁
    只针对导入通道的计划。
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, imported=False)
    assert not (plan_dir / IMPORT_PAYLOAD_FILENAME).exists()

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, resp.text
    assert len(spawns) == 1, spawns


# ---------------------------------------------------------------------------
# 2) 边界条件
# ---------------------------------------------------------------------------


def test_imported_plan_with_executable_adapter_starts(env, client, spawns):
    """有可执行的适配脚本 → 照常启动（门禁不是「导入通道一律拒绝」）。"""
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, imported=True)
    _install_adapter(project_dir)

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert resp.status_code == 200, resp.text
    assert len(spawns) == 1, spawns


def test_broken_project_dir_is_not_masked_by_the_adapter_gate(env, client, spawns):
    """``project_dir`` 本身不合法时，答的还是它自己的 400。

    两道检查都拒接，但顺序是「先说 project_dir 不对」：一个指向不存在
    目录的请求，缺适配脚本只是它的第二个问题 —— 先说第二个，会让操作员
    去补一个根本没提过的脚本。
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, imported=True)
    absent = plan_dir / "no-such-project"

    resp = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(absent)},
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "does not exist" in detail, resp.text
    assert "ci_adapter_missing" not in detail, resp.text


def test_gate_looks_for_the_file_the_import_lane_writes():
    """门禁找的那份材料，就是导入通道写下的那份。

    门禁按字面量 ``import.json`` 找计划目录里的上游材料。这条用例是
    漂移报警：导入通道改了文件名，这里立刻红，而不是让门禁悄悄地对每
    一个计划都不生效。
    """
    from fast_lane import IMPORT_PAYLOAD_FILENAME as written_by_import_lane

    assert written_by_import_lane == IMPORT_PAYLOAD_FILENAME
