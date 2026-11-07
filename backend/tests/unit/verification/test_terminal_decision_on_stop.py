"""The ordinary repair loop's terminal round must **ask** a human.

Background
----------
2026-11-06: the root-cause chain (``root_cause`` -> ``implementation`` ->
``MACHINE_EXHAUSTED``) is deleted.  What is left is the ordinary repair
loop -- *VP failed -> repair task -> execute -> verify again* -- with a
budget of **two** repair rounds: the third same-cause failure stops the
round and a human takes over.

Before the chain was deleted, the terminal decision (the question that
carries facts, the decision row, the session sidecar, the CPEA archive)
lived in the **chain's** terminal branch.  That branch is gone, so the
whole apparatus moved to the "same cause, budget spent" branch -- the
``should_stop`` path of ``check_cycle_conditions``.  Without the move
the plan would **stop without anybody being asked**: the loop would
end, the operator would never hear about it, and the failure would sit
on disk with nobody looking.

What these tests pin
--------------------
1. **A stop asks.**  The triage says "stop" -> exactly one **plan-level**
   decision (``vp_id=None``) is created, and the round's payload says
   ``needs_operator_decision``.
2. **A passing round asks nothing** (negative control -- the wiring must
   not be too wide).
3. **The plan is asked once.**  A second stop does not create a second
   decision: the plan-level check
   (:meth:`VerificationOrchestrator._plan_has_terminal_decision`) says
   the plan already has the question.
4. **The repair budget.**  Three same-cause failures produce exactly
   **two** batches of repair tasks (the first two failures); the third
   failure produces none and asks instead.

Everything is driven through the **production entry** --
``check_cycle_conditions`` -- so "the branch is wired" cannot be true
only for a direct call into an internal method.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import hitl.service  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402

pytestmark = pytest.mark.unit


#: The point every round below fails on.
VP_ID = "VP-3"

#: The failure class the rounds carry.  It counts (it is neither
#: ``infra`` nor ``out_of_scope``), so the same-cause counter moves.
FAILURE_CLASS = "deliverable_defect"

#: The recorded failure text -- the triage judge may only quote from text
#: that is really there.
ACTUAL_RESULT = "AssertionError: expected 200, got 500"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "terminal-decision-on-stop"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "terminal-decision-on-stop"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the round's rows are observable.

    The generator **echoes** the candidates it was handed, so what the
    round decided about a row is what the caller can read back.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator, \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": VP_ID, "title": "Fix VP-3", "priority": "high"},
        ]
        mock_generator.return_value.generate_repair_contents.return_value = [
            {"failed_vp_id": VP_ID, "title": "Fix VP-3"},
        ]
        mock_assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "failed_vp_id": VP_ID, "title": "Fix VP-3"},
        ]
        yield mock_generator


class _FakeTriageJudge:
    """A triage judge that answers "the deliverable is wrong"."""

    def __init__(self, failure_class: str = FAILURE_CLASS) -> None:
        self.failure_class = failure_class
        self.calls = []

    def judge(self, vp, failure_history, round_number=0):
        self.calls.append({
            "vp": dict(vp) if isinstance(vp, dict) else vp,
            "failure_history": list(failure_history or []),
            "round_number": round_number,
        })
        from verification.triage_agent import TriageConclusion

        return TriageConclusion(
            vp_id=VP_ID,
            failure_class=self.failure_class,
            conclusive=True,
            evidence_quote=ACTUAL_RESULT,
            rationale="the deliverable does not do what the point asks",
        )


class _FakeTerminalJudge:
    """The terminal analysis judge -- a fixed, well-formed analysis."""

    def __init__(self) -> None:
        self.last_session_id = "sess-stop"
        self.kwargs = None

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return {
            "context": "the third round stopped on the same failure",
            "problem": "the same assertion failed for the third time",
            "evidence": [f"{VP_ID} failed"],
            "recommendation": "manual_repair_tasks",
            "recommendation_reason": "two repair rounds did not fix it",
            "uncertainty": "the server log was never read",
        }


@pytest.fixture
def decision_calls(monkeypatch):
    """A **pass-through** spy on ``hitl.service.create_decision``.

    The spy records the request and then calls the real implementation,
    so the decision really lands in ``plan_decisions`` -- a spy that only
    recorded would leave the table empty and prove nothing.
    """
    real = hitl.service.create_decision
    calls = []

    def spy(request, *args, **kwargs):
        calls.append(request)
        return real(request, *args, **kwargs)

    monkeypatch.setattr(hitl.service, "create_decision", spy)
    return calls


# ---------------------------------------------------------------------------
# The scenario on disk
# ---------------------------------------------------------------------------


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


def _fail_report():
    """A round report: the point failed, the same way it always fails."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "failure_class": FAILURE_CLASS,
                "actual_result": ACTUAL_RESULT,
                "evidence": ACTUAL_RESULT,
            },
        ],
        "requirement_deviations": [],
    }


