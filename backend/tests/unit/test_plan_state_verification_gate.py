"""验证分支的「进入验证」门禁与 PRD skip 路径。

背景
----
验证分支上**没有执行产出**：它带一份计划书直接进验证。因此状态机里那两道
「必须先执行完」的判据 —— :meth:`PlanState.transition_to` 的阶段门禁与
:meth:`PlanState.start_verification` 的门禁 —— 在验证分支上恒为假，验证分支
永远进不了验证。

改动之后的判据是「``execution`` 在 ``completed_phases`` 里 **或** 验证分支已就绪」
（:func:`verification_gate.verification_branch_ready`）：开发分支的右侧恒假，
原判定逐字生效；验证分支走另一条路。两道门禁问的是同一件事，所以两处一起改。

PRD skip 路径
-------------
验证分支的最小 PRD 允许整条 skip，于是 PRD 审完之后需要一条
``prd_review → tasks_generation`` 的边。这条边**不进**通用转移表：通用入口
（``POST /api/plan/{id}/phase``）只认主表与验证子表，不认它；只有专用入口
:meth:`PlanState.start_verification_plan_generation` 走这条边。

「是否已解决」由 :func:`verification_plan_review.prd_is_resolved` 回答，它自己
按分支区分 skip 的语义：验证分支的 ``skipped`` 算数，开发分支只认 ``accepted``。

钉住的四件事
------------
* ``test_verification_branch_skips_execution_gate`` —— 基线 + 审核完备 → 进验证；
* ``test_dev_branch_gate_unchanged`` —— 开发分支没有 ``execution`` → 仍然拒绝；
* ``test_prd_skip_only_reachable_via_dedicated_entry`` —— 通用入口不可达、专用入口可达；
* ``test_dev_prd_with_skip_still_incomplete`` —— 开发分支的 ``skipped`` 不算数。

测试数据
--------
真实 git 仓库（``refs/pull/7/head``）与真实的计划书 / 审核写入路径：基线冻结在
计划状态里，审核条目用 :func:`record_action` 落盘 —— 与生产路径同一条。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import (  # noqa: E402
    PHASE_TRANSITIONS,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)
from tests.fixtures.verification_mode import (  # noqa: E402
    build_plan_doc,
    build_upstream_repo,
)
from verification_plan_doc import parse_plan_doc  # noqa: E402
from verification_plan_review import extract_items, record_action  # noqa: E402


# ---------------------------------------------------------------------------
# 计划状态与产物的写入助手
# ---------------------------------------------------------------------------


def _write_plan_state(
    plan_dir: Path,
    phase: str,
    completed_phases: Optional[List[str]] = None,
    verification: Optional[Dict[str, Any]] = None,
) -> None:
    """把计划直接放在某个阶段上。

    写的是 ``plan_routing`` 那一行：阶段、已完成阶段、审核轮次、标志位与
    ``verification`` 段（基线就冻在这里）。读侧
    :meth:`PlanState._load_state_from_sqlite` 读的也是这一行 —— 测试与生产
    走同一条路。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_dir.name,
            phase=phase,
            completed_phases=list(completed_phases or []),
            review_rounds={},
            flags={},
            verification=verification
            if verification is not None
            else {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
            last_updated=None,
        )
    finally:
        conn.close()


def _write_prd_review(plan_dir: Path, status: str) -> None:
    """写一份带**一个**决策点的 PRD 与它的审核结果。

    ``status`` 是那个决策点的审核状态（``accepted`` / ``skipped``）。
    """
    (plan_dir / "prd.json").write_text(
        json.dumps({"decision_points": [{"index": 0, "title": "凭据校验方式"}]}),
        encoding="utf-8",
    )
    (plan_dir / "review.json").write_text(
        json.dumps({
            "items": [{"index": 0, "title": "凭据校验方式", "status": status, "note": "理由"}],
            "total": 1,
        }),
        encoding="utf-8",
    )


