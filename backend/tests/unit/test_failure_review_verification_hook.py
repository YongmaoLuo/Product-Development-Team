"""验证终态失败 → 旁路宣告一次失败复盘。

What this pins
--------------
1. **终态失败要宣告。** 计划真的走到终态失败（``status='failed'`` 且
   ``chain_ending=True``）时，:mod:`failure_review` 的
   :func:`~failure_review.announce_failure` 要被调用一次，``plan_id``
   与 ``stop_reason`` 都要对 —— 宣告的人得说清楚是哪个计划、因为什么
   停的。
2. **两种情况不宣告（负控）。** ``status='passed'``（验证通过）与
   ``chain_ending=False``（中间轮失败，执行子进程还在飞）都不能宣告：
   失败复盘只对**终态**失败开口，中间轮失败不是终态。
3. **宣告要发生在终态写入之后。** 宣告的那一刻，相位必须**已经是**
   终态 —— 共享判据 ``failure_review.is_terminal_failure`` 读的就是
   ``plan_routing.current_phase``；宣告跑在终态写入之前，读到的还是
   ``verification_running``，于是每一次真实验证链的终态失败都被否决：
   不落 ``failure_review.json``、不建决策，计划悄悄死掉。
4. **收尾阶段的失败不是终态失败。** ``postprocess_failed`` 不在
   ``TERMINAL_PHASES`` 里（还要等人决定是否续跑），所以收尾失败
   **不**宣告 —— 宣告它等于把「还在等人」说成「已经结束」。
5. **冻结哈希要跟着改。** ``backend/verification_loop.py`` 被
   ``FROZEN_R10_SURFACE_SHA256`` 逐字节钉住，改了实现就必须同时刷新
   那张表里的哈希值，否则门禁红。

Test command::

    pytest backend/tests/unit/test_failure_review_verification_hook.py -v
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import failure_review
import server as server_mod

#: The plan the failure is announced for.
PLAN_ID = "p1"

#: Why the plan stopped, as the caller knows it.
STOP_REASON = "boom"

#: ``backend/tests/unit/`` → the backend package root is two levels up.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The gate module that owns the frozen-hash table.
_SCOPE_GATE = (
    Path(__file__).resolve().parents[1]
    / "static_gates"
    / "test_verification_mode_scope_gates.py"
)

#: The file the frozen-hash table pins.
_VERIFICATION_LOOP = _BACKEND_DIR / "verification_loop.py"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def announced(monkeypatch) -> List[Dict[str, Any]]:
    """Record every ``announce_failure`` call instead of performing one.

    The production call site imports the function *inside* the function
    body, so the attribute is looked up on the module at call time —
    patching the module attribute is enough to intercept it.
    """
    calls: List[Dict[str, Any]] = []

    def _record(plan_id: str, **kwargs: Any) -> str:
        calls.append({"plan_id": plan_id, **kwargs})
        return "decision-under-test"

    monkeypatch.setattr(failure_review, "announce_failure", _record)
    return calls


# ---------------------------------------------------------------------------
# 1. The chain-ending failure announces
# ---------------------------------------------------------------------------


def test_chain_ending_failure_announces(announced: List[Dict[str, Any]]) -> None:
    """终态失败 → 宣告一次。

    ``chain_ending=True`` 是「这条终态写入真的结束了整条链」的显式
    说法；配上 ``status='failed'``，这就是一次真正的终态失败。
    """
    server_mod._persist_verification_terminal(
        PLAN_ID, "failed", STOP_REASON, chain_ending=True,
    )

    assert len(announced) == 1, f"expected one announcement, got {announced!r}"
    assert announced[0]["plan_id"] == PLAN_ID
    assert announced[0]["stop_reason"] == STOP_REASON


# ---------------------------------------------------------------------------
# 2. The negative controls
# ---------------------------------------------------------------------------


def test_passed_terminal_does_not_announce(announced: List[Dict[str, Any]]) -> None:
    """验证通过 → 不宣告。

    成功不是失败，没有失败可复盘。
    """
    server_mod._persist_verification_terminal(
        PLAN_ID, "passed", STOP_REASON, chain_ending=True,
    )

    assert announced == []


def test_non_chain_ending_failure_does_not_announce(
    announced: List[Dict[str, Any]],
) -> None:
    """中间轮失败 → 不宣告。

    ``chain_ending=False`` 说明执行子进程还在飞，这条失败只是链中间
    的一轮，不是终态 —— 对中间失败宣告复盘是噪声。
    """
    server_mod._persist_verification_terminal(
        PLAN_ID, "failed", STOP_REASON, chain_ending=False,
    )

    assert announced == []


# ---------------------------------------------------------------------------
# 3. The announcement runs AFTER the terminal write
# ---------------------------------------------------------------------------
#
# 2026-11-20: the announcement used to run BEFORE the routing CAS, so
# the shared rule (``failure_review.is_terminal_failure``) read the
# plan's phase while it was still ``verification_running`` and vetoed
# the announcement.  The production chain therefore never produced a
# sidecar and never asked a human anything: a real verification failure
# died quietly.  These tests pin the ORDER, not just the presence of the
# call.


def _seed_routing_row(plan_id: str, phase: str) -> None:
    """Put the plan in ``phase``, through the production routing repository."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
    finally:
        conn.close()


