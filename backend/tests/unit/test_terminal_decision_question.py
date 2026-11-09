"""The question the terminal branch puts to a human.

When the chain's terminal branch cannot repair its way out, it stops
and asks a human how to proceed.  The question has to carry facts the
human can check, not an adjective: which round stopped, why it stopped,
and which verification points failed with which verdict.

This module pins down the **factual** half of the question:

* **The facts are in the question.**  Which round stopped, why it
  stopped and every failed point's verdict are all named, so the answer
  does not depend on the human going and reading the report.
* **A point without a verdict is named without one.**  The report may
  name a point that carries no status at all; the card lists the point
  and claims no verdict for it, rather than inventing one.
* **No failed points is written down.**  An empty list is stated
  (``（无失败点记录）``), not left as a blank.
* **The freshest entry wins.**  The same point can appear twice — once
  in the top-level results, once in the last round's snapshot — and the
  top-level one is the newer reading, so the question must not quote
  the older snapshot's verdict.

And the **analysis** half — the reading of the failure scene, produced
by the judge and rendered as a decision card:

* **With an analysis the question is the card.**  What has to be
  decided, the options on the table, the advice and the path of the
  full report replace the one sentence of facts: a person reading the
  question reads the decision, not a document.
* **Without an analysis the question is the fallback card.**  The judge
  is a model call and a model call may fail; a human must still be
  asked something, and what they are asked is the **same decision
  shape** — the failed points written out by hand instead of by a
  model — with the reason there is no analysis said on the card.
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
    "recommendation": "revise_criterion",
    "recommendation_reason": "重跑不会改变同一失败",
    "uncertainty": "无法判断是否环境问题",
}

#: The card the terminal branch puts to a human when the judge has no
#: analysis to offer — the fallback card, byte for byte.  The archive's
#: path is stamped with the clock, so it is the one part that cannot be
#: a constant: the test fills it in from the payload.
#:
#: The plan id is the fixture's plan directory name (``plan``), and the
#: failed point is the one the report below carries.
_FALLBACK_CARD = (
    "【需要你决定】plan 第 2 轮验证终止：repair_execution_failed\n"
    "\n"
    "失败点：\n"
    "1. VP-3 FAILED\n"
    "选项：\n"
    "1. 重跑本轮验证（rerun_this_round）—— 环境偶发失败，重跑一轮可能就通过\n"
    "2. 修订判据（revise_criterion）—— 判据本身有问题，需要人重写判据\n"
    "建议：（本次没有生成分析，无推荐）\n"
    "完整分析：{report_path}（分析未生成：analysis_empty）"
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
    """The question names the round, the reason and the verdicts.

    The repair-task count is no longer part of the question: the card
    is a decision, not a log of the round, and a terminal round
    produces no repair tasks by construction anyway.
    """
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
    assert "第 2 轮验证终止" in question
    assert "repair_execution_failed" in question
    assert "VP-3 FAILED" in question
    assert "VP-4 FAILED" in question
    assert payload["options"] == [
        "rerun_this_round",
        "revise_criterion",
    ]
    assert payload["options"] == list(TERMINAL_OPTIONS)
    assert payload["default_option"] == "rerun_this_round"
    assert payload["default_option"] == TERMINAL_DEFAULT_OPTION


def test_missing_verdict_lists_the_point_without_inventing_one(orchestrator):
    """A point the report gives no verdict for is named, with no verdict."""
    report = {
        "verification_results": [
            {"id": "VP-9"},
        ],
    }

    payload = orchestrator._terminal_decision_question(
        report, 1, ["VP-9"], 0,
    )

    question = payload["question"]
    assert "VP-9" in question, "the point is not on the card at all"
    assert "VP-9 FAILED" not in question, "a verdict was invented for it"
    assert "VP-9 PASSED" not in question, "a verdict was invented for it"


def test_no_failed_points_says_no_failure_point_recorded(orchestrator):
    """An empty list of failed points is written down, not left blank."""
    payload = orchestrator._terminal_decision_question({}, 1, [], 0)

    assert "（无失败点记录）" in payload["question"]


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

    assert "VP-3 FAILED" in payload["question"]


# ---------------------------------------------------------------------------
# The analysis — the question when there is one
# ---------------------------------------------------------------------------


def test_analysis_success_question_is_the_brief_card(
    analysis_orchestrator,
):
    """With an analysis the question is the card a human answers."""
    payload = analysis_orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    question = payload["question"]
    assert question.startswith("【需要你决定】"), (
        f"the question is not a decision card: {question[:40]!r}"
    )
    assert len(question) <= 600, (
        f"the card is {len(question)} characters, over the cap"
    )
    assert len(question.splitlines()) <= 8, (
        f"the card is {len(question.splitlines())} lines, over the cap"
    )
    assert "VP-3" in question, "the failed point is not on the card"
    assert "rerun_this_round" in question, "the first option is missing"
    assert "revise_criterion" in question, "the second option is missing"
    assert "完整分析：" in question, "the card does not point at the report"


def test_failure_question_is_byte_identical_golden(orchestrator):
    """No analysis — the question is the fallback card, byte for byte.

    The golden is the card the renderer writes for this round: the
    same decision shape as the analysis card, with the failed point
    written out by hand and the reason there is no analysis said on
    the card.  The archive's path is stamped with the clock, so it is
    the one part the golden takes from the payload.
    """
    payload = orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    assert payload["question"] == _FALLBACK_CARD.format(
        report_path=payload["report_path"],
    ), (
        "the fallback card is not the card the renderer writes — the "
        "question a human is asked changed shape"
    )
    assert set(payload) == {
        "question", "options", "default_option", "report_path",
        "analysis_error",
    }, (
        "the fallback payload must still carry the five keys the caller "
        "reads — the decision is created either way"
    )
    assert payload["analysis_error"] == "analysis_empty", (
        "the payload must say why there is no analysis to read"
    )


def test_question_capped_at_600_with_twenty_points(analysis_orchestrator):
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

    assert len(payload["question"]) <= 600


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
    assert payload["default_option"] == "rerun_this_round"
    assert payload["default_option"] == TERMINAL_DEFAULT_OPTION
    assert payload["options"] == list(TERMINAL_OPTIONS)
