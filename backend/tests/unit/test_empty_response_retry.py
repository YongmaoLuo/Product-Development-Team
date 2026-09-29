"""C2 (2026-09-17): an empty subagent response must not burn a retry.

Motivation: an attempt finished in
0 seconds with a 0-char response (provider returned nothing). The
executor scored it as a normal attempt — "AI did not report test
result" — and the task failed seconds later, after the previous attempt
had already spent real time on real work. The last retry slot was gone.

Contract pinned here:

* A blank/whitespace response triggers ``_retry_empty_query``, which
  re-queries up to ``_MAX_EMPTY_OUTPUT_RETRIES`` times and returns the
  first non-blank answer.
* Every empty reply logs ``task_api_error_empty_output`` (distinguishable
  from a real failed attempt in execution.log).
* Recovery logs ``task_empty_output_recovered``.
* When the budget is exhausted the helper returns "" and the caller's
  pre-existing failure path is unchanged (no behaviour change for a
  genuinely broken provider).
* A retry that raises must not propagate — the helper degrades to the
  last empty response.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task import SubTask  # noqa: E402


class _LogRecorder:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str):
        def _fn(event: str, message: str, **kwargs: Any) -> None:
            self.events.append({
                "level": level, "event": event, "message": message,
                "task_id": kwargs.get("task_id"), "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name: str) -> Any:
        if name in ("info", "warning", "error", "debug"):
            return self._record(name)
        raise AttributeError(name)

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


class _StubCodingTool:
    """Returns a scripted sequence; records every prompt it was given."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def query(self, prompt, system_instruction=None, model_type=None, scene=None):
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            "model_type": model_type,
            "scene": scene,
        })
        item = self.responses.pop(0) if self.responses else ""
        if isinstance(item, Exception):
            raise item
        return item


class _StubAgent:
    _MAX_EMPTY_OUTPUT_RETRIES = AutonomousAgent._MAX_EMPTY_OUTPUT_RETRIES
    _retry_empty_query = AutonomousAgent._retry_empty_query

    def __init__(self, coding_tool, logger=None):
        self.coding_tool = coding_tool
        self.logger = logger


def _task() -> SubTask:
    return SubTask(
        id="repair-r3-06-3",
        title="t",
        description="d",
        model_type="complex",
    )


def test_empty_then_real_response_recovers() -> None:
    tool = _StubCodingTool(["", "   \n", "real answer\nTEST_RESULT: FAILED"])
    agent = _StubAgent(tool)
    agent.logger = _LogRecorder()

    out = agent._retry_empty_query("ctx", "sys", _task(), 2)

    assert out == "real answer\nTEST_RESULT: FAILED"
    assert len(tool.calls) == 3
    assert len(agent.logger.events_named("task_api_error_empty_output")) == 2
    assert agent.logger.events_named("task_empty_output_recovered")
    # Scene/model routing must be preserved on the retry.
    assert tool.calls[0]["scene"] == "execution"
    assert tool.calls[0]["model_type"] == "complex"


def test_all_empty_returns_blank_after_budget() -> None:
    tool = _StubCodingTool([])  # always ""
    agent = _StubAgent(tool)
    agent.logger = _LogRecorder()

    out = agent._retry_empty_query("ctx", "sys", _task(), 1)

    assert out == ""
    assert len(tool.calls) == AutonomousAgent._MAX_EMPTY_OUTPUT_RETRIES
    assert len(agent.logger.events_named("task_api_error_empty_output")) == (
        AutonomousAgent._MAX_EMPTY_OUTPUT_RETRIES
    )


def test_retry_exception_degrades_without_raising() -> None:
    tool = _StubCodingTool([RuntimeError("provider died")])
    agent = _StubAgent(tool)
    agent.logger = _LogRecorder()

    out = agent._retry_empty_query("ctx", "sys", _task(), 2)

    assert out == ""
    assert agent.logger.events_named("task_api_error_empty_output_retry_failed")


def test_first_response_not_re_queried_by_helper() -> None:
    """The helper is only reached when the first response was blank."""
    source = (BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    assert (
        "if coder_response is not None and not coder_response.strip():" in source
    ), "the empty-response guard is no longer wired into the retry loop"
    assert "coder_response = self._retry_empty_query(" in source
