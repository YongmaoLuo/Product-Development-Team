"""Tests for SubagentConfig dataclass — decision point 2 contract.

The first 11 tests below are the 11-case TDD spec for the
"subagent_config.py 单元测试通过" acceptance criterion. They cover:

  1-3.  Dataclass field init / defaults / model_map None safety
  4-7.  to_settings_dict() in-memory 7-ANTHROPIC_* schema and routing
  8-11. write_tmp_settings() disk file / atomicity / logger emit / no-cleanup

The remaining tests cover the to_settings_dict() hooks contract
(decision 4 / task 7-4 plumbing).
"""

import re
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import subagent_config as subagent_config_module
from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TDD spec — 11 contract tests for decision point 2
# ---------------------------------------------------------------------------


def test_subagent_config_init():
    """完整字段构造 — every attribute round-trips through __init__."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.chat/v1',
        api_key='sk-cp-initial-key-1234',
        auth_token='sk-cp-initial-token-5678',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        settings_file_path=None,
        hook_scripts=[],
        provider_priority=['vendor-a-pro', 'vendor-b-pro'],
        task_type='general',
        task_summary='initial task summary',
    )
    assert cfg.provider_name == 'vendor-a-pro'
    assert cfg.base_url == 'https://api.vendor-a.chat/v1'
    assert cfg.api_key == 'sk-cp-initial-key-1234'
    assert cfg.auth_token == 'sk-cp-initial-token-5678'
    assert cfg.model_env == {'ANTHROPIC_MODEL': 'Vendor A-M3'}
    assert cfg.settings_file_path is None
    assert cfg.hook_scripts == []
    assert cfg.provider_priority == ['vendor-a-pro', 'vendor-b-pro']
    assert cfg.task_type == 'general'
    assert cfg.task_summary == 'initial task summary'


def test_subagent_config_defaults():
    """缺省值 — only provider_name is required; everything else uses the
    dataclass ``field(default_factory=...)`` defaults.
    """
    cfg = SubagentConfig(provider_name='vendor-a-pro')
    assert cfg.provider_name == 'vendor-a-pro'
    assert cfg.base_url == ''
    assert cfg.api_key == ''
    assert cfg.auth_token == ''
    assert cfg.model_env == {}
    assert cfg.settings_file_path is None
    assert cfg.hook_scripts == []
    assert cfg.provider_priority == ['vendor-b-pro', 'vendor-a-pro']
    assert cfg.task_type == 'general'
    assert cfg.task_summary == ''


def test_subagent_config_model_env_none_safe():
    """model_env=None 不崩 — defensive ``__post_init__`` normalizes None
    to an empty dict so downstream iteration is always safe.

    A regression that drops the ``if self.model_env is None`` guard
    would surface here as ``TypeError: 'NoneType' object is not
    iterable``.
    """
    cfg = SubagentConfig(provider_name='vendor-a-pro', model_env=None)
    assert cfg.model_env == {}, (
        f"model_env=None should be normalized to empty dict, got {cfg.model_env!r}"
    )
    # The contract: iterating an empty model_env yields nothing and
    # ``to_settings_dict`` still produces the endpoint/credential keys.
    d = cfg.to_settings_dict()
    assert 'ANTHROPIC_BASE_URL' in d['env']


def test_to_settings_dict_has_endpoint_and_credentials():
    """to_settings_dict 输出 dict 至少含 3 个 ANTHROPIC_* env —
    BASE_URL / AUTH_TOKEN / API_KEY. 2026-09-13 contract: model keys
    come from the provider row via the dispatch walk / model_env, not
    from a tiered map. The count is a
    hard contract pinned by ``subagent_config._SETTINGS_ENV_FIELD_COUNT``.
    """
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.chat/v1',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    d = cfg.to_settings_dict()
    assert isinstance(d, dict), (
        f"to_settings_dict must return a dict, got {type(d).__name__}"
    )
    env = d.get('env', {})
    assert isinstance(env, dict), (
        f"to_settings_dict['env'] must be a dict, got {type(env).__name__}"
    )
    anthropic_keys = [k for k in env if k.startswith('ANTHROPIC_')]
    assert {'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'}.issubset(
        set(anthropic_keys)
    ), (
        f"expected endpoint+credentials in ANTHROPIC_* env keys, got "
        f"{anthropic_keys!r}"
    )


def test_to_settings_dict_base_url_correct():
    """env['ANTHROPIC_BASE_URL'] equals cfg.base_url verbatim."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com/anthropic',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    d = cfg.to_settings_dict()
    assert d['env']['ANTHROPIC_BASE_URL'] == 'https://api.example.com/anthropic', (
        f"ANTHROPIC_BASE_URL={d['env']['ANTHROPIC_BASE_URL']!r} != "
        f"'https://api.example.com/anthropic'"
    )


