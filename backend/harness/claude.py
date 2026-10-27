"""The claude adapter: the command line, and the output it produces.

Why this module exists
======================

Every claude dispatch in this codebase spawned the same binary with the
same flags, and the flags were assembled inline in the middle of the
function that spawned them -- between "the provider has been chosen"
and "the process is about to start". That placement is what made the
command line hard to reason about: the argv was a side effect of a
long function that also resolved providers, wrote settings files and
stamped a UUID onto the environment, and there was nowhere to *test*
the argv without doing all of that.

This module is that part on its own. It takes a
:class:`harness.contract.TaskInvocation` and returns the command line
to run, with nothing else in it: no process, no filesystem, no
provider, no environment. That is what makes it testable -- the argv
is a pure function of the request, so the tests construct a request
and compare the argv, and never stub anything (test decision point 2).

The other end of the same run lives here too: the parser that reads
the child's output and the classifier that says what its exit code
meant. Both are pure functions of what they are given -- a list of
lines, an exit code and the events that came with it -- so both are
testable without a child process, and neither of them knows anything
about a provider.

What is deliberately *not* here
===============================

* **The prompt.** The claude dispatch is interactive: the prompt is
  written to the child's stdin and the child ends the conversation by
  emitting a ``result`` event. There is no ``--print`` flag, so the
  prompt has no place in the argv.
* **The settings file.** ``--settings`` points at a file that has to
  be written first, into a per-run directory, after the environment has
  been finalised. That is the wiring's job, and the flag is appended
  there -- not here.
* **The environment.** The adapter adds nothing to the environment the
  child inherits, so ``env_delta`` is always empty. The values the
  dispatch does contribute (``PDT_SUBAGENT_UUID`` and friends) belong
  to the run, not to the command line.

The order of the flags is fixed
===============================

``base -> append-system-prompt -> model -> mcp-config -> session ->
allowedTools``

The order is not arbitrary and not negotiable. The command line this
module produces is the one the code produced before it moved here --
``backend/tests/fixtures/harness/claude_argv_golden.json`` was
captured from that inline construction, and it is the evidence this
adapter has to reproduce. A flag that moved one slot to the left would
still parse and would still run the same model, so nothing would
break -- which is exactly why the order is pinned by a test rather
than left to chance.

Scope
=====

This adapter answers the five questions that describe a claude run:
what it is called (:attr:`ClaudeHarness.name`), what it can do
(:meth:`ClaudeHarness.capabilities`), what command line it runs
(:meth:`ClaudeHarness.build_invocation`), how its output is read
(:meth:`ClaudeHarness.parse_stream`) and what its exit code meant
(:meth:`ClaudeHarness.classify_exit`).

The parser is an identity
=========================

The claude CLI writes one JSON document per line. The layer above this
one consumes that stream *in the claude event shape* -- it wants the
CLI's own events, not a paraphrase of them -- so the parser's job is
narrow and strict: **carry the payload across unchanged**.

Three rules hold it to that:

1. **A valid line arrives as the payload it was.** ``event.raw`` is the
   parsed document, key for key: nothing dropped, nothing added,
   nothing renamed, nothing reordered. The events are the stream, in
   the order the stream arrived.
2. **Nothing is dropped silently.** A line that does not parse -- the
   tail of a stream that was cut off mid-write -- becomes a
   ``parse_error`` event carrying the offending text and the line it
   came from. A dropped line is a run whose failure leaves no trace.
3. **Undecodable bytes do not stop the stream.** They are decoded with
   ``errors="replace"`` and the run continues.

The exit code table
===================

An exit code is a number; what the run *meant* is a verdict. The layer
above branches on the verdict, so the mapping is written down once, in
:data:`EXIT_CODE_KINDS`:

============  ==========================================================
Exit code    Verdict
============  ==========================================================
``0``        the run finished
``1``        the model failed
``2``        the run could not be set up
``124``      the run ran out of time
``130``      the run was signalled (``SIGINT``)
``143``      the run was signalled (``SIGTERM``)
anything     ``unknown`` -- the envelope is kept, not thrown away
============  ==========================================================

The table itself lives in :mod:`harness.exit_codes`, because it is not
the claude CLI's table alone: the same code has to mean the same thing
to every adapter, or the layer above would have to know which CLI
produced the run before it could act on the verdict.

One event overrides one code. The CLI exits zero on a run whose answer
was a failure, so a ``result`` event that reports ``is_error`` turns a
zero exit code into a model error: reading the exit code alone would
call that run a success.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .contract import (
    HarnessCapabilities,
    HarnessEvent,
    HarnessInvocation,
    HarnessOutcome,
    SessionMode,
    TaskInvocation,
)
from .exit_codes import (
    EXIT_CODE_KINDS,
    SIGNAL_NAMES,
    classify_exit as _classify_exit,
    extract_result as _extract_result,
)

__all__ = [
    "ClaudeHarness",
    "PARSE_ERROR_TYPE",
    "EXIT_CODE_KINDS",
    "SIGNAL_NAMES",
    "parse_stream",
    "classify_exit",
    "extract_result",
]


#: The binary that is spawned. It is the first element of every claude
#: command line, and nothing is put in front of it: a wrapper here
#: would change what the command line *is*.
CLAUDE_BINARY = "claude"

#: The six elements every claude command line starts with, in order.
#:
#: ``--verbose`` and ``--output-format stream-json`` are what turn the
#: child's output into an event stream the parent can read; ``--verbose``
#: is what makes the CLI emit that stream at all.
#: ``--permission-mode bypassPermissions`` is what lets the dispatched
#: agent work without a human answering permission prompts -- the
#: dispatch is unattended by construction.
BASE_ARGV: Tuple[str, ...] = (
    CLAUDE_BINARY,
    "--verbose",
    "--output-format",
    "stream-json",
    "--permission-mode",
    "bypassPermissions",
)

#: The event type a line that could not be read is reported under. It
#: is deliberately not one of the CLI's own types: it says the *parser*
#: had something to say, and the CLI never said it.
PARSE_ERROR_TYPE = "parse_error"


# ---------------------------------------------------------------------------
# Reading the child's output
# ---------------------------------------------------------------------------


def parse_stream(lines: Iterable[Any]) -> Iterator[HarnessEvent]:
    """Turn the child's output lines into events, unchanged.

    The parser is an identity: a line that parses to a JSON object
    arrives as that object, in that order, with its keys as it wrote
    them. :attr:`HarnessEvent.raw` carries the payload verbatim; the
    other fields of the event are read out of that same payload, so
    nothing is invented and nothing is rewritten.

    A blank line carries no event and produces none. A line that does
    not parse -- the tail of a stream that was cut off mid-write --
    becomes a :data:`PARSE_ERROR_TYPE` event carrying the offending
    text and the line number it came from, because a dropped line is a
    failure that leaves no trace.

    Args:
        lines: The child's output, one line at a time. Bytes are
            decoded as UTF-8 with ``errors="replace"``, so a line that
            is not valid UTF-8 is read with replacement characters
            instead of ending the stream.

    Yields:
        One event per line that carries one, in the order of the lines.
    """
    for line_no, line in enumerate(lines, start=1):
        text = _as_text(line)
        if not text:
            continue

        try:
            payload = json.loads(text)
        except ValueError:
            # A truncated or otherwise broken line. It is reported
            # rather than dropped: the point of the diagnostic is that
            # the damage is visible.
            yield _parse_error(line_no, text)
            continue

        if not isinstance(payload, dict):
            # Valid JSON, but not an event: the payload of a claude
            # event is an object, and there is nothing here the layer
            # above could consume.
            yield _parse_error(line_no, text)
            continue

        yield HarnessEvent(
            type=_event_type(payload),
            raw=payload,
            text=_event_text(payload),
            session_id=_event_session_id(payload),
            usage=_event_usage(payload),
            is_error=bool(payload.get("is_error")),
        )


def extract_result(events: Sequence[HarnessEvent]) -> Optional[Dict[str, Any]]:
    """The payload of the run's terminal ``result`` event.

    The claude CLI ends a run with exactly one ``result`` event: the
    summary of what the run did, whether it failed, what it cost. This
    hands back that payload -- the envelope -- as it was written.

    The lookup itself is shared with the other adapters -- see
    :mod:`harness.exit_codes` -- so that the envelope of a run is
    found the same way whichever CLI produced it.

    Args:
        events: The events of one run, in the order they arrived.

    Returns:
        The result event's payload, or ``None`` when the stream never
        reached one.
    """
    return _extract_result(events)


def classify_exit(code: int, events: Sequence[HarnessEvent]) -> HarnessOutcome:
    """Say what an exit code meant.

    The verdict comes from :data:`EXIT_CODE_KINDS`: a code that is in
    the table names its own outcome, and a code that is not in it is
    :data:`~harness.contract.OUTCOME_UNKNOWN`. One event overrides one
    code -- a ``result`` event that reports ``is_error`` turns a zero
    exit code into a model error, because the CLI exits zero on a run
    whose answer was a failure.

    An unknown verdict keeps the envelope: the payload that came with
    the run is quoted in the message, because it is the only thing
    left that says what happened.

    The table is shared with the other adapters -- see
    :mod:`harness.exit_codes` -- so that the same exit code means the
    same thing whichever CLI produced the run.

    Args:
        code: The process's exit code.
        events: The events the run produced.

    Returns:
        The verdict on what the run meant.
    """
    envelope = extract_result(events)

    return _classify_exit(
        code,
        backend="claude",
        envelope=envelope,
        failed_result=bool(envelope is not None and envelope.get("is_error")),
    )


def _as_text(line: Any) -> str:
    """The text of one output line.

    Bytes are decoded as UTF-8 with ``errors="replace"``: a line that
    is not valid UTF-8 is read with replacement characters rather than
    ending the stream, because a stream that stops being read is a run
    whose remaining output is lost.

    Args:
        line: One line of the child's output.

    Returns:
        The line's text, without its surrounding whitespace.
    """
    if isinstance(line, (bytes, bytearray)):
        return bytes(line).decode("utf-8", errors="replace").strip()
    if isinstance(line, str):
        return line.strip()
    return str(line).strip()


def _parse_error(line_no: int, text: str) -> HarnessEvent:
    """The event that reports a line the parser could not read.

    Args:
        line_no: The line the text came from, counted from one.
        text: The text of the line.

    Returns:
        An event of type :data:`PARSE_ERROR_TYPE` carrying the line
        number and the offending text.
    """
    return HarnessEvent(
        type=PARSE_ERROR_TYPE,
        raw={"type": PARSE_ERROR_TYPE, "line_no": line_no, "raw": text},
        text=text,
        is_error=True,
    )


def _event_type(payload: Dict[str, Any]) -> str:
    """The event type the payload declares."""
    value = payload.get("type", "")
    return value if isinstance(value, str) else str(value)


def _event_text(payload: Dict[str, Any]) -> str:
    """The text the event carries.

    A ``result`` event carries its answer in ``result``; an
    ``assistant`` or ``user`` event carries it in the text blocks of
    its message content. Nothing else carries text.

    Args:
        payload: The event's payload.

    Returns:
        The text the event carries, or the empty string when it
        carries none.
    """
    if payload.get("type") == "result":
        result = payload.get("result")
        return result if isinstance(result, str) else ""

    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _event_session_id(payload: Dict[str, Any]) -> Optional[str]:
    """The conversation the event belongs to, when it names one."""
    session_id = payload.get("session_id")
    return session_id if isinstance(session_id, str) else None


def _event_usage(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The token and cost accounting the event carries, when it does."""
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else None


