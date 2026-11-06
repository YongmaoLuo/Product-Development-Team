"""TDD spec —— 「机器无路可走」与「在等人回答」是两个名字（2026-11-06）。

背景
----
``BLOCKED_ON_USER`` 一个名字被用在了**两个**概念上：

1. **根因链的阶段**（``verification_root_cause_chain.json`` 的 ``stage``）——
   「诊断做了、实现也做了，仍然失败，机器没有下一步」；
2. **计划层的 payload**（``blocked_on_user``）—— 「有一个问题在等人回答」。

两者不是一回事：链标成 exhausted 的时候，往往**一个问题都还没送出去**
（决策可能 ``unroutable``）。读日志的人把前者当成后者，就读错了。

本文件钉的契约
--------------
* 链的阶段改名成 ``MACHINE_EXHAUSTED``，新写的记录只写新名字；
* **旧记录照样读**：``BLOCKED_ON_USER`` 的记录读进内存时归一化成新名字，
  那个点**仍然算 terminal**（不能被读成「不在链上」或「阶段未知」）；
* 旧记录被读过一次再写回 → 磁盘上是新名字；
* payload 拆成两个字段：``machine_exhausted``（机器没路了）与
  ``blocked_on_user``（**真的**有一个问题在等人回答）。

测试命令::

    pytest backend/tests/unit/test_root_cause_chain_stage_naming.py -v
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import (  # noqa: E402
    CHAIN_STAGE_MACHINE_EXHAUSTED,
    VerificationOrchestrator,
)


#: The point the rounds fail on, and the point the chain owns.
VP_ID = "VP-3"

#: The chain's record — the file that says where the escalation stands.
CHAIN_FILENAME = "verification_root_cause_chain.json"

#: The stage name the chain's record carried before 2026-11-06.
LEGACY_STAGE = "BLOCKED_ON_USER"

#: The failure class the rounds carry (it counts, so the old key moves).
FAILURE_CLASS = "deliverable_defect"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """Keep the state machine and the plan artifacts out of the real tree.

    ``PDT_STATE_DB_PATH`` is where the plan's rows live (and where the
    HITL store lives), ``PDT_PLANS_DIR`` is where the audit mirror is
    written.  Both are redirected into ``tmp_path`` so a test cannot
    pollute the operator's tree.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    return tmp_path


@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "chain-stage-naming"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "chain-stage-naming"
    project.mkdir(parents=True, exist_ok=True)
    return project


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the orchestrator reads on construction."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8",
    )


def _write_chain(plan_dir, stage, vp_stage=None):
    """The chain's record on disk, with the given stage."""
    payload = {
        "version": 1,
        "stage": stage,
        "vps": {
            VP_ID: {
                "stage": vp_stage if vp_stage is not None else stage,
                "round": 1,
                "report_path": (
                    f"plans/{plan_dir.name}/reports/root-cause-{VP_ID}.md"
                ),
            },
        },
        "head_sha": None,
        "updated_round": 1,
    }
    (plan_dir / CHAIN_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _read_chain(plan_dir):
    """The chain's record on disk, parsed."""
    path = plan_dir / CHAIN_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _fail_report():
    """A round report: the point failed."""
    error = "AssertionError: the endpoint returns 401"
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "failure_class": FAILURE_CLASS,
                "actual_result": error,
                "evidence": error,
            },
        ],
        "requirement_deviations": [],
    }


def _orchestrator(plan_dir, project_dir):
    """An orchestrator with the repair path mocked out."""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
        orch.start_verification_cycle(round_number=1)
    return orch


def _run_round(plan_dir, project_dir, *, ask_human=""):
    """Run one round; return ``(orchestrator, verdict)``.

    ``ask_human`` is what the round's question to the human returns: the
    default (``""``) means **no question could be asked**, which is a
    production shape (the store may refuse the question) and the one
    that makes "nobody is waiting" observable.
    """
    orch = _orchestrator(plan_dir, project_dir)
    with patch.object(
        VerificationOrchestrator, "_ask_a_human_about",
        return_value=ask_human,
    ):
        verdict = orch.check_cycle_conditions(_fail_report(), round_number=2)
    return orch, verdict


