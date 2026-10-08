"""
Unit tests for ClaudeCodingTool subprocess invocation.

These tests verify that:
1. Subprocess is launched correctly with proper arguments
2. Environment variables are inherited by default, overridden only when explicitly passed
3. Stream-json output is parsed correctly
4. Error conditions (API errors, timeouts) are handled properly
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from coding_tool import ClaudeCodingTool, ApiError
import coding_tool as ct


class TestClaudeCodingToolSubprocessInvocation:
    """Tests for ClaudeCodingTool._run_claude_interactive() subprocess behavior.

    All LLM calls go through ``_run_claude_interactive`` (M1 interactive
    mode). The legacy ``_run_claude`` --print path was deleted when the
    M1 migration was consolidated across query() and query_json().
    """

    @pytest.fixture(autouse=True)
    def _no_providers(self, monkeypatch):
        """Block real provider detection for all tests in this class.

        _load_provider_from_db reads ~/.cc-switch/cc-switch.db and
        _check_*_availability may make HTTP requests — both cause side
        effects that break tests expecting no provider overrides. We
        stub the loader to return empty (all providers unavailable)
        so the only way a test sees a provider is when it explicitly
        patches _check_*_availability.
        """
        monkeypatch.setattr(
            ClaudeCodingTool, "_load_provider_from_db", staticmethod(lambda provider_name: {})
        )

    def _make_mock_process(self, stdout_lines, stderr="", returncode=0):
        """Build a MagicMock that satisfies ``_run_claude_interactive``.

        Interactive mode writes the prompt as plain text (NOT JSON)
        to stdin, then reads stream-json events from stdout. ``stdin``
        must accept arbitrary ``write`` / ``close`` calls so the
        subprocess shutdown phase can drain the pipe.
        """
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()  # plain-text stdin (interactive mode)
        mock_proc.stdout = iter(stdout_lines)
        mock_proc.stderr.read.return_value = stderr
        mock_proc.wait.return_value = returncode
        mock_proc.poll.return_value = returncode
        return mock_proc

    def _init_event(self, session_id="00000000-0000-0000-0000-000000000000"):
        """Build the ``system init`` event that interactive mode emits
        before any assistant / result events. Including it keeps the
        ``resolved_session_id`` non-None; tests that don't care about
        the session id can ignore it.
        """
        return json.dumps({"type": "system", "subtype": "init", "session_id": session_id})

    @patch("coding_tool.subprocess.Popen")
    @patch(
        "coding_tool.ClaudeCodingTool._check_provider_availability",
        return_value=(False, {}),
    )
    def test_interactive_default_no_model_override(self, mock_check_provider, mock_popen):
        """Default: no --model flag, no env override. Parent env is inherited."""

        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "hello", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        with patch.dict(
            os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")},
            clear=True,
        ):
            tool = ClaudeCodingTool()
            result, _sid, _meta = tool._run_claude_interactive("test prompt")

        assert result == "hello"

        assert mock_popen.call_count == 1
        call_args = mock_popen.call_args
        cmd = call_args[1]["args"] if "args" in call_args[1] else call_args[0][0]

        # Interactive mode does NOT pass --print / -p
        assert "-p" not in cmd
        assert "--print" not in cmd
        assert "--model" not in cmd

        env = call_args[1].get("env") or call_args[1].get("kwargs", {}).get("env")
        if env is None:
            _, kwargs = call_args
            env = kwargs.get("env")
        assert env is not None
        assert "ANTHROPIC_MODEL" not in env
        assert "ANTHROPIC_API_KEY" not in env

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_default_inherits_parent_env(self, mock_popen):
        """Default: subprocess inherits all env vars from parent."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        tool._run_claude_interactive("test")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert set(env.keys()).issuperset(
            {"PATH", "HOME"}.intersection(os.environ)
        )

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_with_explicit_model(self, mock_popen):
        """When model is explicitly passed, --model flag and env var are set."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool(model="claude-opus-4-7")
        tool._run_claude_interactive("test")

        args, kwargs = mock_popen.call_args
        cmd = args[0] if args else kwargs["args"]
        env = kwargs["env"]

        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-7"
        assert env.get("ANTHROPIC_MODEL") == "claude-opus-4-7"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_with_explicit_api_key(self, mock_popen):
        """When api_key is explicitly passed, ANTHROPIC_API_KEY env var is set."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool(api_key="sk-ant-test-key")
        tool._run_claude_interactive("test")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-test-key"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_with_both_model_and_api_key(self, mock_popen):
        """Both model and api_key explicitly passed."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool(model="claude-opus-4-7", api_key="sk-ant-test")
        tool._run_claude_interactive("test")

        args, kwargs = mock_popen.call_args
        cmd = args[0] if args else kwargs["args"]
        env = kwargs["env"]

        assert "--model" in cmd
        assert env.get("ANTHROPIC_MODEL") == "claude-opus-4-7"
        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-test"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_reads_assistant_text_blocks(self, mock_popen):
        """assistant events with text blocks are accumulated into result."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ]
                }
            }),
            json.dumps({"type": "result", "result": "", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        result, _sid, _meta = tool._run_claude_interactive("test")

        assert result == "Hello world"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_reads_result_event(self, mock_popen):
        """result event provides the final output."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "final answer", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        result, _sid, _meta = tool._run_claude_interactive("test")

        assert result == "final answer"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_session_id_captured_from_init(self, mock_popen):
        """system init event carries the session_id we should return."""
        target_sid = "11111111-2222-3333-4444-555555555555"
        mock_proc = self._make_mock_process([
            self._init_event(session_id=target_sid),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        _result, sid, _meta = tool._run_claude_interactive("test")
        assert sid == target_sid

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_api_error_raises(self, mock_popen):
        """result event with is_error=True raises ApiError."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({
                "type": "result",
                "result": "Rate limit exceeded",
                "is_error": True,
                "api_error_status": "rate_limit",
            }),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        with pytest.raises(ApiError) as exc_info:
            tool._run_claude_interactive("test")

        assert exc_info.value.status == "rate_limit"
        assert "Rate limit exceeded" in str(exc_info.value)

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_writes_plain_prompt(self, mock_popen):
        """Interactive mode writes the prompt as plain text (not JSON)."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        tool._run_claude_interactive("my prompt text")

        mock_proc.stdin.write.assert_called()
        written = mock_proc.stdin.write.call_args_list[0][0][0]
        assert "my prompt text" in written
        # The legacy --print JSON envelope must NOT appear.
        assert '"type": "user"' not in written

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_graceful_shutdown_called(self, mock_popen):
        """After reading, graceful shutdown is performed."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        tool._run_claude_interactive("test")

        mock_proc.stdin.close.assert_called()
        mock_proc.wait.assert_called()

    @patch("coding_tool.subprocess.Popen")
    def test_query_uses_interactive(self, mock_popen):
        """query() delegates to _run_claude_interactive and returns the text."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({"type": "result", "result": "response text", "is_error": False}),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        result = tool.query("test prompt", system_instruction="be helpful")

        assert result == "response text"
        args, kwargs = mock_popen.call_args
        cmd = args[0] if args else kwargs["args"]
        assert "--append-system-prompt" in cmd
        sys_idx = cmd.index("--append-system-prompt")
        assert cmd[sys_idx + 1] == "be helpful"

    @patch("coding_tool.subprocess.Popen")
    def test_query_json_extracts_json_from_response(self, mock_popen):
        """query_json() extracts JSON from the text response."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({
                "type": "result",
                "result": 'Some text before {\n  "key": "value",\n  "num": 42\n} after',
                "is_error": False,
            }),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        result = tool.query_json("return json")

        assert result == {"key": "value", "num": 42}

    @patch("coding_tool.subprocess.Popen")
    def test_query_json_pure_json_response(self, mock_popen):
        """query_json() handles response that is pure JSON."""
        mock_proc = self._make_mock_process([
            self._init_event(),
            json.dumps({
                "type": "result",
                "result": '{"tasks": [{"id": "1", "title": "test"}]}',
                "is_error": False,
            }),
        ])
        mock_popen.return_value = mock_proc

        tool = ClaudeCodingTool()
        result = tool.query_json("plan tasks")

        assert result == {"tasks": [{"id": "1", "title": "test"}]}


