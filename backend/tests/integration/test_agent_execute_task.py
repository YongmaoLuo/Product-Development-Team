"""
test_agent_execute_task.py — end-to-end integration tests for
``AutonomousAgent._execute_task_with_retry`` (``execute_single_task``)
exercising the full SubagentConfig → ``write_tmp_settings`` →
``ClaudeCodingTool.settings`` chain.

Background:
  Task 7's earlier tests (``test_subagent_instantiation.py``,
  ``test_coding_tool_settings_injection.py``) covered two halves of
  the chain in isolation:
    * 7-1: ``autonomous_coding()`` instantiates SubagentConfig with
      the right fields.
    * 7-4-5: ``ClaudeCodingTool.__init__`` writes ``--settings
      <path>`` to the subprocess cmdline.
  Neither test actually runs ``_execute_task_with_retry`` end-to-end
  — they either stub the agent's ``plan``/``run`` to no-ops, or
  only inspect the constructor. The 6 tests below close that gap.

The 6 TDD acceptance bullets (one-to-one with the spec):

  * test_execute_single_task_passes_settings_to_coding_tool
        — ClaudeCodingTool.__init__ kwargs include a non-None
          ``settings`` path.
  * test_execute_single_task_settings_file_exists
        — the file at that path exists on disk (not just in
          memory).
  * test_execute_single_task_settings_has_endpoint_and_credentials
        — parse the JSON, ``len(env) == 7`` ANTHROPIC_* keys.
  * test_execute_single_task_passes_hook_stdin
        — ClaudeCodingTool.__init__ kwargs include ``hook_stdin``
          as a dict with ``task_type`` and ``task_summary``.
  * test_execute_single_task_completes_on_test_pass
        — when the AI reports ``TEST_RESULT: PASSED``, the task's
          persisted status is ``completed``.
  * test_execute_single_task_writes_tmpfile_unique_per_task
        — running 2 tasks produces 2 different uuid tmpfiles
          (per-task regeneration, not the bootstrap-only design).

Test isolation strategy:
  * ``tmp_path`` is the project_dir; we run real ``git init`` so
    ``GitManager(search_parent_directories=True)`` finds a repo
    at the leaf — not a sibling checkout's ancestor.
  * ``$HOME`` is redirected to a private tmpdir containing a fake
    ``~/.cc-switch/cc-switch.db`` so ``_load_provider_info`` is
    exercised end-to-end (we never patch it out).
  * ``tasks.json`` is pre-baked in the project_dir; we call
    ``autonomous_coding(recover=True, ...)`` so ``agent.plan()``
    is skipped (no LLM call). The agent's ``__init__`` is
    **not** touched.
  * ``ClaudeCodingTool.__init__`` is wrapped with a spy that
    captures kwargs and **forwards to the real constructor** —
    the real coding_tool instance is populated, the real
    ``settings`` path is on disk, only the LLM call is mocked.
  * ``ClaudeCodingTool.query`` is mocked to return
    ``"TEST_RESULT: PASSED\\n"`` (a single line, no FILE: blocks)
    so the per-task loop runs through ``_parse_test_result`` →
    ``update_task_status('completed')`` → git commit.
  * ``SubagentConfig.write_tmp_settings`` is wrapped with a spy
    that captures the file paths written — the test for
    "unique per task" reads off this list, not a glob sweep of
    ``/tmp`` (which would race with other tests' leftovers).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Make ``agent.py`` and ``subagent_config.py`` importable in isolation.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


# Provider fixture used by every test. Two providers so the first-match-wins
# semantic of ``_load_provider_info`` is exercised end-to-end (vendor-a-pro
# is the default first provider).
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
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Execute Task E2E Test"],
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


def _write_tasks_file(project_dir: Path, num_tasks: int) -> Path:
    """Write a ``tasks.json`` with N pending tasks.

    The shape matches what ``TaskManager.save_tasks`` writes so the
    agent's ``load_tasks`` path is exercised end-to-end.
    """
    tasks_payload = {
        "requirement": "test execute_single_task end-to-end",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": str(i),
                "title": f"Test task {i}",
                "description": f"Test task {i} description",
                "test_command": "echo done",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
            }
            for i in range(1, num_tasks + 1)
        ],
    }
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_payload, indent=2), encoding="utf-8")
    return tasks_file


def _install_claude_coding_tool_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Dict[str, Any]]:
    """Wrap ``ClaudeCodingTool.__init__`` with a capture + delegate spy.

    The real ``__init__`` runs after the capture, so the resulting
    instance is fully populated — the test can read off
    ``instance.settings``, ``instance.hook_stdin``, etc. We do NOT
    replace the constructor.
    """
    from coding_tool import ClaudeCodingTool

    captured: List[Dict[str, Any]] = []
    real_init = ClaudeCodingTool.__init__

    def spy_init(self, *args, **kwargs):
        captured.append(
            {
                "self_id": id(self),
                "args": args,
                "kwargs": dict(kwargs),
                "instance": self,
            }
        )
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(ClaudeCodingTool, "__init__", spy_init)
    return captured


def _install_claude_query_mock(
    monkeypatch: pytest.MonkeyPatch,
    response: str = "TEST_RESULT: PASSED\n",
) -> None:
    """Stub ``ClaudeCodingTool.query`` and ``query_json`` to return fixed
    responses so no real LLM is invoked.

    ``query_json`` is required because ``autonomous_coding`` calls
    ``agent.plan()`` first (which calls ``query_json``); we use
    ``recover=True`` to skip plan() in our tests, but the import
    side-effects of agent.py register the function on the class —
    we still stub it defensively in case plan() is ever called.
    """
    from coding_tool import ClaudeCodingTool

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: response,
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )


def _install_write_tmp_settings_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> List[str]:
    """Wrap ``SubagentConfig.write_tmp_settings`` to record every path
    written. The real method runs (so the file lands on disk and
    the env-block contract is preserved); the spy only captures
    the returned paths.

    Used by test 6 to assert "2 tasks = 2 unique tmpfiles" without
    racing against leftover files from other tests in /tmp.
    """
    from subagent_config import SubagentConfig

    captured: List[str] = []
    real_write = SubagentConfig.write_tmp_settings

    def spy_write(self, *args, **kwargs):
        path = real_write(self, *args, **kwargs)
        captured.append(str(path))
        return path

    monkeypatch.setattr(SubagentConfig, "write_tmp_settings", spy_write)
    return captured


def _run_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    num_tasks: int = 1,
    query_response: str = "TEST_RESULT: PASSED\n",
) -> Dict[str, Any]:
    """Drive the end-to-end chain once. Returns a dict with the
    spy captures and the post-run persisted task statuses.
    """
    # 2026-09-14: the fixture directory used to be named ``project``,
    # which ``TaskManager`` now refuses as a reserved plan_id (too
    # generic — would collide across ``plan_routing`` /
    # ``plan_execution`` rows), so the SQLite mirror was silently
    # skipped and the only persisted status copy lived in memory that
    # evaporated at return. A >=4-unique-char name lets the mirror
    # engage, which matters because on-disk ``tasks.json`` strips
    # ``status`` — the SQLite ``plan_execution.task_progress`` row is
    # the only post-run source of truth.
    project_dir = tmp_path / "single-task-proj"
    plan_id = project_dir.name
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_tasks_file(project_dir, num_tasks)

    coding_tool_spy = _install_claude_coding_tool_spy(monkeypatch)
    _install_claude_query_mock(monkeypatch, response=query_response)
    write_settings_spy = _install_write_tmp_settings_spy(monkeypatch)
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
        requirement="test execute_single_task end-to-end",
        project_dir=str(project_dir),
        recover=True,
        max_tasks=None,
        config_name="coding",
        tool="claude",
        logger=None,
    )

    return {
        "project_dir": project_dir,
        "plan_id": plan_id,
        "coding_tool_spy": coding_tool_spy,
        "write_settings_spy": write_settings_spy,
        "tasks_after": _read_persisted_tasks(project_dir),
        "persisted_statuses": _read_persisted_task_statuses(plan_id),
    }


def _read_persisted_tasks(project_dir: Path) -> List[Dict[str, Any]]:
    """Re-read the on-disk ``tasks.json`` after the run completes."""
    tasks_file = project_dir / "tasks.json"
    with open(tasks_file, "r", encoding="utf-8") as f:
        tasks_data = json.load(f)
    return tasks_data["tasks"]


def _read_persisted_task_statuses(plan_id: str) -> Dict[str, Any]:
    """Read per-task statuses from the SQLite ``plan_tasks`` mirror.

    ``save_tasks`` strips ``status`` from the on-disk ``tasks.json``
    (the file is a spec snapshot; the state machine is the source of
    truth), so after the run the only persisted per-task status is the
    ``plan_tasks`` row written by
    ``task_manager._persist_status_to_sqlite``. The per-test hermetic
    DB path comes from the ``PDT_STATE_DB_PATH`` env var the conftest
    sets.
    """
    import os

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = Path(os.environ["PDT_STATE_DB_PATH"])
    conn = open_db(db_path)
    try:
        migrate(conn)
        rows = conn.execute(
            "SELECT task_id, status FROM plan_tasks WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return {task_id: status for task_id, status in rows}


def _cleanup_tmp_files(result: Dict[str, Any]) -> None:
    """Delete any /tmp/subagent_settings_*.json files written by
    the real ``write_tmp_settings`` during the test. Best-effort.
    """
    seen: set = set()
    for path_str in result.get("write_settings_spy", []):
        if path_str in seen:
            continue
        seen.add(path_str)
        try:
            p = Path(path_str)
            if p.exists():
                p.unlink()
        except OSError:
            pass
    # Also clean up the settings path the coding_tool currently holds
    # (the per-task regeneration updates self.coding_tool.settings,
    # so the last write_tmp_settings call is the one we already
    # have in the spy list — no extra cleanup needed).


# ---------------------------------------------------------------------------
# 6 TDD tests
# ---------------------------------------------------------------------------


def test_execute_single_task_passes_settings_to_coding_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ClaudeCodingTool.__init__`` kwargs include a non-None ``settings``.

    The settings kwarg is the path produced by
    ``SubagentConfig.write_tmp_settings``. A regression that drops
    the ``settings=`` kwarg (or passes ``None``) would mean the
    Claude subprocess is launched without ``--settings`` and
    inherits the parent process's ``ANTHROPIC_*`` env vars
    — exactly the failure mode decisions 2/3 were meant to fix.
    """
    result: Dict[str, Any] = {}
    try:
        result = _run_end_to_end(monkeypatch, tmp_path, num_tasks=1)
        captured = result["coding_tool_spy"]
        assert len(captured) >= 1, (
            f"ClaudeCodingTool was never constructed; "
            f"write_tmp_settings was called {len(result['write_settings_spy'])} time(s)"
        )
        settings = captured[0]["kwargs"].get("settings")
        assert settings is not None, (
            f"settings kwarg is None — agent failed to pass "
            f"SubagentConfig.write_tmp_settings result. kwargs: "
            f"{captured[0]['kwargs']}"
        )
        assert isinstance(settings, (str, Path)), (
            f"settings must be str or Path, got {type(settings).__name__}: "
            f"{settings!r}"
        )
    finally:
        _cleanup_tmp_files(result)


