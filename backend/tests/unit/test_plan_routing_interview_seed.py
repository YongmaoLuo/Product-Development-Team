"""Unit tests for task #3.9 — plan_routing tracks the interview phase.

Background
----------
Before task #3.9, ``PlanState._load_state`` fell through to
``_default_state`` → ``_infer_phase`` whenever no ``plan_routing``
row existed for the plan, and ``_infer_phase`` returned
``"interview_complete"`` based on the mere presence of
``interview.json`` on disk. The bug surfaced the moment
``interviewer.start()`` wrote that file — a plan with an in-flight
interview and a plan with a completed interview were
indistinguishable to anyone reading the routing table.

The fix: ``_seed_plan_routing_phase(plan_id, phase)`` INSERTs a
``plan_routing`` row at plan creation (called from
``/api/interview/start``) so the routing table is the source of
truth for the interview phase from the very first request
onwards. The call is ``INSERT OR IGNORE`` so a re-run on a plan
that has already advanced past ``phase`` does not regress the row.

This test file pins the contract:

  1. ``test_seed_inserts_row_when_absent`` — the helper writes a row
     with the requested phase when the table is empty.
  2. ``test_seed_is_idempotent_on_existing_row`` — calling the
     helper twice does not overwrite a row that has already
     advanced past ``phase``.
  3. ``test_seed_uses_canonical_db_path`` — the helper resolves the
     state DB through ``PDT_STATE_DB_PATH`` env override (same path
     as ``server._state_db_path``) so a hermetic test DB is
     honoured.

TDD spec:

  - test_seed_inserts_row_when_absent
  - test_seed_is_idempotent_on_existing_row
  - test_seed_writes_through_state_db_path
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open a hermetic SQLite handle + apply the state-machine schema."""
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate

    conn = _open_db(db_path)
    _migrate(conn)
    return conn


def _read_plan_routing_phase(db_path: Path, plan_id: str):
    """Return ``(phase, version)`` for the row, or ``(None, None)``.

    The helper applies the state-machine schema before reading so
    a freshly-created DB that has not been migrated yet does not
    fail with ``no such table: plan_routing``.
    """
    from state_machine.db.schema import migrate as _migrate

    conn = sqlite3.connect(str(db_path))
    try:
        _migrate(conn)
        row = conn.execute(
            "SELECT current_phase, version FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return (None, None)
        return (row[0], row[1])
    finally:
        conn.close()


def test_seed_inserts_row_when_absent(tmp_path) -> None:
    """A fresh plan gets a ``plan_routing`` row with ``current_phase='interview'``."""
    db_path = tmp_path / "state.db"
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    try:
        from server import _seed_plan_routing_phase

        _seed_plan_routing_phase("plan-fresh", phase="interview")

        phase, version = _read_plan_routing_phase(db_path, "plan-fresh")
        assert phase == "interview", (
            f"_seed_plan_routing_phase must insert current_phase='interview'; "
            f"got current_phase={phase!r}"
        )
        assert version == 0, (
            f"newly-seeded row must have version=0; got version={version!r}"
        )
    finally:
        os.environ.pop("PDT_STATE_DB_PATH", None)


def test_seed_is_idempotent_on_existing_row(tmp_path) -> None:
    """A re-seed does NOT overwrite a row that has already advanced.

    Critical regression guard: if the helper blindly ``INSERT OR
    REPLACE``d, a restart on a plan whose row already carries
    ``current_phase='prd_review'`` would silently regress the phase back
    to ``'interview'`` and the routing layer would lose track of
    the plan's real position. ``INSERT OR IGNORE`` keeps the
    existing row intact.
    """
    db_path = tmp_path / "state.db"
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    try:
        conn = _open_db(db_path)
        try:
            # Pre-seed a row that has advanced past interview.
            conn.execute(
                "INSERT INTO plan_routing "
                "(plan_id, current_phase, version, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "plan-advanced",
                    "prd_review",
                    7,
                    "2026-08-01T00:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        from server import _seed_plan_routing_phase

        # Re-seed with phase='interview' — must NOT regress.
        _seed_plan_routing_phase("plan-advanced", phase="interview")

        phase, version = _read_plan_routing_phase(db_path, "plan-advanced")
        assert phase == "prd_review", (
            f"_seed_plan_routing_phase must NOT regress an advanced "
            f"row's phase; got {phase!r}, expected 'prd_review'"
        )
        assert version == 7, (
            f"existing row's version must be preserved; "
            f"got version={version!r}, expected 7"
        )
    finally:
        os.environ.pop("PDT_STATE_DB_PATH", None)


def test_seed_writes_through_state_db_path(tmp_path) -> None:
    """The helper routes through ``_state_db_path`` (not a hard-coded path).

    The production ``server._state_db_path`` is the seam that
    resolves the state DB (env override → canonical repo-root).
    The helper MUST use it, not open a fixed-path DB directly —
    otherwise a per-test ``PDT_STATE_DB_PATH`` override (or the
    production ``<repo-root>/state.db``) is silently bypassed and
    routing writes land in the wrong place.

    Pinned here via the ``isolated_plans_dir`` autouse fixture,
    which patches ``server._state_db_path`` to
    ``tmp_path/state.db``. The seed must write to that path.
    """
    db_path = tmp_path / "state.db"
    # ``isolated_plans_dir`` autouse fixture already patched
    # ``server._state_db_path`` to return this ``tmp_path/state.db``.
    # We re-derive it here only so the test reads from the same
    # source-of-truth.
    from server import _state_db_path
    assert _state_db_path() == db_path, (
        f"isolated_plans_dir fixture must route _state_db_path to "
        f"tmp_path/state.db; got {_state_db_path()!r}"
    )

    from server import _seed_plan_routing_phase

    _seed_plan_routing_phase("plan-through-fixture", phase="interview")

    phase, _ = _read_plan_routing_phase(db_path, "plan-through-fixture")
    assert phase == "interview", (
        f"helper must write through _state_db_path; "
        f"got current_phase={phase!r} at db_path={db_path!r}"
    )