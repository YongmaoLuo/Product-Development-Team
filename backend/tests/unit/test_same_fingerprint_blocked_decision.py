"""TDD spec —— 同一失败指纹修满 2 次之后，必须真的**问人**。

背景
----
任务 5 已经把「同一指纹已修 2 次」的点从修复集合里摘出来、记成
``blocked``，但**没有人被问过**：记录里只有一句 ``reason``，要人自己去读
round result 才知道要决定什么。auto-loop 的
``_wait_for_open_verification_decisions()`` 只等 ``plan_decisions`` 表里的
问题 —— 在记录里写一句 reason 不会让它停下，这一轮照跑。

所以这个文件钉住：**blocked 的点必须真的建一个 decision**
（``hitl.service.create_decision`` 是 decision 写入的唯一路径），并把
``decision_id`` 记在 deferred 记录上。

钉住五件事
----------
1. run == 3（已修 2 次）→ deferred 记录带 ``decision_id``；题目说清楚
   「连续 3 轮未变 / 已修 2 次」，选项是 ``repair_again`` /
   ``void_the_point``，默认 ``void_the_point``；request 带 ``vp_id`` 与
   ``round``；
2. run == 2（已修 1 次）→ 照旧进 ``repairable``，**一个问题都不许建**；
3. ``create_decision`` 抛异常 → 问不出来也不崩：``decision_id`` 是空串，
   记录仍然是 ``blocked``，audit 里留下失败事件；
4. 同一个点已经有 open question → 不重复问（``decision_id`` 是空串，
   ``create_decision`` 没被调用）；
5. 建出来的 decision 走 verification 源 —— auto-loop 只认这个源的等待。

测试命令::

    pytest backend/tests/unit/test_same_fingerprint_blocked_decision.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import (  # noqa: E402
    SAME_FINGERPRINT_DEFAULT_OPTION,
    SAME_FINGERPRINT_OPTIONS,
    VERIFICATION_DECISION_SOURCE,
    VerificationOrchestrator,
)


#: The point the rounds below fail on.
VP_ID = "VP-1"

#: The failure class that buys a repair task — the only class this gate
#: touches.
DELIVERABLE_DEFECT = "deliverable_defect"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "same-fingerprint-blocked-decision"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "same-fingerprint-blocked-decision"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    Without this the orchestrator would write the test plan's row into
    the repo's real ``state.db``.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _make_orchestrator(plan_dir, project_dir, coding_tool):
    """Build an orchestrator with the model-facing halves mocked out."""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        return VerificationOrchestrator(plan_dir, project_dir, coding_tool)


class _DecisionSpy:
    """Stands in for ``hitl.service.create_decision``.

    Records every request it is handed, and answers with a decision that
    carries a known id — so a test can assert both *that* the question
    was asked and *what* it said.
    """

    def __init__(self, decision_id="DEC-1"):
        self.requests = []
        self._decision_id = decision_id

    def __call__(self, request, plan_dir, *args, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(decision_id=self._decision_id)


def _broken_deliverable():
    """A failed point whose class buys a repair task."""
    return {"id": VP_ID, "title": "the broken deliverable"}


# ---------------------------------------------------------------------------
# 1. The blocked point is really asked about
# ---------------------------------------------------------------------------


def test_blocked_vp_gets_a_human_decision(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """run == 3（已修 2 次）→ 建 decision，decision_id 记在记录上。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 3}
    monkeypatch.setattr(
        orch, "_open_decision_for", lambda *args, **kwargs: False,
    )
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)

    repairable, deferred = orch._route_failed_vps([_broken_deliverable()], 3)

    assert repairable == [], (
        f"同一指纹修满 2 次之后不得再派修复任务，实际是 {repairable!r}"
    )
    assert len(deferred) == 1, (
        f"被拦下的点必须被记下来，实际是 {deferred!r}"
    )
    record = deferred[0]
    assert record.get("decision_id") == "DEC-1", (
        f"blocked 的点必须真的问人并记下 decision_id，实际是 {record!r}"
    )
    assert record.get("blocked") is True, (
        f"被拦下的记录必须带 blocked，实际是 {record!r}"
    )

    assert len(spy.requests) == 1, (
        f"只该问一个问题，实际是 {spy.requests!r}"
    )
    request = spy.requests[0]
    assert request.vp_id == VP_ID, (
        f"问题必须说清楚是哪个点，实际是 {request!r}"
    )
    assert request.round == 3, (
        f"问题必须说清楚是哪一轮，实际是 {request!r}"
    )
    assert request.source == VERIFICATION_DECISION_SOURCE, (
        f"问题必须走 verification 源（auto-loop 只等这个源），实际是 {request!r}"
    )
    assert request.failure_class == DELIVERABLE_DEFECT, (
        f"问题必须带着失败类别，实际是 {request!r}"
    )
    assert "同一指纹已修 2 次" in request.question, (
        f"题目必须说清楚已经修了多少次，实际是 {request.question!r}"
    )
    assert request.options == ["repair_again", "void_the_point"], (
        f"选项必须是修 / 作废这两个，实际是 {request.options!r}"
    )
    assert request.options == list(SAME_FINGERPRINT_OPTIONS), (
        f"选项必须与模块常量一致，实际是 {request.options!r}"
    )
    assert request.default_option == "void_the_point", (
        f"默认必须是作废，实际是 {request.default_option!r}"
    )
    assert request.default_option == SAME_FINGERPRINT_DEFAULT_OPTION, (
        f"默认必须与模块常量一致，实际是 {request.default_option!r}"
    )


