"""2026-10-20 的四种历史形状：阶段门禁不再把它们拒在验证之外。

背景
----
2026-10-20 有四条计划在「进验证」这一步炸了，报的是
``Cannot transition to 'verification_running' without 'execution' in
completed_phases``。当时的阶段门禁只有一条判据：``execution`` 必须在
``completed_phases`` 里。

2026-10-25 门禁加宽成两条（:meth:`PlanState._execution_gate_open`）：

* **开发分支**：``execution`` 在 ``completed_phases`` 里；
* **验证分支**：没有执行产出，改问「基线已冻结且审核完备」
  （:func:`verification_gate.verification_branch_ready`）。

那四条计划是历史记录，不会再跑一遍；**目前没有任何测试钉住「当前不会再
触发」**。这个文件就是那把锁：把四种形状各写成一行状态，复刻它们当时走过的
路径，证明每一条现在都能进 ``verification_running``。

三种形状，**假设**
------------------
⚠️ **下面四种形状是按材料推断出来的** —— 原始材料没有逐条列出 2026-10-20
那四条计划各自的状态行。所以本文件把它们当作「当时可能出现的形状」钉住；
将来若拿到原始状态行，应按原始材料补齐用例，而不是反过来让原始材料迁就
本文件的假设。

推断出的四种是：

1. ``executing → completed → verification`` 重入：执行完走终态，再从终态
   重入验证子机器；
2. :meth:`PlanState.begin_verification` 从 ``executing`` 进入：绕开转移表直接
   进验证；
3. 终态迁移自动补 ``execution``：迁移到终态时状态机把 ``execution`` 补进
   ``completed_phases``，之后的验证入口因此放行；
4. 早先完成过 ``execution`` 再回验证：``completed_phases`` 里已经有
   ``execution``（上一轮跑出来的），计划又回到验证。

三条门禁判据
------------
本文件钉住的是**同一个门禁**（:meth:`PlanState._execution_gate_open`）的两条
合法路径与一条非法路径：

* 开发分支行（``phase="verification"`` + ``completed_phases=["execution"]``）
  → 进 ``verification_running`` 不抛；
* 验证分支行（``completed_phases=[]`` + 冻结基线 + 审核完备）→ 进
  ``verification_running`` 不抛，且**不凭空多出** ``execution``；
* 非法行（既无 ``execution`` 又无基线）→ 抛 ``ValueError``，**阶段不变**。

测试数据
--------
与 :mod:`test_plan_state_verification_gate` 同一套：本地 ``state_db``
（monkeypatch ``PDT_STATE_DB_PATH``）与真的 git 仓库
（``tests.fixtures.verification_mode.build_upstream_repo``）。状态行写在
``plan_routing`` 那一行 —— 读侧
:meth:`PlanState._load_state_from_sqlite` 读的也是这一行，测试与生产走
同一条路。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import PlanState  # noqa: E402
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
    verification: Optional[Dict[str, object]] = None,
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


def _write_dev_marker(plan_dir: Path) -> None:
    """放一份开发任务清单 —— 这条计划走**开发分支**。

    ``plan_artifacts.detect`` 只看目录里有哪类产物：有 ``tasks.json`` 就是
    开发分支（先执行再验证），有 ``verification-plan.md`` 就是验证分支
    （跳过执行直接验证）。两份都在是冲突，两份都不在是「空」。
    """
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")


def _write_verification_plan(plan_dir: Path, repo_dir: Path) -> Path:
    """放一份验证计划书，并把全部审核条目标为 accepted。

    返回计划书路径。审核记录走 :func:`record_action` —— 与生产路径同一条。
    """
    doc = build_plan_doc(repo_dir)
    doc_path = plan_dir / "verification-plan.md"
    doc_path.write_text(doc, encoding="utf-8")
    for item in extract_items(parse_plan_doc(doc)):
        record_action(plan_dir, item.index, "accept")
    return doc_path


def _frozen_baseline(upstream) -> Dict[str, object]:
    """审核冻结下来的基线：仓库、ref、commit、PR 号。"""
    return {
        "repo_dir": str(upstream.repo),
        "ref": upstream.pr_ref,
        "commit_sha": upstream.head_sha(),
        "pr_number": upstream.pr_number,
    }


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每次用例一份自己的状态库。

    刻意**不依赖全局 conftest**：这个文件自己 monkeypatch
    ``PDT_STATE_DB_PATH``，所以单独跑它、或者被别的 conftest 换了状态库，
    读写的都还是这一份临时的库。
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def upstream(tmp_path: Path):
    """一个真的 git 仓库，``refs/pull/7/head`` 指向第二个提交。"""
    return build_upstream_repo(tmp_path / "upstream")


# ---------------------------------------------------------------------------
# 1) 开发分支：``execution`` 在 completed_phases 里
# ---------------------------------------------------------------------------


def test_dev_plan_with_execution_reaches_verification_running(
    tmp_path: Path, state_db: Path
) -> None:
    """开发分支行 → 进 ``verification_running`` 不抛。

    门禁的左半边：``execution`` 已经在 ``completed_phases`` 里，判定为真。
    """
    plan_dir = tmp_path / "plan-dev-execution"
    plan_dir.mkdir()
    _write_dev_marker(plan_dir)
    _write_plan_state(plan_dir, "verification", completed_phases=["execution"])

    state = PlanState(plan_dir)

    assert state._execution_gate_open() is True, (
        "development branch with 'execution' in completed_phases must open the gate"
    )

    state.transition_to("verification_running")

    assert state.get_current_phase() == "verification_running", (
        "development branch must reach verification_running; "
        f"actually stopped at {state.get_current_phase()!r}"
    )


# ---------------------------------------------------------------------------
# 2) 验证分支：没有 execution，靠冻结基线 + 审核完备
# ---------------------------------------------------------------------------


def test_verification_branch_without_execution_is_allowed(
    tmp_path: Path, state_db: Path, upstream
) -> None:
    """验证分支行 → 进 ``verification_running`` 不抛，且**不凭空多出** execution。

    门禁的右半边：这条计划压根没有执行产出，它靠「基线冻结 + 审核完备」放行。
    验证分支**不许**因为进了验证就被记上一条它没做过的 ``execution``。
    """
    plan_dir = tmp_path / "plan-verification-branch"
    plan_dir.mkdir()
    _write_verification_plan(plan_dir, upstream.repo)
    _write_plan_state(
        plan_dir,
        "verification",
        completed_phases=[],
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
            "baseline": _frozen_baseline(upstream),
        },
    )

    state = PlanState(plan_dir)

    assert "execution" not in state.get_completed_phases(), (
        "the verification branch has no execution output; "
        f"actually got {state.get_completed_phases()!r}"
    )
    assert state._execution_gate_open() is True, (
        "a ready verification branch (frozen baseline + complete review) "
        "must open the gate even without 'execution'"
    )

    state.transition_to("verification_running")

    assert state.get_current_phase() == "verification_running", (
        "the verification branch must reach verification_running; "
        f"actually stopped at {state.get_current_phase()!r}"
    )
    assert "execution" not in state.get_completed_phases(), (
        "the verification branch must not grow a synthetic 'execution' phase; "
        f"actually got {state.get_completed_phases()!r}"
    )


# ---------------------------------------------------------------------------
# 3) 非法形状：既没有 execution，也没有基线 → 仍然拒绝
# ---------------------------------------------------------------------------


def test_illegal_shape_is_still_rejected(tmp_path: Path, state_db: Path) -> None:
    """非法行 → 抛 ``ValueError``，**阶段不变**。

    负控。门禁加宽的是**合法路径**，不是把门拆了：没有执行产出、又没冻结
    基线的计划仍然进不了验证。
    """
    plan_dir = tmp_path / "plan-illegal"
    plan_dir.mkdir()
    _write_dev_marker(plan_dir)
    _write_plan_state(plan_dir, "verification", completed_phases=[])

    state = PlanState(plan_dir)

    assert state._execution_gate_open() is False, (
        "neither 'execution' nor a ready verification branch -> gate stays closed"
    )

    with pytest.raises(ValueError, match="without 'execution' in completed_phases"):
        state.transition_to("verification_running")

    assert state.get_current_phase() == "verification", (
        "a rejected transition must not change the phase; "
        f"actually stopped at {state.get_current_phase()!r}"
    )
    assert "execution" not in state.get_completed_phases(), (
        "a rejected transition must not change the phase history; "
        f"actually got {state.get_completed_phases()!r}"
    )


# ---------------------------------------------------------------------------
# 4) 四种历史形状（**假设**，见模块 docstring）
# ---------------------------------------------------------------------------


def _drive_exec_completed_reentry(state: PlanState) -> None:
    """形状 1：executing → completed → verification 重入。"""
    state.transition_to("completed")
    state.transition_to("verification")


def _drive_begin_verification_from_executing(state: PlanState) -> None:
    """形状 2：:meth:`PlanState.begin_verification` 从 executing 进入。"""
    state.begin_verification()


def _drive_terminal_migration(state: PlanState) -> None:
    """形状 3：终态迁移自动补 ``execution``，再从终态重入验证。"""
    state.transition_to("failed")
    state.begin_verification()


def _drive_already_completed(state: PlanState) -> None:
    """形状 4：早先完成过 ``execution`` 再回验证（无需迁移）。"""
    return None


@dataclass(frozen=True)
class _HistoricalShape:
    """一条历史形状：起始状态行 + 到达验证相位要走的那几步。

    Attributes:
        name: 形状名（参数化 id）。
        phase: 起始相位。
        completed_phases: 起始的已完成阶段。
        drive: 从起始相位走到「可以进 ``verification_running``」的那几步。
    """

    name: str
    phase: str
    completed_phases: List[str]
    drive: Callable[[PlanState], None]


#: 四种历史形状 —— **推断**，不是原始材料逐条列出的（见模块 docstring）。
_HISTORICAL_SHAPES: List[_HistoricalShape] = [
    _HistoricalShape(
        name="executing-completed-reentry",
        phase="executing",
        completed_phases=[],
        drive=_drive_exec_completed_reentry,
    ),
    _HistoricalShape(
        name="begin-verification-from-executing",
        phase="executing",
        completed_phases=[],
        drive=_drive_begin_verification_from_executing,
    ),
    _HistoricalShape(
        name="terminal-migration-adds-execution",
        phase="executing",
        completed_phases=[],
        drive=_drive_terminal_migration,
    ),
    _HistoricalShape(
        name="execution-completed-earlier",
        phase="verification",
        completed_phases=["execution"],
        drive=_drive_already_completed,
    ),
]


@pytest.mark.parametrize(
    "shape",
    _HISTORICAL_SHAPES,
    ids=[shape.name for shape in _HISTORICAL_SHAPES],
)
def test_the_four_historical_shapes_are_admitted(
    tmp_path: Path, state_db: Path, shape: _HistoricalShape
) -> None:
    """四种历史形状各走一遍 → 全部能进 ``verification_running``。

    这些形状当年全部撞在同一句 ``without 'execution' in completed_phases``
    上。现在每一条都放行 —— 这就是这把锁要钉住的东西。
    """
    plan_dir = tmp_path / f"plan-{shape.name}"
    plan_dir.mkdir()
    _write_dev_marker(plan_dir)
    _write_plan_state(
        plan_dir,
        shape.phase,
        completed_phases=list(shape.completed_phases),
    )

    state = PlanState(plan_dir)
    shape.drive(state)

    state.transition_to("verification_running")

    assert state.get_current_phase() == "verification_running", (
        f"historical shape {shape.name!r} must reach verification_running; "
        f"actually stopped at {state.get_current_phase()!r}"
    )
    assert "execution" in state.get_completed_phases(), (
        f"historical shape {shape.name!r} is a development-branch shape: "
        f"'execution' must be recorded; actually got "
        f"{state.get_completed_phases()!r}"
    )