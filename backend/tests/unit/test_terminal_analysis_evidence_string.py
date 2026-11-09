"""Unit tests for the terminal analysis **evidence contract**
(``verification.terminal_analysis``).

The judge answers with six fields, and one of them is ``evidence``: the
facts the recommendation rests on.  The contract used to say that
``evidence`` is an **array of strings** — and a judge that found a single
fact and wrote it as a sentence ("VP-1 连续 3 轮 500") had its whole
answer thrown away over the shape of one field.  The result was a
terminal round with *no analysis at all*: the human was asked the bare
factual question, with none of the judge's reasoning in it.

So the contract now says: **one string is evidence too**.  It is taken
as the one-element array it means, and the text is kept verbatim — it is
the judge's own evidence, and trimming or paraphrasing it would be
rewriting it.

What is pinned here
-------------------

1. **A string of evidence is accepted** and normalised into a
   one-element list whose element is the original text, word for word.
2. **Every other unusable evidence is still refused** — and the refusal
   now *says which check failed* instead of returning a bare ``None``.
3. **The prompt asks for an array of strings**, in both halves (the
   system prompt and the per-call prompt), so the judge writes the shape
   the contract accepts: 字符串数组, and a single fact still goes into a
   单元素数组.
4. **The old array contract is untouched** — a list is still trimmed
   item by item, and a tuple is still read as a list.
5. **The judge's own call path keeps the answer** — this is the symptom
   the fix is for, asserted through :meth:`TerminalDecisionJudge.analyze`
   rather than through the helper alone.

The module under test is imported inside the test bodies on purpose: a
module that does not export the contract then shows up as a test
FAILURE carrying the real reason, instead of a collection ERROR that
hides which contract broke.
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


def _terminal_analysis():
    """The module under test.

    Imported inside the test bodies so a missing module is a FAILURE
    with the real reason rather than a collection ERROR.
    """
    try:
        from verification import terminal_analysis
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"verification.terminal_analysis is not importable: {exc}")
    return terminal_analysis


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


#: A well-formed answer: the six fields the contract asks for.
_PAYLOAD = {
    "context": "第 3 轮终局停止",
    "problem": "0 条修复任务，链路无法自愈",
    "evidence": ["VP-1 连续 3 轮 500"],
    "recommendation": "rerun_this_round",
    "recommendation_reason": "环境偶发失败",
    "uncertainty": "缺 VP-1 的服务日志，去 <project_dir>/logs 读",
}


def _payload(**overrides):
    """A well-formed answer with ``overrides`` applied."""
    payload = dict(_PAYLOAD)
    payload.update(overrides)
    return payload


def _validate(payload):
    """The validated answer, and the reason it was refused (if it was).

    The two-value entry point is the one under test here: it is what
    makes a refusal say *which* check failed.
    """
    module = _terminal_analysis()
    entry = getattr(module, "validate_analysis_with_reason", None)
    assert entry is not None, (
        "verification.terminal_analysis does not export "
        "validate_analysis_with_reason"
    )
    return entry(payload)


class _FakeTool:
    """A coding tool that answers with a fixture."""

    def __init__(self, response=None) -> None:
        self.calls: list[dict] = []
        self._response = response

    def query_json(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


# ---------------------------------------------------------------------------
# 1. A string of evidence is accepted
# ---------------------------------------------------------------------------


def test_string_evidence_is_accepted_and_normalized():
    """One piece of evidence written as a sentence is still evidence."""
    text = "VP-1 连续 3 轮 500"

    analysis, reason = _validate(_payload(evidence=text))

    assert analysis is not None, f"a string evidence was thrown away: {reason}"
    assert reason == "", f"a valid answer came back with a reason: {reason}"
    assert analysis["evidence"] == [text]
    assert analysis["evidence"][0] == text, (
        "the evidence was not kept word for word"
    )


def test_string_evidence_is_kept_verbatim():
    """The judge's own text is kept as written — not trimmed, not cut."""
    text = "  VP-1 连续 3 轮 500 （同一处断言）\n"

    analysis, reason = _validate(_payload(evidence=text))

    assert analysis is not None, f"a string evidence was thrown away: {reason}"
    assert analysis["evidence"] == [text]


