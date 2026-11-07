"""TDD spec for :mod:`harness.contract` — the backend-agnostic contract types.

The contract layer is pure data: no process, no provider, no filesystem.
That is deliberate (test decision point 2 — adapter unit tests mock the
*boundary*, never the data types), and it is why these tests construct
the types directly instead of stubbing them.

The contract points pinned here:

1. ``TaskInvocation`` needs only a prompt; the conversation reference
   defaults to empty and the session mode defaults to ``CREATE``.
2. Every dataclass in the module is frozen except for
   ``HarnessEvent.raw`` — assignment raises ``FrozenInstanceError``.
3. ``HarnessCapabilities`` requires all seven flags; a missing one is a
   ``TypeError`` at construction.
4. ``HarnessOutcome.kind`` is a closed set of seven values.
"""

import dataclasses

import pytest

from harness.contract import (
    Harness,
    HarnessCapabilities,
    HarnessConfigError,
    HarnessEvent,
    HarnessInvocation,
    HarnessOutcome,
    HarnessResult,
    OUTCOME_KINDS,
    ProfileBinding,
    SessionMode,
    SessionRef,
    TaskInvocation,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. TaskInvocation defaults
# ---------------------------------------------------------------------------


def test_task_invocation_defaults():
    """A prompt is the only required field of a request."""
    req = TaskInvocation(prompt="hi")

    assert req.prompt == "hi"
    assert req.session is None
    assert req.mode is SessionMode.CREATE


def test_task_invocation_keeps_the_session_it_was_given():
    """A request that names a conversation keeps it verbatim."""
    req = TaskInvocation(
        prompt="hi",
        allowed_tools=("Bash",),
        session=SessionRef(pinned_session_id="u1"),
        mode=SessionMode.CREATE,
    )

    assert req.allowed_tools == ("Bash",)
    assert req.session == SessionRef(pinned_session_id="u1")
    assert req.session.pinned_session_id == "u1"


# ---------------------------------------------------------------------------
# 2. Immutability
# ---------------------------------------------------------------------------


def test_invocation_is_frozen():
    """The spawn description cannot be rewritten after it is built."""
    invocation = HarnessInvocation(argv=("run", "--flag"), env_delta={})

    with pytest.raises(dataclasses.FrozenInstanceError):
        invocation.argv = ("other",)


def test_task_invocation_is_frozen():
    """The request cannot be rewritten after it is built."""
    req = TaskInvocation(prompt="hi")

    with pytest.raises(dataclasses.FrozenInstanceError):
        req.prompt = "bye"


def test_event_raw_is_the_only_mutable_field():
    """``HarnessEvent.raw`` is the payload the adapter parsed out."""
    event = HarnessEvent(type="result", raw={"type": "result"})

    event.raw = {"type": "result", "extra": True}
    assert event.raw["extra"] is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.type = "assistant"


# ---------------------------------------------------------------------------
# 3. Capabilities are all-or-nothing
# ---------------------------------------------------------------------------


def test_capabilities_require_all_seven():
    """A capability flag that defaulted would hide a missing capability."""
    with pytest.raises(TypeError):
        HarnessCapabilities(
            append_system_prompt=True,
            mcp_injection=True,
            tool_whitelist=False,
            stream_events=True,
            usage=True,
            session_create=True,
            # session_resume omitted on purpose
        )


def test_capabilities_accept_all_seven():
    """With all seven supplied, the capability set is complete."""
    caps = HarnessCapabilities(
        append_system_prompt=True,
        mcp_injection=True,
        tool_whitelist=False,
        stream_events=True,
        usage=True,
        session_create=True,
        session_resume=True,
    )

    assert caps.append_system_prompt is True
    assert caps.tool_whitelist is False
    assert caps.session_resume is True


# ---------------------------------------------------------------------------
# 4. The outcome vocabulary is closed
# ---------------------------------------------------------------------------


def test_outcome_kind_is_a_closed_set():
    """Only the seven known kinds are outcomes."""
    assert OUTCOME_KINDS == frozenset(
        {
            "ok",
            "model_error",
            "timeout",
            "permission_denied",
            "config_error",
            "signaled",
            "unknown",
        }
    )

    for kind in sorted(OUTCOME_KINDS):
        assert HarnessOutcome(kind=kind).kind == kind

    with pytest.raises(ValueError):
        HarnessOutcome(kind="something_else")


def test_result_carries_the_outcome_it_was_built_with():
    """The result and the verdict travel together."""
    result = HarnessResult(
        exit_code=1,
        harness="example-harness",
        outcome=HarnessOutcome(kind="model_error", message="upstream refused"),
    )

    assert result.exit_code == 1
    assert result.harness == "example-harness"
    assert result.outcome.kind == "model_error"
    assert result.outcome.message == "upstream refused"


# ---------------------------------------------------------------------------
# 5. The remaining surface
# ---------------------------------------------------------------------------


def test_prepare_defaults_to_none():
    """A harness that needs no preparation says so by leaving it unset."""
    assert HarnessInvocation(argv=("run",)).prepare is None


def test_session_ref_defaults_to_empty():
    """A session reference that names nothing is empty, not unknown."""
    ref = SessionRef()

    assert ref.session_id is None
    assert ref.pinned_session_id is None


def test_profile_binding_is_frozen():
    """A profile binding is configuration, not a mutable handle."""
    binding = ProfileBinding(profile="default", harness="example-harness")

    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.harness = "other-harness"


def test_config_error_is_an_exception():
    """A misconfigured harness raises a dedicated error type."""
    assert issubclass(HarnessConfigError, Exception)


def test_harness_protocol_pins_the_four_members():
    """An adapter is anything that answers these four questions."""

    class _Adapter:
        def capabilities(self):
            return HarnessCapabilities(
                append_system_prompt=True,
                mcp_injection=True,
                tool_whitelist=False,
                stream_events=True,
                usage=True,
                session_create=True,
                session_resume=True,
            )

        def build_invocation(self, req):
            return HarnessInvocation(argv=("run", req.prompt))

        def parse_stream(self, lines):
            return [HarnessEvent(type="result", raw={"line": line}) for line in lines]

        def classify_exit(self, code, events):
            return HarnessOutcome(kind="ok" if code == 0 else "unknown")

    adapter = _Adapter()

    assert isinstance(adapter, Harness)
    assert adapter.classify_exit(0, []).kind == "ok"