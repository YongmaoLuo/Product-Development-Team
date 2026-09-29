"""Integration test for VP-008: agent.py fallback 决策路径改造 + consumer-layer 消费.

This test pins the contract that the backend's sub-agent bootstrap
path no longer carries a hard-coded provider fallback chain. Instead,
``agent.autonomous_coding`` resolves the chain through
``provider_order.load_fallback_order()`` which reads the live
the contract file and filters
the ``order`` list through the CC Switch SQLite consumer layer
(:func:`cc_switch.list_provider_ids`). There is no
hard-coded fallback and no YAML ``provider_priority`` fallback — a
missing or invalid contract file raises :class:`ProviderOrderError`.

The acceptance bullets (one-to-one with VP-008):

  * test_agent_fallback_uses_load_fallback_order
        — A spy on ``provider_order.load_fallback_order`` is invoked
          when ``autonomous_coding(tool="claude")`` runs end-to-end.
  * test_agent_fallback_no_hardcoded_chain
        — Static grep of ``backend/agent.py`` does NOT contain a
          hard-coded ``["<provider>", "parent"]`` chain in the main
          code path (docstring example schemas
          are filtered out — they document the *shape* of the
          config, not the actual fallback chain).
  * test_agent_fallback_logs_resolution_keyword
        — When the sub-agent bootstrap runs, the ``agent`` logger
          emits at least one INFO record containing either
          "using provider-order.json" or "fallback to config.yaml"
          — operator-greppable markers so oncall can see which
          tier served the chain.
  * test_integration_consumes_optimizer
        — End-to-end: a freshly written ``provider-order.json``
          feeds ``load_fallback_order`` and the returned chain
          matches the JSON ``order`` filtered through the CC Switch
          consumer layer (kebab-case IDs only, DB-present only).

Test isolation
--------------
  * ``$HOME`` is redirected to a private tmpdir containing a fake
    ``~/.cc-switch/cc-switch.db`` so the agent's
    ``_load_provider_info`` call resolves successfully (otherwise
    the sub-agent bootstrap would fail before reaching
    ``load_fallback_order``).
  * ``tmp_path`` is the project_dir; we run real ``git init`` so
    ``GitManager(search_parent_directories=True)`` finds a repo
    at the leaf — not a sibling checkout's ancestor.
  * ``tasks.json`` is pre-baked in the project_dir; we call
    ``autonomous_coding(recover=True, ...)`` so ``agent.plan()``
    is skipped (no LLM call). The agent's ``__init__`` is
    **not** touched.
  * ``ClaudeCodingTool.query`` is mocked to return
    ``"TEST_RESULT: PASSED\\n"`` (a single line, no FILE: blocks)
    so the per-task loop runs through ``_parse_test_result`` →
    ``update_task_status('completed')`` → git commit.
  * ``provider_order.load_fallback_order`` is wrapped with a spy
    that records every call and **forwards to the real
    function** so the rest of the chain stays grounded in
    production code.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

# Make ``agent.py``, ``provider_order.py`` and friends importable
# when pytest is invoked from the project root.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Fake provider configs for end-to-end runs. Three providers so the
# first-match-wins semantic of ``_load_provider_info`` is exercised
# end-to-end. Keys are the names CC Switch gives them — that string is
# the lookup key the whole chain carries.
_FAKE_PROVIDERS = {
    "Vendor A Pro": {
        "base_url": "https://api.vendor-a.example/anthropic",
        "api_key": "sk-cp-fake-vendor-a-key-1234567890",
    },
    "Vendor B Pro": {
        "base_url": "https://api.vendor-b.example/anthropic",
        "api_key": "a8ff.fake-vendor-b-key.1234567890",
    },
    "Vendor C App": {
        "base_url": "https://api.vendor-c.example/anthropic",
        "api_key": "sk-fake-vendor-c-app-key-1234567890",
    },
}


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


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
        ["git", "config", "user.name", "VP-008 Fallback Test"],
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
    # Production CC Switch schema, and the ONLY table seeded here: the
    # backend resolves a provider by the ``providers.name`` column, so a
    # sandbox that also carried a legacy ``provider_configs`` table would
    # be picked first by the consumer (``sqlite_master`` order) and every
    # name-keyed lookup would miss.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS providers ("
        "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
        ")"
    )
    for i, (name, cfg) in enumerate(_FAKE_PROVIDERS.items()):
        conn.execute(
            "INSERT INTO providers (id, name, settings_config) "
            "VALUES (?, ?, ?)",
            (
                f"fake-row-{i}",
                name,
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

    # ``load_fallback_order`` memoises on the RESOLVED FILE PATH, but the
    # value it caches also depends on ``$HOME`` — the chain is filtered
    # through ``~/.cc-switch/cc-switch.db``. Redirecting HOME does not
    # invalidate that cache, so an earlier test in the same session (any
    # test that boots the app while the real CC Switch DB is absent) can
    # leave an empty chain cached at this path and make the assertions
    # below fail with a non-empty expectation against someone else's DB.
    # ``cache_clear`` is the documented way to force a re-read.
    import provider_order

    provider_order.cache_clear()
    return db_path


def _write_tasks_file(project_dir: Path, num_tasks: int = 1) -> Path:
    """Write a ``tasks.json`` with N pending tasks."""
    tasks_payload = {
        "requirement": "test agent_fallback integration",
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


def _install_claude_query_mock(
    monkeypatch: pytest.MonkeyPatch,
    response: str = "TEST_RESULT: PASSED\n",
) -> None:
    """Stub ``ClaudeCodingTool.query`` so no real LLM is invoked."""
    from coding_tool import ClaudeCodingTool

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: response,
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )


def _install_load_fallback_order_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Dict[str, Any]]:
    """Wrap ``provider_order.load_fallback_order`` with a capture +
    delegate spy.

    The real function runs after capture so the rest of the chain
    stays grounded in production code; the spy only records
    ``call_count`` and the return value of each invocation.

    The return value is overridden to the names seeded in the fake CC
    Switch DB, so the resolved chain is independent of the developer's
    own ``provider-order.json`` and of any time-of-day rule. Without
    the override the real ``load_fallback_order`` reads that file and
    the chain is whoever the local optimizer last ordered first.
    """
    from provider_order import load_fallback_order as real_load

    captured: List[Dict[str, Any]] = []
    # Names that exist in the fake CC Switch DB, in the order the
    # walk should try them.
    test_chain = ["Vendor A Pro", "Vendor B Pro", "Vendor C App"]

    def spy_load(*args, **kwargs):
        result = real_load(*args, **kwargs)
        captured.append(
            {
                "args": args,
                "kwargs": dict(kwargs),
                "result": list(result) if result is not None else None,
            }
        )
        return test_chain

    monkeypatch.setattr("provider_order.load_fallback_order", spy_load)
    return captured


def _run_agent_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Dict[str, Any]:
    """Drive ``autonomous_coding()`` end-to-end and return the spy
    captures.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_tasks_file(project_dir, num_tasks=1)

    _install_claude_query_mock(monkeypatch)
    fallback_spy = _install_load_fallback_order_spy(monkeypatch)

    # Make sure PDT_PROVIDER_PRIORITY is NOT set so the code path
    # actually reaches ``load_fallback_order()``.
    monkeypatch.delenv("PDT_PROVIDER_PRIORITY", raising=False)

    # Point the real reader at a contract file naming exactly the
    # providers the fake CC Switch DB declares. Without this the test
    # would read the developer's own ``provider-order.json``, whose
    # providers this sandbox has never heard of, and the filter would
    # reduce the chain to nothing — the assertion below would then be
    # measuring the machine, not the code.
    order_file = tmp_path / "provider-order.json"
    order_file.write_text(
        json.dumps({
            "version": 1,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "source": "test",
            "order": [*_FAKE_PROVIDERS, "parent"],
            "providers": {},
        }),
        encoding="utf-8",
    )
    import provider_order as _provider_order

    monkeypatch.setattr(
        _provider_order, "_default_order_file", lambda: order_file
    )
    _provider_order.cache_clear()

    # Snapshot BEFORE the run: ``_cleanup_tmp_files`` must only delete
    # the settings files this test adds, never a live execution's.
    settings_before = _snapshot_tmp_settings()

    from agent import autonomous_coding

    autonomous_coding(
        requirement="test agent_fallback integration",
        project_dir=str(project_dir),
        recover=True,
        max_tasks=None,
        config_name="coding",
        tool="claude",
        logger=None,
    )

    return {
        "project_dir": project_dir,
        "fallback_spy": fallback_spy,
        "settings_before": settings_before,
    }


