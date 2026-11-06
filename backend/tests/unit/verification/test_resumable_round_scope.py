"""执行器按**本轮 + 四条判据**决定复用（2026-11-05）。

背景
----
``_resumable_completed_ids`` 只按 id 判断"已经跑过"：上一轮 PASSED 的验证点
在这一轮被当成已完成而跳过（实测 27 个验证点只跑了 7 个，却报出 27 条结果）。
索引桶同样混入历史轮 —— 上一轮的结论在内存里与本轮真跑出来的长得一模一样。

复用不是不能做，但**必须说得出凭什么**。那"凭什么"是四条判据（判据内核在
:mod:`verification_round_scope`，执行器与编排器共用同一份口径）：

1. 上一轮该验证点是 ``PASSED``；
2. 该验证点声明了非空的范围（``covers_paths``）；
3. 本轮变更集与那个范围**不相交**；
4. 本轮变更集不碰任何**共享面**。

默认是"本轮真跑" —— 复用是例外，例外必须能被那四条复核出来。

本文件钉住执行器这一侧的七条：

1. ``test_historical_passed_not_reused_by_default`` — 默认不复用（没有本轮的
   轮次作用域记录 = 不知道变了什么 → 全量重跑）。
2. ``test_reuse_when_disjoint`` — 变更集与范围不相交 → 复用。
3. ``test_rerun_when_touched`` — 变更集落在范围里 → 重跑。
4. ``test_shared_surface_forces_full_rerun`` — 共享面命中 → 全量重跑。
5. ``test_reused_marker_recorded`` — 复用项带 ``reused_from_round``。
6. ``test_reused_verdict_restamped_to_current_round`` — 复用项以**本轮**的轮次号
   重新写入（否则本轮视图会把它当历史轮排除）。
7. ``test_definition_fingerprint_still_enforced`` — 同轮内恢复仍要过既有
   定义指纹校验（不得放宽）。

测试命令::

    cd backend && python3 -m pytest tests/unit/verification/test_resumable_round_scope.py -v
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
from verification_executor import (  # noqa: E402
    REUSED_FROM_ROUND_FIELD,
    VerificationExecutor,
    definition_fingerprint_for_vp,
)
from verification_round_scope import record_round_scope  # noqa: E402


pytestmark = pytest.mark.unit

#: 计划 id（每个用例一份独立的状态库，所以 id 相同也不会串行）。
PLAN_ID = "resumable-round-scope"

#: 上一轮那条结论负责的范围（目录前缀）。
COVERS = ("backend/verification/",)

#: 本轮改到的文件 —— 与 :data:`COVERS` **不相交**。
DISJOINT_CHANGES = ("backend/server.py",)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _plan(*vp_ids: str, covers: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """一份只含给定验证点的计划。

    计划**必须**定义这些 id —— 计划没定义的 id，它的裁决会被当成孤儿隔离掉
    （``_quarantine_orphan_verdicts``），那测的就不是轮次作用域了。
    """
    scope = covers or {}
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
                "covers_paths": list(scope.get(vp_id, [])),
            }
            for vp_id in vp_ids
        ],
        "criteria": [{"id": "AC-001", "text": "系统可用"}],
    }


def _plan_dir(tmp_path: Path) -> Path:
    """这个计划的目录（轮次作用域记录也落在这里）。"""
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


async def _runner(vp: Dict[str, Any]) -> Dict[str, Any]:
    """本文件不真跑验证点 —— 这个 runner 只是为了让构造器有东西可包。"""
    return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}


def _executor(
    plan_dir: Path,
    plan: Dict[str, Any],
    repo: Optional[Any] = None,
    round_no: Optional[int] = None,
) -> VerificationExecutor:
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


def _previous_verdict(
    vp_id: str,
    plan: Dict[str, Any],
    *,
    status: str = "PASSED",
    round_no: int = 1,
) -> Dict[str, Any]:
    """上一轮（默认第 1 轮）留下的一条结论。

    带着三样东西：轮次号、它负责的范围（``covers_paths``）、以及产生它的那份
    **定义指纹** —— 指纹对得上时这一轮才可能跳过它。
    """
    vp = next(v for v in plan["vps"] if v["id"] == vp_id)
    return {
        "vp_id": vp_id,
        "status": status,
        "reasons": ["ok"],
        "evidence": {},
        "round": round_no,
        "covers_paths": list(vp.get("covers_paths") or []),
        "definition_fingerprint": definition_fingerprint_for_vp(vp),
    }


def _write_scope(
    plan_dir: Path,
    round_no: int,
    changed: Any = (),
    *,
    head_sha: str = "sha-of-this-round",
    prev_head_sha: Optional[str] = "sha-of-previous-round",
) -> None:
    """记下某一轮的轮次作用域（HEAD / 上一轮 HEAD / 变更集）。"""
    record_round_scope(
        plan_dir,
        round_no,
        head_sha=head_sha,
        prev_head_sha=prev_head_sha,
        changed=changed,
    )


# ---------------------------------------------------------------------------
# 1. 默认不复用
# ---------------------------------------------------------------------------


def test_historical_passed_not_reused_by_default(tmp_path: Path) -> None:
    """上一轮的 PASSED 默认**不**复用。

    没有本轮的轮次作用域记录 = 不知道这一轮相对上一轮改了什么 = 全量重跑
    （安全的一侧）。复用是例外，例外必须能被那四条判据复核出来。
    """
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        # **没有**作用域记录 —— 变更集未知。
        ex = _executor(plan_dir, plan, repo=repo)

        assert ex._seed_reused_verdicts(1) == set()
        assert "VP-001" not in ex._resumable_completed_ids(plan["vps"])
        # 本轮视图里也没有它 —— 上一轮的结论不替本轮说话。
        assert ex._current_round_verdicts() == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 变更集与范围不相交 → 复用
# ---------------------------------------------------------------------------


def test_reuse_when_disjoint(tmp_path: Path) -> None:
    """变更集与该验证点的范围不相交 → 复用上一轮的结论。"""
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        _write_scope(plan_dir, 2, DISJOINT_CHANGES)
        ex = _executor(plan_dir, plan, repo=repo)

        assert ex._seed_reused_verdicts(1) == {"VP-001"}
        # 复用项进本轮视图 —— 本轮不用再跑它。
        assert set(ex._current_round_verdicts()) == {"VP-001"}
        assert "VP-001" in ex._resumable_completed_ids(plan["vps"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 变更集落在范围里 → 重跑
# ---------------------------------------------------------------------------


def test_rerun_when_touched(tmp_path: Path) -> None:
    """本轮改到了该验证点负责的那一片 → 必须重跑，不管上轮是否 PASSED。"""
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        _write_scope(plan_dir, 2, ("backend/verification/orchestrator.py",))
        ex = _executor(plan_dir, plan, repo=repo)

        assert ex._seed_reused_verdicts(1) == set()
        assert "VP-001" not in ex._resumable_completed_ids(plan["vps"])
        assert ex._current_round_verdicts() == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. 共享面命中 → 全量重跑
# ---------------------------------------------------------------------------


def test_shared_surface_forces_full_rerun(tmp_path: Path) -> None:
    """共享面（提示词 / 状态机 / 跑测试的脚本…）一改 → 本轮全量重跑。

    范围不相交也不能救它：共享面一改，几乎所有验证点的结论都可能失效。
    """
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        _write_scope(plan_dir, 2, ("backend/prompts.py",))
        ex = _executor(plan_dir, plan, repo=repo)

        assert ex._seed_reused_verdicts(1) == set()
        assert "VP-001" not in ex._resumable_completed_ids(plan["vps"])
        assert ex._current_round_verdicts() == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. 复用项带 reused_from_round
# ---------------------------------------------------------------------------


def test_reused_marker_recorded(tmp_path: Path) -> None:
    """复用项标 ``reused_from_round`` —— 账本据此把"本轮真跑"与"复用"分开列。"""
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        _write_scope(plan_dir, 2, DISJOINT_CHANGES)
        ex = _executor(plan_dir, plan, repo=repo)
        assert ex._seed_reused_verdicts(1) == {"VP-001"}

        verdict = ex._current_round_verdicts()["VP-001"]
        assert verdict[REUSED_FROM_ROUND_FIELD] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. 复用项以本轮轮次号重新写入
# ---------------------------------------------------------------------------


def test_reused_verdict_restamped_to_current_round(tmp_path: Path) -> None:
    """复用项以**本轮**的轮次号重新写入。

    不重新盖戳的话，``_current_round_verdicts`` 会把它当历史轮排除 —— 复用
    等于没发生（本轮视图里那条验证点仍然是空的）。
    """
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        _write_scope(plan_dir, 2, DISJOINT_CHANGES)
        ex = _executor(plan_dir, plan, repo=repo)
        assert ex._seed_reused_verdicts(1) == {"VP-001"}

        verdict = ex._current_round_verdicts()["VP-001"]
        assert verdict["round"] == 2
        # 上一轮那一份仍在（审计痕迹不丢）。
        assert ex._verdicts_by_round[1]["VP-001"]["round"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. 定义指纹仍然生效（不得放宽）
# ---------------------------------------------------------------------------


def test_definition_fingerprint_still_enforced(tmp_path: Path) -> None:
    """同轮内恢复仍要过既有定义指纹校验 —— 指纹对不上就重跑。

    轮次作用域是**加**了一道判据，不是把既有那道换掉：一条结论只对它
    产生它的那份定义有效。
    """
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        # 本轮（round 2）已经跑过一条结论，但它是**改判据之前**挣的。
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-001", plan, round_no=2),
        )
        conn.commit()

        changed_plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
        changed_plan["vps"][0]["expected_result"] = "改成别的东西"

        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, changed_plan, repo=repo)

        assert "VP-001" not in ex._resumable_completed_ids(changed_plan["vps"])

        # 对照：判据没变 → 指纹对得上 → 同轮内恢复照旧跳过。
        same = _executor(plan_dir, plan, repo=repo)
        assert "VP-001" in same._resumable_completed_ids(plan["vps"])
    finally:
        conn.close()