def _seed_pending_decision(plan_id, db_path):
    """Put one **pending** decision for the plan into the HITL store."""
    from datetime import datetime, timezone

    from hitl.models import Decision, DecisionStatus
    from hitl.store import HitlStore

    now = datetime.now(timezone.utc).isoformat()
    decision = Decision(
        decision_id="d-pending-1",
        plan_id=plan_id,
        source="verification",
        question="what happens to the plan?",
        options=["stop", "continue"],
        default_option="stop",
        status=DecisionStatus.PENDING.value,
        created_at=now,
        updated_at=now,
    )
    store = HitlStore(db_path)
    try:
        store.insert(decision)
    finally:
        store.close()
    return decision.decision_id


def _pending_decisions(plan_id, db_path):
    """The plan's pending/paused decisions, as the store holds them."""
    from hitl.service import list_decisions
    from hitl.store import HitlStore

    store = HitlStore(db_path)
    try:
        decisions = list_decisions(plan_id, store=store)
    finally:
        store.close()
    return [
        decision for decision in decisions
        if str(decision.status) in ("pending", "paused")
    ]


# ---------------------------------------------------------------------------
# 1. 新写的链记录只写新名字
# ---------------------------------------------------------------------------


def test_new_chain_record_carries_the_new_stage_name(plan_dir, project_dir):
    """A point whose implementation ran and still fails lands on the new name.

    The record on disk must say ``MACHINE_EXHAUSTED`` — both the
    chain's own stage and the point's stage inside it.  Nothing may say
    ``BLOCKED_ON_USER`` any more: that name is what made the stage and
    the "a human is waiting" flag look like the same fact.
    """
    _write_plan_state(plan_dir)
    # The point is at ``implementation``: the implementation task ran and
    # the point still fails, so this round demotes it.
    _write_chain(plan_dir, "implementation")

    _run_round(plan_dir, project_dir)

    chain = _read_chain(plan_dir)
    assert chain is not None, "the chain's record must be written"
    assert chain["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the chain's stage must be the new name: {chain!r}"
    )
    assert chain["stage"] == "MACHINE_EXHAUSTED", (
        f"the stage name is spelled out on purpose: {chain!r}"
    )
    assert chain["vps"][VP_ID]["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the point's stage must be the new name too: {chain!r}"
    )
    assert LEGACY_STAGE not in json.dumps(chain), (
        f"the old name must not survive into a new record: {chain!r}"
    )


# ---------------------------------------------------------------------------
# 2. 旧记录读进来 → 归一化，且那个点仍然是 terminal
# ---------------------------------------------------------------------------


def test_legacy_record_is_normalised_in_memory(plan_dir, project_dir):
    """A record written before the rename still says what it said.

    The stage comes back as the current name — and, more importantly,
    the point is still **on** the chain and still terminal.  A legacy
    record must never read as "not on the chain" or "stage unknown".
    """
    _write_plan_state(plan_dir)
    _write_chain(plan_dir, LEGACY_STAGE)

    orch = _orchestrator(plan_dir, project_dir)
    chain = orch._load_root_cause_chain()

    assert chain["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the legacy stage must be normalised in memory: {chain!r}"
    )
    assert chain["vps"][VP_ID]["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the point's legacy stage must be normalised too: {chain!r}"
    )

    decision = orch._root_cause_chain_decision([], 2)
    assert decision["terminal"] is True, (
        f"a legacy exhausted point is still terminal: {decision!r}"
    )
    assert decision["blocked"] == [VP_ID], (
        f"the legacy point is still blocked: {decision!r}"
    )


# ---------------------------------------------------------------------------
# 3. 旧记录读一次再写回 → 磁盘上是新名字
# ---------------------------------------------------------------------------


def test_legacy_record_is_rewritten_with_the_new_name(plan_dir, project_dir):
    """The next write of a legacy record lands the current name.

    The legacy name is a read-side fact only: once the record has been
    read, the next write says ``MACHINE_EXHAUSTED``, so the old name
    disappears from the disk on its own.
    """
    _write_plan_state(plan_dir)
    _write_chain(plan_dir, LEGACY_STAGE)

    _run_round(plan_dir, project_dir)

    chain = _read_chain(plan_dir)
    assert chain is not None, "the chain's record must be written"
    assert chain["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the rewritten record must carry the new name: {chain!r}"
    )
    assert chain["vps"][VP_ID]["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the rewritten point must carry the new name: {chain!r}"
    )
    assert LEGACY_STAGE not in json.dumps(chain), (
        f"the old name must not survive the rewrite: {chain!r}"
    )


