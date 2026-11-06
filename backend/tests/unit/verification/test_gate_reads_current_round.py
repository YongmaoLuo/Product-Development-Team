"""Phase-2 关卡只读**本轮**的结论（2026-11-05）。

背景
----
实测过的那个病：**这一轮 VP-001 真跑 PASSED，关卡却按上一轮那份 FAILED 判。**

关卡与报告必须用**同一份本轮结论**。一份结论是「这一次复核的结论」：上一轮
那份 FAILED 留在裁决表里（审计痕迹不丢），但它不是本轮的结论 —— 拿它替本轮
说话，等于上一轮的话在这一轮又说了一遍。

本文件钉住三条：

1. ``test_gate_reads_current_round_only`` —— 同一个验证点两轮各有一条结论时，
   关卡只读本轮那条（本轮 PASSED 就不是上一轮的 FAILED）。
2. ``test_missing_vp_not_backfilled_from_history`` —— 本轮没有的验证点，
   报告里**不出现**（不回落历史轮）。
3. ``test_reused_counts_as_current`` —— 本轮复用的结论按 PASSED 计入。

外加一条边界：``test_empty_verdicts_produce_empty_ledger`` —— 一条结论都没有
时空账本、空表，不抛。

读侧的取值口只有一条：``_current_round_verdicts()``。它给本轮那一份；本轮
没有的验证点**不回落**到历史轮 —— 回落等于「上一轮的结论替本轮说话」。

测试命令::

    cd backend && python3 -m pytest tests/unit/verification/test_gate_reads_current_round.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ``backend/`` 进 sys.path，``import verification_agent`` 才成立 ——
# 与同目录其它测试文件同一套做法，从哪个目录起 pytest 都一样。
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
from verification_agent import VerificationAgent  # noqa: E402
from verification_executor import (  # noqa: E402
    REUSED_FROM_ROUND_FIELD,
    VerificationExecutor,
    definition_fingerprint_for_vp,
)
from verification_report_md import render  # noqa: E402
from verification_round_scope import record_round_scope  # noqa: E402


pytestmark = pytest.mark.unit

#: 计划 id（每个用例一份独立的状态库，所以 id 相同也不会串行）。
PLAN_ID = "gate-reads-current-round"

#: 复用那节里那条结论负责的范围（目录前缀）。
COVERS = ("backend/verification/",)

#: 本轮改到的文件 —— 与 :data:`COVERS` **不相交**（复用得以成立的前提）。
DISJOINT_CHANGES = ("backend/server.py",)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _plan(
    *vp_ids: str, covers: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """一份只含给定验证点的计划（执行器形状）。

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


