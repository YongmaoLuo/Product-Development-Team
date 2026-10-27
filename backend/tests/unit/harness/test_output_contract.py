"""TDD spec for the harness output contract — the OC-01..12 table.

Why this file exists
====================

The two adapters turn a child's output into events, and the layer above
them reads those events as if one CLI had written them. That layer
makes three assumptions, and every one of them fails *silently*:

* **a number that was not reported is not zero.** A usage field the CLI
  never filled in, read as ``0``, cannot be told apart from a run that
  really did not touch the cache — and it is wrong in the direction that
  costs money;
* **an exit code nothing describes is not a success.** A verdict of
  ``ok`` for a code no table maps is a failed run reported as one that
  worked;
* **a line that could not be read is not a line that may be dropped.** A
  truncated tail is the fingerprint of a run that was cut off; losing it
  leaves a failure with no trace.

None of the three has a symptom of its own. They are only visible in
the *shape* of the output, so they are pinned here as shapes: twelve
output forms, each applied to both adapters, each with what the
contract says about it. The table is the regression suite — a change in
either adapter that alters one of these shapes fails a row of it.

The twelve shapes
=================

========  ===========================================================
Case      The output shape
========  ===========================================================
``OC-01``  a recorded multi-turn run, with tools
``OC-02``  a recorded single-turn run
``OC-03``  a multi-turn task that produced only its terminal envelope
``OC-04``  a run that reported some of its usage
``OC-05``  a stream whose last line was cut off mid-write
``OC-06``  an event type the adapter does not know
``OC-07``  a run that failed
``OC-08``  an exit code nothing describes
``OC-09``  a run that reported no usage at all
``OC-10``  one line of ten mebibytes
``OC-11``  a stream with blank lines and no final newline
``OC-12``  a run that named no session
========  ===========================================================

The two adapters are not the same shape
=======================================

The claude adapter is an *identity*: its events carry the CLI's payload
key for key, so a usage block is the CLI's own block — extra keys and
all — and a result event carries no marker of the adapter's own. The
mcode adapter is a *translation*: its events are canonical, its usage
block has exactly the four canonical fields, and a field the CLI never
counted is ``None`` rather than a number.

So a row is pinned on the *canonical projection* of the output — the
four usage fields, projected out of whichever spelling the CLI used —
and a row that can only be pinned on one side says which side, and
why. The two spellings the CLIs use are the same four names in
different cases (``inputTokens`` vs ``input_tokens``), and the mcode CLI
has no cache-creation field at all: a number that was never counted
stays missing on both sides.

Where a shape has to *fail*
===========================

Two of the twelve shapes are not about what an adapter produces but
about what a run that produces it means, and neither is knowable to
the adapter: only the caller knows that a task was multi-turn
(``OC-03``) and only the caller knows that it needs to know which
conversation the run belonged to (``OC-12``). Those two failures are
therefore raised by the contract itself — by :func:`require_stream_events`
and :func:`require_session_id` below — and what is pinned is that the
failure happens *explicitly* and that its message cites the output it
saw. A requirement that fails quietly is the defect this whole file
exists to catch.

What the failures may cite
==========================

Every failure message has to say what it saw: the line number and the
text of a line that could not be read, or a fragment of the envelope
that says what the run reported. A failure that cannot say what it saw
is a failure nobody can act on — it is the same defect one layer down.

No process is spawned and no ``Popen`` is mocked (test decision point
2): the adapters' job is to turn a recorded stream into events, and the
events are the whole observable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

from harness import runner as harness_runner
from harness.claude import ClaudeHarness
from harness.contract import HarnessEvent, HarnessOutcome
from harness.mcode import McodeHarness

pytestmark = pytest.mark.unit


#: The two adapters the contract is pinned against.
ADAPTERS: Tuple[str, ...] = ("claude", "mcode")

#: The four canonical usage fields. A field that was not reported is
#: missing from this set of values — never zero.
CANONICAL_USAGE_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: The session id the MiniMax Code CLI mints.
MCODE_SESSION_ID = "mvs_0ecaf7d2e4b64d1f9b2c7a5d8e1f043"

#: The session id the recorded claude streams carry.
CLAUDE_SESSION_ID = "22222222-0000-4000-8000-000000000000"

#: The usage the MiniMax Code CLI reports: no cache-creation field,
#: because that number was never counted.
MCODE_USAGE: Dict[str, Any] = {
    "inputTokens": 15095,
    "outputTokens": 3,
    "cacheReadTokens": 1792,
    "totalTokens": 15098,
}

#: The size of the text the oversized line carries. The line itself is
#: a few dozen bytes longer — that is the JSON around it.
TEN_MIB = 10 * 1024 * 1024

#: The text of the oversized line. One copy, shared by both adapters.
BIG_TEXT = "x" * TEN_MIB

#: The line that was cut off mid-write, in both rows that use it.
TRUNCATED_LINE = '{"type":"result","is_err'

#: A marker that only ever appears in the unknown event of ``OC-06``.
UNKNOWN_MARKER = "mvs_unknown_event_marker"

#: The answer of the run that produced only its terminal envelope.
TERMINAL_MARKER = "terminal-envelope-only"

#: The recorded claude streams.
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "harness" / "claude_stream_json"


class OutputContractError(RuntimeError):
    """A run's output broke the output contract.

    Attributes:
        evidence: What the failure saw — the fragment of the output
            that proves it. A failure that cannot say what it saw is a
            failure nobody can act on.
    """

    def __init__(self, message: str, evidence: str = "") -> None:
        super().__init__(message)
        self.evidence = evidence


# ---------------------------------------------------------------------------
# Reading one run's output, whichever adapter produced it
# ---------------------------------------------------------------------------


def _adapter(name: str):
    """The adapter that answers to ``name``."""
    if name == "claude":
        return ClaudeHarness()
    return McodeHarness()


def _events(adapter: str, lines: Sequence[str]) -> List[HarnessEvent]:
    """The events one stream turns into."""
    return list(_adapter(adapter).parse_stream(lines))


def _result(events: Sequence[HarnessEvent]) -> Optional[HarnessEvent]:
    """The run's terminal result event, when the stream carried one."""
    results = [event for event in events if event.type == "result"]
    return results[-1] if results else None


