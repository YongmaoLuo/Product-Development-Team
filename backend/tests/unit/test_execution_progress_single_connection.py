"""Regression test for the progress endpoint's single-connection reuse.

Background
----------
``GET /api/execution/{plan_id}/progress`` reads two pieces of state
from the state-machine SQLite database:

  1. ``plan_task_repository.load_all(plan_id)`` — the per-task runtime
     overlay (``status`` / ``end_ts`` / ``commit_sha`` / etc.).
  2. ``execution_repository.progress(plan_id)`` — the aggregated
     ``task_progress`` column used as a sanity-check hint.

Before this task the endpoint opened **two** separate SQLite
connections to the same file, each going through ``migrate()`` and
applying the WAL / busy_timeout / synchronous PRAGMAs. The two
opens are wasted work: the schema is identical, the second open
adds another ``fsync`` against the WAL, and an in-flight writer
between the two opens can show a transient inconsistent snapshot
(the first open sees the runtime overlay pre-write, the second sees
the aggregated row post-write — or vice versa).

The fix is to open the SQLite handle **once** and pass the same
``sqlite3.Connection`` to both repositories. This pins down the
single-connection contract and also makes a regression that adds a
third accidental open impossible to merge.

TDD spec:

  - test_progress_endpoint_opens_state_db_once — calls the endpoint
    and asserts ``state_machine.db.connection.open`` was called
    exactly once. The runtime overlay AND the aggregated
    ``task_progress`` read must both flow through that single handle.

  - test_progress_endpoint_returns_runtime_overlay — pins the
    contract that the single connection still serves both repos:
    the response's ``tasks[i].status`` is the value from
    ``PlanTaskRepository.load_all`` (not the static tasks.json).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _seed_state_db(db_path: Path, plan_id: str, project_dir: Path) -> None:
    """Hermetic state-machine setup: insert one plan_execution row
    with project_dir + a per-task overlay + an aggregated
    task_progress column. Mirrors what the runtime writes during a
    real execution."""
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = _open_db(db_path)
    _migrate(conn)
    try:
        er = ExecutionRepository(conn)
        # Aggregated task_progress column — the sanity-check hint.
        er.insert(
            plan_id,
            "running",
            exec_status="running",
            project_dir=str(project_dir),
            task_progress=json.dumps(
                {
                    "total": 2,
                    "completed": 1,
                    "failed": 0,
                    "in_progress": 0,
                    "pending": 1,
                }
            ),
        )
        conn.commit()
        # Per-task overlay (current schema; see
        # server.get_execution_progress docstring).
        # ``update_task_progress`` JSON-encodes its argument internally,
        # so we pass a dict (not a pre-encoded string) here.
        er.update_task_progress(
            plan_id,
            {
                "tasks": {
                    "1": {
                        "id": "1",
                        "status": "completed",
                        "end_ts": "2026-09-05T00:00:00",
                    },
                    "2": {
                        "id": "2",
                        "status": "pending",
                    },
                }
            },
        )
        conn.commit()
    finally:
        conn.close()


def _write_plan_tasks_file(plan_id: str, plans_dir: Path) -> None:
    """Write a static tasks.json so ``get_execution_progress`` can
    find the per-task rows to overlay onto."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": "1", "title": "first", "description": "d1", "status": "pending"},
        {"id": "2", "title": "second", "description": "d2", "status": "pending"},
    ]
    (plan_dir / "tasks.json").write_text(json.dumps(tasks))


def test_progress_endpoint_opens_state_db_once(tmp_path, monkeypatch) -> None:
    """The progress endpoint must open the state-machine SQLite
    exactly **once** per request for the runtime overlay +
    ``task_progress`` aggregated read, not twice (one for
    ``PlanTaskRepository`` and a second for ``ExecutionRepository``).

    ``_get_project_dir`` is mocked out so we measure ONLY the opens
    the endpoint body performs (the runtime overlay block + the
    ``task_progress`` read block). The project_dir lookup is a
    separate concern, addressed by :func:`server._open_state_machine`,
    and not what this task is consolidating.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_id = "plan-single-conn"

    _write_plan_tasks_file(plan_id, plans_dir)
    _seed_state_db(db_path, plan_id, project_dir)

    # Patch _state_db_path so the endpoint hits our hermetic DB.
    import server

    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    # Skip _get_project_dir's _open_state_machine call so we count
    # only the opens inside get_execution_progress's body.
    monkeypatch.setattr(server, "_get_project_dir", lambda pid: project_dir)

    # Track every call to state_machine.db.connection.open. The
    # endpoint imports it locally as ``open_db`` inside the try
    # block, so patching the symbol at its source module
    # intercepts every call.
    from state_machine.db import connection as _conn_mod

    real_open = _conn_mod.open
    call_log: list[Path] = []

    def _counting_open(db_path_arg):
        call_log.append(db_path_arg)
        return real_open(db_path_arg)

    monkeypatch.setattr(_conn_mod, "open", _counting_open)

    # Invoke the endpoint via the TestClient so we hit the real
    # route + middleware stack (no in-memory shortcut).
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.get(f"/api/execution/{plan_id}/progress")

    # The endpoint must NOT 404/500 — both repositories must have
    # produced data on the single connection.
    assert resp.status_code == 200, (
        f"expected 200 OK; got {resp.status_code} body={resp.text!r}"
    )
    body = resp.json()
    # Counts come from the per-task overlay (status fields merged
    # onto tasks via PlanTaskRepository.load_all). The endpoint
    # reports the per-task overlay as authoritative.
    counts = body["counts"]
    assert counts["completed"] == 1, (
        f"per-task overlay must drive counts; expected 1 completed, "
        f"got {counts!r}"
    )
    assert counts["pending"] == 1, (
        f"per-task overlay must drive counts; expected 1 pending, "
        f"got {counts!r}"
    )

    # The critical contract: ONE open call for the runtime overlay
    # + the aggregated ``task_progress`` read, not two.
    assert len(call_log) == 1, (
        f"get_execution_progress must open the state-machine SQLite "
        f"exactly once for the runtime overlay + task_progress read "
        f"(both repositories share the same handle); got {len(call_log)} "
        f"opens: {[str(p) for p in call_log]}"
    )
    # And the opened path is the one we configured.
    assert call_log[0] == db_path, (
        f"single open must target the configured state DB; "
        f"got {call_log[0]!r}, expected {db_path!r}"
    )


def test_progress_endpoint_returns_runtime_overlay(tmp_path, monkeypatch) -> None:
    """Sanity: the single-connection refactor must still propagate the
    per-task runtime overlay onto ``tasks[i].status``. If the second
    repository accidentally re-uses the first's connection but loses
    the read, this test catches it.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_id = "plan-overlay"

    _write_plan_tasks_file(plan_id, plans_dir)
    _seed_state_db(db_path, plan_id, project_dir)

    import server

    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)

    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.get(f"/api/execution/{plan_id}/progress")
    assert resp.status_code == 200
    body = resp.json()

    # Per-task overlay must have overwritten the static
    # ``status='pending'`` from tasks.json with ``status='completed'``
    # for task 1 and ``status='pending'`` for task 2.
    by_id = {t["id"]: t for t in body["tasks"]}
    assert by_id["1"]["status"] == "completed", (
        f"task 1 status must come from the per-task overlay; "
        f"got {by_id['1']['status']!r}"
    )
    assert by_id["2"]["status"] == "pending", (
        f"task 2 status must come from the per-task overlay; "
        f"got {by_id['2']['status']!r}"
    )
