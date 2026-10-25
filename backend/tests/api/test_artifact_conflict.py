"""产物冲突在五个调用点统一处理 —— 409 ``artifact_conflict``。

背景
----
一个 plan 目录里可能躺着两类互斥的产物：``tasks.json``（开发分支）与
``verification-plan.md``（验证分支）。两类同时存在时，「这条 plan 到底想干什么」
**只有人能回答** —— 框架替它选一个，等于替用户做决定。

判定收敛在 :func:`plan_artifacts.detect` 这一个纯函数里（任务 1）。本文件钉住
的是**消费端**：五个调用点都必须接上这份判定，冲突时统一 409
``artifact_conflict``。漏接一个，某个入口就会自己择一个把事情做下去。

五个调用点
----------
============================  ==========================================
入口                          冲突时的答案
============================  ==========================================
任务生成                       409 ``artifact_conflict``
单计划状态                     409 ``artifact_conflict``
计划书生成                     409 ``artifact_conflict``
``/start``                    409 ``artifact_conflict``（且**不进 CAS**）
计划列表                       200，条目落进**冲突桶**
============================  ==========================================

为什么列表不是 409
------------------
列表是**集合**接口：为了一个 plan 把整张列表变成 409，会让侧栏对所有其它 plan
一起失明。所以列表用同一套错误语义表达同一件事 —— 条目的桶是 ``conflict``，
条目上带着**逐字相同**的 ``artifact_conflict`` 载荷（两个产物的相对名）。桶 +
载荷就是列表这一层的 409。

钉住的东西
----------
* ``test_five_call_points_return_409`` —— 五个入口逐个参数化，答案一致；
* ``test_conflict_does_not_delete_or_touch_files`` —— 被拒绝时两个文件都还在，
  mtime 一个字节都没变；
* ``test_response_has_no_absolute_paths`` —— 响应体里只有相对名；
* ``test_list_marks_conflict_bucket`` —— 列表不崩，且该计划在冲突桶里。

测试数据
--------
两个产物是手写的：一个开发任务清单、一份计划书标题。冲突判定只看**文件在不在**，
不看内容 —— 所以这里不需要模型，也不需要真仓库。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from server import app, invalidate_plans_cache  # noqa: E402

client = TestClient(app)

#: 开发分支的产物：任务清单（相对名）。
TASKS = "tasks.json"
#: 验证分支的产物：计划书（相对名）。
PLAN_DOC = "verification-plan.md"
#: 冲突时交回去的两个产物 —— 只有相对名。
CONFLICT_ARTIFACTS = [TASKS, PLAN_DOC]
#: 列表里冲突计划的桶。
CONFLICT_BUCKET = "conflict"
#: 五个调用点的机器可读错误码。
ERROR_CODE = "artifact_conflict"

#: 五个调用点。列表是集合接口，答案不同（见模块 docstring）。
CALL_POINTS: Tuple[str, ...] = (
    "tasks_generate",
    "plan_status",
    "plan_doc_generate",
    "verification_start",
    "plans_list",
)


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_plans_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """把 ``PLANS_DIR`` 指到 tmp 目录，并让列表缓存每次都是冷的。"""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


def _seed_routing_row(plan_id: str, phase: str) -> None:
    """写一行 ``plan_routing`` —— 让列表认得这个计划。"""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        if repo.find(plan_id) is None:
            repo.insert(plan_id, phase)
    finally:
        conn.close()


def _drop_routing_row(plan_id: str) -> None:
    """删掉那一行 —— routing 表住在 session 级数据库里，测试活得比行久。"""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        conn.execute("DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def conflicting_plan() -> Tuple[str, Path]:
    """一个**同时**存在两类产物的计划。

    目录名故意不带日期前缀：``classify_plan`` 只把 ``YYYYMMDD-*`` 认成历史
    归档计划，带日期就意味着这个计划在列表里是「archived」，冲突桶轮不到它。
    """
    plan_id = "conflict-plan-1"
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / TASKS).write_text('{"tasks": []}', encoding="utf-8")
    (plan_dir / PLAN_DOC).write_text("# 验证计划书\n", encoding="utf-8")
    _seed_routing_row(plan_id, "tasks_generation")
    try:
        yield plan_id, plan_dir
    finally:
        _drop_routing_row(plan_id)


# ---------------------------------------------------------------------------
# 发起调用
# ---------------------------------------------------------------------------


def _call(call_point: str, plan_id: str):
    """调用五个调用点之一，返回响应。"""
    if call_point == "tasks_generate":
        return client.post(f"/api/tasks/{plan_id}/generate")
    if call_point == "plan_status":
        return client.get(f"/api/plan/{plan_id}/status")
    if call_point == "plan_doc_generate":
        return client.post(
            f"/api/verification-plan/{plan_id}/generate",
            json={"feedback": None},
        )
    if call_point == "verification_start":
        return client.post(f"/api/verification/{plan_id}/start")
    if call_point == "plans_list":
        return client.get("/api/plans")
    raise AssertionError(f"未知的调用点：{call_point!r}")


def _entry(payload: Any, plan_id: str) -> Dict[str, Any]:
    """从 ``GET /api/plans`` 的返回里挑出某个计划那一行。"""
    assert isinstance(payload, list), payload
    for item in payload:
        if isinstance(item, dict) and item.get("id") == plan_id:
            return item
    raise AssertionError(f"{plan_id} 不在 /api/plans 的返回里：{payload!r}")


def _mtimes(plan_dir: Path) -> Dict[str, int]:
    """两个产物的 mtime（纳秒）。"""
    return {
        name: (plan_dir / name).stat().st_mtime_ns
        for name in (TASKS, PLAN_DOC)
    }


# ---------------------------------------------------------------------------
# 1) 五个调用点 → 同一套 artifact_conflict 语义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call_point", CALL_POINTS)
def test_five_call_points_return_409(
    call_point: str,
    conflicting_plan: Tuple[str, Path],
) -> None:
    """五个入口逐个参数化：冲突的答案一致。

    四个单 plan 接口是 409；列表是集合接口 —— 它不能为了一个 plan 把整张列表
    变成 409，所以同一套语义在列表里表现为「冲突桶 + 同一份载荷」。
    """
    plan_id, _plan_dir = conflicting_plan

    resp = _call(call_point, plan_id)

    if call_point == "plans_list":
        assert resp.status_code == 200, resp.text
        entry = _entry(resp.json(), plan_id)
        assert entry["status"] == CONFLICT_BUCKET, (
            "冲突的计划必须落进冲突桶；实际是 %r" % (entry.get("status"),)
        )
        conflict = entry.get("artifact_conflict") or {}
        assert conflict.get("error") == ERROR_CODE, entry
        assert conflict.get("artifacts") == CONFLICT_ARTIFACTS, entry
        return

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == ERROR_CODE, body
    assert body["artifacts"] == CONFLICT_ARTIFACTS, (
        "响应必须给出两个产物的相对名；实际是 %r" % (body.get("artifacts"),)
    )


def test_start_does_not_enter_cas(conflicting_plan: Tuple[str, Path]) -> None:
    """``/start`` 在冲突时**不进 CAS**：阶段不许被推进。

    CAS 是 ``try_mark_phase``：一旦它成功，plan 的阶段就动了。冲突的 plan 连
    「该走哪条分支」都还没人裁决，阶段一动就把人的裁决权抹掉了。
    """
    plan_id, _plan_dir = conflicting_plan

    before = _phase_of(plan_id)
    resp = _call("verification_start", plan_id)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == ERROR_CODE
    assert _phase_of(plan_id) == before, "冲突时阶段不许被推进"


def _phase_of(plan_id: str) -> Optional[str]:
    """计划当前的阶段（走生产读路径）。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    from server import _state_db_path

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        row = RoutingRepository(conn).find(plan_id)
        return str(row.get("current_phase")) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2) 被拒绝时两个文件都还在，mtime 一个字节都没变
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call_point", CALL_POINTS)
def test_conflict_does_not_delete_or_touch_files(
    call_point: str,
    conflicting_plan: Tuple[str, Path],
) -> None:
    """被拒绝时：不删文件、不改文件、不动 mtime。

    冲突的处置权在人手上 —— 框架连文件碰一下都不行，遑论替人择一。
    """
    plan_id, plan_dir = conflicting_plan
    before = _mtimes(plan_dir)

    _call(call_point, plan_id)

    assert (plan_dir / TASKS).exists(), "tasks.json 不许被删"
    assert (plan_dir / PLAN_DOC).exists(), "verification-plan.md 不许被删"
    after = _mtimes(plan_dir)
    assert after == before, "两个产物的 mtime 都不许变：%r → %r" % (
        before,
        after,
    )


