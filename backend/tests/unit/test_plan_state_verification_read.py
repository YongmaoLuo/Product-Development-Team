"""``PlanState`` 的验证状态读权威行 ``plan_verification``，不读镜像列。

背景
----
验证状态有两个存储：``plan_verification``（权威表）与 ``plan_routing.verification``
（镜像列）。写入已经全部改道到权威表（arch 决策点 1 / 3），读路径必须同步 ——
否则 getter 仍返回镜像列里那个陈旧的值。

本文件钉住改道之后的行为：

* 四个生命周期键（``status`` / ``round`` / ``max_rounds`` / ``stop_reason``）
  来自 ``plan_verification`` 行 —— 镜像列与之冲突时以权威行为准；
* 没有权威行 → ``not_started`` / ``0`` / ``DEFAULT_MAX_VERIFICATION_ROUNDS`` /
  ``None``，与 ``GET /api/verification/{id}/status`` 的默认值一致；
* ``baseline`` / ``baseline_doc_sha256`` 仍然只从镜像列解出 —— 它们不是验证
  状态，``plan_verification`` 没有这两列；
* 单参调用 ``_sqlite_row_to_state(row)`` 仍然可用，返回值里不再有镜像列的
  四个状态键。

测试数据
--------
真实的 ``plan_routing`` 行 + 真实的 ``plan_verification`` 行：计划状态用
:class:`RoutingRepository` 写，验证状态用 :class:`VerificationRepository` 写 ——
与生产路径同一条路。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_state as plan_state_module  # noqa: E402
from plan_state import PlanState  # noqa: E402


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份自己的状态库。"""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _stale_lifecycle(payload: dict) -> dict:
    """The four lifecycle keys, as a stale writer left them in the column.

    The mirror column no longer carries the verification state; these
    cases exist to prove the reader does not pick them up, so they have
    to be *in* the column — written the way the old writer wrote them.
    """
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

    ``verification`` 是**镜像列**的内容。列里如今只剩基线；生命周期四键只
    以**过期写者**的形态留在列里 —— 读侧不得被它们带偏，所以那四键走裸
    SQL（照旧写者的手笔），基线走列现在唯一的写者
    :meth:`RoutingRepository.write_verification_baseline`。
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
        baseline = payload.get("baseline")
        if baseline is not None:
            repo.write_verification_baseline(
                plan_dir.name,
                baseline,
                payload.get("baseline_doc_sha256", ""),
            )
    finally:
        conn.close()


