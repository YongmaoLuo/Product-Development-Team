"""One verification LLM query may not eat the afternoon (2026-11-01).

Why this file exists
--------------------
The heartbeat (2026-11-01, task 8) makes a waiting dispatch VISIBLE: it
writes one ``coding_query_progress`` line per interval into the same
``execution.log`` the run already uses, so "the round is alive and
waiting on a provider" and "the round is wedged" can be told apart from
the log instead of from a guess.

Visible is not bounded.  A dispatch could still sit on a provider for
hours, and the operator watching the log would see a healthy heartbeat
the whole time — which is exactly the situation the heartbeat was
supposed to make legible.

So the verification scene gets a CEILING as well.  A query that
outstays ``VERIFICATION_LLM_QUERY_CAP_SECONDS`` is killed — the whole
process group — and fails with ``HardTimeoutError``, the class the
verification layer routes into auto-split / auto-refine.

The contracts pinned here:

1. A dispatch that outstays the cap is killed and raises
   ``HardTimeoutError``; the log says ``coding_query_wall_timeout``.
2. A dispatch that finishes UNDER the cap is not touched at all: no
   kill, no event, the same answer as before.
3. The cap is 2700 s by default, and
   ``PDT_VERIFICATION_LLM_TIMEOUT_SECONDS`` moves it.  2700 sits above
   the 900 s silence threshold and below the 3600 s watchdog hard cap,
   so the cap that fires is always this one and never the outer
   watchdog.
4. The tool the verification loop creates carries the cap.
5. ``query_wall_cap_seconds is None`` changes nothing: no cap, no
   thread for a tool without a logger, no kill, no event — today's
   behaviour, exactly.
6. A process that has already exited is not killed again.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    EmptyResponseError,
    HardTimeoutError,
    _QueryProgressReporter,
)

# ``server`` first: ``verification_loop`` imports it and ``server``
# imports back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402

#: The event the wall-cap expiry writes into the execution log.
WALL_TIMEOUT_EVENT = "coding_query_wall_timeout"

#: The event the heartbeat writes (the one the cap rides along with).
PROGRESS_EVENT = "coding_query_progress"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """A logger that keeps what it was told.

    Mirrors the ``ExecutionLogger`` surface the reporter uses
    (``info``/``warning``/``error`` with a ``data=`` payload) without
    touching the filesystem.
    """

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
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

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        """Only the lines for ``event``, in the order they were written."""
        with self._lock:
            return [e for e in self.events if e["event"] == event]


class _FakeProcess:
    """The subprocess a dispatch is running, reduced to what the kill asks.

    ``poll()`` is the only thing the kill path reads: ``None`` means
    "still running", a number means "already gone".
    """

    def __init__(self, exited: bool = False) -> None:
        self.pid = 424242
        self._exited = exited

    def poll(self):
        return 0 if self._exited else None


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    """Wait for ``predicate`` to hold, up to ``timeout`` seconds.

    The cap is enforced on the reporter's own thread, so "the cap
    fired" is a fact the test has to WAIT for rather than assume after
    a fixed sleep: a fixed sleep is a race against a loaded CI runner,
    and a race that fails intermittently is worse than no test at all.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _capture_kills(monkeypatch: Any) -> List[Any]:
    """Record every process group the code under test kills."""
    killed: List[Any] = []
    monkeypatch.setattr(
        coding_tool, "kill_process_group",
        lambda proc, **kwargs: killed.append(proc),
    )
    return killed


# ---------------------------------------------------------------------------
# 1. Past the cap: killed, and failed
# ---------------------------------------------------------------------------


