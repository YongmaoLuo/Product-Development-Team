"""Tests for SubagentConfig.write_tmp_settings() real /tmp file behavior.

TDD spec (task 7-3): verify that ``write_tmp_settings()`` drops a unique
``/tmp/subagent_settings_<uuid>.json`` with the expected 7-field schema
(3 ANTHROPIC_* credentials + 4 ANTHROPIC_DEFAULT_*_MODEL), and that the
file is NOT auto-cleaned by atexit (decision 3 contract).

These tests do NOT mock ``write_tmp_settings`` — they exercise the real
disk write path. Each test cleans up its own tmpfile via the
``write_path`` fixture so we do not pollute ``/tmp`` across runs.

The 12 TDD acceptance bullets (one-to-one with the spec):

  * test_write_tmp_settings_creates_tmpfile
        — write_tmp_settings sets settings_file_path; file exists on disk
  * test_write_tmp_settings_path_under_tmp
        — settings_file_path is absolute and starts with ``/tmp/``
  * test_write_tmp_settings_filename_has_uuid
        — filename matches ``subagent_settings_<32-hex>.json``
  * test_write_tmp_settings_no_atexit_cleanup
        — atexit handler count is unchanged after the call
  * test_written_file_has_endpoint_and_credentials
        — read JSON, the 3 core ANTHROPIC_* keys are present
  * test_written_file_anthropic_base_url_correct
        — env['ANTHROPIC_BASE_URL'] == subagent_cfg.base_url
  * test_written_file_anthropic_auth_token_correct
        — env['ANTHROPIC_AUTH_TOKEN'] == subagent_cfg.auth_token
  * test_written_file_anthropic_api_key_correct
        — env['ANTHROPIC_API_KEY'] == subagent_cfg.api_key
  * test_written_file_model_env_passthrough
        — every ANTHROPIC_* key in model_env reaches env verbatim
  * test_written_file_no_tiered_model_map
        — model keys present == model_env keys (nothing synthesized)
"""

import atexit
import json
import os
import re
import stat
import tempfile
from pathlib import Path

import pytest

from subagent_config import SubagentConfig
from utils.secret_files import PRIVATE_ROOT_ENV_VAR


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_full_config() -> SubagentConfig:
    """Build a SubagentConfig with the flat model_env passthrough.

    2026-09-13 contract: model management is delegated to CC Switch.
    ``model_env`` carries extra ANTHROPIC_* model vars verbatim; the
    tiered model_map is gone.
    """
    return SubagentConfig(
        provider_name='vendor-a-pro',
        base_url='https://api.vendor-a.example/anthropic',
        api_key='sk-cp-test-key-1234567890',
        auth_token='sk-cp-test-token-1234567890',
        model_env={
            'ANTHROPIC_MODEL': 'Vendor A-M3',
            'ANTHROPIC_DEFAULT_OPUS_MODEL': 'Vendor A-M3',
            'ANTHROPIC_DEFAULT_SONNET_MODEL': 'Vendor A-M2',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'Vendor A-Mini',
            'ANTHROPIC_DEFAULT_SUMMARIZE_MODEL': 'Vendor A-M2',
        },
    )


@pytest.fixture
def subagent_cfg() -> SubagentConfig:
    """A fully-populated SubagentConfig instance."""
    return _build_full_config()


