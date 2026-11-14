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

The word was lost even earlier, on the way in.  The executor's verdict
schema did not know ``INCONCLUSIVE`` either, and a verdict it rejects
is rewritten into ``FAILED`` with an **empty** evidence dict — so the
gate's "I could not decide" was recorded as "the deliverable failed".
And the report layer had no way to restore it: the Phase-3 judgment
LLM's vocabulary is PASSED|FAILED|SKIPPED|PARTIAL, so it answered
SKIPPED for a gate that ran out of budget, and the round looked like a
round nobody judged — the operator was never asked, and the plan spun
until ``max_rounds``.

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
    round could not decide;
 7. the executor's schema accepts the word, so the ledger keeps it
    instead of turning it into an evidence-less FAILED;
 8. the report layer restores the ledger's INCONCLUSIVE — the judgment
    layer cannot say it, and the report may not lose it either;
 9. an INCONCLUSIVE point buys no requirement deviation, and the round
    never calls itself PASSED;
10. nothing else moves: a real failure, an empty round, an all-PASSED
    round and a DEFERRED round all keep their behaviour.

The interface contract
-----------------------
``_round_judged_nothing(report, failed_count)`` gains the word
``INCONCLUSIVE``; ``_round_needs_operator_decision(report)`` is new; and
``_judged_nothing_cycle_result(report)`` is the single source of the
judged-nothing exit — ``check_cycle_conditions`` returns it verbatim.

On the way in, ``verification_executor.ALLOWED_STATUSES`` gains the
word; ``verification_buckets.bucket_for_status`` files it next to
DEFERRED (never next to FAILED — the failed bucket is the repair task's
input); and ``verification_agent`` restores it from the ledger, the way
it already restores DEFERRED.

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


def test_the_cycle_result_of_a_deferred_round_asks_nobody():
    """A deferred round keeps every key and asks no one."""
    result = _judged_nothing_cycle_result(_deferred_report())

    assert result["needs_operator_decision"] is False, result
    assert result["budget_exhausted_in"] is None, result
    assert result["repair_tasks"] == [], result
    assert result["status"] == "loop_stopped", result
    assert result["environment_blocked"] is True, result


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


# ---------------------------------------------------------------------------
# The ledger keeps the word (the executor's verdict schema)
# ---------------------------------------------------------------------------


def _make_executor(tmp_path: Path):
    """An executor over an empty plan — only its verdict schema matters."""
    from verification_executor import VerificationExecutor

    return VerificationExecutor(
        verification_plan={"vps": []},
        plan_id="vp013",
        plan_dir=tmp_path,
        sub_agent_runner=Mock(),
    )


def test_the_executor_schema_accepts_an_inconclusive_verdict(tmp_path):
    """A gate that ran out of budget has a verdict the schema accepts.

    The schema is the ledger's gate: a status it does not know is
    rewritten into ``FAILED`` with an **empty** evidence dict
    (``_run_single_vp``), so the runner's INCONCLUSIVE used to land in
    the ledger as "the deliverable failed" — the failure that buys a
    repair task for a defect nobody found.
    """
    from verification_executor import ALLOWED_STATUSES

    assert "INCONCLUSIVE" in ALLOWED_STATUSES, ALLOWED_STATUSES

    executor = _make_executor(tmp_path)
    verdict = {"status": "INCONCLUSIVE", "reasons": [], "evidence": {}}
    assert executor._validate_verdict(verdict) is None


def test_the_executor_schema_still_rejects_an_unknown_status(tmp_path):
    """The negative control: the schema was widened by one word, not opened."""
    from verification_executor import VerdictSchemaError

    executor = _make_executor(tmp_path)
    with pytest.raises(VerdictSchemaError):
        executor._validate_verdict(
            {"status": "UNKNOWN", "reasons": [], "evidence": {}}
        )


