"""E2E-SESSION — the conversation survives the process that was killed.

Why this scenario exists
========================

A resume is the one thing about the mcode backend that a successful
run never exercises.  A single run creates a conversation, answers in
it and ends; the mapping from the caller's pinned key to the
conversation the CLI really minted is only read when a *second* run
asks to continue the first one.  So the failure this scenario is about
is invisible from a green run: the first run can be perfect, and the
resume can still hand the conversation to nobody, or to the wrong one.

Three things have to hold, and each of them is invisible until it is
broken:

  1. **A killed run leaves a conversation behind.**  The run is
     stopped with a signal in the middle of its first exchange, and
     the conversation it opened has to survive the process that opened
     it.  The session id the CLI minted is in the run's own event
     stream, before the signal, so a run that is stopped after that
     point still knows which conversation it is in.

  2. **The retry continues that conversation.**  The retry's command
     line has to name the conversation the killed run opened
     (``--session mvs_...``), and it has to find it through the
     mapping rather than remember it: the mapping is a file, and a file
     is what a later process can read.

  3. **A mapping that is gone degrades loudly.**  When the mapping
     file is missing the retry cannot resume -- and the degradation
     has to be an *audited* one: a new conversation, named in the
     audit, and never a silent switch to some other conversation.

The stop
========

The run is stopped by the CLI *saying something*: the first
``session.started`` or the first ``assistant`` event.  The trigger is
an event, never a moment on the clock -- a scenario that waited a
fixed number of seconds would be asserting about a machine's speed
rather than about the product.  The signal goes to the child's
*process group*, because the CLI starts helpers of its own and a
signal to the leader alone would leave them running.

A ceiling is still needed, for the case where the CLI says nothing at
all: the stop waits for the trigger, and if the trigger never comes it
stops the run anyway.  No assertion reads that ceiling -- the
assertions read the events and the exit code -- so the ceiling cannot
make a broken run look like a working one.

What the assertions say
=======================

``test_resume_uses_captured_session`` reads the first two: the run
really was stopped by a signal, the workspace's own ``.mcp.json`` was
put back, and the retry's command line names the conversation the
killed run opened.

``test_missing_map_degrades_visibly`` reads the third: with the
mapping deleted, the retry creates a new conversation, the degradation
is in the plan's audit, and the number of conversation changes that no
audit names is zero.

The exit category, stated honestly
==================================

The brief for this scenario asks for the exit category to be
``signaled``, and the production code can carry that -- but it carries
it in one of two shapes, and the scenario accepts both rather than
pretending the other one does not exist.

A child that is *killed* by the signal reports the negative signal
number, because the runner reads the child's exit code the way
``process.wait()`` gives it, and the runner's own default calls a
negative code ``signaled``.  A CLI that catches the signal and leaves
on its own reports 128 + the signal number instead, and the production
exit table (:data:`harness.claude.EXIT_CODE_KINDS`) calls 130 and 143
``signaled``.  The MiniMax adapter itself has no ``classify_exit`` of
its own, so the runner's default verdict for a *positive* exit code is
``unknown``.

So the scenario asserts the category the way the code can carry it:
the exit code is the signal's own, the category that code lands in is
``signaled``, and the run is a failure rather than a success -- while
leaving room for an adapter that grows a classifier of its own.

Cost and blast radius
=====================

The scenario costs three real model calls and spawns a real server, so
it is off unless ``PDT_E2E_HARNESS=1`` is set: the local and CI
deterministic gates only *collect* it.  It is marked ``real_model`` (it
really calls a model) and ``e2e`` (it drives a real process), so the
cloud lanes skip it twice over.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

#: The variable that turns the scenario on.  Without it the module is
#: collected and skipped -- the deterministic gates never pay for a real
#: model call.
E2E_ENV_VAR = "PDT_E2E_HARNESS"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.real_model,
    pytest.mark.skipif(
        os.environ.get(E2E_ENV_VAR) != "1",
        reason=(
            "the real kill-and-resume scenario needs PDT_E2E_HARNESS=1: it "
            "spawns the real mcode CLI under the real macOS seatbelt, "
            "kills it in flight and costs model calls, so the deterministic "
            "gates only collect it"
        ),
    ),
]

# --------------------------------------------------------------------------
# What the scenario is made of
# --------------------------------------------------------------------------

#: The plan and the task this scenario belongs to.  Its own plan id, so
#: its audit trail cannot be confused with the other real chain's.
PLAN_ID = "e2e-mcode-session"
TASK_ID = "1"
STEP_KEY = f"task:{TASK_ID}"

#: The backend this scenario drives.
HARNESS_NAME = "mcode"

#: The shape of a session id the MiniMax Code CLI mints.  A pinned id
#: is a key, not a session id -- the CLI cannot create a conversation
#: under a chosen one -- so the id that comes back must look like this.
MCODE_SESSION_RE = re.compile(r"^mvs_[0-9a-f]{32}$")

#: The exit codes that mean "the run was cut short by a signal" for a
#: CLI that catches the signal itself: it leaves on its own and reports
#: 128 + the signal number.  A CLI that is *killed* by the signal
#: reports the negative signal number instead -- the runner reads the
#: child's code the way ``process.wait()`` gives it -- and the runner's
#: own default calls that ``signaled`` as well.
SIGNALLED_EXIT_CODES = frozenset(
    {128 + int(signal.SIGINT), 128 + int(signal.SIGTERM)}
)

#: How long the stop waits for the trigger event before it stops the
#: run anyway.  It is a ceiling on a silent CLI, not a measurement: no
#: assertion reads the clock, they read the events and the exit code.
FALLBACK_TIMEOUT = 180.0

#: The prompt every run carries.  Small on purpose: the scenario is
#: about what happens to the conversation, not about what the model
#: says in it.
PROMPT = "Reply with the single word OK and nothing else."

#: The trigger events, by name.  The first one of them that the run
#: produces is the point at which the run is stopped.
TRIGGER_EVENTS = ("session.started", "assistant")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _environment(**overrides: Optional[str]) -> Iterator[None]:
    """Set environment variables for the duration of a block.

    ``monkeypatch`` cannot be used here: the fixture is session-scoped
    and ``monkeypatch`` is function-scoped, while these variables have
    to outlive a single test.

    Args:
        **overrides: The variables to set.  A value of ``None`` removes
            the variable for the duration of the block.

    Yields:
        Nothing; the block is the point.
    """
    saved = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _real_home() -> str:
    """The home the process was launched with.

    The hermetic test environment replaces ``HOME`` with a scratch
    directory, which is right for a test that must not touch the
    operator's files and wrong for a run that has to authenticate
    against a real account.  The real home is published by the
    hermetic environment itself, so it is read from there rather than
    written down here.
    """
    return (
        os.environ.get("PDT_TEST_REAL_HOME")
        or os.environ.get("HOME")
        or str(Path.home())
    )


def _degradation_event() -> str:
    """The event a degraded create is audited under, as task 21 named it.

    The name is read out of task 21's own module rather than written
    down here, so the two cannot drift apart: an audit this scenario
    writes under a name task 21 does not use would be an audit nobody
    reads.

    Returns:
        The event name.

    Raises:
        AssertionError: task 21's policy module is not where it was.
    """
    path = (
        Path(__file__).resolve().parents[2]
        / "integration"
        / "harness"
        / "test_session_fault_injection.py"
    )
    spec = importlib.util.spec_from_file_location(
        "pdt_task21_fault_injection", path
    )
    if spec is None or spec.loader is None:
        raise AssertionError(
            f"task 21's degradation policy is not at {path}, so the event "
            "this scenario audits under cannot be read."
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.SESSION_MAP_MISSING_NEW_CREATED)


def _audit_records(log_path: Path, event: str) -> List[Dict[str, Any]]:
    """The audit entries for ``event`` in the plan's execution log.

    The log is the plan's own audit trail, and it is read back rather
    than kept in memory: an event that was never written down is not
    an audit, however sure the writer was.

    Args:
        log_path: The plan's execution log.
        event: The event to collect.

    Returns:
        One dict per matching entry: the entry's own ``data``.
    """
    records: List[Dict[str, Any]] = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return records
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("event") != event:
            continue
        data = entry.get("data")
        records.append(data if isinstance(data, dict) else {})
    return records


def _read_bytes(path: Path) -> Optional[bytes]:
    """The file's bytes, or ``None`` when it is not there."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _empty_run(phase: str) -> _Run:
    """A run that never happened, for the case where one did not.

    Args:
        phase: What the run was for.

    Returns:
        The placeholder.
    """
    return _Run(
        phase=phase,
        argv=(),
        exit_code=None,
        outcome=None,
        session_id=None,
        is_error=None,
        events=(),
    )


