"""A dispatch that is waiting must say so (2026-11-01).

Why this file exists
--------------------
``_query_json_internal`` dispatches an LLM call and writes NOTHING while
it waits.  A plan whose directory mtime has not moved is therefore
ambiguous: the round may be alive and waiting on a provider, or the
worker may be wedged.  From the outside the two cannot be told apart,
and the difference decides whether a plan gets declared dead.

``_QueryProgressReporter`` breaks the tie from the inside: it writes
one ``coding_query_progress`` line per interval into the same
``execution.log`` the run already uses, so "the last thing that
happened" is answerable from the log instead of from a guess::

    coding_query_progress elapsed_sec=120 scene=verification prompt_chars=154321

The contracts pinned here:

1. One line per interval — not one line for the whole dispatch — and
   each line carries the elapsed time, the scene and the prompt size.
2. The reporter's thread is DEAD once the context manager exits.  A
   heartbeat that outlives its dispatch is a heartbeat that keeps
   writing into the next one's log.
3. The interval is bounded on both sides (1 s .. 120 s) and can be
   overridden through ``PDT_CODING_QUERY_PROGRESS_SECONDS``.
4. A real ``query_json`` dispatch writes the event while it is in
   flight, and stops writing once it is over.
5. A tool with no logger grows NO thread at all.
6. A dispatch that raises still stops its heartbeat.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import ClaudeCodingTool, _QueryProgressReporter  # noqa: E402
from execution_logger import LOG_EVENT_TYPES  # noqa: E402

#: The event name the reporter writes.  It is a first-class event in
#: the log's registry, not an ad-hoc string.
PROGRESS_EVENT = "coding_query_progress"


class _RecordingLogger:
    """A logger that keeps what it was told.

    Mirrors the ``ExecutionLogger`` surface the reporter uses
    (``info``/``warning``/``error`` with a ``data=`` payload) without
    touching the filesystem.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _record(self, event: str, message: str, **kwargs) -> None:
        with self._lock:
            self.events.append({"event": event, "message": message, **kwargs})

    def debug(self, event, message, **kwargs):
        self._record(event, message, **kwargs)

    def info(self, event, message, **kwargs):
        self._record(event, message, **kwargs)

    def warning(self, event, message, **kwargs):
        self._record(event, message, **kwargs)

    def error(self, event, message, **kwargs):
        self._record(event, message, **kwargs)

    def progress_events(self) -> list[dict]:
        """Only the heartbeat lines, in the order they were written."""
        with self._lock:
            return [e for e in self.events if e["event"] == PROGRESS_EVENT]


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Wait for ``predicate`` to hold, up to ``timeout`` seconds.

    The heartbeat runs on its own thread, so "it ticked" is a fact the
    test has to WAIT for rather than assume after a fixed sleep: a
    fixed sleep is a race against a loaded CI runner, and a race that
    fails intermittently is worse than no test at all.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


# ---------------------------------------------------------------------------
# 1. One line per interval
# ---------------------------------------------------------------------------


def test_reporter_emits_a_line_per_interval():
    """A dispatch that runs for several intervals leaves a line each time.

    One line for the whole dispatch would answer "did it start?" and
    nothing else; the point of the heartbeat is that the gap between
    two lines is bounded, so the reader can tell a live wait from a
    dead one.
    """
    logger = _RecordingLogger()
    reporter = _QueryProgressReporter(logger, scene="verification", interval=1)
    reporter.set_prompt_chars(154321)

    with reporter:
        assert _wait_until(lambda: len(logger.progress_events()) >= 2), (
            "the heartbeat must repeat once per interval — one line for "
            "the whole dispatch proves nothing about how long it waited"
        )

    lines = logger.progress_events()
    assert len(lines) >= 2

    data = lines[0]["data"]
    assert data["scene"] == "verification"
    assert data["prompt_chars"] == 154321
    assert data["elapsed_sec"] >= 1, "the elapsed time must be reported"

    # The elapsed time has to grow: a heartbeat that always reports the
    # same number is a counter that never ticked.
    elapsed = [line["data"]["elapsed_sec"] for line in lines]
    assert elapsed == sorted(elapsed)
    assert elapsed[-1] > elapsed[0]


