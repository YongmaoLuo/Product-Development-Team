"""TDD spec —— 预检结果驱动 repair 执行（任务 11）。

背景
----
任务 10 让 ``start_verification_cycle`` 在跑验证轮**之前**先问一次根因链：
链欠着活就把活派出去，链在人手里就不跑这一轮。产出的轮报告里带着
``root_cause_preflight`` 这个键 —— 它说「这一轮被链接手了」。

但**接手**本身没人接：

* 队列里写着任务，却没有人去跑 —— 预检把任务写进 ``state.db`` 就收工了；
* 自动循环与手动 ``/start`` 还会把这份报告当成一轮失败的验证去 judge ——
  报告里没有验证点结果，judge 读到的是「什么都没验」，于是把修复链派给一个
  没人验过的东西。

本任务交付的是**接手之后的那一步**：

* 链欠着活 → 拉起 repair 执行（复用 base-drift 分支现成的入口：
  ``_make_repair_on_complete`` + ``_server._run_repair_execution_async``）；
* 链在人手里 → 计划停在 ``verification_loop_stopped``，不派活、不收尾、
  不推进轮次；
* 预检没接手（``handled=False``）→ 原行为逐字节不变。

边界条件
--------
* ``blocked=True`` → 置 ``verification_loop_stopped``；不启 repair、
  不 ``complete_round``。
* ``handled=False`` → 调用方走的还是原来那条路，一行都不变。
* repair 启动抛错 → 记录日志，计划**保持** ``verification_repairing``
  （任务还在队列里，操作员下一次 ``/start`` 能续跑）。
* 手动路径 → 不调用 ``verification.complete_round``、不写终态。

TDD contract —— 3 cases
-----------------------
1. ``test_auto_loop_starts_preflight_repair`` —— 自动循环拉起 repair 执行，
   并且**不** judge 这一轮。
2. ``test_manual_start_returns_early`` —— 手动 ``/start`` 提前返回：不收尾、
   不写终态。
3. ``test_blocked_chain_does_not_advance_round`` —— 链在人手里：不派活、
   不推进轮次、计划停在 ``verification_loop_stopped``。

测试命令::

    pytest backend/tests/unit/verification/test_chain_preflight_callers.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import pytest  # noqa: E402


#: The provider-routing scene the preflight's repair runs under.
PREFLIGHT_SCENE = "root_cause_preflight"

#: The phase a plan whose chain was continued by the preflight belongs in.
REPAIRING_PHASE = "verification_repairing"

#: The phase a plan whose chain is with a human is parked in.
STOPPED_PHASE = "verification_loop_stopped"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fresh_state_db(tmp_path: Path, monkeypatch) -> Path:
    """A hermetic ``state.db`` every reader agrees on.

    ``PDT_STATE_DB_PATH`` is the seam: ``config_paths.resolve_state_db_path``
    reads it on every call, so ``plan_state`` and ``server`` land on the
    same file without either of them being patched.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    import server

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    return db


