"""A fresh-session retry must carry the task book, not a continuation cue.

Why this file exists
--------------------
``_run_interactive_resilient`` retries an :class:`EmptyResponseError` on a
**brand-new session id** with ``resume=False`` — the session that came
back empty was never created, so re-opening it cannot work.

But the prompt was not changed with it. On the provider-switch path the
prompt is :data:`RESUME_CONTINUATION_PROMPT` ("your previous turn was cut
off… continue from where you stopped"), which only means anything to a
model that can SEE the previous turn. The fresh session cannot: it has no
history, so the cue describes a conversation that does not exist, and the
sub-agent is left guessing what the task was — and a sub-agent guessing
may answer a *different* verification point than the one it was given.

So the fresh-session retry has to be able to send the ORIGINAL task book
instead. ``fresh_prompt`` is that knob: it applies to the fresh-session
retry only, and leaves every other caller's behaviour untouched.

Three contracts are pinned here:

1. **With ``fresh_prompt``, the fresh-session retry sends it.**
2. **Without it, the retry still sends the caller's own prompt.**
3. **A resumed retry (``fresh_session_on_retry=False``) keeps the
   resume prompt and the session id** — that path still has the history
   the continuation cue refers to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    EmptyResponseError,
    RESUME_CONTINUATION_PROMPT,
)

#: The first sentence of the continuation cue — the part that would
#: describe a conversation the fresh session does not have.
_FIRST_SENTENCE = RESUME_CONTINUATION_PROMPT.split(". ")[0] + "."

#: The task book the fresh session is supposed to receive.
_TASK_BOOK = "TASK: verify endpoint X"


def _resp_tool(monkeypatch, side_effect):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    Retry backoff is zeroed on the instance so the test does not wait
    the real 2 s / 4 s. Every dispatch is recorded as
    ``(prompt, session_id, resume)`` — the three things the retry branch
    is allowed to change.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    calls = []

    def _spy(prompt, *args, **kwargs):
        calls.append({
            "prompt": prompt,
            "session_id": kwargs.get("session_id"),
            "resume": kwargs.get("resume"),
        })
        result = side_effect(len(calls), calls[-1])
        # A side effect that *returns* an exception means "raise it" —
        # lets a test pass the exception factory straight in.
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_claude_interactive", _spy)
    tool._calls = calls
    return tool


def _empty(n: int) -> EmptyResponseError:
    return EmptyResponseError(f"empty #{n}", provider="Vendor A Pro", returncode=0)


def _resilient(tool, prompt, **overrides):
    kwargs = dict(
        session_id="11111111-1111-1111-1111-111111111111",
        resume=True,
        excluded_providers=None,
        total_timeout=30,
        allowed_tools=None,
        scene="verification",
    )
    kwargs.update(overrides)
    return tool._run_interactive_resilient(prompt, "sys", **kwargs)


# ---------------------------------------------------------------------------
# 1. The fresh-session retry sends the original task book
# ---------------------------------------------------------------------------


def test_fresh_session_retry_sends_the_original_task_prompt(monkeypatch):
    """The fresh session has no history — the task book must travel with it."""
    def _side(n, call):
        if n == 1:
            raise _empty(n)
        return ("ok", call["session_id"], {})

    tool = _resp_tool(monkeypatch, _side)
    text, sid = _resilient(tool, RESUME_CONTINUATION_PROMPT, fresh_prompt=_TASK_BOOK)

    first, second = tool._calls
    assert first["prompt"] == RESUME_CONTINUATION_PROMPT, (
        "the first dispatch keeps the caller's prompt"
    )
    assert second["prompt"] == _TASK_BOOK, (
        "the fresh-session retry must send the original task book; the "
        "continuation cue describes a conversation the new session cannot "
        "see, so the sub-agent would be left guessing the task"
    )
    assert _FIRST_SENTENCE not in second["prompt"], (
        "the fresh-session retry must not carry the resume cue — there is "
        "no previous turn for it to refer to"
    )
    assert second["resume"] is False
    assert second["session_id"] != first["session_id"], (
        "the retry is a fresh session: a new id, never a resume"
    )
    assert text == "ok"
    assert sid == second["session_id"], (
        "the returned id is the session that actually exists"
    )


# ---------------------------------------------------------------------------
# 2. Without the task book, the caller's own prompt is still sent
# ---------------------------------------------------------------------------


def test_without_fresh_prompt_the_retry_still_sends_the_callers_prompt(monkeypatch):
    """``fresh_prompt`` is opt-in — every other caller keeps its behaviour."""
    def _side(n, call):
        if n == 1:
            raise _empty(n)
        return ("ok", call["session_id"], {})

    tool = _resp_tool(monkeypatch, _side)
    _resilient(tool, "prompt")

    first, second = tool._calls
    assert first["prompt"] == "prompt"
    assert second["prompt"] == "prompt", (
        "no task book was supplied, so the retry sends the caller's prompt "
        "exactly as before"
    )


# ---------------------------------------------------------------------------
# 3. A resumed retry keeps the resume prompt and the session
# ---------------------------------------------------------------------------


def test_resumed_retry_keeps_the_resume_prompt(monkeypatch):
    """``fresh_session_on_retry=False`` — the history is still there.

    The continuation cue is correct on this path: the session exists and
    holds the previous turn. Swapping in a task book here would throw
    away the context the cue depends on.
    """
    def _side(n, call):
        if n == 1:
            raise _empty(n)
        return ("ok", call["session_id"], {})

    tool = _resp_tool(monkeypatch, _side)
    _resilient(
        tool,
        RESUME_CONTINUATION_PROMPT,
        session_id="22222222-2222-2222-2222-222222222222",
        fresh_session_on_retry=False,
        fresh_prompt=_TASK_BOOK,
    )

    first, second = tool._calls
    assert second["prompt"] == RESUME_CONTINUATION_PROMPT, (
        "a resumed retry keeps the resume cue — the session it re-opens "
        "holds the turn the cue refers to"
    )
    assert second["session_id"] == first["session_id"]
    assert second["resume"] is True


# ---------------------------------------------------------------------------
# 4. The retry budget still bounds the retries
# ---------------------------------------------------------------------------


def test_retry_budget_exhausted_still_raises_empty_response(monkeypatch):
    """A task book does not buy extra retries."""
    tool = _resp_tool(monkeypatch, lambda n, call: _empty(n))

    with pytest.raises(EmptyResponseError):
        _resilient(tool, RESUME_CONTINUATION_PROMPT, fresh_prompt=_TASK_BOOK)

    assert len(tool._calls) == 1 + ClaudeCodingTool.MAX_EMPTY_RESPONSE_RETRIES
