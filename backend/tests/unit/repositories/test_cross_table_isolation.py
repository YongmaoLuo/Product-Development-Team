"""Cross-table isolation tests for ExecutionRepository and VerificationRepository.

Bug 1 anchor (VP-009): Writes to ``plan_execution`` MUST NOT touch any
``plan_verification`` row, and writes to ``plan_verification`` MUST NOT
touch any ``plan_execution`` row.  Each repository is scoped to a single
table and uses ``UPDATE ... WHERE plan_id = ?`` so a mistyped SQL or a
future refactor that accidentally expands the WHERE clause MUST be caught
by these tests.

The test command from the verification plan is::

    pytest tests/unit/repositories/test_cross_table_isolation.py -v -m bug_1

The tests below cover:

  1. ``test_execution_write_keeps_verification_snapshot`` — every
     :class:`ExecutionRepository` write method, parameterised across
     ``update_phase`` / ``update_task_progress`` /
     ``update_next_run_at`` / ``update_card_state`` / ``update_flags``,
     leaves the ``plan_verification`` row byte-for-byte unchanged.
  2. ``test_verification_write_keeps_execution_snapshot`` — every
     :class:`VerificationRepository` write method, parameterised
     across ``init_round`` / ``complete_round`` /
     ``append_verdict`` / ``mark_stopped``, leaves the
     ``plan_execution`` row byte-for-byte unchanged.
  3. ``test_old_implementation_phase_flatten_breaks_invariant`` —
     the regression guard.  An "old" implementation that flattens
     both writes into a single transaction over the wrong table is
     demonstrably detected by the snapshot assertion.

Every test in this file is decorated with ``@pytest.mark.bug_1`` so the
verification suite can be run with ``-m bug_1``.
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
        setup.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        setup.close()


@pytest.mark.bug_1
@pytest.mark.parametrize(
    "method_name,invoke",
    EXECUTION_WRITE_METHODS,
    ids=[m for m, _ in EXECUTION_WRITE_METHODS],
)
def test_execution_write_keeps_verification_snapshot(
    method_name,
    invoke,
    tmp_path,
):
    """Every ExecutionRepository write keeps plan_verification untouched.

    **Bug 1 anchor — execution side.**  Writes via
    :class:`ExecutionRepository` are scoped to ``plan_execution`` only.
    A mistyped SQL or a future refactor that accidentally expands the
    UPDATE's WHERE clause MUST be caught by this test.
    """
    db_path = tmp_path / "state.db"
    _bootstrap_all_tables(db_path)

    snap_conn = open_db(db_path)
    try:
        before_v = snap_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        before_r = snap_conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert before_v is not None, "plan_verification row missing pre-write"
        assert before_r is not None, "plan_routing row missing pre-write"
    finally:
        snap_conn.close()

    write_conn = open_db(db_path)
    try:
        write_repo = ExecutionRepository(write_conn)
        invoke(write_repo)
    finally:
        write_conn.close()

    after_conn = open_db(db_path)
    try:
        after_v = after_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        after_r = after_conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_v is not None, "plan_verification row disappeared"
        assert after_r is not None, "plan_routing row disappeared"
        assert tuple(after_v) == tuple(before_v), (
            f"{method_name!r} mutated plan_verification: "
            f"before={tuple(before_v)!r} after={tuple(after_v)!r}"
        )
        assert tuple(after_r) == tuple(before_r), (
            f"{method_name!r} mutated plan_routing: "
            f"before={tuple(before_r)!r} after={tuple(after_r)!r}"
        )
    finally:
        after_conn.close()


@pytest.mark.bug_1
@pytest.mark.parametrize(
    "method_name,invoke",
    VERIFICATION_WRITE_METHODS,
    ids=[m for m, _ in VERIFICATION_WRITE_METHODS],
)
def test_verification_write_keeps_execution_snapshot(
    method_name,
    invoke,
    tmp_path,
):
    """Every VerificationRepository write keeps plan_execution untouched.

    **Bug 1 anchor — verification side.**  The inverse contract:
    writes via :class:`VerificationRepository` are scoped to
    ``plan_verification`` only and must NEVER mutate any row in
    ``plan_execution``.
    """
    db_path = tmp_path / "state.db"
    _bootstrap_all_tables(db_path)

    snap_conn = open_db(db_path)
    try:
        before_e = snap_conn.execute(
            "SELECT * FROM plan_execution WHERE plan_id = ?", ("p1",)
        ).fetchone()
        before_r = snap_conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert before_e is not None, "plan_execution row missing pre-write"
        assert before_r is not None, "plan_routing row missing pre-write"
    finally:
        snap_conn.close()

    write_conn = open_db(db_path)
    try:
        write_repo = VerificationRepository(write_conn)
        invoke(write_repo)
    finally:
        write_conn.close()

    after_conn = open_db(db_path)
    try:
        after_e = after_conn.execute(
            "SELECT * FROM plan_execution WHERE plan_id = ?", ("p1",)
        ).fetchone()
        after_r = after_conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_e is not None, "plan_execution row disappeared"
        assert after_r is not None, "plan_routing row disappeared"
        assert tuple(after_e) == tuple(before_e), (
            f"{method_name!r} mutated plan_execution: "
            f"before={tuple(before_e)!r} after={tuple(after_e)!r}"
        )
        assert tuple(after_r) == tuple(before_r), (
            f"{method_name!r} mutated plan_routing: "
            f"before={tuple(before_r)!r} after={tuple(after_r)!r}"
        )
    finally:
        after_conn.close()


@pytest.mark.bug_1
def test_old_implementation_phase_flatten_breaks_invariant(tmp_path):
    """Regression guard: the byte-for-byte snapshot pattern detects bug 1.

    Bug 1 historically manifested as ``ExecutionRepository.update_phase``
    incidentally mutating ``plan_verification``.  This test simulates
    that regression and confirms the snapshot pattern IS sensitive
    enough to detect it.
    """
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

    regression_conn = open_db(db_path)
    try:
        regression_conn.execute(
            "UPDATE plan_verification SET verification_status = ? "
            "WHERE plan_id = ?",
            ("failed", "p1"),
        )
    finally:
        regression_conn.close()

    after_conn = open_db(db_path)
    try:
        after_v = after_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_v is not None
        assert tuple(after_v) != tuple(before_v), (
            "sanity check failed: a direct UPDATE to plan_verification "
            "should change the snapshot; if this assertion fails, the "
            "byte-for-byte snapshot pattern in the parametrised tests "
            "above is not sensitive enough to detect bug 1."
        )
    finally:
        after_conn.close()


@pytest.mark.bug_1
def test_execution_insert_does_not_touch_verification(tmp_path):
    """``ExecutionRepository.insert`` MUST NOT create or mutate a
    sibling ``plan_verification`` row for the same ``plan_id``.
    """
    db_path = tmp_path / "state.db"

    setup = open_db(db_path)
    try:
        migrate(setup)
        setup.execute(
            "INSERT INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            " verification_stop_reason, runtime_state, executor_state, "
            " progress_state, results, verdicts, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "p1", "pending", 0, 3, None, None, None,
                None, None, None, None, "2026-08-05T10:00:00Z",
            ),
        )
    finally:
        setup.close()

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
        write_repo = ExecutionRepository(write_conn)
        write_repo.insert("p1", current_phase="ready")
    finally:
        write_conn.close()

    after_conn = open_db(db_path)
    try:
        after_v = after_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_v is not None, "plan_verification row disappeared after insert"
        assert tuple(after_v) == tuple(before_v), (
            "insert() mutated plan_verification: "
            f"before={tuple(before_v)!r} after={tuple(after_v)!r}"
        )
        exec_row = after_conn.execute(
            "SELECT * FROM plan_execution WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert exec_row is not None, "insert() did not create plan_execution row"
    finally:
        after_conn.close()
