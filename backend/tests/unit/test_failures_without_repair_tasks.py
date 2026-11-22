"""有失败、但一条修复任务都没有 → 交给人工裁定，而不是静默空转。

2026-11-22。一轮验证判了一个点 FAILED，修复任务生成器却产出 0 条任务
（``tasks: []``、``needs_operator_decision: false``）。自动循环把这条路
读成「没什么要修的」，于是回到 ``executing``；执行器发现没活可干，退出来；
链子又回到验证 —— 一个**静默的空转闭环**：计划永远不收敛，也永远不说
为什么。

空修复队列有两种完全不同的含义：

  * 「没什么要修的」（本轮过了，或该修的都修完了）→ 老路：回到
    ``executing`` 排空剩余工作。**零回归**，这里必须钉住。
  * 「有失败，却没人把它变成一条任务」→ 交给人工裁定：写一条终态
    判决，把计划交出去。

本文件用 mock 替换 ``_dead_end_terminal`` / ``confirm_repair_and_rerun``，
不启动真的执行器，也不启动真的验证轮。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server  # noqa: E402
import verification_loop as vl  # noqa: E402
import round_process_ledger  # noqa: E402


class _World:
    """Everything the auto-loop touches, recorded as it happens."""

    def __init__(self, plan_id: str, plan_dir: Path, project_dir: Path):
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        #: every ``_dead_end_terminal`` call
        self.dead_ends: List[Dict[str, Any]] = []
        #: every ``confirm_repair_and_rerun`` call
        self.confirmed: List[str] = []
        #: every repair-execution hand-off
        self.hand_offs: List[Dict[str, Any]] = []
        #: the report the round produces
        self.report: Dict[str, Any] = {"verification_results": []}
        #: what the orchestrator's judgment phase returns
        self.result: Dict[str, Any] = {
            "status": "verification_failed",
            "repair_tasks": [],
            "stop_reason": None,
        }


def _orchestrator_class(world: _World):
    class _Orchestrator:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def check_cycle_conditions(
            self, _report: Any, round_number: Optional[int] = None,
        ) -> Dict[str, Any]:
            return dict(world.result)

        def confirm_repair_and_rerun(self) -> None:
            world.confirmed.append(world.plan_id)

        def _extract_failed_ids(self, report: Any) -> set:
            """The orchestrator's own notion of "failed"."""
            if not isinstance(report, dict):
                return set()
            return {
                str(r.get("id"))
                for r in (report.get("verification_results") or [])
                if isinstance(r, dict) and r.get("status") == "FAILED"
            }

    return _Orchestrator


@pytest.fixture
def world(monkeypatch: Any, tmp_path: Path):
    """Wire the auto-loop's world and hand back the recorder."""
    plan_id = "p-failures-without-repair-tasks"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    w = _World(plan_id, plan_dir, project_dir)

    round_process_ledger.reset_for_tests()

    # --- the plan ---------------------------------------------------------
    monkeypatch.setattr(server, "_plan_dir", lambda _pid: plan_dir)
    monkeypatch.setattr(
        server, "_state_db_path", lambda: tmp_path / "state.db",
    )
    monkeypatch.setattr(
        server, "PlanState", lambda *_a, **_kw: MagicMock(),
    )
    monkeypatch.setattr(
        server, "create_coding_tool", lambda *_a, **_kw: MagicMock(),
    )

    def _no_verification_state():
        raise RuntimeError("this test has no verification state")

    monkeypatch.setattr(
        server, "_open_verification_state", _no_verification_state,
    )

    # --- the orchestrator -------------------------------------------------
    monkeypatch.setattr(
        server, "VerificationOrchestrator", _orchestrator_class(w),
    )

    # --- the terminal write ----------------------------------------------
    monkeypatch.setattr(
        server, "_record_verification_terminal",
        lambda *_a, **_kw: None,
    )

    def _hand_off(*_a: Any, **kwargs: Any) -> None:
        w.hand_offs.append(kwargs)

    monkeypatch.setattr(
        server, "_run_repair_execution_async", _hand_off,
    )

    # --- the dead end -----------------------------------------------------
    def _dead_end(
        pid: str, reason: str, **kwargs: Any,
    ) -> None:
        w.dead_ends.append({"plan_id": pid, "reason": reason, **kwargs})

    monkeypatch.setattr(vl, "_dead_end_terminal", _dead_end)

    # --- the round --------------------------------------------------------
    def _dispatch(
        _pid: str, _plan_dir: Path, _orch: Any,
        round_number: int, **_kwargs: Any,
    ):
        return w.report, None

    monkeypatch.setattr(vl, "_dispatch_verification_round", _dispatch)
    monkeypatch.setattr(vl, "_get_pending_repair_tasks", lambda *_a, **_kw: [])
    monkeypatch.setattr(vl, "_reconcile_failure_history", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        vl, "_wait_for_open_verification_decisions", lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        vl, "_count_unfinished_execution_tasks", lambda *_a, **_kw: 0,
    )

    import verification_cancel as _vc_mod

    monkeypatch.setattr(_vc_mod, "is_cancelled", lambda _pid: False)

    yield w

    round_process_ledger.reset_for_tests()