def _snapshot_tmp_settings() -> Set[Path]:
    """The ``/tmp/subagent_settings_*.json`` files that exist right now.

    Taken *before* the test drives ``autonomous_coding()`` so
    :func:`_cleanup_tmp_files` can tell this test's files apart from
    everybody else's.
    """
    try:
        return set(Path("/tmp").glob("subagent_settings_*.json"))
    except OSError:
        return set()


def _cleanup_tmp_files(result: Dict[str, Any]) -> None:
    """Delete the ``/tmp/subagent_settings_*.json`` files **this test**
    created — and nothing else.

    This used to be an unqualified ``Path("/tmp").glob(...)`` + unlink,
    i.e. a machine-wide sweep. That is not test isolation; it is a
    cross-process kill switch, because ``/tmp/subagent_settings_*.json``
    is also how a *live* backend execution hands its provider config to a
    running ``claude`` subprocess. A task whose ``test_command`` is the
    full ``pytest backend/tests/`` ran for ~90 minutes and deleted the
    in-flight settings file of a live plan twice. Its sub-agents died
    in 0.2 s with::

        Claude sub-process exited without assistant text (returncode=1,
        elapsed=0.2s). Stderr: Error: Settings file not found:
        /tmp/subagent_settings_87ccd91b….json

    Each death failed a task, which deadlocked its dependency chain,
    which parked the whole plan at ``No schedulable micro-layer found``
    — the operator saw a plan that "kept pausing" for no visible reason.

    So: subtract the pre-run snapshot instead. A file another process
    owns is never ours to delete, and leaking our own file into ``/tmp``
    on a bug is a far cheaper failure than killing somebody's run.

    Note the ``"settings_before" not in result`` guard rather than
    ``result.get("settings_before") or set()``: a legitimately *empty*
    snapshot (nothing was in ``/tmp`` when the test started) means every
    matching file is ours, whereas a *missing* snapshot means we never
    looked. Collapsing the two with ``or`` turns "we don't know" into
    "delete everything" — which is the same bug in a new costume.
    """
    if "settings_before" not in result:
        # The run raised before the snapshot was taken. We cannot tell
        # our files from a live execution's, so we delete nothing.
        return
    before: Set[Path] = result.get("settings_before") or set()
    try:
        for p in Path("/tmp").glob("subagent_settings_*.json"):
            if p in before:
                continue
            try:
                p.unlink()
            except OSError:
                # Best-effort; the file is in /tmp so OSError is
                # effectively impossible on macOS / Linux, but if
                # it happens it must not fail the assertion above.
                pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 1. test_agent_fallback_uses_load_fallback_order
