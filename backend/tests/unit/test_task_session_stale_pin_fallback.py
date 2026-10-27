"""A stale pinned id degrades to a fresh session instead of failing the task.

Background
----------
2026-10-26. The executor binds a session to a task and writes that
binding into the execution log BEFORE the task's first dispatch runs.
The binding may name an id the dispatch is about to CREATE — the
"pinned" stage, where the id is derived from the plan and the task so
that a later ``/start`` re-opens the same conversation.

That is a bet: the log says "this task's session is X" while the
session does not exist yet. The bet is only paid off if the first
dispatch actually creates it. If the task was stopped, or the
provider died, or the process was killed between the binding and the
dispatch, the session was never created — and the id in the log
names nothing.

The next ``/start`` reads that id and aims ``--resume`` at it. The CLI
refuses it ("Invalid session ID") in about 0.4s. So the failure this
file pins is not hypothetical: it is what a stopped round looks like to
the round that follows it.

What is pinned here
-------------------
``_resume_or_fresh_session`` is the place that degradation lives: a
resume that cannot be honoured falls back to the fresh-session call it
replaces, and reports the NEW id, so the caller's handle stops naming
a session that does not exist.

The tests drive the plain-text ``query_in_session`` path with a handle
that ALREADY carries a stale id — the state a ``/start`` after a
``/stop`` finds. The round that follows is a resume attempt from its
very first dispatch, which is why this is not the same case as "round
two re-opens a session that round one created".

The three things that must NOT be degraded are pinned too, because the
degradation is a *retry* and a retry is wrong for each of them:

* a cancellation — the work was stopped deliberately, and the
  fallback would run it again on another provider;
* a hard timeout — the resumed session is fine, the provider is slow,
  and the caller's own timeout routing owns that answer;
* a deterministic local failure — the machine cannot run the call at
  all, so the fallback fails the same way and buries the real cause.

And when BOTH dispatches fail, the exception reaches the caller: a
degradation that swallows the second failure would report success for
work that never happened.

TDD spec — 6 contract tests
----------------------------
1. ``test_stale_resume_degrades_to_fresh_session``: the resume raises
   ``EmptyResponseError`` and the fresh dispatch answers → the caller
   gets the fresh text and nothing is raised.
2. ``test_degraded_handle_points_at_the_new_session``: after the
   degradation the handle names the NEW session, not the dead id.
3. ``test_cancellation_is_not_degraded``: ``VerificationCancelled`` is
   re-raised and no second dispatch is made.
4. ``test_hard_timeout_is_not_degraded``: ``HardTimeoutError`` is
   re-raised and no second dispatch is made.
5. ``test_deterministic_local_error_is_not_degraded``: a missing
   working directory is re-raised and no second dispatch is made.
6. ``test_both_dispatches_failing_propagates``: the second failure
   reaches the caller instead of being swallowed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure backend/ is on sys.path so ``import coding_tool`` works
# regardless of which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool as coding_tool_module  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    EmptyResponseError,
    JsonSession,
    query_in_session,
)
from verification_cancel import VerificationCancelled  # noqa: E402

#: The id a task's binding names before the session exists — the state a
#: ``/start`` after a ``/stop`` finds when the stopped round never got
#: as far as creating the session.
STALE_SESSION_ID = "never-created"

#: The id the degraded dispatch reports: the session that actually exists
#: after the fallback.
FRESH_SESSION_ID = "fresh-1"

FRESH_TEXT = "the work, done in a fresh session"


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
    """A tool whose ``_run_interactive_resilient`` is driven by ``side_effect``.

    ``side_effect(resume, requested_sid, calls)`` returns either the
    ``(text, session_id)`` pair the real method returns, or an exception
    instance to raise. Every dispatch is recorded with its kwargs, which
    is what the assertions below read: "resume=True, under session id X"
    is a statement about the dispatch, not about the plumbing.

    The provider lookup is patched out as well. Nothing here may reach
    the CC Switch database: the test is about which dispatch happens,
    and a live provider row would make the answer depend on the
    machine it runs on.
    """
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_load_provider_from_db",
        staticmethod(lambda *args, **kwargs: {}),
    )

    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded: list[dict] = []

    def _spy(prompt, system_instruction, **kwargs):
        # The launcher stamps the provider it selected; the degradation
        # path reads that label back to say which provider missed. A
        # stub that left it unset would not be modelling a dispatch
        # that ever happened.
        tool.current_call_provider = "vendor-a-pro"
        recorded.append({"prompt": prompt, **kwargs})
        result = side_effect(
            kwargs.get("resume"), kwargs.get("session_id"), recorded,
        )
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_interactive_resilient", _spy)
    tool._calls = recorded
    return tool


def _stale_then_fresh(resume, requested_sid, calls):
    """The resume is refused; the fresh dispatch answers.

    This is the shape of the real failure: the CLI refuses to re-open a
    session that was never created, and the work still has to happen.
    """
    if resume:
        raise EmptyResponseError(
            "Invalid session ID",
            provider="vendor-a-pro",
            returncode=1,
            stderr="Error: Invalid session ID",
        )
    return (FRESH_TEXT, FRESH_SESSION_ID)


# ---------------------------------------------------------------------------
# 1. The degradation happens
# ---------------------------------------------------------------------------


def test_stale_resume_degrades_to_fresh_session(monkeypatch):
    """A resume that cannot be honoured falls back to a fresh session.

    The handle already carries a stale id when the call arrives — that
    is the whole shape of the bug: the FIRST dispatch of this round is
    a resume attempt, and it aims at a session nobody ever created.
    """
    tool = _tool(monkeypatch, _stale_then_fresh)
    handle = JsonSession(session_id=STALE_SESSION_ID)

    result = query_in_session(tool, handle, "do it")

    assert result == FRESH_TEXT, (
        "the caller must still get an answer; a stale pinned id is a "
        "session that does not exist, not a reason to fail the task"
    )
    assert len(tool._calls) == 2, (
        f"expected a resume attempt and a fresh dispatch, got {tool._calls!r}"
    )
    assert tool._calls[0]["resume"] is True, (
        "the first dispatch must have tried to re-open the stale id"
    )
    assert tool._calls[0]["session_id"] == STALE_SESSION_ID
    assert tool._calls[1]["resume"] is False, (
        "the fallback must open a NEW session, not re-aim at the dead id"
    )
    assert tool._calls[1]["session_id"] != STALE_SESSION_ID


# ---------------------------------------------------------------------------
# 2. The handle follows the degradation
# ---------------------------------------------------------------------------


def test_degraded_handle_points_at_the_new_session(monkeypatch):
    """After the degradation the handle names the session that exists.

    Otherwise the NEXT round aims ``--resume`` at the same dead id and
    pays for the same failure again — and, worse, a caller that
    persists the handle would write the dead id back to disk.
    """
    tool = _tool(monkeypatch, _stale_then_fresh)
    handle = JsonSession(session_id=STALE_SESSION_ID)

    query_in_session(tool, handle, "do it")

    assert handle.session_id == FRESH_SESSION_ID, (
        "the handle must name the session that exists, not the one that "
        f"does not; got {handle.session_id!r}"
    )


# ---------------------------------------------------------------------------
# 3. The degradation is not applied to a cancellation
# ---------------------------------------------------------------------------


def test_cancellation_is_not_degraded(monkeypatch):
    """A stopped dispatch is re-raised, and nothing is re-dispatched.

    The fallback is a RETRY of the same work. A stop forbids running
    that work again on another provider.
    """
    def _cancelled(resume, requested_sid, calls):
        raise VerificationCancelled("the operator stopped the plan")

    tool = _tool(monkeypatch, _cancelled)
    handle = JsonSession(session_id=STALE_SESSION_ID)

    with pytest.raises(VerificationCancelled):
        query_in_session(tool, handle, "do it")

    assert len(tool._calls) == 1, (
        "a cancelled dispatch must not be retried; got "
        f"{tool._calls!r}"
    )
    assert handle.session_id == STALE_SESSION_ID, (
        "a cancelled dispatch leaves the handle as it found it"
    )


# ---------------------------------------------------------------------------
# 4. The degradation is not applied to a timeout
# ---------------------------------------------------------------------------


def test_hard_timeout_is_not_degraded(monkeypatch):
    """A slow provider is re-raised, and nothing is re-dispatched.

    The resumed session is fine — the provider is slow. That is the
    caller's own timeout routing (auto-split, next provider), and
    masking it as a resume failure would send the same work to a fresh
    session and throw the conversation away.
    """

    def _slow(resume, requested_sid, calls):
        raise coding_tool_module.HardTimeoutError(
            total_sec=10, elapsed=11.0, last_line="",
        )

    tool = _tool(monkeypatch, _slow)
    handle = JsonSession(session_id=STALE_SESSION_ID)

    with pytest.raises(coding_tool_module.HardTimeoutError):
        query_in_session(tool, handle, "do it")

    assert len(tool._calls) == 1, (
        "a timeout is not a resume failure; got " f"{tool._calls!r}"
    )
    assert handle.session_id == STALE_SESSION_ID


# ---------------------------------------------------------------------------
# 5. The degradation is not applied to a deterministic local failure
# ---------------------------------------------------------------------------


def test_deterministic_local_error_is_not_degraded(monkeypatch):
    """A missing working directory is re-raised, and nothing is re-dispatched.

    The fallback would run in the SAME missing directory and fail the
    same way — so the dispatch is doubled and the real cause is buried
    under a "retrying in a fresh session" log line.
    """

    def _missing_cwd(resume, requested_sid, calls):
        raise FileNotFoundError(
            "[Errno 2] No such file or directory: '/gone/wt'"
        )

    tool = _tool(monkeypatch, _missing_cwd)
    handle = JsonSession(session_id=STALE_SESSION_ID)

    with pytest.raises(FileNotFoundError):
        query_in_session(tool, handle, "do it")

    assert len(tool._calls) == 1, (
        "a deterministic local failure must not be retried; got "
        f"{tool._calls!r}"
    )
    assert handle.session_id == STALE_SESSION_ID


# ---------------------------------------------------------------------------
# 6. Two failures are still a failure
# ---------------------------------------------------------------------------


def test_both_dispatches_failing_propagates(monkeypatch):
    """When the fallback fails too, the caller hears about it.

    A degradation that swallowed the second failure would report
    success for work that never happened — the worst possible answer,
    because it is indistinguishable from a task that ran.
    """

    def _both_fail(resume, requested_sid, calls):
        raise EmptyResponseError(
            "the provider is down",
            provider="vendor-a-pro",
            returncode=1,
        )

    tool = _tool(monkeypatch, _both_fail)
    handle = JsonSession(session_id=STALE_SESSION_ID)

    with pytest.raises(EmptyResponseError):
        query_in_session(tool, handle, "do it")

    assert len(tool._calls) == 2, (
        "the resume was tried and the fallback was tried; got "
        f"{tool._calls!r}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))