def _pass_report():
    """A round report: the point passed -- nothing is broken, nothing to ask."""
    return {
        "overall_status": "PASSED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "PASSED",
                "actual_result": "200",
                "evidence": "200",
            },
        ],
        "requirement_deviations": [],
    }


def _orchestrator(plan_dir, project_dir, coding_tool, triage_judge=None,
                  terminal_judge=None):
    """An orchestrator with the two judges injected."""
    return VerificationOrchestrator(
        plan_dir, project_dir, coding_tool,
        triage_judge=(
            triage_judge if triage_judge is not None else _FakeTriageJudge()
        ),
        terminal_judge=(
            terminal_judge if terminal_judge is not None else _FakeTerminalJudge()
        ),
    )


def _run_round(orchestrator, report, round_number):
    """One verification round: start the cycle, then classify it."""
    orchestrator.start_verification_cycle(round_number=round_number)
    return orchestrator.check_cycle_conditions(
        report, round_number=round_number,
    )


def _run_rounds(plan_dir, project_dir, coding_tool, reports,
                triage_judge=None, terminal_judge=None):
    """Run a sequence of rounds; return ``[(orchestrator, result), ...]``.

    Every round runs through a **fresh** orchestrator -- that is how
    production runs it (the auto-loop rebuilds the orchestrator after
    every repair), so anything a round remembers has to come back from
    disk.
    """
    results = []
    for round_number, report in enumerate(reports, start=1):
        orchestrator = _orchestrator(
            plan_dir, project_dir, coding_tool,
            triage_judge=triage_judge,
            terminal_judge=terminal_judge,
        )
        results.append((orchestrator, _run_round(orchestrator, report, round_number)))
    return results


# ---------------------------------------------------------------------------
# 1. The stop asks a human
# ---------------------------------------------------------------------------


