"""``PlanState`` 的验证 getter 只认权威行 —— 正控与负控。

背景
----
验证状态有两个存储：``plan_verification``（权威表）与 ``plan_routing.verification``
（镜像列）。任务 9 把 ``PlanState`` 的读路径改到权威表，任务 12 把写路径里的镜像
列删掉。任务 5 钉的是 API 层与消费方，缺的那一层是 ``PlanState`` 本身 ——
``PlanState.get_verification_*()`` 是模块内部与调度侧最常读的那一份。

本文件补上这一层：

* **负控** —— 把冲突值写进 ``plan_routing.verification`` 之后，四个 getter
  仍然只报权威行的值（写冲突值前后各构造一次 ``PlanState``，答案不变）；
* **再写一次** —— 冲突值换三个写法，答案仍然不变；
* **正控** —— 跑完一轮正常验证（``complete_round``）之后两边不得分歧，而且
  那次调用**不得**改动路由列（前后逐字节相同）；
* **没有权威行** —— 报文档化的默认值，镜像列里的 ``failed`` 不算数，也不得
  被读成一行权威行；
* **基线** —— 仍然只住在镜像列里，仍能经 ``get_verification_baseline()`` 读回。

测试数据
--------
真实的 ``plan_routing`` 行 + 真实的 ``plan_verification`` 行，与生产路径同一条
路。镜像列用裸 SQL 写 —— 它现在只承载冻结的基线，生产路径上唯一写它的是
``RoutingRepository.write_verification_baseline``；冲突值就是要绕过那个入口
塞进去的东西。

测试命令::

    cd backend && pytest tests/unit/test_plan_state_verification_authority.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import PlanState  # noqa: E402
from verification_plan_review import doc_sha256  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份自己的状态库 —— 绝不碰操作员的库。"""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _plan_dir(tmp_path: Path, plan_id: str) -> Path:
    """计划的目录 —— 目录名就是 plan_id。"""
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _connect():
    """打开状态库（并保证表结构是最新的）。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(resolve_state_db_path())
    migrate(conn)
    return conn


def _seed_routing_row(plan_id: str, *, phase: str = "executing") -> None:
    """按生产写路径建 ``plan_routing`` 行（镜像列留空）。"""
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _connect()
    try:
        RoutingRepository(conn).write_plan_state(
            plan_id,
            phase=phase,
            completed_phases=["execution"],
            review_rounds={},
            flags={},
            last_updated=None,
        )
    finally:
        conn.close()


def _write_mirror_column(plan_id: str, payload: Dict[str, Any]) -> None:
    """把镜像列写成给定的 JSON —— 冲突值就是从这里进去的。"""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (json.dumps(payload), plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mirror_column(plan_id: str) -> Optional[str]:
    """读镜像列的原始文本 —— 用来做逐字节比较。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT verification FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _seed_verification_row(
    plan_id: str,
    *,
    status: str,
    round_n: int = 0,
    max_rounds: int = 3,
    stop_reason: Optional[str] = None,
) -> None:
    """写 ``plan_verification`` 行（权威表）。"""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _connect()
    try:
        VerificationRepository(conn).insert(
            plan_id,
            status,
            round=round_n,
            max_rounds=max_rounds,
            verification_stop_reason=stop_reason,
        )
    finally:
        conn.close()


def _verification_row(plan_id: str) -> Optional[Dict[str, Any]]:
    """读 ``plan_verification`` 行（权威表）。"""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _connect()
    try:
        return VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()


def _getters(ps: PlanState) -> Tuple[str, int, int, Optional[str]]:
    """四个 getter 的答案 —— 一次读完。"""
    return (
        ps.get_verification_status(),
        ps.get_verification_round(),
        ps.get_verification_max_rounds(),
        ps.get_verification_stop_reason(),
    )


# ---------------------------------------------------------------------------
# 1. 负控：冲突的镜像列不改变 getter 的答案
# ---------------------------------------------------------------------------


def test_getters_ignore_conflicting_column(state_db: Path, tmp_path: Path):
    """镜像列说 failed/9，权威行说 passed/2 —— getter 读权威行。

    写冲突值**前后**各构造一次 ``PlanState``：两次的答案必须一样，且等于
    权威行的值。读一次状态不该改写任何东西，所以镜像列的字节也必须原样。
    """
    plan_id = "authority-conflict"
    plan_dir = _plan_dir(tmp_path, plan_id)
    _seed_routing_row(plan_id)
    _seed_verification_row(plan_id, status="passed", round_n=2, max_rounds=3)
    assert state_db.exists(), "用例必须落在自己的临时库里"

    before = _getters(PlanState(plan_dir))
    assert before == ("passed", 2, 3, None), f"权威行的值必须先是对的那一份；got {before!r}"

    _write_mirror_column(
        plan_id, {"status": "failed", "round": 9, "max_rounds": 3},
    )
    mirror_after_write = _mirror_column(plan_id)

    after = _getters(PlanState(plan_dir))

    assert after == before, f"冲突值不得改变 getter 的答案；before={before!r} after={after!r}"
    assert after == ("passed", 2, 3, None), f"got {after!r}"
    assert _mirror_column(plan_id) == mirror_after_write, (
        "读状态不得改写镜像列"
    )


# ---------------------------------------------------------------------------
# 2. 负控：换一种写法再冲突一次，答案仍然不变
# ---------------------------------------------------------------------------