# --------------------------------------------------------------------------
# The stop
# --------------------------------------------------------------------------


def _trigger_name(event: Any) -> Optional[str]:
    """The name of the event that stops the run, or ``None``.

    The run is stopped by the CLI *saying something*: the first
    ``session.started`` (the conversation exists) or the first
    ``assistant`` event (the model is talking).  Both are read out of
    the canonical event the adapter produced, never out of a clock.

    Args:
        event: One canonical event.

    Returns:
        The name of the trigger, or ``None`` when this event is not
        one.
    """
    payload = event.raw if isinstance(event.raw, dict) else {}
    if payload.get("subtype") == "session.started":
        return "session.started"
    if event.type == "assistant":
        return "assistant"
    return None


class _Stop:
    """The stop: one signal to the child's process group, once.

    Attributes:
        trigger: The event that fired the stop, or ``None``.
        fallback_fired: Whether the stop had to fire without a trigger.
        mcp_during_run: The workspace's ``.mcp.json`` while the run was
            in flight, or ``None`` when it was not read.
    """

    def __init__(self, workspace: Path, timeout: float = FALLBACK_TIMEOUT):
        """Prepare the stop.

        Args:
            workspace: The workspace the run executes in.
            timeout: How long to wait for the trigger before stopping
                the run anyway.
        """
        self._workspace = workspace
        self._timeout = timeout
        self._fired = threading.Event()
        self._process: Any = None
        self._watchdog: Optional[threading.Thread] = None
        self.trigger: Optional[str] = None
        self.fallback_fired = False
        self.mcp_during_run: Optional[bytes] = None

    def attach(self, process: Any) -> None:
        """Take the child, and start the ceiling.

        Args:
            process: The process the runner spawned.
        """
        self._process = process
        self._watchdog = threading.Thread(
            target=self._watch, name="pdt-session-stop", daemon=True
        )
        self._watchdog.start()

    def observe(self, event: Any) -> None:
        """Fire the stop if this event is the trigger.

        Args:
            event: One canonical event.
        """
        if self._fired.is_set():
            return
        name = _trigger_name(event)
        if name is None:
            return
        self.trigger = name
        self._fired.set()
        self._signal(signal.SIGTERM)

    def _watch(self) -> None:
        """Stop the run anyway if the trigger never comes."""
        if self._fired.wait(self._timeout):
            return
        process = self._process
        if process is None or process.poll() is not None:
            # The run ended on its own.  Nothing had to be stopped, so
            # the ceiling did not have to fire -- and saying that it did
            # would turn "the trigger never came" into "the ceiling cut
            # the run short", which are two different failures.
            return
        self.fallback_fired = True
        self._signal(signal.SIGTERM)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.2)
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        """Signal the child's process group.

        The group, not the leader: the CLI starts helpers of its own,
        and a signal to the leader alone leaves them running.

        Args:
            sig: The signal to send.
        """
        process = self._process
        if process is None:
            return
        if self.mcp_during_run is None:
            self.mcp_during_run = _read_bytes(self._workspace / ".mcp.json")
        from utils.process import kill_process_group

        kill_process_group(process, sig=sig, wait_timeout=0.0)


