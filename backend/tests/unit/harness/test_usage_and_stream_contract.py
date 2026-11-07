"""What a run reports about its usage, and what it reports about its stream.

Why this file exists
====================

Two facts about a run used to be lost between the adapter that knew
them and the layer that had to act on them.

**The usage count.** The mcode adapter knows which of the four counts
the CLI never reported — it read the payload. The runner used to
re-derive that from the exit code instead, so a run that ended cleanly
but reported two of the four counts was counted as complete, and the
number that understates the cost said nothing about it. The runner's
own rule looked at one field only, so a count that was missing its
*output* tokens or its *cache read* tokens was called complete too,
and a run that reported no usage at all was called complete as well.

**The stream.** ``multi_turn`` was a field of the request that nothing
ever read: no production code set it, no production code consumed it,
and a run that was asked for a conversation but produced only its
terminal envelope was reported as a successful single-turn run. The
check that would have caught it existed only in a test fixture, which
is why a round of review could call the guarantee hollow.

Both facts are carried end to end now. The adapter writes them onto
the event, the runner reads them off the event, and the production
dispatch asks for a stream and is refused when it does not get one.

What these tests pin
====================

* a count that is missing any of the four fields is partial, and a
  field that was never reported stays ``None`` — never zero;
* the marker the adapter wrote on the event is the marker the runner
  reports, and a clean exit does not erase it;
* the ``multi_turn`` fact travels from the request, through the
  adapter, onto the invocation — and the runner refuses what it
  cannot honour: a multi-turn run on a harness that cannot stream,
  and a multi-turn run that produced no stream at all;
* a single-turn run is unaffected: an envelope on its own is the
  whole answer of a single-turn task;
* and the production dispatch itself asks for a stream — the last
  test drives the real dispatch and watches it refuse an
  envelope-only run.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

import claude_sandbox
from harness.claude import ClaudeHarness
from harness.contract import (
    HarnessCapabilities,
    HarnessConfigError,
    HarnessEvent,
    HarnessInvocation,
    TaskInvocation,
)
from harness.mcode import McodeHarness, parse_stream
from harness.runner import Runner

#: The four fields of the canonical usage block. A count that carries
#: all four is complete; a count that is missing one of them is
#: partial, whatever the missing one is.
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: A usage payload the CLI reported in full.
COMPLETE_USAGE: Dict[str, Any] = {
    "inputTokens": 10,
    "outputTokens": 3,
    "cacheReadTokens": 4,
    "cacheCreationTokens": 5,
}

#: A usage payload the CLI reported in part: the output tokens and the
#: cache read tokens are missing.
PARTIAL_USAGE: Dict[str, Any] = {
    "inputTokens": 10,
    "cacheCreationTokens": 5,
}

#: The canonical usage the complete payload becomes.
COMPLETE_CANONICAL: Dict[str, Any] = {
    "input_tokens": 10,
    "output_tokens": 3,
    "cache_read_input_tokens": 4,
    "cache_creation_input_tokens": 5,
}

#: The canonical usage the partial payload becomes. The two fields the
#: CLI did not report stay ``None``: they are not zero.
PARTIAL_CANONICAL: Dict[str, Any] = {
    "input_tokens": 10,
    "output_tokens": None,
    "cache_read_input_tokens": None,
    "cache_creation_input_tokens": 5,
}


# ---------------------------------------------------------------------------
# The stream the mcode CLI speaks
# ---------------------------------------------------------------------------


def _item_completed(text: str = "DONE") -> Dict[str, Any]:
    """The event the agent's own message becomes."""
    return {
        "type": "item.completed",
        "sessionId": "mvs_probe",
        "item": {"type": "agent_message", "content": text},
    }


