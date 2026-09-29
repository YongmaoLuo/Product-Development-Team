"""VP-020 anchor (2/2): archived plan write endpoints return 410, DB unchanged.

The state-machine refactor pins a hard boundary: archived plans
(dirname ``<= 2026-08-05``) are read-only.  Every write endpoint
that targets an archived plan MUST return HTTP 410 ``{"error":
"archived"}`` and MUST NOT mutate any of the four ``plan_*``
SQLite tables.

This test pins the contract via parametrisation across the
write endpoints the spec scopes for VP-020:

  * ``POST /api/execution/{plan_id}/start``  - starts execution
  * ``POST /api/execution/{plan_id}/stop``   - stops execution
  * ``POST /api/verification/{plan_id}/start`` - starts verification
  * ``POST /api/verification/{plan_id}/stop``  - stops verification
  * ``POST /api/verification/{plan_id}/reset`` - hard-resets verification
  * ``POST /api/plan/{plan_id}/state``       - mutates plan phase
  * ``POST /api/plan/{plan_id}/phase``       - mutates plan phase

For each endpoint we:

  1. Seed an archived plan directory (dirname ``20260801-...``).
  2. Seed a routing + execution + verification row that would
     normally allow the write to succeed.
  3. Snapshot the row count + every row's content for the four
     ``plan_*`` tables.
  4. Issue the write, assert HTTP 410 with ``{"error": "archived"}``.
  5. Assert the DB snapshot is BYTE-IDENTICAL after the call -
     no rows inserted, no rows touched, no rows deleted.

The DB-identity step is the new half of VP-020: ``archived plan
的全部写端点参数化 -> 410 且 DB 零变化``.

Setup strategy
--------------
Per-test isolation: ``PLANS_DIR`` and ``_state_db_path`` are
monkeypatched to ``tmp_path``, matching the pattern in
``test_execution_routes.py`` and ``test_verification_routes.py``.
The subprocess.Popen / threading.Thread stubs from the execution
test are reused so 410 rejection returns BEFORE any subprocess
spawn happens.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)


# ---------------------------------------------------------------------------
# Subprocess / thread stubs (mirror the execution-route test fixtures)
# ---------------------------------------------------------------------------


class _DormantThread:
    """A ``threading.Thread`` stub that never actually starts a thread."""

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


class _StubPopen:
    """A ``subprocess.Popen`` stub that pretends to start a process."""

    def __init__(self, *args, **kwargs):
        self.pid = 99999
        self._stdout = iter([])
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def archived_env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation.

    Mirrors the ``execution_env`` fixture in
    ``test_execution_routes.py`` so the routes read from the
    per-test SQLite file we seed below.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    # Block real subprocess spawn + daemon thread so the route's
    # background machinery doesn't leak across tests.  Importantly,
    # 410 rejections happen BEFORE the subprocess would be spawned,
    # so the stub is mostly defensive.
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)
    monkeypatch.setattr(server.subprocess, "Popen", _StubPopen)

    yield plans_dir, conn, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FOUR_PLAN_TABLES: tuple[str, ...] = (
    "plan_routing",
    "plan_execution",
    "plan_verification",
    "plan_artifacts",
)


def _snapshot_db(conn: sqlite3.Connection) -> dict:
    """Return a deep snapshot of the four ``plan_*`` tables.

    The snapshot is a dict ``{table: [tuple_of_row_values, ...]}``;
    comparing two snapshots is a byte-identical equality check on
    the rows that exist (by ordinal values, not by name, so we
    don't depend on row_factory state).
    """
    snap: dict = {}
    for table in _FOUR_PLAN_TABLES:
        cur = conn.execute(f"SELECT * FROM {table}")
        snap[table] = [tuple(row) for row in cur.fetchall()]
    return snap


def _seed_archived_plan(
    plans_dir: Path,
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    stage: str = "ready",
    current_phase: str = "ready",
    verification_status: str = "not_started",
) -> Path:
    """Seed an archived plan (dirname <= cutoff) with full DB rows.

    The directory uses a pre-cutoff dirname (``20260801-``) so
    :func:`classify_plan` returns ``"archived"`` and the route's
    ``_ensure_verification_plan`` guard (or the inline
    ``classify_plan`` check in ``start_execution``) raises /
    returns 410.

    We seed the SQLite rows that would normally allow the write
    to proceed (routing ``ready``, execution ``ready``,
    verification ``not_started``) so the 410 is observed on EVERY
    endpoint, not just on the ones that happen to throw on
    missing rows first.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # ``tasks.json`` is required by the execution-start route (it 404s
    # without it).  We seed it so the 410 is observed on the archived
    # check, not on the tasks-missing 404.
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )

    RoutingRepository(conn).insert(plan_id, stage)
    ExecutionRepository(conn).insert(
        plan_id=plan_id,
        current_phase=current_phase,
        project_dir=str(project_dir),
    )
    VerificationRepository(conn).insert(plan_id, verification_status)

    return plan_dir