class TestClaudeCodingToolEnvLoading:
    """Tests for the static _load_api_key_from_env() utility."""

    def test_load_api_key_from_env_variable(self, monkeypatch):
        """Load key from ANTHROPIC_API_KEY environment variable."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        key = ClaudeCodingTool._load_api_key_from_env()
        assert key == "sk-ant-env-key"

    def test_load_api_key_from_dotenv(self, tmp_path, monkeypatch):
        """Load key from .env file when env var is not set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        dotenv = tmp_path / ".env"
        dotenv.write_text("ANTHROPIC_API_KEY=sk-ant-dotenv-key\n")

        key = ClaudeCodingTool._load_api_key_from_env()
        assert key == "sk-ant-dotenv-key"

    def test_load_api_key_prefers_env_over_dotenv(self, tmp_path, monkeypatch):
        """Environment variable takes precedence over .env file."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        monkeypatch.chdir(tmp_path)

        dotenv = tmp_path / ".env"
        dotenv.write_text("ANTHROPIC_API_KEY=sk-ant-dotenv-key\n")

        key = ClaudeCodingTool._load_api_key_from_env()
        assert key == "sk-ant-env-key"

    def test_load_api_key_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Return None when neither env var nor .env file has the key."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # 2026-09-13: chdir to a bare tmp dir — running from the
        # repository root would otherwise pick up the developer's real
        # .env (the loader deliberately reads the CWD's .env).
        monkeypatch.chdir(tmp_path)
        key = ClaudeCodingTool._load_api_key_from_env()
        assert key is None


class TestClaudeCodingToolInit:
    """Tests for constructor behavior."""

    def test_init_defaults_none(self):
        """Default: model and api_key are None (inherit from parent)."""
        tool = ClaudeCodingTool()
        assert tool.model is None
        assert tool.api_key is None

    def test_init_with_explicit_values(self):
        """Explicit values are stored."""
        tool = ClaudeCodingTool(model="claude-opus", api_key="sk-test", cwd="/tmp")
        assert tool.model == "claude-opus"
        assert tool.api_key == "sk-test"
        assert tool.cwd == "/tmp"

    def test_init_does_not_auto_set_default_model(self):
        """Constructor does NOT auto-set a model — not even a default one."""
        tool = ClaudeCodingTool()
        # The class used to carry a ``DEFAULT_MODEL`` constant and stamp it
        # here. That is exactly the shape this project avoids: naming a
        # model in source picks one for every deployment. The model now
        # comes from the caller or the local CLI config, so an unconfigured
        # tool is None.
        assert tool.model is None


class TestClaudeCodingToolProviderSelection:
    """Provider resolution keyed by the CC Switch name, and the dispatch walk.

    A provider's identity is the name CC Switch gives it — the same
    string ``provider_routing.yaml`` matches its regexes against, the
    same string ``provider-order.json`` carries, and the column the DB
    row is keyed by. There is no second vocabulary and no translation
    table, so these tests name providers the way CC Switch spells them
    and assert that the lookup uses the name verbatim.
    """

    @pytest.fixture(autouse=True)
    def _deterministic_routing(self, monkeypatch):
        """Pin the routing inputs the dispatch reads before the walk.

        Two ambient sources can answer "which provider?" ahead of the
        priority walk: the shell's ``ANTHROPIC_BASE_URL`` and CC
        Switch's own current-provider shortcut (which reads the real
        ``~/.cc-switch`` when a test does not redirect ``$HOME``). Both
        are cleared so a test's own arrangement is the only input.
        """
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("PDT_PROVIDER_PRIORITY", raising=False)
        monkeypatch.setattr(
            ClaudeCodingTool,
            "_load_cc_switch_current_provider",
            staticmethod(lambda: None),
        )

    def _write_cc_switch_db(
        self,
        tmp_path: Path,
        providers: dict,
    ) -> None:
        """Write a fake cc-switch SQLite DB with the given provider rows.

        ``providers`` maps cc-switch ``name`` (e.g. ``"Vendor B Pro"``) to
        the env block that should land in the row's ``settings_config``.
        The schema mirrors the real cc-switch ``providers`` table.
        """
        import sqlite3
        db_dir = tmp_path / ".cc-switch"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "cc-switch.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE providers (
                    id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_config TEXT NOT NULL,
                    website_url TEXT,
                    category TEXT,
                    created_at INTEGER,
                    sort_index INTEGER,
                    notes TEXT,
                    icon TEXT,
                    icon_color TEXT,
                    meta TEXT NOT NULL DEFAULT '{}',
                    is_current BOOLEAN NOT NULL DEFAULT 0,
                    in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                    cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                    limit_daily_usd TEXT,
                    limit_monthly_usd TEXT,
                    provider_type TEXT,
                    PRIMARY KEY (id, app_type)
                )
                """
            )
            for i, (cc_name, env) in enumerate(providers.items()):
                settings_config = json.dumps({"env": env})
                conn.execute(
                    """
                    INSERT INTO providers
                        (id, app_type, name, settings_config, meta, is_current,
                         in_failover_queue, cost_multiplier)
                    VALUES (?, ?, ?, ?, '{}', 0, 0, '1.0')
                    """,
                    (f"id-{i}", "claude", cc_name, settings_config),
                )
            conn.commit()
        finally:
            conn.close()

    def _make_ok_proc(self):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = iter([
            json.dumps({"type": "system", "subtype": "init", "session_id": "x"}),
            json.dumps({"type": "result", "result": "ok", "is_error": False}),
        ])
        proc.stderr.read.return_value = ""
        proc.wait.return_value = 0
        return proc

    # ------------------------------------------------------------------
    # 1. _load_provider_from_db(name) — the named lookup
    # ------------------------------------------------------------------

    def test_load_provider_from_db(self, tmp_path, monkeypatch):
        """Reads base_url and api_key from the cc-switch row of that name."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        self._write_cc_switch_db(
            tmp_path,
            {
                "Vendor B Pro": {
                    "ANTHROPIC_BASE_URL": "https://api.vendor-b.example/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "test-vendor-b-key",
                    "ANTHROPIC_MODEL": "b-pro-4.7",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "b-pro-4.7",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "b-pro-5.1",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "b-pro-5.1",
                }
            },
        )

        # The argument is the CC Switch name verbatim — no kebab-case
        # id, no cc_switch_name indirection left to translate through.
        cfg = ClaudeCodingTool._load_provider_from_db("Vendor B Pro")
        assert cfg["base_url"] == "https://api.vendor-b.example/anthropic"
        assert cfg["api_key"] == "test-vendor-b-key"
        # The models mapping should be parsed from the env block too.
        assert cfg["models"]["haiku"] == "b-pro-4.7"
        assert cfg["models"]["default"] == "b-pro-4.7"

    def test_db_fallback_when_missing(self, tmp_path, monkeypatch):
        """DB row missing → return empty dict (caller treats as unavailable)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # DB exists but the providers table has no Vendor B row.
        import sqlite3
        db_dir = tmp_path / ".cc-switch"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "cc-switch.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE providers (
                    id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_config TEXT NOT NULL,
                    website_url TEXT,
                    category TEXT,
                    created_at INTEGER,
                    sort_index INTEGER,
                    notes TEXT,
                    icon TEXT,
                    icon_color TEXT,
                    meta TEXT NOT NULL DEFAULT '{}',
                    is_current BOOLEAN NOT NULL DEFAULT 0,
                    in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                    cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                    limit_daily_usd TEXT,
                    limit_monthly_usd TEXT,
                    provider_type TEXT,
                    PRIMARY KEY (id, app_type)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO providers
                    (id, app_type, name, settings_config, meta, is_current,
                     in_failover_queue, cost_multiplier)
                VALUES (?, ?, ?, ?, '{}', 0, 0, '1.0')
                """,
                ("id-1", "claude", "Some Other Provider", json.dumps({"env": {}})),
            )
            conn.commit()
        finally:
            conn.close()

        # The only row is "Some Other Provider"; nothing matches "Vendor B Pro".
        cfg = ClaudeCodingTool._load_provider_from_db("Vendor B Pro")
        assert cfg == {}

    def test_load_provider_from_db_returns_empty_when_db_missing(self, tmp_path, monkeypatch):
        """cc-switch.db absent → return empty dict, never crash."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # tmp_path/.cc-switch/ is empty (no DB file)
        cfg = ClaudeCodingTool._load_provider_from_db("Vendor B Pro")
        assert cfg == {}

    def test_load_provider_from_db_returns_empty_for_unknown_provider(self, tmp_path, monkeypatch):
        """Unknown provider name → empty dict."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg = ClaudeCodingTool._load_provider_from_db("not-a-real-provider")
        assert cfg == {}

    def test_load_provider_from_db_returns_empty_on_invalid_json(self, tmp_path, monkeypatch):
        """Malformed settings_config JSON → empty dict, never crash."""
        import sqlite3
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        db_dir = tmp_path / ".cc-switch"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "cc-switch.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE providers (
                    id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_config TEXT NOT NULL,
                    website_url TEXT,
                    category TEXT,
                    created_at INTEGER,
                    sort_index INTEGER,
                    notes TEXT,
                    icon TEXT,
                    icon_color TEXT,
                    meta TEXT NOT NULL DEFAULT '{}',
                    is_current BOOLEAN NOT NULL DEFAULT 0,
                    in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                    cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                    limit_daily_usd TEXT,
                    limit_monthly_usd TEXT,
                    provider_type TEXT,
                    PRIMARY KEY (id, app_type)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO providers
                    (id, app_type, name, settings_config, meta, is_current,
                     in_failover_queue, cost_multiplier)
                VALUES (?, ?, ?, ?, '{}', 0, 0, '1.0')
                """,
                ("id-1", "claude", "Vendor B Pro", "not valid json"),
            )
            conn.commit()
        finally:
            conn.close()

        # Malformed JSON for the "Vendor B Pro" row → the JSON parse error is
        # caught and an empty dict comes back.
        cfg = ClaudeCodingTool._load_provider_from_db("Vendor B Pro")
        assert cfg == {}

    def test_provider_name_is_used_verbatim(self, tmp_path, monkeypatch):
        """The DB key is the CC Switch name, unmodified.

        The deleted indirection translated a kebab-case id
        (``vendor-a-pro``) into the row label through a hard-coded
        table. The name is now the key, so the lookup must match
        ``"Vendor A Pro"`` exactly — and a kebab approximation of it
        must NOT resolve, because no row is spelled that way.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        self._write_cc_switch_db(
            tmp_path,
            {
                "Vendor A Pro": {
                    "ANTHROPIC_BASE_URL": "https://api.vendor-a.example/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "vendor-a-pro-key",
                    "ANTHROPIC_MODEL": "Vendor A-M3",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "Vendor A-M2.7",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "Vendor A-M3",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "Vendor A-M3",
                }
            },
        )

        cfg = ClaudeCodingTool._load_provider_from_db("Vendor A Pro")
        assert cfg["base_url"] == "https://api.vendor-a.example/anthropic"
        assert cfg["api_key"] == "vendor-a-pro-key"

        # Nothing normalizes the name on the way in.
        assert ClaudeCodingTool._load_provider_from_db("vendor-a-pro") == {}
        assert ClaudeCodingTool._load_provider_from_db("vendor-a-pro") == {}

    def test_named_lookup_never_answers_from_the_ambient_env(self, tmp_path, monkeypatch):
        """A named provider with no row is unavailable — not "use $ANTHROPIC_*".

        This is the property that keeps a log honest: the environment
        describes *an* endpoint and never says which provider it belongs
        to, so answering a named lookup from it would report one
        provider's name over another provider's endpoint. Measured
        2026-09-21: a call logged as ``vendor-a-pro`` with a hard-coded
        ``m2.7`` model was in fact billed by CC Switch to vendor-d-lite.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.example.com/v1")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-key")

        assert ClaudeCodingTool._load_provider_from_db("No Such Provider") == {}

    def test_unnamed_lookup_reads_the_ambient_env(self, tmp_path, monkeypatch):
        """With no name there is nothing to look up, so the env IS the config.

        This is the no-CC-Switch path: a machine with no rows still has
        a working Claude Code configuration in its environment.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.example.com/v1")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-key")

        cfg = ClaudeCodingTool._load_provider_from_db()
        assert cfg["base_url"] == "https://ambient.example.com/v1"
        assert cfg["api_key"] == "ambient-key"

    def test_unnamed_lookup_rejects_the_proxy_managed_sentinel(self, tmp_path, monkeypatch):
        """``PROXY MANAGED`` is not a credential — it names no endpoint."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://parent-proxy.invalid/anthropic")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "PROXY_MANAGED")

        assert ClaudeCodingTool._load_provider_from_db() == {}

    # ------------------------------------------------------------------
    # 2. _check_provider_availability(name)
    # ------------------------------------------------------------------

    def test_check_provider_availability_unknown_provider_returns_false(self, tmp_path, monkeypatch):
        """Unknown provider name → (False, {})."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        available, config = ClaudeCodingTool._check_provider_availability("not-a-real-provider")
        assert available is False
        assert config == {}

    def test_check_provider_availability_reads_the_named_row(self, tmp_path, monkeypatch):
        """A provider with a usable row is available, with its own credentials."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        self._write_cc_switch_db(
            tmp_path,
            {
                "Vendor A Pro": {
                    "ANTHROPIC_BASE_URL": "https://api.vendor-a.example/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "vendor-a-key",
                }
            },
        )

        available, config = ClaudeCodingTool._check_provider_availability("Vendor A Pro")
        assert available is True
        assert config["base_url"] == "https://api.vendor-a.example/anthropic"
        assert config["api_key"] == "vendor-a-key"

    def test_check_provider_availability_requires_both_url_and_key(self, tmp_path, monkeypatch):
        """A row missing the endpoint is unavailable, not half-configured."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        self._write_cc_switch_db(
            tmp_path,
            {
                "Vendor B Pro": {
                    "ANTHROPIC_AUTH_TOKEN": "test-key",
                    # ANTHROPIC_BASE_URL missing
                }
            },
        )

        available, config = ClaudeCodingTool._check_provider_availability("Vendor B Pro")
        assert available is False
        assert config == {}

    # ------------------------------------------------------------------
    # 3. _run_claude_interactive provider selection integration
    # ------------------------------------------------------------------

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_uses_the_routed_provider(self, mock_popen, monkeypatch, tmp_path):
        """The selected provider's own triple reaches the subprocess env."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mock_popen.return_value = self._make_ok_proc()

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (
                True,
                {
                    "base_url": "https://api.vendor-b.example/anthropic",
                    "api_key": "vendor-b-api-key",
                    "models": {
                        "sonnet": "b-pro-5.1",
                        "haiku": "b-pro-4.7",
                        "opus": "b-pro-5.1",
                        "default": "b-pro-4.7",
                    },
                },
            )),
        ):
            tool = ClaudeCodingTool(provider_priority=["Vendor B Pro"])
            tool._run_claude_interactive("test")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]

        assert tool.current_call_provider == "Vendor B Pro"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-b.example/anthropic"
        assert env["ANTHROPIC_API_KEY"] == "vendor-b-api-key"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "b-pro-5.1"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "b-pro-4.7"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "b-pro-5.1"
        assert env["ANTHROPIC_MODEL"] == "b-pro-4.7"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_inherits_parent_when_no_provider_available(
        self, mock_popen, monkeypatch
    ):
        """Nothing routable → inherit the parent process env without overrides."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://parent-proxy.invalid/anthropic")
        monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
        mock_popen.return_value = self._make_ok_proc()

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (False, {})),
        ):
            tool = ClaudeCodingTool(provider_priority=["Vendor B Pro"])
            tool._run_claude_interactive("test")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]

        assert env["ANTHROPIC_BASE_URL"] == "https://parent-proxy.invalid/anthropic"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-4-6"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_routed_provider_wins_over_explicit_api_key(
        self, mock_popen, monkeypatch
    ):
        """The routed provider supplies the whole triple, not just the URL.

        2026-09-14: the provider mapping (scene → tier → chain) is the
        single source of truth, so an explicit ``api_key`` may no longer
        outrank it. The previous contract had it the other way round —
        the assertion was ``ANTHROPIC_BASE_URL`` == Vendor B's URL **and**
        ``ANTHROPIC_API_KEY`` == a foreign key, i.e. it pinned exactly
        the cross-provider triple that made every subagent dispatch die
        with ``API Error: 401`` (plan
        2026-09-04). No production caller
        passes ``api_key=``; the one real site (``agent.autonomous_coding``)
        passes a coherent ``base_url`` / ``auth_token`` pair.

        The fallback meaning of ``api_key`` — used only when nothing is
        routable — is covered by
        ``test_interactive_explicit_api_key_used_when_no_provider_routed``.
        """
        mock_popen.return_value = self._make_ok_proc()

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (
                True,
                {
                    "base_url": "https://api.vendor-b.example/anthropic",
                    "api_key": "vendor-b-key",
                    "models": {"sonnet": "b-pro-5.1"},
                },
            )),
        ):
            tool = ClaudeCodingTool(
                api_key="explicit-key", provider_priority=["Vendor B Pro"]
            )
            tool._run_claude_interactive("test")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]

        assert env["ANTHROPIC_API_KEY"] == "vendor-b-key"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-b.example/anthropic"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_explicit_api_key_used_when_no_provider_routed(
        self, mock_popen
    ):
        """Fallback path: with nothing routable the explicit key survives.

        The sentinel priority list is non-empty on purpose — ``[]`` is
        falsy and the constructor would then read ``PDT_PROVIDER_PRIORITY``
        instead of using the list this test means to pin.
        """
        mock_popen.return_value = self._make_ok_proc()

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (False, {})),
        ):
            tool = ClaudeCodingTool(
                api_key="explicit-key",
                provider_priority=["__no_such_provider__"],
            )
            tool._run_claude_interactive("test")

        _, kwargs = mock_popen.call_args
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == "explicit-key"

    @patch("coding_tool.subprocess.Popen")
    def test_interactive_logs_provider_selection(self, mock_popen):
        """Logger receives provider selection info naming the chosen provider."""
        mock_popen.return_value = self._make_ok_proc()
        mock_logger = MagicMock()

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (
                True,
                {
                    "base_url": "https://api.vendor-b.example/anthropic",
                    "api_key": "vendor-b-key",
                    "models": {"sonnet": "b-pro-5.1"},
                },
            )),
        ):
            tool = ClaudeCodingTool(logger=mock_logger, provider_priority=["Vendor B Pro"])
            tool._run_claude_interactive("test")

        info_calls = [c for c in mock_logger.info.call_args_list]
        assert len(info_calls) >= 1
        assert info_calls[0][0][0] == "provider_selected"
        assert info_calls[0][1]["data"]["provider"] == "Vendor B Pro"

    # ------------------------------------------------------------------
    # 4. Fallback when the selected provider fails at runtime
    # ------------------------------------------------------------------

    @patch("coding_tool.subprocess.Popen")
    def test_query_fallback_to_parent_when_provider_api_error(self, mock_popen):
        """Selected provider fails → fall back to the parent config for the retry."""
        fail_proc = MagicMock()
        fail_proc.stdout = iter([
            json.dumps({
                "type": "result",
                "result": "Rate limit exceeded",
                "is_error": True,
                "api_error_status": "rate_limit",
            }),
        ])
        fail_proc.stderr.read.return_value = ""
        fail_proc.wait.return_value = 0

        ok_proc = MagicMock()
        ok_proc.stdout = iter([
            json.dumps({"type": "result", "result": "fallback-ok", "is_error": False}),
        ])
        ok_proc.stderr.read.return_value = ""
        ok_proc.wait.return_value = 0

        mock_popen.side_effect = [fail_proc, ok_proc]

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (
                True,
                {
                    "base_url": "https://api.vendor-b.example/anthropic",
                    "api_key": "vendor-b-key",
                    "models": {"sonnet": "b-pro-5.1"},
                },
            )),
        ):
            # Run with a clean parent env so the fallback is distinguishable.
            with patch.dict(
                os.environ,
                {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")},
                clear=True,
            ):
                tool = ClaudeCodingTool(provider_priority=["Vendor B Pro"])
                result = tool.query("test")

        assert result == "fallback-ok"
        assert mock_popen.call_count == 2

        # First call used the routed provider's endpoint.
        _, kwargs1 = mock_popen.call_args_list[0]
        assert kwargs1["env"]["ANTHROPIC_BASE_URL"] == "https://api.vendor-b.example/anthropic"

        # Second call (fallback) must not re-use it.
        _, kwargs2 = mock_popen.call_args_list[1]
        assert kwargs2["env"].get("ANTHROPIC_BASE_URL") != "https://api.vendor-b.example/anthropic"

    @patch("coding_tool.subprocess.Popen")
    def test_query_json_fallback_to_parent_when_provider_api_error(self, mock_popen):
        """query_json also falls back to the parent config on ApiError."""
        fail_proc = MagicMock()
        fail_proc.stdout = iter([
            json.dumps({
                "type": "result",
                "result": "Rate limit exceeded",
                "is_error": True,
                "api_error_status": "rate_limit",
            }),
        ])
        fail_proc.stderr.read.return_value = ""
        fail_proc.wait.return_value = 0

        ok_proc = MagicMock()
        ok_proc.stdout = iter([
            json.dumps({"type": "result", "result": '{"key": "fallback-value"}', "is_error": False}),
        ])
        ok_proc.stderr.read.return_value = ""
        ok_proc.wait.return_value = 0

        mock_popen.side_effect = [fail_proc, ok_proc]

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (
                True,
                {
                    "base_url": "https://api.vendor-b.example/anthropic",
                    "api_key": "vendor-b-key",
                    "models": {"sonnet": "b-pro-5.1"},
                },
            )),
        ):
            with patch.dict(
                os.environ,
                {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")},
                clear=True,
            ):
                tool = ClaudeCodingTool(provider_priority=["Vendor B Pro"])
                result = tool.query_json("return json")

        assert result == {"key": "fallback-value"}
        assert mock_popen.call_count == 2

    @patch("coding_tool.subprocess.Popen")
    def test_query_no_fallback_when_nothing_was_selected(self, mock_popen):
        """If no provider was selected, ApiError is raised immediately."""
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            json.dumps({
                "type": "result",
                "result": "Some API error",
                "is_error": True,
                "api_error_status": "unknown",
            }),
        ])
        mock_proc.stderr.read.return_value = ""
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (False, {})),
        ):
            tool = ClaudeCodingTool(
                provider_priority=["__no_such_provider__"]
            )
            with pytest.raises(ApiError) as exc_info:
                tool.query("test")

        assert exc_info.value.status == "unknown"
        # Only called once — no fallback retry without a selected provider.
        assert mock_popen.call_count == 1

    @patch("coding_tool.subprocess.Popen")
    def test_query_no_infinite_fallback_loop(self, mock_popen):
        """Fallback only happens once; if the parent config also fails, raise."""
        def _make_fail_proc():
            p = MagicMock()
            p.stdout = iter([
                json.dumps({
                    "type": "result",
                    "result": "Error",
                    "is_error": True,
                    "api_error_status": "rate_limit",
                }),
            ])
            p.stderr.read.return_value = ""
            p.wait.return_value = 0
            return p

        mock_popen.side_effect = [_make_fail_proc(), _make_fail_proc()]

        with patch.object(
            ClaudeCodingTool,
            "_check_provider_availability",
            staticmethod(lambda name: (
                True,
                {
                    "base_url": "https://api.vendor-b.example/anthropic",
                    "api_key": "vendor-b-key",
                    "models": {"sonnet": "b-pro-5.1"},
                },
            )),
        ):
            with patch.dict(
                os.environ,
                {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")},
                clear=True,
            ):
                tool = ClaudeCodingTool(provider_priority=["Vendor B Pro"])
                with pytest.raises(ApiError):
                    tool.query("test")

        # Selected-provider attempt + exactly one fallback.
        assert mock_popen.call_count == 2


def test_interactive_dispatch_has_no_hardcoded_provider_branches():
    """Static check: the dispatch selects providers by name, not by literal branch.

    The deleted per-provider wrappers were resolved by *deriving a method
    name from the provider key* (``vendor-b-pro`` →
    ``getattr(tool, "_check_vendor-b_glm_availability")``), which made a
    provider's existence depend on a Python method existing — the
    opposite of what a config file is for. The dispatch must stay
    branch-free: one loop over candidate names, one checker.
    """
    import inspect

    from coding_tool import ClaudeCodingTool

    source = inspect.getsource(ClaudeCodingTool._run_claude_interactive)
    assert 'prov_key == "vendor-b"' not in source, (
        "_run_claude_interactive still has a hardcoded 'if prov_key == \"vendor-b\"' branch"
    )
    assert 'prov_key == "vendor-a-pro"' not in source, (
        "_run_claude_interactive still has a hardcoded vendor-a-pro branch"
    )
    assert "PROVIDER_REGISTRY" not in source, (
        "_run_claude_interactive still dispatches through the deleted registry"
    )
    assert "_check_provider_availability(" in source, (
        "_run_claude_interactive must check candidates through the single "
        "name-keyed checker"
    )


# ---------------------------------------------------------------------------
# TDD: no hardcoded provider identifiers in coding_tool.py (task 11/12)
# ---------------------------------------------------------------------------


def test_no_bare_vendor_b_in_coding_tool():
    """Static scan reports no hardcoded provider identifiers in coding_tool.py."""
    project_root = Path(__file__).parent.parent.parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "scripts/scanners/scan_hardcoded_provider_ids.py",
            "--file",
            "backend/coding_tool.py",
        ],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        # The only subprocess in this file that no ``@patch`` covers, so
        # the 28 ``Popen`` mocks do nothing for it. Without a timeout
        # ``communicate()`` waits for an EOF that a wedged child never
        # sends, and the whole shard goes with it — which is what a
        # 45-minute CI reap with no log and no artifact looks like from
        # the outside. The scan takes 0.05s; anything near the ceiling
        # means it is stuck, not slow.
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan_hardcoded_provider_ids found violations in coding_tool.py:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# M1 consolidation: query() must use _run_claude_interactive exclusively,
# and the legacy ``_run_claude`` / ``_query_with_retry`` paths must be gone.
# ---------------------------------------------------------------------------


@patch("coding_tool.subprocess.Popen")
def test_query_returns_str_from_interactive_tuple(mock_popen):
    """query() unwraps the (text, session_id) tuple returned by interactive.

    Confirms the M1 consolidation of query() onto the interactive path:
    the new method returns a tuple, but the public ``query`` API must
    still return a plain ``str`` (its 30 callers depend on it).
    """
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": "abc"}),
        json.dumps({"type": "result", "result": "the-answer", "is_error": False}),
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_popen.return_value = mock_proc

    tool = ClaudeCodingTool()
    result = tool.query("hello")
    assert result == "the-answer"
    assert isinstance(result, str)


@patch("coding_tool.ClaudeCodingTool._check_provider_availability")
@patch("coding_tool.subprocess.Popen")
def test_query_interactive_session_id_is_none(mock_popen, mock_check_provider):
    """query() passes session_id=None (no resume) to interactive on each call.

    query() is a one-shot call: there is no caller that needs to resume a
    conversation across calls. Every call uses a fresh session so provider
    failures do not pollute prior history.
    """
    mock_check_provider.return_value = (
        True,
        {
            "base_url": "https://example.com",
            "api_key": "k",
            "models": {"sonnet": "m", "haiku": "m", "opus": "m", "default": "m"},
        },
    )
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": "x"}),
        json.dumps({"type": "result", "result": "ok", "is_error": False}),
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_popen.return_value = mock_proc

    tool = ClaudeCodingTool()
    tool.query("hello")

    # The cmd line should NOT include --resume / --session-id (query() is
    # a fresh session per call).
    call_args = mock_popen.call_args
    cmd = call_args[1]["args"] if "args" in call_args[1] else call_args[0][0]
    assert "--resume" not in cmd
    assert "--session-id" not in cmd


@patch("coding_tool.subprocess.Popen")
def test_query_interactive_no_print_flag(mock_popen):
    """query() never launches ``claude -p`` / ``--print``.

    The legacy --print path was the entire motivation for deleting
    ``_run_claude``. This guards against future regressions that re-add
    the --print flag (which would strip the LLM's Read/Bash/Edit
    tool access).
    """
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": "x"}),
        json.dumps({"type": "result", "result": "ok", "is_error": False}),
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_popen.return_value = mock_proc

    tool = ClaudeCodingTool()
    tool.query("hello")

    call_args = mock_popen.call_args
    cmd = call_args[1]["args"] if "args" in call_args[1] else call_args[0][0]
    # ``-p`` is a Claude Code shortcut for ``--print``. In interactive
    # mode the launcher builds ``claude --verbose --output-format stream-json
    # --permission-mode bypassPermissions ...`` — ``-p`` / ``--print`` MUST
    # be absent as standalone argv elements.
    assert "-p" not in cmd, f"claude -p (legacy --print) detected in cmd: {cmd}"
    assert "--print" not in cmd


def test_run_claude_method_removed():
    """Static guard: the legacy ``_run_claude`` method has been deleted.

    After the M1 consolidation of query() onto ``_run_claude_interactive``,
    there is no longer a need for two parallel launchers. ``_run_claude``
    existed only as the --print implementation; its deletion is the
    whole point of the M1 cleanup.
    """
    assert not hasattr(ClaudeCodingTool, "_run_claude"), (
        "legacy _run_claude (--print mode) must be removed; "
        "query() / query_json() both use _run_claude_interactive"
    )


def test_query_with_retry_method_removed():
    """Static guard: the legacy ``_query_with_retry`` method has been deleted.

    ``_query_internal_m1`` (added with the M1 consolidation) owns the
    same retry / provider-fallback semantics as the old
    ``_query_with_retry``, but drives ``_run_claude_interactive``
    directly. Keeping both creates a maintenance trap where one path
    silently diverges from the other.
    """
    assert not hasattr(ClaudeCodingTool, "_query_with_retry"), (
        "legacy _query_with_retry (--print mode retry path) must be removed; "
        "_query_internal_m1 owns the equivalent M1 retry loop"
    )


@patch("coding_tool.ClaudeCodingTool._check_provider_availability")
@patch("coding_tool.subprocess.Popen")
def test_query_timeout_fallback_uses_new_session(mock_popen, mock_check_provider):
    """On timeout, query() retries with a fresh subprocess (no session resume).

    M1 path: the timeout-driven provider fallback spawns a new claude
    process, NOT ``--resume`` of the previous session. This avoids
    contaminating the new provider's conversation with whatever the
    timed-out provider already emitted.
    """
    mock_check_provider.return_value = (False, {})

    # First process hangs (no stdout → idle kill).
    hanging_proc = MagicMock()
    hanging_proc.stdin = MagicMock()
    hanging_proc.stdout = iter([])
    hanging_proc.stderr.read.return_value = ""
    hanging_proc.wait.return_value = 0
    hanging_proc.poll.return_value = None

    # Second process succeeds after fallback to parent env.
    ok_proc = MagicMock()
    ok_proc.stdin = MagicMock()
    ok_proc.stdout = iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": "y"}),
        json.dumps({"type": "result", "result": "fallback-ok", "is_error": False}),
    ])
    ok_proc.stderr.read.return_value = ""
    ok_proc.wait.return_value = 0
    mock_popen.side_effect = [hanging_proc, ok_proc]

    with patch.dict(
        os.environ,
        {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")},
        clear=True,
    ):
        tool = ClaudeCodingTool()
        call_count = {"n": 0}

        def fake_interactive(prompt, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                tool.current_call_provider = "vendor-b-pro"
                raise TimeoutError("simulated timeout")
            return ("fallback-ok", "session-y", {})

        fake_mock = MagicMock(side_effect=fake_interactive)
        with patch.object(tool, "_run_claude_interactive", fake_mock):
            result = tool.query("hello", timeout=10)
        assert result == "fallback-ok"
        assert call_count["n"] == 2
        second_kwargs = fake_mock.call_args_list[1].kwargs
        assert not second_kwargs.get("resume", False) or second_kwargs.get("session_id") is None


@patch("coding_tool.ClaudeCodingTool._check_provider_availability")
@patch("coding_tool.subprocess.Popen")
def test_query_api_error_fallback_uses_new_session(mock_popen, mock_check_provider):
    """On ApiError, query() retries with a fresh subprocess (no session resume)."""
    mock_check_provider.return_value = (
        True,
        {
            "base_url": "https://example.com",
            "api_key": "k",
            "models": {"sonnet": "m", "haiku": "m", "opus": "m", "default": "m"},
        },
    )

    ok_proc = MagicMock()
    ok_proc.stdin = MagicMock()
    ok_proc.stdout = iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": "z"}),
        json.dumps({"type": "result", "result": "fallback-ok", "is_error": False}),
    ])
    ok_proc.stderr.read.return_value = ""
    ok_proc.wait.return_value = 0
    mock_popen.return_value = ok_proc

    call_count = {"n": 0}

    def fake_interactive(prompt, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            tool.current_call_provider = "vendor-b-pro"
            raise ApiError("rate_limit", status="rate_limit")
        return ("fallback-ok", "session-z", {})

    tool = ClaudeCodingTool()
    fake_mock = MagicMock(side_effect=fake_interactive)
    with patch.object(tool, "_run_claude_interactive", fake_mock):
        result = tool.query("hello")
    assert result == "fallback-ok"
    assert call_count["n"] == 2
    second_kwargs = fake_mock.call_args_list[1].kwargs
    assert not second_kwargs.get("resume", False) or second_kwargs.get("session_id") is None




def test_query_internal_m1_api_error_falls_back_instead_of_name_error(monkeypatch):
    """A provider ApiError must fall through to the next provider.

    2026-09-23: the ``except ApiError`` handler in ``_query_internal_m1``
    logged ``remaining_after_exclusion`` computed from ``scene_chain`` —
    a local variable of ``_run_claude_interactive``, not of that method.
    The ``NameError`` fired *inside* the handler, so ``return _call(
    excluded=new_excluded)`` never ran and a recoverable provider error
    (Vendor A 402 "insufficient balance") became a hard task failure
    reading ``name 'scene_chain' is not defined``. Tasks 9 and 14-3 of
    the 0921 plan died that way, and the executor stopped with
    "No schedulable micro-layer found".

    The fix reports ``excluded_after_failure`` (what this scope knows)
    instead of a chain it cannot see. This test drives the handler and
    asserts the retry actually happens.
    """
    monkeypatch.setattr(
        ClaudeCodingTool, "_load_provider_from_db",
        staticmethod(lambda provider_name: {}),
    )
    monkeypatch.setattr(
        ct, "_park_provider_after_error", lambda provider, exc: 60.0,
    )

    tool = ClaudeCodingTool()
    tool.logger = MagicMock()
    tool.current_call_provider = "Vendor A API"

    attempts = []

    def fake_resilient(self, prompt, system_instruction=None, session_id=None,
                       resume=False, excluded_providers=None,
                       total_timeout=None, allowed_tools=None, scene=None):
        attempts.append(list(excluded_providers or []))
        if len(attempts) == 1:
            self.current_call_provider = "Vendor A API"
            raise ApiError("402 insufficient balance", status="402")
        return ("RECOVERED", "session-x")

    monkeypatch.setattr(
        ClaudeCodingTool, "_run_interactive_resilient", fake_resilient,
    )

    out = tool._query_internal_m1(
        "prompt", None, None, start_time=0.0, prompt_len=6,
    )

    assert out == "RECOVERED", (
        "the ApiError handler must retry with the failed provider "
        "excluded and return the retry's result"
    )
    assert len(attempts) == 2, (
        f"expected exactly one retry; attempts={attempts}"
    )
    assert "Vendor A API" in attempts[1], (
        f"the failed provider must be excluded on the retry; got {attempts[1]}"
    )
