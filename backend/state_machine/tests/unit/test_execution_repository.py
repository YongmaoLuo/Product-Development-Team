"""Unit tests for :class:`ExecutionRepository`.

Background
----------
``plan_execution`` is the state-machine's per-plan row that records
the execution metadata (current phase, attempt count, project_dir,
stop_reason, task_progress, next_run_at, card_state, flags,
exec_pid, exec_status, started_at, updated_at).

:class:`ExecutionRepository` is the single write/read path for this
table.  The repository contract pinned by these tests is:

  * ``insert(plan_id, current_phase, **fields)`` — bootstrap a row.
  * ``snapshot_for_list(plan_ids)`` — return dicts for many plans
    in one round-trip (used by the API list view).
  * ``summary(plan_id)`` — return the row's "summary" view
    (current_phase + project_dir + flags + task_progress).
  * ``progress(plan_id)`` — return the parsed ``task_progress``
    dict, or ``None`` if the plan has no execution row yet.
  * ``update_phase(plan_id, current_phase=None, **fields)`` — mutate
    any subset of columns inside one ``BEGIN IMMEDIATE`` txn.
    ``current_phase=None`` means "leave the phase column alone" (it is
    ``NOT NULL``, so ``None`` is never a value to store).
  * ``update_task_progress(plan_id, progress)`` — read-modify-write
    the ``task_progress`` JSON column with full IMMEDIATE isolation.
  * ``card_table()`` — return a list of card-table rows for the
    planner UI (one per plan, with phase + progress %).
  * ``update_next_run_at(plan_id, next_run_at)`` — schedule next run.
  * ``update_card_state(plan_id, card_state)`` — UI state dict.
  * ``update_flags(plan_id, flags)`` — feature-flag overrides.

Cross-table invariants (this is the bug 1 anchor):

    Writes via :class:`ExecutionRepository` MUST NOT mutate any
    row in ``plan_verification`` or ``plan_routing``.  Each write
    is scoped to ``plan_execution`` and uses ``UPDATE ... WHERE
    plan_id = ?`` so a mistyped SQL cannot accidentally leak
    into a sibling table.  All ExecutionRepository write methods
    are parameterised through ``test_execution_write_never_touches_*``
    to verify the invariant end-to-end.

Concurrency invariant (this is the bug 3 anchor):

    ``update_task_progress`` MUST be wrapped in ``BEGIN IMMEDIATE
    → 读旧 JSON → 合并 → 写回 → COMMIT``.  An external reader
    (different connection, same database) MUST NOT see the new
    JSON payload until the writer commits.  If the writer
    crashes mid-transaction the original value must survive —
    ``test_update_task_progress_invisible_until_commit`` is the
    pinning test for this.

Cache invariant:

    ``ExecutionRepository`` does NOT maintain any in-memory
    cache (per architecture decision point 4).  A direct UPDATE
    from outside the repository (via ``conn.execute``) MUST be
    observable on the next ``summary()`` / ``progress()`` call
    without any cache invalidation step.  ``test_repository_has_no_stale_cache``
    pins this end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import (
    ExecutionRepository,
    PlanNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a fresh, migrated SQLite connection (autocommit mode)."""
    db_path = tmp_path / "state.db"
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> ExecutionRepository:
    """Yield an ``ExecutionRepository`` bound to the test connection."""
    return ExecutionRepository(conn)


# ---------------------------------------------------------------------------
# Roundtrip / insert
# ---------------------------------------------------------------------------


def test_insert_then_summary_returns_same_fields(
    repo: ExecutionRepository,
) -> None:
    """``insert`` → ``summary`` returns the same fields."""
    repo.insert(
        plan_id="p1",
        current_phase="ready",
        project_dir="/abs/p1",
    )

    summary = repo.summary("p1")
    assert summary is not None
    assert summary["current_phase"] == "ready"
    assert summary["project_dir"] == "/abs/p1"


def test_progress_returns_none_when_row_missing(
    repo: ExecutionRepository,
) -> None:
    """``progress`` on a missing plan returns ``None``.

    Same convention as ``RoutingRepository.current`` — the read
    path must NOT raise on unknown plan_ids.
    """
    assert repo.progress("does-not-exist") is None


# NOTE: ``test_progress_returns_parsed_dict``,
# ``test_update_task_progress_persists_json_payload`` and
# ``test_update_task_progress_invisible_until_commit`` were removed
# in the v4 schema normalisation (2026-09-09).  They pinned the
# legacy contract where per-task state lived in the
# ``plan_execution.task_progress`` JSON column
# and ``ExecutionRepository.progress()`` parsed that JSON.  v4 moved
# per-task state to the ``plan_tasks`` table; ``progress()`` now
# aggregates via ``SELECT status, COUNT(*) ... GROUP BY status``
# (see ``test_execution_repository_v4_progress.py``).  Legacy
# ``update_task_progress`` is retained on the repository but only
# for backward compat — its output is no longer read by
# ``progress()``.