def _seed_routing(db: Path, plan_id: str, phase: str) -> None:
    """A ``plan_routing`` row for the plan, in ``phase``."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        repo.insert(plan_id, phase)
        repo.write_plan_state(
            plan_id,
            phase=phase,
            completed_phases=[],
            review_rounds={"prd": 0, "arch": 0, "test": 0},
            flags={},
            verification={
                "status": "pending", "round": 0, "max_rounds": 3,
                "stop_reason": None,
            },
            last_updated="2026-11-05T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()


def _preflight_report(*, handled: bool = True, blocked: bool = False) -> dict:
    """A round report that carries the preflight's decision.

    The shape is the one task 10 produces: ``start_verification_cycle``
    returns it under ``root_cause_preflight`` when the preflight took the
    round over.
    """
    return {
        "overall_status": "FAILED",
        "verification_results": [],
        "requirement_deviations": [],
        "summary": "the root-cause chain's preflight dispatched its tasks",
        "execution_profile": {"mode": "root_cause_preflight"},
        "root_cause_preflight": {
            "handled": handled,
            "blocked": blocked,
            "actions": [{"vp_id": "VP-3", "action": "implementation"}],
            "tasks": [{"id": "repair-1"}],
            "task_ids": ["repair-1"],
            "vp_ids": ["VP-3"],
            "phase": REPAIRING_PHASE,
            "reason": (
                "root_cause_blocked_on_user" if blocked
                else "root_cause_chain_continued"
            ),
        },
    }


def _stub_orchestrator(judged: list, report: dict):
    """An orchestrator whose round is replaced by the preflight.

    ``check_cycle_conditions`` records every call: a round the preflight
    took over must never be judged, so a non-empty list is exactly the
    defect this file is about.
    """

    class _Stub:
        def __init__(self, plan_dir, project_dir, coding_tool=None,
                     verif_repo=None):
            self.plan_dir = plan_dir
            self.project_dir = project_dir

        def start_verification_cycle(self, round_number=1, force=False,
                                     resume=False):
            return report

        def check_cycle_conditions(self, rep, round_number=1):
            judged.append(rep)
            return {"status": "failed", "repair_tasks": []}

    return _Stub


@pytest.fixture
def repair_dispatch_spy(monkeypatch):
    """Every repair dispatch, recorded — the executor never really runs."""
    import server

    calls: list = []

    def _fake_dispatch(plan_id, project_dir, **kwargs):
        calls.append({"plan_id": plan_id, "project_dir": project_dir,
                      **kwargs})
        return {"status": "started", "pid": 4242}

    monkeypatch.setattr(server, "_run_repair_execution_async", _fake_dispatch)
    return calls


@pytest.fixture
def complete_round_spy(monkeypatch):
    """Every ``VerificationRepository.complete_round`` call, recorded."""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    calls: list = []
    real = VerificationRepository.complete_round

    def _spy(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(VerificationRepository, "complete_round", _spy)
    return calls


@pytest.fixture(autouse=True)
def _clean_server_state():
    """Keep the process-global verification/execution state from leaking."""
    import server

    before_verification = dict(server._verification_state)
    before_execution = dict(server._execution_state)
    yield
    with server._verification_lock:
        server._verification_state.clear()
        server._verification_state.update(before_verification)
    with server._execution_state_lock:
        server._execution_state.clear()
        server._execution_state.update(before_execution)


def _plan_phase(plan_dir: Path) -> str:
    """The plan's own phase, read back from the state machine."""
    from plan_state import PlanState

    return PlanState(plan_dir).get_current_phase()


# ---------------------------------------------------------------------------
# 1. 链欠着活 → 自动循环拉起 repair 执行
# ---------------------------------------------------------------------------
def test_auto_loop_starts_preflight_repair(
    tmp_path, monkeypatch, repair_dispatch_spy,
):
    """预检接手时，自动循环把链欠的活派给执行器，并且不 judge 这一轮。"""
    import server
    import verification_cancel
    import verification_loop

    plan_id = "preflight-auto-loop"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "executing")
    monkeypatch.setattr(server, "_plan_dir", lambda pid: plan_dir)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: None)
    monkeypatch.setattr(
        verification_cancel, "is_cancelled", lambda pid: False,
    )

    judged: list = []
    monkeypatch.setattr(
        server, "VerificationOrchestrator",
        _stub_orchestrator(judged, _preflight_report()),
    )

    verification_loop._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, max_rounds=1,
    )

    assert judged == [], (
        "the round never ran — the preflight took it over, so there is no "
        "verdict to judge"
    )
    assert len(repair_dispatch_spy) == 1, (
        f"the chain's work must be dispatched, nobody else runs it: "
        f"{repair_dispatch_spy!r}"
    )
    dispatch = repair_dispatch_spy[0]
    assert dispatch["plan_id"] == plan_id
    assert dispatch["project_dir"] == project_dir
    assert dispatch.get("scene") == PREFLIGHT_SCENE, (
        f"the preflight's repair runs under its own scene: {dispatch!r}"
    )
    assert dispatch.get("on_complete") is not None, (
        "the dispatch must hand over a callback — without it the chain "
        "waits for a round that never comes"
    )
    assert dispatch.get("outcome") is not None, (
        "the dispatch must hand over the loop's outcome — without it the "
        "loop's return reads as a terminal verdict"
    )
    assert _plan_phase(plan_dir) == REPAIRING_PHASE, (
        f"the plan is in the repair half of the loop: "
        f"{_plan_phase(plan_dir)!r}"
    )


