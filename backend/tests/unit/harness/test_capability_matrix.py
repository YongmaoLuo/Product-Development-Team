"""TDD spec for the capability matrix — seven capabilities, four quadrants.

What is pinned here
-------------------

A backend is never asked for everything. ``HarnessCapabilities`` says
what a backend can do, and a task only needs the capabilities its own
shape demands: a task that names no MCP servers does not need
``mcp_injection``, and a task that names the tools it may use does need
``tool_whitelist``.

So a declaration is read against a task, and the two axes together make
four quadrants:

======================  =============================================
needed + supported      the task runs
needed + missing        the task is refused, and the refusal names the capability and the evidence behind it
not needed + supported  the task runs, the capability's path is not
                        taken
not needed + missing    the task runs, the capability's path is not
                        taken
======================  =============================================

Seven capabilities times four quadrants is twenty-eight cases, and all
twenty-eight of them are here.

Two things this file refuses to let slide:

* **A missing capability is never satisfied by accident.** ``mcode
  exec`` always runs with ``--permission full``. That flag grants every
  tool and restricts none, so a task that asked for a whitelist and
  found that flag on its command line would run wide open. The
  whitelist is refused by name instead.
* **A missing capability refuses only the tasks that need it.** If
  every capability were required all at once, the mcode backend — which
  cannot narrow the tool set — would be unusable forever, and the
  backend would be dead on arrival rather than limited to one job.

The adapters are read through the same gate the layer above them uses,
so what ``capabilities()`` declares and what ``build_invocation`` does
are held to one answer.

No process is spawned and no ``Popen`` is mocked (test decision point
2): the command line is the whole observable.
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, List, Set, Tuple

import pytest

from harness.claude import ClaudeHarness
from harness.contract import (
    HarnessCapabilities,
    HarnessConfigError,
    HarnessInvocation,
    SessionMode,
    SessionRef,
    TaskInvocation,
)
from harness.mcode import McodeHarness

pytestmark = pytest.mark.unit


#: The seven capabilities the contract declares, in the order the
#: contract lists them.
CAPS: Tuple[str, ...] = (
    "append_system_prompt",
    "mcp_injection",
    "tool_whitelist",
    "stream_events",
    "usage",
    "session_create",
    "session_resume",
)

#: The four quadrants a capability and a task make together.
QUADRANTS: Tuple[str, ...] = (
    "needed+supported",
    "needed+missing",
    "not-needed+supported",
    "not-needed+missing",
)

#: The whole matrix: every capability against every quadrant.
MATRIX: Tuple[Tuple[str, str], ...] = tuple(
    (capability, quadrant)
    for capability in CAPS
    for quadrant in QUADRANTS
)

#: Why a task needs a capability, in the words of the request that
#: asked for it. The refusal quotes this, so the caller can see which
#: part of its own task the backend could not honour.
CAPABILITY_DEMAND = {
    "append_system_prompt": (
        "the request carries a system instruction (system_instruction)"
    ),
    "mcp_injection": (
        "the request carries MCP servers (mcp_config / mcp_config_path)"
    ),
    "tool_whitelist": (
        "the request names the tools the run may use (allowed_tools)"
    ),
    "stream_events": "the request is multi-turn (multi_turn)",
    "usage": "the request is multi-turn and its cost must be reported (multi_turn)",
    "session_create": (
        "the request asks for a conversation under its own id "
        "(session.pinned_session_id)"
    ),
    "session_resume": "the request resumes a conversation (mode=resume)",
}


# ---------------------------------------------------------------------------
# The need: what a task asks the backend to be able to do
# ---------------------------------------------------------------------------


def required_capabilities(req: TaskInvocation) -> FrozenSet[str]:
    """What this task needs the backend to be able to do.

    The need is computed from the request and from nothing else. A
    request that asks for nothing needs nothing; a request that names
    the tools the run may use needs a backend that can narrow the tool
    set. Nothing here is a blanket requirement, because a blanket
    requirement is what makes a backend permanently unusable.

    Args:
        req: The request.

    Returns:
        The names of the capabilities the request depends on.
    """
    needed: Set[str] = set()

    if req.system_instruction is not None:
        needed.add("append_system_prompt")

    if req.mcp_config or req.mcp_config_path:
        needed.add("mcp_injection")

    if req.allowed_tools:
        needed.add("tool_whitelist")

    # A multi-turn run is not one final answer: the caller reads the
    # turns as they arrive and prices the conversation as a whole. A
    # single-turn run needs neither the live stream nor the accounting.
    if req.multi_turn:
        needed.add("stream_events")
        needed.add("usage")

    session = req.session
    if req.mode is SessionMode.RESUME:
        needed.add("session_resume")
    elif session is not None and (
        session.session_id or session.pinned_session_id
    ):
        needed.add("session_create")

    return frozenset(needed)


def _needing_task(capability: str) -> TaskInvocation:
    """A task that needs the capability named.

    The two capabilities that share a trigger — the live stream and the
    accounting of a multi-turn run — come as a pair, because the
    request that needs one needs the other.

    Args:
        capability: The capability the task needs.

    Returns:
        The task.
    """
    if capability == "append_system_prompt":
        return TaskInvocation(prompt="p", system_instruction="S")
    if capability == "mcp_injection":
        return TaskInvocation(prompt="p", mcp_config='{"mcpServers": {}}')
    if capability == "tool_whitelist":
        return TaskInvocation(prompt="p", allowed_tools=("Bash", "Read"))
    if capability in ("stream_events", "usage"):
        return TaskInvocation(prompt="p", multi_turn=True)
    if capability == "session_create":
        return TaskInvocation(
            prompt="p",
            session=SessionRef(pinned_session_id="u1"),
            mode=SessionMode.CREATE,
        )
    if capability == "session_resume":
        return TaskInvocation(
            prompt="p",
            session=SessionRef(session_id="mvs_ab"),
            mode=SessionMode.RESUME,
        )
    raise AssertionError(f"unknown capability {capability!r}")


def _plain_task() -> TaskInvocation:
    """A task that needs nothing at all."""
    return TaskInvocation(prompt="p")


def _without(capability: str) -> Tuple[str, ...]:
    """Every capability but the one named.

    Args:
        capability: The capability left out.

    Returns:
        The other six.
    """
    return tuple(other for other in CAPS if other != capability)


# ---------------------------------------------------------------------------
# The backends the matrix is read against
# ---------------------------------------------------------------------------


class _CapabilityHarness:
    """A backend that declares exactly the capabilities it was given.

    It is the only way to reach the quadrants a real backend does not
    live in: the claude backend has every capability and the mcode
    backend has all but one, so the remaining quadrants need a backend
    whose declaration can be set freely.

    It records what it was asked to build, so a test can tell that a
    refused task never reached the backend at all.

    Attributes:
        asked: The requests the backend was asked to build.
    """

    name = "probe"

    def __init__(self, supported: Iterable[str]) -> None:
        self._supported = frozenset(supported)
        self.asked: List[TaskInvocation] = []

    def capabilities(self) -> HarnessCapabilities:
        """The capabilities this backend was given.

        Returns:
            The declaration, with every one of the seven answered.
        """
        return HarnessCapabilities(
            **{
                capability: capability in self._supported
                for capability in CAPS
            }
        )

    def build_invocation(self, req: TaskInvocation) -> HarnessInvocation:
        """Build a command line for the request.

        Args:
            req: The request.

        Returns:
            The invocation.
        """
        self.asked.append(req)
        return HarnessInvocation(argv=("probe", "exec", "--input", "-"))


def _backend_name(harness) -> str:
    """The backend's name — the same rule the runner uses.

    Args:
        harness: The backend.

    Returns:
        Its name.
    """
    name = getattr(harness, "name", "")
    if isinstance(name, str) and name.strip():
        return name.strip()
    class_name = type(harness).__name__
    if class_name.endswith("Harness"):
        class_name = class_name[: -len("Harness")]
    return class_name.lower()


def run_ok(harness, task: TaskInvocation) -> HarnessInvocation:
    """What the layer above a backend does with a task.

    The capability is checked before the command line is built, so a
    task the backend cannot run never reaches the backend.

    Args:
        harness: The backend.
        task: The request.

    Returns:
        The invocation the backend was asked to run.

    Raises:
        HarnessConfigError: The task needs a capability the backend does
            not have.
    """
    check_capabilities(
        harness.capabilities(), task, backend=_backend_name(harness)
    )
    return harness.build_invocation(task)


def check_capabilities(
    caps: HarnessCapabilities,
    task: TaskInvocation,
    *,
    backend: str,
) -> None:
    """Refuse a task the backend cannot run as asked.

    Args:
        caps: What the backend declared.
        task: The request.
        backend: The backend's name, for the message.

    Returns:
        Nothing, when the task can be run as asked.

    Raises:
        HarnessConfigError: The task needs a capability the backend does
            not have. The message names the capability, the probe that
            found it missing, and the part of the request that needed it.
    """
    missing = sorted(
        capability
        for capability in required_capabilities(task)
        if not getattr(caps, capability)
    )
    if not missing:
        return

    named = ", ".join(repr(capability) for capability in missing)
    probed = ", ".join(f"{capability}=False" for capability in missing)
    demanded = "; ".join(
        f"{capability}: {CAPABILITY_DEMAND[capability]}" for capability in missing
    )
    raise HarnessConfigError(
        f"{backend} cannot honour {named}, which this task needs: "
        f"probed through {backend}.capabilities() -> {probed}; "
        f"{demanded}; refusing to run the task as if the capability "
        f"were there"
    )


# ---------------------------------------------------------------------------
# 1. The matrix itself
# ---------------------------------------------------------------------------


def _parametrized_over(function) -> Tuple[str, ...]:
    """The capabilities a parametrized test was expanded over.

    Args:
        function: The test function.

    Returns:
        The capabilities, in the order they were parametrized.
    """
    for mark in function.pytestmark:
        if mark.name == "parametrize" and mark.args[0] == "capability":
            return tuple(mark.args[1])
    return ()


def test_matrix_has_28_cases():
    """Seven capabilities times four quadrants, every pair exactly once."""
    assert len(CAPS) == 7
    assert len(QUADRANTS) == 4
    assert len(MATRIX) == 28

    # Every pair appears once and only once: no quadrant is missing,
    # and none is counted twice.
    assert len(set(MATRIX)) == 28
    assert set(MATRIX) == {
        (capability, quadrant)
        for capability in CAPS
        for quadrant in QUADRANTS
    }

    # The matrix is not only declared, it is run: each quadrant is a
    # test expanded over all seven capabilities.
    quadrants = {
        "needed+supported": test_needed_and_supported_ok,
        "needed+missing": test_needed_and_missing_fails,
        "not-needed+supported": test_not_needed_and_supported_ok,
        "not-needed+missing": test_not_needed_and_missing_ok,
    }
    assert set(quadrants) == set(QUADRANTS)
    for quadrant, function in quadrants.items():
        assert _parametrized_over(function) == CAPS, (
            f"the {quadrant!r} quadrant is not expanded over every capability"
        )


# ---------------------------------------------------------------------------
# 2. Needed and supported — the task runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", CAPS, ids=list(CAPS))
def test_needed_and_supported_ok(capability):
    """A task that needs what the backend can do runs."""
    harness = _CapabilityHarness(supported=CAPS)
    task = _needing_task(capability)

    assert capability in required_capabilities(task)

    invocation = run_ok(harness, task)

    assert invocation.argv
    assert harness.asked == [task]


# ---------------------------------------------------------------------------
# 3. Needed and missing — the task is refused, by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", CAPS, ids=list(CAPS))
def test_needed_and_missing_fails(capability):
    """A task that needs what the backend cannot do is refused by name."""
    harness = _CapabilityHarness(supported=_without(capability))
    task = _needing_task(capability)

    assert capability in required_capabilities(task)

    with pytest.raises(HarnessConfigError) as excinfo:
        run_ok(harness, task)

    message = str(excinfo.value)

    # The capability is named.
    assert capability in message

    # The evidence is named: what was probed, and how it was probed.
    assert f"{capability}=False" in message
    assert "capabilities()" in message

    # And the part of the request that needed it.
    assert CAPABILITY_DEMAND[capability].split(" (")[0] in message

    # The backend was never asked to build anything: the refusal comes
    # before the command line exists.
    assert harness.asked == []


# ---------------------------------------------------------------------------
# 4. Not needed and supported — the task runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", CAPS, ids=list(CAPS))
def test_not_needed_and_supported_ok(capability):
    """A task that needs nothing runs on a backend that has everything."""
    harness = _CapabilityHarness(supported=CAPS)
    task = _plain_task()

    assert capability not in required_capabilities(task)

    invocation = run_ok(harness, task)

    assert invocation.argv
    assert harness.asked == [task]


# ---------------------------------------------------------------------------
# 5. Not needed and missing — the task runs, the path is not taken
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", CAPS, ids=list(CAPS))
def test_not_needed_and_missing_ok(capability):
    """A task that does not need the missing capability still runs."""
    harness = _CapabilityHarness(supported=_without(capability))
    task = _plain_task()

    # The task asks for nothing, so nothing is missing.
    assert required_capabilities(task) == frozenset()
    assert capability not in required_capabilities(task)

    invocation = run_ok(harness, task)

    assert invocation.argv
    assert harness.asked == [task]


# ---------------------------------------------------------------------------
# 6. The need is computed per task, never all at once
# ---------------------------------------------------------------------------


def test_the_requirements_are_computed_per_task():
    """A task that asks for nothing is refused for nothing."""
    assert required_capabilities(_plain_task()) == frozenset()

    task = _needing_task("tool_whitelist")
    assert required_capabilities(task) == frozenset({"tool_whitelist"})


def test_a_multi_turn_task_needs_the_stream_and_the_accounting():
    """The two capabilities that share a trigger come as a pair."""
    task = _needing_task("stream_events")

    assert required_capabilities(task) == frozenset({"stream_events", "usage"})


def test_a_resumed_conversation_needs_no_creation():
    """A resume asks for one conversation, not two."""
    task = _needing_task("session_resume")

    assert "session_resume" in required_capabilities(task)
    assert "session_create" not in required_capabilities(task)


# ---------------------------------------------------------------------------
# 7. What the real backends declare
# ---------------------------------------------------------------------------


def test_claude_declares_every_capability():
    """The claude CLI has every flag the contract names."""
    caps = ClaudeHarness().capabilities()

    for capability in CAPS:
        assert getattr(caps, capability) is True, (
            f"claude declares {capability}={getattr(caps, capability)!r}"
        )


def test_mcode_declares_every_capability_but_the_whitelist():
    """The mcode CLI can do everything except narrow the tool set."""
    caps = McodeHarness().capabilities()

    for capability in CAPS:
        expected = capability != "tool_whitelist"
        assert getattr(caps, capability) is expected, (
            f"mcode declares {capability}={getattr(caps, capability)!r}"
        )


def test_mcode_is_missing_exactly_one_capability():
    """One missing capability, not a backend that is out of the game."""
    caps = McodeHarness().capabilities()

    missing = [capability for capability in CAPS if not getattr(caps, capability)]

    assert missing == ["tool_whitelist"]


def test_mcode_runs_a_task_that_asks_for_nothing():
    """A missing capability does not stop the tasks that do not need it."""
    invocation = run_ok(McodeHarness(), _plain_task())

    assert invocation.argv


@pytest.mark.parametrize(
    "capability",
    [capability for capability in CAPS if capability != "tool_whitelist"],
    ids=[c for c in CAPS if c != "tool_whitelist"],
)
def test_mcode_runs_the_tasks_that_do_not_need_the_whitelist(capability):
    """mcode is refused one job, not every job."""
    harness = McodeHarness()
    task = _needing_task(capability)

    invocation = run_ok(harness, task)

    assert invocation.argv


# ---------------------------------------------------------------------------
# 8. --permission full is not a whitelist
# ---------------------------------------------------------------------------


def test_permission_full_is_not_whitelist():
    """``--permission full`` grants every tool; it restricts none.

    The mcode command line always carries it, so a task that asked for
    a whitelist would find that flag on its command line. That flag
    says nothing about which tools may run — it says the opposite.
    """
    harness = McodeHarness()

    # The flag is always there.
    invocation = harness.build_invocation(_plain_task())
    assert "--permission" in invocation.argv
    assert invocation.argv[invocation.argv.index("--permission") + 1] == "full"

    # And no flag on the command line narrows the tool set.
    for flag in ("--allowedTools", "--allowed-tools", "--disallowedTools"):
        assert flag not in invocation.argv

    # So the requirement is not met, and the task is refused by name.
    with pytest.raises(HarnessConfigError, match="tool_whitelist"):
        run_ok(harness, _needing_task("tool_whitelist"))


def test_the_refusal_names_the_capability_and_the_evidence():
    """A refusal says which capability, and what was probed."""
    harness = McodeHarness()

    with pytest.raises(HarnessConfigError) as excinfo:
        run_ok(harness, _needing_task("tool_whitelist"))

    message = str(excinfo.value)

    assert "tool_whitelist" in message
    assert "mcode" in message
    assert "capabilities()" in message
    assert "tool_whitelist=False" in message
    assert "allowed_tools" in message


# ---------------------------------------------------------------------------
# 9. The path a capability takes is the path the command line shows
# ---------------------------------------------------------------------------


def test_claude_carries_the_flag_of_every_capability_it_has():
    """claude's command line says what the task asked for."""
    harness = ClaudeHarness()

    # The system prompt.
    argv = harness.build_invocation(_needing_task("append_system_prompt")).argv
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "S"

    # The MCP servers.
    argv = harness.build_invocation(_needing_task("mcp_injection")).argv
    assert "--mcp-config" in argv

    # The tool whitelist.
    argv = harness.build_invocation(_needing_task("tool_whitelist")).argv
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Bash,Read"

    # The conversation to create.
    argv = harness.build_invocation(_needing_task("session_create")).argv
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "u1"

    # The conversation to resume.
    argv = harness.build_invocation(_needing_task("session_resume")).argv
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "mvs_ab"


def test_claude_carries_no_flag_for_a_task_that_asked_for_none():
    """A task that asked for nothing gets no optional flag."""
    argv = ClaudeHarness().build_invocation(_plain_task()).argv

    for flag in (
        "--append-system-prompt",
        "--mcp-config",
        "--allowedTools",
        "--session-id",
        "--resume",
    ):
        assert flag not in argv


def test_mcode_has_an_mcp_hook_only_when_mcp_is_asked_for():
    """The MCP path is the workspace file, and only when it is needed."""
    harness = McodeHarness()

    assert harness.build_invocation(_plain_task()).prepare is None
    assert harness.build_invocation(_needing_task("mcp_injection")).prepare is not None


def test_mcode_carries_no_tool_flag_when_no_whitelist_is_asked_for():
    """The tool path is not taken when the task did not ask for it."""
    argv = McodeHarness().build_invocation(_plain_task()).argv

    for flag in ("--allowedTools", "--allowed-tools"):
        assert flag not in argv