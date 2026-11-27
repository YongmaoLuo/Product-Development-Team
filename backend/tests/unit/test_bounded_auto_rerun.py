"""An auto-rerun must have a hard ceiling — and the ceiling must count.

Background
----------
``rerun_this_round`` is a terminal decision the **clock** may take on
the operator's behalf: the decision falls back to its default answer
when nobody replies.  The answer resets the plan's verification state
and starts the round again — and the reset used to throw the counters
away with everything else:

* ``reset_verification`` deleted ``verification_loop_tracking.json``
  outright (``clear_artifacts`` is on by default), and
* the round counter was rewound to ``restart_at_round - 1``, so every
  round counted from one again.

Two consequences, both invisible to the plan:

1. the unjudged-round streak was zeroed by every auto-rerun, so the
   "the third unjudged round in a row waits for a person" rule could
   never fire — the clock kept answering for the plan;
2. the terminal question had no ceiling of its own: a decision that
   fell back to its default did not stop the next round from asking
   the same question again, so the clock could answer the plan for
   ever.

What these tests pin
--------------------
 1. **A reset keeps the two safety counts.**  ``unjudged_streak`` and
    ``terminal_auto_streak`` are *how many times the machine has
    already answered for this plan* — a reset does not un-ask the
    question, so it must not un-count the answer.  The verdict set
    (``previous_failed_keys`` / ``same_failure_counts``) still goes
    with the reset: it counts verdicts against a plan generation that
    no longer exists.
 2. **The third terminal question waits for a person.**  Strict mode,
    no default answer, no deadline — the clock does not get to decide
    what happens to a plan it has already answered twice.
 3. **The unjudged streak survives a reset.**  The same rule, seen
    through the unjudged round's own door: a plan that was answered
    by the clock twice must not get two more free rounds.
 4. **A conclusion ends both streaks.**  The counters are *streaks*:
    a plan that concluded has nothing left to answer.

The interface contract
-----------------------
``verification_loop_tracking.json`` grows a second safety key
(``terminal_auto_streak``); the orchestrator reads and writes it the
way it reads and writes ``unjudged_streak``; and the reset route
preserves both counts across the deletion of the file they live in.

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


#: The answers the terminal question offers — the terminal branch's two.
EXPECTED_OPTIONS = ["rerun_this_round", "revise_criterion"]

#: The answer the terminal question falls back to when nobody replies.
EXPECTED_DEFAULT = "rerun_this_round"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state **and** HITL persistence at a hermetic SQLite file.

    Both live in the same ``state.db``: ``PlanState`` keeps the plan's
    routing row there, ``hitl.service`` keeps ``plan_decisions`` there
    and the reset route keeps the plan's verification row there.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def plan_dir(tmp_path):
    """A plan directory with the plan state the orchestrator reads."""
    path = tmp_path / "plans" / "20261127-bounded-rerun"
    path.mkdir(parents=True)
    (path / "plan_state.json").write_text(
        json.dumps({
            "plan_id": path.name,
            "current_phase": "verification",
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
    return path


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "projects" / "bounded-rerun"
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tracking_path(plan_dir):
    return plan_dir / "verification_loop_tracking.json"


def _tracking(plan_dir):
    """The loop-tracking payload on disk."""
    return json.loads(_tracking_path(plan_dir).read_text(encoding="utf-8"))


def _write_tracking(plan_dir, **keys):
    """Write a loop-tracking payload, as a previous round would have."""
    payload = {"version": 2, "last_round": 0}
    payload.update(keys)
    _tracking_path(plan_dir).write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _reset_plan(plan_dir, monkeypatch):
    """Run the production reset route over the plan.

    The route is the **production entry** for a reset — the terminal
    decision's port calls it — so the test goes through it rather than
    through the file deletion it happens to do.
    """
    import server  # noqa: F401  (import order is load-bearing)

    from routes import verification as routes_verification

    monkeypatch.setattr(server, "_plan_dir", lambda _pid: plan_dir)
    server._verification_state.clear()
    routes_verification.reset_verification(
        plan_dir.name, server.ResetVerificationRequest(),
    )


def _decisions(plan_id):
    from hitl.service import list_decisions

    return list_decisions(plan_id)


def _unknown_report():
    """A round whose point's outcome could not be read."""
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "verification_results": [
            {"id": "VP-1", "status": "UNKNOWN", "evidence": {}},
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


def _failed_report():
    """A round whose one verification point failed."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "title": "登录接口",
                "expected_result": "返回 200",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            },
        ],
    }


class _FakeTriageJudge:
    """A triage judge that answers "the deliverable is wrong".

    The triage is what decides whether a stuck round stops or repairs
    itself again.  Its answer ``deliverable_defect`` means the repair
    budget is spent, so the loop stops and asks a human.
    """

    def judge(self, vp, failure_history, round_number=0):
        from verification.triage_agent import TriageConclusion

        return TriageConclusion(
            vp_id="VP-3",
            failure_class="deliverable_defect",
            conclusive=True,
            evidence_quote="expected 200, got 500",
            rationale="the deliverable does not do what the point asks",
        )


class _FakeTerminalJudge:
    """A terminal judge that offers no analysis."""

    def __init__(self):
        self.last_analysis_error = ""
        self.last_session_id = "sess-abc"

    def analyze(self, **kwargs):
        return None


def _terminal_orchestrator(plan_dir, project_dir):
    """An orchestrator whose heavy collaborators are stubbed out."""
    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
        terminal_judge=_FakeTerminalJudge(),
        triage_judge=_FakeTriageJudge(),
    )
    orchestrator.verification_agent = Mock()
    orchestrator.repair_generator = Mock()
    return orchestrator


def _run_terminal_round(orchestrator, report, round_number=3):
    """Drive the orchestrator into the terminal branch.

    The same failure has to repeat three times before the chain's
    hard cap fires, so the round is run until it stops.
    """
    verdict = {}
    for _ in range(3):
        verdict = orchestrator.check_cycle_conditions(report, round_number)
        if verdict.get("should_stop"):
            break
    return verdict


# ---------------------------------------------------------------------------
# A. The reset keeps the two safety counts
# ---------------------------------------------------------------------------


def test_reset_keeps_the_safety_counts_and_drops_the_verdicts(
    plan_dir, monkeypatch,
):
    """A reset keeps the counts; it drops the verdicts they sat next to.

    ``unjudged_streak`` and ``terminal_auto_streak`` answer "how many
    times has the machine already answered for this plan?"  A reset
    does not un-ask the question.  ``previous_failed_keys`` and
    ``same_failure_counts`` count *verdicts* against a plan generation
    that the reset throws away — inheriting them would stop the very
    next round on a comparison against verdicts that no longer exist.
    """
    _write_tracking(
        plan_dir,
        previous_failed_keys=["VP-3|deliverable_defect"],
        same_failure_counts={"VP-3|deliverable_defect": 3},
        unjudged_streak=2,
        terminal_auto_streak=2,
    )

    _reset_plan(plan_dir, monkeypatch)

    payload = _tracking(plan_dir)
    assert payload["unjudged_streak"] == 2, payload
    assert payload["terminal_auto_streak"] == 2, payload
    assert payload.get("previous_failed_keys", []) == [], payload
    assert payload.get("same_failure_counts", {}) == {}, payload
    assert payload["version"] == 2, payload


def test_reset_without_a_count_leaves_no_tracking_file(
    plan_dir, monkeypatch,
):
    """A plan with no count gets no file back.

    The write-back only happens when there is a count to preserve: a
    plan that was never answered by the clock keeps the file it had —
    which is none.
    """
    _write_tracking(plan_dir, previous_failed_keys=["VP-3|deliverable_defect"])

    _reset_plan(plan_dir, monkeypatch)

    assert not _tracking_path(plan_dir).exists()


# ---------------------------------------------------------------------------
# B. The third terminal question waits for a person
# ---------------------------------------------------------------------------


def test_the_third_terminal_question_asks_strictly(
    plan_dir, project_dir,
):
    """Two clock-answered terminal questions, then a person.

    The first two carry a default answer, so a plan nobody answers
    falls back to *rerun this round* and carries on by itself.  The
    third does not: the clock has already answered twice, and letting
    it answer a third time is the system deciding what happens to the
    plan.
    """
    _write_tracking(plan_dir, terminal_auto_streak=2)

    orchestrator = _terminal_orchestrator(plan_dir, project_dir)
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as extract, \
         patch("repair_generator.RepairTaskAssembler") as assembler:
        extract.return_value = [
            {"id": "VP-3", "title": "修 VP-3", "priority": "high"},
        ]
        assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "failed_vp_id": "VP-3", "title": "修 VP-3"},
        ]
        verdict = _run_terminal_round(orchestrator, _failed_report())

    assert verdict.get("should_stop") is True, verdict

    decisions = _decisions(plan_dir.name)
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision.mode == "strict", decision.mode
    assert decision.deadline_at is None, decision.deadline_at
    assert decision.default_option is None, decision.default_option
    assert decision.options == EXPECTED_OPTIONS, decision.options

    # The count that produced the strict question is on disk, so the
    # next round's fresh orchestrator continues it.
    assert _tracking(plan_dir)["terminal_auto_streak"] == 3, _tracking(plan_dir)


def test_the_first_terminal_question_asks_the_ordinary_way(
    plan_dir, project_dir,
):
    """The first terminal question keeps its default answer."""
    _write_tracking(plan_dir, terminal_auto_streak=0)

    orchestrator = _terminal_orchestrator(plan_dir, project_dir)
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as extract, \
         patch("repair_generator.RepairTaskAssembler") as assembler:
        extract.return_value = [
            {"id": "VP-3", "title": "修 VP-3", "priority": "high"},
        ]
        assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "failed_vp_id": "VP-3", "title": "修 VP-3"},
        ]
        _run_terminal_round(orchestrator, _failed_report())

    decisions = _decisions(plan_dir.name)
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision.mode == "auto", decision.mode
    assert decision.default_option == EXPECTED_DEFAULT, decision.default_option
    assert _tracking(plan_dir)["terminal_auto_streak"] == 1, _tracking(plan_dir)


# ---------------------------------------------------------------------------
# C. The unjudged streak survives a reset
# ---------------------------------------------------------------------------


def test_the_unjudged_streak_survives_a_reset(
    plan_dir, project_dir, monkeypatch,
):
    """A reset does not hand the clock two more free rounds.

    The unjudged round's own rule: the third unjudged round in a row
    waits for a person.  An auto-rerun used to reset that count, so
    the rule could never fire — the plan was answered by the clock
    for ever.
    """
    _write_tracking(plan_dir, unjudged_streak=2)

    _reset_plan(plan_dir, monkeypatch)

    assert _tracking(plan_dir)["unjudged_streak"] == 2, _tracking(plan_dir)

    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
    )
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        result = orchestrator.check_cycle_conditions(
            _unknown_report(), round_number=1,
        )

    decisions = _decisions(plan_dir.name)
    assert len(decisions) == 1, decisions
    assert decisions[0].mode == "strict", decisions[0].mode
    assert decisions[0].deadline_at is None, decisions[0].deadline_at
    assert decisions[0].default_option is None, decisions[0].default_option
    assert _tracking(plan_dir)["unjudged_streak"] == 3, _tracking(plan_dir)
    assert result["waiting_for_user"] is True, result


# ---------------------------------------------------------------------------
# D. A conclusion ends both streaks
# ---------------------------------------------------------------------------


def test_a_passing_round_ends_both_streaks(
    plan_dir, project_dir,
):
    """A plan that concluded has nothing left to answer.

    Both counters are streaks: a round that judged the deliverable
    ends the unjudged one, and the conclusion itself ends the terminal
    one — the plan is not going to be asked what happens next.
    """
    _write_tracking(plan_dir, unjudged_streak=2, terminal_auto_streak=2)

    orchestrator = VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=Mock(),
    )
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orchestrator.check_cycle_conditions(_passed_report(), round_number=1)

    payload = _tracking(plan_dir)
    assert payload.get("unjudged_streak", 0) == 0, payload
    assert payload.get("terminal_auto_streak", 0) == 0, payload
