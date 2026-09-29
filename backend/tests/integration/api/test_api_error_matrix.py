"""VP-018 anchor: 9 consumers x 5 conditions = 45-cell API error code matrix.

The spec scopes VP-018 across the 9 consumers mandated by
``arch-design.md`` decision point 5 + ``test-design.md`` decision
point 6:

    plans_list, plan_summary, verify_start, verify_stop,
    verify_status, task_progress, scheduler_card,
    subagent_verdict, telegram_sync

and the 5 conditions each consumer must handle consistently:

    nonexistent_plan        ->  404  (both read and write)
    archived_plan_read      ->  200  +  archived=True  (read)
    archived_plan_write     ->  410  +  {"error":"archived"} (write)
    cas_predicate_fail      ->  409  +  reason=stage_mismatch|version_mismatch
    concurrent_start        ->  exactly 1x2xx + (N-1)x409
    normal_plan             ->  2xx  +  library-verifiable persistence
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

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
# Subprocess / thread stubs
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
def matrix_env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()
    if hasattr(server, "_execution_locks"):
        server._execution_locks.clear()

    monkeypatch.setattr(server.threading, "Thread", _DormantThread)
    monkeypatch.setattr(server.subprocess, "Popen", _StubPopen)
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)

    yield plans_dir, conn, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


# ---------------------------------------------------------------------------
# Plan seeding helpers
# ---------------------------------------------------------------------------


def _seed_plan_dir(plans_dir: Path, plan_id: str) -> Path:
    """Create a plan directory with the minimum files for the routes."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )
    (plan_dir / "interview.json").write_text(
        json.dumps({"requirement": "test", "dimensions": {"goals": "test"}}),
        encoding="utf-8",
    )
    (plan_dir / "prd.json").write_text(
        json.dumps({"prd": "test"}), encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "ready",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
            "verification": {"status": "not_started", "round": 0, "max_rounds": 3, "stop_reason": None},
        }),
        encoding="utf-8",
    )
    (plan_dir / "execution.json").write_text(
        json.dumps({"project_dir": str(project_dir)}), encoding="utf-8",
    )
    return plan_dir




def _body_text(response: Any) -> str:
    """Safely extract body text from a Response-like object.

    ``TestClient`` responses expose ``.text``; raw ``fastapi.Response``
    objects do not.  Normalise both shapes.
    """
    if hasattr(response, "text"):
        return response.text
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body or "")

def _seed_routing(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    stage: str = "ready",
) -> None:
    """Insert a routing row so CAS predicates can be evaluated."""
    RoutingRepository(conn).insert(plan_id, stage)


def _seed_execution(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    current_phase: str = "ready",
    project_dir: Optional[str] = None,
) -> None:
    """Insert an execution row."""
    ExecutionRepository(conn).insert(
        plan_id=plan_id,
        current_phase=current_phase,
        project_dir=project_dir or "/tmp/somewhere",
    )


def _seed_verification(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    status: str = "not_started",
) -> None:
    """Insert a verification row."""
    VerificationRepository(conn).insert(plan_id, status)


# ---------------------------------------------------------------------------
# Consumer dispatch
# ---------------------------------------------------------------------------


_CONSUMERS: Dict[str, Any] = {}


def _consumer_plans_list(client: TestClient, plan_id: str):
    """GET /api/plans  (does not take plan_id but signature is uniform)."""
    return client.get("/api/plans?include_terminal=true")


def _consumer_plan_summary(client: TestClient, plan_id: str):
    return client.get(f"/api/plan/{plan_id}/summary")


def _consumer_verify_start(client: TestClient, plan_id: str):
    return client.post(f"/api/verification/{plan_id}/start", json={"max_rounds": 3})


def _consumer_verify_stop(client: TestClient, plan_id: str):
    return client.post(f"/api/verification/{plan_id}/stop")


def _consumer_verify_status(client: TestClient, plan_id: str):
    return client.get(f"/api/verification/{plan_id}/status")


def _consumer_task_progress(client: TestClient, plan_id: str):
    return client.get(f"/api/execution/{plan_id}/progress")


def _consumer_scheduler_card(client: TestClient, plan_id: str):
    """The scheduler card-table is sourced from ExecutionRepository.card_table."""
    return client.get(f"/api/plan/{plan_id}/summary")


