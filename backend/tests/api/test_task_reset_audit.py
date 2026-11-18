"""重置接口的审计：每次**真的写入**都追加一条记录（2026-11-13）。

为什么要有这一层
----------------
没有审计的重置就是又一次匿名写库：谁在什么时候把哪条任务放回队列，
事后没人说得清。这份审计落在 ``plans/<plan_id>/task_reset_log.json``，
**只增不改**（与 ``task_delta_log.json`` 并列），每次成功重置追加一条::

    {at, task_id, from_status, to_status, reason, caller, forced}

谁在写
------
``caller`` 取自请求头 ``X-PDT-Caller``：拿不到就写 ``"http"``。

**不要**拿 ``X-PDT-Request`` 当身份 —— 那是 CSRF 守卫头，恒为
``"1"``（见 ``backend/request_guard.py``），拿它当 caller 等于把
「所有调用者」都记成同一个人。

什么时候不写
------------
只在**真的写入**之后写：已经是 ``pending`` 的 no-op 与所有 4xx 拒绝
都没有发生重置，记一条审计等于在链上留下一条假记录。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import task_reset
from server import app
from tests.fixtures import task_reset_env as env

client = TestClient(app)

#: 审计条目必须带的七个键。
REQUIRED_FIELDS = (
    "at",
    "task_id",
    "from_status",
    "to_status",
    "reason",
    "caller",
    "forced",
)

#: ``at`` 的形状 —— ``2026-11-13T01:00:00Z``。
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


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


def _audit_path(plans_dir: Path, plan_id: str) -> Path:
    """这条计划的重置审计日志路径。

    文件名由 ``task_reset.RESET_LOG_FILENAME`` 钉死 —— 与
    ``task_delta_log.json`` 并列放在计划目录下。
    """
    return Path(plans_dir) / plan_id / task_reset.RESET_LOG_FILENAME


def _entries(plans_dir: Path, plan_id: str) -> List[Dict[str, Any]]:
    """读回审计条目（走 ``task_reset.read_reset_log``）。"""
    return task_reset.read_reset_log(Path(plans_dir) / plan_id)


def _fail_task_again(db_path: Path, plan_id: str, task_id: str) -> None:
    """把任务重新置为 ``failed`` —— 模拟「重置之后又跑失败了一次」。

    第二次重置必须是**一次真的写入**：任务已经是 ``pending`` 时那次
    调用落在 no-op 分支上，按契约不写审计（见
    ``test_noop_writes_no_audit_entry``）。
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        PlanTaskRepository(conn).update_task(
            plan_id=plan_id,
            task_id=task_id,
            fields={
                "status": "failed",
                "failure_reason": "boom again",
                "end_ts": "2026-11-13T01:00:00Z",
                "attempt": 1,
            },
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1) 一次成功重置 = 一条审计
# ---------------------------------------------------------------------------


def test_successful_reset_appends_exactly_one_entry(reset_env):
    """一次成功重置**恰好**追加一条审计。

    「恰好一条」不是「至少一条」：一次重置记两条，链上就有了两条说
    同一件事的记录，谁也说不清哪条是真的。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 1, entries
    assert entries[0]["task_id"] == "1", entries[0]


def test_entry_carries_every_required_field(reset_env):
    """每条审计都带齐七个键，且值说得清「谁把什么从什么状态改成了什么」。

    缺一个键，这条审计就答不出一道问题：缺 ``from_status`` 就说不清
    改的是什么，缺 ``caller`` 就说不清是谁改的。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="manual retry"
    )

    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 1, entries
    entry = entries[0]

    missing = [field for field in REQUIRED_FIELDS if field not in entry]
    assert not missing, f"审计条目缺字段：{missing}（条目：{entry}）"

    assert entry["task_id"] == "1", entry
    assert entry["from_status"] == "failed", entry
    assert entry["to_status"] == "pending", entry
    assert entry["reason"] == "manual retry", entry
    assert entry["forced"] is False, entry
    assert isinstance(entry["at"], str) and _AT_RE.match(entry["at"]), entry


