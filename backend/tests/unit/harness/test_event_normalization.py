"""TDD spec for the mcode event normalization.

What is pinned here
-------------------

``mcode exec --output-format stream-json`` does not speak the claude
stream-json vocabulary. It says ``exec.started``, ``session.started``,
``turn.started``, ``item.started`` / ``item.completed``,
``turn.completed`` and ``exec.completed``; it reports usage as
``inputTokens`` / ``outputTokens`` / ``cacheReadTokens`` /
``totalTokens``. The layer above this one consumes claude-shaped
events, so the adapter has to translate.

The translation is pinned here on four points, each of which is a way
the translation can go wrong in a way that is invisible afterwards:

1. **A message is an assistant event.** One ``item.completed`` carrying
   an ``agent_message`` becomes one ``assistant`` event whose message
   content is the text — not a message that is silently swallowed, and
   not one that is emitted twice.
2. **There is exactly one ``result``, and it is the last event.** A
   stream that ends in two results, or in a result with events after
   it, breaks the reader above: it reads the *last* result as the
   answer, so a second one would overwrite the first.
3. **A missing number stays missing.** mcode never reports cache
   creation tokens. The canonical usage says so with ``None`` and
   marks the count partial; it does not say ``0``, because ``0`` is a
   number that was never counted, and a report that says ``0`` cannot
   be told apart from a report of a run that really did not create a
   cache.
4. **A failed run is an error.** ``exec.completed`` reports a
   ``status``; anything but ``succeeded`` means the run failed, and
   the canonical result says so through ``is_error`` — the claude
   CLI exits zero on a failed answer, so the exit code alone would
   call it a success.

The remaining boundaries of the same translation are pinned
alongside: an event whose type the adapter does not know is kept
rather than dropped, and a stream with no ``exec.completed`` still
ends in exactly one ``result``, synthesized from the turn that did
finish.

No process is spawned and no ``Popen`` is mocked (test decision point
2): the adapter's job is to turn a recorded stream into events, and
the events are the whole observable.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

import pytest

from harness.mcode import McodeHarness

pytestmark = pytest.mark.unit


#: The session id the CLI mints. Every event of one run carries it.
SESSION_ID = "mvs_0ecaf7d2e4b64d1f9b2c7a5d8e1f043"


def _line(payload: Dict[str, Any]) -> str:
    """One line of the child's output: the payload, as JSON."""
    return json.dumps(payload)


def _lines(*payloads: Dict[str, Any]) -> List[str]:
    """The child's output, one line per payload."""
    return [_line(payload) for payload in payloads]


def _exec_started() -> Dict[str, Any]:
    """The first event of every run."""
    return {"type": "exec.started", "sessionId": SESSION_ID, "runId": "exec_turn_1"}


def _session_started() -> Dict[str, Any]:
    """The event that announces the conversation."""
    return {"type": "session.started", "sessionId": SESSION_ID}


def _turn_started() -> Dict[str, Any]:
    """The event that announces the turn."""
    return {"type": "turn.started", "sessionId": SESSION_ID, "turnId": "turn_1"}


def _item_completed(content: str = "OK") -> Dict[str, Any]:
    """The agent's finished message."""
    return {
        "type": "item.completed",
        "sessionId": SESSION_ID,
        "item": {
            "id": "e53a7798-2893-4959-8c0e-ad2ac4cab933:message",
            "type": "agent_message",
            "content": content,
        },
    }


def _usage() -> Dict[str, Any]:
    """The usage block mcode reports — with no cache-creation field."""
    return {
        "inputTokens": 15095,
        "outputTokens": 3,
        "cacheReadTokens": 1792,
        "totalTokens": 15098,
    }


def _model() -> Dict[str, Any]:
    """The model block mcode reports."""
    return {
        "providerId": "minimax",
        "modelId": "MiniMax-M3.1-Flash",
        "variant": "thinking",
        "providerSource": "provider",
        "providerKind": "minimax-managed",
    }


def _turn_completed() -> Dict[str, Any]:
    """The event that closes the turn, with its usage and model."""
    return {
        "type": "turn.completed",
        "sessionId": SESSION_ID,
        "model": _model(),
        "usage": _usage(),
        "usageSource": "completed_responses",
        "usageIncomplete": False,
        "durationMs": 2348,
    }


def _exec_completed(status: str = "succeeded", output: str = "OK") -> Dict[str, Any]:
    """The last event of a finished run."""
    return {
        "type": "exec.completed",
        "sessionId": SESSION_ID,
        "result": {
            "type": "exec.result",
            "runId": "exec_turn_1",
            "sessionId": SESSION_ID,
            "turnId": "turn_1",
            "status": status,
            "output": output,
            "model": _model(),
            "usage": _usage(),
            "usageSource": "completed_responses",
            "usageIncomplete": False,
            "durationMs": 2348,
        },
    }


