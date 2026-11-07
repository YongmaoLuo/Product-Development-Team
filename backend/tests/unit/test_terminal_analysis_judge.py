"""Unit tests for the terminal-round analysis prompt (``verification.terminal_analysis``).

The terminal branch of the verification loop stops and asks a human how to
proceed.  The question it asks carries one English sentence of facts; this
module is the first half of the answer to "who should be asked, and on
what basis" — the half that renders the failure scene into an analysis
prompt.

What is pinned here
-------------------

1. **The prompt names every failed point.**  A judge that is asked to
   reason about a round must be shown which points failed and what they
   were called — the ids and titles, not a count.
2. **The prompt carries the failure facts.**  The failure class, how many
   rounds the point has now failed, how many repair tasks this round
   produced and how many points were deferred are the numbers the
   recommendation is read against.
3. **The prompt is bounded.**  A point's verdict can be a stack trace
   thousands of characters long; it is cut, and the cut is marked.  More
   than ten failed points are cut too, and the count of the ones left out
   is stated — a judge that silently sees ten of twelve failures does not
   know it is missing two.
4. **The actions are spelled out.**  The judge is told which
   answers exist and what each one means, in the language the human who
   reads the result will read.
5. **The recommendation vocabulary is the orchestrator's.**  The module
   deliberately does not import the orchestrator (the orchestrator will
   import *this* module, so an import the other way would be a cycle) —
   which means the two lists of recommendations can drift apart unless
   something pins them together.  This is that something.
6. **The prompt tells the judge where to look and what to answer**
   (2026-11-06).  The judge now investigates on its own, so the prompt
   carries ``plan_dir`` / ``project_dir`` and the files under them that
   hold the facts the summary does not — the root-cause chain record,
   the root-cause reports, the repair tasks.  It also demands the three
   things the summary alone cannot give: a per-point 预期 / 实际 /
   为何失败, why the framework could not iterate on its own, and an
   actionable next step.  「无法确定」 is forbidden: the prompt asks for
   the missing fact and where to read it instead.

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


def _judge():
    """A judge with a stubbed tool.

    The tool is never called by these tests — they exercise the prompt
    half only — but handing one in keeps the judge from reaching for a
    real :class:`coding_tool.ClaudeCodingTool`.
    """
    return _terminal_analysis().TerminalDecisionJudge(coding_tool=object())


def _build(judge, failed_points, **overrides):
    """Build a prompt with sensible defaults for the round-level facts."""
    kwargs = {
        "plan_id": "p1",
        "round_number": 3,
        "stop_reason": "repair_execution_failed",
        "repair_task_count": 0,
        "deferred_count": 4,
        "failed_points": failed_points,
        "plan_dir": "/tmp/plans/p1",
        "project_dir": "/tmp/projects/demo",
    }
    kwargs.update(overrides)
    return judge.build_prompt(**kwargs)


def _point(**overrides):
    """One failed verification point, with overrides."""
    point = {
        "id": "VP-3",
        "title": "登录接口",
        "expected_result": "返回 200",
        "failure_class": "deliverable_defect",
        "consecutive_rounds": 3,
        "verdict": "AssertionError: expected 200, got 500",
    }
    point.update(overrides)
    return point


# ---------------------------------------------------------------------------
# 1. Every failed point is named
# ---------------------------------------------------------------------------


def test_build_prompt_names_each_failed_point():
    """Two failed points: both ids and both titles are in the prompt."""
    prompt = _build(
        _judge(),
        [
            _point(id="VP-3", title="登录接口"),
            _point(id="VP-7", title="导出报表"),
        ],
    )

    assert "VP-3" in prompt, "the first failed point's id is missing"
    assert "登录接口" in prompt, "the first failed point's title is missing"
    assert "VP-7" in prompt, "the second failed point's id is missing"
    assert "导出报表" in prompt, "the second failed point's title is missing"


# ---------------------------------------------------------------------------
# 2. The failure facts are carried
# ---------------------------------------------------------------------------


def test_build_prompt_carries_failure_facts():
    """The facts a recommendation is read against are all in the prompt."""
    prompt = _build(_judge(), [_point()])

    assert "failure_class" in prompt, "the failure class field is missing"
    assert "deliverable_defect" in prompt, "the failure class value is missing"
    assert "consecutive_rounds: 3" in prompt, (
        "how many rounds the point has failed is missing"
    )
    assert "repair_task_count" in prompt, "the repair task count is missing"
    assert "repair tasks generated this round: 0" in prompt, (
        "the repair task count value is missing"
    )
    assert "deferred_count" in prompt, "the deferred count is missing"
    assert "deferred / not-executed points: 4" in prompt, (
        "the deferred count value is missing"
    )


# ---------------------------------------------------------------------------
# 3. The prompt is bounded
# ---------------------------------------------------------------------------


def test_build_prompt_truncates_long_verdict():
    """A thousand-character verdict is cut, and the cut is marked."""
    verdict = "A" * 900 + "TAIL_OF_THE_VERDICT"
    prompt = _build(_judge(), [_point(verdict=verdict)])

    assert "…" in prompt, "the truncation is not marked"
    assert "TAIL_OF_THE_VERDICT" not in prompt, (
        "the tail of a truncated verdict is still in the prompt"
    )


def test_build_prompt_truncates_long_expected_result():
    """A long expected result is cut at its own, shorter limit."""
    expected = "B" * 400 + "TAIL_OF_THE_EXPECTED_RESULT"
    prompt = _build(_judge(), [_point(expected_result=expected)])

    assert "…" in prompt, "the truncation is not marked"
    assert "TAIL_OF_THE_EXPECTED_RESULT" not in prompt, (
        "the tail of a truncated expected result is still in the prompt"
    )


def test_build_prompt_caps_points_at_ten():
    """Twelve failed points: ten are rendered, two are counted out loud."""
    points = [_point(id=f"VP-{index}") for index in range(1, 13)]
    prompt = _build(_judge(), points)

    assert "2 more failed point(s) not shown" in prompt, (
        "the points left out are not counted"
    )
    assert "VP-10" in prompt, "the tenth point should still be rendered"
    assert "VP-11" not in prompt, "the eleventh point should have been cut"


def test_build_prompt_states_when_no_points_failed():
    """An empty list of failed points is stated, not left blank."""
    prompt = _build(_judge(), [])

    assert "(no failed points recorded)" in prompt


def test_build_prompt_tolerates_missing_and_none_fields():
    """A point with missing or None fields renders empty and raises nothing."""
    prompt = _build(
        _judge(),
        [
            {
                "id": "VP-3",
                "title": None,
                "expected_result": None,
                "failure_class": None,
                "consecutive_rounds": None,
                "verdict": None,
            },
            {},
        ],
    )

    assert "VP-3" in prompt, "the id that IS present should still render"
    assert "None" not in prompt, "a missing field rendered as the word None"


# ---------------------------------------------------------------------------
# 4. The actions are spelled out
# ---------------------------------------------------------------------------


def test_build_prompt_lists_both_actions():
    """Every recommendation id is offered, with its Chinese meaning."""
    module = _terminal_analysis()
    prompt = _build(_judge(), [_point()])

    assert module.RECOMMENDATIONS == (
        "rerun_this_round",
        "revise_criterion",
    )
    for recommendation in module.RECOMMENDATIONS:
        assert recommendation in prompt, (
            f"the prompt does not offer {recommendation!r}"
        )
    assert "重跑本轮" in prompt, "rerun_this_round is not explained in Chinese"
    assert "修订判据" in prompt, "revise_criterion is not explained in Chinese"


def test_system_prompt_states_its_rules():
    """The system prompt says: read-only, no invention, two choices, JSON."""
    system_prompt = _judge().build_system_prompt()

    assert "READ ONLY" in system_prompt, "the read-only rule is not stated"
    assert "不要修改任何文件" in system_prompt, (
        "the no-writing rule is not stated"
    )
    assert "DO NOT FABRICATE" in system_prompt, (
        "the no-invention rule is not stated"
    )
    assert "NO \"无法确定\"" in system_prompt, (
        "the no-「无法确定」 rule is not stated"
    )
    assert "recommendation" in system_prompt, (
        "the recommendation field is not named"
    )
    for recommendation in _terminal_analysis().RECOMMENDATIONS:
        assert recommendation in system_prompt, (
            f"the system prompt does not name {recommendation!r}"
        )
    assert "中文" in system_prompt, "the all-Chinese rule is not stated"
    assert "JSON" in system_prompt, "the JSON-only rule is not stated"


# ---------------------------------------------------------------------------
# 6. The judge is told where to look and what to answer
# ---------------------------------------------------------------------------


def test_build_prompt_carries_the_investigation_paths():
    """The prompt names the plan's directory and the project's."""
    prompt = _build(_judge(), [_point()])

    assert "/tmp/plans/p1" in prompt, "the plan directory is missing"
    assert "/tmp/projects/demo" in prompt, "the project directory is missing"
    assert "plan_dir: /tmp/plans/p1" in prompt, (
        "the plan directory is not labelled"
    )
    assert "project_dir: /tmp/projects/demo" in prompt, (
        "the project directory is not labelled"
    )


def test_build_prompt_names_the_root_cause_chain_file():
    """The prompt says which file holds the root-cause chain."""
    prompt = _build(_judge(), [_point()])

    assert "/tmp/plans/p1/verification_root_cause_chain.json" in prompt, (
        "the root-cause chain file is not named"
    )
    assert "/tmp/plans/p1/reports/root-cause-<vp_id>.md" in prompt, (
        "the root-cause report is not named"
    )
    assert "/tmp/plans/p1/verification_repair_tasks.json" in prompt, (
        "the repair-task record is not named"
    )
    for field in ("implementation_round", "blocked_round", "blocked_reason"):
        assert field in prompt, (
            f"the prompt does not tell the judge about {field}"
        )


def test_build_prompt_demands_a_per_vp_verdict():
    """The prompt asks for 预期 / 实际 / 为何失败, per failed point."""
    prompt = _build(_judge(), [_point()])

    assert "预期:" in prompt, "the expected-result line is not asked for"
    assert "实际:" in prompt, "the actual-result line is not asked for"
    assert "为何失败:" in prompt, "the why-it-failed line is not asked for"
    assert "id — title" in prompt, (
        "the per-point heading format is not spelled out"
    )


def test_build_prompt_demands_why_the_framework_cannot_iterate():
    """The prompt asks why the loop could not fix it on its own."""
    prompt = _build(_judge(), [_point()])

    assert "框架为什么不能自动迭代" in prompt, (
        "the framework's own limit is not asked for"
    )
    assert "BLOCKED_ON_USER" in prompt, (
        "the blocked stage is not named"
    )
    assert "repair_task_count=0" in prompt, (
        "the empty repair-task table is not asked about"
    )


def test_build_prompt_forbids_无法确定():
    """The prompt says what to write instead of 「无法确定」."""
    prompt = _build(_judge(), [_point()])

    assert "禁止「无法确定」" in prompt, (
        "the ban on 「无法确定」 is not stated"
    )
    assert "缺哪一条具体事实" in prompt, (
        "the prompt does not say what to write instead"
    )


def test_build_prompt_carries_each_points_expected_and_actual():
    """Every failed point's expected result and verdict are in the prompt."""
    prompt = _build(
        _judge(),
        [
            _point(
                id="VP-3",
                title="登录接口",
                expected_result="返回 200 且 body 带 token",
                verdict="AssertionError: expected 200, got 500",
            ),
            _point(
                id="VP-7",
                title="导出报表",
                expected_result="生成 xlsx 文件",
                verdict="FileNotFoundError: no such file",
            ),
        ],
    )

    for point_id in ("VP-3", "VP-7"):
        assert point_id in prompt, f"{point_id} is missing"
    assert "返回 200 且 body 带 token" in prompt, (
        "the first point's expected_result is missing"
    )
    assert "AssertionError: expected 200, got 500" in prompt, (
        "the first point's verdict is missing"
    )
    assert "生成 xlsx 文件" in prompt, (
        "the second point's expected_result is missing"
    )
    assert "FileNotFoundError: no such file" in prompt, (
        "the second point's verdict is missing"
    )



# ---------------------------------------------------------------------------
# 5. The vocabulary matches the orchestrator's
# ---------------------------------------------------------------------------


def test_recommendations_match_terminal_options():
    """The module's recommendations are the orchestrator's terminal options."""
    from verification.orchestrator import TERMINAL_OPTIONS

    assert _terminal_analysis().RECOMMENDATIONS == TERMINAL_OPTIONS