def test_dispatch_past_the_cap_is_killed_and_raises(monkeypatch):
    """A dispatch that outstays the cap is killed and fails.

    Three things have to be true at once, and each of them is a
    different failure mode:

    * the process group is killed — otherwise the CLI keeps running and
      the cap only *reported* the problem;
    * the caller gets ``HardTimeoutError`` — the class the verification
      layer routes into auto-split, not a generic failure that the
      provider-fallback path would retry on another provider;
    * the log says why — ``coding_query_wall_timeout``.
    """
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)
    tool.query_wall_cap_seconds = 1.0

    killed = _capture_kills(monkeypatch)

    def _dispatch(prompt, *args, **kwargs):
        # Stand in for the spawned CLI: the process exists for as long
        # as the dispatch does, and the dispatch only ends when the cap
        # has fired and killed it.
        tool._current_process = _FakeProcess()
        if not _wait_until(lambda: bool(killed)):
            raise AssertionError(
                "the wall cap never killed the process group — the "
                "dispatch was left running"
            )
        # What a killed CLI looks like to the caller: no assistant text.
        raise EmptyResponseError("the CLI was killed by the wall cap")

    monkeypatch.setattr(tool, "_run_claude_interactive", _dispatch)

    with pytest.raises(HardTimeoutError):
        tool.query_json(prompt="x", scene="verification")

    assert killed, "the process group was not killed when the cap fired"

    events = logger.events_named(WALL_TIMEOUT_EVENT)
    assert events, (
        "the wall timeout was not logged — the operator has to be able "
        "to see WHY the dispatch died"
    )
    data = events[0]["data"]
    assert data["cap_sec"] == 1.0
    assert data["elapsed_sec"] >= 0.9
    assert data["scene"] == "verification"
    assert "(1s)" in events[0]["message"], (
        "the message must name the cap that was blown — a cap rounded "
        f"away would tell the operator nothing: {events[0]['message']!r}"
    )


# ---------------------------------------------------------------------------
# 1b. The cap in the message is the cap that was configured
# ---------------------------------------------------------------------------


def test_the_logged_cap_text_is_the_configured_cap():
    """``2700`` stays ``2700``; a fraction is not rounded to a lie.

    The operator reads the message, not the structured field, when they
    ask "how long was this dispatch allowed to run?".  A fractional
    cap rounded to a whole second would print ``0s`` — a cap of zero
    seconds, which nobody configured.
    """
    assert coding_tool._format_cap_seconds(2700.0) == "2700"
    assert coding_tool._format_cap_seconds(2700) == "2700"
    assert coding_tool._format_cap_seconds(100.0) == "100"
    assert coding_tool._format_cap_seconds(1.0) == "1"
    assert coding_tool._format_cap_seconds(0.4) == "0.4"
    assert coding_tool._format_cap_seconds(2.5) == "2.5"


# ---------------------------------------------------------------------------
# 2. Under the cap: untouched
# ---------------------------------------------------------------------------


def test_dispatch_under_the_cap_is_untouched(monkeypatch):
    """A dispatch that finishes under the cap is not touched at all.

    The cap is a ceiling, not a budget: a call that answers in time
    must not notice that a ceiling exists.  The heartbeat is made to
    tick during the dispatch on purpose — a dispatch whose reporter
    never woke up cannot prove that the cap did not fire.
    """
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "1")
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)
    tool.query_wall_cap_seconds = 3600.0

    killed = _capture_kills(monkeypatch)

    def _dispatch(prompt, *args, **kwargs):
        tool._current_process = _FakeProcess()
        time.sleep(1.5)
        tool._current_process = None
        return ('{"ok": true}', "sid", {})

    monkeypatch.setattr(tool, "_run_claude_interactive", _dispatch)

    assert tool.query_json(prompt="x", scene="verification") == {"ok": True}

    assert not killed, "a dispatch that finished under the cap was killed"
    assert not logger.events_named(WALL_TIMEOUT_EVENT), (
        "the wall timeout was reported for a dispatch that answered in time"
    )
    assert logger.events_named(PROGRESS_EVENT), (
        "the heartbeat never ticked — this test cannot claim the cap "
        "was not what ended the dispatch"
    )


# ---------------------------------------------------------------------------
# 3. The default, and the override
# ---------------------------------------------------------------------------