def test_the_progress_event_is_a_registered_event_type():
    """The log's event registry has to know the name.

    A line whose event is not in ``LOG_EVENT_TYPES`` renders without a
    bracket prefix, which breaks the ``grep`` workflows that attribute
    a line to its source.
    """
    assert PROGRESS_EVENT in LOG_EVENT_TYPES
    assert LOG_EVENT_TYPES[PROGRESS_EVENT].startswith("[")


# ---------------------------------------------------------------------------
# 2. The thread must not outlive the dispatch
# ---------------------------------------------------------------------------


def test_reporter_stops_on_exit():
    """After the context manager exits, the thread is dead.

    A reporter that kept running would keep writing into whatever log
    came next, and the "elapsed" it reported would be about a dispatch
    that had already finished.
    """
    logger = _RecordingLogger()
    reporter = _QueryProgressReporter(logger, scene="verification", interval=1)

    with reporter:
        thread = reporter.thread
        assert thread is not None, "a reporter with a logger must run a thread"
        assert thread.is_alive()

    assert not thread.is_alive(), (
        "the heartbeat thread must be joined on exit — it may not "
        "outlive the dispatch it is reporting on"
    )


def test_the_reporter_stops_when_the_dispatch_raises():
    """An exception on the dispatch path still stops the heartbeat."""
    logger = _RecordingLogger()
    reporter = _QueryProgressReporter(logger, scene="verification", interval=1)

    with pytest.raises(RuntimeError):
        with reporter:
            thread = reporter.thread
            assert _wait_until(lambda: bool(logger.progress_events()))
            raise RuntimeError("dispatch blew up")

    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# 3. The interval is bounded
# ---------------------------------------------------------------------------


def test_interval_is_capped_at_120():
    """Nothing may make the heartbeat rarer than 120 s.

    A longer interval would let a wedged dispatch look healthy for
    longer than the cap allows.
    """
    assert _QueryProgressReporter(None, interval=1000).interval == 120
    assert _QueryProgressReporter(None, interval=121).interval == 120
    assert _QueryProgressReporter(None, interval=120).interval == 120


def test_interval_has_a_floor_of_one_second():
    """Nothing may make the heartbeat busier than 1 s."""
    assert _QueryProgressReporter(None, interval=0.01).interval == 1
    assert _QueryProgressReporter(None, interval=0).interval == 1
    assert _QueryProgressReporter(None, interval=-5).interval == 1


def test_the_default_interval_is_60_seconds():
    assert _QueryProgressReporter(None).interval == 60


def test_the_interval_can_be_set_through_the_environment(monkeypatch):
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "5")
    assert _QueryProgressReporter(None).interval == 5

    # The cap still applies to the environment: the knob moves the
    # interval, it does not lift the bound.
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "9999")
    assert _QueryProgressReporter(None).interval == 120

    # A value that is not a number falls back to the default rather
    # than to a crash inside the dispatch.
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "not-a-number")
    assert _QueryProgressReporter(None).interval == 60


# ---------------------------------------------------------------------------
# 4. A real dispatch writes the heartbeat
# ---------------------------------------------------------------------------


def test_query_json_writes_progress_while_in_flight(monkeypatch):
    """``query_json`` leaves a trace while the LLM call is outstanding.

    This is the whole point of the change: the dispatch that used to be
    invisible in the log for its entire duration now says, once per
    interval, that it is still there.
    """
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "1")
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)

    def _slow_dispatch(prompt, *args, **kwargs):
        # The dispatch stays outstanding until the heartbeat has shown
        # up — that IS the behaviour under test, and waiting for it
        # beats sleeping for a guessed number of seconds.
        _wait_until(lambda: bool(logger.progress_events()))
        return ('{"ok": true}', "sid", {})

    monkeypatch.setattr(tool, "_run_claude_interactive", _slow_dispatch)

    assert tool.query_json(prompt="x", system_instruction="y", scene="verification") == {
        "ok": True,
    }

    lines = logger.progress_events()
    assert lines, (
        "a dispatch that took seconds must have left progress lines — "
        "otherwise a slow round and a dead round look the same"
    )
    data = lines[0]["data"]
    assert data["scene"] == "verification"
    assert data["prompt_chars"] > 0, "the size of the prompt must be reported"
    assert data["elapsed_sec"] >= 1


