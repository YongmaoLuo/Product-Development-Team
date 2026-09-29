"""
test_subagent_instantiation.py — integration tests pinning the real
SubagentConfig instantiation path inside ``agent.autonomous_coding()``.

This file is the regression-guard for task 7-1: the previous test
(``test_agent_wiring.py``) used ``monkeypatch.setattr(AutonomousAgent,
"__init__", stub_agent_init)`` to avoid the cost of constructing a
real ``AutonomousAgent``. That shortcut bypassed the
``SubagentConfig`` field contracts — the test only asserted that
``write_tmp_settings`` was called, never that the *fields* on the
instance were read from ``~/.cc-switch/cc-switch.db`` and
populated correctly. A regression that hard-coded ``api_key=""`` in
the agent (the very class of bug the dataclass refactor was meant
to surface) would have slipped past the existing tests.

The 7 tests below close that gap. They do NOT:

  * patch ``AutonomousAgent.__init__`` — the constructor runs for
    real, on a real ``tmp_path`` that has been ``git init``'d;
  * mock ``SubagentConfig`` — a wrapper ``__init__`` is installed
    that captures the kwargs and **then calls the real
    ``SubagentConfig.__init__``**, so a real dataclass instance
    exists for every assertion.

The 7 TDD acceptance bullets (one-to-one with the spec):

  * test_autonomous_coding_constructs_subagent_config
       — spy SubagentConfig → call_count == 1
  * test_subagent_config_provider_name_valid
       — provider_name ∈ {'vendor-a-pro', 'vendor-b'}
  * test_subagent_config_base_url_not_localhost
       — base_url excludes 127.0.0.1 / localhost
  * test_subagent_config_api_key_nonempty
       — api_key length > 10
  * test_subagent_config_no_model_map_forwarded
       — no tiered model_map kwarg (removed 2026-09-13)
  * test_subagent_config_hook_scripts_has_2_hooks
       — hook_scripts length == 2, contains pre+post
  * test_subagent_config_task_type_set
       — task_type is a non-empty string

Test isolation strategy:
  * ``tmp_path`` is the project_dir — we run real ``git init`` on it
    so ``GitManager(repo, search_parent_directories=True)`` finds a
    repository without falling back to the parent's .git/.
  * We redirect ``HOME`` to a private tmpdir that contains a fake
    ``~/.cc-switch/cc-switch.db`` with two providers (vendor-a-pro +
    vendor-b). This means the ``_load_provider_info`` path is exercised
    end-to-end — we never patch it out.
  * ``AutonomousAgent.plan`` and ``AutonomousAgent.run`` are
    stubbed to no-ops so no LLM is called. The ``__init__`` is
    **not** touched (this is the regression guard).
  * ``SubagentConfig.__init__`` is wrapped with a spy that
    records the kwargs and forwards to the real constructor. A
    tmp settings file is written by the real code; we clean it
    up in a finally block per test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Make ``agent.py`` and ``subagent_config.py`` importable in isolation.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Provider fixture used by every test. The two providers are picked to
# match the spec's ``provider_name ∈ {'vendor-a-pro', 'vendor-b'}`` set
# and to make it easy to assert the "first match wins" semantic of
# ``_load_provider_info`` (the spy will see ``vendor-a-pro`` because
# that's the first entry in the default ``provider_priority``).
_FAKE_PROVIDERS = {
    "vendor-a-pro": {
        "base_url": "https://api.vendor-a.example/anthropic",
        "api_key": "sk-cp-fake-vendor-a-key-1234567890",
    },
    "vendor-b": {
        "base_url": "https://api.vendor-b.example/anthropic",
        "api_key": "a8ff.fake-vendor-b-key.1234567890",
    },
}


def _git_init(project_dir: Path) -> None:
    """Run a real ``git init`` so GitManager(search_parent_directories=True)
    finds the repo at the leaf — not a sibling checkout
    ancestor repo.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    # Some sandboxes ship git older than 2.28 (no --initial-branch).
    # Fall back to the classic form if the previous command failed.
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    # Configure a local user so any later commit attempt does not
    # bail out with "Author identity unknown".
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Subagent Instantiation Test"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )


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
    import sqlite3

    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provider_configs ("
        "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT"
        ")"
    )
    for provider_id, cfg in _FAKE_PROVIDERS.items():
        extra = json.dumps({"ANTHROPIC_AUTH_TOKEN": cfg["api_key"]})
        conn.execute(
            "INSERT INTO provider_configs (id, url, model, extra_params) "
            "VALUES (?, ?, ?, ?)",
            (provider_id, cfg["base_url"], "fake-model", extra),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(fake_home))
    return db_path