def test_the_executor_schema_still_requires_the_three_keys(tmp_path):
    """The negative control: widening the vocabulary changes nothing else."""
    from verification_executor import VerdictSchemaError

    executor = _make_executor(tmp_path)
    with pytest.raises(VerdictSchemaError):
        executor._validate_verdict({"status": "INCONCLUSIVE", "reasons": []})


def test_an_inconclusive_verdict_lands_in_the_deferred_bucket(tmp_path):
    """The write side: an exhausted gate is deferred, never failed.

    This is the whole chain in one step — the schema accepts the word,
    and the bucket rule files it next to DEFERRED instead of next to
    FAILED.  The failed bucket is the repair task's input: a gate that
    could not decide must never land there, or the plan buys a task to
    fix "the thing the machine could not answer".
    """
    from verification_buckets import (
        BUCKET_DEFERRED,
        BUCKET_FAILED,
        derive_index_buckets,
    )
    from verification_executor import VerificationExecutor

    executor = VerificationExecutor(
        verification_plan={"vps": [{"id": "VP-1"}]},
        plan_id="vp013",
        plan_dir=tmp_path,
        sub_agent_runner=Mock(),
    )
    executor.record_verdict("VP-1", {
        "status": "INCONCLUSIVE",
        "reasons": ["[full_ci] `exit 0` 预算用尽（用时 42s）"],
        "evidence": {"budget_exhausted_in": "unit"},
    })

    buckets = derive_index_buckets({
        verdict["vp_id"]: verdict
        for verdict in executor.collect_verdicts()
    })
    assert buckets[BUCKET_FAILED] == [], buckets
    assert buckets[BUCKET_DEFERRED] == ["VP-1"], buckets


# ---------------------------------------------------------------------------
# The report layer restores the ledger's INCONCLUSIVE
# ---------------------------------------------------------------------------


class _Judgment:
    """The Phase-3 judgment LLM: it answers with a fixed payload.

    The real judgment LLM's vocabulary is PASSED|FAILED|SKIPPED|PARTIAL
    (see ``backend/prompts.py``) — it has no word for "the gate ran out
    of budget".  This stand-in makes that limitation explicit: it can
    only answer what its schema carries, so any INCONCLUSIVE that ends
    up in the report has to come from the ledger.
    """

    def __init__(self, payload: dict):
        self._payload = payload

    def query_json(self, *args, **kwargs):
        # A fresh copy per call: the report layer mutates what it gets.
        return json.loads(json.dumps(self._payload))


def _report_agent(tmp_path: Path, payload: dict):
    """An orchestrator whose judgment LLM answers with ``payload``."""
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=_Judgment(payload),
    )


def _skipped_judgment() -> dict:
    """A judgment payload that can only say SKIPPED about the gate."""
    return {
        "overall_status": "PASSED",
        "summary": "no verdict the schema can carry for an exhausted gate",
        "verification_results": [{
            "id": "VP-X",
            "status": "SKIPPED",
            "actual_result": "the gate could not decide",
            "evidence": json.dumps({"budget_exhausted_in": "phase 2"}),
        }],
        "requirement_deviations": [],
    }


def _ledger_inconclusive() -> dict:
    """The ledger's own entry for a gate that ran out of budget."""
    return {
        "id": "VP-X",
        "status": "INCONCLUSIVE",
        "actual_result": "gate budget exhausted",
        "evidence": json.dumps({"budget_exhausted_in": "phase 2"}),
    }


def test_the_report_layer_restores_the_ledger_inconclusive_verdict(tmp_path):
    """An executor-INCONCLUSIVE point survives the judgment layer.

    The judgment LLM cannot say INCONCLUSIVE, so it answers SKIPPED for
    a gate that ran out of budget — and the round then looks like a
    round nobody judged: the orchestrator never asks the operator, and
    the plan spins until ``max_rounds``.  The ledger is the authority
    here (the gate's own verdict says INCONCLUSIVE), so the report
    layer restores it — the same shape as the DEFERRED restoration.
    """
    agent = _report_agent(tmp_path, _skipped_judgment())

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_ledger_inconclusive()],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "INCONCLUSIVE", entry


