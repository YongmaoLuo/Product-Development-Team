"""The orchestrator's wiring to the terminal-round analysis judge.

The terminal branch of the verification loop stops and asks a human what
happens next.  The question it asks carries one English sentence of
facts; the other half of the answer is that the failure scene should
also be **analysed** — read by a judge, which says what the failure
means and which of the three actions it recommends.

This module pins the wiring between the orchestrator and that judge:

1. **Every fact is handed over.**  The judge is told the plan, the
   round, why the round stopped, how many repair tasks it produced, how
   many points were deferred and which points failed (with their facts).
   A judge that receives only some of them reasons about a scene that is
   not the one that happened.
2. **Only ``DEFERRED`` counts as "not executed".**  ``SKIPPED`` and
   ``SPLIT`` are terminal successes and ``BLOCKED`` is a failure — none
   of them is a point that did not run.  The count is per verification
   point: the same point recorded twice counts once.
3. **A broken analysis never keeps the question from being asked.**  The
   judge may raise, or answer with something that is not an answer; both
   degrade to "no analysis" (``None``) rather than to an exception that
   would stop the terminal branch before it reaches the human.
4. **The judge is built on first use.**  It costs one model call and
   most rounds never stop, so the common path must not pay for it; when
   it is built, it is built on the orchestrator's own coding tool.

The judge under test is a fake: the real one is a model call, and what
is pinned here is the *wiring* — which facts are handed over and what
happens when the wiring breaks.
"""

from unittest.mock import Mock

import pytest

from verification.orchestrator import VerificationOrchestrator


# ---------------------------------------------------------------------------
# The fake judge
# ---------------------------------------------------------------------------


class _FakeJudge:
    """A judge that records the call it was given and answers a fixed thing."""

    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return self.result


class _RaisingJudge:
    """A judge whose call blows up."""

    def analyze(self, **kwargs):
        raise RuntimeError("the model call exploded")


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
    """Build an orchestrator with its heavy collaborators stubbed out.

    The judge is a parameter, so the same builder serves the tests that
    inject a fake judge and the one that watches the real judge being
    built on first use.
    """
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )

    def _make(judge=None, coding_tool=None):
        return VerificationOrchestrator(
            plan_dir=plan_dir,
            project_dir=tmp_path,
            coding_tool=Mock() if coding_tool is None else coding_tool,
            terminal_judge=judge,
        )

    return _make


def _report(**overrides):
    """A report with one failed point, as the terminal branch sees it."""
    report = {
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
    report.update(overrides)
    return report


# ---------------------------------------------------------------------------
# 1. Every fact is handed over
# ---------------------------------------------------------------------------


def test_analysis_passes_all_facts_to_judge(make_orchestrator):
    """The judge is told the plan, the round, the stop and the failures."""
    judge = _FakeJudge({
        "recommendation": "accept_as_failed",
        "reason": "同一失败重复 3 轮",
    })
    orchestrator = make_orchestrator(judge=judge)
    report = _report()

    analysis = orchestrator._terminal_analysis(report, 3, ["VP-3"], 0)

    assert analysis == {
        "recommendation": "accept_as_failed",
        "reason": "同一失败重复 3 轮",
    }, "the judge's answer did not come back"
    kwargs = judge.kwargs
    assert kwargs is not None, "the judge was never asked"
    assert kwargs["plan_id"] == orchestrator.plan_dir.name
    assert kwargs["round_number"] == 3
    assert kwargs["stop_reason"] == "repair_execution_failed"
    assert kwargs["repair_task_count"] == 0
    assert kwargs["deferred_count"] == 0, "no point was deferred in this report"
    failed_points = kwargs["failed_points"]
    assert len(failed_points) == 1
    assert failed_points[0]["id"] == "VP-3"
    assert failed_points[0]["failure_class"] == "deliverable_defect"


def test_analysis_counts_the_deferred_points_it_hands_over(make_orchestrator):
    """The deferred count the judge is told is the report's own count."""
    judge = _FakeJudge({"recommendation": "rerun_this_round"})
    orchestrator = make_orchestrator(judge=judge)
    report = _report(
        verification_results=[
            {"id": "VP-3", "status": "FAILED", "reason": "boom"},
            {"id": "VP-9", "status": "DEFERRED"},
        ],
    )

    orchestrator._terminal_analysis(report, 2, ["VP-3"], 4)

    assert judge.kwargs["deferred_count"] == 1
    assert judge.kwargs["repair_task_count"] == 4


# ---------------------------------------------------------------------------
# 2. Only DEFERRED counts as "not executed"
# ---------------------------------------------------------------------------


def test_deferred_count_counts_distinct_points(make_orchestrator):
    """Two records of the same deferred point count once; the rest do not."""
    orchestrator = make_orchestrator()
    report = {
        "verification_results": [
            {"id": "VP-1", "status": "DEFERRED"},
            {"id": "VP-1", "status": "DEFERRED"},
            {"id": "VP-2", "status": "SKIPPED"},
            {"id": "VP-3", "status": "BLOCKED"},
            {"id": "VP-4", "status": "SPLIT"},
        ],
    }

    assert orchestrator._terminal_deferred_count(report) == 1


def test_deferred_count_is_zero_without_deferred_points(make_orchestrator):
    """A round where every point ran has nothing deferred."""
    orchestrator = make_orchestrator()
    report = {
        "verification_results": [
            {"id": "VP-1", "status": "PASSED"},
            {"id": "VP-2", "status": "FAILED"},
            {"id": "VP-3", "status": "SKIPPED"},
        ],
    }

    assert orchestrator._terminal_deferred_count(report) == 0


# ---------------------------------------------------------------------------
# 3. A broken analysis never keeps the question from being asked
# ---------------------------------------------------------------------------


def test_judge_exception_returns_none(make_orchestrator):
    """A judge that raises yields no analysis — and no exception."""
    orchestrator = make_orchestrator(judge=_RaisingJudge())

    analysis = orchestrator._terminal_analysis(_report(), 3, ["VP-3"], 0)

    assert analysis is None


def test_judge_non_mapping_returns_none(make_orchestrator):
    """An answer that is not an answer yields no analysis."""
    orchestrator = make_orchestrator(judge=_FakeJudge("oops"))

    analysis = orchestrator._terminal_analysis(_report(), 3, ["VP-3"], 0)

    assert analysis is None


# ---------------------------------------------------------------------------
# 4. The judge is built on first use
# ---------------------------------------------------------------------------


def test_judge_is_built_lazily_from_coding_tool(make_orchestrator):
    """No judge is built until one is asked for — and it uses our tool."""
    orchestrator = make_orchestrator()
    assert orchestrator._terminal_judge is None, (
        "the judge was built while the orchestrator was constructed"
    )

    judge = orchestrator._terminal_judge_for()

    from verification.terminal_analysis import TerminalDecisionJudge

    assert isinstance(judge, TerminalDecisionJudge)
    assert judge.coding_tool is orchestrator.coding_tool
    assert orchestrator._terminal_judge_for() is judge, (
        "the judge was built again instead of being kept"
    )


def test_injected_judge_is_the_one_used(make_orchestrator):
    """The injected judge is the judge that is asked."""
    judge = _FakeJudge({"recommendation": "accept_as_failed"})
    orchestrator = make_orchestrator(judge=judge)

    assert orchestrator._terminal_judge_for() is judge