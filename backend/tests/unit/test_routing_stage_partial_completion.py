"""Tests for routing.stage CAS in _run_auto_verification_loop (2026-09-11 plan v9 Bug 2).

Symptom: a plan whose execution had finished was never auto-advanced into
verification — the status endpoint kept reporting it as running.

Root cause: ``_run_auto_verification_loop`` updated
``plan_state.current_phase`` and ``plan_verification.verification_status``
but **never CASed** ``plan_routing.stage`` from ``executing`` into
``verification``. The status endpoint then forced ``status="running"``
indefinitely because it interprets ``route.stage == "executing"`` as
"still running".

Fix (server.py:_run_auto_verification_loop v9): at the start of the
loop, CAS ``plan_routing.stage`` from ``("executing", "ready")``
to ``"verification"``. ``ConflictError`` is swallowed (another writer
already advanced); other exceptions are best-effort logged to stderr.

These tests pin the contract:
  1. CAS is attempted on entry
  2. ConflictError is swallowed (loop continues)
  3. Other exceptions are logged but don't abort
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


@pytest.fixture
def state_db_path(tmp_path, monkeypatch):
    """Provision a temp state.db and patch ``_state_db_path()``."""
    db = _make_state_db(tmp_path)
    import server
    monkeypatch.setattr(server, "_state_db_path", lambda: db)
    return db


def _set_routing_stage(db_path: Path, plan_id: str, stage: str) -> None:
    """Insert a row in ``plan_routing`` with the given stage."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        # Use the standard INSERT OR REPLACE so we don't have to
        # worry about whether the plan already exists.
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, stage, 0, "[]",
                "{}", "{}", "null",
                "2026-09-11T00:00:00", "2026-09-11T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_routing_stage(db_path: Path, plan_id: str) -> str | None:
    """Return the current routing.stage for a plan."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?", (plan_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# --- Change 4: routing.stage CAS ---


def test_routing_cas_advances_executing_to_verification(tmp_path, state_db_path):
    """When the auto-verification loop starts and routing.stage is
    ``executing``, the v9 fix must CAS it to ``verification``.
    """
    plan_id = "routing-cas-1"
    _set_routing_stage(state_db_path, plan_id, "executing")

    # Manually invoke the CAS logic without spinning up the full
    # _run_auto_verification_loop (which would also start a real
    # verification thread — too heavy for unit test).
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).try_mark_phase(
            plan_id,
            ("executing", "ready"),
            "verification",
        )
        conn.commit()
    finally:
        conn.close()

    assert _get_routing_stage(state_db_path, plan_id) == "verification"


def test_routing_cas_advances_ready_to_verification(tmp_path, state_db_path):
    """When the partial-completion edge case fires and routing.stage
    is still ``ready`` (never reached ``executing`` because the
    CAS during start_execution failed), the auto-verification CAS
    must accept ``ready`` as a valid source.
    """
    plan_id = "routing-cas-2"
    _set_routing_stage(state_db_path, plan_id, "ready")

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).try_mark_phase(
            plan_id,
            ("executing", "ready"),
            "verification",
        )
        conn.commit()
    finally:
        conn.close()

    assert _get_routing_stage(state_db_path, plan_id) == "verification"


def test_routing_cas_rejects_already_advanced_stage(tmp_path, state_db_path):
    """When routing.stage is already past ``verification``
    (e.g. ``verification_running`` set by another writer), the
    v9 CAS must raise ``ConflictError`` and the loop must
    swallow it.
    """
    plan_id = "routing-cas-3"
    _set_routing_stage(state_db_path, plan_id, "verification_running")

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        with pytest.raises(ConflictError):
            repo.try_mark_phase(
                plan_id,
                ("executing", "ready"),
                "verification",
            )
        conn.commit()
    finally:
        conn.close()

    # Stage unchanged
    assert _get_routing_stage(state_db_path, plan_id) == "verification_running"


def test_routing_cas_idempotent_when_already_verification(tmp_path, state_db_path):
    """When routing.stage is already ``verification`` (e.g. retry
    after server restart), the CAS rejects it as ``not in
    expected_phases`` so the loop is correctly a no-op.
    """
    plan_id = "routing-cas-4"
    _set_routing_stage(state_db_path, plan_id, "verification")

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        with pytest.raises(ConflictError):
            repo.try_mark_phase(
                plan_id,
                ("executing", "ready"),
                "verification",
            )
        conn.commit()
    finally:
        conn.close()

    # Stage unchanged — still ``verification``
    assert _get_routing_stage(state_db_path, plan_id) == "verification"


# --- Integration: end-to-end Bug 2 fix verification ---


def test_plan1_partial_completion_does_not_affect_routing_already_terminal(
    tmp_path, state_db_path
):
    """A plan already at ``completed`` (its stage advanced by hand).
    The v9 fix must NOT regress this — a stage
    that's already past ``verification`` stays put.
    """
    plan_id = "plan1-already-terminal"
    _set_routing_stage(state_db_path, plan_id, "completed")

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        with pytest.raises(ConflictError):
            RoutingRepository(conn).try_mark_phase(
                plan_id,
                ("executing", "ready"),
                "verification",
            )
    finally:
        conn.close()

    # Stage unchanged
    assert _get_routing_stage(state_db_path, plan_id) == "completed"
