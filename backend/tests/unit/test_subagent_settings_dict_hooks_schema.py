"""Tests for SubagentConfig.to_settings_dict() hooks matcher schema.

TDD spec (task 7-4-2): pure-function level verification that
``to_settings_dict()['hooks']`` contains BOTH ``PreToolUse`` AND
``PostToolUse`` matcher entries, with each matcher's first hook entry
carrying ``type='command'`` and an absolute ``command`` path.

The Anthropic SDK is case-sensitive: ``PreToolUse`` (capital P/T/U) is
the only accepted matcher key for the "before tool call" event —
``preToolUse`` would be silently ignored. Similarly, ``type`` must be
exactly the string ``'command'`` (not ``'bash'`` / ``'script'``). This
file pins the full matcher schema so future refactors cannot drift
toward a wrong-case variant.

The 8 TDD acceptance bullets (one-to-one with the spec):

  * test_to_settings_dict_hooks_contains_pre_tool_use
        — 'PreToolUse' is a key of d['hooks']
  * test_to_settings_dict_hooks_contains_post_tool_use
        — 'PostToolUse' is a key of d['hooks']
  * test_to_settings_dict_pre_tool_use_command_abs_path
        — d['hooks']['PreToolUse'][0]['hooks'][0]['command'].startswith('/')
  * test_to_settings_dict_post_tool_use_command_abs_path
        — same, for PostToolUse
  * test_to_settings_dict_pre_tool_use_type_command
        — d['hooks']['PreToolUse'][0]['hooks'][0]['type'] == 'command'
  * test_to_settings_dict_post_tool_use_type_command
        — same, for PostToolUse
  * test_to_settings_dict_pre_tool_use_filename
        — d['hooks']['PreToolUse'][0]['hooks'][0]['command'] contains
          'pre_tool_use.sh'
  * test_to_settings_dict_post_tool_use_filename
        — same, for 'post_tool_use.sh'

Constraints:
  * hook_scripts are passed as ``pathlib.Path`` objects (not str) so
    the matcher picks the script up by filename substring ('pre' /
    'post').
  * model_map is fully populated so ``to_settings_dict()`` does NOT
    raise KeyError on the model_map inner lookup; this keeps each
    test focused on the matcher schema, not on the env block.
  * No disk I/O — no call to ``write_tmp_settings`` anywhere in this
    file. Pure-function only.
"""

from pathlib import Path

import pytest

from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------



@pytest.fixture
def subagent_cfg_with_hooks() -> SubagentConfig:
    """A SubagentConfig with both pre and post hook script paths.

    Both scripts are passed as absolute ``pathlib.Path`` instances so
    the matcher can locate them by filename substring ('pre' / 'post')
    AND so the serialized command is an absolute path string starting
    with ``/``.
    """
    return SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.example/anthropic',
        api_key='sk-cp-test-key-1234567890',
        auth_token='sk-cp-test-token-1234567890',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[
            Path('/abs/pre_tool_use.sh'),
            Path('/abs/post_tool_use.sh'),
        ],
    )


# ---------------------------------------------------------------------------
# Pure-function level: matcher key presence (case-sensitive)
# ---------------------------------------------------------------------------


class TestToSettingsDictHooksMatcherKeys:
    """``d['hooks']`` must contain BOTH PreToolUse AND PostToolUse keys.

    The Anthropic SDK is case-sensitive: the matcher key for the
    "before tool call" event is the camelcase form ``PreToolUse``
    (capital P, T, U). The lowercase ``preToolUse`` (or any other
    variant) is silently ignored by the SDK. This class pins the
    exact case so a future refactor cannot drift toward the
    wrong-case variant.
    """

    def test_to_settings_dict_hooks_contains_pre_tool_use(
        self, subagent_cfg_with_hooks
    ):
        """'PreToolUse' is a key of d['hooks'] (camelcase, capital P/T/U)."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        assert 'PreToolUse' in d['hooks'], (
            f"d['hooks'] missing 'PreToolUse' matcher key, got keys: "
            f"{sorted(d['hooks'].keys())!r}. The Anthropic SDK is "
            f"case-sensitive — 'preToolUse' / 'pre_tool_use' variants "
            f"are silently ignored."
        )

    def test_to_settings_dict_hooks_contains_post_tool_use(
        self, subagent_cfg_with_hooks
    ):
        """'PostToolUse' is a key of d['hooks'] (camelcase, capital P/T/U)."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        assert 'PostToolUse' in d['hooks'], (
            f"d['hooks'] missing 'PostToolUse' matcher key, got keys: "
            f"{sorted(d['hooks'].keys())!r}. The Anthropic SDK is "
            f"case-sensitive — 'postToolUse' / 'post_tool_use' variants "
            f"are silently ignored."
        )


