"""The executor owns the verdict of an ``api_test`` verification point.

2026-11-12 (VP-014 — arch decision point 6 / test decision point 11)
--------------------------------------------------------------------
An ``api_test`` point is graded by the framework, deterministically:
the request goes out, **every declared assertion is evaluated**, and
the verdict is the assertions' verdict.  The Phase 3 judgment LLM never
sees that — it sees a summary of it — and it may write the point down
as FAILED even though every assertion passed.

That is exactly what happened to VP-014 of the plan this fix came
from.  The executor sent the request, evaluated the five assertions,
and recorded PASSED with ``assertions_failed`` empty and
``assertions_passed`` non-empty; the round log's ``api_test_executed``
event said PASSED.  The report nevertheless said **FAILED** — and its
own ``actual_result`` described the successful run ("5 条断言全部通过、
assertions_failed 为空"), so the artifact contradicted itself.  A report
that contradicts its own evidence is not a verdict, and PRD 验收④ asks
for a *trustworthy* judgment.

The behaviour those five assertions graded is pinned at the API level,
in ``backend/tests/api/test_verification_plan_structure_errors.py``.

The rule implemented here is deliberately narrow:

  * only ``api_test`` points — the framework graded them, so the
    framework's verdict is the one about the assertions;
  * the executor's verdict must be ``PASSED``;
  * the evidence must show that assertions **were** evaluated and none
    of them failed (``assertions_passed`` non-empty,
    ``assertions_failed`` empty).

``code_review`` points keep the judgment LLM's FAILED verdict: that
method reads the diff, which the executor never sees, so the LLM may
know something the executor does not.  That is the negative control
these tests exist for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ReportingTool:
    """A ``coding_tool`` stand-in whose judgment answers with a report."""

    def __init__(self, report: dict):
        self.report = report
        self.calls = 0

    def query_json(self, *args, **kwargs):
        self.calls += 1
        return json.loads(json.dumps(self.report))


def _make_agent(tmp_path: Path, tool=None):
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=tool,
    )


def _point(vp_id: str = "VP-014", method: str = "api_test") -> dict:
    """The plan's own definition of a verification point."""
    return {
        "id": vp_id,
        "title": "畸形计划书：结构非法是可见的结构违规，不是接口错误",
        "verification_method": method,
    }


def _executor_result(
    vp_id: str = "VP-014",
    status: str = "PASSED",
    *,
    passed=("[状态码] status: expected equals 200, got 200",),
    failed=(),
) -> dict:
    """The executor's verdict for an ``api_test`` point.

    The evidence is a JSON **string**, which is the shape the Phase 2
    envelope actually carries (``_enrich_evidence_with_address``
    flattens the verdict's evidence dict into a string).
    """
    evidence = {
        "vp_id": vp_id,
        "status_code": 200,
        "assertions_passed": list(passed),
        "assertions_failed": list(failed),
    }
    return {
        "id": vp_id,
        "status": status,
        "actual_result": "全部断言通过",
        "evidence": json.dumps(evidence, ensure_ascii=False),
    }


def _llm_report(entries: list) -> dict:
    """A judgment report that writes the statuses down as given."""
    return {
        "overall_status": (
            "FAILED" if any(e.get("status") == "FAILED" for e in entries)
            else "PASSED"
        ),
        "summary": "the judgment LLM's own words",
        "verification_results": entries,
        "requirement_deviations": [],
    }


def _entry(report: dict, vp_id: str) -> dict:
    for entry in report.get("verification_results", []):
        if isinstance(entry, dict) and str(entry.get("id")) == vp_id:
            return entry
    raise AssertionError(
        f"the report has no entry for {vp_id}: "
        f"{report.get('verification_results')!r}"
    )


# ---------------------------------------------------------------------------
# 1. The case that failed: the executor's green, the LLM's red.
# ---------------------------------------------------------------------------


def test_api_test_passed_by_the_executor_is_not_rewritten_as_failed(tmp_path):
    """Every assertion passed → the point is PASSED, whatever the LLM wrote.

    The VP-014 shape: the executor evaluated five assertions, all five
    passed (``assertions_failed`` empty, ``assertions_passed``
    non-empty), and the judgment LLM wrote the point down as FAILED.
    """
    tool = _ReportingTool(_llm_report([
        {
            "id": "VP-014",
            "status": "FAILED",
            "actual_result": "5 条断言全部通过、assertions_failed 为空",
            "evidence": "5 条断言全部通过",
        },
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point()],
        "execution_results": [_executor_result()],
    })

    entry = _entry(report, "VP-014")
    assert entry["status"] == "PASSED", (
        "the executor evaluated every assertion and none of them failed; "
        f"the judgment must not invent a failure; got {entry['status']!r}"
    )


def test_the_round_verdict_follows_the_restored_entry(tmp_path):
    """The round's verdict is derived from the entries — so it is green too.

    A report whose entries are all green but whose ``overall_status``
    says FAILED is the same self-contradiction one layer up.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-014", "status": "FAILED", "actual_result": "…"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point()],
        "execution_results": [_executor_result()],
    })

    assert report["overall_status"] == "PASSED", (
        "one green point and no other entry must not leave the round red; "
        f"got {report['overall_status']!r}"
    )


# ---------------------------------------------------------------------------
# 2. The negative controls: what the rule must NOT touch.
# ---------------------------------------------------------------------------


def test_code_review_failed_by_the_llm_stays_failed(tmp_path):
    """``code_review`` keeps the LLM's FAILED — it reads the diff.

    The executor never sees the diff, so the judgment LLM may know
    something the executor does not.  Restoring its verdict would throw
    away exactly the finding the method exists to produce.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-014", "status": "FAILED", "actual_result": "diff 违反冻结约束"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point(method="code_review")],
        "execution_results": [_executor_result()],
    })

    entry = _entry(report, "VP-014")
    assert entry["status"] == "FAILED", (
        "a code_review point the judgment LLM failed must stay failed; "
        f"got {entry['status']!r}"
    )
    assert report["overall_status"] == "FAILED", (
        f"the round must stay red; got {report['overall_status']!r}"
    )


