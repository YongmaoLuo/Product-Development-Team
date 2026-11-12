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

2026-11-08 追加三节：

8. **派发集**（纯函数 ``vps_requiring_dispatch``）—— 计划里每个**本轮还没有
   结论**的验证点都必须被派发；DEFERRED 不算结论，复用搬过来的结论算。
9. **执行器侧的派发集重建** —— 队列取并集（不覆盖别人的条目），且派发集
   压过 resume 路径（「以前跑过」不能顶掉「这一轮还没跑」）。
10. **跨轮的延后记号** —— 上一轮真跑出来的结论不被本轮的 DEFERRED 顶掉，
    但本轮的记号必须进本轮账本（报告只列有本轮条目的验证点，没有条目的
    那一条会被判定环节自己补成 FAILED —— 那是**编造**）。

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
    vps_requiring_dispatch,
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


# ---------------------------------------------------------------------------
# 8. 派发集：这一轮必须被派发的验证点（2026-11-08）
# ---------------------------------------------------------------------------
#
# 「这一轮还没跑」与「这一轮已经跑完」之间必须有一份**纯函数**判据：
# 派发侧（谁该被叫起来跑）与读侧（报告里有没有这一条）共用同一份口径。
#
# 缺了它，一个既没被复用、又没有本轮结论的验证点会**两头不落**：不在复用
# 里（复用判据不成立 —— 它没有 ``covers_paths``），也不在派发集里（没人叫
# 它跑）。2026-11-08 的 VP-015 就是这样：报告里它没有条目，判定环节只能
# 自己编一条 FAILED —— 报告在说一件从没发生过的事。


def test_dispatch_set_requires_a_vp_without_any_current_round_verdict() -> None:
    """本轮一条结论都没有 → 必须被派发。"""
    assert vps_requiring_dispatch(["VP-001", "VP-002"], {}) == {"VP-001", "VP-002"}


def test_dispatch_set_excludes_a_vp_with_a_current_round_conclusion() -> None:
    """本轮已经有结论 → 不必再派发。"""
    current = {"VP-001": {"status": "PASSED", "round": 5}}
    assert vps_requiring_dispatch(["VP-001"], current) == set()


def test_dispatch_set_requires_a_vp_whose_only_verdict_is_from_an_older_round() -> None:
    """上一轮的结论不替本轮说话 —— 本轮没跑过就得再跑。

    这正是 2026-11-08 的 VP-015：第 4 轮 PASSED，第 5 轮没有结论。
    """
    # 本轮视图（``_current_round_verdicts``）里没有它。
    assert vps_requiring_dispatch(["VP-001"], {}) == {"VP-001"}


def test_dispatch_set_requires_a_vp_whose_current_round_verdict_is_deferred() -> None:
    """DEFERRED 不是结论 —— 「这一轮还没跑」仍要留在派发集里。"""
    current = {"VP-001": {"status": "DEFERRED", "round": 5}}
    assert vps_requiring_dispatch(["VP-001"], current) == {"VP-001"}


def test_dispatch_set_excludes_a_reused_carry_over() -> None:
    """复用搬过来的是**本轮**的结论 —— 它不需要再被派发一遍。

    复用的结论以本轮的轮次号重新写入（见
    :meth:`VerificationExecutor._seed_reused_verdicts`），所以它已经是本轮
    的答案；把它塞回派发集等于把「已经答过」当成「还没答」。
    """
    current = {
        "VP-001": {
            "status": "PASSED",
            "round": 5,
            REUSED_FROM_ROUND_FIELD: 4,
        },
    }
    assert vps_requiring_dispatch(["VP-001"], current) == set()
    # 即便本轮视图里暂时没有它，复用这一条也仍然算数。
    assert vps_requiring_dispatch(
        ["VP-001"], {}, reused_vp_ids={"VP-001"},
    ) == set()


def test_dispatch_set_ignores_verdicts_that_are_not_dicts() -> None:
    """坏数据不许让整轮什么都不跑（没有结论 = 要派发）。"""
    assert vps_requiring_dispatch(["VP-001"], {"VP-001": None}) == {"VP-001"}
    assert vps_requiring_dispatch(["VP-001"], {"VP-001": {}}) == {"VP-001"}


# ---------------------------------------------------------------------------
# 9. 执行器侧：派发集重建 + 延后记号不许被上一轮的结论顶掉
# ---------------------------------------------------------------------------


def test_rebuild_dispatch_set_queues_vps_without_a_current_round_conclusion(
    tmp_path: Path,
) -> None:
    """派发集重建后，「本轮没结论」的验证点回到队列里。"""
    plan = _plan("VP-001", "VP-002")
    conn, repo = _db(tmp_path, 5)
    try:
        # VP-001 本轮已经跑过（结论在本轮账本里），VP-002 没有。
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-001", plan, round_no=5),
        )
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        required = ex._rebuild_dispatch_set()

        assert required == {"VP-002"}
        assert "VP-002" in ex.pending_vps
        assert "VP-001" not in ex.pending_vps
    finally:
        conn.close()


