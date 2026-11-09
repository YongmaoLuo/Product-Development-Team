"""The terminal question when the judge produced **no analysis**.

The decision card (:func:`verification.terminal_analysis.render_brief`)
is rendered from an *analysis*.  When the judge produced none — the
model call raised, the answer was not JSON, the answer broke the
contract — there is nothing to render, and the human used to be asked a
bare sentence of facts::

    verification round 2 stopped with repair_execution_failed: 0 repair
    task(s) were generated; failed checks: VP-3=FAILED. Choose how to
    proceed.

That sentence names **no failure point**: it says a check failed and
nothing else.  The reader cannot tell *which* check, what it was
checking, or what came back — and the silence is silent: the question
does not say that the analysis is missing, so a reader cannot tell
"nothing is known" from "the analysis is on its way".

What is pinned here
-------------------

1. **Without an analysis the question is the fallback card.**  The
   card names every failed point — its id, its status, its title and
   the first sentence of what actually came back — so the reader can
   act without going to read the report.
2. **The card stays inside its caps.**  Twelve lines and nine hundred
   characters, however many points failed; the points that do not fit
   are **counted** on the card rather than dropped silently.
3. **The payload carries why there is no analysis.**  ``analysis_empty``
   and ``analysis_rejected:<reason>`` ask a human different questions,
   and a card that does not say which one it is answers neither.
4. **A missing title or a missing actual result leaves its own part
   empty** — the rest of the line is still worth reading, and nothing
   raises.

The judge is a fake: the real one is a model call, and what is pinned
here is the wiring, not the model.  The fake is built **here** rather
than imported, so this module does not depend on the modules whose
no-analysis assertions the fallback card replaces.
"""

from unittest.mock import Mock

import pytest

from verification.orchestrator import VerificationOrchestrator


#: The reason a judge gives when it has no answer at all.
_ANALYSIS_EMPTY = "analysis_empty"

#: The card's caps — the ones the fallback renderer keeps to.
_MAX_LINES = 12
_MAX_CHARS = 900


class _FakeJudge:
    """A judge that answers a fixed thing, whatever it is asked.

    It keeps no reason of its own unless it is given one, so the
    orchestrator's own vocabulary for "no analysis" is what the
    question has to carry.
    """

    def __init__(self, result, analysis_error=None):
        self.result = result
        self.kwargs = None
        if analysis_error is not None:
            self.last_analysis_error = analysis_error

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
def no_analysis_orchestrator(tmp_path, monkeypatch):
    """An orchestrator whose judge has no analysis to offer."""
    return _build_orchestrator(tmp_path, monkeypatch, _FakeJudge(None))


@pytest.fixture
def broken_analysis_orchestrator(tmp_path, monkeypatch):
    """An orchestrator whose judge answers something that is not an analysis."""
    return _build_orchestrator(
        tmp_path, monkeypatch, _FakeJudge({"recommendation": "not_an_option"}),
    )


def _report(*points):
    """A report whose points all failed, as the terminal branch sees it."""
    return {"verification_results": list(points)}


def _failed_point(vp_id, title="登录接口", actual_result="HTTP 500。堆栈未捕获"):
    """One failed point, as the report records it."""
    return {
        "id": vp_id,
        "status": "FAILED",
        "title": title,
        "actual_result": actual_result,
    }


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


def test_no_analysis_question_is_the_fallback_card(no_analysis_orchestrator):
    """No analysis — the question names the point that failed, not "none"."""
    report = _report(_failed_point("VP-3"))

    payload = no_analysis_orchestrator._terminal_decision_question(
        report, 2, ["VP-3"], 0,
    )

    question = payload["question"]
    assert question.startswith("【需要你决定】"), (
        f"the question is not a decision card: {question[:60]!r}"
    )
    assert "VP-3 FAILED" in question, (
        f"the failed point is not named on the card:\n{question}"
    )
    assert "—— 登录接口" in question, (
        f"the point's title is not on the card:\n{question}"
    )
    assert "HTTP 500" in question, (
        f"the point's actual result is not on the card:\n{question}"
    )
    assert "failed checks:" not in question, (
        f"the card still carries the old factual sentence:\n{question}"
    )


def test_four_failures_stay_within_the_caps(no_analysis_orchestrator):
    """Four failed points: three are named, the fourth is counted."""
    points = [_failed_point(f"VP-{index}") for index in range(1, 5)]
    report = _report(*points)

    payload = no_analysis_orchestrator._terminal_decision_question(
        report, 2, [f"VP-{index}" for index in range(1, 5)], 0,
    )

    question = payload["question"]
    assert len(question.splitlines()) <= _MAX_LINES, (
        f"the card is {len(question.splitlines())} lines, over the cap:\n"
        f"{question}"
    )
    assert len(question) <= _MAX_CHARS, (
        f"the card is {len(question)} characters, over the cap:\n{question}"
    )
    assert "另有 1 个失败点" in question, (
        f"the card does not count the point it left out:\n{question}"
    )


