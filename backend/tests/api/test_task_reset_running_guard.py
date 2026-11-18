"""活进程持有时的 409 拒绝：scheduler running 守卫（2026-11-13）。

这个文件钉的是「重置**不能**跟活着的执行器抢一条任务」：

* 任务当前 ``in_progress``，而 scheduler 认为这个 plan **正在跑**
  → **409** ``task_in_progress``，并且**一个列都不许改**；
* ``force=true`` **不能**绕过这条拒绝 —— 它买的是「允许重跑已完成的
  工作」，不是「允许覆盖活进程的写入」；
* 任务 ``in_progress`` 但 plan **不在跑**（执行器已经死了）→ **200**，
  正常重置：这是**恢复**场景，不是冲突；
* 任务 ``breakdown_in_progress``（同样算「正在跑」）→ 同样 409。

为什么这条拒绝必须存在
----------------------
一个活着的执行器会在下一次写盘时把这条任务的状态**覆盖掉**：它手里
握着自己那份内存状态，落盘时按自己的来。于是「重置」这个动作会被静默
吞掉 —— 操作者以为任务回到队列了，下一秒它又变回 ``in_progress``。
这与 verification reset 拒绝 live thread 是同一条理由：活的持有者
优先，重置必须让路。

判定源只有一个
--------------
「plan 到底在不在跑」只由 ``PlanFactsReader.active_plans()`` 回答 ——
它是**从数据库派生**的运行集合（执行侧看 ``exec_status`` + pid 是否
还活着，验证侧看 verification 行）。**不许**去读服务进程内存里的
``_execution_state``：那份内存态在进程重启后就没了，而数据库里的执行
行还在 —— 用内存态判定的守卫会在重启后突然放行。

``force`` 不豁免这条拒绝
-----------------------
``force=true`` 的语义是「我知道重跑已完成的工作是破坏性的，我仍然要
做」。而这条 409 的语义完全不同：不是「破坏性」，是「**争不到**」——
活着的执行器会把你的写入覆盖掉，写了等于没写。这种时候正确的做法是
**先停计划**（``POST /api/execution/{plan_id}/stop``）再重置，而不是
硬闯。

边界
----
* 409 的响应体是 ``{"detail": {"error", "detail", "plan_id", "task_id"}}``
  —— 与「已完成任务 409」那条拒绝同一个形状。
* 409 不写审计：这次什么都没发生。
* 拒绝时不许写任何一列：``read_raw_row`` 前后整行逐列相等
  （``_repo_version`` / ``updated_at`` 都不许推进）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import task_reset
from server import app
from tests.fixtures import task_reset_env as env

client = TestClient(app)


@pytest.fixture
def reset_env(tmp_path):
    """计划目录与状态库都指到 per-test 的位置。

    套件级的 ``isolated_plans_dir`` 已经把 ``server.PLANS_DIR`` 与
    ``server._state_db_path`` 指到 ``tmp_path``；这里只是把它们取出来，
    让夹具和断言用同一份路径。
    """
    import server

    plans_dir = Path(server.PLANS_DIR)
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(server._state_db_path())
    return plans_dir, db_path


def _audit_log_path(plans_dir: Path, plan_id: str) -> Path:
    """这条计划的重置审计日志路径。"""
    return Path(plans_dir) / plan_id / task_reset.RESET_LOG_FILENAME


def _seed_running(reset_env, *, status: str = "in_progress"):
    """种一条「正在跑的任务」+ 一个「活着的执行器」。

    执行器的 pid 用本进程的 pid —— 一个**确实活着**的 pid，才让
    「活进程持有时拒绝重置」这条守卫有东西可查（行里的
    ``exec_status="running"`` 只是半条腿：pid 没了就是没在跑）。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status=status)
    env.seed_running_execution(db_path, env.DEFAULT_PLAN_ID)
    return plans_dir, db_path


# ---------------------------------------------------------------------------
# 1) 活进程持有 → 409
# ---------------------------------------------------------------------------


