"""
Unit tests for configuration loading from environment variables.

Verifies the env_config module behavior:
- Loads .env file into process environment
- Validates required keys are present
- Provides clear errors when required values are missing
- Falls back to sane defaults for optional keys
- Validates ANTHROPIC_API_KEY format
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from env_config import load_env_config, validate_config, REQUIRED_VARS, DEFAULTS


class TestConfigLoadFromDotenv:
    def test_config_loads_from_dotenv(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-valid-key\n"
            "NOTION_TOKEN=secret_test123\n"
            "NOTION_PARENT_PAGE_ID=page-id-123\n"
        )
        for var in [
            "ANTHROPIC_API_KEY",
            "NOTION_TOKEN",
            "NOTION_PARENT_PAGE_ID",
            "TIMEZONE",
        ]:
            monkeypatch.delenv(var, raising=False)

        config = load_env_config(str(env_file))

        assert config["ANTHROPIC_API_KEY"] == "sk-ant-valid-key"
        assert config["NOTION_TOKEN"] == "secret_test123"
        assert config["NOTION_PARENT_PAGE_ID"] == "page-id-123"

    def test_config_missing_required_var_raises_keyerror(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("NOTION_TOKEN=secret_test\nNOTION_PARENT_PAGE_ID=page-id\n")
        for var in ["ANTHROPIC_API_KEY", "NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"]:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(KeyError) as excinfo:
            load_env_config(str(env_file))
        assert "ANTHROPIC_API_KEY" in str(excinfo.value)

    def test_config_error_message_mentions_missing_key(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-valid\n"
            "NOTION_PARENT_PAGE_ID=page-id\n"
        )
        for var in ["ANTHROPIC_API_KEY", "NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"]:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(KeyError) as excinfo:
            load_env_config(str(env_file))
        assert "NOTION_TOKEN" in str(excinfo.value)

    def test_config_missing_env_file_does_not_crash(self, tmp_path, monkeypatch):
        for var in ["ANTHROPIC_API_KEY", "NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        monkeypatch.setenv("NOTION_TOKEN", "tok")
        monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "pid")

        nonexistent = tmp_path / "does_not_exist.env"
        config = load_env_config(str(nonexistent))
        assert config["ANTHROPIC_API_KEY"] == "sk-ant-from-env"


class TestConfigValidation:
    def test_invalid_api_key_format_raises_value_error(self):
        config = {"ANTHROPIC_API_KEY": "invalid-key"}
        with pytest.raises(ValueError) as excinfo:
            validate_config(config)
        assert "sk-ant-" in str(excinfo.value)

    def test_valid_api_key_format_passes(self):
        config = {"ANTHROPIC_API_KEY": "sk-ant-valid-key"}
        validate_config(config)

    def test_empty_api_key_rejected(self):
        config = {"ANTHROPIC_API_KEY": ""}
        with pytest.raises(ValueError):
            validate_config(config)


class TestConfigDefaults:
    def test_config_uses_defaults_when_vars_unset(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-valid\n"
            "NOTION_TOKEN=token\n"
            "NOTION_PARENT_PAGE_ID=page\n"
        )
        for var in [
            "ANTHROPIC_API_KEY",
            "NOTION_TOKEN",
            "NOTION_PARENT_PAGE_ID",
            "TIMEZONE",
        ]:
            monkeypatch.delenv(var, raising=False)

        config = load_env_config(str(env_file))

        assert config["TIMEZONE"] == "Asia/Shanghai"

    def test_config_env_overrides_defaults(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-valid\n"
            "NOTION_TOKEN=token\n"
            "NOTION_PARENT_PAGE_ID=page\n"
            "TIMEZONE=UTC\n"
        )
        for var in [
            "ANTHROPIC_API_KEY",
            "NOTION_TOKEN",
            "NOTION_PARENT_PAGE_ID",
            "TIMEZONE",
        ]:
            monkeypatch.delenv(var, raising=False)

        config = load_env_config(str(env_file))

        assert config["TIMEZONE"] == "UTC"


class TestRequiredVarsContract:
    def test_required_vars_include_core_keys(self):
        assert "ANTHROPIC_API_KEY" in REQUIRED_VARS
        assert "NOTION_TOKEN" in REQUIRED_VARS
        assert "NOTION_PARENT_PAGE_ID" in REQUIRED_VARS

    def test_defaults_include_safe_values(self):
        assert "TIMEZONE" in DEFAULTS