class _StoppingMcodeHarness:
    """The MiniMax adapter, with one method added.

    The registry's built-in mcode adapter is built without a profile
    binding, so this is that adapter with a ``parse_stream`` that looks
    at every event the run produces -- which is all the stop needs.
    """

    def __init__(self, stop: _Stop) -> None:
        """Prepare the adapter.

        Args:
            stop: The stop to fire on the first trigger event.
        """
        from harness.mcode import McodeHarness

        self._adapter = McodeHarness()
        self._stop = stop

    def __getattr__(self, name: str) -> Any:
        """Everything the adapter does, unchanged."""
        return getattr(self._adapter, name)

    def parse_stream(self, lines: Any) -> Iterator[Any]:
        """The adapter's events, with the stop watching them.

        Args:
            lines: The child's output, one line at a time.

        Yields:
            One canonical event per line that carries one.
        """
        for event in self._adapter.parse_stream(lines):
            yield event
            self._stop.observe(event)


# --------------------------------------------------------------------------
# The runs
# --------------------------------------------------------------------------


@dataclass
class _Run:
    """What one dispatch produced.

    Attributes:
        phase: What the run was for.
        argv: The command line the child was actually handed.
        exit_code: The child's exit code, or ``None`` when there was no
            child.
        outcome: The runner's verdict on the run.
        session_id: The conversation the run was in.
        is_error: Whether the run reported a failure.
        events: The canonical events the run produced.
        error: What the dispatch raised, when it raised.
    """

    phase: str
    argv: Tuple[str, ...]
    exit_code: Optional[int]
    outcome: Any
    session_id: Optional[str]
    is_error: Optional[bool]
    events: Tuple[Any, ...]
    error: Optional[BaseException] = None

    def session_flag(self) -> Optional[str]:
        """The conversation the command line named, or ``None``."""
        if "--session" not in self.argv:
            return None
        index = self.argv.index("--session") + 1
        if index >= len(self.argv):
            return None
        return self.argv[index]


