"""The question the terminal branch puts to a human.

When the chain's terminal branch cannot repair its way out, it stops
and asks a human how to proceed.  The question has to carry facts the
human can check, not an adjective: which round stopped, why it stopped,
how many repair tasks were produced, and which verification points
failed with which verdict.

This module pins down the **factual** half of the question:

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

And the **analysis** half — the reading of the failure scene, produced
by the judge and rendered as C/P/E/A/U:

* **With an analysis the question is the analysis.**  The five
  sections and the evidence the judge gave replace the one sentence of
  facts: a person reading the question reads the argument, not a list.
* **Without an analysis the question is the old sentence, byte for
  byte.**  The judge is a model call and a model call may fail; a human
  must still be asked something, and what they are asked is exactly
  what they were asked before the analysis existed.
* **The analysis never moves the default.**  The judge recommends; the
  human decides.  The answer the question falls back to when nobody
  replies does not change because a model had an opinion.
* **The question stays inside its cap.**  Twenty failed points with
  eight-hundred-character verdicts still render a question a person can
  read.

The orchestrator is built with its two heavy collaborators replaced and
its judge faked: the method under test reads the report it is handed and
nothing else, so a real agent, a real repair generator or a real model
call would only add noise.
"""

from unittest.mock import Mock

import pytest

from verification.orchestrator import (
    TERMINAL_DEFAULT_OPTION,
    TERMINAL_OPTIONS,
    VerificationOrchestrator,
)


#: The analysis the fixed judge answers with — one section per part of
#: the contract, and evidence the judge made up.
_ANALYSIS = {
    "context": "第 2 轮终局停止",
    "problem": "修复链没有可用的下一步",
    "evidence": ["VP-3 连续 3 轮失败"],
    "recommendation": "manual_repair_tasks",
    "recommendation_reason": "重跑不会改变同一失败",
    "uncertainty": "无法判断是否环境问题",
}

#: The question the terminal branch asked before the analysis existed —
#: the one the analysis replaces, and the one it falls back to.
_FACTUAL_QUESTION = (
    "verification round 2 stopped with repair_execution_failed: "
    "0 repair task(s) were generated; failed checks: VP-3=FAILED. "
    "Choose how to proceed."
)


class _FakeJudge:
    """A judge that answers a fixed thing, whatever it is asked."""

    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def _build_orchestrator(tmp_path, monkeypatch, judge):
    """An orchestrator with its heavy collaborators and its judge replaced."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(
        plan_dir=plan_dir,
        project_dir=tmp_path,
        coding_tool=Mock(),
        terminal_judge=judge,
    )


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """An orchestrator whose judge has no analysis to offer."""
    return _build_orchestrator(tmp_path, monkeypatch, _FakeJudge(None))


@pytest.fixture
def analysis_orchestrator(tmp_path, monkeypatch):
    """An orchestrator whose judge answers with a fixed analysis."""
    return _build_orchestrator(tmp_path, monkeypatch, _FakeJudge(dict(_ANALYSIS)))


def _report(*vp_ids):
    """A report whose points all failed, as the terminal branch sees it."""
    return {
        "verification_results": [
            {"id": vp_id, "status": "FAILED"} for vp_id in vp_ids
        ],
    }


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


# ---------------------------------------------------------------------------
# The analysis — the question when there is one
# ---------------------------------------------------------------------------


def test_analysis_success_question_contains_cpea_and_evidence(
    analysis_orchestrator,
):
    """With an analysis the question carries the five sections and the evidence."""
    payload = analysis_orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    question = payload["question"]
    assert question.startswith("## 背景（C）")
    for heading in (
        "## 问题（P）", "## 证据（E）", "## 建议（A）", "## 不确定（U）",
    ):
        assert heading in question, f"{heading} is missing from the question"
    assert "VP-3 连续 3 轮失败" in question, "the judge's evidence was dropped"


def test_failure_question_is_byte_identical_golden(orchestrator):
    """No analysis — the question is the old sentence, byte for byte."""
    payload = orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    assert payload["question"] == _FACTUAL_QUESTION
    assert set(payload) == {"question", "options", "default_option"}, (
        "the fallback payload must still carry the three keys the caller "
        "reads — the decision is created either way"
    )


def test_question_capped_at_2000_with_twenty_points(analysis_orchestrator):
    """Twenty long verdicts still render a question inside the cap."""
    failed_ids = [f"VP-{index}" for index in range(20)]
    analysis_orchestrator._terminal_judge.result = {
        **_ANALYSIS,
        "evidence": [
            f"{vp_id}: " + "判词" * 400 for vp_id in failed_ids
        ],
    }

    payload = analysis_orchestrator._terminal_decision_question(
        _report(*failed_ids), 2, failed_ids, 0,
    )

    assert len(payload["question"]) <= 2000


def test_recommendation_never_changes_default_option(analysis_orchestrator):
    """The recommendation is written in the body; the default does not move."""
    analysis_orchestrator._terminal_judge.result = {
        **_ANALYSIS,
        "recommendation": "rerun_this_round",
    }

    payload = analysis_orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    assert "rerun_this_round" in payload["question"]
    assert payload["default_option"] == "accept_as_failed"
    assert payload["default_option"] == TERMINAL_DEFAULT_OPTION
    assert payload["options"] == list(TERMINAL_OPTIONS)
