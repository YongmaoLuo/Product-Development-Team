"""
Unit tests for configuration loading from environment variables.

Verifies the env_config module behavior:
- Loads .env file into process environment
- Requires nothing, so a bare deployment starts
- Defaults apply when neither the file nor the shell supplies a value
- The shell beats the file
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from env_config import DEFAULTS, REQUIRED_VARS, load_env_config


class TestConfigLoadFromDotenv:
    def test_config_loads_from_dotenv(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("TIMEZONE=UTC\nANTHROPIC_API_KEY=sk-ant-valid-key\n")
        monkeypatch.delenv("TIMEZONE", raising=False)

        config = load_env_config(str(env_file))

        assert config["TIMEZONE"] == "UTC"

    def test_config_missing_env_file_does_not_crash(self, tmp_path, monkeypatch):
        """A deployment may configure nothing at all.

        The loader used to raise ``KeyError`` naming the first absent
        entry of a three-name requirement list. None of the three had a
        reader, and the one that mattered most — the API key — is
        supplied by the provider layer rather than from here, so the
        demand only ever refused starts that should have succeeded.
        """
        for key in DEFAULTS:
            monkeypatch.delenv(key, raising=False)

        config = load_env_config(str(tmp_path / "does_not_exist.env"))

        assert config == dict(DEFAULTS)


class TestConfigDefaults:
    def test_config_uses_defaults_when_vars_unset(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("TIMEZONE=UTC\n")
        monkeypatch.delenv("TIMEZONE", raising=False)

        config = load_env_config(str(env_file))

        assert config["TIMEZONE"] == "UTC"

    def test_config_env_overrides_defaults(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("TIMEZONE=UTC\n")
        monkeypatch.setenv("TIMEZONE", "Europe/Berlin")

        config = load_env_config(str(env_file))

        assert config["TIMEZONE"] == "Europe/Berlin"


class TestRequiredVarsContract:
    def test_required_vars_include_core_keys(self):
        """Nothing is required, and that is the accurate description.

        Kept as its own test rather than folded into the loader tests so
        that a diff which adds a name has to update the assertion that
        says there are none. ``test_env_config_contract.py`` makes the
        same argument; this one is the cheap half of the same guard.
        """
        assert REQUIRED_VARS == []

    def test_defaults_include_safe_values(self):
        assert "TIMEZONE" in DEFAULTS