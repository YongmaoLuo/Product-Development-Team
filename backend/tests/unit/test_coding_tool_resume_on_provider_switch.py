"""A provider switch resumes the session the previous provider died in.

What this pins
--------------
When a dispatch times out on one provider and retries on the next, the
retry used to open a brand-new session and re-send the whole prompt. For
a sub-agent task that is minutes of reading files, running test suites and
editing files, paid for a second time from zero.

The contract these tests hold in place:

1. **The switch resumes.** The second dispatch carries
   ``--resume <the id attempt 1 was pinned to>`` — not
   ``--session-id <a new uuid>`` and not no session flag at all.
2. **The id is pinned before the first attempt.** A CLI-chosen id is
   never returned on the failure path, so a switch would have nothing to
   resume.
3. **Success is untouched.** A call that succeeds first time makes
   exactly one CLI call and never resumes.
4. **An empty reply still opens a fresh session.** A session that never
   produced anything has no transcript, so resuming it cannot work; that
   retry stays on a new id and never enters the resume path.
5. **A resume that cannot be honoured degrades, it does not fail.** The
   call falls back to a fresh session, says so in the log, and still
   returns a result.
6. **The JSON path resumes too**, and its reply stays parseable.
7. **A capacity error is answered on the same provider.** 429 / 402 /
   503 / 529 land in the ``except ApiError`` arm, which used to
   re-dispatch into a brand-new session and re-send the whole prompt.
   A refusal that reads as a *rate limit* is now waited out on the
   provider that gave it (10s / 20s / 40s / 80s) and asked again
   there; only a provider that refuses every one of those waits is
   parked and dropped.
8. **A capacity error does not re-send the prompt.** A caller that holds
   a reusable session (the executor's ``query_in_session``) used to have
   its *whole* prompt re-sent on every 429, because the switch
   inherited the re-ask text. The prompt the failed attempt already
   sent is already in the restored transcript, so the switch says
   "continue" and nothing more.

Why (4) and (5) are not the same thing
--------------------------------------
They look like the same event — a retry that cannot use the session it was
offered — but they are opposite. (4) is a session that was *never created*:
the CLI never wrote a transcript for it, so there is nothing to re-open and
a new one is the only retry that can work. (5) is a session that *did* exist
and whose re-open failed anyway (pruned, mis-formatted, provider error on
the resume call). Treating (5) as fatal would turn a recoverable provider
hiccup into a dead task; treating (4) as resumable would spend a retry on a
guaranteed failure. The two are told apart by which path raises: an empty
reply is caught inside ``_run_interactive_resilient`` and retried on a fresh
session without ever reaching the provider switch, while a resume failure is
caught by the provider-switch helper and logged.

Why a switch and a re-ask are not the same thing either
-------------------------------------------------------
Both re-open a session, and both were decided by the same ``from_handle``
flag — but they carry different text, because the restored conversation
holds different things. A *re-ask* is a new ask the transcript has never
seen (the validator's objection, the reason the last answer was rejected),
so it has to travel with the turn: ``RESUME_REASK_PROMPT + prompt``. A
*switch* is the same ask on another provider, and that ask is already the
last thing in the transcript — the failed attempt sent it. Re-sending it
pays for the same tokens twice and leaves the model guessing which copy is
current, so the switch sends the continuation cue alone.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool as ct  # noqa: E402
import provider_order  # noqa: E402
from coding_tool import (  # noqa: E402
    ApiError,
    ClaudeCodingTool,
    EmptyResponseError,
    JSON_ONLY_SUFFIX,
    JsonSession,
    RESUME_CONTINUATION_PROMPT,
    RESUME_REASK_PROMPT,
    query_in_session,
    query_json_in_session,
)


@pytest.fixture(autouse=True)
def _no_rate_limit_waits(monkeypatch):
    """The rate-limit backoff's waits are replaced, not served.

    A dispatch whose provider answers 429 now waits that provider out
    (10s / 20s / 40s / 80s) and asks again on the SAME provider before
    it gives up and rotates. Those waits are replaced here so a test
    reads the decision rather than the patience of the suite.

    The live usage reading is stubbed for the same reason a unit test
    does not ask CC Switch anything: the answer would depend on the
    machine the test runs on, and it is not what these cases are about.
    """
    monkeypatch.setattr(ct, "_rate_limit_sleep", lambda seconds: None)
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )


def _tool(monkeypatch, side_effect, *, calls=None):
    """A tool whose ``_run_claude_interactive`` is driven by ``side_effect``.

    ``side_effect(n)`` receives the 1-based dispatch index and returns
    either ``(text, session_id, meta)`` or an exception instance to raise.
    Every dispatch is recorded — prompt positionally, the rest by keyword —
    so a test can assert on what the launcher was asked to run.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded = calls if calls is not None else []

    def _spy(prompt, *args, **kwargs):
        recorded.append({"prompt": prompt, **kwargs})
        if "provider" not in kwargs:
            # The real launcher stamps the provider it selected; the
            # fallback handlers key off it.
            tool.current_call_provider = f"provider-{len(recorded)}"
        result = side_effect(len(recorded), tool)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_claude_interactive", _spy)
    tool._calls = recorded
    return tool


