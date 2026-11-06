"""The facts the terminal analysis needs about every failed point.

The analysis prompt asks a model to explain *why* a verification chain
stopped, and it cannot do that from point ids and statuses: it needs to
know what each failed point asked for, how it failed, and how long the
same failure has been repeating.

Those facts live in three places that the analysis has to gather into
one list:

* the **plan file** (``verification_plan.json``) — what the point asked
  for (its title, its expected result);
* the **report** — how it went (the failure class, the verdict text);
* the **same-cause counter** (``_same_failure_counts``) — how many rounds
  in a row this point has failed this very way.

This module pins down:

* **The plan file wins for the criterion.**  The plan is where the
  criterion was written down; the report only knows how the run went.
* **The report is the fallback.**  A missing or unreadable plan file
  falls back to the report's own copy of the fields, and a field
  neither has reads as the empty string — a fact about the record
  rather than a guess.
* **The repeat count comes from the loop's own counter**, keyed by the
  point *and* the failure class, so the analysis can never quote a count
  for a different failure than the one that repeated.
* **The verdict text is the first thing the entry said.**  The report
  spreads the failure's prose over several fields and the order says
  how much each one says about the failure.
* **The freshest entry wins.**  The same point can appear twice — once
  in the top-level results, once in the last round's snapshot — and the
  top-level one is the newer reading.
* **No failed points yields no facts.**  An empty list is empty, not a
  record of nothing.

The orchestrator is built with its two heavy collaborators replaced: the
methods under test read the report and the plan file they are handed
and nothing else, so a real agent or repair generator would only add
noise.
"""

import json
from unittest.mock import Mock

import pytest

from verification.orchestrator import VerificationOrchestrator


@pytest.fixture
def plan_dir(tmp_path):
    """The plan directory the orchestrator reads ``verification_plan.json`` from."""
    path = tmp_path / "plan"
    path.mkdir()
    return path


@pytest.fixture
def orchestrator(plan_dir, tmp_path, monkeypatch):
    """A verification orchestrator with its heavy collaborators stubbed out."""
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(plan_dir=plan_dir, project_dir=tmp_path)


def _write_plan(plan_dir, points):
    """Write the plan file the orchestrator reads the criteria from."""
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": points}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_failed_points_carry_plan_title_and_expected_result(
    orchestrator, plan_dir,
):
    """The plan file says what the point asked for — the report does not win."""
    _write_plan(plan_dir, [
        {
            "id": "VP-3",
            "title": "登录接口",
            "expected_result": "返回 200",
        },
    ])
    report = {
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "title": "报告里的旧标题",
                "expected_result": "报告里的旧期望",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            },
        ],
    }

    points = orchestrator._terminal_failed_points(report, ["VP-3"])

    assert len(points) == 1
    point = points[0]
    assert point["id"] == "VP-3"
    assert point["title"] == "登录接口"
    assert point["expected_result"] == "返回 200"


def test_failed_points_fall_back_to_report_fields(orchestrator):
    """No plan file — the report's own copy of the fields is used."""
    report = {
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

    points = orchestrator._terminal_failed_points(report, ["VP-3"])

    assert len(points) == 1
    assert points[0]["title"] == "登录接口"
    assert points[0]["expected_result"] == "返回 200"


def test_repeat_counts_read_same_failure_counter(orchestrator):
    """The repeat count is the loop's own same-cause count for that failure."""
    report = {
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            },
        ],
    }
    orchestrator._same_failure_counts = {"VP-3|deliverable_defect": 3}

    points = orchestrator._terminal_failed_points(report, ["VP-3"])

    assert points[0]["consecutive_rounds"] == 3


def test_reason_text_prefers_reason_over_evidence(orchestrator):
    """The verdict text is the first thing the entry said."""
    report = {
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
                "evidence": "Traceback: ...",
            },
        ],
    }

    points = orchestrator._terminal_failed_points(report, ["VP-3"])

    assert points[0]["verdict"] == "expected 200, got 500"


def test_freshest_entry_wins(orchestrator):
    """The top-level result is newer than the last round's snapshot."""
    report = {
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            },
        ],
        "rounds": [
            {
                "results": [
                    {
                        "id": "VP-3",
                        "status": "PASSED",
                        "reason": "stale snapshot reading",
                    },
                ],
            },
        ],
    }

    points = orchestrator._terminal_failed_points(report, ["VP-3"])

    assert points[0]["verdict"] == "expected 200, got 500"
    assert points[0]["failure_class"] == "deliverable_defect"


def test_empty_failed_ids_yield_empty_list(orchestrator):
    """No failed points — no facts."""
    report = {
        "verification_results": [
            {"id": "VP-3", "status": "FAILED", "reason": "boom"},
        ],
    }

    assert orchestrator._terminal_failed_points(report, []) == []
