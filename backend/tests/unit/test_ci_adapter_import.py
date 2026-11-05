"""导入通道的 CI 适配脚本门禁（2026-11-05）。

「导入 fail-close」的 arch 决策点 3：**导入 fail-close**。
一个项目没有可执行的 ``scripts/ci_gate.sh`` 时，它的 CI 验收无从谈起 ——
这种计划在导入阶段就该被拒绝，而不是等到执行期才炸在「找不到门禁脚本」
上，而且炸在一个已经建了目录、已经进了队列的计划身上。

拒绝的**位置**和拒绝的**理由**一样重要：

* 位置在 ``_allocate_plan_id`` **之前** —— 被拒的导入不建计划目录、不写
  routing 行、不写队列行。一条被拒的导入不留任何痕迹：调用方回头问
  「这个计划去哪了」，答案就是「它从来没有被创建过」，而不是「它在
  ``import_failed`` 里躺着」。
* 理由用 :mod:`ci_adapter` 的那一份文本（``ci_adapter_missing: …``）——
  导入通道与启动通道共用同一个判定函数，不各写一份文案。两条链路给出
  同一个前缀，调用方按前缀识别就够。

本文件钉住四件事：

* 没有适配脚本 → 422，响应体点名 ``ci_adapter_missing``；
* 被拒 → routing 表与队列表都没有新行；
* 有可执行适配脚本 → 走原来的 ``queued`` 流程；
* 负控：同一个项目，加脚本 → ``queued``，删脚本 → 422，再加回来 →
  ``queued``。删掉脚本这一下必须让同一个请求变红 —— 否则「422」只证明
  了「这个目录没有脚本」，没证明「门禁在看这个目录」。

判定规则本身（文件存在 + 有执行位）由
``backend/tests/unit/test_ci_adapter_helper.py`` 钉住，本文件只钉导入
通道用它。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from ci_adapter import CI_ADAPTER_RELATIVE_PATH  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    """RED 探针会真的去跑生成出来的测试命令。"""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def checkout(tmp_path):
    """一个项目检出目录 —— 有任务要改的文件，没有 CI 适配脚本。"""
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


def _install_adapter(project_dir: Path, mode: int = 0o755) -> Path:
    """在项目里装一份 CI 适配脚本，并显式设置权限位。"""
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    adapter = scripts_dir / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(mode)
    return adapter


def _remove_adapter(project_dir: Path) -> None:
    """把适配脚本从项目里拿走（留下空的 ``scripts/`` 目录）。"""
    (project_dir / "scripts" / "ci_gate.sh").unlink()


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
    """顶替 ``CodingTool``：把两份脚本好的答案交给生成器。"""

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


def _row_count(table: str) -> int:
    """某张状态表里有多少行（只读）。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = Path(os.environ["PDT_STATE_DB_PATH"])
    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def _plan_dirs(plans_root: Path):
    """计划目录树里已有的计划目录（没有就一个也没有）。"""
    if not plans_root.exists():
        return []
    return sorted(p.name for p in plans_root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# 1 — 没有适配脚本的项目，导入被拒
# ---------------------------------------------------------------------------


def test_import_refuses_project_without_adapter(checkout, monkeypatch):
    """没有 CI 适配脚本 → 422，响应体点名缺了什么。

    拒绝的理由必须**可读**：调用方要知道的是「这个项目没有门禁脚本」，
    而不是「导入失败」。
    """
    _install_tool(monkeypatch)

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "Project has no CI adapter"
    assert body["problems"][0].startswith("ci_adapter_missing")
    assert "ci_adapter_missing" in response.text
    assert CI_ADAPTER_RELATIVE_PATH in response.text


def test_import_refuses_a_script_that_is_not_executable(checkout, monkeypatch):
    """脚本存在但没有执行位 → 同样拒绝（存在 ≠ 可执行）。"""
    _install_adapter(checkout, mode=0o644)
    assert not os.access(checkout / CI_ADAPTER_RELATIVE_PATH, os.X_OK)
    _install_tool(monkeypatch)

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422, response.text
    assert "ci_adapter_missing" in response.text


# ---------------------------------------------------------------------------
# 2 — 被拒的导入不留任何痕迹
# ---------------------------------------------------------------------------


def test_refused_import_registers_no_rows(checkout, plans_root, monkeypatch):
    """被拒 → routing 表与队列表都没有新行，计划目录也没有。

    判定发生在 ``_allocate_plan_id`` **之前**，所以拒绝之后：
    没有计划目录、没有 ``plan_routing`` 行、没有 ``scheduler_queue`` 行。
    一条被拒的导入如果留下了半成品（目录建了、行写了），它就是一个
    「看起来存在、其实跑不起来」的计划 —— 正是这次门禁要消灭的东西。
    """
    _install_tool(monkeypatch)

    routing_before = _row_count("plan_routing")
    queue_before = _row_count("scheduler_queue")
    dirs_before = _plan_dirs(plans_root)

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422, response.text
    assert _row_count("plan_routing") == routing_before
    assert _row_count("scheduler_queue") == queue_before
    assert _plan_dirs(plans_root) == dirs_before


# ---------------------------------------------------------------------------
# 3 — 有适配脚本的项目，走原来的 queued 流程
# ---------------------------------------------------------------------------


def test_import_accepts_project_with_adapter(checkout, plans_root, monkeypatch,
                                              state_db_reader):
    """有可执行的适配脚本 → 导入照旧，落在 ``queued``。"""
    _install_adapter(checkout)
    _install_tool(monkeypatch)

    response = _post({
        "project_dir": str(checkout),
        "content": "do the thing",
        "title": "Fast lane import",
    })

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    assert state_db_reader.routing(plan_id)["current_phase"] == "queued"
    assert (plans_root / plan_id / "tasks.json").exists()


# ---------------------------------------------------------------------------
# 4 — 负控：同一个项目，脚本在 → queued，脚本走 → 422，脚本回来 → queued
# ---------------------------------------------------------------------------


def test_negative_control_remove_and_restore_adapter(checkout, plans_root,
                                                      monkeypatch,
                                                      state_db_reader):
    """把适配脚本拿走必须让同一个请求变红，拿回来必须再变绿。

    没有这一条，前面的 422 只证明了「这个目录没有脚本」，没证明「门禁
    在看这个目录」—— 一条永远返回 422 的门禁也能让那些用例全绿。
    """
    _install_tool(monkeypatch)

    # 有脚本 → 绿
    _install_adapter(checkout)
    green = _post({"project_dir": str(checkout), "content": "do the thing"})
    assert green.status_code == 200, green.text
    assert state_db_reader.routing(
        green.json()["plan_id"]
    )["current_phase"] == "queued"

    # 拿走脚本 → 红
    _remove_adapter(checkout)
    red = _post({"project_dir": str(checkout), "content": "do the thing"})
    assert red.status_code == 422, red.text
    assert "ci_adapter_missing" in red.text

    # 拿回来 → 又绿
    _install_adapter(checkout)
    green_again = _post({"project_dir": str(checkout), "content": "do the thing"})
    assert green_again.status_code == 200, green_again.text
    assert state_db_reader.routing(
        green_again.json()["plan_id"]
    )["current_phase"] == "queued"
