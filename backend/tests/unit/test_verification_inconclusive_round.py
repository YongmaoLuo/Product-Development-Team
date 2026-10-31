"""An INCONCLUSIVE round must not dispatch repairs — it asks the operator.

Background
----------
The CI gate has a third verdict next to ``PASSED`` and ``FAILED``:
``INCONCLUSIVE`` (``verification_ci_runner.STATUS_INCONCLUSIVE``).  A gate
that ran out of its wall-clock budget did not prove the deliverable
defective — it proved the *gate* ran out of time.  The runner says so and
carries the evidence: which phase exhausted the budget
(``budget_exhausted_in``).

The orchestrator did not know the word at all: ``_round_judged_nothing``
only knew ``DEFERRED`` and the environment-blocked signal, so an
INCONCLUSIVE round fell into the failure path — the repair model was
asked to rework a deliverable for a defect nobody found, and the plan
was recorded as a failed verification.

What these tests pin
--------------------
 1. an INCONCLUSIVE entry makes the round "judged nothing";
 2. such a round asks the operator for a decision — only a human can say
    whether the gate gets a bigger budget or the plan moves on;
 3. a DEFERRED round keeps asking nobody (existing behaviour);
 4. a real failure keeps its repair path (the negative control);
 5. the round's exit carries the budget evidence and no repair tasks;
 6. the ``full_ci_executed`` event carries the exhausted phase and the
    duration, so an operator reading the round log can see *why* the
    round could not decide.

The interface contract
-----------------------
``_round_judged_nothing(report, failed_count)`` gains the word
``INCONCLUSIVE``; ``_round_needs_operator_decision(report)`` is new; and
``_judged_nothing_cycle_result(report)`` is the single source of the
judged-nothing exit — ``check_cycle_conditions`` returns it verbatim.

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

from verification.orchestrator import (  # noqa: E402
    _judged_nothing_cycle_result,
    _round_judged_nothing,
    _round_needs_operator_decision,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    Without the override the orchestrator would write its row into the
    repo's real ``state.db`` and read back whatever an earlier test left
    behind.  Autouse, so no test in this module can leak into — or
    inherit from — another.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


def _inconclusive_report() -> dict:
    """A round whose gate ran out of budget and could not decide.

    ``VP-1`` carries the verdict the runner produced and the evidence it
    produced it with: the phase whose budget was exhausted.
    """
    return {
        "overall_status": "SKIPPED",
        "verification_results": [
            {
                "vp_id": "VP-1",
                "status": "INCONCLUSIVE",
                "evidence": {"budget_exhausted_in": "unit"},
            },
        ],
    }


def _deferred_report() -> dict:
    """A round the plan could not judge yet — no operator question."""
    return {
        "overall_status": "SKIPPED",
        "verification_results": [
            {"vp_id": "VP-2", "status": "DEFERRED"},
        ],
    }


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=Mock(),
    )


def _ci_vp(**over) -> dict:
    vp = {
        "id": "VP-017",
        "title": "【全量】Nightly CI",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": "exit 0",
    }
    vp.update(over)
    return vp


# ---------------------------------------------------------------------------
# The round that judged nothing
# ---------------------------------------------------------------------------


def test_an_inconclusive_entry_makes_the_round_judge_nothing():
    """A gate that could not decide judged nothing — like a deferred point."""
    assert _round_judged_nothing(_inconclusive_report(), 0) is True


def test_an_inconclusive_round_asks_the_operator():
    """Only a human can say whether the gate gets a bigger budget."""
    assert _round_needs_operator_decision(_inconclusive_report()) is True


def test_a_deferred_round_does_not_ask_the_operator():
    """A deferred round keeps asking nobody — existing behaviour."""
    assert _round_needs_operator_decision(_deferred_report()) is False


def test_a_real_failure_keeps_the_repair_path():
    """The negative control: a real failure still produces repair tasks."""
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"vp_id": "VP-9", "status": "FAILED"},
            {
                "vp_id": "VP-1",
                "status": "INCONCLUSIVE",
                "evidence": {"budget_exhausted_in": "unit"},
            },
        ],
    }
    assert _round_judged_nothing(report, 1) is False


def test_a_round_without_entries_asks_nobody():
    """No entries means no INCONCLUSIVE entry — nothing to decide."""
    for report in (
        {},
        {"overall_status": "PASSED"},
        {"verification_results": None},
        {"verification_results": "not-a-list"},
    ):
        assert _round_judged_nothing(report, 0) is False, report
        assert _round_needs_operator_decision(report) is False, report


def test_a_mixed_passed_and_inconclusive_round_asks_the_operator():
    """A passing point does not turn the inconclusive one into a verdict."""
    report = {
        "overall_status": "PASSED",
        "verification_results": [
            {"vp_id": "VP-3", "status": "PASSED"},
            {
                "vp_id": "VP-1",
                "status": "INCONCLUSIVE",
                "evidence": {"budget_exhausted_in": "unit"},
            },
        ],
    }
    assert _round_judged_nothing(report, 0) is True
    assert _round_needs_operator_decision(report) is True


# ---------------------------------------------------------------------------
# The exit the round leaves behind
# ---------------------------------------------------------------------------


def test_the_cycle_result_marks_the_round_for_the_operator():
    """No repair tasks, the budget evidence, and the operator's turn."""
    result = _judged_nothing_cycle_result(_inconclusive_report())

    assert result["repair_tasks"] == [], result
    assert result["needs_operator_decision"] is True, result
    assert result["budget_exhausted_in"] == "unit", result
    assert result["environment_blocked"] is True, result
    assert result["status"] == "loop_stopped", result
    assert result["should_continue"] is False, result
    assert result["should_stop"] is True, result
    assert result["stop_reason"] is None, result
    assert result["waiting_for_user"] is False, result
    assert result["intent_conflicts"] == [], result
    assert result["root_cause_scope_missing"] == [], result


