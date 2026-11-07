"""The one place a harness process is spawned.

Why this module exists
======================

Spawning a coding agent is four steps that used to be written out
again at every call site: build the argv, wrap it in the sandbox,
merge the environment, spawn the process and read its stream. Each
copy was slightly different — one wrapped the command *after* it had
already decided the environment, another read the whole stream into
memory before looking at it — and the differences were invisible,
because the code that had them was a long function that also resolved
providers and wrote settings files.

This module is those four steps, once. A caller hands it a harness and
an invocation; it returns a :class:`harness.contract.HarnessResult`.
Nothing above this layer spawns a process, so there is exactly one
place where the order of the steps is decided, and exactly one place
where a change to it has to be made.

The order is fixed
==================

``sandbox decision -> environment merge -> spawn -> read line by line``

* **The sandbox decision comes first.** A profile that is configured
  but cannot be applied is a configuration error, and
  :func:`claude_sandbox.wrap_command` raises for it. The raise
  happens *before* the spawn, so a broken profile leaves no
  half-started child behind — there is nothing to clean up, because
  nothing was started.
* **The environment is merged after the decision** and before the
  spawn, so the child gets the inherited environment, the marker the
  sandbox wrapper wrote, and the adapter's own additions — in that
  order, with the adapter's additions last.
* **The stream is read line by line.** The result carries the
  *normalized event stream* (the contract's own shape); the raw text
  of the stream is never held as one blob, so a run that talks for an
  hour does not cost an hour's worth of memory.

What the runner does not know
==============================

It does not know which harness it is driving. The argv, the
environment and the working directory arrive in the invocation; the
meaning of the output and of the exit code belongs to the adapter.

An adapter that has not implemented :meth:`Harness.parse_stream` or
:meth:`Harness.classify_exit` yet is not an error — the claude
adapter's stream work arrives separately from its command-line work.
Such an adapter gets the raw lines as events of type ``"output"``, and
the exit code is classified by the rules below. An adapter that *has*
implemented them gets its own answers, and the runner does not second-
guess them.

The child is always reaped
==========================

A spawn that fails half-way must not leave a process behind. The
child is started in its own session, so it can be signalled as a group
rather than as a single process, and it is waited for on every path:
the normal one (the stream ended, the exit code is read) and the
exceptional one (something raised while the stream was being read, in
which case the group is terminated and reaped before the error
leaves).

The runner owns the signal
==========================

A run can be cut short from the outside: the framework stops a plan
by signalling the process group the run belongs to. The default
disposition of ``SIGTERM`` ends the process where it stands, and a
process that ends there gives nothing back — the file the adapter
wrote for the child stays in the workspace, and the next
``git add -A`` puts it into a commit.

So the runner owns the signal for the length of the run. A
``SIGTERM`` becomes an exception the run can unwind from, and the
unwind is the ordinary one: the child is ended, the prepared context
is closed, the workspace goes back to what it was found as. The
disposition that was in place before the run is the disposition that
is in place after it — the runner borrows the signal, it does not
take it.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from collections import deque
from contextlib import contextmanager, nullcontext
from typing import (
    Any,
    Deque,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)

import claude_sandbox
from bounded_subprocess import terminate_process_tree

from .contract import (
    Harness,
    HarnessConfigError,
    HarnessEvent,
    HarnessInvocation,
    HarnessOutcome,
    HarnessResult,
    OUTCOME_OK,
    OUTCOME_SIGNALED,
    OUTCOME_UNKNOWN,
)

__all__ = [
    "HarnessInterrupted",
    "Runner",
    "SANDBOX_APPLIED",
    "SANDBOX_INHERITED",
    "SANDBOX_UNSANDBOXED",
    "carried_the_conversation",
]

logger = logging.getLogger(__name__)

#: The sandbox wrapper applied the profile: the child is confined.
SANDBOX_APPLIED = "applied"

#: The sandbox was inherited from an enclosing one.
SANDBOX_INHERITED = "inherited"

#: No profile is configured, so the child is not confined.
#:
#: The contract lists ``"not_configured"`` among the tokens a sandbox
#: state may take. This runner says ``"unsandboxed"`` instead, and the
#: difference is deliberate: the token answers "what was the child
#: run under", not "what was configured". An operator reading a run's
#: result wants to know whether the child was confined — and a token
#: named after the *configuration* reads as a statement about the
#: machine rather than about the run.
SANDBOX_UNSANDBOXED = "unsandboxed"

#: What each sandbox decision's reason becomes in the result.
_SANDBOX_STATE = {
    "applied": SANDBOX_APPLIED,
    "inherited": SANDBOX_INHERITED,
    "not_configured": SANDBOX_UNSANDBOXED,
}

#: The event type a raw output line carries when the adapter has no
#: parser of its own.
OUTPUT_EVENT = "output"

#: The event type a line the child wrote to stderr carries.
STDERR_EVENT = "stderr"

#: How many stderr lines are kept. A chatty child must not be able to
#: fill the pipe and block, and its output must not be unbounded
#: either; the tail is what diagnoses a failure.
STDERR_KEEP_LINES = 200

#: How long the stderr drain is given to finish once stdout ended.
STDERR_JOIN_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# The signal guard
# ---------------------------------------------------------------------------


class HarnessInterrupted(BaseException):
    """A termination signal reached the run before the child was done.

    A run that is cut short still owns things it has to give back: the
    file the adapter wrote for the child's benefit, the settings file
    that carries a credential, the child itself. The platform's default
    disposition for ``SIGTERM`` gives none of that back — it ends the
    process where it stands, and a file the adapter wrote stays in the
    workspace, where the next ``git add -A`` puts it into a commit.

    So the signal is turned into this exception instead, and the run
    unwinds through the same path every other ending takes: the child
    is ended, the prepared context is closed, and only then does the
    exception leave the runner.

    It is a :class:`BaseException` and not an :class:`Exception` on
    purpose. An ``except Exception`` somewhere up the stack would
    swallow it, and the process would carry on as if the run had ended
    normally — which is the failure this class exists to prevent.

    Attributes:
        signum: The signal that ended the run.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(f"the run was interrupted by signal {signum}")
        self.signum = signum


