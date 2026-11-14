"""The other failure paths still fail loudly (2026-11-04).

2026-11-04 narrowed the verification section's exception handling in
``_plan_status_payload``: the 404 that means "verification not
started" is an answer, not a fault, so it is now answered quietly
instead of raising a full traceback at ERROR level for every plan that
is still running (2393 of them in two hours).

A narrowing is only safe if the paths it did **not** cover keep
failing loudly.  Otherwise the quiet is not a quieter report — it is
the disappearance of the signal: a real verification crash, a 404 that
means something else, a broken summary, a broken execution section would
all vanish into the same silence, and the operator would see a plan
whose card looks fine while the section behind it is dead.

So these tests pin the other side of the contract — four failure
paths, each of which must still log an ERROR and still degrade its own
section:

* a real verification failure (``RuntimeError``) → ERROR + ``None``;
* a 404 that is NOT "verification not started" → ERROR + ``None``;
* a broken summary section → ERROR + ``None``;
* a broken execution section → ERROR + a degraded section that keeps
  the plan's durable verdict.

The fixtures (``state_db`` and the seed helpers) are copied from
``test_plan_status.py`` so the plan is a real one — real routing row,
real execution row, real task rows — and only the section under test
is broken.
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
def plan_in_flight(tmp_path, monkeypatch, state_db, clean_execution_state):
    """A real plan in ``executing``, with all three sections readable.

    Nothing is broken here: each test breaks the one section it is
    about, so a failure in a test is the failure of that section and
    not of the plan's plumbing.
    """
    import server

    plan_id = "plan-section-failures"
    (tmp_path / plan_id).mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    _seed_routing(state_db, plan_id, "executing")
    _seed_execution_record(state_db, plan_id)
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "work", "status": "in_progress"},
    ])
    return plan_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_records(caplog, needle: str):
    """The ERROR records whose message mentions ``needle``."""
    return [
        record for record in caplog.records
        if record.levelno >= logging.ERROR and needle in record.getMessage()
    ]


def _explode(message: str):
    """A section handler that fails the way a broken section really does."""
    def _boom(_plan_id):
        raise RuntimeError(message)
    return _boom


# ---------------------------------------------------------------------------
# The verification section: everything except "not started" is a fault
# ---------------------------------------------------------------------------


def test_a_real_verification_failure_still_logs_an_error(
    plan_in_flight, monkeypatch, caplog,
):
    """A verification crash is a fault — it must be reported as one."""
    import server

    caplog.set_level(logging.DEBUG, logger="server")
    monkeypatch.setattr(
        server, "get_verification_progress", _explode("boom"),
    )

    payload = server._plan_status_payload(plan_in_flight)

    errors = _error_records(caplog, "verification section failed")
    assert errors, (
        "a verification section that raised is a broken section; the "
        "operator has to see it, and the 2026-11-04 quiet must not "
        "have swallowed it"
    )
    assert plan_in_flight in errors[0].getMessage(), (
        "the ERROR names the plan whose section broke"
    )
    assert payload["verification"] is None, (
        "the broken section degrades to None — it is unreadable, not "
        "empty-and-fine"
    )


def test_a_real_verification_failure_still_carries_its_traceback(
    plan_in_flight, monkeypatch, caplog,
):
    """The report of a real failure keeps the traceback that explains it."""
    import server

    caplog.set_level(logging.DEBUG, logger="server")
    monkeypatch.setattr(
        server, "get_verification_progress", _explode("boom"),
    )

    server._plan_status_payload(plan_in_flight)

    errors = _error_records(caplog, "verification section failed")
    assert errors, "the failure must be reported at all"
    assert errors[0].exc_info is not None, (
        "a real failure is reported with its traceback — a bare message "
        "would say that something broke, not what broke"
    )


def test_a_different_404_still_logs_an_error(
    plan_in_flight, monkeypatch, caplog,
):
    """Only ONE 404 is an answer.  A 404 saying something else is a fault.

    The quiet is scoped to ``detail['error'] == 'verification not
    started'`` exactly.  A 404 whose detail says anything else is a
    different answer (the plan is not in the tree, a plain string, …)
    and it keeps being logged as the failure it is.
    """
    import server

    caplog.set_level(logging.DEBUG, logger="server")

    def _other_404(_plan_id):
        raise HTTPException(404, {"error": "something else"})

    monkeypatch.setattr(server, "get_verification_progress", _other_404)

    payload = server._plan_status_payload(plan_in_flight)

    errors = _error_records(caplog, "verification section failed")
    assert errors, (
        "a 404 that is not 'verification not started' is a different "
        "answer and must still be reported as a failure"
    )
    assert payload["verification"] is None


def test_a_server_error_from_verification_still_logs_an_error(
    plan_in_flight, monkeypatch, caplog,
):
    """A 500 is a fault by any reading — ERROR, and the section degrades."""
    import server

    caplog.set_level(logging.DEBUG, logger="server")

    def _server_error(_plan_id):
        raise HTTPException(500, "x")

    monkeypatch.setattr(server, "get_verification_progress", _server_error)

    payload = server._plan_status_payload(plan_in_flight)

    errors = _error_records(caplog, "verification section failed")
    assert errors, "a 500 must be reported as an ERROR"
    assert payload["verification"] is None


# ---------------------------------------------------------------------------
# The other two sections: untouched by the narrowing, still fatal to the section
# ---------------------------------------------------------------------------


def test_a_broken_summary_section_still_logs_an_error(
    plan_in_flight, monkeypatch, caplog,
):
    """The summary section is not part of the narrowing — it still breaks."""
    import server
    import routes.plans

    caplog.set_level(logging.DEBUG, logger="server")
    monkeypatch.setattr(
        routes.plans, "_plan_summary_section", _explode("summary boom"),
    )

    payload = server._plan_status_payload(plan_in_flight)

    errors = _error_records(caplog, "summary section failed")
    assert errors, (
        "the 2026-11-04 quiet is scoped to the verification section; a "
        "broken summary must still be reported"
    )
    assert plan_in_flight in errors[0].getMessage()
    assert payload["summary"] is None, (
        "a broken summary degrades to None, and the rest of the "
        "snapshot still answers"
    )


def test_a_broken_execution_section_still_logs_an_error(
    plan_in_flight, monkeypatch, caplog,
):
    """The execution section still breaks loudly and still degrades.

    The degraded execution section is not ``None``: it keeps the
    plan's durable verdict (the ``plan_execution`` row) and says that
    it is degraded, because "the section is unreadable" and "the plan's
    state is unknown" are two different answers.
    """
    import server

    caplog.set_level(logging.DEBUG, logger="server")
    monkeypatch.setattr(
        server, "get_execution_progress", _explode("execution boom"),
    )

    payload = server._plan_status_payload(plan_in_flight)

    errors = _error_records(caplog, "execution section failed")
    assert errors, (
        "a broken execution section must still be reported — the "
        "2026-11-04 quiet does not reach it"
    )
    assert plan_in_flight in errors[0].getMessage()
    execution = payload["execution"]
    assert execution is not None, (
        "the durable execution verdict must survive a broken section"
    )
    assert execution.get("degraded") is True, (
        "a reader has to be able to tell a reduced answer from a "
        "complete one"
    )
    assert execution.get("execution_status") == "running"