# ---------------------------------------------------------------------------
# 2. 手动 /start → 提前返回
# ---------------------------------------------------------------------------
def test_manual_start_returns_early(
    tmp_path, monkeypatch, repair_dispatch_spy, complete_round_spy,
):
    """手动 ``/start`` 在预检接手时提前返回：不收尾、不写终态。"""
    import server
    from routes import verification as routes_verification

    plan_id = "preflight-manual"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "verification")
    monkeypatch.setattr(server, "_plan_dir", lambda pid: plan_dir)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: None)

    judged: list = []
    monkeypatch.setattr(
        server, "VerificationOrchestrator",
        _stub_orchestrator(judged, _preflight_report()),
    )

    terminals: list = []
    monkeypatch.setattr(
        server, "_persist_verification_terminal",
        lambda *a, **k: terminals.append(a),
    )

    server._execution_state[plan_id] = {"project_dir": str(project_dir)}

    response = routes_verification.start_verification(
        plan_id, server.StartVerificationRequest(auto_fix=False),
    )
    assert isinstance(response, dict), (
        f"the start must be admitted, not refused: {response!r}"
    )
    thread = server._verification_state.get(plan_id, {}).get("thread")
    assert thread is not None, "the manual start must run its worker"
    thread.join(timeout=30)
    assert not thread.is_alive(), "the manual worker must have returned"

    assert judged == [], (
        "the round never ran — the preflight took it over, so there is no "
        "verdict to judge"
    )
    assert complete_round_spy == [], (
        "the preflight's round is not a round: closing it would write a "
        "verdict for a round that never ran"
    )
    assert terminals == [], (
        "the manual path must not write a terminal state — the chain is "
        "still the plan's business"
    )
    assert len(repair_dispatch_spy) == 1, (
        f"the chain's work must be dispatched: {repair_dispatch_spy!r}"
    )
    assert repair_dispatch_spy[0].get("scene") == PREFLIGHT_SCENE


# ---------------------------------------------------------------------------
# 3. 链在人手里 → 不派活、不推进轮次
# ---------------------------------------------------------------------------
def test_blocked_chain_does_not_advance_round(
    tmp_path, monkeypatch, repair_dispatch_spy, complete_round_spy,
):
    """链在人手里：不派活、不收尾，计划停在 ``verification_loop_stopped``。"""
    import server
    import verification_cancel
    import verification_loop

    plan_id = "preflight-blocked"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "executing")
    monkeypatch.setattr(server, "_plan_dir", lambda pid: plan_dir)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: None)
    monkeypatch.setattr(
        verification_cancel, "is_cancelled", lambda pid: False,
    )

    judged: list = []
    monkeypatch.setattr(
        server, "VerificationOrchestrator",
        _stub_orchestrator(judged, _preflight_report(blocked=True)),
    )

    verification_loop._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, max_rounds=1,
    )

    assert repair_dispatch_spy == [], (
        "a chain that is with a human has nothing the machine can run"
    )
    assert judged == [], (
        "the round never ran — the preflight took it over, so there is no "
        "verdict to judge"
    )
    assert complete_round_spy == [], (
        "the round must not be closed: nothing was verified, so there is no "
        "verdict to record and no round to advance"
    )
    assert _plan_phase(plan_dir) == STOPPED_PHASE, (
        f"the plan is parked where a human can pick it up: "
        f"{_plan_phase(plan_dir)!r}"
    )


# ---------------------------------------------------------------------------
# 4. repair 启动抛错 → 计划留在 verification_repairing
# ---------------------------------------------------------------------------
def test_repair_start_failure_keeps_the_plan_in_repairing(
    tmp_path, monkeypatch, complete_round_spy,
):
    """repair 拉不起来时，计划停在 ``verification_repairing``。

    链欠的活还在队列里。计划停在修复相位，操作员下一次 ``/start``
    还能接着跑 —— 停在别处就等于把这份活丢了。
    """
    import server
    import verification_cancel
    import verification_loop

    plan_id = "preflight-repair-raises"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "executing")
    monkeypatch.setattr(server, "_plan_dir", lambda pid: plan_dir)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: None)
    monkeypatch.setattr(
        verification_cancel, "is_cancelled", lambda pid: False,
    )

    judged: list = []
    monkeypatch.setattr(
        server, "VerificationOrchestrator",
        _stub_orchestrator(judged, _preflight_report()),
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("the executor refused to start")

    monkeypatch.setattr(server, "_run_repair_execution_async", _explode)

    verification_loop._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, max_rounds=1,
    )

    assert judged == [], (
        "the round never ran — the preflight took it over, so there is no "
        "verdict to judge"
    )
    assert complete_round_spy == [], (
        "the round must not be closed: nothing was verified, so there is no "
        "verdict to record"
    )
    assert _plan_phase(plan_dir) == REPAIRING_PHASE, (
        f"the chain's work is still owed — the plan must stay where the "
        f"work can be picked up: {_plan_phase(plan_dir)!r}"
    )
