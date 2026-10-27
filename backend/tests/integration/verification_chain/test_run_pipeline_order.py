"""TDD spec —— ``VerificationExecutor.run()`` 的主题相关性判定管线。

背景
----
判定内核（:mod:`verification.topic_relevance`）与归档审计
（:mod:`verification.vp_removal_audit`）都已就绪，但**执行器没有调用点**：
一条主题外的验证点每轮照样被全量验证 —— 跑一遍、产出一条裁决、报告里再出现
一次。它花的钱与它给出的结论一样没有价值。

这个任务把判定接进 ``run()`` 的管线，并把顺序钉死::

    装载裁决 → 隔离孤儿 → 扣除移除集 → 判定 → 先归档后移除 → 派生索引

三条承重契约
------------
1. **先归档、后移除** —— 审计行先落盘，投影才允许少一条。没有审计行的移除
   等于「没人知道它为什么消失」。
2. **移除集每轮重建** —— 移除集由 ``load_removed_vp_ids()`` 从审计文件重建，
   **不写进** ``executor_state``（不二次落盘，两处真相必然漂移）。
3. **移除是单向的** —— 第二轮不得复活上一轮移除的验证点。

TDD contract —— 4 cases
------------------------
1. ``test_archive_before_remove`` —— 审计行先落盘，投影再消失。
2. ``test_second_run_no_revival`` —— 第二次 run 不复活、不重复归档。
3. ``test_orphan_path_untouched`` —— 孤儿隔离行为与现状一致，且不进移除集。
4. ``test_pipeline_order`` —— 判定 → 归档 → 移除 → 派生索引的顺序。

输入
----
``vp-fixture-002`` 那份混合计划（一条主题外、一条孤儿、一条正常）扩到 5 条
验证点，再加上一份**编号式**的判据表（``AC-001`` / ``AC-002``）。这一轮
``llm_batch=None`` —— 没有模型可问，判定只能靠确定性层。

测试命令::

    pytest backend/tests/integration/verification_chain/test_run_pipeline_order.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

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
from verification import vp_removal_audit  # noqa: E402
from verification.vp_removal_audit import (  # noqa: E402
    AUDIT_FILENAME,
    load_removed_vp_ids,
)
from verification_executor import VerificationExecutor  # noqa: E402


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

#: 夹具计划（任务 7 的交付物）：一条主题外、一条孤儿、一条正常。
FIXTURE_PLAN_PATH = (
    _BACKEND_DIR
    / "tests"
    / "fixtures"
    / "verification"
    / "vp-fixture-002"
    / "verification_plan.json"
)

#: 本计划的判据表 —— 编号式（``id`` + 正文）。主题判定拿它当「这个计划到底
#: 要什么」的权威；一条验证点声明的编号在这里不存在，就是一条可核对的矛盾。
CRITERIA: List[Dict[str, str]] = [
    {"id": "AC-001", "text": "用户可以用邮箱和密码登录"},
    {"id": "AC-002", "text": "用户可以导出 CSV 报表"},
]

#: 主题外的那条：它声明的 ``AC-NOT-IN-PRD`` 不在本计划的判据表里。
OFF_TOPIC_VP = "VP-900"

#: 孤儿的那条：计划里有它，但它没有声明任何判据。
ORPHAN_VP = "VP-902"

#: 本测试用的计划 id（与夹具的 id 区分开 —— 夹具目录本身是只读的）。
PLAN_ID = "vp-fixture-002-pipeline"

#: 计划里剩下的三条正常验证点。
NORMAL_VPS = ["VP-901", "VP-903", "VP-904"]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    d = tmp_path / PLAN_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def verif_repo(tmp_path: Path) -> VerificationRepository:
    """真的 SQLite 仓库 —— 判定与移除要落在真实的持久化状态上。"""
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(PLAN_ID, verification_status="running")
    conn.commit()
    return repo


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _normal_point(vp_id: str, criteria: List[str]) -> Dict[str, Any]:
    """一条正常的验证点：声明的判据在本计划的判据表里。"""
    return {
        "id": vp_id,
        "title": f"{vp_id} 验的是计划里的一条判据",
        "related_prd_criteria": list(criteria),
        "verification_method": "api_test",
        "verification_phase": 1,
        "priority": "medium",
        "expected_result": "这条验证点验的是本计划要的判据",
    }


def _plan() -> Dict[str, Any]:
    """夹具计划扩到 5 条验证点 + 一份编号式判据表。"""
    payload = json.loads(FIXTURE_PLAN_PATH.read_text(encoding="utf-8"))
    points = [dict(vp) for vp in payload["verification_points"]]
    points.append(_normal_point("VP-903", ["AC-002"]))
    points.append(_normal_point("VP-904", ["AC-001", "AC-002"]))
    return {
        "plan_id": payload["plan_id"],
        "verification_points": points,
        "criteria": [dict(c) for c in CRITERIA],
    }


def _executor(
    plan_dir: Path,
    repo: VerificationRepository,
    runner=None,
) -> VerificationExecutor:
    """执行器本体。``topic_relevance_llm_batch`` 缺省 = None（没有模型）。"""
    return VerificationExecutor(
        verification_plan=_plan(),
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=runner or _runner([]),
        verif_repo=repo,
    )


def _runner(ran: List[str]):
    """一个只记账的假执行器：跑过的验证点 id 记在 ``ran`` 里。"""

    async def _run(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        ran.append(vp_node["id"])
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    return _run


def _active_ids(executor: VerificationExecutor) -> List[str]:
    """活动集合 —— 这一轮**会跑**的验证点（计划里的，不是已移除的）。"""
    return [str(vp.get("id")) for vp in executor._build_items_from_plan()]


def _audit_rows(plan_dir: Path) -> List[Dict[str, Any]]:
    """审计文件里的每一行（坏行跳过，与读路径同一套容错）。"""
    path = plan_dir / AUDIT_FILENAME
    if not path.is_file():
        return []
    rows = []
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


# ---------------------------------------------------------------------------
# 1. 先归档、后移除
# ---------------------------------------------------------------------------


def test_archive_before_remove(plan_dir: Path, verif_repo: VerificationRepository, monkeypatch):
    """审计行先落盘，投影再消失。

    归档的那一刻这条验证点**还在**活动集合里 —— 移除发生在归档返回之后。
    顺序反了的话，投影先少一条、审计行后落盘，中间那一瞬就有一条「消失
    了但没人知道为什么」的验证点；写审计失败时它更是永远消失。
    """
    executor = _executor(plan_dir, verif_repo)
    observed: Dict[str, Any] = {}

    real_archive = vp_removal_audit.archive_removal

    def _spy(record, target_dir):
        observed["active_at_archive"] = _active_ids(executor)
        observed["lines_before"] = len(_audit_rows(plan_dir))
        real_archive(record, target_dir)
        observed["lines_after"] = len(_audit_rows(plan_dir))

    monkeypatch.setattr(vp_removal_audit, "archive_removal", _spy)

    asyncio.run(executor.run(max_parallel=1))

    # 归档的那一刻它还在活动集合里。
    assert observed["active_at_archive"] == [
        "VP-900", "VP-901", "VP-902", "VP-903", "VP-904",
    ], observed
    # 归档之前审计文件是空的，归档之后才有一行。
    assert observed["lines_before"] == 0, observed
    assert observed["lines_after"] == 1, observed

    # 归档之后，投影里它没了。
    assert OFF_TOPIC_VP not in _active_ids(executor)

    # 审计文件里恰好一行，就是它。
    rows = _audit_rows(plan_dir)
    assert len(rows) == 1, rows
    assert rows[0]["vp_id"] == OFF_TOPIC_VP, rows
    assert load_removed_vp_ids(plan_dir) == {OFF_TOPIC_VP}


# ---------------------------------------------------------------------------
# 2. 第二次 run 不复活
# ---------------------------------------------------------------------------


def test_second_run_no_revival(plan_dir: Path, verif_repo: VerificationRepository):
    """第二轮：移除集从审计文件重建，那条验证点不复活、不重复归档。

    移除是单向的 —— 一条被判定为主题外的验证点不该在下一轮又跑一遍、
    又产出一条裁决。审计文件是移除的**唯一**持久化真相，所以第二轮的移除集
    必须从它重建（而不是从上一轮的内存状态继承）。
    """
    first_run: List[str] = []
    second_run: List[str] = []

    executor1 = _executor(plan_dir, verif_repo, _runner(first_run))
    asyncio.run(executor1.run(max_parallel=1))

    assert OFF_TOPIC_VP not in first_run, first_run
    assert len(_audit_rows(plan_dir)) == 1

    # 第二轮：同一个计划目录，一个全新的执行器实例。
    executor2 = _executor(plan_dir, verif_repo, _runner(second_run))
    asyncio.run(executor2.run(max_parallel=1))

    assert OFF_TOPIC_VP not in second_run, (
        f"第二轮复活了上一轮移除的验证点: {second_run!r}"
    )
    assert OFF_TOPIC_VP not in _active_ids(executor2)
    assert OFF_TOPIC_VP not in [
        v["vp_id"] for v in executor2.collect_verdicts()
    ], "第二轮又为它产出了一条裁决"
    assert len(_audit_rows(plan_dir)) == 1, (
        "第二轮重复归档了同一条移除 —— 审计文件是只增的"
    )


# ---------------------------------------------------------------------------
# 3. 孤儿路径原样
# ---------------------------------------------------------------------------


def test_orphan_path_untouched(plan_dir: Path, verif_repo: VerificationRepository):
    """孤儿裁决照旧被隔离，且**绝不**进移除集。

    两种「孤儿」在这里：

    * 计划**没有定义**的 id（上一轮遗留的裁决）—— 走既有的隔离路径：
      不进报告、不进任何桶；
    * 计划里有它、但它**没声明任何判据**（``VP-902``）—— 它没有可以被
      推翻的主张，判定按「相关」处理。

    两条都不许变成「被移除」—— 移除的语义是「这条验证点不属于本计划」，
    孤儿的语义是「这条裁决没有判据支撑」，不是一回事。
    """
    ran: List[str] = []
    verif_repo.append_verdict(
        PLAN_ID,
        {"vp_id": "VP-777", "status": "FAILED", "reasons": ["boom"], "evidence": {}},
    )
    verif_repo._conn.commit()  # noqa: SLF001 - 测试直接落库

    executor = _executor(plan_dir, verif_repo, _runner(ran))
    asyncio.run(executor.run(max_parallel=1))

    # 计划没有定义的 id：照旧被隔离。
    collected = [v["vp_id"] for v in executor.collect_verdicts()]
    assert "VP-777" not in collected, collected
    assert "VP-777" not in executor.failed_vps, executor.failed_vps

    # 计划里有、但没声明判据的那条：照旧跑、照旧报。
    assert ORPHAN_VP in ran, ran
    assert ORPHAN_VP in collected, collected

    # 移除集里只有那一条主题外的验证点。
    rows = _audit_rows(plan_dir)
    assert [r["vp_id"] for r in rows] == [OFF_TOPIC_VP], rows
    assert load_removed_vp_ids(plan_dir) == {OFF_TOPIC_VP}


# ---------------------------------------------------------------------------
# 4. 管线顺序
# ---------------------------------------------------------------------------


def test_pipeline_order(plan_dir: Path, verif_repo: VerificationRepository):
    """判定 → 归档 → 移除 → 派生索引，四步的顺序都被钉住。

    * 判定在**执行**之前：主题外的验证点一次都没跑（跑了就是白花钱）。
    * 移除在**归档**之后：见 ``test_archive_before_remove``。
    * 索引在**移除**之后派生：被移除的点不进任何桶，其余的照常分桶。
    * 移除集**不写进** ``executor_state``：审计文件是唯一的持久化真相。
    """
    ran: List[str] = []
    executor = _executor(plan_dir, verif_repo, _runner(ran))
    asyncio.run(executor.run(max_parallel=1))

    # 判定在执行之前。
    assert OFF_TOPIC_VP not in ran, ran
    assert sorted(ran) == ["VP-901", "VP-902", "VP-903", "VP-904"], ran

    # 移除在归档之后：审计里有一行。
    assert len(_audit_rows(plan_dir)) == 1

    # 索引在移除之后派生。
    assert OFF_TOPIC_VP not in executor.completed_vps, executor.completed_vps
    assert OFF_TOPIC_VP not in executor.failed_vps, executor.failed_vps
    assert OFF_TOPIC_VP not in executor.skipped_vps, executor.skipped_vps
    assert sorted(executor.completed_vps) == [
        "VP-901", "VP-902", "VP-903", "VP-904",
    ], executor.completed_vps

    # 移除集不写进 executor_state —— 两处真相必然漂移。
    row = verif_repo.summary(PLAN_ID) or {}
    assert OFF_TOPIC_VP not in json.dumps(row.get("executor_state") or {})
