"""R4 —— infra 终端 VP 的 HITL 提问。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2）
------------------------------------------------------
R4：``infrastructure_unresolved`` 的点 —— 环境的重试预算耗尽，这个点
**从未被判过**。它有两条路，而两条都是死路：

* **混合轮次**：``_route_failed_vps`` 的 infra 终端分支只把它记进
  ``deferred``，没有人被问；
* **全 infra 轮次**：``_all_failed_are_exhausted_infra`` 直接 return，
  链子停在 ``infrastructure_unresolved``，同样没有人被问。

被问才有出路。本任务给这两条路都补上 HITL 决策：文案、选项、默认值
全部来自 ``verification.repair_coverage``（任务 1），写入仍然只走
``_ask_a_human_about`` —— 它按 VP 去重，已经有一个 pending/paused 的
问题时不会再写第二个问题。

钉住的五件事
------------
1. **混合轮次也问** —— 终端 VP 进 ``deferred`` 时带上 ``decision_id``；
2. **全 infra 轮次也问** —— 早退分支把每个终端点问一遍；
3. **一个点只问一次** —— 同一条 VP 在报告里出现两次（顶层结果 + 末轮
   快照）只开一个问题；
4. **只问终端条目** —— passed 的点、没有终端标记的失败点都不问；
5. **默认跟随 recommendation** —— recommendation 在三元组内就当默认，
   否则 ``retry_more``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.repair_coverage import (  # noqa: E402
    INFRA_TERMINAL_DEFAULT_OPTION,
    INFRA_TERMINAL_OPTIONS,
)
from verification.orchestrator import (  # noqa: E402
    VERIFICATION_DECISION_SOURCE,
    VerificationOrchestrator,
)


#: The terminal the report carries when the environment never let the
#: round judge a point.  Spelled out here so the test pins the code
#: itself rather than whatever the constant holds.
INFRA_UNRESOLVED_TERMINAL = "infrastructure_unresolved"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    The HITL decisions land in ``state.db``'s ``plan_decisions`` table.
    Without an override the questions would be written into the repo's
    real ``state.db``, and a question another test left behind would be
    read back as "this point already has an open question".
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)
    return plan_dir


@pytest.fixture
def orchestrator(plan_dir, tmp_path, monkeypatch):
    """A verification orchestrator whose heavy dependencies are replaced."""
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(
        plan_dir=plan_dir, project_dir=tmp_path, coding_tool=Mock(),
    )


class _DecisionSpy:
    """``hitl.service.create_decision``'s replacement.

    It records every request it is handed and answers with a decision id
    of its own — so the test can assert both *that* the question was
    asked and *what* it said.
    """

    def __init__(self):
        self.requests = []

    def __call__(self, request, plan_dir, *args, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(decision_id=f"DEC-R4-{len(self.requests)}")


@pytest.fixture
def spy(monkeypatch):
    """Put the replacement in place."""
    _spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", _spy)
    return _spy


def _write_plan_state(plan_dir, phase="executing", verification_round=0):
    """Bootstrap the plan state the orchestrator reads."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": 3,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# R4 —— 混合轮次：终端 VP 进 deferred 时也问
# ---------------------------------------------------------------------------


def test_route_failed_vps_asks_about_infra_terminal(orchestrator, spy):
    """A terminal point is asked about on the way into ``deferred``.

    The mixed round keeps its repair path for the real defect, and the
    terminal point — which was never judged — gets a question of its
    own instead of silently landing in ``deferred``.
    """
    repairable, deferred = orchestrator._route_failed_vps(
        [
            {"id": "VP-009", "title": "the real defect"},
            {
                "id": "VP-006",
                "title": "the blocked point",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
            },
        ],
        2,
    )

    # The real defect still goes to the repair path.
    assert [vp.get("id") for vp in repairable] == ["VP-009"]

    # The terminal point is recorded — and it now carries the question.
    terminal = [r for r in deferred if r.get("id") == "VP-006"]
    assert len(terminal) == 1
    record = terminal[0]
    assert record["failure_class"] == "infra"
    assert record["decision_id"] == "DEC-R4-1"

    # One question, about that point, with the infra wording.
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.vp_id == "VP-006"
    assert request.options == ["retry_more", "skip_this_vp", "defer"]
    assert request.options == list(INFRA_TERMINAL_OPTIONS)
    assert request.default_option == "retry_more"
    assert request.failure_class == "infra"
    assert request.source == VERIFICATION_DECISION_SOURCE
    assert request.round == 2
    assert "VP-006" in request.question