def _run(world: _World) -> None:
    """Run one auto-verification round over the wired world."""
    vl._run_auto_verification_loop_inner(
        world.plan_id, world.plan_dir, world.project_dir,
        max_rounds=1, tool=None, start_round=1, round_token="rt-1",
    )


def _failed_report(vp_id: str = "VP-020") -> Dict[str, Any]:
    return {
        "verification_results": [
            {"id": vp_id, "status": "FAILED", "summary": "boom"},
        ],
    }


# ---------------------------------------------------------------------------
# The two readings of an empty repair queue
# ---------------------------------------------------------------------------


def test_failures_without_repair_tasks_go_to_an_operator(world: _World) -> None:
    """有失败 + 0 条修复任务 → 终态判决，等人来裁。

    计划不能再回到 ``executing``：那里没有活可干，执行器退出来，链子
    又回到验证 —— 一个静默的空转闭环。
    """
    world.report = _failed_report()
    world.result = {
        "status": "verification_failed",
        "repair_tasks": [],
        "stop_reason": None,
    }

    _run(world)

    assert not world.confirmed, (
        "the plan was routed to executing although the round had "
        "failures and no repair task — that is the silent spin"
    )
    assert world.dead_ends, "no dead end was recorded at all"
    dead_end = world.dead_ends[-1]
    assert dead_end["reason"] == "failures_without_repair_tasks"
    assert dead_end["chain_ending"] is True
    assert dead_end["rollback_to_ready"] is False


def test_no_failures_still_routes_to_executing(world: _World) -> None:
    """没有失败项 + 0 条修复任务 → 老路：回到 executing 排空剩余工作。

    这是零回归的那一边：空修复队列在**没有**失败项的时候，说的确实
    是「没什么要修的」。
    """
    world.report = {
        "verification_results": [
            {"id": "VP-001", "status": "PASSED", "summary": "ok"},
        ],
    }
    world.result = {
        "status": "verification_failed",
        "repair_tasks": [],
        "stop_reason": None,
    }

    _run(world)

    assert world.confirmed, (
        "a round without failures must still route to executing — the "
        "empty repair queue meant 'nothing to repair' there"
    )
    assert not [
        d for d in world.dead_ends
        if d["reason"] == "failures_without_repair_tasks"
    ], "a round without failures must not be terminalised"


def test_the_verdict_names_the_stop_reason(world: _World) -> None:
    """判决的理由是一个**登记在案的** stop_reason。

    操作员看到的是「为什么停」，不是一个随手写出来的字符串。
    """
    world.report = _failed_report()
    world.result = {
        "status": "verification_failed",
        "repair_tasks": [],
        "stop_reason": None,
    }

    _run(world)

    assert vl.FAILURES_WITHOUT_REPAIR_TASKS_STOP_REASON == (
        "failures_without_repair_tasks"
    )
    assert world.dead_ends[-1]["reason"] == (
        vl.FAILURES_WITHOUT_REPAIR_TASKS_STOP_REASON
    )


def test_a_generation_failure_is_not_this_verdict(world: _World) -> None:
    """生成失败是另一条路：它要重试，不是终态。

    ``repair_generation_error`` 的分支在前面，它自己没有走到这里 ——
    本测试钉住的是「生成失败不会被误判成『有失败但没任务』」。
    """
    world.report = _failed_report()
    world.result = {
        "status": "verification_failed",
        "repair_tasks": [],
        "stop_reason": None,
        "repair_generation_error": "the LLM call died",
    }

    _run(world)

    assert not [
        d for d in world.dead_ends
        if d["reason"] == "failures_without_repair_tasks"
    ], "a generation failure must not be terminalised as 'failures without tasks'"
