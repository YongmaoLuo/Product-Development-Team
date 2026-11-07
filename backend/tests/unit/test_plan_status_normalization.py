"""显示状态归一化 —— 相位 completed 的计划不再显示陈旧的 failed/stopped。

背景
----
``plan_routing.current_phase`` 与 ``plan_execution.exec_status`` 是两套记录：

* 相位是**工作流**的状态（``completed`` / ``failed`` / ``stopped`` …）；
* 执行行是**最后一次执行尝试**的结果。

计划走到终点时，没有任何写者会去改执行行 —— 于是完成了一条的计划，行上
还留着当年那次被中断的运行留下的 ``failed`` / ``stopped``。实测
``.pdt/state.db``：34 条 fail/stop 里有 19 条的相位是 ``completed``，而对外
的读模型照旧把它们显示成 fail/stop。

这里钉住**读模型**的规则（``plan_status.normalize_exec_status``）：

=============================  ==============  ==============
相位                            执行行            对外显示
=============================  ==============  ==============
``completed``                  ``failed``       ``completed``
``completed``                  ``stopped``      ``completed``
``completed``                  ``running``      ``completed``
``failed``                     ``failed``       ``failed``
``stopped``                    ``stopped``      ``stopped``
``failed``                     ``completed``    ``failed``
=============================  ==============  ==============

真失败不被伪装成完成：相位是 ``failed`` / ``stopped`` 的计划照旧显示失败 /
停止。

另外几条钉的是**读路径真的接上了**，不是一个孤立的纯函数：

* ``GET /api/execution/{id}/status`` 的 ``status``；
* ``GET /api/execution/{id}/progress`` 的 ``execution_status``；
* ``GET /api/plan/{id}/summary`` 的 ``execution.status``。

本次改动只动读模型：不改数据库、不回填、不改 ``plan_execution`` 的写入语义，
也不改 ``current_phase`` 的写路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from plan_status import normalize_exec_status
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 规则本身（纯函数）
# ---------------------------------------------------------------------------


def test_rule_completed_phase_hides_a_stale_failed_verdict():
    """相位 completed + 执行行 failed → 对外显示 completed。

    这是本任务的主规则：相位说这条计划做完了，执行行上那个 ``failed`` 是
    很久以前某次被中断的运行留下的，不是这条计划的结果。
    """
    assert normalize_exec_status("completed", "failed") == "completed"


def test_rule_completed_phase_hides_a_stale_stopped_verdict():
    """相位 completed + 执行行 stopped → 对外显示 completed。

    与上一条同形，只是陈旧的裁决是 ``stopped``（被停止的那次运行）。
    """
    assert normalize_exec_status("completed", "stopped") == "completed"


def test_rule_completed_phase_hides_a_leftover_running_verdict():
    """相位 completed + 执行行 running（异常残留）→ 对外仍显示 completed。

    相位是终态成功，行上那个 ``running`` 是残留：工作流已经说它做完了，
    执行行却没有跟上。相位是权威。
    """
    assert normalize_exec_status("completed", "running") == "completed"


def test_rule_failed_phase_keeps_the_failed_verdict():
    """相位 failed + 执行行 failed → 仍然是 failed（不变）。

    真失败不许被伪装成完成 —— 这是本次改动最不能碰的一条。
    """
    assert normalize_exec_status("failed", "failed") == "failed"


def test_rule_stopped_phase_keeps_the_stopped_verdict():
    """相位 stopped + 执行行 stopped → 仍然是 stopped（不变）。"""
    assert normalize_exec_status("stopped", "stopped") == "stopped"


def test_rule_failed_phase_keeps_failed_even_when_the_row_says_completed():
    """相位 failed + 执行行 completed → 仍然是 failed（不变）。

    真失败不被一条「说完成了」的执行行盖住：相位是工作流自己的裁决。
    """
    assert normalize_exec_status("failed", "completed") == "failed"


def test_rule_a_non_terminal_phase_leaves_the_execution_row_alone():
    """相位不是终态 → 执行行说什么就是什么。

    ``queued`` / ``executing`` 这样的相位说的不是「怎么结束的」，所以行上
    的裁决仍然是唯一的答案。
    """
    assert normalize_exec_status("queued", "failed") == "failed"
    assert normalize_exec_status("executing", "running") == "running"
    assert normalize_exec_status("", "failed") == "failed"


def test_rule_not_started_is_not_a_verdict():
    """执行行没有裁决（``not_started``）时，相位不替它编一个。

    ``not_started`` 说的是「这条计划从没跑过执行」，相位 completed 也不
    能把它说成 completed —— 那是在替执行行撒谎。
    """
    assert normalize_exec_status("completed", "not_started") == "not_started"
    assert normalize_exec_status("completed", "") == ""


# ---------------------------------------------------------------------------
# 读路径（生产入口）
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch) -> Path:
    """A state database of this test's own, plus the plans tree beside it."""
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    return db_path


def _seed(state_db: Path, plan_id: str, *, phase: str, exec_status: str) -> str:
    """Materialise one plan: a routing row and an execution row.

    The two rows are the two records the read model reconciles — the
    workflow's phase and the last run's verdict.
    """
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )

    conn = open_db(state_db)
    try:
        migrate(conn)
        routing = RoutingRepository(conn)
        if routing.find(plan_id) is None:
            routing.insert(plan_id, phase)
        else:
            routing.write_plan_state(
                plan_id,
                phase=phase,
                completed_phases=[],
                review_rounds={},
                flags={},
                verification={},
                last_updated=None,
            )
        ExecutionRepository(conn).insert(plan_id, exec_status=exec_status)
    finally:
        conn.close()
    return plan_id


