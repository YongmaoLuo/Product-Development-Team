"""A resume that comes back empty must not leave the new session guessing.

Why this file exists
--------------------
``_resume_or_fresh_session`` is the provider-switch path: the previous
attempt died on one provider and the retry runs on another. It re-opens
the session the previous attempt left off, so the prompt it sends is
:data:`RESUME_CONTINUATION_PROMPT` — "your previous turn was cut off…
continue from where you stopped".

That cue is only meaningful to a model that can SEE the previous turn.
Two things can happen to the resume, and both of them can leave the
dispatch talking to a session with no history at all:

* the resume comes back **empty** (:class:`EmptyResponseError`). The
  session never existed, so ``_run_interactive_resilient`` opens a NEW
  session to retry. The new session has no history — and if it is sent
  the continuation cue, the sub-agent is left guessing what the task
  was. A sub-agent guessing may answer a *different* verification point
  than the one it was given.
* the resume fails with something else (an API error). The degradation
  path in ``_resume_or_fresh_session`` opens a new session too — and
  that one already sends the task book.

So the empty-retry inside the resume has to carry the task book as well.
That is what ``fresh_prompt`` is for: the knob exists on
``_run_interactive_resilient`` (added for the fresh-session retry), and
this file pins that the resume entry point actually passes it down.

Two contracts are pinned here:

1. **An empty resume retries on a new session with the task book.**
2. **A non-empty resume failure still degrades with the task book** —
   the existing fallback must keep working unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import (  # noqa: E402
    ApiError,
    ClaudeCodingTool,
    EmptyResponseError,
    RESUME_CONTINUATION_PROMPT,
)

#: The session the previous attempt left off on.
PREV_SESSION_ID = "11111111-1111-1111-1111-111111111111"

#: The task book the new session is supposed to receive.
TASK_BOOK = "TASK: verify endpoint X"


@pytest.fixture(autouse=True)
def no_scripted_llm(monkeypatch):
    """The real dispatch path, not the strict double.

    ``query()`` answers from a fixture file when the scripted double is
    switched on, and it does so BEFORE the handle is ever read — so a
    scripted environment would answer this file's questions without
    dispatching anything, and every assertion below would pass for the
    wrong reason.
    """
    monkeypatch.delenv("PDT_LLM_MODE", raising=False)


def _tool(monkeypatch, side_effect):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    Retry backoff is zeroed on the instance so the test does not wait
    the real 2 s. Every dispatch is recorded as
    ``(prompt, session_id, resume)`` — the three things the retry branch
    is allowed to change.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    calls: list[dict] = []

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


def _resume_or_fresh(tool):
    """The provider-switch entry point, as the caller invokes it."""
    return tool._resume_or_fresh_session(
        prev_session_id=PREV_SESSION_ID,
        resume_prompt=RESUME_CONTINUATION_PROMPT,
        fresh_prompt=TASK_BOOK,
        system_instruction=None,
        excluded_providers=None,
        total_timeout=30,
        allowed_tools=None,
        scene="verification",
    )


# ---------------------------------------------------------------------------
# 1. An empty resume retries on a new session with the task book
# ---------------------------------------------------------------------------


def test_resume_empty_then_fresh_retry_sends_the_task_book(monkeypatch):
    """The new session has no history — the task book must travel with it.

    The resume is the FIRST dispatch and it comes back empty: the
    session never existed, so the retry opens a new one. Sending that
    new session the continuation cue would describe a conversation it
    cannot see, and the sub-agent would be left guessing the task.
    """
    def _side(n, call):
        if n == 1:
            raise EmptyResponseError(
                "empty resume",
                provider="Vendor A Pro",
                returncode=0,
                stderr="",
            )
        return ("ok", call["session_id"], {})

    tool = _tool(monkeypatch, _side)
    text, sid = _resume_or_fresh(tool)

    assert len(tool._calls) == 2, (
        f"expected a resume attempt and a fresh retry, got {tool._calls!r}"
    )
    first, second = tool._calls

    assert first["prompt"] == RESUME_CONTINUATION_PROMPT, (
        "the first dispatch resumes the session with the continuation cue"
    )
    assert first["resume"] is True
    assert first["session_id"] == PREV_SESSION_ID

    assert second["prompt"] == TASK_BOOK, (
        "the fresh retry must send the original task book; the continuation "
        f"cue describes a conversation the new session cannot see, so the "
        f"sub-agent would be left guessing the task — it received "
        f"{second['prompt']!r}"
    )
    assert second["resume"] is False, (
        "the retry must open a NEW session, never re-aim at the dead id"
    )
    assert second["session_id"] != PREV_SESSION_ID, (
        "the retry is a fresh session: a new id, never a resume"
    )
    assert text == "ok"
    assert sid == second["session_id"], (
        "the returned id is the session that actually exists"
    )


# ---------------------------------------------------------------------------
# 2. A non-empty resume failure still degrades with the task book
# ---------------------------------------------------------------------------


def test_resume_failure_without_empty_response_still_falls_back_with_the_task_book(monkeypatch):
    """The existing degradation path keeps sending the task book.

    An API error is not an empty response: the retry policy belongs to
    the provider-fallback handlers, so the error reaches
    ``_resume_or_fresh_session`` and its own fallback answers — on a
    new session, with the task book.
    """
    def _side(n, call):
        if n == 1:
            raise ApiError("x", status="500")
        return ("ok", call["session_id"], {})

    tool = _tool(monkeypatch, _side)
    text, sid = _resume_or_fresh(tool)

    assert len(tool._calls) == 2, (
        f"expected a resume attempt and a fresh fallback, got {tool._calls!r}"
    )
    first, second = tool._calls

    assert first["resume"] is True
    assert second["prompt"] == TASK_BOOK, (
        "the fallback must send the original task book; it received "
        f"{second['prompt']!r}"
    )
    assert second["resume"] is False
    assert second["session_id"] != PREV_SESSION_ID
    assert text == "ok"
    assert sid == second["session_id"]