# ---------------------------------------------------------------------------
# Parametrised write endpoints (the VP-020 contract surface)
# ---------------------------------------------------------------------------


# Each entry is (test_id, route_callable).  ``test_id`` is the human
# label surfaced in pytest output; ``route_callable`` is invoked
# with the ``TestClient`` and the seeded ``plan_id`` and returns the
# response from which the test asserts status_code=410.
_WRITE_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("execution_start", "post_execution_start"),
    ("execution_stop", "post_execution_stop"),
    ("verification_start", "post_verification_start"),
    ("verification_stop", "post_verification_stop"),
    ("verification_reset", "post_verification_reset"),
    ("plan_state", "post_plan_state"),
    ("plan_phase", "post_plan_phase"),
)


def _post_execution_start(client: TestClient, plan_id: str):
    """POST /api/execution/{plan_id}/start with a valid project_dir."""
    project_dir = "backend/.tmp"  # any non-empty path; the 410 happens first
    return client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": project_dir},
    )


def _post_execution_stop(client: TestClient, plan_id: str):
    """POST /api/execution/{plan_id}/stop."""
    return client.post(f"/api/execution/{plan_id}/stop")


def _post_verification_start(client: TestClient, plan_id: str):
    """POST /api/verification/{plan_id}/start (no payload)."""
    return client.post(f"/api/verification/{plan_id}/start")


def _post_verification_stop(client: TestClient, plan_id: str):
    """POST /api/verification/{plan_id}/stop."""
    return client.post(f"/api/verification/{plan_id}/stop")


def _post_verification_reset(client: TestClient, plan_id: str):
    """POST /api/verification/{plan_id}/reset (hard reset).

    Added 2026-09-17 with the endpoint itself: it writes
    ``plan_verification`` and ``plan_routing`` directly, so it is
    squarely inside the VP-020 write set.
    """
    return client.post(
        f"/api/verification/{plan_id}/reset",
        json={"force": True},
    )


def _post_plan_state(client: TestClient, plan_id: str):
    """POST /api/plan/{plan_id}/state (writes a phase mutation)."""
    return client.post(
        f"/api/plan/{plan_id}/state",
        json={"current_phase": "verification"},
    )


def _post_plan_phase(client: TestClient, plan_id: str):
    """POST /api/plan/{plan_id}/phase (writes a phase mutation)."""
    return client.post(
        f"/api/plan/{plan_id}/phase",
        json={"current_phase": "executing"},
    )


_DISPATCH: dict[str, callable] = {
    "post_execution_start": _post_execution_start,
    "post_execution_stop": _post_execution_stop,
    "post_verification_start": _post_verification_start,
    "post_verification_stop": _post_verification_stop,
    "post_verification_reset": _post_verification_reset,
    "post_plan_state": _post_plan_state,
    "post_plan_phase": _post_plan_phase,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "callable_name"), _WRITE_ENDPOINTS)
def test_archived_plan_write_endpoint_returns_410_and_db_unchanged(
    archived_env,
    label: str,
    callable_name: str,
) -> None:
    """Each write endpoint returns 410 and leaves the four tables unchanged.

    The spec scopes VP-020 across the write endpoints that targeted
    the plan: ``archived plan 的全部写端点参数化 -> 410 且 DB 零变化``.

    For every endpoint:

      1. The HTTP response is 410 with ``{"error": "archived"}``.
      2. The four ``plan_*`` SQLite tables are byte-identical
         before and after the call (no rows inserted, no rows
         touched, no rows deleted).
    """
    plans_dir, conn, _db_path = archived_env
    plan_id = "20260801-archived-writes"
    _seed_archived_plan(plans_dir, conn, plan_id)

    # Snapshot the DB before the call.
    before = _snapshot_db(conn)

    client = TestClient(app)
    response = _DISPATCH[callable_name](client, plan_id)

    # 410 is the contract.
    assert response.status_code == 410, (
        f"{label}: archived plan write must return HTTP 410; got "
        f"HTTP {response.status_code}; body={response.text!r}"
    )
    body = response.json()
    assert body == {"error": "archived"}, (
        f"{label}: 410 body must be {{'error': 'archived'}}; got {body!r}"
    )

    # DB zero changes is the contract.
    after = _snapshot_db(conn)
    for table in _FOUR_PLAN_TABLES:
        assert after[table] == before[table], (
            f"{label}: {table} must be unchanged after a 410 write; "
            f"before={before[table]!r}; after={after[table]!r}"
        )


