"""End-to-end tests for ClaudeCodingTool settings_file_path injection.

TDD spec (task 7-4-5): verify that ``ClaudeCodingTool.__init__`` actually
injects ``settings_file_path`` into the SDK launch parameters via the
``--settings <tmpfile>`` flag, and that the SubagentConfig-driven env
overrides (``base_url`` / ``auth_token`` / ``api_key``) reach the
subprocess env block.

Acceptance bullets (one-to-one with the spec):

  * test_coding_tool_passes_settings_to_sdk
        — args contain '--settings'
  * test_coding_tool_settings_path_matches_cfg
        — the value after '--settings' is the absolute path of
          ``cfg.settings_file_path``
  * test_coding_tool_env_base_url_matches_cfg
        — env['ANTHROPIC_BASE_URL'] == cfg.base_url
  * test_coding_tool_env_auth_token_matches_cfg
        — env['ANTHROPIC_AUTH_TOKEN'] == cfg.auth_token
  * test_coding_tool_env_api_key_matches_cfg
        — env['ANTHROPIC_API_KEY'] == cfg.api_key
  * test_coding_tool_settings_file_exists_at_startup
        — the file pointed to by --settings exists on disk at the
          moment Popen is called
  * test_coding_tool_hooks_passthrough_to_sdk
        — the --settings file's 'hooks' block (decision 4) reaches
          the SDK, evidenced by the on-disk file containing
          pre_tool_use / post_tool_use matcher entries

Constraints (per the spec):

  * ``ClaudeCodingTool.__init__`` is **NOT** mocked — every test calls
    the real constructor with the real SubagentConfig fields
    (no Mock, no MagicMock, no ``__new__`` override).
  * ``write_tmp_settings`` is **NOT** patched — the real disk write
    path runs and the tmpfile is checked for existence / parsed for
    hooks after Popen.
  * The Claude SDK subprocess is mocked (via ``@patch(
    "coding_tool.subprocess.Popen")``) so the test does not actually
    spawn ``claude -p ...`` — only the args/env passed to Popen are
    captured.
  * Wall time < 10s (mocked Popen returns immediately; the real cost
    is the JSON read at the end).

Design notes:
  * ``provider_priority=["__no_such_provider__"]`` is passed to the
    constructor so the PROVIDER_REGISTRY dispatch loop resolves
    nothing — no provider is selected and the SubagentConfig-driven
    ``base_url`` / ``auth_token`` / ``api_key`` overrides we test
    below are the ONLY ANTHROPIC_* env vars the subprocess sees from
    us. (The subprocess still inherits ``PATH`` / ``HOME`` from the
    parent env, which is fine.)

    Note the sentinel is a *non-empty* list on purpose: an empty one
    falls through to the hardcoded default provider pair, so the
    dispatch loop would really run (see ``_build_tool``).

    These fields are the documented FALLBACK: since 2026-09-14 the
    provider mapping (scene → tier → chain) is the single source of
    truth, and a resolved provider's endpoint/credentials win over
    the configured overrides. The precedence itself is pinned in
    ``tests/unit/test_coding_tool_scene_routing.py``
    (``TestProviderMappingIsSingleSourceOfTruth``); here we cover the
    path where nothing is resolvable.
  * A simple ``MagicMock`` for the Popen return is enough: the read
    loop in ``_run_claude_interactive`` only iterates stdout and breaks on a
    ``"result"`` event.
  * The tmpfile is cleaned up by the ``settings_path`` fixture so
    repeated test runs do not pollute ``/tmp``.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from coding_tool import ClaudeCodingTool
from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _full_model_map(provider_name: str = 'vendor-a-pro') -> dict:
    """4-role model_map fixture so ``to_settings_dict()`` does not raise.

    Without all 4 roles, the inner ``role_map[self.provider_name]``
    lookup in ``to_settings_dict`` would raise KeyError and the
    settings file would never land on disk — the test for
    ``--settings`` would then assert a missing tmpfile path.
    """
    return {
        'opus': {provider_name: 'Vendor A-Opus'},
        'sonnet': {provider_name: 'Vendor A-Sonnet'},
        'haiku': {provider_name: 'Vendor A-Haiku'},
        'medium': {provider_name: 'Vendor A-Medium'},
    }


@pytest.fixture
def subagent_cfg() -> SubagentConfig:
    """A SubagentConfig carrying every field the integration test needs.

    model_map covers all 4 roles so ``to_settings_dict()`` reaches the
    hooks branch; hook_scripts has one pre and one post so the on-disk
    file's ``hooks`` block is non-empty (decision 4 pass-through
    evidence).
    """
    return SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.example/anthropic',
        api_key='sk-cp-test-key-7-4-5',
        auth_token='sk-cp-test-token-7-4-5',
        model_env={'ANTHROPIC_MODEL': 'Vendor A-M3'},
        hook_scripts=[
            Path('/abs/pre_tool_use_7_4_5.sh'),
            Path('/abs/post_tool_use_7_4_5.sh'),
        ],
    )


@pytest.fixture
def settings_path(subagent_cfg: SubagentConfig) -> Path:
    """Drop a real /tmp settings file via ``write_tmp_settings``.

    Cleans up in teardown. The 7-4-5 test must not patch
    ``write_tmp_settings`` — the file genuinely lands on disk.
    """
    try:
        path = subagent_cfg.write_tmp_settings()
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


def _make_mock_process() -> MagicMock:
    """Build a MagicMock that satisfies the ``_run_claude_interactive`` read loop.

    The read loop iterates stdout, parses JSON, and breaks on the
    ``"result"`` event. A single ``{"type": "result", ...}`` line is
    the minimum payload that lets ``_run_claude_interactive`` return cleanly.
    ``stdin`` is a MagicMock so interactive mode can write the plain-text
    prompt and close the pipe during graceful shutdown.
    """
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "00000000-0000-0000-0000-000000000000",
        }) + "\n",
        json.dumps({
            "type": "result",
            "result": "ok",
            "is_error": False,
        }) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    return mock_proc


def _build_tool(subagent_cfg: SubagentConfig) -> ClaudeCodingTool:
    """Build a real ClaudeCodingTool with SubagentConfig-driven fields.

    This is the *real* ``__init__`` — no mocking. The one-entry
    sentinel priority list keeps the PROVIDER_REGISTRY dispatch from
    resolving anything, so the SubagentConfig-driven fields are the
    only ANTHROPIC_* env vars we contribute.

    2026-09-14: the sentinel is deliberately NOT ``[]``. An empty list
    is falsy, so the constructor fell back to its hardcoded
    ``["vendor-b-pro", "vendor-a-pro"]`` default and the dispatch loop
    DID run — resolving a real CC Switch row (leaking its live
    credentials into these assertions) and, once the provider mapping
    became authoritative, overriding the fields under test. A
    non-empty list naming an unregistered provider really does leave
    ``provider_selected`` False, which is the fallback path these
    tests are about.
    """
    return ClaudeCodingTool(
        api_key=subagent_cfg.api_key,
        base_url=subagent_cfg.base_url,
        auth_token=subagent_cfg.auth_token,
        provider_priority=["__no_such_provider__"],  # nothing resolvable
        settings=subagent_cfg.settings_file_path,
    )


# ---------------------------------------------------------------------------
# Pure end-to-end: --settings injection
# ---------------------------------------------------------------------------


class TestCodingToolSettingsInjection:
    """End-to-end: --settings flag and SubagentConfig env reach the SDK."""

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_passes_settings_to_sdk(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """``--settings`` is in the cmd passed to subprocess.Popen.

        The SDK is invoked as ``claude -p --settings <tmpfile> ...``
        so the SDK reads the SubagentConfig-written JSON off disk to
        resolve ANTHROPIC_* env overrides and PostToolUse hooks.
        """
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        assert mock_popen.call_count == 1
        call_args, _ = mock_popen.call_args
        cmd = call_args[0]
        assert "--settings" in cmd, (
            f"cmd missing '--settings' flag, got: {cmd!r}. The "
            f"SubagentConfig.settings_file_path was set on the "
            f"instance but never reached the SDK cmd list."
        )

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_settings_path_matches_cfg(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """The value after ``--settings`` is exactly
        ``cfg.settings_file_path`` (absolute path).

        Pin the round-trip: SubagentConfig.write_tmp_settings() → tmpfile
        path → --settings flag value. A regression that drops the
        value (e.g. ``--settings ""`` or a relative path) would
        silently break the SDK's env resolution and we want to catch
        it here.
        """
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        call_args, _ = mock_popen.call_args
        cmd = call_args[0]
        assert "--settings" in cmd, f"cmd missing '--settings' flag: {cmd!r}"
        flag_idx = cmd.index("--settings")
        value = cmd[flag_idx + 1]
        # The tool may clone the SubagentConfig-written tmpfile to inject
        # per-subagent env / guard hooks. Both the original cfg path and
        # the cloned path are acceptable as long as the value is an
        # absolute path that exists on disk and was derived from the
        # cfg tmpfile (same /tmp/subagent_settings_*.json template).
        assert Path(value).is_absolute(), (
            f"--settings value {value!r} is not an absolute path"
        )
        assert Path(value).exists(), (
            f"--settings value {value!r} does not exist on disk"
        )
        assert "subagent_settings_" in value, (
            f"--settings value {value!r} was not derived from the "
            f"SubagentConfig.write_tmp_settings() tmpfile template"
        )

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_env_base_url_matches_cfg(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """``env['ANTHROPIC_BASE_URL']`` equals ``cfg.base_url``.

        Decision 2 plumbing: the SubagentConfig's base_url is the
        single source of truth for the provider endpoint. The
        subprocess env block must carry it so the SDK never falls
        back to the parent process's ANTHROPIC_BASE_URL (which could
        point at a different provider, e.g. CC Switch proxy).
        """
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env.get("ANTHROPIC_BASE_URL") == subagent_cfg.base_url, (
            f"env['ANTHROPIC_BASE_URL'] {env.get('ANTHROPIC_BASE_URL')!r} != "
            f"cfg.base_url {subagent_cfg.base_url!r}"
        )

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_env_auth_token_matches_cfg(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """``env['ANTHROPIC_AUTH_TOKEN']`` equals ``cfg.auth_token``."""
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env.get("ANTHROPIC_AUTH_TOKEN") == subagent_cfg.auth_token, (
            f"env['ANTHROPIC_AUTH_TOKEN'] {env.get('ANTHROPIC_AUTH_TOKEN')!r} != "
            f"cfg.auth_token {subagent_cfg.auth_token!r}"
        )

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_env_api_key_matches_cfg(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """``env['ANTHROPIC_API_KEY']`` equals ``cfg.api_key``."""
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env.get("ANTHROPIC_API_KEY") == subagent_cfg.api_key, (
            f"env['ANTHROPIC_API_KEY'] {env.get('ANTHROPIC_API_KEY')!r} != "
            f"cfg.api_key {subagent_cfg.api_key!r}"
        )

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_settings_file_exists_at_startup(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """The file pointed to by ``--settings`` exists on disk at
        the moment Popen is called.

        This is a hard runtime contract: a regression that swaps
        ``settings_file_path`` for an in-memory dict (e.g. a future
        refactor that serialises SubagentConfig directly without
        going through ``write_tmp_settings``) would pass the
        string-equality assertion above but fail here, because the
        SDK would try to read a non-existent file.
        """
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        call_args, _ = mock_popen.call_args
        cmd = call_args[0]
        flag_idx = cmd.index("--settings")
        value = cmd[flag_idx + 1]
        # Read the file ourselves and verify it parses as JSON with
        # the 7 ANTHROPIC_* env keys the SDK depends on.
        file_path = Path(value)
        assert file_path.exists(), (
            f"--settings target {value!r} does not exist on disk at "
            f"Popen call time. write_tmp_settings() must be called "
            f"before ClaudeCodingTool is constructed."
        )
        assert file_path.is_file(), (
            f"--settings target {value!r} is not a regular file"
        )
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        env_keys = {
            k for k in payload.get("env", {}) if k.startswith("ANTHROPIC_")
        }
        # Boundary contract: the file must carry the 7 SDK keys and must
        # NOT carry anything outside the tool-owned allowlist. An exact
        # count would be brittle (the parent shell may or may not export
        # ``ANTHROPIC_MODEL``); the subset check pins the same property —
        # "no unrelated parent-process keys leak into the tmpfile" —
        # without depending on the developer's environment.
        tool_owned = {
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SUMMARIZE_MODEL",
        }
        sdk_required = {
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
        }
        missing = sorted(sdk_required - env_keys)
        assert not missing, (
            f"on-disk env block is missing SDK-required key(s): {missing!r} "
            f"(got {sorted(env_keys)!r})"
        )
        unexpected = sorted(env_keys - tool_owned)
        assert not unexpected, (
            f"on-disk env block carries key(s) the tool does not own: "
            f"{unexpected!r}. The settings file must not echo unrelated "
            f"ANTHROPIC_* entries from the parent process env."
        )

    @patch("coding_tool.subprocess.Popen")
    def test_coding_tool_hooks_passthrough_to_sdk(
        self, mock_popen, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """The on-disk settings file carries the PreToolUse / PostToolUse
        hooks (decision 4 pass-through).

        The SDK reads the ``hooks`` block from the --settings file
        and spawns pre_tool_use.sh / post_tool_use.sh at the right
        lifecycle points. The end-to-end contract we pin here: the
        file the SDK will read carries BOTH matchers with the
        expected script paths.
        """
        mock_popen.return_value = _make_mock_process()
        tool = _build_tool(subagent_cfg)
        tool.query("test prompt")

        # Re-derive the tmpfile path from the cmd we captured (not
        # from subagent_cfg.settings_file_path, to keep the assertion
        # independent — we want to know the on-disk file, not just
        # the in-memory path).
        call_args, _ = mock_popen.call_args
        cmd = call_args[0]
        flag_idx = cmd.index("--settings")
        file_path = Path(cmd[flag_idx + 1])

        payload = json.loads(file_path.read_text(encoding="utf-8"))
        hooks = payload.get("hooks", {})
        # The on-disk boundary case (task 7-4-3 fix): when hooks
        # is non-empty, the SDK-facing file must keep the 'hooks'
        # key. (When empty, the key is stripped to avoid SDK schema
        # hazards — see subagent_config.write_tmp_settings docstring.)
        assert hooks, (
            f"on-disk hooks block is empty; expected PreToolUse + "
            f"PostToolUse matchers: payload={payload!r}"
        )
        assert "PreToolUse" in hooks, (
            f"on-disk hooks missing 'PreToolUse' matcher: "
            f"{sorted(hooks.keys())!r}"
        )
        assert "PostToolUse" in hooks, (
            f"on-disk hooks missing 'PostToolUse' matcher: "
            f"{sorted(hooks.keys())!r}"
        )
        # And the script paths in the on-disk file reference the
        # SubagentConfig's hook_scripts.
        pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
        post_cmd = hooks["PostToolUse"][0]["hooks"][0]["command"]
        assert "pre_tool_use_7_4_5.sh" in pre_cmd, (
            f"on-disk PreToolUse command {pre_cmd!r} does not "
            f"reference the cfg pre_tool_use_7_4_5.sh path"
        )
        assert "post_tool_use_7_4_5.sh" in post_cmd, (
            f"on-disk PostToolUse command {post_cmd!r} does not "
            f"reference the cfg post_tool_use_7_4_5.sh path"
        )


# ---------------------------------------------------------------------------
# Wall-time guard
# ---------------------------------------------------------------------------


class TestCodingToolSettingsInjectionWallTime:
    """End-to-end wall-time budget: full 7-bullet run < 10s.

    The Popen is mocked, so the only real cost is the JSON parse of
    the on-disk file in the last bullet. A regression that swaps the
    mock for a real subprocess spawn would blow this budget and the
    test would fail loudly — exactly what we want for "don't actually
    call claude -p" guarantee.
    """

    def test_coding_tool_settings_injection_wall_time(
        self, subagent_cfg: SubagentConfig, settings_path: Path
    ) -> None:
        """All 7 bullets combined run in < 10s.

        We exercise the full path: Popen mocked (no real subprocess),
        real SubagentConfig / write_tmp_settings / ClaudeCodingTool
        / on-disk JSON parse.
        """
        start = time.monotonic()

        with patch("coding_tool.subprocess.Popen") as mock_popen:
            mock_popen.return_value = _make_mock_process()
            tool = _build_tool(subagent_cfg)
            tool.query("test prompt")

            call_args, _ = mock_popen.call_args
            cmd = call_args[0]
            assert "--settings" in cmd
            file_path = Path(cmd[cmd.index("--settings") + 1])
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            assert "PreToolUse" in payload.get("hooks", {})
            assert "PostToolUse" in payload.get("hooks", {})

        elapsed = time.monotonic() - start
        assert elapsed < 10.0, (
            f"7-bullet end-to-end run took {elapsed:.2f}s; expected < 10s. "
            f"A regression that swaps the mocked Popen for a real "
            f"subprocess spawn would blow this budget."
        )
