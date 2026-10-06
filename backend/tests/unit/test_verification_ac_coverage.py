"""每条 PRD 验收标准都必须有 VP 声明要验。

2026-10-04 的一次计划里有 12 条验收标准、17 条计划内 VP。其中「FD 传递·多级」
那条在计划阶段**一条 VP 都没有** —— 它是执行期靠增量评估补出来的第 18 条。

也就是说测试设计阶段漏掉一整条验收标准，机制上完全不可见：报告照样
PASSED，因为 ``overall_status`` 只由 VP 决定，而漏掉的那条标准没有 VP 会
去判它。漏掉的那条恰好是本方案里最核心的安全断言之一（A→B→C 三级 fd 中继，
任一级环境变量里都不能有 secret）。

判据刻意保守：只认 ``【标签】`` 前缀的显式匹配。语义匹配自由文本必然误判，
而误判的方向决定了这个检查值不值得存在 —— 宁可漏报也不误报。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_ac_coverage import (  # noqa: E402
    GAP_FIELD,
    annotate_acceptance_gap,
    extract_criterion_labels,
    find_uncovered_criteria,
    render_gap_feedback,
)


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------


def test_label_is_taken_from_the_bracketed_prefix():
    labels = extract_criterion_labels([
        "【静态门禁】secret 键名不得出现在任何 os.environ 写入中。",
        "【CLI】secrets verify 逐项打印取值来源但不打印值。",
    ])
    assert [(i, l) for i, l, _ in labels] == [
        (1, "静态门禁"), (2, "CLI"),
    ]


def test_unlabelled_criteria_are_skipped_not_guessed_at():
    """No label means no reliable match, and a wrong match is a leak."""
    assert extract_criterion_labels([
        "凭据来自 .env，两个 secret 走独立钥匙串。",
    ]) == []


def test_single_character_label_is_below_the_threshold():
    assert extract_criterion_labels(["【密】短标签"]) == []


def test_dict_entries_are_read_through_their_text_keys():
    labels = extract_criterion_labels([
        {"text": "【授权体验】一次进程生命周期内最多取值一次。"},
    ])
    assert [l for _, l, _ in labels] == ["授权体验"]


def test_a_dict_with_no_readable_key_is_skipped():
    assert extract_criterion_labels([{"weight": 3}]) == []


def test_non_list_acceptance_yields_nothing():
    assert extract_criterion_labels(None) == []
    assert extract_criterion_labels("【范围】不是列表") == []


def test_index_is_one_based_and_matches_prd_positioning():
    labels = extract_criterion_labels([
        "无标签，跳过",
        "【范围】第二条",
    ])
    assert labels[0][0] == 2


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _vp(vid: str, claim: str) -> dict:
    return {
        "id": vid,
        "title": f"vp {vid}",
        "related_prd_criteria": claim,
    }


def test_criterion_named_by_a_vp_is_covered():
    plan = {"verification_points": [_vp("VP-001", "【静态门禁】键名门禁通过")]}
    acceptance = ["【静态门禁】键名门禁通过"]
    assert find_uncovered_criteria(plan, acceptance) == []


def test_the_incident_shape_is_reported():
    """Every criterion named except one — the 2026-10-04 plan."""
    plan = {"verification_points": [
        _vp("VP-001", "【回退一致】未设置开关时行为不变"),
        _vp("VP-002", "【跨平台】Linux 上无行为变化"),
    ]}
    acceptance = [
        "【回退一致】未设置开关时行为不变",
        "【跨平台】Linux 上无行为变化",
        "【FD 传递·多级】A→B→C 三级链路均能取到 secret",
    ]
    missing = find_uncovered_criteria(plan, acceptance)
    assert [m.label for m in missing] == ["FD 传递·多级"]
    assert missing[0].index == 3


def test_coverage_is_per_criterion_not_per_plan():
    """One VP citing two criteria covers both; it does not cover the rest."""
    plan = {"verification_points": [
        _vp("VP-001", "【范围】迁移名单；【文档】env.example 已改写"),
    ]}
    acceptance = ["【范围】迁移名单", "【文档】env.example 已改写", "【授权体验】"]
    assert [m.label for m in find_uncovered_criteria(plan, acceptance)] == [
        "授权体验",
    ]


def test_a_citation_of_a_different_criterion_does_not_cover_this_one():
    plan = {"verification_points": [_vp("VP-001", "【范围】迁移名单严格为两个 secret")]}
    acceptance = ["【范围】…", "【跨平台】Linux 与 Windows 不变"]
    assert [m.label for m in find_uncovered_criteria(plan, acceptance)] == [
        "跨平台",
    ]


def test_a_plan_with_no_points_reports_every_criterion():
    plan = {"verification_points": []}
    acceptance = ["【范围】a", "【跨平台】b"]
    assert len(find_uncovered_criteria(plan, acceptance)) == 2


def test_malformed_plans_do_not_raise():
    acceptance = ["【范围】a"]
    assert find_uncovered_criteria(None, acceptance) == []
    assert find_uncovered_criteria({}, acceptance) == []
    assert find_uncovered_criteria({"verification_points": "nope"}, acceptance) == []


def test_no_prd_acceptance_disables_the_check():
    """A plan without a PRD is reported elsewhere; this must not fail it."""
    plan = {"verification_points": []}
    assert find_uncovered_criteria(plan, []) == []
    assert find_uncovered_criteria(plan, None) == []


def test_vp_with_a_non_string_claim_is_ignored():
    """A claim the LLM emitted as a list covers nothing.

    Reading it as a string would be a silent widening of the match: the
    planner is free to shape the field, and a shape this check cannot
    read must count as "no claim", not "claim we guessed at".
    """
    plan = {"verification_points": [
        {"id": "VP-001", "related_prd_criteria": ["【范围】迁移名单"]},
    ]}
    acceptance = ["【范围】迁移名单", "【跨平台】Linux 与 Windows 不变"]
    assert [m.label for m in find_uncovered_criteria(plan, acceptance)] == [
        "范围", "跨平台",
    ]


# ---------------------------------------------------------------------------
# Annotation and feedback
# ---------------------------------------------------------------------------


def test_gap_is_recorded_where_an_operator_will_see_it():
    plan = {"verification_points": []}
    assert annotate_acceptance_gap(plan, find_uncovered_criteria(
        plan, ["【授权体验】一次进程内最多取值一次"],
    )) is True
    assert GAP_FIELD in plan
    entry = plan[GAP_FIELD][0]
    assert entry["label"] == "授权体验"
    assert entry["index"] == 1


def test_nothing_is_annotated_when_nothing_is_missing():
    plan = {"verification_points": []}
    assert annotate_acceptance_gap(plan, []) is False
    assert GAP_FIELD not in plan


def test_feedback_names_each_missing_criterion():
    plan = {"verification_points": []}
    missing = find_uncovered_criteria(plan, ["【授权体验】x", "【跨平台】y"])
    text = render_gap_feedback(missing)
    assert "授权体验" in text
    assert "跨平台" in text
    assert "verification_point" in text


def test_feedback_is_empty_when_there_is_no_gap():
    assert render_gap_feedback([]) == ""


# ---------------------------------------------------------------------------
# Against the plan that actually had the gap
# ---------------------------------------------------------------------------


def _real_plan_dir() -> Path | None:
    """The 2026-10-04 plan, when this checkout sits next to it."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "product-development-team" / "plans"
        if candidate.is_dir():
            for plan_dir in sorted(candidate.iterdir()):
                prd = plan_dir / "prd.json"
                vplan = plan_dir / "verification_plan.json"
                if not (prd.is_file() and vplan.is_file()):
                    continue
                try:
                    acceptance = json.loads(
                        prd.read_text(encoding="utf-8"),
                    ).get("acceptance")
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(acceptance, list) and any(
                    isinstance(a, str) and a.startswith("【FD 传递·多级】")
                    for a in acceptance
                ):
                    return plan_dir
    return None


def test_the_2026_10_04_gap_is_found_without_hand_analysis():
    """The check must rediscover AC-04 from the files alone."""
    plan_dir = _real_plan_dir()
    if plan_dir is None:
        import pytest
        pytest.skip("the 2026-10-04 plan is not reachable from this checkout")

    prd = json.loads((plan_dir / "prd.json").read_text(encoding="utf-8"))
    plan = json.loads(
        (plan_dir / "verification_plan.json").read_text(encoding="utf-8"),
    )

    labels = extract_criterion_labels(prd["acceptance"])
    assert len(labels) == len(prd["acceptance"]), (
        "every criterion in that PRD carries an explicit label"
    )

    missing = find_uncovered_criteria(plan, prd["acceptance"])
    assert [m.label for m in missing] == ["FD 传递·多级"], (
        "the multi-level FD relay was the criterion with no planned VP"
    )