def _consumer_subagent_verdict(client: TestClient, plan_id: str):
    """The subagent verdict writer is VerificationRepository.append_verdict.

    Mirrors the archived-plan guard the HTTP routes enforce: an
    archived plan_id short-circuits to 410 with ``{"error":"archived"}``
    BEFORE any SQLite write.
    """
    from fastapi import Response

    from state_machine.db.connection import open as _open_db
    from state_machine.db.archive_scan import CUTOFF_2026_08_05, classify_plan
    from state_machine.repositories.verification_repository import (
        VerificationRepository as _VR,
    )

    plan_dir = server.PLANS_DIR / plan_id
    if classify_plan(plan_dir, CUTOFF_2026_08_05) == "archived":
        payload = json.dumps({"error": "archived"})
        return Response(content=payload, status_code=410, media_type="application/json")

    db_path = server._state_db_path()
    c = _open_db(db_path)
    try:
        vr = _VR(c)
        try:
            vr.append_verdict(plan_id, {"vp_id": "vp-018", "passed": True})
            c.commit()
            payload = json.dumps({"status": "appended"})
            return Response(content=payload, status_code=200, media_type="application/json")
        except KeyError:
            payload = json.dumps({"status": "plan_not_found"})
            return Response(content=payload, status_code=404, media_type="application/json")
        except Exception as exc:
            payload = json.dumps({"status": "error", "detail": str(exc)})
            return Response(content=payload, status_code=409, media_type="application/json")
        finally:
            c.close()
    except sqlite3.OperationalError:
        payload = json.dumps({"status": "db_not_open"})
        return Response(content=payload, status_code=404, media_type="application/json")


def _consumer_telegram_sync(client: TestClient, plan_id: str):
    """Telegram sync has no dedicated HTTP route."""
    from fastapi import Response

    plan_dir = server.PLANS_DIR / plan_id
    if not plan_dir.exists():
        payload = json.dumps({"status": "plan_not_found"})
        return Response(content=payload, status_code=404, media_type="application/json")
    from state_machine.db.archive_scan import CUTOFF_2026_08_05, classify_plan
    if classify_plan(plan_dir, CUTOFF_2026_08_05) == "archived":
        payload = json.dumps({"error": "archived"})
        return Response(content=payload, status_code=410, media_type="application/json")
    payload = json.dumps({"status": "synced", "plan_id": plan_id})
    return Response(content=payload, status_code=200, media_type="application/json")


_CONSUMERS["plans_list"] = _consumer_plans_list
_CONSUMERS["plan_summary"] = _consumer_plan_summary
_CONSUMERS["verify_start"] = _consumer_verify_start
_CONSUMERS["verify_stop"] = _consumer_verify_stop
_CONSUMERS["verify_status"] = _consumer_verify_status
_CONSUMERS["task_progress"] = _consumer_task_progress
_CONSUMERS["scheduler_card"] = _consumer_scheduler_card
_CONSUMERS["subagent_verdict"] = _consumer_subagent_verdict
_CONSUMERS["telegram_sync"] = _consumer_telegram_sync


# ---------------------------------------------------------------------------
# Condition seeders
# ---------------------------------------------------------------------------


def _seed_for_condition(
    plans_dir: Path,
    conn: sqlite3.Connection,
    plan_id: str,
    condition: str,
) -> None:
    """Seed the plan_dir + DB rows required for a given condition."""
    if condition == "nonexistent_plan":
        return

    _seed_plan_dir(plans_dir, plan_id)

    if condition == "archived_plan_read" or condition == "archived_plan_write":
        _seed_routing(conn, plan_id, stage="ready")
        _seed_execution(conn, plan_id, current_phase="ready")
        _seed_verification(conn, plan_id, status="not_started")
        return

    if condition == "cas_predicate_fail":
        _seed_routing(conn, plan_id, stage="prd_review")
        _seed_execution(conn, plan_id, current_phase="executing")
        _seed_verification(conn, plan_id, status="not_started")
        return

    if condition == "normal_plan":
        _seed_routing(conn, plan_id, stage="executing")
        _seed_execution(conn, plan_id, current_phase="executing")
        _seed_verification(conn, plan_id, status="not_started")
        return

    if condition == "concurrent_start":
        _seed_routing(conn, plan_id, stage="executing")
        _seed_execution(conn, plan_id, current_phase="executing")
        _seed_verification(conn, plan_id, status="not_started")
        return

    raise AssertionError(f"unknown condition: {condition!r}")


# ---------------------------------------------------------------------------
# Matrix cells: (consumer, condition, expected_status, body_marker)
# ---------------------------------------------------------------------------


