"""A continued turn must know WHICH verification point it is answering.

2026-11-08 (VP-028 · provenance level). The round-24 attempt that was
supposed to judge VP-028 came back with a verdict about VP-038: the
sub-agent answered a DIFFERENT question, and the answer was accepted
because it looked like a verdict.

The dispatch that produced it was a CONTINUATION. When the provider
switches, the session is re-opened and the only user turn that comes
with the re-opened transcript is :data:`RESUME_CONTINUATION_PROMPT` —
"your previous turn was cut off, carry on". It names no task, no
verification point, no deliverable. So a session that belonged to
another question had nothing to contradict it: the model was told to
"continue the task", and the task it found in the transcript was
somebody else's.

What this pins
--------------
1. ``JsonSession`` carries an optional ``resume_anchor``. The default is
   ``None``, and a handle built without one behaves exactly as it did
   before the field existed.
2. A CONTINUATION — the provider switch, the rate-limit retry — carries
   the anchor right after the continuation cue.
3. With no anchor the continuation prompt is byte-identical to what it
   was before the field existed. A field that changes nothing when it is
   not used is the only kind that can be added to a dispatch path every
   caller shares.
4. The re-ask dispatch is untouched: a re-ask sends the new question
   itself, so the anchor would be saying nothing new there.
5. The text path (``query``) carries it too — both paths use the same
   continuation cue, and a fix that reaches only one of them is a fix
   that half-applies.

Where the assertion is
----------------------
At the dispatch: the mocked ``_run_interactive_resilient`` records the
prompt it was handed, so "the anchor reached the turn" is a statement
about what the model was actually sent, not about what a caller passed
along. The end-of-chain argv is covered by the reusable-session suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import (  # noqa: E402
    JSON_ONLY_SUFFIX,
    RESUME_CONTINUATION_PROMPT,
    ClaudeCodingTool,
    JsonSession,
    query_in_session,
    query_json_in_session,
)

ANCHOR = (
    "本会话正在判定验证点 VP-028：延后关卡重算，"
    "最终回复必须是该验证点的裁决 JSON 并引用它点名的源码。"
)


def _tool(monkeypatch, side_effect):
    """A tool whose ``_run_interactive_resilient`` is driven by ``side_effect``.

    ``side_effect(n, requested, recorded)`` gets the 1-based dispatch
    index, the session id the dispatch asked for, and the dispatch log so
    far; it returns either ``(text, session_id)`` — the pair the real
    method returns — or an exception instance to raise.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded: List[Dict[str, Any]] = []

    def _spy(prompt, system_instruction, **kwargs):
        # The launcher stamps the provider it selected; the fallback and
        # the degradation paths read it back to say which provider missed.
        tool.current_call_provider = "vendor-a-pro"
        recorded.append({"prompt": prompt, **kwargs})
        result = side_effect(
            len(recorded), kwargs.get("session_id"), recorded
        )
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_interactive_resilient", _spy)
    tool._calls = recorded
    return tool


def _times_out_once_then_answers():
    """The provider dies on the first dispatch; the retry lands on the next.

    That retry is the CONTINUATION this file is about: it re-opens the
    session the failed attempt was using, so the turn it sends is the
    continuation cue rather than the original question.
    """

    def _side(n: int, requested, recorded) -> Tuple[str, str]:
        if n == 1:
            raise TimeoutError("the provider went away")
        return (json.dumps({"ok": True}), requested)

    return _side


# ---------------------------------------------------------------------------
# 1. The field itself
# ---------------------------------------------------------------------------


def test_a_handle_without_an_anchor_has_none():
    """The default is what every existing caller gets.

    ``JsonSession`` is constructed by the task executor, the repair
    generator and the interview loop. None of them knows anything about
    verification points, so the field has to default to "no anchor" and
    the dispatch has to behave byte-identically when it is absent.
    """
    assert JsonSession().resume_anchor is None


def test_the_anchor_survives_construction():
    handle = JsonSession(resume_anchor=ANCHOR)
    assert handle.resume_anchor == ANCHOR


def test_the_anchor_does_not_disturb_the_other_fields():
    """A new field on a shared handle must not move the existing ones."""
    handle = JsonSession(
        session_id="sid-live",
        pinned_session_id="sid-pinned",
        stage="pinned",
        resume_anchor=ANCHOR,
    )
    assert handle.session_id == "sid-live"
    assert handle.pinned_session_id == "sid-pinned"
    assert handle.stage == "pinned"


# ---------------------------------------------------------------------------
# 2. The continuation carries the anchor
# ---------------------------------------------------------------------------


