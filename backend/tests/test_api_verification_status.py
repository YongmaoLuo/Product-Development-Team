"""Integration contract for ``GET /api/verification/{plan_id}/status`` terminal responses.

This module pins the public contract of the verification-status
endpoint after the in-memory state-flow repairs (predecessor task 3
and task 6). The endpoint's source of truth is now the SQLite state
machine: ``plan_routing.current_phase`` carries the plan's current phase
(``verification_passed`` once the completed -> verification_passed
transition lands) and ``plan_verification.verification_status``
carries the matching verdict. The endpoint MUST surface that contract
without 500-ing on either terminal or pending rows.

TDD specification
-----------------

The two contracts below are the acceptance boundary for this test
file:

  1. ``test_api_status_returns_passed_after_completed_transition`` —
     A plan whose routing row reads ``current_phase="verification_passed"``
     AND whose verification row reads ``verification_status="passed"``
     must round-trip through the endpoint as HTTP 200 with
     ``verification_status == "passed"`` and ``current_phase ==
     "verification_passed"``. This is the end-state of the migration
     installed by task 6 (the deadlock-fix migration moves a pending
     plan to ``verification_passed``; task 3 unlocked the
     ``completed -> verification_passed`` transition so the state
     machine accepts it). The endpoint must report this converged
     state — not crash, not return a different value.

  2. ``test_api_status_does_not_500_for_pending_plan`` — A plan that
     has just been registered in the state machine and is still
     parked in ``current_phase="ready"`` with
     ``verification_status="pending"`` must return HTTP 200 and
     ``verification_status == "pending"``. This is the regression
     guard: a previous version of the endpoint routed through
     in-memory state and 500'd on plans that had no in-memory entry;
     the new SQLite-backed implementation must treat the DB row as
     sufficient source of truth.

Test isolation strategy
-----------------------
Per-test ``PLANS_DIR`` and ``_state_db_path`` are monkey-patched to
``tmp_path``, matching the pattern in
``tests/integration/test_verification_api.py`` and
``backend/state_machine/tests/integration/test_verification_routes.py``.
The ``_verification_state`` and ``_execution_state`` globals are
cleared in the autouse fixture so no in-memory shortcut can mask a
DB-truth regression.

Each test seeds its own plan dir + SQLite rows so the two cases
cannot leak state into one another.
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
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)


# ---------------------------------------------------------------------------
# Subprocess / thread stubs (mirror the execution-route test fixtures)
# ---------------------------------------------------------------------------


class _DormantThread:
    """A ``threading.Thread`` stub that never actually spawns a thread.

    Prevents the verification worker spawned by ``start_verification``
    from leaking across tests. Although the tests in this module never
    call ``/start`` (they only exercise ``/status``), the stub is
    installed defensively so a future addition does not silently
    spawn a real background verifier.
    """

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def status_env(monkeypatch, tmp_path) -> Iterator[tuple[Path, sqlite3.Connection, Path]]:
    """Per-test PLANS_DIR + state.db isolation.

    The endpoint reads from the SQLite file the request handler
    resolves via :func:`server._state_db_path`, so both the plans
    directory and that function must point at the per-test tmp
    location. ``_verification_state`` is cleared so the DB row
    alone decides the response.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._verification_state.clear()
    if hasattr(server, "_execution_state"):
        server._execution_state.clear()
    if hasattr(server, "_execution_locks"):
        server._execution_locks.clear()

    # Defensive: replace the threading.Thread class so the route's
    # background machinery cannot leak across tests, even though the
    # tests in this module only exercise the read endpoint.
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)

    yield plans_dir, conn, db_path

    server._verification_state.clear()
    conn.close()


# ---------------------------------------------------------------------------
# Plan seeding helpers
# ---------------------------------------------------------------------------


