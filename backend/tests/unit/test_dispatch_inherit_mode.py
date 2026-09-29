"""Inherit mode must reach the subprocess, and reach it cleanly.

Why this exists
---------------
The library-level tests (``test_subagent_inherit_env``) prove that a
``SubagentConfig`` with ``inherit_env=True`` emits no provider
configuration. That is necessary but not sufficient: the settings file
the sub-agent actually receives is assembled inside the dispatch walk,
from a **copy of the process environment**, and anything in that copy can
be promoted into ``--settings`` — which outranks
``~/.claude/settings.json``.

So the interesting case is not "inherit mode with a clean environment",
where nothing could leak anyway. It is "inherit mode on a machine where
CC Switch has exported ``ANTHROPIC_BASE_URL`` into the shell". The user
set that variable for another tool; letting it outrank the config file
they wrote for *this* one would be a silent, plausible-looking override.

The control test runs the same scenario without inherit mode and asserts
the opposite, so "no ANTHROPIC_* in the file" cannot be satisfied by
breaking the normal path instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from coding_tool import ClaudeCodingTool  # noqa: E402

_INIT_EVENT = json.dumps(
    {
        "type": "system",
        "subtype": "init",
        "session_id": "00000000-0000-0000-0000-000000000000",
    }
)
_RESULT_EVENT = json.dumps({"type": "result", "result": "ok", "is_error": False})

#: The ambient provider configuration a CC Switch install leaves in the
#: shell. Worth noting these are *plausible* values, not obviously wrong
#: ones — which is exactly why promoting them is hard to notice.
_AMBIENT = {
    "ANTHROPIC_BASE_URL": "https://parent-proxy.invalid/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "tok-from-shell",
}


def _mock_process():
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter([_INIT_EVENT, _RESULT_EVENT])
    proc.stderr.read.return_value = ""
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    return proc


def _dispatch(tmp_path: Path, *, mode: str, ambient: dict):
    """Run one dispatch; return ``(cmd, written_settings_dict)``."""
    base = tmp_path / "base_settings.json"
    base.write_text(
        json.dumps({"env": {"PDT_PROJECT_DIR": "/somewhere"}, "hooks": {}}),
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env.update(ambient)
    env["PDT_PROVIDER_MODE"] = mode

    with patch.dict(os.environ, env, clear=True), patch(
        "coding_tool.subprocess.Popen"
    ) as popen:
        popen.return_value = _mock_process()
        tool = ClaudeCodingTool(settings=base)
        tool._run_claude_interactive("do something")

        cmd = popen.call_args[1].get("args") or popen.call_args[0][0]
        settings_path = Path(cmd[cmd.index("--settings") + 1])
        written = json.loads(settings_path.read_text(encoding="utf-8"))
        try:
            settings_path.unlink()
        except OSError:
            pass

    return list(cmd), written


class TestInheritModeReachesTheSubprocess:
    def test_the_settings_file_carries_no_provider_configuration(self, tmp_path):
        """The whole point, tested against the artifact that ships."""
        _cmd, written = _dispatch(tmp_path, mode="inherit", ambient=_AMBIENT)
        leaked = [k for k in written.get("env", {}) if k.startswith("ANTHROPIC_")]
        assert not leaked, (
            f"inherit mode promoted ambient provider config into --settings: "
            f"{leaked}. --settings outranks ~/.claude/settings.json, so "
            f"these would silently replace the user's own configuration."
        )

    def test_ambient_credentials_do_not_appear_anywhere_in_the_file(
        self, tmp_path
    ):
        _cmd, written = _dispatch(tmp_path, mode="inherit", ambient=_AMBIENT)
        rendered = json.dumps(written)
        assert "tok-from-shell" not in rendered
        assert "parent-proxy.invalid" not in rendered, (
            "the CC Switch proxy endpoint reached the settings file; that "
            "endpoint belongs to a different tool"
        )

    def test_no_model_flag_is_passed(self, tmp_path):
        """``--model`` would override the user's own model choice just as
        surely as an env var would."""
        cmd, _written = _dispatch(tmp_path, mode="inherit", ambient=_AMBIENT)
        assert "--model" not in cmd

    def test_hooks_are_still_written(self, tmp_path):
        """Instrumentation survives; it is not provider configuration.

        Without this, "inject nothing" could be satisfied by writing no
        settings file at all — which would quietly disable the
        stuck-subagent watchdog.
        """
        _cmd, written = _dispatch(tmp_path, mode="inherit", ambient=_AMBIENT)
        assert written.get("env", {}).get("PDT_PROJECT_DIR") == "/somewhere", (
            "the base settings payload (AC_* keys, hooks) must survive"
        )


class TestTheNormalPathStillWritesProviderConfiguration:
    """The control.

    Without it, the assertions above could be satisfied by breaking the
    normal path — e.g. by never copying anything, for everyone.

    The assertion is deliberately about *whether* an endpoint is written
    rather than *which* one: on a machine with CC Switch the provider
    walk resolves a real row, and on one without it the ambient env is
    promoted instead. Both are "the settings file carries provider
    configuration", which is the property inherit mode must turn off.
    """

    def test_normal_mode_writes_a_provider_endpoint(self, tmp_path):
        _cmd, written = _dispatch(tmp_path, mode="cc-switch", ambient=_AMBIENT)
        assert written["env"].get("ANTHROPIC_BASE_URL"), (
            "the normal path is expected to put an endpoint in the settings "
            "file — from the provider walk when CC Switch is present, from "
            "the ambient env otherwise"
        )


class TestModeDetection:
    def test_the_env_var_pins_the_mode(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_MODE", "inherit")
        assert ClaudeCodingTool._detect_inherit_mode() is True
        monkeypatch.setenv("PDT_PROVIDER_MODE", "cc-switch")
        assert ClaudeCodingTool._detect_inherit_mode() is False

    def test_the_env_var_is_case_and_space_insensitive(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_MODE", "  INHERIT  ")
        assert ClaudeCodingTool._detect_inherit_mode() is True

    def test_an_absent_cc_switch_means_inherit(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PDT_PROVIDER_MODE", raising=False)
        monkeypatch.delenv("PDT_CC_SWITCH_DB", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert ClaudeCodingTool._detect_inherit_mode() is True

    def test_a_present_cc_switch_means_do_not_inherit(self, monkeypatch):
        monkeypatch.delenv("PDT_PROVIDER_MODE", raising=False)
        monkeypatch.setenv("PDT_CC_SWITCH_DB", str(_stock_db(monkeypatch)))
        assert ClaudeCodingTool._detect_inherit_mode() is False


def _stock_db(monkeypatch) -> Path:
    """A minimal but schema-complete CC Switch database."""
    import sqlite3
    import tempfile

    path = Path(tempfile.mkdtemp()) / "cc-switch.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE providers ("
        " id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL,"
        " settings_config TEXT NOT NULL, is_current BOOLEAN DEFAULT 0,"
        " PRIMARY KEY (id, app_type))"
    )
    conn.commit()
    conn.close()
    return path
