"""An empty reply is a failure with a diagnosis, not a silent ``""`` (2026-09-17).

Why this file exists
--------------------
A repair generation died and reported::

    JSONDecodeError: no JSON object / array boundaries found

That message is a *symptom*. The subprocess had exited without emitting
any assistant text at all; ``_run_claude_interactive`` returned ``""``
for it, ``parse_llm_json("")`` produced exactly that error, and the
stderr the function had already collected was thrown away on the way
out. The operator was told "the model produced unparseable JSON" when
the truth was "the CLI produced nothing", and the two need different
responses.

Three contracts are pinned here:

1. **Empty output raises.** ``EmptyResponseError`` — never ``""`` — and
   it carries the exit code and the stderr that used to be discarded.
2. **The retry for it is a FRESH session.** A session that never replied
   was never created, so ``--resume``-ing it can only fail (measured:
   ~0.4 s, ``Invalid session ID``). This is the fix for a retry that was
   structurally guaranteed to fail.
3. **The format retry keeps forwarding ``scene``.** Attempt 2 of
   ``query_json`` used to omit it, silently dropping back to the
   capacity-blind registry walk for a session attempt 1 had opened on a
   scene-routed provider.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
from coding_tool import ClaudeCodingTool, EmptyResponseError  # noqa: E402


def _install_fake_claude(tmp_path, monkeypatch, script: str) -> None:
    """Put an executable ``claude`` first on ``PATH``."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


# ---------------------------------------------------------------------------
# 1. The subprocess-level contract
# ---------------------------------------------------------------------------


