"""把一条失败的任务重新排队：``POST /api/execution/{plan_id}/tasks/{task_id}/reset``。

背景
----
一条失败的任务原本只有两种下场：被修好，或者作为一条永久的红条目
留在进度卡上。操作者**没有**受支持的办法说「这条再跑一遍」—— 唯一
的门是手写 ``UPDATE state.db``：没有审计、没有并发保护。

这个端点就是那扇门，但它是按规矩开的门：

* 只写**执行记账**（``status`` / ``failure_reason`` / ``end_ts`` /
  ``commit_sha`` / ``attempt``）—— 见 ``backend/task_reset.py``；
* **规格**（标题、描述、要改哪些文件、测试命令、依赖、project_dir）
  一个字节都不动；
* **不放行计划**：计划的相位归计划自己管，响应里的 ``note`` 指向
  ``POST /api/plan/{plan_id}/state``；
* **不做批量**：``/tasks/reset`` 这种形状的路由不存在。

钉死的行为
----------
下面每一条测试钉一个条款。合在一起是「这条接口的全部契约」。

边界
----
* 任务行不存在 → 404（不能靠 ``update_task`` 的 INSERT 分支凭空造行）；
* ``obsolete`` 的语义不受影响 —— 仍然把 ``status`` 写成 ``voided``。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from server import app
from tests.fixtures import task_reset_env as env

client = TestClient(app)


@pytest.fixture
def reset_env(tmp_path):
    """把计划目录与状态库指到 per-test 的位置。

    套件级的 ``isolated_plans_dir`` 已经把 ``server.PLANS_DIR`` 与
    ``server._state_db_path`` 指到 ``tmp_path``；这里只是把它们取出来，
    让夹具和断言用同一份路径。
    """
    import server

    plans_dir = Path(server.PLANS_DIR)
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(server._state_db_path())
    return plans_dir, db_path


def _routing_phase(db_path: Path, plan_id: str) -> Optional[str]:
    """读回 ``plan_routing`` 行的 ``current_phase``（裸 SQL，不经仓储）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _disk_task_ids(plans_dir: Path, plan_id: str):
    """``tasks.json`` 里的任务 id 列表。"""
    raw = json.loads(
        (plans_dir / plan_id / "tasks.json").read_text(encoding="utf-8")
    )
    tasks = raw if isinstance(raw, list) else raw.get("tasks", [])
    return [str(task.get("id")) for task in tasks if isinstance(task, dict)]


# ---------------------------------------------------------------------------
# 1) failed → pending 主路径
# ---------------------------------------------------------------------------


def test_reset_failed_task_becomes_pending(reset_env):
    """一条 ``failed`` 的任务重置之后变回 ``pending``，重试预算归零。

    这是整个接口存在的原因：把「再跑一遍」变成一次受支持的调用，
    而不是一次手写 SQL。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    env.assert_task_reset_took_effect(db_path, env.DEFAULT_PLAN_ID, "1")


def test_response_carries_the_row_after_the_write(reset_env):
    """响应里的 ``task`` 是**写完之后**那一行，不是请求之前的快照。

    操作者拿到的必须是可以直接照着写回库的现状：状态、尝试次数、
    失败痕迹，全部是重置后的值。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    task = payload["task"]
    assert task["id"] == "1", payload
    assert task["status"] == "pending", payload
    assert task["attempt"] == 0, payload
    assert task["failure_reason"] is None, payload
    assert task["end_ts"] is None, payload
    assert task["commit_sha"] is None, payload

    # 响应里的这一行必须和库里的这一行一致（响应不是另一份真相）。
    row = env.read_row(db_path, env.DEFAULT_PLAN_ID, "1")
    for key in ("status", "attempt", "failure_reason", "end_ts", "commit_sha"):
        assert task[key] == row[key], (
            f"响应里的 {key} 与库里的不一致：{task[key]!r} vs {row[key]!r}"
        )


