"""The MiniMax Code (``mcode``) adapter for the harness layer.

Why this module exists
======================

The claude backend and the MiniMax Code backend are driven the same way
— build a command line, spawn it, read a stream of events out of it —
but their command lines are not the same command line. ``claude`` takes
``--session-id`` to create a conversation under a chosen id,
``--mcp-config`` to hand it MCP servers, ``--allowedTools`` to narrow
what it may touch, ``--permission-mode`` to say how it asks for
permission and ``--settings`` to load a settings file. ``mcode exec``
knows none of them: passing any one of them makes the command line
unrunnable, and the failure arrives as a usage error long after the
run was supposed to have started.

The same holds for the output. The layer above the harness reads one
vocabulary — claude's stream-json — and ``mcode exec --output-format
stream-json`` does not speak it. What mcode says was measured on the
CLI:

===========================  =========================================
mcode event                  what it carries
===========================  =========================================
``exec.started``             the run and the session
``session.started``          the session
``turn.started``             the turn
``item.started``             the beginning of an item
``item.completed``           a finished item — the agent's message
``turn.completed``           the model, the usage, the duration
``exec.completed``           the result: status, output, model, usage
===========================  =========================================

The usage block mcode reports names ``inputTokens``, ``outputTokens``,
``cacheReadTokens`` and ``totalTokens``. It has no cache *creation*
field, and it never will: the number was never counted.

This module owns the spelling of the mcode command line and the
translation of the mcode event stream, so that the runner above it
never has to know that ``mcode exec`` is not ``claude`` wearing a
different binary. It answers seven questions:

* :meth:`McodeHarness.capabilities` — what this backend can do.
* :meth:`McodeHarness.build_invocation` — the command line for a
  request.
* :meth:`McodeHarness._profile_env` — the environment the resolved
  MiniMax profile contributes.
* :meth:`McodeHarness.parse_stream` — the events the child's output
  turns into, in the vocabulary the layer above already speaks.
* :meth:`McodeHarness.classify_exit` — what an exit code meant.
* :meth:`McodeHarness.mcp_prepare` — the MCP servers a run gets, and
  the workspace that gets them.
* :class:`SessionMap` — which real mcode conversation the id the
  caller pinned stands for. It is re-exported from
  :mod:`harness.session_map`, where it lives: the mapping is used by
  the layer *above* the adapters, and the claude path must not have to
  import this module to get there.

The rules this module holds to
------------------------------

1. **No claude-only flag is ever passed.** The five flags claude
   accepts are not options here; they are mistakes. The mcode command
   line is built from what ``mcode exec`` documents and nothing else.
2. **A model names its provider.** ``mcode`` spells a model as
   ``<provider>/<model>``. A bare model name is refused here, loudly,
   rather than handed over — mcode would fall back to some default and
   the run would answer as a different model than the one that was
   asked for, which is the kind of mistake nobody notices until the
   bill arrives.
3. **A created session names no id; a resumed one names a real one.**
   mcode cannot be asked to create a conversation under a chosen id,
   so the id a caller pins is not passed at creation. Resuming is the
   other way round: it needs the id of a conversation that exists, and
   those ids look like ``mvs_...``. A resume that cannot name such an
   id is a configuration error, because the alternative — running
   without ``--session`` — would answer in a brand-new conversation
   while the caller believed it continued the old one.
4. **The environment says MiniMax, not Anthropic.** The profile
   contributes ``MCODE_PROFILE``, ``HOME`` and ``MINIMAX_DATA_DIR``
   and nothing else. An ``ANTHROPIC_*`` variable left in the
   environment would point the run back at the provider this backend
   is meant to have left.
5. **The event stream is translated, not reinterpreted.** A message
   becomes an ``assistant`` event, a finished run becomes exactly one
   ``result`` event, and the result is the last event of the stream.
   An event the adapter does not know is kept rather than dropped.
6. **A number that was not reported stays missing.** mcode does not
   report cache creation tokens; the canonical usage says ``None``
   where a number would be a guess, and marks the count partial. Zero
   is a number that was counted. ``None`` is a number that was not.
7. **A pinned id is a key, not a session id.** The caller pins an id
   it can compute again (a uuid5 of the plan and the task); the CLI
   owns the id of the conversation that was really created, and only
   the CLI knows it. The mapping between the two is written down
   before the observed id is handed back — so a crash can only orphan
   a conversation, never strand an id nobody can resolve — and a
   mapping that cannot be written fails the call rather than losing
   the id. A mapping that is not there is a miss, not another
   session.
8. **MCP servers arrive through the workspace and leave no trace.**
   ``mcode exec`` has no ``--mcp-config`` — that is one of the five
   claude-only flags — so the only channel it reads is the workspace
   root's ``.mcp.json``. The adapter writes that file for the run and
   puts the workspace back exactly as it found it: the original bytes
   and the original mode, or no file at all when there was none. The
   file never leaves the workspace, is never larger than a mebibyte,
   and is handed over as it was given — ``${VAR}`` stays ``${VAR}``.
   A run that is *killed* cannot put the workspace back, so the
   adapter writes down what it is about to overwrite — in a recovery
   journal outside the workspace, holding the user's own file and not
   the credential — and the next start of any product entry point
   replays it.
9. **An exit code means the same thing here as anywhere else.** The
   verdict on an exit comes out of the table every adapter reads (see
   :mod:`harness.exit_codes`), because the layer above branches on the
   verdict and does not know which CLI produced the run. A code nothing
   describes is ``unknown``, and the verdict keeps the envelope — the
   ``exec.completed`` payload the run ended with — rather than throwing
   away the only thing that says what happened.

The external process is stubbed at the function boundary (test
decision point 2): building an invocation spawns nothing and reads
nothing, so the whole of this module is observable from the command
line it produces and from the events a recorded stream turns into.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import stat
from collections.abc import Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from harness.contract import (
    HarnessCapabilities,
    HarnessConfigError,
    HarnessEvent,
    HarnessInvocation,
    HarnessOutcome,
    SessionMode,
    TaskInvocation,
)
from harness.exit_codes import classify_exit as _classify_exit
from harness import session_map as _session_map
from harness.session_map import (
    MCODE_SESSION_ID_RE,
    SessionIdNotResumable,
    SessionMap,
    _write_mapping,
)
from config_paths import STATE_DIR
from mcode_profiles import ProfileBinding, profile_env

__all__ = [
    "MCODE_BINARY",
    "MCODE_SESSION_PREFIX",
    "MCODE_SESSION_ID_RE",
    "CLAUDE_ONLY_FLAGS",
    "MCP_RESTORE_DIR_ENV",
    "MCP_RESTORE_EVENT",
    "PARSE_ERROR_TYPE",
    "McodeHarness",
    "SessionIdNotResumable",
    "SessionMap",
    "parse_stream",
    "recover_pending_mcp_restores",
]

logger = logging.getLogger(__name__)

#: A path, spelled the way a caller spells it.
PathLike = Union[str, Path]

#: The MiniMax Code binary, as it is spelled on ``PATH``.
MCODE_BINARY = "mcode"

#: The prefix every MiniMax Code session id carries. The CLI mints
#: them as ``mvs_`` followed by a uuid without its dashes.
MCODE_SESSION_PREFIX = "mvs_"

#: The shape of a real MiniMax Code session id — ``MCODE_SESSION_ID_RE``
#: — is imported from :mod:`harness.session_map`, where the mapping
#: that stores such ids lives. It is re-exported here so the name
#: keeps resolving where it always did.

#: The flags claude accepts and ``mcode exec`` does not. None of them
#: may appear on an mcode command line.
CLAUDE_ONLY_FLAGS: Tuple[str, ...] = (
    "--session-id",
    "--mcp-config",
    "--allowedTools",
    "--permission-mode",
    "--settings",
)

#: The event type a line that could not be read is reported under. It
#: is deliberately not one of mcode's own types: it says the *parser*
#: had something to say, and mcode never said it.
PARSE_ERROR_TYPE = "parse_error"

#: The mcode events that carry the agent's items. Both spellings of
#: the same item — the one that starts it and the one that finishes
#: it — become ``assistant`` events.
ITEM_EVENT_TYPES: Tuple[str, ...] = ("item.started", "item.completed")

#: The mcode events that open a run. They carry bookkeeping, not
#: content, and become ``system`` events.
LIFECYCLE_EVENT_TYPES: Tuple[str, ...] = (
    "exec.started",
    "session.started",
    "turn.started",
)

#: The status an ``exec.completed`` reports for a run that worked.
SUCCEEDED_STATUS = "succeeded"

#: The four fields of the canonical usage block, and nothing else.
#:
#: A count that carries all four is complete; a count that is missing
#: one of them is partial, whatever the missing one is. The list is
#: written down once so that the reader of the count and the writer of
#: it cannot drift apart.
USAGE_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: The file mcode reads MCP servers from. ``mcode exec`` has no
#: ``--mcp-config`` flag — that is one of the five claude-only flags —
#: so the workspace root's ``.mcp.json`` is the only channel there is.
_MCP_FILENAME = ".mcp.json"

#: The largest MCP configuration this adapter will write, and the
#: largest existing file it will snapshot: one mebibyte. A bigger one
#: is refused outright rather than truncated. A truncated configuration
#: is a configuration that means something else, and that is worse than
#: no configuration at all.
_SNAPSHOT_MAX_BYTES = 1024 * 1024

# ---------------------------------------------------------------------------
# The recovery journal — what a run that was killed leaves behind
# ---------------------------------------------------------------------------

#: The name of the recovery-journal directory, inside the checkout's
#: own runtime state directory.
#:
#: The journals are the framework's state, not the workspace's: a
#: workspace is somebody's working tree, and a file left in it is a
#: file the next ``git add -A`` picks up. The copy of the user's own
#: ``.mcp.json`` is exactly the kind of file that must never be
#: staged, so it is kept somewhere else entirely.
MCP_RESTORE_DIRNAME = "mcp-restore"

#: The environment variable that redirects the recovery-journal
#: directory. It exists for the same reason ``PDT_PLANS_DIR`` exists:
#: a test — or an operator with more than one checkout — needs the
#: journals somewhere of its own choosing.
MCP_RESTORE_DIR_ENV = "PDT_MCP_RESTORE_DIR"

#: The event type recorded when a pending restore was replayed.
#:
#: The same string is written to the log, so one grep finds both the
#: line and the event that says a killed run's workspace was put back.
MCP_RESTORE_EVENT = "mcp_restore_replayed"

#: The suffix of the journal file: the record of what was in the
#: workspace before the adapter overwrote it.
_MCP_JOURNAL_SUFFIX = ".json"

#: The suffix of the private copy of the file the adapter replaced.
_MCP_COPY_SUFFIX = ".bin"


# ---------------------------------------------------------------------------
# Reading the child's output
# ---------------------------------------------------------------------------


def parse_stream(lines: Iterable[Any]) -> Iterator[HarnessEvent]:
    """Turn the child's output lines into canonical events.

    The events come out in the vocabulary the layer above already
    speaks, so that a reader written against the claude stream reads
    an mcode run without knowing which CLI produced it:

    * an ``assistant`` event for every message the agent produced;
    * a ``system`` event for the bookkeeping of the run;
    * exactly one ``result`` event, and it is the last event of the
      stream.

    The result is the last event because it is the summary of the
    run: a reader takes the last ``result`` as the answer, so a
    second one would overwrite the first, and an event after it would
    look like part of an answer that was already given. The event
    that carries it (``exec.completed``) therefore waits until the
    stream has ended.

    A stream that never reaches ``exec.completed`` still ends in one
    result: it is synthesized from the ``turn.completed`` that closed
    the turn, so a run that was cut short after its turn finished
    still says what it spent.

    A line that cannot be read becomes a
    :data:`PARSE_ERROR_TYPE` event rather than nothing: a dropped
    line is a failure that leaves no trace.

    Args:
        lines: The child's output, one line at a time. Bytes are
            decoded as UTF-8 with ``errors="replace"``.

    Yields:
        One event per line that carries one, and — last — the run's
        result.
    """
    exec_result: Optional[Dict[str, Any]] = None
    turn: Optional[Dict[str, Any]] = None
    last_text = ""

    for line_no, line in enumerate(lines, start=1):
        text = _as_text(line)
        if not text:
            continue

        try:
            payload = json.loads(text)
        except ValueError:
            # A truncated or otherwise broken line.
            yield _parse_error(line_no, text)
            continue

        if not isinstance(payload, dict):
            # Valid JSON, but not an event.
            yield _parse_error(line_no, text)
            continue

        canonical = _to_canonical(payload)
        kind = canonical.get("type")

        if kind == "result":
            # Held back: the stream is not over yet, and the result
            # has to be its last event.
            exec_result = canonical
            continue

        if payload.get("type") == "turn.completed":
            turn = canonical

        if kind == "assistant":
            spoken = _canonical_text(canonical)
            if spoken:
                last_text = spoken

        yield _event(canonical)

    yield _event(_final_result(exec_result, turn, last_text))


def _to_canonical(event: Dict[str, Any]) -> Dict[str, Any]:
    """The canonical event one mcode event becomes.

    The translation is by event type, and the type decides:

    * ``item.started`` / ``item.completed`` become ``assistant``
      events carrying the item's text;
    * ``turn.completed`` becomes a ``system`` event carrying the model
      and the usage of the turn;
    * ``exec.started`` / ``session.started`` / ``turn.started`` become
      ``system`` events carrying the bookkeeping of the run;
    * ``exec.completed`` becomes the run's ``result``;
    * anything else becomes an ``assistant`` event that keeps the
      untouched payload under ``raw`` — an event this adapter does not
      know is kept, not dropped.

    Args:
        event: One mcode event, as the CLI wrote it.

    Returns:
        The canonical event, in the claude stream-json shape.
    """
    event_type = event.get("type")

    if event_type in ITEM_EVENT_TYPES:
        return _assistant_from_item(event)
    if event_type == "turn.completed":
        return _turn_event(event)
    if event_type == "exec.completed":
        return _result_from_exec(event)
    if event_type in LIFECYCLE_EVENT_TYPES:
        return _lifecycle_event(event, str(event_type))

    # An event this adapter does not know. It rides along in an
    # assistant event's ``raw``: nothing the CLI said is thrown away.
    return {"type": "assistant", "raw": event}


def _usage_from(usage: Any) -> Optional[Dict[str, Any]]:
    """The canonical usage block of an mcode usage payload.

    Four fields, and no fifth. mcode reports the input tokens, the
    output tokens and the tokens it read back out of the cache; it
    never reports the tokens it *created* in the cache, because that
    number was never counted. The fourth field therefore stays
    ``None``. It is not zero: zero is a count that came back, and a
    report that says zero cannot be told apart from a report of a run
    that really did not touch the cache.

    Args:
        usage: The usage payload the CLI reported, or ``None``.

    Returns:
        The canonical usage block, or ``None`` when the CLI reported
        no usage at all.
    """
    if not isinstance(usage, dict):
        return None

    return {
        "input_tokens": _count(usage.get("inputTokens")),
        "output_tokens": _count(usage.get("outputTokens")),
        "cache_read_input_tokens": _count(usage.get("cacheReadTokens")),
        "cache_creation_input_tokens": _count(usage.get("cacheCreationTokens")),
    }


def _assistant_from_item(event: Dict[str, Any]) -> Dict[str, Any]:
    """The ``assistant`` event one item event becomes.

    An agent message is text, and text is what the canonical shape
    carries. An item of any other kind — a tool call, say — has no
    place in that shape, so the untouched payload rides along under
    ``raw`` instead of being thrown away.

    Args:
        event: The ``item.started`` or ``item.completed`` event.

    Returns:
        The canonical ``assistant`` event.
    """
    item = event.get("item")
    if not isinstance(item, dict):
        item = {}

    text = _item_text(item)
    content: List[Dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})

    canonical: Dict[str, Any] = {
        "type": "assistant",
        "message": {"content": content},
    }
    if item.get("type") != "agent_message":
        canonical["raw"] = event
    return canonical


def _item_text(item: Dict[str, Any]) -> str:
    """The text one item carries.

    A finished item carries ``content``; an item that is still being
    written carries the same text as ``contentDelta``. Both are read,
    so the streamed text is not lost.

    Args:
        item: The item the event carried.

    Returns:
        The item's text, or the empty string when it carries none.
    """
    for key in ("content", "contentDelta"):
        value = item.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [
                block["text"]
                for block in value
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if parts:
                return "".join(parts)
    return ""


def _turn_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """The ``system`` event a finished turn becomes.

    Args:
        event: The ``turn.completed`` event.

    Returns:
        The canonical ``system`` event, carrying the turn's model and
        usage.
    """
    return {
        "type": "system",
        "subtype": "turn.completed",
        "session_id": _session_id_of(event),
        "model": _model_name(event.get("model")),
        "usage": _usage_from(event.get("usage")),
        "raw": event,
    }


def _lifecycle_event(event: Dict[str, Any], subtype: str) -> Dict[str, Any]:
    """The ``system`` event one lifecycle event becomes.

    Args:
        event: The lifecycle event.
        subtype: The mcode event type it came from.

    Returns:
        The canonical ``system`` event.
    """
    return {
        "type": "system",
        "subtype": subtype,
        "session_id": _session_id_of(event),
        "raw": event,
    }


def _result_from_exec(event: Dict[str, Any]) -> Dict[str, Any]:
    """The ``result`` event a finished run becomes.

    The status decides whether the run worked. mcode reports
    ``succeeded`` for a run that did; anything else — including a
    status that is not there at all — means it did not, because the
    claude CLI exits zero on a failed answer, so the exit code alone
    would call the run a success.

    The event the run ended with rides along under ``raw``, the way
    every other mcode event keeps the payload it was built from. It
    is what the verdict on an exit code nothing describes quotes: the
    exit code is the only thing left that says what happened, and the
    payload is the only thing that says what the run did.

    Args:
        event: The ``exec.completed`` event.

    Returns:
        The canonical ``result`` event.
    """
    result = event.get("result")
    if not isinstance(result, dict):
        result = {}

    usage = _usage_from(result.get("usage"))
    return {
        "type": "result",
        "session_id": _session_id_of(result) or _session_id_of(event),
        "result": _result_text(result),
        "is_error": result.get("status") != SUCCEEDED_STATUS,
        "model": _model_name(result.get("model")),
        "usage": usage,
        "usage_partial": _is_partial(usage),
        "raw": event,
    }


def _final_result(
    exec_result: Optional[Dict[str, Any]],
    turn: Optional[Dict[str, Any]],
    last_text: str,
) -> Dict[str, Any]:
    """The one result event the stream ends with.

    The run's own ``exec.completed`` is the result when there is one.
    A stream that ended without one — a run that was cut short, or a
    CLI that did not report it — gets a result synthesized from the
    turn that did finish, so the stream still ends in exactly one
    result that says what the run spent and what it answered.

    Args:
        exec_result: The canonical result of ``exec.completed``, or
            ``None`` when the stream never carried one.
        turn: The canonical event of ``turn.completed``, or ``None``.
        last_text: The text of the last message the agent produced.

    Returns:
        The canonical ``result`` event.
    """
    if exec_result is not None:
        result = dict(exec_result)
        if result.get("model") is None and turn is not None:
            result["model"] = turn.get("model")
        if result.get("usage") is None and turn is not None:
            result["usage"] = turn.get("usage")
        result["usage_partial"] = _is_partial(result.get("usage"))
        return result

    usage = turn.get("usage") if turn is not None else None
    return {
        "type": "result",
        "session_id": turn.get("session_id") if turn is not None else None,
        "result": last_text,
        "is_error": False,
        "model": turn.get("model") if turn is not None else None,
        "usage": usage,
        "usage_partial": _is_partial(usage),
    }


def _is_partial(usage: Optional[Dict[str, Any]]) -> bool:
    """Whether a usage count is a partial count.

    A count that is missing a field understates what the run cost,
    and a reader that believes it is complete will believe a number
    that is too low. So *every* field has to be there: a count with
    one field missing is as partial as a count with all four missing,
    and a count that was never reported at all is the most partial
    count there is.

    The rule is deliberately about the four canonical fields and not
    about one of them in particular. mcode never reports cache
    creation tokens, so a rule that only looked at that field would
    call a count complete while its output tokens or its cache read
    tokens were missing -- and a number that understates the cost is
    exactly what the marker exists to prevent.

    A field that was not reported stays ``None``. It is never turned
    into a zero: zero is a count that came back, and a report of zero
    cannot be told apart from a report of a run that really did not
    spend it.

    Args:
        usage: The canonical usage block, or ``None``.

    Returns:
        ``True`` when the count is missing a field — or when there is
        no count at all.
    """
    if not isinstance(usage, dict):
        return True
    for key in USAGE_FIELDS:
        if usage.get(key) is None:
            return True
    return False


def _last_result_event(
    events: Sequence[HarnessEvent],
) -> Optional[HarnessEvent]:
    """The run's terminal ``result`` event, when the stream carried one.

    Args:
        events: The events of one run, in the order they arrived.

    Returns:
        The last ``result`` event, or ``None`` when the stream never
        reached one.
    """
    for event in reversed(list(events)):
        if event.type == "result":
            return event
    return None


def _session_id_of(payload: Dict[str, Any]) -> Optional[str]:
    """The conversation a payload belongs to, when it names one."""
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _model_name(model: Any) -> Optional[str]:
    """The model as ``provider/model``.

    mcode names the model the way it takes it on the command line:
    the provider first, the model second.

    Args:
        model: The model block the CLI reported, or a model name.

    Returns:
        The model name, or ``None`` when the payload named none.
    """
    if isinstance(model, str):
        return model or None
    if not isinstance(model, dict):
        return None

    provider = model.get("providerId") or model.get("provider")
    model_id = model.get("modelId") or model.get("id")
    if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
        return f"{provider}/{model_id}"
    if isinstance(model_id, str) and model_id:
        return model_id
    return None


def _result_text(result: Dict[str, Any]) -> str:
    """The answer the run gave, as text."""
    for key in ("output", "error", "message"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _count(value: Any) -> Optional[int]:
    """A token count the CLI reported, or ``None`` when it reported none."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _canonical_text(canonical: Dict[str, Any]) -> str:
    """The text a canonical event carries.

    A ``result`` event carries its answer in ``result``; an
    ``assistant`` event carries it in the text blocks of its message
    content. Nothing else carries text.

    Args:
        canonical: The canonical event.

    Returns:
        The text the event carries, or the empty string.
    """
    if canonical.get("type") == "result":
        text = canonical.get("result")
        return text if isinstance(text, str) else ""

    message = canonical.get("message")
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


