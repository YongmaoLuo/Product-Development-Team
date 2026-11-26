"""``POST /api/verification/{id}/start`` 的源阶段集合：验证分支的放行判定。

背景
----
``/start`` 的**唯一**放行判定是 :meth:`RoutingRepository.try_mark_phase` 的源阶段
集合：计划当前的阶段不在集合里 → ``409 {"error": "conflict", "reason":
"stage_mismatch"}``。阶段词汇表变大**不会**让新阶段自动被接纳——所以「验证分支
已就绪」这个事实必须由 :func:`verification_gate.verification_branch_ready` 回答，
而它的答案只以「多一个源阶段」的形式进入 CAS。

本文件钉住六件事：

* **验证分支六维全真** → 200，阶段推进到 ``verification_running``；
* **开发分支的源阶段元组逐项不变**（冻结值断言）——验证分支的放行不得收窄
  开发分支的既有可达性；
* **停放的计划够得着**（``waiting_for_quota`` 条件追加进源阶段集合），
  **没停放的计划够不着**（同一项不进冻结的基准元组）；
* **审核不完备 / 基线漂移** → 既有 409，**不新增错误码**；
* **既有的 409 用例**（阶段不匹配）保持绿；
* ``verification_plan_review`` **不在** ``begin_verification`` 的「已是验证态」
  集合里——从该阶段调 ``begin_verification`` 仍然进入 ``verification``。

测试数据
--------
真实 git 仓库（两个提交 + ``refs/pull/7/head``）与一份 canonical 验证计划书。
真实 git 是刻意的：基线维度问的就是「ref 当前解析到哪个 commit」，mock 掉它等于
把要测的维度测没了。审核条目用 :func:`record_action` 落盘，与生产路径同一条。

夹具
----
四个夹具（``make_dev_plan`` / ``make_verification_plan`` / ``no_worker_thread`` /
``cas_spy``）与那份 canonical 计划书住在
:mod:`tests.fixtures.verification_mode`：pytest 只在**提问的那个模块**的命名空间里
找夹具，所以住在本文件里的夹具别的模块用不了——要复用只能复制一份，而复制出来的
那份会漂。

线程
----
``/start`` 在 CAS 之后会 spawn 一个验证 worker 线程。这些用例测的是**线程启动
之前**的那次 CAS，所以线程被替换成不执行 target 的空壳——worker 真的会去驱动
整条验证回路。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
import verification_gate  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    REVIEW_PHASE,
    cas_spy,
    hermetic_env,
    make_dev_plan,
    make_verification_plan,
    no_worker_thread,
    record_routing_row,
)

client = TestClient(server.app)


#: 开发分支的源阶段集合 —— **冻结值**。
#:
#: 验证分支多一个源阶段是这次改动唯一允许的形状，开发分支的这条元组必须
#: 逐项不变：多一项就意味着开发分支的既有可达性被悄悄收窄了。
DEV_SOURCE_PHASES: Tuple[str, ...] = (
    "executing",
    "verification",
    "verification_failed",
    "verification_loop_stopped",
    "verification_passed",
    "completed",
    "failed",
    "stopped",
    "verification_repairing",
)


def _routing_phase(plan_id: str) -> Optional[str]:
    """Read the plan's routing phase straight out of the state DB."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 验证分支：就绪 → 放行
# ---------------------------------------------------------------------------


def test_verification_branch_start_succeeds(
    make_verification_plan,
    no_worker_thread,
) -> None:
    """六维全真的验证分支 → 200，阶段推进到 ``verification_running``。"""
    plan = make_verification_plan()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "started"
    assert _routing_phase(plan.plan_id) == "verification_running"


def test_verification_branch_source_tuple_gains_the_review_phase(
    make_verification_plan,
    no_worker_thread,
    cas_spy,
) -> None:
    """就绪时，源阶段元组多出 ``verification_plan_review`` 一项。"""
    plan = make_verification_plan()

    client.post(f"/api/verification/{plan.plan_id}/start")

    assert cas_spy, "try_mark_phase was never called"
    _plan_id, sources, new_phase = cas_spy[-1]
    assert new_phase == "verification_running"
    assert REVIEW_PHASE in sources


# ---------------------------------------------------------------------------
# 2. 开发分支：源阶段元组逐项不变
# ---------------------------------------------------------------------------


def test_dev_source_tuple_byte_identical(
    make_dev_plan,
    no_worker_thread,
    cas_spy,
) -> None:
    """开发分支的源阶段元组与冻结值**逐项**相等。

    逐项比而不是整体比：整体比在长度不同时报的是一句「不等」，看不出是
    多了哪一项、少了哪一项。验证分支多一项是这次改动唯一允许的形状。
    """
    plan = make_dev_plan()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert cas_spy, "try_mark_phase was never called"
    _plan_id, sources, _new_phase = cas_spy[-1]
    assert len(sources) == len(DEV_SOURCE_PHASES), (
        f"开发分支的源阶段元组长度变了：{sources}"
    )
    for index, (got, want) in enumerate(zip(sources, DEV_SOURCE_PHASES)):
        assert got == want, (
            f"开发分支的源阶段元组第 {index} 项变了：{got!r} != {want!r}"
        )