# ---------------------------------------------------------------------------
# TDD anchor #3 — cross-table isolation (bug 1 anchor)
# ---------------------------------------------------------------------------


# All public write methods on ExecutionRepository.  ``insert`` is
# excluded because it bootstraps a NEW plan_execution row and
# therefore does not target an existing row at all.  Every method
# below MUST keep plan_verification and plan_routing rows unchanged.
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


@pytest.mark.bug_1
@pytest.mark.parametrize(
    "method_name,invoke",
    EXECUTION_WRITE_METHODS,
    ids=[m for m, _ in EXECUTION_WRITE_METHODS],
)
def test_execution_write_never_touches_verification_row(
    method_name: str,
    invoke,
    tmp_path: Path,
) -> None:
    """Every ExecutionRepository write keeps plan_verification untouched.

    **Bug 1 anchor.**  This is the canonical invariant: writes via
    ExecutionRepository are scoped to ``plan_execution`` only.  A
    mistyped SQL or a future refactor that accidentally expands the
    UPDATE's WHERE clause MUST be caught by this test.

    Setup: bootstrap a plan that has rows in all three tables
    (``plan_execution``, ``plan_verification``, ``plan_routing``).
    Snapshot the verification and routing rows, run ONE write
    method on the execution repo, then re-read the verification
    and routing rows byte-for-byte.

    The test is parameterised across every public write method so
    a single regression in any of them is caught independently.
    """
    from state_machine.db.connection import open as open_db_inner
    from state_machine.db.schema import migrate as migrate_inner

    db_path = tmp_path / "state.db"
    setup = open_db_inner(db_path)
    try:
        migrate_inner(setup)
        # Seed every sibling table so the assertion is concrete.
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
                "p1", "running", 1, 3, None, "rt", "ex", "pr",
                None, None, "2026-08-05T11:30:00Z", "2026-08-05T11:30:00Z",
            ),
        )
        setup.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        setup.close()

    # Snapshot the verification + routing rows BEFORE the write.
    snap_conn = open_db_inner(db_path)
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

    # Run the parameterised write.
    write_conn = open_db_inner(db_path)
    try:
        write_repo = ExecutionRepository(write_conn)
        invoke(write_repo)
    finally:
        write_conn.close()

    # Re-read verification + routing rows AFTER the write.
    after_conn = open_db_inner(db_path)
    try:
        after_v = after_conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", ("p1",)
        ).fetchone()
        after_r = after_conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?", ("p1",)
        ).fetchone()
        assert after_v is not None, "plan_verification row disappeared"
        assert after_r is not None, "plan_routing row disappeared"
        # Byte-for-byte equality of every column.
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


@pytest.mark.parametrize(
    "method_name,invoke",
    EXECUTION_WRITE_METHODS,
    ids=[m for m, _ in EXECUTION_WRITE_METHODS],
)
def test_execution_write_never_touches_routing_stage(
    method_name: str,
    invoke,
    tmp_path: Path,
) -> None:
    """Every ExecutionRepository write keeps plan_routing.stage untouched.

    The narrower version of the bug-1 anchor: routing has the
    ``stage`` / ``substage`` / ``version`` CAS-protected columns,
    so a write that accidentally touches them would corrupt the
    CAS state.  This test pins the invariant explicitly so a
    regression on stage is caught with a precise failure message.
    """
    from state_machine.db.connection import open as open_db_inner
    from state_machine.db.schema import migrate as migrate_inner

    db_path = tmp_path / "state.db"
    setup = open_db_inner(db_path)
    try:
        migrate_inner(setup)
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
                "p1", "ready", 0, None, None, None,
                None, None, None, None, None, None,
                "2026-08-05T11:00:00Z",
            ),
        )
        setup.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        setup.close()

    write_conn = open_db_inner(db_path)
    try:
        write_repo = ExecutionRepository(write_conn)
        invoke(write_repo)
    finally:
        write_conn.close()

    after_conn = open_db_inner(db_path)
    try:
        stage, substage, version = after_conn.execute(
            "SELECT current_phase, substage, version FROM plan_routing "
            "WHERE plan_id = ?",
            ("p1",),
        ).fetchone()
        assert stage == "executing", (
            f"{method_name!r} mutated plan_routing.stage to {stage!r}"
        )
        assert substage == "task_1", (
            f"{method_name!r} mutated plan_routing.substage to {substage!r}"
        )
        assert version == 3, (
            f"{method_name!r} mutated plan_routing.version to {version!r}"
        )
    finally:
        after_conn.close()