def test_missing_reason_is_recorded_as_null(reset_env):
    """没给 ``reason`` → 审计里是 ``null``，不是空串、也不是编一个。

    「没给理由」和「理由是空串」是两回事：前者是操作者没说，后者是
    操作者说了一句空的。记成 ``null`` 才不撒谎。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 1, entries
    assert "reason" in entries[0], entries[0]
    assert entries[0]["reason"] is None, entries[0]


# ---------------------------------------------------------------------------
# 2) 只增不改
# ---------------------------------------------------------------------------


def test_second_reset_appends_second_entry_and_keeps_first(reset_env):
    """第二次重置追加第二条，**第一条逐字节不变**。

    这份日志是「只增不改」的：改写旧条目等于改写历史 —— 事后看链的
    人必须能确定第一条当时写的就是现在这个样子。

    第二次重置之前任务又失败了一次（重置 → 跑 → 又失败 → 再重置），
    所以那是一次**真的写入**；任务还挂着 ``pending`` 时的那次调用
    落在 no-op 分支上，按契约不写审计。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    first = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="first retry"
    )
    assert first.status_code == 200, first.text
    assert len(_entries(plans_dir, env.DEFAULT_PLAN_ID)) == 1
    first_bytes = _audit_path(plans_dir, env.DEFAULT_PLAN_ID).read_bytes()
    first_entry = _entries(plans_dir, env.DEFAULT_PLAN_ID)[0]

    _fail_task_again(db_path, env.DEFAULT_PLAN_ID, "1")

    second = env.post_reset(
        client, env.DEFAULT_PLAN_ID, "1", reason="second retry"
    )
    assert second.status_code == 200, second.text

    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 2, entries
    assert entries[0] == first_entry, (
        "第一条审计不许被改写"
        f"\n之前：{first_entry}"
        f"\n之后：{entries[0]}"
    )
    assert entries[1]["reason"] == "second retry", entries[1]

    # 逐字节：第一条那段文本原封不动地留在文件里（文件是整表重写的，
    # 所以第一条的字节必须在新文件里原样出现，后面才接第二条）。
    second_bytes = _audit_path(plans_dir, env.DEFAULT_PLAN_ID).read_bytes()
    # 单条目文件形如 ``[ {...}\n]``，两条目文件形如 ``[ {...},\n {...}\n]``：
    # 第一条的字节块到 ``}`` 为止，后面紧跟一个逗号。
    first_block = first_bytes[:-1].rstrip(b"\r\n")
    assert second_bytes.startswith(first_block), (
        "第一条审计的字节被改写了"
        f"\n之前：{first_bytes!r}"
        f"\n之后：{second_bytes!r}"
    )
    assert second_bytes[len(first_block):len(first_block) + 1] == b",", (
        "第一条与第二条之间应当只隔一个逗号"
    )


# ---------------------------------------------------------------------------
# 3) caller 是谁
# ---------------------------------------------------------------------------


def test_caller_defaults_to_http(reset_env):
    """没带 ``X-PDT-Caller`` → ``caller`` 是 ``"http"``。

    至少得记下「这是一次 HTTP 调用」—— 总比空着强：空着的字段在事后
    看链时既不能区分来源，也不能证明来源被记过。

    注意这个请求**带着** ``X-PDT-Request: 1``（守卫头，测试环境会注入，
    下面还显式带了一次）。它是 CSRF 守卫，不是身份：拿它当 caller 等
    于把「所有调用者」都记成同一个人。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/1/reset",
        json={"reason": "manual retry"},
        headers={"X-PDT-Request": "1"},
    )

    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 1, entries
    assert entries[0]["caller"] == "http", entries[0]


def test_caller_comes_from_x_pdt_caller_header(reset_env):
    """带了 ``X-PDT-Caller`` → ``caller`` 就是那个值。

    这是审计存在的理由：操作者（或脚本）在请求头上自报身份，重置链
    上就留下了「谁重置的」。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/1/reset",
        json={"reason": "manual retry"},
        headers={"X-PDT-Caller": "ops-cli"},
    )

    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 1, entries
    assert entries[0]["caller"] == "ops-cli", entries[0]


