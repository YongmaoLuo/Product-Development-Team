"""``PlanState`` 的验证写入只落 ``plan_verification``。

背景
----
验证状态有两个存储：``plan_verification``（权威表）与 ``plan_routing.verification``
（镜像列）。``PlanState`` 的四个写入方法以前都改内存里的
``_state['verification']`` 再 ``_save_state()``，落点是镜像列 —— 于是同一轮验证
有两个存储、两个答案。

本文件钉住改道之后的行为：

* 写入落到 ``plan_verification`` 行（权威表）；
* ``plan_routing.verification`` 列**不再被这些写入改动**（逐字节相同）；
* 写成功后内存里的 ``_state['verification']`` 跟着更新 —— 读后写一致；
* ``stop_reason=None`` 的语义是「不改」（与任务 7 的 ``set_state`` 一致）；
* 状态库打不开时按 best-effort 处理：记 warning、只改内存、不抛。

测试数据
--------
真实的 ``plan_routing`` 行 + 真实的 ``plan_verification`` 行：计划状态用
:class:`RoutingRepository` 写，验证状态用 :class:`VerificationRepository` 读 ——
与生产路径同一条路。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_state as plan_state_module  # noqa: E402
from plan_state import PlanState  # noqa: E402

#: ``backend/plan_state.py`` 的模块级 logger。
LOGGER_NAME = "plan_state"


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份自己的状态库。"""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _stale_lifecycle(payload: dict) -> dict:
    """The four lifecycle keys, as a stale writer left them in the column."""
    return {
        key: value
        for key, value in payload.items()
        if key not in ("baseline", "baseline_doc_sha256")
    }


def _seed_routing_row(
    plan_dir: Path,
    *,
    phase: str = "executing",
    verification: Optional[dict] = None,
) -> None:
    """按生产写路径的方式写 ``plan_routing`` 行。

    ``PlanState`` 的状态就是从这一行读出来的，所以这里也走
    :class:`RoutingRepository`，测试与生产同路。

    镜像列里如今只剩基线；生命周期四键只以**过期写者**的形态留在列里。
    这些用例要证明的正是「写权威表不许顺带重写列」，所以列里必须先有
    内容可比 —— 那四键走裸 SQL（照旧写者的手笔）。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    payload = (
        verification
        if verification is not None
        else {
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        }
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        repo.write_plan_state(
            plan_dir.name,
            phase=phase,
            completed_phases=["execution"],
            review_rounds={},
            flags={},
            last_updated=None,
        )
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (json.dumps(_stale_lifecycle(payload)), plan_dir.name),
        )
        conn.commit()
    finally:
        conn.close()


def _verification_row(plan_id: str) -> Optional[dict[str, Any]]:
    """读 ``plan_verification`` 行（权威表）。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        return VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()