def _parse(lines: Iterable[str]) -> List[Any]:
    """The events one stream turns into."""
    return list(McodeHarness().parse_stream(lines))


# ---------------------------------------------------------------------------
# 1. A message is an assistant event
# ---------------------------------------------------------------------------


def test_agent_message_to_assistant():
    """One agent message becomes one assistant event carrying the text."""
    events = _parse(_lines(_item_completed("OK")))

    assistants = [event for event in events if event.type == "assistant"]
    assert len(assistants) == 1

    assistant = assistants[0]
    assert assistant.raw["message"] == {
        "content": [{"type": "text", "text": "OK"}]
    }
    assert assistant.text == "OK"


# ---------------------------------------------------------------------------
# 2. Exactly one result, and it is last
# ---------------------------------------------------------------------------


def test_result_is_last_and_unique():
    """A finished stream ends in one result event, and only one."""
    events = _parse(
        _lines(
            _exec_started(),
            _session_started(),
            _turn_started(),
            _item_completed(),
            _turn_completed(),
            _exec_completed(),
        )
    )

    results = [event for event in events if event.type == "result"]
    assert len(results) == 1
    assert events[-1].type == "result"
    assert results[0] is events[-1]


def test_a_run_without_exec_completed_still_ends_in_one_result():
    """A stream cut short before ``exec.completed`` still has a result."""
    events = _parse(
        _lines(
            _exec_started(),
            _session_started(),
            _turn_started(),
            _item_completed(),
            _turn_completed(),
        )
    )

    results = [event for event in events if event.type == "result"]
    assert len(results) == 1
    assert events[-1].type == "result"
    # The result is synthesized from the turn that did finish, so it
    # carries that turn's usage.
    assert results[0].usage == {
        "input_tokens": 15095,
        "output_tokens": 3,
        "cache_read_input_tokens": 1792,
        "cache_creation_input_tokens": None,
    }


# ---------------------------------------------------------------------------
# 3. A number that was never reported stays missing
# ---------------------------------------------------------------------------


def test_usage_partial_without_cache_creation():
    """Cache creation was not reported, so it is ``None`` and the count partial."""
    events = _parse(_lines(_exec_started(), _turn_completed(), _exec_completed()))

    result = events[-1]
    assert result.usage == {
        "input_tokens": 15095,
        "output_tokens": 3,
        "cache_read_input_tokens": 1792,
        "cache_creation_input_tokens": None,
    }
    assert result.raw["usage_partial"] is True


def test_a_reported_cache_creation_is_not_partial():
    """A count that was actually reported is not marked partial."""
    payload = _exec_completed()
    payload["result"]["usage"] = dict(_usage(), cacheCreationTokens=64)

    events = _parse(_lines(payload))

    result = events[-1]
    assert result.usage["cache_creation_input_tokens"] == 64
    assert result.raw["usage_partial"] is False


def test_the_usage_has_exactly_the_four_fields():
    """The canonical usage carries the four fields, and no invented one."""
    events = _parse(_lines(_turn_completed(), _exec_completed()))

    assert set(events[-1].usage) == {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }


# ---------------------------------------------------------------------------
# 4. A failed run is an error
# ---------------------------------------------------------------------------


def test_failed_status_is_error():
    """A status other than ``succeeded`` makes the result an error."""
    events = _parse(_lines(_exec_completed(status="failed", output="")))

    result = events[-1]
    assert result.type == "result"
    assert result.is_error is True
    assert result.raw["is_error"] is True


def test_a_succeeded_run_is_not_an_error():
    """The normal case is not an error."""
    events = _parse(_lines(_exec_completed()))

    assert events[-1].is_error is False


# ---------------------------------------------------------------------------
# 5. Nothing is thrown away
# ---------------------------------------------------------------------------


def test_an_unknown_event_type_is_kept():
    """An event the adapter does not know survives in an assistant event."""
    events = _parse(
        _lines(
            {"type": "some.future.event", "sessionId": SESSION_ID, "detail": 7},
            _item_completed(),
        )
    )

    kept = [
        event
        for event in events
        if event.type == "assistant"
        and event.raw.get("raw", {}).get("type") == "some.future.event"
    ]
    assert len(kept) == 1
    assert kept[0].raw["raw"]["detail"] == 7


def test_the_result_carries_the_session_and_the_model():
    """The result names the conversation and the model that ran."""
    events = _parse(_lines(_turn_completed(), _exec_completed()))

    result = events[-1]
    assert result.session_id == SESSION_ID
    assert result.raw["model"] == "minimax/MiniMax-M3.1-Flash"


def test_the_result_carries_the_final_text():
    """The final answer is the text the reader above will hand back."""
    events = _parse(_lines(_item_completed("OK"), _exec_completed(output="OK")))

    assert events[-1].text == "OK"
    assert events[-1].raw["result"] == "OK"
