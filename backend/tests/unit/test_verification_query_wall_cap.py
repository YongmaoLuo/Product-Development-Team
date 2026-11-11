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

import asyncio
import os
import signal
import subprocess
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

    The constant and the resolver live in ``coding_tool`` now — the cap
    is the TOOL's own default, so the tool is where the number belongs.
    ``verification_loop`` re-exports both names, so the import that
    used to work still works.
    """
    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)

    assert coding_tool.VERIFICATION_LLM_QUERY_CAP_SECONDS == 2700
    assert coding_tool._verification_query_cap_seconds() == 2700

    # The re-export: the old import path is still there.
    assert vl.VERIFICATION_LLM_QUERY_CAP_SECONDS == 2700
    assert vl.VERIFICATION_LLM_QUERY_CAP_ENV == "PDT_VERIFICATION_LLM_TIMEOUT_SECONDS"

    assert coding_tool.VERIFICATION_LLM_QUERY_CAP_SECONDS > ClaudeCodingTool.DEFAULT_TOTAL_TIMEOUT, (
        "the cap must sit above the silence threshold — a dispatch that "
        "keeps printing must not be killed"
    )
    assert coding_tool.VERIFICATION_LLM_QUERY_CAP_SECONDS < ClaudeCodingTool.HARD_WALL_CLOCK_TIMEOUT_SECONDS, (
        "the cap must sit below the watchdog's hard ceiling — otherwise "
        "the watchdog fires first and this cap never gets to"
    )

    monkeypatch.setenv(coding_tool.VERIFICATION_LLM_QUERY_CAP_ENV, "600")
    assert coding_tool._verification_query_cap_seconds() == 600

    # A value that is not a number falls back to the default rather than
    # to a dispatch that dies on the first tick.
    monkeypatch.setenv(coding_tool.VERIFICATION_LLM_QUERY_CAP_ENV, "not-a-number")
    assert coding_tool._verification_query_cap_seconds() == 2700

    # A cap that is not positive would kill every dispatch instantly;
    # it is treated as "no opinion" instead.
    monkeypatch.setenv(coding_tool.VERIFICATION_LLM_QUERY_CAP_ENV, "0")
    assert coding_tool._verification_query_cap_seconds() == 2700


# ---------------------------------------------------------------------------
# 4. The verification tool carries the cap
# ---------------------------------------------------------------------------


class _FakePlanState:
    """The plan's state machine, reduced to what the loop calls."""

    def __init__(self, plan_dir: Path) -> None:
        self.plan_dir = plan_dir

    def ensure_row(self) -> None:
        # 2026-11-11: the loop asks for the routing row to exist before
        # its CAS (constructing a PlanState no longer creates it).
        pass

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