def _routing_phase(plan_id: str) -> Optional[str]:
    """The plan's current phase, read from the one authority for it."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return None if row is None else row.get("current_phase")


def test_the_announcement_sees_the_terminal_phase(monkeypatch) -> None:
    """宣告的那一刻，相位已经是终态。

    The recorder reads the phase at call time — that is the fact the
    shared rule reads too, so this pins the ordering directly.
    """
    plan_id = "p-ordering"
    _seed_routing_row(plan_id, "verification_running")

    seen: List[Optional[str]] = []

    def _record(plan_id: str, **kwargs: Any) -> str:
        seen.append(_routing_phase(plan_id))
        return "decision-under-test"

    monkeypatch.setattr(failure_review, "announce_failure", _record)

    server_mod._persist_verification_terminal(
        plan_id, "failed", STOP_REASON, chain_ending=True,
    )

    assert seen == ["failed"], (
        f"the announcement ran while the plan was still at {seen!r} — it "
        "has to run after the terminal write, or the shared terminal-"
        "failure rule vetoes it"
    )


def test_a_terminal_failure_is_announced_for_real(isolated_plans_dir) -> None:
    """终态失败真的被宣告：落旁路记录、建一条待答决策。

    This is the production path, not a stub: the announcement goes
    through ``failure_review.announce_failure`` and therefore through
    the shared terminal-failure rule.
    """
    from config_paths import resolve_state_db_path
    from hitl.models import DecisionStatus
    from hitl.store import HitlStore

    plan_id = "p-real"
    _seed_routing_row(plan_id, "verification_running")

    server_mod._persist_verification_terminal(
        plan_id, "failed", STOP_REASON, chain_ending=True,
    )

    assert _routing_phase(plan_id) == "failed", (
        "the terminal write did not move the plan to the failed phase"
    )

    sidecar = Path(isolated_plans_dir) / plan_id / "failure_review.json"
    assert sidecar.exists(), (
        "the terminal failure was not announced: the announcement ran "
        "before the terminal phase was persisted, so the shared rule "
        "vetoed it and the plan died quietly"
    )

    with HitlStore(resolve_state_db_path()) as store:
        decisions = [
            decision
            for decision in store.list_by_plan(plan_id)
            if decision.source == "failure_review"
        ]
    assert len(decisions) == 1, decisions
    assert decisions[0].status == DecisionStatus.PENDING.value, decisions[0]


# ---------------------------------------------------------------------------
# 4. A post-processing failure is not a terminal failure
# ---------------------------------------------------------------------------


def test_a_postprocess_failure_is_not_announced(isolated_plans_dir) -> None:
    """收尾阶段的失败不宣告。

    ``postprocess_failed`` is not in ``TERMINAL_PHASES`` — the plan is
    still waiting for a person to say whether it runs again.  Announcing
    it would describe a state the plan has not reached.
    """
    plan_id = "p-postprocess"
    _seed_routing_row(plan_id, "post_processing")

    server_mod._persist_verification_terminal(
        plan_id, "failed", STOP_REASON, chain_ending=True,
        phase_target="postprocess_failed",
    )

    sidecar = Path(isolated_plans_dir) / plan_id / "failure_review.json"
    assert not sidecar.exists(), (
        "a post-processing failure was announced — postprocess_failed is "
        "not a terminal phase"
    )


# ---------------------------------------------------------------------------
# 5. The frozen hash pin
# ---------------------------------------------------------------------------


def test_verification_loop_pin_matches_disk() -> None:
    """冻结表里的哈希必须等于磁盘上的字节。

    ``backend/verification_loop.py`` 被 ``FROZEN_R10_SURFACE_SHA256``
    逐字节钉住；改了它就要同时刷新那张表。这里算的是文件本身的
    sha256，不是抄来的字面量。
    """
    actual = hashlib.sha256(_VERIFICATION_LOOP.read_bytes()).hexdigest()
    source = _SCOPE_GATE.read_text(encoding="utf-8")

    assert actual in source, (
        "the frozen-hash table does not carry the sha256 of "
        f"backend/verification_loop.py on disk ({actual}); refresh "
        "FROZEN_R10_SURFACE_SHA256 or the scope gate goes red"
    )