def _seed_plan_dir(plans_dir: Path, plan_id: str) -> Path:
    """Create the plan dir + minimum on-disk files the route needs.

    The endpoint checks ``plan_dir.exists()`` before reading the
    state-machine tables, so the directory must be on disk even
    when the test's source of truth is purely the SQLite row.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps({"requirement": "verification status contract test"}),
        encoding="utf-8",
    )
    return plan_dir


def _seed_routing(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    phase: str,
) -> None:
    """Insert a ``plan_routing`` row carrying the supplied ``phase``."""
    RoutingRepository(conn).insert(plan_id, phase)


def _seed_verification(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    status: str,
) -> None:
    """Insert a ``plan_verification`` row carrying the supplied status."""
    VerificationRepository(conn).insert(plan_id, status)


# ---------------------------------------------------------------------------
# TDD spec 1: passed terminal after the completed transition
# ---------------------------------------------------------------------------


def test_api_status_returns_passed_after_completed_transition(status_env):
    """A plan converged to ``passed`` round-trips as 200 + status=passed.

    The two predecessor tasks installed:

      * Task 3 — ``VERIFICATION_PHASE_TRANSITIONS["completed"]`` now
        declares ``["verification_passed", "verification_failed"]``,
        so the state machine accepts the legal edge out of the
        dead-end ``completed`` phase.
      * Task 6 — ``migrate_20260805_deadlock`` converges a plan
        whose ``verification_report.json`` carries ``overall_status ==
        "PASSED"`` from ``pending`` into
        ``current_phase == "verification_passed"`` /
        ``verification.status == "passed"`` and persists that
        combined state.

    After the migration runs, the on-disk SQLite state-machine rows
    must reflect the convergence: ``plan_routing.current_phase ==
    "verification_passed"`` and
    ``plan_verification.verification_status == "passed"``. The
    endpoint MUST surface that exact pair as HTTP 200 with
    ``verification_status == "passed"`` and ``current_phase ==
    "verification_passed"``.

    A regression to the in-memory flow (e.g. accidentally routing
    the endpoint through the ``_verification_state`` global that no
    longer holds a record for this plan) would 500 or return a
    non-``passed`` value, both of which this test catches.
    """
    plans_dir, conn, _db_path = status_env
    plan_id = "20260806-passed-after-completed-transition"
    _seed_plan_dir(plans_dir, plan_id)
    _seed_routing(conn, plan_id, phase="verification_passed")
    _seed_verification(conn, plan_id, status="passed")

    client = TestClient(app)
    resp = client.get(f"/api/verification/{plan_id}/status")

    # Contract 1: HTTP 200, not 5xx. The migration succeeded so the
    # endpoint must return success, not an internal error.
    assert resp.status_code == 200, (
        f"a plan converged to verification_passed must return 200; "
        f"got HTTP {resp.status_code} body={resp.text!r}"
    )

    payload = resp.json()

    # Contract 2: verification_status field reflects the terminal
    # verdict from the SQLite row, not the in-memory default.
    assert payload.get("verification_status") == "passed", (
        f"verification_status must be 'passed' after the "
        f"completed -> verification_passed transition; got "
        f"{payload.get('verification_status')!r}; full payload: {payload!r}"
    )

    # Contract 3: the phase field carries the routing row's value, so
    # the frontend's "current phase" badge shows the converged
    # terminal phase. Since schema v5 there is only one workflow-state
    # column, so the routing value and the reported phase are the same
    # datum by construction rather than by mirroring.
    assert payload.get("current_phase") == "verification_passed", (
        f"current_phase must be 'verification_passed' after the migration; "
        f"got {payload.get('current_phase')!r}; full payload: {payload!r}"
    )

    # Contract 4: the four legacy top-level fields stay present and
    # correctly typed (the frontend JS keys on them).
    assert isinstance(payload.get("verification_round"), int)
    assert isinstance(payload.get("verification_max_rounds"), int)
    assert isinstance(payload.get("results"), dict)
    assert isinstance(payload.get("repair_tasks"), list)


# ---------------------------------------------------------------------------
# TDD spec 2: pending plan must not 500
# ---------------------------------------------------------------------------


def test_api_status_does_not_500_for_pending_plan(status_env):
    """A pending plan round-trips as 200 + status=pending (no 5xx).

    Boundary case: a plan that has just been registered in the
    state machine (routing row at ``ready``, verification row
    at ``pending``) MUST produce a 200 with
    ``verification_status == "pending"``.

    The previous version of the endpoint routed through
    ``_verification_state`` (in-memory) and fell back to a 500
    when the plan had no in-memory entry — even though the SQLite
    state machine already had full knowledge of the plan. This
    test is the regression guard: a future refactor that pulls
    the endpoint back onto the in-memory path would fail here with
    a 5xx, and the test name spells the contract out.

    The route must additionally keep the existing non-5xx
    behaviour for pending plans, i.e. the stop endpoint's "keep
    the existing non-success contract" boundary condition from the
    spec — for ``/status`` that means HTTP 200 with a valid
    ``verification_status`` of ``"pending"``.
    """
    plans_dir, conn, _db_path = status_env
    plan_id = "20260806-pending-no-500"
    _seed_plan_dir(plans_dir, plan_id)
    _seed_routing(conn, plan_id, phase="ready")
    _seed_verification(conn, plan_id, status="pending")

    client = TestClient(app)
    resp = client.get(f"/api/verification/{plan_id}/status")

    # Contract 1: HTTP 200, not 5xx. The legacy "no in-memory entry
    # -> 500" failure mode would surface here.
    assert resp.status_code == 200, (
        f"a pending plan must NOT 5xx; got HTTP {resp.status_code} "
        f"body={resp.text!r}"
    )

    payload = resp.json()

    # Contract 2: the endpoint reflects the SQLite row's pending
    # status, not some default like "not_started" or "running".
    assert payload.get("verification_status") == "pending", (
        f"verification_status must be 'pending' for a freshly "
        f"registered plan; got {payload.get('verification_status')!r}; "
        f"full payload: {payload!r}"
    )

    # Contract 3: the phase reflects the routing row. The pending
    # verification never entered a verification-running phase, so
    # the phase stays at the executor-owned ``ready``.
    assert payload.get("current_phase") == "ready", (
        f"current_phase must be 'ready' for a pending plan; got "
        f"{payload.get('current_phase')!r}; full payload: {payload!r}"
    )
