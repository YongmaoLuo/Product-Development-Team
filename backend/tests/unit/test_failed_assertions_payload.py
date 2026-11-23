"""R0 — the failed assertions have to reach the report entry as a field.

2026-10-29 (VP-030, root-cause report ``root-cause-VP-030.md``).

The fingerprint is supposed to mean *the failure*.  It can only mean that
while the failure's **payload** is actually recorded — the grader's own
``assertions_failed`` list, which ``verification_api_runner`` already
writes into ``vp_artifacts/<VP>/api_response.json``.

The payload had no producer.  The report entry carried no such field, so
the fingerprint fell back to the prose around it — and prose is
rewritten from one round to the next, which is how "the root cause moved"
came to mean "the report was worded differently".

This file pins the missing half (the producer) and the consequence that
actually matters: **the same payload under rewritten prose is one
failure**.

Adapted to this branch (origin/main).  Two names moved, because main
grew its own machinery in the meantime:

  * the judgment path gets the payload through
    :meth:`Orchestrator._backfill_failure_classes` — main's own backfill,
    which restores the executor's failure class and carries the payload
    with it (the executor is the authority on both);
  * the fallback path (the judgment LLM could not be reached at all) gets
    it through :meth:`Orchestrator._attach_failed_assertions`;
  * the chain's fingerprint view is :meth:`_round_fingerprint_records` —
    main records the fingerprint **and the rule** that produced it, and
    the rule is what makes two fingerprints comparable.

Every assertion below drives production code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.failure_fingerprint import failure_fingerprint  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402


VP_ID = "VP-030"

FAILED_ASSERTIONS = [
    "[状态码] status: expected equals 200, got 500",
    "[detail] body: contains 'capacity'",
]


def _agent(tmp_path) -> Orchestrator:
    """A bare executor orchestrator — only the attributes these paths read."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.plan_dir = tmp_path / "plan"
    orch.project_dir = tmp_path
    orch._last_vp_capacity = None
    return orch


def _chain_orchestrator(tmp_path) -> VerificationOrchestrator:
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_dir = tmp_path / "plan"
    orch.project_dir = tmp_path
    orch._failure_fingerprints = {}
    return orch


def _failed_result() -> dict:
    """The executor's result for a point whose two assertions failed."""
    return {
        "id": VP_ID,
        "status": "FAILED",
        "actual_result": "判据不成立",
        "failure_class": "deliverable_defect",
        "evidence": {
            "status_code": 500,
            "assertions_failed": list(FAILED_ASSERTIONS),
        },
    }


# ---------------------------------------------------------------------------
# The producer: the payload lands in the entry as a field
# ---------------------------------------------------------------------------


def test_the_report_entry_carries_the_failed_assertions(tmp_path):
    """A verdict's failed assertions become the entry's own field."""
    entry = _agent(tmp_path)._project_verdict_to_result(
        VP_ID,
        {
            "status": "FAILED",
            "actual_result": "判据不成立",
            "evidence": {"assertions_failed": list(FAILED_ASSERTIONS)},
        },
    )
    assert entry["failed_assertions"] == FAILED_ASSERTIONS


def test_a_json_encoded_evidence_still_yields_the_failed_assertions(tmp_path):
    """The report's evidence is a *string* — the payload is read out of it."""
    entry = _agent(tmp_path)._project_verdict_to_result(
        VP_ID,
        {
            "status": "FAILED",
            "actual_result": "判据不成立",
            "evidence": json.dumps(
                {"assertions_failed": list(FAILED_ASSERTIONS)},
                ensure_ascii=False,
            ),
        },
    )
    assert entry["failed_assertions"] == FAILED_ASSERTIONS


def test_a_result_without_a_payload_has_none(tmp_path):
    """``None`` means "no payload" — not "nothing failed"."""
    entry = _agent(tmp_path)._project_verdict_to_result(
        VP_ID,
        {"status": "FAILED", "actual_result": "boom", "evidence": {"x": 1}},
    )
    assert entry.get("failed_assertions") is None


def test_the_round_report_carries_the_failed_assertions(tmp_path):
    """The report the round writes names the payload of each failed point."""
    orch = _agent(tmp_path)
    report = orch._generate_minimal_report(
        {"execution_results": [_failed_result()]},
    )
    entry = report["verification_results"][0]
    assert entry["failed_assertions"] == FAILED_ASSERTIONS