def _envelope(events: Sequence[HarnessEvent]) -> Optional[Dict[str, Any]]:
    """The payload the run ended with — the envelope."""
    result = _result(events)
    return None if result is None else dict(result.raw)


def _content_events(events: Sequence[HarnessEvent]) -> List[HarnessEvent]:
    """The events that carry the run's content.

    Bookkeeping (``system``), the terminal envelope (``result``) and
    the reports of lines that could not be read (``parse_error``) are
    not content: they are what happened *around* the run.
    """
    return [
        event
        for event in events
        if event.type not in ("result", "system", "parse_error")
    ]


def _canonical_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """The four canonical usage fields, projected out of a usage block.

    A field the CLI did not report comes out as ``None``. It is not
    zero: zero is a count that came back, and a report that says zero
    cannot be told apart from a report of a run that really did not
    touch the cache.
    """
    usage = usage or {}
    return {field: usage.get(field) for field in CANONICAL_USAGE_FIELDS}


def _expected_usage(source: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """The canonical usage the contract expects from an mcode payload."""
    return {
        "input_tokens": source.get("inputTokens"),
        "output_tokens": source.get("outputTokens"),
        "cache_read_input_tokens": source.get("cacheReadTokens"),
        "cache_creation_input_tokens": source.get("cacheCreationTokens"),
    }


def _verdict(adapter: str, code: int, events: Sequence[HarnessEvent]) -> HarnessOutcome:
    """The verdict the layer above gets for one run.

    An adapter that classifies exits itself has the last word. One that
    does not gets the runner's fallback — which is what the layer above
    would really see, so the contract is pinned against that and not
    against a guess.
    """
    classifier = getattr(_adapter(adapter), "classify_exit", None)
    if classifier is not None:
        return classifier(code, events)
    return harness_runner._default_outcome(code)


def _envelope_evidence(envelope: Optional[Dict[str, Any]]) -> str:
    """A fragment of the envelope that shows what a failure saw."""
    if not envelope:
        return "<no envelope>"
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)[:400]


def _contains(payload: Any, needle: Any) -> bool:
    """Whether ``needle`` is anywhere inside ``payload``."""
    if isinstance(payload, dict):
        return any(_contains(value, needle) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains(value, needle) for value in payload)
    return payload == needle


# ---------------------------------------------------------------------------
# The two requirements the caller knows and the adapter cannot
# ---------------------------------------------------------------------------