def _seed_verification_row(
    plan_id: str,
    *,
    status: str,
    round_n: int = 0,
    max_rounds: int = 3,
    stop_reason: Optional[str] = None,
) -> None:
    """写 ``plan_verification`` 行（权威表）。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id,
            status,
            round=round_n,
            max_rounds=max_rounds,
            verification_stop_reason=stop_reason,
        )
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


def _corrupt_verification_column(plan_id: str) -> None:
    """把镜像列写成坏 JSON。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            ("{not json at all", plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _corrupt_authority_json_column(plan_id: str) -> None:
    """把权威行的一个 JSON 列写成坏 blob —— 行在，但整行解不开。

    ``plan_verification`` 的 JSON 列（``results`` / ``verdicts`` /
    ``execution_results`` / …）逐列 ``json.loads``，所以一个坏 blob 就让
    整行读不出来 —— 行**存在**，却读不出来。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        conn.execute(
            "UPDATE plan_verification SET results = 'not-json' WHERE plan_id = ?",
            (plan_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_lifecycle(plan_id: str) -> Optional[tuple]:
    """读权威行的四个生命周期列 —— 不解码任何 JSON 列。

    用裸 SQL 读，因为断言要看的正是「权威行原封不动」：走
    :meth:`VerificationRepository.current` 会先解码 JSON 列，坏 blob 会
    直接抛异常，断言就没法说「行还在、内容没变」。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        return conn.execute(
            "SELECT verification_status, round, max_rounds, "
            "verification_stop_reason FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()


def _plan_dir(tmp_path: Path, plan_id: str) -> Path:
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _routing_row(plan_id: str) -> Optional[dict[str, Any]]:
    """读 ``plan_routing`` 行（镜像列住在这一行）。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        return RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TDD spec 1: getter 读权威行
# ---------------------------------------------------------------------------


def test_getters_read_plan_verification(state_db: Path, tmp_path: Path):
    """镜像列说 failed/9，权威行说 passed/2 —— getter 读权威行。"""
    plan_dir = _plan_dir(tmp_path, "conflict")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "stale_reason",
        },
    )
    _seed_verification_row(
        "conflict", status="passed", round_n=2, max_rounds=3,
    )

    ps = PlanState(plan_dir)

    assert ps.get_verification_status() == "passed"
    assert ps.get_verification_round() == 2
    assert ps.get_verification_max_rounds() == 3
    assert ps.get_verification_stop_reason() is None


def test_stop_reason_comes_from_the_authoritative_row(
    state_db: Path, tmp_path: Path,
):
    """停止原因也是权威行的值 —— 镜像列里的旧理由不算数。"""
    plan_dir = _plan_dir(tmp_path, "stop-reason")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "stale_reason",
        },
    )
    _seed_verification_row(
        "stop-reason",
        status="loop_stopped",
        round_n=3,
        max_rounds=4,
        stop_reason="max_rounds_reached",
    )

    ps = PlanState(plan_dir)

    assert ps.get_verification_status() == "loop_stopped"
    assert ps.get_verification_stop_reason() == "max_rounds_reached"


# ---------------------------------------------------------------------------
# TDD spec 2: 没有权威行 → 与 /status 端点一致的默认值
# ---------------------------------------------------------------------------


def test_missing_row_defaults_match_status_endpoint(
    state_db: Path, tmp_path: Path,
):
    """没有 ``plan_verification`` 行 → not_started / 0 / 4 / None。"""
    plan_dir = _plan_dir(tmp_path, "no-row")
    _seed_routing_row(plan_dir)

    ps = PlanState(plan_dir)

    assert ps.get_verification_status() == "not_started"
    assert ps.get_verification_round() == 0
    assert ps.get_verification_max_rounds() == 4
    assert ps.get_verification_stop_reason() is None


def test_default_max_rounds_matches_the_server_constant():
    """``plan_state`` 的默认值必须与 ``server`` 的常量一致。"""
    import server

    assert plan_state_module.DEFAULT_MAX_VERIFICATION_ROUNDS == 4
    assert (
        plan_state_module.DEFAULT_MAX_VERIFICATION_ROUNDS
        == server.DEFAULT_MAX_VERIFICATION_ROUNDS
    )


def test_a_fresh_plan_without_any_row_reports_the_documented_defaults(
    state_db: Path, tmp_path: Path,
):
    """什么都没有的计划 → 与 /status 端点同一套默认值。

    「第一次读」与「第二次读」必须给同一个答案 —— 新建计划时读的是
    ``_default_state()``，第二次读走的是 SQLite 行；两者不一致的话，
    getter 就会先说 ``pending``、后说 ``not_started``。
    """
    plan_dir = _plan_dir(tmp_path, "fresh")

    first = PlanState(plan_dir)
    second = PlanState(plan_dir)

    assert first.get_verification_status() == "not_started"
    assert first.get_verification_round() == 0
    assert first.get_verification_max_rounds() == 4
    assert first.get_verification_stop_reason() is None

    assert second.get_verification_status() == first.get_verification_status()
    assert second.get_verification_round() == first.get_verification_round()
    assert (
        second.get_verification_max_rounds()
        == first.get_verification_max_rounds()
    )
    assert second.get_verification_stop_reason() is None


# ---------------------------------------------------------------------------
# TDD spec 3: baseline 仍然从镜像列解出
# ---------------------------------------------------------------------------


def test_baseline_survives(state_db: Path, tmp_path: Path):
    """``baseline`` / ``baseline_doc_sha256`` 原样保留 —— 它们只存在列里。"""
    baseline = {
        "repo_dir": "/tmp/some-repo",
        "ref": "main",
        "commit_sha": "abc123",
        "pr_number": 7,
    }
    plan_dir = _plan_dir(tmp_path, "baseline")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": None,
            "baseline": baseline,
            "baseline_doc_sha256": "deadbeef",
        },
    )
    _seed_verification_row(
        "baseline", status="passed", round_n=2, max_rounds=3,
    )

    ps = PlanState(plan_dir)
    block = ps.get_state()["verification"]

    assert block["baseline"] == baseline
    assert block["baseline_doc_sha256"] == "deadbeef"
    # 四个生命周期键仍然来自权威行 —— baseline 的存活不改变来源。
    assert block["status"] == "passed"
    assert block["round"] == 2


def test_no_baseline_in_the_column_means_no_baseline_key(
    state_db: Path, tmp_path: Path,
):
    """列里没有 baseline → 状态里也不凭空多一个 baseline 键。"""
    plan_dir = _plan_dir(tmp_path, "no-baseline")
    _seed_routing_row(plan_dir)
    _seed_verification_row(
        "no-baseline", status="passed", round_n=1, max_rounds=3,
    )

    ps = PlanState(plan_dir)
    block = ps.get_state()["verification"]

    assert "baseline" not in block
    assert "baseline_doc_sha256" not in block


def test_corrupt_column_json_keeps_the_status_from_the_repository(
    state_db: Path, tmp_path: Path,
):
    """列里的 JSON 坏了 → baseline 视为缺失，状态仍然取权威行。"""
    plan_dir = _plan_dir(tmp_path, "corrupt")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": None,
            "baseline": {"repo_dir": "/tmp/some-repo"},
        },
    )
    _seed_verification_row(
        "corrupt", status="passed", round_n=2, max_rounds=3,
    )
    _corrupt_verification_column("corrupt")

    ps = PlanState(plan_dir)
    block = ps.get_state()["verification"]

    assert ps.get_verification_status() == "passed"
    assert ps.get_verification_round() == 2
    assert "baseline" not in block, "坏 JSON 不得解出基线"


# ---------------------------------------------------------------------------
# TDD spec 4: 单参调用保持旧签名可运行
# ---------------------------------------------------------------------------


def test_single_arg_signature_still_works():
    """``_sqlite_row_to_state(row)`` 单参调用不抛，且不含镜像的四个状态键。"""
    row: dict[str, Any] = {
        "plan_id": "solo",
        "current_phase": "verification_running",
        "completed_phases": "[]",
        "review_rounds": "{}",
        "flags": "{}",
        "verification": json.dumps(
            {
                "status": "failed",
                "round": 9,
                "max_rounds": 9,
                "stop_reason": "stale_reason",
            }
        ),
        "last_updated": "2026-11-04T00:00:00Z",
    }

    state = PlanState._sqlite_row_to_state(row)

    assert state["plan_id"] == "solo"
    assert state["current_phase"] == "verification_running"
    block = state["verification"]
    assert block["status"] != "failed", "镜像列的状态不再是状态的源"
    assert block["round"] != 9
    assert block["max_rounds"] != 9
    assert block["stop_reason"] != "stale_reason"


def test_single_arg_call_defaults_to_the_documented_values():
    """单参调用 → 四个键是默认值（不是镜像值）。"""
    row: dict[str, Any] = {
        "plan_id": "solo-defaults",
        "current_phase": "executing",
        "completed_phases": "[]",
        "review_rounds": "{}",
        "flags": "{}",
        "verification": json.dumps(
            {"status": "failed", "round": 9, "max_rounds": 9, "stop_reason": None}
        ),
        "last_updated": None,
    }

    block = PlanState._sqlite_row_to_state(row)["verification"]

    assert block["status"] == "not_started"
    assert block["round"] == 0
    assert block["max_rounds"] == plan_state_module.DEFAULT_MAX_VERIFICATION_ROUNDS
    assert block["stop_reason"] is None


# ---------------------------------------------------------------------------
# 权威行必须真的被写进去 —— 否则读权威行读到的是一片空白
# ---------------------------------------------------------------------------


def test_phase_transition_writes_the_authoritative_row(
    state_db: Path, tmp_path: Path,
):
    """相位转换同时写 ``plan_verification`` —— 读权威行才有东西可读。"""
    plan_dir = _plan_dir(tmp_path, "transition-write")
    _seed_routing_row(plan_dir, phase="verification_running")

    ps = PlanState(plan_dir)
    ps.transition_to("verification_passed")

    row = _verification_row("transition-write")
    assert row is not None, "权威表里必须有这个计划的行"
    assert row["verification_status"] == "passed"
    assert ps.get_verification_status() == "passed"


def test_the_legacy_file_migration_seeds_the_authoritative_row(
    state_db: Path, tmp_path: Path,
):
    """旧 ``plan_state.json`` 里的验证状态要迁进权威表。

    迁移之前，验证状态只住在那个文件里；读路径改读权威表之后，
    不迁移就等于把这个计划的状态清零（变成 not_started / round 0）。
    """
    plan_dir = _plan_dir(tmp_path, "legacy-migrate")
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": "legacy-migrate",
                "current_phase": "verification_running",
                "completed_phases": ["execution"],
                "review_rounds": {},
                "flags": {},
                "verification": {
                    "status": "failed",
                    "round": 2,
                    "max_rounds": 3,
                    "stop_reason": "max_rounds_reached",
                },
            }
        ),
        encoding="utf-8",
    )

    ps = PlanState(plan_dir)

    assert ps.get_verification_status() == "failed"
    assert ps.get_verification_round() == 2
    assert ps.get_verification_stop_reason() == "max_rounds_reached"

    row = _verification_row("legacy-migrate")
    assert row is not None, "迁移必须建出权威行"
    assert row["verification_status"] == "failed"
    assert row["round"] == 2
    assert row["verification_stop_reason"] == "max_rounds_reached"


# ---------------------------------------------------------------------------
# TDD spec 5: 读路径是纯读 —— 镜像列不再是状态的源，读也不写库
# ---------------------------------------------------------------------------
#
# 老计划确实把停止理由只住在镜像列里（基线提交 ``aabce578`` 的
# ``backend/verification_loop.py`` 用裸 SQL 写的就是那一列）。把这件事做成
# 「读路径的兜底」有两个后果：
#
# 1. 读一次就写一次权威行 —— 同一个 plan 因读序不同给出两个答案
#    （VP-015 (c)）；
# 2. 镜像列又变成了状态的源 —— 那一列的写入者早就退休了。
#
# 所以迁移改由运维显式触发（``scripts/migrate_legacy_verification_stop_reason.py``），
# 读路径只读权威行。


def test_the_mirror_stop_reason_is_not_a_source_for_the_read_path(
    state_db: Path, tmp_path: Path,
):
    """镜像列里的停止理由不是读路径的源 —— 读路径只读权威行。"""
    plan_dir = _plan_dir(tmp_path, "mirror-stop-reason")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "MIRROR_POISON",
        },
    )
    assert _verification_row("mirror-stop-reason") is None, (
        "前提：权威表里没有这个计划的行"
    )

    ps = PlanState(plan_dir)

    assert ps.get_verification_stop_reason() is None, (
        "镜像列的停止理由不得成为读路径的答案"
    )
    assert ps.get_verification_status() == "not_started"
    assert ps.get_verification_round() == 0
    assert ps.get_verification_max_rounds() == 4
    assert _verification_row("mirror-stop-reason") is None, (
        "读路径不得写库：镜像列的停止理由不得被读成一行权威行"
    )


def test_the_read_path_writes_nothing(
    state_db: Path, tmp_path: Path,
):
    """连着读两次：权威表不建行，路由行逐字节不变。"""
    plan_dir = _plan_dir(tmp_path, "pure-read")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "MIRROR_POISON",
        },
    )
    before = _routing_row("pure-read")
    assert before is not None

    PlanState(plan_dir).get_state()
    PlanState(plan_dir).get_state()

    assert _routing_row("pure-read") == before, (
        "读状态不得改写路由行（包括镜像列）"
    )
    assert _verification_row("pure-read") is None, (
        "读状态不得建出权威行"
    )


def test_two_read_orders_give_the_same_answer(
    state_db: Path, tmp_path: Path,
):
    """读序 A（先读后看库）与读序 B（先看库后读）给出完全相同的答案。"""
    plan_dir = _plan_dir(tmp_path, "read-order")
    _seed_routing_row(
        plan_dir,
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "MIRROR_POISON",
        },
    )

    # 读序 A：先读，再看库
    order_a = PlanState(plan_dir).get_state()["verification"]
    db_after_a = _verification_row("read-order")

    # 读序 B：先看库，再读
    db_before_b = _verification_row("read-order")
    order_b = PlanState(plan_dir).get_state()["verification"]

    assert db_after_a is None, "读序 A 不得建出权威行"
    assert db_before_b is None, "读序 B 不得建出权威行"

    for key in ("status", "round", "max_rounds", "stop_reason"):
        assert order_a[key] == order_b[key], (
            f"两种读序在 {key} 上答案不同："
            f"A={order_a[key]!r} B={order_b[key]!r}"
        )

    assert order_a == {
        "status": "not_started",
        "round": 0,
        "max_rounds": 4,
        "stop_reason": None,
    }
    assert order_b == order_a
    for value in (*order_a.values(), *order_b.values()):
        assert value not in ("failed", 9, "MIRROR_POISON"), (
            f"镜像列的值不得出现在答案里；got {value!r}"
        )


# ---------------------------------------------------------------------------
# TDD spec 6: 存在性判据是「行在不在」，不是「行读得出来吗」
# ---------------------------------------------------------------------------
#
# 旧文件迁移的守卫问的是「权威行在不在」。``_read_verification_row`` 的
# 既定契约是「读不出来 = 没有行、绝不抛」（读路径的兜底语义），拿它当
# 存在性判据就会在行**存在但解不开**时答「没有行」——
# 而 ``plan_verification`` 的 JSON 列逐列 ``json.loads``，一个坏 blob
# 就让整行读不出来。
#
# 守卫答错的方向是不安全的那一侧：答「没有行」→ 迁移把旧文件的值写进去，
# 权威记录被覆盖，且不可恢复。


def _legacy_plan_dir(tmp_path: Path, plan_id: str, verification: dict) -> Path:
    """造一个只有旧 ``plan_state.json`` 的计划目录。"""
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "verification_running",
                "completed_phases": ["execution"],
                "review_rounds": {},
                "flags": {},
                "verification": verification,
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def test_the_legacy_migration_does_not_overwrite_an_unreadable_authority_row(
    state_db: Path, tmp_path: Path,
):
    """既有权威行读不出来时，旧文件迁移也不许覆盖它。

    守卫必须答「行在」而不是「没有行」：行在，就归它管。
    """
    plan_dir = _legacy_plan_dir(
        tmp_path,
        "legacy-must-not-clobber",
        {
            "status": "running",
            "round": 2,
            "max_rounds": 7,
            "stop_reason": "legacy_reason",
        },
    )
    _seed_verification_row(
        "legacy-must-not-clobber",
        status="passed",
        round_n=3,
        max_rounds=9,
        stop_reason="authoritative_reason",
    )
    _corrupt_authority_json_column("legacy-must-not-clobber")

    PlanState(plan_dir).get_state()

    assert _raw_lifecycle("legacy-must-not-clobber") == (
        "passed",
        3,
        9,
        "authoritative_reason",
    ), "既有权威行不得被旧文件迁移覆盖（哪怕它读不出来）"


def test_the_legacy_migration_fails_closed_when_the_probe_cannot_answer(
    state_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """探测答不出来时不迁移 —— 宁可漏种，不可覆盖。

    覆盖既有权威行不可恢复；漏种可以再跑一次迁移补上。
    """
    plan_dir = _legacy_plan_dir(
        tmp_path,
        "legacy-probe-unknown",
        {
            "status": "running",
            "round": 2,
            "max_rounds": 7,
            "stop_reason": "legacy_reason",
        },
    )

    monkeypatch.setattr(
        plan_state_module,
        "_verification_row_exists",
        lambda *args, **kwargs: None,
    )

    PlanState(plan_dir).get_state()

    assert _raw_lifecycle("legacy-probe-unknown") is None, (
        "探测答不出来时不得迁移（fail closed）"
    )


def test_the_existence_probe_answers_presence_not_readability(
    state_db: Path,
):
    """存在性探测答的是「行在不在」，不是「行读得出来吗」。

    这正是本次修复的核心判据：``plan_verification`` 的 JSON 列逐列
    ``json.loads``，一个坏 blob 就让整行读不出来 —— 但行仍然**存在**。
    探测一旦依赖解码，就会把「行存在但读不出来」答成「没有行」，迁移
    随后用旧文件的值覆盖既有权威行。
    """
    assert plan_state_module._verification_row_exists("probe-contract") is False, (
        "没有行时探测必须答 False"
    )

    _seed_verification_row(
        "probe-contract", status="passed", round_n=1, max_rounds=3,
    )
    assert plan_state_module._verification_row_exists("probe-contract") is True, (
        "有行时探测必须答 True"
    )

    _corrupt_authority_json_column("probe-contract")
    assert plan_state_module._verification_row_exists("probe-contract") is True, (
        "行解不开也是「行存在」—— 探测不得依赖解码"
    )
