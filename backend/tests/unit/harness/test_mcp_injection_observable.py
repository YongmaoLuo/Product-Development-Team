"""TDD spec for the MCP channel — the injection has to be *observable*.

What is pinned here
-------------------

``mcode exec`` has no ``--mcp-config``: the only place it reads MCP
servers from is the ``.mcp.json`` in the workspace root. The adapter
writes that file for the length of the run and puts the workspace back
afterwards — but a file that was written is not a file that was *read*.
The injection is only real if the CLI served the servers that were
written into it, and the only evidence of that is what the CLI says.

So the channel is judged by observation, and there are three places to
look:

1. **the server list** — the CLI reports the MCP servers it serves;
2. **the first turn's tool set** — the first turn offers tools named
   after the servers (``mcp__<server>__<tool>``);
3. **the fallback** — the first task's first event, which is the last
   thing there is to look at.

The rule that matters is the one about what happens when none of the
three shows anything: **the verdict is then a failure, never a
success.** A run whose MCP servers cannot be seen is not a run whose
MCP servers worked. Defaulting to success here is the one mistake this
file exists to prevent, because it is silent: the file was written, the
run finished, the answer looked fine, and the servers were never
served at all.

The probe below is the reference implementation of that rule — the
runtime's own probe has to agree with it, event for event. The CLI it
runs against is a stand-in written by the test: a real process that
reads the workspace's ``.mcp.json`` the way the real CLI does, so the
question "was the file really read?" is answered by a reader rather
than by an assumption.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pytest

import claude_sandbox
from harness.contract import HarnessEvent, TaskInvocation
from harness.mcode import MCODE_BINARY, McodeHarness
from harness.runner import Runner

pytestmark = pytest.mark.unit


#: An MCP configuration with one server, named so a test can look for it
#: by name.
MCP_CONFIG = {"mcpServers": {"demo": {"command": "demo-server"}}}

#: The server the configuration above asks for.
SERVER = "demo"

#: The payload keys a CLI may report its server list under. The list is
#: open on purpose: the rule is about *where the evidence may come
#: from*, not about one spelling.
SERVER_LIST_KEYS = ("mcpServers", "mcp_servers", "servers")

#: The payload keys a CLI may report its tool set under.
TOOL_LIST_KEYS = ("tools", "toolNames", "availableTools")

#: The prefix an MCP tool name carries.
MCP_TOOL_PREFIX = "mcp__"

#: A stand-in for the mcode CLI. It reads the MCP configuration the way
#: the real CLI does — from the ``.mcp.json`` in the workspace root —
#: and reports what it found, in the shape the mode asks for:
#:
#: * ``server_list``: the servers it serves, in the run's first event;
#: * ``first_turn_tools``: no server list, but the tools the first turn
#:   offers;
#: * ``first_event``: no server list and no first turn — only the tools
#:   the run's first event carries;
#: * ``silent``: none of them — a run in which the servers cannot be
#:   seen at all;
#: * ``mention_only``: the run talks about the servers in its output
#:   but reports none of them.
#:
#: ``__PYTHON__`` is replaced with the interpreter running the test.
_STAND_IN = """#!__PYTHON__
import json
import os
import sys

sys.stdin.buffer.read()

mode = os.environ.get("PDT_STAND_IN_MODE", "server_list")
config_path = os.path.join(os.getcwd(), ".mcp.json")

