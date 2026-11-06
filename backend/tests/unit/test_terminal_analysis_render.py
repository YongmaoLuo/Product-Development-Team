"""Unit tests for the terminal-round analysis rendering (``verification.terminal_analysis``).

The judge (see ``test_terminal_analysis_judge.py``) produces a six-field
dict — ``context``, ``problem``, ``evidence``, ``recommendation``,
``recommendation_reason``, ``uncertainty``.  The human who answers the
terminal question does not read a dict; they read a question.  This
module covers the pure function that turns the dict into that question:
five C/P/E/A/U sections, bounded.

What is pinned here
-------------------

1. **The rendering has the five C/P/E/A/U sections**, and the evidence
   the judge produced is carried verbatim — a section that paraphrases
   the evidence is not evidence.
2. **The question is bounded.**  ``TERMINAL_QUESTION_MAX_CHARS`` is the
   cap, and a rendered question never exceeds it, however long the
   evidence is.
3. **The truncation order is the argument order.**  Evidence is dropped
   from its **tail** first (the front of the argument is what the
   recommendation rests on), the whole "不确定性" section is dropped
   only when dropping evidence was not enough, and a hard cut is the last
   resort.
4. **An invalid analysis renders as nothing.**  A missing field, an
   illegal recommendation, a non-mapping — all of them produce ``""``,
   and the caller's fallback is the factual question it asked before.

The module under test is imported inside the test bodies on purpose: a
module that does not exist (or does not export the contract) then shows
up as a test FAILURE carrying the real reason, instead of a collection
ERROR that hides which contract broke.
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


def _analysis(**overrides):
    """A six-field analysis, with overrides."""
    analysis = {
        "context": "第 3 轮终局停止",
        "problem": "链路无法自愈",
        "evidence": ["VP-3 连续 3 轮失败"],
        "recommendation": "accept_as_failed",
        "recommendation_reason": "同一失败重复 3 轮",
        "uncertainty": "无法判断",
    }
    analysis.update(overrides)
    return analysis


# ---------------------------------------------------------------------------
# 1. The five C/P/E/A/U sections
# ---------------------------------------------------------------------------


def test_render_has_five_cpea_sections():
    """The five section markers are there, and the evidence is carried."""
    module = _terminal_analysis()

    result = module.render_analysis(_analysis())

    for marker in (
        "## 背景（C）",
        "## 问题（P）",
        "## 证据（E）",
        "## 建议（A）",
        "## 不确定（U）",
    ):
        assert marker in result, f"the section marker {marker} is missing"

    assert "第 3 轮终局停止" in result, "the context is missing"
    assert "链路无法自愈" in result, "the problem is missing"
    assert "VP-3 连续 3 轮失败" in result, "the evidence is missing"
    assert "accept_as_failed" in result, "the recommendation is missing"
    assert "同一失败重复 3 轮" in result, "the recommendation reason is missing"
    assert "无法判断" in result, "the uncertainty is missing"


# ---------------------------------------------------------------------------
# 2. The cap
# ---------------------------------------------------------------------------


def test_constant_is_2000():
    """The question's cap is 2000 characters."""
    module = _terminal_analysis()

    assert module.TERMINAL_QUESTION_MAX_CHARS == 2000


def test_render_limits_to_default_2000():
    """Sixty long evidence entries still render within the default cap."""
    module = _terminal_analysis()

    analysis = _analysis(
        evidence=[
            f"证据 {index}：VP-{index} 连续 3 轮失败，"
            "failure_class=deliverable_defect，"
            "verdict 为 AssertionError: expected 200, got 500，"
            "未见环境偶发特征，判定为交付物缺陷。"
            for index in range(60)
        ]
    )

    result = module.render_analysis(analysis)

    assert result, "a long analysis rendered as nothing"
    assert len(result) <= 2000, (
        f"the rendered question is {len(result)} characters, over the cap"
    )


# ---------------------------------------------------------------------------
# 3. The truncation order
# ---------------------------------------------------------------------------


def test_render_drops_evidence_tail_before_uncertainty():
    """Evidence is dropped from its tail first; the U section survives."""
    module = _terminal_analysis()

    analysis = _analysis(
        evidence=[
            "证据一：VP-3「登录接口」连续 3 轮失败，failure_class="
            "deliverable_defect，verdict 为 AssertionError: expected 200, "
            "got 500，未见环境偶发特征，判定为交付物缺陷。",
            "证据二：本轮自动修复任务生成数为 0，失败点没有收敛的路径，"
            "继续重跑本轮的预期收益很低，缺陷形态在同一位置反复出现。",
            "证据三：这一条证据应当被丢弃，它出现在证据列表的末尾，"
            "长度足够长以便触发上限，并且它的编号是 3；它还说明修复任务"
            "没有落到具体文件上，继续重跑之前必须先由人安排修复动作，"
            "否则同一位置会再次失败；另外日志里没有出现任何指向外部依赖"
            "抖动的迹象，这条证据本身也支持「不是偶发」的判断。",
        ]
    )

    unbounded = module.render_analysis(analysis)
    assert len(unbounded) > 400, (
        "the test data is not long enough to trigger the limit"
    )

    result = module.render_analysis(analysis, limit=400)

    assert len(result) <= 400, (
        f"the rendered question is {len(result)} characters, over the limit"
    )
    assert "## 不确定（U）" in result, (
        "the uncertainty section was dropped before the evidence tail was"
    )
    assert "证据三" not in result, "the last evidence entry survived"
    assert "证据一" in result, "the first evidence entry was dropped"


# ---------------------------------------------------------------------------
# 4. An invalid analysis renders as nothing
# ---------------------------------------------------------------------------


def test_render_returns_empty_for_invalid_analysis():
    """No mapping, a missing field, an illegal recommendation → ``""``."""
    module = _terminal_analysis()

    assert module.render_analysis(None) == "", "None is not an analysis"

    missing = _analysis()
    del missing["uncertainty"]
    assert module.render_analysis(missing) == "", "a missing field rendered"

    illegal = _analysis(recommendation="do_something_else")
    assert module.render_analysis(illegal) == "", (
        "an illegal recommendation rendered"
    )

    assert module.render_analysis("not an analysis") == "", (
        "a string is not an analysis"
    )
    assert module.render_analysis(["not", "an", "analysis"]) == "", (
        "a list is not an analysis"
    )