def test_the_json_continuation_carries_the_anchor(monkeypatch):
    """The provider switch must say what the session is about.

    Without the anchor the re-opened session gets "carry on" and nothing
    else, and the model has to guess which task it is carrying on from
    whatever the transcript happens to hold. When the transcript belongs
    to another verification point, the guess is wrong and the answer
    comes back about the wrong subject — which is what round 24 did.
    """
    tool = _tool(monkeypatch, _times_out_once_then_answers())

    query_json_in_session(
        tool, JsonSession(resume_anchor=ANCHOR), prompt="the original ask",
    )

    continuation = tool._calls[1]["prompt"]
    assert tool._calls[1]["resume"] is True, (
        "the second dispatch must re-open the session; that is the "
        "continuation this file is about"
    )
    assert ANCHOR in continuation, (
        "the continuation must name the verification point it is judging"
    )
    assert "cut off" in continuation, (
        "the continuation cue itself must still be there"
    )


def test_the_json_continuation_keeps_the_format_instruction_last(monkeypatch):
    """The format instruction is the last thing the model reads.

    The anchor is a paragraph about the subject; the format instruction
    is the contract. Putting the subject after the contract would make
    the contract the first thing the model stops reading.
    """
    tool = _tool(monkeypatch, _times_out_once_then_answers())

    query_json_in_session(
        tool, JsonSession(resume_anchor=ANCHOR), prompt="the original ask",
    )

    continuation = tool._calls[1]["prompt"]
    assert continuation.endswith(JSON_ONLY_SUFFIX), (
        "the JSON-only instruction must stay at the end of the turn"
    )
    assert continuation.index(ANCHOR) < continuation.index(JSON_ONLY_SUFFIX)


def test_the_text_continuation_carries_the_anchor(monkeypatch):
    """Both paths use the same continuation cue, so both need the anchor.

    The text path is where the planning and reporting calls live. A fix
    that reaches only the JSON path is a fix that half-applies.
    """
    tool = _tool(monkeypatch, _times_out_once_then_answers())

    query_in_session(
        tool, JsonSession(resume_anchor=ANCHOR), prompt="the original ask",
    )

    continuation = tool._calls[1]["prompt"]
    assert tool._calls[1]["resume"] is True
    assert ANCHOR in continuation, (
        "the text path must carry the anchor too"
    )
    assert "cut off" in continuation


# ---------------------------------------------------------------------------
# 3. No anchor, no change at all
# ---------------------------------------------------------------------------


def test_a_continuation_without_an_anchor_is_byte_identical_to_before(monkeypatch):
    """The field is inert until somebody fills it in.

    Every other caller of the shared dispatch path pays for this one
    field. A prompt that grew a stray newline, or an extra paragraph,
    would change what every caller sends — so the no-anchor case has to
    be the old prompt, exactly.
    """
    tool = _tool(monkeypatch, _times_out_once_then_answers())

    query_json_in_session(tool, JsonSession(), prompt="the original ask")

    assert tool._calls[1]["prompt"] == (
        RESUME_CONTINUATION_PROMPT + JSON_ONLY_SUFFIX
    ), (
        "without an anchor the continuation must be exactly what it was "
        "before the field existed"
    )


def test_a_text_continuation_without_an_anchor_is_byte_identical_to_before(
    monkeypatch,
):
    tool = _tool(monkeypatch, _times_out_once_then_answers())

    query_in_session(tool, JsonSession(), prompt="the original ask")

    assert tool._calls[1]["prompt"] == RESUME_CONTINUATION_PROMPT


# ---------------------------------------------------------------------------
# 4. The re-ask is not the continuation
# ---------------------------------------------------------------------------


def test_the_reask_does_not_carry_the_anchor(monkeypatch):
    """A re-ask sends the new question; the anchor would add nothing.

    The re-ask is a NEW question about the same subject, sent alongside
    the restored conversation. The question itself is the task book, so
    an identity anchor appended to it would be a second copy of what
    the prompt already says.
    """

    def _side(n: int, requested, recorded) -> Tuple[str, str]:
        return (json.dumps({"ok": True}), requested)

    tool = _tool(monkeypatch, _side)
    handle = JsonSession(resume_anchor=ANCHOR)

    query_json_in_session(tool, handle, prompt="round one")
    query_json_in_session(tool, handle, prompt="round two, and this: it is wrong")

    reask = tool._calls[1]["prompt"]
    assert reask.startswith("This is a follow-up"), (
        "the second round of a handle is a re-ask, not a continuation"
    )
    assert ANCHOR not in reask, (
        "the re-ask carries the new request itself; the anchor would be "
        "saying what the prompt already says"
    )
