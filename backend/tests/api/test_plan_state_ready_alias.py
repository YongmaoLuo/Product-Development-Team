"""写入口的等待相位归一化（R10：``queued`` 是唯一的等待相位）。

背景
----
``POST /api/plan/{id}/state`` 与 ``POST /api/plan/{id}/phase`` 是两个**外部
写入口** —— 前端、旧脚本、别的仓都还在往里传 ``ready``。库里不该再出现
``ready``，但直接 400 会让老调用方全挂。所以入口负责**归一化**：
``ready`` → ``queued``，落库与响应都只说现行词汇。

钉死的行为
----------
* ``/state`` 收到 ``current_phase='ready'`` → 落库 ``queued``，响应体的
  ``current_phase`` 是 ``queued``；``arch_enabled`` / ``test_enabled`` 与相位
  同传时三者都生效（归一化不能吃掉另外两个字段）；
* ``/phase`` 收到 ``target_phase='ready'`` → 落库 ``queued``，响应体的
  ``current_phase`` 是 ``queued``；
* 归一化后等于当前相位 → 幂等 200（不是 400）；
* 未知 plan → 404（语义不变）；
* 命中历史词汇要留痕：一条带 plan_id 与**来源**（``api.plan_state`` /
  ``api.plan_phase``）的 warning —— 旧数据要清得掉，先得知道它在哪儿、
  是谁写进来的。

边界
----
* 非法目标相位 → 400，语义不变（归一化只认 ``ready`` 这一个历史别名）；
* 已经是 ``queued`` 的相位不产生弃用 warning（现行词汇不是弃用）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)

#: 弃用日志的标记（``phase_vocabulary.log_deprecated_ready`` 打的）。
_DEPRECATION_MARKER = "deprecated phase vocabulary"


@pytest.fixture
def state_db(tmp_path, monkeypatch) -> Path:
    """把 plan-state 持久化指向一份 per-test 的 SQLite 文件。

    ``plan_routing`` 表住在共享的 state.db 里；不隔离的话，种进去的行会
    比测试活得久，出现在别的测试的读取里。
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def plans_dir(tmp_path, monkeypatch) -> Path:
    """把 ``PLANS_DIR`` 指到 tmp 目录（计划目录是写入口的前提）。"""
    import server

    plans = tmp_path / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", plans)
    return plans


