"""The question the terminal branch puts to a human.

When the chain's terminal branch cannot repair its way out, it stops
and asks a human how to proceed.  The question has to carry facts the
human can check, not an adjective: which round stopped, why it stopped,
how many repair tasks were produced, and which verification points
failed with which verdict.

This module pins down:

* **The facts are in the question.**  Round number, stop reason,
  repair-task count and every failed point's verdict are all named, so
  the answer does not depend on the human going and reading the report.
* **A point without a verdict reads ``unknown``.**  The report may
  name a point that carries no status at all; that is a fact about the
  report, and the question says so rather than guessing.
* **No failed points reads ``none``.**  An empty list is stated, not
  left as a blank.
* **The freshest entry wins.**  The same point can appear twice — once
  in the top-level results, once in the last round's snapshot — and the
  top-level one is the newer reading, so the question must not quote
  the older snapshot's verdict.

The orchestrator is built with its two heavy collaborators replaced: the
method under test reads the report it is handed and nothing else, so a
real agent or repair generator would only add noise.
"""

from unittest.mock import Mock

import pytest

from verification.orchestrator import (
    TERMINAL_DEFAULT_OPTION,
    TERMINAL_OPTIONS,
    VerificationOrchestrator,
)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """A verification orchestrator with its heavy collaborators stubbed out."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(plan_dir=plan_dir, project_dir=tmp_path)


def test_question_carries_round_stop_reason_verdicts_and_count(orchestrator):
    """The question names the round, the reason, the count and the verdicts."""
    report = {
        "verification_results": [
            {"id": "VP-3", "status": "FAILED"},
            {"id": "VP-4", "status": "FAILED"},
        ],
    }

    payload = orchestrator._terminal_decision_question(
        report, 2, ["VP-3", "VP-4"], 0,
    )

    question = payload["question"]
    assert "round 2" in question
    assert "repair_execution_failed" in question
    assert "0 repair task(s)" in question
    assert "VP-3=FAILED" in question
    assert "VP-4=FAILED" in question
    assert payload["options"] == [
        "rerun_this_round",
        "accept_as_failed",
        "manual_repair_tasks",
    ]
    assert payload["options"] == list(TERMINAL_OPTIONS)
    assert payload["default_option"] == "accept_as_failed"
    assert payload["default_option"] == TERMINAL_DEFAULT_OPTION


def test_missing_verdict_reads_unknown(orchestrator):
    """A point the report gives no verdict for reads ``unknown``."""
    report = {
        "verification_results": [
            {"id": "VP-9"},
        ],
    }

    payload = orchestrator._terminal_decision_question(
        report, 1, ["VP-9"], 0,
    )

    assert "VP-9=unknown" in payload["question"]


def test_no_failed_points_says_none(orchestrator):
    """An empty list of failed points is stated as ``none``."""
    payload = orchestrator._terminal_decision_question({}, 1, [], 0)

    assert "failed checks: none" in payload["question"]


def test_freshest_entry_wins_when_a_point_appears_twice(orchestrator):
    """The top-level result is newer than the last round's snapshot."""
    report = {
        "verification_results": [
            {"id": "VP-3", "status": "FAILED"},
        ],
        "rounds": [
            {"results": [{"id": "VP-3", "status": "PASSED"}]},
        ],
    }

    payload = orchestrator._terminal_decision_question(
        report, 1, ["VP-3"], 0,
    )

    assert "VP-3=FAILED" in payload["question"]
