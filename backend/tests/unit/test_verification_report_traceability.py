"""验证报告的**回溯链**检查（:func:`traceability_issues`）的契约测试。

背景
----
报告里的每个验证点都必须能回溯回去：

    报告 VP → 机器计划（``verification_plan.json``）→ 计划书条目

链断在哪一环，报告就不可信 —— 读者没法确认「这条结论对应的是哪条判据」。
两个方向都会毁掉这条链：

  * **凭空多出来的 VP** —— 报告里出现一个计划里没有的 id，读者无从查证；
  * **delta VP 伪装成已审核条目** —— 增量新增的验证点**不在**已审核的计划书
    里，它必须自报家门（``origin="delta"`` + ``added_reason``），否则一条没人
    审过的判据会以「已审核条目」的样子出现在报告里。

被跳过的验证点同样要能被查证：报告里写了「跳过」，就必须出现在
``skipped_items[]`` 里 —— 否则「跳过」和「没跑过」在报告里长得一模一样。

这些检查是**纯函数**：同一份入参两次调用结果相同，入参顺序打乱不改变结果。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_report_md import render, traceability_issues  # noqa: E402


@dataclass
class _Item:
    """计划书里的一个条目（鸭子类型：鸭子只需要有 ``id``）。"""

    id: str


@dataclass
class _Parsed:
    """计划书的解析产物（鸭子类型：有 ``items``）。"""

    items: List[_Item] = field(default_factory=list)


def _parsed(*ids: str) -> _Parsed:
    """一份计划书的解析产物，条目 id 是给定的那些。"""
    return _Parsed(items=[_Item(vp_id) for vp_id in ids])


_PLAN_JSON = {
    "verification_points": [
        {"id": "VP-001", "title": "登录接口"},
        {"id": "VP-002", "title": "退出接口"},
    ]
}


def _issues(report: Any, plan_json: Any = None, parsed: Any = None) -> List[str]:
    """调用 :func:`traceability_issues`（缺省参数给一份能回溯的机器计划）。"""
    if plan_json is None:
        plan_json = _PLAN_JSON
    if parsed is None:
        parsed = _parsed("VP-001", "VP-002")
    return traceability_issues(report, plan_json, parsed)


# ---------------------------------------------------------------------------
# 回溯链：报告 VP → 机器计划 VP → 计划书条目
# ---------------------------------------------------------------------------


def test_unknown_vp_id_flagged():
    """报告里有计划外的 id → 两处回溯都断，问题必须报出来。"""
    report = {"verification_points": [{"id": "VP-999", "plan_item_id": "VP-999"}]}

    issues = _issues(report, _PLAN_JSON, _parsed("VP-001", "VP-002"))

    assert "VP-999 not in verification_plan.json" in issues
    assert "VP-999 not in plan document" in issues


def test_traced_vp_has_no_issue():
    """能一路回溯到计划书条目的 VP 不报任何问题。"""
    report = {"verification_points": [{"id": "VP-001", "status": "passed"}]}

    assert _issues(report, _PLAN_JSON, _parsed("VP-001", "VP-002")) == []


def test_empty_report_has_no_issue():
    """空报告 → 空问题列表。"""
    assert traceability_issues({}, _PLAN_JSON, _parsed("VP-001")) == []
    assert traceability_issues({"verification_points": []}, _PLAN_JSON, _parsed()) == []


def test_vp_missing_in_machine_plan_only():
    """机器计划里有、计划书里没有 → 只报计划书这一处。"""
    report = {"verification_points": [{"id": "VP-001"}]}

    issues = _issues(report, _PLAN_JSON, _parsed("VP-002"))

    assert issues == ["VP-001 not in plan document"]


# ---------------------------------------------------------------------------
# delta VP：不得伪装成已审核条目
# ---------------------------------------------------------------------------


def test_delta_vp_requires_origin_and_reason():
    """delta VP 缺 ``origin="delta"`` 或 ``added_reason`` → 报问题。"""
    report = {
        "verification_points": [
            {"id": "VP-010", "origin": "delta"},  # 缺 added_reason
        ]
    }

    issues = _issues(report)

    assert any("VP-010" in issue and "added_reason" in issue for issue in issues)


def test_delta_vp_requires_origin():
    """只写 ``added_reason`` 而没写 ``origin="delta"`` → 报问题。"""
    report = {
        "verification_points": [
            {"id": "VP-010", "added_reason": "repair-r1-02 引入"},
        ]
    }

    issues = _issues(report)

    assert any("VP-010" in issue and "origin" in issue for issue in issues)


def test_delta_vp_with_markers_has_no_issue():
    """标记齐全的 delta VP 不报问题（它本来就不在计划书里）。"""
    report = {
        "verification_points": [
            {
                "id": "VP-010",
                "origin": "delta",
                "added_reason": "repair-r1-02 引入",
                "status": "passed",
            }
        ]
    }

    assert _issues(report, _PLAN_JSON, _parsed("VP-001", "VP-002")) == []


def test_delta_vp_detected_from_machine_plan():
    """机器计划里带 ``added_reason`` 的条目 → 报告里必须自报 delta。"""
    plan_json = {
        "verification_points": [
            {"id": "VP-001"},
            {"id": "VP-010", "added_reason": "repair-r1-02 引入"},
        ]
    }
    report = {"verification_points": [{"id": "VP-010", "status": "passed"}]}

    issues = _issues(report, plan_json, _parsed("VP-001"))

    assert any("VP-010" in issue and "origin" in issue for issue in issues)


# ---------------------------------------------------------------------------
# skip：跳过的验证点必须出现在 skipped_items[] 里
# ---------------------------------------------------------------------------


def test_skip_items_listed():
    """被跳过的 VP 没进 ``skipped_items[]`` → 报问题。"""
    report = {
        "verification_points": [
            {"id": "VP-001", "status": "passed"},
            {"id": "VP-002", "status": "skipped"},
        ],
        "skipped_items": [],
    }

    issues = _issues(report)

    assert any("VP-002" in issue and "skipped_items" in issue for issue in issues)


def test_listed_skip_has_no_issue():
    """进了 ``skipped_items[]`` 的跳过 VP 不报问题。"""
    report = {
        "verification_points": [
            {"id": "VP-002", "status": "skipped"},
        ],
        "skipped_items": ["VP-002"],
    }

    assert _issues(report) == []


def test_non_skipped_vp_need_not_be_listed():
    """通过 / 失败的 VP 不必出现在 ``skipped_items[]`` 里。"""
    report = {
        "verification_points": [
            {"id": "VP-001", "status": "passed"},
            {"id": "VP-002", "status": "failed"},
        ],
        "skipped_items": [],
    }

    assert _issues(report) == []


def test_skipped_items_entries_carry_id_and_reason():
    """``skipped_items[]`` 的条目是「id + 原因」两字段的 dict。"""
    report = {
        "verification_points": [
            {"id": "VP-002", "status": "skipped"},
        ],
        "skipped_items": [{"id": "VP-002", "reason": "需要人工验证"}],
    }

    assert _issues(report) == []


# ---------------------------------------------------------------------------
# 输入形状
# ---------------------------------------------------------------------------


def test_executor_schema_vps_key_is_accepted():
    """机器计划用执行器 schema 的 ``vps`` 键时同样认得。"""
    plan_json = {"vps": [{"id": "VP-001"}, {"id": "VP-002"}]}

    report = {"verification_points": [{"id": "VP-001", "status": "passed"}]}

    assert _issues(report, plan_json, _parsed("VP-001")) == []


def test_parsed_plan_as_mapping_is_accepted():
    """计划书解析产物是 ``{"items": [...]}`` 映射时同样认得。"""
    parsed = {"items": [{"id": "VP-001"}, {"id": "VP-002"}]}

    report = {"verification_points": [{"id": "VP-001", "status": "passed"}]}

    assert _issues(report, _PLAN_JSON, parsed) == []


def test_plan_json_without_points_flags_every_vp():
    """机器计划里一条 VP 都没有 → 每条报告 VP 都断链。"""
    report = {"verification_points": [{"id": "VP-001", "status": "passed"}]}

    issues = _issues(report, {"verification_points": []}, _parsed("VP-001"))

    assert issues == ["VP-001 not in verification_plan.json"]


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_issues_are_order_independent():
    """入参顺序打乱，问题列表逐条相同。"""
    points = [
        {"id": "VP-001", "status": "passed"},
        {"id": "VP-002", "status": "skipped"},
        {"id": "VP-999", "status": "passed"},
    ]

    first = _issues({"verification_points": list(points)})
    second = _issues({"verification_points": list(reversed(points))})

    assert first == second


def test_issues_are_deterministic():
    """同一份入参两次调用结果相同。"""
    report = {"verification_points": [{"id": "VP-999"}, {"id": "VP-010"}]}

    assert _issues(report) == _issues(report)


# ---------------------------------------------------------------------------
# 接入渲染路径
# ---------------------------------------------------------------------------


def test_render_surfaces_traceability_issues():
    """给了机器计划 / 计划书时，渲染结果里带得出回溯问题。"""
    report = {"verification_points": [{"id": "VP-999"}]}

    markdown = render(
        report,
        plan_items={},
        review_state={},
        repair_rows=[],
        plan_json=_PLAN_JSON,
        parsed=_parsed("VP-001", "VP-002"),
    )

    assert "VP-999 not in verification_plan.json" in markdown
    assert "VP-999 not in plan document" in markdown


def test_render_without_plan_keeps_traceability_section_out():
    """没给机器计划 / 计划书时，不多这一节（旧行为不变）。"""
    report = {"verification_points": [{"id": "VP-999"}]}

    markdown = render(report, plan_items={}, review_state={}, repair_rows=[])

    assert "not in verification_plan.json" not in markdown


def test_render_with_clean_traceability_omits_section():
    """回溯没问题时不渲染这一节（空节不携带信息）。"""
    report = {"verification_points": [{"id": "VP-001", "status": "passed"}]}

    markdown = render(
        report,
        plan_items={},
        review_state={},
        repair_rows=[],
        plan_json=_PLAN_JSON,
        parsed=_parsed("VP-001", "VP-002"),
    )

    assert "not in verification_plan.json" not in markdown
    assert "## 验证点结果" in markdown
