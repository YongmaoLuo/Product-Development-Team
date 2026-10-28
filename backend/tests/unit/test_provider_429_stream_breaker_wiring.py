"""The 429 stream-level stall breaker, wired into the read loop.

What is pinned here
-------------------
The state machine itself is pinned by
``test_provider_429_stream_breaker_state.py``. What this file pins is
the WIRING — that ``_run_claude_interactive``'s read loop actually feeds
the stream to that state machine, and that a stall ends the dispatch the
way the architecture decided it should:

* a storm of ``api_error`` events trips the breaker → the child is shut
  down GRACEFULLY (never a bare SIGKILL) and the call fails with an
  ``ApiError`` whose status is ``429``;
* that error's wording carries 「用量上限」, so
  ``_classify_provider_failure`` calls it ``exhausted`` — which parks
  the provider and moves the dispatch on to the next one, instead of
  retrying the same dead stream;
* a stream that produces something after its errors is NOT broken;
* a scene the breaker is not armed for only REPORTS the stall.

Why the clock is faked
----------------------
The breaker's window is measured in seconds. A test that has to sit
through a real minute to watch a window elapse is a test nobody runs,
so the clock is installed over ``coding_tool.time`` and the fake
``claude``'s stdout advances it by hand: "two errors 61 seconds apart"
is a fact the test states rather than a fact it waits for.

The one test that runs a REAL child process
-------------------------------------------
The mocked tests can show that the shutdown was *called*; only a real
process can show that the child is *gone*. The last test in this file
runs a real ``claude`` on ``PATH`` and checks that nothing is left
behind — the difference between "the shutdown ran" and "no orphan".
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
from coding_tool import ApiError, ClaudeCodingTool  # noqa: E402


def _breaker_text() -> str:
    """The message the breaker raises as.

    Read through the module rather than imported by name on purpose: a
    missing constant must fail the test that needs it, not the
    collection of the whole file.
    """
    return coding_tool.STREAM_BREAKER_TEXT


#: The stall the operator's defaults describe: three consecutive
#: ``api_error`` events spanning more than a minute.
STORM = [
    (0.0, {"type": "system", "subtype": "api_error", "error": {"status": 429}}),
    (30.0, {"type": "system", "subtype": "api_error", "error": {"status": 429}}),
    (31.0, {"type": "system", "subtype": "api_error", "error": {"status": 429}}),
]


def _api_error():
    return {"type": "system", "subtype": "api_error", "error": {"status": 429}}


def _assistant(text: str = "the answer"):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _result(text: str = "the answer"):
    return {"type": "result", "subtype": "success", "result": text, "is_error": False}


class _FakeClock:
    """A monotonic clock the test drives by hand.

    Installed over ``coding_tool.time``. Every attribute other than
    ``monotonic`` is delegated to the real module, so the dispatch path
    keeps working — it just reads a clock the test controls.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def __getattr__(self, name):  # pragma: no cover - delegation shim
        return getattr(time, name)


class _RecordingLogger:
    """A structured logger that keeps what it was told.

    The production logger writes JSON lines into the plan's execution
    log; this one keeps the same calls in memory so the test can ask
    "was the stall reported?" without reading a file.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
        self.events.append(
            {
                "level": level,
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            }
        )

    def debug(self, event, message, **kwargs):
        self._record("debug", event, message, **kwargs)

    def info(self, event, message, **kwargs):
        self._record("info", event, message, **kwargs)

    def warning(self, event, message, **kwargs):
        self._record("warning", event, message, **kwargs)

    def error(self, event, message, **kwargs):
        self._record("error", event, message, **kwargs)

    def critical(self, event, message, **kwargs):
        self._record("critical", event, message, **kwargs)

    def named(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]


def _fake_stdout(clock: _FakeClock, events):
    """A ``claude`` event stream.

    Each entry is ``(delay, payload)``: the clock is advanced BY the
    delay BEFORE the line is emitted, so the read loop sees the clock
    the delay bought when it reads the line.
    """
    for delay, payload in events:
        clock.advance(delay)
        yield json.dumps(payload) + "\n"


def _make_process(clock: _FakeClock, events):
    """A ``Popen`` stand-in whose stdout is the fake event stream."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stderr.read.return_value = ""
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    proc.stdout = _fake_stdout(clock, events)
    return proc


def _make_tool() -> tuple[ClaudeCodingTool, _RecordingLogger]:
    """A tool with no provider lookup, no DB and no network.

    ``inherit_providers`` skips the scene→provider resolution, which
    needs a CC Switch database; the scene still reaches the breaker,
    because the breaker reads the scene, not the provider.
    """
    log = _RecordingLogger()
    tool = ClaudeCodingTool(logger=log)
    tool.inherit_providers = True
    return tool, log