def _timeout_then(text="done", sid="sid-2"):
    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return TimeoutError("simulated provider timeout")
        return (text, sid, {})

    return _side


# ---------------------------------------------------------------------------
# 1. The switch resumes the session the timeout killed
# ---------------------------------------------------------------------------


def test_provider_switch_resumes_the_pinned_session(monkeypatch):
    tool = _tool(monkeypatch, _timeout_then())
    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "one attempt + one provider switch"
    first, second = tool._calls

    assert second["resume"] is True, (
        "the retry must --resume the session attempt 1 was using; "
        "starting a new session re-pays for the whole task"
    )
    assert second["session_id"] == first["session_id"], (
        "it must resume THAT session, not pin a fresh one"
    )
    assert "-" in first["session_id"], (
        "--session-id requires the dashed uuid form; the undashed "
        "uuid4().hex is rejected with 'Invalid session ID'"
    )


def test_the_resumed_turn_does_not_resend_the_whole_prompt(monkeypatch):
    """The restored conversation carries the prompt; re-sending doubles it."""
    tool = _tool(monkeypatch, _timeout_then())
    tool.query("do the task", timeout=10)

    first, second = tool._calls
    assert second["prompt"] != first["prompt"]
    assert first["prompt"] not in second["prompt"], (
        "the original prompt is already in the resumed conversation; "
        "sending it again pays for it twice and leaves the model "
        "guessing which copy is current"
    )
    assert "continue" in second["prompt"].lower()


def test_the_session_id_is_pinned_before_the_first_attempt(monkeypatch):
    """A CLI-chosen id is never returned on the failure path."""
    tool = _tool(monkeypatch, _timeout_then())
    tool.query("do the task", timeout=10)

    first = tool._calls[0]
    assert first["session_id"] is not None, (
        "the first attempt must pin its own session id, otherwise the "
        "provider switch has no id to resume and the whole feature is "
        "unreachable"
    )
    assert first["resume"] is False, "a fresh session is created, not resumed"


# ---------------------------------------------------------------------------
# 2. Reverse controls — the paths that must not move
# ---------------------------------------------------------------------------


def test_a_call_that_succeeds_makes_one_dispatch_and_never_resumes(monkeypatch):
    def _side(n, tool):
        return ("all good", "sid-1", {})

    tool = _tool(monkeypatch, _side)
    assert tool.query("do the task", timeout=10) == "all good"

    assert len(tool._calls) == 1, "a successful call must not retry at all"
    assert tool._calls[0]["resume"] is False