def _turn_completed(usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The event the finished turn becomes."""
    payload: Dict[str, Any] = {
        "type": "turn.completed",
        "sessionId": "mvs_probe",
        "model": {"providerId": "minimax", "modelId": "MiniMax-M3.1-Flash"},
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _exec_completed(usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The event the finished run ends with."""
    result: Dict[str, Any] = {"status": "succeeded", "output": "DONE"}
    if usage is not None:
        result["usage"] = usage
    return {"type": "exec.completed", "sessionId": "mvs_probe", "result": result}


def _mcode_events(*payloads: Dict[str, Any]) -> List[HarnessEvent]:
    """The events one mcode stream turns into."""
    return list(parse_stream(json.dumps(payload) for payload in payloads))


def _result_event(events: Sequence[HarnessEvent]) -> HarnessEvent:
    """The run's terminal event."""
    results = [event for event in events if event.type == "result"]
    assert len(results) == 1, f"the stream carried {len(results)} results"
    return results[0]


# ---------------------------------------------------------------------------
# A child that speaks the mcode stream
# ---------------------------------------------------------------------------

#: A child that writes the payloads it was handed and exits.
_CHILD = """
import json
import sys

for payload in json.loads(sys.argv[1]):
    sys.stdout.write(json.dumps(payload) + "\\n")
sys.stdout.flush()
"""

#: A child that leaves a mark behind, so that a test can tell whether
#: it was ever spawned.
_MARKING_CHILD = """
import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text("spawned", encoding="utf-8")
for payload in json.loads(sys.argv[2]):
    sys.stdout.write(json.dumps(payload) + "\\n")
sys.stdout.flush()
"""


def _child(
    *payloads: Dict[str, Any], exit_code: int = 0
) -> Tuple[str, ...]:
    """The command line of a child that speaks the mcode stream."""
    script = _CHILD + f"sys.exit({exit_code})\n"
    return (sys.executable, "-c", script, json.dumps(list(payloads)))


def _child_that_marks(
    marker: Path, *payloads: Dict[str, Any]
) -> Tuple[str, ...]:
    """The command line of a child that marks that it ran."""
    return (
        sys.executable,
        "-c",
        _MARKING_CHILD,
        str(marker),
        json.dumps(list(payloads)),
    )


@pytest.fixture
def unsandboxed(monkeypatch):
    """Run without the sandbox.

    A configured profile would wrap the command line in
    ``sandbox-exec``, and the child of these tests is the test
    suite's own interpreter. The sandbox is not what this file is
    about.
    """
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None
    )


# ---------------------------------------------------------------------------
# 1. A number that was never reported stays missing, and says so
# ---------------------------------------------------------------------------


def test_a_count_without_its_output_tokens_is_partial():
    """The output tokens were never reported, so the count is partial."""
    events = _mcode_events(_exec_completed(PARTIAL_USAGE))

    result = _result_event(events)
    assert result.usage == PARTIAL_CANONICAL
    assert result.raw["usage_partial"] is True


def test_a_count_without_its_cache_read_tokens_is_partial():
    """The cache read tokens were never reported either.

    The rule is about every field, not about the one field mcode
    happens never to report: a count that is missing its cache *read*
    tokens understates the cost exactly as much as one that is missing
    its cache *creation* tokens.
    """
    usage = dict(COMPLETE_USAGE)
    usage.pop("cacheReadTokens")

    events = _mcode_events(_exec_completed(usage))

    result = _result_event(events)
    assert result.usage["cache_read_input_tokens"] is None
    assert result.raw["usage_partial"] is True


def test_a_count_with_all_four_fields_is_not_partial():
    """A count that came back in full is not marked partial."""
    events = _mcode_events(_exec_completed(COMPLETE_USAGE))

    result = _result_event(events)
    assert result.usage == COMPLETE_CANONICAL
    assert result.raw["usage_partial"] is False


def test_a_run_that_reported_no_usage_at_all_is_partial():
    """No count is the most partial count there is."""
    events = _mcode_events(_exec_completed())

    result = _result_event(events)
    assert result.usage is None
    assert result.raw["usage_partial"] is True


def test_a_field_that_was_never_reported_is_never_zero():
    """Zero is a count that came back; ``None`` is one that did not."""
    events = _mcode_events(_exec_completed(PARTIAL_USAGE))

    result = _result_event(events)
    assert set(result.usage) == set(USAGE_FIELDS)
    for field in USAGE_FIELDS:
        assert result.usage[field] is None or isinstance(result.usage[field], int)
    assert result.usage["output_tokens"] is None
    assert result.usage["cache_read_input_tokens"] is None


# ---------------------------------------------------------------------------
# 2. The marker travels on the event
# ---------------------------------------------------------------------------


def test_the_event_carries_the_marker_the_adapter_wrote():
    """The flag is on the event, next to the usage it describes."""
    events = _mcode_events(_exec_completed(PARTIAL_USAGE))

    result = _result_event(events)
    assert result.usage_partial is True


