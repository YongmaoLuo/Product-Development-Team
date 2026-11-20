"""基线只走窄写入口 ``write_verification_baseline``。

背景
----
``plan_routing.verification`` 是验证状态的**镜像列**；验证状态的权威存储是
``plan_verification`` 表（arch 决策点 1）。arch 决策点 3 要求删掉镜像写路径
—— ``RoutingRepository.write_plan_state`` 不再整块重写这一列。列里只剩两件事：
冻结的基线，以及它被冻结时那份文档的字节哈希。

本文件钉住改道之后的行为：

* ``write_plan_state`` 的签名里已经没有 ``verification``；
* 元数据写（``set_verification_max_rounds`` / ``enable_arch``）**逐字节**不动
  这一列 —— 镜像列不是 ``write_plan_state`` 的属主；
* 基线写只覆盖 ``baseline`` / ``baseline_doc_sha256`` 两个键，列里其它键
  （历史遗留的 ``status`` / ``round`` …）原样保留；
* 路由行不存在时建行，基线不被静默丢弃；
* 列里的 JSON 坏了 → 当作空 dict 写回，不抛。

测试数据
--------
真实的 ``plan_routing`` 行 + 真实的 ``plan_verification`` 行：计划状态用
:class:`RoutingRepository` 写，验证状态用 :class:`VerificationRepository` 写 ——
与生产路径同一条路。镜像列里那点历史遗留内容用一次直接 UPDATE 铺进去：
改道之后 ``write_plan_state`` 不再拥有这一列（这正是本文件要钉住的事实）。
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import PlanState  # noqa: E402


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份自己的状态库。"""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """一个空计划目录（还没有任何产物）。"""
    path = tmp_path / "plan-baseline-write"
    path.mkdir()
    return path


#: 镜像列里那点历史遗留内容：验证状态早已搬到权威表，列里的这份是旧的。
LEGACY_MIRROR = {"status": "failed", "round": 9}


def _conn():
    """打开状态库（已迁移）。"""
    import sqlite3

    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(resolve_state_db_path())
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _seed_routing_row(
    plan_dir: Path,
    *,
    phase: str = "executing",
    mirror: Optional[dict] = None,
) -> None:
    """按生产写路径写 ``plan_routing`` 行。

    镜像列**不**经过 ``write_plan_state``：改道之后它不再是那个方法的属主，
    所以这里用一次直接 UPDATE 把历史遗留内容铺进去。
    """
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _conn()
    try:
        RoutingRepository(conn).write_plan_state(
            plan_dir.name,
            phase=phase,
            completed_phases=["execution"],
            review_rounds={},
            flags={},
            last_updated=None,
        )
        if mirror is not None:
            conn.execute(
                "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
                (json.dumps(mirror), plan_dir.name),
            )
    finally:
        conn.close()


def _seed_verification_row(
    plan_id: str,
    *,
    status: str,
    round_n: int = 0,
    max_rounds: int = 3,
) -> None:
    """写 ``plan_verification`` 行（权威表）。"""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _conn()
    try:
        VerificationRepository(conn).insert(
            plan_id,
            status,
            round=round_n,
            max_rounds=max_rounds,
        )
    finally:
        conn.close()