def _install_subagent_spy(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Install a wrapper around ``SubagentConfig.__init__`` that
    captures the kwargs into a list and then calls the real
    constructor. Returns the list the spy appends to.

    This is a spy (capture + delegate), NOT a Mock (replace). The
    real ``SubagentConfig`` instance is fully populated after the
    call, so the test can read any field directly off the captured
    instance — but the spec asks us to assert on the kwargs
    dictionary, so we do that here.
    """
    from subagent_config import SubagentConfig

    captured: List[Dict[str, Any]] = []
    real_init = SubagentConfig.__init__

    def spy_init(self, *args, **kwargs):
        captured.append(
            {
                "self_id": id(self),
                "args": args,
                "kwargs": dict(kwargs),
                # Also stash the constructed instance so tests that want
                # to read off the real object can do so. This is the
                # only "extra" thing the spy does — it does not
                # replace the real __init__.
                "instance": self,
            }
        )
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(SubagentConfig, "__init__", spy_init)
    return captured


def _stub_plan_and_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the *methods* ``plan`` and ``run`` on
    ``AutonomousAgent`` so we never call the real LLM. The
    ``__init__`` is left untouched — that is the whole point of
    this test file. Real ``GitManager`` / ``TaskManager`` /
    ``RollbackManager`` etc. all run.
    """
    from agent import AutonomousAgent

    monkeypatch.setattr(AutonomousAgent, "plan", lambda self: None)
    monkeypatch.setattr(AutonomousAgent, "run", lambda self, *a, **kw: None)


def _cleanup_tmp_settings_files() -> None:
    """Best-effort cleanup of the /tmp/subagent_settings_*.json files
    the agent creates via write_tmp_settings().

    The agent does not clean up after itself (decision 3 — the
    tmpfile is intentionally kept on disk for post-mortem
    debugging). For unit-test isolation we sweep the files we own
    by their uuid prefix recorded per-test.
    """
    # Per-test uuid prefix avoids the rare race where two tests
    # pick up each other's leftovers. The test that called
    # ``write_tmp_settings`` recorded the path on the spy-captured
    # instance; we cleanup based on that. Falls back to a no-op if
    # no such file exists.
    pass  # cleanup is done inline by each test via the path recorded on cfg_calls


def _run_autonomous_coding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requirement: str = "instantiate subagent test",
) -> List[Dict[str, Any]]:
    """End-to-end: real git init + real provider map + real
    SubagentConfig construction + no-LLM plan/run stubs. Returns
    the spy capture list (one entry per SubagentConfig() call).
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _stub_plan_and_run(monkeypatch)
    captured = _install_subagent_spy(monkeypatch)
    # Pin the provider chain to the two IDs in the fake DB so the test
    # is independent of the production ``provider-order.json`` (which
    # historically listed ``vendor-a``, not ``vendor-a-pro``) and of
    # the time-of-day vendor-b peak-hour degradation rule. Without this
    # pin, when vendor-b is in peak hour (14:00-18:00 Beijing time) the
    # chain collapses to a single entry and ``_load_provider_info``
    # returns an empty dict, which used to make ``SubagentConfig``
    # raise from the tiered model_map lookup (removed 2026-09-13 —
    # model management is delegated to CC Switch).
    # Pinned: 2026-06-17.
    monkeypatch.setenv("PDT_PROVIDER_PRIORITY", "vendor-a-pro,vendor-b")

    from agent import autonomous_coding

    autonomous_coding(
        requirement=requirement,
        project_dir=str(project_dir),
        recover=False,
        max_tasks=None,
        config_name="coding",
        tool="claude",
        logger=None,
    )
    return captured


def _cleanup_captured(captured: List[Dict[str, Any]]) -> None:
    """Delete any /tmp/subagent_settings_*.json files written by the
    real SubagentConfig.write_tmp_settings() call during the test.
    """
    seen_paths: set = set()
    for entry in captured:
        inst = entry.get("instance")
        if inst is None:
            continue
        sfp = getattr(inst, "settings_file_path", None)
        if sfp is not None and sfp not in seen_paths:
            seen_paths.add(sfp)
            try:
                if sfp.exists():
                    sfp.unlink()
            except OSError:
                # Best-effort; the file is in /tmp so OSError is
                # effectively impossible on macOS / Linux, but if
                # /tmp is on a read-only volume we want the test
                # to keep going so the assertion can still fire.
                pass


# ---------------------------------------------------------------------------
# 7 TDD tests
# ---------------------------------------------------------------------------


def test_autonomous_coding_constructs_subagent_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``autonomous_coding()`` instantiates exactly one SubagentConfig.

    The spy sits on ``SubagentConfig.__init__``; the test asserts the
    capture list has length 1. This is the regression guard for the
    case where a future refactor accidentally bypasses the
    SubagentConfig pathway (e.g. calls ``ClaudeCodingTool`` directly
    with the 7+ scattered kwargs the dataclass refactor was meant to
    consolidate).
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1, (
            f"expected SubagentConfig to be constructed exactly once, "
            f"got {len(captured)}: {captured}"
        )
    finally:
        _cleanup_captured(captured)


def test_subagent_config_provider_name_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spy's ``provider_name`` ∈ {'vendor-a-pro', 'vendor-b'}.

    Comes from the first provider in ``_load_provider_info``'s
    priority list whose ``base_url`` + ``api_key`` are both
    populated. With our fake map the first match is
    ``vendor-a-pro`` (the default priority order), but the
    spec accepts either.
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1
        provider_name = captured[0]["kwargs"].get("provider_name", "")
        assert provider_name in {"vendor-a-pro", "vendor-b"}, (
            f"provider_name {provider_name!r} not in "
            f"{{'vendor-a-pro', 'vendor-b'}}; full kwargs: {captured[0]['kwargs']}"
        )
    finally:
        _cleanup_captured(captured)


def test_subagent_config_base_url_not_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spy's ``base_url`` excludes ``127.0.0.1`` and ``localhost``.

    Decision 2/3 contract: the Claude subprocess must NEVER fall
    back to the parent process's CC Switch proxy
    (parent-proxy.invalid) silently. We pin that by reading the
    ``base_url`` off the spy kwargs and asserting the proxy loopback
    address is absent.
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1
        base_url = captured[0]["kwargs"].get("base_url", "")
        assert isinstance(base_url, str)
        assert base_url, (
            f"base_url is empty; the agent fell through to its "
            f"empty-string default. kwargs: {captured[0]['kwargs']}"
        )
        assert "127.0.0.1" not in base_url, (
            f"base_url contains 127.0.0.1 (CC Switch proxy leak): {base_url!r}"
        )
        assert "localhost" not in base_url, (
            f"base_url contains localhost (CC Switch proxy leak): {base_url!r}"
        )
    finally:
        _cleanup_captured(captured)


def test_subagent_config_api_key_nonempty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spy's ``api_key`` is a non-empty string longer than 10 chars.

    A real Anthropic-style key is at least a few dozen characters;
    the 10-char floor here is a soft sanity check that catches the
    "agent passed empty string" regression without being brittle to
    key format changes.
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1
        api_key = captured[0]["kwargs"].get("api_key", "")
        assert isinstance(api_key, str), (
            f"api_key must be a str, got {type(api_key).__name__}: {api_key!r}"
        )
        assert len(api_key) > 10, (
            f"api_key length {len(api_key)} <= 10 — agent likely "
            f"passed the empty-string default. kwargs: {captured[0]['kwargs']}"
        )
    finally:
        _cleanup_captured(captured)


def test_subagent_config_no_model_map_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spy's kwargs must NOT carry a tiered ``model_map`` (removed).

    2026-09-13 contract: model management is delegated ENTIRELY to CC
    Switch — the dispatch walk forwards the provider row's own
    ANTHROPIC_* env block verbatim, so the agent no longer constructs
    a tiered ``model_map`` for the SubagentConfig. A regression that
    re-introduces a non-empty ``model_map`` kwarg would silently
    re-enable tiered model binding inside the workflow; we catch
    that regression at the kwargs boundary.
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1
        model_map = captured[0]["kwargs"].get("model_map")
        assert not model_map, (
            f"model_map must be absent or empty (removed 2026-09-13), "
            f"got: {model_map!r}; kwargs: {captured[0]['kwargs']}"
        )
    finally:
        _cleanup_captured(captured)


def test_subagent_config_hook_scripts_has_2_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spy's ``hook_scripts`` has length 2 (pre + post).

    Decision 4: the post_tool_use hook is the metrics-injection
    target, the pre_tool_use hook is its symmetric counterpart.
    The agent builds the list as
    ``[pre_tool_use.sh, post_tool_use.sh]`` (line 874-877); a
    regression that drops one of them would silently disable the
    PostToolUse metrics pipeline.
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1
        hook_scripts = captured[0]["kwargs"].get("hook_scripts", [])
        assert isinstance(hook_scripts, list), (
            f"hook_scripts must be a list, got {type(hook_scripts).__name__}"
        )
        assert len(hook_scripts) == 2, (
            f"expected 2 hook scripts (pre + post), got {len(hook_scripts)}: "
            f"{hook_scripts}"
        )
        hook_names = [str(p) for p in hook_scripts]
        assert any("pre_tool_use.sh" in n for n in hook_names), (
            f"pre_tool_use.sh missing from hook_scripts: {hook_names}"
        )
        assert any("post_tool_use.sh" in n for n in hook_names), (
            f"post_tool_use.sh missing from hook_scripts: {hook_names}"
        )
    finally:
        _cleanup_captured(captured)


def test_subagent_config_task_type_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spy's ``task_type`` is a non-empty string.

    ``task_type`` is the decision-4 tag that selects which
    ``post_tool_use`` metrics row to land in. The default is
    ``"general"`` (from ``os.environ.get("PDT_TASK_TYPE", "general")``).
    We pin that the agent always passes a non-empty string — a
    regression that passes ``None`` or omits the kwarg would
    propagate into the hook payload and break the
    ``task_type NOT NULL`` constraint.
    """
    captured: List[Dict[str, Any]] = []
    try:
        captured = _run_autonomous_coding(monkeypatch, tmp_path)
        assert len(captured) == 1
        task_type = captured[0]["kwargs"].get("task_type", "")
        assert isinstance(task_type, str), (
            f"task_type must be a str, got {type(task_type).__name__}: {task_type!r}"
        )
        assert len(task_type) > 0, (
            f"task_type is empty — agent likely passed None or omitted "
            f"the kwarg. kwargs: {captured[0]['kwargs']}"
        )
    finally:
        _cleanup_captured(captured)
