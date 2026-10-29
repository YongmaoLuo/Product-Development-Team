"""plan 级统一 stop 门面：停泊相位与 requeue（R11 · 任务 18）。

被钉住的行为
------------
1. **停泊相位（``berth_phase``）恒为 ``'stopped'``** —— 派发了处理器就说明
   计划被停下来了，它停在哪儿就是停泊相位。
2. **最终相位（``final_phase``）**：
   * 派发了、``requeue=False`` → ``'stopped'``；
   * 派发了、``requeue=True`` → 迁回等待相位成功则是 ``'queued'``；
   * 迁移失败（``ValueError``）→ 回落 ``'stopped'``，**stop 不因此变成 500**。
3. **idle 忽略 requeue** —— 没有在跑的东西就没有可停的：停泊相位与最终相位
   都等于当前相位，``checkpoint`` 是 ``None``，计划**不被塞进队列**。
4. **checkpoint 只读回显** —— 读 ``read_pause_checkpoint(plan_dir)`` 把它带
   回来；门面**不消费、不删除**它（消费是恢复路径的事）。
5. **requeue 的迁移是 ``stopped → queued``** —— 那条边由状态机承担，门面
   只发起迁移，不自己改相位。
6. **requeue 之后执行行的终态裁决被清掉** —— 迁回只动了路由相位；执行行
   上那条 ``stopped`` 是 stop 写的。计划列表的终态判定读**两个**裁决
   （``routes/plans.py`` 的 ``_plan_is_terminal``），两个裁决互相矛盾时
   计划被列表过滤掉 —— 用户点了「重新排队」，卡片却从页面上消失了。

被测的是门面的**相位语义**。既有 stop 端点在这里被换成记录器，外加它真实
会做的两件事（写下 pause checkpoint、把相位停到 ``stopped``）—— 门面的
``checkpoint`` 与相位都要对着这两件事断言。端点自身的语义另有测试钉着。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

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


def _write_checkpoint(plan_dir: Path, kind: str) -> None:
    """模拟真实 stop 的第 3 步：写下 pause checkpoint。"""
    from pause_state import write_pause_checkpoint

    write_pause_checkpoint(
        plan_dir,
        kind=kind,
        project_dir=str(plan_dir),
        reason="user_requested",
    )


def _park_as_stopped(plan_dir: Path) -> None:
    """模拟真实 stop 的第 4 步：把相位停到 ``stopped``。"""
    from plan_state import PlanState

    ps = PlanState(plan_dir)
    ps.reload()
    ps.transition_to("stopped")


def _persisted_phase(plan_dir: Path) -> str:
    """读回计划**落库**的相位（相位以 SQLite 为准，不是遗留 json 文件）。"""
    from plan_state import PlanState

    return PlanState(plan_dir).get_current_phase()


def _write_exec_verdict(plan_id: str, **fields: Any) -> None:
    """把执行行的裁决写进状态库。

    真实 stop 走 ``routes/execution.py`` 的 ``_stop_running_execution``，
    它落下的正是这条 ``exec_status='stopped'`` —— requeue 那条路径没有碰
    它。这里把那次写入摆到门面前面，模拟「停已经发生」的现场。
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(server._state_db_path(None))
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        if repo.summary(plan_id) is None:
            repo.insert(plan_id, **fields)
        else:
            repo.update_fields(plan_id, **fields)
    finally:
        conn.close()


def _exec_row(plan_id: str) -> Dict[str, Any]:
    """读回执行行（读不到就是空 dict）。"""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(server._state_db_path(None))
    try:
        row = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    return row or {}


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
def stop_handlers(monkeypatch, plans_dir):
    """把既有 stop 端点换成记录器，返回调用记录列表。

    真实 stop 的顺序是「标记 → 终止 → 写 checkpoint → 停到 stopped」。这里
    保留后面两步：前面两步是进程内的进程状态，与本任务无关；而后面两步
    正是门面的 ``checkpoint`` 与相位语义要对着的对象。

    ``import server`` 必须排在 ``from routes import ...`` 前面：
    ``routes.execution`` / ``routes.verification`` 在模块层 import
    ``server``，而 ``server`` 在文件末尾 import 这两个 routes 模块 ——
    顺序反了就是一次半初始化的循环导入。
    """
    import server  # noqa: F401  (import 顺序是承重墙)

    from routes import execution as routes_execution
    from routes import verification as routes_verification

    calls = []

    def fake_stop_execution(plan_id, discard=False):
        calls.append(("execution", plan_id, discard))
        plan_dir = plans_dir / plan_id
        _write_checkpoint(plan_dir, "execution")
        _park_as_stopped(plan_dir)

    def fake_stop_verification(plan_id):
        calls.append(("verification", plan_id))
        plan_dir = plans_dir / plan_id
        _write_checkpoint(plan_dir, "verification")
        _park_as_stopped(plan_dir)

    monkeypatch.setattr(routes_execution, "stop_execution", fake_stop_execution)
    monkeypatch.setattr(
        routes_verification, "stop_verification", fake_stop_verification,
    )
    return calls


# ---------------------------------------------------------------------------
# requeue
# ---------------------------------------------------------------------------


