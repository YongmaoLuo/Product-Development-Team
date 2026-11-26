"""共存目录的阶段推断与计划列表 —— 没有冲突桶。

背景
----
``tasks.json`` 与 ``verification-plan.md`` 同时存在**不是冲突**：按「任务优先」
的主分支语义，这条 plan 就是一条开发分支的计划，只不过额外带着一份计划书
（附加产物）。产物判定收敛在 :func:`plan_artifacts.detect`（任务 1），它的输出
域是 ``dev`` / ``verification`` / ``empty`` 三个取值 —— **不再有** ``conflict``。

本文件钉住的是**消费端**的两条读路径：

* :meth:`plan_state.PlanState._infer_phase` —— 共存目录按 dev 推断为
  ``queued``，**不再抛异常**；
* ``GET /api/plans`` —— 共存 plan 不再落进冲突桶，条目上给出主分支与两份产物
  的相对名。

还有两条**刻意不变**的语义

----------------------------  ------------------------------------------
``POST /api/tasks/{id}/generate``  仍然 409 ``artifact_conflict``（共存是单向的）
``verification_branch_ready()``    对共存目录仍是 ``False``
----------------------------  ------------------------------------------

共存是**单向**的：允许「先任务、后补计划书」，不允许把一条纯验证分支的 plan
改造成开发分支。所以任务生成入口在目录里已有计划书时仍然拒绝 —— 错误码字符串
``artifact_conflict`` 保留（API 契约），**文案**改（不再是「两类产物互斥」，
而是「反向生成被拒」）。

而 ``verification_branch_ready()`` 对共存目录仍是 ``False``：验证分支跳过初始
执行，把共存目录当成验证分支，开发任务就会被跳过。共用谓词
``is_verification_branch()`` 的定义因此**不许**被顺手放宽。

测试数据
--------
两个产物是手写的：一个开发任务清单、一份计划书标题。判定只看**文件在不在**，
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
#: 共存目录里两份产物都在 —— 相对名，不是路径。
BOTH_ARTIFACTS: List[str] = [TASKS, PLAN_DOC]
#: 共存目录的主分支 —— 「任务优先」。
PRIMARY_BRANCH = "dev"
#: 列表里冲突计划的桶 —— 共存之后**不再有**这个桶。
CONFLICT_BUCKET = "conflict"
#: 机器可读错误码（保留：API 契约）。
ERROR_CODE = "artifact_conflict"


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


def _seed_routing_row(
    plan_id: str,
    phase: str,
    updated_at: Optional[str] = None,
) -> None:
    """写一行 ``plan_routing`` —— 让列表认得这个计划。

    ``updated_at`` 显式给出时按它写：列表的**行序**曾经由这一列决定
    （数据库按 ``updated_at`` 降序读行），所以要证明行序只由 plan id 决定，
    就得能控制这一列。
    """
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
        if updated_at is not None:
            conn.execute(
                "UPDATE plan_routing SET updated_at = ? WHERE plan_id = ?",
                (updated_at, plan_id),
            )
            conn.commit()
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


def _write_artifacts(plan_dir: Path, *names: str) -> Path:
    """在 ``plan_dir`` 里写下指定的产物。"""
    plan_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name == TASKS:
            (plan_dir / TASKS).write_text('{"tasks": []}', encoding="utf-8")
        else:
            (plan_dir / name).write_text("# 验证计划书\n", encoding="utf-8")
    return plan_dir


@pytest.fixture
def coexist_plan() -> Tuple[str, Path]:
    """一个**同时**存在两类产物的计划。

    目录名故意不带日期前缀：``classify_plan`` 只把 ``YYYYMMDD-*`` 认成历史
    归档计划，带日期就意味着这个计划在列表里是「archived」，冲突桶轮不到它。
    """
    plan_id = "coexist-plan-listing"
    plan_dir = server.PLANS_DIR / plan_id
    _write_artifacts(plan_dir, TASKS, PLAN_DOC)
    _seed_routing_row(plan_id, "queued")
    try:
        yield plan_id, plan_dir
    finally:
        _drop_routing_row(plan_id)


def _entry(payload: Any, plan_id: str) -> Dict[str, Any]:
    """从 ``GET /api/plans`` 的返回里挑出某个计划那一行。"""
    assert isinstance(payload, list), payload
    for item in payload:
        if isinstance(item, dict) and item.get("id") == plan_id:
            return item
    raise AssertionError(f"{plan_id} 不在 /api/plans 的返回里：{payload!r}")


# ---------------------------------------------------------------------------
# 1) 阶段推断：共存 → queued，不抛异常
# ---------------------------------------------------------------------------


def test_plan_state_infer_phase_coexist_is_queued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共存目录按 dev 推断为 ``queued``，**不抛异常**。

    冲突分支已经不存在了：产物判定的输出域是 ``dev`` / ``verification`` /
    ``empty`` 三个取值，共存落在 ``dev`` 上 —— 「任务优先」。
    """
    from plan_state import PlanState

    # plan-state 的持久化走自己的解析器；把它指向 tmp 目录下的库，这条
    # plan 就不会在 session 级数据库里留下一行。
    monkeypatch.setattr(
        "plan_state._state_db_path",
        lambda: tmp_path / "state.db",
    )

    plan_dir = _write_artifacts(
        tmp_path / "coexist-plan-infer", TASKS, PLAN_DOC,
    )

    state = PlanState(plan_dir)

    assert state._infer_phase() == "queued"


