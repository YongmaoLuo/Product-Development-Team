"""stop 门面的**裁决**：杀不掉（``kill_incomplete``）与孤儿执行行。

被钉住的行为
------------
1. **杀不掉 → 不算停** —— 处理器说「还有 pid 活着」，门面就不能对外说
   「停了」：相位一个字节都不动，不写 checkpoint，不迁回队列。相位说
   「stopped」而进程还在跑，是同一个谎，只是高了一层：slot 被提前释放，
   第二个执行器接手同一个计划。
2. **幸存的 pid 被点名** —— 那几个 pid 写进一条 WARNING：操作员要能看见
   「到底是谁没死」。
3. **孤儿执行行 → 先停到 ``stopped``，再按 requeue 处理** —— 进程重启之后，
   执行行还说着「在跑」，那个进程确实也还活着，但**这个进程没有它的句柄**
   （句柄随进程一起没了）。门面原来走 idle 分支：计划既没真停，也没回
   队列 —— 一个还在跑的计划在队列里排着号，谁也不知道它还在跑。
4. **没有执行行 → 还是 idle** —— 孤儿判定为假时走原来的 idle 路径，
   相位原样不动，requeue 被忽略。

被测的是门面的**裁决**，不是端点自己的语义：既有 stop 端点在这里被换成
记录器（返回它真实会返回的那种 dict），端点语义另有测试钉着。
"""

from __future__ import annotations

import json
import logging
import os
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


def _persisted_phase(plan_dir: Path) -> str:
    """读回计划**落库**的相位。"""
    from plan_state import PlanState

    return PlanState(plan_dir).get_current_phase()


def _write_exec_row(plan_id: str, **fields: Any) -> None:
    """把执行行写进状态库（真实 stop 留下的那一行就是它）。"""
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
def stop_handler(monkeypatch):
    """把既有 stop 端点换成记录器；返回装结论的函数。

    ``import server`` 必须排在 ``from routes import ...`` 前面：
    ``routes.execution`` 在模块层 import ``server``，而 ``server`` 在
    文件末尾 import 这个 routes 模块 —— 顺序反了就是一次半初始化的循环
    导入。

    装进去的结论就是真实端点会返回的那种 dict（``status`` 加上可能有的
    ``surviving_pids``）。**返回 ``None``** 是旧假实现的形状：门面必须
    把它当成「停成功了」。
    """
    import server  # noqa: F401  (import 顺序是承重墙)

    from routes import execution as routes_execution

    calls = []
    verdict: Dict[str, Any] = {"value": None}

    def fake_stop_execution(plan_id, discard=False):
        calls.append(("execution", plan_id, discard))
        return verdict["value"]

    monkeypatch.setattr(routes_execution, "stop_execution", fake_stop_execution)
    return calls, verdict


# ---------------------------------------------------------------------------
# kill_incomplete —— 进程还活着就不算停
# ---------------------------------------------------------------------------


def test_kill_incomplete_keeps_the_phase_and_skips_requeue(
    plans_dir, clean_state, stop_handler,
):
    """处理器说「没杀干净」→ 门面不算停。

    相位一个字节都不动（还是 ``executing``），不写 checkpoint，也不迁回
    队列：迁回队列等于把这个还在跑的计划排进下一次执行，那一次执行会
    变成第二个执行器。
    """
    from plan_lifecycle_service import PlanLifecycleService

    calls, verdict = stop_handler
    verdict["value"] = {"status": "kill_incomplete", "surviving_pids": [4242]}

    plan_id = "kill-incomplete-p1"
    _make_plan_dir(plans_dir, plan_id, phase="executing")
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    # 处理器被派发了（它说的话是要听的）。
    assert calls == [("execution", plan_id, False)]
    # 结论是「没停成」，不是「停了」。
    assert result.detail == "kill_incomplete"
    # 相位不变 —— 还是 executing，不是 stopped。
    assert result.berth_phase == "executing"
    assert result.final_phase == "executing"
    assert _persisted_phase(plans_dir / plan_id) == "executing"
    # 没有 checkpoint：恢复点挨着一个还在跑的进程就是一个二次执行的入口。
    assert result.checkpoint is None
    assert not (plans_dir / plan_id / "pause_state.json").exists()
    # 没有被塞进队列。
    assert result.final_phase != "queued"


