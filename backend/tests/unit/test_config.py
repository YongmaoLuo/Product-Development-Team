import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from env_config import load_env_config, validate_config


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

    def test_config_missing_required_var(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("NOTION_TOKEN=secret_test\nNOTION_PARENT_PAGE_ID=page-id\n")
        for var in ["ANTHROPIC_API_KEY", "NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"]:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(KeyError):
            load_env_config(str(env_file))


class TestConfigValidation:
    def test_invalid_api_key_format(self):
        config = {"ANTHROPIC_API_KEY": "invalid-key"}
        with pytest.raises(ValueError):
            validate_config(config)

    def test_valid_api_key_format(self):
        config = {"ANTHROPIC_API_KEY": "sk-ant-valid-key"}
        validate_config(config)


class TestConfigDefaults:
    def test_config_has_defaults(self, tmp_path, monkeypatch):
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