def _wire_inner(
    monkeypatch: Any, tmp_path: Path,
    created: Optional[Dict[str, Any]] = None,
    built: Optional[List[Any]] = None,
) -> None:
    """Everything the inner loop touches that is not the loop itself.

    The loop is production code and is not touched here.  What is
    replaced is everything that would reach the outside world: the
    coding tool (a real one spawns a subprocess), the plan state (a real
    one writes into the operator's plan tree) and the state database.

    ``created`` collects the keyword arguments the loop passed to
    ``create_coding_tool`` — the scene it asked for is what decides
    whether the real tool carries the cap.  The tool the fake hands
    back is a REAL ``ClaudeCodingTool`` built for that scene: a
    stand-in with a hardcoded attribute would answer a different
    question than the one this file asks.
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

    def _fake_create_coding_tool(*args: Any, **kwargs: Any) -> Any:
        if created is not None:
            created.update(kwargs)
        # A REAL tool, built the way the factory builds it: the scene
        # the loop asked for is what decides the cap.  A stand-in with
        # a hardcoded attribute would answer a different question.
        tool = ClaudeCodingTool(scene=kwargs.get("scene"))
        if built is not None:
            built.append(tool)
        return tool

    monkeypatch.setattr(srv, "_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(srv, "PlanState", _FakePlanState)
    monkeypatch.setattr(srv, "_open_verification_state", _refuse)
    monkeypatch.setattr(
        srv, "_record_verification_terminal", lambda *a, **k: None,
    )
    monkeypatch.setattr(srv, "create_coding_tool", _fake_create_coding_tool)
    monkeypatch.setattr(srv, "VerificationOrchestrator", _FakeOrchestrator)


def test_the_loop_asks_for_a_verification_scene_tool(
    tmp_path: Path, monkeypatch: Any,
):
    """The loop builds its tool with the ``verification`` scene.

    A cap nobody sets is a cap nobody has.  The cap is now the TOOL's
    own construction-time default for the verification scene, so the
    one thing the loop still has to get right is the scene it asks
    for — and it has to ask for it on EVERY path that builds a tool
    (this test covers the outer round; the re-entered round is the
    second call site).
    """
    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)

    plan_id = "plan-wall-cap"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)

    token = "verification_loop-wall-cap-4242"
    srv._verification_state.pop(plan_id, None)

    created: Dict[str, Any] = {}
    built: List[Any] = []
    _wire_inner(monkeypatch, tmp_path, created=created, built=built)

    vl._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, round_token=token,
    )

    assert created.get("scene") == "verification", (
        f"the loop asked for scene={created.get('scene')!r} — a tool "
        f"built for another scene carries no cap"
    )
    # And the tool the loop got is capped — because it was built for
    # the verification scene.
    assert built, "the loop never built a coding tool"
    assert built[0].query_wall_cap_seconds == 2700.0, (
        f"the verification tool carries {built[0].query_wall_cap_seconds!r} "
        f"instead of the cap — a tool without the cap is unbounded"
    )


# ---------------------------------------------------------------------------
# 4b. The cap is the tool's own CONSTRUCTION-TIME default (2026-11-10)
# ---------------------------------------------------------------------------


def test_the_cap_is_a_construction_time_default(monkeypatch: Any):
    """Every verification-scene tool carries the cap from its constructor.

    The cap used to be applied by a helper in the verification loop,
    at two call sites.  Every OTHER way of building a verification
    tool — the scoped clone the sub-agent builds per VP, for one —
    silently lost it, and a tool with no cap never expires.

    So the cap is a default of the CONSTRUCTOR now: the scene decides,
    and no call site can forget.
    """
    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)
    monkeypatch.delenv("PDT_LLM_MODE", raising=False)

    direct = ClaudeCodingTool(scene="verification")
    assert direct.query_wall_cap_seconds == 2700.0, (
        f"a directly built verification tool carries "
        f"{direct.query_wall_cap_seconds!r} instead of the cap"
    )

    from_factory = coding_tool.create_coding_tool(scene="verification")
    assert from_factory.query_wall_cap_seconds == 2700.0, (
        f"a factory-built verification tool carries "
        f"{from_factory.query_wall_cap_seconds!r} instead of the cap"
    )


def test_a_tool_outside_the_verification_scene_carries_no_cap(
    monkeypatch: Any,
):
    """Only the verification scene is capped (the negative control).

    The cap comes with a kill path: a dispatch that outstays it has its
    process group killed.  A tool that is not part of verification must
    not grow that path — so every other scene keeps the class default
    ``None``, which means "no ceiling".
    """
    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)
    monkeypatch.delenv("PDT_LLM_MODE", raising=False)

    for scene in ("summarizer", None, "", "task_execution"):
        tool = ClaudeCodingTool(scene=scene)
        assert tool.query_wall_cap_seconds is None, (
            f"a tool built for scene={scene!r} carries a cap "
            f"({tool.query_wall_cap_seconds!r}) — the cap belongs to "
            f"the verification scene only"
        )

    assert coding_tool.create_coding_tool(
        scene="summarizer",
    ).query_wall_cap_seconds is None


def test_the_environment_override_moves_the_cap_at_construction(
    monkeypatch: Any,
):
    """The override reaches a tool through its constructor, not a setter.

    The override is read when the tool is built, so a tool built under
    an override carries the override — and a value that is not a number
    falls back to the default rather than to a dispatch that dies on
    its first tick.
    """
    monkeypatch.delenv("PDT_LLM_MODE", raising=False)

    monkeypatch.setenv(coding_tool.VERIFICATION_LLM_QUERY_CAP_ENV, "60")
    assert ClaudeCodingTool(scene="verification").query_wall_cap_seconds == 60.0
    assert coding_tool.create_coding_tool(
        scene="verification",
    ).query_wall_cap_seconds == 60.0

    # A value that is not a number falls back to the default.
    monkeypatch.setenv(coding_tool.VERIFICATION_LLM_QUERY_CAP_ENV, "abc")
    assert ClaudeCodingTool(scene="verification").query_wall_cap_seconds == 2700.0
    assert coding_tool.create_coding_tool(
        scene="verification",
    ).query_wall_cap_seconds == 2700.0


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


# ---------------------------------------------------------------------------
# 7. Ending the in-flight dispatch is ONE action (2026-11-10)
# ---------------------------------------------------------------------------


def test_kill_in_flight_dispatch_kills_the_process_group():
    """The action that ends a dispatch really ends it.

    The wall cap's kill and the outer cap's kill are the SAME action
    now — one method on the tool — so this test is about the action
    itself: given a dispatch that is genuinely in flight, the method
    has to take the whole process group down and say that it did.
    """
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)

    proc = subprocess.Popen(
        ["sleep", "300"], start_new_session=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
        tool._current_process = proc

        assert tool.kill_in_flight_dispatch("test:vp_outer_cap") is True, (
            "the action reported that it killed nothing while a process "
            "group was in flight"
        )

        # The process is dead (reaped, so it is no longer a member of
        # the group), and the group is gone with it.
        proc.wait(timeout=10)
        assert proc.returncode is not None, (
            "the process survived the kill — the CLI keeps running"
        )
        assert not _group_alive(pgid), (
            "the process group survived the kill — the CLI keeps running"
        )
        assert tool._current_process is None, (
            "the tool still points at a process it just killed"
        )

        events = logger.events_named("coding_query_dispatch_killed")
        assert events, "the kill was not logged"
        assert events[0]["data"]["reason"] == "test:vp_outer_cap"
        assert events[0]["data"]["killed"] is True
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_kill_in_flight_dispatch_without_a_dispatch_returns_false():
    """Nothing in flight: the action says so and does not raise.

    Killing a corpse is not a kill.  The caller has to be able to tell
    "the dispatch was ended" from "there was nothing to end", and a
    missing process must not turn into an exception.
    """
    logger = _RecordingLogger()
    tool = ClaudeCodingTool(logger=logger)

    assert tool._current_process is None
    assert tool.kill_in_flight_dispatch("test:nothing_in_flight") is False

    # And a process that has already exited is not killed again.
    tool._current_process = _FakeProcess(exited=True)
    assert tool.kill_in_flight_dispatch("test:already_gone") is False
    assert tool._current_process is None


def _group_alive(pgid: int) -> bool:
    """True when a process group still has members."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# 8. The outer cap ends the in-flight dispatch (2026-11-10)