def test_rebuild_dispatch_set_keeps_the_existing_queue(tmp_path: Path) -> None:
    """与既有队列取**并集** —— 不覆盖别人的条目。"""
    plan = _plan("VP-001", "VP-002")
    conn, repo = _db(tmp_path, 5)
    try:
        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)
        ex._pending_vps = ["VP-001"]

        ex._rebuild_dispatch_set()

        assert ex.pending_vps == ["VP-001", "VP-002"]
    finally:
        conn.close()


def test_stale_previous_round_verdict_does_not_suppress_the_deferred_marker(
    tmp_path: Path,
) -> None:
    """上一轮留下的结论不许顶掉本轮的「延后」记号。

    2026-11-08（VP-015）：第 4 轮 PASSED 的 e2e 关卡在第 5 轮被更早的
    关卡拦下（DEFERRED）—— 延后记号被第 4 轮那条结论顶掉，本轮账本里一条
    都没有，报告里也没有它的条目，判定环节只能自己编一条 FAILED。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-001", plan, round_no=4),
        )
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        ex._mark_layer_deferred(
            [{"id": "VP-001"}],
            "Phase 2 关卡延后：更早的全量关卡 VP-000 结论为 FAILED",
        )

        verdict = ex._verdicts_by_round[5]["VP-001"]
        assert verdict["status"] == "DEFERRED"
        assert verdict["round"] == 5
        # 上一轮那份仍在（审计痕迹不丢）。
        assert ex._verdicts_by_round[4]["VP-001"]["status"] == "PASSED"
    finally:
        conn.close()


def test_current_round_conclusion_still_blocks_the_deferred_marker(
    tmp_path: Path,
) -> None:
    """本轮真跑出来的结论仍然不许被一条延后记号顶掉（不得放宽）。"""
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-001", plan, round_no=5),
        )
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        ex._mark_layer_deferred(
            [{"id": "VP-001"}],
            "Phase 2 关卡延后：更早的全量关卡 VP-000 结论为 FAILED",
        )

        assert ex._verdicts_by_round[5]["VP-001"]["status"] == "PASSED"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_round_end_marks_a_gate_deferred_vp_with_a_current_round_verdict(
    tmp_path: Path,
) -> None:
    """整轮跑完后，被关卡拦下的验证点必须有**本轮**的条目。

    这是「报告不许编造 FAILED」的最后一道闸：报告只列有本轮结论的验证点，
    没有条目的那一条会被判定环节自己补成 FAILED —— 那是在说一件从没发生过
    的事（2026-11-08 的 VP-015：第 4 轮 PASSED，第 5 轮被更早的关卡拦下，
    延后记号被上一轮的结论顶掉，本轮账本里一条都没有）。
    """
    plan = _plan("VP-001", "VP-002")
    plan["vps"][1]["verification_phase"] = 2
    plan["vps"][1]["phase_order"] = 1
    conn, repo = _db(tmp_path, 5)
    try:
        # 上一轮（第 4 轮）VP-002 PASSED —— 正是 2026-11-08 的现场。
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-002", plan, round_no=4),
        )
        conn.commit()

        async def failing_runner(vp: Dict[str, Any]) -> Dict[str, Any]:
            return {"status": "FAILED", "reasons": ["boom"], "evidence": {}}

        plan_dir = _plan_dir(tmp_path)
        ex = VerificationExecutor(
            verification_plan=plan,
            plan_id=PLAN_ID,
            plan_dir=plan_dir,
            sub_agent_runner=failing_runner,
            verif_repo=repo,
            round_no=5,
        )

        await ex.run(max_parallel=1)

        # VP-001 真跑过（FAILED）→ Phase 2 的关卡被拦下。
        assert ex._verdicts_by_round[5]["VP-001"]["status"] == "FAILED"
        # VP-002 本轮没跑 —— 但它必须有本轮的条目（DEFERRED = 这一轮还没跑）。
        assert ex._verdicts_by_round[5]["VP-002"]["status"] == "DEFERRED"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 10. 跨轮：上一轮真跑出来的结论不被本轮的「还没跑」记号顶掉
#
#     两边必须同时成立，缺一个就出事：
#
#     * 上一轮 PASSED 的结论**留在裁决表里** —— 一条「本轮还没跑」的记号
#       顶掉一份真跑出来的结论，等于把「没跑」说成「跑过、而且结论相反」
#       （``test_a_real_conclusion_is_not_overwritten_by_a_deferral`` 钉的
#       就是这条）；
#     * 本轮的「还没跑」记号**进本轮账本** —— 报告只列有本轮条目的验证点，
#       没有条目的那一条会被判定环节自己补成 FAILED（2026-11-08 的 VP-015）。
#
#     读侧（:meth:`VerificationExecutor.collect_verdicts`）按轮次取值：本轮
#     有条目就用本轮的 —— 报告因此看到的是「这一轮还没跑」，而不是上一轮的
#     PASSED。
# ---------------------------------------------------------------------------


def test_a_previous_rounds_conclusion_survives_this_rounds_deferral(
    tmp_path: Path,
) -> None:
    """跨轮的延后记号只进本轮账本 —— 上一轮的结论留在裁决表里。"""
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-001", plan, round_no=4),
        )
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        ex._mark_layer_deferred(
            [{"id": "VP-001"}],
            "Phase 2 关卡延后：更早的全量关卡 VP-000 结论为 FAILED",
        )

        # 上一轮那份结论还在（没被「还没跑」顶掉）。
        assert ex._verdicts["VP-001"]["status"] == "PASSED"
        assert ex._verdicts["VP-001"]["round"] == 4
        # 本轮的记号在本轮账本里。
        assert ex._verdicts_by_round[5]["VP-001"]["status"] == "DEFERRED"
    finally:
        conn.close()


def test_collect_verdicts_reports_this_rounds_deferral(
    tmp_path: Path,
) -> None:
    """读侧（报告）看到的是**本轮**的条目 —— 上一轮的 PASSED 不替本轮说话。

    这一条是「报告不许编造 FAILED」的最后一道闸：报告拿到的每一条都必须
    是本轮的结论；本轮没有结论的验证点，判定环节会自己补成 FAILED。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        repo.append_verdict(
            PLAN_ID, _previous_verdict("VP-001", plan, round_no=4),
        )
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        ex._mark_layer_deferred(
            [{"id": "VP-001"}],
            "Phase 2 关卡延后：更早的全量关卡 VP-000 结论为 FAILED",
        )

        entries = {e["vp_id"]: e for e in ex.collect_verdicts()}
        assert entries["VP-001"]["status"] == "DEFERRED"
        assert entries["VP-001"]["round"] == 5
    finally:
        conn.close()