def _seed_plan(plans_dir: Path, plan_id: str, current_phase: str = "tasks_generation") -> Path:
    """种一个计划目录，并给它一个能走到 ``queued`` 的起始相位。

    起始相位是 ``tasks_generation``：``ready`` / ``queued`` 都是从那里分叉
    出去的等待相位（见 ``plan_state.PHASE_TRANSITIONS``）。从 ``interview``
    起步的话，请求本身没错，是转移不合法 —— 那样测的就不是归一化了。
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "plan_id": plan_id,
        "current_phase": current_phase,
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")
    return plan_dir


def _routing_phase(plan_id: str, db_path: Path) -> Optional[str]:
    """读回 ``plan_routing`` 行里的 ``current_phase``（落库的那一份）。"""
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_db(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    if row is None:
        return None
    return row.get("current_phase")


def _deprecation_warnings(caplog) -> List[str]:
    """caplog 里与「等待相位旧词汇」有关的 warning 文本。"""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and _DEPRECATION_MARKER in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1) /state 写入口：ready → queued
# ---------------------------------------------------------------------------


def test_state_endpoint_normalizes_ready_to_queued(plans_dir, state_db):
    """``/state`` 收到 ``ready`` 时落库 ``queued``，响应体也只说 ``queued``。

    归一化的是**词汇**，不是这条计划：请求照样成功（200），计划照样落到那个
    「等人」的相位，只是这个相位从此只有 ``queued`` 一个名字。

    ``arch_enabled`` 与相位同传时两个都生效 —— 归一化不能把同传的另一个
    字段吃掉。
    """
    plan_id = "plan-state-ready-alias"
    _seed_plan(plans_dir, plan_id)

    resp = client.post(
        f"/api/plan/{plan_id}/state",
        json={"current_phase": "ready", "arch_enabled": True, "test_enabled": True},
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["current_phase"] == "queued", (
        "响应体的相位永远不能说旧词汇 'ready'；实际是 "
        f"{payload.get('current_phase')!r}"
    )
    assert payload["flags"]["arch_enabled"] is True, payload
    assert payload["flags"]["test_enabled"] is True, payload

    persisted = _routing_phase(plan_id, state_db)
    assert persisted == "queued", (
        f"库里不该再出现 'ready'；落库的是 {persisted!r}"
    )

    # 归一化后等于当前相位 → 幂等 200（不是 400）。
    again = client.post(
        f"/api/plan/{plan_id}/state",
        json={"current_phase": "ready"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["current_phase"] == "queued", again.text


# ---------------------------------------------------------------------------
# 2) /phase 写入口：ready → queued
# ---------------------------------------------------------------------------


def test_phase_endpoint_normalizes_ready_to_queued(plans_dir, state_db):
    """``/phase`` 收到 ``target_phase='ready'`` 时落库 ``queued``。

    两个写入口说的是同一件事（「把这条计划推进到等待相位」），所以它们必须
    说同一个词：只归一化一个入口，等于把两套词汇留在接口上。
    """
    plan_id = "plan-phase-ready-alias"
    _seed_plan(plans_dir, plan_id)

    resp = client.post(
        f"/api/plan/{plan_id}/phase",
        json={"target_phase": "ready"},
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["current_phase"] == "queued", (
        "响应体的相位永远不能说旧词汇 'ready'；实际是 "
        f"{payload.get('current_phase')!r}"
    )
    assert payload["requested_phase"] == "queued", (
        "请求相位经归一化后回显，旧词汇不该从接口漏出去；实际是 "
        f"{payload.get('requested_phase')!r}"
    )

    persisted = _routing_phase(plan_id, state_db)
    assert persisted == "queued", (
        f"库里不该再出现 'ready'；落库的是 {persisted!r}"
    )


# ---------------------------------------------------------------------------
# 3) 未知 plan → 404；非法目标相位 → 400（语义不变）
# ---------------------------------------------------------------------------


def test_unknown_plan_still_404(plans_dir, state_db):
    """归一化不改变「计划不存在」的语义：两个写入口都还是 404。

    归一化发生在**找到计划之后**：一个不存在的计划没有相位可言，拿它去查
    转移表只会把 404 变成别的错误码。
    """
    missing = "plan-does-not-exist"

    resp = client.post(
        f"/api/plan/{missing}/state",
        json={"current_phase": "ready"},
    )
    assert resp.status_code == 404, resp.text

    resp = client.post(
        f"/api/plan/{missing}/phase",
        json={"target_phase": "ready"},
    )
    assert resp.status_code == 404, resp.text


def test_illegal_target_phase_still_400(plans_dir, state_db):
    """归一化只认 ``ready`` 这一个历史别名：其它非法相位照旧 400。

    归一化不能变成「什么相位都放行」—— 那会把一个拼写错误从 400 变成一个
    静默落库的相位。
    """
    plan_id = "plan-illegal-phase"
    _seed_plan(plans_dir, plan_id)

    resp = client.post(
        f"/api/plan/{plan_id}/state",
        json={"current_phase": "nonexistent_phase"},
    )
    assert resp.status_code == 400, resp.text

    resp = client.post(
        f"/api/plan/{plan_id}/phase",
        json={"target_phase": "nonexistent_phase"},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# 4) 命中历史词汇要留痕（带 plan_id 与来源）
# ---------------------------------------------------------------------------


def test_normalized_call_logs_deprecation(plans_dir, state_db, caplog):
    """两个写入口命中 ``ready`` 时都要留一条带来源的 warning。

    来源必须能区分是哪个入口写进来的（``api.plan_state`` /
    ``api.plan_phase``）—— 只知道「有人传了旧词」是清不掉旧数据的：得知道
    是谁在写。
    """
    state_plan_id = "plan-state-deprecation"
    phase_plan_id = "plan-phase-deprecation"
    _seed_plan(plans_dir, state_plan_id)
    _seed_plan(plans_dir, phase_plan_id)

    with caplog.at_level(logging.WARNING, logger="server"):
        client.post(
            f"/api/plan/{state_plan_id}/state",
            json={"current_phase": "ready"},
        )
        client.post(
            f"/api/plan/{phase_plan_id}/phase",
            json={"target_phase": "ready"},
        )

    warnings = _deprecation_warnings(caplog)
    assert warnings, "命中历史词汇必须留痕：旧数据要清得掉，先得知道它在哪儿"

    assert len(warnings) == 2, (
        "每个写入口各留一条就够（归一化只做一次）；实际记录："
        f"{warnings!r}"
    )

    assert any(
        state_plan_id in text and "source=api.plan_state" in text
        for text in warnings
    ), f"/state 的弃用日志必须带 plan_id 与来源；实际记录：{warnings!r}"

    assert any(
        phase_plan_id in text and "source=api.plan_phase" in text
        for text in warnings
    ), f"/phase 的弃用日志必须带 plan_id 与来源；实际记录：{warnings!r}"