def test_cleared_reports_the_previous_values(reset_env):
    """``cleared`` 回显的是**重置之前**被清掉的那些值。

    操作者需要知道「到底清掉了什么」—— 否则一次重置就是一次
    不明不白的擦除。回显的是上一轮的失败原因、结束时间、commit 和
    尝试次数，外加重置前的状态。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] == {
        "status": "failed",
        "failure_reason": env.FAILED_REASON,
        "end_ts": env.FAILED_END_TS,
        "commit_sha": "a" * 40,
        "attempt": 3,
    }, resp.json()["cleared"]


# ---------------------------------------------------------------------------
# 2) 重置不许碰的东西
# ---------------------------------------------------------------------------


def test_spec_fields_are_byte_identical(reset_env):
    """规格字段重置前后逐字段相等（**原始列**比，字节级）。

    任务「要做什么」写定之后就不许改：审计链靠它对得上号。重置改的
    只是「上一次跑成了什么」，那是执行记账。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)
    before = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    after = env.read_raw_row(db_path, env.DEFAULT_PLAN_ID, "1")
    for field in env.SPEC_FIELDS:
        assert before[field] == after[field], (
            f"规格字段 {field} 被重置改动了：{before[field]!r} → "
            f"{after[field]!r}"
        )


def test_plan_phase_is_untouched(reset_env):
    """重置任务不动计划的相位。

    重置一条任务不等于放行计划：计划还失败着，就还失败着。要动相位
    的人得自己走 ``POST /api/plan/{plan_id}/state``。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)
    before = _routing_phase(db_path, env.DEFAULT_PLAN_ID)
    assert before == "executing", before

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    assert _routing_phase(db_path, env.DEFAULT_PLAN_ID) == before, (
        "重置任务不该动计划相位"
    )


def test_note_points_at_the_plan_state_endpoint(reset_env):
    """响应里的 ``note`` 指向放行计划该走的那个端点。

    少了这一句，操作者会以为重置任务等于重置计划。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    note = resp.json()["note"]
    assert "queued" in note, note
    assert "/api/plan/" in note, note


def test_reset_never_marks_the_task_completed(reset_env):
    """重置后的状态是 ``pending``，绝不是 ``completed``。

    「重置即成功」是最危险的读法：它会让进度卡把一条没跑过的任务
    算成已完成。响应里也不许出现 ``completed`` 这个词。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["task"]["status"] == "pending", payload
    assert payload["task"]["status"] != "completed", payload
    assert "completed" not in resp.text, resp.text

    row = env.read_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert row["status"] != "completed", row


# ---------------------------------------------------------------------------
# 3) 不新增的那几条路
# ---------------------------------------------------------------------------


def test_no_batch_reset_route(reset_env):
    """``POST /api/execution/{plan_id}/tasks/reset`` 上没有路由。

    批量重置会需要一套「哪些任务该重置」的判据（按轮次？按失败
    原因？），而这条接口的契约是「重置这一条」。批量是另一件事，
    这里不做。

    这条路径上**没有注册任何路由**，所以它答的不是 200。仓库里有一
    条兜底的 ``GET /{path:path}``（前端静态回退）：它对**未知路径**回
    404，对**路径匹配得上、方法匹配不上**回 405。POST 打过去落进后者
    ——「方法不对」；用 GET 探同一条路径才是「路径不存在」的那个 404
    （这正是 ``tests/api/test_request_guard.py`` 探路由存在性的写法）。
    两种答案都表示同一件事：批量重置这条路由不存在。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/reset", json={}
    )

    assert resp.status_code in (404, 405), resp.text

    # 「这条路由不存在」的正面证据：同一条路径用 GET 探是 404。
    probe = client.get(f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/reset")
    assert probe.status_code == 404, probe.text

    # 而且它什么也没重置：那条失败的任务还失败着。
    row = env.read_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert row["status"] == "failed", row
    assert row["attempt"] == 3, row


def test_task_delta_obsolete_still_voids(reset_env):
    """``obsolete`` 仍然把 ``status`` 写成 ``voided``，任务留在 ``tasks.json``。

    新端点不许改动既有端点的语义：作废是「前提被推翻了」，与
    「再跑一遍」是两件事，状态词也不能混。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/task_delta",
        json={
            "obsolete": [{"id": "1", "reason": "coverage probe"}],
            "round": 0,
            "actor": "test",
        },
    )

    assert resp.status_code == 200, resp.text
    row = env.read_row(db_path, env.DEFAULT_PLAN_ID, "1")
    assert row is not None, "作废是标记，不是删除"
    assert row["status"] == "voided", row
    assert "1" in _disk_task_ids(plans_dir, env.DEFAULT_PLAN_ID), (
        "作废过的任务仍要留在 tasks.json 里留痕"
    )
