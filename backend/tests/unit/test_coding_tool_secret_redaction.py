"""Redaction is *wired* into the dispatch path (2026-09-27).

:mod:`test_secret_files` covers what redaction does; this file covers who
calls it, when, and on which paths — the part that a unit test of the
helper cannot see.

Two contracts, and the second one is a regression guard:

1. the settings file handed to the child via ``--settings`` has its
   credentials replaced once the child is reaped, with the non-secret
   routing config left intact for post-mortem debugging;
2. the *caller's* settings file — ``self.settings`` — is **not** touched.
   ``agent.py`` reassigns it once per task, but a task can make more than
   one ``query()`` call, and the second call re-reads it. Redacting it
   hands the next dispatch ``<redacted>`` as its API key and silently
   breaks provider routing. That mistake was made once, on 2026-09-27,
   and it is why the production call site passes a one-element list.

Contract 1 is asserted per **exit path**, not once. A dispatch ends
cleanly, with a provider error, or before the child starts, and the
credential has to be gone in all three: the redaction only ever covered
the first, which is why the two failure exits below each carry a test.
The exits are driven through ``_run_claude_interactive`` rather than
through ``query()`` so the retry wrapper above cannot rotate providers
and re-enter the dispatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import ApiError, ClaudeCodingTool  # noqa: E402
from subagent_config import SubagentConfig  # noqa: E402
from utils.secret_files import REDACTED  # noqa: E402


_PROXY_BASE_URL = "https://proxy.invalid/anthropic"
_ROUTED_BASE_URL = "https://scene-routed.invalid/anthropic"
_ROUTED_ROW = {
    "base_url": _ROUTED_BASE_URL,
    "api_key": "sk-scene-routed-key",
    "models": {"default": "scene-default-model"},
}


@pytest.fixture
def subagent_cfg() -> SubagentConfig:
    """A real config whose tmpfile stands in for the caller's settings."""
    cfg = SubagentConfig(
        provider_name="vendor-a-pro",
        base_url=_PROXY_BASE_URL,
        api_key="sk-parent-key",
        auth_token="sk-parent-token",
        model_env={"ANTHROPIC_MODEL": "Vendor A-M3"},
    )
    path = cfg.write_tmp_settings()
    try:
        yield cfg
    finally:
        import shutil
        shutil.rmtree(Path(path).parent, ignore_errors=True)


def _make_mock_process() -> MagicMock:
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
    monkeypatch.setattr(
        "provider_routing.resolve_provider_chain",
        lambda scene: ["Vendor A Pro"],
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_check_provider_availability",
        staticmethod(lambda provider_name: (True, dict(_ROUTED_ROW))),
    )


def _settings_path_from_cmd(mock_popen: MagicMock) -> Path:
    cmd = mock_popen.call_args[0][0]
    assert "--settings" in cmd, f"cmd missing '--settings': {cmd!r}"
    return Path(cmd[cmd.index("--settings") + 1])


