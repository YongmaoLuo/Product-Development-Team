"""Unit tests for the default port's free-text resume path.

Background
----------
The terminal round of the verification loop asks a human what to do
next.  When the human picks one of the three options the port carries
that option out mechanically.  When the human writes **free text**,
nobody knows what they meant — so the *analysis agent* has to read it,
in the conversation it already had about the failure.

That conversation is the point.  The analysis call recorded which
session it ran in (task 8) and the sidecar keeps that id next to the
analysis (task 9).  This file pins the port half of that:

1. **A recorded session id is re-opened.**  The handle the tool sees
   carries that id and says it was *resumed*.
2. **No session id degrades to a fresh session — with the full analysis
   carried in the prompt.**  The context the agent would have had is
   put into the prompt instead, so a fresh conversation still starts
   from the same facts.
3. **The human's own words reach the prompt.**
4. **A missing record degrades, it does not raise.**
5. **An injected tool is used as-is** — no provider is built behind the
   caller's back.

Two more are pinned because they are load-bearing and cheap to state:
an empty note still leaves a placeholder section (an empty note must
not produce a sentence with nothing in it), and a provider failure is
**not** swallowed here — the executor is what audits it.

Test command::

    pytest backend/tests/unit/test_terminal_decision_port_agent.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Ensure backend/ is on sys.path so ``import coding_tool`` and
# ``import hitl.terminal_decision_port`` work regardless of the test
# runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _port_module():
    """The module under test.

    Imported inside the test bodies so a missing module is a FAILURE
    with the real reason rather than a collection ERROR.
    """
    try:
        from hitl import terminal_decision_port
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"hitl.terminal_decision_port is not importable: {exc}")
    return terminal_decision_port


#: An analysis context, as the judge would have written it.
_ANALYSIS = {
    "context": "第 3 轮验证停在同一个失败点上",
    "problem": "自动修复没有产出任何修复任务",
    "evidence": ["VP-3 连续三轮拿到同一个 500"],
}

#: The failed points the analysis was read against.
_FAILED_POINTS = [
    {"id": "VP-3", "verdict": "expected 200, got 500"},
]

#: The human's own words.
_NOTE = "先别重跑，看看是不是环境问题"


class _FakeTool:
    """A coding tool that records the session handle bound for each call.

    The handle is read the way a real tool reads it: through
    ``coding_tool.current_json_session()``, *inside* the call.  The
    handle object survives the call, so the test can read its id and
    stage afterwards.
    """

    def __init__(self, response=None, error=None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.handles: List[Any] = []
        self.response = {"ok": True} if response is None else response
        self.error = error

    def query_json(self, prompt=None, **kwargs):
        import coding_tool

        handle = coding_tool.current_json_session()
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        self.handles.append(handle)
        if self.error is not None:
            raise self.error
        return self.response

    @property
    def prompt(self) -> str:
        """The prompt of the single call this tool received."""
        assert self.calls, "the tool was never called"
        return self.calls[0]["prompt"] or ""


def _section_body(prompt: str, heading: str) -> str:
    """The body of the section under ``heading``.

    A section runs until the next ``## `` heading, so a body that is
    only whitespace means the section said nothing.
    """
    marker = f"## {heading}"
    assert marker in prompt, f"the prompt has no {marker!r} section:\n{prompt}"
    rest = prompt.split(marker, 1)[1]
    lines: List[str] = []
    for line in rest.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. A recorded session id is re-opened
# ---------------------------------------------------------------------------


def test_resume_uses_recorded_session_id():
    """The recorded session is re-opened, and the handle says so."""
    module = _port_module()
    tool = _FakeTool()
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)

    port.resume_agent(
        "p1",
        {
            "session_id": "sess-abc",
            "analysis": dict(_ANALYSIS),
            "failed_points": list(_FAILED_POINTS),
        },
        _NOTE,
    )

    assert len(tool.calls) == 1, (
        f"the resume must make exactly one call; it made {len(tool.calls)}"
    )
    handle = tool.handles[0]
    assert handle is not None, "no session handle was bound for the call"
    assert handle.session_id == "sess-abc", (
        "the recorded session was not re-opened; the handle carried "
        f"{handle.session_id!r}"
    )
    assert handle.stage == "resumed", (
        "the handle must say the session was resumed; it said "
        f"{handle.stage!r}"
    )


# ---------------------------------------------------------------------------
# 2. No session id degrades to a fresh session, with the full analysis
# ---------------------------------------------------------------------------


def test_missing_session_opens_fresh_session_with_full_analysis():
    """Without an id a new session opens — and carries the whole analysis."""
    module = _port_module()
    tool = _FakeTool()
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)
    analysis = dict(_ANALYSIS)

    port.resume_agent(
        "p1",
        {"analysis": analysis, "failed_points": list(_FAILED_POINTS)},
        _NOTE,
    )

    assert len(tool.calls) == 1, (
        f"the resume must make exactly one call; it made {len(tool.calls)}"
    )
    handle = tool.handles[0]
    assert handle is not None, "no session handle was bound for the call"
    assert handle.session_id is None, (
        "there was no session id to re-open; the handle carried "
        f"{handle.session_id!r}"
    )
    assert handle.stage is None, (
        f"a fresh session carries no stage; the handle said {handle.stage!r}"
    )
    assert json.dumps(analysis, ensure_ascii=False) in tool.prompt, (
        "the full analysis context must be carried into the prompt of the "
        "new session"
    )


# ---------------------------------------------------------------------------
# 3. The human's own words reach the prompt
# ---------------------------------------------------------------------------


def test_prompt_carries_note():
    """The free text the human wrote is in the prompt, verbatim."""
    module = _port_module()
    tool = _FakeTool()
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)

    port.resume_agent(
        "p1",
        {"session_id": "sess-abc", "analysis": dict(_ANALYSIS)},
        _NOTE,
    )

    assert _NOTE in tool.prompt, (
        "the human's own words must reach the prompt"
    )


# ---------------------------------------------------------------------------
# 4. A missing record degrades, it does not raise
# ---------------------------------------------------------------------------


def test_none_record_does_not_raise():
    """No record at all: one call, a fresh session, no exception."""
    module = _port_module()
    tool = _FakeTool()
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)

    port.resume_agent("p1", None, _NOTE)

    assert len(tool.calls) == 1, (
        "a missing record must still produce one call; it produced "
        f"{len(tool.calls)}"
    )
    handle = tool.handles[0]
    assert handle is not None, "no session handle was bound for the call"
    assert handle.session_id is None, (
        "a missing record has no session to re-open; the handle carried "
        f"{handle.session_id!r}"
    )
    assert "分析上下文不可用" in tool.prompt, (
        "the prompt must say that the analysis context is unavailable"
    )


# ---------------------------------------------------------------------------
# 5. An injected tool is used as-is
# ---------------------------------------------------------------------------


def test_coding_tool_is_injected(monkeypatch):
    """An injected tool is the tool that gets called — nothing is built."""
    import coding_tool

    def _must_not_be_built(*args, **kwargs):
        raise AssertionError(
            "an injected tool must not be replaced by a freshly built one"
        )

    monkeypatch.setattr(coding_tool, "ClaudeCodingTool", _must_not_be_built)

    module = _port_module()
    tool = _FakeTool()
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)

    port.resume_agent("p1", {"session_id": "sess-abc"}, _NOTE)

    assert len(tool.calls) == 1, (
        f"the injected tool was called {len(tool.calls)} times"
    )


def test_missing_tool_is_built_lazily_on_the_verification_scene(monkeypatch):
    """Without an injected tool, one is built — on the verification scene."""
    import coding_tool

    built: List[Dict[str, Any]] = []

    class _Recorder:
        def __init__(self, *args, **kwargs):
            built.append(kwargs)

        def query_json(self, prompt=None, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(coding_tool, "ClaudeCodingTool", _Recorder)

    module = _port_module()
    port = module.DefaultTerminalDecisionPort()

    port.resume_agent("p1", {"session_id": "sess-abc"}, _NOTE)

    assert len(built) == 1, f"the tool was built {len(built)} times"
    assert built[0].get("scene") == "verification", (
        "the tool must be built on the verification scene; it was built with "
        f"{built[0]!r}"
    )


# ---------------------------------------------------------------------------
# 6. Two smaller guarantees
# ---------------------------------------------------------------------------


def test_empty_note_keeps_a_placeholder_section():
    """An empty note leaves a placeholder, not a sentence with nothing in it."""
    module = _port_module()
    tool = _FakeTool()
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)

    port.resume_agent(
        "p1",
        {"session_id": "sess-abc", "analysis": dict(_ANALYSIS)},
        "",
    )

    body = _section_body(tool.prompt, "人工的自由文本回答")
    assert body.strip(), (
        "an empty note must still leave a placeholder section; the section "
        f"was empty:\n{tool.prompt!r}"
    )
    assert not body.rstrip().endswith(("：", ":")), (
        "an empty note must not produce a sentence with nothing in it; the "
        f"section was {body!r}"
    )


def test_provider_failure_is_not_swallowed():
    """The port does not eat the failure — the executor is what audits it."""
    module = _port_module()
    tool = _FakeTool(error=RuntimeError("provider is down"))
    port = module.DefaultTerminalDecisionPort(coding_tool=tool)

    with pytest.raises(RuntimeError):
        port.resume_agent("p1", {"session_id": "sess-abc"}, _NOTE)