def test_an_empty_reply_retries_on_a_fresh_session_and_never_resumes(monkeypatch):
    """The empty-response design: a session that never produced anything
    has no transcript, so resuming it cannot work."""

    def _side(n, tool):
        return EmptyResponseError("empty", provider="vendor-a-pro", returncode=0)

    tool = _tool(monkeypatch, _side)
    with pytest.raises(EmptyResponseError):
        tool.query("do the task", timeout=10)

    assert tool._calls, "the dispatch was attempted"
    assert all(c["resume"] is False for c in tool._calls), (
        "an empty reply must never take the resume path"
    )
    ids = [c["session_id"] for c in tool._calls]
    assert len(set(ids)) == len(ids), (
        "each empty-reply retry gets its own session; re-using the dead "
        "id reproduces the guaranteed-to-fail resume"
    )


# ---------------------------------------------------------------------------
# 3. A resume that cannot be honoured degrades instead of failing
# ---------------------------------------------------------------------------


def test_failed_resume_falls_back_to_a_fresh_session_and_still_succeeds(monkeypatch):
    """What the CLI does when the session is not there to resume."""

    def _side(n, tool):
        if n == 1:
            tool.current_call_provider = "vendor-a-pro"
            return TimeoutError("simulated provider timeout")
        if tool._calls[n - 1]["resume"]:
            # A session with no transcript resumes like this: the CLI
            # exits immediately with an error result.
            return ApiError(
                "Claude API returned error status: unknown", status="unknown"
            )
        return ("recovered", tool._calls[n - 1]["session_id"], {})

    tool = _tool(monkeypatch, _side)
    assert tool.query("do the task", timeout=10) == "recovered"

    resumed = [c for c in tool._calls if c["resume"]]
    assert len(resumed) == 1, "the switch tried to resume once"
    fallback = tool._calls[-1]
    assert fallback["resume"] is False, "and then fell back to a fresh session"
    assert fallback["session_id"] != resumed[0]["session_id"], (
        "the fallback needs its own session — resuming the same dead id "
        "again would fail the same way"
    )


def test_the_fallback_is_logged_with_its_reason(monkeypatch):
    def _side(n, tool):
        if n == 1:
            tool.current_call_provider = "vendor-a-pro"
            return TimeoutError("simulated provider timeout")
        if tool._calls[n - 1]["resume"]:
            return ApiError("No conversation found", status="unknown")
        return ("recovered", tool._calls[n - 1]["session_id"], {})

    tool = _tool(monkeypatch, _side)
    tool.query("do the task", timeout=10)

    events = [
        call for call in tool.logger.warning.call_args_list
        if call[0] and call[0][0] == "coding_resume_fallback"
    ]
    assert events, (
        "a silent degradation is indistinguishable from a resume that "
        "worked; the fallback has to leave a trace"
    )
    data = events[0][1]["data"]
    assert data["fallback"] == "fresh_session"
    assert data["resume"] is True
    assert data["error"]


def test_a_hard_timeout_on_the_resume_is_not_treated_as_a_resume_failure(
    monkeypatch,
):
    """A timeout means the provider is slow, not that the session is gone.

    It must reach the caller's existing timeout routing (auto-split,
    next provider) rather than being retried as a fresh session here.
    """

    def _side(n, tool):
        if n == 1:
            tool.current_call_provider = "vendor-a-pro"
            return TimeoutError("simulated provider timeout")
        if tool._calls[n - 1]["resume"]:
            return ct.HardTimeoutError(total_sec=10, elapsed=11.0, last_line="")
        return ("unreachable", "sid", {})

    tool = _tool(monkeypatch, _side)
    with pytest.raises(ct.HardTimeoutError):
        tool.query("do the task", timeout=10)

    assert len([c for c in tool._calls if not c["resume"]]) == 1, (
        "no fresh-session retry: the provider being slow is not a resume "
        "failure and must keep its own routing"
    )


# ---------------------------------------------------------------------------
# 4. The JSON path resumes the same way
# ---------------------------------------------------------------------------


