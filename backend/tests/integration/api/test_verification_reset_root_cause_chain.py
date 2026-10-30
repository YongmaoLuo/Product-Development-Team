"""``POST /api/verification/{plan_id}/reset`` — 根因链台账的清理（2026-10-30）。

背景
----
根因链台账 ``plans/<plan>/verification_root_cause_chain.json`` 记着每个验证点
走到了链条的哪一段（``root_cause`` → ``implement`` → ``BLOCKED_ON_USER``）。
reset 会把裁决、修复任务、每轮的磁盘产物都清掉，却**没有**清这条台账 ——
于是重跑时，链上那条旧的 ``BLOCKED_ON_USER`` 记录继续占着验证点：链记得
这个点已经"交给人了"，新一轮不会再自动派根因任务。

台账点名的报告（``vps[*].report_path``，即
``plans/<plan>/reports/root-cause-<vp>.md``）也是同一件事的两半：报告还在，
reader 就仍然认为"诊断已经交付"，于是判 ``root_cause_report_without_scope``
并再次停在人这里。

所以 reset 多了一个开关：``clear_root_cause_chain``。默认关 —— 清台账是
一个**决定**，不是 reset 的副作用。

三条契约
--------
1. **默认不动。** 不带这个字段的 reset 把台账和报告都留着，
   ``cleared.root_cause_chain`` 是空列表。
2. **只删点名的。** 删台账 + 台账 ``vps[*].report_path`` 点名的报告；同目录
   下别的报告（``keep-me.md``）不动。**不做 glob** —— 报告目录里放着不在链上
   的点的报告，reset 没有资格把它们一起带走。
3. **越界的路径不删。** 绝对路径、含 ``..`` 的路径、不在
   ``plans/<plan_id>/`` 下的路径一律跳过：台账是数据，数据没有资格指名
   磁盘上别的文件。单个文件删不掉只记日志，不让整个 reset 变成 500。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261030-reset-root-cause"

#: The chain's ledger file, relative to the plan directory.
CHAIN_FILENAME = "verification_root_cause_chain.json"


class _DormantThread:
    """``threading.Thread`` stub that never actually starts a thread."""

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation.

    Mirrors ``test_verification_reset.py``'s fixture (read-only reference):
    the endpoint reads the plan directory and the state DB, and both have to
    point at this test's own tree.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)

    yield plans_dir, conn, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _seed_plan(plans_dir: Path, db_path: Path):
    """A plan that is in verification, with a ``reports/`` directory.

    Returns the plan directory. A second connection is used (not the
    fixture's) so the rows the endpoint writes are re-read fresh.
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "reports").mkdir(exist_ok=True)

    conn = open_db(db_path)
    migrate(conn)
    RoutingRepository(conn).insert(PLAN_ID, "verification_running")
    ExecutionRepository(conn).insert(
        plan_id=PLAN_ID,
        project_dir=str(plan_dir / "project"),
    )
    VerificationRepository(conn).insert(
        PLAN_ID, verification_status="failed", max_rounds=3,
    )
    conn.close()
    return plan_dir


def _write_chain(plan_dir: Path, vps: dict) -> None:
    """Write the chain's ledger in the shape the orchestrator writes it."""
    payload = {
        "version": 1,
        "stage": "BLOCKED_ON_USER",
        "vps": vps,
        "updated_round": 3,
    }
    (plan_dir / CHAIN_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1) 开了就清：台账 + 点名的报告
# ---------------------------------------------------------------------------