@pytest.mark.parametrize(
    ("artifacts", "expected"),
    [
        ((TASKS,), "queued"),
        ((PLAN_DOC,), "verification_plan_review"),
    ],
)
def test_plan_state_infer_phase_single_artifact(
    artifacts: Tuple[str, ...],
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单独一份产物时，阶段推断**维持原样**。

    共存改造动的是「两份都在」那一种情形；只改它会让另外两种跟着漂。
    """
    from plan_state import PlanState

    monkeypatch.setattr(
        "plan_state._state_db_path",
        lambda: tmp_path / "state.db",
    )

    plan_dir = _write_artifacts(tmp_path / "single-artifact-plan", *artifacts)

    assert PlanState(plan_dir)._infer_phase() == expected


def test_plan_state_infer_phase_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两份产物都没有时，阶段推断维持既有行为（``interview``）。"""
    from plan_state import PlanState

    monkeypatch.setattr(
        "plan_state._state_db_path",
        lambda: tmp_path / "state.db",
    )

    plan_dir = tmp_path / "empty-plan"
    plan_dir.mkdir()

    assert PlanState(plan_dir)._infer_phase() == "interview"


# ---------------------------------------------------------------------------
# 2) 计划列表：没有冲突桶，两份产物都在
# ---------------------------------------------------------------------------


def test_plans_list_has_no_conflict_bucket(
    coexist_plan: Tuple[str, Path],
) -> None:
    """列表不再把共存 plan 标为冲突，并把两份产物都交回去。

    共存是合法的：主分支是 dev，计划书是附加产物。条目上给的是**主分支**与
    **两份产物的相对名** —— 调用方拿它去自己的 plan 目录里找文件，不必知道
    服务器把 plan 放在哪。
    """
    plan_id, _plan_dir = coexist_plan

    resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), plan_id)
    assert entry["status"] != CONFLICT_BUCKET, (
        "共存的 plan 不许落进冲突桶；实际是 %r" % (entry.get("status"),)
    )
    assert "artifact_conflict" not in entry, (
        "共存的 plan 不是冲突，条目上不许再挂冲突载荷：%r" % (entry,)
    )
    assert entry.get("branch") == PRIMARY_BRANCH, entry
    assert entry.get("artifacts") == BOTH_ARTIFACTS, entry


