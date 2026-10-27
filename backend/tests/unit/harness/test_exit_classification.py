"""TDD spec for :func:`harness.claude.classify_exit` — what the exit meant.

What is pinned here
-------------------

An exit code is a number; what the run *meant* is a verdict. The layer
above this one branches on the verdict — retry, re-run with different
settings, give up — so the mapping from exit code to verdict has to be
written down rather than re-derived at each call site.

The contract points pinned here:

1. **The exit code decides the verdict.** ``0`` is a run that finished,
   ``1`` a run the model failed, ``2`` a run that could not be set up,
   ``124`` a run that ran out of time, ``130``/``143`` a run that was
   signalled, and anything else a run nothing else describes.
2. **A failed result is not a success.** A ``result`` event that reports
   ``is_error`` turns a zero exit code into a model error. The CLI
   exits zero on a run whose answer was a failure, and reading the
   exit code alone would call that run a success.
3. **An unmapped code keeps the envelope.** When nothing describes the
   exit code, the verdict is ``unknown`` — but the payload that came
   with it is not thrown away, because it is the only thing left that
   says what happened.
4. **The verdict is always one of the known kinds.** The outcome kind
   is a closed set; a mapping that produced anything else would be a
   verdict the runner cannot branch on.

The classifier is a pure function of the exit code and the events (test
decision point 2): it starts no process and reads no file.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from harness.claude import ClaudeHarness, classify_exit, extract_result, parse_stream
from harness.contract import OUTCOME_KINDS, HarnessEvent

pytestmark = pytest.mark.unit


#: A terminal ``result`` event as the claude CLI writes it.
def _result_event(**extra) -> HarnessEvent:
    """One ``result`` event, as the CLI writes it."""
    payload = {"type": "result", "is_error": False, "subtype": "success"}
    payload.update(extra)
    return HarnessEvent(type="result", raw=payload, is_error=bool(payload["is_error"]))


# ---------------------------------------------------------------------------
# 1. The exit code decides the verdict
# ---------------------------------------------------------------------------


def test_exit_code_table():
    """Every exit code the contract names maps to the verdict it names.

    The table is the whole mapping. A code that is not in it is not
    guessed at — it is ``unknown``, which is a verdict the runner can
    still act on.
    """
    cases = (
        (0, "ok"),
        (1, "model_error"),
        (2, "config_error"),
        (124, "timeout"),
        (130, "signaled"),
        (143, "signaled"),
        (9, "unknown"),
    )

    for code, expected in cases:
        outcome = classify_exit(code, [])

        assert outcome.kind == expected, (
            f"exit code {code} was classified as {outcome.kind!r}, "
            f"expected {expected!r}"
        )


def test_an_unmapped_code_is_unknown():
    """A code nothing describes is not guessed at."""
    for code in (3, 9, 42, 125, 137):
        assert classify_exit(code, []).kind == "unknown"


def test_the_verdict_is_always_a_known_kind():
    """The outcome kind is a closed set."""
    for code in (0, 1, 2, 9, 124, 130, 143):
        assert classify_exit(code, []).kind in OUTCOME_KINDS


# ---------------------------------------------------------------------------
# 2. A failed result is not a success
# ---------------------------------------------------------------------------


def test_is_error_with_exit_zero():
    """A result that reports a failure is a model error, not a success.

    The CLI exits zero on a run whose answer was a failure. Reading
    the exit code alone would call that run a success, so the result
    event is what has the last word on a zero exit.
    """
    events = list(parse_stream(['{"type":"result","is_error":true}']))

    outcome = classify_exit(0, events)

    assert outcome.kind == "model_error"


def test_a_successful_result_with_exit_zero_is_ok():
    """The ordinary case is still a success."""
    events = list(parse_stream(['{"type":"result","is_error":false}']))

    assert classify_exit(0, events).kind == "ok"


def test_a_run_without_a_result_event_and_exit_zero_is_ok():
    """Nothing reported a failure, so nothing failed."""
    assert classify_exit(0, []).kind == "ok"


# ---------------------------------------------------------------------------
# 3. An unmapped code keeps the envelope
# ---------------------------------------------------------------------------


def test_an_unmapped_code_keeps_the_envelope():
    """The payload that came with the run is not thrown away.

    When no exit code describes the run, the envelope is the only thing
    left that says what happened — so the verdict carries it.
    """
    payload = {"type": "result", "is_error": True, "subtype": "error_during_execution"}
    events = [HarnessEvent(type="result", raw=payload, is_error=True)]

    outcome = classify_exit(9, events)

    assert outcome.kind == "unknown"
    assert "error_during_execution" in outcome.message


def test_the_envelope_is_readable_back_out_of_the_events():
    """``extract_result`` hands back the result event's payload."""
    payload = {"type": "result", "is_error": False, "result": "done"}
    events = [HarnessEvent(type="result", raw=payload)]

    assert extract_result(events) == payload


def test_extract_result_returns_nothing_when_there_is_no_result():
    """A stream that never reached its result event has no result."""
    events = list(parse_stream(['{"type":"assistant","message":{"content":[]}}']))

    assert extract_result(events) is None


def test_extract_result_finds_the_result_in_a_recorded_stream():
    """The recorded streams end with a result event."""
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "harness"
        / "claude_stream_json"
        / "single_turn.jsonl"
    )
    lines = fixture.read_text(encoding="utf-8").splitlines()

    events = list(parse_stream(lines))
    envelope = extract_result(events)

    assert envelope is not None
    assert envelope["type"] == "result"
    assert envelope == json.loads(lines[-1])


# ---------------------------------------------------------------------------
# 4. The adapter answers the same question
# ---------------------------------------------------------------------------