def require_stream_events(adapter: str, events: Sequence[HarnessEvent]) -> None:
    """The stream events a multi-turn run has to produce.

    A run that was asked for more than one turn and that produced only
    its terminal envelope has no conversation to replay and no answer
    that can be attributed to the turns before it. The parse of such a
    stream is still correct — one result, nothing invented — but the
    *run* is not what was asked for, and that has to be said out loud.

    Args:
        adapter: The adapter the events came from.
        events: The events the run produced.

    Raises:
        OutputContractError: The run produced no stream events. The
            evidence is the envelope that should have come with them.
    """
    if _content_events(events):
        return

    evidence = _envelope_evidence(_envelope(events))
    raise OutputContractError(
        f"{adapter}: a multi-turn run produced no stream events; only its "
        f"terminal envelope was reported: {evidence}",
        evidence=evidence,
    )


def require_session_id(adapter: str, events: Sequence[HarnessEvent]) -> None:
    """The session a run has to name.

    A run that named no conversation cannot be recorded against one, and
    a recording that invents an id points at a conversation that does
    not exist — which is worse than recording nothing at all.

    Args:
        adapter: The adapter the events came from.
        events: The events the run produced.

    Raises:
        OutputContractError: The run named no session. The evidence is
            the envelope that should have named it.
    """
    for event in events:
        if event.session_id:
            return

    evidence = _envelope_evidence(_envelope(events))
    raise OutputContractError(
        f"{adapter}: the run named no session: {evidence}",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Building the output of each shape
# ---------------------------------------------------------------------------


def _fixture_lines(name: str) -> List[str]:
    """The lines of one recorded claude stream."""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()


def _fixture_usage(name: str) -> Dict[str, Optional[int]]:
    """The canonical usage the recorded stream's own result line says."""
    payload = json.loads(_fixture_lines(name)[-1])
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }


def _mcode_item(text: str, session_id: str = MCODE_SESSION_ID) -> Dict[str, Any]:
    """One finished agent message."""
    return {
        "type": "item.completed",
        "sessionId": session_id,
        "item": {"type": "agent_message", "content": text},
    }