def test_the_judgment_report_carries_the_failed_assertions(tmp_path):
    """The judgment LLM writes the prose; the framework writes the payload.

    The LLM is handed a summary and rewords it every round — so the
    payload cannot come from there.  It comes from the executor's result,
    and main's backfill copies it onto the entry the LLM produced.
    """
    orch = _agent(tmp_path)
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "actual_result": "这一轮的措辞和上一轮不一样",
                "evidence": "框架记录：status_code=500，断言两条均为 status / detail 不成立。",
            },
        ],
    }
    orch._backfill_failure_classes(
        report, {"execution_results": [_failed_result()]},
    )
    assert report["verification_results"][0]["failed_assertions"] == sorted(
        FAILED_ASSERTIONS
    )


def test_the_executors_payload_is_what_the_judgment_report_carries(tmp_path):
    """The executor is the authority — its payload replaces the LLM's own.

    Main's rule (2026-11-04) is that the executor's record wins in both
    directions: the report states what the executor said.  The payload
    rides with that authority — it is the executor's own machine-written
    record of what failed.
    """
    orch = _agent(tmp_path)
    report = {
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "actual_result": "x",
                "evidence": "y",
                "failed_assertions": ["[status] expected 404, got 404"],
            },
        ],
    }
    orch._backfill_failure_classes(
        report, {"execution_results": [_failed_result()]},
    )
    assert report["verification_results"][0]["failed_assertions"] == sorted(
        FAILED_ASSERTIONS
    )


def test_an_entry_that_already_carries_a_payload_keeps_its_own(tmp_path):
    """A payload already on the entry stays the entry's own.

    This is the fallback report's rule: the entry is the executor's own
    record, and the framework does not second-guess a payload that was
    recorded.
    """
    orch = _agent(tmp_path)
    report = {
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "actual_result": "x",
                "evidence": "y",
                "failed_assertions": ["[status] expected 404, got 404"],
            },
        ],
    }
    orch._attach_failed_assertions(
        report, {"execution_results": [_failed_result()]},
    )
    assert report["verification_results"][0]["failed_assertions"] == [
        "[status] expected 404, got 404",
    ]


# ---------------------------------------------------------------------------
# The consequence: the same payload under rewritten prose is ONE failure
# ---------------------------------------------------------------------------


def _report_with(actual_result: str, evidence: str, failed=None) -> dict:
    entry = {
        "id": VP_ID,
        "status": "FAILED",
        "actual_result": actual_result,
        "evidence": evidence,
        "failure_class": "deliverable_defect",
    }
    if failed is not None:
        entry["failed_assertions"] = list(failed)
    return {"verification_results": [entry]}


def test_the_same_payload_under_rewritten_prose_is_one_failure(tmp_path):
    """A1: the fingerprint is the payload's — the wording cannot move it."""
    orch = _chain_orchestrator(tmp_path)
    first = _report_with(
        "判据②不成立：status 500",
        "框架记录：status_code=500。",
        failed=FAILED_ASSERTIONS,
    )
    second = _report_with(
        "判据②不成立，status 码为 500，与上轮一致",
        "框架记录：status_code=500（表述已更新）。",
        failed=FAILED_ASSERTIONS,
    )
    assert orch._round_fingerprints(first) == orch._round_fingerprints(second)


def test_a_changed_payload_is_a_different_failure(tmp_path):
    """The payload still decides — it is not ignored in favour of the prose."""
    orch = _chain_orchestrator(tmp_path)
    first = _report_with("x", "y", failed=FAILED_ASSERTIONS)
    second = _report_with(
        "x", "y", failed=["[状态码] status: expected equals 200, got 404"]
    )
    assert (
        orch._round_fingerprints(first)[VP_ID]
        != orch._round_fingerprints(second)[VP_ID]
    )


def test_the_payload_reaches_the_fingerprint_from_the_written_report(tmp_path):
    """The chain reads what the round actually wrote, not what it could read.

    The report is produced by the executor's own path, and the chain's
    fingerprint is computed from that report — so the payload has to
    survive the trip.
    """
    orch = _agent(tmp_path)
    report = orch._generate_minimal_report(
        {"execution_results": [_failed_result()]},
    )
    chain = _chain_orchestrator(tmp_path)
    record = chain._round_fingerprint_records(report)[VP_ID]
    # The payload is the failing set the fingerprint is the identity of.
    assert record["fingerprint"] == failure_fingerprint(
        vp_id=VP_ID,
        failure_class="deliverable_defect",
        failed_cases=FAILED_ASSERTIONS,
    )