def test_true_deletes_chain_and_named_reports(env, client):
    """``clear_root_cause_chain=true`` → 台账与它点名的报告都消失。

    对照报告（``keep-me.md``）必须还在：清理是**点名**的，不是扫目录的。
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    named = plan_dir / "reports" / "root-cause-VP-3.md"
    named.write_text("# 根因\n连接池耗尽。\n", encoding="utf-8")
    keep = plan_dir / "reports" / "keep-me.md"
    keep.write_text("不在链上的报告\n", encoding="utf-8")
    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": f"plans/{PLAN_ID}/reports/root-cause-VP-3.md",
        },
    })

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset",
        json={
            "clear_root_cause_chain": True,
            "clear_artifacts": False,
            "supersede_repair_tasks": False,
        },
    )
    assert resp.status_code == 200, resp.text

    assert not (plan_dir / CHAIN_FILENAME).exists(), "台账还在，重跑时会继续占着验证点"
    assert not named.exists(), "台账点名的报告还在，reader 仍会判 scope 缺失"
    assert keep.exists(), "reset 只能删台账点名的报告，不能扫整个 reports 目录"
    assert resp.json()["cleared"]["root_cause_chain"] == [
        CHAIN_FILENAME,
        "reports/root-cause-VP-3.md",
    ]


# ---------------------------------------------------------------------------
# 2) 默认不动 —— 清台账是一个决定，不是 reset 的副作用
# ---------------------------------------------------------------------------


def test_default_keeps_chain(env, client):
    """不带这个字段的 reset 把台账和报告都留着。"""
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    named = plan_dir / "reports" / "root-cause-VP-3.md"
    named.write_text("# 根因\n连接池耗尽。\n", encoding="utf-8")
    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": f"plans/{PLAN_ID}/reports/root-cause-VP-3.md",
        },
    })

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    assert (plan_dir / CHAIN_FILENAME).exists(), "默认不该清台账"
    assert named.exists(), "默认不该清报告"
    assert resp.json()["cleared"]["root_cause_chain"] == []


# ---------------------------------------------------------------------------
# 3) 越界的 report_path 不删 —— 台账是数据，数据没有资格指名别的文件
# ---------------------------------------------------------------------------


def test_report_path_outside_plan_is_skipped(env, client):
    """``../outside.md`` 指向 plan 目录之外 → 跳过，但台账照删。"""
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    outside = plans_dir / "outside.md"
    outside.write_text("不在 plan 目录里\n", encoding="utf-8")
    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": "../outside.md",
        },
    })

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset",
        json={
            "clear_root_cause_chain": True,
            "clear_artifacts": False,
            "supersede_repair_tasks": False,
        },
    )
    assert resp.status_code == 200, resp.text

    assert outside.exists(), "越界的 report_path 不得删文件"
    assert not (plan_dir / CHAIN_FILENAME).exists(), "台账本身照删"
    assert resp.json()["cleared"]["root_cause_chain"] == [CHAIN_FILENAME]


def test_absolute_report_path_is_skipped(env, client, tmp_path):
    """绝对路径同样跳过 —— 台账是数据，不是磁盘的授权。"""
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    absolute = tmp_path / "absolute-target.md"
    absolute.write_text("绝对路径\n", encoding="utf-8")
    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": str(absolute),
        },
    })

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset",
        json={
            "clear_root_cause_chain": True,
            "clear_artifacts": False,
            "supersede_repair_tasks": False,
        },
    )
    assert resp.status_code == 200, resp.text

    assert absolute.exists(), "绝对路径的 report_path 不得删文件"
    assert not (plan_dir / CHAIN_FILENAME).exists(), "台账本身照删"
    assert resp.json()["cleared"]["root_cause_chain"] == [CHAIN_FILENAME]


def test_report_path_of_another_plan_is_skipped(env, client):
    """指着别的 plan 的 report_path 同样跳过 —— 台账只管自己 plan 下的文件。"""
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    other_plan = plans_dir / "20261030-other-plan"
    (other_plan / "reports").mkdir(parents=True)
    other_report = other_plan / "reports" / "root-cause-VP-9.md"
    other_report.write_text("别的 plan 的报告\n", encoding="utf-8")
    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": "plans/20261030-other-plan/reports/root-cause-VP-9.md",
        },
    })

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset",
        json={
            "clear_root_cause_chain": True,
            "clear_artifacts": False,
            "supersede_repair_tasks": False,
        },
    )
    assert resp.status_code == 200, resp.text

    assert other_report.exists(), "别的 plan 的文件不得删"
    assert not (plan_dir / CHAIN_FILENAME).exists(), "台账本身照删"
    assert resp.json()["cleared"]["root_cause_chain"] == [CHAIN_FILENAME]


def test_report_path_naming_a_directory_is_skipped(env, client, caplog):
    """``report_path`` 指着目录 → 跳过。契约说的是「删文件」。

    目录不是「删不掉的报告」，它是**根本不是报告** —— 所以它被安静地跳过，
    而不是记一条 "unlink failed" 的错误日志（那条日志会把一个正常的跳过
    说成一次失败）。
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": f"plans/{PLAN_ID}/reports",
        },
    })

    with caplog.at_level("ERROR"):
        resp = client.post(
            f"/api/verification/{PLAN_ID}/reset",
            json={
                "clear_root_cause_chain": True,
                "clear_artifacts": False,
                "supersede_repair_tasks": False,
            },
        )
    assert resp.status_code == 200, resp.text

    assert (plan_dir / "reports").is_dir(), "目录不是报告，不该被删"
    assert not (plan_dir / CHAIN_FILENAME).exists(), "台账本身照删"
    assert resp.json()["cleared"]["root_cause_chain"] == [CHAIN_FILENAME]
    assert not any(
        "unlink failed" in record.getMessage()
        for record in caplog.records
    ), "跳过一个目录不是一次删除失败，不该留下错误日志"


# ---------------------------------------------------------------------------
# 4) 删不掉的文件只记日志 —— 一个不可写的文件不该把整个 reset 变成 500
# ---------------------------------------------------------------------------


def test_unlink_failure_is_logged_not_raised(env, client, monkeypatch, caplog):
    """一个删不掉的报告 → 记日志，reset 仍然 200。"""
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)

    named = plan_dir / "reports" / "root-cause-VP-3.md"
    named.write_text("# 根因\n连接池耗尽。\n", encoding="utf-8")
    _write_chain(plan_dir, {
        "VP-3": {
            "stage": "root_cause",
            "round": 3,
            "report_path": f"plans/{PLAN_ID}/reports/root-cause-VP-3.md",
        },
    })

    real_unlink = Path.unlink

    def _boom(self, *args, **kwargs):
        if self.name == "root-cause-VP-3.md":
            raise OSError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _boom)

    with caplog.at_level("ERROR"):
        resp = client.post(
            f"/api/verification/{PLAN_ID}/reset",
            json={
                "clear_root_cause_chain": True,
                "clear_artifacts": False,
                "supersede_repair_tasks": False,
            },
        )
    assert resp.status_code == 200, resp.text

    assert named.exists(), "删不掉的文件应当留在原地"
    assert not (plan_dir / CHAIN_FILENAME).exists(), "台账本身照删"
    assert resp.json()["cleared"]["root_cause_chain"] == [CHAIN_FILENAME], (
        "没删掉的报告不得被报成删掉了"
    )
    assert any(
        "unlink failed" in record.getMessage()
        for record in caplog.records
    ), "删不掉的文件必须留下日志"