# ---------------------------------------------------------------------------
# 2. A point that may still be repaired is not asked about
# ---------------------------------------------------------------------------


def test_no_decision_for_a_repairable_vp(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """run == 2（已修 1 次）→ 照旧派修复任务，一个问题都不许建。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 2}
    monkeypatch.setattr(
        orch, "_open_decision_for", lambda *args, **kwargs: False,
    )
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)

    vp = _broken_deliverable()
    repairable, deferred = orch._route_failed_vps([vp], 2)

    assert repairable == [vp], (
        f"只修过一轮还得再修一轮，实际是 {repairable!r}"
    )
    assert deferred == [], (
        f"还没到上限的点不得进 deferred，实际是 {deferred!r}"
    )
    assert spy.requests == [], (
        f"还能修的点不得被问，实际是 {spy.requests!r}"
    )


# ---------------------------------------------------------------------------
# 3. The other deferred paths ask nothing
# ---------------------------------------------------------------------------


def test_no_decision_for_the_other_deferred_paths(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """infra / criterion_defect 走各自的原路径，不许被这个问题盯上。

    这两个类别既不进 ``repairable``，也不是 ``blocked`` —— 它们各有各
    的去处（环境问题重试，判据问题换判据），所以这里既不该建
    decision，也不该带 ``decision_id``。
    """
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 3}
    monkeypatch.setattr(
        orch, "_open_decision_for", lambda *args, **kwargs: False,
    )
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)

    vps = [
        {"id": "VP-INFRA", "title": "the flaky environment",
         "failure_class": "infra"},
        {"id": "VP-CRITERION", "title": "the broken criterion",
         "failure_class": "criterion_defect"},
    ]
    repairable, deferred = orch._route_failed_vps(vps, 3)

    assert repairable == [], (
        f"这两个类别本来就不进修复集合，实际是 {repairable!r}"
    )
    assert len(deferred) == 2, (
        f"两个点都要被记下来，实际是 {deferred!r}"
    )
    for record in deferred:
        assert "decision_id" not in record, (
            f"只有 blocked 的点才建 decision，实际是 {record!r}"
        )
        assert "blocked" not in record, (
            f"这两个类别不受同一个指纹门影响，实际是 {record!r}"
        )
    assert spy.requests == [], (
        f"这两个类别不得被问，实际是 {spy.requests!r}"
    )


# ---------------------------------------------------------------------------
# 4. A question that could not be created does not take the round down
# ---------------------------------------------------------------------------


def test_decision_failure_keeps_the_point_blocked(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """建不出 decision → 不崩，decision_id 为空，记录仍然 blocked。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 3}
    monkeypatch.setattr(
        orch, "_open_decision_for", lambda *args, **kwargs: False,
    )
    events = []

    def _boom(request, plan_dir, *args, **kwargs):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr("hitl.service.create_decision", _boom)
    monkeypatch.setattr(
        orch,
        "_log_audit",
        lambda event, message, data=None: events.append(
            (event, message, data),
        ),
    )

    repairable, deferred = orch._route_failed_vps([_broken_deliverable()], 3)

    assert repairable == [], (
        f"问不出来也不许把点放回修复集合，实际是 {repairable!r}"
    )
    assert len(deferred) == 1, (
        f"被拦下的点必须被记下来，实际是 {deferred!r}"
    )
    record = deferred[0]
    assert record.get("decision_id") == "", (
        f"建不出问题就没有 decision_id，实际是 {record!r}"
    )
    assert record.get("blocked") is True, (
        f"问不出来不等于放行，记录仍然必须 blocked，实际是 {record!r}"
    )
    assert len(events) == 1, (
        f"建不出问题必须留下 audit，实际是 {events!r}"
    )
    event, _message, data = events[0]
    assert event == "verification_same_fingerprint_decision_failed", (
        f"audit 事件名必须说清楚是哪一类问题，实际是 {event!r}"
    )
    assert data["vp_id"] == VP_ID, (
        f"audit 必须说清楚是哪个点，实际是 {data!r}"
    )


# ---------------------------------------------------------------------------
# 5. A point with an open question is not asked twice
# ---------------------------------------------------------------------------


def test_no_second_question_for_a_point_that_already_has_one(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """同一个点已经有 open question → 不重复弹问。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 3}
    monkeypatch.setattr(
        orch, "_open_decision_for", lambda *args, **kwargs: True,
    )
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)

    repairable, deferred = orch._route_failed_vps([_broken_deliverable()], 3)

    assert repairable == [], (
        f"被拦下的点不得回修复集合，实际是 {repairable!r}"
    )
    assert len(deferred) == 1, (
        f"被拦下的点必须被记下来，实际是 {deferred!r}"
    )
    record = deferred[0]
    assert record.get("decision_id") == "", (
        f"已有 open question 就没有新的 decision_id，实际是 {record!r}"
    )
    assert record.get("blocked") is True, (
        f"记录仍然必须 blocked，实际是 {record!r}"
    )
    assert spy.requests == [], (
        f"同一个点不得被问两次，实际是 {spy.requests!r}"
    )
