"""启动源相位集合与终态重跑：只认 ``queued``。

R10 把 ``queued`` 变成**唯一**的等待相位之后，写路径不再产生 ``ready``。
于是读路径必须同时改口，否则会造出一批「谁也启动不了、谁也认领不了」
的行：

* :data:`server._EXECUTION_START_SOURCE_PHASES` —— ``POST
  /api/execution/{id}/start`` 的 CAS 允许从哪些相位起步。这份白名单是
  启动的唯一入口判据；留着 ``ready`` 就等于给一个已经不再被写出来的
  相位留了一条后门。
* ``routes/execution.py`` 的终态重跑 —— 从 ``completed`` /
  ``verification_passed`` / ``verification_loop_stopped`` 重跑时把计划
  强制放回哪个相位。放回 ``ready`` 会造出一行**启动不了**的计划：CAS
  的白名单只认 ``queued``，而这条行既不在白名单里、又没有别的路径能
  把它推出去。

这三个断言合起来钉住的是同一件事：**从「等人」到「开跑」只有
``queued`` 一条路**，中间不留一个半截状态。

边界
----
* 集合的每个成员都必须是 ``plan_state.VALID_PHASES`` 的成员 —— 否则 CAS
  会拿一个语法上非法的相位去比数据库列。
* 重入源（``failed`` / ``completed`` / ``stopped`` /
  ``verification_*``）保持不变 —— 终态重跑是既有能力，不在本次改动
  范围内。
* 存量库里停在 ``ready`` 的行，启动必须得到 **409**（不是 500）：这是
  「这个相位现在不能起步」，而不是「服务器炸了」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import _EXECUTION_START_SOURCE_PHASES


def test_source_phases_contain_queued_not_ready():
    """启动源集合认 ``queued``，不认 ``ready``。"""
    assert "queued" in _EXECUTION_START_SOURCE_PHASES, (
        "the scheduler's inbox phase must stay startable"
    )
    assert "ready" not in _EXECUTION_START_SOURCE_PHASES, (
        "'ready' is a legacy alias that the write path no longer "
        "produces; keeping it in the start whitelist leaves a door open "
        "to rows nobody can produce"
    )


def test_every_source_phase_is_valid():
    """白名单里的每个相位都必须是合法相位。"""
    from plan_state import VALID_PHASES

    for phase in _EXECUTION_START_SOURCE_PHASES:
        assert phase in VALID_PHASES, (
            f"start source phase {phase!r} is not a valid phase; "
            f"valid phases are {VALID_PHASES}"
        )


def test_terminal_reexecution_forces_queued():
    """终态重跑把计划放回 ``queued``，不放回 ``ready``。"""
    import routes.execution as execution_module

    source = Path(execution_module.__file__).read_text(encoding="utf-8")

    assert 'force_set_phase("queued")' in source, (
        "terminal re-execution must force the plan back to 'queued' — "
        "the phase the start whitelist accepts"
    )
    assert 'force_set_phase("ready")' not in source, (
        "'ready' is no longer a startable phase; forcing it there "
        "creates a row that can never be started again"
    )


@pytest.fixture
def legacy_ready_plan(tmp_path, monkeypatch):
    """一个停在旧相位 ``ready`` 的计划：路由行 + 任务文件 + 可用工作目录。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    plan_id = "vp001-legacy-ready-start-phase"
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = tmp_path / "state.db"
    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "ready")
    finally:
        conn.close()

    monkeypatch.setattr("server.PLANS_DIR", plans_dir)
    monkeypatch.setattr("server._state_db_path", lambda: str(db_path))

    # A start that is NOT refused would spawn a real executor process.
    # The point of this fixture is that the start is refused — so make a
    # spawn loud instead of letting it leak out of the test.
    def _no_spawn(*args, **kwargs):
        raise AssertionError(
            "the start was expected to be refused before the spawn"
        )

    monkeypatch.setattr("routes.execution.subprocess.Popen", _no_spawn)

    return plan_id, project_dir


def test_start_from_legacy_ready_row_is_409(legacy_ready_plan):
    """停在 ``ready`` 的存量行：启动得到 409，不是 500。"""
    plan_id, project_dir = legacy_ready_plan

    client = TestClient(server.app)
    try:
        response = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )

        assert response.status_code == 409, (
            f"a start from a legacy 'ready' row must be refused with 409, "
            f"got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body.get("error") == "conflict", body
        assert body.get("reason") == "stage_mismatch", body
    finally:
        server._execution_state.pop(plan_id, None)
