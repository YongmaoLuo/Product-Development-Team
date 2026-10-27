"""TDD spec —— 发号账本（``allocated_vp_ids``）的**永不复用**不变量。

背景
----
A1′ 会把主题外的验证点**移除**掉。移除的是「这条验证点不属于本计划」，
不是「这个号可以再发一次」—— 号一旦发出去就发过：它可能已经跑过、已经有
过结论、可能已经被别的验证点引用过。**发号账本**（计划目录里的
``verification_plan_delta_state.json`` 的 ``allocated_vp_ids``）记的就是
「哪些号发出去过」，它是下一次发号时唯一必须避开的东西。

移除与发号是两条独立的线，但它们共享同一个号：

* 移除**不回收**号 —— 账本长度不变，那条号仍然记着「发出去过」；
* 后续的 delta **不重发**号 —— 新号既不在**移除集**里，也不在**已分配
  集合**里。

两条不变量
----------
1. ``test_allocated_length_unchanged`` —— 删除前后 ``allocated_vp_ids`` 的
   长度不变。
2. ``test_new_vp_id_skips_removed`` —— 删除之后新 delta 申请到的新号，
   既不在移除集里，也不在已分配集合里。

为什么第二条要跑两遍
--------------------
「计划没变」这一遍只能证明*目前*没撞号：被移除的验证点仍然留在计划文件
里，于是发号器按计划里最大的编号往后发，**碰巧**跳过了它。

真正有牙齿的是「计划被重新写过」那一遍：新计划里已经没有那条被移除的
验证点，计划本身不再挡着这个号 —— 挡住它的是**发号账本**。账本一旦漏了
这一条，同一个号就会被发第二次，新验证点于是继承旧号在 SQLite 里的旧结论
（一次都没跑就记成失败，或者一次都没跑就被当成"验证通过"而跳过）。

测试命令::

    pytest backend/tests/integration/verification_chain/test_vp_id_ledger.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
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
from verification.vp_removal_audit import (  # noqa: E402
    AUDIT_FILENAME,
    load_removed_vp_ids,
)
from verification_executor import VerificationExecutor  # noqa: E402
from verification_plan_delta import (  # noqa: E402
    PlanDelta,
    apply_delta_persisted,
    load_delta_state,
    plan_file_digest,
    save_delta_state,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

#: 本测试用的计划 id。
PLAN_ID = "vp-ledger-plan"

#: 计划文件在计划目录里的名字。
PLAN_FILENAME = "verification_plan.json"

#: 判据表（编号式）。
CRITERIA: List[Dict[str, str]] = [
    {"id": "AC-001", "text": "用户可以用邮箱和密码登录"},
]

#: 正常的那条。
NORMAL_VP = "VP-001"

#: 主题外的那条 —— 它声明的 ``AC-NOT-IN-PRD`` 不在判据表里，所以被移除。
#:
#: 它是计划里**编号最大**的那条：这一点是刻意的。「计划被重新写过」之后，
#: 计划本身不再挡着这个号，能挡住它的只剩发号账本。
OFF_TOPIC_VP = "VP-002"

#: 计划里这两条的号都发出去过 —— 账本记着它们。
LEDGER: List[str] = [NORMAL_VP, OFF_TOPIC_VP]

#: 两种存储后端。账本住在计划目录里，与后端无关 —— 但移除是在执行器里
#: 发生的，所以两条后端都要验一遍。
MODES = ("sqlite", "legacy")


# ---------------------------------------------------------------------------
# 计划与夹具
# ---------------------------------------------------------------------------


def _plan() -> Dict[str, Any]:
    """两条验证点：正常 1 + 主题外 1（编号最大的那条）。"""
    return {
        "plan_id": PLAN_ID,
        "criteria": [dict(c) for c in CRITERIA],
        "verification_points": [
            {
                "id": NORMAL_VP,
                "title": "正常验证点：验的是计划里的一条判据",
                "related_prd_criteria": ["AC-001"],
                "verification_method": "api_test",
                "verification_phase": 1,
                "priority": "medium",
                "expected_result": "这条验证点验的是本计划要的判据",
            },
            {
                "id": OFF_TOPIC_VP,
                "title": "主题外验证点：引用的验收标准不在本计划的判据表里",
                "related_prd_criteria": ["AC-NOT-IN-PRD"],
                "verification_method": "api_test",
                "verification_phase": 1,
                "priority": "medium",
                "expected_result": "主题外的验证点不该被跑，也不该进报告",
            },
        ],
    }


def _runner(ran: List[str]):
    """一个只记账的假执行器：跑过的验证点 id 记在 ``ran`` 里。"""

    async def _run(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        ran.append(vp_node["id"])
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    return _run


def _setup(root: Path, mode: str) -> Tuple[Path, Optional[VerificationRepository]]:
    """建一个计划目录：计划文件 + 发号账本（+ SQLite 那一侧的行）。"""
    plan_dir = root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / PLAN_FILENAME).write_text(
        json.dumps(_plan(), ensure_ascii=False, indent=2), encoding="utf-8",
    )
    # 发号账本：这两条的号都发出去过。
    save_delta_state(
        plan_dir,
        {
            "seen_task_ids": ["TASK-1"],
            "round": 1,
            "allocated_vp_ids": list(LEDGER),
        },
    )

    repo: Optional[VerificationRepository] = None
    if mode == "sqlite":
        conn = open_db(root / "state.db")
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(PLAN_ID, verification_status="running")
        conn.commit()
    return plan_dir, repo


def _run_executor(
    plan_dir: Path,
    repo: Optional[VerificationRepository],
) -> List[str]:
    """跑一轮执行器，返回跑过的验证点 id。"""
    ran: List[str] = []
    executor = VerificationExecutor(
        verification_plan=_plan(),
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=_runner(ran),
        verif_repo=repo,
    )
    asyncio.run(executor.run(max_parallel=1))
    return ran


def _allocated(plan_dir: Path) -> List[str]:
    """发号账本（``allocated_vp_ids``）。"""
    return list(load_delta_state(plan_dir).get("allocated_vp_ids") or [])


def _audit_rows(plan_dir: Path) -> List[Dict[str, Any]]:
    """审计文件里的每一行（坏行跳过，与读路径同一套容错）。"""
    path = plan_dir / AUDIT_FILENAME
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _new_delta() -> PlanDelta:
    """一次增量：新增一条验证点（定义完整，才配拿到号）。"""
    return PlanDelta(
        added=[
            {
                "title": "新增的一条验证点",
                "related_prd_criteria": "AC-001",
                "verification_method": "api_test",
                "expected_result": "新增的这条验证点通过",
                "reason": "本轮新增了一个功能",
            }
        ]
    )


def _apply_new_delta(plan_dir: Path, plan_data: Dict[str, Any]) -> Dict[str, Any]:
    """按生产路径的方式落一次增量，返回摘要。

    参数与生产调用一致：账本既作 ``avoid_ids``（避开发过的号）也作
    ``reserved_ids``（拒绝复用旧号的占位集合）。
    """
    plan_file = plan_dir / PLAN_FILENAME
    ledger = _allocated(plan_dir)
    return apply_delta_persisted(
        plan_file,
        plan_data,
        _new_delta(),
        2,
        expected_digest=plan_file_digest(plan_file),
        avoid_ids=ledger,
        reserved_ids=ledger,
    )


def _issued_ids(summary: Dict[str, Any]) -> List[str]:
    """这一轮发出去的号。"""
    return [str(vp_id) for vp_id in (summary.get("allocated_vp_ids") or [])]


# ---------------------------------------------------------------------------
# 1. 移除不回收号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_allocated_length_unchanged(
    tmp_path: Path, mode: str, reclaim_workers
):
    """删除前后，发号账本的长度**不变**。

    移除的是「这条验证点不属于本计划」，不是「这个号可以再发一次」。号已经
    发出去过 —— 它可能跑过、可能有结论、可能已经被别的验证点引用过。把它从
    账本上抹掉，下一次发号就会撞上它。

    归档记录自己也声明了这一点（``allocated_vp_ids_preserved``）—— 那是
    「移除没有动账本」的书面留痕，不是顺带写的一个字段。
    """
    plan_dir, repo = _setup(tmp_path / mode, mode)
    try:
        allocated_before = _allocated(plan_dir)
        assert allocated_before, "发号账本一开始就是空的 —— 这条测试什么也没钉住"

        _run_executor(plan_dir, repo)

        removed = load_removed_vp_ids(plan_dir)
        assert removed == {OFF_TOPIC_VP}, f"主题外的验证点没有被移除: {removed!r}"
        allocated_after = _allocated(plan_dir)
    finally:
        alive = reclaim_workers()

    assert not alive, f"后台 worker 没收干净: {alive!r}"

    # 长度不变 —— 一个号都没被回收。
    assert len(allocated_after) == len(allocated_before), (
        f"移除改动了发号账本的长度: {allocated_before!r} -> {allocated_after!r}"
    )
    # 账本的内容也不变（不只是长度碰巧一样）。
    assert set(allocated_after) == set(allocated_before), (
        f"移除改动了发号账本的内容: {allocated_before!r} -> {allocated_after!r}"
    )
    # 被移除的那个号仍然记着「发出去过」。
    assert OFF_TOPIC_VP in allocated_after, allocated_after

    # 归档记录里的书面留痕。
    rows = _audit_rows(plan_dir)
    assert len(rows) == 1, rows
    assert rows[0]["vp_id"] == OFF_TOPIC_VP, rows
    assert rows[0].get("allocated_vp_ids_preserved") is True, rows


# ---------------------------------------------------------------------------
# 2. 后续 delta 不重发已删的号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_new_vp_id_skips_removed(
    tmp_path: Path, mode: str, reclaim_workers
):
    """删除之后，新 delta 申请到的号既不在移除集里，也不在已分配集合里。

    两遍：

    * **计划没变** —— 新号不能是刚被移除的那个；
    * **计划被重新写过**（新计划里已经没有那条被移除的验证点）—— 新号仍然
      不能是它。这一遍才有牙齿：计划本身不再挡着这个号，能挡住它的只剩
      **发号账本**。
    """
    # --- 第一遍：计划没变 -------------------------------------------------
    plan_dir, repo = _setup(tmp_path / f"{mode}-same-plan", mode)
    try:
        allocated_before = _allocated(plan_dir)
        _run_executor(plan_dir, repo)
        removed = load_removed_vp_ids(plan_dir)
        assert removed == {OFF_TOPIC_VP}, f"主题外的验证点没有被移除: {removed!r}"

        plan_data = json.loads(
            (plan_dir / PLAN_FILENAME).read_text(encoding="utf-8")
        )
        summary = _apply_new_delta(plan_dir, plan_data)
        issued = _issued_ids(summary)
        assert len(issued) == 1, f"这一轮没有发出去恰好一个号: {summary!r}"
        new_id = issued[0]

        assert new_id not in removed, (
            f"新号 {new_id} 撞上了刚被移除的验证点: {removed!r}"
        )
        assert new_id not in allocated_before, (
            f"新号 {new_id} 撞上了已经发出去的号: {allocated_before!r}"
        )

        allocated_after = _allocated(plan_dir)
        assert len(allocated_after) == len(allocated_before) + 1, (
            f"发号账本没有记下新号: {allocated_before!r} -> {allocated_after!r}"
        )
        assert new_id in allocated_after, allocated_after
        assert set(allocated_before) <= set(allocated_after), allocated_after
    finally:
        alive = reclaim_workers()

    assert not alive, f"后台 worker 没收干净: {alive!r}"

    # --- 第二遍：计划被重新写过 --------------------------------------------
    plan_dir2, repo2 = _setup(tmp_path / f"{mode}-regenerated-plan", mode)
    try:
        allocated_before2 = _allocated(plan_dir2)
        _run_executor(plan_dir2, repo2)
        removed2 = load_removed_vp_ids(plan_dir2)
        assert removed2 == {OFF_TOPIC_VP}, (
            f"主题外的验证点没有被移除: {removed2!r}"
        )

        # 计划被重新写过：新计划里没有那条被移除的验证点。计划本身不再
        # 挡着这个号 —— 接下来能挡住它的只有发号账本。
        regenerated = json.loads(
            (plan_dir2 / PLAN_FILENAME).read_text(encoding="utf-8")
        )
        regenerated["verification_points"] = [
            vp
            for vp in regenerated["verification_points"]
            if str(vp.get("id")) not in removed2
        ]
        (plan_dir2 / PLAN_FILENAME).write_text(
            json.dumps(regenerated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        summary2 = _apply_new_delta(plan_dir2, regenerated)
        issued2 = _issued_ids(summary2)
        assert len(issued2) == 1, f"这一轮没有发出去恰好一个号: {summary2!r}"
        new_id2 = issued2[0]

        assert new_id2 not in removed2, (
            f"计划被重新写过之后，新号 {new_id2} 撞上了刚被移除的验证点: "
            f"{removed2!r}"
        )
        assert new_id2 not in set(allocated_before2), (
            f"新号 {new_id2} 撞上了已经发出去的号: {allocated_before2!r}"
        )
    finally:
        alive = reclaim_workers()

    assert not alive, f"后台 worker 没收干净: {alive!r}"