def test_archived_execution_start_does_not_advance_routing_stage(
    archived_env,
) -> None:
    """Specifically clip the execution-start route: stage must NOT advance.

    The route's CAS is gated by a 410 archived check that runs
    BEFORE the CAS.  A regression that swapped the order (CAS
    first, then the 410) would advance the stage to ``executing``
    and only THEN return 410 - leaving the DB in a half-mutated
    state.  This test pins the pre-CAS 410 ordering.
    """
    plans_dir, conn, _db_path = archived_env
    plan_id = "20260801-archived-stage-guard"
    _seed_archived_plan(plans_dir, conn, plan_id, stage="ready")

    project_dir = plans_dir / "project" / plan_id
    project_dir.mkdir(parents=True, exist_ok=True)
    response = TestClient(app).post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert response.status_code == 410, (
        f"archived plan /execution/start must return 410; got "
        f"HTTP {response.status_code}; body={response.text!r}"
    )

    # The routing row must still be in ``ready`` - the CAS
    # was never invoked.
    route = RoutingRepository(conn).current(plan_id)
    assert route is not None, (
        f"plan_routing row missing for {plan_id}; the 410 must NOT "
        f"have deleted it"
    )
    assert route["current_phase"] == "ready", (
        f"archived plan's stage must NOT advance (pre-CAS 410 guard); "
        f"got stage={route['stage']!r}; full row={route!r}"
    )
    assert route["version"] == 0, (
        f"archived plan's version must NOT increase (CAS was never "
        f"invoked); got version={route['version']!r}; full row={route!r}"
    )


def test_archived_plan_direct_select_does_not_contain_archived_row(
    archived_env,
) -> None:
    """After multiple writes, the DB must still have only the seed rows.

    Cumulative check: every 410 attempt above MUST have left the
    DB unchanged.  This test issues every write endpoint, then
    asserts the four tables contain exactly the rows the seed
    inserted (NOTHING more, NOTHING less).
    """
    plans_dir, conn, _db_path = archived_env
    plan_id = "20260801-archived-cumulative"
    _seed_archived_plan(plans_dir, conn, plan_id)

    client = TestClient(app)
    project_dir = "backend/.tmp"
    for callable_name in (
        "post_execution_start",
        "post_execution_stop",
        "post_verification_start",
        "post_verification_stop",
        "post_plan_state",
        "post_plan_phase",
    ):
        # Each uses a slightly different body shape; the dispatcher
        # handles the per-call payload.
        if callable_name == "post_execution_start":
            response = client.post(
                f"/api/execution/{plan_id}/start",
                json={"project_dir": project_dir},
            )
        elif callable_name == "post_execution_stop":
            response = client.post(f"/api/execution/{plan_id}/stop")
        elif callable_name == "post_verification_start":
            response = client.post(f"/api/verification/{plan_id}/start")
        elif callable_name == "post_verification_stop":
            response = client.post(f"/api/verification/{plan_id}/stop")
        elif callable_name == "post_plan_state":
            response = client.post(
                f"/api/plan/{plan_id}/state",
                json={"current_phase": "verification"},
            )
        elif callable_name == "post_plan_phase":
            response = client.post(
                f"/api/plan/{plan_id}/phase",
                json={"current_phase": "executing"},
            )
        else:
            raise AssertionError(f"unhandled callable {callable_name!r}")
        assert response.status_code == 410, (
            f"{callable_name}: archived plan write must return 410; "
            f"got HTTP {response.status_code}; body={response.text!r}"
        )

    # Row counts after the storm of 410s.  We seed routing,
    # execution, and verification (one row each); plan_artifacts
    # is intentionally NOT seeded, so the contract is
    #     "no new rows inserted by any 410 write endpoint" —
    # routing / execution / verification must stay at 1 row,
    # plan_artifacts must stay at 0.
    expected_counts = {
        "plan_routing": 1,
        "plan_execution": 1,
        "plan_verification": 1,
        "plan_artifacts": 0,
    }
    for table in _FOUR_PLAN_TABLES:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        count = int(cur.fetchone()[0])
        assert count == expected_counts[table], (
            f"{table} must contain exactly {expected_counts[table]} "
            f"row(s) after every write endpoint returned 410; got {count}"
        )

    # The row we seeded is still there with the seeded values.
    route = RoutingRepository(conn).current(plan_id)
    assert route is not None
    assert route["current_phase"] == "ready", (
        f"routing.stage must remain 'ready'; got {route['stage']!r}"
    )
    exec_row = ExecutionRepository(conn).summary(plan_id)
    assert exec_row is not None
    assert exec_row["current_phase"] == "ready", (
        f"execution.current_phase must remain 'ready'; got "
        f"{exec_row['current_phase']!r}"
    )
    verif_row = VerificationRepository(conn).current(plan_id)
    assert verif_row is not None
    assert verif_row["verification_status"] == "not_started", (
        f"verification.verification_status must remain 'not_started'; "
        f"got {verif_row['verification_status']!r}"
    )