def test_the_judge_keeps_an_answer_whose_evidence_is_a_sentence():
    """The judge's own call path keeps the answer — this is the symptom."""
    module = _terminal_analysis()
    judge = module.TerminalDecisionJudge(
        coding_tool=_FakeTool(response=_payload(evidence="VP-1 连续 3 轮 500"))
    )

    analysis = judge.analyze(
        plan_id="p1",
        round_number=3,
        stop_reason="repair_execution_failed",
        repair_task_count=0,
        deferred_count=1,
        failed_points=[],
        plan_dir="/tmp/plans/p1",
        project_dir="/tmp/projects/demo",
    )

    assert analysis is not None, (
        "the judge threw away a good answer over the shape of one field"
    )
    assert analysis["evidence"] == ["VP-1 连续 3 轮 500"]


# ---------------------------------------------------------------------------
# 2. Everything else is still refused — and says why
# ---------------------------------------------------------------------------


def test_invalid_evidence_still_returns_none_with_a_reason():
    """Empty string, empty list and non-string items are refused, with a reason."""
    for bad in ("", [], [123]):
        analysis, reason = _validate(_payload(evidence=bad))

        assert analysis is None, f"{bad!r} was accepted as evidence"
        assert "evidence must be a list of strings" in reason, (
            f"{bad!r} was refused without saying why: {reason!r}"
        )


def test_a_blank_string_evidence_is_refused_with_a_reason():
    """A string that says nothing is not evidence."""
    analysis, reason = _validate(_payload(evidence="   \n "))

    assert analysis is None
    assert "evidence must be a list of strings" in reason, (
        f"the refusal did not say why: {reason!r}"
    )


def test_a_list_holding_an_empty_string_is_refused_with_a_reason():
    """An array with an empty item in it is not evidence either."""
    analysis, reason = _validate(_payload(evidence=[""]))

    assert analysis is None
    assert "evidence must be a list of strings" in reason, (
        f"the refusal did not say why: {reason!r}"
    )


def test_a_missing_field_is_refused_with_a_reason():
    """A missing field is refused, and the refusal names the field."""
    payload = _payload()
    del payload["uncertainty"]

    analysis, reason = _validate(payload)

    assert analysis is None
    assert reason, "the refusal did not say why"
    assert "uncertainty" in reason, (
        f"the refusal did not name the missing field: {reason!r}"
    )


def test_an_unknown_recommendation_is_refused_with_a_reason():
    """A recommendation outside the vocabulary is refused, and says so."""
    analysis, reason = _validate(_payload(recommendation="delete_plan"))

    assert analysis is None
    assert reason, "the refusal did not say why"
    assert "recommendation" in reason, (
        f"the refusal did not name the field: {reason!r}"
    )


# ---------------------------------------------------------------------------
# 3. The prompt asks for an array of strings
# ---------------------------------------------------------------------------


def test_prompts_require_a_string_array():
    """Both halves of the prompt ask for an array of strings."""
    module = _terminal_analysis()
    judge = module.TerminalDecisionJudge(coding_tool=_FakeTool())

    system_prompt = judge.build_system_prompt()
    per_call_prompt = judge.build_prompt(
        plan_id="p1",
        round_number=3,
        stop_reason="repair_execution_failed",
        repair_task_count=0,
        deferred_count=1,
        failed_points=[],
    )

    for name, prompt in (
        ("system prompt", system_prompt),
        ("per-call prompt", per_call_prompt),
    ):
        assert "字符串数组" in prompt, (
            f"the {name} does not ask for a 字符串数组"
        )
        assert "单元素数组" in prompt, (
            f"the {name} does not ask for a 单元素数组"
        )


# ---------------------------------------------------------------------------
# 4. The old array contract is untouched
# ---------------------------------------------------------------------------


def test_list_evidence_keeps_its_old_normalisation():
    """An array is still normalised the way it always was: item by item."""
    analysis, reason = _validate(_payload(evidence=["  VP-1 连续 3 轮 500  "]))

    assert analysis is not None, reason
    assert analysis["evidence"] == ["VP-1 连续 3 轮 500"]


def test_tuple_evidence_is_read_as_a_list():
    """A tuple is still evidence."""
    analysis, reason = _validate(_payload(evidence=("VP-1 连续 3 轮 500",)))

    assert analysis is not None, reason
    assert analysis["evidence"] == ["VP-1 连续 3 轮 500"]


def test_the_private_entry_point_still_answers_analysis_or_none():
    """``_validate_analysis`` keeps its signature: an analysis or ``None``."""
    module = _terminal_analysis()
    private = getattr(module, "_validate_analysis", None)
    assert private is not None, "the module no longer has _validate_analysis"

    analysis = private(_payload(evidence="VP-1 连续 3 轮 500"))
    assert analysis is not None
    assert analysis["evidence"] == ["VP-1 连续 3 轮 500"]

    assert private(_payload(evidence=[])) is None
    assert private(_payload(evidence="")) is None
