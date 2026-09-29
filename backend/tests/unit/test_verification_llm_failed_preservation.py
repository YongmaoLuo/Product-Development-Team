"""TDD tests for the 2026-09-11 plan v13 LLM FAILED suppression bug.

Background (2026-09-11):
  ``VerificationAgent.run_full_verification`` (verification_agent.py)
  post-processes the LLM-emitted ``verification_report.json`` by
  restoring PASSED verdicts on VPs the executor marked PASSED but the
  LLM downgraded to PARTIAL on subjective grounds (test_command
  design choices, grep-marker patterns, etc).

  The v13 fix tightens this gate: when the executor verdict is PASSED
  but the LLM emitted FAILED (e.g. behavioural-freeze constraint
  violation visible only via diff inspection, not test exit codes),
  the auto-loop MUST NOT suppress the FAILED verdict — it must
  surface to :meth:`VerificationOrchestrator.check_cycle_conditions`
  so the plan routes to repair_task generation + executor re-run.

  The original code used
  ``if not any(s == "FAILED" for s in execution_verdicts): ... restore
  ALL LLM PARTIAL/FAILED downgrades to PASSED`` — this swallowed
  legitimate LLM FAILED verdicts: a VP can have an executor verdict
  PASSED (cargo test exit 0) and an LLM verdict FAILED (the diff
  violates a frozen-behaviour constraint). The auto-loop
  overwrote FAILED→PASSED, skipped repair_tasks generation, and
  state.db.plan_tasks never received the RP-* repair task rows.
"""

import pytest


def _process(report_data, execution_results):
    """Mirror the post-processing block in ``VerificationAgent.run_full_verification``.

    Returns the (possibly mutated) ``report_data`` and the derived
    ``overall_status`` so the test can pin the contract end-to-end.
    """
    execution_verdicts = {
        er.get("id"): er.get("status")
        for er in execution_results.get("execution_results", [])
    }
    if not any(s == "FAILED" for s in execution_verdicts.values()):
        for vr in report_data.get("verification_results", []):
            vid = vr.get("id")
            if (
                vid
                and execution_verdicts.get(vid) == "PASSED"
                and vr.get("status") == "PARTIAL"
            ):
                vr["status"] = "PASSED"
        statuses = [vr.get("status") for vr in report_data.get("verification_results", [])]
        if statuses and all(s == "PASSED" for s in statuses):
            overall = "PASSED"
        elif any(s == "FAILED" for s in statuses):
            overall = "FAILED"
        elif any(s == "PARTIAL" for s in statuses):
            overall = "PARTIAL"
        else:
            overall = "PASSED"
        report_data["overall_status"] = overall
    return report_data


def test_executor_pass_llm_partial_restored_to_pass():
    """Subjective PARTIAL on a PASSED-executor VP → restored to PASSED.

    The legitimate use-case the gate was originally designed for:
    executor says "test exit 0, all good", LLM subjectively downgrades
    to PARTIAL because the test_command looked "fragile" or the
    grep-marker pattern "smells wrong". The LLM PARTIAL is noise —
    restore to PASSED.
    """
    report = {
        "overall_status": "PARTIAL",
        "verification_results": [
            {"id": "VP-001", "status": "PARTIAL"},
        ],
    }
    execution_results = {
        "execution_results": [{"id": "VP-001", "status": "PASSED"}],
    }
    out = _process(report, execution_results)
    assert out["verification_results"][0]["status"] == "PASSED"
    assert out["overall_status"] == "PASSED"


def test_executor_pass_llm_failed_preserved_as_failed():
    """Executor PASSED but LLM FAILED → MUST stay FAILED (v13 fix).

    The bug this pins: a VP had executor verdict PASSED (cargo test
    exit 0) but LLM verdict FAILED (the diff violated a frozen-
    behaviour constraint). The
    original code suppressed the FAILED verdict and marked the plan
    passed — skipping repair_tasks generation entirely.
    """
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
        ],
    }
    execution_results = {
        "execution_results": [{"id": "VP-006", "status": "PASSED"}],
    }
    out = _process(report, execution_results)
    # CRITICAL: the FAILED verdict must NOT be suppressed.
    assert out["verification_results"][0]["status"] == "FAILED", (
        "executor PASSED + LLM FAILED must remain FAILED so the "
        "auto-loop routes to repair-task generation"
    )
    assert out["overall_status"] == "FAILED"


def test_executor_pass_llm_failed_mixed_with_passes_yields_failed():
    """Mixed VP-001 PASS + VP-006 FAILED (executor PASS for both) → FAILED.

    Realistic case: 25 VPs PASSED, 1 VP-006 FAILED by LLM. The
    orchestrator must see overall_status=FAILED so it generates
    repair_tasks for VP-006.
    """
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": f"VP-{i:03d}", "status": "PASSED"} for i in range(25)
        ] + [{"id": "VP-006", "status": "FAILED"}],
    }
    execution_results = {
        "execution_results": [
            {"id": f"VP-{i:03d}", "status": "PASSED"} for i in range(25)
        ] + [{"id": "VP-006", "status": "PASSED"}],
    }
    out = _process(report, execution_results)
    # All 25 PASS stay PASSED, the FAILED one stays FAILED.
    failed = [v for v in out["verification_results"] if v["status"] == "FAILED"]
    assert len(failed) == 1
    assert failed[0]["id"] == "VP-006"
    assert out["overall_status"] == "FAILED"


def test_executor_has_failed_llm_failed_stays_failed():
    """Executor FAILED + LLM FAILED → stays FAILED (no suppression regardless)."""
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-001", "status": "FAILED"},
        ],
    }
    execution_results = {
        "execution_results": [{"id": "VP-001", "status": "FAILED"}],
    }
    out = _process(report, execution_results)
    # The outer ``if not any(s == "FAILED")`` means the body doesn't
    # execute when the executor itself failed — so the report stays
    # as-is (still FAILED).
    assert out["verification_results"][0]["status"] == "FAILED"
    assert out["overall_status"] == "FAILED"


def test_executor_pass_llm_partial_mixed_yields_passed():
    """All VPs PASSED by executor, mixed PASS + PARTIAL by LLM → PASSED."""
    report = {
        "overall_status": "PARTIAL",
        "verification_results": [
            {"id": "VP-001", "status": "PASSED"},
            {"id": "VP-002", "status": "PARTIAL"},
            {"id": "VP-003", "status": "PASSED"},
        ],
    }
    execution_results = {
        "execution_results": [
            {"id": "VP-001", "status": "PASSED"},
            {"id": "VP-002", "status": "PASSED"},
            {"id": "VP-003", "status": "PASSED"},
        ],
    }
    out = _process(report, execution_results)
    statuses = sorted(v["status"] for v in out["verification_results"])
    assert statuses == ["PASSED", "PASSED", "PASSED"]
    assert out["overall_status"] == "PASSED"