def test_cap_defaults_to_2700_and_env_overrides(monkeypatch):
    """2700 s by default; ``PDT_VERIFICATION_LLM_TIMEOUT_SECONDS`` moves it.

    The default is not an arbitrary number: it has to sit ABOVE the
    900 s silence threshold (a dispatch that keeps printing is not
    silent, and killing it would kill a healthy call) and BELOW the
    3600 s watchdog hard cap (otherwise the outer watchdog would fire
    first and this cap would be decoration).
    """
    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)

    assert vl.VERIFICATION_LLM_QUERY_CAP_SECONDS == 2700
    assert vl._verification_query_cap_seconds() == 2700

    assert vl.VERIFICATION_LLM_QUERY_CAP_SECONDS > ClaudeCodingTool.DEFAULT_TOTAL_TIMEOUT, (
        "the cap must sit above the silence threshold — a dispatch that "
        "keeps printing must not be killed"
    )
    assert vl.VERIFICATION_LLM_QUERY_CAP_SECONDS < ClaudeCodingTool.HARD_WALL_CLOCK_TIMEOUT_SECONDS, (
        "the cap must sit below the watchdog's hard ceiling — otherwise "
        "the watchdog fires first and this cap never gets to"
    )

    monkeypatch.setenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, "600")
    assert vl._verification_query_cap_seconds() == 600

    # A value that is not a number falls back to the default rather than
    # to a dispatch that dies on the first tick.
    monkeypatch.setenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, "not-a-number")
    assert vl._verification_query_cap_seconds() == 2700

    # A cap that is not positive would kill every dispatch instantly;
    # it is treated as "no opinion" instead.
    monkeypatch.setenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, "0")
    assert vl._verification_query_cap_seconds() == 2700


# ---------------------------------------------------------------------------
# 4. The verification tool carries the cap
# ---------------------------------------------------------------------------


class _FakePlanState:
    """The plan's state machine, reduced to what the loop calls."""

    def __init__(self, plan_dir: Path) -> None:
        self.plan_dir = plan_dir

    def set_verification_max_rounds(self, max_rounds: int) -> None:
        pass

    def reload(self) -> None:
        pass

    def begin_verification(self, round_n: Optional[int] = None) -> None:
        pass

    def transition_to(self, phase: str) -> None:
        pass

    def get_current_phase(self) -> str:
        return "verification"


class _FakeCodingTool:
    """The orchestrator's coding tool, reduced to what the loop touches."""

    def __init__(self) -> None:
        self.query_wall_cap_seconds: Optional[float] = None


def _wire_inner(monkeypatch: Any, tmp_path: Path, tool: Any) -> None:
    """Everything the inner loop touches that is not the loop itself.

    The loop is production code and is not touched here.  What is
    replaced is everything that would reach the outside world: the
    coding tool (a real one spawns a subprocess), the plan state (a real
    one writes into the operator's plan tree) and the state database.
    """
    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no verification repository in this test")

    class _FakeOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.coding_tool = kwargs.get("coding_tool")

        def start_verification_cycle(self, *args: Any, **kwargs: Any) -> Dict:
            return {"verification_results": []}

        def check_cycle_conditions(self, *args: Any, **kwargs: Any) -> Dict:
            return {
                "status": "passed",
                "repair_tasks": [],
                "stop_reason": None,
            }

    monkeypatch.setattr(srv, "_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(srv, "PlanState", _FakePlanState)
    monkeypatch.setattr(srv, "_open_verification_state", _refuse)
    monkeypatch.setattr(
        srv, "_record_verification_terminal", lambda *a, **k: None,
    )
    monkeypatch.setattr(srv, "create_coding_tool", lambda *a, **k: tool)
    monkeypatch.setattr(srv, "VerificationOrchestrator", _FakeOrchestrator)


def test_verification_tool_carries_the_cap(tmp_path: Path, monkeypatch: Any):
    """The tool the verification loop creates is capped.

    A cap nobody sets is a cap nobody has: the tool has to carry it,
    or the loop's dispatches are unbounded exactly as they are today.
    """
    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)

    plan_id = "plan-wall-cap"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)

    token = "verification_loop-wall-cap-4242"
    srv._verification_state.pop(plan_id, None)

    tool = _FakeCodingTool()
    _wire_inner(monkeypatch, tmp_path, tool)

    vl._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, round_token=token,
    )

    assert tool.query_wall_cap_seconds == 2700, (
        f"the verification tool carries {tool.query_wall_cap_seconds!r} "
        f"instead of the cap — a tool without the cap is unbounded"
    )

    # The same wiring, with the environment override in place.  A
    # SECOND plan, because a plan whose round is already running is
    # refused a second round — and the refusal would leave the second
    # tool uncreated.
    monkeypatch.setenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, "600")
    plan_id2 = "plan-wall-cap-override"
    plan_dir2 = tmp_path / "plans" / plan_id2
    plan_dir2.mkdir(parents=True, exist_ok=True)
    srv._verification_state.pop(plan_id2, None)

    tool2 = _FakeCodingTool()
    _wire_inner(monkeypatch, tmp_path, tool2)

    vl._run_auto_verification_loop_inner(
        plan_id2, plan_dir2, project_dir, round_token=token,
    )

    assert tool2.query_wall_cap_seconds == 600, (
        f"the verification tool carries {tool2.query_wall_cap_seconds!r} "
        f"instead of the overridden cap"
    )


