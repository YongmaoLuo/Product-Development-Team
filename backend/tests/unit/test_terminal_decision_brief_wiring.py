"""The terminal question is the **decision card**, and the card knows the report.

Three things are pinned here, and they are the three things the human on the
other end of the decision actually sees:

* **The question is a card, not a report.**  When the judge has read the
  failure scene, the question is the short card — what has to be
  decided, the options, the advice and where the full analysis lives.
  A three-kilobyte report is not a question: it is a document, and a
  person asked to answer a document does not.
* **The card carries the path of the archive.**  The card is put to the
  human *before* the round's archive is written, so the path cannot be
  read back from the write — it is computed from the same round number
  and the same clock reading the write will use.  A card that says
  "see the full analysis" without saying where is a card that sends
  the reader looking.
* **Without an analysis the question is still a card.**  The judge is a
  model call and a model call may fail; a human must still be asked
  something, and what they are asked is the **fallback card** — the
  same decision shape, with the failed points written out by hand
  instead of by a model, and the reason there is no analysis said on
  the card.

The judge is a fake: the real one is a model call, and what is pinned
here is the wiring, not the model.
"""

from tests.unit.test_terminal_decision_question import (  # noqa: F401
    analysis_orchestrator,
    orchestrator,
)


def _report(*vp_ids):
    """A report whose points all failed, as the terminal branch sees it."""
    return {
        "verification_results": [
            {"id": vp_id, "status": "FAILED"} for vp_id in vp_ids
        ],
    }


def test_question_is_the_brief_card(analysis_orchestrator):
    """With an analysis the question is the card — short, and complete."""
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


def test_payload_carries_the_archive_path(analysis_orchestrator):
    """The payload carries the path the round's archive is written to."""
    payload = analysis_orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    report_path = payload["report_path"]
    assert "reports/terminal-decision-round-2-" in report_path, (
        f"{report_path} is not the round-2 archive path"
    )
    assert report_path.endswith(".md"), (
        f"{report_path} does not name the archive's markdown half"
    )


def test_no_analysis_renders_the_fallback_card(orchestrator):
    """No analysis — the question is the fallback card."""
    payload = orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    question = payload["question"]
    assert question.startswith("【需要你决定】"), (
        f"the question is not a decision card: {question[:40]!r}"
    )
    assert "第 2 轮验证终止" in question, (
        f"the card does not say which round stopped: {question!r}"
    )
    assert "失败点：" in question, (
        f"the card does not write the failed points down: {question!r}"
    )
    assert "VP-3 FAILED" in question, (
        f"the failed point is not on the card: {question!r}"
    )
    assert "完整分析：" in question, (
        f"the card does not point at the report: {question!r}"
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