def test_execute_single_task_settings_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The settings file passed to ClaudeCodingTool exists on disk.

    A regression that calls ``SubagentConfig.to_settings_dict()``
    but never actually calls ``write_tmp_settings()`` (or that
    points ClaudeCodingTool at a non-existent path) would let
    this assertion catch it. The 7-4-3 task added atomic
    write-then-rename semantics so the file is either fully on
    disk or not at all — there is no half-written state.
    """
    result: Dict[str, Any] = {}
    try:
        result = _run_end_to_end(monkeypatch, tmp_path, num_tasks=1)
        captured = result["coding_tool_spy"]
        assert len(captured) >= 1
        settings = captured[0]["kwargs"].get("settings")
        assert settings is not None, (
            f"settings kwarg is None; kwargs: {captured[0]['kwargs']}"
        )
        path = Path(settings)
        assert path.exists(), (
            f"settings file does not exist on disk: {path}. "
            f"write_tmp_settings spy saw: {result['write_settings_spy']}"
        )
        assert path.is_file(), f"settings path is not a file: {path}"
    finally:
        _cleanup_tmp_files(result)


def test_execute_single_task_settings_has_endpoint_and_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Parse the on-disk settings file; the env block carries the core keys.

    2026-09-13 contract: model management is delegated ENTIRELY to CC
    Switch — the tiered 4-role model env block was removed. The
    mandatory core is now:

        1. ANTHROPIC_BASE_URL
        2. ANTHROPIC_AUTH_TOKEN
        3. ANTHROPIC_API_KEY

    Model keys arrive via the flat ``model_env`` passthrough from the
    CC Switch provider row (this test's fake DB supplies none, so the
    file may legitimately contain exactly the 3 core keys).
    """
    result: Dict[str, Any] = {}
    try:
        result = _run_end_to_end(monkeypatch, tmp_path, num_tasks=1)
        captured = result["coding_tool_spy"]
        assert len(captured) >= 1
        settings = captured[0]["kwargs"].get("settings")
        assert settings is not None
        with open(settings, "r", encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env", {})
        assert isinstance(env, dict), (
            f"env block must be a dict, got {type(env).__name__}: {env!r}"
        )
        for required in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
        ):
            assert required in env, (
                f"settings file env block missing required key {required!r}; "
                f"got env: {env}"
            )
    finally:
        _cleanup_tmp_files(result)