def test_stop_creates_one_plan_level_decision(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """Three same-cause failures -> exactly one plan-level decision."""
    _write_plan_state(plan_dir)
    coding_tool = Mock()

    results = _run_rounds(
        plan_dir, project_dir, coding_tool,
        [_fail_report(), _fail_report(), _fail_report()],
    )
    _, third = results[-1]

    assert third["should_stop"] is True, (
        "the third same-cause failure must stop the round"
    )
    assert third["status"] == "loop_stopped"
    assert third["repair_tasks"] == [], (
        "a stopped round produces no repair task -- the budget is spent"
    )
    assert len(decision_calls) == 1, (
        "the terminal round must ask exactly one question -- the plan's "
        f"questions were {decision_calls!r}"
    )
    request = decision_calls[0]
    assert request.vp_id is None, (
        "the question is about the plan, not about one point -- the "
        f"person who decides what happens to the plan is not one "
        f"point's owner (vp_id was {request.vp_id!r})"
    )
    assert request.source == "verification"
    assert request.options, "the question must offer answers on the table"
    assert request.default_option in request.options
    assert third["needs_operator_decision"] is True, (
        "the payload must say a human has to act"
    )
    # The two "is somebody waiting" fields are **read** from the store, so
    # they agree with each other by construction.
    assert third["waiting_for_user"] == third["blocked_on_user"], (
        "the two waiting flags are read from the same place, so they "
        "cannot disagree"
    )


def test_the_question_names_the_real_stop_reason(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """The question names the reason the round really stopped for.

    The judge that reads the failure scene is a **model call**, and it
    may answer with nothing.  When it does, the question is the
    factual sentence -- and that sentence must name the reason the
    round really stopped for, not the root-cause chain's old wording.
    """
    _write_plan_state(plan_dir)
    coding_tool = Mock()

    class _NoAnalysisJudge(_FakeTerminalJudge):
        def analyze(self, **kwargs):
            self.kwargs = kwargs
            return None

    _run_rounds(
        plan_dir, project_dir, coding_tool,
        [_fail_report(), _fail_report(), _fail_report()],
        terminal_judge=_NoAnalysisJudge(),
    )

    assert len(decision_calls) == 1
    question = decision_calls[0].question
    assert "same_failure_repeated_after_max_attempts" in question, (
        f"the question must name the reason the round really stopped for "
        f"(question was {question!r})"
    )
    assert "repair_execution_failed" not in question, (
        f"the root-cause chain's wording must not be the question "
        f"(question was {question!r})"
    )


# ---------------------------------------------------------------------------
# 2. A passing round asks nothing (negative control)
# ---------------------------------------------------------------------------


def test_passing_round_creates_no_decision(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """A fully passing round creates no decision at all."""
    _write_plan_state(plan_dir)
    coding_tool = Mock()

    results = _run_rounds(
        plan_dir, project_dir, coding_tool, [_pass_report()],
    )
    _, result = results[-1]

    assert result["status"] == "passed"
    assert decision_calls == [], (
        "a passing round must not ask anybody anything"
    )


# ---------------------------------------------------------------------------
# 3. The plan is asked once
# ---------------------------------------------------------------------------


def _seed_open_terminal_decision(plan_id, round_number):
    """Write an **open** terminal question for the plan into the store.

    The row is seeded, not asked: this is the state the plan is in when
    somebody is already holding its terminal question.  Reaching it
    through the delivery layer would need an outlet to deliver to, and a
    hermetic test has none -- so the question the plan already holds is
    written straight into the store.
    """
    from config_paths import resolve_state_db_path
    from hitl.models import Decision, DecisionRequest
    from hitl.store import HitlStore

    request = DecisionRequest(
        plan_id=plan_id,
        source="verification",
        vp_id=None,
        round=round_number,
        question=(
            f"verification round {int(round_number)} stopped with "
            f"same_failure_repeated_after_max_attempts: 0 repair task(s) "
            f"were generated; failed checks: {VP_ID}=FAILED. Choose how "
            f"to proceed."
        ),
        options=["rerun_this_round", "accept_as_failed", "manual_repair_tasks"],
        default_option="accept_as_failed",
    )
    decision = Decision.from_request(request)
    decision.status = "pending"
    store = HitlStore(resolve_state_db_path())
    try:
        store.insert(decision)
    finally:
        store.close()
    return decision


def test_the_plan_is_asked_once(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """The plan holds an open question -> the round does not ask again.

    The question is about the **plan**, so the point-keyed check cannot
    see it; without the plan-level check every entry into the branch
    wrote a new row, and the person answering it got the same question
    once per round.
    """
    _write_plan_state(plan_dir)
    coding_tool = Mock()
    seeded = _seed_open_terminal_decision(plan_dir.name, 3)

    results = _run_rounds(
        plan_dir, project_dir, coding_tool,
        [_fail_report(), _fail_report(), _fail_report()],
    )
    _, third = results[-1]

    assert third["should_stop"] is True
    assert decision_calls == [], (
        f"the plan is asked once, not once per round: {decision_calls!r}"
    )
    from hitl.service import list_decisions

    stored = list_decisions(plan_dir.name)
    assert len(stored) == 1, (
        f"the question the plan already holds is the only one: {stored!r}"
    )
    assert stored[0].decision_id == seeded.decision_id
    # The plan holds an open question, so somebody really is waiting.
    assert third["needs_operator_decision"] is True
    assert third["waiting_for_user"] is True
    assert third["blocked_on_user"] is True


# ---------------------------------------------------------------------------
# 4. The repair budget: two rounds of repair, then a human
# ---------------------------------------------------------------------------


def test_three_same_failures_buy_exactly_two_repair_batches(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """Three same-cause failures -> exactly two batches of repair tasks."""
    _write_plan_state(plan_dir)
    coding_tool = Mock()
    triage_judge = _FakeTriageJudge()

    results = _run_rounds(
        plan_dir, project_dir, coding_tool,
        [_fail_report(), _fail_report(), _fail_report()],
        triage_judge=triage_judge,
    )

    batches = [
        bool(result.get("repair_tasks")) for _, result in results
    ]
    assert batches == [True, True, False], (
        "the budget is two repair rounds: the first two failures each "
        f"buy a batch of repair tasks, the third buys none (batches: "
        f"{batches!r})"
    )
    assert len(triage_judge.calls) == 1, (
        "the triage buys exactly one call -- it runs at the cap, not on "
        f"every round (calls: {len(triage_judge.calls)})"
    )
    assert len(decision_calls) == 1, (
        "the third failure stops the round and asks a human"
    )