def _routing_verification_column(plan_id: str) -> Optional[str]:
    """读 ``plan_routing.verification`` 列的原始文本（镜像列）。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT verification FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _plan(tmp_path: Path, plan_id: str, **kwargs: Any) -> PlanState:
    """建一个计划目录并返回它的 ``PlanState``。"""
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    _seed_routing_row(plan_dir, **kwargs)
    return PlanState(plan_dir)


# ---------------------------------------------------------------------------
# TDD spec 1: set_verification_status 写权威表
# ---------------------------------------------------------------------------


def test_set_status_writes_plan_verification(state_db: Path, tmp_path: Path):
    """状态 / 轮次 / 停止原因一起落到 ``plan_verification`` 行。"""
    ps = _plan(tmp_path, "status-write")

    ps.set_verification_status(
        "failed", stop_reason="max_rounds_reached", round_n=3,
    )

    row = _verification_row("status-write")
    assert row is not None, "权威表里必须有这个计划的行"
    assert row["verification_status"] == "failed"
    assert row["round"] == 3
    assert row["verification_stop_reason"] == "max_rounds_reached"


def test_set_status_keeps_the_in_memory_copy_in_sync(
    state_db: Path, tmp_path: Path,
):
    """写成功后内存副本跟着更新 —— 读后写一致。"""
    ps = _plan(tmp_path, "status-sync")

    ps.set_verification_status("passed", round_n=2)

    assert ps.get_verification_status() == "passed"
    assert ps.get_verification_round() == 2


def test_set_status_leaves_the_routing_column_alone(
    state_db: Path, tmp_path: Path,
):
    """``set_verification_status`` 不再碰镜像列。"""
    ps = _plan(tmp_path, "status-column")
    before = _routing_verification_column("status-column")

    ps.set_verification_status("failed", stop_reason="user_stopped", round_n=1)

    assert _routing_verification_column("status-column") == before


def test_stop_reason_none_means_do_not_change(
    state_db: Path, tmp_path: Path,
):
    """``stop_reason=None`` 是「不改」，不是「清空」（与任务 7 一致）。"""
    ps = _plan(tmp_path, "status-none")
    ps.set_verification_stop_reason("max_rounds_reached")

    ps.set_verification_status("failed")

    assert ps.get_verification_stop_reason() == "max_rounds_reached"
    row = _verification_row("status-none")
    assert row["verification_stop_reason"] == "max_rounds_reached"


# ---------------------------------------------------------------------------
# TDD spec 2: set_verification_max_rounds 不动镜像列
# ---------------------------------------------------------------------------


def test_max_rounds_write_leaves_routing_column_alone(
    state_db: Path, tmp_path: Path,
):
    """上限写进权威表，镜像列逐字节不变。"""
    ps = _plan(tmp_path, "max-rounds")
    before = _routing_verification_column("max-rounds")
    assert before is not None, "前置条件：镜像列存在"

    ps.set_verification_max_rounds(5)

    assert _routing_verification_column("max-rounds") == before, (
        "上限写入不得顺带重写镜像列"
    )
    row = _verification_row("max-rounds")
    assert row["max_rounds"] == 5
    assert ps.get_verification_max_rounds() == 5


def test_max_rounds_write_does_not_rewrite_the_phase(
    state_db: Path, tmp_path: Path,
):
    """上限写入不得顺带重写相位 / 已完成相位。

    注意读的是**同一个实例**的 getter：本任务只改写路径，读路径
    （从 ``plan_verification`` 取值）是任务 9 的事 —— 新建的实例此刻仍从
    镜像列读验证状态。
    """
    ps = _plan(tmp_path, "max-rounds-phase", phase="executing")
    ps.reload()

    ps.set_verification_max_rounds(7)

    assert ps.get_verification_max_rounds() == 7, "内存副本跟着更新"
    fresh = PlanState(ps.plan_dir)
    assert fresh.get_current_phase() == "executing", "相位不得被顺带重写"
    assert fresh.get_completed_phases() == ["execution"], (
        "已完成相位不得被顺带重写"
    )


# ---------------------------------------------------------------------------
# TDD spec 3: increment_verification_round 落库
# ---------------------------------------------------------------------------


def test_increment_round_is_persisted(state_db: Path, tmp_path: Path):
    """连续两次自增 → 轮次落在权威表上是 2。"""
    ps = _plan(tmp_path, "increment")

    ps.increment_verification_round()
    ps.increment_verification_round()

    row = _verification_row("increment")
    assert row["round"] == 2
    assert ps.get_verification_round() == 2


def test_increment_round_leaves_the_routing_column_alone(
    state_db: Path, tmp_path: Path,
):
    """轮次自增不再重写镜像列。"""
    ps = _plan(tmp_path, "increment-column")
    before = _routing_verification_column("increment-column")

    ps.increment_verification_round()

    assert _routing_verification_column("increment-column") == before


# ---------------------------------------------------------------------------
# TDD spec 4: begin_verification 经仓库落 pending / round_n
# ---------------------------------------------------------------------------


def test_begin_verification_writes_pending_through_repo(
    state_db: Path, tmp_path: Path,
):
    """进入验证相位时，pending 与轮次落进权威表。"""
    ps = _plan(tmp_path, "begin-repo", phase="executing")

    ps.begin_verification(round_n=4)

    row = _verification_row("begin-repo")
    assert row is not None, "权威表里必须有这个计划的行"
    assert row["verification_status"] == "pending"
    assert row["round"] == 4


def test_begin_verification_keeps_the_phase_transition(
    state_db: Path, tmp_path: Path,
):
    """相位转换本身不受影响：相位落 routing，完成相位被追加。"""
    ps = _plan(tmp_path, "begin-phase", phase="executing")

    ps.begin_verification()

    assert ps.get_current_phase() == "verification"
    fresh = PlanState(ps.plan_dir)
    assert fresh.get_current_phase() == "verification"
    assert "execution" in fresh.get_completed_phases()


def test_begin_verification_is_a_no_op_in_a_verification_phase(
    state_db: Path, tmp_path: Path,
):
    """已在验证相位 → 不改任何东西（轮次不被清掉）。"""
    ps = _plan(tmp_path, "begin-noop", phase="verification_running")
    ps.set_verification_round(2)

    ps.begin_verification(round_n=99)

    assert ps.get_verification_round() == 2
    row = _verification_row("begin-noop")
    assert row["round"] == 2


# ---------------------------------------------------------------------------
# TDD spec 5: 两个新方法各写各的字段
# ---------------------------------------------------------------------------


def test_set_stop_reason_and_round_helpers(
    state_db: Path, tmp_path: Path,
):
    """``set_verification_round`` 写轮次，``set_verification_stop_reason``
    写停止原因 —— 各写各的字段。"""
    ps = _plan(tmp_path, "helpers")

    ps.set_verification_round(3)
    row = _verification_row("helpers")
    assert row["round"] == 3
    assert row["verification_stop_reason"] is None

    ps.set_verification_stop_reason("max_rounds_reached")
    row = _verification_row("helpers")
    assert row["verification_stop_reason"] == "max_rounds_reached"
    assert row["round"] == 3, "写停止原因不得改轮次"


def test_the_helpers_keep_the_in_memory_copy_in_sync(
    state_db: Path, tmp_path: Path,
):
    ps = _plan(tmp_path, "helpers-sync")

    ps.set_verification_round(5)
    ps.set_verification_stop_reason("user_stopped")

    assert ps.get_verification_round() == 5
    assert ps.get_verification_stop_reason() == "user_stopped"


# ---------------------------------------------------------------------------
# 第五个验证写入方法：stop_verification_loop
# ---------------------------------------------------------------------------


def test_stop_verification_loop_records_the_reason_on_the_authoritative_row(
    state_db: Path, tmp_path: Path,
):
    """停验证的理由落在权威行上 —— 镜像列死了之后，理由就只剩这一处。"""
    ps = _plan(tmp_path, "stop-loop", phase="verification_failed")

    ps.stop_verification_loop("max_rounds_reached")

    row = _verification_row("stop-loop")
    assert row["verification_stop_reason"] == "max_rounds_reached"
    assert row["verification_status"] == "loop_stopped"
    assert ps.get_verification_stop_reason() == "max_rounds_reached"


# ---------------------------------------------------------------------------
# best-effort：状态库打不开时只改内存，不抛
# ---------------------------------------------------------------------------


def test_unavailable_state_db_is_best_effort(
    state_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """库打不开 → 记 warning、只改内存、不抛异常。"""
    ps = _plan(tmp_path, "no-db")

    def _boom() -> Path:
        # A directory is not a database file: sqlite3 refuses to open it.
        return tmp_path

    monkeypatch.setattr(plan_state_module, "_state_db_path", _boom)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        ps.set_verification_max_rounds(9)

    assert ps.get_verification_max_rounds() == 9, "内存副本仍然要更新"
    messages = [record.getMessage() for record in caplog.records]
    assert any("verification_state_write" in m for m in messages), (
        f"必须留下 warning, got {messages!r}"
    )


def test_unavailable_state_db_does_not_break_begin_verification(
    state_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """库打不开也不能让「进入验证」炸掉。"""
    ps = _plan(tmp_path, "no-db-begin", phase="executing")

    def _boom() -> Path:
        return tmp_path

    monkeypatch.setattr(plan_state_module, "_state_db_path", _boom)

    ps.begin_verification(round_n=3)

    assert ps.get_current_phase() == "verification"
    assert ps.get_verification_round() == 3


# ---------------------------------------------------------------------------
# 写入方法不得再调用 _save_state（那是顺带重写相位的入口）
# ---------------------------------------------------------------------------


def test_the_writers_do_not_call_save_state(
    state_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """验证状态写入不得顺带把整个 plan_state 存盘。"""
    ps = _plan(tmp_path, "no-save")

    calls: list[str] = []
    monkeypatch.setattr(
        ps, "_save_state", lambda *a, **kw: calls.append("save"),
    )

    ps.set_verification_status("failed", round_n=1)
    ps.set_verification_max_rounds(4)
    ps.increment_verification_round()
    ps.set_verification_round(2)
    ps.set_verification_stop_reason("user_stopped")

    assert calls == [], (
        f"这些写入方法不得调用 _save_state, got {calls!r}"
    )