def test_json_provider_switch_resumes_and_the_reply_still_parses(monkeypatch):
    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return TimeoutError("simulated provider timeout")
        return (json.dumps({"verdict": "pass"}), "sid-2", {})

    tool = _tool(monkeypatch, _side)
    result = tool.query_json("produce a verdict", timeout=10)

    assert result == {"verdict": "pass"}
    first, second = tool._calls
    assert second["resume"] is True
    assert second["session_id"] == first["session_id"]
    assert "Return ONLY the JSON object" in second["prompt"], (
        "a resumed turn still has to be told the format — the restored "
        "context ends on the previous attempt's output, not on the ask"
    )


def test_json_first_attempt_success_is_unchanged(monkeypatch):
    def _side(n, tool):
        return (json.dumps({"ok": True}), "sid-1", {})

    tool = _tool(monkeypatch, _side)
    assert tool.query_json("produce a verdict", timeout=10) == {"ok": True}
    assert len(tool._calls) == 1
    assert tool._calls[0]["resume"] is False


def test_json_failed_resume_falls_back_to_a_fresh_session(monkeypatch):
    def _side(n, tool):
        if n == 1:
            tool.current_call_provider = "vendor-a-pro"
            return TimeoutError("simulated provider timeout")
        if tool._calls[n - 1]["resume"]:
            return ApiError("No conversation found", status="unknown")
        return (json.dumps({"ok": True}), tool._calls[n - 1]["session_id"], {})

    tool = _tool(monkeypatch, _side)
    assert tool.query_json("produce a verdict", timeout=10) == {"ok": True}

    resumed = [c for c in tool._calls if c["resume"]]
    assert len(resumed) == 1
    assert tool._calls[-1]["resume"] is False
    assert tool._calls[-1]["session_id"] != resumed[0]["session_id"]


# ---------------------------------------------------------------------------
# 5. The launcher itself — the argv the CLI is actually handed
# ---------------------------------------------------------------------------


