"""Tests for SubagentConfig empty hook_scripts boundary.

TDD spec (task 7-4-3): boundary case for ``hook_scripts=[]`` (the
default). The contract pins two related behaviors:

  * **In-memory** (``to_settings_dict()``): when no hook scripts are
    configured, the returned settings dict must NOT contain any
    ``PreToolUse`` / ``PostToolUse`` matcher. The contract is
    intentionally written as an "OR" form — either the 'hooks' key
    is absent, OR it is present as an empty dict, OR (the catch-all)
    the PreToolUse / PostToolUse keys are simply not present in
    ``d.get('hooks', {})``. This is the **反例测试 (counter-example
    test)** to 7-4-1 and 7-4-2: those tasks pin that hook_scripts
    non-empty produces a populated ``hooks`` dict, this task pins
    that hook_scripts empty produces an empty (or absent) one.

  * **Disk** (``write_tmp_settings()``): the on-disk JSON must NOT
    contain a top-level ``'hooks'`` key at all. This is strictly
    stronger than the in-memory contract — the rationale ("避免
    写入空 hooks") is that the Claude Code SDK receives the
    settings file as the ``--settings`` flag payload, and a
    stray empty ``hooks: {}`` block can either be silently
    dropped or raise a schema error depending on SDK version. To
    guarantee forward compatibility, ``write_tmp_settings``
    must strip the empty ``hooks`` block from the on-disk
    representation.

The 4 TDD acceptance bullets (one-to-one with the spec):

  * test_to_settings_dict_no_hooks_when_empty
        — hook_scripts=[] → ``'hooks' not in d`` OR ``d['hooks'] == {}``
  * test_to_settings_dict_no_pre_tool_use_when_empty
        — ``'PreToolUse' not in d.get('hooks', {})`` (works whether
          'hooks' is absent, present-but-empty, or contains other
          unexpected keys but never PreToolUse)
  * test_to_settings_dict_no_post_tool_use_when_empty
        — same shape for PostToolUse
  * test_write_tmp_settings_no_hooks_field_when_empty
        — hook_scripts=[] → ``json.loads(disk)['hooks'] is KeyError``,
          i.e. the disk file has no 'hooks' key

Constraints:
  * hook_scripts is the **default** empty list (i.e. not passed
    to the SubagentConfig constructor at all). This matches the
    agent.py:874-877 production wiring where the list is always
    populated, so this test is a true boundary that the
    production code path does not normally exercise.
  * model_map is fully populated so ``to_settings_dict()`` does
    NOT raise KeyError on the model_map inner lookup; this keeps
    each test focused on the empty-hooks contract, not on the
    env block.
  * Each test reads ``hook_scripts`` from the fixture — the
    fixture intentionally omits it so we exercise the
    ``field(default_factory=list)`` default rather than an
    explicit empty-list assignment.
"""

import json
from pathlib import Path

import pytest

from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------



@pytest.fixture
def subagent_cfg_no_hooks() -> SubagentConfig:
    """A SubagentConfig with NO hook_scripts kwarg passed (default empty list).

    The dataclass declares ``hook_scripts: List[Path] = field(
    default_factory=list)`` so constructing without the kwarg
    exercises the empty-list default — this is the boundary case
    the test pins.
    """
    return SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.example/anthropic',
        api_key='sk-cp-test-key-1234567890',
        auth_token='sk-cp-test-token-1234567890',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        # NOTE: no hook_scripts kwarg → default_factory(list) → []
    )


@pytest.fixture
def write_path_no_hooks(subagent_cfg_no_hooks: SubagentConfig):
    """Call ``write_tmp_settings()`` and yield the file path.

    Cleans up the tmpfile in teardown so repeated test runs do
    not pollute ``/tmp``. Decision 3 says the file is NOT
    auto-cleaned by atexit — we explicitly unlink here.

    Skips the test (rather than failing) if ``/tmp`` is unwritable,
    so a sandboxed CI that mounts a read-only /tmp does not mask
    the disk-level assertion with an OSError.
    """
    try:
        path = subagent_cfg_no_hooks.write_tmp_settings()
    except OSError as e:
        pytest.skip(f"/tmp is not writable in this environment: {e}")
    try:
        yield path
    finally:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# In-memory: empty hooks is acceptable in either form
# ---------------------------------------------------------------------------


