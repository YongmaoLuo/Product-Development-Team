"""A round that could not be judged must ask a human — not just stop.

Background
----------
``check_cycle_conditions`` has an exit for a round that judged nothing:
``_round_judged_nothing`` → ``_judged_nothing_cycle_result``.  The round
stops the chain, writes no repair task and — until 2026-11-27 — asked
**nobody**.  The plan sat there with a verdict nobody could produce and
no question in front of anybody.

Two of the ways a round judges nothing are the ones only a person can
answer:

* a point's outcome could not be read at all (``UNKNOWN``);
* a gate ran out of its budget (``INCONCLUSIVE``).

``_round_needs_operator_decision`` already knew about the second one and
returned a bool — but nothing in the repository ever used that bool to
``create_decision``.  The flag was read into the card and the result, and
that was all: the round stopped in silence.

What these tests pin
--------------------
 1. an ``UNKNOWN`` round asks the operator, with the two terminal
    options and the ordinary default answer (``auto``);
 2. an ``INCONCLUSIVE`` round asks the operator, and the question names
    the phase whose budget ran out;
 3. the question reaches the store (``hitl.service.list_decisions``) and
    the terminal decision's sidecar, and the round's exit says somebody
    is waiting;
 4. the third unjudged round in a row asks **strictly** — no default
    answer, no deadline: only a person can settle it;
 5. a round that judged something resets the streak, so the next unjudged
    round asks the ordinary way again;
 6. the rounds that judge nothing for the other reasons — a pure
    ``DEFERRED`` round, an environment-blocked round — keep asking
    nobody.

The interface contract
-----------------------
``VerificationOrchestrator._ask_a_human_about`` gains ``require_answer``;
the orchestrator counts the unjudged rounds in the loop-tracking file
(``unjudged_streak``); and the unjudged exit asks the operator through
the one writer of decisions (``hitl.service.create_decision``).

Pure rule tests: no model, no subprocess — the only side effects are
pytest's own ``tmp_path`` and the hermetic per-test SQLite file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


#: The answers the question offers — the terminal branch's two.
EXPECTED_OPTIONS = ["rerun_this_round", "revise_criterion"]

#: The answer the question falls back to when nobody replies.
EXPECTED_DEFAULT = "rerun_this_round"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state **and** HITL persistence at a hermetic SQLite file.

    Both live in the same ``state.db``: ``PlanState`` keeps the plan's
    routing row there, and ``hitl.service`` keeps ``plan_decisions``
    there.  Without the override the orchestrator would write its rows
    into the repo's real ``state.db`` and read back whatever an earlier
    test left behind.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3, stop_reason=None, status="pending"):
    """Bootstrap ``plan_state.json`` for a test."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase not in ("interview",) else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


def _unknown_report():
    """A round whose point's outcome could not be read."""
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "verification_results": [
            {"id": "VP-1", "status": "UNKNOWN", "evidence": {}},
        ],
    }


def _inconclusive_report(phase="unit"):
    """A round whose gate ran out of budget."""
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "verification_results": [
            {
                "id": "VP-1",
                "status": "INCONCLUSIVE",
                "evidence": {"budget_exhausted_in": phase},
            },
        ],
    }


def _deferred_report():
    """A round that deferred its point — the environment took it out."""
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "verification_results": [
            {"id": "VP-1", "status": "DEFERRED"},
        ],
    }


def _environment_blocked_report():
    """A round the environment took a point out of."""
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "environment_blocked": True,
        "verification_results": [
            {
                "id": "VP-1",
                "status": "BLOCKED",
                "evidence": {"reason_code": "service_data_plane_mismatch"},
            },
        ],
    }


def _passed_report():
    """A round that judged the deliverable and found it good."""
    return {
        "overall_status": "PASSED",
        "failed_count": 0,
        "verification_results": [
            {"id": "VP-1", "status": "PASSED"},
        ],
    }


def _orchestrator(plan_dir, project_dir, coding_tool):
    """An orchestrator over a hermetic plan."""
    orch = VerificationOrchestrator(plan_dir, project_dir, coding_tool)
    orch.start_verification_cycle(round_number=1)
    return orch


def _run_round(orch, report, round_number=1):
    """One round through ``check_cycle_conditions``."""
    return orch.check_cycle_conditions(report, round_number=round_number)


def _decisions(plan_id):
    from hitl.service import list_decisions

    return list_decisions(plan_id)


def _sidecar(plan_dir):
    from verification import terminal_decision_session

    return terminal_decision_session.load(plan_dir)


