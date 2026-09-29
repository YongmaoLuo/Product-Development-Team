"""
test_agent_wiring.py — integration tests for agent.py SubagentConfig wiring.

These tests pin the integration contract introduced by decisions 2/3/4
(subagent settings, hooks, and the explicit removal of any
``atexit.register``-based cleanup of the settings tmpfile). They do
NOT make real LLM calls — ClaudeCodingTool and AutonomousAgent are
patched so we can observe how the agent.py entry point wires the
SubagentConfig into the coding tool.

The three tests correspond to the three TDD acceptance bullets in
the upstream task spec:

  * test_agent_uses_subagent_config — agent.py builds a
    SubagentConfig and passes ``settings=<tmpfile>`` + ``hook_stdin=``
    into ClaudeCodingTool. Pinned by the integration contract that
    the Claude subprocess must NEVER fall back to the parent process's
    CC Switch proxy (decision 2/3) — the tmpfile is the only source
    of truth for ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / model
    overrides.

  * test_agent_no_atexit_cleanup — agent.py must not call
    ``atexit.register`` to clean up the tmp settings file. Decision
    3 explicitly removed atexit-based cleanup: the tmpfile is
    intentionally left on disk so that asynchronous child processes
    can still read it after the parent exits, and so post-mortem
    debugging has a trail.

  * test_agent_settings_path_in_logger — when an ExecutionLogger is
    passed in, write_tmp_settings() emits a structured
    ``subagent_settings_file_written`` event into execution.log
    (with path/uuid/fields). This is the only correlation trail a
    debug session has for linking a settings-file path back to the
    tmpfile uuid the SDK was started with.

Test isolation: every test creates its own tmp project_dir, its own
plan_id, and its own ExecutionLogger pointing at a private plans_dir.
We redirect ``$HOME`` to a private tmpdir containing a fake
``~/.cc-switch/cc-switch.db`` with three providers. This means the ``_load_provider_info`` path is exercised
end-to-end — we never patch it out.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Make ``agent.py`` importable. pytest.ini already adds ``backend/`` to
# PYTHONPATH, but the redundant insert below makes this test runnable
# in isolation (e.g. ``python -m pytest tests/integration/test_agent_wiring.py``).
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scratch_project_dir() -> Path:
    """Fresh tmp directory for the agent's project_dir."""
    d = Path(tempfile.mkdtemp(prefix="agent_wiring_proj_"))
    return d


def _scratch_plans_dir() -> Path:
    """Fresh tmp directory used as plans_dir for ExecutionLogger."""
    d = Path(tempfile.mkdtemp(prefix="agent_wiring_plans_"))
    return d


# Provider fixture used by every test. Two providers so the first-match-wins
# semantic of ``_load_provider_info`` is exercised end-to-end.
# Providers are named the way CC Switch names them — that string is the
# lookup key.
_FAKE_PROVIDERS = {
    "Vendor A Pro": {
        "base_url": "https://api.vendor-a.example/anthropic",
        "api_key": "sk-cp-test-fake-key",
    },
    "Vendor B Pro": {
        "base_url": "https://api.vendor-b.example/anthropic",
        "api_key": "a8ff.fake-vendor-b-key.1234567890",
    },
    "Vendor C App": {
        "base_url": "https://api.vendor-c.com/coding/",
        "api_key": "a8ff.fake-vendor-c-key.9876543210",
    },
}


