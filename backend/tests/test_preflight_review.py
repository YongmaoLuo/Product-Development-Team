"""
TDD tests for ``backend.preflight_review`` — PreFlightReviewer runs an
LLM-assisted cross-document consistency check **before** tasks.json
generation.

Background
----------
DP2 (Design Principle 2) requires that the PRD / architecture / test
design documents are consistent BEFORE tasks are generated. Without
this gate, a PRD acceptance criterion with no corresponding
architecture component will silently drop out of the resulting
``tasks.json`` and only surface during verification — by which
point the user has spent hours and tokens on downstream tasks.

The class lives at ``backend/preflight_review.py`` and exports:

  * ``PreFlightReviewer(coding_tool, plans_root="plans")``
  * ``reviewer.run(plan_id) -> dict`` returning a
    ``PreFlightReport`` with ``findings``/``high_count``/``passed``.
  * Helpers ``_load_docs``, ``_build_alignment_prompt``,
    ``_parse_report`` (private but exercised through ``run``).

Edge cases pinned by the spec:

  * arch / test docs absent → only the PRD-internal dimension runs;
    the LLM is still asked, but ``findings`` are typically all
    severity=high for missing coverage.
  * LLM failure (exception) → graceful fallback to
    ``passed=True`` with empty findings so tasks generation is
    never blocked by a flaky reviewer.

TDD spec
--------
1. ``test_preflight_detects_missing_arch_coverage``:
   When a PRD acceptance item ``AC-001`` has no corresponding
   architecture component, the LLM returns a finding with
   ``severity="high"``, ``source_doc="prd"``, ``source_id`` carrying
   the acceptance id, and ``target_doc="arch"``. ``PreFlightReviewer.run``
   returns ``high_count >= 1`` and ``passed=False`` for that case.

2. ``test_preflight_detects_missing_test_scenario``:
   When an arch module has no corresponding test scenario, the LLM
   returns a finding with ``severity="high"`` and
   ``target_doc="test"``. ``PreFlightReviewer.run`` reports the
   high-severity finding and ``passed=False``.

3. ``test_preflight_passes_on_aligned_docs``:
   When the LLM reports ``{"findings": []}`` (all three documents
   are aligned), ``PreFlightReviewer.run`` returns ``passed=True``
   with ``high_count == 0`` and an empty findings list.

4. ``test_preflight_fallback_on_llm_failure``:
   When ``coding_tool.query_json`` raises any exception, the
   reviewer MUST NOT propagate it; it returns
   ``{"findings": [], "high_count": 0, "passed": True}`` (degraded
   but non-blocking). This is the safety-net contract that the
   task generator relies on.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_coding_tool(canned_response):
    """Build a CodingTool-like MagicMock whose ``query_json`` returns the
    canned dict. Also stubs ``query`` so any incidental caller doesn't
    trip a MissingMock error.
    """
    tool = MagicMock()
    tool.query_json.return_value = canned_response
    tool.query.return_value = json.dumps(canned_response, ensure_ascii=False)
    return tool


def _write_prd(plan_dir: Path, acceptance_ids=("AC-001", "AC-002")) -> None:
    prd = {
        "title": "Test PRD",
        "overview": "Cross-doc consistency test fixture.",
        "acceptance": list(acceptance_ids),
        "decision_points": [],
    }
    (plan_dir / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_arch(plan_dir: Path, body: str) -> None:
    (plan_dir / "arch-design.md").write_text(body, encoding="utf-8")


def _write_test_design(plan_dir: Path, body: str) -> None:
    (plan_dir / "test-design.md").write_text(body, encoding="utf-8")


def test_preflight_detects_missing_arch_coverage(tmp_path):
    """PRD acceptance has no arch correspondence → finding severity=high.

    The PRD defines ``AC-001`` and ``AC-002`` as acceptance criteria,
    but the arch design has NO components that implement them. The
    reviewer must surface a high-severity finding for the missing
    arch coverage.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    # Arch intentionally has nothing that mentions AC-001 / AC-002.
    _write_prd(plan_dir, acceptance_ids=["AC-001: must do X", "AC-002: must do Y"])
    _write_arch(
        plan_dir,
        "# Architecture\n\n"
        "## Modules\n"
        "- LoggerModule: wraps stdlib logging\n",
    )
    _write_test_design(plan_dir, "# Test Design\n\n## Scenarios\n- T-001: smoke\n")

    canned_response = {
        "findings": [
            {
                "source_doc": "prd",
                "source_id": "AC-001",
                "target_doc": "arch",
                "target_id": None,
                "severity": "high",
                "finding": "PRD acceptance AC-001 has no corresponding arch component.",
                "suggested_fix": "Add an arch module that implements AC-001.",
            },
            {
                "source_doc": "prd",
                "source_id": "AC-002",
                "target_doc": "arch",
                "target_id": None,
                "severity": "high",
                "finding": "PRD acceptance AC-002 has no corresponding arch component.",
                "suggested_fix": "Add an arch module that implements AC-002.",
            },
        ]
    }
    coding_tool = _make_coding_tool(canned_response)

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    # The reviewer must surface the high-severity findings as-is.
    assert report["high_count"] >= 1, (
        f"expected high_count >= 1, got {report.get('high_count')}; "
        f"findings={report.get('findings')}"
    )
    assert report["passed"] is False, (
        f"expected passed=False when arch coverage missing; got {report.get('passed')}"
    )

    # The findings list must contain at least one high-severity entry
    # whose target_doc is "arch" (i.e. PRD AC → arch gap).
    arch_findings = [
        f for f in report["findings"]
        if f["severity"] == "high" and f["target_doc"] == "arch"
    ]
    assert arch_findings, (
        f"expected at least one high-severity finding targeting 'arch'; "
        f"got findings={report['findings']}"
    )

    # The reviewer should have called query_json exactly once.
    assert coding_tool.query_json.call_count == 1

    # The persisted artifact must exist on disk.
    artifact = plan_dir / "preflight_report.json"
    assert artifact.exists(), f"missing artifact: {artifact}"


