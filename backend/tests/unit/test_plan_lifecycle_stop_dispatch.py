"""plan 级统一 stop 门面：按实时状态分派（R11 · 任务 17）。

被钉住的行为
------------
1. **验证在飞 → 停验证**，并且不碰执行。
2. **执行在飞 → 停执行**，``discard=False`` —— 「停」是暂停，不是丢弃：
   丢弃会把已经做完的活扔掉。
3. **两边都没在飞 → 不派发**：``dispatched is None``，相位保持原样。
4. **两边都在飞（异常态）→ 先停验证再停执行**：验证是外层循环，它不停，
   就会在执行被停之后又拉起新一轮。``detail == 'both'``。
5. **未知计划 → 抛既有 ``PlanNotFoundError``**，不吞成「什么都没发生」。
6. **处理器异常原样透传**：门面不 catch，调用方拿到真实的状态码。
7. **进程内直接调用处理器**：门面不碰 httpx / requests —— 走 HTTP 会把
   「同进程按实时状态分派」退化成「同进程发 HTTP 请求」，状态判断又变成
   两套。

被测的是**分派**，不是端点自己的语义：既有 stop 端点在这里被换成记录器，
端点语义另有测试钉着。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_plan_dir(plans_dir: Path, plan_id: str, phase: str = "queued") -> Path:
    """建一个计划目录，并写一份遗留相位快照（相位 = ``phase``）。"""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    return plan_dir


def _mark_execution_alive(plan_id: str) -> None:
    """把执行记录标成「在飞」（``status=running`` 且没有 ``ended_at``）。"""
    import server

    with server._execution_state_lock:
        server._execution_state[plan_id] = {"status": "running", "ended_at": None}


def _mark_verification_alive(plan_id: str) -> None:
    """把验证记录标成「在飞」（``verification_status=running``）。"""
    import server

    server._verification_state[plan_id] = {"verification_status": "running"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """把 ``server.PLANS_DIR`` 指到 tmp_path。"""
    import server

    target = tmp_path / "plans"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", target)
    return target


@pytest.fixture
def clean_state():
    """测试结束后把进程内的执行 / 验证状态恢复原样。"""
    import server

    execution_before = dict(server._execution_state)
    verification_before = dict(server._verification_state)
    yield
    server._execution_state.clear()
    server._execution_state.update(execution_before)
    server._verification_state.clear()
    server._verification_state.update(verification_before)


@pytest.fixture
def handler_calls(monkeypatch):
    """把既有 stop 端点换成记录器，返回调用记录列表。

    ``import server`` 必须排在 ``from routes import ...`` 前面：
    ``routes.execution`` / ``routes.verification`` 在模块层 import
    ``server``，而 ``server`` 在文件末尾 import 这两个 routes 模块 ——
    顺序反了就是一次半初始化的循环导入。
    """
    import server  # noqa: F401  (import 顺序是承重墙，见 docstring)

    from routes import execution as routes_execution
    from routes import verification as routes_verification

    calls = []

    def fake_stop_execution(plan_id, discard=False):
        calls.append(("execution", plan_id, discard))

    def fake_stop_verification(plan_id):
        calls.append(("verification", plan_id))

    monkeypatch.setattr(routes_execution, "stop_execution", fake_stop_execution)
    monkeypatch.setattr(
        routes_verification, "stop_verification", fake_stop_verification,
    )
    return calls


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------


def test_stop_dispatches_to_verification_when_verification_alive(
    plans_dir, clean_state, handler_calls,
):
    """验证在飞 → 停验证，执行一根都不碰。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id)
    _mark_verification_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id)

    assert handler_calls == [("verification", plan_id)]
    assert result.dispatched == "verification"
    assert result.berth_phase == "stopped"
    assert result.final_phase == "stopped"
    assert result.checkpoint is None
    assert result.detail == "stop_verification"


def test_stop_dispatches_to_execution_when_execution_alive(
    plans_dir, clean_state, handler_calls,
):
    """执行在飞 → 停执行，且是暂停（``discard=False``）不是丢弃。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id)
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id)

    assert handler_calls == [("execution", plan_id, False)]
    assert result.dispatched == "execution"
    assert result.berth_phase == "stopped"
    assert result.final_phase == "stopped"
    assert result.checkpoint is None
    assert result.detail == "stop_execution"


def test_stop_on_idle_plan_does_not_dispatch(
    plans_dir, clean_state, handler_calls,
):
    """两边都没在飞 → 不派发，相位保持原样。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id, phase="queued")

    result = PlanLifecycleService().stop_plan(plan_id)

    assert handler_calls == []
    assert result.dispatched is None
    assert result.berth_phase == "queued"
    assert result.final_phase == "queued"
    assert result.checkpoint is None
    assert result.detail == "idle"


def test_stop_unknown_plan_raises(
    plans_dir, clean_state, handler_calls,
):
    """未知计划 → 抛既有 ``PlanNotFoundError``，且不派发任何处理器。"""
    from state_machine.repositories.routing_repository import PlanNotFoundError

    from plan_lifecycle_service import PlanLifecycleService

    with pytest.raises(PlanNotFoundError):
        PlanLifecycleService().stop_plan("no-such-plan")

    assert handler_calls == []


def test_stop_stops_verification_then_execution_when_both_alive(
    plans_dir, clean_state, handler_calls,
):
    """两边都在飞（异常态）→ 先停验证再停执行。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id)
    _mark_verification_alive(plan_id)
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id)

    assert handler_calls == [
        ("verification", plan_id),
        ("execution", plan_id, False),
    ]
    assert result.dispatched == "verification"
    assert result.detail == "both"


def test_stop_propagates_handler_exception(
    plans_dir, clean_state, monkeypatch,
):
    """处理器抛什么就抛什么 —— 调用方要拿到真实的状态码。"""
    from fastapi import HTTPException

    import server  # noqa: F401  (import 顺序，见 handler_calls 的 docstring)
    from routes import execution as routes_execution

    from plan_lifecycle_service import PlanLifecycleService

    def boom(plan_id, discard=False):
        raise HTTPException(409, {"error": "conflict", "reason": "stage_mismatch"})

    monkeypatch.setattr(routes_execution, "stop_execution", boom)

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id)
    _mark_execution_alive(plan_id)

    with pytest.raises(HTTPException) as excinfo:
        PlanLifecycleService().stop_plan(plan_id)

    assert excinfo.value.status_code == 409


def test_stop_never_reaches_for_an_http_client():
    """门面必须进程内直接调用处理器（禁止 httpx / requests）。"""
    import plan_lifecycle_service

    source = Path(plan_lifecycle_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "httpx" not in imported
    assert "requests" not in imported