"""Unit tests for the terminal judge's session bookkeeping.

The terminal branch of the verification loop asks a human how to proceed.
That question is put together by a judge that makes **one** model call —
and until now the judge threw away the one thing that call produced
besides the answer: *which session* produced it.  Without the session id
the execution phase cannot re-ask the same agent, and the human's answer
to the question is the only thing that survives the call.

What is pinned here
-------------------

1. **A successful call records its session.**  The judge keeps the id of
   the session that produced the analysis, so the caller can continue the
   conversation instead of starting a new one.
2. **A failed call does not leak the previous id.**  Every call starts by
   clearing what the last one recorded: an id that belongs to a *previous*
   answer is worse than no id at all, because the caller would resume a
   session that has nothing to do with the answer it is holding.
3. **An unusable answer keeps its session.**  A payload that breaks the
   contract was still produced *inside a session* — and re-asking inside
   that session is exactly what makes sense, so the id is kept.
4. **A tool without session support is not an error.**  A coding tool that
   never writes an id simply leaves the judge with no id; nothing raises.

The modules under test are imported inside the test bodies on purpose: a
module that does not exist (or does not export the contract) then shows up
as a test FAILURE carrying the real reason, instead of a collection ERROR
that hides which contract broke.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Ensure backend/ is on sys.path so ``import coding_tool`` and
# ``import verification`` work regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _coding_tool():
    """The module that owns the session machinery."""
    try:
        import coding_tool
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"coding_tool is not importable: {exc}")
    return coding_tool


def _terminal_analysis():
    """The module under test."""
    try:
        from verification import terminal_analysis
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"verification.terminal_analysis is not importable: {exc}")
    return terminal_analysis


#: A well-formed answer: the six fields the contract asks for.
_PAYLOAD = {
    "context": "第 3 轮终局停止",
    "problem": "0 条修复任务，链路无法自愈",
    "evidence": ["VP-3 连续 3 轮失败"],
    "recommendation": "revise_criterion",
    "recommendation_reason": "同一失败已重复 3 轮",
    "uncertainty": "无法判断是否环境问题",
}


class _FakeTool:
    """A coding tool that answers with a fixture and writes a session id.

    The id is written the way a real tool writes it: onto the handle the
    caller bound for this call, which the tool reaches through
    ``coding_tool.current_json_session()``.  A tool with
    ``session_id=None`` writes nothing — that is the "tool without
    session support" case.
    """

    def __init__(self, response=None, error=None, session_id="sess-abc") -> None:
        self.calls: list = []
        self.response = response
        self.error = error
        self.session_id = session_id

    def query_json(self, prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.session_id is not None:
            handle = _coding_tool().current_json_session()
            if handle is not None:
                handle.session_id = self.session_id
        if self.error is not None:
            raise self.error
        return self.response


def _judge(tool):
    """A judge wired to ``tool``."""
    return _terminal_analysis().TerminalDecisionJudge(coding_tool=tool)


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
        deferred_count=1,
        failed_points=_points(),
        plan_dir="/tmp/plans/p1",
        project_dir="/tmp/projects/demo",
    )


# ---------------------------------------------------------------------------
# 1. A successful call records its session
# ---------------------------------------------------------------------------


def test_success_records_session_id():
    """The id of the session that produced the analysis is kept."""
    judge = _judge(_FakeTool(response=dict(_PAYLOAD)))

    analysis = _analyze(judge)

    assert analysis is not None, "a well-formed answer was thrown away"
    assert judge.last_session_id == "sess-abc", (
        f"the session id was not recorded: {judge.last_session_id!r}"
    )
    assert judge.last_session is not None, "the session handle was not kept"
    assert judge.last_session.session_id == "sess-abc"


# ---------------------------------------------------------------------------
# 2. A failed call does not leak the previous id
# ---------------------------------------------------------------------------


def test_failure_clears_stale_session_id():
    """A call that fails leaves no id behind — not even the last one's."""
    tool = _FakeTool(response=dict(_PAYLOAD))
    judge = _judge(tool)

    assert _analyze(judge) is not None
    assert judge.last_session_id == "sess-abc"

    # The same judge, the same tool — the second call fails.
    tool.error = RuntimeError("provider is down")

    assert _analyze(judge) is None
    assert judge.last_session_id is None, (
        f"the previous call's session id leaked into the failed call: "
        f"{judge.last_session_id!r}"
    )
    assert judge.last_session is None, (
        "the previous call's session handle leaked into the failed call"
    )


# ---------------------------------------------------------------------------
# 3. An unusable answer keeps its session
# ---------------------------------------------------------------------------


def test_invalid_payload_keeps_session_id():
    """A payload that breaks the contract still came from a session."""
    payload = dict(_PAYLOAD)
    payload["recommendation"] = "delete_plan"
    judge = _judge(_FakeTool(response=payload))

    assert _analyze(judge) is None
    assert judge.last_session_id == "sess-abc", (
        "the session id was dropped even though the session exists — "
        "re-asking inside it is what makes sense"
    )
    assert judge.last_session is not None
    assert judge.last_session.session_id == "sess-abc"


# ---------------------------------------------------------------------------
# 4. A tool without session support is not an error
# ---------------------------------------------------------------------------


def test_tool_without_session_support_yields_none():
    """A tool that writes no id leaves the judge with no id — and no error."""
    judge = _judge(_FakeTool(response=dict(_PAYLOAD), session_id=None))

    analysis = _analyze(judge)

    assert analysis is not None, "the answer was thrown away over a session id"
    assert judge.last_session_id is None