def _tracking(plan_dir):
    """The loop-tracking payload the orchestrator persisted."""
    path = plan_dir / "verification_loop_tracking.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_tracking(plan_dir, unjudged_streak):
    """Pre-set the tracking file, as a previous round would have."""
    path = plan_dir / "verification_loop_tracking.json"
    path.write_text(
        json.dumps({
            "version": 2,
            "previous_failed_keys": [],
            "same_failure_counts": {},
            "last_round": 0,
            "unjudged_streak": unjudged_streak,
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# A. The UNKNOWN round asks the operator
# ---------------------------------------------------------------------------


def test_an_unknown_round_asks_the_operator(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A round whose point's outcome could not be read asks a human.

    Before this, the round stopped the chain in silence: the exit said
    ``environment_blocked`` and nobody was asked, so the plan sat there
    with a verdict nobody could produce.
    """
    _write_plan_state(temp_plan_dir, phase="executing")

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        result = _run_round(orch, _unknown_report())

    decisions = _decisions(temp_plan_dir.name)
    assert len(decisions) == 1, (
        f"没判出的轮次必须问一个人，实际有 {len(decisions)} 个问题：{decisions!r}"
    )
    decision = decisions[0]
    assert decision.options == EXPECTED_OPTIONS, decision.options
    assert decision.default_option == EXPECTED_DEFAULT, decision.default_option
    assert decision.mode == "auto", decision.mode
    assert "UNKNOWN" in decision.question, decision.question
    assert "could not be judged" in decision.question, decision.question

    record = _sidecar(temp_plan_dir)
    assert record is not None, "决策的 sidecar 必须写下来"
    assert record["decision_id"] == decision.decision_id, record
    # The points that produced no verdict are the ones the *revise the
    # criterion* answer obsoletes, so they travel with the record.
    assert [p["id"] for p in record["failed_points"]] == ["VP-1"], record

    assert result["waiting_for_user"] is True, result
    assert result["needs_operator_decision"] is True, result
    assert result["should_stop"] is True, result


# ---------------------------------------------------------------------------
# B. The INCONCLUSIVE round asks the operator, naming the phase
# ---------------------------------------------------------------------------


def test_an_inconclusive_round_asks_the_operator_and_names_the_phase(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A gate that ran out of budget asks a human — and says which gate."""
    _write_plan_state(temp_plan_dir, phase="executing")

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        result = _run_round(orch, _inconclusive_report(phase="unit"))

    decisions = _decisions(temp_plan_dir.name)
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision.options == EXPECTED_OPTIONS, decision.options
    assert decision.default_option == EXPECTED_DEFAULT, decision.default_option
    assert decision.mode == "auto", decision.mode
    assert "unit" in decision.question, decision.question

    record = _sidecar(temp_plan_dir)
    assert record is not None, "决策的 sidecar 必须写下来"
    assert record["decision_id"] == decision.decision_id, record

    assert result["waiting_for_user"] is True, result
    assert result["needs_operator_decision"] is True, result


# ---------------------------------------------------------------------------
# C. The third unjudged round in a row waits for a person
# ---------------------------------------------------------------------------


def test_the_third_unjudged_round_asks_strictly(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """Three unjudged rounds in a row: the question waits for a person.

    The first two rounds carry a default answer, so a plan nobody
    answers falls back to *rerun this round* and carries on by itself.
    The third round does not: the chain has been stuck for three rounds,
    and letting the clock answer for the fourth would be the system
    answering on the operator's behalf.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    _write_tracking(temp_plan_dir, unjudged_streak=2)

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        result = _run_round(orch, _unknown_report())

    decisions = _decisions(temp_plan_dir.name)
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision.mode == "strict", decision.mode
    assert decision.deadline_at is None, decision.deadline_at
    assert decision.default_option is None, decision.default_option
    assert decision.options == EXPECTED_OPTIONS, decision.options

    assert _tracking(temp_plan_dir)["unjudged_streak"] == 3, _tracking(
        temp_plan_dir
    )
    assert result["waiting_for_user"] is True, result


# ---------------------------------------------------------------------------
# D. A round that judged something resets the streak
# ---------------------------------------------------------------------------


def test_a_judged_round_resets_the_unjudged_streak(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A round that judged the deliverable ends the streak.

    The chain is back on its feet, so the next unjudged round is the
    **first** one again — and the first one asks the ordinary way, with
    the default answer.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    _write_tracking(temp_plan_dir, unjudged_streak=2)

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        _run_round(orch, _passed_report())

    assert _tracking(temp_plan_dir)["unjudged_streak"] == 0, _tracking(
        temp_plan_dir
    )

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        result = _run_round(orch, _unknown_report(), round_number=2)

    decisions = _decisions(temp_plan_dir.name)
    assert len(decisions) == 1, decisions
    assert decisions[0].mode == "auto", decisions[0].mode
    assert decisions[0].default_option == EXPECTED_DEFAULT, decisions[0]
    assert _tracking(temp_plan_dir)["unjudged_streak"] == 1, _tracking(
        temp_plan_dir
    )
    assert result["waiting_for_user"] is True, result


# ---------------------------------------------------------------------------
# E. The other unjudged rounds keep asking nobody
# ---------------------------------------------------------------------------


def test_a_deferred_round_asks_nobody(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A pure ``DEFERRED`` round keeps its behaviour: nobody is asked.

    Deferring is the plan's own business — the point waits for a later
    round — so it is not the operator's turn.
    """
    _write_plan_state(temp_plan_dir, phase="executing")

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        result = _run_round(orch, _deferred_report())

    assert _decisions(temp_plan_dir.name) == [], _decisions(temp_plan_dir.name)
    assert result["waiting_for_user"] is False, result
    assert result["status"] == "loop_stopped", result


def test_an_environment_blocked_round_asks_nobody(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A round the environment took out of keeps asking nobody."""
    _write_plan_state(temp_plan_dir, phase="executing")

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = _orchestrator(temp_plan_dir, temp_project_dir, mock_coding_tool)
        result = _run_round(orch, _environment_blocked_report())

    assert _decisions(temp_plan_dir.name) == [], _decisions(temp_plan_dir.name)
    assert result["waiting_for_user"] is False, result
    assert result["status"] == "loop_stopped", result
