"""Integration coverage for SQLite-backed execution routes.

Task 10 (this file)
-------------------
The routes ``/api/execution/{id}/start`` and
``/api/execution/{id}/progress`` previously read / wrote
``plans/{id}/execution.json`` and an in-memory ``_execution_state``
dict.  The state-machine refactor replaces both with the
:class:`RoutingRepository` (CAS for ``start_execution``) and the
:class:`ExecutionRepository` (for ``project_dir`` /
``task_progress`` / ``current_phase``).

Architecture decision point 3 pins the contract:

  * ``POST /api/execution/{id}/start`` must call
    ``RoutingRepository.try_mark_phase(pid, ('ready',
    'failed', 'completed'), 'executing', ...)`` — the
    CAS predicate makes concurrent ``start`` calls safe (1 wins, 1
    fails with 409).
  * Archived plans (``dir <= 2026-08-05``) return HTTP 410.
  * ``GET /api/execution/{id}/progress`` reads ``plan_routing.stage``
    + ``plan_execution.project_dir`` directly; no in-memory
    aggregation. Per-task counts are aggregated from the
    ``plan_tasks`` table (plan v4, 2026-09-09) — the legacy
    ``plan_execution.task_progress`` JSON column is deprecated and
    never read.

TDD spec (4 tests, the same ones pinned by the task brief):

  1. test_start_execution_succeeds_from_ready
     A plan with ``stage=ready`` accepts ``start`` and the
     stage CAS-advances to ``executing``; ``plan_execution`` carries
     the new ``current_phase=executing`` and ``project_dir``.

  2. test_start_execution_409_when_already_executing
     After ``start`` succeeds, a second ``start`` returns 409 with
     ``{"error": "conflict", "reason": "stage_mismatch"}``.

  3. test_start_execution_410_for_archived_plan
     An archived plan (dirname pre-2026-08-05) returns HTTP 410.

  4. test_progress_reflects_db_state_immediately
     After ``start``, ``GET /api/execution/{id}/progress`` reflects
     the ``plan_tasks`` counts and ``current_phase=executing``
     directly from SQLite — no in-memory aggregation.

  2026-09-19: the former test 5
  (``test_task_progress_update_atomic_read_modify_write``) was
  retired. It pinned the application-level read-modify-write over the
  ``plan_execution.task_progress`` JSON column, which plan v4 replaced
  with ``INSERT ... ON CONFLICT DO UPDATE`` against ``plan_tasks`` —
  a contract that no longer has a JSON column to protect. The
  concurrency guarantee it was defending now lives in
  ``state_machine/tests/unit/test_plan_task_repository_v4.py::
  test_concurrent_writers_no_lost_update``, and the retirement of the
  JSON column itself is pinned by
  ``test_execution_repository_v4_progress.py::
  test_progress_ignores_legacy_task_progress_json_column``.

Setup strategy
--------------
A per-test SQLite file is opened via :func:`open` and migrated;
``server.PLANS_DIR`` and ``server._state_db_path`` are monkeypatched
to point at ``tmp_path / "state.db"`` so the routes read the same
file.  We also stub ``subprocess.Popen`` and ``threading.Thread`` so
no real subprocess is spawned — the unit of testing is the route's
state-machine contract, not the executor.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _DormantThread:
    """A ``threading.Thread`` stub that never actually starts a thread.

    The route starts a daemon thread to read the subprocess stdout;
    for state-machine contract testing we don't want a real thread,
    so we replace the ``Thread`` constructor with this class.
    """

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


class _StubPopen:
    """A ``subprocess.Popen`` stub that pretends to start a process.

    The route records ``state["pid"] = process.pid``; we just give it
    a stable integer so the response payload is deterministic.  The
    ``stdout`` iterator is empty so the daemon thread exits
    immediately.
    """

    def __init__(self, *args, **kwargs):
        self.pid = 99999
        self._stdout = iter([])
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode


@pytest.fixture
def execution_env(monkeypatch, tmp_path):
    """Per-test state-machine + PLANS_DIR + subprocess isolation.

    Mirrors ``verification_env`` in ``test_verification_routes.py``.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()

    # Block real subprocess spawn + daemon thread so the route's
    # background machinery doesn't leak across tests.
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)
    monkeypatch.setattr(server.subprocess, "Popen", _StubPopen)

    yield plans_dir, conn, db_path

    server._execution_state.clear()
    conn.close()