config = {}
if os.path.isfile(config_path):
    with open(config_path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    record = os.environ.get("PDT_STAND_IN_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as handle:
            json.dump({"path": config_path, "config": config}, handle)

servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
session = "mvs_" + "0" * 32

started = {"type": "exec.started", "session_id": session}
if mode == "server_list":
    started["mcpServers"] = servers
if mode == "first_event":
    started["tools"] = ["mcp__" + name + "__ping" for name in servers]
if mode == "mention_only":
    started["message"] = "connected to " + ", ".join(servers)
print(json.dumps(started))

if mode == "first_turn_tools":
    tools = ["Bash"] + ["mcp__" + name + "__ping" for name in servers]
    print(json.dumps({"type": "turn.started", "session_id": session,
                      "tools": tools}))

print(json.dumps({"type": "exec.completed",
                  "result": {"status": "succeeded", "output": "done"}}))
"""


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpObservation:
    """What the run said about the MCP servers it was given.

    Attributes:
        source: Where the evidence was found — ``"server_list"``,
            ``"first_turn_tools"``, ``"first_event"``, or ``"none"``
            when nothing showed the servers at all.
        observable: Whether the servers could be seen.
        succeeded: Whether the servers that were asked for were the
            ones that were served. Never ``True`` without evidence.
        servers: The servers that were observed being served.
        reason: Why the verdict is what it is.
    """

    source: str
    observable: bool
    succeeded: bool
    servers: Tuple[str, ...]
    reason: str


def _payload(event: HarnessEvent) -> Dict[str, Any]:
    """The mcode payload the event was made of.

    A canonical event carries the whole payload it was translated from,
    and the translation keeps the untouched one under ``raw``.

    Args:
        event: The event.

    Returns:
        The payload the event came from.
    """
    raw = event.raw or {}
    inner = raw.get("raw")
    if isinstance(inner, dict):
        return inner
    return raw


def _server_names(payload: Dict[str, Any]) -> Tuple[str, ...]:
    """The server names a payload reports, if it reports any.

    Args:
        payload: The payload.

    Returns:
        The names, in the order the payload listed them.
    """
    for key in SERVER_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            return tuple(str(name) for name in value)
        if isinstance(value, list):
            return tuple(str(name) for name in value)
    return ()


def _tool_names(payload: Dict[str, Any]) -> Tuple[str, ...]:
    """The tool names a payload reports, if it reports any.

    Args:
        payload: The payload.

    Returns:
        The names, in the order the payload listed them.
    """
    for key in TOOL_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(str(name) for name in value)
    return ()


def _servers_behind(tools: Iterable[str]) -> Tuple[str, ...]:
    """The servers an MCP tool name belongs to.

    ``mcp__<server>__<tool>`` is the spelling every client uses, and
    the server is the middle of it.

    Args:
        tools: The tool names.

    Returns:
        The server names, without duplicates, in the order they appeared.
    """
    names: List[str] = []
    for tool in tools:
        if not tool.startswith(MCP_TOOL_PREFIX):
            continue
        parts = tool.split("__")
        if len(parts) >= 2 and parts[1] and parts[1] not in names:
            names.append(parts[1])
    return tuple(names)


def _is_first_turn(payload: Dict[str, Any]) -> bool:
    """Whether a payload opens a turn.

    Args:
        payload: The payload.

    Returns:
        ``True`` when the payload is a turn's opening event.
    """
    return (
        payload.get("type") == "turn.started"
        or payload.get("subtype") == "turn.started"
    )


def _verdict(
    source: str, servers: Sequence[str], requested: Sequence[str]
) -> McpObservation:
    """The verdict for servers that were seen.

    Args:
        source: Where they were seen.
        servers: The servers that were seen.
        requested: The servers the request asked for.

    Returns:
        The observation.
    """
    missing = [name for name in requested if name not in servers]
    if missing:
        return McpObservation(
            source=source,
            observable=True,
            succeeded=False,
            servers=tuple(servers),
            reason=(
                f"the servers {missing} were asked for and are not among "
                f"the ones that were served ({list(servers)})"
            ),
        )
    return McpObservation(
        source=source,
        observable=True,
        succeeded=True,
        servers=tuple(servers),
        reason=(
            f"the servers {list(requested)} were served, seen through the "
            f"{source}"
        ),
    )


def observe_mcp(
    requested: Sequence[str], events: Sequence[HarnessEvent]
) -> McpObservation:
    """Look for the MCP servers a run served.

    The three sources are tried in order: the server list, the first
    turn's tool set, and — when neither shows anything — the first task's
    first event. A run in which none of them shows the servers is a run
    whose servers could not be seen, and that is a failure. It is never
    a success: the file was written, the run finished, and the servers
    were not served.

    Args:
        requested: The servers the request asked for.
        events: The events of the first task.

    Returns:
        What the run showed, and what that means.
    """
    wanted = tuple(requested)
    stream = list(events)

    if not stream:
        return McpObservation(
            source="none",
            observable=False,
            succeeded=False,
            servers=(),
            reason=(
                "the first task produced no event at all, so there is "
                "nothing that could show the MCP servers"
            ),
        )

    # 1. The server list.
    for event in stream:
        servers = _server_names(_payload(event))
        if servers:
            return _verdict("server_list", servers, wanted)

    # 2. The first turn's tool set.
    for event in stream:
        payload = _payload(event)
        if not _is_first_turn(payload):
            continue
        servers = _servers_behind(_tool_names(payload))
        if servers:
            return _verdict("first_turn_tools", servers, wanted)
        break

    # 3. The fallback: the first task's first event.
    first = _payload(stream[0])
    servers = _server_names(first)
    if not servers:
        servers = _servers_behind(_tool_names(first))
    if servers:
        return _verdict("first_event", servers, wanted)

    return McpObservation(
        source="none",
        observable=False,
        succeeded=False,
        servers=(),
        reason=(
            "the MCP servers could not be seen: the run reported no "
            "server list, its first turn offered no MCP tool, and its "
            "first event showed nothing either"
        ),
    )


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    """A throwaway workspace — where the MCP channel writes its file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _install_stand_in(tmp_path: Path, monkeypatch, mode: str) -> Path:
    """Put the stand-in ``mcode`` on the PATH, in the mode asked for.

    Args:
        tmp_path: The test's temporary directory.
        monkeypatch: The test's monkeypatch.
        mode: What the stand-in reports.

    Returns:
        The directory the stand-in was installed in.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    binary = bin_dir / MCODE_BINARY
    binary.write_text(
        _STAND_IN.replace("__PYTHON__", sys.executable), encoding="utf-8"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PDT_STAND_IN_MODE", mode)
    return bin_dir


def _run(
    tmp_path: Path,
    monkeypatch,
    mode: str,
    request: TaskInvocation,
) -> Tuple[List[HarnessEvent], Optional[Path]]:
    """Run the request against the stand-in and report what came back.

    Args:
        tmp_path: The test's temporary directory.
        monkeypatch: The test's monkeypatch.
        mode: What the stand-in reports.
        request: The request to run.

    Returns:
        The events of the run, and the file the stand-in recorded what
        it read (or ``None`` when it read nothing).
    """
    record = tmp_path / "read.json"
    _install_stand_in(tmp_path, monkeypatch, mode)
    monkeypatch.setenv("PDT_STAND_IN_RECORD", str(record))
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None,
    )

    harness = McodeHarness()
    invocation = harness.build_invocation(request)
    result = Runner().run(harness, invocation, stdin_text=request.prompt)

    assert result.exit_code == 0, "the stand-in CLI did not finish"
    return list(result.events), (record if record.exists() else None)


# ---------------------------------------------------------------------------
# 1. The servers the CLI reports are the servers that were served
# ---------------------------------------------------------------------------


def test_mcp_observable_via_server_list(tmp_path, monkeypatch):
    """The server list shows the servers, and the file was really read."""
    workspace = _workspace(tmp_path)
    request = TaskInvocation(
        prompt="p",
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )

    events, record = _run(tmp_path, monkeypatch, "server_list", request)

    # The file the CLI read is the file the adapter wrote, in the
    # workspace root, with the configuration the request carried. This
    # is what "the .mcp.json must really be read" means: a reader read
    # it, and it said what the request asked for.
    assert record is not None, (
        "the stand-in CLI read no .mcp.json at all — the file the "
        "adapter writes was never in front of the CLI"
    )
    read = json.loads(record.read_text(encoding="utf-8"))
    assert Path(read["path"]).resolve() == (workspace / ".mcp.json").resolve()
    assert read["config"] == MCP_CONFIG

    observation = observe_mcp((SERVER,), events)

    assert observation.source == "server_list"
    assert observation.observable is True
    assert observation.succeeded is True
    assert observation.servers == (SERVER,)


def test_mcp_observable_via_first_turn_tools(tmp_path, monkeypatch):
    """The first turn's tools show the servers when no server list does."""
    workspace = _workspace(tmp_path)
    request = TaskInvocation(
        prompt="p",
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )

    events, _record = _run(tmp_path, monkeypatch, "first_turn_tools", request)

    observation = observe_mcp((SERVER,), events)

    assert observation.source == "first_turn_tools"
    assert observation.observable is True
    assert observation.succeeded is True
    assert observation.servers == (SERVER,)


def test_mcp_observable_via_the_first_task_first_event(tmp_path, monkeypatch):
    """The fallback finds the servers when the run says nothing else.

    This is the last place there is to look: the run reported no server
    list, and it has no first turn to look at either — but its first
    event carries the tools of the servers it served.
    """
    workspace = _workspace(tmp_path)
    request = TaskInvocation(
        prompt="p",
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )

    events, _record = _run(tmp_path, monkeypatch, "first_event", request)

    observation = observe_mcp((SERVER,), events)

    assert observation.source == "first_event"
    assert observation.observable is True
    assert observation.succeeded is True
    assert observation.servers == (SERVER,)


# ---------------------------------------------------------------------------
# 2. What cannot be seen is not a success
# ---------------------------------------------------------------------------


def test_mcp_unobservable_does_not_default_success(tmp_path, monkeypatch):
    """A run whose servers cannot be seen has not served them.

    The stand-in reports neither a server list nor an MCP tool, and its
    first event shows nothing either — so the fallback finds nothing
    and the verdict is a failure. It is not "probably fine": the whole
    point of the rule is that an injection nobody can see is an
    injection nobody can vouch for.
    """
    workspace = _workspace(tmp_path)
    request = TaskInvocation(
        prompt="p",
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )

    events, _record = _run(tmp_path, monkeypatch, "silent", request)

    observation = observe_mcp((SERVER,), events)

    assert observation.observable is False
    assert observation.succeeded is False, (
        "the MCP injection was called a success although nothing in the "
        "run showed the servers"
    )
    assert observation.source == "none"
    assert observation.reason, "a failure has to say why"


def test_mcp_missing_first_event_fails():
    """A first task with no event at all is a failure, not a success."""
    observation = observe_mcp((SERVER,), [])

    assert observation.observable is False
    assert observation.succeeded is False
    assert observation.source == "none"
    assert "no event" in observation.reason


def test_mcp_servers_that_were_not_served_are_not_a_success(tmp_path, monkeypatch):
    """A server that was asked for and not served fails, even when others were.

    The configuration names one server and the run is said to have been
    given two. One of them was served; the other was not — and a run
    that served only half of what it was given has not served the
    injection.
    """
    workspace = _workspace(tmp_path)
    request = TaskInvocation(
        prompt="p",
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )

    events, _record = _run(tmp_path, monkeypatch, "server_list", request)

    observation = observe_mcp((SERVER, "other"), events)

    assert observation.observable is True
    assert observation.succeeded is False
    assert "other" in observation.reason


def test_mcp_a_server_name_in_the_output_is_not_evidence(tmp_path, monkeypatch):
    """A name the run talks about is not a server the run served.

    A CLI that says "connected to demo" in its output has not served
    anything a turn could call; it has talked about it. The probe
    looks for the servers where the CLI *reports* them, so this run is
    not observable — and a probe that merely looked for the name
    anywhere in the stream would have called it a success.
    """
    workspace = _workspace(tmp_path)
    request = TaskInvocation(
        prompt="p",
        mcp_config=MCP_CONFIG,
        cwd=str(workspace),
    )

    events, _record = _run(tmp_path, monkeypatch, "mention_only", request)

    # The run really does mention the server: without that, this test
    # would say nothing about false positives.
    assert any(SERVER in json.dumps(event.raw or {}) for event in events)

    observation = observe_mcp((SERVER,), events)

    assert observation.observable is False
    assert observation.succeeded is False, (
        "a server named in the output was called a served server"
    )
    assert observation.source == "none"


# ---------------------------------------------------------------------------
# 3. The rule is not vacuous
# ---------------------------------------------------------------------------


def test_mcp_injection_that_reached_nothing_is_not_observable(tmp_path, monkeypatch):
    """The same run, with no configuration written, shows no servers.

    This is the negative control: the stand-in reads the workspace's
    ``.mcp.json``, so a request that carries no configuration leaves it
    with nothing to read — and the same probe then answers "not
    observable" instead of "served". The positive case is a fact about
    the file, not about the probe.
    """
    workspace = _workspace(tmp_path)
    request = TaskInvocation(prompt="p", cwd=str(workspace))

    events, record = _run(tmp_path, monkeypatch, "server_list", request)

    assert record is None, "the stand-in read a configuration nobody wrote"
    observation = observe_mcp((SERVER,), events)
    assert observation.observable is False
    assert observation.succeeded is False