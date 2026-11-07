"""What the production dispatch promises its run, and what the runner does about it.

Why this file exists
====================

The ``multi_turn`` fact used to be written down once, for every call:
the production dispatch declared *every* run a conversation, so the
runner's criterion — a run that promised a stream and produced only
its terminal envelope is refused — landed on every one-shot query as
well. The default claude path's own fixtures produce the terminal
envelope alone, and the suite's tests said so.

So the fact has to describe **this** call, and there are two kinds of
call:

* a **one-shot query** — one question, one answer. The terminal
  envelope *is* that answer, on every backend, and the dispatch has
  always read it from there. Such a run promises no stream, and a run
  that produced only its envelope is not a failed run.
* a **session-style run** — the call continues a conversation. On the
  backend whose conversation travels in the event stream, such a run
  has to produce that stream: the work of a continued conversation
  lives in the stream, and a run that ends with only its envelope is a
  ``stream_events`` capability failure — refused explicitly, never
  answered with a summary.

The promise is therefore made per call *and* per backend, and the
tests below say which is which — including the one case where the
promise is deliberately NOT made, so that the rule is stated in full
rather than half.

What these tests pin
====================

* the one-shot dispatch accepts an envelope-only run, and the verdict
  on that run is the verdict on a run that worked;
* a run that promised the stream is still refused when it produces
  only its envelope — the criterion is not switched off;
* the promise follows the call: a one-shot query promises nothing, and
  a session-style run promises the stream on the backend whose
  conversation lives in it;
* the mcode session-style run still fails explicitly when the stream
  is missing.

The runner is not stubbed
=========================

Every test here drives the **real** runner, so the verdict under test
is the one the runner produces and the refusal under test is the one
the runner raises. A stubbed runner would answer "the promise was
made" without ever reading a stream, and the criterion this file
protects is exactly the one a stub cannot check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import claude_sandbox  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from harness.contract import (  # noqa: E402
    HarnessConfigError,
    HarnessInvocation,
    OUTCOME_OK,
)
from harness.mcode import McodeHarness  # noqa: E402
from harness.runner import Runner  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The streams the children write
# ---------------------------------------------------------------------------

#: The id of a claude conversation, in the shape the CLI writes it: a
#: UUID.
_CLAUDE_SESSION_ID = "11111111-1111-1111-1111-111111111111"

#: The claude stream of a run that produced no assistant turn: the init
#: event and the terminal envelope. This is the shape the default
#: path's own fixtures produce, and it is a complete answer to a
#: one-shot question.
_CLAUDE_ENVELOPE_ONLY: Tuple[str, ...] = (
    json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": _CLAUDE_SESSION_ID,
    }),
    json.dumps({"type": "result", "result": "the summary", "is_error": False}),
)

#: The claude stream of a run that carried nothing but its terminal
#: envelope: the CLI never announced a session, never wrote a turn, and
#: ended the run with its summary. This is what a run that never
#: streamed looks like.
_CLAUDE_ENVELOPE_ALONE: Tuple[str, ...] = (
    json.dumps({"type": "result", "result": "the summary", "is_error": False}),
)

#: The mcode session id, in the shape the CLI mints it: the ``mvs_``
#: prefix and 32 hex digits. A resume that names anything else is
#: refused by the adapter before a child is ever spawned.
_MCODE_SESSION_ID = "mvs_0ecaf7d26ff845a19fbaf6ecd45285e9"

#: The mcode stream of a session-style run that carried no assistant
#: item: the session's own events and the run's terminal envelope.
_MCODE_ENVELOPE_ONLY: Tuple[str, ...] = (
    json.dumps({"type": "session.started", "sessionId": _MCODE_SESSION_ID}),
    json.dumps({
        "type": "exec.completed",
        "sessionId": _MCODE_SESSION_ID,
        "result": {"status": "succeeded", "output": "DONE"},
    }),
)

#: The mcode stream of a session-style run that carried the
#: conversation's turn: the agent's own message becomes an assistant
#: event, and the run ends with its envelope.
_MCODE_WITH_TURN: Tuple[str, ...] = (
    json.dumps({"type": "session.started", "sessionId": _MCODE_SESSION_ID}),
    json.dumps({
        "type": "item.completed",
        "sessionId": _MCODE_SESSION_ID,
        "item": {"type": "agent_message", "content": "DONE"},
    }),
    json.dumps({
        "type": "exec.completed",
        "sessionId": _MCODE_SESSION_ID,
        "result": {"status": "succeeded", "output": "DONE"},
    }),
)


# ---------------------------------------------------------------------------
# The child the dispatch spawns
# ---------------------------------------------------------------------------

#: A child that writes the lines it was handed and exits cleanly.
_CHILD = """
import json
import sys