# ---------------------------------------------------------------------------
# 3) 响应体里只有相对名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call_point", [cp for cp in CALL_POINTS if cp != "plans_list"]
)
def test_response_has_no_absolute_paths(
    call_point: str,
    conflicting_plan: Tuple[str, Path],
    tmp_path: Path,
) -> None:
    """响应体不许泄露本机绝对路径。

    产物名是 **plan 相对名**（``tasks.json`` / ``verification-plan.md``）——
    调用方拿它去自己的 plan 目录里找文件，不需要知道服务器把 plan 放在哪。
    """
    plan_id, plan_dir = conflicting_plan

    resp = _call(call_point, plan_id)

    assert resp.status_code == 409, resp.text
    assert str(plan_dir) not in resp.text, (
        "响应不许出现 plan 目录的绝对路径；实际响应：%s" % resp.text
    )
    assert str(tmp_path) not in resp.text, (
        "响应不许出现本机路径；实际响应：%s" % resp.text
    )
    body = resp.json()
    assert body["artifacts"] == CONFLICT_ARTIFACTS, body
    for name in body["artifacts"]:
        assert "/" not in name and "\\" not in name, (
            "产物名必须是相对名；实际是 %r" % (name,)
        )


# ---------------------------------------------------------------------------
# 4) 列表：冲突桶
# ---------------------------------------------------------------------------


