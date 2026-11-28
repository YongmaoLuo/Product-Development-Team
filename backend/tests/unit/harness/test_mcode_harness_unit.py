"""TDD spec for :mod:`harness.mcode` — the MiniMax Code command line.

What is pinned here
-------------------

``mcode exec`` and ``claude`` are different command lines wearing the
same coat. The claude adapter's argv carries ``--session-id``,
``--mcp-config``, ``--allowedTools``, ``--permission-mode`` and
``--settings``; ``mcode exec`` knows none of them and refuses a command
line that carries one. A flag copied over from the claude side is
therefore not a cosmetic difference — it is a command the harness
cannot run at all.

So the cases pinned here are about what the mcode argv says and, just as
importantly, about what it never says:

1. every field of a request lands on the command line where mcode
   expects it, in the order mcode documents it;
2. no claude-only flag is ever passed;
3. a model that is not a ``provider/model`` pair is refused rather than
   passed on — mcode would otherwise pick some default and run the
   wrong model;
4. the environment a profile contributes carries the three MiniMax
   variables and nothing Anthropic — a stray ``ANTHROPIC_*`` would
   point the run at the claude provider it is supposed to have left.

The session rules are pinned alongside: a *created* session never names
an id (mcode cannot be asked to create a conversation under one), and a
*resumed* one names only a real ``mvs_`` id.

No process is spawned and no ``Popen`` is mocked (test decision point
2): the adapter's job is to produce a command line, and the command
line is the whole observable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.contract import (
    HarnessConfigError,
    SessionMode,
    SessionRef,
    TaskInvocation,
)
from harness.mcode import McodeHarness
from mcode_profiles import ProfileBinding

pytestmark = pytest.mark.unit


#: The flags claude accepts and ``mcode exec`` does not. Passing any of
#: them makes the command line unrunnable, so none of them may appear.
CLAUDE_ONLY_FLAGS = (
    "--session-id",
    "--mcp-config",
    "--allowedTools",
    "--permission-mode",
    "--settings",
)


def _full_request() -> TaskInvocation:
    """A request that exercises every field the mcode argv can carry."""
    return TaskInvocation(
        prompt="p",
        system_instruction="S",
        model="minimax/MiniMax-M3.1-Flash",
        cwd="/w",
        session=SessionRef(pinned_session_id="u1", session_id="mvs_ab"),
        mode=SessionMode.RESUME,
    )


def _profile() -> ProfileBinding:
    """A MiniMax profile, pointed at a placeholder home directory."""
    return ProfileBinding(name="acc2", home="/profile/acc2/home")


# ---------------------------------------------------------------------------
# 1. The shape of the command line
# ---------------------------------------------------------------------------


def test_argv_shape():
    """Every field lands on the command line where mcode expects it."""
    invocation = McodeHarness().build_invocation(_full_request())

    assert invocation.argv == (
        "mcode",
        "exec",
        "--output-format",
        "stream-json",
        "--permission",
        "full",
        "--prompt-mode",
        "coding",
        "--input",
        "-",
        "--cwd",
        "/w",
        "--append-system-prompt",
        "S",
        "--model",
        "minimax/MiniMax-M3.1-Flash",
        "--session",
        "mvs_ab",
    )


def test_the_working_directory_is_carried_on_the_invocation_too():
    """The invocation names the working directory, not only the argv."""
    invocation = McodeHarness().build_invocation(_full_request())

    assert invocation.cwd == "/w"


def test_optional_fields_are_left_out_when_they_are_not_asked_for():
    """A bare request carries no optional flag at all."""
    invocation = McodeHarness().build_invocation(TaskInvocation(prompt="p"))

    assert invocation.argv == (
        "mcode",
        "exec",
        "--output-format",
        "stream-json",
        "--permission",
        "full",
        "--prompt-mode",
        "coding",
        "--input",
        "-",
    )
    assert invocation.cwd is None


# ---------------------------------------------------------------------------
# 2. No claude-only flag is ever passed
# ---------------------------------------------------------------------------


def test_no_claude_only_flags():
    """The mcode argv carries none of the flags that only claude knows."""
    invocation = McodeHarness().build_invocation(_full_request())

    for flag in CLAUDE_ONLY_FLAGS:
        assert flag not in invocation.argv, (
            f"{flag!r} is a claude-only flag; mcode exec refuses it"
        )


# ---------------------------------------------------------------------------
# 3. The model has to name its provider
# ---------------------------------------------------------------------------


def test_bare_model_rejected():
    """A model without a provider is refused, naming the required form."""
    req = TaskInvocation(prompt="p", model="MiniMax-M3.1-Flash")

    with pytest.raises(HarnessConfigError) as excinfo:
        McodeHarness().build_invocation(req)

    message = str(excinfo.value)
    assert "provider/model" in message
    assert "MiniMax-M3.1-Flash" in message


def test_a_provider_model_pair_is_passed_through():
    """A model that names its provider is handed to mcode unchanged."""
    req = TaskInvocation(prompt="p", model="minimax/MiniMax-M3.1-Flash")

    invocation = McodeHarness().build_invocation(req)

    assert "--model" in invocation.argv
    assert invocation.argv[invocation.argv.index("--model") + 1] == (
        "minimax/MiniMax-M3.1-Flash"
    )


# ---------------------------------------------------------------------------
# 4. The environment a profile contributes
# ---------------------------------------------------------------------------


def test_env_delta_has_no_anthropic():
    """The profile environment carries no Anthropic variable at all."""
    invocation = McodeHarness(_profile()).build_invocation(_full_request())

    assert not [
        key for key in invocation.env_delta if key.startswith("ANTHROPIC_")
    ], f"the mcode environment carries Anthropic variables: {invocation.env_delta}"


def test_env_delta_carries_the_three_minimax_variables():
    """The profile contributes exactly the variables the launcher reads."""
    binding = _profile()

    invocation = McodeHarness(binding).build_invocation(_full_request())

    assert invocation.env_delta == {
        "MCODE_PROFILE": binding.name,
        "HOME": binding.home,
        "MINIMAX_DATA_DIR": str(Path(binding.home) / ".minimax"),
    }


def test_without_a_profile_the_environment_is_untouched():
    """No profile means no injected environment at all."""
    invocation = McodeHarness().build_invocation(_full_request())

    assert invocation.env_delta == {}


# ---------------------------------------------------------------------------
# 5. The session rules
# ---------------------------------------------------------------------------


def test_create_mode_names_no_session():
    """A created session carries no id: mcode cannot be pinned to one."""
    req = TaskInvocation(
        prompt="p",
        session=SessionRef(pinned_session_id="u1", session_id="mvs_ab"),
        mode=SessionMode.CREATE,
    )

    invocation = McodeHarness().build_invocation(req)

    assert "--session" not in invocation.argv


def test_resume_uses_the_real_mcode_session_id():
    """A resumed session names the ``mvs_`` id, not the caller's pin."""
    req = TaskInvocation(
        prompt="p",
        session=SessionRef(pinned_session_id="u1", session_id="mvs_ab"),
        mode=SessionMode.RESUME,
    )

    invocation = McodeHarness().build_invocation(req)

    assert invocation.argv[invocation.argv.index("--session") + 1] == "mvs_ab"


def test_resume_without_a_real_session_id_is_refused():
    """A resume that cannot name its conversation is a configuration error."""
    req = TaskInvocation(
        prompt="p",
        session=SessionRef(pinned_session_id="u1"),
        mode=SessionMode.RESUME,
    )

    with pytest.raises(HarnessConfigError) as excinfo:
        McodeHarness().build_invocation(req)

    assert "mvs_" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. What the harness says it can do
# ---------------------------------------------------------------------------


def test_capabilities():
    """Everything but the tool whitelist is on."""
    capabilities = McodeHarness().capabilities()

    assert capabilities.tool_whitelist is False
    assert capabilities.append_system_prompt is True
    assert capabilities.mcp_injection is True
    assert capabilities.stream_events is True
    assert capabilities.usage is True
    assert capabilities.session_create is True
    assert capabilities.session_resume is True