def _spy_graceful_shutdown(tool: ClaudeCodingTool) -> list[tuple]:
    """Record every graceful shutdown, and whether an error was in flight.

    The second element of each record says whether an exception was
    being propagated at the moment of the call. That is what separates
    the shutdown the breaker performs from the idempotent repeat the
    read loop's ``finally`` performs: the first one happens with
    nothing in flight (the breaker broke the stream deliberately), the
    second one happens while the ``ApiError`` is on its way out.
    """
    calls: list[tuple] = []
    original = tool._graceful_shutdown

    def _spy(process):
        calls.append((process, sys.exc_info()[0] is not None))
        return original(process)

    tool._graceful_shutdown = _spy
    return calls


# ---------------------------------------------------------------------------
# The storm
# ---------------------------------------------------------------------------


def test_api_error_storm_raises_429_and_reaps_child(monkeypatch):
    """Three api_error events spanning a minute end the dispatch.

    The failure has to be an ``ApiError`` with status ``429`` — that is
    what the provider-fallback layer reads when it decides whether to
    park this provider and move on to the next one.
    """
    clock = _FakeClock()
    monkeypatch.setattr(coding_tool, "time", clock)
    tool, log = _make_tool()
    shutdowns = _spy_graceful_shutdown(tool)
    proc = _make_process(clock, STORM)
    monkeypatch.setattr(
        coding_tool.subprocess, "Popen", lambda cmd, **kw: proc,
    )

    with pytest.raises(ApiError) as excinfo:
        tool._run_claude_interactive("hello", scene="execution")

    err = excinfo.value
    assert err.status == "429", (
        f"the stall must surface as a 429, got {err.status!r}"
    )
    breaker_text = _breaker_text()
    assert err.args[0] == breaker_text
    assert str(err) == f"[API_ERROR:429] {breaker_text}"

    # The wording decides what happens next: 「用量上限」 is an
    # exhaustion marker, so the provider is parked and the dispatch
    # moves on — instead of a plain 429, which would earn a short
    # rate-limit park and a retry against the same dead stream.
    assert coding_tool._classify_provider_failure(err) == "exhausted"

    # The child was shut down gracefully, BEFORE the raise — not only
    # by the read loop's ``finally``, and never with a bare SIGKILL.
    assert shutdowns, "the breaker must shut the child down"
    assert shutdowns[0][0] is proc
    assert shutdowns[0][1] is False, (
        "the graceful shutdown must happen with no exception in flight — "
        "i.e. in the breaker itself, not only in the read loop's finally"
    )
    assert proc.wait.call_args is not None
    assert proc.wait.call_args.kwargs.get("timeout") == (
        ClaudeCodingTool.GRACEFUL_STOP_TIMEOUT
    )

    # And the report says how many retries there were and how long the
    # stream produced nothing.
    observed = log.named("provider_429_stream_breaker_observed")
    assert observed, "the stall must be reported"
    data = observed[0]["data"]
    assert data["retries"] == 3
    assert data["silent_sec"] > 0
    assert data["scene"] == "execution"
    assert data["armed"] is True
    assert log.named("provider_429_stream_breaker_tripped"), (
        "an armed scene must also say that it broke the stream"
    )


def test_no_direct_sigkill_is_ever_used(monkeypatch):
    """The break must be the graceful one — never a bare SIGKILL.

    A SIGKILL aimed at the child alone would leave the child's own
    children running and the process group orphaned, which is exactly
    what ``_graceful_shutdown`` exists to prevent.
    """
    sent: list[int] = []

    def _record_kill(proc, *, sig=None, wait_timeout=5.0):
        sent.append(sig)
        return None

    monkeypatch.setattr(coding_tool, "kill_process_group", _record_kill)

    clock = _FakeClock()
    monkeypatch.setattr(coding_tool, "time", clock)
    tool, _log = _make_tool()
    proc = _make_process(clock, STORM)
    monkeypatch.setattr(
        coding_tool.subprocess, "Popen", lambda cmd, **kw: proc,
    )

    with pytest.raises(ApiError):
        tool._run_claude_interactive("hello", scene="execution")

    assert sent == [], (
        f"the breaker must not signal the process group itself: {sent}"
    )


# ---------------------------------------------------------------------------
# A stream that recovered
# ---------------------------------------------------------------------------


def test_429_then_output_does_not_trip(monkeypatch):
    """Errors followed by output are not a stall.

    The errors here are spread over more than the window, and there are
    more than the streak's worth of them — so a breaker that ignored
    production lines WOULD trip here. The assistant line is what stops
    it, and this test is what keeps that line wired up.
    """
    clock = _FakeClock()
    monkeypatch.setattr(coding_tool, "time", clock)
    tool, log = _make_tool()
    shutdowns = _spy_graceful_shutdown(tool)
    events = [
        (0.0, _api_error()),
        (30.0, _api_error()),
        (10.0, _assistant("the answer")),
        (5.0, _api_error()),
        (5.0, _api_error()),
        (11.0, _api_error()),
        (1.0, _result("the answer")),
    ]
    proc = _make_process(clock, events)
    monkeypatch.setattr(
        coding_tool.subprocess, "Popen", lambda cmd, **kw: proc,
    )

    text, _session_id, _meta = tool._run_claude_interactive(
        "hello", scene="execution",
    )

    assert "the answer" in text
    assert not log.named("provider_429_stream_breaker_tripped"), (
        "a stream that produced an answer must not be broken"
    )
    # One shutdown only: the read loop's own cleanup reap at the end of
    # a normal exit. A break would have added a second one — the
    # breaker's own, before the raise.
    assert len(shutdowns) == 1, (
        f"expected only the cleanup reap, got {shutdowns}"
    )


