"""已完成任务的 409 拒绝与 ``force`` 逃生口（2026-11-13）。

这个文件钉的是「重跑一件**已经完成**的工作必须由调用方显式要求」：

* 一条 ``completed`` 的任务被重置 → **409** ``task_already_completed``，
  并且**一个列都不许改**（``read_raw_row`` 前后逐列相等）；
* 请求体里带 ``force=true`` → 放行：状态回到 ``pending``、上一轮的
  ``commit_sha`` 被清掉、``cleared`` 回显重置前是 ``completed``；
* ``force`` 缺省是 ``false`` —— 空 body 与 ``{"force": false}`` 行为
  一致，都是 409；
* ``force`` **只**绕过这一条拒绝：不绕过 404，也不绕过 pending 的
  幂等 no-op。

为什么 completed 要单独拦
----------------------
「重跑一条失败的任务」是**恢复**：它本来就没跑成，重跑不损失任何东西。
「重跑一条已完成的任务」是**改写历史**：那条任务此前挣到的结论、commit
checkpoint、验收记录都在，重跑会把它们全部覆盖掉，审计链上从此有一段
空白。所以失败可以随手重置，完成必须由人显式点头 —— ``force=true``
就是那个点头。

拒绝时一个列都不许改
------------------
409 不是「先写了再回滚」，是**根本没写**。落点用 ``read_raw_row``
的整行逐列相等来钉：``_repo_version`` 与 ``updated_at`` 都不许推进 ——
推进了就说明真的写过。

边界
----
* 409 的响应体是 ``{"detail": {"error", "detail", "plan_id", "task_id"}}``
  —— 结构化错误体整体落在 ``detail`` 里（``detail["detail"]`` 是那句
  告诉调用方「带 force=true」的说明）。
* 409 不写审计：这次什么都没发生。
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


def _seed_completed(reset_env):
    """种一条 ``completed`` 的任务，返回 ``(plans_dir, db_path)``。"""
    return env.seed_plan(*reset_env, status="completed")


# ---------------------------------------------------------------------------
# 1) 默认拒绝
# ---------------------------------------------------------------------------


def test_completed_task_refused_with_409(reset_env):
    """``completed`` 的任务被重置 → 409，且状态原样不动。

    默认必须拒绝：重跑已完成的工作是破坏性的。
    """
    plans_dir, db_path = _seed_completed(reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    row = env.read_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert row is not None, row
    assert row["status"] == "completed", row


def test_refusal_body_names_the_force_escape_hatch(reset_env):
    """409 的响应体要说清楚「想重跑就带 force=true」。

    只回一个 409 等于把话憋回去：调用方不知道怎么才能重跑。错误体里
    那句说明必须点名 ``force`` 这个逃生口。
    """
    _seed_completed(reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert "force" in payload["detail"]["detail"], payload
    assert payload["detail"]["error"] == "task_already_completed", payload
    assert payload["detail"]["plan_id"] == env.DEFAULT_PLAN_ID, payload
    assert payload["detail"]["task_id"] == "1", payload


def test_refusal_status_is_409_not_400_or_403(reset_env):
    """拒绝的状态码必须是 409 —— 不是 400（请求写错了），也不是 403
    （权限不够）。

    请求本身没有错：body 合法、任务存在。是**状态**冲突，所以是 409。
    """
    _seed_completed(reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code not in (400, 403), resp.text
    assert resp.status_code == 409, resp.text


def test_force_defaults_to_false(reset_env):
    """``force`` 缺省是 ``false``：空 body 与 ``{"force": false}`` 一样。

    「显式写 false」和「压根不写」必须同义 —— 否则调用方会以为不写
    就是放行。
    """
    _seed_completed(reset_env)

    empty = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")
    explicit = env.post_reset(client, env.DEFAULT_PLAN_ID, "1", force=False)
    no_body = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/1/reset"
    )

    assert empty.status_code == 409, empty.text
    assert explicit.status_code == 409, explicit.text
    assert no_body.status_code == 409, no_body.text


def test_refused_reset_changes_no_column(reset_env):
    """409 之后数据库一行都不许改（``read_raw_row`` 前后逐列相等）。

    落点是**整行**：``_repo_version`` 与 ``updated_at`` 都不许推进 ——
    推进了就说明「拒绝」其实是「写了再回滚」。
    """
    plans_dir, db_path = _seed_completed(reset_env)
    before = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    after = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert after == before, (
        "被拒绝的重置必须是零写入：整行逐列相等"
        f"\n之前：{before}"
        f"\n之后：{after}"
    )


def test_refusal_writes_no_audit_entry(reset_env):
    """409 不写审计：这次什么都没发生。

    审计记录的是**做过的**重置。写一条「被拒绝了」进重置日志，会让
    审计链看起来像发生过一次重置。
    """
    plans_dir, db_path = _seed_completed(reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    assert not _audit_log_path(plans_dir, env.DEFAULT_PLAN_ID).exists(), (
        "被拒绝的重置不许写审计：这次什么都没发生"
    )


# ---------------------------------------------------------------------------
# 2) force=true 放行
# ---------------------------------------------------------------------------


def test_completed_task_reset_with_force_succeeds(reset_env):
    """``force=true`` → 放行：状态回到 ``pending``，上一轮痕迹被清掉。

    ``cleared`` 回显的是重置**之前**的值，所以它的 ``status`` 是
    ``completed`` —— 操作者据此知道刚才擦掉的是什么。
    """
    plans_dir, db_path = _seed_completed(reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", force=True, reason="重新验证"
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["noop"] is False, payload
    assert payload["task"]["status"] == "pending", payload
    assert payload["cleared"]["status"] == "completed", payload

    row = env.read_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert row["status"] == "pending", row
    assert row["commit_sha"] is None, row
    assert row["attempt"] == 0, row


def test_force_does_not_bypass_missing_task(reset_env):
    """``force=true`` 不绕过 404：计划里没有这条任务，就是没有。

    ``force`` 只买「允许重跑已完成的工作」这一件事，不买「凭空造行」。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "99", force=True)

    assert resp.status_code == 404, resp.text
    assert env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "99") is None, (
        "force 不许绕过 404：库里不许多出这一行"
    )


def test_force_does_not_bypass_pending_noop(reset_env):
    """``force=true`` 不绕过 pending 的幂等 no-op。

    已经排队的任务重置它没有意义（本来就是 pending），所以仍然答
    200 + ``noop``，并且一个列都不写。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="pending")
    before = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1", force=True)

    assert resp.status_code == 200, resp.text
    assert resp.json()["noop"] is True, resp.json()
    after = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert after == before, (
        "force 不许绕过 no-op：整行逐列相等"
        f"\n之前：{before}"
        f"\n之后：{after}"
    )
