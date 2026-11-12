"""A verification attempt must run in a session of its OWN.

2026-11-08 (VP-028 · provenance level). The round-24 attempt that was
supposed to judge VP-028 came back with a verdict about VP-038, and the
verdict was accepted because it looked like a verdict.

The reason it could happen is a dispatch with no session of its own:
``verification_subagent`` called ``scoped_tool.query_json`` with no
handle, so the tool picked whichever session it liked, and a resumed
turn carried no identity — just "your previous turn was cut off, carry
on". Whatever conversation the tool landed in, the sub-agent continued
*that* one, and answered whatever question it held.

What this pins
--------------
1. Every attempt is bound to its own pinned session — one id, created
   by the attempt, never shared with another attempt or another
   verification point.
2. The dispatch goes through that binding, so the tool is running in the
   session the attempt named.
3. The attempt log says which session the attempt asked for and which
   session actually answered, so an attempt can be traced back to a
   conversation.
4. The session carries an identity anchor naming the verification point
   it is judging, because the transcript alone cannot supply one.
5. A verdict that no session produced is not a PASSED. It is an
   unattributable answer, and an unattributable answer is an infra
   failure — never evidence that the deliverable is good.

Where the assertion is
----------------------
At the dispatch: the tool reads the bound handle out of the context
variable the binding sets, so "the attempt ran in its own session" is a
statement about what the sub-agent was really handed, not about what
the caller passed along.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    current_json_session,
)
from verification_subagent import VerificationSubAgent  # noqa: E402


VP_NODE = {
    "id": "VP-028",
    "title": "延后关卡重算",
    "verification_method": "code_review",
    "test_command": "pytest -q",
    "expected_result": "the deferred gate layer is re-run, not left deferred",
}

PASSED_PAYLOAD = {
    "verdict": "PASSED",
    "reasons": ["the deferred gate layer was re-run"],
    "evidence": ["backend/verification_executor.py:120"],
}


async def _no_sleep(_delay: float) -> None:
    """Backoff stand-in — the retry *pacing* is not what this pins."""
    return None


class _CapturingTool:
    """A coding-tool double that records the session it was handed.

    It reads the handle out of the context variable the binding sets, so
    what it records is the session the dispatch really ran in.
    """

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.handles: List[Any] = []
        self._payload = payload

    def query_json(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(dict(kwargs))
        self.handles.append(current_json_session())
        return dict(self._payload if self._payload is not None else PASSED_PAYLOAD)


class _GarbageTool:
    """A tool whose answers are never a verdict — forces the retries."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.handles: List[Any] = []

    def query_json(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(dict(kwargs))
        self.handles.append(current_json_session())
        return {}


def _real_tool(monkeypatch, reported_session_id: str = "sid-reported-by-cli"):
    """Patch the real tool's dispatch so it answers without a subprocess.

    The mock reports a session id of its own — a CLI is free to re-key
    a conversation — so the attempt log can be checked for the id that
    ANSWERED rather than the id that was asked for.
    """
    recorded: List[Dict[str, Any]] = []

    def _spy(self, prompt, system_instruction, **kwargs):
        self.current_call_provider = "vendor-a-pro"
        recorded.append({"prompt": prompt, **kwargs})
        return (json.dumps(PASSED_PAYLOAD), reported_session_id)

    monkeypatch.setattr(ClaudeCodingTool, "_run_interactive_resilient", _spy)
    return recorded


def _events(log_path: Path, name: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if not log_path.exists():
        return entries
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if parsed.get("event") == name:
                entries.append(parsed.get("data") or {})
    return entries


async def _run(
    tool: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vp_node: Optional[Dict[str, Any]] = None,
    max_retries: int = 2,
):
    """Drive one ``_self_heal_loop`` and return the verdict and the log."""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    sub = VerificationSubAgent(method="code_review", max_retries=max_retries)
    log_path = tmp_path / "vp.log"
    verdict = await sub._self_heal_loop(
        vp_node=dict(vp_node or VP_NODE),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )
    return verdict, log_path


# ---------------------------------------------------------------------------
# 1. Every attempt gets a session of its own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_attempt_dispatch_is_bound_to_a_pinned_session(
    tmp_path, monkeypatch,
):
    """The dispatch must run in the session the attempt named.

    A tool with no handle behind it picks its own session, and the
    sub-agent then continues whatever conversation it landed in. The
    bound handle is what makes "this attempt's session" a thing that
    exists at all.
    """
    tool = _CapturingTool()

    verdict, _log = await _run(tool, tmp_path, monkeypatch)

    assert len(tool.handles) == 1, "the attempt dispatched once"
    handle = tool.handles[0]
    assert handle is not None, (
        "the dispatch ran with no session bound; the tool was free to "
        "answer from any conversation it liked"
    )
    assert handle.pinned_session_id, (
        "the attempt must pin the id of the session it creates"
    )
    assert handle.stage == "pinned", (
        "the id is derived, so the first dispatch CREATES the session "
        "under it; that is what the stage says"
    )
    assert verdict.verdict == "PASSED"


@pytest.mark.asyncio
async def test_each_attempt_gets_a_session_of_its_own(tmp_path, monkeypatch):
    """Two attempts of the same VP must not share a session.

    The retry exists because the first answer was unusable. Re-using
    the first attempt's session would put the retry in a conversation
    that already holds the unusable answer, and the third attempt would
    be in a conversation that holds two.
    """
    tool = _GarbageTool()

    _verdict, _log = await _run(tool, tmp_path, monkeypatch)

    assert len(tool.handles) == 3, (
        f"the malformed answers must consume the whole retry budget; "
        f"got {len(tool.handles)} dispatches"
    )
    pinned = [h.pinned_session_id for h in tool.handles]
    assert len(set(pinned)) == len(pinned), (
        f"every attempt must own its session; got {pinned!r}"
    )
    handles = [id(h) for h in tool.handles]
    assert len(set(handles)) == len(handles), (
        "the handles themselves must not be shared between attempts"
    )


@pytest.mark.asyncio
async def test_two_verification_points_never_share_a_session(
    tmp_path, monkeypatch,
):
    """The session is per verification point, not per round.

    This is the shape of the round-24 failure: an attempt judged one
    verification point and the answer that came back was about another
    one. A shared session is what let that happen.
    """
    tool = _CapturingTool()

    await _run(tool, tmp_path, monkeypatch)
    await _run(
        tool, tmp_path, monkeypatch,
        vp_node={**VP_NODE, "id": "VP-038", "title": "另一个验证点"},
    )

    assert len(tool.handles) == 2
    first, second = tool.handles
    assert first is not second, (
        "two verification points must not share one session handle"
    )
    assert first.pinned_session_id != second.pinned_session_id


# ---------------------------------------------------------------------------
# 2. The attempt log says which session answered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_attempt_log_records_the_pinned_session_id(
    tmp_path, monkeypatch,
):
    """The attempt has to name the session it asked for.

    Without it, an attempt log says "an attempt happened" and not "an
    attempt in THIS conversation happened", which is the question the
    round-24 post-mortem could not answer.
    """
    _real_tool(monkeypatch)
    tool = ClaudeCodingTool(settings=tmp_path / "settings.json")

    _verdict, log_path = await _run(tool, tmp_path, monkeypatch)

    started = _events(log_path, "attempt_started")
    assert len(started) == 1
    assert started[0].get("session_id"), (
        f"attempt_started must name the pinned session; got {started[0]!r}"
    )