# ---------------------------------------------------------------------------
# 4. payload：机器没路了，但没有人被问 → blocked_on_user 是 False
# ---------------------------------------------------------------------------


def test_machine_exhausted_without_a_pending_decision(plan_dir, project_dir):
    """The chain is out of road and nobody is waiting: two different facts.

    ``machine_exhausted`` says the machine has no road left on the
    chain's points.  ``blocked_on_user`` says a question is waiting for
    an answer.  With no question asked, the second one is ``False`` —
    reading it as ``True`` is exactly the misreading this rename exists
    to kill.
    """
    _write_plan_state(plan_dir)
    _write_chain(plan_dir, CHAIN_STAGE_MACHINE_EXHAUSTED)

    _orch, verdict = _run_round(plan_dir, project_dir)

    assert verdict["machine_exhausted"] is True, (
        f"the machine is out of road on the chain: {verdict!r}"
    )
    assert verdict["blocked_on_user"] is False, (
        f"nobody is waiting for an answer: {verdict!r}"
    )
    # The old flags keep their meaning: the round still ends with a
    # person in the loop.
    assert verdict["waiting_for_user"] is True, (
        f"the round still waits for a human: {verdict!r}"
    )
    assert verdict["needs_operator_decision"] is True, (
        f"the round still needs an operator: {verdict!r}"
    )


# ---------------------------------------------------------------------------
# 5. payload：真的有未决决策 → blocked_on_user 是 True
# ---------------------------------------------------------------------------


def test_blocked_on_user_is_true_when_a_decision_is_pending(
    plan_dir, project_dir, hermetic,
):
    """A question that is waiting for an answer says so.

    The flag is read from the store, so a decision that is really
    ``pending`` puts the plan's payload in the state a reader expects:
    somebody has a question in front of them.
    """
    _write_plan_state(plan_dir)
    _write_chain(plan_dir, CHAIN_STAGE_MACHINE_EXHAUSTED)
    db_path = hermetic / "state.db"
    _seed_pending_decision(plan_dir.name, db_path)
    assert len(_pending_decisions(plan_dir.name, db_path)) == 1, (
        "the seeded decision must be pending"
    )

    _orch, verdict = _run_round(plan_dir, project_dir)

    assert verdict["blocked_on_user"] is True, (
        f"a pending question means somebody is waiting: {verdict!r}"
    )
    assert verdict["machine_exhausted"] is True, (
        f"the machine is out of road on the chain: {verdict!r}"
    )


# ---------------------------------------------------------------------------
# 6. payload：存储读不了 → False + 一条 WARNING，且不抛
# ---------------------------------------------------------------------------


def test_unreadable_store_falls_back_to_false_with_a_warning(
    plan_dir, project_dir, caplog,
):
    """A store that cannot be read must not be reported as "waiting".

    The round must not claim that a human is waiting for an answer it
    cannot see: the flag falls back to ``False``, the failure is said
    out loud (a WARNING), and the round itself does not die.
    """
    _write_plan_state(plan_dir)
    _write_chain(plan_dir, CHAIN_STAGE_MACHINE_EXHAUSTED)

    with caplog.at_level(logging.WARNING, logger="verification.orchestrator"):
        with patch(
            "hitl.service.list_decisions",
            side_effect=RuntimeError("the store is gone"),
        ):
            _orch, verdict = _run_round(plan_dir, project_dir)

    assert verdict["blocked_on_user"] is False, (
        f"an unreadable store must not claim a human is waiting: {verdict!r}"
    )
    assert verdict["machine_exhausted"] is True, (
        f"the machine is out of road on the chain: {verdict!r}"
    )
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING
        and "could not be read" in record.getMessage()
    ]
    assert warnings, (
        f"the unreadable store must be said out loud: {caplog.records!r}"
    )