def _mcode_turn(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The event that closes a turn."""
    return {
        "type": "turn.completed",
        "sessionId": MCODE_SESSION_ID,
        "model": {"providerId": "minimax", "modelId": "MiniMax-M3.1-Flash"},
        "usage": usage,
    }


def _mcode_exec(
    usage: Optional[Dict[str, Any]] = None,
    status: str = "succeeded",
    output: str = "OK",
    session_id: str = MCODE_SESSION_ID,
) -> Dict[str, Any]:
    """The last event of a finished run."""
    result: Dict[str, Any] = {
        "type": "exec.result",
        "status": status,
        "output": output,
        "model": {"providerId": "minimax", "modelId": "MiniMax-M3.1-Flash"},
    }
    if usage is not None:
        result["usage"] = usage
    return {
        "type": "exec.completed",
        "sessionId": session_id,
        "result": result,
    }


def _mcode_lines(*payloads: Dict[str, Any]) -> List[str]:
    """The child's output, one line per payload."""
    return [json.dumps(payload) for payload in payloads]


def _text_lines(*parts: str) -> List[str]:
    """The lines of a stream written as text.

    The parts are separated by a blank line and the last one has no
    terminator: the shape a stream has when the child was killed just
    after writing its last event.
    """
    return "\n\n".join(parts).split("\n")


def _claude_line(payload: Dict[str, Any]) -> str:
    """One line of the claude child's output."""
    return json.dumps(payload)


def _no_usage() -> Dict[str, Optional[int]]:
    """The canonical usage of a run that reported none."""
    return {field: None for field in CANONICAL_USAGE_FIELDS}


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputCase:
    """One row of the output-contract table.

    Attributes:
        case_id: The row's id, ``OC-01`` .. ``OC-12``.
        shape: What the output is, in one line.
        lines: The child's output, per adapter.
        expected_usage: The canonical usage the contract expects, per
            adapter. A field the CLI did not report is ``None``.
        exit_code: The exit code the run came back with.
        multi_turn: Whether the task was asked for more than one turn.
        check: The contract this row pins.
    """

    case_id: str
    shape: str
    lines: Dict[str, List[str]]
    expected_usage: Dict[str, Dict[str, Optional[int]]]
    check: Callable[[str, List[HarnessEvent], "OutputCase"], None]
    exit_code: int = 0
    multi_turn: bool = False


def _check_recorded_multi_turn(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-01``: a recorded multi-turn run, with tools."""
    _assert_one_result_last(adapter, events)
    result = _result(events)
    assert result is not None
    content = _content_events(events)
    assert len(content) > 1, (
        f"{adapter}/{case.case_id}: a multi-turn run produced "
        f"{len(content)} content events"
    )
    assert result.is_error is False
    assert result.text, f"{adapter}/{case.case_id}: the run carried no final text"
    assert result.session_id, f"{adapter}/{case.case_id}: the run named no session"
    assert _canonical_usage(result.usage) == case.expected_usage[adapter], (
        f"{adapter}/{case.case_id}: the usage is not what the CLI reported"
    )


def _check_recorded_single_turn(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-02``: a recorded single-turn run."""
    _assert_one_result_last(adapter, events)
    result = _result(events)
    assert result is not None
    assert _content_events(events), (
        f"{adapter}/{case.case_id}: the run produced no content at all"
    )
    assert result.is_error is False
    assert result.text, f"{adapter}/{case.case_id}: the run carried no final text"
    assert result.session_id, f"{adapter}/{case.case_id}: the run named no session"
    assert _canonical_usage(result.usage) == case.expected_usage[adapter], (
        f"{adapter}/{case.case_id}: the usage is not what the CLI reported"
    )


def _check_terminal_envelope_only(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-03``: a multi-turn task that produced only its envelope.

    The parse is right — one result, nothing invented — and the *run*
    is not what was asked for. The failure is explicit and cites the
    envelope.
    """
    _assert_one_result_last(adapter, events)
    assert not _content_events(events), (
        f"{adapter}/{case.case_id}: the terminal-envelope case must not "
        f"carry stream events"
    )

    with pytest.raises(OutputContractError) as excinfo:
        require_stream_events(adapter, events)

    assert excinfo.value.evidence, (
        f"{adapter}/{case.case_id}: the failure must cite what it saw"
    )
    assert TERMINAL_MARKER in excinfo.value.evidence, (
        f"{adapter}/{case.case_id}: the failure must cite the envelope, "
        f"got {excinfo.value.evidence!r}"
    )


def _check_partial_usage(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-04``: the run reported some of its usage and left the rest out."""
    _assert_one_result_last(adapter, events)
    result = _result(events)
    assert result is not None
    usage = _canonical_usage(result.usage)

    assert usage == case.expected_usage[adapter], (
        f"{adapter}/{case.case_id}: the usage is not what the CLI reported"
    )
    assert usage["cache_read_input_tokens"] is None, (
        f"{adapter}/{case.case_id}: a count that was never reported is not zero"
    )
    assert usage["cache_creation_input_tokens"] is None, (
        f"{adapter}/{case.case_id}: a count that was never reported is not zero"
    )

    if adapter == "mcode":
        # mcode counts in the canonical shape and says so on the result.
        assert result.raw["usage_partial"] is True
    else:
        # The claude result is the CLI's own payload, verbatim: it
        # carries no marker of the adapter's own. The claude layer above
        # marks a count partial when the run did not end cleanly, which
        # is the runner's rule and not the parser's.
        assert "usage_partial" not in result.raw


def _check_truncated_line(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-05``: the last line was cut off mid-write."""
    errors = [event for event in events if event.type == "parse_error"]
    assert len(errors) == 1, (
        f"{adapter}/{case.case_id}: the truncated line was not reported "
        f"exactly once, got {len(errors)} reports"
    )

    error = errors[0]
    assert error.raw["line_no"] == 2, (
        f"{adapter}/{case.case_id}: the report must say which line it "
        f"came from, got {error.raw.get('line_no')!r}"
    )
    assert error.raw["raw"] == TRUNCATED_LINE, (
        f"{adapter}/{case.case_id}: the report must carry the text that "
        f"could not be read"
    )
    assert error.text == TRUNCATED_LINE
    assert error.is_error is True
    assert _content_events(events), (
        f"{adapter}/{case.case_id}: the line before the truncation was lost"
    )


def _check_unknown_event(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-06``: an event type the adapter does not know."""
    survivors = [
        event for event in events if _contains(event.raw, UNKNOWN_MARKER)
    ]
    assert survivors, (
        f"{adapter}/{case.case_id}: the unknown event was dropped"
    )
    assert all(not event.text for event in survivors), (
        f"{adapter}/{case.case_id}: an event the adapter does not know was "
        f"turned into an answer"
    )


def _check_failed_run(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-07``: the run failed."""
    _assert_one_result_last(adapter, events)
    result = _result(events)
    assert result is not None
    assert result.is_error is True, (
        f"{adapter}/{case.case_id}: a failed run was reported as a success"
    )
    assert result.raw["is_error"] is True

    if adapter == "claude":
        # The CLI exits zero on a run whose answer was a failure, so
        # the verdict has to come from the event and not from the code.
        verdict = _verdict(adapter, 0, events)
        assert verdict.kind == "model_error", (
            f"{adapter}/{case.case_id}: a failed run was classified as "
            f"{verdict.kind!r}"
        )


def _check_unknown_exit(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-08``: an exit code nothing describes."""
    verdict = _verdict(adapter, case.exit_code, events)
    assert verdict.kind != "ok", (
        f"{adapter}/{case.case_id}: an exit code nothing describes was "
        f"reported as a success"
    )
    assert verdict.kind == "unknown"

    envelope = _envelope(events)
    assert envelope is not None, (
        f"{adapter}/{case.case_id}: the envelope was lost"
    )
    assert envelope["type"] == "result"

    if adapter == "claude":
        assert json.dumps(envelope, ensure_ascii=False, sort_keys=True) in (
            verdict.message
        ), (
            f"{adapter}/{case.case_id}: the verdict must keep the envelope, "
            f"got {verdict.message!r}"
        )

    # Classifying the run did not consume the events it was given.
    assert _result(events) is not None


def _check_no_usage(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-09``: the run reported no usage at all."""
    _assert_one_result_last(adapter, events)
    result = _result(events)
    assert result is not None
    assert result.usage is None, (
        f"{adapter}/{case.case_id}: a run with no usage was given a usage "
        f"block: {result.usage!r}"
    )
    assert all(value is None for value in _canonical_usage(None).values())


def _check_oversized_line(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-10``: one line of ten mebibytes.

    The chosen limit is one line at a time: the adapters are handed a
    line, and a line of ten mebibytes is a line like any other. The
    contract does not pin that choice — it pins that the line is
    *accounted for*: carried whole, or refused with the limit named.
    A line that vanishes is the failure this row exists to catch.

    The lengths are compared rather than the texts: an event that
    carries ten mebibytes is not something to put in a failure
    message.
    """
    lengths = {len(event.text) for event in events if event.text}

    if TEN_MIB in lengths:
        # Carried whole. Nothing carries a fragment of it: a line that
        # was cut into pieces would show up as a shorter text.
        assert lengths == {TEN_MIB}, (
            f"{adapter}/{case.case_id}: the oversized line was split: the "
            f"events carry {sorted(lengths)} characters"
        )
        return

    refusals = [event for event in events if event.is_error]
    refusal_count = len(refusals)
    assert refusal_count == 1, (
        f"{adapter}/{case.case_id}: a line of {TEN_MIB} bytes was neither "
        f"carried nor refused"
    )
    message = refusals[0].text[:200]
    assert str(TEN_MIB) in message or "MiB" in message, (
        f"{adapter}/{case.case_id}: a refusal must name the limit it "
        f"applied, got {message!r}"
    )


def _check_blank_lines_and_no_final_newline(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-11``: blank lines are ignored; the last line has no newline."""
    content = _content_events(events)
    assert len(content) == 2, (
        f"{adapter}/{case.case_id}: the stream produced {len(content)} "
        f"content events"
    )
    assert content[0].text == "first"
    assert content[1].text == "second"
    assert not [event for event in events if event.type == "parse_error"], (
        f"{adapter}/{case.case_id}: a blank line was reported as a line "
        f"that could not be read"
    )


def _check_missing_session(
    adapter: str, events: List[HarnessEvent], case: OutputCase
) -> None:
    """``OC-12``: the run named no session.

    The session id is empty on one event and missing on the other. What
    the run said is what the run has: no id is invented for either.
    """
    _assert_one_result_last(adapter, events)
    result = _result(events)
    assert result is not None
    assert not result.session_id, (
        f"{adapter}/{case.case_id}: a session id was invented: "
        f"{result.session_id!r}"
    )
    for event in events:
        assert not event.session_id, (
            f"{adapter}/{case.case_id}: a session id was invented: "
            f"{event.session_id!r}"
        )

    with pytest.raises(OutputContractError) as excinfo:
        require_session_id(adapter, events)

    assert excinfo.value.evidence, (
        f"{adapter}/{case.case_id}: the failure must cite what it saw"
    )


def _assert_one_result_last(adapter: str, events: Sequence[HarnessEvent]) -> None:
    """The stream carries exactly one result, and it is the last event."""
    results = [event for event in events if event.type == "result"]
    assert len(results) == 1, (
        f"{adapter}: the stream carried {len(results)} results"
    )
    assert events[-1] is results[0], (
        f"{adapter}: the result is not the last event of the stream"
    )


#: The twelve output shapes. Every row is applied to both adapters.
CASES: Tuple[OutputCase, ...] = (
    OutputCase(
        case_id="OC-01",
        shape="a recorded multi-turn run, with tools",
        lines={
            "claude": _fixture_lines("multi_turn_tools.jsonl"),
            "mcode": _mcode_lines(
                _mcode_item("first"),
                _mcode_item("second"),
                _mcode_turn(MCODE_USAGE),
                _mcode_exec(MCODE_USAGE),
            ),
        },
        expected_usage={
            "claude": _fixture_usage("multi_turn_tools.jsonl"),
            "mcode": _expected_usage(MCODE_USAGE),
        },
        check=_check_recorded_multi_turn,
        multi_turn=True,
    ),
    OutputCase(
        case_id="OC-02",
        shape="a recorded single-turn run",
        lines={
            "claude": _fixture_lines("single_turn.jsonl"),
            "mcode": _mcode_lines(
                _mcode_item("only"),
                _mcode_turn(MCODE_USAGE),
                _mcode_exec(MCODE_USAGE),
            ),
        },
        expected_usage={
            "claude": _fixture_usage("single_turn.jsonl"),
            "mcode": _expected_usage(MCODE_USAGE),
        },
        check=_check_recorded_single_turn,
    ),
    OutputCase(
        case_id="OC-03",
        shape="a multi-turn task that produced only its terminal envelope",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "result",
                        "is_error": False,
                        "session_id": CLAUDE_SESSION_ID,
                        "result": TERMINAL_MARKER,
                        "subtype": "success",
                    }
                )
            ],
            "mcode": _mcode_lines(_mcode_exec(MCODE_USAGE, output=TERMINAL_MARKER)),
        },
        expected_usage={
            "claude": _no_usage(),
            "mcode": _expected_usage(MCODE_USAGE),
        },
        check=_check_terminal_envelope_only,
        multi_turn=True,
    ),
    OutputCase(
        case_id="OC-04",
        shape="a run that reported some of its usage",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "result",
                        "is_error": False,
                        "session_id": CLAUDE_SESSION_ID,
                        "result": "done",
                        "usage": {"input_tokens": 5, "output_tokens": 1},
                    }
                )
            ],
            "mcode": _mcode_lines(
                _mcode_exec({"inputTokens": 5, "outputTokens": 1}, output="done")
            ),
        },
        expected_usage={
            "claude": {
                "input_tokens": 5,
                "output_tokens": 1,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
            },
            "mcode": {
                "input_tokens": 5,
                "output_tokens": 1,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
            },
        },
        check=_check_partial_usage,
    ),
    OutputCase(
        case_id="OC-05",
        shape="a stream whose last line was cut off mid-write",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "assistant",
                        "session_id": CLAUDE_SESSION_ID,
                        "message": {
                            "content": [{"type": "text", "text": "before"}]
                        },
                    }
                ),
                TRUNCATED_LINE,
            ],
            "mcode": [
                json.dumps(_mcode_item("before")),
                TRUNCATED_LINE,
            ],
        },
        expected_usage={"claude": _no_usage(), "mcode": _no_usage()},
        check=_check_truncated_line,
    ),
    OutputCase(
        case_id="OC-06",
        shape="an event type the adapter does not know",
        lines={
            "claude": [
                _claude_line(
                    {"type": "some.future.event", "detail": UNKNOWN_MARKER}
                )
            ],
            "mcode": [
                json.dumps(
                    {
                        "type": "some.future.event",
                        "sessionId": MCODE_SESSION_ID,
                        "detail": UNKNOWN_MARKER,
                    }
                )
            ],
        },
        expected_usage={"claude": _no_usage(), "mcode": _no_usage()},
        check=_check_unknown_event,
    ),
    OutputCase(
        case_id="OC-07",
        shape="a run that failed",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "result",
                        "is_error": True,
                        "subtype": "error_during_execution",
                        "session_id": CLAUDE_SESSION_ID,
                        "result": "the run failed",
                    }
                )
            ],
            "mcode": _mcode_lines(
                _mcode_exec(MCODE_USAGE, status="failed", output="the run failed")
            ),
        },
        expected_usage={
            "claude": _no_usage(),
            "mcode": _expected_usage(MCODE_USAGE),
        },
        check=_check_failed_run,
    ),
    OutputCase(
        case_id="OC-08",
        shape="an exit code nothing describes",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "result",
                        "is_error": False,
                        "subtype": "error_during_execution",
                        "session_id": CLAUDE_SESSION_ID,
                        "result": "boom",
                    }
                )
            ],
            "mcode": _mcode_lines(_mcode_exec(MCODE_USAGE, output="boom")),
        },
        expected_usage={
            "claude": _no_usage(),
            "mcode": _expected_usage(MCODE_USAGE),
        },
        check=_check_unknown_exit,
        exit_code=9,
    ),
    OutputCase(
        case_id="OC-09",
        shape="a run that reported no usage at all",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "result",
                        "is_error": False,
                        "session_id": CLAUDE_SESSION_ID,
                        "result": "done",
                    }
                )
            ],
            "mcode": _mcode_lines(_mcode_exec(None, output="done")),
        },
        expected_usage={"claude": _no_usage(), "mcode": _no_usage()},
        check=_check_no_usage,
    ),
    OutputCase(
        case_id="OC-10",
        shape="one line of ten mebibytes",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "assistant",
                        "session_id": CLAUDE_SESSION_ID,
                        "message": {
                            "content": [{"type": "text", "text": BIG_TEXT}]
                        },
                    }
                )
            ],
            "mcode": [
                json.dumps(
                    {
                        "type": "item.completed",
                        "sessionId": MCODE_SESSION_ID,
                        "item": {"type": "agent_message", "content": BIG_TEXT},
                    }
                )
            ],
        },
        expected_usage={"claude": _no_usage(), "mcode": _no_usage()},
        check=_check_oversized_line,
    ),
    OutputCase(
        case_id="OC-11",
        shape="a stream with blank lines and no final newline",
        lines={
            "claude": _text_lines(
                _claude_line(
                    {
                        "type": "assistant",
                        "session_id": CLAUDE_SESSION_ID,
                        "message": {
                            "content": [{"type": "text", "text": "first"}]
                        },
                    }
                ),
                "   ",
                _claude_line(
                    {
                        "type": "assistant",
                        "session_id": CLAUDE_SESSION_ID,
                        "message": {
                            "content": [{"type": "text", "text": "second"}]
                        },
                    }
                ),
            ),
            "mcode": _text_lines(
                json.dumps(_mcode_item("first")),
                "   ",
                json.dumps(_mcode_item("second")),
            ),
        },
        expected_usage={"claude": _no_usage(), "mcode": _no_usage()},
        check=_check_blank_lines_and_no_final_newline,
    ),
    OutputCase(
        case_id="OC-12",
        shape="a run that named no session",
        lines={
            "claude": [
                _claude_line(
                    {
                        "type": "assistant",
                        "session_id": "",
                        "message": {
                            "content": [{"type": "text", "text": "hello"}]
                        },
                    }
                ),
                _claude_line(
                    {"type": "result", "is_error": False, "result": "done"}
                ),
            ],
            "mcode": _mcode_lines(
                _mcode_item("hello", session_id=""),
                _mcode_exec(None, output="done", session_id=""),
            ),
        },
        expected_usage={"claude": _no_usage(), "mcode": _no_usage()},
        check=_check_missing_session,
    ),
)


def _case(case_id: str) -> OutputCase:
    """The row with this id."""
    for case in CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(case_id)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_the_matrix_covers_every_output_shape():
    """The table has one row per shape, ``OC-01`` to ``OC-12``, and no dupe."""
    ids = [case.case_id for case in CASES]

    assert ids == [f"OC-{number:02d}" for number in range(1, 13)], (
        f"the table covers {ids}"
    )
    assert len(set(ids)) == len(ids), "the table has a duplicate row"


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_case_matrix(case: OutputCase):
    """Every output shape, on every adapter.

    The row is the contract: what the output is allowed to say, and
    what it is not allowed to leave out. Both adapters are held to it,
    because the layer above them reads the two as one stream.
    """
    for adapter in ADAPTERS:
        events = _events(adapter, case.lines[adapter])
        case.check(adapter, events, case)


def test_usage_never_zero_filled():
    """A number the CLI never reported is missing, not zero.

    Zero is a count that came back. A field the CLI did not fill in
    says nothing about the run, and a reader that cannot tell the two
    apart will believe a number that is too low.
    """
    for case in CASES:
        for adapter in ADAPTERS:
            events = _events(adapter, case.lines[adapter])
            result = _result(events)
            actual = _canonical_usage(result.usage if result is not None else None)
            expected = case.expected_usage[adapter]

            assert actual == expected, (
                f"{case.case_id}/{adapter}: the usage is {actual!r}, "
                f"expected {expected!r}"
            )
            for field in CANONICAL_USAGE_FIELDS:
                if expected[field] is None:
                    assert actual[field] is None, (
                        f"{case.case_id}/{adapter}: {field} was not reported "
                        f"and must not be {actual[field]!r}"
                    )


def test_unknown_exit_preserves_envelope():
    """An exit code nothing describes keeps the envelope.

    The envelope is the only thing left that says what happened, so a
    verdict of ``unknown`` that threw it away would leave a run with no
    account of itself. It is also not a success: nothing described the
    code, so nothing vouched for the run either.
    """
    case = _case("OC-08")

    for adapter in ADAPTERS:
        events = _events(adapter, case.lines[adapter])

        verdict = _verdict(adapter, case.exit_code, events)
        assert verdict.kind == "unknown", (
            f"{adapter}: an exit code nothing describes was classified as "
            f"{verdict.kind!r}"
        )
        assert verdict.kind != "ok"

        envelope = _envelope(events)
        assert envelope is not None, f"{adapter}: the envelope was lost"
        assert envelope["type"] == "result"

        if adapter == "claude":
            assert json.dumps(envelope, ensure_ascii=False, sort_keys=True) in (
                verdict.message
            ), (
                f"{adapter}: the verdict must keep the envelope, got "
                f"{verdict.message!r}"
            )

        # Classifying the run did not consume the events it was given.
        assert _result(events) is not None, (
            f"{adapter}: classifying the run emptied the stream"
        )


def test_error_messages_cite_evidence():
    """Every failure says what it saw.

    A failure that cannot say what it saw is a failure nobody can act
    on, and it is the same defect one layer down: a truncated line that
    is not quoted is a truncated line that is gone.
    """
    # A line that could not be read says which line, and says what it
    # could not read.
    case = _case("OC-05")
    for adapter in ADAPTERS:
        events = _events(adapter, case.lines[adapter])
        errors = [event for event in events if event.type == "parse_error"]
        assert errors, f"{adapter}: the truncated line was not reported"

        for error in errors:
            assert error.raw["line_no"] == 2, (
                f"{adapter}: the report must say which line it came from"
            )
            assert error.text == TRUNCATED_LINE, (
                f"{adapter}: the report must quote the text it could not read"
            )

    # A verdict for a code nothing describes quotes the envelope.
    case = _case("OC-08")
    events = _events("claude", case.lines["claude"])
    verdict = _verdict("claude", case.exit_code, events)
    assert "result envelope" in verdict.message
    assert "boom" in verdict.message, (
        f"the verdict must quote the envelope, got {verdict.message!r}"
    )

    # The two failures the contract raises cite the output they saw.
    for case_id, requirement in (
        ("OC-03", require_stream_events),
        ("OC-12", require_session_id),
    ):
        case = _case(case_id)
        for adapter in ADAPTERS:
            events = _events(adapter, case.lines[adapter])

            with pytest.raises(OutputContractError) as excinfo:
                requirement(adapter, events)

            assert excinfo.value.evidence, (
                f"{case_id}/{adapter}: the failure must cite what it saw"
            )
            assert excinfo.value.evidence in str(excinfo.value), (
                f"{case_id}/{adapter}: the message must carry the evidence"
            )
