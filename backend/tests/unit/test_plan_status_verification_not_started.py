"""A plan whose verification never started is not a failure (2026-11-04).

``_plan_status_payload`` reads three sections through the very handlers
the standalone endpoints serve.  ``get_verification_progress`` answers
``HTTPException(404, {"error": "verification not started"})`` when the
executor has not written a progress state yet — which is the normal
state of every plan that is still executing.

``_plan_status_payload`` used to catch that 404 in its blanket
``except Exception`` and treat it as a broken section: a full
traceback at ERROR level, every time the card asked where a running
plan was.  Measured: 2393 tracebacks in two hours.  The 404 is a
normal answer, not a fault.

These tests pin the difference between the two:

* the 404 that means "verification not started" → ``None`` section,
  one DEBUG line, **no** ERROR record;
* every other failure → still ERROR + still degraded.

The fixtures (``state_db`` and the seed helpers) are copied from
``test_plan_status.py`` so the plan is a real one — real routing row,
real execution row, real task rows — and only the verification part
is missing, which is the state under test.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures — the same ones ``test_plan_status.py`` uses, copied verbatim so
# this file stands on its own.
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Hermetic ``state.db`` wired into ``server``."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    # ``_open_state_machine`` calls ``_state_db_path(request)``.
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db)
    return db


def _seed_routing(db: Path, plan_id: str, phase: str) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, completed_phases, review_rounds, "
            " flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, phase, 0, "[]", "{}", "{}", "null",
             "2026-09-23T00:00:00", "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tasks(db: Path, plan_id: str, rows) -> None:
    """Seed ``plan_tasks`` (identity and runtime state through their two doors)."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        for row in rows:
            fields = dict(row)
            task_id = str(fields.pop("id"))
            status = fields.pop("status", None)
            repo.add_task(plan_id, {"id": task_id, **fields})
            if status is not None:
                repo.update_task(plan_id, task_id, {"status": status})
        conn.commit()
    finally:
        conn.close()


def _seed_execution_record(db: Path, plan_id: str) -> None:
    """Give the plan the ``plan_execution`` row a real run leaves behind."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            plan_id, exec_status="running", exec_pid=4242,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def clean_execution_state():
    """Restore ``_execution_state`` / ``_verification_state`` after a test."""
    import server

    exec_before = dict(server._execution_state)
    verif_before = dict(server._verification_state)
    yield
    server._execution_state.clear()
    server._execution_state.update(exec_before)
    server._verification_state.clear()
    server._verification_state.update(verif_before)


@pytest.fixture
def plan_without_verification(tmp_path, monkeypatch, state_db,
                              clean_execution_state):
    """A plan in ``executing`` whose verification was never started.

    No ``plan_verification`` row is seeded — that is the whole point:
    the handler's answer is the 404 under test.
    """
    import server

    plan_id = "plan-verification-not-started"
    (tmp_path / plan_id).mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    _seed_routing(state_db, plan_id, "executing")
    _seed_execution_record(state_db, plan_id)
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "work", "status": "in_progress"},
    ])

    def _not_started(_plan_id):
        raise HTTPException(404, {"error": "verification not started"})

    monkeypatch.setattr(server, "get_verification_progress", _not_started)
    return plan_id


# ---------------------------------------------------------------------------
# The three sections
# ---------------------------------------------------------------------------


def test_not_started_verification_logs_no_error(plan_without_verification,
                                                 caplog):
    """The 404 is an answer, not a fault — so it must not be logged as one."""
    import server

    caplog.set_level(logging.DEBUG, logger="server")

    payload = server._plan_status_payload(plan_without_verification)

    assert payload["verification"] is None
    errors = [
        record.getMessage() for record in caplog.records
        if record.levelno >= logging.ERROR
    ]
    assert errors == [], (
        "a plan that has not started verification is a normal state; "
        "an ERROR record here is the 2393-tracebacks-per-two-hours bug"
    )


def test_not_started_verification_emits_a_debug_line(plan_without_verification,
                                                      caplog):
    """The section is unreadable for a reason, and the reason is said once."""
    import server

    caplog.set_level(logging.DEBUG, logger="server")

    server._plan_status_payload(plan_without_verification)

    debug_lines = [
        record.getMessage() for record in caplog.records
        if record.levelno == logging.DEBUG
        and "verification not started" in record.getMessage()
    ]
    assert debug_lines, (
        "the DEBUG line names the plan and the reason, so an operator can "
        "tell 'not started yet' from 'the section broke'"
    )
    assert plan_without_verification in debug_lines[0]


def test_other_sections_still_render(plan_without_verification):
    """Only the verification section is affected — the rest still answers."""
    import server

    payload = server._plan_status_payload(plan_without_verification)

    assert payload["summary"] is not None
    assert payload["execution"] is not None
    assert payload["verification"] is None
