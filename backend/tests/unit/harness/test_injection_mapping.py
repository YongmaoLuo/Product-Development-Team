"""TDD spec for the injection mapping — three channels, judged by effect.

What is pinned here
-------------------

A request carries three things that change what a run may do: a system
instruction, the tools the run may use, and a set of MCP servers. Each
of them has exactly one channel, and a channel is judged by what it
*does* — never by whether a flag with the right name happens to sit on
the command line. A flag that is there and does nothing is worse than
a flag that is absent: the request asked for a restriction, the run
got none, and nothing said so.

Two channels are pinned here. The third — MCP — has a file of its own,
because it is judged by what the CLI *reports* rather than by what the
command line says.

**The system instruction.** It travels in mcode's system-level
parameter, ``--append-system-prompt``, and nowhere else. The forbidden
shortcut is to concatenate it into the user prompt: a prompt that
carries its own system instruction is a *user turn that asks* for it,
and the model is free to read it as text to be discussed rather than
as an instruction to be obeyed. The observable is the byte stream the
child reads on standard input — it must be the user's text, byte for
byte, with the system instruction nowhere in it.

**The tool whitelist.** mcode has no allowlist mechanism at all. No
flag narrows the tool set, and ``--permission full`` — which is always
there — grants every tool and restricts none. So the whitelist channel
has no effect on this backend and its verdict is *false*: a fact about
the backend, not a guess, and not "unknown" either. A run that needs
the whitelist is then refused by name, because a dispatch that was
asked to be read-only must not become read-write because the backend
it runs on spells the flag differently.

The observable is the command line the adapter produces and — for the
system instruction — the bytes the child actually reads. The CLI is a
stand-in written by the test: a real process, so the bytes are real
bytes, but a hermetic one (a temporary directory, no network, no
model).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence, Set

import pytest

import claude_sandbox
import coding_tool
from harness.claude import ClaudeHarness
from harness.contract import HarnessConfigError, HarnessInvocation, TaskInvocation
from harness.mcode import MCODE_BINARY, McodeHarness
from harness.runner import Runner

pytestmark = pytest.mark.unit


#: The user's own text — the only thing that may reach the model as a
#: user turn.
USER_PROMPT = "用户原文"

#: The system instruction — text that has to reach the model as an
#: instruction, and not as part of the user's turn.
SYSTEM_INSTRUCTION = "系统约束"

#: The tools a restricted dispatch asks for.
ALLOWED_TOOLS = ("Bash",)

#: An MCP configuration, with one server named so a test can look for
#: it by name.
MCP_CONFIG = {"mcpServers": {"demo": {"command": "demo-server"}}}

#: A stand-in for the mcode CLI. It reads its prompt from standard
#: input — the way ``mcode exec --input -`` does — records the bytes it
#: was given, records the arguments it was handed, and answers with a
#: minimal event stream.
#:
#: ``__PYTHON__`` is replaced with the interpreter running the test, so
#: the stand-in works on a machine whose ``python3`` is somewhere else.
_STAND_IN = """#!__PYTHON__
import json
import os
import sys

with open(os.environ["PDT_STAND_IN_STDIN"], "wb") as handle:
    handle.write(sys.stdin.buffer.read())

argv_record = os.environ.get("PDT_STAND_IN_ARGV")
if argv_record:
    with open(argv_record, "w", encoding="utf-8") as handle:
        json.dump(list(sys.argv), handle)

print(json.dumps({"type": "exec.started", "session_id": "mvs_" + "0" * 32}))
print(json.dumps({"type": "exec.completed",
                  "result": {"status": "succeeded", "output": "done"}}))
"""


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    """A throwaway workspace — where the MCP channel writes its file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _request(workspace: Path) -> TaskInvocation:
    """The request from the brief: all three channels at once."""
    return TaskInvocation(
        prompt=USER_PROMPT,
        system_instruction=SYSTEM_INSTRUCTION,
        allowed_tools=ALLOWED_TOOLS,
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )


def _stand_in_script() -> str:
    """The stand-in CLI, with the interpreter filled in."""
    return _STAND_IN.replace("__PYTHON__", sys.executable)