def test_execute_single_task_passes_hook_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ClaudeCodingTool.__init__`` kwargs include ``hook_stdin`` as a dict
    with ``task_type`` and ``task_summary``.

    ``hook_stdin`` is the decision-4 pass-through payload: the
    Claude SDK forwards it to the PostToolUse hook subprocess's
    stdin so the metrics row knows which task it belongs to.
    A regression that drops the ``hook_stdin=`` kwarg (or
    passes ``None``) would break the post_tool_use metrics
    pipeline silently — the hook would run but with no
    correlation tag.
    """
    result: Dict[str, Any] = {}
    try:
        result = _run_end_to_end(monkeypatch, tmp_path, num_tasks=1)
        captured = result["coding_tool_spy"]
        assert len(captured) >= 1
        hook_stdin = captured[0]["kwargs"].get("hook_stdin")
        assert isinstance(hook_stdin, dict), (
            f"hook_stdin must be a dict, got {type(hook_stdin).__name__}: "
            f"{hook_stdin!r}"
        )
        assert "task_type" in hook_stdin, (
            f"hook_stdin missing 'task_type'; got: {hook_stdin}"
        )
        assert "task_summary" in hook_stdin, (
            f"hook_stdin missing 'task_summary'; got: {hook_stdin}"
        )
        # The pass-through values must be non-empty strings — the
        # post_tool_use.sh script reads them off stdin and an
        # empty task_summary would propagate into the metrics
        # row's NOT NULL constraint.
        assert isinstance(hook_stdin["task_type"], str) and hook_stdin["task_type"], (
            f"hook_stdin.task_type must be a non-empty str, got: "
            f"{hook_stdin['task_type']!r}"
        )
        assert isinstance(hook_stdin["task_summary"], str) and hook_stdin["task_summary"], (
            f"hook_stdin.task_summary must be a non-empty str, got: "
            f"{hook_stdin['task_summary']!r}"
        )
    finally:
        _cleanup_tmp_files(result)


def test_execute_single_task_completes_on_test_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the AI reports ``TEST_RESULT: PASSED``, the task's persisted
    status is ``completed``.

    This is the regression guard for the per-task loop's
    completion path. The mocked Claude returns
    ``"TEST_RESULT: PASSED\\n"`` so ``_parse_test_result`` returns
    ``(True, "")`` and the agent calls
    ``update_task_status(task.id, "completed")`` and
    ``_commit_task_changes(...)``. We then re-read tasks.json
    from disk to confirm the status is persisted.
    """
    result: Dict[str, Any] = {}
    try:
        result = _run_end_to_end(
            monkeypatch, tmp_path, num_tasks=1,
            query_response="TEST_RESULT: PASSED\n",
        )
        tasks_after = result["tasks_after"]
        assert len(tasks_after) == 1, (
            f"expected 1 task in tasks.json, got {len(tasks_after)}: {tasks_after}"
        )
        # Status truth lives in the SQLite execution mirror — the
        # on-disk tasks.json strips ``status`` (see _run_end_to_end).
        statuses = result["persisted_statuses"]
        assert statuses.get("1") == "completed", (
            f"expected persisted status=completed for task 1, got {statuses!r}"
        )
        # Also: write_tmp_settings was called (at minimum once for
        # the bootstrap in autonomous_coding, and once for the
        # per-task regeneration in _execute_task_with_retry).
        # The test does not assert a specific count here because
        # the per-task refactor could change the call count
        # without breaking this assertion — test 6 covers the
        # "unique per task" invariant specifically.
        assert len(result["write_settings_spy"]) >= 1, (
            f"write_tmp_settings was never called; "
            f"coding_tool_spy length: {len(result['coding_tool_spy'])}"
        )
    finally:
        _cleanup_tmp_files(result)


