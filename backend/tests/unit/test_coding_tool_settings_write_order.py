"""Regression: the ``--settings`` file must carry the ROUTED provider env.

Background
----------
``coding_tool._run_claude_interactive`` assembles a per-subagent settings
file and passes it to ``claude --settings <file>``. The Claude CLI gives
the ``--settings`` ``env`` block **precedence over the process env**: a
request served entirely from the settings file succeeds even when the
process env points at an unreachable host.

The settings file used to be written *before* the provider-selection /
scene-routing block mutated ``env``. It therefore pinned whatever
``ANTHROPIC_BASE_URL`` the parent process happened to carry — in this
fleet the CC Switch proxy at ``parent-proxy.invalid`` — and every subagent
call was served by whichever provider CC Switch had selected, no matter
what the scene router had chosen.

Symptom that surfaced it: the backend's execution log reported a Vendor A dispatch
while CC Switch's ``proxy_request_logs`` table recorded **zero** Vendor A
requests for the same window.

These tests pin the ordering contract:

  * ``test_settings_file_env_matches_scene_routed_provider``
       — the on-disk file's ``ANTHROPIC_BASE_URL`` / ``_API_KEY`` /
          ``ANTHROPIC_MODEL`` equal the scene-routed provider's values,
          NOT the parent env's proxy URL.
  * ``test_settings_file_written_after_self_overrides``
       — an explicit ``ClaudeCodingTool(base_url=...)`` override also
          reaches the file (the write happens after every ``env``
          mutation, including the ``self.*`` overrides).

Both tests mock ``subprocess.Popen`` so no ``claude -p`` process is
spawned; only the args/env handed to Popen and the on-disk settings file
are inspected.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from coding_tool import ClaudeCodingTool
from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_ROUTED_BASE_URL = "https://scene-routed.example.invalid/anthropic"
_PROXY_BASE_URL = "https://parent-proxy.invalid/anthropic"
_ROUTED_ROW = {
    "base_url": _ROUTED_BASE_URL,
    "api_key": "sk-scene-routed-key",
    "models": {
        "default": "scene-default-model",
        "sonnet": "scene-sonnet-model",
        "haiku": "scene-haiku-model",
        "opus": "scene-opus-model",
    },
}


@pytest.fixture
def subagent_cfg() -> SubagentConfig:
    """A real SubagentConfig whose tmpfile the tool clones per subagent."""
    cfg = SubagentConfig(
        provider_name="vendor-a-pro",
        base_url=_PROXY_BASE_URL,
        api_key="sk-parent-key",
        auth_token="sk-parent-token",
        model_env={"ANTHROPIC_MODEL": "Vendor A-M3"},
        hook_scripts=[Path("/abs/pre_tool_use.sh")],
    )
    try:
        path = cfg.write_tmp_settings()
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"/tmp is not writable in this environment: {exc}")
    try:
        yield cfg
    finally:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _make_mock_process() -> MagicMock:
    """A MagicMock that satisfies the ``_run_claude_interactive`` read loop."""
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"type": "result", "result": "ok", "is_error": False}) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    return mock_proc


def _install_scene_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route scene ``execution`` at a single fake CC Switch row."""
    monkeypatch.setattr(
        "provider_routing.resolve_provider_chain",
        lambda scene: ["Vendor A Pro"],
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_check_provider_availability",
        staticmethod(lambda provider_name: (True, dict(_ROUTED_ROW))),
    )


