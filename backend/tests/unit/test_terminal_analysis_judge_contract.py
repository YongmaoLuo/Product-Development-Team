"""Unit tests for the terminal-round analysis *call* (``verification.terminal_analysis``).

``tests/unit/test_terminal_analysis_judge.py`` pins the prompt half: what
the judge is shown, and what the vocabulary is.  This file pins the call
half: what the judge does with the answer, and — above all — what it does
when the answer is unusable.

What is pinned here
-------------------

1. **A good answer comes back whole.**  Six fields, exactly those six,
   with the model's own text (stripped) and nothing invented on top.
2. **One call, read-only tools.**  The judge is an investigator that
   may read but not write: it calls the model once, with the read-only
   tool set (``Bash`` / ``Read`` / ``Grep`` / ``Glob``), with the
   timeout the module declares.
3. **Every failure is a downgrade, never an exception.**  A provider
   that raises, an answer that is not JSON, a payload that is not an
   object, a recommendation outside the three actions, a missing field —
   each of them yields ``None``.  The caller's fallback is a human
   question with the facts in it, and the judge must never be the
   reason that question does not get asked.

The module under test is imported inside the test bodies on purpose: a
module that does not exist (or does not export the contract) then shows
up as a test FAILURE carrying the real reason, instead of a collection
ERROR that hides which contract broke.
"""

from __future__ import annotations

import json
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
    "uncertainty": "无法判断是否环境问题",
}

#: The six keys the analysis is allowed to have.
_EXPECTED_KEYS = {
    "context",
    "problem",
    "evidence",
    "recommendation",
    "recommendation_reason",
    "uncertainty",
}


class _FakeTool:
    """A coding tool that records its calls and answers with a fixture.

    ``error`` makes the call raise — the way a provider that is out of
    quota, or a CLI that died, answers.
    """

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response
        self._error = error

    def query_json(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


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
        deferred_count=4,
        failed_points=_points(),
        plan_dir="/tmp/plans/p1",
        project_dir="/tmp/projects/demo",
    )


# ---------------------------------------------------------------------------
# 1. A good answer comes back whole
# ---------------------------------------------------------------------------


def test_success_returns_six_fields():
    """A well-formed answer comes back as the six contracted fields."""
    judge = _judge(_FakeTool(response=dict(_PAYLOAD)))

    analysis = _analyze(judge)

    assert analysis is not None, "a well-formed answer was thrown away"
    assert set(analysis) == _EXPECTED_KEYS, (
        f"the analysis does not carry exactly the six contracted keys: "
        f"{sorted(analysis)}"
    )
    assert analysis["context"] == _PAYLOAD["context"]
    assert analysis["problem"] == _PAYLOAD["problem"]
    assert analysis["evidence"] == _PAYLOAD["evidence"]
    assert analysis["recommendation"] == _PAYLOAD["recommendation"]
    assert analysis["recommendation_reason"] == _PAYLOAD["recommendation_reason"]
    assert analysis["uncertainty"] == _PAYLOAD["uncertainty"]


def test_success_accepts_a_json_string_answer():
    """An answer that arrives as raw JSON text is read, not discarded."""
    judge = _judge(_FakeTool(response=json.dumps(_PAYLOAD, ensure_ascii=False)))

    analysis = _analyze(judge)

    assert analysis is not None, "a JSON string answer was thrown away"
    assert analysis["recommendation"] == "revise_criterion"
    assert analysis["evidence"] == _PAYLOAD["evidence"]


def test_text_is_stripped():
    """The text comes back stripped — the model pads, the contract does not."""
    padded = dict(_PAYLOAD)
    padded["context"] = "  第 3 轮终局停止  "
    padded["evidence"] = ["  VP-3 连续 3 轮失败  "]
    judge = _judge(_FakeTool(response=padded))

    analysis = _analyze(judge)

    assert analysis is not None
    assert analysis["context"] == "第 3 轮终局停止"
    assert analysis["evidence"] == ["VP-3 连续 3 轮失败"]