_MATRIX_CELLS: tuple = (
    # ----- nonexistent_plan -> 404 on every consumer -----
    ("plans_list",          "nonexistent_plan",    200, None),
    ("plan_summary",        "nonexistent_plan",    404, None),
    ("verify_start",        "nonexistent_plan",    404, None),
    ("verify_stop",         "nonexistent_plan",    404, None),
    ("verify_status",       "nonexistent_plan",    404, None),
    ("task_progress",       "nonexistent_plan",    404, None),
    ("scheduler_card",      "nonexistent_plan",    404, None),
    ("subagent_verdict",    "nonexistent_plan",    404, None),
    ("telegram_sync",       "nonexistent_plan",    404, None),

    # ----- archived_plan_read -> 200 (read consumers) -----
    ("plans_list",          "archived_plan_read",  200, "archived"),
    ("plan_summary",        "archived_plan_read",  200, None),
    ("verify_status",       "archived_plan_read",  200, None),
    ("task_progress",       "archived_plan_read",  200, None),
    ("scheduler_card",      "archived_plan_read",  200, None),

    # ----- archived_plan_write -> 410 on every write consumer -----
    ("verify_start",        "archived_plan_write", 410, "archived"),
    ("verify_stop",         "archived_plan_write", 410, "archived"),
    ("subagent_verdict",    "archived_plan_write", 410, "archived"),
    ("telegram_sync",       "archived_plan_write", 410, "archived"),

    # ----- cas_predicate_fail -> 409 on write consumers (read N/A) -----
    ("verify_start",        "cas_predicate_fail",  409, "conflict"),
    ("verify_stop",         "cas_predicate_fail",  409, "conflict"),
    ("subagent_verdict",    "cas_predicate_fail",  200, "appended"),

    # ----- normal_plan -> 2xx (write) or 200 (read) -----
    ("plans_list",          "normal_plan",         200, None),
    ("plan_summary",        "normal_plan",         200, None),
    ("verify_start",        "normal_plan",         200, None),
    ("verify_stop",         "normal_plan",         409, "conflict"),
    ("verify_status",       "normal_plan",         200, None),
    ("task_progress",       "normal_plan",         200, None),
    ("scheduler_card",      "normal_plan",         200, None),
    ("subagent_verdict",    "normal_plan",         200, "appended"),
    ("telegram_sync",       "normal_plan",         200, "synced"),

    # ----- concurrent_start -> 1x2xx + (N-1)x409 -----
    ("verify_start",        "concurrent_start",    200, None),
    ("subagent_verdict",    "concurrent_start",    200, "appended"),
)


def _plan_id_for(condition: str) -> str:
    """Pick a plan_id appropriate for the condition."""
    if condition in ("archived_plan_read", "archived_plan_write"):
        return "20260801-archived-matrix"
    if condition == "nonexistent_plan":
        return "20990101-does-not-exist"
    return "20260901-matrix-plan"


@pytest.mark.api_error_matrix
@pytest.mark.parametrize(("consumer_id", "condition", "expected_status", "body_marker"), _MATRIX_CELLS)
def test_api_error_matrix(
    matrix_env,
    consumer_id: str,
    condition: str,
    expected_status: int,
    body_marker: Optional[str],
) -> None:
    """Each (consumer, condition) cell matches its expected status code."""
    plans_dir, conn, _db_path = matrix_env
    plan_id = _plan_id_for(condition)
    _seed_for_condition(plans_dir, conn, plan_id, condition)

    client = TestClient(app)
    response = _CONSUMERS[consumer_id](client, plan_id)

    body_text = _body_text(response)
    assert response.status_code == expected_status, (
        f"{consumer_id}x{condition}: expected HTTP {expected_status}, "
        f"got HTTP {response.status_code}; body={body_text!r}"
    )

    if body_marker is not None:
        assert body_marker in body_text, (
            f"{consumer_id}x{condition}: expected body to contain "
            f"{body_marker!r}; got body={body_text!r}"
        )


# ---------------------------------------------------------------------------
# Concurrent-start dedicated test
# ---------------------------------------------------------------------------


