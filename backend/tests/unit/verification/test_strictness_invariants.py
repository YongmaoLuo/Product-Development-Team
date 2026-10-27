"""TDD spec —— 主题外移除的严格度不变量（故障矩阵）。

为什么要有这张矩阵
------------------
移除一条验证点是一次**严格度损失**：报告里少一条、这一轮少跑一次、那条
验证点想验的东西从此没人验。所以移除必须由**两个独立条件**同时成立才发生：

1. **判定说它主题外** —— 模型明说无关（判定内核的 ``llm_irrelevant``）；
2. **审计写成功** —— 归档先行，写不进去就不许移除。

任何一条不成立，那条验证点都必须**留在活动集合里**。这是「fail-open 向
严格」：判断不了就不动它，宁可多验一次（多花点钱）也不要少验一条（丢掉
一个结论）。

这张矩阵把两个条件、三种模型行为、六种验证点状态交叉起来 ——
3 × 3 × 6 = **54 组**，每一组都断言同一件事：

    被移除  ⟺  判定说无关 **且** 审计写成功

TDD contract —— 2 cases
------------------------
1. ``test_fault_matrix_keeps_vp`` —— 54 组全过；任何一组把验证点误移除了，
   或者该移除的没移除，测试都失败（并把全部出错组合一次列出来）。
2. ``test_reset_state_is_not_revived_by_hydration`` —— 清空之后不许被
   「装载裁决」复活（同一族的「不复活」不变量）。

为什么是「一个测试跑 54 组」而不是 54 个参数化用例
--------------------------------------------------
参数化会把 54 组拆成 54 个独立用例，**第一个坏组合就把后面的全挡住了** ——
而这张矩阵的价值恰恰在于「一次看清哪些组合坏了」。所以这里在一个用例里
跑完全部组合，把失败的组合收集起来一次报出。

边界条件
--------
* 审计写失败（``OSError`` / ``TypeError``）→ 验证点**留在**活动集合里。
* 模型不可用（``llm_batch=None``）且没有确定性矛盾 → 验证点**留在**活动
  集合里（判定内核按「相关」兜底）。
* 计划**没有定义**的 id（孤儿）→ 既不进移除集，也不进审计文件。
* ``reset_state()`` 清空之后的那一轮 → 旧结论**不许**被「装载裁决」读回来
  （清空等于没做的话，这条契约就是空话）。

测试命令::

    pytest backend/tests/unit/verification/test_strictness_invariants.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

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


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 矩阵的三根轴
# ---------------------------------------------------------------------------

#: 模型行为。
#:
#: * ``irrelevant`` —— 模型明说这条验证点与计划主题无关；
#: * ``relevant`` —— 模型明说相关；
#: * ``unavailable`` —— 没有模型可问（``llm_batch=None``），判定内核按
#:   「相关」兜底。
LLM_BEHAVIORS = ("irrelevant", "relevant", "unavailable")

#: 归档写入的结果。
#:
#: * ``ok`` —— 写成功；
#: * ``oserror`` —— 磁盘满 / 只读（``OSError``）；
#: * ``typeerror`` —— 记录里有不可序列化的东西（``TypeError``）。
AUDIT_OUTCOMES = ("ok", "oserror", "typeerror")

#: 验证点在**这一轮开始时**的状态。
#:
#: * ``pending`` —— 还没有裁决；
#: * ``passed`` / ``failed`` / ``skipped`` / ``deferred`` —— 上一轮留下的裁决；
#: * ``not_in_plan`` —— 计划**没有定义**这个 id（孤儿裁决）。
VP_STATES = ("pending", "passed", "failed", "skipped", "deferred", "not_in_plan")

#: 被测的那条验证点。
SUBJECT_VP = "VP-TARGET"

#: ``not_in_plan`` 那一组里计划**确实**定义的那条（它声明了计划里有的
#: 判据，永远不会被判定、也不会被移除 —— 免得它污染审计文件）。
PLAN_ONLY_VP = "VP-OTHER"

#: 计划自己的判据表（编号式）。
CRITERIA: List[Dict[str, str]] = [
    {"id": "AC-001", "text": "用户可以用邮箱和密码登录"},
]

#: 被测验证点声明的判据：**散文式**，既不是编号（所以不构成「引用了计划
#: 里没有的判据」这条确定性矛盾），也不与判据正文互相包含。
#:
#: 这样一来，这 54 组里唯一能触发移除的就是「模型明说无关」—— 矩阵测的是
#: 判定与归档这两道闸，不掺第三种理由。
SUBJECT_DECLARATION = "与本计划主题无关的一条散文式声明"

#: 计划 id（每个组合一份独立的状态库，所以 id 相同也不会串行）。
PLAN_ID = "vp-strictness-matrix"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _subject_point() -> Dict[str, Any]:
    return {
        "id": SUBJECT_VP,
        "title": "被测的那条验证点",
        "related_prd_criteria": [SUBJECT_DECLARATION],
        "verification_method": "api_test",
        "verification_phase": 1,
        "priority": "medium",
        "expected_result": "这条验证点验的是与本计划主题无关的事",
    }


def _plan_only_point() -> Dict[str, Any]:
    return {
        "id": PLAN_ONLY_VP,
        "title": "计划里确实有的那条",
        "related_prd_criteria": ["AC-001"],
        "verification_method": "api_test",
        "verification_phase": 1,
        "priority": "medium",
        "expected_result": "这条验证点验的是计划里的一条判据",
    }


def _plan(state: str) -> Dict[str, Any]:
    """被测组合的计划。``not_in_plan`` 那一组里计划不含被测 id。"""
    points = [_plan_only_point()] if state == "not_in_plan" else [_subject_point()]
    return {
        "plan_id": PLAN_ID,
        "vps": points,
        "criteria": [dict(c) for c in CRITERIA],
    }


def _llm_batch(relevant: bool):
    """一个说「相关 / 无关」的批量判定器。"""

    def _batch(vps, criteria):
        return {
            str(vp.get("id")): {
                "relevant": relevant,
                "reason": "与本计划的主题无关" if not relevant else "在本计划的主题内",
            }
            for vp in vps
        }

    return _batch


def _runner(ran: List[str]):
    async def _run(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        ran.append(vp_node["id"])
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    return _run


def _seeded_verdict(state: str) -> Optional[Dict[str, Any]]:
    """这一轮开始时，那条验证点已经带着的裁决。"""
    if state == "pending":
        return None
    status = {
        "passed": "PASSED",
        "failed": "FAILED",
        "skipped": "SKIPPED",
        "deferred": "DEFERRED",
        "not_in_plan": "FAILED",
    }[state]
    return {
        "vp_id": SUBJECT_VP,
        "status": status,
        "reasons": ["上一轮留下的结论"],
        "evidence": {},
    }


def _active_ids(executor: VerificationExecutor) -> List[str]:
    return [str(vp.get("id")) for vp in executor._build_items_from_plan()]


def _audit_rows(plan_dir: Path) -> List[Dict[str, Any]]:
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
# 故障矩阵
# ---------------------------------------------------------------------------


def test_fault_matrix_keeps_vp(tmp_path: Path):
    """54 组：被移除 ⟺ 判定说无关 **且** 审计写成功。

    任何一组不满足，两边都要被指出来 —— 误移除（严格度受损）与漏移除
    （该跑的没跑）都是 bug，只是方向相反。
    """
    failures: List[str] = []

    for llm in LLM_BEHAVIORS:
        for audit in AUDIT_OUTCOMES:
            for state in VP_STATES:
                case = f"llm={llm} audit={audit} state={state}"
                try:
                    _assert_one_case(tmp_path, llm, audit, state)
                except AssertionError as exc:  # noqa: PERF203 - 矩阵要跑满
                    failures.append(f"{case}: {exc}")

    assert not failures, (
        "严格度不变量被打破了（{0} 组）：\n{1}".format(
            len(failures), "\n".join(failures),
        )
    )


def _assert_one_case(
    tmp_path: Path,
    llm: str,
    audit: str,
    state: str,
) -> None:
    """跑一组组合，断言它落在「该移除 / 不该移除」的哪一边。"""
    case_dir = tmp_path / f"{llm}-{audit}-{state}"
    plan_dir = case_dir / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True)

    conn = open_db(case_dir / "state.db")
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(PLAN_ID, verification_status="running")
        seeded = _seeded_verdict(state)
        if seeded is not None:
            repo.append_verdict(PLAN_ID, dict(seeded))
        conn.commit()

        llm_batch = None if llm == "unavailable" else _llm_batch(llm == "relevant")
        ran: List[str] = []
        executor = VerificationExecutor(
            verification_plan=_plan(state),
            plan_id=PLAN_ID,
            plan_dir=plan_dir,
            sub_agent_runner=_runner(ran),
            verif_repo=repo,
            topic_relevance_llm_batch=llm_batch,
        )

        archive_error: Optional[Exception] = None
        if audit == "oserror":
            archive_error = OSError("disk full")
        elif audit == "typeerror":
            archive_error = TypeError("record is not serialisable")

        if archive_error is not None:
            with patch.object(
                vp_removal_audit, "archive_removal", side_effect=archive_error,
            ):
                asyncio.run(executor.run(max_parallel=1))
        else:
            asyncio.run(executor.run(max_parallel=1))

        removed_ids = load_removed_vp_ids(plan_dir)
        rows = _audit_rows(plan_dir)
        active = _active_ids(executor)

        expected_removed = (
            llm == "irrelevant" and audit == "ok" and state != "not_in_plan"
        )

        if expected_removed:
            assert SUBJECT_VP in removed_ids, (
                f"该移除的没有移除（removed={sorted(removed_ids)!r}）"
            )
            assert [r.get("vp_id") for r in rows] == [SUBJECT_VP], (
                f"审计行不对：{rows!r}"
            )
            assert SUBJECT_VP not in active, (
                f"移除后它还在活动集合里：{active!r}"
            )
        else:
            # 不该移除的那一半：验证点必须留在活动集合里。
            assert SUBJECT_VP not in removed_ids, (
                f"不该移除的进了移除集（removed={sorted(removed_ids)!r}）"
            )
            assert rows == [], f"不该移除的却写了审计行：{rows!r}"
            if state != "not_in_plan":
                assert SUBJECT_VP in active, (
                    f"不该移除的却离开了活动集合：{active!r}"
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 清空之后不许被装载复活
# ---------------------------------------------------------------------------


def test_reset_state_is_not_revived_by_hydration(tmp_path: Path):
    """``reset_state()`` 之后的 run 必须从零开始。

    「装载裁决」这一步把持久化存储里的旧结论读回内存 —— 刚被
    ``reset_state()`` 清掉的结论若在这一步回来，那条验证点会被当成「上一轮
    已经验过」而跳过：清空等于没做。

    这与「第二轮不复活被移除的验证点」是同一族的不变量：**已经被判定为
    「不需要了」的东西，不许自己回来。**
    """
    case_dir = tmp_path / "reset-state"
    plan_dir = case_dir / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True)

    conn = open_db(case_dir / "state.db")
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(PLAN_ID, verification_status="running")
        conn.commit()

        ran: List[str] = []
        executor = VerificationExecutor(
            verification_plan=_plan("pending"),
            plan_id=PLAN_ID,
            plan_dir=plan_dir,
            sub_agent_runner=_runner(ran),
            verif_repo=repo,
        )

        # 第一轮：跑一遍，结论落库。
        asyncio.run(executor.run(max_parallel=1))
        assert ran == [SUBJECT_VP], ran

        # 清空 —— 契约是「下一次 run 从零开始」。
        executor.reset_state()

        # 第二轮：必须重跑。
        asyncio.run(executor.run(max_parallel=1))
        assert ran == [SUBJECT_VP, SUBJECT_VP], (
            f"清空之后第二轮没有重跑 —— 旧结论被装载回来了: {ran!r}"
        )
    finally:
        conn.close()
