"""TDD spec for :func:`harness.claude.parse_stream` — identity pass-through.

What is pinned here
-------------------

The claude CLI writes one JSON document per line (``--output-format
stream-json``). The layer above this one consumes that stream *in the
claude event shape*: it wants the CLI's own events, not a paraphrase of
them. So the parser's job is narrow and strict — **carry the payload
across unchanged**.

The contract points pinned here:

1. **A valid line arrives as the payload it was.** ``event.raw`` is the
   parsed document, key for key: nothing dropped, nothing added, nothing
   renamed. The recorded fixtures under
   ``tests/fixtures/harness/claude_stream_json/`` are replayed line by
   line, and each event's payload is compared with the JSON the line
   parses to.
2. **The order is the order of the stream.** The parser never sorts,
   never buffers and never merges: the n-th event comes from the n-th
   line.
3. **A blank line is not an event.** The CLI pads its output; an empty
   line carries no event and produces none.
4. **A line that does not parse is not dropped.** A truncated line — the
   tail of a stream that was cut off mid-write — becomes a
   ``parse_error`` event that carries the offending text, so the damage
   is reported instead of silently swallowed.
5. **Bytes that are not UTF-8 do not stop the stream.** They are decoded
   with ``errors="replace"``, and the run continues.

The parser is a pure function of its input (test decision point 2): it
reads no file, starts no process and writes nothing. The only external
thing it touches is the recorded fixtures, which are data, not a
dependency to stub.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from harness.claude import ClaudeHarness, parse_stream

pytestmark = pytest.mark.unit


# ``backend/tests/unit/harness/`` -> the stream fixtures live two levels
# up, next to the other fixture trees.
FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "harness" / "claude_stream_json"
)

#: The recordings of the real claude CLI.
ALL_FIXTURES = (
    "single_turn.jsonl",
    "multi_turn_tools.jsonl",
    "error_and_partial_usage.jsonl",
    "session_edge_cases.jsonl",
)

#: The recording whose session ids are degenerate: empty on most of its
#: events, absent from one of them.
SESSION_EDGE_CASES = "session_edge_cases.jsonl"

#: The directory holding the negative samples -- the streams that are
#: *meant* not to parse.
MALFORMED_DIR = FIXTURE_DIR / "malformed"

#: The negative sample: a stream with one line cut off mid-write.
MALFORMED_FIXTURE = "truncated_tail.jsonl"


def _lines(name: str) -> List[str]:
    """The lines of one recorded fixture."""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()


def _malformed_lines(name: str) -> List[str]:
    """The lines of one negative sample."""
    return (MALFORMED_DIR / name).read_text(encoding="utf-8").splitlines()


def _parses(line: str) -> bool:
    """Whether *line* is a JSON document."""
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# 1. A valid line arrives as the payload it was
# ---------------------------------------------------------------------------


def test_parse_is_identity():
    """Every recorded line arrives as the payload it was written as.

    The recorded fixtures are recordings of the real CLI, so they are the
    ground truth for what a claude event looks like. Each event's
    ``raw`` is compared with the JSON its line parses to — key for key —
    and the events are compared with the lines positionally, which
    pins the order at the same time.
    """
    for name in ALL_FIXTURES:
        lines = _lines(name)
        events = list(parse_stream(lines))

        assert len(events) == len(lines), (
            f"{name}: {len(lines)} lines produced {len(events)} events"
        )

        for line_no, (event, line) in enumerate(zip(events, lines), start=1):
            payload = json.loads(line)

            assert event.raw == payload, (
                f"{name}:{line_no}: the payload was not carried across "
                f"unchanged"
            )
            assert set(event.raw) == set(payload), (
                f"{name}:{line_no}: the payload gained or lost keys"
            )
            assert event.type == payload["type"], (
                f"{name}:{line_no}: the event type was not carried across"
            )


def test_the_type_comes_from_the_payload():
    """The event type is the CLI's own type, not one of our own."""
    events = list(parse_stream(['{"type":"assistant","message":{"content":[]}}']))

    assert events[0].type == "assistant"