for payload in json.loads(sys.argv[1]):
    sys.stdout.write(json.dumps(payload) + "\\n")
sys.stdout.flush()
"""


def _child(*payloads: Dict[str, Any]) -> Tuple[str, ...]:
    """The command line of a child that writes the payloads it was given.

    Args:
        payloads: The events the child writes, one per line.

    Returns:
        The command line.
    """
    return (sys.executable, "-c", _CHILD, json.dumps(list(payloads)))


def _fake_process(lines: Tuple[str, ...]) -> MagicMock:
    """A child process that already said what it had to say.

    The dispatch does not spawn anything itself — the runner does — so
    the child is put in the runner's way instead. The lines are the
    ones the child would have written.

    Args:
        lines: The lines the child wrote.

    Returns:
        The process the runner finds.
    """
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdout = iter(lines)
    process.stderr = MagicMock()
    process.wait.return_value = 0
    process.poll.return_value = 0
    return process


@pytest.fixture
def unsandboxed(monkeypatch):
    """Run without the sandbox.

    A configured profile would wrap the command line in
    ``sandbox-exec``, and the child of these tests is the test suite's
    own interpreter. The sandbox is not what this file is about.
    """
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None
    )


# ---------------------------------------------------------------------------
# The production dispatch
# ---------------------------------------------------------------------------


def _tool(monkeypatch, tmp_path: Path = None) -> ClaudeCodingTool:
    """A tool whose provider walk answers with nothing.

    The dispatch resolves a provider before it spawns. Which provider
    it would have picked is not what this file is about, so the walk is
    cut at its root — the same cut the dispatch-parity file makes.

    A tool built with ``tmp_path`` is a CONFIGURED one: it carries a
    settings file, which is what a task tool carries — the file that
    holds the run's hooks, its project and its provider. A tool built
    without one is a bare query tool.

    Args:
        monkeypatch: The fixture that puts the walk back.
        tmp_path: Where to write the settings file, when the tool is
            to be a configured one.

    Returns:
        The tool.
    """
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_load_provider_from_db",
        staticmethod(lambda *a, **k: {}),
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_check_provider_availability",
        staticmethod(lambda *a, **k: (False, {})),
    )
    if tmp_path is None:
        return ClaudeCodingTool()
    settings = tmp_path / "base_settings.json"
    settings.write_text(
        json.dumps({"env": {"PDT_PROJECT_DIR": "/somewhere"}, "hooks": {}}),
        encoding="utf-8",
    )
    return ClaudeCodingTool(settings=settings)


def _recorded_runs(monkeypatch) -> List[Dict[str, Any]]:
    """Let the real runner run, and record what it was asked for.

    The wrapper calls the runner that is really there, so the result it
    records is the one the real runner produced — the verdict on the
    run included. What it adds is the invocation the dispatch handed
    over, which is where the promise lives.

    Args:
        monkeypatch: The fixture that puts the runner back.

    Returns:
        The list the recorded runs land in.
    """
    runs: List[Dict[str, Any]] = []
    original = Runner.run

    def _run(self, harness, invocation, stdin_text=None):
        result = original(self, harness, invocation, stdin_text)
        runs.append({
            "harness": harness,
            "invocation": invocation,
            "result": result,
        })
        return result

    monkeypatch.setattr(Runner, "run", _run)
    return runs


def _dispatch(
    tool: ClaudeCodingTool,
    lines: Tuple[str, ...],
    **kwargs: Any,
) -> Tuple[str, Any]:
    """Run the production dispatch against a child that already spoke.

    The patch reaches the runner as well: ``coding_tool.subprocess`` is
    the :mod:`subprocess` module itself, so the child the runner spawns
    is the one handed to it here. That is the point — the run under
    test is a real run, read by the real runner.

    Args:
        tool: The tool that dispatches.
        lines: The lines the child wrote.
        kwargs: The rest of the dispatch's arguments.

    Returns:
        What the dispatch answered.
    """
    with patch(
        "coding_tool.subprocess.Popen", return_value=_fake_process(lines)
    ):
        return tool._run_claude_interactive("p", **kwargs)


# ---------------------------------------------------------------------------
# 1. A one-shot query is answered by the terminal envelope
# ---------------------------------------------------------------------------


def test_one_shot_dispatch_accepts_terminal_envelope(monkeypatch, unsandboxed):
    """The one-shot dispatch is not refused for producing an envelope.

    The dispatch is the production one and the child is the shape the
    default path's own fixtures produce: the init event and the
    terminal envelope, and no assistant turn at all. The caller asked
    one question and got one answer, so the run is a run that worked.
    """
    runs = _recorded_runs(monkeypatch)
    tool = _tool(monkeypatch)

    text, _session_id, _meta = _dispatch(tool, _CLAUDE_ENVELOPE_ONLY)

    assert len(runs) == 1, (
        f"the dispatch ran {len(runs)} times; it must run exactly once"
    )
    result = runs[0]["result"]

    assert result.outcome.kind == OUTCOME_OK, (
        f"the run was judged {result.outcome.kind!r}: a one-shot query "
        "that produced its terminal envelope is a run that worked"
    )
    assert text == "the summary", (
        f"the dispatch answered {text!r}; the answer is the envelope's"
    )


# ---------------------------------------------------------------------------
# 2. A run that promised the stream is still refused without one
# ---------------------------------------------------------------------------


def test_multi_turn_dispatch_still_refuses_envelope_only(unsandboxed):
    """The criterion is not switched off.

    The negative control. A run that promised the stream and produced
    only its terminal envelope is refused, and the refusal names what
    was missing — the whole point of the promise is that a run which
    cannot keep it says so instead of pretending.
    """
    invocation = HarnessInvocation(
        argv=_child(json.loads(_MCODE_ENVELOPE_ONLY[1])),
        multi_turn=True,
    )

    with pytest.raises(HarnessConfigError) as excinfo:
        Runner().run(McodeHarness(), invocation)

    message = str(excinfo.value)
    assert "stream_events 缺失" in message, (
        f"the refusal does not name what was missing: {message!r}"
    )
    assert "envelope" in message, (
        f"the refusal does not name the shape the run produced: {message!r}"
    )


# ---------------------------------------------------------------------------
# 3. The promise follows the call
# ---------------------------------------------------------------------------


def test_production_dispatch_declares_multi_turn_from_the_call(monkeypatch, unsandboxed):
    """What the dispatch promises is decided per call, not per layer.

    A one-shot query promises no stream: the terminal envelope is its
    answer, and the dispatch reads the answer from there — on either
    backend.

    A call that continues a conversation promises the stream on the
    backend whose conversation travels in the event stream — there, an
    envelope-only run has thrown the work of the conversation away.

    And on the backend whose answer is read from the terminal envelope,
    a call that continues a conversation promises no stream either:
    that call wants an answer, not the turns, and a promise it cannot
    keep would fail every call of that kind.
    """
    runs = _recorded_runs(monkeypatch)
    tool = _tool(monkeypatch)

    _dispatch(tool, _CLAUDE_ENVELOPE_ONLY)
    _dispatch(tool, _MCODE_WITH_TURN, harness=McodeHarness())
    _dispatch(
        tool,
        _MCODE_WITH_TURN,
        harness=McodeHarness(),
        session_id=_MCODE_SESSION_ID,
        resume=True,
    )
    _dispatch(
        tool,
        _CLAUDE_ENVELOPE_ONLY,
        session_id=_CLAUDE_SESSION_ID,
        resume=True,
    )

    assert len(runs) == 4, (
        f"the dispatch ran {len(runs)} times; it must run exactly four times"
    )
    one_shot, one_shot_other, session_run, session_run_envelope = runs

    assert one_shot["invocation"].multi_turn is False, (
        "the one-shot query promised a stream: the terminal envelope is "
        "its answer, and a promise it cannot keep fails every call"
    )
    assert one_shot_other["invocation"].multi_turn is False, (
        "the one-shot query on the other backend promised a stream: the "
        "terminal envelope is its answer there too"
    )
    assert session_run["invocation"].multi_turn is True, (
        "the session-style run promised no stream: the work of a "
        "continued conversation lives in the stream, and a run without "
        "one has to be refused rather than answered with a summary"
    )
    assert session_run_envelope["invocation"].multi_turn is False, (
        "the session-style run on the envelope-answering backend promised "
        "a stream: that call wants an answer, not the turns, and the "
        "promise would fail every call of that kind"
    )


# ---------------------------------------------------------------------------
# 4. The mcode session-style run still needs the stream
# ---------------------------------------------------------------------------


def test_mcode_session_run_still_requires_a_stream(monkeypatch, unsandboxed):
    """The mcode session-style run fails explicitly when the stream is missing.

    The production path, the mcode backend, a call that continues a
    conversation — and a child that produced nothing but its terminal
    envelope. The failure is explicit: the run is refused, not answered
    with a summary that pretends the conversation went well.
    """
    tool = _tool(monkeypatch)

    with pytest.raises(HarnessConfigError) as excinfo:
        _dispatch(
            tool,
            _MCODE_ENVELOPE_ONLY,
            harness=McodeHarness(),
            session_id=_MCODE_SESSION_ID,
            resume=True,
        )

    message = str(excinfo.value)
    assert "stream_events" in message, (
        f"the refusal does not name the missing capability: {message!r}"
    )


# ---------------------------------------------------------------------------
# 5. The dispatch that reads the conversation refuses a run that never streamed
# ---------------------------------------------------------------------------


def test_the_conversation_dispatch_refuses_a_run_that_never_streamed(
    monkeypatch, unsandboxed, tmp_path
):
    """A run that carried only its envelope is not a conversation.

    The dispatch reads the answer out of the run's CONVERSATION — the
    assistant's own turns — and falls back to the envelope's summary
    only when there were none. A run that carried nothing but that
    envelope never streamed the conversation, so the dispatch is
    refused rather than answered with the summary: the summary is not
    the conversation, and a caller that asked for one must not be
    handed the other.

    The tool here is a CONFIGURED one — it carries the settings file a
    task run carries — so this is the dispatch of a task, and the work
    of a task is the conversation it had.

    The refusal names what was missing and what the run produced
    instead, because "it did not work" is not a diagnosis an operator
    can act on.
    """
    tool = _tool(monkeypatch, tmp_path)

    with pytest.raises(HarnessConfigError) as excinfo:
        _dispatch(tool, _CLAUDE_ENVELOPE_ALONE)

    message = str(excinfo.value)
    assert "stream_events" in message, (
        f"the refusal does not name the missing capability: {message!r}"
    )
    assert "envelope" in message, (
        f"the refusal does not name the shape the run produced: {message!r}"
    )


def test_the_answer_api_is_served_by_the_terminal_envelope(
    monkeypatch, unsandboxed, tmp_path
):
    """The control: the answer API asked one question and got one answer.

    ``query()`` is a different contract from the conversation dispatch:
    it asks one question and wants one answer, and the terminal
    envelope is that answer on every backend. A run that carried only
    its envelope is therefore a run that worked, and the dispatch must
    not refuse it.

    The tool here is the SAME configured tool the refusal above used,
    and the stream is the same one: what tells the two apart is the
    call, not the run. Without this control the refusal above could be
    satisfied by refusing every envelope-only run — including the ones
    the default path's own fixtures produce, which is how the default
    path was broken in the first place.
    """
    tool = _tool(monkeypatch, tmp_path)

    with patch(
        "coding_tool.subprocess.Popen",
        return_value=_fake_process(_CLAUDE_ENVELOPE_ALONE),
    ):
        text = tool.query("p")

    assert text == "the summary", (
        f"the answer API answered {text!r}; the envelope is the answer"
    )


def test_a_bare_call_is_served_by_the_terminal_envelope(monkeypatch, unsandboxed):
    """The other control: a bare call asked one question of the CLI.

    A tool with no settings file, no project and no hooks of its own is
    a query tool, and the receipt of the run is the answer to the one
    question it asked. The run carried nothing but that receipt, and it
    worked.

    This is the shape the default path's own fixtures produce, and it
    is the shape the provider-walk tests of this codebase dispatch
    against; refusing it here is how the default path was broken in the
    first place.
    """
    tool = _tool(monkeypatch)

    text, _session_id, _meta = _dispatch(tool, _CLAUDE_ENVELOPE_ALONE)

    assert text == "the summary", (
        f"the bare call answered {text!r}; the receipt is the answer"
    )