def test_plan_status_payload_gives_branch_and_artifacts(
    coexist_plan: Tuple[str, Path],
) -> None:
    """单计划状态载荷同样给出主分支与两份产物的相对名。

    状态与列表读的是同一份判定（:func:`plan_artifacts.detect`），所以两个读者
    不许对同一条 plan 给出两个答案。
    """
    plan_id, _plan_dir = coexist_plan

    resp = client.get(f"/api/plan/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("branch") == PRIMARY_BRANCH, body
    assert body.get("artifacts") == BOTH_ARTIFACTS, body
    assert "artifact_conflict" not in body, body


# ---------------------------------------------------------------------------
# 3) 验证分支就绪谓词：共存目录仍然不就绪
# ---------------------------------------------------------------------------


def test_verification_branch_ready_false_for_dev_with_plan_doc(
    tmp_path: Path,
) -> None:
    """``verification_branch_ready()`` 对共存目录是 ``False``。

    验证分支跳过初始执行；把共存目录当成验证分支，开发任务就会被跳过。这个
    谓词是纯函数，直接调它 —— 它没有独立端点，端点级的断言盖不住它。
    """
    from verification_gate import verification_branch_ready

    plan_dir = _write_artifacts(tmp_path / "coexist-plan-gate", TASKS, PLAN_DOC)

    assert verification_branch_ready(plan_dir) is False


# ---------------------------------------------------------------------------
# 4) 任务生成入口：共存目录仍然被拒（单向性）
# ---------------------------------------------------------------------------


def test_task_generation_still_refused_when_plan_doc_exists(
    coexist_plan: Tuple[str, Path],
) -> None:
    """共存目录调任务生成仍然 409，错误码**不变**。

    共存是单向的：允许「先任务、后补计划书」，不允许把一条纯验证分支的 plan
    改造成开发分支。所以目录里已有计划书时，任务生成入口仍然拒绝。

    错误码字符串 ``artifact_conflict`` 保留（API 契约）；**文案**改 ——
    共存合法之后，这个 409 描述的是「反向生成被拒」，不再是「两类产物互斥」。
    """
    plan_id, plan_dir = coexist_plan

    resp = client.post(f"/api/tasks/{plan_id}/generate")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == ERROR_CODE, body
    assert body["artifacts"] == BOTH_ARTIFACTS, body
    # 被拒绝时两个产物都还在：框架不替人删文件。
    assert (plan_dir / TASKS).exists()
    assert (plan_dir / PLAN_DOC).exists()
    # 文案不许说「互斥」了 —— 共存是合法的。
    assert "互斥" not in body["detail"], body


def test_conflict_detail_does_not_mislabel_the_calling_entry_point(
    coexist_plan: Tuple[str, Path],
) -> None:
    """共存的 409 文案不许替**某一个**入口选方向。

    409 载荷是**共用**的构造器（:func:`routes.plans
    .artifact_conflict_payload`）：谁被拒就调它。构造器拿不到「是哪个入口在
    问」这条信息 —— 文案一旦说「这个入口却要生成 X」，对另一个调用者来说
    那句话就可能是假的。

    共存是**单向**的，两个方向各归各位：

    * 任务生成 → **被拒**（409 ``artifact_conflict``）：目录里已有计划书时
      不再写任务清单 —— 不把一条纯验证分支的计划改造成开发分支；
    * 计划书生成 → **放行**：计划书是任务清单之后的**可选附加动作**
      （PRD 决策点 2），「目录里已有 ``tasks.json``」不再是拒绝的理由。

    所以文案必须说**规则**，不说「这个入口」；而且不许把放行的那一侧也说成
    被拒 —— 「已有任务清单时不再写计划书」是一句假话：那个方向是放行的。
    """
    plan_id, _plan_dir = coexist_plan

    tasks_resp = client.post(f"/api/tasks/{plan_id}/generate")
    doc_resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    # 方向一：任务生成被拒。
    assert tasks_resp.status_code == 409, tasks_resp.text
    # 方向二：计划书生成放行 —— 产物门不拦它（它的答案由验证流水线自己的门
    # 决定：基线门、审核面门）。
    assert doc_resp.status_code != 409, doc_resp.text
    assert doc_resp.json().get("error") != ERROR_CODE, doc_resp.text

    tasks_detail = tasks_resp.json()["detail"]
    # 不许把方向说死：构造器不知道是谁在问。
    assert "这个入口却要生成" not in tasks_detail, tasks_detail
    # 文案必须同时说到两份产物 —— 这样它对任何调用者都不是假话。
    assert "开发任务清单" in tasks_detail, tasks_detail
    assert "验证计划书" in tasks_detail, tasks_detail
    # 被拒的方向只有一条：文案不许把放行的那一侧也说成被拒。
    assert "不再写计划书" not in tasks_detail, tasks_detail
    assert "不再写任务清单" in tasks_detail, tasks_detail


# ---------------------------------------------------------------------------
# 5) 行序：列表的顺序只由 plan id 决定
# ---------------------------------------------------------------------------

#: 三条测试用 plan 的 id —— 升序是 a、b、c。
ORDERED_PLAN_IDS: List[str] = ["listing-a", "listing-b", "listing-c"]

#: 每条 plan 的 ``updated_at``：**故意**排成与 plan id 相反的顺序。
#: 数据库按 ``updated_at`` 降序读行，于是「按写入时间排」给出的行序是
#: b、c、a —— 与「按 plan id 升序排」的 a、b、c 不是同一个答案。
#: 这份 fixture 因此能分辨：行序到底由谁决定。
ORDERED_UPDATED_AT: Dict[str, str] = {
    "listing-a": "2026-11-01T00:00:00+00:00",
    "listing-b": "2026-11-03T00:00:00+00:00",
    "listing-c": "2026-11-02T00:00:00+00:00",
}


@pytest.fixture
def ordered_plan_rows() -> List[str]:
    """三条 ``plan_routing`` 行，``updated_at`` 与 plan id 反序。"""
    for plan_id in ORDERED_PLAN_IDS:
        _write_artifacts(server.PLANS_DIR / plan_id, TASKS)
        _seed_routing_row(
            plan_id, "queued", updated_at=ORDERED_UPDATED_AT[plan_id],
        )
    try:
        yield list(ORDERED_PLAN_IDS)
    finally:
        for plan_id in ORDERED_PLAN_IDS:
            _drop_routing_row(plan_id)


def test_plans_list_rows_are_ordered_by_plan_id(
    ordered_plan_rows: List[str],
) -> None:
    """列表的行序由 plan id **唯一**决定 —— 升序。

    前端明说「顺序是列表自己的，面板不排序」（``frontend/app.js``）：所以
    列表的顺序是一份**契约**，契约得有一个稳定的依据。写入时间不是 ——
    同一批 plan 换个顺序写进去，列表就换一张，调用方按下标取行就取错了
    那条 plan。

    钉死的证据：三条行的 ``updated_at`` **故意**排成与 plan id 相反的顺序
    （见 :data:`ORDERED_UPDATED_AT`）—— 于是「按写入时间排」与「按 plan id
    排」在这份 fixture 上给出的不是同一个答案；只有后者能让这条断言变绿。
    """
    resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    ids = [
        item.get("id")
        for item in resp.json()
        if isinstance(item, dict)
    ]
    seeded = [pid for pid in ids if pid in ordered_plan_rows]

    assert seeded == ordered_plan_rows, (
        "列表的行序必须由 plan id 升序唯一决定；实际是 %r" % (seeded,)
    )
