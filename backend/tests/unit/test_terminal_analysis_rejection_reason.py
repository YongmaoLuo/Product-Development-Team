"""TDD spec: the terminal judge must say *why* it produced no analysis.

Background
----------
The terminal round of the verification loop stops and asks a human what
happens next.  Half of the question is the **analysis**: a judge's
reading of the failure scene.  When that analysis is missing, the human
is asked a bare sentence of facts that names no failure point — and
the reason the analysis was missing never left the judge: it logged a
warning and returned ``None``.

``None`` says *that* there is no analysis, not *why*.  A card that has
to render the fallback has to name the reason: the provider call
raised, the answer was not JSON, the evidence field was not a list of
strings.  So the judge keeps the reason on itself
(:attr:`TerminalDecisionJudge.last_analysis_error`) and the orchestrator
carries it over
(:attr:`VerificationOrchestrator._terminal_last_analysis_error`).

What this file pins down
------------------------

1. A refused answer names the check that failed:
   ``analysis_rejected:<reason>``.
2. An answer that is not there at all reads as ``analysis_empty`` — a
   raising call, an answer that is not a JSON object, a ``None``.
3. A good answer clears the reason: ``""``.
4. The orchestrator keeps the judge's reason, and reads
   ``analysis_empty`` when the judge cannot be asked (an old judge, a
   fake in a test).

The judge's fake tool is the one the judge-contract file already uses
(``test_terminal_analysis_judge_contract._FakeTool``): a recorder that
answers with a fixture or raises.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The fake tool the judge-contract file already uses — one recorder,
# two files, so the two can never drift apart.  It is reached through
# the package path on purpose: ``tests/unit`` holds a test file named
# ``test_command_quality.py`` that shadows the production module of
# the same name, so putting this directory on ``sys.path`` would break
# every importer of the real one.
from tests.unit.test_terminal_analysis_judge_contract import (  # noqa: E402
    _FakeTool,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


#: A well-formed answer: the six fields the contract asks for.
_PAYLOAD = {
    "context": "第 3 轮终局停止",
    "problem": "0 条修复任务，链路无法自愈",
    "evidence": ["VP-3 连续 3 轮失败"],
    "recommendation": "revise_criterion",
    "recommendation_reason": "同一失败已重复 3 轮",
    "uncertainty": "缺少最近一次实现的 diff",
}

#: The reason the judge gives when the answer breaks the contract.
_REJECTED_REASON = "analysis_rejected:evidence must be a list of strings"

#: The reason when there is no answer at all.
_EMPTY_REASON = "analysis_empty"


def _judge(tool):
    """A judge wired to ``tool``."""
    from verification.terminal_analysis import TerminalDecisionJudge

    return TerminalDecisionJudge(coding_tool=tool)


def _points():
    """One failed verification point."""
    return [
        {
            "id": "VP-3",
            "title": "登录接口",
            "expected_result": "返回 200",
            "failure_class": "deliverable_defect",
            "consecutive_rounds": 3,
            "verdict": "AssertionError: expected 200, got 500",
        }
    ]


def _analyze(judge):
    """Run one analysis over the sample scene."""
    return judge.analyze(
        plan_id="p1",
        round_number=3,
        stop_reason="repair_execution_failed",
        repair_task_count=0,
        deferred_count=4,
        failed_points=_points(),
        plan_dir="/tmp/plans/p1",
        project_dir="/tmp/projects/demo",
    )


# ---------------------------------------------------------------------------
# 1. The judge names the check that failed
# ---------------------------------------------------------------------------


def test_rejected_payload_sets_the_reason():
    """An answer that breaks the contract says which check broke it."""
    payload = dict(_PAYLOAD)
    payload["evidence"] = []
    judge = _judge(_FakeTool(response=payload))

    analysis = _analyze(judge)

    assert analysis is None, "an answer with no evidence was accepted"
    assert judge.last_analysis_error == _REJECTED_REASON, (
        "the judge did not say which check the answer failed: "
        f"{judge.last_analysis_error!r}"
    )


# ---------------------------------------------------------------------------
# 2. No answer at all reads as "empty"
# ---------------------------------------------------------------------------


def test_unparseable_answer_reads_as_empty():
    """An answer that is not JSON names no check — there was no answer."""
    judge = _judge(_FakeTool(response="not json"))

    analysis = _analyze(judge)

    assert analysis is None, "an unparseable answer was accepted"
    assert judge.last_analysis_error == _EMPTY_REASON, (
        "the judge did not say the answer was missing: "
        f"{judge.last_analysis_error!r}"
    )


def test_raising_call_reads_as_empty():
    """A provider that raises produces an empty answer, not a crash."""
    judge = _judge(_FakeTool(error=RuntimeError("provider is down")))

    analysis = _analyze(judge)

    assert analysis is None, "a raising call produced an analysis"
    assert judge.last_analysis_error == _EMPTY_REASON, (
        "the judge did not say the answer was missing: "
        f"{judge.last_analysis_error!r}"
    )


def test_none_answer_reads_as_empty():
    """A ``None`` answer is no answer."""
    judge = _judge(_FakeTool(response=None))

    analysis = _analyze(judge)

    assert analysis is None, "a ``None`` answer was accepted"
    assert judge.last_analysis_error == _EMPTY_REASON, (
        "the judge did not say the answer was missing: "
        f"{judge.last_analysis_error!r}"
    )


# ---------------------------------------------------------------------------
# 3. A good answer clears the reason
# ---------------------------------------------------------------------------


def test_valid_answer_clears_the_reason():
    """A good answer leaves no reason behind."""
    judge = _judge(_FakeTool(response=dict(_PAYLOAD)))

    analysis = _analyze(judge)

    assert analysis is not None, "a well-formed answer was thrown away"
    assert judge.last_analysis_error == "", (
        "a good answer left a reason behind: "
        f"{judge.last_analysis_error!r}"
    )


# ---------------------------------------------------------------------------
# 4. The orchestrator carries the reason over
# ---------------------------------------------------------------------------


class _FakeJudge:
    """A judge that answers with a fixed thing and keeps a reason.

    ``reason=None`` means the judge does not keep one at all — an old
    judge, or a fake that never heard of the attribute.  The
    orchestrator has to survive that.
    """

    def __init__(self, result=None, reason=None):
        self.result = result
        self.kwargs = None
        if reason is not None:
            self.last_analysis_error = reason

    def analyze(self, **kwargs):
        self.kwargs = kwargs
        return self.result


@pytest.fixture
def make_orchestrator(tmp_path, monkeypatch):
    """Build an orchestrator with its heavy collaborators stubbed out."""
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    def _make(judge=None):
        from verification.orchestrator import VerificationOrchestrator

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


def test_orchestrator_keeps_the_judges_reason(make_orchestrator):
    """The reason the judge gave is the reason the orchestrator keeps."""
    judge = _FakeJudge(result=None, reason=_REJECTED_REASON)
    orchestrator = make_orchestrator(judge=judge)

    orchestrator._terminal_analysis(_report(), 3, ["VP-3"], 0)

    assert orchestrator._terminal_last_analysis_error == _REJECTED_REASON, (
        "the orchestrator did not keep the judge's reason: "
        f"{orchestrator._terminal_last_analysis_error!r}"
    )


def test_orchestrator_defaults_to_analysis_empty(make_orchestrator):
    """A judge that cannot be asked reads as "there was no analysis"."""
    judge = _FakeJudge(result=None)
    orchestrator = make_orchestrator(judge=judge)

    orchestrator._terminal_analysis(_report(), 3, ["VP-3"], 0)

    assert orchestrator._terminal_last_analysis_error == _EMPTY_REASON, (
        "the orchestrator did not fall back to the empty reason: "
        f"{orchestrator._terminal_last_analysis_error!r}"
    )