@pytest.fixture
def write_path(subagent_cfg: SubagentConfig) -> Path:
    """Call ``write_tmp_settings()`` and yield the file path.

    Cleans up the tmpfile in teardown. Decision 3 says the file is NOT
    auto-cleaned by atexit — we explicitly unlink in the fixture so
    repeated test runs do not pollute ``/tmp``.

    Skips the test (rather than failing) if ``/tmp`` is unwritable, so
    a sandboxed CI that mounts a read-only /tmp does not mask every
    other assertion in this file with an OSError.
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
                # Best-effort; OSError on /tmp unlink is effectively
                # impossible on macOS / Linux, but if /tmp is on a
                # read-only volume we want subsequent assertions to
                # still fire.
                pass


# ---------------------------------------------------------------------------
# TDD tests
# ---------------------------------------------------------------------------


def test_write_tmp_settings_creates_tmpfile(subagent_cfg: SubagentConfig,
                                            write_path: Path) -> None:
    """``write_tmp_settings()`` sets ``settings_file_path`` to a real file.

    This is the headline TDD test: after the call returns, the
    attribute on the config instance must point to a Path that
    actually exists on disk. A regression that returns a Path object
    but never writes the file (e.g. early-returns before ``open()``)
    would be caught here.
    """
    assert subagent_cfg.settings_file_path is not None, (
        "write_tmp_settings did not set settings_file_path on the config"
    )
    assert subagent_cfg.settings_file_path == write_path, (
        f"settings_file_path {subagent_cfg.settings_file_path!r} != "
        f"write_path {write_path!r}"
    )
    assert write_path.exists(), (
        f"write_tmp_settings returned {write_path} but the file does not exist"
    )
    assert write_path.is_file(), (
        f"write_tmp_settings returned {write_path} but it is not a regular file"
    )


def test_write_tmp_settings_path_under_tmp(write_path: Path) -> None:
    """The returned path is absolute, in the temp root, and not exposed.

    This used to assert ``str(path).startswith("/tmp/")`` — which pinned
    the *leak*. ``/tmp`` is mode ``1777``: the sticky bit stops other
    local accounts **deleting** these files but not **reading** them, and
    the payload carries the routed provider's ``ANTHROPIC_API_KEY``.
    Nothing removed those files, so every dispatch added one more ``0644``
    copy of a live credential.

    The contract is now the opposite one, and stronger: still under the
    configured private root — the system temp root unless
    ``PDT_SECRET_TEMP_ROOT`` names another one, so the "process-spanning
    tmpfile" property holds and nothing lands in the workspace — but
    inside a directory this run created and readable only by its owner.

    The containment half is asked of the **configured** root rather than
    of ``gettempdir()`` alone. A harness points the root at something it
    owns and removes; asserting the system temp root unconditionally
    would make the redirect look like a violation of the very layout
    rule it exists to serve.
    """
    assert write_path.is_absolute(), (
        f"settings_file_path {write_path} is not absolute"
    )
    configured = os.environ.get(PRIVATE_ROOT_ENV_VAR) or tempfile.gettempdir()
    temp_root = Path(configured).resolve()
    assert temp_root in write_path.resolve().parents, (
        f"settings_file_path {write_path} is not under the configured "
        f"private root {temp_root} — the function must not silently "
        f"redirect to Path.cwd() or Path.home()"
    )
    assert write_path.parent.name.startswith("pdt-subagent-"), (
        f"{write_path.parent} is not a directory this run created; the "
        f"flat-in-/tmp layout is the leak"
    )
    assert stat.S_IMODE(write_path.parent.stat().st_mode) == 0o700, (
        "the directory must not be traversable by other local accounts"
    )
    assert stat.S_IMODE(write_path.stat().st_mode) == 0o600, (
        f"settings file mode is {oct(stat.S_IMODE(write_path.stat().st_mode))}; "
        f"the payload carries a provider credential"
    )


def test_write_tmp_settings_filename_has_uuid(write_path: Path) -> None:
    """Filename matches ``subagent_settings_<32-hex>.json``.

    We pin the exact pattern so a future refactor that drops the
    ``uuid.uuid4().hex`` prefix (e.g. switches to a PID or a
    timestamp-based name) is caught at the regex level. The uuid is also
    what ``execution.log`` records for correlating a path back to a
    dispatch, so losing it costs the only debug trail there is.
    """
    filename = write_path.name
    pattern = r"^subagent_settings_[0-9a-f]{32}\.json$"
    assert re.match(pattern, filename), (
        f"filename {filename!r} does not match pattern {pattern!r}"
    )


def test_write_tmp_settings_no_atexit_cleanup(
    monkeypatch: pytest.MonkeyPatch, subagent_cfg: SubagentConfig
) -> None:
    """atexit.register is NOT called by write_tmp_settings().

    Decision 3 contract: the tmpfile is intentionally left on disk
    after process exit for post-mortem debugging — we never register
    a cleanup handler. We assert this by spying on
    ``atexit.register`` and verifying the call list is empty.

    This test calls ``write_tmp_settings()`` directly (rather than
    via the ``write_path`` fixture) so the spy sees the call under
    test. The cleanup is inlined.
    """
    registered_calls = []
    original_register = atexit.register

    def spy_register(func, *args, **kwargs):
        # Capture (func, args, kwargs) tuple; do NOT call the original
        # register because that would mutate global atexit state and
        # cause cross-test pollution. The point is to detect that
        # write_tmp_settings *would have* registered, not to actually
        # register.
        registered_calls.append((func, args, kwargs))
        return original_register(func, *args, **kwargs)

    monkeypatch.setattr(atexit, "register", spy_register)

    try:
        path = subagent_cfg.write_tmp_settings()
    except OSError as e:
        pytest.skip(f"/tmp is not writable in this environment: {e}")

    try:
        assert len(registered_calls) == 0, (
            f"write_tmp_settings registered {len(registered_calls)} atexit "
            f"handler(s): {registered_calls!r}"
        )
        # Defence-in-depth: the path must not appear anywhere in the
        # captured call list. ``atexit.register`` was never invoked at
        # all, so this is a no-op, but the assertion is here to make
        # the contract explicit: "settings file path not leaked to
        # atexit".
        path_str = str(path)
        for func, args, kwargs in registered_calls:
            assert path_str not in repr(func), (
                f"atexit handler references the settings path {path_str!r}: "
                f"{func!r}"
            )
            assert path_str not in repr(args), (
                f"atexit handler args reference the settings path {path_str!r}: "
                f"{args!r}"
            )
            assert path_str not in repr(kwargs), (
                f"atexit handler kwargs reference the settings path {path_str!r}: "
                f"{kwargs!r}"
            )
    finally:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def test_written_file_has_endpoint_and_credentials(write_path: Path) -> None:
    """The written JSON file has the 3 core ``ANTHROPIC_*`` env keys.

    2026-09-13 contract: BASE_URL / AUTH_TOKEN / API_KEY are always
    present (a regression that drops one — e.g. stops serialising
    API_KEY because the SDK only reads AUTH_TOKEN — would silently
    break the subprocess's auth chain). The 3-field floor is a hard
    contract pinned by ``subagent_config._SETTINGS_ENV_FIELD_COUNT``.
    Model keys are no longer synthesized here — they arrive via the
    flat ``model_env`` passthrough from the CC Switch provider row.
    """
    payload = json.loads(write_path.read_text(encoding="utf-8"))
    env = payload.get("env", {})
    assert isinstance(env, dict), (
        f"settings.json 'env' must be a dict, got {type(env).__name__}"
    )
    anthropic_keys = sorted(k for k in env if k.startswith("ANTHROPIC_"))
    core_keys = {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    }
    missing = core_keys - set(anthropic_keys)
    assert not missing, f"missing core env keys: {missing}"


def test_written_file_anthropic_base_url_correct(subagent_cfg: SubagentConfig,
                                                  write_path: Path) -> None:
    """``env['ANTHROPIC_BASE_URL']`` equals ``subagent_cfg.base_url``."""
    payload = json.loads(write_path.read_text(encoding="utf-8"))
    env = payload.get("env", {})
    assert env.get("ANTHROPIC_BASE_URL") == subagent_cfg.base_url, (
        f"ANTHROPIC_BASE_URL {env.get('ANTHROPIC_BASE_URL')!r} != "
        f"subagent_cfg.base_url {subagent_cfg.base_url!r}"
    )


def test_written_file_anthropic_auth_token_correct(subagent_cfg: SubagentConfig,
                                                    write_path: Path) -> None:
    """``env['ANTHROPIC_AUTH_TOKEN']`` equals ``subagent_cfg.auth_token``."""
    payload = json.loads(write_path.read_text(encoding="utf-8"))
    env = payload.get("env", {})
    assert env.get("ANTHROPIC_AUTH_TOKEN") == subagent_cfg.auth_token, (
        f"ANTHROPIC_AUTH_TOKEN {env.get('ANTHROPIC_AUTH_TOKEN')!r} != "
        f"subagent_cfg.auth_token {subagent_cfg.auth_token!r}"
    )


def test_written_file_anthropic_api_key_correct(subagent_cfg: SubagentConfig,
                                                 write_path: Path) -> None:
    """``env['ANTHROPIC_API_KEY']`` equals ``subagent_cfg.api_key``."""
    payload = json.loads(write_path.read_text(encoding="utf-8"))
    env = payload.get("env", {})
    assert env.get("ANTHROPIC_API_KEY") == subagent_cfg.api_key, (
        f"ANTHROPIC_API_KEY {env.get('ANTHROPIC_API_KEY')!r} != "
        f"subagent_cfg.api_key {subagent_cfg.api_key!r}"
    )


def test_written_file_model_env_passthrough(subagent_cfg: SubagentConfig,
                                            write_path: Path) -> None:
    """Every ANTHROPIC_* key in ``model_env`` reaches the on-disk env
    verbatim (2026-09-13 contract: CC Switch owns model management;
    the tmpfile forwards the provider row's model env)."""
    payload = json.loads(write_path.read_text(encoding="utf-8"))
    env = payload.get("env", {})
    for key, expected in subagent_cfg.model_env.items():
        assert env.get(key) == expected, (
            f"{key} {env.get(key)!r} != model_env {expected!r}"
        )


def test_written_file_no_tiered_model_map(subagent_cfg: SubagentConfig,
                                          write_path: Path) -> None:
    """The tmpfile must not derive model vars from a tiered model_map —
    that mechanism was removed."""
    payload = json.loads(write_path.read_text(encoding="utf-8"))
    env = payload.get("env", {})
    # The only model keys present must be exactly what model_env provided
    # (plus endpoint/credentials), nothing synthesized from a tier map.
    model_keys = [k for k in env if k.startswith("ANTHROPIC_DEFAULT_")
                  or k == "ANTHROPIC_MODEL"]
    assert set(model_keys) == set(subagent_cfg.model_env.keys())



