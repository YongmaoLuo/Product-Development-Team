"""Legacy walk resolves CC Switch's *actual* current provider (2026-09-21).

The legacy walk must read CC Switch's *actually routed* provider
rather than guessing one; that information is available from CC Switch.

Background: the walk used to *guess*. It iterated the priority list, and
a name that did not resolve in the DB silently fell back to the shell env,
producing a hybrid — provider name said one thing, the endpoint was CC
Switch's proxy, and the model was a hard-coded registry default. Measured
on 2026-09-21: the registry recorded ``provider=vendor-a-pro`` /
``model=m2.7`` while CC Switch billed the request as vendor-d-lite.

These tests cover both halves:

  * ``_load_cc_switch_current_provider`` — the resolver, exercised
    hermetically against a temp ``HOME`` (never the operator's real
    ``~/.cc-switch``).
  * the dispatch walk — the shortcut that consumes it, with the resolver
    patched at the ``ClaudeCodingTool`` seam (the autouse
    ``hermetic_cc_switch_current_provider`` conftest fixture disables it
    for every other test in the suite).
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as coding_tool_module
from cc_switch import ProviderConfig
from coding_tool import ClaudeCodingTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_process() -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({
            "type": "system", "subtype": "init",
            "session_id": "00000000-0000-0000-0000-000000000000",
        }) + "\n",
        json.dumps({"type": "result", "result": "ok", "is_error": False}) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    return mock_proc


def _capture_popen_env_and_args():
    calls = []

    def _popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env", {})})
        return _make_mock_process()

    return patch.object(coding_tool_module.subprocess, "Popen", side_effect=_popen), calls


def _seed_cc_switch(home: Path, *, current_id="prov-current", is_current=None,
                    env=None, name="Vendor D API") -> None:
    """Write a minimal ~/.cc-switch (settings.json + sqlite DB) under ``home``."""
    cc_dir = home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    if current_id is not None:
        (cc_dir / "settings.json").write_text(
            json.dumps({"currentProviderClaude": current_id}), encoding="utf-8"
        )
    conn = sqlite3.connect(str(cc_dir / "cc-switch.db"))
    try:
        conn.execute(
            "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT,"
            " settings_config TEXT, is_current INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO providers (id, app_type, name, settings_config, is_current)"
            " VALUES (?,?,?,?,?)",
            (
                current_id or "prov-fallback",
                "claude",
                name,
                json.dumps({"env": env or {}}),
                1 if is_current else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


_FULL_ENV = {
    "ANTHROPIC_BASE_URL": "https://api.vendor-d.com/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "sk-ds-test",
    "ANTHROPIC_MODEL": "vendor-d-lite",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "vendor-d-lite",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "vendor-d-lite",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "vendor-d-lite",
}


#: The suite-wide autouse ``hermetic_cc_switch_current_provider`` fixture
#: replaces the resolver with a stub so no other test reads the operator's
#: live CC Switch. Captured here (at import, before any fixture runs) so
#: the resolver's own tests can put the real implementation back — they
#: stay hermetic by pointing ``HOME`` at ``tmp_path``.
_REAL_LOAD_CURRENT_PROVIDER = ClaudeCodingTool._load_cc_switch_current_provider


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def real_resolver(monkeypatch):
    """Restore the real resolver for the tests that cover it."""
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_load_cc_switch_current_provider",
        _REAL_LOAD_CURRENT_PROVIDER,
    )
    return _REAL_LOAD_CURRENT_PROVIDER


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveCurrentProvider:
    def test_resolves_current_provider_from_settings_pointer(self, fake_home, real_resolver):
        _seed_cc_switch(fake_home, current_id="prov-abc", env=_FULL_ENV)

        cfg = ClaudeCodingTool._load_cc_switch_current_provider()

        assert cfg.name == "Vendor D API"
        assert cfg.base_url == "https://api.vendor-d.com/anthropic"
        assert cfg.api_key == "sk-ds-test"
        assert cfg.models["default"] == "vendor-d-lite"
        assert cfg.models["opus"] == "vendor-d-lite"

    def test_falls_back_to_is_current_row(self, fake_home, real_resolver):
        _seed_cc_switch(fake_home, current_id=None, is_current=True, env=_FULL_ENV)

        cfg = ClaudeCodingTool._load_cc_switch_current_provider()

        assert cfg.name == "Vendor D API"

    def test_row_without_credentials_is_not_usable(self, fake_home, real_resolver):
        _seed_cc_switch(
            fake_home,
            env={"ANTHROPIC_BASE_URL": "https://x.test", "ANTHROPIC_AUTH_TOKEN": ""},
        )

        assert ClaudeCodingTool._load_cc_switch_current_provider() is None

    def test_missing_db_returns_none(self, fake_home):
        assert ClaudeCodingTool._load_cc_switch_current_provider() is None

    def test_api_key_field_is_accepted_as_token_alias(self, fake_home, real_resolver):
        _seed_cc_switch(
            fake_home,
            env={
                "ANTHROPIC_BASE_URL": "https://x.test",
                "ANTHROPIC_API_KEY": "sk-alt",
            },
        )

        cfg = ClaudeCodingTool._load_cc_switch_current_provider()

        assert cfg.api_key == "sk-alt"


# ---------------------------------------------------------------------------
# Dispatch walk shortcut
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLegacyWalkUsesCcSwitchCurrentProvider:
    def _tool(self, monkeypatch, priority=("Vendor B Pro",)):
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority",
            lambda *a, **k: list(priority),
        )
        monkeypatch.delenv("PDT_PROVIDER_PRIORITY", raising=False)
        return ClaudeCodingTool()

    def test_current_provider_wins_over_priority_walk(self, monkeypatch):
        monkeypatch.setattr(
            ClaudeCodingTool,
            "_load_cc_switch_current_provider",
            staticmethod(lambda: ProviderConfig(
                name="Vendor D API",
                env={
                    "ANTHROPIC_BASE_URL": "https://api.vendor-d.com/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "sk-ds",
                    "ANTHROPIC_MODEL": "vendor-d-lite",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "vendor-d-lite",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "vendor-d-lite",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "vendor-d-lite",
                },
                base_url="https://api.vendor-d.com/anthropic",
            )),
        )
        # The priority walk would have selected Vendor B Pro — prove it did not run
        # by making its checker explode if consulted.
        def _boom(*_a, **_k):
            raise AssertionError("priority walk must not run")

        monkeypatch.setattr(ClaudeCodingTool, "_check_provider_availability", _boom)

        tool = self._tool(monkeypatch)
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-d.com/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ds"
        assert env["ANTHROPIC_MODEL"] == "vendor-d-lite"
        assert tool.current_call_provider == "Vendor D API"
        assert tool.current_call_model == "vendor-d-lite"

    def test_explicit_provider_priority_env_still_wins(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_PRIORITY", "Vendor B Pro")
        monkeypatch.setattr(
            ClaudeCodingTool,
            "_load_cc_switch_current_provider",
            staticmethod(lambda: ProviderConfig(
                name="Vendor D API",
                env={
                    "ANTHROPIC_BASE_URL": "https://api.vendor-d.com/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "sk-ds",
                    "ANTHROPIC_MODEL": "vendor-d-lite",
                },
                base_url="https://api.vendor-d.com/anthropic",
            )),
        )
        monkeypatch.setattr(
            ClaudeCodingTool, "_check_provider_availability",
            staticmethod(lambda _name: (True, {
                "base_url": "https://vendor-b.test/anthropic",
                "api_key": "sk-vendor-b",
                "models": {},
            })),
        )

        tool = ClaudeCodingTool()
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://vendor-b.test/anthropic"
        assert tool.current_call_provider == "Vendor B Pro"

    def test_explicit_caller_base_url_still_wins(self, monkeypatch):
        """the caller's SubagentConfig endpoint is deliberate — don't override it.

        ``agent.py`` resolves a provider from the optimizer's chain and
        stamps ``base_url``/``auth_token`` on the tool (documented to
        exclude the proxy endpoint). The CC Switch shortcut must not
        second-guess that.
        """
        monkeypatch.setattr(
            ClaudeCodingTool,
            "_load_cc_switch_current_provider",
            staticmethod(lambda: ProviderConfig(
                name="Vendor D API",
                env={
                    "ANTHROPIC_BASE_URL": "https://api.vendor-d.com/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "sk-ds",
                    "ANTHROPIC_MODEL": "vendor-d-lite",
                },
                base_url="https://api.vendor-d.com/anthropic",
            )),
        )
        # Make the priority walk deterministically unavailable so the
        # assertion is about the caller's config, not about whichever
        # provider happens to answer on this machine.
        monkeypatch.setattr(
            ClaudeCodingTool, "_check_provider_availability",
            staticmethod(lambda _name: (False, {})),
        )
        tool = self._tool(monkeypatch)
        tool.base_url = "https://caller-chosen.test/anthropic"
        tool.auth_token = "sk-caller"
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://caller-chosen.test/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-caller"

    def test_empty_current_provider_falls_through_to_walk(self, monkeypatch):
        monkeypatch.setattr(
            ClaudeCodingTool,
            "_load_cc_switch_current_provider",
            staticmethod(lambda: None),
        )
        monkeypatch.setattr(
            ClaudeCodingTool, "_check_provider_availability",
            staticmethod(lambda _name: (True, {
                "base_url": "https://vendor-b.test/anthropic",
                "api_key": "sk-vendor-b",
                "models": {},
            })),
        )

        tool = self._tool(monkeypatch)
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        assert calls[0]["env"]["ANTHROPIC_BASE_URL"] == "https://vendor-b.test/anthropic"