def test_ten_failures_stay_within_the_caps(no_analysis_orchestrator):
    """Ten failed points: the card still fits, and counts the rest."""
    ids = [f"VP-{index}" for index in range(1, 11)]
    report = _report(*[_failed_point(vp_id) for vp_id in ids])

    payload = no_analysis_orchestrator._terminal_decision_question(
        report, 3, ids, 0,
    )

    question = payload["question"]
    assert len(question.splitlines()) <= _MAX_LINES, (
        f"the card is {len(question.splitlines())} lines, over the cap:\n"
        f"{question}"
    )
    assert len(question) <= _MAX_CHARS, (
        f"the card is {len(question)} characters, over the cap:\n{question}"
    )
    assert "另有 7 个失败点" in question, (
        f"the card does not count the points it left out:\n{question}"
    )


def test_no_failed_points_says_so(no_analysis_orchestrator):
    """No failed point at all is said, not left blank."""
    payload = no_analysis_orchestrator._terminal_decision_question(
        {}, 1, [], 0,
    )

    question = payload["question"]
    assert "（无失败点记录）" in question, (
        f"the card does not say that no failed point was recorded:\n{question}"
    )
    assert len(question.splitlines()) <= _MAX_LINES, (
        f"the card is over the line cap:\n{question}"
    )


def test_missing_title_and_result_leave_their_parts_empty(
    no_analysis_orchestrator,
):
    """A point with no title and no result still renders, and raises nothing."""
    report = _report({"id": "VP-7", "status": "FAILED"})

    payload = no_analysis_orchestrator._terminal_decision_question(
        report, 1, ["VP-7"], 0,
    )

    question = payload["question"]
    point_lines = [
        line for line in question.splitlines() if line.startswith("1. ")
    ]
    assert point_lines, (
        f"the failed point is not named on the card:\n{question}"
    )
    assert point_lines[0] == "1. VP-7 FAILED", (
        f"the card wrote a title or a result that is not there:\n{question}"
    )


# ---------------------------------------------------------------------------
# The reason
# ---------------------------------------------------------------------------


def test_payload_carries_the_analysis_error(no_analysis_orchestrator):
    """The payload says why there is no analysis."""
    payload = no_analysis_orchestrator._terminal_decision_question(
        _report(_failed_point("VP-3")), 2, ["VP-3"], 0,
    )

    assert payload["analysis_error"] == _ANALYSIS_EMPTY, (
        f"the payload does not say the analysis is empty: "
        f"{payload['analysis_error']!r}"
    )
    assert _ANALYSIS_EMPTY in payload["question"], (
        f"the card does not say why there is no analysis:\n"
        f"{payload['question']}"
    )


def test_the_judges_own_reason_is_carried_over(tmp_path, monkeypatch):
    """The reason the judge gives is the reason the payload carries."""
    orchestrator = _build_orchestrator(
        tmp_path, monkeypatch,
        _FakeJudge(None, analysis_error="analysis_empty"),
    )

    payload = orchestrator._terminal_decision_question(
        _report(_failed_point("VP-3")), 2, ["VP-3"], 0,
    )

    assert payload["analysis_error"] == "analysis_empty", (
        f"the judge's reason was not carried over: "
        f"{payload['analysis_error']!r}"
    )


def test_invalid_mapping_analysis_reports_a_rejection(
    broken_analysis_orchestrator,
):
    """An answer that is not an analysis is a rejection, and says so."""
    payload = broken_analysis_orchestrator._terminal_decision_question(
        _report(_failed_point("VP-3")), 2, ["VP-3"], 0,
    )

    analysis_error = payload["analysis_error"]
    assert analysis_error.startswith("analysis_rejected:"), (
        f"the payload does not name a rejection: {analysis_error!r}"
    )
    assert analysis_error != "analysis_rejected:", (
        "the rejection does not say which check refused the answer"
    )
    assert "VP-3 FAILED" in payload["question"], (
        f"the card does not name the failed point:\n{payload['question']}"
    )


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------


def test_an_analysis_is_still_the_analysis_card(tmp_path, monkeypatch):
    """With an analysis the fallback card does not take over."""
    analysis = {
        "context": "第 2 轮终局停止",
        "problem": "修复链没有可用的下一步",
        "evidence": ["VP-3 连续 3 轮失败"],
        "recommendation": "revise_criterion",
        "recommendation_reason": "重跑不会改变同一失败",
        "uncertainty": "无法判断是否环境问题",
    }
    orchestrator = _build_orchestrator(
        tmp_path, monkeypatch, _FakeJudge(dict(analysis)),
    )

    payload = orchestrator._terminal_decision_question(
        _report(_failed_point("VP-3")), 2, ["VP-3"], 0,
    )

    question = payload["question"]
    assert "建议" not in question or "无推荐" not in question, (
        f"the fallback card took over from the analysis card:\n{question}"
    )
    assert payload["analysis_error"] == "", (
        f"a round with an analysis reports an error: "
        f"{payload['analysis_error']!r}"
    )
    assert len(question) <= 600, (
        f"the card is {len(question)} characters, over the analysis cap"
    )