def test_caller_is_trimmed_and_capped(reset_env):
    """``caller`` 去掉首尾空白，最长 200 字符；空白-only 记成 ``"http"``。

    空白-only 的头不是身份，是没写身份 —— 记成 ``""`` 会让「谁」这一栏
    空着，记成 ``"http"`` 才和缺头的那条一样。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/1/reset",
        json={},
        headers={"X-PDT-Caller": "   "},
    )
    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 1, entries
    assert entries[0]["caller"] == "http", entries[0]

    # 任务又失败了一次 —— 第二次重置才是一次真的写入。
    _fail_task_again(db_path, env.DEFAULT_PLAN_ID, "1")

    long_caller = "x" * 500
    resp = client.post(
        f"/api/execution/{env.DEFAULT_PLAN_ID}/tasks/1/reset",
        json={},
        headers={"X-PDT-Caller": long_caller},
    )
    assert resp.status_code == 200, resp.text
    entries = _entries(plans_dir, env.DEFAULT_PLAN_ID)
    assert len(entries) == 2, entries
    assert entries[1]["caller"] == long_caller[:200], (
        f"caller 应当截到 200 字符：{entries[1]['caller']!r}"
    )


# ---------------------------------------------------------------------------
# 4) 不该写的时候就不写
# ---------------------------------------------------------------------------


def test_noop_writes_no_audit_entry(reset_env):
    """已经是 ``pending`` 的 no-op → 一条审计都不写。

    no-op 什么都没发生（一个列都没写）。给它记一条，等于在链上留一条
    「什么都没干的重置」—— 那正是这条接口当初拒绝重复写库的理由。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="pending")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    assert resp.json()["noop"] is True, resp.json()
    assert _entries(plans_dir, env.DEFAULT_PLAN_ID) == [], (
        "no-op 不许写审计：这次什么都没发生"
    )
    assert not _audit_path(plans_dir, env.DEFAULT_PLAN_ID).exists(), (
        "no-op 不许留下审计文件"
    )


def test_refusal_writes_no_audit_entry(reset_env):
    """4xx 拒绝 → 一条审计都不写。

    被拒绝的重置没有发生，记一条审计等于把一次「没做」记成「做了」。
    两条拒绝路径都验：409（已完成、不肯 force）与 404（任务不存在）。
    """
    plans_dir, db_path = env.seed_plan(*reset_env, status="completed")

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 409, resp.text
    assert _entries(plans_dir, env.DEFAULT_PLAN_ID) == [], (
        "409 拒绝不许写审计：这次什么都没发生"
    )
    assert not _audit_path(plans_dir, env.DEFAULT_PLAN_ID).exists(), (
        "409 拒绝不许留下审计文件"
    )

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "99")

    assert resp.status_code == 404, resp.text
    assert _entries(plans_dir, env.DEFAULT_PLAN_ID) == [], (
        "404 拒绝不许写审计：这次什么都没发生"
    )
    assert not _audit_path(plans_dir, env.DEFAULT_PLAN_ID).exists(), (
        "404 拒绝不许留下审计文件"
    )


# ---------------------------------------------------------------------------
# 5) 文件落在该落的地方
# ---------------------------------------------------------------------------


def test_audit_file_lives_next_to_the_other_plan_artifacts(reset_env):
    """审计文件落在 ``plans/<plan_id>/`` 下，文件名 ``task_reset_log.json``。

    与 ``task_delta_log.json`` 并列 —— 都是计划目录里的只增日志。
    """
    plans_dir, db_path = env.seed_plan(*reset_env)

    resp = env.post_reset(client, env.DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    path = _audit_path(plans_dir, env.DEFAULT_PLAN_ID)
    assert path.exists(), path
    assert path.name == "task_reset_log.json", path.name
    assert path.parent == Path(plans_dir) / env.DEFAULT_PLAN_ID, path.parent

    # 文件是 JSON 数组，条目就是读回来的那一条。
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, list), raw
    assert len(raw) == 1, raw
