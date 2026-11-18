"""R3 —— 缺失 VP 的 HITL 提问。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2）
------------------------------------------------------
一个 ``deliverable_defect`` 失败的 VP 没能拿到 repair 任务 —— 生成器漏了
它，或者 ``assemble`` 把它那行 ``files_to_modify`` 为空的行丢掉了。repair
内容是一次生成的，漏掉的那条既没有任务、也没有 HITL：没有人知道它失败
过，直到下一轮它再次失败。

本任务只加**薄封装**：文案、选项、默认值全部来自
``verification.repair_coverage``（任务 1），写入仍然只走
``hitl.service.create_decision`` —— ``_ask_a_human_about`` 是唯一写入口，
并且按 VP 去重。

钉住的五件事
------------
1. **问题点名 VP 与原因** —— 只被告知「VP-023 失败了」的人分不清是生成
   器漏了还是被 assemble 丢了，而这两件事要修的地方完全不同；
2. **默认跟随这条 VP 自己的 recommendation** —— 没有 recommendation、
   或者 recommendation 不在选项表内，才回落到 ``manual_fix``；
3. **同一 VP 不重复开问** —— 已经有一个 pending/paused 的问题时返回
   ``""``，不写第二行；
4. **失败类与审计事件写死** —— ``deliverable_defect`` /
   ``verification_repair_coverage_decision_failed``；
5. **问不出来也不抛** —— 问题写不进去时审计留痕，方法返回 ``""``。

orchestrator 的两个重依赖被换掉：被测方法只读 ``self.plan_dir.name``，
留着一个真的 agent 或 repair 生成器只会添乱。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.repair_coverage import (  # noqa: E402
    MISSED_VP_DEFAULT_OPTION,
    MISSED_VP_OPTIONS,
    REASON_EMPTY_FILES_TO_MODIFY,
    REASON_GENERATOR_MISSING,
)
from verification.orchestrator import (  # noqa: E402
    VERIFICATION_DECISION_SOURCE,
    VerificationOrchestrator,
)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """一个重依赖被替换掉的 verification orchestrator。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(plan_dir=plan_dir, project_dir=tmp_path)