# ---------------------------------------------------------------------------
# Pure-function level: command path is absolute
# ---------------------------------------------------------------------------


class TestToSettingsDictHooksCommandAbsPath:
    """The ``command`` field of each matcher entry is an absolute path.

    The Anthropic SDK spawns the hook as a subprocess and resolves
    the ``command`` string relative to the cwd of the Claude Code
    process. To guarantee the right script is executed regardless
    of the cwd, the path must be absolute (start with ``/``).
    """

    def test_to_settings_dict_pre_tool_use_command_abs_path(
        self, subagent_cfg_with_hooks
    ):
        """PreToolUse matcher's first hook command starts with '/'."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        cmd = d['hooks']['PreToolUse'][0]['hooks'][0]['command']
        assert cmd.startswith('/'), (
            f"PreToolUse command must be an absolute path (start with '/'), "
            f"got: {cmd!r}. Relative paths would be resolved against the "
            f"Claude Code process cwd, which is not guaranteed."
        )

    def test_to_settings_dict_post_tool_use_command_abs_path(
        self, subagent_cfg_with_hooks
    ):
        """PostToolUse matcher's first hook command starts with '/'."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        cmd = d['hooks']['PostToolUse'][0]['hooks'][0]['command']
        assert cmd.startswith('/'), (
            f"PostToolUse command must be an absolute path (start with '/'), "
            f"got: {cmd!r}. Relative paths would be resolved against the "
            f"Claude Code process cwd, which is not guaranteed."
        )


# ---------------------------------------------------------------------------
# Pure-function level: hook type is 'command' (Anthropic SDK contract)
# ---------------------------------------------------------------------------


class TestToSettingsDictHooksTypeCommand:
    """Each matcher entry's hook ``type`` is the exact string ``'command'``.

    The Anthropic SDK expects ``type`` to be one of a small enum; the
    only type currently accepted for a script-style hook is
    ``'command'`` (NOT ``'bash'`` / ``'script'`` / ``'shell'``). A
    wrong value is silently dropped by the SDK.
    """

    def test_to_settings_dict_pre_tool_use_type_command(
        self, subagent_cfg_with_hooks
    ):
        """PreToolUse matcher's first hook type is 'command'."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        hook_type = d['hooks']['PreToolUse'][0]['hooks'][0]['type']
        assert hook_type == 'command', (
            f"PreToolUse hook type must be exactly 'command' (Anthropic SDK "
            f"enum), got: {hook_type!r}. Variants like 'bash' / 'script' / "
            f"'shell' are silently dropped by the SDK."
        )

    def test_to_settings_dict_post_tool_use_type_command(
        self, subagent_cfg_with_hooks
    ):
        """PostToolUse matcher's first hook type is 'command'."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        hook_type = d['hooks']['PostToolUse'][0]['hooks'][0]['type']
        assert hook_type == 'command', (
            f"PostToolUse hook type must be exactly 'command' (Anthropic SDK "
            f"enum), got: {hook_type!r}. Variants like 'bash' / 'script' / "
            f"'shell' are silently dropped by the SDK."
        )


# ---------------------------------------------------------------------------
# Pure-function level: command contains the script filename
# ---------------------------------------------------------------------------


class TestToSettingsDictHooksCommandFilename:
    """The ``command`` string contains the script filename, identifying
    which script the SDK should spawn.

    This is the routing contract: a pre_tool_use.sh script must
    appear under the PreToolUse matcher, and post_tool_use.sh under
    the PostToolUse matcher. If a future refactor accidentally
    swapped the two (e.g. by picking the wrong substring), the
    script would be invoked at the wrong lifecycle point.
    """

    def test_to_settings_dict_pre_tool_use_filename(
        self, subagent_cfg_with_hooks
    ):
        """PreToolUse command contains 'pre_tool_use.sh' substring."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        cmd = d['hooks']['PreToolUse'][0]['hooks'][0]['command']
        assert 'pre_tool_use.sh' in cmd, (
            f"PreToolUse command must reference the pre_tool_use.sh script, "
            f"got: {cmd!r}. Filename routing is by 'pre' / 'post' substring "
            f"in the script name."
        )

    def test_to_settings_dict_post_tool_use_filename(
        self, subagent_cfg_with_hooks
    ):
        """PostToolUse command contains 'post_tool_use.sh' substring."""
        d = subagent_cfg_with_hooks.to_settings_dict()
        cmd = d['hooks']['PostToolUse'][0]['hooks'][0]['command']
        assert 'post_tool_use.sh' in cmd, (
            f"PostToolUse command must reference the post_tool_use.sh script, "
            f"got: {cmd!r}. Filename routing is by 'pre' / 'post' substring "
            f"in the script name."
        )
