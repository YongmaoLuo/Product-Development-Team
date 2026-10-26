"""plan 级统一 start 门面：按检查点 kind 分派（R11 · 任务 19）。

被钉住的行为
------------
1. **有 verification 检查点 → 派发验证**（``start_verification``），执行
   一根都不碰。
2. **有 execution 检查点 → 派发执行**（``start_execution``）。
3. **没有检查点 → 全新执行**：仍然派发执行，而且必须带 ``project_dir``。
4. **绝不能把 verification 检查点交给 ``start_execution``** —— 那会让恢复
   退化成全新执行，还会误判「缺 project_dir」。
5. **门面不自己编错误**：没有检查点且 ``project_dir`` 为空时，那个 400 由
   ``start_execution`` 说，门面原样透传，不自己编一个。
6. **检查点在派发之前读**：派发的那一侧会消费掉自己写的那份检查点，派发
   之后再读就只剩 ``None``。

被测的是**分派**，不是端点自己的语义：既有 start 端点在这里被换成记录器，
端点语义另有测试钉着。
"""

from __future__ import annotations

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


def _write_checkpoint(plan_dir: Path, kind: str) -> dict:
    """在计划目录里写一份 pause 检查点（``kind`` 决定恢复哪一侧）。"""
    from pause_state import write_pause_checkpoint

    return write_pause_checkpoint(
        plan_dir,
        kind=kind,
        project_dir="/tmp/project",
        reason="interrupted",
    )


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
def handler_calls(monkeypatch):
    """把既有 start 端点换成记录器，返回调用记录列表。

    ``import server`` 必须排在 ``from routes import ...`` 前面：
    ``routes.execution`` / ``routes.verification`` 在模块层 import
    ``server``，而 ``server`` 在文件末尾 import 这两个 routes 模块 ——
    顺序反了就是一次半初始化的循环导入。
    """
    import server  # noqa: F401  (import 顺序是承重墙，见 docstring)

    from routes import execution as routes_execution
    from routes import verification as routes_verification

    calls = []

    def fake_start_execution(plan_id, req=None):
        calls.append(("execution", plan_id, req))

    def fake_start_verification(plan_id, req=None):
        calls.append(("verification", plan_id, req))

    monkeypatch.setattr(routes_execution, "start_execution", fake_start_execution)
    monkeypatch.setattr(
        routes_verification, "start_verification", fake_start_verification,
    )
    return calls


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------


def test_start_routes_verification_checkpoint_to_verification(
    plans_dir, handler_calls,
):
    """verification 检查点 → 派发验证，执行一根都不碰。"""
    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    plan_id = "p1"
    plan_dir = _make_plan_dir(plans_dir, plan_id, phase="verification")
    _write_checkpoint(plan_dir, "verification")

    result = PlanLifecycleService().start_plan(plan_id, StartPlanSpec())

    assert [call[0] for call in handler_calls] == ["verification"]
    assert handler_calls[0][1] == plan_id
    req = handler_calls[0][2]
    assert req.max_rounds is None
    assert req.tool is None
    assert req.force is False

    assert result.plan_id == plan_id
    assert result.dispatched == "verification"
    assert result.berth_phase == "verification"
    assert result.final_phase == "verification_running"
    assert result.checkpoint["kind"] == "verification"
    assert result.detail == "start_verification(resume)"


def test_start_routes_execution_checkpoint_to_execution(
    plans_dir, handler_calls,
):
    """execution 检查点 → 派发执行，请求里的字段原样带过去。"""
    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    plan_id = "p1"
    plan_dir = _make_plan_dir(plans_dir, plan_id)
    _write_checkpoint(plan_dir, "execution")

    result = PlanLifecycleService().start_plan(
        plan_id,
        StartPlanSpec(
            project_dir="/tmp/x",
            tool="claude",
            sync_targets=["feishu"],
        ),
    )

    assert [call[0] for call in handler_calls] == ["execution"]
    req = handler_calls[0][2]
    assert req.project_dir == "/tmp/x"
    assert req.tool == "claude"
    assert req.sync_targets == ["feishu"]

    assert result.dispatched == "execution"
    assert result.berth_phase == "queued"
    assert result.final_phase == "executing"
    assert result.checkpoint["kind"] == "execution"
    assert result.detail == "start_execution(resume)"


