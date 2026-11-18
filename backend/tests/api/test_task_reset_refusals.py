"""重置接口的拒绝路径与幂等（2026-11-13）。

这个文件钉的是「重置**不做**什么」：

* 计划目录不存在 → 404 ``Plan not found``；
* 计划里没有这一条任务 → 404 ``Task not found``，**且不许凭空造一行**；
* 这条任务属于**另一个**计划 → 404（按 ``(plan_id, task_id)`` 查，
  天然隔离）；
* ``task_id`` 的形状非法（例如带空格）→ 404，不许冒 500；
* 任务已经是 ``pending`` → 200，且是**真 no-op**：一个列都不写，
  ``_repo_version`` / ``updated_at`` 都不许推进。

为什么 no-op 要比「整行逐列相等」而不是比状态
--------------------------------------------
「已经是 pending 还去重置」是最容易被写成「顺手再写一遍」的地方。
再写一遍的结果是 ``_repo_version`` 往前走、``updated_at`` 变新 ——
审计链上就多出一条「什么都没干」的重置记录。而 ``updated_at`` 只有
秒级精度，同一秒里的两次写可能撞出同一个时间戳；``_repo_version``
不会。所以这条契约的落点是**整行逐列相等**，不是「状态还是 pending」。

「pending」那一行的记账字段也不是干净的（``attempt`` / ``failure_reason``
都还在），这正是要用：no-op 若偷偷把它们清了，整行就不相等了。

边界
----
* 404 的错误体是 ``{"error": ...}``（不是 FastAPI 默认的 ``detail``
  包裹）—— ``server.py`` 的 ``HTTPException`` 处理器遇到 dict 里的
  ``error`` 键时原样返回。
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
    """这条计划的重置审计日志路径（审计本身是后面一步接的）。"""
    return Path(plans_dir) / plan_id / task_reset.RESET_LOG_FILENAME


# ---------------------------------------------------------------------------
# 1) 404 三态
# ---------------------------------------------------------------------------


def test_plan_not_found_returns_404(reset_env):
    """计划目录不存在 → 404 ``Plan not found``。

    「没有这个计划」和「这个计划里没有这条任务」是两件事，答案必须
    能分开 —— 否则操作者分不清自己打错了计划号还是打错了任务号。
    """
    resp = env.post_reset(client, "no-such-plan", "1")

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"error": "Plan not found"}, resp.json()


def test_task_not_in_plan_returns_404(reset_env):
    """计划里有别的任务，但**没有**这一条 → 404，且库里不许多出一行。

    「404 而不是 200」只是表面。真正的坑在仓储那一侧：
    ``update_task`` 的 SQL 是 ``INSERT ... ON CONFLICT DO UPDATE`` ——
    查不到就 INSERT，凭空造行。重置必须是「把已存在的行改回去」，
    不是「加一条任务」。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "99")

    assert resp.status_code == 404, resp.text
    assert resp.json() == {
        "error": "Task not found",
        "plan_id": env.DEFAULT_PLAN_ID,
        "task_id": "99",
    }, resp.json()
    assert env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "99") is None, (
        "404 之后库里不许凭空多出这一行"
    )


def test_task_of_another_plan_returns_404(reset_env):
    """这条任务属于**另一个**计划 → 404。

    计划 A 有 task ``1``，计划 B 有 task ``2``。拿 A 的计划号去要 B 的
    任务：``get_task`` 按 ``(plan_id, task_id)`` 查，天然隔离 —— 不许
    跨计划重置。
    """
    plans_dir, db_path = env.seed_plan(
        *reset_env, plan_id="plan-a", task_id="1"
    )
    plans_dir, db_path = env.seed_plan(
        plans_dir, db_path, plan_id="plan-b", task_id="2"
    )

    resp = env.post_reset(client, "plan-a", "2")

    assert resp.status_code == 404, resp.text
    assert resp.json() == {
        "error": "Task not found",
        "plan_id": "plan-a",
        "task_id": "2",
    }, resp.json()

    # 另一条计划里的那条任务不许被这次请求碰到。
    other = env.read_row(db_path, "plan-b", "2")
    assert other is not None, "另一个计划的任务行不该被这次请求动过"
    assert other["status"] == "failed", other
    assert other["attempt"] == 3, other


def test_invalid_task_id_format_returns_404(reset_env):
    """``task_id`` 的形状非法（例如带空格）→ 404，不许冒 500。

    仓储的 ``update_task`` 对非法 task_id 抛
    ``TaskProgressValidationError``。那是一条**校验**错误，不是服务器
    故障 —— 操作者打错了一个 id 而已，答案该是 404，不是 500。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "a b")

    assert resp.status_code == 404, resp.text
    assert resp.json() == {
        "error": "Task not found",
        "plan_id": env.DEFAULT_PLAN_ID,
        "task_id": "a b",
    }, resp.json()


# ---------------------------------------------------------------------------
# 2) pending 的幂等 no-op
# ---------------------------------------------------------------------------


def test_pending_task_reset_is_a_noop_200(reset_env):
    """已经是 ``pending`` 的任务 → 200，且是**真 no-op**。

    「真」的意思是：一个列都没写。连 ``_repo_version`` 和
    ``updated_at`` 都不许推进 —— 推进了就等于在审计链上留下一条
    「什么都没干」的重置。

    种出来的那条 pending 行带着上一轮的痕迹（``attempt=3``、
    ``failure_reason="boom"``）：这不是凑数，是让「整行逐列相等」这条
    断言有东西可比 —— no-op 若偷偷清了这些，整行就不相等了。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="pending")
    before = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["noop"] is True, payload
    assert payload["task"]["status"] == "pending", payload

    after = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert after == before, (
        "pending 的重置必须是零写入：整行逐列相等"
        f"\n之前：{before}"
        f"\n之后：{after}"
    )

    # no-op 不写审计（审计是后面一步接的，但这条契约从现在起就成立）。
    assert not _audit_log_path(plans_dir, env.DEFAULT_PLAN_ID).exists(), (
        "no-op 不许写审计：这次什么都没发生"
    )


def test_noop_reports_all_cleared_none(reset_env):
    """no-op 的 ``cleared`` 全是 ``None`` —— 这次什么都没清掉。

    非 no-op 的路径回显的是「重置**之前**被清掉的那些值」。no-op 路径
    没有清掉任何东西，所以回显必须是空的 —— 把 pending 那一行原样再
    报一遍会让操作者以为真的清过。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="pending")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] == {
        "status": None,
        "failure_reason": None,
        "end_ts": None,
        "commit_sha": None,
        "attempt": None,
    }, resp.json()["cleared"]


def test_second_reset_changes_no_column(reset_env):
    """连续调用两次，DB 行逐列相等。

    第一次把 ``failed`` 改回 ``pending``；第二次落在 no-op 分支上，
    所以第二次之后**整行**（含 ``updated_at`` 与 ``_repo_version``）
    必须与第一次之后逐列相等。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    first = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")
    assert first.status_code == 200, first.text
    after_first = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    second = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")
    assert second.status_code == 200, second.text
    after_second = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    assert after_second == after_first, (
        "第二次重置是 no-op：整行逐列相等"
        f"\n第一次之后：{after_first}"
        f"\n第二次之后：{after_second}"
    )
    assert after_second["_repo_version"] == after_first["_repo_version"], (
        "no-op 不许推进 _repo_version：它一动就说明真的写了"
    )
