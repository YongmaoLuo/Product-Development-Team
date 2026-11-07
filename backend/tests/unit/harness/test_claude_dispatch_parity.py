"""The claude dispatch must go through the harness layer — and change nothing.

What is pinned here
-------------------
Three contracts, in the order they matter:

1. **The dispatch asks the runner to run.** ``_run_claude_interactive``
   used to assemble the command line, wrap it in a sandbox and spawn it
   itself. It now hands a :class:`~harness.contract.HarnessInvocation`
   to :class:`~harness.runner.Runner` and reads the answer out of the
   result the runner answers with. This file is the gate that says so:
   the runner is stubbed, and the stub records what it was handed.

2. **The command line is the one that was there before.** The argv the
   dispatch builds is compared against the baseline captured from the
   pre-refactor construction
   (``backend/tests/fixtures/harness/claude_argv_golden.json``) — first
   the six elements every claude command line starts with, then one
   whole baseline case, byte for byte.

3. **The spawn is not in this module any more.** ``subprocess.Popen`` is
   the thing that used to be inlined here, so the source of
   ``coding_tool.py`` must not mention it: a second spawn path cannot
   grow back in here unnoticed.

Why the runner is stubbed rather than driven
--------------------------------------------
The question this file asks is "which object does the dispatch hand the
command line to, and what does it read the answer out of". A real child
process would answer a different question — "does a claude CLI still
work" — and it would answer it by accident: the assertions below would
hold whether the dispatch went through the runner or spawned the child
itself. The stub makes the seam visible. If the dispatch stops using the
runner, the stub is never called and the first test fails.

Why the answer comes out of the events
--------------------------------------
The stub answers with the events of one finished conversation, in the
shape the claude adapter produces them, and the dispatch is expected to
read the text and the session id out of them. That is the contract the
harness layer offers: the layer above the adapter consumes the
*normalized* stream, not a private re-parse of the raw lines.

The stream the stub answers with is deliberately not self-consistent:
the assistant says ``hello`` and the result event says something else.
A dispatch that took its answer from the result event instead of the
assistant's text would answer ``"the summary"`` — and the old code did
not do that, so neither may this one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import coding_tool
from coding_tool import ClaudeCodingTool
from harness.claude import parse_stream
from harness.contract import (
    HarnessInvocation,
    HarnessOutcome,
    HarnessResult,
    OUTCOME_OK,
)
from harness.mcode import McodeHarness
from harness.runner import Runner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------

#: ``backend/tests/unit/harness/`` -> the harness fixtures live two
#: levels up, next to the other fixture trees.
_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "harness" / "claude_argv_golden.json"
)

#: The conversation the recorded stream belongs to. A fixed, obviously
#: synthetic id: the value is part of the expectation, so it has to be
#: the same on every machine.
_SESSION_ID = "11111111-1111-1111-1111-111111111111"

#: The working directory the baseline case was captured in. It travels in
#: the comparison — a command line that is right but runs in the wrong
#: directory is still a change — and it is only ever carried as a
#: string, because the runner is stubbed and nothing is spawned.
_CAPTURED_CWD = "/tmp/pdt-claude-baseline/workdir"

#: The stream a claude CLI writes for one turn: the init event, the
#: assistant's text, and the result that ends the conversation.
#:
#: The result event carries a *different* text than the assistant did,
#: on purpose — see the module docstring.
_STREAM_LINES: Tuple[str, ...] = (
    json.dumps({"type": "system", "subtype": "init", "session_id": _SESSION_ID}),
    json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }),
    json.dumps({"type": "result", "result": "the summary", "is_error": False}),
)

#: What the assistant said — the answer the dispatch owes.
_ANSWER = "hello"


def _golden() -> Dict[str, Any]:
    """The baseline, read from disk.

    A missing baseline is a failure, not a skip: a skipped gate is
    indistinguishable from a passing one.
    """
    if not _GOLDEN_PATH.is_file():
        pytest.fail(
            f"the claude argv baseline is missing: {_GOLDEN_PATH}. A "
            "baseline that is not there cannot prove that moving the "
            "dispatch changed nothing."
        )
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _golden_case(name: str) -> Dict[str, Any]:
    """One baseline case, by name.

    Args:
        name: The case's name.

    Returns:
        The case — its argv and its working directory.
    """
    for case in _golden()["cases"]:
        if case["name"] == name:
            return case
    pytest.fail(
        f"the baseline has no case named {name!r}; it holds "
        f"{[case['name'] for case in _golden()['cases']]}"
    )


# ---------------------------------------------------------------------------
# The harness the dispatch runs against
# ---------------------------------------------------------------------------


def _tool(monkeypatch, **kwargs: Any) -> ClaudeCodingTool:
    """A tool whose provider walk resolves nothing.

    The walk is not what this file is about, and letting it reach the
    network would make the file slow and flaky. Both hooks are cut at
    the root: no provider is loaded, and no provider is reachable.

    Args:
        monkeypatch: The fixture that undoes the cuts afterwards.
        kwargs: The tool's own constructor arguments.

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
    return ClaudeCodingTool(**kwargs)


def _recorded_runner(monkeypatch) -> List[Dict[str, Any]]:
    """Replace the runner with a recorder that answers like the CLI.

    The stub plays the part the runner plays: it is handed the harness
    and the invocation, and it answers with the events of one finished
    conversation. What it records is what the dispatch handed over.

    Args:
        monkeypatch: The fixture that puts the real runner back.

    Returns:
        The list the recorded calls land in.
    """
    calls: List[Dict[str, Any]] = []

    def _run(self, harness, invocation, stdin_text=None, **kwargs):
        calls.append({
            "harness": harness,
            "invocation": invocation,
            "stdin": stdin_text,
        })
        return HarnessResult(
            exit_code=0,
            events=tuple(parse_stream(_STREAM_LINES)),
            final_text=_ANSWER,
            session_id=_SESSION_ID,
            sandbox_state="unsandboxed",
            harness="claude",
            outcome=HarnessOutcome(kind=OUTCOME_OK),
        )

    monkeypatch.setattr(Runner, "run", _run)
    return calls