def test_the_cycle_result_of_a_deferred_round_asks_nobody():
    """A deferred round keeps every key and asks no one."""
    result = _judged_nothing_cycle_result(_deferred_report())

    assert result["needs_operator_decision"] is False, result
    assert result["budget_exhausted_in"] is None, result
    assert result["repair_tasks"] == [], result
    assert result["status"] == "loop_stopped", result
    assert result["environment_blocked"] is True, result
    assert result["root_cause_scope_missing"] == [], result


def test_the_orchestrator_returns_the_same_exit(tmp_path):
    """``check_cycle_conditions`` is the judged-nothing exit's only source."""
    from verification.orchestrator import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": "executing",
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(_inconclusive_report())

    assert result == _judged_nothing_cycle_result(_inconclusive_report()), result
    assert result["repair_tasks"] == [], result
    assert result["needs_operator_decision"] is True, result
    assert result["budget_exhausted_in"] == "unit", result


# ---------------------------------------------------------------------------
# The event the round log carries
# ---------------------------------------------------------------------------


def test_the_full_ci_event_carries_the_exhausted_phase(tmp_path, monkeypatch):
    """The audit event says which phase ran out of budget, and for how long."""
    import verification_ci_runner
    from verification_ci_runner import CiRunResult, STATUS_INCONCLUSIVE

    def _fake_run(vp, *, project_dir, artifact_dir=None):
        return CiRunResult(
            status=STATUS_INCONCLUSIVE,
            reasons=["[full_ci] `exit 0` 预算用尽（用时 42s）"],
            evidence={
                "vp_id": str(vp.get("id", "unknown")),
                "exit_code": None,
                "timed_out": False,
                "duration_seconds": 42.5,
                "budget_exhausted_in": "unit",
            },
        )

    monkeypatch.setattr(
        verification_ci_runner, "run_ci_verification", _fake_run,
    )

    captured: dict = {}

    agent = _make_agent(tmp_path)

    def _capture(vp_id, event_type, data):
        captured["vp_id"] = vp_id
        captured["event_type"] = event_type
        captured["data"] = data

    agent._safe_log_freshness_event = _capture

    verdict = agent._run_full_ci_vp(_ci_vp())

    assert verdict["status"] == STATUS_INCONCLUSIVE, verdict
    assert captured["event_type"] == "full_ci_executed", captured
    payload = captured["data"]
    assert payload["budget_exhausted_in"] == "unit", payload
    assert payload["duration_seconds"] == 42.5, payload
    assert payload["status"] == STATUS_INCONCLUSIVE, payload
