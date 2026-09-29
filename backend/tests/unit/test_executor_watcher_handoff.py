"""Regression tests for the executor-watcher → auto-verification hand-off.

Background (2026-09-13)
--------------------------------------
A plan execution that returned rc=0 never reached verification. The
executor subprocess logged ``execution_completed`` and exited cleanly,
but the server left ``plan_routing.stage='executing'`` and
``plan_execution.exec_status='running'`` forever, with nothing in
``tools/logs/server.log``.

Two independent defects combined to hide it:

1. **UnboundLocalError on ``open_db`` / ``migrate``.**
   ``start_execution`` binds ``open_db``, ``migrate`` and
   ``ExecutionRepository`` as enclosing-function locals (server.py
   ~6626-6631). The nested ``_run`` watcher re-imported ``open_db``
   and ``migrate`` inside one deeply-nested branch (the
   ``unfinished_ids`` derivation that only executes when
   ``_count_unfinished_tasks`` reports pending work). A
   function-local import makes the name local to ``_run`` for the
   *entire* function, so on the normal all-tasks-terminal success
   path — where that branch is skipped — every ``open_db(...)`` call
   in ``_run`` raised ``UnboundLocalError``. The first one sits
   immediately before the auto-verification call.

2. **The watcher's outer ``except Exception`` swallowed it.**
   ``UnboundLocalError`` propagated to the bottom-of-``_run``
   handler, whose only action was ``if state["status"] == "running"``
   — already ``"completed"`` by then — so the exception vanished with
   no log line and the trailing ``update_status`` was skipped too.

Contract pinned here
--------------------
* ``_run`` must not rebind a name that ``start_execution`` binds as an
  enclosing local — the AST guard stops the shadowing from coming
  back in any future edit.
* The auto-verification hand-off must fire even when the
  best-effort ``exec_status`` persist raises.
"""

from __future__ import annotations

import ast
import json
import sys
import time
from pathlib import Path

import pytest
from tests.app_source import find_def

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _seed_plan(db_path: Path, plan_id: str, project_dir: Path) -> None:
    """Insert the ``plan_routing`` / ``plan_execution`` rows ``start_execution`` needs."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, "ready", 0, "[]",
                "{}", "{}",
                json.dumps(
                    {"status": "pending", "round": 0, "max_rounds": 3, "stop_reason": None}
                ),
                "2026-09-13T00:00:00", "2026-09-13T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, current_phase, attempt_count, project_dir, stop_reason, "
            " task_progress, next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, "ready", 0, str(project_dir), None,
                json.dumps({"tasks": {}}), None, "{}", "{}", None, "not_started",
                None, "2026-09-13T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle."""

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def _local_bindings(fn: ast.FunctionDef) -> set[str]:
    """Names bound *inside* ``fn`` (imports, assignments, params)."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _watcher_bodies() -> tuple[set[str], set[str]]:
    """Return (start_execution locals, _run locals) from the live source.

    ``start_execution`` lives in ``backend/routes/execution.py`` since the
    2026-09-25 split; :func:`find_def` follows the code across modules.
    """
    outer = find_def("start_execution").node
    inner = next(
        n for n in ast.walk(outer)
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    return _local_bindings(outer), _local_bindings(inner)


# ---------------------------------------------------------------------------
# 1. Structural guard — no shadowing of the enclosing scope's DB helpers
# ---------------------------------------------------------------------------


def test_run_does_not_shadow_enclosing_db_helpers():
    """``_run`` must not rebind ``open_db`` / ``migrate`` / ``ExecutionRepository``.

    Re-importing any of these inside ``_run`` turns the name into a
    ``_run``-local for the whole function, so branches that skip the
    import raise ``UnboundLocalError`` at every later use. That is the
    2026-09-13 bug: the auto-verification hand-off was skipped because
    ``open_db`` was unbound on the all-tasks-terminal success path.
    """
    outer, inner = _watcher_bodies()
    enclosing = {"open_db", "migrate", "ExecutionRepository"}

    assert enclosing <= outer, (
        "start_execution no longer binds the DB helpers the watcher closes "
        "over — update this guard if the import seam moved."
    )
    shadowed = sorted(enclosing & inner)
    assert shadowed == [], (
        f"_run re-binds enclosing-scope name(s) {shadowed}; remove the local "
        "import and rely on the start_execution closure instead."
    )


def test_run_actually_uses_open_db():
    """Guard against a vacuous pass: ``_run`` must still call ``open_db``."""
    outer = find_def("start_execution").node
    inner = next(
        n for n in ast.walk(outer)
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    called = {
        n.func.id
        for n in ast.walk(inner)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "open_db" in called


# ---------------------------------------------------------------------------
# 2. Behavioural — the hand-off fires even when the status persist fails
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    """Wire ``server`` to a hermetic plan dir + state.db and neutral seams."""
    import server

    plan_id = "handoff-plan"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]})
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, plan_id, project_dir)

    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server, "_reject_if_placeholder_prd", lambda *a, **k: None)
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan", lambda *a, **k: "active"
    )
    # All tasks terminal → the clean success path (no orphan derivation branch).
    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 1, "pending_or_unstarted": 0, "failed": 0},
    )
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (_FakeProcess(), tmp_path / "executor.log"),
    )

    calls: list[tuple] = []
    monkeypatch.setattr(
        server,
        "_run_auto_verification_loop",
        lambda *a, **k: calls.append((a, k)),
    )

    server._execution_state.pop(plan_id, None)
    yield server, plan_id, project_dir, calls
    server._execution_state.pop(plan_id, None)


def _await(calls: list, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not calls:
        time.sleep(0.02)
    assert calls, "auto-verification hand-off never fired within the timeout"


def test_auto_verification_fires_on_clean_exit(isolated_server):
    """Baseline: rc=0 with every task terminal reaches ``_run_auto_verification_loop``."""
    server, plan_id, project_dir, calls = isolated_server

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    _await(calls)


def test_auto_verification_fires_even_if_status_persist_fails(isolated_server, monkeypatch):
    """A failure in the best-effort ``exec_status`` persist must not skip verification.

    Before the fix, *any* exception raised between ``state["ended_at"]``
    and the auto-verification call propagated into ``_run``'s outer
    ``except Exception`` — which swallowed it silently and skipped the
    hand-off entirely.
    """
    server, plan_id, project_dir, calls = isolated_server

    from state_machine.repositories.execution_repository import ExecutionRepository

    def _boom(self, *a, **k):
        raise RuntimeError("simulated state.db contention")

    monkeypatch.setattr(ExecutionRepository, "update_status", _boom)

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    _await(calls)