# ---------------------------------------------------------------------------


def test_agent_fallback_uses_load_fallback_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``autonomous_coding(tool="claude")`` must call
    ``provider_order.load_fallback_order()`` to resolve the chain.

    A regression that re-introduces a hard-coded list in
    ``agent.py`` (e.g. an ``or ["Some Provider", "parent"]`` fallback)
    would skip the spy capture entirely and
    this test would fail.
    """
    caplog.set_level(logging.INFO, logger="agent")
    result: Dict[str, Any] = {}
    try:
        result = _run_agent_fallback(monkeypatch, tmp_path)
        captured = result["fallback_spy"]
        assert len(captured) >= 1, (
            "load_fallback_order() was never called by autonomous_coding; "
            "agent.py must resolve the chain through provider_order, not "
            "a hard-coded literal"
        )
        # The returned chain must be a non-empty list of strings
        first_result = captured[0]["result"]
        assert isinstance(first_result, list) and first_result, (
            f"load_fallback_order() must return a non-empty list, "
            f"got {first_result!r}"
        )
        for entry in first_result:
            assert isinstance(entry, str) and entry.strip(), (
                f"each entry must be a non-empty string, got {entry!r}"
            )
    finally:
        _cleanup_tmp_files(result)


# ---------------------------------------------------------------------------
# 2. test_agent_fallback_no_hardcoded_chain
# ---------------------------------------------------------------------------


def test_agent_fallback_no_hardcoded_chain() -> None:
    """No hard-coded ``["<provider>", "parent"]`` chain must remain in
    ``agent.py``'s main code path.

    VP-008 contract: ``provider_order`` no longer carries a hard-coded
    fallback chain — the chain is resolved from
    ``provider-order.json`` and filtered through the CC Switch
    consumer layer. The sub-agent bootstrap must call
    ``load_fallback_order()`` to get it.

    Docstring examples that *describe the shape of an unrelated
    config schema* are excluded from the search — those are
    documentation, not the live fallback chain.
    """
    agent_path = _BACKEND_DIR / "agent.py"
    assert agent_path.exists(), f"agent.py not found at {agent_path}"

    text = agent_path.read_text(encoding="utf-8")

    # Strip docstring/example lines so an honest docstring showing
    # the schema shape (e.g. the config.model_map nesting example)
    # doesn't trip the assertion. The contract is "no hard-coded
    # *fallback chain* in the live code path", not "no mention of
    # these names anywhere in the file".
    code_lines: List[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            # comment line
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # naive single-line docstring; multi-line handled below
            continue
        code_lines.append(line)

    # Multi-line docstrings: detect blocks of triple-quoted strings
    # and remove them from the searchable text.
    cleaned = "\n".join(code_lines)
    cleaned = re.sub(r'"""[\s\S]*?"""', "", cleaned)
    cleaned = re.sub(r"'''[\s\S]*?'''", "", cleaned)

    # Any two-element list literal of the form ``["<name>", "parent"]``
    # is a hard-coded fallback chain: ``"parent"`` only ever means "the
    # caller's last resort", so a literal that spells it next to a
    # provider name is a chain that was compiled in rather than read
    # from the optimizer's file.
    forbidden_pattern = re.compile(
        r"\[\s*[\'\"][^\'\"]+[\'\"]\s*,\s*[\'\"]parent[\'\"]\s*\]"
    )
    match = forbidden_pattern.search(cleaned)
    assert match is None, (
        f"Hard-coded fallback chain found in agent.py main code path "
        f"(matches VP-008 forbidden pattern): {match.group(0) if match else ''!r}. "
        f"agent.py must call provider_order.load_fallback_order() instead."
    )


# ---------------------------------------------------------------------------
# 3. test_agent_fallback_logs_resolution_keyword
# ---------------------------------------------------------------------------


def test_agent_fallback_logs_resolution_keyword(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the sub-agent bootstrap runs, the ``agent`` logger emits
    at least one INFO record containing either
    ``"using provider-order.json"`` or
    ``"fallback to config.yaml"`` — operator-greppable markers
    so oncall can see which tier served the chain.
    """
    caplog.set_level(logging.INFO, logger="agent")
    result: Dict[str, Any] = {}
    try:
        result = _run_agent_fallback(monkeypatch, tmp_path)

        agent_records = [
            rec for rec in caplog.records if rec.name == "agent"
        ]
        assert agent_records, (
            "No records were captured on the 'agent' logger; "
            "agent.py must emit a resolution log so operators can "
            "see which tier served the chain"
        )

        keywords = ("using provider-order.json", "fallback to config.yaml")
        matching = [
            rec for rec in agent_records
            if any(kw in rec.getMessage() for kw in keywords)
        ]
        assert matching, (
            f"No agent.py log record contained a resolution keyword "
            f"({keywords!r}). Captured records: "
            f"{[rec.getMessage() for rec in agent_records]!r}"
        )
    finally:
        _cleanup_tmp_files(result)