def test_recommendation_sets_the_default_option(orchestrator, spy):
    """The default follows the point's own recommendation."""
    orchestrator._ask_human_about_infra_terminal(
        {"id": "VP-006", "recommendation": "skip_this_vp"}, 2,
    )
    orchestrator._ask_human_about_infra_terminal({"id": "VP-007"}, 2)
    orchestrator._ask_human_about_infra_terminal(
        {"id": "VP-008", "recommendation": "not_an_option"}, 2,
    )

    assert [request.default_option for request in spy.requests] == [
        "skip_this_vp",
        INFRA_TERMINAL_DEFAULT_OPTION,
        INFRA_TERMINAL_DEFAULT_OPTION,
    ]
    assert spy.requests[1].default_option == "retry_more"
    assert spy.requests[2].default_option == "retry_more"
    for request in spy.requests:
        assert request.default_option in request.options


# ---------------------------------------------------------------------------
# R4 —— 全 infra 轮次：每个终端点都问一遍
# ---------------------------------------------------------------------------


def test_all_infra_entries_each_get_one_question(orchestrator, spy):
    """Two terminal points — two questions."""
    entries = [
        {"id": "VP-031", "infrastructure_unresolved": True},
        {"id": "VP-032", "infrastructure_unresolved": True},
    ]

    raised = orchestrator._ask_humans_about_infra_terminals(entries, 4)

    assert len(spy.requests) == 2
    assert [request.vp_id for request in spy.requests] == ["VP-031", "VP-032"]
    assert raised == ["VP-031", "VP-032"]


def test_duplicate_terminal_entry_asked_once(orchestrator, spy):
    """The same point twice — one question.

    ``_report_failure_entries`` hands back the top-level results **and**
    the last round's snapshot, so one point shows up twice.
    """
    entries = [
        {"id": "VP-031", "infrastructure_unresolved": True},
        {"id": "VP-031", "infrastructure_unresolved": True},
    ]

    raised = orchestrator._ask_humans_about_infra_terminals(entries, 4)

    assert len(spy.requests) == 1
    assert spy.requests[0].vp_id == "VP-031"
    assert raised == ["VP-031"]


def test_open_question_suppresses_second_ask(orchestrator, spy, monkeypatch):
    """A question that is already open is not asked a second time."""
    monkeypatch.setattr(
        orchestrator, "_open_decision_for", lambda *args, **kwargs: True,
    )

    decision_id = orchestrator._ask_human_about_infra_terminal(
        {"id": "VP-031"}, 4,
    )
    raised = orchestrator._ask_humans_about_infra_terminals(
        [{"id": "VP-031", "infrastructure_unresolved": True}], 4,
    )

    assert decision_id == ""
    assert raised == []
    assert spy.requests == []


def test_non_terminal_entries_are_ignored(orchestrator, spy):
    """Only the terminal entries are asked about."""
    entries = [
        {"id": "VP-001", "status": "PASSED"},
        {"id": "VP-002", "status": "FAILED"},
        {"id": "VP-003", "status": "BLOCKED"},
        {"id": "VP-031", "infrastructure_unresolved": True},
        # A terminal entry without an id names no point: there is
        # nobody to ask about.
        {"id": "", "infrastructure_unresolved": True},
    ]

    raised = orchestrator._ask_humans_about_infra_terminals(entries, 4)

    assert [request.vp_id for request in spy.requests] == ["VP-031"]
    assert raised == ["VP-031"]


def test_all_infra_cycle_asks_about_every_terminal(orchestrator, spy, plan_dir):
    """The all-infra round asks before it stops.

    This is the branch that used to be a dead end: the chain stopped
    with ``infrastructure_unresolved`` and nobody was ever asked.
    """
    _write_plan_state(plan_dir)
    report = {
        "overall_status": "SKIPPED",
        "verification_results": [
            {
                "id": "VP-031",
                "status": "BLOCKED",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
                "infrastructure_unresolved": True,
            },
        ],
    }

    result = orchestrator.check_cycle_conditions(report, 4)

    assert result["infrastructure_unresolved"] is True
    assert [request.vp_id for request in spy.requests] == ["VP-031"]
    assert spy.requests[0].options == list(INFRA_TERMINAL_OPTIONS)


