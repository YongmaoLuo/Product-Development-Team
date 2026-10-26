"""稳定标签与显式桶 —— 状态接口与列表接口说同一句话（2026-10-25）。

背景
----
``plan_routing.current_phase`` 是阶段权威列，但侧栏此前按「文档存在性」**猜**
``status``（有 ``tasks.json`` → ``queued``，有 ``review.json`` → ``reviewed``，…）。
猜出来的桶不认识计划书审核阶段，于是这个新阶段在列表里被标成 ``reviewed`` ——
一个没有发生过的阶段。

这里钉住三条契约：

1. ``GET /api/plan/{id}/status`` 返回该阶段的**稳定标签**
   （``plan_state.PHASE_LABELS``），并且该阶段的验证结论保持 ``pending``
   （计划书审核门在验证子机器**之前**，此时还没有任何一轮验证开始）；
2. ``GET /api/plans`` 对「有 routing 行、且阶段带稳定标签」的计划，桶 = 阶段名
   本身（同名桶）；
3. **没有 routing 行**的计划仍走既有启发式兜底 —— 这次改动不许动它。

标签写在**一处**（``plan_state.PHASE_LABELS``），两个接口共用同一份映射，所以
同一个阶段在两个接口里不可能对不上。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from server import app, invalidate_plans_cache

client = TestClient(app)

#: 计划书审核阶段（任务 14 引入）。
REVIEW_PHASE = "verification_plan_review"

#: 该阶段的稳定标签（任务 14 的 ``plan_state.PHASE_LABELS``）。
REVIEW_LABEL = "计划书待审"


@pytest.fixture(autouse=True)
def _isolated_plans_dir(monkeypatch, tmp_path):
    """把 ``PLANS_DIR`` 指到 tmp 目录，并让列表缓存每次都是冷的。"""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


@pytest.fixture
def seed_plan():
    """种一个计划目录（可选地带 routing 行），并在测试后清掉 routing 行。

    routing 表住在 session级的 hermetic ``state.db`` 里，所以这里写的行会
    比测试活得久，出现在别的测试的 ``GET /api/plans`` 里。数据库路径在
    **setup 时**就抓下来：无 routing 行那个用例会把 ``server._state_db_path``
    指向一个不存在的文件，teardown 绝不能去打开那个文件。
    """
    from server import _state_db_path

    db_path = _state_db_path(None)
    seeded: List[str] = []

    def _seed(
        plan_id: str,
        *,
        files: Optional[Dict[str, str]] = None,
        phase: Optional[str] = None,
    ) -> str:
        from server import PLANS_DIR

        plan_dir = PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (files or {}).items():
            (plan_dir / name).write_text(content, encoding="utf-8")
        if phase:
            from state_machine.db.connection import open as open_db
            from state_machine.db.schema import migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )

            conn = open_db(db_path)
            try:
                migrate(conn)
                repo = RoutingRepository(conn)
                if repo.find(plan_id) is None:
                    repo.insert(plan_id, phase)
            finally:
                conn.close()
        seeded.append(plan_id)
        return plan_id

    yield _seed

    if seeded:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate

        conn = open_db(db_path)
        try:
            # The schema may not exist yet: a test that seeds no routing
            # row never migrated this database.
            migrate(conn)
            for plan_id in seeded:
                conn.execute(
                    "DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,)
                )
            conn.commit()
        finally:
            conn.close()


def _entry(payload: Any, plan_id: str) -> Dict[str, Any]:
    """从 ``GET /api/plans`` 的返回里挑出某个计划那一行。"""
    assert isinstance(payload, list), payload
    for item in payload:
        if isinstance(item, dict) and item.get("id") == plan_id:
            return item
    raise AssertionError(
        f"{plan_id} 不在 /api/plans 的返回里：{payload!r}"
    )


# ---------------------------------------------------------------------------
# 1) 状态接口：稳定标签 + pending
# ---------------------------------------------------------------------------


def test_status_returns_label_and_pending(seed_plan):
    """状态接口返回阶段的稳定标签，验证结论保持 ``pending``。

    计划书审核门在验证子机器**之前**：坐在这个阶段里的计划还没开始任何
    一轮验证，结论就是 ``pending``，不是一个让展示面去猜的空串。
    """
    plan_id = seed_plan(
        "plan-review-status",
        files={"review.json": "{}", "prd.md": "# prd"},
        phase=REVIEW_PHASE,
    )

    resp = client.get(f"/api/plan/{plan_id}/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["current_phase"] == REVIEW_PHASE, data
    assert data["current_phase_label"] == REVIEW_LABEL, (
        "稳定标签必须来自 plan_state.PHASE_LABELS；实际是 "
        f"{data.get('current_phase_label')!r}"
    )
    assert data["verification_status"] == "pending", (
        "计划书审核阶段还没开始验证，结论必须是 pending；实际是 "
        f"{data.get('verification_status')!r}"
    )


# ---------------------------------------------------------------------------
# 2) 列表接口：同名桶
# ---------------------------------------------------------------------------


def test_list_bucket_for_new_phase(seed_plan):
    """列表把计划书审核阶段放进**同名桶**。

    启发式会猜成 ``reviewed``（目录里有 ``review.json``）；阶段列说的是
    ``verification_plan_review``。阶段列是权威，桶必须跟着它走。
    """
    plan_id = seed_plan(
        "plan-review-bucket",
        files={"review.json": "{}", "prd.md": "# prd"},
        phase=REVIEW_PHASE,
    )

    resp = client.get("/api/plans")
    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)

    assert entry["status"] == REVIEW_PHASE, (
        "计划书审核阶段必须有自己的同名桶；实际是 "
        f"{entry.get('status')!r}"
    )
    assert entry["current_phase"] == REVIEW_PHASE, entry
    assert entry["current_phase_label"] == REVIEW_LABEL, (
        "列表与状态接口必须返回同一个标签；实际是 "
        f"{entry.get('current_phase_label')!r}"
    )


# ---------------------------------------------------------------------------
# 3) 列表接口：没有 routing 行 → 既有启发式兜底不变
# ---------------------------------------------------------------------------


def test_list_fallback_without_routing(seed_plan, monkeypatch, tmp_path):
    """没有 routing 行的计划，桶仍然是既有启发式算出来的那个。

    没有记录下来的阶段就没有权威可依，启发式兜底必须原样留着 —— 这次
    改动只给「有 routing 行、且阶段带稳定标签」的计划置桶。
    """
    plan_id = seed_plan(
        "plan-no-routing",
        files={"review.json": "{}", "prd.md": "# prd"},
    )
    # 没有 state.db → 列表走目录列举的兜底路径，那些行不带任何阶段，
    # 这就是「没有 routing 行」的真实形状。
    monkeypatch.setattr(
        "server._state_db_path",
        lambda request=None: tmp_path / "absent-state.db",
    )
    invalidate_plans_cache()

    resp = client.get("/api/plans")
    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)

    assert entry["status"] == "reviewed", (
        "没有 routing 行时，既有启发式兜底不许变；实际是 "
        f"{entry.get('status')!r}"
    )
    assert entry["current_phase"] == "reviewed", entry


# ---------------------------------------------------------------------------
# 4) 列表接口：有 routing 行、但阶段没有稳定标签 → 桶仍是既有启发式
# ---------------------------------------------------------------------------


def test_list_bucket_unchanged_for_phase_without_label(seed_plan):
    """有 routing 行、但阶段**没有**稳定标签时，桶仍然是既有启发式。

    显式桶只给「阶段带稳定标签」的计划（``plan_state.PHASE_LABELS``）。
    其它阶段的列表词汇一个都不许变 —— 否则每一次给某个阶段补标签，
    列表里所有既有计划的桶都会跟着换词。等待桶本身是**改过名**的：
    等待相位合并成 ``queued`` 之后，「有 ``tasks.json``」猜出来的桶跟着
    换成 ``queued`` —— 说的是同一件事（旧词 ``ready``），但用的是现行词。

    阶段列仍然说阶段自己的话：``current_phase`` 取 routing 列的权威值，
    桶取启发式。两者不再共用一个词，但它们各说各的事实。
    """
    plan_id = seed_plan(
        "plan-other-phase",
        files={"tasks.json": "[]", "prd.md": "# prd"},
        phase="verification",
    )

    resp = client.get("/api/plans")
    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)

    assert entry["status"] == "queued", (
        "没有稳定标签的阶段，桶必须仍是既有启发式（有 tasks.json → queued，"
        "等待相位合并后桶跟着现行词走）；"
        f"实际是 {entry.get('status')!r}"
    )
    assert entry["current_phase"] == "verification", entry