def _write_verification_plan(plan_dir: Path, repo_dir: Path) -> Path:
    """放一份验证计划书，并把全部审核条目标为 accepted。

    返回计划书路径。
    """
    doc = build_plan_doc(repo_dir)
    doc_path = plan_dir / "verification-plan.md"
    doc_path.write_text(doc, encoding="utf-8")
    for item in extract_items(parse_plan_doc(doc)):
        record_action(plan_dir, item.index, "accept")
    return doc_path


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每次用例一份自己的状态库。"""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def upstream(tmp_path: Path):
    """一个真的 git 仓库，``refs/pull/7/head`` 指向第二个提交。"""
    return build_upstream_repo(tmp_path / "upstream")


# ---------------------------------------------------------------------------
# 1) 验证分支越过执行门禁
# ---------------------------------------------------------------------------


def test_verification_branch_skips_execution_gate(
    tmp_path: Path, state_db: Path, upstream
) -> None:
    """基线 + 审核完备 → 进验证，不需要 ``execution``。

    这是验证分支的正常路径：没有执行产出，计划书审完就直接进验证。
    """
    plan_dir = tmp_path / "plan-verification"
    plan_dir.mkdir()
    _write_verification_plan(plan_dir, upstream.repo)
    _write_plan_state(
        plan_dir,
        "verification_plan_review",
        completed_phases=[],
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
            "baseline": {
                "repo_dir": str(upstream.repo),
                "ref": upstream.pr_ref,
                "commit_sha": upstream.head_sha(),
                "pr_number": upstream.pr_number,
            },
        },
    )

    state = PlanState(plan_dir)
    state.start_verification()

    assert state.get_current_phase() == "verification", (
        "验证分支在基线与审核完备时必须能进验证；"
        f"实际停在 {state.get_current_phase()!r}"
    )
    assert "execution" not in state.get_completed_phases(), (
        "验证分支没有执行产出：completed_phases 里不该凭空多出 'execution'；"
        f"实际有 {state.get_completed_phases()!r}"
    )
    # 落盘也是「验证」：重读一次仍然是这个阶段。
    assert PlanState(plan_dir).get_current_phase() == "verification"


# ---------------------------------------------------------------------------
# 2) 开发分支的门禁逐字不变（负控）
# ---------------------------------------------------------------------------


def test_dev_branch_gate_unchanged(tmp_path: Path, state_db: Path) -> None:
    """开发分支没有 ``execution`` → 仍然拒绝进验证。

    负控。门禁的右侧（验证分支就绪）在开发分支上恒假，所以原判定逐字生效：
    两道门禁都必须继续拒绝。
    """
    plan_dir = tmp_path / "plan-dev"
    plan_dir.mkdir()
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")
    _write_plan_state(plan_dir, "ready", completed_phases=[])

    state = PlanState(plan_dir)

    with pytest.raises(ValueError, match="without 'execution' in completed_phases"):
        state.transition_to("verification")
    assert state.get_current_phase() == "ready", (
        f"被拒绝的转移不许改阶段；实际停在 {state.get_current_phase()!r}"
    )

    with pytest.raises(ValueError, match="Cannot start verification"):
        state.start_verification()
    assert state.get_current_phase() == "ready", (
        f"被拒绝的转移不许改阶段；实际停在 {state.get_current_phase()!r}"
    )


# ---------------------------------------------------------------------------
# 3) PRD skip 路径只有专用入口能走
# ---------------------------------------------------------------------------


def test_prd_skip_only_reachable_via_dedicated_entry(
    tmp_path: Path, state_db: Path
) -> None:
    """通用入口不可达；专用入口可达。

    PRD 全部 skip 的验证分支要能进计划书生成，但这条边不进通用转移表 ——
    通用入口（``POST /api/plan/{id}/phase``）不许用它。
    """
    plan_dir = tmp_path / "plan-skip"
    plan_dir.mkdir()
    (plan_dir / "verification-plan.md").write_text(
        "# 验证计划书\n\n最小计划书。\n", encoding="utf-8"
    )
    _write_prd_review(plan_dir, "skipped")
    _write_plan_state(plan_dir, "prd_review", completed_phases=[])

    state = PlanState(plan_dir)

    # 通用入口：转移表里没有这条边 → 不可达。
    with pytest.raises(ValueError, match="Illegal transition"):
        state.transition_to("tasks_generation")
    assert state.get_current_phase() == "prd_review", (
        f"被拒绝的转移不许改阶段；实际停在 {state.get_current_phase()!r}"
    )

    # 通用入口用的目标表里也不该有这条边。
    assert "tasks_generation" not in PHASE_TRANSITIONS["prd_review"], (
        f"通用转移表不许新增 prd_review → tasks_generation；"
        f"实际有 {PHASE_TRANSITIONS['prd_review']!r}"
    )
    assert "tasks_generation" not in VERIFICATION_PHASE_TRANSITIONS.get(
        "prd_review", []
    ), (
        "验证子表也不许新增 prd_review → tasks_generation；"
        f"实际有 {VERIFICATION_PHASE_TRANSITIONS.get('prd_review', [])!r}"
    )

    # 专用入口：可达。
    state.start_verification_plan_generation()
    assert state.get_current_phase() == "tasks_generation", (
        "专用入口必须能走到 tasks_generation；"
        f"实际停在 {state.get_current_phase()!r}"
    )


# ---------------------------------------------------------------------------
# 4) 开发分支的 skip 不算数（负控）
# ---------------------------------------------------------------------------


def test_dev_prd_with_skip_still_incomplete(tmp_path: Path, state_db: Path) -> None:
    """开发分支含 ``skipped`` → PRD 仍然不完整，专用入口不放行。

    负控。skip 的语义按分支区分：验证分支的最小 PRD 允许整条 skip，开发分支
    只认 ``accepted``。
    """
    plan_dir = tmp_path / "plan-dev-skip"
    plan_dir.mkdir()
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")
    _write_prd_review(plan_dir, "skipped")
    _write_plan_state(plan_dir, "prd_review", completed_phases=[])

    state = PlanState(plan_dir)

    with pytest.raises(ValueError):
        state.start_verification_plan_generation()
    assert state.get_current_phase() == "prd_review", (
        f"被拒绝的转移不许改阶段；实际停在 {state.get_current_phase()!r}"
    )