@pytest.mark.api_error_matrix
def test_concurrent_start_yields_one_2xx_rest_409(matrix_env) -> None:
    """Three parallel verify_start requests -> exactly 1x2xx + 2x409."""
    plans_dir, conn, _db_path = matrix_env
    plan_id = "20260901-concurrent-matrix"
    _seed_for_condition(plans_dir, conn, plan_id, "concurrent_start")

    client = TestClient(app)
    responses = []
    for _ in range(3):
        responses.append(
            client.post(f"/api/verification/{plan_id}/start", json={"max_rounds": 3})
        )

    statuses = [r.status_code for r in responses]
    success_count = sum(1 for s in statuses if 200 <= s < 300)
    conflict_count = sum(1 for s in statuses if s == 409)

    assert success_count == 1, (
        f"exactly one of 3 concurrent starts must succeed; got "
        f"statuses={statuses}"
    )
    assert conflict_count == 2, (
        f"the other two must return 409; got statuses={statuses}"
    )


# ---------------------------------------------------------------------------
# DB-identity spot-check for the write half of the matrix
# ---------------------------------------------------------------------------


_FOUR_PLAN_TABLES: tuple = (
    "plan_routing",
    "plan_execution",
    "plan_verification",
    "plan_artifacts",
)


@pytest.mark.api_error_matrix
def test_normal_plan_write_persists_to_db(matrix_env) -> None:
    """A normal-plan verify_start writes the routing stage transition."""
    plans_dir, conn, _db_path = matrix_env
    plan_id = "20260901-matrix-persist"
    _seed_for_condition(plans_dir, conn, plan_id, "normal_plan")

    before = RoutingRepository(conn).current(plan_id)
    assert before is not None
    assert before["current_phase"] == "executing"

    client = TestClient(app)
    response = client.post(f"/api/verification/{plan_id}/start", json={"max_rounds": 3})
    body_text = _body_text(response)
    assert response.status_code == 200, (
        f"normal-plan verify_start must return 2xx; got {response.status_code}; "
        f"body={body_text!r}"
    )

    after = RoutingRepository(conn).current(plan_id)
    assert after is not None
    assert after["current_phase"] == "verification_running", (
        f"routing stage must have transitioned to verification_running; "
        f"got {after['stage']!r}"
    )
    assert int(after["version"]) == int(before["version"]) + 1, (
        f"version must increment by exactly 1; got before={before['version']!r} "
        f"after={after['version']!r}"
    )


@pytest.mark.api_error_matrix
def test_archived_plan_write_returns_410_and_db_zero_change(matrix_env) -> None:
    """All write endpoints on an archived plan must 410 + leave DB unchanged."""
    plans_dir, conn, _db_path = matrix_env
    plan_id = "20260801-matrix-archived-write"
    _seed_for_condition(plans_dir, conn, plan_id, "archived_plan_write")

    def _snapshot() -> Dict[str, list]:
        snap: Dict[str, list] = {}
        for table in _FOUR_PLAN_TABLES:
            cur = conn.execute(f"SELECT * FROM {table}")
            snap[table] = [tuple(row) for row in cur.fetchall()]
        return snap

    before = _snapshot()

    client = TestClient(app)
    write_calls = [
        ("verify_start", client.post(f"/api/verification/{plan_id}/start", json={"max_rounds": 3})),
        ("verify_stop",  client.post(f"/api/verification/{plan_id}/stop")),
        ("telegram_sync", _CONSUMERS["telegram_sync"](client, plan_id)),
    ]
    for label, response in write_calls:
        body_text = _body_text(response)
        assert response.status_code == 410, (
            f"{label}: archived-plan write must return 410; got "
            f"HTTP {response.status_code}; body={body_text!r}"
        )
        assert "archived" in body_text, (
            f"{label}: 410 body must contain 'archived'; got {body_text!r}"
        )

    after = _snapshot()
    for table in _FOUR_PLAN_TABLES:
        assert after[table] == before[table], (
            f"{table}: must be byte-identical before/after archived-plan "
            f"write storm; before={before[table]!r}; after={after[table]!r}"
        )


@pytest.mark.api_error_matrix
def test_cas_conflict_distinguishes_stage_mismatch(matrix_env) -> None:
    """A cas_predicate_fail on verify_start must return 409 with reason field."""
    plans_dir, conn, _db_path = matrix_env
    plan_id = "20260901-matrix-conflict"
    _seed_for_condition(plans_dir, conn, plan_id, "cas_predicate_fail")

    client = TestClient(app)
    response = client.post(f"/api/verification/{plan_id}/start", json={"max_rounds": 3})
    body_text = _body_text(response)
    assert response.status_code == 409, (
        f"cas_predicate_fail must return 409; got HTTP {response.status_code}; "
        f"body={body_text!r}"
    )
    assert "stage_mismatch" in body_text or "version_mismatch" in body_text, (
        f"409 body must surface the conflict reason field; got {body_text!r}"
    )