def test_api_test_failed_by_the_executor_stays_failed(tmp_path):
    """The executor's own FAILED is never rewritten.

    The executor says the contract broke; the judgment LLM says the
    same thing.  There is nothing to restore — and a point whose
    assertions failed must never be laundered into a pass.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-014", "status": "FAILED", "actual_result": "500 != 200"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point()],
        "execution_results": [
            _executor_result(
                status="FAILED",
                passed=(),
                failed=("[状态码] status: expected equals 200, got 500",),
            ),
        ],
    })

    entry = _entry(report, "VP-014")
    assert entry["status"] == "FAILED", (
        f"a broken contract must stay broken; got {entry['status']!r}"
    )
    assert report["overall_status"] == "FAILED", (
        f"the round must stay red; got {report['overall_status']!r}"
    )


def test_api_test_without_evaluated_assertions_is_not_restored(tmp_path):
    """No assertion was evaluated → the executor cannot have graded it.

    The evidence carries no ``assertions_passed``: nothing ran, so the
    executor's PASSED says nothing about the assertions.  Restoring on
    that basis would be a guess.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-014", "status": "FAILED", "actual_result": "…"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point()],
        "execution_results": [
            {
                "id": "VP-014",
                "status": "PASSED",
                "actual_result": "…",
                "evidence": json.dumps(
                    {"vp_id": "VP-014", "assertions_executed": 0},
                    ensure_ascii=False,
                ),
            },
        ],
    })

    entry = _entry(report, "VP-014")
    assert entry["status"] == "FAILED", (
        "without an evaluated assertion there is nothing to restore from; "
        f"got {entry['status']!r}"
    )


def test_a_failed_assertion_is_never_restored(tmp_path):
    """A failed assertion in the evidence blocks the restoration.

    The contradictory record (executor PASSED, one assertion failed)
    must not be resolved in favour of the pass: the evidence says the
    contract broke, and that is the claim the reader has to see.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-014", "status": "FAILED", "actual_result": "…"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point()],
        "execution_results": [
            _executor_result(failed=("[正文仍可读] json_path $.content: missing",)),
        ],
    })

    entry = _entry(report, "VP-014")
    assert entry["status"] == "FAILED", (
        "an evidence that names a failed assertion must not be restored; "
        f"got {entry['status']!r}"
    )


def test_a_point_the_executor_never_judged_is_not_restored(tmp_path):
    """No executor verdict → nothing to restore from.

    The report keeps the judgment LLM's verdict for a point the
    executor has no record of — the backfill fills in what the
    executor said, it does not invent a verdict.
    """
    tool = _ReportingTool(_llm_report([
        {"id": "VP-014", "status": "FAILED", "actual_result": "…"},
    ]))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point()],
        "execution_results": [],
    })

    entry = _entry(report, "VP-014")
    assert entry["status"] == "FAILED", (
        f"a point the executor never judged must keep the LLM's verdict; "
        f"got {entry['status']!r}"
    )


# ---------------------------------------------------------------------------
# 3. The semantics that must NOT move.
# ---------------------------------------------------------------------------


def test_the_partial_downgrade_gate_is_unchanged(tmp_path):
    """PARTIAL is still restored only when the executor was all green.

    The pre-existing rule ("restore the LLM's subjective PARTIAL only
    when the executor had no FAILED at all") is untouched by this fix:
    here the executor failed another point, so the LLM's PARTIAL on the
    api_test point stands.
    """
    tool = _ReportingTool({
        "overall_status": "FAILED",
        "summary": "…",
        "verification_results": [
            {"id": "VP-014", "status": "PARTIAL", "actual_result": "…"},
            {"id": "VP-020", "status": "FAILED", "actual_result": "…"},
        ],
        "requirement_deviations": [],
    })
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [_point(), _point(vp_id="VP-020")],
        "execution_results": [
            _executor_result(),
            _executor_result(
                vp_id="VP-020",
                status="FAILED",
                passed=(),
                failed=("[status] expected equals 200, got 500",),
            ),
        ],
    })

    assert _entry(report, "VP-014")["status"] == "PARTIAL", (
        "the PARTIAL downgrade gate is unchanged: the executor failed a "
        "point, so the LLM's PARTIAL stands; got "
        f"{_entry(report, 'VP-014')['status']!r}"
    )


# ---------------------------------------------------------------------------
# 4. The behaviour the judgment is about (PRD 验收④)
# ---------------------------------------------------------------------------
#
# The judgment layer above only means something if the behaviour it
# judges is itself pinned: a plan document that breaks the ten-section
# contract must come back **200**, with its **content verbatim**, and
# **every visible violation listed**.
#
# Those live at the API level, where the behaviour is observable:
# ``backend/tests/api/test_verification_plan_structure_errors.py`` holds
# them — one test per assertion VP-014's api_test evaluated.  They are
# not duplicated here: one copy of a test is one thing to maintain.
