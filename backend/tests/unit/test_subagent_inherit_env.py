"""Inherit mode must inject **no** provider configuration.

Why this exists
---------------
When CC Switch is not installed, providers are supposed to be inherited
from the user's own Claude Code configuration. That works only if
nothing here overrides it — ``--settings`` **outranks**
``~/.claude/settings.json``, so a single stray ``ANTHROPIC_*`` entry in
the payload silently replaces the user's endpoint, credentials or model
choice while looking exactly like "we did not touch anything".

The failure is quiet and expensive, which is why it is pinned here
rather than left to review: the dispatch still succeeds, just against a
provider the user never chose — or fails with a 401 whose cause is a
settings file they never wrote.

The other half of the contract is that hooks are **not** dropped. They
are this project's own instrumentation (the per-subagent activity log
the watchdog reads to distinguish "stuck in an LLM call" from "a long
pytest run"), not provider configuration. A version of this that
achieved "inject nothing" by emitting no settings file at all would pass
a naive reading of the requirement and quietly disable stuck-subagent
detection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from subagent_config import SubagentConfig  # noqa: E402

#: Every key the normal path copies into a settings payload. Inherit mode
#: must emit none of them.
ANTHROPIC_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SUMMARIZE_MODEL",
)


def _inherit_cfg(**overrides) -> SubagentConfig:
    kwargs = dict(
        # Deliberately populated: if a future edit reintroduces the env
        # block, it will do so with these values and the assertions below
        # will catch it rather than passing on an empty config.
        provider_name="irrelevant-in-inherit-mode",
        base_url="https://should-not-appear.invalid",
        api_key="should-not-appear",
        auth_token="should-not-appear",
        model_env={"ANTHROPIC_MODEL": "should-not-appear"},
        inherit_env=True,
    )
    kwargs.update(overrides)
    return SubagentConfig(**kwargs)


class TestNothingProviderShapedIsEmitted:
    @pytest.mark.parametrize("key", ANTHROPIC_KEYS)
    def test_the_key_is_absent(self, key):
        assert key not in _inherit_cfg().to_settings_dict()["env"]

    def test_no_anthropic_key_at_all(self):
        """Catches a key nobody thought to enumerate.

        ``ANTHROPIC_BASE_URL_VENDOR_B``-style vendor variants live in the
        parent env on a real machine; a prefix sweep would leak them.
        """
        env = _inherit_cfg().to_settings_dict()["env"]
        leaked = [k for k in env if k.startswith("ANTHROPIC_")]
        assert not leaked, (
            f"inherit mode leaked provider configuration: {leaked}. "
            f"--settings outranks ~/.claude/settings.json, so any entry "
            f"here silently replaces the user's own configuration."
        )

    def test_the_injected_credentials_do_not_appear_anywhere(self):
        payload = _inherit_cfg().to_settings_dict()
        rendered = repr(payload)
        for value in ("should-not-appear", "should-not-appear.invalid"):
            assert value not in rendered


class TestHooksSurvive:
    def test_hook_scripts_are_still_wired(self):
        cfg = _inherit_cfg(
            hook_scripts=[
                Path("/abs/pre_tool_use.sh"),
                Path("/abs/post_tool_use.sh"),
            ]
        )
        hooks = cfg.to_settings_dict()["hooks"]
        assert hooks, (
            "inherit mode must keep the project's own instrumentation; "
            "dropping it would disable stuck-subagent detection"
        )
        assert "PreToolUse" in hooks and "PostToolUse" in hooks

    def test_no_hook_scripts_still_yields_an_empty_hooks_dict(self):
        assert _inherit_cfg().to_settings_dict()["hooks"] == {}


class TestTheSettingsPathIsStillPublished:
    def test_claude_settings_path_is_emitted_when_known(self):
        """The hooks read it to identify the originating settings file.
        It is not an ANTHROPIC_* key and carries no provider config."""
        cfg = _inherit_cfg(settings_file_path=Path("/tmp/subagent_settings_x.json"))
        env = cfg.to_settings_dict()["env"]
        assert env == {"CLAUDE_SETTINGS_PATH": "/tmp/subagent_settings_x.json"}

    def test_the_env_is_empty_when_no_settings_path_is_known(self):
        assert _inherit_cfg().to_settings_dict()["env"] == {}


class TestItDoesNotRaiseWithoutCredentials:
    def test_completely_empty_credentials_are_fine_in_inherit_mode(self):
        """There is nothing to fill in — that is the point of the mode.

        The ANTHROPIC_* minimum is a real contract on the *normal* path
        (see the control test below); in inherit mode the correct number
        of ANTHROPIC_* entries is zero, and asserting otherwise would
        make the mode impossible to use.
        """
        cfg = SubagentConfig(inherit_env=True)
        payload = cfg.to_settings_dict()
        assert payload["env"] == {}

    def test_it_does_not_consult_the_database_for_a_model(self):
        """The normal path falls back to the CC Switch row for
        ``ANTHROPIC_MODEL``. In inherit mode there is no row to consult
        and no model to stamp — doing so would override the user's."""
        env = _inherit_cfg(provider_name="vendor-a-pro").to_settings_dict()["env"]
        assert "ANTHROPIC_MODEL" not in env


class TestTheNormalPathIsUnchanged:
    """The control. Without these, "no ANTHROPIC_* keys" could be
    satisfied by breaking the normal path instead of by the flag."""

    def test_the_normal_path_still_emits_provider_configuration(self):
        cfg = SubagentConfig(
            provider_name="vendor-a-pro",
            base_url="https://api.vendor-a.example/anthropic",
            api_key="sk-cp-test-key",
            auth_token="sk-cp-test-token",
        )
        env = cfg.to_settings_dict()["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-a.example/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cp-test-token"

    def test_the_normal_path_still_meets_the_credential_floor(self):
        """The control for the assertion inherit mode skips.

        Worth being precise about what that floor checks: it counts
        ANTHROPIC_* **keys**, not whether their values are filled in. A
        config with no credentials at all still produces three (endpoint
        + the two credential fields, all empty), so the assertion guards
        against the block being structurally removed — the drift it was
        written for — and not against empty values.

        ``ANTHROPIC_MODEL`` is deliberately **not** counted. It is present
        only when a model actually resolves (the caller's ``model_env`` or
        the provider row); this project does not name a fallback model, so
        an unresolved model leaves the field out and the Claude CLI reads
        the local config instead.

        Empty values are caught one level up, by the provider walk
        declining to select a provider whose config has no base_url.
        """
        cfg = SubagentConfig(provider_name="vendor-a-pro")
        env = cfg.to_settings_dict()["env"]
        keys = [k for k in env if k.startswith("ANTHROPIC_")]
        assert len(keys) >= 3, (
            "the normal path's env block is a structural contract; "
            f"found only {keys}"
        )

    def test_inherit_mode_is_off_by_default(self):
        """The flag must be opt-in, so every existing caller keeps the
        behaviour it had before the mode existed."""
        assert SubagentConfig().inherit_env is False