# ---------------------------------------------------------------------------


def test_the_outer_cap_ends_the_in_flight_dispatch(
    tmp_path: Path, monkeypatch: Any,
):
    """When the outer cap fires, the dispatch it was watching is ended.

    The outer cap used to log, hand the VP to the summarizer and let
    the CLI keep running — and the ``finally`` that unregisters the
    watchdog handle took away the only way to reach the process.  So
    the in-flight dispatch is now ended BEFORE the exception travels
    on, by the same action the wall cap uses.

    The scoped tool the sub-agent builds per VP is the tool that
    carries the cap (it is built for the ``verification`` scene), and it
    is the tool the outer cap has to end.
    """
    import verification_subagent as vsub

    agent = vsub.VerificationSubAgent(method="code_review")
    # The outer cap in seconds — small enough to fire inside the test.
    agent.HARD_WALL_CLOCK_CAP_SECONDS = 0.2

    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)

    release = threading.Event()
    kills: List[Any] = []

    def _fake_query_json(self, *args: Any, **kwargs: Any) -> Dict:
        # A dispatch that never answers: the outer cap is what ends it.
        release.wait(30)
        return {}

    def _fake_kill(self, reason: str = "") -> bool:
        kills.append((self, reason))
        return True

    monkeypatch.setattr(
        ClaudeCodingTool, "query_json", _fake_query_json, raising=True,
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "kill_in_flight_dispatch", _fake_kill, raising=True,
    )

    parent_tool = ClaudeCodingTool(scene="verification")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    vp_node = {
        "id": "VP-777",
        "title": "a point that never answers",
        "verification_method": "code_review",
    }

    async def _run() -> Any:
        return await agent._execute_attempt(
            vp_node=vp_node,
            coding_tool=parent_tool,
            settings_path=str(settings_path),
            log_path=tmp_path / "attempt.log",
            attempt=0,
            project_dir=tmp_path,
        )

    try:
        with pytest.raises(vsub.StuckAgentError) as excinfo:
            asyncio.run(_run())
    finally:
        release.set()

    assert kills, (
        "the outer cap fired and nothing ended the in-flight dispatch — "
        "the CLI keeps running after the VP was handed to the summarizer"
    )
    scoped_tool, reason = kills[0]
    assert reason == "vp_outer_cap:VP-777", (
        f"the kill did not say why it happened: {reason!r}"
    )

    # The tool that carries the cap is the scoped one the sub-agent
    # built — and it is capped, because it was built for the
    # verification scene.
    assert scoped_tool is not parent_tool, (
        "the scoped tool was not the one the sub-agent built — the test "
        "did not exercise the scoped path"
    )
    assert scoped_tool.query_wall_cap_seconds == 2700.0, (
        f"the scoped tool carries {scoped_tool.query_wall_cap_seconds!r} "
        f"instead of the cap — a tool without the cap never expires"
    )

    # And the answer travels with the exception, so the log that reports
    # the routing can say whether anything was actually killed.
    assert getattr(excinfo.value, "killed", False) is True