def test_dispatch_redacts_the_settings_file_it_hands_the_child(
    monkeypatch: pytest.MonkeyPatch, subagent_cfg: SubagentConfig,
) -> None:
    """After the child is reaped, the dispatched file holds no credential."""
    _install_scene_routing(monkeypatch)

    with patch("coding_tool.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_process()
        tool = ClaudeCodingTool(
            provider_priority=[],
            settings=subagent_cfg.settings_file_path,
            scene="execution",
        )
        tool.query("test prompt")

    written = json.loads(
        _settings_path_from_cmd(mock_popen).read_text(encoding="utf-8")
    )
    env = written["env"]

    assert env["ANTHROPIC_API_KEY"] == REDACTED, (
        "the dispatched settings file still holds the provider key after "
        "the child exited; this is the file the 2026-09-27 leak measured"
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == REDACTED
    # Non-secrets survive — they are what an operator reads when
    # debugging a routing mistake.
    assert env["ANTHROPIC_BASE_URL"].startswith("https://")
    assert env["ANTHROPIC_MODEL"]


def _make_mock_process_error_result() -> MagicMock:
    """A child whose last event is an error ``result``.

    That is the shape the CLI emits for a provider-side failure, and it
    is what the read loop turns into the ``ApiError`` raised from inside
    the loop body.
    """
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({
            "type": "result",
            "result": "provider rejected the request",
            "is_error": True,
            "api_error_status": "402",
        }) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    return mock_proc


def test_dispatch_redacts_when_the_read_loop_raises_api_error(
    monkeypatch: pytest.MonkeyPatch, subagent_cfg: SubagentConfig,
) -> None:
    """The provider-error exit must drop the credential too.

    ``ApiError`` is not a ``ValueError``, so it escapes the read loop's
    ``except`` clause and leaves ``_run_claude_interactive`` by the raise
    path. When the redaction sat *after* the read-loop statement rather
    than inside its ``finally``, that path skipped it — which made the
    ordinary provider failure the one exit that kept a live key on disk.

    ``_run_claude_interactive`` is driven directly rather than through
    ``query()``: the retry wrapper above catches ``ApiError`` and rotates
    providers, which would re-enter the dispatch and hide the path under
    test.
    """
    _install_scene_routing(monkeypatch)

    with patch("coding_tool.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_process_error_result()
        tool = ClaudeCodingTool(
            provider_priority=[],
            settings=subagent_cfg.settings_file_path,
            scene="execution",
        )
        with pytest.raises(ApiError):
            tool._run_claude_interactive("test prompt")

    written = json.loads(
        _settings_path_from_cmd(mock_popen).read_text(encoding="utf-8")
    )
    env = written["env"]
    assert env["ANTHROPIC_API_KEY"] == REDACTED, (
        "a dispatch that ended in ApiError kept its provider key; this is "
        "the exit path that carried control past the redaction call"
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == REDACTED
    assert env["ANTHROPIC_BASE_URL"].startswith("https://"), (
        "the routing config must survive — it is what an operator reads "
        "when debugging a provider failure"
    )


def test_dispatch_redacts_when_the_child_cannot_be_spawned(
    monkeypatch: pytest.MonkeyPatch, subagent_cfg: SubagentConfig,
) -> None:
    """A raising ``Popen`` leaves the settings file written but unused.

    The write precedes the spawn, so the credential is already on disk
    when the spawn fails; nothing after the setup region runs, which is
    why the setup region's own handler has to drop it.
    """
    _install_scene_routing(monkeypatch)

    with patch("coding_tool.subprocess.Popen", side_effect=OSError("no fork")) as mock_popen:
        tool = ClaudeCodingTool(
            provider_priority=[],
            settings=subagent_cfg.settings_file_path,
            scene="execution",
        )
        with pytest.raises(OSError):
            tool._run_claude_interactive("test prompt")

    written = json.loads(
        _settings_path_from_cmd(mock_popen).read_text(encoding="utf-8")
    )
    env = written["env"]
    assert env["ANTHROPIC_API_KEY"] == REDACTED, (
        "a failed spawn left the routed provider key on disk; the child "
        "never read the file, so nothing was going to revisit it"
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == REDACTED


def test_dispatch_does_not_touch_the_callers_settings_file(
    monkeypatch: pytest.MonkeyPatch, subagent_cfg: SubagentConfig,
) -> None:
    """``self.settings`` is an input a later dispatch re-reads — leave it.

    The file is in a ``pdt-subagent-*`` directory, so ``is_managed`` is
    True for it: had the call site passed it to ``redact_all``, it would
    have been rewritten. That is exactly the 2026-09-27 mistake — a second
    ``query()`` then read ``<redacted>`` as its API key.
    """
    _install_scene_routing(monkeypatch)
    caller_settings = Path(subagent_cfg.settings_file_path)

    with patch("coding_tool.subprocess.Popen") as mock_popen:
        # A fresh mock per call: the stdout iterator is consumed once,
        # so a shared instance makes the second dispatch look like an
        # empty response and never reaches the redaction point.
        mock_popen.side_effect = lambda *a, **k: _make_mock_process()
        tool = ClaudeCodingTool(
            provider_priority=[],
            settings=caller_settings,
            scene="execution",
        )
        tool.query("first prompt")
        tool.query("second prompt")
        assert mock_popen.call_count == 2

    surviving = json.loads(caller_settings.read_text(encoding="utf-8"))
    env = surviving["env"]
    # The caller's file keeps the *caller's* values — coding_tool clones
    # it and writes the routed values into its own file, it does not
    # rewrite the input. So the assertion is "unchanged", and the values
    # that prove it are the ones ``subagent_cfg`` wrote.
    expected = {
        "ANTHROPIC_API_KEY": subagent_cfg.api_key,
        "ANTHROPIC_AUTH_TOKEN": subagent_cfg.auth_token,
    }
    for key, want in expected.items():
        assert env[key] != REDACTED, (
            f"the caller's settings file was redacted ({key}={env[key]!r}). "
            f"A second dispatch re-reads this file, so the next subagent "
            f"would run with '{REDACTED}' as its API key"
        )
        assert env[key] == want, (
            f"{key} is {env[key]!r}, expected the caller's own {want!r}; "
            f"the input file was rewritten"
        )