def test_preflight_detects_missing_test_scenario(tmp_path):
    """Arch module has no test correspondence → finding severity=high.

    The arch design defines ``PaymentModule`` but the test design
    has no scenario that exercises it. The reviewer must surface a
    high-severity finding for the missing test coverage.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["AC-100: payment flow works"])
    _write_arch(
        plan_dir,
        "# Architecture\n\n"
        "## Modules\n"
        "- PaymentModule: handles payment processing\n"
        "- UserModule: handles user accounts\n",
    )
    # Test design deliberately omits any payment scenario.
    _write_test_design(
        plan_dir,
        "# Test Design\n\n## Scenarios\n- T-USER-1: user login smoke test\n",
    )

    canned_response = {
        "findings": [
            {
                "source_doc": "arch",
                "source_id": "PaymentModule",
                "target_doc": "test",
                "target_id": None,
                "severity": "high",
                "finding": "Arch module PaymentModule has no corresponding test scenario.",
                "suggested_fix": "Add a test scenario that exercises PaymentModule end-to-end.",
            },
        ]
    }
    coding_tool = _make_coding_tool(canned_response)

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    assert report["high_count"] >= 1
    assert report["passed"] is False

    test_findings = [
        f for f in report["findings"]
        if f["severity"] == "high" and f["target_doc"] == "test"
    ]
    assert test_findings, (
        f"expected at least one high-severity finding targeting 'test'; "
        f"got findings={report['findings']}"
    )

    artifact = plan_dir / "preflight_report.json"
    assert artifact.exists(), f"missing artifact: {artifact}"


def test_preflight_passes_on_aligned_docs(tmp_path):
    """Three docs aligned → passed=True, high_count=0, findings=[].

    When the LLM reports no findings (every PRD acceptance has a
    matching arch component, every arch module has a matching test
    scenario, etc.), the reviewer must surface a clean report with
    ``passed=True`` and ``high_count == 0``.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["AC-200: aligned feature"])
    _write_arch(
        plan_dir,
        "# Architecture\n\n"
        "## Modules\n"
        "- AlignedModule: implements AC-200\n",
    )
    _write_test_design(
        plan_dir,
        "# Test Design\n\n## Scenarios\n- T-ALIGNED-1: covers AC-200\n",
    )

    # LLM returns an empty findings array — the aligned case.
    canned_response = {"findings": []}
    coding_tool = _make_coding_tool(canned_response)

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    assert report["passed"] is True, (
        f"expected passed=True when LLM reports no findings; got {report}"
    )
    assert report["high_count"] == 0
    assert report["findings"] == []

    # Persistence contract — the report is still written to disk.
    artifact = plan_dir / "preflight_report.json"
    assert artifact.exists(), f"missing artifact: {artifact}"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["passed"] is True
    assert persisted["high_count"] == 0


def test_preflight_fallback_on_llm_failure(tmp_path):
    """LLM raises → passed=True, findings=[], no crash.

    The preflight reviewer is a safety net — it must NEVER block
    tasks generation. When ``coding_tool.query_json`` raises any
    exception, the reviewer degrades to a clean passed=True report
    with empty findings. This is the contract that the task
    generator relies on: a flaky reviewer can never poison the
    downstream pipeline.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["AC-300: degraded path"])
    _write_arch(plan_dir, "# Architecture\n\n## Modules\n- X\n")
    _write_test_design(plan_dir, "# Test Design\n\n## Scenarios\n- T\n")

    coding_tool = MagicMock()
    # query_json raises — typical real-world LLM outage signature.
    coding_tool.query_json.side_effect = RuntimeError("LLM service unavailable")

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")

    # Must NOT raise — the test would fail with a Traceback.
    report = reviewer.run("test-plan")

    # Degraded-but-non-blocking contract:
    assert report["passed"] is True, (
        f"expected passed=True on LLM failure (degraded fallback); got {report}"
    )
    assert report["high_count"] == 0
    assert report["findings"] == []

    # query_json was invoked exactly once (the single attempt before
    # the graceful fallback).
    assert coding_tool.query_json.call_count == 1

    # The persisted artifact must still exist on disk — the
    # fallback path is a real report, not a silent no-op.
    artifact = plan_dir / "preflight_report.json"
    assert artifact.exists(), f"missing artifact: {artifact}"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["passed"] is True
    assert persisted["high_count"] == 0