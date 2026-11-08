"""Unit tests for the terminal analysis **prompt** (``verification.terminal_analysis``).

The decision card (:func:`render_brief`, see
``test_terminal_analysis_brief_render.py``) shows two fields of the judge's
answer on its own lines: the **context** (the card's first line) and the
**recommendation_reason** (the advice line).  A judge that writes either of
them as a full paragraph turns both lines into a wall of text — the card is
then no shorter than the report it summarises, and the human reading it has
to read a paragraph before reaching the question.

So the prompt has to *ask* for one sentence in each of the two fields, and
say where the detail goes instead: the per-VP detail belongs in
``problem`` / ``evidence`` / ``uncertainty``, which live in the report.

What is pinned here
-------------------

1. **The system prompt asks for one sentence** in ``context`` and in
   ``recommendation_reason``, and names the fields the detail belongs in.
2. **The output format says the same thing** — the JSON template marks
   both fields as 「一句话」, so the contract is stated twice, once as a
   rule and once where the answer is actually written.
3. **The existing contract is untouched.**  The six fields, the
   read-only rule, the no-fabrication rule, the two-recommendation
   vocabulary, the all-Chinese rule and the JSON-only rule all still
   hold — this change makes two fields shorter, it does not change what
   the judge may write.

The module under test is imported inside the test bodies on purpose: a
module that does not export the contract then shows up as a test FAILURE
carrying the real reason, instead of a collection ERROR that hides which
contract broke.
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


def _judge():
    """A judge with a stubbed tool.

    The tool is never called by these tests — they exercise the prompt
    half only — but handing one in keeps the judge from reaching for a
    real :class:`coding_tool.ClaudeCodingTool`.
    """
    return _terminal_analysis().TerminalDecisionJudge(coding_tool=object())


# ---------------------------------------------------------------------------
# 1. The system prompt asks for one sentence
# ---------------------------------------------------------------------------


def test_system_prompt_asks_for_one_sentence_context_and_reason():
    """Both short fields are asked for as one sentence, by name."""
    system_prompt = _judge().build_system_prompt()

    assert "context 只写一句" in system_prompt, (
        "the system prompt does not ask for a one-sentence context"
    )
    assert "recommendation_reason 只写一句" in system_prompt, (
        "the system prompt does not ask for a one-sentence recommendation_reason"
    )
    assert "逐 VP" in system_prompt, (
        "the system prompt does not say where the per-VP detail goes"
    )
    for field in ("problem", "evidence", "uncertainty"):
        assert field in system_prompt, (
            f"the system prompt does not name {field} as where the detail goes"
        )


# ---------------------------------------------------------------------------
# 2. The output format says the same thing
# ---------------------------------------------------------------------------


def _output_requirements(prompt: str) -> str:
    """The 「输出要求」 section of the prompt — where the answer is written."""
    assert "## 输出要求" in prompt, "the prompt has no output-requirements section"
    return prompt.split("## 输出要求", 1)[1]


def test_output_format_marks_the_two_short_fields():
    """The JSON template marks both short fields as 「一句话」."""
    prompt = _judge().build_prompt(
        plan_id="p",
        round_number=1,
        stop_reason="r",
        repair_task_count=0,
        deferred_count=0,
        failed_points=[],
    )
    requirements = _output_requirements(prompt)

    context_desc = requirements.split('"context":', 1)[1].split(', "problem"', 1)[0]
    assert "一句话" in context_desc, (
        "the context description is not marked as one sentence"
    )

    reason_desc = requirements.split('"recommendation_reason":', 1)[1].split(
        ', "uncertainty"', 1
    )[0]
    assert "一句话" in reason_desc, (
        "the recommendation_reason description is not marked as one sentence"
    )

    # The rest of the six-field contract is unchanged.
    for field in ("problem", "evidence", "recommendation", "uncertainty"):
        assert f'"{field}"' in requirements, (
            f"the output format no longer carries the {field} field"
        )


# ---------------------------------------------------------------------------
# 3. The existing contract still holds
# ---------------------------------------------------------------------------


def test_existing_contract_is_untouched():
    """The six fields and the hard rules are still stated."""
    module = _terminal_analysis()
    system_prompt = _judge().build_system_prompt()

    assert "READ ONLY" in system_prompt, "the read-only rule is not stated"
    assert "DO NOT FABRICATE" in system_prompt, (
        "the no-invention rule is not stated"
    )
    assert "NO \"无法确定\"" in system_prompt, (
        "the no-「无法确定」 rule is not stated"
    )
    for recommendation in module.RECOMMENDATIONS:
        assert recommendation in system_prompt, (
            f"the system prompt does not name {recommendation!r}"
        )
    assert "中文" in system_prompt, "the all-Chinese rule is not stated"
    assert "JSON" in system_prompt, "the JSON-only rule is not stated"
    assert "六个字段一个都不许少" in system_prompt, (
        "the six-field contract is no longer stated"
    )