def _install_stand_in(tmp_path: Path, monkeypatch, script: str) -> Path:
    """Put a stand-in ``mcode`` on the PATH.

    The stand-in is a real executable in a temporary directory, so what
    the runner writes to the child's standard input is what a real child
    would read. Nothing outside ``tmp_path`` is touched.

    Args:
        tmp_path: The test's temporary directory.
        monkeypatch: The test's monkeypatch.
        script: What the stand-in does.

    Returns:
        The directory the stand-in was installed in.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    binary = bin_dir / MCODE_BINARY
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def _run(harness, invocation: HarnessInvocation, prompt: str, monkeypatch):
    """Run the invocation against the stand-in and report what happened.

    The sandbox is switched off for the run: a configured profile would
    wrap the command line in ``sandbox-exec``, and the stand-in lives in
    a temporary directory that a profile has no reason to let through.
    The sandbox is not what this file is about.

    Args:
        harness: The adapter that builds the command line.
        invocation: The command line to run.
        prompt: The text the child reads on standard input.
        monkeypatch: The test's monkeypatch.

    Returns:
        The result of the run.
    """
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None,
    )
    return Runner().run(harness, invocation, stdin_text=prompt)


def _served_tools(invocation: HarnessInvocation) -> Optional[Set[str]]:
    """The tools the command line actually restricts the run to.

    ``None`` means the run is not restricted at all: every tool the
    backend has is available to it, which is not the same thing as being
    restricted to an empty set.

    Args:
        invocation: The command line.

    Returns:
        The tools the run is limited to, or ``None`` when it is limited
        to none of them.
    """
    for flag in ("--allowedTools", "--allowed-tools"):
        if flag in invocation.argv:
            index = invocation.argv.index(flag)
            return {
                name
                for name in invocation.argv[index + 1].split(",")
                if name
            }
    return None


def _whitelist_honoured(
    invocation: HarnessInvocation, requested: Sequence[str]
) -> bool:
    """Whether the run really is restricted to the tools that were asked for.

    Args:
        invocation: The command line.
        requested: The tools the request named.

    Returns:
        ``True`` when the run may use exactly those tools and no other.
    """
    served = _served_tools(invocation)
    return served is not None and served == set(requested)


# ---------------------------------------------------------------------------
# 1. The system instruction travels in the system-level parameter
# ---------------------------------------------------------------------------


def test_system_prompt_not_in_user_prompt(tmp_path, monkeypatch):
    """The system instruction is an instruction, not part of the user's turn."""
    workspace = _workspace(tmp_path)
    req = _request(workspace)
    harness = McodeHarness()

    invocation = harness.build_invocation(req)

    # 1. The system instruction travels in mcode's system-level
    #    parameter, and it is the only place it travels.
    assert "--append-system-prompt" in invocation.argv
    index = invocation.argv.index("--append-system-prompt")
    assert invocation.argv[index + 1] == SYSTEM_INSTRUCTION
    assert list(invocation.argv).count(SYSTEM_INSTRUCTION) == 1, (
        "the system instruction appears on the command line more than "
        "once; it has exactly one channel"
    )

    # 2. The user's text is not on the command line at all: it is the
    #    standard input channel.
    assert USER_PROMPT not in invocation.argv

    # 3. And what the child reads is the user's text, byte for byte.
    capture = tmp_path / "stdin.bin"
    monkeypatch.setenv("PDT_STAND_IN_STDIN", str(capture))
    _install_stand_in(tmp_path, monkeypatch, _stand_in_script())

    result = _run(harness, invocation, req.prompt, monkeypatch)

    assert result.exit_code == 0
    assert capture.exists(), "the stand-in CLI read nothing at all"

    stdin_bytes = capture.read_bytes()

    # The runner frames the prompt with exactly one newline, so the
    # child sees the end of the prompt; nothing else is added to it.
    assert stdin_bytes == req.prompt.encode("utf-8") + b"\n"

    # The system instruction is nowhere in the user's turn: it was not
    # concatenated into the prompt to simulate the system-level
    # parameter.
    assert SYSTEM_INSTRUCTION.encode("utf-8") not in stdin_bytes