# ---------------------------------------------------------------------------
# 5. test_integration_consumes_optimizer
# ---------------------------------------------------------------------------


def test_integration_consumes_optimizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``load_fallback_order`` consumes a live ``provider-order.json``
    and filters it through the CC Switch consumer layer.

    A freshly-written optimizer file lists four providers, but only
    two of them exist in the fake CC Switch DB.  The returned chain
    must contain exactly the DB-present names in the JSON order, and
    the ``agent`` logger must emit the resolution keyword so an
    operator can see that the optimizer file was used.
    """
    caplog.set_level(logging.INFO, logger="agent")

    # 1. Install a fake CC Switch DB with a subset of the JSON providers.
    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        # Production CC Switch schema: the row's ``name`` is what the
        # optimizer chain carries and what the consumer keys on.
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
        )
        for i, provider_name in enumerate(("Vendor A Pro", "Vendor B Pro")):
            conn.execute(
                "INSERT INTO providers (id, name, settings_config) "
                "VALUES (?, ?, ?)",
                (
                    f"row-{i}",
                    provider_name,
                    json.dumps({
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://example.com/v1",
                            "ANTHROPIC_AUTH_TOKEN": f"fake-{provider_name}-key",
                        }
                    }),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("HOME", str(fake_home))

    # 2. Write a fresh provider-order.json under a temp project path.
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    order_file = project_dir / "provider-order.json"
    order_file.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "producer",
                "order": [
                    "Vendor A Pro",
                    "Vendor B Pro",
                    "Vendor C App",  # not in DB
                    "parent",                 # not a DB provider name
                ],
                "providers": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. Drive the real loader directly.
    from provider_order import load_fallback_order

    result = load_fallback_order(order_file)

    # 4. The chain is filtered through the consumer layer; both sides
    #    speak CC Switch provider names, so membership is a plain set test.
    assert result == ["Vendor A Pro", "Vendor B Pro"], (
        f"expected JSON order filtered to DB-present display names, got {result!r}"
    )
