"""Unit tests for the terminal-round analysis rendering (``verification.terminal_analysis``).

The judge (see ``test_terminal_analysis_judge.py``) produces a six-field
dict — ``context``, ``problem``, ``evidence``, ``recommendation``,
``recommendation_reason``, ``uncertainty``.  The human who answers the
terminal question does not read a dict; they read a question.  This
module covers the pure function that turns the dict into that question.

There are **two** renderings, and they answer two different questions:

* ``render_analysis`` writes the **report** — five C/P/E/A/U sections,
  bounded, carrying the whole argument.  It is what the round's
  archive is made of.
* ``render_brief`` writes the **card** — what has to be decided, the
  options, the advice and the path of the report.  It is what the
  human is actually asked, and it is bounded far tighter.

What is pinned here
-------------------

1. **The report has the five C/P/E/A/U sections**, and the evidence
   the judge produced is carried verbatim — a section that paraphrases
   the evidence is not evidence.
2. **The report is bounded.**  ``TERMINAL_QUESTION_MAX_CHARS`` is the
   cap, and a rendered report never exceeds it, however long the
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

# The production path: the terminal branch's question is built by
# ``VerificationOrchestrator._terminal_decision_question``, which renders
# the judge's analysis through ``render_brief``.  The orchestrator
# fixture is borrowed from the wiring test, the same way
# ``tests/unit/test_terminal_decision_analysis_question.py`` borrows it.
from tests.unit.test_terminal_decision_question import (  # noqa: F401
    analysis_orchestrator,
)


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
        "recommendation": "revise_criterion",
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
    assert "revise_criterion" in result, "the recommendation is missing"
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


# ---------------------------------------------------------------------------
# 5. The advice survives every cut (2026-11-07)
# ---------------------------------------------------------------------------
#
# The judge answered with 12,092 characters on the 2026-11-07 round: a
# 4,735-character 问题 section (per verification point: the expectation,
# the actual, and why it did not hold) and a 581-character 背景 section.
# The truncation of that day cut the *whole* rendered text in the middle
# of the 问题 section — and the 建议 section is rendered *after* it, so
# the advice never reached the human.  The question was about a failure
# and carried no advice to act on.
#
# The data below is rebuilt from that shape rather than read from a file
# on disk: the regression must hold on a machine that has never seen
# that plan.


_VP_TITLES = (
    "编译门禁",
    "单元测试门禁",
    "整仓门禁",
    "E2E 冒烟",
    "接口契约校验",
    "数据库迁移演练",
    "依赖许可证扫描",
    "静态类型检查",
    "代码覆盖率下限",
    "打包产物一致性",
    "镜像体积回归",
    "配置漂移检测",
    "日志格式校验",
    "链路记录完整性",
    "根因报告落盘",
    "修复任务生成",
    "任务幂等性",
    "并发写冲突",
    "失败点收敛判定",
    "终局问句可读性",
)


def _pad_to(text: str, target: int) -> str:
    """``text`` repeated until it is about ``target`` characters long."""
    parts: list = []
    total = 0
    while total < target:
        parts.append(text)
        total += len(text) + 1
    return "\n".join(parts)[:target].rstrip()


def _vp_block(index: int) -> str:
    """One verification point, narrated the way the judge narrated it."""
    title = _VP_TITLES[index % len(_VP_TITLES)]
    return (
        f"VP-{index + 1:03d}「{title}」本轮失败："
        f"预期 {title} 在干净检出上可复现通过，实际在 {index + 1} 号工作区"
        f"上以 AssertionError 结束，期望值与实际值相差 {index + 2}；"
        f"判据来自 {title} 的验收条款，失败分类为 deliverable_defect；"
        f"未见环境偶发特征，判定为交付物缺陷而非偶发波动；"
        f"该失败点在连续 {index % 5 + 1} 轮中重复出现，"
        f"而本轮自动修复任务生成数为 0，修复没有落到具体文件上；"
        f"人工核对日志后确认，该失败点在本轮的每一次重跑中都以同样的"
        f"断言结束，位置与信息完全一致。"
    )


def _long_analysis():
    """An analysis shaped like the one that lost its advice.

    The shapes are the ones of 2026-11-07: a 问题 section of about
    4,700 characters (one narrative per verification point), a 背景
    section of about 580, four evidence entries, and an 不确定 section
    of about 1,500.
    """
    problem = "\n".join(_vp_block(index) for index in range(20))
    problem = _pad_to(problem, 4700)
    context = _pad_to(
        "plan 2026-11-02 的验证轮于 2026-11-07 13:03:28 起跑、13:25:32 出报告；"
        "21 个验证点里 16 个 PASSED，其余失败点集中在依赖外部服务可达性的"
        "契约校验与整仓门禁上；工作区在跑测前是干净的，HEAD 与被测提交一致；"
        "本轮没有产生任何修复任务，失败点没有出现任何收敛迹象；"
        "人工复核确认失败点在同一位置重复出现，且重跑未改变任何外部条件。",
        580,
    )
    evidence = [
        _pad_to(
            f"证据{index + 1}：{_vp_block(index)}链路上没有任何一次重跑改变了结果，"
            f"日志里也没有出现指向外部依赖抖动的迹象；"
            f"同一位置、同一断言、同一工作区，前后三次运行完全一致。",
            1120,
        )
        for index in range(4)
    ]
    uncertainty = _pad_to(
        "无法确认外部依赖的可用性是否在两次报告之间发生过变化；"
        "没有拿到失败发生时刻的服务端日志，因此不能排除"
        "「响应变慢但未超时」这一情形；"
        "也无法确认当前 HEAD 上的失败是否与上一轮被作废的失败点同源；"
        "本判断只覆盖已记录的验证轮，"
        "更早的轮次里同样的判据是否失败过并未查证。",
        1500,
    )
    return _analysis(
        context=context,
        problem=problem,
        evidence=evidence,
        recommendation="rerun_this_round",
        recommendation_reason=(
            "两个失败点的判据都是明确且可执行的，"
            "而且框架自己的分诊已经裁定它们是 deliverable_defect、"
            "判据本身没问题、必须重做而不是重试或改判据；"
            "重跑本轮是唯一一条不需要人先补新判据就能走通的路。"
        ),
        uncertainty=uncertainty,
    )


def test_render_keeps_the_advice_when_the_problem_is_huge():
    """The 建议 section survives the truncation of a 12,092-char analysis."""
    module = _terminal_analysis()

    analysis = _long_analysis()
    result = module.render_analysis(analysis)

    assert result, "the long analysis rendered as nothing"
    assert len(result) <= 2000, (
        f"the rendered question is {len(result)} characters, over the cap"
    )

    # The advice is there, whole.
    assert "## 建议（A）" in result, (
        "the advice section did not survive the truncation"
    )
    assert analysis["recommendation"] in result, "the recommendation is missing"
    assert analysis["recommendation_reason"] in result, (
        "the recommendation reason is missing"
    )

    # The cut happened inside the 问题 section: the evidence and the
    # uncertainty are gone, and the problem's head is kept.
    assert "## 证据（E）" not in result, "the evidence section survived the cut"
    assert "## 不确定（U）" not in result, "the uncertainty section survived the cut"
    assert analysis["uncertainty"][:20] not in result, (
        "half an uncertainty is in the result"
    )

    problem_section = result[
        result.index("## 问题（P）") : result.index("## 建议（A）")
    ]
    assert problem_section.startswith("## 问题（P）\n")
    assert problem_section.rstrip().endswith("…"), (
        "the 问题 section was not cut with a marked cut"
    )
    assert result.count("…") == 1, (
        "there is a cut somewhere other than the 问题 section"
    )

    # The order of the sections is unchanged: 背景 / 问题 / 建议.
    assert result.index("## 背景（C）") < result.index("## 问题（P）")
    assert result.index("## 问题（P）") < result.index("## 建议（A）")


def test_render_keeps_the_advice_with_twenty_failed_points():
    """Twenty failed verification points, and the advice is still there."""
    module = _terminal_analysis()

    analysis = _long_analysis()
    analysis["evidence"] = [
        f"证据{index + 1}：{_vp_block(index)}" for index in range(20)
    ]

    result = module.render_analysis(analysis)

    assert len(result) <= 2000, (
        f"the rendered question is {len(result)} characters, over the cap"
    )
    assert "## 建议（A）" in result
    assert analysis["recommendation"] in result
    assert analysis["recommendation_reason"] in result


@pytest.mark.parametrize("limit", [200, 400, 800, 1200, 1600, 2000])
def test_render_keeps_the_advice_at_every_limit(limit):
    """However tight the cap is, the advice section is in the result."""
    module = _terminal_analysis()

    result = module.render_analysis(_long_analysis(), limit=limit)

    assert len(result) <= limit, (
        f"the rendered question is {len(result)} characters, over the limit"
    )
    assert "## 建议（A）" in result, (
        f"the advice section did not survive a limit of {limit}"
    )
    assert "rerun_this_round" in result, "the recommendation id is missing"


# ---------------------------------------------------------------------------
# 6. The golden: a short analysis renders byte for byte as it did
# ---------------------------------------------------------------------------


def test_render_short_analysis_is_byte_identical():
    """A short analysis renders exactly as it rendered before the cut."""
    module = _terminal_analysis()

    result = module.render_analysis(_analysis())

    assert result == (
        "## 背景（C）\n第 3 轮终局停止\n\n"
        "## 问题（P）\n链路无法自愈\n\n"
        "## 证据（E）\n- VP-3 连续 3 轮失败\n\n"
        "## 建议（A）\nrevise_criterion —— 同一失败重复 3 轮\n\n"
        "## 不确定（U）\n无法判断"
    )


def test_render_multi_line_analysis_is_byte_identical():
    """Blank lines are removed from a body; the rest is carried verbatim."""
    module = _terminal_analysis()

    analysis = _analysis(
        context="背景第一行\n\n背景第二行  ",
        problem="问题第一行\n问题第二行\n\n问题第四行",
        evidence=["证据一", "证据二"],
        recommendation="rerun_this_round",
        recommendation_reason="理由一行\n理由二行",
        uncertainty="不确定一\n\n不确定二",
    )

    result = module.render_analysis(analysis)

    assert result == (
        "## 背景（C）\n背景第一行\n背景第二行\n\n"
        "## 问题（P）\n问题第一行\n问题第二行\n问题第四行\n\n"
        "## 证据（E）\n- 证据一\n- 证据二\n\n"
        "## 建议（A）\nrerun_this_round —— 理由一行\n理由二行\n\n"
        "## 不确定（U）\n不确定一\n不确定二"
    )


# ---------------------------------------------------------------------------
# 7. The production path: the question the terminal branch asks
# ---------------------------------------------------------------------------


def test_terminal_question_on_the_production_path_keeps_the_advice(
    analysis_orchestrator,
):
    """The question the terminal branch asks carries the advice.

    ``VerificationOrchestrator._terminal_decision_question`` is the
    production entry: it renders the judge's analysis through
    ``render_brief`` and hands the result to the human as the
    question.  The question is a **card** now — a person answers it, so
    it is short — and the advice is the one thing on it that must never
    be cut away.
    """
    analysis = _long_analysis()
    analysis_orchestrator._terminal_judge.result = analysis

    payload = analysis_orchestrator._terminal_decision_question(
        {"verification_results": [{"id": "VP-001", "status": "FAILED"}]},
        2,
        ["VP-001"],
        0,
    )

    question = payload["question"]
    assert len(question) <= 600, (
        f"the card is {len(question)} characters, over the cap"
    )
    assert len(question.splitlines()) <= 8, (
        f"the card is {len(question.splitlines())} lines, over the cap"
    )
    advice_lines = [
        line for line in question.splitlines() if line.startswith("建议：")
    ]
    assert advice_lines, (
        "the card does not carry an advice line — the human is asked to "
        "choose without being told which choice is recommended"
    )
    assert analysis["recommendation"] in advice_lines[0], (
        "the recommendation id is not on the advice line"
    )
    assert "## 建议（A）" not in question, (
        "the card is still the report — the question is a card now"
    )