def _clean_registry(plan_id: str) -> None:
    server._execution_state.pop(plan_id, None)


def test_execution_status_endpoint_shows_completed_for_a_completed_plan(state_db):
    """``GET /api/execution/{id}/status`` —— 相位 completed 的计划显示 completed。

    这就是页面用的那个接口：执行面板的徽标读它。陈旧的 ``failed`` 不再
    透出去。
    """
    plan_id = _seed(
        state_db, "20261023-norm-completed", phase="completed",
        exec_status="failed",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    finally:
        _clean_registry(plan_id)

    assert body["status"] == "completed", (
        "相位 completed 的计划不该再显示陈旧的 failed；实际是 "
        f"{body.get('status')!r}"
    )


def test_execution_status_endpoint_keeps_failed_for_a_failed_plan(state_db):
    """同一条路径：相位 failed 的计划照旧显示 failed。

    真失败不许被伪装成完成。
    """
    plan_id = _seed(
        state_db, "20261023-norm-failed", phase="failed", exec_status="failed",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    finally:
        _clean_registry(plan_id)

    assert body["status"] == "failed", (
        "相位 failed 的计划必须照旧显示 failed；实际是 "
        f"{body.get('status')!r}"
    )


def test_execution_progress_endpoint_shows_completed_for_a_completed_plan(state_db):
    """``GET /api/execution/{id}/progress`` —— ``execution_status`` 同样归一化。

    进度面板读的是这个字段；两个接口不能对同一条计划给出两个答案。
    """
    plan_id = _seed(
        state_db, "20261023-norm-progress", phase="completed",
        exec_status="stopped",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/execution/{plan_id}/progress")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    finally:
        _clean_registry(plan_id)

    assert body["execution_status"] == "completed", (
        "进度接口也必须归一化；实际是 "
        f"{body.get('execution_status')!r}"
    )


def test_plan_summary_execution_status_shows_completed(state_db):
    """``GET /api/plan/{id}/summary`` —— ``execution.status`` 同样归一化。"""
    plan_id = _seed(
        state_db, "20261023-norm-summary", phase="completed",
        exec_status="failed",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/plan/{plan_id}/summary")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    finally:
        _clean_registry(plan_id)

    assert body["execution"]["status"] == "completed", (
        "summary 的 execution.status 必须归一化；实际是 "
        f"{body.get('execution', {}).get('status')!r}"
    )


def test_the_execution_row_itself_is_not_rewritten(state_db):
    """归一化只发生在读模型上 —— 行里的 ``exec_status`` 一个字都没改。

    本次改动不许回填、不许改写语义：读出来是 completed，行里仍然是那次
    被中断的运行的 ``failed``。
    """
    plan_id = _seed(
        state_db, "20261023-norm-untouched", phase="completed",
        exec_status="failed",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200, resp.text
    finally:
        _clean_registry(plan_id)

    conn = open_db(state_db)
    try:
        row = conn.execute(
            "SELECT exec_status FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "the execution row vanished"
    assert row[0] == "failed", (
        "读模型不许回填：行里的 exec_status 必须还是它原来写的那个；实际是 "
        f"{row[0]!r}"
    )


def test_the_routing_phase_itself_is_not_rewritten(state_db):
    """归一化也不许改 ``current_phase`` —— 相位是写路径的地盘。"""
    plan_id = _seed(
        state_db, "20261023-norm-phase-untouched", phase="failed",
        exec_status="failed",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200, resp.text
    finally:
        _clean_registry(plan_id)

    conn = open_db(state_db)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "the routing row vanished"
    assert row[0] == "failed", (
        "读模型不许改相位；实际是 "
        f"{row[0]!r}"
    )


def test_an_unknown_phase_leaves_the_execution_row_alone(state_db):
    """没有 routing 行（相位读不到）时，执行行的裁决照旧透出。

    读不到相位就没有权威可依，不能拿默认值去盖掉一个真实的裁决。
    """
    plan_id = "20261023-norm-no-routing"
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )
    conn = open_db(state_db)
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(plan_id, exec_status="failed")
    finally:
        conn.close()

    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        resp = client.get(f"/api/execution/{plan_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    finally:
        _clean_registry(plan_id)

    assert body["status"] == "failed", (
        "没有相位可依时，执行行的裁决必须原样透出；实际是 "
        f"{body.get('status')!r}"
    )


def test_the_two_readers_agree_about_one_completed_plan(state_db):
    """两个读者对同一条计划必须说同一句话。

    ``/api/execution/{id}/status`` 与 ``/api/execution/{id}/progress`` 是
    两条不同的读路径；归一化之后它们不能对同一条计划给出两个答案。
    """
    plan_id = _seed(
        state_db, "20261023-norm-agree", phase="completed",
        exec_status="failed",
    )
    _clean_registry(plan_id)
    try:
        client = TestClient(server.app)
        status_resp = client.get(f"/api/execution/{plan_id}/status")
        progress_resp = client.get(f"/api/execution/{plan_id}/progress")
        assert status_resp.status_code == 200, status_resp.text
        assert progress_resp.status_code == 200, progress_resp.text
        shown_status = status_resp.json()["status"]
        shown_progress = progress_resp.json()["execution_status"]
    finally:
        _clean_registry(plan_id)

    assert shown_status == shown_progress, (
        f"两个读者给出了两个答案：{shown_status!r} vs {shown_progress!r}"
    )
    assert shown_status == "completed", shown_status
