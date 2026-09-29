"""Tests for SubagentConfig.to_settings_dict() hooks contract.

TDD spec (task 7-4-1): pure-function level verification that
``to_settings_dict()`` returns a dict containing a top-level ``'hooks'``
key (decision 4 plumbing). These tests do NOT touch disk — they only
exercise the in-memory serialization path. No ``write_tmp_settings``
call is made.

The 3 TDD acceptance bullets (one-to-one with the spec):

  * test_to_settings_dict_has_hooks_key
        — when hook_scripts is provided, 'hooks' is a key of the
          returned dict
  * test_to_settings_dict_hooks_is_dict
        — d['hooks'] is a dict (never list, never None, never the
          hook commands list directly)
  * test_to_settings_dict_hooks_nonempty_when_scripts_given
        — hook_scripts=[Path('/abs/pre_tool_use.sh'),
                        Path('/abs/post_tool_use.sh')] → d['hooks']
          is a non-empty dict (not the empty-default {})

Boundary case also pinned:
  * test_to_settings_dict_hooks_empty_dict_when_no_scripts
        — hook_scripts=[] (default) → d['hooks'] == {} (the
          "no scripts given → empty dict" contract documented in
          ``_build_hooks_payload``)

Constraints:
  * hook_scripts are passed as ``pathlib.Path`` objects (not str)
  * model_env carries a representative flat ANTHROPIC_* entry so
    ``to_settings_dict()`` reaches the hooks branch; 2026-09-13
    contract: model management is delegated to CC Switch, so the
    config carries a flat ``model_env`` passthrough (no tiered map).
  * No disk I/O — no call to ``write_tmp_settings`` anywhere in this
    file.
"""

from pathlib import Path

import pytest

from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_config_with_hooks() -> SubagentConfig:
    """Build a SubagentConfig with pre/post tool use hook scripts.

    model_env carries a representative flat ANTHROPIC_* entry —
    2026-09-13 contract: flat passthrough from the CC Switch provider
    row, no tier lookup.
    """
    return SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.example/anthropic',
        api_key='sk-cp-test-key-1234567890',
        auth_token='sk-cp-test-token-1234567890',
        model_env={
            'ANTHROPIC_MODEL': 'Vendor A-M3',
            'ANTHROPIC_DEFAULT_SONNET_MODEL': 'Vendor A-M2',
        },
        hook_scripts=[
            Path('/abs/pre_tool_use.sh'),
            Path('/abs/post_tool_use.sh'),
        ],
    )


@pytest.fixture
def subagent_cfg_with_hooks() -> SubagentConfig:
    """A SubagentConfig with both pre and post hook script paths."""
    return _build_config_with_hooks()


@pytest.fixture
def subagent_cfg_no_hooks() -> SubagentConfig:
    """A SubagentConfig with no hook scripts (default empty list)."""
    return SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.example/anthropic',
        api_key='sk-cp-test-key-1234567890',
        auth_token='sk-cp-test-token-1234567890',
        model_env={
            'ANTHROPIC_MODEL': 'Vendor A-M3',
            'ANTHROPIC_DEFAULT_SONNET_MODEL': 'Vendor A-M2',
        },
    )


# ---------------------------------------------------------------------------
# Pure-function level: hooks key contract
# ---------------------------------------------------------------------------


class TestToSettingsDictHooksContract:
    """Pure-function level verification of the to_settings_dict() hooks key."""

    def test_to_settings_dict_has_hooks_key(self, subagent_cfg_with_hooks):
        """'hooks' is a top-level key of the returned dict when
        hook_scripts is non-empty.

        The Claude Code SDK settings.json schema has a 'hooks' key
        at the top level (decision 4 plumbing). When hook_scripts
        is provided, ``to_settings_dict()`` must surface that key.
        """
        d = subagent_cfg_with_hooks.to_settings_dict()
        assert 'hooks' in d, (
            f"to_settings_dict() must return a dict with 'hooks' key, "
            f"got keys: {sorted(d.keys())!r}"
        )

    def test_to_settings_dict_hooks_is_dict(self, subagent_cfg_with_hooks):
        """d['hooks'] is a dict, not a list, not None, not the raw
        hook command strings.

        The SDK's settings.json schema requires hooks to be a dict
        keyed by event name (PreToolUse / PostToolUse). A list, None,
        or bare command list would either be silently ignored by the
        SDK or raise a schema error.
        """
        d = subagent_cfg_with_hooks.to_settings_dict()
        assert isinstance(d['hooks'], dict), (
            f"d['hooks'] must be a dict, got {type(d['hooks']).__name__}: "
            f"{d['hooks']!r}"
        )

    def test_to_settings_dict_hooks_nonempty_when_scripts_given(
        self, subagent_cfg_with_hooks
    ):
        """When hook_scripts contains 2 Path objects (one pre, one
        post), d['hooks'] is a non-empty dict.

        The boundary case is the empty-default ``{}`` produced when
        ``hook_scripts=[]``. With scripts present, the dict must
        contain at least one of PreToolUse / PostToolUse keys (in
        this case both, since the fixture passes one of each).
        """
        d = subagent_cfg_with_hooks.to_settings_dict()
        assert len(d['hooks']) > 0, (
            f"d['hooks'] must be non-empty when hook_scripts has 2 entries, "
            f"got empty dict: {d['hooks']!r}"
        )
        # Stronger assertion: both pre and post paths should be
        # present because the fixture explicitly provides one of each.
        assert 'PreToolUse' in d['hooks'], (
            f"d['hooks'] missing PreToolUse key: {sorted(d['hooks'].keys())!r}"
        )
        assert 'PostToolUse' in d['hooks'], (
            f"d['hooks'] missing PostToolUse key: {sorted(d['hooks'].keys())!r}"
        )

    def test_to_settings_dict_hooks_empty_dict_when_no_scripts(
        self, subagent_cfg_no_hooks
    ):
        """Boundary: with hook_scripts=[], d['hooks'] == {}.

        This pins the documented empty-default behavior in
        ``_build_hooks_payload`` so future refactors cannot
        silently swap it for a list, None, or omit the key.
        """
        d = subagent_cfg_no_hooks.to_settings_dict()
        assert 'hooks' in d, (
            f"to_settings_dict() must still return 'hooks' key when no "
            f"scripts given (per the d['hooks'] == {{}} contract), got keys: "
            f"{sorted(d.keys())!r}"
        )
        assert isinstance(d['hooks'], dict), (
            f"d['hooks'] must be a dict even when no scripts given, "
            f"got {type(d['hooks']).__name__}: {d['hooks']!r}"
        )
        assert d['hooks'] == {}, (
            f"d['hooks'] must be {{}} when hook_scripts is empty, "
            f"got: {d['hooks']!r}"
        )
