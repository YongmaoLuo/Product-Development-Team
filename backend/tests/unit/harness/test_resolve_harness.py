"""TDD spec for :mod:`harness.registry` — the one place a backend is chosen.

The registry is the only module in the harness layer that reads the
environment to decide *which* backend runs. Everything above it asks the
registry, so "which backend is this process driving" has exactly one
answer in the codebase — and one place where a wrong answer is refused.

The contract points pinned here:

1. Nothing configured means the claude backend.
2. ``PDT_HARNESS=mcode`` means the mcode backend.
3. Every other value — empty, wrong case, stray whitespace, a typo — is
   refused with the legal values named, and **no factory is called**.
4. The configuration is resolved once and cached; a later environment
   change does not move it.
5. The factory that builds the adapter is called once, not once per
   lookup.

The adapters are stubbed at the factory boundary (test decision point 2):
the registry is handed a factory, so no test builds a real adapter, spawns
a process, or needs an adapter module to exist.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys

import pytest

from harness import registry, startup
from harness.contract import (
    HarnessCapabilities,
    HarnessConfigError,
    HarnessEvent,
    HarnessInvocation,
    HarnessOutcome,
)
from harness.registry import (
    RuntimeConfig,
    get_runtime_config,
    reset_runtime_config_for_tests,
    resolve_harness,
)
from harness.startup import StartupConfig

pytestmark = pytest.mark.unit


class _FakeHarness:
    """A stand-in adapter — it answers the four questions the protocol asks."""

    def __init__(self, name: str) -> None:
        self.name = name

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            append_system_prompt=False,
            mcp_injection=False,
            tool_whitelist=False,
            stream_events=False,
            usage=False,
            session_create=False,
            session_resume=False,
        )

    def build_invocation(self, req) -> HarnessInvocation:
        return HarnessInvocation(argv=(self.name, req.prompt))

    def parse_stream(self, lines) -> list:
        return [HarnessEvent(type="result", raw={"line": line}) for line in lines]

    def classify_exit(self, code, events) -> HarnessOutcome:
        return HarnessOutcome(kind="ok" if code == 0 else "unknown")


def _recording_factories(calls):
    """Two factories that record which one the registry reached for."""

    def _make(name):
        def _factory():
            calls.append(name)
            return _FakeHarness(name)

        return _factory

    return {"claude": _make("claude"), "mcode": _make("mcode")}


def _fake_default_factory(calls=None):
    """A stand-in for the built-in factory lookup.

    It answers with the same shape the real one does — a constructor
    that builds an adapter — so a test can count how many times the
    adapter was actually built.
    """

    def _default_factory(name):
        def _factory():
            if calls is not None:
                calls.append(name)
            return _FakeHarness(name)

        return _factory

    return _default_factory


# ---------------------------------------------------------------------------
# 0. The environment is frozen, not inherited
# ---------------------------------------------------------------------------


def test_frozen_env_isolates_the_harness_environment(frozen_env, tmp_path):
    """The four variables the harness layer reads are pinned, not inherited."""
    # The backend variable is *absent*, so the default path is observable
    # and the shell of whoever runs the test cannot decide it.
    assert "PDT_HARNESS" not in os.environ

    # The profile variables point into the test's own throwaway tree,
    # never at the developer's home.
    assert os.environ["MCODE_PROFILE"] == frozen_env["MCODE_PROFILE"]
    assert os.environ["HOME"] == frozen_env["HOME"]
    assert os.environ["MINIMAX_DATA_DIR"] == frozen_env["MINIMAX_DATA_DIR"]
    assert str(tmp_path) in os.environ["HOME"]
    assert str(tmp_path) in os.environ["MINIMAX_DATA_DIR"]


# ---------------------------------------------------------------------------
# 1. The default is claude
# ---------------------------------------------------------------------------


def test_unset_is_claude(frozen_env):
    """Nothing configured means the claude backend."""
    calls = []

    harness = resolve_harness({}, factories=_recording_factories(calls))

    assert harness.name == "claude"
    assert calls == ["claude"]


def test_unset_in_a_partial_environment_is_claude(frozen_env):
    """An environment without the variable is the same as an empty one."""
    calls = []

    harness = resolve_harness(
        {"OTHER": "value"}, factories=_recording_factories(calls)
    )

    assert harness.name == "claude"
    assert calls == ["claude"]


# ---------------------------------------------------------------------------
# 2. An explicit choice is honoured
# ---------------------------------------------------------------------------


def test_mcode_selected(frozen_env):
    """``PDT_HARNESS=mcode`` means the mcode backend."""
    calls = []

    harness = resolve_harness(
        {"PDT_HARNESS": "mcode"}, factories=_recording_factories(calls)
    )

    assert harness.name == "mcode"
    assert calls == ["mcode"]


def test_claude_selected_explicitly(frozen_env):
    """``PDT_HARNESS=claude`` means the claude backend."""
    calls = []

    harness = resolve_harness(
        {"PDT_HARNESS": "claude"}, factories=_recording_factories(calls)
    )

    assert harness.name == "claude"
    assert calls == ["claude"]


# ---------------------------------------------------------------------------
# 3. Anything else is refused, and nothing is built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "Claude", "MCODE", " mcode", "mcdoe"],
    ids=["empty", "wrong-case-claude", "wrong-case-mcode", "leading-space", "typo"],
)
def test_invalid_values_raise(value, frozen_env):
    """A value the process does not know is refused, not repaired."""
    calls = []

    with pytest.raises(HarnessConfigError) as excinfo:
        resolve_harness(
            {"PDT_HARNESS": value}, factories=_recording_factories(calls)
        )

    # Nothing was built: the value is checked before any factory runs.
    assert calls == []

    # The message names the legal values and the offending one.
    message = str(excinfo.value)
    assert "claude" in message
    assert "mcode" in message
    assert repr(value) in message


def test_invalid_value_does_not_fall_back_to_claude(frozen_env):
    """A refused value does not quietly become the default backend."""
    calls = []

    with pytest.raises(HarnessConfigError):
        resolve_harness({"PDT_HARNESS": "MCODE"}, factories=_recording_factories(calls))

    assert calls == []


# ---------------------------------------------------------------------------
# 4. The configuration is resolved once
# ---------------------------------------------------------------------------


def test_resolved_once(frozen_env, monkeypatch):
    """The configuration is resolved once; a later env change does not move it."""
    monkeypatch.setattr(registry, "_default_factory", _fake_default_factory())
    reset_runtime_config_for_tests()

    first = get_runtime_config()
    assert first.name == "claude"

    # The environment changes underneath the process.
    monkeypatch.setenv("PDT_HARNESS", "mcode")

    second = get_runtime_config()

    assert second is first
    assert second.name == "claude"
    assert second.harness.name == "claude"


def test_reset_makes_the_next_lookup_resolve_again(frozen_env, monkeypatch):
    """The reset exists so a test can ask for a fresh resolution."""
    calls = []
    monkeypatch.setattr(registry, "_default_factory", _fake_default_factory(calls))
    reset_runtime_config_for_tests()

    get_runtime_config()
    reset_runtime_config_for_tests()
    monkeypatch.setenv("PDT_HARNESS", "mcode")
    second = get_runtime_config()

    assert calls == ["claude", "mcode"]
    assert second.name == "mcode"


# ---------------------------------------------------------------------------
# 5. The factory runs once
# ---------------------------------------------------------------------------


def test_factory_called_once(frozen_env, monkeypatch):
    """Two lookups build the adapter once."""
    calls = []
    monkeypatch.setattr(registry, "_default_factory", _fake_default_factory(calls))
    reset_runtime_config_for_tests()

    first = get_runtime_config()
    second = get_runtime_config()

    assert calls == ["claude"]
    assert second is first
    assert first.harness is second.harness


# ---------------------------------------------------------------------------
# 6. The shape of what comes back
# ---------------------------------------------------------------------------


def test_runtime_config_carries_the_harness_and_its_name(frozen_env):
    """The configuration says which backend it is, not just what it is."""
    calls = []

    harness = resolve_harness({}, factories=_recording_factories(calls))
    config = RuntimeConfig(harness=harness, name="claude")

    assert config.harness is harness
    assert config.name == "claude"


def test_runtime_config_is_frozen(frozen_env):
    """A resolved configuration cannot be rewritten after the fact."""
    config = RuntimeConfig(harness=_FakeHarness("claude"), name="claude")

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.name = "mcode"


# ---------------------------------------------------------------------------
# 7. The default path stays out of the import path
# ---------------------------------------------------------------------------


def test_importing_the_registry_does_not_import_an_adapter():
    """Importing the registry must not drag in an adapter module.

    The adapter modules bring their own dependencies; a process that only
    asked a question about configuration should not pay for them. The
    probe loads the module body a second time under a private name, so
    the check is about *this* import rather than about what some earlier
    test happened to import.
    """
    spec = importlib.util.spec_from_file_location(
        "harness._registry_import_probe", registry.__file__
    )
    probe = importlib.util.module_from_spec(spec)

    before = set(sys.modules)
    # The module has to be in ``sys.modules`` while its body runs — a
    # dataclass looks its own defining module up there.
    sys.modules[spec.name] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.modules.pop(spec.name, None)
    imported = set(sys.modules) - before

    assert "harness.claude" not in imported
    assert "harness.mcode" not in imported


def test_the_registry_binds_no_adapter_at_import_time():
    """The adapter classes live behind the factory, not in the namespace."""
    assert not hasattr(registry, "ClaudeHarness")
    assert not hasattr(registry, "McodeHarness")


def test_the_backend_env_var_is_the_documented_one():
    """The variable the operator sets is the one the registry reads."""
    assert registry.HARNESS_ENV_VAR == "PDT_HARNESS"
    assert registry.DEFAULT_HARNESS_NAME == "claude"
    assert tuple(registry.HARNESS_NAMES) == ("claude", "mcode")


# ---------------------------------------------------------------------------
# 8. The environment reaches the decision
# ---------------------------------------------------------------------------


def test_the_startup_decides_from_the_environment_it_is_given(frozen_env, monkeypatch):
    """The caller's environment reaches the start-up unchanged.

    The MiniMax Code account the process runs on is decided in
    :mod:`harness.startup`, from the environment the caller handed
    over. If the registry answered about the *process* environment
    instead, a caller could not reproduce — or test — the decision it
    asked for, and the profile map would be read for a provider the
    caller never named.
    """
    seen = {}

    def _fake_startup(env, **kwargs):
        seen["env"] = env
        return StartupConfig(name="mcode", harness=_FakeHarness("mcode"))

    monkeypatch.setattr(startup, "resolve_startup_config", _fake_startup)

    env = {"PDT_HARNESS": "mcode", "PDT_PROVIDER_NAME": "some-provider"}
    harness = resolve_harness(env)

    # The environment is handed over as it was given — the same object,
    # not a copy of the process environment.
    assert seen["env"] is env
    assert harness.name == "mcode"