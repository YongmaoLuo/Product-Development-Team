"""统一 stop/start 门面的 API 契约（R11 · 任务 20）。

被钉住的行为
------------
1. **stop 端点回 ``LifecycleResult``** —— 响应体的键就是那个 dataclass 的
   字段，一个不多一个不少（``dataclasses.asdict`` 直接序列化）。
2. **start 端点把请求体转成 ``StartPlanSpec``** —— 请求体的五个字段
   （``project_dir`` / ``tool`` / ``max_rounds`` / ``sync_targets`` /
   ``force``）落到 spec 的同名字段上，一个都不自己编。
3. **未知 plan → 404 ``{'error': 'Plan not found'}``** —— 不是 500，也不
   是「什么都没发生」。
4. **门面注册在 catch-all 之前** —— ``/{path:path}`` 是 GET，路由匹配又是
   先到先得：门面要是注册在它后面，GET 会被 catch-all 抢先。

被测的是**门面这一层**（请求 → spec → 门面 → 响应）。门面内部怎么分派
（停执行还是停验证、按检查点的 kind 选边）由任务 18 / 19 的单元测试钉着，
这里不重复。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import List, Optional

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)

#: 门面的两条路径（也是契约的一部分：外部系统按这两个 URL 调用）。
STOP_PATH = "/api/plan/{plan_id}/lifecycle/stop"
START_PATH = "/api/plan/{plan_id}/lifecycle/start"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path, monkeypatch) -> Path:
    """把 ``server.PLANS_DIR`` 指到 tmp 目录。

    门面判「计划存不存在」走的是 ``server._plan_dir``，它读的是模块全局
    ``PLANS_DIR``；不隔离的话，404 那条测试会去查真实的 ``plans/`` 目录。
    """
    import server

    target = tmp_path / "plans"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", target)
    return target


@pytest.fixture
def service_calls(monkeypatch):
    """把门面的两个方法换成记录器，返回调用记录列表。

    记的是 ``(plan_id, 参数)``：请求体转成了什么，全在这份记录里。
    """
    from plan_lifecycle_service import LifecycleResult, PlanLifecycleService

    calls = []

    def fake_stop(self, plan_id, requeue=False):
        calls.append(("stop", plan_id, requeue))
        return LifecycleResult(
            plan_id=plan_id,
            dispatched="execution",
            berth_phase="stopped",
            final_phase="queued",
            checkpoint={"kind": "execution"},
            detail="stop_execution+requeue",
        )

    def fake_start(self, plan_id, spec=None):
        calls.append(("start", plan_id, spec))
        return LifecycleResult(
            plan_id=plan_id,
            dispatched="execution",
            berth_phase="queued",
            final_phase="executing",
            checkpoint=None,
            detail="start_execution(fresh)",
        )

    monkeypatch.setattr(PlanLifecycleService, "stop_plan", fake_stop)
    monkeypatch.setattr(PlanLifecycleService, "start_plan", fake_start)
    return calls


# ---------------------------------------------------------------------------
# 响应体
# ---------------------------------------------------------------------------


def test_stop_endpoint_returns_lifecycle_result(service_calls):
    """stop 端点回 ``LifecycleResult`` 的字段 —— 一个键都不少。"""
    response = client.post("/api/plan/p1/lifecycle/stop", json={"requeue": True})

    assert response.status_code == 200
    assert response.json() == {
        "plan_id": "p1",
        "dispatched": "execution",
        "berth_phase": "stopped",
        "final_phase": "queued",
        "checkpoint": {"kind": "execution"},
        "detail": "stop_execution+requeue",
    }
    # 请求体里的 ``requeue`` 到了门面。
    assert service_calls == [("stop", "p1", True)]


def test_start_endpoint_converts_request_to_spec(service_calls):
    """start 端点把请求体的五个字段原样转成 ``StartPlanSpec``。"""
    from plan_lifecycle_service import StartPlanSpec

    response = client.post(
        "/api/plan/p1/lifecycle/start",
        json={
            "project_dir": "/tmp/project",
            "tool": "claude",
            "max_rounds": 3,
            "sync_targets": ["feishu"],
            "force": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["plan_id"] == "p1"

    assert len(service_calls) == 1
    kind, plan_id, spec = service_calls[0]
    assert kind == "start"
    assert plan_id == "p1"
    assert isinstance(spec, StartPlanSpec)
    assert spec.project_dir == "/tmp/project"
    assert spec.tool == "claude"
    assert spec.max_rounds == 3
    assert spec.sync_targets == ["feishu"]
    assert spec.force is True


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_unknown_plan_returns_404(plans_dir):
    """未知 plan → 404 ``{'error': 'Plan not found'}``（不是 500）。

    这一条走**真的**门面：``PlanNotFoundError`` 是门面自己抛的，端点把它
    翻成 404。处理器抛的 ``HTTPException`` 原样透传，不包成 500。
    """
    for path in ("/api/plan/no-such-plan/lifecycle/stop",
                 "/api/plan/no-such-plan/lifecycle/start"):
        response = client.post(path, json={})
        assert response.status_code == 404, path
        assert response.json() == {"error": "Plan not found"}, path


def test_handler_http_exception_is_not_wrapped_as_500(monkeypatch):
    """处理器抛的 ``HTTPException`` 原样透传 —— 409 还是 409。

    把它包成 500 就是把「这个计划正在跑，停不了」说成「服务器坏了」：
    调用方会重试，而重试一次也不会成功。
    """
    from fastapi import HTTPException

    from plan_lifecycle_service import PlanLifecycleService

    def refuse(self, plan_id, requeue=False):
        raise HTTPException(409, "plan is already running")

    monkeypatch.setattr(PlanLifecycleService, "stop_plan", refuse)

    response = client.post("/api/plan/p1/lifecycle/stop", json={})
    assert response.status_code == 409


def test_lifecycle_routes_are_behind_the_request_guard():
    """门面在 ``/api/`` 底下，所以受 request_guard 管。

    守卫是 middleware，套在每一个 ``/api/`` 路径上 —— 门面没有绕过它，
    也没有削弱它：没带（或带错）那个头的请求在进处理器之前就被 403 拒掉。
    """
    unguarded = TestClient(app, headers={"X-PDT-Request": "not-the-value"})

    response = unguarded.post("/api/plan/p1/lifecycle/stop", json={})

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_routes_registered_before_catch_all():
    """门面注册在 ``/{path:path}`` catch-all 之前。

    路由匹配先到先得：注册在 catch-all 之后的 GET 会被那个 ``{path:path}``
    抢先，答案变成前端 bundle 而不是门面的响应。

    ``app.routes`` 自 FastAPI 0.141 起把一次 ``include_router`` 记成**一个**
    ``_IncludedRouter`` 条目（里面的路由在 ``include_context`` 下面），所以
    这里走 ``server.iter_app_routes()`` —— 它就是 ``app.routes`` 的扁平化，
    顺序与注册顺序一致。
    """
    from server import iter_app_routes

    paths = [route.path for route in iter_app_routes()]

    catch_all_index = paths.index("/{path:path}")
    for facade_path in (STOP_PATH, START_PATH):
        assert facade_path in paths, facade_path
        assert paths.index(facade_path) < catch_all_index, facade_path