def _settings_payload(mock_popen: MagicMock) -> dict:
    """Read the on-disk settings file the cmd points at."""
    cmd = mock_popen.call_args[0][0]
    assert "--settings" in cmd, f"cmd missing '--settings': {cmd!r}"
    return json.loads(
        Path(cmd[cmd.index("--settings") + 1]).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Ordering contract
# ---------------------------------------------------------------------------


class TestSettingsFileWriteOrder:
    @pytest.fixture(autouse=True)
    def _no_redaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Read the settings file as *written*, not as redacted.

        Redaction (:mod:`utils.secret_files`) rewrites the credentials in
        that file once the child is reaped, so every read below would
        otherwise see ``<redacted>`` where this class asserts the routed
        value. The two contracts are separate: the redaction contract has
        its own suite (``test_secret_files.py``) and its own wiring test
        (``test_coding_tool_secret_redaction.py``).
        """
        monkeypatch.setattr("coding_tool.redact_all", lambda *a, **k: 0)

    def test_settings_file_env_matches_scene_routed_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
        subagent_cfg: SubagentConfig,
    ) -> None:
        """The file's env block carries the routed provider, not the proxy.

        ``monkeypatch.setenv`` plants the CC Switch proxy in the parent
        env — exactly the production condition. The file must NOT echo
        it: ``--settings`` outranks the process env, so echoing the
        proxy here is what silently rerouted every subagent call.
        """
        _install_scene_routing(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", _PROXY_BASE_URL)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-parent-key")

        with patch("coding_tool.subprocess.Popen") as mock_popen:
            mock_popen.return_value = _make_mock_process()
            tool = ClaudeCodingTool(
                provider_priority=[],
                settings=subagent_cfg.settings_file_path,
                scene="execution",
            )
            tool.query("test prompt")

        _, kwargs = mock_popen.call_args
        assert kwargs["env"]["ANTHROPIC_BASE_URL"] == _ROUTED_BASE_URL, (
            "process env was not routed to the scene provider"
        )

        payload = _settings_payload(mock_popen)
        file_env = payload.get("env", {})
        assert file_env.get("ANTHROPIC_BASE_URL") == _ROUTED_BASE_URL, (
            f"settings file ANTHROPIC_BASE_URL is "
            f"{file_env.get('ANTHROPIC_BASE_URL')!r}, expected the "
            f"scene-routed {_ROUTED_BASE_URL!r}. The write must happen "
            f"AFTER provider selection / scene routing mutate ``env`` — "
            f"``claude --settings`` env outranks the process env, so a "
            f"file written earlier pins the parent proxy and every "
            f"subagent call bypasses the scene router."
        )
        assert file_env.get("ANTHROPIC_API_KEY") == "sk-scene-routed-key"
        assert file_env.get("ANTHROPIC_AUTH_TOKEN") == "sk-scene-routed-key"
        assert file_env.get("ANTHROPIC_MODEL") == "scene-default-model"

    def test_settings_file_written_after_self_overrides(
        self,
        monkeypatch: pytest.MonkeyPatch,
        subagent_cfg: SubagentConfig,
    ) -> None:
        """An explicit ``base_url`` override also reaches the file.

        ``ClaudeCodingTool(base_url=...)`` is the LAST-RESORT writer:
        since 2026-09-14 it applies only when no provider was resolved
        (the provider mapping is the single source of truth — see
        ``TestProviderMappingIsSingleSourceOfTruth`` in
        ``test_coding_tool_scene_routing.py``). The sentinel priority
        list below forces exactly that path, and the settings file must
        still reflect the override — same ordering rule.
        """
        monkeypatch.setenv("ANTHROPIC_BASE_URL", _PROXY_BASE_URL)

        explicit = "https://explicit-override.example.invalid/anthropic"
        with patch("coding_tool.subprocess.Popen") as mock_popen:
            mock_popen.return_value = _make_mock_process()
            tool = ClaudeCodingTool(
                base_url=explicit,
                api_key="sk-explicit",
                auth_token="sk-explicit",
                # Non-empty on purpose: ``[]`` is falsy and falls back to
                # the constructor's hardcoded default pair, which really
                # does resolve a provider (and would then, correctly,
                # outrank the override under test).
                provider_priority=["__no_such_provider__"],
                settings=subagent_cfg.settings_file_path,
            )
            tool.query("test prompt")

        file_env = _settings_payload(mock_popen).get("env", {})
        assert file_env.get("ANTHROPIC_BASE_URL") == explicit, (
            f"settings file ANTHROPIC_BASE_URL is "
            f"{file_env.get('ANTHROPIC_BASE_URL')!r}, expected the "
            f"explicit override {explicit!r}"
        )
        assert file_env.get("ANTHROPIC_API_KEY") == "sk-explicit"
        assert file_env.get("ANTHROPIC_AUTH_TOKEN") == "sk-explicit"