# ---------------------------------------------------------------------------
# R4 —— 真实路径：reader 必须把终端条目交给路由
# ---------------------------------------------------------------------------
#
# 上面的用例都直接喂 ``_route_failed_vps`` —— 契约本身是对的。但生产
# 路径上，路由的输入来自
# :func:`verification.verification_report_reader.extract_failed_vps_with_paths`，
# 而它只保留 ``status == "FAILED"`` 的条目。终端条目的 status 是
# ``BLOCKED``（它不是对交付物的判决），于是它从来到不了路由：
#
#   * 没有修复任务（对的），
#   * 也没有人问（**静默搁置** —— 这正是本任务要堵的洞），
#   * 连 ``deferred`` 都不记。
#
# 混合轮最致命：真缺陷走修复路径，终端点人间蒸发。


def _mixed_round_report():
    """A round with a real defect and a terminal point."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "BLOCKED",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
                "reason_code": INFRA_UNRESOLVED_TERMINAL,
                "infrastructure_unresolved": True,
                "failure_class": "infra",
                "failure_class_source": "deterministic",
                "title": "the blocked point",
            },
            {
                "id": "VP-009",
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "title": "the real defect",
                "actual_result": "boom",
            },
        ],
    }


def _write_report_and_plan(plan_dir, report, point_ids=("VP-006", "VP-009")):
    """Put a report and a plan on disk, the way a real round leaves them."""
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({
            "verification_points": [
                {"id": vp_id, "title": f"point {vp_id}"}
                for vp_id in point_ids
            ],
        }),
        encoding="utf-8",
    )
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report), encoding="utf-8",
    )


def test_mixed_round_extraction_keeps_infra_terminal_entry(plan_dir):
    """The reader hands the terminal entry on — markers and all.

    The selection rule cannot be "status == FAILED" alone: the terminal
    entry's status is ``BLOCKED`` because it is **not** a verdict about
    the deliverable.  Dropping it is what made the point vanish.
    """
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    _write_plan_state(plan_dir)
    _write_report_and_plan(plan_dir, _mixed_round_report())

    entries = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json",
        plan_dir / "verification_plan.json",
    )
    ids = {entry["id"] for entry in entries}

    assert ids == {"VP-006", "VP-009"}, (
        f"混合轮的两个点都必须被 reader 抽到，实际抽到 {sorted(ids)!r}"
    )
    terminal = [e for e in entries if e["id"] == "VP-006"][0]
    # The markers travel with the entry — the router reads them to know
    # this is a terminal and not a broken deliverable.
    assert (
        terminal.get("infrastructure_unresolved") is True
        or terminal.get("failure_terminal") == INFRA_UNRESOLVED_TERMINAL
    ), terminal
    assert terminal["failure_class"] == "infra"


def test_plain_blocked_entry_without_infra_terminal_marker_is_still_excluded(
    plan_dir,
):
    """``BLOCKED`` on its own is not the rule.

    A blocked entry that carries **no** terminal marker is somebody
    else's verdict — the environment block, which has a round-level path
    of its own.  Including every blocked entry would drag it into the
    per-point question and change the disposition / same-cause
    behaviour.
    """
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    _write_plan_state(plan_dir)
    report = {
        "overall_status": "SKIPPED",
        "verification_results": [
            {
                "id": "VP-007",
                "status": "BLOCKED",
                "environment_blocked": True,
                "reason_code": "service_unreachable",
                "title": "the environment-blocked point",
            },
        ],
    }
    _write_report_and_plan(plan_dir, report, point_ids=("VP-007",))

    entries = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json",
        plan_dir / "verification_plan.json",
    )

    assert "VP-007" not in {entry["id"] for entry in entries}, (
        f"没有终端标记的 BLOCKED 条目不得被 reader 收进来，"
        f"实际抽到 {entries!r}"
    )


def _orchestrator_for(plan_dir, project_dir):
    """An orchestrator whose heavy dependencies are replaced.

    Built here rather than taken from the ``orchestrator`` fixture so a
    test can drive **two** rounds — two rounds, two plans, two sets of
    state.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orchestrator = VerificationOrchestrator(
            plan_dir, project_dir, Mock(),
        )
    orchestrator.repair_generator.generate_repair_contents.return_value = [
        {"failed_vp_id": "VP-009", "title": "fix VP-009"},
    ]
    return orchestrator