def _plan_data(*vp_ids: str) -> Dict[str, Any]:
    """给账本投影用的计划（LLM 形状）。"""
    return {
        "plan_id": PLAN_ID,
        "verification_points": [
            {"id": vp_id, "title": f"验证点 {vp_id}"} for vp_id in vp_ids
        ],
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


def _verdict(
    vp_id: str,
    status: str,
    *,
    round_no: int,
    covers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """一条落库的裁决（带轮次 —— 没有它读侧分不出本轮与历史轮）。"""
    return {
        "vp_id": vp_id,
        "status": status,
        "reasons": ["ok"],
        "evidence": {},
        "round": round_no,
        "covers_paths": list(covers or []),
    }


def _previous_verdict(
    vp_id: str, plan: Dict[str, Any], *, round_no: int = 1,
) -> Dict[str, Any]:
    """上一轮留下的一条结论。

    带着三样东西：轮次号、它负责的范围（``covers_paths``）、以及产生它的那份
    **定义指纹** —— 指纹对得上时这一轮才可能跳过它。
    """
    vp = next(v for v in plan["vps"] if v["id"] == vp_id)
    return {
        "vp_id": vp_id,
        "status": "PASSED",
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


def _agent() -> VerificationAgent:
    """一个不做任何持久化的代理（账本只需要它投影裁决）。"""
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None
    return agent


def _ledger(
    plan_data: Dict[str, Any], executor: VerificationExecutor,
) -> Dict[str, Any]:
    """关卡读到的那份账本（``execution_results``）。"""
    return _agent()._build_execution_results_from_executor(plan_data, executor)


def _summary(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """账本里每条结论的 ``(id, status)`` —— 其余字段与本文件无关。"""
    return [
        {"id": str(r.get("id")), "status": str(r.get("status"))}
        for r in results
    ]


def _render(envelope: Dict[str, Any]) -> str:
    """按**账本**渲染报告 —— 报告与关卡同源（同一份本轮结论）。"""
    report = {
        "final_verdict": "passed",
        "verification_results": envelope.get("execution_results", []),
    }
    return render(report, plan_items={}, review_state={}, repair_rows=[])


def _cells(line: str) -> List[str]:
    """按**未转义**的竖线切分一行表格。"""
    cells: List[str] = []
    current: List[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    # 首尾各有一个分隔符产生的空串，去掉。
    return cells[1:-1]


def _section(markdown: str, title: str) -> str:
    """取某一节的内容（标题行之后、下一个标题之前）。"""
    body = markdown.split(title, 1)[1]
    return body.split("\n## ", 1)[0]


def _rows(section: str, prefix: str) -> List[str]:
    """某一节里以 ``prefix`` 开头的表格行。"""
    return [line for line in section.splitlines() if line.startswith(prefix)]


# ---------------------------------------------------------------------------
# 1. 关卡只读本轮
# ---------------------------------------------------------------------------


def test_gate_reads_current_round_only(tmp_path: Path) -> None:
    """本轮 PASSED 就是本轮 PASSED —— 关卡不读上一轮那份 FAILED。

    同一个验证点两轮各有一条结论时，账本里只有**本轮**那条：这一轮 VP-001
    真跑 PASSED，上一轮那份 FAILED 不许替它说话。

    场景里另有 VP-002 —— 它只有**上一轮**的结论（这一轮没跑它）。它必须不
    进账本：跨轮那张原始裁决表里它还在（「每个验证点最新的一条」是**跨轮**
    的），关卡若直接读那张表，VP-001 的 PASSED 与 VP-002 的 FAILED 会一起
    端上桌 —— 那正是实测里「本轮 VP-001 PASSED，关卡却读到上一轮的
    FAILED」的那一幕。
    """
    plan = _plan("VP-001", "VP-002")
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _verdict("VP-001", "FAILED", round_no=1))
        repo.append_verdict(PLAN_ID, _verdict("VP-001", "PASSED", round_no=2))
        # VP-002 只有上一轮那份结论 —— 这一轮没跑它。
        repo.append_verdict(PLAN_ID, _verdict("VP-002", "FAILED", round_no=1))
        conn.commit()

        ex = _executor(_plan_dir(tmp_path), plan, repo=repo)

        # 读侧只给本轮那一份 —— 关卡与报告都从这儿取值。
        current = ex._current_round_verdicts()
        assert set(current) == {"VP-001"}
        assert current["VP-001"]["status"] == "PASSED"

        envelope = _ledger(_plan_data("VP-001", "VP-002"), ex)
        assert _summary(envelope["execution_results"]) == [
            {"id": "VP-001", "status": "PASSED"},
        ]

        # 报告与关卡同源：渲染出来的那份也是「通过」。
        rows = _rows(_section(_render(envelope), "## 验证点结果"), "| VP-001")
        assert len(rows) == 1
        assert _cells(rows[0])[2] == "通过"

        # 上一轮那份仍在裁决表里（审计痕迹不丢）—— 账本没读它。
        assert ex._verdicts_by_round[1]["VP-001"]["status"] == "FAILED"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 本轮没有的验证点不回落
# ---------------------------------------------------------------------------


def test_missing_vp_not_backfilled_from_history(tmp_path: Path) -> None:
    """本轮没有该验证点 → 报告里**不出现**（不回落历史轮）。

    回落等于「上一轮的结论替本轮说话」：这一轮根本没验过它，报告里却有一条
    上一轮的 FAILED，读者会以为这一轮验出了失败。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 2)
    try:
        # 上一轮验过，失败了；这一轮没验它。
        repo.append_verdict(PLAN_ID, _verdict("VP-001", "FAILED", round_no=1))
        conn.commit()

        ex = _executor(_plan_dir(tmp_path), plan, repo=repo)

        # 本轮视图里没有它 —— 不回落。
        assert ex._current_round_verdicts() == {}

        envelope = _ledger(_plan_data("VP-001"), ex)
        assert envelope["execution_results"] == []

        # 报告里也不出现它。
        assert "VP-001" not in _render(envelope)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 本轮复用的结论按 PASSED 计入
# ---------------------------------------------------------------------------


def test_reused_counts_as_current(tmp_path: Path) -> None:
    """本轮复用的结论按 PASSED 计入 —— 它在本轮视图里。

    复用是例外（凭四条判据），但它仍然是**本轮**的结论：这一轮不用再跑它，
    于是这一轮的账本里就有它，状态是上一轮挣来的那个 PASSED。
    """
    plan = _plan("VP-001", covers={"VP-001": list(COVERS)})
    conn, repo = _db(tmp_path, 2)
    try:
        repo.append_verdict(PLAN_ID, _previous_verdict("VP-001", plan))
        conn.commit()

        plan_dir = _plan_dir(tmp_path)
        # 变更集与该验证点的范围不相交 → 四条判据全真 → 复用。
        _write_scope(plan_dir, 2, DISJOINT_CHANGES)
        ex = _executor(plan_dir, plan, repo=repo)

        current = ex._current_round_verdicts()
        assert current["VP-001"]["status"] == "PASSED"
        assert current["VP-001"][REUSED_FROM_ROUND_FIELD] == 1

        envelope = _ledger(_plan_data("VP-001"), ex)
        assert _summary(envelope["execution_results"]) == [
            {"id": "VP-001", "status": "PASSED"},
        ]
        assert envelope["execution_results"][0][REUSED_FROM_ROUND_FIELD] == 1

        # 报告把它单列在「复用」一节里，并写明来自第几轮。
        rows = _rows(_section(_render(envelope), "## 复用"), "| VP-001")
        assert len(rows) == 1
        assert _cells(rows[0]) == ["VP-001", "通过", "复用自第 1 轮"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. 一条结论都没有
# ---------------------------------------------------------------------------


def test_empty_verdicts_produce_empty_ledger(tmp_path: Path) -> None:
    """空 verdicts → 空结果、不抛。

    本轮什么也没验出来时，账本是空的、报告是空表 —— 而不是把上一轮的结论
    顶上来充数。
    """
    plan = _plan("VP-001")
    conn, repo = _db(tmp_path, 2)
    try:
        ex = _executor(_plan_dir(tmp_path), plan, repo=repo)

        assert ex._current_round_verdicts() == {}

        envelope = _ledger(_plan_data("VP-001"), ex)
        assert envelope["execution_results"] == []

        markdown = _render(envelope)
        assert "## 验证点结果" in markdown
        assert "VP-001" not in markdown
    finally:
        conn.close()