def _install_fake_cc_switch_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Build a private ``~/.cc-switch/cc-switch.db`` and redirect
    ``$HOME`` to it for the test.

    The ``_load_provider_info`` helper in agent.py reads
    ``Path.home() / ".cc-switch" / "cc-switch.db"`` — by pointing
    ``$HOME`` at a private tmpdir we exercise the real loading path
    end-to-end without touching the developer's real ``~/.cc-switch``
    directory.
    """
    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    # Production CC Switch schema: the row's ``name`` is the provider's
    # identity, and the endpoint + credential live in the
    # ``settings_config.env`` block — the same shape ``~/.claude/settings.json``
    # uses.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS providers ("
        "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
        ")"
    )
    for i, (provider_name, cfg) in enumerate(_FAKE_PROVIDERS.items()):
        conn.execute(
            "INSERT INTO providers (id, name, settings_config) VALUES (?, ?, ?)",
            (
                f"row-{i}",
                provider_name,
                json.dumps({
                    "env": {
                        "ANTHROPIC_BASE_URL": cfg["base_url"],
                        "ANTHROPIC_AUTH_TOKEN": cfg["api_key"],
                    }
                }),
            ),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(fake_home))
    return db_path


def _cleanup_cfg_calls(cfg_calls: list) -> None:
    """Best-effort cleanup of /tmp/subagent_settings_*.json files written
    by the real SubagentConfig.write_tmp_settings() call during the test.
    """
    seen: set = set()
    for kwargs in cfg_calls:
        inst = kwargs.get("instance")
        if inst is None:
            continue
        sfp = getattr(inst, "settings_file_path", None)
        if sfp is not None and sfp not in seen:
            seen.add(sfp)
            try:
                if Path(sfp).exists():
                    Path(sfp).unlink()
            except OSError:
                pass


def _patch_heavy_io(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch out the agent's heavy machinery so no real LLM call runs.

    Returns a dict of fake / recording objects so individual tests can
    assert on what the entry point did.
    """
    # SubagentConfig: track construction so test_agent_uses_subagent_config
    # can assert on the call. We also stash the constructed instance so
    # tests can clean up the /tmp settings file it writes.
    from subagent_config import SubagentConfig

    cfg_calls: list = []
    real_init = SubagentConfig.__init__

    def spy_init(self, *args, **kwargs):
        cfg_calls.append({**kwargs, "instance": self})
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(SubagentConfig, "__init__", spy_init)

    # ClaudeCodingTool: track construction so we can assert on kwargs.
    from coding_tool import ClaudeCodingTool

    tool_calls: list = []
    real_tool_init = ClaudeCodingTool.__init__

    def spy_tool_init(self, *args, **kwargs):
        tool_calls.append(kwargs)
        return real_tool_init(self, *args, **kwargs)

    monkeypatch.setattr(ClaudeCodingTool, "__init__", spy_tool_init)

    # AutonomousAgent.run / plan: no-op so the test does not actually
    # start an execution loop. We still want AutonomousAgent.__init__
    # to run so the wiring inside autonomous_coding is exercised.
    from agent import AutonomousAgent

    monkeypatch.setattr(AutonomousAgent, "run", lambda self, *a, **kw: None)
    monkeypatch.setattr(AutonomousAgent, "plan", lambda self: None)

    # Also patch __init__ so it doesn't try to open a git repo / load
    # domain knowledge / create a worktree. The test cares about the
    # SubagentConfig + ClaudeCodingTool wiring inside
    # ``autonomous_coding``, not about AutonomousAgent's real
    # construction. The original __init__ is preserved on the class
    # object (not the instance) so other tests are not affected.
    real_agent_init = AutonomousAgent.__init__

    def stub_agent_init(self, *args, **kwargs):
        # Bind the few attributes that downstream call sites in
        # autonomous_coding() read after construction.
        self.coding_tool = kwargs.get("coding_tool")
        self.config = kwargs.get("config")
        self.logger = kwargs.get("logger")
        self.project_dir = kwargs.get("project_dir")
        self.task_manager = None
        self.git_manager = None

    monkeypatch.setattr(AutonomousAgent, "__init__", stub_agent_init)
    # Keep a reference so the closure can be re-applied cleanly if a
    # test wants to restore the real init. (Not currently used.)
    _ = real_agent_init

    return {"cfg_calls": cfg_calls, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# test_agent_uses_subagent_config
# ---------------------------------------------------------------------------


def test_agent_uses_subagent_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """agent.py builds a SubagentConfig and passes settings + hook_stdin.

    Pins:
      * SubagentConfig.__init__ is called with provider_name, base_url,
        api_key, auth_token, hook_scripts (2026-09-13: no model_map —
        model env comes from the CC Switch provider row).
      * The ClaudeCodingTool constructor receives ``settings=`` (a Path
        pointing to a JSON file on disk) and ``hook_stdin=`` (a dict
        containing task_type + task_summary).
      * The settings file on disk contains the ``env`` and ``hooks``
        keys from the SubagentConfig schema, and the env block has
        the endpoint+credential ANTHROPIC_* entries decision 2 mandates.
      * The hook entries on disk reference the pre_tool_use.sh and
        post_tool_use.sh scripts (decision 4 wiring).
    """
    project_dir = _scratch_project_dir()
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    record = _patch_heavy_io(monkeypatch)
    # Pin the provider chain to the names in the fake DB so the test
    # is independent of the production ``provider-order.json`` and of
    # any time-of-day rule: without this pin the chain comes from
    # ``load_fallback_order()`` and the first provider would be
    # whichever row the optimizer last ordered first.
    monkeypatch.setenv(
        "PDT_PROVIDER_PRIORITY",
        "Vendor A Pro,Vendor B Pro,Vendor C App",
    )

    from agent import autonomous_coding

    try:
        autonomous_coding(
            requirement="test wiring",
            project_dir=str(project_dir),
            recover=False,
            max_tasks=None,
            config_name="coding",
            tool="claude",
            logger=None,
        )

        # 1. SubagentConfig was instantiated.
        assert record["cfg_calls"], "SubagentConfig was not instantiated"
        cfg_kwargs = record["cfg_calls"][0]
        assert cfg_kwargs.get("provider_name") == "Vendor A Pro"
        assert cfg_kwargs.get("base_url") == "https://api.vendor-a.example/anthropic"
        assert cfg_kwargs.get("api_key") == "sk-cp-test-fake-key"
        assert cfg_kwargs.get("auth_token") == "sk-cp-test-fake-key"
        # 2026-09-13: no tiered model_map is forwarded — model env comes
        # from the CC Switch provider row via the dispatch walk.
        assert not cfg_kwargs.get("model_map"), (
            "model_map must NOT be forwarded (removed 2026-09-13)"
        )

        # hook_scripts: pre + post paths present.
        hook_scripts = cfg_kwargs.get("hook_scripts", [])
        assert any("pre_tool_use" in str(p) for p in hook_scripts), (
            f"hook_scripts missing pre_tool_use: {hook_scripts}"
        )
        assert any("post_tool_use" in str(p) for p in hook_scripts), (
            f"hook_scripts missing post_tool_use: {hook_scripts}"
        )

        # 2. ClaudeCodingTool was constructed with settings + hook_stdin.
        assert record["tool_calls"], "ClaudeCodingTool was not constructed"
        tool_kwargs = record["tool_calls"][0]
        assert "settings" in tool_kwargs, "ClaudeCodingTool needs settings= kwarg"
        settings_path = tool_kwargs["settings"]
        assert settings_path is not None, "settings kwarg is None"
        assert isinstance(settings_path, Path), (
            f"settings should be a Path, got {type(settings_path).__name__}"
        )
        assert settings_path.exists(), f"settings tmpfile missing: {settings_path}"

        hook_stdin = tool_kwargs.get("hook_stdin")
        assert hook_stdin, "hook_stdin must not be empty"
        assert "task_type" in hook_stdin
        assert "task_summary" in hook_stdin
        assert hook_stdin["task_summary"] == "test wiring"

        # 3. Settings file on disk has env + hooks blocks.
        payload = json.loads(settings_path.read_text())
        assert "env" in payload, "settings.json missing env block"
        assert "hooks" in payload, "settings.json missing hooks block"

        anthropic_keys = [k for k in payload["env"] if k.startswith("ANTHROPIC_")]
        assert {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}.issubset(
            set(anthropic_keys)
        ), (
            f"settings.json env must carry the endpoint+credential keys, "
            f"got {anthropic_keys}"
        )
        assert payload["env"]["ANTHROPIC_BASE_URL"] == "https://api.vendor-a.example/anthropic"
        assert payload["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-cp-test-fake-key"

        # 4. Hook entries reference the pre + post scripts.
        hooks = payload["hooks"]
        if "PreToolUse" in hooks:
            pre_cmds = []
            for entry in hooks["PreToolUse"]:
                for h in entry.get("hooks", []):
                    pre_cmds.append(h.get("command", ""))
            assert any("pre_tool_use" in c for c in pre_cmds), (
                f"PreToolUse entries do not reference pre_tool_use.sh: {pre_cmds}"
            )
        if "PostToolUse" in hooks:
            post_cmds = []
            for entry in hooks["PostToolUse"]:
                for h in entry.get("hooks", []):
                    post_cmds.append(h.get("command", ""))
            assert any("post_tool_use" in c for c in post_cmds), (
                f"PostToolUse entries do not reference post_tool_use.sh: {post_cmds}"
            )
    finally:
        _cleanup_cfg_calls(record["cfg_calls"])


# ---------------------------------------------------------------------------
# test_agent_no_atexit_cleanup
# ---------------------------------------------------------------------------


def test_agent_no_atexit_cleanup():
    """agent.py must not register atexit handlers to clean up the tmpfile.

    Decision 3 explicitly removed atexit-based cleanup: the tmpfile is
    intentionally kept on disk so asynchronous child processes can
    read it after the parent exits, and so post-mortem debugging has
    a trail. This test pins that contract by scanning the agent.py
    source for the atexit symbols.
    """
    agent_path = _BACKEND_DIR / "agent.py"
    source = agent_path.read_text()

    # No top-level import.
    assert "import atexit" not in source, (
        "agent.py must not import atexit (decision 3 removed atexit-based "
        "cleanup of the subagent settings tmpfile)"
    )
    # No atexit.register calls anywhere in the module.
    assert "atexit.register" not in source, (
        "agent.py must not call atexit.register() — the subagent "
        "settings tmpfile is intentionally not cleaned up at process exit"
    )
    # No from-atexit-import form either.
    assert "from atexit" not in source, (
        "agent.py must not import any name from the atexit module"
    )


# ---------------------------------------------------------------------------
# test_agent_settings_path_in_logger
# ---------------------------------------------------------------------------


def test_agent_settings_path_in_logger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """execution.log must contain a subagent_settings_file_written event.

    When the caller passes a real ExecutionLogger (via cli.py's
    ``get_logger()``), write_tmp_settings() must record the
    correlation event (path/uuid/fields) into the plan's
    execution.log. This is the only trail a debug session has for
    mapping a settings tmpfile path back to a tmpfile uuid.
    """
    project_dir = _scratch_project_dir()
    plans_dir = _scratch_plans_dir()
    plan_id = "test-wiring-001"

    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    record = _patch_heavy_io(monkeypatch)
    # Pin the provider chain to the names in the fake DB so the test
    # is independent of the production ``provider-order.json``.
    monkeypatch.setenv(
        "PDT_PROVIDER_PRIORITY",
        "Vendor A Pro,Vendor B Pro,Vendor C App",
    )

    from execution_logger import ExecutionLogger
    from agent import autonomous_coding

    logger = ExecutionLogger(plan_id=plan_id, plans_dir=plans_dir)

    try:
        autonomous_coding(
            requirement="test settings path in logger",
            project_dir=str(project_dir),
            recover=False,
            max_tasks=None,
            config_name="coding",
            tool="claude",
            logger=logger,
        )

        log_file = plans_dir / plan_id / "execution.log"
        assert log_file.exists(), f"execution.log not created at {log_file}"

        # Scan the log for the correlation event. Each line is a JSON
        # object; we want an entry with event == 'subagent_settings_file_written'.
        matching = []
        for line in log_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event") == "subagent_settings_file_written":
                matching.append(entry)

        assert matching, (
            "execution.log does not contain a subagent_settings_file_written "
            f"event. Full log:\n{log_file.read_text()}"
        )

        # The event must carry the path/uuid/fields correlation payload.
        entry = matching[-1]
        data = entry.get("data", {})
        assert "path" in data, f"event data missing path: {data}"
        assert "uuid" in data, f"event data missing uuid: {data}"
        # Endpoint + credentials only. A model field appears solely when one
        # actually resolves (caller ``model_env`` or the provider row); this
        # fixture resolves none, so the field is absent on purpose and the
        # Claude CLI falls back to the local config.
        assert data["fields"] == 3, (
            f"event data.fields must be 3 (endpoint + credentials; no model "
            f"resolved for this fixture), got {data.get('fields')}"
        )
        settings_path = Path(data["path"])
        assert settings_path.exists(), (
            f"event references missing tmpfile: {settings_path}"
        )

        # The file referenced by the log entry must contain the same
        # endpoint/credential env block that test_agent_uses_subagent_config
        # asserts.
        payload = json.loads(settings_path.read_text())
        anthropic_keys = [k for k in payload["env"] if k.startswith("ANTHROPIC_")]
        assert {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}.issubset(
            set(anthropic_keys)
        )
    finally:
        _cleanup_cfg_calls(record["cfg_calls"])


# ---------------------------------------------------------------------------
# Consumer-layer provider wiring tests
# ---------------------------------------------------------------------------