# ---------------------------------------------------------------------------
# 1. The dispatch goes through the runner
# ---------------------------------------------------------------------------


def test_dispatch_goes_through_runner(monkeypatch):
    """The command line is handed to the runner, and the answer is read
    out of what the runner answered with."""
    calls = _recorded_runner(monkeypatch)
    tool = _tool(monkeypatch)

    text, session_id, _meta = tool._run_claude_interactive(
        "p", system_instruction="S", resume=False,
    )

    assert len(calls) == 1, (
        "the dispatch did not run through the runner exactly once — it ran "
        f"{len(calls)} times"
    )
    call = calls[0]

    assert isinstance(call["invocation"], HarnessInvocation), (
        "the dispatch handed the runner a "
        f"{type(call['invocation']).__name__}, not a HarnessInvocation"
    )
    assert call["harness"] is coding_tool.current_harness(), (
        "the dispatch ran against a different harness than the one this "
        "process drives"
    )

    # The answer is the assistant's text, not the result event's summary.
    assert text == _ANSWER, (
        f"the dispatch answered {text!r}; the answer comes out of the run's "
        "events, and the assistant's text is what the old path returned"
    )
    assert session_id == _SESSION_ID, (
        f"the dispatch answered with session {session_id!r}; the session id "
        "comes out of the run's init event"
    )


# ---------------------------------------------------------------------------
# 2. The command line is the one that was there before
# ---------------------------------------------------------------------------


def test_argv_unchanged_for_claude(monkeypatch):
    """The command line still starts with the six elements it always did."""
    calls = _recorded_runner(monkeypatch)
    tool = _tool(monkeypatch)

    tool._run_claude_interactive("p", system_instruction="S", resume=False)

    argv = list(calls[0]["invocation"].argv)
    base = list(_golden_case("create_plain")["argv"][:6])

    assert argv[:6] == base, (
        f"the command line starts {argv[:6]!r}; the baseline starts {base!r}"
    )


def test_the_whole_command_line_matches_the_baseline(monkeypatch):
    """One baseline case, replayed through the dispatch, byte for byte.

    The six-element check above says the command line has the right
    *shape*. This one says it is the *same* command line: every flag, in
    the same order, with the same value, run in the same directory.
    """
    calls = _recorded_runner(monkeypatch)
    tool = _tool(monkeypatch, cwd=_CAPTURED_CWD)

    tool._run_claude_interactive("p", session_id=_SESSION_ID, resume=False)

    case = _golden_case("create_plain")
    invocation = calls[0]["invocation"]

    assert list(invocation.argv) == list(case["argv"]), (
        f"the command line is {list(invocation.argv)!r}; the baseline's is "
        f"{list(case['argv'])!r}"
    )
    assert invocation.cwd == case["cwd"], (
        f"the command line runs in {invocation.cwd!r}; the baseline's runs "
        f"in {case['cwd']!r}"
    )


# ---------------------------------------------------------------------------
# 3. The spawn is not in this module any more
# ---------------------------------------------------------------------------


def test_no_popen_in_coding_tool():
    """``coding_tool`` does not spawn a process itself any more.

    The runner is the one place a harness process is started. A second
    spawn path in this module would be a second set of bugs — the kind
    that only shows up under a sandbox, or under a timeout, or on the
    one machine whose environment differs.
    """
    source = Path(coding_tool.__file__).read_text(encoding="utf-8")

    assert "subprocess.Popen" not in source, (
        "coding_tool still spawns a process itself; the runner is the one "
        "place a harness process is started"
    )


# ---------------------------------------------------------------------------
# 4. The claude-only settings file stays on the claude path
# ---------------------------------------------------------------------------


def test_the_other_backend_gets_no_claude_settings_file(monkeypatch, tmp_path):
    """A dispatch that drives the other backend never writes the file.

    The settings file is the claude CLI's own convention: a JSON file of
    environment overrides it reads at start-up. The other CLI has no such
    flag, and a command line that carries one is a command it refuses —
    so a dispatch that drives it must neither write the file nor put
    the flag on the command line.

    The file is not written either: it carries the routed provider's
    credentials and the guard hook's configuration, and a file no CLI
    will read is a secret written down for nothing.
    """
    calls = _recorded_runner(monkeypatch)
    tool = _tool(monkeypatch, settings=str(_settings_template(tmp_path)))

    written: List[Path] = []
    monkeypatch.setattr(
        coding_tool,
        "write_private_json",
        lambda path, payload: written.append(Path(path)),
    )

    tool._run_claude_interactive("p", harness=McodeHarness())

    argv = list(calls[0]["invocation"].argv)
    assert "--settings" not in argv, (
        f"the command line carries the claude-only settings flag: {argv!r}"
    )
    assert written == [], (
        f"the claude settings file was written for a dispatch that does "
        f"not read it: {[str(p) for p in written]}"
    )


def _settings_template(tmp_path: Path) -> Path:
    """A settings template on disk, the way the claude path is given one.

    Args:
        tmp_path: The directory the template is written into.

    Returns:
        The path of the template.
    """
    path = tmp_path / "subagent_settings.json"
    path.write_text(
        json.dumps({"env": {"ANTHROPIC_MODEL": "x"}, "hooks": {}}),
        encoding="utf-8",
    )
    return path