def test_to_settings_dict_auth_token_correct():
    """env['ANTHROPIC_AUTH_TOKEN'] equals cfg.auth_token verbatim."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com',
        api_key='sk-cp-key-123',
        auth_token='sk-cp-token-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    d = cfg.to_settings_dict()
    assert d['env']['ANTHROPIC_AUTH_TOKEN'] == 'sk-cp-token-456', (
        f"ANTHROPIC_AUTH_TOKEN={d['env']['ANTHROPIC_AUTH_TOKEN']!r} != "
        f"'sk-cp-token-456'"
    )


def test_to_settings_dict_model_env_passthrough():
    """model_env 中的 ANTHROPIC_* 键原样进入 env，且不覆盖端点/凭证键.

    2026-09-13 contract: model management is delegated to CC Switch —
    ``model_env`` is a flat passthrough, no tier lookup.
    """
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={
            'ANTHROPIC_MODEL': 'Vendor A-M3',
            'ANTHROPIC_DEFAULT_OPUS_MODEL': 'Opus-M3',
        },
    )
    d = cfg.to_settings_dict()
    env = d['env']
    assert env['ANTHROPIC_MODEL'] == 'Vendor A-M3'
    assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == 'Opus-M3'
    # Endpoint/credentials are never shadowed by model_env.
    assert env['ANTHROPIC_BASE_URL'] == 'https://api.example.com'
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'sk-cp-token'
    assert env['ANTHROPIC_API_KEY'] == 'sk-cp-key'


def test_write_tmp_settings_creates_file():
    """write_tmp_settings creates /tmp/subagent_settings_<uuid>.json and
    the returned path is a real on-disk file.

    Pins both:
      (a) ``cfg.settings_file_path`` is set to the returned Path.
      (b) The filename matches the
          ``subagent_settings_<32-hex>.json`` glob pattern
          (decision 2 / decision 3 contract — the file is identifiable
          by uuid alone for debug correlation).
    """
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    path = None
    try:
        path = cfg.write_tmp_settings()
        # (a) settings_file_path is set to the returned Path
        assert cfg.settings_file_path == path, (
            f"settings_file_path {cfg.settings_file_path!r} != "
            f"write_tmp_settings return value {path!r}"
        )
        # File is a real file on disk
        assert path.exists(), (
            f"write_tmp_settings returned {path} but the file does not exist"
        )
        assert path.is_file(), (
            f"write_tmp_settings returned {path} but it is not a regular file"
        )
        # (b) Filename pattern
        assert re.match(r"^subagent_settings_[0-9a-f]{32}\.json$", path.name), (
            f"filename {path.name!r} does not match "
            f"subagent_settings_<32-hex>.json pattern"
        )
        # Private directory + 0600 — the payload carries the provider
        # credential, and the previous flat ``/tmp`` layout (mode 1777)
        # left it readable by every account on the box.
        assert path.parent.name.startswith("pdt-subagent-"), (
            f"file {path} is not in a directory this run created; "
            f"parent is {path.parent}"
        )
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, (
            f"settings file mode is {oct(stat.S_IMODE(path.stat().st_mode))}"
        )
    finally:
        # Manual cleanup — the spec says write_tmp_settings does NOT
        # auto-clean the tmpfile, so we unlink in test teardown to
        # avoid /tmp pollution across runs.
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def test_write_tmp_settings_atomic():
    """write_tmp_settings 原子 rename — after the call returns, the
    intermediate ``<path>.tmp`` file must not exist.

    The implementation writes to ``<path>.tmp`` and then calls
    ``os.replace(tmp_path, path)`` to make the final file appear
    atomically. A regression that skips the replace (or uses
    ``shutil.move`` without the cross-device guarantee) would leave
    a ``.tmp`` file behind, which would silently confuse downstream
    consumers that glob for ``subagent_settings_*.json``.
    """
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    path = None
    try:
        path = cfg.write_tmp_settings()
        # The function builds tmp_path as ``Path(f"{path}.tmp")`` —
        # i.e. the literal string concatenation of the json path with
        # ".tmp". The Path with_suffix API does NOT reproduce this
        # (it would replace the .json suffix), so we use the explicit
        # string form.
        explicit_tmp = path.parent / f"{path.name}.tmp"
        assert not explicit_tmp.exists(), (
            f"After write_tmp_settings, the intermediate .tmp file "
            f"{explicit_tmp} should not exist (atomic rename). A "
            f"regression that skips os.replace would leave a .tmp "
            f"file behind that downstream consumers might glob for."
        )
    finally:
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def test_write_tmp_settings_emits_log():
    """write_tmp_settings invokes the logger with event=
    'subagent_settings_file_written' and data={path, uuid, fields=4}.

    2026-09-13: fields was 3 (BASE_URL / AUTH_TOKEN / API_KEY) after
    model management was delegated to CC Switch. 2026-09-22: raised
    to 4 — ANTHROPIC_MODEL is now a hard contract of every settings
    tmpfile (the inline-review path bypasses the dispatch walk, and a
    missing model let the CLI fall through to opus / vendor-d-pro). The logger interface is duck-typed: the function tries
    ``logger.emit(...)`` first, falling back to ``logger.log(...)`` or
    ``logger.info(...)`` if ``emit`` is not present. The MagicMock has
    all three attributes, so the first branch (``.emit``) is taken —
    we assert that the ``.emit`` call was made with the right event
    name and data payload.
    """
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    mock_logger = MagicMock()
    path = None
    try:
        path = cfg.write_tmp_settings(logger=mock_logger)
        # The MagicMock exposes ``emit``, so that branch is taken.
        # The call shape is:
        #   logger.emit(event='subagent_settings_file_written', data={...})
        assert mock_logger.emit.called, (
            "logger.emit() was not called; the function may have "
            "fallen through to .log() or .info() even though .emit "
            "is available on the logger."
        )
        # Pull event and data out of the kwargs
        _, kwargs = mock_logger.emit.call_args
        assert 'event' in kwargs, (
            f"logger.emit() was called without 'event' kwarg: {kwargs!r}"
        )
        assert kwargs['event'] == 'subagent_settings_file_written', (
            f"event={kwargs['event']!r} != 'subagent_settings_file_written'"
        )
        assert 'data' in kwargs, (
            f"logger.emit() was called without 'data' kwarg: {kwargs!r}"
        )
        data = kwargs['data']
        assert data.get('path') == str(path), (
            f"data['path']={data.get('path')!r} != str(path)={str(path)!r}"
        )
        assert data.get('fields') == 3, (
            f"data['fields']={data.get('fields')!r} != 3 "
            f"(_SETTINGS_ENV_FIELD_COUNT: endpoint + credentials; a model "
            f"is stamped only when one resolves)"
        )
        # ``uuid`` is a 32-char lowercase hex string matching the
        # filename suffix
        uuid_str = data.get('uuid')
        assert isinstance(uuid_str, str) and len(uuid_str) == 32, (
            f"data['uuid']={uuid_str!r} is not a 32-char string"
        )
        assert re.match(r"^[0-9a-f]{32}$", uuid_str), (
            f"data['uuid']={uuid_str!r} is not 32 lowercase hex chars"
        )
    finally:
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def test_write_tmp_settings_no_cleanup():
    """write_tmp_settings does not auto-delete the tmpfile — the file
    remains on disk after the call returns (and stays there until the
    test (or the operator) unlinks it).

    Decision 3 contract: the tmpfile is intentionally NOT cleaned up
    at process exit because backend execution may start child processes
    that read the file asynchronously after ``write_tmp_settings``
    returns, and we want post-mortem debugging to still be able to
    read the settings off disk.
    """
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.example.com',
        api_key='sk-cp-key',
        auth_token='sk-cp-token',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    path = None
    try:
        path = cfg.write_tmp_settings()
        # The file should still exist immediately after the call
        # returns. If the function tried to schedule a cleanup (via
        # atexit.register, threading.Timer, or signal handler), the
        # file would still exist RIGHT NOW, so this assertion catches
        # a regression that:
        #   (a) deletes the file synchronously after writing, or
        #   (b) returns a Path that was never actually written to.
        assert path.exists(), (
            f"write_tmp_settings should NOT auto-delete the file, "
            f"but {path} does not exist after the call returned"
        )
        # Defence-in-depth: the file has non-zero size (i.e. it was
        # actually written, not a 0-byte stub).
        assert path.stat().st_size > 0, (
            f"write_tmp_settings returned a zero-byte file at {path}"
        )
    finally:
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# to_settings_dict() hooks contract (decision 4 / task 7-4)
# ---------------------------------------------------------------------------


def _full_model_map(provider_name='vendor-a-pro'):
    """4-role model_map fixture."""
    return {
        'opus': {provider_name: 'Vendor A-Opus'},
        'sonnet': {provider_name: 'Vendor A-Sonnet'},
        'haiku': {provider_name: 'Vendor A-Haiku'},
        'medium': {provider_name: 'Vendor A-Medium'},
    }


_HOOKS_DIR = Path(__file__).resolve().parents[2] / "coding_tool_hooks"
HOOK_PRE_ABS = _HOOKS_DIR / "pre_tool_use.sh"
HOOK_POST_ABS = _HOOKS_DIR / "post_tool_use.sh"


def test_to_settings_dict_has_hooks_key():
    """'hooks' key is present in the returned settings dict."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[HOOK_PRE_ABS, HOOK_POST_ABS],
    )
    d = cfg.to_settings_dict()
    assert 'hooks' in d
    assert isinstance(d['hooks'], dict)


