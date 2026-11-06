"""The terminal question, when there is an analysis to put in it.

The terminal branch's question used to be one sentence of facts.  It is
now the **analysis** of the failure scene when there is one — the five
sections a person reads before answering — and that one sentence of
facts when there is not.

This module is the wiring smoke test for that switch.  It asks the one
question a wiring bug answers wrongly: *which of the two is the
question?*  The detail of the switch — the sections, the evidence, the
cap, the default option, and the byte-identical fallback — is pinned in
``tests/unit/test_terminal_decision_question.py``; this file only asks
whether the analysis path is wired at all.

The judge is a fake: the real one is a model call, and what is pinned
here is the wiring, not the model.
"""

from tests.unit.test_terminal_decision_question import (  # noqa: F401
    analysis_orchestrator,
)

#: The question the terminal branch asked before the analysis existed.
_FACTUAL_QUESTION = (
    "verification round 2 stopped with repair_execution_failed: "
    "0 repair task(s) were generated; failed checks: VP-3=FAILED. "
    "Choose how to proceed."
)


def test_analysis_path_is_wired_not_the_factual_sentence(analysis_orchestrator):
    """With an analysis the question is the analysis, not the old sentence."""
    report = {"verification_results": [{"id": "VP-3", "status": "FAILED"}]}

    payload = analysis_orchestrator._terminal_decision_question(
        report, 2, ["VP-3"], 0,
    )

    assert payload["question"] != _FACTUAL_QUESTION, (
        "the question is still the old factual sentence — the analysis "
        "never reached it"
    )
    assert payload["question"].startswith("## 背景（C）")