def test_clean_exit_with_no_output_raises_empty_response(tmp_path, monkeypatch):
    """``exit 0`` with no stdout is a failure, not an empty success."""
    _install_fake_claude(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")

    tool = ClaudeCodingTool()
    with pytest.raises(EmptyResponseError) as excinfo:
        tool._run_claude_interactive("hello", idle_timeout=30, total_timeout=30)

    err = excinfo.value
    assert err.returncode == 0, (
        "the exit code is the whole point — a non-zero code means the CLI "
        "crashed, a zero code means it decided to say nothing"
    )
    assert err.stdout_len == 0
    assert "returncode=0" in str(err)


def test_stderr_from_the_empty_run_is_captured(tmp_path, monkeypatch):
    """The regression that made the original failure undiagnosable.

    ``stderr_data`` was read into a local and then dropped on the empty
    path, so the one piece of evidence that explains *why* the CLI said
    nothing never reached a log or an exception.
    """
    _install_fake_claude(
        tmp_path, monkeypatch,
        "#!/bin/sh\necho 'API Error: 529 overloaded_error' >&2\nexit 0\n",
    )

    tool = ClaudeCodingTool()
    with pytest.raises(EmptyResponseError) as excinfo:
        tool._run_claude_interactive("hello", idle_timeout=30, total_timeout=30)

    assert "529 overloaded_error" in excinfo.value.stderr
    assert "529 overloaded_error" in str(excinfo.value)


def test_nonzero_exit_with_no_output_is_still_an_empty_response(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch, "#!/bin/sh\nexit 3\n")

    tool = ClaudeCodingTool()
    with pytest.raises(EmptyResponseError) as excinfo:
        tool._run_claude_interactive("hello", idle_timeout=30, total_timeout=30)

    assert excinfo.value.returncode == 3


def test_the_error_serialises_for_log_payloads(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")

    tool = ClaudeCodingTool()
    with pytest.raises(EmptyResponseError) as excinfo:
        tool._run_claude_interactive("hello", idle_timeout=30, total_timeout=30)

    payload = excinfo.value.to_dict()
    assert payload["type"] == "empty_response"
    assert payload["returncode"] == 0
    assert isinstance(payload["elapsed_sec"], float)


# ---------------------------------------------------------------------------
# 2. The retry strategy — fresh session, bounded
# ---------------------------------------------------------------------------


def _resp_tool(monkeypatch, side_effect):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    Retry backoff is zeroed on the instance so the test does not wait
    the real 2 s / 4 s.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    calls = []

    def _spy(prompt, *args, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        result = side_effect(len(calls))
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


def _resilient(tool, **overrides):
    kwargs = dict(
        session_id=str("11111111-1111-1111-1111-111111111111"),
        resume=False,
        excluded_providers=None,
        total_timeout=30,
        allowed_tools=None,
        scene="verification",
    )
    kwargs.update(overrides)
    return tool._run_interactive_resilient("prompt", "sys", **kwargs)


def test_empty_reply_is_retried_then_succeeds(monkeypatch):
    def _side(n):
        if n <= 2:
            raise _empty(n)
        return ("ok", "sid", {})

    tool = _resp_tool(monkeypatch, _side)
    text, _ = _resilient(tool)

    assert text == "ok"
    assert len(tool._calls) == 3, "one attempt + MAX_EMPTY_RESPONSE_RETRIES"


def test_the_empty_retry_uses_a_new_session_never_a_resume(monkeypatch):
    """The core fix: resuming a session that never replied cannot work."""
    def _side(n):
        if n == 1:
            raise _empty(n)
        return ("ok", "sid", {})

    tool = _resp_tool(monkeypatch, _side)
    _resilient(tool, session_id="11111111-1111-1111-1111-111111111111", resume=False)

    first, second = tool._calls
    assert second["resume"] is False, (
        "the retry must NOT --resume: the session was never created, so "
        "claude --resume <unknown-uuid> fails in ~0.4s with 'Invalid session ID'"
    )
    assert second["session_id"] != first["session_id"], (
        "a fresh session id is required; re-using the dead one reproduces "
        "the original guaranteed-to-fail retry"
    )


def test_the_empty_retry_gives_up_after_the_budget(monkeypatch):
    tool = _resp_tool(monkeypatch, _empty)

    with pytest.raises(EmptyResponseError):
        _resilient(tool)

    assert len(tool._calls) == 1 + ClaudeCodingTool.MAX_EMPTY_RESPONSE_RETRIES


def test_other_errors_are_not_swallowed_by_the_empty_retry(monkeypatch):
    """Timeouts and API errors keep their own (provider-fallback) handlers."""
    def _side(n):
        raise TimeoutError("idle")

    tool = _resp_tool(monkeypatch, _side)

    with pytest.raises(TimeoutError):
        _resilient(tool)

    assert len(tool._calls) == 1, "the empty-response retry must not absorb timeouts"


def test_resumed_sessions_keep_resuming_on_an_empty_retry(monkeypatch):
    """The format retry DOES have a live session, so it must not be reset.

    ``fresh_session_on_retry=False`` is what attempt 2 of ``query_json``
    uses — a new session there would throw away the conversational
    context the follow-up hint depends on.
    """
    def _side(n):
        if n == 1:
            raise _empty(n)
        return ("ok", "sid", {})

    tool = _resp_tool(monkeypatch, _side)
    _resilient(tool, session_id="22222222-2222-2222-2222-222222222222",
               resume=True, fresh_session_on_retry=False)

    second = tool._calls[1]
    assert second["resume"] is True
    assert second["session_id"] == "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# 3. query_json wiring: scene survives the format retry
# ---------------------------------------------------------------------------


def test_query_json_retries_empty_on_a_fresh_session(monkeypatch):
    def _side(n):
        if n == 1:
            raise _empty(n)
        return ('{"tasks": []}', "sid", {})

    tool = _resp_tool(monkeypatch, _side)

    assert tool.query_json(prompt="x", system_instruction="y", scene="verification") == {
        "tasks": []
    }
    assert tool._calls[1]["resume"] is False


def test_query_json_format_retry_forwards_the_scene(monkeypatch):
    """Regression: attempt 2 used to omit ``scene`` entirely.

    Without it the resume fell through to the legacy registry walk and
    could address a different provider than the session it was
    continuing.
    """
    def _side(n):
        if n == 1:
            return ("not json at all, just prose", "sid", {})
        return ('{"a": 1}', "sid", {})

    tool = _resp_tool(monkeypatch, _side)
    assert tool.query_json(prompt="x", system_instruction="y", scene="verification") == {"a": 1}

    assert len(tool._calls) == 2
    assert tool._calls[1]["resume"] is True
    assert tool._calls[1]["scene"] == "verification", (
        "attempt 2 must stay on the same scene/provider chain as attempt 1"
    )
