"""「CI 入口由项目自己决定」——两条通道都不再要求 ``scripts/ci_gate.sh``。

背景
----
框架曾经把「项目必须有一份可执行的 ``scripts/ci_gate.sh``」当成**基本条件**：
导入通道（``POST /api/plan/import``）在项目没有这份脚本时答 422，启动通道
（``POST /api/execution/{plan_id}/start``）在导入通道产生的计划上没有这份
脚本时答 400。那道门禁把「框架认得的那个脚本」和「项目自己的 CI 入口」混成
了一件事 —— 而后者本来就是项目的决定：同一套框架还要跑别的项目（比如 CC
Switch），它们各自的 CI 入口不叫 ``scripts/ci_gate.sh``。

现在那条门禁没有了：**CI 入口是什么、放在哪儿，是项目自己的决定**。框架只
做两件它一直做的事 —— 计划里 ``ci_entry`` 必填、且必须是单条脚本调用；
判定依据是那条入口的退出码。

本文件从**生产入口**钉住这件事（真的发请求给挂载好的 app，不直接调内部函
数）：

* 导入通道：项目里**没有** ``scripts/ci_gate.sh`` → 不再 422，照常落到
  ``queued``；
* 启动通道：导入通道产生的计划、项目里**没有** ``scripts/ci_gate.sh`` →
  不再 400，照常启动（执行器真的被 spawn 了）。

两个用例都从**生产入口**进去，所以「门禁还在」这件事在这两个用例里是藏不住
的：门禁只要还在，导入通道就会答 422、启动通道就会答 400。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)


client = TestClient(server.app)

PLAN_ID = "fixture-project-owns-ci-entry"

#: 导入通道把上游材料逐字写进 ``plans/<id>/import.json`` —— 这是「这个计划
#: 来自导入通道」的唯一凭据。
IMPORT_PAYLOAD_FILENAME = "import.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    """RED 探针会真的去跑生成出来的测试命令。"""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def checkout(tmp_path):
    """一个项目检出目录 —— 有任务要改的文件，**没有**任何 CI 脚本。

    「没有 ``scripts/ci_gate.sh``」正是这个夹具要摆出来的形状：框架不再
    要求它存在。
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    return path


@pytest.fixture
def plans_root(tmp_path):
    """这次测试要写进去的计划树（已被自动夹具重定向到 ``tmp_path``）。"""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _tasks_payload():
    """一份模型可能给出的、且能通过审计的任务列表。"""
    return {
        "requirement": "add a daily grouped bar chart to the reporting view",
        "tasks": [
            {
                "id": "T1",
                "title": "add the chart renderer",
                "description": "## 背景\nthe reporting view has no chart",
                "files_to_modify": ["backend/chart.py"],
                "test_command": "pytest -q backend/tests/test_chart.py",
            },
            {
                "id": "T2",
                "title": "wire the renderer into the view",
                "description": "## 目标\nmount the renderer",
                "files_to_modify": ["backend/reporting_view.py"],
                "test_command": "pytest -q",
                "depends_on": ["T1"],
            },
        ],
    }


class _FakeCodingTool:
    """顶替 ``CodingTool``：把一份脚本好的答案交给生成器。"""

    def __init__(self, reply):
        self._reply = reply
        self.json_calls = []
        self.query_calls = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        return self._reply

    def query(self, prompt: str) -> str:
        self.query_calls.append(prompt)
        return json.dumps(
            {"findings": [], "fixed_content": self._reply},
            ensure_ascii=False,
        )


def _install_tool(monkeypatch, reply=None):
    """把 ``server.create_coding_tool`` 指到一个假工具上。"""
    tool = _FakeCodingTool(reply if reply is not None else _tasks_payload())
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


def _post(body):
    return client.post("/api/plan/import", json=body)


# ---------------------------------------------------------------------------
# 1) 导入通道：项目没有 CI 脚本，照常落到 queued
# ---------------------------------------------------------------------------


def test_import_accepts_a_project_with_no_ci_script(
    checkout, plans_root, monkeypatch, state_db_reader
):
    """导入通道不再问「这个项目的 CI 入口在哪」。

    夹具里的 checkout **没有** ``scripts/ci_gate.sh`` —— 断言的是：这一次
    请求没有被门禁挡下来，计划照常落到 ``queued``。
    """
    assert not (checkout / "scripts" / "ci_gate.sh").exists()
    _install_tool(monkeypatch)

    response = _post({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane import",
    })

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    assert (plans_root / plan_id / "tasks.json").exists()
    assert state_db_reader.routing(plan_id)["current_phase"] == "queued"
    assert state_db_reader.execution(plan_id) is None


# ---------------------------------------------------------------------------
# 2) 启动通道：导入通道的计划 + 没有 CI 脚本 → 照常启动
# ---------------------------------------------------------------------------


class _DormantThread:
    """``threading.Thread`` 的替身 —— 从不真的起线程。"""

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


@pytest.fixture
def start_env(monkeypatch, tmp_path):
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
def spawns(monkeypatch):
    """记录每一次执行器 spawn；真的一个也不 spawn。"""
    calls: List[Dict[str, Any]] = []

    def _spawn(**kwargs):
        calls.append(kwargs)
        return _FakeSpawnedProc(), Path(str(kwargs.get("log_path") or "/dev/null"))

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _spawn)
    return calls


def _seed_imported_plan(plans_dir: Path, db_path: Path) -> Path:
    """一份「来自导入通道」的计划：tasks、plan_state、routing 行、import.json。

    计划目录里的 ``project/`` **没有** ``scripts/ci_gate.sh`` —— 启动通道
    不再因为这件事拒绝它。
    """
    plan_dir = plans_dir / PLAN_ID
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
            "plan_id": PLAN_ID,
            "current_phase": "queued",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )
    (plan_dir / IMPORT_PAYLOAD_FILENAME).write_text(
        json.dumps({"tasks": [{"title": "imported task"}]}),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "queued")
    finally:
        conn.close()

    return project_dir


def test_start_accepts_an_imported_plan_with_no_ci_script(start_env, spawns):
    """启动通道不再问「这个项目的 CI 入口在哪」。

    计划来自导入通道（有 ``import.json``），项目里**没有**
    ``scripts/ci_gate.sh`` —— 启动照常发生：执行器被 spawn 了。
    """
    plans_dir, db_path = start_env
    project_dir = _seed_imported_plan(plans_dir, db_path)
    assert not (project_dir / "scripts" / "ci_gate.sh").exists()

    response = client.post(
        f"/api/execution/{PLAN_ID}/start",
        json={"project_dir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    assert len(spawns) == 1, spawns