# ---------------------------------------------------------------------------
# 5. No cap: today's behaviour
# ---------------------------------------------------------------------------


def test_none_cap_keeps_todays_behaviour(monkeypatch):
    """``None`` means "no ceiling", and nothing else changes.

    The default has to be ``None`` — a tool that is not part of the
    verification scene must not grow a kill path it never had, and a
    tool without a logger must not grow a thread.
    """
    monkeypatch.setenv("PDT_CODING_QUERY_PROGRESS_SECONDS", "1")
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)

    assert tool.query_wall_cap_seconds is None, (
        "a tool that nobody capped must not carry a cap"
    )

    killed = _capture_kills(monkeypatch)

    def _dispatch(prompt, *args, **kwargs):
        tool._current_process = _FakeProcess()
        time.sleep(1.5)
        tool._current_process = None
        return ('{"ok": true}', "sid", {})

    monkeypatch.setattr(tool, "_run_claude_interactive", _dispatch)

    assert tool.query_json(prompt="x", scene="verification") == {"ok": True}

    assert not killed, "a dispatch without a cap was killed"
    assert not logger.events_named(WALL_TIMEOUT_EVENT), (
        "a wall timeout was reported for a tool that carries no cap"
    )

    # And a tool with no logger grows no thread at all — as before.
    quiet = ClaudeCodingTool()
    reporter = _QueryProgressReporter(
        quiet.logger, wall_cap_seconds=quiet.query_wall_cap_seconds,
    )
    with reporter:
        time.sleep(0.2)
    assert reporter.thread is None, (
        "a tool without a logger must not grow a background thread"
    )


# ---------------------------------------------------------------------------
# 6. A process that already exited
# ---------------------------------------------------------------------------


def test_a_process_that_already_exited_is_not_killed_again(monkeypatch):
    """Killing a corpse is not a kill.

    The CLI can exit on its own a moment before the cap fires — and
    then there is nothing left to kill.  The kill path has to be able to
    tell that apart from "a process is still running", because the
    answer is different: one is a no-op, the other is the whole point.
    """
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)
    tool.query_wall_cap_seconds = 1.0

    killed = _capture_kills(monkeypatch)

    def _dispatch(prompt, *args, **kwargs):
        # The CLI exited on its own a moment ago.
        tool._current_process = _FakeProcess(exited=True)
        if not _wait_until(
            lambda: bool(logger.events_named(WALL_TIMEOUT_EVENT)),
        ):
            raise AssertionError("the wall cap never fired")
        raise EmptyResponseError("the CLI exited without assistant text")

    monkeypatch.setattr(tool, "_run_claude_interactive", _dispatch)

    with pytest.raises(HardTimeoutError):
        tool.query_json(prompt="x", scene="verification")

    assert not killed, "a process that had already exited was killed again"