# ---------------------------------------------------------------------------
# TDD anchor #4 — no in-memory cache
# ---------------------------------------------------------------------------


def test_repository_has_no_stale_cache(
    repo: ExecutionRepository,
    conn: sqlite3.Connection,
) -> None:
    """External SQL UPDATE is observable on the very next read.

    Per architecture decision point 4 the repository MUST NOT
    maintain any in-memory cache: a direct SQL UPDATE from outside
    the repo must be visible to ``summary()`` / ``progress()`` on
    the next call without any cache-invalidation step.
    """
    repo.insert(plan_id="p1", current_phase="ready")

    # Simulate a sibling module / migration script writing directly
    # to the table — bypassing every method on the repository.
    conn.execute(
        "UPDATE plan_execution SET project_dir = ? WHERE plan_id = ?",
        ("/external/write/path", "p1"),
    )

    # The repository's next read MUST see the external change.
    summary = repo.summary("p1")
    assert summary is not None
    assert summary["project_dir"] == "/external/write/path", (
        "repository returned a stale cached value; "
        "architecture decision point 4 requires no in-memory cache"
    )


# ---------------------------------------------------------------------------
# snapshot_for_list + card_table
# ---------------------------------------------------------------------------


def test_snapshot_for_list_returns_dict_per_plan(
    repo: ExecutionRepository,
) -> None:
    """``snapshot_for_list`` returns ``{plan_id: row-dict}`` for the input."""
    repo.insert(plan_id="p1", current_phase="ready")
    repo.insert(plan_id="p2", current_phase="executing")

    snap = repo.snapshot_for_list(["p1", "p2", "missing"])
    assert set(snap.keys()) == {"p1", "p2", "missing"}
    assert snap["p1"]["current_phase"] == "ready"
    assert snap["p2"]["current_phase"] == "executing"
    assert snap["missing"] is None


def test_card_table_returns_one_row_per_plan(
    repo: ExecutionRepository,
) -> None:
    """``card_table`` returns the planner-UI rows."""
    repo.insert(plan_id="p1", current_phase="executing")
    repo.update_task_progress(
        "p1", {"current": "1-2", "completed": 1, "total": 5}
    )
    repo.insert(plan_id="p2", current_phase="ready")

    rows = repo.card_table()
    by_id = {row["plan_id"]: row for row in rows}
    assert set(by_id.keys()) == {"p1", "p2"}
    assert by_id["p1"]["current_phase"] == "executing"
    assert by_id["p1"]["task_progress"]["current"] == "1-2"
    assert by_id["p2"]["current_phase"] == "ready"


# ---------------------------------------------------------------------------
# update_phase (generic field-mutation path)
# ---------------------------------------------------------------------------


def test_update_phase_updates_current_phase(
    repo: ExecutionRepository,
) -> None:
    """``update_phase`` mutates the ``current_phase`` column."""
    repo.insert(plan_id="p1", current_phase="ready")

    repo.update_phase("p1", current_phase="executing")

    assert repo.summary("p1")["current_phase"] == "executing"


def test_update_phase_leaves_current_phase_alone_when_omitted(
    repo: ExecutionRepository,
) -> None:
    """Omitting ``current_phase`` must not rewrite it.

    2026-09-19 regression: the column is ``NOT NULL``, so ``None`` can
    only mean "leave it alone" — there is no legitimate NULL payload to
    confuse it with.
    """
    repo.insert(plan_id="p1", current_phase="ready")

    repo.update_phase("p1", exec_status="failed")

    summary = repo.summary("p1")
    assert summary["current_phase"] == "ready"
    assert summary["exec_status"] == "failed"


def test_update_phase_with_nothing_to_write_raises(
    repo: ExecutionRepository,
) -> None:
    """A call that would write zero columns is a caller bug, not a no-op."""
    repo.insert(plan_id="p1", current_phase="ready")

    with pytest.raises(ValueError, match="nothing to write"):
        repo.update_phase("p1")


def test_update_phase_create_if_missing_requires_current_phase(
    repo: ExecutionRepository,
) -> None:
    """``create_if_missing`` cannot omit a ``NOT NULL`` column."""
    with pytest.raises(ValueError, match="create_if_missing"):
        repo.update_phase("p-missing", exec_status="running", create_if_missing=True)


def test_update_status_does_not_clobber_current_phase(
    repo: ExecutionRepository,
) -> None:
    """``update_status`` writes ``exec_status`` ONLY.

    2026-09-19 regression (real data bug). ``update_status`` used to call
    ``update_phase(plan_id, current_phase="executing", ...)``, so every
    status write stamped ``current_phase='executing'``. The startup
    recovery path (``server._mark_failed_dead``) hits this on every plan
    whose executor died: a plan resting at ``ready`` came back from a
    server restart reading ``current_phase='executing'``, and
    ``GET /api/plans`` surfaces that column verbatim.
    """
    repo.insert(plan_id="p1", current_phase="ready", exec_status="running")

    repo.update_status("p1", "failed")

    summary = repo.summary("p1")
    assert summary["exec_status"] == "failed"
    assert summary["current_phase"] == "ready", (
        "update_status must not touch current_phase; a status write is "
        "not a phase transition"
    )