def test_kill_incomplete_logs_surviving_pids(
    plans_dir, clean_state, stop_handler, caplog,
):
    """幸存的 pid 被点名 —— 操作员要看得见「到底是谁没死」。"""
    from plan_lifecycle_service import PlanLifecycleService

    _, verdict = stop_handler
    verdict["value"] = {"status": "kill_incomplete", "surviving_pids": [4242]}

    plan_id = "kill-incomplete-p2"
    _make_plan_dir(plans_dir, plan_id, phase="executing")
    _mark_execution_alive(plan_id)

    with caplog.at_level(logging.WARNING):
        result = PlanLifecycleService().stop_plan(plan_id)

    assert result.detail == "kill_incomplete"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("4242" in r.getMessage() for r in warnings), [
        r.getMessage() for r in warnings
    ]


def test_a_handler_without_a_verdict_is_still_a_stop(
    plans_dir, clean_state, stop_handler,
):
    """旧假实现返回 ``None`` → 照旧当成「停成功了」。

    门面不能因为结论缺失就把一次正常的停变成「没停成」—— 那会让每一次
    停止都变成一次谎报。
    """
    from plan_lifecycle_service import PlanLifecycleService

    _, verdict = stop_handler
    verdict["value"] = None

    plan_id = "no-verdict-p1"
    _make_plan_dir(plans_dir, plan_id, phase="queued")
    _mark_execution_alive(plan_id)

    result = PlanLifecycleService().stop_plan(plan_id)

    assert result.detail == "stop_execution"
    assert result.berth_phase == "stopped"
    assert result.final_phase == "stopped"


# ---------------------------------------------------------------------------
# 孤儿执行行 —— 进程重启之后留下的那一行
# ---------------------------------------------------------------------------


def test_orphan_execution_is_parked_and_requeued(
    plans_dir, clean_state, stop_handler,
):
    """孤儿执行行 → 先停到 ``stopped``，再按 requeue 迁回队列。

    现场：内存表里没有这个计划（这个进程没有那个执行的句柄），执行行却
    还说着「在跑」，而行上的 pid 确实还活着。门面原来走 idle 分支：相位
    一字节不动，计划既没停也没回队列 —— 一个还在跑的计划在队列里排着号。
    """
    from plan_lifecycle_service import PlanLifecycleService

    calls, _ = stop_handler

    plan_id = "orphan-p1"
    _make_plan_dir(plans_dir, plan_id, phase="executing")
    # 内存表是空的：这个进程没有那个执行的句柄（重启之后的孤儿）。
    _write_exec_row(
        plan_id,
        exec_status="running",
        exec_pid=os.getpid(),
        stop_reason=None,
    )
    assert _exec_row(plan_id).get("exec_status") == "running"

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    # 停到 stopped，再迁回队列 —— 终态是 queued。
    assert result.berth_phase == "stopped"
    assert result.final_phase == "queued"
    assert result.detail == "orphan_stop+requeue"
    assert _persisted_phase(plans_dir / plan_id) == "queued"
    # 孤儿这一支不派发处理器：没有句柄可停，门面只停相位。
    assert calls == []


def test_idle_without_running_row_stays_idle(
    plans_dir, clean_state, stop_handler,
):
    """没有执行行 → 孤儿判定为假 → 走 idle 原路径。"""
    from plan_lifecycle_service import PlanLifecycleService

    calls, _ = stop_handler

    plan_id = "idle-p2"
    _make_plan_dir(plans_dir, plan_id, phase="executing")

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    assert calls == []
    assert result.dispatched is None
    assert result.detail == "idle"
    assert result.berth_phase == "executing"
    assert result.final_phase == "executing"
    assert result.checkpoint is None
    assert _persisted_phase(plans_dir / plan_id) == "executing"