def test_start_without_checkpoint_starts_execution(
    plans_dir, handler_calls,
):
    """没有检查点 → 全新执行（不是验证），并且带上 project_dir。"""
    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id)

    result = PlanLifecycleService().start_plan(
        plan_id, StartPlanSpec(project_dir="/tmp/x"),
    )

    assert [call[0] for call in handler_calls] == ["execution"]
    assert handler_calls[0][2].project_dir == "/tmp/x"

    assert result.dispatched == "execution"
    assert result.checkpoint is None
    assert result.detail == "start_execution(fresh)"


def test_start_never_passes_verification_checkpoint_to_execution(
    plans_dir, handler_calls,
):
    """verification 检查点绝不进 start_execution —— 哪怕压根没有 project_dir。

    进了执行侧，恢复就退化成「全新执行」，而且会误判「缺 project_dir」
    （验证的恢复不需要 project_dir）。
    """
    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    plan_id = "p1"
    plan_dir = _make_plan_dir(plans_dir, plan_id, phase="verification")
    _write_checkpoint(plan_dir, "verification")

    result = PlanLifecycleService().start_plan(plan_id, StartPlanSpec())

    assert [call[0] for call in handler_calls] == ["verification"]
    assert result.dispatched == "verification"
    assert result.detail == "start_verification(resume)"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_start_without_project_dir_surfaces_the_handlers_400(
    plans_dir, monkeypatch,
):
    """没有检查点且没有 project_dir → 400 由 start_execution 说，门面不编。"""
    from fastapi import HTTPException

    import server  # noqa: F401  (import 顺序是承重墙)
    from routes import execution as routes_execution

    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    def refuse(plan_id, req=None):
        raise HTTPException(400, "project_dir is required.")

    monkeypatch.setattr(routes_execution, "start_execution", refuse)

    plan_id = "p1"
    _make_plan_dir(plans_dir, plan_id)

    with pytest.raises(HTTPException) as excinfo:
        PlanLifecycleService().start_plan(plan_id, StartPlanSpec())

    assert excinfo.value.status_code == 400
    assert "project_dir" in str(excinfo.value.detail)


def test_start_reads_the_checkpoint_before_dispatching(
    plans_dir, monkeypatch,
):
    """检查点在派发之前读 —— 派发的那一侧会消费掉自己那份。"""
    import server  # noqa: F401  (import 顺序是承重墙)
    from routes import verification as routes_verification

    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    from pause_state import checkpoint_path

    def consume(plan_id, req=None):
        # 验证侧启动成功后会把检查点退休掉（删文件）。
        checkpoint_path(server._plan_dir(plan_id)).unlink(missing_ok=True)

    monkeypatch.setattr(routes_verification, "start_verification", consume)

    plan_id = "p1"
    plan_dir = _make_plan_dir(plans_dir, plan_id, phase="verification")
    _write_checkpoint(plan_dir, "verification")

    result = PlanLifecycleService().start_plan(plan_id, StartPlanSpec())

    assert result.checkpoint is not None
    assert result.checkpoint["kind"] == "verification"


def test_start_unknown_plan_raises(plans_dir, handler_calls):
    """未知计划 → 抛既有 ``PlanNotFoundError``，且不派发任何处理器。"""
    from state_machine.repositories.routing_repository import PlanNotFoundError

    from plan_lifecycle_service import PlanLifecycleService, StartPlanSpec

    with pytest.raises(PlanNotFoundError):
        PlanLifecycleService().start_plan("no-such-plan", StartPlanSpec())

    assert handler_calls == []