def _run_mixed_round(plan_dir, project_dir, report):
    """Write the report, then drive the whole reader → router path."""
    _write_plan_state(plan_dir)
    _write_report_and_plan(plan_dir, report)
    orchestrator = _orchestrator_for(plan_dir, project_dir)
    with patch("repair_generator.RepairTaskAssembler") as assembler:
        assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "title": "fix", "failed_vp_id": "VP-009"},
        ]
        result = orchestrator.check_cycle_conditions(report, round_number=1)
    return orchestrator, result


def test_mixed_round_asks_about_infra_terminal_on_the_spot(
    spy, plan_dir, tmp_path,
):
    """The real report drives the whole path.

    One question — asked **on the spot**, at the failure routing —
    about the point the environment ate; one repair task, for the
    defect that is real.  The terminal point buys neither a repair task
    nor silence: it is written down in ``deferred`` with the decision
    id of the question.
    """
    report = _mixed_round_report()
    _orchestrator, result = _run_mixed_round(plan_dir, tmp_path, report)

    # Exactly one question, about the terminal point.
    assert len(spy.requests) == 1, [
        (r.vp_id, list(r.options), r.default_option) for r in spy.requests
    ]
    request = spy.requests[0]
    assert request.vp_id == "VP-006"
    assert request.failure_class == "infra"
    assert list(request.options) == ["retry_more", "skip_this_vp", "defer"]
    # No recommendation on the entry → the default falls back.
    assert request.default_option == "retry_more"

    # The real defect still gets its repair task — the terminal point
    # does not swallow the round — and it is not asked about.
    tasks = result.get("repair_tasks") or []
    assert {t.get("failed_vp_id") for t in tasks} == {"VP-009"}, tasks
    assert all(r.vp_id != "VP-009" for r in spy.requests)

    # The terminal point is written down: deferred, infra, retry-not-
    # counted, with the question's decision id.
    deferred = [
        item for item in (result.get("deferred_vps") or [])
        if item.get("id") == "VP-006"
    ]
    assert len(deferred) == 1, result.get("deferred_vps")
    record = deferred[0]
    assert record["failure_class"] == "infra"
    assert record["disposition"] == "retry_not_counted"
    assert record["decision_id"], record


def test_mixed_round_default_follows_recommendation(spy, plan_dir, tmp_path):
    """The default follows the point's own recommendation.

    On the real path the recommendation lives in the report; a reader
    that dropped it would make every question default to
    ``retry_more``, and "默认跟随 recommendation" would hold only in the
    unit tests.  A recommendation nobody can pick is not a default: it
    falls back.
    """
    report = _mixed_round_report()
    report["verification_results"][0]["recommendation"] = "skip_this_vp"
    _run_mixed_round(plan_dir, tmp_path, report)

    assert len(spy.requests) == 1, spy.requests
    assert spy.requests[0].default_option == "skip_this_vp"
    assert spy.requests[0].default_option in spy.requests[0].options

    # A recommendation outside the option table is not a default.
    other_report = _mixed_round_report()
    other_report["verification_results"][0]["recommendation"] = "explode"
    other_plan_dir = tmp_path / "plans" / "other-plan"
    other_plan_dir.mkdir(parents=True)
    spy.requests.clear()
    _run_mixed_round(other_plan_dir, tmp_path, other_report)

    assert len(spy.requests) == 1, spy.requests
    assert spy.requests[0].default_option == "retry_more"


def test_all_infra_round_asks_the_terminal_point_once(spy, plan_dir, tmp_path):
    """The all-infra round still asks once — no second question.

    The reader now hands the terminal entry to the router as well, and
    the all-infra exit asks about the terminals of its own.  Both must
    not fire for the same point: one question, one point.
    """
    report = {
        "overall_status": "SKIPPED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "BLOCKED",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
                "reason_code": INFRA_UNRESOLVED_TERMINAL,
                "infrastructure_unresolved": True,
                "failure_class": "infra",
                "title": "the blocked point",
            },
        ],
    }
    _write_plan_state(plan_dir)
    _write_report_and_plan(plan_dir, report, point_ids=("VP-006",))
    orchestrator = _orchestrator_for(plan_dir, tmp_path)
    result = orchestrator.check_cycle_conditions(report, round_number=4)

    assert result["infrastructure_unresolved"] is True
    assert [request.vp_id for request in spy.requests] == ["VP-006"]

