"""verdict 的**轮次作用域** —— 结论只对本轮有效（2026-11-05）。

背景
----
``plan_verification.verdicts`` 里的条目不带轮次：装载时按
``{v["vp_id"]: v}`` 折叠，同一个 id 的历史轮结论与本轮结论挤在一张表里，
后写的覆盖先写的 —— 于是「本轮真跑出来的结论」与「上一轮留下来的结论」
在内存里长得一模一样。读侧（Phase-2 关卡 / 报告 / 卡片）没有办法只取
本轮那一份，于是上一轮的 PASSED 能替本轮说话（实测：本轮 VP-001 真跑
PASSED，关卡却按上一轮那份 FAILED 判）。

契约（三条）
------------
1. **写侧盖戳**：每条裁决都带一个 ``round``（``int``）。轮次从状态库读
   （``_current_round()``），调用方显式给了轮次（``round_no``）时以它为准。
2. **读侧只取本轮**：``_current_round_verdicts()`` 给本轮那一份；本轮
   没有的验证点**不回落**到历史轮 —— 回落等于「上一轮的结论替本轮说话」。
3. **轮次未知时才有兜底**：状态库说「不知道」（0）→ 写 ``round=0``；
   这时读侧才用「最大轮」兜底。轮次已知时**不**兜底。

边界条件
--------
* 轮次未知（``_current_round()`` 返回 0）→ 写入 ``round=0``；
* 同一个 VP **同一轮**重复写 → 后者覆盖前者；
* 同一个 VP **不同轮** → 两条都保留；
* 旧数据没有 ``round`` 字段 → 视为 0，条目本身**不丢**。

TDD contract —— 4 个必测用例
----------------------------
1. ``test_record_verdict_stamps_round`` —— 写侧盖戳（内存 / 收集视图 / 落库三处一致）。
2. ``test_load_keeps_round_from_db`` —— 装载时保留 DB 里的轮次；旧数据当 0 且不丢。
3. ``test_current_round_verdicts_excludes_previous_round`` —— 本轮视图不含上一轮。
4. ``test_current_round_verdicts_falls_back_when_round_unknown`` —— 轮次未知才兜底。

测试命令::

    pytest backend/tests/unit/verification/test_verdict_round_scoping.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ``backend/`` 进 sys.path，``import verification_executor`` 才成立 ——
# 与仓库里其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)
from verification_executor import VerificationExecutor  # noqa: E402


pytestmark = pytest.mark.unit

#: 计划 id（每个用例一份独立的状态库，所以 id 相同也不会串行）。
PLAN_ID = "vp-round-scoping"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _plan(*vp_ids: str) -> Dict[str, Any]:
    """一份只含给定验证点的计划。

    计划**必须**定义这些 id —— 计划没定义的 id，它的裁决会被当成孤儿
    隔离掉（``_quarantine_orphan_verdicts``），那测的就不是轮次作用域了。
    """
    return {
        "plan_id": PLAN_ID,
        "vps": [
            {
                "id": vp_id,
                "title": f"验证点 {vp_id}",
                "related_prd_criteria": ["AC-001"],
                "verification_method": "api_test",
                "verification_phase": 1,
                "priority": "medium",
                "expected_result": "通过",
            }
            for vp_id in vp_ids
        ],
        "criteria": [{"id": "AC-001", "text": "系统可用"}],
    }


def _verdict(status: str = "PASSED", **extra: Any) -> Dict[str, Any]:
    """一条**合乎 schema** 的裁决（``status`` / ``reasons`` / ``evidence``）。"""
    payload: Dict[str, Any] = {
        "status": status,
        "reasons": ["ok"],
        "evidence": {},
    }
    payload.update(extra)
    return payload


async def _runner(vp: Dict[str, Any]) -> Dict[str, Any]:
    """本文件不真跑验证点 —— 这个 runner 只是为了让构造器有东西可包。"""
    return _verdict("PASSED")


def _executor(
    tmp_path: Path,
    plan: Dict[str, Any],
    repo: Optional[Any] = None,
    round_no: Optional[int] = None,
) -> VerificationExecutor:
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    return VerificationExecutor(
        verification_plan=plan,
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=_runner,
        verif_repo=repo,
        round_no=round_no,
    )


def _db(tmp_path: Path, round_n: int) -> Any:
    """一份带 ``plan_verification`` 行的状态库（返回 ``(conn, repo)``）。"""
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(PLAN_ID, verification_status="running", round=round_n)
    conn.commit()
    return conn, repo


def _stored(repo: Any) -> List[Dict[str, Any]]:
    """状态库里那条 ``verdicts`` 列表（append-only 的原始形状）。"""
    row = repo.summary(PLAN_ID) or {}
    raw = row.get("verdicts") or []
    return list(raw) if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# 1. 写侧盖戳
# ---------------------------------------------------------------------------


def test_record_verdict_stamps_round(tmp_path: Path) -> None:
    """每条裁决都要带一个轮次号 —— 内存、收集视图、落库三处一致。

    没有轮次戳，「本轮的结论」与「上一轮的结论」在读侧无法区分。
    """
    # (a) 调用方显式给了轮次（``round_no``）—— 以它为准。
    ex = _executor(tmp_path / "explicit", _plan("VP-001"), round_no=2)
    ex.record_verdict("VP-001", _verdict("PASSED"))

    assert ex._verdicts["VP-001"]["round"] == 2
    collected = ex.collect_verdicts()
    assert collected[0]["vp_id"] == "VP-001"
    assert collected[0]["round"] == 2

    # (b) 轮次来自状态库 —— 落库的那一份也得带着，否则重启后轮次就丢了。
    conn, repo = _db(tmp_path / "from-db", 3)
    try:
        ex_db = _executor(tmp_path / "from-db", _plan("VP-001"), repo=repo)
        ex_db.record_verdict("VP-001", _verdict("PASSED"))

        assert ex_db._verdicts["VP-001"]["round"] == 3
        stored = _stored(repo)
        assert stored and stored[-1]["vp_id"] == "VP-001"
        assert stored[-1]["round"] == 3
    finally:
        conn.close()


def test_record_verdict_writes_zero_round_when_unknown(tmp_path: Path) -> None:
    """轮次未知 → 写 ``round=0``（0 是「不知道」，不是「第一轮」）。"""
    ex = _executor(tmp_path, _plan("VP-001"))
    ex.record_verdict("VP-001", _verdict("PASSED"))

    assert ex._verdicts["VP-001"]["round"] == 0


# ---------------------------------------------------------------------------
# 2. 装载时保留轮次
# ---------------------------------------------------------------------------


def test_load_keeps_round_from_db(tmp_path: Path) -> None:
    """装载时保留 DB 里的轮次；旧数据没有 ``round`` → 当 0，且**不丢**。"""
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _verdict("PASSED", vp_id="VP-001", round=2))
        # 旧数据：没有 ``round`` 字段（2026-11-05 之前的落库形状）。
        repo.append_verdict(PLAN_ID, _verdict("FAILED", vp_id="VP-002"))
        conn.commit()

        ex = _executor(tmp_path, _plan("VP-001", "VP-002"), repo=repo)

        assert ex._verdicts["VP-001"]["round"] == 2
        # 旧条目不许被当成垃圾丢掉。
        assert "VP-002" in ex._verdicts
        assert ex._verdicts["VP-002"]["round"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 本轮视图
# ---------------------------------------------------------------------------


def test_current_round_verdicts_excludes_previous_round(tmp_path: Path) -> None:
    """本轮视图只给本轮那一份 —— 上一轮的结论不许替本轮说话。"""
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _verdict("PASSED", vp_id="VP-001", round=1))
        repo.append_verdict(PLAN_ID, _verdict("FAILED", vp_id="VP-002", round=2))
        conn.commit()

        ex = _executor(tmp_path, _plan("VP-001", "VP-002"), repo=repo)

        current = ex._current_round_verdicts()
        assert set(current) == {"VP-002"}
        # 本轮没有的验证点**不回落**到历史轮。
        assert "VP-001" not in current
        # 历史轮那份本身还在（审计痕迹不丢）。
        assert ex._verdicts["VP-001"]["round"] == 1
    finally:
        conn.close()


def test_current_round_verdicts_falls_back_when_round_unknown(tmp_path: Path) -> None:
    """轮次未知（0）时才兜底 —— 兜底取**最大轮**那一份。"""
    conn, repo = _db(tmp_path, 0)
    try:
        repo.append_verdict(PLAN_ID, _verdict("PASSED", vp_id="VP-001", round=1))
        repo.append_verdict(PLAN_ID, _verdict("FAILED", vp_id="VP-002", round=2))
        conn.commit()

        ex = _executor(tmp_path, _plan("VP-001", "VP-002"), repo=repo)

        current = ex._current_round_verdicts()
        assert set(current) == {"VP-002"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. 写侧的其它两条边界
# ---------------------------------------------------------------------------


def test_same_vp_same_round_overwrites(tmp_path: Path) -> None:
    """同一个 VP 同一轮重复写 → 后者覆盖前者（本轮视图里只有一条）。"""
    ex = _executor(tmp_path, _plan("VP-001"), round_no=2)
    ex.record_verdict("VP-001", _verdict("FAILED"))
    ex.record_verdict("VP-001", _verdict("PASSED"))

    assert ex._verdicts["VP-001"]["status"] == "PASSED"
    current = ex._current_round_verdicts()
    assert list(current) == ["VP-001"]
    assert current["VP-001"]["status"] == "PASSED"


def test_same_vp_in_two_rounds_keeps_both(tmp_path: Path) -> None:
    """同一个 VP 不同轮 → 两条都保留（本轮视图只给本轮那一条）。"""
    ex = _executor(tmp_path, _plan("VP-001"), round_no=1)
    ex.record_verdict("VP-001", _verdict("FAILED"))
    ex._round_no = 2
    ex.record_verdict("VP-001", _verdict("PASSED"))

    # 本轮视图 = 第 2 轮那一条。
    current = ex._current_round_verdicts()
    assert current["VP-001"]["status"] == "PASSED"
    assert current["VP-001"]["round"] == 2

    # 第 1 轮那一条仍在（审计痕迹不丢）。
    assert ex._verdicts_by_round[1]["VP-001"]["status"] == "FAILED"
    assert ex._verdicts_by_round[2]["VP-001"]["status"] == "PASSED"


def test_two_rounds_survive_a_restart(tmp_path: Path) -> None:
    """跨轮的那几条结论得**扛过一次重启**（旧的 state 文件路径）。"""
    ex = _executor(tmp_path, _plan("VP-001"), round_no=1)
    ex.record_verdict("VP-001", _verdict("FAILED"))
    ex._round_no = 2
    ex.record_verdict("VP-001", _verdict("PASSED"))

    # 重新构造执行器 = 重启（装载同一个 plan_dir 里的状态）。
    reloaded = _executor(tmp_path, _plan("VP-001"), round_no=2)

    assert reloaded._verdicts_by_round[1]["VP-001"]["status"] == "FAILED"
    assert reloaded._verdicts_by_round[2]["VP-001"]["status"] == "PASSED"
    current = reloaded._current_round_verdicts()
    assert current["VP-001"]["status"] == "PASSED"