def test_a_payload_without_a_type_is_still_delivered():
    """An event the parser cannot type is not dropped."""
    events = list(parse_stream(['{"session_id":"s1"}']))

    assert len(events) == 1
    assert events[0].raw == {"session_id": "s1"}


# ---------------------------------------------------------------------------
# 2. The order is the order of the stream
# ---------------------------------------------------------------------------


def test_the_events_keep_the_order_of_the_lines():
    """The parser does not sort, buffer or merge."""
    lines = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[]}}',
        '{"type":"user","message":{"content":[]}}',
        '{"type":"result","is_error":false}',
    ]

    events = list(parse_stream(lines))

    assert [event.type for event in events] == [
        "system",
        "assistant",
        "user",
        "result",
    ]


# ---------------------------------------------------------------------------
# 3. A blank line is not an event
# ---------------------------------------------------------------------------


def test_blank_lines_are_ignored():
    """An empty line carries no event and produces none."""
    lines = ["", '{"type":"result","is_error":false}', "   ", ""]

    events = list(parse_stream(lines))

    assert len(events) == 1
    assert events[0].type == "result"


# ---------------------------------------------------------------------------
# 4. A line that does not parse is not dropped
# ---------------------------------------------------------------------------


def test_truncated_line_emits_diagnostic():
    """A half-written line becomes a diagnostic that carries the text.

    The tail of a stream that was cut off mid-write is not valid JSON.
    Dropping it would lose the only evidence that the run was cut off,
    so it is reported as a ``parse_error`` event that carries the
    offending text and the line it came from.
    """
    lines = [
        '{"type":"assistant","message":{"content":[]}}',
        '{"type":"result"',
    ]

    events = list(parse_stream(lines))

    assert len(events) == 2, "the truncated line was dropped instead of reported"

    diagnostic = events[1]
    assert diagnostic.type == "parse_error"
    assert diagnostic.raw == {
        "type": "parse_error",
        "line_no": 2,
        "raw": '{"type":"result"',
    }
    assert diagnostic.is_error is True


def test_the_parse_error_carries_the_original_text():
    """The diagnostic quotes the line it is about."""
    events = list(parse_stream(['{"type":"result","is_error":fal']))

    assert events[0].raw["raw"] == '{"type":"result","is_error":fal'


def test_a_json_document_that_is_not_an_object_is_reported():
    """A line that is valid JSON but not an event is still reported.

    The payload of a claude event is an object; a bare array or number
    is not something the layer above can consume, so it cannot be
    carried across — and it is reported rather than dropped.
    """
    events = list(parse_stream(["[1, 2, 3]"]))

    assert len(events) == 1
    assert events[0].type == "parse_error"
    assert events[0].raw["raw"] == "[1, 2, 3]"


def test_a_parse_error_does_not_stop_the_stream():
    """One broken line does not end the stream."""
    lines = ['{"type":"result"', '{"type":"result","is_error":false}']

    events = list(parse_stream(lines))

    assert [event.type for event in events] == ["parse_error", "result"]


# ---------------------------------------------------------------------------
# 5. Bytes that are not UTF-8 do not stop the stream
# ---------------------------------------------------------------------------


def test_non_utf8_bytes_are_replaced_and_the_stream_continues():
    """Undecodable bytes become replacement characters, not a crash."""
    raw = b'{"type":"result","result":"\xff\xfe"}'

    events = list(parse_stream([raw]))

    assert len(events) == 1
    assert events[0].type == "result"
    assert "�" in events[0].raw["result"]


# ---------------------------------------------------------------------------
# 6. The adapter answers the same question
# ---------------------------------------------------------------------------


def test_the_adapter_parses_the_same_way():
    """``ClaudeHarness.parse_stream`` is the same parser."""
    lines = ['{"type":"assistant","message":{"content":[]}}']

    from_adapter = ClaudeHarness().parse_stream(lines)
    from_function = list(parse_stream(lines))

    assert isinstance(from_adapter, list)
    assert [event.raw for event in from_adapter] == [
        event.raw for event in from_function
    ]


def test_the_payload_is_not_shared_between_events():
    """Two events never share one payload object."""
    lines = ['{"type":"result","is_error":false}', '{"type":"result","is_error":false}']

    events = list(parse_stream(lines))

    assert events[0].raw is not events[1].raw