def test_in_progress_task_on_running_plan_refused_409(reset_env):
    """任务 ``in_progress`` 且 plan 正在跑 → 409 ``task_in_progress``。

    这是本任务的核心断言：守卫**只看**数据库派生的运行集合就能拒绝。
    测试里服务进程的内存态（``_execution_state``）根本没有这个计划 ——
    拒绝照样发生，说明判定源不是内存态。
    """
    _seed_running(reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert payload["detail"]["error"] == "task_in_progress", payload
    assert payload["detail"]["plan_id"] == env.DEFAULT_PLAN_ID, payload
    assert payload["detail"]["task_id"] == "1", payload


def test_refusal_body_says_how_to_get_unblocked(reset_env):
    """409 的错误体要说清楚「先停计划，再重置」。

    只回一个 409 等于把话憋回去：调用方以为自己按错了，需要被告诉
    下一步该做什么 —— 停掉这个 plan，然后再重置。
    """
    _seed_running(reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]["detail"]
    assert "stop" in detail, detail
    assert "/api/execution/" in detail, detail


def test_breakdown_in_progress_task_on_running_plan_refused(reset_env):
    """``breakdown_in_progress`` 的任务同样被拒 —— 同一个 frozenset。

    「正在跑」有两种任务状态：``in_progress``（执行中）与
    ``breakdown_in_progress``（拆解中）。拆解中的任务同样被活着的执行
    器握着，同样会被覆盖，所以必须走同一条拒绝。
    """
    _seed_running(reset_env, status="breakdown_in_progress")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "task_in_progress", resp.json()


def test_force_does_not_bypass_the_running_refusal(reset_env):
    """``force=true`` **不能**绕过这条拒绝。

    ``force`` 买的是「允许重跑已完成的工作」；这条拒绝说的是「你争不
    到」——活着的执行器会覆盖你的写入。硬闯的结果是操作者以为重置成功
    了，下一秒状态又变回 ``in_progress``。
    """
    plans_dir, db_path = _seed_running(reset_env)
    before = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1", force=True)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "task_in_progress", resp.json()
    after = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert after == before, (
        "被拒绝的重置必须是零写入：整行逐列相等"
        f"\n之前：{before}"
        f"\n之后：{after}"
    )


def test_refused_reset_changes_no_column(reset_env):
    """409 之后数据库一行都不许改（``read_raw_row`` 前后逐列相等）。

    落点是**整行**：``_repo_version`` 与 ``updated_at`` 都不许推进 ——
    推进了就说明「拒绝」其实是「写了再回滚」。而且这次重置**不写
    审计**：审计记录的是做过的重置。
    """
    plans_dir, db_path = _seed_running(reset_env)
    before = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    after = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert after == before, (
        "被拒绝的重置必须是零写入：整行逐列相等"
        f"\n之前：{before}"
        f"\n之后：{after}"
    )
    assert not _audit_log_path(plans_dir, env.DEFAULT_PLAN_ID).exists(), (
        "被拒绝的重置不许写审计：这次什么都没发生"
    )


# ---------------------------------------------------------------------------
# 2) plan 空闲 → 放行
# ---------------------------------------------------------------------------


def test_in_progress_task_on_idle_plan_is_reset(reset_env):
    """任务 ``in_progress`` 但 plan 空闲 → **200**，正常重置。

    这是**恢复**场景，不是冲突：执行器已经死了（``exec_status``
    不是 ``running``），那条 ``in_progress`` 只是上一次崩掉时留下的
    残值。此时拒绝重置就等于把这条任务永久锁死 —— 谁也跑不了它。

    所以守卫只看「现在有没有活着的持有者」，**不**看「任务上一次是
    什么状态」。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="in_progress")
    env.seed_execution(
        db_path, env.DEFAULT_PLAN_ID, exec_status="stopped", exec_pid=999999
    )

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["noop"] is False, payload
    assert payload["task"]["status"] == "pending", payload
    env.assert_task_reset_took_effect(db_path, env.DEFAULT_PLAN_ID, "1")


def test_in_progress_task_with_dead_executor_is_reset(reset_env):
    """执行器**还标着 running** 但进程已经死了 → 同样放行。

    ``exec_status="running"`` 只是半条腿：pid 没了就是没在跑。这正是
    「从数据库派生的运行集合」比「看状态字段」强的地方 —— 状态字段会
    在执行器崩掉时留下残值，而活着的 pid 不会说谎。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="in_progress")
    env.seed_execution(
        db_path, env.DEFAULT_PLAN_ID, exec_status="running", exec_pid=999999
    )

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "pending", resp.json()