def test_the_restored_entry_carries_the_ledger_evidence(tmp_path):
    """The ledger's text wins — the judgment's prose describes what never happened.

    The judgment layer wrote "the gate could not decide"; the ledger
    wrote "gate budget exhausted" and carries the phase that ran out.
    The operator reads the report to decide what happens next, so the
    ledger's words are the ones that belong there.
    """
    agent = _report_agent(tmp_path, _skipped_judgment())

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_ledger_inconclusive()],
    })

    entry = report["verification_results"][0]
    assert entry["actual_result"] == "gate budget exhausted", entry


def test_an_inconclusive_report_hands_the_round_to_the_operator(tmp_path):
    """The whole point of the word: an operator's turn, and no repair task."""
    agent = _report_agent(tmp_path, _skipped_judgment())

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_ledger_inconclusive()],
    })

    exit_ = _judged_nothing_cycle_result(report)
    assert exit_["needs_operator_decision"] is True, exit_
    assert exit_["repair_tasks"] == [], exit_
    assert exit_["environment_blocked"] is True, exit_


def test_a_missing_inconclusive_entry_is_added_back_from_the_ledger(tmp_path):
    """The report may not simply lose the point (VP-016's lesson).

    A gate that ran out of budget is still a point of the round: if the
    judgment layer dropped it, the round would look complete and green
    while the gate never decided anything.
    """
    judgment = _skipped_judgment()
    judgment["verification_results"] = []
    agent = _report_agent(tmp_path, judgment)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_ledger_inconclusive()],
    })

    entries = report["verification_results"]
    ids = [str(e.get("id")) for e in entries]
    assert "VP-X" in ids, entries
    restored = [e for e in entries if str(e.get("id")) == "VP-X"][0]
    assert restored["status"] == "INCONCLUSIVE", restored


def test_an_inconclusive_point_registers_no_requirement_deviation(tmp_path):
    """A deviation is a statement about the deliverable — this is not one.

    "The gate ran out of budget" says nothing about the product, so the
    point must not buy a requirement deviation (which is a claim that
    the implementation does not match the PRD).
    """
    judgment = _skipped_judgment()
    judgment["requirement_deviations"] = [{
        "verification_point_id": "VP-X",
        "type": "missing",
        "description": "the gate could not decide",
        "severity": "high",
    }]
    agent = _report_agent(tmp_path, judgment)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_ledger_inconclusive()],
    })

    deviations = report.get("requirement_deviations") or []
    assert [d for d in deviations
            if str(d.get("verification_point_id")) == "VP-X"] == [], deviations


def test_the_round_verdict_is_not_a_pass_when_a_gate_could_not_decide(tmp_path):
    """A round that could not decide everything says so.

    The DEFERRED point is the precedent: a point the round did not
    judge makes the round ``SKIPPED``, never ``PASSED``.
    """
    agent = _report_agent(tmp_path, _skipped_judgment())

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [_ledger_inconclusive()],
    })

    assert report["overall_status"] != "PASSED", report["overall_status"]


# ---------------------------------------------------------------------------
# Negative controls: nothing else moves
# ---------------------------------------------------------------------------


def test_a_real_failure_is_not_washed_into_an_inconclusive(tmp_path):
    """A point that ran and failed keeps its failure.

    The INCONCLUSIVE restoration reads the ledger, and the ledger says
    FAILED for this point — so it must stay FAILED, and the round must
    stay red.  This is the control that keeps the restoration honest:
    it restores the ledger's word, and only that word.
    """
    judgment = {
        "overall_status": "FAILED",
        "summary": "the gate failed",
        "verification_results": [{
            "id": "VP-F",
            "status": "FAILED",
            "actual_result": "the test failed",
            "evidence": json.dumps({"exit_code": 1}),
        }],
        "requirement_deviations": [],
    }
    agent = _report_agent(tmp_path, judgment)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [{
            "id": "VP-F",
            "status": "FAILED",
            "actual_result": "the test failed",
            "evidence": json.dumps({"exit_code": 1}),
        }],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", entry
    assert report["overall_status"] == "FAILED", report["overall_status"]