# ---------------------------------------------------------------------------
# 2b. 停放的计划：条件追加的那一项
# ---------------------------------------------------------------------------
#
# ``waiting_for_quota`` 不是基准元组里的一项——基准元组是冻结值，逐项不变。
# 它是**条件追加**的一项：只有当计划自己停在 ``waiting_for_quota`` 上时，
# 它才进源阶段集合。
#
# 两个方向都要钉住：
#
# * 停放的计划要**够得着**——CAS 在轮次解析**之前**按源阶段集合放行，集合
#   里没有这一项，停放的计划永远拿不到 ``409 stage_mismatch`` 之外的东西，
#   也就永远续不上它停下的那一轮；
# * 非停放的计划要**够不着**——把这一项无条件塞进基准元组，看着一样能跑，
#   实际是把开发分支的既有可达性悄悄改掉了（这正是冻结值要拦的形状）。


def test_parked_plan_gains_the_quota_source_phase(
    make_dev_plan,
    no_worker_thread,
    cas_spy,
) -> None:
    """停在 ``waiting_for_quota`` 的计划：源阶段集合多出这一项，且放行。"""
    plan = make_dev_plan()
    record_routing_row(
        plan.plan_id,
        phase="waiting_for_quota",
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    )

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert cas_spy, "try_mark_phase was never called"
    _plan_id, sources, new_phase = cas_spy[-1]
    assert new_phase == "verification_running"
    assert "waiting_for_quota" in sources, (
        f"停放的计划不在源阶段集合里，它永远续不上：{sources}"
    )


def test_non_parked_plan_does_not_gain_the_quota_source_phase(
    make_dev_plan,
    no_worker_thread,
    cas_spy,
) -> None:
    """没停放的计划：源阶段集合里没有这一项（基准元组逐项不变）。"""
    plan = make_dev_plan()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert cas_spy, "try_mark_phase was never called"
    _plan_id, sources, _new_phase = cas_spy[-1]
    assert "waiting_for_quota" not in sources, (
        f"停放的源阶段被无条件塞进了基准元组：{sources}"
    )


# ---------------------------------------------------------------------------
# 3. 谓词为假 → 既有 409，不新增错误码
# ---------------------------------------------------------------------------


def test_incomplete_review_or_baseline_409(
    make_verification_plan,
    no_worker_thread,
) -> None:
    """审核不完备 / 基线漂移 → 409 ``stage_mismatch``。"""
    incomplete = make_verification_plan(review_complete=False)
    resp = client.post(f"/api/verification/{incomplete.plan_id}/start")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict"
    assert body["reason"] == "stage_mismatch"

    drifted = make_verification_plan(ref_moved=True)
    resp = client.post(f"/api/verification/{drifted.plan_id}/start")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict"
    assert body["reason"] == "stage_mismatch"


def test_predicate_exception_is_treated_as_not_ready(
    make_verification_plan,
    no_worker_thread,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """谓词抛异常 → 按假处理：409，而不是 500。

    「读不懂」与「没准备好」对调用方是同一件事：还没到能跑的时候。
    """
    plan = make_verification_plan()

    def _boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(verification_gate, "verification_branch_ready", _boom)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 409, resp.text
    assert resp.json()["reason"] == "stage_mismatch"


# ---------------------------------------------------------------------------
# 4. 既有的 409 用例保持绿
# ---------------------------------------------------------------------------


def test_existing_stage_mismatch_still_green(
    hermetic_env: Path,
    plan_sqlite_seeder,
    no_worker_thread,
) -> None:
    """阶段不在源集合里 → 既有 409 ``stage_mismatch`` 保持不变。"""
    plan_id = "prd-review-plan"
    plan_dir = hermetic_env / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text('{"tasks": []}', encoding="utf-8")

    record_routing_row(
        plan_id,
        phase="prd_review",
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    )
    project_dir = plan_dir / "project"
    project_dir.mkdir()
    plan_sqlite_seeder(
        plan_dir, plan_id, phase="prd_review", project_dir=str(project_dir)
    )

    resp = client.post(f"/api/verification/{plan_id}/start")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict"
    assert body["reason"] == "stage_mismatch"


# ---------------------------------------------------------------------------
# 5. ``begin_verification`` 的「已是验证态」集合不含新阶段
# ---------------------------------------------------------------------------


def test_begin_verification_states_exclude_review_phase(
    make_verification_plan,
) -> None:
    """``verification_plan_review`` 不在「已是验证态」集合里。

    ``begin_verification`` 在「已经是某个验证态」时是**幂等空操作**。新阶段
    如果被算进那个集合，从它调 ``begin_verification`` 就什么都不会发生——
    计划会永远卡在「计划书待审」。所以从该阶段调 ``begin_verification``
    仍然必须进入 ``verification``。
    """
    from plan_state import PlanState

    plan = make_verification_plan()

    ps = PlanState(plan.plan_dir)
    ps.reload()
    assert ps.get_current_phase() == REVIEW_PHASE

    ps.begin_verification()

    assert ps.get_current_phase() == "verification"
