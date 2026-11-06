"""The orchestrator's record of a terminal decision's session and analysis.

The terminal branch of the verification loop stops and asks a human what
happens next.  Two things come out of that stop that used to be lost the
moment the round ended: **which analysis session** produced the
recommendation the question carries, and **the analysis itself**.

The decision row in the state database keeps the question and the answer.
It cannot keep a session id or a free-form analysis context without a
schema change, and this work does not change schemas (arch decision
point 8) — so the two travel in a **sidecar** next to the plan
(``verification.terminal_decision_session``).  This module pins the
orchestrator's side of that handoff.

What is pinned here
-------------------

1. **A decision writes the session and the analysis.**  The record names
   the decision, carries the session to resume and the analysis that was
   read, and names the failed points the analysis was read against — so
   the file is enough to pick the conversation up again.
2. **A missing session does not cost the analysis.**  A round with no
   session id still writes the analysis in full: the context is worth
   having even when it cannot be resumed (arch decision point 2).
3. **A write that fails never takes the terminal branch down.**  The
   decision was already created and the question already went out; a
   sidecar that could not be written is a fact, not a reason to fail the
   round (arch decision point 5).
4. **A decision that was never created writes nothing.**  There is
   nothing to attach a session to.
5. **The analysis remembers its session.**  The orchestrator keeps the
   session id the judge's last call ran in and the analysis it produced,
   so the record can be written from what the round actually did.

The judge under test is a fake: the real one is a model call, and what is
pinned here is the *bookkeeping* around it.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from verification import terminal_decision_session as tds
from verification.orchestrator import VerificationOrchestrator


# ---------------------------------------------------------------------------
# The fake judge
# ---------------------------------------------------------------------------


class _FakeJudge:
    """A judge that answers a fixed analysis and remembers its session."""

    def __init__(self, analysis, session_id="sess-abc") -> None:
        self.analysis = analysis
        self.last_session_id = session_id
        self.kwargs = None

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return self.analysis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    """The plan directory the orchestrator belongs to."""
    path = tmp_path / "plan"
    path.mkdir()
    return path


@pytest.fixture
def make_orchestrator(plan_dir, tmp_path, monkeypatch):
    """Build an orchestrator with its heavy collaborators stubbed out."""
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )

    def _make(judge=None):
        return VerificationOrchestrator(
            plan_dir=plan_dir,
            project_dir=tmp_path,
            coding_tool=Mock(),
            terminal_judge=judge,
        )

    return _make


def _report():
    """A report with one failed point, as the terminal branch sees it."""
    return {
        "verification_results": [
            {
                "id": "VP-3",
                "status": "FAILED",
                "title": "登录接口",
                "expected_result": "返回 200",
                "failure_class": "deliverable_defect",
                "reason": "expected 200, got 500",
            },
        ],
    }


def _analysis():
    """A well-formed analysis — the six fields the contract asks for."""
    return {
        "context": "第 3 轮终局停止",
        "problem": "0 条修复任务，链路无法自愈",
        "evidence": ["VP-3 连续 3 轮失败"],
        "recommendation": "revise_criterion",
        "recommendation_reason": "同一失败已重复 3 轮",
        "uncertainty": "无法判断是否环境问题",
    }


# ---------------------------------------------------------------------------
# 1. A decision writes the session and the analysis
# ---------------------------------------------------------------------------


def test_record_writes_session_and_analysis(make_orchestrator, plan_dir):
    """The record carries the decision, the session and the analysis."""
    orchestrator = make_orchestrator()
    analysis = _analysis()
    orchestrator._terminal_last_session_id = "sess-abc"
    orchestrator._terminal_last_analysis = analysis

    path = orchestrator._record_terminal_decision_session(
        decision_id="d1",
        analysis=analysis,
        report=_report(),
        round_number=3,
        failed_vp_ids=["VP-3"],
    )

    assert path is not None, "the record was not written"
    assert path == plan_dir / tds.SESSION_FILENAME
    loaded = tds.load(plan_dir)
    assert loaded is not None, "the record cannot be read back"
    assert loaded["decision_id"] == "d1"
    assert loaded["plan_id"] == plan_dir.name
    assert loaded["session_id"] == "sess-abc"
    assert loaded["analysis"] == analysis
    assert loaded["stop_reason"] == "repair_execution_failed"
    assert loaded["round_number"] == 3
    failed = loaded["failed_points"]
    assert len(failed) == 1, f"the failed points were not recorded: {failed!r}"
    assert failed[0]["id"] == "VP-3"


# ---------------------------------------------------------------------------
# 2. A missing session does not cost the analysis
# ---------------------------------------------------------------------------


def test_record_without_session_keeps_full_analysis(make_orchestrator, plan_dir):
    """No session id: the analysis is still written, in full."""
    orchestrator = make_orchestrator()
    analysis = _analysis()
    orchestrator._terminal_last_session_id = None
    orchestrator._terminal_last_analysis = analysis

    orchestrator._record_terminal_decision_session(
        decision_id="d1",
        analysis=analysis,
        report=_report(),
        round_number=3,
        failed_vp_ids=["VP-3"],
    )

    loaded = tds.load(plan_dir)
    assert loaded is not None
    assert loaded["session_id"] is None, (
        f"a session id was invented: {loaded['session_id']!r}"
    )
    assert loaded["analysis"] == analysis
    assert set(loaded["analysis"]) == {
        "context",
        "problem",
        "evidence",
        "recommendation",
        "recommendation_reason",
        "uncertainty",
    }, "the analysis was written incomplete"


# ---------------------------------------------------------------------------
# 3. A write that fails never takes the terminal branch down
# ---------------------------------------------------------------------------


def test_record_save_failure_returns_none_and_does_not_raise(
    make_orchestrator, plan_dir, monkeypatch,
):
    """A sidecar that cannot be written is a fact, not a failed round."""
    orchestrator = make_orchestrator()
    orchestrator._terminal_last_session_id = "sess-abc"

    def _boom(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(tds, "save", _boom)

    result = orchestrator._record_terminal_decision_session(
        decision_id="d1",
        analysis=_analysis(),
        report=_report(),
        round_number=3,
        failed_vp_ids=["VP-3"],
    )

    assert result is None, "a failed write reported a path"
    assert not (plan_dir / tds.SESSION_FILENAME).exists()


# ---------------------------------------------------------------------------
# 4. A decision that was never created writes nothing
# ---------------------------------------------------------------------------


def test_empty_decision_id_writes_nothing(make_orchestrator, plan_dir):
    """No decision, no record — there is nothing to attach a session to."""
    orchestrator = make_orchestrator()
    orchestrator._terminal_last_session_id = "sess-abc"

    result = orchestrator._record_terminal_decision_session(
        decision_id="",
        analysis=_analysis(),
        report=_report(),
        round_number=3,
        failed_vp_ids=["VP-3"],
    )

    assert result is None
    assert not (plan_dir / tds.SESSION_FILENAME).exists()


# ---------------------------------------------------------------------------
# 5. The analysis remembers its session
# ---------------------------------------------------------------------------


def test_analysis_remembers_session_and_payload(make_orchestrator):
    """The judge's session id and its answer are both kept."""
    analysis = _analysis()
    judge = _FakeJudge(analysis, session_id="sess-abc")
    orchestrator = make_orchestrator(judge=judge)

    returned = orchestrator._terminal_analysis(_report(), 3, ["VP-3"], 0)

    assert returned == analysis
    assert orchestrator._terminal_last_session_id == "sess-abc", (
        "the session the analysis came out of was not remembered: "
        f"{orchestrator._terminal_last_session_id!r}"
    )
    assert orchestrator._terminal_last_analysis == analysis


# ---------------------------------------------------------------------------
# 6. The terminal branch records the session after the decision exists
# ---------------------------------------------------------------------------


def test_terminal_branch_records_the_session_after_the_decision():
    """The record is written once the decision exists — never before.

    The order is the whole point: a record written before the decision
    would name a decision that does not exist yet, and the boundary
    above ("no decision, no record") would throw it away.
    """
    import inspect

    source = inspect.getsource(VerificationOrchestrator.check_cycle_conditions)
    asked = source.index("decision_id = self._ask_a_human_about(")
    recorded = source.index("self._record_terminal_decision_session(")

    assert asked < recorded, (
        "the session record must be written after the decision is created"
    )