def test_extra_keys_are_not_passed_through():
    """An answer with extra keys yields the six contracted keys, no more."""
    payload = dict(_PAYLOAD)
    payload["markdown"] = "```json\n{}\n```"
    judge = _judge(_FakeTool(response=payload))

    analysis = _analyze(judge)

    assert analysis is not None
    assert set(analysis) == _EXPECTED_KEYS


# ---------------------------------------------------------------------------
# 2. One call, read-only tools
# ---------------------------------------------------------------------------


def test_single_call_uses_read_only_tools():
    """One call, the read-only investigation tools, and the declared timeout."""
    tool = _FakeTool(response=dict(_PAYLOAD))
    judge = _judge(tool)

    _analyze(judge)

    assert len(tool.calls) == 1, (
        f"the judge made {len(tool.calls)} calls; it is one call, not a loop"
    )
    call = tool.calls[0]
    allowed = list(call["allowed_tools"])
    for tool_name in ("Read", "Grep", "Glob", "Bash"):
        assert tool_name in allowed, (
            f"the judge cannot {tool_name}; it cannot investigate the "
            f"failure it was handed"
        )
    for tool_name in ("Edit", "Write", "NotebookEdit"):
        assert tool_name not in allowed, (
            f"the judge was given {tool_name}; the analysis is a reading, "
            f"not a repair"
        )
    assert call["timeout"] == 600, "the judge used another timeout"
    assert call["prompt"], "the judge called the model without a prompt"
    assert call["system_instruction"], "the judge called the model without rules"


# ---------------------------------------------------------------------------
# 3. Every failure is a downgrade, never an exception
# ---------------------------------------------------------------------------


def test_provider_exception_returns_none():
    """A provider that raises is a downgrade, not a crash."""
    judge = _judge(_FakeTool(error=RuntimeError("provider is down")))

    assert _analyze(judge) is None


def test_bad_json_returns_none():
    """An answer that is not JSON is a downgrade."""
    judge = _judge(_FakeTool(response="not json"))

    assert _analyze(judge) is None


def test_non_mapping_payload_returns_none():
    """An answer that is not an object is a downgrade."""
    judge = _judge(_FakeTool(response=[1, 2]))

    assert _analyze(judge) is None


def test_invalid_recommendation_returns_none():
    """A recommendation outside the three actions is a downgrade."""
    payload = dict(_PAYLOAD)
    payload["recommendation"] = "delete_plan"
    judge = _judge(_FakeTool(response=payload))

    assert _analyze(judge) is None


def test_missing_field_returns_none():
    """A missing field is a downgrade."""
    payload = dict(_PAYLOAD)
    del payload["uncertainty"]
    judge = _judge(_FakeTool(response=payload))

    assert _analyze(judge) is None


def test_empty_evidence_returns_none():
    """An empty evidence list is a downgrade — there is no analysis without it."""
    payload = dict(_PAYLOAD)
    payload["evidence"] = []
    judge = _judge(_FakeTool(response=payload))

    assert _analyze(judge) is None


def test_evidence_of_non_strings_returns_none():
    """Evidence that is not a list of strings is a downgrade."""
    payload = dict(_PAYLOAD)
    payload["evidence"] = [{"id": "VP-3"}]
    judge = _judge(_FakeTool(response=payload))

    assert _analyze(judge) is None


def test_blank_text_field_returns_none():
    """A field that is present but says nothing is a downgrade."""
    payload = dict(_PAYLOAD)
    payload["problem"] = "   "
    judge = _judge(_FakeTool(response=payload))

    assert _analyze(judge) is None


def test_failure_never_raises():
    """Whatever the answer is, the judge returns — it does not raise."""
    for response in (None, 42, "{}", [], {"recommendation": "rerun_this_round"}):
        judge = _judge(_FakeTool(response=response))
        result = _analyze(judge)
        assert result is None or set(result) == _EXPECTED_KEYS
