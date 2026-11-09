"""Unit tests for the fallback card (``verification.terminal_analysis``).

The decision card (:func:`render_brief`) is rendered from an
*analysis*.  When the judge produced no analysis — no answer at all, or
an answer that failed the contract — there is no analysis to render, and
the human used to be asked a bare sentence of facts that named no
failure point.  The reader could not tell *what* failed, let alone
decide.

The fallback card (:func:`render_fallback_brief`) is what is sent
instead: the same decision shape — what has to be decided, the options,
the advice, the path of the report — with the failed points spelled out
by hand instead of by a model.

What is pinned here
-------------------

1. **The card stays inside its caps.**  At most
   ``TERMINAL_FALLBACK_BRIEF_MAX_LINES`` lines and
   ``TERMINAL_FALLBACK_BRIEF_MAX_CHARS`` characters, however many
   points failed and however long their actual results were.
2. **The points that do not fit are counted, not dropped silently.**
   The card names at most ``TERMINAL_FALLBACK_MAX_POINTS`` of them and
   says how many more there are.
3. **The detail is cut before the point is.**  An over-long
   ``actual_result`` loses its tail; the title, the status, the options
   and the report path never lose anything, because they are what the
   human acts on.
4. **The reason the analysis is missing is on the card.**  Otherwise
   the reader cannot tell "no analysis yet" from "the analysis was
   rejected".

The renderer is imported by name inside the test bodies on purpose: a
module that does not export it then fails with the real
``ImportError: cannot import name 'render_fallback_brief' ...``
instead of a collection ERROR that hides which contract broke.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _render_fallback_brief():
    """The fallback card renderer under test.

    Imported by name: when the entry point does not exist yet, the
    test fails with the real ``ImportError`` naming it.
    """
    from verification.terminal_analysis import render_fallback_brief

    return render_fallback_brief


def _points(count: int, *, actual_result: str = "HTTP 500。堆栈未捕获"):
    """``count`` failed points, the shape the orchestrator hands over."""
    return [
        {
            "id": f"VP-{index}",
            "status": "FAILED",
            "title": f"登录接口 {index}",
            "actual_result": actual_result,
        }
        for index in range(1, count + 1)
    ]


def _card(**overrides) -> str:
    """The fallback card for the sample scene, with the given overrides."""
    kwargs = {
        "plan_id": "p",
        "round_number": 3,
        "stop_reason": "same_failure_repeated",
        "points": _points(1),
        "report_path": "reports/terminal-decision-round-3-x.md",
        "analysis_error": "analysis_empty",
    }
    kwargs.update(overrides)
    return _render_fallback_brief()(**kwargs)


# ---------------------------------------------------------------------------
# The caps
# ---------------------------------------------------------------------------


def test_four_points_fit_and_count_the_rest():
    """Four failed points: three are named, the fourth is counted."""
    card = _card(points=_points(4))

    assert len(card.splitlines()) == 12, (
        "the card is not the twelve lines it is supposed to be:\n" + card
    )
    assert len(card) <= 900, (
        f"the card is {len(card)} characters long, over the 900 cap:\n" + card
    )
    assert "另有 1 个失败点" in card, (
        "the card does not say that one failed point was left out:\n" + card
    )
    for index in (1, 2, 3):
        assert f"VP-{index} FAILED" in card, (
            f"VP-{index} is not named on the card:\n" + card
        )
    assert "VP-4 FAILED" not in card, (
        "the card named four points — the cap is three:\n" + card
    )


def test_ten_points_fit_and_count_the_rest():
    """Ten failed points: three are named, the other seven are counted."""
    card = _card(points=_points(10))

    assert len(card.splitlines()) == 12, (
        "the card is not the twelve lines it is supposed to be:\n" + card
    )
    assert len(card) <= 900, (
        f"the card is {len(card)} characters long, over the 900 cap:\n" + card
    )
    assert "另有 7 个失败点" in card, (
        "the card does not count the seven failed points it left out:\n" + card
    )


# ---------------------------------------------------------------------------
# The cut order
# ---------------------------------------------------------------------------


def test_long_actual_result_is_cut_before_title():
    """An over-long actual result loses its tail, not the point itself."""
    # One sentence, on purpose: a result with a sentence break in it
    # would be cut by the first-sentence rule, and this test is about
    # the card's own cut.
    long_result = "堆栈未捕获：" + "AssertionError: expected 200, got 500; " * 200

    card = _card(points=_points(1, actual_result=long_result))

    assert len(card) <= 900, (
        f"the card is {len(card)} characters long, over the 900 cap:\n" + card
    )
    assert len(card.splitlines()) <= 12, (
        "the card is over the twelve line cap:\n" + card
    )
    assert "VP-1 FAILED" in card, (
        "the point's id and status were cut:\n" + card
    )
    assert "登录接口 1" in card, (
        "the point's title was cut:\n" + card
    )
    assert "rerun_this_round" in card, (
        "an option was cut:\n" + card
    )
    assert "revise_criterion" in card, (
        "an option was cut:\n" + card
    )
    assert "reports/terminal-decision-round-3-x.md" in card, (
        "the report path was cut:\n" + card
    )
    assert long_result not in card, (
        "the actual result was not cut at all:\n" + card
    )
    assert "…" in card, (
        "the actual result was cut without saying so:\n" + card
    )


# ---------------------------------------------------------------------------
# The cut order
# ---------------------------------------------------------------------------


def test_long_details_are_cut_before_a_point_is_dropped():
    """The detail is cut first: the points themselves are kept."""
    long_result = "堆栈未捕获：" + "AssertionError: expected 200, got 500; " * 200

    card = _card(points=_points(10, actual_result=long_result))

    assert len(card) <= 900, (
        f"the card is {len(card)} characters long, over the 900 cap:\n" + card
    )
    assert len(card.splitlines()) == 12, (
        "the card is not the twelve lines it is supposed to be:\n" + card
    )
    for index in (1, 2, 3):
        assert f"VP-{index} FAILED —— 登录接口 {index}" in card, (
            f"VP-{index} was dropped instead of having its detail cut:\n" + card
        )
    assert "另有 7 个失败点" in card, (
        "the card does not count the seven failed points it left out:\n" + card
    )


# ---------------------------------------------------------------------------
# The reason the analysis is missing
# ---------------------------------------------------------------------------


def test_reason_is_rendered():
    """The reason the analysis is missing is on the card, verbatim."""
    reason = "analysis_rejected:evidence must be a list of strings"

    card = _card(analysis_error=reason)

    assert reason in card, (
        "the card does not say why there is no analysis:\n" + card
    )


# ---------------------------------------------------------------------------
# The shape of the card
# ---------------------------------------------------------------------------


def test_card_has_one_blank_line_after_the_heading():
    """The blank line is after the heading and nowhere else."""
    card = _card(points=_points(3))

    lines = card.splitlines()
    assert lines[0].startswith("【需要你决定】"), (
        "the card does not open with the decision heading:\n" + card
    )
    assert lines[1] == "", (
        "there is no blank line after the heading:\n" + card
    )
    assert "" not in lines[2:], (
        "the card has a blank line somewhere it should not:\n" + card
    )


def test_card_without_failed_points_says_so():
    """No failed point at all is said, not left blank."""
    card = _card(points=[])

    assert "（无失败点记录）" in card, (
        "the card does not say that no failed point was recorded:\n" + card
    )
    assert len(card.splitlines()) <= 12, (
        "the card is over the twelve line cap:\n" + card
    )


def test_card_names_the_decision_the_options_and_the_advice():
    """The decision card keeps the shape a human answers."""
    card = _card()

    assert "【需要你决定】p 第 3 轮验证终止：same_failure_repeated" in card, (
        "the heading does not name the decision:\n" + card
    )
    assert "失败点：" in card, (
        "the card does not have a failed points section:\n" + card
    )
    assert "VP-1 FAILED —— 登录接口 1：HTTP 500" in card, (
        "the failed point is not rendered as id, status, title and result:\n"
        + card
    )
    assert "选项：" in card, (
        "the card does not offer the options:\n" + card
    )
    assert "重跑本轮验证（rerun_this_round）" in card, (
        "the first option is missing:\n" + card
    )
    assert "修订判据（revise_criterion）" in card, (
        "the second option is missing:\n" + card
    )
    assert "建议：（本次没有生成分析，无推荐）" in card, (
        "the card does not say that there is no recommendation:\n" + card
    )
    assert (
        "完整分析：reports/terminal-decision-round-3-x.md（分析未生成：analysis_empty）"
        in card
    ), ("the card does not point at the report and the reason:\n" + card)