@contextmanager
def _interrupt_guard() -> Iterator[None]:
    """Turn a termination signal into an exception the run can unwind from.

    The guard owns the signal for the length of the run and gives it
    back afterwards: whatever disposition was in place when the run
    started is the disposition in place when it ends. A handler
    somebody else installed — the server's, say — is displaced for the
    length of the run and put back afterwards, so the process keeps the
    termination semantics it had.

    A thread that is not the main thread gets no guard: Python only
    lets the main thread own a signal handler. That is not a quiet
    fallback — it is written down, because a run that cannot be
    interrupted cleanly is a run whose prepared context may be left
    behind, and a thing that fails quietly is worse than a thing that
    says so.

    Yields:
        Nothing. The block is the length of the run.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning(
            "[HARNESS RUN] no signal guard installed: the run is on a "
            "thread that is not the main thread, and only the main "
            "thread may own a signal handler. A SIGTERM to this process "
            "will end it the way the platform ends it, and what the run "
            "prepared may not be given back."
        )
        yield
        return

    previous = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }

    def _interrupt(signum: int, _frame: Any) -> None:
        raise HarnessInterrupted(signum)

    for signum in previous:
        signal.signal(signum, _interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(
                    signum,
                    handler if handler is not None else signal.SIG_DFL,
                )
            except (OSError, TypeError, ValueError):  # pragma: no cover
                logger.warning(
                    "[HARNESS RUN] could not restore the %s handler",
                    signal.Signals(signum).name,
                )


class Runner:
    """Spawns a harness and turns what it said into a result.

    The runner holds no state: one instance serves every run, and two
    runs never share anything but the process they each own.
    """

    def run(
        self,
        harness: Harness,
        invocation: HarnessInvocation,
        stdin_text: Optional[str] = None,
    ) -> HarnessResult:
        """Run one harness process and report what it produced.

        Args:
            harness: The adapter that owns the command line's meaning.
                Its ``parse_stream`` and ``classify_exit`` are used when
                it has them.
            invocation: How to spawn the process: the argv, the
                environment to add, the working directory, and an
                optional context manager to hold for the run.
            stdin_text: What to write to the child's standard input.
                The pipe is closed afterwards, so a child that reads to
                EOF sees the prompt and then the end of it.

        Returns:
            The result of the run: its exit code, its events, and what
            those mean.

        Raises:
            ValueError: The invocation carries no argv.
            HarnessConfigError: The run was asked for something the
                harness cannot do — a multi-turn task on a harness that
                reports no stream — or it produced no stream at all.
                No child is left behind in either case.
            claude_sandbox.SandboxUnavailableError: A profile is
                configured but cannot be applied. The child was never
                started.
            HarnessInterrupted: A termination signal reached the run.
                The child was ended and the prepared context was
                closed before the interruption left the runner.
        """
        argv = list(invocation.argv)
        if not argv:
            raise ValueError("Runner.run() needs a non-empty argv")

        # 1. The sandbox decision, before anything is spawned. A
        #    configured profile that cannot be applied raises here, and
        #    a child that was never started leaves nothing behind.
        env = os.environ.copy()
        decision = claude_sandbox.wrap_command(argv, env)

        # 2. The capability the run needs, before the child exists. A
        #    multi-turn task is a conversation, and a conversation needs
        #    a stream; a harness that says it cannot stream cannot
        #    produce one, so the run is refused here rather than
        #    answered with an envelope.
        _assert_can_stream(harness, invocation)

        # 3. The invocation's environment, merged on top of the
        #    inherited one and of the marker the wrapper just wrote.
        env.update(invocation.env_delta)

        # 4. The spawn, and everything that happens to the child.
        prepare = invocation.prepare
        context = prepare() if prepare is not None else nullcontext()
        # The guard is entered *before* the prepare context and left
        # *after* it. A signal that arrives while the context is being
        # unwound must still be ours to turn into an exception, and
        # the context must be unwound while the guard is in place: the
        # order is what makes a signalled run give the workspace back.
        with _interrupt_guard(), context:
            logger.debug(
                "[HARNESS RUN cmd=%r sandbox=%s]",
                decision.command[0],
                _SANDBOX_STATE.get(decision.reason, SANDBOX_UNSANDBOXED),
            )
            process: Optional["subprocess.Popen[str]"] = None
            try:
                process = subprocess.Popen(
                    decision.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=invocation.cwd,
                    env=env,
                    start_new_session=True,
                )
                _write_stdin(process, stdin_text)
                events = self._collect_events(harness, process)
                exit_code = process.wait()
            except BaseException:
                # The child is ours until it is reaped. An error while
                # the stream was being read — or a signal that ended
                # the run — must not leave it running.
                if process is not None:
                    terminate_process_tree(process)
                raise
            finally:
                # The child is gone; its pipes are not needed and must
                # not be left for the garbage collector to close.
                if process is not None:
                    _close_pipes(process)

        # The child is gone and its context is closed. What it said is
        # now the only thing there is to judge: a multi-turn task that
        # produced no stream did not do what it was asked, and saying
        # so is the only honest report.
        _assert_streamed(invocation, events, _harness_name(harness))

        summary = _summarize(harness, events, exit_code)
        return HarnessResult(
            exit_code=exit_code,
            events=tuple(events),
            final_text=summary.final_text,
            usage=summary.usage,
            usage_partial=summary.usage_partial,
            is_error=summary.is_error,
            session_id=summary.session_id,
            sandbox_state=_SANDBOX_STATE.get(decision.reason, SANDBOX_UNSANDBOXED),
            harness=_harness_name(harness),
            outcome=summary.outcome,
        )

    def _collect_events(
        self,
        harness: Harness,
        process: "subprocess.Popen[str]",
    ) -> List[HarnessEvent]:
        """Read the child's output and turn it into events.

        Standard output is read line by line and handed to the
        adapter's parser; standard error is drained at the same time,
        because a child that fills the stderr pipe while nobody is
        reading it blocks forever.
        """
        stderr_lines: Deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        drain = threading.Thread(
            target=_drain_stderr,
            args=(process.stderr, stderr_lines),
            name="harness-stderr-drain",
            daemon=True,
        )
        drain.start()

        parser = getattr(harness, "parse_stream", None)
        if parser is None:
            # No parser yet: the raw lines are the events. They are
            # still read one at a time, so the stream is never held as
            # one blob.
            events = [_line_event(OUTPUT_EVENT, line) for line in _lines(process.stdout)]
        else:
            events = list(parser(_lines(process.stdout)))
            # A parser is free to stop early. The rest of the stream
            # still has to be drained, or the child blocks on a full
            # pipe and the run never ends.
            for _ in process.stdout:
                pass

        drain.join(timeout=STDERR_JOIN_TIMEOUT)
        events.extend(_line_event(STDERR_EVENT, line) for line in stderr_lines)
        return events


# ---------------------------------------------------------------------------
# The pieces the run is made of
# ---------------------------------------------------------------------------


def _assert_can_stream(harness: Harness, invocation: HarnessInvocation) -> None:
    """Refuse a multi-turn run on a harness that cannot stream.

    The fact that the run is multi-turn is the adapter's to state — it
    wrote the command line — and the capability that says whether the
    harness can stream is the adapter's to declare. The runner only
    puts the two together, and refuses the combination that cannot
    work: a multi-turn task on a harness that reports no stream would
    be answered with the one thing a multi-turn task must not be
    answered with, namely an envelope.

    A single-turn run is not affected. An envelope on its own is the
    whole answer of a single-turn task.

    Args:
        harness: The adapter that owns the command line.
        invocation: How the run was asked for.

    Raises:
        HarnessConfigError: The run is multi-turn and the harness
            declares no stream. Nothing was spawned.
    """
    if not invocation.multi_turn:
        return

    capabilities = harness.capabilities()
    if capabilities.stream_events:
        return

    raise HarnessConfigError(
        f"a multi-turn task needs a stream of events, and the harness "
        f"{_harness_name(harness)!r} declares no stream "
        f"(stream_events is False); refusing to run a multi-turn task "
        f"that could only produce its terminal envelope"
    )


def _assert_streamed(
    invocation: HarnessInvocation,
    events: Sequence[HarnessEvent],
    harness_name: str,
) -> None:
    """Refuse a multi-turn run that produced no stream.

    A multi-turn task is a conversation, and a conversation is the
    assistant's own turns arriving while it happens. A run that
    produced only its terminal envelope answered a single-turn
    question; handing that to a caller who asked for a conversation
    would let one event be read as a whole conversation.

    So the run is refused, loudly, instead of being passed off as the
    thing it was asked for. A single-turn run is not affected: an
    envelope on its own is the whole answer of a single-turn task.

    Args:
        invocation: How the run was asked for.
        events: The events the run produced, in the order they arrived.
        harness_name: The adapter's name, for the message.

    Raises:
        HarnessConfigError: The run is multi-turn and the stream
            carried no assistant event at all.
    """
    if not invocation.multi_turn:
        return
    if _streamed_assistant(events):
        return

    types = [event.type for event in events]
    raise HarnessConfigError(
        f"a multi-turn task needs a stream of assistant events, and the "
        f"harness {harness_name!r} produced none: the run carried the "
        f"terminal envelope only (stream_events 缺失 / 仅终态 envelope). "
        f"Event types: {types!r}"
    )


def _streamed_assistant(events: Sequence[HarnessEvent]) -> bool:
    """Whether the stream carried the work of a conversation.

    A conversation is the assistant's own turns, and — in the claude
    stream — the tool calls those turns make. A run that produced
    neither produced no conversation at all.

    Args:
        events: The events of one run, in the order they arrived.

    Returns:
        ``True`` when the stream carried an assistant event or a
        ``tool_use`` content block.
    """
    for event in events:
        if event.type == "assistant":
            return True
        if _carries_tool_use(event.raw):
            return True
    return False


def carried_the_conversation(events: Sequence[HarnessEvent]) -> bool:
    """Whether the stream carried a conversation, and not only its envelope.

    The terminal envelope is what every run ends with — including a run
    that never streamed: it is the summary of the run, not the run. A
    conversation is more than that. It is the assistant's own turns, the
    tool calls those turns make, or the session event that says a
    conversation was opened at all (claude's ``init``, mcode's
    ``session.started``).

    This is the question the DISPATCH asks, and it is not the question
    the runner's criterion asks. The runner refuses a run that
    **promised** a stream and produced no assistant event (see
    :func:`_assert_streamed`) — the stricter question, and it stays
    where it is. This one asks the weaker thing: did the run say
    anything at all beyond its envelope.

    Args:
        events: The events of one run, in the order they arrived.

    Returns:
        ``True`` when the stream carried anything but the terminal
        envelope.
    """
    if _streamed_assistant(events):
        return True
    return any(event.type == "system" for event in events)


def _carries_tool_use(raw: Any) -> bool:
    """Whether a payload carries a ``tool_use`` content block.

    Args:
        raw: The payload an event was parsed out of.

    Returns:
        ``True`` when the payload's message content carries a
        ``tool_use`` block.
    """
    if not isinstance(raw, dict):
        return False
    message = raw.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )


def _lines(stream: Any) -> Iterable[str]:
    """The stream's lines, without their terminator.

    The adapter is handed the text of a line, not the line and the
    byte that ends it: parsing a line should not have to know how the
    child chose to end it.
    """
    if stream is None:
        return ()
    for raw in stream:
        yield raw.rstrip("\n").rstrip("\r")


def _line_event(event_type: str, line: str) -> HarnessEvent:
    """One line of output, as an event."""
    return HarnessEvent(type=event_type, raw={"line": line}, text=line)


def _close_pipes(process: "subprocess.Popen[str]") -> None:
    """Close the child's pipes.

    Called once the child has been reaped: nothing can be written to
    it any more, and a backend that runs a harness many times over
    must not leave a descriptor behind each time.
    """
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):  # pragma: no cover - already closed
            pass


def _drain_stderr(stream: Any, sink: Deque[str]) -> None:
    """Read standard error into ``sink`` until the stream ends."""
    if stream is None:
        return
    try:
        for raw in stream:
            sink.append(raw.rstrip("\n").rstrip("\r"))
    except (OSError, ValueError):  # pragma: no cover - closed pipe
        pass


def _write_stdin(process: "subprocess.Popen[str]", stdin_text: Optional[str]) -> None:
    """Write the prompt and close the pipe.

    The pipe is closed even when there is nothing to write: a child
    that reads its prompt from standard input waits for the end of the
    stream, and a pipe left open is a run that never finishes.
    """
    stream = process.stdin
    if stream is None:
        return
    try:
        if stdin_text:
            stream.write(stdin_text)
            if not stdin_text.endswith("\n"):
                stream.write("\n")
            stream.flush()
    except (BrokenPipeError, OSError):
        # The child closed its input before reading all of it. The run
        # is the child's to answer for, not an error of ours.
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass


def _default_outcome(exit_code: int) -> HarnessOutcome:
    """The verdict for a run whose adapter does not classify exits."""
    if exit_code == 0:
        return HarnessOutcome(kind=OUTCOME_OK)
    if exit_code < 0:
        return HarnessOutcome(
            kind=OUTCOME_SIGNALED,
            message=f"the child was killed by signal {-exit_code}",
        )
    return HarnessOutcome(
        kind=OUTCOME_UNKNOWN,
        message=f"the child exited with code {exit_code}",
    )


def _harness_name(harness: Harness) -> str:
    """The adapter's name, for the result.

    An adapter that names itself is taken at its word. One that does
    not is named after its class — ``McodeHarness`` is ``mcode`` —
    because a result that cannot say which backend produced it is
    half an answer.
    """
    name = getattr(harness, "name", "")
    if isinstance(name, str) and name.strip():
        return name.strip()
    class_name = type(harness).__name__
    if class_name.endswith("Harness"):
        class_name = class_name[: -len("Harness")]
    return class_name.lower()


class _Summary:
    """What the events of a run add up to."""

    __slots__ = (
        "final_text",
        "usage",
        "usage_partial",
        "is_error",
        "session_id",
        "outcome",
    )

    def __init__(
        self,
        final_text: str,
        usage: Optional[Dict[str, Any]],
        usage_partial: bool,
        is_error: bool,
        session_id: Optional[str],
        outcome: HarnessOutcome,
    ) -> None:
        self.final_text = final_text
        self.usage = usage
        self.usage_partial = usage_partial
        self.is_error = is_error
        self.session_id = session_id
        self.outcome = outcome


def _summarize(
    harness: Harness,
    events: Sequence[HarnessEvent],
    exit_code: int,
) -> _Summary:
    """Read the run's verdict out of its events.

    The event vocabulary is the contract's: ``"result"`` carries the
    final answer, ``"error"`` carries a failure, and an event that
    reports usage or a session id is the last one that knows about it.
    An adapter that classifies the exit itself has the final word.

    The ``usage_partial`` flag is read, not recomputed. The adapter
    that wrote the usage block is the one that knows which of its
    fields were never reported, and it says so on the event that
    carries the count. A runner that re-derived the flag from the exit
    code would throw that knowledge away: a run that ended cleanly but
    reported two of the four counts would be counted as complete, and
    the number would understate the cost without saying so.
    """
    final_text = ""
    usage: Optional[Dict[str, Any]] = None
    usage_partial: Optional[bool] = None
    session_id: Optional[str] = None
    reported_error = False

    for event in events:
        if event.type == "result" and event.text:
            final_text = event.text
        if event.usage:
            # The count and the flag that describes it travel together:
            # the last event that reports a usage is the one whose
            # marker counts.
            usage = event.usage
            usage_partial = event.usage_partial
        elif event.usage_partial:
            usage_partial = True
        if event.session_id:
            session_id = event.session_id
        if event.is_error or event.type == "error":
            reported_error = True

    classifier = getattr(harness, "classify_exit", None)
    if classifier is not None:
        outcome = classifier(exit_code, events)
    else:
        outcome = _default_outcome(exit_code)

    return _Summary(
        final_text=final_text,
        usage=usage,
        # The adapter's own verdict on the count, and — on top of it —
        # the one fact the adapter cannot know: a run that did not end
        # cleanly may have reported only part of what it spent.
        usage_partial=bool(usage_partial) or exit_code != 0,
        is_error=reported_error or outcome.kind != OUTCOME_OK,
        session_id=session_id,
        outcome=outcome,
    )