class TestToSettingsDictNoHooksWhenEmpty:
    """``to_settings_dict()`` must not surface a populated hooks block
    when ``hook_scripts=[]``.

    The "OR" contract pins two acceptable shapes:

      A. ``'hooks' not in d`` — the implementation omits the key
         entirely. The on-disk file mirrors this.
      B. ``d['hooks'] == {}`` — the implementation includes the key
         but leaves it empty. The matcher sub-keys are absent.

    The reverse-test for hook_scripts non-empty is 7-4-1
    (test_to_settings_dict_has_hooks_key) and 7-4-2
    (test_to_settings_dict_hooks_contains_pre_tool_use). This
    class is the 7-4-1 / 7-4-2 反例 — same code path, opposite
    input.
    """

    def test_to_settings_dict_no_hooks_when_empty(
        self, subagent_cfg_no_hooks: SubagentConfig
    ):
        """hook_scripts=[] → 'hooks' not in d OR d['hooks'] == {}.

        Both forms are acceptable per the spec. The test will fail
        only if the implementation produces a hooks dict that
        contains matcher entries (e.g. ``{'PreToolUse': []}``) —
        those would be silently misinterpreted by the SDK as
        "PreToolUse is configured but with no commands", which is
        a schema error in some SDK versions.
        """
        d = subagent_cfg_no_hooks.to_settings_dict()
        assert 'hooks' not in d or d['hooks'] == {}, (
            f"to_settings_dict() must not produce a populated hooks "
            f"block when hook_scripts=[]. Expected one of: "
            f"(A) 'hooks' key absent, or (B) d['hooks'] == {{}}. "
            f"Got d['hooks'] = {d.get('hooks', '<<absent>>')!r}."
        )

    def test_to_settings_dict_no_pre_tool_use_when_empty(
        self, subagent_cfg_no_hooks: SubagentConfig
    ):
        """'PreToolUse' is NOT a key of d.get('hooks', {}) (empty
        default fallback covers both contract shapes).

        The ``d.get('hooks', {})`` fallback handles both acceptable
        forms uniformly: if 'hooks' is absent, the default ``{}``
        has no PreToolUse; if d['hooks'] is empty, same. The only
        way this test fails is if the implementation explicitly
        emits ``hooks: {'PreToolUse': []}`` (empty matcher list)
        for the empty case — which is exactly the regression we
        want to catch.
        """
        d = subagent_cfg_no_hooks.to_settings_dict()
        assert 'PreToolUse' not in d.get('hooks', {}), (
            f"to_settings_dict() must not produce a PreToolUse matcher "
            f"when hook_scripts=[]. The SDK can misinterpret "
            f"PreToolUse=[] as 'matcher configured with no commands' "
            f"and raise a schema error. Got d.get('hooks', {{}}) = "
            f"{d.get('hooks', {})!r}."
        )

    def test_to_settings_dict_no_post_tool_use_when_empty(
        self, subagent_cfg_no_hooks: SubagentConfig
    ):
        """'PostToolUse' is NOT a key of d.get('hooks', {}) (empty
        default fallback covers both contract shapes).

        Same shape as the PreToolUse test, pinned independently
        so a regression in the PreToolUse / PostToolUse split (e.g.
        one branch added by mistake) is caught per-key.
        """
        d = subagent_cfg_no_hooks.to_settings_dict()
        assert 'PostToolUse' not in d.get('hooks', {}), (
            f"to_settings_dict() must not produce a PostToolUse matcher "
            f"when hook_scripts=[]. The SDK can misinterpret "
            f"PostToolUse=[] as 'matcher configured with no commands' "
            f"and raise a schema error. Got d.get('hooks', {{}}) = "
            f"{d.get('hooks', {})!r}."
        )


# ---------------------------------------------------------------------------
# Disk-level: the on-disk JSON must not carry a 'hooks' key at all
# ---------------------------------------------------------------------------


class TestWriteTmpSettingsNoHooksFieldWhenEmpty:
    """``write_tmp_settings()`` must not persist a ``'hooks'`` key
    in the on-disk JSON when ``hook_scripts=[]``.

    The in-memory contract is lenient (either form is acceptable),
    but the **disk** contract is strict: the file passed to
    ClaudeCodingTool's ``--settings`` flag must not have a
    ``'hooks'`` key. The rationale is that:

      1. The Claude Code SDK reads the file as the ``--settings``
         flag payload. An empty ``hooks: {}`` block is a no-op
         for the SDK's current schema validator, but downstream
         consumers (e.g. 3rd-party hooks that read the file
         themselves) may interpret it as "hooks are configured,
         with no actual scripts" and emit spurious "no-op
         invocation" log lines.
      2. Forward compatibility: an SDK that tightens the schema
         and rejects empty ``hooks`` objects would break
         silently. Stripping the key on disk guarantees forward
         compatibility without requiring code changes.

    This is the disk-level 反例 test for 7-3
    (test_written_file_has_7_env_keys): 7-3 pins the env block
    (7 ANTHROPIC_* keys), this class pins the hooks block
    (``'hooks'`` absent when empty).
    """

    def test_write_tmp_settings_no_hooks_field_when_empty(
        self, write_path_no_hooks: Path
    ) -> None:
        """hook_scripts=[] → on-disk JSON has no 'hooks' key.

        Reads back the tmpfile written by ``write_tmp_settings()``
        and asserts that the parsed JSON does not contain a
        top-level ``'hooks'`` field. The assertion uses
        ``'hooks' not in loaded`` (not ``loaded['hooks'] != {}``)
        because the contract is strictly "absent" — the file
        must not carry the key at all.
        """
        payload = json.loads(write_path_no_hooks.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), (
            f"settings file must parse to a dict, got {type(payload).__name__}: "
            f"{payload!r}"
        )
        assert 'hooks' not in payload, (
            f"settings file must not carry a 'hooks' key when "
            f"hook_scripts=[]. An empty hooks block is a forward-"
            f"compatibility hazard (some SDK versions reject it). "
            f"Got payload keys: {sorted(payload.keys())!r}."
        )
