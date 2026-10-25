"""Unit tests for the semantic triage judge (``verification.triage_agent``).

The judge is the **tier-B** half of the four-class failure triage: when a
verification point has failed for the same reason often enough that the
loop control wants a human, one tool-less LLM call reads the recorded
failure and names the nature of the failure.

Three contracts are pinned here, and they are the ones that decide whether
the judge's answer may be acted on at all:

1. **A conclusive answer names one of the four classes and points at
   text that is really there.** The quote is verified verbatim against
   the recorded failure text (``failure_triage.validate_evidence_quote``)
   — a paraphrase is not an anchor.
2. **An answer that cannot be verified is not an answer.** An LLM
   exception, an unparseable payload, a class outside the vocabulary or
   a quote that appears nowhere in the recorded failure all degrade to
   ``conclusive=False`` and to the class that counts
   (``deliverable_defect``), so the caller falls back to the repair path
   instead of acting on a claim nobody checked.
3. **One call, no tools.** The judge gets exactly one LLM call per
   ``judge()`` and that call carries ``allowed_tools=[]`` — a judge that
   could run commands would investigate instead of classifying, and a
   judge that could ask twice would quietly disagree with itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _triage_agent():
    """The module under test.

    Imported inside the test bodies on purpose: a module that does not
    exist (or does not export the contract) then shows up as a test
    FAILURE carrying the real reason, instead of a collection ERROR
    that hides which contract broke.
    """
    try:
        from verification import triage_agent
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"verification.triage_agent is not importable: {exc}")
    return triage_agent


class _FakeCodingTool:
    """Records every ``query_json`` call and returns a canned payload.

    A payload that is an exception instance is raised instead of
    returned — that is how the "the provider blew up" path is exercised
    without a real provider.
    """

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def query_json(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


#: The recorded answer of the judge for a broken criterion.
_RECORDED_CRITERION_ANSWER = {
    "failure_class": "criterion_defect",
    "evidence_quote": "schema rejected",
    "rationale": "criterion is wrong",
    "replacement": {"title": "x", "verification_method": "api_test"},
}

#: The verification point and the failure history the judge is handed.
_VP = {"id": "VP-007"}
_HISTORY = [{"failure_class": "criterion_defect", "evidence_quote": "schema rejected"}]


# ---------------------------------------------------------------------------
# 1. A conclusive criterion answer
# ---------------------------------------------------------------------------


def test_conclusive_criterion() -> None:
    """A recorded answer naming a class and quoting real text is kept.

    The conclusion carries the point it is about, the class, the verbatim
    quote, the rationale and the proposed replacement criterion — the
    caller acts on exactly these fields.
    """
    triage_agent = _triage_agent()
    tool = _FakeCodingTool(dict(_RECORDED_CRITERION_ANSWER))

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert conclusion.vp_id == "VP-007"
    assert conclusion.failure_class == "criterion_defect"
    assert conclusion.conclusive is True
    assert conclusion.evidence_quote == "schema rejected"
    assert conclusion.rationale == "criterion is wrong"
    assert conclusion.replacement == {"title": "x", "verification_method": "api_test"}


# ---------------------------------------------------------------------------
# 2. A quote that is not in the recorded failure is not an anchor
# ---------------------------------------------------------------------------


def test_bad_quote_inconclusive() -> None:
    """A quote that appears nowhere in the recorded failure is refused.

    The model is describing what it saw rather than quoting it. The
    conclusion is not conclusive, and the class falls back to the one
    that counts (``deliverable_defect``) so the caller repairs the
    deliverable instead of acting on an unearned claim.
    """
    triage_agent = _triage_agent()
    tool = _FakeCodingTool({
        "failure_class": "criterion_defect",
        "evidence_quote": "the schema was rejected by the validator",
        "rationale": "criterion is wrong",
    })

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert conclusion.conclusive is False
    assert conclusion.failure_class == "deliverable_defect"
    # The refused quote is not carried as if it were an anchor.
    assert conclusion.evidence_quote == ""
    assert conclusion.replacement is None


# ---------------------------------------------------------------------------
# 3. One call, no tools
# ---------------------------------------------------------------------------


def test_single_call_no_tools() -> None:
    """One ``judge()`` is one LLM call, and that call has no tools.

    A judge with tools would investigate instead of classifying, and a
    judge that could ask twice would quietly disagree with itself.
    """
    triage_agent = _triage_agent()
    tool = _FakeCodingTool(dict(_RECORDED_CRITERION_ANSWER))

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert len(tool.calls) == 1
    assert tool.calls[0]["allowed_tools"] == []


# ---------------------------------------------------------------------------
# 4. The other ways an answer fails to be an answer
# ---------------------------------------------------------------------------


def test_llm_exception_inconclusive() -> None:
    """A provider that blows up is not a failed verdict — it is no verdict."""
    triage_agent = _triage_agent()
    tool = _FakeCodingTool(RuntimeError("provider down"))

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert conclusion.conclusive is False
    assert conclusion.failure_class == "deliverable_defect"
    assert len(tool.calls) == 1


def test_unparseable_output_inconclusive() -> None:
    """A payload that is not a JSON object cannot be read as an answer."""
    triage_agent = _triage_agent()
    tool = _FakeCodingTool("not json at all")

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert conclusion.conclusive is False
    assert conclusion.failure_class == "deliverable_defect"


def test_class_outside_the_vocabulary_inconclusive() -> None:
    """A class the four-class vocabulary does not carry is unparseable."""
    triage_agent = _triage_agent()
    tool = _FakeCodingTool({
        "failure_class": "environment_bug",
        "evidence_quote": "schema rejected",
        "rationale": "the machine was busy",
    })

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert conclusion.conclusive is False
    assert conclusion.failure_class == "deliverable_defect"


def test_json_string_payload_is_read() -> None:
    """A payload delivered as a JSON string is the same answer."""
    triage_agent = _triage_agent()
    import json

    tool = _FakeCodingTool(json.dumps(_RECORDED_CRITERION_ANSWER))

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    assert conclusion.conclusive is True
    assert conclusion.failure_class == "criterion_defect"


# ---------------------------------------------------------------------------
# 5. The prompt carries what the judge is allowed to quote
# ---------------------------------------------------------------------------


def test_prompt_carries_the_recorded_failure_text() -> None:
    """The judge may only quote what it was shown.

    The recorded failure text goes into the prompt verbatim, and the
    four classes go in with it, so the answer can be checked against the
    text the judge actually read.
    """
    triage_agent = _triage_agent()
    tool = _FakeCodingTool(dict(_RECORDED_CRITERION_ANSWER))

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    judge.judge(vp=_VP, failure_history=_HISTORY, round_number=4)

    prompt = tool.calls[0]["prompt"]
    assert "schema rejected" in prompt
    assert "VP-007" in prompt
    for failure_class in ("infra", "deliverable_defect", "criterion_defect",
                          "out_of_scope"):
        assert failure_class in prompt


# ---------------------------------------------------------------------------
# 6. The recorded raw output is quotable too
# ---------------------------------------------------------------------------


def test_quote_anchors_in_the_recorded_raw_output() -> None:
    """The anchor is the raw output, not only the previous fragment.

    A history entry carries the raw output of the round; a quote of it
    is a quote of the failure, and it is the shape the production
    history has.
    """
    triage_agent = _triage_agent()
    tool = _FakeCodingTool({
        "failure_class": "deliverable_defect",
        "evidence_quote": "assert 1 == 2",
        "rationale": "the test failed",
    })

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(
        vp=_VP,
        failure_history=[{
            "round": 4,
            "failure_class": "deliverable_defect",
            "actual_result": "E   assert 1 == 2\nE   AssertionError",
        }],
        round_number=4,
    )

    assert conclusion.conclusive is True
    assert conclusion.failure_class == "deliverable_defect"
    assert conclusion.evidence_quote == "assert 1 == 2"


def test_mapping_history_is_read_as_its_entries() -> None:
    """A history handed over keyed by point id is read as its entries.

    A caller that forgot to select its own point would otherwise hand
    the judge a list of id strings, and the judge would read that as
    "nothing was recorded" instead of as the mistake it is.
    """
    triage_agent = _triage_agent()
    tool = _FakeCodingTool(dict(_RECORDED_CRITERION_ANSWER))

    judge = triage_agent.VPTriageJudge(coding_tool=tool)
    conclusion = judge.judge(
        vp=_VP,
        failure_history={"VP-007": dict(_HISTORY[0])},
        round_number=4,
    )

    assert conclusion.conclusive is True
    assert conclusion.evidence_quote == "schema rejected"