def test_requeue_lands_queued_and_reports_both_phases(
    plans_dir, clean_state, stop_handlers,
):
    """``requeue=True`` → 计划落回 ``queued``，两个相位各报各的。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-p1"
    _make_plan_dir(plans_dir, plan_id, phase="queued")
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    # 停还是停了 —— requeue 不替代 stop。
    assert stop_handlers == [("execution", plan_id, False)]
    assert result.dispatched == "execution"
    # 停泊相位：停完之后停在哪儿。
    assert result.berth_phase == "stopped"
    # 最终相位：调用方最终看到的相位。
    assert result.final_phase == "queued"
    assert result.detail == "stop_execution+requeue"
    # checkpoint 是回显，不是消费。
    assert result.checkpoint is not None
    assert result.checkpoint["kind"] == "execution"
    # 回显不等于消费：checkpoint 文件还在原地。
    assert (plans_dir / plan_id / "pause_state.json").exists()
    # 计划真的进了等待相位（不是只在返回值里说说）。
    assert _persisted_phase(plans_dir / plan_id) == "queued"


def test_no_requeue_stays_stopped(plans_dir, clean_state, stop_handlers):
    """``requeue=False`` → 停在 ``stopped``，不进队列。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "norequeue-p1"
    _make_plan_dir(plans_dir, plan_id, phase="queued")
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id)

    assert stop_handlers == [("execution", plan_id, False)]
    assert result.dispatched == "execution"
    assert result.berth_phase == "stopped"
    assert result.final_phase == "stopped"
    assert result.detail == "stop_execution"
    assert result.checkpoint is not None
    assert result.checkpoint["kind"] == "execution"
    assert _persisted_phase(plans_dir / plan_id) == "stopped"


def test_idle_requeue_is_ignored(plans_dir, clean_state, stop_handlers):
    """idle → requeue 被忽略：不把没在跑的计划塞进队列。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "idle-p1"
    _make_plan_dir(plans_dir, plan_id, phase="executing")

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    assert stop_handlers == []
    assert result.dispatched is None
    # 没有东西可停，两个相位都等于当前相位。
    assert result.berth_phase == "executing"
    assert result.final_phase == "executing"
    assert result.checkpoint is None
    assert result.detail == "idle"
    # 相位原样不动 —— 没有被塞进队列。
    assert _persisted_phase(plans_dir / plan_id) == "executing"


def test_requeue_transition_failure_keeps_stopped(
    plans_dir, clean_state, stop_handlers, monkeypatch, caplog,
):
    """迁移抛 ``ValueError`` → 记 warning，最终相位回落 ``stopped``。

    stop 本身是成功的：迁移失败只是「没进队列」，不能把它变成一个 500。
    """
    import plan_state

    from plan_lifecycle_service import PlanLifecycleService

    real_transition_to = plan_state.PlanState.transition_to

    def refuse_queued(self, phase):
        if phase == "queued":
            raise ValueError(
                "Illegal transition from 'stopped' to 'queued'. Allowed: []"
            )
        return real_transition_to(self, phase)

    monkeypatch.setattr(plan_state.PlanState, "transition_to", refuse_queued)

    plan_id = "requeue-fail-p1"
    _make_plan_dir(plans_dir, plan_id, phase="queued")
    _mark_execution_alive(plan_id)

    with caplog.at_level(logging.WARNING):
        result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    # 停还是停了，迁移失败没有把它变成异常。
    assert stop_handlers == [("execution", plan_id, False)]
    assert result.dispatched == "execution"
    assert result.berth_phase == "stopped"
    assert result.final_phase == "stopped"
    # 计划仍停在 stopped —— 没有被塞进队列。
    assert _persisted_phase(plans_dir / plan_id) == "stopped"
    # 失败被记下来了（不是静默吞掉）。
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("requeue" in r.getMessage() for r in warnings), [
        r.getMessage() for r in warnings
    ]


def test_requeue_clears_the_terminal_execution_verdict(
    plans_dir, clean_state, stop_handlers,
):
    """requeue 之后，执行行上的终态裁决必须被清掉。

    为什么这是必须的：计划列表的终态判定读**两个**裁决
    （``routes/plans.py`` 的 ``_plan_is_terminal``）—— 路由相位不在终态
    相位里时，执行裁决 ``stopped`` / ``failed`` **仍然是**终态。

    两条裁决不同步：stop 把 ``stopped`` 写进执行行
    （``routes/execution.py`` 的 ``_stop_running_execution``），requeue 只
    迁回路由相位（``stopped → queued``）—— 它没有碰执行行。于是被迁回
    队列的计划**不进列表**：用户点了「重新排队」，卡片却从页面上消失了。

    终态判据本身不能动（它的第二个判据是为了兜住「进程崩了但路由相位
    没跟上」那条路径），要修的是 requeue 这条路径 —— 把执行行的终态裁决
    清掉，让两个裁决重新一致。
    """
    from plan_lifecycle_service import PlanLifecycleService
    from routes.plans import _plan_is_terminal

    plan_id = "requeue-verdict-p1"
    _make_plan_dir(plans_dir, plan_id, phase="queued")
    _mark_execution_alive(plan_id)
    # 真实 stop 留下的那条执行行裁决。
    _write_exec_verdict(
        plan_id,
        exec_status="stopped",
        exec_pid=None,
        stop_reason="user_requested",
    )
    assert _exec_row(plan_id).get("exec_status") == "stopped"

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    assert result.final_phase == "queued"
    row = _exec_row(plan_id)
    # 执行裁决不再是终态 —— 计划的两个裁决一致了，它因此能回到列表里。
    assert not _plan_is_terminal(
        _persisted_phase(plans_dir / plan_id), row.get("exec_status")
    ), (
        f"the requeued plan is still terminal: phase="
        f"{_persisted_phase(plans_dir / plan_id)!r}, exec_status="
        f"{row.get('exec_status')!r}"
    )
    # 执行进程已经没了，pid 不能留在行上（accident B 的形状）。
    assert row.get("exec_pid") is None, (
        f"the requeued plan still carries a pid: {row.get('exec_pid')!r}"
    )
    # 上一次运行留下的停止理由属于那一轮，不属于排队中的计划。
    assert row.get("stop_reason") is None, (
        f"the requeued plan still carries a stop reason: "
        f"{row.get('stop_reason')!r}"
    )