def test_a_deferral_in_the_same_round_still_replaces_the_round_entry(
    tmp_path: Path,
) -> None:
    """同一轮里后来的结论照旧顶掉前面的（不得放宽既有语义）。

    「跨轮不顶掉」只针对**别的轮次**真跑出来的结论；同一轮里先失败后延后
    仍然是「延后」（那正是关卡拦下时的现场）。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        ex.record_verdict(
            "VP-001", {"status": "FAILED", "reasons": ["boom"], "evidence": {}},
        )
        ex.record_verdict(
            "VP-001", {"status": "DEFERRED", "reasons": ["延后"], "evidence": {}},
        )

        assert ex._verdicts["VP-001"]["status"] == "DEFERRED"
        assert ex._verdicts["VP-001"]["round"] == 5
    finally:
        conn.close()


def test_the_round_end_gate_keeps_a_precise_deferral_reason(
    tmp_path: Path,
) -> None:
    """收尾闸不许把关卡给出的具体理由换成笼统的「未被派发」。

    关卡拦下时已经写清了「被谁拦下」—— 那是读者唯一能据此行动的线索。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)

        ex._mark_layer_deferred(
            [{"id": "VP-001"}],
            "Phase 2 关卡延后：更早的全量关卡 VP-000 结论为 FAILED",
        )
        ex._ensure_current_round_entries()

        entry = ex._verdicts_by_round[5]["VP-001"]
        assert entry["status"] == "DEFERRED"
        assert "VP-000" in entry["reasons"][0]
    finally:
        conn.close()


def test_the_dispatch_set_overrides_the_resume_path(tmp_path: Path) -> None:
    """派发集说「必须跑」的验证点不许被 resume 路径跳过。

    resume 路径认的是「缓存里有 + 定义指纹对得上 + 本轮**有条目**」；
    派发集认的是「本轮**没有结论** → 必须跑」。本轮那条是 ``DEFERRED``
    （「这一轮还没跑」）时两条判据会打架 —— 以派发集为准：以前跑过不能
    顶掉「这一轮还没跑」。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 5)
    try:
        plan_dir = _plan_dir(tmp_path)
        ex = _executor(plan_dir, plan, repo=repo, round_no=5)
        items = ex._build_items_from_plan()

        # 本轮被延后 —— 本轮账本里有一条 DEFERRED（「这一轮还没跑」）。
        ex._mark_layer_deferred(
            [{"id": "VP-001"}],
            "Phase 2 关卡延后：更早的全量关卡 VP-000 结论为 FAILED",
        )
        # 缓存里还有它（以前跑过、定义指纹也对得上）→ resume 路径本来会
        # 把它当「已经跑过」跳过。
        ex._completed_items = ["VP-001"]

        ex._rebuild_dispatch_set()

        assert ex._dispatch_required_ids == {"VP-001"}
        assert "VP-001" not in ex._resumable_completed_ids(items)
    finally:
        conn.close()