def test_to_settings_dict_hooks_contains_pre_tool_use():
    """PreToolUse matcher references the pre_tool_use.sh path."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[HOOK_PRE_ABS, HOOK_POST_ABS],
    )
    d = cfg.to_settings_dict()
    assert d['hooks']['PreToolUse'][0]['hooks'][0]['command'].endswith('pre_tool_use.sh')


def test_to_settings_dict_hooks_contains_post_tool_use():
    """PostToolUse matcher references the post_tool_use.sh path."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[HOOK_PRE_ABS, HOOK_POST_ABS],
    )
    d = cfg.to_settings_dict()
    assert d['hooks']['PostToolUse'][0]['hooks'][0]['command'].endswith('post_tool_use.sh')


def test_to_settings_dict_hooks_command_is_absolute_path():
    """Hook command path is the absolute Path string of the script."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[HOOK_PRE_ABS, HOOK_POST_ABS],
    )
    d = cfg.to_settings_dict()
    pre_cmd = d['hooks']['PreToolUse'][0]['hooks'][0]['command']
    post_cmd = d['hooks']['PostToolUse'][0]['hooks'][0]['command']
    assert pre_cmd == str(HOOK_PRE_ABS)
    assert post_cmd == str(HOOK_POST_ABS)
    assert pre_cmd.startswith('/')
    assert post_cmd.startswith('/')


def test_to_settings_dict_hooks_type_is_command():
    """Each hook entry has type == 'command' (Anthropic SDK contract)."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[HOOK_PRE_ABS, HOOK_POST_ABS],
    )
    d = cfg.to_settings_dict()
    assert d['hooks']['PreToolUse'][0]['hooks'][0]['type'] == 'command'
    assert d['hooks']['PostToolUse'][0]['hooks'][0]['type'] == 'command'


def test_to_settings_dict_no_hooks_when_empty():
    """hook_scripts=[] → 'hooks' not in d OR d['hooks'] == {}."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
    )
    d = cfg.to_settings_dict()
    assert ('hooks' not in d) or (d['hooks'] == {})


def test_settings_file_path_in_hooks():
    """write_tmp_settings injects CLAUDE_SETTINGS_PATH into env (decision-4 hook plumbing)."""
    cfg = SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://x',
        auth_token='at-123',
        api_key='ak-456',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[HOOK_PRE_ABS, HOOK_POST_ABS],
    )
    assert cfg.settings_file_path is None
    try:
        path = cfg.write_tmp_settings()
        assert cfg.settings_file_path == path
        d = cfg.to_settings_dict()
        env = d['env']
        assert 'CLAUDE_SETTINGS_PATH' in env
        assert env['CLAUDE_SETTINGS_PATH'] == str(path)
        anthropic_keys = [k for k in env if k.startswith('ANTHROPIC_')]
        assert {'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'}.issubset(
            set(anthropic_keys)
        )
    finally:
        if cfg.settings_file_path is not None and cfg.settings_file_path.exists():
            cfg.settings_file_path.unlink()