def test_second_conflicting_write_still_ignored(state_db: Path, tmp_path: Path):
    """三个冲突写法连着来，getter 的答案一次都没变。

    只钉一种冲突写法是不够的 —— 那一列是自由格式的 JSON，键的顺序、值的
    类型、``stop_reason`` 在不在都可能不一样。四个 getter 各自都得站得住。
    """
    plan_id = "authority-conflict-2"
    plan_dir = _plan_dir(tmp_path, plan_id)
    _seed_routing_row(plan_id)
    _seed_verification_row(plan_id, status="passed", round_n=2, max_rounds=3)
    assert state_db.exists(), "用例必须落在自己的临时库里"

    expected = ("passed", 2, 3, None)
    assert _getters(PlanState(plan_dir)) == expected

    conflicts = [
        {"status": "failed", "round": 9, "max_rounds": 3},
        {
            "status": "loop_stopped",
            "round": 42,
            "max_rounds": 7,
            "stop_reason": "stale_reason",
        },
        {
            "status": "running",
            "round": 1,
            "max_rounds": 1,
            "stop_reason": "another_stale_reason",
        },
    ]
    for payload in conflicts:
        _write_mirror_column(plan_id, payload)
        got = _getters(PlanState(plan_dir))
        assert got == expected, f"冲突值 {payload!r} 改变了 getter 的答案；got {got!r}"


# ---------------------------------------------------------------------------
# 3. 正控：跑完一轮正常验证，两边不得分歧，且路由列不被写
# ---------------------------------------------------------------------------


def test_positive_control_after_normal_round(state_db: Path, tmp_path: Path):
    """一轮正常验证跑完 → getter 报权威行的值，路由列逐字节不变。

    这一条是正控：负控证明「冲突时不读列」，正控证明「正常时读的是列以外
    的那个来源，并且那一轮**没有**去写列」—— 两边不得分歧。
    """
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_id = "authority-positive"
    plan_dir = _plan_dir(tmp_path, plan_id)
    _seed_routing_row(plan_id)

    # 镜像列里先放一份真实的基线 —— 列不是空的，逐字节比较才有意义。
    baseline = {
        "repo_dir": "/tmp/some-repo",
        "ref": "main",
        "commit_sha": "abc123",
        "pr_number": 7,
    }
    conn = _connect()
    try:
        RoutingRepository(conn).write_verification_baseline(
            plan_id, baseline, doc_sha256="deadbeef",
        )
    finally:
        conn.close()
    before = _mirror_column(plan_id)
    assert before is not None, "镜像列里必须有内容，逐字节比较才不是空转"
    assert state_db.exists(), "用例必须落在自己的临时库里"

    # 一轮正常验证：开一轮 → 结束这一轮。
    conn = _connect()
    try:
        repo = VerificationRepository(conn)
        repo.init_round(plan_id, round_n=2, max_rounds=3)
        repo.complete_round(plan_id, results={}, status="passed")
    finally:
        conn.close()

    after = _mirror_column(plan_id)
    assert after == before, (
        "complete_round 不得改动路由列；"
        f"before={before!r} after={after!r}"
    )

    ps = PlanState(plan_dir)
    row = _verification_row(plan_id)
    assert row is not None, "权威表里必须有这个计划的行"
    assert _getters(ps) == ("passed", 2, 3, None), f"got {_getters(ps)!r}"
    assert ps.get_verification_round() == row["round"], (
        "getter 的 round 必须与仓库里的 round 一致"
    )
    assert ps.get_verification_max_rounds() == row["max_rounds"], (
        "getter 的 max_rounds 必须与仓库里的 max_rounds 一致"
    )
    assert row["verification_status"] == "passed"


# ---------------------------------------------------------------------------
# 4. 没有权威行 → 文档化的默认值；基线仍能读回
# ---------------------------------------------------------------------------


def test_missing_row_defaults_and_baseline(state_db: Path, tmp_path: Path):
    """没有 ``plan_verification`` 行 → not_started/0/4/None，基线仍读得回。

    镜像列里写着 ``failed``，但那一列不是状态的源：没有权威行就是
    「从没跑过一轮」，答案是文档化的默认值。基线是另一回事 —— 它只住在列里，
    仍然要能读回来。
    """
    plan_id = "authority-no-row"
    plan_dir = _plan_dir(tmp_path, plan_id)
    _seed_routing_row(plan_id)
    assert state_db.exists(), "用例必须落在自己的临时库里"

    doc_bytes = b"# verification plan\n\nbaseline: main\n"
    (plan_dir / "verification-plan.md").write_bytes(doc_bytes)
    baseline = {
        "repo_dir": "/tmp/some-repo",
        "ref": "main",
        "commit_sha": "abc123",
        "pr_number": 7,
    }
    _write_mirror_column(
        plan_id,
        {
            "status": "failed",
            "round": 9,
            "max_rounds": 7,
            "baseline": baseline,
            "baseline_doc_sha256": doc_sha256(doc_bytes),
        },
    )

    ps = PlanState(plan_dir)

    assert _getters(ps) == ("not_started", 0, 4, None), (
        f"没有权威行 → 文档化的默认值；got {_getters(ps)!r}"
    )
    assert _verification_row(plan_id) is None, (
        "镜像列不得被读成一行权威行"
    )
    assert ps.get_verification_baseline() == baseline, (
        f"基线仍要读得回来；got {ps.get_verification_baseline()!r}"
    )