def test_execute_single_task_writes_tmpfile_unique_per_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running 2 tasks produces 2 different uuid tmpfiles.

    This pins the per-task tmpfile regeneration: each task in the
    ``agent.run()`` loop calls ``SubagentConfig.write_tmp_settings``
    so the Claude subprocess for that task gets a unique
    ``--settings <path>``. The uuids in those paths are what the
    cross-process correlation in execution.log uses to link a
    settings file back to a specific task — without per-task
    regeneration, two parallel-running tasks would share one
    tmpfile and the correlation would be ambiguous.
    """
    result: Dict[str, Any] = {}
    try:
        result = _run_end_to_end(monkeypatch, tmp_path, num_tasks=2)
        paths = result["write_settings_spy"]
        # At least 2 distinct write_tmp_settings invocations.
        assert len(paths) >= 2, (
            f"expected >= 2 write_tmp_settings calls (one per task), "
            f"got {len(paths)}: {paths}"
        )
        # All recorded paths must be unique by uuid. The pattern is
        # ``<private tmpdir>/subagent_settings_<uuid>.json`` — flat in
        # ``/tmp`` until 2026-09-27, when the settings payload (which
        # carries the provider credential) moved into a 0700 directory
        # with 0600 on the file. Match on the basename so the assertion
        # is about the naming contract, not about where the temp root is.
        uuid_pattern = re.compile(
            r"^subagent_settings_(?P<u>[0-9a-f]{32})\.json$"
        )
        uuids = []
        for p in paths:
            m = uuid_pattern.match(Path(p).name)
            assert m, f"path {p!r} does not match expected uuid tmpfile pattern"
            uuids.append(m.group("u"))
        assert len(set(uuids)) >= 2, (
            f"expected 2+ unique uuids across tasks, got {uuids}"
        )
        # And: both tasks completed (the per-task regeneration is
        # only meaningful if the task actually ran through the
        # full path — including the LLM call, the parsed result,
        # and the git commit). Status truth is the SQLite mirror;
        # on-disk tasks.json strips ``status``.
        statuses = result["persisted_statuses"]
        completed = [tid for tid, st in statuses.items() if st == "completed"]
        assert sorted(completed) == ["1", "2"], (
            f"expected both tasks to be completed, got persisted statuses: "
            f"{statuses!r}"
        )
    finally:
        _cleanup_tmp_files(result)
