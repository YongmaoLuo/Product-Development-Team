"""The terminal question is the **decision card**, and the card knows the report.

Two things are pinned here, and they are the two things the human on the
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

And the contract that survives both: **without an analysis the question
is still the old factual sentence, byte for byte.**  The judge is a
model call and a model call may fail; a human must still be asked
something, and what they are asked is exactly what they were asked
before the card existed.

The judge is a fake: the real one is a model call, and what is pinned
here is the wiring, not the model.
"""

from tests.unit.test_terminal_decision_question import (  # noqa: F401
    analysis_orchestrator,
    orchestrator,
)


#: The question the terminal branch asked before the card existed —
#: the one the card replaces, and the one it falls back to.
_FACTUAL_QUESTION = (
    "verification round 2 stopped with repair_execution_failed: "
    "0 repair task(s) were generated; failed checks: VP-3=FAILED. "
    "Choose how to proceed."
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


def test_no_analysis_keeps_the_factual_sentence(orchestrator):
    """No analysis — the old sentence stands, byte for byte."""
    payload = orchestrator._terminal_decision_question(
        _report("VP-3"), 2, ["VP-3"], 0,
    )

    assert payload["question"] == _FACTUAL_QUESTION, (
        "the factual sentence was not kept byte for byte"
    )
    assert set(payload) == {
        "question", "options", "default_option", "report_path",
    }, (
        "the fallback payload must still carry the four keys the caller "
        "reads — the decision is created either way"
    )