class _DecisionSpy:
    """``hitl.service.create_decision`` 的替身。

    记下每一个交给它的 request，并回一个带已知 id 的决定 —— 于是测试既
    能断言「问题问出去了」，也能断言「它问的是什么」。
    """

    def __init__(self, decision_id="DEC-R3"):
        self.requests = []
        self._decision_id = decision_id

    def __call__(self, request, plan_dir, *args, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(decision_id=self._decision_id)


@pytest.fixture
def spy(monkeypatch):
    """装上 ``create_decision`` 的替身。"""
    _spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", _spy)
    return _spy


# ---------------------------------------------------------------------------
# R3 —— 问题、选项、默认值
# ---------------------------------------------------------------------------


def test_question_names_vp_and_reason(orchestrator, spy):
    """问题点名 VP 与原因；选项与默认值跟着这条 VP 的 recommendation。"""
    vp = {"id": "VP-023", "recommendation": "defer"}

    decision_id = orchestrator._ask_human_about_missing_repair(
        vp, REASON_EMPTY_FILES_TO_MODIFY, 3,
    )

    assert decision_id == "DEC-R3"
    assert len(spy.requests) == 1
    request = spy.requests[0]

    # 问题要能被人读懂：哪个点、哪一轮、为什么没有任务。
    assert request.vp_id == "VP-023"
    assert "VP-023" in request.question
    assert "files_to_modify" in request.question
    assert "3" in request.question

    # 三个出路，一个不多一个不少。
    assert request.options == ["manual_fix", "revise_criterion", "defer"]
    assert request.options == list(MISSED_VP_OPTIONS)
    # 这条 VP 自己建议 defer，默认就跟着它走。
    assert request.default_option == "defer"

    # 写进决策的是「产物真的错了」这一类，来源仍是验证。
    assert request.failure_class == "deliverable_defect"
    assert request.source == VERIFICATION_DECISION_SOURCE
    assert request.round == 3
    assert request.plan_id == orchestrator.plan_dir.name


def test_default_follows_recommendation(orchestrator, spy):
    """默认跟随 recommendation；没有或不在表内才回落到 ``manual_fix``。"""
    orchestrator._ask_human_about_missing_repair(
        {"id": "VP-023", "recommendation": "defer"},
        REASON_EMPTY_FILES_TO_MODIFY,
        3,
    )
    orchestrator._ask_human_about_missing_repair(
        {"id": "VP-031"},
        REASON_GENERATOR_MISSING,
        4,
    )
    orchestrator._ask_human_about_missing_repair(
        {"id": "VP-032", "recommendation": "not_an_option"},
        REASON_GENERATOR_MISSING,
        5,
    )

    assert [request.default_option for request in spy.requests] == [
        "defer",
        MISSED_VP_DEFAULT_OPTION,
        MISSED_VP_DEFAULT_OPTION,
    ]
    assert spy.requests[1].default_option == "manual_fix"
    assert spy.requests[2].default_option == "manual_fix"
    # 每个默认值都必须在自己的选项表里。
    for request in spy.requests:
        assert request.default_option in request.options


# ---------------------------------------------------------------------------
# R6 —— 同一 VP 不重复开问
# ---------------------------------------------------------------------------


def test_open_question_suppresses_second_ask(orchestrator, spy, monkeypatch):
    """已经有一个 pending/paused 的问题时，不再写第二个问题。"""
    monkeypatch.setattr(
        orchestrator, "_open_decision_for", lambda *args, **kwargs: True,
    )

    decision_id = orchestrator._ask_human_about_missing_repair(
        {"id": "VP-023"}, REASON_EMPTY_FILES_TO_MODIFY, 3,
    )

    assert decision_id == ""
    assert spy.requests == []


# ---------------------------------------------------------------------------
# R7 —— 问出来的那一行在 plan_decisions 里查得到
# ---------------------------------------------------------------------------


def test_decision_lands_in_plan_decisions(orchestrator):
    """真的写一次：那一行落在 ``plan_decisions`` 里，查得到。"""
    from hitl.service import list_decisions

    decision_id = orchestrator._ask_human_about_missing_repair(
        {"id": "VP-023", "recommendation": "defer"},
        REASON_EMPTY_FILES_TO_MODIFY,
        3,
    )

    assert decision_id
    stored = [
        decision
        for decision in list_decisions(orchestrator.plan_dir.name)
        if decision.decision_id == decision_id
    ]
    assert len(stored) == 1
    assert stored[0].vp_id == "VP-023"
    assert stored[0].failure_class == "deliverable_defect"
    assert stored[0].round == 3
    assert list(stored[0].options) == list(MISSED_VP_OPTIONS)
    assert stored[0].default_option == "defer"


# ---------------------------------------------------------------------------
# 边界 —— 问不出来也不抛
# ---------------------------------------------------------------------------


def test_decision_failure_is_audited_and_returns_empty(
    orchestrator, monkeypatch,
):
    """问题写不进去时审计留痕，方法不抛、返回 ``""``。"""
    events = []

    def _boom(request, plan_dir, *args, **kwargs):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr("hitl.service.create_decision", _boom)
    monkeypatch.setattr(
        orchestrator,
        "_log_audit",
        lambda event, message, data=None: events.append((event, message, data)),
    )

    decision_id = orchestrator._ask_human_about_missing_repair(
        {"id": "VP-023"}, REASON_EMPTY_FILES_TO_MODIFY, 3,
    )

    assert decision_id == ""
    assert len(events) == 1
    event, message, data = events[0]
    assert event == "verification_repair_coverage_decision_failed"
    assert "VP-023" in message
    assert data["vp_id"] == "VP-023"
    assert data["failure_class"] == "deliverable_defect"
    assert data["round"] == 3