def test_the_heartbeat_stops_when_the_dispatch_is_over(monkeypatch):
    """No line is written after the dispatch returned."""
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "1")
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)

    monkeypatch.setattr(
        tool, "_run_claude_interactive",
        lambda prompt, *args, **kwargs: ('{"ok": true}', "sid", {}),
    )

    tool.query_json(prompt="x", scene="verification")
    after = len(logger.progress_events())

    time.sleep(1.3)
    assert len(logger.progress_events()) == after, (
        "the heartbeat must not outlive the dispatch"
    )


# ---------------------------------------------------------------------------
# 5. No logger, no thread
# ---------------------------------------------------------------------------


def test_a_tool_without_a_logger_grows_no_thread():
    """``logger is None`` means zero side effects — no thread at all."""
    reporter = _QueryProgressReporter(None, scene="verification", interval=1)

    with reporter:
        time.sleep(0.5)

    assert reporter.thread is None, (
        "a tool with no logger must not grow a background thread"
    )
    assert not [
        t for t in threading.enumerate() if t.name == "pdt-query-progress"
    ], "no heartbeat thread may outlive the reporter"


# ---------------------------------------------------------------------------
# 6. The provider fallback is the SAME dispatch
# ---------------------------------------------------------------------------


def _heartbeat_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "pdt-query-progress"]


def test_a_provider_fallback_does_not_start_a_second_heartbeat(monkeypatch):
    """The fallback re-enters the dispatch — the heartbeat must not.

    A second reporter would restart ``elapsed_sec`` at zero, so the log
    would show the elapsed time going BACKWARDS in the middle of one
    dispatch: the operator would read a 300 s wait as a fresh 0 s one.
    """
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "1")
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)
    # The provider-fallback arm only retries when it knows which
    # provider failed; the real dispatch sets this, the double does not.
    tool.current_call_provider = "vendor-a"

    seen_threads = []

    def _dispatch(prompt, *args, **kwargs):
        seen_threads.append(len(_heartbeat_threads()))
        if len(seen_threads) == 1:
            raise TimeoutError("the provider went quiet")
        return ('{"ok": true}', "sid", {})

    monkeypatch.setattr(tool, "_run_claude_interactive", _dispatch)

    assert tool.query_json(prompt="x", scene="verification") == {"ok": True}

    assert len(seen_threads) == 2, "the fallback must have re-entered the dispatch"
    assert seen_threads[1] == 1, (
        "the retry must share the heartbeat with the attempt it replaces — "
        f"saw {seen_threads[1]} heartbeat threads during the retry"
    )


def test_the_elapsed_time_never_goes_backwards(monkeypatch):
    """Across a fallback, the reported elapsed time only grows."""
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "1")
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)
    tool.current_call_provider = "vendor-a"

    calls = []

    def _dispatch(prompt, *args, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise TimeoutError("the provider went quiet")
        # Hold the retry open long enough for the heartbeat to tick.
        _wait_until(lambda: len(logger.progress_events()) >= 1)
        return ('{"ok": true}', "sid", {})

    monkeypatch.setattr(tool, "_run_claude_interactive", _dispatch)

    tool.query_json(prompt="x", scene="verification")

    elapsed = [line["data"]["elapsed_sec"] for line in logger.progress_events()]
    assert elapsed == sorted(elapsed), (
        f"the elapsed time must not restart inside one dispatch: {elapsed}"
    )