@pytest.mark.asyncio
async def test_the_attempt_log_records_the_session_that_answered(
    tmp_path, monkeypatch,
):
    """The completed attempt names the session that ANSWERED.

    Not the one that was asked for: a provider fallback may re-key the
    conversation, and the id that answered is the id a reader has to be
    able to open.
    """
    _real_tool(monkeypatch, reported_session_id="sid-reported-by-cli")
    tool = ClaudeCodingTool(settings=tmp_path / "settings.json")

    _verdict, log_path = await _run(tool, tmp_path, monkeypatch)

    completed = _events(log_path, "attempt_completed")
    assert len(completed) == 1
    assert completed[0].get("session_id") == "sid-reported-by-cli", (
        f"attempt_completed must name the session that answered; got "
        f"{completed[0]!r}"
    )
    started = _events(log_path, "attempt_started")
    assert started[0].get("session_id") != "sid-reported-by-cli", (
        "the fixture only proves this if the two ids actually differ"
    )


# ---------------------------------------------------------------------------
# 3. The session carries the identity of the verification point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_session_carries_the_verification_point_anchor(
    tmp_path, monkeypatch,
):
    """The transcript cannot say which question it is answering.

    A resumed turn gets "carry on" and nothing else, so the anchor —
    the verification point's id and title — is what tells the sub-agent
    what the session is for.
    """
    tool = _CapturingTool()

    await _run(tool, tmp_path, monkeypatch)

    anchor = tool.handles[0].resume_anchor
    assert anchor, (
        "the session must carry an anchor; without it a continued turn "
        "has no idea what it is continuing"
    )
    assert "VP-028" in anchor, (
        f"the anchor must name the verification point; got {anchor!r}"
    )
    assert VP_NODE["title"] in anchor, (
        f"the anchor must name the verification point's subject; got "
        f"{anchor!r}"
    )


# ---------------------------------------------------------------------------
# 4. An unattributable answer is not a pass
# ---------------------------------------------------------------------------


class _SessionlessTool(ClaudeCodingTool):
    """A tool that answers WITHOUT ever recording a session.

    It is a real tool — it owns the session machinery — but its
    ``query_json`` never touches the bound handle, so no session
    produced the answer it is about to return.
    """

    def query_json(self, **kwargs: Any) -> Dict[str, Any]:
        return dict(PASSED_PAYLOAD)


@pytest.mark.asyncio
async def test_a_verdict_no_session_produced_is_not_a_pass(
    tmp_path, monkeypatch,
):
    """No session, no attribution — so not a PASSED.

    The answer exists and it is well-formed. What it does not have is a
    conversation behind it, so there is nothing that says it was
    produced by *this* attempt on *this* verification point. Filing it
    as a PASSED would file an unattributable answer as evidence about
    the deliverable.
    """
    settings = tmp_path / "settings.json"
    tool = _SessionlessTool(settings=settings)

    verdict, _log = await _run(tool, tmp_path, monkeypatch)

    assert verdict.verdict == "FAILED", (
        f"an answer no session produced must not be filed as a pass; got "
        f"{verdict.verdict!r} / {verdict.reasons!r}"
    )
    assert verdict.failure_class == "infra", (
        f"the failure is the attempt's, not the deliverable's; got "
        f"{verdict.failure_class!r}"
    )