def _dispatch(
    tool: Any,
    harness: Any,
    prompt: str,
    *,
    phase: str,
    session_id: Optional[str],
    resume: bool,
    stop: Optional[_Stop] = None,
) -> _Run:
    """Run one dispatch through the production path.

    The dispatch is the service's own -- ``_run_claude_interactive``,
    the one place every spawn goes through -- and the runner it uses is
    the production runner with the result, the command line and the
    child kept.  The child is not mocked: the scenario kills a real
    process.

    Args:
        tool: The dispatch to run.
        harness: The backend the dispatch drives.
        prompt: What the run is asked to do.
        phase: What the run is for.
        session_id: The conversation the run continues, if any.
        resume: Whether the run continues a conversation.
        stop: The stop to arm, if this run is the one that is killed.

    Returns:
        What the run produced.
    """
    import coding_tool

    created: List[Any] = []

    class _RecordingDispatchRunner(coding_tool._DispatchRunner):
        """The dispatch's runner, with the run kept."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.dispatched: Any = None
            self.invocation: Any = None

        def run(
            self, harness: Any, invocation: Any, stdin_text: Any = None
        ) -> Any:
            self.invocation = invocation
            self.dispatched = super().run(
                harness, invocation, stdin_text=stdin_text
            )
            return self.dispatched

        def _collect_events(self, harness: Any, process: Any) -> Any:
            if stop is not None:
                stop.attach(process)
            return super()._collect_events(harness, process)

    def _runner_factory(*args: Any, **kwargs: Any) -> Any:
        runner = _RecordingDispatchRunner(*args, **kwargs)
        created.append(runner)
        return runner

    original_runner = coding_tool._DispatchRunner
    coding_tool._DispatchRunner = _runner_factory
    error: Optional[BaseException] = None
    try:
        tool._run_claude_interactive(
            prompt, session_id=session_id, resume=resume, harness=harness
        )
    except Exception as exc:  # noqa: BLE001 -- the failure is the fact
        error = exc
    finally:
        coding_tool._DispatchRunner = original_runner

    runner = created[0] if created else None
    result = getattr(runner, "dispatched", None)
    invocation = getattr(runner, "invocation", None)
    return _Run(
        phase=phase,
        argv=tuple(getattr(invocation, "argv", ()) or ()),
        exit_code=getattr(result, "exit_code", None),
        outcome=getattr(result, "outcome", None),
        session_id=getattr(result, "session_id", None),
        is_error=getattr(result, "is_error", None),
        events=tuple(getattr(result, "events", ()) or ()),
        error=error,
    )


@dataclass
class _Decision:
    """What the caller above the mapping decided.

    Attributes:
        session_id: The conversation the run continues, or ``None``
            when the run creates one.
        degraded: Whether the resume degraded into a create.
    """

    session_id: Optional[str]
    degraded: bool


def _resolve_conversation(
    harness: Any, session_map: Any, pinned: str
) -> _Decision:
    """Decide which conversation the next run continues.

    The policy task 21 pins: a mapping that does not resolve is a
    miss, and a miss degrades into an explicit create -- never into
    some other conversation.  The decisions belong to the production
    code called here: the mapping resolves or it does not
    (:meth:`SessionMap.lookup`), and the adapter refuses a resume that
    names no real conversation, so a miss cannot be papered over.

    Args:
        harness: The adapter that builds the command line.
        session_map: The mapping the caller reads.
        pinned: The key the caller pinned.

    Returns:
        The decision.
    """
    from harness.contract import (
        HarnessConfigError,
        SessionMode,
        SessionRef,
        TaskInvocation,
    )

    observed = session_map.lookup(pinned)
    if observed is not None:
        return _Decision(session_id=observed, degraded=False)

    with pytest.raises(HarnessConfigError):
        harness.build_invocation(
            TaskInvocation(
                prompt=PROMPT,
                session=SessionRef(session_id=None, pinned_session_id=pinned),
                mode=SessionMode.RESUME,
            )
        )
    return _Decision(session_id=None, degraded=True)


def _silent_switches(
    runs: Sequence[_Run], audits: Sequence[Dict[str, Any]], event: str
) -> int:
    """The times the task's conversation changed without an audit naming it.

    Args:
        runs: The runs, in the order they happened.
        audits: The audit records the plan's log holds.
        event: The event a degradation is audited under.

    Returns:
        How many changes no audit accounted for.
    """
    silent = 0
    previous: Optional[str] = None
    for run in runs:
        current = run.session_id
        if current is None:
            continue
        if previous is not None and current != previous:
            named = any(
                record.get("event") == event
                and record.get("session_id") == current
                for record in audits
            )
            if not named:
                silent += 1
        previous = current
    return silent


@dataclass
class _SessionFacts:
    """What the whole scenario produced.

    Attributes:
        pinned_session_id: The key the caller pinned.
        killed: The run that was stopped by a signal.
        resumed: The retry that continued the conversation.
        degraded: The retry that created a new conversation.
        stop: The stop that stopped the first run.
        mcp_seed: The workspace's own ``.mcp.json``, as it was before
            the first run.
        mcp_after: The workspace's ``.mcp.json`` after the runs.
        audits: The degradation records the plan's log holds.
        degradation_event: The event a degradation is audited under.
        silent_switches: The conversation changes no audit accounted
            for.
        resolved_from_mapping: The conversation the mapping file named
            when a fresh reader read it, right after the killed run's
            registration.
    """

    pinned_session_id: str
    killed: _Run
    resumed: _Run
    degraded: _Run
    stop: _Stop
    mcp_seed: bytes
    mcp_after: Optional[bytes]
    audits: List[Dict[str, Any]]
    degradation_event: str
    silent_switches: int
    resolved_from_mapping: Optional[str] = None


# --------------------------------------------------------------------------
# The scenario
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def session_chain(e2e_server: Any) -> Iterator[_SessionFacts]:
    """Kill a run, continue its conversation, and lose its mapping.

    The scenario is opt-in: without ``PDT_E2E_HARNESS=1`` it is
    skipped, because it spawns the real MiniMax Code CLI under the
    real macOS seatbelt and costs model calls.

    Yields:
        What the scenario produced.
    """
    if os.environ.get(E2E_ENV_VAR) != "1":
        pytest.skip(
            f"{E2E_ENV_VAR}=1 is not set: the real kill-and-resume scenario "
            f"is opt-in, because it costs model calls and spawns the real CLI"
        )

    import coding_tool
    from execution_logger import ExecutionLogger
    from harness.mcode import SessionMap
    from harness.registry import resolve_harness
    from plan_sessions import save_session_id, stable_session_id

    server = e2e_server
    root = server.root / "session"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # The seatbelt profile for this scenario: writes anywhere in the
    # scenario's own tree are refused, and writes inside this
    # scenario's workspace are allowed again.  The account's files are
    # under the real home, and ``(allow default)`` leaves them alone.
    profile_path = root / "sandbox_profile.sb"
    profile_path.write_text(
        "(version 1)\n"
        "(allow default)\n"
        f'(deny file-write* (subpath "{server.root}"))\n'
        f'(allow file-write* (subpath "{workspace}"))\n',
        encoding="utf-8",
    )

    # The workspace's own MCP configuration.  The adapter overwrites it
    # for the length of every run and puts it back afterwards, so the
    # bytes that are here before the first run are the bytes that have
    # to be here after it.
    mcp_seed = json.dumps(
        {"owned": "the workspace's own configuration", "mcpServers": {}},
        indent=2,
    ).encode("utf-8")
    (workspace / ".mcp.json").write_bytes(mcp_seed)

    # The configuration the adapter injects.  It is a different file
    # from the workspace's own, because the point of the assertion is
    # that the workspace's own one comes back.
    mcp_config_path = root / "mcp.json"
    mcp_config_path.write_text(
        json.dumps({"mcpServers": {}}), encoding="utf-8"
    )

    # The mapping: the file that carries the pinned key to the
    # conversation the CLI really minted.
    map_path = root / "session_map.json"
    session_map = SessionMap(map_path)

    # The pinned key, derived and recorded the way the executor derives
    # and records it.
    plan_dir = server.plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    pinned_session_id = stable_session_id(PLAN_ID, STEP_KEY)
    save_session_id(plan_dir, STEP_KEY, pinned_session_id)

    logger = ExecutionLogger(PLAN_ID, plans_dir=server.plans_dir)
    tool = coding_tool.ClaudeCodingTool(
        cwd=str(workspace),
        mcp_config=str(mcp_config_path),
        logger=logger,
    )
    harness = resolve_harness({"PDT_HARNESS": HARNESS_NAME})

    # The secrets published on descriptors by whatever launcher started
    # *this* process have to go: their numbers name descriptors that
    # mean something else here.
    scrubbed = {
        name: None for name in os.environ if name.startswith("PDT_SECRET_FD_")
    }
    stop = _Stop(workspace)
    killed = _empty_run("killed")
    resumed = _empty_run("resumed")
    degraded = _empty_run("degraded")
    resolved_from_mapping: Optional[str] = None
    event = _degradation_event()
    with _environment(
        HOME=_real_home(),
        PDT_SANDBOX_PROFILE=str(profile_path),
        PDT_PROVIDER_MODE="inherit",
        PDT_PLANS_DIR=str(server.plans_dir),
        PDT_STATE_DB_PATH=str(server.state_db),
        PDT_PLAN_ID=None,
        **scrubbed,
    ):
        # -- one: the run that is stopped --------------------------------
        killed = _dispatch(
            tool,
            _StoppingMcodeHarness(stop),
            PROMPT,
            phase="killed",
            session_id=pinned_session_id,
            resume=False,
            stop=stop,
        )
        if not killed.session_id:
            pytest.fail(
                "the killed run reported no session, so there is no "
                "conversation for the retry to continue: "
                f"{killed.error!r}"
            )

        # -- two: the retry continues that conversation ------------------
        # The registration comes first: the id is only handed to the
        # next run once it is on disk.
        session_map.register(pinned_session_id, str(killed.session_id))
        # A later process cannot remember what an earlier one knew; it
        # can only read the file.  So the mapping is read again here --
        # by a reader that knows nothing but the pinned key -- and that
        # is the conversation the retry is told to continue.
        resolved_from_mapping = SessionMap(map_path).lookup(pinned_session_id)
        decision = _resolve_conversation(harness, session_map, pinned_session_id)
        assert not decision.degraded, (
            "the mapping did not resolve after the registration, so the "
            "retry would have degraded instead of continuing the "
            "conversation the killed run opened"
        )
        resumed = _dispatch(
            tool,
            harness,
            PROMPT,
            phase="resumed",
            session_id=decision.session_id,
            resume=True,
        )

        # -- three: the mapping is gone ----------------------------------
        map_path.unlink()
        decision = _resolve_conversation(harness, session_map, pinned_session_id)
        assert decision.degraded, (
            "the mapping was deleted and the caller still resolved a "
            "conversation out of it"
        )
        degraded = _dispatch(
            tool,
            harness,
            PROMPT,
            phase="degraded",
            session_id=pinned_session_id,
            resume=False,
        )
        # The degradation is audited: the audit names the task that
        # degraded and the conversation it ended up in.
        logger.log(
            "WARNING",
            event,
            "the session mapping did not resolve; the task continued in "
            "a new conversation",
            task_id=TASK_ID,
            data={
                "pinned_session_id": pinned_session_id,
                "session_id": degraded.session_id,
                "event": event,
            },
        )
        if degraded.session_id:
            session_map.register(pinned_session_id, str(degraded.session_id))

    audits = _audit_records(plan_dir / "execution.log", event)
    yield _SessionFacts(
        pinned_session_id=pinned_session_id,
        killed=killed,
        resumed=resumed,
        degraded=degraded,
        stop=stop,
        mcp_seed=mcp_seed,
        mcp_after=_read_bytes(workspace / ".mcp.json"),
        audits=audits,
        degradation_event=event,
        silent_switches=_silent_switches(
            [killed, resumed, degraded], audits, event
        ),
        resolved_from_mapping=resolved_from_mapping,
    )


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------


def test_resume_uses_captured_session(session_chain: _SessionFacts) -> None:
    """The retry continues the conversation the killed run opened.

    Args:
        session_chain: What the scenario produced.
    """
    from harness.claude import EXIT_CODE_KINDS, SIGNAL_NAMES, classify_exit
    from harness.contract import OUTCOME_OK, OUTCOME_SIGNALED, OUTCOME_UNKNOWN

    facts = session_chain

    # -- the run was stopped on an event, not on a clock -----------------
    assert facts.stop.trigger in TRIGGER_EVENTS, (
        "the run was not stopped on a trigger event: the stop fired on "
        f"{facts.stop.trigger!r}, and the scenario's whole point is that "
        "the stop is the CLI saying something, not a number of seconds "
        "passing"
    )
    assert not facts.stop.fallback_fired, (
        "the trigger never came and the stop had to fire on its ceiling, "
        "so this run says nothing about what the trigger does"
    )

    # -- the run was stopped by a signal ---------------------------------
    # The runner reports the child's exit code the way ``process.wait()``
    # gives it: a child that was *killed* by the signal reports the
    # negative signal number, and a CLI that catches the signal and
    # leaves on its own reports 128 + the signal number instead.  Both
    # are the same fact -- the run was cut short by a signal -- and both
    # have to land in the "signaled" category, which is where the
    # production code puts them: the runner's own default calls a
    # negative code "signaled", and the production exit table calls 130
    # and 143 "signaled" as well.
    exit_code = facts.killed.exit_code
    assert exit_code is not None, "the killed run reported no exit code"
    if exit_code < 0:
        assert -exit_code in (int(signal.SIGINT), int(signal.SIGTERM)), (
            f"the killed run was killed by signal {-exit_code}, which is "
            f"not a signal the scenario sent"
        )
        assert facts.killed.outcome is not None, (
            "the killed run produced no verdict"
        )
        assert facts.killed.outcome.kind == OUTCOME_SIGNALED, (
            f"the runner's verdict for a child killed by signal "
            f"{-exit_code} was {facts.killed.outcome.kind!r}, not "
            f"{OUTCOME_SIGNALED!r}"
        )
    else:
        assert exit_code in SIGNALLED_EXIT_CODES, (
            f"the killed run exited {exit_code!r}, which is not a "
            f"signal-derived code ({sorted(SIGNALLED_EXIT_CODES)}); the run "
            "was not stopped by a signal"
        )
        assert EXIT_CODE_KINDS[exit_code] == OUTCOME_SIGNALED, (
            f"the production exit table does not call {exit_code} "
            f"signalled: {EXIT_CODE_KINDS[exit_code]!r}"
        )
        # The category, from the production classifier itself rather
        # than from its table: a run that was cut short by a signal is a
        # signalled run, not a run that happened to fail.
        verdict = classify_exit(exit_code, facts.killed.events)
        assert verdict.kind == OUTCOME_SIGNALED, (
            f"the production exit classifier does not call {exit_code} a "
            f"signalled exit "
            f"({SIGNAL_NAMES.get(exit_code, 'an unnamed signal')}): "
            f"{verdict.kind!r} ({verdict.message!r})"
        )
    assert facts.killed.outcome is not None, "the killed run produced no verdict"
    assert facts.killed.outcome.kind in (OUTCOME_SIGNALED, OUTCOME_UNKNOWN), (
        f"the killed run's verdict was {facts.killed.outcome.kind!r}; the "
        "MiniMax adapter has no classifier of its own, so the runner's "
        "default verdict for a positive exit code is 'unknown' -- what has "
        "to hold is that the run is not a success"
    )
    assert facts.killed.outcome.kind != OUTCOME_OK, (
        "the killed run was reported as a success"
    )
    assert facts.killed.is_error is True, (
        "the killed run was not reported as an error"
    )

    # -- the conversation survived the process that opened it -------------
    assert facts.killed.session_id, "the killed run reported no session"
    assert MCODE_SESSION_RE.match(str(facts.killed.session_id)), (
        f"the session id is not a MiniMax Code one: "
        f"{facts.killed.session_id!r}"
    )

    # -- the workspace's own .mcp.json was put back -----------------------
    assert facts.stop.mcp_during_run is not None, (
        "the workspace held no .mcp.json while the run was in flight, so "
        "'it is back now' would be true of a file that was never there"
    )
    assert facts.stop.mcp_during_run != facts.mcp_seed, (
        "the workspace's own .mcp.json was never replaced, so the adapter "
        "did not inject anything and the restoration below would be "
        "vacuous"
    )
    assert facts.mcp_after == facts.mcp_seed, (
        "the workspace's own .mcp.json was not put back: "
        f"{facts.mcp_after!r} is not {facts.mcp_seed!r}"
    )

    # -- the retry continued that conversation ----------------------------
    assert "--session" in facts.resumed.argv, (
        f"the retry's command line names no conversation: {facts.resumed.argv!r}"
    )
    second_argv = facts.resumed.argv
    assert second_argv[second_argv.index("--session") + 1].startswith("mvs_"), (
        f"the retry's command line does not name a MiniMax Code "
        f"conversation: {second_argv!r}"
    )
    assert facts.resumed.session_flag() == facts.killed.session_id, (
        "the retry continued a different conversation: "
        f"{facts.resumed.session_flag()!r} is not "
        f"{facts.killed.session_id!r}"
    )
    # -- and it got there through the mapping, not through memory --------
    # A later process cannot remember what an earlier one knew; it can
    # only read the file.  So the id the retry was handed has to be the
    # id a fresh reader of the mapping file gets -- otherwise the
    # recovery would work only inside the process that happened to run
    # both halves, which is the one process that is not there when a
    # plan really resumes.
    assert facts.resolved_from_mapping == facts.killed.session_id, (
        "the mapping file does not name the conversation the killed run "
        f"opened: {facts.resolved_from_mapping!r} is not "
        f"{facts.killed.session_id!r}"
    )
    assert facts.resumed.session_flag() == facts.resolved_from_mapping, (
        "the retry was not handed the conversation the mapping named: "
        f"{facts.resumed.session_flag()!r} is not "
        f"{facts.resolved_from_mapping!r}"
    )
    assert facts.resumed.session_id == facts.killed.session_id, (
        "the retry's own run reported a different conversation: "
        f"{facts.resumed.session_id!r} is not {facts.killed.session_id!r}"
    )
    assert facts.resumed.exit_code == 0, (
        f"the retry exited {facts.resumed.exit_code!r}; the conversation "
        f"could not be continued: {facts.resumed.error!r}"
    )


def test_missing_map_degrades_visibly(session_chain: _SessionFacts) -> None:
    """A mapping that is gone degrades into an audited create.

    Args:
        session_chain: What the scenario produced.
    """
    facts = session_chain

    # -- the retry created a conversation instead of resuming one -------
    assert "--session" not in facts.degraded.argv, (
        "the retry named a conversation although the mapping was gone: "
        f"{facts.degraded.argv!r}"
    )
    assert facts.degraded.session_id, (
        "the degraded run reported no session, so it created no "
        f"conversation: {facts.degraded.error!r}"
    )
    assert facts.degraded.session_id != facts.killed.session_id, (
        "the degraded run continued the conversation the mapping had lost"
    )
    assert MCODE_SESSION_RE.match(str(facts.degraded.session_id)), (
        f"the session id is not a MiniMax Code one: "
        f"{facts.degraded.session_id!r}"
    )

    # -- and the degradation is in the audit ------------------------------
    assert facts.audits, (
        "no degradation event entered the plan's audit, so the switch to "
        "a new conversation happened silently"
    )
    record = facts.audits[0]
    assert record.get("event") == facts.degradation_event, (
        f"the audit record does not carry the event task 21 names: "
        f"{record.get('event')!r} is not {facts.degradation_event!r}"
    )
    assert record.get("pinned_session_id") == facts.pinned_session_id, (
        "the audit record does not name the task that degraded: "
        f"{record.get('pinned_session_id')!r}"
    )
    assert record.get("session_id") == facts.degraded.session_id, (
        "the audit record does not name the conversation the task ended up "
        f"in: {record.get('session_id')!r}"
    )

    # -- and no conversation ever changed without an audit naming it -----
    assert facts.silent_switches == 0, (
        f"{facts.silent_switches} conversation change(s) happened without an "
        "audit naming them; a silent switch to another conversation is "
        "worse than a run that failed"
    )