def test_the_event_carries_the_session_it_belongs_to():
    """The session id is read out of the payload, not invented."""
    payload: Dict[str, Any] = {"type": "result", "is_error": False, "session_id": "s1"}

    events = list(parse_stream([json.dumps(payload)]))

    assert events[0].session_id == "s1"


# ---------------------------------------------------------------------------
# 7. The recorded fixtures cover the degenerate cases
# ---------------------------------------------------------------------------


def test_fixtures_cover_empty_session_id_and_truncated_line():
    """The fixtures name the two cases the parser has to survive.

    **A payload that names no conversation.** A ``session_id`` that is
    an empty string and a ``session_id`` that is not there at all mean
    the same thing -- this event belongs to no session -- and neither
    may be filled in. The parser carries the payload across: an empty
    string stays an empty string, an absent key stays absent, and
    nothing is invented to fill the hole. A session id the parser made
    up would be a session the run never had.

    **A line that does not parse.** The tail of a write that was cut
    off is reported -- one ``parse_error`` event, carrying the number
    of the line it came from and the text that broke -- and the lines
    after it keep arriving. A stream that ends at the first broken
    line loses every event that followed it.
    """
    edge_path = FIXTURE_DIR / SESSION_EDGE_CASES
    assert edge_path.exists(), f"missing stream-json fixture: {edge_path}"

    edge_events = list(parse_stream(_lines(SESSION_EDGE_CASES)))

    empty = [event for event in edge_events if event.raw.get("session_id") == ""]
    assert empty, (
        f"{SESSION_EDGE_CASES} carries no event with an empty session id"
    )
    for event in empty:
        assert event.session_id == "", (
            "an empty session id was filled in instead of carried across"
        )
        assert event.raw["session_id"] == "", (
            "the payload's session id was rewritten"
        )

    missing = [event for event in edge_events if "session_id" not in event.raw]
    assert missing, (
        f"{SESSION_EDGE_CASES} carries no event without a session id"
    )
    for event in missing:
        assert event.session_id is None, (
            "a payload without a session id was given one"
        )
        assert "session_id" not in event.raw, (
            "a session id was added to a payload that did not carry one"
        )

    malformed_path = MALFORMED_DIR / MALFORMED_FIXTURE
    assert malformed_path.exists(), (
        f"missing malformed stream-json fixture: {malformed_path}"
    )

    malformed_lines = _malformed_lines(MALFORMED_FIXTURE)
    broken = [
        (line_no, line)
        for line_no, line in enumerate(malformed_lines, start=1)
        if not _parses(line)
    ]
    assert len(broken) == 1, (
        f"{MALFORMED_FIXTURE} carries {len(broken)} unparseable lines, "
        "expected exactly one"
    )
    broken_no, broken_text = broken[0]

    malformed_events = list(parse_stream(malformed_lines))

    errors = [
        event for event in malformed_events if event.type == "parse_error"
    ]
    assert len(errors) == 1, (
        f"{MALFORMED_FIXTURE}: expected exactly one parse error, got "
        f"{len(errors)}"
    )
    assert errors[0].raw["line_no"] == broken_no, (
        f"{MALFORMED_FIXTURE}: the parse error names line "
        f"{errors[0].raw['line_no']}, but the broken line is {broken_no}"
    )
    assert errors[0].raw["raw"] == broken_text, (
        f"{MALFORMED_FIXTURE}: the parse error does not quote the broken line"
    )
    assert errors[0].is_error is True, (
        f"{MALFORMED_FIXTURE}: the parse error is not marked as an error"
    )

    # The broken line did not end the stream: the legal line after it
    # is still delivered.
    error_index = malformed_events.index(errors[0])
    assert error_index < len(malformed_events) - 1, (
        f"{MALFORMED_FIXTURE}: the broken line is the last one, so nothing "
        "follows it"
    )
    assert malformed_events[-1].type == "result", (
        f"{MALFORMED_FIXTURE}: the legal line after the broken one was "
        f"not delivered, the stream ended with "
        f"{malformed_events[-1].type!r}"
    )