def test_the_scoped_tool_the_sub_agent_builds_carries_the_cap(
    tmp_path: Path, monkeypatch: Any,
):
    """The clone the sub-agent builds per VP is capped — this is the bug.

    The cap used to be applied by a helper in the verification loop, at
    two call sites.  The verification sub-agent builds its OWN clone of
    the coding tool for every VP — and that clone was never capped, so
    the tool that actually ran the query had no ceiling: the heartbeat
    kept ticking and the cap never fired.

    The clone is built for the ``verification`` scene, so it inherits
    the cap from the constructor now.
    """
    import verification_subagent as vsub

    agent = vsub.VerificationSubAgent(method="code_review")
    agent.HARD_WALL_CLOCK_CAP_SECONDS = 0.2

    monkeypatch.delenv(vl.VERIFICATION_LLM_QUERY_CAP_ENV, raising=False)

    built: List[Any] = []

    class _RecordingTool(ClaudeCodingTool):
        """A real tool that remembers that it was built."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    # The sub-agent imports ``ClaudeCodingTool`` from the module at
    # call time, so patching the module attribute is what the
    # production path will pick up.
    monkeypatch.setattr(coding_tool, "ClaudeCodingTool", _RecordingTool)

    release = threading.Event()

    def _fake_query_json(self, *args: Any, **kwargs: Any) -> Dict:
        release.wait(30)
        return {}

    monkeypatch.setattr(
        ClaudeCodingTool, "query_json", _fake_query_json, raising=True,
    )

    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    vp_node = {
        "id": "VP-778",
        "title": "a point that never answers",
        "verification_method": "code_review",
    }

    async def _run() -> Any:
        return await agent._execute_attempt(
            vp_node=vp_node,
            coding_tool=_RecordingTool(scene="verification"),
            settings_path=str(settings_path),
            log_path=tmp_path / "attempt.log",
            attempt=0,
            project_dir=tmp_path,
        )

    try:
        with pytest.raises(vsub.StuckAgentError):
            asyncio.run(_run())
    finally:
        release.set()

    assert len(built) == 2, (
        f"expected the parent tool and the scoped clone, got {len(built)}"
    )
    scoped_tool = built[-1]
    assert scoped_tool is not built[0], (
        "the sub-agent did not build its own clone — the scoped path "
        "was not exercised"
    )
    assert scoped_tool.query_wall_cap_seconds == 2700.0, (
        f"the scoped tool carries {scoped_tool.query_wall_cap_seconds!r} "
        f"instead of the cap — a tool without the cap never expires"
    )
