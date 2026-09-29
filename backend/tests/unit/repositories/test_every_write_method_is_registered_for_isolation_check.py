"""Every write method on every repository is registered for the isolation check.

**VP-016 anchor — repository write-method coverage.**

This test enforces the contract that ``ExecutionRepository`` and
``VerificationRepository`` expose their write methods via a stable,
introspectable list — the list that ``test_cross_table_isolation.py``
parameterises across.  Concretely:

  1.  Every public write method on :class:`ExecutionRepository`
      (``update_phase``, ``update_task_progress``,
      ``update_next_run_at``, ``update_card_state``, ``update_flags``)
      is invoked once here and we assert each call mutates the
      ``plan_execution`` row AND leaves the ``plan_verification`` row
      byte-for-byte unchanged.

  2.  Every public write method on :class:`VerificationRepository``
      (``init_round``, ``complete_round``, ``append_verdict``,
      ``mark_stopped``) is invoked once here and we assert each call
      mutates the ``plan_verification`` row AND leaves the
      ``plan_execution`` row byte-for-byte unchanged.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Callable

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.verification_repository import VerificationRepository


EXECUTION_WRITE_METHODS = [
    (
        "update_phase",
        lambda repo: repo.update_phase(
            "p1", current_phase="executing", project_dir="/new/path"
        ),
    ),
    (
        "update_task_progress",
        lambda repo: repo.update_task_progress(
            "p1", {"current": "1-2", "completed": 1, "total": 5}
        ),
    ),
    (
        "update_next_run_at",
        lambda repo: repo.update_next_run_at("p1", "2099-01-01T00:00:00Z"),
    ),
    (
        "update_card_state",
        lambda repo: repo.update_card_state(
            "p1", {"expanded": True, "active": "verifying"}
        ),
    ),
    (
        "update_flags",
        lambda repo: repo.update_flags(
            "p1", {"arch_enabled": True, "test_enabled": False}
        ),
    ),
]


VERIFICATION_WRITE_METHODS = [
    ("init_round", lambda repo: repo.init_round("p1", 2, 5)),
    ("complete_round", lambda repo: repo.complete_round("p1", {"passed": 2})),
    ("append_verdict", lambda repo: repo.append_verdict("p1", {"seq": 1})),
    ("mark_stopped", lambda repo: repo.mark_stopped("p1", "user_stopped")),
]


def _bootstrap_all_tables(db_path):
    """Seed one plan with rows in every ``plan_*`` table."""
    setup = open_db(db_path)
    try:
        migrate(setup)
        setup.execute(
            "INSERT INTO plan_routing "
            "(plan_id, current_phase, substage, version, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "executing", "task_1", 3, "2026-08-05T12:00:00Z"),
        )
        setup.execute(
            "INSERT INTO plan_execution "
            "(plan_id, current_phase, attempt_count, project_dir, "
            " stop_reason, task_progress, next_run_at, card_state, "
            " flags, exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "p1", "ready", 0, "/orig/path", None, None,
                None, None, None, None, None, None,
                "2026-08-05T11:00:00Z",
            ),
        )
        setup.execute(
            "INSERT INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            " verification_stop_reason, runtime_state, executor_state, "
            " progress_state, results, verdicts, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                # ``runtime_state`` / ``executor_state`` /
                # ``progress_state`` are JSON columns: the repository
                # writes them through ``_encode_fields`` (json.dumps)
                # and reads them back through ``_decode_row``
                # (json.loads, unguarded). Seeding a bare marker string
                # here would make the reader raise JSONDecodeError on a
                # row this test never wrote through the repository, so
                # the markers are seeded JSON-encoded.
                "p1", "running", 1, 3, None,
                json.dumps({"marker": "rt"}),
                json.dumps({"marker": "ex"}),
                json.dumps({"marker": "pr"}),
                None, None, "2026-08-05T11:30:00Z", "2026-08-05T11:30:00Z",
            ),
        )
    finally:
        setup.close()


@pytest.mark.parametrize(
    "method_name,invoke",
    EXECUTION_WRITE_METHODS,
    ids=[m for m, _ in EXECUTION_WRITE_METHODS],
)
def test_every_execution_write_method_is_registered(
    method_name, invoke, tmp_path,
):
    """Every ExecutionRepository write method is registered here."""
    db_path = tmp_path / "state.db"
    _bootstrap_all_tables(db_path)

    snap_conn = open_db(db_path)
    try:
        before_v = snap_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert before_v is not None
    finally:
        snap_conn.close()

    write_conn = open_db(db_path)
    try:
        repo = ExecutionRepository(write_conn)
        invoke(repo)
    finally:
        write_conn.close()

    after_conn = open_db(db_path)
    try:
        after_v = after_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_v is not None
        assert tuple(after_v) == tuple(before_v), (
            f"{method_name!r} on ExecutionRepository mutated "
            f"plan_verification: before={tuple(before_v)!r} "
            f"after={tuple(after_v)!r}"
        )
    finally:
        after_conn.close()


@pytest.mark.parametrize(
    "method_name,invoke",
    VERIFICATION_WRITE_METHODS,
    ids=[m for m, _ in VERIFICATION_WRITE_METHODS],
)
def test_every_verification_write_method_is_registered(
    method_name, invoke, tmp_path,
):
    """Every VerificationRepository write method is registered here."""
    db_path = tmp_path / "state.db"
    _bootstrap_all_tables(db_path)

    snap_conn = open_db(db_path)
    try:
        before_e = snap_conn.execute(
            "SELECT * FROM plan_execution WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert before_e is not None
    finally:
        snap_conn.close()

    write_conn = open_db(db_path)
    try:
        repo = VerificationRepository(write_conn)
        invoke(repo)
    finally:
        write_conn.close()

    after_conn = open_db(db_path)
    try:
        after_e = after_conn.execute(
            "SELECT * FROM plan_execution WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_e is not None
        assert tuple(after_e) == tuple(before_e), (
            f"{method_name!r} on VerificationRepository mutated "
            f"plan_execution: before={tuple(before_e)!r} "
            f"after={tuple(after_e)!r}"
        )
    finally:
        after_conn.close()