class ClaudeHarness:
    """The claude backend, as the harness layer sees it.

    Attributes:
        name: The backend this adapter runs. The registry keeps the
            name next to the adapter as well, but the adapter is the
            thing that answers for it.
    """

    #: The backend this adapter runs.
    name = "claude"

    def capabilities(self) -> HarnessCapabilities:
        """Describe what the claude CLI can be asked to do.

        Every flag this adapter can emit is one the claude CLI
        documents: ``--append-system-prompt``, ``--mcp-config``,
        ``--allowedTools``, ``--output-format stream-json`` (which
        carries token usage in its events), ``--session-id`` and
        ``--resume``.

        Returns:
            The capability set of the claude backend.
        """
        return HarnessCapabilities(
            append_system_prompt=True,
            mcp_injection=True,
            tool_whitelist=True,
            stream_events=True,
            usage=True,
            session_create=True,
            session_resume=True,
        )

    def build_invocation(self, req: TaskInvocation) -> HarnessInvocation:
        """Turn a request into the command line that runs it.

        The order of the flags is fixed: base, then the system prompt,
        then the model, then the MCP configuration, then the session,
        then the tool whitelist. Each optional flag is emitted only
        when the request has something to say for it -- a flag with
        nothing behind it is not emitted at all, because an empty
        value is a different command line, not a shorter one.

        Args:
            req: The request to run.

        Returns:
            The spawn description: the argv, an empty environment
            delta, and the working directory the request named.
        """
        argv: List[str] = list(BASE_ARGV)

        if req.system_instruction:
            argv += ["--append-system-prompt", req.system_instruction]

        if req.model:
            argv += ["--model", req.model]

        # A path to an MCP configuration file is the more specific of
        # the two ways the request can carry one, so it wins; the
        # inline configuration is the fallback.
        mcp_config = req.mcp_config_path or req.mcp_config
        if mcp_config:
            argv += ["--mcp-config", mcp_config]

        session_id = self._session_id(req)
        if session_id:
            if req.mode == SessionMode.RESUME:
                argv += ["--resume", session_id]
            else:
                argv += ["--session-id", session_id]

        allowed_tools = req.allowed_tools or ()
        if allowed_tools:
            argv += ["--allowedTools", ",".join(allowed_tools)]

        return HarnessInvocation(
            argv=tuple(argv),
            env_delta={},
            cwd=req.cwd,
            # The adapter is the author of the command line, so it is
            # the one that says whether the run has to produce a
            # stream. The runner only reads the fact.
            multi_turn=req.multi_turn,
        )

    def parse_stream(self, lines: Iterable[Any]) -> List[HarnessEvent]:
        """Turn the child's output lines into events, unchanged.

        The same parser :func:`parse_stream` is, gathered into a list
        because the protocol asks the adapter for the events it
        produced rather than for a stream of them.

        Args:
            lines: The child's output, one line at a time.

        Returns:
            One event per line that carries one, in the order of the
            lines.
        """
        return list(parse_stream(lines))

    def classify_exit(
        self, code: int, events: Sequence[HarnessEvent]
    ) -> HarnessOutcome:
        """Say what an exit code meant.

        The same classifier :func:`classify_exit` is.

        Args:
            code: The process's exit code.
            events: The events the run produced.

        Returns:
            The verdict on what the run meant.
        """
        return classify_exit(code, events)

    @staticmethod
    def _session_id(req: TaskInvocation) -> Optional[str]:
        """The conversation this request belongs to, if it names one.

        A request that names no conversation gets no session flag at
        all -- there is nothing to pin and nothing to re-open. A
        request that names a conversation gets the id the caller
        insists on, falling back to the id the request carries.

        Args:
            req: The request to read the conversation out of.

        Returns:
            The conversation id, or ``None`` when the request names
            none.
        """
        if req.session is None:
            return None
        return req.session.pinned_session_id or req.session.session_id
