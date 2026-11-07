"""TDD spec for :mod:`harness.claude` — the claude adapter's argv construction.

What is pinned here
-------------------

The claude command line used to be assembled inline in
``coding_tool.ClaudeCodingTool._run_claude_interactive``. It is now
built by :class:`harness.claude.ClaudeHarness`, and this file is the
spec that says the move changed nothing.

The contract points pinned here:

1. **The order of the flags is fixed.** ``base -> append-system-prompt
   -> model -> mcp-config -> session -> allowedTools``. A flag that
   moved one slot to the left would still parse, and would still run the
   same model — but the argv would no longer be the argv the pre-
   refactor baseline recorded, which is the thing the refactor has to
   reproduce byte for byte.
2. **The base command line is always the same six elements.** The
   prompt is *not* one of them: the claude dispatch is interactive, the
   prompt arrives on stdin.
3. **The session flags follow the mode.** ``CREATE`` pins the new
   conversation with ``--session-id``; ``RESUME`` re-opens an existing
   one with ``--resume``. A request that names no conversation gets
   neither.
4. **The optional flags are absent when they have nothing to say.** An
   empty tool whitelist emits no ``--allowedTools``; a request with no
   system prompt, model or MCP config emits none of those either.
5. **``--settings`` is not this adapter's job.** The settings file is
   written by the wiring that owns it, and the flag is appended there.
6. **The adapter adds nothing to the environment.** ``env_delta`` is
   empty: the claude CLI is spawned with the environment it inherits.

The adapter is a pure function of the request — it starts no process,
reads no file and writes nothing (test decision point 2 — pure
transformation functions are not stubbed). So these tests construct
:class:`harness.contract.TaskInvocation` directly and compare the
argv it produces.

The last test in this file is the strongest one: it replays the eight
inputs of the pre-refactor baseline (``claude_argv_golden.json``,
captured from the old inline code) through the new adapter and requires
the same argv back.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from harness.claude import ClaudeHarness
from harness.contract import SessionMode, SessionRef, TaskInvocation

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The command line the adapter always starts with
# ---------------------------------------------------------------------------

#: The six elements every claude command line starts with.
BASE_ARGV = (
    "claude",
    "--verbose",
    "--output-format",
    "stream-json",
    "--permission-mode",
    "bypassPermissions",
)


def _full_request() -> TaskInvocation:
    """A request that exercises every optional flag at once."""
    return TaskInvocation(
        prompt="p",
        system_instruction="S",
        model="m1",
        mcp_config_path="/tmp/m.json",
        allowed_tools=("Bash", "Read"),
        session=SessionRef(pinned_session_id="u1"),
        mode=SessionMode.RESUME,
        cwd="/w",
    )


# ---------------------------------------------------------------------------
# 1. The order of the flags
# ---------------------------------------------------------------------------


def test_argv_order():
    """A request with every field produces the argv in the fixed order."""
    invocation = ClaudeHarness().build_invocation(_full_request())

    assert invocation.argv == (
        "claude",
        "--verbose",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        "S",
        "--model",
        "m1",
        "--mcp-config",
        "/tmp/m.json",
        "--resume",
        "u1",
        "--allowedTools",
        "Bash,Read",
    )


def test_no_optional_flags():
    """A request with nothing optional gets the bare command line."""
    invocation = ClaudeHarness().build_invocation(TaskInvocation(prompt="p"))

    assert invocation.argv == BASE_ARGV


# ---------------------------------------------------------------------------
# 2. The session flags follow the mode
# ---------------------------------------------------------------------------


def test_create_uses_session_id():
    """Creating a conversation pins it with ``--session-id``."""
    req = TaskInvocation(
        prompt="p",
        session=SessionRef(pinned_session_id="u1"),
        mode=SessionMode.CREATE,
    )

    argv = ClaudeHarness().build_invocation(req).argv

    assert "--session-id" in argv
    assert "--resume" not in argv
    assert argv[argv.index("--session-id") + 1] == "u1"


def test_resume_uses_resume():
    """Re-opening a conversation uses ``--resume``."""
    req = TaskInvocation(
        prompt="p",
        session=SessionRef(pinned_session_id="u1"),
        mode=SessionMode.RESUME,
    )

    argv = ClaudeHarness().build_invocation(req).argv

    assert "--resume" in argv
    assert "--session-id" not in argv
    assert argv[argv.index("--resume") + 1] == "u1"


def test_no_session_means_no_session_flags():
    """A request that names no conversation gets neither flag."""
    argv = ClaudeHarness().build_invocation(TaskInvocation(prompt="p")).argv

    assert "--session-id" not in argv
    assert "--resume" not in argv


def test_a_session_without_an_id_gets_no_session_flags():
    """A flag with no value behind it would break the command line."""
    req = TaskInvocation(prompt="p", session=SessionRef(), mode=SessionMode.RESUME)

    argv = ClaudeHarness().build_invocation(req).argv

    assert "--session-id" not in argv
    assert "--resume" not in argv


# ---------------------------------------------------------------------------
# 3. The optional flags are absent when they have nothing to say
# ---------------------------------------------------------------------------


def test_empty_allowed_tools_omits_the_flag():
    """No tool whitelist means the default set, not an empty one."""
    argv = ClaudeHarness().build_invocation(TaskInvocation(prompt="p")).argv

    assert "--allowedTools" not in argv


def test_missing_allowed_tools_omits_the_flag():
    """``allowed_tools`` may arrive as ``None`` rather than an empty tuple."""
    req = TaskInvocation(prompt="p", allowed_tools=None)

    argv = ClaudeHarness().build_invocation(req).argv

    assert "--allowedTools" not in argv


def test_allowed_tools_are_one_comma_joined_argument():
    """The whitelist is a single argument, not one argument per tool."""
    req = TaskInvocation(prompt="p", allowed_tools=("Bash", "Read"))

    argv = ClaudeHarness().build_invocation(req).argv

    assert argv[-2:] == ("--allowedTools", "Bash,Read")


def test_settings_path_is_not_spliced_here():
    """``--settings`` belongs to the wiring that writes the file."""
    req = TaskInvocation(prompt="p", settings_path="/tmp/settings.json")

    argv = ClaudeHarness().build_invocation(req).argv

    assert "--settings" not in argv


def test_mcp_config_is_used_when_no_path_is_given():
    """The inline MCP configuration is the fallback for the path."""
    req = TaskInvocation(prompt="p", mcp_config="/tmp/inline.json")

    argv = ClaudeHarness().build_invocation(req).argv

    assert argv[argv.index("--mcp-config") + 1] == "/tmp/inline.json"


def test_the_mcp_config_path_wins_over_the_inline_configuration():
    """A path is the more specific of the two, so it is the one used."""
    req = TaskInvocation(
        prompt="p", mcp_config="/tmp/inline.json", mcp_config_path="/tmp/m.json"
    )

    argv = ClaudeHarness().build_invocation(req).argv

    assert argv[argv.index("--mcp-config") + 1] == "/tmp/m.json"
    assert "/tmp/inline.json" not in argv


# ---------------------------------------------------------------------------
# 4. What the invocation carries besides the argv
# ---------------------------------------------------------------------------


def test_env_delta_is_empty():
    """The claude CLI runs with the environment it inherits."""
    invocation = ClaudeHarness().build_invocation(_full_request())

    assert invocation.env_delta == {}


def test_cwd_is_the_one_the_request_names():
    """The working directory travels with the request."""
    invocation = ClaudeHarness().build_invocation(_full_request())

    assert invocation.cwd == "/w"


def test_cwd_is_empty_when_the_request_names_none():
    """No working directory means "inherit the runner's"."""
    invocation = ClaudeHarness().build_invocation(TaskInvocation(prompt="p"))

    assert invocation.cwd is None


# ---------------------------------------------------------------------------
# 5. What the adapter says about itself
# ---------------------------------------------------------------------------


def test_the_adapter_names_itself_claude():
    """The backend this adapter runs."""
    assert ClaudeHarness.name == "claude"
    assert ClaudeHarness().name == "claude"


def test_capabilities_describe_the_claude_cli():
    """Every flag the adapter can emit is a capability it declares."""
    caps = ClaudeHarness().capabilities()

    assert caps.append_system_prompt is True
    assert caps.mcp_injection is True
    assert caps.tool_whitelist is True
    assert caps.stream_events is True
    assert caps.usage is True
    assert caps.session_create is True
    assert caps.session_resume is True


# ---------------------------------------------------------------------------
# 6. The pre-refactor baseline, replayed through the new adapter
# ---------------------------------------------------------------------------

# ``backend/tests/unit/harness/`` -> the harness fixtures live two
# levels up, next to the other fixture trees.
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "harness"
_GOLDEN_PATH = _FIXTURE_DIR / "claude_argv_golden.json"
_CAPTURE_PATH = _FIXTURE_DIR / "capture_claude_baseline.py"


def _load_capture_module():
    """Import the capture module by path.

    It lives in a fixture directory rather than on ``sys.path``, and it
    is a generator, not a test module -- so it is loaded explicitly
    instead of being imported by name.
    """
    spec = importlib.util.spec_from_file_location(
        "pdt_capture_claude_baseline", _CAPTURE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _golden() -> dict:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _without_settings(argv):
    """Drop the ``--settings`` flag and its value from a captured argv.

    The settings file is written by the wiring that owns it, and the
    flag is appended there -- not by this adapter. The baseline was
    captured before that split, so the flag is still in it.
    """
    trimmed = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--settings":
            skip_next = True
            continue
        trimmed.append(item)
    return trimmed


def test_the_adapter_reproduces_the_captured_baseline():
    """The eight pre-refactor inputs produce the argv they used to.

    This is the byte-for-byte check: the baseline was captured from the
    inline argv construction before it moved into the adapter, so an
    adapter that reproduces all eight of them reproduces the command
    line the code used to spawn.
    """
    module = _load_capture_module()
    golden = {case["name"]: case for case in _golden()["cases"]}
    mcp_path = str(module.SCRATCH_ROOT / "workdir" / "mcp.json")

    for spec in module.CASES:
        req = TaskInvocation(
            prompt="",
            system_instruction=spec["sys"],
            model="m1" if spec["model"] else None,
            mcp_config_path=mcp_path if spec["mcp"] else None,
            allowed_tools=tuple(spec["tools"]) if spec["tools"] else (),
            session=SessionRef(pinned_session_id=module.SESSION_ID),
            mode=(
                SessionMode.RESUME
                if spec["mode"] == "resume"
                else SessionMode.CREATE
            ),
        )

        argv = ClaudeHarness().build_invocation(req).argv

        assert list(argv) == _without_settings(golden[spec["name"]]["argv"]), (
            f"the argv for {spec['name']!r} no longer matches the baseline"
        )