def _install_fake_claude(tmp_path, monkeypatch, script: str) -> Path:
    """Put an executable ``claude`` first on ``PATH``; return its argv log."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    argv_log = tmp_path / "argv.log"
    fake = bindir / "claude"
    fake.write_text(script.replace("@ARGV_LOG@", str(argv_log)), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return argv_log


def test_the_launcher_builds_resume_with_the_first_session_id(
    tmp_path, monkeypatch
):
    """End of the chain: ``_run_claude_interactive`` → the real argv.

    The tests above assert on the kwargs the launcher is called with;
    this one asserts on the command line the CLI would execute, so the
    flag cannot be renamed or dropped between the two.
    """
    argv_log = _install_fake_claude(
        tmp_path,
        monkeypatch,
        '#!/bin/sh\n'
        'for a in "$@"; do echo "$a" >> @ARGV_LOG@; done\n'
        'echo \'{"type":"system","subtype":"init","session_id":"sid-x"}\'\n'
        'echo \'{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\'\n'
        'echo \'{"type":"result","result":"ok","is_error":false}\'\n',
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_check_provider_availability",
        staticmethod(lambda provider_name=None: (
            True,
            {
                "base_url": "https://example.com",
                "api_key": "k",
                "models": {"sonnet": "m", "haiku": "m", "opus": "m", "default": "m"},
            },
        )),
    )

    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0

    # The first attempt is failed here rather than by the fake CLI so
    # that exactly one process is ever spawned: the one the provider
    # switch launches. Its argv is what this test reads.
    real_run = tool._run_claude_interactive
    pinned = {}
    seen = {"n": 0}

    def _spy(prompt, *args, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            tool.current_call_provider = "vendor-a-pro"
            # Whatever id the production code pinned for this attempt is
            # the id the switch has to come back with.
            pinned["session_id"] = kwargs["session_id"]
            assert kwargs["resume"] is False
            raise TimeoutError("simulated provider timeout")
        return real_run(prompt, *args, **kwargs)

    monkeypatch.setattr(tool, "_run_claude_interactive", _spy)
    result = tool.query("do the task", timeout=10)

    assert result == "ok"
    argv = argv_log.read_text(encoding="utf-8").splitlines()
    first_session = pinned["session_id"]

    assert first_session in argv, (
        f"the pinned session id never reached the command line: {argv}"
    )
    idx = argv.index(first_session)
    assert argv[idx - 1] == "--resume", (
        f"expected --resume <{first_session}>, argv was: {argv}"
    )
    assert "--session-id" not in argv, (
        "a resumed dispatch must not also pin a new session id"
    )


# ---------------------------------------------------------------------------
# 6. A rate limit is waited out on the same provider
# ---------------------------------------------------------------------------
#
# 429 / 402 / 503 / 529 land in the ``except ApiError`` arm of the
# provider-fallback handlers. That arm used to re-dispatch into a
# brand-new session (``_call(excluded=...)`` / ``_query_json_internal(
# excluded=...)``), which re-sent the whole prompt from nothing.
#
# The timeout arm above already had the right answer — hand the retry the
# session the previous attempt was using — and this is the same switch, so
# it gets the same answer. The case that makes it expensive is the
# observed one: a sub-agent that ran 52 rounds over 38 minutes and then
# hit a rate limit. Re-sending the prompt from zero pays for every one of
# those rounds a second time.
#
# The session that died may never have produced a token (the 429 arrived
# before the first one), and that is fine: the resume is best-effort and
# degrades to a fresh session by itself, so a switch on a session that
# does not exist costs one failed resume, not a dead task.
#
# What changed in 2026-10-22 is WHICH provider the retry goes to. A
# refusal that reads as a rate limit is not a spent budget: the provider
# is waited out (10s / 20s / 40s / 80s) and asked again on the SAME
# provider, and only a provider that refuses every one of those waits is
# parked and dropped. So the retry here is NOT a provider switch any
# more: it resumes the same session, on the same provider, and the
# provider is not excluded while the wait is still working.

#: The statuses that mean "this provider has no capacity right now".
_CAPACITY_STATUSES = ("429", "402", "503", "529")


def _capacity_error_then(text="done", sid="sid-2", status="429"):
    """First dispatch dies on a capacity error; the next one succeeds."""

    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return ApiError("rate limited", status=status)
        return (text, sid, {})

    return _side


@pytest.mark.parametrize("status", _CAPACITY_STATUSES)
def test_a_rate_limit_backoff_retry_resumes_the_pinned_session(
    monkeypatch, status,
):
    """The interactive path: a rate-limited retry must resume."""
    tool = _tool(monkeypatch, _capacity_error_then(status=status))
    assert tool.query("do the task", timeout=10) == "done"

    assert len(tool._calls) == 2, "one attempt + one backoff retry"
    first, second = tool._calls

    assert second["resume"] is True, (
        f"a {status} must be answered by resuming the session the "
        "failed attempt was using, not by opening a new one"
    )
    assert second["session_id"] == first["session_id"], (
        "it must resume THAT session — a fresh id restarts the task from "
        "nothing and re-pays for every round already done"
    )
    assert "vendor-a-pro" not in (second["excluded_providers"] or []), (
        "the provider is not excluded while the backoff is still working: "
        "a rate limit clears itself, and excluding the provider here is "
        "how a healthy one disappeared from the pool for half an hour"
    )


@pytest.mark.parametrize("status", _CAPACITY_STATUSES)
def test_json_rate_limit_backoff_retry_resumes_the_pinned_session(
    monkeypatch, status
):
    """The JSON path: same backoff retry, same contract."""

    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return ApiError("rate limited", status=status)
        return (json.dumps({"verdict": "pass"}), "sid-2", {})

    tool = _tool(monkeypatch, _side)
    result = tool.query_json("produce a verdict", timeout=10)

    assert result == {"verdict": "pass"}
    assert len(tool._calls) == 2, "one attempt + one backoff retry"
    first, second = tool._calls

    assert second["resume"] is True, (
        f"a {status} must be answered by resuming the session the "
        "failed attempt was using, not by opening a new one"
    )
    assert second["session_id"] == first["session_id"], (
        "it must resume THAT session — a fresh id restarts the task from "
        "nothing and re-pays for every round already done"
    )
    assert "vendor-a-pro" not in (second["excluded_providers"] or []), (
        "the provider is not excluded while the backoff is still working: "
        "a rate limit clears itself, and excluding the provider here is "
        "how a healthy one disappeared from the pool for half an hour"
    )


def test_a_capacity_error_whose_resume_fails_still_returns_the_answer(
    monkeypatch,
):
    """The degradation, reached through the ApiError entry.

    The switch resumes; the re-open is refused (the session was never
    written, or was pruned). That must cost one failed resume, not the
    task: the call falls back to a fresh session and still answers.
    """

    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return ApiError("rate limited", status="429")
        if tool._calls[n - 1]["resume"]:
            # A session with no transcript re-opens like this.
            return ApiError("No conversation found", status="unknown")
        return ("recovered", tool._calls[n - 1]["session_id"], {})

    tool = _tool(monkeypatch, _side)
    assert tool.query("do the task", timeout=10) == "recovered"

    assert len(tool._calls) == 3, "attempt + resume attempt + fresh fallback"
    resumed = [c for c in tool._calls if c["resume"]]
    assert len(resumed) == 1, "the switch tried to resume once"
    fallback = tool._calls[-1]
    assert fallback["resume"] is False, "and then fell back to a fresh session"
    assert fallback["session_id"] != resumed[0]["session_id"], (
        "the fallback needs its own session — resuming the same dead id "
        "again would fail the same way"
    )


def test_the_capacity_error_degradation_is_logged(monkeypatch):
    """A silent degradation is indistinguishable from a resume that worked."""

    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return ApiError("rate limited", status="429")
        if tool._calls[n - 1]["resume"]:
            return ApiError("No conversation found", status="unknown")
        return ("recovered", tool._calls[n - 1]["session_id"], {})

    tool = _tool(monkeypatch, _side)
    tool.query("do the task", timeout=10)

    events = [
        call for call in tool.logger.warning.call_args_list
        if call[0] and call[0][0] == "coding_resume_fallback"
    ]
    assert events, (
        "a resume that could not be honoured has to leave a trace"
    )
    data = events[0][1]["data"]
    assert data["fallback"] == "fresh_session"
    assert data["resume"] is True
    assert data["error"]


# ---------------------------------------------------------------------------
# 7. A capacity error must not re-send the prompt to a caller holding a session
# ---------------------------------------------------------------------------
#
# The executor drives one task per session (``query_in_session``), so the
# dispatch that hits the rate limit is a caller's *own* ask inside a
# session it holds. The switch that follows re-opens that session — and
# the prompt the failed attempt already sent is already the last thing in
# the restored transcript.
#
# Before the fix the switch inherited the caller's *re-ask* text
# (``RESUME_REASK_PROMPT + prompt``), so every 429 re-sent the whole task
# prompt. The re-ask is right for the caller's own next round (a new ask
# the transcript has never seen) and wrong for the switch (the same ask,
# already in the transcript), and the two used to be told apart by a flag
# that could not tell them apart.

#: A prompt long enough to be unmistakable in a transcript, and short
#: enough that ``_compact_prompt`` leaves it alone.
_TASK_PROMPT = "implement the whole task, reading every file it names"


def _ok_then(text="done", sid="sid-1"):
    """Every dispatch answers, in the session it was asked for."""

    def _side(n, tool):
        return (text, sid, {})

    return _side


def test_a_capacity_error_does_not_resend_the_prompt_to_a_caller_holding_a_session(
    monkeypatch,
):
    """The interactive path: a 429 switch continues, it does not re-ask.

    The whole task prompt is the expensive thing here — it names every
    file the sub-agent has to read — and it was already sent by the
    attempt that hit the rate limit. Sending it again pays for it twice
    and leaves the model working out which copy is current.
    """
    tool = _tool(monkeypatch, _capacity_error_then(status="429"))
    session = JsonSession()

    query_in_session(tool, session, _TASK_PROMPT)

    assert len(tool._calls) == 2, "attempt + provider switch"
    first, second = tool._calls

    assert first["prompt"] == _TASK_PROMPT, (
        "the first attempt sends the caller's prompt as written"
    )
    assert second["resume"] is True, (
        "the switch still resumes the session the failed attempt used"
    )
    assert second["prompt"] == RESUME_CONTINUATION_PROMPT, (
        "the switch continues the ask the transcript already carries; "
        "it must not re-ask, because a re-ask re-sends the prompt"
    )
    assert _TASK_PROMPT not in second["prompt"], (
        "the prompt is already in the restored conversation; re-sending "
        "it pays for the whole task twice"
    )


def test_a_json_capacity_error_does_not_resend_the_prompt_to_a_caller_holding_a_session(
    monkeypatch,
):
    """The JSON path: same switch, same contract, plus the format cue."""

    def _side(n, tool):
        tool.current_call_provider = "vendor-a-pro"
        if n == 1:
            return ApiError("rate limited", status="429")
        return (json.dumps({"verdict": "pass"}), "sid-2", {})

    tool = _tool(monkeypatch, _side)
    session = JsonSession()

    result = query_json_in_session(tool, session, _TASK_PROMPT)

    assert result == {"verdict": "pass"}
    assert len(tool._calls) == 2, "attempt + provider switch"
    first, second = tool._calls

    assert second["resume"] is True
    assert second["prompt"] == RESUME_CONTINUATION_PROMPT + JSON_ONLY_SUFFIX, (
        "the continuation cue still carries the JSON-only instruction: "
        "the restored context ends on the previous attempt's own turn"
    )
    assert _TASK_PROMPT not in second["prompt"], (
        "the prompt is already in the restored conversation; re-sending "
        "it pays for the whole task twice"
    )


def test_the_callers_own_next_round_still_reasks_with_the_prompt(monkeypatch):
    """The other side of the fix: the caller's own re-ask is unchanged.

    The caller's next round is a *new* ask — the prompt has moved on
    since the last one (the validator's objection, the reason the last
    answer was rejected) — and that text exists nowhere in the
    transcript. The switch cue alone would make this round answer the
    previous round's question again, which is the one outcome reuse
    must never produce.
    """
    tool = _tool(monkeypatch, _ok_then())
    session = JsonSession()

    query_in_session(tool, session, "round one")
    query_in_session(tool, session, "round two asks something new")

    first, second = tool._calls
    assert first["resume"] is False, "round one opens the session"
    assert second["resume"] is True, "round two re-opens it"
    assert second["prompt"] == RESUME_REASK_PROMPT + "round two asks something new", (
        "the caller's own next round is a re-ask: the new request has to "
        "travel with the turn"
    )


def test_the_callers_own_next_json_round_still_reasks_with_the_prompt(monkeypatch):
    """The JSON twin of the test above."""

    def _side(n, tool):
        return (json.dumps({"ok": True}), "sid-1", {})

    tool = _tool(monkeypatch, _side)
    session = JsonSession()

    query_json_in_session(tool, session, "round one")
    query_json_in_session(tool, session, "round two asks something new")

    first, second = tool._calls
    assert first["resume"] is False
    assert second["resume"] is True
    assert second["prompt"] == (
        RESUME_REASK_PROMPT + "round two asks something new" + JSON_ONLY_SUFFIX
    ), (
        "the caller's own next round is a re-ask: the new request has to "
        "travel with the turn, and the format instruction with it"
    )