# ---------------------------------------------------------------------------
# A scene the breaker is not armed for
# ---------------------------------------------------------------------------


def test_non_execution_scene_only_logs(monkeypatch):
    """A scene outside the armed set reports the stall and keeps reading.

    ``planning`` is not armed by default (the armed set is
    ``{"execution"}``), so the breaker must NOT break the stream there —
    but the observation is still worth having, so it is logged.
    """
    clock = _FakeClock()
    monkeypatch.setattr(coding_tool, "time", clock)
    tool, log = _make_tool()
    shutdowns = _spy_graceful_shutdown(tool)
    events = list(STORM) + [
        (1.0, _assistant("the answer")),
        (1.0, _result("the answer")),
    ]
    proc = _make_process(clock, events)
    monkeypatch.setattr(
        coding_tool.subprocess, "Popen", lambda cmd, **kw: proc,
    )

    text, _session_id, _meta = tool._run_claude_interactive(
        "hello", scene="planning",
    )

    assert "the answer" in text, "an unarmed scene must keep reading"

    observed = log.named("provider_429_stream_breaker_observed")
    assert observed, "the stall must be reported even where it is not armed"
    data = observed[0]["data"]
    assert data["scene"] == "planning"
    assert data["armed"] is False
    assert data["retries"] == 3
    assert not log.named("provider_429_stream_breaker_tripped"), (
        "an unarmed scene must not break the stream"
    )
    # One shutdown only: the read loop's own cleanup reap at the end of
    # a normal exit. A break would have added a second one — the
    # breaker's own, before the raise.
    assert len(shutdowns) == 1, (
        f"an unarmed scene must not shut the child down early: {shutdowns}"
    )


# ---------------------------------------------------------------------------
# The module logger carries the event name too
# ---------------------------------------------------------------------------


def test_stall_is_reported_on_the_module_logger(monkeypatch, caplog):
    """The console tail names the event, not just the plan's log.

    Two sinks, one fact: the structured logger writes into the plan's
    execution log, the module logger into the console. Either one can
    answer "was this stall seen?".
    """
    caplog.set_level(logging.WARNING, logger="coding_tool")
    clock = _FakeClock()
    monkeypatch.setattr(coding_tool, "time", clock)
    tool, _log = _make_tool()
    proc = _make_process(clock, STORM)
    monkeypatch.setattr(
        coding_tool.subprocess, "Popen", lambda cmd, **kw: proc,
    )

    with pytest.raises(ApiError):
        tool._run_claude_interactive("hello", scene="execution")

    assert "provider_429_stream_breaker_observed" in caplog.text


# ---------------------------------------------------------------------------
# A real child process
# ---------------------------------------------------------------------------


#: A real ``claude`` that answers with nothing but rate limits, and
#: then stays alive — so the shutdown has to be the thing that ends it.
_REAL_FAKE_CLAUDE = """#!@PYTHON@
import json, sys, time


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


for _ in range(3):
    emit({"type": "system", "subtype": "api_error", "error": {"status": 429}})
    time.sleep(0.8)
time.sleep(120)
"""


def test_real_child_is_gone_after_the_breaker_trips(tmp_path, monkeypatch):
    """The child really is gone — the break leaves no orphan.

    The mocked tests above can show that the shutdown was *called*.
    Only a real process shows that the process is *gone*: the fake
    ``claude`` ignores stdin EOF and stays alive, so the child can only
    disappear because the graceful shutdown terminated it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_claude = bindir / "claude"
    fake_claude.write_text(
        _REAL_FAKE_CLAUDE.replace("@PYTHON@", sys.executable), encoding="utf-8",
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    # A one-second window, so the test does not sit through a minute.
    monkeypatch.setenv("PDT_429_BREAKER_SEC", "1.0")
    monkeypatch.setenv("PDT_429_BREAKER_STREAK", "3")
    # The fake child ignores stdin EOF, so the graceful path has to
    # escalate to SIGTERM. Two seconds is enough for that.
    monkeypatch.setattr(
        coding_tool.ClaudeCodingTool, "GRACEFUL_STOP_TIMEOUT", 2,
    )

    tool, _log = _make_tool()

    with pytest.raises(ApiError) as excinfo:
        tool._run_claude_interactive(
            "hello", scene="execution", total_timeout=20, idle_timeout=20,
        )

    assert excinfo.value.status == "429"

    # ``_current_process`` is left set on this path: the ``ApiError``
    # leaves the function through the read loop's ``finally`` and never
    # reaches the line that clears it. That is the handle on the child.
    proc = tool._current_process
    assert proc is not None
    assert proc.poll() is not None, (
        "the child was left running — the break left an orphan behind"
    )
