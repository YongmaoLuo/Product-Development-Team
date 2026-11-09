"""Unit tests for the terminal decision card (``verification.terminal_analysis``).

The report renderer (:func:`render_analysis`) produces the five C/P/E/A/U
sections.  That is a good *report*: it carries the evidence verbatim and
keeps the advice whole under every cut.  It is also too long to answer a
question with — a real one runs to about 3.2KB, and the person reading
it has to dig out of it what they are being asked to decide.

This module covers the other rendering: the **card**.  It is what the
human actually reads, and it is bounded on purpose.

What is pinned here
-------------------

1. **The card is short and complete.**  A six-field analysis of about
   3KB renders into at most ``TERMINAL_BRIEF_MAX_CHARS`` characters and
   ``TERMINAL_BRIEF_MAX_LINES`` lines, and still carries the failed
   point id, every option, the recommendation id and the report path.
   A card that is short but incomplete is not a decision card — it is a
   teaser.
2. **The cut order keeps the argument whole.**  An over-long
   ``recommendation_reason`` is reduced to its **first sentence**
   first; only then is the background sentence cut; the option lines,
   the recommendation id and the report path are cut *not at all*,
   because they are what the human acts on.
3. **An invalid analysis renders as nothing.**  Not a mapping, a
   missing field, a recommendation outside the vocabulary — all of them
   produce ``""``, and the caller's fallback is the factual sentence it
   asked before.

The renderer is imported by name inside the test bodies on purpose: a
module that does not export it then fails with the real
``ImportError: cannot import name 'render_brief' ...`` instead of a
collection ERROR that hides which contract broke.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _module():
    """The module under test."""
    from verification import terminal_analysis

    return terminal_analysis


def _render_brief():
    """The card renderer under test.

    Imported by name: when the entry point does not exist yet, the
    test fails with the real ``ImportError`` naming it.
    """
    from verification.terminal_analysis import render_brief

    return render_brief


# ---------------------------------------------------------------------------
# The sample: an analysis of the size the judge really writes
# ---------------------------------------------------------------------------

_DETAIL_TEMPLATE = (
    "第 {round} 轮生成的修复任务只覆盖了 {point} 的失败分支，其余断言没有"
    "被触达；验证链路在下一轮仍然读到同一处 AssertionError: expected 200, "
    "got 500，于是被判定为实现缺陷而不是环境偶发，repair_generator 也不再"
    "为同一判据生成新的修复任务，第 {round} 轮的 repair 任务数因此为 0。"
)


def _details(count: int) -> str:
    return "\n".join(
        _DETAIL_TEMPLATE.format(round=index, point="VP-3")
        for index in range(1, count + 1)
    )


def _long_analysis():
    """A six-field analysis of about 3KB — the size of a real one.

    The judge writes the report in full: the context names the files it
    read, the problem lists every failed point, the evidence quotes the
    verdicts.  The card has to be shorter than any of that, which is
    why the sample is long.
    """
    return {
        "context": (
            "第 4 轮停止：实现已存在，但契约测试仍失败。\n" + _details(12)
        ),
        "problem": (
            "VP-3「登录接口返回 401」连续三轮失败，判定为实现缺陷而非环境偶发："
            "第 2 轮修的是 handler 的入参校验，第 3 轮修的是 client 的重试退避，"
            "两处改动都没有触达断言失败的那一行，"
            "因此第 4 轮读到的仍是同一个 AssertionError。"
            "链路无法自愈的根因是判据本身覆盖不到实现分支，"
            "而不是修复任务没有生成。"
        ),
        "evidence": [
            "VP-3 连续 3 轮失败：failure_class=deliverable_defect，"
            "verdict 为 AssertionError: expected 200, got 500，"
            "未见环境偶发特征（无超时、无 5xx 抖动、无依赖不可用）。",
            "第 4 轮 repair 任务数为 0：repair_generator 在同一判据连续失败"
            "达到阈值后不再生成新的修复任务。",
            "verification_report.json 的 generated_at 为 2026-11-08T10:10:10Z，"
            "state.db 的 plan_verification 行同步写入本轮结果。",
        ],
        "recommendation": "revise_criterion",
        "recommendation_reason": (
            "同一失败连续 3 轮，重跑不会再产生新信息："
            "判据覆盖不到实现分支，重跑只会再失败一次。"
        ),
        "uncertainty": (
            "未读 vp_artifacts/VP-3/ci_output.json 的第一条错误，"
            "因此不能断言断言失败的那一行属于哪一次改动引入的。"
        ),
    }


#: The report the card points at.
_REPORT_PATH = (
    "plans/demo-plan/reports/terminal-decision-round-4-20261108T101010Z.md"
)


# ---------------------------------------------------------------------------
# 1. The card is short and complete
# ---------------------------------------------------------------------------


def test_the_card_has_the_documented_shape():
    """The card is exactly the shape the plan documents."""
    render_brief = _render_brief()

    analysis = {
        "context": "第 4 轮停止：实现已存在，但契约测试仍失败。",
        "problem": "VP-3 连续三轮失败，判定为实现缺陷。",
        "evidence": ["VP-3 连续 3 轮失败", "第 4 轮 repair 任务数为 0"],
        "recommendation": "revise_criterion",
        "recommendation_reason": "同一失败连续 3 轮，重跑不会再产生新信息。",
        "uncertainty": "未读 vp_artifacts/VP-3/ci_output.json 的第一条错误。",
    }

    card = render_brief(
        analysis,
        plan_id="p1",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=(
            "plans/p1/reports/terminal-decision-round-4-20261108T101010Z.md"
        ),
    )

    assert card == (
        "【需要你决定】p1 第 4 轮验证终止：VP-3："
        "第 4 轮停止：实现已存在，但契约测试仍失败\n"
        "\n"
        "选项：\n"
        "1. 重跑本轮验证（rerun_this_round）—— 环境偶发失败，重跑一轮可能就通过\n"
        "2. 修订判据（本轮失败的判据作废，由操作者另行补充新判据）"
        "（revise_criterion）—— 判据本身有问题，需要人重写判据\n"
        "\n"
        "建议：修订判据（revise_criterion）—— "
        "同一失败连续 3 轮，重跑不会再产生新信息\n"
        "完整分析：plans/p1/reports/"
        "terminal-decision-round-4-20261108T101010Z.md"
    ), f"the card is not the documented shape:\n{card}"


def test_brief_is_short_and_complete():
    """A 3KB analysis renders into a card that is short AND complete."""
    module = _module()
    render_brief = _render_brief()

    analysis = _long_analysis()
    sample_size = len(json.dumps(analysis, ensure_ascii=False))
    assert sample_size > 2000, (
        f"the sample is only {sample_size} characters; the shortness "
        "assertion below would be vacuous"
    )

    card = render_brief(
        analysis,
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    assert card, "a valid analysis rendered as nothing"
    assert len(card) <= module.TERMINAL_BRIEF_MAX_CHARS, (
        f"the card is {len(card)} characters, over the cap of "
        f"{module.TERMINAL_BRIEF_MAX_CHARS}"
    )
    assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
        f"the card has {len(card.splitlines())} lines, over the cap of "
        f"{module.TERMINAL_BRIEF_MAX_LINES}"
    )

    assert "VP-3" in card, "the failed point id is missing"
    assert card.startswith("【需要你决定】"), "the card does not open as a decision"
    for recommendation in module.RECOMMENDATIONS:
        assert recommendation in card, (
            f"the option {recommendation!r} is missing from the card"
        )
    assert "revise_criterion" in card, "the recommendation id is missing"
    assert _REPORT_PATH in card, "the report path is missing"


def test_brief_carries_every_option_on_its_own_line():
    """Every option is offered, one per line, with its consequence."""
    module = _module()
    render_brief = _render_brief()

    card = render_brief(
        _long_analysis(),
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    lines = card.splitlines()
    option_lines = [
        line
        for line in lines
        if line.startswith(("1. ", "2. "))
    ]
    assert len(option_lines) == len(module.RECOMMENDATIONS), (
        f"expected {len(module.RECOMMENDATIONS)} option lines, got "
        f"{option_lines!r}"
    )
    for index, recommendation in enumerate(module.RECOMMENDATIONS, start=1):
        expected = (
            f"{index}. {module.RECOMMENDATION_LABELS[recommendation]}"
            f"（{recommendation}）—— "
            f"{module.RECOMMENDATION_CONSEQUENCES[recommendation]}"
        )
        assert expected in lines, (
            f"the option line {expected!r} is not one of the card's lines"
        )


# ---------------------------------------------------------------------------
# 2. The cut order
# ---------------------------------------------------------------------------


def test_brief_keeps_the_reasons_first_sentence_and_cuts_the_rest():
    """An over-long reason keeps its first sentence; the rest is cut."""
    module = _module()
    render_brief = _render_brief()

    long_reason = "同一失败连续 3 轮，重跑不会再产生新信息。" + ("细节论证。" * 400)
    analysis = _long_analysis()
    analysis["recommendation_reason"] = long_reason

    card = render_brief(
        analysis,
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    assert len(card) <= module.TERMINAL_BRIEF_MAX_CHARS, (
        f"the card is {len(card)} characters, over the cap"
    )
    assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
        f"the card has {len(card.splitlines())} lines, over the cap"
    )

    # The argument's opening is kept: the decision rests on it.
    assert "同一失败连续 3 轮，重跑不会再产生新信息" in card, (
        f"the reason's first sentence was cut: {card!r}"
    )

    # The rest of the argument is not.
    assert long_reason not in card, "the over-long reason was not cut"
    assert "细节论证" not in card, (
        f"the reason's tail was not cut: {card!r}"
    )

    # The options are cut not at all.
    for index, recommendation in enumerate(module.RECOMMENDATIONS, start=1):
        expected = (
            f"{index}. {module.RECOMMENDATION_LABELS[recommendation]}"
            f"（{recommendation}）—— "
            f"{module.RECOMMENDATION_CONSEQUENCES[recommendation]}"
        )
        assert expected in card.splitlines(), (
            f"the option line {expected!r} was cut"
        )

    # The recommendation id is cut not at all.
    advice_lines = [line for line in card.splitlines() if line.startswith("建议：")]
    assert len(advice_lines) == 1, f"the advice line is missing: {card!r}"
    assert "revise_criterion" in advice_lines[0], (
        f"the recommendation id was cut: {advice_lines[0]!r}"
    )


def test_brief_cuts_the_background_when_the_reason_is_not_enough():
    """When the first sentence is not enough, the background sentence goes."""
    module = _module()
    render_brief = _render_brief()

    analysis = _long_analysis()
    # One sentence: there is no second sentence to fall back on, so the
    # background can only be cut short — and it is.
    analysis["context"] = "背景" + ("很长的背景细节" * 200)
    analysis["recommendation_reason"] = "第一句理由。" + ("后续论证" * 300)

    card = render_brief(
        analysis,
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    assert len(card) <= module.TERMINAL_BRIEF_MAX_CHARS, (
        f"the card is {len(card)} characters, over the cap"
    )
    assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
        f"the card has {len(card.splitlines())} lines, over the cap"
    )

    # The reason's first sentence is kept.
    assert "第一句理由" in card, (
        f"the reason's first sentence was cut: {card!r}"
    )
    # The reason's tail is not.
    assert "后续论证" not in card, (
        f"the reason's tail was not cut: {card!r}"
    )
    # The background is the part that was cut, and the cut is marked.
    assert "…" in card, f"the cut is not marked: {card!r}"

    # The options are cut not at all.
    for index, recommendation in enumerate(module.RECOMMENDATIONS, start=1):
        expected = (
            f"{index}. {module.RECOMMENDATION_LABELS[recommendation]}"
            f"（{recommendation}）—— "
            f"{module.RECOMMENDATION_CONSEQUENCES[recommendation]}"
        )
        assert expected in card.splitlines(), (
            f"the option line {expected!r} was cut"
        )


def test_brief_keeps_the_first_sentence_of_an_english_reason():
    """An English reason has a first sentence too, and it is kept."""
    module = _module()
    render_brief = _render_brief()

    analysis = _long_analysis()
    analysis["recommendation_reason"] = (
        "Rerunning will not add information. " + ("More argument. " * 100)
    )

    card = render_brief(
        analysis,
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    assert len(card) <= module.TERMINAL_BRIEF_MAX_CHARS, (
        f"the card is {len(card)} characters, over the cap"
    )
    assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
        f"the card has {len(card.splitlines())} lines, over the cap"
    )

    assert "Rerunning will not add information" in card, (
        f"the reason's first sentence was cut: {card!r}"
    )
    assert "More argument" not in card, (
        f"the reason's tail was not cut: {card!r}"
    )


def test_brief_does_not_split_on_decimal_points_or_abbreviations():
    """A decimal point and an abbreviation are not sentence ends."""
    module = _module()
    render_brief = _render_brief()

    analysis = _long_analysis()
    analysis["recommendation_reason"] = (
        "version 1.2 stays untouched and e.g. this stays too. "
        "The second sentence goes on and on. " + ("More. " * 100)
    )

    card = render_brief(
        analysis,
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    assert len(card) <= module.TERMINAL_BRIEF_MAX_CHARS, (
        f"the card is {len(card)} characters, over the cap"
    )
    assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
        f"the card has {len(card.splitlines())} lines, over the cap"
    )

    # The first sentence is whole: the decimal point and the
    # abbreviation did not cut it.
    assert "version 1.2 stays untouched and e.g. this stays too" in card, (
        f"the first sentence was cut at a decimal point or an "
        f"abbreviation: {card!r}"
    )


# ---------------------------------------------------------------------------
# 3. An analysis that is not one
# ---------------------------------------------------------------------------


def test_brief_rejects_a_non_analysis():
    """None, a missing field, an illegal recommendation: all render as ""."""
    render_brief = _render_brief()

    assert render_brief(None) == "", "None rendered as something"

    analysis = _long_analysis()
    del analysis["recommendation_reason"]
    assert render_brief(analysis) == "", "a missing field rendered as something"

    analysis = _long_analysis()
    analysis["recommendation"] = "just_do_it"
    assert render_brief(analysis) == "", "an illegal recommendation rendered as something"

    analysis = _long_analysis()
    analysis["context"] = ""
    assert render_brief(analysis) == "", "an empty field rendered as something"

    assert render_brief("not an analysis") == "", "a string rendered as something"


# ---------------------------------------------------------------------------
# 4. The card's shape
# ---------------------------------------------------------------------------


def test_brief_without_failed_points_has_no_id_prefix():
    """With no failed point, the first line carries no id prefix."""
    render_brief = _render_brief()

    card = render_brief(
        _long_analysis(),
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=[],
        report_path=_REPORT_PATH,
    )

    first_line = card.splitlines()[0]
    assert first_line.startswith("【需要你决定】demo-plan 第 4 轮验证终止："), (
        f"the first line is not what it should be: {first_line!r}"
    )


def test_brief_without_a_report_path_has_no_report_line():
    """With no report path, the card does not render an empty one."""
    render_brief = _render_brief()

    card = render_brief(
        _long_analysis(),
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path="",
    )

    assert "完整分析：" not in card, "an empty report path was rendered"
    assert len(card.splitlines()) <= _module().TERMINAL_BRIEF_MAX_LINES


def test_brief_uses_the_first_sentence_of_the_context():
    """The first line carries the context's first sentence, and only it."""
    render_brief = _render_brief()

    card = render_brief(
        _long_analysis(),
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    first_line = card.splitlines()[0]
    assert "第 4 轮停止：实现已存在，但契约测试仍失败" in first_line, (
        f"the first sentence is missing from the first line: {first_line!r}"
    )
    assert "AssertionError" not in first_line, (
        "the first line carries more than the first sentence"
    )


def test_the_caps_are_the_documented_ones():
    """The card's caps are 600 characters and 8 lines."""
    module = _module()

    assert module.TERMINAL_BRIEF_MAX_CHARS == 600
    assert module.TERMINAL_BRIEF_MAX_LINES == 8


def test_render_analysis_is_untouched():
    """The report renderer is still the report renderer."""
    module = _module()

    report = module.render_analysis(_long_analysis())

    assert "## 背景（C）" in report
    assert "## 建议（A）" in report
    assert len(report) > 600, (
        "the report stopped being a report; the card is the short one"
    )


# ---------------------------------------------------------------------------
# 5. The card is eight lines, whatever the judge wrote
# ---------------------------------------------------------------------------

#: Every character ``str.splitlines()`` cuts a line at.  A card counted
#: with ``splitlines()`` has to fold all of them — a card that is nine
#: lines because the reason carried a vertical tab is as far over the cap
#: as one that is nine lines because the reason carried a newline.
_LINE_BREAKS = (
    "\n",
    "\r",
    "\r\n",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


def _analysis_with_reason(reason: str):
    """The long analysis, with the reason the judge wrote."""
    analysis = _long_analysis()
    analysis["recommendation_reason"] = reason
    return analysis


def test_a_multi_line_reason_still_renders_an_eight_line_card():
    """A reason the judge wrote over several lines is folded into one."""
    module = _module()
    render_brief = _render_brief()

    for breaks in ("\n", "\n" * 5, "\r\n", "\u2028"):
        card = render_brief(
            _analysis_with_reason("理由第一行" + breaks + "理由第二行"),
            plan_id="demo-plan",
            round_number=4,
            failed_point_ids=["VP-3"],
            report_path=_REPORT_PATH,
        )

        assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
            f"the card has {len(card.splitlines())} lines for a reason "
            f"broken by {breaks!r}:\n{card}"
        )
        assert "理由第一行" in card, f"the reason's head is gone:\n{card}"
        assert "理由第二行" in card, f"the reason's tail is gone:\n{card}"


def test_every_line_break_is_folded_into_the_card():
    """Every character ``splitlines()`` cuts at is folded into one line."""
    module = _module()
    render_brief = _render_brief()

    for break_char in _LINE_BREAKS:
        card = render_brief(
            _analysis_with_reason("理由第一行" + break_char + "理由第二行"),
            plan_id="demo-plan",
            round_number=4,
            failed_point_ids=["VP-3"],
            report_path=_REPORT_PATH,
        )

        assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
            f"the card has {len(card.splitlines())} lines for a reason "
            f"broken by {break_char!r}:\n{card}"
        )


def test_every_free_text_field_is_folded_into_one_line():
    """The plan id, the point ids and the report path add no lines."""
    module = _module()
    render_brief = _render_brief()

    card = render_brief(
        _long_analysis(),
        plan_id="demo-plan\n第 4 轮",
        round_number=4,
        failed_point_ids=["VP-3\nVP-4"],
        report_path=_REPORT_PATH + "\n第 2 行",
    )

    assert len(card.splitlines()) <= module.TERMINAL_BRIEF_MAX_LINES, (
        f"the card has {len(card.splitlines())} lines:\n{card}"
    )
    assert "VP-3 VP-4" in card, f"the point ids were not folded:\n{card}"


def test_folding_keeps_the_spaces_inside_a_line():
    """Folding a line does not rewrite the judge's own spacing."""
    render_brief = _render_brief()

    card = render_brief(
        _analysis_with_reason("理由  第一行\n理由第二行"),
        plan_id="demo-plan",
        round_number=4,
        failed_point_ids=["VP-3"],
        report_path=_REPORT_PATH,
    )

    assert "理由  第一行 理由第二行" in card, (
        f"the spacing inside the line was rewritten:\n{card}"
    )