def _routing_row(plan_id: str) -> Optional[dict[str, Any]]:
    """读 ``plan_routing`` 行（没有行 → ``None``）。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?", (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
    finally:
        conn.close()


def _mirror_column(plan_id: str) -> Optional[str]:
    """读镜像列的**原始文本**（逐字节比较用）。"""
    row = _routing_row(plan_id)
    if row is None:
        return None
    return row["verification"]


def _delete_routing_row(plan_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,))
    finally:
        conn.close()


def _set_raw_mirror(plan_id: str, raw: str) -> None:
    """把镜像列写成给定的原始文本（坏 JSON 也走这里）。"""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (raw, plan_id),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. write_plan_state 不再拥有镜像列
# ---------------------------------------------------------------------------


def test_write_plan_state_has_no_verification_parameter() -> None:
    """签名里没有 ``verification`` —— 镜像列不再是它的参数。"""
    from state_machine.repositories.routing_repository import RoutingRepository

    params = inspect.signature(RoutingRepository.write_plan_state).parameters

    assert "verification" not in params, (
        f"write_plan_state 不得再接收镜像列；got {list(params)}"
    )


def test_metadata_write_leaves_column_unchanged(
    plan_dir: Path, state_db: Path,
) -> None:
    """元数据写逐字节不动镜像列。

    权威行与镜像列**故意**不一致（``passed``/2 对 ``failed``/9）：镜像列是
    旧记录，改道之后它不再被 ``write_plan_state`` 整块重写，所以这两份数据
    各自保持原样。
    """
    _seed_routing_row(plan_dir, phase="executing", mirror=dict(LEGACY_MIRROR))
    _seed_verification_row(plan_dir.name, status="passed", round_n=2)

    before = _mirror_column(plan_dir.name)
    assert before is not None, "先得有镜像列的内容可比"

    state = PlanState(plan_dir)
    state.set_verification_max_rounds(5)
    state.enable_arch()

    assert _mirror_column(plan_dir.name) == before, (
        "元数据写不得改镜像列；"
        f"before={before!r} after={_mirror_column(plan_dir.name)!r}"
    )


# ---------------------------------------------------------------------------
# 2. 基线写：只覆盖两个键
# ---------------------------------------------------------------------------


def test_baseline_write_preserves_other_keys(
    plan_dir: Path, state_db: Path,
) -> None:
    """基线写只覆盖 ``baseline`` / ``baseline_doc_sha256``，其它键原样保留。"""
    _seed_routing_row(plan_dir, phase="executing", mirror=dict(LEGACY_MIRROR))
    _seed_verification_row(plan_dir.name, status="passed", round_n=2)

    baseline = {
        "repo_dir": "/tmp/repo",
        "ref": "main",
        "commit_sha": "abc",
        "pr_number": 7,
    }
    PlanState(plan_dir).set_verification_baseline(baseline, "sha256:x")

    column = json.loads(_mirror_column(plan_dir.name))
    assert column == {
        **LEGACY_MIRROR,
        "baseline": baseline,
        "baseline_doc_sha256": "sha256:x",
    }, f"got {column!r}"


def test_baseline_write_creates_missing_row(
    plan_dir: Path, state_db: Path,
) -> None:
    """路由行不存在 → 建行，基线不丢。"""
    state = PlanState(plan_dir)
    _delete_routing_row(plan_dir.name)
    assert _routing_row(plan_dir.name) is None, "先确认行真的没了"

    baseline = {
        "repo_dir": "/tmp/repo",
        "ref": "main",
        "commit_sha": "abc",
        "pr_number": 7,
    }
    state.set_verification_baseline(baseline)

    row = _routing_row(plan_dir.name)
    assert row is not None, "行不存在时要建行，基线不许被静默丢弃"
    assert json.loads(row["verification"])["baseline"] == baseline
    assert PlanState(plan_dir).get_verification_baseline() == baseline


def test_corrupted_mirror_column_is_replaced_not_raised(
    plan_dir: Path, state_db: Path,
) -> None:
    """列里的 JSON 坏了 → 当作空 dict 写回，不抛。"""
    _seed_routing_row(plan_dir, phase="executing")
    _set_raw_mirror(plan_dir.name, "{not json at all")

    baseline = {
        "repo_dir": "/tmp/repo",
        "ref": "main",
        "commit_sha": "abc",
        "pr_number": 7,
    }
    PlanState(plan_dir).set_verification_baseline(baseline, "sha256:x")

    column = json.loads(_mirror_column(plan_dir.name))
    assert column == {
        "baseline": baseline,
        "baseline_doc_sha256": "sha256:x",
    }, f"got {column!r}"


# ---------------------------------------------------------------------------
# 3. 窄写不得越界：别的列一律不动
# ---------------------------------------------------------------------------


def test_baseline_write_leaves_the_other_plan_state_columns_alone(
    plan_dir: Path, state_db: Path,
) -> None:
    """基线写不动 ``completed_phases`` / ``review_rounds`` / ``flags``。

    这三列的属主是 ``write_plan_state``；窄写只碰镜像列，不越界。
    """
    _seed_routing_row(plan_dir, phase="executing", mirror=dict(LEGACY_MIRROR))
    before = _routing_row(plan_dir.name)

    PlanState(plan_dir).set_verification_baseline(
        {"repo_dir": "/tmp/repo", "ref": "main", "commit_sha": "abc"},
        "sha256:x",
    )

    after = _routing_row(plan_dir.name)
    for column in ("completed_phases", "review_rounds", "flags"):
        assert after[column] == before[column], (
            f"{column} 不是基线写的属主，不得被改动；"
            f"before={before[column]!r} after={after[column]!r}"
        )


def test_created_row_uses_the_schema_defaults(
    plan_dir: Path, state_db: Path,
) -> None:
    """建行沿用 ``insert`` 的默认：相位 ``interview``、``version=0``、无 substage。"""
    state = PlanState(plan_dir)
    _delete_routing_row(plan_dir.name)

    state.set_verification_baseline(
        {"repo_dir": "/tmp/repo", "ref": "main", "commit_sha": "abc"},
    )

    row = _routing_row(plan_dir.name)
    assert row is not None
    assert row["current_phase"] == "interview", f"got {row['current_phase']!r}"
    assert row["version"] == 0, f"got {row['version']!r}"
    assert row["substage"] is None, f"got {row['substage']!r}"
