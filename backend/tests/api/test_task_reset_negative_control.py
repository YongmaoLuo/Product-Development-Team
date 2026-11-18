"""重置接口的负控：证明这条断言**有牙齿**（2026-11-13）。

为什么要有这个文件
------------------
验收的第 5 条要求证明「测试不是恒绿」：把实现里那行 ``status="pending"``
改成保持原状态，至少一条测试要红。

一次性地改生产代码去证明这件事，只能证明一次 —— 改完还得改回来，
而改回来的那一次就没人盯了：下一次谁再动实现，恒绿就又回来了。所以
这里把那次改动**变成常驻测试**：不碰生产代码，只在测试里把实现换成那个
「一个字段都不写」的变异体，然后要求断言助手**必须**认出它。

两条用例，一对
--------------
* :func:`test_mutated_payload_is_detected` —— 把
  ``task_reset.build_reset_fields`` 换成返回空字典的变异体，打**真实
  路由**，然后要求 ``assert_task_reset_took_effect`` **抛**
  ``AssertionError``。它证明的是：这条断言对「没写 status」是敏感的，
  不是恒真。
* :func:`test_unmutated_implementation_passes_the_same_assertion` ——
  不换实现，打同一条路由，同一个助手**不许抛**。它证明的是：当前实现
  确实写了执行记账字段。

两条合起来才是「重置真的生效了」这句话的全部内容：助手能认出「没生效」
（第一条），而现在确实生效（第二条）。只写第二条 = 恒绿；只写第一条 =
一条永远失败的假测试。

为什么要打在**路由**上
--------------------
变异必须落在**路由实际使用的那个模块对象**上，否则这个负控就是假的：
直接调 ``build_reset_fields`` 只能证明「这个函数返回了空字典」，证明
不了「路由用了它」。所以这里打的是真的 ``POST`` —— 走 ``TestClient``、
走全部中间件、走真的仓储、真的 SQLite。

补丁落在 ``routes.execution._task_reset.build_reset_fields`` 上：
``routes/execution.py`` 在**模块层**（不是 handler 里面）import 了
``task_reset``，就是为了让负控能从路由脚下把实现换掉（见那个 import
上方的注释）。补丁若落错了对象，第一条用例会以
「DID NOT RAISE」失败 —— 假负控会自己喊出来。

为什么变异体是「空字典」而不是「少写一个字段」
--------------------------------------------
``update_task(fields={})`` 走的是**部分更新**语义：载荷里没有的列一个
都不动，只有 ``_repo_version`` 和 ``updated_at`` 会推进。所以它精确地
模拟了「一个字段都不写」这个变异，而生产代码里真正会发生的偏差
（「status 那一行没写」）也正是这个形状。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server
import routes.execution as exec_routes
from tests.fixtures.task_reset_env import (
    DEFAULT_PLAN_ID,
    assert_task_reset_took_effect,
    post_reset,
    seed_plan,
)

client = TestClient(server.app)


def test_mutated_payload_is_detected(isolated_plans_dir, tmp_path, monkeypatch):
    """变异体（一个字段都不写）必须被断言助手抓住。

    这条用例是整个负控的支点：它把「重置没生效」这件事变成一次**必然**
    的红灯。实现里那行 ``status="pending"`` 一旦没了、或者换了别的写法
    导致没写 status，这里就会以「DID NOT RAISE」失败 —— 而不是让整个
    套件安安静静地全绿。

    抓的是**同一个助手**：``assert_task_reset_took_effect``。所以这条
    用例同时钉住了两件事 —— 助手对变异敏感（这里），助手对真实实现
    放行（``test_unmutated_implementation_passes_the_same_assertion``）。
    """
    plans_dir, db_path = seed_plan(
        isolated_plans_dir,
        tmp_path / "state.db",
        plan_id=DEFAULT_PLAN_ID,
        status="failed",
    )

    # 负控：模拟「保持原状态」的实现——一个字段都不写。
    monkeypatch.setattr(
        exec_routes._task_reset, "build_reset_fields", lambda: {}
    )

    resp = post_reset(client, DEFAULT_PLAN_ID, "1")

    # 路由本身照常成功：负控改的是**写什么**，不是**能不能写**。
    assert resp.status_code == 200, resp.text
    # 变异确实生效了：状态原封不动地留在 ``failed``。这一行是给
    # 假负控看的 —— 补丁若落错了对象，这里会先红。
    assert resp.json()["task"]["status"] == "failed", resp.json()

    # 而助手认出了它：断言失败，失败的消息里带着 ``status`` 这个词
    # （助手的消息都带，负控才能认出是「这条」失败而不是「别的」失败）。
    with pytest.raises(AssertionError, match="status"):
        assert_task_reset_took_effect(db_path, DEFAULT_PLAN_ID, "1")


def test_unmutated_implementation_passes_the_same_assertion(
    isolated_plans_dir, tmp_path
):
    """对照组：当前实现过同一个助手，不抛。

    这是负控的另一半。没有它，第一条只说明「助手能挑刺」；加上它，
    才说明「现在这个实现经得起挑刺」。
    """
    plans_dir, db_path = seed_plan(
        isolated_plans_dir,
        tmp_path / "state.db",
        plan_id=DEFAULT_PLAN_ID,
        status="failed",
    )

    resp = post_reset(client, DEFAULT_PLAN_ID, "1")

    assert resp.status_code == 200, resp.text
    # 不抛：重置真的写了执行记账字段（status 回到 pending、attempt
    # 归零、上一轮的失败痕迹清空）。
    assert_task_reset_took_effect(db_path, DEFAULT_PLAN_ID, "1")