def test_the_adapter_classifies_the_same_way():
    """``ClaudeHarness.classify_exit`` is the same classifier."""
    events = list(parse_stream(['{"type":"result","is_error":true}']))

    from_adapter = ClaudeHarness().classify_exit(0, events)
    from_function = classify_exit(0, events)

    assert from_adapter.kind == from_function.kind
    assert from_adapter.kind == "model_error"


def test_a_signalled_run_names_the_signal():
    """A signalled exit says which signal ended the run."""
    assert "SIGINT" in classify_exit(130, []).message
    assert "SIGTERM" in classify_exit(143, []).message


def test_the_verdict_carries_the_exit_code_it_came_from():
    """The message says which code produced the verdict."""
    assert "124" in classify_exit(124, []).message


def test_the_events_are_not_consumed_by_classifying_them():
    """Classifying a run does not empty the stream it was given."""
    events: List[HarnessEvent] = list(parse_stream(['{"type":"result","is_error":true}']))

    classify_exit(1, events)

    assert len(events) == 1


# ---------------------------------------------------------------------------
# 5. Both adapters answer the same question the same way
# ---------------------------------------------------------------------------

#: The conversation a recorded mcode run happened in.
MCODE_SESSION_ID = "mvs_fb69527a6d6242d9bc1cc70c46ed71a2"


def _mcode_exec_completed(
    status: str = "succeeded", output: str = "OK"
) -> Dict[str, Any]:
    """The last event of a finished mcode run."""
    return {
        "type": "exec.completed",
        "sessionId": MCODE_SESSION_ID,
        "result": {
            "type": "exec.result",
            "status": status,
            "output": output,
            "model": {
                "providerId": "minimax",
                "modelId": "MiniMax-M3.1-Flash",
            },
            "usage": {
                "inputTokens": 16271,
                "outputTokens": 3,
                "cacheReadTokens": 618,
                "totalTokens": 16274,
            },
        },
    }


def _mcode_events(*payloads: Dict[str, Any]) -> List[HarnessEvent]:
    """The events an mcode stream turns into."""
    from harness.mcode import McodeHarness

    lines = [json.dumps(payload) for payload in payloads]
    return list(McodeHarness().parse_stream(lines))


def _claude_events(*payloads: Dict[str, Any]) -> List[HarnessEvent]:
    """The events a claude stream turns into."""
    lines = [json.dumps(payload) for payload in payloads]
    return list(parse_stream(lines))


def test_mcode_and_claude_classify_exit_codes_identically_and_keep_unknown_envelopes():
    """The two adapters answer the same question the same way.

    The layer above a run does not know which CLI produced it, so the
    verdict it gets must not depend on that either: the same exit code
    has to mean the same thing to both adapters, out of the same closed
    set of kinds.

    Three things are pinned here:

    1. **The mcode adapter is a harness.** The protocol asks every
       adapter for a classifier; an adapter without one makes the runner
       fall back to its own rule, and a fallback that knows nothing
       about mcode sends every exit code to ``unknown`` -- which is how
       a model error and a timeout both came out as "something nobody
       describes".
    2. **A failed result is a model error to both.** mcode reports a
       failed run as an ``exec.completed`` whose status is not
       ``succeeded``; the CLI still exits zero, so the event has to
       have the last word on a zero exit.
    3. **An unmapped code keeps the envelope.** The verdict quotes the
       payload that came with the run -- for mcode, the ``exec.completed``
       event it was built from -- because that payload is the only thing
       left that says what happened.
    """
    from harness.contract import Harness
    from harness.mcode import McodeHarness

    mcode = McodeHarness()
    claude = ClaudeHarness()

    # -- the mcode adapter answers the protocol --------------------------
    assert isinstance(mcode, Harness), (
        "McodeHarness does not satisfy the Harness protocol: it has no "
        "classify_exit, so the runner falls back to a rule that knows "
        "nothing about mcode and every exit code lands in unknown"
    )

    # -- the same code means the same thing to both ----------------------
    mcode_events = _mcode_events(_mcode_exec_completed())
    claude_events = _claude_events(
        {"type": "result", "is_error": False, "result": "OK"}
    )

    for code in (0, 1, 2, 124, 130, 143, 9, -15):
        from_mcode = mcode.classify_exit(code, mcode_events)
        from_claude = claude.classify_exit(code, claude_events)

        assert from_mcode.kind == from_claude.kind, (
            f"exit code {code} meant {from_mcode.kind!r} to mcode and "
            f"{from_claude.kind!r} to claude"
        )
        assert from_mcode.kind in OUTCOME_KINDS, (
            f"exit code {code} was classified as {from_mcode.kind!r}"
        )

    # -- a failed result is a model error to both ------------------------
    mcode_failed = _mcode_events(
        _mcode_exec_completed(status="failed", output="")
    )
    assert mcode.classify_exit(0, mcode_failed).kind == "model_error"

    claude_failed = _claude_events({"type": "result", "is_error": True})
    assert claude.classify_exit(0, claude_failed).kind == "model_error"

    # -- an unmapped code keeps the envelope ------------------------------
    original = _mcode_exec_completed(status="failed", output="boom")
    unknown_events = _mcode_events(original)

    verdict = mcode.classify_exit(9, unknown_events)

    assert verdict.kind == "unknown", (
        f"an exit code nothing describes was classified as {verdict.kind!r}"
    )
    assert verdict.kind != "ok"

    envelope = unknown_events[-1].raw
    assert json.dumps(envelope, ensure_ascii=False, sort_keys=True) in (
        verdict.message
    ), (
        f"the verdict must keep the envelope, got {verdict.message!r}"
    )
    assert json.dumps(original, ensure_ascii=False, sort_keys=True) in (
        verdict.message
    ), (
        f"the verdict must quote the exec.completed payload it was built "
        f"from, got {verdict.message!r}"
    )