def _seed_plan(
    plans_dir: Path,
    conn: sqlite3.Connection,
    plan_id: str,
    stage: str,
    *,
    project_dir: Path | None = None,
) -> Path:
    """Create the on-disk + SQLite row for one plan.

    Returns the ``plan_dir`` so callers can write additional JSON
    fixtures (e.g. ``tasks.json``) into it.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    target = project_dir or (plan_dir / "project")
    target.mkdir(parents=True, exist_ok=True)
    # ``tasks.json`` is required by the route (it 404s without it).
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    # Insert routing + execution rows so the route's CAS has a row
    # to read.
    RoutingRepository(conn).insert(plan_id, stage)
    ExecutionRepository(conn).insert(
        plan_id=plan_id,
        current_phase=stage,
        project_dir=str(target),
    )
    return plan_dir


# ---------------------------------------------------------------------------
# 1. test_start_execution_succeeds_from_ready
# ---------------------------------------------------------------------------


def test_start_execution_succeeds_from_ready(execution_env):
    """stage=ready + POST /api/execution/{id}/start → 200 + CAS advances."""
    plans_dir, conn, _db_path = execution_env
    plan_id = "20260810-tasks-ready-plan"
    _seed_plan(plans_dir, conn, plan_id, "ready")

    project_dir = plans_dir / "project" / plan_id
    project_dir.mkdir(parents=True, exist_ok=True)
    response = TestClient(app).post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )

    assert response.status_code == 200, (
        f"start_execution returned HTTP {response.status_code}; expected 200; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert body.get("plan_id") == plan_id, (
        f"response must echo plan_id; got {body!r}"
    )

    # The routing row must have CAS'd to executing.
    route = RoutingRepository(conn).current(plan_id)
    assert route is not None, f"plan_routing row missing for {plan_id}"
    assert route["current_phase"] == "executing", (
        f"routing stage must CAS to 'executing' after start; "
        f"got stage={route['stage']!r}; full row={route!r}"
    )

    # The execution row must carry the new current_phase + project_dir.
    summary = ExecutionRepository(conn).summary(plan_id)
    assert summary is not None, (
        f"plan_execution row missing for {plan_id} after start"
    )
    assert summary["current_phase"] == "executing", (
        f"plan_execution.current_phase must be 'executing'; got "
        f"{summary['current_phase']!r}; full summary={summary!r}"
    )
    assert summary["project_dir"] == str(project_dir.resolve()), (
        f"plan_execution.project_dir must be persisted; got "
        f"{summary['project_dir']!r}; full summary={summary!r}"
    )


# ---------------------------------------------------------------------------
# 2. test_start_execution_409_when_already_executing  (bug 2 anchor)
# ---------------------------------------------------------------------------


@pytest.mark.bug_2
def test_start_execution_409_when_already_executing(execution_env):
    """Second POST /api/execution/{id}/start → 409 stage_mismatch.

    **Bug 2 anchor**:  the old implementation accepted both calls
    silently — both passed through to ``subprocess.Popen``, leading
    to two executors racing on the same plan.  The CAS in
    ``RoutingRepository.try_mark_phase`` rejects the second call
    because ``stage='executing'`` is NOT in
    ``expected_phases=('ready', 'failed',
    'completed')``.
    """
    plans_dir, conn, _db_path = execution_env
    plan_id = "20260810-twice-start"
    _seed_plan(plans_dir, conn, plan_id, "ready")

    project_dir = plans_dir / "project" / plan_id
    project_dir.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)

    first = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert first.status_code == 200, (
        f"first start_execution must succeed; got HTTP {first.status_code}; "
        f"body={first.text!r}"
    )

    second = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert second.status_code == 409, (
        f"second start_execution must be 409 (CAS rejects double-start); "
        f"got HTTP {second.status_code}; body={second.text!r}"
    )
    body = second.json()
    assert body.get("error") == "conflict", (
        f"409 body must report error=conflict; got {body!r}"
    )
    assert body.get("reason") == "stage_mismatch", (
        f"409 reason must be stage_mismatch (not version_mismatch); "
        f"got reason={body.get('reason')!r}; full body={body!r}"
    )


# ---------------------------------------------------------------------------
# 3. test_start_execution_410_for_archived_plan
# ---------------------------------------------------------------------------


def test_start_execution_410_for_archived_plan(execution_env):
    """Archived plan (dirname pre-2026-08-05) → 410 archived."""
    plans_dir, conn, _db_path = execution_env
    archived_plan_id = "20260801-archived-execution"
    _seed_plan(plans_dir, conn, archived_plan_id, "ready")

    project_dir = plans_dir / "project" / archived_plan_id
    project_dir.mkdir(parents=True, exist_ok=True)

    response = TestClient(app).post(
        f"/api/execution/{archived_plan_id}/start",
        json={"project_dir": str(project_dir)},
    )

    assert response.status_code == 410, (
        f"archived plan must return 410; got HTTP {response.status_code}; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert body.get("error") == "archived", (
        f"410 body must report error=archived; got {body!r}"
    )

    # The routing row MUST NOT have advanced — the archived rejection
    # is a pre-CAS guard.
    route = RoutingRepository(conn).current(archived_plan_id)
    assert route is not None
    assert route["current_phase"] == "ready", (
        f"archived plan's stage must NOT advance; got "
        f"stage={route['stage']!r}; full row={route!r}"
    )


# ---------------------------------------------------------------------------
# 4. test_progress_reflects_db_state_immediately
# ---------------------------------------------------------------------------


def test_progress_reflects_db_state_immediately(execution_env):
    """``GET /api/execution/{id}/progress`` reads from SQLite only.

    After ``start`` advances the CAS to ``executing`` and persists
    ``project_dir`` + ``task_progress`` to ``plan_execution``, the
    progress endpoint must reflect that state **without** any
    in-memory ``_execution_state`` aggregation.  We poison
    ``_execution_state`` with a sentinel dict; the response must NOT
    surface the sentinel — it must surface the DB row.
    """
    plans_dir, conn, _db_path = execution_env
    plan_id = "20260810-progress-from-db"
    _seed_plan(plans_dir, conn, plan_id, "ready")

    project_dir = plans_dir / "project" / plan_id
    project_dir.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)

    start_resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert start_resp.status_code == 200, (
        f"start_execution must succeed; got HTTP {start_resp.status_code}; "
        f"body={start_resp.text!r}"
    )

    # Seed a deterministic per-task distribution so the progress
    # endpoint has something concrete to surface.
    #
    # 2026-09-19: the counts are aggregated from the ``plan_tasks``
    # table, not the deprecated ``plan_execution.task_progress`` JSON
    # column. Schema v4 (2026-09-09) moved
    # per-task runtime state into ``plan_tasks`` and left the JSON
    # column as an empty deprecated remnant — ``ExecutionRepository
    # .progress`` runs ``SELECT status, COUNT(*) FROM plan_tasks ...
    # GROUP BY status`` and never reads the column again. Seeding the
    # column therefore produced all-zero counts.
    expected_progress = {
        "completed": 3,
        "total": 10,
        "failed": 1,
        "in_progress": 2,
        "pending": 4,
    }
    plan_tasks = PlanTaskRepository(conn)
    statuses = (
        ["completed"] * expected_progress["completed"]
        + ["failed"] * expected_progress["failed"]
        + ["in_progress"] * expected_progress["in_progress"]
        + ["pending"] * expected_progress["pending"]
    )
    for index, status in enumerate(statuses):
        plan_tasks.update_task(
            plan_id=plan_id, task_id=f"T{index:02d}", fields={"status": status},
        )

    # Poison the in-memory state — the route must NOT use this.
    server._execution_state[plan_id] = {
        "status": "running",
        "pid": 4242,
        "project_dir": str(project_dir),
        "should_not_leak": "sentinel_value",
    }

    progress_resp = client.get(f"/api/execution/{plan_id}/progress")

    assert progress_resp.status_code == 200, (
        f"progress returned HTTP {progress_resp.status_code}; expected 200; "
        f"body={progress_resp.text!r}"
    )
    body = progress_resp.json()

    # The poisoned in-memory state MUST NOT appear in the response —
    # only DB-derived fields.  We assert this by checking the unique
    # sentinel key.
    assert "should_not_leak" not in body, (
        f"in-memory state must NOT leak into the progress response; "
        f"full body={body!r}"
    )
    assert body.get("plan_id") == plan_id, (
        f"progress must echo plan_id; got {body!r}"
    )
    assert body.get("project_dir") == str(project_dir.resolve()), (
        f"project_dir must be read from plan_execution; got "
        f"{body.get('project_dir')!r}; full body={body!r}"
    )
    counts = body.get("counts") or {}
    assert counts.get("completed") == 3, (
        f"counts.completed must be sourced from plan_tasks; "
        f"got counts={counts!r}; full body={body!r}"
    )
    assert counts.get("total") == 10, (
        f"counts.total must be sourced from plan_tasks; "
        f"got counts={counts!r}; full body={body!r}"
    )