def test_a_round_without_entries_stays_a_pass(tmp_path):
    """The empty round keeps saying PASSED (existing behaviour)."""
    judgment = {
        "overall_status": "PASSED",
        "summary": "nothing to judge",
        "verification_results": [],
        "requirement_deviations": [],
    }
    agent = _report_agent(tmp_path, judgment)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [],
    })

    assert report["verification_results"] == [], report
    assert report["overall_status"] == "PASSED", report["overall_status"]


def test_an_all_passed_round_is_unchanged(tmp_path):
    """A green round stays green — the restoration adds nothing to it."""
    judgment = {
        "overall_status": "PASSED",
        "summary": "all good",
        "verification_results": [{
            "id": "VP-A",
            "status": "PASSED",
            "actual_result": "the test passed",
            "evidence": json.dumps({"exit_code": 0}),
        }],
        "requirement_deviations": [],
    }
    agent = _report_agent(tmp_path, judgment)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [{
            "id": "VP-A",
            "status": "PASSED",
            "actual_result": "the test passed",
            "evidence": json.dumps({"exit_code": 0}),
        }],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "PASSED", entry
    assert report["overall_status"] == "PASSED", report["overall_status"]


def test_a_deferred_round_is_unchanged(tmp_path):
    """A deferred gate keeps its own word — it is not an INCONCLUSIVE.

    The two are siblings, not the same thing: DEFERRED is "not this
    round" (the plan's own business), INCONCLUSIVE is "the gate could
    not decide" (the operator's business).  The restoration must not
    merge them.
    """
    judgment = {
        "overall_status": "SKIPPED",
        "summary": "the gate was deferred",
        "verification_results": [{
            "id": "VP-D",
            "status": "SKIPPED",
            "actual_result": "the gate was not asked",
            "evidence": json.dumps({}),
        }],
        "requirement_deviations": [],
    }
    agent = _report_agent(tmp_path, judgment)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [{
            "id": "VP-D",
            "status": "DEFERRED",
            "actual_result": "the gate was not asked",
            "evidence": json.dumps({}),
        }],
    })

    entry = report["verification_results"][0]
    assert entry["status"] != "INCONCLUSIVE", entry


# ---------------------------------------------------------------------------
# The judgment prompt
# ---------------------------------------------------------------------------


def test_the_judgment_prompt_keeps_its_four_valued_vocabulary():
    """INCONCLUSIVE is the framework's word — the LLM does not get to guess it."""
    from prompts import VERIFICATION_JUDGMENT_SYSTEM_PROMPT as prompt

    assert '"status": "PASSED|FAILED|SKIPPED|PARTIAL"' in prompt
    assert '"status": "PASSED|FAILED|SKIPPED|PARTIAL|INCONCLUSIVE"' not in prompt


def test_the_judgment_prompt_forbids_filing_an_undecided_gate_as_a_defect():
    """The prompt says what to do with a point the executor could not judge.

    The judgment layer must not call an exhausted gate a deliverable
    defect, and must not register a requirement deviation for it — the
    framework restores the point's identity from the ledger.
    """
    from prompts import VERIFICATION_JUDGMENT_SYSTEM_PROMPT as prompt

    rules = [
        paragraph
        for paragraph in prompt.split("\n\n")
        if "INCONCLUSIVE" in paragraph
    ]
    assert rules, "the prompt says nothing about INCONCLUSIVE"
    assert any("requirement_deviations" in rule for rule in rules), rules