def test_the_system_instruction_reaches_the_child_as_a_system_parameter(
    tmp_path, monkeypatch
):
    """The parameter is not only on the invocation — the CLI receives it.

    The invocation object says what the adapter built; the child's own
    argument list says what the CLI was actually handed. A runner that
    dropped an argument on the way would leave the first one intact,
    so only the second one proves the injection reached the CLI — and
    it has to arrive as the *system-level* parameter, next to the flag
    the CLI reads it from.
    """
    workspace = _workspace(tmp_path)
    req = _request(workspace)
    harness = McodeHarness()

    invocation = harness.build_invocation(req)

    stdin_capture = tmp_path / "stdin.bin"
    argv_capture = tmp_path / "argv.json"
    monkeypatch.setenv("PDT_STAND_IN_STDIN", str(stdin_capture))
    monkeypatch.setenv("PDT_STAND_IN_ARGV", str(argv_capture))
    _install_stand_in(tmp_path, monkeypatch, _stand_in_script())

    result = _run(harness, invocation, req.prompt, monkeypatch)

    assert result.exit_code == 0
    assert argv_capture.exists(), "the CLI did not record what it was handed"

    child_argv = json.loads(argv_capture.read_text(encoding="utf-8"))

    # The system instruction arrives as the CLI's system-level
    # parameter, with the flag in front of it.
    assert "--append-system-prompt" in child_argv
    index = child_argv.index("--append-system-prompt")
    assert child_argv[index + 1] == SYSTEM_INSTRUCTION

    # The user's text is not on the command line at all: it travelled
    # on standard input, and that is what the CLI read.
    assert USER_PROMPT not in child_argv
    assert stdin_capture.read_bytes() == req.prompt.encode("utf-8") + b"\n"


def test_the_system_instruction_is_not_smuggled_into_the_prompt(tmp_path):
    """A system instruction that is part of the prompt is not honoured.

    The two are different things. A system instruction appended to the
    command line is an instruction the CLI puts in front of the model; a
    system instruction pasted into the prompt is a sentence the user
    said, and the model may do anything with it. This is the failure
    the byte-for-byte check above exists to catch.
    """
    workspace = _workspace(tmp_path)
    req = _request(workspace)

    invocation = McodeHarness().build_invocation(req)

    # The instruction is where the CLI expects an instruction...
    assert invocation.argv[invocation.argv.index("--append-system-prompt") + 1] == (
        SYSTEM_INSTRUCTION
    )

    # ...and the prompt is untouched by it.
    assert req.prompt == USER_PROMPT
    assert SYSTEM_INSTRUCTION not in req.prompt


# ---------------------------------------------------------------------------
# 2. The whitelist has no mechanism, so its verdict is false
# ---------------------------------------------------------------------------


def test_whitelist_false_without_allowlist():
    """mcode cannot narrow the tool set, so the whitelist verdict is false."""
    harness = McodeHarness()

    # The verdict is false — a fact about the backend, not a guess and
    # not "unknown".
    assert harness.capabilities().tool_whitelist is False

    # The command line does not pretend otherwise: there is no
    # allowlist flag on it, in any spelling.
    invocation = harness.build_invocation(
        TaskInvocation(prompt=USER_PROMPT, allowed_tools=ALLOWED_TOOLS)
    )
    assert "--allowedTools" not in invocation.argv
    assert "--allowed-tools" not in invocation.argv
    assert "--tools" not in invocation.argv

    # ``--permission full`` is there, and it is not a whitelist: it
    # grants every tool and restricts none. A permission level says how
    # the run asks; an allowlist says what it may touch.
    assert "--permission" in invocation.argv
    assert invocation.argv[invocation.argv.index("--permission") + 1] == "full"

    # So the run is not restricted at all, and the tools it may use are
    # not the ones that were asked for.
    assert _served_tools(invocation) is None
    assert _whitelist_honoured(invocation, ALLOWED_TOOLS) is False


def test_the_whitelist_verdict_is_not_a_constant():
    """The same question has two different answers on the two backends.

    A verdict that is always false is not a verdict. The claude backend
    can narrow the tool set, and the mcode backend cannot, and the same
    probe has to tell them apart.
    """
    request = TaskInvocation(prompt=USER_PROMPT, allowed_tools=ALLOWED_TOOLS)

    claude = ClaudeHarness().build_invocation(request)
    mcode = McodeHarness().build_invocation(request)

    assert _whitelist_honoured(claude, ALLOWED_TOOLS) is True
    assert _whitelist_honoured(mcode, ALLOWED_TOOLS) is False


def test_a_run_that_needs_the_whitelist_is_refused_by_name(frozen_env):
    """A dispatch that asks for a whitelist on mcode is refused, not run wide open.

    The refusal happens before anything is spawned: a dispatch that was
    asked to be read-only must not become read-write because the
    backend it runs on spells the flag differently.
    """
    tool = coding_tool.ClaudeCodingTool()

    with pytest.raises(HarnessConfigError) as excinfo:
        tool._run_claude_interactive(
            USER_PROMPT,
            allowed_tools=list(ALLOWED_TOOLS),
            harness=McodeHarness(),
        )

    message = str(excinfo.value)
    assert "cannot restrict the tools" in message
    assert "Bash" in message, (
        "the refusal does not name the restriction that could not be "
        "honoured"
    )