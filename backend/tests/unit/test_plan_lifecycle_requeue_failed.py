"""requeue 门面对 ``failed`` 相位开放。

背景
----
调度面板对 ``failed`` 的计划点「启动」必然 409：那条路径调
``PlanLifecycleService.requeue_plan``，而门面只认 ``stopped``。

状态机那边的边**一直都在**：``plan_state.PHASE_TRANSITIONS["failed"]`` 里
有 ``queued`` 这一行。收窄只发生在门面 —— 门面的入口相位集合少写了一个
名字，于是那条本来合法的边永远走不到。

被钉住的行为
------------
1. **``failed`` → ``queued``** —— 失败的计划可以被放回等待相位：
   ``ok`` 是 ``True``、``final_phase`` 是 ``'queued'``、``detail`` 是
   ``'requeue'``，相位真的落库成 ``queued``。
2. **``executing`` 仍然是冲突** —— 在跑的计划没有「放回等待相位」这回事：
   那是一次停止加一次入队，两件事，不能用一个名字糊过去。冲突的结果是
   ``ok=False`` / ``detail='conflict'``，两个相位都还是 ``executing``，
   相位一个字节都不动。
3. **``stopped`` 行为不变** —— 仍然是 ``stopped → queued``。
4. **``post_processing`` → ``queued``** —— 收尾中的计划被人回答之后必须
   回到队列：否则 PRD A9 的「唤醒后重新排队」没有实现（等待中的计划不占
   执行槽位，但回答之后计划永远回不到队列里，就再也拿不到槽位）。
5. **``completed`` 与 ``verification_running`` 仍然是冲突** —— 入口相位
   集合只多了一个名字，没有被放宽成「任何非终态都能 requeue」。

被测的是**入口相位集合**这一个东西：状态机的边表（``post_processing →
queued`` 那条边）同步加了，执行行上的终态裁决没动（那是另一条路径的事）。
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


#: 本任务 brief 的输入示例里用的名字 —— 与 ``_make_plan_dir`` 是同一个助手。
_write_plan = _make_plan_dir


def _persisted_phase(plan_dir: Path) -> str:
    """读回计划**落库**的相位（相位以 SQLite 为准，不是遗留 json 文件）。"""
    from plan_state import PlanState

    return PlanState(plan_dir).get_current_phase()


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


# ---------------------------------------------------------------------------
# requeue 的入口相位集合
# ---------------------------------------------------------------------------


def test_requeue_failed_lands_queued(plans_dir, clean_state):
    """``failed`` → 放回等待相位：``ok=True``，相位落库 ``queued``。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-failed-p1"
    plan_dir = _write_plan(plans_dir, plan_id, "failed")
    # 前置事实：计划确实停在 ``failed``（相位以落库的那份为准）。
    assert _persisted_phase(plan_dir) == "failed"

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is True
    assert result.final_phase == "queued"
    assert result.berth_phase == "queued"
    assert result.detail == "requeue"
    # 相位真的动了（落库的那份，不是只在返回值里说说）。
    assert _persisted_phase(plan_dir) == "queued"


def test_requeue_executing_still_conflicts(plans_dir, clean_state):
    """``executing`` → 冲突，相位一个字节都不动。

    在跑的计划没有「放回等待相位」这回事：那是「停掉」加「入队」两件事。
    放开门面只针对 ``failed`` —— ``executing`` 仍然是冲突。
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-executing-p1"
    plan_dir = _write_plan(plans_dir, plan_id, "executing")
    assert _persisted_phase(plan_dir) == "executing"

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is False
    assert result.detail == "conflict"
    # 冲突不改相位：两个相位都还是计划原来所在的那个相位。
    assert result.final_phase == "executing"
    assert result.berth_phase == "executing"
    assert _persisted_phase(plan_dir) == "executing"


def test_requeue_stopped_unchanged(plans_dir, clean_state):
    """``stopped`` → 行为不变：仍然是 ``stopped → queued``。"""
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-stopped-p1"
    plan_dir = _write_plan(plans_dir, plan_id, "stopped")
    assert _persisted_phase(plan_dir) == "stopped"

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is True
    assert result.final_phase == "queued"
    assert result.berth_phase == "queued"
    assert result.detail == "requeue"
    assert _persisted_phase(plan_dir) == "queued"


def test_requeue_post_processing_lands_queued(plans_dir, clean_state):
    """``post_processing`` → 放回等待相位：``ok=True``，相位落库 ``queued``。

    收尾中的计划被人回答（HITL 普通回答 → ``resume`` → ``requeue_plan``）
    之后必须回到队列 —— 否则它永远拿不到执行槽位，PRD A9 的「唤醒后
    重新排队」就没有实现。
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-post-processing-p1"
    plan_dir = _write_plan(plans_dir, plan_id, "post_processing")
    assert _persisted_phase(plan_dir) == "post_processing"

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is True
    assert result.final_phase == "queued"
    assert result.berth_phase == "queued"
    assert result.detail == "requeue"
    # 相位真的动了（落库的那份，不是只在返回值里说说）。
    assert _persisted_phase(plan_dir) == "queued"


def test_requeue_completed_still_conflicts(plans_dir, clean_state):
    """负控：``completed`` → 冲突，相位一个字节都不动。

    入口相位集合只多了一个名字（``post_processing``），没有被放宽成
    「任何相位都能 requeue」。
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-completed-p1"
    plan_dir = _write_plan(plans_dir, plan_id, "completed")
    assert _persisted_phase(plan_dir) == "completed"

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is False
    assert result.detail == "conflict"
    assert result.final_phase == "completed"
    assert result.berth_phase == "completed"
    assert _persisted_phase(plan_dir) == "completed"


def test_requeue_verification_running_still_conflicts(plans_dir, clean_state):
    """负控：``verification_running`` → 冲突，相位一个字节都不动。

    验证正在跑的计划没有「放回等待相位」这回事。
    """
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "requeue-verification-running-p1"
    plan_dir = _write_plan(plans_dir, plan_id, "verification_running")
    assert _persisted_phase(plan_dir) == "verification_running"

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is False
    assert result.detail == "conflict"
    assert result.final_phase == "verification_running"
    assert result.berth_phase == "verification_running"
    assert _persisted_phase(plan_dir) == "verification_running"