def test_a_dead_pid_is_not_an_orphan(
    plans_dir, clean_state, stop_handler,
):
    """行上说「在跑」但 pid 已经没了 → 不是孤儿，照旧 idle。

    活着的那一半才是孤儿：进程真的还在跑，门面才需要把它停到 stopped。
    一个早就没了的 pid 只是行上没清干净的残留。
    """
    from plan_lifecycle_service import PlanLifecycleService

    calls, _ = stop_handler

    plan_id = "dead-pid-p1"
    _make_plan_dir(plans_dir, plan_id, phase="executing")
    _write_exec_row(
        plan_id,
        exec_status="running",
        exec_pid=_a_dead_pid(),
        stop_reason=None,
    )

    result = PlanLifecycleService().stop_plan(plan_id, requeue=True)

    assert calls == []
    assert result.detail == "idle"
    assert result.final_phase == "executing"
    assert _persisted_phase(plans_dir / plan_id) == "executing"


def _a_dead_pid() -> int:
    """一个确实已经不在了的 pid（**不 spawn 任何子进程**）。

    从一段高位 pid 里挑第一个探针说「不在了」的：探针与孤儿判定用的是
    同一个，所以这个选择与被测的判据自洽。
    """
    from plan_scheduler.facts import _pid_is_alive

    for candidate in range(999_000, 999_100):
        if not _pid_is_alive(candidate):
            return candidate
    raise AssertionError("no dead pid found in the scanned range")


def test_a_cas_conflict_on_the_orphan_stop_is_not_swallowed(
    plans_dir, clean_state, stop_handler, monkeypatch,
):
    """孤儿那一支的迁移撞上 CAS 冲突 → 原样外抛，不重试。

    CAS 冲突不是「那条边不合法」，而是「行在读与写之间被别的手改了」。
    重试就是拿自己的写入盖掉别人的 —— 所以它照既有语义往外抛。
    """
    import plan_state
    from state_machine.repositories.routing_repository import ConflictError

    from plan_lifecycle_service import PlanLifecycleService

    def boom(self, phase):
        raise ConflictError("orphan-p2", "the routing row changed under us")

    monkeypatch.setattr(plan_state.PlanState, "transition_to", boom)

    plan_id = "orphan-cas-p1"
    _make_plan_dir(plans_dir, plan_id, phase="executing")
    _write_exec_row(
        plan_id,
        exec_status="running",
        exec_pid=os.getpid(),
        stop_reason=None,
    )

    with pytest.raises(ConflictError):
        PlanLifecycleService().stop_plan(plan_id, requeue=True)

    # 相位原样不动 —— 冲突不改相位。
    assert _persisted_phase(plans_dir / plan_id) == "executing"


# ---------------------------------------------------------------------------
# 进程内调用（不新增依赖）
# ---------------------------------------------------------------------------


def test_the_facade_never_reaches_for_an_http_client():
    """门面必须进程内直接调用处理器（禁止 httpx / requests）。"""
    import ast

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


def test_the_orphan_check_uses_the_facts_pid_probe():
    """孤儿判定用的 pid 探针必须是 ``plan_scheduler.facts`` 那一个。

    自己重写一份 ``os.kill(pid, 0)`` 就是第二个「pid 是不是活着」的判
    据 —— 两个判据迟早对不上，而对不上的那一次就是一个被当成活着的死
    pid（或者反过来）。
    """
    import inspect

    from plan_lifecycle_service import PlanLifecycleService

    source = inspect.getsource(PlanLifecycleService._persisted_execution_running)
    assert "plan_scheduler.facts" in source, source
    assert "_pid_is_alive" in source, source


def test_the_stop_handler_conclusion_is_not_dropped():
    """``_stop_execution`` 必须把处理器的结论带回来。

    丢了它，门面就永远不知道「进程到底有没有死透」—— 那正是这个模块要
    回答的问题。
    """
    import inspect

    from plan_lifecycle_service import PlanLifecycleService

    source = inspect.getsource(PlanLifecycleService._stop_execution)
    assert "return" in source, source


def test_the_result_type_still_has_exactly_six_fields():
    """``LifecycleResult`` 的六个字段是冻住的（跨仓契约 §1.3）。"""
    import dataclasses

    from plan_lifecycle_service import LifecycleResult

    names = [f.name for f in dataclasses.fields(LifecycleResult)]
    assert names == [
        "plan_id",
        "dispatched",
        "berth_phase",
        "final_phase",
        "checkpoint",
        "detail",
    ]