def test_list_marks_conflict_bucket(conflicting_plan: Tuple[str, Path]) -> None:
    """列表不崩，且这个计划落进**冲突桶**。

    列表是集合接口：一个 plan 的冲突不该把整张列表变成 409（那会让侧栏对所有
    其它 plan 一起失明）。但它也**不能装作没看见** —— 桶就是 ``conflict``，
    条目上还带着同一份 ``artifact_conflict`` 载荷。
    """
    plan_id, _plan_dir = conflicting_plan

    resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)
    assert entry["status"] == CONFLICT_BUCKET, (
        "冲突的计划必须落进冲突桶；实际是 %r" % (entry.get("status"),)
    )
    conflict = entry.get("artifact_conflict") or {}
    assert conflict.get("error") == ERROR_CODE, entry
    assert conflict.get("artifacts") == CONFLICT_ARTIFACTS, entry
    assert conflict.get("existing_artifacts") == CONFLICT_ARTIFACTS, entry


def test_list_keeps_other_plans_readable(
    conflicting_plan: Tuple[str, Path],
) -> None:
    """一个 plan 冲突，不许把别的 plan 挤出列表。

    列表读的是**每个** plan 目录的产物；一个目录读不出来（或者冲突）时，
    那个 plan 自己进冲突桶，其它 plan 的行一个都不许少。
    """
    plan_id, _plan_dir = conflicting_plan
    other_id = "conflict-plan-neighbour"
    other_dir = server.PLANS_DIR / other_id
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "prd.md").write_text("# prd", encoding="utf-8")
    _seed_routing_row(other_id, "prd_review")
    try:
        resp = client.get("/api/plans")

        assert resp.status_code == 200, resp.text
        entry = _entry(resp.json(), other_id)
        assert entry["status"] != CONFLICT_BUCKET, (
            "没有冲突的 plan 不许落进冲突桶；实际是 %r" % (entry.get("status"),)
        )
    finally:
        _drop_routing_row(other_id)
