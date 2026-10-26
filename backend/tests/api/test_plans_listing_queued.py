"""列表接口的等待相位归一化（R10：``queued`` 是唯一的等待相位）。

背景
----
``GET /api/plans`` 的 ``current_phase`` 直接取 ``plan_routing.current_phase``
（阶段权威列）。存量数据库行里还存着历史词汇 ``ready`` —— 那是 ``queued``
的旧名。于是列表会把「ready」原样透出给前端与调度器：同一个等待状态，两套
词汇，下游得学会两套说法。

启动时的迁移（``phase_migration``）会把这些行改写成 ``queued``，但迁移可能
还没跑过、也可能跑在另一个库上。**读路径必须自己兜住**：列表说出去的相位
永远是现行词汇。

钉死的行为
----------
* routing 行停在 ``ready`` → 列表说 ``queued``，标签是「排队中」，桶是
  ``queued``；这一行**仍然返回**（不跳过、不 500）；
* 命中历史词汇要留痕：一条带 plan_id 与来源的 warning —— 旧数据要清得掉，
  先得知道它在哪儿；
* 已经是 ``queued`` 的行 → 一切照旧，**不产生** warning（现行词汇不是弃用）；
* 启发式兜底（没有 routing 行时按文档存在性猜出来的桶）不受影响。

边界
----
* ``status`` 字段的启发式桶（``new`` / ``reviewed`` / ``interviewed`` /
  ``queued``）除了等待桶跟着改名之外一个字都不改 —— 等待桶说的还是同一
  件事（旧词 ``ready``），只是用现行词 ``queued``；
* 没有 routing 行的计划仍然走启发式，不被路由行的逻辑接管。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from server import app, invalidate_plans_cache

client = TestClient(app)

#: 弃用日志的标记（``phase_vocabulary.log_deprecated_ready`` 打的）。
#:
#: 用它而不是「有没有 warning」来筛：列表接口自己也会打别的 warning
#: （比如跳过一个 id 不合法的旧行），那些与等待相位无关。
_DEPRECATION_MARKER = "deprecated phase vocabulary"

#: ``queued`` 的稳定标签（``plan_state.PHASE_LABELS``）。
QUEUED_LABEL = "排队中"


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

    routing 表住在 session 级的 hermetic ``state.db`` 里，所以这里写的行会
    比测试活得久，出现在别的测试的 ``GET /api/plans`` 里。数据库路径在
    **setup 时**就抓下来 —— teardown 绝不能去打开一个可能不存在的文件。
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


def _deprecation_warnings(caplog) -> List[str]:
    """caplog 里与「等待相位旧词汇」有关的 warning 文本。"""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and _DEPRECATION_MARKER in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1) 未迁移的 ready 行 → 归一化为 queued（并留痕）
# ---------------------------------------------------------------------------


def test_listing_normalizes_ready_to_queued(seed_plan, caplog):
    """routing 行停在 ``ready`` 时，列表说 ``queued``，且这一行仍然返回。

    旧行不会因为「相位词汇过时了」而从列表里消失 —— 消失比说错更糟：一个
    还在等人的计划不见了，就没有人知道它还在等。归一化的是**说法**，不是
    这条计划本身。
    """
    plan_id = seed_plan(
        "plan-listing-ready",
        files={"tasks.json": "[]", "prd.md": "# prd"},
        phase="ready",
    )

    with caplog.at_level(logging.WARNING, logger="server"):
        resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)

    assert entry["current_phase"] == "queued", (
        "列表的相位字段永远不能说旧词汇 'ready'；实际是 "
        f"{entry.get('current_phase')!r}"
    )
    assert entry["status"] == "queued", (
        "归一化后的等待相位有自己的显式桶；实际是 "
        f"{entry.get('status')!r}"
    )

    warnings = _deprecation_warnings(caplog)
    assert warnings, (
        "读到历史词汇必须留痕：旧数据要清得掉，先得知道它在哪儿"
    )
    assert any(plan_id in text for text in warnings), (
        f"弃用日志必须带 plan_id；实际记录：{warnings!r}"
    )


# ---------------------------------------------------------------------------
# 2) 已经是 queued 的行 → 不产生 warning
# ---------------------------------------------------------------------------


def test_listing_queued_row_has_no_warning(seed_plan, caplog):
    """现行词汇不是弃用：已经是 ``queued`` 的行不产生任何弃用 warning。

    否则每次刷侧栏都要刷一遍「弃用」噪音，真正的旧数据就淹没在里面。
    """
    plan_id = seed_plan(
        "plan-listing-queued",
        files={"tasks.json": "[]", "prd.md": "# prd"},
        phase="queued",
    )

    with caplog.at_level(logging.WARNING, logger="server"):
        resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)
    assert entry["current_phase"] == "queued", entry
    assert entry["status"] == "queued", entry

    warnings = _deprecation_warnings(caplog)
    assert not warnings, (
        "已经是 queued 的行不该产生弃用 warning；实际记录：{warnings!r}"
    )


# ---------------------------------------------------------------------------
# 3) 标签是中文
# ---------------------------------------------------------------------------


def test_listing_label_is_chinese(seed_plan):
    """归一化后的相位带中文稳定标签「排队中」。

    标签来自 ``plan_state.PHASE_LABELS``（任务 4 定的），列表与状态接口
    共用同一份映射 —— 同一个相位在两个接口里不可能对不上。
    """
    plan_id = seed_plan(
        "plan-listing-label",
        files={"tasks.json": "[]", "prd.md": "# prd"},
        phase="ready",
    )

    resp = client.get("/api/plans")
    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)

    assert entry["current_phase_label"] == QUEUED_LABEL, (
        "queued 的标签必须是「排队中」；实际是 "
        f"{entry.get('current_phase_label')!r}"
    )


# ---------------------------------------------------------------------------
# 4) 启发式兜底：等待桶改名，相位字段仍然不说旧词汇
# ---------------------------------------------------------------------------


def test_listing_heuristic_bucket_unchanged(seed_plan, monkeypatch, tmp_path):
    """没有 routing 行时：桶仍是启发式算出来的那个，相位字段不说旧词汇。

    两个字段各说各的事实：

    * ``status`` 是**桶** —— 启发式猜的（有 ``tasks.json`` → ``queued``），
      这次是**改名**不是放宽：等待相位合并成 ``queued`` 之后，说同一件事
      的旧词 ``ready`` 退场，桶只说现行词；
    * ``current_phase`` 是**相位** —— 相位词汇里只有一个等待相位，所以
      无论这个值是 routing 列说的还是启发式猜的，它都不能是旧词汇。

    没有 routing 行的计划仍然走启发式：路由行的逻辑不接管它。
    """
    plan_id = seed_plan(
        "plan-listing-heuristic",
        files={"tasks.json": "[]", "prd.md": "# prd"},
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

    assert entry["status"] == "queued", (
        "启发式桶必须是精确的现行词（有 tasks.json → queued）；实际是 "
        f"{entry.get('status')!r}"
    )
    assert entry["current_phase"] != "ready", (
        "相位字段永远不说旧词汇 'ready'；实际是 "
        f"{entry.get('current_phase')!r}"
    )