def test_a_complete_count_carries_no_marker():
    """The flag says what the count is, not that a flag exists."""
    events = _mcode_events(_exec_completed(COMPLETE_USAGE))

    result = _result_event(events)
    assert result.usage_partial is False


def test_the_event_is_frozen_apart_from_its_raw():
    """A contract object that can be rewritten is a contract nobody can rely on."""
    event = HarnessEvent(type="result", raw={"type": "result"})

    with pytest.raises(FrozenInstanceError):
        event.usage_partial = True


# ---------------------------------------------------------------------------
# 3. The runner reads the marker instead of recomputing it
# ---------------------------------------------------------------------------


def test_a_clean_run_keeps_the_partial_marker_the_adapter_wrote(unsandboxed):
    """The run ended cleanly and still reported part of its usage.

    The adapter read the payload and knows that two of the four
    counts were never reported. The runner used to throw that away
    and re-derive the flag from the exit code, so a clean run was
    always counted as complete.
    """
    invocation = HarnessInvocation(
        argv=_child(
            _item_completed(),
            _turn_completed(PARTIAL_USAGE),
            _exec_completed(PARTIAL_USAGE),
        )
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0
    assert result.usage == PARTIAL_CANONICAL
    assert result.usage_partial is True


def test_a_clean_run_with_a_complete_count_is_not_partial(unsandboxed):
    """The control: a count that came back in full is not partial."""
    invocation = HarnessInvocation(
        argv=_child(
            _item_completed(),
            _turn_completed(COMPLETE_USAGE),
            _exec_completed(COMPLETE_USAGE),
        )
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0
    assert result.usage == COMPLETE_CANONICAL
    assert result.usage_partial is False


def test_a_failed_run_is_partial_even_with_a_complete_count(unsandboxed):
    """The one fact the adapter cannot know: the run did not end cleanly."""
    invocation = HarnessInvocation(
        argv=_child(
            _item_completed(),
            _turn_completed(COMPLETE_USAGE),
            _exec_completed(COMPLETE_USAGE),
            exit_code=3,
        )
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 3
    assert result.usage_partial is True


def test_the_run_reports_the_marker_on_its_last_event(unsandboxed):
    """The event the reader takes is the event that carries the mark."""
    invocation = HarnessInvocation(
        argv=_child(
            _item_completed(),
            _turn_completed(PARTIAL_USAGE),
            _exec_completed(PARTIAL_USAGE),
        )
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.events[-1].raw.get("usage_partial") is True
    assert result.events[-1].usage_partial is True


# ---------------------------------------------------------------------------
# 4. The invocation carries the multi-turn fact
# ---------------------------------------------------------------------------


def test_the_mcode_adapter_copies_the_multi_turn_fact():
    """The adapter is the author of the command line, so it says this."""
    request = TaskInvocation(prompt="p", multi_turn=True)

    invocation = McodeHarness().build_invocation(request)

    assert invocation.multi_turn is True


def test_the_claude_adapter_copies_the_multi_turn_fact():
    """The other adapter says it the same way."""
    request = TaskInvocation(prompt="p", multi_turn=True)

    invocation = ClaudeHarness().build_invocation(request)

    assert invocation.multi_turn is True


def test_a_single_turn_request_stays_single_turn():
    """The control: nothing is claimed that was not asked for."""
    request = TaskInvocation(prompt="p")

    assert McodeHarness().build_invocation(request).multi_turn is False
    assert ClaudeHarness().build_invocation(request).multi_turn is False


# ---------------------------------------------------------------------------
# 5. The runner refuses what it cannot honour
# ---------------------------------------------------------------------------


class _NoStreamHarness:
    """A harness that declares no stream."""

    name = "no-stream"

    def capabilities(self) -> HarnessCapabilities:
        """What this harness can do — which is not to stream."""
        return HarnessCapabilities(
            append_system_prompt=False,
            mcp_injection=False,
            tool_whitelist=False,
            stream_events=False,
            usage=False,
            session_create=False,
            session_resume=False,
        )

    def build_invocation(self, req: TaskInvocation) -> HarnessInvocation:
        """Never reached: the refusal happens before the spawn."""
        raise AssertionError("the harness must not be asked to build a command")

    def parse_stream(self, lines):
        """Never reached: the refusal happens before the spawn."""
        raise AssertionError("the harness must not be asked to parse a stream")

    def classify_exit(self, code, events):
        """Never reached: the refusal happens before the spawn."""
        raise AssertionError("the harness must not be asked to classify an exit")


def test_a_multi_turn_run_on_a_harness_that_cannot_stream_is_refused(
    tmp_path, unsandboxed
):
    """The refusal happens before the child exists.

    A harness that declares no stream cannot produce one, so the run
    is refused where the refusal is free: nothing was started, so
    nothing has to be cleaned up.
    """
    marker = tmp_path / "spawned"
    invocation = HarnessInvocation(
        argv=_child_that_marks(marker), multi_turn=True
    )

    with pytest.raises(HarnessConfigError) as excinfo:
        Runner().run(_NoStreamHarness(), invocation)

    assert "stream_events" in str(excinfo.value)
    assert not marker.exists(), "the child was spawned despite the refusal"


def test_a_multi_turn_run_that_produced_no_stream_is_refused(unsandboxed):
    """A multi-turn task that got an envelope and nothing else is a failure.

    The refusal names the capability that was missing and lists the
    event types the stream actually carried, because "it did not work"
    is not a diagnosis an operator can act on.
    """
    invocation = HarnessInvocation(
        argv=_child(_exec_completed(COMPLETE_USAGE)), multi_turn=True
    )

    with pytest.raises(HarnessConfigError) as excinfo:
        Runner().run(McodeHarness(), invocation)

    message = str(excinfo.value)
    assert "stream_events" in message
    assert "envelope" in message
    assert "result" in message


def test_a_single_turn_run_that_produced_only_an_envelope_is_allowed(
    unsandboxed,
):
    """The control: an envelope on its own answers a single-turn task."""
    invocation = HarnessInvocation(
        argv=_child(_exec_completed(COMPLETE_USAGE)), multi_turn=False
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0
    assert result.outcome.kind == "ok"
    assert [event.type for event in result.events] == ["result"]


def test_a_multi_turn_run_with_an_assistant_event_is_allowed(unsandboxed):
    """A stream that carried the conversation is what was asked for."""
    invocation = HarnessInvocation(
        argv=_child(
            _item_completed(),
            _turn_completed(COMPLETE_USAGE),
            _exec_completed(COMPLETE_USAGE),
        ),
        multi_turn=True,
    )

    result = Runner().run(McodeHarness(), invocation)

    assert result.exit_code == 0
    assert "assistant" in [event.type for event in result.events]


# ---------------------------------------------------------------------------
# 6. The production dispatch asks for a stream
# ---------------------------------------------------------------------------


def test_the_production_dispatch_refuses_a_run_with_no_stream(tmp_path, monkeypatch):
    """The judgement has to live in the production execution path.

    The dispatch below is the real one — the one every task goes
    through — and its fake CLI answers with the terminal envelope
    alone, which is what a run that never streamed looks like. The
    dispatch has to refuse it: a task that asked for a conversation
    and got an envelope is not a task that worked.

    A check that only lived in a test fixture would pass this file
    while the production path kept answering "ok" — so this test
    drives the production dispatch itself.
    """
    from unittest.mock import MagicMock, patch

    import coding_tool

    base = tmp_path / "base_settings.json"
    base.write_text(
        json.dumps({"env": {"PDT_PROJECT_DIR": "/somewhere"}, "hooks": {}}),
        encoding="utf-8",
    )

    process = MagicMock()
    process.stdin = MagicMock()
    process.stdout = iter(
        [json.dumps({"type": "result", "result": "ok", "is_error": False})]
    )
    process.stderr = MagicMock()
    process.wait.return_value = 0
    process.poll.return_value = 0

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANTHROPIC_")
    }
    env["PDT_PROVIDER_MODE"] = "inherit"

    tool = coding_tool.ClaudeCodingTool(settings=base)
    with patch.dict(os.environ, env, clear=True), patch(
        "coding_tool.subprocess.Popen", return_value=process
    ):
        with pytest.raises(HarnessConfigError) as excinfo:
            tool._run_claude_interactive("do something")

    message = str(excinfo.value)
    assert "stream_events" in message
    assert "envelope" in message