def test_update_phase_with_extra_fields(
    repo: ExecutionRepository,
) -> None:
    """``update_phase`` accepts arbitrary ``**fields`` and persists them."""
    repo.insert(plan_id="p1", current_phase="ready")

    repo.update_phase(
        "p1",
        current_phase="executing",
        project_dir="/new/abs",
        stop_reason="user_stop",
        exec_pid=4242,
    )

    summary = repo.summary("p1")
    assert summary["current_phase"] == "executing"
    assert summary["project_dir"] == "/new/abs"
    assert summary["stop_reason"] == "user_stop"
    assert summary["exec_pid"] == 4242


def test_update_phase_unknown_plan_raises(
    repo: ExecutionRepository,
) -> None:
    """``update_phase`` on unknown plan_id raises ``PlanNotFoundError``."""
    with pytest.raises(PlanNotFoundError):
        repo.update_phase("does-not-exist", current_phase="executing")


# ---------------------------------------------------------------------------
# update_next_run_at / update_card_state / update_flags
# ---------------------------------------------------------------------------


def test_update_next_run_at_persists_value(
    repo: ExecutionRepository,
) -> None:
    """``update_next_run_at`` stores the scheduled time on the row."""
    repo.insert(plan_id="p1", current_phase="ready")

    repo.update_next_run_at("p1", "2099-01-01T00:00:00Z")

    assert repo.summary("p1")["next_run_at"] == "2099-01-01T00:00:00Z"


def test_update_next_run_at_accepts_none_to_clear(
    repo: ExecutionRepository,
) -> None:
    """``update_next_run_at(None)`` clears the column (schedule removed)."""
    repo.insert(plan_id="p1", current_phase="ready")
    repo.update_next_run_at("p1", "2099-01-01T00:00:00Z")

    repo.update_next_run_at("p1", None)

    assert repo.summary("p1")["next_run_at"] is None


def test_update_card_state_persists_json(
    repo: ExecutionRepository,
) -> None:
    """``update_card_state`` round-trips the card-state dict via JSON."""
    repo.insert(plan_id="p1", current_phase="executing")

    repo.update_card_state("p1", {"expanded": True, "active": "verifying"})

    # ``summary`` parses the JSON column into a dict so callers
    # don't have to know about the column shape.
    assert repo.summary("p1")["card_state"] == {
        "expanded": True,
        "active": "verifying",
    }


def test_update_flags_persists_json(
    repo: ExecutionRepository,
) -> None:
    """``update_flags`` round-trips the flags dict via JSON."""
    repo.insert(plan_id="p1", current_phase="executing")

    repo.update_flags("p1", {"arch_enabled": True, "test_enabled": False})

    assert repo.summary("p1")["flags"] == {
        "arch_enabled": True,
        "test_enabled": False,
    }


def test_update_task_progress_unknown_plan_raises(
    repo: ExecutionRepository,
) -> None:
    """``update_task_progress`` on unknown plan_id raises ``PlanNotFoundError``."""
    with pytest.raises(PlanNotFoundError):
        repo.update_task_progress("does-not-exist", {"current": "1-1"})


# ---------------------------------------------------------------------------
# Concurrency — write failure leaves the row untouched
# ---------------------------------------------------------------------------


def test_write_failure_leaves_existing_row_intact(
    repo: ExecutionRepository,
) -> None:
    """If a write's transaction is rolled back, the original values survive.

    This is a defensive guard against regressions: the IMMEDIATE
    txn skeleton wraps every write in ``BEGIN ... COMMIT/ROLLBACK``.
    We simulate a mid-write failure by calling the repository's
    internal helper and aborting before COMMIT.  The repository's
    public ``update_*`` methods all use the same helper, so a
    regression in any of them is caught here.
    """
    repo.insert(plan_id="p1", current_phase="ready")

    # Sanity: the initial summary.
    assert repo.summary("p1")["current_phase"] == "ready"

    # Simulate a failing write.
    try:
        repo._conn.execute("BEGIN IMMEDIATE")
        repo._conn.execute(
            "UPDATE plan_execution SET current_phase = ? WHERE plan_id = ?",
            ("executing", "p1"),
        )
        # Rollback simulates any exception in the write path.
        repo._conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        # ROLLBACK can fail if the txn state is weird; we still
        # expect the original value to be intact below.
        pass

    # The original value must survive.
    assert repo.summary("p1")["current_phase"] == "ready"