def _event(canonical: Dict[str, Any]) -> HarnessEvent:
    """The event one canonical event becomes.

    Args:
        canonical: The canonical event.

    Returns:
        The event, reading the fields the contract has out of the
        canonical payload.
    """
    session_id = canonical.get("session_id")
    usage = canonical.get("usage")
    return HarnessEvent(
        type=str(canonical.get("type", "")),
        raw=canonical,
        text=_canonical_text(canonical),
        session_id=session_id if isinstance(session_id, str) else None,
        usage=usage if isinstance(usage, dict) else None,
        usage_partial=bool(canonical.get("usage_partial")),
        is_error=bool(canonical.get("is_error")),
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


# ---------------------------------------------------------------------------
# MCP servers: the workspace's ``.mcp.json``
# ---------------------------------------------------------------------------


def _assert_scope(path: Path, workspace: Path) -> None:
    """Refuse a target that does not stay inside the workspace.

    Both sides are resolved first — symlinks included — and the
    resolved path has to be inside the resolved workspace. A ``..``
    that climbs out and a symlink that points out are the same escape,
    and both are refused before a single byte is written.

    Args:
        path: The file that is about to be written.
        workspace: The root the file has to stay inside.

    Raises:
        HarnessConfigError: The path resolves outside the workspace, or
            is the workspace root itself.
    """
    try:
        root = Path(workspace).resolve()
        target = Path(path).resolve()
    except OSError as exc:
        raise HarnessConfigError(
            f"mcode could not resolve {path!s} inside {workspace!s}: {exc}"
        ) from exc

    if target == root:
        raise HarnessConfigError(
            f"mcode refused to write {target!s}: it is the workspace root "
            f"itself"
        )

    try:
        target.relative_to(root)
    except ValueError:
        raise HarnessConfigError(
            f"mcode refused to write {target!s}: it resolves outside the "
            f"workspace {root!s}"
        ) from None


@dataclass(frozen=True)
class _McpSnapshot:
    """What was in the workspace before the adapter wrote its own file.

    Attributes:
        resolved: The file the write lands on — the workspace root's
            ``.mcp.json``, resolved. It differs from the path the
            adapter was given only when that path is a symlink; the
            symlink is never replaced, so the link the child reads
            keeps pointing where it pointed.
        payload: The bytes to write.
        existed: Whether there was a file to put back.
        original: The bytes that were there.
        mode: The mode that was there.
    """

    resolved: Path
    payload: bytes
    existed: bool
    original: bytes
    mode: int


def _mcp_payload(config: Mapping[str, Any]) -> bytes:
    """Serialize the configuration exactly as it was handed over.

    Nothing is expanded on the way: a ``${VAR}`` in the configuration
    stays a ``${VAR}`` in the file. Whatever the shell would have made
    of it is the child's business, not the adapter's.

    Args:
        config: The configuration, as a mapping.

    Returns:
        The bytes to write — one line of JSON.

    Raises:
        HarnessConfigError: The configuration is not JSON-serialisable.
    """
    try:
        text = json.dumps(
            config if isinstance(config, dict) else dict(config),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise HarnessConfigError(
            f"mcode MCP configuration is not JSON: {exc}"
        ) from exc
    return text.encode("utf-8")


def _write_mcp_config(snapshot: _McpSnapshot) -> None:
    """Write the configuration, privately.

    The file is written in place rather than moved into place: the
    file may be a symlink the workspace already had, and replacing the
    link would change what the workspace looked like before the run.

    The recovery journal comes first, and it is not optional. A write
    that cannot be undone is a write that may cost somebody their
    file: a run that is killed between this write and the restore
    leaves the workspace holding the adapter's payload — a credential
    — and the user's own file is gone. So the journal is written
    before the target is opened, and a journal that cannot be written
    aborts the write: there is no such thing as an overwrite this
    adapter cannot undo.

    Args:
        snapshot: What to write and where.

    Raises:
        HarnessConfigError: The file could not be written, or the
            recovery journal that makes the write undoable could not
            be written either.
    """
    _write_restore_journal(snapshot)

    try:
        handle = os.open(
            str(snapshot.resolved),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
    except OSError as exc:
        raise HarnessConfigError(
            f"mcode could not write {snapshot.resolved!s}: {exc}"
        ) from exc

    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(snapshot.payload)
            stream.flush()
            os.fsync(stream.fileno())
        # The mode is set after the fact as well: the mode a file is
        # created with is the mode it would have had without the
        # umask, and a configuration can carry a token.
        os.chmod(snapshot.resolved, 0o600)
    except OSError as exc:
        raise HarnessConfigError(
            f"mcode could not write {snapshot.resolved!s}: {exc}"
        ) from exc


def _restore_mcp_config(snapshot: _McpSnapshot) -> None:
    """Put the workspace back exactly as it was found.

    A file that was there is written back with the bytes and the mode
    it had; a file that was not there is removed. The write is in place
    for the same reason the write was: the workspace may have had a
    symlink there, and the symlink has to survive the run.

    A file that already says what it said and carries the mode it
    carried is not written at all: the restore then leaves a file the
    adapter never managed to change exactly as it found it.

    Args:
        snapshot: What to put back.
    """
    try:
        if snapshot.existed:
            try:
                current = snapshot.resolved.read_bytes()
            except OSError:
                current = None
            if current != snapshot.original:
                with open(snapshot.resolved, "wb") as stream:
                    stream.write(snapshot.original)
                    stream.flush()
                    os.fsync(stream.fileno())
            try:
                mode = stat.S_IMODE(snapshot.resolved.stat().st_mode)
            except OSError:
                mode = None
            if mode is not None and mode != snapshot.mode:
                os.chmod(snapshot.resolved, snapshot.mode)
        else:
            try:
                os.unlink(snapshot.resolved)
            except FileNotFoundError:
                pass
    except OSError as exc:
        # The outcome of the run matters more than this: a restore that
        # fails must not mask the exception the run raised.
        logger.error(
            "mcode could not restore %s: %s", snapshot.resolved, exc
        )


@contextmanager
def _mcp_context(snapshot: _McpSnapshot) -> Iterator[None]:
    """Hold the configuration in place for the length of the run.

    The file is put back on every way out of the block — a normal
    return, an exception, or a cancellation.

    The recovery journal is taken away only when the workspace really
    is back as it was found. A restore that did not put the file back
    leaves the journal in place, because the next start still owes the
    user that file.

    Args:
        snapshot: What to write and what to put back.

    Yields:
        Nothing. The block is the length of the run.
    """
    try:
        _write_mcp_config(snapshot)
        yield
    finally:
        _restore_mcp_config(snapshot)
        _discard_journal_if_settled(snapshot)


# ---------------------------------------------------------------------------
# The recovery journal
# ---------------------------------------------------------------------------
#
# The adapter writes the workspace's ``.mcp.json`` for the length of a
# run and puts the workspace back afterwards. Every way out of a run
# that the framework controls ends in that restore — a normal return,
# an exception, a SIGTERM turned into an interruption.
#
# One way is not the framework's to control: a SIGKILL, or a crash, or
# the machine going away. The process is gone between the write and
# the restore, and nothing this code can do in that process will ever
# run again. What is left behind is a workspace holding the adapter's
# payload — a credential — in place of the user's own file, and the
# user's own file is gone.
#
# The journal is the answer to that. Before the adapter opens the
# target, it writes down what the target held: the path, whether it
# existed, the bytes themselves in a private copy beside the record,
# their hash, the mode, the process that owns the write, and when it
# happened. A later start reads the journals it finds, puts the
# workspaces back, and takes the journals away.
#
# Two rules hold the whole thing together:
#
# * **The journal is written before the target is opened.** A journal
#   that cannot be written aborts the write. There is no overwrite
#   this adapter cannot undo.
# * **The journal never carries the credential.** It names the file
#   and what was in it; the adapter's own payload is not written down
#   anywhere. The copy holds the *user's* file — the thing that has
#   to be given back — and it is written private, outside the
#   workspace, where no ``git add -A`` can reach it.


def _mcp_restore_dir() -> Path:
    """Where the recovery journals and their copies live.

    The journals are the framework's own runtime state, so they live
    beside the rest of it — in the checkout's state directory, which
    is gitignored — and never in the workspace the run happens in.

    Args:
        None.

    Returns:
        The directory the journals live in. It need not exist yet.
    """
    override = os.environ.get(MCP_RESTORE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return STATE_DIR / MCP_RESTORE_DIRNAME


def _journal_paths(resolved: Path) -> Tuple[Path, Path]:
    """The journal and the copy that belong to one workspace file.

    The name is derived from the path the adapter writes to, so one
    workspace has one journal — and two workspaces never share one.

    Args:
        resolved: The file the adapter writes to.

    Returns:
        The journal's path and the copy's path.
    """
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:32]
    directory = _mcp_restore_dir()
    return (
        directory / f"{key}{_MCP_JOURNAL_SUFFIX}",
        directory / f"{key}{_MCP_COPY_SUFFIX}",
    )


def _fsync_dir(directory: Path) -> None:
    """Flush a directory so a rename inside it survives a crash.

    Args:
        directory: The directory to flush.

    Returns:
        Nothing. A directory that cannot be flushed is not a reason to
        fail the write that was already made.
    """
    try:
        handle = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically, and privately.

    The write goes to a temporary file beside the target and is moved
    into place with ``os.replace``, so a reader never sees half a
    journal and a crash never leaves a torn one. The temporary file is
    created with the mode the file is to keep — a journal names the
    user's file and the copy holds its bytes, and neither is a file to
    share.

    Args:
        path: Where to write.
        data: What to write.
        mode: The mode the file is to carry.

    Raises:
        OSError: The file could not be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        handle = os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode
        )
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _read_journal(path: Path) -> Optional[Dict[str, Any]]:
    """Read one journal.

    Args:
        path: The journal's path.

    Returns:
        The record, or ``None`` when the file cannot be read or does
        not hold a record. A journal that cannot be read is kept: the
        copy beside it may still hold the user's file.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def _journal_bytes(record: Dict[str, Any]) -> bytes:
    """The bytes one journal is written as."""
    return json.dumps(
        record, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")


def _write_restore_journal(snapshot: _McpSnapshot) -> None:
    """Record what was in the workspace before the adapter overwrote it.

    The record names the file, says whether it was there, and says
    what was in it — the hash of the original bytes, the mode it
    carried, the process that owns the write, and when it happened.
    The bytes themselves are kept in a private copy beside the record,
    not in the record: the journal is read by whoever starts the next
    process, and the payload the adapter is about to write — which is
    where the credential is — is not written down anywhere at all.

    A journal that is already there for this file is not overwritten
    with a worse answer. That is what a run that was killed looks
    like: the adapter's own payload is sitting where the user's file
    was, and the journal beside it holds the file the user actually
    had. The recorded original is the truer of the two, so it is
    kept, and only the ownership moves to this process.

    Args:
        snapshot: What is about to be written over.

    Raises:
        HarnessConfigError: The journal could not be written. The
            write it was written for does not happen: an overwrite
            that cannot be undone is refused.
    """
    journal_path, copy_path = _journal_paths(snapshot.resolved)
    directory = journal_path.parent
    current_sha = hashlib.sha256(snapshot.original).hexdigest()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise HarnessConfigError(
            f"mcode could not open the restore journal directory "
            f"{directory!s}: {exc}"
        ) from exc

    existing = _read_journal(journal_path)
    if existing is not None and existing.get("original_sha256") != current_sha:
        # The file on disk is not what the journal recorded — the
        # adapter's payload is sitting where the user's file was. The
        # recorded original is kept; only the owner changes.
        record = dict(existing)
        record["pid"] = os.getpid()
        record["created_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _atomic_write(journal_path, _journal_bytes(record))
        except OSError as exc:
            raise HarnessConfigError(
                f"mcode could not take over the restore journal for "
                f"{snapshot.resolved!s}: {exc}"
            ) from exc
        return

    try:
        if snapshot.existed:
            _atomic_write(copy_path, snapshot.original)
        record = {
            "version": 1,
            "target": str(snapshot.resolved),
            "existed": bool(snapshot.existed),
            "original_sha256": current_sha,
            "mode": int(snapshot.mode),
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "copy": copy_path.name if snapshot.existed else None,
        }
        _atomic_write(journal_path, _journal_bytes(record))
    except OSError as exc:
        raise HarnessConfigError(
            f"mcode could not record the restore journal for "
            f"{snapshot.resolved!s}: {exc}"
        ) from exc


def _matches_record(target: Path, record: Dict[str, Any]) -> bool:
    """Whether ``target`` holds what the journal says it should hold.

    Args:
        target: The file the journal is about.
        record: The journal's record.

    Returns:
        ``True`` when the file is in the state the journal recorded —
        the bytes and the mode, or the absence of the file.
    """
    if not record.get("existed"):
        return not target.exists()
    try:
        data = target.read_bytes()
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return False
    if hashlib.sha256(data).hexdigest() != record.get("original_sha256"):
        return False
    return mode == int(record.get("mode", 0o600))


def _discard_journal(journal_path: Path, copy_path: Optional[Path]) -> None:
    """Take the journal and its copy away: the restore is done.

    Args:
        journal_path: The journal's path.
        copy_path: The copy's path, or ``None`` when there was no copy.

    Returns:
        Nothing. A file that cannot be removed is logged and left: it
        is stale, not dangerous, and the next replay will simply find
        nothing to do.
    """
    for path in (journal_path, copy_path):
        if path is None:
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("mcode could not remove %s: %s", path, exc)


def _discard_journal_if_settled(snapshot: _McpSnapshot) -> None:
    """Take the journal away when the workspace is back as it was found.

    A restore that did not put the file back leaves the journal where
    it is: the next start still owes the user that file, and the copy
    beside the journal is still the only place it exists.

    Args:
        snapshot: What was written and what was put back.

    Returns:
        Nothing.
    """
    journal_path, copy_path = _journal_paths(snapshot.resolved)
    record = _read_journal(journal_path)
    if record is None:
        return
    if _matches_record(snapshot.resolved, record):
        _discard_journal(journal_path, copy_path)


def _process_alive(pid: Any) -> bool:
    """Whether the process that wrote a journal is still running.

    A journal whose owner is still alive belongs to a run that is still
    going: the workspace is being used right now, and putting it back
    under it would take the file away from a live run.

    Args:
        pid: The pid the journal recorded.

    Returns:
        ``True`` when that process is still there.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It is there; it simply is not ours to signal.
        return True
    except OSError:
        return False
    return True


def _replay_one(journal_path: Path) -> Optional[HarnessEvent]:
    """Replay one journal, and report what happened.

    Args:
        journal_path: The journal's path.

    Returns:
        The event that says the workspace was put back, or ``None``
        when there was nothing to do — the journal belongs to a live
        run, it could not be read, or the workspace could not be put
        back. In every one of those cases the journal stays where it
        is, so the next start tries again.
    """
    record = _read_journal(journal_path)
    if record is None:
        logger.error(
            "%s: the restore journal %s could not be read; it stays in "
            "place", MCP_RESTORE_EVENT, journal_path,
        )
        return None

    owner = record.get("pid")
    if _process_alive(owner):
        logger.info(
            "%s: skipped %s — the run that wrote the journal (pid %s) is "
            "still alive", MCP_RESTORE_EVENT, record.get("target"), owner,
        )
        return None

    target_text = str(record.get("target") or "")
    if not target_text:
        logger.error(
            "%s: the restore journal %s names no file", MCP_RESTORE_EVENT,
            journal_path,
        )
        return None
    target = Path(target_text)

    copy_name = record.get("copy")
    copy_path: Optional[Path] = None
    if copy_name:
        copy_path = journal_path.parent / str(copy_name)

    if not target.parent.is_dir():
        # The workspace this journal belongs to is gone — the task's
        # directory was removed after the run was killed. There is
        # nothing left to put anything back into, and a journal that
        # stays behind is retried on every start and says the same
        # thing every time. So it goes away with the copy beside it.
        logger.info(
            "%s: the workspace of %s is gone; the stale journal is taken "
            "away", MCP_RESTORE_EVENT, target,
        )
        _discard_journal(journal_path, copy_path)
        return None

    existed = bool(record.get("existed"))
    mode = int(record.get("mode", 0o600))
    original = b""
    if existed:
        if copy_path is None:
            logger.error(
                "%s: the restore journal for %s names no copy of the "
                "original file", MCP_RESTORE_EVENT, target,
            )
            return None
        try:
            original = copy_path.read_bytes()
        except OSError as exc:
            logger.error(
                "%s: could not read the copy of %s: %s", MCP_RESTORE_EVENT,
                target, exc,
            )
            return None
        if hashlib.sha256(original).hexdigest() != record.get(
            "original_sha256"
        ):
            logger.error(
                "%s: the copy of %s does not match the journal; it is not "
                "used", MCP_RESTORE_EVENT, target,
            )
            return None

    # The restore itself is the one this module has always done: the
    # same bytes, the same mode, and a symlink left as a symlink.
    _restore_mcp_config(
        _McpSnapshot(
            resolved=target,
            payload=b"",
            existed=existed,
            original=original,
            mode=mode,
        )
    )

    if not _matches_record(target, record):
        logger.error(
            "%s: could not put %s back; the journal stays in place",
            MCP_RESTORE_EVENT, target,
        )
        return None

    _discard_journal(journal_path, copy_path)
    logger.info(
        "%s: %s was put back (existed=%s mode=%s)", MCP_RESTORE_EVENT,
        target, existed, mode,
    )
    return HarnessEvent(
        type=MCP_RESTORE_EVENT,
        raw={
            "event": MCP_RESTORE_EVENT,
            "target": str(target),
            "existed": existed,
            "mode": mode,
        },
        text=f"the workspace {target} was put back",
    )


def recover_pending_mcp_restores() -> List[HarnessEvent]:
    """Put back the workspaces a run that was killed left dirty.

    A run that ended cleanly put the workspace back itself. A run that
    was killed could not: the process is gone between the write and
    the restore, and the workspace keeps the adapter's payload — a
    credential — where the user's own file was.

    This is what puts it back. Every product entry point calls it
    before it starts anything of its own — the CLI, the server's
    start-up, and the construction of this adapter — so the workspace
    of a killed run is repaired by the next start of anything.

    A journal whose owner is still alive is left alone: that run is
    still going, and the workspace is in use.

    Returns:
        One event per workspace that was put back. The event's type is
        :data:`MCP_RESTORE_EVENT`, which is also what the log says, so
        one grep finds the evidence of either.
    """
    directory = _mcp_restore_dir()
    try:
        journals = sorted(directory.glob(f"*{_MCP_JOURNAL_SUFFIX}"))
    except OSError:
        return []

    events: List[HarnessEvent] = []
    for journal_path in journals:
        event = _replay_one(journal_path)
        if event is not None:
            events.append(event)
    return events


class McodeHarness:
    """The MiniMax Code backend.

    Attributes:
        binding: The MiniMax profile this backend runs under, or
            ``None`` to run on the default profile. A profile is
            resolved by :mod:`mcode_profiles` before the adapter is
            built — this adapter never enumerates profiles itself, so
            it can be constructed without touching the machine.
    """

    def __init__(self, binding: Optional[ProfileBinding] = None) -> None:
        self._binding = binding
        # A workspace a run that was killed left holding the adapter's
        # MCP configuration is put back here, before this adapter runs
        # anything: the run that wrote the file is gone, and this is
        # the first moment anybody is looking.
        self._recovered_restores: Tuple[HarnessEvent, ...] = tuple(
            recover_pending_mcp_restores()
        )

    @property
    def recovered_restores(self) -> Tuple[HarnessEvent, ...]:
        """The restores this adapter replayed when it was built.

        One event per workspace that was put back, each of the type
        :data:`MCP_RESTORE_EVENT`.
        """
        return self._recovered_restores

    def capabilities(self) -> HarnessCapabilities:
        """Describe what the MiniMax Code backend supports.

        Everything except the tool whitelist is on: the CLI appends a
        system prompt, reports a stream of events and their usage,
        and can create and resume conversations.
        """
        return HarnessCapabilities(
            append_system_prompt=True,
            mcp_injection=True,
            tool_whitelist=False,
            stream_events=True,
            usage=True,
            session_create=True,
            session_resume=True,
        )

    def _profile_env(self, binding: Optional[ProfileBinding]) -> Dict[str, str]:
        """The environment a resolved profile contributes.

        The MiniMax launcher reads exactly three variables:
        ``MCODE_PROFILE`` selects the account, ``HOME`` isolates the
        account's files and ``MINIMAX_DATA_DIR`` isolates the account's
        data directory inside that home. Nothing else is contributed —
        in particular nothing Anthropic, which would point the run
        back at the claude provider.

        Args:
            binding: The resolved profile, or ``None`` when nothing was
                mapped.

        Returns:
            The environment delta, empty when there is no binding.
        """
        return profile_env(binding)

    def build_invocation(self, req: TaskInvocation) -> HarnessInvocation:
        """Turn a request into the command line that runs it.

        The prompt is handed over on standard input (``--input -``),
        so it never appears in the command line and never lands in a
        process listing.

        A request that carries MCP servers gets a prepare hook as
        well: ``mcode exec`` cannot be handed a configuration file on
        the command line, so the configuration travels through the
        workspace instead.

        Args:
            req: The request to run.

        Returns:
            The invocation: the command line, the environment the
            profile contributes, the working directory, and the hook
            that holds the MCP configuration in place while the child
            runs.

        Raises:
            HarnessConfigError: The request cannot be run as asked —
                a model that does not name its provider, a resume
                that names no real MiniMax Code session, or an MCP
                configuration that is not a JSON object.
        """
        argv: List[str] = [
            MCODE_BINARY,
            "exec",
            # Events, not a final answer: the runner above this layer
            # reads a stream and decides what it meant.
            "--output-format",
            "stream-json",
            # There is nobody to ask: an exec run cannot answer a
            # permission prompt, so it is told not to ask.
            "--permission",
            "full",
            # The coding prompt mode is the one that behaves like the
            # coding agent this backend stands in for.
            "--prompt-mode",
            "coding",
            "--input",
            "-",
        ]

        if req.cwd:
            argv += ["--cwd", req.cwd]

        if req.system_instruction:
            argv += ["--append-system-prompt", req.system_instruction]

        if req.model:
            argv += ["--model", self._model(req.model)]

        session_id = self._session_id(req)
        if session_id:
            argv += ["--session", session_id]

        return HarnessInvocation(
            argv=tuple(argv),
            env_delta=self._profile_env(self._binding),
            cwd=req.cwd,
            prepare=self._prepare(req),
            # The adapter is the author of the command line, so it is
            # the one that says whether the run has to produce a
            # stream. The runner only reads the fact.
            multi_turn=req.multi_turn,
        )

    def _prepare(
        self, req: TaskInvocation
    ) -> Optional[Callable[[], AbstractContextManager[None]]]:
        """The hook that gives the run its MCP servers, if it asked for any.

        ``mcode exec`` has no ``--mcp-config``, so the configuration
        cannot travel on the command line: it travels through the
        workspace. The hook writes the workspace's ``.mcp.json`` for
        the length of the run and puts the workspace back afterwards.

        The hook is built here and entered by the runner, so nothing is
        written — and nothing is read — until the child is about to be
        spawned.

        Args:
            req: The request to run.

        Returns:
            The hook, or ``None`` when the request carries no MCP
            configuration — a run without MCP servers has nothing to
            prepare.
        """
        raw = req.mcp_config_path or req.mcp_config
        if not raw:
            return None

        workspace = Path(req.cwd) if req.cwd else Path.cwd()
        return functools.partial(self._mcp_prepare_for, workspace, raw)

    def _mcp_prepare_for(
        self, workspace: Path, raw: Any
    ) -> AbstractContextManager[None]:
        """The hook, with the configuration read at the moment it is needed.

        Args:
            workspace: The workspace the run happens in.
            raw: The configuration, as the request carries it.

        Returns:
            The context manager that holds the configuration in place.

        Raises:
            HarnessConfigError: The configuration is not a JSON object,
                or names a file that cannot be read.
        """
        return self.mcp_prepare(workspace, self._mcp_config(raw))

    def _mcp_config(self, raw: Any) -> Dict[str, Any]:
        """The MCP configuration, parsed.

        A configuration reaches the harness in either of the two forms
        the claude side accepts: a JSON object in text form, or the
        path of a file that holds one. Both are read here, so the
        adapter does not have to guess which one it was handed.

        Args:
            raw: The configuration, as the request carries it.

        Returns:
            The configuration, as a mapping.

        Raises:
            HarnessConfigError: The configuration is not a JSON object,
                or names a file that cannot be read.
        """
        parsed: Any = None

        if isinstance(raw, Mapping):
            parsed = dict(raw)
        elif isinstance(raw, str):
            text = raw.strip()
            if text.startswith(("{", "[")):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
            if parsed is None:
                path = Path(text)
                try:
                    content = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise HarnessConfigError(
                        f"mcode MCP configuration is neither a JSON object "
                        f"nor a readable file ({raw!r}): {exc}"
                    ) from exc
                try:
                    parsed = json.loads(content)
                except ValueError as exc:
                    raise HarnessConfigError(
                        f"mcode MCP configuration file {path!s} is not JSON: "
                        f"{exc}"
                    ) from exc
        else:
            raise HarnessConfigError(
                f"mcode MCP configuration must be a JSON object or a path, "
                f"got {type(raw).__name__}"
            )

        if not isinstance(parsed, dict):
            raise HarnessConfigError(
                f"mcode MCP configuration must be a JSON object, got "
                f"{type(parsed).__name__}"
            )
        return parsed

    def _model(self, model: str) -> str:
        """The model as ``mcode`` spells it: ``<provider>/<model>``.

        Args:
            model: The model the request asked for.

        Returns:
            The model, unchanged.

        Raises:
            HarnessConfigError: The model does not name a provider.
                The message names the form that is needed, because the
                caller is the only one who can supply it.
        """
        if "/" not in model:
            raise HarnessConfigError(
                f"mcode model {model!r} does not name a provider; mcode "
                f"needs the provider/model form, e.g. "
                f"'minimax/MiniMax-M3.1-Flash'"
            )
        return model

    def parse_stream(self, lines: Iterable[Any]) -> Iterator[HarnessEvent]:
        """Turn the child's output lines into canonical events.

        The mcode stream is translated into the vocabulary the layer
        above already speaks — ``assistant`` for the agent's messages,
        ``system`` for the bookkeeping of the run, and exactly one
        ``result``, last, for the summary of the run. See
        :func:`parse_stream` for what the translation does with the
        parts of the stream that have no claude counterpart.

        Args:
            lines: The child's output, one line at a time.

        Returns:
            An iterator over the canonical events.
        """
        return parse_stream(lines)

    def classify_exit(
        self, code: int, events: Sequence[HarnessEvent]
    ) -> HarnessOutcome:
        """Say what an exit code meant.

        The table is the one every adapter reads -- see
        :mod:`harness.exit_codes` -- so that the same exit code means
        the same thing whichever CLI produced the run. The layer above
        branches on the verdict and does not know which CLI that was.

        One event overrides one code. ``mcode exec`` exits zero on a
        run whose answer was a failure, so the ``exec.completed`` event
        that ended the run has the last word on a zero exit: a result
        whose status is not ``succeeded`` is a model error.

        A code nothing describes is ``unknown``, and the verdict keeps
        the envelope: the payload of the ``exec.completed`` event the
        run ended with is quoted in the message, because it is the only
        thing left that says what happened.

        Args:
            code: The process's exit code.
            events: The events the run produced.

        Returns:
            The verdict on what the run meant.
        """
        result = _last_result_event(events)
        envelope = result.raw if result is not None else None

        failed = bool(result is not None and result.is_error)
        if not failed and isinstance(envelope, dict):
            failed = bool(envelope.get("is_error"))

        return _classify_exit(
            code,
            backend="mcode",
            envelope=envelope,
            failed_result=failed,
        )

    def _session_id(self, req: TaskInvocation) -> Optional[str]:
        """The session a resumed run continues, if there is one.

        A created session names nothing: ``mcode`` cannot be asked to
        create a conversation under a chosen id, so an id pinned by the
        caller is not one mcode can honour.

        A resumed session names the id of a conversation that exists.
        Those ids are minted by the CLI and carry the ``mvs_`` prefix,
        which is how the real one is told apart from the id a caller
        pinned.

        Args:
            req: The request to run.

        Returns:
            The session id to resume, or ``None`` when the request does
            not resume one.

        Raises:
            SessionIdNotResumable: A resume that names no real MiniMax
                Code session. Running without ``--session`` instead
                would answer in a new conversation, which is not what
                the caller asked for.
        """
        if req.mode is not SessionMode.RESUME:
            return None

        session = req.session
        candidates = ()
        if session is not None:
            candidates = (session.session_id, session.pinned_session_id)

        for candidate in candidates:
            if candidate and candidate.startswith(MCODE_SESSION_PREFIX):
                return candidate

        named = [candidate for candidate in candidates if candidate]
        raise SessionIdNotResumable(
            f"mcode resume needs a real session id (one starting with "
            f"{MCODE_SESSION_PREFIX!r}), got {named or 'no session id'}"
        )

    def mcp_prepare(
        self, workspace: Path, config: Mapping[str, Any]
    ) -> AbstractContextManager[None]:
        """Prepare the workspace's ``.mcp.json`` for a run.

        ``mcode exec`` has no ``--mcp-config`` — the workspace root's
        ``.mcp.json`` is the only place it reads MCP servers from — so
        the adapter writes that file itself and takes it back when the
        run is over.

        The workspace is put back exactly as it was found: a file that
        was there comes back with its bytes and its mode, a file that
        was not there is gone again. The file therefore exists for the
        length of the run only — it is gone before the run returns, and
        the framework's own checkpoint commit (``git add -A``) happens
        after that — so it cannot end up in a commit.

        Everything that could go wrong is refused *before* anything is
        written: a target that leaves the workspace, a target inside
        the account's own data directory, and a configuration over
        :data:`_SNAPSHOT_MAX_BYTES`. A configuration that is too large
        is refused rather than truncated.

        Args:
            workspace: The workspace the run happens in.
            config: The MCP configuration, as a mapping. It is handed
                over as it was given: a ``${VAR}`` in it stays a
                ``${VAR}``.

        Returns:
            The context manager that holds the configuration in place.
            Entering it writes the file; leaving it puts the workspace
            back.

        Raises:
            HarnessConfigError: The configuration cannot be written
                where it was asked for — it escapes the workspace, it
                is in the account's data directory, it is over the
                size limit, or it is not JSON at all.
        """
        # A configuration that arrives as text is read as one; the
        # request carries it in either shape.
        if isinstance(config, str):
            config = self._mcp_config(config)
        return _mcp_context(self._plan_mcp(workspace, config))

    def _plan_mcp(
        self, workspace: Path, config: Mapping[str, Any]
    ) -> _McpSnapshot:
        """Work out what to write into the workspace, and what to put back.

        Args:
            workspace: The workspace the run happens in.
            config: The MCP configuration, as a mapping.

        Returns:
            The snapshot: the file, the bytes, and what was there.

        Raises:
            HarnessConfigError: The configuration cannot be written
                where it was asked for.
        """
        root = Path(workspace)
        target = root / _MCP_FILENAME

        # The escape checks come first: a target that leaves the
        # workspace, or that sits in the account's own data directory,
        # is refused before anything is read and before anything is
        # written.
        _assert_scope(target, root)
        self._assert_not_data_dir(target)

        payload = _mcp_payload(config)
        if len(payload) > _SNAPSHOT_MAX_BYTES:
            raise HarnessConfigError(
                f"mcode MCP configuration is {len(payload)} bytes, over the "
                f"{_SNAPSHOT_MAX_BYTES} byte limit; it is refused rather "
                f"than truncated"
            )

        resolved = target.resolve()
        existed = resolved.exists()
        original = b""
        mode = 0o600
        if existed:
            try:
                original = resolved.read_bytes()
                mode = stat.S_IMODE(resolved.stat().st_mode)
            except OSError as exc:
                raise HarnessConfigError(
                    f"mcode could not read {resolved!s}: {exc}"
                ) from exc
            if len(original) > _SNAPSHOT_MAX_BYTES:
                raise HarnessConfigError(
                    f"mcode refused to overwrite {resolved!s}: it is "
                    f"{len(original)} bytes, over the {_SNAPSHOT_MAX_BYTES} "
                    f"byte limit, and truncating it would change what the "
                    f"file says"
                )

        return _McpSnapshot(
            resolved=resolved,
            payload=payload,
            existed=existed,
            original=original,
            mode=mode,
        )

    def _protected_data_dirs(self) -> List[Path]:
        """The account directories that are never written to.

        mcode keeps the account's own files under ``<home>/.minimax``:
        the profile's persistent MCP registry (``<dataDir>/mcp.json``)
        among them. A workspace that *is* that directory is a
        configuration mistake, and writing there would overwrite the
        user's own MCP servers.

        Returns:
            The directories, resolved.
        """
        candidates: List[Path] = []

        home = self._binding.home if self._binding is not None else ""
        if home:
            candidates.append(Path(home) / ".minimax")

        env_home = os.environ.get("HOME")
        if env_home:
            candidates.append(Path(env_home) / ".minimax")

        env_data = os.environ.get("MINIMAX_DATA_DIR")
        if env_data:
            candidates.append(Path(env_data))

        protected: List[Path] = []
        for candidate in candidates:
            try:
                protected.append(candidate.resolve())
            except OSError:
                continue
        return protected

    def _assert_not_data_dir(self, target: Path) -> None:
        """Refuse a target that is the account's own data directory.

        Args:
            target: The file that is about to be written.

        Raises:
            HarnessConfigError: The target is in the account's data
                directory.
        """
        try:
            resolved = target.resolve()
        except OSError:
            return

        for data_dir in self._protected_data_dirs():
            if resolved.parent == data_dir:
                raise HarnessConfigError(
                    f"mcode refused to write {resolved!s}: it is in the "
                    f"account data directory {data_dir!s}, where mcode "
                    f"keeps the account's own MCP registry "
                    f"(<dataDir>/mcp.json)"
                )


# ---------------------------------------------------------------------------
# The write the mapping goes through
# ---------------------------------------------------------------------------

# The mapping itself lives in :mod:`harness.session_map`, which imports
# no adapter — the claude path must be able to reach it without this
# module being loaded. The *write* is installed here instead: it goes
# out under this module's name, and the name is resolved when the write
# happens rather than when this module is imported, so a caller that
# replaces ``mcode._write_mapping`` sees its own function called.
#
# There is one implementation of the write itself — the neutral module's
# — and this is the alias for it.
_session_map.SessionMap._write = (
    lambda self, mapping: _write_mapping(self.path, mapping)
)
